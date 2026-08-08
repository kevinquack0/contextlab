"""Deterministic orchestration and screening analysis for G2 answer repeats."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation
import hashlib
import math
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence

from .answer_metrics import score_generated_answer
from .baseline import repository_root
from .experiments import METHOD_IDS, load_protocol
from .generations import (
    GenerationBatchError,
    load_public_generation_results,
    run_public_generation_batch,
    validate_generation_manifest_envelope,
)
from .provider import ALLOWED_REASONING_EFFORTS
from .reports import validate_lab
from .static_benchmark import load_public_gold, public_static_tasks
from .statistics import repeat_summary
from .tasking import sha256_json


REPEAT_ANALYSIS_SCHEMA = "contextlab.g2-repeat-analysis.v1"
REPEAT_TRIAL_COUNT = 5
REPEAT_TASK_COUNT = 12
REPEAT_CELL_COUNT = REPEAT_TASK_COUNT * len(METHOD_IDS) * len(ALLOWED_REASONING_EFFORTS)

_TASK_ID = re.compile(r"S\d{3}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SCREENING_METRICS = (
    "accepted_proxy",
    "critical_value_recall",
    "citation_precision",
    "required_evidence_citation_recall",
)


class RepeatError(ValueError):
    """Repeat inputs are incomplete, unsafe, or internally inconsistent."""


def _repeat_contract(root: Path) -> tuple[dict[str, Any], tuple[str, ...]]:
    try:
        protocol = load_protocol(root)
    except Exception as exc:
        raise RepeatError(f"cannot load the G2 repeat protocol: {exc}") from exc
    promotion = protocol.get("promotion")
    if not isinstance(promotion, Mapping):
        raise RepeatError("retrieval protocol has no promotion contract")
    if promotion.get("stochastic_trial_count") != REPEAT_TRIAL_COUNT:
        raise RepeatError("G2 repeats require exactly five stochastic trials")
    raw_task_ids = promotion.get("temperature_zero_repeat_task_ids")
    if not isinstance(raw_task_ids, list) or len(raw_task_ids) != REPEAT_TASK_COUNT:
        raise RepeatError(
            "temperature-zero repeat sample must contain exactly 12 tasks"
        )
    task_ids = tuple(raw_task_ids)
    if len(set(task_ids)) != REPEAT_TASK_COUNT or any(
        not isinstance(task_id, str) or _TASK_ID.fullmatch(task_id) is None
        for task_id in task_ids
    ):
        raise RepeatError(
            "temperature-zero repeat task IDs must be 12 unique public IDs"
        )
    return protocol, task_ids


def _lab_trace_index(
    lab: Mapping[str, Any],
) -> tuple[dict[tuple[str, str], Mapping[str, Any]], tuple[str, ...]]:
    try:
        validate_lab(lab)
    except Exception as exc:
        raise RepeatError(f"public component lab is invalid: {exc}") from exc
    traces = lab.get("traces")
    if not isinstance(traces, list):
        raise RepeatError("public component lab has no trace list")
    index: dict[tuple[str, str], Mapping[str, Any]] = {}
    task_ids: set[str] = set()
    for trace in traces:
        if not isinstance(trace, Mapping):
            raise RepeatError("public component lab contains a non-object trace")
        task = trace.get("task")
        task_id = task.get("task_id") if isinstance(task, Mapping) else None
        strategy = trace.get("strategy_id")
        run_id = trace.get("run_id")
        if (
            not isinstance(task_id, str)
            or _TASK_ID.fullmatch(task_id) is None
            or strategy not in METHOD_IDS
            or not isinstance(run_id, str)
            or not run_id
        ):
            raise RepeatError("public component lab trace identity is invalid")
        key = (task_id, str(strategy))
        if key in index:
            raise RepeatError("public component lab repeats a task/strategy trace")
        index[key] = trace
        task_ids.add(task_id)
    expected = {(task_id, strategy) for task_id in task_ids for strategy in METHOD_IDS}
    if set(index) != expected or len(task_ids) != lab.get("task_count"):
        raise RepeatError("public component lab does not cover every task and strategy")
    return index, tuple(sorted(task_ids))


def _validate_manifest_envelope(
    manifest: Mapping[str, Any],
    *,
    trial: int,
    lab: Mapping[str, Any],
    protocol: Mapping[str, Any],
) -> None:
    try:
        validate_generation_manifest_envelope(
            manifest, lab, protocol, expected_trial=trial
        )
    except GenerationBatchError as exc:
        raise RepeatError(
            f"trial {trial} generation envelope is invalid: {exc}"
        ) from exc
    if manifest.get("strategies") != list(METHOD_IDS):
        raise RepeatError(f"trial {trial} does not use exactly R0 through R7")
    if manifest.get("reasoning_efforts") != list(ALLOWED_REASONING_EFFORTS):
        raise RepeatError(f"trial {trial} does not use exactly low and high effort")


def _manifest_cell_index(
    manifest: Mapping[str, Any],
    *,
    trial: int,
    traces: Mapping[tuple[str, str], Mapping[str, Any]],
    campaign_id: str,
) -> dict[tuple[str, str, str], Mapping[str, Any]]:
    cells = manifest["cells"]
    index: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for cell in cells:
        if not isinstance(cell, Mapping):
            raise RepeatError(f"trial {trial} contains a non-object cell")
        task_id = cell.get("task_id")
        strategy = cell.get("strategy_id")
        effort = cell.get("reasoning_effort")
        if (
            not isinstance(task_id, str)
            or strategy not in METHOD_IDS
            or effort not in ALLOWED_REASONING_EFFORTS
        ):
            raise RepeatError(f"trial {trial} contains an invalid cell identity")
        trace = traces.get((task_id, str(strategy)))
        if trace is None:
            raise RepeatError(
                f"trial {trial} contains a cell outside the component lab"
            )
        key = (task_id, str(strategy), str(effort))
        if key in index:
            raise RepeatError(f"trial {trial} repeats a generation cell")
        expected_run_id = f"{campaign_id}-{task_id}-{strategy}-{effort}-t{trial}"
        if (
            cell.get("trial") != trial
            or cell.get("run_id") != expected_run_id
            or cell.get("trace_run_id") != trace["run_id"]
            or cell.get("status") not in {"completed", "failed", "pending"}
        ):
            raise RepeatError(f"trial {trial} generation cell identity is inconsistent")
        expected_path = (
            Path("results/v2/generations/public")
            / campaign_id
            / f"trial-{trial}"
            / str(strategy)
            / str(effort)
            / f"{task_id}.json"
        )
        result_path = cell.get("result_path")
        if (
            not isinstance(result_path, str)
            or Path(result_path).is_absolute()
            or Path(result_path) != expected_path
        ):
            raise RepeatError(f"trial {trial} generation result path is unsafe")
        result_sha256 = cell.get("result_sha256")
        if cell.get("status") == "completed" and (
            not isinstance(result_sha256, str)
            or _SHA256.fullmatch(result_sha256) is None
        ):
            raise RepeatError(f"trial {trial} completed result hash is invalid")
        index[key] = cell
    return index


def _expected_cells(
    task_ids: Sequence[str],
) -> set[tuple[str, str, str]]:
    return {
        (task_id, strategy, effort)
        for task_id in task_ids
        for strategy in METHOD_IDS
        for effort in ALLOWED_REASONING_EFFORTS
    }


def _validate_selection_counts(
    manifest: Mapping[str, Any],
    *,
    trial: int,
    expected_keys: set[tuple[str, str, str]],
    actual_keys: set[tuple[str, str, str]],
    allow_partial: bool,
) -> None:
    selected_trace_count = len(expected_keys) // len(ALLOWED_REASONING_EFFORTS)
    if manifest.get("selected_trace_count") != selected_trace_count:
        raise RepeatError(f"trial {trial} selected trace count is inconsistent")
    if manifest.get("expected_cell_count") != len(expected_keys):
        raise RepeatError(f"trial {trial} expected cell count is inconsistent")
    if allow_partial:
        if not actual_keys <= expected_keys:
            raise RepeatError(
                f"trial {trial} contains a cell outside the repeat sample"
            )
    elif actual_keys != expected_keys:
        missing = len(expected_keys - actual_keys)
        extra = len(actual_keys - expected_keys)
        raise RepeatError(
            f"trial {trial} coverage is incomplete (missing={missing}, extra={extra})"
        )


def _validate_max_new_calls(value: int | None) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value < 0
    ):
        raise RepeatError("max new calls per trial must be a non-negative integer")


def run_public_generation_repeats(
    lab: Mapping[str, Any],
    trial_one_manifest: Mapping[str, Any],
    *,
    root: Path | None = None,
    max_new_calls_per_trial: int | None = None,
    concurrency: int = 4,
    environment: Mapping[str, str] | None = None,
    generation_runner: Any | None = None,
    batch_runner: Callable[..., Mapping[str, Any]] = run_public_generation_batch,
) -> list[Mapping[str, Any]]:
    """Run or resume trials 2..5 while retaining the full trial-1 manifest."""

    root = (root or repository_root()).resolve()
    _validate_max_new_calls(max_new_calls_per_trial)
    protocol, repeat_task_ids = _repeat_contract(root)
    campaign_id = str(protocol["fixed_comparison"]["generation_campaign_id"])
    traces, lab_task_ids = _lab_trace_index(lab)
    if not set(repeat_task_ids) <= set(lab_task_ids):
        raise RepeatError("repeat protocol names tasks outside the component lab")
    _validate_manifest_envelope(
        trial_one_manifest,
        trial=1,
        lab=lab,
        protocol=protocol,
    )
    trial_one_cells = _manifest_cell_index(
        trial_one_manifest,
        trial=1,
        traces=traces,
        campaign_id=campaign_id,
    )
    full_expected = _expected_cells(lab_task_ids)
    _validate_selection_counts(
        trial_one_manifest,
        trial=1,
        expected_keys=full_expected,
        actual_keys=set(trial_one_cells),
        allow_partial=False,
    )

    manifests: list[Mapping[str, Any]] = [trial_one_manifest]
    repeat_expected = _expected_cells(repeat_task_ids)
    for trial in range(2, REPEAT_TRIAL_COUNT + 1):
        call_args: dict[str, Any] = {
            "root": root,
            "strategies": METHOD_IDS,
            "efforts": ALLOWED_REASONING_EFFORTS,
            "task_ids": repeat_task_ids,
            "trial": trial,
            "max_new_calls": max_new_calls_per_trial,
            "concurrency": concurrency,
            "environment": environment,
        }
        if generation_runner is not None:
            call_args["generation_runner"] = generation_runner
        generated = batch_runner(lab, **call_args)
        _validate_manifest_envelope(
            generated,
            trial=trial,
            lab=lab,
            protocol=protocol,
        )
        generated_cells = _manifest_cell_index(
            generated,
            trial=trial,
            traces=traces,
            campaign_id=campaign_id,
        )
        _validate_selection_counts(
            generated,
            trial=trial,
            expected_keys=repeat_expected,
            actual_keys=set(generated_cells),
            allow_partial=max_new_calls_per_trial is not None,
        )
        manifests.append(generated)
    return manifests


def _safe_result_projection(
    manifest: Mapping[str, Any], cells: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    projection = {
        key: value for key, value in manifest.items() if key != "manifest_sha256"
    }
    projection["cells"] = list(cells)
    projection["recorded_cell_count"] = len(cells)
    projection["status_counts"] = {
        "completed": len(cells),
        "failed": 0,
        "pending": 0,
    }
    projection["manifest_sha256"] = sha256_json(projection)
    return projection


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise RepeatError(f"{label} must be a finite non-negative number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise RepeatError(f"{label} must be a finite non-negative number") from exc
    if not number.is_finite() or number < 0:
        raise RepeatError(f"{label} must be a finite non-negative number")
    return number


def _float_number(value: Any, label: str) -> float:
    number = float(_decimal(value, label))
    if not math.isfinite(number):
        raise RepeatError(f"{label} cannot be represented as a finite number")
    return number


def _load_selected_results(
    manifest: Mapping[str, Any],
    selected_cells: Sequence[Mapping[str, Any]],
    *,
    trial: int,
    root: Path,
) -> dict[str, Mapping[str, Any]]:
    public_root = (root / "results/v2/generations/public").resolve()
    for cell in selected_cells:
        path = (root / str(cell["result_path"])).resolve()
        try:
            path.relative_to(public_root)
        except ValueError as exc:
            raise RepeatError(
                f"trial {trial} result resolves outside the public run tree"
            ) from exc
    projection = _safe_result_projection(manifest, selected_cells)
    try:
        loaded = load_public_generation_results(projection, root)
    except Exception as exc:
        raise RepeatError(f"trial {trial} completed result scan failed: {exc}") from exc
    by_run: dict[str, Mapping[str, Any]] = {}
    cells_by_run = {str(cell["run_id"]): cell for cell in selected_cells}
    for result in loaded:
        if not isinstance(result, Mapping):
            raise RepeatError(f"trial {trial} contains a non-object generation result")
        run_id = result.get("run_id")
        cell = cells_by_run.get(str(run_id))
        metadata = result.get("metadata")
        if (
            cell is None
            or result.get("schema_version") != "contextlab.generation-result.v1"
            or result.get("task_id") != cell["task_id"]
            or not isinstance(result.get("answer"), str)
            or not isinstance(metadata, Mapping)
        ):
            raise RepeatError(f"trial {trial} completed result identity is invalid")
        if run_id in by_run:
            raise RepeatError(f"trial {trial} repeats a completed result")
        cell_cost = _decimal(cell.get("actual_usd"), f"trial {trial} cell cost")
        result_cost = _decimal(metadata.get("actual_usd"), f"trial {trial} result cost")
        cell_latency = _decimal(cell.get("latency_ms"), f"trial {trial} cell latency")
        result_latency = _decimal(
            metadata.get("latency_ms"), f"trial {trial} result latency"
        )
        if cell_cost != result_cost or cell_latency != result_latency:
            raise RepeatError(
                f"trial {trial} result accounting differs from its manifest"
            )
        by_run[str(run_id)] = result
    if set(by_run) != set(cells_by_run):
        raise RepeatError(
            f"trial {trial} completed results do not cover the repeat sample"
        )
    return by_run


def _context_references(trace: Mapping[str, Any]) -> tuple[str, ...]:
    candidates = trace.get("selected_candidates")
    if not isinstance(candidates, list):
        raise RepeatError("component trace has no selected candidate list")
    references: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise RepeatError("component trace contains a non-object candidate")
        reference = candidate.get("section_id") or candidate.get("source_id")
        if not isinstance(reference, str) or not reference:
            raise RepeatError("component trace candidate has no evidence reference")
        references.append(reference)
    return tuple(references)


def _score_selected_results(
    selected: Mapping[tuple[str, str, str], Mapping[str, Any]],
    results: Mapping[str, Mapping[str, Any]],
    traces: Mapping[tuple[str, str], Mapping[str, Any]],
    gold: Mapping[str, Mapping[str, Any]],
    tasks: Mapping[str, Mapping[str, Any]],
    *,
    trial: int,
) -> dict[tuple[str, str, str], dict[str, Any]]:
    scored: dict[tuple[str, str, str], dict[str, Any]] = {}
    for key, cell in selected.items():
        task_id, strategy, _ = key
        gold_row = gold.get(task_id)
        task_row = tasks.get(task_id)
        if gold_row is None or task_row is None:
            raise RepeatError(f"repeat task {task_id} lacks public scorer data")
        result = results[str(cell["run_id"])]
        try:
            metrics = score_generated_answer(
                str(result["answer"]),
                str(gold_row["expected_answer"]),
                gold_row["required_evidence"],
                _context_references(traces[(task_id, strategy)]),
                abstention_task=task_row.get("task_family") == "abstention",
            )
        except RepeatError:
            raise
        except Exception as exc:
            raise RepeatError(
                f"trial {trial} deterministic screening failed for {cell['run_id']}"
            ) from exc
        scored[key] = {
            "answer_sha256": hashlib.sha256(
                str(result["answer"]).encode("utf-8")
            ).hexdigest(),
            "metrics": metrics,
            "actual_usd": _float_number(
                cell.get("actual_usd"), f"trial {trial} cell cost"
            ),
            "latency_ms": _float_number(
                cell.get("latency_ms"), f"trial {trial} cell latency"
            ),
        }
    return scored


def _metric_value(row: Mapping[str, Any], metric: str) -> float:
    if metric == "accepted_proxy":
        value = row["metrics"].get(metric)
        if not isinstance(value, bool):
            raise RepeatError("accepted_proxy screening output must be boolean")
        return 1.0 if value else 0.0
    value = _float_number(row["metrics"].get(metric), f"screening metric {metric}")
    if value > 1.0:
        raise RepeatError(f"screening metric {metric} must be within zero and one")
    return value


def analyze_public_generation_repeats(
    manifests: Sequence[Mapping[str, Any]],
    lab: Mapping[str, Any],
    root: Path | None = None,
) -> dict[str, Any]:
    """Validate and summarize five zero-temperature public generation trials."""

    root = (root or repository_root()).resolve()
    protocol, repeat_task_ids = _repeat_contract(root)
    campaign_id = str(protocol["fixed_comparison"]["generation_campaign_id"])
    traces, lab_task_ids = _lab_trace_index(lab)
    if not set(repeat_task_ids) <= set(lab_task_ids):
        raise RepeatError("repeat protocol names tasks outside the component lab")
    if (
        isinstance(manifests, (str, bytes))
        or not isinstance(manifests, Sequence)
        or len(manifests) != REPEAT_TRIAL_COUNT
    ):
        raise RepeatError("repeat analysis requires exactly five generation manifests")

    lab_sha256 = str(lab["artifact_sha256"])
    full_expected = _expected_cells(lab_task_ids)
    repeat_expected = _expected_cells(repeat_task_ids)
    selected_by_trial: list[dict[tuple[str, str, str], Mapping[str, Any]]] = []
    manifest_identity: tuple[Any, Any, Any] | None = None
    for trial, manifest in enumerate(manifests, start=1):
        _validate_manifest_envelope(
            manifest,
            trial=trial,
            lab=lab,
            protocol=protocol,
        )
        identity = (
            manifest.get("prompt_version"),
            manifest.get("prompt_sha256"),
            manifest.get("requested_model"),
        )
        if manifest_identity is None:
            manifest_identity = identity
        elif identity != manifest_identity:
            raise RepeatError(
                "repeat manifests use different prompt or model contracts"
            )
        index = _manifest_cell_index(
            manifest,
            trial=trial,
            traces=traces,
            campaign_id=campaign_id,
        )
        expected = full_expected if trial == 1 else repeat_expected
        _validate_selection_counts(
            manifest,
            trial=trial,
            expected_keys=expected,
            actual_keys=set(index),
            allow_partial=False,
        )
        selected = {key: cell for key, cell in index.items() if key in repeat_expected}
        if set(selected) != repeat_expected or any(
            cell["status"] != "completed" for cell in selected.values()
        ):
            raise RepeatError(
                f"trial {trial} must have 192 completed repeat-sample cells"
            )
        selected_by_trial.append(selected)

    try:
        gold_rows = load_public_gold(root)
        task_rows = public_static_tasks(root)
    except Exception as exc:
        raise RepeatError(
            f"cannot load public deterministic scorer data: {exc}"
        ) from exc
    if any(not isinstance(row, Mapping) for row in (*gold_rows, *task_rows)):
        raise RepeatError("public deterministic scorer data contains a non-object row")
    gold = {str(row.get("task_id")): row for row in gold_rows}
    tasks = {str(row.get("task_id")): row for row in task_rows}
    if len(gold) != len(gold_rows) or len(tasks) != len(task_rows):
        raise RepeatError("public deterministic scorer data repeats a task ID")

    scored_by_trial: list[dict[tuple[str, str, str], dict[str, Any]]] = []
    for trial, (manifest, selected) in enumerate(
        zip(manifests, selected_by_trial), start=1
    ):
        ordered_cells = [selected[key] for key in sorted(selected)]
        results = _load_selected_results(
            manifest, ordered_cells, trial=trial, root=root
        )
        scored_by_trial.append(
            _score_selected_results(selected, results, traces, gold, tasks, trial=trial)
        )

    per_cell: list[dict[str, Any]] = []
    consistent_count = 0
    modal_shares: list[float] = []
    matching_pairs = 0
    pair_count = REPEAT_TRIAL_COUNT * (REPEAT_TRIAL_COUNT - 1) // 2
    aggregate_trial_values: dict[str, list[list[float]]] = {
        metric: [[] for _ in range(REPEAT_TRIAL_COUNT)]
        for metric in (*_SCREENING_METRICS, "actual_usd", "latency_ms")
    }
    for key in sorted(repeat_expected):
        rows = [trial_rows[key] for trial_rows in scored_by_trial]
        answer_hashes = [str(row["answer_sha256"]) for row in rows]
        hash_counts = Counter(answer_hashes)
        consistent = len(hash_counts) == 1
        consistent_count += int(consistent)
        modal_share = max(hash_counts.values()) / REPEAT_TRIAL_COUNT
        modal_shares.append(modal_share)
        matching_pairs += sum(
            count * (count - 1) // 2 for count in hash_counts.values()
        )
        summaries: dict[str, Any] = {}
        for metric in _SCREENING_METRICS:
            values = [_metric_value(row, metric) for row in rows]
            summaries[metric] = repeat_summary(values)
            for trial_index, value in enumerate(values):
                aggregate_trial_values[metric][trial_index].append(value)
        for metric in ("actual_usd", "latency_ms"):
            values = [float(row[metric]) for row in rows]
            summaries[metric] = repeat_summary(values)
            for trial_index, value in enumerate(values):
                aggregate_trial_values[metric][trial_index].append(value)
        task_id, strategy, effort = key
        per_cell.append(
            {
                "task_id": task_id,
                "strategy_id": strategy,
                "reasoning_effort": effort,
                "trial_answer_sha256": answer_hashes,
                "unique_answer_sha256_count": len(hash_counts),
                "answer_hash_consistent": consistent,
                "modal_answer_hash_share": modal_share,
                "repeat_summary": summaries,
            }
        )

    aggregate_summaries: dict[str, Any] = {}
    for metric, trial_values in aggregate_trial_values.items():
        if metric == "actual_usd":
            values = [sum(values_for_trial) for values_for_trial in trial_values]
        else:
            values = [
                sum(values_for_trial) / len(values_for_trial)
                for values_for_trial in trial_values
            ]
        aggregate_summaries[metric] = repeat_summary(values)

    trial_match_rates = [
        sum(
            scored_by_trial[trial_index][key]["answer_sha256"]
            == scored_by_trial[0][key]["answer_sha256"]
            for key in repeat_expected
        )
        / REPEAT_CELL_COUNT
        for trial_index in range(REPEAT_TRIAL_COUNT)
    ]

    aggregate_consistency = {
        "cell_count": REPEAT_CELL_COUNT,
        "exact_match_cell_count": consistent_count,
        "exact_match_cell_rate": consistent_count / REPEAT_CELL_COUNT,
        "mean_modal_answer_hash_share": sum(modal_shares) / len(modal_shares),
        "matching_trial_pair_count": matching_pairs,
        "trial_pair_count": REPEAT_CELL_COUNT * pair_count,
        "trial_pair_match_rate": matching_pairs / (REPEAT_CELL_COUNT * pair_count),
        "repeat_summary": repeat_summary(trial_match_rates),
    }
    payload: dict[str, Any] = {
        "schema_version": REPEAT_ANALYSIS_SCHEMA,
        "scope": "public_temperature_zero_deterministic_screening",
        "protocol_sha256": sha256_json(protocol),
        "component_lab_sha256": lab_sha256,
        "trial_count": REPEAT_TRIAL_COUNT,
        "repeat_task_ids": list(repeat_task_ids),
        "expected_cell_count_per_trial": REPEAT_CELL_COUNT,
        "generation_manifests": [
            {
                "trial": trial,
                "manifest_sha256": manifest["manifest_sha256"],
            }
            for trial, manifest in enumerate(manifests, start=1)
        ],
        "aggregate_consistency": aggregate_consistency,
        "aggregate_repeat_summary": aggregate_summaries,
        "cells": per_cell,
        "limitations": [
            "Exact answer hashes measure reproducibility, not semantic equivalence.",
            "Deterministic screening metrics are not correctness grades.",
        ],
    }
    payload["analysis_sha256"] = sha256_json(payload)
    return payload
