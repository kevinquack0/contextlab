"""Evidence-producing acceptance checks for ContextLab v2 program gates."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

from .adapters import (
    V1ReplayAdapter,
    verify_v1_adapter_equivalence,
    verify_v1_executable_sample,
)
from .baseline import default_manifest_path, repository_root, verify_manifest
from .boundaries import CorpusBoundary, ProtectedDataError
from .contracts import verify_contract_artifacts
from .costs import CostLedger, canonical_ledger_path
from .credentials import keychain_entry_present, redact, require_runtime_credential
from .grading import DeterministicAnswerSpec, check_answer
from .gateway import (
    KEY_STATUS_URL,
    preflight_live_provider,
    run_paid_generation,
    validate_key_status,
)
from .provider import build_generation_request, validate_provider_snapshot
from .immutable_io import (
    ImmutableIOError,
    read_bytes_snapshot,
    write_json_once_or_verify,
)
from .review import (
    REASONING_EFFORTS,
    REVIEW_RUBRIC_SHA256,
    STRATEGY_LANES,
    ReviewContractError,
    build_review_packets,
    evaluate_calibration,
    release_calibration_packets,
    release_review_packets,
    validate_review_protocol,
    validate_token_preflight,
)
from .sealed import SealedImportError, import_sealed_return
from .tasking import (
    build_split_manifest,
    sha256_json,
    task_catalog,
    validate_g1_task_drafts,
    validate_split_manifest,
)
from .traces import validate_trace_record
from .truth_audit import audit_truth_language


class GateError(RuntimeError):
    """A gate acceptance item failed."""


G0_TECHNICAL_SCHEMA = "contextlab.g0-technical-gate.v2"
G0_APPROVAL_SCHEMA = "contextlab.g0-kevin-approval.v1"
G0_TECHNICAL_PATH = Path("results/v2/gates/G0.json")
G0_APPROVAL_PATH = Path("results/v2/gates/G0.approval.json")
G1_TECHNICAL_SCHEMA = "contextlab.g1-technical-gate.v2"
G1_APPROVAL_SCHEMA = "contextlab.g1-kevin-approval.v1"
G1_TECHNICAL_PATH = Path("results/v2/gates/G1.json")
G1_APPROVAL_PATH = Path("results/v2/gates/G1.approval.json")

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _legacy_validate(root: Path) -> str:
    result = subprocess.run(
        [sys.executable, "evaluation/harness.py", "validate"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise GateError(f"v1 structural validation failed: {output}")
    return output


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _deterministic_check_evidence(root: Path) -> dict[str, Any]:
    fixture = json.loads(
        (root / "evaluation/v2/fixtures/deterministic_answer_checks.json").read_text(
            encoding="utf-8"
        )
    )
    results: dict[str, Any] = {}
    for name in ("known_good", "broken"):
        row = fixture[name]
        spec = DeterministicAnswerSpec(
            **{key: tuple(value) for key, value in row["spec"].items()}
        )
        results[name] = check_answer(row["answer"], spec, repository=root)
    if not results["known_good"]["passed"] or results["broken"]["passed"]:
        raise GateError("deterministic known-good/broken acceptance fixture failed")
    return {"known_good": "passed", "deliberately_broken": "failed_as_expected"}


def _provider_and_cost_evidence(root: Path) -> dict[str, Any]:
    snapshot = validate_provider_snapshot(root)
    live_route = preflight_live_provider()
    for effort in ("low", "high"):
        build_generation_request(
            [{"role": "user", "content": "G1 request-shape fixture"}],
            effort=effort,
            max_tokens=64,
        )
    require_runtime_credential(
        {"OPENROUTER_API_KEY": "sk-or-v1-fixture-runtime-credential"}
    )
    if not keychain_entry_present():
        raise GateError("macOS Keychain item contextlab-openrouter is missing")
    with tempfile.TemporaryDirectory() as directory:
        ledger = CostLedger(Path(directory) / "ledger.jsonl")
        warning = ledger.reserve(
            "g1-warning-fixture",
            input_tokens=70_000_000,
            output_tokens=10_000_000,
        )
        if not warning["informational_warning"]:
            raise GateError("US$12 warning guard did not activate")
        unblocked = ledger.reserve(
            "g1-external-limit-fixture",
            input_tokens=20_000_000,
            output_tokens=1,
        )
        if unblocked["projected_within_external_key_limit"]:
            raise GateError(
                "diagnostic ledger fixture did not cross the external key limit"
            )
        gateway_root = Path(directory) / "gateway-root"
        gateway_ledger = CostLedger(gateway_root / "results/v2/cost/paid_calls.jsonl")
        question = "Reply with OK."
        spec = {
            "schema_version": "contextlab.generation-spec.v1",
            "run_id": "g1-fixed-gateway-fixture",
            "task": {
                "schema_version": "contextlab.prompt-task.v1",
                "task_id": "S001",
                "suite": "static",
                "question_text": question,
                "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
            },
            "system_instruction": "Use only the supplied evidence.",
            "rendered_context": "The fixture answer is OK.",
            "reasoning_effort": "low",
            "max_tokens": 8,
            "temperature": 0.0,
        }

        def fixture_endpoints(_: str) -> dict[str, Any]:
            return {
                "data": {
                    "id": snapshot["model_id"],
                    "endpoints": [
                        {
                            "provider_name": "DeepSeek",
                            "status": 0,
                            "pricing": {
                                "prompt": "0.00000014",
                                "completion": "0.00000028",
                            },
                            "supported_parameters": ["reasoning_effort"],
                        }
                    ],
                }
            }

        fixture_post_calls = 0

        def fixture_post(_: str, __: dict[str, Any], credential: str) -> dict[str, Any]:
            nonlocal fixture_post_calls
            fixture_post_calls += 1
            if not credential.startswith("sk-or-v1-"):
                raise GateError("fixed gateway did not receive a runtime credential")
            return {
                "id": "gen-g1-fixture",
                "model": snapshot["model_id"],
                "provider": "DeepSeek",
                "choices": [{"message": {"content": "OK"}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 1,
                    "cost": 0.00000168,
                },
            }

        def fixture_authorized(url: str, __: str) -> dict[str, Any]:
            if url == KEY_STATUS_URL:
                return {
                    "data": {
                        "limit": 15,
                        "limit_remaining": 15,
                        "limit_reset": None,
                        "usage": 0,
                    }
                }
            return {
                "data": {
                    "id": "gen-g1-fixture",
                    "model": snapshot["model_id"],
                    "provider_name": "DeepSeek",
                    "latency": 10,
                    "generation_time": 5,
                    "native_tokens_prompt": 10,
                    "native_tokens_completion": 1,
                    "native_tokens_reasoning": 0,
                    "total_cost": 0.00000168,
                }
            }

        gateway_result = run_paid_generation(
            spec,
            ledger=gateway_ledger,
            environment={"OPENROUTER_API_KEY": "sk-or-v1-fixture-runtime-credential"},
            get_json=fixture_endpoints,
            post_json=fixture_post,
            get_authorized_json=fixture_authorized,
            root=gateway_root,
        )
        gateway_ledger_events = len(gateway_ledger.path.read_text().splitlines())
        if fixture_post_calls != 1:
            raise GateError(
                "fixed gateway fixture did not make exactly one injected call"
            )
    key_snapshot_path = root / "results/v2/provider/openrouter_key_limit_snapshot.json"
    key_snapshot = json.loads(key_snapshot_path.read_text(encoding="utf-8"))
    key_status = validate_key_status(
        {
            "data": {
                "limit": key_snapshot.get("limit_usd"),
                "limit_remaining": key_snapshot.get("remaining_usd"),
                "limit_reset": key_snapshot.get("limit_reset"),
                "usage": key_snapshot.get("usage_usd"),
            }
        }
    )
    paid_ledger = CostLedger(canonical_ledger_path(root))
    paid_summary = paid_ledger.summary()
    successful_smoke_path = (
        root / "results/v2/provider/openrouter_smoke_20260805_1950.json"
    )
    failed_smoke_path = (
        root / "results/v2/provider/openrouter_smoke_20260805_1941_failed.json"
    )
    successful_smoke = json.loads(successful_smoke_path.read_text(encoding="utf-8"))
    failed_smoke = json.loads(failed_smoke_path.read_text(encoding="utf-8"))
    try:
        g1_smoke_cost = sum(
            (
                Decimal(str(record.get("metadata", {}).get("actual_usd")))
                for record in (successful_smoke, failed_smoke)
            ),
            Decimal("0"),
        )
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise GateError("G1 smoke costs are invalid") from exc
    saved_key_usage = Decimal(str(key_status["usage_usd"]))
    if g1_smoke_cost != saved_key_usage:
        raise GateError("G1 smoke costs differ from the saved key-usage snapshot")
    if Decimal(str(paid_summary["actual_usd"])) < saved_key_usage:
        raise GateError("canonical ledger no longer contains the G1 paid evidence")
    call_fields = {
        "requested_model",
        "resolved_model",
        "provider",
        "reasoning_effort",
        "request_id",
        "prompt_tokens",
        "completion_tokens",
        "actual_usd",
        "latency_ms",
        "retry_count",
        "error",
    }
    for label, record in (("successful", successful_smoke), ("failed", failed_smoke)):
        metadata = record.get("metadata")
        if not isinstance(metadata, dict) or not call_fields.issubset(metadata):
            raise GateError(f"{label} smoke evidence omits required paid-call fields")
    validate_provider_snapshot(root)
    if successful_smoke["metadata"]["provider"] != "DeepSeek":
        raise GateError("successful smoke did not retain the exact DeepSeek provider")
    if failed_smoke.get("status") != "failed_evidence_capture" or not failed_smoke.get(
        "error"
    ):
        raise GateError("initial smoke evidence loss is not preserved as a failure")
    redacted = json.dumps(redact({"key": "sk-or-v1-fixture-runtime-credential"}))
    if "sk-or-v1-fixture-runtime-credential" in redacted:
        raise GateError("credential redaction failed")
    return {
        "model": snapshot["model_id"],
        "provider_route": snapshot["provider_route"],
        "live_provider_price": live_route,
        "reasoning_efforts": snapshot["experiment_reasoning_efforts"],
        "credential_runtime_guard": "passed",
        "credential_keychain_entry": "present",
        "redaction": "passed",
        "informational_warning_usd": "12.00",
        "external_key_limit": key_status,
        "external_key_limit_snapshot": str(key_snapshot_path.relative_to(root)),
        "g1_smoke_cost_usd": str(g1_smoke_cost),
        "canonical_paid_ledger": paid_summary,
        "authorized_smoke_evidence": {
            "successful": str(successful_smoke_path.relative_to(root)),
            "failed_evidence_capture": str(failed_smoke_path.relative_to(root)),
        },
        "fixed_gateway_fixture": {
            "answer": gateway_result["answer"],
            "latency_ms": gateway_result["metadata"]["latency_ms"],
            "ledger_events": gateway_ledger_events,
            "injected_transport_calls": fixture_post_calls,
            "network_transport": "disabled_by_injection",
        },
    }


def _sealed_fixture_evidence(root: Path) -> dict[str, Any]:
    fixture_dir = root / "evaluation/v2/fixtures"
    allowed_fixture = json.loads(
        (fixture_dir / "sealed_return_allowed.json").read_text(encoding="utf-8")
    )
    expected_bundle_hash = str(allowed_fixture["external_bundle_sha256"])
    output = root / "results/v2/sealed/g1_fixture_import.json"
    with tempfile.TemporaryDirectory() as directory:
        allowed_external = Path(directory) / "allowed.json"
        forbidden_external = Path(directory) / "forbidden.json"
        shutil.copyfile(fixture_dir / "sealed_return_allowed.json", allowed_external)
        shutil.copyfile(
            fixture_dir / "sealed_return_forbidden.json", forbidden_external
        )
        imported = import_sealed_return(
            allowed_external,
            output,
            candidate_manifest_path=fixture_dir / "sealed_candidate_manifest.json",
            expected_external_bundle_sha256=expected_bundle_hash,
            root=root,
        )
        try:
            import_sealed_return(
                forbidden_external,
                output,
                candidate_manifest_path=fixture_dir / "sealed_candidate_manifest.json",
                expected_external_bundle_sha256=expected_bundle_hash,
                root=root,
            )
        except SealedImportError:
            pass
        else:
            raise GateError("sealed importer accepted a raw expected answer")
        wrong_hash = "0" * 64
        try:
            import_sealed_return(
                allowed_external,
                output,
                candidate_manifest_path=fixture_dir / "sealed_candidate_manifest.json",
                expected_external_bundle_sha256=wrong_hash,
                root=root,
            )
        except SealedImportError:
            pass
        else:
            raise GateError("sealed importer accepted a different external bundle hash")
        boolean_ordinal = json.loads(allowed_external.read_text(encoding="utf-8"))
        boolean_ordinal["records"][0]["grades"]["ordinal"] = True
        boolean_external = Path(directory) / "boolean-ordinal.json"
        boolean_external.write_text(json.dumps(boolean_ordinal), encoding="utf-8")
        try:
            import_sealed_return(
                boolean_external,
                output,
                candidate_manifest_path=fixture_dir / "sealed_candidate_manifest.json",
                expected_external_bundle_sha256=expected_bundle_hash,
                root=root,
            )
        except SealedImportError:
            pass
        else:
            raise GateError("sealed importer accepted a boolean ordinal")
    return {
        "allowed_fixture_records": len(imported["records"]),
        "forbidden_gold_fixture": "rejected",
        "external_bundle_hash_mismatch": "rejected",
        "boolean_ordinal_fixture": "rejected",
        "import": str(output.relative_to(root)),
        "import_sha256": _sha256_file(output),
    }


def _review_packet_evidence(root: Path) -> dict[str, Any]:
    protocol = validate_review_protocol(root / "evaluation/v2/review_protocol.json")
    split = json.loads(
        (root / "results/v2/splits/task_split_manifest.json").read_text()
    )
    sealed_ids = {
        row["task_id"]
        for row in split["tasks"]
        if row["partition"] == "sealed_capability"
    }
    task_ids = [f"S{index:03d}" for index in range(1, 121)] + [
        f"T{index:03d}" for index in range(1, 41)
    ]

    def cell(cell_id: str, task_id: str, strategy: str, effort: str) -> dict[str, Any]:
        answer = (
            "G1 packet-contract fixture "
            + hashlib.sha256(cell_id.encode("utf-8")).hexdigest()[:12]
        )
        return {
            "cell_id": cell_id,
            "task_id": task_id,
            "question": "G1 fixture question "
            + hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:12],
            "candidate_answer": answer,
            "cited_evidence": [{"reference": "NL-003#NL-003-S02", "text": "fixture"}],
            "candidate_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
            "strategy_id": strategy,
            "reasoning_effort": effort,
        }

    main = [
        cell(f"{task_id}-{strategy}-{effort}", task_id, strategy, effort)
        for task_id in task_ids
        for strategy in STRATEGY_LANES
        for effort in REASONING_EFFORTS
    ]
    calibration = [
        cell(f"cal-{index:02d}", f"C{index:03d}", "full_context", "low")
        for index in range(1, 21)
    ]
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        leaked_main = [dict(row) for row in main]
        leaked_answer = "identity leak: strategy_id=full_context; reasoning_effort=low"
        leaked_main[0] = {
            **leaked_main[0],
            "candidate_answer": leaked_answer,
            "candidate_sha256": hashlib.sha256(
                leaked_answer.encode("utf-8")
            ).hexdigest(),
        }
        try:
            build_review_packets(
                leaked_main,
                calibration,
                seed=b"contextlab-g1-leak-rejection-seed-32-bytes",
                staging_directory=base / "leak-staging",
                external_identity_map=base / "leak-private" / "identity.json",
                sealed_task_ids=sealed_ids,
            )
        except ReviewContractError:
            pass
        else:
            raise GateError(
                "review packet builder accepted strategy and effort content leakage"
            )
        citation_leak = [dict(row) for row in main]
        citation_leak[0] = {
            **citation_leak[0],
            "cited_evidence": [{"expected_answer": "protected truth"}],
        }
        try:
            build_review_packets(
                citation_leak,
                calibration,
                seed=b"contextlab-g1-citation-leak-seed-32-bytes",
                staging_directory=base / "citation-leak-staging",
                external_identity_map=base / "citation-leak-private" / "identity.json",
                sealed_task_ids=sealed_ids,
            )
        except ReviewContractError:
            pass
        else:
            raise GateError("review packet builder accepted protected citation fields")
        task_leak = [dict(row) for row in main]
        task_leak[0] = {**task_leak[0], "question": "G1 fixture question for S001"}
        try:
            build_review_packets(
                task_leak,
                calibration,
                seed=b"contextlab-g1-task-leak-seed-32-bytes-min",
                staging_directory=base / "task-leak-staging",
                external_identity_map=base / "task-leak-private" / "identity.json",
                sealed_task_ids=sealed_ids,
            )
        except ReviewContractError:
            pass
        else:
            raise GateError("review packet builder accepted a public task ID")
        manifest = build_review_packets(
            main,
            calibration,
            seed=b"contextlab-g1-packet-contract-seed-32-bytes-minimum",
            staging_directory=base / "staging",
            external_identity_map=base / "private" / "identity.json",
            sealed_task_ids=sealed_ids,
        )
        token_preflights = {
            reviewer: validate_token_preflight(
                manifest,
                reviewer,
                {
                    row["packet_id"]: len(
                        (base / "staging" / row["path"])
                        .read_text(encoding="utf-8")
                        .split()
                    )
                    for row in manifest["packets"]
                    if row["reviewer"] == reviewer
                },
                tokenizer_id="g1-whitespace-contract-tokenizer-v1",
                confirmed_by="Kevin Araujo",
            )
            for reviewer in manifest["reviewers"][:2]
        }
        calibration_release = release_calibration_packets(
            base / "staging",
            base / "calibration-release",
            manifest,
            token_preflights,
        )
        reference_path = base / "private" / "calibration-reference.json"
        reference_path.write_text(
            json.dumps(
                {
                    "schema_version": "contextlab.calibration-reference.v1",
                    "targets": [
                        {
                            "canonical_cell_id": row["cell_id"],
                            "overall_ordinal": 3,
                            "accepted": True,
                        }
                        for row in calibration
                    ],
                }
            ),
            encoding="utf-8",
        )
        identity_path = base / "private" / "identity.json"
        identities = json.loads(identity_path.read_text())["identities"]
        grade = {
            "overall_ordinal": 3,
            "factual_correctness": 3,
            "completeness": 3,
            "citation_support": 3,
            "authority_freshness": 3,
            "abstention_quality": "not_applicable",
            "accepted": True,
            "failure_labels": [],
            "comment": "G1 calibration fixture",
        }
        calibration_grades = {
            reviewer: {
                row["blind_cell_id"]: grade
                for row in identities
                if row["reviewer"] == reviewer and row["phase"] == "calibration"
            }
            for reviewer in manifest["reviewers"]
        }
        calibration_record = evaluate_calibration(
            calibration_grades,
            identity_map_path=identity_path,
            external_reference_path=reference_path,
            review_manifest=manifest,
            rubric_ambiguity_by_reviewer={
                reviewer: False for reviewer in manifest["reviewers"]
            },
        )
        review_release = release_review_packets(
            base / "staging",
            base / "review-release",
            manifest,
            calibration_record,
            token_preflights,
        )
    return {
        **protocol,
        "generated_packet_count": len(manifest["packets"]),
        "generated_manifest_sha256": manifest["manifest_sha256"],
        "unique_cells_per_reviewer": manifest["unique_cells_per_reviewer"],
        "hidden_repeats_per_reviewer": manifest["hidden_repeats_per_reviewer"],
        "calibration_cells_per_reviewer": manifest["calibration_cells_per_reviewer"],
        "calibration_gate": calibration_record["status"],
        "calibration_release_packets": calibration_release["packet_count"],
        "review_release_packets": review_release["packet_count"],
        "content_identity_leak_fixture": "rejected",
        "protected_citation_leak_fixture": "rejected",
        "task_identity_leak_fixture": "rejected",
        "ai_token_preflight_contracts": len(token_preflights),
    }


def _trace_evidence(root: Path) -> dict[str, Any]:
    json_path = root / "results/v2/traces/v1_static_trace_mock.json"
    html_path = root / "results/v2/traces/v1_static_trace_mock.html"
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    if payload.get("run_count") != 160 or payload.get("read_only") is not True:
        raise GateError(
            "static trace mock is not a read-only conversion of 160 v1 runs"
        )
    for run in payload.get("runs", []):
        validate_trace_record(run)
    serialized = json.dumps(payload)
    if any(
        field in serialized
        for field in ("expected_answer", "gold_answer", "scoring_notes")
    ):
        raise GateError("static trace mock leaked protected truth")
    return {
        "runs": 160,
        "json": str(json_path.relative_to(root)),
        "json_sha256": _sha256_file(json_path),
        "html": str(html_path.relative_to(root)),
        "html_sha256": _sha256_file(html_path),
    }


def _secret_scan(root: Path) -> dict[str, Any]:
    exposed_pattern = re.compile(rb"sk-or-v1-[A-Za-z0-9_-]{48,}")
    findings: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        try:
            with path.open("rb") as handle:
                overlap = b""
                while block := handle.read(1024 * 1024):
                    data = overlap + block
                    if exposed_pattern.search(data):
                        findings.append(str(path.relative_to(root)))
                        break
                    overlap = data[-128:]
        except OSError as exc:
            raise GateError(
                f"secret scan cannot read {path.relative_to(root)}"
            ) from exc
    git_pattern = r"sk-or-v1-[A-Za-z0-9_-]{48,}"
    index_result = subprocess.run(
        ["git", "grep", "--cached", "-I", "-l", "-E", git_pattern],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if index_result.returncode not in {0, 1}:
        raise GateError("secret scan could not inspect the Git index")
    findings.extend(
        f"git-index:{line}" for line in index_result.stdout.splitlines() if line
    )

    object_listing = subprocess.run(
        [
            "git",
            "cat-file",
            "--batch-check=%(objectname) %(objecttype)",
            "--batch-all-objects",
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if object_listing.returncode != 0:
        raise GateError("secret scan could not enumerate all Git objects")
    object_ids: list[str] = []
    for line in object_listing.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or re.fullmatch(r"[0-9a-f]{40,64}", fields[0]) is None:
            raise GateError("secret scan received an invalid Git object listing")
        object_ids.append(fields[0])

    process = subprocess.Popen(
        ["git", "cat-file", "--batch"],
        cwd=root,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        if process.stdin is None or process.stdout is None:
            raise GateError("secret scan could not open the Git object stream")
        for object_id in object_ids:
            process.stdin.write(f"{object_id}\n".encode("ascii"))
            process.stdin.flush()
            header = process.stdout.readline().rstrip(b"\n").split()
            if (
                len(header) != 3
                or header[0].decode("ascii", errors="replace") != object_id
                or not header[2].isdigit()
            ):
                raise GateError("secret scan received an invalid Git object header")
            remaining = int(header[2])
            overlap = b""
            exposed = False
            while remaining:
                block = process.stdout.read(min(1024 * 1024, remaining))
                if not block:
                    raise GateError("secret scan received a truncated Git object")
                remaining -= len(block)
                data = overlap + block
                exposed = exposed or exposed_pattern.search(data) is not None
                overlap = data[-128:]
            if process.stdout.read(1) != b"\n":
                raise GateError("secret scan received an invalid Git object terminator")
            if exposed:
                findings.append(f"git-object:{object_id}")
        process.stdin.close()
        if process.wait() != 0:
            raise GateError("secret scan could not inspect all Git objects")
    finally:
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        if process.poll() is None:
            process.terminate()
            process.wait()
        if process.stdout is not None:
            process.stdout.close()
    if findings:
        raise GateError(f"possible OpenRouter credentials found: {findings}")
    return {"status": "passed", "findings": 0}


def _g0_protected_boundary(root: Path) -> None:
    corpus = root / "novalearn_synthetic_corpus" / "corpus"
    protected = root / "novalearn_synthetic_corpus" / "evaluation_only_do_not_index"
    boundary = CorpusBoundary(corpus, (protected,))
    if not boundary.discover(("*.md",)):
        raise GateError("approved corpus contains no Markdown documents")
    try:
        boundary.load_text(protected / "canonical_fact_ledger.md")
    except ProtectedDataError:
        return
    raise GateError("protected data was loadable through the corpus boundary")


def build_g0_technical_record(root: Path | None = None) -> dict[str, Any]:
    """Rebuild deterministic G0 evidence without carrying any prior approval."""

    repository = (root or repository_root()).resolve()
    manifest_path = default_manifest_path(repository)
    manifest = verify_manifest(manifest_path, repository)
    findings = audit_truth_language(repository)
    if findings:
        raise GateError("truth-language audit failed:\n" + "\n".join(findings))
    _g0_protected_boundary(repository)
    evidence = {
        "baseline_manifest": str(manifest_path.relative_to(repository)),
        "baseline_manifest_sha256": _sha256_file(manifest_path),
        "snapshots": len(manifest["snapshots"]),
        "snapshot_files": sum(row["file_count"] for row in manifest["snapshots"]),
        "saved_fingerprints_reproduced": len(manifest["fingerprint_checks"]),
        "truth_language_audit": "passed",
        "protected_boundary": "passed",
        "v1_structural_validation": _legacy_validate(repository),
    }
    record: dict[str, Any] = {
        "schema_version": G0_TECHNICAL_SCHEMA,
        "gate": "G0",
        "technical_status": "passed",
        "technical_evidence_sha256": sha256_json(evidence),
        "evidence": evidence,
    }
    record["artifact_sha256"] = sha256_json(record)
    validate_g0_technical_record(record)
    return record


def validate_g0_technical_record(value: Any) -> None:
    expected = {
        "schema_version",
        "gate",
        "technical_status",
        "technical_evidence_sha256",
        "evidence",
        "artifact_sha256",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise GateError("G0 technical record fields changed")
    evidence = value.get("evidence")
    if (
        value.get("schema_version") != G0_TECHNICAL_SCHEMA
        or value.get("gate") != "G0"
        or value.get("technical_status") != "passed"
        or not isinstance(evidence, dict)
        or value.get("technical_evidence_sha256") != sha256_json(evidence)
        or value.get("artifact_sha256")
        != sha256_json(
            {key: item for key, item in value.items() if key != "artifact_sha256"}
        )
    ):
        raise GateError("G0 technical record is invalid")


def _read_g0_json(root: Path, path: Path, label: str) -> dict[str, Any]:
    target = path if path.is_absolute() else root / path
    try:
        value = json.loads(read_bytes_snapshot(root, target).decode("utf-8"))
    except (ImmutableIOError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateError(f"cannot read {label}") from exc
    if not isinstance(value, dict):
        raise GateError(f"{label} must be an object")
    return value


def run_g0_gate(root: Path | None = None, output: Path | None = None) -> dict[str, Any]:
    """Persist the deterministic G0 technical record once, without approval state."""

    repository = (root or repository_root()).resolve()
    target = output or repository / G0_TECHNICAL_PATH
    target = target if target.is_absolute() else repository / target
    record = build_g0_technical_record(repository)
    try:
        write_json_once_or_verify(repository, target, record)
    except ImmutableIOError as exc:
        raise GateError("immutable G0 technical record differs") from exc
    return record


def _g0_approval_value(
    technical: dict[str, Any], *, approved_at: str
) -> dict[str, Any]:
    validate_g0_technical_record(technical)
    try:
        parsed = dt.datetime.fromisoformat(approved_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GateError("G0 approval timestamp must be ISO 8601 UTC") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != dt.timedelta(0)
        or parsed.microsecond != 0
    ):
        raise GateError("G0 approval timestamp must use whole-second UTC")
    approval: dict[str, Any] = {
        "schema_version": G0_APPROVAL_SCHEMA,
        "gate": "G0",
        "reviewer": "Kevin Araujo",
        "status": "approved",
        "approved_at": parsed.astimezone(dt.UTC).isoformat().replace("+00:00", "Z"),
        "technical_evidence_sha256": technical["technical_evidence_sha256"],
        "technical_gate_artifact_sha256": technical["artifact_sha256"],
    }
    approval["artifact_sha256"] = sha256_json(approval)
    return approval


def validate_g0_approval(value: Any, *, technical: dict[str, Any]) -> None:
    validate_g0_technical_record(technical)
    expected = {
        "schema_version",
        "gate",
        "reviewer",
        "status",
        "approved_at",
        "technical_evidence_sha256",
        "technical_gate_artifact_sha256",
        "artifact_sha256",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise GateError("G0 approval fields changed")
    rebuilt = _g0_approval_value(technical, approved_at=str(value.get("approved_at")))
    if value != rebuilt:
        raise GateError("G0 approval is not bound to the exact technical record")


def approve_g0_gate(
    root: Path | None = None,
    *,
    expected_technical_evidence_sha256: str,
    approved_at: str,
    technical_path: Path | None = None,
    approval_path: Path | None = None,
) -> dict[str, Any]:
    """Create Kevin's separate G0 approval for the exact current evidence."""

    repository = (root or repository_root()).resolve()
    if _SHA256.fullmatch(expected_technical_evidence_sha256) is None:
        raise GateError("G0 approval requires an exact technical evidence SHA-256")
    technical_target = technical_path or repository / G0_TECHNICAL_PATH
    approval_target = approval_path or repository / G0_APPROVAL_PATH
    technical = _read_g0_json(repository, technical_target, "G0 technical record")
    validate_g0_technical_record(technical)
    current = build_g0_technical_record(repository)
    if technical != current:
        raise GateError("saved G0 technical record is not current")
    if technical["technical_evidence_sha256"] != expected_technical_evidence_sha256:
        raise GateError("G0 approval names a different technical evidence hash")
    approval = _g0_approval_value(technical, approved_at=approved_at)
    target = (
        approval_target
        if approval_target.is_absolute()
        else repository / approval_target
    )
    try:
        write_json_once_or_verify(repository, target, approval)
    except ImmutableIOError as exc:
        raise GateError("immutable G0 approval differs") from exc
    return approval


