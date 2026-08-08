"""Post-G4 freeze, release, import, and final three-member review analysis.

The protected bundle and every reviewer-visible packet stay outside the repository.
Only content-free commitments, import receipts, and grade-derived reports may be
written below ``results/v2``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
import hashlib
from itertools import combinations
import json
import math
import os
from pathlib import Path
import re
from typing import Any

from .baseline import repository_root
from .immutable_io import (
    ImmutableIOError,
    read_bytes_snapshot,
    write_json_once_or_verify,
)
from .final_review_ai import (
    FinalReviewAIError,
    validate_final_review_ai_manifest,
)
from .review import (
    AI_KEVIN_ACCEPTED_MATCH_MIN,
    AI_KEVIN_MEAN_ABSOLUTE_DIFFERENCE_MAX,
    CALIBRATION_ACCEPTED_MATCH_MIN,
    CALIBRATION_EXACT_ORDINAL_MIN,
    CALIBRATION_WITHIN_ONE_MIN,
    HIDDEN_REPEAT_COUNT,
    MAIN_CELL_COUNT,
    PACKET_TOKEN_VERIFIER_ID,
    PACKET_TOKEN_VERIFIER_SHA256,
    PINNED_REVIEW_TOKEN_PROFILES,
    REASONING_EFFORTS,
    REVIEWERS,
    STRATEGY_LANES,
    ReviewContractError,
    ReviewStore,
    aggregate_completed_panel,
    build_review_packets,
    evaluate_calibration,
    harden_external_review_file,
    harden_external_review_tree,
    hidden_repeat_consistency,
    read_external_bytes_snapshot,
    replay_review_release,
    release_calibration_packets,
    release_review_packets,
    validate_grade,
    validate_public_packets,
    validate_review_protocol,
    verified_review_assignments,
    verified_reviewer_packet_payloads,
    verify_packet_token_preflight,
    write_external_bytes_once_or_verify,
)
from .tasking import sha256_json, validate_split_manifest
from .statistics import (
    StatisticsError,
    distribution_summary,
    paired_bootstrap_ci,
    task_family_effect_summaries,
)

FINAL_REVIEW_BUNDLE_SCHEMA = "contextlab.final-review-protected-bundle.v1"
FINAL_REVIEW_FREEZE_SCHEMA = "contextlab.final-review-freeze.v2"
FINAL_REVIEW_PREFLIGHT_SCHEMA = "contextlab.final-review-preflight.v2"
FINAL_REVIEW_CONFIRMATION_SCHEMA = "contextlab.final-review-confirmation.v2"
FINAL_REVIEW_CALIBRATION_RETURN_SCHEMA = "contextlab.final-review-calibration-return.v1"
FINAL_REVIEW_RETURN_SCHEMA = "contextlab.final-review-return.v1"
FINAL_REVIEW_IMPORT_SCHEMA = "contextlab.final-review-import.v2"
FINAL_REVIEW_REPORT_SCHEMA = "contextlab.final-review-report.v2"
FINAL_REVIEW_STATISTICAL_PLAN_SCHEMA = "contextlab.final-review-statistical-plan.v1"
FINAL_REVIEW_STATISTICAL_ANALYSIS_SCHEMA = (
    "contextlab.final-review-statistical-analysis.v1"
)

FINAL_REVIEW_PRIMARY_METRIC = "panel_accepted"
FINAL_REVIEW_SECONDARY_METRIC = "panel_overall_ordinal"
FINAL_REVIEW_BOOTSTRAP_RESAMPLES = 10_000
FINAL_REVIEW_BOOTSTRAP_SEED_NAME = "contextlab-final-review-paired-bootstrap-v1"
FINAL_REVIEW_BOOTSTRAP_SEED = int.from_bytes(
    hashlib.sha256(FINAL_REVIEW_BOOTSTRAP_SEED_NAME.encode("utf-8")).digest()[:8],
    "big",
)

FINAL_REVIEW_FREEZE_PATH = Path("results/v2/reviews/final/freeze.json")
FINAL_REVIEW_PREFLIGHT_PATH = Path("results/v2/reviews/final/preflight.json")
FINAL_REVIEW_CONFIRMATION_PATH = Path(
    "results/v2/reviews/final/preflight-confirmation.json"
)
FINAL_REVIEW_REPORT_PATH = Path("results/v2/reviews/final/report.json")
FINAL_REVIEW_IMPORT_DIRECTORY = Path("results/v2/reviews/final/imports")

TASK_SPLIT_PATH = Path("results/v2/splits/task_split_manifest.json")
G2_GATE_PATH = Path("results/v2/gates/G2.json")
G3_GATE_PATH = Path("results/v2/gates/G3.json")
G4_GATE_PATH = Path("results/v2/gates/G4.json")
REVIEW_PROTOCOL_PATH = Path("evaluation/v2/review_protocol.json")
RETRIEVAL_PROTOCOL_PATH = Path("evaluation/v2/retrieval_protocol.json")
MEMORY_PROTOCOL_PATH = Path("evaluation/v2/memory_protocol.json")

_FRONTIER_EXPERIMENT_IDS = ("F1", "F2", "F3", "F4", "F5", "F6", "F7")
_FRONTIER_RESULT_CONTRACT_EXPERIMENT_IDS = frozenset(("F1", "F2", "F3"))
_FRONTIER_FINAL_DISPOSITIONS = frozenset(("promoted", "accepted-negative"))

MAIN_RETURN_GRADE_COUNT = MAIN_CELL_COUNT + HIDDEN_REPEAT_COUNT
AGREEMENT_STRATUM_MIN_CELLS = 20
MATERIAL_EXACT_RATE_DROP_MAX = 0.10
MATERIAL_WITHIN_ONE_RATE_DROP_MAX = 0.05
MATERIAL_ACCEPTED_RATE_DROP_MAX = 0.10
MATERIAL_MEAN_ABSOLUTE_DIFFERENCE_INCREASE_MAX = 0.25
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_UTC_SECOND = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_MAIN_CELL_FIELDS = frozenset(
    {
        "task_id",
        "lane_id",
        "reasoning_effort",
        "lane_binding_sha256",
        "question",
        "candidate_answer",
        "candidate_sha256",
        "cited_evidence",
    }
)
_CALIBRATION_CELL_FIELDS = frozenset(
    {
        "calibration_id",
        "question",
        "candidate_answer",
        "candidate_sha256",
        "cited_evidence",
    }
)
_RETURN_FIELDS = frozenset(
    {
        "schema_version",
        "reviewer",
        "review_manifest_sha256",
        "release_manifest_sha256",
        "phase",
        "grade_count",
        "grades",
        "rubric_ambiguous",
        "review_comment",
        "artifact_sha256",
    }
)
_GRADE_ROW_FIELDS = frozenset({"blind_cell_id", "grade"})
_CITATION_FIELDS = frozenset({"reference", "text"})


class FinalReviewError(ValueError):
    """The final review is premature, incomplete, unsafe, or stale."""


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _absolute_path(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    for alias in (Path("/var"), Path("/tmp")):
        try:
            relative = absolute.relative_to(alias)
        except ValueError:
            continue
        if alias.is_symlink():
            absolute = alias.resolve() / relative
        break
    return absolute


def _decode_json(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FinalReviewError(f"cannot decode {label}") from exc
    if not isinstance(value, dict):
        raise FinalReviewError(f"{label} must be a JSON object")
    return value


def _outside_repository(path: Path, root: Path, label: str) -> Path:
    repository = root.resolve()
    absolute = _absolute_path(path)
    try:
        absolute.relative_to(repository)
    except ValueError:
        return absolute
    raise FinalReviewError(f"{label} must stay outside the repository")


def _safe_repository_target(root: Path, path: Path, label: str) -> Path:
    repository = root.resolve()
    target = path if path.is_absolute() else repository / path
    absolute = _absolute_path(target)
    try:
        relative = absolute.relative_to(repository)
    except ValueError as exc:
        raise FinalReviewError(f"{label} must stay inside the repository") from exc
    if (
        len(relative.parts) < 3
        or relative.parts[:2] != ("results", "v2")
        or ".." in relative.parts
        or not relative.name
    ):
        raise FinalReviewError(f"{label} must stay below results/v2")
    return absolute


def _canonical_repository_target(
    root: Path,
    requested: Path | None,
    canonical: Path,
    label: str,
) -> Path:
    """Reject alternate outputs when later stages replay one fixed path."""

    target = _safe_repository_target(root, requested or canonical, label)
    expected = _safe_repository_target(root, canonical, label)
    if target != expected:
        raise FinalReviewError(f"{label} output must use the canonical path")
    return target


def _read_external_snapshot(
    path: Path, root: Path, label: str
) -> tuple[dict[str, Any], bytes]:
    external = _outside_repository(path, root, label)
    try:
        payload = read_external_bytes_snapshot(external, label=label)
    except ReviewContractError as exc:
        raise FinalReviewError(f"cannot read {label}") from exc
    return _decode_json(payload, label), payload


def _read_repository_snapshot(
    root: Path, path: Path, label: str
) -> tuple[dict[str, Any], bytes]:
    target = path if path.is_absolute() else root / path
    try:
        payload = read_bytes_snapshot(root, target)
    except ImmutableIOError as exc:
        raise FinalReviewError(f"cannot read {label}") from exc
    return _decode_json(payload, label), payload


def _write_repository_once(
    root: Path, path: Path, value: Mapping[str, Any], label: str
) -> None:
    target = _safe_repository_target(root, path, label)
    try:
        write_json_once_or_verify(root, target, value)
    except ImmutableIOError as exc:
        raise FinalReviewError(f"cannot publish {label}: {exc}") from exc


def _require_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise FinalReviewError(f"{label} must be a lowercase SHA-256")
    return value


def _load_json_and_hash(
    root: Path, relative: Path, label: str
) -> tuple[dict[str, Any], str]:
    value, payload = _read_repository_snapshot(root, relative, label)
    return value, hashlib.sha256(payload).hexdigest()


def _load_approved_gates(
    root: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Replay G4 first, before any protected input is opened."""

    from .g3_freeze import load_approved_g2_gate
    from .g4_gate import load_approved_g4_gate
    from .viewer_export import require_replayed_approved_g3_gate

    try:
        g4 = load_approved_g4_gate(root)
        g2 = load_approved_g2_gate(root)
        g3 = require_replayed_approved_g3_gate(root)
    except (ValueError, OSError) as exc:
        raise FinalReviewError(
            "final review remains locked until Kevin-approved G4 replays"
        ) from exc
    if (
        g4.get("final_decision") != "passed"
        or g4.get("human_approval", {}).get("status") != "approved"
        or g4.get("human_approval", {}).get("reviewer") != "Kevin Araujo"
        or g2.get("human_approval", {}).get("status") != "approved"
        or g3.get("human_decision", {}).get("status") != "recorded"
    ):
        raise FinalReviewError("G2, G3, and G4 owner decisions are incomplete")
    return g2, g3, g4


