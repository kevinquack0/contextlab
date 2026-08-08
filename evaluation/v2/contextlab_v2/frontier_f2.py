"""Provider-free preparation and scoring contracts for frontier experiment F2.

The model-facing packet and the deterministic M3 reference labels are separate
objects.  A caller can therefore send only ``candidate_packet`` to a provider;
the packet never contains the action target used for scoring.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any

from .baseline import repository_root
from .costs import canonical_ledger_path
from .credentials import redact
from .frontier import _write_immutable_plan, require_frontier_experiment_approved
from .immutable_io import ImmutableIOError
from .provider import CANONICAL_MODEL_ID, MODEL_ID, PROVIDER_SLUG
from .tasking import sha256_json


F2_FREEZE_SCHEMA = "contextlab.f2-action-decision-freeze.v2"
F2_CANDIDATE_PACKET_SCHEMA = "contextlab.f2-candidate-packet.v1"
F2_REFERENCE_SET_SCHEMA = "contextlab.f2-m3-reference-set.v1"
F2_PROMPT_SCHEMA = "contextlab.f2-action-prompt.v1"
F2_MODEL_ACTIONS_SCHEMA = "contextlab.f2-model-actions.v1"
F2_CANDIDATE_SCHEMA = "contextlab.f2-candidate-actions.v2"
F2_SCORE_SCHEMA = "contextlab.f2-score.v2"
F2_ACTIONS = ("write", "retrieve", "update", "summarize", "discard")
F2_REASONING_EFFORTS = ("low", "high")
F2_MODEL_POLICY_ID = "deepseek-v4-flash-active-memory-v1"
F2_MAX_CELLS = 64
F2_FRONTIER_PROTOCOL_SCHEMA = "contextlab.frontier-protocol.v2"
F2_MINIMUM_COMPLETE_TRIALS = 5
F2_PROVIDER_REPEAT_SAMPLE_COUNT = 5
F2_TEMPERATURE = 0.0
F2_TRIAL_IDS = tuple(
    f"f2-trial-{index:02d}" for index in range(1, F2_MINIMUM_COMPLETE_TRIALS + 1)
)
F2_PROVIDER_REPEAT_SAMPLE_IDS = tuple(
    f"f2-provider-repeat-{index:02d}"
    for index in range(1, F2_PROVIDER_REPEAT_SAMPLE_COUNT + 1)
)
F2_FREEZE_PATH = Path("results/v2/frontier/f2/action_decision_freeze.json")
F2_CANDIDATE_PATH = Path("results/v2/frontier/f2/candidate_actions.json")
F2_SCORE_PATH = Path("results/v2/frontier/f2/score.json")

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CELL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_LEDGER_RESERVATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_TRACE_KINDS = {"event_decision", "memory_read"}
_TRACE_LABELS = {"write", "ignore", "merge", "conflict", "tombstone", "remove", "read"}
_MUTATING_ACTIONS = {"write", "update", "summarize"}
_FORBIDDEN_INPUT_KEY_TOKENS = {
    "answer",
    "expected",
    "gold",
    "grade",
    "label",
    "oracle",
    "reference",
    "score",
    "target_action",
}
_FORBIDDEN_PUBLIC_PATH_TOKENS = {
    "sealed",
    "protected",
    "evaluation_only",
    "canonical_fact_ledger",
    "gold",
    "grade",
    "scoring",
}

_SYSTEM_INSTRUCTION = """You are the bounded active-memory action policy for ContextLab F2.
For every supplied cell, choose exactly one action from this fixed vocabulary:
- write: create a durable memory for novel final or corrected information.
- retrieve: inspect existing memory for a query or unresolved competition; do not mutate it.
- update: supersede an existing memory with newer corrected or final information.
- summarize: consolidate redundant compatible memories without losing their source pointers.
- discard: make no durable write, or remove information that is draft, expired, retracted, or tombstoned.

