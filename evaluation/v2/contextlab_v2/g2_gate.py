"""Content-free final acceptance gate for the frozen G2 retrieval study.

The gate consumes canonical public repository evidence and the narrow,
metric-only sealed import. It never reads the external sealed bundle or its
evaluator work directory.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import datetime as dt
import hashlib
import json
import math
import re
import fcntl
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .answer_metrics import ANSWER_METRICS_SCHEMA
from .experiments import METHOD_IDS
from .g2_sealed import (
    G2_COMPONENT_CELL_COUNT,
    G2_SEALED_IMPORT_SCHEMA,
    G2_SEALED_RETURN_SCHEMA,
    G2_SEALED_TASK_IDS,
    validate_g2_sealed_return,
)
from .costs import HARD_CAP_USD, canonical_ledger_path, estimate_cost
from .generations import GenerationBatchError, validate_generation_manifest_envelope
from .provider import ALLOWED_REASONING_EFFORTS, MODEL_ID
from .reports import ANALYSIS_SCHEMA, PARENT_METHOD, analyze_component_lab, validate_lab
from .repeats import REPEAT_ANALYSIS_SCHEMA, REPEAT_CELL_COUNT, REPEAT_TRIAL_COUNT
from .statistics import distribution_summary, paired_bootstrap_ci
from .static_benchmark import public_static_tasks, validate_static_freeze
from .tasking import sha256_json


G2_GATE_SCHEMA = "contextlab.g2-final-gate.v1"
G2_HUMAN_APPROVAL_SCHEMA = "contextlab.g2-human-approval.v1"
G2_PAID_LEDGER_FREEZE_SCHEMA = "contextlab.g2-paid-ledger-freeze.v1"
G2_PAID_LEDGER_FREEZE_PATH = Path("results/v2/cost/g2_paid_ledger_freeze.json")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_FORBIDDEN_SEALED_FIELD = re.compile(r"(?:question|gold|answer|trace)", re.I)
_SAFE_TRACE_FIELD = "trace_commitment_sha256"
_REPEAT_TASK_IDS = (
    "S080",
    "S043",
    "S020",
    "S004",
    "S007",
    "S014",
    "S051",
    "S025",
    "S040",
    "S070",
    "S052",
    "S063",
)


class G2GateError(ValueError):
    """G2 evidence is missing, altered, or cannot support a gate decision."""


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise G2GateError(f"{label} must be an object")
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise G2GateError(f"{label} must be a lowercase SHA-256")
    return value


def _number(value: Any, label: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise G2GateError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise G2GateError(f"{label} must be finite and at least {minimum:g}")
    return number


def _cost(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise G2GateError(f"{label} must be a finite decimal") from exc
    if not result.is_finite() or result < 0:
        raise G2GateError(f"{label} must be a non-negative finite decimal")
    return result


def _hash_checked(
    value: Mapping[str, Any], field: str, schema: str, label: str
) -> None:
    if value.get("schema_version") != schema:
        raise G2GateError(f"unsupported {label} schema")
    body = {key: item for key, item in value.items() if key != field}
    if value.get(field) != sha256_json(body):
        raise G2GateError(f"{label} hash mismatch")


def _protocol_contract(
    protocol: Mapping[str, Any],
) -> tuple[str, Mapping[str, Any], Mapping[str, Any]]:
    methods = _object(protocol.get("methods"), "retrieval protocol methods")
    fixed = _object(protocol.get("fixed_comparison"), "retrieval fixed comparison")
    promotion = _object(protocol.get("promotion"), "retrieval promotion contract")
    if set(methods) != set(METHOD_IDS):
        raise G2GateError("retrieval protocol must define R0 through R7")
    if fixed.get("generation_campaign_id") != "g2r2":
        raise G2GateError("G2 gate accepts only the frozen g2r2 campaign")
    if fixed.get("answer_generator") != MODEL_ID:
        raise G2GateError("retrieval protocol model differs from the pinned model")
    if fixed.get("output_token_limit") != 8192 or fixed.get("temperature") != 0.0:
        raise G2GateError(
            "retrieval protocol output limit differs from the frozen limit"
        )
    if tuple(fixed.get("reasoning_efforts", ())) != ALLOWED_REASONING_EFFORTS:
        raise G2GateError("retrieval protocol reasoning efforts differ from low/high")
    if promotion.get("stochastic_trial_count") != REPEAT_TRIAL_COUNT:
        raise G2GateError("retrieval protocol does not require five repeat trials")
    if tuple(promotion.get("temperature_zero_repeat_task_ids", ())) != _REPEAT_TASK_IDS:
        raise G2GateError(
            "retrieval protocol repeat task IDs differ from the frozen sample"
        )
    return sha256_json(protocol), fixed, promotion


def _static_commitments(
    static_freeze: Mapping[str, Any], root: Path
) -> tuple[str, str, dict[str, Any]]:
    try:
        canonical = json.loads(
            (root / "results/v2/splits/static_g2_freeze.json").read_text(
                encoding="utf-8"
            )
        )
        validate_static_freeze(canonical, root)
    except Exception as exc:
        raise G2GateError(f"static G2 freeze allocation is invalid: {exc}") from exc
    if dict(static_freeze) != canonical:
        raise G2GateError(
            "supplied static freeze differs from the canonical frozen manifest"
        )
    external = _object(
        canonical.get("external_sealed_bundle"), "sealed bundle commitment"
    )
    return (
        str(canonical["manifest_sha256"]),
        _sha(external.get("bundle_sha256"), "external sealed bundle hash"),
        canonical,
    )


def _canonical_public_grid(
    supplied_tasks: Sequence[Mapping[str, Any]], freeze: Mapping[str, Any], root: Path
) -> dict[str, Mapping[str, Any]]:
    try:
        canonical = public_static_tasks(root)
    except Exception as exc:
        raise G2GateError(f"cannot load canonical frozen public tasks: {exc}") from exc
    if list(supplied_tasks) != canonical:
        raise G2GateError("supplied public tasks differ from canonical frozen tasks")
    by_id = {str(task.get("task_id")): task for task in canonical}
    if len(by_id) != 84:
        raise G2GateError("canonical freeze does not expose exactly 84 public tasks")
    frozen = {str(row.get("task_id")): row for row in freeze["tasks"]}
    if set(frozen) != {f"S{number:03d}" for number in range(1, 121)}:
        raise G2GateError("canonical freeze task allocation is incomplete")
    for task_id, task in by_id.items():
        row = frozen.get(task_id)
        if row is None or row.get("question_sha256") != task.get("question_sha256"):
            raise G2GateError("canonical public task question commitment changed")
    if (
        tuple(
            task_id
            for task_id, row in frozen.items()
            if row.get("partition") == "sealed_capability"
        )
        != G2_SEALED_TASK_IDS
    ):
        raise G2GateError(
            "canonical freeze sealed task allocation differs from S081-S116"
        )
    return by_id


def _validate_lab_public_grid(
    lab: Mapping[str, Any], public_tasks: Mapping[str, Mapping[str, Any]]
) -> None:
    expected = {
        (task_id, strategy) for task_id in public_tasks for strategy in METHOD_IDS
    }
    observed: set[tuple[str, str]] = set()
    for trace in lab["traces"]:
        task = _object(trace.get("task"), "component lab trace task")
        task_id, strategy = str(task.get("task_id")), str(trace.get("strategy_id"))
        key = (task_id, strategy)
        if key in observed or key not in expected:
            raise G2GateError("component lab does not match the canonical public grid")
        canonical = public_tasks[task_id]
        if task.get("question_sha256") != canonical.get("question_sha256"):
            raise G2GateError(
                "component lab trace question commitment differs from freeze"
            )
        observed.add(key)
    if observed != expected or lab.get("task_count") != len(public_tasks):
        raise G2GateError(
            "component lab does not cover every frozen public task and strategy"
        )


def _validate_sealed_import(
    sealed_import: Mapping[str, Any],
    *,
    freeze_sha: str,
    external_sha: str,
    protocol_sha: str,
) -> Mapping[str, Any]:
    allowed = {
        "schema_version",
        "static_freeze_manifest_sha256",
        "external_bundle_sha256",
        "retrieval_protocol_sha256",
        "source_return_sha256",
        "component_records",
        "generation_summary",
    }
    if set(sealed_import) - allowed or not {
        "schema_version",
        "static_freeze_manifest_sha256",
        "external_bundle_sha256",
        "retrieval_protocol_sha256",
        "source_return_sha256",
        "component_records",
    } <= set(sealed_import):
        raise G2GateError("sealed import fields differ from the safe import schema")
    if sealed_import.get("schema_version") != G2_SEALED_IMPORT_SCHEMA:
        raise G2GateError("unsupported sealed import schema")
    if (
        sealed_import.get("static_freeze_manifest_sha256") != freeze_sha
        or sealed_import.get("external_bundle_sha256") != external_sha
        or sealed_import.get("retrieval_protocol_sha256") != protocol_sha
    ):
        raise G2GateError("sealed import commitments do not match the frozen G2 inputs")
    _sha(sealed_import.get("source_return_sha256"), "sealed source-return hash")
    # The import format has no artifact hash. Reuse the strict import boundary's
    # schema checker after translating its safe fields back to a return envelope.
    safe_return: dict[str, Any] = {
        "schema_version": G2_SEALED_RETURN_SCHEMA,
        "static_freeze_manifest_sha256": freeze_sha,
        "external_bundle_sha256": external_sha,
        "retrieval_protocol_sha256": protocol_sha,
        "component_records": sealed_import.get("component_records"),
    }
    if "generation_summary" in sealed_import:
        safe_return["generation_summary"] = sealed_import["generation_summary"]
    try:
        validate_g2_sealed_return(
            safe_return,
            static_freeze_manifest_sha256=freeze_sha,
            external_bundle_sha256=external_sha,
            retrieval_protocol_sha256=protocol_sha,
        )
    except Exception as exc:
        raise G2GateError(
            f"sealed import violates the content-free schema: {exc}"
        ) from exc

    # Guard against future relaxed import schemas. The only trace-like data allowed
    # here is a commitment hash, never a path or a trace body.
    def visit(value: Any, field: str = "") -> None:
        if _FORBIDDEN_SEALED_FIELD.search(field) and field != _SAFE_TRACE_FIELD:
            raise G2GateError("sealed import contains a prohibited content field")
        if isinstance(value, Mapping):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise G2GateError("sealed import contains a non-string field")
                visit(item, key)
        elif isinstance(value, list):
            for item in value:
                visit(item, field)

    visit(sealed_import)
    return sealed_import


def _validate_component_analysis(
    analysis: Mapping[str, Any],
    *,
    protocol_sha: str,
    lab_sha: str,
    promotion: Mapping[str, Any],
    lab: Mapping[str, Any],
    protocol: Mapping[str, Any],
    public_tasks: Sequence[Mapping[str, Any]],
) -> list[str]:
    _hash_checked(
        analysis, "analysis_sha256", ANALYSIS_SCHEMA, "public component analysis"
    )
    if (
        analysis.get("scope") != "public_component_evidence_only"
        or analysis.get("protocol_sha256") != protocol_sha
        or analysis.get("component_lab_sha256") != lab_sha
    ):
        raise G2GateError(
            "public component analysis has stale protocol or lab evidence"
        )
    try:
        recomputed = analyze_component_lab(lab, protocol, public_tasks)
    except Exception as exc:
        raise G2GateError(
            f"public component analysis cannot be recomputed: {exc}"
        ) from exc
    if dict(analysis) != recomputed:
        raise G2GateError(
            "public component analysis differs from the deterministic frozen-lab analysis"
        )
    leakage = _object(
        analysis.get("question_reference_leakage_audit"), "component leakage audit"
    )
    if (
        leakage.get("status") != "passed"
        or leakage.get("leaked_reference_count", 0) != 0
    ):
        raise G2GateError("public component leakage audit did not pass")
    methods = _object(analysis.get("methods"), "component methods")
    if set(methods) != set(METHOD_IDS):
        raise G2GateError("component analysis methods differ from frozen protocol")
    candidate_ids = analysis.get("public_component_candidates")
    if not isinstance(candidate_ids, list) or len(candidate_ids) != len(
        set(candidate_ids)
    ):
        raise G2GateError("public component candidate list is invalid")
    candidate_set = set(candidate_ids)
    if not candidate_set <= set(METHOD_IDS[1:]):
        raise G2GateError("public component candidate is not an experimental method")
    minimum = _number(
        promotion.get("minimum_target_family_delta"), "minimum target-family delta"
    )
    regression = _number(
        promotion.get("full_set_material_regression"),
        "full-set regression floor",
        minimum=-1.0,
    )
    latency_limit = _number(
        promotion.get("retrieval_p95_latency_ceiling_ms"), "retrieval latency ceiling"
    )
    for method_id in METHOD_IDS[1:]:
        row = _object(methods.get(method_id), f"component method {method_id}")
        if row.get("parent") != PARENT_METHOD[method_id]:
            raise G2GateError(f"component method {method_id} parent changed")
        criteria = _object(
            row.get("criteria"), f"component method {method_id} criteria"
        )
        if method_id in candidate_set:
            target = _object(
                row.get("target_bootstrap"), f"{method_id} target bootstrap"
            )
            full = _object(
                row.get("full_set_bootstrap"), f"{method_id} full-set bootstrap"
            )
            latency = _object(row.get("latency_ms"), f"{method_id} latency")
            expected = {
                "target_delta_meets_minimum": _number(
                    row.get("target_delta"), f"{method_id} target delta", minimum=-1.0
                )
                >= minimum,
                "target_ci_supports_direction": _number(
                    target.get("ci_lower"), f"{method_id} target CI lower", minimum=-1.0
                )
                >= 0.0,
                "full_set_not_materially_regressed": _number(
                    row.get("full_set_delta"),
                    f"{method_id} full-set delta",
                    minimum=-1.0,
                )
                >= regression,
                "latency_within_budget": _number(
                    latency.get("p95"), f"{method_id} p95 latency"
                )
                <= latency_limit,
                "question_reference_leakage_passed": True,
                "identifier_mask_check_passed": True,
                "retrieval_cost_is_zero": True,
            }
            if row.get("status") != "public_passed" or any(
                criteria.get(key) is not value for key, value in expected.items()
            ):
                raise G2GateError(
                    f"{method_id} public criteria are not a valid preregistered pass"
                )
            if (
                _number(
                    full.get("ci_lower"), f"{method_id} full-set CI lower", minimum=-1.0
                )
                < regression
            ):
                raise G2GateError(
                    f"{method_id} full-set paired CI violates the regression floor"
                )
    return sorted(candidate_set)


def _validate_manifests(
    manifests: Sequence[Mapping[str, Any]],
    *,
    lab: Mapping[str, Any],
    protocol: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    if (
        isinstance(manifests, (str, bytes))
        or not isinstance(manifests, Sequence)
        or len(manifests) != REPEAT_TRIAL_COUNT
    ):
        raise G2GateError("G2 final gate requires exactly five generation manifests")
    validated: list[Mapping[str, Any]] = []
    lab_task_ids = {str(trace["task"]["task_id"]) for trace in lab["traces"]}
    if len(lab_task_ids) != lab.get("task_count"):
        raise G2GateError("public component lab task allocation is incomplete")
    for trial, manifest in enumerate(manifests, start=1):
        item = _object(manifest, f"generation manifest {trial}")
        try:
            validate_generation_manifest_envelope(
                item, lab, protocol, expected_trial=trial
            )
        except GenerationBatchError as exc:
            raise G2GateError(f"generation manifest {trial} is invalid: {exc}") from exc
        if (
            item.get("requested_model") != MODEL_ID
            or item.get("output_token_limit")
            != protocol["fixed_comparison"]["output_token_limit"]
        ):
            raise G2GateError(
                "generation manifest model or output limit differs from the protocol"
            )
        expected_tasks = lab_task_ids if trial == 1 else set(_REPEAT_TASK_IDS)
        expected_cells = {
            (task_id, strategy, effort)
            for task_id in expected_tasks
            for strategy in METHOD_IDS
            for effort in ALLOWED_REASONING_EFFORTS
        }
        cells = item.get("cells")
        if not isinstance(cells, list):
            raise G2GateError(f"generation manifest {trial} has no cells")
        observed: set[tuple[str, str, str]] = set()
        for cell in cells:
            row = _object(cell, f"generation manifest {trial} cell")
            key = (
                str(row.get("task_id")),
                str(row.get("strategy_id")),
                str(row.get("reasoning_effort")),
            )
            if key in observed or row.get("status") != "completed":
                raise G2GateError(
                    f"generation manifest {trial} has duplicate or incomplete cells"
                )
            observed.add(key)
        if (
            observed != expected_cells
            or item.get("expected_cell_count") != len(expected_cells)
            or item.get("recorded_cell_count") != len(expected_cells)
            or item.get("status_counts")
            != {"completed": len(expected_cells), "failed": 0, "pending": 0}
        ):
            raise G2GateError(
                f"generation manifest {trial} does not exactly cover its frozen cell set"
            )
        validated.append(item)
    return validated


def _validate_answer_metrics(
    metrics: Mapping[str, Any],
    *,
    protocol_sha: str,
    lab: Mapping[str, Any],
    manifests: Sequence[Mapping[str, Any]],
) -> None:
    _hash_checked(
        metrics, "artifact_sha256", ANSWER_METRICS_SCHEMA, "public answer metrics"
    )
    first = manifests[0]
    if (
        metrics.get("scope") != "public_deterministic_screening"
        or metrics.get("generation_campaign_id") != first.get("generation_campaign_id")
        or metrics.get("generation_protocol_sha256") != protocol_sha
        or metrics.get("generation_manifest_sha256") != first.get("manifest_sha256")
        or metrics.get("component_lab_sha256") != lab.get("artifact_sha256")
        or metrics.get("output_token_limit") != first.get("output_token_limit")
    ):
        raise G2GateError("public answer metrics are not bound to the frozen G2 run")
    expected = int(lab["task_count"]) * len(METHOD_IDS) * len(ALLOWED_REASONING_EFFORTS)
    rows = metrics.get("rows")
    if (
        metrics.get("completed_cell_count") != expected
        or not isinstance(rows, list)
        or len(rows) != expected
    ):
        raise G2GateError("public answer metrics do not cover every G2 generation cell")
    cells = {str(cell.get("run_id")): cell for cell in first.get("cells", [])}
    trace_ids = {
        (str(trace["task"]["task_id"]), str(trace["strategy_id"])): str(trace["run_id"])
        for trace in lab["traces"]
    }
    seen: set[str] = set()
    for row in rows:
        item = _object(row, "public answer metric row")
        run_id = item.get("run_id")
        cell = cells.get(str(run_id))
        if (
            not isinstance(run_id, str)
            or run_id in seen
            or cell is None
            or cell.get("status") != "completed"
        ):
            raise G2GateError("public answer metric row has no completed manifest cell")
        if (
            item.get("task_id"),
            item.get("strategy_id"),
            item.get("reasoning_effort"),
        ) != (
            cell.get("task_id"),
            cell.get("strategy_id"),
            cell.get("reasoning_effort"),
        ):
            raise G2GateError(
                "public answer metric row identity differs from its manifest"
            )
        if trace_ids.get(
            (str(item.get("task_id")), str(item.get("strategy_id")))
        ) != cell.get("trace_run_id"):
            raise G2GateError(
                "public answer metric row is not bound to a component trace"
            )
        _sha(item.get("answer_sha256"), "public answer metric answer hash")
        seen.add(run_id)
    if seen != {
        run_id for run_id, cell in cells.items() if cell.get("status") == "completed"
    }:
        raise G2GateError(
            "public answer metric rows do not match completed manifest cells"
        )


def _validate_repeats(
    repeats: Mapping[str, Any],
    *,
    protocol_sha: str,
    lab_sha: str,
    manifests: Sequence[Mapping[str, Any]],
) -> None:
    _hash_checked(repeats, "analysis_sha256", REPEAT_ANALYSIS_SCHEMA, "repeat analysis")
    if (
        repeats.get("scope") != "public_temperature_zero_deterministic_screening"
        or repeats.get("protocol_sha256") != protocol_sha
        or repeats.get("component_lab_sha256") != lab_sha
        or repeats.get("trial_count") != REPEAT_TRIAL_COUNT
        or repeats.get("expected_cell_count_per_trial") != REPEAT_CELL_COUNT
    ):
        raise G2GateError("repeat analysis is not the frozen five-trial G2 analysis")
    if tuple(repeats.get("repeat_task_ids", ())) != _REPEAT_TASK_IDS:
        raise G2GateError("repeat analysis task IDs differ from the frozen protocol")
    listed = repeats.get("generation_manifests")
    expected = [
        {"trial": index, "manifest_sha256": manifest["manifest_sha256"]}
        for index, manifest in enumerate(manifests, start=1)
    ]
    if listed != expected:
        raise G2GateError(
            "repeat analysis manifest commitments differ from the five saved trials"
        )
    consistency = _object(repeats.get("aggregate_consistency"), "repeat consistency")
    if consistency.get("cell_count") != REPEAT_CELL_COUNT:
        raise G2GateError("repeat consistency does not cover the frozen sample")
    summary = _object(consistency.get("repeat_summary"), "repeat consistency summary")
    if (
        summary.get("trial_count") != REPEAT_TRIAL_COUNT
        or not isinstance(summary.get("trial_values"), list)
        or len(summary["trial_values"]) != REPEAT_TRIAL_COUNT
    ):
        raise G2GateError("repeat consistency is incomplete")
    cells = repeats.get("cells")
    expected_cells = {
        (task_id, strategy, effort)
        for task_id in _REPEAT_TASK_IDS
        for strategy in METHOD_IDS
        for effort in ALLOWED_REASONING_EFFORTS
    }
    if not isinstance(cells, list):
        raise G2GateError("repeat analysis has no per-cell evidence")
    observed: set[tuple[str, str, str]] = set()
    for row in cells:
        item = _object(row, "repeat cell")
        key = (
            str(item.get("task_id")),
            str(item.get("strategy_id")),
            str(item.get("reasoning_effort")),
        )
        if key in observed:
            raise G2GateError("repeat analysis duplicates a repeat cell")
        observed.add(key)
    if len(cells) != REPEAT_CELL_COUNT or observed != expected_cells:
        raise G2GateError(
            "repeat analysis cells do not exactly cover the frozen sample"
        )


def _g2_ledger_prefix(root: Path, path: Path, serialized: bytes) -> bytes:
    """Return the immutable G2 ledger prefix when its commitment is present."""

    freeze_path = root / G2_PAID_LEDGER_FREEZE_PATH
    if not freeze_path.is_file():
        return serialized
    try:
        freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G2GateError("cannot read the G2 paid-ledger freeze") from exc
    if not isinstance(freeze, Mapping):
        raise G2GateError("G2 paid-ledger freeze must be an object")
    _hash_checked(
        freeze,
        "artifact_sha256",
        G2_PAID_LEDGER_FREEZE_SCHEMA,
        "G2 paid-ledger freeze",
    )
    if set(freeze) != {
        "schema_version",
        "ledger_path",
        "event_count",
        "ledger_prefix_sha256",
        "artifact_sha256",
    } or freeze.get("ledger_path") != str(path.relative_to(root)):
        raise G2GateError("G2 paid-ledger freeze fields are invalid")
    event_count = freeze.get("event_count")
    if (
        isinstance(event_count, bool)
        or not isinstance(event_count, int)
        or event_count < 1
    ):
        raise G2GateError("G2 paid-ledger freeze event count is invalid")
    expected_sha = _sha(
        freeze.get("ledger_prefix_sha256"), "G2 paid-ledger prefix hash"
    )
    physical_lines = serialized.splitlines(keepends=True)
    if len(physical_lines) < event_count:
        raise G2GateError("canonical paid ledger is shorter than the G2 freeze")
    prefix = b"".join(physical_lines[:event_count])
    if hashlib.sha256(prefix).hexdigest() != expected_sha:
        raise G2GateError("canonical paid ledger differs inside the frozen G2 prefix")
    return prefix


def _audit_canonical_paid_ledger(root: Path) -> dict[str, Any]:
    """Replay the immutable G2 prefix of the authoritative append-only ledger."""
    root = root.resolve()
    path = canonical_ledger_path(root)
    try:
        with path.open("rb") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            serialized = handle.read()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise G2GateError("cannot read canonical paid ledger") from exc
    serialized = _g2_ledger_prefix(root, path, serialized)
    try:
        text = serialized.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise G2GateError("canonical paid ledger is not UTF-8") from exc
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise G2GateError("canonical paid ledger is empty")
    ledger_sha = hashlib.sha256(serialized).hexdigest()
    active: dict[str, Decimal] = {}
    seen: dict[str, set[str]] = {}
    _parsed_events: dict[str, list[dict[str, Any]]] = {}
    actual = Decimal("0")
    settled = 0
    for number, raw in enumerate(lines, start=1):
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise G2GateError(f"paid-ledger line {number} is not JSON") from exc
        if (
            not isinstance(event, Mapping)
            or event.get("schema_version") != "contextlab.cost-event.v1"
        ):
            raise G2GateError(f"paid-ledger line {number} has an invalid schema")
        kind = event.get("event")
        reservation_id = event.get("reservation_id")
        if (
            not isinstance(kind, str)
            or not isinstance(reservation_id, str)
            or not reservation_id
        ):
            raise G2GateError(f"paid-ledger line {number} has no lifecycle identity")
        history = seen.setdefault(reservation_id, set())
        metadata = event.get("metadata")
        if kind in {"acknowledge", "settle", "failure", "enrich"} and not isinstance(
            metadata, Mapping
        ):
            raise G2GateError("paid-ledger event metadata is malformed")
        if kind != "failure" and "failure" in history:
            raise G2GateError("paid-ledger failure is terminal")
        if kind == "reserve":
            if history:
                raise G2GateError("paid-ledger duplicates a reservation lifecycle")
            required = {
                "schema_version",
                "event",
                "reservation_id",
                "input_token_limit",
                "output_token_limit",
                "call_count",
                "estimated_usd",
            }
            if set(event) != required:
                raise G2GateError(
                    "paid-ledger reservation fields differ from the canonical schema"
                )
            input_tokens = event.get("input_token_limit")
            output_tokens = event.get("output_token_limit")
            if (
                isinstance(input_tokens, bool)
                or not isinstance(input_tokens, int)
                or input_tokens < 0
                or isinstance(output_tokens, bool)
                or not isinstance(output_tokens, int)
                or output_tokens < 0
            ):
                raise G2GateError("paid-ledger reservation token limits are invalid")
            estimate = _cost(event.get("estimated_usd"), "paid-ledger reservation")
            if (
                isinstance(event.get("call_count"), bool)
                or not isinstance(event.get("call_count"), int)
                or event["call_count"] < 1
            ):
                raise G2GateError("paid-ledger reservation call count is invalid")
            expected_estimate = estimate_cost(
                input_tokens, output_tokens, calls=event["call_count"]
            )
            if estimate != expected_estimate:
                raise G2GateError(
                    "paid-ledger reservation estimate differs from the canonical estimator"
                )
            active[reservation_id] = estimate
        elif kind == "acknowledge":
            if reservation_id not in active or "acknowledge" in history:
                raise G2GateError("paid-ledger acknowledgment lifecycle is invalid")
            if (
                not isinstance(metadata.get("request_id"), str)
                or not metadata["request_id"]
            ):
                raise G2GateError("paid-ledger acknowledgment lacks a request ID")
        elif kind == "failure":
            if (
                not history
                or "failure" in history
                or "cancel" in history
                or "enrich" in history
            ):
                raise G2GateError("paid-ledger failure lifecycle is invalid")
        elif kind == "settle":
            if (
                reservation_id not in active
                or "settle" in history
                or "cancel" in history
            ):
                raise G2GateError("paid-ledger settlement lifecycle is invalid")
            # Legacy key-usage reconciliation is the sole allowed settlement without
            # an acknowledgment. All gateway-originated settlements need one.
            reconciled = (
                isinstance(metadata, Mapping)
                and metadata.get("cost_source") == "openrouter_key_usage_reconciliation"
            )
            if "acknowledge" not in history and not reconciled:
                raise G2GateError(
                    "paid-ledger settlement lacks provider acknowledgment"
                )
            acknowledgments = [
                row
                for row in _parsed_events.get(reservation_id, [])
                if row.get("event") == "acknowledge"
            ]
            if not reconciled and (
                len(acknowledgments) != 1
                or acknowledgments[0]["metadata"].get("request_id")
                != metadata.get("request_id")
            ):
                raise G2GateError(
                    "paid-ledger settlement request ID differs from its acknowledgment"
                )
            amount = _cost(event.get("actual_usd"), "paid-ledger settlement")
            if amount > active[reservation_id]:
                raise G2GateError("paid-ledger settlement exceeds its reservation")
            actual += amount
            active.pop(reservation_id)
            settled += 1
        elif kind == "cancel":
            if reservation_id not in active or history != {"reserve"}:
                raise G2GateError("paid-ledger cancellation lifecycle is invalid")
            active.pop(reservation_id)
        elif kind == "enrich":
            if "settle" not in history or "acknowledge" not in history:
                raise G2GateError("paid-ledger enrichment lifecycle is invalid")
            settlements = [
                row
                for row in _parsed_events.get(reservation_id, [])
                if row.get("event") == "settle"
            ]
            acknowledgments = [
                row
                for row in _parsed_events.get(reservation_id, [])
                if row.get("event") == "acknowledge"
            ]
            if (
                len(settlements) != 1
                or len(acknowledgments) != 1
                or metadata.get("request_id")
                != acknowledgments[0]["metadata"].get("request_id")
                or _cost(metadata.get("actual_usd"), "paid-ledger enrichment")
                != _cost(settlements[0].get("actual_usd"), "paid-ledger settlement")
            ):
                raise G2GateError(
                    "paid-ledger enrichment differs from its settled request"
                )
            if "enrich" in history:
                raise G2GateError(
                    "paid-ledger duplicate enrichment must not be appended"
                )
        else:
            raise G2GateError("paid-ledger event is unknown")
        _parsed_events.setdefault(reservation_id, []).append(dict(event))
        history.add(kind)
    active_reserved = sum(active.values(), Decimal("0"))
    exposure = actual + active_reserved
    if exposure >= HARD_CAP_USD:
        raise G2GateError("paid billed and reserved exposure is not below US$15")
    return {
        "ledger_path": str(path.relative_to(root)),
        "ledger_sha256": ledger_sha,
        "event_count": len(lines),
        "settled_call_count": settled,
        "active_reservation_count": len(active),
        "actual_usd": str(actual),
        "active_reserved_usd": str(active_reserved),
        "total_billed_and_reserved_usd": str(exposure),
    }


def _sealed_stage(
    sealed: Mapping[str, Any],
    *,
    candidate: str,
    parent: str,
    metric: str,
    promotion: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    records = sealed["component_records"]
    index = {(row["task_id"], row["strategy_id"]): row for row in records}
    baseline = {
        task: float(index[(task, parent)]["metrics"][metric])
        for task in G2_SEALED_TASK_IDS
    }
    candidate_scores = {
        task: float(index[(task, candidate)]["metrics"][metric])
        for task in G2_SEALED_TASK_IDS
    }
    try:
        ci = paired_bootstrap_ci(
            baseline,
            candidate_scores,
            seed=int.from_bytes(f"g2-sealed:{candidate}".encode(), "little") % (2**63),
            resamples=int(promotion["bootstrap_resamples"]),
        )
        latency = distribution_summary(
            float(index[(task, candidate)]["metrics"]["retrieval_latency_ms"])
            for task in G2_SEALED_TASK_IDS
        )
    except Exception as exc:
        raise G2GateError(f"cannot calculate sealed paired evidence: {exc}") from exc
    zero_cost = all(
        _cost(
            index[(task, candidate)]["metrics"]["retrieval_cost_usd"],
            "sealed retrieval cost",
        )
        == 0
        for task in G2_SEALED_TASK_IDS
    )
    summary = sealed.get("generation_summary")
    complete_generation = (
        isinstance(summary, Mapping)
        and summary.get("generation_count") == 576
        and summary.get("status_counts")
        == {"completed": 576, "failed": 0, "pending": 0}
    )
    regression = _number(
        promotion.get("full_set_material_regression"),
        "full-set regression floor",
        minimum=-1.0,
    )
    latency_limit = _number(
        promotion.get("retrieval_p95_latency_ceiling_ms"), "retrieval latency ceiling"
    )
    # The safe sealed import intentionally contains only task IDs and aggregate
    # component metrics. It does not contain a content-free target-family
    # assignment or a target-family aggregate. A full-set comparison cannot
    # satisfy the preregistered target-family minimum in its place.
    target_family = {
        "status": "unavailable",
        "minimum_delta": _number(
            promotion.get("minimum_target_family_delta"),
            "minimum target-family delta",
        ),
        "met": False,
        "reason": "safe sealed import has no content-free target-family aggregate",
    }
    criteria = {
        "target_family_minimum_met": False,
        "full_set_not_materially_regressed": float(ci["mean_delta"]) >= regression,
        "paired_ci_supports_direction": float(ci["ci_lower"]) >= 0.0,
        "latency_within_budget": float(latency["p95"]) <= latency_limit,
        "retrieval_cost_is_zero": zero_cost,
        "trace_commitments_complete": len(index) == G2_COMPONENT_CELL_COUNT,
        "sealed_generation_complete": complete_generation,
    }
    return {
        "candidate": candidate,
        "parent": parent,
        "primary_metric": metric,
        "target_family_aggregate": target_family,
        "paired_bootstrap": ci,
        "latency_ms": latency,
        "criteria": criteria,
    }, all(criteria.values())


def _promotion_eligibility(
    incremental_candidates: Sequence[str], methods: Mapping[str, Any]
) -> tuple[list[str], dict[str, list[str]]]:
    """Return candidates whose complete experimental ancestry publicly passed."""
    eligible: list[str] = []
    blockers: dict[str, list[str]] = {}
    for candidate in incremental_candidates:
        failed_ancestors: list[str] = []
        ancestor = PARENT_METHOD[candidate]
        while ancestor != "R0":
            status = _object(methods.get(ancestor), f"component method {ancestor}").get(
                "status"
            )
            if status != "public_passed":
                failed_ancestors.append(ancestor)
            ancestor = PARENT_METHOD[ancestor]
        if failed_ancestors:
            blockers[candidate] = sorted(
                failed_ancestors, key=lambda value: int(value[1:])
            )
        else:
            eligible.append(candidate)
    return eligible, blockers


def _select_furthest_passing(
    candidates: Sequence[str], *, promotion_eligible_candidates: Sequence[str]
) -> str | None:
    """Use the preregistered retrieval ladder: highest R-stage wins deterministically."""
    if len(set(candidates)) != len(candidates) or any(
        candidate not in METHOD_IDS[1:] for candidate in candidates
    ):
        raise G2GateError("sealed passing-candidate set is invalid")
    eligible = set(promotion_eligible_candidates)
    if len(eligible) != len(promotion_eligible_candidates) or not eligible <= set(
        METHOD_IDS[1:]
    ):
        raise G2GateError("promotion-eligible candidate set is invalid")
    if not set(candidates) <= eligible:
        raise G2GateError("cannot select an ineligible sealed candidate")
    if not candidates:
        return None
    return max(candidates, key=lambda method_id: int(method_id[1:]))


def _approval(approval: Mapping[str, Any] | None, technical_sha: str) -> dict[str, Any]:
    if approval is None:
        return {
            "status": "pending",
            "reviewer": "Kevin Araujo",
            "reason": "separate human approval record is required",
        }
    value = _object(approval, "human approval record")
    required = {
        "schema_version",
        "gate_sha256",
        "reviewer",
        "reviewer_role",
        "decision",
        "approved_at",
    }
    if (
        set(value) != required
        or value.get("schema_version") != G2_HUMAN_APPROVAL_SCHEMA
    ):
        raise G2GateError("human approval record schema is invalid")
    approved_at = value.get("approved_at")
    try:
        valid_utc = (
            isinstance(approved_at, str)
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", approved_at)
            is not None
            and bool(dt.datetime.strptime(approved_at, "%Y-%m-%dT%H:%M:%SZ"))
        )
    except ValueError:
        valid_utc = False
    if (
        value.get("gate_sha256") != technical_sha
        or value.get("reviewer") != "Kevin Araujo"
        or value.get("reviewer_role") != "human_reviewer"
        or value.get("decision") != "approved"
        or not valid_utc
    ):
        raise G2GateError(
            "human approval record is forged, stale, or not Kevin's approval"
        )
    return {
        "status": "approved",
        "reviewer": "Kevin Araujo",
        "approval_record": dict(value),
    }


def build_g2_final_gate(
    *,
    protocol: Mapping[str, Any],
    static_freeze: Mapping[str, Any],
    component_lab: Mapping[str, Any],
    component_analysis: Mapping[str, Any],
    public_answer_metrics: Mapping[str, Any],
    repeat_analysis: Mapping[str, Any],
    generation_manifests: Sequence[Mapping[str, Any]],
    sealed_import: Mapping[str, Any],
    public_tasks: Sequence[Mapping[str, Any]],
    root: Path,
    human_approval: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a deterministic G2 decision without reading any protected content.

    A technically successful record is only *ready* for promotion.  ``promote``
    is emitted only after an independent Kevin approval record binds to the
    technical-record hash.
    """
    root = root.resolve()
    protocol = _object(protocol, "retrieval protocol")
    static_freeze = _object(static_freeze, "static freeze")
    lab = _object(component_lab, "public component lab")
    component_analysis = _object(component_analysis, "public component analysis")
    public_answer_metrics = _object(public_answer_metrics, "public answer metrics")
    repeat_analysis = _object(repeat_analysis, "repeat analysis")
    sealed_import = _object(sealed_import, "sealed import")
    protocol_sha, _fixed, promotion = _protocol_contract(protocol)
    freeze_sha, external_sha, canonical_freeze = _static_commitments(
        static_freeze, root
    )
    canonical_public_tasks = _canonical_public_grid(
        public_tasks, canonical_freeze, root
    )
    try:
        validate_lab(lab)
    except Exception as exc:
        raise G2GateError(f"public component lab is invalid: {exc}") from exc
    if lab.get("protocol_sha256") != protocol_sha:
        raise G2GateError("component lab protocol commitment is stale")
    _validate_lab_public_grid(lab, canonical_public_tasks)
    lab_sha = _sha(lab.get("artifact_sha256"), "component lab hash")
    incremental_candidates = _validate_component_analysis(
        component_analysis,
        protocol_sha=protocol_sha,
        lab_sha=lab_sha,
        promotion=promotion,
        lab=lab,
        protocol=protocol,
        public_tasks=list(canonical_public_tasks.values()),
    )
    promotion_eligible_candidates, failed_ancestor_blockers = _promotion_eligibility(
        incremental_candidates,
        _object(component_analysis.get("methods"), "component methods"),
    )
    manifests = _validate_manifests(generation_manifests, lab=lab, protocol=protocol)
    _validate_answer_metrics(
        public_answer_metrics, protocol_sha=protocol_sha, lab=lab, manifests=manifests
    )
    _validate_repeats(
        repeat_analysis, protocol_sha=protocol_sha, lab_sha=lab_sha, manifests=manifests
    )
    sealed = _validate_sealed_import(
        sealed_import,
        freeze_sha=freeze_sha,
        external_sha=external_sha,
        protocol_sha=protocol_sha,
    )
    ledger_audit = _audit_canonical_paid_ledger(root)

    stages: dict[str, dict[str, Any]] = {
        "public_component": {
            "decision": "retain-simple"
            if not promotion_eligible_candidates
            else "promote",
            "incremental_candidates": incremental_candidates,
            "promotion_eligible_candidates": promotion_eligible_candidates,
            "failed_ancestor_blockers": failed_ancestor_blockers,
        },
        "public_generation": {
            "decision": "promote",
            "trace_path_bound": True,
            "completed_cell_count": public_answer_metrics["completed_cell_count"],
        },
        "repeat_evidence": {
            "decision": "promote",
            "trial_count": REPEAT_TRIAL_COUNT,
            "repeat_cell_count": REPEAT_CELL_COUNT,
        },
    }
    sealed_rows: list[dict[str, Any]] = []
    for candidate in incremental_candidates:
        method = component_analysis["methods"][candidate]
        row, passed = _sealed_stage(
            sealed,
            candidate=candidate,
            parent=str(method["parent"]),
            metric=str(method["primary_metric"]),
            promotion=promotion,
        )
        row["promotion_eligible"] = candidate in promotion_eligible_candidates
        row["failed_ancestor_blockers"] = failed_ancestor_blockers.get(candidate, [])
        row["decision"] = (
            "ineligible"
            if not row["promotion_eligible"]
            else "promote"
            if passed
            else "revise"
        )
        sealed_rows.append(row)
    stages["sealed_evaluation"] = {
        "decision": "not-applicable"
        if not incremental_candidates
        else (
            "promote"
            if any(row["decision"] == "promote" for row in sealed_rows)
            else "retain-simple"
        ),
        "incremental_candidates": sealed_rows,
        "promotion_eligible_candidates": promotion_eligible_candidates,
        "failed_ancestor_blockers": failed_ancestor_blockers,
    }
    passing = [row["candidate"] for row in sealed_rows if row["decision"] == "promote"]
    selected = _select_furthest_passing(
        passing, promotion_eligible_candidates=promotion_eligible_candidates
    )
    technical_decision = "promote" if selected else "retain-simple"
    limitations = [
        "Public answer screening is not a semantic correctness grade.",
        "Sealed evidence contains only aggregate retrieval metrics and commitments; it cannot expose task content.",
        "Exact answer hashes in repeat evidence measure reproducibility, not semantic equivalence.",
        "Technical evidence cannot substitute for Kevin's independent human review.",
    ]
    if failed_ancestor_blockers:
        limitations.append(
            "Failed experimental ancestors block incremental promotion; the affected candidates remain audit evidence only."
        )
    if any(
        row["target_family_aggregate"]["status"] == "unavailable" for row in sealed_rows
    ):
        limitations.append(
            "The safe sealed import has no content-free target-family aggregate, so it cannot meet the preregistered sealed target-family minimum."
        )
    failure_methods = (
        list(METHOD_IDS[1:])
        if technical_decision == "retain-simple"
        else [
            method
            for method in METHOD_IDS[1:]
            if component_analysis["methods"][method].get("status") != "public_passed"
        ]
    )
    technical: dict[str, Any] = {
        "schema_version": G2_GATE_SCHEMA,
        "scope": "content_free_g2_final_gate",
        "protocol_sha256": protocol_sha,
        "static_freeze_manifest_sha256": freeze_sha,
        "external_bundle_sha256": external_sha,
        "component_lab_sha256": lab_sha,
        "generation_campaign_id": "g2r2",
        "requested_model": MODEL_ID,
        "output_token_limit": 8192,
        "generation_manifest_sha256s": [
            manifest["manifest_sha256"] for manifest in manifests
        ],
        "sealed_source_return_sha256": sealed["source_return_sha256"],
        "paid_ledger_audit": ledger_audit,
        "promoted_retriever_id": selected,
        "retained_retriever_id": "R0" if selected is None else None,
        "promoted_retriever_protocol_sha256": protocol_sha
        if selected is not None
        else None,
        "promoted_retriever_config_sha256": (
            sha256_json(
                {"strategy_id": selected, "method": protocol["methods"][selected]}
            )
            if selected is not None
            else None
        ),
        "stages": stages,
        "technical_decision": technical_decision,
        "technical_promotion_ready": selected is not None,
        "failure_trace_review_references": {
            "public_trace_viewer": "results/v2/traces/g2_retrieval_trace_viewer.html",
            "public_component_analysis": "results/v2/reports/g2_public_component_analysis.json",
            "failure_trace_review": "results/v2/reports/g2_failure_trace_review.json",
            "methods_requiring_review": failure_methods,
        },
        "limitations": limitations,
    }
    technical_sha = sha256_json(technical)
    approval = _approval(human_approval, technical_sha)
    final_decision = (
        "blocked"
        if approval["status"] != "approved"
        else "promote"
        if technical_decision == "promote"
        else technical_decision
    )
    result = {
        **technical,
        "technical_record_sha256": technical_sha,
        "human_approval": approval,
        "final_decision": final_decision,
    }
    result["artifact_sha256"] = sha256_json(result)
    return result
