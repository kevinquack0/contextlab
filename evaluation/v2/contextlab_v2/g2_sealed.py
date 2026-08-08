"""Strict import boundary for external G2 sealed-evaluator results."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping

from .baseline import repository_root
from .tasking import sha256_json


G2_SEALED_RETURN_SCHEMA = "contextlab.g2-sealed-return.v1"
G2_SEALED_IMPORT_SCHEMA = "contextlab.g2-sealed-import.v1"
STATIC_FREEZE_SCHEMA = "contextlab.static-freeze.v1"

G2_SEALED_TASK_IDS = tuple(f"S{number:03d}" for number in range(81, 117))
G2_STRATEGY_IDS = tuple(f"R{number}" for number in range(8))
G2_COMPONENT_CELL_COUNT = len(G2_SEALED_TASK_IDS) * len(G2_STRATEGY_IDS)

UNIT_INTERVAL_METRICS = frozenset(
    {
        "recall_at_k",
        "precision_at_k",
        "reciprocal_rank",
        "ndcg",
        "required_source_coverage",
        "context_recall",
        "context_required_source_coverage",
    }
)
COUNT_METRICS = frozenset({"source_diversity", "candidate_tokens", "context_tokens"})
NONNEGATIVE_METRICS = frozenset({"retrieval_latency_ms"})
COST_METRICS = frozenset({"retrieval_cost_usd"})
COMPONENT_METRIC_FIELDS = (
    UNIT_INTERVAL_METRICS | COUNT_METRICS | NONNEGATIVE_METRICS | COST_METRICS
)

_RETURN_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "static_freeze_manifest_sha256",
        "external_bundle_sha256",
        "retrieval_protocol_sha256",
        "component_records",
    }
)
_RETURN_OPTIONAL_FIELDS = frozenset({"generation_summary"})
_COMPONENT_RECORD_FIELDS = frozenset(
    {"task_id", "strategy_id", "trace_commitment_sha256", "metrics"}
)
_GENERATION_SUMMARY_FIELDS = frozenset(
    {
        "generation_count",
        "status_counts",
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "cost_usd",
        "latency_ms",
        "screening_by_strategy_effort",
    }
)
_GENERATION_STATUS_FIELDS = frozenset({"completed", "failed", "pending"})
_LATENCY_DISTRIBUTION_FIELDS = frozenset(
    {"n", "mean", "sample_stddev", "min", "median", "p50", "p95", "max"}
)
_SCREENING_FIELDS = frozenset(
    {
        "n",
        "accepted_proxy_rate",
        "expected_content_token_recall",
        "critical_value_recall",
        "citation_precision",
        "required_evidence_citation_recall",
        "unsupported_citation_count",
        "abstention_task_count",
        "abstention_correct_count",
    }
)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_DECIMAL_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")


class G2SealedImportError(ValueError):
    """An external G2 return violates the sealed-data boundary."""


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise G2SealedImportError(f"{label} must be an object")
    return value


def _exact_fields(
    value: Mapping[str, Any],
    required: frozenset[str],
    label: str,
    *,
    optional: frozenset[str] = frozenset(),
) -> None:
    actual = set(value)
    missing = required.difference(actual)
    unknown = actual.difference(required | optional)
    if missing or unknown:
        raise G2SealedImportError(
            f"{label} fields differ; missing={sorted(missing)}, "
            f"unknown={sorted(unknown)}"
        )


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_PATTERN.fullmatch(value) is not None


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise G2SealedImportError("sealed import JSON contains a duplicate field")
        value[key] = item
    return value


def _reject_json_constant(_value: str) -> None:
    raise G2SealedImportError("sealed import JSON contains a non-finite number")


def _decode_json(data: bytes, label: str) -> Any:
    try:
        text = data.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except G2SealedImportError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise G2SealedImportError(f"{label} is not valid strict JSON") from exc


def _read_json(path: Path, label: str) -> tuple[Any, bytes]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise G2SealedImportError(f"cannot read {label}") from exc
    return _decode_json(data, label), data


def _validate_static_freeze(manifest: Any) -> tuple[str, str]:
    value = _require_object(manifest, "static G2 freeze manifest")
    if value.get("schema_version") != STATIC_FREEZE_SCHEMA:
        raise G2SealedImportError("unsupported static G2 freeze manifest schema")
    manifest_hash = value.get("manifest_sha256")
    body = {key: item for key, item in value.items() if key != "manifest_sha256"}
    if not _is_sha256(manifest_hash) or manifest_hash != sha256_json(body):
        raise G2SealedImportError("static G2 freeze manifest hash mismatch")
    if value.get("benchmark_status") != "frozen":
        raise G2SealedImportError("static G2 benchmark is not frozen")

    tasks = value.get("tasks")
    if not isinstance(tasks, list):
        raise G2SealedImportError("static G2 freeze manifest has no task list")
    sealed_ids: list[str] = []
    for row in tasks:
        task = _require_object(row, "static G2 task record")
        if task.get("partition") == "sealed_capability":
            task_id = task.get("task_id")
            if not isinstance(task_id, str):
                raise G2SealedImportError("static G2 sealed task ID is invalid")
            sealed_ids.append(task_id)
    if tuple(sealed_ids) != G2_SEALED_TASK_IDS:
        raise G2SealedImportError(
            "static G2 freeze must seal exactly tasks S081 through S116"
        )

    external = _require_object(
        value.get("external_sealed_bundle"), "external sealed bundle commitment"
    )
    _exact_fields(
        external,
        frozenset({"task_count", "bundle_sha256", "contents"}),
        "external sealed bundle commitment",
    )
    if external.get("task_count") != len(G2_SEALED_TASK_IDS):
        raise G2SealedImportError("external sealed bundle task count is invalid")
    external_hash = external.get("bundle_sha256")
    if not _is_sha256(external_hash):
        raise G2SealedImportError("external sealed bundle commitment is invalid")
    if external.get("contents") != "withheld_by_external_evaluator":
        raise G2SealedImportError("external sealed bundle contents are not withheld")
    return manifest_hash, external_hash


def _validate_protocol(protocol: Any) -> str:
    value = _require_object(protocol, "retrieval protocol")
    if value.get("schema_version") != "contextlab.retrieval-protocol.v1":
        raise G2SealedImportError("unsupported retrieval protocol schema")
    methods = _require_object(value.get("methods"), "retrieval protocol methods")
    if set(methods) != set(G2_STRATEGY_IDS):
        raise G2SealedImportError(
            "retrieval protocol must define exactly R0 through R7"
        )
    return sha256_json(value)


def _number(value: Any, label: str, *, upper: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise G2SealedImportError(f"{label} must be numeric")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise G2SealedImportError(f"{label} must be a finite number") from exc
    if (
        not math.isfinite(number)
        or number < 0
        or (upper is not None and number > upper)
    ):
        range_label = f"0..{upper:g}" if upper is not None else "non-negative"
        raise G2SealedImportError(f"{label} must be finite and {range_label}")
    return number


def _nonnegative_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise G2SealedImportError(f"{label} must be a non-negative integer")
    return value


def _nonnegative_cost(value: Any, label: str) -> None:
    if isinstance(value, bool):
        raise G2SealedImportError(f"{label} must be a non-negative decimal")
    if isinstance(value, str):
        if _DECIMAL_PATTERN.fullmatch(value) is None:
            raise G2SealedImportError(f"{label} must be a non-negative decimal")
        decimal_value = Decimal(value)
    elif isinstance(value, (int, float)):
        try:
            decimal_value = Decimal(str(value))
        except InvalidOperation as exc:
            raise G2SealedImportError(
                f"{label} must be a non-negative decimal"
            ) from exc
    else:
        raise G2SealedImportError(f"{label} must be a non-negative decimal")
    if not decimal_value.is_finite() or decimal_value < 0:
        raise G2SealedImportError(f"{label} must be finite and non-negative")


def _validate_metrics(metrics: Any, index: int) -> None:
    value = _require_object(metrics, f"component record {index} metrics")
    _exact_fields(value, COMPONENT_METRIC_FIELDS, f"component record {index} metrics")
    for field in UNIT_INTERVAL_METRICS:
        _number(value[field], f"component record {index} metric {field}", upper=1.0)
    for field in COUNT_METRICS:
        _nonnegative_integer(value[field], f"component record {index} metric {field}")
    for field in NONNEGATIVE_METRICS:
        _number(value[field], f"component record {index} metric {field}")
    for field in COST_METRICS:
        _nonnegative_cost(value[field], f"component record {index} metric {field}")


def _validate_generation_summary(summary: Any) -> None:
    value = _require_object(summary, "generation_summary")
    _exact_fields(value, _GENERATION_SUMMARY_FIELDS, "generation_summary")
    generation_count = _nonnegative_integer(
        value["generation_count"], "generation_summary generation_count"
    )
    if generation_count != len(G2_SEALED_TASK_IDS) * len(G2_STRATEGY_IDS) * 2:
        raise G2SealedImportError(
            "generation_summary must cover 36 tasks by 8 strategies by 2 efforts"
        )
    for field in ("prompt_tokens", "completion_tokens", "reasoning_tokens"):
        _nonnegative_integer(value[field], f"generation_summary {field}")
    _nonnegative_cost(value["cost_usd"], "generation_summary cost_usd")
    latency = _require_object(value["latency_ms"], "generation_summary latency_ms")
    _exact_fields(
        latency, _LATENCY_DISTRIBUTION_FIELDS, "generation_summary latency_ms"
    )
    latency_n = _nonnegative_integer(latency["n"], "generation_summary latency_ms n")
    latency_values = {
        field: _number(latency[field], f"generation_summary latency_ms {field}")
        for field in _LATENCY_DISTRIBUTION_FIELDS.difference({"n"})
    }
    if latency_values["median"] != latency_values["p50"] or not (
        latency_values["min"]
        <= latency_values["p50"]
        <= latency_values["p95"]
        <= latency_values["max"]
    ):
        raise G2SealedImportError(
            "generation_summary latency distribution is inconsistent"
        )

    statuses = _require_object(
        value["status_counts"], "generation_summary status_counts"
    )
    _exact_fields(
        statuses, _GENERATION_STATUS_FIELDS, "generation_summary status_counts"
    )
    status_total = sum(
        _nonnegative_integer(
            statuses[field], f"generation_summary status count {field}"
        )
        for field in _GENERATION_STATUS_FIELDS
    )
    if status_total != generation_count:
        raise G2SealedImportError(
            "generation_summary status counts differ from generation_count"
        )
    completed = statuses["completed"]
    if latency_n != completed:
        raise G2SealedImportError(
            "generation_summary latency count differs from completed generations"
        )

    screening = _require_object(
        value["screening_by_strategy_effort"],
        "generation_summary screening_by_strategy_effort",
    )
    expected_screening_keys = {
        f"{strategy}:{effort}"
        for strategy in G2_STRATEGY_IDS
        for effort in ("low", "high")
    }
    if set(screening) != expected_screening_keys:
        raise G2SealedImportError(
            "generation_summary screening must cover R0-R7 at low and high effort"
        )
    screened = 0
    for key, raw_row in screening.items():
        row = _require_object(raw_row, f"generation_summary screening {key}")
        _exact_fields(row, _SCREENING_FIELDS, f"generation_summary screening {key}")
        n = _nonnegative_integer(row["n"], f"generation_summary screening {key} n")
        if n > len(G2_SEALED_TASK_IDS):
            raise G2SealedImportError(
                f"generation_summary screening {key} has too many cells"
            )
        screened += n
        for field in (
            "accepted_proxy_rate",
            "expected_content_token_recall",
            "critical_value_recall",
            "citation_precision",
            "required_evidence_citation_recall",
        ):
            _number(
                row[field],
                f"generation_summary screening {key} {field}",
                upper=1.0,
            )
        unsupported = _nonnegative_integer(
            row["unsupported_citation_count"],
            f"generation_summary screening {key} unsupported_citation_count",
        )
        abstention_tasks = _nonnegative_integer(
            row["abstention_task_count"],
            f"generation_summary screening {key} abstention_task_count",
        )
        abstention_correct = _nonnegative_integer(
            row["abstention_correct_count"],
            f"generation_summary screening {key} abstention_correct_count",
        )
        if (
            unsupported < 0
            or abstention_tasks > n
            or abstention_correct > abstention_tasks
        ):
            raise G2SealedImportError(
                f"generation_summary screening {key} counts are inconsistent"
            )
    if screened != completed:
        raise G2SealedImportError(
            "generation_summary screening count differs from completed generations"
        )


def validate_g2_sealed_return(
    bundle: Any,
    *,
    static_freeze_manifest_sha256: str,
    external_bundle_sha256: str,
    retrieval_protocol_sha256: str,
) -> None:
    """Validate a parsed return against the three frozen G2 commitments."""
    value = _require_object(bundle, "G2 sealed return")
    _exact_fields(
        value,
        _RETURN_REQUIRED_FIELDS,
        "G2 sealed return",
        optional=_RETURN_OPTIONAL_FIELDS,
    )
    if value["schema_version"] != G2_SEALED_RETURN_SCHEMA:
        raise G2SealedImportError("unsupported G2 sealed-return schema")
    commitments = (
        ("static_freeze_manifest_sha256", static_freeze_manifest_sha256),
        ("external_bundle_sha256", external_bundle_sha256),
        ("retrieval_protocol_sha256", retrieval_protocol_sha256),
    )
    for field, expected in commitments:
        if not _is_sha256(value[field]) or value[field] != expected:
            raise G2SealedImportError(f"G2 sealed return {field} mismatch")

    records = value["component_records"]
    if not isinstance(records, list) or len(records) != G2_COMPONENT_CELL_COUNT:
        raise G2SealedImportError(
            "G2 sealed return must contain exactly 288 component records"
        )
    seen_cells: set[tuple[str, str]] = set()
    for index, record in enumerate(records):
        row = _require_object(record, f"component record {index}")
        _exact_fields(row, _COMPONENT_RECORD_FIELDS, f"component record {index}")
        task_id = row["task_id"]
        strategy_id = row["strategy_id"]
        if task_id not in G2_SEALED_TASK_IDS:
            raise G2SealedImportError(
                f"component record {index} has an invalid task ID"
            )
        if strategy_id not in G2_STRATEGY_IDS:
            raise G2SealedImportError(
                f"component record {index} has an invalid strategy ID"
            )
        cell = (task_id, strategy_id)
        if cell in seen_cells:
            raise G2SealedImportError(f"component record {index} duplicates a cell")
        seen_cells.add(cell)
        if not _is_sha256(row["trace_commitment_sha256"]):
            raise G2SealedImportError(
                f"component record {index} trace commitment is invalid"
            )
        _validate_metrics(row["metrics"], index)

    expected_cells = {
        (task_id, strategy_id)
        for task_id in G2_SEALED_TASK_IDS
        for strategy_id in G2_STRATEGY_IDS
    }
    if seen_cells != expected_cells:
        raise G2SealedImportError(
            "G2 sealed return must cover every S081-S116 by R0-R7 cell"
        )
    if "generation_summary" in value:
        _validate_generation_summary(value["generation_summary"])


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(
                value,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def import_g2_sealed_return(
    external_path: Path,
    output_path: Path,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    """Import one content-free G2 sealed return from outside the repository."""
    repository = (root or repository_root()).resolve()
    source = external_path.resolve()
    if _is_relative_to(source, repository):
        raise G2SealedImportError(
            "G2 sealed-return input must be outside the repository"
        )

    results_root = (repository / "results/v2").resolve()
    if not _is_relative_to(results_root, repository):
        raise G2SealedImportError("results/v2 resolves outside the repository")
    output = output_path.resolve()
    if output == results_root or not _is_relative_to(output, results_root):
        raise G2SealedImportError("G2 sealed import output must stay under results/v2")

    bundle, source_bytes = _read_json(source, "G2 sealed return")
    freeze, _ = _read_json(
        repository / "results/v2/splits/static_g2_freeze.json",
        "static G2 freeze manifest",
    )
    protocol, _ = _read_json(
        repository / "evaluation/v2/retrieval_protocol.json",
        "retrieval protocol",
    )
    freeze_hash, external_hash = _validate_static_freeze(freeze)
    protocol_hash = _validate_protocol(protocol)
    validate_g2_sealed_return(
        bundle,
        static_freeze_manifest_sha256=freeze_hash,
        external_bundle_sha256=external_hash,
        retrieval_protocol_sha256=protocol_hash,
    )

    imported: dict[str, Any] = {
        "schema_version": G2_SEALED_IMPORT_SCHEMA,
        "static_freeze_manifest_sha256": freeze_hash,
        "external_bundle_sha256": external_hash,
        "retrieval_protocol_sha256": protocol_hash,
        "source_return_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "component_records": bundle["component_records"],
    }
    if "generation_summary" in bundle:
        imported["generation_summary"] = bundle["generation_summary"]

    output.parent.mkdir(parents=True, exist_ok=True)
    resolved_after_create = output.resolve()
    if not _is_relative_to(resolved_after_create, results_root):
        raise G2SealedImportError("G2 sealed import output escaped results/v2")
    _atomic_write_json(resolved_after_create, imported)
    return imported
