"""Blind three-member review packets and resumable grade storage."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import stat
import statistics
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable, Mapping

from .baseline import repository_root
from .tasking import FORBIDDEN_PUBLIC_FIELDS


REVIEW_PACKET_SCHEMA = "contextlab.review-packet.v1"
REVIEW_MANIFEST_SCHEMA = "contextlab.review-manifest.v1"
REVIEW_RUBRIC_SHA256 = (
    "61290b20a518200b85b33953875b3af3f40dc3b92a1c8d4dc315f69e23323696"
)


def _load_frozen_review_rubric() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[1] / "review_protocol.json"
    try:
        protocol = json.loads(path.read_text(encoding="utf-8"))
        rubric = protocol["rubric"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
        raise RuntimeError("cannot load the canonical review rubric") from exc
    if not isinstance(rubric, dict):
        raise RuntimeError("canonical review rubric must be an object")
    encoded = json.dumps(
        rubric, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if hashlib.sha256(encoded).hexdigest() != REVIEW_RUBRIC_SHA256:
        raise RuntimeError("canonical review rubric hash differs")
    return rubric


REVIEW_RUBRIC = _load_frozen_review_rubric()
RUBRIC_VERSION = str(REVIEW_RUBRIC["version"])
REVIEWERS = ("gpt-5.6-sol-high", "claude-opus-5-medium", "kevin")
STRATEGY_LANES = (
    "full_context",
    "v1_dense_rag",
    "compiled_wiki",
    "text_to_sql",
    "promoted_v2",
)
REASONING_EFFORTS = ("low", "high")
TASK_COUNT = 160
PACKET_SIZE = 20
MAIN_CELL_COUNT = TASK_COUNT * len(STRATEGY_LANES) * len(REASONING_EFFORTS)
CALIBRATION_CELL_COUNT = 20
HIDDEN_REPEAT_COUNT = 80
MAIN_PACKET_COUNT = MAIN_CELL_COUNT // PACKET_SIZE
CALIBRATION_PACKET_COUNT = CALIBRATION_CELL_COUNT // PACKET_SIZE
REPEAT_PACKET_COUNT = HIDDEN_REPEAT_COUNT // PACKET_SIZE
SEALED_TASK_COUNT = 48
CALIBRATION_REFERENCE_SCHEMA = "contextlab.calibration-reference.v1"
CALIBRATION_GATE_SCHEMA = "contextlab.calibration-gate.v1"
TOKEN_PREFLIGHT_SCHEMA = "contextlab.review-token-preflight.v1"
REVIEW_RELEASE_SCHEMA = "contextlab.review-release-manifest.v1"
CALIBRATION_EXACT_ORDINAL_MIN = 0.65
CALIBRATION_WITHIN_ONE_MIN = 0.90
CALIBRATION_ACCEPTED_MATCH_MIN = 0.85
AI_KEVIN_ACCEPTED_MATCH_MIN = 0.80
AI_KEVIN_MEAN_ABSOLUTE_DIFFERENCE_MAX = 0.75
PACKET_TOKEN_VERIFIER_ID = "contextlab.packet-lexeme-tokenizer.v1"
_PACKET_TOKEN_VERIFIER_SPEC = (
    b"contextlab.packet-lexeme-tokenizer.v1\n"
    b"input=strict-utf8-exact-packet-bytes\n"
    b"pattern=[A-Za-z0-9]+(?:[-_/.][A-Za-z0-9]+)*|[^\\s]\n"
    b"count=non-overlapping-regex-matches\n"
)
PACKET_TOKEN_VERIFIER_SHA256 = hashlib.sha256(_PACKET_TOKEN_VERIFIER_SPEC).hexdigest()
PINNED_REVIEW_TOKEN_PROFILES: dict[str, dict[str, str]] = {
    "gpt-5.6-sol-high": {
        "model_id": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "tokenizer_id": (
            f"{PACKET_TOKEN_VERIFIER_ID}:gpt-5.6-sol:high:"
            f"{PACKET_TOKEN_VERIFIER_SHA256[:16]}"
        ),
    },
    "claude-opus-5-medium": {
        "model_id": "claude-opus-5",
        "reasoning_effort": "medium",
        "tokenizer_id": (
            f"{PACKET_TOKEN_VERIFIER_ID}:claude-opus-5:medium:"
            f"{PACKET_TOKEN_VERIFIER_SHA256[:16]}"
        ),
    },
}
_PACKET_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]+(?:[-_/.][A-Za-z0-9]+)*|[^\s]")

PUBLIC_CELL_FIELDS = frozenset(
    {
        "blind_cell_id",
        "question",
        "candidate_answer",
        "cited_evidence",
        "rubric_version",
    }
)
CITED_EVIDENCE_FIELDS = frozenset({"reference", "text"})
CALIBRATION_GATE_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "review_manifest_sha256",
        "identity_map_sha256",
        "reference_sha256",
        "cell_count_per_reviewer",
        "metrics_vs_reference",
        "ai_vs_kevin",
        "rubric_ambiguity_by_reviewer",
        "restart_all_three_reviews",
    }
)
TOKEN_PREFLIGHT_FIELDS = frozenset(
    {
        "schema_version",
        "reviewer",
        "review_manifest_sha256",
        "tokenizer_id",
        "packet_count",
        "utf8_bytes_total",
        "token_total",
        "packet_token_counts",
        "packet_token_counts_sha256",
        "confirmed_before_review",
        "confirmed_by",
    }
)
_TASK_ID_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:S|T|C)\d{3}(?![A-Za-z0-9])", re.IGNORECASE
)
PRIVATE_CELL_REQUIRED_FIELDS = frozenset(
    {
        "cell_id",
        "task_id",
        "question",
        "candidate_answer",
        "cited_evidence",
        "candidate_sha256",
        "strategy_id",
        "reasoning_effort",
    }
)
PRIVATE_CELL_FIELDS = PRIVATE_CELL_REQUIRED_FIELDS | {"task_family"}
REVIEW_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "rubric_version",
        "reviewers",
        "packet_size",
        "unique_cells_per_reviewer",
        "calibration_cells_per_reviewer",
        "hidden_repeats_per_reviewer",
        "main_packets_per_reviewer",
        "calibration_packets_per_reviewer",
        "total_packets_per_reviewer",
        "seed_sha256",
        "identity_map_sha256",
        "packets",
        "manifest_sha256",
    }
)


class ReviewContractError(ValueError):
    """A review packet, grade, or aggregate violates the blind-review contract."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _pretty_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _digest(seed: bytes, *parts: str) -> str:
    return hmac.new(seed, "\0".join(parts).encode("utf-8"), hashlib.sha256).hexdigest()


def _chunks(values: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    if len(values) % size:
        raise ReviewContractError(
            f"{len(values)} cells do not divide into packets of {size}"
        )
    return [values[index : index + size] for index in range(0, len(values), size)]


def _outside_repository(path: Path, label: str) -> None:
    absolute = Path(os.path.abspath(path))
    repository = Path(os.path.abspath(repository_root()))
    try:
        absolute.relative_to(repository)
    except ValueError:
        return
    raise ReviewContractError(f"{label} must stay outside the repository")


def _packet_path(root: Path, relative_value: Any) -> Path:
    relative = Path(str(relative_value))
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ReviewContractError("review packet path is not a contained relative path")
    return Path(os.path.abspath(root)) / relative


_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _external_absolute(path: Path, label: str) -> Path:
    absolute = Path(os.path.abspath(path))
    for alias in (Path("/var"), Path("/tmp")):
        try:
            relative = absolute.relative_to(alias)
        except ValueError:
            continue
        if alias.is_symlink():
            absolute = alias.resolve() / relative
        break
    _outside_repository(absolute, label)
    if not absolute.is_absolute() or not absolute.name:
        raise ReviewContractError(f"{label} path is invalid")
    return absolute


def _open_absolute_directory(
    path: Path, *, create: bool, label: str
) -> tuple[int, Path]:
    """Open an absolute directory through retained no-follow descriptors."""

    absolute = _external_absolute(path, label)
    anchor = Path(absolute.anchor)
    relative = absolute.relative_to(anchor)
    descriptor = -1
    try:
        descriptor = os.open(anchor, _DIRECTORY_FLAGS)
        for component in relative.parts:
            if component in {"", ".", ".."}:
                raise ReviewContractError(f"{label} path is invalid")
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor, absolute
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise ReviewContractError(f"{label} has an unsafe directory component") from exc
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        raise


def _open_absolute_parent(
    path: Path, *, create: bool, label: str
) -> tuple[int, str, Path]:
    absolute = _external_absolute(path, label)
    descriptor, _ = _open_absolute_directory(
        absolute.parent, create=create, label=f"{label} parent"
    )
    return descriptor, absolute.name, absolute


def _safe_relative_parts(relative_value: Any) -> tuple[str, ...]:
    relative = Path(str(relative_value))
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ReviewContractError("review packet path is not a contained relative path")
    return tuple(relative.parts)


def _open_relative_parent(
    root_descriptor: int,
    relative_value: Any,
    *,
    create: bool,
) -> tuple[int, str]:
    parts = _safe_relative_parts(relative_value)
    descriptor = os.dup(root_descriptor)
    try:
        for component in parts[:-1]:
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor, parts[-1]
    except OSError as exc:
        os.close(descriptor)
        raise ReviewContractError("review packet path has an unsafe parent") from exc


def _read_regular_at(parent: int, name: str, label: str) -> bytes | None:
    descriptor = -1
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=parent)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ReviewContractError(f"{label} target is unsafe")
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
            raise ReviewContractError(f"{label} changed while it was read")
        return b"".join(chunks)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReviewContractError(f"cannot read {label}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_relative_bytes(
    root_descriptor: int, relative_value: Any, label: str
) -> bytes:
    parent, name = _open_relative_parent(root_descriptor, relative_value, create=False)
    try:
        payload = _read_regular_at(parent, name, label)
    finally:
        os.close(parent)
    if payload is None:
        raise ReviewContractError(f"{label} is missing")
    return payload