def _load_frontier_completion(root: Path) -> dict[str, Any]:
    """Replay the complete frontier queue before opening protected review inputs."""

    from .frontier import load_approved_frontier_entry_gate
    from .frontier_review import load_approved_frontier_result

    try:
        entry = load_approved_frontier_entry_gate(root)
    except Exception as exc:
        raise FinalReviewError(
            "final review remains locked until the Kevin-approved frontier entry gate replays"
        ) from exc

    entry_artifact_sha256 = _require_sha(
        entry.get("artifact_sha256"), "frontier entry gate artifact hash"
    )
    experiments = entry.get("experiments")
    if not isinstance(experiments, list) or len(experiments) != len(
        _FRONTIER_EXPERIMENT_IDS
    ):
        raise FinalReviewError("frontier entry gate does not cover F1-F7")

    dispositions: dict[str, dict[str, str]] = {}
    eligible: list[str] = []
    for expected_id, experiment in zip(
        _FRONTIER_EXPERIMENT_IDS, experiments, strict=True
    ):
        if (
            not isinstance(experiment, Mapping)
            or experiment.get("experiment_id") != expected_id
        ):
            raise FinalReviewError("frontier entry gate experiment order changed")
        entry_disposition = experiment.get("technical_decision")
        if entry_disposition == "failed-entry":
            dispositions[expected_id] = {"entry_disposition": "failed-entry"}
            continue
        if entry_disposition != "eligible":
            raise FinalReviewError(
                f"{expected_id} frontier entry disposition is invalid"
            )
        eligible.append(expected_id)

    unsupported = [
        experiment_id
        for experiment_id in eligible
        if experiment_id not in _FRONTIER_RESULT_CONTRACT_EXPERIMENT_IDS
    ]
    if unsupported:
        raise FinalReviewError(
            f"{unsupported[0]} is eligible but no runner/result contract exists"
        )

    for experiment_id in eligible:
        try:
            result = load_approved_frontier_result(root, experiment_id)
        except Exception as exc:
            raise FinalReviewError(
                f"{experiment_id} has no current Kevin-approved final result"
            ) from exc
        final_disposition = result.get("final_status")
        if final_disposition not in _FRONTIER_FINAL_DISPOSITIONS:
            raise FinalReviewError(
                f"{experiment_id} final frontier result disposition is invalid"
            )
        dispositions[experiment_id] = {
            "entry_disposition": "eligible",
            "result_disposition": str(final_disposition),
            "result_artifact_sha256": _require_sha(
                result.get("artifact_sha256"),
                f"{experiment_id} final frontier result artifact hash",
            ),
        }

    if set(dispositions) != set(_FRONTIER_EXPERIMENT_IDS):
        raise FinalReviewError("frontier completion dispositions are incomplete")
    return {
        "frontier_entry_gate_artifact_sha256": entry_artifact_sha256,
        "frontier_experiment_dispositions": {
            experiment_id: dispositions[experiment_id]
            for experiment_id in _FRONTIER_EXPERIMENT_IDS
        },
    }


