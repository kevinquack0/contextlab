"""Canonical, approval-gated freeze for the public G3 memory experiment."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any, Mapping, Sequence

from .baseline import repository_root
from .g2_gate import G2_GATE_SCHEMA, G2_HUMAN_APPROVAL_SCHEMA, build_g2_final_gate
from .g2_gate_io import load_canonical_g2_gate_inputs
from .g3_evidence import (
    public_raw_evidence_ids,
    validate_temporal_r0_lab,
)
from .memory_experiments import (
    G3_PAIRED_BOOTSTRAP_RESAMPLES,
    G3_PAIRED_BOOTSTRAP_SEED,
    G3_PAIRED_BOOTSTRAP_SEED_NAME,
    G3_PRIMARY_METRIC,
    G3_PROVENANCE_MINIMUM,
    G3_STATIC_ACCURACY_REGRESSION_FLOOR,
    MEMORY_CONFIGURATIONS,
    build_memory_experiment_manifest,
    validate_memory_experiment_manifest,
)
from .provider import ALLOWED_REASONING_EFFORTS, MODEL_ID, PROVIDER_SLUG
from .reports import validate_lab
from .tasking import sha256_json
from .temporal import sealed_temporal_references


G3_MEMORY_PROTOCOL_SCHEMA = "contextlab.g3-memory-protocol.v1"
G3_CORPUS_SNAPSHOT_SCHEMA = "contextlab.g3-corpus-snapshot.v1"
G3_FREEZE_SCHEMA = "contextlab.g3-public-freeze.v1"
G3_PRIOR_BOOTSTRAP_SCHEMA = "contextlab.g3-prior-bootstrap.v1"
G3_PROMPT_VERSION = "contextlab.memory-answer.v1"


class G3FreezeError(ValueError):
    """G3 cannot freeze before its public inputs and G2 approval are canonical."""


def _valid_approval_time(value: object) -> bool:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value) is None
    ):
        return False
    try:
        dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise G3FreezeError(f"cannot read {label}") from exc
    if not isinstance(value, dict):
        raise G3FreezeError(f"{label} must be an object")
    return value


def load_memory_protocol(root: Path | None = None) -> dict[str, Any]:
    root = (root or repository_root()).resolve()
    value = _read_json(root / "evaluation/v2/memory_protocol.json", "G3 protocol")
    validate_memory_protocol(value)
    return value


def validate_memory_protocol(value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "campaign_id",
        "entry_gate",
        "surface",
        "generation",
        "retrieval",
        "episodes",
        "acceptance",
        "sealed_boundary",
    }:
        raise G3FreezeError("G3 protocol fields are invalid")
    entry = value.get("entry_gate")
    surface = value.get("surface")
    generation = value.get("generation")
    retrieval = value.get("retrieval")
    episodes = value.get("episodes")
    acceptance = value.get("acceptance")
    sealed = value.get("sealed_boundary")
    if (
        value.get("schema_version") != G3_MEMORY_PROTOCOL_SCHEMA
        or value.get("campaign_id") != "g3-public-v1"
        or not all(
            isinstance(item, Mapping)
            for item in (
                entry,
                surface,
                generation,
                retrieval,
                episodes,
                acceptance,
                sealed,
            )
        )
    ):
        raise G3FreezeError("G3 protocol identity is invalid")
    if dict(entry) != {
        "gate": "G2",
        "required_human_approval": "approved",
        "required_decision": "retain-simple",
        "retriever_id": "R0",
    }:
        raise G3FreezeError("G3 entry gate changed")
    if (
        surface.get("public_temporal_tasks") != 28
        or surface.get("external_sealed_temporal_tasks") != 12
        or surface.get("public_static_regression_tasks") != 84
        or surface.get("memory_policies") != list(MEMORY_CONFIGURATIONS)
        or surface.get("reasoning_efforts") != list(ALLOWED_REASONING_EFFORTS)
    ):
        raise G3FreezeError("G3 experiment surface changed")
    if dict(generation) != {
        "model": MODEL_ID,
        "provider": PROVIDER_SLUG,
        "prompt_version": G3_PROMPT_VERSION,
        "temperature": 0.0,
        "output_token_limit": 8192,
        "context_budget_tokens": 3200,
        "concurrency": 4,
        "automatic_retries": 0,
    }:
        raise G3FreezeError("G3 generation protocol changed")
    if dict(retrieval) != {
        "static_source": "accepted-g2-r0-traces",
        "temporal_source": "r0-dense-over-public-corpus-events",
        "corpus_evidence_reserved": True,
    }:
        raise G3FreezeError("G3 retrieval protocol changed")
    if dict(episodes) != {
        "source_campaign": "prior-objectively-graded-runs-only",
        "same_campaign_feedback": False,
        "require_nonempty_seed_for_m4_claims": True,
    }:
        raise G3FreezeError("G3 episode protocol changed")
    expected_acceptance = {
        "primary_metric": G3_PRIMARY_METRIC,
        "provenance_minimum": G3_PROVENANCE_MINIMUM,
        "static_accuracy_regression_floor": G3_STATIC_ACCURACY_REGRESSION_FLOOR,
        "paired_bootstrap_resamples": G3_PAIRED_BOOTSTRAP_RESAMPLES,
        "paired_bootstrap_seed_name": G3_PAIRED_BOOTSTRAP_SEED_NAME,
        "paired_bootstrap_seed": G3_PAIRED_BOOTSTRAP_SEED,
        "temporal_improvement_rule": "mean_delta_vs_m0_gt_0",
        "failed_results_allowed_for_promotion": 0,
    }
    if dict(acceptance) != expected_acceptance:
        raise G3FreezeError("G3 acceptance protocol changed")
    expected_sealed_ids = [row["task_id"] for row in sealed_temporal_references()]
    if dict(sealed) != {
        "task_ids": expected_sealed_ids,
        "content_location": "external-only",
        "shared_return": "aggregate-and-content-free-records-only",
    }:
        raise G3FreezeError("G3 sealed boundary changed")


def validate_approved_g2_gate(value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping) or value.get("schema_version") != G2_GATE_SCHEMA:
        raise G3FreezeError("G2 gate schema is invalid")
    body = {key: item for key, item in value.items() if key != "artifact_sha256"}
    technical = {
        key: item
        for key, item in value.items()
        if key
        not in {
            "technical_record_sha256",
            "human_approval",
            "final_decision",
            "artifact_sha256",
        }
    }
    approval = value.get("human_approval")
    approval_record = (
        approval.get("approval_record") if isinstance(approval, Mapping) else None
    )
    if (
        value.get("artifact_sha256") != sha256_json(body)
        or value.get("technical_record_sha256") != sha256_json(technical)
        or value.get("technical_decision") != "retain-simple"
        or value.get("technical_promotion_ready") is not False
        or value.get("retained_retriever_id") != "R0"
        or value.get("promoted_retriever_id") is not None
        or value.get("promoted_retriever_protocol_sha256") is not None
        or value.get("promoted_retriever_config_sha256") is not None
        or value.get("final_decision") != "retain-simple"
        or not isinstance(approval, Mapping)
        or approval.get("status") != "approved"
        or approval.get("reviewer") != "Kevin Araujo"
        or not isinstance(approval_record, Mapping)
        or set(approval_record)
        != {
            "schema_version",
            "gate_sha256",
            "reviewer",
            "reviewer_role",
            "decision",
            "approved_at",
        }
        or approval_record.get("schema_version") != G2_HUMAN_APPROVAL_SCHEMA
        or approval_record.get("gate_sha256") != value.get("technical_record_sha256")
        or approval_record.get("decision") != "approved"
        or approval_record.get("reviewer") != "Kevin Araujo"
        or approval_record.get("reviewer_role") != "human_reviewer"
        or not _valid_approval_time(approval_record.get("approved_at"))
    ):
        raise G3FreezeError("G2 is not the approved retain-simple/R0 gate")


def load_approved_g2_gate(root: Path | None = None) -> dict[str, Any]:
    root = (root or repository_root()).resolve()
    saved = _read_json(root / "results/v2/gates/G2.json", "G2 gate")
    try:
        fresh = build_g2_final_gate(**load_canonical_g2_gate_inputs(root))
    except ValueError as exc:
        raise G3FreezeError(
            "cannot reconstruct the approved canonical G2 gate"
        ) from exc
    if saved != fresh:
        raise G3FreezeError("saved G2 gate is not the fresh canonical record")
    validate_approved_g2_gate(saved)
    return saved


def build_g3_corpus_snapshot(
    *,
    static_lab: Mapping[str, Any],
    temporal_lab: Mapping[str, Any],
    raw_evidence_ids: Sequence[str],
) -> dict[str, Any]:
    validate_lab(static_lab)
    validate_temporal_r0_lab(temporal_lab)
    raw_ids = sorted(set(raw_evidence_ids))
    if list(raw_evidence_ids) != raw_ids or not raw_ids:
        raise G3FreezeError("G3 raw evidence registry is not canonical")
    payload: dict[str, Any] = {
        "schema_version": G3_CORPUS_SNAPSHOT_SCHEMA,
        "static_component_lab_sha256": static_lab["artifact_sha256"],
        "static_retriever_id": "R0",
        "temporal_r0_lab_sha256": temporal_lab["artifact_sha256"],
        "temporal_event_history_sha256": temporal_lab["event_history_sha256"],
        "temporal_corpus_snapshot_sha256": temporal_lab[
            "temporal_corpus_snapshot_sha256"
        ],
        "raw_evidence_ids_sha256": sha256_json(raw_ids),
        "raw_evidence_id_count": len(raw_ids),
    }
    payload["artifact_sha256"] = sha256_json(payload)
    return payload


def build_canonical_g3_freeze(
    *,
    root: Path | None = None,
    temporal_lab: Mapping[str, Any],
    trusted_grade_artifacts: Sequence[Mapping[str, Any]],
    m4_episode_seed: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the public G3 freeze; this performs no network or paid work."""

    root = (root or repository_root()).resolve()
    protocol = load_memory_protocol(root)
    g2 = load_approved_g2_gate(root)
    static_lab = _read_json(
        root / "results/v2/retrieval/public_component_lab.json", "G2 component lab"
    )
    raw_ids = public_raw_evidence_ids(root)
    corpus = build_g3_corpus_snapshot(
        static_lab=static_lab,
        temporal_lab=temporal_lab,
        raw_evidence_ids=raw_ids,
    )
    if protocol["episodes"]["require_nonempty_seed_for_m4_claims"] and (
        not trusted_grade_artifacts or not m4_episode_seed
    ):
        raise G3FreezeError(
            "canonical G3 requires a non-empty, objectively graded prior M4 seed"
        )
    from .g3_prior_runs import G3PriorRunError, resolve_canonical_prior_sources

    try:
        prior_sources = resolve_canonical_prior_sources(
            root,
            [dict(row) for row in trusted_grade_artifacts],
            [dict(row) for row in m4_episode_seed],
        )
    except G3PriorRunError as exc:
        raise G3FreezeError("canonical G3 prior episode source is invalid") from exc
    prompt_path = root / "evaluation/v2/prompts/memory_answer_v1.md"
    prompt_sha256 = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
    generation = protocol["generation"]
    manifest = build_memory_experiment_manifest(
        retriever_id="R0",
        retriever_protocol_sha256=str(g2["protocol_sha256"]),
        retriever_disposition="retain-simple",
        g2_technical_record_sha256=str(g2["technical_record_sha256"]),
        g2_gate_artifact_sha256=str(g2["artifact_sha256"]),
        g2_approval_status="approved",
        g2_gate_disposition="retain-simple",
        generation_protocol_sha256=sha256_json(protocol),
        prompt_version=G3_PROMPT_VERSION,
        prompt_sha256=prompt_sha256,
        corpus_snapshot_sha256=str(corpus["artifact_sha256"]),
        output_token_limit=int(generation["output_token_limit"]),
        context_budget_tokens=int(generation["context_budget_tokens"]),
        available_raw_evidence_ids=raw_ids,
        trusted_grade_artifacts=trusted_grade_artifacts,
        campaign_id=str(protocol["campaign_id"]),
        m4_episode_seed=m4_episode_seed,
    )
    payload: dict[str, Any] = {
        "schema_version": G3_FREEZE_SCHEMA,
        "memory_protocol_sha256": sha256_json(protocol),
        "g2_gate_artifact_sha256": g2["artifact_sha256"],
        "prior_run_source_sha256s": sorted(
            str(source["artifact_sha256"]) for source in prior_sources
        ),
        "corpus_snapshot": corpus,
        "manifest": manifest,
    }
    payload["artifact_sha256"] = sha256_json(payload)
    validate_g3_freeze(payload)
    return payload


