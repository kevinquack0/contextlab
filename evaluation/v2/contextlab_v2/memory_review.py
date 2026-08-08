"""Exhaustive dispositions for the public G3 unsupported-memory export.

The historical export name is intentionally retained because it is part of the
frozen metrics schema.  Its predicate is broader than unsupported memory
claims: every completed M1-M4 answer with incomplete answer provenance is
included.  This module records that distinction for every exported row.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .baseline import repository_root
from .g3_execution import validate_prepared_public_g3_cell
from .g3_grading import (
    G3GradingError,
    build_public_temporal_grade,
    parse_answer_footer,
)
from .g3_static_grading import (
    build_public_static_grade,
    validate_public_static_grade_evidence,
)
from .memory_experiments import MEMORY_CONFIGURATIONS
from .memory_metrics import (
    MEMORY_METRICS_SCHEMA,
    UNSUPPORTED_MEMORY_EXPORT_SCHEMA,
)
from .provider import ALLOWED_REASONING_EFFORTS
from .tasking import sha256_json
from .temporal import (
    all_events,
    expected_answer_for_question,
    temporal_question_catalog,
)


UNSUPPORTED_MEMORY_REVIEW_SCHEMA = "contextlab.g3-unsupported-memory-review.v2"
UNSUPPORTED_MEMORY_REVIEW_APPROVAL_SCHEMA = (
    "contextlab.g3-unsupported-memory-review-kevin-approval.v2"
)
UNSUPPORTED_MEMORY_REVIEW_PATH = Path(
    "results/v2/reviews/g3_unsupported_memory_dispositions.json"
)
UNSUPPORTED_MEMORY_REVIEW_APPROVAL_PATH = Path(
    "results/v2/reviews/g3_unsupported_memory_dispositions_kevin_approval.json"
)
EXPECTED_UNSUPPORTED_MEMORY_ANSWER_COUNT = 558
REVIEW_METHOD = {
    "schema_version": "contextlab.g3-unsupported-memory-review-method.v2",
    "method_id": "evidence-bound-exhaustive-disposition-v2",
    "source_scope": "public G3 M1-M4 unsupported-memory export only",
    "classification_basis": [
        "frozen export row",
        "hash-bound result receipt",
        "deterministic answer grade",
        "static objective evidence or temporal footer and raw-event rules",
    ],
    "attestation_scope": (
        "The report proves deterministic source coverage and classification. "
        "It does not assert an invocation identity for informal slice reviews."
    ),
    "sealed_or_private_data_accessed": False,
}

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_TASK_ID = re.compile(r"[ST][0-9]{3}\Z")
_CITATION = re.compile(r"\[([^\]\n]{1,200})\]")
_LOOSE_STATUS = re.compile(r"ANSWER_STATUS:\s*(answer|abstain)\s*\Z")
_LOOSE_CLAIMS = re.compile(r"USED_MEMORY_CLAIMS:\s*(.*?)\s*\Z")
_CLAIM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class UnsupportedMemoryReviewError(ValueError):
    """The exhaustive public-memory review is incomplete or not evidence-bound."""


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise UnsupportedMemoryReviewError(f"{label} must be an object")
    return dict(value)


def _hash_checked(value: Mapping[str, Any], *, schema: str, label: str) -> str:
    if value.get("schema_version") != schema:
        raise UnsupportedMemoryReviewError(f"unsupported {label} schema")
    supplied = value.get("artifact_sha256")
    body = {key: item for key, item in value.items() if key != "artifact_sha256"}
    expected = sha256_json(body)
    if supplied != expected:
        raise UnsupportedMemoryReviewError(f"{label} hash mismatch")
    return expected


def _utc_second(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise UnsupportedMemoryReviewError(f"{label} must be UTC text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UnsupportedMemoryReviewError(f"{label} is invalid") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.astimezone(timezone.utc).microsecond != 0
    ):
        raise UnsupportedMemoryReviewError(
            f"{label} must have UTC timezone and whole-second precision"
        )
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UnsupportedMemoryReviewError(f"cannot read {label}") from exc
    return _object(value, label)


def _source_export(metrics: Mapping[str, Any]) -> tuple[dict[str, Any], list[Any]]:
    _hash_checked(metrics, schema=MEMORY_METRICS_SCHEMA, label="public G3 metrics")
    exported = _object(
        metrics.get("unsupported_memory_answers"), "unsupported-memory export"
    )
    _hash_checked(
        exported,
        schema=UNSUPPORTED_MEMORY_EXPORT_SCHEMA,
        label="unsupported-memory export",
    )
    rows = exported.get("answers")
    if (
        not isinstance(rows, list)
        or exported.get("answer_count") != len(rows)
        or any(not isinstance(row, Mapping) for row in rows)
    ):
        raise UnsupportedMemoryReviewError(
            "unsupported-memory export answer count or rows are invalid"
        )
    return exported, rows


def _result_path(root: Path, row: Mapping[str, Any]) -> Path:
    policy = row.get("policy")
    effort = row.get("reasoning_effort")
    task_id = row.get("task_id")
    run_id = row.get("run_id")
    if (
        policy not in MEMORY_CONFIGURATIONS[1:]
        or effort not in ALLOWED_REASONING_EFFORTS
        or not isinstance(task_id, str)
        or _TASK_ID.fullmatch(task_id) is None
        or run_id != f"g3-public-v1-{policy}-{effort}-{task_id}"
    ):
        raise UnsupportedMemoryReviewError("unsupported-memory row identity is invalid")
    return (
        root
        / "results/v2/memory/receipts/g3-public-v1"
        / str(policy)
        / str(effort)
        / f"{task_id}.json"
    )


def _prepared_path(root: Path, row: Mapping[str, Any]) -> Path:
    return (
        root
        / "results/v2/memory/prepared/g3-public-v1"
        / str(row["policy"])
        / str(row["reasoning_effort"])
        / f"{row['task_id']}.json"
    )


def _static_evidence_path(root: Path, row: Mapping[str, Any]) -> Path:
    return (
        root
        / "results/v2/memory/grades/g3-public-v1"
        / str(row["policy"])
        / str(row["reasoning_effort"])
        / f"{row['task_id']}.json"
    )


def _claim_provenance(receipt: Mapping[str, Any]) -> tuple[list[Any], list[Any]]:
    selected = receipt.get("trace", {}).get("selected_memory_evidence", [])
    if not isinstance(selected, list):
        raise UnsupportedMemoryReviewError(
            "receipt selected memory evidence is invalid"
        )
    by_claim = {
        item.get("claim_id"): item
        for item in selected
        if isinstance(item, Mapping) and isinstance(item.get("claim_id"), str)
    }
    claims: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    used = receipt.get("used_memory_claims")
    if not isinstance(used, list):
        raise UnsupportedMemoryReviewError("receipt used memory claims are invalid")
    for item in used:
        if not isinstance(item, Mapping) or not isinstance(item.get("claim_id"), str):
            raise UnsupportedMemoryReviewError("receipt memory claim is invalid")
        claim_id = str(item["claim_id"])
        supplied = item.get("supporting_event_ids")
        if not isinstance(supplied, list) or any(
            not isinstance(raw_id, str) or not raw_id for raw_id in supplied
        ):
            raise UnsupportedMemoryReviewError("receipt claim evidence is invalid")
        raw_ids = sorted(set(supplied))
        candidate = by_claim.get(claim_id)
        trace_raw_ids = (
            sorted(candidate.get("raw_evidence_ids", []))
            if isinstance(candidate, Mapping)
            else []
        )
        valid = (
            candidate is not None
            and bool(raw_ids)
            and set(raw_ids).issubset(trace_raw_ids)
        )
        claim = {
            "claim_id": claim_id,
            "supporting_event_ids": raw_ids,
            "trace_raw_evidence_ids": trace_raw_ids,
            "provenance_valid": valid,
        }
        claims.append(claim)
        if not valid:
            unsupported.append(claim)
    return claims, unsupported


def _bind_export_row(row: Mapping[str, Any], receipt: Mapping[str, Any]) -> None:
    body = {key: item for key, item in receipt.items() if key != "result_sha256"}
    if receipt.get("result_sha256") != sha256_json(body):
        raise UnsupportedMemoryReviewError("source result receipt hash mismatch")
    task = receipt.get("run_spec", {}).get("task", {})
    claims, unsupported = _claim_provenance(receipt)
    expected_fields = {
        "run_id",
        "task_id",
        "policy",
        "reasoning_effort",
        "answer",
        "provenance_complete",
        "unsupported_claims",
        "claim_provenance",
    }
    if (
        set(row) != expected_fields
        or receipt.get("status") != "completed"
        or receipt.get("run_id") != row.get("run_id")
        or task.get("task_id") != row.get("task_id")
        or receipt.get("policy") != row.get("policy")
        or receipt.get("reasoning_effort") != row.get("reasoning_effort")
        or receipt.get("answer") != row.get("answer")
        or receipt.get("provenance_complete") is not False
        or row.get("provenance_complete") is not False
        or claims != row.get("claim_provenance")
        or unsupported != row.get("unsupported_claims")
    ):
        raise UnsupportedMemoryReviewError(
            "unsupported-memory row differs from its source receipt"
        )


def _static_causes(evidence: Mapping[str, Any]) -> tuple[list[str], dict[str, Any]]:
    _hash_checked(
        evidence,
        schema="contextlab.g3-public-static-objective-evidence.v1",
        label="static objective evidence",
    )
    checks = _object(evidence.get("objective_checks"), "static objective checks")
    footer = _object(evidence.get("answer_footer"), "static answer footer")
    exact_count = checks.get("exact_citation_count")
    unsupported_count = checks.get("unsupported_citation_count")
    required_recall = checks.get("required_evidence_citation_recall")
    if (
        isinstance(exact_count, bool)
        or not isinstance(exact_count, int)
        or exact_count < 0
        or isinstance(unsupported_count, bool)
        or not isinstance(unsupported_count, int)
        or unsupported_count < 0
        or isinstance(required_recall, bool)
        or not isinstance(required_recall, (int, float))
        or not 0.0 <= float(required_recall) <= 1.0
        or not isinstance(footer.get("footer_exact"), bool)
    ):
        raise UnsupportedMemoryReviewError("static objective checks are invalid")
    causes: list[str] = []
    if not footer["footer_exact"]:
        causes.append("static_footer_not_exact")
    if unsupported_count:
        causes.append("static_unsupported_or_malformed_citation")
    if exact_count == 0:
        causes.append("static_no_exact_raw_citation")
    if float(required_recall) < 1.0:
        causes.append("static_required_evidence_not_fully_cited")
    if not causes or evidence.get("provenance_complete") is not False:
        raise UnsupportedMemoryReviewError(
            "static provenance failure has no reproducible cause"
        )
    if unsupported_count:
        primary = "static_unsupported_or_malformed_citation"
    elif exact_count == 0:
        primary = "static_no_exact_raw_citation"
    elif float(required_recall) < 1.0:
        primary = "static_required_evidence_not_fully_cited"
    else:
        primary = "static_footer_not_exact"
    return causes, {
        "primary_cause": primary,
        "footer_exact": footer["footer_exact"],
        "exact_citation_count": exact_count,
        "unsupported_citation_count": unsupported_count,
        "required_evidence_citation_recall": float(required_recall),
        "static_objective_evidence_sha256": evidence["artifact_sha256"],
    }


def _cites_raw(reference: str, raw_id: str) -> bool:
    return reference == raw_id or reference.endswith(f"#{raw_id}")


def _loose_footer(answer: str) -> dict[str, Any]:
    lines = answer.rstrip("\n").splitlines()
    status_line = lines[-2] if len(lines) >= 2 else ""
    claims_line = lines[-1] if lines else ""
    status = _LOOSE_STATUS.fullmatch(status_line)
    claims = _LOOSE_CLAIMS.fullmatch(claims_line)
    claim_text = claims.group(1) if claims is not None else None
    if claim_text == "none":
        declared: list[str] = []
        malformed: list[str] = []
    elif claim_text is None:
        declared = []
        malformed = []
    else:
        tokens = [item.strip() for item in claim_text.split(",")]
        declared = [item for item in tokens if _CLAIM_ID.fullmatch(item)]
        malformed = [item for item in tokens if _CLAIM_ID.fullmatch(item) is None]
    return {
        "status_line_exact": re.fullmatch(
            r"ANSWER_STATUS: (answer|abstain)", status_line
        )
        is not None,
        "loose_status": status.group(1) if status is not None else None,
        "claims_line_present": claims is not None,
        "declared_claim_ids": declared,
        "malformed_claim_tokens": malformed,
    }


def _temporal_required_raw_ids(task_id: str, prepared: Mapping[str, Any]) -> list[str]:
    questions = {
        question.task_id: question
        for question in temporal_question_catalog()
        if not question.is_sealed
    }
    question = questions.get(task_id)
    observable = prepared.get("observable_event_ids")
    if (
        question is None
        or not isinstance(observable, list)
        or any(not isinstance(item, str) for item in observable)
    ):
        raise UnsupportedMemoryReviewError("temporal review source is invalid")
    observable_ids = set(observable)
    events = [event for event in all_events() if event.event_id in observable_ids]
    assertions = [
        event
        for event in events
        if event.subject == question.subject and event.predicate == question.predicate
    ]
    assertion_ids = {event.event_id for event in assertions}
    lifecycle = [
        event for event in events if event.tombstone_for_event_id in assertion_ids
    ]
    expected = expected_answer_for_question(task_id)
    required = (
        set(expected.supporting_event_ids)
        if expected.status == "answer"
        else {
            event.event_id
            for event in lifecycle
            if event.status in {"expired", "retracted", "tombstone"}
        }
    )
    return sorted(required)


def _temporal_causes(
    row: Mapping[str, Any],
    receipt: Mapping[str, Any],
    prepared: Mapping[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    answer = row.get("answer")
    selected = receipt.get("trace", {}).get("selected_memory_evidence")
    if not isinstance(answer, str) or not isinstance(selected, list):
        raise UnsupportedMemoryReviewError("temporal review receipt is invalid")
    selected_ids = sorted(
        str(item["claim_id"])
        for item in selected
        if isinstance(item, Mapping) and isinstance(item.get("claim_id"), str)
    )
    footer_exact = True
    try:
        parse_answer_footer(answer, selected_ids)
    except G3GradingError:
        footer_exact = False
    loose = _loose_footer(answer)
    references = _CITATION.findall(answer)
    body_claim_ids = sorted(
        {reference for reference in references if reference in set(selected_ids)}
    )
    declared = sorted(set(loose["declared_claim_ids"]))
    causes: list[str] = []
    if not footer_exact:
        causes.append("temporal_footer_not_exact_or_unbound")
    if not loose["status_line_exact"]:
        causes.append("temporal_status_footer_not_exact")
    if not loose["claims_line_present"] or loose["malformed_claim_tokens"]:
        causes.append("temporal_memory_claim_footer_malformed")
    if set(declared) - set(body_claim_ids):
        causes.append("temporal_declared_claim_not_cited_in_body")
    if set(body_claim_ids) - set(declared):
        causes.append("temporal_body_claim_not_declared")
    if set(declared) - set(selected_ids):
        causes.append("temporal_declared_claim_outside_trace")
    used = receipt.get("used_memory_claims")
    if not isinstance(used, list):
        raise UnsupportedMemoryReviewError("temporal used claims are invalid")
    used_raw_complete = True
    for claim in used:
        raw_ids = (
            claim.get("supporting_event_ids", []) if isinstance(claim, Mapping) else []
        )
        if not isinstance(raw_ids, list) or not all(
            any(_cites_raw(reference, str(raw_id)) for reference in references)
            for raw_id in raw_ids
        ):
            used_raw_complete = False
            break
    if not used_raw_complete:
        causes.append("temporal_used_claim_missing_raw_citation")
    required = _temporal_required_raw_ids(str(row["task_id"]), prepared)
    missing_required = [
        raw_id
        for raw_id in required
        if not any(_cites_raw(reference, raw_id) for reference in references)
    ]
    if missing_required:
        causes.append("temporal_required_evidence_not_fully_cited")
    causes = list(dict.fromkeys(causes))
    if not causes or receipt.get("provenance_complete") is not False:
        raise UnsupportedMemoryReviewError(
            "temporal provenance failure has no reproducible cause"
        )
    if "temporal_memory_claim_footer_malformed" in causes:
        primary = "temporal_memory_claim_footer_malformed"
    elif "temporal_declared_claim_not_cited_in_body" in causes:
        primary = "temporal_declared_claim_not_cited_in_body"
    elif "temporal_body_claim_not_declared" in causes:
        primary = "temporal_body_claim_not_declared"
    elif "temporal_status_footer_not_exact" in causes:
        primary = "temporal_status_footer_not_exact"
    elif "temporal_required_evidence_not_fully_cited" in causes:
        primary = "temporal_required_evidence_not_fully_cited"
    elif "temporal_used_claim_missing_raw_citation" in causes:
        primary = "temporal_used_claim_missing_raw_citation"
    else:
        primary = "temporal_footer_not_exact_or_unbound"
    return causes, {
        "primary_cause": primary,
        "footer_exact_and_bound": footer_exact,
        "status_line_exact": loose["status_line_exact"],
        "declared_memory_claim_ids": declared,
        "body_memory_claim_ids": body_claim_ids,
        "malformed_memory_claim_tokens": loose["malformed_claim_tokens"],
        "required_raw_evidence_ids": required,
        "missing_required_raw_evidence_ids": missing_required,
        "used_claim_raw_citations_complete": used_raw_complete,
    }


def _export_scope(row: Mapping[str, Any], details: Mapping[str, Any]) -> str:
    if row.get("unsupported_claims"):
        return "unsupported_memory_claim"
    if str(row.get("task_id", "")).startswith("S"):
        return "static_answer_provenance_failure_no_memory_applicable"
    declared = details.get("declared_memory_claim_ids")
    claim_provenance = row.get("claim_provenance")
    if isinstance(claim_provenance, list) and claim_provenance:
        return "temporal_answer_failure_with_valid_memory_claim_trace"
    if isinstance(declared, list) and declared:
        return "temporal_answer_failure_with_unbound_declared_memory_claim"
    return "temporal_answer_provenance_failure_no_memory_claim_declared"


def _review_bucket(receipt: Mapping[str, Any]) -> str:
    if receipt.get("stale_answer") is True:
        return "stale_answer"
    if receipt.get("is_correct") is not True:
        return "incorrect_answer"
    return "provenance_only"


def _scope_and_disposition(
    suite: str,
    causes: Sequence[str],
    details: Mapping[str, Any],
    *,
    unsupported_memory_claim_detected: bool,
) -> tuple[list[str], str, bool]:
    if unsupported_memory_claim_detected:
        return ["memory_claim"], "unsupported_memory_claim", False
    if suite == "static":
        body_raw_complete = bool(
            details.get("exact_citation_count", 0)
            and details.get("unsupported_citation_count") == 0
            and details.get("required_evidence_citation_recall") == 1.0
        )
        scopes = ["corpus_evidence"]
        if "static_footer_not_exact" in causes:
            scopes.append("footer_contract")
        if details.get("unsupported_citation_count", 0) > 0:
            code = "static_malformed_or_unsupported_citation"
        elif details.get("exact_citation_count") == 0:
            code = "static_no_exact_raw_citation"
        elif details.get("required_evidence_citation_recall") != 1.0:
            code = "static_missing_required_evidence"
        else:
            code = "static_footer_format_only"
        return scopes, code, body_raw_complete
    missing_required = details.get("missing_required_raw_evidence_ids", [])
    body_raw_complete = not missing_required and bool(
        details.get("used_claim_raw_citations_complete")
    )
    scopes = ["footer_contract"]
    mismatch = any(
        cause
        in {
            "temporal_declared_claim_not_cited_in_body",
            "temporal_body_claim_not_declared",
            "temporal_declared_claim_outside_trace",
            "temporal_memory_claim_footer_malformed",
        }
        for cause in causes
    )
    if mismatch:
        scopes.append("memory_claim_binding")
    if not body_raw_complete:
        scopes.append("raw_event_evidence")
        code = "temporal_footer_and_raw_evidence_gap"
    elif mismatch:
        code = "temporal_memory_footer_body_mismatch"
    else:
        code = "temporal_footer_format_only"
    return scopes, code, body_raw_complete


def _review_one(
    index: int,
    row: Mapping[str, Any],
    *,
    root: Path,
) -> dict[str, Any]:
    receipt = _read_json(_result_path(root, row), "source result receipt")
    _bind_export_row(row, receipt)
    task = _object(receipt.get("run_spec", {}).get("task"), "receipt task")
    suite = task.get("suite")
    prepared = _read_json(_prepared_path(root, row), "prepared G3 cell")
    try:
        validate_prepared_public_g3_cell(prepared, root=root)
    except Exception as exc:
        raise UnsupportedMemoryReviewError(
            f"prepared G3 cell is invalid: {exc}"
        ) from exc
    if prepared.get("artifact_sha256") != receipt.get(
        "prepared_cell_artifact_sha256"
    ) or prepared.get("run_spec") != receipt.get("run_spec"):
        raise UnsupportedMemoryReviewError(
            "source receipt is not bound to its prepared cell"
        )
    generation = {
        "schema_version": "contextlab.generation-result.v1",
        "run_id": receipt.get("run_id"),
        "task_id": row.get("task_id"),
        "answer": receipt.get("answer"),
        "metadata": receipt.get("generation_metadata"),
    }
    if receipt.get("generation_result_sha256") != sha256_json(generation):
        raise UnsupportedMemoryReviewError(
            "source receipt generation commitment changed"
        )
    if suite == "static":
        evidence = _read_json(
            _static_evidence_path(root, row), "static objective evidence"
        )
        try:
            validate_public_static_grade_evidence(
                evidence,
                prepared,
                generation,
                saved_generation_result_sha256=str(receipt["generation_result_sha256"]),
                root=root,
            )
            canonical_grade = build_public_static_grade(
                prepared,
                generation,
                saved_generation_result_sha256=str(receipt["generation_result_sha256"]),
                root=root,
            )
        except Exception as exc:
            raise UnsupportedMemoryReviewError(
                f"static grade evidence is invalid: {exc}"
            ) from exc
        causes, details = _static_causes(evidence)
    elif suite == "temporal":
        try:
            canonical_grade = build_public_temporal_grade(prepared, generation)
        except Exception as exc:
            raise UnsupportedMemoryReviewError(
                f"temporal grade evidence is invalid: {exc}"
            ) from exc
        causes, details = _temporal_causes(row, receipt, prepared)
    else:
        raise UnsupportedMemoryReviewError("review row suite is invalid")
    grade = receipt.get("grade_artifact")
    outcome_fields = (
        "answer_status",
        "expected_answer_status",
        "is_correct",
        "stale_answer",
        "provenance_complete",
        "used_memory_claims",
        "relevant_memory_claim_ids",
        "correction_latency",
        "correction_latency_unit",
    )
    if (
        not isinstance(grade, Mapping)
        or dict(grade) != canonical_grade
        or receipt.get("grade_artifact_sha256")
        != canonical_grade.get("artifact_sha256")
        or any(
            receipt.get(field) != canonical_grade.get(field) for field in outcome_fields
        )
    ):
        raise UnsupportedMemoryReviewError(
            "source receipt differs from its canonical answer grade"
        )
    outcome_flags = ["provenance_incomplete"]
    if receipt.get("is_correct") is not True:
        outcome_flags.append("incorrect_answer")
    if receipt.get("stale_answer") is True:
        outcome_flags.append("stale_answer")
    if receipt.get("answer_status") != receipt.get("expected_answer_status"):
        outcome_flags.append("answer_status_mismatch")
    unsupported_detected = bool(row["unsupported_claims"])
    provenance_scopes, disposition_code, body_raw_complete = _scope_and_disposition(
        str(suite),
        causes,
        details,
        unsupported_memory_claim_detected=unsupported_detected,
    )
    disposition: dict[str, Any] = {
        "row_index": index,
        "run_id": row["run_id"],
        "task_id": row["task_id"],
        "suite": suite,
        "task_family": task.get("task_family"),
        "policy": row["policy"],
        "reasoning_effort": row["reasoning_effort"],
        "source_export_row_sha256": sha256_json(row),
        "source_result_sha256": receipt["result_sha256"],
        "source_grade_sha256": receipt["grade_artifact_sha256"],
        "answer_sha256": hashlib.sha256(str(row["answer"]).encode("utf-8")).hexdigest(),
        "export_trigger": "provenance_incomplete",
        "export_scope": _export_scope(row, details),
        "unsupported_memory_claim_detected": unsupported_detected,
        "claim_provenance_entry_count": len(row["claim_provenance"]),
        "failure_causes": causes,
        "provenance_scopes": provenance_scopes,
        "disposition_code": disposition_code,
        "body_raw_provenance_complete_ignoring_footer": body_raw_complete,
        "likely_content_false_positive": (
            suite == "temporal" and body_raw_complete and not unsupported_detected
        ),
        "evidence_details": details,
        "outcome_flags": outcome_flags,
        "review_bucket": _review_bucket(receipt),
        "disposition": "confirmed_export_contract_failure",
        "action": (
            "retain immutable failure evidence and exclude this answer from the "
            "provenance-complete set; do not repair the frozen experiment in place"
        ),
    }
    disposition["disposition_sha256"] = sha256_json(disposition)
    return disposition


def _counter(rows: Sequence[Mapping[str, Any]], field: str) -> dict[str, int]:
    counts = Counter(str(row[field]) for row in rows)
    return dict(sorted(counts.items()))


def _list_counter(rows: Sequence[Mapping[str, Any]], field: str) -> dict[str, int]:
    counts = Counter(str(item) for row in rows for item in row.get(field, []))
    return dict(sorted(counts.items()))


def build_unsupported_memory_review(
    metrics: Mapping[str, Any],
    *,
    reviewed_at: str,
    root: Path | None = None,
    expected_answer_count: int = EXPECTED_UNSUPPORTED_MEMORY_ANSWER_COUNT,
) -> dict[str, Any]:
    """Build one exhaustive, hash-bound disposition for every exported answer."""

    resolved_root = (root or repository_root()).resolve()
    normalized_time = _utc_second(reviewed_at, "reviewed_at")
    exported, source_rows = _source_export(metrics)
    if len(source_rows) != expected_answer_count:
        raise UnsupportedMemoryReviewError(
            f"expected {expected_answer_count} unsupported-memory answers"
        )
    dispositions = [
        _review_one(index, row, root=resolved_root)
        for index, row in enumerate(source_rows)
    ]
    if len({row["run_id"] for row in dispositions}) != len(dispositions):
        raise UnsupportedMemoryReviewError("review dispositions repeat a run ID")
    summary = {
        "disposition_count": len(dispositions),
        "unresolved_count": sum(
            row["disposition"] != "confirmed_export_contract_failure"
            for row in dispositions
        ),
        "unsupported_memory_claim_detected_count": sum(
            row["unsupported_memory_claim_detected"] for row in dispositions
        ),
        "by_suite": _counter(dispositions, "suite"),
        "by_policy": _counter(dispositions, "policy"),
        "by_reasoning_effort": _counter(dispositions, "reasoning_effort"),
        "by_task_family": _counter(dispositions, "task_family"),
        "by_export_scope": _counter(dispositions, "export_scope"),
        "by_disposition_code": _counter(dispositions, "disposition_code"),
        "by_primary_cause": dict(
            sorted(
                Counter(
                    str(row["evidence_details"]["primary_cause"])
                    for row in dispositions
                ).items()
            )
        ),
        "all_failure_cause_occurrences": _list_counter(dispositions, "failure_causes"),
        "by_review_bucket": _counter(dispositions, "review_bucket"),
        "likely_content_false_positive_count": sum(
            row["likely_content_false_positive"] for row in dispositions
        ),
        "outcome_flag_occurrences": _list_counter(dispositions, "outcome_flags"),
        "all_source_rows_disposed": True,
    }
    report: dict[str, Any] = {
        "schema_version": UNSUPPORTED_MEMORY_REVIEW_SCHEMA,
        "reviewed_at": normalized_time,
        "review_method": dict(REVIEW_METHOD),
        "sole_human_auditor": {
            "reviewer": "Kevin Araujo",
            "status": "pending",
            "scope": "full report and all source-row evidence",
        },
        "source_public_metrics_sha256": metrics["artifact_sha256"],
        "source_export_sha256": exported["artifact_sha256"],
        "source_answer_count": len(source_rows),
        "historical_export_name_note": (
            "The frozen export includes every completed M1-M4 answer with "
            "incomplete answer provenance. It is not limited to unsupported "
            "memory claims. Each disposition records the narrower failure scope."
        ),
        "summary": summary,
        "dispositions": dispositions,
    }
    report["artifact_sha256"] = sha256_json(report)
    return report


def validate_unsupported_memory_review(
    value: Mapping[str, Any],
    *,
    metrics: Mapping[str, Any],
    root: Path | None = None,
    expected_answer_count: int = EXPECTED_UNSUPPORTED_MEMORY_ANSWER_COUNT,
) -> None:
    """Rebuild every disposition and reject changed or fabricated classifications."""

    report = _object(value, "unsupported-memory review")
    _hash_checked(
        report,
        schema=UNSUPPORTED_MEMORY_REVIEW_SCHEMA,
        label="unsupported-memory review",
    )
    expected = build_unsupported_memory_review(
        metrics,
        reviewed_at=report.get("reviewed_at"),
        root=root,
        expected_answer_count=expected_answer_count,
    )
    if report != expected:
        raise UnsupportedMemoryReviewError(
            "unsupported-memory review differs from canonical evidence"
        )


def build_kevin_unsupported_memory_review_approval(
    report: Mapping[str, Any],
    *,
    expected_review_sha256: str,
    approved_at: str,
) -> dict[str, Any]:
    """Bind Kevin's approval to the exact exhaustive disposition report."""

    _hash_checked(
        report,
        schema=UNSUPPORTED_MEMORY_REVIEW_SCHEMA,
        label="unsupported-memory review",
    )
    if (
        _SHA256.fullmatch(expected_review_sha256) is None
        or report.get("artifact_sha256") != expected_review_sha256
    ):
        raise UnsupportedMemoryReviewError(
            "Kevin approval does not name the exact review artifact"
        )
    approval: dict[str, Any] = {
        "schema_version": UNSUPPORTED_MEMORY_REVIEW_APPROVAL_SCHEMA,
        "reviewer": "Kevin Araujo",
        "status": "approved",
        "approved_at": _utc_second(approved_at, "approved_at"),
        "review_artifact_sha256": expected_review_sha256,
        "source_public_metrics_sha256": report["source_public_metrics_sha256"],
        "source_export_sha256": report["source_export_sha256"],
        "disposition_count": report["summary"]["disposition_count"],
    }
    approval["artifact_sha256"] = sha256_json(approval)
    return approval


def validate_kevin_unsupported_memory_review_approval(
    value: Mapping[str, Any], *, report: Mapping[str, Any]
) -> None:
    """Require an exact, whole-second, report-bound Kevin approval."""

    approval = _object(value, "Kevin unsupported-memory review approval")
    _hash_checked(
        approval,
        schema=UNSUPPORTED_MEMORY_REVIEW_APPROVAL_SCHEMA,
        label="Kevin unsupported-memory review approval",
    )
    expected = build_kevin_unsupported_memory_review_approval(
        report,
        expected_review_sha256=str(approval.get("review_artifact_sha256", "")),
        approved_at=str(approval.get("approved_at", "")),
    )
    if approval != expected:
        raise UnsupportedMemoryReviewError(
            "Kevin unsupported-memory review approval differs from its report"
        )
