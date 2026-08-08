"""Bounded public F5 search demonstration over the saved R5/R6 ceiling cohort."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from functools import partial
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any

from .baseline import repository_root
from .costs import CostLedger, canonical_ledger_path
from .experiments import FROZEN_RAW_CHUNKS_SHA256, load_frozen_chunks
from .frontier import (
    FrontierError,
    load_approved_frontier_entry_gate,
    require_frontier_experiment_approved,
)
from .gateway import post_json_with_timeout, run_paid_generation_to_file
from .immutable_io import ImmutableIOError, write_bytes_once_or_verify
from .provider import ALLOWED_REASONING_EFFORTS, FRONTIER_PROVIDER_SLUG
from .retrieval import bm25_retrieve, estimate_tokens, render_evidence_block
from .tasking import prompt_safe_task, sha256_json


F5_RESULT_SCHEMA = "contextlab.f5-bounded-search-result.v1"
F5_RESULT_PATH = Path("results/v2/frontier/f5/bounded_search.final.json")
F5_CEILING_PATH = Path("results/v2/frontier/f5/retrieval_ceiling.json")
F5_CEILING_EVIDENCE_PATH = Path(
    "results/v2/frontier/f5/retrieval_ceiling_evidence.json"
)
F5_COMPONENT_LAB_PATH = Path("results/v2/retrieval/public_component_lab.json")
F5_ANSWER_METRICS_PATH = Path("results/v2/reports/g2_public_answer_metrics.json")
F5_TASKS_PATH = Path("evaluation/v2/tasks/v1_annotated.jsonl")
F5_PROVIDER_ROOT = Path("results/v2/frontier/f5/provider")

F5_TASK_IDS = ("S025", "S026")
F5_FIXED_METHODS = ("R5", "R6")
F5_TRIAL_IDS = ("f5-trial-01", "f5-trial-02")
F5_APPROVED_TOOLS = ("search_public_chunks", "read_public_source")
F5_MAX_TOOL_CALLS = 4
F5_MAX_TOTAL_TOKENS = 8_000
F5_MAX_SECONDS = 60.0
F5_MAX_TURNS = F5_MAX_TOOL_CALLS + 1
F5_MAX_COMPLETION_TOKENS = 3_000
F5_SEARCH_LIMIT = 5
F5_SOURCE_TOKEN_LIMIT = 2_500
F5_PROMPT_VERSION = "contextlab.f5-bounded-search-prompt.v1"

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_ID = re.compile(r"NL-[0-9]{3}\Z")
_FORBIDDEN_PATH_TOKENS = (
    "sealed",
    "protected",
    "evaluation_only",
    "canonical_fact_ledger",
    "gold",
)
_SYSTEM_INSTRUCTION = f"""You are the bounded public retrieval agent for ContextLab F5.
Use only the supplied public tool results. Return exactly one JSON object, with no markdown.
Allowed actions:
{{"action":"search_public_chunks","query":"short public-corpus query"}}
{{"action":"read_public_source","source_id":"NL-000"}}
{{"action":"answer","answer":"grounded answer with [NL-000#NL-000-S00] citations"}}
The prompt contract is {F5_PROMPT_VERSION}. Do not name or request any other tool."""


class F5Error(ValueError):
    """The bounded F5 entry, execution, import, or replay contract failed."""


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _public_file(root: Path, relative: Path, label: str) -> Path:
    value = relative.as_posix()
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or any(token in value.casefold() for token in _FORBIDDEN_PATH_TOKENS)
    ):
        raise F5Error(f"{label} is not public-only")
    path = root / relative
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise F5Error(f"{label} path is unsafe")
    if not path.is_file():
        raise F5Error(f"{label} is missing")
    return path


def _read_public_json(root: Path, relative: Path, label: str) -> dict[str, Any]:
    path = _public_file(root, relative, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise F5Error(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise F5Error(f"{label} must be an object")
    return value


def _load_tasks(root: Path) -> dict[str, dict[str, Any]]:
    path = _public_file(root, F5_TASKS_PATH, "F5 public tasks")
    tasks: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if value.get("task_id") in F5_TASK_IDS:
            if value.get("task_family") != "procedural_guidance":
                raise F5Error("F5 cohort task family changed")
            tasks[str(value["task_id"])] = value
    if tuple(sorted(tasks)) != F5_TASK_IDS:
        raise F5Error("F5 public two-task cohort is incomplete")
    return tasks


def _approved_gate(root: Path) -> Mapping[str, Any]:
    try:
        gate = load_approved_frontier_entry_gate(root)
        require_frontier_experiment_approved(gate, "F5")
    except (FrontierError, OSError, json.JSONDecodeError) as exc:
        raise F5Error("approved F5 frontier entry is required") from exc
    return gate


def _validate_ceiling(root: Path) -> dict[str, Any]:
    ceiling = _read_public_json(root, F5_CEILING_PATH, "F5 ceiling")
    expected = {
        "schema_version",
        "task_family",
        "metric",
        "fixed_methods",
        "sample_count",
        "measured_ceiling",
        "source_report_path",
        "source_report_sha256",
        "artifact_sha256",
    }
    if (
        set(ceiling) != expected
        or ceiling.get("schema_version") != "contextlab.f5-retrieval-ceiling.v1"
        or ceiling.get("task_family") != "procedural_guidance"
        or ceiling.get("metric") != "required_source_coverage"
        or ceiling.get("fixed_methods") != list(F5_FIXED_METHODS)
        or ceiling.get("sample_count") != len(F5_TASK_IDS)
        or ceiling.get("measured_ceiling") is not True
        or ceiling.get("source_report_path") != F5_CEILING_EVIDENCE_PATH.as_posix()
        or ceiling.get("artifact_sha256")
        != sha256_json({k: v for k, v in ceiling.items() if k != "artifact_sha256"})
    ):
        raise F5Error("F5 ceiling contract changed")
    evidence_path = _public_file(root, F5_CEILING_EVIDENCE_PATH, "F5 ceiling evidence")
    if ceiling.get("source_report_sha256") != _sha_file(evidence_path):
        raise F5Error("F5 ceiling evidence bytes changed")
    evidence = _read_public_json(root, F5_CEILING_EVIDENCE_PATH, "F5 ceiling evidence")
    if evidence.get("artifact_sha256") != sha256_json(
        {k: v for k, v in evidence.items() if k != "artifact_sha256"}
    ) or [row.get("task_id") for row in evidence.get("tasks", [])] != list(F5_TASK_IDS):
        raise F5Error("F5 ceiling evidence is invalid")
    for reference in evidence.get("source_artifacts", []):
        if not isinstance(reference, Mapping):
            raise F5Error("F5 ceiling source reference is invalid")
        source = _public_file(
            root, Path(str(reference.get("path"))), "F5 ceiling source"
        )
        if reference.get("sha256") != _sha_file(source):
            raise F5Error("F5 ceiling source bytes changed")
    return ceiling


def _baseline_cells(
    metrics: Mapping[str, Any], tasks: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    rows = metrics.get("rows")
    if not isinstance(rows, list):
        raise F5Error("F5 saved answer metrics have no rows")
    by_key = {
        (row.get("task_id"), row.get("strategy_id"), row.get("reasoning_effort")): row
        for row in rows
        if isinstance(row, Mapping)
        and row.get("task_id") in F5_TASK_IDS
        and row.get("strategy_id") in F5_FIXED_METHODS
    }
    cells: list[dict[str, Any]] = []
    for task_id in F5_TASK_IDS:
        required = list(tasks[task_id]["required_evidence"])
        for method in F5_FIXED_METHODS:
            for effort in ALLOWED_REASONING_EFFORTS:
                row = by_key.get((task_id, method, effort))
                if not isinstance(row, Mapping) or not isinstance(
                    row.get("metrics"), Mapping
                ):
                    raise F5Error("F5 saved fixed-retrieval cell is missing")
                measured = row["metrics"]
                cell = {
                    "task_id": task_id,
                    "strategy_id": method,
                    "reasoning_effort": effort,
                    # Legacy F5 v1 transport name. This is the saved G2
                    # accepted_proxy, not a semantic task-correctness grade.
                    "task_success": bool(measured.get("accepted_proxy")),
                    "evidence_coverage": float(
                        measured.get("required_evidence_citation_recall")
                    ),
                    "required_source_count": len(required),
                    "tool_calls": 0,
                    "dead_ends": 0,
                    "latency_ms": row.get("latency_ms"),
                    "cost_usd": row.get("actual_usd"),
                    "verifier_failures": (
                        []
                        if bool(measured.get("accepted_proxy"))
                        else ["saved_public_proxy_not_accepted"]
                    ),
                    "source_run_id": row.get("run_id"),
                }
                cell["record_sha256"] = sha256_json(cell)
                cells.append(cell)
    return cells


def _trace_summary(lab: Mapping[str, Any], task_id: str) -> str:
    traces = lab.get("traces")
    if not isinstance(traces, list):
        raise F5Error("F5 component lab has no public traces")
    lines: list[str] = []
    for method in F5_FIXED_METHODS:
        matches = [
            row
            for row in traces
            if isinstance(row, Mapping)
            and row.get("strategy_id") == method
            and isinstance(row.get("task"), Mapping)
            and row["task"].get("task_id") == task_id
        ]
        if len(matches) != 1:
            raise F5Error("F5 fixed public trace is missing")
        refs = [
            str(candidate.get("text_reference"))
            for candidate in matches[0].get("selected_candidates", [])
            if isinstance(candidate, Mapping)
        ]
        lines.append(f"{method} fixed selected refs: {', '.join(refs)}")
    return "\n".join(lines)


def _candidate_chunk(
    chunks: Sequence[Mapping[str, Any]], candidate: Mapping[str, Any]
) -> Mapping[str, Any]:
    matches = [
        chunk
        for chunk in chunks
        if chunk.get("source_id") == candidate.get("source_id")
        and chunk.get("section_id") == candidate.get("section_id")
    ]
    if len(matches) != 1:
        raise F5Error("F5 retrieval candidate does not map to one frozen chunk")
    return matches[0]


def _tool_search(chunks: Sequence[Mapping[str, Any]], query: str) -> dict[str, Any]:
    candidates = bm25_retrieve(query, chunks, limit=F5_SEARCH_LIMIT, stage="f5_search")
    return {
        "tool": "search_public_chunks",
        "query": query,
        "results": [
            {
                "reference": candidate["text_reference"],
                "content_sha256": candidate["content_hash"],
                "content": render_evidence_block(_candidate_chunk(chunks, candidate)),
            }
            for candidate in candidates
        ],
    }


def _tool_read_source(
    chunks: Sequence[Mapping[str, Any]], source_id: str
) -> dict[str, Any]:
    selected: list[dict[str, str]] = []
    used = 0
    for chunk in chunks:
        if chunk.get("source_id") != source_id:
            continue
        content = render_evidence_block(chunk)
        tokens = estimate_tokens(content)
        if used + tokens > F5_SOURCE_TOKEN_LIMIT:
            break
        selected.append(
            {
                "reference": f"{chunk['source_id']}#{chunk['section_id']}",
                "content_sha256": hashlib.sha256(
                    str(chunk["text"]).encode("utf-8")
                ).hexdigest(),
                "content": content,
            }
        )
        used += tokens
    return {
        "tool": "read_public_source",
        "source_id": source_id,
        "results": selected,
    }


def _parse_action(answer: str) -> dict[str, str]:
    try:
        value = json.loads(answer)
    except json.JSONDecodeError as exc:
        raise F5Error("F5 agent response is not exact JSON") from exc
    if not isinstance(value, dict) or value.get("action") not in {
        *F5_APPROVED_TOOLS,
        "answer",
    }:
        raise F5Error("F5 agent selected an unapproved action")
    action = str(value["action"])
    expected = {
        "search_public_chunks": {"action", "query"},
        "read_public_source": {"action", "source_id"},
        "answer": {"action", "answer"},
    }[action]
    if set(value) != expected:
        raise F5Error("F5 agent action fields changed")
    field = next(iter(expected - {"action"}))
    argument = value.get(field)
    if not isinstance(argument, str) or not argument.strip() or len(argument) > 2_000:
        raise F5Error("F5 agent action argument is invalid")
    if action == "read_public_source" and _SOURCE_ID.fullmatch(argument) is None:
        raise F5Error("F5 agent source ID is invalid")
    return {"action": action, field: argument}


def _provider_receipt(
    path: Path, root: Path, result: Mapping[str, Any]
) -> dict[str, Any]:
    metadata = result["metadata"]
    relative = path.resolve().relative_to(root).as_posix()
    return {
        "path": relative,
        "sha256": _sha_file(path),
        "request_id": metadata.get("request_id"),
        "provider": metadata.get("provider"),
        "prompt_tokens": metadata.get("native_prompt_tokens") or 0,
        "completion_tokens": metadata.get("native_completion_tokens") or 0,
        "reasoning_tokens": metadata.get("native_reasoning_tokens") or 0,
        "cost_usd": metadata.get("actual_usd"),
        "latency_ms": metadata.get("latency_ms"),
    }


def _search_cell(
    root: Path,
    *,
    task: Mapping[str, Any],
    trace_summary: str,
    chunks: Sequence[Mapping[str, Any]],
    effort: str,
    trial_id: str,
    generation_runner: Callable[..., Mapping[str, Any]] | None,
    clock: Callable[[], float],
) -> dict[str, Any]:
    prompt_task = prompt_safe_task(dict(task))
    transcript: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    tool_calls = 0
    dead_ends = 0
    total_tokens = 0
    total_cost = Decimal("0")
    total_latency = 0.0
    answer: str | None = None
    failures: list[str] = []
    started = clock()
    ledger = CostLedger(canonical_ledger_path(root))

    for turn in range(1, F5_MAX_TURNS + 1):
        elapsed = clock() - started
        if elapsed > F5_MAX_SECONDS:
            failures.append("wall_time_limit_exceeded")
            break
        rendered = (
            f"Fixed retrieval evidence (navigation only):\n{trace_summary}\n\n"
            "Public tool transcript:\n"
            + json.dumps(transcript, ensure_ascii=False, sort_keys=True)
        )
        estimated = estimate_tokens(
            rendered + _SYSTEM_INSTRUCTION + task["question_text"]
        )
        if total_tokens + estimated + F5_MAX_COMPLETION_TOKENS > F5_MAX_TOTAL_TOKENS:
            failures.append("cumulative_token_limit_would_be_exceeded")
            break
        run_id = f"f5-{task['task_id']}-{effort}-{trial_id}-turn-{turn}"
        spec = {
            "schema_version": "contextlab.generation-spec.v1",
            "run_id": run_id,
            "task": prompt_task,
            "system_instruction": _SYSTEM_INSTRUCTION,
            "rendered_context": rendered,
            "reasoning_effort": effort,
            "max_tokens": F5_MAX_COMPLETION_TOKENS,
            "temperature": 0.0,
        }
        provider_path = root / F5_PROVIDER_ROOT / f"{run_id}.json"
        reservation_id = run_id
        runner = generation_runner or run_paid_generation_to_file
        runner_kwargs: dict[str, Any] = {
            "ledger": ledger,
            "root": root,
            "ledger_reservation_id": reservation_id,
        }
        if generation_runner is None:
            remaining = max(0.001, F5_MAX_SECONDS - (clock() - started))
            runner_kwargs["post_json"] = partial(
                post_json_with_timeout,
                timeout_seconds=remaining,
            )
            runner_kwargs["provider_slug"] = FRONTIER_PROVIDER_SLUG
        result = dict(runner(spec, provider_path, **runner_kwargs))
        receipt = _provider_receipt(provider_path, root, result)
        receipts.append(receipt)
        turn_tokens = int(receipt["prompt_tokens"]) + int(receipt["completion_tokens"])
        total_tokens += turn_tokens
        try:
            total_cost += Decimal(str(receipt["cost_usd"]))
            total_latency += float(receipt["latency_ms"])
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise F5Error("F5 provider accounting is invalid") from exc
        if total_tokens > F5_MAX_TOTAL_TOKENS:
            failures.append("cumulative_token_limit_exceeded")
            break
        if result.get("schema_version") == "contextlab.failed-generation-result.v2":
            failures.append(str(result.get("error")))
            break
        try:
            action = _parse_action(str(result["answer"]))
        except F5Error:
            failures.append("invalid_agent_action")
            break
        if action["action"] == "answer":
            answer = action["answer"]
            transcript.append({"turn": turn, "action": action})
            break
        if tool_calls >= F5_MAX_TOOL_CALLS:
            failures.append("tool_call_limit_exceeded")
            break
        tool_calls += 1
        if action["action"] == "search_public_chunks":
            tool_result = _tool_search(chunks, action["query"])
        else:
            tool_result = _tool_read_source(chunks, action["source_id"])
        if not tool_result["results"]:
            dead_ends += 1
        transcript.append({"turn": turn, "action": action, "tool_result": tool_result})
    else:
        failures.append("turn_limit_exceeded")

    elapsed = clock() - started
    if elapsed > F5_MAX_SECONDS and "wall_time_limit_exceeded" not in failures:
        failures.append("wall_time_limit_exceeded")
    required = list(task["required_evidence"])
    cited = {
        source
        for source in required
        if answer is not None
        and re.search(rf"(?<![A-Za-z0-9]){re.escape(source)}(?![A-Za-z0-9])", answer)
    }
    coverage = round(len(cited) / len(required), 6)
    if answer is None:
        failures.append("no_terminal_answer")
    if coverage < 1.0:
        failures.append("required_source_coverage_incomplete")
    cell = {
        "task_id": task["task_id"],
        "strategy_id": "bounded_search",
        "reasoning_effort": effort,
        "trial_id": trial_id,
        "status": "completed" if answer is not None else "failed",
        "task_success": answer is not None and coverage == 1.0 and not failures,
        "evidence_coverage": coverage,
        "required_source_count": len(required),
        "tool_calls": tool_calls,
        "dead_ends": dead_ends,
        "latency_ms": round(total_latency, 6),
        "wall_time_ms": round(elapsed * 1000, 6),
        "cost_usd": str(total_cost),
        "total_provider_tokens": total_tokens,
        "verifier_failures": sorted(set(failures)),
        "answer": answer,
        "transcript": transcript,
        "provider_receipts": receipts,
    }
    cell["record_sha256"] = sha256_json(cell)
    return cell


def _aggregate(
    baseline_cells: Sequence[Mapping[str, Any]],
    search_cells: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    rows = [*baseline_cells, *search_cells]
    strategies = sorted({str(row["strategy_id"]) for row in rows})
    return {
        strategy: {
            "cell_count": len(
                selected := [row for row in rows if row["strategy_id"] == strategy]
            ),
            "task_success_rate": round(
                sum(bool(row["task_success"]) for row in selected) / len(selected), 6
            ),
            "mean_evidence_coverage": round(
                sum(float(row["evidence_coverage"]) for row in selected)
                / len(selected),
                6,
            ),
            "tool_calls": sum(int(row["tool_calls"]) for row in selected),
            "dead_ends": sum(int(row["dead_ends"]) for row in selected),
            "latency_ms": round(sum(float(row["latency_ms"]) for row in selected), 6),
            "cost_usd": str(
                sum((Decimal(str(row["cost_usd"])) for row in selected), Decimal("0"))
            ),
            "verifier_failure_count": sum(
                len(row["verifier_failures"]) for row in selected
            ),
        }
        for strategy in strategies
    }


def validate_f5_result(value: Mapping[str, Any], *, root: Path | None = None) -> None:
    expected = {
        "schema_version",
        "experiment_id",
        "frontier_entry_gate_sha256",
        "provider_route",
        "ceiling_artifact_sha256",
        "corpus_snapshot_id",
        "task_ids",
        "trial_ids",
        "approved_tools",
        "limits",
        "baseline_cells",
        "search_cells",
        "aggregate",
        "status",
        "artifact_sha256",
    }
    if set(value) != expected or value.get("schema_version") != F5_RESULT_SCHEMA:
        raise F5Error("F5 result fields or schema changed")
    if (
        value.get("experiment_id") != "F5"
        or value.get("status") != "demonstration_pending_result_review"
        or value.get("provider_route") != FRONTIER_PROVIDER_SLUG
        or value.get("task_ids") != list(F5_TASK_IDS)
        or value.get("trial_ids") != list(F5_TRIAL_IDS)
        or value.get("approved_tools") != list(F5_APPROVED_TOOLS)
        or value.get("corpus_snapshot_id") != FROZEN_RAW_CHUNKS_SHA256
        or value.get("limits")
        != {
            "tool_calls": F5_MAX_TOOL_CALLS,
            "total_provider_tokens": F5_MAX_TOTAL_TOKENS,
            "wall_time_seconds": F5_MAX_SECONDS,
        }
    ):
        raise F5Error("F5 result identity or controls changed")
    if value.get("artifact_sha256") != sha256_json(
        {k: v for k, v in value.items() if k != "artifact_sha256"}
    ):
        raise F5Error("F5 result hash mismatch")
    for field in ("frontier_entry_gate_sha256", "ceiling_artifact_sha256"):
        if (
            not isinstance(value.get(field), str)
            or _SHA256.fullmatch(value[field]) is None
        ):
            raise F5Error(f"F5 {field} is invalid")
    baseline = value.get("baseline_cells")
    search = value.get("search_cells")
    if not isinstance(baseline, list) or len(baseline) != 8:
        raise F5Error("F5 result requires eight fixed-retrieval cells")
    if not isinstance(search, list) or len(search) != 8:
        raise F5Error("F5 result requires eight bounded-search cells")
    for cell in [*baseline, *search]:
        if cell.get("record_sha256") != sha256_json(
            {k: v for k, v in cell.items() if k != "record_sha256"}
        ):
            raise F5Error("F5 cell hash mismatch")
    baseline_grid = {
        (cell.get("task_id"), cell.get("strategy_id"), cell.get("reasoning_effort"))
        for cell in baseline
    }
    expected_baseline_grid = {
        (task_id, method, effort)
        for task_id in F5_TASK_IDS
        for method in F5_FIXED_METHODS
        for effort in ALLOWED_REASONING_EFFORTS
    }
    search_grid = {
        (
            cell.get("task_id"),
            cell.get("strategy_id"),
            cell.get("reasoning_effort"),
            cell.get("trial_id"),
        )
        for cell in search
    }
    expected_search_grid = {
        (task_id, "bounded_search", effort, trial_id)
        for task_id in F5_TASK_IDS
        for effort in ALLOWED_REASONING_EFFORTS
        for trial_id in F5_TRIAL_IDS
    }
    if baseline_grid != expected_baseline_grid or search_grid != expected_search_grid:
        raise F5Error("F5 result comparison grid changed")
    for cell in search:
        if (
            cell.get("tool_calls", F5_MAX_TOOL_CALLS + 1) > F5_MAX_TOOL_CALLS
            or cell.get("total_provider_tokens", F5_MAX_TOTAL_TOKENS + 1)
            > F5_MAX_TOTAL_TOKENS
        ):
            raise F5Error("F5 hard execution bound exceeded")
        if cell.get(
            "wall_time_ms", 0
        ) > F5_MAX_SECONDS * 1000 and "wall_time_limit_exceeded" not in cell.get(
            "verifier_failures", []
        ):
            raise F5Error("F5 wall-time overrun was not terminal")
        transcript = cell.get("transcript")
        receipts = cell.get("provider_receipts")
        if not isinstance(transcript, list) or not isinstance(receipts, list):
            raise F5Error("F5 search evidence is malformed")
        observed_tool_calls = 0
        observed_dead_ends = 0
        for entry in cell.get("transcript", []):
            action = entry.get("action", {}).get("action")
            if action not in {*F5_APPROVED_TOOLS, "answer"}:
                raise F5Error("F5 transcript contains an unapproved tool")
            if action in F5_APPROVED_TOOLS:
                observed_tool_calls += 1
                tool_result = entry.get("tool_result")
                if (
                    not isinstance(tool_result, Mapping)
                    or tool_result.get("tool") != action
                    or not isinstance(tool_result.get("results"), list)
                ):
                    raise F5Error("F5 transcript tool result is malformed")
                if not tool_result["results"]:
                    observed_dead_ends += 1
        if observed_tool_calls != cell.get(
            "tool_calls"
        ) or observed_dead_ends != cell.get("dead_ends"):
            raise F5Error("F5 transcript counters changed")
        receipt_tokens = 0
        receipt_cost = Decimal("0")
        receipt_latency = 0.0
        for receipt in receipts:
            if not isinstance(receipt, Mapping):
                raise F5Error("F5 provider receipt is malformed")
            if str(receipt.get("provider", "")).casefold() != FRONTIER_PROVIDER_SLUG:
                raise F5Error("F5 provider receipt used an unapproved route")
            receipt_path = Path(str(receipt.get("path")))
            expected_root = F5_PROVIDER_ROOT
            try:
                receipt_path.relative_to(expected_root)
            except ValueError as exc:
                raise F5Error("F5 provider receipt is outside the public root") from exc
            path_value = receipt_path.as_posix()
            if (
                receipt_path.is_absolute()
                or ".." in receipt_path.parts
                or "\\" in path_value
                or any(
                    token in path_value.casefold() for token in _FORBIDDEN_PATH_TOKENS
                )
                or not isinstance(receipt.get("sha256"), str)
                or _SHA256.fullmatch(str(receipt["sha256"])) is None
            ):
                raise F5Error("F5 provider receipt path is not public-only")
            if root is not None:
                provider_file = _public_file(root, receipt_path, "F5 provider receipt")
                if receipt["sha256"] != _sha_file(provider_file):
                    raise F5Error("F5 provider receipt bytes changed")
            try:
                prompt_tokens = int(receipt["prompt_tokens"])
                completion_tokens = int(receipt["completion_tokens"])
                cost = Decimal(str(receipt["cost_usd"]))
                latency = float(receipt["latency_ms"])
            except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
                raise F5Error("F5 provider receipt accounting is invalid") from exc
            if (
                prompt_tokens < 0
                or completion_tokens < 0
                or not cost.is_finite()
                or cost < 0
                or latency < 0
            ):
                raise F5Error("F5 provider receipt accounting is invalid")
            receipt_tokens += prompt_tokens + completion_tokens
            receipt_cost += cost
            receipt_latency += latency
        if (
            receipt_tokens != cell.get("total_provider_tokens")
            or receipt_cost != Decimal(str(cell.get("cost_usd")))
            or round(receipt_latency, 6) != cell.get("latency_ms")
        ):
            raise F5Error("F5 search accounting differs from provider receipts")
    if value.get("aggregate") != _aggregate(baseline, search):
        raise F5Error("F5 aggregate differs from deterministic replay")


def run_f5_experiment(
    root: Path | None = None,
    *,
    generation_runner: Callable[..., Mapping[str, Any]] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Run the approved two-trial public F5 demonstration and replay it."""

    repository = (root or repository_root()).resolve()
    gate = _approved_gate(repository)
    ceiling = _validate_ceiling(repository)
    result_path = repository / F5_RESULT_PATH
    if result_path.exists():
        result = _read_public_json(repository, F5_RESULT_PATH, "saved F5 result")
        validate_f5_result(result, root=repository)
        if (
            result.get("frontier_entry_gate_sha256") != gate["artifact_sha256"]
            or result.get("ceiling_artifact_sha256") != ceiling["artifact_sha256"]
        ):
            raise F5Error("saved F5 result is bound to different entry evidence")
        return result
    tasks = _load_tasks(repository)
    lab = _read_public_json(repository, F5_COMPONENT_LAB_PATH, "F5 component lab")
    metrics = _read_public_json(repository, F5_ANSWER_METRICS_PATH, "F5 answer metrics")
    if lab.get("corpus_snapshot_id") != FROZEN_RAW_CHUNKS_SHA256:
        raise F5Error("F5 public corpus snapshot changed")
    chunks = load_frozen_chunks(repository)
    baseline = _baseline_cells(metrics, tasks)
    search: list[dict[str, Any]] = []
    for trial_id in F5_TRIAL_IDS:
        for task_id in F5_TASK_IDS:
            summary = _trace_summary(lab, task_id)
            for effort in ALLOWED_REASONING_EFFORTS:
                search.append(
                    _search_cell(
                        repository,
                        task=tasks[task_id],
                        trace_summary=summary,
                        chunks=chunks,
                        effort=effort,
                        trial_id=trial_id,
                        generation_runner=generation_runner,
                        clock=clock,
                    )
                )
    result: dict[str, Any] = {
        "schema_version": F5_RESULT_SCHEMA,
        "experiment_id": "F5",
        "frontier_entry_gate_sha256": gate["artifact_sha256"],
        "provider_route": FRONTIER_PROVIDER_SLUG,
        "ceiling_artifact_sha256": ceiling["artifact_sha256"],
        "corpus_snapshot_id": FROZEN_RAW_CHUNKS_SHA256,
        "task_ids": list(F5_TASK_IDS),
        "trial_ids": list(F5_TRIAL_IDS),
        "approved_tools": list(F5_APPROVED_TOOLS),
        "limits": {
            "tool_calls": F5_MAX_TOOL_CALLS,
            "total_provider_tokens": F5_MAX_TOTAL_TOKENS,
            "wall_time_seconds": F5_MAX_SECONDS,
        },
        "baseline_cells": baseline,
        "search_cells": search,
        "aggregate": _aggregate(baseline, search),
        "status": "demonstration_pending_result_review",
    }
    result["artifact_sha256"] = sha256_json(result)
    validate_f5_result(result, root=repository)
    try:
        write_bytes_once_or_verify(repository, result_path, _json_bytes(result))
    except ImmutableIOError as exc:
        raise F5Error("immutable F5 result differs or is unsafe") from exc
    return result
