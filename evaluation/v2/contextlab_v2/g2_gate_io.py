"""Canonical public I/O and approval persistence for the G2 final gate."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping

from .experiments import load_protocol
from .g2_gate import G2_GATE_SCHEMA, G2_HUMAN_APPROVAL_SCHEMA, build_g2_final_gate
from .generations import generation_manifest_path
from .static_benchmark import public_static_tasks
from .tasking import sha256_json


class G2GateIOError(ValueError):
    """A G2 gate record or its canonical public persistence is unsafe."""


def default_gate_path(root: Path) -> Path:
    return root / "results/v2/gates/G2.json"


def default_approval_path(root: Path) -> Path:
    return root / "results/v2/gates/G2.approval.json"


def _safe_output(root: Path, output: Path) -> Path:
    root = root.resolve()
    results = root / "results/v2"
    candidate = output if output.is_absolute() else root / output
    candidate = candidate.absolute()
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        if current.is_symlink():
            raise G2GateIOError("G2 gate path must not traverse a symlink")
    resolved_results = results.resolve()
    resolved_candidate = candidate.resolve(strict=False)
    try:
        resolved_candidate.relative_to(resolved_results)
    except ValueError as exc:
        raise G2GateIOError("G2 gate output must stay under results/v2") from exc
    return resolved_candidate


def _same_destination(left: Path, right: Path) -> bool:
    """Fail closed for lexical and case-insensitive filesystem aliases."""
    left = left.resolve(strict=False)
    right = right.resolve(strict=False)
    if left == right or str(left).casefold() == str(right).casefold():
        return True
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise G2GateIOError(f"cannot read canonical {label}") from exc
    if not isinstance(value, dict):
        raise G2GateIOError(f"canonical {label} is not an object")
    return value


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _atomic_create(path: Path, value: Mapping[str, Any]) -> None:
    """Create an approval once; never replace an existing attestation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise G2GateIOError(
                "G2 approval record already exists and is immutable"
            ) from exc
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _validate_approved_at(approved_at: str) -> None:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", approved_at) is None:
        raise G2GateIOError("approved_at must be UTC YYYY-MM-DDTHH:MM:SSZ")
    try:
        import datetime as dt

        dt.datetime.strptime(approved_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise G2GateIOError("approved_at must be UTC YYYY-MM-DDTHH:MM:SSZ") from exc


def load_canonical_g2_gate_inputs(
    root: Path, *, approval_path: Path | None = None, include_approval: bool = True
) -> dict[str, Any]:
    """Load only public repository artifacts and the safe G2 sealed import."""
    root = root.resolve()
    approval = None
    if include_approval:
        approval_file = _safe_output(root, approval_path or default_approval_path(root))
        if approval_path is not None and not approval_file.is_file():
            raise G2GateIOError("explicit G2 approval path does not exist")
        if approval_file.is_file():
            approval = _read_json(approval_file, "G2 approval")
    protocol = load_protocol(root)
    campaign = protocol.get("fixed_comparison", {}).get("generation_campaign_id")
    if campaign != "g2r2":
        raise G2GateIOError("canonical G2 protocol does not name g2r2")
    return {
        "protocol": protocol,
        "static_freeze": _read_json(
            root / "results/v2/splits/static_g2_freeze.json", "static freeze"
        ),
        "component_lab": _read_json(
            root / "results/v2/retrieval/public_component_lab.json", "component lab"
        ),
        "component_analysis": _read_json(
            root / "results/v2/reports/g2_public_component_analysis.json",
            "component analysis",
        ),
        "public_answer_metrics": _read_json(
            root / "results/v2/reports/g2_public_answer_metrics.json",
            "public answer metrics",
        ),
        "repeat_analysis": _read_json(
            root / "results/v2/reports/g2_public_repeats.json", "repeat analysis"
        ),
        "generation_manifests": [
            _read_json(
                generation_manifest_path(root, trial, campaign),
                f"generation manifest {trial}",
            )
            for trial in range(1, 6)
        ],
        "sealed_import": _read_json(
            root / "results/v2/sealed/g2-import.json", "safe sealed import"
        ),
        "public_tasks": public_static_tasks(root),
        "root": root,
        "human_approval": approval,
    }


def run_and_write_g2_gate(
    root: Path, *, output: Path | None = None, approval_path: Path | None = None
) -> dict[str, Any]:
    root = root.resolve()
    destination = _safe_output(root, output or default_gate_path(root))
    approval_target = _safe_output(root, approval_path or default_approval_path(root))
    if _same_destination(destination, approval_target):
        raise G2GateIOError("G2 gate output must not replace an approval record")
    try:
        record = build_g2_final_gate(
            **load_canonical_g2_gate_inputs(root, approval_path=approval_path)
        )
    except ValueError as exc:
        if approval_path is None and not default_approval_path(root).is_file():
            raise
        pending = build_g2_final_gate(
            **load_canonical_g2_gate_inputs(root, include_approval=False)
        )
        _atomic_write(destination, pending)
        raise G2GateIOError(
            "G2 approval is stale or invalid; wrote fresh pending gate"
        ) from exc
    _atomic_write(destination, record)
    return record


def build_kevin_approval(
    gate_record: Mapping[str, Any], *, approved_at: str
) -> dict[str, str]:
    """Create an explicit approval only for an untampered pending G2 record."""
    if not isinstance(approved_at, str):
        raise G2GateIOError("approved_at must be explicit and non-empty")
    _validate_approved_at(approved_at)
    if gate_record.get("schema_version") != G2_GATE_SCHEMA:
        raise G2GateIOError("G2 gate record schema is invalid")
    technical = {
        key: value
        for key, value in gate_record.items()
        if key
        not in {
            "technical_record_sha256",
            "human_approval",
            "final_decision",
            "artifact_sha256",
        }
    }
    if gate_record.get("technical_record_sha256") != sha256_json(
        technical
    ) or gate_record.get("artifact_sha256") != sha256_json(
        {key: value for key, value in gate_record.items() if key != "artifact_sha256"}
    ):
        raise G2GateIOError("G2 gate record is altered")
    approval = gate_record.get("human_approval")
    if (
        not isinstance(approval, Mapping)
        or approval.get("status") != "pending"
        or gate_record.get("final_decision") != "blocked"
    ):
        raise G2GateIOError("G2 gate record is not pending Kevin approval")
    return {
        "schema_version": G2_HUMAN_APPROVAL_SCHEMA,
        "gate_sha256": str(gate_record["technical_record_sha256"]),
        "reviewer": "Kevin Araujo",
        "reviewer_role": "human_reviewer",
        "decision": "approved",
        "approved_at": approved_at,
    }


def approve_existing_g2_gate(
    root: Path,
    *,
    gate_path: Path | None = None,
    output: Path | None = None,
    approved_at: str,
) -> dict[str, str]:
    root = root.resolve()
    gate = _safe_output(root, gate_path or default_gate_path(root))
    destination = _safe_output(root, output or default_approval_path(root))
    if _same_destination(destination, gate):
        raise G2GateIOError("G2 approval output must not replace the gate record")
    supplied = _read_json(gate, "G2 gate record")
    fresh = build_g2_final_gate(
        **load_canonical_g2_gate_inputs(root, include_approval=False)
    )
    if supplied != fresh:
        raise G2GateIOError(
            "supplied G2 gate is not the fresh canonical pending record"
        )
    approval = build_kevin_approval(fresh, approved_at=approved_at)
    _atomic_create(destination, approval)
    return approval
