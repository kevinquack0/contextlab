"""Deterministic, content-free failure review for the frozen G2 study.

The review is derived only from repository-public evidence and the narrow G2
sealed import.  It never opens an external sealed bundle or evaluator work
directory, and it never copies public question or generated-answer text into
the review artifact.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from .answer_metrics import ANSWER_METRICS_SCHEMA
from .experiments import LAB_SCHEMA, METHOD_IDS, load_protocol
from .g2_gate import G2_GATE_SCHEMA, build_g2_final_gate
from .g2_sealed import (
    G2_COMPONENT_CELL_COUNT,
    G2_SEALED_IMPORT_SCHEMA,
    G2_SEALED_RETURN_SCHEMA,
    G2_SEALED_TASK_IDS,
    validate_g2_sealed_return,
)
from .provider import ALLOWED_REASONING_EFFORTS, MODEL_ID
from .generations import generation_manifest_path
from .repeats import REPEAT_ANALYSIS_SCHEMA, REPEAT_CELL_COUNT, REPEAT_TRIAL_COUNT
from .reports import ANALYSIS_SCHEMA, METRIC_ALIASES, PARENT_METHOD, validate_lab
from .static_benchmark import public_static_tasks, validate_static_freeze
from .statistics import distribution_summary, paired_bootstrap_ci
from .tasking import prompt_safe_task, sha256_json


G2_FAILURE_REVIEW_SCHEMA = "contextlab.g2-failure-trace-review.v1"
TRACE_REFERENCE_LIMIT = 3
FAILED_METHOD_IDS = ("R1", "R2", "R3", "R5", "R6", "R7")

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PROTECTED_SEALED_FIELD = re.compile(r"(?:question|gold|answer|trace)", re.I)
_SAFE_SEALED_TRACE_FIELD = "trace_commitment_sha256"
_PUBLIC_CRITERIA = frozenset(
    {
        "target_delta_meets_minimum",
        "target_ci_supports_direction",
        "full_set_not_materially_regressed",
        "latency_within_budget",
        "question_reference_leakage_passed",
        "identifier_mask_check_passed",
        "retrieval_cost_is_zero",
    }
)
_GATE_NONTECHNICAL_FIELDS = frozenset(
    {"technical_record_sha256", "human_approval", "final_decision", "artifact_sha256"}
)


class G2FailureReviewError(ValueError):
    """G2 failure evidence is missing, unsafe, stale, or internally inconsistent."""


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise G2FailureReviewError(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise G2FailureReviewError(f"{label} must be a list")
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise G2FailureReviewError(f"{label} must be a lowercase SHA-256")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise G2FailureReviewError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise G2FailureReviewError(f"{label} must be finite")
    return result


def _same_number(actual: Any, expected: float, label: str) -> None:
    value = _number(actual, label)
    if not math.isclose(value, expected, rel_tol=0.0, abs_tol=1e-12):
        raise G2FailureReviewError(f"{label} differs from task-level evidence")


def _hash_checked(
    value: Mapping[str, Any], *, schema: str, field: str, label: str
) -> None:
    if value.get("schema_version") != schema:
        raise G2FailureReviewError(f"unsupported {label} schema")
    expected = sha256_json({key: item for key, item in value.items() if key != field})
    if value.get(field) != expected:
        raise G2FailureReviewError(f"{label} hash mismatch")


def _validate_gate(gate: Mapping[str, Any]) -> None:
    _hash_checked(
        gate,
        schema=G2_GATE_SCHEMA,
        field="artifact_sha256",
        label="technical G2 gate",
    )
    technical = {
        key: item for key, item in gate.items() if key not in _GATE_NONTECHNICAL_FIELDS
    }
    if gate.get("technical_record_sha256") != sha256_json(technical):
        raise G2FailureReviewError("technical G2 gate record hash mismatch")
    approval = _object(gate.get("human_approval"), "technical G2 gate approval state")
    approval_status = approval.get("status")
    if approval_status not in {"pending", "approved"}:
        raise G2FailureReviewError("technical G2 gate approval state is invalid")
    if approval_status == "pending":
        if (
            set(approval) != {"status", "reviewer", "reason"}
            or approval.get("reviewer") != "Kevin Araujo"
            or gate.get("final_decision") != "blocked"
        ):
            raise G2FailureReviewError("pending G2 gate must remain blocked")
    else:
        record = _object(approval.get("approval_record"), "G2 approval record")
        approved_at = record.get("approved_at")
        try:
            valid_approved_at = (
                isinstance(approved_at, str)
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", approved_at)
                is not None
                and bool(dt.datetime.strptime(approved_at, "%Y-%m-%dT%H:%M:%SZ"))
            )
        except ValueError:
            valid_approved_at = False
        if (
            set(approval) != {"status", "reviewer", "approval_record"}
            or set(record)
            != {
                "schema_version",
                "gate_sha256",
                "reviewer",
                "reviewer_role",
                "decision",
                "approved_at",
            }
            or approval.get("reviewer") != "Kevin Araujo"
            or record.get("schema_version") != "contextlab.g2-human-approval.v1"
            or record.get("gate_sha256") != gate.get("technical_record_sha256")
            or record.get("reviewer") != "Kevin Araujo"
            or record.get("reviewer_role") != "human_reviewer"
            or record.get("decision") != "approved"
            or not valid_approved_at
            or gate.get("final_decision") != "retain-simple"
        ):
            raise G2FailureReviewError("approved retain-simple gate is invalid")


def _technical_subset(gate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: item for key, item in gate.items() if key not in _GATE_NONTECHNICAL_FIELDS
    }


def _read_canonical_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise G2FailureReviewError(f"cannot read canonical {label}") from exc
    if not isinstance(value, dict):
        raise G2FailureReviewError(f"canonical {label} is not an object")
    return value


def _canonical_anchor(
    *,
    root: Path,
    component_lab: Mapping[str, Any],
    component_analysis: Mapping[str, Any],
    public_answer_metrics: Mapping[str, Any],
    repeat_analysis: Mapping[str, Any],
    sealed_import: Mapping[str, Any],
    technical_gate: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Bind supplied evidence to the repository and a fresh ledger-audited gate."""

    root = root.resolve()
    try:
        protocol = load_protocol(root)
        freeze = _read_canonical_json(
            root / "results/v2/splits/static_g2_freeze.json", "static freeze"
        )
        validate_static_freeze(freeze, root)
        tasks = public_static_tasks(root)
    except Exception as exc:
        if isinstance(exc, G2FailureReviewError):
            raise
        raise G2FailureReviewError(f"canonical G2 freeze is invalid: {exc}") from exc
    if len(tasks) != 84 or len({str(task.get("task_id")) for task in tasks}) != 84:
        raise G2FailureReviewError("canonical G2 public task grid is not exactly 84")
    canonical = {
        "component_lab": _read_canonical_json(
            root / "results/v2/retrieval/public_component_lab.json",
            "public component lab",
        ),
        "component_analysis": _read_canonical_json(
            root / "results/v2/reports/g2_public_component_analysis.json",
            "public component analysis",
        ),
        "public_answer_metrics": _read_canonical_json(
            root / "results/v2/reports/g2_public_answer_metrics.json",
            "public answer metrics",
        ),
        "repeat_analysis": _read_canonical_json(
            root / "results/v2/reports/g2_public_repeats.json", "repeat analysis"
        ),
        "sealed_import": _read_canonical_json(
            root / "results/v2/sealed/g2-import.json", "safe sealed import"
        ),
    }
    supplied = {
        "component_lab": component_lab,
        "component_analysis": component_analysis,
        "public_answer_metrics": public_answer_metrics,
        "repeat_analysis": repeat_analysis,
        "sealed_import": sealed_import,
    }
    for name, expected in canonical.items():
        if dict(supplied[name]) != expected:
            raise G2FailureReviewError(
                f"supplied {name.replace('_', ' ')} differs from canonical evidence"
            )
    campaign = protocol.get("fixed_comparison", {}).get("generation_campaign_id")
    if campaign != "g2r2":
        raise G2FailureReviewError("canonical protocol does not freeze g2r2")
    manifests = [
        _read_canonical_json(
            generation_manifest_path(root, trial, str(campaign)),
            f"generation manifest {trial}",
        )
        for trial in range(1, REPEAT_TRIAL_COUNT + 1)
    ]
    try:
        pending_gate = build_g2_final_gate(
            protocol=protocol,
            static_freeze=freeze,
            component_lab=canonical["component_lab"],
            component_analysis=canonical["component_analysis"],
            public_answer_metrics=canonical["public_answer_metrics"],
            repeat_analysis=canonical["repeat_analysis"],
            generation_manifests=manifests,
            sealed_import=canonical["sealed_import"],
            public_tasks=tasks,
            root=root,
            human_approval=None,
        )
    except Exception as exc:
        raise G2FailureReviewError(
            f"fresh canonical pending G2 gate reconstruction failed: {exc}"
        ) from exc
    pending_technical = _technical_subset(pending_gate)
    supplied_technical = _technical_subset(technical_gate)
    pending_sha = pending_gate.get("technical_record_sha256")
    if (
        supplied_technical != pending_technical
        or technical_gate.get("technical_record_sha256") != pending_sha
        or pending_sha != sha256_json(pending_technical)
    ):
        raise G2FailureReviewError(
            "supplied G2 gate technical record differs from fresh canonical evidence"
        )
    return protocol, freeze, tasks


