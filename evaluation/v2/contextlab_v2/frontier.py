"""Frozen entry contract and fail-closed controls for frontier experiments F1-F7."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any

from .baseline import repository_root
from .immutable_io import ImmutableIOError
from .provider import ALLOWED_REASONING_EFFORTS, FRONTIER_PROVIDER_SLUG, MODEL_ID
from .review_invocations import native_review_paths
from .tasking import sha256_json


FRONTIER_PROTOCOL_SCHEMA = "contextlab.frontier-protocol.v2"
FRONTIER_PROTOCOL_PATH = Path("evaluation/v2/frontier_protocol.json")
G4_GATE_PATH = Path("results/v2/gates/G4.json")
FRONTIER_STATUS_SCHEMA = "contextlab.frontier-status.v1"
FRONTIER_ENTRY_GATE_SCHEMA = "contextlab.frontier-entry-gate.v1"
FRONTIER_ENTRY_EVIDENCE_SCHEMA = "contextlab.frontier-entry-evidence.v1"
FRONTIER_ENTRY_EVIDENCE_PATH = Path(
    "results/v2/frontier/entry_evidence.attempt-07.json"
)
FRONTIER_ENTRY_GATE_PATH = Path("results/v2/frontier/entry_gate.attempt-07.json")
FRONTIER_ENTRY_REVIEWED_GATE_PATH = Path(
    "results/v2/frontier/entry_gate.attempt-07.reviewed.json"
)
FRONTIER_ENTRY_APPROVAL_PATH = Path(
    "results/v2/frontier/entry_approval.attempt-07.json"
)
FRONTIER_ENTRY_APPROVED_GATE_PATH = Path(
    "results/v2/frontier/entry_gate.attempt-07.approved.json"
)
FRONTIER_ENTRY_APPROVAL_SCHEMA = "contextlab.frontier-entry-approval.v1"
FRONTIER_ENTRY_AI_REVIEW_SCHEMA = "contextlab.frontier-entry-ai-review.v1"
FRONTIER_ENTRY_AI_INVOCATION_RECEIPT_SCHEMA = (
    "contextlab.frontier-entry-ai-invocation-receipt.v2"
)
FRONTIER_ENTRY_AI_REVIEW_PATHS = (
    Path("results/v2/reviews/frontier-entry/gpt-5.6-sol-high/attempt-10/review.json"),
    Path("results/v2/reviews/frontier-entry/gpt-5.6-terra-high/attempt-04/review.json"),
)
FRONTIER_ENTRY_AI_INVOCATION_RECEIPT_PATHS = (
    Path(
        "results/v2/reviews/frontier-entry/gpt-5.6-sol-high/attempt-10/"
        "invocation-receipt.json"
    ),
    Path(
        "results/v2/reviews/frontier-entry/gpt-5.6-terra-high/attempt-04/"
        "invocation-receipt.json"
    ),
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_UTC_SECOND = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_FORBIDDEN_EVIDENCE_PATH_TOKENS = {
    "sealed",
    "protected",
    "evaluation_only",
    "canonical_fact_ledger",
    "gold",
    "grade",
    "scoring",
}

_G3_FREEZE_PATH = Path("results/v2/memory/g3_public_freeze.json")
_G3_PUBLIC_RUN_PATH = Path("results/v2/memory/g3_public_generation_run.json")
_G3_LIFECYCLE_PATH = Path("results/v2/memory/g3_lifecycle_evidence.json")
_G3_FAILURE_PATH = Path("results/v2/memory/g3_failure_and_harm_report.json")
_MEMORY_PROTOCOL_PATH = Path("evaluation/v2/memory_protocol.json")
_CONTEXT_PACK_SCHEMA_PATH = Path("evaluation/v2/schemas/ContextPack.schema.json")
_SPAN_SCHEMA_PATH = Path("evaluation/v2/schemas/Span.schema.json")
_F3_SOURCE_MANIFEST_PATH = Path("results/v2/frontier/f3/public_source_manifest.json")
_F3_SOURCE_MANIFEST_SCHEMA = "contextlab.f3-public-source-manifest.v1"
_F4_CORPUS_PATH = Path("results/v2/frontier/f4/page_corpus_manifest.json")
_F4_REFERENCE_PATH = Path(
    "results/v2/frontier/f4/human_checked_reference_manifest.json"
)
_F5_CEILING_PATH = Path("results/v2/frontier/f5/retrieval_ceiling.json")
_G5_TECHNICAL_PATH = Path("results/v2/gates/G5.pending.json")
_G5_APPROVAL_PATH = Path("results/v2/gates/G5.approval.json")
_G5_GATE_PATH = Path("results/v2/gates/G5.json")
_F7_FAILURES_PATH = Path("results/v2/frontier/f7/human_confirmed_failures.json")

_EXPECTED_CONDITIONS = {
    "F1": "episodic outcome cards and full trace links from M4 work",
    "F2": "the deterministic memory suite has stable labels and failure cases",
    "F3": "ContextPack and trace schemas can record every page-in and page-out action",
    "F4": "a versioned page-level corpus and human-checked gold evidence exist",
    "F5": "fixed retrieval methods reached a measured ceiling on a named task family",
    "F6": "G5 passes",
    "F7": "enough repeated human-confirmed failures exist to justify a reusable rule",
}
_ENTRY_PARAMETERS = {
    "F1": {"minimum_episode_cards": 1, "require_all_m4_trace_links": True},
    "F2": {
        "expected_stable_labels": 5,
        "minimum_failed_configurations": 1,
        "minimum_harmful_configurations": 1,
        "require_lifecycle_replay": True,
    },
    "F3": {
        "required_operations": [
            "page_in",
            "page_out",
            "expand",
            "quote_recovery",
        ]
    },
    "F4": {
        "page_corpus_schema": "contextlab.f4-page-corpus-manifest.v1",
        "human_reference_schema": "contextlab.f4-human-reference-manifest.v1",
    },
    "F5": {
        "fixed_methods": ["R5", "R6"],
        "minimum_sample_count": 2,
        "allowed_metrics": ["required_source_coverage", "answer_quality"],
    },
    "F6": {"required_gate": "G5"},
    "F7": {"minimum_same_failure_recurrence": 2},
}
_EXPERIMENT_FIELDS = {
    "experiment_id",
    "title",
    "entry_condition",
    "additional_gate",
    "first_experiment",
    "roadmap_controls",
    "measures",
}
_ROADMAP_CONTROL_FIELDS = {
    "primary_question",
    "primary_metric",
    "target_task_family",
    "fixed_execution_controls",
    "changed_variable",
    "expected_artifact",
    "stop_condition",
    "recording_contract",
    "stochastic_trial_plan",
    "temperature_zero_provider_repeat_sample_plan",
}
_FIXED_EXECUTION_CONTROL_FIELDS = {"model", "prompt", "corpus", "tokens", "output"}
_RECORDING_CONTRACT_FIELDS = {"cost_usd", "latency_ms", "evidence", "outcome"}
_STOCHASTIC_TRIAL_PLAN_FIELDS = {
    "stochastic",
    "minimum_complete_trials",
    "unit",
}
_TEMPERATURE_ZERO_REPEAT_PLAN_FIELDS = {
    "temperature",
    "minimum_provider_repeat_samples",
    "unit",
}
_NOT_APPLICABLE_PREFIX = "not_applicable: entry failure prevents"
_ROADMAP_CONTROL_SHA256 = {
    "F1": "bb7914b1daa19490dd91d13a5dcf8865d1687b305421aeef417f812c5c289739",
    "F2": "4def4a78c423d671d992b3224bf413ce0bd6301f8a76459478e5738a30ba6cb1",
    "F3": "3d048ec319f7fa28620f0577879ad41702a3efbc0cce1fd00d7481f10688135a",
    "F4": "dd77d2d060604aef893d0d26594878e77cc75c2e2c46abca9d3895ed4f464f6d",
    "F5": "6cc7e269355273034bf83523ebe5ff693d47db3251b976f4cd0be79057080a07",
    "F6": "c89aef70298cb2cdf21be153cbc25862876ee9fa93372897eb4cd7feaccd7a62",
    "F7": "b846168576ae783358a5cb99fc38692b68ba6dbe4585d6ef67871b902b4a23fe",
}
_CHECK_IDS = {
    "F1": "m4_episode_cards_and_trace_links",
    "F2": "stable_memory_labels_and_failures",
    "F3": "lossless_paging_contract",
    "F4": "versioned_page_corpus_and_human_reference",
    "F5": "measured_fixed_retrieval_ceiling",
    "F6": "g5_passed",
    "F7": "repeated_human_confirmed_failures",
}
_ENTRY_REVIEWERS = {
    "gpt-5.6-sol-high": ("gpt-5.6-sol", "high", "codex-subagent"),
    "gpt-5.6-terra-high": ("gpt-5.6-terra", "high", "codex-subagent"),
}
_ENTRY_REVIEW_PATHS = dict(
    zip(_ENTRY_REVIEWERS, FRONTIER_ENTRY_AI_REVIEW_PATHS, strict=True)
)
_ENTRY_RECEIPT_PATHS = dict(
    zip(
        _ENTRY_REVIEWERS,
        FRONTIER_ENTRY_AI_INVOCATION_RECEIPT_PATHS,
        strict=True,
    )
)
_INVOCATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{7,255}\Z")


class FrontierError(ValueError):
    """The frontier protocol or gate evidence is unsafe, missing, or altered."""


def _nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FrontierError(f"{label} must be a non-empty string")
    return value


def _not_applicable(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.startswith(_NOT_APPLICABLE_PREFIX):
        raise FrontierError(
            f"{label} must be explicit not_applicable after entry failure"
        )


def _validate_roadmap_controls(experiment_id: str, value: Any) -> None:
    """Reject any added, missing, or weakened pre-entry roadmap control."""

    if not isinstance(value, Mapping) or set(value) != _ROADMAP_CONTROL_FIELDS:
        raise FrontierError(f"{experiment_id} roadmap-control fields changed")
    for field in (
        "primary_question",
        "primary_metric",
        "changed_variable",
        "stop_condition",
    ):
        _nonempty(value.get(field), f"{experiment_id} {field}")

    execution = value.get("fixed_execution_controls")
    if (
        not isinstance(execution, Mapping)
        or set(execution) != _FIXED_EXECUTION_CONTROL_FIELDS
    ):
        raise FrontierError(f"{experiment_id} fixed execution controls changed")
    recording = value.get("recording_contract")
    if (
        not isinstance(recording, Mapping)
        or set(recording) != _RECORDING_CONTRACT_FIELDS
    ):
        raise FrontierError(f"{experiment_id} recording contract changed")

    if experiment_id in {"F1", "F2", "F3", "F5"}:
        for field in ("target_task_family", "expected_artifact"):
            _nonempty(value.get(field), f"{experiment_id} {field}")
        for field in _FIXED_EXECUTION_CONTROL_FIELDS:
            _nonempty(execution.get(field), f"{experiment_id} fixed {field} control")
        for field in _RECORDING_CONTRACT_FIELDS:
            _nonempty(recording.get(field), f"{experiment_id} {field} recording")
        trials = value.get("stochastic_trial_plan")
        if (
            not isinstance(trials, Mapping)
            or set(trials) != _STOCHASTIC_TRIAL_PLAN_FIELDS
            or trials.get("stochastic") is not True
            or _integer(
                trials.get("minimum_complete_trials"), f"{experiment_id} trial count"
            )
            < (2 if experiment_id == "F5" else 5)
        ):
            raise FrontierError(f"{experiment_id} stochastic trial plan changed")
        _nonempty(trials.get("unit"), f"{experiment_id} stochastic trial unit")
        repeats = value.get("temperature_zero_provider_repeat_sample_plan")
        if (
            not isinstance(repeats, Mapping)
            or set(repeats) != _TEMPERATURE_ZERO_REPEAT_PLAN_FIELDS
            or repeats.get("temperature") != 0
            or _integer(
                repeats.get("minimum_provider_repeat_samples"),
                f"{experiment_id} provider repeat-sample count",
            )
            < (2 if experiment_id == "F5" else 5)
        ):
            raise FrontierError(f"{experiment_id} temperature-zero repeat plan changed")
        _nonempty(repeats.get("unit"), f"{experiment_id} provider repeat-sample unit")
    else:
        _not_applicable(
            value.get("target_task_family"), f"{experiment_id} target task family"
        )
        _not_applicable(
            value.get("expected_artifact"), f"{experiment_id} expected artifact"
        )
        for field in _FIXED_EXECUTION_CONTROL_FIELDS:
            _not_applicable(
                execution.get(field), f"{experiment_id} fixed {field} control"
            )
        for field in ("cost_usd", "latency_ms"):
            _not_applicable(recording.get(field), f"{experiment_id} {field} recording")
        for field in ("evidence", "outcome"):
            _nonempty(recording.get(field), f"{experiment_id} {field} recording")
        _not_applicable(
            value.get("stochastic_trial_plan"), f"{experiment_id} stochastic trial plan"
        )
        _not_applicable(
            value.get("temperature_zero_provider_repeat_sample_plan"),
            f"{experiment_id} temperature-zero repeat plan",
        )

    if sha256_json(value) != _ROADMAP_CONTROL_SHA256.get(experiment_id):
        raise FrontierError(f"{experiment_id} frozen roadmap controls changed")


def validate_frontier_protocol(value: Mapping[str, Any]) -> None:
    """Validate the exact roadmap-approved F1-F7 protocol."""

    expected_fields = {
        "schema_version",
        "program_barrier",
        "requested_model",
        "provider_route_overrides",
        "frontier_entry_reviewers",
        "reasoning_efforts",
        "entry_failure_is_result",
        "entry_parameters",
        "experiments",
    }
    if set(value) != expected_fields:
        raise FrontierError("frontier protocol fields changed")
    if value.get("schema_version") != FRONTIER_PROTOCOL_SCHEMA:
        raise FrontierError("unsupported frontier protocol schema")
    if value.get("program_barrier") != "G4":
        raise FrontierError("frontier experiments must remain behind G4")
    if value.get("requested_model") != MODEL_ID:
        raise FrontierError("frontier generator model changed")
    if value.get("provider_route_overrides") != {
        "F3": FRONTIER_PROVIDER_SLUG,
        "F5": FRONTIER_PROVIDER_SLUG,
    }:
        raise FrontierError("frontier provider route overrides changed")
    if value.get("frontier_entry_reviewers") != list(_ENTRY_REVIEWERS):
        raise FrontierError("frontier entry reviewers changed")
    if value.get("reasoning_efforts") != list(ALLOWED_REASONING_EFFORTS):
        raise FrontierError("frontier reasoning efforts must be exactly low and high")
    if value.get("entry_failure_is_result") is not True:
        raise FrontierError("failed frontier entry gates must remain results")
    if value.get("entry_parameters") != _ENTRY_PARAMETERS:
        raise FrontierError("frontier entry parameters changed")

    experiments = value.get("experiments")
    if not isinstance(experiments, Sequence) or isinstance(experiments, (str, bytes)):
        raise FrontierError("frontier experiments must be an ordered list")
    if len(experiments) != 7:
        raise FrontierError("frontier protocol must contain F1-F7 exactly once")
    observed_ids: list[str] = []
    for index, raw in enumerate(experiments):
        if not isinstance(raw, Mapping) or set(raw) != _EXPERIMENT_FIELDS:
            raise FrontierError(f"frontier experiment {index} fields changed")
        experiment_id = _nonempty(raw.get("experiment_id"), "experiment_id")
        observed_ids.append(experiment_id)
        if raw.get("entry_condition") != _EXPECTED_CONDITIONS.get(experiment_id):
            raise FrontierError(f"{experiment_id} entry condition changed")
        expected_gate = "G5" if experiment_id == "F6" else None
        if raw.get("additional_gate") != expected_gate:
            raise FrontierError(f"{experiment_id} additional gate changed")
        _nonempty(raw.get("title"), f"{experiment_id} title")
        _nonempty(raw.get("first_experiment"), f"{experiment_id} first experiment")
        _validate_roadmap_controls(experiment_id, raw.get("roadmap_controls"))
        measures = raw.get("measures")
        if (
            not isinstance(measures, list)
            or not measures
            or any(not isinstance(item, str) or not item for item in measures)
            or len(set(measures)) != len(measures)
        ):
            raise FrontierError(f"{experiment_id} measures are invalid")
    if observed_ids != list(_EXPECTED_CONDITIONS):
        raise FrontierError("frontier experiment order or identity changed")


def load_frontier_protocol(root: Path | None = None) -> dict[str, Any]:
    """Load the canonical repository protocol and reject any altered shape."""

    repository = (root or repository_root()).resolve()
    path = repository / FRONTIER_PROTOCOL_PATH
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrontierError("cannot read frontier protocol") from exc
    if not isinstance(value, dict):
        raise FrontierError("frontier protocol must be an object")
    validate_frontier_protocol(value)
    return value


def _valid_artifact_hash(value: Mapping[str, Any]) -> bool:
    artifact = value.get("artifact_sha256")
    if not isinstance(artifact, str):
        return False
    body = {key: item for key, item in value.items() if key != "artifact_sha256"}
    return artifact == sha256_json(body)


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise FrontierError(f"{label} must be a lowercase SHA-256")
    return value


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FrontierError(f"{label} must be a non-negative integer")
    return value


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise FrontierError(f"{label} must be boolean")
    return value


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _public_path(root: Path, relative: Path, label: str) -> Path:
    root = root.resolve()
    if relative.is_absolute() or ".." in relative.parts:
        raise FrontierError(f"{label} path escapes the repository")
    lowered = relative.as_posix().casefold()
    if any(token in lowered for token in _FORBIDDEN_EVIDENCE_PATH_TOKENS):
        raise FrontierError(f"{label} is not public evidence")
    path = root / relative
    try:
        path.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise FrontierError(f"{label} path escapes the repository") from exc
    if path.is_symlink():
        raise FrontierError(f"{label} cannot be a symlink")
    return path


def _read_public_json(root: Path, relative: Path, label: str) -> dict[str, Any]:
    path = _public_path(root, relative, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrontierError(f"cannot read {label}") from exc
    if not isinstance(value, dict):
        raise FrontierError(f"{label} must be an object")
    return value


def _optional_public_json(
    root: Path, relative: Path, label: str
) -> dict[str, Any] | None:
    path = _public_path(root, relative, label)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _source_reference(root: Path, relative: Path) -> dict[str, str]:
    path = _public_path(root, relative, relative.as_posix())
    if not path.is_file():
        raise FrontierError(f"missing public source artifact: {relative}")
    return {"path": relative.as_posix(), "sha256": _sha256_file(path)}


def _valid_optional_artifact(
    value: Mapping[str, Any] | None, *, schema: str, fields: set[str]
) -> bool:
    return bool(
        isinstance(value, Mapping)
        and set(value) == fields | {"schema_version", "artifact_sha256"}
        and value.get("schema_version") == schema
        and _valid_artifact_hash(value)
    )


def _collect_f1(root: Path) -> tuple[dict[str, Any], list[Path]]:
    from .g3_freeze import validate_g3_freeze
    from .memory_experiments import validate_memory_trace

    freeze = _read_public_json(root, _G3_FREEZE_PATH, "G3 public freeze")
    try:
        validate_g3_freeze(freeze)
    except Exception as exc:
        raise FrontierError(f"G3 public freeze is invalid: {exc}") from exc
    episodes = freeze.get("manifest", {}).get("m4_episode_seed", [])
    if not isinstance(episodes, list):
        episodes = []

    public_run = _read_public_json(root, _G3_PUBLIC_RUN_PATH, "G3 public run")
    if (
        public_run.get("schema_version") != "contextlab.g3-public-generation-run.v1"
        or not _valid_artifact_hash(public_run)
        or not isinstance(public_run.get("cells"), list)
    ):
        raise FrontierError("G3 public run is invalid")
    m4_cells = [
        cell
        for cell in public_run["cells"]
        if isinstance(cell, Mapping) and cell.get("policy") == "M4"
    ]
    valid_count = 0
    for index, cell in enumerate(m4_cells):
        raw_path = cell.get("receipt_path")
        if not isinstance(raw_path, str):
            raise FrontierError(f"M4 cell {index} has no public receipt")
        relative = Path(raw_path)
        expected_prefix = Path("results/v2/memory/receipts/g3-public-v1/M4")
        try:
            relative.relative_to(expected_prefix)
        except ValueError as exc:
            raise FrontierError(f"M4 cell {index} receipt path changed") from exc
        receipt = _read_public_json(root, relative, f"M4 receipt {index}")
        if receipt.get("result_sha256") != cell.get("receipt_sha256"):
            raise FrontierError(f"M4 cell {index} receipt commitment changed")
        trace = receipt.get("trace")
        try:
            validate_memory_trace(trace)
        except Exception as exc:
            raise FrontierError(f"M4 cell {index} trace is invalid: {exc}") from exc
        valid_count += 1
    return (
        {
            "episode_card_count": len(episodes),
            "m4_trace_count": valid_count,
            "expected_m4_trace_count": len(m4_cells),
            "all_traces_valid": valid_count == len(m4_cells),
        },
        [_G3_FREEZE_PATH, _G3_PUBLIC_RUN_PATH],
    )


def _collect_f2(root: Path) -> tuple[dict[str, Any], list[Path]]:
    from .g3_freeze import load_memory_protocol
    from .g3_lifecycle import validate_g3_lifecycle_evidence

    protocol = load_memory_protocol(root)
    lifecycle = _read_public_json(root, _G3_LIFECYCLE_PATH, "G3 lifecycle evidence")
    try:
        validate_g3_lifecycle_evidence(lifecycle)
    except Exception as exc:
        raise FrontierError(f"G3 lifecycle evidence is invalid: {exc}") from exc
    failure = _read_public_json(root, _G3_FAILURE_PATH, "G3 failure report")
    if (
        failure.get("schema_version") != "contextlab.g3-failure-and-harm-report.v1"
        or not _valid_artifact_hash(failure)
        or not isinstance(failure.get("failed_configurations"), list)
        or not isinstance(failure.get("harmful_configurations"), list)
    ):
        raise FrontierError("G3 failure report is invalid")
    policies = protocol["surface"]["memory_policies"]
    return (
        {
            "stable_label_count": len(policies),
            "failed_configuration_count": len(failure["failed_configurations"]),
            "harmful_configuration_count": len(failure["harmful_configurations"]),
            "lifecycle_replay_passed": lifecycle.get("all_passed") is True,
        },
        [_MEMORY_PROTOCOL_PATH, _G3_LIFECYCLE_PATH, _G3_FAILURE_PATH],
    )


def _schema_action_support(value: Mapping[str, Any]) -> tuple[bool, list[str]]:
    try:
        action = value["properties"]["context_actions"]["items"]
        operation = action["properties"]["operation"]["enum"]
        required = action["required"]
    except (KeyError, TypeError):
        return False, []
    expected_fields = {
        "schema_version",
        "action_id",
        "sequence",
        "operation",
        "pointer",
        "content_sha256",
        "token_delta",
    }
    expected_operations = _ENTRY_PARAMETERS["F3"]["required_operations"]
    return (
        isinstance(operation, list)
        and operation == expected_operations
        and set(required) == expected_fields
        and action.get("additionalProperties") is False,
        list(operation) if isinstance(operation, list) else [],
    )


def _f3_source_manifest_commitment(root: Path) -> dict[str, str] | None:
    from .frontier_f3 import validate_f3_public_source_manifest

    manifest = _optional_public_json(
        root, _F3_SOURCE_MANIFEST_PATH, "F3 public source manifest"
    )
    if not isinstance(manifest, Mapping):
        return None
    try:
        validate_f3_public_source_manifest(manifest, root=root.resolve())
    except Exception:
        return None
    manifest_path = _public_path(
        root, _F3_SOURCE_MANIFEST_PATH, "F3 public source manifest"
    )
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return None
    return {
        "path": _F3_SOURCE_MANIFEST_PATH.as_posix(),
        "sha256": _sha256_file(manifest_path),
    }


def _collect_f3(root: Path) -> tuple[dict[str, Any], list[Path]]:
    context = _read_public_json(root, _CONTEXT_PACK_SCHEMA_PATH, "ContextPack schema")
    span = _read_public_json(root, _SPAN_SCHEMA_PATH, "Span schema")
    context_supported, operations = _schema_action_support(context)
    span_supported, span_operations = _schema_action_support(span)
    commitment = _f3_source_manifest_commitment(root)
    return (
        {
            "context_pack_actions_supported": context_supported,
            "trace_actions_supported": span_supported,
            "operations": operations if operations == span_operations else [],
            "source_manifest_valid": commitment is not None,
            "approved_source_commitment": commitment,
        },
        [
            _CONTEXT_PACK_SCHEMA_PATH,
            _SPAN_SCHEMA_PATH,
            *([_F3_SOURCE_MANIFEST_PATH] if commitment is not None else []),
        ],
    )


def _collect_f4(root: Path) -> tuple[dict[str, Any], list[Path]]:
    parameters = _ENTRY_PARAMETERS["F4"]
    corpus = _optional_public_json(root, _F4_CORPUS_PATH, "F4 page corpus")
    reference = _optional_public_json(root, _F4_REFERENCE_PATH, "F4 human reference")
    corpus_valid = (
        _valid_optional_artifact(
            corpus,
            schema=parameters["page_corpus_schema"],
            fields={"corpus_id", "version", "page_count", "page_sha256s"},
        )
        and isinstance(corpus.get("page_count"), int)
        and corpus["page_count"] > 0
    )
    reference_valid = _valid_optional_artifact(
        reference,
        schema=parameters["human_reference_schema"],
        fields={
            "page_corpus_artifact_sha256",
            "reference_count",
            "reviewer",
            "reviewed_at",
        },
    )
    if reference_valid:
        reference_valid = bool(
            corpus_valid
            and reference.get("page_corpus_artifact_sha256")
            == corpus.get("artifact_sha256")
            and reference.get("reviewer") == "Kevin Araujo"
            and isinstance(reference.get("reviewed_at"), str)
            and _UTC_SECOND.fullmatch(reference["reviewed_at"]) is not None
            and isinstance(reference.get("reference_count"), int)
            and reference["reference_count"] > 0
        )
    paths = []
    if corpus_valid:
        paths.append(_F4_CORPUS_PATH)
    if reference_valid:
        paths.append(_F4_REFERENCE_PATH)
    return (
        {
            "page_corpus_manifest_valid": bool(corpus_valid),
            "human_checked_reference_manifest_valid": bool(reference_valid),
        },
        paths,
    )


def _collect_f5(root: Path) -> tuple[dict[str, Any], list[Path]]:
    parameters = _ENTRY_PARAMETERS["F5"]
    ceiling = _optional_public_json(root, _F5_CEILING_PATH, "F5 retrieval ceiling")
    valid = _valid_optional_artifact(
        ceiling,
        schema="contextlab.f5-retrieval-ceiling.v1",
        fields={
            "task_family",
            "metric",
            "fixed_methods",
            "sample_count",
            "measured_ceiling",
            "source_report_path",
            "source_report_sha256",
        },
    )
    if valid:
        source_raw = ceiling.get("source_report_path")
        source = Path(source_raw) if isinstance(source_raw, str) else Path("/")
        try:
            source_path = _public_path(root, source, "F5 source report")
        except FrontierError:
            valid = False
        else:
            valid = bool(
                source_path.is_file()
                and ceiling.get("source_report_sha256") == _sha256_file(source_path)
                and isinstance(ceiling.get("task_family"), str)
                and bool(ceiling["task_family"])
                and ceiling.get("metric") in parameters["allowed_metrics"]
                and ceiling.get("fixed_methods") == parameters["fixed_methods"]
                and isinstance(ceiling.get("sample_count"), int)
                and ceiling["sample_count"] >= parameters["minimum_sample_count"]
                and ceiling.get("measured_ceiling") is True
            )
    paths = [_F5_CEILING_PATH] if valid else []
    return (
        {
            "named_task_family": ceiling.get("task_family") if valid else None,
            "measured_ceiling": bool(valid),
        },
        paths,
    )


def _collect_f6(root: Path) -> tuple[dict[str, Any], list[Path]]:
    from .g5_gate import load_approved_g5_gate

    technical_exists = (root / _G5_TECHNICAL_PATH).is_file()
    approval_exists = (root / _G5_APPROVAL_PATH).is_file()
    final_exists = (root / _G5_GATE_PATH).is_file()
    if not any((technical_exists, approval_exists, final_exists)):
        return {"g5_status": "missing"}, []
    try:
        load_approved_g5_gate(root)
    except Exception:
        status = (
            "pending"
            if technical_exists and not approval_exists and not final_exists
            else "failed"
        )
        return {"g5_status": status}, []
    return {"g5_status": "passed"}, [
        _G5_TECHNICAL_PATH,
        _G5_APPROVAL_PATH,
        _G5_GATE_PATH,
    ]


def _collect_f7(root: Path) -> tuple[dict[str, Any], list[Path]]:
    threshold = _ENTRY_PARAMETERS["F7"]["minimum_same_failure_recurrence"]
    failures = _optional_public_json(root, _F7_FAILURES_PATH, "F7 confirmed failures")
    valid = _valid_optional_artifact(
        failures,
        schema="contextlab.f7-human-confirmed-failures.v1",
        fields={"minimum_required_recurrence", "records"},
    )
    maximum = 0
    if valid:
        records = failures.get("records")
        valid = bool(
            failures.get("minimum_required_recurrence") == threshold
            and isinstance(records, list)
        )
        counts: Counter[tuple[str, str]] = Counter()
        seen: set[str] = set()
        for index, record in enumerate(records or []):
            if not isinstance(record, Mapping) or set(record) != {
                "occurrence_id",
                "failure_class",
                "task_family",
                "source_artifact_path",
                "source_artifact_sha256",
                "reviewer",
                "confirmed_at",
            }:
                valid = False
                break
            occurrence = record.get("occurrence_id")
            source_raw = record.get("source_artifact_path")
            if (
                not isinstance(occurrence, str)
                or not occurrence
                or occurrence in seen
                or record.get("reviewer") != "Kevin Araujo"
                or not isinstance(record.get("confirmed_at"), str)
                or _UTC_SECOND.fullmatch(record["confirmed_at"]) is None
                or not isinstance(record.get("failure_class"), str)
                or not record["failure_class"]
                or not isinstance(record.get("task_family"), str)
                or not record["task_family"]
                or not isinstance(source_raw, str)
            ):
                valid = False
                break
            seen.add(occurrence)
            try:
                source = _public_path(root, Path(source_raw), f"F7 record {index}")
            except FrontierError:
                valid = False
                break
            if not source.is_file() or record.get(
                "source_artifact_sha256"
            ) != _sha256_file(source):
                valid = False
                break
            counts[(record["task_family"], record["failure_class"])] += 1
        if valid and counts:
            maximum = max(counts.values())
    return (
        {
            "maximum_same_failure_recurrence": maximum if valid else 0,
            "minimum_required_recurrence": threshold,
        },
        [_F7_FAILURES_PATH] if valid else [],
    )


def collect_frontier_entry_evidence(
    root: Path | None = None, *, g4_gate_sha256: str
) -> dict[str, Any]:
    """Collect content-free public facts without entering or running any experiment."""

    repository = (root or repository_root()).resolve()
    _sha(g4_gate_sha256, "G4 gate hash")
    protocol = load_frontier_protocol(repository)
    collectors = (
        _collect_f1,
        _collect_f2,
        _collect_f3,
        _collect_f4,
        _collect_f5,
        _collect_f6,
        _collect_f7,
    )
    observations = []
    source_paths: list[Path] = [FRONTIER_PROTOCOL_PATH]
    for experiment_id, collector in zip(_EXPECTED_CONDITIONS, collectors, strict=True):
        facts, paths = collector(repository)
        observations.append(
            {
                "experiment_id": experiment_id,
                "check_id": _CHECK_IDS[experiment_id],
                "facts": facts,
            }
        )
        source_paths.extend(paths)
    unique_paths = sorted(set(source_paths), key=lambda path: path.as_posix())
    evidence: dict[str, Any] = {
        "schema_version": FRONTIER_ENTRY_EVIDENCE_SCHEMA,
        "frontier_protocol_sha256": sha256_json(protocol),
        "g4_gate_artifact_sha256": g4_gate_sha256,
        "source_artifacts": [
            _source_reference(repository, path) for path in unique_paths
        ],
        "observations": observations,
    }
    evidence["artifact_sha256"] = sha256_json(evidence)
    validate_frontier_entry_evidence(evidence)
    return evidence


def _observation_passes(experiment_id: str, facts: Mapping[str, Any]) -> bool:
    if experiment_id == "F1":
        if set(facts) != {
            "episode_card_count",
            "m4_trace_count",
            "expected_m4_trace_count",
            "all_traces_valid",
        }:
            raise FrontierError("F1 observation facts changed")
        cards = _integer(facts["episode_card_count"], "F1 episode-card count")
        traces = _integer(facts["m4_trace_count"], "F1 M4 trace count")
        expected = _integer(
            facts["expected_m4_trace_count"], "F1 expected M4 trace count"
        )
        valid = _boolean(facts["all_traces_valid"], "F1 trace validity")
        parameters = _ENTRY_PARAMETERS["F1"]
        return (
            cards >= parameters["minimum_episode_cards"]
            and expected > 0
            and traces == expected
            and valid is parameters["require_all_m4_trace_links"]
        )
    if experiment_id == "F2":
        if set(facts) != {
            "stable_label_count",
            "failed_configuration_count",
            "harmful_configuration_count",
            "lifecycle_replay_passed",
        }:
            raise FrontierError("F2 observation facts changed")
        labels = _integer(facts["stable_label_count"], "F2 stable-label count")
        failures = _integer(
            facts["failed_configuration_count"], "F2 failed-configuration count"
        )
        harmful = _integer(
            facts["harmful_configuration_count"], "F2 harmful-configuration count"
        )
        replay = _boolean(facts["lifecycle_replay_passed"], "F2 lifecycle replay")
        parameters = _ENTRY_PARAMETERS["F2"]
        return (
            labels == parameters["expected_stable_labels"]
            and failures >= parameters["minimum_failed_configurations"]
            and harmful >= parameters["minimum_harmful_configurations"]
            and replay is parameters["require_lifecycle_replay"]
        )
    if experiment_id == "F3":
        if set(facts) != {
            "context_pack_actions_supported",
            "trace_actions_supported",
            "operations",
            "source_manifest_valid",
            "approved_source_commitment",
        }:
            raise FrontierError("F3 observation facts changed")
        context = _boolean(
            facts["context_pack_actions_supported"], "F3 ContextPack support"
        )
        trace = _boolean(facts["trace_actions_supported"], "F3 trace support")
        operations = facts["operations"]
        expected = _ENTRY_PARAMETERS["F3"]["required_operations"]
        if not isinstance(operations, list) or any(
            not isinstance(item, str) for item in operations
        ):
            raise FrontierError("F3 operations must be a string list")
        manifest_valid = _boolean(
            facts["source_manifest_valid"], "F3 public source manifest"
        )
        commitment = facts["approved_source_commitment"]
        if commitment is not None:
            if not isinstance(commitment, Mapping) or set(commitment) != {
                "path",
                "sha256",
            }:
                raise FrontierError("F3 approved source commitment fields changed")
            if commitment.get("path") != _F3_SOURCE_MANIFEST_PATH.as_posix():
                raise FrontierError("F3 approved source manifest path changed")
            _sha(commitment.get("sha256"), "F3 approved source manifest file hash")
        if manifest_valid != (commitment is not None):
            raise FrontierError(
                "F3 source-manifest status disagrees with its commitment"
            )
        return context and trace and operations == expected and manifest_valid
    if experiment_id == "F4":
        if set(facts) != {
            "page_corpus_manifest_valid",
            "human_checked_reference_manifest_valid",
        }:
            raise FrontierError("F4 observation facts changed")
        corpus = _boolean(
            facts["page_corpus_manifest_valid"], "F4 page-corpus manifest"
        )
        reference = _boolean(
            facts["human_checked_reference_manifest_valid"],
            "F4 human reference manifest",
        )
        return corpus and reference
    if experiment_id == "F5":
        if set(facts) != {"named_task_family", "measured_ceiling"}:
            raise FrontierError("F5 observation facts changed")
        family = facts["named_task_family"]
        if family is not None and (not isinstance(family, str) or not family):
            raise FrontierError("F5 named task family is invalid")
        measured = _boolean(facts["measured_ceiling"], "F5 measured ceiling")
        return measured and family is not None
    if experiment_id == "F6":
        if set(facts) != {"g5_status"} or facts["g5_status"] not in {
            "missing",
            "pending",
            "failed",
            "passed",
        }:
            raise FrontierError("F6 G5 status is invalid")
        return facts["g5_status"] == "passed"
    if experiment_id == "F7":
        if set(facts) != {
            "maximum_same_failure_recurrence",
            "minimum_required_recurrence",
        }:
            raise FrontierError("F7 observation facts changed")
        maximum = _integer(
            facts["maximum_same_failure_recurrence"], "F7 repeated failures"
        )
        minimum = _integer(
            facts["minimum_required_recurrence"], "F7 recurrence threshold"
        )
        if minimum != _ENTRY_PARAMETERS["F7"]["minimum_same_failure_recurrence"]:
            raise FrontierError("F7 recurrence threshold changed")
        return maximum >= minimum
    raise FrontierError(f"unknown frontier experiment: {experiment_id}")


def validate_frontier_entry_evidence(value: Mapping[str, Any]) -> None:
    """Validate the content-free public facts used to decide F1-F7 entry."""

    if set(value) != {
        "schema_version",
        "frontier_protocol_sha256",
        "g4_gate_artifact_sha256",
        "source_artifacts",
        "observations",
        "artifact_sha256",
    }:
        raise FrontierError("frontier entry evidence fields changed")
    if value.get("schema_version") != FRONTIER_ENTRY_EVIDENCE_SCHEMA:
        raise FrontierError("unsupported frontier entry evidence schema")
    _sha(value.get("frontier_protocol_sha256"), "frontier protocol hash")
    _sha(value.get("g4_gate_artifact_sha256"), "G4 gate hash")
    if not _valid_artifact_hash(value):
        raise FrontierError("frontier entry evidence hash mismatch")
    _validate_public_evidence(
        value.get("source_artifacts"), "frontier entry source artifacts"
    )
    observations = value.get("observations")
    if not isinstance(observations, list) or len(observations) != 7:
        raise FrontierError("frontier entry evidence must contain F1-F7")
    identifiers: list[str] = []
    for index, observation in enumerate(observations):
        if not isinstance(observation, Mapping) or set(observation) != {
            "experiment_id",
            "check_id",
            "facts",
        }:
            raise FrontierError(f"frontier observation {index} fields changed")
        experiment_id = _nonempty(
            observation.get("experiment_id"), f"frontier observation {index}"
        )
        identifiers.append(experiment_id)
        if observation.get("check_id") != _CHECK_IDS.get(experiment_id):
            raise FrontierError(f"{experiment_id} check identity changed")
        facts = observation.get("facts")
        if not isinstance(facts, Mapping):
            raise FrontierError(f"{experiment_id} facts must be an object")
        _observation_passes(experiment_id, facts)
    if identifiers != list(_EXPECTED_CONDITIONS):
        raise FrontierError("frontier observation order changed")
    f3_facts = observations[2]["facts"]
    commitment = f3_facts.get("approved_source_commitment")
    if commitment is not None and dict(commitment) not in value["source_artifacts"]:
        raise FrontierError(
            "F3 approved source manifest is missing from source artifacts"
        )


def _frontier_review_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "reviewer_id",
        "model_id",
        "reasoning_effort",
        "pending_gate_artifact_sha256",
        "frontier_entry_evidence_sha256",
        "frontier_protocol_sha256",
        "g4_gate_artifact_sha256",
        "decision",
        "p0_findings",
        "p1_findings",
        "completed_at",
    )
    return {field: value.get(field) for field in fields}


def validate_frontier_entry_ai_invocation_receipt(value: Mapping[str, Any]) -> None:
    expected = {
        "schema_version",
        "reviewer_id",
        "invocation_source",
        "invocation_id",
        "requested_model",
        "resolved_model",
        "reasoning_effort",
        "pending_gate_artifact_sha256",
        "frontier_entry_evidence_sha256",
        "frontier_protocol_sha256",
        "g4_gate_artifact_sha256",
        "review_payload_sha256",
        "native_invocation_evidence_path",
        "native_invocation_evidence_sha256",
        "native_output_sha256",
        "completed_at",
        "artifact_sha256",
    }
    if set(value) != expected:
        raise FrontierError("frontier AI invocation receipt fields changed")
    if value.get(
        "schema_version"
    ) != FRONTIER_ENTRY_AI_INVOCATION_RECEIPT_SCHEMA or not _valid_artifact_hash(value):
        raise FrontierError("frontier AI invocation receipt hash is invalid")
    reviewer_id = value.get("reviewer_id")
    if not isinstance(reviewer_id, str) or reviewer_id not in _ENTRY_REVIEWERS:
        raise FrontierError("unknown frontier AI invocation reviewer")
    model, effort, source = _ENTRY_REVIEWERS[reviewer_id]
    if (
        value.get("invocation_source") != source
        or value.get("requested_model") != model
        or value.get("resolved_model") != model
        or value.get("reasoning_effort") != effort
    ):
        raise FrontierError(
            "frontier AI invocation did not resolve the exact model and effort"
        )
    invocation_id = value.get("invocation_id")
    if (
        not isinstance(invocation_id, str)
        or _INVOCATION_ID.fullmatch(invocation_id) is None
    ):
        raise FrontierError("frontier AI invocation ID is missing or invalid")
    for field, label in (
        ("pending_gate_artifact_sha256", "reviewed pending frontier gate hash"),
        ("frontier_entry_evidence_sha256", "reviewed frontier evidence hash"),
        ("frontier_protocol_sha256", "reviewed frontier protocol hash"),
        ("g4_gate_artifact_sha256", "reviewed G4 gate hash"),
        ("review_payload_sha256", "frontier AI review-payload hash"),
        (
            "native_invocation_evidence_sha256",
            "frontier native invocation evidence hash",
        ),
        ("native_output_sha256", "frontier native invocation output hash"),
    ):
        _sha(value.get(field), label)
    if (
        value.get("native_invocation_evidence_path")
        != native_review_paths(_ENTRY_RECEIPT_PATHS[reviewer_id])[0].as_posix()
    ):
        raise FrontierError("frontier native invocation evidence path changed")
    completed_at = value.get("completed_at")
    if not isinstance(completed_at, str) or _UTC_SECOND.fullmatch(completed_at) is None:
        raise FrontierError("frontier AI invocation timestamp is invalid")


def build_frontier_entry_ai_invocation_receipt(
    *,
    reviewer_id: str,
    invocation_id: str,
    pending_gate: Mapping[str, Any],
    evidence: Mapping[str, Any],
    decision: str,
    p0_findings: Sequence[str],
    p1_findings: Sequence[str],
    completed_at: str,
    native_invocation_evidence_sha256: str,
    native_output_sha256: str,
) -> dict[str, Any]:
    identity = _ENTRY_REVIEWERS.get(reviewer_id)
    if identity is None:
        raise FrontierError("unknown frontier AI reviewer")
    validate_frontier_entry_evidence(evidence)
    validate_frontier_entry_gate(pending_gate)
    if pending_gate.get("technical_status") != "pending-ai-review":
        raise FrontierError("frontier AI review must target the canonical pending gate")
    payload = {
        "reviewer_id": reviewer_id,
        "model_id": identity[0],
        "reasoning_effort": identity[1],
        "pending_gate_artifact_sha256": pending_gate["artifact_sha256"],
        "frontier_entry_evidence_sha256": evidence["artifact_sha256"],
        "frontier_protocol_sha256": pending_gate["frontier_protocol_sha256"],
        "g4_gate_artifact_sha256": pending_gate["g4_gate_artifact_sha256"],
        "decision": decision,
        "p0_findings": list(p0_findings),
        "p1_findings": list(p1_findings),
        "completed_at": completed_at,
    }
    receipt: dict[str, Any] = {
        "schema_version": FRONTIER_ENTRY_AI_INVOCATION_RECEIPT_SCHEMA,
        "reviewer_id": reviewer_id,
        "invocation_source": identity[2],
        "invocation_id": invocation_id,
        "requested_model": identity[0],
        "resolved_model": identity[0],
        "reasoning_effort": identity[1],
        "pending_gate_artifact_sha256": pending_gate["artifact_sha256"],
        "frontier_entry_evidence_sha256": evidence["artifact_sha256"],
        "frontier_protocol_sha256": pending_gate["frontier_protocol_sha256"],
        "g4_gate_artifact_sha256": pending_gate["g4_gate_artifact_sha256"],
        "review_payload_sha256": sha256_json(payload),
        "native_invocation_evidence_path": native_review_paths(
            _ENTRY_RECEIPT_PATHS[reviewer_id]
        )[0].as_posix(),
        "native_invocation_evidence_sha256": _sha(
            native_invocation_evidence_sha256,
            "frontier native invocation evidence hash",
        ),
        "native_output_sha256": _sha(
            native_output_sha256, "frontier native invocation output hash"
        ),
        "completed_at": completed_at,
    }
    receipt["artifact_sha256"] = sha256_json(receipt)
    validate_frontier_entry_ai_invocation_receipt(receipt)
    return receipt


def validate_frontier_entry_ai_review(
    value: Mapping[str, Any], receipt: Mapping[str, Any] | None = None
) -> None:
    expected = {
        "schema_version",
        "reviewer_id",
        "model_id",
        "reasoning_effort",
        "pending_gate_artifact_sha256",
        "frontier_entry_evidence_sha256",
        "frontier_protocol_sha256",
        "g4_gate_artifact_sha256",
        "invocation_receipt_path",
        "invocation_receipt_sha256",
        "decision",
        "p0_findings",
        "p1_findings",
        "completed_at",
        "artifact_sha256",
    }
    if set(value) != expected:
        raise FrontierError("frontier AI review fields changed")
    if value.get(
        "schema_version"
    ) != FRONTIER_ENTRY_AI_REVIEW_SCHEMA or not _valid_artifact_hash(value):
        raise FrontierError("frontier AI review hash is invalid")
    reviewer_id = value.get("reviewer_id")
    if not isinstance(reviewer_id, str) or reviewer_id not in _ENTRY_REVIEWERS:
        raise FrontierError("unknown frontier AI reviewer")
    model, effort, _ = _ENTRY_REVIEWERS[reviewer_id]
    if (value.get("model_id"), value.get("reasoning_effort")) != (model, effort):
        raise FrontierError("frontier AI reviewer identity changed")
    if (
        value.get("invocation_receipt_path")
        != _ENTRY_RECEIPT_PATHS[reviewer_id].as_posix()
    ):
        raise FrontierError("frontier AI invocation-receipt path changed")
    for field, label in (
        ("pending_gate_artifact_sha256", "reviewed pending frontier gate hash"),
        ("frontier_entry_evidence_sha256", "reviewed frontier evidence hash"),
        ("frontier_protocol_sha256", "reviewed frontier protocol hash"),
        ("g4_gate_artifact_sha256", "reviewed G4 gate hash"),
        ("invocation_receipt_sha256", "frontier invocation-receipt hash"),
    ):
        _sha(value.get(field), label)
    if value.get("decision") not in {"pass", "fail"}:
        raise FrontierError("frontier AI review decision is invalid")
    for field in ("p0_findings", "p1_findings"):
        findings = value.get(field)
        if not isinstance(findings, list) or any(
            not isinstance(item, str) or not item for item in findings
        ):
            raise FrontierError(f"frontier AI {field} are invalid")
    if value.get("decision") == "pass" and (
        value["p0_findings"] or value["p1_findings"]
    ):
        raise FrontierError("a passing frontier AI review cannot retain P0/P1 findings")
    completed_at = value.get("completed_at")
    if not isinstance(completed_at, str) or _UTC_SECOND.fullmatch(completed_at) is None:
        raise FrontierError("frontier AI review timestamp is invalid")
    if receipt is not None:
        validate_frontier_entry_ai_invocation_receipt(receipt)
        if (
            receipt.get("reviewer_id") != reviewer_id
            or receipt.get("artifact_sha256") != value["invocation_receipt_sha256"]
            or any(
                receipt.get(field) != value[field]
                for field in (
                    "pending_gate_artifact_sha256",
                    "frontier_entry_evidence_sha256",
                    "frontier_protocol_sha256",
                    "g4_gate_artifact_sha256",
                    "completed_at",
                )
            )
            or receipt.get("review_payload_sha256")
            != sha256_json(_frontier_review_payload(value))
        ):
            raise FrontierError(
                "frontier AI review differs from its invocation receipt"
            )


def build_frontier_entry_ai_review(
    *,
    reviewer_id: str,
    pending_gate: Mapping[str, Any],
    evidence: Mapping[str, Any],
    decision: str,
    p0_findings: Sequence[str],
    p1_findings: Sequence[str],
    completed_at: str,
    invocation_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    identity = _ENTRY_REVIEWERS.get(reviewer_id)
    if identity is None:
        raise FrontierError("unknown frontier AI reviewer")
    review: dict[str, Any] = {
        "schema_version": FRONTIER_ENTRY_AI_REVIEW_SCHEMA,
        "reviewer_id": reviewer_id,
        "model_id": identity[0],
        "reasoning_effort": identity[1],
        "pending_gate_artifact_sha256": pending_gate.get("artifact_sha256"),
        "frontier_entry_evidence_sha256": evidence.get("artifact_sha256"),
        "frontier_protocol_sha256": pending_gate.get("frontier_protocol_sha256"),
        "g4_gate_artifact_sha256": pending_gate.get("g4_gate_artifact_sha256"),
        "invocation_receipt_path": _ENTRY_RECEIPT_PATHS[reviewer_id].as_posix(),
        "invocation_receipt_sha256": invocation_receipt.get("artifact_sha256"),
        "decision": decision,
        "p0_findings": list(p0_findings),
        "p1_findings": list(p1_findings),
        "completed_at": completed_at,
    }
    review["artifact_sha256"] = sha256_json(review)
    validate_frontier_entry_ai_review(review, invocation_receipt)
    return review


def validate_frontier_entry_ai_review_provenance(
    root: Path,
    *,
    pending_gate: Mapping[str, Any],
    evidence: Mapping[str, Any],
    review: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> None:
    """Replay the native CLI execution behind one frontier-entry review."""

    from .review_invocations import (
        AIReviewInvocationError,
        assert_native_proof_fields,
        validate_recorded_ai_review,
    )

    validate_frontier_entry_ai_review(review, receipt)
    reviewer_id = str(review["reviewer_id"])
    anchor = _ENTRY_RECEIPT_PATHS[reviewer_id]
    bindings = {
        "frontier_protocol_path": FRONTIER_PROTOCOL_PATH.as_posix(),
        "frontier_protocol_sha256": str(pending_gate["frontier_protocol_sha256"]),
        "frontier_entry_evidence_path": FRONTIER_ENTRY_EVIDENCE_PATH.as_posix(),
        "frontier_entry_evidence_sha256": str(evidence["artifact_sha256"]),
        "pending_gate_path": FRONTIER_ENTRY_GATE_PATH.as_posix(),
        "pending_gate_artifact_sha256": str(pending_gate["artifact_sha256"]),
        "g4_gate_artifact_sha256": str(pending_gate["g4_gate_artifact_sha256"]),
    }
    try:
        native = validate_recorded_ai_review(
            root,
            anchor_path=anchor,
            reviewer_id=reviewer_id,
            review_kind="frontier-entry",
            target_bindings=bindings,
            expected_response={
                "decision": review["decision"],
                "p0_findings": review["p0_findings"],
                "p1_findings": review["p1_findings"],
            },
            invocation_id=str(receipt["invocation_id"]),
            completed_at=str(receipt["completed_at"]),
        )
        assert_native_proof_fields(anchor_path=anchor, receipt=receipt, evidence=native)
    except AIReviewInvocationError as exc:
        raise FrontierError(
            "frontier AI review lacks valid native execution proof"
        ) from exc


def build_frontier_entry_gate(
    protocol: Mapping[str, Any], evidence: Mapping[str, Any]
) -> dict[str, Any]:
    """Classify exact content-free evidence and create a pending Kevin gate."""

    validate_frontier_protocol(protocol)
    validate_frontier_entry_evidence(evidence)
    protocol_sha = sha256_json(protocol)
    if evidence.get("frontier_protocol_sha256") != protocol_sha:
        raise FrontierError("frontier entry evidence uses a different protocol")
    evidence_reference = [
        {
            "path": FRONTIER_ENTRY_EVIDENCE_PATH.as_posix(),
            "sha256": evidence["artifact_sha256"],
        }
    ]
    experiments = []
    for observation in evidence["observations"]:
        passed = _observation_passes(observation["experiment_id"], observation["facts"])
        experiments.append(
            {
                "experiment_id": observation["experiment_id"],
                "technical_decision": "eligible" if passed else "failed-entry",
                "checks": [
                    {
                        "check_id": observation["check_id"],
                        "passed": passed,
                        "evidence": evidence_reference,
                    }
                ],
            }
        )
    technical: dict[str, Any] = {
        "schema_version": FRONTIER_ENTRY_GATE_SCHEMA,
        "frontier_protocol_sha256": protocol_sha,
        "g4_gate_artifact_sha256": evidence["g4_gate_artifact_sha256"],
        "experiments": experiments,
        "ai_review_target_sha256": None,
        "ai_invocation_receipts": [],
        "ai_reviews": [],
        "technical_status": "pending-ai-review",
    }
    technical_sha = sha256_json(technical)
    gate: dict[str, Any] = {
        **technical,
        "technical_record_sha256": technical_sha,
        "human_approval": {
            "status": "pending",
            "reviewer": "Kevin Araujo",
            "technical_record_sha256": technical_sha,
        },
        "final_status": "blocked-pending-ai-review",
    }
    gate["artifact_sha256"] = sha256_json(gate)
    validate_frontier_entry_gate(gate)
    return gate


def build_frontier_entry_reviewed_gate(
    protocol: Mapping[str, Any],
    evidence: Mapping[str, Any],
    pending_gate: Mapping[str, Any],
    *,
    ai_reviews: Sequence[Mapping[str, Any]],
    ai_invocation_receipts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Bind the two exact AI invocations to the canonical pending entry gate."""

    expected_pending = build_frontier_entry_gate(protocol, evidence)
    if dict(pending_gate) != expected_pending:
        raise FrontierError("frontier AI reviews target a non-canonical pending gate")
    reviews = [dict(review) for review in ai_reviews]
    receipts = [dict(receipt) for receipt in ai_invocation_receipts]
    if len(reviews) != 2 or len(receipts) != 2:
        raise FrontierError(
            "frontier entry requires exactly two AI reviews and invocation receipts"
        )
    for review, receipt in zip(reviews, receipts, strict=True):
        validate_frontier_entry_ai_review(review, receipt)
        if (
            review["pending_gate_artifact_sha256"] != pending_gate["artifact_sha256"]
            or review["frontier_entry_evidence_sha256"] != evidence["artifact_sha256"]
            or review["frontier_protocol_sha256"]
            != pending_gate["frontier_protocol_sha256"]
            or review["g4_gate_artifact_sha256"]
            != pending_gate["g4_gate_artifact_sha256"]
        ):
            raise FrontierError("frontier AI review binds different entry evidence")
    if len({receipt["invocation_id"] for receipt in receipts}) != 2:
        raise FrontierError("frontier AI reviews must use separate invocations")
    if [review["reviewer_id"] for review in reviews] != list(_ENTRY_REVIEWERS):
        raise FrontierError("frontier AI review order or identity changed")
    technical = {
        key: item
        for key, item in expected_pending.items()
        if key
        not in {
            "technical_record_sha256",
            "human_approval",
            "final_status",
            "artifact_sha256",
        }
    }
    technical["ai_review_target_sha256"] = pending_gate["artifact_sha256"]
    technical["ai_invocation_receipts"] = receipts
    technical["ai_reviews"] = reviews
    technical["technical_status"] = (
        "passed"
        if all(review["decision"] == "pass" for review in reviews)
        else "failed"
    )
    technical_sha = sha256_json(technical)
    gate: dict[str, Any] = {
        **technical,
        "technical_record_sha256": technical_sha,
        "human_approval": {
            "status": "pending",
            "reviewer": "Kevin Araujo",
            "technical_record_sha256": technical_sha,
        },
        "final_status": (
            "blocked-pending-human-review"
            if technical["technical_status"] == "passed"
            else "blocked-ai-review-failed"
        ),
    }
    gate["artifact_sha256"] = sha256_json(gate)
    validate_frontier_entry_gate(gate)
    return gate


