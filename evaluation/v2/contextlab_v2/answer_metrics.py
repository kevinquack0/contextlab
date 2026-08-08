"""Deterministic, explicitly provisional metrics for G2 generated answers."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from .baseline import repository_root
from .experiments import load_protocol
from .generations import (
    GenerationBatchError,
    load_public_generation_results,
    validate_generation_manifest_envelope,
)
from .static_benchmark import load_public_gold, public_static_tasks
from .statistics import distribution_summary
from .tasking import sha256_json


ANSWER_METRICS_SCHEMA = "contextlab.g2-answer-metrics.v1"
_CITATION = re.compile(r"\[(NL-\d{3})(?:#(NL-\d{3}-S\d{2}))?\]")
_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[-_/.][A-Za-z0-9]+)*")
_CRITICAL = re.compile(
    r"(?<![A-Za-z])(?:\$?\d+(?:[.,]\d+)*(?:%|\s*(?:days?|weeks?|months?|hours?|seconds?))?|[A-Z]{2,}(?:-[A-Z0-9]+)+)(?![A-Za-z])"
)
_ABSTENTION = re.compile(
    r"\b(?:insufficient evidence|cannot (?:be )?determine|not (?:reported|provided|available|stated)|does not (?:report|provide|state)|no evidence)\b",
    re.IGNORECASE,
)
_STOPWORDS = frozenset(
    "a an and are as at be by for from has have in is it of on or that the their this to was were will with".split()
)


class AnswerMetricError(ValueError):
    """Saved answer and trace evidence cannot be paired safely."""


def _tokens(text: str) -> set[str]:
    return {
        token.lower()
        for token in _TOKEN.findall(text)
        if len(token) > 2 and token.lower() not in _STOPWORDS
    }


def _critical_values(text: str) -> set[str]:
    return {
        re.sub(r"\s+", " ", value.lower()).strip() for value in _CRITICAL.findall(text)
    }


def _citation_refs(text: str) -> list[str]:
    return [section or source for source, section in _CITATION.findall(text)]


def _ref_matches(candidate: str, gold: str) -> bool:
    if gold == candidate:
        return True
    if "-S" not in gold and candidate.startswith(f"{gold}-S"):
        return True
    return False


def score_generated_answer(
    answer: str,
    expected_answer: str,
    required_evidence: Iterable[str],
    context_references: Iterable[str],
    *,
    abstention_task: bool,
) -> dict[str, Any]:
    """Score observable answer properties; this is not an AI or human correctness grade."""
    required = tuple(map(str, required_evidence))
    contexts = set(map(str, context_references))
    citations = _citation_refs(answer)
    expected_tokens = _tokens(expected_answer)
    answer_tokens = _tokens(answer)
    critical = _critical_values(expected_answer)
    answer_lower = answer.lower()
    critical_hits = {value for value in critical if value in answer_lower}
    valid_citations = [
        citation
        for citation in citations
        if any(
            _ref_matches(citation, context) or _ref_matches(context, citation)
            for context in contexts
        )
    ]
    covered_required = {
        gold
        for gold in required
        if any(_ref_matches(citation, gold) for citation in citations)
    }
    abstained = _ABSTENTION.search(answer) is not None
    content_recall = (
        len(expected_tokens & answer_tokens) / len(expected_tokens)
        if expected_tokens
        else 0.0
    )
    critical_recall = len(critical_hits) / len(critical) if critical else 1.0
    citation_precision = len(valid_citations) / len(citations) if citations else 0.0
    evidence_citation_recall = (
        len(covered_required) / len(required) if required else 0.0
    )
    accepted_proxy = (
        abstained
        if abstention_task
        else (
            not abstained
            and critical_recall == 1.0
            and content_recall >= 0.35
            and citation_precision == 1.0
            and evidence_citation_recall > 0.0
        )
    )
    return {
        "expected_content_token_recall": content_recall,
        "critical_value_recall": critical_recall,
        "citation_count": len(citations),
        "citation_precision": citation_precision,
        "required_evidence_citation_recall": evidence_citation_recall,
        "unsupported_citation_count": len(citations) - len(valid_citations),
        "abstained": abstained,
        "abstention_quality": (
            "correct"
            if abstention_task and abstained
            else "incorrect"
            if abstention_task
            else "not_applicable"
        ),
        "accepted_proxy": accepted_proxy,
        "proxy_notice": "deterministic screening only; not a correctness grade",
    }


def _context_references(trace: Mapping[str, Any]) -> list[str]:
    return [
        str(row["section_id"] or row["source_id"])
        for row in trace["selected_candidates"]
    ]


def build_public_answer_metrics(
    generation_manifest: Mapping[str, Any],
    lab: Mapping[str, Any],
    root: Path | None = None,
) -> dict[str, Any]:
    root = (root or repository_root()).resolve()
    protocol = load_protocol(root)
    try:
        validate_generation_manifest_envelope(
            generation_manifest, lab, protocol, expected_trial=1
        )
    except GenerationBatchError as exc:
        raise AnswerMetricError(
            f"generation scoring envelope is invalid: {exc}"
        ) from exc
    results = load_public_generation_results(generation_manifest, root)
    traces = {str(row["run_id"]): row for row in lab["traces"]}
    gold = {str(row["task_id"]): row for row in load_public_gold(root)}
    tasks = {str(row["task_id"]): row for row in public_static_tasks(root)}
    cells_by_run = {
        str(row["run_id"]): row
        for row in generation_manifest["cells"]
        if row["status"] == "completed"
    }
    rows: list[dict[str, Any]] = []
    for result in results:
        run_id = str(result["run_id"])
        cell = cells_by_run[run_id]
        trace = traces[str(cell["trace_run_id"])]
        task_id = str(cell["task_id"])
        screen = score_generated_answer(
            str(result["answer"]),
            str(gold[task_id]["expected_answer"]),
            gold[task_id]["required_evidence"],
            _context_references(trace),
            abstention_task=tasks[task_id]["task_family"] == "abstention",
        )
        rows.append(
            {
                "run_id": run_id,
                "task_id": task_id,
                "task_family": tasks[task_id]["task_family"],
                "strategy_id": cell["strategy_id"],
                "reasoning_effort": cell["reasoning_effort"],
                "answer_sha256": hashlib.sha256(
                    str(result["answer"]).encode("utf-8")
                ).hexdigest(),
                "metrics": screen,
                "actual_usd": cell["actual_usd"],
                "latency_ms": cell["latency_ms"],
            }
        )
    by_cell: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_cell[(str(row["strategy_id"]), str(row["reasoning_effort"]))].append(row)
    aggregate: dict[str, Any] = {}
    for (strategy, effort), values in sorted(by_cell.items()):
        metric_names = (
            "expected_content_token_recall",
            "critical_value_recall",
            "citation_precision",
            "required_evidence_citation_recall",
        )
        aggregate[f"{strategy}:{effort}"] = {
            "n": len(values),
            "means": {
                metric: sum(float(row["metrics"][metric]) for row in values)
                / len(values)
                for metric in metric_names
            },
            "accepted_proxy_rate": sum(
                bool(row["metrics"]["accepted_proxy"]) for row in values
            )
            / len(values),
            "unsupported_citations": sum(
                int(row["metrics"]["unsupported_citation_count"]) for row in values
            ),
            "latency_ms": distribution_summary(
                float(row["latency_ms"])
                for row in values
                if isinstance(row["latency_ms"], (int, float))
                and not isinstance(row["latency_ms"], bool)
                and math.isfinite(float(row["latency_ms"]))
            ),
            "actual_usd": str(
                sum((Decimal(str(row["actual_usd"])) for row in values), Decimal("0"))
            ),
        }
    payload: dict[str, Any] = {
        "schema_version": ANSWER_METRICS_SCHEMA,
        "scope": "public_deterministic_screening",
        "generation_campaign_id": generation_manifest["generation_campaign_id"],
        "generation_manifest_sha256": generation_manifest["manifest_sha256"],
        "generation_protocol_sha256": generation_manifest["generation_protocol_sha256"],
        "output_token_limit": generation_manifest["output_token_limit"],
        "component_lab_sha256": lab["artifact_sha256"],
        "completed_cell_count": len(rows),
        "aggregate": aggregate,
        "rows": rows,
        "limitations": [
            "Expected-token overlap is not semantic correctness.",
            "Citation presence does not prove that a passage entails a claim.",
            "Final claims require the frozen three-member review panel, including Kevin.",
        ],
    }
    payload["artifact_sha256"] = sha256_json(payload)
    return payload


def write_public_answer_metrics(
    generation_manifest: Mapping[str, Any],
    lab: Mapping[str, Any],
    root: Path | None = None,
    output: Path | None = None,
) -> dict[str, Any]:
    root = (root or repository_root()).resolve()
    payload = build_public_answer_metrics(generation_manifest, lab, root)
    destination = output or root / "results/v2/reports/g2_public_answer_metrics.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return payload
