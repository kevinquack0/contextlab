"""Replayable, content-free Week 12 temporal-memory report."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any

from .baseline import repository_root
from .immutable_io import (
    ImmutableIOError,
    read_bytes_snapshot,
    write_json_once_or_verify,
)
from .g3_freeze import G3_FREEZE_SCHEMA, validate_g3_freeze
from .g3_sealed import (
    G3_SEALED_CANDIDATE_SCHEMA,
    G3_SEALED_IMPORT_SCHEMA,
    G3_SEALED_RETURN_SCHEMA,
    build_g3_sealed_metrics,
    validate_g3_sealed_candidate_manifest,
    validate_g3_sealed_return,
)
from .tasking import sha256_json


G3_TEMPORAL_MEMORY_REPORT_SCHEMA = "contextlab.g3-temporal-memory-report.v1"
G3_TEMPORAL_MEMORY_REPORT_PATH = Path(
    "results/v2/reports/g3_temporal_memory_report.json"
)

_SOURCE_SPECS = {
    "public_metrics": (
        Path("results/v2/memory/g3_public_metrics.json"),
        "contextlab.g3-memory-metrics.v4",
    ),
    "failure_and_harm": (
        Path("results/v2/memory/g3_failure_and_harm_report.json"),
        "contextlab.g3-failure-and-harm-report.v1",
    ),
    "lifecycle": (
        Path("results/v2/memory/g3_lifecycle_evidence.json"),
        "contextlab.g3-lifecycle-evidence.v1",
    ),
    "public_generation_run": (
        Path("results/v2/memory/g3_public_generation_run.json"),
        "contextlab.g3-public-generation-run.v1",
    ),
    "prior_bootstrap": (
        Path("results/v2/memory/g3_prior_bootstrap.json"),
        "contextlab.g3-prior-bootstrap.v1",
    ),
    "sealed_import": (
        Path("results/v2/memory/g3_sealed_import.json"),
        "contextlab.g3-sealed-import.v1",
    ),
    "unsupported_memory_review": (
        Path("results/v2/reviews/g3_unsupported_memory_dispositions.json"),
        "contextlab.g3-unsupported-memory-review.v2",
    ),
}
_CONFIGURATIONS = tuple(
    f"M{policy}:{effort}" for policy in range(5) for effort in ("low", "high")
)
_MEMORY_CONFIGURATIONS = tuple(
    f"M{policy}:{effort}" for policy in range(1, 5) for effort in ("low", "high")
)
_G3_FREEZE_PATH = Path("results/v2/memory/g3_public_freeze.json")
_G3_SEALED_CANDIDATE_PATH = Path("results/v2/memory/g3_sealed_candidates.json")


class MemoryReportError(ValueError):
    """A source or report does not replay exactly."""


def _artifact_hash_valid(value: Mapping[str, Any]) -> bool:
    return value.get("artifact_sha256") == sha256_json(
        {key: item for key, item in value.items() if key != "artifact_sha256"}
    )


def _load_source(
    root: Path, relative: Path, schema_version: str
) -> tuple[dict[str, Any], str]:
    try:
        payload = read_bytes_snapshot(root, root / relative)
        value = json.loads(payload.decode("utf-8"))
    except (ImmutableIOError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MemoryReportError(f"cannot read {relative.as_posix()}") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != schema_version
        or not _artifact_hash_valid(value)
    ):
        raise MemoryReportError(f"source artifact is invalid: {relative.as_posix()}")
    return value, hashlib.sha256(payload).hexdigest()


def _load_sources(root: Path) -> dict[str, tuple[dict[str, Any], str]]:
    return {
        name: _load_source(root, path, schema)
        for name, (path, schema) in _SOURCE_SPECS.items()
    }


def _validate_strict_g3_sealed_import(root: Path, sealed: Mapping[str, Any]) -> None:
    """Replay the imported aggregate through the sealed boundary contract."""

    try:
        freeze, _ = _load_source(root, _G3_FREEZE_PATH, G3_FREEZE_SCHEMA)
        candidate, _ = _load_source(
            root,
            _G3_SEALED_CANDIDATE_PATH,
            G3_SEALED_CANDIDATE_SCHEMA,
        )
        validate_g3_freeze(freeze)
        validate_g3_sealed_candidate_manifest(candidate)
    except (MemoryReportError, ValueError) as exc:
        raise MemoryReportError(
            "content-free sealed import has invalid canonical dependencies"
        ) from exc

    expected_fields = {
        "schema_version",
        "evaluation_id",
        "candidate_manifest_sha256",
        "g3_freeze_sha256",
        "external_bundle_sha256",
        "temporal_event_history_sha256",
        "requested_model",
        "provider",
        "source_return_sha256",
        "records",
        "aggregate_metadata",
        "sealed_metrics",
        "artifact_sha256",
    }
    source_return_sha256 = sealed.get("source_return_sha256")
    if (
        set(sealed) != expected_fields
        or sealed.get("schema_version") != G3_SEALED_IMPORT_SCHEMA
        or sealed.get("candidate_manifest_sha256") != candidate.get("artifact_sha256")
        or sealed.get("g3_freeze_sha256") != freeze.get("artifact_sha256")
        or sealed.get("external_bundle_sha256")
        != candidate.get("external_bundle_sha256")
        or sealed.get("temporal_event_history_sha256")
        != candidate.get("temporal_event_history_sha256")
        or sealed.get("requested_model") != candidate.get("requested_model")
        or sealed.get("provider") != candidate.get("provider")
        or not isinstance(source_return_sha256, str)
        or len(source_return_sha256) != 64
        or any(
            character not in "0123456789abcdef" for character in source_return_sha256
        )
    ):
        raise MemoryReportError(
            "content-free sealed import differs from its canonical schema"
        )

    reconstructed: dict[str, Any] = {
        "schema_version": G3_SEALED_RETURN_SCHEMA,
        "evaluation_id": sealed.get("evaluation_id"),
        "candidate_manifest_sha256": sealed.get("candidate_manifest_sha256"),
        "g3_freeze_sha256": sealed.get("g3_freeze_sha256"),
        "external_bundle_sha256": sealed.get("external_bundle_sha256"),
        "temporal_event_history_sha256": sealed.get("temporal_event_history_sha256"),
        "requested_model": sealed.get("requested_model"),
        "provider": sealed.get("provider"),
        "records": sealed.get("records"),
        "aggregate_metadata": sealed.get("aggregate_metadata"),
    }
    reconstructed["artifact_sha256"] = sha256_json(reconstructed)
    try:
        validate_g3_sealed_return(reconstructed, candidate)
        recomputed_metrics = build_g3_sealed_metrics(sealed)
    except (KeyError, TypeError, ValueError) as exc:
        raise MemoryReportError(
            "content-free sealed import violates its strict return contract"
        ) from exc
    if sealed.get("sealed_metrics") != recomputed_metrics:
        raise MemoryReportError(
            "content-free sealed metrics differ from the validated records"
        )


def _validate_sources(
    sources: Mapping[str, tuple[dict[str, Any], str]], *, root: Path
) -> None:
    if set(sources) != set(_SOURCE_SPECS):
        raise MemoryReportError("temporal-memory report sources are incomplete")
    metrics = sources["public_metrics"][0]
    failure = sources["failure_and_harm"][0]
    lifecycle = sources["lifecycle"][0]
    generation = sources["public_generation_run"][0]
    sealed = sources["sealed_import"][0]
    unsupported = sources["unsupported_memory_review"][0]

    _validate_strict_g3_sealed_import(root, sealed)

    policy_metrics = metrics.get("policy_effort_metrics")
    screen = metrics.get("acceptance_screen")
    comparisons = metrics.get("paired_comparisons")
    effort_effects = metrics.get("reasoning_effort_effects")
    acceptance = metrics.get("acceptance_parameters")
    if (
        not isinstance(policy_metrics, Mapping)
        or set(policy_metrics) != set(_CONFIGURATIONS)
        or not isinstance(screen, Mapping)
        or set(screen) != set(_CONFIGURATIONS)
        or not isinstance(comparisons, Mapping)
        or set(comparisons) != set(_CONFIGURATIONS)
        or not isinstance(effort_effects, Mapping)
        or not isinstance(acceptance, Mapping)
        or acceptance.get("primary_metric") != "temporal_accuracy"
        or acceptance.get("paired_bootstrap_resamples") != 10_000
        or metrics.get("bootstrap_resamples") != 10_000
        or metrics.get("bootstrap_seed") != acceptance.get("paired_bootstrap_seed")
    ):
        raise MemoryReportError("public temporal-memory metrics are incomplete")
    for configuration in _CONFIGURATIONS:
        row = policy_metrics[configuration]
        if (
            not isinstance(row, Mapping)
            or row.get("temporal_task_count") != 28
            or row.get("static_task_count") != 84
        ):
            raise MemoryReportError("public task coverage changed")
    if (
        failure.get("public_metrics_sha256") != metrics["artifact_sha256"]
        or failure.get("sealed_import_sha256") != sealed["artifact_sha256"]
        or failure.get("configuration_count") != 10
        or tuple(failure.get("rejected_configurations", ())) != _MEMORY_CONFIGURATIONS
        or tuple(failure.get("harmful_configurations", ())) != _MEMORY_CONFIGURATIONS
    ):
        raise MemoryReportError("failure-and-harm evidence is stale or incomplete")
    if (
        lifecycle.get("all_passed") is not True
        or lifecycle.get("event_count") != 36
        or lifecycle.get("policies") != ["M0", "M1", "M2", "M3", "M4"]
    ):
        raise MemoryReportError("memory lifecycle replay is incomplete")
    if (
        generation.get("expected_full_cell_count") != 1_120
        or generation.get("recorded_cell_count") != 1_120
        or not isinstance(generation.get("generation_status_counts"), Mapping)
    ):
        raise MemoryReportError("public generation coverage is incomplete")
    sealed_metrics = sealed.get("sealed_metrics")
    aggregate = sealed.get("aggregate_metadata")
    if (
        not isinstance(sealed_metrics, Mapping)
        or set(sealed_metrics) != set(_CONFIGURATIONS)
        or not isinstance(aggregate, Mapping)
        or aggregate.get("cell_count") != 120
        or aggregate.get("task_count") != 12
    ):
        raise MemoryReportError("content-free sealed aggregates are incomplete")
    summary = unsupported.get("summary")
    if (
        unsupported.get("source_public_metrics_sha256") != metrics["artifact_sha256"]
        or not isinstance(summary, Mapping)
        or summary.get("all_source_rows_disposed") is not True
        or summary.get("disposition_count") != 558
        or summary.get("unresolved_count") != 0
    ):
        raise MemoryReportError("unsupported-memory review is incomplete")


def build_g3_temporal_memory_report(
    root: Path | None = None,
) -> dict[str, Any]:
    """Build the named Week 12 report from current immutable evidence."""

    repository = (root or repository_root()).resolve()
    sources = _load_sources(repository)
    _validate_sources(sources, root=repository)
    metrics = sources["public_metrics"][0]
    failure = sources["failure_and_harm"][0]
    lifecycle = sources["lifecycle"][0]
    generation = sources["public_generation_run"][0]
    sealed = sources["sealed_import"][0]
    unsupported = sources["unsupported_memory_review"][0]

    report: dict[str, Any] = {
        "schema_version": G3_TEMPORAL_MEMORY_REPORT_SCHEMA,
        "status": "technical-result-complete-pending-calibration-and-kevin-decision",
        "scope": {
            "public_temporal_tasks_per_configuration": 28,
            "public_static_regression_tasks_per_configuration": 84,
            "sealed_temporal_tasks_per_configuration": 12,
            "policy_effort_configurations": list(_CONFIGURATIONS),
            "sealed_content_policy": "content_free_aggregates_only",
        },
        "source_artifacts": {
            name: {
                "path": _SOURCE_SPECS[name][0].as_posix(),
                "schema_version": value[0]["schema_version"],
                "artifact_sha256": value[0]["artifact_sha256"],
                "file_sha256": value[1],
            }
            for name, value in sources.items()
        },
        "experimental_controls": deepcopy(metrics["acceptance_parameters"]),
        "public_results": {
            "policy_effort_metrics": deepcopy(metrics["policy_effort_metrics"]),
            "paired_comparisons": deepcopy(metrics["paired_comparisons"]),
            "reasoning_effort_effects": deepcopy(metrics["reasoning_effort_effects"]),
            "acceptance_screen": deepcopy(metrics["acceptance_screen"]),
            "generation_status_counts": deepcopy(
                generation["generation_status_counts"]
            ),
            "grade_status_counts": deepcopy(generation["grade_status_counts"]),
            "completed_generation_cost_usd": generation[
                "completed_generation_cost_usd"
            ],
        },
        "sealed_results": {
            "aggregate_metadata": deepcopy(sealed["aggregate_metadata"]),
            "policy_effort_metrics": deepcopy(sealed["sealed_metrics"]),
        },
        "lifecycle_summary": {
            "all_passed": lifecycle["all_passed"],
            "event_count": lifecycle["event_count"],
            "policies": deepcopy(lifecycle["policies"]),
            "lifecycle_check_count": len(lifecycle["lifecycle_checks"]),
            "replay_check_count": len(lifecycle["replay_checks"]),
        },
        "failure_and_harm": {
            "failed_configurations": deepcopy(failure["failed_configurations"]),
            "harmful_configurations": deepcopy(failure["harmful_configurations"]),
            "rejected_configurations": deepcopy(failure["rejected_configurations"]),
            "configurations": deepcopy(failure["configurations"]),
        },
        "unsupported_memory_audit": deepcopy(unsupported["summary"]),
        "technical_conclusion": {
            "eligible_memory_configurations": [],
            "candidate_decision": "retain-simple",
            "final_gate_decision": "pending",
            "reason": "No M1-M4 low/high lane improved the temporal target while satisfying the frozen static, sealed, provenance, and failure controls.",
        },
        "claim_limits": [
            "The public acceptance screen is not the G3 gate.",
            "No sealed question, answer, trace, reference target, or identity is included.",
            "The final G3 decision remains pending the three-member calibration and Kevin's separate gate decision.",
            "Failed and harmful configurations remain part of the result and are not discarded.",
        ],
    }
    report["artifact_sha256"] = sha256_json(report)
    validate_g3_temporal_memory_report(report, sources=sources)
    return report


def validate_g3_temporal_memory_report(
    value: Mapping[str, Any],
    *,
    root: Path | None = None,
    sources: Mapping[str, tuple[dict[str, Any], str]] | None = None,
) -> None:
    """Require byte-current sources and the exact deterministic report."""

    if not isinstance(value, Mapping) or not _artifact_hash_valid(value):
        raise MemoryReportError("temporal-memory report hash is invalid")
    repository = (root or repository_root()).resolve()
    current_sources = (
        dict(sources) if sources is not None else _load_sources(repository)
    )
    _validate_sources(current_sources, root=repository)
    if value.get("schema_version") != G3_TEMPORAL_MEMORY_REPORT_SCHEMA:
        raise MemoryReportError("temporal-memory report schema changed")
    if sources is None:
        expected = build_g3_temporal_memory_report(repository)
        if dict(value) != expected:
            raise MemoryReportError("temporal-memory report is stale or changed")


def write_g3_temporal_memory_report(
    root: Path | None = None,
) -> dict[str, Any]:
    """Create or verify the canonical report without overwriting other bytes."""

    repository = (root or repository_root()).resolve()
    report = build_g3_temporal_memory_report(repository)
    try:
        write_json_once_or_verify(
            repository,
            repository / G3_TEMPORAL_MEMORY_REPORT_PATH,
            report,
        )
    except ImmutableIOError as exc:
        raise MemoryReportError("immutable temporal-memory report differs") from exc
    return report