def _validate_safe_sealed_import(sealed: Mapping[str, Any]) -> None:
    allowed = {
        "schema_version",
        "static_freeze_manifest_sha256",
        "external_bundle_sha256",
        "retrieval_protocol_sha256",
        "source_return_sha256",
        "component_records",
        "generation_summary",
    }
    required = allowed - {"generation_summary"}
    if set(sealed) - allowed or not required <= set(sealed):
        raise G2FailureReviewError("safe sealed import fields differ from its schema")
    if sealed.get("schema_version") != G2_SEALED_IMPORT_SCHEMA:
        raise G2FailureReviewError("unsupported safe sealed import schema")

    def visit(value: Any, field: str = "") -> None:
        if (
            _PROTECTED_SEALED_FIELD.search(field)
            and field != _SAFE_SEALED_TRACE_FIELD
        ):
            raise G2FailureReviewError(
                "safe sealed import contains a protected content field"
            )
        if isinstance(value, Mapping):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise G2FailureReviewError(
                        "safe sealed import contains a non-string field"
                    )
                visit(item, key)
        elif isinstance(value, list):
            for item in value:
                visit(item, field)

    visit(sealed)
    freeze_sha = _sha(
        sealed.get("static_freeze_manifest_sha256"), "sealed static-freeze hash"
    )
    external_sha = _sha(
        sealed.get("external_bundle_sha256"), "sealed external-bundle hash"
    )
    protocol_sha = _sha(
        sealed.get("retrieval_protocol_sha256"), "sealed protocol hash"
    )
    _sha(sealed.get("source_return_sha256"), "sealed source-return hash")
    safe_return: dict[str, Any] = {
        "schema_version": G2_SEALED_RETURN_SCHEMA,
        "static_freeze_manifest_sha256": freeze_sha,
        "external_bundle_sha256": external_sha,
        "retrieval_protocol_sha256": protocol_sha,
        "component_records": sealed.get("component_records"),
    }
    if "generation_summary" in sealed:
        safe_return["generation_summary"] = sealed["generation_summary"]
    try:
        validate_g2_sealed_return(
            safe_return,
            static_freeze_manifest_sha256=freeze_sha,
            external_bundle_sha256=external_sha,
            retrieval_protocol_sha256=protocol_sha,
        )
    except Exception as exc:
        raise G2FailureReviewError(f"safe sealed import is invalid: {exc}") from exc


def _trace_index(
    lab: Mapping[str, Any],
    public_tasks: Sequence[Mapping[str, Any]],
    static_freeze: Mapping[str, Any],
) -> tuple[dict[tuple[str, str], Mapping[str, Any]], tuple[str, ...]]:
    try:
        validate_lab(lab)
    except Exception as exc:
        raise G2FailureReviewError(f"public component lab is invalid: {exc}") from exc
    traces = _list(lab.get("traces"), "public component traces")
    if (
        lab.get("task_count") != 84
        or lab.get("cell_count") != 84 * len(METHOD_IDS)
        or len(traces) != 84 * len(METHOD_IDS)
        or len(public_tasks) != 84
    ):
        raise G2FailureReviewError("public component lab must be the 84 by 8 grid")
    canonical_tasks = {str(task.get("task_id")): task for task in public_tasks}
    if len(canonical_tasks) != 84:
        raise G2FailureReviewError("canonical public tasks contain duplicate IDs")
    frozen_rows = {
        str(row.get("task_id")): row
        for row in _list(static_freeze.get("tasks"), "static freeze tasks")
        if isinstance(row, Mapping)
    }
    for task_id, task in canonical_tasks.items():
        frozen = frozen_rows.get(task_id)
        if (
            frozen is None
            or frozen.get("question_sha256") != task.get("question_sha256")
        ):
            raise G2FailureReviewError(
                "canonical public task question commitment differs from static freeze"
            )
    index: dict[tuple[str, str], Mapping[str, Any]] = {}
    run_ids: set[str] = set()
    task_ids: set[str] = set()
    for number, raw_trace in enumerate(traces):
        trace = _object(raw_trace, f"public component trace {number}")
        task = _object(trace.get("task"), f"public component trace {number} task")
        task_id = task.get("task_id")
        method_id = trace.get("strategy_id")
        run_id = trace.get("run_id")
        metrics = trace.get("component_metrics")
        if (
            not isinstance(task_id, str)
            or not task_id
            or method_id not in METHOD_IDS
            or not isinstance(run_id, str)
            or not run_id
            or not isinstance(metrics, Mapping)
        ):
            raise G2FailureReviewError("public component trace identity is invalid")
        canonical_task = canonical_tasks.get(task_id)
        if canonical_task is None or dict(task) != prompt_safe_task(dict(canonical_task)):
            raise G2FailureReviewError(
                "public component trace question commitment is not canonical"
            )
        key = (task_id, str(method_id))
        if key in index or run_id in run_ids:
            raise G2FailureReviewError("public component trace identity is duplicated")
        index[key] = trace
        task_ids.add(task_id)
        run_ids.add(run_id)
    expected = {
        (task_id, method_id)
        for task_id in canonical_tasks
        for method_id in METHOD_IDS
    }
    if (
        set(index) != expected
        or task_ids != set(canonical_tasks)
        or len(index) != 672
    ):
        raise G2FailureReviewError("public component lab grid is incomplete")
    return index, tuple(sorted(canonical_tasks))


