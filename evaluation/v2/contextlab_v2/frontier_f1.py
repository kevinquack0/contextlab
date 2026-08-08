"""Provider-free preparation and generation-result contracts for frontier F1."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any

from .credentials import redact
from .costs import canonical_ledger_path
from .immutable_io import ImmutableIOError, write_bytes_once_or_verify
from .provider import (
    ALLOWED_REASONING_EFFORTS,
    CANONICAL_MODEL_ID,
    MODEL_ID,
    PROVIDER_SLUG,
)
from .tasking import sha256_json


F1_LAB_SCHEMA = "contextlab.f1-indexed-memory-lab.v3"
F1_CELL_COMPLETION_SCHEMA = "contextlab.f1-cell-completion.v2"
F1_GENERATOR_RECEIPT_SCHEMA = "contextlab.f1-generator-receipt.v2"
F1_PUBLIC_OUTPUT_SCHEMA = "contextlab.f1-public-generation-output.v1"
F1_RESULT_EVIDENCE_SCHEMA = "contextlab.f1-public-result-evidence.v1"
F1_STRATEGIES = ("full_history", "summary_only", "summary_pointer")
F1_REASONING_EFFORTS = tuple(ALLOWED_REASONING_EFFORTS)
F1_DEREFERENCE_BUDGET = 2
F1_FRONTIER_PROTOCOL_SCHEMA = "contextlab.frontier-protocol.v2"
F1_MINIMUM_COMPLETE_TRIALS = 5
F1_PROVIDER_REPEAT_SAMPLE_COUNT = 5
F1_TEMPERATURE = 0.0
F1_TRIAL_IDS = tuple(
    f"f1-trial-{index:02d}" for index in range(1, F1_MINIMUM_COMPLETE_TRIALS + 1)
)
F1_PROVIDER_REPEAT_SAMPLE_IDS = tuple(
    f"f1-provider-repeat-{index:02d}"
    for index in range(1, F1_PROVIDER_REPEAT_SAMPLE_COUNT + 1)
)
F1_PREPARED_LAB_PATH = Path("results/v2/frontier/f1/indexed_memory_lab.prepared.json")
F1_GENERATED_LAB_PATH = Path(
    "results/v2/frontier/f1/indexed_memory_lab.generated_pending_result_review.json"
)
# Compatibility name for callers that only prepare the experiment.
F1_LAB_PATH = F1_PREPARED_LAB_PATH
_F1_PUBLIC_OUTPUT_ROOT = Path("results/v2/frontier/f1/public_outputs")
_F1_PUBLIC_EVIDENCE_ROOT = Path("results/v2/frontier/f1/public_evidence")
_MAX_PUBLIC_ARTIFACT_BYTES = 2 * 1024 * 1024
_LEDGER_RESERVATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")

_FORBIDDEN_PUBLIC_PATH_TOKENS = {
    "sealed",
    "protected",
    "evaluation_only",
    "canonical_fact_ledger",
    "gold",
    "grade",
    "grading",
    "scoring",
}
_LAB_FIELDS = {
    "schema_version",
    "experiment_id",
    "lab_status",
    "frontier_entry_gate_sha256",
    "g3_freeze_sha256",
    "protocol",
    "episode_index",
    "queries",
    "cells",
    "completion_records",
    "prepared_lab_sha256",
    "answer_quality_status",
    "artifact_sha256",
}
_CELL_FIELDS = {
    "cell_id",
    "trial_id",
    "provider_repeat_sample_id",
    "query_id",
    "task_id",
    "reasoning_effort",
    "strategy",
    "selected_episode_ids",
    "dereferenced_episode_ids",
    "trace_pointers",
    "evidence_recovery",
    "dereference_count",
    "active_tokens",
    "latency",
    "information_loss_flags",
    "answer_quality",
    "evidence_outcome",
    "completion_record_sha256",
    "cell_sha256",
}


def _f1_repeat_controls() -> dict[str, Any]:
    pairings = [
        {
            "trial_id": trial_id,
            "provider_repeat_sample_id": sample_id,
        }
        for trial_id, sample_id in zip(
            F1_TRIAL_IDS, F1_PROVIDER_REPEAT_SAMPLE_IDS, strict=True
        )
    ]
    return {
        "frontier_protocol_schema": F1_FRONTIER_PROTOCOL_SCHEMA,
        "stochastic_trial_plan": {
            "stochastic": True,
            "minimum_complete_trials": F1_MINIMUM_COMPLETE_TRIALS,
            "trial_ids": list(F1_TRIAL_IDS),
        },
        "temperature_zero_provider_repeat_sample_plan": {
            "temperature": F1_TEMPERATURE,
            "minimum_provider_repeat_samples": F1_PROVIDER_REPEAT_SAMPLE_COUNT,
            "sample_ids": list(F1_PROVIDER_REPEAT_SAMPLE_IDS),
            "trial_sample_pairing": pairings,
        },
    }


class FrontierF1Error(ValueError):
    """An F1 input, measurement, completion, or artifact is invalid."""


def _sha(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise FrontierF1Error(f"{label} must be a lowercase SHA-256")
    return value


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FrontierF1Error(f"{label} must be a non-empty string")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise FrontierF1Error(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FrontierF1Error(f"{label} must be a non-negative integer")
    return value


def _finite_number(value: object, label: str, *, minimum: float = 0.0) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < minimum
    ):
        raise FrontierF1Error(f"{label} must be a finite number >= {minimum}")
    return float(value)


def _decimal_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise FrontierF1Error(f"{label} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise FrontierF1Error(f"{label} must be a decimal string") from exc
    if not parsed.is_finite() or parsed < 0:
        raise FrontierF1Error(f"{label} must be a non-negative finite decimal")
    canonical = format(parsed, "f")
    if canonical != value:
        raise FrontierF1Error(f"{label} must use canonical fixed-point notation")
    return value


def _ordered_texts(value: object, label: str) -> list[str]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or any(not isinstance(item, str) or not item for item in value)
    ):
        raise FrontierF1Error(f"{label} must be a string list")
    rows = list(value)
    if rows != sorted(set(rows)):
        raise FrontierF1Error(f"{label} must be sorted and unique")
    return rows


def _normalize_public_artifact_reference(
    value: object,
    label: str,
    *,
    allowed_root: Path,
    nullable: bool = False,
) -> dict[str, str] | None:
    if value is None and nullable:
        return None
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256"}:
        raise FrontierF1Error(f"{label} must be a public artifact path and SHA")
    path_value = _nonempty(value.get("path"), f"{label}.path")
    relative = Path(path_value)
    normalized = path_value.casefold().replace("-", "_")
    try:
        relative.relative_to(allowed_root)
    except ValueError as exc:
        raise FrontierF1Error(
            f"{label} must stay in {allowed_root.as_posix()}"
        ) from exc
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or "\\" in path_value
        or relative.as_posix() != path_value
        or relative == allowed_root
        or relative.suffix != ".json"
        or any(token in normalized for token in _FORBIDDEN_PUBLIC_PATH_TOKENS)
    ):
        raise FrontierF1Error(f"{label} is not a repository-safe public artifact")
    return {
        "path": path_value,
        "sha256": _sha(value.get("sha256"), f"{label}.sha256"),
    }


def _without_hash(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != field}


def _normalize_pointer(value: object, label: str, *, episode: bool) -> dict[str, str]:
    expected = {"path", "artifact_sha256", "trace_sha256", "json_pointer"}
    if episode:
        expected.add("trace_id")
    if not isinstance(value, Mapping) or set(value) != expected:
        raise FrontierF1Error(f"{label} fields changed")
    pointer = {key: _nonempty(value.get(key), f"{label}.{key}") for key in expected}
    path = Path(pointer["path"])
    normalized_path = pointer["path"].casefold().replace("-", "_")
    if (
        path.is_absolute()
        or ".." in path.parts
        or any(token in normalized_path for token in _FORBIDDEN_PUBLIC_PATH_TOKENS)
    ):
        raise FrontierF1Error(f"{label}.path must reference public G3 evidence")
    if episode:
        prefix = Path("results/v2/memory/prior_runs")
        canonical_location = path.parent == prefix
        expected_json_pointer = "/result_receipt/trace"
    else:
        prefix = Path("results/v2/memory/receipts/g3-public-v1/M4")
        canonical_location = path.parent.parent == prefix and path.parent.name in {
            "low",
            "high",
        }
        expected_json_pointer = "/trace"
    try:
        path.relative_to(prefix)
    except ValueError as exc:
        raise FrontierF1Error(f"{label}.path is not a canonical public trace") from exc
    if (
        not canonical_location
        or path.suffix != ".json"
        or pointer["json_pointer"] != expected_json_pointer
    ):
        raise FrontierF1Error(f"{label} does not identify a canonical public trace")
    _sha(pointer["artifact_sha256"], f"{label}.artifact_sha256")
    _sha(pointer["trace_sha256"], f"{label}.trace_sha256")
    if episode and pointer["trace_id"] != f"trace-{pointer['trace_sha256']}":
        raise FrontierF1Error(f"{label} trace binding changed")
    return {key: pointer[key] for key in sorted(pointer)}


def _normalize_episode(value: object) -> dict[str, Any]:
    expected = {
        "episode_id",
        "source_task_id",
        "task_signature",
        "task_family",
        "summary",
        "summary_token_count",
        "rank",
        "raw_evidence_ids",
        "raw_episode_token_count",
        "raw_episode_pointer",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise FrontierF1Error("episode fields changed")
    episode = {
        "episode_id": _nonempty(value.get("episode_id"), "episode_id"),
        "source_task_id": _nonempty(value.get("source_task_id"), "source_task_id"),
        "task_signature": _sha(value.get("task_signature"), "task_signature"),
        "task_family": _nonempty(value.get("task_family"), "task_family"),
        "summary": _nonempty(value.get("summary"), "summary"),
        "summary_token_count": _positive_int(
            value.get("summary_token_count"), "summary_token_count"
        ),
        "rank": _positive_int(value.get("rank"), "rank"),
        "raw_evidence_ids": _ordered_texts(
            value.get("raw_evidence_ids"), "raw_evidence_ids"
        ),
        "raw_episode_token_count": _positive_int(
            value.get("raw_episode_token_count"), "raw_episode_token_count"
        ),
        "raw_episode_pointer": _normalize_pointer(
            value.get("raw_episode_pointer"), "raw_episode_pointer", episode=True
        ),
    }
    episode["record_sha256"] = sha256_json(episode)
    return episode


def _normalize_query(value: object) -> dict[str, Any]:
    expected = {
        "query_id",
        "task_id",
        "task_signature",
        "task_family",
        "question_sha256",
        "reasoning_effort",
        "source_trace_pointer",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise FrontierF1Error("query fields changed")
    effort = value.get("reasoning_effort")
    if effort not in F1_REASONING_EFFORTS:
        raise FrontierF1Error("query reasoning effort must be exactly low or high")
    query = {
        "query_id": _nonempty(value.get("query_id"), "query_id"),
        "task_id": _nonempty(value.get("task_id"), "task_id"),
        "task_signature": _sha(value.get("task_signature"), "task_signature"),
        "task_family": _nonempty(value.get("task_family"), "task_family"),
        "question_sha256": _sha(value.get("question_sha256"), "question_sha256"),
        "reasoning_effort": effort,
        "source_trace_pointer": _normalize_pointer(
            value.get("source_trace_pointer"),
            "source_trace_pointer",
            episode=False,
        ),
    }
    query["record_sha256"] = sha256_json(query)
    return query


def _validate_query_factorial(queries: Sequence[Mapping[str, Any]]) -> None:
    by_task: dict[str, list[Mapping[str, Any]]] = {}
    for query in queries:
        by_task.setdefault(str(query["task_id"]), []).append(query)
    for task_id, rows in by_task.items():
        if len(rows) != len(F1_REASONING_EFFORTS) or {
            row["reasoning_effort"] for row in rows
        } != set(F1_REASONING_EFFORTS):
            raise FrontierF1Error(
                f"task {task_id} must contain exactly low and high reasoning effort"
            )
        identity = {
            (
                row["task_signature"],
                row["task_family"],
                row["question_sha256"],
            )
            for row in rows
        }
        if len(identity) != 1:
            raise FrontierF1Error(f"task {task_id} effort cells disagree on identity")


def _recovery(required: list[str], recovered: list[str]) -> dict[str, Any]:
    if not required:
        return {
            "required_evidence_ids": [],
            "recovered_evidence_ids": [],
            "recovered_count": 0,
            "required_count": 0,
            "recall": None,
            "status": "not_applicable",
        }
    recovered_set = set(recovered).intersection(required)
    return {
        "required_evidence_ids": required,
        "recovered_evidence_ids": sorted(recovered_set),
        "recovered_count": len(recovered_set),
        "required_count": len(required),
        "recall": len(recovered_set) / len(required),
        "status": "measured",
    }


def _build_cell_body(
    query: Mapping[str, Any],
    episodes: Sequence[Mapping[str, Any]],
    strategy: str,
    *,
    trial_id: str,
    provider_repeat_sample_id: str,
) -> dict[str, Any]:
    selected = [
        episode
        for episode in episodes
        if episode["task_family"] == query["task_family"]
        and episode["source_task_id"] != query["task_id"]
        and episode["task_signature"] != query["task_signature"]
    ]
    required = sorted(
        {
            evidence_id
            for episode in selected
            for evidence_id in episode["raw_evidence_ids"]
        }
    )
    dereferenced = (
        selected[:F1_DEREFERENCE_BUDGET] if strategy == "summary_pointer" else []
    )
    recovered = (
        required
        if strategy == "full_history"
        else sorted(
            {
                evidence_id
                for episode in dereferenced
                for evidence_id in episode["raw_evidence_ids"]
            }
        )
    )
    if strategy == "full_history":
        active_tokens = sum(int(row["raw_episode_token_count"]) for row in selected)
    else:
        active_tokens = sum(int(row["summary_token_count"]) for row in selected)
        if strategy == "summary_pointer":
            active_tokens += sum(
                int(row["raw_episode_token_count"]) for row in dereferenced
            )
    flags: list[str] = []
    if not selected:
        flags.append("no_eligible_experience")
    elif set(recovered) != set(required):
        flags.append("raw_evidence_unrecoverable")
    return {
        "cell_id": (
            f"{query['query_id']}:{strategy}:{trial_id}:{provider_repeat_sample_id}"
        ),
        "trial_id": trial_id,
        "provider_repeat_sample_id": provider_repeat_sample_id,
        "query_id": query["query_id"],
        "task_id": query["task_id"],
        "reasoning_effort": query["reasoning_effort"],
        "strategy": strategy,
        "selected_episode_ids": [row["episode_id"] for row in selected],
        "dereferenced_episode_ids": [row["episode_id"] for row in dereferenced],
        "trace_pointers": [
            row["raw_episode_pointer"]
            for row in (selected if strategy == "full_history" else dereferenced)
        ],
        "evidence_recovery": _recovery(required, recovered),
        "dereference_count": len(dereferenced),
        "active_tokens": active_tokens,
        "information_loss_flags": flags,
    }


def _clock_read(clock: Callable[[], int], label: str) -> int:
    value = clock()
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FrontierF1Error(f"{label} must return non-negative integer nanoseconds")
    return value


def _fixture_latency(
    values: Mapping[str, int | float], cell_id: str, strategy: str
) -> dict[str, Any]:
    raw = values.get(cell_id, values.get(strategy))
    milliseconds = _finite_number(raw, f"{cell_id} fixture preparation latency")
    return {
        "kind": "provider_free_preparation",
        "milliseconds": milliseconds,
        "measurement_status": "fixture",
        "measurement_source": "fixture_supplied",
        "started_monotonic_ns": None,
        "finished_monotonic_ns": None,
    }


def _measured_cell(
    query: Mapping[str, Any],
    episodes: Sequence[Mapping[str, Any]],
    strategy: str,
    trial_id: str,
    provider_repeat_sample_id: str,
    clock: Callable[[], int],
) -> dict[str, Any]:
    started = _clock_read(clock, "monotonic clock")
    body = _build_cell_body(
        query,
        episodes,
        strategy,
        trial_id=trial_id,
        provider_repeat_sample_id=provider_repeat_sample_id,
    )
    finished = _clock_read(clock, "monotonic clock")
    if finished < started:
        raise FrontierF1Error("monotonic clock moved backwards")
    return body | {
        "latency": {
            "kind": "provider_free_preparation",
            "milliseconds": (finished - started) / 1_000_000,
            "measurement_status": "measured",
            "measurement_source": "monotonic_ns",
            "started_monotonic_ns": started,
            "finished_monotonic_ns": finished,
        }
    }


def _assemble_lab(
    *,
    episodes: Sequence[Mapping[str, Any]],
    queries: Sequence[Mapping[str, Any]],
    frontier_entry_gate_sha256: str,
    g3_freeze_sha256: str,
    fixture_preparation_time_ms: Mapping[str, int | float] | None,
    monotonic_ns: Callable[[], int] | None,
) -> dict[str, Any]:
    if (fixture_preparation_time_ms is None) == (monotonic_ns is None):
        raise FrontierF1Error(
            "choose exactly one preparation latency source: fixture or monotonic clock"
        )
    normalized_episodes = sorted(
        (_normalize_episode(row) for row in episodes),
        key=lambda row: (row["rank"], row["episode_id"]),
    )
    effort_order = {effort: index for index, effort in enumerate(F1_REASONING_EFFORTS)}
    normalized_queries = sorted(
        (_normalize_query(row) for row in queries),
        key=lambda row: (row["task_id"], effort_order[row["reasoning_effort"]]),
    )
    if not normalized_episodes or not normalized_queries:
        raise FrontierF1Error("F1 requires public episodes and paired queries")
    if len({row["episode_id"] for row in normalized_episodes}) != len(
        normalized_episodes
    ):
        raise FrontierF1Error("episode IDs must be unique")
    if len({row["query_id"] for row in normalized_queries}) != len(normalized_queries):
        raise FrontierF1Error("query IDs must be unique")
    eligible_query_task_families = sorted(
        {episode["task_family"] for episode in normalized_episodes}
    )
    if any(
        query["task_family"] not in eligible_query_task_families
        for query in normalized_queries
    ):
        raise FrontierF1Error(
            "F1 queries must use task families represented by episode cards"
        )
    _validate_query_factorial(normalized_queries)
    if fixture_preparation_time_ms is not None and not isinstance(
        fixture_preparation_time_ms, Mapping
    ):
        raise FrontierF1Error("fixture preparation timings must be a mapping")

    cells: list[dict[str, Any]] = []
    for trial_id, sample_id in zip(
        F1_TRIAL_IDS, F1_PROVIDER_REPEAT_SAMPLE_IDS, strict=True
    ):
        for query in normalized_queries:
            for strategy in F1_STRATEGIES:
                if monotonic_ns is not None:
                    cell = _measured_cell(
                        query,
                        normalized_episodes,
                        strategy,
                        trial_id,
                        sample_id,
                        monotonic_ns,
                    )
                else:
                    body = _build_cell_body(
                        query,
                        normalized_episodes,
                        strategy,
                        trial_id=trial_id,
                        provider_repeat_sample_id=sample_id,
                    )
                    assert fixture_preparation_time_ms is not None
                    cell = body | {
                        "latency": _fixture_latency(
                            fixture_preparation_time_ms, body["cell_id"], strategy
                        )
                    }
                cell |= {
                    "answer_quality": {
                        "status": "pending_generation",
                        "score": None,
                    },
                    "evidence_outcome": None,
                    "completion_record_sha256": None,
                }
                cell["cell_sha256"] = sha256_json(cell)
                cells.append(cell)
    latency_source = "monotonic_ns" if monotonic_ns is not None else "fixture_supplied"
    payload: dict[str, Any] = {
        "schema_version": F1_LAB_SCHEMA,
        "experiment_id": "F1",
        "lab_status": "prepared_pending_generation",
        "frontier_entry_gate_sha256": _sha(
            frontier_entry_gate_sha256, "frontier entry gate hash"
        ),
        "g3_freeze_sha256": _sha(g3_freeze_sha256, "G3 freeze hash"),
        "protocol": {
            "strategies": list(F1_STRATEGIES),
            "reasoning_efforts": list(F1_REASONING_EFFORTS),
            "eligible_query_task_families": eligible_query_task_families,
            "dereference_budget": F1_DEREFERENCE_BUDGET,
            "leave_one_task_out": True,
            "preparation_provider_calls": 0,
            "answer_generation": "external_receipt_import_only",
            "preparation_latency_source": latency_source,
            "repeat_controls": _f1_repeat_controls(),
        },
        "episode_index": normalized_episodes,
        "queries": normalized_queries,
        "cells": cells,
        "completion_records": [],
        "prepared_lab_sha256": None,
        "answer_quality_status": "pending_generation",
    }
    payload["artifact_sha256"] = sha256_json(payload)
    return payload


def build_f1_indexed_memory_lab(
    *,
    episodes: Sequence[Mapping[str, Any]],
    queries: Sequence[Mapping[str, Any]],
    frontier_entry_gate_sha256: str,
    g3_freeze_sha256: str,
    fixture_preparation_time_ms: Mapping[str, int | float],
) -> dict[str, Any]:
    """Build a deterministic fixture lab; fixture latency is never called measured."""

    lab = _assemble_lab(
        episodes=episodes,
        queries=queries,
        frontier_entry_gate_sha256=frontier_entry_gate_sha256,
        g3_freeze_sha256=g3_freeze_sha256,
        fixture_preparation_time_ms=fixture_preparation_time_ms,
        monotonic_ns=None,
    )
    validate_f1_indexed_memory_lab(lab, require_generated=False)
    return lab


def _validate_latency(value: object, expected_source: str, label: str) -> None:
    fields = {
        "kind",
        "milliseconds",
        "measurement_status",
        "measurement_source",
        "started_monotonic_ns",
        "finished_monotonic_ns",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise FrontierF1Error(f"{label} fields changed")
    milliseconds = _finite_number(value.get("milliseconds"), f"{label}.milliseconds")
    if value.get("kind") != "provider_free_preparation":
        raise FrontierF1Error(f"{label} kind changed")
    if expected_source == "fixture_supplied":
        if (
            value.get("measurement_status") != "fixture"
            or value.get("measurement_source") != "fixture_supplied"
            or value.get("started_monotonic_ns") is not None
            or value.get("finished_monotonic_ns") is not None
        ):
            raise FrontierF1Error(f"{label} mislabels fixture timing as measured")
        return
    started = _nonnegative_int(value.get("started_monotonic_ns"), f"{label}.start")
    finished = _nonnegative_int(value.get("finished_monotonic_ns"), f"{label}.finish")
    if (
        value.get("measurement_status") != "measured"
        or value.get("measurement_source") != "monotonic_ns"
        or finished < started
        or milliseconds != (finished - started) / 1_000_000
    ):
        raise FrontierF1Error(f"{label} monotonic measurement is invalid")


def _pending_cell_from_generated(cell: Mapping[str, Any]) -> dict[str, Any]:
    pending = dict(cell)
    pending["answer_quality"] = {"status": "pending_generation", "score": None}
    pending["evidence_outcome"] = None
    pending["completion_record_sha256"] = None
    pending.pop("cell_sha256", None)
    pending["cell_sha256"] = sha256_json(pending)
    return pending


def _normalize_completion(
    raw: object, pending_cell: Mapping[str, Any]
) -> dict[str, Any]:
    fields = {
        "schema_version",
        "cell_id",
        "trial_id",
        "provider_repeat_sample_id",
        "prepared_cell_sha256",
        "generator_receipt",
        "answer_quality_outcome",
        "evidence_outcome",
        "artifact_sha256",
    }
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise FrontierF1Error("F1 cell completion fields changed")
    if (
        raw.get("schema_version") != F1_CELL_COMPLETION_SCHEMA
        or raw.get("cell_id") != pending_cell.get("cell_id")
        or raw.get("trial_id") != pending_cell.get("trial_id")
        or raw.get("provider_repeat_sample_id")
        != pending_cell.get("provider_repeat_sample_id")
        or raw.get("prepared_cell_sha256") != pending_cell.get("cell_sha256")
        or raw.get("artifact_sha256")
        != sha256_json(_without_hash(raw, "artifact_sha256"))
    ):
        raise FrontierF1Error("F1 cell completion binding or hash is invalid")

    receipt = raw.get("generator_receipt")
    receipt_fields = {
        "schema_version",
        "trial_id",
        "provider_repeat_sample_id",
        "temperature",
        "requested_model",
        "resolved_model",
        "reasoning_effort",
        "provider",
        "request_id",
        "ledger_reservation_id",
        "native_usage",
        "billed_cost_usd",
        "billed_price",
        "latency_ms",
        "retry_count",
        "status",
        "error",
        "output_sha256",
        "output_artifact",
    }
    if not isinstance(receipt, Mapping) or set(receipt) != receipt_fields:
        raise FrontierF1Error("F1 generator receipt fields changed")
    if (
        receipt.get("schema_version") != F1_GENERATOR_RECEIPT_SCHEMA
        or receipt.get("trial_id") != pending_cell.get("trial_id")
        or receipt.get("provider_repeat_sample_id")
        != pending_cell.get("provider_repeat_sample_id")
        or receipt.get("temperature") != F1_TEMPERATURE
        or receipt.get("requested_model") != MODEL_ID
        or receipt.get("resolved_model") not in {MODEL_ID, CANONICAL_MODEL_ID}
        or str(receipt.get("provider", "")).casefold() != PROVIDER_SLUG
        or receipt.get("reasoning_effort") != pending_cell.get("reasoning_effort")
        or receipt.get("reasoning_effort") not in F1_REASONING_EFFORTS
        or not isinstance(receipt.get("request_id"), str)
        or not receipt["request_id"]
        or not isinstance(receipt.get("ledger_reservation_id"), str)
        or _LEDGER_RESERVATION_ID.fullmatch(receipt["ledger_reservation_id"]) is None
        or receipt.get("retry_count") != 0
    ):
        raise FrontierF1Error("F1 generator identity or retry contract changed")
    usage = receipt.get("native_usage")
    if not isinstance(usage, Mapping) or set(usage) != {
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "total_tokens",
    }:
        raise FrontierF1Error("F1 native usage fields changed")
    prompt_tokens = _nonnegative_int(usage.get("prompt_tokens"), "prompt tokens")
    completion_tokens = _nonnegative_int(
        usage.get("completion_tokens"), "completion tokens"
    )
    _nonnegative_int(usage.get("reasoning_tokens"), "reasoning tokens")
    if usage.get("total_tokens") != prompt_tokens + completion_tokens:
        raise FrontierF1Error("F1 native total token count is invalid")
    _decimal_text(receipt.get("billed_cost_usd"), "billed cost")
    price = receipt.get("billed_price")
    if not isinstance(price, Mapping) or set(price) != {
        "input_usd_per_million",
        "output_usd_per_million",
    }:
        raise FrontierF1Error("F1 billed price fields changed")
    _decimal_text(price.get("input_usd_per_million"), "input billed price")
    _decimal_text(price.get("output_usd_per_million"), "output billed price")
    _finite_number(receipt.get("latency_ms"), "generation latency")
    output_sha = _sha(receipt.get("output_sha256"), "generator output commitment")
    status = receipt.get("status")
    if status == "completed":
        if receipt.get("error") is not None:
            raise FrontierF1Error("completed F1 generation cannot contain an error")
        output_artifact = _normalize_public_artifact_reference(
            receipt.get("output_artifact"),
            "F1 public output artifact",
            allowed_root=_F1_PUBLIC_OUTPUT_ROOT,
        )
        assert output_artifact is not None
        if output_artifact["sha256"] != output_sha:
            raise FrontierF1Error(
                "F1 public output artifact differs from output_sha256"
            )
    elif status == "failed":
        error = _nonempty(receipt.get("error"), "failed F1 generation error")
        if redact(error) != error:
            raise FrontierF1Error(
                "failed F1 generation error must already be redacted and safe"
            )
        if (
            _normalize_public_artifact_reference(
                receipt.get("output_artifact"),
                "failed F1 output artifact",
                allowed_root=_F1_PUBLIC_OUTPUT_ROOT,
                nullable=True,
            )
            is not None
        ):
            raise FrontierF1Error("failed F1 output artifact must be null")
    else:
        raise FrontierF1Error("F1 generator status must be completed or failed")

    answer = raw.get("answer_quality_outcome")
    outcome_fields = {
        "status",
        "score",
        "evaluator_id",
        "prepared_cell_sha256",
        "output_sha256",
        "public_evidence_artifact",
    }
    if not isinstance(answer, Mapping) or set(answer) != outcome_fields:
        raise FrontierF1Error("F1 answer-quality outcome fields changed")
    score = _finite_number(answer.get("score"), "answer-quality score")
    if score > 1:
        raise FrontierF1Error("answer-quality score must be within 0..1")
    expected_answer_status = (
        "measured" if status == "completed" else "generation_failed"
    )
    answer_artifact = _normalize_public_artifact_reference(
        answer.get("public_evidence_artifact"),
        "F1 answer-quality public evidence artifact",
        allowed_root=_F1_PUBLIC_EVIDENCE_ROOT,
        nullable=status == "failed",
    )
    if (
        answer.get("status") != expected_answer_status
        or answer.get("prepared_cell_sha256") != pending_cell.get("cell_sha256")
        or answer.get("output_sha256") != output_sha
        or not isinstance(answer.get("evaluator_id"), str)
        or not answer["evaluator_id"]
        or (status == "failed" and answer_artifact is not None)
        or (status == "failed" and score != 0.0)
    ):
        raise FrontierF1Error("F1 answer-quality outcome is not bound or terminal")

    evidence = raw.get("evidence_outcome")
    evidence_fields = {
        "status",
        "required_evidence_ids",
        "recovered_evidence_ids",
        "recall",
        "provenance_complete",
        "prepared_cell_sha256",
        "output_sha256",
        "public_evidence_artifact",
    }
    if not isinstance(evidence, Mapping) or set(evidence) != evidence_fields:
        raise FrontierF1Error("F1 evidence outcome fields changed")
    required = _ordered_texts(
        evidence.get("required_evidence_ids"), "completion required evidence"
    )
    recovered = _ordered_texts(
        evidence.get("recovered_evidence_ids"), "completion recovered evidence"
    )
    prepared_required = pending_cell["evidence_recovery"]["required_evidence_ids"]
    if required != prepared_required or not set(recovered).issubset(required):
        raise FrontierF1Error("F1 completion evidence differs from the prepared cell")
    expected_recall = None if not required else len(recovered) / len(required)
    evidence_artifact = _normalize_public_artifact_reference(
        evidence.get("public_evidence_artifact"),
        "F1 metric public evidence artifact",
        allowed_root=_F1_PUBLIC_EVIDENCE_ROOT,
        nullable=status == "failed",
    )
    if (
        evidence.get("status")
        != ("measured" if status == "completed" else "generation_failed")
        or evidence.get("recall") != expected_recall
        or not isinstance(evidence.get("provenance_complete"), bool)
        or evidence.get("prepared_cell_sha256") != pending_cell.get("cell_sha256")
        or evidence.get("output_sha256") != output_sha
        or (status == "completed" and evidence_artifact != answer_artifact)
        or (status == "failed" and evidence_artifact is not None)
        or (
            status == "failed"
            and (recovered or evidence.get("provenance_complete") is not False)
        )
    ):
        raise FrontierF1Error("F1 evidence outcome is not bound or terminal")
    return dict(raw)


def _validate_lab_envelope(value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping) or set(value) != _LAB_FIELDS:
        raise FrontierF1Error("F1 lab fields changed")
    if (
        value.get("schema_version") != F1_LAB_SCHEMA
        or value.get("experiment_id") != "F1"
        or value.get("artifact_sha256")
        != sha256_json(_without_hash(value, "artifact_sha256"))
    ):
        raise FrontierF1Error("F1 lab envelope or hash is invalid")
    _sha(value.get("frontier_entry_gate_sha256"), "frontier entry gate hash")
    _sha(value.get("g3_freeze_sha256"), "G3 freeze hash")
    protocol = value.get("protocol")
    if not isinstance(protocol, Mapping) or set(protocol) != {
        "strategies",
        "reasoning_efforts",
        "eligible_query_task_families",
        "dereference_budget",
        "leave_one_task_out",
        "preparation_provider_calls",
        "answer_generation",
        "preparation_latency_source",
        "repeat_controls",
    }:
        raise FrontierF1Error("F1 protocol fields changed")
    if (
        protocol.get("strategies") != list(F1_STRATEGIES)
        or protocol.get("reasoning_efforts") != list(F1_REASONING_EFFORTS)
        or not _ordered_texts(
            protocol.get("eligible_query_task_families"),
            "F1 eligible query task families",
        )
        or protocol.get("dereference_budget") != F1_DEREFERENCE_BUDGET
        or protocol.get("leave_one_task_out") is not True
        or protocol.get("preparation_provider_calls") != 0
        or protocol.get("answer_generation") != "external_receipt_import_only"
        or protocol.get("preparation_latency_source")
        not in {"fixture_supplied", "monotonic_ns"}
        or protocol.get("repeat_controls") != _f1_repeat_controls()
    ):
        raise FrontierF1Error("F1 frozen protocol changed")


def validate_f1_indexed_memory_lab(
    value: Mapping[str, Any], *, require_generated: bool | None = None
) -> None:
    """Validate a prepared or generated-pending-review F1 lab."""

    _validate_lab_envelope(value)
    generated = value.get("lab_status") == "generated_pending_result_review"
    if value.get("lab_status") not in {
        "prepared_pending_generation",
        "generated_pending_result_review",
    }:
        raise FrontierF1Error("F1 lab status is invalid")
    if require_generated is not None and generated is not require_generated:
        raise FrontierF1Error(
            "F1 generated-pending-review lab required"
            if require_generated
            else "F1 prepared lab required"
        )
    raw_episodes = value.get("episode_index")
    raw_queries = value.get("queries")
    raw_cells = value.get("cells")
    if not all(
        isinstance(rows, list) and rows
        for rows in (raw_episodes, raw_queries, raw_cells)
    ):
        raise FrontierF1Error("F1 episodes, queries, and cells must be non-empty lists")

    episodes: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_episodes):
        if not isinstance(raw, Mapping) or "record_sha256" not in raw:
            raise FrontierF1Error(f"episode {index} is invalid")
        normalized = _normalize_episode(_without_hash(raw, "record_sha256"))
        if dict(raw) != normalized:
            raise FrontierF1Error(f"episode {index} hash or fields changed")
        episodes.append(normalized)
    queries: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_queries):
        if not isinstance(raw, Mapping) or "record_sha256" not in raw:
            raise FrontierF1Error(f"query {index} is invalid")
        normalized = _normalize_query(_without_hash(raw, "record_sha256"))
        if dict(raw) != normalized:
            raise FrontierF1Error(f"query {index} hash or fields changed")
        queries.append(normalized)
    _validate_query_factorial(queries)
    if len({row["episode_id"] for row in episodes}) != len(episodes):
        raise FrontierF1Error("episode IDs must be unique")
    if len({row["query_id"] for row in queries}) != len(queries):
        raise FrontierF1Error("query IDs must be unique")
    eligible_families = sorted({episode["task_family"] for episode in episodes})
    if value["protocol"].get(
        "eligible_query_task_families"
    ) != eligible_families or any(
        query["task_family"] not in eligible_families for query in queries
    ):
        raise FrontierF1Error(
            "F1 query selection is not bound to episode-card task families"
        )

    expected_pairs = [
        (query, strategy, trial_id, sample_id)
        for trial_id, sample_id in zip(
            F1_TRIAL_IDS, F1_PROVIDER_REPEAT_SAMPLE_IDS, strict=True
        )
        for query in queries
        for strategy in F1_STRATEGIES
    ]
    if len(raw_cells) != len(expected_pairs):
        raise FrontierF1Error("F1 cell factorial is incomplete")
    pending_cells: list[dict[str, Any]] = []
    for index, (raw, (query, strategy, trial_id, sample_id)) in enumerate(
        zip(raw_cells, expected_pairs, strict=True)
    ):
        if not isinstance(raw, Mapping) or set(raw) != _CELL_FIELDS:
            raise FrontierF1Error(f"F1 cell {index} fields changed")
        if raw.get("cell_sha256") != sha256_json(_without_hash(raw, "cell_sha256")):
            raise FrontierF1Error(f"F1 cell {index} hash changed")
        expected_body = _build_cell_body(
            query,
            episodes,
            strategy,
            trial_id=trial_id,
            provider_repeat_sample_id=sample_id,
        )
        for key, expected in expected_body.items():
            if raw.get(key) != expected:
                raise FrontierF1Error(f"F1 cell {index} derived field {key} changed")
        _validate_latency(
            raw.get("latency"),
            str(value["protocol"]["preparation_latency_source"]),
            f"F1 cell {index} latency",
        )
        pending = _pending_cell_from_generated(raw) if generated else dict(raw)
        if not generated and (
            raw.get("answer_quality") != {"status": "pending_generation", "score": None}
            or raw.get("evidence_outcome") is not None
            or raw.get("completion_record_sha256") is not None
        ):
            raise FrontierF1Error("prepared F1 cells must remain pending generation")
        pending_cells.append(pending)

    if not generated:
        if (
            value.get("answer_quality_status") != "pending_generation"
            or value.get("completion_records") != []
            or value.get("prepared_lab_sha256") is not None
        ):
            raise FrontierF1Error("prepared F1 lab completion fields changed")
        return

    if value.get("answer_quality_status") != "generated_pending_result_review":
        raise FrontierF1Error("generated F1 lab is not pending result review")
    _sha(value.get("prepared_lab_sha256"), "prepared F1 lab hash")
    completions = value.get("completion_records")
    if not isinstance(completions, list) or len(completions) != len(pending_cells):
        raise FrontierF1Error("generated F1 lab requires one completion per cell")
    normalized_completions = [
        _normalize_completion(raw, pending)
        for raw, pending in zip(completions, pending_cells, strict=True)
    ]
    request_ids = [
        str(row["generator_receipt"]["request_id"]) for row in normalized_completions
    ]
    reservation_ids = [
        str(row["generator_receipt"]["ledger_reservation_id"])
        for row in normalized_completions
    ]
    if len(set(request_ids)) != len(request_ids):
        raise FrontierF1Error("F1 generator request IDs must be unique across cells")
    if len(set(reservation_ids)) != len(reservation_ids):
        raise FrontierF1Error("F1 ledger reservation IDs must be unique across cells")
    for index, (cell, completion) in enumerate(
        zip(raw_cells, normalized_completions, strict=True)
    ):
        if (
            cell.get("answer_quality") != completion["answer_quality_outcome"]
            or cell.get("evidence_outcome") != completion["evidence_outcome"]
            or cell.get("completion_record_sha256") != completion["artifact_sha256"]
        ):
            raise FrontierF1Error(f"generated F1 cell {index} outcome binding changed")
    prepared: dict[str, Any] = {
        key: item for key, item in value.items() if key != "artifact_sha256"
    }
    prepared["lab_status"] = "prepared_pending_generation"
    prepared["cells"] = pending_cells
    prepared["completion_records"] = []
    prepared["prepared_lab_sha256"] = None
    prepared["answer_quality_status"] = "pending_generation"
    prepared["artifact_sha256"] = sha256_json(prepared)
    if prepared["artifact_sha256"] != value.get("prepared_lab_sha256"):
        raise FrontierF1Error("generated F1 lab is not bound to its prepared lab")


def import_f1_answer_quality_completions(
    prepared_lab: Mapping[str, Any], completion_records: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Import terminal generator receipts and outcomes without making provider calls."""

    validate_f1_indexed_memory_lab(prepared_lab, require_generated=False)
    records = list(completion_records)
    cells = prepared_lab["cells"]
    if len(records) != len(cells):
        raise FrontierF1Error("F1 completion import must cover every prepared cell")
    normalized = [
        _normalize_completion(record, cell)
        for record, cell in zip(records, cells, strict=True)
    ]
    generated_cells: list[dict[str, Any]] = []
    for cell, record in zip(cells, normalized, strict=True):
        generated = dict(cell)
        generated["answer_quality"] = record["answer_quality_outcome"]
        generated["evidence_outcome"] = record["evidence_outcome"]
        generated["completion_record_sha256"] = record["artifact_sha256"]
        generated.pop("cell_sha256")
        generated["cell_sha256"] = sha256_json(generated)
        generated_cells.append(generated)
    result = {
        key: item for key, item in prepared_lab.items() if key != "artifact_sha256"
    }
    result["lab_status"] = "generated_pending_result_review"
    result["cells"] = generated_cells
    result["completion_records"] = normalized
    result["prepared_lab_sha256"] = prepared_lab["artifact_sha256"]
    result["answer_quality_status"] = "generated_pending_result_review"
    result["artifact_sha256"] = sha256_json(result)
    validate_f1_indexed_memory_lab(result, require_generated=True)
    return result


