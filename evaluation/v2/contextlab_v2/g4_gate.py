"""Hash-bound G4 acceptance contract for the immutable public viewer."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any
from urllib.parse import urlsplit

from .baseline import repository_root
from .immutable_io import ImmutableIOError, read_bytes_snapshot
from .review_invocations import native_review_paths
from .tasking import sha256_json


G4_GATE_SCHEMA = "contextlab.g4-final-gate.v1"
G4_VERIFICATION_SCHEMA = "contextlab.g4-verification.v1"
G4_AI_REVIEW_SCHEMA = "contextlab.g4-ai-review.v1"
G4_AI_INVOCATION_RECEIPT_SCHEMA = "contextlab.g4-ai-invocation-receipt.v2"
G4_APPROVAL_SCHEMA = "contextlab.g4-human-approval.v1"
G4_PENDING_PATH = Path("results/v2/gates/G4.pending.json")
G4_APPROVAL_PATH = Path("results/v2/gates/G4.approval.json")
G4_FINAL_PATH = Path("results/v2/gates/G4.json")
G4_VIEWER_MANIFEST_PATH = Path("results/v2/viewer/g4_export_manifest.json")
G4_VIEWER_EXPORT_PATH = Path("viewer/public/contextlab-viewer.v1.json")
G4_VERIFICATION_PATH = Path("results/v2/viewer/g4_verification.json")
G4_AI_REVIEW_PATHS = (
    Path("results/v2/reviews/g4/gpt-5.6-sol-high/review.json"),
    Path("results/v2/reviews/g4/claude-opus-5-medium/review.json"),
)
G4_AI_INVOCATION_RECEIPT_PATHS = (
    Path("results/v2/reviews/g4/gpt-5.6-sol-high/invocation-receipt.json"),
    Path("results/v2/reviews/g4/claude-opus-5-medium/invocation-receipt.json"),
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_UTC_SECOND = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_REVIEWERS = {
    "gpt-5.6-sol-high": ("gpt-5.6-sol", "high"),
    "claude-opus-5-medium": ("claude-opus-5", "medium"),
}
_REVIEWER_INVOCATION_SOURCES = {
    "gpt-5.6-sol-high": "codex-subagent",
    "claude-opus-5-medium": "claude-cli",
}
_REVIEW_RECEIPT_PATHS = dict(
    zip(_REVIEWERS, G4_AI_INVOCATION_RECEIPT_PATHS, strict=True)
)
_INVOCATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{7,255}\Z")
_VIEWER_CREDENTIAL_PATTERNS = (
    re.compile(rb"(?<![A-Za-z0-9_-])(sk-or-v1-[A-Za-z0-9_-]{20,})"),
    re.compile(
        rb"(?<![A-Za-z0-9_-])"
        rb"(sk-ant-(?:api|admin|oat|ort)[0-9]{2}-[A-Za-z0-9_-]{20,})"
    ),
    re.compile(rb"(?<![A-Z0-9])((?:AKIA|ASIA)[A-Z0-9]{16})(?![A-Z0-9])"),
    re.compile(rb"(?<![A-Za-z0-9_])(gh[pousr]_[A-Za-z0-9]{36,255})"),
    re.compile(rb"(?<![A-Za-z0-9_])(github_pat_[A-Za-z0-9_]{22,255})"),
    re.compile(
        rb"(?:aws_secret_access_key|aws_session_token)[\"']?\s*[:=]\s*"
        rb"[\"']?([A-Za-z0-9/+=]{40,})",
        re.IGNORECASE,
    ),
)
_VIEWER_LOCAL_LOCATION_MARKERS = (b"/Users/", b"/Volumes/")
_CREDENTIAL_PLACEHOLDER_MARKERS = (
    b"example",
    b"placeholder",
    b"redacted",
    b"replace",
    b"your_",
    b"your-",
    b"dummy",
    b"fake",
    b"fixture",
    b"sample",
    b"test_",
    b"test-",
)
_CHECKS = {
    "viewer_contract",
    "publication_binding",
    "public_artifact_hashes",
    "sealed_boundary",
    "static_route",
    "viewer_npm_check",
    "python_regression",
    "secret_scan",
    "g3_gate_replay",
}


class G4GateError(ValueError):
    """G4 evidence or approval is missing, altered, or insufficient."""


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise G4GateError(f"{label} must be a lowercase SHA-256")
    return value


def _valid_hash(value: Mapping[str, Any]) -> bool:
    artifact = value.get("artifact_sha256")
    body = {key: item for key, item in value.items() if key != "artifact_sha256"}
    return isinstance(artifact, str) and artifact == sha256_json(body)


def validate_g4_verification(value: Mapping[str, Any]) -> None:
    expected = {
        "schema_version",
        "viewer_manifest_sha256",
        "viewer_export_sha256",
        "checks",
        "commands",
        "static_asset_count",
        "static_assets_sha256",
        "artifact_sha256",
    }
    if set(value) != expected:
        raise G4GateError("G4 verification fields changed")
    if value.get("schema_version") != G4_VERIFICATION_SCHEMA or not _valid_hash(value):
        raise G4GateError("G4 verification hash is invalid")
    _sha(value.get("viewer_manifest_sha256"), "viewer manifest hash")
    _sha(value.get("viewer_export_sha256"), "viewer export hash")
    checks = value.get("checks")
    if (
        not isinstance(checks, Mapping)
        or set(checks) != _CHECKS
        or any(not isinstance(item, bool) for item in checks.values())
    ):
        raise G4GateError("G4 verification checks changed")
    commands = value.get("commands")
    if not isinstance(commands, list) or len(commands) != 2:
        raise G4GateError("G4 verification must preserve both check commands")
    names: list[str] = []
    for index, command in enumerate(commands):
        if not isinstance(command, Mapping) or set(command) != {
            "command",
            "exit_code",
            "output_sha256",
        }:
            raise G4GateError(f"G4 verification command {index} fields changed")
        name = command.get("command")
        if not isinstance(name, str) or not name:
            raise G4GateError("G4 verification command is empty")
        names.append(name)
        if isinstance(command.get("exit_code"), bool) or not isinstance(
            command.get("exit_code"), int
        ):
            raise G4GateError("G4 verification exit code is invalid")
        _sha(command.get("output_sha256"), "G4 command-output hash")
    if names != ["npm run check", "python viewer regression"]:
        raise G4GateError("G4 verification commands changed")
    assets = value.get("static_asset_count")
    if isinstance(assets, bool) or not isinstance(assets, int) or assets < 0:
        raise G4GateError("G4 static asset count is invalid")
    _sha(value.get("static_assets_sha256"), "G4 static-assets hash")


def _g4_ai_review_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "reviewer_id",
        "model_id",
        "reasoning_effort",
        "viewer_manifest_sha256",
        "viewer_export_sha256",
        "g4_verification_sha256",
        "decision",
        "p0_findings",
        "p1_findings",
        "completed_at",
    )
    return {field: value.get(field) for field in fields}


def validate_g4_ai_invocation_receipt(value: Mapping[str, Any]) -> None:
    """Validate exact resolved identity and reviewed-byte bindings for one AI call."""

    expected = {
        "schema_version",
        "reviewer_id",
        "invocation_source",
        "invocation_id",
        "requested_model",
        "resolved_model",
        "reasoning_effort",
        "viewer_manifest_sha256",
        "viewer_export_sha256",
        "g4_verification_sha256",
        "review_payload_sha256",
        "native_invocation_evidence_path",
        "native_invocation_evidence_sha256",
        "native_output_sha256",
        "completed_at",
        "artifact_sha256",
    }
    if set(value) != expected:
        raise G4GateError("G4 AI invocation receipt fields changed")
    if value.get(
        "schema_version"
    ) != G4_AI_INVOCATION_RECEIPT_SCHEMA or not _valid_hash(value):
        raise G4GateError("G4 AI invocation receipt hash is invalid")
    reviewer_id = value.get("reviewer_id")
    if not isinstance(reviewer_id, str):
        raise G4GateError("unknown G4 AI invocation reviewer")
    identity = _REVIEWERS.get(reviewer_id)
    if identity is None:
        raise G4GateError("unknown G4 AI invocation reviewer")
    model, effort = identity
    if (
        value.get("invocation_source") != _REVIEWER_INVOCATION_SOURCES[reviewer_id]
        or value.get("requested_model") != model
        or value.get("resolved_model") != model
        or value.get("reasoning_effort") != effort
    ):
        raise G4GateError("G4 AI invocation did not resolve the exact model and effort")
    invocation_id = value.get("invocation_id")
    if (
        not isinstance(invocation_id, str)
        or _INVOCATION_ID.fullmatch(invocation_id) is None
    ):
        raise G4GateError("G4 AI invocation ID is missing or invalid")
    for field, label in (
        ("viewer_manifest_sha256", "invoked viewer manifest hash"),
        ("viewer_export_sha256", "invoked viewer export hash"),
        ("g4_verification_sha256", "invoked G4 verification hash"),
        ("review_payload_sha256", "G4 review-payload hash"),
        (
            "native_invocation_evidence_sha256",
            "G4 native invocation evidence hash",
        ),
        ("native_output_sha256", "G4 native invocation output hash"),
    ):
        _sha(value.get(field), label)
    expected_native_path = native_review_paths(_REVIEW_RECEIPT_PATHS[reviewer_id])[
        0
    ].as_posix()
    if value.get("native_invocation_evidence_path") != expected_native_path:
        raise G4GateError("G4 native invocation evidence path changed")
    completed_at = value.get("completed_at")
    if not isinstance(completed_at, str) or _UTC_SECOND.fullmatch(completed_at) is None:
        raise G4GateError("G4 AI invocation timestamp is invalid")


def build_g4_ai_invocation_receipt(
    *,
    reviewer_id: str,
    invocation_id: str,
    viewer_manifest_sha256: str,
    viewer_export_sha256: str,
    g4_verification_sha256: str,
    decision: str,
    p0_findings: Sequence[str],
    p1_findings: Sequence[str],
    completed_at: str,
    native_invocation_evidence_sha256: str,
    native_output_sha256: str,
) -> dict[str, Any]:
    """Build the receipt emitted after one exact G4 review invocation."""

    identity = _REVIEWERS.get(reviewer_id)
    if identity is None:
        raise G4GateError("unknown G4 AI reviewer")
    payload = {
        "reviewer_id": reviewer_id,
        "model_id": identity[0],
        "reasoning_effort": identity[1],
        "viewer_manifest_sha256": viewer_manifest_sha256,
        "viewer_export_sha256": viewer_export_sha256,
        "g4_verification_sha256": g4_verification_sha256,
        "decision": decision,
        "p0_findings": list(p0_findings),
        "p1_findings": list(p1_findings),
        "completed_at": completed_at,
    }
    receipt: dict[str, Any] = {
        "schema_version": G4_AI_INVOCATION_RECEIPT_SCHEMA,
        "reviewer_id": reviewer_id,
        "invocation_source": _REVIEWER_INVOCATION_SOURCES[reviewer_id],
        "invocation_id": invocation_id,
        "requested_model": identity[0],
        "resolved_model": identity[0],
        "reasoning_effort": identity[1],
        "viewer_manifest_sha256": _sha(
            viewer_manifest_sha256, "invoked viewer manifest hash"
        ),
        "viewer_export_sha256": _sha(
            viewer_export_sha256, "invoked viewer export hash"
        ),
        "g4_verification_sha256": _sha(
            g4_verification_sha256, "invoked G4 verification hash"
        ),
        "review_payload_sha256": sha256_json(payload),
        "native_invocation_evidence_path": native_review_paths(
            _REVIEW_RECEIPT_PATHS[reviewer_id]
        )[0].as_posix(),
        "native_invocation_evidence_sha256": _sha(
            native_invocation_evidence_sha256,
            "G4 native invocation evidence hash",
        ),
        "native_output_sha256": _sha(
            native_output_sha256, "G4 native invocation output hash"
        ),
        "completed_at": completed_at,
    }
    receipt["artifact_sha256"] = sha256_json(receipt)
    validate_g4_ai_invocation_receipt(receipt)
    return receipt


def validate_g4_ai_review(
    value: Mapping[str, Any],
    invocation_receipt: Mapping[str, Any] | None = None,
) -> None:
    expected = {
        "schema_version",
        "reviewer_id",
        "model_id",
        "reasoning_effort",
        "viewer_manifest_sha256",
        "viewer_export_sha256",
        "g4_verification_sha256",
        "invocation_receipt_path",
        "invocation_receipt_sha256",
        "decision",
        "p0_findings",
        "p1_findings",
        "completed_at",
        "artifact_sha256",
    }
    if set(value) != expected:
        raise G4GateError("G4 AI review fields changed")
    if value.get("schema_version") != G4_AI_REVIEW_SCHEMA or not _valid_hash(value):
        raise G4GateError("G4 AI review hash is invalid")
    reviewer_id = value.get("reviewer_id")
    expected_identity = _REVIEWERS.get(reviewer_id)
    if expected_identity != (
        value.get("model_id"),
        value.get("reasoning_effort"),
    ):
        raise G4GateError("G4 reviewer identity changed")
    _sha(value.get("viewer_manifest_sha256"), "reviewed viewer manifest hash")
    _sha(value.get("viewer_export_sha256"), "reviewed viewer export hash")
    _sha(value.get("g4_verification_sha256"), "reviewed G4 verification hash")
    expected_path = _REVIEW_RECEIPT_PATHS.get(reviewer_id)
    if value.get("invocation_receipt_path") != (
        expected_path.as_posix() if expected_path is not None else None
    ):
        raise G4GateError("G4 AI review invocation-receipt path changed")
    _sha(value.get("invocation_receipt_sha256"), "G4 invocation-receipt hash")
    if value.get("decision") not in {"pass", "fail"}:
        raise G4GateError("G4 AI review decision is invalid")
    for key in ("p0_findings", "p1_findings"):
        findings = value.get(key)
        if not isinstance(findings, list) or any(
            not isinstance(item, str) or not item for item in findings
        ):
            raise G4GateError(f"G4 {key} are invalid")
    if value.get("decision") == "pass" and (
        value["p0_findings"] or value["p1_findings"]
    ):
        raise G4GateError("a passing G4 AI review cannot retain P0/P1 findings")
    completed_at = value.get("completed_at")
    if not isinstance(completed_at, str) or _UTC_SECOND.fullmatch(completed_at) is None:
        raise G4GateError("G4 AI review timestamp is invalid")
    if invocation_receipt is not None:
        validate_g4_ai_invocation_receipt(invocation_receipt)
        if (
            invocation_receipt.get("reviewer_id") != reviewer_id
            or invocation_receipt.get("artifact_sha256")
            != value["invocation_receipt_sha256"]
            or invocation_receipt.get("viewer_manifest_sha256")
            != value["viewer_manifest_sha256"]
            or invocation_receipt.get("viewer_export_sha256")
            != value["viewer_export_sha256"]
            or invocation_receipt.get("g4_verification_sha256")
            != value["g4_verification_sha256"]
            or invocation_receipt.get("completed_at") != completed_at
            or invocation_receipt.get("review_payload_sha256")
            != sha256_json(_g4_ai_review_payload(value))
        ):
            raise G4GateError("G4 AI review differs from its invocation receipt")


def build_g4_ai_review(
    *,
    reviewer_id: str,
    viewer_manifest_sha256: str,
    viewer_export_sha256: str,
    g4_verification_sha256: str,
    decision: str,
    p0_findings: Sequence[str],
    p1_findings: Sequence[str],
    completed_at: str,
    invocation_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one exact, hash-bound AI review without accepting identity overrides."""

    identity = _REVIEWERS.get(reviewer_id)
    if identity is None:
        raise G4GateError("unknown G4 AI reviewer")
    for findings, label in (
        (p0_findings, "P0 findings"),
        (p1_findings, "P1 findings"),
    ):
        if isinstance(findings, (str, bytes)) or not isinstance(findings, Sequence):
            raise G4GateError(f"G4 {label} must be a list")
    review: dict[str, Any] = {
        "schema_version": G4_AI_REVIEW_SCHEMA,
        "reviewer_id": reviewer_id,
        "model_id": identity[0],
        "reasoning_effort": identity[1],
        "viewer_manifest_sha256": _sha(
            viewer_manifest_sha256, "reviewed viewer manifest hash"
        ),
        "viewer_export_sha256": _sha(
            viewer_export_sha256, "reviewed viewer export hash"
        ),
        "g4_verification_sha256": _sha(
            g4_verification_sha256, "reviewed G4 verification hash"
        ),
        "invocation_receipt_path": _REVIEW_RECEIPT_PATHS[reviewer_id].as_posix(),
        "invocation_receipt_sha256": invocation_receipt.get("artifact_sha256"),
        "decision": decision,
        "p0_findings": list(p0_findings),
        "p1_findings": list(p1_findings),
        "completed_at": completed_at,
    }
    review["artifact_sha256"] = sha256_json(review)
    validate_g4_ai_review(review, invocation_receipt)
    return review


