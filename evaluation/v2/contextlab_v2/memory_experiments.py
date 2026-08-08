"""Deterministic, provider-free G3 memory experiment contracts."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .credentials import redact
from .memory import POLICIES, TRUSTED_OBJECTIVE_GRADERS
from .provider import (
    ALLOWED_REASONING_EFFORTS,
    CANONICAL_MODEL_ID,
    MODEL_ID,
    PROVIDER_SLUG,
)
from .static_benchmark import public_static_tasks
from .tasking import sha256_json
from .temporal import public_temporal_tasks


MEMORY_EXPERIMENT_SCHEMA = "contextlab.g3-memory-experiment.v3"
MEMORY_RUN_SPEC_SCHEMA = "contextlab.g3-memory-run-spec.v3"
MEMORY_TRACE_SCHEMA = "contextlab.g3-memory-trace.v3"
MEMORY_RESULT_SCHEMA = "contextlab.g3-memory-result.v4"
G3_ANSWER_GRADE_SCHEMA = "contextlab.g3-answer-grade.v1"
MEMORY_CONFIGURATIONS = ("M0", "M1", "M2", "M3", "M4")
PUBLIC_TEMPORAL_TASK_COUNT = 28
PUBLIC_STATIC_TASK_COUNT = 84
PUBLIC_TASK_COUNT = PUBLIC_TEMPORAL_TASK_COUNT + PUBLIC_STATIC_TASK_COUNT
TRUSTED_OBJECTIVE_GRADE_ARTIFACT_SCHEMA = "contextlab.trusted-objective-grade.v1"
RETRIEVER_BINDING_SCHEMA = "contextlab.g3-retriever-binding.v1"
G3_ACCEPTANCE_SCHEMA = "contextlab.g3-acceptance-parameters.v1"
G3_RETAINED_RETRIEVER_ID = "R0"
G3_RETRIEVER_DISPOSITION = "retain-simple"
G3_PRIMARY_METRIC = "temporal_accuracy"
G3_PROVENANCE_MINIMUM = 0.95
G3_STATIC_ACCURACY_REGRESSION_FLOOR = -0.01
G3_PAIRED_BOOTSTRAP_RESAMPLES = 10_000
G3_PAIRED_BOOTSTRAP_SEED_NAME = "contextlab-g3-memory-paired-bootstrap-v1"
G3_PAIRED_BOOTSTRAP_SEED = 6_427_086_973_263_857_567
EPISODE_RETRIEVAL_LIMIT = 3
LEGACY_RETRIEVER_FIELDS = frozenset(
    {"promoted_retriever_id", "promoted_retriever_protocol_sha256"}
)


class MemoryExperimentError(ValueError):
    """A G3 artifact cannot be replayed or independently checked."""


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MemoryExperimentError(f"{label} must be non-empty text")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise MemoryExperimentError(f"{label} must be a positive integer")
    return value


def _retriever_binding(
    *,
    retriever_id: object,
    retriever_protocol_sha256: object,
    retriever_disposition: object,
    g2_technical_record_sha256: object,
    g2_gate_artifact_sha256: object,
    g2_approval_status: object,
    g2_gate_disposition: object,
) -> dict[str, str]:
    binding = {
        "schema_version": RETRIEVER_BINDING_SCHEMA,
        "retriever_id": retriever_id,
        "retriever_protocol_sha256": retriever_protocol_sha256,
        "retriever_disposition": retriever_disposition,
        "g2_technical_record_sha256": g2_technical_record_sha256,
        "g2_gate_artifact_sha256": g2_gate_artifact_sha256,
        "g2_approval_status": g2_approval_status,
        "g2_gate_disposition": g2_gate_disposition,
    }
    if retriever_id != G3_RETAINED_RETRIEVER_ID:
        raise MemoryExperimentError("G3 must bind the retained R0 retriever")
    if retriever_disposition != G3_RETRIEVER_DISPOSITION:
        raise MemoryExperimentError("G3 retriever disposition must be retain-simple")
    for key in (
        "retriever_protocol_sha256",
        "g2_technical_record_sha256",
        "g2_gate_artifact_sha256",
    ):
        if not _is_sha256(binding[key]):
            raise MemoryExperimentError(f"{key} is invalid")
    if (g2_approval_status, g2_gate_disposition) not in {
        ("pending", "blocked"),
        ("approved", "retain-simple"),
    }:
        raise MemoryExperimentError(
            "G2 approval and gate dispositions are inconsistent"
        )
    return {key: str(value) for key, value in binding.items()}


def _validated_retriever_binding(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise MemoryExperimentError("retriever binding must be an object")
    expected_fields = {
        "schema_version",
        "retriever_id",
        "retriever_protocol_sha256",
        "retriever_disposition",
        "g2_technical_record_sha256",
        "g2_gate_artifact_sha256",
        "g2_approval_status",
        "g2_gate_disposition",
    }
    if (
        set(value) != expected_fields
        or value.get("schema_version") != RETRIEVER_BINDING_SCHEMA
    ):
        raise MemoryExperimentError("retriever binding fields are ambiguous")
    return _retriever_binding(
        retriever_id=value.get("retriever_id"),
        retriever_protocol_sha256=value.get("retriever_protocol_sha256"),
        retriever_disposition=value.get("retriever_disposition"),
        g2_technical_record_sha256=value.get("g2_technical_record_sha256"),
        g2_gate_artifact_sha256=value.get("g2_gate_artifact_sha256"),
        g2_approval_status=value.get("g2_approval_status"),
        g2_gate_disposition=value.get("g2_gate_disposition"),
    )


def _acceptance_parameters() -> dict[str, Any]:
    return {
        "schema_version": G3_ACCEPTANCE_SCHEMA,
        "primary_metric": G3_PRIMARY_METRIC,
        "provenance_minimum": G3_PROVENANCE_MINIMUM,
        "static_accuracy_regression_floor": G3_STATIC_ACCURACY_REGRESSION_FLOOR,
        "paired_bootstrap_resamples": G3_PAIRED_BOOTSTRAP_RESAMPLES,
        "paired_bootstrap_seed_name": G3_PAIRED_BOOTSTRAP_SEED_NAME,
        "paired_bootstrap_seed": G3_PAIRED_BOOTSTRAP_SEED,
    }


def _validated_acceptance_parameters(value: object) -> dict[str, Any]:
    expected = _acceptance_parameters()
    if not isinstance(value, Mapping) or dict(value) != expected:
        raise MemoryExperimentError("G3 acceptance parameters changed")
    return expected


def _task_view(task: Mapping[str, Any], suite: str) -> dict[str, str]:
    if not isinstance(task, Mapping):
        raise MemoryExperimentError("task must be an object")
    task_id = _text(task.get("task_id"), "task_id")
    if task.get("suite") != suite:
        raise MemoryExperimentError(f"{task_id}: task suite changed")
    question = _text(task.get("question_text"), f"{task_id} question_text")
    question_hash = hashlib.sha256(question.encode()).hexdigest()
    if task.get("question_sha256") not in (None, question_hash):
        raise MemoryExperimentError(f"{task_id}: question hash changed")
    return {
        "task_id": task_id,
        "suite": suite,
        "question_text": question,
        "question_sha256": question_hash,
        "task_family": _text(task.get("task_family"), f"{task_id} task_family"),
    }


def _public_tasks() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    temporal = sorted(
        (_task_view(row, "temporal") for row in public_temporal_tasks()),
        key=lambda row: row["task_id"],
    )
    static = sorted(
        (_task_view(row, "static") for row in public_static_tasks()),
        key=lambda row: row["task_id"],
    )
    if len(temporal) != PUBLIC_TEMPORAL_TASK_COUNT or len(
        {row["task_id"] for row in temporal}
    ) != len(temporal):
        raise MemoryExperimentError(
            "G3 requires exactly 28 unique public temporal tasks"
        )
    if len(static) != PUBLIC_STATIC_TASK_COUNT or len(
        {row["task_id"] for row in static}
    ) != len(static):
        raise MemoryExperimentError("G3 requires exactly 84 unique public static tasks")
    return temporal, static


def _raw_ids(values: Iterable[object]) -> list[str]:
    rows = sorted({_text(value, "raw evidence ID") for value in values})
    if not rows:
        raise MemoryExperimentError("available_raw_evidence_ids must not be empty")
    return rows


def _trusted_commitment(
    manifest: Mapping[str, Any], trusted_frozen_manifest_sha256: object
) -> str:
    if not _is_sha256(trusted_frozen_manifest_sha256):
        raise MemoryExperimentError(
            "trusted_frozen_manifest_sha256 must be an explicit SHA-256 commitment"
        )
    if manifest.get("frozen_manifest_sha256") != trusted_frozen_manifest_sha256:
        raise MemoryExperimentError("frozen manifest differs from trusted commitment")
    return str(trusted_frozen_manifest_sha256)


def _grade_artifact(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MemoryExperimentError("trusted grade artifact must be an object")
    body = {key: item for key, item in value.items() if key != "artifact_sha256"}
    if (
        value.get("schema_version") != TRUSTED_OBJECTIVE_GRADE_ARTIFACT_SCHEMA
        or value.get("artifact_sha256") != sha256_json(body)
        or value.get("grader_id") not in TRUSTED_OBJECTIVE_GRADERS
        or value.get("accepted") is not True
        or value.get("outcome") not in {"success", "failure"}
    ):
        raise MemoryExperimentError("trusted grade artifact is invalid")
    artifact = {
        "schema_version": TRUSTED_OBJECTIVE_GRADE_ARTIFACT_SCHEMA,
        "grade_artifact_id": _text(value.get("grade_artifact_id"), "grade artifact ID"),
        "grader_id": value["grader_id"],
        "accepted": True,
        "outcome": value["outcome"],
        "source_run_id": _text(value.get("source_run_id"), "grade source_run_id"),
        "trace_id": _text(value.get("trace_id"), "grade trace_id"),
        "source_artifact_sha256": value.get("source_artifact_sha256"),
    }
    if not _is_sha256(artifact["source_artifact_sha256"]):
        raise MemoryExperimentError("trusted grade source artifact hash is invalid")
    canonical = {**artifact}
    canonical["artifact_sha256"] = sha256_json(canonical)
    if canonical != dict(value):
        raise MemoryExperimentError("trusted grade artifact fields are invalid")
    return artifact | {"artifact_sha256": value["artifact_sha256"]}


def _grade_registry(
    artifacts: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows = sorted(
        (_grade_artifact(item) for item in artifacts),
        key=lambda row: row["grade_artifact_id"],
    )
    if len({row["grade_artifact_id"] for row in rows}) != len(rows):
        raise MemoryExperimentError("trusted grade artifact IDs must be unique")
    return rows


def _episode_path_provenance(
    episode_id: str,
    evidence_path: Sequence[str],
    mapping: object,
    available_raw_ids: set[str],
) -> dict[str, list[str]]:
    if not isinstance(mapping, Mapping):
        raise MemoryExperimentError(
            f"{episode_id}: evidence_path_raw_ids must be an object"
        )
    normalized: dict[str, list[str]] = {}
    unknown_mapping = set(mapping).difference(evidence_path)
    if unknown_mapping:
        raise MemoryExperimentError(
            f"{episode_id}: evidence path mapping has unknown entries"
        )
    for path in evidence_path:
        if path in available_raw_ids:
            resolved = [path]
        else:
            supplied = mapping.get(path)
            if not isinstance(supplied, Sequence) or isinstance(supplied, (str, bytes)):
                raise MemoryExperimentError(
                    f"{episode_id}: evidence path does not resolve: {path}"
                )
            resolved = sorted(
                {
                    _text(raw_id, f"{episode_id} mapped raw evidence")
                    for raw_id in supplied
                }
            )
        if not resolved or not set(resolved).issubset(available_raw_ids):
            raise MemoryExperimentError(
                f"{episode_id}: evidence path does not resolve: {path}"
            )
        normalized[path] = resolved
    return normalized


def _episode_seed(
    seed: Mapping[str, Any],
    available_raw_ids: set[str],
    grade_artifacts: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    if not isinstance(seed, Mapping):
        raise MemoryExperimentError("M4 episode seed must be an object")
    episode_id = _text(seed.get("episode_id"), "episode_id")
    raw_ids = sorted(
        {
            _text(value, f"{episode_id} raw evidence")
            for value in seed.get("raw_evidence_ids", [])
        }
    )
    evidence_path = sorted(
        {
            _text(value, f"{episode_id} evidence path")
            for value in seed.get("evidence_path", [])
        }
    )
    if not raw_ids or not evidence_path or not set(raw_ids).issubset(available_raw_ids):
        raise MemoryExperimentError(
            f"{episode_id}: episode evidence lacks frozen raw provenance"
        )
    path_provenance = _episode_path_provenance(
        episode_id,
        evidence_path,
        seed.get("evidence_path_raw_ids", {}),
        available_raw_ids,
    )
    resolved_raw_ids = {
        raw_id for resolved_ids in path_provenance.values() for raw_id in resolved_ids
    }
    if set(raw_ids) != resolved_raw_ids:
        raise MemoryExperimentError(
            f"{episode_id}: raw evidence must exactly resolve the evidence path"
        )
    if "objective_grade" in seed:
        raise MemoryExperimentError(
            f"{episode_id}: objective grade snippets are not trusted artifacts"
        )
    grade_artifact_id = _text(
        seed.get("grade_artifact_id"), f"{episode_id} grade_artifact_id"
    )
    grade = grade_artifacts.get(grade_artifact_id)
    if grade is None:
        raise MemoryExperimentError(
            f"{episode_id}: grade artifact is not in the frozen trusted registry"
        )
    if (
        seed.get("promotion_decision") != "promoted"
        or seed.get("retention_decision") != "retain"
    ):
        raise MemoryExperimentError(
            f"{episode_id}: only retained, objectively promoted episodes may seed M4"
        )
    task_signature = _text(seed.get("task_signature"), f"{episode_id} task_signature")
    if not _is_sha256(task_signature):
        raise MemoryExperimentError(
            f"{episode_id}: task_signature must be a SHA-256 digest"
        )
    result = {
        "episode_id": episode_id,
        "task_signature": task_signature,
        "task_family": _text(seed.get("task_family"), f"{episode_id} task_family"),
        "task_feature": _text(seed.get("task_feature"), f"{episode_id} task_feature"),
        "selected_strategy": _text(
            seed.get("selected_strategy"), f"{episode_id} selected_strategy"
        ),
        "token_count": _positive_int(
            seed.get("token_count"), f"{episode_id} token_count"
        ),
        "rank": _positive_int(seed.get("rank"), f"{episode_id} rank"),
        "evidence_path": evidence_path,
        "evidence_path_raw_ids": path_provenance,
        "raw_evidence_ids": raw_ids,
        "grade_artifact_id": grade_artifact_id,
        "grade_outcome": grade["outcome"],
        "source_run_id": _text(
            seed.get("source_run_id"), f"{episode_id} source_run_id"
        ),
        "trace_id": _text(seed.get("trace_id"), f"{episode_id} trace_id"),
        "source_artifact_sha256": seed.get("source_artifact_sha256"),
        "retention_decision": "retain",
        "promotion_decision": "promoted",
    }
    if (
        not _is_sha256(result["source_artifact_sha256"])
        or result["source_run_id"] != grade["source_run_id"]
        or result["trace_id"] != grade["trace_id"]
        or result["source_artifact_sha256"] != grade["source_artifact_sha256"]
    ):
        raise MemoryExperimentError(f"{episode_id}: grade artifact bindings differ")
    return result


def _episodes(
    seeds: Iterable[Mapping[str, Any]],
    available_raw_ids: set[str],
    grade_artifacts: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows = sorted(
        (_episode_seed(seed, available_raw_ids, grade_artifacts) for seed in seeds),
        key=lambda row: row["episode_id"],
    )
    if len({row["episode_id"] for row in rows}) != len(rows):
        raise MemoryExperimentError("M4 episode IDs must be unique")
    return rows


def _spec(
    task: Mapping[str, str], policy: str, effort: str, manifest: Mapping[str, Any]
) -> dict[str, Any]:
    spec = {
        "schema_version": MEMORY_RUN_SPEC_SCHEMA,
        "run_id": f"{manifest['campaign_id']}-{policy}-{effort}-{task['task_id']}",
        "campaign_id": manifest["campaign_id"],
        "policy": policy,
        "reasoning_effort": effort,
        "task": dict(task),
        "retriever_binding_sha256": manifest["retriever_binding_sha256"],
        "acceptance_parameters_sha256": manifest["acceptance_parameters_sha256"],
        "generation_protocol_sha256": manifest["generation_protocol_sha256"],
        "requested_model": manifest["requested_model"],
        "provider": manifest["provider"],
        "prompt_version": manifest["prompt_version"],
        "prompt_sha256": manifest["prompt_sha256"],
        "corpus_snapshot_sha256": manifest["corpus_snapshot_sha256"],
        "output_token_limit": manifest["output_token_limit"],
        "context_budget_tokens": manifest["context_budget_tokens"],
        "available_raw_evidence_ids_sha256": manifest[
            "available_raw_evidence_ids_sha256"
        ],
        "trusted_grade_artifacts_sha256": manifest["trusted_grade_artifacts_sha256"],
        "m4_episode_seed_sha256": manifest["m4_episode_seed_sha256"]
        if policy == "M4"
        else None,
        "m4_episode_seed_count": len(manifest["m4_episode_seed"])
        if policy == "M4"
        else 0,
    }
    spec["run_spec_sha256"] = sha256_json(spec)
    return spec


def _frozen_manifest_sha256(manifest: Mapping[str, Any]) -> str:
    """Hash the immutable experiment plan, excluding resumable result state."""

    ignored = {
        "cells",
        "status_counts",
        "manifest_sha256",
        "frozen_manifest_sha256",
    }
    return sha256_json(
        {key: value for key, value in manifest.items() if key not in ignored}
    )


def build_memory_experiment_manifest(
    static_regression_tasks: Iterable[Mapping[str, Any]] | None = None,
    *,
    retriever_id: str,
    retriever_protocol_sha256: str,
    retriever_disposition: str,
    g2_technical_record_sha256: str,
    g2_gate_artifact_sha256: str,
    g2_approval_status: str,
    g2_gate_disposition: str,
    generation_protocol_sha256: str,
    prompt_version: str,
    prompt_sha256: str,
    corpus_snapshot_sha256: str,
    output_token_limit: int,
    context_budget_tokens: int,
    available_raw_evidence_ids: Iterable[object],
    trusted_grade_artifacts: Iterable[Mapping[str, Any]] = (),
    campaign_id: str = "g3",
    m4_episode_seed: Iterable[Mapping[str, Any]] = (),
    requested_model: str = MODEL_ID,
) -> dict[str, Any]:
    """Freeze the exact 28 temporal + 84 static × M0--M4 × low/high surface."""

    temporal, frozen_static = _public_tasks()
    if static_regression_tasks is not None:
        supplied = sorted(
            (_task_view(row, "static") for row in static_regression_tasks),
            key=lambda row: row["task_id"],
        )
        if supplied != frozen_static:
            raise MemoryExperimentError(
                "G3 static regression tasks must equal the frozen public 84"
            )
    raw_ids = _raw_ids(available_raw_evidence_ids)
    grades = _grade_registry(trusted_grade_artifacts)
    retriever_binding = _retriever_binding(
        retriever_id=retriever_id,
        retriever_protocol_sha256=retriever_protocol_sha256,
        retriever_disposition=retriever_disposition,
        g2_technical_record_sha256=g2_technical_record_sha256,
        g2_gate_artifact_sha256=g2_gate_artifact_sha256,
        g2_approval_status=g2_approval_status,
        g2_gate_disposition=g2_gate_disposition,
    )
    acceptance_parameters = _acceptance_parameters()
    if not all(
        _is_sha256(value)
        for value in (
            generation_protocol_sha256,
            prompt_sha256,
            corpus_snapshot_sha256,
        )
    ):
        raise MemoryExperimentError(
            "G3 protocol, prompt, and corpus commitments require SHA-256 values"
        )
    if requested_model != MODEL_ID or not _text(prompt_version, "prompt_version"):
        raise MemoryExperimentError("G3 model or prompt commitment changed")
    if any(not (c.isalnum() or c in "-_") for c in _text(campaign_id, "campaign_id")):
        raise MemoryExperimentError("campaign_id contains unsupported characters")
    manifest: dict[str, Any] = {
        "schema_version": MEMORY_EXPERIMENT_SCHEMA,
        "campaign_id": campaign_id,
        "retriever_binding": retriever_binding,
        "retriever_binding_sha256": sha256_json(retriever_binding),
        "acceptance_parameters": acceptance_parameters,
        "acceptance_parameters_sha256": sha256_json(acceptance_parameters),
        "generation_protocol_sha256": generation_protocol_sha256,
        "requested_model": requested_model,
        "provider": PROVIDER_SLUG,
        "reasoning_efforts": list(ALLOWED_REASONING_EFFORTS),
        "prompt_version": prompt_version,
        "prompt_sha256": prompt_sha256,
        "corpus_snapshot_sha256": corpus_snapshot_sha256,
        "output_token_limit": _positive_int(output_token_limit, "output_token_limit"),
        "context_budget_tokens": _positive_int(
            context_budget_tokens, "context_budget_tokens"
        ),
        "available_raw_evidence_ids": raw_ids,
        "available_raw_evidence_ids_sha256": sha256_json(raw_ids),
        "trusted_grade_artifacts": grades,
        "trusted_grade_artifacts_sha256": sha256_json(grades),
        "policies": list(MEMORY_CONFIGURATIONS),
        "temporal_task_count": len(temporal),
        "static_regression_task_count": len(frozen_static),
    }
    manifest["m4_episode_seed"] = _episodes(
        m4_episode_seed,
        set(raw_ids),
        {row["grade_artifact_id"]: row for row in grades},
    )
    manifest["m4_episode_seed_sha256"] = sha256_json(manifest["m4_episode_seed"])
    manifest["run_specs"] = [
        _spec(task, policy, effort, manifest)
        for policy in MEMORY_CONFIGURATIONS
        for effort in ALLOWED_REASONING_EFFORTS
        for task in (*temporal, *frozen_static)
    ]
    manifest["frozen_manifest_sha256"] = _frozen_manifest_sha256(manifest)
    manifest["cells"] = [
        {
            "run_id": spec["run_id"],
            "run_spec_sha256": spec["run_spec_sha256"],
            "status": "pending",
            "result_sha256": None,
            "result_receipt": None,
        }
        for spec in manifest["run_specs"]
    ]
    manifest["status_counts"] = {
        "completed": 0,
        "failed": 0,
        "pending": len(manifest["cells"]),
    }
    manifest["manifest_sha256"] = sha256_json(manifest)
    validate_memory_experiment_manifest(manifest)
    return manifest


def validate_memory_experiment_manifest(
    manifest: Mapping[str, Any],
    *,
    trusted_frozen_manifest_sha256: str | None = None,
) -> None:
    if not isinstance(manifest, Mapping):
        raise MemoryExperimentError("memory experiment manifest must be an object")
    if LEGACY_RETRIEVER_FIELDS.intersection(manifest):
        raise MemoryExperimentError("legacy promoted retriever fields are ambiguous")
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if manifest.get("schema_version") != MEMORY_EXPERIMENT_SCHEMA or manifest.get(
        "manifest_sha256"
    ) != sha256_json(body):
        raise MemoryExperimentError("memory experiment manifest hash is invalid")
    if manifest.get("frozen_manifest_sha256") != _frozen_manifest_sha256(manifest):
        raise MemoryExperimentError("frozen memory experiment commitment changed")
    if trusted_frozen_manifest_sha256 is not None:
        _trusted_commitment(manifest, trusted_frozen_manifest_sha256)
    temporal, static = _public_tasks()
    if (
        manifest.get("policies") != list(MEMORY_CONFIGURATIONS)
        or manifest.get("reasoning_efforts") != list(ALLOWED_REASONING_EFFORTS)
        or manifest.get("temporal_task_count") != 28
        or manifest.get("static_regression_task_count") != 84
    ):
        raise MemoryExperimentError("memory experiment surface changed")
    if (
        manifest.get("requested_model") != MODEL_ID
        or manifest.get("provider") != PROVIDER_SLUG
    ):
        raise MemoryExperimentError("memory experiment provider commitment changed")
    retriever_binding = _validated_retriever_binding(manifest.get("retriever_binding"))
    if manifest.get("retriever_binding_sha256") != sha256_json(retriever_binding):
        raise MemoryExperimentError("retriever binding hash changed")
    acceptance_parameters = _validated_acceptance_parameters(
        manifest.get("acceptance_parameters")
    )
    if manifest.get("acceptance_parameters_sha256") != sha256_json(
        acceptance_parameters
    ):
        raise MemoryExperimentError("acceptance parameters hash changed")
    for key in (
        "retriever_binding_sha256",
        "acceptance_parameters_sha256",
        "generation_protocol_sha256",
        "prompt_sha256",
        "corpus_snapshot_sha256",
        "m4_episode_seed_sha256",
        "available_raw_evidence_ids_sha256",
        "trusted_grade_artifacts_sha256",
    ):
        if not _is_sha256(manifest.get(key)):
            raise MemoryExperimentError(f"{key} is invalid")
    _text(manifest.get("prompt_version"), "prompt_version")
    _positive_int(manifest.get("output_token_limit"), "output_token_limit")
    budget = _positive_int(
        manifest.get("context_budget_tokens"), "context_budget_tokens"
    )
    raw_ids = _raw_ids(manifest.get("available_raw_evidence_ids", []))
    if manifest.get("available_raw_evidence_ids_sha256") != sha256_json(raw_ids):
        raise MemoryExperimentError("frozen raw evidence commitment changed")
    grades = _grade_registry(manifest.get("trusted_grade_artifacts", []))
    if manifest.get("trusted_grade_artifacts_sha256") != sha256_json(grades):
        raise MemoryExperimentError("trusted grade registry commitment changed")
    episodes = _episodes(
        manifest.get("m4_episode_seed", []),
        set(raw_ids),
        {row["grade_artifact_id"]: row for row in grades},
    )
    if episodes != manifest.get("m4_episode_seed") or sha256_json(
        episodes
    ) != manifest.get("m4_episode_seed_sha256"):
        raise MemoryExperimentError("M4 episode seed changed")
    specs = manifest.get("run_specs")
    cells = manifest.get("cells")
    expected = (
        len(MEMORY_CONFIGURATIONS) * len(ALLOWED_REASONING_EFFORTS) * PUBLIC_TASK_COUNT
    )
    if (
        not isinstance(specs, list)
        or not isinstance(cells, list)
        or len(specs) != expected
        or len(cells) != expected
    ):
        raise MemoryExperimentError("memory experiment factorial count is invalid")
    expected_tasks = {row["task_id"]: row for row in (*temporal, *static)}
    spec_by_id: dict[str, Mapping[str, Any]] = {}
    task_ids_by_lane: dict[tuple[str, str], set[str]] = {}
    for spec in specs:
        if not isinstance(spec, Mapping):
            raise MemoryExperimentError("memory run spec must be an object")
        if LEGACY_RETRIEVER_FIELDS.intersection(spec):
            raise MemoryExperimentError(
                "legacy promoted retriever fields are ambiguous"
            )
        spec_body = {
            key: value for key, value in spec.items() if key != "run_spec_sha256"
        }
        task = spec.get("task")
        policy = spec.get("policy")
        effort = spec.get("reasoning_effort")
        if (
            spec.get("schema_version") != MEMORY_RUN_SPEC_SCHEMA
            or spec.get("run_spec_sha256") != sha256_json(spec_body)
            or policy not in POLICIES
            or effort not in ALLOWED_REASONING_EFFORTS
            or not isinstance(task, Mapping)
        ):
            raise MemoryExperimentError("memory run spec identity changed")
        task_view = _task_view(task, str(task.get("suite")))
        if (
            expected_tasks.get(task_view["task_id"]) != task_view
            or spec.get("run_id")
            != f"{manifest['campaign_id']}-{policy}-{effort}-{task_view['task_id']}"
        ):
            raise MemoryExperimentError("memory run task or ID changed")
        for key in (
            "retriever_binding_sha256",
            "acceptance_parameters_sha256",
            "generation_protocol_sha256",
            "requested_model",
            "provider",
            "prompt_version",
            "prompt_sha256",
            "corpus_snapshot_sha256",
            "output_token_limit",
            "context_budget_tokens",
            "available_raw_evidence_ids_sha256",
            "trusted_grade_artifacts_sha256",
        ):
            if spec.get(key) != manifest.get(key):
                raise MemoryExperimentError("memory run commitment changed")
        if policy == "M4":
            if spec.get("m4_episode_seed_sha256") != manifest[
                "m4_episode_seed_sha256"
            ] or spec.get("m4_episode_seed_count") != len(episodes):
                raise MemoryExperimentError("M4 seed contract changed")
        elif (
            spec.get("m4_episode_seed_sha256") is not None
            or spec.get("m4_episode_seed_count") != 0
        ):
            raise MemoryExperimentError("non-M4 run has episodic seed")
        if spec["run_id"] in spec_by_id:
            raise MemoryExperimentError("memory run IDs must be unique")
        task_ids_by_lane.setdefault((str(policy), str(effort)), set()).add(
            task_view["task_id"]
        )
        spec_by_id[spec["run_id"]] = spec
    expected_lanes = {
        (policy, effort)
        for policy in MEMORY_CONFIGURATIONS
        for effort in ALLOWED_REASONING_EFFORTS
    }
    if set(task_ids_by_lane) != expected_lanes or any(
        task_ids != set(expected_tasks) for task_ids in task_ids_by_lane.values()
    ):
        raise MemoryExperimentError("memory experiment factorial coverage changed")
    seen: set[str] = set()
    observed = {"completed": 0, "failed": 0, "pending": 0}
    for cell in cells:
        if (
            not isinstance(cell, Mapping)
            or cell.get("run_id") not in spec_by_id
            or cell.get("run_id") in seen
            or cell.get("run_spec_sha256")
            != spec_by_id[cell["run_id"]]["run_spec_sha256"]
            or cell.get("status") not in observed
        ):
            raise MemoryExperimentError("memory result cell identity changed")
        pending = cell["status"] == "pending"
        if pending != (cell.get("result_sha256") is None) or pending != (
            cell.get("result_receipt") is None
        ):
            raise MemoryExperimentError("memory result cell receipt changed")
        if not pending:
            if trusted_frozen_manifest_sha256 is None:
                raise MemoryExperimentError(
                    "stored result receipts require an explicit trusted frozen manifest commitment"
                )
            receipt = cell["result_receipt"]
            _validate_memory_result_receipt_in_valid_manifest(
                receipt,
                spec_by_id[cell["run_id"]],
                manifest,
                trusted_frozen_manifest_sha256,
            )
            if cell["result_sha256"] != receipt["result_sha256"]:
                raise MemoryExperimentError("stored result receipt hash changed")
        seen.add(cell["run_id"])
        observed[cell["status"]] += 1
    if observed != manifest.get("status_counts") or budget < 1:
        raise MemoryExperimentError("memory result status counts changed")


def _evidence(
    value: Mapping[str, Any], kind: str, available_raw_ids: set[str]
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MemoryExperimentError(f"{kind} evidence must be an object")
    evidence_id = _text(value.get("evidence_id"), f"{kind} evidence ID")
    tokens = _positive_int(value.get("token_count"), f"{evidence_id} token_count")
    rank = value.get("rank", 1_000_000)
    if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
        raise MemoryExperimentError(f"{evidence_id}: rank is invalid")
    raw_ids = sorted(
        {
            _text(item, f"{evidence_id} raw evidence")
            for item in value.get(
                "raw_evidence_ids", [evidence_id] if kind == "corpus" else []
            )
        }
    )
    if not raw_ids or not set(raw_ids).issubset(available_raw_ids):
        raise MemoryExperimentError(
            f"{evidence_id}: raw provenance is outside frozen corpus"
        )
    result: dict[str, Any] = {
        "evidence_id": evidence_id,
        "token_count": tokens,
        "rank": rank,
        "raw_evidence_ids": raw_ids,
    }
    if kind == "memory":
        result["claim_id"] = _text(value.get("claim_id"), f"{evidence_id} claim_id")
    return result


def _episode_evidence(
    value: Mapping[str, Any], available_raw_ids: set[str]
) -> dict[str, Any]:
    episode_id = _text(value.get("episode_id"), "episode evidence ID")
    evidence = _evidence(
        {
            "evidence_id": episode_id,
            "token_count": value.get("token_count"),
            "rank": value.get("rank"),
            "raw_evidence_ids": value.get("raw_evidence_ids", []),
        },
        "episode",
        available_raw_ids,
    )
    evidence["episode_id"] = episode_id
    evidence["evidence_path"] = list(value.get("evidence_path", []))
    return evidence


def _similarity(left: str, right: str) -> float:
    left_tokens = set(re.findall(r"[a-z0-9]+", left.casefold()))
    right_tokens = set(re.findall(r"[a-z0-9]+", right.casefold()))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _task_signature(task: Mapping[str, Any]) -> str:
    return sha256_json(
        {
            "suite": _text(task.get("suite"), "episode task suite"),
            "task_family": _text(task.get("task_family"), "episode task family"),
            "question_sha256": _text(
                task.get("question_sha256"), "episode question hash"
            ),
        }
    )


def _eligible_episode_evidence(
    seeds: Sequence[Mapping[str, Any]],
    task: Mapping[str, Any],
    available_raw_ids: set[str],
) -> list[dict[str, Any]]:
    family = _text(task.get("task_family"), "episode query task family")
    question = _text(task.get("question_text"), "episode query text")
    signature = _task_signature(task)
    ranked: list[tuple[int, float, int, str, Mapping[str, Any]]] = []
    for seed in seeds:
        if seed.get("task_family") != family:
            continue
        exact = int(seed.get("task_signature") == signature)
        similarity = _similarity(question, str(seed.get("task_feature", "")))
        if not exact and similarity <= 0:
            continue
        ranked.append(
            (
                exact,
                similarity,
                int(seed["rank"]),
                str(seed["episode_id"]),
                seed,
            )
        )
    ranked.sort(key=lambda row: (-row[0], -row[1], row[2], row[3]))
    evidence: list[dict[str, Any]] = []
    for retrieval_rank, (exact, similarity, _rank, _episode_id, seed) in enumerate(
        ranked[:EPISODE_RETRIEVAL_LIMIT], start=1
    ):
        row = _episode_evidence({**seed, "rank": retrieval_rank}, available_raw_ids)
        row.update(
            {
                "task_family_match": True,
                "task_signature_match": bool(exact),
                "task_similarity": similarity,
            }
        )
        evidence.append(row)
    return evidence


def _select(rows: Sequence[Mapping[str, Any]], budget: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    used = 0
    for row in sorted(
        rows, key=lambda item: (int(item["rank"]), str(item["evidence_id"]))
    ):
        if used + int(row["token_count"]) <= budget:
            selected.append(dict(row))
            used += int(row["token_count"])
    return selected


def _selection(
    corpus: Sequence[Mapping[str, Any]],
    memory: Sequence[Mapping[str, Any]],
    episodes: Sequence[Mapping[str, Any]],
    budget: int,
    policy: str,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    corpus_only = _select(corpus, budget)
    if not corpus_only:
        raise MemoryExperimentError(
            "fixed context budget cannot reserve corpus evidence"
        )
    reserve = corpus_only[0]
    selected_episodes = _select(
        episodes if policy == "M4" else [], budget - int(reserve["token_count"])
    )
    corpus_by_raw_id: dict[str, list[Mapping[str, Any]]] = {}
    for row in corpus:
        for raw_id in row["raw_evidence_ids"]:
            corpus_by_raw_id.setdefault(str(raw_id), []).append(row)
    for rows in corpus_by_raw_id.values():
        rows.sort(key=lambda row: (int(row["rank"]), str(row["evidence_id"])))

    mandatory_corpus = {str(reserve["evidence_id"]): reserve}
    selected_memory: list[dict[str, Any]] = []
    candidates = memory if policy != "M0" else ()
    for row in sorted(
        candidates, key=lambda item: (int(item["rank"]), str(item["evidence_id"]))
    ):
        backing: dict[str, Mapping[str, Any]] = {}
        for raw_id in row["raw_evidence_ids"]:
            matches = corpus_by_raw_id.get(str(raw_id), [])
            if not matches:
                raise MemoryExperimentError(
                    f"{row['evidence_id']}: selected memory lacks raw corpus evidence"
                )
            match = matches[0]
            if str(match["evidence_id"]) not in mandatory_corpus:
                backing[str(match["evidence_id"])] = match
        projected = (
            sum(int(item["token_count"]) for item in mandatory_corpus.values())
            + sum(int(item["token_count"]) for item in selected_episodes)
            + sum(int(item["token_count"]) for item in selected_memory)
            + int(row["token_count"])
            + sum(int(item["token_count"]) for item in backing.values())
        )
        if projected <= budget:
            selected_memory.append(dict(row))
            mandatory_corpus.update(backing)

    mandatory_rows = [reserve] + sorted(
        (
            row
            for evidence_id, row in mandatory_corpus.items()
            if evidence_id != reserve["evidence_id"]
        ),
        key=lambda row: (int(row["rank"]), str(row["evidence_id"])),
    )
    remaining_budget = (
        budget
        - sum(int(row["token_count"]) for row in mandatory_rows)
        - sum(int(row["token_count"]) for row in selected_episodes)
        - sum(int(row["token_count"]) for row in selected_memory)
    )
    selected_corpus = mandatory_rows + _select(
        [row for row in corpus if str(row["evidence_id"]) not in mandatory_corpus],
        remaining_budget,
    )
    return corpus_only, selected_memory, selected_episodes, selected_corpus


def build_memory_trace(
    run_spec: Mapping[str, Any],
    *,
    corpus_evidence: Iterable[Mapping[str, Any]],
    memory_evidence: Iterable[Mapping[str, Any]] = (),
    m4_episode_seed: Iterable[Mapping[str, Any]] = (),
    available_raw_evidence_ids: Iterable[object],
    trusted_grade_artifacts: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    spec = dict(run_spec)
    if LEGACY_RETRIEVER_FIELDS.intersection(spec):
        raise MemoryExperimentError("legacy promoted retriever fields are ambiguous")
    spec_hash = spec.pop("run_spec_sha256", None)
    if spec_hash != sha256_json(spec):
        raise MemoryExperimentError("memory trace run spec hash is invalid")
    policy = spec.get("policy")
    budget = _positive_int(spec.get("context_budget_tokens"), "context_budget_tokens")
    if policy not in POLICIES:
        raise MemoryExperimentError("memory trace policy is invalid")
    for key in ("retriever_binding_sha256", "acceptance_parameters_sha256"):
        if not _is_sha256(spec.get(key)):
            raise MemoryExperimentError(f"memory trace {key} is invalid")
    available = set(_raw_ids(available_raw_evidence_ids))
    if spec.get("available_raw_evidence_ids_sha256") != sha256_json(sorted(available)):
        raise MemoryExperimentError("memory trace raw evidence commitment changed")
    grades = _grade_registry(trusted_grade_artifacts)
    if spec.get("trusted_grade_artifacts_sha256") != sha256_json(grades):
        raise MemoryExperimentError("memory trace trusted grade registry changed")
    corpus = [_evidence(row, "corpus", available) for row in corpus_evidence]
    memory = [_evidence(row, "memory", available) for row in memory_evidence]
    if policy == "M0" and memory:
        raise MemoryExperimentError("M0 cannot receive memory evidence")
    seed = _episodes(
        m4_episode_seed,
        available,
        {row["grade_artifact_id"]: row for row in grades},
    )
    if policy == "M4":
        if spec.get("m4_episode_seed_sha256") != sha256_json(seed) or spec.get(
            "m4_episode_seed_count"
        ) != len(seed):
            raise MemoryExperimentError("M4 trace does not use frozen episode seed")
    elif seed:
        raise MemoryExperimentError("only M4 can receive episodic seed input")
    episode_candidates = _eligible_episode_evidence(seed, spec["task"], available)
    corpus_only, selected_memory, selected_episodes, selected_corpus = _selection(
        corpus, memory, episode_candidates, budget, str(policy)
    )
    memory_tokens = sum(row["token_count"] for row in selected_memory)
    episode_tokens = sum(row["token_count"] for row in selected_episodes)
    corpus_tokens = sum(row["token_count"] for row in selected_corpus)
    selected_corpus_raw_ids = {
        raw_id for row in selected_corpus for raw_id in row["raw_evidence_ids"]
    }
    trace: dict[str, Any] = {
        "schema_version": MEMORY_TRACE_SCHEMA,
        "run_id": spec["run_id"],
        "campaign_id": spec["campaign_id"],
        "policy": policy,
        "reasoning_effort": spec["reasoning_effort"],
        "task": spec["task"],
        "run_spec_sha256": spec_hash,
        "retriever_binding_sha256": spec["retriever_binding_sha256"],
        "acceptance_parameters_sha256": spec["acceptance_parameters_sha256"],
        "generation_protocol_sha256": spec["generation_protocol_sha256"],
        "requested_model": spec["requested_model"],
        "provider": spec["provider"],
        "prompt_version": spec["prompt_version"],
        "prompt_sha256": spec["prompt_sha256"],
        "corpus_snapshot_sha256": spec["corpus_snapshot_sha256"],
        "output_token_limit": spec["output_token_limit"],
        "context_budget_tokens": budget,
        "available_raw_evidence_ids": sorted(available),
        "available_raw_evidence_ids_sha256": sha256_json(sorted(available)),
        "trusted_grade_artifacts": grades,
        "trusted_grade_artifacts_sha256": sha256_json(grades),
        "corpus_candidate_evidence": corpus,
        "memory_candidate_evidence": memory,
        "episode_candidate_evidence": episode_candidates,
        "corpus_only_selected_evidence": corpus_only,
        "selected_memory_evidence": selected_memory,
        "selected_episode_evidence": selected_episodes,
        "selected_corpus_evidence": selected_corpus,
        "selected_memory_raw_evidence_in_context": all(
            set(row["raw_evidence_ids"]).issubset(selected_corpus_raw_ids)
            for row in selected_memory
        ),
        "context_token_count": memory_tokens + episode_tokens + corpus_tokens,
        "memory_retrieval_tokens": memory_tokens,
        "episode_retrieval_tokens": episode_tokens,
        "corpus_retrieval_tokens": corpus_tokens,
        "corpus_tokens_displaced": max(
            0, sum(row["token_count"] for row in corpus_only) - corpus_tokens
        ),
        "memory_evidence_displaces_corpus": bool(selected_memory or selected_episodes)
        and [row["evidence_id"] for row in corpus_only]
        != [row["evidence_id"] for row in selected_corpus],
        "episode_evidence_displaces_corpus": bool(selected_episodes)
        and [row["evidence_id"] for row in corpus_only]
        != [row["evidence_id"] for row in selected_corpus],
        "m4_episode_seed_sha256": sha256_json(seed) if policy == "M4" else None,
        "m4_episode_seed": seed if policy == "M4" else [],
        "episode_feedback_count": 0,
        "episode_write_count": 0,
    }
    trace["trace_sha256"] = sha256_json(trace)
    validate_memory_trace(trace)
    return trace


def validate_memory_trace(trace: Mapping[str, Any]) -> None:
    if not isinstance(trace, Mapping):
        raise MemoryExperimentError("memory trace must be an object")
    if LEGACY_RETRIEVER_FIELDS.intersection(trace):
        raise MemoryExperimentError("legacy promoted retriever fields are ambiguous")
    body = {key: value for key, value in trace.items() if key != "trace_sha256"}
    if trace.get("schema_version") != MEMORY_TRACE_SCHEMA or trace.get(
        "trace_sha256"
    ) != sha256_json(body):
        raise MemoryExperimentError("memory trace hash is invalid")
    policy = trace.get("policy")
    budget = _positive_int(trace.get("context_budget_tokens"), "context_budget_tokens")
    if (
        policy not in POLICIES
        or trace.get("reasoning_effort") not in ALLOWED_REASONING_EFFORTS
    ):
        raise MemoryExperimentError("memory trace identity is invalid")
    for key in ("retriever_binding_sha256", "acceptance_parameters_sha256"):
        if not _is_sha256(trace.get(key)):
            raise MemoryExperimentError(f"memory trace {key} is invalid")
    available = set(_raw_ids(trace.get("available_raw_evidence_ids", [])))
    if trace.get("available_raw_evidence_ids_sha256") != sha256_json(sorted(available)):
        raise MemoryExperimentError("memory trace raw evidence commitment changed")
    grades = _grade_registry(trace.get("trusted_grade_artifacts", []))
    if trace.get("trusted_grade_artifacts_sha256") != sha256_json(grades):
        raise MemoryExperimentError("memory trace trusted grade registry changed")
    corpus = [
        _evidence(row, "corpus", available)
        for row in trace.get("corpus_candidate_evidence", [])
    ]
    memory = [
        _evidence(row, "memory", available)
        for row in trace.get("memory_candidate_evidence", [])
    ]
    seed = _episodes(
        trace.get("m4_episode_seed", []),
        available,
        {row["grade_artifact_id"]: row for row in grades},
    )
    if policy == "M4":
        if seed != trace.get("m4_episode_seed") or trace.get(
            "m4_episode_seed_sha256"
        ) != sha256_json(seed):
            raise MemoryExperimentError("M4 trace seed identity is invalid")
    elif seed or trace.get("m4_episode_seed_sha256") is not None:
        raise MemoryExperimentError("non-M4 trace contains episode input")
    task = trace.get("task")
    if not isinstance(task, Mapping):
        raise MemoryExperimentError("memory trace task is invalid")
    episodes = _eligible_episode_evidence(seed, task, available)
    if trace.get("episode_candidate_evidence") != episodes:
        raise MemoryExperimentError("memory trace episode candidates changed")
    corpus_only, selected_memory, selected_episodes, selected_corpus = _selection(
        corpus, memory, episodes, budget, str(policy)
    )
    for key, expected in (
        ("corpus_only_selected_evidence", corpus_only),
        ("selected_memory_evidence", selected_memory),
        ("selected_episode_evidence", selected_episodes),
        ("selected_corpus_evidence", selected_corpus),
    ):
        if trace.get(key) != expected:
            raise MemoryExperimentError("memory trace candidate selection changed")
    memory_tokens = sum(row["token_count"] for row in selected_memory)
    episode_tokens = sum(row["token_count"] for row in selected_episodes)
    corpus_tokens = sum(row["token_count"] for row in selected_corpus)
    selected_corpus_raw_ids = {
        raw_id for row in selected_corpus for raw_id in row["raw_evidence_ids"]
    }
    if (
        trace.get("context_token_count")
        != memory_tokens + episode_tokens + corpus_tokens
        or trace.get("memory_retrieval_tokens") != memory_tokens
        or trace.get("episode_retrieval_tokens") != episode_tokens
        or trace.get("corpus_retrieval_tokens") != corpus_tokens
        or memory_tokens + episode_tokens + corpus_tokens > budget
        or not selected_corpus
        or trace.get("selected_memory_raw_evidence_in_context") is not True
        or any(
            not set(row["raw_evidence_ids"]).issubset(selected_corpus_raw_ids)
            for row in selected_memory
        )
    ):
        raise MemoryExperimentError("memory trace budget accounting is invalid")
    corpus_displaced = [row["evidence_id"] for row in corpus_only] != [
        row["evidence_id"] for row in selected_corpus
    ]
    if (
        trace.get("corpus_tokens_displaced")
        != max(0, sum(row["token_count"] for row in corpus_only) - corpus_tokens)
        or trace.get("memory_evidence_displaces_corpus")
        != (bool(selected_memory or selected_episodes) and corpus_displaced)
        or trace.get("episode_evidence_displaces_corpus")
        != (bool(selected_episodes) and corpus_displaced)
    ):
        raise MemoryExperimentError("memory trace displacement accounting is invalid")
    if (
        trace.get("episode_feedback_count") != 0
        or trace.get("episode_write_count") != 0
    ):
        raise MemoryExperimentError("memory trace contains same-run episodic feedback")


def _nonnegative_number(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise MemoryExperimentError(f"{label} must be a finite non-negative number")
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise MemoryExperimentError(
            f"{label} must be a finite non-negative number"
        ) from exc
    if not math.isfinite(number) or number < 0:
        raise MemoryExperimentError(f"{label} must be a finite non-negative number")
    return number


def _canonical_used_claims(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise MemoryExperimentError("answer grade used memory claims must be a list")
    claims: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {
            "claim_id",
            "supporting_event_ids",
        }:
            raise MemoryExperimentError("answer grade memory claim fields are invalid")
        claim_id = _text(item.get("claim_id"), "answer grade claim ID")
        raw_value = item.get("supporting_event_ids")
        if not isinstance(raw_value, Sequence) or isinstance(raw_value, (str, bytes)):
            raise MemoryExperimentError("answer grade claim provenance must be a list")
        raw_ids = sorted(
            {_text(raw_id, f"{claim_id} supporting event") for raw_id in raw_value}
        )
        if claim_id in seen or not raw_ids:
            raise MemoryExperimentError(
                "answer grade claim IDs must be unique and evidence-backed"
            )
        claims.append({"claim_id": claim_id, "supporting_event_ids": raw_ids})
        seen.add(claim_id)
    return claims


def build_answer_grade_artifact(
    run_spec: Mapping[str, Any],
    trace: Mapping[str, Any],
    *,
    prepared_cell_artifact_sha256: str,
    generation_result_sha256: str,
    answer: str,
    grader_id: str,
    grade_basis: str,
    source_grade_sha256s: Sequence[str],
    answer_status: str,
    expected_answer_status: str,
    is_correct: bool,
    stale_answer: bool,
    provenance_complete: bool,
    used_memory_claims: Sequence[Mapping[str, Any]],
    relevant_memory_claim_ids: Sequence[str],
    correction_latency: float | None,
) -> dict[str, Any]:
    """Build a grade commitment; canonical runners must derive its outcome fields."""

    task = run_spec.get("task") if isinstance(run_spec, Mapping) else None
    if not isinstance(task, Mapping):
        raise MemoryExperimentError("answer grade requires a run task")
    source_hashes = sorted(
        {_text(value, "source grade hash") for value in source_grade_sha256s}
    )
    relevant = sorted(
        {
            _text(value, "relevant memory claim ID")
            for value in relevant_memory_claim_ids
        }
    )
    grade: dict[str, Any] = {
        "schema_version": G3_ANSWER_GRADE_SCHEMA,
        "grade_artifact_id": f"grade-{run_spec['run_id']}",
        "grader_id": grader_id,
        "grade_basis": grade_basis,
        "source_grade_sha256s": source_hashes,
        "run_id": run_spec.get("run_id"),
        "task_id": task.get("task_id"),
        "suite": task.get("suite"),
        "policy": run_spec.get("policy"),
        "reasoning_effort": run_spec.get("reasoning_effort"),
        "prepared_cell_artifact_sha256": prepared_cell_artifact_sha256,
        "generation_result_sha256": generation_result_sha256,
        "trace_sha256": trace.get("trace_sha256"),
        "answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
        "answer_status": answer_status,
        "expected_answer_status": expected_answer_status,
        "is_correct": is_correct,
        "stale_answer": stale_answer,
        "provenance_complete": provenance_complete,
        "used_memory_claims": _canonical_used_claims(used_memory_claims),
        "relevant_memory_claim_ids": relevant,
        "correction_latency": correction_latency,
        "correction_latency_unit": (
            "observed_event_steps"
            if task.get("suite") == "temporal"
            else "not_applicable"
        ),
    }
    grade["artifact_sha256"] = sha256_json(grade)
    _validate_answer_grade_artifact(
        grade,
        run_spec,
        trace,
        prepared_cell_artifact_sha256=prepared_cell_artifact_sha256,
        generation_result_sha256=generation_result_sha256,
        answer=answer,
    )
    return grade


def _validate_answer_grade_artifact(
    grade: Mapping[str, Any],
    run_spec: Mapping[str, Any],
    trace: Mapping[str, Any],
    *,
    prepared_cell_artifact_sha256: str,
    generation_result_sha256: str,
    answer: str,
) -> None:
    if not isinstance(grade, Mapping):
        raise MemoryExperimentError("completed result requires an answer grade")
    expected_fields = {
        "schema_version",
        "grade_artifact_id",
        "grader_id",
        "grade_basis",
        "source_grade_sha256s",
        "run_id",
        "task_id",
        "suite",
        "policy",
        "reasoning_effort",
        "prepared_cell_artifact_sha256",
        "generation_result_sha256",
        "trace_sha256",
        "answer_sha256",
        "answer_status",
        "expected_answer_status",
        "is_correct",
        "stale_answer",
        "provenance_complete",
        "used_memory_claims",
        "relevant_memory_claim_ids",
        "correction_latency",
        "correction_latency_unit",
        "artifact_sha256",
    }
    body = {key: value for key, value in grade.items() if key != "artifact_sha256"}
    task = run_spec.get("task")
    if (
        set(grade) != expected_fields
        or grade.get("schema_version") != G3_ANSWER_GRADE_SCHEMA
        or grade.get("artifact_sha256") != sha256_json(body)
        or not isinstance(task, Mapping)
        or grade.get("grade_artifact_id") != f"grade-{run_spec.get('run_id')}"
        or grade.get("run_id") != run_spec.get("run_id")
        or grade.get("task_id") != task.get("task_id")
        or grade.get("suite") != task.get("suite")
        or grade.get("policy") != run_spec.get("policy")
        or grade.get("reasoning_effort") != run_spec.get("reasoning_effort")
        or grade.get("prepared_cell_artifact_sha256") != prepared_cell_artifact_sha256
        or grade.get("generation_result_sha256") != generation_result_sha256
        or grade.get("trace_sha256") != trace.get("trace_sha256")
        or grade.get("answer_sha256")
        != hashlib.sha256(answer.encode("utf-8")).hexdigest()
    ):
        raise MemoryExperimentError("answer grade identity or hash is invalid")
    for value in (
        prepared_cell_artifact_sha256,
        generation_result_sha256,
        grade.get("trace_sha256"),
        grade.get("answer_sha256"),
    ):
        if not _is_sha256(value):
            raise MemoryExperimentError(
                "answer grade commitments require SHA-256 values"
            )
    source_hashes = grade.get("source_grade_sha256s")
    if (
        not isinstance(source_hashes, list)
        or source_hashes != sorted(set(source_hashes))
        or not source_hashes
        or any(not _is_sha256(value) for value in source_hashes)
    ):
        raise MemoryExperimentError("answer grade sources are invalid")
    suite = task.get("suite")
    expected_grades = (
        {
            (
                "deterministic",
                "public-temporal-answer-key-v1",
                1,
                "observed_event_steps",
            )
        }
        if suite == "temporal"
        else {
            ("panel-majority", "blind-panel-majority-v1", 3, "not_applicable"),
            ("deterministic", "public-static-objective-v1", 1, "not_applicable"),
        }
    )
    if (
        (
            grade.get("grader_id"),
            grade.get("grade_basis"),
            len(source_hashes),
            grade.get("correction_latency_unit"),
        )
        not in expected_grades
        or grade.get("answer_status") not in {"answer", "abstain"}
        or grade.get("expected_answer_status") not in {"answer", "abstain"}
        or not all(
            isinstance(grade.get(key), bool)
            for key in ("is_correct", "stale_answer", "provenance_complete")
        )
        or (grade.get("is_correct") is True and grade.get("stale_answer") is True)
    ):
        raise MemoryExperimentError("answer grade outcome is invalid")
    claims = _canonical_used_claims(grade.get("used_memory_claims"))
    if claims != grade.get("used_memory_claims"):
        raise MemoryExperimentError("answer grade memory claims are not canonical")
    relevant = grade.get("relevant_memory_claim_ids")
    if (
        not isinstance(relevant, list)
        or relevant != sorted(set(relevant))
        or any(not isinstance(value, str) or not value for value in relevant)
    ):
        raise MemoryExperimentError("answer grade relevant claims are invalid")
    if suite == "static" and grade.get("correction_latency") is not None:
        raise MemoryExperimentError(
            "static answer grade cannot claim correction latency"
        )
    if grade.get("correction_latency") is not None:
        _nonnegative_number(
            grade["correction_latency"], "answer grade correction latency"
        )


def build_memory_result_receipt(
    run_spec: Mapping[str, Any],
    trace: Mapping[str, Any],
    *,
    frozen_manifest: Mapping[str, Any],
    trusted_frozen_manifest_sha256: str,
    prepared_cell_artifact_sha256: str,
    generation_result: Mapping[str, Any] | None,
    grade_artifact: Mapping[str, Any] | None,
    memory_write_count: int,
    memory_write_tokens: int,
    status: str = "completed",
    failure: str | None = None,
) -> dict[str, Any]:
    if not isinstance(run_spec, Mapping) or not isinstance(trace, Mapping):
        raise MemoryExperimentError("receipt requires run spec and trace")
    validate_memory_experiment_manifest(
        frozen_manifest,
        trusted_frozen_manifest_sha256=trusted_frozen_manifest_sha256,
    )
    frozen_spec = next(
        (
            item
            for item in frozen_manifest["run_specs"]
            if item["run_id"] == run_spec.get("run_id")
        ),
        None,
    )
    if frozen_spec != run_spec:
        raise MemoryExperimentError("receipt run spec is outside frozen manifest")
    validate_memory_trace(trace)
    spec_hash = run_spec.get("run_spec_sha256")
    if trace.get("run_spec_sha256") != spec_hash or trace.get("run_id") != run_spec.get(
        "run_id"
    ):
        raise MemoryExperimentError("receipt trace differs from run spec")
    if not _is_sha256(prepared_cell_artifact_sha256):
        raise MemoryExperimentError("receipt prepared cell hash is invalid")
    if status == "completed":
        if (
            not isinstance(generation_result, Mapping)
            or generation_result.get("schema_version")
            != "contextlab.generation-result.v1"
            or generation_result.get("run_id") != run_spec.get("run_id")
            or generation_result.get("task_id") != run_spec["task"].get("task_id")
            or not isinstance(generation_result.get("answer"), str)
            or not str(generation_result["answer"]).strip()
            or not isinstance(generation_result.get("metadata"), Mapping)
            or failure is not None
        ):
            raise MemoryExperimentError(
                "completed receipt generation result is invalid"
            )
        answer = str(generation_result["answer"])
        metadata = dict(generation_result["metadata"])
        if (
            metadata.get("requested_model") != run_spec["requested_model"]
            or metadata.get("reasoning_effort") != run_spec["reasoning_effort"]
            or str(metadata.get("provider", "")).casefold() != PROVIDER_SLUG
            or metadata.get("resolved_model") not in {MODEL_ID, CANONICAL_MODEL_ID}
            or metadata.get("retry_count") != 0
            or not isinstance(metadata.get("request_id"), str)
            or not metadata["request_id"]
        ):
            raise MemoryExperimentError(
                "completed receipt provider metadata is invalid"
            )
        generation_result_sha256 = sha256_json(generation_result)
        _validate_answer_grade_artifact(
            grade_artifact,
            run_spec,
            trace,
            prepared_cell_artifact_sha256=prepared_cell_artifact_sha256,
            generation_result_sha256=generation_result_sha256,
            answer=answer,
        )
        grade = dict(grade_artifact)
        actual_usd = _nonnegative_number(metadata.get("actual_usd"), "actual_usd")
        latency_ms = _nonnegative_number(metadata.get("latency_ms"), "latency_ms")
        resolved_model = str(metadata["resolved_model"])
        provider = PROVIDER_SLUG
        failure_value = None
    elif status == "failed":
        if generation_result is not None or grade_artifact is not None:
            raise MemoryExperimentError("failed receipt cannot contain result or grade")
        answer = ""
        metadata = None
        generation_result_sha256 = None
        grade = None
        actual_usd = 0.0
        latency_ms = 0.0
        resolved_model = None
        provider = None
        failure_value = redact(failure)
    else:
        raise MemoryExperimentError("receipt status is invalid")
    receipt: dict[str, Any] = {
        "schema_version": MEMORY_RESULT_SCHEMA,
        "frozen_manifest_sha256": frozen_manifest["frozen_manifest_sha256"],
        "trusted_frozen_manifest_sha256": trusted_frozen_manifest_sha256,
        "run_spec": dict(run_spec),
        "run_id": run_spec["run_id"],
        "campaign_id": run_spec["campaign_id"],
        "policy": run_spec["policy"],
        "reasoning_effort": run_spec["reasoning_effort"],
        "run_spec_sha256": spec_hash,
        "prepared_cell_artifact_sha256": prepared_cell_artifact_sha256,
        "trace": dict(trace),
        "trace_sha256": trace["trace_sha256"],
        "status": status,
        "answer": answer,
        "failure": failure_value,
        "generation_result_sha256": generation_result_sha256,
        "generation_metadata": metadata,
        "ledger_reservation_id": run_spec["run_id"],
        "grade_artifact": grade,
        "grade_artifact_sha256": grade["artifact_sha256"] if grade else None,
        "answer_status": grade["answer_status"] if grade else "error",
        "expected_answer_status": (
            grade["expected_answer_status"] if grade else "unknown"
        ),
        "is_correct": grade["is_correct"] if grade else False,
        "stale_answer": grade["stale_answer"] if grade else False,
        "provenance_complete": grade["provenance_complete"] if grade else False,
        "used_memory_claims": grade["used_memory_claims"] if grade else [],
        "relevant_memory_claim_ids": (
            grade["relevant_memory_claim_ids"] if grade else []
        ),
        "correction_latency": grade["correction_latency"] if grade else None,
        "correction_latency_unit": (
            grade["correction_latency_unit"] if grade else None
        ),
        "memory_write_count": memory_write_count,
        "memory_write_tokens": memory_write_tokens,
        "actual_usd": actual_usd,
        "latency_ms": latency_ms,
        "requested_model": run_spec["requested_model"],
        "resolved_model": resolved_model,
        "provider": provider,
        "output_token_limit": run_spec["output_token_limit"],
        "episode_feedback_count": 0,
        "episode_write_count": 0,
        "m4_episode_seed_sha256": run_spec["m4_episode_seed_sha256"],
    }
    receipt["result_sha256"] = sha256_json(receipt)
    _validate_memory_result_receipt_in_valid_manifest(
        receipt,
        run_spec,
        frozen_manifest,
        trusted_frozen_manifest_sha256,
    )
    return receipt


def validate_memory_result_receipt(
    receipt: Mapping[str, Any],
    spec: Mapping[str, Any],
    frozen_manifest: Mapping[str, Any],
    trusted_frozen_manifest_sha256: str,
) -> None:
    """Validate a receipt against a freshly verified trusted manifest."""
    validate_memory_experiment_manifest(
        frozen_manifest,
        trusted_frozen_manifest_sha256=trusted_frozen_manifest_sha256,
    )
    _validate_memory_result_receipt_in_valid_manifest(
        receipt,
        spec,
        frozen_manifest,
        trusted_frozen_manifest_sha256,
    )


def _validate_memory_result_receipt_in_valid_manifest(
    receipt: Mapping[str, Any],
    spec: Mapping[str, Any],
    frozen_manifest: Mapping[str, Any],
    trusted_frozen_manifest_sha256: str,
) -> None:
    """Validate a receipt only after a caller has verified the manifest above."""
    if not isinstance(receipt, Mapping):
        raise MemoryExperimentError("memory result receipt must be an object")
    body = {key: value for key, value in receipt.items() if key != "result_sha256"}
    expected_fields = {
        "schema_version",
        "frozen_manifest_sha256",
        "trusted_frozen_manifest_sha256",
        "run_spec",
        "run_id",
        "campaign_id",
        "policy",
        "reasoning_effort",
        "run_spec_sha256",
        "prepared_cell_artifact_sha256",
        "trace",
        "trace_sha256",
        "status",
        "answer",
        "failure",
        "generation_result_sha256",
        "generation_metadata",
        "ledger_reservation_id",
        "grade_artifact",
        "grade_artifact_sha256",
        "answer_status",
        "expected_answer_status",
        "is_correct",
        "stale_answer",
        "provenance_complete",
        "used_memory_claims",
        "relevant_memory_claim_ids",
        "correction_latency",
        "correction_latency_unit",
        "memory_write_count",
        "memory_write_tokens",
        "actual_usd",
        "latency_ms",
        "requested_model",
        "resolved_model",
        "provider",
        "output_token_limit",
        "episode_feedback_count",
        "episode_write_count",
        "m4_episode_seed_sha256",
        "result_sha256",
    }
    if (
        set(receipt) != expected_fields
        or receipt.get("schema_version") != MEMORY_RESULT_SCHEMA
        or receipt.get("result_sha256") != sha256_json(body)
    ):
        raise MemoryExperimentError("memory result receipt hash is invalid")
    _trusted_commitment(frozen_manifest, trusted_frozen_manifest_sha256)
    if receipt.get("frozen_manifest_sha256") != frozen_manifest.get(
        "frozen_manifest_sha256"
    ):
        raise MemoryExperimentError("memory result receipt frozen manifest changed")
    if receipt.get("trusted_frozen_manifest_sha256") != trusted_frozen_manifest_sha256:
        raise MemoryExperimentError("memory result receipt trusted commitment changed")
    frozen_spec = next(
        (
            item
            for item in frozen_manifest["run_specs"]
            if item["run_id"] == spec.get("run_id")
        ),
        None,
    )
    if frozen_spec != spec:
        raise MemoryExperimentError("memory result receipt run spec is not frozen")
    if receipt.get("run_spec") != spec:
        raise MemoryExperimentError("memory result receipt run spec differs")
    required = (
        "run_id",
        "campaign_id",
        "policy",
        "reasoning_effort",
        "run_spec_sha256",
        "requested_model",
        "output_token_limit",
        "m4_episode_seed_sha256",
    )
    if (
        any(receipt.get(key) != spec.get(key) for key in required)
        or receipt.get("ledger_reservation_id") != spec.get("run_id")
        or not _is_sha256(receipt.get("prepared_cell_artifact_sha256"))
    ):
        raise MemoryExperimentError("memory result receipt identity changed")
    trace = receipt.get("trace")
    if not isinstance(trace, Mapping) or receipt.get("trace_sha256") != trace.get(
        "trace_sha256"
    ):
        raise MemoryExperimentError("memory result receipt trace is missing")
    validate_memory_trace(trace)
    if trace.get("run_spec_sha256") != spec.get("run_spec_sha256") or trace.get(
        "run_id"
    ) != spec.get("run_id"):
        raise MemoryExperimentError("memory result receipt trace differs")
    for key in (
        "campaign_id",
        "policy",
        "reasoning_effort",
        "retriever_binding_sha256",
        "acceptance_parameters_sha256",
        "generation_protocol_sha256",
        "requested_model",
        "provider",
        "prompt_version",
        "prompt_sha256",
        "corpus_snapshot_sha256",
        "output_token_limit",
        "context_budget_tokens",
        "available_raw_evidence_ids_sha256",
        "trusted_grade_artifacts_sha256",
        "m4_episode_seed_sha256",
    ):
        if trace.get(key) != spec.get(key):
            raise MemoryExperimentError(
                "memory result receipt trace commitment changed"
            )
    status = receipt.get("status")
    if status not in {"completed", "failed"}:
        raise MemoryExperimentError("memory result receipt outcome is incomplete")
    if status == "completed":
        metadata = receipt.get("generation_metadata")
        grade = receipt.get("grade_artifact")
        generation = {
            "schema_version": "contextlab.generation-result.v1",
            "run_id": receipt.get("run_id"),
            "task_id": spec["task"]["task_id"],
            "answer": receipt.get("answer"),
            "metadata": metadata,
        }
        if (
            not _text(receipt.get("answer"), "answer")
            or receipt.get("answer_status") not in {"answer", "abstain"}
            or receipt.get("expected_answer_status") not in {"answer", "abstain"}
            or receipt.get("failure") is not None
            or not isinstance(metadata, Mapping)
            or receipt.get("generation_result_sha256") != sha256_json(generation)
            or metadata.get("requested_model") != spec.get("requested_model")
            or metadata.get("reasoning_effort") != spec.get("reasoning_effort")
            or str(metadata.get("provider", "")).casefold() != PROVIDER_SLUG
            or metadata.get("resolved_model") not in {MODEL_ID, CANONICAL_MODEL_ID}
            or metadata.get("retry_count") != 0
            or not isinstance(metadata.get("request_id"), str)
            or not metadata["request_id"]
            or receipt.get("provider") != PROVIDER_SLUG
            or receipt.get("resolved_model") != metadata.get("resolved_model")
            or receipt.get("actual_usd")
            != _nonnegative_number(metadata.get("actual_usd"), "actual_usd")
            or receipt.get("latency_ms")
            != _nonnegative_number(metadata.get("latency_ms"), "latency_ms")
            or not all(
                isinstance(receipt.get(key), bool)
                for key in ("is_correct", "stale_answer", "provenance_complete")
            )
        ):
            raise MemoryExperimentError(
                "completed result receipt outcome is incomplete"
            )
        _validate_answer_grade_artifact(
            grade,
            spec,
            trace,
            prepared_cell_artifact_sha256=str(receipt["prepared_cell_artifact_sha256"]),
            generation_result_sha256=str(receipt["generation_result_sha256"]),
            answer=str(receipt["answer"]),
        )
        if receipt.get("grade_artifact_sha256") != grade.get("artifact_sha256") or any(
            receipt.get(key) != grade.get(key)
            for key in (
                "answer_status",
                "expected_answer_status",
                "is_correct",
                "stale_answer",
                "provenance_complete",
                "used_memory_claims",
                "relevant_memory_claim_ids",
                "correction_latency",
                "correction_latency_unit",
            )
        ):
            raise MemoryExperimentError("result receipt differs from its answer grade")
    else:
        if (
            receipt.get("answer") != ""
            or receipt.get("answer_status") != "error"
            or receipt.get("expected_answer_status") != "unknown"
            or not _text(receipt.get("failure"), "failure")
            or receipt.get("failure") != redact(receipt.get("failure"))
            or receipt.get("generation_result_sha256") is not None
            or receipt.get("generation_metadata") is not None
            or receipt.get("grade_artifact") is not None
            or receipt.get("grade_artifact_sha256") is not None
            or receipt.get("provider") is not None
            or receipt.get("resolved_model") is not None
            or receipt.get("actual_usd") != 0.0
            or receipt.get("latency_ms") != 0.0
            or any(
                receipt.get(key) is not False
                for key in ("is_correct", "stale_answer", "provenance_complete")
            )
            or receipt.get("used_memory_claims") != []
            or receipt.get("relevant_memory_claim_ids") != []
            or receipt.get("correction_latency") is not None
            or receipt.get("correction_latency_unit") is not None
        ):
            raise MemoryExperimentError(
                "failed result receipt must not fabricate an outcome"
            )
    if (
        receipt.get("episode_feedback_count") != 0
        or receipt.get("episode_write_count") != 0
    ):
        raise MemoryExperimentError("same-run episodic feedback is forbidden")
    _nonnegative_number(receipt.get("actual_usd"), "actual_usd")
    _nonnegative_number(receipt.get("latency_ms"), "latency_ms")
    for key in ("memory_write_count", "memory_write_tokens"):
        if (
            isinstance(receipt.get(key), bool)
            or not isinstance(receipt.get(key), int)
            or receipt[key] < 0
        ):
            raise MemoryExperimentError(f"{key} is invalid")
    if receipt.get("correction_latency") is not None:
        _nonnegative_number(receipt["correction_latency"], "correction_latency")


def resume_memory_experiment(
    manifest: Mapping[str, Any],
    results: Iterable[Mapping[str, Any]],
    *,
    trusted_frozen_manifest_sha256: str,
) -> dict[str, Any]:
    validate_memory_experiment_manifest(
        manifest,
        trusted_frozen_manifest_sha256=trusted_frozen_manifest_sha256,
    )
    updated = json.loads(json.dumps(manifest, sort_keys=True))
    specs = {row["run_id"]: row for row in updated["run_specs"]}
    cells = {row["run_id"]: row for row in updated["cells"]}
    for receipt in results:
        run_id = receipt.get("run_id") if isinstance(receipt, Mapping) else None
        spec = specs.get(str(run_id))
        if spec is None or receipt.get("campaign_id") != updated["campaign_id"]:
            raise MemoryExperimentError("memory result is outside this campaign")
        _validate_memory_result_receipt_in_valid_manifest(
            receipt,
            spec,
            updated,
            trusted_frozen_manifest_sha256,
        )
        cell = cells[str(run_id)]
        if cell["status"] != "pending":
            if cell["result_sha256"] != receipt["result_sha256"]:
                raise MemoryExperimentError("saved memory result cannot be replaced")
            continue
        cell.update(
            {
                "status": receipt["status"],
                "result_sha256": receipt["result_sha256"],
                "result_receipt": dict(receipt),
            }
        )
    updated["cells"] = [cells[row["run_id"]] for row in updated["run_specs"]]
    updated["status_counts"] = {
        status: sum(cell["status"] == status for cell in updated["cells"])
        for status in ("completed", "failed", "pending")
    }
    updated.pop("manifest_sha256")
    updated["manifest_sha256"] = sha256_json(updated)
    validate_memory_experiment_manifest(
        updated,
        trusted_frozen_manifest_sha256=trusted_frozen_manifest_sha256,
    )
    return updated
