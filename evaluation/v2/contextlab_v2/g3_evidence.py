"""Public, gold-free evidence preparation for the G3 memory experiment."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from .baseline import repository_root
from .experiments import (
    chunk_embedding_text,
    load_frozen_chunks,
    load_protocol,
    run_task_ladder,
)
from .memory import Episode, MemoryRead
from .reports import validate_lab
from .retrieval import estimate_tokens
from .static_benchmark import public_static_tasks
from .tasking import sha256_json
from .temporal import (
    CorpusEvent,
    TemporalScenario,
    all_events,
    event_history_sha256,
    public_temporal_tasks,
    scenario_catalog,
    temporal_question_catalog,
)


TEMPORAL_R0_LAB_SCHEMA = "contextlab.g3-temporal-r0-lab.v1"
PROMPT_TASK_SCHEMA = "contextlab.prompt-task.v1"
RETRIEVER_ID = "R0"


class G3EvidenceError(ValueError):
    """Public evidence cannot be bound to the retained G2 retriever."""


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _event_text(event: CorpusEvent) -> str:
    fields = (
        ("Event", event.event_id),
        ("Scenario", event.scenario_id),
        ("Subject", event.subject),
        ("Predicate", event.predicate),
        (
            "Value",
            json.dumps(
                event.value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
        ("Status", event.status),
        ("Authority level", str(event.authority_level)),
        ("Observed time", event.observed_time),
        ("Published time", event.published_time or "not published"),
        ("Effective time", event.effective_time),
        ("Valid from", event.valid_from),
        ("Valid to", event.valid_to or "open"),
        ("Supersedes", event.supersedes_event_id or "none"),
        ("Lifecycle target", event.tombstone_for_event_id or "none"),
    )
    return "\n".join(f"{label}: {value}" for label, value in fields)


def temporal_event_chunks(
    scenarios: Iterable[TemporalScenario] | None = None,
) -> list[dict[str, Any]]:
    """Project public CorpusEvents into the exact chunk shape used by R0."""

    scenario_rows = tuple(scenarios or scenario_catalog())
    titles = {scenario.scenario_id: scenario.title for scenario in scenario_rows}
    chunks = [
        {
            "chunk_id": event.event_id,
            "source_id": event.source_id,
            # Event IDs are the raw-provenance identity used by memory claims.
            "section_id": event.event_id,
            "title": titles[event.scenario_id],
            "heading": f"{titles[event.scenario_id]} — {event.event_id}",
            "text": _event_text(event),
            "content_hash": event.content_hash,
            "authority_level": event.authority_level,
            "publication_date": (event.published_time or event.observed_time),
            "status": event.status,
            "effective_time": event.effective_time,
        }
        for event in all_events(scenario_rows)
    ]
    ids = [str(row["chunk_id"]) for row in chunks]
    if len(ids) != len(set(ids)) or any(
        not _is_sha256(row["content_hash"]) for row in chunks
    ):
        raise G3EvidenceError("temporal event chunk identity is invalid")
    return chunks


def temporal_corpus_snapshot_sha256(
    scenarios: Iterable[TemporalScenario] | None = None,
) -> str:
    return sha256_json(temporal_event_chunks(scenarios))


def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise G3EvidenceError(f"invalid temporal snapshot time: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise G3EvidenceError("temporal snapshot time requires an explicit timezone")
    return parsed.astimezone(timezone.utc)


def temporal_task_snapshot_time(task_id: str) -> str:
    """Return the public knowledge cutoff used by corpus and memory retrieval."""

    question = next(
        (row for row in temporal_question_catalog() if row.task_id == task_id), None
    )
    if question is None or question.is_sealed:
        raise G3EvidenceError("temporal task is not on the public G3 surface")
    if question.snapshot_time is not None:
        return question.snapshot_time
    events = all_events()
    return max(
        events, key=lambda event: (_time(event.observed_time), event.event_id)
    ).observed_time


def observable_temporal_event_chunks(task_id: str) -> list[dict[str, Any]]:
    """Filter the organization event corpus by observation and publication time."""

    observable_ids = {event.event_id for event in observable_temporal_events(task_id)}
    chunks = [
        chunk
        for chunk in temporal_event_chunks()
        if str(chunk["chunk_id"]) in observable_ids
    ]
    if not chunks:
        raise G3EvidenceError(
            "public temporal snapshot has no observable corpus events"
        )
    return chunks


def observable_temporal_events(task_id: str) -> tuple[CorpusEvent, ...]:
    """Return the exact organization-wide events visible at one task cutoff."""

    cutoff = _time(temporal_task_snapshot_time(task_id))
    return tuple(
        event
        for event in all_events()
        if _time(event.observed_time) <= cutoff
        and event.published_time is not None
        and _time(event.published_time) <= cutoff
    )


def g3_embedding_inputs() -> list[str]:
    """Return the exact public temporal query and event strings needed by R0."""

    values = [
        *(str(task["question_text"]) for task in public_temporal_tasks()),
        *(chunk_embedding_text(chunk) for chunk in temporal_event_chunks()),
    ]
    return list(dict.fromkeys(values))


def _prompt_task(task: Mapping[str, Any]) -> dict[str, str]:
    question = task.get("question_text")
    digest = task.get("question_sha256")
    if (
        task.get("suite") != "temporal"
        or not isinstance(question, str)
        or not question
        or not _is_sha256(digest)
        or hashlib.sha256(question.encode("utf-8")).hexdigest() != digest
    ):
        raise G3EvidenceError("public temporal task identity is invalid")
    return {
        "schema_version": PROMPT_TASK_SCHEMA,
        "task_id": str(task["task_id"]),
        "suite": "temporal",
        "question_text": question,
        "question_sha256": str(digest),
    }


def build_temporal_r0_lab(
    embeddings: Mapping[str, Sequence[float]],
    *,
    root: Path | None = None,
    protocol: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run only the retained R0 trace over all 28 public temporal tasks."""

    root = (root or repository_root()).resolve()
    frozen_protocol = dict(protocol or load_protocol(root))
    corpus_sha256 = temporal_corpus_snapshot_sha256()
    traces: list[dict[str, Any]] = []
    for task in public_temporal_tasks():
        chunks = observable_temporal_event_chunks(str(task["task_id"]))
        ladder = run_task_ladder(
            _prompt_task(task),
            chunks,
            embeddings,
            (),
            frozen_protocol,
            corpus_snapshot_id=sha256_json(chunks),
        )
        trace = next(
            (row for row in ladder if row["strategy_id"] == RETRIEVER_ID), None
        )
        if trace is None:
            raise G3EvidenceError("retained R0 trace is missing")
        traces.append(trace)
    payload: dict[str, Any] = {
        "schema_version": TEMPORAL_R0_LAB_SCHEMA,
        "retriever_id": RETRIEVER_ID,
        "retriever_protocol_sha256": sha256_json(frozen_protocol),
        "event_history_sha256": event_history_sha256(),
        "temporal_corpus_snapshot_sha256": corpus_sha256,
        "task_count": len(traces),
        "trace_count": len(traces),
        "traces": sorted(traces, key=lambda row: str(row["task"]["task_id"])),
    }
    payload["artifact_sha256"] = sha256_json(payload)
    validate_temporal_r0_lab(payload)
    return payload


