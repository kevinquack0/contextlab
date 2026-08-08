"""Fail-closed G5 approval contract for the F6 self-improvement probe.

G5 records only public artifact commitments and an opaque commitment to the
external sealed evaluator.  This module never opens the external sealed path.
The F6 runner must present the recorded commitment to that evaluator before it
can execute a sealed evaluation.
"""

from __future__ import annotations

from collections.abc import Mapping
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

from .baseline import repository_root
from .boundaries import CorpusBoundary, ProtectedDataError
from .g4_gate import load_approved_g4_gate
from .immutable_io import (
    ImmutableIOError,
    read_bytes_snapshot,
    write_json_once_or_verify,
)
from .tasking import sha256_json


G5_TECHNICAL_SCHEMA = "contextlab.g5-technical-gate.v2"
G5_APPROVAL_SCHEMA = "contextlab.g5-kevin-approval.v2"
G5_FINAL_SCHEMA = "contextlab.g5-final-gate.v2"

F6_PROPOSAL_SCHEMA = "contextlab.f6-change-proposal.v1"
F6_ROLLBACK_SCHEMA = "contextlab.f6-rollback-contract.v1"
F6_BASELINE_SCHEMA = "contextlab.f6-simpler-baseline.v1"
F6_SEALED_EVALUATION_SCHEMA = "contextlab.f6-external-sealed-evaluation-commitment.v1"

G5_TECHNICAL_PATH = Path("results/v2/gates/G5.pending.json")
G5_APPROVAL_PATH = Path("results/v2/gates/G5.approval.json")
G5_FINAL_PATH = Path("results/v2/gates/G5.json")

_F6_ROOT = Path("results/v2/frontier/f6")
_PUBLIC_RESULTS_ROOT = Path("results/v2")
_MUTABLE_SYSTEM_ROOT = Path("evaluation/v2/contextlab_v2")
_PUBLIC_ARTIFACT_MAX_BYTES = 20_000_000
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}\Z")
_UTC_SECOND = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_CHANGE_KINDS = frozenset({"prompt", "route", "retrieval-rule", "verifier"})
_COST_LEDGER_PATH = "results/v2/cost/paid_calls.jsonl"
_COST_RESERVATION_API = "contextlab_v2.costs.CostLedger.reserve"
_COST_GUARD = "projected_within_external_key_limit-must-be-true-before-send"
_FORBIDDEN_F6_TARGET_NAMES = frozenset(
    {
        "baseline.py",
        "boundaries.py",
        "costs.py",
        "credentials.py",
        "frontier.py",
        "g4_gate.py",
        "g5_gate.py",
        "gates.py",
        "immutable_io.py",
    }
)
_FORBIDDEN_PUBLIC_PATH_TOKENS = frozenset(
    {
        "sealed",
        "protected",
        "evaluation_only",
        "canonical_fact_ledger",
        "gold",
        "grade",
        "scoring",
    }
)
_STATIC_CONTROLS = {
    "proposal_actor_kind": "ai-agent",
    "proposal_actor_may_approve": False,
    "human_approver": "Kevin Araujo",
    "separate_human_approval_required": True,
    "sealed_expected_answers_readable_by_proposal_actor": False,
    "graders_and_tested_system_editable_in_same_proposal": False,
    "failed_trials_deletable": False,
    "v1_baseline_modifiable": False,
}
_CHECKS = {
    "current_approved_g4": True,
    "proposal_artifact": True,
    "external_sealed_evaluation_commitment": True,
    "rollback_artifact": True,
    "explicit_budget_limit": True,
    "simpler_baseline": True,
    "protected_boundary": True,
    "no_self_approval": True,
}


class G5GateError(ValueError):
    """G5 evidence, persistence, or approval is missing, unsafe, or stale."""


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise G5GateError(f"{label} must be a lowercase SHA-256")
    return value


def _valid_artifact_hash(value: Mapping[str, Any]) -> bool:
    return value.get("artifact_sha256") == sha256_json(
        {key: item for key, item in value.items() if key != "artifact_sha256"}
    )


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise G5GateError(f"{label} must be a stable identifier")
    return value


