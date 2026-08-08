"""Deterministic, evidence-bound grading for public G3 static answers.

The scorer is the only boundary that loads the protected, non-sealed static
answer key.  Persisted evidence contains hashes and objective measurements,
never expected-answer text or scoring notes.  The sealed S081-S116 surface is
rejected before any scorer source is loaded.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .answer_metrics import score_generated_answer
from .baseline import repository_root
from .g3_evidence import render_selected_context, trace_corpus_evidence
from .g3_execution import validate_prepared_public_g3_cell
from .generations import validate_saved_generation_result
from .memory_experiments import G3_ANSWER_GRADE_SCHEMA
from .static_benchmark import (
    FROZEN_RAW_CHUNKS_SHA256,
    load_public_gold,
    public_static_tasks,
)
from .tasking import FORBIDDEN_PUBLIC_FIELDS, sha256_json

STATIC_OBJECTIVE_EVIDENCE_SCHEMA = "contextlab.g3-public-static-objective-evidence.v1"
STATIC_OBJECTIVE_GRADE_BASIS = "public-static-objective-v1"
STATIC_OBJECTIVE_GRADER_ID = "deterministic"

PUBLIC_STATIC_TASK_IDS = tuple(
    f"S{number:03d}" for number in (*range(1, 81), *range(117, 121))
)
_PUBLIC_STATIC_TASK_ID_SET = frozenset(PUBLIC_STATIC_TASK_IDS)

# These sources are frozen experiment inputs.  Raw file commitments make a
# changed public task or protected public label fail closed, even if a caller
# also recomputes a row hash.
CANONICAL_STATIC_FREEZE_MANIFEST_SHA256 = (
    "f6262cb663a186a03533325659d5f47b4d19acb6d907f66e980db6ba3f4d57e4"
)
CANONICAL_STATIC_R0_LAB_ARTIFACT_SHA256 = (
    "8955848dc438931351dcf5edaaed6ecdf821851eab5c025e7924480810d61a5a"
)
CANONICAL_STATIC_R0_LAB_FILE_SHA256 = (
    "6b59f8660d3185c5ee0cdf878c1e0703210e2acf5732f3894bea405f680eba20"
)

_SOURCE_FILE_SHA256 = {
    "evaluation/questions.jsonl": (
        "eb85f9cc1a3a5cf9b4dc0369fb65127b82866c10717672312d936d8373c4f704"
    ),
    "evaluation/v2/tasks/v1_annotated.jsonl": (
        "8c4c162d328309173d70cf99ce375f997755f5b5dfd1a86b9f44ddb5dcb64b68"
    ),
    "evaluation/v2/tasks/static_new_g1.jsonl": (
        "0c29780333a86f8923738b8f8e08e249955d9dc1fd73b4f59abdc089abf48e8c"
    ),
    "evaluation/v2/tasks/static_completion_g2.jsonl": (
        "abc1e76875611cf85260b271fbf60100a7ae68f6f52d44425a2cf5c300dabfb1"
    ),
    "novalearn_synthetic_corpus/evaluation_only_do_not_index/v2/"
    "static_new_g1_gold.jsonl": (
        "b8e4a39f7e69d0a5a73096653c22af9591b613ded2886980a71f84673adc3f04"
    ),
    "novalearn_synthetic_corpus/evaluation_only_do_not_index/v2/"
    "static_completion_g2_gold.jsonl": (
        "cb496d284b2e64fc05cb1db38fd0be38bc52665da257a9091b74784d5ad371ad"
    ),
}

_STATIC_LAB_PATH = "results/v2/retrieval/public_component_lab.json"
_STATIC_FREEZE_PATH = "results/v2/splits/static_g2_freeze.json"
_STATIC_REF = re.compile(r"NL-\d{3}#NL-\d{3}-S\d{2}\Z")
_STATIC_RAW_ID = re.compile(r"NL-\d{3}-S\d{2}\Z")
_EXACT_CITATION = re.compile(r"\[(NL-\d{3}#NL-\d{3}-S\d{2})\]")
_EVIDENCE_LIKE_CITATION = re.compile(r"\[(NL-[^\]\n]{1,200})\]")
_FOOTER_STATUS = re.compile(r"ANSWER_STATUS: (answer|abstain)\Z")
_FORBIDDEN_RESULT_KEYS = frozenset(FORBIDDEN_PUBLIC_FIELDS) | {
    "answer_key",
    "expected_answers",
    "gold",
    "gold_row",
}


class G3StaticGradingError(ValueError):
    """A public static grade cannot be reproduced from canonical evidence."""


@dataclass(frozen=True)
class _SourceBinding:
    task_path: str
    task_file_sha256: str
    gold_path: str
    gold_file_sha256: str


@dataclass(frozen=True)
class _StaticCatalog:
    tasks: Mapping[str, Mapping[str, Any]]
    gold: Mapping[str, Mapping[str, Any]]
    bindings: Mapping[str, _SourceBinding]


@dataclass(frozen=True)
class _Footer:
    body: str
    answer_status: str | None
    citations: tuple[str, ...]
    malformed_evidence_citations: tuple[str, ...]
    footer_sha256: str
    footer_exact: bool
    used_memory_claims_none: bool


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_bytes(path: Path, label: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise G3StaticGradingError(f"cannot read canonical {label}") from exc


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(_read_bytes(path, label).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G3StaticGradingError(f"canonical {label} is invalid") from exc
    if not isinstance(value, dict):
        raise G3StaticGradingError(f"canonical {label} must be an object")
    return value


def _reject_protected_fields(value: object, label: str) -> None:
    """Reject evaluator-only fields crossing into a prompt, trace, or result."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key).casefold()
            if key_text in _FORBIDDEN_RESULT_KEYS:
                raise G3StaticGradingError(
                    f"{label} contains a protected evaluator field"
                )
            _reject_protected_fields(item, label)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            _reject_protected_fields(item, label)


