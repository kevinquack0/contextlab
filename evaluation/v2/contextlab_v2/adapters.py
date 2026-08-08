"""Shared strategy adapter interface and behavior-preserving v1 replay wrapper."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .baseline import repository_root
from .boundaries import PROTECTED_COMPONENT, ProtectedDataError
from .contracts import validate_instance
from .tasking import FORBIDDEN_PUBLIC_FIELDS, read_jsonl, sha256_json, write_jsonl


ADAPTER_SAMPLE_SCHEMA = "contextlab.v1-adapter-sample.v1"
ADAPTER_IDS = frozenset({"full_context", "rag", "wiki", "sql"})
ADAPTER_SAMPLE_FIELDS = frozenset(
    {
        "schema_version",
        "task_id",
        "legacy_question_id",
        "strategy_id",
        "source_run_id",
        "source_run_sha256",
        "legacy_file",
        "legacy_file_sha256",
        "legacy_prompt_version",
        "instructions_sha256",
        "corpus_version",
        "legacy_input_tokens",
        "retrieved_sources",
        "retrieved_evidence",
        "answer",
        "answer_sha256",
    }
)


class AdapterError(ValueError):
    """An adapter input or output violates the shared strategy contract."""


@dataclass(frozen=True)
class AdapterRequest:
    task_id: str
    strategy_id: str


@dataclass(frozen=True)
class AdapterOutput:
    task_id: str
    strategy_id: str
    evidence_candidates: tuple[dict[str, Any], ...]
    context_pack: dict[str, Any]
    answer: str
    source_run_id: str


class StrategyAdapter(Protocol):
    """All v2 strategies expose candidates, one context pack, and one answer."""

    def run(self, request: AdapterRequest) -> AdapterOutput: ...


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_parts(reference: str) -> tuple[str, str | None]:
    if "#" in reference:
        source_id, section_id = reference.split("#", 1)
        return source_id, section_id
    section_match = re.fullmatch(r"(NL-\d{3})-S\d{2}", reference)
    if section_match:
        return section_match.group(1), reference
    return reference, None


class V1ReplayAdapter:
    """Replay frozen v1 outputs through the v2 contract without reading answer keys."""

    def __init__(self, fixture_path: Path):
        resolved = fixture_path.resolve()
        if PROTECTED_COMPONENT in resolved.parts:
            raise ProtectedDataError(f"protected adapter input rejected: {fixture_path}")
        rows = read_jsonl(resolved)
        self._rows: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            forbidden = FORBIDDEN_PUBLIC_FIELDS.intersection(row)
            if forbidden:
                raise ProtectedDataError(f"adapter fixture contains protected fields: {sorted(forbidden)}")
            unknown = set(row).difference(ADAPTER_SAMPLE_FIELDS)
            missing = ADAPTER_SAMPLE_FIELDS.difference(row)
            if unknown or missing:
                raise AdapterError(
                    f"adapter fixture fields differ; missing={sorted(missing)}, unknown={sorted(unknown)}"
                )
            if row.get("schema_version") != ADAPTER_SAMPLE_SCHEMA:
                raise AdapterError("unsupported v1 adapter sample schema")
            key = (str(row.get("task_id")), str(row.get("strategy_id")))
            if key in self._rows:
                raise AdapterError(f"duplicate adapter replay row: {key}")
            if key[1] not in ADAPTER_IDS:
                raise AdapterError(f"unknown v1 strategy: {key[1]}")
            answer = str(row.get("answer", ""))
            if hashlib.sha256(answer.encode("utf-8")).hexdigest() != row.get("answer_sha256"):
                raise AdapterError(f"{key}: answer hash mismatch")
            self._rows[key] = row

    def run(self, request: AdapterRequest) -> AdapterOutput:
        if request.strategy_id not in ADAPTER_IDS:
            raise AdapterError(f"unknown v1 strategy: {request.strategy_id}")
        try:
            row = self._rows[(request.task_id, request.strategy_id)]
        except KeyError as exc:
            raise AdapterError(
                f"no frozen replay for {request.task_id}/{request.strategy_id}"
            ) from exc
        return _adapter_output(
            request,
            row,
            answer=str(row["answer"]),
            references=[str(value) for value in row["retrieved_sources"]],
            retrieval_stage="v1_saved_output",
        )


def _adapter_output(
    request: AdapterRequest,
    row: Mapping[str, Any],
    *,
    answer: str,
    references: list[str],
    retrieval_stage: str,
    execution: Mapping[str, Any] | None = None,
) -> AdapterOutput:
    candidates: list[dict[str, Any]] = []
    evidence_by_reference = {
        str(item["reference"]): item for item in row["retrieved_evidence"]
    }
    for rank, reference in enumerate(references, start=1):
        source_id, section_id = _source_parts(reference)
        try:
            evidence = evidence_by_reference[reference]
        except KeyError as exc:
            raise AdapterError(f"missing evidence metadata for {reference}") from exc
        candidate_id = hashlib.sha256(
            f"{request.task_id}\0{request.strategy_id}\0{reference}\0{rank}".encode("utf-8")
        ).hexdigest()[:24]
        candidates.append(
            {
                "schema_version": "contextlab.evidence-candidate.v1",
                "candidate_id": candidate_id,
                "source_id": source_id,
                "section_id": section_id,
                "content_hash": evidence["content_sha256"],
                "retrieval_stage": retrieval_stage,
                "rank": rank,
                "raw_score": None,
                "normalized_score": None,
                "authority": None,
                "effective_time": None,
                "text_reference": reference,
                "removal_reason": None,
            }
        )
        errors = validate_instance("EvidenceCandidate", candidates[-1])
        if errors:
            raise AdapterError(f"invalid EvidenceCandidate for {reference}: {errors}")
    selected_ids = [candidate["candidate_id"] for candidate in candidates]
    if execution is None:
        corpus_snapshot_id = str(row["corpus_version"])
        token_budget = int(row["legacy_input_tokens"])
        source_run_id = str(row["source_run_id"])
        rendered_context_identity = {
            "replay_context": [item["text_reference"] for item in candidates],
            "source_run_id": source_run_id,
        }
    else:
        run_id = execution.get("run_id")
        corpus_version = execution.get("corpus_version")
        input_tokens = execution.get("input_tokens")
        if not isinstance(run_id, str) or not run_id:
            raise AdapterError("executed v1 output has no run ID")
        if not isinstance(corpus_version, str) or not corpus_version:
            raise AdapterError("executed v1 output has no corpus version")
        if isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens < 0:
            raise AdapterError("executed v1 output has invalid input-token usage")
        corpus_snapshot_id = corpus_version
        token_budget = input_tokens
        source_run_id = run_id
        rendered_context_identity = {
            "executed_context": [item["text_reference"] for item in candidates],
            "execution_run_id": source_run_id,
            "execution_input_tokens": token_budget,
        }
    context_identity = {
        "task_id": request.task_id,
        "strategy_id": request.strategy_id,
        "corpus_snapshot_id": corpus_snapshot_id,
        "selected_candidate_ids": selected_ids,
    }
    context_pack = {
        "schema_version": "contextlab.context-pack.v1",
        **context_identity,
        "token_budget": token_budget,
        "rendered_context_hash": sha256_json(rendered_context_identity),
        "instructions_hash": row["instructions_sha256"],
        "build_time_ms": 0,
    }
    context_errors = validate_instance("ContextPack", context_pack)
    if context_errors:
        raise AdapterError(f"invalid replay ContextPack: {context_errors}")
    return AdapterOutput(
        task_id=request.task_id,
        strategy_id=request.strategy_id,
        evidence_candidates=tuple(candidates),
        context_pack=context_pack,
        answer=answer,
        source_run_id=source_run_id,
    )


class V1ExecutableAdapter(V1ReplayAdapter):
    """Run a v1 strategy implementation, then normalize its output to v2 contracts."""

    def __init__(
        self,
        fixture_path: Path,
        execute: Callable[[AdapterRequest], Mapping[str, Any]],
    ):
        super().__init__(fixture_path)
        self._execute = execute

    def run(self, request: AdapterRequest) -> AdapterOutput:
        if request.strategy_id not in ADAPTER_IDS:
            raise AdapterError(f"unknown v1 strategy: {request.strategy_id}")
        try:
            row = self._rows[(request.task_id, request.strategy_id)]
        except KeyError as exc:
            raise AdapterError(
                f"no frozen executable sample for {request.task_id}/{request.strategy_id}"
            ) from exc
        executed = self._execute(request)
        if executed.get("strategy") != request.strategy_id:
            raise AdapterError("executed v1 strategy identity changed")
        answer = executed.get("answer")
        references = executed.get("retrieved_sources")
        if not isinstance(answer, str) or not isinstance(references, list) or not all(
            isinstance(value, str) for value in references
        ):
            raise AdapterError("executed v1 output lacks an answer or ordered source list")
        if executed.get("prompt_version") != row["legacy_prompt_version"]:
            raise AdapterError("executed v1 prompt version changed")
        return _adapter_output(
            request,
            row,
            answer=answer,
            references=references,
            retrieval_stage="v1_executed_strategy",
            execution=executed,
        )


def build_v1_adapter_sample(root: Path | None = None) -> list[dict[str, Any]]:
    """Sanitize all 160 frozen v1 runs without reading protected answer keys."""
    root = (root or repository_root()).resolve()
    legacy_path = root / "results" / "final" / "main_gemini.jsonl"
    legacy_rows = read_jsonl(legacy_path)
    if len(legacy_rows) != 160:
        raise AdapterError(f"expected 160 frozen v1 runs, found {len(legacy_rows)}")
    by_question: dict[str, set[str]] = {}
    for legacy in legacy_rows:
        by_question.setdefault(str(legacy.get("question_id")), set()).add(
            str(legacy.get("strategy"))
        )
    if len(by_question) != 40 or any(strategies != ADAPTER_IDS for strategies in by_question.values()):
        raise AdapterError("each of 40 v1 questions must contain all four frozen strategies")
    relative_legacy = str(legacy_path.relative_to(root))
    evidence_hashes = _evidence_hashes(root)
    rows: list[dict[str, Any]] = []
    for row in sorted(
        legacy_rows,
        key=lambda value: (str(value["question_id"]), str(value["strategy"])),
    ):
        answer = str(row["answer"])
        strategy = str(row["strategy"])
        prompt_path = root / "results" / "final" / "prompts" / f"{strategy}.md"
        references = [str(value) for value in row["retrieved_sources"]]
        retrieved_evidence = []
        for reference in references:
            source_id, section_id = _source_parts(reference)
            lookup = section_id or source_id
            try:
                content_sha256 = evidence_hashes[lookup]
            except KeyError as exc:
                raise AdapterError(f"cannot resolve saved v1 evidence reference: {reference}") from exc
            retrieved_evidence.append(
                {
                    "reference": reference,
                    "source_id": source_id,
                    "section_id": section_id,
                    "content_sha256": content_sha256,
                }
            )
        rows.append(
            {
                "schema_version": ADAPTER_SAMPLE_SCHEMA,
                "task_id": f"S{int(str(row['question_id'])[1:]):03d}",
                "legacy_question_id": str(row["question_id"]),
                "strategy_id": strategy,
                "source_run_id": str(row["run_id"]),
                "source_run_sha256": sha256_json(row),
                "legacy_file": relative_legacy,
                "legacy_file_sha256": _sha256_file(legacy_path),
                "legacy_prompt_version": str(row["prompt_version"]),
                "instructions_sha256": _sha256_file(prompt_path),
                "corpus_version": str(row["corpus_version"]),
                "legacy_input_tokens": int(row["input_tokens"]),
                "retrieved_sources": references,
                "retrieved_evidence": retrieved_evidence,
                "answer": answer,
                "answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
            }
        )
    output = root / "evaluation" / "v2" / "fixtures" / "v1_adapter_sample.jsonl"
    write_jsonl(output, rows)
    return rows


def _evidence_hashes(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    corpus = root / "novalearn_synthetic_corpus" / "corpus"
    heading = re.compile(r"^## \[(NL-\d{3}-S\d{2})\].*$", flags=re.MULTILINE)
    for path in corpus.rglob("NL-*.md"):
        source_match = re.match(r"(NL-\d{3})_", path.name)
        if not source_match:
            continue
        text = path.read_text(encoding="utf-8")
        hashes[source_match.group(1)] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        matches = list(heading.finditer(text))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            hashes[match.group(1)] = hashlib.sha256(
                text[match.start() : end].encode("utf-8")
            ).hexdigest()
    return hashes


def verify_v1_adapter_equivalence(root: Path | None = None) -> dict[str, int]:
    root = (root or repository_root()).resolve()
    fixture = root / "evaluation" / "v2" / "fixtures" / "v1_adapter_sample.jsonl"
    adapter = V1ReplayAdapter(fixture)
    legacy_rows = {
        (str(row["question_id"]), str(row["strategy"])): row
        for row in read_jsonl(root / "results" / "final" / "main_gemini.jsonl")
    }
    checked = 0
    for row in read_jsonl(fixture):
        legacy = legacy_rows[(str(row["legacy_question_id"]), str(row["strategy_id"]))]
        output = adapter.run(AdapterRequest(str(row["task_id"]), str(row["strategy_id"])))
        output_sources = [candidate["text_reference"] for candidate in output.evidence_candidates]
        if output.answer != legacy["answer"]:
            raise AdapterError(f"{row['strategy_id']}: replay answer changed")
        if output_sources != list(map(str, legacy["retrieved_sources"])):
            raise AdapterError(
                f"{row['legacy_question_id']}/{row['strategy_id']}: replay source order changed"
            )
        checked += 1
    return {
        "runs_checked": checked,
        "questions_checked": len({row["legacy_question_id"] for row in read_jsonl(fixture)}),
        "strategies_checked": len(ADAPTER_IDS),
        "answers_equal": checked,
        "ordered_source_lists_equal": checked,
        "contract_valid": checked,
    }


def _load_v1_harness(root: Path) -> Any:
    harness_path = root / "evaluation" / "harness.py"
    spec = importlib.util.spec_from_file_location("contextlab_v1_harness_check", harness_path)
    if spec is None or spec.loader is None:
        raise AdapterError("cannot load the frozen v1 harness")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_v1_executable_sample(root: Path | None = None) -> dict[str, int]:
    """Execute Q001 through all original v1 paths with recorded model replies."""
    root = (root or repository_root()).resolve()
    fixture = root / "evaluation" / "v2" / "fixtures" / "v1_adapter_sample.jsonl"
    legacy = {
        (str(row["question_id"]), str(row["strategy"])): row
        for row in read_jsonl(root / "results" / "final" / "main_gemini.jsonl")
    }
    sample_rows = {
        strategy: legacy[("Q001", strategy)] for strategy in sorted(ADAPTER_IDS)
    }
    first = sample_rows["full_context"]
    question = {
        "question_id": "Q001",
        "question_text": str(first["question_text"]),
        "category": str(first["category"]),
        # run_one copies this field into its result but never uses it to build a prompt.
        "expected_answer": "",
    }
    harness = _load_v1_harness(root)
    documents = harness.load_corpus()
    chunks = harness.read_jsonl(root / "evaluation" / "build" / "chunks.jsonl")
    wiki_nodes = harness.load_wiki_nodes()
    sections_by_ref = harness.build_sections_by_ref(documents)
    database = root / "evaluation" / "build" / "novalearn.db"
    original_model_call = harness.call_openrouter
    original_embedding_call = harness.call_openrouter_embeddings
    original_get_embeddings = harness.get_embeddings
    replayed_model_calls = 0
    answer_overrides: dict[str, str] = {}
    executed_results: dict[str, Mapping[str, Any]] = {}
    rag_score_by_text = {
        harness.chunk_embedding_text(chunk): float(score["similarity_score"])
        for chunk in chunks
        for score in sample_rows["rag"]["trace"]["retrieval_scores"]
        if chunk["chunk_id"] == score["chunk_id"]
    }

    def execute(request: AdapterRequest) -> Mapping[str, Any]:
        nonlocal replayed_model_calls
        saved = sample_rows[request.strategy_id]
        trace = saved["trace"]
        final_answer = answer_overrides.get(request.strategy_id, str(saved["answer"]))
        if request.strategy_id in {"full_context", "rag"}:
            replies = [final_answer]
        elif request.strategy_id == "wiki":
            replies = [
                json.dumps(
                    {
                        "seed_nodes": trace["seed_nodes"],
                        "follow_nodes": trace["follow_nodes"],
                        "expand_sections": trace["expanded_sections"],
                    }
                ),
                final_answer,
            ]
        else:
            replies = [
                json.dumps({"sql": trace["final_sql"]}),
                final_answer,
            ]

        def replay_model(*_: Any, **__: Any) -> dict[str, Any]:
            nonlocal replayed_model_calls
            if not replies:
                raise AdapterError("v1 strategy made an unexpected model call")
            replayed_model_calls += 1
            return {
                "answer": replies.pop(0),
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "raw_response_id": "recorded-g1-wrapper-proof",
            }

        harness.call_openrouter = replay_model
        result = harness.run_one(
            request.strategy_id,
            question,
            documents,
            chunks,
            wiki_nodes,
            sections_by_ref,
            database,
            str(saved["model"]),
            str(saved.get("embedding_model") or harness.DEFAULT_EMBEDDING_MODEL),
            0.0,
            True,
        )
        if replies:
            raise AdapterError("v1 strategy did not execute every expected model stage")
        if result.get("error"):
            raise AdapterError(f"v1 executable sample failed: {result['error']}")
        executed_results[request.strategy_id] = result
        return result

    def reject_embedding_network(*_: Any, **__: Any) -> list[list[float]]:
        raise AdapterError("v1 executable sample attempted an embedding network call")

    def replay_embedding_scores(
        texts: list[str], model: str, batch_size: int = 32
    ) -> dict[str, list[float]]:
        del batch_size
        vectors: dict[str, list[float]] = {}
        for text in texts:
            if text == question["question_text"]:
                vector = [1.0, 0.0]
            else:
                score = rag_score_by_text.get(text, -1.0)
                vector = [score, max(0.0, 1.0 - score * score) ** 0.5]
            vectors[f"{model}:{harness.sha1_text(text)}"] = vector
        return vectors

    harness.call_openrouter_embeddings = reject_embedding_network
    harness.get_embeddings = replay_embedding_scores
    adapter = V1ExecutableAdapter(fixture, execute)
    checked = 0
    perturbations = 0
    try:
        for strategy, saved in sample_rows.items():
            output = adapter.run(AdapterRequest("S001", strategy))
            executed = executed_results[strategy]
            references = [
                candidate["text_reference"] for candidate in output.evidence_candidates
            ]
            if output.answer != saved["answer"]:
                raise AdapterError(f"{strategy}: executed wrapper answer changed")
            if references != list(map(str, saved["retrieved_sources"])):
                raise AdapterError(f"{strategy}: executed wrapper source order changed")
            if output.context_pack["token_budget"] != executed["input_tokens"]:
                raise AdapterError(f"{strategy}: ContextPack ignored executed token usage")
            if output.context_pack["corpus_snapshot_id"] != executed["corpus_version"]:
                raise AdapterError(f"{strategy}: ContextPack ignored executed corpus version")
            if output.source_run_id != executed["run_id"]:
                raise AdapterError(f"{strategy}: wrapper ignored executed run identity")
            checked += 1
        for strategy in sample_rows:
            sentinel = f"contextlab-executed-answer-probe-{strategy}"
            answer_overrides[strategy] = sentinel
            output = adapter.run(AdapterRequest("S001", strategy))
            if output.answer != sentinel:
                raise AdapterError(f"{strategy}: wrapper did not preserve executed answer")
            perturbations += 1
    finally:
        harness.call_openrouter = original_model_call
        harness.call_openrouter_embeddings = original_embedding_call
        harness.get_embeddings = original_get_embeddings
    return {
        "runs_executed": checked,
        "strategies_executed": checked,
        "answers_equal": checked,
        "ordered_source_lists_equal": checked,
        "answer_passthrough_perturbations": perturbations,
        "execution_derived_context_packs": checked,
        "recorded_model_calls_replayed": replayed_model_calls,
        "paid_network_calls": 0,
    }
