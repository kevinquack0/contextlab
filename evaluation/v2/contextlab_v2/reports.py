"""G2 ablation analysis, Markdown report, and trace viewer alpha."""

from __future__ import annotations

import html
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .baseline import repository_root
from .experiments import (
    LAB_SCHEMA,
    METHOD_IDS,
    load_protocol,
    method_cell_index,
    route_distribution,
    task_family_index,
)
from .static_benchmark import public_static_tasks
from .statistics import (
    distribution_summary,
    paired_bootstrap_ci,
    task_family_effect_summaries,
)
from .tasking import sha256_json


ANALYSIS_SCHEMA = "contextlab.g2-retrieval-analysis.v1"
PARENT_METHOD = {
    "R1": "R0",
    "R2": "R0",
    "R3": "R2",
    "R4": "R3",
    "R5": "R4",
    "R6": "R0",
    "R7": "R5",
}
METRIC_ALIASES = {"recall_at_8": "recall_at_k"}


class ReportError(ValueError):
    """Saved G2 evidence is incomplete or internally inconsistent."""


def _seed(value: str) -> int:
    return int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest()[:8], "big")


def _subset(values: Mapping[str, float], task_ids: Iterable[str]) -> dict[str, float]:
    ids = set(task_ids)
    return {task_id: value for task_id, value in values.items() if task_id in ids}


def _mean(values: Mapping[str, float]) -> float:
    if not values:
        raise ReportError("analysis subset is empty")
    return sum(values.values()) / len(values)


def validate_lab(lab: Mapping[str, Any]) -> None:
    if lab.get("schema_version") != LAB_SCHEMA:
        raise ReportError("unsupported public component lab schema")
    body = {key: value for key, value in lab.items() if key != "artifact_sha256"}
    if lab.get("artifact_sha256") != sha256_json(body):
        raise ReportError("public component lab artifact hash mismatch")
    traces = lab.get("traces")
    if not isinstance(traces, list):
        raise ReportError("public component lab has no trace list")
    task_count = lab.get("task_count")
    if (
        isinstance(task_count, bool)
        or not isinstance(task_count, int)
        or len(traces) != task_count * len(METHOD_IDS)
    ):
        raise ReportError("public component lab cell count is inconsistent")
    oracle = lab.get("oracle_router_analysis")
    if (
        not isinstance(oracle, Mapping)
        or oracle.get("schema_version") != "contextlab.oracle-router-analysis.v1"
        or oracle.get("deployable") is not False
        or oracle.get("task_count") != task_count
    ):
        raise ReportError("public component lab lacks the label-only oracle analysis")
    wiki = lab.get("compiled_wiki_control")
    if (
        not isinstance(wiki, Mapping)
        or wiki.get("schema_version") != "contextlab.compiled-wiki-control-analysis.v1"
        or wiki.get("task_count") != task_count
    ):
        raise ReportError("public component lab lacks the compiled wiki control")


