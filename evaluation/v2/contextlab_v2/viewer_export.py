"""Build the immutable public ContextLab viewer export after the G3 gate.

The viewer is a projection of saved public evidence, not a second evaluator.  This
module therefore rebuilds the G2 and G3 gates, selects showcase cases from saved
receipts, binds every displayed metric to a file hash, run ID, and JSON pointer,
and copies only an explicit public allow-list into ``viewer/public``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import stat
from typing import Any

from .baseline import repository_root
from .g2_gate import build_g2_final_gate
from .g2_gate_io import load_canonical_g2_gate_inputs
from .g3_gate import (
    G3_AI_GATE_INVOCATION_RECEIPT_PATHS,
    G3_AI_GATE_REVIEW_PATHS,
    G3_GATE_SCHEMA,
    G3_PENDING_GATE_PATH,
    validate_g3_ai_gate_review_provenance,
    validate_g3_final_gate,
    validate_g3_pending_gate,
)
from .immutable_io import ImmutableIOError, read_bytes_snapshot
from .memory_experiments import MEMORY_CONFIGURATIONS
from .tasking import sha256_json, task_catalog


VIEWER_SCHEMA_VERSION = "contextlab.viewer.v1"
VIEWER_MANIFEST_SCHEMA = "contextlab.viewer-export-manifest.v1"
VIEWER_EXPORT_PATH = Path("viewer/public/contextlab-viewer.v1.json")
VIEWER_ARTIFACT_ROOT = Path("viewer/public/artifacts")
VIEWER_MANIFEST_PATH = Path("results/v2/viewer/g4_export_manifest.json")

G2_GATE_PATH = Path("results/v2/gates/G2.json")
G3_GATE_PATH = Path("results/v2/gates/G3.json")
G2_COMPONENT_LAB_PATH = Path("results/v2/retrieval/public_component_lab.json")
G2_COMPONENT_ANALYSIS_PATH = Path(
    "results/v2/reports/g2_public_component_analysis.json"
)
G3_TEMPORAL_R0_LAB_PATH = Path("results/v2/retrieval/g3_temporal_r0_lab.json")
G3_FREEZE_PATH = Path("results/v2/memory/g3_public_freeze.json")
G3_PUBLIC_RUN_PATH = Path("results/v2/memory/g3_public_generation_run.json")
G3_PUBLIC_METRICS_PATH = Path("results/v2/memory/g3_public_metrics.json")
G3_LIFECYCLE_PATH = Path("results/v2/memory/g3_lifecycle_evidence.json")
G3_PANEL_PATH = Path("results/v2/memory/g3_panel_calibration.json")
G3_FAILURE_PATH = Path("results/v2/memory/g3_failure_and_harm_report.json")
G3_UNSUPPORTED_MEMORY_REVIEW_PATH = Path(
    "results/v2/reviews/g3_unsupported_memory_dispositions.json"
)
G3_UNSUPPORTED_MEMORY_REVIEW_APPROVAL_PATH = Path(
    "results/v2/reviews/g3_unsupported_memory_dispositions_kevin_approval.json"
)
G3_SEALED_CANDIDATES_PATH = Path("results/v2/memory/g3_sealed_candidates.json")
G3_SEALED_IMPORT_PATH = Path("results/v2/memory/g3_sealed_import.json")
TEMPORAL_EVENTS_PATH = Path("novalearn_synthetic_corpus/v2/temporal_events.jsonl")
MEMORY_PROTOCOL_PATH = Path("evaluation/v2/memory_protocol.json")
REVIEW_PROTOCOL_PATH = Path("evaluation/v2/review_protocol.json")
ROADMAP_PATH = Path("docs/CONTEXTLAB_V2_DETAILED_ROADMAP.md")

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_INVALID_JSON_POINTER_ESCAPE = re.compile(r"~(?![01])")
_JSON_ARRAY_INDEX = re.compile(r"0|[1-9][0-9]*\Z")
_EVENT_ID = re.compile(r"TL-\d{2}-E\d{2}")
_LOCAL_ANGLE_PATH = re.compile(r"</(?:Users|Volumes)/[^>]+>")
_LOCAL_FILE_URI = re.compile(r"file:///(?:Users|Volumes)/[^\s\"')>]+")
_LOCAL_ABSOLUTE_PATH = re.compile(r"/(?:Users|Volumes)/[^\s\"')>,]+")
_LOCAL_LOCATION_MARKERS = (b"/Users/", b"/Volumes/")
_STATIC_SECTION_ID = re.compile(r"NL-\d{3}-S\d{2}")
_STAGE_KINDS = (
    "retrieval",
    "fusion",
    "reranking",
    "deduplication",
    "diversity",
    "budget",
    "context",
)
_POLICY_LABELS = {
    "M0": ("M0 · no memory", "Corpus evidence only; no persistent memory writes."),
    "M1": ("M1 · bounded memory", "A small evidence-linked claim memory."),
    "M2": ("M2 · lifecycle memory", "Evidence-linked claims with lifecycle handling."),
    "M3": ("M3 · governed memory", "Lifecycle memory with governed retrieval."),
    "M4": ("M4 · episodic memory", "Governed memory plus trusted outcome episodes."),
}

# These tokens are never allowed in a file copied into the public viewer.  G3
# gate validation may read content-free sealed commitments, but they are not
# members of the publication catalog.
VIEWER_FORBIDDEN_PUBLIC_TOKENS = (
    "evaluation_only_do_not_index",
    "protected",
    "sealed",
    "gold",
    "/grades/",
    "scoring",
)
# G3 result receipts contain internal grader links and score inputs alongside the
# public execution trace.  The viewer must never publish those receipt fields.
# Keep this list deliberately about field names (rather than prose values): a
# public answer may legitimately use a word such as "gold", but it must not
# carry a protected grading field.
VIEWER_FORBIDDEN_RECEIPT_FIELD_FRAGMENTS = (
    "accept",
    "accur",
    "correct",
    "grade",
    "gold",
    "outcome",
    "pass",
    "protected",
    "scor",
    "sealed",
    "verdict",
)
VIEWER_PUBLIC_SOURCE_PREFIXES = (
    "docs/",
    "evaluation/v2/memory_protocol.json",
    "evaluation/v2/prompts/",
    "evaluation/v2/review_protocol.json",
    "novalearn_synthetic_corpus/corpus/",
    "novalearn_synthetic_corpus/v2/temporal_events.jsonl",
    "results/v2/gates/G2.json",
    "results/v2/gates/G3.json",
    "results/v2/generations/public/",
    "results/v2/memory/g3_lifecycle_evidence.json",
    "results/v2/memory/receipts/",
    "results/v2/reports/g2_public_component_analysis.json",
    "results/v2/retrieval/g3_temporal_r0_lab.json",
    "results/v2/retrieval/public_component_lab.json",
)


class ViewerExportError(ValueError):
    """The public viewer projection is unsafe, stale, or unsupported."""


@dataclass(frozen=True)
class _Evidence:
    g2_gate: dict[str, Any]
    g3_gate: dict[str, Any]
    g2_lab: dict[str, Any]
    g2_analysis: dict[str, Any]
    temporal_r0_lab: dict[str, Any]
    g3_freeze: dict[str, Any]
    public_run: dict[str, Any]
    receipts: tuple[dict[str, Any], ...]
    metrics: dict[str, Any]
    lifecycle: dict[str, Any]
    panel: dict[str, Any]
    failure: dict[str, Any]
    review_protocol: dict[str, Any]
    events: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class _ReplayedG3Evidence:
    gate: dict[str, Any]
    generated_at: str
    freeze: dict[str, Any]
    public_run: dict[str, Any]
    receipts: tuple[dict[str, Any], ...]
    metrics: dict[str, Any]
    lifecycle: dict[str, Any]
    panel: dict[str, Any]
    failure: dict[str, Any]


@dataclass(frozen=True)
class _Selection:
    groups: tuple[tuple[str, str], ...]
    temporal_group: tuple[str, str]
    baseline_run_id: str
    memory_evidence_run_id: str
    execution_failure_run_id: str | None
    retrieval_win: dict[str, Any]
    timeline_event_ids: tuple[str, ...]


@dataclass(frozen=True)
class _RetrieverTrace:
    run_id: str
    artifact: Mapping[str, Any]
    trace_pointer: str
    scores: Mapping[str, tuple[float, str]]


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_relative_path(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ViewerExportError(f"{label} path is missing")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ViewerExportError(f"{label} path escapes the repository")
    return path


def _contained_path(
    root: Path, relative: Path, *, label: str, must_exist: bool = True
) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise ViewerExportError(f"{label} must be repository-relative")
    candidate = root / relative
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ViewerExportError(f"{label} must not traverse a symlink")
    try:
        resolved = candidate.resolve(strict=must_exist)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        raise ViewerExportError(
            f"{label} is missing or outside the repository"
        ) from exc
    if must_exist and not resolved.is_file():
        raise ViewerExportError(f"{label} is not a saved file")
    return resolved


def _read_source_bytes(root: Path, relative: Path, label: str) -> bytes:
    """Read one public source through a stable no-follow descriptor chain."""

    try:
        return read_bytes_snapshot(root, relative)
    except ImmutableIOError as exc:
        raise ViewerExportError(f"{label} is not a stable repository file") from exc


def _read_json(root: Path, relative: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(_read_source_bytes(root, relative, label).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ViewerExportError(f"cannot read {label}") from exc
    if not isinstance(value, dict):
        raise ViewerExportError(f"{label} must be a JSON object")
    return value


def _read_events(root: Path) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    try:
        source = _read_source_bytes(root, TEMPORAL_EVENTS_PATH, "temporal event stream")
        for line_number, line in enumerate(
            source.decode("utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ViewerExportError(
                    f"temporal event line {line_number} is not an object"
                )
            value = dict(value)
            value["_viewer_line"] = line_number
            rows.append(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ViewerExportError("cannot read the public temporal event stream") from exc
    ids = [row.get("event_id") for row in rows]
    if (
        not rows
        or any(not isinstance(item, str) for item in ids)
        or len(ids) != len(set(ids))
    ):
        raise ViewerExportError("public temporal event IDs are missing or duplicated")
    return tuple(rows)


def _valid_internal_hash(value: Mapping[str, Any]) -> bool:
    digest = value.get("artifact_sha256")
    return isinstance(digest, str) and digest == sha256_json(
        {key: item for key, item in value.items() if key != "artifact_sha256"}
    )


def _require_approved_g3_gate(gate: Mapping[str, Any]) -> str:
    if gate.get("schema_version") != G3_GATE_SCHEMA or not _valid_internal_hash(gate):
        raise ViewerExportError("canonical G3 gate is missing, altered, or unsupported")
    human = gate.get("human_decision")
    decision = human.get("decision_record") if isinstance(human, Mapping) else None
    if (
        gate.get("technical_complete") is not True
        or gate.get("technical_disposition") not in {"promotion-ready", "retain-simple"}
        or gate.get("final_decision") not in {"promote", "retain-simple"}
        or not isinstance(human, Mapping)
        or human.get("status") != "recorded"
        or not isinstance(decision, Mapping)
        or decision.get("reviewer") != "Kevin Araujo"
        or decision.get("reviewer_role") != "sole_human_reviewer"
        or decision.get("decision") not in {"promote", "retain-simple"}
    ):
        raise ViewerExportError(
            "G4 export requires a technically complete G3 gate and Kevin's explicit decision"
        )
    decided_at = decision.get("decided_at")
    if (
        not isinstance(decided_at, str)
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", decided_at) is None
    ):
        raise ViewerExportError("Kevin's G3 decision timestamp is missing or invalid")
    return decided_at


def _require_approved_g2_gate(gate: Mapping[str, Any]) -> None:
    human = gate.get("human_approval")
    approval = human.get("approval_record") if isinstance(human, Mapping) else None
    if (
        not _valid_internal_hash(gate)
        or gate.get("final_decision") not in {"promote", "retain-simple"}
        or not isinstance(human, Mapping)
        or human.get("status") != "approved"
        or human.get("reviewer") != "Kevin Araujo"
        or not isinstance(approval, Mapping)
        or approval.get("reviewer") != "Kevin Araujo"
        or approval.get("decision") != "approved"
    ):
        raise ViewerExportError("G2 gate is not a validated Kevin-approved decision")


def _validate_g2(root: Path, saved_gate: Mapping[str, Any]) -> None:
    try:
        rebuilt = build_g2_final_gate(**load_canonical_g2_gate_inputs(root))
    except Exception as exc:
        raise ViewerExportError("canonical G2 evidence does not rebuild") from exc
    if dict(saved_gate) != rebuilt:
        raise ViewerExportError("saved G2 gate differs from its canonical evidence")


def _load_receipts(
    root: Path, public_run: Mapping[str, Any]
) -> tuple[dict[str, Any], ...]:
    cells = public_run.get("cells")
    if not isinstance(cells, list) or not cells:
        raise ViewerExportError("public G3 run contains no saved cells")
    receipts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, cell in enumerate(cells):
        if not isinstance(cell, Mapping):
            raise ViewerExportError(f"public G3 cell {index} is not an object")
        relative = _safe_relative_path(cell.get("receipt_path"), f"G3 cell {index}")
        if not relative.as_posix().startswith("results/v2/memory/receipts/"):
            raise ViewerExportError(
                "G3 receipt path is outside the public receipt tree"
            )
        receipt = _read_json(root, relative, f"G3 receipt {index}")
        run_id = receipt.get("run_id")
        if not isinstance(run_id, str) or run_id in seen:
            raise ViewerExportError(
                "G3 public receipts have missing or duplicate run IDs"
            )
        if cell.get("run_id") != run_id:
            raise ViewerExportError("G3 public-run cell and receipt run IDs differ")
        seen.add(run_id)
        receipts.append(receipt)
    return tuple(receipts)


def _validate_g3(
    root: Path,
    gate: Mapping[str, Any],
    *,
    freeze: Mapping[str, Any],
    public_run: Mapping[str, Any],
    receipts: Sequence[Mapping[str, Any]],
    metrics: Mapping[str, Any],
    sealed_candidates: Mapping[str, Any],
    sealed_import: Mapping[str, Any],
    lifecycle: Mapping[str, Any],
    panel: Mapping[str, Any],
    failure: Mapping[str, Any],
    unsupported_memory_review: Mapping[str, Any],
    unsupported_memory_review_approval: Mapping[str, Any],
) -> None:
    try:
        validate_g3_final_gate(
            gate,
            g3_freeze=freeze,
            public_run=public_run,
            public_receipts=receipts,
            public_metrics=metrics,
            sealed_candidate_manifest=sealed_candidates,
            sealed_import=sealed_import,
            lifecycle_evidence=lifecycle,
            panel_calibration=panel,
            failure_report=failure,
            unsupported_memory_review=unsupported_memory_review,
            unsupported_memory_review_approval=(unsupported_memory_review_approval),
            root=root,
        )
        pending = _read_json(root, G3_PENDING_GATE_PATH, "pending G3 gate")
        validate_g3_pending_gate(pending)
        if (
            gate.get("pending_gate_artifact_sha256") != pending["artifact_sha256"]
            or gate.get("technical_record_sha256") != pending["technical_record_sha256"]
        ):
            raise ViewerExportError("approved G3 gate differs from its pending record")
        embedded_reviews = gate.get("ai_gate_reviews")
        reviewers = ("gpt-5.6-sol-high", "claude-opus-5-medium")
        if not isinstance(embedded_reviews, list) or len(embedded_reviews) != 2:
            raise ViewerExportError("approved G3 gate lacks both AI reviews")
        for index, reviewer in enumerate(reviewers):
            receipt = _read_json(
                root,
                G3_AI_GATE_INVOCATION_RECEIPT_PATHS[reviewer],
                f"{reviewer} G3 invocation receipt",
            )
            review = _read_json(
                root,
                G3_AI_GATE_REVIEW_PATHS[reviewer],
                f"{reviewer} G3 gate review",
            )
            if (
                review != embedded_reviews[index]
                or review.get("invocation_receipt") != receipt
            ):
                raise ViewerExportError("approved G3 AI review artifacts differ")
            validate_g3_ai_gate_review_provenance(
                root, pending_gate=pending, review=review
            )
    except Exception as exc:
        raise ViewerExportError(
            "canonical G3 gate does not replay from exact evidence"
        ) from exc


def _load_replayed_g3_evidence(root: Path) -> _ReplayedG3Evidence:
    """Load and replay the exact approved canonical G3 gate and its evidence."""

    # Keep the approval barrier first. A missing or pending gate must fail before
    # large evidence files are read or a public output directory can be created.
    g3_gate = _read_json(root, G3_GATE_PATH, "G3 final gate")
    generated_at = _require_approved_g3_gate(g3_gate)
    freeze = _read_json(root, G3_FREEZE_PATH, "G3 public freeze")
    public_run = _read_json(root, G3_PUBLIC_RUN_PATH, "G3 public run")
    receipts = _load_receipts(root, public_run)
    metrics = _read_json(root, G3_PUBLIC_METRICS_PATH, "G3 public metrics")
    lifecycle = _read_json(root, G3_LIFECYCLE_PATH, "G3 lifecycle evidence")
    panel = _read_json(root, G3_PANEL_PATH, "G3 panel calibration")
    failure = _read_json(root, G3_FAILURE_PATH, "G3 failure report")
    unsupported_memory_review = _read_json(
        root,
        G3_UNSUPPORTED_MEMORY_REVIEW_PATH,
        "G3 unsupported-memory disposition report",
    )
    unsupported_memory_review_approval = _read_json(
        root,
        G3_UNSUPPORTED_MEMORY_REVIEW_APPROVAL_PATH,
        "G3 unsupported-memory disposition Kevin approval",
    )
    sealed_candidates = _read_json(
        root, G3_SEALED_CANDIDATES_PATH, "G3 sealed candidate commitment"
    )
    sealed_import = _read_json(
        root, G3_SEALED_IMPORT_PATH, "G3 content-free sealed import"
    )
    _validate_g3(
        root,
        g3_gate,
        freeze=freeze,
        public_run=public_run,
        receipts=receipts,
        metrics=metrics,
        sealed_candidates=sealed_candidates,
        sealed_import=sealed_import,
        lifecycle=lifecycle,
        panel=panel,
        failure=failure,
        unsupported_memory_review=unsupported_memory_review,
        unsupported_memory_review_approval=unsupported_memory_review_approval,
    )
    return _ReplayedG3Evidence(
        gate=g3_gate,
        generated_at=generated_at,
        freeze=freeze,
        public_run=public_run,
        receipts=receipts,
        metrics=metrics,
        lifecycle=lifecycle,
        panel=panel,
        failure=failure,
    )


def require_replayed_approved_g3_gate(root: Path | None = None) -> dict[str, Any]:
    """Require the saved approved G3 gate to replay from its canonical evidence."""

    repository = (root or repository_root()).resolve()
    return dict(_load_replayed_g3_evidence(repository).gate)


def _load_evidence(root: Path) -> tuple[_Evidence, str]:
    g3 = _load_replayed_g3_evidence(root)

    g2_gate = _read_json(root, G2_GATE_PATH, "G2 final gate")
    _require_approved_g2_gate(g2_gate)
    _validate_g2(root, g2_gate)

    evidence = _Evidence(
        g2_gate=g2_gate,
        g3_gate=g3.gate,
        g2_lab=_read_json(root, G2_COMPONENT_LAB_PATH, "G2 component lab"),
        g2_analysis=_read_json(
            root, G2_COMPONENT_ANALYSIS_PATH, "G2 component analysis"
        ),
        temporal_r0_lab=_read_json(
            root, G3_TEMPORAL_R0_LAB_PATH, "G3 temporal R0 retrieval lab"
        ),
        g3_freeze=g3.freeze,
        public_run=g3.public_run,
        receipts=g3.receipts,
        metrics=g3.metrics,
        lifecycle=g3.lifecycle,
        panel=g3.panel,
        failure=g3.failure,
        review_protocol=_read_json(root, REVIEW_PROTOCOL_PATH, "review protocol"),
        events=_read_events(root),
    )
    return evidence, g3.generated_at


def _receipt_task(receipt: Mapping[str, Any]) -> Mapping[str, Any]:
    run_spec = receipt.get("run_spec")
    task = run_spec.get("task") if isinstance(run_spec, Mapping) else None
    if not isinstance(task, Mapping):
        raise ViewerExportError("G3 receipt omits its frozen task")
    return task


def _receipt_groups(
    receipts: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], dict[str, Mapping[str, Any]]]:
    grouped: dict[tuple[str, str], dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for receipt in receipts:
        task = _receipt_task(receipt)
        if task.get("suite") not in {"temporal", "static"}:
            raise ViewerExportError("G3 receipt names an unsupported public suite")
        task_id = task.get("task_id")
        effort = receipt.get("reasoning_effort")
        policy = receipt.get("policy")
        if not all(
            isinstance(item, str) and item for item in (task_id, effort, policy)
        ):
            raise ViewerExportError("G3 temporal receipt identity is incomplete")
        key = (str(task_id), str(effort))
        if str(policy) in grouped[key]:
            raise ViewerExportError("G3 temporal factorial contains a duplicate lane")
        grouped[key][str(policy)] = receipt
    return {
        key: rows
        for key, rows in grouped.items()
        if tuple(rows) and set(rows) == set(MEMORY_CONFIGURATIONS)
    }


def _receipt_event_ids(receipt: Mapping[str, Any]) -> set[str]:
    answer_ids = set(_EVENT_ID.findall(str(receipt.get("answer", ""))))
    if answer_ids:
        return answer_ids
    ids: set[str] = set()
    trace = receipt.get("trace")
    if isinstance(trace, Mapping):
        for field in (
            "selected_corpus_evidence",
            "selected_memory_evidence",
        ):
            rows = trace.get(field, [])
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                values: list[object] = [row.get("evidence_id")]
                raw_ids = row.get("raw_evidence_ids", [])
                if isinstance(raw_ids, list):
                    values.extend(raw_ids)
                for value in values:
                    if isinstance(value, str):
                        ids.update(_EVENT_ID.findall(value))
    # Prefer explicit answer citations. Fall back to selected public evidence only
    # when an answer contains no event ID. Private grading is never an input.
    return ids


def _related_timeline_ids(
    baseline: Mapping[str, Any],
    memory_run: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> tuple[str, ...] | None:
    wanted = _receipt_event_ids(baseline) | _receipt_event_ids(memory_run)
    by_scenario: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in events:
        if event.get("event_id") in wanted and isinstance(
            event.get("scenario_id"), str
        ):
            by_scenario[str(event["scenario_id"])].append(event)
    for scenario_id in sorted(by_scenario):
        rows = sorted(
            by_scenario[scenario_id],
            key=lambda row: (str(row.get("observed_time")), str(row.get("event_id"))),
        )
        if len(rows) < 2:
            continue
        for right_index in range(1, len(rows)):
            right = rows[right_index]
            for left in rows[:right_index]:
                left_id = left.get("event_id")
                related = (
                    right.get("supersedes_event_id") == left_id
                    or right.get("tombstone_for_event_id") == left_id
                    or (
                        right.get("subject") == left.get("subject")
                        and right.get("predicate") == left.get("predicate")
                        and right.get("value") != left.get("value")
                    )
                )
                if related:
                    return (str(left_id), str(right.get("event_id")))
    return None


def _public_run_is_displayable(receipt: Mapping[str, Any]) -> bool:
    trace = receipt.get("trace")
    numbers = (
        receipt.get("actual_usd"),
        receipt.get("latency_ms"),
        trace.get("context_token_count") if isinstance(trace, Mapping) else None,
    )
    return (
        isinstance(receipt.get("run_id"), str)
        and bool(receipt.get("run_id"))
        and isinstance(receipt.get("status"), str)
        and isinstance(trace, Mapping)
        and all(
            not isinstance(value, bool) and isinstance(value, (int, float))
            for value in numbers
        )
    )


def _has_selected_memory_evidence(receipt: Mapping[str, Any]) -> bool:
    trace = receipt.get("trace")
    rows = trace.get("selected_memory_evidence") if isinstance(trace, Mapping) else None
    return isinstance(rows, list) and any(isinstance(row, Mapping) for row in rows)


def _select_temporal_comparison(
    groups: Mapping[tuple[str, str], Mapping[str, Mapping[str, Any]]],
    events: Sequence[Mapping[str, Any]],
) -> tuple[tuple[str, str], str, str, tuple[str, ...]]:
    choices: list[tuple[tuple[str, str], int, str, str, tuple[str, ...]]] = []
    for key in sorted(groups):
        rows = groups[key]
        if _receipt_task(rows[MEMORY_CONFIGURATIONS[0]]).get("suite") != "temporal":
            continue
        baseline = rows["M0"]
        if baseline.get("status") != "completed" or not _public_run_is_displayable(
            baseline
        ):
            continue
        for policy_index, memory_policy in enumerate(MEMORY_CONFIGURATIONS[1:], 1):
            memory_run = rows[memory_policy]
            if (
                memory_run.get("status") != "completed"
                or not _public_run_is_displayable(memory_run)
                or not _has_selected_memory_evidence(memory_run)
            ):
                continue
            timeline = _related_timeline_ids(baseline, memory_run, events)
            if timeline is None:
                continue
            choices.append(
                (
                    key,
                    policy_index,
                    str(baseline["run_id"]),
                    str(memory_run["run_id"]),
                    timeline,
                )
            )
    if not choices:
        raise ViewerExportError(
            "no public temporal comparison links an M0 run, selected memory evidence, and an event transition"
        )
    key, _, baseline_id, memory_id, timeline = min(choices)
    return key, baseline_id, memory_id, timeline


def _select_execution_failure(
    groups: Mapping[tuple[str, str], Mapping[str, Mapping[str, Any]]],
) -> tuple[tuple[str, str], str] | None:
    choices: list[tuple[tuple[str, str], int, str]] = []
    for key in sorted(groups):
        for policy_index, policy in enumerate(MEMORY_CONFIGURATIONS):
            receipt = groups[key][policy]
            if receipt.get("status") == "failed" and _public_run_is_displayable(
                receipt
            ):
                choices.append((key, policy_index, str(receipt["run_id"])))
    if not choices:
        return None
    key, _, run_id = min(choices)
    return key, run_id


def _select_retrieval_win(
    lab: Mapping[str, Any], analysis: Mapping[str, Any]
) -> dict[str, Any]:
    methods = analysis.get("methods")
    traces = lab.get("traces")
    if not isinstance(methods, Mapping) or not isinstance(traces, list):
        raise ViewerExportError("G2 component evidence is incomplete")
    indexed: dict[tuple[str, str], tuple[int, Mapping[str, Any]]] = {}
    for index, trace in enumerate(traces):
        if not isinstance(trace, Mapping):
            continue
        task = trace.get("task")
        task_id = task.get("task_id") if isinstance(task, Mapping) else None
        strategy = trace.get("strategy_id")
        if isinstance(task_id, str) and isinstance(strategy, str):
            indexed[(task_id, strategy)] = (index, trace)
    wins: list[tuple[float, str, dict[str, Any]]] = []
    for strategy, method in methods.items():
        if not isinstance(strategy, str) or not isinstance(method, Mapping):
            continue
        parent = method.get("parent")
        metric = method.get("primary_metric")
        if not isinstance(parent, str) or not isinstance(metric, str):
            continue
        for (task_id, lane), (index, candidate) in indexed.items():
            if lane != strategy or (task_id, parent) not in indexed:
                continue
            parent_index, parent_trace = indexed[(task_id, parent)]
            candidate_metrics = candidate.get("component_metrics")
            parent_metrics = parent_trace.get("component_metrics")
            if not isinstance(candidate_metrics, Mapping) or not isinstance(
                parent_metrics, Mapping
            ):
                continue
            left = parent_metrics.get(metric)
            right = candidate_metrics.get(metric)
            if (
                isinstance(left, bool)
                or isinstance(right, bool)
                or not isinstance(left, (int, float))
                or not isinstance(right, (int, float))
                or float(right) <= float(left)
            ):
                continue
            record = {
                "task_id": task_id,
                "strategy_id": strategy,
                "parent_strategy_id": parent,
                "metric": metric,
                "parent_value": float(left),
                "candidate_value": float(right),
                "parent_run_id": parent_trace.get("run_id"),
                "candidate_run_id": candidate.get("run_id"),
                "parent_json_pointer": f"/traces/{parent_index}/component_metrics/{metric}",
                "candidate_json_pointer": f"/traces/{index}/component_metrics/{metric}",
            }
            if not all(
                isinstance(record[field], str) and record[field]
                for field in ("parent_run_id", "candidate_run_id")
            ):
                continue
            wins.append((float(right) - float(left), task_id, record))
    if not wins:
        raise ViewerExportError(
            "no measured public G2 retrieval win supports the narrative"
        )
    return max(wins, key=lambda item: (item[0], item[1]))[2]


def _select_evidence(evidence: _Evidence) -> _Selection:
    groups = _receipt_groups(evidence.receipts)
    if not groups:
        raise ViewerExportError("G3 has no complete five-policy temporal comparison")
    temporal, baseline, memory_run, timeline = _select_temporal_comparison(
        groups, evidence.events
    )
    execution_failure = _select_execution_failure(groups)
    selected = {temporal}
    if execution_failure is not None:
        selected.add(execution_failure[0])
    for key in sorted(groups):
        if len(selected) >= 2:
            break
        if all(_public_run_is_displayable(receipt) for receipt in groups[key].values()):
            selected.add(key)
    return _Selection(
        groups=tuple(sorted(selected)),
        temporal_group=temporal,
        baseline_run_id=baseline,
        memory_evidence_run_id=memory_run,
        execution_failure_run_id=(
            execution_failure[1] if execution_failure is not None else None
        ),
        retrieval_win=_select_retrieval_win(evidence.g2_lab, evidence.g2_analysis),
        timeline_event_ids=timeline,
    )


def public_viewer_source_allowed(relative: Path) -> bool:
    """Return whether a repository artifact is in the frozen public allow-list."""

    text = relative.as_posix()
    lowered = f"/{text.lower()}"
    if any(token in lowered for token in VIEWER_FORBIDDEN_PUBLIC_TOKENS):
        return False
    return any(
        text.startswith(prefix) if prefix.endswith("/") else text == prefix
        for prefix in VIEWER_PUBLIC_SOURCE_PREFIXES
    )


def _media_type(path: Path) -> str:
    if path.suffix == ".jsonl":
        return "application/x-ndjson"
    value, _ = mimetypes.guess_type(path.name)
    return value or "application/octet-stream"


def _public_receipt_projection(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Return the only receipt shape that may be copied into the public bundle."""

    run_spec = receipt.get("run_spec")
    trace = receipt.get("trace")
    if not isinstance(run_spec, Mapping) or not isinstance(trace, Mapping):
        raise ViewerExportError(
            "public receipt is missing its saved run specification or trace"
        )
    task = run_spec.get("task")
    if not isinstance(task, Mapping):
        raise ViewerExportError("public receipt run specification has no task")

    def selected_mapping(
        value: Mapping[str, Any], fields: Sequence[str]
    ) -> dict[str, Any]:
        return {field: value[field] for field in fields if field in value}

    def evidence_rows(value: object) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        rows: list[dict[str, Any]] = []
        for row in value:
            if not isinstance(row, Mapping):
                raise ViewerExportError(
                    "public receipt trace has a malformed evidence row"
                )
            rows.append(
                selected_mapping(
                    row,
                    (
                        "evidence_id",
                        "rank",
                        "raw_evidence_ids",
                        "task_similarity",
                        "token_count",
                    ),
                )
            )
        return rows

    projection: dict[str, Any] = {
        "viewer_projection_schema": "contextlab.public-g3-receipt.v2",
        "run_id": receipt.get("run_id"),
        "policy": receipt.get("policy"),
        "reasoning_effort": receipt.get("reasoning_effort"),
        "provider": receipt.get("provider"),
        "requested_model": receipt.get("requested_model"),
        "resolved_model": receipt.get("resolved_model"),
        "answer": receipt.get("answer"),
        "actual_usd": receipt.get("actual_usd"),
        "latency_ms": receipt.get("latency_ms"),
        "status": receipt.get("status"),
        "run_spec": {
            "context_budget_tokens": run_spec.get("context_budget_tokens"),
            "prompt_version": run_spec.get("prompt_version"),
            "task": selected_mapping(
                task,
                ("question_text", "suite", "task_family", "task_id"),
            ),
        },
        "trace": {
            "context_token_count": trace.get("context_token_count"),
            **{
                field: evidence_rows(trace.get(field))
                for field in (
                    "corpus_candidate_evidence",
                    "memory_candidate_evidence",
                    "selected_corpus_evidence",
                    "selected_memory_evidence",
                )
            },
        },
    }
    return projection