def _write_regular_once_at(parent: int, name: str, payload: bytes, label: str) -> None:
    """Create one exact file under a retained parent descriptor."""

    existing = _read_regular_at(parent, name, f"existing {label}")
    if existing is not None:
        if existing != payload:
            raise ReviewContractError(f"existing {label} differs")
        return
    temporary = f".{name}.contextlab-{secrets.token_hex(12)}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = -1
    temporary_stat: os.stat_result | None = None
    destination_created = False
    try:
        descriptor = os.open(temporary, flags, 0o600, dir_fd=parent)
        temporary_stat = os.fstat(descriptor)
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError("short review artifact write")
            written += count
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.link(
            temporary,
            name,
            src_dir_fd=parent,
            dst_dir_fd=parent,
            follow_symlinks=False,
        )
        destination_created = True
        published = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if temporary_stat is None or (
            published.st_dev,
            published.st_ino,
        ) != (temporary_stat.st_dev, temporary_stat.st_ino):
            raise ReviewContractError(f"{label} changed during publication")
        os.unlink(temporary, dir_fd=parent)
        temporary_stat = None
        os.fsync(parent)
    except FileExistsError:
        if _read_regular_at(parent, name, f"concurrent {label}") != payload:
            raise ReviewContractError(f"concurrent {label} differs")
    except OSError as exc:
        raise ReviewContractError(f"cannot publish {label}") from exc
    except Exception:
        if destination_created and temporary_stat is not None:
            try:
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (
                    temporary_stat.st_dev,
                    temporary_stat.st_ino,
                ):
                    os.unlink(name, dir_fd=parent)
            except OSError:
                pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent)
        except OSError:
            pass


def _write_relative_bytes(
    root_descriptor: int, relative_value: Any, payload: bytes, label: str
) -> None:
    parent, name = _open_relative_parent(root_descriptor, relative_value, create=True)
    try:
        _write_regular_once_at(parent, name, payload, label)
    finally:
        os.close(parent)


def read_external_bytes_snapshot(
    path: Path, *, label: str = "external artifact"
) -> bytes:
    """Read exact external bytes once through a retained no-follow parent."""

    parent, name, _ = _open_absolute_parent(path, create=False, label=label)
    try:
        payload = _read_regular_at(parent, name, label)
    finally:
        os.close(parent)
    if payload is None:
        raise ReviewContractError(f"{label} is missing")
    return payload


def write_external_bytes_once_or_verify(
    path: Path, payload: bytes, *, label: str = "external artifact"
) -> None:
    """Publish exact external bytes create-only under a retained parent."""

    if not isinstance(payload, bytes):
        raise ReviewContractError(f"{label} payload must be bytes")
    parent, name, _ = _open_absolute_parent(path, create=True, label=label)
    try:
        _write_regular_once_at(parent, name, payload, label)
    finally:
        os.close(parent)


def _harden_directory_descriptor(descriptor: int) -> None:
    for name in os.listdir(descriptor):
        child = -1
        try:
            child = os.open(name, _READ_FLAGS, dir_fd=descriptor)
            metadata = os.fstat(child)
        except OSError as exc:
            if child >= 0:
                os.close(child)
            raise ReviewContractError("external tree contains an unsafe entry") from exc
        try:
            if stat.S_ISREG(metadata.st_mode):
                os.fchmod(child, 0o400)
            elif stat.S_ISDIR(metadata.st_mode):
                _harden_directory_descriptor(child)
                os.fchmod(child, 0o500)
            else:
                raise ReviewContractError("external tree contains an unsafe entry")
        finally:
            os.close(child)


def harden_external_review_tree(path: Path) -> None:
    """Make a packet/release tree read-only through its retained descriptor."""

    descriptor, _ = _open_absolute_directory(
        path, create=False, label="external review tree"
    )
    try:
        _harden_directory_descriptor(descriptor)
        os.fchmod(descriptor, 0o500)
    finally:
        os.close(descriptor)


def harden_external_review_file(path: Path) -> None:
    parent, name, _ = _open_absolute_parent(
        path, create=False, label="external review file"
    )
    descriptor = -1
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=parent)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ReviewContractError("external review file is unsafe")
        os.fchmod(descriptor, 0o400)
    except OSError as exc:
        raise ReviewContractError("cannot harden external review file") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def _validate_blind_content(
    question: Any,
    candidate_answer: Any,
    cited_evidence: Any,
    *,
    cell_id: str | None = None,
    task_id: str | None = None,
) -> None:
    """Reject machine-readable identity markers from reviewer-visible content."""
    visible = json.dumps(
        {
            "question": question,
            "candidate_answer": candidate_answer,
            "cited_evidence": cited_evidence,
        },
        ensure_ascii=False,
        sort_keys=True,
    ).casefold()
    markers = {
        "strategy_id",
        "reasoning_effort",
        *STRATEGY_LANES,
        *FORBIDDEN_PUBLIC_FIELDS,
    }
    if cell_id:
        markers.add(cell_id.casefold())
    if task_id:
        markers.add(task_id.casefold())
    if any(marker.casefold() in visible for marker in markers):
        raise ReviewContractError("reviewer-visible content exposes an identity marker")
    if _TASK_ID_PATTERN.search(visible):
        raise ReviewContractError("reviewer-visible content exposes a task ID")
    for effort in REASONING_EFFORTS:
        if any(
            marker in visible
            for marker in (
                f"reasoning effort: {effort}",
                f"reasoning effort={effort}",
                f"reasoning-effort-{effort}",
                f"effort={effort}",
            )
        ):
            raise ReviewContractError(
                "reviewer-visible content exposes reasoning effort"
            )


def _validate_private_cells(
    cells: Iterable[dict[str, Any]], *, calibration: bool
) -> list[dict[str, Any]]:
    rows = list(cells)
    expected = CALIBRATION_CELL_COUNT if calibration else MAIN_CELL_COUNT
    if len(rows) != expected:
        label = "calibration" if calibration else "main"
        raise ReviewContractError(
            f"expected {expected} {label} cells, found {len(rows)}"
        )
    seen_ids: set[str] = set()
    combinations: set[tuple[str, str, str]] = set()
    for index, row in enumerate(rows):
        missing = PRIVATE_CELL_REQUIRED_FIELDS.difference(row)
        unknown = set(row).difference(PRIVATE_CELL_FIELDS)
        if missing or unknown:
            raise ReviewContractError(
                f"private cell {index} fields differ; missing={sorted(missing)}, unknown={sorted(unknown)}"
            )
        cell_id = str(row["cell_id"])
        if cell_id in seen_ids:
            raise ReviewContractError(f"duplicate private cell ID: {cell_id}")
        seen_ids.add(cell_id)
        if not calibration:
            if row["strategy_id"] not in STRATEGY_LANES:
                raise ReviewContractError(f"{cell_id}: invalid strategy lane")
            if row["reasoning_effort"] not in REASONING_EFFORTS:
                raise ReviewContractError(f"{cell_id}: invalid reasoning effort")
            combinations.add(
                (
                    str(row["task_id"]),
                    str(row["strategy_id"]),
                    str(row["reasoning_effort"]),
                )
            )
        task_family = row.get("task_family", "unspecified")
        if not isinstance(task_family, str) or not task_family.strip():
            raise ReviewContractError(f"{cell_id}: task family must be non-empty text")
        answer_hash = hashlib.sha256(
            str(row["candidate_answer"]).encode("utf-8")
        ).hexdigest()
        if not isinstance(row["question"], str) or not isinstance(
            row["candidate_answer"], str
        ):
            raise ReviewContractError(
                f"{cell_id}: question and candidate answer must be text"
            )
        citations = row["cited_evidence"]
        if not isinstance(citations, list):
            raise ReviewContractError(f"{cell_id}: cited evidence must be a list")
        for citation in citations:
            if not isinstance(citation, dict) or set(citation) != CITED_EVIDENCE_FIELDS:
                raise ReviewContractError(f"{cell_id}: cited evidence fields differ")
            if not all(
                isinstance(citation[field], str) for field in CITED_EVIDENCE_FIELDS
            ):
                raise ReviewContractError(
                    f"{cell_id}: cited evidence values must be text"
                )
        if answer_hash != row["candidate_sha256"]:
            raise ReviewContractError(f"{cell_id}: candidate answer hash mismatch")
        _validate_blind_content(
            row["question"],
            row["candidate_answer"],
            row["cited_evidence"],
            cell_id=cell_id,
            task_id=str(row["task_id"]),
        )
    if not calibration and len(combinations) != MAIN_CELL_COUNT:
        raise ReviewContractError(
            "main review cells do not cover 160 x 5 x 2 unique combinations"
        )
    if not calibration and len({row["task_id"] for row in rows}) != TASK_COUNT:
        raise ReviewContractError("main review cells do not cover exactly 160 task IDs")
    return rows


def _public_cell(
    row: dict[str, Any],
    *,
    reviewer: str,
    phase: str,
    occurrence: str,
    seed: bytes,
) -> tuple[dict[str, Any], dict[str, Any]]:
    blind_id = (
        "B-" + _digest(seed, reviewer, phase, occurrence, str(row["cell_id"]))[:24]
    )
    public = {
        "blind_cell_id": blind_id,
        "question": row["question"],
        "candidate_answer": row["candidate_answer"],
        "cited_evidence": row["cited_evidence"],
        "rubric_version": RUBRIC_VERSION,
    }
    identity = {
        "blind_cell_id": blind_id,
        "canonical_cell_id": row["cell_id"],
        "task_id": row["task_id"],
        "task_family": row.get("task_family", "unspecified"),
        "strategy_id": row["strategy_id"],
        "reasoning_effort": row["reasoning_effort"],
        "reviewer": reviewer,
        "phase": phase,
        "occurrence": occurrence,
        "candidate_sha256": row["candidate_sha256"],
    }
    return public, identity


