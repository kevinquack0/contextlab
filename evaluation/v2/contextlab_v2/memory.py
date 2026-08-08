"""Deterministic, evidence-grounded memory policies for the temporal laboratory.

The engine deliberately keeps the corpus event stream separate from memory.  A
read always carries corpus evidence, including for M0, so a memory claim is
never an uncheckable replacement for its source.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Literal

from .contracts import validate_instance
from .tasking import sha256_json
from .temporal import Claim, CorpusEvent


MemoryPolicy = Literal["M0", "M1", "M2", "M3", "M4"]
MEMORY_SNAPSHOT_SCHEMA = "contextlab.memory-snapshot.v2"
MEMORY_READ_SCHEMA = "contextlab.memory-read.v1"
M1_CLAIM_LIMIT = 5
POLICIES = frozenset(("M0", "M1", "M2", "M3", "M4"))
TRUSTED_OBJECTIVE_GRADERS = frozenset(
    {
        "deterministic",
        "gpt-5.6-sol-high",
        "claude-opus-5-medium",
        "kevin",
        "panel-majority",
    }
)


class MemoryContractError(ValueError):
    """A memory policy action or persisted memory state is invalid."""


@dataclass(frozen=True)
class Episode:
    """A compact, evidence-linked outcome card used only by M4."""

    episode_id: str
    task_signature: str
    category: str
    selected_strategy: str
    evidence_path: tuple[str, ...]
    graded_outcome: dict[str, Any]
    cost_usd: float
    latency_ms: int
    failure_mode: str | None
    source_run_id: str
    trace_id: str
    retention_decision: Literal["retain", "expire", "remove"]
    promotion_decision: Literal["pending", "promoted", "rejected"]

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": "contextlab.episode.v1",
            "episode_id": self.episode_id,
            "task_signature": self.task_signature,
            "category": self.category,
            "selected_strategy": self.selected_strategy,
            "evidence_path": list(self.evidence_path),
            "graded_outcome": self.graded_outcome,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
            "failure_mode": self.failure_mode,
            "source_run_id": self.source_run_id,
            "trace_id": self.trace_id,
            "retention_decision": self.retention_decision,
            "promotion_decision": self.promotion_decision,
        }


def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MemoryContractError(f"invalid timestamp: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MemoryContractError(f"timestamp requires an explicit timezone: {value}")
    return parsed.astimezone(timezone.utc)


def _time_text(value: str) -> str:
    return _time(value).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _event_sort_key(event: CorpusEvent) -> tuple[datetime, str]:
    return (_time(event.observed_time), event.event_id)


def _claim_sort_key(claim: Claim) -> tuple[str, str]:
    return (claim.claim_id, _canonical_json(claim.value))


@dataclass(frozen=True)
class MemoryDecision:
    """An observable result of processing a corpus event or episode."""

    sequence: int
    item_id: str
    kind: Literal["event", "episode"]
    decision: Literal["write", "ignore", "merge", "conflict", "tombstone", "remove"]
    reason: str
    claim_ids: tuple[str, ...] = ()

    def to_record(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "item_id": self.item_id,
            "kind": self.kind,
            "decision": self.decision,
            "reason": self.reason,
            "claim_ids": list(self.claim_ids),
        }


@dataclass(frozen=True)
class MemoryRead:
    """A policy read plus the corpus evidence used to verify it."""

    policy: MemoryPolicy
    subject: str
    predicate: str
    observed_through: str
    as_of_time: str
    corpus_events: tuple[CorpusEvent, ...]
    selected_claims: tuple[Claim, ...]
    conflict_claims: tuple[Claim, ...]
    supporting_events: tuple[CorpusEvent, ...]
    episodes: tuple[Episode, ...]
    verified_episode_evidence_ids: tuple[str, ...] = ()

    def provenance_complete(self) -> bool:
        available = {event.event_id for event in self.supporting_events}
        claims_resolve = all(
            set(claim.supporting_event_ids).issubset(available)
            for claim in (*self.selected_claims, *self.conflict_claims)
        )
        episode_evidence = {
            evidence_id
            for episode in self.episodes
            for evidence_id in episode.evidence_path
        }
        return claims_resolve and episode_evidence.issubset(
            set(self.verified_episode_evidence_ids)
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": MEMORY_READ_SCHEMA,
            "policy": self.policy,
            "subject": self.subject,
            "predicate": self.predicate,
            "observed_through": self.observed_through,
            "as_of_time": self.as_of_time,
            "corpus_event_ids": [event.event_id for event in self.corpus_events],
            "selected_claim_ids": [claim.claim_id for claim in self.selected_claims],
            "conflict_claim_ids": [claim.claim_id for claim in self.conflict_claims],
            "supporting_event_ids": [
                event.event_id for event in self.supporting_events
            ],
            "episode_ids": [episode.episode_id for episode in self.episodes],
            "verified_episode_evidence_ids": list(self.verified_episode_evidence_ids),
            "provenance_complete": self.provenance_complete(),
        }


class MemoryEngine:
    """One small replayable engine implementing M0 through M4.

    Corpus events are an append-only source history.  Claims and episodes are
    policy products that can be rebuilt deterministically from that history and
    a saved episode ledger.  This avoids mutable in-place correction logic.
    """

    def __init__(
        self,
        policy: MemoryPolicy,
        *,
        episode_cap: int = 100,
        external_evidence_ids: Iterable[str] = (),
    ) -> None:
        if policy not in POLICIES:
            raise MemoryContractError(f"unsupported memory policy: {policy}")
        if not isinstance(episode_cap, int) or episode_cap < 1:
            raise MemoryContractError("episode_cap must be a positive integer")
        self.policy: MemoryPolicy = policy
        self.episode_cap = episode_cap
        external_ids = tuple(external_evidence_ids)
        if any(not isinstance(item, str) or not item for item in external_ids):
            raise MemoryContractError("external evidence IDs must be non-empty strings")
        self._external_evidence_ids = frozenset(external_ids)
        self._events: dict[str, CorpusEvent] = {}
        self._claims: dict[str, Claim] = {}
        self._episodes: dict[str, Episode] = {}
        self._trusted_episode_sources: dict[tuple[str, str], str] = {}
        self._episode_features: dict[str, str] = {}
        self._episode_order: dict[str, int] = {}
        self._next_episode_order = 1
        self._ledger: list[MemoryDecision] = []

    @property
    def corpus_events(self) -> tuple[CorpusEvent, ...]:
        return tuple(sorted(self._events.values(), key=_event_sort_key))

    @property
    def claims(self) -> tuple[Claim, ...]:
        return tuple(sorted(self._claims.values(), key=_claim_sort_key))

    @property
    def episodes(self) -> tuple[Episode, ...]:
        return tuple(sorted(self._episodes.values(), key=lambda item: item.episode_id))

    @property
    def decision_ledger(self) -> tuple[MemoryDecision, ...]:
        return tuple(self._ledger)

    def ingest(self, events: Iterable[CorpusEvent]) -> tuple[MemoryDecision, ...]:
        """Append source events, then deterministically rebuild fact memory.

        Duplicate IDs with the exact same canonical source record are harmless;
        a duplicate ID with different source content is a hard provenance error.
        """
        received = tuple(events)
        for event in received:
            self._validate_event(event)
            existing = self._events.get(event.event_id)
            if existing is not None and existing.to_record() != event.to_record():
                raise MemoryContractError(
                    f"event ID has conflicting source content: {event.event_id}"
                )
            self._events[event.event_id] = event
        self._validate_event_relations()
        self._rebuild_claims()
        received_ids = {event.event_id for event in received}
        return tuple(
            decision
            for decision in self._ledger
            if decision.kind == "event" and decision.item_id in received_ids
        )

    def replay(self, events: Iterable[CorpusEvent]) -> "MemoryEngine":
        """Replace source history and rebuild this policy from an empty fact store."""
        self._events = {}
        for event in events:
            self._validate_event(event)
            if event.event_id in self._events:
                raise MemoryContractError(
                    f"replay source contains duplicate event ID: {event.event_id}"
                )
            self._events[event.event_id] = event
        self._validate_event_relations()
        self._rebuild_claims()
        return self

    @classmethod
    def rebuilt(
        cls,
        policy: MemoryPolicy,
        events: Iterable[CorpusEvent],
        *,
        episode_cap: int = 100,
        external_evidence_ids: Iterable[str] = (),
    ) -> "MemoryEngine":
        engine = cls(
            policy,
            episode_cap=episode_cap,
            external_evidence_ids=external_evidence_ids,
        )
        engine.replay(events)
        return engine

    @staticmethod
    def _validate_event(event: CorpusEvent) -> None:
        errors = validate_instance("CorpusEvent", event.to_record())
        if errors:
            raise MemoryContractError(
                f"{event.event_id}: invalid CorpusEvent: {errors}"
            )
        if event.content_hash != sha256_json(event.source_payload()):
            raise MemoryContractError(
                f"{event.event_id}: content hash differs from source payload"
            )

    def _validate_event_relations(self) -> None:
        event_ids = set(self._events)
        for event in self._events.values():
            for field, target in (
                ("supersedes_event_id", event.supersedes_event_id),
                ("tombstone_for_event_id", event.tombstone_for_event_id),
            ):
                if target is not None and target not in event_ids:
                    raise MemoryContractError(
                        f"{event.event_id}: {field} does not resolve to a corpus event"
                    )

    def _append_decision(
        self,
        item_id: str,
        kind: Literal["event", "episode"],
        decision: Literal[
            "write", "ignore", "merge", "conflict", "tombstone", "remove"
        ],
        reason: str,
        claim_ids: Iterable[str] = (),
    ) -> None:
        self._ledger.append(
            MemoryDecision(
                len(self._ledger) + 1,
                item_id,
                kind,
                decision,
                reason,
                tuple(sorted(claim_ids)),
            )
        )

    def _claim_for(
        self,
        event: CorpusEvent,
        *,
        state: Literal[
            "candidate", "current", "superseded", "expired", "retracted", "conflicted"
        ],
        decision: Literal["write", "ignore", "merge", "conflict", "tombstone"],
        superseded_claim_id: str | None = None,
        tombstone_event_id: str | None = None,
    ) -> Claim:
        return Claim(
            claim_id=f"claim-{event.event_id}",
            subject=event.subject,
            predicate=event.predicate,
            value=event.value,
            supporting_event_ids=(event.event_id,),
            valid_from=event.valid_from,
            valid_to=event.valid_to,
            superseded_claim_id=superseded_claim_id,
            tombstone_event_id=tombstone_event_id,
            authority_level=event.authority_level,
            observed_time=event.observed_time,
            effective_time=event.effective_time,
            published_time=event.published_time,
            state=state,
            confidence=1.0,
            write_policy_decision=decision,
        )

    def _rebuild_claims(self) -> None:
        self._claims = {}
        # Episode decisions are retained after all source-event decisions, which
        # makes a replay with the same inputs byte-for-byte stable.
        episode_decisions = [
            decision for decision in self._ledger if decision.kind == "episode"
        ]
        self._ledger = []
        for event in self.corpus_events:
            if self.policy == "M0":
                self._append_decision(
                    event.event_id,
                    "event",
                    "ignore",
                    "M0 has no persistent fact writes",
                )
            elif self.policy == "M1":
                self._write_m1(event)
            elif self.policy == "M2":
                self._write_m2(event)
            else:
                self._write_m3(event)
        for decision in episode_decisions:
            self._append_decision(
                decision.item_id,
                decision.kind,
                decision.decision,
                decision.reason,
                decision.claim_ids,
            )

    def _write_m1(self, event: CorpusEvent) -> None:
        if event.status in {"draft", "final", "corrected"}:
            claim = self._claim_for(event, state="candidate", decision="write")
            self._claims[claim.claim_id] = claim
            self._append_decision(
                event.event_id,
                "event",
                "write",
                "append-only candidate fact",
                (claim.claim_id,),
            )
            return
        self._append_decision(
            event.event_id,
            "event",
            "ignore",
            "append-only policy does not interpret lifecycle events",
        )

    def _write_m2(self, event: CorpusEvent) -> None:
        if event.status not in {"final", "corrected"}:
            self._append_decision(
                event.event_id,
                "event",
                "ignore",
                "selective policy accepts final assertions only",
            )
            return
        if event.authority_level < 3:
            self._append_decision(
                event.event_id,
                "event",
                "ignore",
                "selective policy requires authority level 3 or higher",
            )
            return
        same_key = [
            claim
            for claim in self._claims.values()
            if (claim.subject, claim.predicate) == (event.subject, event.predicate)
        ]
        same_value = next(
            (claim for claim in same_key if claim.value == event.value), None
        )
        if same_value is not None:
            merged = replace(
                same_value,
                supporting_event_ids=tuple(
                    sorted((*same_value.supporting_event_ids, event.event_id))
                ),
                write_policy_decision="merge",
            )
            self._claims[merged.claim_id] = merged
            self._append_decision(
                event.event_id,
                "event",
                "merge",
                "same selected fact value",
                (merged.claim_id,),
            )
            return
        claim = self._claim_for(
            event,
            state="conflicted" if same_key else "current",
            decision="conflict" if same_key else "write",
        )
        self._claims[claim.claim_id] = claim
        if same_key:
            for existing in same_key:
                self._claims[existing.claim_id] = replace(
                    existing, state="conflicted", write_policy_decision="conflict"
                )
            self._append_decision(
                event.event_id,
                "event",
                "conflict",
                "different selected values are retained separately",
                (claim.claim_id, *(item.claim_id for item in same_key)),
            )
        else:
            self._append_decision(
                event.event_id,
                "event",
                "write",
                "selected final fact",
                (claim.claim_id,),
            )

    def _write_m3(self, event: CorpusEvent) -> None:
        if event.status in {"tombstone", "retracted", "expired"}:
            targets = [
                claim
                for claim in self._claims.values()
                if event.tombstone_for_event_id in claim.supporting_event_ids
            ]
            if not targets:
                self._append_decision(
                    event.event_id,
                    "event",
                    "ignore",
                    "lifecycle target is not a stored claim",
                )
                return
            state = "expired" if event.status == "expired" else "retracted"
            lifecycle_start = max(_time(event.effective_time), _time(event.valid_from))
            for target in targets:
                valid_to = lifecycle_start
                if target.valid_to is not None:
                    valid_to = min(valid_to, _time(target.valid_to))
                self._claims[target.claim_id] = replace(
                    target,
                    state=state,
                    valid_to=valid_to.isoformat().replace("+00:00", "Z"),
                    tombstone_event_id=event.event_id,
                    write_policy_decision="tombstone",
                )
            self._append_decision(
                event.event_id,
                "event",
                "tombstone",
                f"{event.status} stored claim(s)",
                (item.claim_id for item in targets),
            )
            return
        if event.status not in {"final", "corrected"}:
            self._append_decision(
                event.event_id,
                "event",
                "ignore",
                "temporal policy ignores non-final assertions",
            )
            return
        same_key = [
            claim
            for claim in self._claims.values()
            if (claim.subject, claim.predicate) == (event.subject, event.predicate)
            and claim.state in {"candidate", "current", "conflicted"}
        ]
        same_value = next(
            (claim for claim in same_key if claim.value == event.value), None
        )
        superseded_claim_id: str | None = None
        if event.supersedes_event_id:
            targets = [
                claim
                for claim in self._claims.values()
                if event.supersedes_event_id in claim.supporting_event_ids
            ]
            for target in targets:
                self._claims[target.claim_id] = replace(target, state="superseded")
            if targets:
                superseded_claim_id = sorted(targets, key=_claim_sort_key)[0].claim_id
        if same_value is not None and not event.supersedes_event_id:
            # Keep one claim per source event. A later lifecycle action can then
            # target one assertion without corrupting an independent source.
            merged = self._claim_for(event, state="current", decision="merge")
            self._claims[merged.claim_id] = merged
            self._append_decision(
                event.event_id,
                "event",
                "merge",
                "same temporal value retained with independent provenance",
                (same_value.claim_id, merged.claim_id),
            )
            return
        claim = self._claim_for(
            event,
            state="current",
            decision="write",
            superseded_claim_id=superseded_claim_id,
        )
        competing = [
            existing
            for existing in same_key
            if existing.claim_id != superseded_claim_id
            and existing.state == "current"
            and existing.value != event.value
        ]
        if competing and not event.supersedes_event_id:
            highest = max(
                [event.authority_level, *(item.authority_level for item in competing)]
            )
            if event.authority_level == highest and any(
                item.authority_level == highest for item in competing
            ):
                self._claims[claim.claim_id] = replace(
                    claim, state="conflicted", write_policy_decision="conflict"
                )
                for item in competing:
                    if item.authority_level == highest:
                        self._claims[item.claim_id] = replace(
                            item, state="conflicted", write_policy_decision="conflict"
                        )
                self._append_decision(
                    event.event_id,
                    "event",
                    "conflict",
                    "equal-authority contradictory claims remain unresolved",
                    (claim.claim_id, *(item.claim_id for item in competing)),
                )
                return
            if event.authority_level > max(item.authority_level for item in competing):
                for item in competing:
                    self._claims[item.claim_id] = replace(
                        item, state="conflicted", write_policy_decision="conflict"
                    )
            else:
                claim = replace(
                    claim, state="conflicted", write_policy_decision="conflict"
                )
        self._claims[claim.claim_id] = claim
        decision = "conflict" if claim.state == "conflicted" else "write"
        reason = (
            "authority rule retained a competing claim"
            if decision == "conflict"
            else "temporal final fact"
        )
        self._append_decision(
            event.event_id, "event", decision, reason, (claim.claim_id,)
        )

    def _read_times(
        self, observed_through: str | None, as_of_time: str | None
    ) -> tuple[str, str]:
        if not self._events:
            raise MemoryContractError("memory read requires at least one corpus event")
        observed = _time_text(observed_through or self.corpus_events[-1].observed_time)
        return observed, _time_text(as_of_time or observed)

    def _published_events(self, observed_through: str) -> tuple[CorpusEvent, ...]:
        cutoff = _time(observed_through)
        return tuple(
            event
            for event in self.corpus_events
            if _time(event.observed_time) <= cutoff
            and event.published_time is not None
            and _time(event.published_time) <= cutoff
        )

    def _corpus_candidates(
        self, subject: str, predicate: str, observed_through: str
    ) -> tuple[CorpusEvent, ...]:
        published = self._published_events(observed_through)
        assertions = tuple(
            event
            for event in published
            if event.subject == subject and event.predicate == predicate
        )
        assertion_ids = {event.event_id for event in assertions}
        lifecycles = tuple(
            event
            for event in published
            if event.status in {"tombstone", "retracted", "expired"}
            and event.tombstone_for_event_id in assertion_ids
        )
        unique = {event.event_id: event for event in (*assertions, *lifecycles)}
        return tuple(sorted(unique.values(), key=_event_sort_key))

    def _ranked_m1_claims(
        self, subject: str, predicate: str, observed_through: str
    ) -> tuple[Claim, ...]:
        """Return a bounded, deterministic lexical lookup over append-only facts."""
        available = {
            event.event_id for event in self._published_events(observed_through)
        }
        query = f"{subject} {predicate}"
        ranked: list[tuple[int, float, str, Claim]] = []
        for claim in self.claims:
            supporting = tuple(
                event_id
                for event_id in claim.supporting_event_ids
                if event_id in available
            )
            if not supporting:
                continue
            score = self._similarity(query, f"{claim.subject} {claim.predicate}")
            if score <= 0:
                continue
            exact = int((claim.subject, claim.predicate) == (subject, predicate))
            ranked.append(
                (
                    exact,
                    score,
                    claim.claim_id,
                    replace(claim, supporting_event_ids=supporting),
                )
            )
        ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
        return tuple(item[3] for item in ranked[:M1_CLAIM_LIMIT])

    def _claims_for_events(
        self, events: Iterable[CorpusEvent], *, state: str | None = None
    ) -> tuple[Claim, ...]:
        ids = {event.event_id for event in events}
        claims = [
            claim
            for claim in self._claims.values()
            if ids.intersection(claim.supporting_event_ids)
        ]
        if state is not None:
            claims = [claim for claim in claims if claim.state == state]
        return tuple(sorted(claims, key=_claim_sort_key))

    @staticmethod
    def _visible_claims(
        claims: Iterable[Claim], corpus_events: Iterable[CorpusEvent]
    ) -> tuple[Claim, ...]:
        """Remove future source links when rendering an historical snapshot."""
        available = {event.event_id for event in corpus_events}
        visible: list[Claim] = []
        for claim in claims:
            supporting = tuple(
                event_id
                for event_id in claim.supporting_event_ids
                if event_id in available
            )
            if supporting:
                visible.append(replace(claim, supporting_event_ids=supporting))
        return tuple(sorted(visible, key=_claim_sort_key))

    def _active_temporal_events(
        self, subject: str, predicate: str, observed_through: str, as_of_time: str
    ) -> tuple[CorpusEvent, ...]:
        observed = _time(observed_through)
        as_of = _time(as_of_time)

        def applies(event: CorpusEvent) -> bool:
            return (
                _time(event.observed_time) <= observed
                and event.published_time is not None
                and _time(event.published_time) <= observed
                and _time(event.effective_time) <= as_of
                and _time(event.valid_from) <= as_of
                and (event.valid_to is None or as_of < _time(event.valid_to))
            )

        lifecycles = {
            event.tombstone_for_event_id
            for event in self.corpus_events
            if event.status in {"tombstone", "retracted", "expired"}
            and event.tombstone_for_event_id
            and applies(event)
        }
        assertions = [
            event
            for event in self.corpus_events
            if event.subject == subject
            and event.predicate == predicate
            and event.status in {"final", "corrected"}
            and applies(event)
            and event.event_id not in lifecycles
        ]
        return tuple(assertions)

    def _claim_view_for_read(
        self,
        claim: Claim,
        event: CorpusEvent,
        observed_through: str,
        as_of_time: str,
    ) -> Claim:
        """Hide lifecycle mutations that are not applicable to this read."""
        if claim.tombstone_event_id is None:
            return claim
        lifecycle = self._events.get(claim.tombstone_event_id)
        observed = _time(observed_through)
        lifecycle_is_known = (
            lifecycle is not None
            and _time(lifecycle.observed_time) <= observed
            and lifecycle.published_time is not None
            and _time(lifecycle.published_time) <= observed
        )
        lifecycle_is_still_valid = lifecycle is not None and (
            lifecycle.valid_to is None or _time(as_of_time) < _time(lifecycle.valid_to)
        )
        if lifecycle_is_known and lifecycle_is_still_valid:
            return claim
        return replace(
            claim,
            valid_to=event.valid_to,
            tombstone_event_id=None,
            state="current",
            write_policy_decision=(
                "write"
                if claim.write_policy_decision == "tombstone"
                else claim.write_policy_decision
            ),
        )

    @staticmethod
    def _claim_applies(claim: Claim, observed_through: str, as_of_time: str) -> bool:
        observed = _time(observed_through)
        as_of = _time(as_of_time)
        return (
            _time(claim.observed_time) <= observed
            and claim.published_time is not None
            and _time(claim.published_time) <= observed
            and _time(claim.effective_time) <= as_of
            and _time(claim.valid_from) <= as_of
            and (claim.valid_to is None or as_of < _time(claim.valid_to))
        )

    def _resolved_m3(
        self, subject: str, predicate: str, observed_through: str, as_of_time: str
    ) -> tuple[tuple[Claim, ...], tuple[Claim, ...]]:
        event_candidates = self._active_temporal_events(
            subject, predicate, observed_through, as_of_time
        )
        active: list[tuple[CorpusEvent, Claim]] = []
        for event in event_candidates:
            claims = (
                self._claim_view_for_read(claim, event, observed_through, as_of_time)
                for claim in self._claims_for_events((event,))
            )
            applicable = [
                claim
                for claim in claims
                if self._claim_applies(claim, observed_through, as_of_time)
            ]
            if applicable:
                active.append(
                    (
                        event,
                        max(
                            applicable,
                            key=lambda item: (
                                item.authority_level,
                                _time(item.effective_time),
                                _time(item.observed_time),
                                item.claim_id,
                            ),
                        ),
                    )
                )
        superseded = {
            event.supersedes_event_id
            for event, _claim in active
            if event.supersedes_event_id
        }
        active = [
            (event, claim)
            for event, claim in active
            if event.event_id not in superseded
        ]
        if not active:
            return (), ()
        top_authority = max(event.authority_level for event, _claim in active)
        strongest = [
            (event, claim)
            for event, claim in active
            if event.authority_level == top_authority
        ]
        values = {_canonical_json(event.value) for event, _claim in strongest}
        if len(values) > 1:
            conflicts = tuple(
                sorted(
                    (replace(claim, state="conflicted") for _event, claim in strongest),
                    key=_claim_sort_key,
                )
            )
            return (), conflicts
        _ranked_event, claim = max(
            strongest,
            key=lambda item: (
                _time(item[0].effective_time),
                _time(item[0].published_time or item[0].observed_time),
                _time(item[0].observed_time),
                item[0].event_id,
                item[1].claim_id,
            ),
        )
        return (
            replace(
                claim,
                state="current",
                write_policy_decision=(
                    "write"
                    if claim.write_policy_decision == "tombstone"
                    else claim.write_policy_decision
                ),
            ),
        ), ()

    def read(
        self,
        subject: str,
        predicate: str,
        *,
        observed_through: str | None = None,
        as_of_time: str | None = None,
        task_family: str | None = None,
        task_signature: str | None = None,
        query_text: str = "",
        episode_limit: int = 3,
    ) -> MemoryRead:
        """Read facts while always preserving matching corpus-event retrieval."""
        if not subject or not predicate:
            raise MemoryContractError(
                "memory reads require non-empty subject and predicate"
            )
        observed, as_of = self._read_times(observed_through, as_of_time)
        corpus = self._corpus_candidates(subject, predicate, observed)
        selected: tuple[Claim, ...] = ()
        conflicts: tuple[Claim, ...] = ()
        if self.policy == "M1":
            selected = self._ranked_m1_claims(subject, predicate, observed)
            selected_event_ids = {
                event_id
                for claim in selected
                for event_id in claim.supporting_event_ids
            }
            corpus_by_id = {event.event_id: event for event in corpus}
            for event in self._published_events(observed):
                if event.event_id in selected_event_ids:
                    corpus_by_id[event.event_id] = event
            corpus = tuple(sorted(corpus_by_id.values(), key=_event_sort_key))
        elif self.policy == "M2":
            candidates = list(
                self._visible_claims(
                    (
                        claim
                        for claim in self.claims
                        if (claim.subject, claim.predicate) == (subject, predicate)
                    ),
                    corpus,
                )
            )
            conflicts = tuple(item for item in candidates if item.state == "conflicted")
            non_conflicts = [item for item in candidates if item.state != "conflicted"]
            if non_conflicts:
                selected = (
                    max(
                        non_conflicts,
                        key=lambda item: (
                            item.authority_level,
                            _time(item.effective_time),
                            item.claim_id,
                        ),
                    ),
                )
        elif self.policy in {"M3", "M4"}:
            selected, conflicts = self._resolved_m3(subject, predicate, observed, as_of)
            selected = self._visible_claims(selected, corpus)
            conflicts = self._visible_claims(conflicts, corpus)
        evidence_ids = {
            event_id
            for claim in (*selected, *conflicts)
            for event_id in claim.supporting_event_ids
        }
        supporting = tuple(event for event in corpus if event.event_id in evidence_ids)
        episodes = ()
        if self.policy == "M4" and task_family:
            episodes = self.retrieve_episodes(
                task_family,
                task_signature=task_signature,
                query_text=query_text,
                limit=episode_limit,
            )
        episode_evidence_ids = tuple(
            sorted(
                {
                    evidence_id
                    for episode in episodes
                    for evidence_id in episode.evidence_path
                }
            )
        )
        available_evidence_ids = set(self._events) | set(self._external_evidence_ids)
        if not set(episode_evidence_ids).issubset(available_evidence_ids):
            raise MemoryContractError(
                "retrieved episode has unresolved evidence provenance"
            )
        episode_event_ids = set(episode_evidence_ids).intersection(self._events)
        supporting_by_id = {event.event_id: event for event in supporting}
        for event_id in episode_event_ids:
            supporting_by_id[event_id] = self._events[event_id]
        supporting = tuple(sorted(supporting_by_id.values(), key=_event_sort_key))
        result = MemoryRead(
            self.policy,
            subject,
            predicate,
            observed,
            as_of,
            corpus,
            selected,
            conflicts,
            supporting,
            episodes,
            episode_evidence_ids,
        )
        if not result.provenance_complete():
            raise MemoryContractError("selected claim lacks a source CorpusEvent")
        return result

    def inspect_current(
        self,
        subject: str,
        predicate: str,
        *,
        observed_through: str | None = None,
        as_of_time: str | None = None,
    ) -> dict[str, Any]:
        """Return an explanation for a current value, conflict, or abstention."""
        read = self.read(
            subject, predicate, observed_through=observed_through, as_of_time=as_of_time
        )
        relevant_ids = {event.event_id for event in read.corpus_events}
        decisions = [
            decision.to_record()
            for decision in self._ledger
            if decision.kind == "event" and decision.item_id in relevant_ids
        ]
        return {
            "schema_version": "contextlab.memory-inspection.v1",
            "read": read.to_record(),
            "selected_claims": [claim.to_record() for claim in read.selected_claims],
            "conflict_claims": [claim.to_record() for claim in read.conflict_claims],
            "corpus_events": [event.to_record() for event in read.corpus_events],
            "decisions": decisions,
            "explanation": (
                "no active claim; abstain"
                if not read.selected_claims and not read.conflict_claims
                else "equal-authority conflict remains unresolved"
                if read.conflict_claims
                else "selected claim passed observed-time, effective-time, validity, and authority checks"
            ),
        }

    @staticmethod
    def _objective_grade(graded_outcome: dict[str, Any]) -> bool:
        source = str(
            graded_outcome.get("source", graded_outcome.get("grader", ""))
        ).lower()
        return (
            graded_outcome.get("objective") is True
            and ("accepted" in graded_outcome or "score" in graded_outcome)
            and source in TRUSTED_OBJECTIVE_GRADERS
        )

    def register_trusted_episode_source(
        self,
        *,
        source_run_id: str,
        trace_id: str,
        source_artifact_sha256: str,
    ) -> None:
        """Trust one immutable run/trace binding as an eligible episode source."""
        if self.policy != "M4":
            raise MemoryContractError("episodic outcomes are available only in M4")
        if (
            not isinstance(source_run_id, str)
            or not source_run_id.strip()
            or not isinstance(trace_id, str)
            or not trace_id.strip()
        ):
            raise MemoryContractError(
                "trusted episode source run and trace IDs must be non-empty strings"
            )
        if not isinstance(source_artifact_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", source_artifact_sha256
        ):
            raise MemoryContractError(
                "trusted episode source artifact must be a lowercase SHA-256 digest"
            )
        key = (source_run_id, trace_id)
        existing = self._trusted_episode_sources.get(key)
        if existing is not None and existing != source_artifact_sha256:
            raise MemoryContractError(
                "trusted episode source has a conflicting source artifact"
            )
        self._trusted_episode_sources[key] = source_artifact_sha256

    def record_episode(
        self,
        *,
        episode_id: str,
        task_signature: str,
        category: str,
        selected_strategy: str,
        evidence_path: Iterable[str],
        graded_outcome: dict[str, Any],
        cost_usd: float,
        latency_ms: int,
        source_run_id: str,
        trace_id: str,
        failure_mode: str | None = None,
        similarity_text: str = "",
    ) -> Episode:
        """Store an M4 outcome card only when an objective grader promoted it."""
        if self.policy != "M4":
            raise MemoryContractError("episodic outcomes are available only in M4")
        if episode_id in self._episodes:
            raise MemoryContractError(f"episode ID already exists: {episode_id}")
        if not re.fullmatch(r"[0-9a-f]{64}", task_signature):
            raise MemoryContractError("task_signature must be a SHA-256 hex digest")
        evidence_ids = tuple(sorted(set(evidence_path)))
        available_evidence_ids = set(self._events) | set(self._external_evidence_ids)
        unknown_evidence = set(evidence_ids) - available_evidence_ids
        if unknown_evidence:
            raise MemoryContractError(
                f"episode evidence does not resolve: {', '.join(sorted(unknown_evidence))}"
            )
        if (
            not isinstance(source_run_id, str)
            or not isinstance(trace_id, str)
            or (source_run_id, trace_id) not in self._trusted_episode_sources
        ):
            raise MemoryContractError(
                "episode does not resolve to a trusted episode source"
            )
        objective = self._objective_grade(graded_outcome)
        episode = Episode(
            episode_id=episode_id,
            task_signature=task_signature,
            category=category,
            selected_strategy=selected_strategy,
            evidence_path=evidence_ids,
            graded_outcome=dict(graded_outcome),
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            failure_mode=failure_mode,
            source_run_id=source_run_id,
            trace_id=trace_id,
            retention_decision="retain" if objective else "remove",
            promotion_decision="promoted" if objective else "rejected",
        )
        errors = validate_instance("Episode", episode.to_record())
        if errors:
            raise MemoryContractError(f"{episode_id}: invalid Episode: {errors}")
        self._episodes[episode_id] = episode
        self._episode_features[episode_id] = similarity_text
        self._episode_order[episode_id] = self._next_episode_order
        self._next_episode_order += 1
        if objective:
            self._append_decision(
                episode_id, "episode", "write", "objective grader promoted episode"
            )
            self._enforce_episode_cap()
        else:
            self._append_decision(
                episode_id,
                "episode",
                "ignore",
                "self-reported or ungraded outcome cannot be promoted",
            )
        return self._episodes[episode_id]

    def _enforce_episode_cap(self) -> None:
        retained = [
            episode
            for episode in self._episodes.values()
            if episode.retention_decision == "retain"
            and episode.promotion_decision == "promoted"
        ]
        overflow = len(retained) - self.episode_cap
        if overflow <= 0:
            return
        for episode in sorted(
            retained,
            key=lambda item: (self._episode_order[item.episode_id], item.episode_id),
        )[:overflow]:
            self._episodes[episode.episode_id] = replace(
                episode, retention_decision="expire"
            )
            self._append_decision(
                episode.episode_id,
                "episode",
                "remove",
                "episode retention cap exceeded",
            )

    def remove_episode(
        self, episode_id: str, *, reason: str = "removed by reviewer"
    ) -> Episode:
        if self.policy != "M4":
            raise MemoryContractError("episodic outcomes are available only in M4")
        try:
            episode = self._episodes[episode_id]
        except KeyError as exc:
            raise MemoryContractError(f"unknown episode: {episode_id}") from exc
        removed = replace(
            episode, retention_decision="remove", promotion_decision="rejected"
        )
        self._episodes[episode_id] = removed
        self._append_decision(episode_id, "episode", "remove", reason)
        return removed

    @staticmethod
    def _similarity(left: str, right: str) -> float:
        left_tokens = set(re.findall(r"[a-z0-9]+", left.lower()))
        right_tokens = set(re.findall(r"[a-z0-9]+", right.lower()))
        if not left_tokens or not right_tokens:
            return 0.0
        return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)

    def retrieve_episodes(
        self,
        task_family: str,
        *,
        task_signature: str | None = None,
        query_text: str = "",
        limit: int = 3,
    ) -> tuple[Episode, ...]:
        if self.policy != "M4":
            return ()
        if limit < 1:
            raise MemoryContractError("episode retrieval limit must be positive")
        candidates = [
            episode
            for episode in self._episodes.values()
            if episode.category == task_family
            and episode.retention_decision == "retain"
            and episode.promotion_decision == "promoted"
        ]
        if task_signature or query_text:
            candidates = [
                episode
                for episode in candidates
                if (
                    bool(task_signature and episode.task_signature == task_signature)
                    or self._similarity(
                        query_text, self._episode_features.get(episode.episode_id, "")
                    )
                    > 0
                )
            ]
        ranked = sorted(
            candidates,
            key=lambda episode: (
                -(
                    1
                    if task_signature and episode.task_signature == task_signature
                    else 0
                ),
                -self._similarity(
                    query_text, self._episode_features.get(episode.episode_id, "")
                ),
                -self._episode_order.get(episode.episode_id, 0),
                episode.episode_id,
            ),
        )
        return tuple(ranked[:limit])

    def _trusted_episode_source_records(self) -> list[dict[str, str]]:
        return [
            {
                "source_run_id": source_run_id,
                "trace_id": trace_id,
                "source_artifact_sha256": source_artifact_sha256,
            }
            for (source_run_id, trace_id), source_artifact_sha256 in sorted(
                self._trusted_episode_sources.items()
            )
        ]

    def snapshot_record(self) -> dict[str, Any]:
        """Return a canonical, lossless state record suitable for JSON persistence."""
        payload = {
            "schema_version": MEMORY_SNAPSHOT_SCHEMA,
            "policy": self.policy,
            "episode_cap": self.episode_cap,
            "external_evidence_ids": sorted(self._external_evidence_ids),
            "trusted_episode_sources": self._trusted_episode_source_records(),
            "corpus_events": [event.to_record() for event in self.corpus_events],
            "claims": [claim.to_record() for claim in self.claims],
            "episodes": [episode.to_record() for episode in self.episodes],
            "episode_features": {
                key: self._episode_features[key]
                for key in sorted(self._episode_features)
            },
            "episode_order": {
                key: self._episode_order[key] for key in sorted(self._episode_order)
            },
            "decision_ledger": [decision.to_record() for decision in self._ledger],
        }
        return {**payload, "snapshot_id": f"memory-{_sha256(payload)[:16]}"}

    @property
    def snapshot_id(self) -> str:
        return str(self.snapshot_record()["snapshot_id"])

    def write_snapshot(self, path: Path) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                self.snapshot_record(), ensure_ascii=False, sort_keys=True, indent=2
            )
            + "\n",
            encoding="utf-8",
        )
        return self.snapshot_id

    @classmethod
    def from_snapshot_record(cls, record: dict[str, Any]) -> "MemoryEngine":
        required = {
            "schema_version",
            "policy",
            "episode_cap",
            "corpus_events",
            "claims",
            "episodes",
            "external_evidence_ids",
            "trusted_episode_sources",
            "episode_features",
            "episode_order",
            "decision_ledger",
            "snapshot_id",
        }
        if (
            set(record) != required
            or record.get("schema_version") != MEMORY_SNAPSHOT_SCHEMA
        ):
            raise MemoryContractError("unsupported or incomplete memory snapshot")
        payload = {key: value for key, value in record.items() if key != "snapshot_id"}
        expected_id = f"memory-{_sha256(payload)[:16]}"
        if record["snapshot_id"] != expected_id:
            raise MemoryContractError(
                "memory snapshot ID does not match canonical content"
            )
        engine = cls(
            str(record["policy"]),
            episode_cap=record["episode_cap"],
            external_evidence_ids=record["external_evidence_ids"],
        )
        source_rows = record["trusted_episode_sources"]
        if not isinstance(source_rows, list):
            raise MemoryContractError("trusted episode source registry must be a list")
        for source in source_rows:
            if not isinstance(source, dict) or set(source) != {
                "source_run_id",
                "trace_id",
                "source_artifact_sha256",
            }:
                raise MemoryContractError(
                    "trusted episode source registry entry is invalid"
                )
            engine.register_trusted_episode_source(
                source_run_id=source["source_run_id"],
                trace_id=source["trace_id"],
                source_artifact_sha256=source["source_artifact_sha256"],
            )
        if source_rows != engine._trusted_episode_source_records():
            raise MemoryContractError(
                "trusted episode source registry is not canonical"
            )
        restored_events: list[CorpusEvent] = []
        for event_record in record["corpus_events"]:
            body = dict(event_record)
            body.pop("schema_version", None)
            restored_events.append(CorpusEvent(**body))
        engine.replay(restored_events)
        canonical_event_decisions = [
            decision.to_record()
            for decision in engine._ledger
            if decision.kind == "event"
        ]
        restored_claims: dict[str, Claim] = {}
        for claim_record in record["claims"]:
            body = dict(claim_record)
            body.pop("schema_version", None)
            body["supporting_event_ids"] = tuple(body["supporting_event_ids"])
            claim = Claim(**body)
            restored_claims[claim.claim_id] = claim
        if [
            claim.to_record()
            for claim in sorted(restored_claims.values(), key=_claim_sort_key)
        ] != [claim.to_record() for claim in engine.claims]:
            raise MemoryContractError(
                "memory snapshot claims do not match deterministic replay"
            )
        for episode_record in record["episodes"]:
            body = dict(episode_record)
            body.pop("schema_version", None)
            body["evidence_path"] = tuple(body["evidence_path"])
            episode = Episode(**body)
            engine._episodes[episode.episode_id] = episode
        engine._episode_features = {
            str(key): str(value) for key, value in record["episode_features"].items()
        }
        engine._episode_order = {
            str(key): int(value) for key, value in record["episode_order"].items()
        }
        engine._next_episode_order = max(engine._episode_order.values(), default=0) + 1
        restored_ledger = [
            MemoryDecision(
                int(item["sequence"]),
                str(item["item_id"]),
                str(item["kind"]),
                str(item["decision"]),
                str(item["reason"]),
                tuple(item["claim_ids"]),
            )
            for item in record["decision_ledger"]
        ]
        if [decision.sequence for decision in restored_ledger] != list(
            range(1, len(restored_ledger) + 1)
        ):
            raise MemoryContractError(
                "memory snapshot decision sequence is not contiguous"
            )
        restored_event_decisions = [
            decision.to_record()
            for decision in restored_ledger
            if decision.kind == "event"
        ]
        if restored_event_decisions != canonical_event_decisions:
            raise MemoryContractError(
                "memory snapshot event decisions do not match deterministic replay"
            )
        engine._ledger = restored_ledger
        engine._validate_internal_state()
        return engine

    @classmethod
    def load_snapshot(cls, path: Path) -> "MemoryEngine":
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MemoryContractError(f"cannot load memory snapshot: {path}") from exc
        if not isinstance(record, dict):
            raise MemoryContractError("memory snapshot must be a JSON object")
        return cls.from_snapshot_record(record)

    def _validate_internal_state(self) -> None:
        event_ids = set(self._events)
        for claim in self._claims.values():
            errors = validate_instance("Claim", claim.to_record())
            if errors:
                raise MemoryContractError(f"{claim.claim_id}: invalid Claim: {errors}")
            if not set(claim.supporting_event_ids).issubset(event_ids):
                raise MemoryContractError(
                    f"{claim.claim_id}: missing source event provenance"
                )
        for episode in self._episodes.values():
            errors = validate_instance("Episode", episode.to_record())
            if errors:
                raise MemoryContractError(
                    f"{episode.episode_id}: invalid Episode: {errors}"
                )
            objectively_graded = self._objective_grade(episode.graded_outcome)
            if episode.promotion_decision == "promoted" and not objectively_graded:
                raise MemoryContractError(
                    f"{episode.episode_id}: promoted episode lacks a trusted objective grade"
                )
            if not objectively_graded and (
                episode.promotion_decision != "rejected"
                or episode.retention_decision != "remove"
            ):
                raise MemoryContractError(
                    f"{episode.episode_id}: ungraded episode has an invalid lifecycle state"
                )
            if (
                episode.retention_decision == "retain"
                and episode.promotion_decision != "promoted"
            ):
                raise MemoryContractError(
                    f"{episode.episode_id}: retained episode is not promoted"
                )
            if not set(episode.evidence_path).issubset(
                event_ids | set(self._external_evidence_ids)
            ):
                raise MemoryContractError(
                    f"{episode.episode_id}: missing episode evidence provenance"
                )
            if (
                episode.source_run_id,
                episode.trace_id,
            ) not in self._trusted_episode_sources:
                raise MemoryContractError(
                    f"{episode.episode_id}: episode lacks a trusted episode source"
                )
        episode_ids = set(self._episodes)
        if (
            set(self._episode_features) != episode_ids
            or set(self._episode_order) != episode_ids
        ):
            raise MemoryContractError(
                "episode feature/order state differs from stored episodes"
            )