def derive_final_lane_bindings(
    g2_gate: Mapping[str, Any],
    g3_gate: Mapping[str, Any],
    *,
    retrieval_protocol: Mapping[str, Any],
    memory_protocol: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Derive all lane identities; external callers cannot name source aliases."""

    if g2_gate.get("final_decision") not in {"promote", "retain-simple"}:
        raise FinalReviewError("G2 has no final retrieval decision")
    if g3_gate.get("final_decision") not in {"promote", "retain-simple"}:
        raise FinalReviewError("G3 has no final memory decision")
    methods = retrieval_protocol.get("methods")
    policies = memory_protocol.get("surface", {}).get("memory_policies")
    if not isinstance(methods, Mapping) or "R0" not in methods:
        raise FinalReviewError("retrieval protocol has no canonical R0 baseline")
    if not isinstance(policies, list) or policies[:1] != ["M0"]:
        raise FinalReviewError("memory protocol has no canonical M0 baseline")
    experimental_retrievers = sorted(
        (str(method) for method in methods if method != "R0"),
        key=lambda value: int(value[1:]) if value[1:].isdigit() else math.inf,
    )
    experimental_memories = [str(policy) for policy in policies if policy != "M0"]
    if not experimental_retrievers or not experimental_memories:
        raise FinalReviewError("no tested v2 failure fallback is frozen")

    g2_promoted = g2_gate.get("final_decision") == "promote"
    g3_promoted = g3_gate.get("final_decision") == "promote"
    promoted_retriever: object | None = None
    promoted_memory: object | None = None
    if g2_promoted:
        promoted_retriever = g2_gate.get("promoted_retriever_id")
        if (
            not isinstance(promoted_retriever, str)
            or promoted_retriever not in methods
            or promoted_retriever == "R0"
        ):
            raise FinalReviewError("G2 promoted retriever is invalid")
    if g3_promoted:
        promoted_memory = g3_gate.get("promoted_memory_policy")
        if (
            not isinstance(promoted_memory, str)
            or promoted_memory not in policies
            or promoted_memory == "M0"
        ):
            raise FinalReviewError("G3 promoted memory policy is invalid")

    promoted = g2_promoted or g3_promoted
    static_retriever = (
        promoted_retriever
        if g2_promoted
        else "R0"
        if g3_promoted
        else experimental_retrievers[0]
    )
    temporal_retriever = promoted_retriever if g2_promoted else "R0"
    temporal_memory = (
        promoted_memory
        if g3_promoted
        else "M0"
        if g2_promoted
        else experimental_memories[0]
    )
    bindings: dict[str, dict[str, Any]] = {
        "full_context": {
            "lane_id": "full_context",
            "source_system": "v1_full_context",
            "lane_status": "baseline",
        },
        "v1_dense_rag": {
            "lane_id": "v1_dense_rag",
            "source_system": "v1_dense_semantic_rag",
            "retriever_id": "R0",
            "lane_status": "baseline",
        },
        "compiled_wiki": {
            "lane_id": "compiled_wiki",
            "source_system": "v1_compiled_wiki",
            "lane_status": "baseline",
        },
        "text_to_sql": {
            "lane_id": "text_to_sql",
            "source_system": "v1_text_to_sql",
            "lane_status": "baseline",
        },
        "promoted_v2": {
            "lane_id": "promoted_v2",
            "source_system": "contextlab_v2",
            "suite_mode": "task_suite_specific",
            "static_retriever_id": static_retriever,
            "temporal_retriever_id": temporal_retriever,
            "temporal_memory_policy": temporal_memory,
            "g2_decision": g2_gate["final_decision"],
            "g3_decision": g3_gate["final_decision"],
            "lane_status": "promoted" if promoted else "experimental_failure",
        },
    }
    if tuple(bindings) != STRATEGY_LANES:
        raise FinalReviewError("final lane order differs from the locked contract")
    return {
        lane: {**binding, "binding_sha256": sha256_json(binding)}
        for lane, binding in bindings.items()
    }


def _canonical_inputs(root: Path) -> dict[str, Any]:
    from .experiments import validate_protocol as validate_retrieval_protocol
    from .g3_freeze import validate_memory_protocol

    g2, g3, g4 = _load_approved_gates(root)
    frontier_completion = _load_frontier_completion(root)
    split, split_file_sha = _load_json_and_hash(root, TASK_SPLIT_PATH, "task split")
    try:
        validate_split_manifest(split)
    except ValueError as exc:
        raise FinalReviewError("final task split is invalid") from exc
    review_protocol, review_sha = _load_json_and_hash(
        root, REVIEW_PROTOCOL_PATH, "review protocol"
    )
    retrieval_protocol, retrieval_sha = _load_json_and_hash(
        root, RETRIEVAL_PROTOCOL_PATH, "retrieval protocol"
    )
    memory_protocol, memory_sha = _load_json_and_hash(
        root, MEMORY_PROTOCOL_PATH, "memory protocol"
    )
    try:
        validate_review_protocol(root / REVIEW_PROTOCOL_PATH)
        validate_retrieval_protocol(retrieval_protocol)
        validate_memory_protocol(memory_protocol)
    except (RuntimeError, ValueError) as exc:
        raise FinalReviewError(
            "one or more final review protocols are invalid"
        ) from exc
    lanes = derive_final_lane_bindings(
        g2,
        g3,
        retrieval_protocol=retrieval_protocol,
        memory_protocol=memory_protocol,
    )
    binding = {
        "task_split_manifest_sha256": split["manifest_sha256"],
        "task_split_file_sha256": split_file_sha,
        "g2_gate_artifact_sha256": _require_sha(
            g2.get("artifact_sha256"), "G2 gate artifact hash"
        ),
        "g3_gate_artifact_sha256": _require_sha(
            g3.get("artifact_sha256"), "G3 gate artifact hash"
        ),
        "g4_gate_artifact_sha256": _require_sha(
            g4.get("artifact_sha256"), "G4 gate artifact hash"
        ),
        "review_protocol_file_sha256": review_sha,
        "retrieval_protocol_file_sha256": retrieval_sha,
        "memory_protocol_file_sha256": memory_sha,
        "lane_bindings_sha256": sha256_json(lanes),
        **frontier_completion,
    }
    return {
        "g2": g2,
        "g3": g3,
        "g4": g4,
        "split": split,
        "lanes": lanes,
        "repository_binding": binding,
        "review_protocol": review_protocol,
    }


def _validate_citations(value: object, label: str) -> None:
    if not isinstance(value, list):
        raise FinalReviewError(f"{label} citations must be a list")
    for citation in value:
        if (
            not isinstance(citation, dict)
            or set(citation) != _CITATION_FIELDS
            or any(not isinstance(citation[field], str) for field in _CITATION_FIELDS)
        ):
            raise FinalReviewError(f"{label} citation fields differ")


def _private_cells_from_bundle(
    bundle: Mapping[str, Any], canonical: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
    expected_bundle_fields = {
        "schema_version",
        "repository_binding",
        "main_cells",
        "calibration_cells",
        "artifact_sha256",
    }
    body = {key: item for key, item in bundle.items() if key != "artifact_sha256"}
    if (
        set(bundle) != expected_bundle_fields
        or bundle.get("schema_version") != FINAL_REVIEW_BUNDLE_SCHEMA
        or bundle.get("artifact_sha256") != sha256_json(body)
        or bundle.get("repository_binding") != canonical["repository_binding"]
    ):
        raise FinalReviewError("protected final-review bundle is stale or malformed")
    split = canonical["split"]
    expected_task_ids = {str(row["task_id"]) for row in split["tasks"]}
    task_families = {
        str(row["task_id"]): str(row.get("task_family", "")) for row in split["tasks"]
    }
    if any(not family for family in task_families.values()):
        raise FinalReviewError("final split contains an empty task family")
    sealed_ids = {
        str(row["task_id"])
        for row in split["tasks"]
        if row["partition"] == "sealed_capability"
    }
    if len(expected_task_ids) != 160 or len(sealed_ids) != 48:
        raise FinalReviewError(
            "final split does not contain 160 tasks and 48 sealed tasks"
        )
    main = bundle.get("main_cells")
    if not isinstance(main, list) or len(main) != MAIN_CELL_COUNT:
        raise FinalReviewError("protected bundle must contain exactly 1,600 main cells")
    expected_combinations = {
        (task_id, lane, effort)
        for task_id in expected_task_ids
        for lane in STRATEGY_LANES
        for effort in REASONING_EFFORTS
    }
    seen: set[tuple[str, str, str]] = set()
    private_main: list[dict[str, Any]] = []
    for index, row in enumerate(main):
        if not isinstance(row, dict) or set(row) != _MAIN_CELL_FIELDS:
            raise FinalReviewError(f"protected main cell {index} fields differ")
        task_id = str(row["task_id"])
        lane_id = str(row["lane_id"])
        effort = str(row["reasoning_effort"])
        combination = (task_id, lane_id, effort)
        if combination not in expected_combinations or combination in seen:
            raise FinalReviewError("main cell identity is unknown or repeated")
        seen.add(combination)
        lane = canonical["lanes"][lane_id]
        if row["lane_binding_sha256"] != lane["binding_sha256"]:
            raise FinalReviewError(
                "main cell uses a caller-chosen or stale lane binding"
            )
        question = row["question"]
        answer = row["candidate_answer"]
        if not isinstance(question, str) or not question.strip():
            raise FinalReviewError("main cell question must be non-empty text")
        if not isinstance(answer, str):
            raise FinalReviewError("main cell answer must be text")
        if (
            hashlib.sha256(answer.encode("utf-8")).hexdigest()
            != row["candidate_sha256"]
        ):
            raise FinalReviewError("main cell candidate hash mismatch")
        _validate_citations(row["cited_evidence"], "main cell")
        private_main.append(
            {
                "cell_id": f"FR-{task_id}-{lane_id}-{effort}",
                "task_id": task_id,
                "task_family": task_families[task_id],
                "question": question,
                "candidate_answer": answer,
                "cited_evidence": row["cited_evidence"],
                "candidate_sha256": row["candidate_sha256"],
                "strategy_id": lane_id,
                "reasoning_effort": effort,
            }
        )
    if seen != expected_combinations:
        raise FinalReviewError("protected main cells do not cover 160 x 5 x 2")

    calibration = bundle.get("calibration_cells")
    if not isinstance(calibration, list) or len(calibration) != 20:
        raise FinalReviewError("protected bundle must contain 20 calibration cells")
    expected_calibration_ids = {f"C{index:03d}" for index in range(1, 21)}
    seen_calibration: set[str] = set()
    private_calibration: list[dict[str, Any]] = []
    for index, row in enumerate(calibration):
        if not isinstance(row, dict) or set(row) != _CALIBRATION_CELL_FIELDS:
            raise FinalReviewError(f"calibration cell {index} fields differ")
        calibration_id = str(row["calibration_id"])
        if (
            calibration_id not in expected_calibration_ids
            or calibration_id in seen_calibration
        ):
            raise FinalReviewError("calibration cell identity is unknown or repeated")
        seen_calibration.add(calibration_id)
        answer = row["candidate_answer"]
        question = row["question"]
        if (
            not isinstance(question, str)
            or not question.strip()
            or not isinstance(answer, str)
        ):
            raise FinalReviewError("calibration question and answer must be text")
        if (
            hashlib.sha256(answer.encode("utf-8")).hexdigest()
            != row["candidate_sha256"]
        ):
            raise FinalReviewError("calibration candidate hash mismatch")
        _validate_citations(row["cited_evidence"], "calibration cell")
        private_calibration.append(
            {
                "cell_id": f"FR-CAL-{calibration_id}",
                "task_id": calibration_id,
                "task_family": "calibration",
                "question": question,
                "candidate_answer": answer,
                "cited_evidence": row["cited_evidence"],
                "candidate_sha256": row["candidate_sha256"],
                "strategy_id": "full_context",
                "reasoning_effort": "low",
            }
        )
    if seen_calibration != expected_calibration_ids:
        raise FinalReviewError("calibration bundle does not cover C001-C020")
    return private_main, private_calibration, sealed_ids


def _final_statistical_analysis_plan() -> dict[str, Any]:
    """Return the fixed analysis choices committed before reviewer release."""

    return {
        "schema_version": FINAL_REVIEW_STATISTICAL_PLAN_SCHEMA,
        "primary_metric": FINAL_REVIEW_PRIMARY_METRIC,
        "secondary_metric": FINAL_REVIEW_SECONDARY_METRIC,
        "unit_of_analysis": "task",
        "pairing_key": "task_id",
        "lane_effort_score": "one_three_member_panel_aggregate_per_task",
        "lane_task_score": "arithmetic_mean_across_low_and_high",
        "lane_comparisons": "all_unordered_pairs_in_frozen_lane_order",
        "reasoning_effort_contrast": "high_minus_low",
        "strategy_effort_interaction": ("difference_in_high_minus_low_vs_full_context"),
        "task_family_effect_size": "paired_cohens_dz",
        "confidence_interval": {
            "method": "paired_task_level_percentile_bootstrap",
            "confidence_level": 0.95,
            "resamples": FINAL_REVIEW_BOOTSTRAP_RESAMPLES,
            "seed_name": FINAL_REVIEW_BOOTSTRAP_SEED_NAME,
            "seed": FINAL_REVIEW_BOOTSTRAP_SEED,
            "seed_derivation": ("uint64_be(first_8_bytes(SHA256(UTF8(seed_name))))"),
            "percentile_interpolation": "linear_r_type_7",
            "seed_application": "same_seed_for_each_sorted_task_vector",
        },
        "claim_scopes": {
            "confirmatory": [
                "primary_metric_paired_lane_comparisons",
                "primary_metric_reasoning_effort_main_effect",
                "primary_metric_strategy_by_effort_interactions",
            ],
            "exploratory": [
                "secondary_metric_descriptives",
                "task_family_effect_sizes",
            ],
        },
    }


def validate_final_review_freeze(value: Mapping[str, Any]) -> None:
    expected = {
        "schema_version",
        "status",
        "repository_binding",
        "lane_bindings",
        "protected_bundle_sha256",
        "protected_bundle_artifact_sha256",
        "randomization_seed_sha256",
        "task_count",
        "sealed_task_count",
        "main_cell_count",
        "calibration_cell_count",
        "reviewers",
        "packet_manifest",
        "packet_manifest_sha256",
        "identity_map_sha256",
        "statistical_analysis_plan",
        "statistical_analysis_plan_sha256",
        "content_policy",
        "artifact_sha256",
    }
    body = {key: item for key, item in value.items() if key != "artifact_sha256"}
    if (
        set(value) != expected
        or value.get("schema_version") != FINAL_REVIEW_FREEZE_SCHEMA
        or value.get("status") != "frozen_not_released"
        or value.get("artifact_sha256") != sha256_json(body)
        or value.get("task_count") != 160
        or value.get("sealed_task_count") != 48
        or value.get("main_cell_count") != MAIN_CELL_COUNT
        or value.get("calibration_cell_count") != 20
        or value.get("reviewers") != list(REVIEWERS)
        or value.get("content_policy")
        != {
            "protected_bundle": "external_only",
            "reviewer_packets": "external_only",
            "identity_map": "external_only",
            "repository_artifact": "content_free_commitment_only",
        }
    ):
        raise FinalReviewError("final review freeze commitment is invalid")
    for field in (
        "protected_bundle_sha256",
        "protected_bundle_artifact_sha256",
        "randomization_seed_sha256",
        "packet_manifest_sha256",
        "identity_map_sha256",
        "statistical_analysis_plan_sha256",
    ):
        _require_sha(value.get(field), field)
    expected_plan = _final_statistical_analysis_plan()
    if value.get("statistical_analysis_plan") != expected_plan or value.get(
        "statistical_analysis_plan_sha256"
    ) != sha256_json(expected_plan):
        raise FinalReviewError("final statistical analysis plan changed")
    manifest = value.get("packet_manifest")
    if not isinstance(manifest, dict):
        raise FinalReviewError("final review freeze has no packet manifest")
    if manifest.get("manifest_sha256") != value.get("packet_manifest_sha256"):
        raise FinalReviewError("packet manifest binding changed")
    lanes = value.get("lane_bindings")
    if not isinstance(lanes, dict) or set(lanes) != set(STRATEGY_LANES):
        raise FinalReviewError("final lane bindings changed")
    binding = value.get("repository_binding")
    if not isinstance(binding, Mapping):
        raise FinalReviewError("final review repository binding is invalid")
    _require_sha(
        binding.get("frontier_entry_gate_artifact_sha256"),
        "frontier entry gate artifact hash",
    )
    frontier_dispositions = binding.get("frontier_experiment_dispositions")
    if (
        not isinstance(frontier_dispositions, Mapping)
        or tuple(frontier_dispositions) != _FRONTIER_EXPERIMENT_IDS
    ):
        raise FinalReviewError("frontier completion binding is invalid")
    for experiment_id, disposition in frontier_dispositions.items():
        if not isinstance(disposition, Mapping):
            raise FinalReviewError("frontier completion disposition is invalid")
        if disposition.get("entry_disposition") == "failed-entry":
            if set(disposition) != {"entry_disposition"}:
                raise FinalReviewError("failed frontier entry binding changed")
            continue
        if (
            disposition.get("entry_disposition") != "eligible"
            or set(disposition)
            != {
                "entry_disposition",
                "result_disposition",
                "result_artifact_sha256",
            }
            or disposition.get("result_disposition") not in _FRONTIER_FINAL_DISPOSITIONS
        ):
            raise FinalReviewError(
                f"{experiment_id} frontier result binding is invalid"
            )
        _require_sha(
            disposition.get("result_artifact_sha256"),
            f"{experiment_id} final frontier result artifact hash",
        )


def freeze_final_review(
    root: Path | None,
    *,
    protected_bundle_path: Path,
    seed_path: Path,
    staging_directory: Path,
    external_identity_map: Path,
    output: Path | None = None,
) -> dict[str, Any]:
    """Freeze the exact 1,600 cells and 85 packets per reviewer after G4."""

    repository = (root or repository_root()).resolve()
    canonical = _canonical_inputs(repository)  # Gate barrier precedes protected reads.
    bundle_path = _outside_repository(
        protected_bundle_path, repository, "protected final-review bundle"
    )
    seed_file = _outside_repository(seed_path, repository, "review randomization seed")
    bundle, bundle_bytes = _read_external_snapshot(
        bundle_path, repository, "protected final-review bundle"
    )
    try:
        seed = read_external_bytes_snapshot(
            seed_file, label="review randomization seed"
        )
    except ReviewContractError as exc:
        raise FinalReviewError("cannot read review randomization seed") from exc
    if len(seed) < 32:
        raise FinalReviewError(
            "review randomization seed must contain at least 32 bytes"
        )
    target = _canonical_repository_target(
        repository,
        output,
        FINAL_REVIEW_FREEZE_PATH,
        "final review freeze",
    )
    main, calibration, sealed_ids = _private_cells_from_bundle(bundle, canonical)
    staging = _outside_repository(
        staging_directory, repository, "review packet staging directory"
    )
    identity = _outside_repository(
        external_identity_map, repository, "blind identity map"
    )
    if identity.parent == staging or staging in identity.parents:
        raise FinalReviewError("blind identity map must stay outside packet staging")
    try:
        manifest = build_review_packets(
            main,
            calibration,
            seed=seed,
            staging_directory=staging,
            external_identity_map=identity,
            sealed_task_ids=sealed_ids,
        )
        validate_public_packets(staging, manifest)
        _, identity_bytes = _read_external_snapshot(
            identity, repository, "blind identity map"
        )
        identity_sha = hashlib.sha256(identity_bytes).hexdigest()
        if identity_sha != manifest["identity_map_sha256"]:
            raise FinalReviewError("blind identity map differs after packet build")
        harden_external_review_tree(staging)
        harden_external_review_file(identity)
    except (ReviewContractError, OSError) as exc:
        raise FinalReviewError(
            "cannot build the protected final-review packets"
        ) from exc
    freeze: dict[str, Any] = {
        "schema_version": FINAL_REVIEW_FREEZE_SCHEMA,
        "status": "frozen_not_released",
        "repository_binding": canonical["repository_binding"],
        "lane_bindings": canonical["lanes"],
        "protected_bundle_sha256": hashlib.sha256(bundle_bytes).hexdigest(),
        "protected_bundle_artifact_sha256": bundle["artifact_sha256"],
        "randomization_seed_sha256": hashlib.sha256(seed).hexdigest(),
        "task_count": 160,
        "sealed_task_count": len(sealed_ids),
        "main_cell_count": len(main),
        "calibration_cell_count": len(calibration),
        "reviewers": list(REVIEWERS),
        "packet_manifest": manifest,
        "packet_manifest_sha256": manifest["manifest_sha256"],
        "identity_map_sha256": identity_sha,
        "statistical_analysis_plan": _final_statistical_analysis_plan(),
        "statistical_analysis_plan_sha256": sha256_json(
            _final_statistical_analysis_plan()
        ),
        "content_policy": {
            "protected_bundle": "external_only",
            "reviewer_packets": "external_only",
            "identity_map": "external_only",
            "repository_artifact": "content_free_commitment_only",
        },
    }
    freeze["artifact_sha256"] = sha256_json(freeze)
    validate_final_review_freeze(freeze)
    _write_repository_once(repository, target, freeze, "final review freeze")
    return freeze


def _load_current_freeze(root: Path, staging_directory: Path) -> dict[str, Any]:
    canonical = _canonical_inputs(root)
    freeze, _ = _read_repository_snapshot(
        root, FINAL_REVIEW_FREEZE_PATH, "final review freeze"
    )
    validate_final_review_freeze(freeze)
    if (
        freeze["repository_binding"] != canonical["repository_binding"]
        or freeze["lane_bindings"] != canonical["lanes"]
    ):
        raise FinalReviewError("final review freeze is stale")
    staging = _outside_repository(
        staging_directory, root, "review packet staging directory"
    )
    try:
        validate_public_packets(staging, freeze["packet_manifest"])
    except (ReviewContractError, OSError) as exc:
        raise FinalReviewError("staged final-review packets failed replay") from exc
    return freeze


def _derive_final_review_preflight(
    freeze: Mapping[str, Any], staging_directory: Path
) -> dict[str, Any]:
    records: dict[str, dict[str, Any]] = {}
    for reviewer in REVIEWERS[:2]:
        try:
            payloads = verified_reviewer_packet_payloads(
                staging_directory,
                freeze["packet_manifest"],
                reviewer,
            )
            records[reviewer] = verify_packet_token_preflight(
                freeze["packet_manifest"], reviewer, payloads
            )
        except ReviewContractError as exc:
            raise FinalReviewError(f"{reviewer} token preflight is invalid") from exc
        if records[reviewer]["packet_count"] != 85:
            raise FinalReviewError(f"{reviewer} preflight does not contain 85 packets")
    preflight: dict[str, Any] = {
        "schema_version": FINAL_REVIEW_PREFLIGHT_SCHEMA,
        "freeze_artifact_sha256": freeze["artifact_sha256"],
        "review_manifest_sha256": freeze["packet_manifest_sha256"],
        "token_verifier": {
            "verifier_id": PACKET_TOKEN_VERIFIER_ID,
            "verifier_sha256": PACKET_TOKEN_VERIFIER_SHA256,
            "profiles": {
                reviewer: PINNED_REVIEW_TOKEN_PROFILES[reviewer]
                for reviewer in REVIEWERS[:2]
            },
            "input_binding": "exact_manifest_packet_bytes",
        },
        "packet_count_per_ai": {
            reviewer: records[reviewer]["packet_count"] for reviewer in REVIEWERS[:2]
        },
        "token_total_per_ai": {
            reviewer: records[reviewer]["token_total"] for reviewer in REVIEWERS[:2]
        },
        "preflights": records,
        "confirmed_before_review": False,
    }
    preflight["artifact_sha256"] = sha256_json(preflight)
    return preflight


def build_final_review_preflight(
    root: Path | None,
    *,
    staging_directory: Path,
    output: Path | None = None,
) -> dict[str, Any]:
    """Derive exact counts from hash-bound packet bytes with the pinned verifier."""

    repository = (root or repository_root()).resolve()
    freeze = _load_current_freeze(repository, staging_directory)
    preflight = _derive_final_review_preflight(freeze, staging_directory)
    target = _canonical_repository_target(
        repository,
        output,
        FINAL_REVIEW_PREFLIGHT_PATH,
        "final review preflight",
    )
    _write_repository_once(repository, target, preflight, "final review preflight")
    return preflight


def confirm_final_review_preflight(
    root: Path | None,
    *,
    staging_directory: Path,
    confirmed_at: str,
    output: Path | None = None,
) -> dict[str, Any]:
    """Bind Kevin's confirmation to the exact packet and token totals."""

    repository = (root or repository_root()).resolve()
    freeze = _load_current_freeze(repository, staging_directory)
    preflight, _ = _read_repository_snapshot(
        repository, FINAL_REVIEW_PREFLIGHT_PATH, "final review preflight"
    )
    expected_preflight = _derive_final_review_preflight(freeze, staging_directory)
    if (
        preflight != expected_preflight
        or not isinstance(confirmed_at, str)
        or _UTC_SECOND.fullmatch(confirmed_at) is None
    ):
        raise FinalReviewError(
            "final review preflight is stale or confirmation time is invalid"
        )
    confirmed_records = {
        reviewer: {
            **expected_preflight["preflights"][reviewer],
            "confirmed_before_review": True,
            "confirmed_by": "Kevin Araujo",
        }
        for reviewer in REVIEWERS[:2]
    }
    confirmation: dict[str, Any] = {
        "schema_version": FINAL_REVIEW_CONFIRMATION_SCHEMA,
        "freeze_artifact_sha256": freeze["artifact_sha256"],
        "preflight_artifact_sha256": preflight["artifact_sha256"],
        "review_manifest_sha256": freeze["packet_manifest_sha256"],
        "reviewer": "Kevin Araujo",
        "reviewer_role": "sole_human_reviewer",
        "confirmed_at": confirmed_at,
        "confirmed_preflights": confirmed_records,
    }
    confirmation["artifact_sha256"] = sha256_json(confirmation)
    target = _canonical_repository_target(
        repository,
        output,
        FINAL_REVIEW_CONFIRMATION_PATH,
        "final review confirmation",
    )
    _write_repository_once(
        repository, target, confirmation, "final review confirmation"
    )
    return confirmation


def _load_confirmation(
    root: Path, freeze: Mapping[str, Any], staging_directory: Path
) -> dict[str, Any]:
    preflight, _ = _read_repository_snapshot(
        root, FINAL_REVIEW_PREFLIGHT_PATH, "final review preflight"
    )
    expected_preflight = _derive_final_review_preflight(freeze, staging_directory)
    confirmation, _ = _read_repository_snapshot(
        root, FINAL_REVIEW_CONFIRMATION_PATH, "final review confirmation"
    )
    if (
        preflight != expected_preflight
        or confirmation.get("schema_version") != FINAL_REVIEW_CONFIRMATION_SCHEMA
        or confirmation.get("artifact_sha256")
        != sha256_json(
            {
                key: item
                for key, item in confirmation.items()
                if key != "artifact_sha256"
            }
        )
        or confirmation.get("freeze_artifact_sha256") != freeze["artifact_sha256"]
        or confirmation.get("review_manifest_sha256")
        != freeze["packet_manifest_sha256"]
        or confirmation.get("preflight_artifact_sha256")
        != expected_preflight["artifact_sha256"]
        or confirmation.get("reviewer") != "Kevin Araujo"
        or confirmation.get("reviewer_role") != "sole_human_reviewer"
        or not isinstance(confirmation.get("confirmed_at"), str)
        or _UTC_SECOND.fullmatch(confirmation["confirmed_at"]) is None
        or set(confirmation.get("confirmed_preflights", {})) != set(REVIEWERS[:2])
    ):
        raise FinalReviewError("Kevin's token confirmation is missing or stale")
    for reviewer in REVIEWERS[:2]:
        expected = {
            **expected_preflight["preflights"][reviewer],
            "confirmed_before_review": True,
            "confirmed_by": "Kevin Araujo",
        }
        if confirmation["confirmed_preflights"][reviewer] != expected:
            raise FinalReviewError(
                f"{reviewer} confirmed token preflight differs from packet bytes"
            )
    return confirmation


def _replay_release(
    path: Path,
    *,
    root: Path,
    freeze: Mapping[str, Any],
    confirmation: Mapping[str, Any],
    phase: str,
) -> dict[str, Any]:
    release = _outside_repository(path, root, "final review release directory")
    try:
        return replay_review_release(
            release,
            freeze["packet_manifest"],
            confirmation["confirmed_preflights"],
            phase=phase,
        )
    except ReviewContractError as exc:
        raise FinalReviewError("saved final review release differs") from exc


def release_final_review(
    root: Path | None,
    *,
    staging_directory: Path,
    release_directory: Path,
    phase: str,
    calibration_record_path: Path | None = None,
) -> dict[str, Any]:
    """Release calibration first, then the main packets after calibration passes."""

    repository = (root or repository_root()).resolve()
    freeze = _load_current_freeze(repository, staging_directory)
    confirmation = _load_confirmation(repository, freeze, staging_directory)
    preflights = confirmation["confirmed_preflights"]
    calibration: dict[str, Any] | None = None
    if phase == "calibration":
        if calibration_record_path is not None:
            raise FinalReviewError("calibration release cannot accept a gate record")
    elif phase == "main":
        if calibration_record_path is None:
            raise FinalReviewError(
                "main release requires an approved calibration record"
            )
        calibration_path = _outside_repository(
            calibration_record_path, repository, "calibration gate record"
        )
        calibration, _ = _read_external_snapshot(
            calibration_path, repository, "calibration gate record"
        )
    else:
        raise FinalReviewError("release phase must be calibration or main")
    requested_release = _outside_repository(
        release_directory, repository, "final review release directory"
    )
    try:
        if phase == "calibration":
            released = release_calibration_packets(
                staging_directory,
                requested_release,
                freeze["packet_manifest"],
                preflights,
            )
        else:
            released = release_review_packets(
                staging_directory,
                requested_release,
                freeze["packet_manifest"],
                calibration or {},
                preflights,
            )
    except (ReviewContractError, OSError) as exc:
        raise FinalReviewError("final review packet release failed closed") from exc
    try:
        harden_external_review_tree(requested_release)
    except ReviewContractError as exc:
        raise FinalReviewError("cannot harden final review release") from exc
    return released


def _expected_phase_blind_ids(
    staging: Path,
    manifest: Mapping[str, Any],
    reviewer: str,
    *,
    phase: str,
) -> set[str]:
    if phase not in {"calibration", "review"}:
        raise FinalReviewError("review packet phase is unsupported")
    expected: set[str] = set()
    try:
        payloads = verified_reviewer_packet_payloads(
            staging, manifest, reviewer, phase=phase
        )
        for payload in payloads.values():
            packet = _decode_json(payload, "review packet")
            expected.update(str(cell["blind_cell_id"]) for cell in packet["cells"])
    except ReviewContractError as exc:
        raise FinalReviewError("review packet set failed exact-byte replay") from exc
    expected_count = 20 if phase == "calibration" else MAIN_RETURN_GRADE_COUNT
    if len(expected) != expected_count:
        raise FinalReviewError(
            f"reviewer {phase} packet set does not contain {expected_count:,} cells"
        )
    return expected


def _expected_review_blind_ids(
    staging: Path, manifest: Mapping[str, Any], reviewer: str
) -> set[str]:
    return _expected_phase_blind_ids(staging, manifest, reviewer, phase="review")


def validate_final_review_return(
    value: Mapping[str, Any],
    *,
    reviewer: str,
    review_manifest_sha256: str,
    release_manifest_sha256: str,
    expected_blind_ids: set[str],
) -> dict[str, dict[str, Any]]:
    body = {key: item for key, item in value.items() if key != "artifact_sha256"}
    if (
        set(value) != _RETURN_FIELDS
        or value.get("schema_version") != FINAL_REVIEW_RETURN_SCHEMA
        or value.get("reviewer") != reviewer
        or value.get("review_manifest_sha256") != review_manifest_sha256
        or value.get("release_manifest_sha256") != release_manifest_sha256
        or value.get("phase") != "review"
        or value.get("grade_count") != MAIN_RETURN_GRADE_COUNT
        or value.get("artifact_sha256") != sha256_json(body)
        or not isinstance(value.get("rubric_ambiguous"), bool)
        or not isinstance(value.get("review_comment"), str)
        or len(value["review_comment"]) > 4000
    ):
        raise FinalReviewError("completed final-review return is malformed or stale")
    rows = value.get("grades")
    if not isinstance(rows, list) or len(rows) != MAIN_RETURN_GRADE_COUNT:
        raise FinalReviewError("completed return does not contain 1,680 grades")
    grades: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != _GRADE_ROW_FIELDS:
            raise FinalReviewError("completed return grade fields differ")
        blind_id = str(row["blind_cell_id"])
        grade = row["grade"]
        if blind_id in grades or not isinstance(grade, dict):
            raise FinalReviewError("completed return repeats a blind cell")
        try:
            validate_grade(grade)
        except ReviewContractError as exc:
            raise FinalReviewError(
                "completed return contains an invalid grade"
            ) from exc
        grades[blind_id] = grade
    if set(grades) != expected_blind_ids:
        raise FinalReviewError("completed return is incomplete or contains extra cells")
    return grades


def validate_final_review_calibration_return(
    value: Mapping[str, Any],
    *,
    reviewer: str,
    review_manifest_sha256: str,
    release_manifest_sha256: str,
    expected_blind_ids: set[str],
) -> dict[str, dict[str, Any]]:
    body = {key: item for key, item in value.items() if key != "artifact_sha256"}
    if (
        set(value) != _RETURN_FIELDS
        or value.get("schema_version") != FINAL_REVIEW_CALIBRATION_RETURN_SCHEMA
        or value.get("reviewer") != reviewer
        or value.get("review_manifest_sha256") != review_manifest_sha256
        or value.get("release_manifest_sha256") != release_manifest_sha256
        or value.get("phase") != "calibration"
        or value.get("grade_count") != 20
        or value.get("artifact_sha256") != sha256_json(body)
        or not isinstance(value.get("rubric_ambiguous"), bool)
        or not isinstance(value.get("review_comment"), str)
        or len(value["review_comment"]) > 4000
    ):
        raise FinalReviewError("completed calibration return is malformed or stale")
    rows = value.get("grades")
    if not isinstance(rows, list) or len(rows) != 20:
        raise FinalReviewError(
            "completed calibration return does not contain 20 grades"
        )
    grades: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != _GRADE_ROW_FIELDS:
            raise FinalReviewError("completed calibration grade fields differ")
        blind_id = str(row["blind_cell_id"])
        grade = row["grade"]
        if blind_id in grades or not isinstance(grade, dict):
            raise FinalReviewError("completed calibration repeats a blind cell")
        try:
            validate_grade(grade)
        except ReviewContractError as exc:
            raise FinalReviewError(
                "completed calibration has an invalid grade"
            ) from exc
        grades[blind_id] = grade
    if set(grades) != expected_blind_ids:
        raise FinalReviewError("completed calibration is incomplete or has extra cells")
    return grades


def finalize_final_review_calibration(
    root: Path | None,
    *,
    staging_directory: Path,
    release_directory: Path,
    external_identity_map: Path,
    external_reference: Path,
    returns_by_reviewer: Mapping[str, Path],
    output: Path,
) -> dict[str, Any]:
    """Require the same complete 20-cell calibration from all three reviewers."""

    repository = (root or repository_root()).resolve()
    if set(returns_by_reviewer) != set(REVIEWERS):
        raise FinalReviewError("calibration requires GPT, Claude, and Kevin returns")
    freeze = _load_current_freeze(repository, staging_directory)
    confirmation = _load_confirmation(repository, freeze, staging_directory)
    released = _replay_release(
        release_directory,
        root=repository,
        freeze=freeze,
        confirmation=confirmation,
        phase="calibration",
    )
    staging = _outside_repository(
        staging_directory, repository, "review packet staging directory"
    )
    identity_path = _outside_repository(
        external_identity_map, repository, "blind identity map"
    )
    reference_path = _outside_repository(
        external_reference, repository, "calibration reference"
    )
    identity, identity_bytes = _read_external_snapshot(
        identity_path, repository, "blind identity map"
    )
    reference, reference_bytes = _read_external_snapshot(
        reference_path, repository, "calibration reference"
    )
    if hashlib.sha256(identity_bytes).hexdigest() != freeze["identity_map_sha256"]:
        raise FinalReviewError("calibration identity map differs from the freeze")
    del identity, reference
    grades_by_reviewer: dict[str, dict[str, dict[str, Any]]] = {}
    ambiguity: dict[str, bool] = {}
    for reviewer in REVIEWERS:
        returned = _outside_repository(
            returns_by_reviewer[reviewer], repository, f"{reviewer} calibration return"
        )
        response, _ = _read_external_snapshot(
            returned, repository, f"{reviewer} calibration return"
        )
        expected = _expected_phase_blind_ids(
            staging,
            freeze["packet_manifest"],
            reviewer,
            phase="calibration",
        )
        grades_by_reviewer[reviewer] = validate_final_review_calibration_return(
            response,
            reviewer=reviewer,
            review_manifest_sha256=freeze["packet_manifest_sha256"],
            release_manifest_sha256=released["manifest_sha256"],
            expected_blind_ids=expected,
        )
        if reviewer in REVIEWERS[:2]:
            try:
                validate_final_review_ai_manifest(
                    repository,
                    release_directory=release_directory,
                    reviewer=reviewer,
                    phase="calibration",
                    freeze=freeze,
                    confirmation=confirmation,
                    release=released,
                    return_path=returned,
                    return_value=response,
                )
            except FinalReviewAIError as exc:
                raise FinalReviewError(
                    f"{reviewer} calibration return has no valid native manifest"
                ) from exc
        ambiguity[reviewer] = bool(response["rubric_ambiguous"])
    try:
        calibration = evaluate_calibration(
            grades_by_reviewer,
            identity_map_path=identity_path,
            external_reference_path=reference_path,
            review_manifest=freeze["packet_manifest"],
            rubric_ambiguity_by_reviewer=ambiguity,
            identity_map_bytes=identity_bytes,
            reference_bytes=reference_bytes,
        )
    except ReviewContractError as exc:
        raise FinalReviewError("final review calibration failed closed") from exc
    target = _outside_repository(output, repository, "calibration gate record")
    try:
        write_external_bytes_once_or_verify(
            target, _json_bytes(calibration), label="calibration gate record"
        )
        harden_external_review_file(target)
    except ReviewContractError as exc:
        raise FinalReviewError("cannot publish calibration gate record") from exc
    return calibration


def import_final_review_return(
    root: Path | None,
    *,
    staging_directory: Path,
    release_directory: Path,
    reviewer: str,
    return_path: Path,
    store_path: Path,
    output: Path | None = None,
) -> dict[str, Any]:
    """Import one complete independent return into a new external review store."""

    repository = (root or repository_root()).resolve()
    if reviewer not in REVIEWERS:
        raise FinalReviewError("unknown final-review panel member")
    freeze = _load_current_freeze(repository, staging_directory)
    confirmation = _load_confirmation(repository, freeze, staging_directory)
    release = _replay_release(
        release_directory,
        root=repository,
        freeze=freeze,
        confirmation=confirmation,
        phase="review",
    )
    staging = _outside_repository(
        staging_directory, repository, "review packet staging directory"
    )
    returned = _outside_repository(return_path, repository, "completed review return")
    response, return_bytes = _read_external_snapshot(
        returned, repository, "completed review return"
    )
    expected = _expected_review_blind_ids(staging, freeze["packet_manifest"], reviewer)
    grades = validate_final_review_return(
        response,
        reviewer=reviewer,
        review_manifest_sha256=freeze["packet_manifest_sha256"],
        release_manifest_sha256=release["manifest_sha256"],
        expected_blind_ids=expected,
    )
    native_manifest: dict[str, Any] | None = None
    if reviewer in REVIEWERS[:2]:
        try:
            native_manifest = validate_final_review_ai_manifest(
                repository,
                release_directory=release_directory,
                reviewer=reviewer,
                phase="review",
                freeze=freeze,
                confirmation=confirmation,
                release=release,
                return_path=returned,
                return_value=response,
            )
        except FinalReviewAIError as exc:
            raise FinalReviewError(
                f"{reviewer} return has no valid native manifest"
            ) from exc
    store_target = _outside_repository(store_path, repository, "review grade store")
    target = _canonical_repository_target(
        repository,
        output,
        FINAL_REVIEW_IMPORT_DIRECTORY / f"{reviewer}.json",
        "final review import receipt",
    )
    try:
        assignments = verified_review_assignments(
            staging, freeze["packet_manifest"], reviewer
        )
        store_bytes = ReviewStore.build_snapshot(reviewer, assignments, grades)
        store = ReviewStore.from_snapshot(store_bytes)
        write_external_bytes_once_or_verify(
            store_target, store_bytes, label="review grade store"
        )
        harden_external_review_file(store_target)
    except ReviewContractError as exc:
        raise FinalReviewError("cannot import the completed review return") from exc
    if set(store.export_grades(reviewer)) != expected:
        raise FinalReviewError("review grade store is incomplete after import")
    receipt: dict[str, Any] = {
        "schema_version": FINAL_REVIEW_IMPORT_SCHEMA,
        "reviewer": reviewer,
        "freeze_artifact_sha256": freeze["artifact_sha256"],
        "review_manifest_sha256": freeze["packet_manifest_sha256"],
        "release_manifest_sha256": release["manifest_sha256"],
        "return_artifact_sha256": response["artifact_sha256"],
        "return_file_sha256": hashlib.sha256(return_bytes).hexdigest(),
        "store_sha256": hashlib.sha256(store_bytes).hexdigest(),
        "grade_count": len(grades),
        "rubric_ambiguous": response["rubric_ambiguous"],
        "review_comment_sha256": hashlib.sha256(
            response["review_comment"].encode("utf-8")
        ).hexdigest(),
        "native_ai_manifest_artifact_sha256": (
            None if native_manifest is None else native_manifest["artifact_sha256"]
        ),
        "native_ai_manifest_file_sha256": (
            None if native_manifest is None else native_manifest["file_sha256"]
        ),
    }
    receipt["artifact_sha256"] = sha256_json(receipt)
    _write_repository_once(repository, target, receipt, "final review import receipt")
    return receipt


def _pairwise_agreement(
    panel_cells: Sequence[Mapping[str, Any]], left: str, right: str
) -> dict[str, Any]:
    exact = 0
    within_one = 0
    accepted = 0
    absolute = 0
    for cell in panel_cells:
        grades = cell["individual_grades"]
        left_grade = grades[left]
        right_grade = grades[right]
        difference = abs(
            int(left_grade["overall_ordinal"]) - int(right_grade["overall_ordinal"])
        )
        exact += difference == 0
        within_one += difference <= 1
        accepted += left_grade["accepted"] == right_grade["accepted"]
        absolute += difference
    count = len(panel_cells)
    return {
        "cell_count": count,
        "exact_ordinal_rate": exact / count,
        "within_one_ordinal_rate": within_one / count,
        "accepted_match_rate": accepted / count,
        "mean_absolute_ordinal_difference": absolute / count,
    }


def _pairwise_threshold_pass(metrics: Mapping[str, Any]) -> bool:
    return bool(
        metrics["exact_ordinal_rate"] >= CALIBRATION_EXACT_ORDINAL_MIN
        and metrics["within_one_ordinal_rate"] >= CALIBRATION_WITHIN_ONE_MIN
        and metrics["accepted_match_rate"] >= CALIBRATION_ACCEPTED_MATCH_MIN
    )


def _ai_kevin_threshold_pass(metrics: Mapping[str, Any]) -> bool:
    return bool(
        metrics["accepted_match_rate"] >= AI_KEVIN_ACCEPTED_MATCH_MIN
        and metrics["mean_absolute_ordinal_difference"]
        <= AI_KEVIN_MEAN_ABSOLUTE_DIFFERENCE_MAX
    )


def _material_pairwise_difference(
    metrics: Mapping[str, Any], global_metrics: Mapping[str, Any]
) -> bool:
    return bool(
        metrics["exact_ordinal_rate"]
        < global_metrics["exact_ordinal_rate"] - MATERIAL_EXACT_RATE_DROP_MAX
        or metrics["within_one_ordinal_rate"]
        < global_metrics["within_one_ordinal_rate"] - MATERIAL_WITHIN_ONE_RATE_DROP_MAX
        or metrics["accepted_match_rate"]
        < global_metrics["accepted_match_rate"] - MATERIAL_ACCEPTED_RATE_DROP_MAX
        or metrics["mean_absolute_ordinal_difference"]
        > global_metrics["mean_absolute_ordinal_difference"]
        + MATERIAL_MEAN_ABSOLUTE_DIFFERENCE_INCREASE_MAX
    )


def _material_ai_kevin_difference(
    metrics: Mapping[str, Any], global_metrics: Mapping[str, Any]
) -> bool:
    return bool(
        metrics["accepted_match_rate"]
        < global_metrics["accepted_match_rate"] - MATERIAL_ACCEPTED_RATE_DROP_MAX
        or metrics["mean_absolute_ordinal_difference"]
        > global_metrics["mean_absolute_ordinal_difference"]
        + MATERIAL_MEAN_ABSOLUTE_DIFFERENCE_INCREASE_MAX
    )


def _stratified_agreement(
    cells: Sequence[Mapping[str, Any]],
    *,
    metadata: Mapping[str, tuple[str, str, str]],
    global_pairwise: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], bool]:
    dimensions: dict[str, tuple[str, ...]] = {
        "lane": tuple(STRATEGY_LANES),
        "reasoning_effort": tuple(REASONING_EFFORTS),
        "task_family": tuple(
            sorted({metadata[str(cell["canonical_cell_id"])][2] for cell in cells})
        ),
    }
    indexes = {"lane": 0, "reasoning_effort": 1, "task_family": 2}
    output: dict[str, dict[str, Any]] = {}
    all_required_pass = True
    pairs = (
        (REVIEWERS[0], REVIEWERS[1]),
        (REVIEWERS[0], REVIEWERS[2]),
        (REVIEWERS[1], REVIEWERS[2]),
    )
    for dimension, values in dimensions.items():
        dimension_rows: dict[str, Any] = {}
        index = indexes[dimension]
        for value in values:
            selected = [
                cell
                for cell in cells
                if metadata[str(cell["canonical_cell_id"])][index] == value
            ]
            required = len(selected) >= AGREEMENT_STRATUM_MIN_CELLS
            pairwise: dict[str, Any] = {}
            for left, right in pairs:
                key = f"{left}__{right}"
                metrics = _pairwise_agreement(selected, left, right)
                threshold_pass = _pairwise_threshold_pass(metrics)
                material = _material_pairwise_difference(metrics, global_pairwise[key])
                pairwise[key] = {
                    **metrics,
                    "threshold_pass": threshold_pass,
                    "materially_worse_than_global": material,
                    "gate_pass": not required or (threshold_pass and not material),
                }
            ai_vs_kevin: dict[str, Any] = {}
            for reviewer in REVIEWERS[:2]:
                key = f"{reviewer}__kevin"
                metrics = _pairwise_agreement(selected, reviewer, "kevin")
                threshold_pass = _ai_kevin_threshold_pass(metrics)
                material = _material_ai_kevin_difference(metrics, global_pairwise[key])
                ai_vs_kevin[reviewer] = {
                    **metrics,
                    "threshold_pass": threshold_pass,
                    "materially_worse_than_global": material,
                    "gate_pass": not required or (threshold_pass and not material),
                }
            gate_pass = all(
                row["gate_pass"] for row in (*pairwise.values(), *ai_vs_kevin.values())
            )
            dimension_rows[value] = {
                "cell_count": len(selected),
                "gate_required": required,
                "pairwise": pairwise,
                "ai_vs_kevin": ai_vs_kevin,
                "gate_pass": gate_pass,
            }
            if required:
                all_required_pass = all_required_pass and gate_pass
        output[dimension] = dimension_rows
    return output, all_required_pass


