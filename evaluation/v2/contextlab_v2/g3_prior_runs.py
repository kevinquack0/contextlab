"""Immutable objectively graded prior runs used to seed G3 episodic memory."""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any

from .baseline import repository_root
from .g3_execution import validate_prepared_public_g3_cell
from .g3_grading import build_public_g3_result_receipt
from .memory_experiments import validate_memory_experiment_manifest
from .retrieval import estimate_tokens
from .tasking import sha256_json


PRIOR_OBJECTIVE_RUN_SCHEMA = "contextlab.g3-prior-objective-run.v1"
TRUSTED_OBJECTIVE_GRADE_SCHEMA = "contextlab.trusted-objective-grade.v1"
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class G3PriorRunError(ValueError):
    """A prior run is not an immutable, objectively graded episode source."""


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _task_signature(task: Mapping[str, Any]) -> str:
    return sha256_json(
        {
            "suite": task.get("suite"),
            "task_family": task.get("task_family"),
            "question_sha256": task.get("question_sha256"),
        }
    )


def _episode_raw_ids(receipt: Mapping[str, Any]) -> list[str]:
    trace = receipt["trace"]
    relevant = set(receipt["relevant_memory_claim_ids"])
    selected = {str(row["claim_id"]): row for row in trace["selected_memory_evidence"]}
    raw_ids = {
        str(raw_id)
        for claim in receipt["used_memory_claims"]
        for raw_id in claim["supporting_event_ids"]
    }
    for claim_id in relevant:
        row = selected.get(str(claim_id))
        if row is not None:
            raw_ids.update(str(raw_id) for raw_id in row["raw_evidence_ids"])
    if not raw_ids:
        raise G3PriorRunError(
            "prior episodic run must resolve to raw evidence through its grade"
        )
    available = set(trace["available_raw_evidence_ids"])
    if not raw_ids.issubset(available):
        raise G3PriorRunError("prior episodic run raw evidence is unavailable")
    return sorted(raw_ids)


def build_prior_objective_run(
    prepared_cell: Mapping[str, Any],
    generation_result: Mapping[str, Any],
    *,
    bootstrap_manifest: Mapping[str, Any],
    trusted_bootstrap_manifest_sha256: str,
) -> dict[str, Any]:
    """Build a source whose grade can be reproduced without trusting a snippet."""

    validate_prepared_public_g3_cell(prepared_cell)
    try:
        validate_memory_experiment_manifest(
            bootstrap_manifest,
            trusted_frozen_manifest_sha256=trusted_bootstrap_manifest_sha256,
        )
    except ValueError as exc:
        raise G3PriorRunError("prior run bootstrap manifest is invalid") from exc
    spec = prepared_cell["run_spec"]
    task = spec["task"]
    if task.get("suite") != "temporal" or spec.get("policy") not in {"M1", "M2", "M3"}:
        raise G3PriorRunError(
            "prior episodic seed must come from an objectively graded fact-memory run"
        )
    frozen_spec = next(
        (
            row
            for row in bootstrap_manifest["run_specs"]
            if row.get("run_id") == spec.get("run_id")
        ),
        None,
    )
    if (
        frozen_spec != spec
        or prepared_cell.get("frozen_manifest_sha256")
        != trusted_bootstrap_manifest_sha256
    ):
        raise G3PriorRunError("prior run is outside its bootstrap manifest")
    receipt = build_public_g3_result_receipt(
        prepared_cell,
        generation_result,
        frozen_manifest=bootstrap_manifest,
        trusted_frozen_manifest_sha256=trusted_bootstrap_manifest_sha256,
    )
    raw_ids = _episode_raw_ids(receipt)
    trace_id = f"trace-{receipt['trace_sha256']}"
    payload: dict[str, Any] = {
        "schema_version": PRIOR_OBJECTIVE_RUN_SCHEMA,
        "source_run_id": spec["run_id"],
        "task_id": task["task_id"],
        "task_family": task["task_family"],
        "task_signature": _task_signature(task),
        "task_feature": task["question_text"],
        "selected_strategy": spec["policy"],
        "trace_id": trace_id,
        "trace_sha256": receipt["trace_sha256"],
        "bootstrap_manifest_sha256": trusted_bootstrap_manifest_sha256,
        "bootstrap_manifest": dict(bootstrap_manifest),
        "prepared_cell_sha256": prepared_cell["artifact_sha256"],
        "prepared_cell": dict(prepared_cell),
        "generation_result_sha256": sha256_json(generation_result),
        "generation_result": dict(generation_result),
        "result_receipt_sha256": receipt["result_sha256"],
        "result_receipt": receipt,
        "objective_outcome": (
            "success"
            if receipt["is_correct"]
            and receipt["provenance_complete"]
            and not receipt["stale_answer"]
            else "failure"
        ),
        "evidence_path": [trace_id],
        "evidence_path_raw_ids": {trace_id: raw_ids},
        "raw_evidence_ids": raw_ids,
    }
    payload["artifact_sha256"] = sha256_json(payload)
    validate_prior_objective_run(payload)
    return payload


