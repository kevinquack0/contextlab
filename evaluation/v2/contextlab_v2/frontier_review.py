"""Exact dual-AI and Kevin review gates for completed frontier probes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .baseline import repository_root
from .frontier import FRONTIER_ENTRY_APPROVED_GATE_PATH, _write_immutable_plan
from .immutable_io import ImmutableIOError
from .review_invocations import native_review_paths
from .tasking import sha256_json


FRONTIER_RESULT_TECHNICAL_SCHEMA = "contextlab.frontier-result-technical.v1"
FRONTIER_RESULT_INVOCATION_SCHEMA = "contextlab.frontier-review-invocation.v2"
FRONTIER_RESULT_AI_REVIEW_SCHEMA = "contextlab.frontier-result-ai-review.v1"
FRONTIER_RESULT_GATE_SCHEMA = "contextlab.frontier-result-gate.v1"
FRONTIER_RESULT_APPROVAL_SCHEMA = "contextlab.frontier-result-approval.v1"
FRONTIER_RESULT_REVIEW_DIRECTORY = "reviews"
_REVIEW_DIRECTORY_BY_EXPERIMENT = {
    "F3": "reviews-attempt-04",
    "F5": "reviews-attempt-04",
}

_EXPERIMENTS = ("F1", "F2", "F3", "F5")
_PRIMARY_REVIEWERS = ("gpt-5.6-sol-high", "claude-opus-5-medium")
_FALLBACK_REVIEWERS = ("gpt-5.6-sol-high", "gpt-5.6-terra-high")
_REVIEWER_PAIRS = (_PRIMARY_REVIEWERS, _FALLBACK_REVIEWERS)
_IDENTITIES = {
    "gpt-5.6-sol-high": ("fresh Codex subagent", "gpt-5.6-sol", "high"),
    "gpt-5.6-terra-high": ("fresh Codex subagent", "gpt-5.6-terra", "high"),
    "claude-opus-5-medium": ("local Claude CLI", "claude-opus-5", "medium"),
}
_INVOCATION_SOURCES = {
    "gpt-5.6-sol-high": "codex-subagent",
    "gpt-5.6-terra-high": "codex-subagent",
    "claude-opus-5-medium": "claude-cli",
}
_PROPOSED_DECISIONS = {"promote", "accept-negative"}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_UTC_SECOND = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_INVOCATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{7,255}\Z")
_FORBIDDEN_PATH_TOKENS = {
    "sealed",
    "protected",
    "evaluation_only",
    "canonical_fact_ledger",
    "gold",
}


class FrontierResultReviewError(ValueError):
    """A frontier result or its review evidence is incomplete or altered."""


def frontier_result_review_directory(experiment_id: str) -> str:
    """Return the current immutable review-attempt directory for one result."""

    identifier = _experiment(experiment_id)
    return _REVIEW_DIRECTORY_BY_EXPERIMENT.get(
        identifier, FRONTIER_RESULT_REVIEW_DIRECTORY
    )


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise FrontierResultReviewError(f"{label} must be lowercase SHA-256")
    return value


def _utc(value: Any, label: str) -> str:
    if not isinstance(value, str) or _UTC_SECOND.fullmatch(value) is None:
        raise FrontierResultReviewError(f"{label} must be UTC to the second")
    return value


def _experiment(value: Any) -> str:
    if value not in _EXPERIMENTS:
        raise FrontierResultReviewError("frontier result review supports only F1-F3 and F5")
    return str(value)


def _artifact_hash_valid(value: Mapping[str, Any]) -> bool:
    return value.get("artifact_sha256") == sha256_json(
        {key: item for key, item in value.items() if key != "artifact_sha256"}
    )


def _source_artifact(value: Any, experiment_id: str, index: int) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {
        "path",
        "file_sha256",
        "artifact_sha256",
    }:
        raise FrontierResultReviewError(
            f"{experiment_id} source artifact {index} fields changed"
        )
    path_value = value.get("path")
    if not isinstance(path_value, str) or not path_value:
        raise FrontierResultReviewError("frontier result source path is invalid")
    path = Path(path_value)
    required_root = Path("results/v2/frontier") / experiment_id.casefold()
    try:
        path.relative_to(required_root)
    except ValueError as exc:
        raise FrontierResultReviewError(
            f"{experiment_id} source artifact is outside its public result root"
        ) from exc
    lowered = path_value.casefold()
    if (
        path.is_absolute()
        or ".." in path.parts
        or "\\" in path_value
        or path.as_posix() != path_value
        or any(token in lowered for token in _FORBIDDEN_PATH_TOKENS)
    ):
        raise FrontierResultReviewError("frontier result source is not public")
    return {
        "path": path.as_posix(),
        "file_sha256": _sha(value.get("file_sha256"), "source file hash"),
        "artifact_sha256": _sha(value.get("artifact_sha256"), "source artifact hash"),
    }


def build_frontier_result_technical_record(
    *,
    experiment_id: str,
    frontier_entry_gate_sha256: str,
    source_artifacts: Sequence[Mapping[str, Any]],
    proposed_decision: str,
    claim: str,
) -> dict[str, Any]:
    """Build one content-free result claim bound to exact public artifacts."""

    identifier = _experiment(experiment_id)
    if isinstance(source_artifacts, (str, bytes)) or not isinstance(
        source_artifacts, Sequence
    ):
        raise FrontierResultReviewError("frontier result sources must be a list")
    sources = [
        _source_artifact(value, identifier, index)
        for index, value in enumerate(source_artifacts)
    ]
    if not sources or len({row["path"] for row in sources}) != len(sources):
        raise FrontierResultReviewError(
            "frontier result needs unique public source artifacts"
        )
    if proposed_decision not in _PROPOSED_DECISIONS:
        raise FrontierResultReviewError("frontier proposed decision is invalid")
    if not isinstance(claim, str) or not claim.strip() or len(claim) > 2_000:
        raise FrontierResultReviewError("frontier result claim is invalid")
    technical: dict[str, Any] = {
        "schema_version": FRONTIER_RESULT_TECHNICAL_SCHEMA,
        "experiment_id": identifier,
        "frontier_entry_gate_sha256": _sha(
            frontier_entry_gate_sha256, "frontier entry gate hash"
        ),
        "source_artifacts": sources,
        "source_artifacts_sha256": sha256_json(sources),
        "proposed_decision": proposed_decision,
        "claim": claim.strip(),
        "review_status": "pending-ai-review",
    }
    technical["technical_record_sha256"] = sha256_json(technical)
    technical["artifact_sha256"] = sha256_json(technical)
    validate_frontier_result_technical_record(technical)
    return technical


def validate_frontier_result_technical_record(value: Mapping[str, Any]) -> None:
    expected = {
        "schema_version",
        "experiment_id",
        "frontier_entry_gate_sha256",
        "source_artifacts",
        "source_artifacts_sha256",
        "proposed_decision",
        "claim",
        "review_status",
        "technical_record_sha256",
        "artifact_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise FrontierResultReviewError("frontier technical record fields changed")
    identifier = _experiment(value.get("experiment_id"))
    if value.get("schema_version") != FRONTIER_RESULT_TECHNICAL_SCHEMA:
        raise FrontierResultReviewError("unsupported frontier technical schema")
    sources = value.get("source_artifacts")
    if not isinstance(sources, list) or not sources:
        raise FrontierResultReviewError("frontier technical sources are missing")
    normalized = [
        _source_artifact(source, identifier, index)
        for index, source in enumerate(sources)
    ]
    if normalized != sources or value.get("source_artifacts_sha256") != sha256_json(
        normalized
    ):
        raise FrontierResultReviewError("frontier technical sources changed")
    _sha(value.get("frontier_entry_gate_sha256"), "frontier entry gate hash")
    if value.get("proposed_decision") not in _PROPOSED_DECISIONS:
        raise FrontierResultReviewError("frontier proposed decision changed")
    if (
        not isinstance(value.get("claim"), str)
        or not value["claim"].strip()
        or value.get("review_status") != "pending-ai-review"
    ):
        raise FrontierResultReviewError("frontier technical claim or status changed")
    body = {
        key: item
        for key, item in value.items()
        if key not in {"technical_record_sha256", "artifact_sha256"}
    }
    if value.get("technical_record_sha256") != sha256_json(body):
        raise FrontierResultReviewError("frontier technical-record hash changed")
    if not _artifact_hash_valid(value):
        raise FrontierResultReviewError("frontier technical artifact hash changed")


def _review_payload(
    technical: Mapping[str, Any],
    *,
    reviewer_id: str,
    decision: str,
    findings: Sequence[str],
    completed_at: str,
) -> dict[str, Any]:
    validate_frontier_result_technical_record(technical)
    identity = _IDENTITIES.get(reviewer_id)
    if identity is None:
        raise FrontierResultReviewError("unknown frontier AI reviewer identity")
    if decision not in {"pass", "fail"}:
        raise FrontierResultReviewError("frontier AI review decision is invalid")
    if isinstance(findings, (str, bytes)) or not isinstance(findings, Sequence):
        raise FrontierResultReviewError("frontier AI review findings must be a list")
    normalized = []
    for finding in findings:
        if not isinstance(finding, str) or not finding.strip() or len(finding) > 2_000:
            raise FrontierResultReviewError("frontier AI review finding is invalid")
        normalized.append(finding.strip())
    if decision == "pass" and normalized:
        raise FrontierResultReviewError("passing frontier AI review has findings")
    return {
        "experiment_id": technical["experiment_id"],
        "technical_record_sha256": technical["technical_record_sha256"],
        "reviewer_id": reviewer_id,
        "model_id": identity[1],
        "reasoning_effort": identity[2],
        "decision": decision,
        "findings": normalized,
        "completed_at": _utc(completed_at, "frontier AI completion time"),
    }


def review_payload_sha256(
    technical: Mapping[str, Any],
    *,
    reviewer_id: str,
    decision: str,
    findings: Sequence[str],
    completed_at: str,
) -> str:
    """Return the exact payload hash an outer invocation receipt must bind."""

    return sha256_json(
        _review_payload(
            technical,
            reviewer_id=reviewer_id,
            decision=decision,
            findings=findings,
            completed_at=completed_at,
        )
    )


def _review_anchor_path(experiment_id: str, reviewer_id: str) -> Path:
    return (
        Path("results/v2/frontier")
        / _experiment(experiment_id).casefold()
        / frontier_result_review_directory(experiment_id)
        / f"{reviewer_id}.json"
    )


def _canonical_reviewer_pair(reviews: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    if isinstance(reviews, (str, bytes)) or not isinstance(reviews, Sequence):
        raise FrontierResultReviewError("frontier result AI reviews must be a list")
    reviewer_ids = [
        str(review.get("reviewer_id"))
        for review in reviews
        if isinstance(review, Mapping)
    ]
    for pair in _REVIEWER_PAIRS:
        if len(reviews) == len(pair) and set(reviewer_ids) == set(pair):
            return pair
    raise FrontierResultReviewError(
        "frontier result needs Sol plus Claude, or Sol plus Terra when Claude is unavailable"
    )


def _reviewer_pair_from_gate(value: Mapping[str, Any]) -> tuple[str, str]:
    commitments = value.get("ai_reviews")
    if not isinstance(commitments, list):
        raise FrontierResultReviewError("frontier result gate AI reviews are missing")
    reviewer_ids = tuple(
        str(commitment.get("reviewer_id"))
        for commitment in commitments
        if isinstance(commitment, Mapping)
    )
    if reviewer_ids not in _REVIEWER_PAIRS:
        raise FrontierResultReviewError("frontier result reviewer pair changed")
    return reviewer_ids


def build_frontier_result_invocation_receipt(
    technical: Mapping[str, Any],
    *,
    reviewer_id: str,
    invocation_id: str,
    decision: str,
    findings: Sequence[str],
    completed_at: str,
    native_invocation_evidence_sha256: str,
    native_output_sha256: str,
    usage: Mapping[str, int] | None,
) -> dict[str, Any]:
    """Build one result-review receipt only from native execution evidence."""

    validate_frontier_result_technical_record(technical)
    identity = _IDENTITIES.get(reviewer_id)
    if identity is None:
        raise FrontierResultReviewError("unknown frontier AI reviewer identity")
    if (
        not isinstance(invocation_id, str)
        or _INVOCATION_ID.fullmatch(invocation_id) is None
    ):
        raise FrontierResultReviewError("frontier native invocation ID is invalid")
    completed = _utc(completed_at, "frontier invocation completion time")
    payload_hash = review_payload_sha256(
        technical,
        reviewer_id=reviewer_id,
        decision=decision,
        findings=findings,
        completed_at=completed,
    )
    normalized_usage: dict[str, int] | None
    if usage is None:
        normalized_usage = None
    else:
        normalized_usage = dict(sorted(usage.items()))
        if not normalized_usage or any(
            not isinstance(key, str)
            or not key
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for key, value in normalized_usage.items()
        ):
            raise FrontierResultReviewError("frontier invocation usage is invalid")
    anchor = _review_anchor_path(str(technical["experiment_id"]), reviewer_id)
    receipt: dict[str, Any] = {
        "schema_version": FRONTIER_RESULT_INVOCATION_SCHEMA,
        "reviewer_id": reviewer_id,
        "invocation": identity[0],
        "invocation_source": _INVOCATION_SOURCES[reviewer_id],
        "invocation_id": invocation_id,
        "requested_model": identity[1],
        "resolved_model": identity[1],
        "reasoning_effort": identity[2],
        "status": "completed",
        "completed_at": completed,
        "technical_record_sha256": technical["technical_record_sha256"],
        "review_payload_sha256": payload_hash,
        "native_invocation_evidence_path": native_review_paths(anchor)[0].as_posix(),
        "native_invocation_evidence_sha256": _sha(
            native_invocation_evidence_sha256,
            "frontier native invocation evidence hash",
        ),
        "native_output_sha256": _sha(
            native_output_sha256, "frontier native invocation output hash"
        ),
        "usage_available": normalized_usage is not None,
        "usage": normalized_usage,
    }
    receipt["artifact_sha256"] = sha256_json(receipt)
    _validate_invocation_receipt(
        receipt,
        technical=technical,
        reviewer_id=reviewer_id,
        payload_sha256=payload_hash,
    )
    return receipt


def _validate_invocation_receipt(
    value: Any,
    *,
    technical: Mapping[str, Any],
    reviewer_id: str,
    payload_sha256: str,
) -> dict[str, Any]:
    expected = {
        "schema_version",
        "reviewer_id",
        "invocation",
        "invocation_source",
        "invocation_id",
        "requested_model",
        "resolved_model",
        "reasoning_effort",
        "status",
        "completed_at",
        "technical_record_sha256",
        "review_payload_sha256",
        "native_invocation_evidence_path",
        "native_invocation_evidence_sha256",
        "native_output_sha256",
        "usage_available",
        "usage",
        "artifact_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise FrontierResultReviewError("frontier invocation receipt fields changed")
    identity = _IDENTITIES[reviewer_id]
    if (
        value.get("schema_version") != FRONTIER_RESULT_INVOCATION_SCHEMA
        or value.get("reviewer_id") != reviewer_id
        or value.get("invocation") != identity[0]
        or value.get("invocation_source") != _INVOCATION_SOURCES[reviewer_id]
        or value.get("requested_model") != identity[1]
        or value.get("resolved_model") != identity[1]
        or value.get("reasoning_effort") != identity[2]
        or value.get("status") != "completed"
    ):
        raise FrontierResultReviewError("frontier invocation identity changed")
    invocation_id = value.get("invocation_id")
    if (
        not isinstance(invocation_id, str)
        or _INVOCATION_ID.fullmatch(invocation_id) is None
    ):
        raise FrontierResultReviewError("frontier native invocation ID is invalid")
    if (
        value.get("technical_record_sha256") != technical["technical_record_sha256"]
        or value.get("review_payload_sha256") != payload_sha256
    ):
        raise FrontierResultReviewError(
            "frontier invocation technical or payload binding changed"
        )
    _utc(value.get("completed_at"), "frontier invocation completion time")
    anchor = _review_anchor_path(str(technical["experiment_id"]), reviewer_id)
    if (
        value.get("native_invocation_evidence_path")
        != native_review_paths(anchor)[0].as_posix()
    ):
        raise FrontierResultReviewError("frontier native invocation path changed")
    _sha(
        value.get("native_invocation_evidence_sha256"),
        "frontier native invocation evidence hash",
    )
    _sha(value.get("native_output_sha256"), "frontier native invocation output hash")
    usage_available = value.get("usage_available")
    usage = value.get("usage")
    if usage_available is False:
        if usage is not None:
            raise FrontierResultReviewError("unavailable frontier usage must be null")
    elif usage_available is True:
        if not isinstance(usage, Mapping) or not usage:
            raise FrontierResultReviewError("available frontier usage is missing")
        if any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in usage.values()
        ):
            raise FrontierResultReviewError("frontier invocation usage is invalid")
    else:
        raise FrontierResultReviewError("frontier usage availability is invalid")
    if not _artifact_hash_valid(value):
        raise FrontierResultReviewError("frontier invocation receipt hash changed")
    return dict(value)


def build_frontier_result_ai_review(
    technical: Mapping[str, Any],
    *,
    reviewer_id: str,
    invocation_receipt: Mapping[str, Any],
    decision: str,
    findings: Sequence[str],
    completed_at: str,
) -> dict[str, Any]:
    """Build one AI review only when its outer invocation proves exact identity."""

    payload = _review_payload(
        technical,
        reviewer_id=reviewer_id,
        decision=decision,
        findings=findings,
        completed_at=completed_at,
    )
    receipt = _validate_invocation_receipt(
        invocation_receipt,
        technical=technical,
        reviewer_id=reviewer_id,
        payload_sha256=sha256_json(payload),
    )
    if receipt["completed_at"] != payload["completed_at"]:
        raise FrontierResultReviewError(
            "frontier invocation and review completion times differ"
        )
    review: dict[str, Any] = {
        "schema_version": FRONTIER_RESULT_AI_REVIEW_SCHEMA,
        **payload,
        "invocation_receipt": receipt,
    }
    review["artifact_sha256"] = sha256_json(review)
    validate_frontier_result_ai_review(review, technical=technical)
    return review


def validate_frontier_result_ai_review(
    value: Mapping[str, Any], *, technical: Mapping[str, Any]
) -> None:
    expected = {
        "schema_version",
        "experiment_id",
        "technical_record_sha256",
        "reviewer_id",
        "model_id",
        "reasoning_effort",
        "decision",
        "findings",
        "completed_at",
        "invocation_receipt",
        "artifact_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise FrontierResultReviewError("frontier AI review fields changed")
    if value.get("schema_version") != FRONTIER_RESULT_AI_REVIEW_SCHEMA:
        raise FrontierResultReviewError("unsupported frontier AI review schema")
    payload = _review_payload(
        technical,
        reviewer_id=str(value.get("reviewer_id")),
        decision=str(value.get("decision")),
        findings=value.get("findings")
        if isinstance(value.get("findings"), list)
        else [],
        completed_at=str(value.get("completed_at")),
    )
    for key, item in payload.items():
        if value.get(key) != item:
            raise FrontierResultReviewError("frontier AI review payload changed")
    receipt = _validate_invocation_receipt(
        value.get("invocation_receipt"),
        technical=technical,
        reviewer_id=payload["reviewer_id"],
        payload_sha256=sha256_json(payload),
    )
    if receipt["completed_at"] != payload["completed_at"]:
        raise FrontierResultReviewError("frontier AI review timing changed")
    if not _artifact_hash_valid(value):
        raise FrontierResultReviewError("frontier AI review hash changed")


def validate_frontier_result_ai_review_provenance(
    root: Path,
    *,
    technical: Mapping[str, Any],
    review: Mapping[str, Any],
) -> None:
    """Replay the native execution that produced one frontier-result review."""

    from .review_invocations import (
        AIReviewInvocationError,
        assert_native_proof_fields,
        validate_recorded_ai_review,
    )

    validate_frontier_result_ai_review(review, technical=technical)
    reviewer_id = str(review["reviewer_id"])
    receipt = review["invocation_receipt"]
    if not isinstance(receipt, Mapping):
        raise FrontierResultReviewError("frontier invocation receipt is missing")
    anchor = _review_anchor_path(str(technical["experiment_id"]), reviewer_id)
    bindings = {
        "experiment_id": str(technical["experiment_id"]),
        "frontier_entry_gate_path": FRONTIER_ENTRY_APPROVED_GATE_PATH.as_posix(),
        "technical_record_path": (
            Path("results/v2/frontier")
            / str(technical["experiment_id"]).casefold()
            / frontier_result_review_directory(str(technical["experiment_id"]))
            / "technical.json"
        ).as_posix(),
        "technical_record_sha256": str(technical["technical_record_sha256"]),
        "technical_artifact_sha256": str(technical["artifact_sha256"]),
        "frontier_entry_gate_sha256": str(technical["frontier_entry_gate_sha256"]),
        "source_artifacts_sha256": str(technical["source_artifacts_sha256"]),
    }
    try:
        native = validate_recorded_ai_review(
            root,
            anchor_path=anchor,
            reviewer_id=reviewer_id,
            review_kind="frontier-result",
            target_bindings=bindings,
            expected_response={
                "decision": review["decision"],
                "findings": review["findings"],
            },
            invocation_id=str(receipt["invocation_id"]),
            completed_at=str(receipt["completed_at"]),
        )
        assert_native_proof_fields(anchor_path=anchor, receipt=receipt, evidence=native)
    except AIReviewInvocationError as exc:
        raise FrontierResultReviewError(
            "frontier AI review lacks valid native execution proof"
        ) from exc


def _frontier_result_pending_gate_value(
    technical: Mapping[str, Any], reviews: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    validate_frontier_result_technical_record(technical)
    reviewer_pair = _canonical_reviewer_pair(reviews)
    by_reviewer = {
        str(review.get("reviewer_id")): review
        for review in reviews
        if isinstance(review, Mapping)
    }
    commitments = []
    decisions = []
    for reviewer_id in reviewer_pair:
        review = by_reviewer[reviewer_id]
        validate_frontier_result_ai_review(review, technical=technical)
        decisions.append(review["decision"])
        commitments.append(
            {
                "reviewer_id": reviewer_id,
                "review_artifact_sha256": review["artifact_sha256"],
                "invocation_receipt_sha256": review["invocation_receipt"][
                    "artifact_sha256"
                ],
                "decision": review["decision"],
                "findings": review["findings"],
            }
        )
    passed = decisions == ["pass", "pass"]
    technical_status = "passed" if passed else "failed"
    human_approval = {
        "status": "pending" if passed else "unavailable",
        "reviewer": "Kevin Araujo",
        "technical_record_sha256": technical["technical_record_sha256"],
    }
    gate: dict[str, Any] = {
        "schema_version": FRONTIER_RESULT_GATE_SCHEMA,
        "experiment_id": technical["experiment_id"],
        "technical_record_sha256": technical["technical_record_sha256"],
        "technical_artifact_sha256": technical["artifact_sha256"],
        "proposed_decision": technical["proposed_decision"],
        "ai_reviews": commitments,
        "technical_status": technical_status,
        "human_approval": human_approval,
        "final_status": (
            "blocked-pending-kevin" if passed else "blocked-revision-required"
        ),
    }
    gate["artifact_sha256"] = sha256_json(gate)
    return gate


def build_frontier_result_pending_gate(
    technical: Mapping[str, Any], reviews: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Combine the exact two AI reviews while keeping Kevin separate."""

    gate = _frontier_result_pending_gate_value(technical, reviews)
    validate_frontier_result_gate(gate, technical=technical, reviews=reviews)
    return gate


