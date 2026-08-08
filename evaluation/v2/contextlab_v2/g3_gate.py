"""Strict, provider-free final acceptance gate for ContextLab G3.

The public scorer deliberately emits only a public-screen hint.  This module
never treats that hint as a gate decision.  It revalidates the canonical G3
freeze, the complete public factorial, the content-free external return, the
canonical replay/lifecycle artifact, and the three-member calibration report.
Every accepted input is bound into the final record by its exact artifact hash.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path
import re
from typing import Any, Callable

from .baseline import repository_root
from .credentials import redact
from .g3_execution import validate_prepared_public_g3_cell
from .g3_freeze import load_memory_protocol, validate_g3_freeze
from .g3_lifecycle import (
    G3_LIFECYCLE_PATH,
    validate_g3_lifecycle_evidence,
)
from .g3_panel import (
    AI_REVIEWERS,
    G3_PANEL_CALIBRATION_SCHEMA,
    SOLE_HUMAN_REVIEWER,
    validate_g3_panel_calibration,
)
from .g3_sealed import (
    G3_SEALED_IMPORT_SCHEMA,
    G3_SEALED_RETURN_SCHEMA,
    build_g3_sealed_metrics,
    validate_g3_sealed_candidate_manifest,
    validate_g3_sealed_return,
)
from .g3_static_grading import (
    validate_public_static_grade,
    validate_public_static_grade_evidence,
)
from .generations import validate_saved_generation_result
from .gates import GateError, load_approved_g1_gate
from .memory_experiments import (
    MEMORY_CONFIGURATIONS,
    PUBLIC_TASK_COUNT,
    validate_memory_result_receipt,
)
from .memory_metrics import MEMORY_METRICS_SCHEMA, score_memory_outcomes
from .memory_review import (
    validate_kevin_unsupported_memory_review_approval,
    validate_unsupported_memory_review,
)
from .provider import ALLOWED_REASONING_EFFORTS
from .review import (
    AI_KEVIN_ACCEPTED_MATCH_MIN,
    AI_KEVIN_MEAN_ABSOLUTE_DIFFERENCE_MAX,
    CALIBRATION_ACCEPTED_MATCH_MIN,
    CALIBRATION_EXACT_ORDINAL_MIN,
    CALIBRATION_WITHIN_ONE_MIN,
    REVIEWERS,
)
from .review_invocations import native_review_paths
from .tasking import sha256_json


G3_GATE_SCHEMA = "contextlab.g3-final-gate.v2"
G3_FAILURE_REPORT_SCHEMA = "contextlab.g3-failure-and-harm-report.v1"
G3_KEVIN_DECISION_SCHEMA = "contextlab.g3-kevin-decision.v1"
G3_AI_GATE_REVIEW_SCHEMA = "contextlab.g3-ai-gate-review.v1"
G3_AI_GATE_INVOCATION_RECEIPT_SCHEMA = "contextlab.g3-ai-gate-invocation-receipt.v2"
G1_GATE_PATH = Path("results/v2/gates/G1.json")
G3_PENDING_GATE_PATH = Path("results/v2/gates/G3.pending.json")
G3_REVIEWED_GATE_PATH = Path("results/v2/gates/G3.reviewed.json")
G3_FINAL_GATE_PATH = Path("results/v2/gates/G3.json")
G3_KEVIN_DECISION_PATH = Path("results/v2/reviews/g3/kevin/final-gate-decision.json")
G3_FREEZE_PATH = Path("results/v2/memory/g3_public_freeze.json")
G3_PUBLIC_RUN_PATH = Path("results/v2/memory/g3_public_generation_run.json")
G3_AI_GATE_REVIEW_PATHS = {
    "gpt-5.6-sol-high": Path(
        "results/v2/reviews/g3/gpt-5.6-sol-high/final-gate-review.json"
    ),
    "claude-opus-5-medium": Path(
        "results/v2/reviews/g3/claude-opus-5-medium/final-gate-review.json"
    ),
}
G3_AI_GATE_INVOCATION_RECEIPT_PATHS = {
    reviewer: path.with_name("invocation-receipt.json")
    for reviewer, path in G3_AI_GATE_REVIEW_PATHS.items()
}

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_UTC_SECOND = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_INVOCATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{7,255}\Z")
_EFFORT_ORDER = {
    effort: index for index, effort in enumerate(ALLOWED_REASONING_EFFORTS)
}
_CONFIGURATION_KEYS = tuple(
    f"{policy}:{effort}"
    for policy in MEMORY_CONFIGURATIONS
    for effort in ALLOWED_REASONING_EFFORTS
)
_G3_AI_GATE_REVIEWERS = {
    "gpt-5.6-sol-high": {
        "reviewer_name": "GPT-5.6 Sol",
        "model_id": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "invocation": "Codex subagent",
        "invocation_source": "codex-subagent",
    },
    "claude-opus-5-medium": {
        "reviewer_name": "Claude Opus 5",
        "model_id": "claude-opus-5",
        "reasoning_effort": "medium",
        "invocation": "local Claude CLI",
        "invocation_source": "claude-cli",
    },
}


class G3GateError(ValueError):
    """G3 evidence is missing, altered, incomplete, or not promotion-safe."""


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise G3GateError(f"{label} must be an object")
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise G3GateError(f"{label} must be a lowercase SHA-256")
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise G3GateError(f"{label} must be an integer of at least {minimum}")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise G3GateError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise G3GateError(f"{label} must be finite")
    return result


def _decimal(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise G3GateError(f"{label} must be a finite decimal") from exc
    if not result.is_finite() or result < 0:
        raise G3GateError(f"{label} must be a non-negative finite decimal")
    return result


def _descriptive_panel_consensus(panel: Mapping[str, Any]) -> bool:
    """Allow a non-promotional demo when the panel agrees but the reference does not."""

    if panel.get("status") != "restart_required":
        return False
    ambiguity = panel.get("rubric_ambiguity_by_reviewer")
    ai_vs_kevin = panel.get("ai_vs_kevin")
    repeats = panel.get("hidden_repeat_consistency_by_reviewer")
    if (
        not isinstance(ambiguity, Mapping)
        or set(ambiguity) != set(REVIEWERS)
        or any(value is not False for value in ambiguity.values())
        or not isinstance(ai_vs_kevin, Mapping)
        or set(ai_vs_kevin) != set(AI_REVIEWERS)
        or not isinstance(repeats, Mapping)
        or set(repeats) != set(REVIEWERS)
    ):
        return False
    try:
        ai_kevin_pass = all(
            _number(values["accepted_match_rate"], "AI-Kevin accepted match")
            >= AI_KEVIN_ACCEPTED_MATCH_MIN
            and _number(
                values["mean_absolute_ordinal_difference"],
                "AI-Kevin mean ordinal difference",
            )
            <= AI_KEVIN_MEAN_ABSOLUTE_DIFFERENCE_MAX
            for values in ai_vs_kevin.values()
        )
        repeat_pass = all(
            _integer(values["pairs"], "hidden repeat pairs", minimum=1) >= 1
            and _number(values["exact_ordinal_rate"], "hidden repeat exact rate")
            >= CALIBRATION_EXACT_ORDINAL_MIN
            and _number(
                values["within_one_ordinal_rate"],
                "hidden repeat within-one rate",
            )
            >= CALIBRATION_WITHIN_ONE_MIN
            and _number(
                values["accepted_match_rate"],
                "hidden repeat accepted match",
            )
            >= CALIBRATION_ACCEPTED_MATCH_MIN
            for values in repeats.values()
        )
    except (G3GateError, KeyError, TypeError):
        return False
    return ai_kevin_pass and repeat_pass


def _hash_checked(
    value: Mapping[str, Any], *, schema: str, hash_field: str, label: str
) -> str:
    if value.get("schema_version") != schema:
        raise G3GateError(f"unsupported {label} schema")
    body = {key: item for key, item in value.items() if key != hash_field}
    expected = sha256_json(body)
    if value.get(hash_field) != expected:
        raise G3GateError(f"{label} hash mismatch")
    return expected


def _read_canonical(
    supplied: Mapping[str, Any],
    *,
    root: Path,
    relative_path: Path,
    label: str,
    validator: Callable[[Mapping[str, Any]], None],
) -> dict[str, Any]:
    path = root / relative_path
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G3GateError(f"cannot read canonical {label}") from exc
    if not isinstance(value, dict):
        raise G3GateError(f"canonical {label} must be an object")
    try:
        validator(value)
    except Exception as exc:
        raise G3GateError(f"canonical {label} is invalid: {exc}") from exc
    if dict(supplied) != value:
        raise G3GateError(f"supplied {label} differs from its canonical artifact")
    return value


def _safe_relative_path(value: Any, label: str, prefix: Path) -> str:
    if not isinstance(value, str) or not value:
        raise G3GateError(f"{label} must be a non-empty repository-relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise G3GateError(f"{label} escapes the repository")
    try:
        path.relative_to(prefix)
    except ValueError as exc:
        raise G3GateError(f"{label} is outside {prefix}") from exc
    return value


def _canonical_freeze(
    supplied: Mapping[str, Any], root: Path
) -> tuple[dict[str, Any], dict[str, Any], Mapping[str, Any]]:
    freeze = _read_canonical(
        supplied,
        root=root,
        relative_path=G3_FREEZE_PATH,
        label="G3 freeze",
        validator=validate_g3_freeze,
    )
    try:
        protocol = load_memory_protocol(root)
    except Exception as exc:
        raise G3GateError(f"canonical G3 memory protocol is invalid: {exc}") from exc
    if freeze["memory_protocol_sha256"] != sha256_json(protocol):
        raise G3GateError("canonical G3 freeze is not bound to the memory protocol")
    acceptance = _object(protocol.get("acceptance"), "G3 acceptance protocol")
    manifest_acceptance = _object(
        freeze["manifest"].get("acceptance_parameters"),
        "frozen G3 acceptance parameters",
    )
    shared = (
        "primary_metric",
        "provenance_minimum",
        "static_accuracy_regression_floor",
        "paired_bootstrap_resamples",
        "paired_bootstrap_seed_name",
        "paired_bootstrap_seed",
    )
    if any(acceptance.get(key) != manifest_acceptance.get(key) for key in shared):
        raise G3GateError("G3 protocol and frozen acceptance parameters disagree")
    if (
        acceptance.get("temporal_improvement_rule") != "mean_delta_vs_m0_gt_0"
        or acceptance.get("failed_results_allowed_for_promotion") != 0
        or _number(acceptance.get("provenance_minimum"), "provenance minimum") != 0.95
    ):
        raise G3GateError("G3 acceptance rules are not the preregistered strict rules")
    return freeze, protocol, acceptance


def _validate_public_run(
    value: Mapping[str, Any], *, freeze: Mapping[str, Any]
) -> dict[str, Mapping[str, Any]]:
    expected_fields = {
        "schema_version",
        "g3_freeze_sha256",
        "frozen_manifest_sha256",
        "selected_task_ids",
        "expected_full_cell_count",
        "recorded_cell_count",
        "new_call_count",
        "generation_status_counts",
        "grade_status_counts",
        "completed_generation_cost_usd",
        "cells",
        "artifact_sha256",
    }
    if set(value) != expected_fields:
        raise G3GateError("public G3 run fields differ from the frozen run schema")
    _hash_checked(
        value,
        schema="contextlab.g3-public-generation-run.v1",
        hash_field="artifact_sha256",
        label="public G3 run",
    )
    manifest = _object(freeze.get("manifest"), "frozen G3 manifest")
    specs = manifest.get("run_specs")
    if not isinstance(specs, list):
        raise G3GateError("frozen G3 run specs must be a list")
    expected_count = (
        len(MEMORY_CONFIGURATIONS) * len(ALLOWED_REASONING_EFFORTS) * PUBLIC_TASK_COUNT
    )
    if len(specs) != expected_count:
        raise G3GateError(
            "frozen G3 manifest does not contain the full 1,120-cell factorial"
        )
    by_run_id = {
        str(spec.get("run_id")): spec
        for spec in specs
        if isinstance(spec, Mapping) and isinstance(spec.get("run_id"), str)
    }
    if len(by_run_id) != expected_count:
        raise G3GateError("frozen G3 run IDs are incomplete or duplicated")
    if (
        value.get("g3_freeze_sha256") != freeze.get("artifact_sha256")
        or value.get("frozen_manifest_sha256") != manifest.get("frozen_manifest_sha256")
        or value.get("selected_task_ids") != []
        or value.get("expected_full_cell_count") != expected_count
        or value.get("recorded_cell_count") != expected_count
    ):
        raise G3GateError("public G3 run is partial or bound to a different freeze")
    _integer(value.get("new_call_count"), "public G3 new-call count")
    if int(value["new_call_count"]) > expected_count:
        raise G3GateError("public G3 new-call count exceeds the frozen grid")
    _decimal(value.get("completed_generation_cost_usd"), "public G3 generation cost")
    cells = value.get("cells")
    if not isinstance(cells, list) or len(cells) != expected_count:
        raise G3GateError("public G3 run must record every frozen cell exactly once")
    cell_fields = {
        "run_id",
        "task_id",
        "suite",
        "policy",
        "reasoning_effort",
        "prepared_path",
        "prepared_cell_sha256",
        "generation_path",
        "generation_status",
        "generation_artifact_sha256",
        "generation_result_sha256",
        "grade_status",
        "receipt_path",
        "receipt_sha256",
        "static_grade_evidence_path",
        "static_grade_evidence_sha256",
    }
    indexed: dict[str, Mapping[str, Any]] = {}
    generation_counts: Counter[str] = Counter()
    grade_counts: Counter[str] = Counter()
    for cell in cells:
        if not isinstance(cell, Mapping) or set(cell) != cell_fields:
            raise G3GateError("public G3 run cell fields are invalid")
        run_id = cell.get("run_id")
        spec = by_run_id.get(str(run_id))
        task = spec.get("task") if isinstance(spec, Mapping) else None
        if (
            spec is None
            or run_id in indexed
            or not isinstance(task, Mapping)
            or (
                cell.get("task_id"),
                cell.get("suite"),
                cell.get("policy"),
                cell.get("reasoning_effort"),
            )
            != (
                task.get("task_id"),
                task.get("suite"),
                spec.get("policy"),
                spec.get("reasoning_effort"),
            )
        ):
            raise G3GateError("public G3 run cell is duplicate or outside the freeze")
        _safe_relative_path(
            cell.get("prepared_path"),
            f"{run_id} prepared path",
            Path("results/v2/memory/prepared"),
        )
        _safe_relative_path(
            cell.get("generation_path"),
            f"{run_id} generation path",
            Path("results/v2/generations/public"),
        )
        campaign = str(manifest.get("campaign_id"))
        policy = str(spec.get("policy"))
        effort = str(spec.get("reasoning_effort"))
        task_id = str(task.get("task_id"))
        expected_paths = {
            "prepared_path": (
                Path("results/v2/memory/prepared")
                / campaign
                / policy
                / effort
                / f"{task_id}.json"
            ).as_posix(),
            "generation_path": (
                Path("results/v2/generations/public")
                / campaign
                / policy
                / effort
                / f"{task_id}.json"
            ).as_posix(),
            "receipt_path": (
                Path("results/v2/memory/receipts")
                / campaign
                / policy
                / effort
                / f"{task_id}.json"
            ).as_posix(),
            "static_grade_evidence_path": (
                Path("results/v2/memory/grades")
                / campaign
                / policy
                / effort
                / f"{task_id}.json"
            ).as_posix(),
        }
        if any(
            cell.get(field) != expected_paths[field]
            for field in ("prepared_path", "generation_path", "receipt_path")
        ):
            raise G3GateError("public G3 cell artifact path is not canonical")
        _sha(cell.get("prepared_cell_sha256"), f"{run_id} prepared-cell hash")
        generation_status = cell.get("generation_status")
        if generation_status not in {"completed", "failed"}:
            raise G3GateError("public G3 run contains a missing generation")
        _sha(
            cell.get("generation_artifact_sha256"),
            f"{run_id} generation-artifact hash",
        )
        if generation_status == "completed":
            _sha(
                cell.get("generation_result_sha256"),
                f"{run_id} generation-result hash",
            )
            if cell.get("generation_result_sha256") != cell.get(
                "generation_artifact_sha256"
            ):
                raise G3GateError(
                    "completed public G3 generation hashes must be identical"
                )
            if cell.get("grade_status") != "objective_completed":
                raise G3GateError("completed public G3 cell is not objectively graded")
        elif (
            cell.get("generation_result_sha256") is not None
            or cell.get("grade_status") != "failed"
        ):
            raise G3GateError("failed public G3 generation fabricates a result")
        _safe_relative_path(
            cell.get("receipt_path"),
            f"{run_id} receipt path",
            Path("results/v2/memory/receipts"),
        )
        _sha(cell.get("receipt_sha256"), f"{run_id} receipt hash")
        static_path = cell.get("static_grade_evidence_path")
        static_sha = cell.get("static_grade_evidence_sha256")
        completed_static = (
            generation_status == "completed" and task.get("suite") == "static"
        )
        if completed_static:
            if static_path != expected_paths["static_grade_evidence_path"]:
                raise G3GateError("public static grade-evidence path is not canonical")
            _safe_relative_path(
                static_path,
                f"{run_id} static grade-evidence path",
                Path("results/v2/memory/grades"),
            )
            _sha(static_sha, f"{run_id} static grade-evidence hash")
        elif static_path is not None or static_sha is not None:
            raise G3GateError(
                "temporal and failed G3 cells cannot carry static grade evidence"
            )
        indexed[str(run_id)] = cell
        generation_counts[str(generation_status)] += 1
        grade_counts[str(cell.get("grade_status"))] += 1
    if set(indexed) != set(by_run_id):
        raise G3GateError("public G3 run does not cover every frozen run ID")
    expected_generation_counts = {
        "completed": generation_counts["completed"],
        "failed": generation_counts["failed"],
        "missing": 0,
    }
    expected_grade_counts = {
        "objective_completed": grade_counts["objective_completed"],
        "panel_pending": 0,
        "failed": grade_counts["failed"],
        "generation_pending": 0,
    }
    if value.get("generation_status_counts") != expected_generation_counts:
        raise G3GateError("public G3 generation status counts are not derived")
    if value.get("grade_status_counts") != expected_grade_counts:
        raise G3GateError("public G3 grade status counts are not derived")
    return indexed


def _canonical_public_run(
    supplied: Mapping[str, Any], *, root: Path, freeze: Mapping[str, Any]
) -> dict[str, Mapping[str, Any]]:
    canonical = _read_canonical(
        supplied,
        root=root,
        relative_path=G3_PUBLIC_RUN_PATH,
        label="public G3 run",
        validator=lambda value: _validate_public_run(value, freeze=freeze),
    )
    return _validate_public_run(canonical, freeze=freeze)


def _load_cell_artifact(root: Path, path_value: Any, label: str) -> dict[str, Any]:
    """Load one already-contained canonical run artifact as strict JSON."""

    if not isinstance(path_value, str) or not path_value:
        raise G3GateError(f"{label} path is missing")
    relative = Path(path_value)
    if relative.is_absolute() or ".." in relative.parts:
        raise G3GateError(f"{label} path escapes the repository")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise G3GateError(f"{label} path resolves outside the repository") from exc
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G3GateError(f"cannot read canonical {label}") from exc
    if not isinstance(value, dict):
        raise G3GateError(f"canonical {label} must be an object")
    return value


def _public_receipts_and_metrics(
    receipts: Sequence[Mapping[str, Any]],
    metrics: Mapping[str, Any],
    *,
    freeze: Mapping[str, Any],
    run_cells: Mapping[str, Mapping[str, Any]],
    root: Path,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    if isinstance(receipts, (str, bytes)) or not isinstance(receipts, Sequence):
        raise G3GateError("public G3 receipts must be a sequence")
    manifest = _object(freeze.get("manifest"), "frozen G3 manifest")
    specs = {
        str(spec["run_id"]): spec
        for spec in manifest["run_specs"]
        if isinstance(spec, Mapping)
    }
    if len(receipts) != len(specs):
        raise G3GateError("public G3 receipts do not cover the full frozen factorial")
    trusted = _sha(
        manifest.get("frozen_manifest_sha256"), "trusted frozen G3 manifest hash"
    )
    supplied: dict[str, Mapping[str, Any]] = {}
    for receipt in receipts:
        if not isinstance(receipt, Mapping):
            raise G3GateError("public G3 receipt must be an object")
        run_id = receipt.get("run_id")
        spec = specs.get(str(run_id))
        if spec is None or run_id in supplied:
            raise G3GateError("public G3 receipt is duplicate or outside the freeze")
        supplied[str(run_id)] = receipt
    if set(supplied) != set(specs):
        raise G3GateError("public G3 receipt run IDs are incomplete")

    indexed: dict[str, Mapping[str, Any]] = {}
    for run_id, spec in specs.items():
        run_cell = run_cells[run_id]
        prepared = _load_cell_artifact(
            root, run_cell.get("prepared_path"), f"{run_id} prepared cell"
        )
        try:
            validate_prepared_public_g3_cell(prepared, root=root)
        except Exception as exc:
            raise G3GateError(
                f"public G3 prepared cell {run_id} is invalid: {exc}"
            ) from exc
        if (
            prepared.get("artifact_sha256") != run_cell.get("prepared_cell_sha256")
            or prepared.get("run_spec") != spec
        ):
            raise G3GateError("public G3 prepared cell identity or hash changed")

        generation = _load_cell_artifact(
            root, run_cell.get("generation_path"), f"{run_id} generation artifact"
        )
        generation_artifact_sha = sha256_json(generation)
        if generation_artifact_sha != run_cell.get("generation_artifact_sha256"):
            raise G3GateError("public G3 generation artifact hash changed")
        status = run_cell.get("generation_status")
        task = _object(spec.get("task"), f"{run_id} frozen task")
        if status == "completed":
            try:
                validate_saved_generation_result(
                    generation,
                    expected_run_id=run_id,
                    expected_task_id=str(task.get("task_id")),
                    expected_effort=str(spec.get("reasoning_effort")),
                )
            except Exception as exc:
                raise G3GateError(
                    f"public G3 generation result {run_id} is invalid: {exc}"
                ) from exc
            if generation_artifact_sha != run_cell.get("generation_result_sha256"):
                raise G3GateError("public G3 generation-result commitment changed")
        elif (
            set(generation) != {"schema_version", "run_id", "error"}
            or generation.get("schema_version")
            != "contextlab.failed-generation-result.v1"
            or generation.get("run_id") != run_id
            or not isinstance(generation.get("error"), str)
            or not str(generation["error"]).strip()
        ):
            raise G3GateError("failed public G3 generation artifact is invalid")

        saved_receipt = _load_cell_artifact(
            root, run_cell.get("receipt_path"), f"{run_id} result receipt"
        )
        receipt = supplied[run_id]
        if dict(receipt) != saved_receipt:
            raise G3GateError(
                "supplied public G3 receipt differs from its canonical file"
            )
        try:
            validate_memory_result_receipt(receipt, spec, manifest, trusted)
        except Exception as exc:
            raise G3GateError(f"public G3 receipt {run_id} is invalid: {exc}") from exc
        if receipt.get("result_sha256") != run_cell.get("receipt_sha256"):
            raise G3GateError("public G3 run receipt commitment changed")
        if receipt.get("status") != status or receipt.get(
            "prepared_cell_artifact_sha256"
        ) != prepared.get("artifact_sha256"):
            raise G3GateError(
                "public G3 receipt status or prepared-cell identity changed"
            )
        if receipt.get("generation_result_sha256") != run_cell.get(
            "generation_result_sha256"
        ):
            raise G3GateError("public G3 receipt is bound to a different generation")
        if status == "failed":
            if receipt.get("failure") != redact(generation["error"]):
                raise G3GateError(
                    "failed public G3 receipt differs from its generation artifact"
                )
        elif task.get("suite") == "static":
            evidence = _load_cell_artifact(
                root,
                run_cell.get("static_grade_evidence_path"),
                f"{run_id} static objective evidence",
            )
            if evidence.get("artifact_sha256") != run_cell.get(
                "static_grade_evidence_sha256"
            ):
                raise G3GateError("public static grade-evidence hash changed")
            try:
                validate_public_static_grade_evidence(
                    evidence,
                    prepared,
                    generation,
                    saved_generation_result_sha256=generation_artifact_sha,
                    root=root,
                )
                validate_public_static_grade(
                    _object(receipt.get("grade_artifact"), f"{run_id} static grade"),
                    prepared,
                    generation,
                    saved_generation_result_sha256=generation_artifact_sha,
                    root=root,
                )
            except Exception as exc:
                raise G3GateError(
                    f"public G3 static objective grade {run_id} is invalid: {exc}"
                ) from exc
            if receipt["grade_artifact"].get("source_grade_sha256s") != [
                evidence["artifact_sha256"]
            ]:
                raise G3GateError(
                    "public static receipt is not bound to its objective evidence"
                )
        indexed[run_id] = receipt
    try:
        recomputed = score_memory_outcomes(
            indexed.values(),
            frozen_manifest=manifest,
            trusted_frozen_manifest_sha256=trusted,
        )
    except Exception as exc:
        raise G3GateError(f"cannot recompute public G3 metrics: {exc}") from exc
    if dict(metrics) != recomputed:
        raise G3GateError("public G3 metrics differ from the exact saved receipts")
    _hash_checked(
        metrics,
        schema=MEMORY_METRICS_SCHEMA,
        hash_field="artifact_sha256",
        label="public G3 metrics",
    )
    commitments = [
        {
            "run_id": run_id,
            "result_sha256": _sha(
                indexed[run_id].get("result_sha256"), f"{run_id} result hash"
            ),
        }
        for run_id in sorted(indexed)
    ]
    return commitments, recomputed


def _validate_sealed_artifacts(
    candidate: Mapping[str, Any],
    imported: Mapping[str, Any],
    *,
    freeze: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        validate_g3_sealed_candidate_manifest(candidate)
    except Exception as exc:
        raise G3GateError(f"G3 sealed candidate manifest is invalid: {exc}") from exc
    if candidate.get("g3_freeze_sha256") != freeze.get("artifact_sha256"):
        raise G3GateError("G3 sealed candidate manifest binds a different freeze")
    event_sha = freeze["corpus_snapshot"]["temporal_event_history_sha256"]
    if candidate.get("temporal_event_history_sha256") != event_sha:
        raise G3GateError("G3 sealed candidate event history changed")
    expected_import_fields = {
        "schema_version",
        "evaluation_id",
        "candidate_manifest_sha256",
        "g3_freeze_sha256",
        "external_bundle_sha256",
        "temporal_event_history_sha256",
        "requested_model",
        "provider",
        "source_return_sha256",
        "records",
        "aggregate_metadata",
        "sealed_metrics",
        "artifact_sha256",
    }
    if set(imported) != expected_import_fields:
        raise G3GateError("G3 sealed import fields differ from the content-free schema")
    _hash_checked(
        imported,
        schema=G3_SEALED_IMPORT_SCHEMA,
        hash_field="artifact_sha256",
        label="G3 sealed import",
    )
    if (
        imported.get("candidate_manifest_sha256") != candidate.get("artifact_sha256")
        or imported.get("g3_freeze_sha256") != freeze.get("artifact_sha256")
        or imported.get("external_bundle_sha256")
        != candidate.get("external_bundle_sha256")
        or imported.get("temporal_event_history_sha256") != event_sha
        or imported.get("requested_model") != candidate.get("requested_model")
        or imported.get("provider") != candidate.get("provider")
    ):
        raise G3GateError(
            "G3 sealed import commitments differ from the frozen candidate"
        )
    _sha(imported.get("source_return_sha256"), "external G3 source-return hash")
    reconstructed: dict[str, Any] = {
        "schema_version": G3_SEALED_RETURN_SCHEMA,
        "evaluation_id": imported.get("evaluation_id"),
        "candidate_manifest_sha256": imported.get("candidate_manifest_sha256"),
        "g3_freeze_sha256": imported.get("g3_freeze_sha256"),
        "external_bundle_sha256": imported.get("external_bundle_sha256"),
        "temporal_event_history_sha256": imported.get("temporal_event_history_sha256"),
        "requested_model": imported.get("requested_model"),
        "provider": imported.get("provider"),
        "records": imported.get("records"),
        "aggregate_metadata": imported.get("aggregate_metadata"),
    }
    reconstructed["artifact_sha256"] = sha256_json(reconstructed)
    try:
        validate_g3_sealed_return(reconstructed, candidate)
    except Exception as exc:
        raise G3GateError(
            f"G3 sealed import violates the content-free return contract: {exc}"
        ) from exc
    recomputed = build_g3_sealed_metrics(imported)
    if imported.get("sealed_metrics") != recomputed:
        raise G3GateError("G3 sealed metrics differ from the content-free records")
    return recomputed


def _configuration_rows(
    public_metrics: Mapping[str, Any], sealed_metrics: Mapping[str, Any]
) -> list[dict[str, Any]]:
    summaries = _object(
        public_metrics.get("policy_effort_metrics"), "public policy-effort metrics"
    )
    comparisons = _object(
        public_metrics.get("paired_comparisons"), "public paired comparisons"
    )
    screens = _object(public_metrics.get("acceptance_screen"), "public screens")
    acceptance = _object(
        public_metrics.get("acceptance_parameters"), "public acceptance parameters"
    )
    expected = set(_CONFIGURATION_KEYS)
    if (
        set(summaries) != expected
        or set(comparisons) != expected
        or set(screens) != expected
        or set(sealed_metrics) != expected
    ):
        raise G3GateError("G3 metrics do not report every M0-M4 low/high configuration")
    provenance_minimum = _number(
        acceptance.get("provenance_minimum"), "frozen provenance minimum"
    )
    static_floor = _number(
        acceptance.get("static_accuracy_regression_floor"),
        "frozen static regression floor",
    )
    sealed_accuracy = {
        key: _number(
            _object(sealed_metrics[key], f"sealed {key}").get("accuracy"),
            f"sealed {key} accuracy",
        )
        for key in _CONFIGURATION_KEYS
    }
    rows: list[dict[str, Any]] = []
    for policy in MEMORY_CONFIGURATIONS:
        for effort in ALLOWED_REASONING_EFFORTS:
            key = f"{policy}:{effort}"
            summary = _object(summaries[key], f"public {key} summary")
            comparison = _object(comparisons[key], f"public {key} comparison")
            temporal = _object(
                _object(
                    comparison.get("temporal_vs_m0"), f"{key} temporal comparison"
                ).get("paired_bootstrap_ci"),
                f"{key} temporal paired bootstrap",
            )
            static = _object(
                _object(
                    comparison.get("static_regression_vs_m0"),
                    f"{key} static comparison",
                ).get("paired_bootstrap_ci"),
                f"{key} static paired bootstrap",
            )
            sealed = _object(sealed_metrics[key], f"sealed {key}")
            public_temporal_delta = _number(
                temporal.get("mean_delta"), f"{key} public temporal delta"
            )
            public_static_delta = _number(
                static.get("mean_delta"), f"{key} public static delta"
            )
            sealed_delta = sealed_accuracy[key] - sealed_accuracy[f"M0:{effort}"]
            occurrence_count = _integer(
                summary.get("used_memory_claim_occurrence_count"),
                f"{key} used claim occurrence count",
            )
            provenance_value = summary.get("claim_level_provenance_rate")
            provenance_rate = (
                _number(provenance_value, f"{key} occurrence-level provenance")
                if provenance_value is not None
                else None
            )
            public_failed = _integer(
                summary.get("failed_result_count"), f"{key} public failed count"
            )
            sealed_failed = _integer(
                sealed.get("failed_count"), f"{key} sealed failed count"
            )
            sealed_provenance = _number(
                sealed.get("provenance_completeness"),
                f"{key} sealed provenance completeness",
            )
            temporal_improved = public_temporal_delta > 0.0
            sealed_improved = sealed_delta > 0.0
            static_within_limit = public_static_delta >= static_floor
            provenance_met = (
                occurrence_count > 0
                and provenance_rate is not None
                and provenance_rate >= provenance_minimum
            )
            sealed_provenance_met = sealed_provenance >= provenance_minimum
            no_failed_results = public_failed == 0 and sealed_failed == 0
            harmful_reasons: list[str] = []
            if public_temporal_delta < 0:
                harmful_reasons.append("public_temporal_regression")
            if public_static_delta < 0:
                harmful_reasons.append("public_static_regression")
            if sealed_delta < 0:
                harmful_reasons.append("sealed_temporal_regression")
            failure_reasons: list[str] = []
            if public_failed:
                failure_reasons.append("public_failed_results")
            if sealed_failed:
                failure_reasons.append("sealed_failed_results")
            screen = _object(screens[key], f"public {key} screen")
            eligible = (
                policy != "M0"
                and temporal_improved
                and sealed_improved
                and static_within_limit
                and provenance_met
                and sealed_provenance_met
                and no_failed_results
            )
            rows.append(
                {
                    "configuration": key,
                    "policy": policy,
                    "reasoning_effort": effort,
                    "public_temporal_mean_delta_vs_m0": public_temporal_delta,
                    "sealed_temporal_accuracy_delta_vs_m0": sealed_delta,
                    "public_static_accuracy_mean_delta_vs_m0": public_static_delta,
                    "used_memory_claim_occurrence_count": occurrence_count,
                    "claim_level_raw_provenance_rate": provenance_rate,
                    "sealed_answer_provenance_rate": sealed_provenance,
                    "public_failed_result_count": public_failed,
                    "sealed_failed_result_count": sealed_failed,
                    "public_temporal_improved": temporal_improved,
                    "sealed_temporal_improved": sealed_improved,
                    "static_within_preregistered_limit": static_within_limit,
                    "occurrence_level_provenance_minimum_met": provenance_met,
                    "sealed_provenance_minimum_met": sealed_provenance_met,
                    "no_failed_results": no_failed_results,
                    "harmful_reasons": harmful_reasons,
                    "failure_reasons": failure_reasons,
                    "public_screen_eligible_claim": screen.get(
                        "public_screen_eligible"
                    ),
                    "public_screen_is_non_authoritative": True,
                    "full_gate_configuration_eligible": eligible,
                }
            )
    return rows


def build_g3_failure_report(
    *, public_metrics: Mapping[str, Any], sealed_import: Mapping[str, Any]
) -> dict[str, Any]:
    """Build a complete content-free report of every failed or harmful lane."""

    metrics = _object(public_metrics, "public G3 metrics")
    sealed = _object(sealed_import, "G3 sealed import")
    sealed_metrics = _object(sealed.get("sealed_metrics"), "G3 sealed metrics")
    rows = _configuration_rows(metrics, sealed_metrics)
    payload: dict[str, Any] = {
        "schema_version": G3_FAILURE_REPORT_SCHEMA,
        "public_metrics_sha256": _sha(
            metrics.get("artifact_sha256"), "public G3 metrics hash"
        ),
        "sealed_import_sha256": _sha(
            sealed.get("artifact_sha256"), "G3 sealed import hash"
        ),
        "configuration_count": len(rows),
        "configurations": rows,
        "failed_configurations": [
            row["configuration"] for row in rows if row["failure_reasons"]
        ],
        "harmful_configurations": [
            row["configuration"] for row in rows if row["harmful_reasons"]
        ],
        "rejected_configurations": [
            row["configuration"]
            for row in rows
            if row["policy"] != "M0" and not row["full_gate_configuration_eligible"]
        ],
    }
    payload["artifact_sha256"] = sha256_json(payload)
    return payload


def validate_g3_failure_report(
    value: Mapping[str, Any],
    *,
    public_metrics: Mapping[str, Any],
    sealed_import: Mapping[str, Any],
) -> None:
    """Reject omissions, relabeling, stale hashes, and self-authored pass claims."""

    report = _object(value, "G3 failure and harm report")
    expected_fields = {
        "schema_version",
        "public_metrics_sha256",
        "sealed_import_sha256",
        "configuration_count",
        "configurations",
        "failed_configurations",
        "harmful_configurations",
        "rejected_configurations",
        "artifact_sha256",
    }
    if set(report) != expected_fields:
        raise G3GateError("G3 failure and harm report fields differ")
    _hash_checked(
        report,
        schema=G3_FAILURE_REPORT_SCHEMA,
        hash_field="artifact_sha256",
        label="G3 failure and harm report",
    )
    expected = build_g3_failure_report(
        public_metrics=public_metrics, sealed_import=sealed_import
    )
    if dict(report) != expected:
        raise G3GateError("G3 failure and harm report omits or changes a configuration")


def _validate_reasoning_effects(metrics: Mapping[str, Any]) -> str:
    effects = _object(
        metrics.get("reasoning_effort_effects"), "G3 reasoning-effort effects"
    )
    if set(effects) != {"temporal", "static"}:
        raise G3GateError("G3 metrics omit a low/high main-effect suite")
    for suite in ("temporal", "static"):
        row = _object(effects[suite], f"{suite} low/high effects")
        if set(row) != {
            "main_high_minus_low",
            "main_effect_task_family_effects",
            "by_policy",
            "policy_interactions_vs_m0",
        }:
            raise G3GateError(f"{suite} low/high effect fields differ")
        _object(row["main_high_minus_low"], f"{suite} low/high main effect")
        families = _object(
            row["main_effect_task_family_effects"],
            f"{suite} low/high task-family effects",
        )
        if not families:
            raise G3GateError(f"{suite} low/high task-family effects are empty")
        by_policy = _object(row["by_policy"], f"{suite} effects by policy")
        interactions = _object(
            row["policy_interactions_vs_m0"], f"{suite} policy interactions"
        )
        if set(by_policy) != set(MEMORY_CONFIGURATIONS) or set(interactions) != set(
            MEMORY_CONFIGURATIONS
        ):
            raise G3GateError(f"{suite} low/high policy effects are incomplete")
        for policy in MEMORY_CONFIGURATIONS:
            policy_row = _object(by_policy[policy], f"{suite} {policy} low/high effect")
            if set(policy_row) != {"high_minus_low", "task_family_effects"}:
                raise G3GateError(f"{suite} {policy} low/high effect fields differ")
            _object(policy_row["high_minus_low"], f"{suite} {policy} main effect")
            _object(
                policy_row["task_family_effects"],
                f"{suite} {policy} task-family effects",
            )
            interaction = _object(
                interactions[policy], f"{suite} {policy} policy interaction"
            )
            if set(interaction) != {"difference_in_differences_vs_m0"}:
                raise G3GateError(f"{suite} {policy} policy interaction fields differ")
            _object(
                interaction["difference_in_differences_vs_m0"],
                f"{suite} {policy} difference in differences",
            )
    return sha256_json(effects)


def _canonical_lifecycle(
    supplied: Mapping[str, Any], *, root: Path, freeze: Mapping[str, Any]
) -> dict[str, Any]:
    lifecycle = _read_canonical(
        supplied,
        root=root,
        relative_path=G3_LIFECYCLE_PATH,
        label="G3 lifecycle evidence",
        validator=validate_g3_lifecycle_evidence,
    )
    if lifecycle.get("event_history_sha256") != freeze["corpus_snapshot"].get(
        "temporal_event_history_sha256"
    ):
        raise G3GateError("G3 lifecycle replay is bound to a different event history")
    cases = lifecycle.get("lifecycle_checks")
    if (
        lifecycle.get("all_passed") is not True
        or not isinstance(cases, list)
        or {row.get("case") for row in cases if isinstance(row, Mapping)}
        != {"correction", "expiry", "tombstone", "rollback"}
        or any(row.get("passed") is not True for row in cases)
    ):
        raise G3GateError(
            "G3 correction, expiry, tombstone, or rollback evidence failed"
        )
    return lifecycle


def _valid_utc_second(value: Any) -> bool:
    if not isinstance(value, str) or _UTC_SECOND.fullmatch(value) is None:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _valid_utc_iso(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    offset = parsed.utcoffset()
    return offset is not None and offset.total_seconds() == 0


def validate_g3_g1_prerequisite(
    value: Mapping[str, Any],
    *,
    approval: Mapping[str, Any],
    root: Path,
) -> None:
    """Replay canonical G0/G1 evidence and require exact separate approval bytes."""

    try:
        current_technical, current_approval = load_approved_g1_gate(root)
    except GateError as exc:
        raise G3GateError("G1 prerequisite replay failed") from exc
    if dict(value) != current_technical or dict(approval) != current_approval:
        raise G3GateError("G1 prerequisite is not the current canonical approval")


def _g1_binding(
    value: Mapping[str, Any], approval: Mapping[str, Any]
) -> dict[str, str]:
    record = _object(value, "G1 technical record")
    approval_record = _object(approval, "G1 Kevin approval")
    technical_sha = _sha(
        record.get("technical_evidence_sha256"), "G1 technical-evidence hash"
    )
    if (
        record.get("artifact_sha256")
        != sha256_json(
            {key: item for key, item in record.items() if key != "artifact_sha256"}
        )
        or approval_record.get("technical_evidence_sha256") != technical_sha
        or approval_record.get("technical_gate_artifact_sha256")
        != record.get("artifact_sha256")
        or approval_record.get("artifact_sha256")
        != sha256_json(
            {
                key: item
                for key, item in approval_record.items()
                if key != "artifact_sha256"
            }
        )
    ):
        raise G3GateError("G1 binding is invalid")
    return {
        "g1_gate_record_sha256": sha256_json(value),
        "g1_approved_technical_evidence_sha256": technical_sha,
        "g1_kevin_approval_sha256": sha256_json(approval_record),
    }


def _canonical_g1_gate(
    supplied: Mapping[str, Any] | None, *, root: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        canonical, approval = load_approved_g1_gate(root)
    except GateError as exc:
        raise G3GateError("cannot replay the current canonical G1 gate") from exc
    validate_g3_g1_prerequisite(canonical, approval=approval, root=root)
    if supplied is not None and dict(supplied) != canonical:
        raise G3GateError("supplied G1 gate is not the current canonical record")
    return canonical, approval


def _build_g3_pending_gate(
    *,
    g1_gate: Mapping[str, Any] | None,
    g3_freeze: Mapping[str, Any],
    public_run: Mapping[str, Any],
    public_receipts: Sequence[Mapping[str, Any]],
    public_metrics: Mapping[str, Any],
    sealed_candidate_manifest: Mapping[str, Any],
    sealed_import: Mapping[str, Any],
    lifecycle_evidence: Mapping[str, Any],
    panel_calibration: Mapping[str, Any],
    failure_report: Mapping[str, Any],
    unsupported_memory_review: Mapping[str, Any],
    unsupported_memory_review_approval: Mapping[str, Any],
    root: Path,
) -> dict[str, Any]:
    current_g1, current_g1_approval = _canonical_g1_gate(g1_gate, root=root)
    g1_binding = _g1_binding(current_g1, current_g1_approval)
    freeze, protocol, protocol_acceptance = _canonical_freeze(g3_freeze, root)
    run_cells = _canonical_public_run(public_run, root=root, freeze=freeze)
    receipt_commitments, metrics = _public_receipts_and_metrics(
        public_receipts,
        public_metrics,
        freeze=freeze,
        run_cells=run_cells,
        root=root,
    )
    sealed_metrics = _validate_sealed_artifacts(
        sealed_candidate_manifest, sealed_import, freeze=freeze
    )
    lifecycle = _canonical_lifecycle(lifecycle_evidence, root=root, freeze=freeze)
    panel = _object(panel_calibration, "G3 panel calibration")
    try:
        validate_g3_panel_calibration(panel)
    except Exception as exc:
        raise G3GateError(f"G3 panel calibration is invalid: {exc}") from exc
    if (
        panel.get("schema_version") != G3_PANEL_CALIBRATION_SCHEMA
        or panel.get("reviewers") != list(REVIEWERS)
        or panel.get("ai_reviewers") != list(AI_REVIEWERS)
        or panel.get("sole_human_reviewer") != SOLE_HUMAN_REVIEWER
    ):
        raise G3GateError("G3 calibration does not contain GPT, Claude, and Kevin")
    reasoning_effects_sha = _validate_reasoning_effects(metrics)
    validate_g3_failure_report(
        failure_report,
        public_metrics=metrics,
        sealed_import=sealed_import,
    )
    try:
        validate_unsupported_memory_review(
            unsupported_memory_review,
            metrics=metrics,
            root=root,
        )
        validate_kevin_unsupported_memory_review_approval(
            unsupported_memory_review_approval,
            report=unsupported_memory_review,
        )
    except Exception as exc:
        raise G3GateError(
            f"G3 unsupported-memory dispositions are invalid: {exc}"
        ) from exc
    rows = _configuration_rows(metrics, sealed_metrics)
    eligible_configurations = [
        row["configuration"] for row in rows if row["full_gate_configuration_eligible"]
    ]
    metric_eligible_policies = [
        policy
        for policy in MEMORY_CONFIGURATIONS[1:]
        if all(
            f"{policy}:{effort}" in eligible_configurations
            for effort in ALLOWED_REASONING_EFFORTS
        )
    ]
    panel_approved = panel.get("status") == "approved"
    descriptive_consensus = _descriptive_panel_consensus(panel)
    eligible_policies = metric_eligible_policies if panel_approved else []
    technical_complete = lifecycle.get("all_passed") is True and (
        panel_approved or descriptive_consensus
    )
    technical_disposition = (
        "blocked"
        if not technical_complete
        else "promotion-ready"
        if eligible_policies
        else "retain-simple"
    )
    acceptance = freeze["manifest"]["acceptance_parameters"]
    technical: dict[str, Any] = {
        "schema_version": G3_GATE_SCHEMA,
        "scope": "provider_free_content_free_g3_final_gate",
        **g1_binding,
        "g3_freeze_sha256": freeze["artifact_sha256"],
        "frozen_manifest_sha256": freeze["manifest"]["frozen_manifest_sha256"],
        "memory_protocol_sha256": freeze["memory_protocol_sha256"],
        "public_run_sha256": public_run["artifact_sha256"],
        "public_receipt_count": len(receipt_commitments),
        "public_receipt_commitments": receipt_commitments,
        "public_receipt_commitments_sha256": sha256_json(receipt_commitments),
        "public_metrics_sha256": metrics["artifact_sha256"],
        "sealed_candidate_manifest_sha256": sealed_candidate_manifest[
            "artifact_sha256"
        ],
        "sealed_import_sha256": sealed_import["artifact_sha256"],
        "sealed_source_return_sha256": sealed_import["source_return_sha256"],
        "external_bundle_sha256": sealed_import["external_bundle_sha256"],
        "lifecycle_evidence_sha256": lifecycle["artifact_sha256"],
        "panel_calibration_sha256": panel["artifact_sha256"],
        "panel_agreement_report_sha256": sha256_json(
            {
                "metrics_vs_reference": panel["metrics_vs_reference"],
                "ai_vs_kevin": panel["ai_vs_kevin"],
                "ai_invocation_receipts": panel["ai_invocation_receipts"],
                "hidden_repeat_consistency_by_reviewer": panel[
                    "hidden_repeat_consistency_by_reviewer"
                ],
            }
        ),
        "failure_and_harm_report_sha256": failure_report["artifact_sha256"],
        "unsupported_memory_review_sha256": unsupported_memory_review[
            "artifact_sha256"
        ],
        "unsupported_memory_review_kevin_approval_sha256": (
            unsupported_memory_review_approval["artifact_sha256"]
        ),
        "reasoning_effort_effects_sha256": reasoning_effects_sha,
        "preregistered_acceptance": {
            "primary_metric": acceptance["primary_metric"],
            "provenance_minimum": acceptance["provenance_minimum"],
            "static_accuracy_regression_floor": acceptance[
                "static_accuracy_regression_floor"
            ],
            "paired_bootstrap_resamples": acceptance["paired_bootstrap_resamples"],
            "paired_bootstrap_seed": acceptance["paired_bootstrap_seed"],
            "temporal_improvement_rule": protocol_acceptance[
                "temporal_improvement_rule"
            ],
            "failed_results_allowed_for_promotion": protocol_acceptance[
                "failed_results_allowed_for_promotion"
            ],
        },
        "configuration_evidence": rows,
        "eligible_configurations": eligible_configurations,
        "eligible_policies": eligible_policies,
        "acceptance_checks": {
            "current_hash_approved_g1": True,
            "canonical_g3_freeze": True,
            "complete_public_run_and_receipts": True,
            "public_metrics_recomputed": True,
            "external_content_free_sealed_return": True,
            "event_replay_and_lifecycle_tests": lifecycle["all_passed"] is True,
            "three_member_panel_calibration": panel_approved,
            "descriptive_panel_consensus_fallback": descriptive_consensus,
            "low_high_main_and_policy_interaction_effects": True,
            "all_failed_and_harmful_configurations_reported": True,
            "all_unsupported_memory_answers_disposed": True,
            "unsupported_memory_dispositions_audited_by_kevin": True,
            "public_screen_used_as_gate": False,
        },
        "technical_complete": technical_complete,
        "technical_disposition": technical_disposition,
        "limitations": [
            "Public-screen eligibility is diagnostic evidence only and cannot satisfy G3.",
            "Sealed records are content-free; the gate binds their external source-return commitment without reading protected content.",
            "Only Kevin can select an eligible memory policy or explicitly retain simple memory.",
            "The calibration panel contains one human reviewer.",
            *(
                [
                    "The preregistered calibration reference disagreed with three mutually consistent reviewers; this demonstration is descriptive only and cannot promote or rank a memory policy."
                ]
                if descriptive_consensus
                else []
            ),
            "The historical unsupported-memory export also contains broader answer-provenance failures; the exhaustive disposition report separates those scopes.",
        ],
    }
    technical_record_sha256 = sha256_json(technical)
    result: dict[str, Any] = {
        **technical,
        "technical_record_sha256": technical_record_sha256,
        "pending_gate_artifact_sha256": None,
        "ai_gate_reviews": [],
        "gate_review_status": "pending",
        "human_decision": {
            "status": "pending",
            "reviewer": "Kevin Araujo",
            "reason": "both exact AI final gate reviews must pass before Kevin decides",
        },
        "promoted_memory_policy": None,
        "final_decision": "blocked",
    }
    result["artifact_sha256"] = sha256_json(result)
    return result


_G3_TECHNICAL_FIELDS = frozenset(
    {
        "schema_version",
        "scope",
        "g1_gate_record_sha256",
        "g1_approved_technical_evidence_sha256",
        "g1_kevin_approval_sha256",
        "g3_freeze_sha256",
        "frozen_manifest_sha256",
        "memory_protocol_sha256",
        "public_run_sha256",
        "public_receipt_count",
        "public_receipt_commitments",
        "public_receipt_commitments_sha256",
        "public_metrics_sha256",
        "sealed_candidate_manifest_sha256",
        "sealed_import_sha256",
        "sealed_source_return_sha256",
        "external_bundle_sha256",
        "lifecycle_evidence_sha256",
        "panel_calibration_sha256",
        "panel_agreement_report_sha256",
        "failure_and_harm_report_sha256",
        "unsupported_memory_review_sha256",
        "unsupported_memory_review_kevin_approval_sha256",
        "reasoning_effort_effects_sha256",
        "preregistered_acceptance",
        "configuration_evidence",
        "eligible_configurations",
        "eligible_policies",
        "acceptance_checks",
        "technical_complete",
        "technical_disposition",
        "limitations",
    }
)
_G3_RECORD_FIELDS = _G3_TECHNICAL_FIELDS | {
    "technical_record_sha256",
    "pending_gate_artifact_sha256",
    "ai_gate_reviews",
    "gate_review_status",
    "human_decision",
    "promoted_memory_policy",
    "final_decision",
    "artifact_sha256",
}
_G3_AI_REVIEW_FIELDS = frozenset(
    {
        "schema_version",
        "reviewer_id",
        "reviewer_name",
        "model_id",
        "reasoning_effort",
        "invocation",
        "technical_record_sha256",
        "pending_gate_artifact_sha256",
        "verdict",
        "blocking_findings",
        "completed_at",
        "invocation_receipt",
        "artifact_sha256",
    }
)
_G3_AI_INVOCATION_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "reviewer_id",
        "invocation_source",
        "invocation_id",
        "requested_model",
        "resolved_model",
        "reasoning_effort",
        "technical_record_sha256",
        "pending_gate_artifact_sha256",
        "review_payload_sha256",
        "native_invocation_evidence_path",
        "native_invocation_evidence_sha256",
        "native_output_sha256",
        "completed_at",
        "artifact_sha256",
    }
)


def _technical_part(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != _G3_RECORD_FIELDS:
        raise G3GateError("G3 gate record fields differ")
    return {key: value[key] for key in _G3_TECHNICAL_FIELDS}


def _pending_human_decision() -> dict[str, str]:
    return {
        "status": "pending",
        "reviewer": "Kevin Araujo",
        "reason": "both exact AI final gate reviews must pass before Kevin decides",
    }


def validate_g3_pending_gate(
    value: Mapping[str, Any],
    *,
    g1_gate: Mapping[str, Any] | None = None,
    g1_approval: Mapping[str, Any] | None = None,
) -> None:
    """Validate a pure pending G3 technical record before either AI review."""

    pending = _object(value, "pending G3 gate")
    technical = _technical_part(pending)
    if (
        pending.get("schema_version") != G3_GATE_SCHEMA
        or pending.get("scope") != "provider_free_content_free_g3_final_gate"
        or pending.get("artifact_sha256")
        != sha256_json(
            {key: item for key, item in pending.items() if key != "artifact_sha256"}
        )
        or pending.get("technical_record_sha256") != sha256_json(technical)
    ):
        raise G3GateError("pending G3 technical-record hash is invalid")
    for field in (
        "g1_gate_record_sha256",
        "g1_approved_technical_evidence_sha256",
        "g1_kevin_approval_sha256",
    ):
        _sha(pending.get(field), field)
    if (g1_gate is None) != (g1_approval is None):
        raise G3GateError("G1 technical record and approval must be supplied together")
    if g1_gate is not None and g1_approval is not None:
        binding = _g1_binding(g1_gate, g1_approval)
        if any(pending.get(key) != item for key, item in binding.items()):
            raise G3GateError("pending G3 gate is bound to a different G1 record")
    eligible = pending.get("eligible_policies")
    if (
        not isinstance(eligible, list)
        or len(eligible) != len(set(eligible))
        or any(policy not in MEMORY_CONFIGURATIONS[1:] for policy in eligible)
        or not isinstance(pending.get("technical_complete"), bool)
    ):
        raise G3GateError("pending G3 technical eligibility is invalid")
    expected_disposition = (
        "blocked"
        if pending["technical_complete"] is False
        else "promotion-ready"
        if eligible
        else "retain-simple"
    )
    if pending.get("technical_disposition") != expected_disposition:
        raise G3GateError("pending G3 technical disposition is invalid")
    if (
        pending.get("pending_gate_artifact_sha256") is not None
        or pending.get("ai_gate_reviews") != []
        or pending.get("gate_review_status") != "pending"
        or pending.get("human_decision") != _pending_human_decision()
        or pending.get("promoted_memory_policy") is not None
        or pending.get("final_decision") != "blocked"
    ):
        raise G3GateError("G3 technical record is not pending both AI reviews")


def build_g3_pending_gate(
    *,
    g1_gate: Mapping[str, Any] | None = None,
    g3_freeze: Mapping[str, Any],
    public_run: Mapping[str, Any],
    public_receipts: Sequence[Mapping[str, Any]],
    public_metrics: Mapping[str, Any],
    sealed_candidate_manifest: Mapping[str, Any],
    sealed_import: Mapping[str, Any],
    lifecycle_evidence: Mapping[str, Any],
    panel_calibration: Mapping[str, Any],
    failure_report: Mapping[str, Any],
    unsupported_memory_review: Mapping[str, Any],
    unsupported_memory_review_approval: Mapping[str, Any],
    root: Path | None = None,
) -> dict[str, Any]:
    """Build the current G1-bound technical record with both AI reviews pending."""

    root_value = (root or repository_root()).resolve()
    pending = _build_g3_pending_gate(
        g1_gate=g1_gate,
        g3_freeze=_object(g3_freeze, "G3 freeze"),
        public_run=_object(public_run, "public G3 run"),
        public_receipts=public_receipts,
        public_metrics=_object(public_metrics, "public G3 metrics"),
        sealed_candidate_manifest=_object(
            sealed_candidate_manifest, "G3 sealed candidate manifest"
        ),
        sealed_import=_object(sealed_import, "G3 sealed import"),
        lifecycle_evidence=_object(lifecycle_evidence, "G3 lifecycle evidence"),
        panel_calibration=_object(panel_calibration, "G3 panel calibration"),
        failure_report=_object(failure_report, "G3 failure and harm report"),
        unsupported_memory_review=_object(
            unsupported_memory_review, "G3 unsupported-memory disposition report"
        ),
        unsupported_memory_review_approval=_object(
            unsupported_memory_review_approval,
            "G3 unsupported-memory disposition Kevin approval",
        ),
        root=root_value,
    )
    current_g1, current_g1_approval = _canonical_g1_gate(g1_gate, root=root_value)
    validate_g3_pending_gate(
        pending,
        g1_gate=current_g1,
        g1_approval=current_g1_approval,
    )
    return pending


def _g3_ai_gate_review_payload(
    pending_gate: Mapping[str, Any],
    *,
    reviewer_id: str,
    verdict: str,
    blocking_findings: Sequence[str],
    completed_at: str,
) -> dict[str, Any]:
    validate_g3_pending_gate(pending_gate)
    if pending_gate.get("technical_complete") is not True:
        raise G3GateError(
            "AI final gate review requires complete G3 technical evidence"
        )
    identity = _G3_AI_GATE_REVIEWERS[reviewer_id]
    if isinstance(blocking_findings, (str, bytes)) or not isinstance(
        blocking_findings, Sequence
    ):
        raise G3GateError("G3 AI review findings must be a list")
    findings = list(blocking_findings)
    if verdict == "pass":
        if findings:
            raise G3GateError("a passing G3 AI gate review cannot retain blockers")
    elif verdict == "fail":
        if not findings:
            raise G3GateError("a failed G3 AI gate review must explain its blocker")
    else:
        raise G3GateError("G3 AI gate-review verdict is invalid")
    if not _valid_utc_second(completed_at):
        raise G3GateError("G3 AI gate-review timestamp must be UTC to the second")
    return {
        "schema_version": G3_AI_GATE_REVIEW_SCHEMA,
        "reviewer_id": reviewer_id,
        **{
            key: identity[key]
            for key in (
                "reviewer_name",
                "model_id",
                "reasoning_effort",
                "invocation",
            )
        },
        "technical_record_sha256": pending_gate["technical_record_sha256"],
        "pending_gate_artifact_sha256": pending_gate["artifact_sha256"],
        "verdict": verdict,
        "blocking_findings": findings,
        "completed_at": completed_at,
    }


def build_g3_ai_gate_invocation_receipt(
    pending_gate: Mapping[str, Any],
    *,
    reviewer_id: str,
    invocation_id: str,
    verdict: str,
    blocking_findings: Sequence[str],
    completed_at: str,
    native_invocation_evidence_sha256: str,
    native_output_sha256: str,
) -> dict[str, Any]:
    """Bind one real fixed-profile invocation to its exact G3 review payload."""

    if reviewer_id not in _G3_AI_GATE_REVIEWERS:
        raise G3GateError("unknown G3 AI gate reviewer")
    if (
        not isinstance(invocation_id, str)
        or _INVOCATION_ID.fullmatch(invocation_id) is None
    ):
        raise G3GateError("G3 AI gate invocation ID is invalid")
    payload = _g3_ai_gate_review_payload(
        pending_gate,
        reviewer_id=reviewer_id,
        verdict=verdict,
        blocking_findings=blocking_findings,
        completed_at=completed_at,
    )
    identity = _G3_AI_GATE_REVIEWERS[reviewer_id]
    receipt: dict[str, Any] = {
        "schema_version": G3_AI_GATE_INVOCATION_RECEIPT_SCHEMA,
        "reviewer_id": reviewer_id,
        "invocation_source": identity["invocation_source"],
        "invocation_id": invocation_id,
        "requested_model": identity["model_id"],
        "resolved_model": identity["model_id"],
        "reasoning_effort": identity["reasoning_effort"],
        "technical_record_sha256": pending_gate["technical_record_sha256"],
        "pending_gate_artifact_sha256": pending_gate["artifact_sha256"],
        "review_payload_sha256": sha256_json(payload),
        "native_invocation_evidence_path": native_review_paths(
            G3_AI_GATE_INVOCATION_RECEIPT_PATHS[reviewer_id]
        )[0].as_posix(),
        "native_invocation_evidence_sha256": _sha(
            native_invocation_evidence_sha256,
            "G3 native invocation evidence hash",
        ),
        "native_output_sha256": _sha(
            native_output_sha256, "G3 native invocation output hash"
        ),
        "completed_at": completed_at,
    }
    receipt["artifact_sha256"] = sha256_json(receipt)
    _validate_g3_ai_gate_invocation_receipt(
        receipt,
        pending_gate=pending_gate,
        reviewer_id=reviewer_id,
        review_payload=payload,
    )
    return receipt


def _validate_g3_ai_gate_invocation_receipt(
    value: Any,
    *,
    pending_gate: Mapping[str, Any],
    reviewer_id: str,
    review_payload: Mapping[str, Any],
) -> None:
    receipt = _object(value, "G3 AI gate invocation receipt")
    if set(receipt) != _G3_AI_INVOCATION_RECEIPT_FIELDS:
        raise G3GateError("G3 AI gate invocation-receipt fields differ")
    _hash_checked(
        receipt,
        schema=G3_AI_GATE_INVOCATION_RECEIPT_SCHEMA,
        hash_field="artifact_sha256",
        label="G3 AI gate invocation receipt",
    )
    identity = _G3_AI_GATE_REVIEWERS[reviewer_id]
    expected_native_path = native_review_paths(
        G3_AI_GATE_INVOCATION_RECEIPT_PATHS[reviewer_id]
    )[0].as_posix()
    if (
        receipt.get("reviewer_id") != reviewer_id
        or receipt.get("invocation_source") != identity["invocation_source"]
        or receipt.get("requested_model") != identity["model_id"]
        or receipt.get("resolved_model") != identity["model_id"]
        or receipt.get("reasoning_effort") != identity["reasoning_effort"]
        or not isinstance(receipt.get("invocation_id"), str)
        or _INVOCATION_ID.fullmatch(str(receipt["invocation_id"])) is None
        or receipt.get("technical_record_sha256")
        != pending_gate.get("technical_record_sha256")
        or receipt.get("pending_gate_artifact_sha256")
        != pending_gate.get("artifact_sha256")
        or receipt.get("review_payload_sha256") != sha256_json(review_payload)
        or receipt.get("native_invocation_evidence_path") != expected_native_path
        or receipt.get("completed_at") != review_payload.get("completed_at")
    ):
        raise G3GateError("G3 AI gate invocation identity or payload binding changed")
    _sha(
        receipt.get("native_invocation_evidence_sha256"),
        "G3 native invocation evidence hash",
    )
    _sha(receipt.get("native_output_sha256"), "G3 native invocation output hash")


def _build_g3_ai_gate_review(
    pending_gate: Mapping[str, Any],
    *,
    reviewer_id: str,
    invocation_receipt: Mapping[str, Any],
    verdict: str,
    blocking_findings: Sequence[str],
    completed_at: str,
) -> dict[str, Any]:
    payload = _g3_ai_gate_review_payload(
        pending_gate,
        reviewer_id=reviewer_id,
        verdict=verdict,
        blocking_findings=blocking_findings,
        completed_at=completed_at,
    )
    _validate_g3_ai_gate_invocation_receipt(
        invocation_receipt,
        pending_gate=pending_gate,
        reviewer_id=reviewer_id,
        review_payload=payload,
    )
    review: dict[str, Any] = {**payload, "invocation_receipt": dict(invocation_receipt)}
    review["artifact_sha256"] = sha256_json(review)
    validate_g3_ai_gate_review(review, pending_gate=pending_gate)
    return review


def build_g3_sol_gate_review(
    pending_gate: Mapping[str, Any],
    *,
    invocation_receipt: Mapping[str, Any],
    verdict: str,
    blocking_findings: Sequence[str],
    completed_at: str,
) -> dict[str, Any]:
    """Build only the fixed GPT-5.6 Sol/high final gate review artifact."""

    return _build_g3_ai_gate_review(
        pending_gate,
        reviewer_id="gpt-5.6-sol-high",
        invocation_receipt=invocation_receipt,
        verdict=verdict,
        blocking_findings=blocking_findings,
        completed_at=completed_at,
    )


def build_g3_claude_gate_review(
    pending_gate: Mapping[str, Any],
    *,
    invocation_receipt: Mapping[str, Any],
    verdict: str,
    blocking_findings: Sequence[str],
    completed_at: str,
) -> dict[str, Any]:
    """Build only the fixed Claude Opus 5/medium final gate review artifact."""

    return _build_g3_ai_gate_review(
        pending_gate,
        reviewer_id="claude-opus-5-medium",
        invocation_receipt=invocation_receipt,
        verdict=verdict,
        blocking_findings=blocking_findings,
        completed_at=completed_at,
    )


def validate_g3_ai_gate_review(
    value: Mapping[str, Any], *, pending_gate: Mapping[str, Any]
) -> None:
    """Validate one fixed-identity AI review against the pending technical record."""

    validate_g3_pending_gate(pending_gate)
    review = _object(value, "G3 AI gate review")
    if set(review) != _G3_AI_REVIEW_FIELDS:
        raise G3GateError("G3 AI gate-review fields differ")
    _hash_checked(
        review,
        schema=G3_AI_GATE_REVIEW_SCHEMA,
        hash_field="artifact_sha256",
        label="G3 AI gate review",
    )
    reviewer_id = review.get("reviewer_id")
    identity = _G3_AI_GATE_REVIEWERS.get(str(reviewer_id))
    if identity is None or any(
        review.get(key) != identity[key]
        for key in ("reviewer_name", "model_id", "reasoning_effort", "invocation")
    ):
        raise G3GateError("G3 AI gate-review model, effort, or invocation changed")
    if review.get("technical_record_sha256") != pending_gate.get(
        "technical_record_sha256"
    ) or review.get("pending_gate_artifact_sha256") != pending_gate.get(
        "artifact_sha256"
    ):
        raise G3GateError("G3 AI gate review is bound to a stale technical record")
    findings = review.get("blocking_findings")
    if not isinstance(findings, list) or any(
        not isinstance(item, str) or not item.strip() for item in findings
    ):
        raise G3GateError("G3 AI gate-review findings are invalid")
    if review.get("verdict") == "pass":
        if findings:
            raise G3GateError("a passing G3 AI gate review cannot retain blockers")
    elif review.get("verdict") == "fail":
        if not findings:
            raise G3GateError("a failed G3 AI gate review must explain its blocker")
    else:
        raise G3GateError("G3 AI gate-review verdict is invalid")
    if not _valid_utc_second(review.get("completed_at")):
        raise G3GateError("G3 AI gate-review timestamp must be UTC to the second")
    payload = {
        key: item
        for key, item in review.items()
        if key not in {"invocation_receipt", "artifact_sha256"}
    }
    _validate_g3_ai_gate_invocation_receipt(
        review.get("invocation_receipt"),
        pending_gate=pending_gate,
        reviewer_id=str(reviewer_id),
        review_payload=payload,
    )


def validate_g3_ai_gate_review_provenance(
    root: Path,
    *,
    pending_gate: Mapping[str, Any],
    review: Mapping[str, Any],
) -> None:
    """Replay the CLI-native execution that produced one exact G3 review."""

    from .review_invocations import (
        AIReviewInvocationError,
        assert_native_proof_fields,
        validate_recorded_ai_review,
    )

    validate_g3_ai_gate_review(review, pending_gate=pending_gate)
    reviewer_id = str(review["reviewer_id"])
    receipt = _object(review["invocation_receipt"], "G3 invocation receipt")
    anchor = G3_AI_GATE_INVOCATION_RECEIPT_PATHS[reviewer_id]
    bindings = {
        "pending_gate_path": G3_PENDING_GATE_PATH.as_posix(),
        "pending_gate_artifact_sha256": str(pending_gate["artifact_sha256"]),
        "technical_record_sha256": str(pending_gate["technical_record_sha256"]),
    }
    try:
        evidence = validate_recorded_ai_review(
            root,
            anchor_path=anchor,
            reviewer_id=reviewer_id,
            review_kind="g3-gate",
            target_bindings=bindings,
            expected_response={
                "verdict": review["verdict"],
                "blocking_findings": review["blocking_findings"],
            },
            invocation_id=str(receipt["invocation_id"]),
            completed_at=str(receipt["completed_at"]),
        )
        assert_native_proof_fields(
            anchor_path=anchor, receipt=receipt, evidence=evidence
        )
    except AIReviewInvocationError as exc:
        raise G3GateError("G3 AI review lacks valid native execution proof") from exc


def validate_g3_gate_reviews(
    ai_gate_reviews: Sequence[Mapping[str, Any]],
    *,
    pending_gate: Mapping[str, Any],
) -> None:
    """Require the two separate fixed-profile AI final gate reviews."""

    if isinstance(ai_gate_reviews, (str, bytes)) or not isinstance(
        ai_gate_reviews, Sequence
    ):
        raise G3GateError("G3 final gate reviews must be a sequence")
    reviews = list(ai_gate_reviews)
    if len(reviews) != len(_G3_AI_GATE_REVIEWERS):
        raise G3GateError("G3 requires exactly two independent AI final gate reviews")
    for review in reviews:
        if not isinstance(review, Mapping):
            raise G3GateError("G3 AI gate review must be an object")
        validate_g3_ai_gate_review(review, pending_gate=pending_gate)
    if [review.get("reviewer_id") for review in reviews] != list(_G3_AI_GATE_REVIEWERS):
        raise G3GateError("G3 AI gate-review order or identity changed")


def _review_hashes(ai_gate_reviews: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    return {
        str(review["reviewer_id"]): str(review["artifact_sha256"])
        for review in ai_gate_reviews
    }


def build_g3_kevin_decision(
    pending_gate: Mapping[str, Any],
    ai_gate_reviews: Sequence[Mapping[str, Any]],
    *,
    decision: str,
    selected_policy: str | None,
    decided_at: str,
) -> dict[str, Any]:
    """Build Kevin's decision only after both exact AI reviews pass."""

    validate_g3_pending_gate(pending_gate)
    validate_g3_gate_reviews(ai_gate_reviews, pending_gate=pending_gate)
    if any(review.get("verdict") != "pass" for review in ai_gate_reviews):
        raise G3GateError("Kevin cannot decide before both AI gate reviews pass")
    if decision not in {"promote", "retain-simple"}:
        raise G3GateError("Kevin's G3 decision must be promote or retain-simple")
    if decision == "promote":
        if selected_policy not in pending_gate["eligible_policies"]:
            raise G3GateError(
                "Kevin selected a policy that did not pass the full G3 gate"
            )
    elif selected_policy is not None:
        raise G3GateError("a retain-simple decision cannot select a memory policy")
    if not _valid_utc_second(decided_at):
        raise G3GateError("Kevin's G3 decision time must be a valid UTC second")
    payload: dict[str, Any] = {
        "schema_version": G3_KEVIN_DECISION_SCHEMA,
        "technical_record_sha256": pending_gate["technical_record_sha256"],
        "pending_gate_artifact_sha256": pending_gate["artifact_sha256"],
        "ai_gate_review_sha256s": _review_hashes(ai_gate_reviews),
        "reviewer": "Kevin Araujo",
        "reviewer_role": "sole_human_reviewer",
        "decision": decision,
        "selected_policy": selected_policy,
        "decided_at": decided_at,
    }
    payload["artifact_sha256"] = sha256_json(payload)
    validate_g3_kevin_decision(
        payload,
        pending_gate=pending_gate,
        ai_gate_reviews=ai_gate_reviews,
    )
    return payload