def _public_freeze_projection(freeze: Mapping[str, Any]) -> dict[str, Any]:
    """Project only public run identities and configuration from the G3 freeze."""

    manifest = freeze.get("manifest")
    run_specs = manifest.get("run_specs") if isinstance(manifest, Mapping) else None
    if not isinstance(run_specs, list) or not run_specs:
        raise ViewerExportError("G3 freeze has no public run specifications")
    projected_specs: list[dict[str, Any]] = []
    for spec in run_specs:
        if not isinstance(spec, Mapping):
            raise ViewerExportError("G3 freeze has a malformed run specification")
        task = spec.get("task")
        if not isinstance(task, Mapping):
            raise ViewerExportError("G3 freeze run specification has no public task")
        projected_specs.append(
            {
                "campaign_id": spec.get("campaign_id"),
                "context_budget_tokens": spec.get("context_budget_tokens"),
                "output_token_limit": spec.get("output_token_limit"),
                "policy": spec.get("policy"),
                "prompt_version": spec.get("prompt_version"),
                "provider": spec.get("provider"),
                "reasoning_effort": spec.get("reasoning_effort"),
                "requested_model": spec.get("requested_model"),
                "run_id": spec.get("run_id"),
                "task": {
                    key: task.get(key)
                    for key in ("question_text", "suite", "task_family", "task_id")
                },
            }
        )
    projection = {
        "viewer_projection_schema": "contextlab.public-g3-freeze.v1",
        "manifest": {"run_specs": projected_specs},
    }
    _reject_forbidden_receipt_fields(projection, label="G3 public freeze")
    return projection