def _require_approved_f1_gate(root: Path) -> dict[str, Any]:
    from .frontier import (
        load_approved_frontier_entry_gate,
        require_frontier_experiment_approved,
    )

    try:
        gate = load_approved_frontier_entry_gate(root)
        require_frontier_experiment_approved(gate, "F1")
    except (OSError, ValueError) as exc:
        raise FrontierF1Error(
            "repository F1 work requires an approved F1 entry gate"
        ) from exc
    return gate


def _read_public_json(root: Path, relative: Path, label: str) -> dict[str, Any]:
    normalized = relative.as_posix().casefold().replace("-", "_")
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or any(token in normalized for token in _FORBIDDEN_PUBLIC_PATH_TOKENS)
    ):
        raise FrontierF1Error(f"{label} must reference public evidence")
    path = root / relative
    try:
        path.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise FrontierF1Error(f"{label} escapes the repository") from exc
    if path.is_symlink():
        raise FrontierF1Error(f"{label} cannot be a symlink")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrontierF1Error(f"cannot read {label}") from exc
    if not isinstance(value, dict):
        raise FrontierF1Error(f"{label} must be an object")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_referenced_public_json(
    root: Path,
    reference: object,
    *,
    allowed_root: Path,
    label: str,
) -> tuple[dict[str, Any], str]:
    normalized_reference = _normalize_public_artifact_reference(
        reference,
        label,
        allowed_root=allowed_root,
    )
    assert normalized_reference is not None
    path = root / normalized_reference["path"]
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise FrontierF1Error(f"{label} is missing or escapes the repository") from exc
    if not path.is_file() or path.is_symlink():
        raise FrontierF1Error(f"{label} is missing or unsafe")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise FrontierF1Error(f"cannot read {label}") from exc
    if len(raw) > _MAX_PUBLIC_ARTIFACT_BYTES:
        raise FrontierF1Error(f"{label} exceeds the public artifact size bound")
    observed_sha = hashlib.sha256(raw).hexdigest()
    if observed_sha != normalized_reference["sha256"]:
        raise FrontierF1Error(f"{label} file hash mismatch")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrontierF1Error(f"{label} must contain valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise FrontierF1Error(f"{label} JSON must be an object")
    if redact(value) != value:
        raise FrontierF1Error(f"{label} contains credential-shaped content")
    return value, observed_sha


def _validate_generated_public_artifacts(root: Path, value: Mapping[str, Any]) -> None:
    for index, completion in enumerate(value["completion_records"]):
        receipt = completion["generator_receipt"]
        if receipt["status"] == "failed":
            continue

        output, observed_output_sha = _read_referenced_public_json(
            root,
            receipt["output_artifact"],
            allowed_root=_F1_PUBLIC_OUTPUT_ROOT,
            label=f"F1 public output artifact {index}",
        )
        if set(output) != {
            "schema_version",
            "cell_id",
            "prepared_cell_sha256",
            "reasoning_effort",
            "answer",
            "record_sha256",
        }:
            raise FrontierF1Error("F1 public output artifact fields changed")
        if (
            output.get("schema_version") != F1_PUBLIC_OUTPUT_SCHEMA
            or output.get("cell_id") != completion.get("cell_id")
            or output.get("prepared_cell_sha256")
            != completion.get("prepared_cell_sha256")
            or output.get("reasoning_effort") != receipt.get("reasoning_effort")
            or not isinstance(output.get("answer"), str)
            or not output["answer"].strip()
            or output.get("record_sha256")
            != sha256_json(_without_hash(output, "record_sha256"))
            or observed_output_sha != receipt.get("output_sha256")
        ):
            raise FrontierF1Error("F1 public output artifact binding changed")

        answer = completion["answer_quality_outcome"]
        evidence = completion["evidence_outcome"]
        if answer["public_evidence_artifact"] != evidence["public_evidence_artifact"]:
            raise FrontierF1Error("F1 public evidence references differ")
        metric, _ = _read_referenced_public_json(
            root,
            answer["public_evidence_artifact"],
            allowed_root=_F1_PUBLIC_EVIDENCE_ROOT,
            label=f"F1 public result evidence artifact {index}",
        )
        if set(metric) != {
            "schema_version",
            "cell_id",
            "prepared_cell_sha256",
            "output_sha256",
            "answer_quality",
            "evidence",
            "record_sha256",
        }:
            raise FrontierF1Error("F1 public result evidence fields changed")
        expected_answer = {
            key: item
            for key, item in answer.items()
            if key != "public_evidence_artifact"
        }
        expected_evidence = {
            key: item
            for key, item in evidence.items()
            if key != "public_evidence_artifact"
        }
        if (
            metric.get("schema_version") != F1_RESULT_EVIDENCE_SCHEMA
            or metric.get("cell_id") != completion.get("cell_id")
            or metric.get("prepared_cell_sha256")
            != completion.get("prepared_cell_sha256")
            or metric.get("output_sha256") != receipt.get("output_sha256")
            or metric.get("answer_quality") != expected_answer
            or metric.get("evidence") != expected_evidence
            or metric.get("record_sha256")
            != sha256_json(_without_hash(metric, "record_sha256"))
        ):
            raise FrontierF1Error("F1 public result evidence binding changed")


def _approved_public_source_evidence(
    root: Path, gate: Mapping[str, Any]
) -> dict[str, Any]:
    from .frontier import (
        FRONTIER_ENTRY_EVIDENCE_PATH,
        validate_frontier_entry_evidence,
    )

    evidence = _read_public_json(
        root, FRONTIER_ENTRY_EVIDENCE_PATH, "frontier entry evidence"
    )
    try:
        validate_frontier_entry_evidence(evidence)
    except ValueError as exc:
        raise FrontierF1Error("frontier entry evidence is invalid") from exc
    f1 = next(
        (row for row in gate["experiments"] if row["experiment_id"] == "F1"),
        None,
    )
    expected_reference = [
        {
            "path": FRONTIER_ENTRY_EVIDENCE_PATH.as_posix(),
            "sha256": evidence["artifact_sha256"],
        }
    ]
    if not isinstance(f1, Mapping) or f1["checks"][0]["evidence"] != expected_reference:
        raise FrontierF1Error("approved F1 gate does not bind its entry evidence")
    source_hashes = {row["path"]: row["sha256"] for row in evidence["source_artifacts"]}
    for relative in (
        Path("results/v2/memory/g3_public_freeze.json"),
        Path("results/v2/memory/g3_public_generation_run.json"),
    ):
        try:
            current = (
                None
                if (root / relative).is_symlink()
                else _sha256_file(root / relative)
            )
        except OSError:
            current = None
        if current != source_hashes.get(relative.as_posix()):
            raise FrontierF1Error(
                f"approved F1 evidence does not match {relative.as_posix()}"
            )
    return evidence


def _canonical_token_count(value: object) -> int:
    from .retrieval import estimate_tokens

    return max(
        1,
        estimate_tokens(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        ),
    )


def _public_g3_episodes(root: Path, freeze: Mapping[str, Any]) -> list[dict[str, Any]]:
    from .g3_prior_runs import (
        canonical_prior_run_path,
        resolve_canonical_prior_sources,
    )
    from .retrieval import estimate_tokens

    manifest = freeze["manifest"]
    seeds = manifest["m4_episode_seed"]
    grades = manifest["trusted_grade_artifacts"]
    for seed in seeds:
        path = canonical_prior_run_path(root, str(seed.get("source_run_id")))
        _read_public_json(
            root,
            path.relative_to(root),
            f"F1 source episode {seed.get('episode_id')}",
        )
    try:
        sources = resolve_canonical_prior_sources(root, grades, seeds)
    except ValueError as exc:
        raise FrontierF1Error("F1 prior episode sources are invalid") from exc
    source_by_id = {str(row["source_run_id"]): row for row in sources}
    episodes: list[dict[str, Any]] = []
    for seed in seeds:
        source_run_id = str(seed["source_run_id"])
        source = source_by_id[source_run_id]
        summary = " | ".join(
            (
                f"family={seed['task_family']}",
                f"task={seed['task_feature']}",
                f"strategy={seed['selected_strategy']}",
                f"outcome={seed['grade_outcome']}",
            )
        )
        episodes.append(
            {
                "episode_id": seed["episode_id"],
                "source_task_id": source["task_id"],
                "task_signature": seed["task_signature"],
                "task_family": seed["task_family"],
                "summary": summary,
                "summary_token_count": max(1, estimate_tokens(summary)),
                "rank": seed["rank"],
                "raw_evidence_ids": sorted(seed["raw_evidence_ids"]),
                "raw_episode_token_count": _canonical_token_count(
                    source["result_receipt"]["trace"]
                ),
                "raw_episode_pointer": {
                    "path": (
                        Path("results/v2/memory/prior_runs") / f"{source_run_id}.json"
                    ).as_posix(),
                    "artifact_sha256": source["artifact_sha256"],
                    "trace_id": source["trace_id"],
                    "trace_sha256": source["trace_sha256"],
                    "json_pointer": "/result_receipt/trace",
                },
            }
        )
    return episodes


def _public_g3_queries(
    root: Path,
    freeze: Mapping[str, Any],
    public_run: Mapping[str, Any],
    *,
    eligible_task_families: Sequence[str],
) -> list[dict[str, Any]]:
    from .memory_experiments import validate_memory_result_receipt

    manifest = freeze["manifest"]
    trusted = str(manifest["frozen_manifest_sha256"])
    eligible = set(eligible_task_families)
    if not eligible or sorted(eligible) != list(eligible_task_families):
        raise FrontierF1Error("F1 eligible task families must be sorted and unique")
    queries: list[dict[str, Any]] = []
    for index, cell in enumerate(public_run["cells"]):
        if not isinstance(cell, Mapping) or cell.get("policy") != "M4":
            continue
        receipt_value = cell.get("receipt_path")
        if not isinstance(receipt_value, str):
            raise FrontierF1Error(f"M4 public cell {index} has no receipt")
        relative = Path(receipt_value)
        receipt = _read_public_json(root, relative, f"M4 public receipt {index}")
        spec = receipt.get("run_spec")
        try:
            validate_memory_result_receipt(receipt, spec, manifest, trusted)
        except (TypeError, ValueError) as exc:
            raise FrontierF1Error(f"M4 public receipt {index} is invalid") from exc
        if (
            receipt.get("result_sha256") != cell.get("receipt_sha256")
            or receipt.get("run_id") != cell.get("run_id")
            or receipt.get("reasoning_effort") not in F1_REASONING_EFFORTS
        ):
            raise FrontierF1Error(f"M4 public receipt {index} commitment changed")
        task = spec["task"]
        if task["task_family"] not in eligible:
            continue
        trace = receipt["trace"]
        queries.append(
            {
                "query_id": f"query-{receipt['run_id']}",
                "task_id": task["task_id"],
                "task_signature": sha256_json(
                    {
                        "suite": task["suite"],
                        "task_family": task["task_family"],
                        "question_sha256": task["question_sha256"],
                    }
                ),
                "task_family": task["task_family"],
                "question_sha256": task["question_sha256"],
                "reasoning_effort": receipt["reasoning_effort"],
                "source_trace_pointer": {
                    "path": relative.as_posix(),
                    "artifact_sha256": receipt["result_sha256"],
                    "trace_sha256": trace["trace_sha256"],
                    "json_pointer": "/trace",
                },
            }
        )
    if not queries:
        raise FrontierF1Error(
            "public G3 run contains no episode-family-backed M4 query traces"
        )
    _validate_query_factorial(queries)
    return queries


def _repository_inputs(
    root: Path,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    from .g3_freeze import validate_g3_freeze

    gate = _require_approved_f1_gate(root)
    evidence = _approved_public_source_evidence(root, gate)
    freeze = _read_public_json(
        root, Path("results/v2/memory/g3_public_freeze.json"), "G3 public freeze"
    )
    try:
        validate_g3_freeze(freeze)
    except ValueError as exc:
        raise FrontierF1Error("G3 public freeze is invalid") from exc
    public_run = _read_public_json(
        root,
        Path("results/v2/memory/g3_public_generation_run.json"),
        "G3 public generation run",
    )
    run_body = _without_hash(public_run, "artifact_sha256")
    if (
        public_run.get("schema_version") != "contextlab.g3-public-generation-run.v1"
        or public_run.get("artifact_sha256") != sha256_json(run_body)
        or public_run.get("g3_freeze_sha256") != freeze.get("artifact_sha256")
        or public_run.get("frozen_manifest_sha256")
        != freeze["manifest"].get("frozen_manifest_sha256")
        or not isinstance(public_run.get("cells"), list)
    ):
        raise FrontierF1Error("G3 public generation run is invalid")
    facts = next(
        row["facts"] for row in evidence["observations"] if row["experiment_id"] == "F1"
    )
    m4_cells = [
        row
        for row in public_run["cells"]
        if isinstance(row, Mapping) and row.get("policy") == "M4"
    ]
    if (
        len(freeze["manifest"]["m4_episode_seed"]) != facts["episode_card_count"]
        or len(m4_cells) != facts["expected_m4_trace_count"]
        or facts["m4_trace_count"] != facts["expected_m4_trace_count"]
        or facts["all_traces_valid"] is not True
    ):
        raise FrontierF1Error("public G3 inputs differ from approved F1 entry facts")
    episodes = _public_g3_episodes(root, freeze)
    eligible_task_families = sorted({episode["task_family"] for episode in episodes})
    queries = _public_g3_queries(
        root,
        freeze,
        public_run,
        eligible_task_families=eligible_task_families,
    )
    return gate, freeze, episodes, queries


def prepare_f1_indexed_memory_lab(
    root: Path, *, monotonic_ns: Callable[[], int] = time.perf_counter_ns
) -> dict[str, Any]:
    """Measure the canonical provider-free F1 preparation after approved entry."""

    repository = root.resolve()
    gate, freeze, episodes, queries = _repository_inputs(repository)
    lab = _assemble_lab(
        episodes=episodes,
        queries=queries,
        frontier_entry_gate_sha256=str(gate["artifact_sha256"]),
        g3_freeze_sha256=str(freeze["artifact_sha256"]),
        fixture_preparation_time_ms=None,
        monotonic_ns=monotonic_ns,
    )
    validate_f1_indexed_memory_lab(lab, require_generated=False)
    return lab


def _assert_canonical_sources(root: Path, value: Mapping[str, Any]) -> None:
    gate, freeze, episodes, queries = _repository_inputs(root)
    normalized_episodes = sorted(
        (_normalize_episode(row) for row in episodes),
        key=lambda row: (row["rank"], row["episode_id"]),
    )
    effort_order = {effort: index for index, effort in enumerate(F1_REASONING_EFFORTS)}
    normalized_queries = sorted(
        (_normalize_query(row) for row in queries),
        key=lambda row: (row["task_id"], effort_order[row["reasoning_effort"]]),
    )
    if (
        value.get("frontier_entry_gate_sha256") != gate.get("artifact_sha256")
        or value.get("g3_freeze_sha256") != freeze.get("artifact_sha256")
        or value.get("episode_index") != normalized_episodes
        or value.get("queries") != normalized_queries
        or value.get("protocol", {}).get("preparation_latency_source") != "monotonic_ns"
    ):
        raise FrontierF1Error("F1 lab differs from canonical public G3 inputs")


def _safe_create_only_path(root: Path, relative: Path) -> Path:
    """Preflight existing path components without creating through pathnames."""

    if relative.is_absolute() or ".." in relative.parts:
        raise FrontierF1Error("F1 artifact path escapes the repository")
    raw_root = root.absolute()
    if raw_root.is_symlink() or not raw_root.is_dir():
        raise FrontierF1Error("F1 repository root is missing or unsafe")
    resolved_root = raw_root.resolve(strict=True)
    parent = raw_root
    for part in relative.parent.parts:
        parent = parent / part
        if not os.path.lexists(parent):
            break
        if parent.is_symlink() or not parent.is_dir():
            raise FrontierF1Error("F1 artifact parent is unsafe")
        try:
            parent.resolve(strict=True).relative_to(resolved_root)
        except ValueError as exc:
            raise FrontierF1Error("F1 artifact parent escapes the repository") from exc
    target = raw_root / relative
    if os.path.lexists(target) and target.is_symlink():
        raise FrontierF1Error("F1 immutable artifact path is a symlink")
    return target


def _write_create_only(root: Path, relative: Path, value: Mapping[str, Any]) -> Path:
    path = _safe_create_only_path(root, relative)
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        write_bytes_once_or_verify(root, path, data)
    except ImmutableIOError as exc:
        raise FrontierF1Error(
            f"immutable F1 artifact differs or is unsafe: {path}"
        ) from exc
    return path


def _paid_ledger_rows(root: Path) -> list[dict[str, Any]]:
    path = canonical_ledger_path(root)
    if (
        path.is_symlink()
        or not path.is_file()
        or path.resolve(strict=True).parent
        != (root / "results/v2/cost").resolve(strict=False)
    ):
        raise FrontierF1Error("F1 paid ledger evidence is missing or unsafe")
    rows: list[dict[str, Any]] = []
    try:
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise FrontierF1Error(
                    f"F1 paid ledger row {line_number} must be an object"
                )
            rows.append(row)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrontierF1Error("F1 paid ledger evidence cannot be read") from exc
    return rows


def _ledger_amount(value: object, label: str) -> Decimal:
    return Decimal(_decimal_text(value, label))


def _ledger_events(
    rows: Sequence[Mapping[str, Any]], reservation_id: str
) -> list[Mapping[str, Any]]:
    events = [row for row in rows if row.get("reservation_id") == reservation_id]
    if not events or any(
        row.get("schema_version") != "contextlab.cost-event.v1" for row in events
    ):
        raise FrontierF1Error("F1 paid ledger event schema changed")
    return events


def _validate_paid_completion_ledger(
    root: Path, completions: Sequence[Mapping[str, Any]]
) -> None:
    """Bind every imported F1 call to one canonical paid-ledger lifecycle."""

    rows = _paid_ledger_rows(root)
    request_ids = [str(row["generator_receipt"]["request_id"]) for row in completions]
    reservation_ids = [
        str(row["generator_receipt"]["ledger_reservation_id"]) for row in completions
    ]
    if len(set(request_ids)) != len(request_ids) or len(set(reservation_ids)) != len(
        reservation_ids
    ):
        raise FrontierF1Error("F1 paid completion IDs must be unique across cells")

    for completion in completions:
        receipt = completion["generator_receipt"]
        reservation_id = str(receipt["ledger_reservation_id"])
        events = _ledger_events(rows, reservation_id)
        event_names = [row.get("event") for row in events]
        reserve = events[0] if events else None
        if (
            not isinstance(reserve, Mapping)
            or reserve.get("event") != "reserve"
            or reserve.get("call_count") != 1
            or isinstance(reserve.get("input_token_limit"), bool)
            or not isinstance(reserve.get("input_token_limit"), int)
            or reserve["input_token_limit"] < 0
            or isinstance(reserve.get("output_token_limit"), bool)
            or not isinstance(reserve.get("output_token_limit"), int)
            or reserve["output_token_limit"] < 0
        ):
            raise FrontierF1Error("F1 paid ledger reservation is invalid")
        estimated = _ledger_amount(reserve.get("estimated_usd"), "F1 reserved cost")
        if receipt["status"] == "completed":
            if event_names != ["reserve", "acknowledge", "settle"]:
                raise FrontierF1Error(
                    "F1 completed call requires reserve, acknowledge, settle only"
                )
            acknowledgement = events[1].get("metadata")
            settlement = events[2]
            metadata = settlement.get("metadata")
            if not isinstance(acknowledgement, Mapping) or not isinstance(
                metadata, Mapping
            ):
                raise FrontierF1Error("F1 paid ledger provider metadata is missing")
            usage = receipt["native_usage"]
            billed = _ledger_amount(receipt["billed_cost_usd"], "F1 billed cost")
            if billed > estimated:
                raise FrontierF1Error("F1 billed cost exceeds its reservation")
            if (
                acknowledgement.get("request_id") != receipt["request_id"]
                or metadata.get("request_id") != receipt["request_id"]
                or str(acknowledgement.get("provider", "")).casefold() != PROVIDER_SLUG
                or str(metadata.get("provider", "")).casefold() != PROVIDER_SLUG
                or acknowledgement.get("resolved_model") != receipt["resolved_model"]
                or metadata.get("requested_model") != MODEL_ID
                or metadata.get("resolved_model") != receipt["resolved_model"]
                or acknowledgement.get("prompt_tokens") != usage["prompt_tokens"]
                or acknowledgement.get("completion_tokens")
                != usage["completion_tokens"]
                or metadata.get("native_prompt_tokens") != usage["prompt_tokens"]
                or metadata.get("native_completion_tokens")
                != usage["completion_tokens"]
                or metadata.get("native_reasoning_tokens") != usage["reasoning_tokens"]
                or _ledger_amount(events[2].get("actual_usd"), "F1 settled cost")
                != billed
                or _ledger_amount(metadata.get("actual_usd"), "F1 settlement cost")
                != billed
                or metadata.get("latency_ms") != receipt["latency_ms"]
                or metadata.get("retry_count") != receipt["retry_count"]
                or metadata.get("error") is not None
            ):
                raise FrontierF1Error(
                    "F1 paid ledger differs from the imported provider receipt"
                )
            continue

        if event_names not in (
            ["reserve", "acknowledge", "failure", "cancel"],
            ["reserve", "acknowledge", "failure", "settle"],
            ["reserve", "acknowledge", "settle", "failure"],
        ):
            raise FrontierF1Error(
                "F1 failed call requires acknowledge, failure, and cancel or settle"
            )
        acknowledgement = events[1].get("metadata")
        failure = next(row for row in events if row.get("event") == "failure")
        metadata = failure.get("metadata")
        error = receipt["error"]
        usage = receipt["native_usage"]
        if (
            not isinstance(acknowledgement, Mapping)
            or acknowledgement.get("request_id") != receipt["request_id"]
            or str(acknowledgement.get("provider", "")).casefold() != PROVIDER_SLUG
            or acknowledgement.get("resolved_model") != receipt["resolved_model"]
            or acknowledgement.get("prompt_tokens") != usage["prompt_tokens"]
            or acknowledgement.get("completion_tokens") != usage["completion_tokens"]
            or not isinstance(failure.get("stage"), str)
            or not failure["stage"]
            or failure.get("reason") != error
            or not isinstance(metadata, Mapping)
            or metadata.get("request_id") != receipt["request_id"]
            or metadata.get("error") != error
        ):
            raise FrontierF1Error("F1 failed ledger receipt is not authoritative")
        billed = _ledger_amount(receipt["billed_cost_usd"], "F1 failed billed cost")
        terminal = next(
            row for row in events if row.get("event") in {"cancel", "settle"}
        )
        if terminal.get("event") == "cancel":
            if (
                billed != Decimal("0")
                or not isinstance(terminal.get("reason"), str)
                or not terminal["reason"]
            ):
                raise FrontierF1Error(
                    "F1 cancelled failed call must have zero billed cost"
                )
            continue

        settlement_metadata = terminal.get("metadata")
        if not isinstance(settlement_metadata, Mapping) or (
            _ledger_amount(terminal.get("actual_usd"), "F1 failed settled cost")
            != billed
            or _ledger_amount(
                settlement_metadata.get("actual_usd"),
                "F1 failed settlement cost",
            )
            != billed
            or settlement_metadata.get("request_id") != receipt["request_id"]
            or settlement_metadata.get("requested_model") != MODEL_ID
            or settlement_metadata.get("resolved_model") != receipt["resolved_model"]
            or str(settlement_metadata.get("provider", "")).casefold() != PROVIDER_SLUG
            or settlement_metadata.get("native_prompt_tokens") != usage["prompt_tokens"]
            or settlement_metadata.get("native_completion_tokens")
            != usage["completion_tokens"]
            or settlement_metadata.get("native_reasoning_tokens")
            != usage["reasoning_tokens"]
            or settlement_metadata.get("latency_ms") != receipt["latency_ms"]
            or settlement_metadata.get("retry_count") != receipt["retry_count"]
            or settlement_metadata.get("error") not in {None, error}
        ):
            raise FrontierF1Error(
                "F1 failed-call settlement differs from the imported provider receipt"
            )


def write_f1_indexed_memory_lab(root: Path, value: Mapping[str, Any]) -> Path:
    """Create the approved, measured, pending-generation F1 artifact once."""

    if root.absolute().is_symlink():
        raise FrontierF1Error("F1 repository root is unsafe")
    repository = root.resolve()
    _require_approved_f1_gate(repository)
    validate_f1_indexed_memory_lab(value, require_generated=False)
    _assert_canonical_sources(repository, value)
    return _write_create_only(repository, F1_PREPARED_LAB_PATH, value)


def write_f1_generated_lab(root: Path, value: Mapping[str, Any]) -> Path:
    """Create the generated-pending-review F1 artifact after strict validation."""

    if root.absolute().is_symlink():
        raise FrontierF1Error("F1 repository root is unsafe")
    repository = root.resolve()
    _require_approved_f1_gate(repository)
    validate_f1_indexed_memory_lab(value, require_generated=True)
    _assert_canonical_sources(repository, value)
    prepared_path = repository / F1_PREPARED_LAB_PATH
    prepared = _read_public_json(repository, F1_PREPARED_LAB_PATH, "prepared F1 lab")
    validate_f1_indexed_memory_lab(prepared, require_generated=False)
    if (
        not prepared_path.is_file()
        or prepared_path.is_symlink()
        or prepared.get("artifact_sha256") != value.get("prepared_lab_sha256")
    ):
        raise FrontierF1Error("generated F1 lab is not bound to the saved prepared lab")
    _validate_generated_public_artifacts(repository, value)
    _validate_paid_completion_ledger(repository, value["completion_records"])
    return _write_create_only(repository, F1_GENERATED_LAB_PATH, value)
