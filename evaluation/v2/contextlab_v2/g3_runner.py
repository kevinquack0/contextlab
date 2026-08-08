"""Resumable preparation, paid generation, and objective grading for public G3."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
import json
import os
from pathlib import Path
import secrets
import stat
from typing import Any

from .baseline import repository_root
from .costs import CostLedger, canonical_ledger_path
from .g3_execution import (
    build_g3_preparation_context,
    prepare_public_g3_cell_with_context,
)
from .g3_freeze import validate_g3_freeze
from .g3_grading import build_public_g3_result_receipt
from .g3_static_grading import (
    build_public_static_grade_evidence,
    validate_public_static_grade_evidence,
)
from .gateway import run_paid_generation_to_file
from .generations import validate_saved_generation_result
from .tasking import sha256_json


G3_PUBLIC_RUN_SCHEMA = "contextlab.g3-public-generation-run.v1"


class G3RunnerError(ValueError):
    """The frozen public G3 run cannot prepare, resume, or grade safely."""


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise G3RunnerError(f"cannot read {label}") from exc
    if not isinstance(value, dict):
        raise G3RunnerError(f"{label} must be an object")
    return value


def _safe_parent_descriptor(root: Path, path: Path) -> tuple[int, str]:
    """Open an artifact parent without following repository-local symlinks."""

    repository = root.absolute()
    if repository.is_symlink() or not repository.is_dir():
        raise G3RunnerError("G3 repository root is missing or unsafe")
    try:
        relative = path.absolute().relative_to(repository)
    except ValueError as exc:
        raise G3RunnerError("G3 artifact path escaped the repository") from exc
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise G3RunnerError("G3 artifact path escaped the repository")

    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(repository, flags)
    except OSError as exc:
        raise G3RunnerError("cannot open G3 repository root safely") from exc
    try:
        for component in relative.parts[:-1]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o755, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except OSError as exc:
        os.close(descriptor)
        raise G3RunnerError("G3 artifact path contains an unsafe parent") from exc
    return descriptor, relative.name


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _open_json_at(parent: int, name: str, label: str) -> dict[str, Any] | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise G3RunnerError(f"{label} is unsafe") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise G3RunnerError(f"{label} is not a file")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            value = json.load(handle)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise G3RunnerError(f"{label} changed while it was read")
    except (OSError, json.JSONDecodeError) as exc:
        raise G3RunnerError(f"cannot read {label}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise G3RunnerError(f"{label} must be an object")
    return value


def _create_temporary(
    parent: int, name: str, data: bytes, *, mode: int = 0o600
) -> tuple[str, os.stat_result]:
    temporary = f".{name}.{secrets.token_hex(12)}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(temporary, flags, mode, dir_fd=parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        temporary_stat = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(temporary_stat.st_mode):
            raise G3RunnerError("G3 temporary artifact is not a file")
        return temporary, temporary_stat
    except Exception:
        try:
            os.unlink(temporary, dir_fd=parent)
        except OSError:
            pass
        raise


def _unlink_same_inode(parent: int, name: str, expected: os.stat_result) -> None:
    try:
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino):
            os.unlink(name, dir_fd=parent)
    except OSError:
        pass


def _atomic_write(
    path: Path, value: Mapping[str, Any], *, root: Path | None = None
) -> None:
    repository = (root or repository_root()).absolute()
    parent, name = _safe_parent_descriptor(repository, path)
    temporary: str | None = None
    try:
        try:
            existing = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise G3RunnerError("cannot inspect G3 artifact target safely") from exc
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise G3RunnerError("G3 artifact target is unsafe")
        temporary, _ = _create_temporary(parent, name, _json_bytes(value))
        os.replace(
            temporary,
            name,
            src_dir_fd=parent,
            dst_dir_fd=parent,
        )
        temporary = None
        os.fsync(parent)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=parent)
            except OSError:
                pass
        os.close(parent)


def _write_once_or_verify(
    path: Path, value: Mapping[str, Any], *, root: Path | None = None
) -> None:
    repository = (root or repository_root()).absolute()
    parent, name = _safe_parent_descriptor(repository, path)
    temporary: str | None = None
    destination_created = False
    temporary_stat: os.stat_result | None = None
    try:
        existing = _open_json_at(parent, name, "saved G3 artifact")
        if existing is not None:
            if existing != dict(value):
                raise G3RunnerError(f"saved G3 artifact changed: {path}")
            return
        temporary, temporary_stat = _create_temporary(
            parent, name, _json_bytes(value), mode=0o666
        )
        try:
            os.link(
                temporary,
                name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
                follow_symlinks=False,
            )
            destination_created = True
        except FileExistsError:
            concurrent = _open_json_at(parent, name, "concurrent G3 artifact")
            if concurrent != dict(value):
                raise G3RunnerError(f"concurrent G3 artifact changed: {path}")
            return
        os.unlink(temporary, dir_fd=parent)
        temporary = None
        os.fsync(parent)
    except Exception:
        if destination_created and temporary_stat is not None:
            _unlink_same_inode(parent, name, temporary_stat)
        raise
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=parent)
            except OSError:
                pass
        os.close(parent)


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise G3RunnerError("G3 artifact path escaped the repository") from exc


def _cell_paths(root: Path, spec: Mapping[str, Any]) -> dict[str, Path]:
    policy = str(spec["policy"])
    effort = str(spec["reasoning_effort"])
    task = spec["task"]
    task_id = str(task["task_id"])
    base = root / "results/v2"
    return {
        "prepared": base
        / "memory/prepared/g3-public-v1"
        / policy
        / effort
        / f"{task_id}.json",
        "generation": base
        / "generations/public/g3-public-v1"
        / policy
        / effort
        / f"{task_id}.json",
        "receipt": base
        / "memory/receipts/g3-public-v1"
        / policy
        / effort
        / f"{task_id}.json",
        "static_grade_evidence": base
        / "memory/grades/g3-public-v1"
        / policy
        / effort
        / f"{task_id}.json",
    }


def _prepare_cells(
    root: Path,
    freeze: Mapping[str, Any],
    static_lab: Mapping[str, Any],
    temporal_lab: Mapping[str, Any],
    selected_task_ids: set[str],
) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Path]]]:
    manifest = freeze["manifest"]
    trusted = str(manifest["frozen_manifest_sha256"])
    context = build_g3_preparation_context(
        manifest,
        trusted_frozen_manifest_sha256=trusted,
        static_r0_lab=static_lab,
        temporal_r0_lab=temporal_lab,
        root=root,
    )
    cells: list[tuple[dict[str, Any], dict[str, Any], dict[str, Path]]] = []
    for spec in manifest["run_specs"]:
        if selected_task_ids and str(spec["task"]["task_id"]) not in selected_task_ids:
            continue
        prepared = prepare_public_g3_cell_with_context(context, spec)
        paths = _cell_paths(root, spec)
        _write_once_or_verify(paths["prepared"], prepared, root=root)
        cells.append((dict(spec), prepared, paths))
    return cells


def _saved_generation_status(
    path: Path, spec: Mapping[str, Any]
) -> tuple[str, dict[str, Any] | None]:
    if not path.exists():
        return "missing", None
    value = _read_json(path, "saved G3 generation")
    schema = value.get("schema_version")
    if schema == "contextlab.generation-result.v1":
        try:
            validate_saved_generation_result(
                value,
                expected_run_id=str(spec["run_id"]),
                expected_task_id=str(spec["task"]["task_id"]),
                expected_effort=str(spec["reasoning_effort"]),
            )
        except ValueError as exc:
            raise G3RunnerError("saved G3 generation result is invalid") from exc
        return "completed", value
    if schema == "contextlab.failed-generation-result.v1" and value.get(
        "run_id"
    ) == spec.get("run_id"):
        return "failed", value
    if schema == "contextlab.pending-generation-result.v1" and value.get(
        "run_id"
    ) == spec.get("run_id"):
        raise G3RunnerError(
            f"G3 generation remains pending after interruption: {spec['run_id']}"
        )
    raise G3RunnerError("saved G3 generation file has an unsupported schema")


def _run_one_paid(
    root: Path,
    prepared: Mapping[str, Any],
    path: Path,
) -> tuple[str, dict[str, Any] | None, str | None]:
    try:
        result = run_paid_generation_to_file(
            prepared["generation_spec"],
            path,
            ledger=CostLedger(canonical_ledger_path(root)),
            root=root,
        )
        return "completed", result, None
    except Exception as exc:
        failed = _read_json(path, "failed G3 generation") if path.exists() else None
        message = (
            str(failed.get("error"))
            if isinstance(failed, Mapping) and failed.get("error")
            else str(exc)
        )
        return "failed", failed, message


def _receipt_for_cell(
    *,
    freeze: Mapping[str, Any],
    prepared: Mapping[str, Any],
    generation_status: str,
    generation: Mapping[str, Any] | None,
    failure: str | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    spec = prepared["run_spec"]
    static_evidence: dict[str, Any] | None = None
    if generation_status == "completed" and spec["task"]["suite"] == "static":
        assert generation is not None
        generation_sha256 = sha256_json(generation)
        static_evidence = build_public_static_grade_evidence(
            prepared,
            generation,
            saved_generation_result_sha256=generation_sha256,
        )
        validate_public_static_grade_evidence(
            static_evidence,
            prepared,
            generation,
            saved_generation_result_sha256=generation_sha256,
        )
    receipt = build_public_g3_result_receipt(
        prepared,
        generation if generation_status == "completed" else None,
        frozen_manifest=freeze["manifest"],
        trusted_frozen_manifest_sha256=freeze["manifest"]["frozen_manifest_sha256"],
        failure=failure if generation_status == "failed" else None,
    )
    if static_evidence is not None and receipt["grade_artifact"][
        "source_grade_sha256s"
    ] != [static_evidence["artifact_sha256"]]:
        raise G3RunnerError("static grade receipt source differs from its evidence")
    return receipt, static_evidence


def run_public_g3_generations(
    *,
    root: Path | None = None,
    freeze: Mapping[str, Any],
    static_lab: Mapping[str, Any],
    temporal_lab: Mapping[str, Any],
    max_new_calls: int | None = None,
    concurrency: int = 4,
    selected_task_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Prepare all cells, make only missing paid calls, and grade public temporal cells."""

    root = (root or repository_root()).absolute()
    validate_g3_freeze(freeze)
    if (
        isinstance(concurrency, bool)
        or not isinstance(concurrency, int)
        or not 1 <= concurrency <= 4
    ):
        raise G3RunnerError("G3 generation concurrency must be 1 through 4")
    if max_new_calls is not None and (
        isinstance(max_new_calls, bool)
        or not isinstance(max_new_calls, int)
        or max_new_calls < 0
    ):
        raise G3RunnerError("max_new_calls must be a non-negative integer")
    selected = set(selected_task_ids or ())
    unknown = selected.difference(
        str(spec["task"]["task_id"]) for spec in freeze["manifest"]["run_specs"]
    )
    if unknown:
        raise G3RunnerError(f"unknown G3 task IDs: {sorted(unknown)}")
    cells = _prepare_cells(root, freeze, static_lab, temporal_lab, selected)
    saved: dict[str, tuple[str, dict[str, Any] | None, str | None]] = {}
    pending: list[tuple[dict[str, Any], dict[str, Any], dict[str, Path]]] = []
    for spec, prepared, paths in cells:
        status, generation = _saved_generation_status(paths["generation"], spec)
        if status == "missing":
            pending.append((spec, prepared, paths))
        else:
            failure = (
                str(generation.get("error"))
                if status == "failed" and generation is not None
                else None
            )
            saved[str(spec["run_id"])] = (status, generation, failure)
    budget = len(pending) if max_new_calls is None else min(max_new_calls, len(pending))
    to_run = pending[:budget]
    if to_run:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {
                pool.submit(_run_one_paid, root, prepared, paths["generation"]): spec
                for spec, prepared, paths in to_run
            }
            for future in as_completed(futures):
                spec = futures[future]
                saved[str(spec["run_id"])] = future.result()
    records: list[dict[str, Any]] = []
    costs = Decimal("0")
    for spec, prepared, paths in cells:
        run_id = str(spec["run_id"])
        status, generation, failure = saved.get(run_id, ("missing", None, None))
        receipt: dict[str, Any] | None = None
        static_grade_evidence: dict[str, Any] | None = None
        grade_status = "generation_pending"
        if status in {"completed", "failed"}:
            receipt, static_grade_evidence = _receipt_for_cell(
                freeze=freeze,
                prepared=prepared,
                generation_status=status,
                generation=generation,
                failure=failure,
            )
            if static_grade_evidence is not None:
                _write_once_or_verify(
                    paths["static_grade_evidence"], static_grade_evidence, root=root
                )
            _write_once_or_verify(paths["receipt"], receipt, root=root)
            grade_status = "objective_completed" if status == "completed" else "failed"
        if status == "completed" and generation is not None:
            costs += Decimal(str(generation["metadata"]["actual_usd"]))
        records.append(
            {
                "run_id": run_id,
                "task_id": spec["task"]["task_id"],
                "suite": spec["task"]["suite"],
                "policy": spec["policy"],
                "reasoning_effort": spec["reasoning_effort"],
                "prepared_path": _relative(root, paths["prepared"]),
                "prepared_cell_sha256": prepared["artifact_sha256"],
                "generation_path": _relative(root, paths["generation"]),
                "generation_status": status,
                "generation_artifact_sha256": (
                    sha256_json(generation)
                    if status in {"completed", "failed"} and generation is not None
                    else None
                ),
                "generation_result_sha256": (
                    sha256_json(generation) if status == "completed" else None
                ),
                "grade_status": grade_status,
                "receipt_path": (
                    _relative(root, paths["receipt"]) if receipt is not None else None
                ),
                "receipt_sha256": (
                    receipt["result_sha256"] if receipt is not None else None
                ),
                "static_grade_evidence_path": (
                    _relative(root, paths["static_grade_evidence"])
                    if static_grade_evidence is not None
                    else None
                ),
                "static_grade_evidence_sha256": (
                    static_grade_evidence["artifact_sha256"]
                    if static_grade_evidence is not None
                    else None
                ),
            }
        )
    status_counts = Counter(row["generation_status"] for row in records)
    grade_counts = Counter(row["grade_status"] for row in records)
    payload: dict[str, Any] = {
        "schema_version": G3_PUBLIC_RUN_SCHEMA,
        "g3_freeze_sha256": freeze["artifact_sha256"],
        "frozen_manifest_sha256": freeze["manifest"]["frozen_manifest_sha256"],
        "selected_task_ids": sorted(selected),
        "expected_full_cell_count": len(freeze["manifest"]["run_specs"]),
        "recorded_cell_count": len(records),
        "new_call_count": len(to_run),
        "generation_status_counts": {
            key: status_counts.get(key, 0) for key in ("completed", "failed", "missing")
        },
        "grade_status_counts": {
            key: grade_counts.get(key, 0)
            for key in (
                "objective_completed",
                "panel_pending",
                "failed",
                "generation_pending",
            )
        },
        "completed_generation_cost_usd": str(costs),
        "cells": records,
    }
    payload["artifact_sha256"] = sha256_json(payload)
    output = root / "results/v2/memory/g3_public_generation_run.json"
    _atomic_write(output, payload, root=root)
    return payload


def load_public_g3_receipts(
    run_manifest: Mapping[str, Any], *, root: Path | None = None
) -> list[dict[str, Any]]:
    """Load every saved receipt and verify its path and committed hash."""

    root = (root or repository_root()).resolve()
    receipts: list[dict[str, Any]] = []
    for cell in run_manifest["cells"]:
        path_text = cell.get("receipt_path")
        if path_text is None:
            continue
        path = (root / str(path_text)).resolve()
        if not path.is_relative_to(root):
            raise G3RunnerError("G3 receipt path escaped the repository")
        receipt = _read_json(path, "G3 receipt")
        if receipt.get("result_sha256") != cell.get("receipt_sha256"):
            raise G3RunnerError("G3 receipt commitment changed")
        receipts.append(receipt)
    return receipts
