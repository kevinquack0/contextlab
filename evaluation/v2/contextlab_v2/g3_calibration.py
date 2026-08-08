"""Provider-free, blind three-member calibration workflow for G3."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any

from .baseline import repository_root
from .g3_execution import validate_prepared_public_g3_cell
from .g3_freeze import validate_g3_freeze
from .g3_gate import _validate_public_run
from .g3_panel import (
    AI_REVIEWERS,
    G3_CALIBRATION_AI_INVOCATIONS,
    G3_PANEL_HIDDEN_REPEAT_COUNT,
    build_g3_panel_calibration as _build_g3_panel_calibration,
    validate_g3_panel_calibration,
)
from .generations import validate_saved_generation_result
from .immutable_io import ImmutableIOError, read_bytes_snapshot
from .memory_experiments import (
    MEMORY_CONFIGURATIONS,
    validate_memory_result_receipt,
)
from .provider import ALLOWED_REASONING_EFFORTS
from .review import (
    PACKET_TOKEN_VERIFIER_ID,
    PACKET_TOKEN_VERIFIER_SHA256,
    PINNED_REVIEW_TOKEN_PROFILES,
    REVIEWERS,
    REVIEW_RUBRIC_SHA256,
    RUBRIC_VERSION,
    ReviewContractError,
    validate_grade,
    validate_review_protocol,
    verify_packet_token_preflight,
)
from .review_invocations import (
    AIReviewInvocationError,
    assert_native_proof_fields,
    native_proof_fields,
    run_and_record_ai_review,
    validate_recorded_ai_review,
)
from .tasking import sha256_json, validate_split_manifest


G3_CALIBRATION_MANIFEST_SCHEMA = "contextlab.g3-calibration-manifest.v2"
G3_CALIBRATION_PACKET_SCHEMA = "contextlab.g3-calibration-packet.v1"
G3_CALIBRATION_IDENTITY_MAP_SCHEMA = "contextlab.g3-calibration-identity-map.v1"
G3_CALIBRATION_REFERENCE_SCHEMA = "contextlab.g3-calibration-reference.v1"
G3_CALIBRATION_RETURN_SCHEMA = "contextlab.g3-calibration-review-return.v1"
G3_CALIBRATION_INVOCATION_RECEIPT_SCHEMA = "contextlab.review-invocation-receipt.v3"
G3_CALIBRATION_TOKEN_PREFLIGHT_SCHEMA = "contextlab.g3-calibration-token-preflight.v1"
G3_CALIBRATION_TOKEN_CONFIRMATION_SCHEMA = (
    "contextlab.g3-calibration-token-confirmation.v1"
)
G3_REFERENCE_MAPPING_VERSION = "contextlab.g3-objective-reference-map.v2"
G3_CALIBRATION_PARTITION = "judge_calibration"
G3_CALIBRATION_SELECTION_ALGORITHM = "hmac-sha256-balanced-configuration-v1"
G3_CALIBRATION_CELLS_PER_CONFIGURATION = 2
G3_CALIBRATION_UNIQUE_CELL_COUNT = 20
G3_CALIBRATION_HIDDEN_REPEAT_COUNT = G3_PANEL_HIDDEN_REPEAT_COUNT
G3_CALIBRATION_PACKET_CELL_COUNT = 22

# The mapping is frozen and deliberately uses only fields from the objective receipt.
# A correct but unsupported answer is not accepted by the calibration reference.
G3_OBJECTIVE_REFERENCE_MAPPING: dict[str, dict[str, object]] = {
    "completed|false|false": {"overall_ordinal": 0, "accepted": False},
    "completed|false|true": {"overall_ordinal": 1, "accepted": False},
    "completed|true|false": {"overall_ordinal": 1, "accepted": False},
    "completed|true|true": {"overall_ordinal": 3, "accepted": True},
    "failed|false|false": {"overall_ordinal": 0, "accepted": False},
}

_SHA256 = re.compile(r"[0-9a-f]{64}")
_BLIND_ID = re.compile(r"G3B-[0-9a-f]{24}")
_PACKET_ID = re.compile(r"G3P-[0-9a-f]{24}")
_UTC_SECOND = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_TASK_ID_LEAK = re.compile(
    r"(?<![A-Za-z0-9])(?:S|T|C)\d{3}(?![A-Za-z0-9])", re.IGNORECASE
)
_POLICY_LEAK = re.compile(r"(?<![A-Za-z0-9])M[0-4](?![A-Za-z0-9])", re.IGNORECASE)
_EFFORT_LEAK = re.compile(
    r"reasoning[ _-]?effort\s*(?:[:=]|is)?\s*(?:low|high)", re.IGNORECASE
)
_IDENTITY_FIELD_LEAK = re.compile(
    r"(?:run_id|task_id|strategy_id|reasoning_effort|provider_route)",
    re.IGNORECASE,
)

_PACKET_CELL_FIELDS = frozenset(
    {
        "blind_cell_id",
        "question",
        "frozen_answer",
        "supporting_rendered_evidence",
        "rubric",
    }
)
_PACKET_FIELDS = frozenset(
    {
        "schema_version",
        "packet_id",
        "reviewer",
        "rubric_version",
        "rubric_sha256",
        "cell_count",
        "cells",
    }
)
_PACKET_RECORD_FIELDS = frozenset(
    {
        "reviewer",
        "packet_id",
        "path",
        "packet_sha256",
        "response_template_path",
        "response_template_sha256",
        "cell_count",
    }
)
_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "g3_freeze_sha256",
        "frozen_manifest_sha256",
        "public_run_sha256",
        "split_manifest_sha256",
        "review_protocol_file_sha256",
        "review_protocol_canonical_sha256",
        "rubric_sha256",
        "rubric_version",
        "reviewers",
        "calibration_partition",
        "selection_algorithm",
        "selection_seed_sha256",
        "reference_mapping_version",
        "reference_mapping_sha256",
        "configuration_cell_counts",
        "unique_cell_count",
        "hidden_repeat_count_per_reviewer",
        "packet_cell_count",
        "packet_count",
        "selection_sha256",
        "identity_map_sha256",
        "reference_sha256",
        "packets",
        "artifact_sha256",
    }
)
_IDENTITY_FIELDS = frozenset(
    {
        "reviewer",
        "packet_id",
        "position",
        "blind_cell_id",
        "canonical_cell_id",
        "occurrence",
        "run_id",
        "task_id",
        "suite",
        "policy",
        "reasoning_effort",
        "source_cell_sha256",
        "prepared_cell_sha256",
        "generation_result_sha256",
        "receipt_sha256",
    }
)
_IDENTITY_MAP_FIELDS = frozenset(
    {
        "schema_version",
        "g3_freeze_sha256",
        "public_run_sha256",
        "split_manifest_sha256",
        "selection_algorithm",
        "selection_seed_sha256",
        "selection_sha256",
        "reference_mapping_sha256",
        "identities",
        "artifact_sha256",
    }
)
_REFERENCE_TARGET_FIELDS = frozenset(
    {
        "canonical_cell_id",
        "source_cell_sha256",
        "receipt_sha256",
        "status",
        "is_correct",
        "provenance_complete",
        "overall_ordinal",
        "accepted",
    }
)
_REFERENCE_FIELDS = frozenset(
    {
        "schema_version",
        "g3_freeze_sha256",
        "public_run_sha256",
        "selection_sha256",
        "reference_mapping_version",
        "reference_mapping",
        "reference_mapping_sha256",
        "targets",
        "artifact_sha256",
    }
)
_RETURN_GRADE_FIELDS = frozenset({"blind_cell_id", "grade"})
_RETURN_FIELDS = frozenset(
    {
        "schema_version",
        "reviewer",
        "packet_id",
        "packet_sha256",
        "review_manifest_sha256",
        "identity_map_sha256",
        "grades",
        "rubric_ambiguous",
        "review_comment",
        "artifact_sha256",
    }
)


class G3CalibrationError(ValueError):
    """The G3 calibration bundle is incomplete, unblinded, or stale."""


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise G3CalibrationError(f"cannot read calibration artifact: {path}") from exc


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G3CalibrationError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise G3CalibrationError(f"{label} must be a JSON object")
    return value


def _lexical_repository_root(root: Path) -> Path:
    repository = Path(os.path.abspath(root))
    if repository.is_symlink() or not repository.is_dir():
        raise G3CalibrationError("calibration repository root is missing or unsafe")
    return repository


def _repository_write_relative(
    repository: Path, path: Path, label: str
) -> tuple[Path, Path]:
    requested = path if path.is_absolute() else repository / path
    absolute = Path(os.path.abspath(requested))
    try:
        relative = absolute.relative_to(repository)
    except ValueError as exc:
        raise G3CalibrationError(f"{label} escaped the repository") from exc
    if (
        len(relative.parts) < 3
        or relative.parts[:2] != ("results", "v2")
        or ".." in relative.parts
        or not relative.name
    ):
        raise G3CalibrationError(f"{label} must stay under results/v2")
    return absolute, relative


def _private_write_target(
    path: Path, repository: Path, label: str
) -> tuple[Path, Path, Path]:
    """Bind an external file to its closest existing approved parent."""

    absolute = Path(os.path.abspath(path))
    try:
        absolute.relative_to(repository)
    except ValueError:
        pass
    else:
        raise G3CalibrationError(f"{label} must be outside the repository")
    if absolute.is_symlink():
        raise G3CalibrationError(f"{label} target is unsafe")

    missing: list[str] = []
    anchor = absolute.parent
    while True:
        if anchor.is_symlink():
            raise G3CalibrationError(f"{label} parent is unsafe")
        try:
            metadata = anchor.stat()
        except FileNotFoundError:
            if anchor == anchor.parent:
                raise G3CalibrationError(f"{label} parent is missing")
            missing.append(anchor.name)
            anchor = anchor.parent
            continue
        except OSError as exc:
            raise G3CalibrationError(f"cannot inspect {label} parent") from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise G3CalibrationError(f"{label} parent is unsafe")
        break
    return anchor, Path(*reversed(missing), absolute.name), absolute


def _open_write_parent(
    anchor: Path, relative: Path, *, create: bool, label: str
) -> tuple[int, str]:
    if relative.is_absolute() or ".." in relative.parts or not relative.name:
        raise G3CalibrationError(f"{label} path is unsafe")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(anchor, flags)
    except OSError as exc:
        raise G3CalibrationError(f"cannot open {label} root safely") from exc
    try:
        for component in relative.parts[:-1]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o755, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except OSError as exc:
        os.close(descriptor)
        raise G3CalibrationError(f"{label} contains an unsafe parent") from exc
    return descriptor, relative.name


def _read_existing_at(parent: int, name: str, label: str) -> bytes | None:
    try:
        metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise G3CalibrationError(f"{label} target is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent)
    except OSError as exc:
        raise G3CalibrationError(f"cannot read {label}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise G3CalibrationError(f"{label} target is unsafe")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            data = handle.read()
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise G3CalibrationError(f"{label} changed while it was read")
        return data
    except OSError as exc:
        raise G3CalibrationError(f"cannot read {label}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_private_json_snapshot(
    path: Path, repository: Path, label: str
) -> tuple[dict[str, Any], str]:
    """Read one external JSON file through a no-follow descriptor chain."""

    anchor, relative, _absolute = _private_write_target(path, repository, label)
    parent, name = _open_write_parent(anchor, relative, create=False, label=label)
    try:
        data = _read_existing_at(parent, name, label)
    finally:
        os.close(parent)
    if data is None:
        raise G3CalibrationError(f"cannot read {label}: {path}")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G3CalibrationError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise G3CalibrationError(f"{label} must be a JSON object")
    return value, hashlib.sha256(data).hexdigest()


def _unlink_same_inode(parent: int, name: str, expected: os.stat_result) -> None:
    try:
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino):
            os.unlink(name, dir_fd=parent)
    except OSError:
        pass


_CreatedCalibrationArtifact = tuple[Path, Path, int, int]


def _write_once_or_verify(
    anchor: Path, relative: Path, payload: bytes, label: str
) -> _CreatedCalibrationArtifact | None:
    """Create one immutable artifact, or verify an identical regular file."""

    parent, name = _open_write_parent(anchor, relative, create=True, label=label)
    temporary = f".{name}.{secrets.token_hex(12)}.tmp"
    temporary_created = False
    destination_created = False
    temporary_stat: os.stat_result | None = None
    try:
        current = _read_existing_at(parent, name, f"existing {label}")
        if current is not None:
            if current != payload:
                raise G3CalibrationError(f"existing {label} changed")
            return None
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor = os.open(temporary, flags, 0o600, dir_fd=parent)
        temporary_created = True
        temporary_stat = os.fstat(descriptor)
        if not stat.S_ISREG(temporary_stat.st_mode):
            os.close(descriptor)
            raise G3CalibrationError(f"{label} temporary target is unsafe")
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        linked_stat = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
        if (linked_stat.st_dev, linked_stat.st_ino) != (
            temporary_stat.st_dev,
            temporary_stat.st_ino,
        ):
            raise G3CalibrationError(f"{label} temporary target changed")
        try:
            os.link(
                temporary,
                name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
                follow_symlinks=False,
            )
            destination_created = True
            published = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (published.st_dev, published.st_ino) != (
                temporary_stat.st_dev,
                temporary_stat.st_ino,
            ):
                raise G3CalibrationError(f"{label} changed during publication")
        except FileExistsError:
            concurrent = _read_existing_at(parent, name, f"concurrent {label}")
            if concurrent != payload:
                raise G3CalibrationError(f"concurrent {label} changed")
        _unlink_same_inode(parent, temporary, temporary_stat)
        temporary_created = False
        os.fsync(parent)
        if not destination_created:
            return None
        return (
            anchor,
            relative,
            temporary_stat.st_dev,
            temporary_stat.st_ino,
        )
    except Exception:
        if destination_created and temporary_stat is not None:
            _unlink_same_inode(parent, name, temporary_stat)
            try:
                os.fsync(parent)
            except OSError:
                pass
        raise
    finally:
        if temporary_created and temporary_stat is not None:
            _unlink_same_inode(parent, temporary, temporary_stat)
        os.close(parent)


def _rollback_created_artifacts(
    created: Iterable[_CreatedCalibrationArtifact],
) -> None:
    for anchor, relative, device, inode in reversed(list(created)):
        try:
            parent, name = _open_write_parent(
                anchor, relative, create=False, label="calibration rollback"
            )
        except G3CalibrationError:
            continue
        try:
            try:
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except OSError:
                continue
            if (current.st_dev, current.st_ino) == (device, inode):
                try:
                    os.unlink(name, dir_fd=parent)
                    os.fsync(parent)
                except OSError:
                    pass
        finally:
            os.close(parent)


def _contained_path(directory: Path, relative_value: object, label: str) -> Path:
    if not isinstance(relative_value, str) or not relative_value:
        raise G3CalibrationError(f"{label} must be a contained relative path")
    relative = Path(relative_value)
    if relative.is_absolute() or ".." in relative.parts:
        raise G3CalibrationError(f"{label} escapes its bundle root")
    root = directory.resolve()
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise G3CalibrationError(f"{label} escapes its bundle root") from exc
    return resolved


def _outside_root(path: Path, root: Path, label: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return resolved
    raise G3CalibrationError(f"{label} must be outside the repository")


def _separate_private_path(path: Path, public_directory: Path, label: str) -> None:
    try:
        path.resolve().relative_to(public_directory.resolve())
    except ValueError:
        return
    raise G3CalibrationError(f"{label} must be outside the public packet bundle")


def _seed_bytes(seed: object) -> bytes:
    if not isinstance(seed, (bytes, bytearray)) or len(seed) < 32:
        raise G3CalibrationError(
            "calibration randomization seed must contain at least 32 bytes"
        )
    return bytes(seed)


def _digest(seed: bytes, *parts: str) -> str:
    message = "\0".join(parts).encode("utf-8")
    return hmac.new(seed, message, hashlib.sha256).hexdigest()


def _configuration_keys() -> tuple[str, ...]:
    return tuple(
        f"{policy}:{effort}"
        for policy in MEMORY_CONFIGURATIONS
        for effort in ALLOWED_REASONING_EFFORTS
    )


def _reference_mapping() -> dict[str, dict[str, object]]:
    return _json_clone(G3_OBJECTIVE_REFERENCE_MAPPING)


def _reference_mapping_sha256() -> str:
    return sha256_json(
        {
            "version": G3_REFERENCE_MAPPING_VERSION,
            "mapping": _reference_mapping(),
        }
    )


def derive_g3_objective_reference_grade(
    receipt: Mapping[str, Any],
) -> dict[str, object]:
    """Map an objective receipt to the frozen two-field panel reference grade."""

    if not isinstance(receipt, Mapping):
        raise G3CalibrationError("objective reference requires a receipt object")
    status = receipt.get("status")
    correct = receipt.get("is_correct")
    provenance = receipt.get("provenance_complete")
    if (
        status not in {"completed", "failed"}
        or not isinstance(correct, bool)
        or not isinstance(provenance, bool)
    ):
        raise G3CalibrationError(
            "objective reference requires status, is_correct, and provenance_complete"
        )
    if status == "failed" and (correct or provenance):
        raise G3CalibrationError("failed receipt fabricates an objective outcome")
    key = f"{status}|{str(correct).lower()}|{str(provenance).lower()}"
    target = _reference_mapping().get(key)
    if target is None:
        raise G3CalibrationError("objective receipt has no frozen reference mapping")
    return target


def _canonical_split(root: Path) -> dict[str, Any]:
    path = root / "results/v2/splits/task_split_manifest.json"
    value = _read_json(path, "canonical split manifest")
    try:
        validate_split_manifest(value)
    except Exception as exc:
        raise G3CalibrationError(f"canonical split manifest is invalid: {exc}") from exc
    if not _is_sha256(value.get("manifest_sha256")):
        raise G3CalibrationError("canonical split manifest hash is invalid")
    return value


def _canonical_review_protocol(root: Path) -> tuple[dict[str, Any], str]:
    path = root / "evaluation/v2/review_protocol.json"
    try:
        raw = read_bytes_snapshot(root, path)
        value = json.loads(raw.decode("utf-8"))
    except (
        ImmutableIOError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise G3CalibrationError("cannot read canonical review protocol") from exc
    if not isinstance(value, dict):
        raise G3CalibrationError("canonical review protocol must be a JSON object")
    try:
        validate_review_protocol(path)
    except Exception as exc:
        raise G3CalibrationError(
            f"canonical review protocol is invalid: {exc}"
        ) from exc
    try:
        if read_bytes_snapshot(root, path) != raw:
            raise G3CalibrationError("canonical review protocol changed while read")
    except ImmutableIOError as exc:
        raise G3CalibrationError("cannot verify canonical review protocol") from exc
    rubric = value.get("rubric")
    if not isinstance(rubric, Mapping) or rubric.get("version") != RUBRIC_VERSION:
        raise G3CalibrationError("canonical review rubric changed")
    return value, hashlib.sha256(raw).hexdigest()


def _selected_specs(
    freeze: Mapping[str, Any],
    split_manifest: Mapping[str, Any],
    seed: bytes,
) -> list[dict[str, Any]]:
    manifest = freeze.get("manifest")
    specs = manifest.get("run_specs") if isinstance(manifest, Mapping) else None
    split_rows = split_manifest.get("tasks")
    if not isinstance(specs, list) or not isinstance(split_rows, list):
        raise G3CalibrationError("freeze or split manifest has no task surface")
    split_by_id: dict[str, Mapping[str, Any]] = {}
    for row in split_rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("task_id"), str):
            raise G3CalibrationError("split manifest contains an invalid task")
        task_id = str(row["task_id"])
        if task_id in split_by_id:
            raise G3CalibrationError("split manifest repeats a task identity")
        split_by_id[task_id] = row

    task_views: dict[str, Mapping[str, Any]] = {}
    candidates = {key: [] for key in _configuration_keys()}
    seen_runs: set[str] = set()
    for raw_spec in specs:
        if not isinstance(raw_spec, Mapping):
            raise G3CalibrationError("frozen G3 run spec is invalid")
        spec = dict(raw_spec)
        run_id = spec.get("run_id")
        policy = spec.get("policy")
        effort = spec.get("reasoning_effort")
        task = spec.get("task")
        if (
            not isinstance(run_id, str)
            or run_id in seen_runs
            or policy not in MEMORY_CONFIGURATIONS
            or effort not in ALLOWED_REASONING_EFFORTS
            or not isinstance(task, Mapping)
            or not isinstance(task.get("task_id"), str)
        ):
            raise G3CalibrationError("frozen G3 run identity is invalid")
        seen_runs.add(run_id)
        task_id = str(task["task_id"])
        split_row = split_by_id.get(task_id)
        if split_row is None or split_row.get("suite") != task.get("suite"):
            raise G3CalibrationError("frozen task is absent from the canonical split")
        split_question_sha256 = split_row.get("question_sha256")
        if split_question_sha256 is not None and split_question_sha256 != task.get(
            "question_sha256"
        ):
            raise G3CalibrationError("frozen task question differs from the split")
        previous = task_views.setdefault(task_id, task)
        if dict(previous) != dict(task):
            raise G3CalibrationError("frozen task changes between configurations")
        if split_row.get("partition") == G3_CALIBRATION_PARTITION:
            candidates[f"{policy}:{effort}"].append(spec)

    public_split_ids = {
        task_id
        for task_id, row in split_by_id.items()
        if row.get("partition") != "sealed_capability"
    }
    if set(task_views) != public_split_ids:
        raise G3CalibrationError("freeze does not match the public split task surface")

    selected: list[dict[str, Any]] = []
    for configuration in _configuration_keys():
        rows = candidates[configuration]
        if len(rows) != 32:
            raise G3CalibrationError(
                f"{configuration} does not contain all 32 judge_calibration cells"
            )
        ordered = sorted(
            rows,
            key=lambda row: (
                _digest(seed, "select", configuration, str(row["run_id"])),
                str(row["run_id"]),
            ),
        )
        selected.extend(ordered[:G3_CALIBRATION_CELLS_PER_CONFIGURATION])
    if (
        len(selected) != G3_CALIBRATION_UNIQUE_CELL_COUNT
        or len({str(row["run_id"]) for row in selected})
        != G3_CALIBRATION_UNIQUE_CELL_COUNT
    ):
        raise G3CalibrationError("calibration selection is not exactly 20 unique cells")
    return selected


def select_g3_calibration_cells(
    freeze: Mapping[str, Any],
    split_manifest: Mapping[str, Any],
    *,
    seed: bytes,
) -> list[dict[str, Any]]:
    """Select two frozen judge-calibration cells from every policy/effort lane."""

    seed_value = _seed_bytes(seed)
    try:
        validate_g3_freeze(freeze)
        validate_split_manifest(dict(split_manifest))
    except Exception as exc:
        raise G3CalibrationError(
            f"cannot select from an invalid freeze or split: {exc}"
        ) from exc
    return _selected_specs(freeze, split_manifest, seed_value)


def _canonical_paths(
    root: Path, manifest: Mapping[str, Any], spec: Mapping[str, Any]
) -> dict[str, Path]:
    task = spec["task"]
    campaign = str(manifest["campaign_id"])
    policy = str(spec["policy"])
    effort = str(spec["reasoning_effort"])
    task_id = str(task["task_id"])
    return {
        "prepared": root
        / "results/v2/memory/prepared"
        / campaign
        / policy
        / effort
        / f"{task_id}.json",
        "generation": root
        / "results/v2/generations/public"
        / campaign
        / policy
        / effort
        / f"{task_id}.json",
        "receipt": root
        / "results/v2/memory/receipts"
        / campaign
        / policy
        / effort
        / f"{task_id}.json",
    }


def _repo_json(path: Path, root: Path, label: str) -> dict[str, Any]:
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise G3CalibrationError(f"{label} path escaped the repository") from exc
    return _read_json(resolved, label)


def _visible_text_is_blind(
    question: object,
    answer: object,
    evidence: object,
    *,
    run_id: str | None = None,
    task_id: str | None = None,
) -> None:
    if any(
        not isinstance(value, str) or not value.strip()
        for value in (question, answer, evidence)
    ):
        raise G3CalibrationError(
            "reviewer-visible question, answer, and rendered evidence must be text"
        )
    visible = json.dumps(
        {"question": question, "answer": answer, "evidence": evidence},
        ensure_ascii=False,
        sort_keys=True,
    )
    folded = visible.casefold()
    exact_leaks = [value for value in (run_id, task_id) if value]
    if (
        any(str(value).casefold() in folded for value in exact_leaks)
        or _TASK_ID_LEAK.search(visible)
        or _POLICY_LEAK.search(visible)
        or _EFFORT_LEAK.search(visible)
        or _IDENTITY_FIELD_LEAK.search(visible)
    ):
        raise G3CalibrationError(
            "reviewer-visible calibration content exposes a task, strategy, or effort identity"
        )


def _run_index(
    public_run: Mapping[str, Any], freeze: Mapping[str, Any]
) -> dict[str, Mapping[str, Any]]:
    try:
        return _validate_public_run(public_run, freeze=freeze)
    except Exception as exc:
        raise G3CalibrationError(f"canonical public G3 run is invalid: {exc}") from exc


def _source_rows(
    *,
    root: Path,
    freeze: Mapping[str, Any],
    public_run: Mapping[str, Any],
    selected_specs: Iterable[Mapping[str, Any]],
    seed: bytes,
) -> list[dict[str, Any]]:
    manifest = freeze["manifest"]
    indexed = _run_index(public_run, freeze)
    trusted = str(manifest["frozen_manifest_sha256"])
    rows: list[dict[str, Any]] = []
    for spec in selected_specs:
        run_id = str(spec["run_id"])
        task = spec["task"]
        task_id = str(task["task_id"])
        run_cell = indexed.get(run_id)
        if (
            not isinstance(run_cell, Mapping)
            or run_cell.get("generation_status") != "completed"
            or run_cell.get("grade_status") != "objective_completed"
        ):
            raise G3CalibrationError(
                f"selected calibration cell is not completed: {run_id}"
            )
        paths = _canonical_paths(root, manifest, spec)
        expected_relative = {
            "prepared_path": paths["prepared"].relative_to(root).as_posix(),
            "generation_path": paths["generation"].relative_to(root).as_posix(),
            "receipt_path": paths["receipt"].relative_to(root).as_posix(),
        }
        if any(
            run_cell.get(field) != value for field, value in expected_relative.items()
        ):
            raise G3CalibrationError(
                f"selected artifact path is not canonical: {run_id}"
            )

        prepared = _repo_json(paths["prepared"], root, "prepared calibration cell")
        generation = _repo_json(paths["generation"], root, "calibration generation")
        receipt = _repo_json(paths["receipt"], root, "calibration receipt")
        try:
            validate_prepared_public_g3_cell(prepared, root=root)
            validate_saved_generation_result(
                generation,
                expected_run_id=run_id,
                expected_task_id=task_id,
                expected_effort=str(spec["reasoning_effort"]),
            )
            validate_memory_result_receipt(receipt, spec, manifest, trusted)
        except Exception as exc:
            raise G3CalibrationError(
                f"selected calibration artifact is invalid: {run_id}: {exc}"
            ) from exc

        generation_sha256 = sha256_json(generation)
        if (
            prepared.get("artifact_sha256") != run_cell.get("prepared_cell_sha256")
            or prepared.get("run_spec") != spec
            or prepared.get("run_spec_sha256") != spec.get("run_spec_sha256")
            or generation_sha256 != run_cell.get("generation_artifact_sha256")
            or generation_sha256 != run_cell.get("generation_result_sha256")
            or receipt.get("result_sha256") != run_cell.get("receipt_sha256")
            or receipt.get("run_id") != run_id
            or receipt.get("status") != "completed"
            or receipt.get("prepared_cell_artifact_sha256")
            != prepared.get("artifact_sha256")
            or receipt.get("generation_result_sha256") != generation_sha256
            or receipt.get("answer") != generation.get("answer")
        ):
            raise G3CalibrationError(
                f"selected calibration commitments changed: {run_id}"
            )
        rendered = prepared.get("rendered_context")
        if (
            not isinstance(rendered, str)
            or prepared.get("rendered_context_sha256")
            != hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        ):
            raise G3CalibrationError(
                f"selected calibration rendered evidence changed: {run_id}"
            )
        question = task.get("question_text")
        answer = generation.get("answer")
        _visible_text_is_blind(
            question, answer, rendered, run_id=run_id, task_id=task_id
        )
        reference = derive_g3_objective_reference_grade(receipt)
        commitments = {
            "run_id": run_id,
            "prepared_cell_sha256": prepared["artifact_sha256"],
            "generation_result_sha256": generation_sha256,
            "receipt_sha256": receipt["result_sha256"],
        }
        rows.append(
            {
                "canonical_cell_id": "G3C-"
                + _digest(seed, "canonical-cell", run_id)[:24],
                "source_cell_sha256": sha256_json(commitments),
                "run_id": run_id,
                "task_id": task_id,
                "suite": task["suite"],
                "policy": spec["policy"],
                "reasoning_effort": spec["reasoning_effort"],
                "question": question,
                "frozen_answer": answer,
                "supporting_rendered_evidence": rendered,
                "prepared_cell_sha256": prepared["artifact_sha256"],
                "generation_result_sha256": generation_sha256,
                "receipt_sha256": receipt["result_sha256"],
                "reference_inputs": {
                    "status": receipt["status"],
                    "is_correct": receipt["is_correct"],
                    "provenance_complete": receipt["provenance_complete"],
                },
                "reference_grade": reference,
            }
        )
    if (
        len(rows) != G3_CALIBRATION_UNIQUE_CELL_COUNT
        or len({row["canonical_cell_id"] for row in rows}) != len(rows)
        or len({row["source_cell_sha256"] for row in rows}) != len(rows)
    ):
        raise G3CalibrationError(
            "selected calibration source identities are not unique"
        )
    counts = Counter(f"{row['policy']}:{row['reasoning_effort']}" for row in rows)
    expected_counts = {
        key: G3_CALIBRATION_CELLS_PER_CONFIGURATION for key in _configuration_keys()
    }
    if dict(counts) != expected_counts:
        raise G3CalibrationError(
            "calibration sources are not balanced by configuration"
        )
    return rows


def _selection_rows(sources: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "canonical_cell_id": str(row["canonical_cell_id"]),
            "run_id": str(row["run_id"]),
            "task_id": str(row["task_id"]),
            "policy": str(row["policy"]),
            "reasoning_effort": str(row["reasoning_effort"]),
        }
        for row in sources
    ]


def _assemble_payloads(
    *,
    freeze: Mapping[str, Any],
    public_run: Mapping[str, Any],
    split_manifest: Mapping[str, Any],
    review_protocol: Mapping[str, Any],
    review_protocol_file_sha256: str,
    sources: list[dict[str, Any]],
    seed: bytes,
) -> dict[str, Any]:
    seed_sha256 = hashlib.sha256(seed).hexdigest()
    selection = _selection_rows(sources)
    selection_sha256 = sha256_json(selection)
    mapping_sha256 = _reference_mapping_sha256()
    rubric = review_protocol["rubric"]
    repeated_sources = sorted(
        sources,
        key=lambda row: (
            _digest(seed, "hidden-repeat-select", str(row["canonical_cell_id"])),
            str(row["canonical_cell_id"]),
        ),
    )[:G3_CALIBRATION_HIDDEN_REPEAT_COUNT]
    repeated_ids = {str(row["canonical_cell_id"]) for row in repeated_sources}

    packet_payloads: dict[str, dict[str, Any]] = {}
    packet_bytes: dict[str, bytes] = {}
    identities: list[dict[str, Any]] = []
    packet_records: list[dict[str, Any]] = []
    for reviewer in REVIEWERS:
        packet_id = "G3P-" + _digest(seed, "packet", reviewer)[:24]
        presentations: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for source in sources:
            occurrences = ["original"]
            if source["canonical_cell_id"] in repeated_ids:
                occurrences.append("hidden_repeat")
            for occurrence in occurrences:
                blind_id = (
                    "G3B-"
                    + _digest(
                        seed,
                        "blind-cell",
                        reviewer,
                        occurrence,
                        str(source["canonical_cell_id"]),
                    )[:24]
                )
                public_cell = {
                    "blind_cell_id": blind_id,
                    "question": source["question"],
                    "frozen_answer": source["frozen_answer"],
                    "supporting_rendered_evidence": source[
                        "supporting_rendered_evidence"
                    ],
                    "rubric": _json_clone(rubric),
                }
                identity = {
                    "reviewer": reviewer,
                    "packet_id": packet_id,
                    "position": 0,
                    "blind_cell_id": blind_id,
                    "canonical_cell_id": source["canonical_cell_id"],
                    "occurrence": occurrence,
                    "run_id": source["run_id"],
                    "task_id": source["task_id"],
                    "suite": source["suite"],
                    "policy": source["policy"],
                    "reasoning_effort": source["reasoning_effort"],
                    "source_cell_sha256": source["source_cell_sha256"],
                    "prepared_cell_sha256": source["prepared_cell_sha256"],
                    "generation_result_sha256": source["generation_result_sha256"],
                    "receipt_sha256": source["receipt_sha256"],
                }
                presentations.append((public_cell, identity))
        presentations.sort(
            key=lambda pair: (
                _digest(
                    seed,
                    "packet-order",
                    reviewer,
                    str(pair[0]["blind_cell_id"]),
                ),
                str(pair[0]["blind_cell_id"]),
            )
        )
        for position, (_public, identity) in enumerate(presentations, start=1):
            identity["position"] = position
            identities.append(identity)
        packet = {
            "schema_version": G3_CALIBRATION_PACKET_SCHEMA,
            "packet_id": packet_id,
            "reviewer": reviewer,
            "rubric_version": RUBRIC_VERSION,
            "rubric_sha256": REVIEW_RUBRIC_SHA256,
            "cell_count": G3_CALIBRATION_PACKET_CELL_COUNT,
            "cells": [public for public, _identity in presentations],
        }
        relative_path = f"{reviewer}/calibration.json"
        encoded = _json_bytes(packet)
        packet_payloads[relative_path] = packet
        packet_bytes[relative_path] = encoded
        packet_records.append(
            {
                "reviewer": reviewer,
                "packet_id": packet_id,
                "path": relative_path,
                "packet_sha256": hashlib.sha256(encoded).hexdigest(),
                "response_template_path": f"{reviewer}/response-template.json",
                "response_template_sha256": None,
                "cell_count": G3_CALIBRATION_PACKET_CELL_COUNT,
            }
        )

    if len({row["blind_cell_id"] for row in identities}) != len(identities):
        raise G3CalibrationError("calibration blind IDs are not unique")
    identity_map: dict[str, Any] = {
        "schema_version": G3_CALIBRATION_IDENTITY_MAP_SCHEMA,
        "g3_freeze_sha256": freeze["artifact_sha256"],
        "public_run_sha256": public_run["artifact_sha256"],
        "split_manifest_sha256": split_manifest["manifest_sha256"],
        "selection_algorithm": G3_CALIBRATION_SELECTION_ALGORITHM,
        "selection_seed_sha256": seed_sha256,
        "selection_sha256": selection_sha256,
        "reference_mapping_sha256": mapping_sha256,
        "identities": identities,
    }
    identity_map["artifact_sha256"] = sha256_json(identity_map)

    targets = sorted(
        [
            {
                "canonical_cell_id": source["canonical_cell_id"],
                "source_cell_sha256": source["source_cell_sha256"],
                "receipt_sha256": source["receipt_sha256"],
                **source["reference_inputs"],
                **source["reference_grade"],
            }
            for source in sources
        ],
        key=lambda row: str(row["canonical_cell_id"]),
    )
    reference: dict[str, Any] = {
        "schema_version": G3_CALIBRATION_REFERENCE_SCHEMA,
        "g3_freeze_sha256": freeze["artifact_sha256"],
        "public_run_sha256": public_run["artifact_sha256"],
        "selection_sha256": selection_sha256,
        "reference_mapping_version": G3_REFERENCE_MAPPING_VERSION,
        "reference_mapping": _reference_mapping(),
        "reference_mapping_sha256": mapping_sha256,
        "targets": targets,
    }
    reference["artifact_sha256"] = sha256_json(reference)
    identity_bytes = _json_bytes(identity_map)
    reference_bytes = _json_bytes(reference)
    identity_file_sha256 = hashlib.sha256(identity_bytes).hexdigest()

    invalid_grade = {
        "overall_ordinal": None,
        "factual_correctness": None,
        "completeness": None,
        "citation_support": None,
        "authority_freshness": None,
        "abstention_quality": None,
        "accepted": None,
        "failure_labels": [],
        "comment": "",
    }
    response_templates: dict[str, dict[str, Any]] = {}
    response_template_bytes: dict[str, bytes] = {}
    for record in packet_records:
        packet = packet_payloads[str(record["path"])]
        template = {
            "schema_version": G3_CALIBRATION_RETURN_SCHEMA,
            "reviewer": record["reviewer"],
            "packet_id": record["packet_id"],
            "packet_sha256": record["packet_sha256"],
            "review_manifest_sha256": None,
            "identity_map_sha256": identity_file_sha256,
            "grades": [
                {
                    "blind_cell_id": cell["blind_cell_id"],
                    "grade": _json_clone(invalid_grade),
                }
                for cell in packet["cells"]
            ],
            "rubric_ambiguous": None,
            "review_comment": "",
            "artifact_sha256": None,
        }
        template_path = str(record["response_template_path"])
        encoded = _json_bytes(template)
        record["response_template_sha256"] = hashlib.sha256(encoded).hexdigest()
        response_templates[template_path] = template
        response_template_bytes[template_path] = encoded

    manifest: dict[str, Any] = {
        "schema_version": G3_CALIBRATION_MANIFEST_SCHEMA,
        "g3_freeze_sha256": freeze["artifact_sha256"],
        "frozen_manifest_sha256": freeze["manifest"]["frozen_manifest_sha256"],
        "public_run_sha256": public_run["artifact_sha256"],
        "split_manifest_sha256": split_manifest["manifest_sha256"],
        "review_protocol_file_sha256": review_protocol_file_sha256,
        "review_protocol_canonical_sha256": sha256_json(review_protocol),
        "rubric_sha256": REVIEW_RUBRIC_SHA256,
        "rubric_version": RUBRIC_VERSION,
        "reviewers": list(REVIEWERS),
        "calibration_partition": G3_CALIBRATION_PARTITION,
        "selection_algorithm": G3_CALIBRATION_SELECTION_ALGORITHM,
        "selection_seed_sha256": seed_sha256,
        "reference_mapping_version": G3_REFERENCE_MAPPING_VERSION,
        "reference_mapping_sha256": mapping_sha256,
        "configuration_cell_counts": {
            key: G3_CALIBRATION_CELLS_PER_CONFIGURATION for key in _configuration_keys()
        },
        "unique_cell_count": G3_CALIBRATION_UNIQUE_CELL_COUNT,
        "hidden_repeat_count_per_reviewer": G3_CALIBRATION_HIDDEN_REPEAT_COUNT,
        "packet_cell_count": G3_CALIBRATION_PACKET_CELL_COUNT,
        "packet_count": len(REVIEWERS),
        "selection_sha256": selection_sha256,
        "identity_map_sha256": identity_file_sha256,
        "reference_sha256": hashlib.sha256(reference_bytes).hexdigest(),
        "packets": packet_records,
    }
    manifest["artifact_sha256"] = sha256_json(manifest)
    return {
        "manifest": manifest,
        "manifest_bytes": _json_bytes(manifest),
        "packets": packet_payloads,
        "packet_bytes": packet_bytes,
        "response_templates": response_templates,
        "response_template_bytes": response_template_bytes,
        "identity_map": identity_map,
        "identity_bytes": identity_bytes,
        "reference": reference,
        "reference_bytes": reference_bytes,
    }


def _workflow_payloads(
    *,
    root: Path,
    freeze: Mapping[str, Any],
    public_run: Mapping[str, Any],
    seed: bytes,
) -> dict[str, Any]:
    try:
        validate_g3_freeze(freeze)
    except Exception as exc:
        raise G3CalibrationError(f"G3 freeze is invalid: {exc}") from exc
    split = _canonical_split(root)
    protocol, protocol_file_sha256 = _canonical_review_protocol(root)
    selected = _selected_specs(freeze, split, seed)
    sources = _source_rows(
        root=root,
        freeze=freeze,
        public_run=public_run,
        selected_specs=selected,
        seed=seed,
    )
    return _assemble_payloads(
        freeze=freeze,
        public_run=public_run,
        split_manifest=split,
        review_protocol=protocol,
        review_protocol_file_sha256=protocol_file_sha256,
        sources=sources,
        seed=seed,
    )


def _validate_packet(
    value: Mapping[str, Any],
    *,
    record: Mapping[str, Any],
    rubric: Mapping[str, Any],
) -> None:
    if (
        not isinstance(value, Mapping)
        or set(value) != _PACKET_FIELDS
        or value.get("schema_version") != G3_CALIBRATION_PACKET_SCHEMA
        or value.get("packet_id") != record.get("packet_id")
        or value.get("reviewer") != record.get("reviewer")
        or value.get("rubric_version") != RUBRIC_VERSION
        or value.get("rubric_sha256") != REVIEW_RUBRIC_SHA256
        or value.get("cell_count") != G3_CALIBRATION_PACKET_CELL_COUNT
        or record.get("cell_count") != G3_CALIBRATION_PACKET_CELL_COUNT
    ):
        raise G3CalibrationError("calibration packet identity or fields differ")
    if not _PACKET_ID.fullmatch(str(value["packet_id"])):
        raise G3CalibrationError("calibration packet ID is invalid")
    cells = value.get("cells")
    if not isinstance(cells, list) or len(cells) != G3_CALIBRATION_PACKET_CELL_COUNT:
        raise G3CalibrationError("calibration packet must contain exactly 22 cells")
    blind_ids: list[str] = []
    for cell in cells:
        if not isinstance(cell, Mapping) or set(cell) != _PACKET_CELL_FIELDS:
            raise G3CalibrationError("calibration packet cell fields differ")
        blind_id = cell.get("blind_cell_id")
        if not isinstance(blind_id, str) or not _BLIND_ID.fullmatch(blind_id):
            raise G3CalibrationError("calibration packet blind cell ID is invalid")
        if cell.get("rubric") != rubric:
            raise G3CalibrationError("calibration packet rubric differs")
        _visible_text_is_blind(
            cell.get("question"),
            cell.get("frozen_answer"),
            cell.get("supporting_rendered_evidence"),
        )
        blind_ids.append(blind_id)
    if len(set(blind_ids)) != len(blind_ids):
        raise G3CalibrationError("calibration packet repeats a blind cell ID")


def _validate_response_template(
    value: Mapping[str, Any],
    *,
    record: Mapping[str, Any],
    packet: Mapping[str, Any],
    identity_map_sha256: str,
) -> None:
    if (
        not isinstance(value, Mapping)
        or set(value) != _RETURN_FIELDS
        or value.get("schema_version") != G3_CALIBRATION_RETURN_SCHEMA
        or value.get("reviewer") != record.get("reviewer")
        or value.get("packet_id") != record.get("packet_id")
        or value.get("packet_sha256") != record.get("packet_sha256")
        or value.get("review_manifest_sha256") is not None
        or value.get("identity_map_sha256") != identity_map_sha256
        or value.get("rubric_ambiguous") is not None
        or value.get("review_comment") != ""
        or value.get("artifact_sha256") is not None
    ):
        raise G3CalibrationError("blank response template identity or fields differ")
    rows = value.get("grades")
    packet_cells = packet.get("cells")
    if (
        not isinstance(rows, list)
        or not isinstance(packet_cells, list)
        or len(rows) != G3_CALIBRATION_PACKET_CELL_COUNT
    ):
        raise G3CalibrationError("blank response template must contain all 22 cells")
    expected_grade = {
        "overall_ordinal": None,
        "factual_correctness": None,
        "completeness": None,
        "citation_support": None,
        "authority_freshness": None,
        "abstention_quality": None,
        "accepted": None,
        "failure_labels": [],
        "comment": "",
    }
    expected_ids = [str(cell["blind_cell_id"]) for cell in packet_cells]
    if [
        row.get("blind_cell_id") for row in rows if isinstance(row, Mapping)
    ] != expected_ids:
        raise G3CalibrationError("blank response template blind IDs differ")
    for row in rows:
        if (
            not isinstance(row, Mapping)
            or set(row) != _RETURN_GRADE_FIELDS
            or row.get("grade") != expected_grade
        ):
            raise G3CalibrationError(
                "blank response template must use invalid grade placeholders"
            )


def validate_g3_calibration_manifest(
    value: Mapping[str, Any],
    *,
    public_directory: Path,
    root: Path | None = None,
) -> None:
    """Validate the public, content-free manifest and all three packet files."""

    root_value = (root or repository_root()).resolve()
    public_root = public_directory.resolve()
    protocol, protocol_file_sha256 = _canonical_review_protocol(root_value)
    if not isinstance(value, Mapping) or set(value) != _MANIFEST_FIELDS:
        raise G3CalibrationError("public calibration manifest fields differ")
    body = {key: item for key, item in value.items() if key != "artifact_sha256"}
    if value.get("schema_version") != G3_CALIBRATION_MANIFEST_SCHEMA or value.get(
        "artifact_sha256"
    ) != sha256_json(body):
        raise G3CalibrationError("public calibration manifest hash is invalid")
    for field in (
        "g3_freeze_sha256",
        "frozen_manifest_sha256",
        "public_run_sha256",
        "split_manifest_sha256",
        "review_protocol_file_sha256",
        "review_protocol_canonical_sha256",
        "rubric_sha256",
        "selection_seed_sha256",
        "reference_mapping_sha256",
        "selection_sha256",
        "identity_map_sha256",
        "reference_sha256",
        "artifact_sha256",
    ):
        if not _is_sha256(value.get(field)):
            raise G3CalibrationError(f"public calibration {field} is invalid")
    if (
        value.get("review_protocol_file_sha256") != protocol_file_sha256
        or value.get("review_protocol_canonical_sha256") != sha256_json(protocol)
        or value.get("rubric_sha256") != REVIEW_RUBRIC_SHA256
        or value.get("rubric_version") != RUBRIC_VERSION
        or value.get("reviewers") != list(REVIEWERS)
        or value.get("calibration_partition") != G3_CALIBRATION_PARTITION
        or value.get("selection_algorithm") != G3_CALIBRATION_SELECTION_ALGORITHM
        or value.get("reference_mapping_version") != G3_REFERENCE_MAPPING_VERSION
        or value.get("reference_mapping_sha256") != _reference_mapping_sha256()
        or value.get("configuration_cell_counts")
        != {
            key: G3_CALIBRATION_CELLS_PER_CONFIGURATION for key in _configuration_keys()
        }
        or value.get("unique_cell_count") != G3_CALIBRATION_UNIQUE_CELL_COUNT
        or value.get("hidden_repeat_count_per_reviewer")
        != G3_CALIBRATION_HIDDEN_REPEAT_COUNT
        or value.get("packet_cell_count") != G3_CALIBRATION_PACKET_CELL_COUNT
        or value.get("packet_count") != len(REVIEWERS)
    ):
        raise G3CalibrationError("public calibration manifest contract changed")
    saved_manifest = _read_json(public_root / "manifest.json", "public manifest")
    if saved_manifest != dict(value):
        raise G3CalibrationError("supplied public manifest differs from its saved file")
    records = value.get("packets")
    if not isinstance(records, list) or len(records) != len(REVIEWERS):
        raise G3CalibrationError("public manifest must bind exactly three packets")
    if [
        record.get("reviewer") for record in records if isinstance(record, Mapping)
    ] != list(REVIEWERS):
        raise G3CalibrationError("public calibration reviewer order changed")
    packet_ids: set[str] = set()
    blind_ids: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping) or set(record) != _PACKET_RECORD_FIELDS:
            raise G3CalibrationError("public calibration packet record fields differ")
        reviewer = str(record["reviewer"])
        expected_path = f"{reviewer}/calibration.json"
        if record.get("path") != expected_path:
            raise G3CalibrationError("public calibration packet path is not canonical")
        path = _contained_path(public_root, record["path"], "calibration packet path")
        if _file_sha256(path) != record.get("packet_sha256"):
            raise G3CalibrationError("public calibration packet hash is stale")
        packet = _read_json(path, "public calibration packet")
        _validate_packet(packet, record=record, rubric=protocol["rubric"])
        expected_template_path = f"{reviewer}/response-template.json"
        if record.get("response_template_path") != expected_template_path:
            raise G3CalibrationError("response template path is not canonical")
        template_path = _contained_path(
            public_root,
            record["response_template_path"],
            "calibration response template path",
        )
        if _file_sha256(template_path) != record.get("response_template_sha256"):
            raise G3CalibrationError("calibration response template hash is stale")
        template = _read_json(template_path, "blank calibration response template")
        _validate_response_template(
            template,
            record=record,
            packet=packet,
            identity_map_sha256=str(value["identity_map_sha256"]),
        )
        if str(record["packet_id"]) in packet_ids:
            raise G3CalibrationError("public calibration packet IDs are duplicated")
        packet_ids.add(str(record["packet_id"]))
        for cell in packet["cells"]:
            blind_id = str(cell["blind_cell_id"])
            if blind_id in blind_ids:
                raise G3CalibrationError("blind cell IDs are reused between reviewers")
            blind_ids.add(blind_id)


def _repository_json_snapshot(
    root: Path, path: Path, label: str
) -> tuple[dict[str, Any], bytes]:
    try:
        payload = read_bytes_snapshot(root, path, max_bytes=16 * 1024 * 1024)
        value = json.loads(payload.decode("utf-8", errors="strict"))
    except (
        ImmutableIOError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise G3CalibrationError(f"cannot read {label} safely") from exc
    if not isinstance(value, dict):
        raise G3CalibrationError(f"{label} must be a JSON object")
    return value, payload


def _require_ai_calibration_not_started(public_directory: Path) -> None:
    for reviewer in AI_REVIEWERS:
        for filename in (
            "response-filled.json",
            "completed-return.json",
            "invocation-receipt.json",
            "native-invocation.json",
            "native-output.jsonl",
            "public-review-workspace.json",
        ):
            path = public_directory / reviewer / filename
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise G3CalibrationError(
                    f"cannot inspect {reviewer} calibration state"
                ) from exc
            raise G3CalibrationError(
                "G3 calibration token confirmation must precede every AI review"
            )


def _verified_g3_ai_packet_payloads(
    manifest: Mapping[str, Any],
    *,
    public_directory: Path,
    root: Path,
) -> tuple[dict[str, dict[str, bytes]], str]:
    validate_g3_calibration_manifest(
        manifest,
        public_directory=public_directory,
        root=root,
    )
    saved_manifest, manifest_bytes = _repository_json_snapshot(
        root,
        public_directory / "manifest.json",
        "G3 calibration manifest",
    )
    if saved_manifest != dict(manifest):
        raise G3CalibrationError("saved G3 calibration manifest changed")
    payloads_by_reviewer: dict[str, dict[str, bytes]] = {}
    for reviewer in AI_REVIEWERS:
        record = _packet_record(manifest, reviewer)
        packet_path = _contained_path(
            public_directory.resolve(),
            record["path"],
            f"{reviewer} calibration packet path",
        )
        try:
            packet_bytes = read_bytes_snapshot(
                root, packet_path, max_bytes=16 * 1024 * 1024
            )
        except ImmutableIOError as exc:
            raise G3CalibrationError(
                f"cannot read {reviewer} calibration packet safely"
            ) from exc
        payloads_by_reviewer[reviewer] = {str(record["packet_id"]): packet_bytes}
    return payloads_by_reviewer, hashlib.sha256(manifest_bytes).hexdigest()


def _g3_calibration_token_preflight_payload(
    manifest: Mapping[str, Any],
    *,
    public_directory: Path,
    root: Path,
) -> dict[str, Any]:
    payloads_by_reviewer, manifest_file_sha256 = _verified_g3_ai_packet_payloads(
        manifest,
        public_directory=public_directory,
        root=root,
    )
    try:
        preflights = {
            reviewer: verify_packet_token_preflight(
                manifest,
                reviewer,
                payloads_by_reviewer[reviewer],
            )
            for reviewer in AI_REVIEWERS
        }
    except ReviewContractError as exc:
        raise G3CalibrationError("G3 calibration token preflight failed") from exc
    preflight: dict[str, Any] = {
        "schema_version": G3_CALIBRATION_TOKEN_PREFLIGHT_SCHEMA,
        "review_manifest_sha256": manifest["artifact_sha256"],
        "review_manifest_file_sha256": manifest_file_sha256,
        "rubric_version": RUBRIC_VERSION,
        "rubric_sha256": REVIEW_RUBRIC_SHA256,
        "token_verifier": {
            "verifier_id": PACKET_TOKEN_VERIFIER_ID,
            "verifier_sha256": PACKET_TOKEN_VERIFIER_SHA256,
            "profiles": {
                reviewer: PINNED_REVIEW_TOKEN_PROFILES[reviewer]
                for reviewer in AI_REVIEWERS
            },
            "input_binding": "exact_manifest_packet_bytes",
        },
        "packet_count_per_ai": {
            reviewer: preflights[reviewer]["packet_count"] for reviewer in AI_REVIEWERS
        },
        "utf8_bytes_total_per_ai": {
            reviewer: preflights[reviewer]["utf8_bytes_total"]
            for reviewer in AI_REVIEWERS
        },
        "token_total_per_ai": {
            reviewer: preflights[reviewer]["token_total"] for reviewer in AI_REVIEWERS
        },
        "preflights": preflights,
        "confirmed_before_review": False,
    }
    preflight["artifact_sha256"] = sha256_json(preflight)
    return preflight


def _write_public_record_once(
    root: Path,
    path: Path,
    value: Mapping[str, Any],
    label: str,
) -> None:
    _, relative = _repository_write_relative(root, path, label)
    _write_once_or_verify(root, relative, _json_bytes(value), label)


def build_g3_calibration_token_preflight(
    manifest: Mapping[str, Any],
    *,
    public_directory: Path,
    root: Path | None = None,
) -> dict[str, Any]:
    """Save exact AI packet counts before any G3 calibration review starts."""

    root_value = (root or repository_root()).resolve()
    public_root = public_directory.resolve()
    _require_ai_calibration_not_started(public_root)
    preflight = _g3_calibration_token_preflight_payload(
        manifest,
        public_directory=public_root,
        root=root_value,
    )
    _write_public_record_once(
        root_value,
        public_root / "ai-token-preflight.json",
        preflight,
        "G3 calibration token preflight",
    )
    return preflight


def confirm_g3_calibration_token_preflight(
    manifest: Mapping[str, Any],
    *,
    confirmed_at: str,
    public_directory: Path,
    root: Path | None = None,
) -> dict[str, Any]:
    """Bind Kevin's confirmation to current G3 packet bytes and exact counts."""

    if not isinstance(confirmed_at, str) or _UTC_SECOND.fullmatch(confirmed_at) is None:
        raise G3CalibrationError("G3 token confirmation time must be UTC to seconds")
    try:
        datetime.strptime(confirmed_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise G3CalibrationError("G3 token confirmation time is invalid") from exc
    root_value = (root or repository_root()).resolve()
    public_root = public_directory.resolve()
    _require_ai_calibration_not_started(public_root)
    expected_preflight = _g3_calibration_token_preflight_payload(
        manifest,
        public_directory=public_root,
        root=root_value,
    )
    saved_preflight, saved_preflight_bytes = _repository_json_snapshot(
        root_value,
        public_root / "ai-token-preflight.json",
        "G3 calibration token preflight",
    )
    if saved_preflight != expected_preflight:
        raise G3CalibrationError("G3 calibration token preflight is stale")
    payloads_by_reviewer, _ = _verified_g3_ai_packet_payloads(
        manifest,
        public_directory=public_root,
        root=root_value,
    )
    try:
        confirmed_preflights = {
            reviewer: verify_packet_token_preflight(
                manifest,
                reviewer,
                payloads_by_reviewer[reviewer],
                confirmed_by="Kevin Araujo",
            )
            for reviewer in AI_REVIEWERS
        }
    except ReviewContractError as exc:
        raise G3CalibrationError(
            "G3 calibration token confirmation failed replay"
        ) from exc
    confirmation: dict[str, Any] = {
        "schema_version": G3_CALIBRATION_TOKEN_CONFIRMATION_SCHEMA,
        "review_manifest_sha256": manifest["artifact_sha256"],
        "preflight_artifact_sha256": expected_preflight["artifact_sha256"],
        "preflight_file_sha256": hashlib.sha256(saved_preflight_bytes).hexdigest(),
        "reviewer": "Kevin Araujo",
        "reviewer_role": "sole_human_reviewer",
        "confirmed_at": confirmed_at,
        "confirmed_preflights": confirmed_preflights,
    }
    confirmation["artifact_sha256"] = sha256_json(confirmation)
    _write_public_record_once(
        root_value,
        public_root / "ai-token-preflight-confirmation.json",
        confirmation,
        "G3 calibration token confirmation",
    )
    return confirmation


def validate_g3_calibration_token_confirmation(
    manifest: Mapping[str, Any],
    *,
    public_directory: Path,
    root: Path | None = None,
) -> tuple[dict[str, Any], str]:
    """Replay Kevin's confirmation against the exact current AI packet bytes."""

    root_value = (root or repository_root()).resolve()
    public_root = public_directory.resolve()
    expected_preflight = _g3_calibration_token_preflight_payload(
        manifest,
        public_directory=public_root,
        root=root_value,
    )
    saved_preflight, saved_preflight_bytes = _repository_json_snapshot(
        root_value,
        public_root / "ai-token-preflight.json",
        "G3 calibration token preflight",
    )
    if saved_preflight != expected_preflight:
        raise G3CalibrationError("G3 calibration token preflight is stale")
    confirmation, confirmation_bytes = _repository_json_snapshot(
        root_value,
        public_root / "ai-token-preflight-confirmation.json",
        "G3 calibration token confirmation",
    )
    payloads_by_reviewer, _ = _verified_g3_ai_packet_payloads(
        manifest,
        public_directory=public_root,
        root=root_value,
    )
    try:
        expected_confirmed = {
            reviewer: verify_packet_token_preflight(
                manifest,
                reviewer,
                payloads_by_reviewer[reviewer],
                confirmed_by="Kevin Araujo",
            )
            for reviewer in AI_REVIEWERS
        }
    except ReviewContractError as exc:
        raise G3CalibrationError(
            "G3 calibration confirmation packet replay failed"
        ) from exc
    body = {key: item for key, item in confirmation.items() if key != "artifact_sha256"}
    try:
        datetime.strptime(str(confirmation.get("confirmed_at")), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise G3CalibrationError(
            "G3 calibration token confirmation time is invalid"
        ) from exc
    if (
        confirmation.get("schema_version") != G3_CALIBRATION_TOKEN_CONFIRMATION_SCHEMA
        or confirmation.get("artifact_sha256") != sha256_json(body)
        or confirmation.get("review_manifest_sha256") != manifest.get("artifact_sha256")
        or confirmation.get("preflight_artifact_sha256")
        != expected_preflight["artifact_sha256"]
        or confirmation.get("preflight_file_sha256")
        != hashlib.sha256(saved_preflight_bytes).hexdigest()
        or confirmation.get("reviewer") != "Kevin Araujo"
        or confirmation.get("reviewer_role") != "sole_human_reviewer"
        or not isinstance(confirmation.get("confirmed_at"), str)
        or _UTC_SECOND.fullmatch(str(confirmation.get("confirmed_at"))) is None
        or confirmation.get("confirmed_preflights") != expected_confirmed
    ):
        raise G3CalibrationError("G3 calibration token confirmation is invalid")
    return confirmation, hashlib.sha256(confirmation_bytes).hexdigest()


def validate_g3_calibration_bundle(
    value: Mapping[str, Any],
    *,
    freeze: Mapping[str, Any],
    public_run: Mapping[str, Any],
    seed: bytes,
    public_directory: Path,
    external_identity_map: Path,
    external_reference: Path,
    root: Path | None = None,
) -> None:
    """Rebuild and validate selection, files, hashes, blindness, and private maps."""

    root_value = (root or repository_root()).resolve()
    seed_value = _seed_bytes(seed)
    identity_path = _outside_root(
        external_identity_map, root_value, "calibration identity map"
    )
    reference_path = _outside_root(
        external_reference, root_value, "calibration reference"
    )
    if identity_path == reference_path:
        raise G3CalibrationError("identity map and reference must be separate files")
    _separate_private_path(identity_path, public_directory, "calibration identity map")
    _separate_private_path(reference_path, public_directory, "calibration reference")
    expected = _workflow_payloads(
        root=root_value,
        freeze=freeze,
        public_run=public_run,
        seed=seed_value,
    )
    validate_g3_calibration_manifest(
        value, public_directory=public_directory, root=root_value
    )
    if dict(value) != expected["manifest"]:
        raise G3CalibrationError(
            "public calibration manifest differs from the frozen source artifacts"
        )
    identity, identity_file_sha256 = _read_private_json_snapshot(
        identity_path, root_value, "external calibration identity map"
    )
    reference, reference_file_sha256 = _read_private_json_snapshot(
        reference_path, root_value, "external calibration reference"
    )
    if identity_file_sha256 != value.get(
        "identity_map_sha256"
    ) or reference_file_sha256 != value.get("reference_sha256"):
        raise G3CalibrationError("external calibration artifact hash is stale")
    if identity != expected["identity_map"]:
        raise G3CalibrationError("external calibration identity map changed")
    if reference != expected["reference"]:
        raise G3CalibrationError("external calibration reference changed")
    if set(identity) != _IDENTITY_MAP_FIELDS or set(reference) != _REFERENCE_FIELDS:
        raise G3CalibrationError("external calibration artifact fields differ")
    if identity.get("artifact_sha256") != sha256_json(
        {key: item for key, item in identity.items() if key != "artifact_sha256"}
    ) or reference.get("artifact_sha256") != sha256_json(
        {key: item for key, item in reference.items() if key != "artifact_sha256"}
    ):
        raise G3CalibrationError("external calibration semantic hash is invalid")
    identities = identity.get("identities")
    targets = reference.get("targets")
    if (
        not isinstance(identities, list)
        or len(identities) != len(REVIEWERS) * G3_CALIBRATION_PACKET_CELL_COUNT
        or any(
            not isinstance(row, Mapping) or set(row) != _IDENTITY_FIELDS
            for row in identities
        )
        or not isinstance(targets, list)
        or len(targets) != G3_CALIBRATION_UNIQUE_CELL_COUNT
        or any(
            not isinstance(row, Mapping) or set(row) != _REFERENCE_TARGET_FIELDS
            for row in targets
        )
    ):
        raise G3CalibrationError("external calibration rows are incomplete")
    for relative_path, expected_packet in expected["packets"].items():
        actual_path = _contained_path(
            public_directory.resolve(), relative_path, "calibration packet path"
        )
        if _read_json(actual_path, "public calibration packet") != expected_packet:
            raise G3CalibrationError("public calibration packet content changed")
    for relative_path, expected_template in expected["response_templates"].items():
        actual_path = _contained_path(
            public_directory.resolve(),
            relative_path,
            "calibration response template path",
        )
        if (
            _read_json(actual_path, "blank calibration response template")
            != expected_template
        ):
            raise G3CalibrationError("calibration response template changed")


def validate_g3_calibration_frozen_bundle(
    value: Mapping[str, Any],
    *,
    freeze: Mapping[str, Any],
    public_run: Mapping[str, Any],
    public_directory: Path,
    external_identity_map: Path,
    external_reference: Path,
    root: Path | None = None,
) -> None:
    """Replay a frozen calibration after its one-time blinding seed is destroyed.

    The protocol intentionally stores only the seed hash.  This validator does
    not try to reproduce the HMAC ordering.  It instead proves that the saved
    packet files, private identity map, objective reference, exact public run,
    and every selected source artifact remain mutually hash-bound.
    """

    root_value = (root or repository_root()).resolve()
    try:
        validate_g3_freeze(freeze)
    except Exception as exc:
        raise G3CalibrationError(f"G3 freeze is invalid: {exc}") from exc
    validate_g3_calibration_manifest(
        value, public_directory=public_directory, root=root_value
    )
    split = _canonical_split(root_value)
    indexed_run = _run_index(public_run, freeze)
    manifest = freeze.get("manifest")
    if not isinstance(manifest, Mapping):
        raise G3CalibrationError("G3 freeze manifest is missing")
    if (
        value.get("g3_freeze_sha256") != freeze.get("artifact_sha256")
        or value.get("frozen_manifest_sha256") != manifest.get("frozen_manifest_sha256")
        or value.get("public_run_sha256") != public_run.get("artifact_sha256")
        or value.get("split_manifest_sha256") != split.get("manifest_sha256")
    ):
        raise G3CalibrationError("frozen calibration source hashes are stale")

    public_root = public_directory.resolve()
    identity_path = _outside_root(
        external_identity_map, root_value, "calibration identity map"
    )
    reference_path = _outside_root(
        external_reference, root_value, "calibration reference"
    )
    if identity_path == reference_path:
        raise G3CalibrationError("identity map and reference must be separate files")
    _separate_private_path(identity_path, public_root, "calibration identity map")
    _separate_private_path(reference_path, public_root, "calibration reference")
    identity, identity_file_sha256 = _read_private_json_snapshot(
        identity_path, root_value, "external calibration identity map"
    )
    reference, reference_file_sha256 = _read_private_json_snapshot(
        reference_path, root_value, "external calibration reference"
    )
    if (
        identity_file_sha256 != value.get("identity_map_sha256")
        or reference_file_sha256 != value.get("reference_sha256")
        or set(identity) != _IDENTITY_MAP_FIELDS
        or set(reference) != _REFERENCE_FIELDS
        or identity.get("schema_version") != G3_CALIBRATION_IDENTITY_MAP_SCHEMA
        or reference.get("schema_version") != G3_CALIBRATION_REFERENCE_SCHEMA
    ):
        raise G3CalibrationError("external calibration artifact is stale or invalid")
    for artifact, label in (
        (identity, "identity map"),
        (reference, "reference"),
    ):
        if artifact.get("artifact_sha256") != sha256_json(
            {key: item for key, item in artifact.items() if key != "artifact_sha256"}
        ):
            raise G3CalibrationError(f"external calibration {label} hash is invalid")
    if (
        identity.get("g3_freeze_sha256") != value.get("g3_freeze_sha256")
        or identity.get("public_run_sha256") != value.get("public_run_sha256")
        or identity.get("split_manifest_sha256") != value.get("split_manifest_sha256")
        or identity.get("selection_algorithm") != G3_CALIBRATION_SELECTION_ALGORITHM
        or identity.get("selection_seed_sha256") != value.get("selection_seed_sha256")
        or identity.get("selection_sha256") != value.get("selection_sha256")
        or identity.get("reference_mapping_sha256")
        != value.get("reference_mapping_sha256")
        or reference.get("g3_freeze_sha256") != value.get("g3_freeze_sha256")
        or reference.get("public_run_sha256") != value.get("public_run_sha256")
        or reference.get("selection_sha256") != value.get("selection_sha256")
        or reference.get("reference_mapping_version") != G3_REFERENCE_MAPPING_VERSION
        or reference.get("reference_mapping") != _reference_mapping()
        or reference.get("reference_mapping_sha256") != _reference_mapping_sha256()
    ):
        raise G3CalibrationError("external calibration commitments disagree")

    identities = identity.get("identities")
    targets = reference.get("targets")
    if (
        not isinstance(identities, list)
        or len(identities) != len(REVIEWERS) * G3_CALIBRATION_PACKET_CELL_COUNT
        or any(
            not isinstance(row, Mapping) or set(row) != _IDENTITY_FIELDS
            for row in identities
        )
        or not isinstance(targets, list)
        or len(targets) != G3_CALIBRATION_UNIQUE_CELL_COUNT
        or any(
            not isinstance(row, Mapping) or set(row) != _REFERENCE_TARGET_FIELDS
            for row in targets
        )
    ):
        raise G3CalibrationError("external calibration rows are incomplete")
    target_by_id = {
        str(row["canonical_cell_id"]): row
        for row in targets
        if isinstance(row, Mapping)
    }
    if len(target_by_id) != G3_CALIBRATION_UNIQUE_CELL_COUNT:
        raise G3CalibrationError("calibration reference repeats a canonical cell")

    packet_cells: dict[tuple[str, int], Mapping[str, Any]] = {}
    for reviewer in REVIEWERS:
        record = _packet_record(value, reviewer)
        packet_path = _contained_path(
            public_root, record["path"], "calibration packet path"
        )
        packet = _read_json(packet_path, "public calibration packet")
        cells = packet.get("cells")
        if not isinstance(cells, list):
            raise G3CalibrationError("public calibration packet cells are missing")
        for position, cell in enumerate(cells, start=1):
            if not isinstance(cell, Mapping):
                raise G3CalibrationError("public calibration packet cell is invalid")
            packet_cells[(reviewer, position)] = cell

    specs = manifest.get("run_specs")
    if not isinstance(specs, list):
        raise G3CalibrationError("frozen G3 run specs are missing")
    specs_by_run = {
        str(spec["run_id"]): spec
        for spec in specs
        if isinstance(spec, Mapping) and isinstance(spec.get("run_id"), str)
    }
    split_rows = split.get("tasks")
    if not isinstance(split_rows, list):
        raise G3CalibrationError("canonical split tasks are missing")
    split_by_task = {
        str(row["task_id"]): row
        for row in split_rows
        if isinstance(row, Mapping) and isinstance(row.get("task_id"), str)
    }

    grouped: dict[str, list[Mapping[str, Any]]] = {}
    reviewer_positions: dict[str, list[int]] = {reviewer: [] for reviewer in REVIEWERS}
    blind_ids: set[str] = set()
    for row in identities:
        reviewer = str(row["reviewer"])
        position = row["position"]
        blind_id = row["blind_cell_id"]
        canonical_id = str(row["canonical_cell_id"])
        if (
            reviewer not in REVIEWERS
            or isinstance(position, bool)
            or not isinstance(position, int)
            or not 1 <= position <= G3_CALIBRATION_PACKET_CELL_COUNT
            or not isinstance(blind_id, str)
            or _BLIND_ID.fullmatch(blind_id) is None
            or blind_id in blind_ids
            or row.get("occurrence") not in {"original", "hidden_repeat"}
            or canonical_id not in target_by_id
        ):
            raise G3CalibrationError("calibration identity row is invalid")
        record = _packet_record(value, reviewer)
        cell = packet_cells[(reviewer, position)]
        if (
            row.get("packet_id") != record.get("packet_id")
            or cell.get("blind_cell_id") != blind_id
        ):
            raise G3CalibrationError("identity row differs from its public packet")
        reviewer_positions[reviewer].append(position)
        blind_ids.add(blind_id)
        grouped.setdefault(canonical_id, []).append(row)
    if any(
        sorted(positions) != list(range(1, G3_CALIBRATION_PACKET_CELL_COUNT + 1))
        for positions in reviewer_positions.values()
    ):
        raise G3CalibrationError("calibration packet positions are incomplete")
    if set(grouped) != set(target_by_id):
        raise G3CalibrationError("identity map and reference cell sets differ")

    selected_runs: set[str] = set()
    configuration_counts: Counter[str] = Counter()
    repeated_ids: set[str] = set()
    trusted = str(manifest["frozen_manifest_sha256"])
    for canonical_id, rows in grouped.items():
        originals = [row for row in rows if row["occurrence"] == "original"]
        repeats = [row for row in rows if row["occurrence"] == "hidden_repeat"]
        if {str(row["reviewer"]) for row in originals} != set(REVIEWERS) or (
            repeats and {str(row["reviewer"]) for row in repeats} != set(REVIEWERS)
        ):
            raise G3CalibrationError(
                "calibration occurrence is not shared by all reviewers"
            )
        if len(originals) != len(REVIEWERS) or len(repeats) not in {0, len(REVIEWERS)}:
            raise G3CalibrationError("calibration occurrence count is invalid")
        if repeats:
            repeated_ids.add(canonical_id)
        commitment_fields = (
            "run_id",
            "task_id",
            "suite",
            "policy",
            "reasoning_effort",
            "source_cell_sha256",
            "prepared_cell_sha256",
            "generation_result_sha256",
            "receipt_sha256",
        )
        baseline = {field: originals[0][field] for field in commitment_fields}
        if any(
            {field: row[field] for field in commitment_fields} != baseline
            for row in rows[1:]
        ):
            raise G3CalibrationError("reviewers received different source commitments")

        run_id = str(baseline["run_id"])
        spec = specs_by_run.get(run_id)
        task = spec.get("task") if isinstance(spec, Mapping) else None
        task_id = str(baseline["task_id"])
        split_row = split_by_task.get(task_id)
        if (
            spec is None
            or not isinstance(task, Mapping)
            or run_id in selected_runs
            or task.get("task_id") != task_id
            or task.get("suite") != baseline["suite"]
            or spec.get("policy") != baseline["policy"]
            or spec.get("reasoning_effort") != baseline["reasoning_effort"]
            or split_row is None
            or split_row.get("partition") != G3_CALIBRATION_PARTITION
        ):
            raise G3CalibrationError(
                "selected calibration source is outside the frozen split"
            )
        selected_runs.add(run_id)
        configuration_counts[
            f"{baseline['policy']}:{baseline['reasoning_effort']}"
        ] += 1

        run_cell = indexed_run.get(run_id)
        if (
            not isinstance(run_cell, Mapping)
            or run_cell.get("generation_status") != "completed"
            or run_cell.get("grade_status") != "objective_completed"
        ):
            raise G3CalibrationError("selected calibration source is not completed")
        paths = _canonical_paths(root_value, manifest, spec)
        prepared = _repo_json(
            paths["prepared"], root_value, "prepared calibration cell"
        )
        generation = _repo_json(
            paths["generation"], root_value, "calibration generation"
        )
        receipt = _repo_json(paths["receipt"], root_value, "calibration receipt")
        try:
            validate_prepared_public_g3_cell(prepared, root=root_value)
            validate_saved_generation_result(
                generation,
                expected_run_id=run_id,
                expected_task_id=task_id,
                expected_effort=str(spec["reasoning_effort"]),
            )
            validate_memory_result_receipt(receipt, spec, manifest, trusted)
        except Exception as exc:
            raise G3CalibrationError(
                f"selected calibration artifact is invalid: {run_id}: {exc}"
            ) from exc
        generation_sha256 = sha256_json(generation)
        source_sha256 = sha256_json(
            {
                "run_id": run_id,
                "prepared_cell_sha256": prepared.get("artifact_sha256"),
                "generation_result_sha256": generation_sha256,
                "receipt_sha256": receipt.get("result_sha256"),
            }
        )
        target = target_by_id[canonical_id]
        expected_reference = derive_g3_objective_reference_grade(receipt)
        if (
            baseline["prepared_cell_sha256"] != prepared.get("artifact_sha256")
            or baseline["generation_result_sha256"] != generation_sha256
            or baseline["receipt_sha256"] != receipt.get("result_sha256")
            or baseline["source_cell_sha256"] != source_sha256
            or run_cell.get("prepared_cell_sha256") != prepared.get("artifact_sha256")
            or run_cell.get("generation_result_sha256") != generation_sha256
            or run_cell.get("receipt_sha256") != receipt.get("result_sha256")
            or target.get("source_cell_sha256") != source_sha256
            or target.get("receipt_sha256") != receipt.get("result_sha256")
            or target.get("status") != receipt.get("status")
            or target.get("is_correct") != receipt.get("is_correct")
            or target.get("provenance_complete") != receipt.get("provenance_complete")
            or target.get("overall_ordinal") != expected_reference["overall_ordinal"]
            or target.get("accepted") != expected_reference["accepted"]
        ):
            raise G3CalibrationError("selected calibration source commitment changed")
        rendered = prepared.get("rendered_context")
        question = task.get("question_text")
        answer = generation.get("answer")
        _visible_text_is_blind(
            question, answer, rendered, run_id=run_id, task_id=task_id
        )
        for row in rows:
            cell = packet_cells[(str(row["reviewer"]), int(row["position"]))]
            if (
                cell.get("question") != question
                or cell.get("frozen_answer") != answer
                or cell.get("supporting_rendered_evidence") != rendered
            ):
                raise G3CalibrationError(
                    "public packet content changed from its source"
                )

    if len(selected_runs) != G3_CALIBRATION_UNIQUE_CELL_COUNT or dict(
        configuration_counts
    ) != {key: G3_CALIBRATION_CELLS_PER_CONFIGURATION for key in _configuration_keys()}:
        raise G3CalibrationError("frozen calibration selection is not balanced")
    if len(repeated_ids) != G3_CALIBRATION_HIDDEN_REPEAT_COUNT:
        raise G3CalibrationError("frozen calibration does not contain two repeats")


def build_g3_calibration_packets(
    *,
    freeze: Mapping[str, Any],
    public_run: Mapping[str, Any],
    seed: bytes,
    public_directory: Path,
    external_identity_map: Path,
    external_reference: Path,
    root: Path | None = None,
) -> dict[str, Any]:
    """Build three blind 22-cell packets and the hash-only public manifest."""

    root_value = _lexical_repository_root(root or repository_root())
    seed_value = _seed_bytes(seed)
    identity_anchor, identity_relative, identity_path = _private_write_target(
        external_identity_map, root_value, "calibration identity map"
    )
    reference_anchor, reference_relative, reference_path = _private_write_target(
        external_reference, root_value, "calibration reference"
    )
    if identity_path == reference_path:
        raise G3CalibrationError("identity map and reference must be separate files")
    public_root = Path(os.path.abspath(public_directory))
    _, public_manifest_relative = _repository_write_relative(
        root_value,
        public_root / "manifest.json",
        "public calibration directory",
    )
    public_relative = public_manifest_relative.parent
    payloads = _workflow_payloads(
        root=root_value,
        freeze=freeze,
        public_run=public_run,
        seed=seed_value,
    )
    write_plan: list[tuple[Path, Path, bytes, str]] = [
        (
            identity_anchor,
            identity_relative,
            payloads["identity_bytes"],
            "calibration identity map",
        ),
        (
            reference_anchor,
            reference_relative,
            payloads["reference_bytes"],
            "calibration reference",
        ),
    ]
    for relative_path, encoded in payloads["packet_bytes"].items():
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts or not relative.name:
            raise G3CalibrationError("calibration packet path is unsafe")
        write_plan.append(
            (
                root_value,
                public_relative / relative,
                encoded,
                "calibration packet",
            )
        )
    for relative_path, encoded in payloads["response_template_bytes"].items():
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts or not relative.name:
            raise G3CalibrationError("calibration response template path is unsafe")
        write_plan.append(
            (
                root_value,
                public_relative / relative,
                encoded,
                "calibration response template",
            )
        )
    write_plan.append(
        (
            root_value,
            public_manifest_relative,
            payloads["manifest_bytes"],
            "calibration manifest",
        )
    )

    created: list[_CreatedCalibrationArtifact] = []
    try:
        for anchor, relative, encoded, label in write_plan:
            result = _write_once_or_verify(anchor, relative, encoded, label)
            if result is not None:
                created.append(result)
        validate_g3_calibration_bundle(
            payloads["manifest"],
            freeze=freeze,
            public_run=public_run,
            seed=seed_value,
            public_directory=public_root,
            external_identity_map=identity_path,
            external_reference=reference_path,
            root=root_value,
        )
    except Exception:
        _rollback_created_artifacts(created)
        raise
    return payloads["manifest"]


def _packet_record(manifest: Mapping[str, Any], reviewer: str) -> Mapping[str, Any]:
    records = manifest.get("packets")
    if not isinstance(records, list):
        raise G3CalibrationError("calibration manifest has no packet records")
    matches = [
        row
        for row in records
        if isinstance(row, Mapping) and row.get("reviewer") == reviewer
    ]
    if len(matches) != 1:
        raise G3CalibrationError(
            f"{reviewer}: calibration packet identity is ambiguous"
        )
    return matches[0]


def _validate_return_comment(value: str) -> None:
    if (
        _TASK_ID_LEAK.search(value)
        or _POLICY_LEAK.search(value)
        or _EFFORT_LEAK.search(value)
        or _IDENTITY_FIELD_LEAK.search(value)
    ):
        raise G3CalibrationError(
            "calibration review comment exposes a blinded identity"
        )


def _validate_grade_without_identity_leak(grade: object) -> dict[str, Any]:
    if not isinstance(grade, Mapping):
        raise G3CalibrationError("calibration return grade must be an object")
    normalized = dict(grade)
    try:
        validate_grade(normalized)
    except ReviewContractError as exc:
        raise G3CalibrationError(f"calibration return grade is invalid: {exc}") from exc
    labels = normalized["failure_labels"]
    if any(not isinstance(label, str) for label in labels):
        raise G3CalibrationError("calibration failure labels must be text")
    visible = json.dumps(
        {"comment": normalized["comment"], "failure_labels": labels},
        ensure_ascii=False,
        sort_keys=True,
    )
    if (
        _TASK_ID_LEAK.search(visible)
        or _POLICY_LEAK.search(visible)
        or _EFFORT_LEAK.search(visible)
        or _IDENTITY_FIELD_LEAK.search(visible)
    ):
        raise G3CalibrationError("calibration grade exposes a blinded identity")
    return normalized


def load_g3_calibration_response_template(
    reviewer: str,
    *,
    manifest: Mapping[str, Any],
    public_directory: Path,
    root: Path | None = None,
) -> dict[str, Any]:
    """Load the hash-bound, intentionally incomplete save/resume template."""

    validate_g3_calibration_manifest(
        manifest, public_directory=public_directory, root=root
    )
    if reviewer not in REVIEWERS:
        raise G3CalibrationError("unknown calibration reviewer")
    record = _packet_record(manifest, reviewer)
    path = _contained_path(
        public_directory.resolve(),
        record["response_template_path"],
        "calibration response template path",
    )
    return _read_json(path, "blank calibration response template")


def finalize_g3_calibration_response_template(
    value: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    public_directory: Path,
    root: Path | None = None,
) -> dict[str, Any]:
    """Turn a filled copy of a blank template into a signed completed return."""

    if not isinstance(value, Mapping) or set(value) != _RETURN_FIELDS:
        raise G3CalibrationError("filled calibration response template fields differ")
    reviewer = value.get("reviewer")
    if reviewer not in REVIEWERS:
        raise G3CalibrationError("filled calibration response reviewer is invalid")
    blank = load_g3_calibration_response_template(
        str(reviewer),
        manifest=manifest,
        public_directory=public_directory,
        root=root,
    )
    for field in (
        "schema_version",
        "reviewer",
        "packet_id",
        "packet_sha256",
        "identity_map_sha256",
    ):
        if value.get(field) != blank.get(field):
            raise G3CalibrationError(
                "filled calibration response template identity changed"
            )
    if (
        value.get("review_manifest_sha256")
        not in {
            None,
            manifest.get("artifact_sha256"),
        }
        or value.get("artifact_sha256") is not None
    ):
        raise G3CalibrationError("filled calibration response template hash is stale")
    rows = value.get("grades")
    blank_rows = blank["grades"]
    if (
        not isinstance(rows, list)
        or len(rows) != G3_CALIBRATION_PACKET_CELL_COUNT
        or [row.get("blind_cell_id") for row in rows if isinstance(row, Mapping)]
        != [row["blind_cell_id"] for row in blank_rows]
        or any(
            not isinstance(row, Mapping) or set(row) != _RETURN_GRADE_FIELDS
            for row in rows
        )
    ):
        raise G3CalibrationError("filled calibration response grades are incomplete")
    grades = {str(row["blind_cell_id"]): row["grade"] for row in rows}
    return build_g3_calibration_return(
        grades,
        reviewer=str(reviewer),
        rubric_ambiguous=value.get("rubric_ambiguous"),
        review_comment=value.get("review_comment"),
        manifest=manifest,
        public_directory=public_directory,
        root=root,
    )


def build_g3_calibration_return(
    grades_by_blind_id: Mapping[str, Mapping[str, Any]],
    *,
    reviewer: str,
    rubric_ambiguous: bool,
    manifest: Mapping[str, Any],
    public_directory: Path,
    review_comment: str = "",
    root: Path | None = None,
) -> dict[str, Any]:
    """Build one strictly bound reviewer return from 22 supplied grades."""

    validate_g3_calibration_manifest(
        manifest, public_directory=public_directory, root=root
    )
    if (
        reviewer not in REVIEWERS
        or not isinstance(rubric_ambiguous, bool)
        or not isinstance(review_comment, str)
    ):
        raise G3CalibrationError("calibration return reviewer or ambiguity is invalid")
    _validate_return_comment(review_comment)
    record = _packet_record(manifest, reviewer)
    packet_path = _contained_path(
        public_directory.resolve(), record["path"], "calibration packet path"
    )
    packet = _read_json(packet_path, "public calibration packet")
    blind_ids = [str(cell["blind_cell_id"]) for cell in packet["cells"]]
    if set(grades_by_blind_id) != set(blind_ids):
        raise G3CalibrationError("calibration return grades are incomplete or extra")
    grades = [
        {
            "blind_cell_id": blind_id,
            "grade": _validate_grade_without_identity_leak(
                grades_by_blind_id[blind_id]
            ),
        }
        for blind_id in blind_ids
    ]
    artifact: dict[str, Any] = {
        "schema_version": G3_CALIBRATION_RETURN_SCHEMA,
        "reviewer": reviewer,
        "packet_id": record["packet_id"],
        "packet_sha256": record["packet_sha256"],
        "review_manifest_sha256": manifest["artifact_sha256"],
        "identity_map_sha256": manifest["identity_map_sha256"],
        "grades": grades,
        "rubric_ambiguous": rubric_ambiguous,
        "review_comment": review_comment,
    }
    artifact["artifact_sha256"] = sha256_json(artifact)
    validate_g3_calibration_return(
        artifact,
        manifest=manifest,
        public_directory=public_directory,
        root=root,
    )
    return artifact


def validate_g3_calibration_return(
    value: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    public_directory: Path,
    root: Path | None = None,
) -> None:
    """Require all 22 grades and exact reviewer, packet, and hash identities."""

    validate_g3_calibration_manifest(
        manifest, public_directory=public_directory, root=root
    )
    if not isinstance(value, Mapping) or set(value) != _RETURN_FIELDS:
        raise G3CalibrationError("calibration review return fields differ")
    body = {key: item for key, item in value.items() if key != "artifact_sha256"}
    if value.get("schema_version") != G3_CALIBRATION_RETURN_SCHEMA or value.get(
        "artifact_sha256"
    ) != sha256_json(body):
        raise G3CalibrationError("calibration review return hash is invalid")
    reviewer = value.get("reviewer")
    if reviewer not in REVIEWERS:
        raise G3CalibrationError("calibration review return reviewer is invalid")
    record = _packet_record(manifest, str(reviewer))
    if (
        value.get("packet_id") != record.get("packet_id")
        or value.get("packet_sha256") != record.get("packet_sha256")
        or value.get("review_manifest_sha256") != manifest.get("artifact_sha256")
        or value.get("identity_map_sha256") != manifest.get("identity_map_sha256")
    ):
        raise G3CalibrationError("calibration review return identity or hash is stale")
    if not isinstance(value.get("rubric_ambiguous"), bool):
        raise G3CalibrationError(
            "calibration review return needs an ambiguity decision"
        )
    if not isinstance(value.get("review_comment"), str):
        raise G3CalibrationError("calibration review return comment must be text")
    _validate_return_comment(str(value["review_comment"]))
    packet_path = _contained_path(
        public_directory.resolve(), record["path"], "calibration packet path"
    )
    packet = _read_json(packet_path, "public calibration packet")
    expected_ids = {str(cell["blind_cell_id"]) for cell in packet["cells"]}
    grades = value.get("grades")
    if not isinstance(grades, list) or len(grades) != G3_CALIBRATION_PACKET_CELL_COUNT:
        raise G3CalibrationError("calibration review return must contain all 22 grades")
    seen: set[str] = set()
    for row in grades:
        if not isinstance(row, Mapping) or set(row) != _RETURN_GRADE_FIELDS:
            raise G3CalibrationError("calibration review grade row fields differ")
        blind_id = row.get("blind_cell_id")
        if not isinstance(blind_id, str) or blind_id in seen:
            raise G3CalibrationError("calibration review return repeats a blind ID")
        seen.add(blind_id)
        _validate_grade_without_identity_leak(row.get("grade"))
    if seen != expected_ids:
        raise G3CalibrationError("calibration review grades are incomplete or extra")


def _g3_calibration_native_bindings(
    *,
    manifest: Mapping[str, Any],
    reviewer: str,
    confirmation: Mapping[str, Any],
    confirmation_file_sha256: str,
    public_directory: Path,
    root: Path,
) -> dict[str, str]:
    record = _packet_record(manifest, reviewer)
    try:
        public_relative = public_directory.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise G3CalibrationError(
            "G3 calibration directory must be inside the repository"
        ) from exc
    packet_path = public_relative / str(record["path"])
    return {
        "packet_id": str(record["packet_id"]),
        "packet_path": packet_path.as_posix(),
        "packet_sha256": str(record["packet_sha256"]),
        "packet_token_preflight_sha256": sha256_json(
            confirmation["confirmed_preflights"][reviewer]
        ),
        "review_manifest_sha256": str(manifest["artifact_sha256"]),
        "rubric_sha256": str(manifest["rubric_sha256"]),
        "token_confirmation_artifact_sha256": str(confirmation["artifact_sha256"]),
        "token_confirmation_file_sha256": confirmation_file_sha256,
    }


def _g3_calibration_native_response(
    completed: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "grades": [dict(row) for row in completed["grades"]],
        "rubric_ambiguous": bool(completed["rubric_ambiguous"]),
        "review_comment": str(completed["review_comment"]),
    }


def validate_g3_calibration_native_review(
    *,
    reviewer: str,
    manifest: Mapping[str, Any],
    completed: Mapping[str, Any],
    receipt: Mapping[str, Any],
    confirmation: Mapping[str, Any],
    confirmation_file_sha256: str,
    public_directory: Path,
    root: Path,
) -> dict[str, Any]:
    """Replay the fixed CLI, isolated workspace, session, and raw AI output."""

    anchor = (
        public_directory.resolve() / reviewer / "invocation-receipt.json"
    ).relative_to(root.resolve())
    bindings = _g3_calibration_native_bindings(
        manifest=manifest,
        reviewer=reviewer,
        confirmation=confirmation,
        confirmation_file_sha256=confirmation_file_sha256,
        public_directory=public_directory,
        root=root,
    )
    try:
        evidence = validate_recorded_ai_review(
            root,
            anchor_path=anchor,
            reviewer_id=reviewer,
            review_kind="g3-calibration",
            target_bindings=bindings,
            expected_response=_g3_calibration_native_response(completed),
            invocation_id=str(receipt.get("native_invocation_id", "")),
            completed_at=str(receipt.get("completed_at", "")),
        )
        assert_native_proof_fields(
            anchor_path=anchor,
            receipt=receipt,
            evidence=evidence,
        )
    except AIReviewInvocationError as exc:
        raise G3CalibrationError(
            f"{reviewer}: native calibration invocation proof is invalid"
        ) from exc
    return evidence


def run_g3_calibration_ai_review(
    *,
    reviewer: str,
    manifest: Mapping[str, Any],
    public_directory: Path,
    root: Path | None = None,
    timeout_seconds: int = 1_800,
) -> dict[str, Any]:
    """Run one post-confirmation AI calibration and persist exact native proof."""

    if reviewer not in AI_REVIEWERS:
        raise G3CalibrationError("G3 native calibration reviewer is invalid")
    root_value = (root or repository_root()).resolve()
    public_root = public_directory.resolve()
    validate_g3_calibration_manifest(
        manifest,
        public_directory=public_root,
        root=root_value,
    )
    confirmation, confirmation_file_sha256 = validate_g3_calibration_token_confirmation(
        manifest,
        public_directory=public_root,
        root=root_value,
    )
    record = _packet_record(manifest, reviewer)
    packet_path = _contained_path(
        public_root,
        str(record["path"]),
        f"{reviewer} calibration packet",
    )
    packet = _read_json(packet_path, f"{reviewer} calibration packet")
    expected_blind_ids = [str(row["blind_cell_id"]) for row in packet["cells"]]
    reviewer_root = public_root / reviewer
    for filename in ("completed-return.json", "invocation-receipt.json"):
        target = reviewer_root / filename
        if target.exists() or target.is_symlink():
            raise G3CalibrationError(
                f"{reviewer}: calibration review artifact already exists"
            )

    built_return: dict[str, Any] | None = None

    def validate_exact_response(response: Mapping[str, Any]) -> None:
        nonlocal built_return
        rows = response.get("grades")
        if (
            not isinstance(rows, list)
            or [
                row.get("blind_cell_id") if isinstance(row, Mapping) else None
                for row in rows
            ]
            != expected_blind_ids
        ):
            raise AIReviewInvocationError(
                "G3 calibration native response order or identity changed"
            )
        grades = {
            str(row["blind_cell_id"]): dict(row["grade"])
            for row in rows
            if isinstance(row, Mapping) and isinstance(row.get("grade"), Mapping)
        }
        built_return = build_g3_calibration_return(
            grades,
            reviewer=reviewer,
            rubric_ambiguous=bool(response["rubric_ambiguous"]),
            review_comment=str(response["review_comment"]),
            manifest=manifest,
            public_directory=public_root,
            root=root_value,
        )

    anchor = (reviewer_root / "invocation-receipt.json").relative_to(root_value)
    bindings = _g3_calibration_native_bindings(
        manifest=manifest,
        reviewer=reviewer,
        confirmation=confirmation,
        confirmation_file_sha256=confirmation_file_sha256,
        public_directory=public_root,
        root=root_value,
    )
    try:
        native = run_and_record_ai_review(
            root_value,
            anchor_path=anchor,
            reviewer_id=reviewer,
            review_kind="g3-calibration",
            target_bindings=bindings,
            timeout_seconds=timeout_seconds,
            response_validator=validate_exact_response,
        )
    except AIReviewInvocationError as exc:
        raise G3CalibrationError(
            f"{reviewer}: native calibration invocation failed"
        ) from exc
    if built_return is None:
        raise G3CalibrationError(f"{reviewer}: calibration return was not built")
    completed_path = reviewer_root / "completed-return.json"
    _write_public_record_once(
        root_value,
        completed_path,
        built_return,
        f"{reviewer} completed calibration return",
    )
    evidence = native["evidence"]
    policy = G3_CALIBRATION_AI_INVOCATIONS[reviewer]
    receipt: dict[str, Any] = {
        "schema_version": G3_CALIBRATION_INVOCATION_RECEIPT_SCHEMA,
        "reviewer": reviewer,
        **policy,
        "status": "completed",
        "recorded_at": evidence["completed_at"],
        "review_manifest_sha256": manifest["artifact_sha256"],
        "token_confirmation_artifact_sha256": confirmation["artifact_sha256"],
        "token_confirmation_file_sha256": confirmation_file_sha256,
        "packet_token_preflight_sha256": sha256_json(
            confirmation["confirmed_preflights"][reviewer]
        ),
        "rubric_ambiguous": built_return["rubric_ambiguous"],
        "grade_count": G3_CALIBRATION_PACKET_CELL_COUNT,
        "completed_return_artifact_sha256": built_return["artifact_sha256"],
        "completed_return_file_sha256": _file_sha256(completed_path),
        "native_invocation_id": evidence["native_invocation_id"],
        **native_proof_fields(anchor, evidence),
    }
    receipt["artifact_sha256"] = sha256_json(receipt)
    receipt_path = reviewer_root / "invocation-receipt.json"
    _write_public_record_once(
        root_value,
        receipt_path,
        receipt,
        f"{reviewer} calibration invocation receipt",
    )
    validate_g3_calibration_native_review(
        reviewer=reviewer,
        manifest=manifest,
        completed=built_return,
        receipt=receipt,
        confirmation=confirmation,
        confirmation_file_sha256=confirmation_file_sha256,
        public_directory=public_root,
        root=root_value,
    )
    return {
        "completed_return": built_return,
        "invocation_receipt": receipt,
        "native_evidence": evidence,
    }


def _grades_by_blind_id(value: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row["blind_cell_id"]): dict(row["grade"]) for row in value["grades"]}


def _ai_invocation_commitments(
    *,
    manifest: Mapping[str, Any],
    public_directory: Path,
    review_returns: Mapping[str, Mapping[str, Any]],
    root: Path,
) -> list[dict[str, str]]:
    """Validate the saved AI receipts and return content-free hash commitments."""

    kevin_receipt = public_directory.resolve() / "kevin/invocation-receipt.json"
    if kevin_receipt.exists() or kevin_receipt.is_symlink():
        raise G3CalibrationError("Kevin must not have an AI invocation receipt")

    confirmation, confirmation_file_sha256 = validate_g3_calibration_token_confirmation(
        manifest,
        public_directory=public_directory,
        root=root,
    )
    confirmation_artifact_sha256 = str(confirmation["artifact_sha256"])
    confirmed_at = datetime.strptime(
        str(confirmation["confirmed_at"]), "%Y-%m-%dT%H:%M:%SZ"
    )

    commitments: list[dict[str, str]] = []
    required_receipt_fields = {
        "schema_version",
        "reviewer",
        "invocation",
        "requested_model",
        "reasoning_effort",
        "status",
        "recorded_at",
        "review_manifest_sha256",
        "token_confirmation_artifact_sha256",
        "token_confirmation_file_sha256",
        "packet_token_preflight_sha256",
        "rubric_ambiguous",
        "grade_count",
        "completed_return_artifact_sha256",
        "completed_return_file_sha256",
        "artifact_sha256",
    }
    for reviewer in AI_REVIEWERS:
        completed_path = _contained_path(
            public_directory.resolve(),
            f"{reviewer}/completed-return.json",
            f"{reviewer} completed-return path",
        )
        completed = _read_json(completed_path, f"{reviewer} completed return")
        returned = review_returns[reviewer]
        if completed != returned:
            raise G3CalibrationError(
                f"{reviewer}: completed-return artifact differs from the supplied return"
            )
        completed_file_sha256 = _file_sha256(completed_path)

        receipt_path = _contained_path(
            public_directory.resolve(),
            f"{reviewer}/invocation-receipt.json",
            f"{reviewer} invocation-receipt path",
        )
        receipt = _read_json(receipt_path, f"{reviewer} invocation receipt")
        body = {key: item for key, item in receipt.items() if key != "artifact_sha256"}
        expected = G3_CALIBRATION_AI_INVOCATIONS[reviewer]
        try:
            recorded_at = datetime.strptime(
                str(receipt.get("recorded_at")), "%Y-%m-%dT%H:%M:%SZ"
            )
        except ValueError as exc:
            raise G3CalibrationError(
                f"{reviewer}: AI invocation timestamps are invalid"
            ) from exc
        packet_preflight_sha256 = sha256_json(
            confirmation["confirmed_preflights"][reviewer]
        )
        if (
            not required_receipt_fields.issubset(receipt)
            or receipt.get("schema_version") != G3_CALIBRATION_INVOCATION_RECEIPT_SCHEMA
            or receipt.get("artifact_sha256") != sha256_json(body)
            or receipt.get("reviewer") != reviewer
            or any(receipt.get(key) != value for key, value in expected.items())
            or receipt.get("status") != "completed"
            or recorded_at <= confirmed_at
            or receipt.get("review_manifest_sha256") != manifest.get("artifact_sha256")
            or receipt.get("token_confirmation_artifact_sha256")
            != confirmation_artifact_sha256
            or receipt.get("token_confirmation_file_sha256") != confirmation_file_sha256
            or receipt.get("packet_token_preflight_sha256") != packet_preflight_sha256
            or receipt.get("grade_count") != G3_CALIBRATION_PACKET_CELL_COUNT
            or receipt.get("rubric_ambiguous") != returned.get("rubric_ambiguous")
            or receipt.get("completed_return_artifact_sha256")
            != returned.get("artifact_sha256")
            or receipt.get("completed_return_file_sha256") != completed_file_sha256
        ):
            raise G3CalibrationError(
                f"{reviewer}: AI invocation receipt is missing, stale, or mismatched"
            )
        if not _is_sha256(receipt.get("artifact_sha256")):
            raise G3CalibrationError(
                f"{reviewer}: AI invocation receipt hash is invalid"
            )
        commitments.append(
            {
                "reviewer": reviewer,
                **expected,
                "review_manifest_sha256": str(manifest["artifact_sha256"]),
                "token_confirmation_artifact_sha256": confirmation_artifact_sha256,
                "token_confirmation_file_sha256": confirmation_file_sha256,
                "packet_token_preflight_sha256": packet_preflight_sha256,
                "completed_return_artifact_sha256": str(returned["artifact_sha256"]),
                "completed_return_file_sha256": completed_file_sha256,
                "invocation_receipt_artifact_sha256": str(receipt["artifact_sha256"]),
                "invocation_receipt_file_sha256": _file_sha256(receipt_path),
            }
        )
    return commitments


def build_g3_calibration_panel(
    *,
    manifest: Mapping[str, Any],
    review_returns: Mapping[str, Mapping[str, Any]],
    freeze: Mapping[str, Any],
    public_run: Mapping[str, Any],
    seed: bytes | None = None,
    public_directory: Path,
    external_identity_map: Path,
    external_reference: Path,
    root: Path | None = None,
) -> dict[str, Any]:
    """Unblind all three complete returns into the final public G3 panel artifact."""

    root_value = (root or repository_root()).resolve()
    if seed is None:
        validate_g3_calibration_frozen_bundle(
            manifest,
            freeze=freeze,
            public_run=public_run,
            public_directory=public_directory,
            external_identity_map=external_identity_map,
            external_reference=external_reference,
            root=root_value,
        )
    else:
        validate_g3_calibration_bundle(
            manifest,
            freeze=freeze,
            public_run=public_run,
            seed=seed,
            public_directory=public_directory,
            external_identity_map=external_identity_map,
            external_reference=external_reference,
            root=root_value,
        )
    if not isinstance(review_returns, Mapping) or set(review_returns) != set(REVIEWERS):
        raise G3CalibrationError(
            "final calibration requires all three reviewer returns"
        )
    normalized_returns: dict[str, Mapping[str, Any]] = {}
    for reviewer in REVIEWERS:
        returned = review_returns[reviewer]
        validate_g3_calibration_return(
            returned,
            manifest=manifest,
            public_directory=public_directory,
            root=root_value,
        )
        if returned.get("reviewer") != reviewer:
            raise G3CalibrationError("review return is stored under the wrong reviewer")
        normalized_returns[reviewer] = returned

    invocation_commitments = _ai_invocation_commitments(
        manifest=manifest,
        public_directory=public_directory,
        review_returns=normalized_returns,
        root=root_value,
    )

    identity_path = _outside_root(
        external_identity_map, root_value, "calibration identity map"
    )
    reference_path = _outside_root(
        external_reference, root_value, "calibration reference"
    )
    identity, identity_file_sha256 = _read_private_json_snapshot(
        identity_path, root_value, "external calibration identity map"
    )
    reference, reference_file_sha256 = _read_private_json_snapshot(
        reference_path, root_value, "external calibration reference"
    )
    if identity_file_sha256 != manifest.get(
        "identity_map_sha256"
    ) or reference_file_sha256 != manifest.get("reference_sha256"):
        raise G3CalibrationError(
            "external calibration artifact changed after validation"
        )
    identities = identity["identities"]
    targets = {str(row["canonical_cell_id"]): row for row in reference["targets"]}
    grade_maps = {
        reviewer: _grades_by_blind_id(normalized_returns[reviewer])
        for reviewer in REVIEWERS
    }
    indexed: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for row in identities:
        key = (
            str(row["reviewer"]),
            str(row["canonical_cell_id"]),
            str(row["occurrence"]),
        )
        if key in indexed:
            raise G3CalibrationError("external identity map repeats an occurrence")
        indexed[key] = row

    canonical_ids = sorted(targets)
    cells: list[dict[str, Any]] = []
    panels: dict[str, dict[str, dict[str, Any]]] = {}
    for canonical_id in canonical_ids:
        panel: dict[str, dict[str, Any]] = {}
        source_hashes: set[str] = set()
        for reviewer in REVIEWERS:
            identity_row = indexed.get((reviewer, canonical_id, "original"))
            if identity_row is None:
                raise G3CalibrationError("external identity map lacks an original")
            blind_id = str(identity_row["blind_cell_id"])
            grade = grade_maps[reviewer].get(blind_id)
            if grade is None:
                raise G3CalibrationError("review return lacks an original grade")
            panel[reviewer] = grade
            source_hashes.add(str(identity_row["source_cell_sha256"]))
        target = targets[canonical_id]
        source_hashes.add(str(target["source_cell_sha256"]))
        if len(source_hashes) != 1:
            raise G3CalibrationError("unblinded source commitments disagree")
        panels[canonical_id] = panel
        cells.append(
            {
                "canonical_cell_id": canonical_id,
                "source_cell_sha256": source_hashes.pop(),
                "reference_grade": {
                    "overall_ordinal": target["overall_ordinal"],
                    "accepted": target["accepted"],
                },
                "individual_grades": panel,
            }
        )

    repeated_ids = sorted(
        {
            canonical_id
            for (_reviewer, canonical_id, occurrence) in indexed
            if occurrence == "hidden_repeat"
        }
    )
    if len(repeated_ids) != G3_CALIBRATION_HIDDEN_REPEAT_COUNT:
        raise G3CalibrationError(
            "external identity map does not contain two hidden repeats"
        )
    hidden_repeats: list[dict[str, Any]] = []
    for canonical_id in repeated_ids:
        repeat_panel: dict[str, dict[str, Any]] = {}
        for reviewer in REVIEWERS:
            identity_row = indexed.get((reviewer, canonical_id, "hidden_repeat"))
            if identity_row is None:
                raise G3CalibrationError("hidden repeat is not shared by all reviewers")
            grade = grade_maps[reviewer].get(str(identity_row["blind_cell_id"]))
            if grade is None:
                raise G3CalibrationError("review return lacks a hidden-repeat grade")
            repeat_panel[reviewer] = grade
        source_hash = str(targets[canonical_id]["source_cell_sha256"])
        hidden_repeats.append(
            {
                "repeat_id": "G3R-"
                + hashlib.sha256(canonical_id.encode("utf-8")).hexdigest()[:24],
                "kind": "hidden_repeat",
                "source_cell_sha256": source_hash,
                "original_grades": panels[canonical_id],
                "repeat_grades": repeat_panel,
            }
        )
    ambiguity = {
        reviewer: bool(normalized_returns[reviewer]["rubric_ambiguous"])
        for reviewer in REVIEWERS
    }
    try:
        panel = _build_g3_panel_calibration(
            cells,
            review_manifest_sha256=str(manifest["artifact_sha256"]),
            identity_map_sha256=str(manifest["identity_map_sha256"]),
            ai_invocation_receipts=invocation_commitments,
            rubric_ambiguity_by_reviewer=ambiguity,
            hidden_repeats=hidden_repeats,
        )
        validate_g3_panel_calibration(panel)
    except Exception as exc:
        raise G3CalibrationError(
            f"final G3 panel calibration is invalid: {exc}"
        ) from exc
    return panel


def validate_g3_calibration_panel(
    value: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    review_returns: Mapping[str, Mapping[str, Any]],
    freeze: Mapping[str, Any],
    public_run: Mapping[str, Any],
    seed: bytes | None = None,
    public_directory: Path,
    external_identity_map: Path,
    external_reference: Path,
    root: Path | None = None,
) -> None:
    """Rebuild the panel and reject a stale or manually altered public artifact."""

    try:
        validate_g3_panel_calibration(value)
    except Exception as exc:
        raise G3CalibrationError(
            f"supplied G3 panel calibration is invalid: {exc}"
        ) from exc
    expected = build_g3_calibration_panel(
        manifest=manifest,
        review_returns=review_returns,
        freeze=freeze,
        public_run=public_run,
        seed=seed,
        public_directory=public_directory,
        external_identity_map=external_identity_map,
        external_reference=external_reference,
        root=root,
    )
    if dict(value) != expected:
        raise G3CalibrationError("supplied G3 panel differs from the complete returns")


# Descriptive aliases for callers that name the workflow rather than the packet stage.
build_g3_calibration_workflow = build_g3_calibration_packets
validate_g3_calibration_workflow = validate_g3_calibration_bundle
build_g3_panel_calibration_from_returns = build_g3_calibration_panel