def validate_prior_objective_run(value: Mapping[str, Any]) -> None:
    expected_fields = {
        "schema_version",
        "source_run_id",
        "task_id",
        "task_family",
        "task_signature",
        "task_feature",
        "selected_strategy",
        "trace_id",
        "trace_sha256",
        "bootstrap_manifest_sha256",
        "bootstrap_manifest",
        "prepared_cell_sha256",
        "prepared_cell",
        "generation_result_sha256",
        "generation_result",
        "result_receipt_sha256",
        "result_receipt",
        "objective_outcome",
        "evidence_path",
        "evidence_path_raw_ids",
        "raw_evidence_ids",
        "artifact_sha256",
    }
    body = {key: item for key, item in value.items() if key != "artifact_sha256"}
    if (
        not isinstance(value, Mapping)
        or set(value) != expected_fields
        or value.get("schema_version") != PRIOR_OBJECTIVE_RUN_SCHEMA
        or value.get("artifact_sha256") != sha256_json(body)
        or not isinstance(value.get("source_run_id"), str)
        or _RUN_ID.fullmatch(str(value["source_run_id"])) is None
    ):
        raise G3PriorRunError("prior objective run envelope or hash is invalid")
    for field in (
        "task_signature",
        "trace_sha256",
        "bootstrap_manifest_sha256",
        "prepared_cell_sha256",
        "generation_result_sha256",
        "result_receipt_sha256",
        "artifact_sha256",
    ):
        if not _is_sha256(value.get(field)):
            raise G3PriorRunError(f"prior objective run {field} is invalid")
    manifest = value.get("bootstrap_manifest")
    prepared = value.get("prepared_cell")
    generation = value.get("generation_result")
    saved_receipt = value.get("result_receipt")
    if not all(
        isinstance(item, Mapping)
        for item in (manifest, prepared, generation, saved_receipt)
    ):
        raise G3PriorRunError("prior objective run nested artifacts are invalid")
    try:
        validate_memory_experiment_manifest(
            manifest,
            trusted_frozen_manifest_sha256=str(value["bootstrap_manifest_sha256"]),
        )
        validate_prepared_public_g3_cell(prepared)
    except ValueError as exc:
        raise G3PriorRunError(
            "prior objective run nested commitments are invalid"
        ) from exc
    spec = prepared["run_spec"]
    task = spec["task"]
    if (
        prepared.get("artifact_sha256") != value.get("prepared_cell_sha256")
        or sha256_json(generation) != value.get("generation_result_sha256")
        or saved_receipt.get("result_sha256") != value.get("result_receipt_sha256")
        or value.get("source_run_id") != spec.get("run_id")
        or value.get("task_id") != task.get("task_id")
        or value.get("task_family") != task.get("task_family")
        or value.get("task_signature") != _task_signature(task)
        or value.get("task_feature") != task.get("question_text")
        or value.get("selected_strategy") != spec.get("policy")
        or value.get("trace_sha256") != prepared["memory_trace"].get("trace_sha256")
        or value.get("trace_id") != f"trace-{value.get('trace_sha256')}"
        or prepared.get("frozen_manifest_sha256")
        != value.get("bootstrap_manifest_sha256")
    ):
        raise G3PriorRunError("prior objective run identity changed")
    fresh_receipt = build_public_g3_result_receipt(
        prepared,
        generation,
        frozen_manifest=manifest,
        trusted_frozen_manifest_sha256=str(value["bootstrap_manifest_sha256"]),
    )
    if fresh_receipt != saved_receipt:
        raise G3PriorRunError("prior objective run receipt is not reproducible")
    expected_outcome = (
        "success"
        if saved_receipt["is_correct"]
        and saved_receipt["provenance_complete"]
        and not saved_receipt["stale_answer"]
        else "failure"
    )
    raw_ids = _episode_raw_ids(saved_receipt)
    trace_id = str(value["trace_id"])
    if (
        value.get("objective_outcome") != expected_outcome
        or value.get("evidence_path") != [trace_id]
        or value.get("evidence_path_raw_ids") != {trace_id: raw_ids}
        or value.get("raw_evidence_ids") != raw_ids
    ):
        raise G3PriorRunError("prior objective run episode provenance changed")