def _require_public_static_task_id(task_id: object) -> str:
    if not isinstance(task_id, str) or task_id not in _PUBLIC_STATIC_TASK_ID_SET:
        raise G3StaticGradingError(
            "static grader requires a known public non-sealed task"
        )
    return task_id


def _source_paths(task_id: str) -> tuple[str, str]:
    number = int(task_id[1:])
    if number <= 40:
        return (
            "evaluation/v2/tasks/v1_annotated.jsonl",
            "evaluation/questions.jsonl",
        )
    if number <= 80:
        return (
            "evaluation/v2/tasks/static_new_g1.jsonl",
            "novalearn_synthetic_corpus/evaluation_only_do_not_index/v2/"
            "static_new_g1_gold.jsonl",
        )
    return (
        "evaluation/v2/tasks/static_completion_g2.jsonl",
        "novalearn_synthetic_corpus/evaluation_only_do_not_index/v2/"
        "static_completion_g2_gold.jsonl",
    )


def _verify_source_files(root: Path) -> None:
    for relative, expected in _SOURCE_FILE_SHA256.items():
        actual = _sha256_bytes(_read_bytes(root / relative, "static source"))
        if actual != expected:
            raise G3StaticGradingError(
                "canonical public static task or gold source hash changed"
            )


def _validate_gold_row(row: Mapping[str, Any], task_id: str) -> None:
    if (
        set(row)
        != {
            "schema_version",
            "task_id",
            "expected_answer",
            "required_evidence",
            "scoring_notes",
        }
        or row.get("schema_version") != "contextlab.gold-task.v1"
        or row.get("task_id") != task_id
        or not isinstance(row.get("expected_answer"), str)
        or not str(row["expected_answer"]).strip()
        or not isinstance(row.get("scoring_notes"), str)
        or not str(row["scoring_notes"]).strip()
        or not isinstance(row.get("required_evidence"), list)
        or not row["required_evidence"]
        or any(
            not isinstance(reference, str) or not reference
            for reference in row["required_evidence"]
        )
    ):
        raise G3StaticGradingError("canonical protected public gold row is invalid")