def build_review_packets(
    main_cells: Iterable[dict[str, Any]],
    calibration_cells: Iterable[dict[str, Any]],
    *,
    seed: bytes,
    staging_directory: Path,
    external_identity_map: Path,
    sealed_task_ids: Iterable[str],
) -> dict[str, Any]:
    """Build a private staged bundle; release functions enforce the calibration gate."""
    if len(seed) < 32:
        raise ReviewContractError(
            "review randomization seed must contain at least 32 bytes"
        )
    main = _validate_private_cells(main_cells, calibration=False)
    calibration = _validate_private_cells(calibration_cells, calibration=True)
    public_root = _external_absolute(
        staging_directory, "review packet staging directory"
    )
    identity_path = _external_absolute(external_identity_map, "blind identity map")
    sealed_ids = {str(task_id) for task_id in sealed_task_ids}
    if len(sealed_ids) != SEALED_TASK_COUNT:
        raise ReviewContractError(
            "review packet build requires exactly 48 sealed task IDs"
        )
    main_task_ids = {str(row["task_id"]) for row in main}
    if not sealed_ids.issubset(main_task_ids):
        raise ReviewContractError(
            "sealed review task IDs are not all present in the final cells"
        )
    sealed_cell_count = sum(1 for row in main if str(row["task_id"]) in sealed_ids)
    if sealed_cell_count != SEALED_TASK_COUNT * len(STRATEGY_LANES) * len(
        REASONING_EFFORTS
    ):
        raise ReviewContractError(
            "sealed review cells do not cover 48 x 5 x 2 combinations"
        )
    try:
        identity_path.relative_to(public_root)
    except ValueError:
        pass
    else:
        raise ReviewContractError(
            "blind identity map must stay outside the public packet directory"
        )
    public_descriptor, _ = _open_absolute_directory(
        public_root, create=True, label="review packet staging directory"
    )
    identity_parent, identity_name, _ = _open_absolute_parent(
        identity_path, create=True, label="blind identity map"
    )
    identities: list[dict[str, Any]] = []
    packet_records: list[dict[str, Any]] = []
    try:
        for reviewer in REVIEWERS:
            ordered_main = sorted(
                main,
                key=lambda row: _digest(seed, reviewer, "main", str(row["cell_id"])),
            )
            ordered_calibration = sorted(
                calibration,
                key=lambda row: _digest(
                    seed, reviewer, "calibration", str(row["cell_id"])
                ),
            )
            repeat_source = sorted(
                main,
                key=lambda row: _digest(
                    seed, reviewer, "repeat-select", str(row["cell_id"])
                ),
            )[:HIDDEN_REPEAT_COUNT]
            ordered_repeats = sorted(
                repeat_source,
                key=lambda row: _digest(
                    seed, reviewer, "repeat-order", str(row["cell_id"])
                ),
            )
            calibration_presentations = [
                _public_cell(
                    row,
                    reviewer=reviewer,
                    phase="calibration",
                    occurrence=f"cal-{index:03d}",
                    seed=seed,
                )
                for index, row in enumerate(ordered_calibration, start=1)
            ]
            review_presentations = [
                _public_cell(
                    row,
                    reviewer=reviewer,
                    phase="main",
                    occurrence=f"main-{index:04d}",
                    seed=seed,
                )
                for index, row in enumerate(ordered_main, start=1)
            ] + [
                _public_cell(
                    row,
                    reviewer=reviewer,
                    phase="hidden_repeat",
                    occurrence=f"repeat-{index:03d}",
                    seed=seed,
                )
                for index, row in enumerate(ordered_repeats, start=1)
            ]
            review_presentations.sort(
                key=lambda pair: _digest(
                    seed,
                    reviewer,
                    "mixed-review-order",
                    str(pair[0]["blind_cell_id"]),
                )
            )
            phases = (
                (
                    "calibration",
                    _chunks(
                        [pair[0] for pair in calibration_presentations], PACKET_SIZE
                    ),
                ),
                (
                    "review",
                    _chunks([pair[0] for pair in review_presentations], PACKET_SIZE),
                ),
            )
            identity_by_blind_id = {
                pair[0]["blind_cell_id"]: pair[1]
                for pair in calibration_presentations + review_presentations
            }
            for public_phase, packet_cells in phases:
                for packet_index, public_cells in enumerate(packet_cells, start=1):
                    for public in public_cells:
                        identities.append(identity_by_blind_id[public["blind_cell_id"]])
                    packet_id = (
                        "P-"
                        + _digest(seed, reviewer, public_phase, f"{packet_index:03d}")[
                            :24
                        ]
                    )
                    packet = {
                        "schema_version": REVIEW_PACKET_SCHEMA,
                        "packet_id": packet_id,
                        "reviewer": reviewer,
                        "phase": public_phase,
                        "cell_count": len(public_cells),
                        "cells": public_cells,
                    }
                    payload = _pretty_json_bytes(packet)
                    relative_path = f"{reviewer}/{public_phase}-{packet_index:03d}.json"
                    _write_relative_bytes(
                        public_descriptor,
                        relative_path,
                        payload,
                        "review packet",
                    )
                    packet_records.append(
                        {
                            "packet_id": packet_id,
                            "reviewer": reviewer,
                            "phase": public_phase,
                            "cell_count": len(public_cells),
                            "sha256": hashlib.sha256(payload).hexdigest(),
                            "path": relative_path,
                            "utf8_bytes": len(payload),
                        }
                    )
        identity_payload = {
            "schema_version": "contextlab.review-identity-map.v1",
            "seed_sha256": hashlib.sha256(seed).hexdigest(),
            "identities": identities,
        }
        identity_bytes = _pretty_json_bytes(identity_payload)
        _write_regular_once_at(
            identity_parent, identity_name, identity_bytes, "blind identity map"
        )
        manifest: dict[str, Any] = {
            "schema_version": REVIEW_MANIFEST_SCHEMA,
            "rubric_version": RUBRIC_VERSION,
            "reviewers": list(REVIEWERS),
            "packet_size": PACKET_SIZE,
            "unique_cells_per_reviewer": MAIN_CELL_COUNT,
            "calibration_cells_per_reviewer": CALIBRATION_CELL_COUNT,
            "hidden_repeats_per_reviewer": HIDDEN_REPEAT_COUNT,
            "main_packets_per_reviewer": MAIN_PACKET_COUNT,
            "calibration_packets_per_reviewer": CALIBRATION_PACKET_COUNT,
            "total_packets_per_reviewer": MAIN_PACKET_COUNT
            + CALIBRATION_PACKET_COUNT
            + REPEAT_PACKET_COUNT,
            "seed_sha256": hashlib.sha256(seed).hexdigest(),
            "identity_map_sha256": hashlib.sha256(identity_bytes).hexdigest(),
            "packets": packet_records,
        }
        manifest["manifest_sha256"] = hashlib.sha256(
            _canonical_json(manifest)
        ).hexdigest()
        _write_relative_bytes(
            public_descriptor,
            "manifest.json",
            _pretty_json_bytes(manifest),
            "review packet manifest",
        )
        _validate_public_packets_descriptor(public_descriptor, manifest)
        return manifest
    finally:
        os.close(identity_parent)
        os.close(public_descriptor)


def _relative_regular_files(
    root_descriptor: int, prefix: tuple[str, ...] = ()
) -> set[str]:
    files: set[str] = set()
    try:
        names = os.listdir(root_descriptor)
    except OSError as exc:
        raise ReviewContractError("cannot enumerate review packet directory") from exc
    for name in names:
        if name in {".", ".."}:
            continue
        try:
            metadata = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
        except OSError as exc:
            raise ReviewContractError("cannot inspect review packet tree") from exc
        relative = (*prefix, name)
        if stat.S_ISREG(metadata.st_mode):
            files.add("/".join(relative))
        elif stat.S_ISDIR(metadata.st_mode):
            try:
                child = os.open(name, _DIRECTORY_FLAGS, dir_fd=root_descriptor)
            except OSError as exc:
                raise ReviewContractError(
                    "review packet tree contains an unsafe directory"
                ) from exc
            try:
                files.update(_relative_regular_files(child, relative))
            finally:
                os.close(child)
        else:
            raise ReviewContractError("review packet tree contains an unsafe entry")
    return files


