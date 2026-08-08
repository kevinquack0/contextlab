"""Deterministic, dependency-free retrieval primitives for ContextLab v2.

The functions in this module operate on the public corpus chunk shape
(``chunk_id``, ``source_id``, ``section_id``, ``heading``, and ``text``).
They never read task gold, call a network service, or require an embedding
provider.  Candidate dictionaries conform to the shared EvidenceCandidate
schema; operational details live in separate result traces.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from hashlib import sha256
from math import log2
import math
import re
from typing import Any, Iterable, Mapping, Sequence


EVIDENCE_SCHEMA_VERSION = "contextlab.evidence-candidate.v1"
RERANKER_ID = "contextlab.linear-query-passage-v1"
RERANKER_COEFFICIENTS = {"overlap": 0.60, "identifier": 0.25, "prior": 0.15}
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]+(?:[-_/.][A-Za-z0-9]+)*")
_IDENTIFIER_PATTERN = re.compile(r"\b(?:[A-Za-z]+-)?\d{2,}(?:-[A-Za-z]+\d+)*\b")
_SOURCE_REFERENCE_PATTERN = re.compile(
    r"\bNL-\d{3}(?:-S\d{2})?(?:-C\d{2})?\b", re.IGNORECASE
)


def tokenize(value: str) -> list[str]:
    """Return the stable lowercase lexical tokens used by every scorer."""
    return [token.lower() for token in _TOKEN_PATTERN.findall(value)]


def estimate_tokens(text: str) -> int:
    """Use a stable word-like token estimate for local budget enforcement."""
    return len(tokenize(text))


def render_evidence_block(chunk: Mapping[str, Any]) -> str:
    """Render one evidence block exactly as it appears in an answer prompt."""
    return (
        f"[{_reference(chunk)}] {str(_value(chunk, 'heading', ''))}\n"
        f"{str(_value(chunk, 'text', ''))}"
    )


def _value(chunk: Mapping[str, Any], name: str, default: Any = "") -> Any:
    value = chunk.get(name, default)
    return default if value is None else value


def _reference(chunk: Mapping[str, Any]) -> str:
    source_id = str(_value(chunk, "source_id", "unknown"))
    section_id = _value(chunk, "section_id", "")
    return f"{source_id}#{section_id}" if section_id else source_id


def _content_hash(chunk: Mapping[str, Any]) -> str:
    supplied = chunk.get("content_hash")
    if isinstance(supplied, str) and re.fullmatch(r"[0-9a-f]{64}", supplied):
        return supplied
    return sha256(str(_value(chunk, "text")).encode("utf-8")).hexdigest()


def _candidate_id(chunk: Mapping[str, Any]) -> str:
    identity = "\0".join(
        (str(_value(chunk, "chunk_id", "")), _reference(chunk), _content_hash(chunk))
    )
    return sha256(identity.encode("utf-8")).hexdigest()[:24]


def _effective_time(chunk: Mapping[str, Any]) -> str | None:
    value = str(_value(chunk, "effective_time", _value(chunk, "publication_date", "")))
    if not value:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return f"{value}T00:00:00Z"
    return value


def evidence_candidate(
    chunk: Mapping[str, Any],
    *,
    stage: str,
    rank: int,
    raw_score: float | None,
    normalized_score: float | None,
) -> dict[str, Any]:
    """Normalize one corpus chunk into the schema's exact public contract."""
    source_id = str(_value(chunk, "source_id", "unknown"))
    section = _value(chunk, "section_id", None)
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "candidate_id": _candidate_id(chunk),
        "source_id": source_id,
        "section_id": str(section) if section else None,
        "content_hash": _content_hash(chunk),
        "retrieval_stage": stage,
        "rank": max(1, int(rank)),
        "raw_score": raw_score,
        "normalized_score": normalized_score,
        "authority": str(
            _value(chunk, "authority", _value(chunk, "authority_level", ""))
        )
        or None,
        "effective_time": _effective_time(chunk),
        "text_reference": _reference(chunk),
        "removal_reason": None,
    }


