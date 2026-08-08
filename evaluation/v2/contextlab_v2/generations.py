"""Resumable fixed-provider answer generation for saved G2 retrieval traces."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .baseline import repository_root
from .costs import CostLedger, canonical_ledger_path
from .experiments import load_protocol
from .gateway import run_paid_generation_to_file
from .provider import (
    ALLOWED_REASONING_EFFORTS,
    MODEL_ID,
    ProviderContractError,
    validate_resolved_response,
)
from .reports import validate_lab
from .statistics import distribution_summary
from .tasking import sha256_json, validate_prompt_safe_task


GENERATION_MANIFEST_SCHEMA = "contextlab.g2-generation-manifest.v1"
GENERATION_SPEC_SCHEMA = "contextlab.generation-spec.v1"
PROMPT_VERSION = "contextlab.retrieval-answer.v1"


class GenerationBatchError(ValueError):
    """A saved component trace cannot enter the fixed answer-generation batch."""


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_answer_instruction(root: Path | None = None) -> str:
    root = (root or repository_root()).resolve()
    prompt = root / "evaluation/v2/prompts/retrieval_answer_v1.md"
    text = prompt.read_text(encoding="utf-8").strip()
    if not text:
        raise GenerationBatchError("retrieval answer instruction is empty")
    return text


def build_generation_spec(
    trace: Mapping[str, Any],
    effort: str,
    *,
    trial: int,
    max_tokens: int,
    temperature: float,
    system_instruction: str,
    campaign_id: str = "g2",
) -> dict[str, Any]:
    task = trace.get("task")
    if not isinstance(task, dict):
        raise GenerationBatchError("retrieval trace has no prompt-safe task")
    validate_prompt_safe_task(task)
    strategy = str(trace.get("strategy_id", ""))
    if not strategy or effort not in ALLOWED_REASONING_EFFORTS:
        raise GenerationBatchError("generation cell has an invalid strategy or effort")
    if isinstance(trial, bool) or not isinstance(trial, int) or trial < 1:
        raise GenerationBatchError("generation trial must be a positive integer")
    if (
        not campaign_id
        or len(campaign_id) > 32
        or any(
            not (character.isalnum() or character in "-_") for character in campaign_id
        )
    ):
        raise GenerationBatchError("generation campaign ID is invalid")
    rendered_context = trace.get("rendered_context")
    if not isinstance(rendered_context, str):
        raise GenerationBatchError("retrieval trace has no rendered context")
    run_id = f"{campaign_id}-{task['task_id']}-{strategy}-{effort}-t{trial}"
    return {
        "schema_version": GENERATION_SPEC_SCHEMA,
        "run_id": run_id,
        "task": dict(task),
        "system_instruction": system_instruction,
        "rendered_context": rendered_context,
        "reasoning_effort": effort,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }


def generation_output_path(
    root: Path,
    spec: Mapping[str, Any],
    strategy: str,
    effort: str,
    trial: int,
    campaign_id: str,
) -> Path:
    return (
        root
        / "results/v2/generations/public"
        / campaign_id
        / f"trial-{trial}"
        / strategy
        / effort
        / f"{spec['task']['task_id']}.json"
    )


def generation_manifest_path(root: Path, trial: int, campaign_id: str) -> Path:
    return (
        root
        / "results/v2/generations"
        / f"public_{campaign_id}_trial_{trial}_manifest.json"
    )


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def validate_generation_manifest_envelope(
    manifest: Mapping[str, Any],
    lab: Mapping[str, Any],
    protocol: Mapping[str, Any],
    *,
    expected_trial: int | None = None,
) -> None:
    """Bind a saved generation manifest to one lab, protocol, and campaign."""
    if not isinstance(manifest, Mapping):
        raise GenerationBatchError("generation manifest is not an object")
    validate_lab(lab)
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if manifest.get("schema_version") != GENERATION_MANIFEST_SCHEMA or manifest.get(
        "manifest_sha256"
    ) != sha256_json(body):
        raise GenerationBatchError("generation manifest hash is invalid")

    comparison = protocol.get("fixed_comparison")
    if not isinstance(comparison, Mapping):
        raise GenerationBatchError("generation protocol has no fixed comparison")
    campaign_id = comparison.get("generation_campaign_id")
    protocol_sha256 = sha256_json(protocol)
    if manifest.get("generation_campaign_id") != campaign_id:
        raise GenerationBatchError("generation manifest campaign identity changed")
    if (
        manifest.get("generation_protocol_sha256") != protocol_sha256
        or manifest.get("output_token_limit") != comparison.get("output_token_limit")
        or lab.get("protocol_sha256") != protocol_sha256
    ):
        raise GenerationBatchError("generation protocol identity changed")
    if manifest.get("component_lab_sha256") != lab.get("artifact_sha256"):
        raise GenerationBatchError(
            "generation manifest points to a different component lab"
        )

    trial = manifest.get("trial")
    if (
        isinstance(trial, bool)
        or not isinstance(trial, int)
        or trial < 1
        or (expected_trial is not None and trial != expected_trial)
    ):
        raise GenerationBatchError("generation manifest trial identity changed")
    strategies = manifest.get("strategies")
    efforts = manifest.get("reasoning_efforts")
    allowed_strategies = {f"R{number}" for number in range(8)}
    if (
        not isinstance(strategies, list)
        or not strategies
        or any(not isinstance(strategy, str) for strategy in strategies)
        or len(strategies) != len(set(strategies))
        or any(strategy not in allowed_strategies for strategy in strategies)
        or not isinstance(efforts, list)
        or not efforts
        or any(not isinstance(effort, str) for effort in efforts)
        or len(efforts) != len(set(efforts))
        or any(effort not in ALLOWED_REASONING_EFFORTS for effort in efforts)
    ):
        raise GenerationBatchError("generation manifest selection is invalid")
    if (
        manifest.get("prompt_version") != PROMPT_VERSION
        or not _is_sha256(manifest.get("prompt_sha256"))
        or manifest.get("requested_model") != MODEL_ID
    ):
        raise GenerationBatchError("generation prompt or model identity changed")

    cells = manifest.get("cells")
    selected_trace_count = manifest.get("selected_trace_count")
    expected_cell_count = manifest.get("expected_cell_count")
    recorded_cell_count = manifest.get("recorded_cell_count")
    new_call_count = manifest.get("new_call_count")
    status_counts = manifest.get("status_counts")
    if (
        not isinstance(cells, list)
        or isinstance(selected_trace_count, bool)
        or not isinstance(selected_trace_count, int)
        or selected_trace_count <= 0
        or expected_cell_count != selected_trace_count * len(efforts)
        or recorded_cell_count != len(cells)
        or len(cells) > expected_cell_count
        or isinstance(new_call_count, bool)
        or not isinstance(new_call_count, int)
        or not 0 <= new_call_count <= len(cells)
        or not isinstance(status_counts, Mapping)
        or set(status_counts) != {"completed", "failed", "pending"}
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in status_counts.values()
        )
        or sum(status_counts.values()) != len(cells)
    ):
        raise GenerationBatchError("generation manifest counts are inconsistent")

    trace_index: dict[str, Mapping[str, Any]] = {}
    for trace in lab["traces"]:
        if not isinstance(trace, Mapping) or not isinstance(trace.get("run_id"), str):
            raise GenerationBatchError("component lab contains an invalid trace")
        trace_run_id = str(trace["run_id"])
        if trace_run_id in trace_index:
            raise GenerationBatchError("component lab repeats a trace run ID")
        trace_index[trace_run_id] = trace
    seen_cells: set[tuple[str, str, str]] = set()
    observed_statuses = {status: 0 for status in status_counts}
    for cell in cells:
        if not isinstance(cell, Mapping):
            raise GenerationBatchError("generation manifest contains a non-object cell")
        task_id = cell.get("task_id")
        strategy = cell.get("strategy_id")
        effort = cell.get("reasoning_effort")
        trace = trace_index.get(str(cell.get("trace_run_id")))
        trace_task = trace.get("task") if trace is not None else None
        if (
            not isinstance(task_id, str)
            or strategy not in strategies
            or effort not in efforts
            or trace is None
            or trace.get("strategy_id") != strategy
            or not isinstance(trace_task, Mapping)
            or trace_task.get("task_id") != task_id
        ):
            raise GenerationBatchError("generation manifest cell is outside its lab")
        key = (task_id, str(strategy), str(effort))
        status = cell.get("status")
        expected_run_id = f"{campaign_id}-{task_id}-{strategy}-{effort}-t{trial}"
        expected_path = (
            Path("results/v2/generations/public")
            / str(campaign_id)
            / f"trial-{trial}"
            / str(strategy)
            / str(effort)
            / f"{task_id}.json"
        )
        result_path = cell.get("result_path")
        if (
            key in seen_cells
            or cell.get("trial") != trial
            or cell.get("run_id") != expected_run_id
            or not isinstance(status, str)
            or status not in status_counts
            or not isinstance(result_path, str)
            or Path(result_path).is_absolute()
            or Path(result_path) != expected_path
            or (status == "completed" and not _is_sha256(cell.get("result_sha256")))
        ):
            raise GenerationBatchError("generation manifest cell identity changed")
        seen_cells.add(key)
        observed_statuses[str(status)] += 1
    if observed_statuses != dict(status_counts):
        raise GenerationBatchError("generation manifest status counts changed")


def validate_saved_generation_result(
    value: Mapping[str, Any],
    *,
    expected_run_id: str,
    expected_task_id: str,
    expected_effort: str,
) -> None:
    """Reject cached answers that do not prove the pinned paid route and identity."""
    if (
        value.get("schema_version") != "contextlab.generation-result.v1"
        or value.get("run_id") != expected_run_id
        or value.get("task_id") != expected_task_id
        or not isinstance(value.get("answer"), str)
    ):
        raise GenerationBatchError("saved generation result identity is invalid")
    metadata = value.get("metadata")
    if not isinstance(metadata, Mapping):
        raise GenerationBatchError("saved generation result has no metadata")
    if (
        metadata.get("requested_model") != MODEL_ID
        or metadata.get("reasoning_effort") != expected_effort
        or metadata.get("retry_count") != 0
        or not isinstance(metadata.get("request_id"), str)
        or not metadata["request_id"]
    ):
        raise GenerationBatchError("saved generation result route identity is invalid")
    for field in ("prompt_tokens", "completion_tokens"):
        value_field = metadata.get(field)
        if (
            isinstance(value_field, bool)
            or not isinstance(value_field, int)
            or value_field < 0
        ):
            raise GenerationBatchError("saved generation token accounting is invalid")
    try:
        actual = Decimal(str(metadata.get("actual_usd")))
        latency = float(metadata.get("latency_ms"))
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise GenerationBatchError("saved generation accounting is invalid") from exc
    if (
        not actual.is_finite()
        or actual < 0
        or not math.isfinite(latency)
        or latency < 0
    ):
        raise GenerationBatchError("saved generation accounting is invalid")
    try:
        validate_resolved_response(metadata, requested_effort=expected_effort)
    except ProviderContractError as exc:
        raise GenerationBatchError(
            "saved generation provider route is invalid"
        ) from exc


def _saved_status(
    path: Path,
    expected_run_id: str,
    expected_task_id: str,
    expected_effort: str,
) -> tuple[str, dict[str, Any] | None]:
    if not path.exists():
        return "missing", None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GenerationBatchError(f"saved generation is unreadable: {path}") from exc
    if not isinstance(value, dict) or value.get("run_id") != expected_run_id:
        raise GenerationBatchError(f"saved generation identity differs: {path}")
    schema = value.get("schema_version")
    if schema == "contextlab.generation-result.v1":
        validate_saved_generation_result(
            value,
            expected_run_id=expected_run_id,
            expected_task_id=expected_task_id,
            expected_effort=expected_effort,
        )
        return "completed", value
    if schema == "contextlab.failed-generation-result.v1":
        return "failed", value
    if schema == "contextlab.pending-generation-result.v1":
        return "pending", value
    raise GenerationBatchError(f"saved generation schema is unsupported: {path}")


def _cell_record(
    *,
    trace: Mapping[str, Any],
    effort: str,
    trial: int,
    spec: Mapping[str, Any],
    path: Path,
    root: Path,
    status: str,
    result: Mapping[str, Any] | None,
) -> dict[str, Any]:
    metadata = result.get("metadata") if isinstance(result, Mapping) else None
    return {
        "run_id": spec["run_id"],
        "task_id": spec["task"]["task_id"],
        "strategy_id": trace["strategy_id"],
        "reasoning_effort": effort,
        "trial": trial,
        "status": status,
        "trace_run_id": trace["run_id"],
        "trace_context_sha256": trace["rendered_context_sha256"],
        "spec_sha256": sha256_json(spec),
        "result_path": str(path.relative_to(root)),
        "result_sha256": _sha256_file(path) if path.exists() else None,
        "request_id": metadata.get("request_id")
        if isinstance(metadata, Mapping)
        else None,
        "requested_model": MODEL_ID,
        "resolved_model": metadata.get("resolved_model")
        if isinstance(metadata, Mapping)
        else None,
        "provider": metadata.get("provider") if isinstance(metadata, Mapping) else None,
        "prompt_tokens": metadata.get("prompt_tokens")
        if isinstance(metadata, Mapping)
        else None,
        "completion_tokens": metadata.get("completion_tokens")
        if isinstance(metadata, Mapping)
        else None,
        "native_prompt_tokens": metadata.get("native_prompt_tokens")
        if isinstance(metadata, Mapping)
        else None,
        "native_completion_tokens": metadata.get("native_completion_tokens")
        if isinstance(metadata, Mapping)
        else None,
        "native_reasoning_tokens": metadata.get("native_reasoning_tokens")
        if isinstance(metadata, Mapping)
        else None,
        "cached_prompt_tokens": metadata.get("cached_prompt_tokens")
        if isinstance(metadata, Mapping)
        else None,
        "actual_usd": metadata.get("actual_usd")
        if isinstance(metadata, Mapping)
        else None,
        "cost_source": metadata.get("cost_source")
        if isinstance(metadata, Mapping)
        else None,
        "latency_ms": metadata.get("latency_ms")
        if isinstance(metadata, Mapping)
        else None,
        "latency_source": metadata.get("latency_source")
        if isinstance(metadata, Mapping)
        else None,
        "generation_time_ms": metadata.get("generation_time_ms")
        if isinstance(metadata, Mapping)
        else None,
        "local_round_trip_ms": metadata.get("local_round_trip_ms")
        if isinstance(metadata, Mapping)
        else None,
        "retry_count": metadata.get("retry_count")
        if isinstance(metadata, Mapping)
        else None,
        "error": result.get("error") if isinstance(result, Mapping) else None,
    }


def _validate_selection(
    values: Iterable[str], allowed: set[str], label: str
) -> tuple[str, ...]:
    selected = tuple(dict.fromkeys(values))
    if not selected or any(value not in allowed for value in selected):
        raise GenerationBatchError(f"invalid {label} selection")
    return selected


def run_public_generation_batch(
    lab: Mapping[str, Any],
    *,
    root: Path | None = None,
    strategies: Sequence[str] = tuple(f"R{number}" for number in range(8)),
    efforts: Sequence[str] = ALLOWED_REASONING_EFFORTS,
    task_ids: Sequence[str] | None = None,
    trial: int = 1,
    max_new_calls: int | None = None,
    concurrency: int = 4,
    environment: Mapping[str, str] | None = None,
    generation_runner: Any = run_paid_generation_to_file,
) -> dict[str, Any]:
    """Run fresh cells once, resume completed cells, and never retry failed cells."""
    root = (root or repository_root()).resolve()
    validate_lab(lab)
    strategy_selection = _validate_selection(
        strategies, {f"R{number}" for number in range(8)}, "strategy"
    )
    effort_selection = _validate_selection(
        efforts, set(ALLOWED_REASONING_EFFORTS), "reasoning effort"
    )
    if (
        isinstance(concurrency, bool)
        or not isinstance(concurrency, int)
        or not 1 <= concurrency <= 16
    ):
        raise GenerationBatchError("generation concurrency must be within 1..16")
    if max_new_calls is not None and (
        isinstance(max_new_calls, bool)
        or not isinstance(max_new_calls, int)
        or max_new_calls < 0
    ):
        raise GenerationBatchError("max new calls must be a non-negative integer")
    traces = [
        trace
        for trace in lab["traces"]
        if trace["strategy_id"] in strategy_selection
        and (task_ids is None or trace["task"]["task_id"] in set(task_ids))
    ]
    if not traces:
        raise GenerationBatchError("generation selection contains no saved traces")
    instruction = load_answer_instruction(root)
    protocol = load_protocol(root)
    comparison = protocol["fixed_comparison"]
    campaign_id = str(comparison["generation_campaign_id"])
    protocol_sha256 = sha256_json(protocol)
    if lab.get("protocol_sha256") != protocol_sha256:
        raise GenerationBatchError(
            "component lab and generation protocol commitments differ"
        )
    cells: list[dict[str, Any]] = []
    new_work: list[tuple[Mapping[str, Any], str, dict[str, Any], Path]] = []
    for trace in traces:
        for effort in effort_selection:
            spec = build_generation_spec(
                trace,
                effort,
                trial=trial,
                max_tokens=int(comparison["output_token_limit"]),
                temperature=float(comparison["temperature"]),
                system_instruction=instruction,
                campaign_id=campaign_id,
            )
            path = generation_output_path(
                root,
                spec,
                str(trace["strategy_id"]),
                effort,
                trial,
                campaign_id,
            )
            status, saved = _saved_status(
                path,
                str(spec["run_id"]),
                str(spec["task"]["task_id"]),
                effort,
            )
            if status == "missing":
                new_work.append((trace, effort, spec, path))
            else:
                cells.append(
                    _cell_record(
                        trace=trace,
                        effort=effort,
                        trial=trial,
                        spec=spec,
                        path=path,
                        root=root,
                        status=status,
                        result=saved,
                    )
                )
    if max_new_calls is not None:
        new_work = new_work[:max_new_calls]

    def run_one(
        work: tuple[Mapping[str, Any], str, dict[str, Any], Path],
    ) -> dict[str, Any]:
        trace, effort, spec, path = work
        result: Mapping[str, Any] | None
        status = "completed"
        try:
            result = generation_runner(
                spec,
                path,
                ledger=CostLedger(canonical_ledger_path(root)),
                environment=environment,
                root=root,
            )
        except Exception:
            status, saved = _saved_status(
                path,
                str(spec["run_id"]),
                str(spec["task"]["task_id"]),
                effort,
            )
            result = saved
            if status not in {"failed", "pending"}:
                raise
        return _cell_record(
            trace=trace,
            effort=effort,
            trial=trial,
            spec=spec,
            path=path,
            root=root,
            status=status,
            result=result,
        )

    if new_work:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(run_one, work) for work in new_work]
            for future in as_completed(futures):
                cells.append(future.result())
    cells.sort(
        key=lambda row: (
            str(row["task_id"]),
            str(row["strategy_id"]),
            str(row["reasoning_effort"]),
        )
    )
    costs = [
        Decimal(str(row["actual_usd"]))
        for row in cells
        if row["actual_usd"] is not None
    ]
    latencies = [
        float(row["latency_ms"])
        for row in cells
        if isinstance(row["latency_ms"], (int, float))
        and not isinstance(row["latency_ms"], bool)
        and math.isfinite(float(row["latency_ms"]))
    ]
    manifest: dict[str, Any] = {
        "schema_version": GENERATION_MANIFEST_SCHEMA,
        "component_lab_sha256": lab["artifact_sha256"],
        "generation_campaign_id": campaign_id,
        "generation_protocol_sha256": protocol_sha256,
        "output_token_limit": int(comparison["output_token_limit"]),
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
        "requested_model": MODEL_ID,
        "strategies": list(strategy_selection),
        "reasoning_efforts": list(effort_selection),
        "trial": trial,
        "selected_trace_count": len(traces),
        "expected_cell_count": len(traces) * len(effort_selection),
        "recorded_cell_count": len(cells),
        "new_call_count": len(new_work),
        "status_counts": {
            status: sum(row["status"] == status for row in cells)
            for status in ("completed", "failed", "pending")
        },
        "actual_usd": str(sum(costs, Decimal("0"))),
        "latency_ms": distribution_summary(latencies) if latencies else None,
        "cells": cells,
    }
    manifest["manifest_sha256"] = sha256_json(manifest)
    output = generation_manifest_path(root, trial, campaign_id)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def load_public_generation_results(
    manifest: Mapping[str, Any], root: Path | None = None
) -> list[dict[str, Any]]:
    root = (root or repository_root()).resolve()
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if manifest.get("schema_version") != GENERATION_MANIFEST_SCHEMA or manifest.get(
        "manifest_sha256"
    ) != sha256_json(body):
        raise GenerationBatchError("generation manifest is invalid")
    results: list[dict[str, Any]] = []
    for cell in manifest["cells"]:
        if cell["status"] != "completed":
            continue
        path = root / str(cell["result_path"])
        result = json.loads(path.read_text(encoding="utf-8"))
        if _sha256_file(path) != cell["result_sha256"]:
            raise GenerationBatchError(
                "saved generation hash differs from its manifest"
            )
        validate_saved_generation_result(
            result,
            expected_run_id=str(cell["run_id"]),
            expected_task_id=str(cell["task_id"]),
            expected_effort=str(cell["reasoning_effort"]),
        )
        results.append(result)
    return results