def analyze_component_lab(
    lab: Mapping[str, Any],
    protocol: Mapping[str, Any],
    tasks: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Apply preregistered paired tests without changing the saved retrieval cells."""
    validate_lab(lab)
    task_rows = list(tasks)
    task_families = task_family_index(task_rows)
    traces = list(lab["traces"])
    resamples = int(protocol["promotion"]["bootstrap_resamples"])
    bootstrap_seed = str(protocol["promotion"]["bootstrap_seed"])
    minimum_delta = float(protocol["promotion"]["minimum_target_family_delta"])
    regression_floor = float(protocol["promotion"]["full_set_material_regression"])
    latency_ceiling = float(protocol["promotion"]["retrieval_p95_latency_ceiling_ms"])
    leakage = lab.get("question_reference_leakage_audit", {})
    leakage_passed = leakage.get("status") == "passed"
    masked_rows = {
        str(row["task_id"]): row
        for row in lab.get("identifier_mask_audit", {}).get("rows", [])
    }
    analyses: dict[str, Any] = {
        "R0": {
            "status": "control",
            "description": protocol["methods"]["R0"]["description"],
        }
    }
    for method in METHOD_IDS[1:]:
        parent = PARENT_METHOD[method]
        declared_metric = str(protocol["methods"][method]["primary_metric"])
        metric = METRIC_ALIASES.get(declared_metric, declared_metric)
        index = method_cell_index(traces, metric)
        baseline = index[parent]
        candidate = index[method]
        target_families = set(protocol["methods"][method]["target_families"])
        target_ids = [
            task_id
            for task_id, family in task_families.items()
            if "all" in target_families or family in target_families
        ]
        target_baseline = _subset(baseline, target_ids)
        target_candidate = _subset(candidate, target_ids)
        target_ci = paired_bootstrap_ci(
            target_baseline,
            target_candidate,
            seed=_seed(f"{bootstrap_seed}:{method}:target"),
            resamples=resamples,
        )
        full_ci = paired_bootstrap_ci(
            baseline,
            candidate,
            seed=_seed(f"{bootstrap_seed}:{method}:full"),
            resamples=resamples,
        )
        family_effects = task_family_effect_summaries(
            baseline, candidate, task_families
        )
        method_traces = [row for row in traces if row["strategy_id"] == method]
        latency = distribution_summary(
            float(row["component_metrics"]["retrieval_latency_ms"])
            for row in method_traces
        )
        context_tokens = distribution_summary(
            float(row["component_metrics"]["context_tokens"]) for row in method_traces
        )
        candidate_tokens = distribution_summary(
            float(row["component_metrics"]["candidate_tokens"]) for row in method_traces
        )
        target_delta = _mean(target_candidate) - _mean(target_baseline)
        full_delta = _mean(candidate) - _mean(baseline)
        identifier_check: dict[str, Any] = {"applicable": False}
        identifier_survives = True
        if method == "R1":
            masked = {
                task_id: float(masked_rows[task_id]["metrics"][metric])
                for task_id in target_ids
                if task_id in masked_rows
            }
            if set(masked) != set(target_ids):
                raise ReportError(
                    "identifier-mask audit does not cover the R1 target family"
                )
            masked_delta = _mean(masked) - _mean(target_baseline)
            identifier_survives = masked_delta >= minimum_delta
            identifier_check = {
                "applicable": True,
                "masked_target_mean": _mean(masked),
                "masked_target_delta_vs_parent": masked_delta,
                "minimum_delta": minimum_delta,
                "survives": identifier_survives,
            }
        criteria = {
            "target_delta_meets_minimum": target_delta >= minimum_delta,
            "target_ci_supports_direction": float(target_ci["ci_lower"]) >= 0.0,
            "full_set_not_materially_regressed": full_delta >= regression_floor,
            "latency_within_budget": float(latency["p95"]) <= latency_ceiling,
            "question_reference_leakage_passed": leakage_passed,
            "identifier_mask_check_passed": identifier_survives,
            "retrieval_cost_is_zero": all(
                str(row.get("retrieval_cost_usd", "")) == "0" for row in method_traces
            ),
        }
        analyses[method] = {
            "status": "public_passed" if all(criteria.values()) else "public_failed",
            "parent": parent,
            "description": protocol["methods"][method]["description"],
            "changed_variable": protocol["methods"][method]["changed_variable"],
            "primary_metric": metric,
            "target_families": sorted(target_families),
            "target_task_count": len(target_ids),
            "target_parent_mean": _mean(target_baseline),
            "target_candidate_mean": _mean(target_candidate),
            "target_delta": target_delta,
            "target_bootstrap": target_ci,
            "full_set_delta": full_delta,
            "full_set_bootstrap": full_ci,
            "task_family_effects": family_effects,
            "latency_ms": latency,
            "context_tokens": context_tokens,
            "candidate_tokens": candidate_tokens,
            "identifier_mask_check": identifier_check,
            "criteria": criteria,
        }
    accepted = [
        method
        for method, row in analyses.items()
        if method != "R0" and row.get("status") == "public_passed"
    ]
    payload: dict[str, Any] = {
        "schema_version": ANALYSIS_SCHEMA,
        "scope": "public_component_evidence_only",
        "protocol_sha256": sha256_json(protocol),
        "component_lab_sha256": lab["artifact_sha256"],
        "task_count": len(task_rows),
        "cell_count": len(traces),
        "methods": analyses,
        "question_reference_leakage_audit": leakage,
        "identifier_mask_audit": lab.get("identifier_mask_audit", {}),
        "route_distribution": route_distribution(traces),
        "oracle_router_analysis": lab["oracle_router_analysis"],
        "compiled_wiki_control": lab["compiled_wiki_control"],
        "public_component_candidates": accepted,
        "provisional_public_selection": None,
        "sealed_evaluation_status": "pending_external_evaluation",
        "end_to_end_status": "pending_fixed_generator_runs",
        "promotion_status": "blocked_until_public_and_sealed_end_to_end_evidence",
    }
    payload["analysis_sha256"] = sha256_json(payload)
    return payload


def _format_number(value: Any, digits: int = 4) -> str:
    return f"{float(value):.{digits}f}"


def render_markdown(analysis: Mapping[str, Any]) -> str:
    lines = [
        "# ContextLab G2 retrieval ablation",
        "",
        "This report contains public component evidence only. No stage is promoted until the "
        "external sealed run and fixed DeepSeek low/high answer runs are complete.",
        "",
        "| Method | Parent | Primary metric | Target mean | Delta | 95% CI | Full-set delta | p95 ms | Public result |",
        "|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for method in METHOD_IDS:
        row = analysis["methods"][method]
        if method == "R0":
            lines.append("| R0 | — | control | — | — | — | — | — | control |")
            continue
        ci = row["target_bootstrap"]
        lines.append(
            "| "
            + " | ".join(
                (
                    method,
                    row["parent"],
                    row["primary_metric"],
                    _format_number(row["target_candidate_mean"]),
                    _format_number(row["target_delta"]),
                    f"[{_format_number(ci['ci_lower'])}, {_format_number(ci['ci_upper'])}]",
                    _format_number(row["full_set_delta"]),
                    _format_number(row["latency_ms"]["p95"], 2),
                    row["status"],
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Boundary checks",
            "",
            f"- Question-to-gold-reference leakage: `{analysis['question_reference_leakage_audit'].get('status', 'missing')}`.",
            f"- R7 prompt-safe route distribution: `{json.dumps(analysis['route_distribution'], sort_keys=True)}`.",
            f"- Analysis-only oracle route distribution: `{json.dumps(analysis['oracle_router_analysis']['route_distribution'], sort_keys=True)}`.",
            f"- Oracle minus R7 required-source coverage: `{_format_number(analysis['oracle_router_analysis']['oracle_minus_rules']['required_source_coverage'])}`.",
            f"- R6 minus compiled-wiki recall: `{_format_number(analysis['compiled_wiki_control']['r6_minus_wiki']['recall_at_k'])}`.",
            f"- Public component candidates: `{json.dumps(analysis['public_component_candidates'])}`.",
            "- Provisional selection: `pending sealed and end-to-end evidence`.",
            f"- Sealed evaluation: `{analysis['sealed_evaluation_status']}`.",
            f"- End-to-end generation: `{analysis['end_to_end_status']}`.",
            "",
            "Every failed stage remains in the component artifact and trace viewer. A public pass is "
            "not a G2 pass; it only marks a candidate for the remaining fixed comparisons.",
            "",
        ]
    )
    return "\n".join(lines)


def render_trace_viewer_html(data_name: str) -> str:
    safe_name = html.escape(data_name, quote=True)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>ContextLab G2 retrieval traces</title>
<style>
body{{font:15px/1.45 system-ui,sans-serif;margin:0;background:#f4f2ea;color:#171717}}
header{{position:sticky;top:0;background:#171717;color:#fff;padding:14px 22px;z-index:2}}
main{{max-width:1200px;margin:24px auto;padding:0 20px}} select{{margin:0 8px;padding:7px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}} section{{background:#fff;border:1px solid #d6d0c2;padding:16px;border-radius:8px}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#efede5;padding:12px;border-radius:5px;max-height:620px;overflow:auto}}
@media(max-width:800px){{.grid{{grid-template-columns:1fr}}}}
</style></head><body>
<header><strong>ContextLab G2 trace viewer alpha</strong><label>Task <select id="task"></select></label><label>Method <select id="method"></select></label></header>
<main><p id="summary"></p><div class="grid"><section><h2>Retrieval stages</h2><pre id="stages"></pre></section><section><h2>Transitions</h2><pre id="transitions"></pre></section><section><h2>Context pack</h2><pre id="context"></pre></section><section><h2>Metrics and route</h2><pre id="metrics"></pre></section></div></main>
<script>
const task=document.querySelector('#task'),method=document.querySelector('#method');let rows=[];
const summary=document.querySelector('#summary'),stages=document.querySelector('#stages'),transitions=document.querySelector('#transitions'),context=document.querySelector('#context'),metrics=document.querySelector('#metrics');
const pretty=v=>JSON.stringify(v,null,2);function show(){{const row=rows.find(r=>r.task.task_id===task.value&&r.strategy_id===method.value);if(!row)return;summary.textContent=`${{row.task.task_id}} · ${{row.strategy_id}} · ${{row.retrieval_latency_ms}} ms · ${{row.context_tokens}} context tokens`;stages.textContent=pretty(row.retrieval_stages);transitions.textContent=pretty(row.transitions);context.textContent=row.rendered_context;metrics.textContent=pretty({{component_metrics:row.component_metrics,route:row.route,route_evidence:row.route_evidence}})}}
fetch('{safe_name}').then(r=>r.json()).then(data=>{{rows=data.traces;const tasks=[...new Set(rows.map(r=>r.task.task_id))],methods=[...new Set(rows.map(r=>r.strategy_id))];task.innerHTML=tasks.map(v=>`<option>${{v}}</option>`).join('');method.innerHTML=methods.map(v=>`<option>${{v}}</option>`).join('');task.onchange=method.onchange=show;show()}}).catch(error=>{{summary.textContent='Serve this directory over HTTP to load the trace JSON: '+error}});
</script></body></html>\n"""


def write_g2_component_reports(
    lab_path: Path,
    root: Path | None = None,
) -> dict[str, Any]:
    root = (root or repository_root()).resolve()
    try:
        lab = json.loads(lab_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError(f"cannot read component lab: {exc}") from exc
    protocol = load_protocol(root)
    tasks = public_static_tasks(root)
    analysis = analyze_component_lab(lab, protocol, tasks)
    reports = root / "results/v2/reports"
    traces = root / "results/v2/traces"
    reports.mkdir(parents=True, exist_ok=True)
    traces.mkdir(parents=True, exist_ok=True)
    analysis_path = reports / "g2_public_component_analysis.json"
    markdown_path = reports / "g2_retrieval_ablation.md"
    trace_json_path = traces / "g2_retrieval_trace_viewer.json"
    trace_html_path = traces / "g2_retrieval_trace_viewer.html"
    analysis_path.write_text(
        json.dumps(analysis, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    markdown_path.write_text(render_markdown(analysis), encoding="utf-8")
    trace_json = {
        "schema_version": "contextlab.retrieval-trace-viewer.v1",
        "component_lab_sha256": lab["artifact_sha256"],
        "traces": lab["traces"],
    }
    trace_json_path.write_text(
        json.dumps(trace_json, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    trace_html_path.write_text(
        render_trace_viewer_html(trace_json_path.name), encoding="utf-8"
    )
    return {
        "analysis": str(analysis_path.relative_to(root)),
        "report": str(markdown_path.relative_to(root)),
        "trace_json": str(trace_json_path.relative_to(root)),
        "trace_html": str(trace_html_path.relative_to(root)),
        "public_component_candidates": analysis["public_component_candidates"],
        "provisional_public_selection": analysis["provisional_public_selection"],
    }
