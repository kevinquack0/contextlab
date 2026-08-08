"""Deterministic G2 retrieval ladder and offline component scoring."""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from typing import Any, Iterable, Mapping, Sequence

from .baseline import repository_root
from .contracts import validate_instance
from .retrieval import (
    PageIndex,
    SelectionResult,
    bm25_retrieve,
    cap_per_source,
    evidence_candidate,
    estimate_tokens,
    expand_adjacent_sections,
    linear_query_passage_rerank,
    merge_duplicates,
    normalize_scored_candidates,
    oracle_route,
    render_evidence_block,
    retrieval_metrics,
    route_query,
    rrf_fuse,
)
from .static_benchmark import (
    FROZEN_EMBEDDINGS_SHA256,
    FROZEN_RAW_CHUNKS_SHA256,
    FROZEN_RAW_TAG,
    load_public_gold,
    public_static_tasks,
    refs_by_task,
)
from .tasking import prompt_safe_task, sha256_json


PROTOCOL_SCHEMA = "contextlab.retrieval-protocol.v1"
TRACE_SCHEMA = "contextlab.retrieval-trace.v1"
LAB_SCHEMA = "contextlab.retrieval-lab.v1"
DENSE_MODEL = "openai/text-embedding-3-small"
FROZEN_DATABASE_SHA256 = (
    "b35df2c1342befc79ab4b3f0bc059813eaeee85b62a0e642d582fde157c13f87"
)
FROZEN_WIKI_NODES_SHA256 = (
    "fbd1c0d465f4487afaeddc4d51f854afba0ee92c47bb3994a184bddac29dd09f"
)
METHOD_IDS = tuple(f"R{number}" for number in range(8))
STRUCTURED_TABLES = (
    "facts_long",
    "feature_adoption",
    "implementation_timeline_plan",
    "pricing_plans",
    "segment_performance",
    "support_sla",
    "support_ticket_categories",
)
ORACLE_ROUTER_VERSION = "contextlab.label-only-oracle-router.v1"
_ORACLE_COMBINED_FAMILIES = frozenset(
    {
        "authority_conflict",
        "comparison",
        "evidence_selection",
        "multi_hop_synthesis",
        "procedural_guidance",
        "recommendation",
        "table_prose_join",
    }
)
ORACLE_ROUTER_SPEC = {
    "version": ORACLE_ROUTER_VERSION,
    "deployable": False,
    "label_fields": [
        "structured_data_dependency",
        "task_family",
        "answer_type",
    ],
    "no_structured_dependency_route": "prose",
    "structured_fact_route": "structured",
    "other_structured_dependency_route": "combined",
}
_GENERATION_AMENDMENT_EVIDENCE = {
    "contextlab.g2-output-budget-amendment.600-to-1600.v1": {
        "pilot_manifest": Path("results/v2/generations/public_trial_1_manifest.json"),
        "pilot_metrics": Path(
            "results/v2/reports/g2_output_limit_600_pilot_metrics.json"
        ),
        "pilot_report": Path("results/v2/reports/g2_output_limit_600_pilot.json"),
    },
    "contextlab.g2-output-budget-amendment.1600-to-8192.v1": {
        "pilot_manifest": Path(
            "results/v2/generations/public_g2r1_trial_1_manifest.json"
        ),
        "pilot_metrics": Path(
            "results/v2/reports/g2_output_limit_1600_pilot_metrics.json"
        ),
        "pilot_report": Path("results/v2/reports/g2_output_limit_1600_pilot.json"),
    },
}
_GENERATION_AMENDMENT_FIELDS = {
    "amendment_id",
    "campaign_id",
    "frozen_before_successor_run",
    "pilot_manifest",
    "pilot_manifest_file_sha256",
    "pilot_manifest_sha256",
    "pilot_metrics",
    "pilot_metrics_file_sha256",
    "pilot_metrics_sha256",
    "pilot_output_token_limit",
    "pilot_report",
    "pilot_report_file_sha256",
    "pilot_status_counts",
    "protocol_sha256",
    "reason",
    "successor_campaign_id",
    "successor_output_token_limit",
}


class ExperimentError(ValueError):
    """A retrieval run violates its frozen comparison contract."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def embedding_key(text: str, model: str = DENSE_MODEL) -> str:
    return f"{model}:{hashlib.sha1(text.encode('utf-8')).hexdigest()}"


def chunk_embedding_text(chunk: Mapping[str, Any]) -> str:
    """Match the frozen v1 dense control's exact embedding input."""
    return (
        f"Source: {chunk['source_id']}#{chunk['section_id']}\n"
        f"Title: {chunk['title']}\n"
        f"Heading: {chunk['heading']}\n"
        f"Authority: {chunk['authority_level']}\n"
        f"Date: {chunk['publication_date']}\n"
        f"Status: {chunk['status']}\n"
        f"Text: {chunk['text']}"
    )


def load_protocol(root: Path | None = None) -> dict[str, Any]:
    root = (root or repository_root()).resolve()
    path = root / "evaluation/v2/retrieval_protocol.json"
    try:
        protocol = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExperimentError(f"cannot read retrieval protocol: {exc}") from exc
    validate_protocol(protocol)
    _validate_generation_amendment_evidence(root, protocol)
    return protocol