@lru_cache(maxsize=4)
def _canonical_catalog(root_text: str) -> _StaticCatalog:
    root = Path(root_text)
    _verify_source_files(root)
    freeze = _load_json(root / _STATIC_FREEZE_PATH, "static freeze")
    if freeze.get("manifest_sha256") != CANONICAL_STATIC_FREEZE_MANIFEST_SHA256:
        raise G3StaticGradingError("canonical public static freeze changed")
    try:
        task_rows = public_static_tasks(root)
        gold_rows = load_public_gold(root)
    except ValueError as exc:
        raise G3StaticGradingError(
            "canonical public static task or gold catalog is invalid"
        ) from exc
    tasks = {str(row.get("task_id")): row for row in task_rows}
    gold = {str(row.get("task_id")): row for row in gold_rows}
    expected_ids = set(PUBLIC_STATIC_TASK_IDS)
    if (
        len(task_rows) != len(PUBLIC_STATIC_TASK_IDS)
        or len(gold_rows) != len(PUBLIC_STATIC_TASK_IDS)
        or set(tasks) != expected_ids
        or len(tasks) != len(PUBLIC_STATIC_TASK_IDS)
        or set(gold) != expected_ids
        or len(gold) != len(PUBLIC_STATIC_TASK_IDS)
    ):
        raise G3StaticGradingError(
            "canonical public static catalog must contain exactly 84 tasks"
        )
    bindings: dict[str, _SourceBinding] = {}
    for task_id in PUBLIC_STATIC_TASK_IDS:
        task = tasks[task_id]
        gold_row = gold[task_id]
        _validate_gold_row(gold_row, task_id)
        if (
            task.get("suite") != "static"
            or task.get("question_status") != "frozen_public"
            or task.get("sealed_eligible") is not False
            or task.get("required_evidence") != gold_row.get("required_evidence")
        ):
            raise G3StaticGradingError(
                "canonical public static task and protected gold differ"
            )
        task_path, gold_path = _source_paths(task_id)
        bindings[task_id] = _SourceBinding(
            task_path=task_path,
            task_file_sha256=_SOURCE_FILE_SHA256[task_path],
            gold_path=gold_path,
            gold_file_sha256=_SOURCE_FILE_SHA256[gold_path],
        )
    return _StaticCatalog(tasks=tasks, gold=gold, bindings=bindings)


@lru_cache(maxsize=4)
def _canonical_r0_traces(root_text: str) -> Mapping[str, Mapping[str, Any]]:
    root = Path(root_text)
    raw = _read_bytes(root / _STATIC_LAB_PATH, "static R0 lab")
    if _sha256_bytes(raw) != CANONICAL_STATIC_R0_LAB_FILE_SHA256:
        raise G3StaticGradingError("canonical public static R0 lab source changed")
    try:
        lab = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G3StaticGradingError("canonical public static R0 lab is invalid") from exc
    if (
        not isinstance(lab, Mapping)
        or lab.get("artifact_sha256") != CANONICAL_STATIC_R0_LAB_ARTIFACT_SHA256
        or not isinstance(lab.get("traces"), list)
    ):
        raise G3StaticGradingError("canonical public static R0 lab changed")
    rows: dict[str, Mapping[str, Any]] = {}
    for trace in lab["traces"]:
        if not isinstance(trace, Mapping) or trace.get("strategy_id") != "R0":
            continue
        task = trace.get("task")
        if not isinstance(task, Mapping):
            raise G3StaticGradingError("canonical R0 trace task is invalid")
        task_id = _require_public_static_task_id(task.get("task_id"))
        if task_id in rows:
            raise G3StaticGradingError("canonical R0 lab repeats a public task")
        _reject_protected_fields(trace, "canonical R0 trace")
        rows[task_id] = trace
    if tuple(sorted(rows)) != PUBLIC_STATIC_TASK_IDS:
        raise G3StaticGradingError(
            "canonical R0 lab does not contain the exact 84 public tasks"
        )
    return rows


def _canonical_task_and_gold(
    task_id: str, root: Path
) -> tuple[Mapping[str, Any], Mapping[str, Any], _SourceBinding]:
    catalog = _canonical_catalog(str(root))
    return catalog.tasks[task_id], catalog.gold[task_id], catalog.bindings[task_id]