def validate_g3_kevin_decision(
    value: Mapping[str, Any],
    *,
    pending_gate: Mapping[str, Any],
    ai_gate_reviews: Sequence[Mapping[str, Any]],
) -> None:
    """Require Kevin's exact decision after both current AI reviews pass."""

    validate_g3_pending_gate(pending_gate)
    validate_g3_gate_reviews(ai_gate_reviews, pending_gate=pending_gate)
    decision = _object(value, "Kevin G3 decision")
    if set(decision) != {
        "schema_version",
        "technical_record_sha256",
        "pending_gate_artifact_sha256",
        "ai_gate_review_sha256s",
        "reviewer",
        "reviewer_role",
        "decision",
        "selected_policy",
        "decided_at",
        "artifact_sha256",
    }:
        raise G3GateError("Kevin G3 decision fields differ")
    _hash_checked(
        decision,
        schema=G3_KEVIN_DECISION_SCHEMA,
        hash_field="artifact_sha256",
        label="Kevin G3 decision",
    )
    if (
        any(review.get("verdict") != "pass" for review in ai_gate_reviews)
        or decision.get("technical_record_sha256")
        != pending_gate.get("technical_record_sha256")
        or decision.get("pending_gate_artifact_sha256")
        != pending_gate.get("artifact_sha256")
        or decision.get("ai_gate_review_sha256s") != _review_hashes(ai_gate_reviews)
        or decision.get("reviewer") != "Kevin Araujo"
        or decision.get("reviewer_role") != "sole_human_reviewer"
        or not _valid_utc_second(decision.get("decided_at"))
    ):
        raise G3GateError("Kevin G3 decision is forged, stale, or premature")
    action = decision.get("decision")
    selected = decision.get("selected_policy")
    if action == "promote":
        if selected not in pending_gate["eligible_policies"]:
            raise G3GateError(
                "Kevin selected a policy that did not pass the full G3 gate"
            )
    elif action == "retain-simple":
        if selected is not None:
            raise G3GateError("retain-simple cannot carry a promoted policy")
    else:
        raise G3GateError("Kevin G3 decision is unsupported")