def _wilson_interval(successes: int, total: int) -> dict[str, float]:
    if total <= 0:
        raise FinalReviewError("Wilson interval requires observations")
    z = 1.959963984540054
    rate = successes / total
    denominator = 1 + z * z / total
    midpoint = (rate + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(rate * (1 - rate) / total + z * z / (4 * total * total))
        / denominator
    )
    return {"lower": midpoint - margin, "upper": midpoint + margin}


def _safe_grade(grade: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in grade.items() if key != "comment"} | {
        "comment_sha256": hashlib.sha256(
            str(grade.get("comment", "")).encode("utf-8")
        ).hexdigest()
    }


def _numeric_distribution(values: Sequence[float]) -> dict[str, Any]:
    """Summarize numeric task scores without publishing task identities."""

    try:
        summary = distribution_summary(values)
    except StatisticsError as exc:
        raise FinalReviewError("cannot summarize final-review task scores") from exc
    counts = Counter(values)
    return {
        **summary,
        "value_counts": [
            {"value": value, "count": counts[value]} for value in sorted(counts)
        ],
    }


def _statistical_task_matrix(
    cells: Sequence[Mapping[str, Any]],
) -> tuple[
    dict[str, dict[str, dict[str, dict[str, float]]]],
    dict[str, str],
]:
    """Validate the 160 x 5 x 2 pairing and extract content-free scores."""

    scores: dict[str, dict[str, dict[str, dict[str, float]]]] = {
        metric: {
            lane: {effort: {} for effort in REASONING_EFFORTS}
            for lane in STRATEGY_LANES
        }
        for metric in (FINAL_REVIEW_PRIMARY_METRIC, FINAL_REVIEW_SECONDARY_METRIC)
    }
    families: dict[str, str] = {}
    seen_cells: set[str] = set()
    seen_combinations: set[tuple[str, str, str]] = set()
    for cell in cells:
        canonical_id = cell.get("canonical_cell_id")
        task_id = cell.get("task_id")
        lane = cell.get("lane_id")
        effort = cell.get("reasoning_effort")
        family = cell.get("task_family")
        aggregate = cell.get("aggregate")
        if (
            not isinstance(canonical_id, str)
            or not canonical_id
            or canonical_id in seen_cells
            or not isinstance(task_id, str)
            or not task_id
            or lane not in STRATEGY_LANES
            or effort not in REASONING_EFFORTS
            or not isinstance(family, str)
            or not family
            or not isinstance(aggregate, Mapping)
        ):
            raise FinalReviewError("final panel statistical identity is invalid")
        combination = (task_id, str(lane), str(effort))
        if combination in seen_combinations:
            raise FinalReviewError("final panel repeats a task, lane, and effort")
        accepted = aggregate.get("accepted")
        ordinal = aggregate.get("overall_ordinal")
        if (
            not isinstance(accepted, bool)
            or isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or not 0 <= ordinal <= 3
        ):
            raise FinalReviewError("final panel aggregate score is invalid")
        if task_id in families and families[task_id] != family:
            raise FinalReviewError("a task changes family across final cells")
        families[task_id] = family
        seen_cells.add(canonical_id)
        seen_combinations.add(combination)
        scores[FINAL_REVIEW_PRIMARY_METRIC][str(lane)][str(effort)][task_id] = float(
            accepted
        )
        scores[FINAL_REVIEW_SECONDARY_METRIC][str(lane)][str(effort)][task_id] = float(
            ordinal
        )
    expected = {
        (task_id, lane, effort)
        for task_id in families
        for lane in STRATEGY_LANES
        for effort in REASONING_EFFORTS
    }
    if (
        len(cells) != MAIN_CELL_COUNT
        or len(families) != 160
        or seen_combinations != expected
    ):
        raise FinalReviewError("final panel does not cover 160 x 5 x 2 paired cells")
    return scores, families