def _task_families(
    metrics: Mapping[str, Any], public_tasks: Sequence[Mapping[str, Any]]
) -> dict[str, str]:
    canonical_families = {
        str(task.get("task_id")): str(task.get("task_family"))
        for task in public_tasks
    }
    task_ids = tuple(sorted(canonical_families))
    if len(canonical_families) != 84 or any(
        not family for family in canonical_families.values()
    ):
        raise G2FailureReviewError("canonical public task families are incomplete")
    rows = _list(metrics.get("rows"), "public answer metric rows")
    expected = {
        (task_id, method_id, effort)
        for task_id in task_ids
        for method_id in METHOD_IDS
        for effort in ALLOWED_REASONING_EFFORTS
    }
    observed: set[tuple[str, str, str]] = set()
    run_ids: set[str] = set()
    families: dict[str, str] = {}
    for number, raw_row in enumerate(rows):
        row = _object(raw_row, f"public answer metric row {number}")
        task_id = row.get("task_id")
        method_id = row.get("strategy_id")
        effort = row.get("reasoning_effort")
        family = row.get("task_family")
        run_id = row.get("run_id")
        if (
            not isinstance(task_id, str)
            or method_id not in METHOD_IDS
            or effort not in ALLOWED_REASONING_EFFORTS
            or not isinstance(family, str)
            or not family
            or not isinstance(run_id, str)
            or not run_id
        ):
            raise G2FailureReviewError("public answer metric identity is invalid")
        key = (task_id, str(method_id), str(effort))
        if key in observed or run_id in run_ids or key not in expected:
            raise G2FailureReviewError("public answer metric identity is duplicated")
        previous = families.setdefault(task_id, family)
        if previous != family or canonical_families.get(task_id) != family:
            raise G2FailureReviewError("public task family changes between answer rows")
        observed.add(key)
        run_ids.add(run_id)
    if observed != expected or set(families) != set(task_ids):
        raise G2FailureReviewError("public answer metrics do not cover the frozen grid")
    if metrics.get("completed_cell_count") != len(expected):
        raise G2FailureReviewError("public answer metric completion count is stale")
    return families


def _validate_repeat_analysis(repeats: Mapping[str, Any]) -> list[str]:
    if (
        repeats.get("scope")
        != "public_temperature_zero_deterministic_screening"
        or repeats.get("trial_count") != REPEAT_TRIAL_COUNT
        or repeats.get("expected_cell_count_per_trial") != REPEAT_CELL_COUNT
    ):
        raise G2FailureReviewError("repeat analysis does not match the frozen run")
    manifests = _list(repeats.get("generation_manifests"), "repeat manifest bindings")
    manifest_hashes: list[str] = []
    for trial, raw in enumerate(manifests, start=1):
        row = _object(raw, f"repeat manifest binding {trial}")
        if set(row) != {"trial", "manifest_sha256"} or row.get("trial") != trial:
            raise G2FailureReviewError("repeat manifest binding is invalid")
        manifest_hashes.append(
            _sha(row.get("manifest_sha256"), f"repeat manifest {trial} hash")
        )
    if len(manifest_hashes) != REPEAT_TRIAL_COUNT:
        raise G2FailureReviewError("repeat analysis does not bind all five trials")
    aggregate = _object(
        repeats.get("aggregate_consistency"), "repeat consistency aggregate"
    )
    summary = _object(aggregate.get("repeat_summary"), "repeat consistency summary")
    if (
        aggregate.get("cell_count") != REPEAT_CELL_COUNT
        or summary.get("trial_count") != REPEAT_TRIAL_COUNT
    ):
        raise G2FailureReviewError("repeat consistency evidence is incomplete")
    return manifest_hashes