def derive_trusted_grade_and_episode_seed(
    source: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Derive the only trusted grade and M4 seed accepted for one source."""

    validate_prior_objective_run(source)
    source_run_id = str(source["source_run_id"])
    grade: dict[str, Any] = {
        "schema_version": TRUSTED_OBJECTIVE_GRADE_SCHEMA,
        "grade_artifact_id": f"objective-grade-{source_run_id}",
        "grader_id": "deterministic",
        "accepted": True,
        "outcome": source["objective_outcome"],
        "source_run_id": source_run_id,
        "trace_id": source["trace_id"],
        "source_artifact_sha256": source["artifact_sha256"],
    }
    grade["artifact_sha256"] = sha256_json(grade)
    token_count = max(
        1,
        estimate_tokens(
            "\n".join(
                (
                    str(source["task_family"]),
                    str(source["task_feature"]),
                    str(source["selected_strategy"]),
                    str(source["objective_outcome"]),
                    str(source["trace_id"]),
                )
            )
        ),
    )
    seed = {
        "episode_id": f"episode-{source_run_id}",
        "task_signature": source["task_signature"],
        "task_family": source["task_family"],
        "task_feature": source["task_feature"],
        "selected_strategy": source["selected_strategy"],
        "token_count": token_count,
        "rank": 1,
        "evidence_path": source["evidence_path"],
        "evidence_path_raw_ids": source["evidence_path_raw_ids"],
        "raw_evidence_ids": source["raw_evidence_ids"],
        "grade_artifact_id": grade["grade_artifact_id"],
        "grade_outcome": source["objective_outcome"],
        "source_run_id": source_run_id,
        "trace_id": source["trace_id"],
        "source_artifact_sha256": source["artifact_sha256"],
        "retention_decision": "retain",
        "promotion_decision": "promoted",
    }
    return grade, seed


def canonical_prior_run_path(root: Path, source_run_id: str) -> Path:
    if _RUN_ID.fullmatch(source_run_id) is None:
        raise G3PriorRunError("prior source run ID is invalid")
    return root / "results/v2/memory/prior_runs" / f"{source_run_id}.json"


def _safe_parent_descriptor(root: Path, destination: Path) -> tuple[int, str]:
    """Open the destination parent without following repository-local symlinks."""

    repository = root.absolute()
    if repository.is_symlink() or not repository.is_dir():
        raise G3PriorRunError("prior run repository root is missing or unsafe")
    try:
        relative = destination.absolute().relative_to(repository)
    except ValueError as exc:
        raise G3PriorRunError("prior run path escaped the repository") from exc
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise G3PriorRunError("prior run path escaped the repository")

    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(repository, flags)
    except OSError as exc:
        raise G3PriorRunError("cannot open prior run repository root safely") from exc
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
        raise G3PriorRunError("prior run path contains an unsafe parent") from exc
    return descriptor, relative.name


def _write_bytes_to_descriptor(descriptor: int, data: bytes) -> None:
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _unlink_same_inode(parent: int, name: str, expected: os.stat_result) -> None:
    """Best-effort rollback without unlinking a name replaced by another process."""

    try:
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino):
            os.unlink(name, dir_fd=parent)
    except OSError:
        pass


def write_prior_objective_run(
    value: Mapping[str, Any], *, root: Path | None = None
) -> Path:
    """Write one immutable prior run below its canonical source-run path."""

    validate_prior_objective_run(value)
    root = (root or repository_root()).absolute()
    destination = canonical_prior_run_path(root, str(value["source_run_id"]))
    parent, name = _safe_parent_descriptor(root, destination)
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
        descriptor = os.open(temporary, flags, 0o600, dir_fd=parent)
        temporary_created = True
        data = (
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        _write_bytes_to_descriptor(descriptor, data)
        temporary_stat = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(temporary_stat.st_mode):
            raise G3PriorRunError("prior run temporary artifact is not a file")
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
            raise G3PriorRunError("canonical prior run already exists") from exc
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
    return destination


def resolve_canonical_prior_sources(
    root: Path,
    trusted_grade_artifacts: list[Mapping[str, Any]],
    m4_episode_seed: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Resolve every supplied grade and seed to a freshly validated source file."""

    grades = {
        str(row.get("source_run_id")): dict(row) for row in trusted_grade_artifacts
    }
    seeds = {str(row.get("source_run_id")): dict(row) for row in m4_episode_seed}
    if (
        not grades
        or set(grades) != set(seeds)
        or len(grades) != len(trusted_grade_artifacts)
        or len(seeds) != len(m4_episode_seed)
    ):
        raise G3PriorRunError(
            "trusted grades and M4 seeds must map one-to-one to prior runs"
        )
    sources: list[dict[str, Any]] = []
    for source_run_id in sorted(grades):
        path = canonical_prior_run_path(root, source_run_id)
        try:
            source = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise G3PriorRunError(
                f"cannot resolve canonical prior run: {source_run_id}"
            ) from exc
        if not isinstance(source, dict):
            raise G3PriorRunError("canonical prior run must be an object")
        expected_grade, expected_seed = derive_trusted_grade_and_episode_seed(source)
        if (
            grades[source_run_id] != expected_grade
            or seeds[source_run_id] != expected_seed
        ):
            raise G3PriorRunError(
                f"prior run grade or episode seed differs: {source_run_id}"
            )
        sources.append(source)
    return sources