def _mean_across_efforts(
    scores: Mapping[str, Mapping[str, float]],
) -> dict[str, float]:
    low = scores["low"]
    high = scores["high"]
    if set(low) != set(high):
        raise FinalReviewError("low and high task identities differ")
    return {
        task_id: math.fsum((low[task_id], high[task_id])) / 2.0
        for task_id in sorted(low)
    }


def _mean_across_lanes(
    scores: Mapping[str, Mapping[str, Mapping[str, float]]], effort: str
) -> dict[str, float]:
    task_ids = set(scores[STRATEGY_LANES[0]][effort])
    if any(set(scores[lane][effort]) != task_ids for lane in STRATEGY_LANES[1:]):
        raise FinalReviewError("lane task identities differ")
    return {
        task_id: math.fsum(scores[lane][effort][task_id] for lane in STRATEGY_LANES)
        / len(STRATEGY_LANES)
        for task_id in sorted(task_ids)
    }


def _paired_primary_analysis(
    baseline: Mapping[str, float],
    candidate: Mapping[str, float],
    families: Mapping[str, str],
) -> dict[str, Any]:
    try:
        return {
            "paired_bootstrap_95": paired_bootstrap_ci(
                baseline,
                candidate,
                seed=FINAL_REVIEW_BOOTSTRAP_SEED,
                resamples=FINAL_REVIEW_BOOTSTRAP_RESAMPLES,
            ),
            "task_family_effect_sizes": task_family_effect_summaries(
                baseline, candidate, families
            ),
        }
    except StatisticsError as exc:
        raise FinalReviewError("cannot compute paired final-review statistics") from exc