def _validate_prepared_static_cell(
    prepared_cell: Mapping[str, Any], root: Path
) -> tuple[
    Mapping[str, Any],
    Mapping[str, Any],
    Mapping[str, Any],
    Mapping[str, Any],
    _SourceBinding,
    Mapping[str, str],
]:
    try:
        validate_prepared_public_g3_cell(prepared_cell, root=root)
    except ValueError as exc:
        raise G3StaticGradingError("prepared public G3 cell is invalid") from exc
    _reject_protected_fields(prepared_cell, "prepared public G3 cell")
    spec = prepared_cell.get("run_spec")
    trace = prepared_cell.get("memory_trace")
    if not isinstance(spec, Mapping) or not isinstance(trace, Mapping):
        raise G3StaticGradingError("prepared public G3 cell is incomplete")
    task = spec.get("task")
    if not isinstance(task, Mapping) or task.get("suite") != "static":
        raise G3StaticGradingError("static grader received a non-static G3 cell")
    task_id = _require_public_static_task_id(task.get("task_id"))
    canonical_task, gold, binding = _canonical_task_and_gold(task_id, root)
    expected_task = {
        "task_id": task_id,
        "suite": "static",
        "task_family": canonical_task["task_family"],
        "question_text": canonical_task["question_text"],
        "question_sha256": canonical_task["question_sha256"],
    }
    if dict(task) != expected_task or trace.get("task") != expected_task:
        raise G3StaticGradingError(
            "prepared static task differs from the canonical public task"
        )
    if (
        prepared_cell.get("source_r0_lab_sha256")
        != CANONICAL_STATIC_R0_LAB_ARTIFACT_SHA256
        or prepared_cell.get("observable_event_ids") != []
        or prepared_cell.get("memory_read_status") != "not_applicable_static"
        or prepared_cell.get("memory_read") is not None
        or trace.get("selected_memory_evidence") != []
        or trace.get("selected_episode_evidence") != []
    ):
        raise G3StaticGradingError(
            "prepared static cell crossed the public static evidence boundary"
        )

    source_trace = _canonical_r0_traces(str(root))[task_id]
    if (
        prepared_cell.get("source_r0_trace_id") != source_trace.get("run_id")
        or prepared_cell.get("source_r0_trace_sha256") != sha256_json(source_trace)
        or source_trace.get("task") != prepared_cell["generation_spec"].get("task")
    ):
        raise G3StaticGradingError(
            "prepared static cell points to an unknown or changed R0 source trace"
        )
    try:
        source_rows, corpus_blocks = trace_corpus_evidence(source_trace)
        rendered = render_selected_context(
            trace,
            corpus_blocks=corpus_blocks,
            memory_blocks={},
            episode_blocks={},
        )
    except ValueError as exc:
        raise G3StaticGradingError(
            "prepared static cell cannot resolve canonical raw evidence"
        ) from exc
    if (
        trace.get("corpus_candidate_evidence") != source_rows
        or prepared_cell.get("rendered_context") != rendered
    ):
        raise G3StaticGradingError(
            "prepared static context differs from canonical raw evidence"
        )
    return spec, trace, canonical_task, gold, binding, corpus_blocks


def _validate_saved_generation(
    generation_result: Mapping[str, Any],
    *,
    spec: Mapping[str, Any],
    saved_generation_result_sha256: str,
) -> str:
    if not _is_sha256(saved_generation_result_sha256):
        raise G3StaticGradingError(
            "saved generation result requires a SHA-256 commitment"
        )
    if (
        not isinstance(generation_result, Mapping)
        or set(generation_result)
        != {"schema_version", "run_id", "task_id", "answer", "metadata"}
        or sha256_json(generation_result) != saved_generation_result_sha256
    ):
        raise G3StaticGradingError("saved generation result envelope or hash changed")
    _reject_protected_fields(generation_result, "saved generation result")
    task = spec["task"]
    try:
        validate_saved_generation_result(
            generation_result,
            expected_run_id=str(spec["run_id"]),
            expected_task_id=str(task["task_id"]),
            expected_effort=str(spec["reasoning_effort"]),
        )
    except ValueError as exc:
        raise G3StaticGradingError("saved generation result is invalid") from exc
    metadata = generation_result.get("metadata")
    if not isinstance(metadata, Mapping) or metadata.get("requested_model") != spec.get(
        "requested_model"
    ):
        raise G3StaticGradingError(
            "saved generation result differs from the prepared route"
        )
    answer = generation_result.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise G3StaticGradingError("saved generation answer is empty")
    return answer


