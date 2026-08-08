"""Build a byte-bound public-only workspace for independent AI gate review."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

from .g2_sealed import (
    G2_SEALED_IMPORT_SCHEMA,
    G2_SEALED_RETURN_SCHEMA,
    validate_g2_sealed_return,
)
from .immutable_io import (
    ImmutableIOError,
    read_bytes_snapshot,
    write_bytes_once_or_verify,
)
from .sealed import (
    SEALED_IMPORT_SCHEMA,
    SEALED_RETURN_SCHEMA,
    validate_sealed_return,
)
from .tasking import sha256_json


PUBLIC_REVIEW_WORKSPACE_SCHEMA = "contextlab.public-review-workspace.v1"
PUBLIC_REVIEW_WORKSPACE_FILENAME = "public-review-workspace.json"

_MAX_FILE_BYTES = 128 * 1024 * 1024
_MAX_TOTAL_BYTES = 384 * 1024 * 1024
_MAX_FILES = 20_000
_PATH_PREFIXES = (
    "docs/",
    "evaluation/",
    "novalearn_synthetic_corpus/corpus/",
    "novalearn_synthetic_corpus/v2/",
    "results/",
    "viewer/",
)
_FORBIDDEN_PARTS = {
    ".git",
    "evaluation_only_do_not_index",
    "node_modules",
}
_FORBIDDEN_NAMES = {
    "response-filled.json",
    "g3_calibration_identity_map.json",
    "g3_calibration_reference.json",
}
_PUBLIC_TOP_LEVELS = {prefix.partition("/")[0] for prefix in _PATH_PREFIXES}
_SAFE_CONTENT_FREE_SEALED_RESULTS = {
    Path("results/v2/sealed/g1_fixture_import.json"),
    Path("results/v2/sealed/g2-import.json"),
}
_OPAQUE_STATIC_GRADE_PREFIX = Path("results/v2/memory/grades")
_OPAQUE_INVENTORY_FILES = {
    Path("results/v2/baseline/v1_manifest.json"),
    Path("results/v2/reviews/g3_calibration/manifest.json"),
    Path("viewer/package-lock.json"),
}
_OPAQUE_AI_CALIBRATION_RESPONSES = {
    Path("results/v2/reviews/g3_calibration/gpt-5.6-sol-high/response-filled.json"),
    Path("results/v2/reviews/g3_calibration/claude-opus-5-medium/response-filled.json"),
}
_CODEX_AGENT_REFERENCE = re.compile(r"/root/[a-z0-9][a-z0-9_/-]{1,127}\Z")
_PRIVATE_REFERENCE_KEYS = {
    "gold_path",
    "grade_evidence_path",
    "identity_map_path",
    "protected_public_gold_source",
    "reference_path",
    "static_grade_evidence_path",
}
_PRIVATE_REFERENCE_TOKENS = {
    "auth",
    "credential",
    "gold",
    "identity",
    "private",
    "reference",
    "secret",
}
_REFERENCE_KEY_TOKENS = {"file", "path", "uri", "url"}
_OPAQUE_JSON_POINTER_PREFIXES = {
    "candidate_evidence": ("/trace/",),
    "candidate_json_pointer": ("/traces/",),
    "context_budget": ("/run_spec/",),
    "context_tokens": ("/trace/",),
    "cost": ("/actual_usd",),
    "execution_status": ("/status",),
    "jsonpointer": ("/",),
    "latency": ("/latency_ms",),
    "parent_json_pointer": ("/traces/",),
    "selected_evidence": ("/trace/",),
}
_PATH_FILE_SUFFIXES = {
    ".css",
    ".html",
    ".json",
    ".jsonl",
    ".md",
    ".py",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}
_SOURCE_ROOTS = (
    Path("evaluation/v2/contextlab_v2"),
    Path("evaluation/v2/schemas"),
    Path("evaluation/v2/prompts"),
    Path("evaluation/v2/frontier_protocol.json"),
    Path("evaluation/v2/memory_protocol.json"),
    Path("evaluation/v2/retrieval_protocol.json"),
    Path("evaluation/v2/review_protocol.json"),
    Path("docs/CONTEXTLAB_V2_DETAILED_ROADMAP.md"),
)
_G4_SOURCE_ROOTS = (
    Path("evaluation/v2/tests/native_review_fixture.py"),
    Path("evaluation/v2/tests/test_cli.py"),
    Path("evaluation/v2/tests/test_g4_gate.py"),
    Path("evaluation/v2/tests/test_viewer_export.py"),
    Path("viewer/src"),
    Path("viewer/dist"),
    Path("viewer/tests"),
    Path("viewer/eslint.config.js"),
    Path("viewer/index.html"),
    Path("viewer/package.json"),
    Path("viewer/package-lock.json"),
    Path("viewer/tsconfig.json"),
    Path("viewer/vite.config.ts"),
)
_REVIEW_KIND_SEEDS: dict[str, tuple[Path, ...]] = {
    "g3-calibration": (
        Path("results/v2/reviews/g3_calibration/ai-token-preflight.json"),
        Path("results/v2/reviews/g3_calibration/ai-token-preflight-confirmation.json"),
    ),
    "g3-gate": (
        Path("results/v2/gates/G0.json"),
        Path("results/v2/gates/G0.approval.json"),
        Path("results/v2/gates/G1.json"),
        Path("results/v2/gates/G1.approval.json"),
        Path("results/v2/gates/G3.pending.json"),
        Path("results/v2/memory/g3_public_freeze.json"),
        Path("results/v2/memory/g3_public_generation_run.json"),
        Path("results/v2/memory/g3_public_metrics.json"),
        Path("results/v2/memory/g3_sealed_candidates.json"),
        Path("results/v2/memory/g3_sealed_import.json"),
        Path("results/v2/memory/g3_lifecycle_evidence.json"),
        Path("results/v2/memory/g3_panel_calibration.json"),
        Path("results/v2/memory/g3_failure_and_harm_report.json"),
        Path("results/v2/splits/static_g2_freeze.json"),
        Path("results/v2/reviews/g3_unsupported_memory_dispositions.json"),
        Path(
            "results/v2/reviews/g3_unsupported_memory_dispositions_kevin_approval.json"
        ),
        Path("results/v2/reviews/g3_calibration/manifest.json"),
        Path("results/v2/reviews/g3_calibration/ai-token-preflight.json"),
        Path("results/v2/reviews/g3_calibration/ai-token-preflight-confirmation.json"),
        Path("results/v2/reviews/g3_calibration/gpt-5.6-sol-high/calibration.json"),
        Path(
            "results/v2/reviews/g3_calibration/gpt-5.6-sol-high/response-template.json"
        ),
        Path(
            "results/v2/reviews/g3_calibration/gpt-5.6-sol-high/completed-return.json"
        ),
        Path(
            "results/v2/reviews/g3_calibration/gpt-5.6-sol-high/invocation-receipt.json"
        ),
        Path("results/v2/reviews/g3_calibration/claude-opus-5-medium/calibration.json"),
        Path(
            "results/v2/reviews/g3_calibration/claude-opus-5-medium/response-template.json"
        ),
        Path(
            "results/v2/reviews/g3_calibration/claude-opus-5-medium/completed-return.json"
        ),
        Path(
            "results/v2/reviews/g3_calibration/claude-opus-5-medium/invocation-receipt.json"
        ),
    ),
    "g4-gate": (
        Path("results/v2/gates/G3.json"),
        Path("results/v2/gates/G3.pending.json"),
        Path("results/v2/gates/G3.reviewed.json"),
        Path("results/v2/reviews/g3/kevin/final-gate-decision.json"),
        Path("results/v2/reviews/g3/gpt-5.6-sol-high/final-gate-review.json"),
        Path("results/v2/reviews/g3/gpt-5.6-sol-high/invocation-receipt.json"),
        Path("results/v2/reviews/g3/gpt-5.6-sol-high/native-output.jsonl"),
        Path("results/v2/reviews/g3/gpt-5.6-sol-high/public-review-workspace.json"),
        Path("results/v2/reviews/g3/claude-opus-5-medium/final-gate-review.json"),
        Path("results/v2/reviews/g3/claude-opus-5-medium/invocation-receipt.json"),
        Path("results/v2/reviews/g3/claude-opus-5-medium/native-output.jsonl"),
        Path("results/v2/reviews/g3/claude-opus-5-medium/public-review-workspace.json"),
        Path("results/v2/viewer/g4_export_manifest.json"),
        Path("results/v2/viewer/g4_verification.json"),
        Path("viewer/public/contextlab-viewer.v1.json"),
    ),
    "frontier-entry": (
        Path("evaluation/v2/tasks/static_completion_g2.jsonl"),
        Path("evaluation/v2/tasks/static_new_g1.jsonl"),
        Path("evaluation/v2/tasks/v1_annotated.jsonl"),
        Path("results/v2/gates/G3.json"),
        Path("results/v2/gates/G4.pending.json"),
        Path("results/v2/gates/G4.approval.json"),
        Path("results/v2/gates/G4.json"),
        Path("results/v2/frontier/entry_evidence.attempt-02.json"),
        Path("results/v2/frontier/entry_gate.attempt-02.json"),
        Path("results/v2/memory/g3_public_metrics.json"),
        Path("results/v2/reviews/g4/gpt-5.6-sol-high/invocation-receipt.json"),
        Path("results/v2/reviews/g4/gpt-5.6-sol-high/review.json"),
        Path("results/v2/reviews/g4/claude-opus-5-medium/invocation-receipt.json"),
        Path("results/v2/reviews/g4/claude-opus-5-medium/review.json"),
        Path("results/v2/splits/static_g2_freeze.json"),
        Path("results/v2/splits/task_split_manifest.json"),
        Path("results/v2/viewer/g4_export_manifest.json"),
        Path("results/v2/viewer/g4_verification.json"),
        Path("viewer/public/contextlab-viewer.v1.json"),
    ),
    "frontier-result": (
        Path("results/v2/gates/G4.json"),
        Path("evaluation/v2/frontier_protocol.json"),
    ),
}


class PublicReviewWorkspaceError(ValueError):
    """A reviewer workspace contains an unsafe, missing, or changed source."""


def review_workspace_manifest_path(anchor_path: Path) -> Path:
    """Return the durable manifest path adjacent to one review receipt."""

    if anchor_path.is_absolute() or ".." in anchor_path.parts:
        raise PublicReviewWorkspaceError("review anchor must be repository-relative")
    return anchor_path.with_name(PUBLIC_REVIEW_WORKSPACE_FILENAME)


def _safe_relative(value: str | Path, label: str) -> Path:
    raw = str(value)
    relative = Path(raw)
    if (
        not raw
        or any(ord(character) < 32 for character in raw)
        or "\\" in raw
        or relative.is_absolute()
        or ".." in relative.parts
        or relative.as_posix() != raw
        or not raw.startswith(_PATH_PREFIXES)
        or any(part.casefold() in _FORBIDDEN_PARTS for part in relative.parts)
        or any(part.startswith(".") for part in relative.parts)
        or relative.name.casefold() in _FORBIDDEN_NAMES
        or (
            relative.parts[:3] == ("results", "v2", "sealed")
            and relative not in _SAFE_CONTENT_FREE_SEALED_RESULTS
        )
        or relative.parts[:4] == ("results", "v2", "memory", "grades")
        or relative.parts[:5] == ("results", "v2", "reviews", "g3_calibration", "kevin")
    ):
        raise PublicReviewWorkspaceError(
            f"{label} is not public and repository-relative"
        )
    return relative


def _source_snapshot(repository: Path, relative: Path, label: str) -> bytes:
    """Read and bind one public source without reopening its pathname."""

    try:
        return read_bytes_snapshot(repository, relative, max_bytes=_MAX_FILE_BYTES)
    except ImmutableIOError as exc:
        raise PublicReviewWorkspaceError(
            f"{label} is not a symlink-safe stable regular file"
        ) from exc


def _strict_json_object(source_bytes: bytes, label: str) -> dict[str, Any]:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise PublicReviewWorkspaceError(f"{label} repeats a JSON field")
            value[key] = item
        return value

    def reject_constant(_: str) -> None:
        raise PublicReviewWorkspaceError(f"{label} contains a non-finite number")

    try:
        value = json.loads(
            source_bytes.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except PublicReviewWorkspaceError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PublicReviewWorkspaceError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise PublicReviewWorkspaceError(f"{label} must be a JSON object")
    return value


def _sha256_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_content_free_sealed_snapshot(
    repository: Path, relative: Path, source_bytes: bytes
) -> None:
    """Validate an opaque sealed import without exposing it to the reviewer."""

    if relative not in _SAFE_CONTENT_FREE_SEALED_RESULTS:
        return
    value = _strict_json_object(source_bytes, "content-free sealed import")
    try:
        if relative.name == "g1_fixture_import.json":
            expected = {
                "schema_version",
                "evaluation_id",
                "split_manifest_sha256",
                "candidate_manifest_sha256",
                "external_bundle_sha256",
                "source_return_sha256",
                "records",
                "aggregate_metadata",
            }
            if (
                set(value) != expected
                or value.get("schema_version") != SEALED_IMPORT_SCHEMA
            ):
                raise PublicReviewWorkspaceError(
                    "G1 content-free sealed import fields changed"
                )
            if not _sha256_text(value.get("source_return_sha256")):
                raise PublicReviewWorkspaceError(
                    "G1 content-free sealed import source hash is invalid"
                )
            split = _strict_json_object(
                _source_snapshot(
                    repository,
                    Path("results/v2/splits/task_split_manifest.json"),
                    "G1 split manifest",
                ),
                "G1 split manifest",
            )
            candidate = _strict_json_object(
                _source_snapshot(
                    repository,
                    Path("evaluation/v2/fixtures/sealed_candidate_manifest.json"),
                    "G1 sealed candidate fixture",
                ),
                "G1 sealed candidate fixture",
            )
            returned = dict(value)
            returned.pop("source_return_sha256")
            returned["schema_version"] = SEALED_RETURN_SCHEMA
            validate_sealed_return(
                returned,
                split,
                candidate,
                expected_external_bundle_sha256=str(value["external_bundle_sha256"]),
            )
            return

        required = {
            "schema_version",
            "static_freeze_manifest_sha256",
            "external_bundle_sha256",
            "retrieval_protocol_sha256",
            "source_return_sha256",
            "component_records",
        }
        allowed = required | {"generation_summary"}
        if (
            not required <= set(value) <= allowed
            or value.get("schema_version") != G2_SEALED_IMPORT_SCHEMA
            or not _sha256_text(value.get("source_return_sha256"))
        ):
            raise PublicReviewWorkspaceError(
                "G2 content-free sealed import fields changed"
            )
        returned = dict(value)
        returned.pop("source_return_sha256")
        returned["schema_version"] = G2_SEALED_RETURN_SCHEMA
        validate_g2_sealed_return(
            returned,
            static_freeze_manifest_sha256=str(value["static_freeze_manifest_sha256"]),
            external_bundle_sha256=str(value["external_bundle_sha256"]),
            retrieval_protocol_sha256=str(value["retrieval_protocol_sha256"]),
        )
    except PublicReviewWorkspaceError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise PublicReviewWorkspaceError(
            "content-free sealed import violates its strict schema"
        ) from exc


def _iter_root_files(repository: Path, relative: Path) -> Iterable[Path]:
    candidate = repository / relative
    if candidate.is_symlink():
        raise PublicReviewWorkspaceError("public review source root is a symlink")
    if not candidate.exists():
        return ()
    if candidate.is_file():
        _safe_relative(relative, "public review source")
        return (relative,)
    if not candidate.is_dir():
        raise PublicReviewWorkspaceError("public review source root is unsafe")
    files: list[Path] = []
    for path in sorted(candidate.rglob("*")):
        child = path.relative_to(repository)
        if "__pycache__" in child.parts:
            continue
        if path.is_symlink():
            raise PublicReviewWorkspaceError("public review source tree has a symlink")
        _safe_relative(child, "public review source")
        if path.is_dir():
            continue
        if not path.is_file():
            raise PublicReviewWorkspaceError(
                "public review source tree has a non-regular entry"
            )
        files.append(child)
    return tuple(files)


def _is_windows_absolute(value: str) -> bool:
    return (
        len(value) >= 3
        and value[0].isalpha()
        and value[1] == ":"
        and value[2] in {"/", "\\"}
    ) or value.startswith(("\\\\", "//"))


def _is_private_reference_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_").replace(".", "_")
    if normalized in _PRIVATE_REFERENCE_KEYS:
        return True
    tokens = set(normalized.split("_"))
    return bool(tokens & _PRIVATE_REFERENCE_TOKENS) and bool(
        tokens & _REFERENCE_KEY_TOKENS
    )


def _is_path_key(key: str | None) -> bool:
    if key is None:
        return False
    normalized = key.casefold().replace("-", "_").replace(".", "_")
    tokens = set(normalized.split("_"))
    return normalized == "path" or "path" in tokens


def _looks_like_repository_path(value: str, key: str | None) -> bool:
    first = value.replace("\\", "/").partition("/")[0].casefold()
    if first in _PUBLIC_TOP_LEVELS:
        return True
    if not _is_path_key(key):
        return False
    return (
        "/" in value
        or "\\" in value
        or Path(value).suffix.casefold() in _PATH_FILE_SUFFIXES
    )


def _string_path(value: str, *, key: str | None = None) -> Path | None:
    folded = value.casefold()
    normalized_parts = value.replace("\\", "/").split("/")
    if (
        folded.startswith("file:")
        or value.startswith(("/", "~"))
        or _is_windows_absolute(value)
        or ".." in normalized_parts
    ):
        raise PublicReviewWorkspaceError(
            "public review artifact references a non-public path"
        )
    if not _looks_like_repository_path(value, key):
        return None
    if not value.startswith(_PATH_PREFIXES):
        raise PublicReviewWorkspaceError(
            "public review artifact references a non-public path"
        )
    if value.count("#") > 1 or value.endswith("#"):
        raise PublicReviewWorkspaceError("public review artifact path is malformed")
    path_value = value.split("#", 1)[0]
    return _safe_relative(path_value, "referenced public review path")


def _validate_opaque_static_grade_reference(repository: Path, value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise PublicReviewWorkspaceError("static grade evidence reference is malformed")
    folded = value.casefold()
    relative = Path(value)
    if (
        not value
        or folded.startswith("file:")
        or value.startswith(("/", "~"))
        or _is_windows_absolute(value)
        or "\\" in value
        or any(ord(character) < 32 for character in value)
        or "#" in value
        or ".." in relative.parts
        or relative.as_posix() != value
        or relative.suffix.casefold() != ".json"
        or any(part.startswith(".") for part in relative.parts)
        or any(part.casefold() in _FORBIDDEN_PARTS for part in relative.parts)
        or relative == _OPAQUE_STATIC_GRADE_PREFIX
        or _OPAQUE_STATIC_GRADE_PREFIX not in relative.parents
    ):
        raise PublicReviewWorkspaceError(
            "static grade evidence reference is not canonical"
        )
    _source_snapshot(repository, relative, "static grade evidence reference")


def _validate_opaque_ai_calibration_response(repository: Path, value: Any) -> None:
    if not isinstance(value, str):
        raise PublicReviewWorkspaceError(
            "AI calibration response reference is malformed"
        )
    relative = Path(value)
    if relative not in _OPAQUE_AI_CALIBRATION_RESPONSES:
        raise PublicReviewWorkspaceError(
            "AI calibration response reference is not allowlisted"
        )
    _source_snapshot(repository, relative, "AI calibration response reference")


def _validate_codex_agent_reference(value: Any) -> None:
    if (
        not isinstance(value, str)
        or _CODEX_AGENT_REFERENCE.fullmatch(value) is None
        or ".." in value.split("/")
    ):
        raise PublicReviewWorkspaceError("Codex agent reference is invalid")


def _validate_opaque_native_review_evidence(repository: Path, value: Any) -> None:
    if not isinstance(value, str):
        raise PublicReviewWorkspaceError(
            "native review evidence reference is malformed"
        )
    relative = _safe_relative(value, "native review evidence reference")
    if (
        relative.parts[:3] != ("results", "v2", "reviews")
        or relative.name != "native-invocation.json"
        or "quarantine" in relative.parts
    ):
        raise PublicReviewWorkspaceError(
            "native review evidence reference is not canonical"
        )
    _source_snapshot(repository, relative, "native review evidence reference")


def _validate_opaque_json_pointers(key: str, value: Any) -> None:
    pointers = value if isinstance(value, list) else [value]
    prefixes = _OPAQUE_JSON_POINTER_PREFIXES[key]
    if not pointers or any(
        not isinstance(pointer, str)
        or len(pointer) > 512
        or not pointer.startswith(prefixes)
        or "\\" in pointer
        or ".." in pointer.split("/")
        or any(ord(character) < 32 for character in pointer)
        for pointer in pointers
    ):
        raise PublicReviewWorkspaceError("public JSON pointer is malformed")


def _referenced_paths(
    repository: Path, value: Any, *, key: str | None = None
) -> set[Path]:
    found: set[Path] = set()
    if isinstance(value, Mapping):
        for item_key, item in value.items():
            normalized_key = str(item_key).casefold()
            if normalized_key == "static_grade_evidence_path":
                _validate_opaque_static_grade_reference(repository, item)
                continue
            if normalized_key == "response_source_path":
                _validate_opaque_ai_calibration_response(repository, item)
                continue
            if normalized_key == "execution_reference":
                _validate_codex_agent_reference(item)
                continue
            if normalized_key == "native_invocation_evidence_path":
                _validate_opaque_native_review_evidence(repository, item)
                continue
            if normalized_key in _OPAQUE_JSON_POINTER_PREFIXES and (
                isinstance(item, str)
                or (
                    isinstance(item, list)
                    and all(isinstance(pointer, str) for pointer in item)
                )
            ):
                _validate_opaque_json_pointers(normalized_key, item)
                continue
            if _is_private_reference_key(normalized_key):
                raise PublicReviewWorkspaceError(
                    "public review artifact contains a private reference key"
                )
            found.update(_referenced_paths(repository, item, key=normalized_key))
    elif isinstance(value, list):
        for item in value:
            found.update(_referenced_paths(repository, item, key=key))
    elif isinstance(value, str):
        relative = _string_path(value, key=key)
        if relative is not None:
            found.add(relative)
    return found


def _json_references(
    repository: Path, relative: Path, source_bytes: bytes
) -> set[Path]:
    if relative.suffix.casefold() != ".json":
        return set()
    try:
        value = json.loads(source_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicReviewWorkspaceError("public review JSON is unreadable") from exc
    if relative in _OPAQUE_INVENTORY_FILES:
        return set()
    return _referenced_paths(repository, value)


def _binding_paths(bindings: Mapping[str, str]) -> set[Path]:
    paths: set[Path] = set()
    for key, value in bindings.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise PublicReviewWorkspaceError("review target bindings are invalid")
        normalized_key = key.casefold()
        if _is_private_reference_key(normalized_key):
            raise PublicReviewWorkspaceError(
                "review target contains a private reference key"
            )
        if key == "path" or key.endswith("_path"):
            paths.add(_safe_relative(value, f"{key} binding"))
        else:
            _string_path(value, key=normalized_key)
    if not paths:
        raise PublicReviewWorkspaceError("review target has no bound artifact path")
    return paths


def collect_public_review_workspace(
    repository: Path,
    *,
    review_kind: str,
    target_bindings: Mapping[str, str],
) -> dict[str, Any]:
    """Hash the exact public file closure exposed to one reviewer."""

    root = Path(os.path.abspath(repository))
    if root.is_symlink() or not root.is_dir():
        raise PublicReviewWorkspaceError("public review repository root is unsafe")
    if review_kind not in _REVIEW_KIND_SEEDS:
        raise PublicReviewWorkspaceError("unknown public review kind")
    queued = set(_binding_paths(target_bindings))
    queued.update(
        path for path in _REVIEW_KIND_SEEDS[review_kind] if (root / path).is_file()
    )
    if review_kind in {"g4-gate", "frontier-entry"}:
        queued.update(
            path for path in _REVIEW_KIND_SEEDS["g3-gate"] if (root / path).is_file()
        )
        viewer_manifest_sha256 = target_bindings.get("viewer_manifest_sha256")
        if review_kind == "frontier-entry":
            viewer_manifest_path = Path("results/v2/viewer/g4_export_manifest.json")
            if (root / viewer_manifest_path).is_file():
                viewer_manifest = _source_snapshot(
                    root,
                    viewer_manifest_path,
                    "G4 viewer manifest",
                )
                viewer_manifest_sha256 = hashlib.sha256(viewer_manifest).hexdigest()
        if viewer_manifest_sha256 is not None:
            if not _sha256_text(viewer_manifest_sha256):
                raise PublicReviewWorkspaceError(
                    "G4 viewer manifest binding is invalid"
                )
            queued.add(
                Path(
                    "viewer/public/artifacts/"
                    f"{viewer_manifest_sha256}/g4_export_manifest.json"
                )
            )
    # Frontier-result review is evidence review, not implementation review. Its
    # exact technical record, current entry gate, and result artifacts arrive
    # through target bindings and their recursive public references. Do not add
    # the implementation tree to this workspace, especially for Claude review.
    if review_kind != "frontier-result":
        for source_root in _SOURCE_ROOTS:
            queued.update(_iter_root_files(root, source_root))
    if review_kind in {"g4-gate", "frontier-entry"}:
        for source_root in _G4_SOURCE_ROOTS:
            queued.update(_iter_root_files(root, source_root))

    visited: set[Path] = set()
    rows: list[dict[str, Any]] = []
    total_bytes = 0
    while queued:
        relative = min(queued, key=Path.as_posix)
        queued.remove(relative)
        if relative in visited:
            continue
        source_bytes = _source_snapshot(root, relative, "public review source")
        visited.add(relative)
        if relative in _SAFE_CONTENT_FREE_SEALED_RESULTS:
            _validate_content_free_sealed_snapshot(root, relative, source_bytes)
            continue
        size = len(source_bytes)
        total_bytes += size
        if len(visited) > _MAX_FILES or total_bytes > _MAX_TOTAL_BYTES:
            raise PublicReviewWorkspaceError(
                "public review workspace exceeds its bound"
            )
        rows.append(
            {
                "path": relative.as_posix(),
                "sha256": hashlib.sha256(source_bytes).hexdigest(),
                "size_bytes": size,
            }
        )
        try:
            references = _json_references(root, relative, source_bytes)
        except PublicReviewWorkspaceError as exc:
            raise PublicReviewWorkspaceError(
                f"public review source {relative.as_posix()} has an unsafe reference: "
                f"{exc}"
            ) from exc
        for referenced in references:
            queued.add(referenced)

    rows.sort(key=lambda row: str(row["path"]))
    manifest: dict[str, Any] = {
        "schema_version": PUBLIC_REVIEW_WORKSPACE_SCHEMA,
        "review_kind": review_kind,
        "target_bindings": dict(sorted(target_bindings.items())),
        "file_count": len(rows),
        "total_bytes": total_bytes,
        "files": rows,
    }
    manifest["artifact_sha256"] = sha256_json(manifest)
    return manifest


def validate_public_review_workspace_manifest(value: Mapping[str, Any]) -> None:
    """Validate one stored public-review manifest without trusting its hash."""

    expected = {
        "schema_version",
        "review_kind",
        "target_bindings",
        "file_count",
        "total_bytes",
        "files",
        "artifact_sha256",
    }
    if (
        set(value) != expected
        or value.get("schema_version") != PUBLIC_REVIEW_WORKSPACE_SCHEMA
    ):
        raise PublicReviewWorkspaceError("public review workspace fields changed")
    bindings = value.get("target_bindings")
    files = value.get("files")
    if not isinstance(bindings, Mapping) or not isinstance(files, list) or not files:
        raise PublicReviewWorkspaceError("public review workspace is incomplete")
    normalized_bindings = {
        str(key): item
        for key, item in bindings.items()
        if isinstance(key, str) and isinstance(item, str)
    }
    if normalized_bindings != bindings:
        raise PublicReviewWorkspaceError("public review bindings are invalid")
    if value.get("review_kind") not in _REVIEW_KIND_SEEDS:
        raise PublicReviewWorkspaceError("public review kind is invalid")
    _binding_paths(normalized_bindings)
    seen: set[str] = set()
    total = 0
    for row in files:
        if not isinstance(row, Mapping) or set(row) != {"path", "sha256", "size_bytes"}:
            raise PublicReviewWorkspaceError("public review file row is invalid")
        path = row.get("path")
        digest = row.get("sha256")
        size = row.get("size_bytes")
        if (
            not isinstance(path, str)
            or path in seen
            or _safe_relative(path, "public review file").as_posix() != path
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or size > _MAX_FILE_BYTES
        ):
            raise PublicReviewWorkspaceError("public review file row is invalid")
        if Path(path) in _SAFE_CONTENT_FREE_SEALED_RESULTS:
            raise PublicReviewWorkspaceError(
                "content-free sealed imports must remain opaque to reviewers"
            )
        seen.add(path)
        total += size
    if (
        files != sorted(files, key=lambda row: str(row["path"]))
        or value.get("file_count") != len(files)
        or value.get("total_bytes") != total
        or total > _MAX_TOTAL_BYTES
        or value.get("artifact_sha256")
        != sha256_json(
            {key: item for key, item in value.items() if key != "artifact_sha256"}
        )
    ):
        raise PublicReviewWorkspaceError(
            "public review workspace hash or totals changed"
        )


def materialize_public_review_workspace(
    repository: Path, workspace: Path, manifest: Mapping[str, Any]
) -> None:
    """Copy the manifest's exact bytes into a new isolated workspace."""

    validate_public_review_workspace_manifest(manifest)
    root = Path(os.path.abspath(repository))
    destination_root = Path(os.path.abspath(workspace))
    if (
        root.is_symlink()
        or not root.is_dir()
        or destination_root.is_symlink()
        or not destination_root.is_dir()
    ):
        raise PublicReviewWorkspaceError("public review workspace root is unsafe")
    if destination_root == root or root in destination_root.parents:
        raise PublicReviewWorkspaceError(
            "public review workspace must be outside the repository"
        )
    if any(destination_root.iterdir()):
        raise PublicReviewWorkspaceError("public review workspace must start empty")
    for row in manifest["files"]:
        relative = Path(str(row["path"]))
        source_bytes = _source_snapshot(root, relative, "public review source")
        if (
            len(source_bytes) != row["size_bytes"]
            or hashlib.sha256(source_bytes).hexdigest() != row["sha256"]
        ):
            raise PublicReviewWorkspaceError("public review source changed before copy")
        target = destination_root / relative
        try:
            write_bytes_once_or_verify(destination_root, target, source_bytes)
        except ImmutableIOError as exc:
            raise PublicReviewWorkspaceError(
                "public review copy could not be published safely"
            ) from exc
    manifest_path = destination_root / PUBLIC_REVIEW_WORKSPACE_FILENAME
    try:
        write_bytes_once_or_verify(
            destination_root,
            manifest_path,
            (json.dumps(dict(manifest), indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            ),
        )
    except ImmutableIOError as exc:
        raise PublicReviewWorkspaceError(
            "public review manifest could not be published safely"
        ) from exc