def _validate_public_evidence(value: Any, label: str) -> None:
    if not isinstance(value, list) or not value:
        raise FrontierError(f"{label} must contain public evidence")
    paths: set[str] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping) or set(raw) != {"path", "sha256"}:
            raise FrontierError(f"{label}[{index}] fields changed")
        path_value = _nonempty(raw.get("path"), f"{label}[{index}].path")
        path = Path(path_value)
        if path.is_absolute() or ".." in path.parts:
            raise FrontierError(f"{label}[{index}] path escapes the repository")
        lowered = path_value.casefold()
        if any(token in lowered for token in _FORBIDDEN_EVIDENCE_PATH_TOKENS):
            raise FrontierError(f"{label}[{index}] is not public evidence")
        if path_value in paths:
            raise FrontierError(f"{label} contains a duplicate path")
        paths.add(path_value)
        _sha(raw.get("sha256"), f"{label}[{index}].sha256")


def validate_frontier_entry_gate(value: Mapping[str, Any]) -> None:
    """Validate a complete seven-experiment gate and its exact human binding."""

    expected = {
        "schema_version",
        "frontier_protocol_sha256",
        "g4_gate_artifact_sha256",
        "experiments",
        "ai_review_target_sha256",
        "ai_invocation_receipts",
        "ai_reviews",
        "technical_status",
        "technical_record_sha256",
        "human_approval",
        "final_status",
        "artifact_sha256",
    }
    if set(value) != expected:
        raise FrontierError("frontier entry gate fields changed")
    if value.get("schema_version") != FRONTIER_ENTRY_GATE_SCHEMA:
        raise FrontierError("unsupported frontier entry gate schema")
    _sha(value.get("frontier_protocol_sha256"), "frontier protocol hash")
    _sha(value.get("g4_gate_artifact_sha256"), "G4 gate hash")
    technical = {
        key: item
        for key, item in value.items()
        if key
        not in {
            "technical_record_sha256",
            "human_approval",
            "final_status",
            "artifact_sha256",
        }
    }
    technical_sha = sha256_json(technical)
    if value.get("technical_record_sha256") != technical_sha:
        raise FrontierError("frontier entry technical-record hash mismatch")
    if not _valid_artifact_hash(value):
        raise FrontierError("frontier entry artifact hash mismatch")

    experiments = value.get("experiments")
    if not isinstance(experiments, list) or len(experiments) != 7:
        raise FrontierError("frontier entry gate must contain F1-F7")
    identifiers: list[str] = []
    evidence_hashes: set[str] = set()
    for index, experiment in enumerate(experiments):
        if not isinstance(experiment, Mapping) or set(experiment) != {
            "experiment_id",
            "technical_decision",
            "checks",
        }:
            raise FrontierError(f"frontier gate experiment {index} fields changed")
        experiment_id = _nonempty(
            experiment.get("experiment_id"), f"frontier experiment {index}"
        )
        identifiers.append(experiment_id)
        decision = experiment.get("technical_decision")
        if decision not in {"eligible", "failed-entry"}:
            raise FrontierError(f"{experiment_id} technical decision is invalid")
        checks = experiment.get("checks")
        if not isinstance(checks, list) or len(checks) != 1:
            raise FrontierError(f"{experiment_id} needs its one frozen entry check")
        check_ids: set[str] = set()
        passed: list[bool] = []
        for check_index, check in enumerate(checks):
            if not isinstance(check, Mapping) or set(check) != {
                "check_id",
                "passed",
                "evidence",
            }:
                raise FrontierError(
                    f"{experiment_id} check {check_index} fields changed"
                )
            check_id = _nonempty(
                check.get("check_id"), f"{experiment_id} check {check_index}"
            )
            if check_id != _CHECK_IDS.get(experiment_id):
                raise FrontierError(f"{experiment_id} check identity changed")
            if check_id in check_ids:
                raise FrontierError(f"{experiment_id} contains duplicate checks")
            check_ids.add(check_id)
            if not isinstance(check.get("passed"), bool):
                raise FrontierError(f"{experiment_id} check result must be boolean")
            passed.append(check["passed"])
            _validate_public_evidence(
                check.get("evidence"), f"{experiment_id}.{check_id}.evidence"
            )
            evidence_rows = check["evidence"]
            if (
                len(evidence_rows) != 1
                or evidence_rows[0]["path"] != FRONTIER_ENTRY_EVIDENCE_PATH.as_posix()
            ):
                raise FrontierError(
                    f"{experiment_id} must bind the canonical entry evidence"
                )
            evidence_hashes.add(evidence_rows[0]["sha256"])
        if (decision == "eligible") != all(passed):
            raise FrontierError(f"{experiment_id} decision disagrees with its checks")
    if identifiers != list(_EXPECTED_CONDITIONS):
        raise FrontierError("frontier entry gate experiment order changed")
    if len(evidence_hashes) != 1:
        raise FrontierError("frontier experiments bind different entry evidence")
    evidence_sha = next(iter(evidence_hashes))

    target = value.get("ai_review_target_sha256")
    receipts = value.get("ai_invocation_receipts")
    reviews = value.get("ai_reviews")
    technical_status = value.get("technical_status")
    if not isinstance(receipts, list) or not isinstance(reviews, list):
        raise FrontierError("frontier AI review records must be ordered lists")
    if target is None:
        if receipts or reviews or technical_status != "pending-ai-review":
            raise FrontierError("unreviewed frontier gate AI state changed")
    else:
        _sha(target, "frontier AI review target")
        base_technical = {
            "schema_version": value["schema_version"],
            "frontier_protocol_sha256": value["frontier_protocol_sha256"],
            "g4_gate_artifact_sha256": value["g4_gate_artifact_sha256"],
            "experiments": value["experiments"],
            "ai_review_target_sha256": None,
            "ai_invocation_receipts": [],
            "ai_reviews": [],
            "technical_status": "pending-ai-review",
        }
        base_technical_sha = sha256_json(base_technical)
        base_gate: dict[str, Any] = {
            **base_technical,
            "technical_record_sha256": base_technical_sha,
            "human_approval": {
                "status": "pending",
                "reviewer": "Kevin Araujo",
                "technical_record_sha256": base_technical_sha,
            },
            "final_status": "blocked-pending-ai-review",
        }
        base_gate["artifact_sha256"] = sha256_json(base_gate)
        if target != base_gate["artifact_sha256"]:
            raise FrontierError(
                "frontier AI reviews target a non-canonical pending gate"
            )
        if len(receipts) != 2 or len(reviews) != 2:
            raise FrontierError(
                "frontier entry requires exactly two AI reviews and receipts"
            )
        if [
            review.get("reviewer_id")
            for review in reviews
            if isinstance(review, Mapping)
        ] != list(_ENTRY_REVIEWERS) or [
            receipt.get("reviewer_id")
            for receipt in receipts
            if isinstance(receipt, Mapping)
        ] != list(_ENTRY_REVIEWERS):
            raise FrontierError("frontier AI reviewer order or identity changed")
        for review, receipt in zip(reviews, receipts, strict=True):
            if not isinstance(review, Mapping) or not isinstance(receipt, Mapping):
                raise FrontierError("frontier AI review records must be objects")
            validate_frontier_entry_ai_review(review, receipt)
            if (
                review.get("pending_gate_artifact_sha256") != target
                or review.get("frontier_entry_evidence_sha256") != evidence_sha
                or review.get("frontier_protocol_sha256")
                != value["frontier_protocol_sha256"]
                or review.get("g4_gate_artifact_sha256")
                != value["g4_gate_artifact_sha256"]
            ):
                raise FrontierError("frontier AI review target binding changed")
        if len({receipt["invocation_id"] for receipt in receipts}) != 2:
            raise FrontierError("frontier AI reviews must use separate invocations")
        expected_technical_status = (
            "passed"
            if all(review["decision"] == "pass" for review in reviews)
            else "failed"
        )
        if technical_status != expected_technical_status:
            raise FrontierError("frontier AI review decisions disagree with status")

    approval = value.get("human_approval")
    if not isinstance(approval, Mapping):
        raise FrontierError("frontier entry approval is missing")
    status = approval.get("status")
    if (
        approval.get("reviewer") != "Kevin Araujo"
        or approval.get("technical_record_sha256") != technical_sha
    ):
        raise FrontierError("frontier entry approval is not bound to Kevin")
    if status == "pending":
        expected_final = {
            "pending-ai-review": "blocked-pending-ai-review",
            "passed": "blocked-pending-human-review",
            "failed": "blocked-ai-review-failed",
        }.get(technical_status)
        if (
            set(approval)
            != {
                "status",
                "reviewer",
                "technical_record_sha256",
            }
            or expected_final is None
            or value.get("final_status") != expected_final
        ):
            raise FrontierError("pending frontier approval fields changed")
    elif status == "approved":
        if (
            set(approval)
            != {
                "status",
                "reviewer",
                "technical_record_sha256",
                "approved_at",
            }
            or not isinstance(approval.get("approved_at"), str)
            or _UTC_SECOND.fullmatch(approval["approved_at"]) is None
            or technical_status != "passed"
            or value.get("final_status") != "approved"
        ):
            raise FrontierError("approved frontier decision is invalid")
    else:
        raise FrontierError("unsupported frontier human-approval status")


