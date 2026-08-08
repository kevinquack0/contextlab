"""Freeze and validate the 120-task ContextLab G2 static benchmark."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Iterable

from .baseline import repository_root
from .tasking import read_jsonl, sha256_json, validate_public_tasks


STATIC_FREEZE_SCHEMA = "contextlab.static-freeze.v1"
STATIC_TARGET_COUNTS = {
    "regression": 48,
    "judge_calibration": 24,
    "sealed_capability": 36,
    "showcase": 12,
}
STATIC_TASK_IDS = tuple(f"S{number:03d}" for number in range(1, 121))
SEALED_TASK_IDS = frozenset(f"S{number:03d}" for number in range(81, 117))
PUBLIC_TASK_COUNT = 84
SEALED_TASK_COUNT = 36
FROZEN_RAW_TAG = "contextlab-v1-raw-2026-06"
FROZEN_RAW_CHUNKS_SHA256 = (
    "6f6cfd25e92088e1e2da198ce14b7b7b56373e22e953261a0287f06cd7143e13"
)
FROZEN_EMBEDDINGS_SHA256 = (
    "a57f55587ab133102cf9961fb407cbcc4ac9120147584951b7a998896ca70bbf"
)


class StaticBenchmarkError(ValueError):
    """The G2 static task set or its freeze record is incomplete or inconsistent."""


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _completion_paths(root: Path) -> tuple[Path, Path]:
    return (
        root / "evaluation/v2/tasks/static_completion_g2.jsonl",
        root
        / "novalearn_synthetic_corpus/evaluation_only_do_not_index/v2"
        / "static_completion_g2_gold.jsonl",
    )


def _known_evidence_ids(root: Path) -> set[str]:
    output = subprocess.run(
        ["git", "show", f"{FROZEN_RAW_TAG}:evaluation/build/chunks.jsonl"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    evidence: set[str] = set()
    for line in output.splitlines():
        if line.strip():
            row = json.loads(line)
            evidence.add(str(row["source_id"]))
            evidence.add(str(row["section_id"]))
    return evidence


def _validated_g2_public_completion(root: Path) -> list[dict[str, Any]]:
    """Validate promptable completion tasks without touching scorer-only gold."""
    public_path, _ = _completion_paths(root)
    public = validate_public_tasks(read_jsonl(public_path))
    expected_ids = {"S117", "S118", "S119", "S120"}
    if {str(row["task_id"]) for row in public} != expected_ids or len(public) != 4:
        raise StaticBenchmarkError(
            "G2 public completion tasks must be exactly S117 through S120"
        )
    known = _known_evidence_ids(root)
    for row in public:
        if row["question_status"] != "frozen_public" or row["sealed_eligible"]:
            raise StaticBenchmarkError(
                f"{row['task_id']}: completion task is not public and frozen"
            )
        unknown = set(map(str, row.get("required_evidence", []))).difference(known)
        if unknown:
            raise StaticBenchmarkError(
                f"{row['task_id']}: unknown evidence {sorted(unknown)}"
            )
    return public


def validate_g2_public_completion(root: Path | None = None) -> dict[str, int]:
    """Validate only the four promptable completion tasks."""
    root = (root or repository_root()).resolve()
    return {"public_tasks": len(_validated_g2_public_completion(root))}


def validate_g2_completion(root: Path | None = None) -> dict[str, int]:
    """Validate public completion tasks and protected evaluator-only gold."""
    root = (root or repository_root()).resolve()
    public = _validated_g2_public_completion(root)
    _, gold_path = _completion_paths(root)
    gold = read_jsonl(gold_path)
    expected_ids = {"S117", "S118", "S119", "S120"}
    if {str(row.get("task_id")) for row in gold} != expected_ids:
        raise StaticBenchmarkError("G2 completion gold IDs differ from public tasks")
    if len(gold) != 4:
        raise StaticBenchmarkError(
            "G2 completion requires exactly four public and four gold rows"
        )
    known = _known_evidence_ids(root)
    for row in gold:
        if set(row) != {
            "schema_version",
            "task_id",
            "expected_answer",
            "required_evidence",
            "scoring_notes",
        }:
            raise StaticBenchmarkError(
                f"{row.get('task_id')}: invalid protected gold fields"
            )
        if row["schema_version"] != "contextlab.gold-task.v1":
            raise StaticBenchmarkError(
                f"{row['task_id']}: invalid protected gold schema"
            )
        if (
            not str(row["expected_answer"]).strip()
            or not str(row["scoring_notes"]).strip()
        ):
            raise StaticBenchmarkError(f"{row['task_id']}: incomplete protected gold")
        unknown = set(map(str, row["required_evidence"])).difference(known)
        if unknown:
            raise StaticBenchmarkError(
                f"{row['task_id']}: unknown gold evidence {sorted(unknown)}"
            )
    return {"public_tasks": len(public), "protected_gold_rows": len(gold)}


def static_task_catalog(root: Path | None = None) -> list[dict[str, Any]]:
    """Return S001-S120, replacing G1's four planned slots with authored G2 tasks."""
    root = (root or repository_root()).resolve()
    completion = _validated_g2_public_completion(root)
    tasks_dir = root / "evaluation/v2/tasks"
    rows = (
        read_jsonl(tasks_dir / "v1_annotated.jsonl")
        + read_jsonl(tasks_dir / "static_new_g1.jsonl")
        + [
            row
            for row in read_jsonl(tasks_dir / "planned_slots.jsonl")
            if str(row.get("task_id")) in SEALED_TASK_IDS
        ]
        + completion
    )
    rows = [
        {
            **row,
            "question_status": "frozen_public",
        }
        if str(row.get("task_id")) not in SEALED_TASK_IDS
        else row
        for row in rows
    ]
    tasks = validate_public_tasks(rows)
    by_id = {str(row["task_id"]): row for row in tasks}
    if len(tasks) != 120 or tuple(sorted(by_id)) != STATIC_TASK_IDS:
        raise StaticBenchmarkError(
            "G2 static catalog must contain exactly S001 through S120"
        )
    for task_id, task in by_id.items():
        if task_id in SEALED_TASK_IDS:
            if task["question_status"] != "external" or "question_text" in task:
                raise StaticBenchmarkError(
                    f"{task_id}: sealed task leaked question text"
                )
        elif not str(task.get("question_text", "")).strip():
            raise StaticBenchmarkError(f"{task_id}: public task has no question text")
    return [by_id[task_id] for task_id in STATIC_TASK_IDS]