def build_g3_prior_bootstrap(
    *, root: Path | None = None, temporal_lab: Mapping[str, Any]
) -> dict[str, Any]:
    """Freeze a no-episode M3 source run used only to bootstrap canonical M4."""

    root = (root or repository_root()).resolve()
    protocol = load_memory_protocol(root)
    g2 = load_approved_g2_gate(root)
    static_lab = _read_json(
        root / "results/v2/retrieval/public_component_lab.json", "G2 component lab"
    )
    raw_ids = public_raw_evidence_ids(root)
    corpus = build_g3_corpus_snapshot(
        static_lab=static_lab,
        temporal_lab=temporal_lab,
        raw_evidence_ids=raw_ids,
    )
    prompt_path = root / "evaluation/v2/prompts/memory_answer_v1.md"
    generation = protocol["generation"]
    manifest = build_memory_experiment_manifest(
        retriever_id="R0",
        retriever_protocol_sha256=str(g2["protocol_sha256"]),
        retriever_disposition="retain-simple",
        g2_technical_record_sha256=str(g2["technical_record_sha256"]),
        g2_gate_artifact_sha256=str(g2["artifact_sha256"]),
        g2_approval_status="approved",
        g2_gate_disposition="retain-simple",
        generation_protocol_sha256=sha256_json(protocol),
        prompt_version=G3_PROMPT_VERSION,
        prompt_sha256=hashlib.sha256(prompt_path.read_bytes()).hexdigest(),
        corpus_snapshot_sha256=str(corpus["artifact_sha256"]),
        output_token_limit=int(generation["output_token_limit"]),
        context_budget_tokens=int(generation["context_budget_tokens"]),
        available_raw_evidence_ids=raw_ids,
        trusted_grade_artifacts=[],
        campaign_id="g3-prior-v1",
        m4_episode_seed=[],
    )
    payload: dict[str, Any] = {
        "schema_version": G3_PRIOR_BOOTSTRAP_SCHEMA,
        "memory_protocol_sha256": sha256_json(protocol),
        "g2_gate_artifact_sha256": g2["artifact_sha256"],
        "corpus_snapshot": corpus,
        "manifest": manifest,
    }
    payload["artifact_sha256"] = sha256_json(payload)
    validate_g3_prior_bootstrap(payload)
    return payload


def validate_g3_prior_bootstrap(value: Mapping[str, Any]) -> None:
    expected_fields = {
        "schema_version",
        "memory_protocol_sha256",
        "g2_gate_artifact_sha256",
        "corpus_snapshot",
        "manifest",
        "artifact_sha256",
    }
    body = {key: item for key, item in value.items() if key != "artifact_sha256"}
    if (
        not isinstance(value, Mapping)
        or set(value) != expected_fields
        or value.get("schema_version") != G3_PRIOR_BOOTSTRAP_SCHEMA
        or value.get("artifact_sha256") != sha256_json(body)
    ):
        raise G3FreezeError("G3 prior bootstrap envelope is invalid")
    manifest = value.get("manifest")
    corpus = value.get("corpus_snapshot")
    if not isinstance(manifest, Mapping) or not isinstance(corpus, Mapping):
        raise G3FreezeError("G3 prior bootstrap nested artifacts are invalid")
    try:
        validate_memory_experiment_manifest(
            manifest,
            trusted_frozen_manifest_sha256=str(
                manifest.get("frozen_manifest_sha256", "")
            ),
        )
    except ValueError as exc:
        raise G3FreezeError("G3 prior bootstrap manifest is invalid") from exc
    binding = manifest.get("retriever_binding")
    if (
        manifest.get("campaign_id") != "g3-prior-v1"
        or manifest.get("trusted_grade_artifacts") != []
        or manifest.get("m4_episode_seed") != []
        or not isinstance(binding, Mapping)
        or binding.get("retriever_id") != "R0"
        or binding.get("g2_approval_status") != "approved"
        or binding.get("g2_gate_disposition") != "retain-simple"
        or binding.get("g2_gate_artifact_sha256")
        != value.get("g2_gate_artifact_sha256")
        or manifest.get("corpus_snapshot_sha256") != corpus.get("artifact_sha256")
        or manifest.get("generation_protocol_sha256")
        != value.get("memory_protocol_sha256")
    ):
        raise G3FreezeError("G3 prior bootstrap commitments disagree")