def _parse_static_footer(answer: str) -> _Footer:
    text = answer[:-1] if answer.endswith("\n") else answer
    lines = text.split("\n")
    body_lines = list(lines)
    claims_line = (
        body_lines.pop()
        if body_lines and body_lines[-1].startswith("USED_MEMORY_CLAIMS:")
        else None
    )
    status_line = (
        body_lines.pop()
        if body_lines and body_lines[-1].startswith("ANSWER_STATUS:")
        else None
    )
    body = "\n".join(body_lines).strip()
    if not body:
        raise G3StaticGradingError("static answer body is empty")
    ambiguous_footer = (
        re.search(r"(?m)^(?:ANSWER_STATUS|USED_MEMORY_CLAIMS):", body) is not None
    )
    loose_status = (
        re.fullmatch(r"ANSWER_STATUS:\s*(answer|abstain)\s*", status_line)
        if status_line is not None
        else None
    )
    used_memory_claims_none = claims_line == "USED_MEMORY_CLAIMS: none"
    footer_exact = (
        "\r" not in answer
        and not answer.endswith((" ", "\t"))
        and status_line is not None
        and _FOOTER_STATUS.fullmatch(status_line) is not None
        and used_memory_claims_none
        and not ambiguous_footer
    )
    citations = tuple(_EXACT_CITATION.findall(body))
    evidence_like = tuple(_EVIDENCE_LIKE_CITATION.findall(body))
    malformed = tuple(
        reference
        for reference in evidence_like
        if _STATIC_REF.fullmatch(reference) is None
    )
    footer_text = (
        f"{status_line or '<missing-answer-status>'}\n"
        f"{claims_line or '<missing-memory-claims>'}"
    )
    return _Footer(
        body=body,
        answer_status=loose_status.group(1) if loose_status is not None else None,
        citations=citations,
        malformed_evidence_citations=malformed,
        footer_sha256=hashlib.sha256(footer_text.encode("utf-8")).hexdigest(),
        footer_exact=footer_exact,
        used_memory_claims_none=used_memory_claims_none,
    )


def _selected_context_references(
    trace: Mapping[str, Any], corpus_blocks: Mapping[str, str]
) -> tuple[dict[str, str], list[str]]:
    selected = trace.get("selected_corpus_evidence")
    if not isinstance(selected, list) or not selected:
        raise G3StaticGradingError("static trace has no selected corpus evidence")
    reference_to_raw: dict[str, str] = {}
    selected_raw_ids: list[str] = []
    for row in selected:
        if not isinstance(row, Mapping):
            raise G3StaticGradingError("static trace selected evidence is invalid")
        evidence_id = row.get("evidence_id")
        raw_ids = row.get("raw_evidence_ids")
        if (
            not isinstance(evidence_id, str)
            or not isinstance(raw_ids, list)
            or len(raw_ids) != 1
            or not isinstance(raw_ids[0], str)
            or _STATIC_RAW_ID.fullmatch(raw_ids[0]) is None
        ):
            raise G3StaticGradingError(
                "static trace selected evidence lacks exact raw provenance"
            )
        raw_id = raw_ids[0]
        block = corpus_blocks.get(evidence_id)
        if not isinstance(block, str):
            raise G3StaticGradingError(
                "static trace selected evidence cannot resolve its raw block"
            )
        first_line = block.splitlines()[0] if block.splitlines() else ""
        expected_reference = f"{raw_id[:6]}#{raw_id}"
        if first_line != f"[{expected_reference}]":
            raise G3StaticGradingError(
                "static raw evidence reference differs from its canonical block"
            )
        reference_to_raw[expected_reference] = raw_id
        selected_raw_ids.append(raw_id)
    return reference_to_raw, selected_raw_ids


def _required_reference_matches(raw_id: str, required: str) -> bool:
    return required == raw_id or required == raw_id[:6]