def _build_statistical_analysis(
    cells: Sequence[Mapping[str, Any]], *, analysis_plan_sha256: str
) -> dict[str, Any]:
    scores, families = _statistical_task_matrix(cells)
    primary = scores[FINAL_REVIEW_PRIMARY_METRIC]
    inference_cache: dict[
        tuple[tuple[float, ...], tuple[float, ...]], dict[str, Any]
    ] = {}

    def infer(
        baseline: Mapping[str, float], candidate: Mapping[str, float]
    ) -> dict[str, Any]:
        task_ids = sorted(families)
        key = (
            tuple(baseline[task_id] for task_id in task_ids),
            tuple(candidate[task_id] for task_id in task_ids),
        )
        if key not in inference_cache:
            inference_cache[key] = _paired_primary_analysis(
                baseline, candidate, families
            )
        return inference_cache[key]

    lane_effort_summaries: dict[str, Any] = {}
    lane_task_distributions: dict[str, Any] = {}
    collapsed_primary: dict[str, dict[str, float]] = {}
    for lane in STRATEGY_LANES:
        lane_effort_summaries[lane] = {}
        for effort in REASONING_EFFORTS:
            lane_effort_summaries[lane][effort] = {
                metric: _numeric_distribution(
                    list(scores[metric][lane][effort].values())
                )
                for metric in (
                    FINAL_REVIEW_PRIMARY_METRIC,
                    FINAL_REVIEW_SECONDARY_METRIC,
                )
            }
        collapsed_primary[lane] = _mean_across_efforts(primary[lane])
        lane_task_distributions[lane] = {
            metric: _numeric_distribution(
                list(_mean_across_efforts(scores[metric][lane]).values())
            )
            for metric in (
                FINAL_REVIEW_PRIMARY_METRIC,
                FINAL_REVIEW_SECONDARY_METRIC,
            )
        }

    paired_lane_comparisons: dict[str, Any] = {}
    for baseline_lane, candidate_lane in combinations(STRATEGY_LANES, 2):
        comparison_id = f"{candidate_lane}__minus__{baseline_lane}"
        paired_lane_comparisons[comparison_id] = {
            "baseline_lane": baseline_lane,
            "candidate_lane": candidate_lane,
            "contrast": "candidate_minus_baseline",
            **infer(
                collapsed_primary[baseline_lane], collapsed_primary[candidate_lane]
            ),
        }

    by_lane: dict[str, Any] = {}
    effort_slopes: dict[str, dict[str, float]] = {}
    for lane in STRATEGY_LANES:
        low = primary[lane]["low"]
        high = primary[lane]["high"]
        by_lane[lane] = {"contrast": "high_minus_low", **infer(low, high)}
        effort_slopes[lane] = {
            task_id: high[task_id] - low[task_id] for task_id in sorted(families)
        }

    main_low = _mean_across_lanes(primary, "low")
    main_high = _mean_across_lanes(primary, "high")
    interaction_reference = STRATEGY_LANES[0]
    interactions: dict[str, Any] = {}
    for lane in STRATEGY_LANES[1:]:
        interactions[lane] = {
            "reference_lane": interaction_reference,
            "contrast": ("lane_high_minus_low_minus_reference_high_minus_low"),
            **infer(effort_slopes[interaction_reference], effort_slopes[lane]),
        }

    analysis: dict[str, Any] = {
        "schema_version": FINAL_REVIEW_STATISTICAL_ANALYSIS_SCHEMA,
        "analysis_plan_sha256": analysis_plan_sha256,
        "primary_metric": FINAL_REVIEW_PRIMARY_METRIC,
        "secondary_metric": FINAL_REVIEW_SECONDARY_METRIC,
        "task_count": len(families),
        "lane_effort_summaries": lane_effort_summaries,
        "lane_task_distributions": lane_task_distributions,
        "paired_lane_comparisons": paired_lane_comparisons,
        "reasoning_effort_analysis": {
            "main_effect_high_minus_low": {
                "contrast": "mean_across_lanes_high_minus_low",
                **infer(main_low, main_high),
            },
            "by_lane_high_minus_low": by_lane,
            "strategy_by_effort_interactions": interactions,
        },
    }
    analysis["artifact_sha256"] = sha256_json(analysis)
    return analysis