def public_static_tasks(root: Path | None = None) -> list[dict[str, Any]]:
    """Return only the 84 promptable public tasks; no protected gold is loaded."""
    root = (root or repository_root()).resolve()
    try:
        manifest = json.loads(
            (root / "results/v2/splits/static_g2_freeze.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise StaticBenchmarkError("cannot load the frozen G2 static manifest") from exc
    validate_static_freeze(manifest, root)
    return [
        row
        for row in static_task_catalog(root)
        if str(row["task_id"]) not in SEALED_TASK_IDS
    ]


def _static_assignments(root: Path) -> tuple[dict[str, str], dict[str, Any]]:
    skeleton = json.loads(
        (root / "results/v2/splits/task_split_manifest.json").read_text()
    )
    assignments = {
        str(row["task_id"]): str(row["partition"])
        for row in skeleton["tasks"]
        if row.get("suite") == "static"
    }
    if tuple(sorted(assignments)) != STATIC_TASK_IDS:
        raise StaticBenchmarkError(
            "G1 allocation skeleton lacks the full static task set"
        )
    if Counter(assignments.values()) != Counter(STATIC_TARGET_COUNTS):
        raise StaticBenchmarkError(
            "G1 static allocation differs from preregistered quotas"
        )
    return assignments, skeleton


def build_static_freeze(
    external_bundle_sha256: str, root: Path | None = None
) -> dict[str, Any]:
    """Build the immutable G2 record after an external author commits sealed tasks."""
    root = (root or repository_root()).resolve()
    if not _is_sha256(external_bundle_sha256):
        raise StaticBenchmarkError(
            "external sealed bundle requires a SHA-256 commitment"
        )
    validate_g2_completion(root)
    tasks = static_task_catalog(root)
    assignments, skeleton = _static_assignments(root)
    records: list[dict[str, Any]] = []
    for task in tasks:
        task_id = str(task["task_id"])
        sealed = task_id in SEALED_TASK_IDS
        identity = {
            "task_id": task_id,
            "suite": "static",
            "source_kind": task["source_kind"],
            "task_family": task["task_family"],
            "difficulty": task["difficulty"],
            "answer_type": task["answer_type"],
            "freshness_sensitivity": task["freshness_sensitivity"],
            "structured_data_dependency": task["structured_data_dependency"],
            "question_status": task["question_status"],
            "partition": assignments[task_id],
            "task_status": "external_committed" if sealed else "authored_frozen",
        }
        record = {**identity, "metadata_sha256": sha256_json(identity)}
        if not sealed:
            record["question_sha256"] = task["question_sha256"]
        records.append(record)
    public_path, _ = _completion_paths(root)
    payload: dict[str, Any] = {
        "schema_version": STATIC_FREEZE_SCHEMA,
        "benchmark_status": "frozen",
        "assignment_algorithm": skeleton["assignment_algorithm"],
        "assignment_seed_sha256": skeleton["assignment_seed_sha256"],
        "allocation_skeleton_sha256": skeleton["manifest_sha256"],
        "corpus_snapshot": {
            "tag": FROZEN_RAW_TAG,
            "chunks_sha256": FROZEN_RAW_CHUNKS_SHA256,
            "dense_embedding_cache_sha256": FROZEN_EMBEDDINGS_SHA256,
        },
        "external_sealed_bundle": {
            "task_count": SEALED_TASK_COUNT,
            "bundle_sha256": external_bundle_sha256,
            "contents": "withheld_by_external_evaluator",
        },
        "public_input_sha256": {
            "v1_annotated": _sha256_file(
                root / "evaluation/v2/tasks/v1_annotated.jsonl"
            ),
            "static_new_g1": _sha256_file(
                root / "evaluation/v2/tasks/static_new_g1.jsonl"
            ),
            "static_completion_g2": _sha256_file(public_path),
        },
        "target_counts": STATIC_TARGET_COUNTS,
        "task_count": 120,
        "public_task_count": PUBLIC_TASK_COUNT,
        "sealed_task_count": SEALED_TASK_COUNT,
        "tasks": records,
    }
    payload["manifest_sha256"] = sha256_json(payload)
    validate_static_freeze(payload, root)
    return payload


def validate_static_freeze(manifest: dict[str, Any], root: Path | None = None) -> None:
    root = (root or repository_root()).resolve()
    expected_hash = manifest.get("manifest_sha256")
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if expected_hash != sha256_json(body):
        raise StaticBenchmarkError("static freeze manifest hash mismatch")
    if manifest.get("schema_version") != STATIC_FREEZE_SCHEMA:
        raise StaticBenchmarkError("unsupported static freeze schema")
    if manifest.get("benchmark_status") != "frozen":
        raise StaticBenchmarkError("static benchmark is not frozen")
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 120:
        raise StaticBenchmarkError("static freeze must contain exactly 120 tasks")
    if [row.get("task_id") for row in tasks] != list(STATIC_TASK_IDS):
        raise StaticBenchmarkError("static freeze task IDs or order changed")
    if Counter(str(row.get("partition")) for row in tasks) != Counter(
        STATIC_TARGET_COUNTS
    ):
        raise StaticBenchmarkError(
            "static freeze partition counts differ from the contract"
        )
    assignments, skeleton = _static_assignments(root)
    if manifest.get("allocation_skeleton_sha256") != skeleton["manifest_sha256"]:
        raise StaticBenchmarkError(
            "static freeze references a different allocation skeleton"
        )
    for row in tasks:
        task_id = str(row["task_id"])
        if row.get("partition") != assignments[task_id]:
            raise StaticBenchmarkError(f"{task_id}: preregistered partition changed")
        if task_id in SEALED_TASK_IDS:
            if row.get("partition") != "sealed_capability" or "question_sha256" in row:
                raise StaticBenchmarkError(
                    f"{task_id}: sealed identity leaked or moved"
                )
            if row.get("task_status") != "external_committed":
                raise StaticBenchmarkError(
                    f"{task_id}: sealed task is not externally committed"
                )
        elif not _is_sha256(row.get("question_sha256")):
            raise StaticBenchmarkError(f"{task_id}: public question is not frozen")
    external = manifest.get("external_sealed_bundle")
    if (
        not isinstance(external, dict)
        or external.get("task_count") != SEALED_TASK_COUNT
    ):
        raise StaticBenchmarkError(
            "external sealed commitment has the wrong task count"
        )
    if not _is_sha256(external.get("bundle_sha256")):
        raise StaticBenchmarkError("external sealed bundle commitment is invalid")
    if external.get("contents") != "withheld_by_external_evaluator":
        raise StaticBenchmarkError("external sealed bundle contents are not withheld")

    public_paths = {
        "v1_annotated": root / "evaluation/v2/tasks/v1_annotated.jsonl",
        "static_new_g1": root / "evaluation/v2/tasks/static_new_g1.jsonl",
        "static_completion_g2": root / "evaluation/v2/tasks/static_completion_g2.jsonl",
    }
    expected_inputs = manifest.get("public_input_sha256")
    if not isinstance(expected_inputs, dict) or set(expected_inputs) != set(
        public_paths
    ):
        raise StaticBenchmarkError("static freeze public input commitments are invalid")
    for name, path in public_paths.items():
        if expected_inputs.get(name) != _sha256_file(path):
            raise StaticBenchmarkError(f"frozen public task input changed: {name}")

    current = {str(task["task_id"]): task for task in static_task_catalog(root)}
    for row in tasks:
        task_id = str(row["task_id"])
        task = current[task_id]
        identity = {
            "task_id": task_id,
            "suite": "static",
            "source_kind": task["source_kind"],
            "task_family": task["task_family"],
            "difficulty": task["difficulty"],
            "answer_type": task["answer_type"],
            "freshness_sensitivity": task["freshness_sensitivity"],
            "structured_data_dependency": task["structured_data_dependency"],
            "question_status": task["question_status"],
            "partition": assignments[task_id],
            "task_status": (
                "external_committed"
                if task_id in SEALED_TASK_IDS
                else "authored_frozen"
            ),
        }
        if row.get("metadata_sha256") != sha256_json(identity):
            raise StaticBenchmarkError(f"{task_id}: frozen task metadata changed")
        if task_id not in SEALED_TASK_IDS and row.get("question_sha256") != task.get(
            "question_sha256"
        ):
            raise StaticBenchmarkError(f"{task_id}: frozen public question changed")


def write_static_freeze(
    external_bundle_sha256: str,
    root: Path | None = None,
    output: Path | None = None,
) -> dict[str, Any]:
    root = (root or repository_root()).resolve()
    manifest = build_static_freeze(external_bundle_sha256, root)
    destination = output or root / "results/v2/splits/static_g2_freeze.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def load_public_gold(root: Path | None = None) -> list[dict[str, Any]]:
    """Trusted scorer-only loader; never pass this output to a strategy or prompt."""
    root = (root or repository_root()).resolve()
    v1 = read_jsonl(root / "evaluation/questions.jsonl")
    mapped = [
        {
            "schema_version": "contextlab.gold-task.v1",
            "task_id": f"S{index:03d}",
            "expected_answer": row["expected_answer"],
            "required_evidence": row.get("required_sections")
            or row["required_source_ids"],
            "scoring_notes": row["scoring_notes"],
        }
        for index, row in enumerate(v1, start=1)
    ]
    _, completion_gold = _completion_paths(root)
    rows = (
        mapped
        + read_jsonl(
            root
            / "novalearn_synthetic_corpus/evaluation_only_do_not_index/v2"
            / "static_new_g1_gold.jsonl"
        )
        + read_jsonl(completion_gold)
    )
    if len(rows) != PUBLIC_TASK_COUNT or len({row["task_id"] for row in rows}) != len(
        rows
    ):
        raise StaticBenchmarkError("public scorer gold must cover exactly 84 tasks")
    return rows


def refs_by_task(rows: Iterable[dict[str, Any]]) -> dict[str, tuple[str, ...]]:
    """Project scorer gold to evidence references only."""
    return {
        str(row["task_id"]): tuple(map(str, row.get("required_evidence", ())))
        for row in rows
    }
