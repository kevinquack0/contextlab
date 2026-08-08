"""Command-line entry point for ContextLab v2."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .baseline import (
    BaselineError,
    build_manifest,
    default_manifest_path,
    repository_root,
    snapshot_file_counts,
    verify_manifest,
    write_manifest,
)
from .boundaries import ProtectedDataError
from .contracts import build_contract_artifacts
from .costs import CostLedger, canonical_ledger_path
from .immutable_io import ImmutableIOError, write_json_once_or_verify
from .tasking import sha256_json
from .truth_audit import audit_truth_language


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ContextLab v2 research harness")
    parser.add_argument("--root", type=Path, default=repository_root())
    subparsers = parser.add_subparsers(dest="command", required=True)
    freeze = subparsers.add_parser(
        "freeze-v1", help="write the deterministic v1 manifest"
    )
    freeze.add_argument("--output", type=Path)
    verify = subparsers.add_parser("verify-v1", help="verify the saved v1 manifest")
    verify.add_argument("--manifest", type=Path)
    subparsers.add_parser("audit-truth", help="check v1 review language")
    subparsers.add_parser("gate-g0", help="run all G0 acceptance checks")
    approve_g0 = subparsers.add_parser(
        "approve-g0", help="bind Kevin's approval to the exact immutable G0 evidence"
    )
    approve_g0.add_argument("--technical-evidence-sha256", required=True)
    approve_g0.add_argument("--approved-at", required=True)
    subparsers.add_parser(
        "build-g1", help="build deterministic G1 foundation artifacts"
    )
    gate_g1 = subparsers.add_parser("gate-g1", help="run all G1 acceptance checks")
    gate_g1.add_argument("--output", type=Path)
    approve_g1 = subparsers.add_parser(
        "approve-g1", help="bind Kevin's approval to exact immutable G1 evidence"
    )
    approve_g1.add_argument("--technical-evidence-sha256", required=True)
    approve_g1.add_argument("--approved-at", required=True)
    freeze_g2 = subparsers.add_parser(
        "freeze-g2-static", help="freeze the complete 120-task G2 static benchmark"
    )
    freeze_g2.add_argument("--sealed-bundle-sha256", required=True)
    freeze_g2.add_argument("--output", type=Path)
    embed_g2 = subparsers.add_parser(
        "embed-g2-queries",
        help="embed missing public G2 queries through the paid gateway",
    )
    embed_g2.add_argument("--run-id", default="g2-public-query-embeddings-v1")
    embed_g2.add_argument("--output", type=Path)
    component_g2 = subparsers.add_parser(
        "run-g2-components", help="run the deterministic public R0-R7 component lab"
    )
    component_g2.add_argument("--output", type=Path)
    report_g2 = subparsers.add_parser(
        "report-g2-components",
        help="analyze the saved G2 component lab and build its viewer",
    )
    report_g2.add_argument("--lab", type=Path)
    generate_g2 = subparsers.add_parser(
        "run-g2-generations", help="run or resume fixed DeepSeek answers for G2 traces"
    )
    generate_g2.add_argument("--lab", type=Path)
    generate_g2.add_argument(
        "--strategies", default=",".join(f"R{n}" for n in range(8))
    )
    generate_g2.add_argument("--efforts", default="low,high")
    generate_g2.add_argument("--task-ids")
    generate_g2.add_argument("--trial", type=int, default=1)
    generate_g2.add_argument("--max-new-calls", type=int)
    generate_g2.add_argument("--concurrency", type=int, default=4)
    score_g2 = subparsers.add_parser(
        "score-g2-generations",
        help="run deterministic screening on saved public G2 answers",
    )
    score_g2.add_argument("--manifest", type=Path)
    score_g2.add_argument("--lab", type=Path)
    score_g2.add_argument("--output", type=Path)
    repeat_g2 = subparsers.add_parser(
        "run-g2-repeats",
        help="run or resume trials 2-5 for the frozen public repeat sample",
    )
    repeat_g2.add_argument("--lab", type=Path)
    repeat_g2.add_argument("--trial-one", type=Path)
    repeat_g2.add_argument("--max-new-calls-per-trial", type=int)
    repeat_g2.add_argument("--concurrency", type=int, default=4)
    score_repeats_g2 = subparsers.add_parser(
        "score-g2-repeats",
        help="analyze all five frozen public repeat trials",
    )
    score_repeats_g2.add_argument("--lab", type=Path)
    score_repeats_g2.add_argument("--output", type=Path)
    sealed_g2 = subparsers.add_parser(
        "g2-sealed-import", help="import a strict external G2 sealed-evaluation return"
    )
    sealed_g2.add_argument("--input", type=Path, required=True)
    sealed_g2.add_argument("--output", type=Path)
    gate_g2 = subparsers.add_parser(
        "gate-g2", help="run the canonical content-free G2 final gate"
    )
    gate_g2.add_argument("--output", type=Path)
    gate_g2.add_argument("--approval", type=Path)
    approve_g2 = subparsers.add_parser(
        "approve-g2", help="write Kevin's explicit approval for a pending G2 gate"
    )
    approve_g2.add_argument("--gate", type=Path)
    approve_g2.add_argument("--output", type=Path)
    approve_g2.add_argument("--approved-at", required=True)
    external_g2 = subparsers.add_parser(
        "g2-external-evaluate",
        help="run G2 against a protected bundle in an external work directory",
    )
    external_g2.add_argument("--bundle", type=Path, required=True)
    external_g2.add_argument("--work-root", type=Path, required=True)
    external_g2.add_argument("--return-path", type=Path)
    external_g2.add_argument("--max-new-calls", type=int)
    external_g2.add_argument("--concurrency", type=int, default=4)
    subparsers.add_parser(
        "verify-g3-protocol", help="verify the frozen G3 memory protocol"
    )
    embed_g3 = subparsers.add_parser(
        "embed-g3-temporal",
        help="embed the public temporal questions and events after approved G2",
    )
    embed_g3.add_argument("--run-id", default="g3-temporal-embeddings-v1")
    embed_g3.add_argument("--output", type=Path)
    build_g3 = subparsers.add_parser(
        "build-g3-temporal-r0",
        help="build the provider-free public temporal R0 evidence lab",
    )
    build_g3.add_argument("--embedding-cache", type=Path)
    build_g3.add_argument("--output", type=Path)
    prior_g3 = subparsers.add_parser(
        "run-g3-prior",
        help="run one objectively graded public M3 cell to seed canonical M4",
    )
    prior_g3.add_argument("--temporal-lab", type=Path)
    prior_g3.add_argument("--task-id", default="T013")
    prior_g3.add_argument("--effort", choices=("low", "high"), default="low")
    prior_g3.add_argument("--bootstrap-output", type=Path)
    freeze_g3 = subparsers.add_parser(
        "freeze-g3-public",
        help="freeze G3 after G2 approval and a trusted prior episode seed",
    )
    freeze_g3.add_argument("--temporal-lab", type=Path)
    freeze_g3.add_argument("--trusted-grades", type=Path, required=True)
    freeze_g3.add_argument("--m4-seed", type=Path, required=True)
    freeze_g3.add_argument("--output", type=Path)
    run_g3 = subparsers.add_parser(
        "run-g3-public",
        help="prepare and resume the frozen public G3 generation grid",
    )
    run_g3.add_argument("--freeze", type=Path)
    run_g3.add_argument("--static-lab", type=Path)
    run_g3.add_argument("--temporal-lab", type=Path)
    run_g3.add_argument("--max-new-calls", type=int)
    run_g3.add_argument("--concurrency", type=int, default=4)
    run_g3.add_argument("--task-ids")
    review_memory_g3 = subparsers.add_parser(
        "review-g3-unsupported-memory",
        help="build one evidence-bound disposition for all 558 exported answers",
    )
    review_memory_g3.add_argument("--reviewed-at", required=True)
    review_memory_g3.add_argument("--output", type=Path)
    approve_memory_review_g3 = subparsers.add_parser(
        "approve-g3-unsupported-memory-review",
        help="bind Kevin's audit to the exact exhaustive disposition report",
    )
    approve_memory_review_g3.add_argument("--review-sha256", required=True)
    approve_memory_review_g3.add_argument("--approved-at", required=True)
    approve_memory_review_g3.add_argument("--output", type=Path)
    sealed_candidates_g3 = subparsers.add_parser(
        "build-g3-sealed-candidates",
        help="freeze the content-free 12 x 5 x 2 external G3 grid",
    )
    sealed_candidates_g3.add_argument("--g3-freeze", type=Path)
    sealed_candidates_g3.add_argument("--external-bundle-sha256", required=True)
    sealed_candidates_g3.add_argument("--output", type=Path)
    external_g3 = subparsers.add_parser(
        "g3-external-evaluate",
        help="run sealed G3 temporal cells in an external work directory",
    )
    external_g3.add_argument("--bundle", type=Path, required=True)
    external_g3.add_argument("--candidate-manifest", type=Path, required=True)
    external_g3.add_argument("--work-root", type=Path, required=True)
    external_g3.add_argument("--return-path", type=Path)
    external_g3.add_argument("--max-new-calls", type=int)
    external_g3.add_argument("--concurrency", type=int, default=4)
    sealed_import_g3 = subparsers.add_parser(
        "g3-sealed-import",
        help="import a content-free external G3 temporal return",
    )
    sealed_import_g3.add_argument("--input", type=Path, required=True)
    sealed_import_g3.add_argument("--candidate-manifest", type=Path)
    sealed_import_g3.add_argument("--output", type=Path)
    subparsers.add_parser(
        "report-g3-memory",
        help="publish the replayable Week 12 temporal-memory technical report",
    )
    subparsers.add_parser(
        "preflight-g3-calibration",
        help="derive exact AI token counts for the active G3 calibration packets",
    )
    confirm_calibration_g3 = subparsers.add_parser(
        "confirm-g3-calibration-preflight",
        help="bind Kevin's confirmation before either G3 AI calibration review",
    )
    confirm_calibration_g3.add_argument("--confirmed-at", required=True)
    run_calibration_g3 = subparsers.add_parser(
        "run-g3-calibration-ai-review",
        help="run one fixed-profile AI against its exact confirmed G3 packet",
    )
    run_calibration_g3.add_argument(
        "--reviewer",
        choices=("gpt-5.6-sol-high", "claude-opus-5-medium"),
        required=True,
    )
    run_calibration_g3.add_argument("--timeout-seconds", type=int, default=1800)
    finalize_calibration_g3 = subparsers.add_parser(
        "finalize-g3-calibration",
        help="finalize Kevin's 22 grades and build the exact three-reviewer panel",
    )
    finalize_calibration_g3.add_argument("--kevin-response", type=Path, required=True)
    finalize_calibration_g3.add_argument("--identity-map", type=Path, required=True)
    finalize_calibration_g3.add_argument("--reference", type=Path, required=True)
    subparsers.add_parser(
        "gate-g3", help="freeze the current G1-bound G3 technical gate for AI review"
    )
    record_g3_review = subparsers.add_parser(
        "record-g3-ai-gate-review",
        help="record one invocation-bound fixed-profile G3 AI gate review",
    )
    record_g3_review.add_argument(
        "--reviewer",
        choices=("gpt-5.6-sol-high", "claude-opus-5-medium"),
        required=True,
    )
    subparsers.add_parser(
        "gate-g3-ai-reviews",
        help="combine both current G3 AI reviews while Kevin remains pending",
    )
    approve_g3 = subparsers.add_parser(
        "approve-g3", help="record Kevin's final G3 decision after both AI reviews"
    )
    approve_g3.add_argument(
        "--decision", choices=("promote", "retain-simple"), required=True
    )
    approve_g3.add_argument("--selected-policy", choices=("M1", "M2", "M3", "M4"))
    approve_g3.add_argument("--decided-at", required=True)
    subparsers.add_parser(
        "export-g4-viewer",
        help="publish the immutable public viewer after Kevin's approved G3 gate",
    )
    subparsers.add_parser(
        "verify-g4", help="run and save reproducible static-viewer verification"
    )
    record_g4_review = subparsers.add_parser(
        "record-g4-ai-review",
        help="record one invocation-bound fixed-profile G4 AI review",
    )
    record_g4_review.add_argument(
        "--reviewer",
        choices=("gpt-5.6-sol-high", "claude-opus-5-medium"),
        required=True,
    )
    subparsers.add_parser(
        "gate-g4", help="build the dual-AI-reviewed pending G4 acceptance gate"
    )
    approve_g4 = subparsers.add_parser(
        "approve-g4", help="bind Kevin's approval to the exact pending G4 gate"
    )
    approve_g4.add_argument("--approved-at", required=True)
    freeze_g5 = subparsers.add_parser(
        "freeze-g5",
        help="freeze the content-free G5 prerequisites for an F6 proposal",
    )
    freeze_g5.add_argument("--proposal-id", required=True)
    freeze_g5.add_argument("--proposal-agent-id", required=True)
    freeze_g5.add_argument("--proposal-artifact", type=Path, required=True)
    freeze_g5.add_argument("--sealed-evaluation-path", type=Path, required=True)
    freeze_g5.add_argument("--sealed-evaluation-commitment-sha256", required=True)
    freeze_g5.add_argument("--rollback-artifact", type=Path, required=True)
    freeze_g5.add_argument("--budget-limit-usd", required=True)
    freeze_g5.add_argument("--proposed-system-id", required=True)
    freeze_g5.add_argument("--simpler-baseline-id", required=True)
    freeze_g5.add_argument("--simpler-baseline-artifact", type=Path, required=True)
    approve_g5 = subparsers.add_parser(
        "approve-g5", help="bind Kevin's approval to the exact current G5 evidence"
    )
    approve_g5.add_argument("--technical-evidence-sha256", required=True)
    approve_g5.add_argument("--approved-at", required=True)
    freeze_final_review = subparsers.add_parser(
        "freeze-final-review",
        help="freeze the external 1,600-cell final review after approved G4",
    )
    freeze_final_review.add_argument("--bundle", type=Path, required=True)
    freeze_final_review.add_argument("--seed-file", type=Path, required=True)
    freeze_final_review.add_argument("--staging", type=Path, required=True)
    freeze_final_review.add_argument("--identity-map", type=Path, required=True)
    preflight_final_review = subparsers.add_parser(
        "preflight-final-review",
        help="derive exact AI packet token counts with the pinned verifier",
    )
    preflight_final_review.add_argument("--staging", type=Path, required=True)
    confirm_final_review = subparsers.add_parser(
        "confirm-final-review-preflight",
        help="bind Kevin's confirmation to the exact AI packet and token totals",
    )
    confirm_final_review.add_argument("--staging", type=Path, required=True)
    confirm_final_review.add_argument("--confirmed-at", required=True)
    release_final_review_parser = subparsers.add_parser(
        "release-final-review",
        help="release calibration or main packets outside the repository",
    )
    release_final_review_parser.add_argument("--staging", type=Path, required=True)
    release_final_review_parser.add_argument("--release", type=Path, required=True)
    release_final_review_parser.add_argument(
        "--phase", choices=("calibration", "main"), required=True
    )
    release_final_review_parser.add_argument("--calibration-record", type=Path)
    finalize_calibration = subparsers.add_parser(
        "finalize-final-review-calibration",
        help="require complete GPT, Claude, and Kevin calibration returns",
    )
    finalize_calibration.add_argument("--staging", type=Path, required=True)
    finalize_calibration.add_argument("--release", type=Path, required=True)
    finalize_calibration.add_argument("--identity-map", type=Path, required=True)
    finalize_calibration.add_argument("--reference", type=Path, required=True)
    finalize_calibration.add_argument("--gpt-return", type=Path, required=True)
    finalize_calibration.add_argument("--claude-return", type=Path, required=True)
    finalize_calibration.add_argument("--kevin-return", type=Path, required=True)
    finalize_calibration.add_argument("--output", type=Path, required=True)
    import_final_review = subparsers.add_parser(
        "import-final-review",
        help="import one complete blinded panel return into a new external store",
    )
    import_final_review.add_argument(
        "--reviewer",
        choices=("gpt-5.6-sol-high", "claude-opus-5-medium", "kevin"),
        required=True,
    )
    import_final_review.add_argument("--staging", type=Path, required=True)
    import_final_review.add_argument("--release", type=Path, required=True)
    import_final_review.add_argument("--input", type=Path, required=True)
    import_final_review.add_argument("--store", type=Path, required=True)
    finalize_final_review = subparsers.add_parser(
        "finalize-final-review",
        help="require all three complete returns and publish agreement analysis",
    )
    finalize_final_review.add_argument("--staging", type=Path, required=True)
    finalize_final_review.add_argument("--identity-map", type=Path, required=True)
    finalize_final_review.add_argument("--gpt-store", type=Path, required=True)
    finalize_final_review.add_argument("--claude-store", type=Path, required=True)
    finalize_final_review.add_argument("--kevin-store", type=Path, required=True)
    finalize_final_review.add_argument("--output", type=Path)
    subparsers.add_parser(
        "frontier-status",
        help="read the F1-F7 barriers without writing or entering an experiment",
    )
    subparsers.add_parser(
        "freeze-frontier-entry",
        help="freeze content-free F1-F7 entry evidence after approved G4",
    )
    record_frontier_entry_review = subparsers.add_parser(
        "record-frontier-entry-ai-review",
        help="record one invocation-bound AI review of the frozen F1-F7 entry gate",
    )
    record_frontier_entry_review.add_argument(
        "--reviewer",
        choices=("gpt-5.6-sol-high", "gpt-5.6-terra-high"),
        required=True,
    )
    subparsers.add_parser(
        "gate-frontier-entry-ai-reviews",
        help="combine both frontier-entry AI reviews for Kevin's audit",
    )
    approve_frontier = subparsers.add_parser(
        "approve-frontier-entry",
        help="bind Kevin's approval to the exact frozen F1-F7 entry gate",
    )
    approve_frontier.add_argument("--approved-at", required=True)
    subparsers.add_parser(
        "prepare-frontier-f1",
        help="prepare the approved provider-free F1 indexed-memory grid",
    )
    finalize_f1 = subparsers.add_parser(
        "finalize-frontier-f1",
        help="import terminal F1 receipts into a pending-review result",
    )
    finalize_f1.add_argument("--input", type=Path, required=True)
    subparsers.add_parser(
        "prepare-frontier-f2",
        help="freeze the approved provider-free F2 action-decision packet",
    )
    finalize_f2 = subparsers.add_parser(
        "finalize-frontier-f2",
        help="import low/high F2 receipts and score the action decisions",
    )
    finalize_f2.add_argument("--input", type=Path, required=True)
    freeze_f3_source = subparsers.add_parser(
        "freeze-frontier-f3-source",
        help="freeze the canonical public F3 page-source manifest before entry review",
    )
    freeze_f3_source.add_argument("--input", type=Path, required=True)
    prepare_f3 = subparsers.add_parser(
        "prepare-frontier-f3",
        help="prepare the approved provider-free F3 paging comparison",
    )
    prepare_f3.add_argument("--input", type=Path, required=True)
    subparsers.add_parser(
        "run-frontier-f3",
        help="run or replay the approved 40-cell F3 provider campaign",
    )
    finalize_f3 = subparsers.add_parser(
        "finalize-frontier-f3",
        help="import 40 F3 outcomes into a pending-review result",
    )
    finalize_f3.add_argument("--input", type=Path, required=True)
    subparsers.add_parser(
        "run-frontier-f5",
        help="run or replay the approved bounded public F5 search demonstration",
    )
    freeze_frontier_result = subparsers.add_parser(
        "freeze-frontier-result-review",
        help="freeze an exact F1-F3 or F5 result claim for dual-AI review",
    )
    freeze_frontier_result.add_argument("--input", type=Path, required=True)
    record_frontier_review = subparsers.add_parser(
        "record-frontier-ai-review",
        help="record one invocation-bound AI review of a frozen frontier result",
    )
    record_frontier_review.add_argument(
        "--experiment", choices=("F1", "F2", "F3", "F5"), required=True
    )
    record_frontier_review.add_argument(
        "--reviewer",
        choices=(
            "gpt-5.6-sol-high",
            "claude-opus-5-medium",
            "gpt-5.6-terra-high",
        ),
        required=True,
    )
    gate_frontier_result = subparsers.add_parser(
        "gate-frontier-result",
        help="combine Sol with Claude, or the Terra fallback, for result review",
    )
    gate_frontier_result.add_argument(
        "--experiment", choices=("F1", "F2", "F3", "F5"), required=True
    )
    approve_frontier_result_parser = subparsers.add_parser(
        "approve-frontier-result",
        help="bind Kevin's approval to a passing frontier-result review gate",
    )
    approve_frontier_result_parser.add_argument(
        "--experiment", choices=("F1", "F2", "F3", "F5"), required=True
    )
    approve_frontier_result_parser.add_argument("--approved-at", required=True)
    sealed = subparsers.add_parser(
        "sealed-import", help="import a strict external sealed return"
    )
    sealed.add_argument("--input", type=Path, required=True)
    sealed.add_argument("--candidate-manifest", type=Path, required=True)
    sealed.add_argument("--external-bundle-sha256", required=True)
    sealed.add_argument("--output", type=Path, required=True)
    quote = subparsers.add_parser(
        "cost-quote", help="estimate a paid run for cost auditing"
    )
    quote.add_argument("--input-tokens", type=int, required=True)
    quote.add_argument("--output-tokens", type=int, required=True)
    quote.add_argument("--calls", type=int, default=1)
    paid = subparsers.add_parser(
        "paid-generate", help="run one fixed, budgeted generation"
    )
    paid.add_argument("--spec", type=Path, required=True)
    paid.add_argument("--output", type=Path, required=True)
    enrich = subparsers.add_parser(
        "paid-enrich", help="fetch delayed OpenRouter timing for a saved generation"
    )
    enrich.add_argument("--result", type=Path, required=True)
    return parser


def _manifest_path(root: Path, explicit: Path | None) -> Path:
    return explicit if explicit is not None else default_manifest_path(root)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json_once_or_verify(
    root: Path, path: Path, value: dict[str, object]
) -> None:
    """Create one immutable JSON artifact, or accept the exact saved value."""

    try:
        write_json_once_or_verify(root, path, value)
    except ImmutableIOError as exc:
        raise ValueError(f"cannot persist immutable artifact: {path}") from exc


def _load_json_object(path: Path, label: str) -> dict[str, object]:
    """Read one explicit CLI input as a JSON object with a stable error."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {label}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _validate_native_review(
    root: Path,
    *,
    anchor_path: Path,
    reviewer_id: str,
    review_kind: str,
    target_bindings: dict[str, str],
    response: dict[str, object],
    receipt: dict[str, object],
) -> dict[str, object]:
    """Replay a CLI-native invocation and its exact gate-receipt proof fields."""

    from .review_invocations import (
        assert_native_proof_fields,
        validate_recorded_ai_review,
    )

    invocation_id = receipt.get("invocation_id")
    completed_at = receipt.get("completed_at")
    if not isinstance(invocation_id, str) or not isinstance(completed_at, str):
        raise ValueError("AI review receipt lacks native invocation identity")
    evidence = validate_recorded_ai_review(
        root,
        anchor_path=anchor_path,
        reviewer_id=reviewer_id,
        review_kind=review_kind,
        target_bindings=target_bindings,
        expected_response=response,
        invocation_id=invocation_id,
        completed_at=completed_at,
    )
    assert_native_proof_fields(
        anchor_path=anchor_path, receipt=receipt, evidence=evidence
    )
    return evidence