def validate_g4_ai_review_provenance(
    root: Path,
    *,
    review: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> None:
    """Replay the fixed native CLI execution behind one G4 review."""

    from .review_invocations import (
        AIReviewInvocationError,
        assert_native_proof_fields,
        validate_recorded_ai_review,
    )

    validate_g4_ai_review(review, receipt)
    reviewer_id = str(review["reviewer_id"])
    anchor = _REVIEW_RECEIPT_PATHS[reviewer_id]
    bindings = {
        "viewer_manifest_path": G4_VIEWER_MANIFEST_PATH.as_posix(),
        "viewer_manifest_sha256": str(review["viewer_manifest_sha256"]),
        "viewer_export_path": G4_VIEWER_EXPORT_PATH.as_posix(),
        "viewer_export_sha256": str(review["viewer_export_sha256"]),
        "g4_verification_path": G4_VERIFICATION_PATH.as_posix(),
        "g4_verification_sha256": str(review["g4_verification_sha256"]),
    }
    try:
        evidence = validate_recorded_ai_review(
            root,
            anchor_path=anchor,
            reviewer_id=reviewer_id,
            review_kind="g4-gate",
            target_bindings=bindings,
            expected_response={
                "decision": review["decision"],
                "p0_findings": review["p0_findings"],
                "p1_findings": review["p1_findings"],
            },
            invocation_id=str(receipt["invocation_id"]),
            completed_at=str(receipt["completed_at"]),
        )
        assert_native_proof_fields(
            anchor_path=anchor, receipt=receipt, evidence=evidence
        )
    except AIReviewInvocationError as exc:
        raise G4GateError("G4 AI review lacks valid native execution proof") from exc


def _technical_passes(
    verification: Mapping[str, Any], reviews: Sequence[Mapping[str, Any]]
) -> bool:
    return (
        all(verification["checks"].values())
        and all(command["exit_code"] == 0 for command in verification["commands"])
        and all(review["decision"] == "pass" for review in reviews)
    )


def build_g4_gate_from_evidence(
    *,
    viewer_manifest_sha256: str,
    viewer_export_sha256: str,
    verification: Mapping[str, Any],
    ai_reviews: Sequence[Mapping[str, Any]],
    ai_invocation_receipts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a pending human gate from exact viewer and dual-AI evidence."""

    manifest_sha = _sha(viewer_manifest_sha256, "viewer manifest hash")
    export_sha = _sha(viewer_export_sha256, "viewer export hash")
    validate_g4_verification(verification)
    reviews = [dict(review) for review in ai_reviews]
    receipts = [dict(receipt) for receipt in ai_invocation_receipts]
    if len(reviews) != 2 or len(receipts) != 2:
        raise G4GateError(
            "G4 requires exactly two independent AI reviews and invocation receipts"
        )
    for review, receipt in zip(reviews, receipts, strict=True):
        validate_g4_ai_review(review, receipt)
    if len({receipt["invocation_id"] for receipt in receipts}) != 2:
        raise G4GateError("G4 AI reviews must come from separate invocations")
    if [review["reviewer_id"] for review in reviews] != list(_REVIEWERS):
        raise G4GateError("G4 AI review order or identity changed")
    bindings = [verification, *reviews]
    if any(
        item["viewer_manifest_sha256"] != manifest_sha
        or item["viewer_export_sha256"] != export_sha
        for item in bindings
    ):
        raise G4GateError("G4 evidence reviews a different viewer artifact")
    if any(
        review["g4_verification_sha256"] != verification["artifact_sha256"]
        for review in reviews
    ):
        raise G4GateError("G4 AI review binds a different verification record")
    technical: dict[str, Any] = {
        "schema_version": G4_GATE_SCHEMA,
        "viewer_manifest_sha256": manifest_sha,
        "viewer_export_sha256": export_sha,
        "verification": dict(verification),
        "ai_invocation_receipts": receipts,
        "ai_reviews": reviews,
        "technical_status": (
            "passed" if _technical_passes(verification, reviews) else "failed"
        ),
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
        "final_decision": "blocked",
    }
    gate["artifact_sha256"] = sha256_json(gate)
    validate_g4_gate(gate)
    return gate


def validate_g4_gate(value: Mapping[str, Any]) -> None:
    """Validate G4 technical evidence and Kevin's exact approval binding."""

    expected = {
        "schema_version",
        "viewer_manifest_sha256",
        "viewer_export_sha256",
        "verification",
        "ai_invocation_receipts",
        "ai_reviews",
        "technical_status",
        "technical_record_sha256",
        "human_approval",
        "final_decision",
        "artifact_sha256",
    }
    if set(value) != expected:
        raise G4GateError("G4 gate fields changed")
    if value.get("schema_version") != G4_GATE_SCHEMA:
        raise G4GateError("unsupported G4 gate schema")
    if not _valid_hash(value):
        raise G4GateError("G4 gate artifact hash mismatch")
    _sha(value.get("viewer_manifest_sha256"), "viewer manifest hash")
    _sha(value.get("viewer_export_sha256"), "viewer export hash")
    verification = value.get("verification")
    if not isinstance(verification, Mapping):
        raise G4GateError("G4 verification must be an object")
    validate_g4_verification(verification)
    if (
        verification["viewer_manifest_sha256"] != value["viewer_manifest_sha256"]
        or verification["viewer_export_sha256"] != value["viewer_export_sha256"]
    ):
        raise G4GateError("G4 verification binding changed")
    reviews = value.get("ai_reviews")
    receipts = value.get("ai_invocation_receipts")
    if (
        not isinstance(reviews, list)
        or len(reviews) != 2
        or not isinstance(receipts, list)
        or len(receipts) != 2
    ):
        raise G4GateError("G4 gate must bind two AI reviews and invocation receipts")
    for review, receipt in zip(reviews, receipts, strict=True):
        if not isinstance(review, Mapping):
            raise G4GateError("G4 AI review must be an object")
        if not isinstance(receipt, Mapping):
            raise G4GateError("G4 AI invocation receipt must be an object")
        validate_g4_ai_review(review, receipt)
        if (
            review["viewer_manifest_sha256"] != value["viewer_manifest_sha256"]
            or review["viewer_export_sha256"] != value["viewer_export_sha256"]
            or review["g4_verification_sha256"] != verification["artifact_sha256"]
        ):
            raise G4GateError("G4 review or verification binding changed")
    if len({receipt["invocation_id"] for receipt in receipts}) != 2:
        raise G4GateError("G4 AI reviews must come from separate invocations")
    if [review["reviewer_id"] for review in reviews] != list(_REVIEWERS):
        raise G4GateError("G4 reviewer order changed")

    technical = {
        key: item
        for key, item in value.items()
        if key
        not in {
            "technical_record_sha256",
            "human_approval",
            "final_decision",
            "artifact_sha256",
        }
    }
    technical_sha = sha256_json(technical)
    if value.get("technical_record_sha256") != technical_sha:
        raise G4GateError("G4 technical-record hash mismatch")
    expected_status = "passed" if _technical_passes(verification, reviews) else "failed"
    if value.get("technical_status") != expected_status:
        raise G4GateError("G4 technical status disagrees with AI review evidence")

    approval = value.get("human_approval")
    if not isinstance(approval, Mapping) or (
        approval.get("reviewer") != "Kevin Araujo"
        or approval.get("technical_record_sha256") != technical_sha
    ):
        raise G4GateError("G4 human approval is not bound to Kevin")
    if approval.get("status") == "pending":
        if (
            set(approval)
            != {
                "status",
                "reviewer",
                "technical_record_sha256",
            }
            or value.get("final_decision") != "blocked"
        ):
            raise G4GateError("pending G4 decision fields changed")
    elif approval.get("status") == "approved":
        if (
            value.get("technical_status") != "passed"
            or set(approval)
            != {
                "status",
                "reviewer",
                "technical_record_sha256",
                "approved_at",
            }
            or not isinstance(approval.get("approved_at"), str)
            or _UTC_SECOND.fullmatch(approval["approved_at"]) is None
            or value.get("final_decision") != "passed"
        ):
            raise G4GateError("approved G4 decision is invalid")
    else:
        raise G4GateError("unsupported G4 human-approval status")


def build_g4_approval(
    pending_gate: Mapping[str, Any], *, approved_at: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bind Kevin's approval to one technically passing pending G4 record."""

    validate_g4_gate(pending_gate)
    if (
        pending_gate.get("technical_status") != "passed"
        or pending_gate.get("human_approval", {}).get("status") != "pending"
        or pending_gate.get("final_decision") != "blocked"
    ):
        raise G4GateError("only a technically passing pending G4 gate can be approved")
    if not isinstance(approved_at, str) or _UTC_SECOND.fullmatch(approved_at) is None:
        raise G4GateError("G4 approval timestamp must be UTC to the second")
    approval: dict[str, Any] = {
        "schema_version": G4_APPROVAL_SCHEMA,
        "technical_record_sha256": pending_gate["technical_record_sha256"],
        "pending_gate_artifact_sha256": pending_gate["artifact_sha256"],
        "reviewer": "Kevin Araujo",
        "reviewer_role": "sole_human_reviewer",
        "decision": "approved",
        "approved_at": approved_at,
    }
    approval["artifact_sha256"] = sha256_json(approval)
    final_gate: dict[str, Any] = {
        key: item
        for key, item in pending_gate.items()
        if key not in {"human_approval", "final_decision", "artifact_sha256"}
    }
    final_gate["human_approval"] = {
        "status": "approved",
        "reviewer": "Kevin Araujo",
        "technical_record_sha256": pending_gate["technical_record_sha256"],
        "approved_at": approved_at,
    }
    final_gate["final_decision"] = "passed"
    final_gate["artifact_sha256"] = sha256_json(final_gate)
    validate_g4_gate(final_gate)
    return approval, final_gate


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _g4_target(repository: Path, path: Path) -> Path:
    """Return a lexical repository-relative target without following links."""

    if not path.is_absolute():
        raise G4GateError("G4 artifact target must be absolute")
    try:
        relative = path.relative_to(repository)
    except ValueError as exc:
        raise G4GateError("G4 artifact target is outside the repository") from exc
    if relative == Path() or ".." in relative.parts or not relative.name:
        raise G4GateError("G4 artifact target is unsafe")
    return relative


def _open_g4_parent(repository: Path, relative: Path, *, create: bool = True) -> int:
    """Open/create a target parent through no-follow directory descriptors."""

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(repository, flags)
    try:
        for part in relative.parent.parts:
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o755, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _g4_target_exists(parent_fd: int, name: str) -> bool:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        raise G4GateError("G4 artifact target must not be a symlink")
    return True


def _inode(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _unlink_g4_inode(
    parent_fd: int, name: str, *, expected_inode: tuple[int, int]
) -> None:
    """Remove only the exact G4 file created by this transaction."""

    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _inode(current) == expected_inode:
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
    except OSError:
        pass


def _read_g4_file(
    parent_fd: int,
    name: str,
    *,
    expected_inode: tuple[int, int] | None = None,
) -> tuple[bytes, tuple[int, int]]:
    """Read one regular file while proving its pathname and descriptor agree."""

    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = -1
    try:
        path_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(path_stat.st_mode):
            raise G4GateError("G4 artifact target is unsafe")
        descriptor = os.open(name, flags, dir_fd=parent_fd)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or _inode(before) != _inode(path_stat):
            raise G4GateError("G4 artifact changed while it was opened")
        chunks: list[bytes] = []
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        after = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        identity = _inode(before)
        if (
            _inode(after) != identity
            or _inode(current) != identity
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
            or (expected_inode is not None and identity != expected_inode)
        ):
            raise G4GateError("G4 artifact changed while it was read")
        return b"".join(chunks), identity
    except OSError as exc:
        raise G4GateError("cannot read G4 artifact safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _create_g4_file(parent_fd: int, name: str, data: bytes) -> tuple[int, int]:
    """Create the final pathname directly and return its bound inode."""

    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = -1
    created_inode: tuple[int, int] | None = None
    completed = False
    try:
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
        except FileExistsError as exc:
            raise G4GateError("G4 approval artifact already exists") from exc
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise G4GateError("G4 artifact target is unsafe")
        created_inode = _inode(metadata)
        written = 0
        while written < len(data):
            count = os.write(descriptor, data[written:])
            if count <= 0:
                raise OSError("short G4 artifact write")
            written += count
        os.fsync(descriptor)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode) or _inode(current) != created_inode:
            raise G4GateError("G4 artifact changed during publication")
        os.fsync(parent_fd)
        completed = True
        return created_inode
    except OSError as exc:
        raise G4GateError("cannot create G4 artifact safely") from exc
    except Exception:
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if created_inode is not None and not completed:
            # A concurrent replacement is intentionally left alone because
            # its inode cannot be attributed to this transaction.
            _unlink_g4_inode(parent_fd, name, expected_inode=created_inode)


def _g4_parent_still_bound(repository: Path, relative: Path, parent_fd: int) -> bool:
    current_fd = -1
    try:
        current_fd = _open_g4_parent(repository, relative, create=False)
        return _inode(os.fstat(current_fd)) == _inode(os.fstat(parent_fd))
    except OSError:
        return False
    finally:
        if current_fd >= 0:
            os.close(current_fd)


def _create_only(repository: Path, plan: Mapping[Path, bytes]) -> None:
    """Create repository-contained G4 artifacts without following symlinks."""

    lexical_repository = repository.absolute()
    repository = lexical_repository.resolve(strict=True)
    targets = [
        (_g4_target(lexical_repository, path.absolute()), data)
        for path, data in plan.items()
    ]
    entries: list[tuple[Path, bytes, int]] = []
    created: list[tuple[Path, bytes, int, tuple[int, int]]] = []
    try:
        for relative, data in targets:
            try:
                parent_fd = _open_g4_parent(repository, relative)
            except OSError as exc:
                raise G4GateError(
                    f"G4 artifact parent is unsafe: {relative.parent}"
                ) from exc
            entries.append((relative, data, parent_fd))
            if _g4_target_exists(parent_fd, relative.name):
                raise G4GateError("G4 approval artifact already exists")

        for relative, data, parent_fd in entries:
            if not _g4_parent_still_bound(repository, relative, parent_fd):
                raise G4GateError(f"G4 artifact parent changed: {relative.parent}")
            identity = _create_g4_file(parent_fd, relative.name, data)
            created.append((relative, data, parent_fd, identity))

        for relative, data, parent_fd, identity in created:
            if not _g4_parent_still_bound(repository, relative, parent_fd):
                raise G4GateError(f"G4 artifact parent changed: {relative.parent}")
            saved, _ = _read_g4_file(parent_fd, relative.name, expected_inode=identity)
            if saved != data:
                raise G4GateError("G4 artifact differs after publication")
    except Exception:
        for relative, _data, parent_fd, identity in reversed(created):
            _unlink_g4_inode(parent_fd, relative.name, expected_inode=identity)
        raise
    finally:
        for _relative, _data, parent_fd in entries:
            os.close(parent_fd)


def write_pending_g4_gate(root: Path | None, gate: Mapping[str, Any]) -> dict[str, Any]:
    """Persist a pending G4 technical record without allowing replacement."""

    repository = (root or repository_root()).resolve()
    validate_g4_gate(gate)
    if gate.get("human_approval", {}).get("status") != "pending":
        raise G4GateError("only a pending G4 gate can be written here")
    path = repository / G4_PENDING_PATH
    data = _json_bytes(gate)
    if path.exists():
        try:
            existing = _read_repository_bytes(
                repository, G4_PENDING_PATH, "pending G4 gate"
            )
        except G4GateError:
            existing = None
        if existing == data:
            return dict(gate)
        raise G4GateError("immutable pending G4 gate differs")
    _create_only(repository, {path: data})
    return dict(gate)


def approve_g4_gate(root: Path | None = None, *, approved_at: str) -> dict[str, Any]:
    """Create Kevin's separate G4 approval and final gate once."""

    repository = (root or repository_root()).resolve()
    pending = _read_json(repository, G4_PENDING_PATH, "pending G4 gate")
    validate_g4_gate(pending)
    _validate_g4_native_review_records(repository, pending)
    _replay_current_g4_publication(
        repository,
        expected_manifest_sha256=str(pending["viewer_manifest_sha256"]),
        expected_export_sha256=str(pending["viewer_export_sha256"]),
        expected_verification=pending["verification"],
        g3_gate=_require_replayed_g3(repository),
    )
    approval, final_gate = build_g4_approval(pending, approved_at=approved_at)
    _create_only(
        repository,
        {
            repository / G4_APPROVAL_PATH: _json_bytes(approval),
            repository / G4_FINAL_PATH: _json_bytes(final_gate),
        },
    )
    return final_gate


def _replay_current_g4_publication(
    root: Path,
    *,
    expected_manifest_sha256: str,
    expected_export_sha256: str,
    expected_verification: Mapping[str, Any],
    g3_gate: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fail closed unless the current viewer is the exact reviewed publication.

    G4 is not a historical statement.  A downstream frontier entry needs the
    current bytes still to be the bytes Kevin approved.  This replay is read
    only: it does not rebuild the viewer, execute tools, or contact providers.
    """

    from .viewer_export import (
        validate_viewer_artifact_pointers,
        validate_viewer_export,
    )

    validate_g4_verification(expected_verification)
    manifest, manifest_bytes = _read_json_snapshot(
        root, G4_VIEWER_MANIFEST_PATH, "G4 viewer manifest"
    )
    export, export_bytes = _read_json_snapshot(
        root, G4_VIEWER_EXPORT_PATH, "G4 viewer export"
    )
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    export_sha = hashlib.sha256(export_bytes).hexdigest()
    if (
        manifest_sha != expected_manifest_sha256
        or export_sha != expected_export_sha256
        or expected_verification.get("viewer_manifest_sha256") != manifest_sha
        or expected_verification.get("viewer_export_sha256") != export_sha
    ):
        raise G4GateError("current viewer manifest or export differs from approved G4")
    if (
        manifest.get("schema_version") != "contextlab.viewer-export-manifest.v1"
        or not _valid_hash(manifest)
        or manifest.get("viewer_export_path") != G4_VIEWER_EXPORT_PATH.as_posix()
    ):
        raise G4GateError("current G4 viewer manifest is invalid")
    if g3_gate is not None and not _g3_manifest_binding_valid(manifest, g3_gate):
        raise G4GateError("current G4 viewer manifest lost its approved G3 binding")
    boundaries = manifest.get("publication_boundaries")
    if (
        not isinstance(boundaries, Mapping)
        or boundaries.get("sealed_artifacts_copied") is not False
        or boundaries.get("protected_gold_copied") is not False
        or boundaries.get("protected_scoring_packets_copied") is not False
        or boundaries.get("public_receipts_are_saved_validated_inputs") is not True
    ):
        raise G4GateError("current G4 publication boundary is invalid")
    try:
        validate_viewer_export(export)
        validate_viewer_artifact_pointers(export, root)
    except Exception as exc:
        raise G4GateError(f"current G4 viewer export is invalid: {exc}") from exc
    if export.get("exportId") != manifest.get("export_id"):
        raise G4GateError("current G4 viewer export identity differs from its manifest")
    if not _publication_binding_valid(root, manifest, export):
        raise G4GateError("current G4 viewer publication binding is invalid")
    if not _viewer_public_artifacts_valid(root, manifest):
        raise G4GateError("current G4 public artifact inventory is invalid")
    current_verification = _read_json(root, G4_VERIFICATION_PATH, "G4 verification")
    validate_g4_verification(current_verification)
    if dict(current_verification) != dict(expected_verification):
        raise G4GateError("current G4 verification differs from the approved record")
    route_valid, asset_count = _static_route_valid(root)
    static_assets_sha, snapshot_count = _static_assets_snapshot(root)
    if (
        not route_valid
        or asset_count != snapshot_count
        or expected_verification.get("static_asset_count") != asset_count
        or expected_verification.get("static_assets_sha256") != static_assets_sha
        or not _viewer_secret_scan(root)
    ):
        raise G4GateError("current G4 static assets differ from approved verification")
    return manifest, export


def load_approved_g4_gate(
    root: Path | None = None, *, replay_historical_provider: bool = True
) -> dict[str, Any]:
    """Load and cross-check canonical pending, Kevin approval, and derived final G4.

    A versioned frontier route migration may disable host-local historical dependencies:
    the retired live-provider preflight and native reviewer sessions. The immutable G3
    approval, embedded G4 reviews, Kevin approval, and all current G4 public artifacts
    still replay.
    """

    repository = (root or repository_root()).resolve()
    pending = _read_json(repository, G4_PENDING_PATH, "pending G4 gate")
    approval = _read_json(repository, G4_APPROVAL_PATH, "G4 Kevin approval")
    final_gate = _read_json(repository, G4_FINAL_PATH, "final G4 gate")
    validate_g4_gate(pending)
    if replay_historical_provider:
        _validate_g4_native_review_records(repository, pending)
    if (
        pending.get("human_approval", {}).get("status") != "pending"
        or pending.get("final_decision") != "blocked"
    ):
        raise G4GateError("canonical pending G4 gate is not pending")
    expected_fields = {
        "schema_version",
        "technical_record_sha256",
        "pending_gate_artifact_sha256",
        "reviewer",
        "reviewer_role",
        "decision",
        "approved_at",
        "artifact_sha256",
    }
    approved_at = approval.get("approved_at")
    if (
        set(approval) != expected_fields
        or approval.get("schema_version") != G4_APPROVAL_SCHEMA
        or not _valid_hash(approval)
        or approval.get("technical_record_sha256") != pending["technical_record_sha256"]
        or approval.get("pending_gate_artifact_sha256") != pending["artifact_sha256"]
        or approval.get("reviewer") != "Kevin Araujo"
        or approval.get("reviewer_role") != "sole_human_reviewer"
        or approval.get("decision") != "approved"
        or not isinstance(approved_at, str)
        or _UTC_SECOND.fullmatch(approved_at) is None
    ):
        raise G4GateError("G4 Kevin approval artifact is invalid")
    expected_approval, expected_final = build_g4_approval(
        pending, approved_at=approved_at
    )
    if approval != expected_approval or final_gate != expected_final:
        raise G4GateError(
            "final G4 gate differs from canonical pending gate or Kevin approval"
        )
    if replay_historical_provider:
        g3_gate = _require_replayed_g3(repository)
    else:
        from .viewer_export import _require_approved_g3_gate

        g3_gate = _read_json(
            repository,
            Path("results/v2/gates/G3.json"),
            "approved G3 gate for frontier route migration",
        )
        _require_approved_g3_gate(g3_gate)
    _replay_current_g4_publication(
        repository,
        expected_manifest_sha256=str(final_gate["viewer_manifest_sha256"]),
        expected_export_sha256=str(final_gate["viewer_export_sha256"]),
        expected_verification=final_gate["verification"],
        g3_gate=g3_gate,
    )
    return final_gate


def _read_repository_bytes(root: Path, relative: Path, label: str) -> bytes:
    try:
        return read_bytes_snapshot(root, relative)
    except ImmutableIOError as exc:
        raise G4GateError(f"cannot read stable {label}") from exc


def _read_json_snapshot(
    root: Path, relative: Path, label: str
) -> tuple[dict[str, Any], bytes]:
    raw = _read_repository_bytes(root, relative, label)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G4GateError(f"cannot read {label}") from exc
    if not isinstance(value, dict):
        raise G4GateError(f"{label} must be an object")
    return value, raw


def _read_json(root: Path, relative: Path, label: str) -> dict[str, Any]:
    value, _ = _read_json_snapshot(root, relative, label)
    return value


def _require_replayed_g3(root: Path) -> dict[str, Any]:
    from .viewer_export import require_replayed_approved_g3_gate

    try:
        return require_replayed_approved_g3_gate(root)
    except Exception as exc:
        raise G4GateError(
            "G4 requires the approved canonical G3 gate to replay"
        ) from exc


def _g3_manifest_binding_valid(
    manifest: Mapping[str, Any], g3_gate: Mapping[str, Any]
) -> bool:
    bindings = manifest.get("approval_bindings")
    human = g3_gate.get("human_decision")
    decision = human.get("decision_record") if isinstance(human, Mapping) else None
    return bool(
        isinstance(bindings, Mapping)
        and isinstance(decision, Mapping)
        and bindings.get("g3_gate_artifact_sha256") == g3_gate.get("artifact_sha256")
        and bindings.get("g3_technical_record_sha256")
        == g3_gate.get("technical_record_sha256")
        and bindings.get("g3_final_decision") == g3_gate.get("final_decision")
        and bindings.get("kevin_decision_sha256") == decision.get("artifact_sha256")
    )


def _validate_g4_native_review_records(root: Path, gate: Mapping[str, Any]) -> None:
    reviews = gate.get("ai_reviews")
    receipts = gate.get("ai_invocation_receipts")
    if not isinstance(reviews, list) or not isinstance(receipts, list):
        raise G4GateError("G4 native review records are missing")
    for review, receipt in zip(reviews, receipts, strict=True):
        if not isinstance(review, Mapping) or not isinstance(receipt, Mapping):
            raise G4GateError("G4 native review record is invalid")
        validate_g4_ai_review_provenance(root, review=review, receipt=receipt)


def run_g4_gate(root: Path | None = None) -> dict[str, Any]:
    """Revalidate the frozen publication and write a pending G4 human gate."""

    repository = (root or repository_root()).resolve()
    g3_gate = _require_replayed_g3(repository)
    verification = _read_json(repository, G4_VERIFICATION_PATH, "G4 verification")
    manifest_sha = str(verification.get("viewer_manifest_sha256", ""))
    export_sha = str(verification.get("viewer_export_sha256", ""))
    _replay_current_g4_publication(
        repository,
        expected_manifest_sha256=manifest_sha,
        expected_export_sha256=export_sha,
        expected_verification=verification,
        g3_gate=g3_gate,
    )
    reviews = [
        _read_json(repository, path, f"G4 AI review {index}")
        for index, path in enumerate(G4_AI_REVIEW_PATHS)
    ]
    receipts = [
        _read_json(repository, path, f"G4 AI invocation receipt {index}")
        for index, path in enumerate(G4_AI_INVOCATION_RECEIPT_PATHS)
    ]
    for review, receipt in zip(reviews, receipts, strict=True):
        validate_g4_ai_review_provenance(repository, review=review, receipt=receipt)
    gate = build_g4_gate_from_evidence(
        viewer_manifest_sha256=manifest_sha,
        viewer_export_sha256=export_sha,
        verification=verification,
        ai_reviews=reviews,
        ai_invocation_receipts=receipts,
    )
    return write_pending_g4_gate(repository, gate)


def _viewer_public_artifacts_valid(root: Path, manifest: Mapping[str, Any]) -> bool:
    from .viewer_export import (
        VIEWER_FORBIDDEN_PUBLIC_TOKENS,
        _media_type,
        public_viewer_source_allowed,
    )

    rows = manifest.get("public_artifacts")
    if not isinstance(rows, list) or not rows:
        return False
    seen_public: set[str] = set()
    seen_source: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {
            "sourcePath",
            "sourceSha256",
            "publicPath",
            "staticUrl",
            "mediaType",
        }:
            return False
        public_raw = row.get("publicPath")
        source_raw = row.get("sourcePath")
        digest = row.get("sourceSha256")
        if (
            not isinstance(public_raw, str)
            or not isinstance(source_raw, str)
            or public_raw in seen_public
            or source_raw in seen_source
            or any(
                token in value.casefold()
                for value in (public_raw, source_raw)
                for token in VIEWER_FORBIDDEN_PUBLIC_TOKENS
            )
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or not isinstance(row.get("mediaType"), str)
            or not row["mediaType"]
        ):
            return False
        seen_public.add(public_raw)
        seen_source.add(source_raw)
        relative = Path(public_raw)
        source_relative = Path(source_raw)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or source_relative.is_absolute()
            or ".." in source_relative.parts
            or relative.name != source_relative.name
            or row.get("mediaType") != _media_type(source_relative)
        ):
            return False
        expected_public = Path("viewer/public/artifacts") / digest / relative.name
        derived_source = source_relative == expected_public
        if not derived_source and not public_viewer_source_allowed(source_relative):
            return False
        if (
            source_relative.as_posix().startswith("viewer/public/artifacts/")
            and not derived_source
        ):
            return False
        if relative != expected_public:
            return False
        try:
            public_bytes = _read_repository_bytes(root, relative, "G4 public artifact")
            source_bytes = _read_repository_bytes(
                root, source_relative, "G4 public artifact source"
            )
        except G4GateError:
            return False
        if (
            hashlib.sha256(public_bytes).hexdigest() != digest
            or hashlib.sha256(source_bytes).hexdigest() != digest
        ):
            return False
        expected_url = f"./artifacts/{digest}/{relative.name}"
        if row.get("staticUrl") != expected_url:
            return False
    return [row["sourcePath"] for row in rows] == sorted(seen_source)


def _publication_binding_valid(
    root: Path, manifest: Mapping[str, Any], export: Mapping[str, Any]
) -> bool:
    try:
        manifest_bytes = _read_repository_bytes(
            root, G4_VIEWER_MANIFEST_PATH, "G4 viewer manifest"
        )
    except G4GateError:
        return False
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    reference = export.get("exportManifest")
    if not isinstance(reference, Mapping):
        return False
    expected_reference = {
        "kind": "export-manifest",
        "label": "G4 viewer export manifest",
        "path": G4_VIEWER_MANIFEST_PATH.as_posix(),
        "sha256": manifest_sha,
        "staticUrl": (f"./artifacts/{manifest_sha}/{G4_VIEWER_MANIFEST_PATH.name}"),
        "mediaType": "application/json",
    }
    public_relative = (
        Path("viewer/public/artifacts") / manifest_sha / G4_VIEWER_MANIFEST_PATH.name
    )
    try:
        public_bytes = _read_repository_bytes(
            root, public_relative, "G4 public manifest copy"
        )
    except G4GateError:
        return False
    return bool(
        dict(reference) == expected_reference
        and export.get("exportId") == manifest.get("export_id")
        and export.get("schemaVersion") == manifest.get("viewer_schema_version")
        and manifest.get("viewer_export_path") == G4_VIEWER_EXPORT_PATH.as_posix()
        and public_bytes == manifest_bytes
    )


_EXECUTABLE_STATIC_SUFFIXES = {".html", ".htm", ".js", ".mjs", ".cjs", ".css", ".svg"}
_INERT_EXECUTABLE_URL_PREFIXES = (
    "http://www.w3.org/1998/Math/MathML",
    "http://www.w3.org/1999/xlink",
    "http://www.w3.org/2000/svg",
    "http://www.w3.org/XML/1998/namespace",
    "https://react.dev/errors/",
    "http://fb.me/use-check-prop-types",
)
_REMOTE_URL = re.compile(r"(?:https?|wss?)://[^\s\"'`<>]+", re.IGNORECASE)
_CSS_URL = re.compile(r"url\(\s*(?P<quote>['\"]?)(?P<value>.*?)\1\s*\)", re.IGNORECASE)
_CSS_IMPORT = re.compile(
    r"@import\s+(?!url\()(?P<quote>['\"])(?P<value>.*?)\1", re.IGNORECASE
)
_JS_DYNAMIC_IMPORT = re.compile(
    r"\bimport\s*\(\s*(?P<quote>['\"`])(?P<value>.*?)\1\s*\)"
)
_JS_STATIC_IMPORT = re.compile(
    r"\b(?:from\s*|import\s*)(?P<quote>['\"`])(?P<value>[^'\"`]+)\1"
)
_JS_LITERAL_FETCH = re.compile(r"\bfetch\s*\(\s*(?P<quote>['\"`])(?P<value>.*?)\1")
_JS_FETCH_ARGUMENT = re.compile(
    r"\bfetch\s*\(\s*(?P<value>"
    r"(?:['\"`])(?:.*?)(?:['\"`])|"
    r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.href)?"
    r")(?=\s*[,\)])"
)


class _StaticHTMLScanner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[str] = []
        self.inline_styles: list[str] = []
        self.inline_scripts: list[str] = []
        self.style_blocks: list[str] = []
        self._active_text_element: str | None = None
        self.invalid = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered_tag = tag.casefold()
        by_name = {name.casefold(): value for name, value in attrs if value is not None}
        if any(name.casefold().startswith("on") for name, _value in attrs):
            self.invalid = True
        if "srcdoc" in by_name:
            self.invalid = True
        if lowered_tag == "base":
            self.invalid = True
        if (
            lowered_tag == "meta"
            and by_name.get("http-equiv", "").casefold() == "refresh"
        ):
            self.invalid = True
        for name in ("src", "href", "action", "formaction", "poster", "data"):
            value = by_name.get(name)
            if value is not None:
                self.references.append(value)
        for name in ("srcset", "imagesrcset"):
            value = by_name.get(name)
            if value is not None:
                self.references.extend(
                    part.strip().split()[0] for part in value.split(",") if part.strip()
                )
        style = by_name.get("style")
        if style is not None:
            self.inline_styles.append(style)
        if lowered_tag in {"script", "style"} and "src" not in by_name:
            self._active_text_element = lowered_tag

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == self._active_text_element:
            self._active_text_element = None

    def handle_data(self, data: str) -> None:
        if self._active_text_element == "script":
            self.inline_scripts.append(data)
        elif self._active_text_element == "style":
            self.style_blocks.append(data)


def _static_reference_valid(
    dist: Path, owner: Path, raw: str, *, allow_data: bool = False
) -> bool:
    value = raw.strip()
    if not value or any(ord(character) < 0x20 for character in value):
        return False
    lowered = value.casefold()
    if value.startswith("#"):
        return True
    if lowered.startswith("data:"):
        return allow_data and lowered.startswith(
            ("data:image/", "data:font/", "data:application/font-")
        )
    parsed = urlsplit(value)
    if (
        parsed.scheme
        or parsed.netloc
        or value.startswith(("//", "/", "\\"))
        or "\\" in value
        or parsed.path.startswith("api/")
    ):
        return False
    if not parsed.path:
        return True
    target = (owner.parent / parsed.path).resolve(strict=False)
    try:
        target.relative_to(dist.resolve())
    except ValueError:
        return False
    return target.is_file() and not target.is_symlink()


def _remote_literals_valid(text: str) -> bool:
    for match in _REMOTE_URL.finditer(text):
        value = match.group(0).rstrip(".,;:)]}")
        if not value.startswith(_INERT_EXECUTABLE_URL_PREFIXES):
            return False
    return re.search(r"['\"`]//[A-Za-z0-9]", text) is None


def _css_references_valid(dist: Path, owner: Path, text: str) -> bool:
    values = [match.group("value") for match in _CSS_URL.finditer(text)]
    values.extend(match.group("value") for match in _CSS_IMPORT.finditer(text))
    return all(
        _static_reference_valid(dist, owner, value, allow_data=True) for value in values
    )


def _javascript_vectors_valid(dist: Path, owner: Path, text: str) -> bool:
    if re.search(
        r"\b(?:XMLHttpRequest|WebSocket|EventSource|importScripts|SharedWorker)\b|"
        r"\.sendBeacon\s*\(|navigator\.serviceWorker|URLSearchParams|"
        r"(?:window|globalThis)\.location\.(?:href|host|hostname|origin|pathname|search)|"
        r"document\.(?:URL|cookie|referrer)|(?:local|session)Storage|"
        r"process\.env|import\.meta\.env",
        text,
    ):
        return False
    imports = [match.group("value") for match in _JS_DYNAMIC_IMPORT.finditer(text)]
    imports.extend(match.group("value") for match in _JS_STATIC_IMPORT.finditer(text))
    if any(not _static_reference_valid(dist, owner, value) for value in imports):
        return False
    dynamic_imports = len(re.findall(r"\bimport\s*\(", text))
    if dynamic_imports != len(list(_JS_DYNAMIC_IMPORT.finditer(text))):
        return False
    literal_fetches = list(_JS_LITERAL_FETCH.finditer(text))
    if any(
        not _static_reference_valid(dist, owner, match.group("value"))
        for match in literal_fetches
    ):
        return False
    fetch_count = len(re.findall(r"\bfetch\s*\(", text))
    arguments = [match.group("value") for match in _JS_FETCH_ARGUMENT.finditer(text)]
    if fetch_count != len(arguments):
        return False
    dynamic = [
        argument for argument in arguments if not argument.startswith(("'", '"', "`"))
    ]
    href_fetches = [argument for argument in dynamic if argument.endswith(".href")]
    data_fetches = [argument for argument in dynamic if not argument.endswith(".href")]
    if href_fetches and (
        len(href_fetches) != 1 or "modulepreload" not in text.casefold()
    ):
        return False
    if data_fetches and (
        len(data_fetches) != 1
        or "./contextlab-viewer.v1.json" not in text
        or "application/json" not in text.casefold()
    ):
        return False
    return True


def _static_route_valid(root: Path) -> tuple[bool, int]:
    dist = root / "viewer/dist"
    index = dist / "index.html"
    if dist.is_symlink() or index.is_symlink() or not index.is_file():
        return False, 0
    all_paths = sorted(dist.rglob("*"), key=lambda path: path.as_posix())
    if any(path.is_symlink() for path in all_paths):
        return False, sum(path.is_file() for path in all_paths)
    assets = [path for path in all_paths if path.is_file()]
    if not assets:
        return False, 0
    for path in assets:
        if path.suffix.casefold() not in _EXECUTABLE_STATIC_SUFFIXES:
            continue
        try:
            text = _read_repository_bytes(
                root, path.relative_to(root), "G4 static route asset"
            ).decode("utf-8")
        except (G4GateError, UnicodeDecodeError):
            return False, len(assets)
        if not _remote_literals_valid(text):
            return False, len(assets)
        suffix = path.suffix.casefold()
        if suffix in {".html", ".htm", ".svg"}:
            scanner = _StaticHTMLScanner()
            try:
                scanner.feed(text)
            except Exception:
                return False, len(assets)
            if scanner.invalid or any(
                not _static_reference_valid(dist, path, reference, allow_data=True)
                for reference in scanner.references
            ):
                return False, len(assets)
            if any(
                not _css_references_valid(dist, path, style)
                for style in [*scanner.inline_styles, *scanner.style_blocks]
            ):
                return False, len(assets)
            if any(
                not _javascript_vectors_valid(dist, path, script)
                for script in scanner.inline_scripts
            ):
                return False, len(assets)
        elif suffix == ".css":
            if not _css_references_valid(dist, path, text):
                return False, len(assets)
        elif not _javascript_vectors_valid(dist, path, text):
            return False, len(assets)
    return True, len(assets)


def _static_assets_snapshot(root: Path) -> tuple[str | None, int]:
    dist = root / "viewer/dist"
    if not dist.is_dir() or dist.is_symlink():
        return None, 0
    rows: list[dict[str, str]] = []
    for path in sorted(dist.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            return None, len(rows)
        if not path.is_file():
            continue
        rows.append(
            {
                "path": path.relative_to(dist).as_posix(),
                "sha256": hashlib.sha256(
                    _read_repository_bytes(
                        root, path.relative_to(root), "G4 static asset"
                    )
                ).hexdigest(),
            }
        )
    if not rows:
        return None, 0
    return sha256_json(rows), len(rows)


def _is_placeholder_credential(candidate: bytes) -> bool:
    lowered = candidate.lower()
    if any(marker in lowered for marker in _CREDENTIAL_PLACEHOLDER_MARKERS):
        return True
    if lowered.startswith(b"sk-or-v1-"):
        material = candidate[len(b"sk-or-v1-") :]
    elif lowered.startswith(b"sk-ant-"):
        material = candidate.split(b"-", 3)[-1]
    elif lowered.startswith(b"github_pat_"):
        material = candidate[len(b"github_pat_") :]
    elif len(candidate) > 4 and candidate[3:4] == b"_":
        material = candidate[4:]
    elif candidate.startswith((b"AKIA", b"ASIA")):
        material = candidate[4:]
    else:
        material = candidate
    material = re.sub(rb"[^A-Za-z0-9]", b"", material)
    return len(set(material.lower())) < 5


def _contains_viewer_credential(payload: bytes) -> bool:
    for pattern in _VIEWER_CREDENTIAL_PATTERNS:
        for match in pattern.finditer(payload):
            candidate = match.group(1)
            if not _is_placeholder_credential(candidate):
                return True
    return False


def _viewer_secret_scan(root: Path) -> bool:
    for relative in (Path("viewer/public"), Path("viewer/dist")):
        directory = root / relative
        if not directory.is_dir() or directory.is_symlink():
            return False
        for path in directory.rglob("*"):
            if path.is_symlink():
                return False
            if path.is_file():
                try:
                    payload = _read_repository_bytes(
                        root, path.relative_to(root), "G4 secret-scan artifact"
                    )
                except G4GateError:
                    return False
                if _contains_viewer_credential(payload):
                    return False
                if any(marker in payload for marker in _VIEWER_LOCAL_LOCATION_MARKERS):
                    return False
    return True


def _run_check(command: list[str], cwd: Path) -> tuple[int, bytes]:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
    )
    return result.returncode, result.stdout + result.stderr


def run_g4_verification(root: Path | None = None) -> dict[str, Any]:
    """Run reproducible public viewer checks and save their content-free record."""

    from .viewer_export import (
        validate_viewer_artifact_pointers,
        validate_viewer_export,
    )

    repository = (root or repository_root()).resolve()
    g3_gate = _require_replayed_g3(repository)
    manifest, manifest_bytes = _read_json_snapshot(
        repository, G4_VIEWER_MANIFEST_PATH, "G4 viewer manifest"
    )
    export, export_bytes = _read_json_snapshot(
        repository, G4_VIEWER_EXPORT_PATH, "G4 viewer export"
    )
    if manifest.get(
        "schema_version"
    ) != "contextlab.viewer-export-manifest.v1" or not _valid_hash(manifest):
        raise G4GateError("G4 viewer manifest is invalid")
    try:
        validate_viewer_export(export)
        validate_viewer_artifact_pointers(export, repository)
        contract_valid = True
    except Exception:
        contract_valid = False

    npm_exit, npm_output = _run_check(["npm", "run", "check"], repository / "viewer")
    python_exit, python_output = _run_check(
        [
            sys.executable,
            "-m",
            "unittest",
            "tests.test_viewer_export",
            "tests.test_cli.G4ViewerCliTests",
            "tests.test_cli.G4GateCliTests",
            "tests.test_g4_gate",
        ],
        repository / "evaluation/v2",
    )
    route_valid, asset_count = _static_route_valid(repository)
    static_assets_sha, snapshot_count = _static_assets_snapshot(repository)
    boundaries = manifest.get("publication_boundaries")
    boundary_valid = bool(
        isinstance(boundaries, Mapping)
        and boundaries.get("sealed_artifacts_copied") is False
        and boundaries.get("protected_gold_copied") is False
        and boundaries.get("protected_scoring_packets_copied") is False
    )
    export_sha = hashlib.sha256(export_bytes).hexdigest()
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    verification: dict[str, Any] = {
        "schema_version": G4_VERIFICATION_SCHEMA,
        "viewer_manifest_sha256": manifest_sha,
        "viewer_export_sha256": export_sha,
        "checks": {
            "viewer_contract": contract_valid,
            "publication_binding": _publication_binding_valid(
                repository, manifest, export
            ),
            "public_artifact_hashes": _viewer_public_artifacts_valid(
                repository, manifest
            ),
            "sealed_boundary": boundary_valid,
            "static_route": route_valid and asset_count == snapshot_count,
            "viewer_npm_check": npm_exit == 0,
            "python_regression": python_exit == 0,
            "secret_scan": _viewer_secret_scan(repository),
            "g3_gate_replay": _g3_manifest_binding_valid(manifest, g3_gate),
        },
        "commands": [
            {
                "command": "npm run check",
                "exit_code": npm_exit,
                "output_sha256": hashlib.sha256(npm_output).hexdigest(),
            },
            {
                "command": "python viewer regression",
                "exit_code": python_exit,
                "output_sha256": hashlib.sha256(python_output).hexdigest(),
            },
        ],
        "static_asset_count": asset_count,
        "static_assets_sha256": static_assets_sha or "0" * 64,
    }
    verification["artifact_sha256"] = sha256_json(verification)
    validate_g4_verification(verification)
    path = repository / G4_VERIFICATION_PATH
    data = _json_bytes(verification)
    if path.exists():
        try:
            existing = _read_repository_bytes(
                repository, G4_VERIFICATION_PATH, "G4 verification"
            )
        except G4GateError:
            existing = None
        if existing == data:
            return verification
        raise G4GateError("immutable G4 verification differs")
    _create_only(repository, {path: data})
    return verification
