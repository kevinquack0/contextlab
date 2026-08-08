"""Evidence-bound grading and receipt construction for public G3 cells."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
import re
import unicodedata
from typing import Any

from .g3_execution import validate_prepared_public_g3_cell
from .g3_static_grading import (
    build_public_static_grade as build_public_static_objective_grade,
)
from .memory_experiments import (
    build_answer_grade_artifact,
    build_memory_result_receipt,
)
from .retrieval import estimate_tokens
from .review import REVIEWERS, aggregate_panel_grades, validate_grade
from .tasking import sha256_json
from .temporal import (
    all_events,
    event_history_sha256,
    expected_answer_for_question,
    temporal_question_catalog,
)


ANSWER_FOOTER_SCHEMA = "contextlab.g3-answer-footer.v1"
STATIC_PANEL_CELL_SCHEMA = "contextlab.g3-static-panel-cell.v1"
_CLAIM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_CITATION = re.compile(r"\[([^\]\n]{1,200})\]")
_INSUFFICIENT = re.compile(
    r"\b(?:insufficient|cannot determine|can't determine|unable to determine|"
    r"not enough evidence|no active (?:claim|evidence|record)|"
    r"no current (?:claim|evidence|record)|evidence (?:is )?(?:absent|unavailable)|"
    r"does not establish|do not establish|abstain)\b",
    re.IGNORECASE,
)


class G3GradingError(ValueError):
    """A G3 outcome cannot be derived from its committed evidence."""


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise G3GradingError(f"invalid temporal grade time: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise G3GradingError("temporal grade time requires an explicit timezone")
    return parsed.astimezone(timezone.utc)


def _normalized_text(value: object) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(normalized.split())


def _contains_value(answer_body: str, value: object) -> bool:
    needle = _normalized_text(value)
    if not needle:
        return False
    haystack = _normalized_text(_CITATION.sub(" ", answer_body))
    return (
        re.search(rf"(?<![\w]){re.escape(needle)}(?![\w])", haystack, re.UNICODE)
        is not None
    )


def _cites_raw(reference: str, raw_id: str) -> bool:
    return reference == raw_id or reference.endswith(f"#{raw_id}")


def parse_answer_footer(
    answer: str, selected_memory_claim_ids: Sequence[str]
) -> dict[str, Any]:
    """Parse the exact two-line footer and bind it to cited selected claims."""

    if not isinstance(answer, str) or not answer.strip():
        raise G3GradingError("completed G3 answer is empty")
    lines = answer.rstrip().splitlines()
    if len(lines) < 3:
        raise G3GradingError("G3 answer lacks the exact two-line footer")
    status_match = re.fullmatch(r"ANSWER_STATUS: (answer|abstain)", lines[-2])
    claims_match = re.fullmatch(r"USED_MEMORY_CLAIMS: (.+)", lines[-1])
    if status_match is None or claims_match is None:
        raise G3GradingError("G3 answer footer differs from the frozen prompt")
    body = "\n".join(lines[:-2]).strip()
    if not body:
        raise G3GradingError("G3 answer body is empty")
    claim_text = claims_match.group(1)
    if claim_text == "none":
        claim_ids: list[str] = []
    else:
        claim_ids = [item.strip() for item in claim_text.split(",")]
        if any(
            not item or _CLAIM_ID.fullmatch(item) is None for item in claim_ids
        ) or len(claim_ids) != len(set(claim_ids)):
            raise G3GradingError("G3 answer footer memory claims are invalid")
    selected = set(selected_memory_claim_ids)
    citations = set(_CITATION.findall(body))
    cited_selected = selected.intersection(citations)
    if not set(claim_ids).issubset(selected):
        raise G3GradingError("G3 answer lists a memory claim outside the trace")
    if set(claim_ids) != cited_selected:
        raise G3GradingError("G3 answer memory footer and body citations do not match")
    return {
        "schema_version": ANSWER_FOOTER_SCHEMA,
        "answer_status": status_match.group(1),
        "used_memory_claim_ids": claim_ids,
        "body": body,
        "citations": sorted(citations),
    }


def _recover_invalid_temporal_footer(
    answer: str, selected_memory_claim_ids: Sequence[str]
) -> dict[str, Any]:
    """Recover safe metric fields without treating a malformed footer as valid."""

    if not isinstance(answer, str) or not answer.strip():
        raise G3GradingError("completed G3 answer is empty")
    status = next(
        (
            match.group(1)
            for line in reversed(answer.rstrip().splitlines())
            if (
                match := re.fullmatch(
                    r"\s*ANSWER_STATUS\s*:\s*(answer|abstain)\s*", line
                )
            )
            is not None
        ),
        None,
    )
    if status is None:
        status = "abstain" if _INSUFFICIENT.search(answer) is not None else "answer"
    citations = sorted(set(_CITATION.findall(answer)))
    selected = set(selected_memory_claim_ids)
    return {
        "schema_version": ANSWER_FOOTER_SCHEMA,
        "answer_status": status,
        "used_memory_claim_ids": sorted(selected.intersection(citations)),
        "body": answer.strip(),
        "citations": citations,
    }


def _generation_identity(
    prepared_cell: Mapping[str, Any], generation_result: Mapping[str, Any]
) -> tuple[Mapping[str, Any], Mapping[str, Any], str]:
    validate_prepared_public_g3_cell(prepared_cell)
    spec = prepared_cell["run_spec"]
    trace = prepared_cell["memory_trace"]
    task = spec["task"]
    if (
        not isinstance(generation_result, Mapping)
        or set(generation_result)
        != {"schema_version", "run_id", "task_id", "answer", "metadata"}
        or generation_result.get("schema_version") != "contextlab.generation-result.v1"
        or generation_result.get("run_id") != spec.get("run_id")
        or generation_result.get("task_id") != task.get("task_id")
        or not isinstance(generation_result.get("answer"), str)
        or not isinstance(generation_result.get("metadata"), Mapping)
    ):
        raise G3GradingError("saved G3 generation result identity is invalid")
    return spec, trace, str(generation_result["answer"])


def _selected_claims(trace: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    selected = trace.get("selected_memory_evidence")
    if not isinstance(selected, list):
        raise G3GradingError("G3 memory trace has no selected claim list")
    rows: dict[str, Mapping[str, Any]] = {}
    for row in selected:
        if not isinstance(row, Mapping):
            raise G3GradingError("G3 memory trace contains an invalid claim")
        claim_id = row.get("claim_id")
        raw_ids = row.get("raw_evidence_ids")
        if (
            not isinstance(claim_id, str)
            or not claim_id
            or claim_id in rows
            or not isinstance(raw_ids, list)
            or not raw_ids
            or any(not isinstance(item, str) or not item for item in raw_ids)
        ):
            raise G3GradingError("G3 selected memory claim identity is invalid")
        rows[claim_id] = row
    return rows


def _canonical_used_claims(
    footer: Mapping[str, Any], selected: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    return [
        {
            "claim_id": claim_id,
            "supporting_event_ids": sorted(
                {str(item) for item in selected[claim_id]["raw_evidence_ids"]}
            ),
        }
        for claim_id in footer["used_memory_claim_ids"]
    ]


def _temporal_relevant_events(
    task_id: str, observable_ids: Sequence[str]
) -> tuple[Any, list[Any], set[str]]:
    question = next(
        (item for item in temporal_question_catalog() if item.task_id == task_id),
        None,
    )
    if question is None or question.is_sealed:
        raise G3GradingError("deterministic grading requires a public temporal task")
    observable = set(observable_ids)
    events = [event for event in all_events() if event.event_id in observable]
    assertions = [
        event
        for event in events
        if event.subject == question.subject and event.predicate == question.predicate
    ]
    assertion_ids = {event.event_id for event in assertions}
    lifecycle = [
        event for event in events if event.tombstone_for_event_id in assertion_ids
    ]
    relevant = sorted(
        {event.event_id: event for event in (*assertions, *lifecycle)}.values(),
        key=lambda event: (_time(event.observed_time), event.event_id),
    )
    determining = {
        event.event_id
        for event in lifecycle
        if event.status in {"expired", "retracted", "tombstone"}
    }
    return question, relevant, determining


def _raw_provenance_complete(
    footer: Mapping[str, Any],
    trace: Mapping[str, Any],
    selected: Mapping[str, Mapping[str, Any]],
    used_claims: Sequence[Mapping[str, Any]],
    required_raw_ids: set[str],
) -> bool:
    citations = set(footer["citations"])
    selected_corpus = trace.get("selected_corpus_evidence")
    if not isinstance(selected_corpus, list):
        return False
    raw_in_context = {
        str(raw_id)
        for row in selected_corpus
        if isinstance(row, Mapping)
        for raw_id in row.get("raw_evidence_ids", [])
    }
    for claim in used_claims:
        claim_id = str(claim["claim_id"])
        raw_ids = {str(item) for item in claim["supporting_event_ids"]}
        if (
            claim_id not in selected
            or not raw_ids
            or not raw_ids.issubset(raw_in_context)
            or claim_id not in citations
            or not all(
                any(_cites_raw(reference, raw_id) for reference in citations)
                for raw_id in raw_ids
            )
        ):
            return False
    return not required_raw_ids or all(
        any(_cites_raw(reference, raw_id) for reference in citations)
        for raw_id in required_raw_ids
    )


def _stale_temporal_answer(
    answer_body: str,
    answer_status: str,
    expected: Any,
    relevant_events: Sequence[Any],
) -> bool:
    if answer_status != "answer":
        return False
    expected_event = next(
        (
            event
            for event in relevant_events
            if event.event_id in expected.supporting_event_ids
        ),
        None,
    )
    for event in relevant_events:
        if event.status not in {"draft", "final", "corrected"}:
            continue
        if expected.value is not None and _normalized_text(
            event.value
        ) == _normalized_text(expected.value):
            continue
        is_prior = expected_event is None or (
            _time(event.effective_time),
            _time(event.observed_time),
            event.event_id,
        ) < (
            _time(expected_event.effective_time),
            _time(expected_event.observed_time),
            expected_event.event_id,
        )
        if is_prior and _contains_value(answer_body, event.value):
            return True
    return False


def _correction_latency(
    *,
    is_correct: bool,
    stale_answer: bool,
    expected: Any,
    relevant_events: Sequence[Any],
    lifecycle_determining_ids: set[str],
) -> float | None:
    if is_correct:
        return 0.0
    if not stale_answer:
        return None
    determining_ids = set(expected.supporting_event_ids) or lifecycle_determining_ids
    indices = [
        index
        for index, event in enumerate(relevant_events)
        if event.event_id in determining_ids
    ]
    if not indices:
        return 1.0
    return float(max(1, len(relevant_events) - 1 - max(indices)))


def build_public_temporal_grade(
    prepared_cell: Mapping[str, Any], generation_result: Mapping[str, Any]
) -> dict[str, Any]:
    """Derive a public temporal grade from the answer key and saved trace."""

    spec, trace, answer = _generation_identity(prepared_cell, generation_result)
    task = spec["task"]
    if task.get("suite") != "temporal":
        raise G3GradingError("temporal grader received a non-temporal G3 cell")
    selected = _selected_claims(trace)
    footer_valid = True
    try:
        footer = parse_answer_footer(answer, sorted(selected))
    except G3GradingError:
        footer_valid = False
        footer = _recover_invalid_temporal_footer(answer, sorted(selected))
    used_claims = _canonical_used_claims(footer, selected)
    expected = expected_answer_for_question(str(task["task_id"]))
    _question, relevant_events, lifecycle_ids = _temporal_relevant_events(
        str(task["task_id"]), prepared_cell["observable_event_ids"]
    )
    if expected.status == "answer":
        is_correct = (
            footer_valid
            and footer["answer_status"] == "answer"
            and _contains_value(str(footer["body"]), expected.value)
        )
        required_raw_ids = set(expected.supporting_event_ids)
    else:
        is_correct = (
            footer_valid
            and footer["answer_status"] == "abstain"
            and _INSUFFICIENT.search(str(footer["body"])) is not None
        )
        required_raw_ids = lifecycle_ids
    stale_answer = (
        False
        if is_correct
        else _stale_temporal_answer(
            str(footer["body"]),
            str(footer["answer_status"]),
            expected,
            relevant_events,
        )
    )
    relevant_claim_ids = sorted(
        claim_id
        for claim_id, row in selected.items()
        if set(row["raw_evidence_ids"]).intersection(expected.supporting_event_ids)
    )
    provenance_complete = footer_valid and _raw_provenance_complete(
        footer, trace, selected, used_claims, required_raw_ids
    )
    return build_answer_grade_artifact(
        spec,
        trace,
        prepared_cell_artifact_sha256=str(prepared_cell["artifact_sha256"]),
        generation_result_sha256=sha256_json(generation_result),
        answer=answer,
        grader_id="deterministic",
        grade_basis="public-temporal-answer-key-v1",
        source_grade_sha256s=[event_history_sha256()],
        answer_status=str(footer["answer_status"]),
        expected_answer_status=expected.status,
        is_correct=is_correct,
        stale_answer=stale_answer,
        provenance_complete=provenance_complete,
        used_memory_claims=used_claims,
        relevant_memory_claim_ids=relevant_claim_ids,
        correction_latency=_correction_latency(
            is_correct=is_correct,
            stale_answer=stale_answer,
            expected=expected,
            relevant_events=relevant_events,
            lifecycle_determining_ids=lifecycle_ids,
        ),
    )


def _validated_static_panel_cell(
    panel_cell: Mapping[str, Any],
    *,
    task_id: str,
    answer: str,
    panel_aggregate_sha256: str,
) -> tuple[dict[str, Any], list[str]]:
    if not _is_sha256(panel_aggregate_sha256):
        raise G3GradingError("static panel aggregate hash is invalid")
    expected_fields = {
        "canonical_cell_id",
        "task_id",
        "candidate_sha256",
        "individual_grades",
        "aggregate",
    }
    grades = panel_cell.get("individual_grades")
    aggregate = panel_cell.get("aggregate")
    answer_sha256 = hashlib.sha256(answer.encode("utf-8")).hexdigest()
    if (
        set(panel_cell) != expected_fields
        or panel_cell.get("task_id") != task_id
        or panel_cell.get("candidate_sha256") != answer_sha256
        or not isinstance(panel_cell.get("canonical_cell_id"), str)
        or not panel_cell["canonical_cell_id"]
        or not isinstance(grades, Mapping)
        or set(grades) != set(REVIEWERS)
        or not isinstance(aggregate, Mapping)
    ):
        raise G3GradingError("static panel cell identity is invalid")
    canonical_grades: dict[str, dict[str, Any]] = {}
    for reviewer in REVIEWERS:
        grade = grades.get(reviewer)
        if not isinstance(grade, dict):
            raise G3GradingError("static panel grade is invalid")
        try:
            validate_grade(grade)
        except ValueError as exc:
            raise G3GradingError("static panel grade is invalid") from exc
        canonical_grades[reviewer] = grade
    try:
        expected_aggregate = aggregate_panel_grades(canonical_grades)
    except ValueError as exc:
        raise G3GradingError("static panel aggregation is invalid") from exc
    if dict(aggregate) != expected_aggregate:
        raise G3GradingError("static panel aggregate differs from its reviewers")
    source_hashes = [
        sha256_json(
            {
                "schema_version": STATIC_PANEL_CELL_SCHEMA,
                "panel_aggregate_sha256": panel_aggregate_sha256,
                "canonical_cell_id": panel_cell["canonical_cell_id"],
                "task_id": task_id,
                "candidate_sha256": answer_sha256,
                "reviewer_id": reviewer,
                "grade": canonical_grades[reviewer],
            }
        )
        for reviewer in REVIEWERS
    ]
    return expected_aggregate, source_hashes


def build_public_static_grade(
    prepared_cell: Mapping[str, Any],
    generation_result: Mapping[str, Any],
    *,
    panel_cell: Mapping[str, Any],
    panel_aggregate_sha256: str,
) -> dict[str, Any]:
    """Derive a static result only from the complete GPT/Claude/Kevin panel."""

    spec, trace, answer = _generation_identity(prepared_cell, generation_result)
    task = spec["task"]
    if task.get("suite") != "static":
        raise G3GradingError("static panel grader received a temporal G3 cell")
    selected = _selected_claims(trace)
    footer = parse_answer_footer(answer, sorted(selected))
    used_claims = _canonical_used_claims(footer, selected)
    aggregate, source_hashes = _validated_static_panel_cell(
        panel_cell,
        task_id=str(task["task_id"]),
        answer=answer,
        panel_aggregate_sha256=panel_aggregate_sha256,
    )
    abstention_quality = str(aggregate["abstention_quality"])
    if abstention_quality == "no_majority":
        raise G3GradingError("static panel has no abstention majority")
    expected_status = "abstain" if abstention_quality == "correct" else "answer"
    return build_answer_grade_artifact(
        spec,
        trace,
        prepared_cell_artifact_sha256=str(prepared_cell["artifact_sha256"]),
        generation_result_sha256=sha256_json(generation_result),
        answer=answer,
        grader_id="panel-majority",
        grade_basis="blind-panel-majority-v1",
        source_grade_sha256s=source_hashes,
        answer_status=str(footer["answer_status"]),
        expected_answer_status=expected_status,
        is_correct=bool(aggregate["accepted"]),
        stale_answer=False,
        provenance_complete=int(aggregate["citation_support"]) >= 2,
        used_memory_claims=used_claims,
        relevant_memory_claim_ids=[],
        correction_latency=None,
    )


def _memory_write_metrics(prepared_cell: Mapping[str, Any]) -> tuple[int, int]:
    decisions = prepared_cell["decision_ledger"]
    writes = [
        row
        for row in decisions
        if row.get("kind") == "event"
        and row.get("decision") in {"write", "merge", "conflict", "tombstone"}
    ]
    snapshot = prepared_cell["memory_snapshot"]
    records = [*snapshot.get("claims", []), *snapshot.get("episodes", [])]
    token_count = sum(
        max(
            1,
            estimate_tokens(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ),
        )
        for row in records
    )
    return len(writes), token_count


def build_public_g3_result_receipt(
    prepared_cell: Mapping[str, Any],
    generation_result: Mapping[str, Any] | None,
    *,
    frozen_manifest: Mapping[str, Any],
    trusted_frozen_manifest_sha256: str,
    panel_cell: Mapping[str, Any] | None = None,
    panel_aggregate_sha256: str | None = None,
    failure: str | None = None,
) -> dict[str, Any]:
    """Build one receipt without accepting caller-supplied score or accounting fields."""

    validate_prepared_public_g3_cell(prepared_cell)
    spec = prepared_cell["run_spec"]
    trace = prepared_cell["memory_trace"]
    writes, write_tokens = _memory_write_metrics(prepared_cell)
    if generation_result is None:
        if panel_cell is not None or panel_aggregate_sha256 is not None:
            raise G3GradingError("failed G3 result cannot carry a panel grade")
        return build_memory_result_receipt(
            spec,
            trace,
            frozen_manifest=frozen_manifest,
            trusted_frozen_manifest_sha256=trusted_frozen_manifest_sha256,
            prepared_cell_artifact_sha256=str(prepared_cell["artifact_sha256"]),
            generation_result=None,
            grade_artifact=None,
            memory_write_count=writes,
            memory_write_tokens=write_tokens,
            status="failed",
            failure=failure,
        )
    if failure is not None:
        raise G3GradingError("completed G3 result cannot carry a failure")
    if spec["task"]["suite"] == "temporal":
        if panel_cell is not None or panel_aggregate_sha256 is not None:
            raise G3GradingError("public temporal grade cannot be panel-supplied")
        grade = build_public_temporal_grade(prepared_cell, generation_result)
    else:
        if (panel_cell is None) != (panel_aggregate_sha256 is None):
            raise G3GradingError("static G3 panel inputs must appear together")
        if panel_cell is None:
            grade = build_public_static_objective_grade(
                prepared_cell,
                generation_result,
                saved_generation_result_sha256=sha256_json(generation_result),
            )
        else:
            assert panel_aggregate_sha256 is not None
            grade = build_public_static_grade(
                prepared_cell,
                generation_result,
                panel_cell=panel_cell,
                panel_aggregate_sha256=panel_aggregate_sha256,
            )
    return build_memory_result_receipt(
        spec,
        trace,
        frozen_manifest=frozen_manifest,
        trusted_frozen_manifest_sha256=trusted_frozen_manifest_sha256,
        prepared_cell_artifact_sha256=str(prepared_cell["artifact_sha256"]),
        generation_result=generation_result,
        grade_artifact=grade,
        memory_write_count=writes,
        memory_write_tokens=write_tokens,
    )