def _validate_generation_amendment_evidence(
    root: Path, protocol: Mapping[str, Any]
) -> None:
    """Verify every frozen failed pilot before accepting the latest protocol."""
    amendments = protocol["generation_feasibility_amendments"]
    for amendment in amendments:
        amendment_id = str(amendment["amendment_id"])
        paths = _GENERATION_AMENDMENT_EVIDENCE[amendment_id]
        evidence: dict[str, Mapping[str, Any]] = {}
        for evidence_field, relative_path in paths.items():
            if amendment[evidence_field] != str(relative_path):
                raise ExperimentError(
                    f"generation feasibility evidence path changed: {amendment_id}"
                )
            path = root / relative_path
            if path.is_symlink():
                raise ExperimentError(
                    f"generation feasibility evidence is a symlink: {relative_path}"
                )
            try:
                raw = path.read_bytes()
            except OSError as exc:
                raise ExperimentError(
                    f"cannot read generation feasibility evidence {relative_path}: {exc}"
                ) from exc
            if _sha256_bytes(raw) != amendment[f"{evidence_field}_file_sha256"]:
                raise ExperimentError(
                    f"generation feasibility evidence hash changed: {relative_path}"
                )
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ExperimentError(
                    f"generation feasibility evidence is not JSON: {relative_path}"
                ) from exc
            if not isinstance(value, Mapping):
                raise ExperimentError(
                    f"generation feasibility evidence is not an object: {relative_path}"
                )
            evidence[evidence_field] = value

        manifest = evidence["pilot_manifest"]
        manifest_body = {
            key: value for key, value in manifest.items() if key != "manifest_sha256"
        }
        statuses = amendment["pilot_status_counts"]
        cells = manifest.get("cells")
        observed_statuses = (
            Counter(cell.get("status") for cell in cells if isinstance(cell, Mapping))
            if isinstance(cells, list)
            else Counter()
        )
        if (
            manifest.get("schema_version") != "contextlab.g2-generation-manifest.v1"
            or manifest.get("manifest_sha256") != amendment["pilot_manifest_sha256"]
            or manifest.get("manifest_sha256") != sha256_json(manifest_body)
            or manifest.get("status_counts") != statuses
            or not isinstance(cells, list)
            or manifest.get("recorded_cell_count") != len(cells)
            or len(cells) != sum(statuses.values())
            or any(
                observed_statuses[status] != count for status, count in statuses.items()
            )
            or set(observed_statuses) - set(statuses)
            or (
                amendment["campaign_id"] != "g2"
                and (
                    manifest.get("generation_campaign_id") != amendment["campaign_id"]
                    or manifest.get("generation_protocol_sha256")
                    != amendment["protocol_sha256"]
                    or manifest.get("output_token_limit")
                    != amendment["pilot_output_token_limit"]
                )
            )
        ):
            raise ExperimentError(
                f"generation feasibility pilot manifest changed: {amendment_id}"
            )

        report = evidence["pilot_report"]
        metrics = evidence["pilot_metrics"]
        metrics_body = {
            key: value for key, value in metrics.items() if key != "artifact_sha256"
        }
        metrics_hash = metrics.get("artifact_sha256")
        if (
            report.get("schema_version")
            != "contextlab.g2-generation-feasibility-pilot.v1"
            or report.get("status") != "halted_before_primary_run"
            or report.get("generation_manifest") != amendment["pilot_manifest"]
            or report.get("generation_manifest_sha256")
            != amendment["pilot_manifest_sha256"]
            or report.get("screening_metrics") != amendment["pilot_metrics"]
            or report.get("screening_metrics_sha256")
            != amendment["pilot_metrics_sha256"]
            or report.get("output_token_limit") != amendment["pilot_output_token_limit"]
            or report.get("status_counts") != statuses
            or report.get("recorded_cell_count") != len(cells)
            or report.get("expected_cell_count") != manifest.get("expected_cell_count")
            or report.get("retrieval_protocol_sha256") != amendment["protocol_sha256"]
            or metrics.get("schema_version") != "contextlab.g2-answer-metrics.v1"
            or metrics.get("scope") != "public_deterministic_screening"
            or metrics_hash != amendment["pilot_metrics_sha256"]
            or metrics_hash != sha256_json(metrics_body)
            or metrics.get("generation_manifest_sha256")
            != amendment["pilot_manifest_sha256"]
            or metrics.get("completed_cell_count") != statuses["completed"]
            or metrics.get("component_lab_sha256") != report.get("component_lab_sha256")
            or (
                amendment["campaign_id"] != "g2"
                and (
                    report.get("generation_campaign_id") != amendment["campaign_id"]
                    or metrics.get("generation_campaign_id") != amendment["campaign_id"]
                    or metrics.get("generation_protocol_sha256")
                    != amendment["protocol_sha256"]
                    or metrics.get("output_token_limit")
                    != amendment["pilot_output_token_limit"]
                )
            )
        ):
            raise ExperimentError(
                f"generation feasibility pilot report changed: {amendment_id}"
            )


def validate_protocol(protocol: Mapping[str, Any]) -> None:
    if protocol.get("schema_version") != PROTOCOL_SCHEMA:
        raise ExperimentError("unsupported retrieval protocol schema")
    comparison = protocol.get("fixed_comparison")
    methods = protocol.get("methods")
    promotion = protocol.get("promotion")
    if not isinstance(comparison, Mapping) or not isinstance(methods, Mapping):
        raise ExperimentError(
            "retrieval protocol lacks comparison or method definitions"
        )
    if tuple(sorted(methods)) != METHOD_IDS:
        raise ExperimentError("retrieval protocol must define exactly R0 through R7")
    if comparison.get("reasoning_efforts") != ["low", "high"]:
        raise ExperimentError(
            "retrieval protocol must compare exactly low and high reasoning"
        )
    for field in (
        "candidate_limit",
        "context_token_budget",
        "final_k",
        "output_token_limit",
    ):
        value = comparison.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ExperimentError(
                f"retrieval protocol {field} must be a positive integer"
            )
    if comparison.get("candidate_limit", 0) < comparison.get("final_k", 0):
        raise ExperimentError("candidate limit cannot be lower than final k")
    if comparison.get("temperature") != 0.0:
        raise ExperimentError("the fixed retrieval answer temperature must be zero")
    campaign = comparison.get("generation_campaign_id")
    if (
        not isinstance(campaign, str)
        or not campaign
        or len(campaign) > 32
        or any(not (character.isalnum() or character in "-_") for character in campaign)
    ):
        raise ExperimentError("retrieval protocol generation campaign ID is invalid")
    amendments = protocol.get("generation_feasibility_amendments")
    expected_chain = (
        (
            "contextlab.g2-output-budget-amendment.600-to-1600.v1",
            "g2",
            600,
            "g2r1",
            1600,
        ),
        (
            "contextlab.g2-output-budget-amendment.1600-to-8192.v1",
            "g2r1",
            1600,
            "g2r2",
            8192,
        ),
    )
    if not isinstance(amendments, list) or len(amendments) != len(expected_chain):
        raise ExperimentError("retrieval protocol feasibility amendments changed")
    for amendment, expected in zip(amendments, expected_chain, strict=True):
        if (
            not isinstance(amendment, Mapping)
            or set(amendment) != _GENERATION_AMENDMENT_FIELDS
        ):
            raise ExperimentError("retrieval protocol feasibility amendment changed")
        (
            amendment_id,
            pilot_campaign,
            pilot_limit,
            successor_campaign,
            successor_limit,
        ) = expected
        hashes = (
            amendment.get("pilot_manifest_file_sha256"),
            amendment.get("pilot_manifest_sha256"),
            amendment.get("pilot_metrics_file_sha256"),
            amendment.get("pilot_metrics_sha256"),
            amendment.get("pilot_report_file_sha256"),
            amendment.get("protocol_sha256"),
        )
        statuses = amendment.get("pilot_status_counts")
        expected_paths = _GENERATION_AMENDMENT_EVIDENCE[amendment_id]
        if (
            amendment.get("amendment_id") != amendment_id
            or amendment.get("campaign_id") != pilot_campaign
            or amendment.get("pilot_output_token_limit") != pilot_limit
            or amendment.get("successor_campaign_id") != successor_campaign
            or amendment.get("successor_output_token_limit") != successor_limit
            or amendment.get("frozen_before_successor_run") is not True
            or any(
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
                for value in hashes
            )
            or any(
                amendment.get(field) != str(path)
                for field, path in expected_paths.items()
            )
            or not isinstance(statuses, Mapping)
            or set(statuses) != {"completed", "failed", "pending"}
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in statuses.values()
            )
            or statuses["failed"] == 0
            or not isinstance(amendment.get("reason"), str)
            or not amendment["reason"]
        ):
            raise ExperimentError("retrieval protocol feasibility amendment is invalid")
    last_amendment = amendments[-1]
    if (
        last_amendment["successor_campaign_id"] != comparison["generation_campaign_id"]
        or last_amendment["successor_output_token_limit"]
        != comparison["output_token_limit"]
    ):
        raise ExperimentError("retrieval protocol latest campaign is not the successor")
    if protocol.get("corpus", {}).get("chunks_sha256") != FROZEN_RAW_CHUNKS_SHA256:
        raise ExperimentError(
            "retrieval protocol points to a different frozen chunk artifact"
        )
    dense = protocol.get("dense_control", {})
    if (
        dense.get("model") != DENSE_MODEL
        or dense.get("base_cache_sha256") != FROZEN_EMBEDDINGS_SHA256
    ):
        raise ExperimentError("retrieval protocol points to a different dense control")
    structured = protocol.get("structured_control", {})
    if (
        structured.get("database_sha256") != FROZEN_DATABASE_SHA256
        or tuple(structured.get("tables", ())) != STRUCTURED_TABLES
    ):
        raise ExperimentError(
            "retrieval protocol points to a different structured control"
        )
    wiki = protocol.get("compiled_wiki_control", {})
    if (
        wiki.get("nodes_sha256") != FROZEN_WIKI_NODES_SHA256
        or wiki.get("node_count") != 13
        or wiki.get("max_seed_nodes") != 3
    ):
        raise ExperimentError(
            "retrieval protocol points to a different compiled wiki control"
        )
    if (
        not isinstance(promotion, Mapping)
        or promotion.get("stochastic_trial_count", 0) < 5
    ):
        raise ExperimentError(
            "retrieval protocol requires at least five stochastic trials"
        )
    repeat_ids = promotion.get("temperature_zero_repeat_task_ids")
    if (
        not isinstance(repeat_ids, list)
        or len(repeat_ids) != 12
        or len(set(repeat_ids)) != 12
        or any(
            not isinstance(task_id, str)
            or task_id
            not in {f"S{number:03d}" for number in (*range(1, 81), *range(117, 121))}
            for task_id in repeat_ids
        )
    ):
        raise ExperimentError("retrieval protocol must freeze 12 public repeat tasks")
    if protocol.get("oracle_router") != ORACLE_ROUTER_SPEC:
        raise ExperimentError("retrieval protocol changed the label-only oracle router")


