"""Deterministic answer checks that run before any model reviewer."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .tasking import _corpus_evidence_ids


CITATION_PATTERN = re.compile(r"(?<![A-Z0-9-])(NL-\d{3})(?:#(NL-\d{3}-S\d{2}))?(?![A-Z0-9-])")
FINAL_CELL_COUNT = 1600


@dataclass(frozen=True)
class DeterministicAnswerSpec:
    required_values: tuple[str, ...]
    required_citations: tuple[str, ...]
    allowed_source_ids: tuple[str, ...]
    forbidden_values: tuple[str, ...] = ()


def check_answer(
    answer: str,
    spec: DeterministicAnswerSpec,
    *,
    repository: Path,
) -> dict[str, Any]:
    failures: list[str] = []
    folded = answer.casefold()
    for value in spec.required_values:
        if value.casefold() not in folded:
            failures.append(f"missing required value: {value}")
    for value in spec.forbidden_values:
        if value.casefold() in folded:
            failures.append(f"contains forbidden value: {value}")
    citations = CITATION_PATTERN.findall(answer)
    cited_source_ids = {source_id for source_id, _ in citations}
    cited_refs = {section_id or source_id for source_id, section_id in citations}
    for reference in spec.required_citations:
        if reference not in cited_refs:
            failures.append(f"missing required citation: {reference}")
    allowed = set(spec.allowed_source_ids)
    unknown_for_task = cited_source_ids.difference(allowed)
    if unknown_for_task:
        failures.append(f"citation outside task evidence: {sorted(unknown_for_task)}")
    corpus_ids = _corpus_evidence_ids(repository.resolve())
    nonexistent = cited_refs.difference(corpus_ids)
    if nonexistent:
        failures.append(f"citation does not exist: {sorted(nonexistent)}")
    return {
        "passed": not failures,
        "failures": failures,
        "cited_source_ids": sorted(cited_source_ids),
        "cited_references": sorted(cited_refs),
    }


def run_deterministic_suite(
    cells: Iterable[Mapping[str, Any]],
    specs_by_task_id: Mapping[str, DeterministicAnswerSpec | None],
    *,
    repository: Path,
) -> dict[str, Any]:
    """Emit one deterministic result for every frozen final answer cell."""
    rows = list(cells)
    if len(rows) != FINAL_CELL_COUNT:
        raise ValueError(f"deterministic suite requires {FINAL_CELL_COUNT} cells, found {len(rows)}")
    cell_ids = [str(row.get("cell_id", "")) for row in rows]
    if any(not cell_id for cell_id in cell_ids) or len(set(cell_ids)) != FINAL_CELL_COUNT:
        raise ValueError("deterministic suite requires 1,600 unique non-empty cell IDs")
    task_ids = {str(row.get("task_id", "")) for row in rows}
    if not task_ids or set(specs_by_task_id) != task_ids:
        raise ValueError("deterministic specs must cover every final task exactly")
    results: list[dict[str, Any]] = []
    for row in rows:
        task_id = str(row["task_id"])
        spec = specs_by_task_id[task_id]
        if spec is None:
            result = {
                "cell_id": str(row["cell_id"]),
                "task_id": task_id,
                "status": "not_applicable",
                "passed": None,
                "failures": [],
            }
        else:
            check = check_answer(str(row.get("answer", "")), spec, repository=repository)
            result = {
                "cell_id": str(row["cell_id"]),
                "task_id": task_id,
                "status": "passed" if check["passed"] else "failed",
                **check,
            }
        results.append(result)
    if len(results) != FINAL_CELL_COUNT or {row["cell_id"] for row in results} != set(cell_ids):
        raise ValueError("deterministic suite did not emit exactly one result per final cell")
    return {
        "schema_version": "contextlab.deterministic-suite.v1",
        "cell_count": len(results),
        "covered_cell_count": len({row["cell_id"] for row in results}),
        "results": results,
    }