def _validate_public_packets_descriptor(
    root_descriptor: int, manifest: dict[str, Any]
) -> None:
    if set(manifest) != REVIEW_MANIFEST_FIELDS:
        raise ReviewContractError("review manifest fields differ")
    if manifest.get("schema_version") != REVIEW_MANIFEST_SCHEMA:
        raise ReviewContractError("unsupported review manifest schema")
    without_hash = {
        key: value for key, value in manifest.items() if key != "manifest_sha256"
    }
    if (
        manifest.get("manifest_sha256")
        != hashlib.sha256(_canonical_json(without_hash)).hexdigest()
    ):
        raise ReviewContractError("review manifest hash mismatch")
    packets = manifest.get("packets")
    if not isinstance(packets, list) or len(packets) != len(REVIEWERS) * 85:
        raise ReviewContractError(
            "review manifest must contain 85 packets for each of three reviewers"
        )
    counts: dict[tuple[str, str], int] = {}
    blind_ids: set[str] = set()
    expected_files = {"manifest.json"}
    for record in packets:
        if set(record) != {
            "packet_id",
            "reviewer",
            "phase",
            "cell_count",
            "sha256",
            "path",
            "utf8_bytes",
        }:
            raise ReviewContractError("review manifest packet fields differ")
        relative_path = str(record["path"])
        payload = _read_relative_bytes(root_descriptor, relative_path, "review packet")
        expected_files.add(relative_path)
        if (
            hashlib.sha256(payload).hexdigest() != record["sha256"]
            or len(payload) != record["utf8_bytes"]
        ):
            raise ReviewContractError(f"packet hash mismatch: {record['path']}")
        try:
            packet = json.loads(payload.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReviewContractError(
                f"packet JSON is invalid: {record['path']}"
            ) from exc
        if set(packet) != {
            "schema_version",
            "packet_id",
            "reviewer",
            "phase",
            "cell_count",
            "cells",
        }:
            raise ReviewContractError(f"packet fields differ: {record['path']}")
        if packet["phase"] not in {"calibration", "review"}:
            raise ReviewContractError(
                f"public packet exposes a private phase: {record['path']}"
            )
        if (
            packet["phase"] != record["phase"]
            or packet["reviewer"] != record["reviewer"]
        ):
            raise ReviewContractError(
                f"packet manifest identity mismatch: {record['path']}"
            )
        if packet["packet_id"] != record["packet_id"]:
            raise ReviewContractError(f"packet ID mismatch: {record['path']}")
        if (
            packet["cell_count"] != len(packet["cells"])
            or packet["cell_count"] != record["cell_count"]
        ):
            raise ReviewContractError(f"packet cell count mismatch: {record['path']}")
        for cell in packet["cells"]:
            if set(cell) != PUBLIC_CELL_FIELDS:
                raise ReviewContractError(
                    f"public review cell leaks or omits fields: {record['path']}"
                )
            citations = cell["cited_evidence"]
            if not isinstance(citations, list) or any(
                not isinstance(citation, dict)
                or set(citation) != CITED_EVIDENCE_FIELDS
                or not all(
                    isinstance(citation[field], str) for field in CITED_EVIDENCE_FIELDS
                )
                for citation in citations
            ):
                raise ReviewContractError(
                    f"public review citations differ: {record['path']}"
                )
            _validate_blind_content(
                cell["question"],
                cell["candidate_answer"],
                cell["cited_evidence"],
            )
            blind_id = str(cell["blind_cell_id"])
            if blind_id in blind_ids:
                raise ReviewContractError(f"duplicate blind cell ID: {blind_id}")
            blind_ids.add(blind_id)
        key = (str(packet["reviewer"]), str(packet["phase"]))
        counts[key] = counts.get(key, 0) + int(packet["cell_count"])
    for reviewer in REVIEWERS:
        expected = {"review": 1680, "calibration": 20}
        actual = {phase: counts.get((reviewer, phase), 0) for phase in expected}
        if actual != expected:
            raise ReviewContractError(
                f"{reviewer}: review cell counts {actual} != {expected}"
            )
    if _relative_regular_files(root_descriptor) != expected_files:
        raise ReviewContractError("review packet tree contains extra or missing files")


def validate_public_packets(public_directory: Path, manifest: dict[str, Any]) -> None:
    descriptor, _ = _open_absolute_directory(
        public_directory, create=False, label="review packet directory"
    )
    try:
        _validate_public_packets_descriptor(descriptor, manifest)
    finally:
        os.close(descriptor)


def verified_reviewer_packet_payloads(
    public_directory: Path,
    manifest: Mapping[str, Any],
    reviewer: str,
    *,
    phase: str | None = None,
) -> dict[str, bytes]:
    """Read exact reviewer packet bytes through one retained directory descriptor."""

    if reviewer not in REVIEWERS:
        raise ReviewContractError("unknown packet reviewer")
    if phase is not None and phase not in {"calibration", "review"}:
        raise ReviewContractError("unknown packet phase")
    descriptor, _ = _open_absolute_directory(
        public_directory, create=False, label="review packet directory"
    )
    try:
        _validate_public_packets_descriptor(descriptor, dict(manifest))
        payloads: dict[str, bytes] = {}
        for record in manifest["packets"]:
            if record["reviewer"] != reviewer or (
                phase is not None and record["phase"] != phase
            ):
                continue
            payload = _read_relative_bytes(descriptor, record["path"], "review packet")
            if (
                hashlib.sha256(payload).hexdigest() != record["sha256"]
                or len(payload) != record["utf8_bytes"]
            ):
                raise ReviewContractError("review packet changed after validation")
            payloads[str(record["packet_id"])] = payload
        return payloads
    finally:
        os.close(descriptor)


def verified_review_assignments(
    public_directory: Path,
    manifest: Mapping[str, Any],
    reviewer: str,
) -> list[dict[str, Any]]:
    """Derive reviewer assignments from exact validated packet bytes."""

    payloads = verified_reviewer_packet_payloads(public_directory, manifest, reviewer)
    assignments: list[dict[str, Any]] = []
    for record in manifest["packets"]:
        if record["reviewer"] != reviewer:
            continue
        payload = payloads[str(record["packet_id"])]
        try:
            packet = json.loads(payload.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReviewContractError("validated review packet JSON changed") from exc
        for position, cell in enumerate(packet["cells"], start=1):
            assignments.append(
                {
                    "blind_cell_id": cell["blind_cell_id"],
                    "packet_id": packet["packet_id"],
                    "phase": packet["phase"],
                    "position": position,
                }
            )
    return assignments


def _copy_release(
    staging_directory: Path,
    release_directory: Path,
    manifest: dict[str, Any],
    token_preflights: dict[str, dict[str, Any]],
    *,
    phase: str,
) -> dict[str, Any]:
    source_descriptor, _ = _open_absolute_directory(
        staging_directory, create=False, label="review packet staging directory"
    )
    release_descriptor = -1
    try:
        _validate_public_packets_descriptor(source_descriptor, manifest)
        release_descriptor, _ = _open_absolute_directory(
            release_directory, create=True, label="review release directory"
        )
        released: list[dict[str, Any]] = []
        for record in manifest["packets"]:
            if record["phase"] != phase:
                continue
            payload = _read_relative_bytes(
                source_descriptor, record["path"], "staged review packet"
            )
            if (
                hashlib.sha256(payload).hexdigest() != record["sha256"]
                or len(payload) != record["utf8_bytes"]
            ):
                raise ReviewContractError("staged review packet changed during release")
            _write_relative_bytes(
                release_descriptor,
                record["path"],
                payload,
                "released review packet",
            )
            released.append(record)
        release_manifest: dict[str, Any] = {
            "schema_version": REVIEW_RELEASE_SCHEMA,
            "source_manifest_sha256": manifest["manifest_sha256"],
            "phase": phase,
            "packet_count": len(released),
            "packets": released,
            "token_preflight_by_reviewer": {
                reviewer: token_preflights[reviewer] for reviewer in REVIEWERS[:2]
            },
            "token_preflight_sha256_by_reviewer": {
                reviewer: hashlib.sha256(
                    _canonical_json(token_preflights[reviewer])
                ).hexdigest()
                for reviewer in REVIEWERS[:2]
            },
        }
        release_manifest["manifest_sha256"] = hashlib.sha256(
            _canonical_json(release_manifest)
        ).hexdigest()
        _write_relative_bytes(
            release_descriptor,
            "manifest.json",
            _pretty_json_bytes(release_manifest),
            "review release manifest",
        )
        expected_files = {"manifest.json", *(str(row["path"]) for row in released)}
        if _relative_regular_files(release_descriptor) != expected_files:
            raise ReviewContractError("review release tree differs after publication")
        return release_manifest
    finally:
        if release_descriptor >= 0:
            os.close(release_descriptor)
        os.close(source_descriptor)


def release_calibration_packets(
    staging_directory: Path,
    release_directory: Path,
    manifest: dict[str, Any],
    token_preflights: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    _validate_ai_token_preflights(token_preflights, manifest)
    released = _copy_release(
        staging_directory,
        release_directory,
        manifest,
        token_preflights,
        phase="calibration",
    )
    if released["packet_count"] != len(REVIEWERS) * CALIBRATION_PACKET_COUNT:
        raise ReviewContractError("calibration release did not contain three packets")
    return released


def _finite_rate(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReviewContractError(f"{label} must be numeric")
    numeric = float(value)
    if not 0.0 <= numeric <= 1.0:
        raise ReviewContractError(f"{label} must be between zero and one")
    return numeric


def _validate_calibration_gate_record(
    record: dict[str, Any], manifest: dict[str, Any]
) -> None:
    if set(record) != CALIBRATION_GATE_FIELDS:
        raise ReviewContractError("calibration gate fields differ")
    if record["schema_version"] != CALIBRATION_GATE_SCHEMA:
        raise ReviewContractError("main release requires a calibration gate record")
    if record["review_manifest_sha256"] != manifest.get("manifest_sha256"):
        raise ReviewContractError(
            "calibration record belongs to a different review manifest"
        )
    if record["identity_map_sha256"] != manifest.get("identity_map_sha256"):
        raise ReviewContractError("calibration record uses a different identity map")
    if (
        not isinstance(record["reference_sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", record["reference_sha256"]) is None
    ):
        raise ReviewContractError("calibration reference hash is invalid")
    if record["cell_count_per_reviewer"] != CALIBRATION_CELL_COUNT:
        raise ReviewContractError("calibration record cell count differs")
    metrics = record["metrics_vs_reference"]
    if not isinstance(metrics, dict) or set(metrics) != set(REVIEWERS):
        raise ReviewContractError("calibration metrics do not cover all reviewers")
    thresholds_pass = True
    for reviewer, values in metrics.items():
        if not isinstance(values, dict) or set(values) != {
            "cells",
            "exact_ordinal_rate",
            "within_one_ordinal_rate",
            "accepted_match_rate",
        }:
            raise ReviewContractError(f"{reviewer}: calibration metric fields differ")
        if values["cells"] != CALIBRATION_CELL_COUNT:
            raise ReviewContractError(f"{reviewer}: calibration metric count differs")
        exact = _finite_rate(values["exact_ordinal_rate"], "exact ordinal rate")
        within_one = _finite_rate(values["within_one_ordinal_rate"], "within-one rate")
        accepted = _finite_rate(values["accepted_match_rate"], "accepted match rate")
        thresholds_pass = thresholds_pass and (
            exact >= CALIBRATION_EXACT_ORDINAL_MIN
            and within_one >= CALIBRATION_WITHIN_ONE_MIN
            and accepted >= CALIBRATION_ACCEPTED_MATCH_MIN
        )
    comparisons = record["ai_vs_kevin"]
    if not isinstance(comparisons, dict) or set(comparisons) != set(REVIEWERS[:2]):
        raise ReviewContractError("AI-versus-Kevin metrics differ")
    for reviewer, values in comparisons.items():
        if not isinstance(values, dict) or set(values) != {
            "accepted_match_rate",
            "mean_absolute_ordinal_difference",
        }:
            raise ReviewContractError(f"{reviewer}: AI-versus-Kevin fields differ")
        accepted = _finite_rate(values["accepted_match_rate"], "AI accepted match rate")
        difference = values["mean_absolute_ordinal_difference"]
        if (
            isinstance(difference, bool)
            or not isinstance(difference, (int, float))
            or not math.isfinite(float(difference))
            or difference < 0
        ):
            raise ReviewContractError("AI ordinal difference is invalid")
        thresholds_pass = thresholds_pass and (
            accepted >= AI_KEVIN_ACCEPTED_MATCH_MIN
            and difference <= AI_KEVIN_MEAN_ABSOLUTE_DIFFERENCE_MAX
        )
    ambiguity = record["rubric_ambiguity_by_reviewer"]
    if (
        not isinstance(ambiguity, dict)
        or set(ambiguity) != set(REVIEWERS)
        or any(not isinstance(value, bool) for value in ambiguity.values())
    ):
        raise ReviewContractError("calibration ambiguity decisions differ")
    approved = thresholds_pass and not any(ambiguity.values())
    expected_status = "approved" if approved else "restart_required"
    restart = record["restart_all_three_reviews"]
    if (
        not isinstance(restart, bool)
        or record["status"] != expected_status
        or restart == approved
    ):
        raise ReviewContractError(
            "calibration decision is inconsistent with its measured evidence"
        )


def _validate_token_preflight_record(
    record: dict[str, Any], manifest: dict[str, Any], reviewer: str
) -> None:
    if set(record) != TOKEN_PREFLIGHT_FIELDS:
        raise ReviewContractError("review token-preflight fields differ")
    if (
        record["schema_version"] != TOKEN_PREFLIGHT_SCHEMA
        or record["reviewer"] != reviewer
    ):
        raise ReviewContractError("review token preflight has the wrong identity")
    if record["review_manifest_sha256"] != manifest.get("manifest_sha256"):
        raise ReviewContractError(
            "review token preflight belongs to a different manifest"
        )
    expected_packets = [
        row for row in manifest["packets"] if row["reviewer"] == reviewer
    ]
    expected_ids = {str(row["packet_id"]) for row in expected_packets}
    if record["packet_count"] != len(expected_packets):
        raise ReviewContractError("review token-preflight packet count differs")
    if record["utf8_bytes_total"] != sum(
        int(row["utf8_bytes"]) for row in expected_packets
    ):
        raise ReviewContractError("review token-preflight byte count differs")
    for field in ("packet_count", "utf8_bytes_total", "token_total"):
        if (
            isinstance(record[field], bool)
            or not isinstance(record[field], int)
            or record[field] < 0
        ):
            raise ReviewContractError(f"review token-preflight {field} is invalid")
    if (
        not isinstance(record["tokenizer_id"], str)
        or not record["tokenizer_id"].strip()
    ):
        raise ReviewContractError("review token preflight has no tokenizer")
    packet_token_counts = record["packet_token_counts"]
    packet_bytes = {
        str(row["packet_id"]): int(row["utf8_bytes"]) for row in expected_packets
    }
    if (
        not isinstance(packet_token_counts, dict)
        or set(packet_token_counts) != expected_ids
        or any(
            not isinstance(packet_id, str)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count <= 0
            or count > packet_bytes.get(packet_id, -1)
            for packet_id, count in packet_token_counts.items()
        )
    ):
        raise ReviewContractError(
            "review token preflight counts do not cover the manifest"
        )
    if record["token_total"] != sum(packet_token_counts.values()):
        raise ReviewContractError(
            "review token-preflight total does not match its packet counts"
        )
    expected_count_hash = hashlib.sha256(
        _canonical_json(packet_token_counts)
    ).hexdigest()
    if (
        not isinstance(record["packet_token_counts_sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", record["packet_token_counts_sha256"]) is None
    ):
        raise ReviewContractError("review token-preflight count hash is invalid")
    if not hmac.compare_digest(
        record["packet_token_counts_sha256"], expected_count_hash
    ):
        raise ReviewContractError(
            "review token-preflight count hash does not match its counts"
        )
    if (
        record["confirmed_before_review"] is not True
        or record["confirmed_by"] != "Kevin Araujo"
    ):
        raise ReviewContractError("Kevin has not confirmed review subscription usage")


def _validate_ai_token_preflights(
    token_preflights: dict[str, dict[str, Any]], manifest: dict[str, Any]
) -> None:
    if set(token_preflights) != set(REVIEWERS[:2]):
        raise ReviewContractError("review release requires both AI token preflights")
    for reviewer in REVIEWERS[:2]:
        _validate_token_preflight_record(token_preflights[reviewer], manifest, reviewer)


def release_review_packets(
    staging_directory: Path,
    release_directory: Path,
    manifest: dict[str, Any],
    calibration_record: dict[str, Any],
    token_preflights: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    _validate_calibration_gate_record(calibration_record, manifest)
    if calibration_record["status"] != "approved":
        raise ReviewContractError(
            "main review remains locked until calibration is approved"
        )
    _validate_ai_token_preflights(token_preflights, manifest)
    released = _copy_release(
        staging_directory,
        release_directory,
        manifest,
        token_preflights,
        phase="review",
    )
    if released["packet_count"] != len(REVIEWERS) * (
        MAIN_PACKET_COUNT + REPEAT_PACKET_COUNT
    ):
        raise ReviewContractError(
            "main review release did not contain 84 packets per reviewer"
        )
    return released


def replay_review_release(
    release_directory: Path,
    source_manifest: Mapping[str, Any],
    token_preflights: Mapping[str, Mapping[str, Any]],
    *,
    phase: str,
) -> dict[str, Any]:
    """Replay a release from exact bytes under one retained directory handle."""

    if phase not in {"calibration", "review"}:
        raise ReviewContractError("review release phase is invalid")
    descriptor, _ = _open_absolute_directory(
        release_directory, create=False, label="review release directory"
    )
    try:
        payload = _read_relative_bytes(
            descriptor, "manifest.json", "review release manifest"
        )
        try:
            manifest = json.loads(payload.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReviewContractError(
                "review release manifest JSON is invalid"
            ) from exc
        expected_packets = [
            row for row in source_manifest["packets"] if row["phase"] == phase
        ]
        expected_fields = {
            "schema_version",
            "source_manifest_sha256",
            "phase",
            "packet_count",
            "packets",
            "token_preflight_by_reviewer",
            "token_preflight_sha256_by_reviewer",
            "manifest_sha256",
        }
        unsigned = {
            key: item for key, item in manifest.items() if key != "manifest_sha256"
        }
        if (
            set(manifest) != expected_fields
            or manifest.get("schema_version") != REVIEW_RELEASE_SCHEMA
            or manifest.get("source_manifest_sha256")
            != source_manifest.get("manifest_sha256")
            or manifest.get("phase") != phase
            or manifest.get("packet_count") != len(expected_packets)
            or manifest.get("packets") != expected_packets
            or manifest.get("token_preflight_by_reviewer") != token_preflights
            or manifest.get("token_preflight_sha256_by_reviewer")
            != {
                reviewer: hashlib.sha256(
                    _canonical_json(token_preflights[reviewer])
                ).hexdigest()
                for reviewer in REVIEWERS[:2]
            }
            or manifest.get("manifest_sha256")
            != hashlib.sha256(_canonical_json(unsigned)).hexdigest()
        ):
            raise ReviewContractError("saved review release differs")
        expected_files = {"manifest.json"}
        for record in expected_packets:
            packet = _read_relative_bytes(
                descriptor, record["path"], "released review packet"
            )
            if (
                hashlib.sha256(packet).hexdigest() != record["sha256"]
                or len(packet) != record["utf8_bytes"]
            ):
                raise ReviewContractError("saved released packet hash differs")
            expected_files.add(str(record["path"]))
        if _relative_regular_files(descriptor) != expected_files:
            raise ReviewContractError(
                "saved review release contains extra or missing files"
            )
        return manifest
    finally:
        os.close(descriptor)


def validate_review_protocol(path: Path) -> dict[str, int]:
    protocol = json.loads(path.read_text(encoding="utf-8"))
    if protocol.get("schema_version") != "contextlab.review-protocol.v1":
        raise ReviewContractError("unsupported review protocol schema")
    if protocol.get("rubric") != REVIEW_RUBRIC:
        raise ReviewContractError(
            "review rubric lacks the frozen ordinal anchors or acceptance rule"
        )
    main = protocol.get("main_review", {})
    repeats = protocol.get("repeats", {})
    calibration = protocol.get("calibration", {})
    total = protocol.get("total_per_reviewer", {})
    if main.get("cells_per_reviewer") != MAIN_CELL_COUNT:
        raise ReviewContractError(
            "review protocol does not require all 1,600 final cells"
        )
    if (
        main.get("packets_per_reviewer") != MAIN_PACKET_COUNT
        or main.get("packet_size") != PACKET_SIZE
    ):
        raise ReviewContractError("main review packet count differs from 80 x 20")
    if repeats.get("cells_per_reviewer") != HIDDEN_REPEAT_COUNT or not repeats.get(
        "hidden"
    ):
        raise ReviewContractError("hidden-repeat contract differs from 80 cells")
    if calibration.get("cells") != CALIBRATION_CELL_COUNT:
        raise ReviewContractError("calibration packet must contain 20 cells")
    expected_thresholds = {
        "exact_ordinal_rate_min": CALIBRATION_EXACT_ORDINAL_MIN,
        "within_one_ordinal_rate_min": CALIBRATION_WITHIN_ONE_MIN,
        "accepted_match_rate_min": CALIBRATION_ACCEPTED_MATCH_MIN,
        "ai_kevin_accepted_match_rate_min": AI_KEVIN_ACCEPTED_MATCH_MIN,
        "ai_kevin_mean_absolute_ordinal_difference_max": (
            AI_KEVIN_MEAN_ABSOLUTE_DIFFERENCE_MAX
        ),
    }
    if calibration.get("agreement_thresholds") != expected_thresholds:
        raise ReviewContractError(
            "calibration thresholds differ from the frozen protocol"
        )
    if calibration.get("reference_location") != "external_to_repository":
        raise ReviewContractError("calibration targets must remain external")
    if calibration.get("main_release_requires_approved_gate") is not True:
        raise ReviewContractError(
            "main review must remain locked until calibration passes"
        )
    if total != {"cells": 1700, "packets": 85}:
        raise ReviewContractError(
            "total review workload must be 1,700 cells in 85 packets"
        )
    blind_fields = protocol.get("blind_fields")
    if not isinstance(blind_fields, list) or not {
        "task_id",
        "strategy_id",
        "reasoning_effort",
    }.issubset(blind_fields):
        raise ReviewContractError(
            "task, strategy, and reasoning identities must remain blind"
        )
    reviewers = protocol.get("reviewers", [])
    if [row.get("id") for row in reviewers] != list(REVIEWERS):
        raise ReviewContractError(
            "reviewer identities or order differ from the approved panel"
        )
    human = protocol.get("human_review", {})
    if not human.get("sole_human_reviewer") or not human.get(
        "all_final_cells_required"
    ):
        raise ReviewContractError(
            "Kevin must remain the sole human reviewer for every final cell"
        )
    return {
        "reviewers": len(reviewers),
        "main_cells_per_reviewer": MAIN_CELL_COUNT,
        "total_packets_per_reviewer": 85,
    }


def validate_grade(grade: dict[str, Any]) -> None:
    expected = {
        "overall_ordinal",
        "factual_correctness",
        "completeness",
        "citation_support",
        "authority_freshness",
        "abstention_quality",
        "accepted",
        "failure_labels",
        "comment",
    }
    if set(grade) != expected:
        raise ReviewContractError("grade fields differ from the frozen rubric")
    for field in (
        "overall_ordinal",
        "factual_correctness",
        "completeness",
        "citation_support",
        "authority_freshness",
    ):
        if (
            isinstance(grade[field], bool)
            or not isinstance(grade[field], int)
            or not 0 <= grade[field] <= 3
        ):
            raise ReviewContractError(f"{field} must be an integer from 0 to 3")
    if grade["abstention_quality"] not in {"not_applicable", "correct", "incorrect"}:
        raise ReviewContractError("invalid abstention_quality")
    if not isinstance(grade["accepted"], bool):
        raise ReviewContractError("accepted must be boolean")
    failure_labels = grade["failure_labels"]
    allowed_failure_labels = set(REVIEW_RUBRIC["failure_labels"])
    if (
        not isinstance(failure_labels, list)
        or any(
            not isinstance(label, str) or label not in allowed_failure_labels
            for label in failure_labels
        )
        or len(failure_labels) != len(set(failure_labels))
    ):
        raise ReviewContractError(
            "failure_labels must be unique values from the frozen rubric"
        )
    if (grade["abstention_quality"] == "incorrect") != (
        "incorrect_abstention" in failure_labels
    ):
        raise ReviewContractError(
            "incorrect abstention quality and its critical failure label must agree"
        )
    label_conflicts = {
        "wrong_answer": grade["factual_correctness"] != 0,
        "material_omission": grade["completeness"] > 1,
        "unsupported_material_claim": grade["citation_support"] > 1,
        "stale_or_low_authority": grade["authority_freshness"] > 1,
        "provider_or_format_failure": grade["overall_ordinal"] != 0,
    }
    if any(
        label in failure_labels and conflict
        for label, conflict in label_conflicts.items()
    ):
        raise ReviewContractError(
            "critical failure label contradicts its frozen dimension constraint"
        )
    dimension_scores = [
        grade["factual_correctness"],
        grade["completeness"],
        grade["citation_support"],
        grade["authority_freshness"],
    ]
    if grade["abstention_quality"] == "correct" and 0 in dimension_scores:
        raise ReviewContractError(
            "a correct abstention cannot receive a zero dimension score"
        )
    expected_overall = min(dimension_scores)
    if grade["abstention_quality"] == "incorrect":
        expected_overall = 0
    elif failure_labels:
        expected_overall = min(expected_overall, 1)
    if grade["overall_ordinal"] != expected_overall:
        raise ReviewContractError(
            "overall_ordinal must follow the frozen minimum-dimension and cap rule"
        )
    expected_acceptance = grade["overall_ordinal"] >= 2 and not failure_labels
    if grade["accepted"] is not expected_acceptance:
        raise ReviewContractError(
            "accepted must follow the frozen ordinal and critical-failure rule"
        )
    if not isinstance(grade["comment"], str):
        raise ReviewContractError("comment must be text")


def evaluate_calibration(
    grades_by_reviewer: dict[str, dict[str, dict[str, Any]]],
    *,
    identity_map_path: Path,
    external_reference_path: Path,
    review_manifest: dict[str, Any],
    rubric_ambiguity_by_reviewer: dict[str, bool],
    identity_map_bytes: bytes | None = None,
    reference_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Score the shared calibration packet without returning its external targets."""
    if set(grades_by_reviewer) != set(REVIEWERS):
        raise ReviewContractError(
            "calibration requires grades from all three reviewers"
        )
    if set(rubric_ambiguity_by_reviewer) != set(REVIEWERS) or any(
        not isinstance(value, bool) for value in rubric_ambiguity_by_reviewer.values()
    ):
        raise ReviewContractError(
            "calibration requires one ambiguity decision per reviewer"
        )
    identity_payload = (
        identity_map_bytes
        if identity_map_bytes is not None
        else read_external_bytes_snapshot(identity_map_path, label="blind identity map")
    )
    reference_payload = (
        reference_bytes
        if reference_bytes is not None
        else read_external_bytes_snapshot(
            external_reference_path, label="calibration reference"
        )
    )
    try:
        identity_map = json.loads(identity_payload.decode("utf-8", errors="strict"))
        reference = json.loads(reference_payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewContractError(
            "calibration identity or reference JSON is invalid"
        ) from exc
    if hashlib.sha256(identity_payload).hexdigest() != review_manifest.get(
        "identity_map_sha256"
    ):
        raise ReviewContractError(
            "calibration identity map differs from the review manifest"
        )
    if reference.get("schema_version") != CALIBRATION_REFERENCE_SCHEMA:
        raise ReviewContractError("unsupported calibration reference schema")
    targets = reference.get("targets")
    if not isinstance(targets, list) or len(targets) != CALIBRATION_CELL_COUNT:
        raise ReviewContractError(
            "calibration reference must contain exactly 20 targets"
        )
    target_by_id: dict[str, dict[str, Any]] = {}
    for target in targets:
        if not isinstance(target, dict) or set(target) != {
            "canonical_cell_id",
            "overall_ordinal",
            "accepted",
        }:
            raise ReviewContractError("calibration target fields differ")
        canonical = str(target["canonical_cell_id"])
        if canonical in target_by_id:
            raise ReviewContractError("duplicate calibration target")
        if (
            isinstance(target["overall_ordinal"], bool)
            or not isinstance(target["overall_ordinal"], int)
            or not 0 <= target["overall_ordinal"] <= 3
        ):
            raise ReviewContractError("calibration target ordinal must be 0..3")
        if not isinstance(target["accepted"], bool):
            raise ReviewContractError("calibration target accepted must be boolean")
        target_by_id[canonical] = target
    identities = identity_map.get("identities")
    if not isinstance(identities, list):
        raise ReviewContractError("blind identity map has no identities")
    metrics: dict[str, dict[str, float | int]] = {}
    canonical_grades: dict[str, dict[str, dict[str, Any]]] = {}
    for reviewer in REVIEWERS:
        reviewer_identities = [
            row
            for row in identities
            if row.get("reviewer") == reviewer and row.get("phase") == "calibration"
        ]
        if len(reviewer_identities) != CALIBRATION_CELL_COUNT:
            raise ReviewContractError(
                f"{reviewer}: calibration identity count is not 20"
            )
        supplied = grades_by_reviewer[reviewer]
        expected_blind_ids = {str(row["blind_cell_id"]) for row in reviewer_identities}
        if set(supplied) != expected_blind_ids:
            raise ReviewContractError(
                f"{reviewer}: calibration grades are incomplete or extra"
            )
        exact = 0
        within_one = 0
        accepted_match = 0
        reviewer_canonical: dict[str, dict[str, Any]] = {}
        for identity in reviewer_identities:
            blind_id = str(identity["blind_cell_id"])
            canonical = str(identity["canonical_cell_id"])
            if canonical not in target_by_id:
                raise ReviewContractError("calibration identity has no external target")
            grade = supplied[blind_id]
            validate_grade(grade)
            reviewer_canonical[canonical] = grade
            target = target_by_id[canonical]
            difference = abs(
                int(grade["overall_ordinal"]) - int(target["overall_ordinal"])
            )
            exact += difference == 0
            within_one += difference <= 1
            accepted_match += grade["accepted"] == target["accepted"]
        canonical_grades[reviewer] = reviewer_canonical
        metrics[reviewer] = {
            "cells": CALIBRATION_CELL_COUNT,
            "exact_ordinal_rate": exact / CALIBRATION_CELL_COUNT,
            "within_one_ordinal_rate": within_one / CALIBRATION_CELL_COUNT,
            "accepted_match_rate": accepted_match / CALIBRATION_CELL_COUNT,
        }
    ai_vs_kevin: dict[str, dict[str, float]] = {}
    for reviewer in REVIEWERS[:2]:
        differences: list[int] = []
        accepted_matches = 0
        for canonical, ai_grade in canonical_grades[reviewer].items():
            kevin_grade = canonical_grades["kevin"][canonical]
            differences.append(
                abs(
                    int(ai_grade["overall_ordinal"])
                    - int(kevin_grade["overall_ordinal"])
                )
            )
            accepted_matches += ai_grade["accepted"] == kevin_grade["accepted"]
        ai_vs_kevin[reviewer] = {
            "accepted_match_rate": accepted_matches / CALIBRATION_CELL_COUNT,
            "mean_absolute_ordinal_difference": sum(differences)
            / CALIBRATION_CELL_COUNT,
        }
    thresholds_pass = all(
        values["exact_ordinal_rate"] >= CALIBRATION_EXACT_ORDINAL_MIN
        and values["within_one_ordinal_rate"] >= CALIBRATION_WITHIN_ONE_MIN
        and values["accepted_match_rate"] >= CALIBRATION_ACCEPTED_MATCH_MIN
        for values in metrics.values()
    ) and all(
        values["accepted_match_rate"] >= AI_KEVIN_ACCEPTED_MATCH_MIN
        and values["mean_absolute_ordinal_difference"]
        <= AI_KEVIN_MEAN_ABSOLUTE_DIFFERENCE_MAX
        for values in ai_vs_kevin.values()
    )
    no_ambiguity = not any(rubric_ambiguity_by_reviewer.values())
    return {
        "schema_version": CALIBRATION_GATE_SCHEMA,
        "status": "approved"
        if thresholds_pass and no_ambiguity
        else "restart_required",
        "review_manifest_sha256": review_manifest["manifest_sha256"],
        "identity_map_sha256": hashlib.sha256(identity_payload).hexdigest(),
        "reference_sha256": hashlib.sha256(reference_payload).hexdigest(),
        "cell_count_per_reviewer": CALIBRATION_CELL_COUNT,
        "metrics_vs_reference": metrics,
        "ai_vs_kevin": ai_vs_kevin,
        "rubric_ambiguity_by_reviewer": rubric_ambiguity_by_reviewer,
        "restart_all_three_reviews": not (thresholds_pass and no_ambiguity),
    }


class ReviewStore:
    """Small SQLite store used by Kevin's and the AI reviewers' save-and-resume flows."""

    def __init__(self, path: Path):
        self.path: Path | None = path
        self._snapshot: bytes | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection:
            self._initialize(connection)
            connection.commit()

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS grade (
                reviewer TEXT NOT NULL,
                blind_cell_id TEXT NOT NULL,
                grade_json TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (reviewer, blind_cell_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS assignment (
                reviewer TEXT NOT NULL,
                blind_cell_id TEXT NOT NULL,
                packet_id TEXT NOT NULL,
                phase TEXT NOT NULL,
                position INTEGER NOT NULL,
                PRIMARY KEY (reviewer, blind_cell_id)
            )
            """
        )

    @classmethod
    def build_snapshot(
        cls,
        reviewer: str,
        assignments: Iterable[dict[str, Any]],
        grades_by_blind_id: Mapping[str, dict[str, Any]],
    ) -> bytes:
        """Build one immutable SQLite image without opening an external path."""

        if reviewer not in REVIEWERS:
            raise ReviewContractError(f"unknown reviewer: {reviewer}")
        rows = list(assignments)
        assignment_ids: set[str] = set()
        for row in rows:
            if set(row) != {"blind_cell_id", "packet_id", "phase", "position"}:
                raise ReviewContractError("review assignment fields differ")
            if row["phase"] not in {"calibration", "review"}:
                raise ReviewContractError("review assignment exposes a private phase")
            blind_id = str(row["blind_cell_id"])
            if blind_id in assignment_ids:
                raise ReviewContractError("review assignment repeats a blind cell")
            assignment_ids.add(blind_id)
        if not set(grades_by_blind_id).issubset(assignment_ids):
            raise ReviewContractError("one or more bulk grades are not enrolled")
        for grade in grades_by_blind_id.values():
            validate_grade(grade)
        with closing(sqlite3.connect(":memory:")) as connection:
            cls._initialize(connection)
            connection.executemany(
                """
                INSERT INTO assignment(reviewer, blind_cell_id, packet_id, phase, position)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        reviewer,
                        str(row["blind_cell_id"]),
                        str(row["packet_id"]),
                        str(row["phase"]),
                        int(row["position"]),
                    )
                    for row in sorted(rows, key=lambda item: str(item["blind_cell_id"]))
                ],
            )
            connection.executemany(
                """
                INSERT INTO grade(reviewer, blind_cell_id, grade_json, updated_at)
                VALUES (?, ?, ?, 'immutable-snapshot')
                """,
                [
                    (reviewer, blind_id, json.dumps(grade, sort_keys=True))
                    for blind_id, grade in sorted(grades_by_blind_id.items())
                ],
            )
            connection.commit()
            payload = connection.serialize()
        if not payload:
            raise ReviewContractError("review store snapshot is empty")
        return payload

    @classmethod
    def from_snapshot(cls, payload: bytes) -> ReviewStore:
        """Open a validated in-memory view of exact immutable store bytes."""

        if not isinstance(payload, bytes) or not payload:
            raise ReviewContractError("review store snapshot is empty")
        instance = cls.__new__(cls)
        instance.path = None
        instance._snapshot = payload
        try:
            with closing(sqlite3.connect(":memory:")) as connection:
                connection.deserialize(payload)
                if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise ReviewContractError(
                        "review store snapshot failed integrity check"
                    )
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if tables != {"assignment", "grade"}:
                    raise ReviewContractError("review store snapshot schema differs")
        except sqlite3.DatabaseError as exc:
            raise ReviewContractError("review store snapshot is invalid") from exc
        return instance

    def _connection(self) -> sqlite3.Connection:
        if self._snapshot is None:
            if self.path is None:
                raise ReviewContractError("review store path is unavailable")
            return sqlite3.connect(self.path)
        connection = sqlite3.connect(":memory:")
        try:
            connection.deserialize(self._snapshot)
        except sqlite3.DatabaseError:
            connection.close()
            raise
        return connection

    def enroll(
        self,
        reviewer: str,
        assignments: Iterable[dict[str, Any]],
    ) -> dict[str, int]:
        if reviewer not in REVIEWERS:
            raise ReviewContractError(f"unknown reviewer: {reviewer}")
        rows = list(assignments)
        with closing(sqlite3.connect(self.path)) as connection:
            for row in rows:
                expected = {"blind_cell_id", "packet_id", "phase", "position"}
                if set(row) != expected:
                    raise ReviewContractError("review assignment fields differ")
                if row["phase"] not in {"calibration", "review"}:
                    raise ReviewContractError(
                        "review assignment exposes a private phase"
                    )
                connection.execute(
                    """
                    INSERT INTO assignment(reviewer, blind_cell_id, packet_id, phase, position)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(reviewer, blind_cell_id) DO NOTHING
                    """,
                    (
                        reviewer,
                        str(row["blind_cell_id"]),
                        str(row["packet_id"]),
                        str(row["phase"]),
                        int(row["position"]),
                    ),
                )
            connection.commit()
        return self.progress(reviewer)

    def enroll_packet_directory(
        self, public_directory: Path, reviewer: str
    ) -> dict[str, int]:
        manifest_payload = read_external_bytes_snapshot(
            public_directory / "manifest.json", label="review packet manifest"
        )
        try:
            manifest = json.loads(manifest_payload.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReviewContractError("review packet manifest JSON is invalid") from exc
        assignments = verified_review_assignments(public_directory, manifest, reviewer)
        return self.enroll(reviewer, assignments)

    def save(self, reviewer: str, blind_cell_id: str, grade: dict[str, Any]) -> None:
        if reviewer not in REVIEWERS:
            raise ReviewContractError(f"unknown reviewer: {reviewer}")
        validate_grade(grade)
        with closing(sqlite3.connect(self.path)) as connection:
            expected = connection.execute(
                "SELECT 1 FROM assignment WHERE reviewer=? AND blind_cell_id=?",
                (reviewer, blind_cell_id),
            ).fetchone()
            if expected is None:
                raise ReviewContractError(
                    "grade cell is not enrolled in this review store"
                )
            connection.execute(
                """
                INSERT INTO grade(reviewer, blind_cell_id, grade_json)
                VALUES (?, ?, ?)
                ON CONFLICT(reviewer, blind_cell_id)
                DO UPDATE SET grade_json=excluded.grade_json, updated_at=CURRENT_TIMESTAMP
                """,
                (reviewer, blind_cell_id, json.dumps(grade, sort_keys=True)),
            )
            connection.commit()

    def save_many(
        self, reviewer: str, grades_by_blind_id: dict[str, dict[str, Any]]
    ) -> None:
        if reviewer not in REVIEWERS:
            raise ReviewContractError(f"unknown reviewer: {reviewer}")
        for grade in grades_by_blind_id.values():
            validate_grade(grade)
        with closing(sqlite3.connect(self.path)) as connection:
            expected = {
                str(row[0])
                for row in connection.execute(
                    "SELECT blind_cell_id FROM assignment WHERE reviewer=?", (reviewer,)
                )
            }
            unknown = set(grades_by_blind_id).difference(expected)
            if unknown:
                raise ReviewContractError("one or more bulk grades are not enrolled")
            connection.executemany(
                """
                INSERT INTO grade(reviewer, blind_cell_id, grade_json)
                VALUES (?, ?, ?)
                ON CONFLICT(reviewer, blind_cell_id)
                DO UPDATE SET grade_json=excluded.grade_json, updated_at=CURRENT_TIMESTAMP
                """,
                [
                    (reviewer, blind_id, json.dumps(grade, sort_keys=True))
                    for blind_id, grade in grades_by_blind_id.items()
                ],
            )
            connection.commit()

    def load(self, reviewer: str, blind_cell_id: str) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute(
                "SELECT grade_json FROM grade WHERE reviewer=? AND blind_cell_id=?",
                (reviewer, blind_cell_id),
            ).fetchone()
        return None if row is None else json.loads(row[0])

    def count(self, reviewer: str) -> int:
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM grade WHERE reviewer=?", (reviewer,)
            ).fetchone()
        return int(row[0]) if row else 0

    def progress(self, reviewer: str) -> dict[str, int]:
        with closing(sqlite3.connect(self.path)) as connection:
            expected_row = connection.execute(
                "SELECT COUNT(*) FROM assignment WHERE reviewer=?", (reviewer,)
            ).fetchone()
            completed_row = connection.execute(
                "SELECT COUNT(*) FROM grade WHERE reviewer=?", (reviewer,)
            ).fetchone()
        expected = int(expected_row[0]) if expected_row else 0
        completed = int(completed_row[0]) if completed_row else 0
        return {
            "expected": expected,
            "completed": completed,
            "remaining": expected - completed,
        }

    def export_grades(self, reviewer: str) -> dict[str, dict[str, Any]]:
        with closing(self._connection()) as connection:
            rows = connection.execute(
                "SELECT blind_cell_id, grade_json FROM grade WHERE reviewer=?",
                (reviewer,),
            ).fetchall()
        return {str(blind_id): json.loads(grade_json) for blind_id, grade_json in rows}


def aggregate_three_grades(grades: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(grades)
    if len(rows) != 3:
        raise ReviewContractError(
            "aggregation requires exactly three independent grades"
        )
    for row in rows:
        validate_grade(row)
    ordinal_fields = (
        "overall_ordinal",
        "factual_correctness",
        "completeness",
        "citation_support",
        "authority_freshness",
    )
    result = {
        field: int(statistics.median(row[field] for row in rows))
        for field in ordinal_fields
    }
    result["accepted"] = sum(bool(row["accepted"]) for row in rows) >= 2
    abstentions = Counter(str(row["abstention_quality"]) for row in rows)
    abstention_value, abstention_count = abstentions.most_common(1)[0]
    result["abstention_quality"] = (
        abstention_value if abstention_count >= 2 else "no_majority"
    )
    result["individual_grades_preserved"] = True
    return result


def aggregate_panel_grades(
    grades_by_reviewer: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if set(grades_by_reviewer) != set(REVIEWERS):
        raise ReviewContractError(
            "panel aggregation requires GPT, Claude, and Kevin grades"
        )
    aggregate = aggregate_three_grades(
        grades_by_reviewer[reviewer] for reviewer in REVIEWERS
    )
    aggregate["disagreement"] = disagreement_report(grades_by_reviewer)
    aggregate["kevin_grade_present"] = True
    return aggregate


def aggregate_completed_panel(
    stores_by_reviewer: dict[str, ReviewStore],
    *,
    identity_map_path: Path,
    review_manifest: dict[str, Any],
    identity_map_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Join blinded stores and require the same 1,600 canonical cells from every reviewer."""
    if set(stores_by_reviewer) != set(REVIEWERS):
        raise ReviewContractError(
            "completed panel requires GPT, Claude, and Kevin review stores"
        )
    identity_payload = (
        identity_map_bytes
        if identity_map_bytes is not None
        else read_external_bytes_snapshot(identity_map_path, label="blind identity map")
    )
    try:
        identity_map = json.loads(identity_payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewContractError("blind identity map JSON is invalid") from exc
    if hashlib.sha256(identity_payload).hexdigest() != review_manifest.get(
        "identity_map_sha256"
    ):
        raise ReviewContractError("panel identity map differs from the review manifest")
    identities = identity_map.get("identities")
    if not isinstance(identities, list):
        raise ReviewContractError("blind identity map has no identities")
    canonical_by_reviewer: dict[str, dict[str, tuple[str, str, str, str]]] = {}
    grades_by_reviewer = {
        reviewer: store.export_grades(reviewer)
        for reviewer, store in stores_by_reviewer.items()
    }
    for reviewer in REVIEWERS:
        reviewer_rows = [
            row
            for row in identities
            if row.get("reviewer") == reviewer and row.get("phase") == "main"
        ]
        if len(reviewer_rows) != MAIN_CELL_COUNT:
            raise ReviewContractError(
                f"{reviewer}: identity map does not contain 1,600 main cells"
            )
        canonical: dict[str, tuple[str, str, str, str]] = {}
        for row in reviewer_rows:
            cell_id = str(row["canonical_cell_id"])
            if cell_id in canonical:
                raise ReviewContractError(f"{reviewer}: duplicate canonical main cell")
            blind_id = str(row["blind_cell_id"])
            if blind_id not in grades_by_reviewer[reviewer]:
                raise ReviewContractError(f"{reviewer}: missing grade for a main cell")
            canonical[cell_id] = (
                blind_id,
                str(row["task_id"]),
                str(row["candidate_sha256"]),
                str(row.get("task_family", "unspecified")),
            )
        canonical_by_reviewer[reviewer] = canonical
    canonical_sets = [set(canonical_by_reviewer[reviewer]) for reviewer in REVIEWERS]
    if any(cell_ids != canonical_sets[0] for cell_ids in canonical_sets[1:]):
        raise ReviewContractError(
            "reviewers did not grade the same canonical main cells"
        )
    cells: list[dict[str, Any]] = []
    for cell_id in sorted(canonical_sets[0]):
        metadata = {
            canonical_by_reviewer[reviewer][cell_id][1:] for reviewer in REVIEWERS
        }
        if len(metadata) != 1:
            raise ReviewContractError(
                "reviewer identity maps disagree on task or candidate hash"
            )
        task_id, candidate_sha256, task_family = metadata.pop()
        panel = {
            reviewer: grades_by_reviewer[reviewer][
                canonical_by_reviewer[reviewer][cell_id][0]
            ]
            for reviewer in REVIEWERS
        }
        cells.append(
            {
                "canonical_cell_id": cell_id,
                "task_id": task_id,
                "task_family": task_family,
                "candidate_sha256": candidate_sha256,
                "individual_grades": panel,
                "aggregate": aggregate_panel_grades(panel),
            }
        )
    if len(cells) != MAIN_CELL_COUNT:
        raise ReviewContractError(
            "panel aggregate does not contain exactly 1,600 cells"
        )
    return {
        "schema_version": "contextlab.panel-aggregate.v1",
        "reviewers": list(REVIEWERS),
        "cell_count": len(cells),
        "kevin_grade_required": True,
        "cells": cells,
    }


def disagreement_report(
    grades_by_reviewer: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if set(grades_by_reviewer) != set(REVIEWERS):
        raise ReviewContractError("disagreement reporting requires all three reviewers")
    for grade in grades_by_reviewer.values():
        validate_grade(grade)
    kevin = grades_by_reviewer["kevin"]
    scores = [int(grade["overall_ordinal"]) for grade in grades_by_reviewer.values()]
    return {
        "overall_ordinal_range": max(scores) - min(scores),
        "accepted_unanimous": len(
            {bool(grade["accepted"]) for grade in grades_by_reviewer.values()}
        )
        == 1,
        "gpt_minus_kevin": int(
            grades_by_reviewer["gpt-5.6-sol-high"]["overall_ordinal"]
        )
        - int(kevin["overall_ordinal"]),
        "claude_minus_kevin": int(
            grades_by_reviewer["claude-opus-5-medium"]["overall_ordinal"]
        )
        - int(kevin["overall_ordinal"]),
    }


def hidden_repeat_consistency(
    reviewer: str,
    grades_by_blind_id: dict[str, dict[str, Any]],
    identity_map: dict[str, Any],
) -> dict[str, Any]:
    if reviewer not in REVIEWERS:
        raise ReviewContractError(f"unknown reviewer: {reviewer}")
    pairs: dict[str, dict[str, str]] = {}
    for identity in identity_map.get("identities", []):
        if identity.get("reviewer") != reviewer:
            continue
        phase = str(identity.get("phase"))
        if phase not in {"main", "hidden_repeat"}:
            continue
        canonical = str(identity["canonical_cell_id"])
        pairs.setdefault(canonical, {})[phase] = str(identity["blind_cell_id"])
    repeat_pairs = [pair for pair in pairs.values() if "hidden_repeat" in pair]
    completed = 0
    exact_scores = 0
    accepted_matches = 0
    absolute_differences = 0
    for pair in repeat_pairs:
        main_grade = grades_by_blind_id.get(pair.get("main", ""))
        repeat_grade = grades_by_blind_id.get(pair["hidden_repeat"])
        if main_grade is None or repeat_grade is None:
            continue
        validate_grade(main_grade)
        validate_grade(repeat_grade)
        completed += 1
        difference = abs(
            int(main_grade["overall_ordinal"]) - int(repeat_grade["overall_ordinal"])
        )
        absolute_differences += difference
        exact_scores += difference == 0
        accepted_matches += main_grade["accepted"] == repeat_grade["accepted"]
    return {
        "expected_pairs": HIDDEN_REPEAT_COUNT,
        "completed_pairs": completed,
        "missing_pairs": HIDDEN_REPEAT_COUNT - completed,
        "exact_ordinal_matches": exact_scores,
        "accepted_matches": accepted_matches,
        "mean_absolute_ordinal_difference": None
        if completed == 0
        else absolute_differences / completed,
    }


def pinned_reviewer_token_profile(reviewer: str) -> dict[str, str]:
    """Return the immutable model/tokenizer identity used for packet preflight."""

    profile = PINNED_REVIEW_TOKEN_PROFILES.get(reviewer)
    if profile is None:
        raise ReviewContractError(
            "packet token verification is available only for the two AI reviewers"
        )
    return dict(profile)


def count_pinned_packet_tokens(packet_bytes: bytes, reviewer: str) -> int:
    """Count one exact packet with the pinned dependency-free verifier."""

    pinned_reviewer_token_profile(reviewer)
    if not isinstance(packet_bytes, bytes) or not packet_bytes:
        raise ReviewContractError("packet token verification requires non-empty bytes")
    try:
        text = packet_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ReviewContractError("review packet is not strict UTF-8") from exc
    count = len(_PACKET_TOKEN_PATTERN.findall(text))
    if count <= 0 or count > len(packet_bytes):
        raise ReviewContractError("verified packet token count is impossible")
    return count


def verify_packet_token_preflight(
    manifest: Mapping[str, Any],
    reviewer: str,
    packet_payloads: Mapping[str, bytes],
    *,
    confirmed_by: str | None = None,
) -> dict[str, Any]:
    """Verify exact packet bytes and derive their pinned deterministic counts.

    The helper accepts both the final-review packet manifest (``sha256``) and the
    G3 calibration packet manifest (``packet_sha256``).  Counts are never accepted
    from a caller: they are recomputed from the hash-bound bytes.
    """

    profile = pinned_reviewer_token_profile(reviewer)
    packets = manifest.get("packets")
    if not isinstance(packets, list):
        raise ReviewContractError("token preflight manifest has no packet records")
    expected: dict[str, Mapping[str, Any]] = {}
    for row in packets:
        if not isinstance(row, Mapping) or row.get("reviewer") != reviewer:
            continue
        packet_id = row.get("packet_id")
        if not isinstance(packet_id, str) or not packet_id or packet_id in expected:
            raise ReviewContractError("token preflight packet identity is invalid")
        expected[packet_id] = row
    if not expected or set(packet_payloads) != set(expected):
        raise ReviewContractError(
            "token preflight bytes must cover every reviewer packet exactly once"
        )
    counts: dict[str, int] = {}
    verified_sizes: dict[str, int] = {}
    for packet_id, record in expected.items():
        payload = packet_payloads[packet_id]
        if not isinstance(payload, bytes):
            raise ReviewContractError("token preflight packet payload must be bytes")
        expected_sha = record.get("sha256", record.get("packet_sha256"))
        if (
            not isinstance(expected_sha, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None
            or not hmac.compare_digest(
                hashlib.sha256(payload).hexdigest(), expected_sha
            )
        ):
            raise ReviewContractError(
                "token preflight packet bytes differ from manifest"
            )
        expected_size = record.get("utf8_bytes")
        if expected_size is not None and (
            isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size != len(payload)
        ):
            raise ReviewContractError("token preflight packet byte count differs")
        counts[packet_id] = count_pinned_packet_tokens(payload, reviewer)
        verified_sizes[packet_id] = len(payload)
    manifest_sha = manifest.get("manifest_sha256", manifest.get("artifact_sha256"))
    if (
        not isinstance(manifest_sha, str)
        or re.fullmatch(r"[0-9a-f]{64}", manifest_sha) is None
    ):
        raise ReviewContractError("token preflight manifest hash is invalid")
    verified_manifest = dict(manifest)
    verified_manifest["packets"] = [
        {
            **dict(row),
            **(
                {"utf8_bytes": verified_sizes[str(row["packet_id"])]}
                if row.get("reviewer") == reviewer and "utf8_bytes" not in row
                else {}
            ),
        }
        for row in packets
    ]
    return validate_token_preflight(
        verified_manifest,
        reviewer,
        counts,
        tokenizer_id=profile["tokenizer_id"],
        confirmed_by=confirmed_by,
        review_manifest_sha256=manifest_sha,
    )


def validate_token_preflight(
    manifest: dict[str, Any],
    reviewer: str,
    packet_token_counts: dict[str, int],
    *,
    tokenizer_id: str,
    confirmed_by: str | None = None,
    review_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    if reviewer not in REVIEWERS:
        raise ReviewContractError(f"unknown reviewer: {reviewer}")
    expected_ids = {
        str(row["packet_id"])
        for row in manifest["packets"]
        if row["reviewer"] == reviewer
    }
    if set(packet_token_counts) != expected_ids:
        raise ReviewContractError(
            "token preflight must cover every reviewer packet exactly once"
        )
    reviewer_packets = [
        row for row in manifest["packets"] if row["reviewer"] == reviewer
    ]
    bytes_by_packet = {
        str(row["packet_id"]): row.get("utf8_bytes") for row in reviewer_packets
    }
    if not tokenizer_id.strip() or any(
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or (
            isinstance(bytes_by_packet.get(packet_id), int)
            and value > int(bytes_by_packet[packet_id])
        )
        for packet_id, value in packet_token_counts.items()
    ):
        raise ReviewContractError(
            "token preflight requires a named tokenizer and possible positive counts"
        )
    manifest_sha = review_manifest_sha256 or manifest.get("manifest_sha256")
    if (
        not isinstance(manifest_sha, str)
        or re.fullmatch(r"[0-9a-f]{64}", manifest_sha) is None
    ):
        raise ReviewContractError("token preflight manifest hash is invalid")
    record = {
        "schema_version": TOKEN_PREFLIGHT_SCHEMA,
        "reviewer": reviewer,
        "review_manifest_sha256": manifest_sha,
        "tokenizer_id": tokenizer_id,
        "packet_count": len(expected_ids),
        "utf8_bytes_total": sum(int(row["utf8_bytes"]) for row in reviewer_packets),
        "token_total": sum(packet_token_counts.values()),
        "packet_token_counts": dict(sorted(packet_token_counts.items())),
        "packet_token_counts_sha256": hashlib.sha256(
            _canonical_json(packet_token_counts)
        ).hexdigest(),
        "confirmed_before_review": confirmed_by == "Kevin Araujo",
        "confirmed_by": confirmed_by,
    }
    if confirmed_by is not None and confirmed_by != "Kevin Araujo":
        raise ReviewContractError("only Kevin can confirm review subscription usage")
    return record