def finalize_g3_gate(
    pending_gate: Mapping[str, Any],
    *,
    ai_gate_reviews: Sequence[Mapping[str, Any]],
    kevin_decision: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach both AI reviews, then optionally Kevin's separate final decision."""

    validate_g3_pending_gate(pending_gate)
    validate_g3_gate_reviews(ai_gate_reviews, pending_gate=pending_gate)
    reviews = [dict(review) for review in ai_gate_reviews]
    reviews_passed = all(review["verdict"] == "pass" for review in reviews)
    if kevin_decision is None:
        human: dict[str, Any] = {
            "status": "pending",
            "reviewer": "Kevin Araujo",
            "reason": (
                "a separate explicit Kevin decision is required"
                if reviews_passed
                else "one or more AI final gate reviews failed"
            ),
        }
        final_decision = "blocked"
        promoted_policy = None
    else:
        if not reviews_passed:
            raise G3GateError("Kevin cannot decide after a failed AI gate review")
        validate_g3_kevin_decision(
            kevin_decision,
            pending_gate=pending_gate,
            ai_gate_reviews=reviews,
        )
        human = {"status": "recorded", "decision_record": dict(kevin_decision)}
        if kevin_decision["decision"] == "promote":
            final_decision = "promote"
            promoted_policy = kevin_decision["selected_policy"]
        else:
            final_decision = "retain-simple"
            promoted_policy = None
    final: dict[str, Any] = {
        **_technical_part(pending_gate),
        "technical_record_sha256": pending_gate["technical_record_sha256"],
        "pending_gate_artifact_sha256": pending_gate["artifact_sha256"],
        "ai_gate_reviews": reviews,
        "gate_review_status": "passed" if reviews_passed else "failed",
        "human_decision": human,
        "promoted_memory_policy": promoted_policy,
        "final_decision": final_decision,
    }
    final["artifact_sha256"] = sha256_json(final)
    _validate_finalized_g3_gate(final, pending_gate=pending_gate)
    return final


def _validate_finalized_g3_gate(
    value: Mapping[str, Any],
    *,
    pending_gate: Mapping[str, Any],
    g1_gate: Mapping[str, Any] | None = None,
    g1_approval: Mapping[str, Any] | None = None,
) -> None:
    validate_g3_pending_gate(
        pending_gate,
        g1_gate=g1_gate,
        g1_approval=g1_approval,
    )
    gate = _object(value, "finalized G3 gate")
    _technical_part(gate)
    if gate.get("artifact_sha256") != sha256_json(
        {key: item for key, item in gate.items() if key != "artifact_sha256"}
    ):
        raise G3GateError("G3 final gate artifact hash mismatch")
    if (
        {key: gate[key] for key in _G3_TECHNICAL_FIELDS}
        != {key: pending_gate[key] for key in _G3_TECHNICAL_FIELDS}
        or gate.get("technical_record_sha256")
        != pending_gate.get("technical_record_sha256")
        or gate.get("pending_gate_artifact_sha256")
        != pending_gate.get("artifact_sha256")
    ):
        raise G3GateError("G3 final gate is bound to a stale pending record")
    reviews = gate.get("ai_gate_reviews")
    if not isinstance(reviews, list):
        raise G3GateError("G3 final gate reviews must be a list")
    validate_g3_gate_reviews(reviews, pending_gate=pending_gate)
    reviews_passed = all(review["verdict"] == "pass" for review in reviews)
    expected_review_status = "passed" if reviews_passed else "failed"
    if gate.get("gate_review_status") != expected_review_status:
        raise G3GateError("G3 final gate review status is invalid")
    human = _object(gate.get("human_decision"), "G3 human decision")
    if human.get("status") == "recorded":
        decision = _object(human.get("decision_record"), "Kevin G3 decision")
        validate_g3_kevin_decision(
            decision,
            pending_gate=pending_gate,
            ai_gate_reviews=reviews,
        )
        expected_final = (
            "promote" if decision["decision"] == "promote" else "retain-simple"
        )
        expected_policy = (
            decision["selected_policy"] if expected_final == "promote" else None
        )
        if (
            not reviews_passed
            or gate.get("final_decision") != expected_final
            or gate.get("promoted_memory_policy") != expected_policy
        ):
            raise G3GateError("Kevin G3 final decision is inconsistent")
    elif human.get("status") == "pending":
        expected_reason = (
            "a separate explicit Kevin decision is required"
            if reviews_passed
            else "one or more AI final gate reviews failed"
        )
        if (
            human
            != {
                "status": "pending",
                "reviewer": "Kevin Araujo",
                "reason": expected_reason,
            }
            or gate.get("final_decision") != "blocked"
            or gate.get("promoted_memory_policy") is not None
        ):
            raise G3GateError("pending Kevin G3 decision is invalid")
    else:
        raise G3GateError("G3 human decision status is invalid")


def build_g3_final_gate(
    *,
    g1_gate: Mapping[str, Any] | None = None,
    g3_freeze: Mapping[str, Any],
    public_run: Mapping[str, Any],
    public_receipts: Sequence[Mapping[str, Any]],
    public_metrics: Mapping[str, Any],
    sealed_candidate_manifest: Mapping[str, Any],
    sealed_import: Mapping[str, Any],
    lifecycle_evidence: Mapping[str, Any],
    panel_calibration: Mapping[str, Any],
    failure_report: Mapping[str, Any],
    unsupported_memory_review: Mapping[str, Any],
    unsupported_memory_review_approval: Mapping[str, Any],
    root: Path | None = None,
    ai_gate_reviews: Sequence[Mapping[str, Any]] = (),
    kevin_decision: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the pending record, or finalize it from both AI reviews and Kevin."""

    pending = build_g3_pending_gate(
        g1_gate=g1_gate,
        g3_freeze=g3_freeze,
        public_run=public_run,
        public_receipts=public_receipts,
        public_metrics=public_metrics,
        sealed_candidate_manifest=sealed_candidate_manifest,
        sealed_import=sealed_import,
        lifecycle_evidence=lifecycle_evidence,
        panel_calibration=panel_calibration,
        failure_report=failure_report,
        unsupported_memory_review=unsupported_memory_review,
        unsupported_memory_review_approval=unsupported_memory_review_approval,
        root=root,
    )
    if not ai_gate_reviews:
        if kevin_decision is not None:
            raise G3GateError("Kevin cannot decide before both AI gate reviews exist")
        return pending
    return finalize_g3_gate(
        pending,
        ai_gate_reviews=ai_gate_reviews,
        kevin_decision=kevin_decision,
    )


def validate_g3_final_gate(
    value: Mapping[str, Any],
    *,
    g1_gate: Mapping[str, Any] | None = None,
    g3_freeze: Mapping[str, Any],
    public_run: Mapping[str, Any],
    public_receipts: Sequence[Mapping[str, Any]],
    public_metrics: Mapping[str, Any],
    sealed_candidate_manifest: Mapping[str, Any],
    sealed_import: Mapping[str, Any],
    lifecycle_evidence: Mapping[str, Any],
    panel_calibration: Mapping[str, Any],
    failure_report: Mapping[str, Any],
    unsupported_memory_review: Mapping[str, Any],
    unsupported_memory_review_approval: Mapping[str, Any],
    root: Path | None = None,
) -> None:
    """Rebuild a G3 record from its exact sources and require total equality."""

    gate = _object(value, "G3 final gate")
    if gate.get("schema_version") != G3_GATE_SCHEMA:
        raise G3GateError("unsupported G3 final gate schema")
    root_value = (root or repository_root()).resolve()
    current_g1, _ = _canonical_g1_gate(g1_gate, root=root_value)
    pending = build_g3_pending_gate(
        g1_gate=current_g1,
        g3_freeze=g3_freeze,
        public_run=public_run,
        public_receipts=public_receipts,
        public_metrics=public_metrics,
        sealed_candidate_manifest=sealed_candidate_manifest,
        sealed_import=sealed_import,
        lifecycle_evidence=lifecycle_evidence,
        panel_calibration=panel_calibration,
        failure_report=failure_report,
        unsupported_memory_review=unsupported_memory_review,
        unsupported_memory_review_approval=unsupported_memory_review_approval,
        root=root_value,
    )
    reviews = gate.get("ai_gate_reviews")
    human = gate.get("human_decision")
    embedded_decision: Mapping[str, Any] | None = None
    if isinstance(human, Mapping) and human.get("status") == "recorded":
        embedded_decision = _object(
            human.get("decision_record"), "embedded Kevin G3 decision"
        )
    if reviews == []:
        expected = pending
    elif isinstance(reviews, list):
        expected = finalize_g3_gate(
            pending,
            ai_gate_reviews=reviews,
            kevin_decision=embedded_decision,
        )
    else:
        raise G3GateError("G3 final gate reviews must be a list")
    if dict(gate) != expected:
        raise G3GateError("G3 final gate differs from its exact evidence")


# Short aliases keep integration call sites clear while preserving the explicit
# final-gate names above.
build_g3_gate = build_g3_final_gate
validate_g3_gate = validate_g3_final_gate