def load_approved_g0_gate(
    root: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Replay the current G0 technical record and Kevin's separate approval."""

    repository = (root or repository_root()).resolve()
    technical = _read_g0_json(repository, G0_TECHNICAL_PATH, "G0 technical record")
    approval = _read_g0_json(repository, G0_APPROVAL_PATH, "G0 approval")
    validate_g0_technical_record(technical)
    if technical != build_g0_technical_record(repository):
        raise GateError("approved G0 technical record is no longer current")
    validate_g0_approval(approval, technical=technical)
    return technical, approval


def _build_g1_evidence(root: Path) -> dict[str, Any]:
    verify_manifest(default_manifest_path(root), root)
    truth_findings = audit_truth_language(root)
    if truth_findings:
        raise GateError("truth-language audit failed:\n" + "\n".join(truth_findings))
    contract_evidence = verify_contract_artifacts(root)
    draft_evidence = validate_g1_task_drafts(root)
    catalog = task_catalog(root)
    rebuilt_split = build_split_manifest(catalog)
    split_path = root / "results/v2/splits/task_split_manifest.json"
    saved_split = json.loads(split_path.read_text(encoding="utf-8"))
    validate_split_manifest(saved_split)
    if rebuilt_split != saved_split:
        raise GateError(
            "saved split manifest is not deterministic from the public task catalog"
        )
    protected = (
        root
        / "novalearn_synthetic_corpus"
        / "evaluation_only_do_not_index"
        / "v2"
        / "static_new_g1_gold.jsonl"
    )
    try:
        V1ReplayAdapter(protected)
    except ProtectedDataError:
        pass
    else:
        raise GateError("adapter loaded a protected answer-key path")
    review_path = root / "evaluation/v2/review_protocol.json"
    review_protocol = json.loads(review_path.read_text(encoding="utf-8"))
    return {
        "v1_manifest": "verified",
        "v1_structural_validation": _legacy_validate(root),
        "truth_language_audit": "passed",
        "schemas": contract_evidence,
        "task_drafts": draft_evidence,
        "split_manifest": str(split_path.relative_to(root)),
        "split_manifest_sha256": saved_split["manifest_sha256"],
        "task_count": saved_split["task_count"],
        "sealed_tasks_external": 48,
        "protected_adapter_boundary": "passed",
        "deterministic_checks": _deterministic_check_evidence(root),
        "v1_adapter_equivalence": verify_v1_adapter_equivalence(root),
        "v1_executable_wrapper": verify_v1_executable_sample(root),
        "review_protocol": _review_packet_evidence(root),
        "review_protocol_file_sha256": _sha256_file(review_path),
        "review_protocol_canonical_sha256": sha256_json(review_protocol),
        "rubric_sha256": REVIEW_RUBRIC_SHA256,
        "provider_and_cost": _provider_and_cost_evidence(root),
        "sealed_import": _sealed_fixture_evidence(root),
        "static_trace_mock": _trace_evidence(root),
        "secret_scan": _secret_scan(root),
    }


def build_g1_technical_record(root: Path | None = None) -> dict[str, Any]:
    """Rebuild the deterministic G1 evidence after replaying approved G0."""

    repository = (root or repository_root()).resolve()
    load_approved_g0_gate(repository)
    evidence = _build_g1_evidence(repository)
    record: dict[str, Any] = {
        "schema_version": G1_TECHNICAL_SCHEMA,
        "gate": "G1",
        "technical_status": "passed",
        "technical_evidence_sha256": sha256_json(evidence),
        "evidence": evidence,
    }
    record["artifact_sha256"] = sha256_json(record)
    validate_g1_technical_record(record)
    return record


def validate_g1_technical_record(value: Any) -> None:
    expected = {
        "schema_version",
        "gate",
        "technical_status",
        "technical_evidence_sha256",
        "evidence",
        "artifact_sha256",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise GateError("G1 technical record fields changed")
    evidence = value.get("evidence")
    if (
        value.get("schema_version") != G1_TECHNICAL_SCHEMA
        or value.get("gate") != "G1"
        or value.get("technical_status") != "passed"
        or not isinstance(evidence, dict)
        or value.get("technical_evidence_sha256") != sha256_json(evidence)
        or value.get("artifact_sha256")
        != sha256_json(
            {key: item for key, item in value.items() if key != "artifact_sha256"}
        )
    ):
        raise GateError("G1 technical record is invalid")


def run_g1_gate(root: Path | None = None, output: Path | None = None) -> dict[str, Any]:
    """Persist the deterministic G1 technical record once, without approval state."""

    repository = (root or repository_root()).resolve()
    target = output or repository / G1_TECHNICAL_PATH
    target = target if target.is_absolute() else repository / target
    record = build_g1_technical_record(repository)
    try:
        write_json_once_or_verify(repository, target, record)
    except ImmutableIOError as exc:
        raise GateError("immutable G1 technical record differs") from exc
    return record


def _g1_approval_value(
    technical: dict[str, Any],
    *,
    g0_technical: dict[str, Any],
    g0_approval: dict[str, Any],
    approved_at: str,
) -> dict[str, Any]:
    validate_g1_technical_record(technical)
    validate_g0_approval(g0_approval, technical=g0_technical)
    try:
        parsed = dt.datetime.fromisoformat(approved_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GateError("G1 approval timestamp must be ISO 8601 UTC") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != dt.timedelta(0)
        or parsed.microsecond != 0
    ):
        raise GateError("G1 approval timestamp must use whole-second UTC")
    approval: dict[str, Any] = {
        "schema_version": G1_APPROVAL_SCHEMA,
        "gate": "G1",
        "reviewer": "Kevin Araujo",
        "status": "approved",
        "approved_at": parsed.astimezone(dt.UTC).isoformat().replace("+00:00", "Z"),
        "technical_evidence_sha256": technical["technical_evidence_sha256"],
        "technical_gate_artifact_sha256": technical["artifact_sha256"],
        "g0_approval_artifact_sha256": g0_approval["artifact_sha256"],
    }
    approval["artifact_sha256"] = sha256_json(approval)
    return approval


def validate_g1_approval(
    value: Any,
    *,
    technical: dict[str, Any],
    g0_technical: dict[str, Any],
    g0_approval: dict[str, Any],
) -> None:
    expected = {
        "schema_version",
        "gate",
        "reviewer",
        "status",
        "approved_at",
        "technical_evidence_sha256",
        "technical_gate_artifact_sha256",
        "g0_approval_artifact_sha256",
        "artifact_sha256",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise GateError("G1 approval fields changed")
    rebuilt = _g1_approval_value(
        technical,
        g0_technical=g0_technical,
        g0_approval=g0_approval,
        approved_at=str(value.get("approved_at")),
    )
    if value != rebuilt:
        raise GateError("G1 approval is not bound to the exact G1 and G0 records")


def approve_g1_gate(
    root: Path | None = None,
    *,
    expected_technical_evidence_sha256: str,
    approved_at: str,
    technical_path: Path | None = None,
    approval_path: Path | None = None,
) -> dict[str, Any]:
    """Bind Kevin's separate approval to exact current G1 and approved G0 bytes."""

    repository = (root or repository_root()).resolve()
    if _SHA256.fullmatch(expected_technical_evidence_sha256) is None:
        raise GateError("G1 approval requires an exact technical evidence SHA-256")
    g0_technical, g0_approval = load_approved_g0_gate(repository)
    technical_target = technical_path or repository / G1_TECHNICAL_PATH
    approval_target = approval_path or repository / G1_APPROVAL_PATH
    technical = _read_g0_json(repository, technical_target, "G1 technical record")
    validate_g1_technical_record(technical)
    current = build_g1_technical_record(repository)
    if technical != current:
        raise GateError("saved G1 technical record is not current")
    if technical["technical_evidence_sha256"] != expected_technical_evidence_sha256:
        raise GateError("G1 approval names a different technical evidence hash")
    approval = _g1_approval_value(
        technical,
        g0_technical=g0_technical,
        g0_approval=g0_approval,
        approved_at=approved_at,
    )
    target = (
        approval_target
        if approval_target.is_absolute()
        else repository / approval_target
    )
    try:
        write_json_once_or_verify(repository, target, approval)
    except ImmutableIOError as exc:
        raise GateError("immutable G1 approval differs") from exc
    return approval


def load_approved_g1_gate(
    root: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Replay current G0/G1 technical evidence and Kevin's separate G1 approval."""

    repository = (root or repository_root()).resolve()
    g0_technical, g0_approval = load_approved_g0_gate(repository)
    technical = _read_g0_json(repository, G1_TECHNICAL_PATH, "G1 technical record")
    approval = _read_g0_json(repository, G1_APPROVAL_PATH, "G1 approval")
    validate_g1_technical_record(technical)
    if technical != build_g1_technical_record(repository):
        raise GateError("approved G1 technical record is no longer current")
    validate_g1_approval(
        approval,
        technical=technical,
        g0_technical=g0_technical,
        g0_approval=g0_approval,
    )
    return technical, approval