def build_final_review_report(
    panel: Mapping[str, Any],
    *,
    identities: Sequence[Mapping[str, Any]],
    grades_by_reviewer: Mapping[str, Mapping[str, Any]],
    freeze: Mapping[str, Any],
    import_receipts: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the content-free final agreement report and gate ranking claims."""

    expected_plan = _final_statistical_analysis_plan()
    analysis_plan_sha256 = sha256_json(expected_plan)
    if (
        freeze.get("statistical_analysis_plan") != expected_plan
        or freeze.get("statistical_analysis_plan_sha256") != analysis_plan_sha256
    ):
        raise FinalReviewError("final report has no frozen statistical plan")
    cells = panel.get("cells")
    if not isinstance(cells, list) or len(cells) != MAIN_CELL_COUNT:
        raise FinalReviewError("panel aggregate does not contain 1,600 cells")
    identity_by_reviewer_cell: dict[tuple[str, str], Mapping[str, Any]] = {}
    for identity in identities:
        if identity.get("phase") == "main":
            key = (str(identity["reviewer"]), str(identity["canonical_cell_id"]))
            identity_by_reviewer_cell[key] = identity
    metadata: dict[str, tuple[str, str, str]] = {}
    for cell in cells:
        canonical_id = str(cell["canonical_cell_id"])
        values = {
            (
                str(identity_by_reviewer_cell[(reviewer, canonical_id)]["strategy_id"]),
                str(
                    identity_by_reviewer_cell[(reviewer, canonical_id)][
                        "reasoning_effort"
                    ]
                ),
                str(identity_by_reviewer_cell[(reviewer, canonical_id)]["task_family"]),
            )
            for reviewer in REVIEWERS
        }
        if len(values) != 1:
            raise FinalReviewError(
                "review identities disagree on lane, effort, or task family"
            )
        metadata_row = values.pop()
        if str(cell.get("task_family")) != metadata_row[2]:
            raise FinalReviewError("panel task family differs from blind identity map")
        metadata[canonical_id] = metadata_row

    pairwise: dict[str, dict[str, Any]] = {}
    for left, right in (
        (REVIEWERS[0], REVIEWERS[1]),
        (REVIEWERS[0], REVIEWERS[2]),
        (REVIEWERS[1], REVIEWERS[2]),
    ):
        pairwise[f"{left}__{right}"] = _pairwise_agreement(cells, left, right)
    ai_vs_kevin = {
        reviewer: pairwise[f"{reviewer}__kevin"] for reviewer in REVIEWERS[:2]
    }
    hidden: dict[str, dict[str, Any]] = {}
    identity_payload = {"identities": list(identities)}
    for reviewer in REVIEWERS:
        consistency = hidden_repeat_consistency(
            reviewer,
            dict(grades_by_reviewer[reviewer]),
            identity_payload,
        )
        completed = int(consistency["completed_pairs"])
        hidden[reviewer] = {
            **consistency,
            "exact_ordinal_rate": (
                None
                if completed == 0
                else consistency["exact_ordinal_matches"] / completed
            ),
            "accepted_match_rate": (
                None if completed == 0 else consistency["accepted_matches"] / completed
            ),
        }

    pairwise_pass = all(
        _pairwise_threshold_pass(metrics) for metrics in pairwise.values()
    )
    ai_kevin_pass = all(
        _ai_kevin_threshold_pass(metrics) for metrics in ai_vs_kevin.values()
    )
    stratified, stratified_pass = _stratified_agreement(
        cells, metadata=metadata, global_pairwise=pairwise
    )
    repeats_pass = all(
        metrics["completed_pairs"] == HIDDEN_REPEAT_COUNT
        and metrics["exact_ordinal_rate"] is not None
        and metrics["exact_ordinal_rate"] >= CALIBRATION_EXACT_ORDINAL_MIN
        and metrics["accepted_match_rate"] is not None
        and metrics["accepted_match_rate"] >= AI_KEVIN_ACCEPTED_MATCH_MIN
        for metrics in hidden.values()
    )
    no_ambiguity = all(
        receipt.get("rubric_ambiguous") is False for receipt in import_receipts.values()
    )
    ranking_allowed = (
        pairwise_pass
        and ai_kevin_pass
        and stratified_pass
        and repeats_pass
        and no_ambiguity
    )

    lane_accumulator: dict[str, dict[str, Any]] = {
        lane: {"count": 0, "accepted": 0, "ordinal_sum": 0} for lane in STRATEGY_LANES
    }
    safe_cells: list[dict[str, Any]] = []
    for cell in cells:
        canonical_id = str(cell["canonical_cell_id"])
        lane, effort, task_family = metadata[canonical_id]
        aggregate = cell["aggregate"]
        accumulator = lane_accumulator[lane]
        accumulator["count"] += 1
        accumulator["accepted"] += bool(aggregate["accepted"])
        accumulator["ordinal_sum"] += int(aggregate["overall_ordinal"])
        safe_cells.append(
            {
                "canonical_cell_id": canonical_id,
                "task_id": cell["task_id"],
                "lane_id": lane,
                "reasoning_effort": effort,
                "task_family": task_family,
                "candidate_sha256": cell["candidate_sha256"],
                "individual_grades": {
                    reviewer: _safe_grade(cell["individual_grades"][reviewer])
                    for reviewer in REVIEWERS
                },
                "aggregate": aggregate,
            }
        )
    lane_summaries: dict[str, dict[str, Any]] = {}
    for lane, values in lane_accumulator.items():
        count = int(values["count"])
        accepted = int(values["accepted"])
        if count != 320:
            raise FinalReviewError(f"{lane} does not contain 160 x 2 cells")
        lane_summaries[lane] = {
            "cell_count": count,
            "accepted_count": accepted,
            "accepted_rate": accepted / count,
            "accepted_rate_wilson_95": _wilson_interval(accepted, count),
            "mean_panel_ordinal": int(values["ordinal_sum"]) / count,
            "lane_binding": freeze["lane_bindings"][lane],
        }
    ranking = (
        sorted(
            STRATEGY_LANES,
            key=lambda lane: (
                -lane_summaries[lane]["accepted_rate"],
                -lane_summaries[lane]["mean_panel_ordinal"],
                lane,
            ),
        )
        if ranking_allowed
        else []
    )
    statistical_analysis = _build_statistical_analysis(
        safe_cells, analysis_plan_sha256=analysis_plan_sha256
    )
    report: dict[str, Any] = {
        "schema_version": FINAL_REVIEW_REPORT_SCHEMA,
        "freeze_artifact_sha256": freeze["artifact_sha256"],
        "review_manifest_sha256": freeze["packet_manifest_sha256"],
        "reviewers": list(REVIEWERS),
        "cell_count": MAIN_CELL_COUNT,
        "statistical_analysis_plan_sha256": analysis_plan_sha256,
        "statistical_analysis": statistical_analysis,
        "kevin_grade_required": True,
        "one_human_reviewer_limitation": (
            "The panel contains one human reviewer (Kevin) and two AI reviewers; "
            "it does not estimate agreement between independent human reviewers."
        ),
        "agreement_thresholds": {
            "pairwise_exact_ordinal_rate_min": CALIBRATION_EXACT_ORDINAL_MIN,
            "pairwise_within_one_ordinal_rate_min": CALIBRATION_WITHIN_ONE_MIN,
            "pairwise_accepted_match_rate_min": CALIBRATION_ACCEPTED_MATCH_MIN,
            "ai_kevin_accepted_match_rate_min": AI_KEVIN_ACCEPTED_MATCH_MIN,
            "ai_kevin_mean_absolute_ordinal_difference_max": (
                AI_KEVIN_MEAN_ABSOLUTE_DIFFERENCE_MAX
            ),
            "hidden_repeat_exact_ordinal_rate_min": CALIBRATION_EXACT_ORDINAL_MIN,
            "hidden_repeat_accepted_match_rate_min": AI_KEVIN_ACCEPTED_MATCH_MIN,
            "required_stratum_min_cells": AGREEMENT_STRATUM_MIN_CELLS,
            "material_exact_rate_drop_max": MATERIAL_EXACT_RATE_DROP_MAX,
            "material_within_one_rate_drop_max": MATERIAL_WITHIN_ONE_RATE_DROP_MAX,
            "material_accepted_rate_drop_max": MATERIAL_ACCEPTED_RATE_DROP_MAX,
            "material_mean_absolute_difference_increase_max": (
                MATERIAL_MEAN_ABSOLUTE_DIFFERENCE_INCREASE_MAX
            ),
        },
        "inter_reviewer_agreement": pairwise,
        "stratified_agreement": stratified,
        "hidden_repeat_consistency": hidden,
        "ai_vs_kevin": ai_vs_kevin,
        "rubric_ambiguity_by_reviewer": {
            reviewer: bool(import_receipts[reviewer]["rubric_ambiguous"])
            for reviewer in REVIEWERS
        },
        "agreement_gate": {
            "pairwise_pass": pairwise_pass,
            "ai_vs_kevin_pass": ai_kevin_pass,
            "all_required_strata_pass": stratified_pass,
            "hidden_repeats_pass": repeats_pass,
            "no_rubric_ambiguity": no_ambiguity,
            "ranking_claims_allowed": ranking_allowed,
            "suppression_reason": (
                None
                if ranking_allowed
                else "Global or required-stratum agreement requirements failed; report lanes descriptively without a ranking claim."
            ),
        },
        "lane_summaries": lane_summaries,
        "ranking": ranking,
        "panel_cells": safe_cells,
        "individual_grade_store_sha256s": {
            reviewer: import_receipts[reviewer]["store_sha256"]
            for reviewer in REVIEWERS
        },
        "import_receipt_sha256s": {
            reviewer: import_receipts[reviewer]["artifact_sha256"]
            for reviewer in REVIEWERS
        },
    }
    report["artifact_sha256"] = sha256_json(report)
    validate_final_review_report(report, freeze=freeze)
    return report


def validate_final_review_report(
    value: Mapping[str, Any],
    *,
    freeze: Mapping[str, Any],
    replay_statistics: bool = False,
) -> None:
    """Validate report hashes and optionally replay all deterministic statistics."""

    expected_fields = {
        "schema_version",
        "freeze_artifact_sha256",
        "review_manifest_sha256",
        "reviewers",
        "cell_count",
        "statistical_analysis_plan_sha256",
        "statistical_analysis",
        "kevin_grade_required",
        "one_human_reviewer_limitation",
        "agreement_thresholds",
        "inter_reviewer_agreement",
        "stratified_agreement",
        "hidden_repeat_consistency",
        "ai_vs_kevin",
        "rubric_ambiguity_by_reviewer",
        "agreement_gate",
        "lane_summaries",
        "ranking",
        "panel_cells",
        "individual_grade_store_sha256s",
        "import_receipt_sha256s",
        "artifact_sha256",
    }
    body = {key: item for key, item in value.items() if key != "artifact_sha256"}
    expected_plan = _final_statistical_analysis_plan()
    plan_sha256 = sha256_json(expected_plan)
    cells = value.get("panel_cells")
    analysis = value.get("statistical_analysis")
    if (
        set(value) != expected_fields
        or value.get("schema_version") != FINAL_REVIEW_REPORT_SCHEMA
        or value.get("artifact_sha256") != sha256_json(body)
        or value.get("freeze_artifact_sha256") != freeze.get("artifact_sha256")
        or value.get("review_manifest_sha256") != freeze.get("packet_manifest_sha256")
        or freeze.get("statistical_analysis_plan") != expected_plan
        or freeze.get("statistical_analysis_plan_sha256") != plan_sha256
        or value.get("statistical_analysis_plan_sha256") != plan_sha256
        or value.get("reviewers") != list(REVIEWERS)
        or value.get("cell_count") != MAIN_CELL_COUNT
        or value.get("kevin_grade_required") is not True
        or not isinstance(cells, list)
        or len(cells) != MAIN_CELL_COUNT
        or not isinstance(analysis, Mapping)
        or analysis.get("schema_version") != FINAL_REVIEW_STATISTICAL_ANALYSIS_SCHEMA
        or analysis.get("analysis_plan_sha256") != plan_sha256
        or analysis.get("primary_metric") != FINAL_REVIEW_PRIMARY_METRIC
        or analysis.get("secondary_metric") != FINAL_REVIEW_SECONDARY_METRIC
        or analysis.get("task_count") != 160
        or analysis.get("artifact_sha256")
        != sha256_json(
            {key: item for key, item in analysis.items() if key != "artifact_sha256"}
        )
    ):
        raise FinalReviewError("final review report is stale or malformed")
    if replay_statistics:
        expected_analysis = _build_statistical_analysis(
            cells, analysis_plan_sha256=plan_sha256
        )
        if analysis != expected_analysis:
            raise FinalReviewError(
                "final review statistical analysis differs on replay"
            )


def finalize_final_review(
    root: Path | None,
    *,
    staging_directory: Path,
    external_identity_map: Path,
    stores_by_reviewer: Mapping[str, Path],
    output: Path | None = None,
) -> dict[str, Any]:
    """Require all three complete returns, then publish a content-free report."""

    repository = (root or repository_root()).resolve()
    if set(stores_by_reviewer) != set(REVIEWERS):
        raise FinalReviewError("finalization requires GPT, Claude, and Kevin stores")
    freeze = _load_current_freeze(repository, staging_directory)
    identity_path = _outside_repository(
        external_identity_map, repository, "blind identity map"
    )
    identity, identity_bytes = _read_external_snapshot(
        identity_path, repository, "blind identity map"
    )
    if hashlib.sha256(identity_bytes).hexdigest() != freeze["identity_map_sha256"]:
        raise FinalReviewError("blind identity map differs from the frozen commitment")
    identities = identity.get("identities")
    if not isinstance(identities, list):
        raise FinalReviewError("blind identity map has no identities")
    stores: dict[str, ReviewStore] = {}
    receipts: dict[str, dict[str, Any]] = {}
    grades: dict[str, dict[str, Any]] = {}
    staging = _outside_repository(
        staging_directory, repository, "review packet staging directory"
    )
    for reviewer in REVIEWERS:
        receipt, _ = _read_repository_snapshot(
            repository,
            FINAL_REVIEW_IMPORT_DIRECTORY / f"{reviewer}.json",
            f"{reviewer} import receipt",
        )
        if (
            receipt.get("schema_version") != FINAL_REVIEW_IMPORT_SCHEMA
            or receipt.get("reviewer") != reviewer
            or receipt.get("artifact_sha256")
            != sha256_json(
                {key: item for key, item in receipt.items() if key != "artifact_sha256"}
            )
            or receipt.get("freeze_artifact_sha256") != freeze["artifact_sha256"]
            or receipt.get("review_manifest_sha256") != freeze["packet_manifest_sha256"]
            or receipt.get("grade_count") != MAIN_RETURN_GRADE_COUNT
            or (
                reviewer in REVIEWERS[:2]
                and (
                    _SHA256.fullmatch(
                        str(receipt.get("native_ai_manifest_artifact_sha256"))
                    )
                    is None
                    or _SHA256.fullmatch(
                        str(receipt.get("native_ai_manifest_file_sha256"))
                    )
                    is None
                )
            )
            or (
                reviewer == "kevin"
                and (
                    receipt.get("native_ai_manifest_artifact_sha256") is not None
                    or receipt.get("native_ai_manifest_file_sha256") is not None
                )
            )
        ):
            raise FinalReviewError(f"{reviewer} import receipt is stale or invalid")
        store_path = _outside_repository(
            stores_by_reviewer[reviewer], repository, f"{reviewer} grade store"
        )
        try:
            store_bytes = read_external_bytes_snapshot(
                store_path, label=f"{reviewer} grade store"
            )
        except ReviewContractError as exc:
            raise FinalReviewError(f"cannot read {reviewer} grade store") from exc
        if hashlib.sha256(store_bytes).hexdigest() != receipt["store_sha256"]:
            raise FinalReviewError(f"{reviewer} grade store was modified")
        try:
            store = ReviewStore.from_snapshot(store_bytes)
        except ReviewContractError as exc:
            raise FinalReviewError(f"{reviewer} grade store is invalid") from exc
        exported = store.export_grades(reviewer)
        expected_ids = _expected_review_blind_ids(
            staging, freeze["packet_manifest"], reviewer
        )
        if set(exported) != expected_ids:
            raise FinalReviewError(f"{reviewer} return is incomplete")
        stores[reviewer] = store
        receipts[reviewer] = receipt
        grades[reviewer] = exported
    if len({receipt["release_manifest_sha256"] for receipt in receipts.values()}) != 1:
        raise FinalReviewError("reviewer imports do not share one main release")
    try:
        panel = aggregate_completed_panel(
            stores,
            identity_map_path=identity_path,
            review_manifest=freeze["packet_manifest"],
            identity_map_bytes=identity_bytes,
        )
    except ReviewContractError as exc:
        raise FinalReviewError("three-member panel aggregation failed") from exc
    report = build_final_review_report(
        panel,
        identities=identities,
        grades_by_reviewer=grades,
        freeze=freeze,
        import_receipts=receipts,
    )
    target = _safe_repository_target(
        repository, output or FINAL_REVIEW_REPORT_PATH, "final review report"
    )
    _write_repository_once(repository, target, report, "final review report")
    return report
