"""Deterministic statistical summaries for paired ContextLab evaluations."""

from __future__ import annotations

import math
import random
import statistics as stdlib_statistics
from collections.abc import Iterable, Mapping, Sequence
from numbers import Real


BOOTSTRAP_CONFIDENCE_LEVEL = 0.95
DEFAULT_BOOTSTRAP_RESAMPLES = 10_000
MINIMUM_REPEAT_TRIALS = 5


class StatisticsError(ValueError):
    """Raised when statistical input is invalid or cannot yield finite output."""


def _finite_float(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise StatisticsError(f"{label} must be a real number")
    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise StatisticsError(f"{label} must be a finite real number") from exc
    if not math.isfinite(numeric):
        raise StatisticsError(f"{label} must be a finite real number")
    return numeric


def _finite_result(value: object, *, label: str) -> float:
    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise StatisticsError(
            f"{label} is not representable as a finite float"
        ) from exc
    if not math.isfinite(numeric):
        raise StatisticsError(f"{label} is not representable as a finite float")
    return numeric


def _mean(values: Sequence[float], *, label: str) -> float:
    try:
        value = stdlib_statistics.mean(values)
    except (OverflowError, stdlib_statistics.StatisticsError) as exc:
        raise StatisticsError(
            f"{label} is not representable as a finite float"
        ) from exc
    return _finite_result(value, label=label)


def _sample_stddev(values: Sequence[float], *, label: str) -> float:
    if len(values) == 1:
        return 0.0
    try:
        value = stdlib_statistics.stdev(values)
    except (OverflowError, stdlib_statistics.StatisticsError) as exc:
        raise StatisticsError(
            f"{label} is not representable as a finite float"
        ) from exc
    return _finite_result(value, label=label)


def _linear_percentile(sorted_values: Sequence[float], probability: float) -> float:
    """Return an empirical percentile with linear interpolation (R type 7)."""

    position = (len(sorted_values) - 1) * probability
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return sorted_values[lower_index]
    upper_weight = position - lower_index
    value = math.fsum(
        (
            sorted_values[lower_index] * (1.0 - upper_weight),
            sorted_values[upper_index] * upper_weight,
        )
    )
    return _finite_result(value, label=f"p{probability * 100:g}")


def _score_mapping(scores: Mapping[str, Real], *, label: str) -> dict[str, float]:
    if not isinstance(scores, Mapping):
        raise StatisticsError(f"{label} scores must be a task-ID mapping")
    validated: dict[str, float] = {}
    for task_id, score in scores.items():
        if not isinstance(task_id, str) or not task_id:
            raise StatisticsError(f"{label} task IDs must be non-empty strings")
        validated[task_id] = _finite_float(score, label=f"{label}[{task_id!r}]")
    if not validated:
        raise StatisticsError(f"{label} scores must not be empty")
    return validated


def _paired_scores(
    baseline_scores: Mapping[str, Real], candidate_scores: Mapping[str, Real]
) -> tuple[dict[str, float], dict[str, float], list[str]]:
    baseline = _score_mapping(baseline_scores, label="baseline")
    candidate = _score_mapping(candidate_scores, label="candidate")
    baseline_ids = set(baseline)
    candidate_ids = set(candidate)
    if baseline_ids != candidate_ids:
        missing_from_candidate = sorted(baseline_ids - candidate_ids)
        missing_from_baseline = sorted(candidate_ids - baseline_ids)
        raise StatisticsError(
            "paired task IDs do not match: "
            f"missing from candidate={missing_from_candidate}, "
            f"missing from baseline={missing_from_baseline}"
        )
    return baseline, candidate, sorted(baseline_ids)


def _paired_deltas(
    baseline_scores: Mapping[str, Real], candidate_scores: Mapping[str, Real]
) -> tuple[dict[str, float], dict[str, float], list[str], list[float]]:
    baseline, candidate, task_ids = _paired_scores(baseline_scores, candidate_scores)
    deltas = [
        _finite_result(
            candidate[task_id] - baseline[task_id],
            label=f"paired delta for task {task_id!r}",
        )
        for task_id in task_ids
    ]
    return baseline, candidate, task_ids, deltas


def paired_bootstrap_ci(
    baseline_scores: Mapping[str, Real],
    candidate_scores: Mapping[str, Real],
    *,
    seed: int,
    resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
) -> dict[str, int | float]:
    """Return a deterministic paired percentile-bootstrap 95% confidence interval.

    Each resample draws task-level candidate-minus-baseline differences with
    replacement. ``seed`` is required so every saved result records an explicit
    source of pseudo-randomness.
    """

    if isinstance(seed, bool) or not isinstance(seed, int):
        raise StatisticsError("seed must be an integer")
    if isinstance(resamples, bool) or not isinstance(resamples, int) or resamples < 1:
        raise StatisticsError("resamples must be a positive integer")

    _baseline, _candidate, task_ids, deltas = _paired_deltas(
        baseline_scores, candidate_scores
    )
    task_count = len(task_ids)
    rng = random.Random(seed)
    bootstrap_means: list[float] = []
    for index in range(resamples):
        sample = [deltas[rng.randrange(task_count)] for _ in range(task_count)]
        bootstrap_means.append(_mean(sample, label=f"bootstrap mean {index}"))
    bootstrap_means.sort()

    return {
        "n": task_count,
        "mean_delta": _mean(deltas, label="mean paired delta"),
        "confidence_level": BOOTSTRAP_CONFIDENCE_LEVEL,
        "ci_lower": _linear_percentile(bootstrap_means, 0.025),
        "ci_upper": _linear_percentile(bootstrap_means, 0.975),
        "seed": seed,
        "resamples": resamples,
    }


def task_family_effect_summaries(
    baseline_scores: Mapping[str, Real],
    candidate_scores: Mapping[str, Real],
    task_families: Mapping[str, str],
) -> dict[str, dict[str, int | float | None]]:
    """Summarize paired scores and Cohen's paired ``d_z`` by task family.

    The standardized effect is ``mean(delta) / sample_stddev(delta)``. It is
    ``None`` when fewer than two pairs exist or a nonzero constant delta has zero
    variance. An all-zero delta has effect ``0.0``.
    """

    baseline, candidate, task_ids, deltas = _paired_deltas(
        baseline_scores, candidate_scores
    )
    delta_by_task = dict(zip(task_ids, deltas, strict=True))
    if not isinstance(task_families, Mapping):
        raise StatisticsError("task families must be a task-ID mapping")
    family_ids = set(task_families)
    task_id_set = set(task_ids)
    if family_ids != task_id_set:
        missing_families = sorted(task_id_set - family_ids)
        unknown_tasks = sorted(family_ids - task_id_set, key=str)
        raise StatisticsError(
            "task-family IDs do not match paired task IDs: "
            f"missing families={missing_families}, unknown tasks={unknown_tasks}"
        )

    grouped_ids: dict[str, list[str]] = {}
    for task_id in task_ids:
        family = task_families[task_id]
        if not isinstance(family, str) or not family:
            raise StatisticsError(
                f"family for task {task_id!r} must be a non-empty string"
            )
        grouped_ids.setdefault(family, []).append(task_id)

    summaries: dict[str, dict[str, int | float | None]] = {}
    for family in sorted(grouped_ids):
        family_task_ids = grouped_ids[family]
        baseline_values = [baseline[task_id] for task_id in family_task_ids]
        candidate_values = [candidate[task_id] for task_id in family_task_ids]
        family_deltas = [delta_by_task[task_id] for task_id in family_task_ids]
        mean_delta = _mean(
            family_deltas, label=f"mean paired delta for family {family!r}"
        )
        if len(family_deltas) < 2:
            standardized_effect: float | None = None
        else:
            delta_stddev = _sample_stddev(
                family_deltas,
                label=f"paired-delta standard deviation for family {family!r}",
            )
            if delta_stddev == 0.0:
                standardized_effect = 0.0 if mean_delta == 0.0 else None
            else:
                standardized_effect = _finite_result(
                    mean_delta / delta_stddev,
                    label=f"paired standardized effect for family {family!r}",
                )
        summaries[family] = {
            "n": len(family_task_ids),
            "baseline_mean": _mean(
                baseline_values, label=f"baseline mean for family {family!r}"
            ),
            "candidate_mean": _mean(
                candidate_values, label=f"candidate mean for family {family!r}"
            ),
            "mean_delta": mean_delta,
            "paired_standardized_effect": standardized_effect,
        }
    return summaries


def distribution_summary(values: Iterable[Real]) -> dict[str, int | float]:
    """Return finite descriptive statistics, including empirical p50 and p95."""

    try:
        iterator = iter(values)
    except TypeError as exc:
        raise StatisticsError("values must be an iterable of real numbers") from exc
    validated = [
        _finite_float(value, label=f"values[{index}]")
        for index, value in enumerate(iterator)
    ]
    if not validated:
        raise StatisticsError("values must not be empty")
    sorted_values = sorted(validated)
    p50 = _linear_percentile(sorted_values, 0.50)
    return {
        "n": len(validated),
        "mean": _mean(validated, label="mean"),
        "sample_stddev": _sample_stddev(validated, label="sample standard deviation"),
        "min": sorted_values[0],
        "median": p50,
        "p50": p50,
        "p95": _linear_percentile(sorted_values, 0.95),
        "max": sorted_values[-1],
    }


def repeat_summary(
    trial_values: Iterable[Real],
) -> dict[str, int | float | list[float]]:
    """Summarize at least five repeats while preserving trial order and values."""

    try:
        iterator = iter(trial_values)
    except TypeError as exc:
        raise StatisticsError(
            "trial values must be an iterable of real numbers"
        ) from exc
    values = [
        _finite_float(value, label=f"trial_values[{index}]")
        for index, value in enumerate(iterator)
    ]
    if len(values) < MINIMUM_REPEAT_TRIALS:
        raise StatisticsError(
            f"repeat summaries require at least {MINIMUM_REPEAT_TRIALS} trial values"
        )
    distribution = distribution_summary(values)
    distribution.pop("n")
    return {
        "trial_count": len(values),
        "trial_values": values,
        **distribution,
    }