def _normalise(values: Sequence[float]) -> list[float]:
    if not values:
        return []
    low, high = min(values), max(values)
    if math.isclose(low, high):
        return [1.0 if high > 0 else 0.0 for _ in values]
    return [(value - low) / (high - low) for value in values]


def normalize_scored_candidates(
    chunks: Iterable[Mapping[str, Any]],
    scores: Mapping[str, float] | Iterable[tuple[str, float]],
    *,
    stage: str = "dense",
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Normalize supplied dense/vector scores without running an embedding model.

    Scores are keyed by ``chunk_id``. Missing, non-finite, and unknown values are
    ignored.  Ties resolve by source, section, chunk ID, then content hash.
    """
    score_map = dict(scores)
    records: list[tuple[Mapping[str, Any], float]] = []
    for chunk in chunks:
        key = str(_value(chunk, "chunk_id", ""))
        score = score_map.get(key)
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
        ):
            continue
        records.append((chunk, float(score)))
    records.sort(key=lambda pair: (-pair[1], _candidate_sort_key(pair[0])))
    if limit is not None:
        records = records[: max(0, limit)]
    normalized = _normalise([score for _, score in records])
    return [
        evidence_candidate(
            chunk, stage=stage, rank=rank, raw_score=score, normalized_score=value
        )
        for rank, ((chunk, score), value) in enumerate(
            zip(records, normalized), start=1
        )
    ]


def _candidate_sort_key(value: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(_value(value, "source_id", "")),
        str(_value(value, "section_id", "")),
        str(_value(value, "chunk_id", _value(value, "candidate_id", ""))),
        _content_hash(value),
    )


def bm25_retrieve(
    query: str,
    chunks: Iterable[Mapping[str, Any]],
    *,
    limit: int = 10,
    k1: float = 1.5,
    b: float = 0.75,
    stage: str = "bm25",
    include_source_identifiers: bool = True,
) -> list[dict[str, Any]]:
    """Run identifier-aware Okapi BM25 over public chunk text and metadata."""
    corpus = list(chunks)
    if not query or not corpus or limit <= 0:
        return []
    lexical_query = (
        query
        if include_source_identifiers
        else _SOURCE_REFERENCE_PATTERN.sub(" ", query)
    )
    query_terms = tokenize(lexical_query)
    if not query_terms:
        return []
    documents = []
    for chunk in corpus:
        fields = (
            ("source_id", "section_id", "chunk_id", "heading", "title", "text")
            if include_source_identifiers
            else ("heading", "title", "text")
        )
        document = " ".join(str(_value(chunk, field)) for field in fields)
        if not include_source_identifiers:
            document = _SOURCE_REFERENCE_PATTERN.sub(" ", document)
        documents.append(tokenize(document))
    lengths = [len(document) for document in documents]
    average_length = sum(lengths) / len(lengths) if lengths else 0.0
    document_frequency = Counter(
        term for document in documents for term in set(document)
    )
    query_counts = Counter(query_terms)
    scores: list[tuple[Mapping[str, Any], float]] = []
    for chunk, document, length in zip(corpus, documents, lengths):
        frequencies = Counter(document)
        score = 0.0
        for term, query_count in query_counts.items():
            frequency = frequencies[term]
            if not frequency:
                continue
            inverse_frequency = math.log(
                1
                + (len(corpus) - document_frequency[term] + 0.5)
                / (document_frequency[term] + 0.5)
            )
            denominator = frequency + k1 * (
                1 - b + b * (length / average_length if average_length else 0)
            )
            score += (
                query_count * inverse_frequency * (frequency * (k1 + 1) / denominator)
            )
        if score > 0:
            scores.append((chunk, score))
    scores.sort(key=lambda pair: (-pair[1], _candidate_sort_key(pair[0])))
    scores = scores[:limit]
    normalized = _normalise([score for _, score in scores])
    return [
        evidence_candidate(
            chunk, stage=stage, rank=rank, raw_score=score, normalized_score=value
        )
        for rank, ((chunk, score), value) in enumerate(zip(scores, normalized), start=1)
    ]


@dataclass(frozen=True)
class FusionResult:
    candidates: tuple[dict[str, Any], ...]
    stage_evidence: dict[str, dict[str, int]]


def rrf_fuse(
    stages: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    constant: int = 60,
    limit: int | None = None,
) -> FusionResult:
    """Fuse ranked lists while retaining source-stage ranks in ``stage_evidence``."""
    if constant < 0:
        raise ValueError("RRF constant must be non-negative")
    totals: dict[str, float] = defaultdict(float)
    originals: dict[str, Mapping[str, Any]] = {}
    evidence: dict[str, dict[str, int]] = defaultdict(dict)
    for stage, candidates in sorted(stages.items()):
        for position, candidate in enumerate(candidates, start=1):
            candidate_id = str(candidate["candidate_id"])
            rank = int(candidate.get("rank", position))
            totals[candidate_id] += 1.0 / (constant + max(1, rank))
            originals.setdefault(candidate_id, candidate)
            evidence[candidate_id][stage] = rank
    ordered = sorted(
        totals, key=lambda key: (-totals[key], _candidate_contract_key(originals[key]))
    )
    if limit is not None:
        ordered = ordered[: max(0, limit)]
    normalized = _normalise([totals[key] for key in ordered])
    candidates = []
    for rank, (candidate_id, score, value) in enumerate(
        zip(ordered, (totals[key] for key in ordered), normalized), start=1
    ):
        candidate = dict(originals[candidate_id])
        candidate.update(
            {
                "retrieval_stage": "rrf",
                "rank": rank,
                "raw_score": score,
                "normalized_score": value,
                "removal_reason": None,
            }
        )
        candidates.append(candidate)
    return FusionResult(
        tuple(candidates), {key: dict(value) for key, value in evidence.items()}
    )


def reciprocal_rank_fusion(
    stages: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    constant: int = 60,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Compatibility convenience wrapper returning only fused candidates."""
    return list(rrf_fuse(stages, constant=constant, limit=limit).candidates)


def linear_query_passage_rerank(
    query: str,
    candidates: Sequence[Mapping[str, Any]],
    chunks: Iterable[Mapping[str, Any]],
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Apply the pinned fixed-coefficient query-passage reranker locally."""
    by_id = {_candidate_id(chunk): chunk for chunk in chunks}
    query_tokens = set(tokenize(query))
    identifiers = set(token.lower() for token in _IDENTIFIER_PATTERN.findall(query))
    scored: list[tuple[Mapping[str, Any], float]] = []
    for candidate in candidates:
        chunk = by_id.get(str(candidate.get("candidate_id")))
        text = str(_value(chunk or candidate, "text", ""))
        passage = set(
            tokenize(" ".join((str(candidate.get("text_reference", "")), text)))
        )
        overlap = (
            len(query_tokens & passage) / len(query_tokens) if query_tokens else 0.0
        )
        identifier = (
            len(identifiers & passage) / len(identifiers) if identifiers else 0.0
        )
        prior = float(candidate.get("normalized_score") or 0.0)
        score = (
            RERANKER_COEFFICIENTS["overlap"] * overlap
            + RERANKER_COEFFICIENTS["identifier"] * identifier
            + RERANKER_COEFFICIENTS["prior"] * prior
        )
        scored.append((candidate, score))
    scored.sort(key=lambda pair: (-pair[1], _candidate_contract_key(pair[0])))
    if limit is not None:
        scored = scored[: max(0, limit)]
    normalized = _normalise([score for _, score in scored])
    result = []
    for rank, ((candidate, score), value) in enumerate(
        zip(scored, normalized), start=1
    ):
        item = dict(candidate)
        item.update(
            {
                "retrieval_stage": RERANKER_ID,
                "rank": rank,
                "raw_score": score,
                "normalized_score": value,
                "removal_reason": None,
            }
        )
        result.append(item)
    return result


def _candidate_contract_key(candidate: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(candidate.get("source_id", "")),
        str(candidate.get("section_id", "")),
        str(candidate.get("candidate_id", "")),
    )


@dataclass(frozen=True)
class SelectionResult:
    candidates: tuple[dict[str, Any], ...]
    transitions: dict[str, str]
    context_tokens: int


def merge_duplicates(candidates: Sequence[Mapping[str, Any]]) -> SelectionResult:
    """Keep the first-ranked candidate for each identical content hash."""
    kept: list[dict[str, Any]] = []
    transitions: dict[str, str] = {}
    seen: set[str] = set()
    for candidate in sorted(
        candidates,
        key=lambda value: (int(value.get("rank", 1)), _candidate_contract_key(value)),
    ):
        key = str(candidate.get("content_hash", candidate.get("candidate_id")))
        if key in seen:
            transitions[str(candidate["candidate_id"])] = "duplicate_content"
            continue
        seen.add(key)
        kept.append(dict(candidate))
    return SelectionResult(tuple(_rerank(kept)), transitions, 0)


def cap_per_source(
    candidates: Sequence[Mapping[str, Any]], *, cap: int
) -> SelectionResult:
    """Keep no more than ``cap`` ranked candidates from one source."""
    if cap < 0:
        raise ValueError("per-source cap must be non-negative")
    counts: Counter[str] = Counter()
    kept: list[dict[str, Any]] = []
    transitions: dict[str, str] = {}
    for candidate in sorted(
        candidates,
        key=lambda value: (int(value.get("rank", 1)), _candidate_contract_key(value)),
    ):
        source = str(candidate.get("source_id", ""))
        if counts[source] >= cap:
            transitions[str(candidate["candidate_id"])] = "per_source_cap"
            continue
        counts[source] += 1
        kept.append(dict(candidate))
    return SelectionResult(tuple(_rerank(kept)), transitions, 0)


def _rerank(candidates: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for rank, candidate in enumerate(candidates, start=1):
        item = dict(candidate)
        item["rank"] = rank
        result.append(item)
    return result


def expand_adjacent_sections(
    selected: Sequence[Mapping[str, Any]],
    chunks: Iterable[Mapping[str, Any]],
    *,
    token_budget: int,
) -> SelectionResult:
    """Add immediate same-source neighbors after ranking, never exceeding budget."""
    if token_budget < 0:
        raise ValueError("token budget must be non-negative")
    corpus = list(chunks)
    by_id = {_candidate_id(chunk): chunk for chunk in corpus}
    source_sections: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    seen_sections: set[tuple[str, str]] = set()
    for chunk in corpus:
        key = (
            str(_value(chunk, "source_id")),
            str(_value(chunk, "section_id", _value(chunk, "chunk_id"))),
        )
        if key not in seen_sections:
            source_sections[key[0]].append(chunk)
            seen_sections.add(key)
    chosen: list[dict[str, Any]] = []
    transitions: dict[str, str] = {}
    total = 0
    chosen_ids: set[str] = set()

    def add(
        chunk: Mapping[str, Any], reason: str, template: Mapping[str, Any] | None = None
    ) -> bool:
        nonlocal total
        candidate_id = _candidate_id(chunk)
        if candidate_id in chosen_ids:
            return False
        token_count = estimate_tokens(render_evidence_block(chunk))
        if total + token_count > token_budget:
            transitions[candidate_id] = "token_budget"
            return False
        candidate = evidence_candidate(
            chunk,
            stage="adjacent_expansion",
            rank=len(chosen) + 1,
            raw_score=(template or {}).get("raw_score"),
            normalized_score=(template or {}).get("normalized_score"),
        )
        chosen.append(candidate)
        chosen_ids.add(candidate_id)
        transitions[candidate_id] = reason
        total += token_count
        return True

    for selected_candidate in sorted(
        selected,
        key=lambda item: (int(item.get("rank", 1)), _candidate_contract_key(item)),
    ):
        chunk = by_id.get(str(selected_candidate.get("candidate_id")))
        if chunk is None:
            transitions[str(selected_candidate.get("candidate_id", ""))] = (
                "missing_chunk"
            )
            continue
        add(chunk, "ranked", selected_candidate)
        source = str(_value(chunk, "source_id"))
        sections = source_sections[source]
        try:
            index = next(
                index
                for index, value in enumerate(sections)
                if _candidate_id(value) == _candidate_id(chunk)
            )
        except StopIteration:
            continue
        for neighbor_index, reason in (
            (index - 1, "adjacent_previous"),
            (index + 1, "adjacent_next"),
        ):
            if 0 <= neighbor_index < len(sections):
                add(sections[neighbor_index], reason)
    return SelectionResult(tuple(_rerank(chosen)), transitions, total)


@dataclass(frozen=True)
class PageIndexNode:
    node_id: str
    heading: str
    summary: str
    raw_section_ids: tuple[str, ...]
    children: tuple["PageIndexNode", ...] = ()


class PageIndex:
    """Small PageIndex-style heading tree with raw evidence pointers."""

    def __init__(self, roots: Sequence[PageIndexNode]):
        self.roots = tuple(roots)

    @classmethod
    def build(cls, chunks: Iterable[Mapping[str, Any]]) -> "PageIndex":
        grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for chunk in chunks:
            source = str(_value(chunk, "source_id", "unknown"))
            grouped[source].append(chunk)
        roots = []
        for source, values in sorted(grouped.items()):
            leaves = []
            for ordinal, chunk in enumerate(values, start=1):
                section = str(
                    _value(
                        chunk,
                        "section_id",
                        _value(chunk, "chunk_id", f"{source}-{ordinal}"),
                    )
                )
                heading = str(_value(chunk, "heading", section)).strip() or section
                text = str(_value(chunk, "text"))
                summary = (heading + ": " + " ".join(text.split()[:24])).strip()
                leaves.append(PageIndexNode(section, heading, summary, (section,)))
            raw = tuple(section for leaf in leaves for section in leaf.raw_section_ids)
            summary = " ".join(leaf.summary for leaf in leaves)[:800]
            roots.append(PageIndexNode(source, source, summary, raw, tuple(leaves)))
        return cls(roots)

    def select_branches(
        self, query: str, *, max_branches: int = 2, token_budget: int = 200
    ) -> list[PageIndexNode]:
        """Select bounded document branches using only public summaries."""
        if max_branches <= 0 or token_budget <= 0:
            return []
        terms = set(tokenize(query))
        scored = []
        for root in self.roots:
            tokens = set(tokenize(root.summary))
            score = len(terms & tokens) / len(terms) if terms else 0.0
            scored.append((root, score))
        scored.sort(key=lambda pair: (-pair[1], pair[0].node_id))
        chosen, used = [], 0
        for root, _ in scored[:max_branches]:
            size = estimate_tokens(root.summary)
            if used + size <= token_budget:
                chosen.append(root)
                used += size
        return chosen

    def expand_raw_leaves(
        self,
        branches: Iterable[PageIndexNode],
        chunks: Iterable[Mapping[str, Any]],
        *,
        token_budget: int,
    ) -> SelectionResult:
        wanted = {section for branch in branches for section in branch.raw_section_ids}
        corpus = list(chunks)
        candidates = [
            evidence_candidate(
                chunk,
                stage="pageindex_raw_leaf",
                rank=rank,
                raw_score=None,
                normalized_score=None,
            )
            for rank, chunk in enumerate(
                (
                    chunk
                    for chunk in corpus
                    if str(_value(chunk, "section_id", _value(chunk, "chunk_id")))
                    in wanted
                ),
                start=1,
            )
        ]
        return expand_adjacent_sections(candidates, corpus, token_budget=token_budget)


def route_query(query: str, *, structured_fields: Iterable[str] = ()) -> str:
    """Return ``prose``, ``structured``, or ``combined`` without task-label access."""
    words = set(tokenize(query))
    structured_words = {
        "count",
        "sum",
        "average",
        "total",
        "percentage",
        "percent",
        "table",
    }
    prose_words = {
        "why",
        "explain",
        "compare",
        "evidence",
        "reason",
        "policy",
        "should",
    }
    has_structured = (
        bool(words & structured_words)
        or "how many" in " ".join(tokenize(query))
        or any(field.lower() in words for field in structured_fields)
    )
    has_prose = bool(words & prose_words)
    if has_structured and has_prose:
        return "combined"
    return "structured" if has_structured else "prose"


def oracle_route(task_label: str) -> str:
    """Analysis-only route that intentionally depends on a task label and nothing else."""
    label = task_label.strip().lower()
    if label in {"combined", "structured_plus_prose", "table_plus_prose"}:
        return "combined"
    if label in {"structured", "structured_data", "sql"}:
        return "structured"
    return "prose"


def retrieval_metrics(
    candidates: Sequence[Mapping[str, Any]],
    relevant_candidate_ids: Iterable[str] = (),
    *,
    relevant_evidence_refs: Iterable[str] = (),
    required_source_ids: Iterable[str] = (),
    k: int = 10,
    candidate_texts: Mapping[str, str] | None = None,
    context_texts: Iterable[str] = (),
) -> dict[str, float | int]:
    """Calculate deterministic component metrics; all empty-result values are zero."""
    relevant = set(relevant_candidate_ids)
    evidence_refs = set(relevant_evidence_refs)
    required_sources = set(required_source_ids)
    ranked = list(
        sorted(
            candidates,
            key=lambda item: (int(item.get("rank", 1)), _candidate_contract_key(item)),
        )
    )[: max(0, k)]

    def matching_refs(candidate: Mapping[str, Any]) -> set[str]:
        source = str(candidate.get("source_id", ""))
        section = str(candidate.get("section_id", ""))
        reference = str(candidate.get("text_reference", ""))
        return {
            ref
            for ref in evidence_refs
            if ref == source
            or ref == section
            or ref == reference
            or reference.endswith(f"#{ref}")
        }

    covered_refs: set[str] = set()
    gains: list[int] = []
    hits: list[bool] = []
    for candidate in ranked:
        id_hit = str(candidate.get("candidate_id")) in relevant
        newly_covered = matching_refs(candidate).difference(covered_refs)
        covered_refs.update(newly_covered)
        hits.append(id_hit or bool(newly_covered))
        gains.append(1 if id_hit or newly_covered else 0)
    denominator = len(evidence_refs) if evidence_refs else len(relevant)
    covered = len(covered_refs) if evidence_refs else sum(hits)
    recall = covered / denominator if denominator else 0.0
    precision = sum(hits) / len(ranked) if ranked else 0.0
    reciprocal_rank = next(
        (1.0 / rank for rank, hit in enumerate(hits, start=1) if hit), 0.0
    )
    dcg = sum(gain / log2(rank + 1) for rank, gain in enumerate(gains, start=1))
    ideal = sum(
        1.0 / log2(rank + 1) for rank in range(1, min(denominator, len(ranked)) + 1)
    )
    selected_sources = {str(candidate.get("source_id", "")) for candidate in ranked}
    texts = candidate_texts or {}
    return {
        "recall_at_k": recall,
        "precision_at_k": precision,
        "reciprocal_rank": reciprocal_rank,
        "ndcg": dcg / ideal if ideal else 0.0,
        "required_source_coverage": len(selected_sources & required_sources)
        / len(required_sources)
        if required_sources
        else 0.0,
        "source_diversity": len(selected_sources),
        "candidate_tokens": sum(
            estimate_tokens(texts.get(str(candidate.get("candidate_id")), ""))
            for candidate in candidates
        ),
        "context_tokens": sum(estimate_tokens(text) for text in context_texts),
    }