def require_frontier_experiment_approved(
    gate: Mapping[str, Any], experiment_id: str
) -> Mapping[str, Any]:
    """Return one runnable entry only after Kevin approved the exact gate."""

    validate_frontier_entry_gate(gate)
    approval = gate["human_approval"]
    if approval["status"] != "approved" or gate["technical_status"] != "passed":
        raise FrontierError(
            "frontier execution requires both exact AI reviews and Kevin's approval"
        )
    for experiment in gate["experiments"]:
        if experiment["experiment_id"] != experiment_id:
            continue
        if experiment["technical_decision"] != "eligible":
            raise FrontierError(f"{experiment_id} has a saved failed entry decision")
        return experiment
    raise FrontierError(f"unknown frontier experiment: {experiment_id}")


def require_approved_f3_source_commitment(
    root: Path,
    gate: Mapping[str, Any],
    commitment: Mapping[str, Any],
) -> dict[str, str]:
    """Bind an F3 execution to the exact source manifest Kevin approved."""

    repository = root.resolve()
    experiment = require_frontier_experiment_approved(gate, "F3")
    evidence = _read_public_json(
        repository, FRONTIER_ENTRY_EVIDENCE_PATH, "frontier entry evidence"
    )
    validate_frontier_entry_evidence(evidence)
    expected_reference = [
        {
            "path": FRONTIER_ENTRY_EVIDENCE_PATH.as_posix(),
            "sha256": evidence["artifact_sha256"],
        }
    ]
    if experiment["checks"][0]["evidence"] != expected_reference:
        raise FrontierError("approved F3 gate does not bind canonical entry evidence")
    if (
        evidence["frontier_protocol_sha256"] != gate["frontier_protocol_sha256"]
        or evidence["g4_gate_artifact_sha256"] != gate["g4_gate_artifact_sha256"]
    ):
        raise FrontierError("approved F3 evidence differs from its frontier gate")

    observation = next(
        (row for row in evidence["observations"] if row["experiment_id"] == "F3"),
        None,
    )
    if not isinstance(observation, Mapping):
        raise FrontierError("approved F3 evidence is missing")
    expected = observation["facts"]["approved_source_commitment"]
    if not isinstance(expected, Mapping):
        raise FrontierError("approved F3 source commitment is missing")
    if not isinstance(commitment, Mapping) or set(commitment) != {"path", "sha256"}:
        raise FrontierError("supplied F3 source commitment fields changed")
    supplied = {
        "path": _nonempty(commitment.get("path"), "supplied F3 source path"),
        "sha256": _sha(commitment.get("sha256"), "supplied F3 source hash"),
    }
    approved = {"path": expected["path"], "sha256": expected["sha256"]}
    if supplied != approved:
        raise FrontierError("supplied F3 source commitment differs from Kevin's gate")
    current = _f3_source_manifest_commitment(repository)
    if current != approved:
        raise FrontierError(
            "approved F3 source manifest no longer matches its gate commitment"
        )
    return approved


