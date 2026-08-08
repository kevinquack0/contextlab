#!/usr/bin/env python3
"""Build and verify the curated ContextLab public repository.

The private research repository is the evidence vault. This tool copies only an
explicit allowlist, replaces the oversized historical G2 viewer artifact with a
lineage-preserving projection, and records every public byte in one deterministic
manifest. It uses only the Python standard library so the exported repository can
verify itself without access to the private vault.
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import stat
import tempfile
from typing import Any, Mapping, Sequence
from urllib.parse import unquote


CONFIG_SCHEMA = "contextlab.public-release-config.v1"
MANIFEST_SCHEMA = "contextlab.public-release-manifest.v1"
G2_PROJECTION_SCHEMA = "contextlab.portfolio-g2-projection.v1"
G4_PROJECTION_SCHEMA = "contextlab.portfolio-g4-manifest-projection.v1"
STORY_EVIDENCE_SCHEMA = "contextlab.story-evidence.v1"
MANIFEST_NAME = "PUBLIC_RELEASE_MANIFEST.json"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_JSON_ARRAY_INDEX = re.compile(r"0|[1-9][0-9]*")
_INVALID_POINTER_ESCAPE = re.compile(r"~(?:[^01]|$)")
_LOCAL_ANGLE_PATH = re.compile(r"</(?:Users|Volumes)/[^>]+>")
_LOCAL_FILE_URI = re.compile(r"file:///(?:Users|Volumes)/[^\s\"')>]+")
_LOCAL_ABSOLUTE_PATH = re.compile(r"/(?:Users|Volumes)/[^\s\"')>,]+")
_PRIVATE_PATHS = (
    re.compile(
        rb"(?<![A-Za-z0-9])/(?:Users|Volumes|Applications|home|private|tmp|var)/"
        rb"[^\s\"')>,]+"
    ),
    re.compile(
        rb"file:///(?:Users|Volumes|Applications|home|private|tmp|var)/"
        rb"[^\s\"')>,]+"
    ),
    re.compile(rb"[A-Za-z]:\\Users\\[A-Za-z0-9._-]+\\"),
)
_SECRET_PATTERNS = (
    ("private key", re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")),
    ("GitHub token", re.compile(rb"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("OpenAI-style key", re.compile(rb"sk-[A-Za-z0-9_-]{32,}")),
    ("AWS access key", re.compile(rb"AKIA[0-9A-Z]{16}")),
    (
        "JWT bearer token",
        re.compile(rb"eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}"),
    ),
    ("Slack token", re.compile(rb"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("GitLab token", re.compile(rb"glpat-[A-Za-z0-9_-]{20,}")),
    ("npm token", re.compile(rb"npm_[A-Za-z0-9]{20,}")),
    (
        "Stripe secret key",
        re.compile(rb"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"),
    ),
    ("Google API key", re.compile(rb"AIza[0-9A-Za-z_-]{35}")),
    ("Anthropic API key", re.compile(rb"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("Vercel token", re.compile(rb"vercel_[A-Za-z0-9_-]{20,}")),
)
_SENSITIVE_FILENAMES = {
    ".env",
    ".npmrc",
    ".pypirc",
    "credentials.json",
    "id_dsa",
    "id_ed25519",
    "id_rsa",
}
_SENSITIVE_SUFFIXES = {".key", ".p12", ".pem", ".pfx"}
_FORBIDDEN_DATA_FIELDS = {
    "answer_key",
    "correct_answer",
    "expected_answer",
    "gold_answer",
    "gold_evidence",
    "gold_label",
    "grader_packet",
    "grading_label",
    "evaluator_only",
    "evaluator_truth",
    "final_grade",
    "private_grade",
    "private_review",
    "private_reviewer_data",
    "protected_answer",
    "protected_data",
    "protected_truth",
    "reference_answer",
    "reviewer_notes",
    "sealed_answer",
    "sealed_content",
    "sealed_data",
    "sealed_truth",
    "scoring_notes",
    "unpublished_reviewer_data",
}
_FORBIDDEN_DATA_FIELD_KEYS = {
    re.sub(r"[^a-z0-9]", "", field.casefold()) for field in _FORBIDDEN_DATA_FIELDS
}
_PROTECTED_TEXT_PATTERNS = (
    (
        "protected answer material",
        re.compile(
            rb"\b(?:gold|expected|reference|correct)\s+"
            rb"(?:answer|evidence|label)\s*[:=]",
            re.IGNORECASE,
        ),
    ),
    (
        "sealed or protected truth",
        re.compile(
            rb"\b(?:protected|sealed)\s+(?:answer|content|data|truth)\s*[:=]",
            re.IGNORECASE,
        ),
    ),
    (
        "private review material",
        re.compile(
            rb"\bprivate\s+(?:grade|review|reviewer\s+data)\s*[:=]",
            re.IGNORECASE,
        ),
    ),
    (
        "evaluator-only material",
        re.compile(rb"\bevaluator[- ]only\s*[:=]", re.IGNORECASE),
    ),
)
_TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".jsonl",
    ".jsx",
    ".md",
    ".py",
    ".scss",
    ".sh",
    ".srt",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".vtt",
    ".yaml",
    ".yml",
}
_PROTECTED_TEXT_SUFFIXES = {
    ".csv",
    ".md",
    ".srt",
    ".tsv",
    ".txt",
    ".vtt",
    ".yaml",
    ".yml",
}
_ARTIFACT_FIELDS = {"kind", "label", "mediaType", "path", "sha256", "staticUrl"}
_INLINE_MARKDOWN_LINK = re.compile(
    r"!?\[[^\]\n]*\]\(\s*(?:<([^>]+)>|([^\s)]+))(?:\s+[^)]*)?\)"
)
_REFERENCE_TARGET = re.compile(
    r"^\s{0,3}\[[^\]]+\]:\s*(?:<([^>]+)>|([^\s]+))", re.MULTILINE
)
_HTML_LINK = re.compile(r"\b(?:href|src)\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_ATX_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_HTML_ID = re.compile(r"\bid\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)


class PublicReleaseError(ValueError):
    """The requested public release is unsafe, stale, or not reproducible."""


@dataclass(frozen=True)
class PlannedFile:
    path: str
    data: bytes
    source_path: str
    source_sha256: str
    projection_lineage: Mapping[str, Any] | None = None

    @property
    def sha256(self) -> str:
        return _sha256_bytes(self.data)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _json_file_bytes(value: Any, *, compact: bool = False) -> bytes:
    if compact:
        return _canonical_json(value) + b"\n"
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _semantic_artifact_sha256(value: Mapping[str, Any]) -> str:
    return _sha256_bytes(
        _canonical_json(
            {key: item for key, item in value.items() if key != "artifact_sha256"}
        )
    )


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PublicReleaseError(f"JSON contains a duplicate field: {key}")
        result[key] = value
    return result


def _load_json_bytes(data: bytes, label: str) -> Any:
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicReleaseError(f"{label} is not valid UTF-8 JSON") from exc


def _relative_path(raw: object, label: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise PublicReleaseError(f"{label} must be a non-empty relative path")
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise PublicReleaseError(f"{label} must be a normalized relative path")
    if raw != path.as_posix() or "\\" in raw:
        raise PublicReleaseError(f"{label} must use normalized POSIX separators")
    return raw


def _safe_root(root: Path, label: str) -> Path:
    absolute = Path(os.path.abspath(root))
    try:
        metadata = absolute.lstat()
    except OSError as exc:
        raise PublicReleaseError(f"{label} does not exist") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PublicReleaseError(f"{label} must be a real directory")
    return absolute.resolve(strict=True)


def _safe_source_path(root: Path, relative: str, label: str) -> Path:
    relative = _relative_path(relative, label)
    current = root
    for part in PurePosixPath(relative).parts:
        current /= part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise PublicReleaseError(f"{label} is missing: {relative}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise PublicReleaseError(f"{label} traverses a symlink: {relative}")
    try:
        current.resolve(strict=True).relative_to(root)
    except ValueError as exc:
        raise PublicReleaseError(f"{label} escapes the repository: {relative}") from exc
    return current


def _read_regular(
    root: Path, relative: str, label: str, *, maximum: int | None = None
) -> bytes:
    path = _safe_source_path(root, relative, label)
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise PublicReleaseError(f"{label} is not a regular file: {relative}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise PublicReleaseError(f"{label} is not a regular file: {relative}")
        if maximum is not None and before.st_size >= maximum:
            raise PublicReleaseError(
                f"{label} is {before.st_size} bytes; limit is less than {maximum}"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            total += len(block)
            if maximum is not None and total >= maximum:
                raise PublicReleaseError(
                    f"{label} is at least {total} bytes; limit is less than {maximum}"
                )
            chunks.append(block)
        after = os.fstat(descriptor)
        current = path.lstat()
        identity = (before.st_dev, before.st_ino)
        if (
            identity != (after.st_dev, after.st_ino)
            or identity != (current.st_dev, current.st_ino)
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
        ):
            raise PublicReleaseError(f"{label} changed while it was read: {relative}")
        return b"".join(chunks)
    except OSError as exc:
        raise PublicReleaseError(f"cannot read {label}: {relative}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _load_config(root: Path, config_path: str) -> dict[str, Any]:
    data = _read_regular(root, config_path, "public release config")
    value = _load_json_bytes(data, "public release config")
    if not isinstance(value, dict) or value.get("schema_version") != CONFIG_SCHEMA:
        raise PublicReleaseError("public release config schema is unsupported")
    return value


def _walk_tree(root: Path, relative: str) -> list[str]:
    tree = _safe_source_path(root, relative, "allowlisted tree")
    if not tree.is_dir():
        raise PublicReleaseError(f"allowlisted tree is not a directory: {relative}")
    discovered: list[str] = []

    def visit(directory: Path) -> None:
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            metadata = child.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise PublicReleaseError(
                    f"allowlisted tree contains a symlink: {child}"
                )
            if stat.S_ISDIR(metadata.st_mode):
                if child.name in {"__pycache__", "node_modules"}:
                    continue
                visit(child)
            elif stat.S_ISREG(metadata.st_mode):
                discovered.append(child.relative_to(root).as_posix())
            else:
                raise PublicReleaseError(
                    f"allowlisted tree contains a non-regular entry: {child}"
                )

    visit(tree)
    return discovered


def _allowlisted_paths(root: Path, config: Mapping[str, Any]) -> dict[str, str | None]:
    selected: dict[str, str | None] = {}
    files = config.get("files")
    trees = config.get("trees")
    if not isinstance(files, list) or not isinstance(trees, list):
        raise PublicReleaseError("public release allowlist is malformed")
    for index, entry in enumerate(files):
        if not isinstance(entry, dict):
            raise PublicReleaseError(f"allowlisted file {index} is malformed")
        relative = _relative_path(entry.get("path"), f"allowlisted file {index}")
        required = entry.get("required")
        expected = entry.get("sha256")
        if not isinstance(required, bool):
            raise PublicReleaseError(
                f"allowlisted file {relative} has no required flag"
            )
        if expected is not None and (
            not isinstance(expected, str) or _SHA256.fullmatch(expected) is None
        ):
            raise PublicReleaseError(f"allowlisted file {relative} has an invalid hash")
        candidate = root / relative
        if not candidate.exists():
            if required:
                raise PublicReleaseError(
                    f"required allowlisted file is missing: {relative}"
                )
            continue
        if relative in selected:
            raise PublicReleaseError(f"allowlisted file is duplicated: {relative}")
        selected[relative] = expected
    for index, entry in enumerate(trees):
        if not isinstance(entry, dict):
            raise PublicReleaseError(f"allowlisted tree {index} is malformed")
        relative = _relative_path(entry.get("path"), f"allowlisted tree {index}")
        required = entry.get("required")
        suffixes = entry.get("suffixes")
        if (
            not isinstance(required, bool)
            or not isinstance(suffixes, list)
            or not suffixes
        ):
            raise PublicReleaseError(f"allowlisted tree {relative} is malformed")
        allowed_suffixes = {
            suffix
            for suffix in suffixes
            if isinstance(suffix, str) and suffix.startswith(".")
        }
        if len(allowed_suffixes) != len(suffixes):
            raise PublicReleaseError(
                f"allowlisted tree {relative} has invalid suffixes"
            )
        tree_path = root / relative
        if not tree_path.exists():
            if required:
                raise PublicReleaseError(
                    f"required allowlisted tree is missing: {relative}"
                )
            continue
        matches = [
            path
            for path in _walk_tree(root, relative)
            if Path(path).suffix in allowed_suffixes
        ]
        if required and not matches:
            raise PublicReleaseError(f"allowlisted tree is empty: {relative}")
        for path in matches:
            if path in selected:
                raise PublicReleaseError(f"allowlisted path is duplicated: {path}")
            selected[path] = None
    return selected


def _scan_json_fields(value: Any, label: str, path: str = "$") -> None:
    if isinstance(value, list):
        for index, item in enumerate(value):
            _scan_json_fields(item, label, f"{path}[{index}]")
        return
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        if not isinstance(key, str):
            raise PublicReleaseError(f"{label} has a non-text JSON field at {path}")
        normalized_key = re.sub(r"[^a-z0-9]", "", key.casefold())
        if normalized_key in _FORBIDDEN_DATA_FIELD_KEYS:
            raise PublicReleaseError(
                f"{label} exposes a protected field at {path}.{key}"
            )
        _scan_json_fields(item, label, f"{path}.{key}")


def _public_relative_local_reference(raw: str) -> str:
    value = raw.removeprefix("file://")
    parts = Path(value).parts
    if "TCC" in parts:
        return Path(*parts[parts.index("TCC") + 1 :]).as_posix().replace(" ", "%20")
    return f"local-reference/{Path(value).name}".replace(" ", "%20")


def _sanitize_public_string(value: str) -> str:
    projected = _LOCAL_ANGLE_PATH.sub(
        lambda match: f"<{_public_relative_local_reference(match.group()[1:-1])}>",
        value,
    )
    projected = _LOCAL_FILE_URI.sub(
        lambda match: _public_relative_local_reference(match.group()), projected
    )
    return _LOCAL_ABSOLUTE_PATH.sub(
        lambda match: _public_relative_local_reference(match.group()), projected
    )


def _public_projection_value(value: Any) -> Any:
    if isinstance(value, str):
        return _sanitize_public_string(value)
    if isinstance(value, list):
        return [_public_projection_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _public_projection_value(item) for key, item in value.items()}
    return copy.deepcopy(value)


def _scan_file(relative: str, data: bytes) -> None:
    path = PurePosixPath(relative)
    lowered_name = path.name.casefold()
    if (
        lowered_name in _SENSITIVE_FILENAMES
        or path.suffix.casefold() in _SENSITIVE_SUFFIXES
    ):
        raise PublicReleaseError(f"sensitive file name is not public: {relative}")
    for pattern in _PRIVATE_PATHS:
        if pattern.search(data):
            raise PublicReleaseError(
                f"private absolute path found in public file: {relative}"
            )
    for label, pattern in _SECRET_PATTERNS:
        match = pattern.search(data)
        if match is not None and not any(
            marker in match.group().lower()
            for marker in (b"example", b"fixture", b"placeholder")
        ):
            raise PublicReleaseError(f"{label} found in public file: {relative}")
    suffix = path.suffix.casefold()
    if suffix in _TEXT_SUFFIXES:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PublicReleaseError(
                f"public text file is not UTF-8: {relative}"
            ) from exc
        if suffix in _PROTECTED_TEXT_SUFFIXES:
            for label, pattern in _PROTECTED_TEXT_PATTERNS:
                if pattern.search(data):
                    raise PublicReleaseError(
                        f"{label} found in public file: {relative}"
                    )
    if suffix == ".json":
        value = _load_json_bytes(data, relative)
        _scan_json_fields(value, relative)
    elif suffix == ".jsonl":
        for line_number, line in enumerate(data.splitlines(), start=1):
            if not line.strip():
                continue
            value = _load_json_bytes(line, f"{relative}:{line_number}")
            _scan_json_fields(value, f"{relative}:{line_number}")


def _markdown_without_fenced_code(text: str) -> str:
    lines: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        stripped = line.lstrip()
        marker = stripped[:3]
        if fence is None and marker in {"```", "~~~"}:
            fence = marker
            lines.append("")
            continue
        if fence is not None:
            if stripped.startswith(fence):
                fence = None
            lines.append("")
            continue
        lines.append(line)
    return "\n".join(lines)


def _markdown_targets(text: str) -> list[str]:
    visible = _markdown_without_fenced_code(text)
    targets: list[str] = []
    for match in _INLINE_MARKDOWN_LINK.finditer(visible):
        targets.append(match.group(1) or match.group(2))
    for match in _REFERENCE_TARGET.finditer(visible):
        targets.append(match.group(1) or match.group(2))
    targets.extend(match.group(1) for match in _HTML_LINK.finditer(visible))
    return targets


def _markdown_local_target(
    source_path: str, raw_target: str
) -> tuple[str, str, str] | None:
    target = raw_target.strip()
    if (
        not target
        or target.startswith("//")
        or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target)
    ):
        return None
    path_part, separator, fragment = target.partition("#")
    decoded_path = unquote(path_part)
    decoded_fragment = unquote(fragment) if separator else ""
    if decoded_path.startswith("/"):
        unresolved = decoded_path.lstrip("/")
    elif decoded_path:
        unresolved = (
            PurePosixPath(source_path).parent / PurePosixPath(decoded_path)
        ).as_posix()
    else:
        unresolved = source_path
    candidate = posixpath.normpath(unresolved)
    if candidate == ".." or candidate.startswith("../"):
        raise PublicReleaseError(f"Markdown target escapes bundle: {target}")
    return (
        _relative_path(candidate, "Markdown link target"),
        decoded_fragment,
        decoded_path,
    )


def _github_slug(raw: str) -> str:
    text = re.sub(r"<[^>]+>", "", raw)
    text = re.sub(r"!?\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = text.replace("`", "").replace("*", "").replace("_", "")
    text = text.casefold().strip()
    text = "".join(
        character
        for character in text
        if character.isalnum() or character in {" ", "-"}
    )
    return re.sub(r"\s", "-", text)


def _markdown_anchors(data: bytes, label: str) -> set[str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PublicReleaseError(f"Markdown file is not UTF-8: {label}") from exc
    visible = _markdown_without_fenced_code(text)
    anchors: set[str] = set()
    slug_counts: dict[str, int] = {}
    lines = visible.splitlines()
    for index, line in enumerate(lines):
        heading: str | None = None
        match = _ATX_HEADING.match(line)
        if match is not None:
            heading = match.group(1)
        elif index + 1 < len(lines) and re.fullmatch(
            r"\s{0,3}(?:=+|-+)\s*", lines[index + 1]
        ):
            if line.strip():
                heading = line.strip()
        if heading is not None:
            base = _github_slug(heading)
            count = slug_counts.get(base, 0)
            slug_counts[base] = count + 1
            anchors.add(base if count == 0 else f"{base}-{count}")
    anchors.update(match.group(1) for match in _HTML_ID.finditer(visible))
    return anchors


def _verify_markdown_links(planned: Mapping[str, PlannedFile]) -> None:
    paths = set(planned)
    anchor_cache: dict[str, set[str]] = {}
    errors: list[str] = []
    for source_path in sorted(paths):
        if not source_path.casefold().endswith(".md"):
            continue
        source = planned[source_path]
        try:
            text = source.data.decode("utf-8")
        except UnicodeDecodeError:
            errors.append(f"{source_path}: Markdown is not UTF-8")
            continue
        for raw_target in _markdown_targets(text):
            try:
                local = _markdown_local_target(source_path, raw_target)
            except PublicReleaseError as exc:
                errors.append(f"{source_path}: {raw_target.strip()}: {exc}")
                continue
            if local is None:
                continue
            candidate, decoded_fragment, _decoded_path = local
            is_directory = any(
                path.startswith(candidate.rstrip("/") + "/") for path in paths
            )
            if candidate not in paths and not is_directory:
                errors.append(f"{source_path}: missing target {raw_target.strip()}")
                continue
            if (
                decoded_fragment
                and candidate in paths
                and candidate.casefold().endswith(".md")
            ):
                if candidate not in anchor_cache:
                    anchor_cache[candidate] = _markdown_anchors(
                        planned[candidate].data, candidate
                    )
                if decoded_fragment not in anchor_cache[candidate]:
                    errors.append(
                        f"{source_path}: missing anchor #{decoded_fragment} in {candidate}"
                    )
    if errors:
        preview = "; ".join(errors[:20])
        suffix = f"; and {len(errors) - 20} more" if len(errors) > 20 else ""
        raise PublicReleaseError(
            f"public Markdown links do not resolve: {preview}{suffix}"
        )


def _valid_pointer(pointer: object) -> bool:
    return bool(
        isinstance(pointer, str)
        and pointer.startswith("/")
        and _INVALID_POINTER_ESCAPE.search(pointer) is None
        and not any(ord(character) < 0x20 for character in pointer)
    )


def _pointer_tokens(pointer: str) -> list[str]:
    if not _valid_pointer(pointer):
        raise PublicReleaseError(f"JSON pointer is malformed: {pointer!r}")
    return [
        token.replace("~1", "/").replace("~0", "~") for token in pointer[1:].split("/")
    ]


def _resolve_pointer(document: Any, pointer: str, label: str) -> Any:
    current = document
    for token in _pointer_tokens(pointer):
        if isinstance(current, dict):
            if token not in current:
                raise PublicReleaseError(f"{label} pointer does not exist: {pointer}")
            current = current[token]
        elif isinstance(current, list):
            if _JSON_ARRAY_INDEX.fullmatch(token) is None:
                raise PublicReleaseError(
                    f"{label} has an invalid array pointer: {pointer}"
                )
            index = int(token)
            if index >= len(current):
                raise PublicReleaseError(f"{label} pointer does not exist: {pointer}")
            current = current[index]
        else:
            raise PublicReleaseError(f"{label} pointer traverses a scalar: {pointer}")
    return current


def _new_container(next_token: str) -> dict[str, Any] | list[Any]:
    return [] if _JSON_ARRAY_INDEX.fullmatch(next_token) else {}


def _set_pointer(document: dict[str, Any], pointer: str, value: Any) -> None:
    tokens = _pointer_tokens(pointer)
    current: Any = document
    for offset, token in enumerate(tokens):
        final = offset == len(tokens) - 1
        next_token = tokens[offset + 1] if not final else ""
        if isinstance(current, dict):
            if final:
                if token in current and current[token] != value:
                    raise PublicReleaseError(f"projection pointer conflicts: {pointer}")
                current[token] = copy.deepcopy(value)
                return
            if token not in current or current[token] is None:
                current[token] = _new_container(next_token)
            current = current[token]
            continue
        if isinstance(current, list):
            if _JSON_ARRAY_INDEX.fullmatch(token) is None:
                raise PublicReleaseError(
                    f"projection pointer array index is invalid: {pointer}"
                )
            index = int(token)
            while len(current) <= index:
                current.append(None)
            if final:
                if current[index] is not None and current[index] != value:
                    raise PublicReleaseError(f"projection pointer conflicts: {pointer}")
                current[index] = copy.deepcopy(value)
                return
            if current[index] is None:
                current[index] = _new_container(next_token)
            current = current[index]
            continue
        raise PublicReleaseError(f"projection pointer traverses a scalar: {pointer}")


def _is_artifact_ref(value: object) -> bool:
    return isinstance(value, dict) and set(value) == _ARTIFACT_FIELDS


def _artifact_matches(value: object, *, path: str, digest: str) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("path") == path
        and value.get("sha256") == digest
    )


def _artifact_exclusions(compact: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = compact.get("excluded_artifacts", [])
    if not isinstance(raw, list):
        raise PublicReleaseError("compact-viewer artifact exclusions are malformed")
    exclusions: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict) or set(entry) not in (
            {"path", "reason", "sha256"},
            {"path", "reason", "replacement", "sha256"},
        ):
            raise PublicReleaseError(
                f"compact-viewer artifact exclusion {index} is malformed"
            )
        path = _relative_path(entry.get("path"), f"artifact exclusion {index}")
        digest = entry.get("sha256")
        reason = entry.get("reason")
        if (
            not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or not isinstance(reason, str)
            or not reason.strip()
            or (path, digest) in identities
        ):
            raise PublicReleaseError(
                f"compact-viewer artifact exclusion {index} is invalid"
            )
        identities.add((path, digest))
        normalized: dict[str, Any] = {
            "path": path,
            "sha256": digest,
            "reason": reason,
        }
        replacement = entry.get("replacement")
        if replacement is not None:
            if not isinstance(replacement, dict):
                raise PublicReleaseError(
                    f"compact-viewer artifact exclusion {index} replacement is malformed"
                )
            replacement_static = _static_path(replacement)
            if replacement.get("path") != replacement_static or (
                replacement.get("path") == path
                and replacement.get("sha256") == digest
            ):
                raise PublicReleaseError(
                    f"compact-viewer artifact exclusion {index} replacement is invalid"
                )
            normalized["replacement"] = copy.deepcopy(replacement)
        exclusions.append(normalized)
    return exclusions


_PRUNED_ARTIFACT = object()


def _prune_artifact_refs(
    value: Any, exclusions: Sequence[Mapping[str, str]]
) -> tuple[Any, dict[str, int]]:
    counts = {entry["path"]: 0 for entry in exclusions}

    def walk(item: Any, *, in_list: bool = False) -> Any:
        if isinstance(item, list):
            rewritten: list[Any] = []
            for child in item:
                result = walk(child, in_list=True)
                if result is not _PRUNED_ARTIFACT:
                    rewritten.append(result)
            return rewritten
        if not isinstance(item, dict):
            return item
        if _is_artifact_ref(item):
            for exclusion in exclusions:
                if (
                    item.get("path") == exclusion["path"]
                    and item.get("sha256") == exclusion["sha256"]
                ):
                    if not in_list:
                        raise PublicReleaseError(
                            "excluded viewer artifact is not in a removable list: "
                            f"{exclusion['path']}"
                        )
                    counts[exclusion["path"]] += 1
                    replacement = exclusion.get("replacement")
                    if isinstance(replacement, dict):
                        return copy.deepcopy(replacement)
                    return _PRUNED_ARTIFACT
            return copy.deepcopy(item)
        return {key: walk(child) for key, child in item.items()}

    rewritten = walk(value)
    return rewritten, counts


def _collect_artifact_pointers(
    value: Any, *, artifact_path: str, artifact_sha256: str
) -> set[str]:
    found: set[str] = set()
    if isinstance(value, list):
        for item in value:
            found.update(
                _collect_artifact_pointers(
                    item,
                    artifact_path=artifact_path,
                    artifact_sha256=artifact_sha256,
                )
            )
        return found
    if not isinstance(value, dict):
        return found
    artifact = value.get("artifact")
    pointer = value.get("jsonPointer")
    if _artifact_matches(artifact, path=artifact_path, digest=artifact_sha256):
        if isinstance(pointer, str):
            if not _valid_pointer(pointer):
                raise PublicReleaseError(
                    f"viewer has a malformed artifact pointer: {pointer}"
                )
            found.add(pointer)
    for item in value.values():
        found.update(
            _collect_artifact_pointers(
                item,
                artifact_path=artifact_path,
                artifact_sha256=artifact_sha256,
            )
        )
    return found


def _artifact_ref(path: str, digest: str, name: str, label: str) -> dict[str, str]:
    return {
        "kind": "report",
        "label": label,
        "mediaType": "application/json",
        "path": path,
        "sha256": digest,
        "staticUrl": f"./artifacts/{digest}/{name}",
    }


def _rewrite_artifacts(
    value: Any,
    *,
    g2_path: str,
    g2_digest: str,
    g2_ref: Mapping[str, str],
    g4_digest: str,
    g4_ref: Mapping[str, str],
) -> tuple[Any, int, int]:
    if isinstance(value, list):
        rewritten: list[Any] = []
        g2_count = 0
        g4_count = 0
        for item in value:
            result, item_g2, item_g4 = _rewrite_artifacts(
                item,
                g2_path=g2_path,
                g2_digest=g2_digest,
                g2_ref=g2_ref,
                g4_digest=g4_digest,
                g4_ref=g4_ref,
            )
            rewritten.append(result)
            g2_count += item_g2
            g4_count += item_g4
        return rewritten, g2_count, g4_count
    if not isinstance(value, dict):
        return value, 0, 0
    if _is_artifact_ref(value):
        if _artifact_matches(value, path=g2_path, digest=g2_digest):
            return dict(g2_ref), 1, 0
        if value.get("sha256") == g4_digest:
            return dict(g4_ref), 0, 1
    rewritten_dict: dict[str, Any] = {}
    g2_count = 0
    g4_count = 0
    for key, item in value.items():
        result, item_g2, item_g4 = _rewrite_artifacts(
            item,
            g2_path=g2_path,
            g2_digest=g2_digest,
            g2_ref=g2_ref,
            g4_digest=g4_digest,
            g4_ref=g4_ref,
        )
        rewritten_dict[key] = result
        g2_count += item_g2
        g4_count += item_g4
    return rewritten_dict, g2_count, g4_count


def _build_g2_projection(
    root: Path,
    viewer: Mapping[str, Any],
    config: Mapping[str, Any],
) -> tuple[PlannedFile, dict[str, str], dict[str, str]]:
    canonical_path = _relative_path(config.get("canonical_path"), "G2 canonical path")
    canonical_file_sha = config.get("canonical_file_sha256")
    canonical_artifact_sha = config.get("canonical_artifact_sha256")
    legacy_path = _relative_path(config.get("legacy_public_path"), "legacy G2 path")
    legacy_sha = config.get("legacy_public_sha256")
    if any(
        not isinstance(item, str) or _SHA256.fullmatch(item) is None
        for item in (canonical_file_sha, canonical_artifact_sha, legacy_sha)
    ):
        raise PublicReleaseError("G2 compact-viewer hashes are malformed")
    source_data = _read_regular(root, canonical_path, "canonical G2 component lab")
    if _sha256_bytes(source_data) != canonical_file_sha:
        raise PublicReleaseError("canonical G2 component lab file hash changed")
    source = _load_json_bytes(source_data, canonical_path)
    if not isinstance(source, dict):
        raise PublicReleaseError("canonical G2 component lab must be a JSON object")
    if source.get("artifact_sha256") != canonical_artifact_sha:
        raise PublicReleaseError("canonical G2 semantic artifact hash changed")
    if _semantic_artifact_sha256(source) != canonical_artifact_sha:
        raise PublicReleaseError("canonical G2 semantic artifact commitment is invalid")
    pointers = _collect_artifact_pointers(
        viewer, artifact_path=legacy_path, artifact_sha256=legacy_sha
    )
    additional = config.get("additional_canonical_pointers")
    if not isinstance(additional, list) or any(
        not isinstance(pointer, str) or not _valid_pointer(pointer)
        for pointer in additional
    ):
        raise PublicReleaseError("G2 additional canonical pointers are malformed")
    pointers.update(additional)
    if not pointers:
        raise PublicReleaseError(
            "viewer has no pointer into the canonical G2 component lab"
        )
    projection: dict[str, Any] = {}
    pointer_map: dict[str, str] = {}
    for pointer in sorted(
        pointers, key=lambda item: (len(_pointer_tokens(item)), item)
    ):
        value = _resolve_pointer(source, pointer, "canonical G2 component lab")
        _set_pointer(projection, pointer, _public_projection_value(value))
        pointer_map[pointer] = pointer
    projection.update(
        {
            "schema_version": G2_PROJECTION_SCHEMA,
            "source": {
                "artifact_sha256": canonical_artifact_sha,
                "file_sha256": canonical_file_sha,
                "path": canonical_path,
            },
            "pointer_map": pointer_map,
        }
    )
    projection["projection_commitment_sha256"] = _sha256_bytes(
        _canonical_json(projection)
    )
    data = _json_file_bytes(projection, compact=True)
    digest = _sha256_bytes(data)
    name = "g2-component-lab.portfolio.v1.json"
    public_path = f"viewer/public/artifacts/{digest}/{name}"
    ref = _artifact_ref(
        public_path, digest, name, "G2 compact public component projection"
    )
    lineage = {
        "schema_version": "contextlab.projection-lineage.v1",
        "kind": "json-pointer-projection",
        "canonical_path": canonical_path,
        "canonical_file_sha256": canonical_file_sha,
        "canonical_artifact_sha256": canonical_artifact_sha,
        "pointer_map": pointer_map,
        "transformations": ["private-local-paths-to-repository-relative-references"],
        "projection_commitment_sha256": projection["projection_commitment_sha256"],
    }
    return (
        PlannedFile(
            path=public_path,
            data=data,
            source_path=canonical_path,
            source_sha256=canonical_file_sha,
            projection_lineage=lineage,
        ),
        ref,
        {"path": legacy_path, "sha256": legacy_sha},
    )


def _build_g4_projection(
    root: Path,
    config: Mapping[str, Any],
    legacy_g2: Mapping[str, str],
    exclusions: Sequence[Mapping[str, str]],
) -> tuple[PlannedFile, dict[str, str], str]:
    canonical_path = _relative_path(config.get("canonical_path"), "G4 manifest path")
    expected_sha = config.get("canonical_file_sha256")
    if not isinstance(expected_sha, str) or _SHA256.fullmatch(expected_sha) is None:
        raise PublicReleaseError("G4 manifest hash is malformed")
    source_data = _read_regular(root, canonical_path, "canonical G4 viewer manifest")
    if _sha256_bytes(source_data) != expected_sha:
        raise PublicReleaseError("canonical G4 viewer manifest file hash changed")
    source = _load_json_bytes(source_data, canonical_path)
    if not isinstance(source, dict):
        raise PublicReleaseError("canonical G4 viewer manifest must be a JSON object")
    source_artifact_sha = source.get("artifact_sha256")
    if (
        not isinstance(source_artifact_sha, str)
        or _SHA256.fullmatch(source_artifact_sha) is None
    ):
        raise PublicReleaseError("canonical G4 viewer manifest has no semantic hash")
    if _semantic_artifact_sha256(source) != source_artifact_sha:
        raise PublicReleaseError(
            "canonical G4 viewer manifest semantic hash is invalid"
        )
    public_artifacts = source.get("public_artifacts")
    if not isinstance(public_artifacts, list):
        raise PublicReleaseError("canonical G4 viewer manifest has no public inventory")
    filtered: list[Any] = []
    excluded = 0
    exclusion_counts = {entry["path"]: 0 for entry in exclusions}
    for entry in public_artifacts:
        serialized = _canonical_json(entry)
        if (
            legacy_g2["path"].encode("utf-8") in serialized
            or legacy_g2["sha256"].encode("ascii") in serialized
        ):
            excluded += 1
            continue
        matched_exclusion = next(
            (
                item
                for item in exclusions
                if isinstance(entry, dict)
                and entry.get("publicPath") == item["path"]
                and entry.get("sourceSha256") == item["sha256"]
            ),
            None,
        )
        if matched_exclusion is not None:
            exclusion_counts[matched_exclusion["path"]] += 1
            continue
        filtered.append(entry)
    if excluded != 1:
        raise PublicReleaseError(
            f"expected one defective G2 entry in the G4 inventory, found {excluded}"
        )
    if any(count != 1 for count in exclusion_counts.values()):
        raise PublicReleaseError(
            f"G4 inventory artifact exclusion count differs: {exclusion_counts}"
        )
    projection = copy.deepcopy(source)
    projection.pop("artifact_sha256", None)
    projection["public_artifacts"] = filtered
    projection["portfolio_projection_schema"] = G4_PROJECTION_SCHEMA
    projection["portfolio_projection_source"] = {
        "artifact_sha256": source_artifact_sha,
        "file_sha256": expected_sha,
        "path": canonical_path,
    }
    projection["excluded_defective_artifact_count"] = excluded
    projection["excluded_public_artifacts"] = [
        {
            "path": entry["path"],
            "reason": entry["reason"],
            "sha256": entry["sha256"],
        }
        for entry in exclusions
    ]
    projection["projection_commitment_sha256"] = _sha256_bytes(
        _canonical_json(projection)
    )
    data = _json_file_bytes(projection, compact=True)
    digest = _sha256_bytes(data)
    name = "g4-export-manifest.portfolio.v1.json"
    public_path = f"viewer/public/artifacts/{digest}/{name}"
    ref = _artifact_ref(
        public_path, digest, name, "G4 compact portfolio evidence manifest"
    )
    lineage = {
        "schema_version": "contextlab.projection-lineage.v1",
        "kind": "inventory-pruning",
        "canonical_path": canonical_path,
        "canonical_file_sha256": expected_sha,
        "canonical_artifact_sha256": source_artifact_sha,
        "excluded_defective_artifact_count": excluded,
        "excluded_public_artifacts": [dict(entry) for entry in exclusions],
        "projection_commitment_sha256": projection["projection_commitment_sha256"],
    }
    return (
        PlannedFile(
            path=public_path,
            data=data,
            source_path=canonical_path,
            source_sha256=expected_sha,
            projection_lineage=lineage,
        ),
        ref,
        expected_sha,
    )


def _artifact_refs(value: Any) -> list[Mapping[str, Any]]:
    refs: list[Mapping[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            refs.extend(_artifact_refs(item))
        return refs
    if not isinstance(value, dict):
        return refs
    if _is_artifact_ref(value):
        refs.append(value)
        return refs
    for item in value.values():
        refs.extend(_artifact_refs(item))
    return refs


def _static_path(artifact: Mapping[str, Any]) -> str:
    digest = artifact.get("sha256")
    static_url = artifact.get("staticUrl")
    path = artifact.get("path")
    if (
        not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
        or not isinstance(static_url, str)
        or not static_url.startswith(f"./artifacts/{digest}/")
        or "?" in static_url
        or "#" in static_url
        or not isinstance(path, str)
        or path.startswith("/")
        or ".." in PurePosixPath(path).parts
    ):
        raise PublicReleaseError("viewer contains an invalid artifact reference")
    return _relative_path(
        f"viewer/public/{static_url.removeprefix('./')}", "viewer static artifact"
    )


def _equal_metric(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return (
            math.isfinite(float(left)) and math.isfinite(float(right)) and left == right
        )
    return left == right


def _verify_viewer_pointers(
    viewer: Mapping[str, Any], planned: Mapping[str, PlannedFile]
) -> None:
    cache: dict[str, Any] = {}
    for artifact in _artifact_refs(viewer):
        static_path = _static_path(artifact)
        record = planned.get(static_path)
        if record is None:
            raise PublicReleaseError(
                f"viewer artifact is missing from bundle: {static_path}"
            )
        if record.sha256 != artifact.get("sha256"):
            raise PublicReleaseError(f"viewer artifact hash differs: {static_path}")

    def resolve_binding(binding: Mapping[str, Any], label: str) -> Any:
        artifact = binding.get("artifact")
        pointer = binding.get("jsonPointer")
        if not isinstance(artifact, dict) or not isinstance(pointer, str):
            raise PublicReleaseError(f"{label} has an incomplete artifact binding")
        static_path = _static_path(artifact)
        if static_path not in cache:
            record = planned.get(static_path)
            if record is None:
                raise PublicReleaseError(f"{label} artifact is not in the bundle")
            cache[static_path] = _load_json_bytes(record.data, static_path)
        return _resolve_pointer(cache[static_path], pointer, label)

    def walk(value: Any, path: str = "$") -> None:
        if isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")
            return
        if not isinstance(value, dict):
            return
        if set(value) == {"value", "unit", "display", "provenance"}:
            provenance = value.get("provenance")
            if not isinstance(provenance, dict):
                raise PublicReleaseError(f"viewer metric has no provenance: {path}")
            resolved = resolve_binding(provenance, f"viewer metric {path}")
            # Some existing pipeline labels derive an ordinal from a bound
            # evidence row. The pointer must resolve, but only scalar source
            # values can be required to equal the displayed metric verbatim.
            if not isinstance(resolved, (dict, list)) and not _equal_metric(
                value.get("value"), resolved
            ):
                raise PublicReleaseError(
                    f"viewer metric value differs from its evidence: {path}"
                )
            return
        if set(value) == {
            "id",
            "sourceId",
            "sectionId",
            "label",
            "excerpt",
            "source",
            "target",
            "provenance",
        }:
            provenance = value.get("provenance")
            if not isinstance(provenance, dict):
                raise PublicReleaseError(f"viewer citation has no provenance: {path}")
            resolve_binding(provenance, f"viewer citation {path}")
            return
        for key, item in value.items():
            walk(item, f"{path}.{key}")

    walk(viewer)


def _load_story_registry(
    root: Path, config: Mapping[str, Any]
) -> tuple[str, dict[str, Any]] | None:
    entry = config.get("story_metric_registry")
    if not isinstance(entry, dict):
        raise PublicReleaseError("story metric registry config is malformed")
    relative = _relative_path(entry.get("path"), "story metric registry")
    required = entry.get("required")
    if not isinstance(required, bool):
        raise PublicReleaseError("story metric registry required flag is malformed")
    if not (root / relative).exists():
        if required:
            raise PublicReleaseError(f"story metric registry is missing: {relative}")
        return None
    data = _read_regular(root, relative, "story metric registry")
    value = _load_json_bytes(data, relative)
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != STORY_EVIDENCE_SCHEMA
    ):
        raise PublicReleaseError("story metric registry schema is unsupported")
    return relative, value


def _verify_story_registry(
    registry: Mapping[str, Any],
    *,
    root: Path | None = None,
    planned: Mapping[str, PlannedFile] | None = None,
    require_public_artifacts: bool = True,
) -> None:
    metrics = registry.get("metrics")
    if set(registry) != {"schema_version", "metrics"} or not isinstance(metrics, list):
        raise PublicReleaseError("story metric registry fields differ")
    identifiers: set[str] = set()
    for index, metric in enumerate(metrics):
        if not isinstance(metric, dict) or set(metric) != {
            "id",
            "json_pointer",
            "public_url",
            "scope",
            "source_artifact_sha256",
            "source_file_sha256",
            "source_path",
            "status",
            "value",
        }:
            raise PublicReleaseError(f"story metric {index} fields differ")
        identifier = metric.get("id")
        relative = _relative_path(metric.get("source_path"), f"story metric {index}")
        file_sha = metric.get("source_file_sha256")
        semantic_sha = metric.get("source_artifact_sha256")
        pointer = metric.get("json_pointer")
        status = metric.get("status")
        scope = metric.get("scope")
        public_url = metric.get("public_url")
        if (
            not isinstance(identifier, str)
            or not identifier
            or identifier in identifiers
            or not isinstance(status, str)
            or not status
            or not isinstance(scope, str)
            or not scope
            or (public_url is not None and not isinstance(public_url, str))
            or not isinstance(file_sha, str)
            or _SHA256.fullmatch(file_sha) is None
            or (
                semantic_sha is not None
                and (
                    not isinstance(semantic_sha, str)
                    or _SHA256.fullmatch(semantic_sha) is None
                )
            )
            or not isinstance(pointer, str)
            or not _valid_pointer(pointer)
        ):
            raise PublicReleaseError(f"story metric {index} identity is invalid")
        identifiers.add(identifier)
        if planned is not None:
            record = planned.get(relative)
            if record is None:
                raise PublicReleaseError(
                    f"story metric artifact is not in bundle: {relative}"
                )
            data = record.data
        elif root is not None:
            data = _read_regular(root, relative, f"story metric artifact {identifier}")
        else:
            raise PublicReleaseError("story metric verification has no artifact source")
        if _sha256_bytes(data) != file_sha:
            raise PublicReleaseError(
                f"story metric artifact hash differs: {identifier}"
            )
        if public_url is not None:
            if not public_url.startswith(f"./artifacts/{file_sha}/"):
                raise PublicReleaseError(
                    f"story metric public URL is invalid: {identifier}"
                )
            public_path = _relative_path(
                f"viewer/public/{public_url.removeprefix('./')}",
                f"story metric public URL {identifier}",
            )
            if planned is not None:
                public_record = planned.get(public_path)
                if public_record is None or public_record.sha256 != file_sha:
                    raise PublicReleaseError(
                        f"story metric public artifact differs: {identifier}"
                    )
            elif root is not None:
                if (root / public_path).exists():
                    public_data = _read_regular(
                        root,
                        public_path,
                        f"story metric public artifact {identifier}",
                    )
                    if _sha256_bytes(public_data) != file_sha:
                        raise PublicReleaseError(
                            f"story metric public artifact differs: {identifier}"
                        )
                elif require_public_artifacts:
                    raise PublicReleaseError(
                        f"story metric public artifact {identifier} is missing: "
                        f"{public_path}"
                    )
        document = _load_json_bytes(data, relative)
        if semantic_sha is not None:
            if not isinstance(document, dict):
                raise PublicReleaseError(
                    f"story metric semantic artifact is not an object: {identifier}"
                )
            actual_semantic = document.get("artifact_sha256")
            if actual_semantic is not None:
                if _semantic_artifact_sha256(document) != actual_semantic:
                    raise PublicReleaseError(
                        f"story metric artifact commitment differs: {identifier}"
                    )
            elif document.get("projection_commitment_sha256") is not None:
                actual_semantic = document.get("projection_commitment_sha256")
                projection_body = {
                    key: item
                    for key, item in document.items()
                    if key != "projection_commitment_sha256"
                }
                if _sha256_bytes(_canonical_json(projection_body)) != actual_semantic:
                    raise PublicReleaseError(
                        f"story metric projection commitment differs: {identifier}"
                    )
            if actual_semantic != semantic_sha:
                raise PublicReleaseError(
                    f"story metric semantic hash differs: {identifier}"
                )
        resolved = _resolve_pointer(document, pointer, f"story metric {identifier}")
        if not _equal_metric(metric.get("value"), resolved):
            raise PublicReleaseError(
                f"story metric value differs from its artifact: {identifier}"
            )


def _project_story_registry(
    registry: Mapping[str, Any],
    *,
    root: Path,
    g2_source_path: str,
    g2_source_file_sha: str,
    g2_source_artifact_sha: str,
    g2_projection: PlannedFile,
    viewer_source_path: str,
    viewer_source_sha: str,
    viewer_projection: PlannedFile,
) -> dict[str, Any]:
    _verify_story_registry(registry, root=root, require_public_artifacts=False)
    projected = copy.deepcopy(registry)
    for metric in projected["metrics"]:
        if metric["source_path"] == g2_source_path:
            if (
                metric["source_file_sha256"] != g2_source_file_sha
                or metric["source_artifact_sha256"] != g2_source_artifact_sha
            ):
                raise PublicReleaseError(
                    f"G2 story metric has stale lineage: {metric['id']}"
                )
            projection = _load_json_bytes(g2_projection.data, g2_projection.path)
            pointer = metric["json_pointer"]
            mapped = projection.get("pointer_map", {}).get(pointer)
            if not isinstance(mapped, str):
                raise PublicReleaseError(
                    f"G2 story metric is not in the compact projection: {metric['id']}"
                )
            metric["source_path"] = g2_projection.path
            metric["source_file_sha256"] = g2_projection.sha256
            metric["source_artifact_sha256"] = projection[
                "projection_commitment_sha256"
            ]
            metric["json_pointer"] = mapped
            metric["public_url"] = "./" + g2_projection.path.removeprefix(
                "viewer/public/"
            )
        elif metric["source_path"] == viewer_source_path:
            if metric["source_file_sha256"] != viewer_source_sha:
                raise PublicReleaseError(
                    f"viewer Story metric has stale lineage: {metric['id']}"
                )
            metric["source_file_sha256"] = viewer_projection.sha256
    return projected


def _add_story_public_artifacts(
    registry: Mapping[str, Any], root: Path, planned: dict[str, PlannedFile]
) -> None:
    metrics = registry.get("metrics")
    if not isinstance(metrics, list):
        raise PublicReleaseError("story metric registry has no metrics")
    for index, metric in enumerate(metrics):
        if not isinstance(metric, dict) or metric.get("public_url") is None:
            continue
        source_path = _relative_path(
            metric.get("source_path"), f"story metric {index} source"
        )
        source_sha = metric.get("source_file_sha256")
        public_url = metric.get("public_url")
        if (
            not isinstance(source_sha, str)
            or _SHA256.fullmatch(source_sha) is None
            or not isinstance(public_url, str)
            or not public_url.startswith(f"./artifacts/{source_sha}/")
        ):
            raise PublicReleaseError(
                f"story metric {index} public artifact binding is invalid"
            )
        public_path = _relative_path(
            f"viewer/public/{public_url.removeprefix('./')}",
            f"story metric {index} public artifact",
        )
        data = _read_regular(root, source_path, f"story metric {index} source")
        if _sha256_bytes(data) != source_sha:
            raise PublicReleaseError(
                f"story metric {index} source hash differs: {source_path}"
            )
        existing = planned.get(public_path)
        if existing is not None:
            if existing.sha256 != source_sha:
                raise PublicReleaseError(
                    f"story metric {index} public artifact differs: {public_path}"
                )
            continue
        _add_plan(
            planned,
            PlannedFile(
                path=public_path,
                data=data,
                source_path=source_path,
                source_sha256=source_sha,
                projection_lineage={
                    "schema_version": "contextlab.projection-lineage.v1",
                    "kind": "story-evidence-public-copy",
                    "copy_sha256": source_sha,
                },
            ),
        )


def _add_artifact_markdown_link_copies(
    viewer: Mapping[str, Any], planned: dict[str, PlannedFile]
) -> None:
    refs_by_source_path: dict[str, Mapping[str, Any]] = {}
    refs_by_static_path: dict[str, Mapping[str, Any]] = {}
    for artifact in _artifact_refs(viewer):
        source_path = _relative_path(
            artifact.get("path"), "viewer artifact source path"
        )
        static_path = _static_path(artifact)
        existing = refs_by_source_path.get(source_path)
        if existing is not None and existing.get("sha256") != artifact.get("sha256"):
            raise PublicReleaseError(
                f"viewer source path has conflicting artifacts: {source_path}"
            )
        refs_by_source_path[source_path] = artifact
        refs_by_static_path[static_path] = artifact

    source_markdown = [
        path
        for path in sorted(planned)
        if path.startswith("viewer/public/artifacts/")
        and path.casefold().endswith(".md")
    ]
    for markdown_path in source_markdown:
        record = planned[markdown_path]
        try:
            text = record.data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PublicReleaseError(
                f"viewer Markdown artifact is not UTF-8: {markdown_path}"
            ) from exc
        source_ref = refs_by_static_path.get(markdown_path)
        for raw_target in _markdown_targets(text):
            local = _markdown_local_target(markdown_path, raw_target)
            if local is None:
                continue
            destination, _fragment, decoded_path = local
            if destination in planned or not decoded_path:
                continue
            possible_sources: list[str] = []
            direct = posixpath.normpath(decoded_path.lstrip("/"))
            if direct != ".." and not direct.startswith("../"):
                possible_sources.append(direct)
            if source_ref is not None:
                original_path = source_ref.get("path")
                if isinstance(original_path, str):
                    relative_source = posixpath.normpath(
                        (
                            PurePosixPath(original_path).parent
                            / PurePosixPath(decoded_path)
                        ).as_posix()
                    )
                    if relative_source != ".." and not relative_source.startswith(
                        "../"
                    ):
                        possible_sources.append(relative_source)
            target_ref = next(
                (
                    refs_by_source_path[candidate]
                    for candidate in possible_sources
                    if candidate in refs_by_source_path
                ),
                None,
            )
            if target_ref is None:
                continue
            target_static_path = _static_path(target_ref)
            target_record = planned.get(target_static_path)
            if target_record is None:
                raise PublicReleaseError(
                    f"viewer Markdown link target is not planned: {target_static_path}"
                )
            _add_plan(
                planned,
                PlannedFile(
                    path=destination,
                    data=target_record.data,
                    source_path=target_record.source_path,
                    source_sha256=target_record.source_sha256,
                    projection_lineage={
                        "schema_version": "contextlab.projection-lineage.v1",
                        "kind": "artifact-link-support-copy",
                        "linked_from": markdown_path,
                        "canonical_public_artifact_path": target_static_path,
                        "copy_sha256": target_record.sha256,
                    },
                ),
            )


def _build_compact_viewer(
    root: Path, config: Mapping[str, Any]
) -> tuple[dict[str, PlannedFile], dict[str, Any], PlannedFile]:
    compact = config.get("compact_viewer")
    if not isinstance(compact, dict):
        raise PublicReleaseError("compact-viewer config is malformed")
    source_path = _relative_path(
        compact.get("source_export_path"), "viewer export path"
    )
    expected_sha = compact.get("source_export_sha256")
    if not isinstance(expected_sha, str) or _SHA256.fullmatch(expected_sha) is None:
        raise PublicReleaseError("viewer export hash is malformed")
    source_data = _read_regular(root, source_path, "approved viewer export")
    if _sha256_bytes(source_data) != expected_sha:
        raise PublicReleaseError("approved viewer export file hash changed")
    viewer = _load_json_bytes(source_data, source_path)
    if not isinstance(viewer, dict):
        raise PublicReleaseError("approved viewer export must be a JSON object")
    g2_config = compact.get("g2")
    g4_config = compact.get("g4_manifest")
    if not isinstance(g2_config, dict) or not isinstance(g4_config, dict):
        raise PublicReleaseError("compact-viewer evidence config is malformed")
    exclusions = _artifact_exclusions(compact)
    g2_file, g2_ref, legacy = _build_g2_projection(root, viewer, g2_config)
    g4_file, g4_ref, g4_legacy_sha = _build_g4_projection(
        root, g4_config, legacy, exclusions
    )
    rewritten, g2_count, g4_count = _rewrite_artifacts(
        viewer,
        g2_path=legacy["path"],
        g2_digest=legacy["sha256"],
        g2_ref=g2_ref,
        g4_digest=g4_legacy_sha,
        g4_ref=g4_ref,
    )
    if not isinstance(rewritten, dict) or g2_count == 0 or g4_count == 0:
        raise PublicReleaseError(
            "viewer compact rewrite did not replace all required artifacts"
        )
    rewritten, exclusion_counts = _prune_artifact_refs(rewritten, exclusions)
    if not isinstance(rewritten, dict) or any(
        count != 1 for count in exclusion_counts.values()
    ):
        raise PublicReleaseError(
            f"viewer artifact exclusion count differs: {exclusion_counts}"
        )
    rewritten_bytes = _json_file_bytes(rewritten)
    if legacy["path"].encode("utf-8") in rewritten_bytes:
        raise PublicReleaseError(
            "compact viewer still links the defective G2 projection"
        )
    viewer_file = PlannedFile(
        path="viewer/public/contextlab-viewer.v1.json",
        data=rewritten_bytes,
        source_path=source_path,
        source_sha256=expected_sha,
        projection_lineage={
            "schema_version": "contextlab.projection-lineage.v1",
            "kind": "artifact-reference-rewrite",
            "canonical_source_sha256": expected_sha,
            "g2_replacement_count": g2_count,
            "g4_replacement_count": g4_count,
            "excluded_artifact_counts": exclusion_counts,
            "projection_sha256": _sha256_bytes(rewritten_bytes),
        },
    )
    planned: dict[str, PlannedFile] = {
        g2_file.path: g2_file,
        g4_file.path: g4_file,
        viewer_file.path: viewer_file,
    }
    for artifact in _artifact_refs(rewritten):
        static_path = _static_path(artifact)
        if static_path in planned:
            if planned[static_path].sha256 != artifact.get("sha256"):
                raise PublicReleaseError(
                    f"generated viewer artifact hash differs: {static_path}"
                )
            continue
        data = _read_regular(root, static_path, "approved viewer public artifact")
        digest = _sha256_bytes(data)
        if digest != artifact.get("sha256"):
            raise PublicReleaseError(
                f"approved viewer public artifact hash differs: {static_path}"
            )
        planned[static_path] = PlannedFile(
            path=static_path,
            data=data,
            source_path=static_path,
            source_sha256=digest,
        )
    _add_artifact_markdown_link_copies(rewritten, planned)
    _verify_viewer_pointers(rewritten, planned)
    return planned, rewritten, g2_file


def _add_plan(planned: dict[str, PlannedFile], record: PlannedFile) -> None:
    if record.path in planned:
        existing = planned[record.path]
        if existing != record:
            raise PublicReleaseError(f"two public inputs collide: {record.path}")
        return
    planned[record.path] = record


def _manifest(planned: Mapping[str, PlannedFile], config: Mapping[str, Any]) -> bytes:
    limits = config.get("limits")
    release_id = config.get("release_id")
    if (
        not isinstance(limits, dict)
        or not isinstance(release_id, str)
        or not release_id
    ):
        raise PublicReleaseError("public release limits or release ID are malformed")
    max_file = limits.get("max_file_bytes_exclusive")
    max_total = limits.get("max_total_bytes_exclusive")
    if not isinstance(max_file, int) or not isinstance(max_total, int):
        raise PublicReleaseError("public release size limits are malformed")
    files: list[dict[str, Any]] = []
    payload_bytes = 0
    for relative in sorted(planned):
        record = planned[relative]
        size = len(record.data)
        if size >= max_file:
            raise PublicReleaseError(
                f"public file is {size} bytes; limit is less than {max_file}: {relative}"
            )
        _scan_file(relative, record.data)
        payload_bytes += size
        files.append(
            {
                "byte_size": size,
                "path": relative,
                "projection_lineage": (
                    dict(record.projection_lineage)
                    if record.projection_lineage is not None
                    else None
                ),
                "sha256": record.sha256,
                "source_path": record.source_path,
                "source_sha256": record.source_sha256,
            }
        )
    body: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA,
        "release_id": release_id,
        "policy": {
            "max_file_bytes_exclusive": max_file,
            "max_total_bytes_exclusive": max_total,
        },
        "file_count": len(files),
        "payload_bytes": payload_bytes,
        "files": files,
    }
    body["manifest_commitment_sha256"] = _sha256_bytes(_canonical_json(body))
    data = _json_file_bytes(body)
    if payload_bytes + len(data) >= max_total:
        raise PublicReleaseError(
            f"public bundle is {payload_bytes + len(data)} bytes; limit is less than {max_total}"
        )
    return data


def build_release_plan(
    repository_root: Path, config_path: str = "public-release.json"
) -> tuple[dict[str, PlannedFile], bytes]:
    root = _safe_root(repository_root, "repository root")
    config = _load_config(root, config_path)
    allowlist = _allowlisted_paths(root, config)
    planned: dict[str, PlannedFile] = {}
    for relative, expected_sha in sorted(allowlist.items()):
        data = _read_regular(root, relative, "allowlisted public file")
        digest = _sha256_bytes(data)
        if expected_sha is not None and digest != expected_sha:
            raise PublicReleaseError(f"allowlisted file hash changed: {relative}")
        _add_plan(
            planned,
            PlannedFile(
                path=relative,
                data=data,
                source_path=relative,
                source_sha256=digest,
            ),
        )
    viewer_files, _viewer, g2_projection = _build_compact_viewer(root, config)
    for record in viewer_files.values():
        _add_plan(planned, record)
    registry_result = _load_story_registry(root, config)
    if registry_result is not None:
        registry_path, registry = registry_result
        compact_config = config["compact_viewer"]
        g2_config = config["compact_viewer"]["g2"]
        viewer_projection = viewer_files["viewer/public/contextlab-viewer.v1.json"]
        projected = _project_story_registry(
            registry,
            root=root,
            g2_source_path=g2_config["canonical_path"],
            g2_source_file_sha=g2_config["canonical_file_sha256"],
            g2_source_artifact_sha=g2_config["canonical_artifact_sha256"],
            g2_projection=g2_projection,
            viewer_source_path=compact_config["source_export_path"],
            viewer_source_sha=compact_config["source_export_sha256"],
            viewer_projection=viewer_projection,
        )
        _add_story_public_artifacts(registry, root, planned)
        projected_data = _json_file_bytes(projected)
        original_data = _read_regular(root, registry_path, "story metric registry")
        planned[registry_path] = PlannedFile(
            path=registry_path,
            data=projected_data,
            source_path=registry_path,
            source_sha256=_sha256_bytes(original_data),
            projection_lineage={
                "schema_version": "contextlab.projection-lineage.v1",
                "kind": "story-metric-artifact-rewrite",
                "projection_sha256": _sha256_bytes(projected_data),
            },
        )
        _verify_story_registry(projected, planned=planned)
    _verify_markdown_links(planned)
    manifest = _manifest(planned, config)
    return planned, manifest


def _write_release(
    output: Path, planned: Mapping[str, PlannedFile], manifest: bytes
) -> None:
    output = Path(os.path.abspath(output))
    if output.exists():
        raise PublicReleaseError(f"public release destination already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".contextlab-public-", dir=output.parent))
    try:
        for relative in sorted(planned):
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as handle:
                handle.write(planned[relative].data)
        with (stage / MANIFEST_NAME).open("xb") as handle:
            handle.write(manifest)
        os.replace(stage, output)
    except Exception:
        if (
            stage.exists()
            and stage.parent == output.parent
            and stage.name.startswith(".contextlab-public-")
        ):
            shutil.rmtree(stage)
        raise


def export_release(
    repository_root: Path, output: Path, config_path: str = "public-release.json"
) -> dict[str, Any]:
    root = _safe_root(repository_root, "repository root")
    output_absolute = Path(os.path.abspath(output))
    if output_absolute == root:
        raise PublicReleaseError(
            "public release destination must differ from the vault"
        )
    planned, manifest = build_release_plan(root, config_path)
    _write_release(output_absolute, planned, manifest)
    summary = verify_release(output_absolute)
    summary["output"] = str(output_absolute)
    return summary


def _bundle_files(root: Path) -> set[str]:
    files: set[str] = set()

    def visit(directory: Path) -> None:
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            if child == root / ".git":
                continue
            metadata = child.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise PublicReleaseError(f"public bundle contains a symlink: {child}")
            if stat.S_ISDIR(metadata.st_mode):
                visit(child)
            elif stat.S_ISREG(metadata.st_mode):
                files.add(child.relative_to(root).as_posix())
            else:
                raise PublicReleaseError(
                    f"public bundle contains an unsafe entry: {child}"
                )

    visit(root)
    return files


def _verify_projection(record: PlannedFile) -> None:
    if Path(record.path).suffix.casefold() != ".json":
        return
    value = _load_json_bytes(record.data, record.path)
    if not isinstance(value, dict):
        return
    schema = value.get("schema_version")
    g4_schema = value.get("portfolio_projection_schema")
    if schema != G2_PROJECTION_SCHEMA and g4_schema != G4_PROJECTION_SCHEMA:
        return
    commitment = value.get("projection_commitment_sha256")
    if not isinstance(commitment, str) or _SHA256.fullmatch(commitment) is None:
        raise PublicReleaseError(f"projection commitment is missing: {record.path}")
    body = {
        key: item
        for key, item in value.items()
        if key != "projection_commitment_sha256"
    }
    if _sha256_bytes(_canonical_json(body)) != commitment:
        raise PublicReleaseError(f"projection commitment differs: {record.path}")
    if schema == G2_PROJECTION_SCHEMA:
        pointer_map = value.get("pointer_map")
        if not isinstance(pointer_map, dict) or not pointer_map:
            raise PublicReleaseError("G2 projection pointer map is missing")
        for canonical, projected in pointer_map.items():
            if not isinstance(canonical, str) or not isinstance(projected, str):
                raise PublicReleaseError("G2 projection pointer map is malformed")
            _resolve_pointer(value, projected, "G2 public projection")


def verify_release(bundle_root: Path) -> dict[str, Any]:
    root = _safe_root(bundle_root, "public bundle root")
    manifest_data = _read_regular(root, MANIFEST_NAME, "public release manifest")
    manifest = _load_json_bytes(manifest_data, MANIFEST_NAME)
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != MANIFEST_SCHEMA
    ):
        raise PublicReleaseError("public release manifest schema is unsupported")
    required = {
        "file_count",
        "files",
        "manifest_commitment_sha256",
        "payload_bytes",
        "policy",
        "release_id",
        "schema_version",
    }
    if set(manifest) != required:
        raise PublicReleaseError("public release manifest fields differ")
    commitment = manifest.get("manifest_commitment_sha256")
    if not isinstance(commitment, str) or _SHA256.fullmatch(commitment) is None:
        raise PublicReleaseError("public release manifest commitment is malformed")
    body = {
        key: item
        for key, item in manifest.items()
        if key != "manifest_commitment_sha256"
    }
    if _sha256_bytes(_canonical_json(body)) != commitment:
        raise PublicReleaseError("public release manifest commitment differs")
    policy = manifest.get("policy")
    files = manifest.get("files")
    if not isinstance(policy, dict) or not isinstance(files, list):
        raise PublicReleaseError(
            "public release manifest policy or files are malformed"
        )
    max_file = policy.get("max_file_bytes_exclusive")
    max_total = policy.get("max_total_bytes_exclusive")
    if not isinstance(max_file, int) or not isinstance(max_total, int):
        raise PublicReleaseError("public release manifest size policy is malformed")
    paths = [entry.get("path") for entry in files if isinstance(entry, dict)]
    if (
        len(paths) != len(files)
        or paths != sorted(paths)
        or len(paths) != len(set(paths))
    ):
        raise PublicReleaseError(
            "public release manifest paths are not unique and sorted"
        )
    if manifest.get("file_count") != len(files):
        raise PublicReleaseError("public release manifest file count differs")
    actual_paths = _bundle_files(root)
    expected_paths = set(paths) | {MANIFEST_NAME}
    if actual_paths != expected_paths:
        missing = sorted(expected_paths - actual_paths)
        extra = sorted(actual_paths - expected_paths)
        raise PublicReleaseError(
            f"public bundle file set differs; missing={missing}, extra={extra}"
        )
    planned: dict[str, PlannedFile] = {}
    payload_bytes = 0
    largest_path = ""
    largest_bytes = -1
    for index, entry in enumerate(files):
        if not isinstance(entry, dict) or set(entry) != {
            "byte_size",
            "path",
            "projection_lineage",
            "sha256",
            "source_path",
            "source_sha256",
        }:
            raise PublicReleaseError(
                f"public release manifest file {index} fields differ"
            )
        relative = _relative_path(entry.get("path"), f"manifest file {index}")
        source_path = _relative_path(
            entry.get("source_path"), f"manifest source {index}"
        )
        digest = entry.get("sha256")
        source_digest = entry.get("source_sha256")
        size = entry.get("byte_size")
        lineage = entry.get("projection_lineage")
        if (
            not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or not isinstance(source_digest, str)
            or _SHA256.fullmatch(source_digest) is None
            or not isinstance(size, int)
            or size < 0
            or size >= max_file
            or (lineage is not None and not isinstance(lineage, dict))
        ):
            raise PublicReleaseError(
                f"public release manifest file {relative} is malformed"
            )
        data = _read_regular(
            root, relative, "manifest-listed public file", maximum=max_file
        )
        if len(data) != size or _sha256_bytes(data) != digest:
            raise PublicReleaseError(f"public file differs from manifest: {relative}")
        _scan_file(relative, data)
        record = PlannedFile(
            path=relative,
            data=data,
            source_path=source_path,
            source_sha256=source_digest,
            projection_lineage=lineage,
        )
        _verify_projection(record)
        planned[relative] = record
        payload_bytes += size
        if size > largest_bytes:
            largest_path = relative
            largest_bytes = size
    if payload_bytes != manifest.get("payload_bytes"):
        raise PublicReleaseError("public release manifest byte total differs")
    total_bytes = payload_bytes + len(manifest_data)
    if total_bytes >= max_total:
        raise PublicReleaseError("public bundle exceeds its total size policy")
    viewer_path = "viewer/public/contextlab-viewer.v1.json"
    if viewer_path in planned:
        viewer = _load_json_bytes(planned[viewer_path].data, viewer_path)
        if not isinstance(viewer, dict):
            raise PublicReleaseError("public viewer export is not a JSON object")
        _verify_viewer_pointers(viewer, planned)
    config = _load_config(root, "public-release.json")
    registry_result = _load_story_registry(root, config)
    if registry_result is not None:
        _, registry = registry_result
        _verify_story_registry(registry, planned=planned)
    _verify_markdown_links(planned)
    return {
        "status": "passed",
        "file_count": len(files) + 1,
        "payload_file_count": len(files),
        "total_bytes": total_bytes,
        "largest_file": largest_path,
        "largest_file_bytes": largest_bytes,
        "manifest_sha256": _sha256_bytes(manifest_data),
        "manifest_commitment_sha256": commitment,
        "secret_scan_findings": 0,
        "protected_data_scan_findings": 0,
        "private_path_scan_findings": 0,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    export = subparsers.add_parser("export", help="create a new curated public bundle")
    export.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    export.add_argument("--config", default="public-release.json")
    export.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify", help="verify an existing public bundle")
    verify.add_argument("--bundle-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "export":
            result = export_release(args.repository_root, args.output, args.config)
        else:
            result = verify_release(args.bundle_root)
    except PublicReleaseError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
