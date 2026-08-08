"""Public, content-free calibration receipt for the frozen G3 review panel."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import re
from typing import Any

from .review import (
    AI_KEVIN_ACCEPTED_MATCH_MIN,
    AI_KEVIN_MEAN_ABSOLUTE_DIFFERENCE_MAX,
    CALIBRATION_ACCEPTED_MATCH_MIN,
    CALIBRATION_CELL_COUNT,
    CALIBRATION_EXACT_ORDINAL_MIN,
    CALIBRATION_WITHIN_ONE_MIN,
    REVIEWERS,
    ReviewContractError,
    aggregate_panel_grades,
    validate_grade,
)
from .tasking import sha256_json


G3_PANEL_CALIBRATION_SCHEMA = "contextlab.g3-panel-calibration.v2"
AI_REVIEWERS = REVIEWERS[:2]
SOLE_HUMAN_REVIEWER = "kevin"
G3_PANEL_HIDDEN_REPEAT_COUNT = 2
G3_CALIBRATION_AI_INVOCATIONS = {
    "gpt-5.6-sol-high": {
        "requested_model": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "invocation": "fresh Codex subagent",
    },
    "claude-opus-5-medium": {
        "requested_model": "claude-opus-5",
        "reasoning_effort": "medium",
        "invocation": "local Claude CLI",
    },
}

_INPUT_CELL_FIELDS = frozenset(
    {
        "canonical_cell_id",
        "source_cell_sha256",
        "reference_grade",
        "individual_grades",
    }
)
_PUBLIC_CELL_FIELDS = _INPUT_CELL_FIELDS | {"aggregate"}
_REFERENCE_GRADE_FIELDS = frozenset({"overall_ordinal", "accepted"})
_HIDDEN_REPEAT_FIELDS = frozenset(
    {
        "repeat_id",
        "kind",
        "source_cell_sha256",
        "original_grades",
        "repeat_grades",
    }
)
_AI_INVOCATION_COMMITMENT_FIELDS = frozenset(
    {
        "reviewer",
        "requested_model",
        "reasoning_effort",
        "invocation",
        "review_manifest_sha256",
        "token_confirmation_artifact_sha256",
        "token_confirmation_file_sha256",
        "packet_token_preflight_sha256",
        "completed_return_artifact_sha256",
        "completed_return_file_sha256",
        "invocation_receipt_artifact_sha256",
        "invocation_receipt_file_sha256",
    }
)
_ARTIFACT_FIELDS = frozenset(
    {
        "schema_version",
        "reviewers",
        "ai_reviewers",
        "sole_human_reviewer",
        "review_manifest_sha256",
        "identity_map_sha256",
        "reference_sha256",
        "cell_count_per_reviewer",
        "cells",
        "cells_sha256",
        "ai_invocation_receipts",
        "ai_invocation_receipts_sha256",
        "metrics_vs_reference",
        "ai_vs_kevin",
        "hidden_repeats",
        "hidden_repeats_sha256",
        "hidden_repeat_consistency_by_reviewer",
        "rubric_ambiguity_by_reviewer",
        "thresholds",
        "status",
        "artifact_sha256",
    }
)
_THRESHOLDS = {
    "exact_ordinal_rate_min": CALIBRATION_EXACT_ORDINAL_MIN,
    "within_one_ordinal_rate_min": CALIBRATION_WITHIN_ONE_MIN,
    "accepted_match_rate_min": CALIBRATION_ACCEPTED_MATCH_MIN,
    "ai_kevin_accepted_match_rate_min": AI_KEVIN_ACCEPTED_MATCH_MIN,
    "ai_kevin_mean_absolute_ordinal_difference_max": (
        AI_KEVIN_MEAN_ABSOLUTE_DIFFERENCE_MAX
    ),
}


class G3PanelError(ValueError):
    """The public G3 panel-calibration receipt is incomplete or inconsistent."""


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _grade(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise G3PanelError(f"{label} must be a grade object")
    result = dict(value)
    try:
        validate_grade(result)
    except ReviewContractError as exc:
        raise G3PanelError(f"{label} is invalid: {exc}") from exc
    return result


def _panel(value: object, label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping) or set(value) != set(REVIEWERS):
        raise G3PanelError(
            f"{label} must preserve all three reviewers, including Kevin"
        )
    return {
        reviewer: _grade(value[reviewer], f"{label} {reviewer}")
        for reviewer in REVIEWERS
    }


def _reference_grade(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _REFERENCE_GRADE_FIELDS:
        raise G3PanelError(f"{label} reference grade fields differ")
    ordinal = value["overall_ordinal"]
    accepted = value["accepted"]
    if (
        isinstance(ordinal, bool)
        or not isinstance(ordinal, int)
        or not 0 <= ordinal <= 3
    ):
        raise G3PanelError(f"{label} reference ordinal must be 0 through 3")
    if not isinstance(accepted, bool):
        raise G3PanelError(f"{label} reference accepted decision must be boolean")
    return {"overall_ordinal": ordinal, "accepted": accepted}


def _calibration_cells(
    values: Iterable[Mapping[str, Any]], *, aggregates_required: bool
) -> list[dict[str, Any]]:
    rows = list(values)
    if len(rows) != CALIBRATION_CELL_COUNT:
        raise G3PanelError("G3 panel calibration requires exactly 20 shared cells")
    expected_fields = _PUBLIC_CELL_FIELDS if aggregates_required else _INPUT_CELL_FIELDS
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping) or set(row) != expected_fields:
            raise G3PanelError(f"calibration cell {index} fields differ")
        cell_id = row["canonical_cell_id"]
        source_hash = row["source_cell_sha256"]
        if not isinstance(cell_id, str) or not cell_id.strip():
            raise G3PanelError("calibration shared cell IDs must be non-empty strings")
        if not _is_sha256(source_hash):
            raise G3PanelError(f"{cell_id}: source cell hash is invalid")
        grades = _panel(row["individual_grades"], f"{cell_id} individual grades")
        try:
            aggregate = aggregate_panel_grades(grades)
        except ReviewContractError as exc:
            raise G3PanelError(f"{cell_id} aggregate is invalid: {exc}") from exc
        if aggregates_required and sha256_json(row["aggregate"]) != sha256_json(
            aggregate
        ):
            raise G3PanelError(
                f"{cell_id} aggregate differs from its individual grades"
            )
        normalized.append(
            {
                "canonical_cell_id": cell_id,
                "source_cell_sha256": source_hash,
                "reference_grade": _reference_grade(row["reference_grade"], cell_id),
                "individual_grades": grades,
                "aggregate": aggregate,
            }
        )
    normalized.sort(key=lambda row: row["canonical_cell_id"])
    cell_ids = [row["canonical_cell_id"] for row in normalized]
    source_hashes = [row["source_cell_sha256"] for row in normalized]
    if len(set(cell_ids)) != CALIBRATION_CELL_COUNT:
        raise G3PanelError("calibration shared cell IDs must be unique")
    if len(set(source_hashes)) != CALIBRATION_CELL_COUNT:
        raise G3PanelError("calibration source cell hashes must be unique")
    return normalized


def _agreement_vs_reference(cells: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for reviewer in REVIEWERS:
        differences = [
            abs(
                int(cell["individual_grades"][reviewer]["overall_ordinal"])
                - int(cell["reference_grade"]["overall_ordinal"])
            )
            for cell in cells
        ]
        accepted_matches = sum(
            cell["individual_grades"][reviewer]["accepted"]
            == cell["reference_grade"]["accepted"]
            for cell in cells
        )
        result[reviewer] = {
            "cells": CALIBRATION_CELL_COUNT,
            "exact_ordinal_rate": sum(value == 0 for value in differences)
            / CALIBRATION_CELL_COUNT,
            "within_one_ordinal_rate": sum(value <= 1 for value in differences)
            / CALIBRATION_CELL_COUNT,
            "accepted_match_rate": accepted_matches / CALIBRATION_CELL_COUNT,
        }
    return result


def _ai_vs_kevin(cells: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for reviewer in AI_REVIEWERS:
        differences = [
            abs(
                int(cell["individual_grades"][reviewer]["overall_ordinal"])
                - int(cell["individual_grades"][SOLE_HUMAN_REVIEWER]["overall_ordinal"])
            )
            for cell in cells
        ]
        accepted_matches = sum(
            cell["individual_grades"][reviewer]["accepted"]
            == cell["individual_grades"][SOLE_HUMAN_REVIEWER]["accepted"]
            for cell in cells
        )
        result[reviewer] = {
            "cells": CALIBRATION_CELL_COUNT,
            "exact_ordinal_rate": sum(value == 0 for value in differences)
            / CALIBRATION_CELL_COUNT,
            "within_one_ordinal_rate": sum(value <= 1 for value in differences)
            / CALIBRATION_CELL_COUNT,
            "accepted_match_rate": accepted_matches / CALIBRATION_CELL_COUNT,
            "mean_absolute_ordinal_difference": sum(differences)
            / CALIBRATION_CELL_COUNT,
        }
    return result


def _hidden_repeats(
    values: Iterable[Mapping[str, Any]],
    cells_by_source_hash: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(values):
        if not isinstance(row, Mapping) or set(row) != _HIDDEN_REPEAT_FIELDS:
            raise G3PanelError(f"hidden repeat {index} fields differ")
        repeat_id = row["repeat_id"]
        source_hash = row["source_cell_sha256"]
        if not isinstance(repeat_id, str) or not repeat_id.strip():
            raise G3PanelError("hidden repeat IDs must be non-empty strings")
        if row["kind"] != "hidden_repeat":
            raise G3PanelError(f"{repeat_id}: hidden repeat is not labelled")
        if not _is_sha256(source_hash) or source_hash not in cells_by_source_hash:
            raise G3PanelError(f"{repeat_id}: hidden repeat source hash is invalid")
        original = _panel(row["original_grades"], f"{repeat_id} original grades")
        repeated = _panel(row["repeat_grades"], f"{repeat_id} repeat grades")
        expected_original = cells_by_source_hash[source_hash]["individual_grades"]
        if sha256_json(original) != sha256_json(expected_original):
            raise G3PanelError(
                f"{repeat_id}: original grades differ from the shared calibration cell"
            )
        normalized.append(
            {
                "repeat_id": repeat_id,
                "kind": "hidden_repeat",
                "source_cell_sha256": source_hash,
                "original_grades": original,
                "repeat_grades": repeated,
            }
        )
    normalized.sort(key=lambda row: row["repeat_id"])
    repeat_ids = [row["repeat_id"] for row in normalized]
    source_hashes = [row["source_cell_sha256"] for row in normalized]
    if len(repeat_ids) != len(set(repeat_ids)):
        raise G3PanelError("hidden repeat IDs must be unique")
    if len(source_hashes) != len(set(source_hashes)):
        raise G3PanelError("each calibration cell may be repeated at most once")
    if len(normalized) != G3_PANEL_HIDDEN_REPEAT_COUNT:
        raise G3PanelError("G3 panel calibration requires exactly two hidden repeats")
    return normalized


def _ai_invocation_receipts(values: object) -> list[dict[str, str]]:
    if not isinstance(values, list) or len(values) != len(AI_REVIEWERS):
        raise G3PanelError(
            "G3 panel calibration requires one invocation receipt per AI reviewer"
        )
    normalized: list[dict[str, str]] = []
    for reviewer, row in zip(AI_REVIEWERS, values, strict=True):
        if not isinstance(row, Mapping) or set(row) != _AI_INVOCATION_COMMITMENT_FIELDS:
            raise G3PanelError("G3 panel AI invocation commitment fields differ")
        expected = G3_CALIBRATION_AI_INVOCATIONS[reviewer]
        if row.get("reviewer") != reviewer or any(
            row.get(key) != value for key, value in expected.items()
        ):
            raise G3PanelError("G3 panel AI invocation identity changed")
        for field in (
            "review_manifest_sha256",
            "token_confirmation_artifact_sha256",
            "token_confirmation_file_sha256",
            "packet_token_preflight_sha256",
            "completed_return_artifact_sha256",
            "completed_return_file_sha256",
            "invocation_receipt_artifact_sha256",
            "invocation_receipt_file_sha256",
        ):
            if not _is_sha256(row.get(field)):
                raise G3PanelError(f"G3 panel {field} is invalid")
        normalized.append({key: str(row[key]) for key in row})
    return normalized


def _hidden_repeat_consistency(
    rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]] | None:
    if not rows:
        return None
    count = len(rows)
    result: dict[str, dict[str, Any]] = {}
    for reviewer in REVIEWERS:
        differences = [
            abs(
                int(row["original_grades"][reviewer]["overall_ordinal"])
                - int(row["repeat_grades"][reviewer]["overall_ordinal"])
            )
            for row in rows
        ]
        accepted_matches = sum(
            row["original_grades"][reviewer]["accepted"]
            == row["repeat_grades"][reviewer]["accepted"]
            for row in rows
        )
        result[reviewer] = {
            "pairs": count,
            "exact_ordinal_rate": sum(value == 0 for value in differences) / count,
            "within_one_ordinal_rate": sum(value <= 1 for value in differences) / count,
            "accepted_match_rate": accepted_matches / count,
            "mean_absolute_ordinal_difference": sum(differences) / count,
        }
    return result


def _ambiguity_decisions(value: object) -> dict[str, bool]:
    if (
        not isinstance(value, Mapping)
        or set(value) != set(REVIEWERS)
        or any(not isinstance(decision, bool) for decision in value.values())
    ):
        raise G3PanelError("calibration requires one ambiguity decision per reviewer")
    return {reviewer: value[reviewer] for reviewer in REVIEWERS}


def _approved(
    metrics: Mapping[str, Mapping[str, Any]],
    ai_vs_kevin: Mapping[str, Mapping[str, Any]],
    ambiguity: Mapping[str, bool],
) -> bool:
    reference_pass = all(
        values["exact_ordinal_rate"] >= CALIBRATION_EXACT_ORDINAL_MIN
        and values["within_one_ordinal_rate"] >= CALIBRATION_WITHIN_ONE_MIN
        and values["accepted_match_rate"] >= CALIBRATION_ACCEPTED_MATCH_MIN
        for values in metrics.values()
    )
    ai_kevin_pass = all(
        values["accepted_match_rate"] >= AI_KEVIN_ACCEPTED_MATCH_MIN
        and values["mean_absolute_ordinal_difference"]
        <= AI_KEVIN_MEAN_ABSOLUTE_DIFFERENCE_MAX
        for values in ai_vs_kevin.values()
    )
    return reference_pass and ai_kevin_pass and not any(ambiguity.values())


def _reference_sha256(cells: list[dict[str, Any]]) -> str:
    return sha256_json(
        [
            {
                "canonical_cell_id": cell["canonical_cell_id"],
                **cell["reference_grade"],
            }
            for cell in cells
        ]
    )


def build_g3_panel_calibration(
    calibration_cells: Iterable[Mapping[str, Any]],
    *,
    review_manifest_sha256: str,
    identity_map_sha256: str,
    ai_invocation_receipts: list[Mapping[str, Any]],
    rubric_ambiguity_by_reviewer: Mapping[str, bool],
    hidden_repeats: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build a public calibration receipt without questions, answers, or evidence text."""
    if not _is_sha256(review_manifest_sha256) or not _is_sha256(identity_map_sha256):
        raise G3PanelError(
            "review manifest and identity map hashes must be lowercase SHA-256"
        )
    cells = _calibration_cells(calibration_cells, aggregates_required=False)
    cells_by_source = {cell["source_cell_sha256"]: cell for cell in cells}
    repeats = _hidden_repeats(hidden_repeats, cells_by_source)
    invocation_receipts = _ai_invocation_receipts(ai_invocation_receipts)
    if any(
        row["review_manifest_sha256"] != review_manifest_sha256
        for row in invocation_receipts
    ):
        raise G3PanelError("G3 panel AI invocation manifest binding changed")
    ambiguity = _ambiguity_decisions(rubric_ambiguity_by_reviewer)
    metrics = _agreement_vs_reference(cells)
    ai_comparisons = _ai_vs_kevin(cells)
    approved = _approved(metrics, ai_comparisons, ambiguity)
    artifact: dict[str, Any] = {
        "schema_version": G3_PANEL_CALIBRATION_SCHEMA,
        "reviewers": list(REVIEWERS),
        "ai_reviewers": list(AI_REVIEWERS),
        "sole_human_reviewer": SOLE_HUMAN_REVIEWER,
        "review_manifest_sha256": review_manifest_sha256,
        "identity_map_sha256": identity_map_sha256,
        "reference_sha256": _reference_sha256(cells),
        "cell_count_per_reviewer": CALIBRATION_CELL_COUNT,
        "cells": cells,
        "cells_sha256": sha256_json(cells),
        "ai_invocation_receipts": invocation_receipts,
        "ai_invocation_receipts_sha256": sha256_json(invocation_receipts),
        "metrics_vs_reference": metrics,
        "ai_vs_kevin": ai_comparisons,
        "hidden_repeats": repeats,
        "hidden_repeats_sha256": sha256_json(repeats),
        "hidden_repeat_consistency_by_reviewer": _hidden_repeat_consistency(repeats),
        "rubric_ambiguity_by_reviewer": ambiguity,
        "thresholds": dict(_THRESHOLDS),
        "status": "approved" if approved else "restart_required",
    }
    artifact["artifact_sha256"] = sha256_json(artifact)
    validate_g3_panel_calibration(artifact)
    return artifact