def validate_frontier_result_gate(
    value: Mapping[str, Any],
    *,
    technical: Mapping[str, Any],
    reviews: Sequence[Mapping[str, Any]],
) -> None:
    expected = _frontier_result_pending_gate_value(technical, reviews)
    if dict(value) != expected:
        raise FrontierResultReviewError("frontier result pending gate changed")


def _review_root(root: Path, experiment_id: str) -> Path:
    return (
        root.resolve()
        / "results/v2/frontier"
        / _experiment(experiment_id).casefold()
        / frontier_result_review_directory(experiment_id)
    )


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _safe_create_parent(repository: Path, path: Path) -> None:
    """Preflight existing result parents without creating through pathnames."""

    root = repository.resolve()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise FrontierResultReviewError(
            "frontier review artifact is outside the repository"
        ) from exc
    cursor = root
    for part in relative.parent.parts:
        cursor = cursor / part
        if cursor.exists() or cursor.is_symlink():
            if cursor.is_symlink() or not cursor.is_dir():
                raise FrontierResultReviewError(
                    "frontier review path is not a safe repository directory"
                )
            continue
        break


def _write_many_create_only(*, repository: Path, plan: Mapping[Path, bytes]) -> None:
    for path in plan:
        _safe_create_parent(repository, path)
    try:
        _write_immutable_plan(repository, plan)
    except ImmutableIOError as exc:
        raise FrontierResultReviewError(
            "immutable frontier review artifact differs or is unsafe"
        ) from exc


