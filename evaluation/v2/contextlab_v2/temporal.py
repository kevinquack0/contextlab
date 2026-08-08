"""Deterministic NovaLearn temporal scenarios and replayable answer keys.

This module is deliberately small and data-first.  It provides the Week 7 event
layer only; it does not choose a memory policy or send temporal gold to a model.
Sealed questions are represented by safe references and must be completed by the
external sealed evaluator.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Literal

from .baseline import repository_root
from .contracts import validate_instance
from .tasking import FORBIDDEN_PUBLIC_FIELDS, sha256_json


TEMPORAL_EVENT_SCHEMA = "contextlab.corpus-event.v1"
TEMPORAL_CLAIM_SCHEMA = "contextlab.claim.v1"
TEMPORAL_TASK_SCHEMA = "contextlab.temporal-task.v1"
SEALED_REFERENCE_SCHEMA = "contextlab.temporal-sealed-reference.v1"
ANSWER_SCHEMA = "contextlab.temporal-expected-answer.v1"
EVENT_SOURCE_RELATIVE_PATH = Path("novalearn_synthetic_corpus/v2/temporal_events.jsonl")

EventStatus = Literal[
    "draft", "final", "corrected", "retracted", "expired", "tombstone"
]
Partition = Literal["regression", "judge_calibration", "sealed_capability", "showcase"]


class TemporalContractError(ValueError):
    """A temporal event, question, or replay request is invalid."""


def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TemporalContractError(f"invalid timestamp: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TemporalContractError(f"timestamp requires an explicit timezone: {value}")
    return parsed.astimezone(timezone.utc)


def _time_text(value: str) -> str:
    parsed = _time(value)
    return parsed.isoformat().replace("+00:00", "Z")


def _event_key(event: "CorpusEvent") -> tuple[datetime, str]:
    return (_time(event.observed_time), event.event_id)


@dataclass(frozen=True)
class CorpusEvent:
    """An immutable, persistable source assertion or lifecycle action."""

    event_id: str
    scenario_id: str
    source_id: str
    section_id: str
    content_hash: str
    observed_time: str
    effective_time: str
    published_time: str | None
    valid_from: str
    valid_to: str | None
    authority_level: int
    status: EventStatus
    subject: str
    predicate: str
    value: Any
    supersedes_event_id: str | None
    tombstone_for_event_id: str | None
    source_text_reference: str

    def __post_init__(self) -> None:
        if (
            not self.event_id
            or not self.scenario_id
            or not self.subject
            or not self.predicate
        ):
            raise TemporalContractError(
                "event identifiers, subject, and predicate must be non-empty"
            )
        observed = _time(self.observed_time)
        effective = _time(self.effective_time)
        valid_from = _time(self.valid_from)
        if self.published_time is not None:
            _time(self.published_time)
        if self.valid_to is not None and _time(self.valid_to) <= valid_from:
            raise TemporalContractError("event valid_to must be after valid_from")
        if observed < effective and self.status not in {"draft", "final", "corrected"}:
            # Future-effective final policies are valid; lifecycle actions are not.
            raise TemporalContractError(
                "only assertions may be observed before their effective time"
            )
        if not 1 <= self.authority_level <= 5:
            raise TemporalContractError("event authority_level must be 1 through 5")
        if (
            self.status in {"tombstone", "retracted", "expired"}
            and not self.tombstone_for_event_id
        ):
            raise TemporalContractError(
                "lifecycle event requires tombstone_for_event_id"
            )
        if (
            self.status in {"draft", "final", "corrected"}
            and self.tombstone_for_event_id
        ):
            raise TemporalContractError("assertion cannot tombstone another event")

    @classmethod
    def create(
        cls,
        *,
        event_id: str,
        scenario_id: str,
        observed_time: str,
        effective_time: str,
        authority_level: int,
        status: EventStatus,
        subject: str,
        predicate: str,
        value: Any,
        published_time: str | None = None,
        valid_from: str | None = None,
        valid_to: str | None = None,
        supersedes_event_id: str | None = None,
        tombstone_for_event_id: str | None = None,
    ) -> "CorpusEvent":
        source_id = f"NLV2-{scenario_id}-{event_id.rsplit('-', 1)[-1]}"
        section_id = f"{source_id}-S01"
        normalized_observed = _time_text(observed_time)
        normalized_effective = _time_text(effective_time)
        normalized_published = _time_text(published_time) if published_time else None
        normalized_valid_from = (
            _time_text(valid_from) if valid_from is not None else normalized_effective
        )
        normalized_valid_to = _time_text(valid_to) if valid_to else None
        source_text_reference = f"{EVENT_SOURCE_RELATIVE_PATH.as_posix()}#{event_id}"
        content = {
            "event_id": event_id,
            "scenario_id": scenario_id,
            "source_id": source_id,
            "section_id": section_id,
            "observed_time": normalized_observed,
            "effective_time": normalized_effective,
            "published_time": normalized_published,
            "valid_from": normalized_valid_from,
            "valid_to": normalized_valid_to,
            "authority_level": authority_level,
            "status": status,
            "subject": subject,
            "predicate": predicate,
            "value": value,
            "supersedes_event_id": supersedes_event_id,
            "tombstone_for_event_id": tombstone_for_event_id,
        }
        return cls(
            event_id=event_id,
            scenario_id=scenario_id,
            source_id=source_id,
            section_id=section_id,
            content_hash=sha256_json(content),
            observed_time=normalized_observed,
            effective_time=normalized_effective,
            published_time=normalized_published,
            valid_from=normalized_valid_from,
            valid_to=normalized_valid_to,
            authority_level=authority_level,
            status=status,
            subject=subject,
            predicate=predicate,
            value=value,
            supersedes_event_id=supersedes_event_id,
            tombstone_for_event_id=tombstone_for_event_id,
            source_text_reference=source_text_reference,
        )

    def source_payload(self) -> dict[str, Any]:
        """Return the exact source-stream row committed by ``content_hash``."""
        return {
            "event_id": self.event_id,
            "scenario_id": self.scenario_id,
            "source_id": self.source_id,
            "section_id": self.section_id,
            "observed_time": self.observed_time,
            "effective_time": self.effective_time,
            "published_time": self.published_time,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "authority_level": self.authority_level,
            "status": self.status,
            "subject": self.subject,
            "predicate": self.predicate,
            "value": self.value,
            "supersedes_event_id": self.supersedes_event_id,
            "tombstone_for_event_id": self.tombstone_for_event_id,
        }

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": TEMPORAL_EVENT_SCHEMA,
            "event_id": self.event_id,
            "scenario_id": self.scenario_id,
            "source_id": self.source_id,
            "section_id": self.section_id,
            "content_hash": self.content_hash,
            "observed_time": self.observed_time,
            "effective_time": self.effective_time,
            "published_time": self.published_time,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "authority_level": self.authority_level,
            "status": self.status,
            "subject": self.subject,
            "predicate": self.predicate,
            "value": self.value,
            "supersedes_event_id": self.supersedes_event_id,
            "tombstone_for_event_id": self.tombstone_for_event_id,
            "source_text_reference": self.source_text_reference,
        }


@dataclass(frozen=True)
class Claim:
    """A replay-derived claim with a direct link back to one corpus event."""

    claim_id: str
    subject: str
    predicate: str
    value: Any
    supporting_event_ids: tuple[str, ...]
    valid_from: str
    valid_to: str | None
    superseded_claim_id: str | None
    tombstone_event_id: str | None
    authority_level: int
    observed_time: str
    effective_time: str
    published_time: str | None
    state: Literal[
        "candidate", "current", "superseded", "expired", "retracted", "conflicted"
    ]
    confidence: float
    write_policy_decision: Literal["write", "ignore", "merge", "conflict", "tombstone"]

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": TEMPORAL_CLAIM_SCHEMA,
            "claim_id": self.claim_id,
            "subject": self.subject,
            "predicate": self.predicate,
            "value": self.value,
            "supporting_event_ids": list(self.supporting_event_ids),
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "superseded_claim_id": self.superseded_claim_id,
            "tombstone_event_id": self.tombstone_event_id,
            "authority_level": self.authority_level,
            "observed_time": self.observed_time,
            "effective_time": self.effective_time,
            "published_time": self.published_time,
            "state": self.state,
            "confidence": self.confidence,
            "write_policy_decision": self.write_policy_decision,
        }


@dataclass(frozen=True)
class ReplayState:
    observed_through: str
    events: tuple[CorpusEvent, ...]

    @property
    def snapshot_id(self) -> str:
        return (
            f"temporal-{sha256_json([event.to_record() for event in self.events])[:16]}"
        )


@dataclass(frozen=True)
class ExpectedAnswer:
    status: Literal["answer", "abstain"]
    value: Any | None
    supporting_event_ids: tuple[str, ...]
    as_of_time: str
    snapshot_time: str

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": ANSWER_SCHEMA,
            "status": self.status,
            "value": self.value,
            "supporting_event_ids": list(self.supporting_event_ids),
            "as_of_time": self.as_of_time,
            "snapshot_time": self.snapshot_time,
        }


@dataclass(frozen=True)
class TemporalScenario:
    scenario_id: str
    title: str
    events: tuple[CorpusEvent, ...]


@dataclass(frozen=True)
class TemporalQuestion:
    task_id: str
    scenario_id: str
    partition: Partition
    task_family: str
    difficulty: str
    question_status: Literal["frozen_public", "external"]
    question_text: str | None
    subject: str | None
    predicate: str | None
    as_of_time: str | None
    snapshot_time: str | None

    @property
    def is_sealed(self) -> bool:
        return self.partition == "sealed_capability"

    def public_record(self) -> dict[str, Any]:
        if self.is_sealed or self.question_text is None:
            raise TemporalContractError(
                "sealed temporal task has no repository public record"
            )
        record = {
            "schema_version": "contextlab.task.v1",
            "task_id": self.task_id,
            "suite": "temporal",
            "source_kind": "temporal_v2",
            "question_status": "frozen_public",
            "question_text": self.question_text,
            "question_sha256": hashlib.sha256(
                self.question_text.encode("utf-8")
            ).hexdigest(),
            "task_family": self.task_family,
            "difficulty": self.difficulty,
            "answer_type": "short_text",
            "required_evidence": [],
            "acceptable_alternative_evidence": [],
            "freshness_sensitivity": "high",
            "structured_data_dependency": "none",
            "sealed_eligible": False,
            "metadata_status": "authored",
        }
        if FORBIDDEN_PUBLIC_FIELDS.intersection(record):
            raise TemporalContractError("temporal public task leaked protected fields")
        return record

    def sealed_reference(self) -> dict[str, Any]:
        if not self.is_sealed:
            raise TemporalContractError("only sealed tasks have a sealed reference")
        return {
            "schema_version": SEALED_REFERENCE_SCHEMA,
            "task_id": self.task_id,
            "scenario_id": self.scenario_id,
            "partition": self.partition,
            "task_family": self.task_family,
            "difficulty": self.difficulty,
            "question_status": "external",
        }


def _event(
    scenario_number: int,
    ordinal: int,
    *,
    observed: str,
    effective: str,
    authority: int,
    status: EventStatus,
    subject: str,
    predicate: str,
    value: Any,
    published: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    supersedes: str | None = None,
    tombstone_for: str | None = None,
) -> CorpusEvent:
    scenario_id = f"TL-{scenario_number:02d}"
    return CorpusEvent.create(
        event_id=f"{scenario_id}-E{ordinal:02d}",
        scenario_id=scenario_id,
        observed_time=observed,
        effective_time=effective,
        published_time=published or observed,
        valid_from=valid_from,
        valid_to=valid_to,
        authority_level=authority,
        status=status,
        subject=subject,
        predicate=predicate,
        value=value,
        supersedes_event_id=supersedes,
        tombstone_for_event_id=tombstone_for,
    )


def _scenario(number: int, title: str, *events: CorpusEvent) -> TemporalScenario:
    scenario_id = f"TL-{number:02d}"
    if any(event.scenario_id != scenario_id for event in events):
        raise TemporalContractError(f"{scenario_id}: event scenario mismatch")
    return TemporalScenario(scenario_id, title, tuple(sorted(events, key=_event_key)))


SCENARIOS: tuple[TemporalScenario, ...] = (
    _scenario(
        1,
        "Starter price draft and approval",
        _event(
            1,
            1,
            observed="2026-01-02T09:00:00Z",
            effective="2026-02-01T00:00:00Z",
            authority=2,
            status="draft",
            subject="Starter",
            predicate="monthly_price_usd",
            value=900,
        ),
        _event(
            1,
            2,
            observed="2026-01-08T09:00:00Z",
            effective="2026-02-01T00:00:00Z",
            authority=5,
            status="final",
            subject="Starter",
            predicate="monthly_price_usd",
            value=1100,
            supersedes="TL-01-E01",
        ),
    ),
    _scenario(
        2,
        "Enterprise price draft and approval",
        _event(
            2,
            1,
            observed="2026-01-03T09:00:00Z",
            effective="2026-02-01T00:00:00Z",
            authority=2,
            status="draft",
            subject="Enterprise",
            predicate="monthly_price_usd",
            value=2800,
        ),
        _event(
            2,
            2,
            observed="2026-01-09T09:00:00Z",
            effective="2026-02-01T00:00:00Z",
            authority=5,
            status="final",
            subject="Enterprise",
            predicate="monthly_price_usd",
            value=3200,
            supersedes="TL-02-E01",
        ),
    ),
    _scenario(
        3,
        "Retention policy future effective date",
        _event(
            3,
            1,
            observed="2026-03-01T09:00:00Z",
            effective="2026-04-01T00:00:00Z",
            authority=5,
            status="final",
            subject="Customer exports",
            predicate="retention_period",
            value="90 days",
        ),
    ),
    _scenario(
        4,
        "Support policy future effective date",
        _event(
            4,
            1,
            observed="2026-05-01T09:00:00Z",
            effective="2026-09-01T00:00:00Z",
            authority=5,
            status="final",
            subject="Enterprise support",
            predicate="coverage",
            value="24/7",
        ),
    ),
    _scenario(
        5,
        "Activation metric correction",
        _event(
            5,
            1,
            observed="2026-07-01T09:00:00Z",
            effective="2026-06-30T00:00:00Z",
            authority=4,
            status="final",
            subject="Q2 activation",
            predicate="rate",
            value="72%",
        ),
        _event(
            5,
            2,
            observed="2026-07-03T09:00:00Z",
            effective="2026-06-30T00:00:00Z",
            authority=5,
            status="corrected",
            subject="Q2 activation",
            predicate="rate",
            value="68%",
            supersedes="TL-05-E01",
        ),
    ),
    _scenario(
        6,
        "Q2 churn correction",
        _event(
            6,
            1,
            observed="2026-07-02T09:00:00Z",
            effective="2026-06-30T00:00:00Z",
            authority=4,
            status="final",
            subject="Q2 churn",
            predicate="rate",
            value="3.1%",
        ),
        _event(
            6,
            2,
            observed="2026-07-05T09:00:00Z",
            effective="2026-06-30T00:00:00Z",
            authority=5,
            status="corrected",
            subject="Q2 churn",
            predicate="rate",
            value="3.4%",
            supersedes="TL-06-E01",
        ),
    ),
    _scenario(
        7,
        "Sales ICP authority conflict",
        _event(
            7,
            1,
            observed="2026-01-10T09:00:00Z",
            effective="2026-01-01T00:00:00Z",
            authority=3,
            status="final",
            subject="Sales ICP",
            predicate="primary_audience",
            value="midmarket teams",
        ),
        _event(
            7,
            2,
            observed="2026-01-12T09:00:00Z",
            effective="2026-01-01T00:00:00Z",
            authority=5,
            status="final",
            subject="Sales ICP",
            predicate="primary_audience",
            value="regulated enterprises",
        ),
    ),
    _scenario(
        8,
        "Sandbox retention expiry",
        _event(
            8,
            1,
            observed="2026-01-10T09:00:00Z",
            effective="2026-01-10T00:00:00Z",
            authority=5,
            status="final",
            subject="Sandbox data",
            predicate="retention_period",
            value="30 days",
        ),
        _event(
            8,
            2,
            observed="2026-06-01T09:00:00Z",
            effective="2026-06-01T00:00:00Z",
            authority=5,
            status="expired",
            subject="Sandbox data",
            predicate="retention_period",
            value=None,
            tombstone_for="TL-08-E01",
        ),
    ),
    _scenario(
        9,
        "Aurora roadmap owner change",
        _event(
            9,
            1,
            observed="2026-01-15T09:00:00Z",
            effective="2026-01-15T00:00:00Z",
            authority=5,
            status="final",
            subject="Aurora Analytics roadmap",
            predicate="owner",
            value="Priya Shah",
        ),
        _event(
            9,
            2,
            observed="2026-03-01T09:00:00Z",
            effective="2026-03-01T00:00:00Z",
            authority=5,
            status="final",
            subject="Aurora Analytics roadmap",
            predicate="owner",
            value="Noah Reed",
            supersedes="TL-09-E01",
        ),
    ),
    _scenario(
        10,
        "Beacon roadmap owner change",
        _event(
            10,
            1,
            observed="2026-01-20T09:00:00Z",
            effective="2026-01-20T00:00:00Z",
            authority=5,
            status="final",
            subject="Beacon integration",
            predicate="owner",
            value="Leila Ortiz",
        ),
        _event(
            10,
            2,
            observed="2026-04-01T09:00:00Z",
            effective="2026-04-01T00:00:00Z",
            authority=5,
            status="final",
            subject="Beacon integration",
            predicate="owner",
            value="Morgan Chen",
            supersedes="TL-10-E01",
        ),
    ),
    _scenario(
        11,
        "Atlas renewal meeting update",
        _event(
            11,
            1,
            observed="2026-02-01T09:00:00Z",
            effective="2026-02-01T00:00:00Z",
            authority=3,
            status="final",
            subject="Atlas Logistics",
            predicate="renewal_status",
            value="at risk",
        ),
        _event(
            11,
            2,
            observed="2026-05-10T09:00:00Z",
            effective="2026-05-10T00:00:00Z",
            authority=5,
            status="final",
            subject="Atlas Logistics",
            predicate="renewal_status",
            value="renewed",
            supersedes="TL-11-E01",
        ),
    ),
    _scenario(
        12,
        "Meridian renewal meeting update",
        _event(
            12,
            1,
            observed="2026-02-02T09:00:00Z",
            effective="2026-02-02T00:00:00Z",
            authority=3,
            status="final",
            subject="Meridian Retail",
            predicate="renewal_status",
            value="at risk",
        ),
        _event(
            12,
            2,
            observed="2026-06-10T09:00:00Z",
            effective="2026-06-10T00:00:00Z",
            authority=5,
            status="final",
            subject="Meridian Retail",
            predicate="renewal_status",
            value="renewed",
            supersedes="TL-12-E01",
        ),
    ),
    _scenario(
        13,
        "Compliance exception authority conflict",
        _event(
            13,
            1,
            observed="2026-02-10T09:00:00Z",
            effective="2026-02-01T00:00:00Z",
            authority=2,
            status="draft",
            subject="Compliance exception",
            predicate="self_approval",
            value="allowed",
        ),
        _event(
            13,
            2,
            observed="2026-02-12T09:00:00Z",
            effective="2026-02-01T00:00:00Z",
            authority=5,
            status="final",
            subject="Compliance exception",
            predicate="self_approval",
            value="not allowed",
            supersedes="TL-13-E01",
        ),
    ),
    _scenario(
        14,
        "Legacy discount expiry",
        _event(
            14,
            1,
            observed="2026-01-05T09:00:00Z",
            effective="2026-01-05T00:00:00Z",
            authority=5,
            status="final",
            subject="Legacy discount",
            predicate="rate",
            value="15%",
        ),
        _event(
            14,
            2,
            observed="2026-04-01T09:00:00Z",
            effective="2026-04-01T00:00:00Z",
            authority=5,
            status="expired",
            subject="Legacy discount",
            predicate="rate",
            value=None,
            tombstone_for="TL-14-E01",
        ),
    ),
    _scenario(
        15,
        "Nimbus integration claim retracted",
        _event(
            15,
            1,
            observed="2026-02-01T09:00:00Z",
            effective="2026-02-01T00:00:00Z",
            authority=4,
            status="final",
            subject="Nimbus integration",
            predicate="support_status",
            value="supported",
        ),
        _event(
            15,
            2,
            observed="2026-03-10T09:00:00Z",
            effective="2026-03-10T00:00:00Z",
            authority=5,
            status="tombstone",
            subject="Nimbus integration",
            predicate="support_status",
            value=None,
            tombstone_for="TL-15-E01",
        ),
    ),
    _scenario(
        16,
        "Acme approval claim retracted",
        _event(
            16,
            1,
            observed="2026-02-05T09:00:00Z",
            effective="2026-02-05T00:00:00Z",
            authority=4,
            status="final",
            subject="Acme Manufacturing",
            predicate="approval_status",
            value="approved",
        ),
        _event(
            16,
            2,
            observed="2026-04-10T09:00:00Z",
            effective="2026-04-10T00:00:00Z",
            authority=5,
            status="retracted",
            subject="Acme Manufacturing",
            predicate="approval_status",
            value=None,
            tombstone_for="TL-16-E01",
        ),
    ),
    _scenario(
        17,
        "Late EMEA adoption observation",
        _event(
            17,
            1,
            observed="2026-05-01T09:00:00Z",
            effective="2026-02-01T00:00:00Z",
            authority=5,
            status="final",
            subject="EMEA adoption",
            predicate="rate",
            value="44%",
        ),
    ),
    _scenario(
        18,
        "Late partner renewal observation",
        _event(
            18,
            1,
            observed="2026-07-01T09:00:00Z",
            effective="2026-03-01T00:00:00Z",
            authority=5,
            status="final",
            subject="Harbor partner",
            predicate="renewal_status",
            value="signed",
        ),
    ),
    _scenario(
        19,
        "Incident estimate and final postmortem",
        _event(
            19,
            1,
            observed="2026-03-05T09:00:00Z",
            effective="2026-03-05T00:00:00Z",
            authority=2,
            status="draft",
            subject="March upload outage",
            predicate="affected_customers",
            value="4000",
        ),
        _event(
            19,
            2,
            observed="2026-03-12T09:00:00Z",
            effective="2026-03-05T00:00:00Z",
            authority=5,
            status="final",
            subject="March upload outage",
            predicate="affected_customers",
            value="3200",
            supersedes="TL-19-E01",
        ),
    ),
    _scenario(
        20,
        "Incident duration postmortem correction",
        _event(
            20,
            1,
            observed="2026-03-06T09:00:00Z",
            effective="2026-03-05T00:00:00Z",
            authority=3,
            status="final",
            subject="March upload outage",
            predicate="duration",
            value="47 minutes",
        ),
        _event(
            20,
            2,
            observed="2026-03-13T09:00:00Z",
            effective="2026-03-05T00:00:00Z",
            authority=5,
            status="corrected",
            subject="March upload outage",
            predicate="duration",
            value="41 minutes",
            supersedes="TL-20-E01",
        ),
    ),
)


_LAYOUT: tuple[tuple[str, Partition, str, str], ...] = (
    ("T001", "sealed_capability", "information_extraction", "easy"),
    ("T002", "sealed_capability", "multi_session_reasoning", "medium"),
    ("T003", "sealed_capability", "temporal_reasoning", "hard"),
    ("T004", "sealed_capability", "knowledge_update", "medium"),
    ("T005", "sealed_capability", "abstention", "hard"),
    ("T006", "sealed_capability", "information_extraction", "easy"),
    ("T007", "sealed_capability", "multi_session_reasoning", "medium"),
    ("T008", "sealed_capability", "temporal_reasoning", "hard"),
    ("T009", "sealed_capability", "knowledge_update", "medium"),
    ("T010", "sealed_capability", "abstention", "hard"),
    ("T011", "sealed_capability", "information_extraction", "easy"),
    ("T012", "sealed_capability", "multi_session_reasoning", "medium"),
    ("T013", "judge_calibration", "temporal_reasoning", "hard"),
    ("T014", "showcase", "knowledge_update", "medium"),
    ("T015", "regression", "abstention", "hard"),
    ("T016", "showcase", "information_extraction", "easy"),
    ("T017", "regression", "multi_session_reasoning", "medium"),
    ("T018", "showcase", "temporal_reasoning", "hard"),
    ("T019", "regression", "knowledge_update", "medium"),
    ("T020", "judge_calibration", "abstention", "hard"),
    ("T021", "judge_calibration", "information_extraction", "easy"),
    ("T022", "judge_calibration", "multi_session_reasoning", "medium"),
    ("T023", "judge_calibration", "temporal_reasoning", "hard"),
    ("T024", "judge_calibration", "knowledge_update", "medium"),
    ("T025", "regression", "abstention", "hard"),
    ("T026", "regression", "information_extraction", "easy"),
    ("T027", "regression", "multi_session_reasoning", "medium"),
    ("T028", "regression", "temporal_reasoning", "hard"),
    ("T029", "judge_calibration", "knowledge_update", "medium"),
    ("T030", "regression", "abstention", "hard"),
    ("T031", "judge_calibration", "information_extraction", "easy"),
    ("T032", "regression", "multi_session_reasoning", "medium"),
    ("T033", "regression", "temporal_reasoning", "hard"),
    ("T034", "regression", "knowledge_update", "medium"),
    ("T035", "regression", "abstention", "hard"),
    ("T036", "regression", "information_extraction", "easy"),
    ("T037", "showcase", "multi_session_reasoning", "medium"),
    ("T038", "regression", "temporal_reasoning", "hard"),
    ("T039", "regression", "knowledge_update", "medium"),
    ("T040", "regression", "abstention", "hard"),
)

_PUBLIC_PROMPTS: dict[str, tuple[str, str, str, str | None, str | None]] = {
    "T013": (
        "At the current snapshot, what is the approved primary audience for the sales ICP?",
        "Sales ICP",
        "primary_audience",
        None,
        None,
    ),
    "T014": (
        "As of 2026-01-15, what primary audience did the authoritative sales ICP identify?",
        "Sales ICP",
        "primary_audience",
        "2026-01-15T23:59:59Z",
        "2026-01-15T23:59:59Z",
    ),
    "T015": (
        "At the current snapshot, what sandbox data retention period should be used?",
        "Sandbox data",
        "retention_period",
        None,
        None,
    ),
    "T016": (
        "As of 2026-05-15, what sandbox data retention period was valid?",
        "Sandbox data",
        "retention_period",
        "2026-05-15T23:59:59Z",
        "2026-05-15T23:59:59Z",
    ),
    "T017": (
        "At the current snapshot, who owns the Aurora Analytics roadmap?",
        "Aurora Analytics roadmap",
        "owner",
        None,
        None,
    ),
    "T018": (
        "As of 2026-02-20, who owned the Aurora Analytics roadmap?",
        "Aurora Analytics roadmap",
        "owner",
        "2026-02-20T23:59:59Z",
        "2026-02-20T23:59:59Z",
    ),
    "T019": (
        "At the current snapshot, who owns the Beacon integration?",
        "Beacon integration",
        "owner",
        None,
        None,
    ),
    "T020": (
        "As of 2026-03-20, who owned the Beacon integration?",
        "Beacon integration",
        "owner",
        "2026-03-20T23:59:59Z",
        "2026-03-20T23:59:59Z",
    ),
    "T021": (
        "At the current snapshot, what is the Atlas Logistics renewal status?",
        "Atlas Logistics",
        "renewal_status",
        None,
        None,
    ),
    "T022": (
        "As of 2026-03-01, what was the Atlas Logistics renewal status?",
        "Atlas Logistics",
        "renewal_status",
        "2026-03-01T23:59:59Z",
        "2026-03-01T23:59:59Z",
    ),
    "T023": (
        "At the current snapshot, what is the Meridian Retail renewal status?",
        "Meridian Retail",
        "renewal_status",
        None,
        None,
    ),
    "T024": (
        "As of 2026-03-01, what was the Meridian Retail renewal status?",
        "Meridian Retail",
        "renewal_status",
        "2026-03-01T23:59:59Z",
        "2026-03-01T23:59:59Z",
    ),
    "T025": (
        "At the current snapshot, may a team self-approve a compliance exception?",
        "Compliance exception",
        "self_approval",
        None,
        None,
    ),
    "T026": (
        "As of 2026-02-15, may a team self-approve a compliance exception?",
        "Compliance exception",
        "self_approval",
        "2026-02-15T23:59:59Z",
        "2026-02-15T23:59:59Z",
    ),
    "T027": (
        "At the current snapshot, what legacy discount rate is active?",
        "Legacy discount",
        "rate",
        None,
        None,
    ),
    "T028": (
        "As of 2026-03-15, what legacy discount rate was active?",
        "Legacy discount",
        "rate",
        "2026-03-15T23:59:59Z",
        "2026-03-15T23:59:59Z",
    ),
    "T029": (
        "At the current snapshot, what is the support status of the Nimbus integration?",
        "Nimbus integration",
        "support_status",
        None,
        None,
    ),
    "T030": (
        "As of 2026-03-01, what was the support status of the Nimbus integration?",
        "Nimbus integration",
        "support_status",
        "2026-03-01T23:59:59Z",
        "2026-03-01T23:59:59Z",
    ),
    "T031": (
        "At the current snapshot, what is Acme Manufacturing's approval status?",
        "Acme Manufacturing",
        "approval_status",
        None,
        None,
    ),
    "T032": (
        "As of 2026-03-01, what was Acme Manufacturing's approval status?",
        "Acme Manufacturing",
        "approval_status",
        "2026-03-01T23:59:59Z",
        "2026-03-01T23:59:59Z",
    ),
    "T033": (
        "At the current snapshot, what was the EMEA adoption rate effective on 2026-02-01?",
        "EMEA adoption",
        "rate",
        "2026-02-01T23:59:59Z",
        None,
    ),
    "T034": (
        "At the 2026-04-01 knowledge snapshot, what was known about the EMEA adoption rate effective on 2026-02-01?",
        "EMEA adoption",
        "rate",
        "2026-02-01T23:59:59Z",
        "2026-04-01T23:59:59Z",
    ),
    "T035": (
        "At the current snapshot, what is the Harbor partner renewal status?",
        "Harbor partner",
        "renewal_status",
        None,
        None,
    ),
    "T036": (
        "At the 2026-06-01 knowledge snapshot, what was known about the Harbor partner renewal status effective on 2026-03-01?",
        "Harbor partner",
        "renewal_status",
        "2026-03-01T23:59:59Z",
        "2026-06-01T23:59:59Z",
    ),
    "T037": (
        "At the current snapshot, how many customers did the March upload outage affect?",
        "March upload outage",
        "affected_customers",
        None,
        None,
    ),
    "T038": (
        "As of 2026-03-06, how many customers were confirmed affected by the March upload outage?",
        "March upload outage",
        "affected_customers",
        "2026-03-06T23:59:59Z",
        "2026-03-06T23:59:59Z",
    ),
    "T039": (
        "At the current snapshot, what was the final duration of the March upload outage?",
        "March upload outage",
        "duration",
        None,
        None,
    ),
    "T040": (
        "As of 2026-03-10, what duration was known for the March upload outage?",
        "March upload outage",
        "duration",
        "2026-03-10T23:59:59Z",
        "2026-03-10T23:59:59Z",
    ),
}


def _questions() -> tuple[TemporalQuestion, ...]:
    rows: list[TemporalQuestion] = []
    for ordinal, (task_id, partition, family, difficulty) in enumerate(
        _LAYOUT, start=1
    ):
        scenario_id = f"TL-{((ordinal - 1) // 2) + 1:02d}"
        if partition == "sealed_capability":
            rows.append(
                TemporalQuestion(
                    task_id,
                    scenario_id,
                    partition,
                    family,
                    difficulty,
                    "external",
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            )
            continue
        text, subject, predicate, as_of, snapshot = _PUBLIC_PROMPTS[task_id]
        rows.append(
            TemporalQuestion(
                task_id,
                scenario_id,
                partition,
                family,
                difficulty,
                "frozen_public",
                text,
                subject,
                predicate,
                as_of,
                snapshot,
            )
        )
    return tuple(rows)


QUESTIONS = _questions()


def scenario_catalog() -> tuple[TemporalScenario, ...]:
    return SCENARIOS


def temporal_question_catalog() -> tuple[TemporalQuestion, ...]:
    return QUESTIONS


def public_temporal_tasks() -> list[dict[str, Any]]:
    return [
        question.public_record() for question in QUESTIONS if not question.is_sealed
    ]


def sealed_temporal_references() -> list[dict[str, Any]]:
    return [question.sealed_reference() for question in QUESTIONS if question.is_sealed]


def event_history_sha256(scenarios: Iterable[TemporalScenario] = SCENARIOS) -> str:
    events = [event.to_record() for scenario in scenarios for event in scenario.events]
    return sha256_json(events)


def load_event_source_rows(root: Path | None = None) -> dict[str, dict[str, Any]]:
    """Load the committed public event stream and reject duplicate or malformed rows."""
    source_path = (root or repository_root()) / EVENT_SOURCE_RELATIVE_PATH
    if not source_path.is_file():
        raise TemporalContractError(
            f"temporal event source does not exist: {source_path}"
        )
    rows: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(
        source_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TemporalContractError(
                f"temporal event source line {line_number} is not valid JSON"
            ) from exc
        event_id = row.get("event_id") if isinstance(row, dict) else None
        if not isinstance(event_id, str) or not event_id:
            raise TemporalContractError(
                f"temporal event source line {line_number} has no event_id"
            )
        if event_id in rows:
            raise TemporalContractError(
                f"duplicate temporal event source row: {event_id}"
            )
        rows[event_id] = row
    return rows


def all_events(
    scenarios: Iterable[TemporalScenario] = SCENARIOS,
) -> tuple[CorpusEvent, ...]:
    events = tuple(event for scenario in scenarios for event in scenario.events)
    ids = [event.event_id for event in events]
    if len(ids) != len(set(ids)):
        raise TemporalContractError("temporal event IDs must be unique")
    by_id = {event.event_id: event for event in events}
    for event in events:
        for relation, target_id in (
            ("supersedes_event_id", event.supersedes_event_id),
            ("tombstone_for_event_id", event.tombstone_for_event_id),
        ):
            if target_id is not None and target_id not in by_id:
                raise TemporalContractError(
                    f"{event.event_id}: {relation} does not resolve to an event"
                )
    return tuple(sorted(events, key=_event_key))


def replay_from_empty(
    events: Iterable[CorpusEvent], observed_through: str
) -> ReplayState:
    """Replay an append-only event stream through one observed-time snapshot."""
    cutoff = _time_text(observed_through)
    selected = tuple(
        event
        for event in all_events((TemporalScenario("replay", "replay", tuple(events)),))
        if _time(event.observed_time) <= _time(cutoff)
    )
    return ReplayState(cutoff, selected)


def replay_scenario(scenario_id: str, observed_through: str) -> ReplayState:
    scenario = next(
        (item for item in SCENARIOS if item.scenario_id == scenario_id), None
    )
    if scenario is None:
        raise TemporalContractError(f"unknown temporal scenario: {scenario_id}")
    return replay_from_empty(scenario.events, observed_through)


def _active_assertions(state: ReplayState, as_of_time: str) -> list[CorpusEvent]:
    as_of = _time(as_of_time)
    observed_through = _time(state.observed_through)
    known = {event.event_id: event for event in state.events}

    def applies(event: CorpusEvent) -> bool:
        return (
            _time(event.observed_time) <= observed_through
            and event.published_time is not None
            and _time(event.published_time) <= observed_through
            and _time(event.effective_time) <= as_of
            and _time(event.valid_from) <= as_of
            and (event.valid_to is None or as_of < _time(event.valid_to))
        )

    lifecycle_targets = {
        event.tombstone_for_event_id: event
        for event in state.events
        if event.status in {"tombstone", "retracted", "expired"}
        and event.tombstone_for_event_id
        and applies(event)
    }
    candidates = [
        event
        for event in state.events
        if event.status in {"final", "corrected"}
        and applies(event)
        and event.event_id not in lifecycle_targets
    ]
    active_ids = {event.event_id for event in candidates}
    superseded = {
        event.supersedes_event_id
        for event in candidates
        if event.supersedes_event_id and event.supersedes_event_id in known
    }
    return [
        event
        for event in candidates
        if event.event_id not in superseded or event.event_id not in active_ids
    ]


def claims_as_of(state: ReplayState, as_of_time: str) -> tuple[Claim, ...]:
    """Resolve current claims by validity, supersession, authority, then publication time."""
    as_of = _time_text(as_of_time)
    grouped: dict[tuple[str, str], list[CorpusEvent]] = {}
    for event in _active_assertions(state, as_of):
        grouped.setdefault((event.subject, event.predicate), []).append(event)
    claims: list[Claim] = []
    for _, events in sorted(grouped.items()):
        winner = max(
            events,
            key=lambda event: (
                event.authority_level,
                _time(event.effective_time),
                _time(event.published_time or event.observed_time),
                _time(event.observed_time),
                event.event_id,
            ),
        )
        claims.append(
            Claim(
                claim_id=f"claim-{winner.event_id}",
                subject=winner.subject,
                predicate=winner.predicate,
                value=winner.value,
                supporting_event_ids=(winner.event_id,),
                valid_from=winner.valid_from,
                valid_to=winner.valid_to,
                superseded_claim_id=None,
                tombstone_event_id=None,
                authority_level=winner.authority_level,
                observed_time=winner.observed_time,
                effective_time=winner.effective_time,
                published_time=winner.published_time,
                state="current",
                confidence=1.0,
                write_policy_decision="write",
            )
        )
    return tuple(claims)


def _answer_for(
    state: ReplayState, subject: str, predicate: str, as_of_time: str
) -> ExpectedAnswer:
    as_of = _time_text(as_of_time)
    claims = [
        claim
        for claim in claims_as_of(state, as_of)
        if (claim.subject, claim.predicate) == (subject, predicate)
    ]
    if not claims:
        return ExpectedAnswer("abstain", None, (), as_of, state.observed_through)
    if len(claims) != 1:
        raise TemporalContractError(
            f"ambiguous resolved claim for {subject}/{predicate}"
        )
    claim = claims[0]
    return ExpectedAnswer(
        "answer", claim.value, claim.supporting_event_ids, as_of, state.observed_through
    )


def build_current_answer(
    events: Iterable[CorpusEvent],
    subject: str,
    predicate: str,
    *,
    observed_through: str | None = None,
) -> ExpectedAnswer:
    event_rows = tuple(events)
    if not event_rows:
        raise TemporalContractError("current answer requires at least one event")
    snapshot = observed_through or max(event_rows, key=_event_key).observed_time
    state = replay_from_empty(event_rows, snapshot)
    return _answer_for(state, subject, predicate, snapshot)


def build_as_of_answer(
    events: Iterable[CorpusEvent],
    subject: str,
    predicate: str,
    as_of_time: str,
    *,
    observed_through: str | None = None,
) -> ExpectedAnswer:
    event_rows = tuple(events)
    if not event_rows:
        raise TemporalContractError("as-of answer requires at least one event")
    snapshot = observed_through or max(event_rows, key=_event_key).observed_time
    return _answer_for(
        replay_from_empty(event_rows, snapshot), subject, predicate, as_of_time
    )


def expected_answer_for_question(task_id: str) -> ExpectedAnswer:
    question = next((item for item in QUESTIONS if item.task_id == task_id), None)
    if question is None:
        raise TemporalContractError(f"unknown temporal task: {task_id}")
    if question.is_sealed:
        raise TemporalContractError("sealed expected answers are external-only")
    scenario = next(
        item for item in SCENARIOS if item.scenario_id == question.scenario_id
    )
    if question.as_of_time is None:
        return build_current_answer(
            scenario.events,
            str(question.subject),
            str(question.predicate),
            observed_through=question.snapshot_time,
        )
    return build_as_of_answer(
        scenario.events,
        str(question.subject),
        str(question.predicate),
        question.as_of_time,
        observed_through=question.snapshot_time,
    )


def validate_temporal_contract() -> dict[str, int]:
    """Validate counts, prompt-safe sealed references, and JSON persistence contracts."""
    scenarios = scenario_catalog()
    questions = temporal_question_catalog()
    if len(scenarios) != 20 or len(questions) != 40:
        raise TemporalContractError(
            "Week 7 requires exactly 20 scenarios and 40 temporal questions"
        )
    if Counter(question.partition for question in questions) != Counter(
        regression=16, judge_calibration=8, sealed_capability=12, showcase=4
    ):
        raise TemporalContractError(
            "temporal question partitions differ from the frozen allocation"
        )
    if len(public_temporal_tasks()) != 28 or len(sealed_temporal_references()) != 12:
        raise TemporalContractError(
            "temporal public/sealed boundary has the wrong task count"
        )
    events = all_events(scenarios)
    source_rows = load_event_source_rows()
    if set(source_rows) != {event.event_id for event in events}:
        raise TemporalContractError(
            "temporal event source IDs differ from the scenario catalog"
        )
    for event in events:
        errors = validate_instance("CorpusEvent", event.to_record())
        if errors:
            raise TemporalContractError(
                f"{event.event_id}: invalid corpus event: {errors}"
            )
        if source_rows[event.event_id] != event.source_payload():
            raise TemporalContractError(
                f"{event.event_id}: committed source payload differs"
            )
        if event.content_hash != sha256_json(source_rows[event.event_id]):
            raise TemporalContractError(
                f"{event.event_id}: committed source hash differs"
            )
    sample_claims = claims_as_of(
        replay_scenario("TL-07", "2026-02-01T00:00:00Z"), "2026-02-01T00:00:00Z"
    )
    for claim in sample_claims:
        errors = validate_instance("Claim", claim.to_record())
        if errors:
            raise TemporalContractError(f"{claim.claim_id}: invalid claim: {errors}")
    return {
        "scenarios": len(scenarios),
        "questions": len(questions),
        "events": len(events),
    }
