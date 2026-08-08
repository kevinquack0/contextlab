"""Strict import boundary for grades returned by Kevin's external sealed evaluator."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .baseline import repository_root
from .tasking import sha256_json, validate_split_manifest


SEALED_RETURN_SCHEMA = "contextlab.sealed-return.v1"
SEALED_IMPORT_SCHEMA = "contextlab.sealed-import.v1"
SEALED_CANDIDATE_SCHEMA = "contextlab.sealed-candidate-manifest.v1"
SEALED_TASK_COUNT = 48
CELLS_PER_TASK = 10
SEALED_CELL_COUNT = SEALED_TASK_COUNT * CELLS_PER_TASK
ALLOWED_FAILURE_LABELS = frozenset(
    {
        "wrong_value",
        "missing_evidence",
        "unsupported_claim",
        "stale_authority",
        "bad_abstention",
        "incomplete",
        "citation_error",
        "other_permitted",
    }
)
ALLOWED_GRADE_CATEGORIES = frozenset(
    {"supported", "partial", "unsupported", "correct_abstention", "incorrect_abstention"}
)
TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "evaluation_id",
        "split_manifest_sha256",
        "candidate_manifest_sha256",
        "external_bundle_sha256",
        "records",
        "aggregate_metadata",
    }
)
RECORD_FIELDS = frozenset(
    {"task_id", "cell_sha256", "candidate_sha256", "grades", "failure_labels"}
)
GRADE_FIELDS = frozenset({"ordinal", "accepted", "category"})
AGGREGATE_FIELDS = frozenset(
    {"task_count", "cell_count", "accepted_count", "evaluator_version"}
)
CANDIDATE_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "split_manifest_sha256",
        "task_count",
        "cell_count",
        "cells",
        "manifest_sha256",
    }
)
CANDIDATE_CELL_FIELDS = frozenset({"task_id", "cell_sha256", "candidate_sha256"})


class SealedImportError(ValueError):
    """An external return contains leaked or invalid sealed data."""


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _exact_fields(value: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = set(value).difference(allowed)
    missing = set(allowed).difference(value)
    if unknown or missing:
        raise SealedImportError(
            f"{label} fields differ; missing={sorted(missing)}, unknown={sorted(unknown)}"
        )


def re_full_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def re_identifier(value: str) -> bool:
    return 1 <= len(value) <= 64 and all(
        character.isalnum() or character in "._-" for character in value
    )


def _sealed_ids(split_manifest: dict[str, Any]) -> set[str]:
    return {
        str(row["task_id"])
        for row in split_manifest["tasks"]
        if row["partition"] == "sealed_capability"
    }


def validate_candidate_manifest(
    manifest: dict[str, Any], split_manifest: dict[str, Any]
) -> dict[str, tuple[str, str]]:
    _exact_fields(manifest, CANDIDATE_MANIFEST_FIELDS, "sealed candidate manifest")
    if manifest["schema_version"] != SEALED_CANDIDATE_SCHEMA:
        raise SealedImportError("unsupported sealed candidate manifest schema")
    validate_split_manifest(split_manifest)
    if manifest["split_manifest_sha256"] != split_manifest["manifest_sha256"]:
        raise SealedImportError("candidate manifest uses a different split manifest")
    without_hash = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if manifest["manifest_sha256"] != sha256_json(without_hash):
        raise SealedImportError("sealed candidate manifest hash mismatch")
    if manifest["task_count"] != SEALED_TASK_COUNT or manifest["cell_count"] != SEALED_CELL_COUNT:
        raise SealedImportError("sealed candidate manifest must cover 48 tasks and 480 cells")
    cells = manifest["cells"]
    if not isinstance(cells, list) or len(cells) != SEALED_CELL_COUNT:
        raise SealedImportError("sealed candidate manifest must contain exactly 480 cells")
    sealed_ids = _sealed_ids(split_manifest)
    if len(sealed_ids) != SEALED_TASK_COUNT:
        raise SealedImportError("split manifest does not contain exactly 48 sealed tasks")
    counts: Counter[str] = Counter()
    by_cell: dict[str, tuple[str, str]] = {}
    for index, cell in enumerate(cells):
        if not isinstance(cell, dict):
            raise SealedImportError(f"candidate cell {index} is not an object")
        _exact_fields(cell, CANDIDATE_CELL_FIELDS, f"candidate cell {index}")
        task_id = str(cell["task_id"])
        cell_hash = str(cell["cell_sha256"])
        candidate_hash = str(cell["candidate_sha256"])
        if task_id not in sealed_ids:
            raise SealedImportError(f"candidate cell {index} is not a sealed task")
        if not re_full_sha256(cell_hash) or not re_full_sha256(candidate_hash):
            raise SealedImportError(f"candidate cell {index} has an invalid hash")
        if cell_hash in by_cell:
            raise SealedImportError(f"duplicate sealed cell hash: {cell_hash}")
        by_cell[cell_hash] = (task_id, candidate_hash)
        counts[task_id] += 1
    if set(counts) != sealed_ids or any(count != CELLS_PER_TASK for count in counts.values()):
        raise SealedImportError("candidate manifest must contain 10 cells for every sealed task")
    return by_cell


def validate_sealed_return(
    bundle: dict[str, Any],
    split_manifest: dict[str, Any],
    candidate_manifest: dict[str, Any],
    *,
    expected_external_bundle_sha256: str,
) -> None:
    _exact_fields(bundle, TOP_LEVEL_FIELDS, "sealed return")
    if bundle["schema_version"] != SEALED_RETURN_SCHEMA:
        raise SealedImportError("unsupported sealed-return schema")
    if not re_identifier(str(bundle["evaluation_id"])):
        raise SealedImportError("evaluation_id contains invalid characters")
    validate_split_manifest(split_manifest)
    if bundle["split_manifest_sha256"] != split_manifest["manifest_sha256"]:
        raise SealedImportError("sealed return uses a different split manifest")
    candidates = validate_candidate_manifest(candidate_manifest, split_manifest)
    if bundle["candidate_manifest_sha256"] != candidate_manifest["manifest_sha256"]:
        raise SealedImportError("sealed return uses a different candidate manifest")
    records = bundle["records"]
    if not isinstance(records, list) or len(records) != SEALED_CELL_COUNT:
        raise SealedImportError("sealed return must contain exactly 480 cell grades")
    seen_cells: set[str] = set()
    task_counts: Counter[str] = Counter()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise SealedImportError(f"record {index} is not an object")
        _exact_fields(record, RECORD_FIELDS, f"record {index}")
        task_id = str(record["task_id"])
        cell_hash = str(record["cell_sha256"])
        candidate_hash = str(record["candidate_sha256"])
        expected = candidates.get(cell_hash)
        if expected != (task_id, candidate_hash):
            raise SealedImportError(f"record {index} does not match the frozen candidate manifest")
        if cell_hash in seen_cells:
            raise SealedImportError(f"record {index} duplicates a sealed cell")
        seen_cells.add(cell_hash)
        task_counts[task_id] += 1
        grades = record["grades"]
        if not isinstance(grades, dict):
            raise SealedImportError(f"record {index} grades must be an object")
        _exact_fields(grades, GRADE_FIELDS, f"record {index} grades")
        if (
            isinstance(grades["ordinal"], bool)
            or not isinstance(grades["ordinal"], int)
            or not 0 <= grades["ordinal"] <= 3
        ):
            raise SealedImportError(f"record {index} ordinal must be 0..3")
        if not isinstance(grades["accepted"], bool):
            raise SealedImportError(f"record {index} accepted must be boolean")
        if (
            not isinstance(grades["category"], str)
            or grades["category"] not in ALLOWED_GRADE_CATEGORIES
        ):
            raise SealedImportError(f"record {index} contains an invalid grade category")
        labels = record["failure_labels"]
        if not isinstance(labels, list) or any(
            not isinstance(label, str) or label not in ALLOWED_FAILURE_LABELS
            for label in labels
        ):
            raise SealedImportError(f"record {index} contains an invalid failure label")
        if len(labels) != len(set(labels)):
            raise SealedImportError(f"record {index} contains duplicate failure labels")
    if set(seen_cells) != set(candidates):
        raise SealedImportError("sealed return does not cover every frozen candidate cell")
    if any(count != CELLS_PER_TASK for count in task_counts.values()):
        raise SealedImportError("sealed return must contain 10 cells for every sealed task")
    metadata = bundle["aggregate_metadata"]
    if not isinstance(metadata, dict):
        raise SealedImportError("aggregate_metadata must be an object")
    _exact_fields(metadata, AGGREGATE_FIELDS, "aggregate_metadata")
    if metadata["task_count"] != SEALED_TASK_COUNT or metadata["cell_count"] != len(records):
        raise SealedImportError("aggregate task or cell count differs from records")
    accepted = sum(1 for row in records if row["grades"]["accepted"])
    if metadata["accepted_count"] != accepted:
        raise SealedImportError("aggregate accepted_count differs from records")
    if not re_identifier(str(metadata["evaluator_version"])):
        raise SealedImportError("evaluator_version contains invalid characters")
    if not re_full_sha256(expected_external_bundle_sha256):
        raise SealedImportError("expected external bundle hash is invalid")
    if bundle["external_bundle_sha256"] != expected_external_bundle_sha256:
        raise SealedImportError("sealed return uses a different external bundle hash")


def build_sealed_contract_fixtures(root: Path | None = None) -> dict[str, int]:
    """Build a complete 48 x 5 x 2 synthetic import contract without sealed gold."""
    root = (root or repository_root()).resolve()
    split_path = root / "results/v2/splits/task_split_manifest.json"
    split_manifest = json.loads(split_path.read_text(encoding="utf-8"))
    validate_split_manifest(split_manifest)
    cells: list[dict[str, str]] = []
    for task_id in sorted(_sealed_ids(split_manifest)):
        for blind_ordinal in range(CELLS_PER_TASK):
            cell_hash = hashlib.sha256(
                f"contextlab-g1-sealed-cell\0{task_id}\0{blind_ordinal}".encode("utf-8")
            ).hexdigest()
            candidate_hash = hashlib.sha256(
                f"contextlab-g1-sealed-candidate\0{cell_hash}".encode("utf-8")
            ).hexdigest()
            cells.append(
                {
                    "task_id": task_id,
                    "cell_sha256": cell_hash,
                    "candidate_sha256": candidate_hash,
                }
            )
    candidate_manifest: dict[str, Any] = {
        "schema_version": SEALED_CANDIDATE_SCHEMA,
        "split_manifest_sha256": split_manifest["manifest_sha256"],
        "task_count": SEALED_TASK_COUNT,
        "cell_count": len(cells),
        "cells": cells,
    }
    candidate_manifest["manifest_sha256"] = sha256_json(candidate_manifest)
    records = [
        {
            **cell,
            "grades": {"ordinal": 3, "accepted": True, "category": "supported"},
            "failure_labels": [],
        }
        for cell in cells
    ]
    allowed: dict[str, Any] = {
        "schema_version": SEALED_RETURN_SCHEMA,
        "evaluation_id": "g1-complete-cell-fixture",
        "split_manifest_sha256": split_manifest["manifest_sha256"],
        "candidate_manifest_sha256": candidate_manifest["manifest_sha256"],
        "external_bundle_sha256": hashlib.sha256(
            b"contextlab-g1-external-sealed-bundle-fixture"
        ).hexdigest(),
        "records": records,
        "aggregate_metadata": {
            "task_count": SEALED_TASK_COUNT,
            "cell_count": len(records),
            "accepted_count": len(records),
            "evaluator_version": "fixture-v1",
        },
    }
    forbidden = {**allowed, "expected_answer": "forbidden raw sealed gold"}
    fixture_dir = root / "evaluation/v2/fixtures"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        "sealed_candidate_manifest.json": candidate_manifest,
        "sealed_return_allowed.json": allowed,
        "sealed_return_forbidden.json": forbidden,
    }
    for name, payload in outputs.items():
        (fixture_dir / name).write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return {"tasks": SEALED_TASK_COUNT, "cells": SEALED_CELL_COUNT}


def import_sealed_return(
    external_path: Path,
    output_path: Path,
    *,
    candidate_manifest_path: Path,
    expected_external_bundle_sha256: str,
    root: Path | None = None,
) -> dict[str, Any]:
    root = (root or repository_root()).resolve()
    source = external_path.resolve()
    if _is_relative_to(source, root):
        raise SealedImportError("sealed-return input must be outside the repository")
    split_path = root / "results/v2/splits/task_split_manifest.json"
    candidate_path = candidate_manifest_path.resolve()
    try:
        bundle = json.loads(source.read_text(encoding="utf-8"))
        split_manifest = json.loads(split_path.read_text(encoding="utf-8"))
        candidate_manifest = json.loads(candidate_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SealedImportError(f"cannot read sealed import inputs: {exc}") from exc
    if not isinstance(bundle, dict) or not isinstance(candidate_manifest, dict):
        raise SealedImportError("sealed return and candidate manifest must be objects")
    validate_sealed_return(
        bundle,
        split_manifest,
        candidate_manifest,
        expected_external_bundle_sha256=expected_external_bundle_sha256,
    )
    imported = {
        "schema_version": SEALED_IMPORT_SCHEMA,
        "evaluation_id": bundle["evaluation_id"],
        "split_manifest_sha256": bundle["split_manifest_sha256"],
        "candidate_manifest_sha256": bundle["candidate_manifest_sha256"],
        "external_bundle_sha256": bundle["external_bundle_sha256"],
        "source_return_sha256": _sha256_file(source),
        "records": bundle["records"],
        "aggregate_metadata": bundle["aggregate_metadata"],
    }
    output = output_path.resolve()
    if not _is_relative_to(output, root / "results/v2"):
        raise SealedImportError("sealed import output must stay under results/v2")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(imported, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return imported
