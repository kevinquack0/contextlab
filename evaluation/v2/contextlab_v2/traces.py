"""Read-only v1-to-v2 trace mock used to verify the planned viewer contract."""

from __future__ import annotations

import csv
import hashlib
import html
import json
from pathlib import Path
from typing import Any

from .baseline import repository_root
from .tasking import read_jsonl, sha256_json


UNAVAILABLE = "unavailable_in_v1"
TRACE_MOCK_SCHEMA = "contextlab.static-trace-mock.v1"
TRACE_FIELDS = frozenset(
    {
        "schema_version",
        "trace_id",
        "source_run_sha256",
        "planned_run_view",
        "candidate_view",
        "context_pack_view",
        "answer_view",
        "grading_view",
    }
)
RUN_VIEW_FIELDS = frozenset(
    {
        "run_id",
        "task_id",
        "strategy_id",
        "requested_model",
        "resolved_model",
        "provider_route",
        "reasoning_effort",
        "prompt_hash",
        "corpus_snapshot_id",
        "memory_snapshot_id",
    }
)
CANDIDATE_VIEW_FIELDS = frozenset(
    {
        "retrieval_method",
        "source_references",
        "candidate_scores",
        "candidate_content_hashes",
        "removal_reasons",
    }
)
CONTEXT_VIEW_FIELDS = frozenset(
    {
        "task_id",
        "strategy_id",
        "corpus_snapshot_id",
        "selected_source_references",
        "context_pack_hash",
        "selected_candidate_ids",
        "context_token_count",
        "input_tokens_including_prompt",
        "rendered_context_hash",
        "build_time_ms",
    }
)
ANSWER_VIEW_FIELDS = frozenset(
    {
        "answer",
        "answer_sha256",
        "output_tokens",
        "latency_ms",
        "estimated_cost_usd",
        "error",
    }
)
GRADING_VIEW_FIELDS = frozenset(
    {
        "status",
        "grader",
        "scores",
        "failure_mode",
        "reviewer_notes_status",
        "human_review",
    }
)
SCORE_FIELDS = frozenset(
    {
        "accuracy_score",
        "completeness_score",
        "citation_score",
        "usefulness_score",
        "risk_handling_score",
        "unsupported_claims_count",
    }
)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _grading_rows(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return {str(row["run_id"]): row for row in csv.DictReader(handle)}


def _candidate_view(row: dict[str, Any]) -> dict[str, Any]:
    trace = row.get("trace") if isinstance(row.get("trace"), dict) else {}
    scores = trace.get("retrieval_scores", UNAVAILABLE)
    return {
        "retrieval_method": trace.get("retrieval_method", UNAVAILABLE),
        "source_references": [str(value) for value in row.get("retrieved_sources", [])],
        "candidate_scores": scores,
        "candidate_content_hashes": UNAVAILABLE,
        "removal_reasons": UNAVAILABLE,
    }


def _grading_view(score: dict[str, str] | None) -> dict[str, Any]:
    if score is None:
        return {
            "status": UNAVAILABLE,
            "grader": UNAVAILABLE,
            "scores": UNAVAILABLE,
            "failure_mode": UNAVAILABLE,
            "reviewer_notes_status": "withheld_answer_key_derived",
            "human_review": UNAVAILABLE,
        }
    return {
        "status": "saved_v1_model_grade",
        "grader": score.get("grader") or UNAVAILABLE,
        "scores": {
            name: score.get(name) or UNAVAILABLE
            for name in (
                "accuracy_score",
                "completeness_score",
                "citation_score",
                "usefulness_score",
                "risk_handling_score",
                "unsupported_claims_count",
            )
        },
        "failure_mode": score.get("failure_mode") or UNAVAILABLE,
        "reviewer_notes_status": "withheld_answer_key_derived",
        "human_review": UNAVAILABLE,
    }


def validate_trace_record(trace: dict[str, Any]) -> None:
    """Reject extra view fields, including paraphrases copied from protected reviewer notes."""
    expected_views = {
        "planned_run_view": RUN_VIEW_FIELDS,
        "candidate_view": CANDIDATE_VIEW_FIELDS,
        "context_pack_view": CONTEXT_VIEW_FIELDS,
        "answer_view": ANSWER_VIEW_FIELDS,
        "grading_view": GRADING_VIEW_FIELDS,
    }
    if set(trace) != TRACE_FIELDS:
        raise ValueError("trace fields differ from the static mock allowlist")
    if trace.get("schema_version") != TRACE_MOCK_SCHEMA:
        raise ValueError("unsupported static trace schema")
    for view_name, allowed in expected_views.items():
        view = trace.get(view_name)
        if not isinstance(view, dict) or set(view) != allowed:
            raise ValueError(f"{view_name} fields differ from the static mock allowlist")
    grading = trace["grading_view"]
    scores = grading["scores"]
    if scores != UNAVAILABLE and (not isinstance(scores, dict) or set(scores) != SCORE_FIELDS):
        raise ValueError("grading score fields differ from the static mock allowlist")
    if grading["reviewer_notes_status"] != "withheld_answer_key_derived":
        raise ValueError("answer-key-derived reviewer notes must remain withheld")


def convert_v1_run(row: dict[str, Any], score: dict[str, str] | None) -> dict[str, Any]:
    answer = str(row.get("answer", ""))
    selected = [str(value) for value in row.get("retrieved_sources", [])]
    context_identity = {
        "task_id": row.get("question_id"),
        "strategy_id": row.get("strategy"),
        "corpus_snapshot_id": row.get("corpus_version"),
        "selected_source_references": selected,
    }
    converted = {
        "schema_version": TRACE_MOCK_SCHEMA,
        "trace_id": f"v1-replay-{row.get('run_id')}",
        "source_run_sha256": sha256_json(row),
        "planned_run_view": {
            "run_id": row.get("run_id"),
            "task_id": row.get("question_id"),
            "strategy_id": row.get("strategy"),
            "requested_model": row.get("model", UNAVAILABLE),
            "resolved_model": UNAVAILABLE,
            "provider_route": UNAVAILABLE,
            "reasoning_effort": UNAVAILABLE,
            "prompt_hash": row.get("prompt_version", UNAVAILABLE),
            "corpus_snapshot_id": row.get("corpus_version", UNAVAILABLE),
            "memory_snapshot_id": UNAVAILABLE,
        },
        "candidate_view": _candidate_view(row),
        "context_pack_view": {
            **context_identity,
            "context_pack_hash": sha256_json(context_identity),
            "selected_candidate_ids": UNAVAILABLE,
            "context_token_count": UNAVAILABLE,
            "input_tokens_including_prompt": row.get("input_tokens", UNAVAILABLE),
            "rendered_context_hash": UNAVAILABLE,
            "build_time_ms": UNAVAILABLE,
        },
        "answer_view": {
            "answer": answer,
            "answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
            "output_tokens": row.get("output_tokens", UNAVAILABLE),
            "latency_ms": row.get("latency_ms", UNAVAILABLE),
            "estimated_cost_usd": row.get("estimated_cost_usd", UNAVAILABLE),
            "error": row.get("error"),
        },
        "grading_view": _grading_view(score),
    }
    validate_trace_record(converted)
    return converted


def _render_view(title: str, value: Any) -> str:
    return (
        f"<section><h3>{html.escape(title)}</h3>"
        f"<pre>{html.escape(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))}</pre>"
        "</section>"
    )


def _render_html(payload: dict[str, Any]) -> str:
    sample = [
        row
        for row in payload["runs"]
        if row["planned_run_view"]["task_id"] == "Q001"
    ]
    cards: list[str] = []
    for run in sample:
        header = html.escape(
            f"{run['planned_run_view']['task_id']} · {run['planned_run_view']['strategy_id']}"
        )
        cards.append(
            "<article>"
            f"<h2>{header}</h2>"
            + _render_view("Planned run", run["planned_run_view"])
            + _render_view("Candidates", run["candidate_view"])
            + _render_view("Context pack", run["context_pack_view"])
            + _render_view("Answer", run["answer_view"])
            + _render_view("Grading", run["grading_view"])
            + "</article>"
        )
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>ContextLab v1 static trace mock</title>
<style>
body{font:15px/1.45 system-ui,sans-serif;max-width:1100px;margin:32px auto;padding:0 20px;background:#f5f4ef;color:#191919}
article{background:white;border:1px solid #d7d4ca;border-radius:10px;padding:22px;margin:24px 0}section{margin:18px 0}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f0efe9;padding:14px;border-radius:6px}h1,h2,h3{line-height:1.15}
</style></head><body>
<h1>ContextLab v1 static trace mock</h1>
<p>This read-only report converts all saved v1 runs to the planned v2 views. It shows Q001 as the visual sample. Missing v1 fields are marked <code>unavailable_in_v1</code>.</p>
""" + "".join(cards) + "</body></html>\n"


def build_static_trace_mock(root: Path | None = None) -> dict[str, Any]:
    root = (root or repository_root()).resolve()
    run_path = root / "results" / "final" / "main_gemini.jsonl"
    score_path = root / "results" / "final" / "scored_results.csv"
    scores = _grading_rows(score_path)
    runs = [convert_v1_run(row, scores.get(str(row.get("run_id")))) for row in read_jsonl(run_path)]
    if len(runs) != 160:
        raise ValueError(f"static trace mock expects 160 v1 runs, found {len(runs)}")
    for run in runs:
        validate_trace_record(run)
    payload = {
        "schema_version": "contextlab.static-trace-report.v1",
        "read_only": True,
        "source_runs": str(run_path.relative_to(root)),
        "source_runs_sha256": _sha256_file(run_path),
        "source_grades": str(score_path.relative_to(root)),
        "source_grades_sha256": _sha256_file(score_path),
        "run_count": len(runs),
        "missing_field_marker": UNAVAILABLE,
        "runs": runs,
    }
    output_dir = root / "results" / "v2" / "traces"
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "v1_static_trace_mock.json"
    html_path = output_dir / "v1_static_trace_mock.html"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    html_path.write_text(_render_html(payload), encoding="utf-8")
    return {
        "run_count": len(runs),
        "json": str(json_path.relative_to(root)),
        "html": str(html_path.relative_to(root)),
        "json_sha256": _sha256_file(json_path),
        "html_sha256": _sha256_file(html_path),
    }