def validate_temporal_r0_lab(value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping):
        raise G3EvidenceError("temporal R0 lab must be an object")
    body = {key: item for key, item in value.items() if key != "artifact_sha256"}
    if (
        value.get("schema_version") != TEMPORAL_R0_LAB_SCHEMA
        or value.get("artifact_sha256") != sha256_json(body)
        or value.get("retriever_id") != RETRIEVER_ID
        or not _is_sha256(value.get("retriever_protocol_sha256"))
        or value.get("event_history_sha256") != event_history_sha256()
        or value.get("temporal_corpus_snapshot_sha256")
        != temporal_corpus_snapshot_sha256()
    ):
        raise G3EvidenceError("temporal R0 lab commitment is invalid")
    traces = value.get("traces")
    expected_tasks = {
        str(row["task_id"]): _prompt_task(row) for row in public_temporal_tasks()
    }
    if (
        not isinstance(traces, list)
        or value.get("task_count") != 28
        or value.get("trace_count") != 28
        or len(traces) != 28
    ):
        raise G3EvidenceError("temporal R0 lab coverage is incomplete")
    observed: set[str] = set()
    for trace in traces:
        if not isinstance(trace, Mapping) or not isinstance(trace.get("task"), Mapping):
            raise G3EvidenceError("temporal R0 trace is invalid")
        task_id = str(trace["task"].get("task_id"))
        selected = trace.get("selected_candidates")
        passages = trace.get("candidate_passages")
        rendered = trace.get("rendered_context")
        observable_chunks = observable_temporal_event_chunks(task_id)
        observable_ids = {str(row["chunk_id"]) for row in observable_chunks}
        if (
            task_id in observed
            or trace.get("strategy_id") != RETRIEVER_ID
            or trace.get("task") != expected_tasks.get(task_id)
            or trace.get("protocol_sha256") != value["retriever_protocol_sha256"]
            or trace.get("corpus_snapshot_id") != sha256_json(observable_chunks)
            or not isinstance(selected, list)
            or not selected
            or not isinstance(passages, Mapping)
            or not isinstance(rendered, str)
            or trace.get("rendered_context_sha256")
            != hashlib.sha256(rendered.encode("utf-8")).hexdigest()
            or trace.get("context_tokens") != estimate_tokens(rendered)
            or {str(row.get("candidate_id")) for row in selected} != set(passages)
            or not {
                str(row.get("section_id") or row.get("source_id")) for row in selected
            }.issubset(observable_ids)
        ):
            raise G3EvidenceError("temporal R0 trace commitment is invalid")
        observed.add(task_id)
    if observed != set(expected_tasks):
        raise G3EvidenceError("temporal R0 task surface changed")


