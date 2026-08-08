"""Content-free import boundary for the 12 externally held G3 temporal tasks."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any

from .baseline import repository_root
from .memory_experiments import MEMORY_CONFIGURATIONS
from .provider import ALLOWED_REASONING_EFFORTS, MODEL_ID, PROVIDER_SLUG
from .tasking import sha256_json
from .temporal import event_history_sha256, sealed_temporal_references


G3_SEALED_CANDIDATE_SCHEMA = "contextlab.g3-sealed-candidate-manifest.v1"
G3_SEALED_RETURN_SCHEMA = "contextlab.g3-sealed-return.v1"
G3_SEALED_IMPORT_SCHEMA = "contextlab.g3-sealed-import.v1"
G3_SEALED_IMPORT_PATH = Path("results/v2/memory/g3_sealed_import.json")
G3_SEALED_TASK_IDS = tuple(f"T{number:03d}" for number in range(1, 13))
G3_SEALED_CELL_COUNT = 120
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_FORBIDDEN_KEY = re.compile(
    r"(?:question|gold|expected[_-]?answer|candidate[_-]?answer|answer[_-]?text|"
    r"rendered[_-]?context|retrieved[_-]?text|trace(?:$|[_-]content)|source[_-]?text)",
    re.IGNORECASE,
)
_FAILURE_LABELS = frozenset(
    {
        "wrong_value",
        "stale_value",
        "missing_provenance",
        "bad_abstention",
        "provider_failure",
        "other_permitted",
    }
)


class G3SealedError(ValueError):
    """A G3 sealed artifact is incomplete, leaked, or not frozen."""


def _sha(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise G3SealedError(f"{label} must be a lowercase SHA-256")
    return value


def _decimal(value: object, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise G3SealedError(f"{label} must be a finite decimal") from exc
    if not result.is_finite() or result < 0:
        raise G3SealedError(f"{label} must be a non-negative finite decimal")
    return result


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise G3SealedError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise G3SealedError(f"{label} must be finite and non-negative")
    return result


def _reject_sensitive_keys(value: object, path: str = "return") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or _FORBIDDEN_KEY.search(key):
                raise G3SealedError(
                    f"sealed return contains a forbidden field at {path}"
                )
            _reject_sensitive_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_sensitive_keys(item, f"{path}[{index}]")
    elif isinstance(value, str) and len(value) > 256:
        raise G3SealedError("sealed return contains an unbounded text value")


def _cell_identity(
    task_id: str,
    policy: str,
    effort: str,
    *,
    g3_freeze_sha256: str,
    external_bundle_sha256: str,
) -> dict[str, str]:
    run_id = f"g3-sealed-{policy}-{effort}-{task_id}"
    return {
        "task_id": task_id,
        "policy": policy,
        "reasoning_effort": effort,
        "run_id": run_id,
        "cell_sha256": sha256_json(
            {
                "schema_version": "contextlab.g3-sealed-cell.v1",
                "g3_freeze_sha256": g3_freeze_sha256,
                "external_bundle_sha256": external_bundle_sha256,
                "temporal_event_history_sha256": event_history_sha256(),
                "model": MODEL_ID,
                "provider": PROVIDER_SLUG,
                "task_id": task_id,
                "policy": policy,
                "reasoning_effort": effort,
                "run_id": run_id,
            }
        ),
    }


def build_g3_sealed_candidate_manifest(
    *, g3_freeze_sha256: str, external_bundle_sha256: str
) -> dict[str, Any]:
    """Freeze the content-free 12 x 5 x 2 external evaluation grid."""

    _sha(g3_freeze_sha256, "G3 freeze hash")
    _sha(external_bundle_sha256, "external temporal bundle hash")
    references = sealed_temporal_references()
    ids = tuple(str(row["task_id"]) for row in references)
    if ids != G3_SEALED_TASK_IDS:
        raise G3SealedError("sealed temporal task allocation changed")
    cells = [
        _cell_identity(
            task_id,
            policy,
            effort,
            g3_freeze_sha256=g3_freeze_sha256,
            external_bundle_sha256=external_bundle_sha256,
        )
        for task_id in G3_SEALED_TASK_IDS
        for policy in MEMORY_CONFIGURATIONS
        for effort in ALLOWED_REASONING_EFFORTS
    ]
    payload: dict[str, Any] = {
        "schema_version": G3_SEALED_CANDIDATE_SCHEMA,
        "g3_freeze_sha256": g3_freeze_sha256,
        "external_bundle_sha256": external_bundle_sha256,
        "temporal_event_history_sha256": event_history_sha256(),
        "requested_model": MODEL_ID,
        "provider": PROVIDER_SLUG,
        "task_ids": list(G3_SEALED_TASK_IDS),
        "memory_policies": list(MEMORY_CONFIGURATIONS),
        "reasoning_efforts": list(ALLOWED_REASONING_EFFORTS),
        "task_count": len(G3_SEALED_TASK_IDS),
        "cell_count": len(cells),
        "cells": cells,
    }
    payload["artifact_sha256"] = sha256_json(payload)
    validate_g3_sealed_candidate_manifest(payload)
    return payload


def validate_g3_sealed_candidate_manifest(value: Mapping[str, Any]) -> None:
    expected_fields = {
        "schema_version",
        "g3_freeze_sha256",
        "external_bundle_sha256",
        "temporal_event_history_sha256",
        "requested_model",
        "provider",
        "task_ids",
        "memory_policies",
        "reasoning_efforts",
        "task_count",
        "cell_count",
        "cells",
        "artifact_sha256",
    }
    body = {key: item for key, item in value.items() if key != "artifact_sha256"}
    if (
        not isinstance(value, Mapping)
        or set(value) != expected_fields
        or value.get("schema_version") != G3_SEALED_CANDIDATE_SCHEMA
        or value.get("artifact_sha256") != sha256_json(body)
        or value.get("temporal_event_history_sha256") != event_history_sha256()
        or value.get("requested_model") != MODEL_ID
        or value.get("provider") != PROVIDER_SLUG
        or value.get("task_ids") != list(G3_SEALED_TASK_IDS)
        or value.get("memory_policies") != list(MEMORY_CONFIGURATIONS)
        or value.get("reasoning_efforts") != list(ALLOWED_REASONING_EFFORTS)
        or value.get("task_count") != len(G3_SEALED_TASK_IDS)
        or value.get("cell_count") != G3_SEALED_CELL_COUNT
    ):
        raise G3SealedError("G3 sealed candidate manifest is invalid")
    freeze_sha = _sha(value.get("g3_freeze_sha256"), "G3 freeze hash")
    bundle_sha = _sha(
        value.get("external_bundle_sha256"), "external temporal bundle hash"
    )
    cells = value.get("cells")
    expected_cells = [
        _cell_identity(
            task_id,
            policy,
            effort,
            g3_freeze_sha256=freeze_sha,
            external_bundle_sha256=bundle_sha,
        )
        for task_id in G3_SEALED_TASK_IDS
        for policy in MEMORY_CONFIGURATIONS
        for effort in ALLOWED_REASONING_EFFORTS
    ]
    if cells != expected_cells:
        raise G3SealedError("G3 sealed candidate cells changed")


def _record_index(candidate: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(row["cell_sha256"]): row for row in candidate["cells"]}


def validate_g3_sealed_return(
    value: Mapping[str, Any], candidate_manifest: Mapping[str, Any]
) -> None:
    _reject_sensitive_keys(value)
    validate_g3_sealed_candidate_manifest(candidate_manifest)
    expected_fields = {
        "schema_version",
        "evaluation_id",
        "candidate_manifest_sha256",
        "g3_freeze_sha256",
        "external_bundle_sha256",
        "temporal_event_history_sha256",
        "requested_model",
        "provider",
        "records",
        "aggregate_metadata",
        "artifact_sha256",
    }
    body = {key: item for key, item in value.items() if key != "artifact_sha256"}
    if (
        not isinstance(value, Mapping)
        or set(value) != expected_fields
        or value.get("schema_version") != G3_SEALED_RETURN_SCHEMA
        or value.get("artifact_sha256") != sha256_json(body)
        or not isinstance(value.get("evaluation_id"), str)
        or _IDENTIFIER.fullmatch(str(value["evaluation_id"])) is None
        or value.get("candidate_manifest_sha256")
        != candidate_manifest.get("artifact_sha256")
        or value.get("g3_freeze_sha256") != candidate_manifest.get("g3_freeze_sha256")
        or value.get("external_bundle_sha256")
        != candidate_manifest.get("external_bundle_sha256")
        or value.get("temporal_event_history_sha256") != event_history_sha256()
        or value.get("requested_model") != MODEL_ID
        or value.get("provider") != PROVIDER_SLUG
    ):
        raise G3SealedError("G3 sealed return identity or hash is invalid")
    records = value.get("records")
    if not isinstance(records, list) or len(records) != G3_SEALED_CELL_COUNT:
        raise G3SealedError("G3 sealed return must contain exactly 120 records")
    candidates = _record_index(candidate_manifest)
    seen: set[str] = set()
    counts: Counter[str] = Counter()
    total_cost = Decimal("0")
    failed_count = 0
    completed_count = 0
    record_fields = {
        "task_id",
        "policy",
        "reasoning_effort",
        "run_id",
        "cell_sha256",
        "result_commitment_sha256",
        "status",
        "response_status",
        "is_correct",
        "stale",
        "provenance_complete",
        "correction_latency",
        "actual_usd",
        "latency_ms",
        "failure_labels",
    }
    for record in records:
        if not isinstance(record, Mapping) or set(record) != record_fields:
            raise G3SealedError("G3 sealed record fields are invalid")
        cell_sha = _sha(record.get("cell_sha256"), "sealed cell hash")
        candidate = candidates.get(cell_sha)
        identity = {
            key: record.get(key)
            for key in (
                "task_id",
                "policy",
                "reasoning_effort",
                "run_id",
                "cell_sha256",
            )
        }
        if candidate != identity or cell_sha in seen:
            raise G3SealedError("G3 sealed record is duplicate or outside the freeze")
        seen.add(cell_sha)
        counts[f"{record['policy']}:{record['reasoning_effort']}"] += 1
        _sha(record.get("result_commitment_sha256"), "sealed result commitment")
        status = record.get("status")
        response_status = record.get("response_status")
        labels = record.get("failure_labels")
        if (
            status not in {"completed", "failed"}
            or response_status not in {"answer", "abstain", "error"}
            or not all(
                isinstance(record.get(field), bool)
                for field in ("is_correct", "stale", "provenance_complete")
            )
            or not isinstance(labels, list)
            or len(labels) != len(set(labels))
            or any(label not in _FAILURE_LABELS for label in labels)
        ):
            raise G3SealedError("G3 sealed record outcome is invalid")
        cost = _decimal(record.get("actual_usd"), "sealed actual_usd")
        _number(record.get("latency_ms"), "sealed latency_ms")
        latency = record.get("correction_latency")
        if latency is not None:
            _number(latency, "sealed correction latency")
        if status == "completed":
            completed_count += 1
            if response_status == "error":
                raise G3SealedError("completed sealed record has an error status")
        else:
            failed_count += 1
            if (
                response_status != "error"
                or record.get("is_correct") is not False
                or record.get("stale") is not False
                or record.get("provenance_complete") is not False
                or latency is not None
                or not labels
            ):
                raise G3SealedError("failed sealed record fabricates an outcome")
        total_cost += cost
    if seen != set(candidates) or any(
        counts[f"{policy}:{effort}"] != len(G3_SEALED_TASK_IDS)
        for policy in MEMORY_CONFIGURATIONS
        for effort in ALLOWED_REASONING_EFFORTS
    ):
        raise G3SealedError("G3 sealed return coverage is incomplete")
    aggregate = value.get("aggregate_metadata")
    if not isinstance(aggregate, Mapping) or set(aggregate) != {
        "task_count",
        "cell_count",
        "completed_count",
        "failed_count",
        "actual_usd",
        "evaluator_version",
    }:
        raise G3SealedError("G3 sealed aggregate metadata is invalid")
    if (
        aggregate.get("task_count") != len(G3_SEALED_TASK_IDS)
        or aggregate.get("cell_count") != len(records)
        or aggregate.get("completed_count") != completed_count
        or aggregate.get("failed_count") != failed_count
        or _decimal(aggregate.get("actual_usd"), "sealed aggregate cost") != total_cost
        or not isinstance(aggregate.get("evaluator_version"), str)
        or _IDENTIFIER.fullmatch(str(aggregate["evaluator_version"])) is None
    ):
        raise G3SealedError("G3 sealed aggregate metadata differs from records")


def build_g3_sealed_metrics(imported: Mapping[str, Any]) -> dict[str, Any]:
    """Compute content-free sealed metrics from already validated records."""

    records = imported["records"]
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in records:
        grouped[f"{row['policy']}:{row['reasoning_effort']}"].append(row)
    metrics: dict[str, Any] = {}
    for key, rows in sorted(grouped.items()):
        completed = [row for row in rows if row["status"] == "completed"]
        metrics[key] = {
            "task_count": len(rows),
            "completed_count": len(completed),
            "failed_count": len(rows) - len(completed),
            "accuracy": (
                sum(bool(row["is_correct"]) for row in rows) / len(rows)
                if rows
                else None
            ),
            "stale_rate": (
                sum(bool(row["stale"]) for row in rows) / len(rows) if rows else None
            ),
            "provenance_completeness": (
                sum(bool(row["provenance_complete"]) for row in rows) / len(rows)
                if rows
                else None
            ),
            "mean_correction_latency": (
                sum(
                    float(row["correction_latency"])
                    for row in rows
                    if row["correction_latency"] is not None
                )
                / sum(row["correction_latency"] is not None for row in rows)
                if any(row["correction_latency"] is not None for row in rows)
                else None
            ),
            "actual_usd": str(
                sum((Decimal(str(row["actual_usd"])) for row in rows), Decimal("0"))
            ),
        }
    return metrics


def _lexical_repository_root(root: Path) -> Path:
    repository = Path(os.path.abspath(root))
    if repository.is_symlink() or not repository.is_dir():
        raise G3SealedError("G3 sealed repository root is missing or unsafe")
    return repository


def _repository_relative(repository: Path, path: Path, label: str) -> Path:
    requested = path if path.is_absolute() else repository / path
    absolute = Path(os.path.abspath(requested))
    try:
        relative = absolute.relative_to(repository)
    except ValueError as exc:
        raise G3SealedError(f"{label} must be inside the repository") from exc
    if not relative.parts or ".." in relative.parts or not relative.name:
        raise G3SealedError(f"{label} path is unsafe")
    return relative


def _external_file_target(path: Path, repository: Path) -> tuple[Path, Path]:
    """Return the closest approved existing parent and a lexical relative target."""

    absolute = Path(os.path.abspath(path))
    try:
        absolute.relative_to(repository)
    except ValueError:
        pass
    else:
        raise G3SealedError("G3 sealed return input must stay outside the repository")
    if absolute.is_symlink():
        raise G3SealedError("G3 sealed return input target is unsafe")

    missing: list[str] = []
    anchor = absolute.parent
    while True:
        if anchor.is_symlink():
            raise G3SealedError("G3 sealed return input parent is unsafe")
        try:
            metadata = anchor.stat()
        except FileNotFoundError:
            if anchor == anchor.parent:
                raise G3SealedError("G3 sealed return input parent is missing")
            missing.append(anchor.name)
            anchor = anchor.parent
            continue
        except OSError as exc:
            raise G3SealedError("cannot inspect G3 sealed return parent") from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise G3SealedError("G3 sealed return input parent is unsafe")
        break
    relative = Path(*reversed(missing), absolute.name)
    return anchor, relative


def _open_parent(
    anchor: Path, relative: Path, *, create: bool, label: str
) -> tuple[int, str]:
    if relative.is_absolute() or ".." in relative.parts or not relative.name:
        raise G3SealedError(f"{label} path is unsafe")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(anchor, flags)
    except OSError as exc:
        raise G3SealedError(f"cannot open {label} root safely") from exc
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
        raise G3SealedError(f"{label} contains an unsafe parent") from exc
    return descriptor, relative.name


def _read_existing_at(parent: int, name: str, label: str) -> bytes | None:
    try:
        metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise G3SealedError(f"{label} target is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent)
    except OSError as exc:
        raise G3SealedError(f"cannot read {label}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise G3SealedError(f"{label} target is unsafe")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            data = handle.read()
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise G3SealedError(f"{label} changed while it was read")
        return data
    except OSError as exc:
        raise G3SealedError(f"cannot read {label}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_file(anchor: Path, relative: Path, label: str) -> bytes:
    parent, name = _open_parent(anchor, relative, create=False, label=label)
    try:
        data = _read_existing_at(parent, name, label)
    finally:
        os.close(parent)
    if data is None:
        raise G3SealedError(f"cannot read {label}")
    return data


def _unlink_same_inode(parent: int, name: str, expected: os.stat_result) -> None:
    try:
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino):
            os.unlink(name, dir_fd=parent)
    except OSError:
        pass


def _write_once_or_identical(repository: Path, relative: Path, payload: bytes) -> None:
    parent, name = _open_parent(
        repository, relative, create=True, label="G3 sealed import"
    )
    temporary = f".{name}.{secrets.token_hex(12)}.tmp"
    temporary_created = False
    destination_created = False
    temporary_stat: os.stat_result | None = None
    try:
        current = _read_existing_at(parent, name, "existing G3 sealed import")
        if current is not None:
            if current != payload:
                raise G3SealedError(
                    "G3 sealed import already exists with different bytes"
                )
            return
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
            raise G3SealedError("G3 sealed import temporary target is unsafe")
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        linked_stat = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
        if (linked_stat.st_dev, linked_stat.st_ino) != (
            temporary_stat.st_dev,
            temporary_stat.st_ino,
        ):
            raise G3SealedError("G3 sealed import temporary target changed")
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
                raise G3SealedError("G3 sealed import changed during publication")
        except FileExistsError:
            concurrent = _read_existing_at(parent, name, "concurrent G3 sealed import")
            if concurrent != payload:
                raise G3SealedError(
                    "G3 sealed import already exists with different bytes"
                )
        _unlink_same_inode(parent, temporary, temporary_stat)
        temporary_created = False
        os.fsync(parent)
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


def import_g3_sealed_return(
    external_return: Path,
    candidate_manifest_path: Path,
    output_path: Path,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    root = _lexical_repository_root(root or repository_root())
    source_anchor, source_relative = _external_file_target(external_return, root)
    candidate_relative = _repository_relative(
        root, candidate_manifest_path, "G3 sealed candidate manifest"
    )
    requested_output = output_path if output_path.is_absolute() else root / output_path
    output = Path(os.path.abspath(requested_output))
    canonical_output = root / G3_SEALED_IMPORT_PATH
    if output != canonical_output:
        raise G3SealedError(
            f"G3 sealed import output must be {G3_SEALED_IMPORT_PATH.as_posix()}"
        )
    source_bytes = _read_file(
        source_anchor, source_relative, "external G3 sealed return"
    )
    candidate_bytes = _read_file(
        root, candidate_relative, "G3 sealed candidate manifest"
    )
    try:
        returned = json.loads(source_bytes.decode("utf-8"))
        candidate = json.loads(candidate_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G3SealedError("cannot read G3 sealed import inputs") from exc
    if not isinstance(returned, dict) or not isinstance(candidate, dict):
        raise G3SealedError("G3 sealed import inputs must be objects")
    validate_g3_sealed_return(returned, candidate)
    imported: dict[str, Any] = {
        "schema_version": G3_SEALED_IMPORT_SCHEMA,
        "evaluation_id": returned["evaluation_id"],
        "candidate_manifest_sha256": returned["candidate_manifest_sha256"],
        "g3_freeze_sha256": returned["g3_freeze_sha256"],
        "external_bundle_sha256": returned["external_bundle_sha256"],
        "temporal_event_history_sha256": returned["temporal_event_history_sha256"],
        "requested_model": returned["requested_model"],
        "provider": returned["provider"],
        "source_return_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "records": returned["records"],
        "aggregate_metadata": returned["aggregate_metadata"],
    }
    imported["sealed_metrics"] = build_g3_sealed_metrics(imported)
    imported["artifact_sha256"] = sha256_json(imported)
    _write_once_or_identical(
        root,
        G3_SEALED_IMPORT_PATH,
        (
            json.dumps(imported, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8"),
    )
    return imported