Use only the supplied public cell input. Never answer the underlying task. Never infer or emit a
reference label, score, hidden answer, or chain of thought. Return one JSON object and no prose. The
object must contain only schema_version and actions. Each action item must contain only cell_id and
action, in the same order as the input cells. Do not omit, duplicate, or add cells."""


class F2Error(ValueError):
    """An F2 input, candidate action, score, or persisted artifact is invalid."""


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise F2Error(f"{label} must be a lowercase SHA-256")
    return value


def _artifact_hash_valid(value: Mapping[str, Any]) -> bool:
    artifact = value.get("artifact_sha256")
    body = {key: item for key, item in value.items() if key != "artifact_sha256"}
    return isinstance(artifact, str) and artifact == sha256_json(body)


def _cell_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _CELL_ID.fullmatch(value) is None:
        raise F2Error(f"{label} is invalid")
    return value


def _json_value(value: Any, label: str) -> None:
    try:
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise F2Error(f"{label} must be finite JSON data") from exc


def _reject_target_leakage(value: Any, label: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise F2Error(f"{label} keys must be non-empty strings")
            lowered = key.casefold()
            if any(token in lowered for token in _FORBIDDEN_INPUT_KEY_TOKENS):
                raise F2Error(f"{label} contains a target-leakage field: {key}")
            _reject_target_leakage(item, label)
    elif isinstance(value, list):
        for item in value:
            _reject_target_leakage(item, label)


def _validate_source_artifacts(value: Any) -> None:
    if not isinstance(value, list) or not value:
        raise F2Error("F2 freeze requires at least one public source artifact")
    paths: list[str] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping) or set(raw) != {"path", "sha256"}:
            raise F2Error(f"F2 source artifact {index} fields changed")
        path_value = raw.get("path")
        if not isinstance(path_value, str) or not path_value:
            raise F2Error(f"F2 source artifact {index} path is empty")
        path = Path(path_value)
        if path.is_absolute() or ".." in path.parts:
            raise F2Error(f"F2 source artifact {index} escapes the repository")
        lowered = path_value.casefold()
        if any(token in lowered for token in _FORBIDDEN_PUBLIC_PATH_TOKENS):
            raise F2Error(f"F2 source artifact {index} is not public")
        _sha(raw.get("sha256"), f"F2 source artifact {index} hash")
        paths.append(path_value)
    if paths != sorted(set(paths)):
        raise F2Error("F2 source artifacts must be unique and sorted")


def _prompt_spec() -> dict[str, Any]:
    prompt: dict[str, Any] = {
        "schema_version": F2_PROMPT_SCHEMA,
        "system_instruction": _SYSTEM_INSTRUCTION,
        "allowed_actions": list(F2_ACTIONS),
        "reasoning_efforts": list(F2_REASONING_EFFORTS),
        "response_contract": {
            "schema_version": F2_MODEL_ACTIONS_SCHEMA,
            "top_level_fields": ["schema_version", "actions"],
            "action_fields": ["cell_id", "action"],
            "one_action_per_cell": True,
            "preserve_cell_order": True,
            "additional_properties": False,
        },
        "provider_execution": "not-performed",
    }
    prompt["artifact_sha256"] = sha256_json(prompt)
    return prompt


def _repeat_controls() -> dict[str, Any]:
    return {
        "frontier_protocol_schema": F2_FRONTIER_PROTOCOL_SCHEMA,
        "stochastic_trial_plan": {
            "stochastic": True,
            "minimum_complete_trials": F2_MINIMUM_COMPLETE_TRIALS,
            "trial_ids": list(F2_TRIAL_IDS),
        },
        "temperature_zero_provider_repeat_sample_plan": {
            "temperature": F2_TEMPERATURE,
            "minimum_provider_repeat_samples": F2_PROVIDER_REPEAT_SAMPLE_COUNT,
            "sample_ids": list(F2_PROVIDER_REPEAT_SAMPLE_IDS),
            "trial_sample_pairing": [
                {
                    "trial_id": trial_id,
                    "provider_repeat_sample_id": sample_id,
                }
                for trial_id, sample_id in zip(
                    F2_TRIAL_IDS, F2_PROVIDER_REPEAT_SAMPLE_IDS, strict=True
                )
            ],
        },
    }


def _execution_identities() -> list[tuple[str, str, str]]:
    return [
        (trial_id, sample_id, effort)
        for trial_id, sample_id in zip(
            F2_TRIAL_IDS, F2_PROVIDER_REPEAT_SAMPLE_IDS, strict=True
        )
        for effort in F2_REASONING_EFFORTS
    ]


def _candidate_policy(prompt_sha256: str) -> dict[str, Any]:
    return {
        "policy_id": F2_MODEL_POLICY_ID,
        "model": MODEL_ID,
        "reasoning_efforts": list(F2_REASONING_EFFORTS),
        "temperature": F2_TEMPERATURE,
        "max_cells": F2_MAX_CELLS,
        "max_provider_calls_per_effort": F2_PROVIDER_REPEAT_SAMPLE_COUNT,
        "max_total_provider_calls": len(_execution_identities()),
        "automatic_retries": 0,
        "actions_per_cell": 1,
        "prompt_artifact_sha256": prompt_sha256,
        "repeat_controls": _repeat_controls(),
        "execution_status": "prepared-not-run",
    }


def _reference_action_valid(trace_label: str, action: str) -> bool:
    if trace_label == "write":
        return action in {"write", "update"}
    return action == {
        "read": "retrieve",
        "merge": "summarize",
        "conflict": "retrieve",
        "ignore": "discard",
        "tombstone": "discard",
        "remove": "discard",
    }.get(trace_label)


def build_f2_fixture_freeze(
    cells: Sequence[Mapping[str, Any]],
    *,
    frontier_gate_artifact_sha256: str = "0" * 64,
    source_artifacts: Sequence[Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    """Build a pure, provider-free freeze from explicit public fixture cells.

    This seam does not authorize persistence. Production persistence is always
    bound to an approved F2 frontier entry gate.
    """

    gate_sha = _sha(frontier_gate_artifact_sha256, "frontier gate artifact hash")
    rows = list(cells)
    if not rows or len(rows) > F2_MAX_CELLS:
        raise F2Error(f"F2 requires 1 through {F2_MAX_CELLS} cells")
    prompt = _prompt_spec()
    candidate_cells: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping) or set(raw) != {
            "cell_id",
            "candidate_input",
            "m3_trace_kind",
            "m3_trace_label",
            "reference_action",
        }:
            raise F2Error(f"F2 fixture cell {index} fields changed")
        identifier = _cell_id(raw.get("cell_id"), f"F2 fixture cell {index} ID")
        if identifier in seen:
            raise F2Error(f"duplicate F2 cell ID: {identifier}")
        seen.add(identifier)
        candidate_input = raw.get("candidate_input")
        if not isinstance(candidate_input, Mapping) or not candidate_input:
            raise F2Error(f"F2 fixture cell {identifier} input must be an object")
        candidate_input = dict(candidate_input)
        _json_value(candidate_input, f"F2 fixture cell {identifier} input")
        _reject_target_leakage(candidate_input, f"F2 fixture cell {identifier} input")
        trace_kind = raw.get("m3_trace_kind")
        trace_label = raw.get("m3_trace_label")
        action = raw.get("reference_action")
        if trace_kind not in _TRACE_KINDS or trace_label not in _TRACE_LABELS:
            raise F2Error(f"F2 fixture cell {identifier} M3 trace is invalid")
        if not isinstance(action, str) or action not in F2_ACTIONS:
            raise F2Error(f"F2 fixture cell {identifier} reference action is invalid")
        if not _reference_action_valid(str(trace_label), action):
            raise F2Error(f"F2 fixture cell {identifier} disagrees with its M3 trace")
        candidate_cells.append(
            {
                "cell_id": identifier,
                "input": candidate_input,
                "input_sha256": sha256_json(candidate_input),
            }
        )
        reference: dict[str, Any] = {
            "cell_id": identifier,
            "m3_policy": "M3",
            "m3_trace_kind": trace_kind,
            "m3_trace_label": trace_label,
            "reference_action": action,
            "useful_write": action in _MUTATING_ACTIONS,
            "stale_risk_if_missed": action == "update"
            or trace_label in {"tombstone", "remove"},
        }
        reference["reference_sha256"] = sha256_json(reference)
        references.append(reference)

    order = sorted(
        range(len(candidate_cells)), key=lambda item: candidate_cells[item]["cell_id"]
    )
    candidate_cells = [candidate_cells[index] for index in order]
    references = [references[index] for index in order]
    candidate_packet: dict[str, Any] = {
        "schema_version": F2_CANDIDATE_PACKET_SCHEMA,
        "policy_id": F2_MODEL_POLICY_ID,
        "reasoning_efforts": list(F2_REASONING_EFFORTS),
        "prompt_artifact_sha256": prompt["artifact_sha256"],
        "cells": candidate_cells,
    }
    candidate_packet["artifact_sha256"] = sha256_json(candidate_packet)
    reference_set: dict[str, Any] = {
        "schema_version": F2_REFERENCE_SET_SCHEMA,
        "policy": "M3",
        "cells": references,
    }
    reference_set["artifact_sha256"] = sha256_json(reference_set)
    sources = [
        dict(row)
        for row in (
            source_artifacts
            if source_artifacts is not None
            else ({"path": "fixtures/f2/public_trace.json", "sha256": "0" * 64},)
        )
    ]
    sources.sort(key=lambda row: row.get("path", ""))
    freeze: dict[str, Any] = {
        "schema_version": F2_FREEZE_SCHEMA,
        "experiment_id": "F2",
        "frontier_gate_artifact_sha256": gate_sha,
        "baseline_policy": "M3",
        "action_surface": list(F2_ACTIONS),
        "candidate_policy": _candidate_policy(prompt["artifact_sha256"]),
        "prompt_spec": prompt,
        "candidate_packet": candidate_packet,
        "reference_set": reference_set,
        "source_artifacts": sources,
    }
    freeze["artifact_sha256"] = sha256_json(freeze)
    validate_f2_freeze(freeze)
    return freeze


def validate_f2_freeze(value: Mapping[str, Any]) -> None:
    """Validate the exact label-separated F2 preparation artifact."""

    expected = {
        "schema_version",
        "experiment_id",
        "frontier_gate_artifact_sha256",
        "baseline_policy",
        "action_surface",
        "candidate_policy",
        "prompt_spec",
        "candidate_packet",
        "reference_set",
        "source_artifacts",
        "artifact_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise F2Error("F2 freeze fields changed")
    if value.get("schema_version") != F2_FREEZE_SCHEMA or not _artifact_hash_valid(
        value
    ):
        raise F2Error("F2 freeze envelope or hash is invalid")
    if value.get("experiment_id") != "F2" or value.get("baseline_policy") != "M3":
        raise F2Error("F2 experiment identity changed")
    _sha(value.get("frontier_gate_artifact_sha256"), "frontier gate artifact hash")
    if value.get("action_surface") != list(F2_ACTIONS):
        raise F2Error("F2 action surface changed")

    prompt = value.get("prompt_spec")
    if not isinstance(prompt, Mapping) or dict(prompt) != _prompt_spec():
        raise F2Error("F2 prompt specification changed")
    policy = value.get("candidate_policy")
    if not isinstance(policy, Mapping) or dict(policy) != _candidate_policy(
        prompt["artifact_sha256"]
    ):
        raise F2Error("F2 candidate policy changed")

    packet = value.get("candidate_packet")
    references = value.get("reference_set")
    if not isinstance(packet, Mapping) or set(packet) != {
        "schema_version",
        "policy_id",
        "reasoning_efforts",
        "prompt_artifact_sha256",
        "cells",
        "artifact_sha256",
    }:
        raise F2Error("F2 candidate packet fields changed")
    if (
        packet.get("schema_version") != F2_CANDIDATE_PACKET_SCHEMA
        or packet.get("policy_id") != F2_MODEL_POLICY_ID
        or packet.get("reasoning_efforts") != list(F2_REASONING_EFFORTS)
        or packet.get("prompt_artifact_sha256") != prompt["artifact_sha256"]
        or not _artifact_hash_valid(packet)
    ):
        raise F2Error("F2 candidate packet identity or hash changed")
    if not isinstance(references, Mapping) or set(references) != {
        "schema_version",
        "policy",
        "cells",
        "artifact_sha256",
    }:
        raise F2Error("F2 reference set fields changed")
    if (
        references.get("schema_version") != F2_REFERENCE_SET_SCHEMA
        or references.get("policy") != "M3"
        or not _artifact_hash_valid(references)
    ):
        raise F2Error("F2 reference set identity or hash changed")
    packet_cells = packet.get("cells")
    reference_cells = references.get("cells")
    if (
        not isinstance(packet_cells, list)
        or not isinstance(reference_cells, list)
        or not packet_cells
        or len(packet_cells) != len(reference_cells)
        or len(packet_cells) > F2_MAX_CELLS
    ):
        raise F2Error("F2 candidate and reference cell counts differ")
    identifiers: list[str] = []
    reference_ids: list[str] = []
    for index, cell in enumerate(packet_cells):
        if not isinstance(cell, Mapping) or set(cell) != {
            "cell_id",
            "input",
            "input_sha256",
        }:
            raise F2Error(f"F2 candidate cell {index} fields changed")
        identifier = _cell_id(cell.get("cell_id"), f"F2 candidate cell {index} ID")
        candidate_input = cell.get("input")
        if not isinstance(candidate_input, Mapping) or not candidate_input:
            raise F2Error(f"F2 candidate cell {identifier} input is invalid")
        _json_value(candidate_input, f"F2 candidate cell {identifier} input")
        _reject_target_leakage(candidate_input, f"F2 candidate cell {identifier} input")
        if cell.get("input_sha256") != sha256_json(candidate_input):
            raise F2Error(f"F2 candidate cell {identifier} input hash changed")
        identifiers.append(identifier)
    for index, reference in enumerate(reference_cells):
        if not isinstance(reference, Mapping) or set(reference) != {
            "cell_id",
            "m3_policy",
            "m3_trace_kind",
            "m3_trace_label",
            "reference_action",
            "useful_write",
            "stale_risk_if_missed",
            "reference_sha256",
        }:
            raise F2Error(f"F2 reference cell {index} fields changed")
        identifier = _cell_id(reference.get("cell_id"), f"F2 reference cell {index} ID")
        body = {
            key: item for key, item in reference.items() if key != "reference_sha256"
        }
        if reference.get("reference_sha256") != sha256_json(body):
            raise F2Error(f"F2 reference cell {identifier} hash changed")
        trace_kind = reference.get("m3_trace_kind")
        trace_label = reference.get("m3_trace_label")
        action = reference.get("reference_action")
        if (
            reference.get("m3_policy") != "M3"
            or trace_kind not in _TRACE_KINDS
            or trace_label not in _TRACE_LABELS
            or action not in F2_ACTIONS
            or not _reference_action_valid(str(trace_label), str(action))
            or reference.get("useful_write") != (action in _MUTATING_ACTIONS)
            or reference.get("stale_risk_if_missed")
            != (action == "update" or trace_label in {"tombstone", "remove"})
        ):
            raise F2Error(f"F2 reference cell {identifier} semantics changed")
        reference_ids.append(identifier)
    if identifiers != sorted(set(identifiers)) or reference_ids != identifiers:
        raise F2Error("F2 cells must be unique, sorted, and aligned")
    _validate_source_artifacts(value.get("source_artifacts"))


def _read_public_json(root: Path, relative: Path, label: str) -> dict[str, Any]:
    if relative.is_absolute() or ".." in relative.parts:
        raise F2Error(f"{label} path escapes the repository")
    lowered = relative.as_posix().casefold()
    if any(token in lowered for token in _FORBIDDEN_PUBLIC_PATH_TOKENS):
        raise F2Error(f"{label} path is not public")
    path = root / relative
    try:
        path.resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:
        raise F2Error(f"{label} path escapes the repository") from exc
    if path.is_symlink() or not path.is_file():
        raise F2Error(f"{label} is missing or is a symlink")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise F2Error(f"cannot read {label}") from exc
    if not isinstance(value, dict):
        raise F2Error(f"{label} must be an object")
    return value


def _source_artifact(root: Path, relative: Path) -> dict[str, str]:
    path = root / relative
    if path.is_symlink() or not path.is_file():
        raise F2Error(f"missing public F2 source artifact: {relative.as_posix()}")
    return {
        "path": relative.as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _safe_event_input(event: Any, prior_claims: Sequence[Any]) -> dict[str, Any]:
    related = [
        claim
        for claim in prior_claims
        if (claim.subject, claim.predicate) == (event.subject, event.predicate)
        or event.supersedes_event_id in claim.supporting_event_ids
        or event.tombstone_for_event_id in claim.supporting_event_ids
    ]
    if len(related) > 8:
        raise F2Error("F2 event input exceeds the relevant-memory bound")
    memories = [
        {
            "memory_id": claim.claim_id,
            "subject": claim.subject,
            "predicate": claim.predicate,
            "value": claim.value,
            "state": claim.state,
            "authority_level": claim.authority_level,
            "valid_from": claim.valid_from,
            "valid_to": claim.valid_to,
            "supporting_event_ids": list(claim.supporting_event_ids),
        }
        for claim in related
    ]
    return {
        "trigger": "corpus_event",
        "event": {
            "event_id": event.event_id,
            "scenario_id": event.scenario_id,
            "content_sha256": event.content_hash,
            "observed_time": event.observed_time,
            "effective_time": event.effective_time,
            "published_time": event.published_time,
            "valid_from": event.valid_from,
            "valid_to": event.valid_to,
            "authority_level": event.authority_level,
            "status": event.status,
            "subject": event.subject,
            "predicate": event.predicate,
            "value": event.value,
            "supersedes_event_id": event.supersedes_event_id,
            "tombstone_for_event_id": event.tombstone_for_event_id,
        },
        "relevant_memory_before": memories,
    }


def _event_reference_action(decision: str, event: Any) -> str:
    if decision == "write":
        return "update" if event.supersedes_event_id is not None else "write"
    mapping = {
        "ignore": "discard",
        "tombstone": "discard",
        "remove": "discard",
        "merge": "summarize",
        "conflict": "retrieve",
    }
    action = mapping.get(decision)
    if action is None:
        raise F2Error(f"unsupported public M3 decision: {decision}")
    return action


def prepare_f2_public_freeze(
    root: Path | None = None,
    *,
    approved_frontier_gate: Mapping[str, Any],
) -> dict[str, Any]:
    """Prepare the canonical 64-cell public F2 surface after approved entry.

    The function reads only the explicit public M3 low/high prepared cells, the
    public temporal event stream, and the content-free lifecycle evidence.  It
    performs no provider or network call and writes nothing.
    """

    require_frontier_experiment_approved(approved_frontier_gate, "F2")
    repository = (root or repository_root()).resolve()
    gate_sha = _sha(
        approved_frontier_gate.get("artifact_sha256"),
        "approved frontier gate artifact hash",
    )
    from .g3_execution import validate_prepared_public_g3_cell
    from .g3_lifecycle import (
        G3_LIFECYCLE_PATH,
        validate_g3_lifecycle_evidence,
    )
    from .memory import MemoryEngine
    from .temporal import (
        EVENT_SOURCE_RELATIVE_PATH,
        all_events,
        load_event_source_rows,
        temporal_question_catalog,
    )

    lifecycle = _read_public_json(
        repository, G3_LIFECYCLE_PATH, "public G3 lifecycle evidence"
    )
    try:
        validate_g3_lifecycle_evidence(lifecycle)
    except ValueError as exc:
        raise F2Error("public G3 lifecycle evidence is invalid") from exc
    if lifecycle.get("all_passed") is not True or lifecycle.get("policies") != [
        "M0",
        "M1",
        "M2",
        "M3",
        "M4",
    ]:
        raise F2Error("public G3 lifecycle evidence does not preserve stable M3 labels")

    events = all_events()
    source_rows = load_event_source_rows(repository)
    if set(source_rows) != {event.event_id for event in events} or any(
        source_rows[event.event_id] != event.source_payload() for event in events
    ):
        raise F2Error(
            "public temporal event stream differs from its deterministic catalog"
        )

    public_questions = sorted(
        (
            question
            for question in temporal_question_catalog()
            if not question.is_sealed
        ),
        key=lambda question: question.task_id,
    )
    trace_by_event: dict[str, dict[str, Any]] = {}
    prepared_by_task: dict[str, dict[str, Any]] = {}
    source_paths = {G3_LIFECYCLE_PATH, EVENT_SOURCE_RELATIVE_PATH}
    for question in public_questions:
        for effort in F2_REASONING_EFFORTS:
            relative = Path(
                "results/v2/memory/prepared/g3-public-v1/M3/"
                f"{effort}/{question.task_id}.json"
            )
            prepared = _read_public_json(
                repository,
                relative,
                f"public G3 M3/{effort} trace {question.task_id}",
            )
            try:
                validate_prepared_public_g3_cell(prepared, root=repository)
            except ValueError as exc:
                raise F2Error(
                    f"public G3 M3/{effort} trace {question.task_id} is invalid"
                ) from exc
            spec = prepared.get("run_spec")
            task = spec.get("task") if isinstance(spec, Mapping) else None
            memory_read = prepared.get("memory_read")
            if (
                not isinstance(spec, Mapping)
                or spec.get("policy") != "M3"
                or spec.get("reasoning_effort") != effort
                or spec.get("requested_model") != MODEL_ID
                or not isinstance(task, Mapping)
                or task.get("task_id") != question.task_id
                or task.get("suite") != "temporal"
                or prepared.get("memory_read_status") != "ready"
                or not isinstance(memory_read, Mapping)
                or memory_read.get("policy") != "M3"
                or memory_read.get("provenance_complete") is not True
            ):
                raise F2Error(
                    f"public G3 M3/{effort} trace {question.task_id} identity changed"
                )
            previous_prepared = prepared_by_task.get(question.task_id)
            if previous_prepared is not None and (
                previous_prepared["decision_ledger"] != prepared["decision_ledger"]
                or previous_prepared["memory_read"] != prepared["memory_read"]
                or previous_prepared["observable_event_ids"]
                != prepared["observable_event_ids"]
            ):
                raise F2Error(
                    f"public M3 labels differ by reasoning effort for {question.task_id}"
                )
            for raw_decision in prepared["decision_ledger"]:
                if not isinstance(raw_decision, Mapping):
                    raise F2Error("public M3 decision trace contains a malformed row")
                item_id = raw_decision.get("item_id")
                if not isinstance(item_id, str) or not item_id:
                    raise F2Error("public M3 decision trace contains an empty item ID")
                decision = dict(raw_decision)
                previous = trace_by_event.get(item_id)
                if previous is not None and previous != decision:
                    raise F2Error(f"public M3 decision label is unstable for {item_id}")
                trace_by_event[item_id] = decision
            prepared_by_task.setdefault(question.task_id, prepared)
            source_paths.add(relative)

    if set(trace_by_event) != {event.event_id for event in events}:
        raise F2Error("public G3 M3 traces do not label every public temporal event")

    fixture_cells: list[dict[str, Any]] = []
    engine = MemoryEngine("M3")
    for event in events:
        trace = trace_by_event[event.event_id]
        prior_claims = engine.claims
        observed = engine.ingest((event,))
        if len(observed) != 1 or observed[0].to_record() != trace:
            raise F2Error(
                f"saved M3 label differs from deterministic replay for {event.event_id}"
            )
        decision = str(trace["decision"])
        fixture_cells.append(
            {
                "cell_id": f"event:{event.event_id}",
                "candidate_input": _safe_event_input(event, prior_claims),
                "m3_trace_kind": "event_decision",
                "m3_trace_label": decision,
                "reference_action": _event_reference_action(decision, event),
            }
        )

    for question in public_questions:
        prepared = prepared_by_task[question.task_id]
        spec = prepared["run_spec"]
        task = spec["task"]
        memory_read = prepared["memory_read"]
        fixture_cells.append(
            {
                "cell_id": f"query:{question.task_id}",
                "candidate_input": {
                    "trigger": "memory_query",
                    "query_id": question.task_id,
                    "task_family": task["task_family"],
                    "question_sha256": task["question_sha256"],
                    "subject": memory_read["subject"],
                    "predicate": memory_read["predicate"],
                    "observed_through": memory_read["observed_through"],
                    "as_of_time": memory_read["as_of_time"],
                },
                "m3_trace_kind": "memory_read",
                "m3_trace_label": "read",
                "reference_action": "retrieve",
            }
        )

    if len(fixture_cells) != F2_MAX_CELLS:
        raise F2Error("canonical F2 surface must contain exactly 64 public cells")
    sources = [
        _source_artifact(repository, relative)
        for relative in sorted(source_paths, key=lambda path: path.as_posix())
    ]
    return build_f2_fixture_freeze(
        fixture_cells,
        frontier_gate_artifact_sha256=gate_sha,
        source_artifacts=sources,
    )


def _decimal_text(value: Any, label: str, *, positive: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise F2Error(f"{label} must be an exact decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise F2Error(f"{label} must be an exact decimal string") from exc
    if not parsed.is_finite() or parsed < 0 or (positive and parsed <= 0):
        raise F2Error(f"{label} is outside the allowed range")
    return value


def _nullable_nonempty(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise F2Error(f"{label} must be null or a non-empty string")
    return value


def _native_token_usage(value: Any, *, succeeded: bool) -> dict[str, int | None]:
    if not isinstance(value, Mapping) or set(value) != {
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
    }:
        raise F2Error("F2 native token usage fields changed")
    usage: dict[str, int | None] = {}
    for field in ("prompt_tokens", "completion_tokens", "reasoning_tokens"):
        item = value.get(field)
        if item is None and not succeeded:
            usage[field] = None
            continue
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise F2Error(f"F2 native token usage {field} is invalid")
        usage[field] = item
    return usage


def _billing(value: Any) -> dict[str, str]:
    expected = {
        "billed_cost_usd",
        "input_price_usd_per_million",
        "output_price_usd_per_million",
        "reasoning_price_usd_per_million",
        "cost_source",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise F2Error("F2 provider billing fields changed")
    source = value.get("cost_source")
    if not isinstance(source, str) or not source:
        raise F2Error("F2 provider billing cost_source is invalid")
    return {
        "billed_cost_usd": _decimal_text(
            value.get("billed_cost_usd"), "F2 billed cost"
        ),
        "input_price_usd_per_million": _decimal_text(
            value.get("input_price_usd_per_million"),
            "F2 billed input price",
            positive=True,
        ),
        "output_price_usd_per_million": _decimal_text(
            value.get("output_price_usd_per_million"),
            "F2 billed output price",
            positive=True,
        ),
        "reasoning_price_usd_per_million": _decimal_text(
            value.get("reasoning_price_usd_per_million"),
            "F2 billed reasoning price",
            positive=True,
        ),
        "cost_source": source,
    }


def _validate_actions(actions: Any, freeze: Mapping[str, Any], effort: str) -> None:
    expected_cells = freeze["candidate_packet"]["cells"]
    if not isinstance(actions, list) or len(actions) != len(expected_cells):
        raise F2Error(f"F2 {effort} result must return exactly one action per cell")
    identifiers: list[str] = []
    for index, action in enumerate(actions):
        if not isinstance(action, Mapping) or set(action) != {"cell_id", "action"}:
            raise F2Error(f"F2 {effort} candidate action {index} fields changed")
        identifier = _cell_id(
            action.get("cell_id"), f"F2 {effort} candidate action {index} ID"
        )
        if action.get("action") not in F2_ACTIONS:
            raise F2Error(f"F2 {effort} candidate action {identifier} is invalid")
        identifiers.append(identifier)
    expected_ids = [cell["cell_id"] for cell in expected_cells]
    if identifiers != expected_ids:
        raise F2Error(
            f"F2 {effort} actions are missing, duplicated, extra, or reordered"
        )


def _provider_receipt(
    raw: Mapping[str, Any],
    freeze: Mapping[str, Any],
    *,
    effort: str,
    trial_id: str,
    provider_repeat_sample_id: str,
) -> dict[str, Any]:
    expected = {
        "trial_id",
        "provider_repeat_sample_id",
        "temperature",
        "reasoning_effort",
        "status",
        "requested_model",
        "resolved_model",
        "provider",
        "request_id",
        "ledger_reservation_id",
        "native_token_usage",
        "billing",
        "latency_ms",
        "retry_count",
        "error",
        "response",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise F2Error(f"F2 {effort} provider result fields changed")
    if (
        raw.get("trial_id") != trial_id
        or raw.get("provider_repeat_sample_id") != provider_repeat_sample_id
        or raw.get("temperature") != F2_TEMPERATURE
        or raw.get("reasoning_effort") != effort
    ):
        raise F2Error(f"F2 provider result order or reasoning effort changed: {effort}")
    if raw.get("requested_model") != MODEL_ID:
        raise F2Error(f"F2 {effort} requested model changed")
    status = raw.get("status")
    if status not in {"succeeded", "failed"}:
        raise F2Error(f"F2 {effort} provider status is invalid")
    succeeded = status == "succeeded"
    resolved_model = _nullable_nonempty(raw.get("resolved_model"), "resolved model")
    provider = _nullable_nonempty(raw.get("provider"), "provider")
    request_id = _nullable_nonempty(raw.get("request_id"), "request ID")
    reservation_id = raw.get("ledger_reservation_id")
    if (
        not isinstance(reservation_id, str)
        or _LEDGER_RESERVATION_ID.fullmatch(reservation_id) is None
    ):
        raise F2Error(f"F2 {effort} ledger reservation ID is invalid")
    usage = _native_token_usage(raw.get("native_token_usage"), succeeded=succeeded)
    billing = _billing(raw.get("billing"))
    latency = raw.get("latency_ms")
    if latency is not None and (
        isinstance(latency, bool)
        or not isinstance(latency, (int, float))
        or not math.isfinite(latency)
        or latency < 0
    ):
        raise F2Error(f"F2 {effort} latency is invalid")
    retry_count = raw.get("retry_count")
    if retry_count != 0 or isinstance(retry_count, bool):
        raise F2Error(f"F2 {effort} retry count must remain zero")
    error = raw.get("error")
    response = raw.get("response")
    if succeeded:
        if (
            resolved_model not in {MODEL_ID, CANONICAL_MODEL_ID}
            or provider is None
            or provider.casefold() != PROVIDER_SLUG
            or request_id is None
            or latency is None
            or error is not None
            or not isinstance(response, Mapping)
            or set(response) != {"schema_version", "actions"}
            or response.get("schema_version") != F2_MODEL_ACTIONS_SCHEMA
        ):
            raise F2Error(f"F2 {effort} successful provider receipt is incomplete")
        _validate_actions(response.get("actions"), freeze, effort)
        normalized_error = None
        normalized_response: dict[str, Any] | None = {
            "schema_version": F2_MODEL_ACTIONS_SCHEMA,
            "actions": [dict(action) for action in response["actions"]],
        }
    else:
        if not isinstance(error, Mapping) or set(error) != {
            "type",
            "message",
            "stage",
        }:
            raise F2Error(f"F2 {effort} failed call needs an explicit error record")
        normalized_error = {}
        for field in ("type", "message", "stage"):
            item = error.get(field)
            if not isinstance(item, str) or not item:
                raise F2Error(f"F2 {effort} failed-call error {field} is invalid")
            normalized_error[field] = item
        if redact(normalized_error) != normalized_error:
            raise F2Error(f"F2 {effort} failed-call error contains credential material")
        if response is not None:
            raise F2Error(f"F2 {effort} failed call cannot contain model actions")
        if resolved_model is not None and resolved_model not in {
            MODEL_ID,
            CANONICAL_MODEL_ID,
        }:
            raise F2Error(f"F2 {effort} failed call resolved a different model")
        if provider is not None and provider.casefold() != PROVIDER_SLUG:
            raise F2Error(f"F2 {effort} failed call used a different provider")
        normalized_response = None
    receipt: dict[str, Any] = {
        "trial_id": trial_id,
        "provider_repeat_sample_id": provider_repeat_sample_id,
        "temperature": F2_TEMPERATURE,
        "reasoning_effort": effort,
        "status": status,
        "requested_model": MODEL_ID,
        "resolved_model": resolved_model,
        "provider": provider,
        "request_id": request_id,
        "ledger_reservation_id": reservation_id,
        "native_token_usage": usage,
        "billing": billing,
        "latency_ms": latency,
        "retry_count": retry_count,
        "error": normalized_error,
        "response": normalized_response,
    }
    receipt["receipt_sha256"] = sha256_json(receipt)
    return receipt


def import_f2_model_results(
    freeze: Mapping[str, Any], *, results: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Import five complete low/high trials as temperature-zero repeats."""

    validate_f2_freeze(freeze)
    rows = list(results)
    identities = _execution_identities()
    if len(rows) != len(identities):
        raise F2Error("F2 candidate requires five complete low/high provider trials")
    receipts = [
        _provider_receipt(
            raw,
            freeze,
            effort=effort,
            trial_id=trial_id,
            provider_repeat_sample_id=sample_id,
        )
        for raw, (trial_id, sample_id, effort) in zip(rows, identities, strict=True)
    ]
    policy = freeze["candidate_policy"]
    candidate: dict[str, Any] = {
        "schema_version": F2_CANDIDATE_SCHEMA,
        "freeze_artifact_sha256": freeze["artifact_sha256"],
        "candidate_packet_artifact_sha256": freeze["candidate_packet"][
            "artifact_sha256"
        ],
        "policy_id": policy["policy_id"],
        "prompt_artifact_sha256": policy["prompt_artifact_sha256"],
        "reasoning_efforts": list(F2_REASONING_EFFORTS),
        "trial_ids": list(F2_TRIAL_IDS),
        "provider_repeat_sample_ids": list(F2_PROVIDER_REPEAT_SAMPLE_IDS),
        "results": receipts,
    }
    candidate["artifact_sha256"] = sha256_json(candidate)
    validate_f2_candidate(candidate, freeze)
    return candidate