def _identity_key(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _kevin_identity(value: str) -> bool:
    return _identity_key(value) in {"kevin", "kevinaraujo"}


def _text_identifier(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > 128
        or any(character in value for character in "\r\n\x00")
    ):
        raise G5GateError(f"{label} must be non-empty single-line text")
    return value


def _budget_amount(value: Any) -> str:
    if isinstance(value, bool) or isinstance(value, float):
        raise G5GateError("G5 budget limit must use exact decimal input")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise G5GateError("G5 budget limit must be an exact decimal") from exc
    if not amount.is_finite() or amount <= 0:
        raise G5GateError("G5 budget limit must be finite and positive")
    if amount.as_tuple().exponent < -8:
        raise G5GateError("G5 budget limit supports at most eight decimal places")
    normalized = format(amount, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def _canonical_external_path(path: Path, repository: Path) -> str:
    raw = os.fspath(path)
    candidate = Path(raw)
    if (
        not candidate.is_absolute()
        or ".." in candidate.parts
        or "\x00" in raw
        or "\\" in raw
    ):
        raise G5GateError("sealed evaluation path must be an absolute external path")
    absolute = Path(os.path.abspath(candidate))
    if raw != absolute.as_posix():
        raise G5GateError("sealed evaluation path must use canonical POSIX spelling")
    try:
        absolute.relative_to(repository)
    except ValueError:
        return absolute.as_posix()
    raise G5GateError("sealed evaluation path must stay outside the repository")


def _public_relative_path(
    repository: Path,
    path: Path,
    *,
    label: str,
    required_root: Path | None,
) -> Path:
    requested = path if path.is_absolute() else repository / path
    absolute = Path(os.path.abspath(requested))
    try:
        relative = absolute.relative_to(repository)
        if required_root is not None:
            relative.relative_to(required_root)
    except ValueError as exc:
        location = (
            f" below {required_root.as_posix()}"
            if required_root is not None
            else " inside the repository"
        )
        raise G5GateError(f"{label} must stay{location}") from exc
    value = relative.as_posix()
    if (
        not relative.parts
        or ".." in relative.parts
        or relative.parts[0] == ".git"
        or any(token in value.casefold() for token in _FORBIDDEN_PUBLIC_PATH_TOKENS)
    ):
        raise G5GateError(f"{label} is not a public artifact path")
    return relative


def _public_artifact_reference(
    repository: Path,
    path: Path,
    *,
    label: str,
    required_root: Path | None,
) -> dict[str, str]:
    relative = _public_relative_path(
        repository,
        path,
        label=label,
        required_root=required_root,
    )
    try:
        payload = read_bytes_snapshot(
            repository,
            relative,
            max_bytes=_PUBLIC_ARTIFACT_MAX_BYTES,
        )
    except ImmutableIOError as exc:
        raise G5GateError(f"{label} is missing, unsafe, or too large") from exc
    if not payload:
        raise G5GateError(f"{label} must not be empty")
    return {
        "path": relative.as_posix(),
        "file_sha256": hashlib.sha256(payload).hexdigest(),
    }


def _validate_public_reference(
    value: Any,
    *,
    label: str,
    required_root: Path | None,
) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"path", "file_sha256"}:
        raise G5GateError(f"{label} reference fields changed")
    raw_path = value.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise G5GateError(f"{label} path is invalid")
    path = Path(raw_path)
    if required_root is not None:
        try:
            path.relative_to(required_root)
        except ValueError as exc:
            raise G5GateError(
                f"{label} must stay below {required_root.as_posix()}"
            ) from exc
    if (
        path.is_absolute()
        or ".." in path.parts
        or not path.parts
        or path.parts[0] == ".git"
        or "\\" in raw_path
        or path.as_posix() != raw_path
        or any(token in raw_path.casefold() for token in _FORBIDDEN_PUBLIC_PATH_TOKENS)
    ):
        raise G5GateError(f"{label} is not a public artifact path")
    return {
        "path": raw_path,
        "file_sha256": _sha(value.get("file_sha256"), f"{label} file hash"),
    }


def _json_contract_reference(
    repository: Path,
    path: Path,
    *,
    label: str,
    required_root: Path | None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Read one public JSON contract once and bind its exact bytes."""

    relative = _public_relative_path(
        repository,
        path,
        label=label,
        required_root=required_root,
    )
    try:
        payload = read_bytes_snapshot(
            repository,
            relative,
            max_bytes=_PUBLIC_ARTIFACT_MAX_BYTES,
        )
    except ImmutableIOError as exc:
        raise G5GateError(f"{label} is missing, unsafe, or too large") from exc
    try:
        value = json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G5GateError(f"{label} must be a safe JSON object") from exc
    if not isinstance(value, dict):
        raise G5GateError(f"{label} must be a JSON object")
    return (
        {
            "path": relative.as_posix(),
            "file_sha256": hashlib.sha256(payload).hexdigest(),
        },
        value,
    )


def _self_hashed_contract(value: Mapping[str, Any], label: str) -> None:
    artifact_hash = value.get("artifact_sha256")
    if not isinstance(artifact_hash, str) or artifact_hash != sha256_json(
        {key: item for key, item in value.items() if key != "artifact_sha256"}
    ):
        raise G5GateError(f"{label} artifact hash is invalid")


def _component_ids(value: Any, label: str, *, minimum: int = 1) -> list[str]:
    if not isinstance(value, list) or len(value) < minimum:
        raise G5GateError(f"{label} must contain at least {minimum} component IDs")
    identifiers = [_identifier(item, label) for item in value]
    if identifiers != sorted(set(identifiers)):
        raise G5GateError(f"{label} must be unique and sorted")
    return identifiers


def _validate_mutable_target_reference(value: Any) -> dict[str, str]:
    reference = _validate_public_reference(
        value,
        label="F6 target artifact",
        required_root=_MUTABLE_SYSTEM_ROOT,
    )
    path = Path(reference["path"])
    if (
        path.name in _FORBIDDEN_F6_TARGET_NAMES
        or path.name.startswith("test_")
        or any(part in {"tests", "__pycache__"} for part in path.parts)
    ):
        raise G5GateError("F6 target cannot modify evaluation or gate controls")
    return reference


def _validate_budget_contract(
    value: Any, *, expected_amount: str | None = None
) -> dict[str, Any]:
    expected_fields = {
        "currency",
        "amount_usd",
        "scope",
        "ledger_path",
        "reservation_api",
        "pre_send_guard",
        "external_key_hard_limit_required",
    }
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise G5GateError("F6 proposal budget-control fields changed")
    amount = value.get("amount_usd")
    if not isinstance(amount, str) or _budget_amount(amount) != amount:
        raise G5GateError("F6 proposal budget amount is invalid")
    if expected_amount is not None and amount != expected_amount:
        raise G5GateError("F6 proposal budget differs from the exact G5 limit")
    if dict(value) != {
        "currency": "USD",
        "amount_usd": amount,
        "scope": "F6-total-paid-provider-cost",
        "ledger_path": _COST_LEDGER_PATH,
        "reservation_api": _COST_RESERVATION_API,
        "pre_send_guard": _COST_GUARD,
        "external_key_hard_limit_required": True,
    }:
        raise G5GateError("F6 proposal budget control is not enforceable")
    return dict(value)


def _validate_sealed_evaluation_contract(
    value: Any,
    *,
    proposal_actor_id: str,
    expected_external_path_sha256: str | None = None,
    expected_commitment_sha256: str | None = None,
) -> dict[str, Any]:
    fields = {
        "schema_version",
        "evaluator_id",
        "evaluator_role",
        "approval_authority",
        "proposal_actor_id",
        "proposal_actor_may_evaluate",
        "proposal_actor_may_approve",
        "external_path_sha256",
        "commitment",
        "request_schema_version",
        "receipt_schema_version",
        "artifact_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise G5GateError("F6 sealed-evaluation commitment fields changed")
    if value.get("schema_version") != F6_SEALED_EVALUATION_SCHEMA:
        raise G5GateError("F6 sealed-evaluation commitment schema changed")
    _self_hashed_contract(value, "F6 sealed-evaluation commitment")
    evaluator = _identifier(value.get("evaluator_id"), "F6 external evaluator ID")
    actor = _identifier(proposal_actor_id, "F6 proposal agent ID")
    if (
        value.get("evaluator_role") != "external-sealed-evaluator"
        or value.get("approval_authority") != "Kevin Araujo"
        or value.get("proposal_actor_id") != actor
        or value.get("proposal_actor_may_evaluate") is not False
        or value.get("proposal_actor_may_approve") is not False
        or value.get("request_schema_version")
        != "contextlab.f6-sealed-evaluation-request.v1"
        or value.get("receipt_schema_version")
        != "contextlab.f6-sealed-evaluation-receipt.v1"
    ):
        raise G5GateError("F6 evaluator and approval separation changed")
    if _identity_key(evaluator) == _identity_key(actor) or _kevin_identity(evaluator):
        raise G5GateError(
            "F6 evaluator must be separate from the proposal actor and approver"
        )
    path_hash = _sha(
        value.get("external_path_sha256"),
        "F6 external sealed-evaluation path hash",
    )
    if (
        expected_external_path_sha256 is not None
        and path_hash != expected_external_path_sha256
    ):
        raise G5GateError("F6 sealed-evaluation contract names a different path")
    commitment = value.get("commitment")
    if not isinstance(commitment, Mapping) or set(commitment) != {
        "algorithm",
        "digest",
        "committed_object_schema",
    }:
        raise G5GateError("F6 external commitment shape changed")
    digest = _sha(commitment.get("digest"), "sealed evaluation commitment")
    if (
        commitment.get("algorithm") != "sha256"
        or commitment.get("committed_object_schema")
        != "contextlab.f6-external-sealed-evaluator.v1"
    ):
        raise G5GateError("F6 external commitment shape changed")
    if expected_commitment_sha256 is not None and digest != expected_commitment_sha256:
        raise G5GateError("F6 proposal names a different sealed-evaluation commitment")
    return dict(value)


def _validate_proposal_contract(
    value: Any,
    *,
    expected_proposal_id: str | None = None,
    expected_proposal_actor_id: str | None = None,
    expected_proposed_system_id: str | None = None,
    expected_budget_amount: str | None = None,
    expected_external_path_sha256: str | None = None,
    expected_commitment_sha256: str | None = None,
) -> dict[str, Any]:
    fields = {
        "schema_version",
        "proposal_id",
        "proposal_agent_id",
        "proposed_system_id",
        "change",
        "candidate_component_ids",
        "budget_control",
        "sealed_evaluation_contract",
        "artifact_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise G5GateError("F6 proposal must use the strict public proposal schema")
    if value.get("schema_version") != F6_PROPOSAL_SCHEMA:
        raise G5GateError("F6 proposal schema changed")
    _self_hashed_contract(value, "F6 proposal")
    proposal_id = _identifier(value.get("proposal_id"), "F6 proposal ID")
    actor = _identifier(value.get("proposal_agent_id"), "F6 proposal agent ID")
    proposed_system = _text_identifier(
        value.get("proposed_system_id"), "proposed system ID"
    )
    if _kevin_identity(actor):
        raise G5GateError("the F6 proposal actor cannot approve its own proposal")
    for actual, expected, label in (
        (proposal_id, expected_proposal_id, "proposal ID"),
        (actor, expected_proposal_actor_id, "proposal actor"),
        (proposed_system, expected_proposed_system_id, "proposed system ID"),
    ):
        if expected is not None and actual != expected:
            raise G5GateError(f"F6 contract {label} differs from the G5 request")

    change = value.get("change")
    if not isinstance(change, Mapping) or set(change) != {
        "kind",
        "target_component",
        "changed_variable_count",
        "target_artifact",
        "candidate_artifact",
    }:
        raise G5GateError("F6 proposal change fields changed")
    if change.get("kind") not in _CHANGE_KINDS:
        raise G5GateError("F6 proposal change kind is not allowed")
    _identifier(change.get("target_component"), "F6 target component")
    if change.get("changed_variable_count") != 1:
        raise G5GateError("F6 proposal must contain exactly one bounded change")
    target = _validate_mutable_target_reference(change.get("target_artifact"))
    candidate = _validate_public_reference(
        change.get("candidate_artifact"),
        label="F6 candidate artifact",
        required_root=_F6_ROOT,
    )
    if (
        target["path"] == candidate["path"]
        or target["file_sha256"] == candidate["file_sha256"]
    ):
        raise G5GateError("F6 candidate must be a distinct changed artifact")
    _component_ids(
        value.get("candidate_component_ids"), "candidate components", minimum=2
    )
    _validate_budget_contract(
        value.get("budget_control"), expected_amount=expected_budget_amount
    )
    _validate_sealed_evaluation_contract(
        value.get("sealed_evaluation_contract"),
        proposal_actor_id=actor,
        expected_external_path_sha256=expected_external_path_sha256,
        expected_commitment_sha256=expected_commitment_sha256,
    )
    return dict(value)


def _validate_rollback_contract(
    value: Any, *, proposal: Mapping[str, Any]
) -> dict[str, Any]:
    fields = {
        "schema_version",
        "proposal_id",
        "proposed_system_id",
        "mechanism",
        "verification",
        "failed_trial_log_policy",
        "artifact_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise G5GateError("F6 rollback must use the strict public rollback schema")
    if value.get("schema_version") != F6_ROLLBACK_SCHEMA:
        raise G5GateError("F6 rollback schema changed")
    _self_hashed_contract(value, "F6 rollback")
    if (
        value.get("proposal_id") != proposal["proposal_id"]
        or value.get("proposed_system_id") != proposal["proposed_system_id"]
        or value.get("failed_trial_log_policy") != "append-only-preserve-all"
    ):
        raise G5GateError("F6 rollback is not bound to the exact proposal")
    mechanism = value.get("mechanism")
    if not isinstance(mechanism, Mapping) or set(mechanism) != {
        "type",
        "target_artifact",
        "expected_candidate_sha256",
        "restore_artifact",
        "restored_sha256",
        "precondition",
        "postcondition",
    }:
        raise G5GateError("F6 rollback mechanics are missing or changed")
    change = proposal["change"]
    target = _validate_mutable_target_reference(mechanism.get("target_artifact"))
    restore = _validate_public_reference(
        mechanism.get("restore_artifact"),
        label="F6 rollback restore artifact",
        required_root=_F6_ROOT,
    )
    if (
        mechanism.get("type") != "atomic-file-replace"
        or target != change["target_artifact"]
        or mechanism.get("expected_candidate_sha256")
        != change["candidate_artifact"]["file_sha256"]
        or restore["file_sha256"] != target["file_sha256"]
        or mechanism.get("restored_sha256") != target["file_sha256"]
        or mechanism.get("precondition") != "target-sha256-equals-expected-candidate"
        or mechanism.get("postcondition") != "target-sha256-equals-restored-sha256"
        or restore["path"] in {target["path"], change["candidate_artifact"]["path"]}
    ):
        raise G5GateError("F6 rollback mechanics do not restore the exact prior bytes")
    verification = value.get("verification")
    if not isinstance(verification, Mapping) or dict(verification) != {
        "type": "sha256-file-match",
        "target_path": target["path"],
        "expected_sha256": target["file_sha256"],
    }:
        raise G5GateError("F6 rollback verification is not executable")
    return dict(value)


def _validate_baseline_contract(
    value: Any,
    *,
    proposal: Mapping[str, Any],
    expected_baseline_id: str | None = None,
) -> dict[str, Any]:
    fields = {
        "schema_version",
        "baseline_id",
        "candidate_system_id",
        "relationship",
        "component_ids",
        "candidate_component_ids",
        "implementation_artifact",
        "evaluation_contract_sha256",
        "artifact_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise G5GateError("F6 baseline must use the strict public baseline schema")
    if value.get("schema_version") != F6_BASELINE_SCHEMA:
        raise G5GateError("F6 baseline schema changed")
    _self_hashed_contract(value, "F6 simpler baseline")
    baseline_id = _text_identifier(value.get("baseline_id"), "simpler baseline ID")
    if expected_baseline_id is not None and baseline_id != expected_baseline_id:
        raise G5GateError("F6 baseline ID differs from the G5 request")
    if (
        baseline_id.casefold() == proposal["proposed_system_id"].casefold()
        or value.get("candidate_system_id") != proposal["proposed_system_id"]
        or value.get("relationship") != "proper-component-subset"
    ):
        raise G5GateError("F6 simpler baseline is not distinct from the proposal")
    baseline_components = _component_ids(
        value.get("component_ids"), "baseline components"
    )
    candidate_components = _component_ids(
        value.get("candidate_component_ids"), "candidate components", minimum=2
    )
    if candidate_components != proposal["candidate_component_ids"] or not set(
        baseline_components
    ) < set(candidate_components):
        raise G5GateError("F6 baseline is not a proper component subset")
    implementation = _validate_public_reference(
        value.get("implementation_artifact"),
        label="F6 baseline implementation",
        required_root=None,
    )
    if implementation["path"] == proposal["change"]["candidate_artifact"]["path"]:
        raise G5GateError("F6 baseline and candidate implementations must be distinct")
    if (
        value.get("evaluation_contract_sha256")
        != proposal["sealed_evaluation_contract"]["artifact_sha256"]
    ):
        raise G5GateError("F6 baseline does not use the same sealed evaluator contract")
    return dict(value)


def _controls_for(proposal: Mapping[str, Any]) -> dict[str, Any]:
    sealed = proposal["sealed_evaluation_contract"]
    return {
        **_STATIC_CONTROLS,
        "proposal_actor_id": proposal["proposal_agent_id"],
        "external_evaluator_id": sealed["evaluator_id"],
        "evaluator_is_separate_from_proposal_actor": True,
        "evaluator_is_separate_from_human_approver": True,
    }


def _replay_public_reference(
    repository: Path,
    saved: Mapping[str, Any],
    *,
    label: str,
    required_root: Path | None,
) -> None:
    current = _public_artifact_reference(
        repository,
        Path(str(saved.get("path", ""))),
        label=label,
        required_root=required_root,
    )
    if current != dict(saved):
        raise G5GateError(f"{label} differs from approved G5 evidence")


def _contract_envelope(
    artifact: Mapping[str, str], contract: Mapping[str, Any]
) -> dict[str, Any]:
    return {"artifact": dict(artifact), "contract": dict(contract)}


def _protected_boundary(repository: Path) -> dict[str, str]:
    corpus_relative = Path("novalearn_synthetic_corpus/corpus")
    protected_relative = Path("novalearn_synthetic_corpus/evaluation_only_do_not_index")
    corpus = repository / corpus_relative
    protected = repository / protected_relative
    try:
        boundary = CorpusBoundary(corpus, (protected,))
        public_documents = boundary.discover(("*.md",))
    except ProtectedDataError as exc:
        raise G5GateError("approved public corpus boundary is unsafe") from exc
    if not public_documents:
        raise G5GateError("approved public corpus contains no Markdown documents")
    try:
        boundary.validate(protected / "canonical_fact_ledger.md")
    except ProtectedDataError:
        pass
    else:
        raise G5GateError("protected data was reachable through the F6 boundary")
    return {
        "public_corpus_root": corpus_relative.as_posix(),
        "protected_root": protected_relative.as_posix(),
        "direct_protected_access": "blocked",
    }


def _require_current_g4(repository: Path) -> dict[str, Any]:
    try:
        gate = load_approved_g4_gate(repository)
    except Exception as exc:
        raise G5GateError("G5 requires the current Kevin-approved G4 gate") from exc
    approval = gate.get("human_approval")
    if (
        gate.get("final_decision") != "passed"
        or not isinstance(approval, Mapping)
        or approval.get("status") != "approved"
        or approval.get("reviewer") != "Kevin Araujo"
    ):
        raise G5GateError("G5 requires the current Kevin-approved G4 gate")
    _sha(gate.get("artifact_sha256"), "G4 gate artifact hash")
    return dict(gate)


def build_g5_technical_record(
    root: Path | None = None,
    *,
    proposal_id: str,
    proposal_agent_id: str,
    proposal_artifact_path: Path,
    sealed_evaluation_path: Path,
    sealed_evaluation_commitment_sha256: str,
    rollback_artifact_path: Path,
    budget_limit_usd: str | Decimal | int,
    proposed_system_id: str,
    simpler_baseline_id: str,
    simpler_baseline_artifact_path: Path,
) -> dict[str, Any]:
    """Build current content-free G5 evidence without opening sealed data."""

    repository = (root or repository_root()).resolve()
    proposal_identifier = _identifier(proposal_id, "F6 proposal ID")
    actor = _identifier(proposal_agent_id, "F6 proposal agent ID")
    if _kevin_identity(actor):
        raise G5GateError("the F6 proposal actor cannot approve its own proposal")
    proposed_system = _text_identifier(proposed_system_id, "proposed system ID")
    baseline_id = _text_identifier(simpler_baseline_id, "simpler baseline ID")
    if proposed_system.casefold() == baseline_id.casefold():
        raise G5GateError("the F6 proposal and simpler baseline must be distinct")

    g4 = _require_current_g4(repository)
    external_path = _canonical_external_path(sealed_evaluation_path, repository)
    external_path_sha256 = hashlib.sha256(external_path.encode("utf-8")).hexdigest()
    commitment = _sha(
        sealed_evaluation_commitment_sha256,
        "sealed evaluation commitment",
    )
    budget_amount = _budget_amount(budget_limit_usd)

    proposal_artifact, proposal = _json_contract_reference(
        repository,
        proposal_artifact_path,
        label="F6 proposal artifact",
        required_root=_F6_ROOT,
    )
    proposal = _validate_proposal_contract(
        proposal,
        expected_proposal_id=proposal_identifier,
        expected_proposal_actor_id=actor,
        expected_proposed_system_id=proposed_system,
        expected_budget_amount=budget_amount,
        expected_external_path_sha256=external_path_sha256,
        expected_commitment_sha256=commitment,
    )
    _replay_public_reference(
        repository,
        proposal["change"]["target_artifact"],
        label="F6 target artifact",
        required_root=_MUTABLE_SYSTEM_ROOT,
    )
    _replay_public_reference(
        repository,
        proposal["change"]["candidate_artifact"],
        label="F6 candidate artifact",
        required_root=_F6_ROOT,
    )

    rollback_artifact, rollback = _json_contract_reference(
        repository,
        rollback_artifact_path,
        label="F6 rollback artifact",
        required_root=_F6_ROOT,
    )
    rollback = _validate_rollback_contract(rollback, proposal=proposal)
    _replay_public_reference(
        repository,
        rollback["mechanism"]["restore_artifact"],
        label="F6 rollback restore artifact",
        required_root=_F6_ROOT,
    )

    baseline_artifact, baseline = _json_contract_reference(
        repository,
        simpler_baseline_artifact_path,
        label="F6 simpler-baseline artifact",
        required_root=_PUBLIC_RESULTS_ROOT,
    )
    baseline = _validate_baseline_contract(
        baseline,
        proposal=proposal,
        expected_baseline_id=baseline_id,
    )
    _replay_public_reference(
        repository,
        baseline["implementation_artifact"],
        label="F6 baseline implementation",
        required_root=None,
    )
    contract_paths = {
        proposal_artifact["path"],
        rollback_artifact["path"],
        baseline_artifact["path"],
    }
    if len(contract_paths) != 3:
        raise G5GateError("proposal, rollback, and baseline contracts must be distinct")

    protected = _protected_boundary(repository)
    evidence: dict[str, Any] = {
        "g4_gate_artifact_sha256": g4["artifact_sha256"],
        "proposal": _contract_envelope(proposal_artifact, proposal),
        "sealed_evaluation": {
            "external_path": external_path,
            "external_path_sha256": external_path_sha256,
            "commitment_contract": proposal["sealed_evaluation_contract"],
            "repository_content_access": "forbidden",
        },
        "rollback": _contract_envelope(rollback_artifact, rollback),
        "budget_limit": proposal["budget_control"],
        "simpler_baseline": _contract_envelope(baseline_artifact, baseline),
        "protected_boundary": protected,
        "controls": _controls_for(proposal),
        "checks": dict(_CHECKS),
    }
    technical: dict[str, Any] = {
        "schema_version": G5_TECHNICAL_SCHEMA,
        "gate": "G5",
        "technical_status": "passed",
        "technical_evidence_sha256": sha256_json(evidence),
        "evidence": evidence,
    }
    technical["artifact_sha256"] = sha256_json(technical)
    validate_g5_technical_record(technical)
    return technical


def validate_g5_technical_record(value: Mapping[str, Any]) -> None:
    """Validate G5 structure and invariants without touching referenced bytes."""

    expected = {
        "schema_version",
        "gate",
        "technical_status",
        "technical_evidence_sha256",
        "evidence",
        "artifact_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise G5GateError("G5 technical record fields changed")
    evidence = value.get("evidence")
    evidence_fields = {
        "g4_gate_artifact_sha256",
        "proposal",
        "sealed_evaluation",
        "rollback",
        "budget_limit",
        "simpler_baseline",
        "protected_boundary",
        "controls",
        "checks",
    }
    if (
        value.get("schema_version") != G5_TECHNICAL_SCHEMA
        or value.get("gate") != "G5"
        or value.get("technical_status") != "passed"
        or not isinstance(evidence, Mapping)
        or set(evidence) != evidence_fields
        or value.get("technical_evidence_sha256") != sha256_json(evidence)
        or not _valid_artifact_hash(value)
    ):
        raise G5GateError("G5 technical record is invalid")
    _sha(evidence.get("g4_gate_artifact_sha256"), "G4 gate artifact hash")

    proposal_envelope = evidence.get("proposal")
    if not isinstance(proposal_envelope, Mapping) or set(proposal_envelope) != {
        "artifact",
        "contract",
    }:
        raise G5GateError("G5 proposal fields changed")
    proposal_artifact = _validate_public_reference(
        proposal_envelope.get("artifact"),
        label="F6 proposal artifact",
        required_root=_F6_ROOT,
    )
    proposal = _validate_proposal_contract(proposal_envelope.get("contract"))

    sealed = evidence.get("sealed_evaluation")
    if not isinstance(sealed, Mapping) or set(sealed) != {
        "external_path",
        "external_path_sha256",
        "commitment_contract",
        "repository_content_access",
    }:
        raise G5GateError("G5 sealed-evaluation fields changed")
    external_path = sealed.get("external_path")
    if (
        not isinstance(external_path, str)
        or not external_path.startswith("/")
        or ".." in Path(external_path).parts
        or "\\" in external_path
        or "\x00" in external_path
        or Path(external_path).as_posix() != external_path
    ):
        raise G5GateError("sealed evaluation path is invalid")
    if (
        sealed.get("external_path_sha256")
        != hashlib.sha256(external_path.encode("utf-8")).hexdigest()
    ):
        raise G5GateError("sealed evaluation path commitment changed")
    if sealed.get("repository_content_access") != "forbidden":
        raise G5GateError("sealed evaluation boundary changed")
    if sealed.get("commitment_contract") != proposal["sealed_evaluation_contract"]:
        raise G5GateError("sealed evaluation is not bound to the proposal contract")
    _validate_sealed_evaluation_contract(
        sealed.get("commitment_contract"),
        proposal_actor_id=proposal["proposal_agent_id"],
        expected_external_path_sha256=sealed["external_path_sha256"],
    )

    rollback_envelope = evidence.get("rollback")
    if not isinstance(rollback_envelope, Mapping) or set(rollback_envelope) != {
        "artifact",
        "contract",
    }:
        raise G5GateError("G5 rollback fields changed")
    rollback_artifact = _validate_public_reference(
        rollback_envelope.get("artifact"),
        label="F6 rollback artifact",
        required_root=_F6_ROOT,
    )
    _validate_rollback_contract(rollback_envelope.get("contract"), proposal=proposal)

    budget = _validate_budget_contract(evidence.get("budget_limit"))
    if budget != proposal["budget_control"]:
        raise G5GateError("G5 budget is not bound to the proposal")

    baseline_envelope = evidence.get("simpler_baseline")
    if not isinstance(baseline_envelope, Mapping) or set(baseline_envelope) != {
        "artifact",
        "contract",
    }:
        raise G5GateError("G5 simpler-baseline fields changed")
    baseline_artifact = _validate_public_reference(
        baseline_envelope.get("artifact"),
        label="F6 simpler-baseline artifact",
        required_root=_PUBLIC_RESULTS_ROOT,
    )
    _validate_baseline_contract(baseline_envelope.get("contract"), proposal=proposal)
    if (
        len(
            {
                proposal_artifact["path"],
                rollback_artifact["path"],
                baseline_artifact["path"],
            }
        )
        != 3
    ):
        raise G5GateError("proposal, rollback, and baseline contracts must be distinct")

    if evidence.get("protected_boundary") != {
        "public_corpus_root": "novalearn_synthetic_corpus/corpus",
        "protected_root": "novalearn_synthetic_corpus/evaluation_only_do_not_index",
        "direct_protected_access": "blocked",
    }:
        raise G5GateError("G5 protected boundary changed")
    if evidence.get("controls") != _controls_for(proposal):
        raise G5GateError("G5 no-self-approval controls changed")
    if evidence.get("checks") != _CHECKS:
        raise G5GateError("G5 technical checks changed")


def _json_value(root: Path, path: Path, label: str) -> dict[str, Any]:
    try:
        payload = read_bytes_snapshot(root, path)
        value = json.loads(payload.decode("utf-8", errors="strict"))
    except (ImmutableIOError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G5GateError(f"cannot read {label}") from exc
    if not isinstance(value, dict):
        raise G5GateError(f"{label} must be a JSON object")
    return value


def _replay_g5_technical_record(repository: Path, technical: Mapping[str, Any]) -> None:
    validate_g5_technical_record(technical)
    g4 = _require_current_g4(repository)
    evidence = technical["evidence"]
    if evidence["g4_gate_artifact_sha256"] != g4["artifact_sha256"]:
        raise G5GateError("G5 is bound to a different approved G4 gate")
    saved_external_path = evidence["sealed_evaluation"]["external_path"]
    if (
        _canonical_external_path(Path(saved_external_path), repository)
        != saved_external_path
    ):
        raise G5GateError("G5 sealed evaluation path is no longer valid")

    contract_specs = (
        (evidence["proposal"], "F6 proposal artifact", _F6_ROOT),
        (evidence["rollback"], "F6 rollback artifact", _F6_ROOT),
        (
            evidence["simpler_baseline"],
            "F6 simpler-baseline artifact",
            _PUBLIC_RESULTS_ROOT,
        ),
    )
    for envelope, label, required_root in contract_specs:
        artifact, contract = _json_contract_reference(
            repository,
            Path(envelope["artifact"]["path"]),
            label=label,
            required_root=required_root,
        )
        if artifact != envelope["artifact"] or contract != envelope["contract"]:
            raise G5GateError(f"{label} differs from approved G5 evidence")

    proposal = evidence["proposal"]["contract"]
    rollback = evidence["rollback"]["contract"]
    baseline = evidence["simpler_baseline"]["contract"]
    replay_specs = (
        (
            proposal["change"]["target_artifact"],
            "F6 target artifact",
            _MUTABLE_SYSTEM_ROOT,
        ),
        (
            proposal["change"]["candidate_artifact"],
            "F6 candidate artifact",
            _F6_ROOT,
        ),
        (
            rollback["mechanism"]["restore_artifact"],
            "F6 rollback restore artifact",
            _F6_ROOT,
        ),
        (
            baseline["implementation_artifact"],
            "F6 baseline implementation",
            None,
        ),
    )
    for saved, label, required_root in replay_specs:
        _replay_public_reference(
            repository,
            saved,
            label=label,
            required_root=required_root,
        )
    if _protected_boundary(repository) != evidence["protected_boundary"]:
        raise G5GateError("G5 protected boundary is no longer current")


def freeze_g5_technical_record(
    root: Path | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Create or exactly verify the canonical pending G5 technical record."""

    repository = (root or repository_root()).resolve()
    technical = build_g5_technical_record(repository, **kwargs)
    try:
        write_json_once_or_verify(repository, G5_TECHNICAL_PATH, technical)
    except ImmutableIOError as exc:
        raise G5GateError("immutable G5 technical record differs or is unsafe") from exc
    return technical


def _canonical_approved_at(value: Any) -> str:
    if not isinstance(value, str):
        raise G5GateError("G5 approval time must be whole-second UTC")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise G5GateError("G5 approval time must be whole-second UTC") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != dt.timedelta(0)
        or parsed.microsecond != 0
    ):
        raise G5GateError("G5 approval time must be whole-second UTC")
    canonical = parsed.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
    if _UTC_SECOND.fullmatch(canonical) is None:
        raise G5GateError("G5 approval time must be whole-second UTC")
    return canonical


def _approval_value(
    technical: Mapping[str, Any], *, approved_at: str
) -> dict[str, Any]:
    validate_g5_technical_record(technical)
    approval: dict[str, Any] = {
        "schema_version": G5_APPROVAL_SCHEMA,
        "gate": "G5",
        "reviewer": "Kevin Araujo",
        "reviewer_role": "sole_human_reviewer",
        "decision": "approved",
        "approved_at": _canonical_approved_at(approved_at),
        "technical_evidence_sha256": technical["technical_evidence_sha256"],
        "technical_gate_artifact_sha256": technical["artifact_sha256"],
    }
    approval["artifact_sha256"] = sha256_json(approval)
    return approval


def validate_g5_approval(
    value: Mapping[str, Any], *, technical: Mapping[str, Any]
) -> None:
    expected = _approval_value(technical, approved_at=str(value.get("approved_at")))
    if dict(value) != expected:
        raise G5GateError("G5 approval is not bound to the exact technical record")


def _final_value(
    technical: Mapping[str, Any], approval: Mapping[str, Any]
) -> dict[str, Any]:
    validate_g5_approval(approval, technical=technical)
    final: dict[str, Any] = {
        "schema_version": G5_FINAL_SCHEMA,
        "gate": "G5",
        "technical_status": "passed",
        "technical_evidence_sha256": technical["technical_evidence_sha256"],
        "technical_gate_artifact_sha256": technical["artifact_sha256"],
        "human_approval": {
            "status": "approved",
            "reviewer": "Kevin Araujo",
            "reviewer_role": "sole_human_reviewer",
            "approved_at": approval["approved_at"],
            "approval_artifact_sha256": approval["artifact_sha256"],
        },
        "final_decision": "passed",
    }
    final["artifact_sha256"] = sha256_json(final)
    return final


def validate_g5_final_gate(
    value: Mapping[str, Any],
    *,
    technical: Mapping[str, Any],
    approval: Mapping[str, Any],
) -> None:
    expected = _final_value(technical, approval)
    if dict(value) != expected:
        raise G5GateError("final G5 gate differs from technical evidence or approval")


def approve_g5_gate(
    root: Path | None = None,
    *,
    expected_technical_evidence_sha256: str,
    approved_at: str,
) -> dict[str, Any]:
    """Create Kevin's exact G5 approval and the derived final gate."""

    repository = (root or repository_root()).resolve()
    _sha(expected_technical_evidence_sha256, "expected G5 technical evidence hash")
    technical = _json_value(repository, G5_TECHNICAL_PATH, "G5 technical record")
    _replay_g5_technical_record(repository, technical)
    if technical["technical_evidence_sha256"] != expected_technical_evidence_sha256:
        raise G5GateError("G5 approval names different technical evidence")
    approval = _approval_value(technical, approved_at=approved_at)
    final = _final_value(technical, approval)
    try:
        write_json_once_or_verify(repository, G5_APPROVAL_PATH, approval)
        write_json_once_or_verify(repository, G5_FINAL_PATH, final)
    except ImmutableIOError as exc:
        raise G5GateError("immutable G5 approval or final gate differs") from exc
    return final


def load_approved_g5_gate(root: Path | None = None) -> dict[str, Any]:
    """Replay current G4, public evidence, Kevin approval, and final G5."""

    repository = (root or repository_root()).resolve()
    technical = _json_value(repository, G5_TECHNICAL_PATH, "G5 technical record")
    approval = _json_value(repository, G5_APPROVAL_PATH, "G5 Kevin approval")
    final = _json_value(repository, G5_FINAL_PATH, "final G5 gate")
    _replay_g5_technical_record(repository, technical)
    validate_g5_approval(approval, technical=technical)
    validate_g5_final_gate(final, technical=technical, approval=approval)
    return final