def _g4_barrier(root: Path, *, route_migration: bool) -> dict[str, Any]:
    from .g4_gate import load_approved_g4_gate

    final_path = root / G4_GATE_PATH
    if not final_path.is_file() or final_path.is_symlink():
        return {
            "gate": "G4",
            "status": "blocked",
            "reason": "g4_gate_missing",
            "evidence_path": G4_GATE_PATH.as_posix(),
            "artifact_sha256": None,
        }
    try:
        gate = load_approved_g4_gate(
            root,
            replay_historical_provider=not route_migration,
        )
    except Exception:
        return {
            "gate": "G4",
            "status": "blocked",
            "reason": "g4_gate_invalid_or_unapproved",
            "evidence_path": G4_GATE_PATH.as_posix(),
            "artifact_sha256": None,
        }
    return {
        "gate": "G4",
        "status": "passed",
        "reason": None,
        "evidence_path": G4_GATE_PATH.as_posix(),
        "artifact_sha256": gate["artifact_sha256"],
    }


def build_frontier_status(root: Path | None = None) -> dict[str, Any]:
    """Return a deterministic, read-only status without entering an experiment."""

    repository = (root or repository_root()).resolve()
    protocol = load_frontier_protocol(repository)
    barrier = _g4_barrier(
        repository, route_migration=bool(protocol["provider_route_overrides"])
    )
    experiments = []
    for row in protocol["experiments"]:
        experiments.append(
            {
                "experiment_id": row["experiment_id"],
                "title": row["title"],
                "entry_condition": row["entry_condition"],
                "additional_gate": row["additional_gate"],
                "entry_status": (
                    "blocked" if barrier["status"] != "passed" else "unevaluated"
                ),
                "reason": (
                    "program_barrier_not_passed"
                    if barrier["status"] != "passed"
                    else "entry_evidence_not_evaluated"
                ),
            }
        )
    status: dict[str, Any] = {
        "schema_version": FRONTIER_STATUS_SCHEMA,
        "frontier_protocol_sha256": sha256_json(protocol),
        "program_barrier": barrier,
        "experiments": experiments,
    }
    status["artifact_sha256"] = sha256_json(status)
    return status