def _validate_source_bindings(
    *,
    root: Path,
    component_lab: Mapping[str, Any],
    component_analysis: Mapping[str, Any],
    public_answer_metrics: Mapping[str, Any],
    repeat_analysis: Mapping[str, Any],
    sealed_import: Mapping[str, Any],
    technical_gate: Mapping[str, Any],
) -> tuple[
    dict[tuple[str, str], Mapping[str, Any]],
    tuple[str, ...],
    dict[str, str],
    list[str],
    Mapping[str, Any],
]:
    _hash_checked(
        component_lab,
        schema=LAB_SCHEMA,
        field="artifact_sha256",
        label="public component lab",
    )
    _hash_checked(
        component_analysis,
        schema=ANALYSIS_SCHEMA,
        field="analysis_sha256",
        label="public component analysis",
    )
    _hash_checked(
        public_answer_metrics,
        schema=ANSWER_METRICS_SCHEMA,
        field="artifact_sha256",
        label="public answer metrics",
    )
    _hash_checked(
        repeat_analysis,
        schema=REPEAT_ANALYSIS_SCHEMA,
        field="analysis_sha256",
        label="repeat analysis",
    )
    _validate_safe_sealed_import(sealed_import)
    _validate_gate(technical_gate)
    protocol, static_freeze, canonical_tasks = _canonical_anchor(
        root=root,
        component_lab=component_lab,
        component_analysis=component_analysis,
        public_answer_metrics=public_answer_metrics,
        repeat_analysis=repeat_analysis,
        sealed_import=sealed_import,
        technical_gate=technical_gate,
    )

    lab_sha = _sha(component_lab.get("artifact_sha256"), "component lab hash")
    protocol_sha = _sha(
        component_analysis.get("protocol_sha256"), "component analysis protocol hash"
    )
    if (
        component_lab.get("protocol_sha256") != protocol_sha
        or component_analysis.get("component_lab_sha256") != lab_sha
        or public_answer_metrics.get("component_lab_sha256") != lab_sha
        or repeat_analysis.get("component_lab_sha256") != lab_sha
        or technical_gate.get("component_lab_sha256") != lab_sha
    ):
        raise G2FailureReviewError("a source has a stale component-lab binding")
    if (
        public_answer_metrics.get("generation_protocol_sha256") != protocol_sha
        or repeat_analysis.get("protocol_sha256") != protocol_sha
        or sealed_import.get("retrieval_protocol_sha256") != protocol_sha
        or technical_gate.get("protocol_sha256") != protocol_sha
    ):
        raise G2FailureReviewError("a source has a stale retrieval-protocol binding")
    if (
        component_analysis.get("scope") != "public_component_evidence_only"
        or public_answer_metrics.get("scope") != "public_deterministic_screening"
        or component_analysis.get("task_count") != component_lab.get("task_count")
        or component_analysis.get("cell_count") != component_lab.get("cell_count")
    ):
        raise G2FailureReviewError("a public source has stale scope or coverage")
    campaign = public_answer_metrics.get("generation_campaign_id")
    if (
        campaign != "g2r2"
        or technical_gate.get("generation_campaign_id") != campaign
        or technical_gate.get("requested_model") != MODEL_ID
        or technical_gate.get("output_token_limit") != 8192
        or public_answer_metrics.get("output_token_limit") != 8192
    ):
        raise G2FailureReviewError("G2 campaign, model, or output limit is not frozen")
    if (
        technical_gate.get("static_freeze_manifest_sha256")
        != sealed_import.get("static_freeze_manifest_sha256")
        or technical_gate.get("external_bundle_sha256")
        != sealed_import.get("external_bundle_sha256")
        or technical_gate.get("sealed_source_return_sha256")
        != sealed_import.get("source_return_sha256")
    ):
        raise G2FailureReviewError("technical gate has a stale safe-sealed binding")

    trace_index, task_ids = _trace_index(
        component_lab, canonical_tasks, static_freeze
    )
    families = _task_families(public_answer_metrics, canonical_tasks)
    manifest_hashes = _validate_repeat_analysis(repeat_analysis)
    gate_manifests = technical_gate.get("generation_manifest_sha256s")
    if gate_manifests != manifest_hashes:
        raise G2FailureReviewError("technical gate has stale generation manifests")
    if public_answer_metrics.get("generation_manifest_sha256") != manifest_hashes[0]:
        raise G2FailureReviewError("answer metrics do not bind repeat trial one")

    methods = _object(component_analysis.get("methods"), "component analysis methods")
    if set(methods) != set(METHOD_IDS):
        raise G2FailureReviewError("component analysis does not define R0 through R7")
    candidates = component_analysis.get("public_component_candidates")
    if candidates != ["R4"]:
        raise G2FailureReviewError("frozen public evidence must name only R4 as candidate")
    for method_id in FAILED_METHOD_IDS:
        row = _object(methods.get(method_id), f"component analysis {method_id}")
        if row.get("status") != "public_failed":
            raise G2FailureReviewError(f"{method_id} is missing from public failures")
    r4 = _object(methods.get("R4"), "component analysis R4")
    if r4.get("status") != "public_passed":
        raise G2FailureReviewError("R4 is not the frozen public candidate")
    failure_refs = _object(
        technical_gate.get("failure_trace_review_references"),
        "technical gate failure-review references",
    )
    if failure_refs.get("methods_requiring_review") != list(METHOD_IDS[1:]):
        raise G2FailureReviewError("technical gate failure-method list is stale")
    if (
        technical_gate.get("technical_decision") != "retain-simple"
        or technical_gate.get("technical_promotion_ready") is not False
        or technical_gate.get("promoted_retriever_id") is not None
        or technical_gate.get("retained_retriever_id") != "R0"
    ):
        raise G2FailureReviewError("technical gate does not retain the R0 control")
    stages = _object(technical_gate.get("stages"), "technical gate stages")
    if set(stages) != {
        "public_component",
        "public_generation",
        "repeat_evidence",
        "sealed_evaluation",
    }:
        raise G2FailureReviewError("technical gate stage set is invalid")
    public_stage = _object(stages.get("public_component"), "public component gate stage")
    expected_public_stage = {
        "decision": "retain-simple",
        "incremental_candidates": ["R4"],
        "promotion_eligible_candidates": [],
        "failed_ancestor_blockers": {"R4": ["R2", "R3"]},
    }
    if dict(public_stage) != expected_public_stage:
        raise G2FailureReviewError("technical gate public stage schema is stale")
    expected_public_generation = {
        "decision": "promote",
        "trace_path_bound": True,
        "completed_cell_count": public_answer_metrics["completed_cell_count"],
    }
    if stages.get("public_generation") != expected_public_generation:
        raise G2FailureReviewError("technical gate public-generation stage is stale")
    expected_repeats = {
        "decision": "promote",
        "trial_count": REPEAT_TRIAL_COUNT,
        "repeat_cell_count": REPEAT_CELL_COUNT,
    }
    if stages.get("repeat_evidence") != expected_repeats:
        raise G2FailureReviewError("technical gate repeat stage is stale")
    sealed_stage = _object(stages.get("sealed_evaluation"), "sealed gate stage")
    if (
        set(sealed_stage)
        != {
            "decision",
            "incremental_candidates",
            "promotion_eligible_candidates",
            "failed_ancestor_blockers",
        }
        or sealed_stage.get("decision") != "retain-simple"
        or sealed_stage.get("promotion_eligible_candidates") != []
        or sealed_stage.get("failed_ancestor_blockers")
        != {"R4": ["R2", "R3"]}
        or not isinstance(sealed_stage.get("incremental_candidates"), list)
        or len(sealed_stage["incremental_candidates"]) != 1
    ):
        raise G2FailureReviewError("technical gate sealed stage schema is stale")
    return trace_index, task_ids, families, manifest_hashes, protocol


def _counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {
        "task_count": len(rows),
        "improvement_count": sum(float(row["delta"]) > 0.0 for row in rows),
        "tie_count": sum(float(row["delta"]) == 0.0 for row in rows),
        "regression_count": sum(float(row["delta"]) < 0.0 for row in rows),
    }


def _trace_reference(
    row: Mapping[str, Any],
    *,
    method_id: str,
    parent_id: str,
    trace_index: Mapping[tuple[str, str], Mapping[str, Any]],
) -> dict[str, Any]:
    task_id = str(row["task_id"])
    candidate = trace_index[(task_id, method_id)]
    parent = trace_index[(task_id, parent_id)]
    return {
        "task_id": task_id,
        "task_family": row["task_family"],
        "in_target_family": row["in_target_family"],
        "delta": row["delta"],
        "candidate_trace": {
            "strategy_id": method_id,
            "run_id": candidate["run_id"],
            "trace_sha256": sha256_json(candidate),
        },
        "parent_trace": {
            "strategy_id": parent_id,
            "run_id": parent["run_id"],
            "trace_sha256": sha256_json(parent),
        },
    }


def _reason(value: str) -> str:
    # Raw RRF transitions include rank coordinates.  The coordinates are retained
    # in the source trace; this bounded review groups them as one stage decision.
    return "rrf_rank_provenance" if value.startswith("rrf:") else value