def validate_g3_freeze(value: Mapping[str, Any]) -> None:
    """Validate every nested commitment before an immutable freeze is written."""

    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "memory_protocol_sha256",
        "g2_gate_artifact_sha256",
        "prior_run_source_sha256s",
        "corpus_snapshot",
        "manifest",
        "artifact_sha256",
    }:
        raise G3FreezeError("G3 freeze fields are invalid")
    if value.get("schema_version") != G3_FREEZE_SCHEMA or value.get(
        "artifact_sha256"
    ) != sha256_json(
        {key: item for key, item in value.items() if key != "artifact_sha256"}
    ):
        raise G3FreezeError("G3 freeze artifact is invalid")
    for key in ("memory_protocol_sha256", "g2_gate_artifact_sha256"):
        if (
            not isinstance(value.get(key), str)
            or re.fullmatch(r"[0-9a-f]{64}", str(value.get(key))) is None
        ):
            raise G3FreezeError(f"G3 freeze {key} is invalid")
    prior_hashes = value.get("prior_run_source_sha256s")
    if (
        not isinstance(prior_hashes, list)
        or prior_hashes != sorted(set(prior_hashes))
        or any(
            not isinstance(item, str) or re.fullmatch(r"[0-9a-f]{64}", item) is None
            for item in prior_hashes
        )
    ):
        raise G3FreezeError("G3 prior run source hashes are invalid")
    corpus = value.get("corpus_snapshot")
    expected_corpus_fields = {
        "schema_version",
        "static_component_lab_sha256",
        "static_retriever_id",
        "temporal_r0_lab_sha256",
        "temporal_event_history_sha256",
        "temporal_corpus_snapshot_sha256",
        "raw_evidence_ids_sha256",
        "raw_evidence_id_count",
        "artifact_sha256",
    }
    if (
        not isinstance(corpus, Mapping)
        or set(corpus) != expected_corpus_fields
        or corpus.get("schema_version") != G3_CORPUS_SNAPSHOT_SCHEMA
        or corpus.get("static_retriever_id") != "R0"
        or corpus.get("artifact_sha256")
        != sha256_json(
            {key: item for key, item in corpus.items() if key != "artifact_sha256"}
        )
        or isinstance(corpus.get("raw_evidence_id_count"), bool)
        or not isinstance(corpus.get("raw_evidence_id_count"), int)
        or int(corpus["raw_evidence_id_count"]) < 1
    ):
        raise G3FreezeError("G3 corpus snapshot is invalid")
    for key in expected_corpus_fields - {
        "schema_version",
        "static_retriever_id",
        "raw_evidence_id_count",
    }:
        if (
            not isinstance(corpus.get(key), str)
            or re.fullmatch(r"[0-9a-f]{64}", str(corpus.get(key))) is None
        ):
            raise G3FreezeError("G3 corpus snapshot hashes are invalid")
    manifest = value.get("manifest")
    if not isinstance(manifest, Mapping):
        raise G3FreezeError("G3 freeze manifest is invalid")
    try:
        validate_memory_experiment_manifest(
            manifest,
            trusted_frozen_manifest_sha256=str(
                manifest.get("frozen_manifest_sha256", "")
            ),
        )
    except ValueError as exc:
        raise G3FreezeError("G3 freeze manifest is invalid") from exc
    binding = manifest.get("retriever_binding")
    if (
        not isinstance(binding, Mapping)
        or binding.get("g2_approval_status") != "approved"
        or binding.get("g2_gate_disposition") != "retain-simple"
        or binding.get("retriever_id") != "R0"
        or value["g2_gate_artifact_sha256"] != binding.get("g2_gate_artifact_sha256")
        or manifest.get("corpus_snapshot_sha256") != corpus["artifact_sha256"]
        or manifest.get("generation_protocol_sha256") != value["memory_protocol_sha256"]
        or prior_hashes
        != sorted(
            str(row["source_artifact_sha256"])
            for row in manifest.get("trusted_grade_artifacts", [])
        )
    ):
        raise G3FreezeError("G3 freeze nested commitments disagree")