def validate_g3_panel_calibration(value: Mapping[str, Any]) -> None:
    """Validate hashes, exact fields, grades, metrics, and the derived gate status."""
    if not isinstance(value, Mapping) or set(value) != _ARTIFACT_FIELDS:
        raise G3PanelError("G3 panel calibration artifact fields differ")
    if value["schema_version"] != G3_PANEL_CALIBRATION_SCHEMA:
        raise G3PanelError("unsupported G3 panel calibration schema")
    artifact_hash = value["artifact_sha256"]
    body = {key: item for key, item in value.items() if key != "artifact_sha256"}
    if not _is_sha256(artifact_hash) or artifact_hash != sha256_json(body):
        raise G3PanelError("G3 panel calibration artifact hash is invalid")
    if (
        value["reviewers"] != list(REVIEWERS)
        or value["ai_reviewers"] != list(AI_REVIEWERS)
        or value["sole_human_reviewer"] != SOLE_HUMAN_REVIEWER
    ):
        raise G3PanelError(
            "G3 panel must contain exactly two AIs and Kevin as sole human"
        )
    if not _is_sha256(value["review_manifest_sha256"]) or not _is_sha256(
        value["identity_map_sha256"]
    ):
        raise G3PanelError("G3 panel source hashes are invalid")
    if value["thresholds"] != _THRESHOLDS:
        raise G3PanelError("G3 panel calibration thresholds differ from review.py")
    if value["cell_count_per_reviewer"] != CALIBRATION_CELL_COUNT:
        raise G3PanelError("G3 panel calibration cell count differs")
    raw_cells = value["cells"]
    if not isinstance(raw_cells, list):
        raise G3PanelError("G3 panel calibration cells must be a list")
    cells = _calibration_cells(raw_cells, aggregates_required=True)
    if sha256_json(raw_cells) != sha256_json(cells):
        raise G3PanelError("G3 panel calibration cells are not canonical")
    if value["cells_sha256"] != sha256_json(cells):
        raise G3PanelError("G3 panel calibration cells hash is invalid")
    invocation_receipts = _ai_invocation_receipts(value["ai_invocation_receipts"])
    if value["ai_invocation_receipts_sha256"] != sha256_json(invocation_receipts):
        raise G3PanelError("G3 panel AI invocation-receipt hash is invalid")
    if value["reference_sha256"] != _reference_sha256(cells):
        raise G3PanelError("G3 panel calibration reference hash is invalid")
    metrics = _agreement_vs_reference(cells)
    if sha256_json(value["metrics_vs_reference"]) != sha256_json(metrics):
        raise G3PanelError("G3 panel calibration agreement metrics differ")
    ai_comparisons = _ai_vs_kevin(cells)
    if sha256_json(value["ai_vs_kevin"]) != sha256_json(ai_comparisons):
        raise G3PanelError("G3 panel AI-versus-Kevin metrics differ")
    raw_repeats = value["hidden_repeats"]
    if not isinstance(raw_repeats, list):
        raise G3PanelError("G3 panel hidden repeats must be a list")
    repeats = _hidden_repeats(
        raw_repeats, {cell["source_cell_sha256"]: cell for cell in cells}
    )
    if sha256_json(raw_repeats) != sha256_json(repeats):
        raise G3PanelError("G3 panel hidden repeats are not canonical")
    if value["hidden_repeats_sha256"] != sha256_json(repeats):
        raise G3PanelError("G3 panel hidden repeats hash is invalid")
    repeat_metrics = _hidden_repeat_consistency(repeats)
    if sha256_json(value["hidden_repeat_consistency_by_reviewer"]) != sha256_json(
        repeat_metrics
    ):
        raise G3PanelError("G3 panel hidden-repeat consistency differs")
    ambiguity = _ambiguity_decisions(value["rubric_ambiguity_by_reviewer"])
    expected_status = (
        "approved"
        if _approved(metrics, ai_comparisons, ambiguity)
        else "restart_required"
    )
    if value["status"] != expected_status:
        raise G3PanelError(
            "G3 panel calibration status is not derived from measured evidence"
        )