def _trace_decisions(
    method_id: str,
    task_ids: Sequence[str],
    trace_index: Mapping[tuple[str, str], Mapping[str, Any]],
) -> dict[str, Any]:
    transitions: Counter[str] = Counter()
    removals: Counter[str] = Counter()
    stages: Counter[str] = Counter()
    routes: Counter[str] = Counter()
    for task_id in task_ids:
        trace = trace_index[(task_id, method_id)]
        raw_transitions = _object(
            trace.get("transitions"), f"{method_id}/{task_id} transitions"
        )
        for candidate_id, raw_reason in raw_transitions.items():
            if not isinstance(candidate_id, str) or not isinstance(raw_reason, str):
                raise G2FailureReviewError("public trace transition is invalid")
            transitions[_reason(raw_reason)] += 1
        retrieval_stages = _object(
            trace.get("retrieval_stages"), f"{method_id}/{task_id} retrieval stages"
        )
        for stage_name, stage_rows in retrieval_stages.items():
            if not isinstance(stage_name, str):
                raise G2FailureReviewError("public trace stage name is invalid")
            if stage_rows:
                stages[stage_name] += 1
            if isinstance(stage_rows, list):
                for raw_candidate in stage_rows:
                    if isinstance(raw_candidate, Mapping):
                        removal = raw_candidate.get("removal_reason")
                        if removal is not None:
                            if not isinstance(removal, str) or not removal:
                                raise G2FailureReviewError(
                                    "public trace removal reason is invalid"
                                )
                            removals[_reason(removal)] += 1
        route = trace.get("route")
        routes[str(route) if isinstance(route, str) and route else "none"] += 1
    return {
        "transition_reason_counts": dict(sorted(transitions.items())),
        "candidate_removal_reason_counts": dict(sorted(removals.items())),
        "retrieval_stage_trace_counts": dict(sorted(stages.items())),
        "route_decision_counts": dict(sorted(routes.items())),
    }