def freeze_frontier_entry_gates(root: Path | None = None) -> dict[str, Any]:
    """Evaluate and persist entry gates only after the approved G4 barrier."""

    repository = (root or repository_root()).resolve()
    status = build_frontier_status(repository)
    if status["program_barrier"]["status"] != "passed":
        raise FrontierError("frontier entry gates require an approved G4 gate")
    g4_sha = status["program_barrier"]["artifact_sha256"]
    evidence = collect_frontier_entry_evidence(repository, g4_gate_sha256=g4_sha)
    gate = build_frontier_entry_gate(load_frontier_protocol(repository), evidence)
    return write_frontier_entry_gate(repository, evidence=evidence, gate=gate)


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _open_immutable_parent(repository: Path, relative: Path) -> int:
    """Open or create one artifact parent without following path symlinks."""

    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(repository, flags)
    except OSError as exc:
        raise ImmutableIOError("immutable repository root is unsafe") from exc
    try:
        for component in relative.parent.parts:
            if component in {"", ".", ".."}:
                raise ImmutableIOError("immutable artifact parent is invalid")
            try:
                os.mkdir(component, 0o755, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError as exc:
        os.close(descriptor)
        raise ImmutableIOError("immutable artifact parent is unsafe") from exc
    except Exception:
        os.close(descriptor)
        raise


def _read_immutable_target(parent: int, name: str) -> bytes | None:
    """Read one regular target while proving its pathname identity is stable."""

    try:
        path_stat = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ImmutableIOError("cannot inspect immutable artifact") from exc
    if not stat.S_ISREG(path_stat.st_mode):
        raise ImmutableIOError("immutable artifact target is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = -1
    try:
        descriptor = os.open(name, flags, dir_fd=parent)
        before = os.fstat(descriptor)
        chunks: list[bytes] = []
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        after = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        identity = (before.st_dev, before.st_ino)
        if (
            not stat.S_ISREG(before.st_mode)
            or identity != (after.st_dev, after.st_ino)
            or identity != (current.st_dev, current.st_ino)
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
        ):
            raise ImmutableIOError("immutable artifact changed while it was read")
        return b"".join(chunks)
    except OSError as exc:
        raise ImmutableIOError("cannot read immutable artifact") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _unlink_matching_inode(parent: int, name: str, *, device: int, inode: int) -> None:
    """Remove only the link created by this process, never an attacker swap."""

    try:
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (current.st_dev, current.st_ino) == (device, inode):
            os.unlink(name, dir_fd=parent)
    except OSError:
        pass


def _write_immutable_plan(
    root: Path,
    plan: Mapping[Path, bytes],
    *,
    allow_partial_exact: bool = False,
) -> None:
    """Publish an exact multi-file plan through retained directory descriptors."""

    repository = Path(os.path.abspath(root))
    try:
        root_stat = repository.lstat()
    except OSError as exc:
        raise ImmutableIOError("immutable repository root is missing") from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise ImmutableIOError("immutable repository root is unsafe")
    if not plan:
        raise ImmutableIOError("immutable artifact plan is empty")

    entries: list[dict[str, Any]] = []
    try:
        for requested, payload in plan.items():
            if not isinstance(requested, Path) or not isinstance(payload, bytes):
                raise ImmutableIOError("immutable artifact plan is invalid")
            absolute = Path(
                os.path.abspath(
                    requested if requested.is_absolute() else repository / requested
                )
            )
            try:
                relative = absolute.relative_to(repository)
            except ValueError as exc:
                raise ImmutableIOError(
                    "immutable artifact escapes the repository"
                ) from exc
            if (
                not relative.parts
                or relative.name in {"", ".", ".."}
                or ".." in relative.parts
            ):
                raise ImmutableIOError("immutable artifact path is invalid")
            parent = _open_immutable_parent(repository, relative)
            entries.append(
                {
                    "parent": parent,
                    "name": relative.name,
                    "payload": payload,
                    "existing": False,
                    "temporary": None,
                    "device": None,
                    "inode": None,
                    "linked": False,
                }
            )

        existing_count = 0
        for entry in entries:
            current = _read_immutable_target(entry["parent"], entry["name"])
            if current is None:
                continue
            if current != entry["payload"]:
                raise ImmutableIOError("immutable artifact differs")
            entry["existing"] = True
            existing_count += 1
        if existing_count == len(entries):
            if any(
                _read_immutable_target(entry["parent"], entry["name"])
                != entry["payload"]
                for entry in entries
            ):
                raise ImmutableIOError("immutable artifact changed after verification")
            return
        if existing_count and not allow_partial_exact:
            raise ImmutableIOError("immutable artifact plan partially exists")

        for entry in entries:
            if entry["existing"]:
                continue
            temporary: str | None = None
            descriptor = -1
            for _ in range(10):
                candidate = f".{entry['name']}.contextlab-{secrets.token_hex(12)}.tmp"
                try:
                    descriptor = os.open(
                        candidate,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_NOFOLLOW", 0)
                        | getattr(os, "O_CLOEXEC", 0),
                        0o600,
                        dir_fd=entry["parent"],
                    )
                except FileExistsError:
                    continue
                temporary = candidate
                break
            if descriptor < 0 or temporary is None:
                raise ImmutableIOError("cannot allocate immutable temporary file")
            entry["temporary"] = temporary
            try:
                staged = os.fstat(descriptor)
                if not stat.S_ISREG(staged.st_mode):
                    raise ImmutableIOError("immutable temporary target is unsafe")
                entry["device"] = staged.st_dev
                entry["inode"] = staged.st_ino
                written = 0
                while written < len(entry["payload"]):
                    count = os.write(descriptor, entry["payload"][written:])
                    if count <= 0:
                        raise OSError("short immutable artifact write")
                    written += count
                os.fsync(descriptor)
            except OSError as exc:
                raise ImmutableIOError("cannot write immutable artifact") from exc
            finally:
                os.close(descriptor)

            current = os.stat(
                temporary,
                dir_fd=entry["parent"],
                follow_symlinks=False,
            )
            if (current.st_dev, current.st_ino) != (
                entry["device"],
                entry["inode"],
            ):
                raise ImmutableIOError("immutable temporary target changed")

        for entry in entries:
            if entry["existing"]:
                continue
            os.link(
                entry["temporary"],
                entry["name"],
                src_dir_fd=entry["parent"],
                dst_dir_fd=entry["parent"],
                follow_symlinks=False,
            )
            entry["linked"] = True
            published = os.stat(
                entry["name"],
                dir_fd=entry["parent"],
                follow_symlinks=False,
            )
            if (published.st_dev, published.st_ino) != (
                entry["device"],
                entry["inode"],
            ) or not stat.S_ISREG(published.st_mode):
                raise ImmutableIOError("immutable artifact changed during publication")
            _unlink_matching_inode(
                entry["parent"],
                entry["temporary"],
                device=entry["device"],
                inode=entry["inode"],
            )
            entry["temporary"] = None
            os.fsync(entry["parent"])

        for entry in entries:
            if (
                _read_immutable_target(entry["parent"], entry["name"])
                != entry["payload"]
            ):
                raise ImmutableIOError("immutable artifact changed after publication")
    except Exception as exc:
        for entry in reversed(entries):
            device = entry["device"]
            inode = entry["inode"]
            if entry["linked"] and device is not None and inode is not None:
                _unlink_matching_inode(
                    entry["parent"],
                    entry["name"],
                    device=device,
                    inode=inode,
                )
            temporary = entry["temporary"]
            if temporary is not None and device is not None and inode is not None:
                _unlink_matching_inode(
                    entry["parent"],
                    temporary,
                    device=device,
                    inode=inode,
                )
            try:
                os.fsync(entry["parent"])
            except OSError:
                pass
        if isinstance(exc, ImmutableIOError):
            raise
        raise ImmutableIOError("cannot persist immutable artifact plan") from exc
    finally:
        for entry in entries:
            temporary = entry["temporary"]
            device = entry["device"]
            inode = entry["inode"]
            if temporary is not None and device is not None and inode is not None:
                _unlink_matching_inode(
                    entry["parent"],
                    temporary,
                    device=device,
                    inode=inode,
                )
            try:
                os.close(entry["parent"])
            except OSError:
                pass


def _safe_artifact_target(root: Path, target: Path, label: str) -> Path:
    """Reject repository escapes and every symlink in an artifact path."""

    repository = root.resolve()
    try:
        relative = target.relative_to(repository)
    except ValueError as exc:
        raise FrontierError(f"{label} path escapes the repository") from exc
    if not relative.parts or ".." in relative.parts:
        raise FrontierError(f"{label} path escapes the repository")

    current = repository
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise FrontierError(f"{label} path contains a symlink: {current}")
        if current != target and current.exists() and not current.is_dir():
            raise FrontierError(f"{label} parent is not a directory: {current}")
    try:
        target.resolve(strict=False).relative_to(repository)
    except ValueError as exc:
        raise FrontierError(f"{label} path escapes the repository") from exc
    return target


def write_frontier_entry_gate(
    root: Path | None = None,
    *,
    evidence: Mapping[str, Any],
    gate: Mapping[str, Any],
) -> dict[str, Any]:
    """Create the content-free evidence and pending gate atomically and immutably."""

    repository = (root or repository_root()).resolve()
    validate_frontier_entry_evidence(evidence)
    validate_frontier_entry_gate(gate)
    protocol = (
        load_frontier_protocol(repository)
        if (repository / FRONTIER_PROTOCOL_PATH).is_file()
        else None
    )
    if protocol is not None:
        expected = build_frontier_entry_gate(protocol, evidence)
        if dict(gate) != expected:
            raise FrontierError("frontier entry gate differs from its exact evidence")
    elif gate.get("frontier_protocol_sha256") != evidence.get(
        "frontier_protocol_sha256"
    ):
        raise FrontierError("frontier entry gate protocol binding differs")
    if gate.get("g4_gate_artifact_sha256") != evidence.get("g4_gate_artifact_sha256"):
        raise FrontierError("frontier entry gate G4 binding differs")

    plan = {
        repository / FRONTIER_ENTRY_EVIDENCE_PATH: _json_bytes(evidence),
        repository / FRONTIER_ENTRY_GATE_PATH: _json_bytes(gate),
    }
    for path in plan:
        _safe_artifact_target(repository, path, "frontier entry artifact")
    try:
        _write_immutable_plan(repository, plan, allow_partial_exact=True)
    except ImmutableIOError as exc:
        raise FrontierError(f"immutable frontier artifact differs: {exc}") from exc
    return dict(gate)


def _load_pending_frontier_entry(
    root: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    protocol = load_frontier_protocol(root)
    evidence = _read_public_json(
        root, FRONTIER_ENTRY_EVIDENCE_PATH, "frontier entry evidence"
    )
    pending = _read_public_json(
        root, FRONTIER_ENTRY_GATE_PATH, "pending frontier entry gate"
    )
    validate_frontier_entry_evidence(evidence)
    validate_frontier_entry_gate(pending)
    expected = build_frontier_entry_gate(protocol, evidence)
    if pending != expected:
        raise FrontierError("pending frontier entry gate differs from exact evidence")
    return protocol, evidence, pending


def _load_reviewed_frontier_entry(
    root: Path,
    protocol: Mapping[str, Any],
    evidence: Mapping[str, Any],
    pending: Mapping[str, Any],
) -> dict[str, Any]:
    receipts = [
        _read_public_json(root, path, f"frontier AI invocation receipt {index}")
        for index, path in enumerate(FRONTIER_ENTRY_AI_INVOCATION_RECEIPT_PATHS)
    ]
    reviews = [
        _read_public_json(root, path, f"frontier AI review {index}")
        for index, path in enumerate(FRONTIER_ENTRY_AI_REVIEW_PATHS)
    ]
    for review, receipt in zip(reviews, receipts, strict=True):
        validate_frontier_entry_ai_review_provenance(
            root,
            pending_gate=pending,
            evidence=evidence,
            review=review,
            receipt=receipt,
        )
    expected = build_frontier_entry_reviewed_gate(
        protocol,
        evidence,
        pending,
        ai_reviews=reviews,
        ai_invocation_receipts=receipts,
    )
    saved = _read_public_json(
        root, FRONTIER_ENTRY_REVIEWED_GATE_PATH, "reviewed frontier entry gate"
    )
    validate_frontier_entry_gate(saved)
    if saved != expected:
        raise FrontierError(
            "reviewed frontier entry gate differs from exact AI reviews"
        )
    return saved


def _create_only_plan(root: Path, plan: Mapping[Path, bytes]) -> None:
    repository = root.resolve()
    for path in plan:
        _safe_artifact_target(repository, path, "frontier approval artifact")
    try:
        _write_immutable_plan(repository, plan)
    except ImmutableIOError as exc:
        raise FrontierError(
            "frontier approval artifact already exists or is unsafe"
        ) from exc


def write_frontier_entry_reviewed_gate(
    root: Path | None = None, *, gate: Mapping[str, Any]
) -> dict[str, Any]:
    """Persist the exact dual-AI reviewed gate for Kevin's audit."""

    repository = (root or repository_root()).resolve()
    validate_frontier_entry_gate(gate)
    if (
        gate.get("ai_review_target_sha256") is None
        or gate.get("human_approval", {}).get("status") != "pending"
        or gate.get("final_status")
        not in {"blocked-pending-human-review", "blocked-ai-review-failed"}
    ):
        raise FrontierError("only a dual-AI reviewed pending entry gate can be saved")
    path = repository / FRONTIER_ENTRY_REVIEWED_GATE_PATH
    data = _json_bytes(gate)
    _safe_artifact_target(repository, path, "frontier approval artifact")
    try:
        _create_only_plan(repository, {path: data})
    except FrontierError as exc:
        raise FrontierError("immutable reviewed frontier entry gate differs") from exc
    return dict(gate)


def approve_frontier_entry_gate(
    root: Path | None = None, *, approved_at: str
) -> dict[str, Any]:
    """Create Kevin's separate approval and the derived approved gate once."""

    repository = (root or repository_root()).resolve()
    if not isinstance(approved_at, str) or _UTC_SECOND.fullmatch(approved_at) is None:
        raise FrontierError("frontier approval timestamp must be UTC to the second")
    protocol, evidence, pending = _load_pending_frontier_entry(repository)
    reviewed = _load_reviewed_frontier_entry(repository, protocol, evidence, pending)
    if reviewed["technical_status"] != "passed":
        raise FrontierError("frontier entry AI reviews did not both pass")
    approval: dict[str, Any] = {
        "schema_version": FRONTIER_ENTRY_APPROVAL_SCHEMA,
        "technical_record_sha256": reviewed["technical_record_sha256"],
        "pending_gate_artifact_sha256": pending["artifact_sha256"],
        "reviewer": "Kevin Araujo",
        "reviewer_role": "sole_human_reviewer",
        "decision": "approved",
        "approved_at": approved_at,
    }
    approval["artifact_sha256"] = sha256_json(approval)
    final_gate: dict[str, Any] = {
        key: item
        for key, item in reviewed.items()
        if key not in {"human_approval", "final_status", "artifact_sha256"}
    }
    final_gate["human_approval"] = {
        "status": "approved",
        "reviewer": "Kevin Araujo",
        "technical_record_sha256": reviewed["technical_record_sha256"],
        "approved_at": approved_at,
    }
    final_gate["final_status"] = "approved"
    final_gate["artifact_sha256"] = sha256_json(final_gate)
    validate_frontier_entry_gate(final_gate)
    _create_only_plan(
        repository,
        {
            repository / FRONTIER_ENTRY_APPROVAL_PATH: _json_bytes(approval),
            repository / FRONTIER_ENTRY_APPROVED_GATE_PATH: _json_bytes(final_gate),
        },
    )
    return approval


def load_approved_frontier_entry_gate(
    root: Path | None = None,
) -> dict[str, Any]:
    """Load the approved gate only while its live G4 and sources still replay."""

    repository = (root or repository_root()).resolve()
    protocol, evidence, pending = _load_pending_frontier_entry(repository)
    try:
        from .g4_gate import load_approved_g4_gate

        current_g4 = load_approved_g4_gate(
            repository,
            replay_historical_provider=not bool(
                protocol["provider_route_overrides"]
            ),
        )
    except Exception as exc:
        raise FrontierError(
            "approved frontier entry requires the current approved G4 gate"
        ) from exc
    current_g4_sha = _sha(
        current_g4.get("artifact_sha256"), "current approved G4 gate hash"
    )
    if (
        evidence.get("g4_gate_artifact_sha256") != current_g4_sha
        or pending.get("g4_gate_artifact_sha256") != current_g4_sha
    ):
        raise FrontierError("approved frontier entry binds a stale G4 gate")
    current_evidence = collect_frontier_entry_evidence(
        repository, g4_gate_sha256=current_g4_sha
    )
    if current_evidence != evidence:
        raise FrontierError(
            "approved frontier entry evidence differs from current public sources"
        )
    reviewed = _load_reviewed_frontier_entry(repository, protocol, evidence, pending)
    approval = _read_public_json(
        repository, FRONTIER_ENTRY_APPROVAL_PATH, "frontier entry approval"
    )
    final_gate = _read_public_json(
        repository,
        FRONTIER_ENTRY_APPROVED_GATE_PATH,
        "approved frontier entry gate",
    )
    if (
        set(approval)
        != {
            "schema_version",
            "technical_record_sha256",
            "pending_gate_artifact_sha256",
            "reviewer",
            "reviewer_role",
            "decision",
            "approved_at",
            "artifact_sha256",
        }
        or approval.get("schema_version") != FRONTIER_ENTRY_APPROVAL_SCHEMA
        or not _valid_artifact_hash(approval)
        or approval.get("technical_record_sha256")
        != reviewed["technical_record_sha256"]
        or approval.get("pending_gate_artifact_sha256") != pending["artifact_sha256"]
        or approval.get("reviewer") != "Kevin Araujo"
        or approval.get("reviewer_role") != "sole_human_reviewer"
        or approval.get("decision") != "approved"
        or not isinstance(approval.get("approved_at"), str)
        or _UTC_SECOND.fullmatch(approval["approved_at"]) is None
    ):
        raise FrontierError("frontier entry approval is invalid")
    validate_frontier_entry_gate(final_gate)
    expected_final: dict[str, Any] = {
        key: item
        for key, item in reviewed.items()
        if key not in {"human_approval", "final_status", "artifact_sha256"}
    }
    expected_final["human_approval"] = {
        "status": "approved",
        "reviewer": "Kevin Araujo",
        "technical_record_sha256": reviewed["technical_record_sha256"],
        "approved_at": approval["approved_at"],
    }
    expected_final["final_status"] = "approved"
    expected_final["artifact_sha256"] = sha256_json(expected_final)
    if final_gate != expected_final:
        raise FrontierError("approved frontier gate differs from its approval")
    return final_gate