def _reject_forbidden_receipt_fields(
    value: object, *, label: str, path: str = "$"
) -> None:
    """Content-scan a queued receipt projection before it reaches viewer/public."""

    if isinstance(value, list):
        for index, item in enumerate(value):
            _reject_forbidden_receipt_fields(item, label=label, path=f"{path}[{index}]")
        return
    if not isinstance(value, Mapping):
        return
    for key, item in value.items():
        if not isinstance(key, str):
            raise ViewerExportError(
                f"public receipt projection {label} has a non-text key"
            )
        lowered = key.casefold()
        if any(
            fragment in lowered for fragment in VIEWER_FORBIDDEN_RECEIPT_FIELD_FRAGMENTS
        ):
            raise ViewerExportError(
                f"public receipt projection {label} contains a forbidden field at {path}.{key}"
            )
        _reject_forbidden_receipt_fields(item, label=label, path=f"{path}.{key}")


def _public_relative_local_reference(raw: str) -> str:
    value = raw.removeprefix("file://")
    parts = Path(value).parts
    for marker, include_marker in (
        ("TCC", False),
        ("AI-Brain", True),
        ("x-bookmarks", True),
    ):
        if marker in parts:
            start = parts.index(marker) + (0 if include_marker else 1)
            return Path(*parts[start:]).as_posix().replace(" ", "%20")
    return f"local-reference/{Path(value).name}".replace(" ", "%20")


def _contains_local_location(data: bytes) -> bool:
    return any(marker in data for marker in _LOCAL_LOCATION_MARKERS)


def _sanitize_public_text(data: bytes, source: Path) -> bytes:
    if source.suffix.casefold() not in {".json", ".jsonl", ".md", ".txt"}:
        return data
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ViewerExportError(f"text public artifact is not UTF-8: {source}") from exc
    text = _LOCAL_ANGLE_PATH.sub(
        lambda match: f"<{_public_relative_local_reference(match.group()[1:-1])}>",
        text,
    )
    text = _LOCAL_FILE_URI.sub(
        lambda match: _public_relative_local_reference(match.group()), text
    )
    text = _LOCAL_ABSOLUTE_PATH.sub(
        lambda match: _public_relative_local_reference(match.group()), text
    )
    projected = text.encode("utf-8")
    if _contains_local_location(projected):
        raise ViewerExportError(
            f"public projection still contains a local filesystem location: {source}"
        )
    return projected