def build_f2_candidate_record(
    freeze: Mapping[str, Any], *, results: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Alias the exact ten-receipt import seam for fixture and external callers."""

    return import_f2_model_results(freeze, results=results)


def validate_f2_candidate(value: Mapping[str, Any], freeze: Mapping[str, Any]) -> None:
    """Reject incomplete, altered, reordered, or unbound effort receipts."""

    validate_f2_freeze(freeze)
    expected = {
        "schema_version",
        "freeze_artifact_sha256",
        "candidate_packet_artifact_sha256",
        "policy_id",
        "prompt_artifact_sha256",
        "reasoning_efforts",
        "trial_ids",
        "provider_repeat_sample_ids",
        "results",
        "artifact_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise F2Error("F2 candidate fields changed")
    if value.get("schema_version") != F2_CANDIDATE_SCHEMA or not _artifact_hash_valid(
        value
    ):
        raise F2Error("F2 candidate envelope or hash is invalid")
    policy = freeze["candidate_policy"]
    if (
        value.get("freeze_artifact_sha256") != freeze["artifact_sha256"]
        or value.get("candidate_packet_artifact_sha256")
        != freeze["candidate_packet"]["artifact_sha256"]
        or value.get("policy_id") != policy["policy_id"]
        or value.get("prompt_artifact_sha256") != policy["prompt_artifact_sha256"]
        or value.get("reasoning_efforts") != list(F2_REASONING_EFFORTS)
        or value.get("trial_ids") != list(F2_TRIAL_IDS)
        or value.get("provider_repeat_sample_ids")
        != list(F2_PROVIDER_REPEAT_SAMPLE_IDS)
    ):
        raise F2Error("F2 candidate is bound to a different frozen policy")
    results = value.get("results")
    identities = _execution_identities()
    if not isinstance(results, list) or len(results) != len(identities):
        raise F2Error("F2 candidate must retain five complete low/high trials")
    for receipt, (trial_id, sample_id, effort) in zip(results, identities, strict=True):
        if not isinstance(receipt, Mapping) or set(receipt) != {
            "trial_id",
            "provider_repeat_sample_id",
            "temperature",
            "reasoning_effort",
            "status",
            "requested_model",
            "resolved_model",
            "provider",
            "request_id",
            "ledger_reservation_id",
            "native_token_usage",
            "billing",
            "latency_ms",
            "retry_count",
            "error",
            "response",
            "receipt_sha256",
        }:
            raise F2Error(f"F2 {effort} receipt fields changed")
        body = {key: item for key, item in receipt.items() if key != "receipt_sha256"}
        if receipt.get("receipt_sha256") != sha256_json(body):
            raise F2Error(f"F2 {effort} receipt hash changed")
        expected_receipt = _provider_receipt(
            body,
            freeze,
            effort=effort,
            trial_id=trial_id,
            provider_repeat_sample_id=sample_id,
        )
        if dict(receipt) != expected_receipt:
            raise F2Error(f"F2 {effort} receipt differs from its provider result")


def _rate(numerator: int, denominator: int) -> dict[str, Any]:
    if denominator == 0:
        return {
            "numerator": numerator,
            "denominator": denominator,
            "value": None,
            "status": "not-applicable",
        }
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator,
        "status": "measured",
    }


def _score_without_validation(
    freeze: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, Any]:
    references = freeze["reference_set"]["cells"]
    effort_scores: list[dict[str, Any]] = []
    succeeded_count = 0
    failed_count = 0
    total_cost = Decimal("0")
    for receipt in candidate["results"]:
        effort = receipt["reasoning_effort"]
        cost = receipt["billing"]["billed_cost_usd"]
        total_cost += Decimal(cost)
        if receipt["status"] == "failed":
            unavailable = {
                "numerator": None,
                "denominator": None,
                "value": None,
                "status": "not-measured-failed-call",
            }
            effort_scores.append(
                {
                    "trial_id": receipt["trial_id"],
                    "provider_repeat_sample_id": receipt["provider_repeat_sample_id"],
                    "temperature": receipt["temperature"],
                    "reasoning_effort": effort,
                    "status": "failed-call",
                    "provider_receipt_sha256": receipt["receipt_sha256"],
                    "cell_count": 0,
                    "comparisons": [],
                    "metrics": {
                        "useful_write_precision": dict(unavailable),
                        "missed_write_rate": dict(unavailable),
                        "stale_memory_rate": dict(unavailable),
                        "tool_action_accuracy": dict(unavailable),
                        "cost_usd": {
                            "value": cost,
                            "status": "reported",
                            "provider_receipt_sha256": receipt["receipt_sha256"],
                        },
                        "temporal_task_quality": {
                            "value": None,
                            "status": "not-measured-failed-call",
                            "reason": "provider call failed before action decisions",
                        },
                    },
                }
            )
            failed_count += 1
            continue

        actions = receipt["response"]["actions"]
        comparisons: list[dict[str, Any]] = []
        useful_correct = 0
        predicted_writes = 0
        missed_writes = 0
        reference_writes = 0
        stale = 0
        stale_risks = 0
        exact = 0
        for reference, action in zip(references, actions, strict=True):
            candidate_action = action["action"]
            expected_action = reference["reference_action"]
            matches = candidate_action == expected_action
            useful_reference = reference["useful_write"]
            stale_reference = reference["stale_risk_if_missed"]
            predicted_write = candidate_action in _MUTATING_ACTIONS
            useful = predicted_write and matches
            missed = useful_reference and not matches
            stale_cell = stale_reference and not matches
            predicted_writes += int(predicted_write)
            useful_correct += int(useful)
            reference_writes += int(useful_reference)
            missed_writes += int(missed)
            stale_risks += int(stale_reference)
            stale += int(stale_cell)
            exact += int(matches)
            comparisons.append(
                {
                    "cell_id": reference["cell_id"],
                    "candidate_action": candidate_action,
                    "reference_action": expected_action,
                    "exact_match": matches,
                    "useful_write": useful,
                    "missed_write": missed,
                    "stale_memory": stale_cell,
                }
            )
        effort_scores.append(
            {
                "trial_id": receipt["trial_id"],
                "provider_repeat_sample_id": receipt["provider_repeat_sample_id"],
                "temperature": receipt["temperature"],
                "reasoning_effort": effort,
                "status": "scored",
                "provider_receipt_sha256": receipt["receipt_sha256"],
                "cell_count": len(comparisons),
                "comparisons": comparisons,
                "metrics": {
                    "useful_write_precision": _rate(useful_correct, predicted_writes),
                    "missed_write_rate": _rate(missed_writes, reference_writes),
                    "stale_memory_rate": _rate(stale, stale_risks),
                    "tool_action_accuracy": _rate(exact, len(comparisons)),
                    "cost_usd": {
                        "value": cost,
                        "status": "reported",
                        "provider_receipt_sha256": receipt["receipt_sha256"],
                    },
                    "temporal_task_quality": {
                        "value": None,
                        "status": "pending-not-measured",
                        "reason": "action decisions alone do not measure downstream answer quality",
                    },
                },
            }
        )
        succeeded_count += 1
    score: dict[str, Any] = {
        "schema_version": F2_SCORE_SCHEMA,
        "freeze_artifact_sha256": freeze["artifact_sha256"],
        "candidate_artifact_sha256": candidate["artifact_sha256"],
        "reasoning_efforts": list(F2_REASONING_EFFORTS),
        "trial_ids": list(F2_TRIAL_IDS),
        "provider_repeat_sample_ids": list(F2_PROVIDER_REPEAT_SAMPLE_IDS),
        "effort_scores": effort_scores,
        "aggregate": {
            "succeeded_effort_count": succeeded_count,
            "failed_effort_count": failed_count,
            "billed_cost_usd": format(total_cost, "f"),
        },
    }
    score["artifact_sha256"] = sha256_json(score)
    return score


def score_f2_candidate(
    freeze: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, Any]:
    """Score one imported candidate deterministically; make no provider call."""

    validate_f2_freeze(freeze)
    validate_f2_candidate(candidate, freeze)
    score = _score_without_validation(freeze, candidate)
    validate_f2_score(score, freeze=freeze, candidate=candidate)
    return score


def validate_f2_score(
    value: Mapping[str, Any], *, freeze: Mapping[str, Any], candidate: Mapping[str, Any]
) -> None:
    """Require exact equality with a fresh deterministic F2 score."""

    validate_f2_freeze(freeze)
    validate_f2_candidate(candidate, freeze)
    if not isinstance(value, Mapping) or not _artifact_hash_valid(value):
        raise F2Error("F2 score envelope or hash is invalid")
    expected = _score_without_validation(freeze, candidate)
    if dict(value) != expected:
        raise F2Error("F2 score differs from its frozen labels and candidate actions")


def _require_approved_gate(
    gate: Mapping[str, Any], *, expected_artifact_sha256: str | None = None
) -> str:
    try:
        require_frontier_experiment_approved(gate, "F2")
    except ValueError as exc:
        raise F2Error(
            "F2 persistence requires an approved eligible frontier entry"
        ) from exc
    gate_sha = _sha(gate.get("artifact_sha256"), "approved frontier gate artifact hash")
    if expected_artifact_sha256 is not None and gate_sha != expected_artifact_sha256:
        raise F2Error("F2 artifact is bound to a different approved frontier gate")
    return gate_sha


def _verify_source_files(root: Path, freeze: Mapping[str, Any]) -> None:
    resolved_root = root.resolve()
    for index, source in enumerate(freeze["source_artifacts"]):
        relative = Path(source["path"])
        path = root / relative
        try:
            path.resolve(strict=False).relative_to(resolved_root)
        except ValueError as exc:
            raise F2Error(f"F2 source artifact {index} escapes the repository") from exc
        if path.is_symlink() or not path.is_file():
            raise F2Error(f"F2 source artifact {index} is missing or is a symlink")
        if hashlib.sha256(path.read_bytes()).hexdigest() != source["sha256"]:
            raise F2Error(f"F2 source artifact {index} hash changed")


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _safe_create_only_path(root: Path, path: Path) -> Path:
    """Preflight existing parents and keep immutable F2 writes inside root."""

    raw_root = root.absolute()
    if raw_root.is_symlink() or not raw_root.is_dir():
        raise F2Error("F2 repository root is missing or unsafe")
    try:
        relative = path.relative_to(raw_root)
    except ValueError as exc:
        raise F2Error("F2 artifact path escapes the repository") from exc
    if relative.is_absolute() or ".." in relative.parts:
        raise F2Error("F2 artifact path escapes the repository")
    resolved_root = raw_root.resolve(strict=True)
    parent = raw_root
    for part in relative.parent.parts:
        parent = parent / part
        if not os.path.lexists(parent):
            break
        if parent.is_symlink() or not parent.is_dir():
            raise F2Error("F2 artifact parent is unsafe")
        try:
            parent.resolve(strict=True).relative_to(resolved_root)
        except ValueError as exc:
            raise F2Error("F2 artifact parent escapes the repository") from exc
    target = raw_root / relative
    if os.path.lexists(target) and target.is_symlink():
        raise F2Error("F2 immutable artifact path is a symlink")
    return target


def _create_only_plan(root: Path, plan: Mapping[Path, bytes]) -> None:
    safe_plan = {
        _safe_create_only_path(root, path): data for path, data in plan.items()
    }
    try:
        _write_immutable_plan(root, safe_plan)
    except ImmutableIOError as exc:
        raise F2Error(
            "immutable F2 artifact already exists with different content or is unsafe"
        ) from exc


def write_f2_freeze(
    root: Path | None = None,
    *,
    approved_frontier_gate: Mapping[str, Any],
    freeze: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist one approved, public-source-bound F2 freeze without overwrite."""

    repository = (root or repository_root()).absolute()
    validate_f2_freeze(freeze)
    _require_approved_gate(
        approved_frontier_gate,
        expected_artifact_sha256=freeze["frontier_gate_artifact_sha256"],
    )
    _verify_source_files(repository, freeze)
    expected = prepare_f2_public_freeze(
        repository, approved_frontier_gate=approved_frontier_gate
    )
    if dict(freeze) != expected:
        raise F2Error("F2 persistence requires the exact canonical public freeze")
    _create_only_plan(repository, {repository / F2_FREEZE_PATH: _json_bytes(freeze)})
    return dict(freeze)


def freeze_f2_public_experiment(
    root: Path | None = None,
    *,
    approved_frontier_gate: Mapping[str, Any],
) -> dict[str, Any]:
    """Prepare and create-only persist the canonical public F2 freeze."""

    repository = (root or repository_root()).absolute()
    freeze = prepare_f2_public_freeze(
        repository, approved_frontier_gate=approved_frontier_gate
    )
    return write_f2_freeze(
        repository,
        approved_frontier_gate=approved_frontier_gate,
        freeze=freeze,
    )


def _load_saved_f2_freeze(
    root: Path, approved_frontier_gate: Mapping[str, Any]
) -> dict[str, Any]:
    freeze = _read_public_json(root, F2_FREEZE_PATH, "saved F2 freeze")
    validate_f2_freeze(freeze)
    _require_approved_gate(
        approved_frontier_gate,
        expected_artifact_sha256=freeze["frontier_gate_artifact_sha256"],
    )
    _verify_source_files(root, freeze)
    canonical = prepare_f2_public_freeze(
        root, approved_frontier_gate=approved_frontier_gate
    )
    if freeze != canonical:
        raise F2Error("saved F2 freeze differs from the exact canonical public freeze")
    return freeze


def _paid_ledger_rows(root: Path) -> list[dict[str, Any]]:
    path = canonical_ledger_path(root)
    if not path.is_file() or path.is_symlink():
        raise F2Error("F2 paid ledger evidence is missing or unsafe")
    rows: list[dict[str, Any]] = []
    try:
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise F2Error(f"F2 paid ledger row {line_number} must be an object")
            rows.append(row)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise F2Error("F2 paid ledger evidence cannot be read") from exc
    return rows


def _reservation_events(
    rows: Sequence[Mapping[str, Any]], reservation_id: str
) -> list[Mapping[str, Any]]:
    events = [row for row in rows if row.get("reservation_id") == reservation_id]
    if not events or any(
        row.get("schema_version") != "contextlab.cost-event.v1" for row in events
    ):
        raise F2Error("F2 paid ledger event schema changed")
    return events


def _ledger_decimal(value: Any, label: str) -> Decimal:
    return Decimal(_decimal_text(value, label))


def _validate_reservation(event: Mapping[str, Any]) -> Decimal:
    if set(event) != {
        "schema_version",
        "event",
        "reservation_id",
        "input_token_limit",
        "output_token_limit",
        "call_count",
        "estimated_usd",
    }:
        raise F2Error("F2 paid ledger reservation fields changed")
    if (
        event.get("event") != "reserve"
        or event.get("call_count") != 1
        or isinstance(event.get("input_token_limit"), bool)
        or not isinstance(event.get("input_token_limit"), int)
        or event["input_token_limit"] < 0
        or isinstance(event.get("output_token_limit"), bool)
        or not isinstance(event.get("output_token_limit"), int)
        or event["output_token_limit"] < 0
    ):
        raise F2Error("F2 paid ledger reservation is invalid")
    return _ledger_decimal(event.get("estimated_usd"), "F2 reserved cost")


def _validate_successful_ledger_receipt(
    receipt: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
) -> None:
    reservation_id = str(receipt["ledger_reservation_id"])
    events = _reservation_events(rows, reservation_id)
    if [row.get("event") for row in events] != ["reserve", "acknowledge", "settle"]:
        raise F2Error("F2 successful call requires reserve, acknowledge, settle only")
    estimated = _validate_reservation(events[0])
    acknowledgment = events[1].get("metadata")
    settlement = events[2]
    metadata = settlement.get("metadata")
    if not isinstance(acknowledgment, Mapping) or not isinstance(metadata, Mapping):
        raise F2Error("F2 paid ledger provider metadata is missing")
    usage = receipt["native_token_usage"]
    billing = receipt["billing"]
    expected_request = receipt["request_id"]
    billed = _ledger_decimal(billing["billed_cost_usd"], "F2 billed cost")
    if billed > estimated:
        raise F2Error("F2 billed cost exceeds its reservation")
    if (
        acknowledgment.get("request_id") != expected_request
        or metadata.get("request_id") != expected_request
        or str(acknowledgment.get("provider", "")).casefold() != PROVIDER_SLUG
        or str(metadata.get("provider", "")).casefold() != PROVIDER_SLUG
        or acknowledgment.get("resolved_model") != receipt["resolved_model"]
        or metadata.get("requested_model") != MODEL_ID
        or metadata.get("resolved_model") != receipt["resolved_model"]
        or acknowledgment.get("prompt_tokens") != usage["prompt_tokens"]
        or acknowledgment.get("completion_tokens") != usage["completion_tokens"]
        or metadata.get("native_prompt_tokens") != usage["prompt_tokens"]
        or metadata.get("native_completion_tokens") != usage["completion_tokens"]
        or metadata.get("native_reasoning_tokens") != usage["reasoning_tokens"]
        or _ledger_decimal(settlement.get("actual_usd"), "F2 settled cost") != billed
        or _ledger_decimal(metadata.get("actual_usd"), "F2 settlement cost") != billed
        or metadata.get("cost_source") != billing["cost_source"]
        or metadata.get("latency_ms") != receipt["latency_ms"]
        or metadata.get("retry_count") != receipt["retry_count"]
        or metadata.get("error") is not None
    ):
        raise F2Error("F2 paid ledger differs from the imported provider receipt")


def _validate_failed_ledger_receipt(
    receipt: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
) -> None:
    reservation_id = str(receipt["ledger_reservation_id"])
    events = _reservation_events(rows, reservation_id)
    event_names = [row.get("event") for row in events]
    estimated = _validate_reservation(events[0])
    if event_names not in (
        ["reserve", "cancel"],
        ["reserve", "failure", "cancel"],
        ["reserve", "acknowledge", "failure", "cancel"],
        ["reserve", "acknowledge", "failure", "settle"],
        ["reserve", "acknowledge", "settle", "failure"],
    ):
        raise F2Error(
            "F2 failed call requires a terminal cancel or settle ledger lifecycle"
        )
    billing = receipt["billing"]
    billed = _ledger_decimal(billing["billed_cost_usd"], "F2 failed billed cost")
    if billed > estimated:
        raise F2Error("F2 failed-call cost exceeds its reservation")
    request_id = receipt.get("request_id")
    error = receipt["error"]

    has_acknowledgment = "acknowledge" in event_names
    if has_acknowledgment:
        acknowledgment = events[1].get("metadata")
        usage = receipt["native_token_usage"]
        if (
            request_id is None
            or not isinstance(acknowledgment, Mapping)
            or acknowledgment.get("request_id") != request_id
            or str(acknowledgment.get("provider", "")).casefold() != PROVIDER_SLUG
            or acknowledgment.get("resolved_model") != receipt.get("resolved_model")
            or acknowledgment.get("prompt_tokens") != usage["prompt_tokens"]
            or acknowledgment.get("completion_tokens") != usage["completion_tokens"]
        ):
            raise F2Error("F2 paid ledger failed-call request binding changed")
    elif request_id is not None:
        raise F2Error("F2 failed call with a request ID requires acknowledgment")

    failures = [row for row in events if row.get("event") == "failure"]
    if failures:
        failure = failures[0]
        metadata = failure.get("metadata")
        if (
            failure.get("stage") != error["stage"]
            or failure.get("reason") != error["message"]
            or not isinstance(metadata, Mapping)
            or metadata.get("error") != error["message"]
            or metadata.get("request_id") != request_id
            or (
                receipt.get("provider") is not None
                and str(metadata.get("provider", "")).casefold() != PROVIDER_SLUG
            )
            or metadata.get("resolved_model") != receipt.get("resolved_model")
        ):
            raise F2Error("F2 failed ledger receipt is not authoritative")

    terminal = next(row for row in events if row.get("event") in {"cancel", "settle"})
    if terminal.get("event") == "cancel":
        if (
            billed != Decimal("0")
            or not isinstance(terminal.get("reason"), str)
            or not terminal["reason"]
            or (not failures and terminal["reason"] != error["message"])
        ):
            raise F2Error("F2 cancelled failed call must have zero billed cost")
        return

    if not has_acknowledgment or request_id is None:
        raise F2Error("F2 failed-call settlement requires provider acknowledgment")
    settlement_metadata = terminal.get("metadata")
    usage = receipt["native_token_usage"]
    if not isinstance(settlement_metadata, Mapping) or (
        _ledger_decimal(terminal.get("actual_usd"), "F2 failed settled cost") != billed
        or _ledger_decimal(
            settlement_metadata.get("actual_usd"), "F2 failed settlement cost"
        )
        != billed
        or settlement_metadata.get("request_id") != request_id
        or settlement_metadata.get("requested_model") != MODEL_ID
        or settlement_metadata.get("resolved_model") != receipt.get("resolved_model")
        or str(settlement_metadata.get("provider", "")).casefold() != PROVIDER_SLUG
        or settlement_metadata.get("native_prompt_tokens") != usage["prompt_tokens"]
        or settlement_metadata.get("native_completion_tokens")
        != usage["completion_tokens"]
        or settlement_metadata.get("native_reasoning_tokens")
        != usage["reasoning_tokens"]
        or settlement_metadata.get("cost_source") != billing["cost_source"]
        or settlement_metadata.get("latency_ms") != receipt["latency_ms"]
        or settlement_metadata.get("retry_count") != receipt["retry_count"]
        or settlement_metadata.get("error") not in {None, error["message"]}
    ):
        raise F2Error(
            "F2 failed-call settlement differs from the imported provider receipt"
        )


def validate_f2_candidate_paid_ledger(root: Path, candidate: Mapping[str, Any]) -> None:
    """Bind imported F2 receipts to exact append-only paid-ledger events."""

    rows = _paid_ledger_rows(root)
    reservation_ids = [
        str(receipt["ledger_reservation_id"]) for receipt in candidate["results"]
    ]
    if len(set(reservation_ids)) != len(reservation_ids):
        raise F2Error("F2 paid ledger reservation IDs must be unique")
    request_ids = [
        str(receipt["request_id"])
        for receipt in candidate["results"]
        if receipt.get("request_id") is not None
    ]
    if len(set(request_ids)) != len(request_ids):
        raise F2Error("F2 paid ledger request IDs must be unique")
    for receipt in candidate["results"]:
        if receipt["status"] == "succeeded":
            _validate_successful_ledger_receipt(receipt, rows)
        else:
            _validate_failed_ledger_receipt(receipt, rows)


def record_f2_candidate_run(
    root: Path | None = None,
    *,
    approved_frontier_gate: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """Score and atomically record imported actions; never call a provider."""

    repository = (root or repository_root()).absolute()
    freeze = _load_saved_f2_freeze(repository, approved_frontier_gate)
    validate_f2_candidate(candidate, freeze)
    validate_f2_candidate_paid_ledger(repository, candidate)
    score = score_f2_candidate(freeze, candidate)
    _create_only_plan(
        repository,
        {
            repository / F2_CANDIDATE_PATH: _json_bytes(candidate),
            repository / F2_SCORE_PATH: _json_bytes(score),
        },
    )
    return score
