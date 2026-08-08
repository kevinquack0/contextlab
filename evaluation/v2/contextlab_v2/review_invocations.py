"""Execute and verify the two fixed-profile AI reviewers.

The gate-specific review modules validate review semantics.  This module owns
the missing operational proof: it launches the approved local CLI, captures
the CLI-native invocation identifier and output, and persists an immutable
sidecar that later gates replay.  No caller supplies an invocation identifier,
model, effort, completion time, or verdict envelope.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
from typing import Any

from .immutable_io import ImmutableIOError, read_bytes_snapshot
from .review_workspace import (
    PublicReviewWorkspaceError,
    collect_public_review_workspace,
    materialize_public_review_workspace,
    review_workspace_manifest_path,
    validate_public_review_workspace_manifest,
)
from .review import ReviewContractError, validate_grade
from .tasking import sha256_json


NATIVE_AI_REVIEW_SCHEMA = "contextlab.native-ai-review-invocation.v4"
NATIVE_AI_REVIEW_FILENAME = "native-invocation.json"
NATIVE_AI_OUTPUT_FILENAME = "native-output.jsonl"

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_UTC_SECOND = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_INVOCATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{7,255}\Z")
_BINDING_KEY = re.compile(r"[a-z][a-z0-9_]{1,63}\Z")
_MAX_NATIVE_OUTPUT_BYTES = 50 * 1024 * 1024
_MAX_NATIVE_SESSION_BYTES = 100 * 1024 * 1024
_MAX_CLAUDE_CREDENTIAL_BYTES = 64 * 1024
_MAX_CLAUDE_OAUTH_TOKEN_BYTES = 16 * 1024
_SYSTEM_ROOT = Path("/")
_SECURITY_EXECUTABLE = _SYSTEM_ROOT / "usr" / "bin" / "security"
_CODEX_EXECUTABLE = (
    _SYSTEM_ROOT / "Applications" / "ChatGPT.app" / "Contents" / "Resources" / "codex"
)
_CLAUDE_EXECUTABLE = _SYSTEM_ROOT / "opt" / "homebrew" / "bin" / "claude"
_CLAUDE_REVIEW_CONFIG_ROOT = Path.home() / ".contextlab-v2-native-review" / "claude"
_CODEX_PERMISSION_BOUNDARY = "codex-cli-read-only-public-workspace-v1"
_CLAUDE_PERMISSION_BOUNDARY = "claude-code-safe-mode-plan-read-only-public-workspace-v1"
_TRUSTED_EXECUTABLES = {
    "codex": _CODEX_EXECUTABLE,
    "claude": _CLAUDE_EXECUTABLE,
}

_PROFILES: dict[str, dict[str, str]] = {
    "gpt-5.6-sol-high": {
        "transport": "codex-cli",
        "invocation_source": "codex-subagent",
        "executable": "codex",
        "model": "gpt-5.6-sol",
        "effort": "high",
        "sandbox": "codex-cli-read-only-public-workspace",
        "credential_transport": "codex-home-auth-with-command-profile",
        "output_format": "jsonl",
        "executable_command_path": str(_CODEX_EXECUTABLE),
    },
    "gpt-5.6-terra-high": {
        "transport": "codex-cli",
        "invocation_source": "codex-subagent",
        "executable": "codex",
        "model": "gpt-5.6-terra",
        "effort": "high",
        "sandbox": "codex-cli-read-only-public-workspace",
        "credential_transport": "codex-home-auth-with-command-profile",
        "output_format": "jsonl",
        "executable_command_path": str(_CODEX_EXECUTABLE),
    },
    "claude-opus-5-medium": {
        "transport": "claude-cli",
        "invocation_source": "claude-cli",
        "executable": "claude",
        "model": "claude-opus-5",
        "effort": "medium",
        "sandbox": "plan-mode-read-only-public-workspace",
        "credential_transport": "claude-oauth-anonymous-fd",
        "output_format": "json",
        "executable_command_path": str(_CLAUDE_EXECUTABLE),
    },
}

_REVIEW_INSTRUCTIONS = {
    "g3-calibration": (
        "Inspect the exact blinded G3 calibration packet named by packet_path. "
        "Apply only the embedded frozen rubric, grade all 22 cells, preserve every "
        "blind_cell_id exactly, and report whether the rubric is ambiguous."
    ),
    "g3-gate": (
        "Inspect the exact pending G3 technical gate and all public artifacts it "
        "binds. Verify sealed-data containment, the public rubric/calibration "
        "record, lifecycle replay, metric derivation logic, failure reporting, and "
        "whether the technical disposition follows from the evidence. Per-answer "
        "static grade files and external sealed records are intentionally opaque; "
        "do not require their protected contents when the canonical gate replay, "
        "content-free commitments, and derivation code are inspectable. The "
        "ship-first protocol permits a restart-required panel only for a descriptive "
        "retain-simple demo when all three reviewers are internally consistent, no "
        "rubric ambiguity is reported, eligible_policies is empty, and promotion or "
        "ranking claims are explicitly forbidden."
    ),
    "g4-gate": (
        "Inspect the exact G4 viewer manifest, public export, static build, and "
        "verification record. Verify public-only projection, absence of protected "
        "grading data or secrets, route integrity, reproducibility, and the exact "
        "approved G3 binding. The public workspace includes the G3 gate and its "
        "public evidence, but intentionally excludes content-free sealed imports "
        "and native session sidecars that contain local machine paths. Those "
        "inputs remain validated and hash-bound outside the reviewer workspace. "
        "Do not fail solely because the canonical G3 native-provenance replay "
        "cannot be rerun inside this public-only workspace; inspect its binding "
        "and the G4 verification result instead."
    ),
    "frontier-entry": (
        "Inspect the exact F1-F7 entry evidence and pending gate. Verify every "
        "eligibility or failed-entry decision from current public bytes, the G4 "
        "barrier, containment, and all experiment-specific commitments. The "
        "workspace includes the approved G3 gate and its public evidence, but not "
        "the original Git tag used by the already-approved G3 freeze. Do not fail "
        "solely because that historical Git-tag replay is unavailable inside the "
        "public-only copy; inspect its approved binding and current public inputs."
    ),
    "frontier-result": (
        "Inspect the exact frontier technical record and every public source it "
        "binds. Verify current-byte hashes, the approved frontier-entry gate, the "
        "claim, the proposed decision, and that no unsupported result is promoted."
        " Hash fields have two distinct contracts: source_artifacts[].file_sha256 "
        "and public-review-workspace files[].sha256 bind raw file bytes, while "
        "artifact_sha256 and technical_record_sha256 bind canonical JSON values. "
        "Do not compare canonical JSON commitments with raw file-byte hashes. The "
        "frontier result's frontier_entry_gate_sha256 must equal the approved entry "
        "gate's artifact_sha256. The entry gate's technical_record_sha256 and human "
        "approval bind its own entry technical record, not this result record; a "
        "frontier-result approval does not exist until after both AI reviews pass."
    ),
}

_RESPONSE_SCHEMAS: dict[str, dict[str, Any]] = {
    "g3-calibration": {
        "type": "object",
        "additionalProperties": False,
        "required": ["grades", "rubric_ambiguous", "review_comment"],
        "properties": {
            "grades": {
                "type": "array",
                "minItems": 22,
                "maxItems": 22,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["blind_cell_id", "grade"],
                    "properties": {
                        "blind_cell_id": {"type": "string", "minLength": 1},
                        "grade": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": [
                                "overall_ordinal",
                                "factual_correctness",
                                "completeness",
                                "citation_support",
                                "authority_freshness",
                                "abstention_quality",
                                "accepted",
                                "failure_labels",
                                "comment",
                            ],
                            "properties": {
                                "overall_ordinal": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "maximum": 3,
                                },
                                "factual_correctness": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "maximum": 3,
                                },
                                "completeness": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "maximum": 3,
                                },
                                "citation_support": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "maximum": 3,
                                },
                                "authority_freshness": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "maximum": 3,
                                },
                                "abstention_quality": {
                                    "type": "string",
                                    "enum": [
                                        "not_applicable",
                                        "correct",
                                        "incorrect",
                                    ],
                                },
                                "accepted": {"type": "boolean"},
                                "failure_labels": {
                                    "type": "array",
                                    "uniqueItems": True,
                                    "items": {
                                        "type": "string",
                                        "enum": [
                                            "wrong_answer",
                                            "material_omission",
                                            "unsupported_material_claim",
                                            "stale_or_low_authority",
                                            "incorrect_abstention",
                                            "provider_or_format_failure",
                                        ],
                                    },
                                },
                                "comment": {"type": "string", "maxLength": 4000},
                            },
                        },
                    },
                },
            },
            "rubric_ambiguous": {"type": "boolean"},
            "review_comment": {"type": "string", "maxLength": 4000},
        },
    },
    "g3-gate": {
        "type": "object",
        "additionalProperties": False,
        "required": ["verdict", "blocking_findings"],
        "properties": {
            "verdict": {"type": "string", "enum": ["pass", "fail"]},
            "blocking_findings": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 2000},
                "maxItems": 100,
            },
        },
    },
    "g4-gate": {
        "type": "object",
        "additionalProperties": False,
        "required": ["decision", "p0_findings", "p1_findings"],
        "properties": {
            "decision": {"type": "string", "enum": ["pass", "fail"]},
            "p0_findings": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 2000},
                "maxItems": 100,
            },
            "p1_findings": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 2000},
                "maxItems": 100,
            },
        },
    },
    "frontier-entry": {
        "type": "object",
        "additionalProperties": False,
        "required": ["decision", "p0_findings", "p1_findings"],
        "properties": {
            "decision": {"type": "string", "enum": ["pass", "fail"]},
            "p0_findings": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 2000},
                "maxItems": 100,
            },
            "p1_findings": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 2000},
                "maxItems": 100,
            },
        },
    },
    "frontier-result": {
        "type": "object",
        "additionalProperties": False,
        "required": ["decision", "findings"],
        "properties": {
            "decision": {"type": "string", "enum": ["pass", "fail"]},
            "findings": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 2000},
                "maxItems": 100,
            },
        },
    },
}


class AIReviewInvocationError(ValueError):
    """A fixed-profile reviewer invocation is missing, failed, or forged."""


def reviewer_profile(reviewer_id: str) -> dict[str, str]:
    """Return one copy of the exact approved reviewer profile."""

    profile = _PROFILES.get(reviewer_id)
    if profile is None:
        raise AIReviewInvocationError("unknown AI reviewer profile")
    return dict(profile)


def native_review_paths(anchor_path: Path) -> tuple[Path, Path]:
    """Derive the immutable evidence/output paths for one review artifact."""

    if anchor_path.is_absolute() or ".." in anchor_path.parts:
        raise AIReviewInvocationError("AI review anchor must be repository-relative")
    if anchor_path.name == "invocation-receipt.json":
        return (
            anchor_path.with_name(NATIVE_AI_REVIEW_FILENAME),
            anchor_path.with_name(NATIVE_AI_OUTPUT_FILENAME),
        )
    return (
        anchor_path.with_name(f"{anchor_path.stem}.native-invocation.json"),
        anchor_path.with_name(f"{anchor_path.stem}.native-output.jsonl"),
    )


def _utc_now() -> str:
    return (
        dt.datetime.now(dt.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_hash_valid(value: Mapping[str, Any]) -> bool:
    return value.get("artifact_sha256") == sha256_json(
        {key: item for key, item in value.items() if key != "artifact_sha256"}
    )


def _normalize_bindings(bindings: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(bindings, Mapping) or not bindings:
        raise AIReviewInvocationError("AI review target bindings are missing")
    normalized: dict[str, str] = {}
    for key, value in bindings.items():
        if not isinstance(key, str) or _BINDING_KEY.fullmatch(key) is None:
            raise AIReviewInvocationError("AI review target binding name is invalid")
        if not isinstance(value, str) or not value or len(value) > 2_000:
            raise AIReviewInvocationError("AI review target binding value is invalid")
        normalized[key] = value
    return dict(sorted(normalized.items()))


def build_ai_review_prompt(
    *, review_kind: str, target_bindings: Mapping[str, Any]
) -> str:
    """Build the deterministic read-only prompt later replayed by the gate."""

    instruction = _REVIEW_INSTRUCTIONS.get(review_kind)
    schema = _RESPONSE_SCHEMAS.get(review_kind)
    if instruction is None or schema is None:
        raise AIReviewInvocationError("unknown AI review kind")
    bindings = _normalize_bindings(target_bindings)
    if review_kind == "frontier-result" and bindings.get("experiment_id") == "F3":
        instruction += (
            " F3 has one legacy field-name exception: the nested "
            "answer_quality_result/evidence_result artifact_sha256 values bind raw "
            "metric-file bytes and must equal the corresponding workspace "
            "files[].sha256. Each linked metric object separately binds its "
            "canonical JSON content in record_sha256. Treat this as a schema-name "
            "limitation, not a stale binding, only when both commitments replay and "
            "the linked record agrees with the enclosing cell."
        )
    return (
        "You are an independent ContextLab v2 gate reviewer. Work read-only inside "
        "the current public-review workspace. The workspace is an allowlisted, "
        "byte-bound copy; public-review-workspace.json lists every authorized file. "
        "Never attempt to access a parent, absolute path, home directory, original "
        "repository, sealed, protected, evaluation-only, gold-answer, secret, or "
        "grader-private data.\n\n"
        f"Review kind: {review_kind}\n"
        f"Task: {instruction}\n\n"
        "Exact target bindings:\n"
        f"{json.dumps(bindings, indent=2, sort_keys=True)}\n\n"
        "Fail if a binding is stale, a required byte cannot be inspected, or any P0/P1 "
        "issue remains. Return only one JSON object that conforms exactly to this "
        "schema; do not include Markdown or hidden reasoning:\n"
        f"{json.dumps(schema, indent=2, sort_keys=True)}"
    )


def _normalize_findings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 100:
        raise AIReviewInvocationError(f"{label} must be a bounded list")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > 2_000:
            raise AIReviewInvocationError(f"{label} contains an invalid finding")
        normalized.append(item.strip())
    return normalized


def validate_ai_review_response(review_kind: str, value: Any) -> dict[str, Any]:
    """Normalize one strict model response and reject semantic contradictions."""

    if not isinstance(value, Mapping):
        raise AIReviewInvocationError("AI reviewer response must be a JSON object")
    if review_kind == "g3-calibration":
        if set(value) != {"grades", "rubric_ambiguous", "review_comment"}:
            raise AIReviewInvocationError("G3 calibration response fields changed")
        rows = value.get("grades")
        if not isinstance(rows, list) or len(rows) != 22:
            raise AIReviewInvocationError("G3 calibration requires 22 grades")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping) or set(row) != {
                "blind_cell_id",
                "grade",
            }:
                raise AIReviewInvocationError("G3 calibration grade fields changed")
            blind_id = row.get("blind_cell_id")
            grade = row.get("grade")
            if not isinstance(blind_id, str) or not blind_id or blind_id in seen:
                raise AIReviewInvocationError("G3 calibration repeats a blind ID")
            if not isinstance(grade, dict):
                raise AIReviewInvocationError("G3 calibration grade is invalid")
            try:
                validate_grade(grade)
            except ReviewContractError as exc:
                raise AIReviewInvocationError(
                    "G3 calibration grade violates the rubric"
                ) from exc
            seen.add(blind_id)
            normalized.append({"blind_cell_id": blind_id, "grade": grade})
        ambiguous = value.get("rubric_ambiguous")
        comment = value.get("review_comment")
        if not isinstance(ambiguous, bool) or not isinstance(comment, str):
            raise AIReviewInvocationError("G3 calibration review metadata is invalid")
        if len(comment) > 4000:
            raise AIReviewInvocationError("G3 calibration comment is too long")
        return {
            "grades": normalized,
            "rubric_ambiguous": ambiguous,
            "review_comment": comment,
        }
    if review_kind == "g3-gate":
        if set(value) != {"verdict", "blocking_findings"}:
            raise AIReviewInvocationError("G3 AI response fields changed")
        verdict = value.get("verdict")
        findings = _normalize_findings(
            value.get("blocking_findings"), "G3 blocking findings"
        )
        if verdict not in {"pass", "fail"} or (verdict == "pass" and findings):
            raise AIReviewInvocationError("G3 AI response verdict is inconsistent")
        return {"verdict": verdict, "blocking_findings": findings}
    if review_kind in {"g4-gate", "frontier-entry"}:
        if set(value) != {"decision", "p0_findings", "p1_findings"}:
            raise AIReviewInvocationError("dual-AI response fields changed")
        decision = value.get("decision")
        p0 = _normalize_findings(value.get("p0_findings"), "P0 findings")
        p1 = _normalize_findings(value.get("p1_findings"), "P1 findings")
        if decision not in {"pass", "fail"} or (decision == "pass" and (p0 or p1)):
            raise AIReviewInvocationError("dual-AI response decision is inconsistent")
        return {"decision": decision, "p0_findings": p0, "p1_findings": p1}
    if review_kind == "frontier-result":
        if set(value) != {"decision", "findings"}:
            raise AIReviewInvocationError("frontier-result AI response fields changed")
        decision = value.get("decision")
        findings = _normalize_findings(value.get("findings"), "frontier findings")
        if decision not in {"pass", "fail"} or (decision == "pass" and findings):
            raise AIReviewInvocationError(
                "frontier-result AI response decision is inconsistent"
            )
        return {"decision": decision, "findings": findings}
    raise AIReviewInvocationError("unknown AI review kind")


def _json_object(text: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AIReviewInvocationError(f"{label} is not JSON") from exc
    if not isinstance(value, dict):
        raise AIReviewInvocationError(f"{label} must be a JSON object")
    return value


def _flat_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    usage = {
        str(key): item
        for key, item in value.items()
        if not isinstance(item, bool) and isinstance(item, int) and item >= 0
    }
    return dict(sorted(usage.items())) or None


def _parse_codex_output(raw: bytes, review_kind: str) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AIReviewInvocationError("Codex native output is not UTF-8") from exc
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        events.append(_json_object(line, "Codex JSONL event"))
    if not events:
        raise AIReviewInvocationError("Codex native output is empty")
    invocation_ids = {
        event.get("thread_id")
        for event in events
        if event.get("type") == "thread.started"
        and isinstance(event.get("thread_id"), str)
    }
    if len(invocation_ids) != 1:
        raise AIReviewInvocationError("Codex native thread identifier is missing")
    invocation_id = next(iter(invocation_ids))
    if _INVOCATION_ID.fullmatch(invocation_id) is None:
        raise AIReviewInvocationError("Codex native thread identifier is invalid")

    response_candidates: list[dict[str, Any]] = []
    usage: dict[str, int] | None = None
    for event in events:
        if event.get("type") == "item.completed":
            item = event.get("item")
            if isinstance(item, Mapping) and item.get("type") == "agent_message":
                message = item.get("text")
                if isinstance(message, str):
                    try:
                        response_candidates.append(
                            _json_object(message, "Codex response")
                        )
                    except AIReviewInvocationError:
                        continue
        if event.get("type") == "turn.completed":
            usage = _flat_usage(event.get("usage")) or usage
    if not response_candidates:
        raise AIReviewInvocationError("Codex native output has no JSON response")
    response = validate_ai_review_response(review_kind, response_candidates[-1])
    return {
        "native_invocation_id": invocation_id,
        "native_model_id": None,
        "response": response,
        "usage": usage,
    }


def _parse_claude_output(raw: bytes, review_kind: str, model: str) -> dict[str, Any]:
    try:
        envelope = _json_object(raw.decode("utf-8"), "Claude native output")
    except UnicodeDecodeError as exc:
        raise AIReviewInvocationError("Claude native output is not UTF-8") from exc
    if envelope.get("is_error") is not False:
        raise AIReviewInvocationError("Claude native invocation reported an error")
    invocation_id = envelope.get("session_id")
    if (
        not isinstance(invocation_id, str)
        or _INVOCATION_ID.fullmatch(invocation_id) is None
    ):
        raise AIReviewInvocationError("Claude native session identifier is invalid")
    model_usage = envelope.get("modelUsage")
    if not isinstance(model_usage, Mapping) or model not in model_usage:
        raise AIReviewInvocationError(
            "Claude native output does not prove the exact model"
        )
    structured = envelope.get("structured_output")
    if isinstance(structured, Mapping):
        response = validate_ai_review_response(review_kind, structured)
    else:
        response_text = envelope.get("result")
        if not isinstance(response_text, str):
            raise AIReviewInvocationError("Claude native result is missing")
        response = validate_ai_review_response(
            review_kind, _json_object(response_text, "Claude response")
        )
    return {
        "native_invocation_id": invocation_id,
        "native_model_id": model,
        "response": response,
        "usage": _flat_usage(envelope.get("usage")),
    }


def _parse_native_output(
    raw: bytes, *, reviewer_id: str, review_kind: str
) -> dict[str, Any]:
    profile = reviewer_profile(reviewer_id)
    if profile["transport"] == "codex-cli":
        return _parse_codex_output(raw, review_kind)
    return _parse_claude_output(raw, review_kind, profile["model"])


def _minimal_environment() -> dict[str, str]:
    """Expose only process basics; never forward ambient credentials or tokens."""

    allowed = {
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "NO_COLOR",
        "SHELL",
        "TERM",
        "TMPDIR",
        "USER",
    }
    environment = {
        key: value for key, value in os.environ.items() if key in allowed and value
    }
    environment["PATH"] = os.pathsep.join(
        str(path)
        for path in (
            _CODEX_EXECUTABLE.parent,
            _CLAUDE_EXECUTABLE.parent,
            _SYSTEM_ROOT / "usr" / "bin",
            _SYSTEM_ROOT / "bin",
            _SYSTEM_ROOT / "usr" / "sbin",
            _SYSTEM_ROOT / "sbin",
        )
    )
    return environment


def _reviewer_environment(
    reviewer_id: str, *, claude_config_root: Path | None = None
) -> dict[str, str]:
    """Return the credential-free environment for one locked reviewer CLI."""

    environment = _minimal_environment()
    if reviewer_profile(reviewer_id)["transport"] == "claude-cli":
        if claude_config_root is None:
            raise AIReviewInvocationError(
                "Claude reviewer config root was not isolated"
            )
        environment["CLAUDE_CONFIG_DIR"] = str(claude_config_root)
    return environment


def _claude_oauth_token_descriptor() -> int:
    """Load Claude subscription OAuth into an anonymous inherited pipe.

    The complete Keychain record is kept out of the child environment and the
    filesystem.  Only the access token is written to the pipe, and callers must
    close the returned read descriptor after the reviewer process starts.
    """

    try:
        executable = _SECURITY_EXECUTABLE.resolve(strict=True)
    except OSError as exc:
        raise AIReviewInvocationError(
            "Claude subscription credential bridge is unavailable"
        ) from exc
    if (
        executable != _SECURITY_EXECUTABLE
        or not executable.is_file()
        or executable.is_symlink()
    ):
        raise AIReviewInvocationError("Claude subscription credential bridge is unsafe")
    try:
        raw = subprocess.check_output(
            [
                str(executable),
                "find-generic-password",
                "-s",
                "Claude Code-credentials",
                "-w",
            ],
            env=_minimal_environment(),
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise AIReviewInvocationError(
            "Claude subscription credential is unavailable"
        ) from exc
    if not raw or len(raw) > _MAX_CLAUDE_CREDENTIAL_BYTES:
        raise AIReviewInvocationError("Claude subscription credential is invalid")
    try:
        credential = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AIReviewInvocationError(
            "Claude subscription credential is invalid"
        ) from exc
    oauth = credential.get("claudeAiOauth") if isinstance(credential, Mapping) else None
    token = oauth.get("accessToken") if isinstance(oauth, Mapping) else None
    if not isinstance(token, str) or not token:
        raise AIReviewInvocationError("Claude subscription OAuth token is unavailable")
    token_bytes = token.encode("utf-8")
    if len(token_bytes) > _MAX_CLAUDE_OAUTH_TOKEN_BYTES or b"\x00" in token_bytes:
        raise AIReviewInvocationError("Claude subscription OAuth token is invalid")

    read_descriptor = -1
    write_descriptor = -1
    try:
        read_descriptor, write_descriptor = os.pipe()
        written = 0
        while written < len(token_bytes):
            written += os.write(write_descriptor, token_bytes[written:])
        os.close(write_descriptor)
        write_descriptor = -1
        return read_descriptor
    except OSError as exc:
        if read_descriptor >= 0:
            os.close(read_descriptor)
        raise AIReviewInvocationError(
            "cannot create Claude subscription credential bridge"
        ) from exc
    finally:
        if write_descriptor >= 0:
            os.close(write_descriptor)


def _trusted_executable(profile: Mapping[str, str]) -> Path:
    """Resolve only the fixed Homebrew launcher selected by the locked profile."""

    name = profile["executable"]
    expected = _TRUSTED_EXECUTABLES.get(name)
    configured = profile.get("executable_command_path")
    if expected is None or configured != str(expected):
        raise AIReviewInvocationError("reviewer executable profile is not pinned")
    located = shutil.which(name, path=_minimal_environment()["PATH"])
    if located is None or Path(located) != expected:
        raise AIReviewInvocationError("trusted reviewer executable is unavailable")
    try:
        executable = expected.resolve(strict=True)
    except OSError as exc:
        raise AIReviewInvocationError(
            "trusted reviewer executable cannot be resolved"
        ) from exc
    if not executable.is_file() or executable.is_symlink():
        raise AIReviewInvocationError("trusted reviewer executable is unsafe")
    return executable


def _executable_version(executable: Path) -> str:
    try:
        raw = subprocess.check_output(
            [str(executable), "--version"],
            env=_minimal_environment(),
            stderr=subprocess.STDOUT,
            timeout=15,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise AIReviewInvocationError(
            "cannot attest reviewer executable version"
        ) from exc
    try:
        version = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise AIReviewInvocationError(
            "reviewer executable version is not UTF-8"
        ) from exc
    if not version or "\n" in version or len(version) > 200:
        raise AIReviewInvocationError("reviewer executable version is invalid")
    return version


def _containment_profile(reviewer_id: str) -> str:
    """Return the fixed permission boundary for one native reviewer process.

    Each CLI runs in an isolated public-only workspace with its built-in
    read-only or plan-mode boundary.
    """

    profile = reviewer_profile(reviewer_id)
    if profile["transport"] == "claude-cli":
        return _CLAUDE_PERMISSION_BOUNDARY
    return _CODEX_PERMISSION_BOUNDARY


def _containment_launcher(reviewer_id: str) -> tuple[Path, str]:
    name = reviewer_profile(reviewer_id)["executable"]
    try:
        executable = _TRUSTED_EXECUTABLES[name].resolve(strict=True)
    except OSError as exc:
        raise AIReviewInvocationError(
            "reviewer permission-boundary launcher is unavailable"
        ) from exc
    if not executable.is_file():
        raise AIReviewInvocationError("reviewer permission-boundary launcher is unsafe")
    return executable, _containment_profile(reviewer_id)


def _native_session_roots(reviewer_id: str) -> tuple[Path, ...]:
    if reviewer_profile(reviewer_id)["transport"] == "codex-cli":
        configured = os.environ.get("CODEX_HOME")
        if configured:
            codex_home = Path(configured).resolve(strict=False)
        else:
            home = Path(os.environ.get("HOME", "")).resolve(strict=False)
            if not str(home) or str(home) == ".":
                raise AIReviewInvocationError(
                    "native reviewer home directory is unavailable"
                )
            codex_home = home / ".codex"
        if not str(codex_home) or str(codex_home) == ".":
            raise AIReviewInvocationError(
                "native reviewer home directory is unavailable"
            )
        return (codex_home / "sessions",)
    return (_CLAUDE_REVIEW_CONFIG_ROOT,)


def _ensure_absolute_directory(path: Path) -> None:
    """Create one absolute directory without following a symlink component."""

    if not path.is_absolute() or not path.parts or path == Path("/"):
        raise AIReviewInvocationError("native reviewer session root is invalid")
    resolved = path.resolve(strict=False)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    try:
        for part in resolved.parts[1:]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
    except OSError as exc:
        raise AIReviewInvocationError("native reviewer session root is unsafe") from exc
    finally:
        os.close(descriptor)


def _native_session_events(path: Path) -> tuple[list[dict[str, Any]], bytes]:
    try:
        if path.is_symlink() or not path.is_file():
            raise AIReviewInvocationError(
                "native reviewer session is missing or unsafe"
            )
        size = path.stat().st_size
        if size <= 0 or size > _MAX_NATIVE_SESSION_BYTES:
            raise AIReviewInvocationError("native reviewer session size is invalid")
        raw = path.read_bytes()
    except OSError as exc:
        raise AIReviewInvocationError("cannot read native reviewer session") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AIReviewInvocationError("native reviewer session is not UTF-8") from exc
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        if line.strip():
            events.append(_json_object(line, "native reviewer session event"))
    if not events:
        raise AIReviewInvocationError("native reviewer session is empty")
    return events, raw


def _session_text(value: object) -> str | None:
    """Extract visible text from one native CLI message content value."""

    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return None
    chunks: list[str] = []
    for block in value:
        if (
            isinstance(block, Mapping)
            and block.get("type") in {"input_text", "output_text", "text"}
            and isinstance(block.get("text"), str)
        ):
            chunks.append(str(block["text"]))
    return "".join(chunks) if chunks else None


def _codex_session_messages(events: list[dict[str, Any]], *, role: str) -> set[str]:
    messages: set[str] = set()
    for event in events:
        payload = event.get("payload")
        if event.get("type") == "event_msg" and isinstance(payload, Mapping):
            expected_type = "user_message" if role == "user" else "agent_message"
            if payload.get("type") == expected_type:
                text = payload.get("message")
                if isinstance(text, str):
                    messages.add(text)
        if (
            event.get("type") == "response_item"
            and isinstance(payload, Mapping)
            and payload.get("type") == "message"
            and payload.get("role") == role
        ):
            text = _session_text(payload.get("content"))
            if text is not None:
                messages.add(text)
    return messages


def _claude_session_messages(events: list[dict[str, Any]], *, role: str) -> set[str]:
    messages: set[str] = set()
    for event in events:
        message = event.get("message")
        if (
            event.get("type") == role
            and isinstance(message, Mapping)
            and message.get("role", role) == role
        ):
            text = _session_text(message.get("content"))
            if text is not None:
                messages.add(text)
    return messages


def _claude_structured_responses(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    responses: list[dict[str, Any]] = []
    for event in events:
        message = event.get("message")
        if event.get("type") != "assistant" or not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if (
                isinstance(item, Mapping)
                and item.get("type") == "tool_use"
                and item.get("name") == "StructuredOutput"
                and isinstance(item.get("input"), Mapping)
            ):
                responses.append(dict(item["input"]))
    return responses


def _session_has_response(
    messages: set[str], expected_response: Mapping[str, Any]
) -> bool:
    for message in messages:
        try:
            parsed = json.loads(message)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, Mapping) and dict(parsed) == dict(expected_response):
            return True
    return False


def _attest_native_session(
    path: Path,
    *,
    reviewer_id: str,
    invocation_id: str,
    expected_prompt: str,
    expected_response: Mapping[str, Any],
) -> dict[str, str]:
    """Prove model, effort, prompt, and response from the native session."""

    profile = reviewer_profile(reviewer_id)
    events, raw = _native_session_events(path)
    if profile["transport"] == "codex-cli":
        identifiers = {
            str(value)
            for event in events
            if event.get("type") == "session_meta"
            and isinstance(event.get("payload"), Mapping)
            for value in (
                event["payload"].get("id"),
                event["payload"].get("session_id"),
            )
            if isinstance(value, str)
        }
        contexts = [
            event["payload"]
            for event in events
            if event.get("type") == "turn_context"
            and isinstance(event.get("payload"), Mapping)
        ]
        models = {item.get("model") for item in contexts}
        efforts = {item.get("effort") for item in contexts}
        prompt_messages = _codex_session_messages(events, role="user")
        response_messages = _codex_session_messages(events, role="assistant")
        structured_responses: list[dict[str, Any]] = []
    else:
        identifiers = {
            str(event["sessionId"])
            for event in events
            if isinstance(event.get("sessionId"), str)
        }
        assistant_events = [
            event
            for event in events
            if event.get("type") == "assistant"
            and isinstance(event.get("message"), Mapping)
        ]
        models = {event["message"].get("model") for event in assistant_events}
        # Claude Code 2.1 records the exact model and response, but not the
        # requested effort, in its JSONL transcript. The effort remains fixed
        # by the immutable command profile used to launch this session.
        efforts = {profile["effort"]}
        prompt_messages = _claude_session_messages(events, role="user")
        response_messages = _claude_session_messages(events, role="assistant")
        structured_responses = _claude_structured_responses(events)
    if invocation_id not in identifiers:
        raise AIReviewInvocationError("native session identifier does not match output")
    if models != {profile["model"]} or efforts != {profile["effort"]}:
        raise AIReviewInvocationError(
            "native session does not prove exact model and effort"
        )
    if expected_prompt not in prompt_messages:
        raise AIReviewInvocationError(
            "native session does not contain the exact prompt"
        )
    if (
        not _session_has_response(response_messages, expected_response)
        and dict(expected_response) not in structured_responses
    ):
        raise AIReviewInvocationError(
            "native session does not contain the exact structured response"
        )
    session_sha = hashlib.sha256(raw).hexdigest()
    attestation = {
        "reviewer_id": reviewer_id,
        "native_invocation_id": invocation_id,
        "native_model_id": profile["model"],
        "native_reasoning_effort": profile["effort"],
        "native_session_sha256": session_sha,
        "native_session_prompt_sha256": hashlib.sha256(
            expected_prompt.encode("utf-8")
        ).hexdigest(),
        "native_session_response_sha256": sha256_json(expected_response),
    }
    return {
        **attestation,
        "native_session_attestation_sha256": sha256_json(attestation),
    }


def _find_native_session(reviewer_id: str, invocation_id: str) -> Path:
    matches: set[Path] = set()
    pattern = (
        f"*{invocation_id}*.jsonl"
        if reviewer_profile(reviewer_id)["transport"] == "codex-cli"
        else f"{invocation_id}.jsonl"
    )
    for root in _native_session_roots(reviewer_id):
        if root.is_symlink() or not root.is_dir():
            continue
        for candidate in root.rglob(pattern):
            if candidate.is_file() and not candidate.is_symlink():
                matches.add(candidate.resolve(strict=True))
    if len(matches) != 1:
        raise AIReviewInvocationError("native reviewer session record is not unique")
    return next(iter(matches))


def _safe_target(root: Path, relative: Path, label: str) -> Path:
    repository = root.resolve()
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise AIReviewInvocationError(f"{label} escapes the repository")
    target = repository / relative
    current = repository
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise AIReviewInvocationError(f"{label} path contains a symlink")
        if current != target and current.exists() and not current.is_dir():
            raise AIReviewInvocationError(f"{label} parent is not a directory")
    try:
        target.resolve(strict=False).relative_to(repository)
    except ValueError as exc:
        raise AIReviewInvocationError(f"{label} escapes the repository") from exc
    return target


def _open_relative_directory(repository: Path, relative: Path) -> int:
    """Open or create a repository directory without following symlinks."""

    if relative.is_absolute() or ".." in relative.parts:
        raise AIReviewInvocationError("AI review evidence parent escapes repository")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        descriptor = os.open(repository, flags)
    except OSError as exc:
        raise AIReviewInvocationError("AI review repository root is unsafe") from exc
    try:
        for part in relative.parts:
            if part in {"", ".", ".."}:
                raise AIReviewInvocationError("AI review evidence parent is invalid")
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _write_create_only_plan(root: Path, plan: Mapping[Path, bytes]) -> None:
    """Publish a multi-file plan without overwrites or symlink traversal."""

    repository = root.resolve()
    if not plan:
        raise AIReviewInvocationError("AI review evidence plan is empty")
    entries: list[dict[str, Any]] = []
    try:
        for relative, data in plan.items():
            if not isinstance(relative, Path) or not isinstance(data, bytes):
                raise AIReviewInvocationError("AI review evidence plan is invalid")
            _safe_target(repository, relative, "AI review evidence")
            if not relative.name or relative.name in {".", ".."}:
                raise AIReviewInvocationError("AI review evidence name is invalid")
            parent_descriptor = _open_relative_directory(repository, relative.parent)
            try:
                try:
                    os.stat(
                        relative.name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    raise AIReviewInvocationError(
                        "AI review native evidence already exists"
                    )

                temporary_name: str | None = None
                temporary_descriptor: int | None = None
                for _ in range(10):
                    candidate = (
                        f".{relative.name}.contextlab-{secrets.token_hex(12)}.tmp"
                    )
                    try:
                        temporary_descriptor = os.open(
                            candidate,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600,
                            dir_fd=parent_descriptor,
                        )
                    except FileExistsError:
                        continue
                    temporary_name = candidate
                    break
                if temporary_descriptor is None or temporary_name is None:
                    raise AIReviewInvocationError(
                        "cannot allocate AI review evidence temporary file"
                    )
                temporary_stat: os.stat_result | None = None
                try:
                    written = 0
                    while written < len(data):
                        count = os.write(temporary_descriptor, data[written:])
                        if count <= 0:
                            raise OSError("short AI review evidence write")
                        written += count
                    os.fsync(temporary_descriptor)
                    temporary_stat = os.fstat(temporary_descriptor)
                    if not stat.S_ISREG(temporary_stat.st_mode):
                        raise AIReviewInvocationError(
                            "AI review evidence temporary file is unsafe"
                        )
                except Exception:
                    try:
                        os.unlink(temporary_name, dir_fd=parent_descriptor)
                    except OSError:
                        pass
                    raise
                finally:
                    os.close(temporary_descriptor)
                if temporary_stat is None:
                    raise AIReviewInvocationError(
                        "AI review evidence temporary file is unavailable"
                    )
                entries.append(
                    {
                        "parent_descriptor": parent_descriptor,
                        "target_name": relative.name,
                        "temporary_name": temporary_name,
                        "device": temporary_stat.st_dev,
                        "inode": temporary_stat.st_ino,
                        "linked": False,
                    }
                )
            except Exception:
                if not any(
                    entry.get("parent_descriptor") == parent_descriptor
                    for entry in entries
                ):
                    os.close(parent_descriptor)
                raise

        for entry in entries:
            descriptor = entry["parent_descriptor"]
            os.link(
                entry["temporary_name"],
                entry["target_name"],
                src_dir_fd=descriptor,
                dst_dir_fd=descriptor,
                follow_symlinks=False,
            )
            entry["linked"] = True
            target_stat = os.stat(
                entry["target_name"],
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            if (
                target_stat.st_dev != entry["device"]
                or target_stat.st_ino != entry["inode"]
                or not stat.S_ISREG(target_stat.st_mode)
            ):
                raise AIReviewInvocationError(
                    "AI review evidence changed during publication"
                )
            os.unlink(entry["temporary_name"], dir_fd=descriptor)
            entry["temporary_name"] = None
            os.fsync(descriptor)

        for entry in entries:
            target_stat = os.stat(
                entry["target_name"],
                dir_fd=entry["parent_descriptor"],
                follow_symlinks=False,
            )
            if (
                target_stat.st_dev != entry["device"]
                or target_stat.st_ino != entry["inode"]
                or not stat.S_ISREG(target_stat.st_mode)
            ):
                raise AIReviewInvocationError(
                    "AI review evidence changed after publication"
                )
    except Exception as exc:
        for entry in reversed(entries):
            descriptor = entry["parent_descriptor"]
            if entry["linked"]:
                try:
                    target_stat = os.stat(
                        entry["target_name"],
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                    if (
                        target_stat.st_dev == entry["device"]
                        and target_stat.st_ino == entry["inode"]
                    ):
                        os.unlink(entry["target_name"], dir_fd=descriptor)
                except OSError:
                    pass
            temporary_name = entry["temporary_name"]
            if temporary_name is not None:
                try:
                    temporary_stat = os.stat(
                        temporary_name,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                    if (
                        temporary_stat.st_dev == entry["device"]
                        and temporary_stat.st_ino == entry["inode"]
                    ):
                        os.unlink(temporary_name, dir_fd=descriptor)
                except OSError:
                    pass
            try:
                os.fsync(descriptor)
            except OSError:
                pass
        if isinstance(exc, AIReviewInvocationError):
            raise
        raise AIReviewInvocationError("cannot persist AI review evidence") from exc
    finally:
        for entry in entries:
            try:
                os.close(entry["parent_descriptor"])
            except OSError:
                pass


def _command(
    *,
    executable: Path,
    profile: Mapping[str, str],
    root: Path,
    prompt: str,
    schema_path: Path,
) -> list[str]:
    if profile["transport"] == "codex-cli":
        return [
            str(executable),
            "--ask-for-approval",
            "never",
            "exec",
            "--model",
            profile["model"],
            "-c",
            f'model_reasoning_effort="{profile["effort"]}"',
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--json",
            "--output-schema",
            str(schema_path),
            "-C",
            str(root),
            prompt,
        ]
    return [
        str(executable),
        "--print",
        "--model",
        profile["model"],
        "--effort",
        profile["effort"],
        "--permission-mode",
        "plan",
        "--safe-mode",
        "--disable-slash-commands",
        "--no-chrome",
        "--strict-mcp-config",
        "--tools",
        "Read,Glob,Grep",
        "--output-format",
        "json",
        "--json-schema",
        schema_path.read_text(encoding="utf-8"),
        prompt,
    ]


def _evidence_profile(profile: Mapping[str, str]) -> dict[str, str]:
    return {
        key: profile[key]
        for key in (
            "transport",
            "invocation_source",
            "model",
            "effort",
            "sandbox",
            "credential_transport",
            "output_format",
        )
    }


def run_and_record_ai_review(
    root: Path,
    *,
    anchor_path: Path,
    reviewer_id: str,
    review_kind: str,
    target_bindings: Mapping[str, Any],
    timeout_seconds: int = 1_800,
    response_validator: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run one fixed native reviewer and immutably save its execution proof."""

    repository = root.resolve()
    profile = reviewer_profile(reviewer_id)
    bindings = _normalize_bindings(target_bindings)
    prompt = build_ai_review_prompt(review_kind=review_kind, target_bindings=bindings)
    schema = _RESPONSE_SCHEMAS.get(review_kind)
    if schema is None:
        raise AIReviewInvocationError("unknown AI review kind")
    evidence_path, output_path = native_review_paths(anchor_path)
    workspace_manifest_path = review_workspace_manifest_path(anchor_path)
    for relative in (evidence_path, output_path):
        path = _safe_target(repository, relative, "AI review evidence")
        if path.exists() or path.is_symlink():
            raise AIReviewInvocationError("AI review native evidence already exists")

    try:
        workspace_manifest = collect_public_review_workspace(
            repository,
            review_kind=review_kind,
            target_bindings=bindings,
        )
    except PublicReviewWorkspaceError as exc:
        raise AIReviewInvocationError("cannot build public reviewer workspace") from exc
    workspace_manifest_bytes = (
        json.dumps(workspace_manifest, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    workspace_manifest_target = _safe_target(
        repository, workspace_manifest_path, "AI review workspace manifest"
    )
    reuse_workspace_manifest = workspace_manifest_target.exists()
    if workspace_manifest_target.is_symlink():
        raise AIReviewInvocationError("AI review workspace manifest is unsafe")
    if reuse_workspace_manifest:
        try:
            existing_manifest = read_bytes_snapshot(
                repository, workspace_manifest_path
            )
        except ImmutableIOError as exc:
            raise AIReviewInvocationError(
                "AI review workspace manifest is unsafe"
            ) from exc
        if existing_manifest != workspace_manifest_bytes:
            raise AIReviewInvocationError(
                "AI review workspace manifest differs between reviewers"
            )
    executable = _trusted_executable(profile)
    executable_version = _executable_version(executable)
    containment_launcher, containment_profile = _containment_launcher(reviewer_id)
    for session_root in _native_session_roots(reviewer_id):
        _ensure_absolute_directory(session_root)
    claude_config_root: Path | None = None
    if profile["transport"] == "claude-cli":
        claude_config_root = (
            _CLAUDE_REVIEW_CONFIG_ROOT
            / "invocations"
            / f"review-{secrets.token_hex(16)}"
        )
        _ensure_absolute_directory(claude_config_root)

    started_at = _utc_now()
    with tempfile.TemporaryDirectory(
        prefix="contextlab-ai-review-", dir="/tmp"
    ) as directory:
        temporary_root = Path(directory).resolve(strict=True)
        workspace = temporary_root / "public-review"
        workspace.mkdir()
        try:
            materialize_public_review_workspace(
                repository, workspace, workspace_manifest
            )
        except PublicReviewWorkspaceError as exc:
            raise AIReviewInvocationError(
                "cannot materialize public reviewer workspace"
            ) from exc
        schema_path = temporary_root / "response-schema.json"
        schema_path.write_text(
            json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        reviewer_command = _command(
            executable=executable,
            profile=profile,
            root=workspace,
            prompt=prompt,
            schema_path=schema_path,
        )
        command = reviewer_command
        environment = _reviewer_environment(
            reviewer_id, claude_config_root=claude_config_root
        )
        if profile["transport"] == "claude-cli":
            claude_tmp = temporary_root / "claude-tmp"
            claude_tmp.mkdir(mode=0o700)
            environment["CLAUDE_CODE_TMPDIR"] = str(claude_tmp)
            environment["TMPDIR"] = str(claude_tmp)
        credential_descriptor: int | None = None
        try:
            if profile["transport"] == "claude-cli":
                credential_descriptor = _claude_oauth_token_descriptor()
                environment["CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR"] = str(
                    credential_descriptor
                )
            completed = subprocess.run(
                command,
                cwd=workspace,
                env=environment,
                pass_fds=(
                    (credential_descriptor,)
                    if credential_descriptor is not None
                    else ()
                ),
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AIReviewInvocationError(
                "native AI reviewer invocation failed"
            ) from exc
        finally:
            if credential_descriptor is not None:
                os.close(credential_descriptor)
    completed_at = _utc_now()
    if completed.returncode != 0:
        raise AIReviewInvocationError(
            f"native AI reviewer exited {completed.returncode}"
        )
    raw = completed.stdout
    if not raw or len(raw) > _MAX_NATIVE_OUTPUT_BYTES:
        raise AIReviewInvocationError("native AI reviewer output size is invalid")
    parsed = _parse_native_output(raw, reviewer_id=reviewer_id, review_kind=review_kind)
    session_path = _find_native_session(reviewer_id, parsed["native_invocation_id"])
    session_attestation = _attest_native_session(
        session_path,
        reviewer_id=reviewer_id,
        invocation_id=parsed["native_invocation_id"],
        expected_prompt=prompt,
        expected_response=parsed["response"],
    )
    if parsed["native_model_id"] not in {
        None,
        session_attestation["native_model_id"],
    }:
        raise AIReviewInvocationError("native output and session model differ")
    response = parsed["response"]
    if response_validator is not None:
        response_validator(response)
    usage = parsed["usage"]
    evidence: dict[str, Any] = {
        "schema_version": NATIVE_AI_REVIEW_SCHEMA,
        "reviewer_id": reviewer_id,
        "review_kind": review_kind,
        "profile": _evidence_profile(profile),
        "native_invocation_id": parsed["native_invocation_id"],
        "native_model_id": session_attestation["native_model_id"],
        "native_reasoning_effort": session_attestation["native_reasoning_effort"],
        "native_session_path": str(session_path),
        "native_session_sha256": session_attestation["native_session_sha256"],
        "native_session_prompt_sha256": session_attestation[
            "native_session_prompt_sha256"
        ],
        "native_session_response_sha256": session_attestation[
            "native_session_response_sha256"
        ],
        "native_session_attestation_sha256": session_attestation[
            "native_session_attestation_sha256"
        ],
        "status": "completed",
        "started_at": started_at,
        "completed_at": completed_at,
        "exit_code": completed.returncode,
        "anchor_path": anchor_path.as_posix(),
        "native_output_path": output_path.as_posix(),
        "review_workspace_manifest_path": workspace_manifest_path.as_posix(),
        "review_workspace_manifest_sha256": workspace_manifest["artifact_sha256"],
        "review_workspace_file_count": workspace_manifest["file_count"],
        "review_workspace_total_bytes": workspace_manifest["total_bytes"],
        "target_bindings": bindings,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "response_schema_sha256": sha256_json(schema),
        "response_payload_sha256": sha256_json(response),
        "native_output_sha256": hashlib.sha256(raw).hexdigest(),
        "stderr_sha256": hashlib.sha256(completed.stderr).hexdigest(),
        "executable_command_path": profile["executable_command_path"],
        "executable_path": str(executable),
        "executable_sha256": _file_sha256(executable),
        "executable_version": executable_version,
        "containment_launcher_path": str(containment_launcher),
        "containment_launcher_sha256": _file_sha256(containment_launcher),
        "containment_profile_sha256": hashlib.sha256(
            containment_profile.encode("utf-8")
        ).hexdigest(),
        "usage_available": usage is not None,
        "usage": usage,
    }
    evidence["artifact_sha256"] = sha256_json(evidence)
    _validate_evidence_shape(evidence)
    if reuse_workspace_manifest:
        try:
            current_manifest = read_bytes_snapshot(
                repository, workspace_manifest_path
            )
        except ImmutableIOError as exc:
            raise AIReviewInvocationError(
                "AI review workspace manifest changed during review"
            ) from exc
        if current_manifest != workspace_manifest_bytes:
            raise AIReviewInvocationError(
                "AI review workspace manifest changed during review"
            )
    plan = {
        output_path: raw,
        evidence_path: (
            json.dumps(evidence, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8"),
    }
    if not reuse_workspace_manifest:
        plan[workspace_manifest_path] = workspace_manifest_bytes
    _write_create_only_plan(repository, plan)
    return {"evidence": evidence, "response": response}


def _validate_evidence_shape(value: Mapping[str, Any]) -> None:
    expected = {
        "schema_version",
        "reviewer_id",
        "review_kind",
        "profile",
        "native_invocation_id",
        "native_model_id",
        "native_reasoning_effort",
        "native_session_path",
        "native_session_sha256",
        "native_session_prompt_sha256",
        "native_session_response_sha256",
        "native_session_attestation_sha256",
        "status",
        "started_at",
        "completed_at",
        "exit_code",
        "anchor_path",
        "native_output_path",
        "review_workspace_manifest_path",
        "review_workspace_manifest_sha256",
        "review_workspace_file_count",
        "review_workspace_total_bytes",
        "target_bindings",
        "prompt_sha256",
        "response_schema_sha256",
        "response_payload_sha256",
        "native_output_sha256",
        "stderr_sha256",
        "executable_command_path",
        "executable_path",
        "executable_sha256",
        "executable_version",
        "containment_launcher_path",
        "containment_launcher_sha256",
        "containment_profile_sha256",
        "usage_available",
        "usage",
        "artifact_sha256",
    }
    if set(value) != expected or value.get("schema_version") != NATIVE_AI_REVIEW_SCHEMA:
        raise AIReviewInvocationError("native AI review evidence fields changed")
    reviewer_id = value.get("reviewer_id")
    if not isinstance(reviewer_id, str) or value.get("profile") != _evidence_profile(
        reviewer_profile(reviewer_id)
    ):
        raise AIReviewInvocationError("native AI reviewer profile changed")
    if value.get("review_kind") not in _REVIEW_INSTRUCTIONS:
        raise AIReviewInvocationError("native AI review kind changed")
    invocation_id = value.get("native_invocation_id")
    if (
        not isinstance(invocation_id, str)
        or _INVOCATION_ID.fullmatch(invocation_id) is None
    ):
        raise AIReviewInvocationError("native AI invocation identifier is invalid")
    profile = reviewer_profile(reviewer_id)
    native_model = value.get("native_model_id")
    if native_model != profile["model"]:
        raise AIReviewInvocationError("native reviewer model evidence changed")
    if value.get("native_reasoning_effort") != profile["effort"]:
        raise AIReviewInvocationError("native reviewer effort evidence changed")
    session_path = value.get("native_session_path")
    if not isinstance(session_path, str) or not Path(session_path).is_absolute():
        raise AIReviewInvocationError("native reviewer session path is invalid")
    if value.get("status") != "completed" or value.get("exit_code") != 0:
        raise AIReviewInvocationError("native AI invocation was not successful")
    for field in ("started_at", "completed_at"):
        timestamp = value.get(field)
        if not isinstance(timestamp, str) or _UTC_SECOND.fullmatch(timestamp) is None:
            raise AIReviewInvocationError("native AI timestamp is invalid")
    for field in (
        "prompt_sha256",
        "response_schema_sha256",
        "response_payload_sha256",
        "native_output_sha256",
        "stderr_sha256",
        "executable_sha256",
        "native_session_sha256",
        "native_session_prompt_sha256",
        "native_session_response_sha256",
        "native_session_attestation_sha256",
        "review_workspace_manifest_sha256",
        "containment_launcher_sha256",
        "containment_profile_sha256",
    ):
        digest = value.get(field)
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise AIReviewInvocationError("native AI evidence hash is invalid")
    for field in (
        "anchor_path",
        "native_output_path",
        "review_workspace_manifest_path",
        "executable_command_path",
        "executable_path",
        "executable_version",
        "containment_launcher_path",
    ):
        if not isinstance(value.get(field), str) or not value[field]:
            raise AIReviewInvocationError("native AI evidence path is invalid")
    if value.get("executable_command_path") != profile["executable_command_path"]:
        raise AIReviewInvocationError("reviewer executable command path changed")
    for field in ("review_workspace_file_count", "review_workspace_total_bytes"):
        item = value.get(field)
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            raise AIReviewInvocationError("public review workspace totals are invalid")
    expected_session_attestation = {
        "reviewer_id": reviewer_id,
        "native_invocation_id": invocation_id,
        "native_model_id": native_model,
        "native_reasoning_effort": profile["effort"],
        "native_session_sha256": value["native_session_sha256"],
        "native_session_prompt_sha256": value["native_session_prompt_sha256"],
        "native_session_response_sha256": value["native_session_response_sha256"],
    }
    if value.get("native_session_attestation_sha256") != sha256_json(
        expected_session_attestation
    ):
        raise AIReviewInvocationError("native reviewer session attestation changed")
    _normalize_bindings(value.get("target_bindings"))
    usage_available = value.get("usage_available")
    usage = value.get("usage")
    if usage_available is False:
        if usage is not None:
            raise AIReviewInvocationError("unavailable native usage must be null")
    elif usage_available is True:
        if not isinstance(usage, Mapping) or not usage or _flat_usage(usage) != usage:
            raise AIReviewInvocationError("native usage is invalid")
    else:
        raise AIReviewInvocationError("native usage availability is invalid")
    if not _artifact_hash_valid(value):
        raise AIReviewInvocationError("native AI evidence artifact hash changed")


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AIReviewInvocationError(f"cannot read {label}") from exc
    if not isinstance(value, dict):
        raise AIReviewInvocationError(f"{label} must be a JSON object")
    return value


def _validate_current_review_targets(
    repository: Path,
    workspace_manifest: Mapping[str, Any],
    bindings: Mapping[str, str],
) -> None:
    rows = {
        str(row["path"]): row
        for row in workspace_manifest["files"]
        if isinstance(row, Mapping) and isinstance(row.get("path"), str)
    }
    path_bindings = {
        value
        for key, value in bindings.items()
        if key == "path" or key.endswith("_path")
    }
    if not path_bindings:
        raise AIReviewInvocationError("public reviewer workspace has no bound target")
    for value in path_bindings:
        row = rows.get(value)
        if row is None:
            raise AIReviewInvocationError("public reviewer target was not reviewed")
        try:
            raw = read_bytes_snapshot(repository, Path(value))
        except ImmutableIOError as exc:
            raise AIReviewInvocationError(
                "public reviewer target is missing or unsafe"
            ) from exc
        if len(raw) != row.get("size_bytes") or hashlib.sha256(
            raw
        ).hexdigest() != row.get("sha256"):
            raise AIReviewInvocationError("public reviewer workspace changed")


def validate_recorded_ai_review(
    root: Path,
    *,
    anchor_path: Path,
    reviewer_id: str,
    review_kind: str,
    target_bindings: Mapping[str, Any],
    expected_response: Mapping[str, Any],
    invocation_id: str,
    completed_at: str,
) -> dict[str, Any]:
    """Replay one immutable native sidecar against an exact gate review."""

    repository = root.resolve()
    evidence_relative, output_relative = native_review_paths(anchor_path)
    workspace_relative = review_workspace_manifest_path(anchor_path)
    evidence_path = _safe_target(repository, evidence_relative, "AI review evidence")
    output_path = _safe_target(repository, output_relative, "AI review output")
    workspace_path = _safe_target(
        repository, workspace_relative, "public review workspace manifest"
    )
    if any(
        path.is_symlink() or not path.is_file()
        for path in (evidence_path, output_path, workspace_path)
    ):
        raise AIReviewInvocationError("native AI review evidence is missing or unsafe")
    evidence = _load_json(evidence_path, "native AI review evidence")
    _validate_evidence_shape(evidence)
    bindings = _normalize_bindings(target_bindings)
    response = validate_ai_review_response(review_kind, expected_response)
    prompt = build_ai_review_prompt(review_kind=review_kind, target_bindings=bindings)
    if (
        evidence.get("reviewer_id") != reviewer_id
        or evidence.get("review_kind") != review_kind
        or evidence.get("anchor_path") != anchor_path.as_posix()
        or evidence.get("native_output_path") != output_relative.as_posix()
        or evidence.get("review_workspace_manifest_path")
        != workspace_relative.as_posix()
        or evidence.get("target_bindings") != bindings
        or evidence.get("prompt_sha256")
        != hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        or evidence.get("response_schema_sha256")
        != sha256_json(_RESPONSE_SCHEMAS[review_kind])
        or evidence.get("response_payload_sha256") != sha256_json(response)
        or evidence.get("native_session_prompt_sha256") != evidence.get("prompt_sha256")
        or evidence.get("native_session_response_sha256")
        != evidence.get("response_payload_sha256")
        or evidence.get("native_invocation_id") != invocation_id
        or evidence.get("completed_at") != completed_at
    ):
        raise AIReviewInvocationError("native AI review binding changed")

    workspace_manifest = _load_json(workspace_path, "public review workspace manifest")
    try:
        validate_public_review_workspace_manifest(workspace_manifest)
    except PublicReviewWorkspaceError as exc:
        raise AIReviewInvocationError(
            "public reviewer workspace cannot be replayed"
        ) from exc
    if (
        evidence.get("review_workspace_manifest_sha256")
        != workspace_manifest.get("artifact_sha256")
        or evidence.get("review_workspace_file_count")
        != workspace_manifest.get("file_count")
        or evidence.get("review_workspace_total_bytes")
        != workspace_manifest.get("total_bytes")
    ):
        raise AIReviewInvocationError("public reviewer workspace changed")
    _validate_current_review_targets(repository, workspace_manifest, bindings)

    profile = reviewer_profile(reviewer_id)
    executable = _trusted_executable(profile)
    if (
        evidence.get("executable_path") != str(executable)
        or evidence.get("executable_sha256") != _file_sha256(executable)
        or evidence.get("executable_version") != _executable_version(executable)
    ):
        raise AIReviewInvocationError("reviewer executable identity changed")
    containment_launcher, containment_profile = _containment_launcher(reviewer_id)
    if (
        evidence.get("containment_launcher_path") != str(containment_launcher)
        or evidence.get("containment_launcher_sha256")
        != _file_sha256(containment_launcher)
        or evidence.get("containment_profile_sha256")
        != hashlib.sha256(containment_profile.encode("utf-8")).hexdigest()
    ):
        raise AIReviewInvocationError("reviewer containment identity changed")

    session_value = Path(str(evidence["native_session_path"]))
    try:
        session_path = session_value.resolve(strict=True)
    except OSError as exc:
        raise AIReviewInvocationError("native reviewer session is unavailable") from exc
    allowed_session = False
    for session_root in _native_session_roots(reviewer_id):
        try:
            session_path.relative_to(session_root.resolve(strict=True))
        except (OSError, ValueError):
            continue
        allowed_session = True
        break
    if (
        not allowed_session
        or session_value != session_path
        or session_value.is_symlink()
        or not session_value.is_file()
    ):
        raise AIReviewInvocationError("native reviewer session path is unsafe")
    session_attestation = _attest_native_session(
        session_path,
        reviewer_id=reviewer_id,
        invocation_id=invocation_id,
        expected_prompt=prompt,
        expected_response=response,
    )
    for field in (
        "native_model_id",
        "native_reasoning_effort",
        "native_session_sha256",
        "native_session_prompt_sha256",
        "native_session_response_sha256",
        "native_session_attestation_sha256",
    ):
        if evidence.get(field) != session_attestation[field]:
            raise AIReviewInvocationError("native reviewer session evidence changed")

    raw = output_path.read_bytes()
    if evidence.get("native_output_sha256") != hashlib.sha256(raw).hexdigest():
        raise AIReviewInvocationError("native AI output hash changed")
    parsed = _parse_native_output(raw, reviewer_id=reviewer_id, review_kind=review_kind)
    if (
        parsed["native_invocation_id"] != invocation_id
        or parsed["native_model_id"] not in {None, evidence.get("native_model_id")}
        or parsed["response"] != response
        or parsed["usage"] != evidence.get("usage")
    ):
        raise AIReviewInvocationError("native AI output differs from its evidence")

    evidence_root = repository / "results/v2"
    if evidence_root.is_dir() and not evidence_root.is_symlink():
        for other in evidence_root.rglob("*native-invocation.json"):
            if other == evidence_path or other.is_symlink() or not other.is_file():
                continue
            other_value = _load_json(other, "other native AI review evidence")
            _validate_evidence_shape(other_value)
            if other_value.get(
                "native_invocation_id"
            ) == invocation_id or other_value.get(
                "native_output_sha256"
            ) == evidence.get("native_output_sha256"):
                raise AIReviewInvocationError(
                    "native AI invocation identifier or output was reused"
                )
    return evidence


def native_proof_fields(
    anchor_path: Path, evidence: Mapping[str, Any]
) -> dict[str, str]:
    """Return the gate-receipt fields bound to one validated native sidecar."""

    _validate_evidence_shape(evidence)
    evidence_path, _ = native_review_paths(anchor_path)
    return {
        "native_invocation_evidence_path": evidence_path.as_posix(),
        "native_invocation_evidence_sha256": str(evidence["artifact_sha256"]),
        "native_output_sha256": str(evidence["native_output_sha256"]),
    }


def assert_native_proof_fields(
    *,
    anchor_path: Path,
    receipt: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> None:
    """Require a gate receipt to name the exact recorded native evidence."""

    expected = native_proof_fields(anchor_path, evidence)
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise AIReviewInvocationError("gate receipt native invocation proof changed")