class _ArtifactCatalog:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.copy_plan: dict[Path, bytes] = {}
        self.inventory: dict[str, dict[str, str]] = {}

    def artifact(self, relative: Path, kind: str, label: str) -> dict[str, str]:
        if relative.as_posix().startswith("results/v2/memory/receipts/"):
            raise ViewerExportError(
                "raw G3 receipts must use a sanitized public projection"
            )
        if not public_viewer_source_allowed(relative):
            raise ViewerExportError(
                f"artifact is not safe for public copying: {relative}"
            )
        data = _read_source_bytes(self.root, relative, f"public artifact {label}")
        projected = _sanitize_public_text(data, relative)
        if projected != data:
            return self.derived_artifact(
                projected,
                name=relative.name,
                kind=kind,
                label=label,
                media_type=_media_type(relative),
            )
        if _contains_local_location(data):
            raise ViewerExportError(
                f"public artifact {label} contains a local filesystem location"
            )
        digest = _sha256_bytes(data)
        destination_relative = VIEWER_ARTIFACT_ROOT / digest / relative.name
        destination = _contained_path(
            self.root,
            destination_relative,
            label=f"public destination {label}",
            must_exist=False,
        )
        previous = self.copy_plan.get(destination)
        if previous is not None and previous != data:
            raise ViewerExportError("two public artifacts collide at one destination")
        self.copy_plan[destination] = data
        static_url = f"./artifacts/{digest}/{relative.name}"
        self.inventory[relative.as_posix()] = {
            "sourcePath": relative.as_posix(),
            "sourceSha256": digest,
            "publicPath": destination_relative.as_posix(),
            "staticUrl": static_url,
            "mediaType": _media_type(relative),
        }
        return {
            "kind": kind,
            "label": label,
            "path": relative.as_posix(),
            "sha256": digest,
            "staticUrl": static_url,
            "mediaType": _media_type(relative),
        }

    def public_receipt_artifact(
        self, relative: Path, kind: str, label: str
    ) -> dict[str, str]:
        """Queue a safe receipt projection instead of copying a raw G3 receipt.

        The raw receipt remains a gate input in ``results/v2``.  A projection is
        used here because the viewer needs a small, inspectable execution trace
        but must not publish grader packet links, gold bindings, or score inputs.
        """

        if not public_viewer_source_allowed(relative):
            raise ViewerExportError(
                f"receipt is not safe for public projection: {relative}"
            )
        try:
            receipt = json.loads(
                _read_source_bytes(
                    self.root, relative, f"public receipt {label}"
                ).decode("utf-8")
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ViewerExportError(f"cannot read public receipt {label}") from exc
        if not isinstance(receipt, Mapping):
            raise ViewerExportError(f"public receipt {label} must be an object")
        projection = _public_receipt_projection(receipt)
        data = _json_bytes(projection)
        _reject_forbidden_receipt_fields(projection, label=label)
        reference = self.derived_artifact(
            data,
            name=f"{relative.stem}.public-receipt.json",
            kind=kind,
            label=label,
            media_type="application/json",
        )
        return reference

    def derived_artifact(
        self,
        data: bytes,
        *,
        name: str,
        kind: str,
        label: str,
        media_type: str,
    ) -> dict[str, str]:
        """Queue an exact, content-addressed public projection such as one source section."""

        if Path(name).name != name or not name or name.startswith("."):
            raise ViewerExportError("derived viewer artifact name is unsafe")
        data = _sanitize_public_text(data, Path(name))
        if _contains_local_location(data):
            raise ViewerExportError(
                f"derived public artifact {label} contains a local filesystem location"
            )
        digest = _sha256_bytes(data)
        destination_relative = VIEWER_ARTIFACT_ROOT / digest / name
        destination = _contained_path(
            self.root,
            destination_relative,
            label=f"derived public artifact {label}",
            must_exist=False,
        )
        previous = self.copy_plan.get(destination)
        if previous is not None and previous != data:
            raise ViewerExportError("two derived artifacts collide at one destination")
        self.copy_plan[destination] = data
        static_url = f"./artifacts/{digest}/{name}"
        generated_path = destination_relative.as_posix()
        self.inventory[generated_path] = {
            "sourcePath": generated_path,
            "sourceSha256": digest,
            "publicPath": generated_path,
            "staticUrl": static_url,
            "mediaType": media_type,
        }
        return {
            "kind": kind,
            "label": label,
            "path": generated_path,
            "sha256": digest,
            "staticUrl": static_url,
            "mediaType": media_type,
        }


def _corpus_source_artifacts(
    root: Path, catalog: _ArtifactCatalog
) -> dict[str, dict[str, str]]:
    relative_root = Path("novalearn_synthetic_corpus/corpus")
    current = root
    for part in relative_root.parts:
        current /= part
        if current.is_symlink():
            raise ViewerExportError("public synthetic corpus traverses a symlink")
    try:
        corpus_root = (root / relative_root).resolve(strict=True)
        corpus_root.relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        raise ViewerExportError(
            "public synthetic corpus is missing or outside the repository"
        ) from exc
    if not corpus_root.is_dir():
        raise ViewerExportError("public synthetic corpus is not a directory")
    sources: dict[str, dict[str, str]] = {}
    for path in sorted(corpus_root.rglob("*.md")):
        if path.is_symlink() or not path.is_file():
            raise ViewerExportError("public synthetic corpus contains a symlink")
        match = re.match(r"(NL-\d{3})_", path.name)
        if match is None:
            continue
        source_id = match.group(1)
        if source_id in sources:
            raise ViewerExportError(f"duplicate public corpus source {source_id}")
        relative = path.relative_to(root)
        sources[source_id] = catalog.artifact(
            relative, "source", f"Synthetic public source {source_id}"
        )
    if not sources:
        raise ViewerExportError("public synthetic corpus contains no source documents")
    return sources


def _corpus_section_artifacts(
    root: Path,
    catalog: _ArtifactCatalog,
) -> dict[str, dict[str, str]]:
    """Publish every public Markdown section as an exact addressable fragment."""

    fragments: dict[str, dict[str, str]] = {}
    # ``_contained_path`` accepts files only. Resolve the corpus directory
    # explicitly and retain equivalent containment and symlink checks.
    current = root
    for part in Path("novalearn_synthetic_corpus/corpus").parts:
        current /= part
        if current.is_symlink():
            raise ViewerExportError("public synthetic corpus traverses a symlink")
    corpus_root = (root / "novalearn_synthetic_corpus/corpus").resolve(strict=True)
    corpus_root.relative_to(root)
    if not corpus_root.is_dir():
        raise ViewerExportError("public synthetic corpus is not a directory")
    header = re.compile(r"^## \[(NL-\d{3}-S\d{2})\](?:\s|$)")
    for source in sorted(corpus_root.rglob("*.md")):
        if source.is_symlink() or not source.is_file():
            raise ViewerExportError("public synthetic corpus contains a symlink")
        relative = source.relative_to(root)
        try:
            lines = (
                _read_source_bytes(
                    root, relative, f"public corpus source {source.name}"
                )
                .decode("utf-8")
                .splitlines(keepends=True)
            )
        except UnicodeDecodeError as exc:
            raise ViewerExportError("public corpus source is not UTF-8") from exc
        starts: list[tuple[int, str]] = []
        for index, line in enumerate(lines):
            match = header.match(line)
            if match is not None:
                starts.append((index, match.group(1)))
        for position, (start, section_id) in enumerate(starts):
            if section_id in fragments:
                raise ViewerExportError(f"duplicate public corpus section {section_id}")
            end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
            data = "".join(lines[start:end]).encode("utf-8")
            fragments[section_id] = catalog.derived_artifact(
                data,
                name=f"{section_id}.md",
                kind="source",
                label=f"Exact saved source section {section_id}",
                media_type="text/markdown",
            )
    if not fragments:
        raise ViewerExportError("public synthetic corpus has no addressable sections")
    return fragments


def _event_section_artifacts(
    root: Path,
    events: Sequence[Mapping[str, Any]],
    catalog: _ArtifactCatalog,
) -> dict[str, dict[str, str]]:
    """Publish each temporal JSONL record as the exact saved source line."""

    lines = _read_source_bytes(
        root, TEMPORAL_EVENTS_PATH, "temporal event stream"
    ).splitlines(keepends=True)
    fragments: dict[str, dict[str, str]] = {}
    for event in events:
        event_id = event.get("event_id")
        line_number = event.get("_viewer_line")
        if (
            not isinstance(event_id, str)
            or isinstance(line_number, bool)
            or not isinstance(line_number, int)
            or line_number < 1
            or line_number > len(lines)
        ):
            raise ViewerExportError("public temporal event has no exact source line")
        data = lines[line_number - 1]
        try:
            row = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ViewerExportError("temporal source fragment is invalid JSON") from exc
        if not isinstance(row, Mapping) or row.get("event_id") != event_id:
            raise ViewerExportError("temporal source fragment identity changed")
        fragments[event_id] = catalog.derived_artifact(
            data,
            name=f"{event_id}.json",
            kind="source",
            label=f"Exact saved temporal source record {event_id}",
            media_type="application/json",
        )
    return fragments


def _metric(
    value: float | int,
    unit: str,
    display: str,
    artifact: Mapping[str, Any],
    run_ids: Sequence[str],
    pointer: str,
) -> dict[str, Any]:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ViewerExportError("viewer metric must be a finite measured number")
    if not run_ids or any(not isinstance(item, str) or not item for item in run_ids):
        raise ViewerExportError("viewer metric must name its exact source runs")
    if not _valid_json_pointer_syntax(pointer):
        raise ViewerExportError("viewer metric must carry an exact JSON pointer")
    return {
        "value": value,
        "unit": unit,
        "display": display,
        "provenance": {
            "artifact": dict(artifact),
            "runIds": list(run_ids),
            "jsonPointer": pointer,
        },
    }


def _event_index(events: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {str(row["event_id"]): row for row in events}


def _citation(
    event_id: str,
    *,
    run_id: str,
    events: Mapping[str, Mapping[str, Any]],
    event_artifact: Mapping[str, Any],
    event_sections: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    event = events.get(event_id)
    if event is None:
        raise ViewerExportError(f"saved run cites unknown public event {event_id}")
    target = event_sections.get(event_id)
    if target is None:
        raise ViewerExportError(f"saved run cites an unaddressable event {event_id}")
    excerpt_fields = {
        key: event.get(key)
        for key in (
            "event_id",
            "subject",
            "predicate",
            "value",
            "status",
            "effective_time",
            "authority_level",
        )
    }
    line_number = event.get("_viewer_line")
    if (
        isinstance(line_number, bool)
        or not isinstance(line_number, int)
        or line_number < 1
    ):
        raise ViewerExportError(f"public event {event_id} has no exact source line")
    return {
        "id": f"citation-{event_id}",
        "sourceId": str(event.get("source_id")),
        "sectionId": str(event.get("section_id")),
        "label": f"{event.get('source_id')}#{event_id}",
        "excerpt": json.dumps(excerpt_fields, ensure_ascii=False, sort_keys=True),
        "source": dict(event_artifact),
        "target": dict(target),
        "provenance": {
            "artifact": dict(target),
            "runIds": [run_id],
            "jsonPointer": "/event_id",
        },
    }


def _answer_citations(
    receipt: Mapping[str, Any],
    *,
    receipt_artifact: Mapping[str, Any],
    static_sources: Mapping[str, Mapping[str, Any]],
    static_sections: Mapping[str, Mapping[str, Any]],
    events: Mapping[str, Mapping[str, Any]],
    event_artifact: Mapping[str, Any],
    event_sections: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    answer = str(receipt.get("answer", ""))
    ids = sorted(set(_EVENT_ID.findall(answer)))
    run_id = str(receipt.get("run_id"))
    citations = [
        _citation(
            event_id,
            run_id=run_id,
            events=events,
            event_artifact=event_artifact,
            event_sections=event_sections,
        )
        for event_id in ids
    ]
    citations.extend(
        _static_citation(
            section_id,
            run_id=run_id,
            receipt_artifact=receipt_artifact,
            static_sources=static_sources,
            static_sections=static_sections,
            pointer="/answer",
        )
        for section_id in sorted(set(_STATIC_SECTION_ID.findall(answer)))
    )
    return citations


def _static_citation(
    section_id: str,
    *,
    run_id: str,
    receipt_artifact: Mapping[str, Any],
    static_sources: Mapping[str, Mapping[str, Any]],
    static_sections: Mapping[str, Mapping[str, Any]],
    pointer: str,
) -> dict[str, Any]:
    source_id = section_id.rsplit("-S", 1)[0]
    source = static_sources.get(source_id)
    if source is None:
        raise ViewerExportError(f"saved run cites missing public source {source_id}")
    target = static_sections.get(section_id)
    if target is None:
        raise ViewerExportError(f"saved run cites missing public section {section_id}")
    return {
        "id": f"citation-{run_id}-{section_id}",
        "sourceId": source_id,
        "sectionId": section_id,
        "label": f"{source_id}#{section_id}",
        "excerpt": "",
        "source": dict(source),
        "target": dict(target),
        "provenance": {
            "artifact": dict(receipt_artifact),
            "runIds": [run_id],
            "jsonPointer": pointer,
        },
    }


def _retriever_trace_index(
    labs: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
) -> dict[tuple[str, str], _RetrieverTrace]:
    """Index persisted R0 scores without reconstructing or normalizing them."""

    indexed: dict[tuple[str, str], _RetrieverTrace] = {}
    for lab, artifact in labs:
        traces = lab.get("traces")
        if not isinstance(traces, list):
            raise ViewerExportError("public R0 retrieval lab has no trace list")
        for trace_index, trace in enumerate(traces):
            if not isinstance(trace, Mapping) or trace.get("strategy_id") != "R0":
                continue
            task = trace.get("task")
            suite = task.get("suite") if isinstance(task, Mapping) else None
            task_id = task.get("task_id") if isinstance(task, Mapping) else None
            run_id = trace.get("run_id")
            stages = trace.get("retrieval_stages")
            if not all(
                isinstance(item, str) and item for item in (suite, task_id, run_id)
            ) or not isinstance(stages, Mapping):
                raise ViewerExportError("public R0 trace identity is incomplete")
            scores: dict[str, tuple[float, str]] = {}
            for stage_name, rows in stages.items():
                if not isinstance(stage_name, str) or not isinstance(rows, list):
                    raise ViewerExportError("public R0 retrieval stage is invalid")
                for candidate_index, candidate in enumerate(rows):
                    if not isinstance(candidate, Mapping):
                        continue
                    candidate_id = candidate.get("candidate_id")
                    score = candidate.get("normalized_score")
                    if (
                        not isinstance(candidate_id, str)
                        or isinstance(score, bool)
                        or not isinstance(score, (int, float))
                        or not math.isfinite(float(score))
                    ):
                        continue
                    scores[candidate_id] = (
                        float(score),
                        f"/traces/{trace_index}/retrieval_stages/{stage_name}/{candidate_index}/normalized_score",
                    )
            key = (str(suite), str(task_id))
            if key in indexed:
                raise ViewerExportError(
                    f"duplicate public R0 trace for {suite}/{task_id}"
                )
            indexed[key] = _RetrieverTrace(
                run_id=str(run_id),
                artifact=dict(artifact),
                trace_pointer=f"/traces/{trace_index}",
                scores=scores,
            )
    if not indexed:
        raise ViewerExportError("public R0 retrieval trace index is empty")
    return indexed


def _pipeline_candidate(
    row: Mapping[str, Any],
    *,
    pointer: str,
    run_id: str,
    receipt_artifact: Mapping[str, Any],
    static_sources: Mapping[str, Mapping[str, Any]],
    static_sections: Mapping[str, Mapping[str, Any]],
    events: Mapping[str, Mapping[str, Any]],
    event_artifact: Mapping[str, Any],
    event_sections: Mapping[str, Mapping[str, Any]],
    kept: bool,
    context_index: int | None,
    retriever_trace: _RetrieverTrace,
    origin: str,
) -> dict[str, Any] | None:
    raw_ids = row.get("raw_evidence_ids")
    if not isinstance(raw_ids, list):
        return None
    event_id = next(
        (str(item) for item in raw_ids if isinstance(item, str) and item in events),
        None,
    )
    static_section_id = next(
        (
            str(item)
            for item in raw_ids
            if isinstance(item, str) and _STATIC_SECTION_ID.fullmatch(item)
        ),
        None,
    )
    rank = row.get("rank")
    tokens = row.get("token_count")
    if (
        (event_id is None and static_section_id is None)
        or isinstance(rank, bool)
        or not isinstance(rank, (int, float))
        or isinstance(tokens, bool)
        or not isinstance(tokens, (int, float))
    ):
        return None
    context_order = (
        _metric(
            context_index,
            "count",
            str(context_index),
            receipt_artifact,
            [run_id],
            pointer,
        )
        if context_index is not None
        else None
    )
    citation = (
        _citation(
            event_id,
            run_id=run_id,
            events=events,
            event_artifact=event_artifact,
            event_sections=event_sections,
        )
        if event_id is not None
        else _static_citation(
            str(static_section_id),
            run_id=run_id,
            receipt_artifact=receipt_artifact,
            static_sources=static_sources,
            static_sections=static_sections,
            pointer=pointer,
        )
    )
    persisted_similarity = row.get("task_similarity")
    saved_score = retriever_trace.scores.get(str(row.get("evidence_id")))
    score = (
        _metric(
            persisted_similarity,
            "score",
            f"{persisted_similarity:.4f}",
            receipt_artifact,
            [run_id],
            f"{pointer}/task_similarity",
        )
        if isinstance(persisted_similarity, (int, float))
        and not isinstance(persisted_similarity, bool)
        and math.isfinite(float(persisted_similarity))
        else _metric(
            saved_score[0],
            "score",
            f"{saved_score[0]:.4f}",
            retriever_trace.artifact,
            [retriever_trace.run_id],
            saved_score[1],
        )
        if saved_score is not None
        else None
    )
    return {
        "id": f"{run_id}:{origin}:{pointer}",
        "citation": citation,
        "origin": origin,
        "rank": _metric(
            rank,
            "count",
            str(rank),
            receipt_artifact,
            [run_id],
            f"{pointer}/rank",
        ),
        "score": score,
        "decision": "kept" if kept else "removed",
        "reason": None if kept else "not selected by the saved trace",
        "tokenCount": _metric(
            tokens,
            "tokens",
            f"{tokens} tokens",
            receipt_artifact,
            [run_id],
            f"{pointer}/token_count",
        ),
        "contextOrder": context_order,
    }


def _pipeline(
    receipt: Mapping[str, Any],
    *,
    receipt_artifact: Mapping[str, Any],
    static_sources: Mapping[str, Mapping[str, Any]],
    static_sections: Mapping[str, Mapping[str, Any]],
    events: Mapping[str, Mapping[str, Any]],
    event_artifact: Mapping[str, Any],
    event_sections: Mapping[str, Mapping[str, Any]],
    retriever_traces: Mapping[tuple[str, str], _RetrieverTrace],
) -> dict[str, Any]:
    run_id = str(receipt.get("run_id"))
    trace = receipt.get("trace")
    run_spec = receipt.get("run_spec")
    if not isinstance(trace, Mapping) or not isinstance(run_spec, Mapping):
        raise ViewerExportError(f"{run_id}: saved trace or run spec is missing")
    task = run_spec.get("task")
    suite = task.get("suite") if isinstance(task, Mapping) else None
    task_id = task.get("task_id") if isinstance(task, Mapping) else None
    retriever_trace = retriever_traces.get((str(suite), str(task_id)))
    if retriever_trace is None:
        raise ViewerExportError(f"{run_id}: persisted R0 retrieval trace is missing")
    budget = run_spec.get("context_budget_tokens")
    used = trace.get("context_token_count")
    if any(
        isinstance(item, bool) or not isinstance(item, (int, float))
        for item in (budget, used)
    ):
        raise ViewerExportError(f"{run_id}: context budget evidence is invalid")
    assert isinstance(budget, (int, float)) and not isinstance(budget, bool)
    assert isinstance(used, (int, float)) and not isinstance(used, bool)

    selected_rows: list[tuple[str, int, Mapping[str, Any]]] = []
    for field in (
        "selected_memory_evidence",
        "selected_corpus_evidence",
    ):
        rows = trace.get(field, [])
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if isinstance(row, Mapping):
                selected_rows.append((field, index, row))
    selected_ids = {str(row.get("evidence_id")) for _, _, row in selected_rows}

    retrieval_candidates: list[dict[str, Any]] = []
    for origin, field in (
        ("corpus", "corpus_candidate_evidence"),
        ("memory", "memory_candidate_evidence"),
    ):
        candidates = trace.get(field, [])
        if not isinstance(candidates, list):
            raise ViewerExportError(f"{run_id}: {field} is not a saved candidate list")
        for index, row in enumerate(candidates):
            if not isinstance(row, Mapping):
                raise ViewerExportError(f"{run_id}: {field}/{index} is invalid")
            candidate = _pipeline_candidate(
                row,
                pointer=f"/trace/{field}/{index}",
                run_id=run_id,
                receipt_artifact=receipt_artifact,
                static_sources=static_sources,
                static_sections=static_sections,
                events=events,
                event_artifact=event_artifact,
                event_sections=event_sections,
                kept=str(row.get("evidence_id")) in selected_ids,
                context_index=None,
                retriever_trace=retriever_trace,
                origin=origin,
            )
            if candidate is None:
                raise ViewerExportError(
                    f"{run_id}: {field}/{index} is not publicly addressable"
                )
            retrieval_candidates.append(candidate)

    context_candidates: list[dict[str, Any]] = []
    for context_index, (field, index, row) in enumerate(selected_rows, start=1):
        candidate = _pipeline_candidate(
            row,
            pointer=f"/trace/{field}/{index}",
            run_id=run_id,
            receipt_artifact=receipt_artifact,
            static_sources=static_sources,
            static_sections=static_sections,
            events=events,
            event_artifact=event_artifact,
            event_sections=event_sections,
            kept=True,
            context_index=context_index,
            retriever_trace=retriever_trace,
            origin={
                "selected_corpus_evidence": "corpus",
                "selected_memory_evidence": "memory",
            }[field],
        )
        if candidate is None:
            raise ViewerExportError(
                f"{run_id}: selected context candidate {field}/{index} is invalid"
            )
        context_candidates.append(candidate)

    stages: list[dict[str, Any]] = []
    for kind in _STAGE_KINDS:
        if kind == "retrieval":
            label = "Retrieval (instrumented in saved trace)"
            rows = retrieval_candidates
            artifact = retriever_trace.artifact
        elif kind in {"budget", "context"}:
            label = f"{kind.title()} (instrumented in saved trace)"
            rows = context_candidates
            artifact = receipt_artifact
        else:
            label = f"{kind.title()} (uninstrumented; bound to trace artifact)"
            rows = []
            artifact = receipt_artifact
        stages.append(
            {
                "id": f"{run_id}-{kind}",
                "kind": kind,
                "label": label,
                "artifact": dict(artifact),
                "candidates": rows,
            }
        )
    return {
        "contextBudget": _metric(
            budget,
            "tokens",
            f"{budget} tokens",
            receipt_artifact,
            [run_id],
            "/run_spec/context_budget_tokens",
        ),
        "contextUsed": _metric(
            used,
            "tokens",
            f"{used} tokens",
            receipt_artifact,
            [run_id],
            "/trace/context_token_count",
        ),
        "stages": stages,
    }


def _cell_by_run_id(public_run: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    cells = public_run.get("cells")
    if not isinstance(cells, list):
        raise ViewerExportError("public G3 run cells are missing")
    result: dict[str, Mapping[str, Any]] = {}
    for cell in cells:
        if isinstance(cell, Mapping) and isinstance(cell.get("run_id"), str):
            result[str(cell["run_id"])] = cell
    return result


def _source_run_records(
    root: Path,
    evidence: _Evidence,
    selection: _Selection,
    *,
    catalog: _ArtifactCatalog,
    g2_lab_artifact: Mapping[str, Any],
    temporal_lab_artifact: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Build the public run registry used by aggregate and cross-gate provenance."""

    records: dict[str, dict[str, Any]] = {}
    cells = _cell_by_run_id(evidence.public_run)
    for receipt in evidence.receipts:
        run_id = str(receipt.get("run_id"))
        task = _receipt_task(receipt)
        cell = cells.get(run_id)
        if not isinstance(cell, Mapping):
            raise ViewerExportError(f"{run_id}: public source cell is missing")
        receipt_path = _safe_relative_path(
            cell.get("receipt_path"), f"{run_id} source receipt"
        )
        records[run_id] = {
            "id": run_id,
            "suite": str(task.get("suite")),
            "taskId": str(task.get("task_id")),
            "taskFamily": str(task.get("task_family")),
            "strategyId": str(receipt.get("policy")),
            "reasoningEffort": str(receipt.get("reasoning_effort")),
            "artifact": catalog.public_receipt_artifact(
                receipt_path, "run", f"{run_id} public source receipt"
            ),
            "jsonPointer": "/",
        }

    task_families = {
        str(task.get("task_id")): str(task.get("task_family"))
        for task in task_catalog(root)
        if isinstance(task.get("task_id"), str)
        and isinstance(task.get("task_family"), str)
    }
    task_families.update(
        {
            str(_receipt_task(receipt).get("task_id")): str(
                _receipt_task(receipt).get("task_family")
            )
            for receipt in evidence.receipts
        }
    )
    win_run_ids = {
        str(selection.retrieval_win["parent_run_id"]),
        str(selection.retrieval_win["candidate_run_id"]),
    }
    for lab, artifact in (
        (evidence.g2_lab, g2_lab_artifact),
        (evidence.temporal_r0_lab, temporal_lab_artifact),
    ):
        traces = lab.get("traces")
        if not isinstance(traces, list):
            raise ViewerExportError("public source-run lab has no traces")
        for index, trace in enumerate(traces):
            if not isinstance(trace, Mapping):
                continue
            run_id = trace.get("run_id")
            strategy = trace.get("strategy_id")
            if (
                not isinstance(run_id, str)
                or not isinstance(strategy, str)
                or (strategy != "R0" and run_id not in win_run_ids)
            ):
                continue
            task = trace.get("task")
            task_id = task.get("task_id") if isinstance(task, Mapping) else None
            suite = task.get("suite") if isinstance(task, Mapping) else None
            family = task_families.get(str(task_id))
            if not isinstance(task_id, str) or not isinstance(suite, str) or not family:
                raise ViewerExportError(
                    f"{run_id}: retrieval source-run task is incomplete"
                )
            if run_id in records:
                raise ViewerExportError(f"duplicate public source run {run_id}")
            records[run_id] = {
                "id": run_id,
                "suite": suite,
                "taskId": task_id,
                "taskFamily": family,
                "strategyId": strategy,
                "reasoningEffort": None,
                "artifact": dict(artifact),
                "jsonPointer": f"/traces/{index}",
            }
    if not win_run_ids.issubset(records):
        raise ViewerExportError(
            "G2 retrieval-win runs are absent from the source registry"
        )
    return [records[run_id] for run_id in sorted(records)]


def _question_id(key: tuple[str, str]) -> str:
    return f"g3-{key[0]}-{key[1]}"


def _run_record(
    receipt: Mapping[str, Any],
    *,
    cell: Mapping[str, Any],
    question_id: str,
    generated_at: str,
    catalog: _ArtifactCatalog,
    static_sources: Mapping[str, Mapping[str, Any]],
    static_sections: Mapping[str, Mapping[str, Any]],
    freeze_artifact: Mapping[str, Any],
    event_artifact: Mapping[str, Any],
    event_sections: Mapping[str, Mapping[str, Any]],
    events: Mapping[str, Mapping[str, Any]],
    retriever_traces: Mapping[tuple[str, str], _RetrieverTrace],
) -> dict[str, Any]:
    run_id = str(receipt.get("run_id"))
    policy = str(receipt.get("policy"))
    receipt_path = _safe_relative_path(cell.get("receipt_path"), f"{run_id} receipt")
    prepared_path = _safe_relative_path(cell.get("prepared_path"), f"{run_id} prepared")
    generation_path = _safe_relative_path(
        cell.get("generation_path"), f"{run_id} generation"
    )
    receipt_ref = catalog.public_receipt_artifact(
        receipt_path, "run", f"{run_id} public result receipt"
    )
    trace_ref = catalog.public_receipt_artifact(
        receipt_path, "trace", f"{run_id} public saved trace"
    )
    public_facts_ref = catalog.public_receipt_artifact(
        receipt_path,
        "run-facts",
        f"{run_id} public execution facts",
    )
    prepared_source = _contained_path(
        catalog.root, prepared_path, label=f"{run_id} prepared cell"
    )
    public_receipt = _public_receipt_projection(receipt)
    public_run_spec = public_receipt["run_spec"]
    public_trace = public_receipt["trace"]
    prepared_ref = catalog.derived_artifact(
        _json_bytes(
            {
                "viewer_projection_schema": "contextlab.public-run-configuration.v1",
                "run_id": run_id,
                "policy": public_receipt["policy"],
                "reasoning_effort": public_receipt["reasoning_effort"],
                "provider": public_receipt["provider"],
                "requested_model": public_receipt["requested_model"],
                "resolved_model": public_receipt["resolved_model"],
                "run_spec": public_run_spec,
            }
        ),
        name=f"{prepared_source.stem}.{policy}.public-configuration.json",
        kind="configuration",
        label=f"{run_id} public configuration",
        media_type="application/json",
    )
    memory_ref = catalog.derived_artifact(
        _json_bytes(
            {
                "viewer_projection_schema": "contextlab.public-memory-selection.v2",
                "run_id": run_id,
                "policy": public_receipt["policy"],
                "selected_memory_evidence": public_trace["selected_memory_evidence"],
            }
        ),
        name=f"{prepared_source.stem}.{policy}.public-memory.json",
        kind="memory",
        label=f"{run_id} public memory-evidence selection",
        media_type="application/json",
    )
    prompt_ref = catalog.derived_artifact(
        _json_bytes(
            {
                "viewer_projection_schema": "contextlab.public-prompt-envelope.v1",
                "run_id": run_id,
                "prompt_version": public_run_spec["prompt_version"],
                "context_budget_tokens": public_run_spec["context_budget_tokens"],
                "task": public_run_spec["task"],
            }
        ),
        name=f"{prepared_source.stem}.{policy}.public-prompt.json",
        kind="prompt",
        label=f"{run_id} public prompt envelope",
        media_type="application/json",
    )
    generation_ref = catalog.artifact(
        generation_path, "tool-result", f"{run_id} saved model result"
    )

    trace = receipt.get("trace")
    if not isinstance(trace, Mapping):
        raise ViewerExportError(f"{run_id}: trace is missing")
    context_tokens = trace.get("context_token_count")
    latency = receipt.get("latency_ms")
    cost = receipt.get("actual_usd")
    if (
        isinstance(context_tokens, bool)
        or not isinstance(context_tokens, (int, float))
        or isinstance(latency, bool)
        or not isinstance(latency, (int, float))
        or isinstance(cost, bool)
        or not isinstance(cost, (int, float))
    ):
        raise ViewerExportError(f"{run_id}: measured run metrics are invalid")
    status = receipt.get("status")
    run_spec = receipt.get("run_spec")
    if not isinstance(run_spec, Mapping):
        raise ViewerExportError(f"{run_id}: frozen run spec is missing")
    task = run_spec.get("task")
    if not isinstance(task, Mapping) or task.get("suite") not in {
        "temporal",
        "static",
    }:
        raise ViewerExportError(f"{run_id}: public task suite is invalid")
    corpus_artifact = (
        event_artifact if task.get("suite") == "temporal" else freeze_artifact
    )
    values = {
        "policy": policy,
        "reasoningEffort": str(receipt.get("reasoning_effort")),
        "provider": str(receipt.get("provider")),
        "requestedModel": str(receipt.get("requested_model")),
        "promptVersion": str(run_spec.get("prompt_version")),
        "savedStatus": str(status),
    }
    return {
        "id": run_id,
        "questionId": question_id,
        "strategyId": policy,
        "reasoningEffort": str(receipt.get("reasoning_effort")),
        "runArtifact": receipt_ref,
        "rawOutput": generation_ref,
        "configuration": {
            "id": f"{policy}-{receipt.get('reasoning_effort')}",
            "artifact": prepared_ref,
            "values": values,
        },
        "corpusSnapshot": dict(corpus_artifact),
        "memorySnapshot": memory_ref,
        "prompt": prompt_ref,
        "executionFacts": public_facts_ref,
        "answer": {
            "text": str(receipt.get("answer", "")),
            "citations": _answer_citations(
                receipt,
                receipt_artifact=receipt_ref,
                static_sources=static_sources,
                static_sections=static_sections,
                events=events,
                event_artifact=event_artifact,
                event_sections=event_sections,
            ),
        },
        "metrics": {
            "contextTokens": _metric(
                context_tokens,
                "tokens",
                f"{context_tokens} tokens",
                receipt_ref,
                [run_id],
                "/trace/context_token_count",
            ),
            "latency": _metric(
                latency,
                "milliseconds",
                f"{latency} ms",
                receipt_ref,
                [run_id],
                "/latency_ms",
            ),
            "estimatedCost": _metric(
                cost,
                "usd",
                f"${float(cost):.6f}",
                receipt_ref,
                [run_id],
                "/actual_usd",
            ),
        },
        "executionStatus": str(status),
        "pipeline": _pipeline(
            receipt,
            receipt_artifact=trace_ref,
            static_sources=static_sources,
            static_sections=static_sections,
            events=events,
            event_artifact=event_artifact,
            event_sections=event_sections,
            retriever_traces=retriever_traces,
        ),
        "traceSpans": [
            {
                "id": f"{run_id}-saved-generation",
                "parentId": None,
                "name": "Gate-bound saved generation observation",
                "startedAt": generated_at,
                "status": "error" if status == "failed" else "ok",
                "duration": _metric(
                    latency,
                    "milliseconds",
                    f"{latency} ms",
                    receipt_ref,
                    [run_id],
                    "/latency_ms",
                ),
                "artifact": trace_ref,
                "toolResult": generation_ref,
            }
        ],
    }


def _selected_receipts(
    evidence: _Evidence, selection: _Selection
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Mapping[str, Any]],
]:
    groups = _receipt_groups(evidence.receipts)
    questions: list[dict[str, Any]] = []
    ordered: list[dict[str, Any]] = []
    by_id: dict[str, Mapping[str, Any]] = {}
    run_specs = evidence.g3_freeze.get("manifest", {}).get("run_specs", [])
    if not isinstance(run_specs, list):
        raise ViewerExportError("G3 freeze run specs are missing")
    spec_index = {
        str(spec.get("run_id")): index
        for index, spec in enumerate(run_specs)
        if isinstance(spec, Mapping) and isinstance(spec.get("run_id"), str)
    }
    for key in selection.groups:
        rows = groups[key]
        comparison = [
            str(rows[policy].get("run_id")) for policy in MEMORY_CONFIGURATIONS
        ]
        first = rows[MEMORY_CONFIGURATIONS[0]]
        task = _receipt_task(first)
        if any(run_id not in spec_index for run_id in comparison):
            raise ViewerExportError("selected G3 comparison is outside the freeze")
        questions.append(
            {
                "id": _question_id(key),
                "text": str(task.get("question_text")),
                "taskFamily": str(task.get("task_family")),
                "_artifact_pointer": f"/manifest/run_specs/{spec_index[comparison[0]]}/task",
                "comparisonRunIds": comparison,
            }
        )
        for policy in MEMORY_CONFIGURATIONS:
            receipt = dict(rows[policy])
            run_id = str(receipt.get("run_id"))
            ordered.append(receipt)
            by_id[run_id] = receipt
    return questions, ordered, by_id


def _trace_evidence_row_count(receipt: Mapping[str, Any], fields: Sequence[str]) -> int:
    trace = receipt.get("trace")
    if not isinstance(trace, Mapping):
        raise ViewerExportError("matrix receipt has no saved trace")
    count = 0
    for field in fields:
        rows = trace.get(field, [])
        if not isinstance(rows, list):
            raise ViewerExportError(f"matrix trace field {field} is invalid")
        if any(not isinstance(row, Mapping) for row in rows):
            raise ViewerExportError(f"matrix trace field {field} has a malformed row")
        count += len(rows)
    return count


def _matrix_evidence(
    evidence: _Evidence,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for receipt in evidence.receipts:
        task = _receipt_task(receipt)
        family = task.get("task_family")
        effort = receipt.get("reasoning_effort")
        policy = receipt.get("policy")
        if not all(isinstance(item, str) and item for item in (family, effort, policy)):
            raise ViewerExportError("matrix receipt identity is incomplete")
        grouped[(str(family), str(effort), str(policy))].append(receipt)

    result: list[dict[str, Any]] = []
    effort_rank = {"low": 0, "high": 1}
    policy_rank = {policy: index for index, policy in enumerate(MEMORY_CONFIGURATIONS)}
    for (family, effort, policy), rows in sorted(
        grouped.items(),
        key=lambda item: (
            item[0][0],
            effort_rank.get(item[0][1], 99),
            policy_rank.get(item[0][2], 99),
        ),
    ):
        run_ids = [str(row.get("run_id")) for row in rows]
        if policy not in policy_rank or effort not in effort_rank or not rows:
            raise ViewerExportError("matrix contains an unsupported policy or effort")
        budgets: list[float] = []
        context_tokens: list[float] = []
        latencies: list[float] = []
        costs: list[float] = []
        candidate_counts: list[int] = []
        selected_counts: list[int] = []
        completed = 0
        source_pointers: list[dict[str, Any]] = []
        for row, run_id in zip(rows, run_ids, strict=True):
            spec = row.get("run_spec")
            budget = (
                spec.get("context_budget_tokens") if isinstance(spec, Mapping) else None
            )
            trace = row.get("trace")
            context = (
                trace.get("context_token_count") if isinstance(trace, Mapping) else None
            )
            latency = row.get("latency_ms")
            cost = row.get("actual_usd")
            status = row.get("status")
            if (
                isinstance(budget, bool)
                or not isinstance(budget, (int, float))
                or isinstance(context, bool)
                or not isinstance(context, (int, float))
                or isinstance(latency, bool)
                or not isinstance(latency, (int, float))
                or isinstance(cost, bool)
                or not isinstance(cost, (int, float))
                or not isinstance(status, str)
            ):
                raise ViewerExportError(f"matrix inputs are invalid for {run_id}")
            budgets.append(float(budget))
            context_tokens.append(float(context))
            latencies.append(float(latency))
            costs.append(float(cost))
            candidate_counts.append(
                _trace_evidence_row_count(
                    row,
                    (
                        "corpus_candidate_evidence",
                        "memory_candidate_evidence",
                    ),
                )
            )
            selected_counts.append(
                _trace_evidence_row_count(
                    row,
                    (
                        "selected_corpus_evidence",
                        "selected_memory_evidence",
                    ),
                )
            )
            completed += status == "completed"
            source_pointers.append(
                {
                    "run_id": run_id,
                    "candidate_evidence": [
                        "/trace/corpus_candidate_evidence",
                        "/trace/memory_candidate_evidence",
                    ],
                    "selected_evidence": [
                        "/trace/selected_corpus_evidence",
                        "/trace/selected_memory_evidence",
                    ],
                    "context_budget": "/run_spec/context_budget_tokens",
                    "context_tokens": "/trace/context_token_count",
                    "latency": "/latency_ms",
                    "cost": "/actual_usd",
                    "execution_status": "/status",
                }
            )
        result.append(
            {
                "task_family": family,
                "reasoning_effort": effort,
                "policy": policy,
                "run_ids": run_ids,
                "completion_ratio": completed / len(rows),
                "mean_candidate_evidence_count": sum(candidate_counts) / len(rows),
                "mean_selected_evidence_count": sum(selected_counts) / len(rows),
                "mean_context_budget_tokens": sum(budgets) / len(budgets),
                "mean_context_tokens": sum(context_tokens) / len(context_tokens),
                "mean_latency_ms": sum(latencies) / len(latencies),
                "mean_execution_cost_usd": sum(costs) / len(costs),
                "trial_count": len(rows),
                "source_receipt_pointers": source_pointers,
            }
        )
    expected_keys = {
        (
            str(_receipt_task(row).get("task_family")),
            str(row.get("reasoning_effort")),
            str(row.get("policy")),
        )
        for row in evidence.receipts
    }
    if {
        (row["task_family"], row["reasoning_effort"], row["policy"]) for row in result
    } != expected_keys:
        raise ViewerExportError("matrix does not cover the complete public factorial")
    return result


def _reviewers(
    protocol: Mapping[str, Any], protocol_artifact: Mapping[str, Any]
) -> dict[str, Any]:
    reviewers = protocol.get("reviewers")
    if not isinstance(reviewers, list):
        raise ViewerExportError("review protocol does not declare the exact panel")
    by_id = {
        str(row.get("id")): row
        for row in reviewers
        if isinstance(row, Mapping) and isinstance(row.get("id"), str)
    }
    expected = {
        "gpt-5.6-sol-high": ("gpt-5.6-sol", "high"),
        "claude-opus-5-medium": ("claude-opus-5", "medium"),
        "kevin": (None, None),
    }
    if set(by_id) != set(expected):
        raise ViewerExportError("review panel must contain exactly two AIs and Kevin")
    for reviewer_id, (model, effort) in expected.items():
        row = by_id[reviewer_id]
        if row.get("model") != model or row.get("reasoning") != effort:
            raise ViewerExportError("reviewer model or reasoning effort changed")

    def record(reviewer_id: str, name: str) -> dict[str, Any]:
        row = by_id[reviewer_id]
        invocation = row.get("invocation")
        if not isinstance(invocation, str) or not invocation:
            raise ViewerExportError("reviewer invocation is missing")
        return {
            "id": reviewer_id,
            "name": name,
            "modelId": row.get("model"),
            "reasoningEffort": row.get("reasoning"),
            "invocation": invocation,
            "artifact": dict(protocol_artifact),
        }

    return {
        "aiJudges": [
            record("gpt-5.6-sol-high", "GPT-5.6 Sol"),
            record("claude-opus-5-medium", "Claude Opus 5"),
        ],
        "human": {
            **record("kevin", "Kevin Araujo"),
            "soleHumanReviewer": True,
        },
    }


def _build_manifest(
    *,
    export_id: str,
    generated_at: str,
    evidence: _Evidence,
    selection: _Selection,
    matrix: Sequence[Mapping[str, Any]],
    catalog: _ArtifactCatalog,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": VIEWER_MANIFEST_SCHEMA,
        "export_id": export_id,
        "generated_at": generated_at,
        "viewer_schema_version": VIEWER_SCHEMA_VERSION,
        "viewer_export_path": VIEWER_EXPORT_PATH.as_posix(),
        "approval_bindings": {
            "g2_gate_artifact_sha256": evidence.g2_gate["artifact_sha256"],
            "g3_gate_artifact_sha256": evidence.g3_gate["artifact_sha256"],
            "g3_technical_record_sha256": evidence.g3_gate["technical_record_sha256"],
            "g3_final_decision": evidence.g3_gate["final_decision"],
            "kevin_decision_sha256": evidence.g3_gate["human_decision"][
                "decision_record"
            ]["artifact_sha256"],
        },
        "selected_evidence": {
            "comparison_groups": [
                {"task_id": task_id, "reasoning_effort": effort}
                for task_id, effort in selection.groups
            ],
            "temporal_evidence_comparison": {
                "task_id": selection.temporal_group[0],
                "reasoning_effort": selection.temporal_group[1],
                "baseline_run_id": selection.baseline_run_id,
                "memory_evidence_run_id": selection.memory_evidence_run_id,
                "timeline_event_ids": list(selection.timeline_event_ids),
            },
            "g2_retrieval_win": selection.retrieval_win,
        },
        "derived_evidence": {
            "strategy_matrix": [dict(row) for row in matrix],
            "timeline_events": [
                {key: value for key, value in event.items() if key != "_viewer_line"}
                for event in evidence.events
                if event.get("event_id") in selection.timeline_event_ids
            ],
        },
        "public_artifacts": sorted(
            catalog.inventory.values(), key=lambda row: row["sourcePath"]
        ),
        "publication_boundaries": {
            "sealed_artifacts_copied": False,
            "protected_gold_copied": False,
            "protected_scoring_packets_copied": False,
            "public_receipts_are_saved_validated_inputs": True,
        },
    }
    if selection.execution_failure_run_id is not None:
        body["selected_evidence"]["execution_failure"] = {
            "run_id": selection.execution_failure_run_id
        }
    body["artifact_sha256"] = sha256_json(body)
    return body


def _manifest_artifact(manifest_bytes: bytes) -> dict[str, str]:
    digest = _sha256_bytes(manifest_bytes)
    name = VIEWER_MANIFEST_PATH.name
    return {
        "kind": "export-manifest",
        "label": "G4 viewer export manifest",
        "path": VIEWER_MANIFEST_PATH.as_posix(),
        "sha256": digest,
        "staticUrl": f"./artifacts/{digest}/{name}",
        "mediaType": "application/json",
    }


def _timeline_case(
    *,
    selection: _Selection,
    evidence: _Evidence,
    manifest_artifact: Mapping[str, Any],
    event_artifact: Mapping[str, Any],
) -> dict[str, Any]:
    indexed = _event_index(evidence.events)
    events: list[dict[str, Any]] = []
    for index, event_id in enumerate(selection.timeline_event_ids):
        source = indexed[event_id]
        supersedes = None
        if index > 0:
            prior = selection.timeline_event_ids[index - 1]
            if (
                source.get("supersedes_event_id") == prior
                or source.get("tombstone_for_event_id") == prior
                or source.get("value") != indexed[prior].get("value")
            ):
                supersedes = prior
        authority = source.get("authority_level")
        if isinstance(authority, bool) or not isinstance(authority, (int, float)):
            raise ViewerExportError("timeline event authority is invalid")
        state = (
            "active" if index == len(selection.timeline_event_ids) - 1 else "superseded"
        )
        events.append(
            {
                "id": event_id,
                "label": f"{source.get('status')} · {event_id}",
                "claim": (
                    f"{source.get('subject')} {source.get('predicate')}: "
                    f"{json.dumps(source.get('value'), ensure_ascii=False)}"
                ),
                "state": state,
                "effectiveAt": str(source.get("effective_time")),
                "authority": _metric(
                    authority,
                    "score",
                    str(authority),
                    manifest_artifact,
                    [selection.baseline_run_id, selection.memory_evidence_run_id],
                    f"/derived_evidence/timeline_events/{index}/authority_level",
                ),
                "source": dict(event_artifact),
                "supersedesEventId": supersedes,
            }
        )
    return {
        "id": f"temporal-evidence-{selection.temporal_group[0]}",
        "title": "Public event transition with saved comparison runs",
        "questionId": _question_id(selection.temporal_group),
        "baselineRunId": selection.baseline_run_id,
        "memoryEvidenceRunId": selection.memory_evidence_run_id,
        "artifact": dict(event_artifact),
        "events": events,
    }


def _strategy_matrix(
    matrix: Sequence[Mapping[str, Any]], manifest_artifact: Mapping[str, Any]
) -> dict[str, Any]:
    cells: list[dict[str, Any]] = []
    for index, row in enumerate(matrix):
        policy = str(row["policy"])
        run_ids = list(row["run_ids"])
        base = f"/derived_evidence/strategy_matrix/{index}"
        cells.append(
            {
                "taskFamily": row["task_family"],
                "reasoningEffort": row["reasoning_effort"],
                "strategyId": policy,
                "artifact": dict(manifest_artifact),
                "completionRatio": _metric(
                    row["completion_ratio"],
                    "ratio",
                    f"{row['completion_ratio'] * 100:.1f}% completed",
                    manifest_artifact,
                    run_ids,
                    f"{base}/completion_ratio",
                ),
                "meanCandidateEvidence": _metric(
                    row["mean_candidate_evidence_count"],
                    "count",
                    f"{row['mean_candidate_evidence_count']:.1f} candidates",
                    manifest_artifact,
                    run_ids,
                    f"{base}/mean_candidate_evidence_count",
                ),
                "meanSelectedEvidence": _metric(
                    row["mean_selected_evidence_count"],
                    "count",
                    f"{row['mean_selected_evidence_count']:.1f} selected rows",
                    manifest_artifact,
                    run_ids,
                    f"{base}/mean_selected_evidence_count",
                ),
                "contextBudget": _metric(
                    row["mean_context_budget_tokens"],
                    "tokens",
                    f"{row['mean_context_budget_tokens']:.0f} tokens",
                    manifest_artifact,
                    run_ids,
                    f"{base}/mean_context_budget_tokens",
                ),
                "meanContextTokens": _metric(
                    row["mean_context_tokens"],
                    "tokens",
                    f"{row['mean_context_tokens']:.1f} tokens",
                    manifest_artifact,
                    run_ids,
                    f"{base}/mean_context_tokens",
                ),
                "meanLatency": _metric(
                    row["mean_latency_ms"],
                    "milliseconds",
                    f"{row['mean_latency_ms']:.1f} ms",
                    manifest_artifact,
                    run_ids,
                    f"{base}/mean_latency_ms",
                ),
                "meanExecutionCost": _metric(
                    row["mean_execution_cost_usd"],
                    "usd",
                    f"${row['mean_execution_cost_usd']:.6f}",
                    manifest_artifact,
                    run_ids,
                    f"{base}/mean_execution_cost_usd",
                ),
                "trialCount": _metric(
                    row["trial_count"],
                    "count",
                    str(row["trial_count"]),
                    manifest_artifact,
                    run_ids,
                    f"{base}/trial_count",
                ),
            }
        )
    return {"artifact": dict(manifest_artifact), "cells": cells}


def _validate_artifact_ref(value: object, label: str) -> None:
    if not isinstance(value, Mapping):
        raise ViewerExportError(f"{label} is not an artifact reference")
    required = {"kind", "label", "path", "sha256", "staticUrl", "mediaType"}
    if set(value) != required:
        raise ViewerExportError(f"{label} artifact fields differ")
    path = value.get("path")
    digest = value.get("sha256")
    url = value.get("staticUrl")
    if (
        not isinstance(path, str)
        or path.startswith("/")
        or ".." in Path(path).parts
        or not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
        or not isinstance(url, str)
        or re.fullmatch(r"\./artifacts/[0-9a-f]{64}/[^/?#]+", url) is None
        or not url.startswith(f"./artifacts/{digest}/")
    ):
        raise ViewerExportError(f"{label} artifact path, hash, or URL is invalid")
    lowered = f"/{path.lower()}"
    if any(token in lowered for token in VIEWER_FORBIDDEN_PUBLIC_TOKENS):
        raise ViewerExportError(f"{label} exposes a sealed, protected, or scoring path")


def _valid_json_pointer_syntax(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and value.startswith("/")
        and _INVALID_JSON_POINTER_ESCAPE.search(value) is None
        and not any(ord(character) < 0x20 for character in value)
    )


def _resolve_json_pointer(document: Any, pointer: str, label: str) -> Any:
    if not _valid_json_pointer_syntax(pointer):
        raise ViewerExportError(f"{label} JSON pointer is malformed")
    current = document
    for raw_token in pointer[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                raise ViewerExportError(f"{label} JSON pointer does not exist")
            current = current[token]
            continue
        if isinstance(current, list):
            if _JSON_ARRAY_INDEX.fullmatch(token) is None:
                raise ViewerExportError(f"{label} JSON pointer array index is invalid")
            index = int(token)
            if index >= len(current):
                raise ViewerExportError(f"{label} JSON pointer does not exist")
            current = current[index]
            continue
        raise ViewerExportError(f"{label} JSON pointer traverses a scalar")
    return current


def _provenance_records(
    value: object, *, path: str = "$"
) -> list[tuple[str, Mapping[str, Any], Mapping[str, Any], str]]:
    records: list[tuple[str, Mapping[str, Any], Mapping[str, Any], str]] = []
    if isinstance(value, list):
        for index, item in enumerate(value):
            records.extend(_provenance_records(item, path=f"{path}[{index}]"))
        return records
    if not isinstance(value, Mapping):
        return records
    keys = set(value)
    if keys == {
        "id",
        "sourceId",
        "sectionId",
        "label",
        "excerpt",
        "source",
        "target",
        "provenance",
    }:
        provenance = value.get("provenance")
        if isinstance(provenance, Mapping):
            records.append(("citation", value, provenance, path))
        return records
    if keys == {"value", "unit", "display", "provenance"}:
        provenance = value.get("provenance")
        if isinstance(provenance, Mapping):
            records.append(("metric", value, provenance, path))
        return records
    for key, item in value.items():
        records.extend(_provenance_records(item, path=f"{path}.{key}"))
    return records


def validate_viewer_artifact_pointers(
    value: Mapping[str, Any],
    root: Path | None = None,
    *,
    pending_artifacts: Mapping[Path, bytes] | None = None,
) -> None:
    """Resolve every metric and citation pointer against its exact public JSON bytes."""

    repository = (root or repository_root()).resolve()
    pending = {
        path.resolve(strict=False): data
        for path, data in (pending_artifacts or {}).items()
    }
    cache: dict[tuple[str, str], Any] = {}
    for _kind, _record, provenance, label in _provenance_records(value):
        artifact = provenance.get("artifact")
        _validate_artifact_ref(artifact, f"{label}.provenance.artifact")
        assert isinstance(artifact, Mapping)
        if artifact.get("mediaType") != "application/json":
            raise ViewerExportError(
                f"{label} provenance must resolve against a JSON artifact"
            )
        digest = str(artifact["sha256"])
        static_url = str(artifact["staticUrl"])
        key = (digest, static_url)
        if key not in cache:
            relative = Path("viewer/public") / static_url.removeprefix("./")
            path = _contained_path(
                repository,
                relative,
                label=f"{label} public provenance artifact",
                must_exist=False,
            )
            data = pending.get(path.resolve(strict=False))
            if data is None:
                if path.is_symlink() or not path.is_file():
                    raise ViewerExportError(
                        f"{label} public provenance artifact is missing"
                    )
                data = _read_source_bytes(
                    repository, relative, f"{label} public provenance artifact"
                )
            if _sha256_bytes(data) != digest:
                raise ViewerExportError(
                    f"{label} public provenance artifact hash differs"
                )
            try:
                cache[key] = json.loads(data)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ViewerExportError(
                    f"{label} provenance artifact is not valid JSON"
                ) from exc
        pointer = provenance.get("jsonPointer")
        if not isinstance(pointer, str):
            raise ViewerExportError(f"{label} JSON pointer is missing")
        _resolve_json_pointer(cache[key], pointer, label)


def _walk_contract(value: object, *, run_ids: set[str], path: str = "$") -> None:
    if isinstance(value, list):
        for index, item in enumerate(value):
            _walk_contract(item, run_ids=run_ids, path=f"{path}[{index}]")
        return
    if not isinstance(value, Mapping):
        return
    keys = set(value)
    if keys == {"kind", "label", "path", "sha256", "staticUrl", "mediaType"}:
        _validate_artifact_ref(value, path)
        return
    if keys == {
        "id",
        "sourceId",
        "sectionId",
        "label",
        "excerpt",
        "source",
        "target",
        "provenance",
    }:
        provenance = value.get("provenance")
        if (
            not isinstance(provenance, Mapping)
            or not isinstance(provenance.get("runIds"), list)
            or not provenance["runIds"]
            or any(item not in run_ids for item in provenance["runIds"])
            or not _valid_json_pointer_syntax(provenance.get("jsonPointer"))
        ):
            raise ViewerExportError(f"{path} citation provenance is incomplete")
        _validate_artifact_ref(value.get("source"), f"{path}.source")
        _validate_artifact_ref(value.get("target"), f"{path}.target")
        _validate_artifact_ref(
            provenance.get("artifact"), f"{path}.provenance.artifact"
        )
        return
    if keys == {"value", "unit", "display", "provenance"}:
        number = value.get("value")
        provenance = value.get("provenance")
        if (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(float(number))
            or not isinstance(provenance, Mapping)
            or not isinstance(provenance.get("runIds"), list)
            or not provenance["runIds"]
            or any(item not in run_ids for item in provenance["runIds"])
            or not _valid_json_pointer_syntax(provenance.get("jsonPointer"))
        ):
            raise ViewerExportError(f"{path} metric provenance is incomplete")
        _validate_artifact_ref(provenance.get("artifact"), f"{path}.provenance")
        return
    for key, item in value.items():
        _walk_contract(item, run_ids=run_ids, path=f"{path}.{key}")


def validate_viewer_export(value: Mapping[str, Any]) -> None:
    """Validate the Python projection against the strict TypeScript contract."""

    required = {
        "schemaVersion",
        "exportId",
        "generatedAt",
        "title",
        "interfaceLanguage",
        "tccLanguage",
        "exportManifest",
        "strategies",
        "questions",
        "runs",
        "sourceRuns",
        "temporalEvidenceCases",
        "showcase",
        "strategyMatrix",
        "methods",
    }
    if set(value) != required or value.get("schemaVersion") != VIEWER_SCHEMA_VERSION:
        raise ViewerExportError("viewer root differs from contextlab.viewer.v1")
    if value.get("interfaceLanguage") != "en" or value.get("tccLanguage") != "pt-BR":
        raise ViewerExportError("viewer language contract changed")
    strategies = value.get("strategies")
    runs = value.get("runs")
    questions = value.get("questions")
    if not isinstance(strategies, list) or [
        row.get("id") for row in strategies
    ] != list(MEMORY_CONFIGURATIONS):
        raise ViewerExportError("viewer must contain exactly the M0-M4 lanes")
    if not isinstance(runs, list) or not runs or not isinstance(questions, list):
        raise ViewerExportError("viewer must contain saved questions and runs")
    detailed_run_ids = {str(run.get("id")) for run in runs if isinstance(run, Mapping)}
    if len(detailed_run_ids) != len(runs):
        raise ViewerExportError("viewer run IDs are missing or duplicated")
    source_runs = value.get("sourceRuns")
    if not isinstance(source_runs, list) or not source_runs:
        raise ViewerExportError("viewer source-run registry is missing")
    source_run_ids: set[str] = set()
    for source_run in source_runs:
        if not isinstance(source_run, Mapping):
            raise ViewerExportError("viewer source run is not an object")
        if set(source_run) != {
            "id",
            "suite",
            "taskId",
            "taskFamily",
            "strategyId",
            "reasoningEffort",
            "artifact",
            "jsonPointer",
        }:
            raise ViewerExportError("viewer source-run fields differ")
        run_id = source_run.get("id")
        pointer = source_run.get("jsonPointer")
        if (
            not isinstance(run_id, str)
            or not run_id
            or run_id in source_run_ids
            or source_run.get("suite") not in {"static", "temporal"}
            or not isinstance(source_run.get("taskId"), str)
            or not isinstance(source_run.get("taskFamily"), str)
            or not isinstance(source_run.get("strategyId"), str)
            or (
                source_run.get("reasoningEffort") is not None
                and not isinstance(source_run.get("reasoningEffort"), str)
            )
            or not _valid_json_pointer_syntax(pointer)
        ):
            raise ViewerExportError("viewer source-run identity is invalid")
        _validate_artifact_ref(
            source_run.get("artifact"), f"source run {run_id}.artifact"
        )
        source_run_ids.add(run_id)
    run_ids = detailed_run_ids | source_run_ids
    if not detailed_run_ids.issubset(source_run_ids):
        raise ViewerExportError("viewer source-run registry omits a detailed run")
    strategy_ids = set(MEMORY_CONFIGURATIONS)
    question_ids = {
        str(question.get("id"))
        for question in questions
        if isinstance(question, Mapping)
    }
    for question in questions:
        if not isinstance(question, Mapping):
            raise ViewerExportError("viewer question is not an object")
        comparison = question.get("comparisonRunIds")
        if (
            not isinstance(comparison, list)
            or len(comparison) != 5
            or any(item not in detailed_run_ids for item in comparison)
        ):
            raise ViewerExportError("each viewer question needs five exact saved runs")
        compared = {
            run.get("strategyId")
            for run in runs
            if isinstance(run, Mapping) and run.get("id") in comparison
        }
        if compared != strategy_ids:
            raise ViewerExportError("viewer comparison does not cover M0-M4 exactly")
    for run in runs:
        if not isinstance(run, Mapping):
            raise ViewerExportError("viewer run is not an object")
        if (
            run.get("questionId") not in question_ids
            or run.get("strategyId") not in strategy_ids
        ):
            raise ViewerExportError("viewer run references an unknown lane or question")
        if "graderPacket" in run or "score" in (
            run.get("metrics") if isinstance(run.get("metrics"), Mapping) else {}
        ):
            raise ViewerExportError("viewer run contains a private-evaluation slot")
        _validate_artifact_ref(
            run.get("executionFacts"), f"viewer run {run.get('id')}.executionFacts"
        )
        pipeline = run.get("pipeline")
        stages = pipeline.get("stages") if isinstance(pipeline, Mapping) else None
        if not isinstance(stages, list) or [
            stage.get("kind") for stage in stages
        ] != list(_STAGE_KINDS):
            raise ViewerExportError("every saved run must expose exactly seven stages")
        for stage in stages:
            candidates = stage.get("candidates")
            if not isinstance(candidates, list):
                raise ViewerExportError("viewer pipeline candidates are missing")
            candidate_ids: set[str] = set()
            for candidate in candidates:
                candidate_id = (
                    candidate.get("id") if isinstance(candidate, Mapping) else None
                )
                if (
                    not isinstance(candidate, Mapping)
                    or not isinstance(candidate_id, str)
                    or not candidate_id
                    or candidate_id in candidate_ids
                    or candidate.get("origin") not in {"corpus", "memory"}
                    or (
                        candidate.get("score") is not None
                        and not isinstance(candidate.get("score"), Mapping)
                    )
                ):
                    raise ViewerExportError(
                        "viewer pipeline candidate contract changed"
                    )
                candidate_ids.add(candidate_id)
            if stage.get("kind") in {
                "fusion",
                "reranking",
                "deduplication",
                "diversity",
            }:
                if stage.get("candidates") != [] or "uninstrumented" not in str(
                    stage.get("label")
                ):
                    raise ViewerExportError(
                        "empty stages must be explicitly uninstrumented"
                    )
        spans = run.get("traceSpans")
        if (
            not isinstance(spans, list)
            or not spans
            or not any(
                isinstance(span, Mapping) and span.get("toolResult") is not None
                for span in spans
            )
        ):
            raise ViewerExportError("each run needs a saved span and tool result")
    temporal_cases = value.get("temporalEvidenceCases")
    if not isinstance(temporal_cases, list) or not temporal_cases:
        raise ViewerExportError("viewer temporal evidence cases are missing")
    for case in temporal_cases:
        if not isinstance(case, Mapping) or set(case) != {
            "id",
            "title",
            "questionId",
            "baselineRunId",
            "memoryEvidenceRunId",
            "artifact",
            "events",
        }:
            raise ViewerExportError("viewer temporal evidence case fields differ")
        if (
            case.get("questionId") not in question_ids
            or case.get("baselineRunId") not in detailed_run_ids
            or case.get("memoryEvidenceRunId") not in detailed_run_ids
        ):
            raise ViewerExportError("viewer temporal evidence case references differ")
        events = case.get("events")
        if not isinstance(events, list) or len(events) < 2:
            raise ViewerExportError("viewer temporal evidence case has no event link")
        event_ids = {
            event.get("id")
            for event in events
            if isinstance(event, Mapping) and isinstance(event.get("id"), str)
        }
        for event in events:
            if not isinstance(event, Mapping) or event.get("state") not in {
                "active",
                "superseded",
            }:
                raise ViewerExportError("viewer temporal event fields differ")
            prior = event.get("supersedesEventId")
            if prior is not None and prior not in event_ids:
                raise ViewerExportError("viewer temporal event link is unknown")
    showcase = value.get("showcase")
    expected_showcase = {"retrievalWin", "temporalEvidence"}
    if not isinstance(showcase, Mapping) or not (
        set(showcase) == expected_showcase
        or set(showcase) == expected_showcase | {"executionFailure"}
    ):
        raise ViewerExportError("viewer showcase narratives are incomplete")
    for insight in showcase.values():
        if (
            not isinstance(insight, Mapping)
            or not insight.get("runIds")
            or any(item not in run_ids for item in insight.get("runIds", []))
        ):
            raise ViewerExportError("showcase narrative cites an unknown viewer run")
    if "executionFailure" in showcase:
        failed_ids = showcase["executionFailure"].get("runIds", [])
        run_by_id = {run.get("id"): run for run in runs if isinstance(run, Mapping)}
        if any(
            run_by_id.get(run_id, {}).get("executionStatus") != "failed"
            for run_id in failed_ids
        ):
            raise ViewerExportError(
                "execution-failure showcase requires an explicit public failed status"
            )
    methods = value.get("methods")
    reviewers = methods.get("reviewers") if isinstance(methods, Mapping) else None
    ai = reviewers.get("aiJudges") if isinstance(reviewers, Mapping) else None
    human = reviewers.get("human") if isinstance(reviewers, Mapping) else None
    if (
        not isinstance(ai, list)
        or [(row.get("modelId"), row.get("reasoningEffort")) for row in ai]
        != [("gpt-5.6-sol", "high"), ("claude-opus-5", "medium")]
        or not isinstance(human, Mapping)
        or human.get("name") != "Kevin Araujo"
        or human.get("soleHumanReviewer") is not True
    ):
        raise ViewerExportError("viewer methods panel differs from the frozen panel")
    source_map = methods.get("sourceMap") if isinstance(methods, Mapping) else None
    labels = {
        str(row.get("label")).lower()
        for row in source_map or []
        if isinstance(row, Mapping)
    }
    if not any("primary" in label for label in labels) or not any(
        "ai-brain" in label or "ai brain" in label for label in labels
    ):
        raise ViewerExportError("viewer methods omit the primary/AI-Brain source map")
    _walk_contract(value, run_ids=run_ids)


def _publication_target(repository: Path, path: Path) -> Path:
    """Return a lexical repository-relative target without following links."""

    if not path.is_absolute():
        raise ViewerExportError("viewer publication target must be absolute")
    try:
        relative = path.relative_to(repository)
    except ValueError as exc:
        raise ViewerExportError(
            "viewer publication target is outside the repository"
        ) from exc
    if relative == Path() or ".." in relative.parts or not relative.name:
        raise ViewerExportError("viewer publication target is unsafe")
    return relative


def _open_publication_parent(
    repository: Path, relative: Path, *, create: bool = True
) -> int:
    """Open/create a target parent through no-follow directory descriptors."""

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(repository, flags)
    try:
        for part in relative.parent.parts:
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o755, dir_fd=descriptor)
                except FileExistsError:
                    # A concurrent creator may have won.  The no-follow open
                    # below still determines whether it made a real directory.
                    pass
                child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _publication_inode(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _read_existing_publication(
    parent_fd: int,
    name: str,
    *,
    expected_inode: tuple[int, int] | None = None,
) -> tuple[bytes, tuple[int, int]] | None:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ViewerExportError("immutable viewer artifact target is unsafe")
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = -1
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or _publication_inode(
            before
        ) != _publication_inode(metadata):
            raise ViewerExportError(
                "immutable viewer artifact changed while it was opened"
            )
        chunks: list[bytes] = []
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        after = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        identity = _publication_inode(before)
        if (
            _publication_inode(after) != identity
            or _publication_inode(current) != identity
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
            or (expected_inode is not None and identity != expected_inode)
        ):
            raise ViewerExportError(
                "immutable viewer artifact changed while it was read"
            )
        return b"".join(chunks), identity
    except OSError as exc:
        raise ViewerExportError("cannot read immutable viewer artifact") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _unlink_publication_inode(
    parent_fd: int, name: str, *, expected_inode: tuple[int, int]
) -> None:
    """Remove only a viewer artifact created by this transaction."""

    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _publication_inode(current) == expected_inode:
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
    except OSError:
        pass


def _create_publication_file(parent_fd: int, name: str, data: bytes) -> tuple[int, int]:
    """Create the final publication pathname and bind it to its inode."""

    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = -1
    created_inode: tuple[int, int] | None = None
    completed = False
    try:
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
        except FileExistsError as exc:
            raise ViewerExportError(f"viewer publication collided at {name}") from exc
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ViewerExportError("immutable viewer artifact target is unsafe")
        created_inode = _publication_inode(metadata)
        written = 0
        while written < len(data):
            count = os.write(descriptor, data[written:])
            if count <= 0:
                raise OSError("short viewer artifact write")
            written += count
        os.fsync(descriptor)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(current.st_mode)
            or _publication_inode(current) != created_inode
        ):
            raise ViewerExportError(
                "immutable viewer artifact changed during publication"
            )
        os.fsync(parent_fd)
        completed = True
        return created_inode
    except OSError as exc:
        raise ViewerExportError("cannot create immutable viewer artifact") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if created_inode is not None and not completed:
            _unlink_publication_inode(parent_fd, name, expected_inode=created_inode)


def _publication_parent_still_bound(
    repository: Path, relative: Path, parent_fd: int
) -> bool:
    current_fd = -1
    try:
        current_fd = _open_publication_parent(repository, relative, create=False)
        return _publication_inode(os.fstat(current_fd)) == _publication_inode(
            os.fstat(parent_fd)
        )
    except OSError:
        return False
    finally:
        if current_fd >= 0:
            os.close(current_fd)


def _preflight_and_publish(repository: Path, plan: Mapping[Path, bytes]) -> None:
    """Publish immutable files without following any target-path symlink."""

    lexical_repository = repository.absolute()
    repository = lexical_repository.resolve(strict=True)
    targets = [
        (_publication_target(lexical_repository, path.absolute()), data)
        for path, data in plan.items()
    ]
    entries: list[tuple[Path, bytes, int, tuple[int, int] | None]] = []
    created: list[tuple[Path, bytes, int, tuple[int, int]]] = []
    try:
        for relative, data in targets:
            try:
                parent_fd = _open_publication_parent(repository, relative)
            except OSError as exc:
                raise ViewerExportError(
                    f"viewer publication parent is unsafe: {relative.parent}"
                ) from exc
            try:
                existing = _read_existing_publication(parent_fd, relative.name)
            except Exception:
                os.close(parent_fd)
                raise
            identity = existing[1] if existing is not None else None
            entries.append((relative, data, parent_fd, identity))
            if existing is not None and existing[0] != data:
                raise ViewerExportError(
                    f"immutable viewer artifact differs: {repository / relative}"
                )

        for relative, data, parent_fd, identity in entries:
            if identity is not None:
                continue
            if not _publication_parent_still_bound(repository, relative, parent_fd):
                raise ViewerExportError(
                    f"viewer publication parent changed: {relative.parent}"
                )
            created_identity = _create_publication_file(parent_fd, relative.name, data)
            created.append((relative, data, parent_fd, created_identity))

        expected_identities = {
            (relative, parent_fd): identity
            for relative, _data, parent_fd, identity in entries
            if identity is not None
        }
        expected_identities.update(
            {
                (relative, parent_fd): identity
                for relative, _data, parent_fd, identity in created
            }
        )
        for relative, data, parent_fd, _identity in entries:
            if not _publication_parent_still_bound(repository, relative, parent_fd):
                raise ViewerExportError(
                    f"viewer publication parent changed: {relative.parent}"
                )
            saved = _read_existing_publication(
                parent_fd,
                relative.name,
                expected_inode=expected_identities[(relative, parent_fd)],
            )
            if saved is None or saved[0] != data:
                raise ViewerExportError(
                    f"immutable viewer artifact differs: {repository / relative}"
                )
    except Exception:
        for relative, _data, parent_fd, identity in reversed(created):
            _unlink_publication_inode(parent_fd, relative.name, expected_inode=identity)
        raise
    finally:
        for _relative, _data, parent_fd, _identity in entries:
            os.close(parent_fd)


def export_g4_viewer(root: Path | None = None) -> dict[str, Any]:
    """Validate canonical evidence and publish the immutable G4 viewer bundle."""

    root = (root or repository_root()).resolve()
    evidence, generated_at = _load_evidence(root)
    selection = _select_evidence(evidence)
    questions_seed, _selected_demo_receipts, receipts_by_id = _selected_receipts(
        evidence, selection
    )
    export_id = f"g4-{str(evidence.g3_gate['artifact_sha256'])[:16]}"

    catalog = _ArtifactCatalog(root)
    static_sources = _corpus_source_artifacts(root, catalog)
    static_sections = _corpus_section_artifacts(root, catalog)
    freeze_ref = catalog.derived_artifact(
        _json_bytes(_public_freeze_projection(evidence.g3_freeze)),
        name="g3-public-freeze.public.json",
        kind="configuration",
        label="G3 public run-specification projection",
        media_type="application/json",
    )
    event_ref = catalog.artifact(
        TEMPORAL_EVENTS_PATH, "corpus", "Public NovaLearn temporal event stream"
    )
    event_sections = _event_section_artifacts(root, evidence.events, catalog)
    g2_lab_ref = catalog.artifact(
        G2_COMPONENT_LAB_PATH, "report", "G2 public component lab"
    )
    temporal_r0_ref = catalog.artifact(
        G3_TEMPORAL_R0_LAB_PATH, "report", "G3 temporal R0 retrieval lab"
    )
    memory_protocol_ref = catalog.artifact(
        MEMORY_PROTOCOL_PATH, "method", "Frozen G3 memory protocol"
    )
    review_protocol_ref = catalog.artifact(
        REVIEW_PROTOCOL_PATH, "method", "Frozen three-member review protocol"
    )
    roadmap_ref = catalog.artifact(
        ROADMAP_PATH, "source", "ContextLab source and AI-Brain map"
    )
    g2_gate_ref = catalog.artifact(G2_GATE_PATH, "report", "Kevin-approved G2 gate")
    g3_gate_ref = catalog.artifact(G3_GATE_PATH, "report", "Kevin-approved G3 gate")

    cells = _cell_by_run_id(evidence.public_run)
    event_by_id = _event_index(evidence.events)
    retriever_traces = _retriever_trace_index(
        (
            (evidence.g2_lab, g2_lab_ref),
            (evidence.temporal_r0_lab, temporal_r0_ref),
        )
    )
    runs: list[dict[str, Any]] = []
    question_artifact = freeze_ref
    questions: list[dict[str, Any]] = []
    for seed in questions_seed:
        question = dict(seed)
        question.pop("_artifact_pointer")
        question["artifact"] = dict(question_artifact)
        questions.append(question)
        for run_id in question["comparisonRunIds"]:
            cell = cells.get(run_id)
            if cell is None:
                raise ViewerExportError(
                    "selected run is missing from public G3 manifest"
                )
            runs.append(
                _run_record(
                    receipts_by_id[run_id],
                    cell=cell,
                    question_id=str(question["id"]),
                    generated_at=generated_at,
                    catalog=catalog,
                    static_sources=static_sources,
                    static_sections=static_sections,
                    freeze_artifact=freeze_ref,
                    event_artifact=event_ref,
                    event_sections=event_sections,
                    events=event_by_id,
                    retriever_traces=retriever_traces,
                )
            )

    source_runs = _source_run_records(
        root,
        evidence,
        selection,
        catalog=catalog,
        g2_lab_artifact=g2_lab_ref,
        temporal_lab_artifact=temporal_r0_ref,
    )
    matrix_evidence = _matrix_evidence(evidence)
    # All public source refs must be catalogued before the manifest inventory is
    # frozen.  The manifest itself is then serialized and content-addressed.
    manifest = _build_manifest(
        export_id=export_id,
        generated_at=generated_at,
        evidence=evidence,
        selection=selection,
        matrix=matrix_evidence,
        catalog=catalog,
    )
    manifest_bytes = _json_bytes(manifest)
    manifest_ref = _manifest_artifact(manifest_bytes)

    strategies = [
        {
            "id": policy,
            "label": _POLICY_LABELS[policy][0],
            "summary": _POLICY_LABELS[policy][1],
            "artifact": dict(freeze_ref),
        }
        for policy in MEMORY_CONFIGURATIONS
    ]
    temporal_runs = [selection.baseline_run_id, selection.memory_evidence_run_id]
    showcase: dict[str, Any] = {
        "retrievalWin": {
            "title": "Measured G2 retrieval difference",
            "explanation": (
                f"Saved task {selection.retrieval_win['task_id']} records a higher "
                f"{selection.retrieval_win['metric']} value for "
                f"{selection.retrieval_win['strategy_id']} than for "
                f"{selection.retrieval_win['parent_strategy_id']}. The two exact "
                "public component runs remain linked below."
            ),
            "runIds": [
                str(selection.retrieval_win["parent_run_id"]),
                str(selection.retrieval_win["candidate_run_id"]),
            ],
            "artifact": g2_lab_ref,
        },
        "temporalEvidence": {
            "title": "Saved runs linked to a public event transition",
            "explanation": (
                f"Task {selection.temporal_group[0]} has an M0 execution and a "
                "memory-enabled execution whose saved evidence rows reference the "
                "same linked public event sequence. This view does not publish or "
                "infer an evaluation disposition."
            ),
            "runIds": temporal_runs,
            "artifact": event_ref,
        },
    }
    if selection.execution_failure_run_id is not None:
        failed_run = next(
            run for run in runs if run["id"] == selection.execution_failure_run_id
        )
        showcase["executionFailure"] = {
            "title": "Saved provider execution failure",
            "explanation": (
                "This run is included only because its sanitized public execution "
                "receipt has status=failed; evaluation dispositions are not used."
            ),
            "runIds": [selection.execution_failure_run_id],
            "artifact": failed_run["executionFacts"],
        }
    payload: dict[str, Any] = {
        "schemaVersion": VIEWER_SCHEMA_VERSION,
        "exportId": export_id,
        "generatedAt": generated_at,
        "title": "ContextLab G4 · Public evidence viewer",
        "interfaceLanguage": "en",
        "tccLanguage": "pt-BR",
        "exportManifest": manifest_ref,
        "strategies": strategies,
        "questions": questions,
        "runs": runs,
        "sourceRuns": source_runs,
        "temporalEvidenceCases": [
            _timeline_case(
                selection=selection,
                evidence=evidence,
                manifest_artifact=manifest_ref,
                event_artifact=event_ref,
            )
        ],
        "showcase": showcase,
        "strategyMatrix": _strategy_matrix(matrix_evidence, manifest_ref),
        "methods": {
            "experimentalContract": memory_protocol_ref,
            "limitations": [
                "The matrix covers the complete public G3 factorial and keeps sealed cells outside the publication.",
                "The matrix reports only public execution, context, cost, and evidence-row counts; unscored fallback candidates remain visibly unscored.",
                "G2 component wins do not override Kevin's final retained-retriever decision.",
                "Uninstrumented pipeline stages are empty and explicitly bound to the saved trace artifact.",
                "Only explicitly projected public execution fields are copied from run artifacts.",
            ],
            "v1V2Boundary": (
                "ContextLab v1 is frozen historical evidence. This viewer exposes only "
                "validated v2 public artifacts selected after G3."
            ),
            "reviewBoundary": (
                "The review panel contains GPT-5.6 Sol at high reasoning, Claude Opus 5 "
                "at medium reasoning, and Kevin Araujo as the sole human reviewer."
            ),
            "sealedDataBoundary": (
                "Sealed inputs were used only through content-free gate commitments. They "
                "and their protected gold or scoring data are not copied into the viewer."
            ),
            "novaLearnSyntheticStatement": (
                "NovaLearn AI and its public corpus are synthetic research materials; they "
                "do not describe a real company or real customers."
            ),
            "portugueseSummary": (
                "Este visor publica somente artefatos públicos validados do ContextLab. "
                "O corpus NovaLearn é sintético, Kevin Araujo é o único revisor humano e "
                "os dados selados permanecem fora da publicação."
            ),
            "reviewers": _reviewers(evidence.review_protocol, review_protocol_ref),
            "sourceMap": [
                {
                    "label": "Primary public evidence",
                    "description": (
                        "Saved G2/G3 labs, gates, sanitized execution receipts, and the synthetic temporal source stream."
                    ),
                    "artifacts": [
                        g2_lab_ref,
                        temporal_r0_ref,
                        event_ref,
                        g2_gate_ref,
                        g3_gate_ref,
                    ],
                },
                {
                    "label": "AI-Brain planning map",
                    "description": (
                        "Local synthesis informed planning only; the roadmap maps it separately "
                        "from primary research evidence."
                    ),
                    "artifacts": [roadmap_ref],
                },
            ],
        },
    }
    validate_viewer_export(payload)
    export_bytes = _json_bytes(payload)

    manifest_public_path = _contained_path(
        root,
        VIEWER_ARTIFACT_ROOT / manifest_ref["sha256"] / VIEWER_MANIFEST_PATH.name,
        label="public viewer manifest copy",
        must_exist=False,
    )
    manifest_path = _contained_path(
        root, VIEWER_MANIFEST_PATH, label="viewer manifest", must_exist=False
    )
    export_path = _contained_path(
        root, VIEWER_EXPORT_PATH, label="viewer export", must_exist=False
    )
    plan = dict(catalog.copy_plan)
    plan[manifest_public_path] = manifest_bytes
    plan[export_path] = export_bytes
    # The results manifest is the publication commit marker and is created last.
    plan[manifest_path] = manifest_bytes
    validate_viewer_artifact_pointers(payload, root, pending_artifacts=plan)
    _preflight_and_publish(root, plan)
    return {
        "status": "published",
        "export_id": export_id,
        "export_path": VIEWER_EXPORT_PATH.as_posix(),
        "export_sha256": _sha256_bytes(export_bytes),
        "manifest_path": VIEWER_MANIFEST_PATH.as_posix(),
        "manifest_sha256": manifest_ref["sha256"],
        "public_artifact_count": len(catalog.copy_plan) + 1,
        "run_count": len(runs),
        "question_count": len(questions),
    }