def load_frozen_chunks(root: Path | None = None) -> list[dict[str, Any]]:
    """Load the exact raw-tag artifact so all 259 cached chunk vectors remain valid."""
    root = (root or repository_root()).resolve()
    result = subprocess.run(
        ["git", "show", f"{FROZEN_RAW_TAG}:evaluation/build/chunks.jsonl"],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise ExperimentError("cannot read the frozen raw chunk artifact")
    if _sha256_bytes(result.stdout) != FROZEN_RAW_CHUNKS_SHA256:
        raise ExperimentError("frozen raw chunk artifact hash changed")
    chunks: list[dict[str, Any]] = []
    document_ordinals: dict[str, int] = {}
    for line_number, line in enumerate(
        result.stdout.decode("utf-8").splitlines(), start=1
    ):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ExperimentError(f"frozen chunk row {line_number} is invalid") from exc
        if not isinstance(row, dict):
            raise ExperimentError(f"frozen chunk row {line_number} is not an object")
        source_id = str(row["source_id"])
        document_ordinals.setdefault(source_id, len(document_ordinals))
        row["document_ordinal"] = document_ordinals[source_id]
        row["corpus_ordinal"] = len(chunks)
        chunks.append(row)
    if len(chunks) != 259 or len({row["chunk_id"] for row in chunks}) != len(chunks):
        raise ExperimentError("frozen raw artifact must contain 259 unique chunks")
    return chunks


def validate_embedding_map(
    embeddings: Mapping[str, Sequence[float]], *, dimensions: int = 1536
) -> None:
    for key, vector in embeddings.items():
        if not isinstance(key, str) or not key:
            raise ExperimentError("embedding map contains an invalid key")
        if len(vector) != dimensions or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in vector
        ):
            raise ExperimentError(
                f"embedding {key} is not a finite {dimensions}-vector"
            )


def required_embedding_keys(
    tasks: Iterable[Mapping[str, Any]], chunks: Iterable[Mapping[str, Any]]
) -> dict[str, list[str]]:
    return {
        "queries": [embedding_key(str(task["question_text"])) for task in tasks],
        "chunks": [embedding_key(chunk_embedding_text(chunk)) for chunk in chunks],
    }


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ExperimentError("embedding dimensions differ")
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


def dense_score_map(
    question: str,
    chunks: Sequence[Mapping[str, Any]],
    embeddings: Mapping[str, Sequence[float]],
) -> dict[str, float]:
    query_key = embedding_key(question)
    if query_key not in embeddings:
        raise ExperimentError(f"dense query embedding is missing: {query_key}")
    query = embeddings[query_key]
    scores: dict[str, float] = {}
    for chunk in chunks:
        key = embedding_key(chunk_embedding_text(chunk))
        vector = embeddings.get(key)
        if vector is None:
            raise ExperimentError(
                f"dense chunk embedding is missing: {chunk['chunk_id']}"
            )
        scores[str(chunk["chunk_id"])] = _cosine(query, vector)
    return scores


def _candidate_id_map(
    chunks: Iterable[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    rows = list(chunks)
    return {
        str(
            evidence_candidate(
                chunk,
                stage="identity",
                rank=index,
                raw_score=None,
                normalized_score=None,
            )["candidate_id"]
        ): chunk
        for index, chunk in enumerate(rows, start=1)
    }


def _passage_maps(
    chunks: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Mapping[str, Any]], dict[str, str]]:
    by_candidate = _candidate_id_map(chunks)
    return by_candidate, {
        candidate_id: str(chunk.get("text", ""))
        for candidate_id, chunk in by_candidate.items()
    }


def _pack_ranked(
    candidates: Sequence[Mapping[str, Any]],
    chunks_by_candidate: Mapping[str, Mapping[str, Any]],
    *,
    token_budget: int,
    final_k: int | None,
) -> SelectionResult:
    selected: list[dict[str, Any]] = []
    transitions: dict[str, str] = {}
    used = 0
    for candidate in sorted(
        candidates, key=lambda row: (int(row.get("rank", 1)), str(row["candidate_id"]))
    ):
        candidate_id = str(candidate["candidate_id"])
        chunk = chunks_by_candidate.get(candidate_id)
        if chunk is None:
            transitions[candidate_id] = "missing_passage"
            continue
        if final_k is not None and len(selected) >= final_k:
            transitions[candidate_id] = "final_k"
            continue
        size = estimate_tokens(render_evidence_block(chunk))
        if used + size > token_budget:
            transitions[candidate_id] = "context_token_budget"
            continue
        item = dict(candidate)
        item["rank"] = len(selected) + 1
        selected.append(item)
        used += size
        transitions[candidate_id] = "selected_by_rank"
    return SelectionResult(tuple(selected), transitions, used)


def _elapsed_ms(start_ns: int) -> float:
    return max(0.0, (time.perf_counter_ns() - start_ns) / 1_000_000)


def _validate_candidates(candidates: Iterable[Mapping[str, Any]]) -> None:
    for candidate in candidates:
        findings = validate_instance("EvidenceCandidate", dict(candidate))
        if findings:
            raise ExperimentError("invalid EvidenceCandidate: " + "; ".join(findings))


def _serialise_sql_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, float):
        return format(value, ".12g")
    return str(value)