def _build_evidence(
    prepared_cell: Mapping[str, Any],
    generation_result: Mapping[str, Any],
    *,
    saved_generation_result_sha256: str,
    root: Path,
) -> tuple[dict[str, Any], Mapping[str, Any], Mapping[str, Any], str]:
    spec, trace, task, gold, binding, corpus_blocks = _validate_prepared_static_cell(
        prepared_cell, root
    )
    answer = _validate_saved_generation(
        generation_result,
        spec=spec,
        saved_generation_result_sha256=saved_generation_result_sha256,
    )
    footer = _parse_static_footer(answer)
    reference_to_raw, selected_raw_ids = _selected_context_references(
        trace, corpus_blocks
    )
    cited_raw_ids = [
        reference_to_raw[reference]
        for reference in footer.citations
        if reference in reference_to_raw
    ]
    supported_citation_count = sum(
        reference in reference_to_raw for reference in footer.citations
    )
    citation_count = len(footer.citations) + len(footer.malformed_evidence_citations)
    unsupported_count = citation_count - supported_citation_count
    required = [str(value) for value in gold["required_evidence"]]
    covered_required = {
        required_ref
        for required_ref in required
        if any(
            _required_reference_matches(raw_id, required_ref)
            for raw_id in cited_raw_ids
        )
    }
    exact_citation_precision = (
        supported_citation_count / citation_count if citation_count else 0.0
    )
    required_recall = len(covered_required) / len(required)
    provenance_complete = (
        citation_count > 0 and unsupported_count == 0 and required_recall == 1.0
    )
    screen = score_generated_answer(
        footer.body,
        str(gold["expected_answer"]),
        required,
        selected_raw_ids,
        abstention_task=task["task_family"] == "abstention",
    )
    answer_status = footer.answer_status or (
        "abstain" if screen["abstained"] else "answer"
    )
    expected_status = "abstain" if task["task_family"] == "abstention" else "answer"
    is_correct = bool(
        screen["accepted_proxy"]
        and footer.footer_exact
        and answer_status == expected_status
        and unsupported_count == 0
    )
    objective_checks = {
        "expected_content_token_recall": screen["expected_content_token_recall"],
        "critical_value_recall": screen["critical_value_recall"],
        "citation_count": citation_count,
        "exact_citation_count": len(footer.citations),
        "exact_citation_precision": exact_citation_precision,
        "required_evidence_citation_recall": required_recall,
        "unsupported_citation_count": unsupported_count,
        "abstained": screen["abstained"],
        "abstention_quality": screen["abstention_quality"],
        "screening_proxy_accepted": screen["accepted_proxy"],
        "footer_exact": footer.footer_exact,
        "footer_status_matches_expected": answer_status == expected_status,
        "all_required_raw_evidence_cited": required_recall == 1.0,
        "all_citations_exact_and_in_context": unsupported_count == 0
        and citation_count > 0,
    }
    evidence: dict[str, Any] = {
        "schema_version": STATIC_OBJECTIVE_EVIDENCE_SCHEMA,
        "task_id": task["task_id"],
        "task_family": task["task_family"],
        "prepared_cell_artifact_sha256": prepared_cell["artifact_sha256"],
        "generation_result_sha256": saved_generation_result_sha256,
        "answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
        "trace_sha256": trace["trace_sha256"],
        "static_freeze_manifest_sha256": (CANONICAL_STATIC_FREEZE_MANIFEST_SHA256),
        "public_task_source": {
            "path": binding.task_path,
            "file_sha256": binding.task_file_sha256,
            "row_sha256": sha256_json(task),
            "question_sha256": task["question_sha256"],
        },
        "protected_public_gold_source": {
            "path": binding.gold_path,
            "file_sha256": binding.gold_file_sha256,
            "row_sha256": sha256_json(gold),
        },
        "raw_evidence_provenance": {
            "static_r0_lab_artifact_sha256": (CANONICAL_STATIC_R0_LAB_ARTIFACT_SHA256),
            "source_r0_trace_id": prepared_cell["source_r0_trace_id"],
            "source_r0_trace_sha256": prepared_cell["source_r0_trace_sha256"],
            "frozen_raw_corpus_sha256": FROZEN_RAW_CHUNKS_SHA256,
            "g3_corpus_snapshot_sha256": prepared_cell["corpus_snapshot_sha256"],
            "selected_raw_evidence_ids": sorted(set(selected_raw_ids)),
            "cited_raw_evidence_ids": sorted(set(cited_raw_ids)),
            "exact_citations": list(footer.citations),
            "required_evidence_sha256": sha256_json(sorted(required)),
            "required_evidence_count": len(required),
            "cited_required_evidence_count": len(covered_required),
        },
        "answer_footer": {
            "schema_version": "contextlab.g3-static-answer-footer-check.v1",
            "answer_status": answer_status,
            "used_memory_claim_ids": [],
            "used_memory_claims_none": footer.used_memory_claims_none,
            "footer_exact": footer.footer_exact,
            "footer_sha256": footer.footer_sha256,
        },
        "expected_answer_status": expected_status,
        "objective_checks": objective_checks,
        "provenance_complete": provenance_complete,
        "is_correct": is_correct,
        "scope_notice": (
            "deterministic public-static screening; calibrate separately on the "
            "frozen 20-cell three-member packet"
        ),
    }
    evidence["artifact_sha256"] = sha256_json(evidence)
    _reject_protected_fields(evidence, "static objective evidence")
    return evidence, spec, trace, answer