def static_r0_trace_index(lab: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Return the exact 84 public static R0 traces from the accepted G2 lab."""

    validate_lab(lab)
    expected = {str(task["task_id"]) for task in public_static_tasks()}
    rows = {
        str(trace["task"]["task_id"]): trace
        for trace in lab["traces"]
        if trace.get("strategy_id") == RETRIEVER_ID
        and str(trace.get("task", {}).get("task_id")) in expected
    }
    if set(rows) != expected or len(rows) != 84:
        raise G3EvidenceError("G2 lab does not contain the frozen 84 static R0 traces")
    return rows


def public_raw_evidence_ids(root: Path | None = None) -> list[str]:
    """Freeze every raw public identifier that a G3 claim may cite."""

    root = (root or repository_root()).resolve()
    static_ids = {
        str(chunk.get("section_id") or chunk.get("source_id"))
        for chunk in load_frozen_chunks(root)
    }
    temporal_ids = {event.event_id for event in all_events()}
    values = sorted(static_ids | temporal_ids)
    if not values or any(not value for value in values):
        raise G3EvidenceError("public raw evidence registry is invalid")
    return values


def trace_corpus_evidence(
    trace: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Convert a retained-R0 trace to G3 candidates and prompt blocks."""

    selected = trace.get("selected_candidates")
    passages = trace.get("candidate_passages")
    if not isinstance(selected, list) or not isinstance(passages, Mapping):
        raise G3EvidenceError("R0 trace lacks selected passages")
    rows: list[dict[str, Any]] = []
    blocks: dict[str, str] = {}
    for candidate in selected:
        if not isinstance(candidate, Mapping):
            raise G3EvidenceError("R0 selected candidate is invalid")
        evidence_id = str(candidate.get("candidate_id", ""))
        reference = str(candidate.get("text_reference", ""))
        raw_id = str(candidate.get("section_id") or candidate.get("source_id") or "")
        passage = passages.get(evidence_id)
        if (
            not evidence_id
            or not reference
            or not raw_id
            or not isinstance(passage, str)
        ):
            raise G3EvidenceError("R0 candidate cannot resolve to raw evidence")
        block = f"[{reference}]\n{passage}"
        rows.append(
            {
                "evidence_id": evidence_id,
                "token_count": max(1, estimate_tokens(block)),
                "rank": int(candidate["rank"]),
                "raw_evidence_ids": [raw_id],
            }
        )
        blocks[evidence_id] = block
    return rows, blocks


def temporal_verification_corpus_evidence(
    task_id: str,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Expose every observable event as bounded raw-verification evidence."""

    rows: list[dict[str, Any]] = []
    blocks: dict[str, str] = {}
    for rank, chunk in enumerate(
        observable_temporal_event_chunks(task_id), start=1_000
    ):
        raw_id = str(chunk["section_id"])
        evidence_id = f"raw-{raw_id}"
        reference = f"{chunk['source_id']}#{raw_id}"
        block = f"[{reference}]\n{chunk['text']}"
        rows.append(
            {
                "evidence_id": evidence_id,
                "token_count": max(1, estimate_tokens(block)),
                "rank": rank,
                "raw_evidence_ids": [raw_id],
            }
        )
        blocks[evidence_id] = block
    return rows, blocks


def memory_read_evidence(
    read: MemoryRead,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Render selected and conflicting claims with direct event provenance."""

    claims = (*read.selected_claims, *read.conflict_claims)
    rows: list[dict[str, Any]] = []
    blocks: dict[str, str] = {}
    for rank, claim in enumerate(claims, start=1):
        raw = sorted(claim.supporting_event_ids)
        citations = " ".join(f"[{event_id}]" for event_id in raw)
        block = (
            f"[{claim.claim_id}] Memory claim\n"
            f"Subject: {claim.subject}\n"
            f"Predicate: {claim.predicate}\n"
            f"Value: {json.dumps(claim.value, ensure_ascii=False, sort_keys=True)}\n"
            f"State: {claim.state}\n"
            f"Authority level: {claim.authority_level}\n"
            f"Valid from: {claim.valid_from}\n"
            f"Valid to: {claim.valid_to or 'open'}\n"
            f"Raw evidence: {citations}"
        )
        rows.append(
            {
                "evidence_id": claim.claim_id,
                "claim_id": claim.claim_id,
                "token_count": max(1, estimate_tokens(block)),
                "rank": rank,
                "raw_evidence_ids": raw,
            }
        )
        blocks[claim.claim_id] = block
    return rows, blocks


def episode_card_block(episode: Episode) -> str:
    outcome = json.dumps(
        episode.graded_outcome,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    evidence = " ".join(f"[{item}]" for item in episode.evidence_path)
    return (
        f"[{episode.episode_id}] Prior graded episode\n"
        f"Task family: {episode.category}\n"
        f"Selected strategy: {episode.selected_strategy}\n"
        f"Objective outcome: {outcome}\n"
        f"Failure mode: {episode.failure_mode or 'none'}\n"
        f"Raw trace evidence: {evidence}\n"
        "Use this card only as strategy guidance; do not treat it as a current fact."
    )


def render_selected_context(
    trace: Mapping[str, Any],
    *,
    corpus_blocks: Mapping[str, str],
    memory_blocks: Mapping[str, str],
    episode_blocks: Mapping[str, str] | None = None,
) -> str:
    """Render exactly the candidates selected and budgeted by a G3 trace."""

    selected_groups = (
        ("selected_corpus_evidence", corpus_blocks),
        ("selected_memory_evidence", memory_blocks),
        ("selected_episode_evidence", episode_blocks or {}),
    )
    blocks: list[str] = []
    for field, lookup in selected_groups:
        rows = trace.get(field, [])
        if not isinstance(rows, list):
            raise G3EvidenceError(f"{field} must be a list")
        for row in rows:
            evidence_id = (
                str(row.get("evidence_id", "")) if isinstance(row, Mapping) else ""
            )
            block = lookup.get(evidence_id)
            if not evidence_id or not isinstance(block, str):
                raise G3EvidenceError(f"{field} cannot resolve selected evidence")
            blocks.append(block)
    if not blocks:
        raise G3EvidenceError("G3 context must retain at least one corpus block")
    rendered = "\n\n---\n\n".join(blocks)
    if estimate_tokens(rendered) > int(trace["context_budget_tokens"]):
        raise G3EvidenceError("rendered G3 context exceeds its frozen budget")
    return rendered