def _method_review(
    method_id: str,
    *,
    analysis_row: Mapping[str, Any],
    component_lab: Mapping[str, Any],
    protocol: Mapping[str, Any],
    task_ids: Sequence[str],
    task_families: Mapping[str, str],
    trace_index: Mapping[tuple[str, str], Mapping[str, Any]],
) -> dict[str, Any]:
    parent_id = analysis_row.get("parent")
    if parent_id != PARENT_METHOD[method_id]:
        raise G2FailureReviewError(f"{method_id} declared parent is stale")
    metric = analysis_row.get("primary_metric")
    if not isinstance(metric, str) or not metric:
        raise G2FailureReviewError(f"{method_id} primary metric is missing")
    protocol_methods = _object(protocol.get("methods"), "canonical protocol methods")
    protocol_method = _object(
        protocol_methods.get(method_id), f"canonical protocol {method_id}"
    )
    declared_metric = protocol_method.get("primary_metric")
    if (
        not isinstance(declared_metric, str)
        or METRIC_ALIASES.get(declared_metric, declared_metric) != metric
    ):
        raise G2FailureReviewError(f"{method_id} metric differs from canonical protocol")
    raw_targets = analysis_row.get("target_families")
    if not isinstance(raw_targets, list) or not raw_targets or any(
        not isinstance(item, str) or not item for item in raw_targets
    ):
        raise G2FailureReviewError(f"{method_id} target families are invalid")
    target_families = set(raw_targets)
    if sorted(target_families) != sorted(protocol_method.get("target_families", [])):
        raise G2FailureReviewError(
            f"{method_id} target families differ from canonical protocol"
        )
    task_rows: list[dict[str, Any]] = []
    baseline_scores: dict[str, float] = {}
    candidate_scores: dict[str, float] = {}
    for task_id in task_ids:
        candidate = _object(
            trace_index[(task_id, method_id)].get("component_metrics"),
            f"{method_id}/{task_id} metrics",
        )
        parent = _object(
            trace_index[(task_id, str(parent_id))].get("component_metrics"),
            f"{parent_id}/{task_id} metrics",
        )
        if metric not in candidate or metric not in parent:
            raise G2FailureReviewError(f"{method_id} primary metric is absent from a trace")
        candidate_value = _number(candidate[metric], f"{method_id}/{task_id} metric")
        parent_value = _number(parent[metric], f"{parent_id}/{task_id} metric")
        delta = candidate_value - parent_value
        baseline_scores[task_id] = parent_value
        candidate_scores[task_id] = candidate_value
        family = task_families[task_id]
        task_rows.append(
            {
                "task_id": task_id,
                "task_family": family,
                "in_target_family": "all" in target_families
                or family in target_families,
                "parent_value": parent_value,
                "candidate_value": candidate_value,
                "delta": delta,
            }
        )
    target_rows = [row for row in task_rows if row["in_target_family"]]
    if not target_rows:
        raise G2FailureReviewError(f"{method_id} target subset is empty")
    full_delta = sum(float(row["delta"]) for row in task_rows) / len(task_rows)
    target_delta = sum(float(row["delta"]) for row in target_rows) / len(target_rows)
    _same_number(analysis_row.get("full_set_delta"), full_delta, f"{method_id} full delta")
    _same_number(analysis_row.get("target_delta"), target_delta, f"{method_id} target delta")
    if analysis_row.get("target_task_count") != len(target_rows):
        raise G2FailureReviewError(f"{method_id} target task count is stale")
    _same_number(
        analysis_row.get("target_parent_mean"),
        sum(float(row["parent_value"]) for row in target_rows) / len(target_rows),
        f"{method_id} target parent mean",
    )
    _same_number(
        analysis_row.get("target_candidate_mean"),
        sum(float(row["candidate_value"]) for row in target_rows) / len(target_rows),
        f"{method_id} target candidate mean",
    )
    promotion = _object(protocol.get("promotion"), "canonical promotion contract")
    resamples = promotion.get("bootstrap_resamples")
    bootstrap_seed = promotion.get("bootstrap_seed")
    minimum_delta = _number(
        promotion.get("minimum_target_family_delta"), "minimum target delta"
    )
    regression_floor = _number(
        promotion.get("full_set_material_regression"), "full regression floor"
    )
    latency_ceiling = _number(
        promotion.get("retrieval_p95_latency_ceiling_ms"), "latency ceiling"
    )
    if (
        isinstance(resamples, bool)
        or not isinstance(resamples, int)
        or resamples < 1
        or not isinstance(bootstrap_seed, str)
        or not bootstrap_seed
    ):
        raise G2FailureReviewError("canonical bootstrap contract is invalid")
    target_ids = [str(row["task_id"]) for row in target_rows]
    target_baseline = {task_id: baseline_scores[task_id] for task_id in target_ids}
    target_candidate = {task_id: candidate_scores[task_id] for task_id in target_ids}

    def seed(label: str) -> int:
        digest = hashlib.sha256(label.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big")

    target_ci = paired_bootstrap_ci(
        target_baseline,
        target_candidate,
        seed=seed(f"{bootstrap_seed}:{method_id}:target"),
        resamples=resamples,
    )
    full_ci = paired_bootstrap_ci(
        baseline_scores,
        candidate_scores,
        seed=seed(f"{bootstrap_seed}:{method_id}:full"),
        resamples=resamples,
    )
    if analysis_row.get("target_bootstrap") != target_ci:
        raise G2FailureReviewError(f"{method_id} target bootstrap is stale")
    if analysis_row.get("full_set_bootstrap") != full_ci:
        raise G2FailureReviewError(f"{method_id} full bootstrap is stale")
    method_traces = [trace_index[(task_id, method_id)] for task_id in task_ids]
    latency = distribution_summary(
        _number(
            _object(trace.get("component_metrics"), f"{method_id} metrics").get(
                "retrieval_latency_ms"
            ),
            f"{method_id} retrieval latency",
        )
        for trace in method_traces
    )
    if analysis_row.get("latency_ms") != latency:
        raise G2FailureReviewError(f"{method_id} latency summary is stale")
    leakage = _object(
        component_lab.get("question_reference_leakage_audit"),
        "public question-reference leakage audit",
    )
    leakage_passed = (
        leakage.get("status") == "passed"
        and leakage.get("leaked_reference_count") == 0
    )
    identifier_survives = True
    expected_identifier: dict[str, Any] = {"applicable": False}
    if method_id == "R1":
        mask_audit = _object(
            component_lab.get("identifier_mask_audit"), "identifier-mask audit"
        )
        masked_rows = {
            str(row.get("task_id")): _object(row, "identifier-mask row")
            for row in _list(mask_audit.get("rows"), "identifier-mask rows")
            if isinstance(row, Mapping)
        }
        if set(masked_rows) != set(task_ids):
            raise G2FailureReviewError("identifier-mask audit does not cover 84 tasks")
        masked_values = {
            task_id: _number(
                _object(masked_rows[task_id].get("metrics"), "identifier-mask metrics")[
                    metric
                ],
                f"identifier-mask {task_id} {metric}",
            )
            for task_id in target_ids
        }
        masked_mean = sum(masked_values.values()) / len(masked_values)
        masked_delta = masked_mean - (
            sum(target_baseline.values()) / len(target_baseline)
        )
        identifier_survives = masked_delta >= minimum_delta
        expected_identifier = {
            "applicable": True,
            "masked_target_mean": masked_mean,
            "masked_target_delta_vs_parent": masked_delta,
            "minimum_delta": minimum_delta,
            "survives": identifier_survives,
        }
    if analysis_row.get("identifier_mask_check") != expected_identifier:
        raise G2FailureReviewError(f"{method_id} identifier-mask result is stale")
    zero_cost = all(str(trace.get("retrieval_cost_usd", "")) == "0" for trace in method_traces)
    expected_criteria = {
        "target_delta_meets_minimum": target_delta >= minimum_delta,
        "target_ci_supports_direction": float(target_ci["ci_lower"]) >= 0.0,
        "full_set_not_materially_regressed": full_delta >= regression_floor,
        "latency_within_budget": float(latency["p95"]) <= latency_ceiling,
        "question_reference_leakage_passed": leakage_passed,
        "identifier_mask_check_passed": identifier_survives,
        "retrieval_cost_is_zero": zero_cost,
    }
    criteria = _object(analysis_row.get("criteria"), f"{method_id} criteria")
    if set(criteria) != _PUBLIC_CRITERIA or any(
        not isinstance(value, bool) for value in criteria.values()
    ):
        raise G2FailureReviewError(f"{method_id} criteria must be boolean")
    if dict(criteria) != expected_criteria:
        raise G2FailureReviewError(
            f"{method_id} criteria differ from canonical preregistration"
        )
    failed = sorted(key for key, value in criteria.items() if value is False)
    status = analysis_row.get("status")
    if (status == "public_failed") != bool(failed):
        raise G2FailureReviewError(f"{method_id} status differs from its criteria")

    best_rows = sorted(task_rows, key=lambda row: (-float(row["delta"]), row["task_id"]))[
        :TRACE_REFERENCE_LIMIT
    ]
    worst_rows = sorted(task_rows, key=lambda row: (float(row["delta"]), row["task_id"]))[
        :TRACE_REFERENCE_LIMIT
    ]
    return {
        "method_id": method_id,
        "parent_method_id": parent_id,
        "public_status": status,
        "primary_metric": metric,
        "target_families": sorted(target_families),
        "failed_preregistered_criteria": failed,
        "target_delta": target_delta,
        "full_set_delta": full_delta,
        "target_outcomes": _counts(target_rows),
        "full_set_outcomes": _counts(task_rows),
        "task_delta_commitment_sha256": sha256_json(task_rows),
        "trace_reference_limit": TRACE_REFERENCE_LIMIT,
        "best_public_trace_references": [
            _trace_reference(
                row,
                method_id=method_id,
                parent_id=str(parent_id),
                trace_index=trace_index,
            )
            for row in best_rows
        ],
        "worst_public_trace_references": [
            _trace_reference(
                row,
                method_id=method_id,
                parent_id=str(parent_id),
                trace_index=trace_index,
            )
            for row in worst_rows
        ],
        "saved_trace_decisions": _trace_decisions(method_id, task_ids, trace_index),
    }


def _sealed_r4_review(
    sealed: Mapping[str, Any],
    technical_gate: Mapping[str, Any],
    protocol: Mapping[str, Any],
    metric: str,
) -> dict[str, Any]:
    stages = _object(technical_gate.get("stages"), "technical gate stages")
    sealed_stage = _object(stages.get("sealed_evaluation"), "sealed gate stage")
    candidates = _list(
        sealed_stage.get("incremental_candidates"), "sealed incremental candidates"
    )
    matches = [
        _object(row, "sealed gate candidate")
        for row in candidates
        if isinstance(row, Mapping) and row.get("candidate") == "R4"
    ]
    if len(matches) != 1:
        raise G2FailureReviewError("technical gate must contain exactly one R4 sealed row")
    gate_row = matches[0]
    if gate_row.get("parent") != "R3" or gate_row.get("primary_metric") != metric:
        raise G2FailureReviewError("sealed R4 parent or metric is stale")
    if set(gate_row) != {
        "candidate",
        "parent",
        "primary_metric",
        "target_family_aggregate",
        "paired_bootstrap",
        "latency_ms",
        "criteria",
        "promotion_eligible",
        "failed_ancestor_blockers",
        "decision",
    }:
        raise G2FailureReviewError("sealed R4 row fields differ from the gate schema")
    records = _list(sealed.get("component_records"), "sealed component records")
    index = {
        (str(row["task_id"]), str(row["strategy_id"])): _object(
            row, "sealed component record"
        )
        for row in records
        if isinstance(row, Mapping)
    }
    baseline = {
        task_id: _number(
            _object(index[(task_id, "R3")].get("metrics"), "sealed R3 metrics")[metric],
            f"sealed {task_id}/R3 {metric}",
        )
        for task_id in G2_SEALED_TASK_IDS
    }
    candidate = {
        task_id: _number(
            _object(index[(task_id, "R4")].get("metrics"), "sealed R4 metrics")[metric],
            f"sealed {task_id}/R4 {metric}",
        )
        for task_id in G2_SEALED_TASK_IDS
    }
    gate_ci = _object(gate_row.get("paired_bootstrap"), "sealed R4 bootstrap")
    promotion = _object(protocol.get("promotion"), "canonical promotion contract")
    resamples = promotion.get("bootstrap_resamples")
    if isinstance(resamples, bool) or not isinstance(resamples, int) or resamples < 1:
        raise G2FailureReviewError("canonical sealed bootstrap count is invalid")
    recomputed_ci = paired_bootstrap_ci(
        baseline,
        candidate,
        seed=int.from_bytes(b"g2-sealed:R4", "little") % (2**63),
        resamples=resamples,
    )
    if dict(gate_ci) != recomputed_ci:
        raise G2FailureReviewError("sealed R4 bootstrap differs from safe metrics")
    recomputed_latency = distribution_summary(
        _number(
            _object(index[(task_id, "R4")].get("metrics"), "sealed R4 metrics")[
                "retrieval_latency_ms"
            ],
            f"sealed {task_id}/R4 latency",
        )
        for task_id in G2_SEALED_TASK_IDS
    )
    gate_latency = _object(gate_row.get("latency_ms"), "sealed R4 latency")
    if dict(gate_latency) != recomputed_latency:
        raise G2FailureReviewError("sealed R4 latency differs from safe metrics")
    try:
        zero_cost = all(
            Decimal(
                str(
                    _object(
                        index[(task_id, "R4")].get("metrics"), "sealed R4 metrics"
                    )["retrieval_cost_usd"]
                )
            )
            == 0
            for task_id in G2_SEALED_TASK_IDS
        )
    except (InvalidOperation, ValueError) as exc:
        raise G2FailureReviewError("sealed R4 retrieval cost is invalid") from exc
    criteria = _object(gate_row.get("criteria"), "sealed R4 gate criteria")
    expected_criteria = {
        "target_family_minimum_met",
        "full_set_not_materially_regressed",
        "paired_ci_supports_direction",
        "latency_within_budget",
        "retrieval_cost_is_zero",
        "trace_commitments_complete",
        "sealed_generation_complete",
    }
    if set(criteria) != expected_criteria or any(
        not isinstance(value, bool) for value in criteria.values()
    ):
        raise G2FailureReviewError("sealed R4 gate criteria are invalid")
    if criteria.get("paired_ci_supports_direction") is not (
        float(recomputed_ci["ci_lower"]) >= 0.0
    ):
        raise G2FailureReviewError("sealed R4 CI criterion differs from safe metrics")
    if criteria.get("retrieval_cost_is_zero") is not zero_cost:
        raise G2FailureReviewError("sealed R4 cost criterion differs from safe metrics")
    if criteria.get("trace_commitments_complete") is not (
        len(index) == G2_COMPONENT_CELL_COUNT
    ):
        raise G2FailureReviewError("sealed R4 trace criterion differs from safe metrics")
    summary = sealed.get("generation_summary")
    generation_complete = (
        isinstance(summary, Mapping)
        and summary.get("generation_count") == 576
        and summary.get("status_counts")
        == {"completed": 576, "failed": 0, "pending": 0}
    )
    if criteria.get("sealed_generation_complete") is not generation_complete:
        raise G2FailureReviewError("sealed R4 generation criterion is stale")
    regression_floor = _number(
        promotion.get("full_set_material_regression"), "sealed regression floor"
    )
    minimum_target_delta = _number(
        promotion.get("minimum_target_family_delta"),
        "sealed target-family minimum",
    )
    latency_ceiling = _number(
        promotion.get("retrieval_p95_latency_ceiling_ms"),
        "sealed latency ceiling",
    )
    expected_gate_criteria = {
        "target_family_minimum_met": False,
        "full_set_not_materially_regressed": float(recomputed_ci["mean_delta"])
        >= regression_floor,
        "paired_ci_supports_direction": float(recomputed_ci["ci_lower"]) >= 0.0,
        "latency_within_budget": float(recomputed_latency["p95"]) <= latency_ceiling,
        "retrieval_cost_is_zero": zero_cost,
        "trace_commitments_complete": len(index) == G2_COMPONENT_CELL_COUNT,
        "sealed_generation_complete": generation_complete,
    }
    if dict(criteria) != expected_gate_criteria:
        raise G2FailureReviewError(
            "sealed R4 criteria differ from canonical preregistration"
        )
    expected_target_family = {
        "status": "unavailable",
        "minimum_delta": minimum_target_delta,
        "met": False,
        "reason": "safe sealed import has no content-free target-family aggregate",
    }
    if gate_row.get("target_family_aggregate") != expected_target_family:
        raise G2FailureReviewError(
            "sealed R4 target-family availability differs from safe evidence"
        )
    decision = gate_row.get("decision")
    if (
        decision != "ineligible"
        or gate_row.get("promotion_eligible") is not False
        or gate_row.get("failed_ancestor_blockers") != ["R2", "R3"]
    ):
        raise G2FailureReviewError("sealed R4 eligibility or ancestry is forged")
    mean_delta = float(recomputed_ci["mean_delta"])
    direction = "improvement" if mean_delta > 0 else "regression" if mean_delta < 0 else "tie"
    return {
        "candidate_method_id": "R4",
        "parent_method_id": "R3",
        "primary_metric": metric,
        "direction": direction,
        "paired_bootstrap": recomputed_ci,
        "latency_ms": recomputed_latency,
        "retrieval_cost_is_zero": zero_cost,
        "target_family_aggregate": expected_target_family,
        "gate_criteria": dict(sorted(criteria.items())),
        "gate_decision": decision,
        "promotion_eligible": False,
        "failed_ancestor_blockers": ["R2", "R3"],
        "sealed_trace_commitment_count": len(index),
    }


def _compose_review(
    *,
    root: Path,
    component_lab: Mapping[str, Any],
    component_analysis: Mapping[str, Any],
    public_answer_metrics: Mapping[str, Any],
    repeat_analysis: Mapping[str, Any],
    sealed_import: Mapping[str, Any],
    technical_gate: Mapping[str, Any],
) -> dict[str, Any]:
    trace_index, task_ids, families, manifest_hashes, protocol = (
        _validate_source_bindings(
            root=root,
            component_lab=component_lab,
            component_analysis=component_analysis,
            public_answer_metrics=public_answer_metrics,
            repeat_analysis=repeat_analysis,
            sealed_import=sealed_import,
            technical_gate=technical_gate,
        )
    )
    methods = _object(component_analysis["methods"], "component analysis methods")
    failures = {
        method_id: _method_review(
            method_id,
            analysis_row=_object(methods[method_id], f"component analysis {method_id}"),
            component_lab=component_lab,
            protocol=protocol,
            task_ids=task_ids,
            task_families=families,
            trace_index=trace_index,
        )
        for method_id in FAILED_METHOD_IDS
    }
    r4_public = _method_review(
        "R4",
        analysis_row=_object(methods["R4"], "component analysis R4"),
        component_lab=component_lab,
        protocol=protocol,
        task_ids=task_ids,
        task_families=families,
        trace_index=trace_index,
    )
    if r4_public["failed_preregistered_criteria"]:
        raise G2FailureReviewError("R4 public evidence contains a failed criterion")
    if (
        float(r4_public["target_delta"]) <= 0.0
        or float(r4_public["full_set_delta"]) <= 0.0
        or r4_public["full_set_outcomes"]["improvement_count"] < 1
    ):
        raise G2FailureReviewError(
            "R4 public traces do not support a plausible improvement path"
        )
    r4_sealed = _sealed_r4_review(
        sealed_import,
        technical_gate,
        protocol,
        str(r4_public["primary_metric"]),
    )
    repeat_aggregate = _object(
        repeat_analysis["aggregate_consistency"], "repeat consistency aggregate"
    )
    payload: dict[str, Any] = {
        "schema_version": G2_FAILURE_REVIEW_SCHEMA,
        "scope": "content_free_g2_failure_trace_review",
        "source_artifacts": {
            "public_component_lab": {
                "schema_version": component_lab["schema_version"],
                "artifact_sha256": component_lab["artifact_sha256"],
            },
            "public_component_analysis": {
                "schema_version": component_analysis["schema_version"],
                "artifact_sha256": component_analysis["analysis_sha256"],
            },
            "public_answer_metrics": {
                "schema_version": public_answer_metrics["schema_version"],
                "artifact_sha256": public_answer_metrics["artifact_sha256"],
            },
            "public_repeat_analysis": {
                "schema_version": repeat_analysis["schema_version"],
                "artifact_sha256": repeat_analysis["analysis_sha256"],
            },
            "safe_sealed_import": {
                "schema_version": sealed_import["schema_version"],
                "artifact_sha256": sha256_json(sealed_import),
                "source_return_sha256": sealed_import["source_return_sha256"],
            },
            "technical_g2_gate": {
                "schema_version": technical_gate["schema_version"],
                "technical_record_sha256": technical_gate[
                    "technical_record_sha256"
                ],
                "technical_subset_sha256": sha256_json(
                    _technical_subset(technical_gate)
                ),
            },
        },
        "frozen_run": {
            "retrieval_protocol_sha256": component_analysis["protocol_sha256"],
            "component_lab_sha256": component_lab["artifact_sha256"],
            "static_freeze_manifest_sha256": sealed_import[
                "static_freeze_manifest_sha256"
            ],
            "external_bundle_sha256": sealed_import["external_bundle_sha256"],
            "generation_campaign_id": technical_gate["generation_campaign_id"],
            "requested_model": technical_gate["requested_model"],
            "reasoning_efforts": list(ALLOWED_REASONING_EFFORTS),
            "output_token_limit": technical_gate["output_token_limit"],
            "generation_manifest_sha256s": manifest_hashes,
        },
        "completeness": {
            "policy": "fail_closed_no_missing_data_pass",
            "missing_data_can_pass": False,
            "required_experimental_methods": list(METHOD_IDS[1:]),
            "reviewed_experimental_methods": list(METHOD_IDS[1:]),
            "missing_experimental_methods": [],
            "required_failed_methods": list(FAILED_METHOD_IDS),
            "reviewed_failed_methods": list(failures),
            "missing_failed_methods": [],
            "public_failed_methods": list(FAILED_METHOD_IDS),
            "incremental_ineligible_methods": ["R4"],
            "source_artifact_count": 6,
            "public_task_count": len(task_ids),
            "public_trace_count": len(trace_index),
            "sealed_component_record_count": G2_COMPONENT_CELL_COUNT,
            "status": "complete",
        },
        "non_promoted_failures": failures,
        "r4_evidence_path_review": {
            "method_id": "R4",
            "interpretation": "plausible_evidence_path_improvement_not_causal_proof",
            "disposition": "non_promoted_ineligible",
            "failed_ancestor_blockers": ["R2", "R3"],
            "retained_retriever_id": "R0",
            "public_incremental_evidence": r4_public,
            "safe_sealed": r4_sealed,
        },
        "evidence_limitations": {
            "public_answer_screening": {
                "scope": public_answer_metrics["scope"],
                "completed_cell_count": public_answer_metrics[
                    "completed_cell_count"
                ],
                "limitation": "deterministic screening only; not a semantic correctness grade",
            },
            "repeat_answer_hashes": {
                "trial_count": repeat_analysis["trial_count"],
                "exact_match_cell_rate": repeat_aggregate["exact_match_cell_rate"],
                "limitation": "exact answer hashes measure byte-level reproducibility, not semantic equivalence",
            },
        },
    }
    payload["artifact_sha256"] = sha256_json(payload)
    return payload


def build_g2_failure_review(
    *,
    root: Path,
    component_lab: Mapping[str, Any],
    component_analysis: Mapping[str, Any],
    public_answer_metrics: Mapping[str, Any],
    repeat_analysis: Mapping[str, Any],
    sealed_import: Mapping[str, Any],
    technical_gate: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the deterministic failure review from the six frozen evidence inputs."""

    return _compose_review(
        root=Path(root).resolve(),
        component_lab=_object(component_lab, "public component lab"),
        component_analysis=_object(component_analysis, "public component analysis"),
        public_answer_metrics=_object(public_answer_metrics, "public answer metrics"),
        repeat_analysis=_object(repeat_analysis, "repeat analysis"),
        sealed_import=_object(sealed_import, "safe sealed import"),
        technical_gate=_object(technical_gate, "technical G2 gate"),
    )


def validate_g2_failure_review(
    review: Mapping[str, Any],
    *,
    root: Path,
    component_lab: Mapping[str, Any],
    component_analysis: Mapping[str, Any],
    public_answer_metrics: Mapping[str, Any],
    repeat_analysis: Mapping[str, Any],
    sealed_import: Mapping[str, Any],
    technical_gate: Mapping[str, Any],
) -> None:
    """Recompute the review and reject altered values or stale trace bindings."""

    value = _object(review, "G2 failure review")
    _hash_checked(
        value,
        schema=G2_FAILURE_REVIEW_SCHEMA,
        field="artifact_sha256",
        label="G2 failure review",
    )
    expected = build_g2_failure_review(
        root=root,
        component_lab=component_lab,
        component_analysis=component_analysis,
        public_answer_metrics=public_answer_metrics,
        repeat_analysis=repeat_analysis,
        sealed_import=sealed_import,
        technical_gate=technical_gate,
    )
    if dict(value) != expected:
        raise G2FailureReviewError(
            "G2 failure review differs from deterministic source recomputation"
        )


def write_g2_failure_review(
    *,
    root: Path,
    component_lab: Mapping[str, Any],
    component_analysis: Mapping[str, Any],
    public_answer_metrics: Mapping[str, Any],
    repeat_analysis: Mapping[str, Any],
    sealed_import: Mapping[str, Any],
    technical_gate: Mapping[str, Any],
    output: Path,
) -> dict[str, Any]:
    """Build, validate, and atomically persist the content-free review."""

    destination = Path(output)
    if destination.is_symlink():
        raise G2FailureReviewError("G2 failure-review output must not be a symlink")
    review = build_g2_failure_review(
        root=root,
        component_lab=component_lab,
        component_analysis=component_analysis,
        public_answer_metrics=public_answer_metrics,
        repeat_analysis=repeat_analysis,
        sealed_import=sealed_import,
        technical_gate=technical_gate,
    )
    validate_g2_failure_review(
        review,
        root=root,
        component_lab=component_lab,
        component_analysis=component_analysis,
        public_answer_metrics=public_answer_metrics,
        repeat_analysis=repeat_analysis,
        sealed_import=sealed_import,
        technical_gate=technical_gate,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(
                review,
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
        temporary.replace(destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return review
