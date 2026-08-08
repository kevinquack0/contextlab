"""Scoring and claim-level audit exports for the fixed G3 experiment surface."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .memory_experiments import (
    MEMORY_CONFIGURATIONS,
    PUBLIC_STATIC_TASK_COUNT,
    PUBLIC_TEMPORAL_TASK_COUNT,
    _validate_memory_result_receipt_in_valid_manifest,
    validate_memory_experiment_manifest,
)
from .provider import ALLOWED_REASONING_EFFORTS
from .static_benchmark import public_static_tasks
from .statistics import (
    distribution_summary,
    paired_bootstrap_ci,
    task_family_effect_summaries,
)
from .tasking import sha256_json
from .temporal import public_temporal_tasks


MEMORY_METRICS_SCHEMA = "contextlab.g3-memory-metrics.v4"
UNSUPPORTED_MEMORY_EXPORT_SCHEMA = "contextlab.g3-unsupported-memory-answers.v3"


class MemoryMetricsError(ValueError):
    """A G3 result receipt cannot be scored as a fixed paired experiment."""


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MemoryMetricsError(f"{label} must be non-empty text")
    return value


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _expected_tasks() -> tuple[dict[str, dict[str, str]], set[str], set[str]]:
    rows = [*public_temporal_tasks(), *public_static_tasks()]
    view: dict[str, dict[str, str]] = {}
    for row in rows:
        task_id = _text(row.get("task_id"), "task ID")
        view[task_id] = {
            "suite": _text(row.get("suite"), f"{task_id} suite"),
            "task_family": _text(row.get("task_family"), f"{task_id} task family"),
        }
    temporal = {task_id for task_id, row in view.items() if row["suite"] == "temporal"}
    static = {task_id for task_id, row in view.items() if row["suite"] == "static"}
    if (
        len(temporal) != PUBLIC_TEMPORAL_TASK_COUNT
        or len(static) != PUBLIC_STATIC_TASK_COUNT
    ):
        raise MemoryMetricsError("frozen G3 public task surface changed")
    return view, temporal, static


def _claim_rows(
    value: object, trace: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise MemoryMetricsError("used_memory_claims must be a list")
    candidate_by_claim = {
        str(row["claim_id"]): row for row in trace["selected_memory_evidence"]
    }
    claims: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise MemoryMetricsError("used_memory_claims entries must be objects")
        claim_id = _text(item.get("claim_id"), "memory claim ID")
        if claim_id in seen:
            raise MemoryMetricsError("used_memory_claims IDs must be unique")
        supplied = item.get("supporting_event_ids", [])
        if not isinstance(supplied, Sequence) or isinstance(supplied, (str, bytes)):
            raise MemoryMetricsError(f"{claim_id}: supporting_event_ids must be a list")
        raw_ids = sorted({_text(raw, f"{claim_id} raw evidence") for raw in supplied})
        candidate = candidate_by_claim.get(claim_id)
        trace_raw_ids = (
            sorted(candidate["raw_evidence_ids"]) if candidate is not None else []
        )
        valid = (
            candidate is not None
            and bool(raw_ids)
            and set(raw_ids).issubset(trace_raw_ids)
        )
        claim = {
            "claim_id": claim_id,
            "supporting_event_ids": raw_ids,
            "trace_raw_evidence_ids": trace_raw_ids,
            "provenance_valid": valid,
        }
        claims.append(claim)
        if not valid:
            unsupported.append(claim)
        seen.add(claim_id)
    return claims, unsupported


def _outcome(
    receipt: Mapping[str, Any],
    frozen_manifest: Mapping[str, Any],
    trusted_frozen_manifest_sha256: str,
) -> dict[str, Any]:
    if not isinstance(receipt, Mapping) or not isinstance(
        receipt.get("run_spec"), Mapping
    ):
        raise MemoryMetricsError("G3 scorer requires schema-versioned result receipts")
    spec = receipt["run_spec"]
    try:
        _validate_memory_result_receipt_in_valid_manifest(
            receipt,
            spec,
            frozen_manifest,
            trusted_frozen_manifest_sha256,
        )
    except ValueError as exc:
        raise MemoryMetricsError(str(exc)) from exc
    task = spec["task"]
    trace = receipt["trace"]
    claims, unsupported = _claim_rows(receipt["used_memory_claims"], trace)
    relevant = receipt["relevant_memory_claim_ids"]
    if not isinstance(relevant, Sequence) or isinstance(relevant, (str, bytes)):
        raise MemoryMetricsError("relevant_memory_claim_ids must be a list")
    relevant_ids = sorted(
        {_text(item, "relevant memory claim ID") for item in relevant}
    )
    if (
        receipt["correction_latency"] is not None
        and float(receipt["correction_latency"]) < 0
    ):
        raise MemoryMetricsError("correction_latency must not be negative")
    return {
        "receipt": dict(receipt),
        "run_id": receipt["run_id"],
        "task_id": task["task_id"],
        "policy": receipt["policy"],
        "reasoning_effort": receipt["reasoning_effort"],
        "suite": task["suite"],
        "task_family": task["task_family"],
        "answer": receipt["answer"],
        "status": receipt["status"],
        "answer_status": receipt["answer_status"],
        "expected_answer_status": receipt["expected_answer_status"],
        "is_correct": receipt["is_correct"],
        "stale_answer": receipt["stale_answer"],
        "provenance_complete": receipt["provenance_complete"],
        "claims": claims,
        "unsupported_claims": unsupported,
        "relevant_ids": relevant_ids,
        "correction_latency": receipt["correction_latency"],
        "memory_write_count": receipt["memory_write_count"],
        "memory_write_tokens": receipt["memory_write_tokens"],
        "memory_retrieval_tokens": trace["memory_retrieval_tokens"],
        "episode_retrieval_tokens": trace["episode_retrieval_tokens"],
        "corpus_retrieval_tokens": trace["corpus_retrieval_tokens"],
        "actual_usd": float(receipt["actual_usd"]),
        "latency_ms": float(receipt["latency_ms"]),
    }


def unsupported_memory_answers(
    outcomes: Iterable[Mapping[str, Any]],
    *,
    frozen_manifest: Mapping[str, Any],
    trusted_frozen_manifest_sha256: str,
) -> list[dict[str, Any]]:
    validate_memory_experiment_manifest(
        frozen_manifest,
        trusted_frozen_manifest_sha256=trusted_frozen_manifest_sha256,
    )
    rows = [
        _outcome(
            outcome,
            frozen_manifest,
            trusted_frozen_manifest_sha256,
        )
        for outcome in outcomes
    ]
    export: list[dict[str, Any]] = []
    for row in rows:
        if (
            row["status"] == "completed"
            and row["policy"] != "M0"
            and (row["unsupported_claims"] or not row["provenance_complete"])
        ):
            export.append(
                {
                    "run_id": row["run_id"],
                    "task_id": row["task_id"],
                    "policy": row["policy"],
                    "reasoning_effort": row["reasoning_effort"],
                    "answer": row["answer"],
                    "provenance_complete": row["provenance_complete"],
                    "unsupported_claims": row["unsupported_claims"],
                    "claim_provenance": row["claims"],
                }
            )
    return sorted(
        export, key=lambda row: (row["policy"], row["reasoning_effort"], row["task_id"])
    )


def build_unsupported_memory_answer_export(
    outcomes: Iterable[Mapping[str, Any]],
    *,
    frozen_manifest: Mapping[str, Any],
    trusted_frozen_manifest_sha256: str,
) -> dict[str, Any]:
    rows = unsupported_memory_answers(
        outcomes,
        frozen_manifest=frozen_manifest,
        trusted_frozen_manifest_sha256=trusted_frozen_manifest_sha256,
    )
    payload: dict[str, Any] = {
        "schema_version": UNSUPPORTED_MEMORY_EXPORT_SCHEMA,
        "answer_count": len(rows),
        "answers": rows,
    }
    payload["artifact_sha256"] = sha256_json(payload)
    return payload


def _summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    temporal = [row for row in rows if row["suite"] == "temporal"]
    static = [row for row in rows if row["suite"] == "static"]
    abstention = [row for row in temporal if row["expected_answer_status"] == "abstain"]
    used_occurrences = [
        (str(row["task_id"]), str(claim["claim_id"]))
        for row in rows
        for claim in row["claims"]
    ]
    relevant_occurrences = {
        (str(row["task_id"]), str(claim_id))
        for row in rows
        for claim_id in row["relevant_ids"]
    }
    relevant_used_count = sum(
        occurrence in relevant_occurrences for occurrence in used_occurrences
    )
    valid_occurrence_count = sum(
        bool(claim["provenance_valid"]) for row in rows for claim in row["claims"]
    )
    accepted = [row for row in rows if row["is_correct"]]
    return {
        "temporal_task_count": len(temporal),
        "static_task_count": len(static),
        "temporal_accuracy": _mean([float(row["is_correct"]) for row in temporal]),
        "static_accuracy": _mean([float(row["is_correct"]) for row in static]),
        "stale_answer_rate": _mean([float(row["stale_answer"]) for row in temporal]),
        "correction_latency": _mean(
            [
                float(row["correction_latency"])
                for row in temporal
                if row["correction_latency"] is not None
            ]
        ),
        "provenance_completeness": _mean(
            [float(row["provenance_complete"]) for row in rows]
        ),
        "used_memory_claim_occurrence_count": len(used_occurrences),
        "relevant_memory_claim_occurrence_count": len(relevant_occurrences),
        "memory_precision": relevant_used_count / len(used_occurrences)
        if used_occurrences
        else None,
        "memory_recall": relevant_used_count / len(relevant_occurrences)
        if relevant_occurrences
        else None,
        "claim_level_provenance_rate": valid_occurrence_count / len(used_occurrences)
        if used_occurrences
        else None,
        "correct_abstention_rate": _mean(
            [
                float(row["answer_status"] == "abstain" and row["is_correct"])
                for row in abstention
            ]
        ),
        "abstention_task_count": len(abstention),
        "memory_write_count": sum(row["memory_write_count"] for row in rows),
        "memory_write_tokens": sum(row["memory_write_tokens"] for row in rows),
        "memory_retrieval_tokens": sum(row["memory_retrieval_tokens"] for row in rows),
        "episode_retrieval_tokens": sum(
            row["episode_retrieval_tokens"] for row in rows
        ),
        "corpus_retrieval_tokens": sum(row["corpus_retrieval_tokens"] for row in rows),
        "total_retrieval_tokens": sum(
            row["memory_retrieval_tokens"]
            + row["episode_retrieval_tokens"]
            + row["corpus_retrieval_tokens"]
            for row in rows
        ),
        "generation_cost_usd": sum(row["actual_usd"] for row in rows),
        "generation_cost_distribution_usd": distribution_summary(
            [row["actual_usd"] for row in rows]
        ),
        "generation_latency_ms": distribution_summary(
            [row["latency_ms"] for row in rows]
        ),
        "cost_per_accepted_answer_usd": sum(row["actual_usd"] for row in rows)
        / len(accepted)
        if accepted
        else None,
        "unsupported_memory_answer_count": sum(
            bool(row["unsupported_claims"]) or not row["provenance_complete"]
            for row in rows
            if row["status"] == "completed" and row["policy"] != "M0"
        ),
        "failed_result_count": sum(
            row["receipt"]["status"] == "failed" for row in rows
        ),
    }


def _scores(
    rows: Sequence[Mapping[str, Any]], suite: str
) -> tuple[dict[str, float], dict[str, str]]:
    selected = [row for row in rows if row["suite"] == suite]
    scores = {row["task_id"]: float(row["is_correct"]) for row in selected}
    families = {row["task_id"]: row["task_family"] for row in selected}
    if len(selected) != len(scores):
        raise MemoryMetricsError(f"duplicate {suite} task outcome")
    return scores, families


def _reasoning_effort_effects(
    grouped: Mapping[tuple[str, str], Sequence[Mapping[str, Any]]],
    *,
    seed: int,
    resamples: int,
) -> dict[str, Any]:
    """Report the preregistered low/high main and policy-interaction effects."""

    by_suite: dict[str, Any] = {}
    for suite in ("temporal", "static"):
        low_by_policy: dict[str, dict[str, float]] = {}
        high_by_policy: dict[str, dict[str, float]] = {}
        families: dict[str, str] | None = None
        for policy in MEMORY_CONFIGURATIONS:
            low, low_families = _scores(grouped[(policy, "low")], suite)
            high, high_families = _scores(grouped[(policy, "high")], suite)
            if low_families != high_families or (
                families is not None and low_families != families
            ):
                raise MemoryMetricsError(
                    "reasoning-effort task family identity changed"
                )
            families = low_families
            low_by_policy[policy] = low
            high_by_policy[policy] = high
        assert families is not None
        task_ids = sorted(families)
        main_low = {
            task_id: _mean(
                [low_by_policy[policy][task_id] for policy in MEMORY_CONFIGURATIONS]
            )
            for task_id in task_ids
        }
        main_high = {
            task_id: _mean(
                [high_by_policy[policy][task_id] for policy in MEMORY_CONFIGURATIONS]
            )
            for task_id in task_ids
        }
        if any(value is None for value in (*main_low.values(), *main_high.values())):
            raise MemoryMetricsError("reasoning-effort main effect is empty")
        main_low_values = {key: float(value) for key, value in main_low.items()}
        main_high_values = {key: float(value) for key, value in main_high.items()}
        per_policy: dict[str, Any] = {}
        interactions: dict[str, Any] = {}
        baseline_differences = {
            task_id: high_by_policy["M0"][task_id] - low_by_policy["M0"][task_id]
            for task_id in task_ids
        }
        for policy in MEMORY_CONFIGURATIONS:
            per_policy[policy] = {
                "high_minus_low": paired_bootstrap_ci(
                    low_by_policy[policy],
                    high_by_policy[policy],
                    seed=seed,
                    resamples=resamples,
                ),
                "task_family_effects": task_family_effect_summaries(
                    low_by_policy[policy], high_by_policy[policy], families
                ),
            }
            policy_differences = {
                task_id: high_by_policy[policy][task_id]
                - low_by_policy[policy][task_id]
                for task_id in task_ids
            }
            interactions[policy] = {
                "difference_in_differences_vs_m0": paired_bootstrap_ci(
                    baseline_differences,
                    policy_differences,
                    seed=seed,
                    resamples=resamples,
                )
            }
        by_suite[suite] = {
            "main_high_minus_low": paired_bootstrap_ci(
                main_low_values,
                main_high_values,
                seed=seed,
                resamples=resamples,
            ),
            "main_effect_task_family_effects": task_family_effect_summaries(
                main_low_values, main_high_values, families
            ),
            "by_policy": per_policy,
            "policy_interactions_vs_m0": interactions,
        }
    return by_suite


def score_memory_outcomes(
    outcomes: Iterable[Mapping[str, Any]],
    *,
    frozen_manifest: Mapping[str, Any],
    trusted_frozen_manifest_sha256: str,
    bootstrap_seed: int | None = None,
    bootstrap_resamples: int | None = None,
) -> dict[str, Any]:
    """Score all five configurations and both efforts; harmful lanes remain reports."""

    validate_memory_experiment_manifest(
        frozen_manifest,
        trusted_frozen_manifest_sha256=trusted_frozen_manifest_sha256,
    )
    acceptance = frozen_manifest["acceptance_parameters"]
    frozen_seed = int(acceptance["paired_bootstrap_seed"])
    frozen_resamples = int(acceptance["paired_bootstrap_resamples"])
    if bootstrap_seed is not None and bootstrap_seed != frozen_seed:
        raise MemoryMetricsError("bootstrap seed differs from the frozen G3 protocol")
    if bootstrap_resamples is not None and bootstrap_resamples != frozen_resamples:
        raise MemoryMetricsError(
            "bootstrap resamples differ from the frozen G3 protocol"
        )
    rows = [
        _outcome(
            row,
            frozen_manifest,
            trusted_frozen_manifest_sha256,
        )
        for row in outcomes
    ]
    expected, expected_temporal, expected_static = _expected_tasks()
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["policy"], row["reasoning_effort"])].append(row)
    required = {
        (policy, effort)
        for policy in MEMORY_CONFIGURATIONS
        for effort in ALLOWED_REASONING_EFFORTS
    }
    if set(grouped) != required:
        raise MemoryMetricsError("G3 scorer requires all M0-M4 and low/high efforts")
    for key, group in grouped.items():
        ids = {row["task_id"] for row in group}
        if len(
            group
        ) != PUBLIC_TEMPORAL_TASK_COUNT + PUBLIC_STATIC_TASK_COUNT or ids != set(
            expected
        ):
            raise MemoryMetricsError(
                "G3 scorer requires full paired 28 temporal and 84 static coverage"
            )
        if any(
            expected[row["task_id"]]
            != {"suite": row["suite"], "task_family": row["task_family"]}
            for row in group
        ):
            raise MemoryMetricsError("G3 task family identity changed")
    summaries = {
        f"{policy}:{effort}": _summary(grouped[(policy, effort)])
        for policy, effort in sorted(required)
    }
    comparisons: dict[str, Any] = {}
    for effort in ALLOWED_REASONING_EFFORTS:
        baseline_temporal, temporal_families = _scores(
            grouped[("M0", effort)], "temporal"
        )
        baseline_static, static_families = _scores(grouped[("M0", effort)], "static")
        for policy in MEMORY_CONFIGURATIONS:
            candidate_temporal, candidate_temporal_families = _scores(
                grouped[(policy, effort)], "temporal"
            )
            candidate_static, candidate_static_families = _scores(
                grouped[(policy, effort)], "static"
            )
            if (
                candidate_temporal_families != temporal_families
                or candidate_static_families != static_families
            ):
                raise MemoryMetricsError("paired task family identity changed")
            temporal_ci = paired_bootstrap_ci(
                baseline_temporal,
                candidate_temporal,
                seed=frozen_seed,
                resamples=frozen_resamples,
            )
            static_ci = paired_bootstrap_ci(
                baseline_static,
                candidate_static,
                seed=frozen_seed,
                resamples=frozen_resamples,
            )
            comparisons[f"{policy}:{effort}"] = {
                "temporal_vs_m0": {
                    "paired_bootstrap_ci": temporal_ci,
                    "task_family_effects": task_family_effect_summaries(
                        baseline_temporal, candidate_temporal, temporal_families
                    ),
                },
                "static_regression_vs_m0": {
                    "paired_bootstrap_ci": static_ci,
                    "task_family_effects": task_family_effect_summaries(
                        baseline_static, candidate_static, static_families
                    ),
                },
                "report_status": "harmful"
                if temporal_ci["mean_delta"] < 0 or static_ci["mean_delta"] < 0
                else "reported",
            }
    acceptance_screen: dict[str, Any] = {}
    for policy, effort in sorted(required):
        key = f"{policy}:{effort}"
        summary = summaries[key]
        comparison = comparisons[key]
        temporal_delta = float(
            comparison["temporal_vs_m0"]["paired_bootstrap_ci"]["mean_delta"]
        )
        static_delta = float(
            comparison["static_regression_vs_m0"]["paired_bootstrap_ci"]["mean_delta"]
        )
        provenance_rate = summary["claim_level_provenance_rate"]
        temporal_improved = temporal_delta > 0.0
        static_within_floor = static_delta >= float(
            acceptance["static_accuracy_regression_floor"]
        )
        provenance_met = (
            isinstance(provenance_rate, (int, float))
            and not isinstance(provenance_rate, bool)
            and float(provenance_rate) >= float(acceptance["provenance_minimum"])
        )
        no_failed_results = summary["failed_result_count"] == 0
        acceptance_screen[key] = {
            "temporal_mean_delta_vs_m0": temporal_delta,
            "temporal_improved": temporal_improved,
            "static_accuracy_mean_delta_vs_m0": static_delta,
            "static_within_regression_floor": static_within_floor,
            "claim_level_provenance_rate": provenance_rate,
            "provenance_minimum_met": provenance_met,
            "no_failed_results": no_failed_results,
            "screen_scope": "public-only-not-a-g3-gate",
            "public_screen_eligible": policy != "M0"
            and temporal_improved
            and static_within_floor
            and provenance_met
            and no_failed_results,
        }
    payload: dict[str, Any] = {
        "schema_version": MEMORY_METRICS_SCHEMA,
        "acceptance_parameters": dict(acceptance),
        "acceptance_parameters_sha256": frozen_manifest["acceptance_parameters_sha256"],
        "bootstrap_seed": frozen_seed,
        "bootstrap_resamples": frozen_resamples,
        "policy_effort_metrics": summaries,
        "paired_comparisons": comparisons,
        "reasoning_effort_effects": _reasoning_effort_effects(
            grouped, seed=frozen_seed, resamples=frozen_resamples
        ),
        "acceptance_screen": acceptance_screen,
        "unsupported_memory_answers": build_unsupported_memory_answer_export(
            [row["receipt"] for row in rows],
            frozen_manifest=frozen_manifest,
            trusted_frozen_manifest_sha256=trusted_frozen_manifest_sha256,
        ),
    }
    payload["artifact_sha256"] = sha256_json(payload)
    return payload