def build_public_static_grade_evidence(
    prepared_cell: Mapping[str, Any],
    generation_result: Mapping[str, Any],
    *,
    saved_generation_result_sha256: str,
    root: Path | None = None,
) -> dict[str, Any]:
    """Build safe, replayable evidence for one public static G3 answer."""

    resolved_root = (root or repository_root()).resolve()
    evidence, _spec, _trace, _answer = _build_evidence(
        prepared_cell,
        generation_result,
        saved_generation_result_sha256=saved_generation_result_sha256,
        root=resolved_root,
    )
    return evidence


def _grade_from_evidence(
    evidence: Mapping[str, Any],
    spec: Mapping[str, Any],
    trace: Mapping[str, Any],
    answer: str,
) -> dict[str, Any]:
    grade: dict[str, Any] = {
        "schema_version": G3_ANSWER_GRADE_SCHEMA,
        "grade_artifact_id": f"grade-{spec['run_id']}",
        "grader_id": STATIC_OBJECTIVE_GRADER_ID,
        "grade_basis": STATIC_OBJECTIVE_GRADE_BASIS,
        "source_grade_sha256s": [evidence["artifact_sha256"]],
        "run_id": spec["run_id"],
        "task_id": spec["task"]["task_id"],
        "suite": "static",
        "policy": spec["policy"],
        "reasoning_effort": spec["reasoning_effort"],
        "prepared_cell_artifact_sha256": evidence["prepared_cell_artifact_sha256"],
        "generation_result_sha256": evidence["generation_result_sha256"],
        "trace_sha256": trace["trace_sha256"],
        "answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
        "answer_status": evidence["answer_footer"]["answer_status"],
        "expected_answer_status": evidence["expected_answer_status"],
        "is_correct": evidence["is_correct"],
        "stale_answer": False,
        "provenance_complete": evidence["provenance_complete"],
        "used_memory_claims": [],
        "relevant_memory_claim_ids": [],
        "correction_latency": None,
        "correction_latency_unit": "not_applicable",
    }
    grade["artifact_sha256"] = sha256_json(grade)
    _reject_protected_fields(grade, "static answer grade")
    return grade


def build_public_static_grade(
    prepared_cell: Mapping[str, Any],
    generation_result: Mapping[str, Any],
    *,
    saved_generation_result_sha256: str,
    root: Path | None = None,
) -> dict[str, Any]:
    """Build a deterministic static grade in memory receipt field semantics."""

    resolved_root = (root or repository_root()).resolve()
    evidence, spec, trace, answer = _build_evidence(
        prepared_cell,
        generation_result,
        saved_generation_result_sha256=saved_generation_result_sha256,
        root=resolved_root,
    )
    return _grade_from_evidence(evidence, spec, trace, answer)


def validate_public_static_grade_evidence(
    value: Mapping[str, Any],
    prepared_cell: Mapping[str, Any],
    generation_result: Mapping[str, Any],
    *,
    saved_generation_result_sha256: str,
    root: Path | None = None,
) -> None:
    """Rebuild evidence so a self-rehashed tampered artifact is still rejected."""

    expected = build_public_static_grade_evidence(
        prepared_cell,
        generation_result,
        saved_generation_result_sha256=saved_generation_result_sha256,
        root=root,
    )
    if not isinstance(value, Mapping) or dict(value) != expected:
        raise G3StaticGradingError(
            "static objective evidence differs from canonical derivation"
        )


def validate_public_static_grade(
    value: Mapping[str, Any],
    prepared_cell: Mapping[str, Any],
    generation_result: Mapping[str, Any],
    *,
    saved_generation_result_sha256: str,
    root: Path | None = None,
) -> None:
    """Rebuild a static grade and reject changed identities, sources, or outcomes."""

    expected = build_public_static_grade(
        prepared_cell,
        generation_result,
        saved_generation_result_sha256=saved_generation_result_sha256,
        root=root,
    )
    if not isinstance(value, Mapping) or dict(value) != expected:
        raise G3StaticGradingError(
            "static answer grade differs from canonical derivation"
        )