def write_frontier_result_technical_record(
    root: Path | None, value: Mapping[str, Any]
) -> Path:
    validate_frontier_result_technical_record(value)
    repository = (root or repository_root()).resolve()
    path = _review_root(repository, str(value["experiment_id"])) / ("technical.json")
    _write_many_create_only(repository=repository, plan={path: _json_bytes(value)})
    return path


def write_frontier_result_ai_review(
    root: Path | None, value: Mapping[str, Any]
) -> Path:
    identifier = _experiment(value.get("experiment_id"))
    repository = (root or repository_root()).resolve()
    technical = _read_json(
        _review_root(repository, identifier) / "technical.json",
        "frontier technical result",
    )
    validate_frontier_result_ai_review(value, technical=technical)
    validate_frontier_result_ai_review_provenance(
        repository, technical=technical, review=value
    )
    reviewer_id = str(value["reviewer_id"])
    path = _review_root(repository, identifier) / f"{reviewer_id}.json"
    _write_many_create_only(repository=repository, plan={path: _json_bytes(value)})
    return path


def write_frontier_result_pending_gate(
    root: Path | None, value: Mapping[str, Any]
) -> Path:
    identifier = _experiment(value.get("experiment_id"))
    repository = (root or repository_root()).resolve()
    review_root = _review_root(repository, identifier)
    technical = _read_json(review_root / "technical.json", "frontier technical result")
    reviewer_pair = _reviewer_pair_from_gate(value)
    reviews = [
        _read_json(review_root / f"{reviewer}.json", f"{reviewer} frontier review")
        for reviewer in reviewer_pair
    ]
    for review in reviews:
        validate_frontier_result_ai_review_provenance(
            repository, technical=technical, review=review
        )
    validate_frontier_result_gate(value, technical=technical, reviews=reviews)
    path = review_root / "pending.json"
    _write_many_create_only(repository=repository, plan={path: _json_bytes(value)})
    return path


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise FrontierResultReviewError(f"{label} is missing or unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrontierResultReviewError(f"cannot read {label}") from exc
    if not isinstance(value, dict):
        raise FrontierResultReviewError(f"{label} must be an object")
    return value


def _read_current_public_source(
    repository: Path, source: Mapping[str, Any], *, experiment_id: str, index: int
) -> None:
    """Replay one reviewed source reference from current bytes, never its path alone."""

    reference = _source_artifact(source, experiment_id, index)
    relative = Path(reference["path"])
    cursor = repository.resolve()
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise FrontierResultReviewError(
                "frontier result source artifact is missing or unsafe"
            )
    path = repository / relative
    if not path.is_file() or path.is_symlink():
        raise FrontierResultReviewError(
            "frontier result source artifact is missing or unsafe"
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise FrontierResultReviewError(
            "frontier result source artifact is missing or unsafe"
        ) from exc
    if hashlib.sha256(raw).hexdigest() != reference["file_sha256"]:
        raise FrontierResultReviewError("frontier result source file hash changed")
    try:
        artifact = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrontierResultReviewError(
            "frontier result source artifact must contain UTF-8 JSON"
        ) from exc
    if not isinstance(artifact, Mapping):
        raise FrontierResultReviewError(
            "frontier result source artifact must be an object"
        )
    if artifact.get("artifact_sha256") != reference["artifact_sha256"]:
        raise FrontierResultReviewError("frontier result source internal hash changed")
    if not _artifact_hash_valid(artifact):
        raise FrontierResultReviewError("frontier result source internal hash changed")


def _require_current_frontier_entry_and_sources(
    repository: Path, technical: Mapping[str, Any]
) -> None:
    """Require the result to remain attached to the currently approved entry gate."""

    validate_frontier_result_technical_record(technical)
    try:
        from .frontier import (
            load_approved_frontier_entry_gate,
            require_frontier_experiment_approved,
        )

        entry = load_approved_frontier_entry_gate(repository)
        require_frontier_experiment_approved(entry, str(technical["experiment_id"]))
    except Exception as exc:
        raise FrontierResultReviewError(
            "current approved frontier entry gate is required"
        ) from exc
    if entry.get("artifact_sha256") != technical["frontier_entry_gate_sha256"]:
        raise FrontierResultReviewError(
            "frontier result is bound to a different current entry gate"
        )
    for index, source in enumerate(technical["source_artifacts"]):
        _read_current_public_source(
            repository,
            source,
            experiment_id=str(technical["experiment_id"]),
            index=index,
        )


def _frontier_result_approval_value(
    technical: Mapping[str, Any],
    pending: Mapping[str, Any],
    *,
    approved_at: str,
) -> dict[str, Any]:
    approval: dict[str, Any] = {
        "schema_version": FRONTIER_RESULT_APPROVAL_SCHEMA,
        "experiment_id": technical["experiment_id"],
        "technical_record_sha256": technical["technical_record_sha256"],
        "pending_gate_artifact_sha256": pending["artifact_sha256"],
        "reviewer": "Kevin Araujo",
        "reviewer_role": "sole_human_reviewer",
        "decision": technical["proposed_decision"],
        "approved_at": _utc(approved_at, "frontier result approval time"),
    }
    approval["artifact_sha256"] = sha256_json(approval)
    return approval


def _frontier_result_final_value(
    technical: Mapping[str, Any],
    pending: Mapping[str, Any],
    approval: Mapping[str, Any],
) -> dict[str, Any]:
    final: dict[str, Any] = {
        key: item
        for key, item in pending.items()
        if key not in {"human_approval", "final_status", "artifact_sha256"}
    }
    final["human_approval"] = {
        "status": "approved",
        "reviewer": "Kevin Araujo",
        "technical_record_sha256": technical["technical_record_sha256"],
        "decision": technical["proposed_decision"],
        "approved_at": approval["approved_at"],
        "approval_artifact_sha256": approval["artifact_sha256"],
    }
    final["final_status"] = (
        "promoted"
        if technical["proposed_decision"] == "promote"
        else "accepted-negative"
    )
    final["artifact_sha256"] = sha256_json(final)
    return final


def validate_frontier_result_final_gate(
    value: Mapping[str, Any],
    *,
    technical: Mapping[str, Any],
    reviews: Sequence[Mapping[str, Any]],
    pending: Mapping[str, Any],
    approval: Mapping[str, Any],
) -> None:
    """Replay a final F1-F3 or F5 result from separate AI and Kevin records."""

    validate_frontier_result_gate(pending, technical=technical, reviews=reviews)
    if pending["technical_status"] != "passed":
        raise FrontierResultReviewError("failed frontier result cannot be final")
    expected_approval = _frontier_result_approval_value(
        technical, pending, approved_at=str(approval.get("approved_at"))
    )
    if dict(approval) != expected_approval:
        raise FrontierResultReviewError("frontier Kevin approval changed")
    expected_final = _frontier_result_final_value(technical, pending, approval)
    if dict(value) != expected_final:
        raise FrontierResultReviewError("frontier final result gate changed")


def approve_frontier_result(
    root: Path | None,
    *,
    experiment_id: str,
    approved_at: str,
) -> dict[str, Any]:
    """Create Kevin's separate approval and one final result gate atomically."""

    repository = (root or repository_root()).resolve()
    identifier = _experiment(experiment_id)
    review_root = _review_root(repository, identifier)
    technical = _read_json(review_root / "technical.json", "frontier technical result")
    _require_current_frontier_entry_and_sources(repository, technical)
    pending = _read_json(review_root / "pending.json", "pending frontier result gate")
    reviewer_pair = _reviewer_pair_from_gate(pending)
    reviews = [
        _read_json(review_root / f"{reviewer}.json", f"{reviewer} frontier review")
        for reviewer in reviewer_pair
    ]
    for review in reviews:
        validate_frontier_result_ai_review_provenance(
            repository, technical=technical, review=review
        )
    validate_frontier_result_gate(pending, technical=technical, reviews=reviews)
    if (
        pending["technical_status"] != "passed"
        or pending["human_approval"]["status"] != "pending"
    ):
        raise FrontierResultReviewError(
            "Kevin cannot approve a failed frontier result review"
        )
    approval = _frontier_result_approval_value(
        technical, pending, approved_at=approved_at
    )
    final = _frontier_result_final_value(technical, pending, approval)
    validate_frontier_result_final_gate(
        final,
        technical=technical,
        reviews=reviews,
        pending=pending,
        approval=approval,
    )
    _write_many_create_only(
        repository=repository,
        plan={
            review_root / "kevin.approval.json": _json_bytes(approval),
            review_root / "final.json": _json_bytes(final),
        },
    )
    return final


def load_approved_frontier_result(
    root: Path | None, experiment_id: str
) -> dict[str, Any]:
    """Load and replay one exact Kevin-approved frontier result gate."""

    repository = (root or repository_root()).resolve()
    identifier = _experiment(experiment_id)
    review_root = _review_root(repository, identifier)
    technical = _read_json(review_root / "technical.json", "frontier technical result")
    _require_current_frontier_entry_and_sources(repository, technical)
    pending = _read_json(review_root / "pending.json", "pending frontier result gate")
    reviewer_pair = _reviewer_pair_from_gate(pending)
    reviews = [
        _read_json(review_root / f"{reviewer}.json", f"{reviewer} frontier review")
        for reviewer in reviewer_pair
    ]
    for review in reviews:
        validate_frontier_result_ai_review_provenance(
            repository, technical=technical, review=review
        )
    approval = _read_json(
        review_root / "kevin.approval.json", "Kevin frontier result approval"
    )
    final = _read_json(review_root / "final.json", "final frontier result gate")
    validate_frontier_result_final_gate(
        final,
        technical=technical,
        reviews=reviews,
        pending=pending,
        approval=approval,
    )
    return final


def _file_reference(
    root: Path, relative: Path
) -> tuple[dict[str, str], dict[str, Any]]:
    path = root / relative
    value = _read_json(path, relative.as_posix())
    internal = _sha(value.get("artifact_sha256"), f"{relative} artifact hash")
    return (
        {
            "path": relative.as_posix(),
            "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "artifact_sha256": internal,
        },
        value,
    )


def freeze_frontier_result_technical_record(
    root: Path | None = None,
    *,
    experiment_id: str,
    proposed_decision: str,
    claim: str,
) -> dict[str, Any]:
    """Replay exact saved F1-F3 or F5 artifacts and freeze their review claim."""

    repository = (root or repository_root()).resolve()
    identifier = _experiment(experiment_id)
    from .frontier import (
        load_approved_frontier_entry_gate,
        require_frontier_experiment_approved,
    )

    entry = load_approved_frontier_entry_gate(repository)
    require_frontier_experiment_approved(entry, identifier)
    refs: list[dict[str, str]] = []
    if identifier == "F1":
        from .frontier_f1 import (
            F1_GENERATED_LAB_PATH,
            F1_PREPARED_LAB_PATH,
            validate_f1_indexed_memory_lab,
        )

        prepared_ref, prepared = _file_reference(repository, F1_PREPARED_LAB_PATH)
        generated_ref, generated = _file_reference(repository, F1_GENERATED_LAB_PATH)
        validate_f1_indexed_memory_lab(prepared, require_generated=False)
        validate_f1_indexed_memory_lab(generated, require_generated=True)
        if generated["prepared_lab_sha256"] != prepared["artifact_sha256"]:
            raise FrontierResultReviewError("F1 result preparation binding changed")
        refs.extend((prepared_ref, generated_ref))
    elif identifier == "F2":
        from .frontier_f2 import (
            F2_CANDIDATE_PATH,
            F2_FREEZE_PATH,
            F2_SCORE_PATH,
            validate_f2_candidate,
            validate_f2_candidate_paid_ledger,
            validate_f2_freeze,
            validate_f2_score,
        )

        freeze_ref, freeze = _file_reference(repository, F2_FREEZE_PATH)
        candidate_ref, candidate = _file_reference(repository, F2_CANDIDATE_PATH)
        score_ref, score = _file_reference(repository, F2_SCORE_PATH)
        validate_f2_freeze(freeze)
        validate_f2_candidate(candidate, freeze)
        validate_f2_score(score, freeze=freeze, candidate=candidate)
        validate_f2_candidate_paid_ledger(repository, candidate)
        refs.extend((freeze_ref, candidate_ref, score_ref))
    elif identifier == "F3":
        from .frontier_f3 import (
            F3_FINAL_RESULT_PATH,
            F3_RESULT_PATH,
            validate_f3_experiment,
            validate_f3_final_result,
        )

        prepared_ref, prepared = _file_reference(repository, F3_RESULT_PATH)
        final_ref, final = _file_reference(repository, F3_FINAL_RESULT_PATH)
        validate_f3_experiment(prepared)
        validate_f3_final_result(final, prepared, root=repository)
        refs.extend((prepared_ref, final_ref))
    else:
        from .frontier_f5 import F5_RESULT_PATH, validate_f5_result

        result_ref, result = _file_reference(repository, F5_RESULT_PATH)
        validate_f5_result(result, root=repository)
        refs.append(result_ref)
    technical = build_frontier_result_technical_record(
        experiment_id=identifier,
        frontier_entry_gate_sha256=str(entry["artifact_sha256"]),
        source_artifacts=refs,
        proposed_decision=proposed_decision,
        claim=claim,
    )
    write_frontier_result_technical_record(repository, technical)
    return technical