def load_structured_chunks(root: Path | None = None) -> list[dict[str, Any]]:
    """Read fixed tables from the frozen SQLite control through read-only queries."""
    root = (root or repository_root()).resolve()
    database = root / "evaluation/build/novalearn.db"
    if _sha256_file(database) != FROZEN_DATABASE_SHA256:
        raise ExperimentError("structured database differs from the frozen control")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    chunks: list[dict[str, Any]] = []
    try:
        for table in STRUCTURED_TABLES:
            columns = [
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            ]
            if not columns or "source_section" not in columns:
                raise ExperimentError(f"structured table lacks source_section: {table}")
            rows = connection.execute(f"SELECT * FROM {table}").fetchall()
            for ordinal, row in enumerate(rows, start=1):
                section_id = str(row["source_section"])
                source_id = section_id.split("-S", 1)[0]
                text = " | ".join(
                    f"{column}={_serialise_sql_value(row[column])}"
                    for column in columns
                )
                chunks.append(
                    {
                        "chunk_id": f"SQL-{table}-{ordinal:03d}",
                        "source_id": source_id,
                        "section_id": section_id,
                        "title": f"Structured table: {table}",
                        "heading": table,
                        "authority_level": row["authority_level"]
                        if "authority_level" in columns
                        else None,
                        "status": row["doc_status"]
                        if "doc_status" in columns
                        else "frozen",
                        "publication_date": row["publication_date"]
                        if "publication_date" in columns
                        else None,
                        "text": text,
                        "structured_table": table,
                    }
                )
    finally:
        connection.close()
    if not chunks:
        raise ExperimentError("structured database produced no retrieval rows")
    return chunks


def load_compiled_wiki_nodes(root: Path | None = None) -> dict[str, dict[str, Any]]:
    """Load the fixed v1 compiled wiki without running its historical LLM navigator."""
    root = (root or repository_root()).resolve()
    path = root / "evaluation/build/wiki/nodes.json"
    if _sha256_file(path) != FROZEN_WIKI_NODES_SHA256:
        raise ExperimentError("compiled wiki nodes differ from the frozen control")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExperimentError("cannot load the compiled wiki control") from exc
    if not isinstance(value, dict) or len(value) != 13:
        raise ExperimentError("compiled wiki control must contain 13 nodes")
    nodes: dict[str, dict[str, Any]] = {}
    for slug, node in value.items():
        if (
            not isinstance(slug, str)
            or not isinstance(node, dict)
            or node.get("slug") != slug
            or not isinstance(node.get("content"), str)
            or not isinstance(node.get("source_refs"), list)
            or not all(isinstance(ref, str) for ref in node["source_refs"])
        ):
            raise ExperimentError("compiled wiki node shape is invalid")
        nodes[slug] = node
    return nodes


def _render_context(
    selected: Sequence[Mapping[str, Any]],
    chunks_by_candidate: Mapping[str, Mapping[str, Any]],
) -> str:
    blocks: list[str] = []
    for candidate in selected:
        chunk = chunks_by_candidate[str(candidate["candidate_id"])]
        blocks.append(render_evidence_block(chunk))
    return "\n\n---\n\n".join(blocks)


def _trace(
    *,
    task: Mapping[str, str],
    strategy_id: str,
    protocol_sha256: str,
    stages: Mapping[str, Sequence[Mapping[str, Any]]],
    selected: Sequence[Mapping[str, Any]],
    chunks_by_candidate: Mapping[str, Mapping[str, Any]],
    transitions: Mapping[str, str],
    stage_latencies_ms: Mapping[str, float],
    route: str | None,
    route_evidence: Mapping[str, Any],
    context_token_budget: int,
    corpus_snapshot_id: str = FROZEN_RAW_CHUNKS_SHA256,
) -> dict[str, Any]:
    all_candidates = {
        str(candidate["candidate_id"]): candidate
        for rows in stages.values()
        for candidate in rows
    }
    for candidate in selected:
        all_candidates[str(candidate["candidate_id"])] = candidate
    all_passage_texts = {
        candidate_id: str(chunks_by_candidate[candidate_id].get("text", ""))
        for candidate_id in all_candidates
        if candidate_id in chunks_by_candidate
    }
    passage_texts = {
        str(candidate["candidate_id"]): str(
            chunks_by_candidate[str(candidate["candidate_id"])].get("text", "")
        )
        for candidate in selected
    }
    rendered = _render_context(selected, chunks_by_candidate)
    run_id = hashlib.sha256(
        f"{protocol_sha256}\0{task['task_id']}\0{strategy_id}".encode("utf-8")
    ).hexdigest()[:24]
    context_tokens = estimate_tokens(rendered)
    if context_tokens > context_token_budget:
        raise ExperimentError("rendered context exceeds the fixed token budget")
    return {
        "schema_version": TRACE_SCHEMA,
        "run_id": run_id,
        "task": dict(task),
        "strategy_id": strategy_id,
        "protocol_sha256": protocol_sha256,
        "corpus_snapshot_id": corpus_snapshot_id,
        "retrieval_stages": {
            name: [dict(row) for row in rows] for name, rows in stages.items()
        },
        "selected_candidates": [dict(row) for row in selected],
        "candidate_passages": passage_texts,
        "transitions": dict(transitions),
        "stage_latencies_ms": {
            key: round(value, 6) for key, value in stage_latencies_ms.items()
        },
        "retrieval_latency_ms": round(sum(stage_latencies_ms.values()), 6),
        "retrieval_cost_usd": "0",
        "route": route,
        "route_evidence": dict(route_evidence),
        "context_token_budget": context_token_budget,
        "context_tokens": context_tokens,
        "candidate_tokens": sum(
            estimate_tokens(text) for text in all_passage_texts.values()
        ),
        "rendered_context": rendered,
        "rendered_context_sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
    }