def _lexical_repository_root(root: Path) -> Path:
    repository = Path(os.path.abspath(root))
    if repository.is_symlink() or not repository.is_dir():
        raise G3FreezeError("G3 repository root is missing or unsafe")
    return repository


def _scoped_result_target(repository: Path, destination: Path, label: str) -> Path:
    requested = destination if destination.is_absolute() else repository / destination
    target = Path(os.path.abspath(requested))
    try:
        relative = target.relative_to(repository)
    except ValueError as exc:
        raise G3FreezeError(f"{label} must stay under results/v2") from exc
    if (
        len(relative.parts) < 3
        or relative.parts[:2] != ("results", "v2")
        or ".." in relative.parts
        or not relative.name
    ):
        raise G3FreezeError(f"{label} must stay under results/v2")
    return target


def _open_result_parent(repository: Path, destination: Path) -> tuple[int, str]:
    """Open/create a repository-local parent through no-follow descriptors."""

    try:
        relative = destination.relative_to(repository)
    except ValueError as exc:  # pragma: no cover - guarded by _scoped_result_target
        raise G3FreezeError("G3 artifact path escaped the repository") from exc
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(repository, flags)
    except OSError as exc:
        raise G3FreezeError("cannot open G3 repository root safely") from exc
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
        raise G3FreezeError("G3 artifact path contains an unsafe parent") from exc
    return descriptor, relative.name


