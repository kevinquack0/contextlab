"""Task taxonomy, deterministic partitions, and public split manifests."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any, Iterable

from .baseline import repository_root


TASK_SCHEMA_VERSION = "contextlab.task.v1"
SPLIT_SCHEMA_VERSION = "contextlab.split-manifest.v1"
SPLIT_SEED = "contextlab-v2-partitions-v1"

PARTITION_TARGETS: dict[str, dict[str, int]] = {
    "static": {
        "regression": 48,
        "judge_calibration": 24,
        "sealed_capability": 36,
        "showcase": 12,
    },
    "temporal": {
        "regression": 16,
        "judge_calibration": 8,
        "sealed_capability": 12,
        "showcase": 4,
    },
}

FORBIDDEN_PUBLIC_FIELDS = frozenset(
    {
        "expected_answer",
        "gold_answer",
        "gold_evidence",
        "grading_label",
        "scoring_notes",
    }
)
PROMPT_TASK_SCHEMA_VERSION = "contextlab.prompt-task.v1"
PROMPT_TASK_FIELDS = frozenset(
    {"schema_version", "task_id", "suite", "question_text", "question_sha256"}
)
PUBLIC_TASK_FIELDS = frozenset(
    {
        "schema_version",
        "task_id",
        "legacy_question_id",
        "suite",
        "source_kind",
        "question_status",
        "question_text",
        "question_sha256",
        "task_family",
        "difficulty",
        "answer_type",
        "required_evidence",
        "acceptable_alternative_evidence",
        "freshness_sensitivity",
        "structured_data_dependency",
        "sealed_eligible",
        "metadata_status",
    }
)

CATEGORY_FAMILIES = {
    "Direct fact lookup": "direct_fact",
    "Citation/evidence questions": "evidence_selection",
    "Cross-document synthesis": "multi_hop_synthesis",
    "Comparison": "comparison",
    "Procedural guidance": "procedural_guidance",
    "Contradiction/freshness": "authority_conflict",
    "Structured/SQL-friendly": "table_prose_join",
    "Business recommendation": "recommendation",
}

ANSWER_TYPES = {
    "Direct fact lookup": "short_text",
    "Citation/evidence questions": "evidence_explanation",
    "Cross-document synthesis": "synthesis",
    "Comparison": "comparison",
    "Procedural guidance": "ordered_guidance",
    "Contradiction/freshness": "authority_resolution",
    "Structured/SQL-friendly": "structured_table",
    "Business recommendation": "recommendation",
}


class TaskContractError(ValueError):
    """A task catalog or partition violates the frozen G1 contract."""


def prompt_safe_task(task: dict[str, Any]) -> dict[str, str]:
    """Project evaluator metadata out before a task reaches any strategy or model."""
    question = task.get("question_text")
    if not isinstance(question, str) or not question.strip():
        raise TaskContractError("prompt-safe projection requires external question text at runtime")
    suite = str(task.get("suite", ""))
    if suite not in PARTITION_TARGETS:
        raise TaskContractError("prompt-safe projection has an invalid suite")
    projected = {
        "schema_version": PROMPT_TASK_SCHEMA_VERSION,
        "task_id": str(task.get("task_id", "")),
        "suite": suite,
        "question_text": question,
        "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
    }
    validate_prompt_safe_task(projected)
    return projected


def validate_prompt_safe_task(task: dict[str, Any]) -> None:
    if set(task) != PROMPT_TASK_FIELDS:
        raise TaskContractError("system-under-test task fields differ from the prompt-safe contract")
    if task.get("schema_version") != PROMPT_TASK_SCHEMA_VERSION:
        raise TaskContractError("unsupported prompt-safe task schema")
    if task.get("suite") not in PARTITION_TARGETS:
        raise TaskContractError("prompt-safe task suite is invalid")
    task_id = task.get("task_id")
    question = task.get("question_text")
    if not isinstance(task_id, str) or not task_id or not isinstance(question, str) or not question:
        raise TaskContractError("prompt-safe task ID and question must be non-empty text")
    digest = hashlib.sha256(question.encode("utf-8")).hexdigest()
    if task.get("question_sha256") != digest:
        raise TaskContractError("prompt-safe task question hash mismatch")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TaskContractError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise TaskContractError(f"{path}:{line_number}: task row must be an object")
        rows.append(value)
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    path.write_text(text, encoding="utf-8")


def _evidence_refs(question: dict[str, Any]) -> list[str]:
    section_ids = [str(value) for value in question.get("required_sections", []) if value]
    if section_ids:
        return section_ids
    return [str(value) for value in question.get("required_source_ids", [])]


def annotate_v1_questions(root: Path) -> list[dict[str, Any]]:
    """Remove answer keys while adding the G1 task taxonomy to the 40 v1 questions."""
    rows = read_jsonl(root / "evaluation" / "questions.jsonl")
    if len(rows) != 40:
        raise TaskContractError(f"expected 40 v1 questions, found {len(rows)}")
    annotated: list[dict[str, Any]] = []
    for index, question in enumerate(rows, start=1):
        category = str(question["category"])
        if category not in CATEGORY_FAMILIES:
            raise TaskContractError(f"unknown v1 category: {category}")
        text = str(question["question_text"])
        annotated.append(
            {
                "schema_version": TASK_SCHEMA_VERSION,
                "task_id": f"S{index:03d}",
                "legacy_question_id": str(question["question_id"]),
                "suite": "static",
                "source_kind": "v1_frozen",
                "question_status": "frozen_public",
                "question_text": text,
                "question_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "task_family": CATEGORY_FAMILIES[category],
                "difficulty": str(question["difficulty"]),
                "answer_type": ANSWER_TYPES[category],
                "required_evidence": _evidence_refs(question),
                "acceptable_alternative_evidence": [],
                "freshness_sensitivity": "high"
                if question.get("freshness_sensitive")
                else "none",
                "structured_data_dependency": "required"
                if category == "Structured/SQL-friendly"
                else ("helpful" if question.get("sql_friendly") else "none"),
                "sealed_eligible": False,
                "metadata_status": "authored",
            }
        )
    return annotated


def _planned_slots() -> list[dict[str, Any]]:
    static_families = (
        "authority_conflict",
        "paraphrase",
        "multi_hop_synthesis",
        "table_prose_join",
        "abstention",
    )
    temporal_families = (
        "information_extraction",
        "multi_session_reasoning",
        "temporal_reasoning",
        "knowledge_update",
        "abstention",
    )
    difficulties = ("easy", "medium", "hard", "medium", "hard")
    rows: list[dict[str, Any]] = []
    for offset in range(40):
        task_number = 81 + offset
        sealed = offset < 36
        rows.append(
            {
                "schema_version": TASK_SCHEMA_VERSION,
                "task_id": f"S{task_number:03d}",
                "suite": "static",
                "source_kind": "planned_slot",
                "question_status": "external" if sealed else "planned",
                "task_family": static_families[offset % len(static_families)],
                "difficulty": difficulties[offset % len(difficulties)],
                "answer_type": "planned",
                "freshness_sensitivity": "planned",
                "structured_data_dependency": "planned",
                "sealed_eligible": sealed,
                "metadata_status": "target",
            }
        )
    for offset in range(40):
        sealed = offset < 12
        rows.append(
            {
                "schema_version": TASK_SCHEMA_VERSION,
                "task_id": f"T{offset + 1:03d}",
                "suite": "temporal",
                "source_kind": "planned_slot",
                "question_status": "external" if sealed else "planned",
                "task_family": temporal_families[offset % len(temporal_families)],
                "difficulty": difficulties[offset % len(difficulties)],
                "answer_type": "planned",
                "freshness_sensitivity": "high",
                "structured_data_dependency": "planned",
                "sealed_eligible": sealed,
                "metadata_status": "target",
            }
        )
    return rows


def validate_public_tasks(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    tasks = list(rows)
    ids: set[str] = set()
    for row in tasks:
        forbidden = FORBIDDEN_PUBLIC_FIELDS.intersection(row)
        if forbidden:
            raise TaskContractError(
                f"{row.get('task_id', '<unknown>')}: forbidden public fields: {sorted(forbidden)}"
            )
        unknown = set(row).difference(PUBLIC_TASK_FIELDS)
        if unknown:
            raise TaskContractError(
                f"{row.get('task_id', '<unknown>')}: unknown public fields: {sorted(unknown)}"
            )
        required = {
            "schema_version",
            "task_id",
            "suite",
            "source_kind",
            "question_status",
            "task_family",
            "difficulty",
            "answer_type",
            "freshness_sensitivity",
            "structured_data_dependency",
            "sealed_eligible",
        }
        missing = required.difference(row)
        if missing:
            raise TaskContractError(f"{row.get('task_id', '<unknown>')}: missing {sorted(missing)}")
        if row["schema_version"] != TASK_SCHEMA_VERSION:
            raise TaskContractError(f"{row['task_id']}: unsupported task schema")
        expected_metadata_status = (
            "target" if row["source_kind"] == "planned_slot" else "authored"
        )
        status = row.setdefault("metadata_status", expected_metadata_status)
        if status != expected_metadata_status:
            raise TaskContractError(
                f"{row['task_id']}: metadata_status must be {expected_metadata_status}"
            )
        if row["task_id"] in ids:
            raise TaskContractError(f"duplicate task ID: {row['task_id']}")
        ids.add(str(row["task_id"]))
        if row["suite"] not in PARTITION_TARGETS:
            raise TaskContractError(f"{row['task_id']}: invalid suite {row['suite']}")
        if row["question_status"] == "external" and "question_text" in row:
            raise TaskContractError(f"{row['task_id']}: external task leaked question text")
        if "question_text" in row:
            digest = hashlib.sha256(str(row["question_text"]).encode("utf-8")).hexdigest()
            if row.get("question_sha256") not in (None, digest):
                raise TaskContractError(f"{row['task_id']}: question hash mismatch")
            row["question_sha256"] = digest
    return tasks


def task_catalog(root: Path | None = None) -> list[dict[str, Any]]:
    root = (root or repository_root()).resolve()
    v1_path = root / "evaluation" / "v2" / "tasks" / "v1_annotated.jsonl"
    new_path = root / "evaluation" / "v2" / "tasks" / "static_new_g1.jsonl"
    planned_path = root / "evaluation" / "v2" / "tasks" / "planned_slots.jsonl"
    rows = read_jsonl(v1_path) + read_jsonl(new_path) + read_jsonl(planned_path)
    return validate_public_tasks(rows)


def _corpus_evidence_ids(root: Path) -> set[str]:
    evidence: set[str] = set()
    corpus = root / "novalearn_synthetic_corpus" / "corpus"
    for path in corpus.rglob("*.md"):
        match = re.match(r"(NL-\d{3})_", path.name)
        if match:
            evidence.add(match.group(1))
        text = path.read_text(encoding="utf-8")
        evidence.update(re.findall(r"^## \[(NL-\d{3}-S\d{2})\]", text, flags=re.MULTILINE))
    return evidence


def validate_g1_task_drafts(root: Path | None = None) -> dict[str, int]:
    """Trusted authoring check; adapters never receive the protected gold rows."""
    root = (root or repository_root()).resolve()
    public_path = root / "evaluation" / "v2" / "tasks" / "static_new_g1.jsonl"
    gold_path = (
        root
        / "novalearn_synthetic_corpus"
        / "evaluation_only_do_not_index"
        / "v2"
        / "static_new_g1_gold.jsonl"
    )
    public_rows = validate_public_tasks(read_jsonl(public_path))
    gold_rows = read_jsonl(gold_path)
    if len(public_rows) != 40 or len(gold_rows) != 40:
        raise TaskContractError(
            f"G1 requires 40 public drafts and 40 protected gold rows; found "
            f"{len(public_rows)} and {len(gold_rows)}"
        )
    public_ids = {str(row["task_id"]) for row in public_rows}
    gold_ids = {str(row.get("task_id")) for row in gold_rows}
    if public_ids != gold_ids:
        raise TaskContractError("public draft IDs and protected gold IDs differ")
    evidence_ids = _corpus_evidence_ids(root)
    for row in public_rows:
        for field in ("required_evidence", "acceptable_alternative_evidence"):
            unknown = set(map(str, row.get(field, []))).difference(evidence_ids)
            if unknown:
                raise TaskContractError(f"{row['task_id']}: unknown {field}: {sorted(unknown)}")
    for row in gold_rows:
        if row.get("schema_version") != "contextlab.gold-task.v1":
            raise TaskContractError(f"{row.get('task_id')}: invalid protected gold schema")
        if not str(row.get("expected_answer", "")).strip():
            raise TaskContractError(f"{row.get('task_id')}: missing expected answer")
        unknown = set(map(str, row.get("required_evidence", []))).difference(evidence_ids)
        if unknown:
            raise TaskContractError(f"{row['task_id']}: unknown gold evidence: {sorted(unknown)}")
    families = Counter(str(row["task_family"]) for row in public_rows)
    expected_families = {
        "authority_conflict": 8,
        "paraphrase": 8,
        "multi_hop_synthesis": 8,
        "table_prose_join": 8,
        "abstention": 8,
    }
    if dict(families) != expected_families:
        raise TaskContractError(f"G1 draft family counts {dict(families)} != {expected_families}")
    return {
        "public_drafts": len(public_rows),
        "protected_gold_rows": len(gold_rows),
        "known_evidence_ids": len(evidence_ids),
    }


def _stable_rank(seed: str, partition: str, task_id: str) -> str:
    return hashlib.sha256(f"{seed}\0{partition}\0{task_id}".encode("utf-8")).hexdigest()


def _stratified_take(
    candidates: Iterable[dict[str, Any]], count: int, *, partition: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    buckets: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(deque)
    for task in sorted(
        candidates,
        key=lambda row: _stable_rank(SPLIT_SEED, partition, str(row["task_id"])),
    ):
        buckets[(str(task["task_family"]), str(task["difficulty"]))].append(task)
    keys = sorted(
        buckets,
        key=lambda key: hashlib.sha256(
            f"{SPLIT_SEED}\0{partition}\0{key[0]}\0{key[1]}".encode("utf-8")
        ).hexdigest(),
    )
    selected: list[dict[str, Any]] = []
    while len(selected) < count:
        progressed = False
        for key in keys:
            if buckets[key] and len(selected) < count:
                selected.append(buckets[key].popleft())
                progressed = True
        if not progressed:
            raise TaskContractError(f"not enough tasks for {partition}: need {count}")
    selected_ids = {str(row["task_id"]) for row in selected}
    remaining = [row for row in candidates if str(row["task_id"]) not in selected_ids]
    return selected, remaining


def assign_partitions(rows: Iterable[dict[str, Any]]) -> dict[str, str]:
    tasks = validate_public_tasks(rows)
    assigned: dict[str, str] = {}
    for suite, targets in PARTITION_TARGETS.items():
        suite_rows = [row for row in tasks if row["suite"] == suite]
        if len(suite_rows) != sum(targets.values()):
            raise TaskContractError(
                f"{suite}: expected {sum(targets.values())} tasks, found {len(suite_rows)}"
            )
        sealed_candidates = [row for row in suite_rows if row["sealed_eligible"]]
        sealed, remaining = _stratified_take(
            sealed_candidates,
            targets["sealed_capability"],
            partition="sealed_capability",
        )
        sealed_ids = {str(row["task_id"]) for row in sealed}
        remaining = [row for row in suite_rows if str(row["task_id"]) not in sealed_ids]
        for row in sealed:
            assigned[str(row["task_id"])] = "sealed_capability"
        for partition in ("showcase", "judge_calibration"):
            chosen, remaining = _stratified_take(
                remaining,
                targets[partition],
                partition=partition,
            )
            for row in chosen:
                assigned[str(row["task_id"])] = partition
        if len(remaining) != targets["regression"]:
            raise TaskContractError(f"{suite}: regression quota mismatch")
        for row in remaining:
            assigned[str(row["task_id"])] = "regression"
    return assigned


def build_split_manifest(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    tasks = validate_public_tasks(rows)
    assignments = assign_partitions(tasks)
    records: list[dict[str, Any]] = []
    for task in sorted(tasks, key=lambda row: str(row["task_id"])):
        public_identity = {
            "task_id": task["task_id"],
            "suite": task["suite"],
            "source_kind": task["source_kind"],
            "task_family": task["task_family"],
            "difficulty": task["difficulty"],
            "answer_type": task["answer_type"],
            "freshness_sensitivity": task["freshness_sensitivity"],
            "structured_data_dependency": task["structured_data_dependency"],
            "question_status": task["question_status"],
            "metadata_status": task["metadata_status"],
        }
        record = {
            **public_identity,
            "partition": assignments[str(task["task_id"])],
            "metadata_sha256": sha256_json(public_identity),
        }
        if task.get("question_sha256") and record["partition"] != "sealed_capability":
            record["question_sha256"] = task["question_sha256"]
        records.append(record)
    payload: dict[str, Any] = {
        "schema_version": SPLIT_SCHEMA_VERSION,
        "assignment_algorithm": "sha256_stratified_round_robin_v1",
        "assignment_seed_sha256": hashlib.sha256(SPLIT_SEED.encode("utf-8")).hexdigest(),
        "benchmark_status": "preregistered_allocation_skeleton",
        "target_counts": PARTITION_TARGETS,
        "task_count": len(records),
        "tasks": records,
    }
    payload["manifest_sha256"] = sha256_json(payload)
    validate_split_manifest(payload)
    return payload


def validate_split_manifest(manifest: dict[str, Any]) -> None:
    expected_hash = manifest.get("manifest_sha256")
    without_hash = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if expected_hash != sha256_json(without_hash):
        raise TaskContractError("split manifest hash mismatch")
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 160:
        raise TaskContractError("split manifest must contain exactly 160 tasks")
    if manifest.get("benchmark_status") != "preregistered_allocation_skeleton":
        raise TaskContractError("G1 split must be labelled as an allocation skeleton")
    for suite, targets in PARTITION_TARGETS.items():
        suite_rows = [row for row in tasks if row.get("suite") == suite]
        counts = Counter(str(row.get("partition")) for row in suite_rows)
        if dict(counts) != targets:
            raise TaskContractError(f"{suite}: partition counts {dict(counts)} != {targets}")
    for row in tasks:
        expected_status = "target" if row.get("source_kind") == "planned_slot" else "authored"
        if row.get("metadata_status") != expected_status:
            raise TaskContractError(f"{row.get('task_id')}: split metadata status is inaccurate")
        if row.get("partition") == "sealed_capability":
            if row.get("question_status") != "external":
                raise TaskContractError(f"{row.get('task_id')}: sealed task is not external")
            if "question_sha256" in row:
                raise TaskContractError(f"{row.get('task_id')}: sealed question hash leaked")


def build_task_foundation(root: Path | None = None) -> dict[str, Any]:
    root = (root or repository_root()).resolve()
    tasks_dir = root / "evaluation" / "v2" / "tasks"
    v1_rows = annotate_v1_questions(root)
    planned_rows = _planned_slots()
    write_jsonl(tasks_dir / "v1_annotated.jsonl", v1_rows)
    write_jsonl(tasks_dir / "planned_slots.jsonl", planned_rows)
    catalog = task_catalog(root)
    manifest = build_split_manifest(catalog)
    output = root / "results" / "v2" / "splits" / "task_split_manifest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest
