"""Recomputable lifecycle and replay evidence for the G3 memory gate."""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import secrets
import stat
from typing import Any

from .baseline import repository_root
from .memory import MemoryEngine
from .tasking import sha256_json
from .temporal import all_events, event_history_sha256, scenario_catalog


G3_LIFECYCLE_SCHEMA = "contextlab.g3-lifecycle-evidence.v1"
G3_LIFECYCLE_PATH = Path("results/v2/memory/g3_lifecycle_evidence.json")
_POLICIES = ("M0", "M1", "M2", "M3", "M4")


class G3LifecycleError(ValueError):
    """Lifecycle evidence differs from a deterministic replay of public events."""


def _scenario(scenario_id: str) -> tuple[Any, ...]:
    return next(
        scenario.events
        for scenario in scenario_catalog()
        if scenario.scenario_id == scenario_id
    )


def _read_commitment(read: Any) -> str:
    return sha256_json(read.to_record())


def _selected_claim_ids(read: Any) -> list[str]:
    return [claim.claim_id for claim in read.selected_claims]


def _replay_checks() -> list[dict[str, Any]]:
    events = all_events()
    rows: list[dict[str, Any]] = []
    for policy in _POLICIES:
        direct = MemoryEngine.rebuilt(policy, events)
        reverse = MemoryEngine.rebuilt(policy, reversed(events))
        snapshot = direct.snapshot_record()
        restored = MemoryEngine.from_snapshot_record(snapshot)
        rows.append(
            {
                "policy": policy,
                "event_count": len(events),
                "snapshot_id": snapshot["snapshot_id"],
                "snapshot_sha256": sha256_json(snapshot),
                "reverse_order_exact": reverse.snapshot_record() == snapshot,
                "restore_exact": restored.snapshot_record() == snapshot,
                "decision_count": len(snapshot["decision_ledger"]),
            }
        )
    return rows


def _lifecycle_checks() -> list[dict[str, Any]]:
    correction_events = _scenario("TL-05")
    correction = MemoryEngine.rebuilt("M3", correction_events)
    correction_before = correction.read(
        "Q2 activation",
        "rate",
        observed_through="2026-07-02T23:59:59Z",
    )
    correction_after = correction.read("Q2 activation", "rate")
    correction_passed = (
        len(correction_before.selected_claims) == 1
        and len(correction_after.selected_claims) == 1
        and correction_before.selected_claims[0].claim_id
        != correction_after.selected_claims[0].claim_id
        and correction_before.provenance_complete()
        and correction_after.provenance_complete()
        and "superseded" in {claim.state for claim in correction.claims}
    )

    expiry = MemoryEngine.rebuilt("M3", _scenario("TL-14"))
    expiry_current = expiry.read("Legacy discount", "rate")
    expiry_historical = expiry.read(
        "Legacy discount", "rate", as_of_time="2026-03-15T00:00:00Z"
    )
    expiry_passed = (
        not expiry_current.selected_claims
        and len(expiry_historical.selected_claims) == 1
        and expiry_historical.provenance_complete()
    )

    tombstone = MemoryEngine.rebuilt("M3", _scenario("TL-15"))
    tombstone_current = tombstone.read("Nimbus integration", "support_status")
    tombstone_historical = tombstone.read(
        "Nimbus integration",
        "support_status",
        as_of_time="2026-03-01T00:00:00Z",
    )
    tombstone_passed = (
        not tombstone_current.selected_claims
        and len(tombstone_historical.selected_claims) == 1
        and tombstone_historical.provenance_complete()
    )

    rollback_prefix = tuple(
        event
        for event in correction_events
        if event.observed_time <= "2026-07-02T23:59:59Z"
    )
    prefix_engine = MemoryEngine.rebuilt("M3", rollback_prefix)
    prefix_read = prefix_engine.read("Q2 activation", "rate")
    rollback_passed = (
        _selected_claim_ids(prefix_read) == _selected_claim_ids(correction_before)
        and prefix_read.provenance_complete()
        and MemoryEngine.from_snapshot_record(
            prefix_engine.snapshot_record()
        ).snapshot_record()
        == prefix_engine.snapshot_record()
    )

    return [
        {
            "case": "correction",
            "scenario_id": "TL-05",
            "before_read_sha256": _read_commitment(correction_before),
            "after_read_sha256": _read_commitment(correction_after),
            "before_selected_claim_ids": _selected_claim_ids(correction_before),
            "after_selected_claim_ids": _selected_claim_ids(correction_after),
            "passed": correction_passed,
        },
        {
            "case": "expiry",
            "scenario_id": "TL-14",
            "current_read_sha256": _read_commitment(expiry_current),
            "historical_read_sha256": _read_commitment(expiry_historical),
            "current_selected_claim_ids": _selected_claim_ids(expiry_current),
            "historical_selected_claim_ids": _selected_claim_ids(expiry_historical),
            "passed": expiry_passed,
        },
        {
            "case": "tombstone",
            "scenario_id": "TL-15",
            "current_read_sha256": _read_commitment(tombstone_current),
            "historical_read_sha256": _read_commitment(tombstone_historical),
            "current_selected_claim_ids": _selected_claim_ids(tombstone_current),
            "historical_selected_claim_ids": _selected_claim_ids(tombstone_historical),
            "passed": tombstone_passed,
        },
        {
            "case": "rollback",
            "scenario_id": "TL-05",
            "prefix_event_ids": [event.event_id for event in rollback_prefix],
            "prefix_snapshot_id": prefix_engine.snapshot_id,
            "prefix_snapshot_sha256": sha256_json(prefix_engine.snapshot_record()),
            "prefix_read_sha256": _read_commitment(prefix_read),
            "historical_read_sha256": _read_commitment(correction_before),
            "passed": rollback_passed,
        },
    ]