def _unlink_same_inode(parent: int, name: str, expected: os.stat_result) -> None:
    try:
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino):
            os.unlink(name, dir_fd=parent)
    except OSError:
        pass


def _existing_target(parent: int, name: str) -> os.stat_result | None:
    try:
        metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise G3FreezeError("canonical G3 artifact target is unsafe")
    return metadata


def _atomic_create(repository: Path, path: Path, value: Mapping[str, Any]) -> None:
    """Create one immutable artifact without following or replacing any link."""

    parent, name = _open_result_parent(repository, path)
    temporary = f".{name}.{secrets.token_hex(12)}.tmp"
    temporary_created = False
    destination_created = False
    temporary_stat: os.stat_result | None = None
    try:
        if _existing_target(parent, name) is not None:
            raise G3FreezeError("canonical G3 freeze already exists and is immutable")
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
            raise G3FreezeError("canonical G3 temporary artifact is unsafe")
        data = (
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        linked_stat = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
        if (linked_stat.st_dev, linked_stat.st_ino) != (
            temporary_stat.st_dev,
            temporary_stat.st_ino,
        ):
            raise G3FreezeError("canonical G3 temporary artifact changed")
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
                raise G3FreezeError("canonical G3 artifact changed during publication")
        except FileExistsError as exc:
            _existing_target(parent, name)
            raise G3FreezeError(
                "canonical G3 freeze already exists and is immutable"
            ) from exc
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


def write_canonical_g3_freeze(
    value: Mapping[str, Any], *, root: Path | None = None, output: Path | None = None
) -> Path:
    root = _lexical_repository_root(root or repository_root())
    destination = (
        output
        if output is not None
        else root / "results/v2/memory/g3_public_freeze.json"
    )
    destination = _scoped_result_target(root, destination, "G3 freeze output")
    validate_g3_freeze(value)
    if value["prior_run_source_sha256s"]:
        from .g3_prior_runs import (
            G3PriorRunError,
            resolve_canonical_prior_sources,
        )

        manifest = value["manifest"]
        try:
            sources = resolve_canonical_prior_sources(
                root,
                manifest["trusted_grade_artifacts"],
                manifest["m4_episode_seed"],
            )
        except G3PriorRunError as exc:
            raise G3FreezeError("canonical G3 prior episode source is invalid") from exc
        if (
            sorted(str(row["artifact_sha256"]) for row in sources)
            != value["prior_run_source_sha256s"]
        ):
            raise G3FreezeError("canonical G3 prior episode sources changed")
    _atomic_create(root, destination, value)
    return destination.resolve(strict=True)


def write_g3_prior_bootstrap(
    value: Mapping[str, Any], *, root: Path | None = None, output: Path | None = None
) -> Path:
    root = _lexical_repository_root(root or repository_root())
    destination = output or root / "results/v2/memory/g3_prior_bootstrap.json"
    destination = _scoped_result_target(root, destination, "G3 prior bootstrap output")
    validate_g3_prior_bootstrap(value)
    _atomic_create(root, destination, value)
    return destination.resolve(strict=True)