def _only_list_field(
    value: dict[str, object], field: str, label: str
) -> list[dict[str, object]]:
    """Require a single-field JSON envelope containing object rows."""

    rows = value.get(field)
    if set(value) != {field} or not isinstance(rows, list):
        raise ValueError(f"{label} must contain only {field}")
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"{label} {field} must contain only JSON objects")
    return rows


def _g3_canonical_gate_inputs(root: Path) -> dict[str, object]:
    """Load only the fixed public/content-free artifacts accepted by G3."""

    paths = {
        "g1_gate": Path("results/v2/gates/G1.json"),
        "g3_freeze": Path("results/v2/memory/g3_public_freeze.json"),
        "public_run": Path("results/v2/memory/g3_public_generation_run.json"),
        "public_metrics": Path("results/v2/memory/g3_public_metrics.json"),
        "sealed_candidate_manifest": Path(
            "results/v2/memory/g3_sealed_candidates.json"
        ),
        "sealed_import": Path("results/v2/memory/g3_sealed_import.json"),
        "lifecycle_evidence": Path("results/v2/memory/g3_lifecycle_evidence.json"),
        "panel_calibration": Path("results/v2/memory/g3_panel_calibration.json"),
        "failure_report": Path("results/v2/memory/g3_failure_and_harm_report.json"),
        "unsupported_memory_review": Path(
            "results/v2/reviews/g3_unsupported_memory_dispositions.json"
        ),
        "unsupported_memory_review_approval": Path(
            "results/v2/reviews/g3_unsupported_memory_dispositions_kevin_approval.json"
        ),
    }
    values: dict[str, object] = {
        key: _load_json_object(root / relative, f"canonical {key.replace('_', ' ')}")
        for key, relative in paths.items()
    }
    public_run = values["public_run"]
    if not isinstance(public_run, dict) or not isinstance(
        public_run.get("cells"), list
    ):
        raise ValueError("canonical public G3 run has no cells")
    receipts: list[dict[str, object]] = []
    seen_paths: set[str] = set()
    for cell in public_run["cells"]:
        if not isinstance(cell, dict):
            raise ValueError("canonical public G3 run cell is invalid")
        path_value = cell.get("receipt_path")
        if not isinstance(path_value, str) or not path_value:
            raise ValueError("canonical public G3 receipt path is missing")
        relative = Path(path_value)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or "\\" in path_value
            or relative.as_posix() != path_value
            or path_value in seen_paths
        ):
            raise ValueError("canonical public G3 receipt path is unsafe or repeated")
        seen_paths.add(path_value)
        receipts.append(
            _load_json_object(root / relative, "canonical public G3 receipt")
        )
    values["public_receipts"] = receipts
    return values


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "freeze-v1":
            manifest = build_manifest(root)
            output = _manifest_path(root, args.output)
            write_manifest(manifest, output)
            print(f"wrote {output.relative_to(root)}")
            for name, count in snapshot_file_counts(manifest):
                print(f"{name}: {count} files")
            return 0
        if args.command == "verify-v1":
            manifest = verify_manifest(_manifest_path(root, args.manifest), root)
            print(
                json.dumps(
                    {"status": "passed", "snapshots": len(manifest["snapshots"])},
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "audit-truth":
            findings = audit_truth_language(root)
            if findings:
                print("\n".join(findings), file=sys.stderr)
                return 1
            print("truth_language_audit=passed")
            return 0
        if args.command == "gate-g0":
            from .gates import run_g0_gate

            record = run_g0_gate(root)
            print(json.dumps(record, sort_keys=True))
            return 0
        if args.command == "approve-g0":
            from .gates import approve_g0_gate

            approval = approve_g0_gate(
                root,
                expected_technical_evidence_sha256=(args.technical_evidence_sha256),
                approved_at=args.approved_at,
            )
            print(json.dumps(approval, sort_keys=True))
            return 0
        if args.command == "build-g1":
            from .adapters import build_v1_adapter_sample
            from .tasking import build_task_foundation
            from .traces import build_static_trace_mock

            artifacts = {
                "contracts": build_contract_artifacts(root),
                "tasks": {
                    "task_count": build_task_foundation(root)["task_count"],
                },
                "adapter_sample_rows": len(build_v1_adapter_sample(root)),
                "trace_mock": build_static_trace_mock(root),
            }
            from .sealed import build_sealed_contract_fixtures

            artifacts["sealed_contract"] = build_sealed_contract_fixtures(root)
            print(json.dumps(artifacts, sort_keys=True))
            return 0
        if args.command == "gate-g1":
            from .gates import run_g1_gate

            print(json.dumps(run_g1_gate(root, args.output), sort_keys=True))
            return 0
        if args.command == "approve-g1":
            from .gates import approve_g1_gate

            record = approve_g1_gate(
                root,
                expected_technical_evidence_sha256=(args.technical_evidence_sha256),
                approved_at=args.approved_at,
            )
            print(
                json.dumps(
                    {
                        "status": "recorded",
                        "gate": "G1",
                        "technical_evidence_sha256": record[
                            "technical_evidence_sha256"
                        ],
                        "approved_at": record["approved_at"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "freeze-g2-static":
            from .static_benchmark import write_static_freeze

            manifest = write_static_freeze(args.sealed_bundle_sha256, root, args.output)
            print(
                json.dumps(
                    {
                        "status": "frozen",
                        "task_count": manifest["task_count"],
                        "manifest_sha256": manifest["manifest_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "embed-g2-queries":
            from .embeddings import (
                embed_texts,
                load_embedding_cache,
                load_extension_cache,
            )
            from .experiments import (
                FROZEN_EMBEDDINGS_SHA256,
                chunk_embedding_text,
                load_frozen_chunks,
            )
            from .static_benchmark import public_static_tasks

            base = (
                root / "evaluation/build/embeddings_openai_text-embedding-3-small.jsonl"
            )
            extension = root / "results/v2/embeddings/public_query_embeddings.jsonl"
            texts = [
                *(str(task["question_text"]) for task in public_static_tasks(root)),
                *(chunk_embedding_text(chunk) for chunk in load_frozen_chunks(root)),
            ]
            result = embed_texts(
                texts,
                base_cache_path=base,
                extension_cache_path=extension,
                expected_base_sha256=FROZEN_EMBEDDINGS_SHA256,
                ledger=CostLedger(canonical_ledger_path(root)),
                run_id=args.run_id,
                root=root,
            )
            base_rows = len(
                load_embedding_cache(
                    base, expected_base_sha256=FROZEN_EMBEDDINGS_SHA256
                )
            )
            extension_rows = len(load_extension_cache(extension))
            record = {
                "schema_version": "contextlab.embedding-run.v1",
                "run_id": args.run_id,
                "input_count": len(texts),
                "base_cache_rows": base_rows,
                "extension_cache_rows": extension_rows,
                "paid_batch_count": len(result.batches),
                "paid_batches": result.batches,
                "extension_cache_sha256": _sha256_file(extension),
            }
            output = args.output or root / "results/v2/embeddings/public_query_run.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            print(json.dumps(record, sort_keys=True))
            return 0
        if args.command == "run-g2-components":
            from .embeddings import load_embedding_cache, load_extension_cache
            from .experiments import (
                FROZEN_EMBEDDINGS_SHA256,
                write_public_component_lab,
            )

            base = (
                root / "evaluation/build/embeddings_openai_text-embedding-3-small.jsonl"
            )
            extension = root / "results/v2/embeddings/public_query_embeddings.jsonl"
            embeddings = load_embedding_cache(
                base, expected_base_sha256=FROZEN_EMBEDDINGS_SHA256
            )
            extra = load_extension_cache(extension)
            if set(embeddings).intersection(extra):
                raise ValueError(
                    "G2 extension cache duplicates a frozen base embedding"
                )
            embeddings.update(extra)
            lab = write_public_component_lab(embeddings, root, args.output)
            print(
                json.dumps(
                    {
                        "status": "completed",
                        "task_count": lab["task_count"],
                        "cell_count": lab["cell_count"],
                        "artifact_sha256": lab["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "report-g2-components":
            from .reports import write_g2_component_reports

            lab_path = (
                args.lab or root / "results/v2/retrieval/public_component_lab.json"
            )
            result = write_g2_component_reports(lab_path, root)
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.command == "run-g2-generations":
            from .generations import run_public_generation_batch

            lab_path = (
                args.lab or root / "results/v2/retrieval/public_component_lab.json"
            )
            lab = json.loads(lab_path.read_text(encoding="utf-8"))
            manifest = run_public_generation_batch(
                lab,
                root=root,
                strategies=tuple(
                    value for value in args.strategies.split(",") if value
                ),
                efforts=tuple(value for value in args.efforts.split(",") if value),
                task_ids=(
                    tuple(value for value in args.task_ids.split(",") if value)
                    if args.task_ids
                    else None
                ),
                trial=args.trial,
                max_new_calls=args.max_new_calls,
                concurrency=args.concurrency,
            )
            print(
                json.dumps(
                    {
                        "generation_campaign_id": manifest["generation_campaign_id"],
                        "output_token_limit": manifest["output_token_limit"],
                        "status_counts": manifest["status_counts"],
                        "recorded_cell_count": manifest["recorded_cell_count"],
                        "new_call_count": manifest["new_call_count"],
                        "actual_usd": manifest["actual_usd"],
                        "manifest_sha256": manifest["manifest_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "score-g2-generations":
            from .answer_metrics import write_public_answer_metrics
            from .experiments import load_protocol
            from .generations import generation_manifest_path

            campaign_id = str(
                load_protocol(root)["fixed_comparison"]["generation_campaign_id"]
            )
            manifest_path = args.manifest or generation_manifest_path(
                root, 1, campaign_id
            )
            lab_path = (
                args.lab or root / "results/v2/retrieval/public_component_lab.json"
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            lab = json.loads(lab_path.read_text(encoding="utf-8"))
            metrics = write_public_answer_metrics(
                manifest, lab, root=root, output=args.output
            )
            print(
                json.dumps(
                    {
                        "completed_cell_count": metrics["completed_cell_count"],
                        "artifact_sha256": metrics["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "run-g2-repeats":
            from .experiments import load_protocol
            from .generations import generation_manifest_path
            from .repeats import run_public_generation_repeats

            lab_path = (
                args.lab or root / "results/v2/retrieval/public_component_lab.json"
            )
            campaign_id = str(
                load_protocol(root)["fixed_comparison"]["generation_campaign_id"]
            )
            trial_one_path = args.trial_one or generation_manifest_path(
                root, 1, campaign_id
            )
            lab = json.loads(lab_path.read_text(encoding="utf-8"))
            trial_one = json.loads(trial_one_path.read_text(encoding="utf-8"))
            manifests = run_public_generation_repeats(
                lab,
                trial_one,
                root=root,
                max_new_calls_per_trial=args.max_new_calls_per_trial,
                concurrency=args.concurrency,
            )
            print(
                json.dumps(
                    {
                        "trials": [
                            {
                                "trial": manifest["trial"],
                                "status_counts": manifest["status_counts"],
                                "recorded_cell_count": manifest["recorded_cell_count"],
                                "actual_usd": manifest["actual_usd"],
                            }
                            for manifest in manifests
                        ]
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "score-g2-repeats":
            from .experiments import load_protocol
            from .generations import generation_manifest_path
            from .repeats import analyze_public_generation_repeats

            lab_path = (
                args.lab or root / "results/v2/retrieval/public_component_lab.json"
            )
            lab = json.loads(lab_path.read_text(encoding="utf-8"))
            campaign_id = str(
                load_protocol(root)["fixed_comparison"]["generation_campaign_id"]
            )
            manifests = [
                json.loads(
                    generation_manifest_path(root, trial, campaign_id).read_text(
                        encoding="utf-8"
                    )
                )
                for trial in range(1, 6)
            ]
            analysis = analyze_public_generation_repeats(manifests, lab, root)
            output = args.output or root / "results/v2/reports/g2_public_repeats.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(analysis, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(
                json.dumps(
                    {
                        "status": "completed",
                        "cell_count": len(analysis["cells"]),
                        "analysis_sha256": analysis["analysis_sha256"],
                        "output": str(output.resolve().relative_to(root)),
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "g2-sealed-import":
            from .g2_sealed import import_g2_sealed_return

            output = args.output or root / "results/v2/sealed/g2-import.json"
            imported = import_g2_sealed_return(args.input, output, root=root)
            print(
                json.dumps(
                    {
                        "status": "imported",
                        "component_cell_count": len(imported["component_records"]),
                        "source_return_sha256": imported["source_return_sha256"],
                        "output": str(output.resolve().relative_to(root)),
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "gate-g2":
            from .g2_gate_io import run_and_write_g2_gate

            record = run_and_write_g2_gate(
                root, output=args.output, approval_path=args.approval
            )
            print(
                json.dumps(
                    {
                        "technical_decision": record["technical_decision"],
                        "final_decision": record["final_decision"],
                        "human_approval": record["human_approval"]["status"],
                        "promoted_retriever_id": record["promoted_retriever_id"],
                        "artifact_sha256": record["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "approve-g2":
            from .g2_gate_io import approve_existing_g2_gate

            approval = approve_existing_g2_gate(
                root,
                gate_path=args.gate,
                output=args.output,
                approved_at=args.approved_at,
            )
            print(
                json.dumps(
                    {
                        "status": "recorded",
                        "reviewer": approval["reviewer"],
                        "gate_sha256": approval["gate_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "g2-external-evaluate":
            from .g2_external_eval import run_g2_external_evaluation

            evaluated = run_g2_external_evaluation(
                args.bundle,
                work_root=args.work_root,
                return_path=args.return_path,
                root=root,
                max_new_calls=args.max_new_calls,
                concurrency=args.concurrency,
            )
            generation = evaluated["generation_summary"]
            print(
                json.dumps(
                    {
                        "status": (
                            "completed"
                            if generation["status_counts"]["pending"] == 0
                            else "partial"
                        ),
                        "component_cell_count": len(evaluated["component_records"]),
                        "generation_count": generation["generation_count"],
                        "generation_status_counts": generation["status_counts"],
                        "static_freeze_manifest_sha256": evaluated[
                            "static_freeze_manifest_sha256"
                        ],
                        "external_bundle_sha256": evaluated["external_bundle_sha256"],
                        "retrieval_protocol_sha256": evaluated[
                            "retrieval_protocol_sha256"
                        ],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "verify-g3-protocol":
            from .g3_freeze import load_memory_protocol

            protocol = load_memory_protocol(root)
            print(
                json.dumps(
                    {
                        "status": "passed",
                        "campaign_id": protocol["campaign_id"],
                        "protocol_sha256": sha256_json(protocol),
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "embed-g3-temporal":
            from .embeddings import (
                embed_texts,
                load_embedding_cache,
                load_extension_cache,
            )
            from .experiments import FROZEN_EMBEDDINGS_SHA256
            from .g3_evidence import g3_embedding_inputs
            from .g3_freeze import load_approved_g2_gate

            # Fail before credential access or provider preflight when G2 is pending.
            load_approved_g2_gate(root)
            base = (
                root / "evaluation/build/embeddings_openai_text-embedding-3-small.jsonl"
            )
            extension = root / "results/v2/embeddings/g3_temporal_embeddings.jsonl"
            inputs = g3_embedding_inputs()
            result = embed_texts(
                inputs,
                base_cache_path=base,
                extension_cache_path=extension,
                expected_base_sha256=FROZEN_EMBEDDINGS_SHA256,
                ledger=CostLedger(canonical_ledger_path(root)),
                run_id=args.run_id,
                root=root,
            )
            record = {
                "schema_version": "contextlab.g3-embedding-run.v1",
                "run_id": args.run_id,
                "input_count": len(inputs),
                "base_cache_rows": len(
                    load_embedding_cache(
                        base, expected_base_sha256=FROZEN_EMBEDDINGS_SHA256
                    )
                ),
                "extension_cache_rows": len(load_extension_cache(extension)),
                "paid_batch_count": len(result.batches),
                "paid_batches": result.batches,
                "extension_cache_sha256": _sha256_file(extension),
                "approved_g2_gate": "verified",
            }
            record["artifact_sha256"] = sha256_json(record)
            output = (
                args.output
                or root / "results/v2/embeddings/g3_temporal_embedding_run.json"
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(json.dumps(record, sort_keys=True))
            return 0
        if args.command == "build-g3-temporal-r0":
            from .embeddings import load_embedding_cache, load_extension_cache
            from .experiments import FROZEN_EMBEDDINGS_SHA256
            from .g3_evidence import build_temporal_r0_lab

            base = (
                root / "evaluation/build/embeddings_openai_text-embedding-3-small.jsonl"
            )
            extension = (
                args.embedding_cache
                or root / "results/v2/embeddings/g3_temporal_embeddings.jsonl"
            )
            embeddings = load_embedding_cache(
                base, expected_base_sha256=FROZEN_EMBEDDINGS_SHA256
            )
            extra = load_extension_cache(extension)
            if set(embeddings).intersection(extra):
                raise ValueError(
                    "G3 extension cache duplicates a frozen base embedding"
                )
            embeddings.update(extra)
            lab = build_temporal_r0_lab(embeddings, root=root)
            output = (
                args.output or root / "results/v2/retrieval/g3_temporal_r0_lab.json"
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(lab, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(
                json.dumps(
                    {
                        "status": "completed",
                        "trace_count": lab["trace_count"],
                        "artifact_sha256": lab["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "run-g3-prior":
            from .g3_execution import prepare_public_g3_cell
            from .g3_freeze import (
                build_g3_prior_bootstrap,
                write_g3_prior_bootstrap,
            )
            from .g3_prior_runs import (
                build_prior_objective_run,
                canonical_prior_run_path,
                derive_trusted_grade_and_episode_seed,
                validate_prior_objective_run,
                write_prior_objective_run,
            )
            from .gateway import run_paid_generation_to_file

            temporal_path = (
                args.temporal_lab
                or root / "results/v2/retrieval/g3_temporal_r0_lab.json"
            )
            temporal_lab = json.loads(temporal_path.read_text(encoding="utf-8"))
            bootstrap = build_g3_prior_bootstrap(root=root, temporal_lab=temporal_lab)
            bootstrap_path = (
                args.bootstrap_output
                or root / "results/v2/memory/g3_prior_bootstrap.json"
            )
            bootstrap_path = (
                bootstrap_path
                if bootstrap_path.is_absolute()
                else root / bootstrap_path
            )
            if bootstrap_path.exists():
                saved_bootstrap = json.loads(bootstrap_path.read_text(encoding="utf-8"))
                if saved_bootstrap != bootstrap:
                    raise ValueError("saved G3 prior bootstrap differs from canonical")
            else:
                write_g3_prior_bootstrap(bootstrap, root=root, output=bootstrap_path)
            manifest = bootstrap["manifest"]
            spec = next(
                (
                    row
                    for row in manifest["run_specs"]
                    if row["policy"] == "M3"
                    and row["reasoning_effort"] == args.effort
                    and row["task"]["task_id"] == args.task_id
                ),
                None,
            )
            if spec is None or spec["task"]["suite"] != "temporal":
                raise ValueError("G3 prior task must be a public temporal M3 cell")
            static_lab = json.loads(
                (root / "results/v2/retrieval/public_component_lab.json").read_text(
                    encoding="utf-8"
                )
            )
            prepared = prepare_public_g3_cell(
                manifest,
                spec,
                trusted_frozen_manifest_sha256=manifest["frozen_manifest_sha256"],
                static_r0_lab=static_lab,
                temporal_r0_lab=temporal_lab,
                root=root,
            )
            prepared_path = (
                root
                / "results/v2/memory/prior_runs"
                / f"{spec['run_id']}.prepared.json"
            )
            _write_json_once_or_verify(root, prepared_path, prepared)
            result_path = (
                root
                / "results/v2/generations/public/g3-prior-v1"
                / "M3"
                / args.effort
                / f"{args.task_id}.json"
            )
            if result_path.exists():
                generation_result = json.loads(result_path.read_text(encoding="utf-8"))
                if generation_result.get("schema_version") != (
                    "contextlab.generation-result.v1"
                ):
                    raise ValueError(
                        "saved G3 prior generation is not a completed result"
                    )
            else:
                generation_result = run_paid_generation_to_file(
                    prepared["generation_spec"],
                    result_path,
                    ledger=CostLedger(canonical_ledger_path(root)),
                    root=root,
                )
            source = build_prior_objective_run(
                prepared,
                generation_result,
                bootstrap_manifest=manifest,
                trusted_bootstrap_manifest_sha256=manifest["frozen_manifest_sha256"],
            )
            source_path = canonical_prior_run_path(root, str(spec["run_id"]))
            if source_path.exists():
                saved_source = json.loads(source_path.read_text(encoding="utf-8"))
                validate_prior_objective_run(saved_source)
                if saved_source != source:
                    raise ValueError("saved G3 prior run differs from canonical")
            else:
                write_prior_objective_run(source, root=root)
            grade, seed = derive_trusted_grade_and_episode_seed(source)
            grade_path = root / "results/v2/memory/g3_trusted_grades.json"
            seed_path = root / "results/v2/memory/g3_m4_seed.json"
            _write_json_once_or_verify(
                root,
                grade_path,
                {
                    "trusted_grade_artifacts": [grade],
                    "source_artifact_sha256": source["artifact_sha256"],
                },
            )
            _write_json_once_or_verify(
                root,
                seed_path,
                {
                    "m4_episode_seed": [seed],
                    "source_artifact_sha256": source["artifact_sha256"],
                },
            )
            print(
                json.dumps(
                    {
                        "status": "completed",
                        "source_run_id": source["source_run_id"],
                        "objective_outcome": source["objective_outcome"],
                        "source_artifact_sha256": source["artifact_sha256"],
                        "grade_path": str(grade_path.relative_to(root)),
                        "seed_path": str(seed_path.relative_to(root)),
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "freeze-g3-public":
            from .g3_freeze import (
                build_canonical_g3_freeze,
                write_canonical_g3_freeze,
            )

            temporal_path = (
                args.temporal_lab
                or root / "results/v2/retrieval/g3_temporal_r0_lab.json"
            )
            temporal_lab = json.loads(temporal_path.read_text(encoding="utf-8"))
            grade_value = json.loads(args.trusted_grades.read_text(encoding="utf-8"))
            seed_value = json.loads(args.m4_seed.read_text(encoding="utf-8"))
            grades = (
                grade_value.get("trusted_grade_artifacts")
                if isinstance(grade_value, dict)
                else grade_value
            )
            seed = (
                seed_value.get("m4_episode_seed")
                if isinstance(seed_value, dict)
                else seed_value
            )
            if not isinstance(grades, list) or not isinstance(seed, list):
                raise ValueError("G3 trusted grades and M4 seed must be JSON lists")
            frozen = build_canonical_g3_freeze(
                root=root,
                temporal_lab=temporal_lab,
                trusted_grade_artifacts=grades,
                m4_episode_seed=seed,
            )
            output = write_canonical_g3_freeze(frozen, root=root, output=args.output)
            print(
                json.dumps(
                    {
                        "status": "frozen",
                        "output": str(output.relative_to(root)),
                        "artifact_sha256": frozen["artifact_sha256"],
                        "frozen_manifest_sha256": frozen["manifest"][
                            "frozen_manifest_sha256"
                        ],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "run-g3-public":
            from .g3_runner import run_public_g3_generations

            freeze_path = (
                args.freeze or root / "results/v2/memory/g3_public_freeze.json"
            )
            static_path = (
                args.static_lab
                or root / "results/v2/retrieval/public_component_lab.json"
            )
            temporal_path = (
                args.temporal_lab
                or root / "results/v2/retrieval/g3_temporal_r0_lab.json"
            )
            selected_task_ids = (
                [item for item in args.task_ids.split(",") if item]
                if args.task_ids
                else None
            )
            record = run_public_g3_generations(
                root=root,
                freeze=json.loads(freeze_path.read_text(encoding="utf-8")),
                static_lab=json.loads(static_path.read_text(encoding="utf-8")),
                temporal_lab=json.loads(temporal_path.read_text(encoding="utf-8")),
                max_new_calls=args.max_new_calls,
                concurrency=args.concurrency,
                selected_task_ids=selected_task_ids,
            )
            print(
                json.dumps(
                    {
                        "status": "checkpointed",
                        "recorded_cell_count": record["recorded_cell_count"],
                        "new_call_count": record["new_call_count"],
                        "generation_status_counts": record["generation_status_counts"],
                        "grade_status_counts": record["grade_status_counts"],
                        "artifact_sha256": record["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "review-g3-unsupported-memory":
            from .memory_review import (
                UNSUPPORTED_MEMORY_REVIEW_PATH,
                build_unsupported_memory_review,
            )

            metrics = _load_json_object(
                root / "results/v2/memory/g3_public_metrics.json",
                "public G3 metrics",
            )
            report = build_unsupported_memory_review(
                metrics,
                reviewed_at=args.reviewed_at,
                root=root,
            )
            output = args.output or root / UNSUPPORTED_MEMORY_REVIEW_PATH
            output = output if output.is_absolute() else root / output
            _write_json_once_or_verify(root, output, report)
            print(
                json.dumps(
                    {
                        "status": "dispositions-complete-pending-kevin-audit",
                        "disposition_count": report["summary"]["disposition_count"],
                        "unresolved_count": report["summary"]["unresolved_count"],
                        "likely_content_false_positive_count": report["summary"][
                            "likely_content_false_positive_count"
                        ],
                        "artifact_sha256": report["artifact_sha256"],
                        "output": output.relative_to(root).as_posix(),
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "approve-g3-unsupported-memory-review":
            from .memory_review import (
                UNSUPPORTED_MEMORY_REVIEW_APPROVAL_PATH,
                UNSUPPORTED_MEMORY_REVIEW_PATH,
                build_kevin_unsupported_memory_review_approval,
            )

            report = _load_json_object(
                root / UNSUPPORTED_MEMORY_REVIEW_PATH,
                "unsupported-memory disposition report",
            )
            approval = build_kevin_unsupported_memory_review_approval(
                report,
                expected_review_sha256=args.review_sha256,
                approved_at=args.approved_at,
            )
            output = args.output or root / UNSUPPORTED_MEMORY_REVIEW_APPROVAL_PATH
            output = output if output.is_absolute() else root / output
            _write_json_once_or_verify(root, output, approval)
            print(
                json.dumps(
                    {
                        "status": "approved",
                        "reviewer": approval["reviewer"],
                        "review_artifact_sha256": approval["review_artifact_sha256"],
                        "artifact_sha256": approval["artifact_sha256"],
                        "output": output.relative_to(root).as_posix(),
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "build-g3-sealed-candidates":
            from .g3_freeze import validate_g3_freeze
            from .g3_sealed import build_g3_sealed_candidate_manifest

            freeze_path = (
                args.g3_freeze or root / "results/v2/memory/g3_public_freeze.json"
            )
            freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
            validate_g3_freeze(freeze)
            candidate = build_g3_sealed_candidate_manifest(
                g3_freeze_sha256=freeze["artifact_sha256"],
                external_bundle_sha256=args.external_bundle_sha256,
            )
            output = args.output or root / "results/v2/memory/g3_sealed_candidates.json"
            output = output if output.is_absolute() else root / output
            _write_json_once_or_verify(root, output, candidate)
            print(
                json.dumps(
                    {
                        "status": "frozen",
                        "cell_count": candidate["cell_count"],
                        "artifact_sha256": candidate["artifact_sha256"],
                        "output": str(output.relative_to(root)),
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "g3-external-evaluate":
            from .g3_external_eval import (
                G3_EXTERNAL_PROGRESS_SCHEMA,
                load_g3_external_progress,
                run_g3_external_evaluation,
            )

            try:
                evaluated = run_g3_external_evaluation(
                    args.bundle,
                    args.candidate_manifest,
                    work_root=args.work_root,
                    return_path=args.return_path,
                    root=root,
                    max_new_calls=args.max_new_calls,
                    concurrency=args.concurrency,
                )
                progress = load_g3_external_progress(args.work_root, root=root)
                if evaluated.get("schema_version") == G3_EXTERNAL_PROGRESS_SCHEMA:
                    if evaluated != progress:
                        raise ValueError("external G3 progress reporting changed")
                elif (
                    evaluated.get("artifact_sha256") != progress["sealed_return_sha256"]
                ):
                    raise ValueError("external G3 sealed return reporting changed")
            except Exception as exc:
                raise ValueError(
                    "external G3 evaluation failed without a safe summary"
                ) from exc
            print(
                json.dumps(
                    {
                        "status": progress["status"],
                        "cell_count": progress["cell_count"],
                        "completed_count": progress["completed_count"],
                        "failed_count": progress["failed_count"],
                        "missing_count": progress["missing_count"],
                        "new_call_count": progress["new_call_count"],
                        "remaining_count": progress["remaining_count"],
                        "actual_usd": progress["actual_usd"],
                        "candidate_manifest_sha256": progress[
                            "candidate_manifest_sha256"
                        ],
                        "g3_freeze_sha256": progress["g3_freeze_sha256"],
                        "external_bundle_sha256": progress["external_bundle_sha256"],
                        "temporal_event_history_sha256": progress[
                            "temporal_event_history_sha256"
                        ],
                        "progress_sha256": progress["artifact_sha256"],
                        "sealed_return_sha256": progress["sealed_return_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "g3-sealed-import":
            from .g3_sealed import import_g3_sealed_return

            candidate_path = (
                args.candidate_manifest
                or root / "results/v2/memory/g3_sealed_candidates.json"
            )
            output = args.output or root / "results/v2/memory/g3_sealed_import.json"
            imported = import_g3_sealed_return(
                args.input,
                candidate_path,
                output,
                root=root,
            )
            print(
                json.dumps(
                    {
                        "status": "imported",
                        "record_count": len(imported["records"]),
                        "artifact_sha256": imported["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "report-g3-memory":
            from .memory_report import write_g3_temporal_memory_report

            report = write_g3_temporal_memory_report(root)
            print(
                json.dumps(
                    {
                        "status": report["status"],
                        "candidate_decision": report["technical_conclusion"][
                            "candidate_decision"
                        ],
                        "final_gate_decision": report["technical_conclusion"][
                            "final_gate_decision"
                        ],
                        "artifact_sha256": report["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "preflight-g3-calibration":
            from .g3_calibration import build_g3_calibration_token_preflight

            public_directory = root / "results/v2/reviews/g3_calibration"
            manifest = _load_json_object(
                public_directory / "manifest.json", "G3 calibration manifest"
            )
            preflight = build_g3_calibration_token_preflight(
                manifest,
                public_directory=public_directory,
                root=root,
            )
            print(
                json.dumps(
                    {
                        "status": "pending-kevin-confirmation",
                        "packet_count_per_ai": preflight["packet_count_per_ai"],
                        "utf8_bytes_total_per_ai": preflight["utf8_bytes_total_per_ai"],
                        "token_total_per_ai": preflight["token_total_per_ai"],
                        "artifact_sha256": preflight["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "confirm-g3-calibration-preflight":
            from .g3_calibration import confirm_g3_calibration_token_preflight

            public_directory = root / "results/v2/reviews/g3_calibration"
            manifest = _load_json_object(
                public_directory / "manifest.json", "G3 calibration manifest"
            )
            confirmation = confirm_g3_calibration_token_preflight(
                manifest,
                confirmed_at=args.confirmed_at,
                public_directory=public_directory,
                root=root,
            )
            print(
                json.dumps(
                    {
                        "status": "confirmed",
                        "reviewer": confirmation["reviewer"],
                        "confirmed_at": confirmation["confirmed_at"],
                        "artifact_sha256": confirmation["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "run-g3-calibration-ai-review":
            from .g3_calibration import run_g3_calibration_ai_review

            public_directory = root / "results/v2/reviews/g3_calibration"
            manifest = _load_json_object(
                public_directory / "manifest.json", "G3 calibration manifest"
            )
            result = run_g3_calibration_ai_review(
                reviewer=args.reviewer,
                manifest=manifest,
                public_directory=public_directory,
                root=root,
                timeout_seconds=args.timeout_seconds,
            )
            receipt = result["invocation_receipt"]
            print(
                json.dumps(
                    {
                        "status": "completed",
                        "reviewer": args.reviewer,
                        "grade_count": receipt["grade_count"],
                        "completed_return_artifact_sha256": receipt[
                            "completed_return_artifact_sha256"
                        ],
                        "native_invocation_evidence_sha256": receipt[
                            "native_invocation_evidence_sha256"
                        ],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "finalize-g3-calibration":
            from .g3_calibration import (
                build_g3_calibration_panel,
                finalize_g3_calibration_response_template,
            )

            public_directory = root / "results/v2/reviews/g3_calibration"
            manifest = _load_json_object(
                public_directory / "manifest.json", "G3 calibration manifest"
            )
            freeze = _load_json_object(
                root / "results/v2/memory/g3_public_freeze.json", "G3 freeze"
            )
            public_run = _load_json_object(
                root / "results/v2/memory/g3_public_generation_run.json",
                "public G3 run",
            )
            filled = _load_json_object(args.kevin_response, "Kevin G3 response")
            kevin_return = finalize_g3_calibration_response_template(
                filled,
                manifest=manifest,
                public_directory=public_directory,
                root=root,
            )
            kevin_completed_path = public_directory / "kevin/completed-return.json"
            _write_json_once_or_verify(root, kevin_completed_path, kevin_return)
            review_returns = {
                reviewer: _load_json_object(
                    public_directory / reviewer / "completed-return.json",
                    f"{reviewer} completed calibration return",
                )
                for reviewer in (
                    "gpt-5.6-sol-high",
                    "claude-opus-5-medium",
                    "kevin",
                )
            }
            panel = build_g3_calibration_panel(
                manifest=manifest,
                review_returns=review_returns,
                freeze=freeze,
                public_run=public_run,
                public_directory=public_directory,
                external_identity_map=args.identity_map,
                external_reference=args.reference,
                root=root,
            )
            panel_path = root / "results/v2/memory/g3_panel_calibration.json"
            _write_json_once_or_verify(root, panel_path, panel)
            print(
                json.dumps(
                    {
                        "status": panel["status"],
                        "reviewer_count": len(panel["reviewers"]),
                        "cell_count": len(panel["cells"]),
                        "hidden_repeat_count": len(panel["hidden_repeats"]),
                        "artifact_sha256": panel["artifact_sha256"],
                        "output": panel_path.relative_to(root).as_posix(),
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "gate-g3":
            from .g3_gate import G3_PENDING_GATE_PATH, build_g3_pending_gate

            gate_inputs = _g3_canonical_gate_inputs(root)
            pending = build_g3_pending_gate(root=root, **gate_inputs)
            _write_json_once_or_verify(root, root / G3_PENDING_GATE_PATH, pending)
            print(
                json.dumps(
                    {
                        "status": "pending-ai-review",
                        "technical_complete": pending["technical_complete"],
                        "technical_disposition": pending["technical_disposition"],
                        "eligible_policies": pending["eligible_policies"],
                        "technical_record_sha256": pending["technical_record_sha256"],
                        "artifact_sha256": pending["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "record-g3-ai-gate-review":
            from .g3_gate import (
                G3_AI_GATE_INVOCATION_RECEIPT_PATHS,
                G3_AI_GATE_REVIEW_PATHS,
                G3_PENDING_GATE_PATH,
                build_g3_ai_gate_invocation_receipt,
                build_g3_claude_gate_review,
                build_g3_sol_gate_review,
            )
            from .review_invocations import run_and_record_ai_review

            pending = _load_json_object(root / G3_PENDING_GATE_PATH, "pending G3 gate")
            anchor_path = G3_AI_GATE_INVOCATION_RECEIPT_PATHS[args.reviewer]
            target_bindings = {
                "pending_gate_path": G3_PENDING_GATE_PATH.as_posix(),
                "pending_gate_artifact_sha256": str(pending["artifact_sha256"]),
                "technical_record_sha256": str(pending["technical_record_sha256"]),
            }
            native = run_and_record_ai_review(
                root,
                anchor_path=anchor_path,
                reviewer_id=args.reviewer,
                review_kind="g3-gate",
                target_bindings=target_bindings,
            )
            response = native["response"]
            evidence = native["evidence"]
            receipt = build_g3_ai_gate_invocation_receipt(
                pending,
                reviewer_id=args.reviewer,
                invocation_id=evidence["native_invocation_id"],
                verdict=response["verdict"],
                blocking_findings=response["blocking_findings"],
                completed_at=evidence["completed_at"],
                native_invocation_evidence_sha256=evidence["artifact_sha256"],
                native_output_sha256=evidence["native_output_sha256"],
            )
            builder = (
                build_g3_sol_gate_review
                if args.reviewer == "gpt-5.6-sol-high"
                else build_g3_claude_gate_review
            )
            review = builder(
                pending,
                invocation_receipt=receipt,
                verdict=response["verdict"],
                blocking_findings=response["blocking_findings"],
                completed_at=evidence["completed_at"],
            )
            _write_json_once_or_verify(
                root, root / G3_AI_GATE_INVOCATION_RECEIPT_PATHS[args.reviewer], receipt
            )
            _write_json_once_or_verify(
                root, root / G3_AI_GATE_REVIEW_PATHS[args.reviewer], review
            )
            print(
                json.dumps(
                    {
                        "status": "recorded",
                        "reviewer_id": args.reviewer,
                        "verdict": review["verdict"],
                        "invocation_receipt_sha256": receipt["artifact_sha256"],
                        "native_invocation_evidence_sha256": evidence[
                            "artifact_sha256"
                        ],
                        "review_artifact_sha256": review["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "gate-g3-ai-reviews":
            from .g3_gate import (
                G3_AI_GATE_INVOCATION_RECEIPT_PATHS,
                G3_AI_GATE_REVIEW_PATHS,
                G3_PENDING_GATE_PATH,
                G3_REVIEWED_GATE_PATH,
                finalize_g3_gate,
            )

            pending = _load_json_object(root / G3_PENDING_GATE_PATH, "pending G3 gate")
            target_bindings = {
                "pending_gate_path": G3_PENDING_GATE_PATH.as_posix(),
                "pending_gate_artifact_sha256": str(pending["artifact_sha256"]),
                "technical_record_sha256": str(pending["technical_record_sha256"]),
            }
            reviews = []
            for reviewer in ("gpt-5.6-sol-high", "claude-opus-5-medium"):
                receipt = _load_json_object(
                    root / G3_AI_GATE_INVOCATION_RECEIPT_PATHS[reviewer],
                    f"{reviewer} G3 invocation receipt",
                )
                review = _load_json_object(
                    root / G3_AI_GATE_REVIEW_PATHS[reviewer],
                    f"{reviewer} G3 gate review",
                )
                if review.get("invocation_receipt") != receipt:
                    raise ValueError("G3 review differs from its invocation receipt")
                _validate_native_review(
                    root,
                    anchor_path=G3_AI_GATE_INVOCATION_RECEIPT_PATHS[reviewer],
                    reviewer_id=reviewer,
                    review_kind="g3-gate",
                    target_bindings=target_bindings,
                    response={
                        "verdict": review["verdict"],
                        "blocking_findings": review["blocking_findings"],
                    },
                    receipt=receipt,
                )
                reviews.append(review)
            reviewed = finalize_g3_gate(pending, ai_gate_reviews=reviews)
            _write_json_once_or_verify(root, root / G3_REVIEWED_GATE_PATH, reviewed)
            print(
                json.dumps(
                    {
                        "status": reviewed["gate_review_status"],
                        "human_decision": reviewed["human_decision"]["status"],
                        "final_decision": reviewed["final_decision"],
                        "artifact_sha256": reviewed["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "approve-g3":
            from .g3_gate import (
                G3_AI_GATE_INVOCATION_RECEIPT_PATHS,
                G3_AI_GATE_REVIEW_PATHS,
                G3_FINAL_GATE_PATH,
                G3_KEVIN_DECISION_PATH,
                G3_PENDING_GATE_PATH,
                G3_REVIEWED_GATE_PATH,
                build_g3_kevin_decision,
                finalize_g3_gate,
            )

            pending = _load_json_object(root / G3_PENDING_GATE_PATH, "pending G3 gate")
            target_bindings = {
                "pending_gate_path": G3_PENDING_GATE_PATH.as_posix(),
                "pending_gate_artifact_sha256": str(pending["artifact_sha256"]),
                "technical_record_sha256": str(pending["technical_record_sha256"]),
            }
            reviews = []
            for reviewer in ("gpt-5.6-sol-high", "claude-opus-5-medium"):
                receipt = _load_json_object(
                    root / G3_AI_GATE_INVOCATION_RECEIPT_PATHS[reviewer],
                    f"{reviewer} G3 invocation receipt",
                )
                review = _load_json_object(
                    root / G3_AI_GATE_REVIEW_PATHS[reviewer],
                    f"{reviewer} G3 gate review",
                )
                if review.get("invocation_receipt") != receipt:
                    raise ValueError("G3 review differs from its invocation receipt")
                _validate_native_review(
                    root,
                    anchor_path=G3_AI_GATE_INVOCATION_RECEIPT_PATHS[reviewer],
                    reviewer_id=reviewer,
                    review_kind="g3-gate",
                    target_bindings=target_bindings,
                    response={
                        "verdict": review["verdict"],
                        "blocking_findings": review["blocking_findings"],
                    },
                    receipt=receipt,
                )
                reviews.append(review)
            reviewed = finalize_g3_gate(pending, ai_gate_reviews=reviews)
            saved_reviewed = _load_json_object(
                root / G3_REVIEWED_GATE_PATH, "reviewed G3 gate"
            )
            if saved_reviewed != reviewed:
                raise ValueError("reviewed G3 gate is stale")
            decision = build_g3_kevin_decision(
                pending,
                reviews,
                decision=args.decision,
                selected_policy=args.selected_policy,
                decided_at=args.decided_at,
            )
            final = finalize_g3_gate(
                pending, ai_gate_reviews=reviews, kevin_decision=decision
            )
            _write_json_once_or_verify(root, root / G3_KEVIN_DECISION_PATH, decision)
            _write_json_once_or_verify(root, root / G3_FINAL_GATE_PATH, final)
            print(
                json.dumps(
                    {
                        "status": final["final_decision"],
                        "promoted_memory_policy": final["promoted_memory_policy"],
                        "decision_artifact_sha256": decision["artifact_sha256"],
                        "artifact_sha256": final["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "export-g4-viewer":
            from .viewer_export import export_g4_viewer

            published = export_g4_viewer(root)
            print(json.dumps(published, sort_keys=True))
            return 0
        if args.command == "verify-g4":
            from .g4_gate import run_g4_verification

            verification = run_g4_verification(root)
            print(
                json.dumps(
                    {
                        "status": (
                            "passed"
                            if all(verification["checks"].values())
                            else "failed"
                        ),
                        "viewer_manifest_sha256": verification[
                            "viewer_manifest_sha256"
                        ],
                        "viewer_export_sha256": verification["viewer_export_sha256"],
                        "checks": verification["checks"],
                        "artifact_sha256": verification["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "record-g4-ai-review":
            from .g4_gate import (
                G4_AI_INVOCATION_RECEIPT_PATHS,
                G4_AI_REVIEW_PATHS,
                G4_VERIFICATION_PATH,
                G4_VIEWER_EXPORT_PATH,
                G4_VIEWER_MANIFEST_PATH,
                build_g4_ai_invocation_receipt,
                build_g4_ai_review,
                validate_g4_verification,
            )
            from .review_invocations import run_and_record_ai_review

            canonical_paths = (
                root / G4_VIEWER_MANIFEST_PATH,
                root / G4_VIEWER_EXPORT_PATH,
                root / G4_VERIFICATION_PATH,
            )
            if any(not path.is_file() or path.is_symlink() for path in canonical_paths):
                raise ValueError("canonical G4 review artifacts are missing or unsafe")
            verification = _load_json_object(
                root / G4_VERIFICATION_PATH, "G4 verification"
            )
            validate_g4_verification(verification)
            reviewer_index = 0 if args.reviewer == "gpt-5.6-sol-high" else 1
            anchor_path = G4_AI_INVOCATION_RECEIPT_PATHS[reviewer_index]
            target_bindings = {
                "viewer_manifest_path": G4_VIEWER_MANIFEST_PATH.as_posix(),
                "viewer_manifest_sha256": _sha256_file(root / G4_VIEWER_MANIFEST_PATH),
                "viewer_export_path": G4_VIEWER_EXPORT_PATH.as_posix(),
                "viewer_export_sha256": _sha256_file(root / G4_VIEWER_EXPORT_PATH),
                "g4_verification_path": G4_VERIFICATION_PATH.as_posix(),
                "g4_verification_sha256": str(verification["artifact_sha256"]),
            }
            native = run_and_record_ai_review(
                root,
                anchor_path=anchor_path,
                reviewer_id=args.reviewer,
                review_kind="g4-gate",
                target_bindings=target_bindings,
            )
            response = native["response"]
            evidence = native["evidence"]
            review_fields = {
                "reviewer_id": args.reviewer,
                "viewer_manifest_sha256": target_bindings["viewer_manifest_sha256"],
                "viewer_export_sha256": target_bindings["viewer_export_sha256"],
                "g4_verification_sha256": verification["artifact_sha256"],
                "decision": response["decision"],
                "p0_findings": response["p0_findings"],
                "p1_findings": response["p1_findings"],
                "completed_at": evidence["completed_at"],
            }
            receipt = build_g4_ai_invocation_receipt(
                invocation_id=evidence["native_invocation_id"],
                native_invocation_evidence_sha256=evidence["artifact_sha256"],
                native_output_sha256=evidence["native_output_sha256"],
                **review_fields,
            )
            review = build_g4_ai_review(invocation_receipt=receipt, **review_fields)
            _write_json_once_or_verify(
                root, root / G4_AI_INVOCATION_RECEIPT_PATHS[reviewer_index], receipt
            )
            _write_json_once_or_verify(
                root, root / G4_AI_REVIEW_PATHS[reviewer_index], review
            )
            print(
                json.dumps(
                    {
                        "status": "recorded",
                        "reviewer_id": args.reviewer,
                        "decision": review["decision"],
                        "invocation_receipt_sha256": receipt["artifact_sha256"],
                        "native_invocation_evidence_sha256": evidence[
                            "artifact_sha256"
                        ],
                        "review_artifact_sha256": review["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "gate-g4":
            from .g4_gate import run_g4_gate

            gate = run_g4_gate(root)
            print(
                json.dumps(
                    {
                        "technical_status": gate["technical_status"],
                        "technical_record_sha256": gate["technical_record_sha256"],
                        "human_approval": gate["human_approval"]["status"],
                        "final_decision": gate["final_decision"],
                        "artifact_sha256": gate["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "approve-g4":
            from .g4_gate import approve_g4_gate

            gate = approve_g4_gate(root, approved_at=args.approved_at)
            print(
                json.dumps(
                    {
                        "technical_status": gate["technical_status"],
                        "technical_record_sha256": gate["technical_record_sha256"],
                        "human_approval": gate["human_approval"]["status"],
                        "final_decision": gate["final_decision"],
                        "artifact_sha256": gate["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "freeze-g5":
            from .g5_gate import freeze_g5_technical_record

            technical = freeze_g5_technical_record(
                root,
                proposal_id=args.proposal_id,
                proposal_agent_id=args.proposal_agent_id,
                proposal_artifact_path=args.proposal_artifact,
                sealed_evaluation_path=args.sealed_evaluation_path,
                sealed_evaluation_commitment_sha256=(
                    args.sealed_evaluation_commitment_sha256
                ),
                rollback_artifact_path=args.rollback_artifact,
                budget_limit_usd=args.budget_limit_usd,
                proposed_system_id=args.proposed_system_id,
                simpler_baseline_id=args.simpler_baseline_id,
                simpler_baseline_artifact_path=args.simpler_baseline_artifact,
            )
            print(
                json.dumps(
                    {
                        "status": technical["technical_status"],
                        "technical_evidence_sha256": technical[
                            "technical_evidence_sha256"
                        ],
                        "artifact_sha256": technical["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "approve-g5":
            from .g5_gate import approve_g5_gate

            final = approve_g5_gate(
                root,
                expected_technical_evidence_sha256=(args.technical_evidence_sha256),
                approved_at=args.approved_at,
            )
            print(
                json.dumps(
                    {
                        "status": final["final_decision"],
                        "technical_evidence_sha256": final["technical_evidence_sha256"],
                        "artifact_sha256": final["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "freeze-final-review":
            from .final_review import freeze_final_review

            frozen = freeze_final_review(
                root,
                protected_bundle_path=args.bundle,
                seed_path=args.seed_file,
                staging_directory=args.staging,
                external_identity_map=args.identity_map,
            )
            print(
                json.dumps(
                    {
                        "status": frozen["status"],
                        "task_count": frozen["task_count"],
                        "main_cell_count": frozen["main_cell_count"],
                        "packet_count": len(frozen["packet_manifest"]["packets"]),
                        "artifact_sha256": frozen["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "preflight-final-review":
            from .final_review import build_final_review_preflight

            preflight = build_final_review_preflight(
                root,
                staging_directory=args.staging,
            )
            print(
                json.dumps(
                    {
                        "status": "pending-kevin-confirmation",
                        "packet_count_per_ai": preflight["packet_count_per_ai"],
                        "token_total_per_ai": preflight["token_total_per_ai"],
                        "artifact_sha256": preflight["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "confirm-final-review-preflight":
            from .final_review import confirm_final_review_preflight

            confirmation = confirm_final_review_preflight(
                root,
                staging_directory=args.staging,
                confirmed_at=args.confirmed_at,
            )
            print(
                json.dumps(
                    {
                        "status": "confirmed",
                        "reviewer": confirmation["reviewer"],
                        "confirmed_at": confirmation["confirmed_at"],
                        "artifact_sha256": confirmation["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "release-final-review":
            from .final_review import release_final_review

            released = release_final_review(
                root,
                staging_directory=args.staging,
                release_directory=args.release,
                phase=args.phase,
                calibration_record_path=args.calibration_record,
            )
            print(
                json.dumps(
                    {
                        "status": "released",
                        "phase": released["phase"],
                        "packet_count": released["packet_count"],
                        "manifest_sha256": released["manifest_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "finalize-final-review-calibration":
            from .final_review import finalize_final_review_calibration

            calibration = finalize_final_review_calibration(
                root,
                staging_directory=args.staging,
                release_directory=args.release,
                external_identity_map=args.identity_map,
                external_reference=args.reference,
                returns_by_reviewer={
                    "gpt-5.6-sol-high": args.gpt_return,
                    "claude-opus-5-medium": args.claude_return,
                    "kevin": args.kevin_return,
                },
                output=args.output,
            )
            print(
                json.dumps(
                    {
                        "status": calibration["status"],
                        "cell_count_per_reviewer": calibration[
                            "cell_count_per_reviewer"
                        ],
                        "restart_all_three_reviews": calibration[
                            "restart_all_three_reviews"
                        ],
                        "review_manifest_sha256": calibration["review_manifest_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "import-final-review":
            from .final_review import import_final_review_return

            imported = import_final_review_return(
                root,
                staging_directory=args.staging,
                release_directory=args.release,
                reviewer=args.reviewer,
                return_path=args.input,
                store_path=args.store,
            )
            print(
                json.dumps(
                    {
                        "status": "imported",
                        "reviewer": imported["reviewer"],
                        "grade_count": imported["grade_count"],
                        "artifact_sha256": imported["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "finalize-final-review":
            from .final_review import finalize_final_review

            report = finalize_final_review(
                root,
                staging_directory=args.staging,
                external_identity_map=args.identity_map,
                stores_by_reviewer={
                    "gpt-5.6-sol-high": args.gpt_store,
                    "claude-opus-5-medium": args.claude_store,
                    "kevin": args.kevin_store,
                },
                output=args.output,
            )
            print(
                json.dumps(
                    {
                        "status": "completed",
                        "cell_count": report["cell_count"],
                        "ranking_claims_allowed": report["agreement_gate"][
                            "ranking_claims_allowed"
                        ],
                        "artifact_sha256": report["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "frontier-status":
            from .frontier import build_frontier_status

            print(json.dumps(build_frontier_status(root), sort_keys=True))
            return 0
        if args.command == "freeze-frontier-entry":
            from .frontier import freeze_frontier_entry_gates

            gate = freeze_frontier_entry_gates(root)
            print(
                json.dumps(
                    {
                        "status": gate["final_status"],
                        "technical_record_sha256": gate["technical_record_sha256"],
                        "decisions": {
                            row["experiment_id"]: row["technical_decision"]
                            for row in gate["experiments"]
                        },
                        "artifact_sha256": gate["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "record-frontier-entry-ai-review":
            from .frontier import (
                FRONTIER_ENTRY_AI_INVOCATION_RECEIPT_PATHS,
                FRONTIER_ENTRY_AI_REVIEW_PATHS,
                FRONTIER_ENTRY_EVIDENCE_PATH,
                FRONTIER_ENTRY_GATE_PATH,
                build_frontier_entry_ai_invocation_receipt,
                build_frontier_entry_ai_review,
                build_frontier_entry_gate,
                load_frontier_protocol,
            )
            from .review_invocations import run_and_record_ai_review

            evidence_path = root / FRONTIER_ENTRY_EVIDENCE_PATH
            pending_path = root / FRONTIER_ENTRY_GATE_PATH
            if any(
                not path.is_file() or path.is_symlink()
                for path in (evidence_path, pending_path)
            ):
                raise ValueError(
                    "canonical frontier-entry artifacts are missing or unsafe"
                )
            protocol = load_frontier_protocol(root)
            evidence = _load_json_object(evidence_path, "frontier entry evidence")
            pending = _load_json_object(pending_path, "pending frontier entry gate")
            if pending != build_frontier_entry_gate(protocol, evidence):
                raise ValueError(
                    "pending frontier entry gate differs from exact evidence"
                )
            reviewer_index = 0 if args.reviewer == "gpt-5.6-sol-high" else 1
            anchor_path = FRONTIER_ENTRY_AI_INVOCATION_RECEIPT_PATHS[reviewer_index]
            target_bindings = {
                "frontier_protocol_path": "evaluation/v2/frontier_protocol.json",
                "frontier_protocol_sha256": str(pending["frontier_protocol_sha256"]),
                "frontier_entry_evidence_path": FRONTIER_ENTRY_EVIDENCE_PATH.as_posix(),
                "frontier_entry_evidence_sha256": str(evidence["artifact_sha256"]),
                "pending_gate_path": FRONTIER_ENTRY_GATE_PATH.as_posix(),
                "pending_gate_artifact_sha256": str(pending["artifact_sha256"]),
                "g4_gate_artifact_sha256": str(pending["g4_gate_artifact_sha256"]),
            }
            native = run_and_record_ai_review(
                root,
                anchor_path=anchor_path,
                reviewer_id=args.reviewer,
                review_kind="frontier-entry",
                target_bindings=target_bindings,
            )
            response = native["response"]
            native_evidence = native["evidence"]
            review_fields = {
                "reviewer_id": args.reviewer,
                "pending_gate": pending,
                "evidence": evidence,
                "decision": response["decision"],
                "p0_findings": response["p0_findings"],
                "p1_findings": response["p1_findings"],
                "completed_at": native_evidence["completed_at"],
            }
            receipt = build_frontier_entry_ai_invocation_receipt(
                invocation_id=native_evidence["native_invocation_id"],
                native_invocation_evidence_sha256=native_evidence["artifact_sha256"],
                native_output_sha256=native_evidence["native_output_sha256"],
                **review_fields,
            )
            review = build_frontier_entry_ai_review(
                invocation_receipt=receipt, **review_fields
            )
            _write_json_once_or_verify(
                root,
                root / FRONTIER_ENTRY_AI_INVOCATION_RECEIPT_PATHS[reviewer_index],
                receipt,
            )
            _write_json_once_or_verify(
                root, root / FRONTIER_ENTRY_AI_REVIEW_PATHS[reviewer_index], review
            )
            print(
                json.dumps(
                    {
                        "status": "recorded",
                        "reviewer_id": args.reviewer,
                        "decision": review["decision"],
                        "invocation_receipt_sha256": receipt["artifact_sha256"],
                        "native_invocation_evidence_sha256": native_evidence[
                            "artifact_sha256"
                        ],
                        "review_artifact_sha256": review["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "gate-frontier-entry-ai-reviews":
            from .frontier import (
                FRONTIER_ENTRY_AI_INVOCATION_RECEIPT_PATHS,
                FRONTIER_ENTRY_AI_REVIEW_PATHS,
                FRONTIER_ENTRY_EVIDENCE_PATH,
                FRONTIER_ENTRY_GATE_PATH,
                build_frontier_entry_gate,
                build_frontier_entry_reviewed_gate,
                load_frontier_protocol,
                validate_frontier_entry_ai_review_provenance,
                write_frontier_entry_reviewed_gate,
            )

            protocol = load_frontier_protocol(root)
            evidence = _load_json_object(
                root / FRONTIER_ENTRY_EVIDENCE_PATH, "frontier entry evidence"
            )
            pending = _load_json_object(
                root / FRONTIER_ENTRY_GATE_PATH, "pending frontier entry gate"
            )
            if pending != build_frontier_entry_gate(protocol, evidence):
                raise ValueError(
                    "pending frontier entry gate differs from exact evidence"
                )
            receipts = [
                _load_json_object(root / path, f"frontier-entry receipt {index}")
                for index, path in enumerate(FRONTIER_ENTRY_AI_INVOCATION_RECEIPT_PATHS)
            ]
            reviews = [
                _load_json_object(root / path, f"frontier-entry review {index}")
                for index, path in enumerate(FRONTIER_ENTRY_AI_REVIEW_PATHS)
            ]
            for review, receipt in zip(reviews, receipts, strict=True):
                validate_frontier_entry_ai_review_provenance(
                    root,
                    pending_gate=pending,
                    evidence=evidence,
                    review=review,
                    receipt=receipt,
                )
            reviewed = build_frontier_entry_reviewed_gate(
                protocol,
                evidence,
                pending,
                ai_reviews=reviews,
                ai_invocation_receipts=receipts,
            )
            write_frontier_entry_reviewed_gate(root, gate=reviewed)
            print(
                json.dumps(
                    {
                        "status": reviewed["technical_status"],
                        "human_approval": reviewed["human_approval"]["status"],
                        "final_status": reviewed["final_status"],
                        "technical_record_sha256": reviewed["technical_record_sha256"],
                        "artifact_sha256": reviewed["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "approve-frontier-entry":
            from .frontier import approve_frontier_entry_gate

            approval = approve_frontier_entry_gate(root, approved_at=args.approved_at)
            print(
                json.dumps(
                    {
                        "status": "approved",
                        "reviewer": approval["reviewer"],
                        "technical_record_sha256": approval["technical_record_sha256"],
                        "approval_sha256": approval["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "prepare-frontier-f1":
            from .frontier_f1 import (
                prepare_f1_indexed_memory_lab,
                write_f1_indexed_memory_lab,
            )

            prepared = prepare_f1_indexed_memory_lab(root)
            output = write_f1_indexed_memory_lab(root, prepared)
            print(
                json.dumps(
                    {
                        "status": "prepared_pending_generation",
                        "cell_count": len(prepared["cells"]),
                        "artifact_sha256": prepared["artifact_sha256"],
                        "output": str(output.resolve().relative_to(root)),
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "finalize-frontier-f1":
            from .frontier_f1 import (
                F1_PREPARED_LAB_PATH,
                import_f1_answer_quality_completions,
                write_f1_generated_lab,
            )

            prepared = _load_json_object(
                root / F1_PREPARED_LAB_PATH, "saved F1 prepared lab"
            )
            envelope = _load_json_object(args.input, "F1 completion input")
            completions = _only_list_field(
                envelope, "completion_records", "F1 completion input"
            )
            generated = import_f1_answer_quality_completions(prepared, completions)
            output = write_f1_generated_lab(root, generated)
            print(
                json.dumps(
                    {
                        "status": "generated_pending_result_review",
                        "cell_count": len(generated["cells"]),
                        "completion_count": len(generated["completion_records"]),
                        "artifact_sha256": generated["artifact_sha256"],
                        "output": str(output.resolve().relative_to(root)),
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "prepare-frontier-f2":
            from .frontier import load_approved_frontier_entry_gate
            from .frontier_f2 import F2_FREEZE_PATH, freeze_f2_public_experiment

            gate = load_approved_frontier_entry_gate(root)
            freeze = freeze_f2_public_experiment(root, approved_frontier_gate=gate)
            print(
                json.dumps(
                    {
                        "status": "prepared_not_run",
                        "cell_count": len(freeze["candidate_packet"]["cells"]),
                        "reasoning_efforts": freeze["candidate_packet"][
                            "reasoning_efforts"
                        ],
                        "artifact_sha256": freeze["artifact_sha256"],
                        "output": F2_FREEZE_PATH.as_posix(),
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "finalize-frontier-f2":
            from .frontier import load_approved_frontier_entry_gate
            from .frontier_f2 import (
                F2_FREEZE_PATH,
                import_f2_model_results,
                record_f2_candidate_run,
            )

            gate = load_approved_frontier_entry_gate(root)
            freeze = _load_json_object(root / F2_FREEZE_PATH, "saved F2 freeze")
            envelope = _load_json_object(args.input, "F2 receipt input")
            results = _only_list_field(envelope, "results", "F2 receipt input")
            candidate = import_f2_model_results(freeze, results=results)
            score = record_f2_candidate_run(
                root, approved_frontier_gate=gate, candidate=candidate
            )
            print(
                json.dumps(
                    {
                        "status": "scored_pending_result_review",
                        "succeeded_effort_count": score["aggregate"][
                            "succeeded_effort_count"
                        ],
                        "failed_effort_count": score["aggregate"][
                            "failed_effort_count"
                        ],
                        "billed_cost_usd": score["aggregate"]["billed_cost_usd"],
                        "artifact_sha256": score["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "freeze-frontier-f3-source":
            from .frontier_f3 import (
                build_f3_public_source_manifest,
                write_f3_public_source_manifest,
            )

            source_input = _load_json_object(args.input, "F3 public source input")
            expected = {"page_specs", "source_paths", "corpus_snapshot_id"}
            if set(source_input) != expected:
                raise ValueError("F3 public source input fields must be exact")
            manifest = build_f3_public_source_manifest(root, **source_input)
            commitment = write_f3_public_source_manifest(root, manifest=manifest)
            print(
                json.dumps(
                    {
                        "status": "frozen_pending_frontier_entry_review",
                        "page_count": len(manifest["pages"]),
                        "artifact_sha256": manifest["artifact_sha256"],
                        "approved_source_commitment": commitment,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "prepare-frontier-f3":
            from .frontier_f3 import run_f3_experiment, write_f3_experiment

            experiment_input = _load_json_object(args.input, "F3 preparation input")
            expected = {
                "page_specs",
                "source_manifest",
                "approved_source_commitment",
                "corpus_snapshot_id",
                "task_id",
                "token_budget",
                "instructions_hash",
                "required_evidence_ids",
                "managed_actions",
            }
            if set(experiment_input) != expected:
                raise ValueError(
                    "F3 preparation input fields must match the public protocol"
                )
            prepared = run_f3_experiment(root, **experiment_input)
            saved = write_f3_experiment(root, result=prepared)
            print(
                json.dumps(
                    {
                        "status": "prepared_pending_generation",
                        "strategy_count": len(saved["preparation"]["strategies"]),
                        "cell_count": sum(
                            len(strategy["answer_quality_cells"])
                            for strategy in saved["preparation"]["strategies"]
                        ),
                        "artifact_sha256": saved["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "finalize-frontier-f3":
            from .frontier_f3 import F3_RESULT_PATH, finalize_f3_experiment

            prepared = _load_json_object(
                root / F3_RESULT_PATH, "saved F3 prepared result"
            )
            envelope = _load_json_object(args.input, "F3 result input")
            if set(envelope) == {"cell_results"}:
                cell_results = _only_list_field(
                    envelope, "cell_results", "F3 result input"
                )
            elif set(envelope) == {
                "cell_results",
                "provider_call_count",
                "overflow_failure_count",
            }:
                cell_results = envelope["cell_results"]
                counts = (
                    envelope["provider_call_count"],
                    envelope["overflow_failure_count"],
                )
                if (
                    not isinstance(cell_results, list)
                    or any(not isinstance(row, dict) for row in cell_results)
                    or any(
                        isinstance(count, bool)
                        or not isinstance(count, int)
                        or count < 0
                        for count in counts
                    )
                ):
                    raise ValueError("F3 result input execution envelope is invalid")
            else:
                raise ValueError(
                    "F3 result input must contain cell_results and optional execution counts"
                )
            final = finalize_f3_experiment(
                root,
                prepared_result=prepared,
                cell_results=cell_results,
            )
            print(
                json.dumps(
                    {
                        "status": final["status"],
                        "cell_count": len(final["cells"]),
                        "artifact_sha256": final["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "run-frontier-f3":
            from .frontier_f3 import (
                F3_COMPLETION_INPUT_PATH,
                F3_RESULT_PATH,
                execute_f3_experiment,
            )

            prepared = _load_json_object(
                root / F3_RESULT_PATH, "saved F3 prepared result"
            )
            completion = execute_f3_experiment(root, prepared_result=prepared)
            print(
                json.dumps(
                    {
                        "status": "completed_pending_finalization",
                        "cell_count": len(completion["cell_results"]),
                        "provider_call_count": completion["provider_call_count"],
                        "overflow_failure_count": completion[
                            "overflow_failure_count"
                        ],
                        "output": F3_COMPLETION_INPUT_PATH.as_posix(),
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "run-frontier-f5":
            from .frontier_f5 import F5_RESULT_PATH, run_f5_experiment

            result = run_f5_experiment(root)
            print(
                json.dumps(
                    {
                        "status": result["status"],
                        "task_ids": result["task_ids"],
                        "search_cell_count": len(result["search_cells"]),
                        "aggregate": result["aggregate"],
                        "artifact_sha256": result["artifact_sha256"],
                        "output": F5_RESULT_PATH.as_posix(),
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "freeze-frontier-result-review":
            from .frontier_review import freeze_frontier_result_technical_record

            request = _load_json_object(args.input, "frontier result review request")
            if set(request) != {"experiment_id", "proposed_decision", "claim"}:
                raise ValueError("frontier result review request fields must be exact")
            technical = freeze_frontier_result_technical_record(
                root,
                experiment_id=request["experiment_id"],
                proposed_decision=request["proposed_decision"],
                claim=request["claim"],
            )
            print(
                json.dumps(
                    {
                        "status": "frozen",
                        "experiment_id": technical["experiment_id"],
                        "technical_record_sha256": technical["technical_record_sha256"],
                        "artifact_sha256": technical["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "record-frontier-ai-review":
            from .frontier import FRONTIER_ENTRY_APPROVED_GATE_PATH
            from .frontier_review import (
                build_frontier_result_invocation_receipt,
                build_frontier_result_ai_review,
                frontier_result_review_directory,
                write_frontier_result_ai_review,
            )
            from .review_invocations import run_and_record_ai_review

            review_root_relative = (
                Path("results/v2/frontier")
                / args.experiment.casefold()
                / frontier_result_review_directory(args.experiment)
            )
            review_root = root / review_root_relative
            technical = _load_json_object(
                review_root / "technical.json", "frontier technical result"
            )
            anchor_path = review_root_relative / f"{args.reviewer}.json"
            target_bindings = {
                "experiment_id": str(technical["experiment_id"]),
                "frontier_entry_gate_path": (
                    FRONTIER_ENTRY_APPROVED_GATE_PATH.as_posix()
                ),
                "technical_record_path": (
                    review_root_relative / "technical.json"
                ).as_posix(),
                "technical_record_sha256": str(technical["technical_record_sha256"]),
                "technical_artifact_sha256": str(technical["artifact_sha256"]),
                "frontier_entry_gate_sha256": str(
                    technical["frontier_entry_gate_sha256"]
                ),
                "source_artifacts_sha256": str(technical["source_artifacts_sha256"]),
            }
            native = run_and_record_ai_review(
                root,
                anchor_path=anchor_path,
                reviewer_id=args.reviewer,
                review_kind="frontier-result",
                target_bindings=target_bindings,
            )
            response = native["response"]
            evidence = native["evidence"]
            receipt = build_frontier_result_invocation_receipt(
                technical,
                reviewer_id=args.reviewer,
                invocation_id=evidence["native_invocation_id"],
                decision=response["decision"],
                findings=response["findings"],
                completed_at=evidence["completed_at"],
                native_invocation_evidence_sha256=evidence["artifact_sha256"],
                native_output_sha256=evidence["native_output_sha256"],
                usage=evidence["usage"],
            )
            review = build_frontier_result_ai_review(
                technical,
                reviewer_id=args.reviewer,
                invocation_receipt=receipt,
                decision=response["decision"],
                findings=response["findings"],
                completed_at=evidence["completed_at"],
            )
            write_frontier_result_ai_review(root, review)
            print(
                json.dumps(
                    {
                        "status": "recorded",
                        "experiment_id": review["experiment_id"],
                        "reviewer_id": review["reviewer_id"],
                        "decision": review["decision"],
                        "native_invocation_evidence_sha256": evidence[
                            "artifact_sha256"
                        ],
                        "artifact_sha256": review["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "gate-frontier-result":
            from .frontier_review import (
                build_frontier_result_pending_gate,
                frontier_result_review_directory,
                write_frontier_result_pending_gate,
            )

            review_root = (
                root
                / "results/v2/frontier"
                / args.experiment.casefold()
                / frontier_result_review_directory(args.experiment)
            )
            technical = _load_json_object(
                review_root / "technical.json", "frontier technical result"
            )
            second_reviewers = [
                reviewer
                for reviewer in ("claude-opus-5-medium", "gpt-5.6-terra-high")
                if (review_root / f"{reviewer}.json").is_file()
            ]
            if len(second_reviewers) != 1:
                raise ValueError(
                    "frontier result needs exactly one Claude review or Terra fallback"
                )
            reviewer_ids = ("gpt-5.6-sol-high", second_reviewers[0])
            reviews = [
                _load_json_object(
                    review_root / f"{reviewer}.json",
                    f"{reviewer} frontier result review",
                )
                for reviewer in reviewer_ids
            ]
            gate = build_frontier_result_pending_gate(technical, reviews)
            write_frontier_result_pending_gate(root, gate)
            print(
                json.dumps(
                    {
                        "status": gate["technical_status"],
                        "experiment_id": gate["experiment_id"],
                        "final_status": gate["final_status"],
                        "artifact_sha256": gate["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "approve-frontier-result":
            from .frontier_review import approve_frontier_result

            final = approve_frontier_result(
                root,
                experiment_id=args.experiment,
                approved_at=args.approved_at,
            )
            print(
                json.dumps(
                    {
                        "status": final["final_status"],
                        "experiment_id": final["experiment_id"],
                        "artifact_sha256": final["artifact_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "sealed-import":
            from .sealed import import_sealed_return

            imported = import_sealed_return(
                args.input,
                args.output,
                candidate_manifest_path=args.candidate_manifest,
                expected_external_bundle_sha256=args.external_bundle_sha256,
                root=root,
            )
            print(
                json.dumps(
                    {
                        "status": "imported",
                        "evaluation_id": imported["evaluation_id"],
                        "record_count": len(imported["records"]),
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "cost-quote":
            quote = CostLedger(canonical_ledger_path(root)).quote(
                input_tokens=args.input_tokens,
                output_tokens=args.output_tokens,
                calls=args.calls,
            )
            print(json.dumps(quote, sort_keys=True))
            return 0
        if args.command == "paid-generate":
            from .gateway import run_paid_generation_to_file

            spec = json.loads(args.spec.read_text(encoding="utf-8"))
            result = run_paid_generation_to_file(
                spec,
                args.output,
                ledger=CostLedger(canonical_ledger_path(root)),
                root=root,
            )
            print(
                json.dumps(
                    {"status": "saved", "run_id": result["run_id"]}, sort_keys=True
                )
            )
            return 0
        if args.command == "paid-enrich":
            from .gateway import refresh_generation_metadata

            result = refresh_generation_metadata(
                args.result,
                ledger=CostLedger(canonical_ledger_path(root)),
                root=root,
            )
            print(
                json.dumps(
                    {"status": "enriched", "run_id": result["run_id"]}, sort_keys=True
                )
            )
            return 0
    except (BaselineError, ProtectedDataError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2