def run_task_ladder(
    task: Mapping[str, str],
    chunks: Sequence[Mapping[str, Any]],
    embeddings: Mapping[str, Sequence[float]],
    structured_chunks: Sequence[Mapping[str, Any]],
    protocol: Mapping[str, Any],
    *,
    corpus_snapshot_id: str = FROZEN_RAW_CHUNKS_SHA256,
) -> list[dict[str, Any]]:
    """Run R0-R7 using a prompt-safe task. This function has no gold input."""
    if set(task) != {
        "schema_version",
        "task_id",
        "suite",
        "question_text",
        "question_sha256",
    }:
        raise ExperimentError("retrieval task must use the prompt-safe projection")
    validate_protocol(protocol)
    if not re.fullmatch(r"[0-9a-f]{64}", corpus_snapshot_id):
        raise ExperimentError("retrieval corpus snapshot ID must be a SHA-256 digest")
    comparison = protocol["fixed_comparison"]
    methods = protocol["methods"]
    candidate_limit = int(comparison["candidate_limit"])
    final_k = int(comparison["final_k"])
    budget = int(comparison["context_token_budget"])
    protocol_hash = sha256_json(protocol)
    question = task["question_text"]

    all_chunks = [*chunks, *structured_chunks]
    chunks_by_candidate, _ = _passage_maps(all_chunks)
    raw_by_candidate, _ = _passage_maps(chunks)
    structured_by_candidate, _ = _passage_maps(structured_chunks)

    started = time.perf_counter_ns()
    dense_scores = dense_score_map(question, chunks, embeddings)
    dense = normalize_scored_candidates(
        chunks, dense_scores, stage="dense", limit=candidate_limit
    )
    dense_ms = _elapsed_ms(started)

    started = time.perf_counter_ns()
    bm25 = bm25_retrieve(
        question,
        chunks,
        limit=candidate_limit,
        k1=float(methods["R1"]["bm25_k1"]),
        b=float(methods["R1"]["bm25_b"]),
    )
    bm25_ms = _elapsed_ms(started)

    started = time.perf_counter_ns()
    fusion_result = rrf_fuse(
        {"dense": dense, "bm25": bm25},
        constant=int(methods["R2"]["rrf_constant"]),
        limit=candidate_limit,
    )
    fused = list(fusion_result.candidates)
    fusion_ms = _elapsed_ms(started)

    started = time.perf_counter_ns()
    reranked = linear_query_passage_rerank(
        question, fused, chunks, limit=candidate_limit
    )
    rerank_ms = _elapsed_ms(started)

    started = time.perf_counter_ns()
    merged = merge_duplicates(reranked)
    diversified = cap_per_source(
        merged.candidates, cap=int(methods["R4"]["per_source_cap"])
    )
    diversity_ms = _elapsed_ms(started)
    diversity_transitions = {**merged.transitions, **diversified.transitions}

    packs = {
        "R0": _pack_ranked(
            dense, raw_by_candidate, token_budget=budget, final_k=final_k
        ),
        "R1": _pack_ranked(
            bm25, raw_by_candidate, token_budget=budget, final_k=final_k
        ),
        "R2": _pack_ranked(
            fused, raw_by_candidate, token_budget=budget, final_k=final_k
        ),
        "R3": _pack_ranked(
            reranked, raw_by_candidate, token_budget=budget, final_k=final_k
        ),
        "R4": _pack_ranked(
            diversified.candidates,
            raw_by_candidate,
            token_budget=budget,
            final_k=final_k,
        ),
    }

    started = time.perf_counter_ns()
    expanded = expand_adjacent_sections(
        packs["R4"].candidates, chunks, token_budget=budget
    )
    expansion_ms = _elapsed_ms(started)

    started = time.perf_counter_ns()
    page_index = PageIndex.build(chunks)
    branches = page_index.select_branches(
        question,
        max_branches=int(methods["R6"]["max_branches"]),
        token_budget=int(methods["R6"]["navigation_token_budget"]),
    )
    tree_pack = page_index.expand_raw_leaves(branches, chunks, token_budget=budget)
    tree_ms = _elapsed_ms(started)

    started = time.perf_counter_ns()
    route = route_query(question)
    structured = bm25_retrieve(
        question, structured_chunks, limit=candidate_limit, stage="sql_structured"
    )
    if route == "structured":
        routed_candidates = structured
    elif route == "combined":
        routed_candidates = list(
            rrf_fuse(
                {"prose": list(expanded.candidates), "structured": structured},
                constant=int(methods["R2"]["rrf_constant"]),
                limit=candidate_limit,
            ).candidates
        )
    else:
        routed_candidates = list(expanded.candidates)
    routed_pack = _pack_ranked(
        routed_candidates,
        chunks_by_candidate,
        token_budget=budget,
        final_k=None,
    )
    routing_ms = _elapsed_ms(started)

    for rows in (
        dense,
        bm25,
        fused,
        reranked,
        diversified.candidates,
        expanded.candidates,
        tree_pack.candidates,
        structured,
        routed_pack.candidates,
    ):
        _validate_candidates(rows)

    stage_sets: dict[str, dict[str, Sequence[Mapping[str, Any]]]] = {
        "R0": {"dense": dense},
        "R1": {"bm25": bm25},
        "R2": {"dense": dense, "bm25": bm25, "rrf": fused},
        "R3": {"dense": dense, "bm25": bm25, "rrf": fused, "reranker": reranked},
        "R4": {
            "dense": dense,
            "bm25": bm25,
            "rrf": fused,
            "reranker": reranked,
            "diversity": list(diversified.candidates),
        },
        "R5": {
            "dense": dense,
            "bm25": bm25,
            "rrf": fused,
            "reranker": reranked,
            "diversity": list(diversified.candidates),
            "adjacent_expansion": list(expanded.candidates),
        },
        "R6": {"pageindex_raw_leaf": list(tree_pack.candidates)},
        "R7": {
            "prose": list(expanded.candidates),
            "sql_structured": structured,
            "routed": list(routed_pack.candidates),
        },
    }
    selections: dict[str, SelectionResult] = {
        **packs,
        "R5": expanded,
        "R6": tree_pack,
        "R7": routed_pack,
    }
    latencies = {
        "R0": {"dense": dense_ms},
        "R1": {"bm25": bm25_ms},
        "R2": {"dense": dense_ms, "bm25": bm25_ms, "rrf": fusion_ms},
        "R3": {
            "dense": dense_ms,
            "bm25": bm25_ms,
            "rrf": fusion_ms,
            "reranker": rerank_ms,
        },
        "R4": {
            "dense": dense_ms,
            "bm25": bm25_ms,
            "rrf": fusion_ms,
            "reranker": rerank_ms,
            "diversity": diversity_ms,
        },
        "R5": {
            "dense": dense_ms,
            "bm25": bm25_ms,
            "rrf": fusion_ms,
            "reranker": rerank_ms,
            "diversity": diversity_ms,
            "adjacent_expansion": expansion_ms,
        },
        "R6": {"pageindex": tree_ms},
        "R7": {
            "dense": dense_ms,
            "bm25": bm25_ms,
            "rrf": fusion_ms,
            "reranker": rerank_ms,
            "diversity": diversity_ms,
            "adjacent_expansion": expansion_ms,
            "routing": routing_ms,
        },
    }
    route_evidence = {
        "rules_version": "contextlab.prompt-safe-router.v1",
        "question_sha256": task["question_sha256"],
        "selected_route": route,
        "oracle_route": "withheld_from_deployable_run",
    }
    traces: list[dict[str, Any]] = []
    for strategy_id in METHOD_IDS:
        selection = selections[strategy_id]
        transitions = dict(selection.transitions)
        if strategy_id in {"R4", "R5", "R7"}:
            transitions = {**diversity_transitions, **transitions}
        if strategy_id in {"R2", "R3", "R4", "R5", "R7"}:
            for candidate_id, ranks in fusion_result.stage_evidence.items():
                transitions.setdefault(
                    candidate_id,
                    "rrf:"
                    + ",".join(
                        f"{name}={rank}" for name, rank in sorted(ranks.items())
                    ),
                )
        traces.append(
            _trace(
                task=task,
                strategy_id=strategy_id,
                protocol_sha256=protocol_hash,
                stages=stage_sets[strategy_id],
                selected=selection.candidates,
                chunks_by_candidate=chunks_by_candidate,
                transitions=transitions,
                stage_latencies_ms=latencies[strategy_id],
                route=route if strategy_id == "R7" else None,
                route_evidence=route_evidence if strategy_id == "R7" else {},
                context_token_budget=budget,
                corpus_snapshot_id=corpus_snapshot_id,
            )
        )
    return traces