def build_g3_lifecycle_evidence() -> dict[str, Any]:
    """Rebuild the canonical public event history and record lifecycle checks."""

    payload = _build_without_validation()
    validate_g3_lifecycle_evidence(payload)
    return payload


def validate_g3_lifecycle_evidence(value: Mapping[str, Any]) -> None:
    """Require byte-for-byte semantic equality with a fresh canonical replay."""

    if not isinstance(value, Mapping):
        raise G3LifecycleError("G3 lifecycle evidence must be an object")
    body = {key: item for key, item in value.items() if key != "artifact_sha256"}
    if value.get("artifact_sha256") != sha256_json(body):
        raise G3LifecycleError("G3 lifecycle evidence hash mismatch")
    expected = _build_without_validation()
    if dict(value) != expected:
        raise G3LifecycleError("G3 lifecycle evidence differs from canonical replay")


def _build_without_validation() -> dict[str, Any]:
    replay = _replay_checks()
    lifecycle = _lifecycle_checks()
    payload: dict[str, Any] = {
        "schema_version": G3_LIFECYCLE_SCHEMA,
        "event_history_sha256": event_history_sha256(),
        "event_count": len(all_events()),
        "policies": list(_POLICIES),
        "replay_checks": replay,
        "lifecycle_checks": lifecycle,
        "all_passed": all(
            row["reverse_order_exact"] and row["restore_exact"] for row in replay
        )
        and all(row["passed"] for row in lifecycle),
    }
    payload["artifact_sha256"] = sha256_json(payload)
    return payload


def _safe_parent_descriptor(root: Path, destination: Path) -> tuple[int, str]:
    """Open a repository-local parent without following child symlinks."""

    repository = root.absolute()
    if repository.is_symlink() or not repository.is_dir():
        raise G3LifecycleError("G3 lifecycle repository root is missing or unsafe")
    try:
        relative = destination.absolute().relative_to(repository)
    except ValueError as exc:
        raise G3LifecycleError(
            "G3 lifecycle evidence path escaped the repository"
        ) from exc
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise G3LifecycleError("G3 lifecycle evidence path escaped the repository")

    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(repository, flags)
    except OSError as exc:
        raise G3LifecycleError(
            "cannot open G3 lifecycle repository root safely"
        ) from exc
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
        raise G3LifecycleError(
            "G3 lifecycle evidence path contains an unsafe parent"
        ) from exc
    return descriptor, relative.name


def _read_existing(parent: int, name: str) -> dict[str, Any] | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise G3LifecycleError("saved G3 lifecycle evidence is unsafe") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise G3LifecycleError("saved G3 lifecycle evidence is not a file")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            value = json.load(handle)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise G3LifecycleError(
                "saved G3 lifecycle evidence changed while it was read"
            )
    except (OSError, json.JSONDecodeError) as exc:
        raise G3LifecycleError("cannot read saved G3 lifecycle evidence") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise G3LifecycleError("saved G3 lifecycle evidence must be an object")
    return value


def _unlink_same_inode(parent: int, name: str, expected: os.stat_result) -> None:
    try:
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino):
            os.unlink(name, dir_fd=parent)
    except OSError:
        pass


def write_g3_lifecycle_evidence(root: Path | None = None) -> dict[str, Any]:
    """Persist the canonical evidence once, or verify an identical existing file."""

    root = (root or repository_root()).absolute()
    path = root / G3_LIFECYCLE_PATH
    value = build_g3_lifecycle_evidence()
    parent, name = _safe_parent_descriptor(root, path)
    try:
        existing = _read_existing(parent, name)
    except Exception:
        os.close(parent)
        raise
    if existing is not None:
        try:
            validate_g3_lifecycle_evidence(existing)
            if existing != value:
                raise G3LifecycleError("saved G3 lifecycle evidence changed")
            return existing
        finally:
            os.close(parent)

    temporary = f".{name}.{secrets.token_hex(12)}.tmp"
    temporary_created = False
    destination_created = False
    temporary_stat: os.stat_result | None = None
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor = os.open(temporary, flags, 0o666, dir_fd=parent)
        temporary_created = True
        data = (
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_stat = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(temporary_stat.st_mode):
            raise G3LifecycleError("G3 lifecycle temporary artifact is not a file")
        try:
            os.link(
                temporary,
                name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
                follow_symlinks=False,
            )
            destination_created = True
        except FileExistsError as exc:
            raise G3LifecycleError(
                "G3 lifecycle evidence was created concurrently"
            ) from exc
        os.unlink(temporary, dir_fd=parent)
        temporary_created = False
        os.fsync(parent)
    except Exception:
        if destination_created and temporary_stat is not None:
            _unlink_same_inode(parent, name, temporary_stat)
        raise
    finally:
        if temporary_created:
            try:
                os.unlink(temporary, dir_fd=parent)
            except OSError:
                pass
        os.close(parent)
    return value
