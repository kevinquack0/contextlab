"""Native, packet-by-packet AI execution for the protected final review.

All packet bytes, model responses, native output, and invocation proofs remain
outside the repository.  The repository may retain only the content-free hash
of the validated manifest through the normal final-review import receipt.
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
import subprocess
import tempfile
from typing import Any

from . import review_invocations as native
from .review import (
    REVIEWERS,
    ReviewContractError,
    harden_external_review_file,
    read_external_bytes_snapshot,
    validate_grade,
    write_external_bytes_once_or_verify,
)
from .tasking import sha256_json


FINAL_REVIEW_AI_INVOCATION_SCHEMA = "contextlab.final-review-native-ai-invocation.v1"
FINAL_REVIEW_AI_MANIFEST_SCHEMA = "contextlab.final-review-native-ai-manifest.v1"

_AI_REVIEWERS = REVIEWERS[:2]
_PHASE_RETURN_SCHEMAS = {
    "calibration": "contextlab.final-review-calibration-return.v1",
    "review": "contextlab.final-review-return.v1",
}
_EXPECTED_PACKET_COUNTS = {"calibration": 1, "review": 84}
_EXPECTED_GRADE_COUNTS = {"calibration": 20, "review": 1680}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_UTC_SECOND = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_INVOCATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{7,255}\Z")
_MAX_NATIVE_OUTPUT_BYTES = 50 * 1024 * 1024


class FinalReviewAIError(ValueError):
    """A final-review AI invocation or its native proof is incomplete."""


PacketExecutor = Callable[..., Mapping[str, Any]]
NativeProofValidator = Callable[..., None]


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _utc_now() -> str:
    return (
        dt.datetime.now(dt.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _require_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise FinalReviewAIError(f"{label} must be a lowercase SHA-256")
    return value


def _artifact_hash_valid(value: Mapping[str, Any]) -> bool:
    return value.get("artifact_sha256") == sha256_json(
        {key: item for key, item in value.items() if key != "artifact_sha256"}
    )


def _outside_repository(path: Path, root: Path, label: str) -> Path:
    repository = root.resolve()
    absolute = Path(os.path.abspath(path))
    try:
        absolute.relative_to(repository)
    except ValueError:
        return absolute
    raise FinalReviewAIError(f"{label} must stay outside the repository")


def _external_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        payload = read_external_bytes_snapshot(path, label=label)
        value = json.loads(payload.decode("utf-8", errors="strict"))
    except (ReviewContractError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FinalReviewAIError(f"cannot read {label}") from exc
    if not isinstance(value, dict):
        raise FinalReviewAIError(f"{label} must be a JSON object")
    return value, payload


def final_review_ai_manifest_path(return_path: Path) -> Path:
    """Return the deterministic external native-manifest path for one return."""

    return return_path.with_name(f"{return_path.stem}.native-manifest.json")


def final_review_ai_evidence_directory(return_path: Path) -> Path:
    """Return the deterministic external per-invocation evidence directory."""

    return return_path.with_name(f"{return_path.stem}.native-invocations")


def _proof_path(return_path: Path, ordinal: int) -> Path:
    return final_review_ai_evidence_directory(return_path) / (
        f"invocation-{ordinal:03d}.json"
    )


def _native_output_path(return_path: Path, ordinal: int) -> Path:
    return final_review_ai_evidence_directory(return_path) / (
        f"native-output-{ordinal:03d}.json"
    )


def _safe_release_path(release: Path, relative_value: object) -> Path:
    if not isinstance(relative_value, str) or not relative_value:
        raise FinalReviewAIError("released packet path is invalid")
    relative = Path(relative_value)
    if relative.is_absolute() or ".." in relative.parts or not relative.name:
        raise FinalReviewAIError("released packet path is unsafe")
    candidate = release / relative
    try:
        candidate.resolve(strict=False).relative_to(release.resolve(strict=False))
    except ValueError as exc:
        raise FinalReviewAIError("released packet path escapes its directory") from exc
    return candidate


def _released_packets(
    root: Path,
    *,
    release_directory: Path,
    release: Mapping[str, Any],
    reviewer: str,
    phase: str,
) -> list[tuple[dict[str, Any], dict[str, Any], bytes]]:
    external_release = _outside_repository(
        release_directory, root, "final-review release directory"
    )
    records = release.get("packets")
    if not isinstance(records, list):
        raise FinalReviewAIError("final-review release has no packet records")
    selected: list[tuple[dict[str, Any], dict[str, Any], bytes]] = []
    for record_value in records:
        if not isinstance(record_value, dict):
            raise FinalReviewAIError("final-review packet record is invalid")
        if record_value.get("reviewer") != reviewer:
            continue
        if record_value.get("phase") != phase:
            raise FinalReviewAIError("final-review packet phase changed")
        packet_path = _safe_release_path(external_release, record_value.get("path"))
        try:
            payload = read_external_bytes_snapshot(
                packet_path, label="released final-review packet"
            )
            packet = json.loads(payload.decode("utf-8", errors="strict"))
        except (ReviewContractError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FinalReviewAIError(
                "cannot read released final-review packet"
            ) from exc
        if not isinstance(packet, dict):
            raise FinalReviewAIError("released final-review packet must be an object")
        if (
            packet.get("packet_id") != record_value.get("packet_id")
            or packet.get("reviewer") != reviewer
            or packet.get("phase") != phase
            or hashlib.sha256(payload).hexdigest() != record_value.get("sha256")
            or len(payload) != record_value.get("utf8_bytes")
        ):
            raise FinalReviewAIError("released final-review packet bytes changed")
        cells = packet.get("cells")
        if (
            not isinstance(cells, list)
            or len(cells) != record_value.get("cell_count")
            or len(cells) != 20
        ):
            raise FinalReviewAIError("released packet does not contain 20 cells")
        selected.append((record_value, packet, payload))
    if len(selected) != _EXPECTED_PACKET_COUNTS[phase]:
        raise FinalReviewAIError(
            f"{reviewer} {phase} release does not contain the fixed packet count"
        )
    return selected


def _grade_schema() -> dict[str, Any]:
    ordinal = {"type": "integer", "minimum": 0, "maximum": 3}
    return {
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
            "overall_ordinal": ordinal,
            "factual_correctness": ordinal,
            "completeness": ordinal,
            "citation_support": ordinal,
            "authority_freshness": ordinal,
            "abstention_quality": {
                "type": "string",
                "enum": ["not_applicable", "correct", "incorrect"],
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
    }


def _packet_response_schema(packet: Mapping[str, Any]) -> dict[str, Any]:
    cells = packet.get("cells")
    if not isinstance(cells, list):
        raise FinalReviewAIError("packet cells are missing")
    blind_ids = [str(cell.get("blind_cell_id")) for cell in cells]
    if len(blind_ids) != 20 or len(set(blind_ids)) != 20:
        raise FinalReviewAIError("packet blind-cell identities are invalid")
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["grades", "rubric_ambiguous", "review_comment"],
        "properties": {
            "grades": {
                "type": "array",
                "minItems": 20,
                "maxItems": 20,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["blind_cell_id", "grade"],
                    "properties": {
                        "blind_cell_id": {"type": "string", "enum": blind_ids},
                        "grade": _grade_schema(),
                    },
                },
            },
            "rubric_ambiguous": {"type": "boolean"},
            "review_comment": {"type": "string", "maxLength": 4000},
        },
    }


def _packet_prompt(
    *,
    reviewer: str,
    phase: str,
    packet_record: Mapping[str, Any],
    ordinal: int,
    packet_count: int,
) -> str:
    profile = native.reviewer_profile(reviewer)
    return (
        "You are one independent member of the frozen ContextLab v2 final-review "
        "panel. Read only packet.json in the current isolated workspace. Grade all "
        "20 blind cells with the rubric and instructions embedded in that packet. "
        "Do not infer strategy, task, provider, effort, or sealed identity. Do not "
        "access any other file. Return only one JSON object matching response-schema.json. "
        "Do not include Markdown or hidden reasoning.\n\n"
        f"Reviewer: {reviewer}\n"
        f"Exact model: {profile['model']}\n"
        f"Exact reasoning effort: {profile['effort']}\n"
        f"Phase: {phase}\n"
        f"Packet ordinal: {ordinal} of {packet_count}\n"
        f"Packet SHA-256: {packet_record['sha256']}\n"
        f"Review manifest packet ID: {packet_record['packet_id']}"
    )


def _validate_packet_response(
    value: object, packet: Mapping[str, Any]
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "grades",
        "rubric_ambiguous",
        "review_comment",
    }:
        raise FinalReviewAIError("native packet response fields changed")
    if not isinstance(value.get("rubric_ambiguous"), bool):
        raise FinalReviewAIError("native packet ambiguity decision is invalid")
    comment = value.get("review_comment")
    if not isinstance(comment, str) or len(comment) > 4000:
        raise FinalReviewAIError("native packet review comment is invalid")
    expected = [str(cell["blind_cell_id"]) for cell in packet["cells"]]
    rows = value.get("grades")
    if not isinstance(rows, list) or len(rows) != len(expected):
        raise FinalReviewAIError("native packet response grade count differs")
    grades: dict[str, dict[str, Any]] = {}
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"blind_cell_id", "grade"}:
            raise FinalReviewAIError("native packet grade fields changed")
        blind_id = row.get("blind_cell_id")
        grade = row.get("grade")
        if not isinstance(blind_id, str) or blind_id in grades:
            raise FinalReviewAIError("native packet repeats a blind cell")
        if not isinstance(grade, dict):
            raise FinalReviewAIError("native packet grade is invalid")
        try:
            validate_grade(grade)
        except ReviewContractError as exc:
            raise FinalReviewAIError("native packet grade violates the rubric") from exc
        grades[blind_id] = grade
        normalized.append({"blind_cell_id": blind_id, "grade": grade})
    if set(grades) != set(expected):
        raise FinalReviewAIError("native packet response does not cover exact cells")
    return {
        "grades": normalized,
        "rubric_ambiguous": bool(value["rubric_ambiguous"]),
        "review_comment": comment,
    }


def _json_object(text: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FinalReviewAIError(f"{label} is not JSON") from exc
    if not isinstance(value, dict):
        raise FinalReviewAIError(f"{label} must be a JSON object")
    return value


def _flat_usage(value: object) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    usage = {
        str(key): item
        for key, item in value.items()
        if not isinstance(item, bool) and isinstance(item, int) and item >= 0
    }
    return dict(sorted(usage.items())) or None


def _parse_native_output(
    raw: bytes, *, reviewer: str, packet: Mapping[str, Any]
) -> dict[str, Any]:
    if not raw or len(raw) > _MAX_NATIVE_OUTPUT_BYTES:
        raise FinalReviewAIError("native packet output size is invalid")
    profile = native.reviewer_profile(reviewer)
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise FinalReviewAIError("native packet output is not UTF-8") from exc
    if profile["transport"] == "codex-cli":
        events = [
            _json_object(line, "Codex JSONL event")
            for line in text.splitlines()
            if line.strip()
        ]
        invocation_ids = {
            str(event["thread_id"])
            for event in events
            if event.get("type") == "thread.started"
            and isinstance(event.get("thread_id"), str)
        }
        responses: list[dict[str, Any]] = []
        usage: dict[str, int] | None = None
        for event in events:
            if event.get("type") == "item.completed":
                item = event.get("item")
                if isinstance(item, Mapping) and item.get("type") == "agent_message":
                    response_text = item.get("text")
                    if isinstance(response_text, str):
                        try:
                            responses.append(
                                _json_object(response_text, "Codex response")
                            )
                        except FinalReviewAIError:
                            continue
            if event.get("type") == "turn.completed":
                usage = _flat_usage(event.get("usage")) or usage
        if len(invocation_ids) != 1 or not responses:
            raise FinalReviewAIError("Codex packet output lacks native proof")
        invocation_id = next(iter(invocation_ids))
        native_model_id: str | None = None
        response_value = responses[-1]
    else:
        envelope = _json_object(text, "Claude native output")
        if envelope.get("is_error") is not False:
            raise FinalReviewAIError("Claude packet invocation reported an error")
        invocation_id = envelope.get("session_id")
        model_usage = envelope.get("modelUsage")
        if not isinstance(model_usage, Mapping) or profile["model"] not in model_usage:
            raise FinalReviewAIError("Claude output does not prove the exact model")
        structured = envelope.get("structured_output")
        if isinstance(structured, Mapping):
            response_value = dict(structured)
        else:
            result = envelope.get("result")
            if not isinstance(result, str):
                raise FinalReviewAIError("Claude packet result is missing")
            response_value = _json_object(result, "Claude packet response")
        usage = _flat_usage(envelope.get("usage"))
        native_model_id = profile["model"]
    if (
        not isinstance(invocation_id, str)
        or _INVOCATION_ID.fullmatch(invocation_id) is None
    ):
        raise FinalReviewAIError("native packet invocation ID is invalid")
    return {
        "native_invocation_id": invocation_id,
        "native_model_id": native_model_id,
        "response": _validate_packet_response(response_value, packet),
        "usage": usage,
    }


def _response_schema_path_text(schema: Mapping[str, Any]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def _native_command(
    *,
    executable: Path,
    reviewer: str,
    prompt: str,
    schema_path: Path,
    workspace: Path,
) -> list[str]:
    profile = native.reviewer_profile(reviewer)
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
            "-c",
            f'default_permissions="{native._CODEX_PERMISSION_PROFILE}"',
            "-c",
            native._CODEX_PERMISSION_OVERRIDE,
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--json",
            "--output-schema",
            str(schema_path),
            "-C",
            str(workspace),
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
        "Read",
        "--output-format",
        "json",
        "--json-schema",
        schema_path.read_text(encoding="utf-8"),
        prompt,
    ]


def _execute_native_packet(
    *,
    reviewer: str,
    packet: Mapping[str, Any],
    packet_bytes: bytes,
    prompt: str,
    response_schema: Mapping[str, Any],
    timeout_seconds: int,
) -> Mapping[str, Any]:
    """Production executor kept separate so tests can inject a no-call fake."""

    profile = native.reviewer_profile(reviewer)
    executable = native._trusted_executable(profile)
    executable_version = native._executable_version(executable)
    launcher, containment_profile = native._containment_launcher(reviewer)
    for session_root in native._native_session_roots(reviewer):
        native._ensure_absolute_directory(session_root)
    claude_config_root: Path | None = None
    if profile["transport"] == "claude-cli":
        claude_config_root = (
            native._CLAUDE_REVIEW_CONFIG_ROOT
            / "invocations"
            / f"final-review-{secrets.token_hex(16)}"
        )
        native._ensure_absolute_directory(claude_config_root)

    started_at = _utc_now()
    with tempfile.TemporaryDirectory(
        prefix="contextlab-final-review-ai-", dir="/tmp"
    ) as directory:
        runtime = Path(directory).resolve(strict=True)
        workspace = runtime / "packet-workspace"
        workspace.mkdir(mode=0o700)
        packet_path = workspace / "packet.json"
        schema_path = workspace / "response-schema.json"
        packet_path.write_bytes(packet_bytes)
        schema_path.write_text(
            _response_schema_path_text(response_schema), encoding="utf-8"
        )
        os.chmod(packet_path, 0o400)
        os.chmod(schema_path, 0o400)
        command = [
            str(launcher),
            "-D",
            f"RUNTIME_ROOT={runtime}",
            "-D",
            f"WORKSPACE_ROOT={workspace}",
            "-p",
            containment_profile,
            *_native_command(
                executable=executable,
                reviewer=reviewer,
                prompt=prompt,
                schema_path=schema_path,
                workspace=workspace,
            ),
        ]
        environment = native._reviewer_environment(
            reviewer, claude_config_root=claude_config_root
        )
        if profile["transport"] == "claude-cli":
            claude_tmp = runtime / "claude-tmp"
            claude_tmp.mkdir(mode=0o700)
            environment["CLAUDE_CODE_TMPDIR"] = str(claude_tmp)
            environment["TMPDIR"] = str(claude_tmp)
        credential_descriptor: int | None = None
        try:
            if profile["transport"] == "claude-cli":
                credential_descriptor = native._claude_oauth_token_descriptor()
                environment["CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR"] = str(
                    credential_descriptor
                )
                command = [
                    *command[:5],
                    "-D",
                    f"OAUTH_FD_PATH=/dev/fd/{credential_descriptor}",
                    "-D",
                    f"SESSION_ROOT={claude_config_root}",
                    *command[5:],
                ]
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
            raise FinalReviewAIError("native packet invocation failed") from exc
        finally:
            if credential_descriptor is not None:
                os.close(credential_descriptor)
    completed_at = _utc_now()
    if completed.returncode != 0:
        raise FinalReviewAIError(
            f"native packet reviewer exited {completed.returncode}"
        )
    parsed = _parse_native_output(completed.stdout, reviewer=reviewer, packet=packet)
    session_path = native._find_native_session(reviewer, parsed["native_invocation_id"])
    attestation = native._attest_native_session(
        session_path,
        reviewer_id=reviewer,
        invocation_id=parsed["native_invocation_id"],
        expected_prompt=prompt,
        expected_response=parsed["response"],
    )
    if parsed["native_model_id"] not in {None, attestation["native_model_id"]}:
        raise FinalReviewAIError("native packet output and session model differ")
    return {
        "raw_output": completed.stdout,
        "stderr": completed.stderr,
        "exit_code": completed.returncode,
        "started_at": started_at,
        "completed_at": completed_at,
        "native_invocation_id": parsed["native_invocation_id"],
        "native_session_id": parsed["native_invocation_id"],
        "native_session_path": str(session_path),
        "native_session_sha256": attestation["native_session_sha256"],
        "native_session_attestation_sha256": attestation[
            "native_session_attestation_sha256"
        ],
        "native_model_id": attestation["native_model_id"],
        "native_reasoning_effort": attestation["native_reasoning_effort"],
        "executable_path": str(executable),
        "executable_sha256": native._file_sha256(executable),
        "executable_version": executable_version,
        "usage": parsed["usage"],
        "response": parsed["response"],
    }


def _normalize_execution(
    value: Mapping[str, Any],
    *,
    reviewer: str,
    packet: Mapping[str, Any],
) -> dict[str, Any]:
    expected = {
        "raw_output",
        "stderr",
        "exit_code",
        "started_at",
        "completed_at",
        "native_invocation_id",
        "native_session_id",
        "native_session_path",
        "native_session_sha256",
        "native_session_attestation_sha256",
        "native_model_id",
        "native_reasoning_effort",
        "executable_path",
        "executable_sha256",
        "executable_version",
        "usage",
        "response",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise FinalReviewAIError("native packet executor result fields changed")
    raw = value.get("raw_output")
    stderr = value.get("stderr")
    if not isinstance(raw, bytes) or not isinstance(stderr, bytes):
        raise FinalReviewAIError("native packet output must be bytes")
    parsed = _parse_native_output(raw, reviewer=reviewer, packet=packet)
    profile = native.reviewer_profile(reviewer)
    if (
        value.get("exit_code") != 0
        or value.get("response") != parsed["response"]
        or value.get("native_invocation_id") != parsed["native_invocation_id"]
        or value.get("native_session_id") != parsed["native_invocation_id"]
        or value.get("native_model_id") != profile["model"]
        or value.get("native_reasoning_effort") != profile["effort"]
        or parsed["native_model_id"] not in {None, profile["model"]}
        or value.get("usage") != parsed["usage"]
    ):
        raise FinalReviewAIError("native packet executor proof is inconsistent")
    for field in ("started_at", "completed_at"):
        timestamp = value.get(field)
        if not isinstance(timestamp, str) or _UTC_SECOND.fullmatch(timestamp) is None:
            raise FinalReviewAIError("native packet timestamp is invalid")
    invocation_id = value.get("native_invocation_id")
    if (
        not isinstance(invocation_id, str)
        or _INVOCATION_ID.fullmatch(invocation_id) is None
    ):
        raise FinalReviewAIError("native packet invocation ID is invalid")
    session_path = value.get("native_session_path")
    if not isinstance(session_path, str) or not Path(session_path).is_absolute():
        raise FinalReviewAIError("native packet session path is invalid")
    for field in (
        "native_session_sha256",
        "native_session_attestation_sha256",
        "executable_sha256",
    ):
        _require_sha(value.get(field), field)
    for field in ("executable_path", "executable_version"):
        if not isinstance(value.get(field), str) or not value[field]:
            raise FinalReviewAIError("native packet executable identity is invalid")
    usage = value.get("usage")
    if usage is not None and _flat_usage(usage) != usage:
        raise FinalReviewAIError("native packet usage is invalid")
    return dict(value)


def _proof_value(
    *,
    reviewer: str,
    phase: str,
    freeze: Mapping[str, Any],
    confirmation: Mapping[str, Any],
    release: Mapping[str, Any],
    record: Mapping[str, Any],
    ordinal: int,
    prompt: str,
    response_schema: Mapping[str, Any],
    execution: Mapping[str, Any],
    native_output_filename: str,
) -> dict[str, Any]:
    profile = native.reviewer_profile(reviewer)
    response = execution["response"]
    usage = execution["usage"]
    proof: dict[str, Any] = {
        "schema_version": FINAL_REVIEW_AI_INVOCATION_SCHEMA,
        "reviewer": reviewer,
        "phase": phase,
        "packet_ordinal": ordinal,
        "packet_id": record["packet_id"],
        "packet_path": record["path"],
        "packet_sha256": record["sha256"],
        "packet_utf8_bytes": record["utf8_bytes"],
        "freeze_artifact_sha256": freeze["artifact_sha256"],
        "preflight_artifact_sha256": confirmation["preflight_artifact_sha256"],
        "confirmation_artifact_sha256": confirmation["artifact_sha256"],
        "review_manifest_sha256": freeze["packet_manifest_sha256"],
        "release_manifest_sha256": release["manifest_sha256"],
        "profile": {
            "transport": profile["transport"],
            "model": profile["model"],
            "effort": profile["effort"],
        },
        "native_invocation_id": execution["native_invocation_id"],
        "native_session_id": execution["native_session_id"],
        "native_session_path": execution["native_session_path"],
        "native_session_sha256": execution["native_session_sha256"],
        "native_session_attestation_sha256": execution[
            "native_session_attestation_sha256"
        ],
        "native_model_id": execution["native_model_id"],
        "native_reasoning_effort": execution["native_reasoning_effort"],
        "executable_path": execution["executable_path"],
        "executable_sha256": execution["executable_sha256"],
        "executable_version": execution["executable_version"],
        "started_at": execution["started_at"],
        "completed_at": execution["completed_at"],
        "exit_code": execution["exit_code"],
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "response_schema_sha256": sha256_json(response_schema),
        "response_payload_sha256": sha256_json(response),
        "native_output_filename": native_output_filename,
        "native_output_sha256": hashlib.sha256(execution["raw_output"]).hexdigest(),
        "stderr_sha256": hashlib.sha256(execution["stderr"]).hexdigest(),
        "usage_available": usage is not None,
        "usage": usage,
    }
    proof["artifact_sha256"] = sha256_json(proof)
    return proof


def _validate_proof_shape(value: Mapping[str, Any]) -> None:
    expected = {
        "schema_version",
        "reviewer",
        "phase",
        "packet_ordinal",
        "packet_id",
        "packet_path",
        "packet_sha256",
        "packet_utf8_bytes",
        "freeze_artifact_sha256",
        "preflight_artifact_sha256",
        "confirmation_artifact_sha256",
        "review_manifest_sha256",
        "release_manifest_sha256",
        "profile",
        "native_invocation_id",
        "native_session_id",
        "native_session_path",
        "native_session_sha256",
        "native_session_attestation_sha256",
        "native_model_id",
        "native_reasoning_effort",
        "executable_path",
        "executable_sha256",
        "executable_version",
        "started_at",
        "completed_at",
        "exit_code",
        "prompt_sha256",
        "response_schema_sha256",
        "response_payload_sha256",
        "native_output_filename",
        "native_output_sha256",
        "stderr_sha256",
        "usage_available",
        "usage",
        "artifact_sha256",
    }
    if (
        set(value) != expected
        or value.get("schema_version") != FINAL_REVIEW_AI_INVOCATION_SCHEMA
        or not _artifact_hash_valid(value)
        or value.get("exit_code") != 0
    ):
        raise FinalReviewAIError("native packet proof is malformed")
    reviewer = value.get("reviewer")
    if not isinstance(reviewer, str) or reviewer not in _AI_REVIEWERS:
        raise FinalReviewAIError("native packet proof reviewer is invalid")
    profile = native.reviewer_profile(reviewer)
    if value.get("profile") != {
        "transport": profile["transport"],
        "model": profile["model"],
        "effort": profile["effort"],
    }:
        raise FinalReviewAIError("native packet proof profile changed")
    if (
        value.get("native_model_id") != profile["model"]
        or value.get("native_reasoning_effort") != profile["effort"]
    ):
        raise FinalReviewAIError("native packet model or effort changed")
    for field in (
        "packet_sha256",
        "freeze_artifact_sha256",
        "preflight_artifact_sha256",
        "confirmation_artifact_sha256",
        "review_manifest_sha256",
        "release_manifest_sha256",
        "native_session_sha256",
        "native_session_attestation_sha256",
        "executable_sha256",
        "prompt_sha256",
        "response_schema_sha256",
        "response_payload_sha256",
        "native_output_sha256",
        "stderr_sha256",
    ):
        _require_sha(value.get(field), field)
    for field in ("started_at", "completed_at"):
        timestamp = value.get(field)
        if not isinstance(timestamp, str) or _UTC_SECOND.fullmatch(timestamp) is None:
            raise FinalReviewAIError("native packet proof timestamp is invalid")
    invocation_id = value.get("native_invocation_id")
    if (
        not isinstance(invocation_id, str)
        or _INVOCATION_ID.fullmatch(invocation_id) is None
        or value.get("native_session_id") != invocation_id
    ):
        raise FinalReviewAIError("native packet proof invocation ID is invalid")
    usage = value.get("usage")
    if value.get("usage_available") is True:
        if _flat_usage(usage) != usage:
            raise FinalReviewAIError("native packet proof usage is invalid")
    elif value.get("usage_available") is False:
        if usage is not None:
            raise FinalReviewAIError("unavailable native usage must be null")
    else:
        raise FinalReviewAIError("native usage availability is invalid")


def _default_native_proof_validator(
    *,
    proof: Mapping[str, Any],
    reviewer: str,
    prompt: str,
    response: Mapping[str, Any],
) -> None:
    """Replay the CLI executable and native session, not caller assertions."""

    profile = native.reviewer_profile(reviewer)
    executable = native._trusted_executable(profile)
    if (
        proof.get("executable_path") != str(executable)
        or proof.get("executable_sha256") != native._file_sha256(executable)
        or proof.get("executable_version") != native._executable_version(executable)
    ):
        raise FinalReviewAIError("native packet executable identity changed")
    session_value = Path(str(proof.get("native_session_path", "")))
    try:
        session_path = session_value.resolve(strict=True)
    except OSError as exc:
        raise FinalReviewAIError("native packet session is unavailable") from exc
    allowed = False
    for session_root in native._native_session_roots(reviewer):
        try:
            session_path.relative_to(session_root.resolve(strict=True))
        except (OSError, ValueError):
            continue
        allowed = True
        break
    if (
        not allowed
        or session_value != session_path
        or session_value.is_symlink()
        or not session_value.is_file()
    ):
        raise FinalReviewAIError("native packet session path is unsafe")
    try:
        attestation = native._attest_native_session(
            session_path,
            reviewer_id=reviewer,
            invocation_id=str(proof["native_invocation_id"]),
            expected_prompt=prompt,
            expected_response=response,
        )
    except native.AIReviewInvocationError as exc:
        raise FinalReviewAIError("native packet session replay failed") from exc
    if (
        proof.get("native_session_sha256") != attestation["native_session_sha256"]
        or proof.get("native_session_attestation_sha256")
        != attestation["native_session_attestation_sha256"]
        or proof.get("native_model_id") != attestation["native_model_id"]
        or proof.get("native_reasoning_effort")
        != attestation["native_reasoning_effort"]
    ):
        raise FinalReviewAIError("native packet session evidence changed")


def run_final_review_ai(
    root: Path,
    *,
    staging_directory: Path,
    release_directory: Path,
    reviewer: str,
    phase: str,
    return_path: Path,
    timeout_seconds: int = 1800,
    executor: PacketExecutor | None = None,
) -> dict[str, Any]:
    """Run every exact released packet independently with one fixed AI profile."""

    if reviewer not in _AI_REVIEWERS:
        raise FinalReviewAIError(
            "native final review supports only the two AI reviewers"
        )
    if phase not in _PHASE_RETURN_SCHEMAS:
        raise FinalReviewAIError("native final-review phase is invalid")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or timeout_seconds <= 0
        or timeout_seconds > 7200
    ):
        raise FinalReviewAIError("native packet timeout is invalid")

    # Late import avoids a module cycle.  These loaders replay G4/frontier,
    # freeze, exact preflight confirmation, and the matching release before any
    # released packet is read below.
    from .final_review import (
        _load_confirmation,
        _load_current_freeze,
        _replay_release,
    )

    repository = root.resolve()
    external_return = _outside_repository(
        return_path, repository, "native final-review return"
    )
    freeze = _load_current_freeze(repository, staging_directory)
    confirmation = _load_confirmation(repository, freeze, staging_directory)
    release = _replay_release(
        release_directory,
        root=repository,
        freeze=freeze,
        confirmation=confirmation,
        phase=phase,
    )
    packets = _released_packets(
        repository,
        release_directory=release_directory,
        release=release,
        reviewer=reviewer,
        phase=phase,
    )
    packet_executor = executor or _execute_native_packet
    all_grades: list[dict[str, Any]] = []
    ambiguity = False
    manifest_entries: list[dict[str, Any]] = []
    invocation_ids: set[str] = set()
    output_hashes: set[str] = set()
    for ordinal, (record, packet, packet_bytes) in enumerate(packets, start=1):
        response_schema = _packet_response_schema(packet)
        prompt = _packet_prompt(
            reviewer=reviewer,
            phase=phase,
            packet_record=record,
            ordinal=ordinal,
            packet_count=len(packets),
        )
        execution = _normalize_execution(
            packet_executor(
                reviewer=reviewer,
                packet=packet,
                packet_bytes=packet_bytes,
                prompt=prompt,
                response_schema=response_schema,
                timeout_seconds=timeout_seconds,
            ),
            reviewer=reviewer,
            packet=packet,
        )
        invocation_id = str(execution["native_invocation_id"])
        native_output_sha = hashlib.sha256(execution["raw_output"]).hexdigest()
        if invocation_id in invocation_ids or native_output_sha in output_hashes:
            raise FinalReviewAIError("native invocation ID or output was reused")
        invocation_ids.add(invocation_id)
        output_hashes.add(native_output_sha)
        output_path = _native_output_path(external_return, ordinal)
        proof_path = _proof_path(external_return, ordinal)
        proof = _proof_value(
            reviewer=reviewer,
            phase=phase,
            freeze=freeze,
            confirmation=confirmation,
            release=release,
            record=record,
            ordinal=ordinal,
            prompt=prompt,
            response_schema=response_schema,
            execution=execution,
            native_output_filename=output_path.name,
        )
        _validate_proof_shape(proof)
        try:
            write_external_bytes_once_or_verify(
                output_path,
                execution["raw_output"],
                label="native final-review output",
            )
            write_external_bytes_once_or_verify(
                proof_path,
                _json_bytes(proof),
                label="native final-review invocation proof",
            )
            harden_external_review_file(output_path)
            harden_external_review_file(proof_path)
        except ReviewContractError as exc:
            raise FinalReviewAIError("cannot publish native packet proof") from exc
        response = execution["response"]
        all_grades.extend(response["grades"])
        ambiguity = ambiguity or bool(response["rubric_ambiguous"])
        proof_bytes = _json_bytes(proof)
        manifest_entries.append(
            {
                "packet_ordinal": ordinal,
                "packet_sha256": record["sha256"],
                "proof_filename": proof_path.name,
                "proof_file_sha256": hashlib.sha256(proof_bytes).hexdigest(),
                "proof_artifact_sha256": proof["artifact_sha256"],
                "native_invocation_id_sha256": hashlib.sha256(
                    invocation_id.encode("utf-8")
                ).hexdigest(),
                "native_output_sha256": native_output_sha,
                "response_payload_sha256": proof["response_payload_sha256"],
            }
        )

    if len(all_grades) != _EXPECTED_GRADE_COUNTS[phase]:
        raise FinalReviewAIError("native final-review return is incomplete")
    response: dict[str, Any] = {
        "schema_version": _PHASE_RETURN_SCHEMAS[phase],
        "reviewer": reviewer,
        "review_manifest_sha256": freeze["packet_manifest_sha256"],
        "release_manifest_sha256": release["manifest_sha256"],
        "phase": phase,
        "grade_count": len(all_grades),
        "grades": all_grades,
        "rubric_ambiguous": ambiguity,
        "review_comment": (
            f"Native packet review completed independently for {len(packets)} "
            "packet(s); per-packet comments remain in external native output."
        ),
    }
    response["artifact_sha256"] = sha256_json(response)
    response_bytes = _json_bytes(response)
    manifest: dict[str, Any] = {
        "schema_version": FINAL_REVIEW_AI_MANIFEST_SCHEMA,
        "reviewer": reviewer,
        "phase": phase,
        "freeze_artifact_sha256": freeze["artifact_sha256"],
        "preflight_artifact_sha256": confirmation["preflight_artifact_sha256"],
        "confirmation_artifact_sha256": confirmation["artifact_sha256"],
        "review_manifest_sha256": freeze["packet_manifest_sha256"],
        "release_manifest_sha256": release["manifest_sha256"],
        "packet_count": len(packets),
        "grade_count": len(all_grades),
        "return_artifact_sha256": response["artifact_sha256"],
        "return_file_sha256": hashlib.sha256(response_bytes).hexdigest(),
        "invocations": manifest_entries,
        "content_policy": {
            "packet_content": "external_only",
            "grade_content": "external_only",
            "sealed_identities": "never_recorded",
            "repository_binding": "manifest_hash_only",
        },
    }
    manifest["artifact_sha256"] = sha256_json(manifest)
    manifest_path = final_review_ai_manifest_path(external_return)
    try:
        write_external_bytes_once_or_verify(
            external_return, response_bytes, label="native final-review return"
        )
        write_external_bytes_once_or_verify(
            manifest_path,
            _json_bytes(manifest),
            label="native final-review manifest",
        )
        harden_external_review_file(external_return)
        harden_external_review_file(manifest_path)
    except ReviewContractError as exc:
        raise FinalReviewAIError("cannot publish native final-review return") from exc
    return {
        "return": response,
        "manifest": manifest,
        "return_path": str(external_return),
        "manifest_path": str(manifest_path),
    }


def _validate_manifest_shape(value: Mapping[str, Any]) -> None:
    expected = {
        "schema_version",
        "reviewer",
        "phase",
        "freeze_artifact_sha256",
        "preflight_artifact_sha256",
        "confirmation_artifact_sha256",
        "review_manifest_sha256",
        "release_manifest_sha256",
        "packet_count",
        "grade_count",
        "return_artifact_sha256",
        "return_file_sha256",
        "invocations",
        "content_policy",
        "artifact_sha256",
    }
    if (
        set(value) != expected
        or value.get("schema_version") != FINAL_REVIEW_AI_MANIFEST_SCHEMA
        or not _artifact_hash_valid(value)
        or value.get("content_policy")
        != {
            "packet_content": "external_only",
            "grade_content": "external_only",
            "sealed_identities": "never_recorded",
            "repository_binding": "manifest_hash_only",
        }
    ):
        raise FinalReviewAIError("native final-review manifest is malformed")
    for field in (
        "freeze_artifact_sha256",
        "preflight_artifact_sha256",
        "confirmation_artifact_sha256",
        "review_manifest_sha256",
        "release_manifest_sha256",
        "return_artifact_sha256",
        "return_file_sha256",
    ):
        _require_sha(value.get(field), field)


def validate_final_review_ai_manifest(
    root: Path,
    *,
    release_directory: Path,
    reviewer: str,
    phase: str,
    freeze: Mapping[str, Any],
    confirmation: Mapping[str, Any],
    release: Mapping[str, Any],
    return_path: Path,
    return_value: Mapping[str, Any],
    native_proof_validator: NativeProofValidator | None = None,
) -> dict[str, Any]:
    """Replay one complete native manifest against exact return and packet bytes."""

    if reviewer not in _AI_REVIEWERS or phase not in _PHASE_RETURN_SCHEMAS:
        raise FinalReviewAIError("native final-review manifest identity is invalid")
    repository = root.resolve()
    external_return = _outside_repository(
        return_path, repository, "native final-review return"
    )
    manifest_path = final_review_ai_manifest_path(external_return)
    manifest, manifest_bytes = _external_json(
        manifest_path, "native final-review manifest"
    )
    _validate_manifest_shape(manifest)
    return_bytes = read_external_bytes_snapshot(
        external_return, label="native final-review return"
    )
    packets = _released_packets(
        repository,
        release_directory=release_directory,
        release=release,
        reviewer=reviewer,
        phase=phase,
    )
    if (
        manifest.get("reviewer") != reviewer
        or manifest.get("phase") != phase
        or manifest.get("freeze_artifact_sha256") != freeze.get("artifact_sha256")
        or manifest.get("preflight_artifact_sha256")
        != confirmation.get("preflight_artifact_sha256")
        or manifest.get("confirmation_artifact_sha256")
        != confirmation.get("artifact_sha256")
        or manifest.get("review_manifest_sha256")
        != freeze.get("packet_manifest_sha256")
        or manifest.get("release_manifest_sha256") != release.get("manifest_sha256")
        or manifest.get("packet_count") != len(packets)
        or manifest.get("grade_count") != _EXPECTED_GRADE_COUNTS[phase]
        or manifest.get("return_artifact_sha256") != return_value.get("artifact_sha256")
        or manifest.get("return_file_sha256")
        != hashlib.sha256(return_bytes).hexdigest()
        or return_value.get("schema_version") != _PHASE_RETURN_SCHEMAS[phase]
        or return_value.get("reviewer") != reviewer
        or return_value.get("phase") != phase
        or return_value.get("review_manifest_sha256")
        != freeze.get("packet_manifest_sha256")
        or return_value.get("release_manifest_sha256") != release.get("manifest_sha256")
        or return_value.get("artifact_sha256")
        != sha256_json(
            {
                key: item
                for key, item in return_value.items()
                if key != "artifact_sha256"
            }
        )
    ):
        raise FinalReviewAIError("native final-review manifest binding changed")

    entries = manifest.get("invocations")
    if not isinstance(entries, list) or len(entries) != len(packets):
        raise FinalReviewAIError(
            "native final-review invocation manifest is incomplete"
        )
    expected_return_grades = return_value.get("grades")
    if not isinstance(expected_return_grades, list):
        raise FinalReviewAIError("native final-review return has no grades")
    replayed_grades: list[dict[str, Any]] = []
    ambiguity = False
    invocation_ids: set[str] = set()
    output_hashes: set[str] = set()
    validator = native_proof_validator or _default_native_proof_validator
    for ordinal, ((record, packet, _), entry) in enumerate(
        zip(packets, entries, strict=True), start=1
    ):
        if not isinstance(entry, Mapping):
            raise FinalReviewAIError("native invocation manifest entry is invalid")
        proof_path = _proof_path(external_return, ordinal)
        output_path = _native_output_path(external_return, ordinal)
        proof, proof_bytes = _external_json(
            proof_path, "native final-review invocation proof"
        )
        _validate_proof_shape(proof)
        raw_output = read_external_bytes_snapshot(
            output_path, label="native final-review output"
        )
        response_schema = _packet_response_schema(packet)
        prompt = _packet_prompt(
            reviewer=reviewer,
            phase=phase,
            packet_record=record,
            ordinal=ordinal,
            packet_count=len(packets),
        )
        parsed = _parse_native_output(raw_output, reviewer=reviewer, packet=packet)
        expected_entry = {
            "packet_ordinal": ordinal,
            "packet_sha256": record["sha256"],
            "proof_filename": proof_path.name,
            "proof_file_sha256": hashlib.sha256(proof_bytes).hexdigest(),
            "proof_artifact_sha256": proof["artifact_sha256"],
            "native_invocation_id_sha256": hashlib.sha256(
                str(proof["native_invocation_id"]).encode("utf-8")
            ).hexdigest(),
            "native_output_sha256": hashlib.sha256(raw_output).hexdigest(),
            "response_payload_sha256": sha256_json(parsed["response"]),
        }
        if dict(entry) != expected_entry:
            raise FinalReviewAIError("native invocation manifest entry changed")
        if (
            proof.get("reviewer") != reviewer
            or proof.get("phase") != phase
            or proof.get("packet_ordinal") != ordinal
            or proof.get("packet_id") != record.get("packet_id")
            or proof.get("packet_path") != record.get("path")
            or proof.get("packet_sha256") != record.get("sha256")
            or proof.get("packet_utf8_bytes") != record.get("utf8_bytes")
            or proof.get("freeze_artifact_sha256") != freeze.get("artifact_sha256")
            or proof.get("preflight_artifact_sha256")
            != confirmation.get("preflight_artifact_sha256")
            or proof.get("confirmation_artifact_sha256")
            != confirmation.get("artifact_sha256")
            or proof.get("review_manifest_sha256")
            != freeze.get("packet_manifest_sha256")
            or proof.get("release_manifest_sha256") != release.get("manifest_sha256")
            or proof.get("prompt_sha256")
            != hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            or proof.get("response_schema_sha256") != sha256_json(response_schema)
            or proof.get("response_payload_sha256") != sha256_json(parsed["response"])
            or proof.get("native_output_filename") != output_path.name
            or proof.get("native_output_sha256")
            != hashlib.sha256(raw_output).hexdigest()
            or proof.get("native_invocation_id") != parsed["native_invocation_id"]
            or proof.get("native_session_id") != parsed["native_invocation_id"]
            or parsed["native_model_id"] not in {None, proof.get("native_model_id")}
            or proof.get("usage") != parsed["usage"]
        ):
            raise FinalReviewAIError("native packet proof binding changed")
        invocation_id = str(proof["native_invocation_id"])
        output_sha = str(proof["native_output_sha256"])
        if invocation_id in invocation_ids or output_sha in output_hashes:
            raise FinalReviewAIError("native invocation ID or output was reused")
        invocation_ids.add(invocation_id)
        output_hashes.add(output_sha)
        validator(
            proof=proof,
            reviewer=reviewer,
            prompt=prompt,
            response=parsed["response"],
        )
        replayed_grades.extend(parsed["response"]["grades"])
        ambiguity = ambiguity or bool(parsed["response"]["rubric_ambiguous"])

    expected_comment = (
        f"Native packet review completed independently for {len(packets)} "
        "packet(s); per-packet comments remain in external native output."
    )
    if (
        replayed_grades != expected_return_grades
        or bool(return_value.get("rubric_ambiguous")) != ambiguity
        or return_value.get("review_comment") != expected_comment
    ):
        raise FinalReviewAIError(
            "native packet outputs differ from the complete return"
        )
    return {
        "artifact_sha256": manifest["artifact_sha256"],
        "file_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "packet_count": len(packets),
        "grade_count": len(replayed_grades),
    }