def build_compiled_wiki_traces(
    tasks: Sequence[Mapping[str, str]],
    chunks: Sequence[Mapping[str, Any]],
    wiki_nodes: Mapping[str, Mapping[str, Any]],
    protocol: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Run a fixed lexical navigator over the existing compiled v1 wiki."""
    validate_protocol(protocol)
    budget = int(protocol["fixed_comparison"]["context_token_budget"])
    max_seeds = int(protocol["compiled_wiki_control"]["max_seed_nodes"])
    protocol_hash = sha256_json(protocol)
    node_chunks = [
        {
            "chunk_id": f"WIKI-{slug}",
            "source_id": f"WIKI-{slug}",
            "section_id": f"WIKI-{slug}",
            "title": str(node.get("title", slug)),
            "heading": str(node.get("title", slug)),
            "authority_level": None,
            "status": "frozen_compiled_control",
            "publication_date": None,
            "text": str(node["content"]),
            "wiki_slug": slug,
        }
        for slug, node in sorted(wiki_nodes.items())
    ]
    node_by_candidate = {
        str(
            evidence_candidate(
                node,
                stage="compiled_wiki_identity",
                rank=index,
                raw_score=None,
                normalized_score=None,
            )["candidate_id"]
        ): wiki_nodes[str(node["wiki_slug"])]
        for index, node in enumerate(node_chunks, start=1)
    }
    raw_by_section: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for chunk in chunks:
        raw_by_section[str(chunk["section_id"])].append(chunk)
    chunks_by_candidate, _ = _passage_maps([*chunks, *node_chunks])
    traces: list[dict[str, Any]] = []
    for task in tasks:
        started = time.perf_counter_ns()
        node_candidates = bm25_retrieve(
            str(task["question_text"]),
            node_chunks,
            limit=max_seeds,
            stage="compiled_wiki_node_bm25",
            include_source_identifiers=False,
        )
        raw_candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        transitions: dict[str, str] = {}
        for node_candidate in node_candidates:
            node = node_by_candidate[str(node_candidate["candidate_id"])]
            for reference in node["source_refs"]:
                section_id = str(reference).split("#", 1)[-1]
                for chunk in raw_by_section.get(section_id, ()):
                    candidate = evidence_candidate(
                        chunk,
                        stage="compiled_wiki_raw_pointer",
                        rank=len(raw_candidates) + 1,
                        raw_score=node_candidate.get("raw_score"),
                        normalized_score=node_candidate.get("normalized_score"),
                    )
                    candidate_id = str(candidate["candidate_id"])
                    if candidate_id in seen:
                        continue
                    seen.add(candidate_id)
                    raw_candidates.append(candidate)
                    transitions[candidate_id] = (
                        "expanded_from_compiled_wiki_node:"
                        + str(node_candidate["text_reference"])
                    )
        selection = _pack_ranked(
            raw_candidates,
            chunks_by_candidate,
            token_budget=budget,
            final_k=None,
        )
        transitions.update(selection.transitions)
        latency = _elapsed_ms(started)
        _validate_candidates([*node_candidates, *selection.candidates])
        traces.append(
            _trace(
                task=task,
                strategy_id="W0",
                protocol_sha256=protocol_hash,
                stages={
                    "compiled_wiki_node_bm25": node_candidates,
                    "compiled_wiki_raw_pointer": raw_candidates,
                },
                selected=selection.candidates,
                chunks_by_candidate=chunks_by_candidate,
                transitions=transitions,
                stage_latencies_ms={"compiled_wiki": latency},
                route=None,
                route_evidence={},
                context_token_budget=budget,
            )
        )
    return traces


def oracle_route_for_task(task: Mapping[str, Any]) -> str:
    """Map authored task labels to an analysis-only route without using gold."""
    dependency = str(task.get("structured_data_dependency", "none"))
    family = str(task.get("task_family", ""))
    answer_type = str(task.get("answer_type", ""))
    if dependency == "none":
        return oracle_route("prose")
    if (
        dependency == "required"
        and family not in _ORACLE_COMBINED_FAMILIES
        and answer_type
        in {
            "calculation",
            "list",
            "short_text",
            "structured_table",
        }
    ):
        return oracle_route("structured")
    return oracle_route("structured_plus_prose")


def build_oracle_route_traces(
    tasks: Sequence[Mapping[str, Any]],
    deployable_traces: Sequence[Mapping[str, Any]],
    chunks: Sequence[Mapping[str, Any]],
    structured_chunks: Sequence[Mapping[str, Any]],
    protocol: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Build label-only oracle contexts before any evaluator gold is loaded."""
    validate_protocol(protocol)
    budget = int(protocol["fixed_comparison"]["context_token_budget"])
    rrf_constant = int(protocol["methods"]["R2"]["rrf_constant"])
    chunks_by_candidate, _ = _passage_maps([*chunks, *structured_chunks])
    by_cell = {
        (str(trace["task"]["task_id"]), str(trace["strategy_id"])): trace
        for trace in deployable_traces
    }
    expected = {
        (str(task["task_id"]), method) for task in tasks for method in ("R5", "R7")
    }
    if not expected.issubset(by_cell):
        raise ExperimentError("oracle router lacks frozen R5 or R7 candidate traces")

    oracle_traces: list[dict[str, Any]] = []
    for task in tasks:
        task_id = str(task["task_id"])
        prose = list(by_cell[(task_id, "R5")]["selected_candidates"])
        structured = list(
            by_cell[(task_id, "R7")]["retrieval_stages"]["sql_structured"]
        )
        combined = list(
            rrf_fuse(
                {"prose": prose, "structured": structured},
                constant=rrf_constant,
            ).candidates
        )
        route = oracle_route_for_task(task)
        candidates = {
            "prose": prose,
            "structured": structured,
            "combined": combined,
        }[route]
        selection = _pack_ranked(
            candidates,
            chunks_by_candidate,
            token_budget=budget,
            final_k=None,
        )
        rendered = _render_context(selection.candidates, chunks_by_candidate)
        context_tokens = estimate_tokens(rendered)
        if context_tokens > budget:
            raise ExperimentError("oracle route exceeds the fixed context budget")
        passages = {
            str(candidate["candidate_id"]): str(
                chunks_by_candidate[str(candidate["candidate_id"])].get("text", "")
            )
            for candidate in selection.candidates
        }
        oracle_traces.append(
            {
                "schema_version": "contextlab.oracle-router-trace.v1",
                "task_id": task_id,
                "route": route,
                "router_version": ORACLE_ROUTER_VERSION,
                "label_fields": [
                    "structured_data_dependency",
                    "task_family",
                    "answer_type",
                ],
                "selected_candidates": [dict(row) for row in selection.candidates],
                "candidate_passages": passages,
                "candidate_tokens": sum(
                    estimate_tokens(
                        render_evidence_block(
                            chunks_by_candidate[str(row["candidate_id"])]
                        )
                    )
                    for row in candidates
                    if str(row["candidate_id"]) in chunks_by_candidate
                ),
                "context_tokens": context_tokens,
                "rendered_context": rendered,
                "rendered_context_sha256": hashlib.sha256(
                    rendered.encode("utf-8")
                ).hexdigest(),
                "retrieval_latency_ms": float(
                    by_cell[(task_id, "R7")]["retrieval_latency_ms"]
                ),
                "retrieval_cost_usd": "0",
            }
        )
    return oracle_traces


def score_oracle_router(
    oracle_traces: Sequence[Mapping[str, Any]],
    relevant_refs: Mapping[str, Iterable[str]],
    deployable_scored_traces: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Score frozen oracle contexts and compare them with the deployable R7 rules."""
    rules = {
        str(trace["task"]["task_id"]): trace["component_metrics"]
        for trace in deployable_scored_traces
        if trace.get("strategy_id") == "R7"
    }
    rows: list[dict[str, Any]] = []
    for trace in oracle_traces:
        task_id = str(trace["task_id"])
        if task_id not in relevant_refs or task_id not in rules:
            raise ExperimentError("oracle router scorer coverage is incomplete")
        metrics = score_trace(trace, relevant_refs[task_id])
        rows.append(
            {
                "task_id": task_id,
                "route": trace["route"],
                "rendered_context_sha256": trace["rendered_context_sha256"],
                "metrics": metrics,
            }
        )
    metric_names = (
        "recall_at_k",
        "precision_at_k",
        "reciprocal_rank",
        "ndcg",
        "required_source_coverage",
        "context_recall",
        "context_required_source_coverage",
    )
    oracle_means = {
        metric: sum(float(row["metrics"][metric]) for row in rows) / len(rows)
        for metric in metric_names
    }
    rule_means = {
        metric: sum(float(rules[row["task_id"]][metric]) for row in rows) / len(rows)
        for metric in metric_names
    }
    return {
        "schema_version": "contextlab.oracle-router-analysis.v1",
        "router_version": ORACLE_ROUTER_VERSION,
        "deployable": False,
        "task_count": len(rows),
        "route_distribution": dict(
            sorted(Counter(str(row["route"]) for row in rows).items())
        ),
        "oracle_means": oracle_means,
        "rules_r7_means": rule_means,
        "oracle_minus_rules": {
            metric: oracle_means[metric] - rule_means[metric] for metric in metric_names
        },
        "rows": rows,
    }


def score_trace(
    trace: Mapping[str, Any], relevant_refs: Iterable[str]
) -> dict[str, Any]:
    """Offline scorer boundary: add gold-reference metrics after retrieval is frozen."""
    candidates = trace.get("selected_candidates")
    passages = trace.get("candidate_passages")
    if not isinstance(candidates, list) or not isinstance(passages, Mapping):
        raise ExperimentError("retrieval trace lacks candidates or passages")
    refs = tuple(map(str, relevant_refs))
    required_sources = {ref.split("-S", 1)[0] if "-S" in ref else ref for ref in refs}
    primary = retrieval_metrics(
        candidates,
        relevant_evidence_refs=refs,
        required_source_ids=required_sources,
        k=8,
        candidate_texts={str(key): str(value) for key, value in passages.items()},
        context_texts=[str(trace.get("rendered_context", ""))],
    )
    context = retrieval_metrics(
        candidates,
        relevant_evidence_refs=refs,
        required_source_ids=required_sources,
        k=len(candidates),
    )
    return {
        **primary,
        "candidate_tokens": int(trace["candidate_tokens"]),
        "context_tokens": int(trace["context_tokens"]),
        "context_recall": context["recall_at_k"],
        "context_required_source_coverage": context["required_source_coverage"],
        "retrieval_latency_ms": float(trace["retrieval_latency_ms"]),
        "retrieval_cost_usd": str(trace["retrieval_cost_usd"]),
    }


def score_public_traces(
    traces: Iterable[Mapping[str, Any]], root: Path | None = None
) -> list[dict[str, Any]]:
    """Load evaluator-only gold only after strategy traces already exist."""
    gold = refs_by_task(load_public_gold(root))
    scored: list[dict[str, Any]] = []
    for trace in traces:
        task_id = str(trace.get("task", {}).get("task_id", ""))
        if task_id not in gold:
            raise ExperimentError(f"public trace has no scorer gold: {task_id}")
        scored.append(
            {**dict(trace), "component_metrics": score_trace(trace, gold[task_id])}
        )
    return scored


def summarize_compiled_wiki_control(
    wiki_scored_traces: Sequence[Mapping[str, Any]],
    deployable_scored_traces: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compare the PageIndex-style route with the fixed compiled wiki control."""
    r6 = {
        str(trace["task"]["task_id"]): trace["component_metrics"]
        for trace in deployable_scored_traces
        if trace.get("strategy_id") == "R6"
    }
    wiki = {
        str(trace["task"]["task_id"]): trace["component_metrics"]
        for trace in wiki_scored_traces
    }
    if not wiki or set(wiki) != set(r6):
        raise ExperimentError("compiled wiki and R6 task coverage differ")
    metric_names = (
        "recall_at_k",
        "precision_at_k",
        "reciprocal_rank",
        "ndcg",
        "required_source_coverage",
        "context_recall",
        "context_required_source_coverage",
        "candidate_tokens",
        "context_tokens",
        "retrieval_latency_ms",
    )
    wiki_means = {
        metric: sum(float(metrics[metric]) for metrics in wiki.values()) / len(wiki)
        for metric in metric_names
    }
    r6_means = {
        metric: sum(float(metrics[metric]) for metrics in r6.values()) / len(r6)
        for metric in metric_names
    }
    return {
        "schema_version": "contextlab.compiled-wiki-control-analysis.v1",
        "nodes_sha256": FROZEN_WIKI_NODES_SHA256,
        "task_count": len(wiki),
        "wiki_means": wiki_means,
        "r6_means": r6_means,
        "r6_minus_wiki": {
            metric: r6_means[metric] - wiki_means[metric] for metric in metric_names
        },
        "rows": [
            {
                "task_id": task_id,
                "wiki_metrics": wiki[task_id],
                "r6_metrics": r6[task_id],
            }
            for task_id in sorted(wiki)
        ],
    }


def run_identifier_mask_audit(
    tasks: Sequence[Mapping[str, Any]],
    chunks: Sequence[Mapping[str, Any]],
    protocol: Mapping[str, Any],
    gold_refs: Mapping[str, Iterable[str]],
) -> dict[str, Any]:
    """Repeat BM25 with all NL source/section identifiers removed from its index."""
    comparison = protocol["fixed_comparison"]
    method = protocol["methods"]["R1"]
    chunks_by_candidate, passage_texts = _passage_maps(chunks)
    rows: list[dict[str, Any]] = []
    for task in tasks:
        safe_task = prompt_safe_task(dict(task))
        candidates = bm25_retrieve(
            safe_task["question_text"],
            chunks,
            limit=int(comparison["candidate_limit"]),
            k1=float(method["bm25_k1"]),
            b=float(method["bm25_b"]),
            stage="bm25_source_ids_masked",
            include_source_identifiers=False,
        )
        selected = _pack_ranked(
            candidates,
            chunks_by_candidate,
            token_budget=int(comparison["context_token_budget"]),
            final_k=int(comparison["final_k"]),
        )
        refs = tuple(map(str, gold_refs[str(task["task_id"])]))
        sources = {ref.split("-S", 1)[0] if "-S" in ref else ref for ref in refs}
        metrics = retrieval_metrics(
            selected.candidates,
            relevant_evidence_refs=refs,
            required_source_ids=sources,
            k=int(comparison["final_k"]),
            candidate_texts=passage_texts,
            context_texts=[_render_context(selected.candidates, chunks_by_candidate)],
        )
        rows.append(
            {
                "task_id": task["task_id"],
                "task_family": task["task_family"],
                "metrics": metrics,
            }
        )
    metric_names = (
        "recall_at_k",
        "precision_at_k",
        "reciprocal_rank",
        "ndcg",
        "required_source_coverage",
    )
    means = {
        metric: sum(float(row["metrics"][metric]) for row in rows) / len(rows)
        for metric in metric_names
    }
    return {
        "schema_version": "contextlab.identifier-mask-audit.v1",
        "method": "R1",
        "task_count": len(rows),
        "mask": "remove NL source, section, and chunk identifiers from query and index text",
        "means": means,
        "rows": rows,
    }


def aggregate_component_metrics(
    scored_traces: Iterable[Mapping[str, Any]], tasks: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    task_metadata = {str(row["task_id"]): row for row in tasks}
    rows = list(scored_traces)
    expected_cells = len(task_metadata) * len(METHOD_IDS)
    if len(rows) != expected_cells:
        raise ExperimentError(
            f"component lab requires {expected_cells} cells, found {len(rows)}"
        )
    by_method: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_method_family: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        strategy = str(row["strategy_id"])
        task_id = str(row["task"]["task_id"])
        family = str(task_metadata[task_id]["task_family"])
        by_method[strategy].append(row)
        by_method_family[(strategy, family)].append(row)

    metric_names = (
        "recall_at_k",
        "precision_at_k",
        "reciprocal_rank",
        "ndcg",
        "required_source_coverage",
        "source_diversity",
        "candidate_tokens",
        "context_tokens",
        "context_recall",
        "context_required_source_coverage",
        "retrieval_latency_ms",
    )

    def means(values: Sequence[Mapping[str, Any]]) -> dict[str, float]:
        return {
            metric: sum(float(row["component_metrics"][metric]) for row in values)
            / len(values)
            for metric in metric_names
        }

    return {
        "cell_count": len(rows),
        "method_count": len(by_method),
        "task_count": len(task_metadata),
        "by_method": {
            method: {"n": len(values), "means": means(values)}
            for method, values in sorted(by_method.items())
        },
        "by_method_and_family": {
            f"{method}:{family}": {"n": len(values), "means": means(values)}
            for (method, family), values in sorted(by_method_family.items())
        },
    }


def build_public_component_lab(
    embeddings: Mapping[str, Sequence[float]], root: Path | None = None
) -> dict[str, Any]:
    """Run and score all 84 public tasks across R0-R7 with no paid generation."""
    root = (root or repository_root()).resolve()
    protocol = load_protocol(root)
    chunks = load_frozen_chunks(root)
    tasks = public_static_tasks(root)
    prompt_tasks = [prompt_safe_task(task) for task in tasks]
    required = required_embedding_keys(tasks, chunks)
    missing = [
        key for group in required.values() for key in group if key not in embeddings
    ]
    if missing:
        raise ExperimentError(
            f"embedding map is missing {len(missing)} required vectors"
        )
    validate_embedding_map(
        {key: embeddings[key] for group in required.values() for key in group}
    )
    structured_chunks = load_structured_chunks(root)
    wiki_nodes = load_compiled_wiki_nodes(root)
    traces: list[dict[str, Any]] = []
    for task in prompt_tasks:
        traces.extend(
            run_task_ladder(task, chunks, embeddings, structured_chunks, protocol)
        )
    oracle_traces = build_oracle_route_traces(
        tasks, traces, chunks, structured_chunks, protocol
    )
    wiki_traces = build_compiled_wiki_traces(prompt_tasks, chunks, wiki_nodes, protocol)
    scored = score_public_traces(traces, root)
    wiki_scored = score_public_traces(wiki_traces, root)
    aggregate = aggregate_component_metrics(scored, tasks)
    gold_refs = refs_by_task(load_public_gold(root))
    oracle_router_analysis = score_oracle_router(oracle_traces, gold_refs, scored)
    compiled_wiki_control = summarize_compiled_wiki_control(wiki_scored, scored)
    identifier_mask_audit = run_identifier_mask_audit(
        tasks, chunks, protocol, gold_refs
    )
    leakage_audit = lexical_leakage_audit(tasks, gold_refs)
    payload: dict[str, Any] = {
        "schema_version": LAB_SCHEMA,
        "scope": "public_static_component_metrics",
        "protocol_sha256": sha256_json(protocol),
        "corpus_snapshot_id": FROZEN_RAW_CHUNKS_SHA256,
        "task_count": len(tasks),
        "method_count": len(METHOD_IDS),
        "cell_count": len(scored),
        "aggregate": aggregate,
        "identifier_mask_audit": identifier_mask_audit,
        "question_reference_leakage_audit": leakage_audit,
        "oracle_router_analysis": oracle_router_analysis,
        "compiled_wiki_control": compiled_wiki_control,
        "traces": scored,
    }
    payload["artifact_sha256"] = sha256_json(payload)
    return payload


def write_public_component_lab(
    embeddings: Mapping[str, Sequence[float]],
    root: Path | None = None,
    output: Path | None = None,
) -> dict[str, Any]:
    root = (root or repository_root()).resolve()
    payload = build_public_component_lab(embeddings, root)
    destination = output or root / "results/v2/retrieval/public_component_lab.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return payload


def lexical_leakage_audit(
    tasks: Iterable[Mapping[str, Any]], gold_refs: Mapping[str, Iterable[str]]
) -> dict[str, Any]:
    """Check whether public questions reveal evaluator-only source or section identifiers."""
    rows = list(tasks)
    findings: list[dict[str, str]] = []
    for task in rows:
        task_id = str(task["task_id"])
        question = str(task.get("question_text", "")).lower()
        for reference in gold_refs.get(task_id, ()):
            if str(reference).lower() in question:
                findings.append({"task_id": task_id, "reference": str(reference)})
    return {
        "task_count": len(rows),
        "leaked_reference_count": len(findings),
        "findings": findings,
        "status": "passed" if not findings else "failed",
    }


def method_cell_index(
    scored_traces: Iterable[Mapping[str, Any]], metric: str
) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = defaultdict(dict)
    for row in scored_traces:
        value = row.get("component_metrics", {}).get(metric)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ExperimentError(f"trace contains invalid metric: {metric}")
        result[str(row["strategy_id"])][str(row["task"]["task_id"])] = float(value)
    return dict(result)


def task_family_index(tasks: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    return {str(task["task_id"]): str(task["task_family"]) for task in tasks}


def route_distribution(traces: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    return dict(
        sorted(
            Counter(
                str(row["route"]) for row in traces if row.get("strategy_id") == "R7"
            ).items()
        )
    )
