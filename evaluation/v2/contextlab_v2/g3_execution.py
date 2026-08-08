"""Provider-free preparation of one frozen public G3 generation cell."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any

from .baseline import repository_root
from .g3_evidence import (
    episode_card_block,
    memory_read_evidence,
    observable_temporal_events,
    public_raw_evidence_ids,
    render_selected_context,
    static_r0_trace_index,
    temporal_verification_corpus_evidence,
    trace_corpus_evidence,
    temporal_task_snapshot_time,
)
from .g3_freeze import G3_PROMPT_VERSION, build_g3_corpus_snapshot
from .gateway import validate_generation_spec
from .generations import build_generation_spec
from .memory import Episode, MemoryEngine, MemoryRead
from .memory_experiments import (
    build_memory_trace,
    validate_memory_experiment_manifest,
    validate_memory_trace,
)
from .retrieval import estimate_tokens
from .tasking import sha256_json, validate_prompt_safe_task
from .temporal import CorpusEvent, temporal_question_catalog


G3_PREPARED_CELL_SCHEMA = "contextlab.g3-prepared-cell.v1"
MEMORY_PROMPT_RELATIVE_PATH = Path("evaluation/v2/prompts/memory_answer_v1.md")


class G3ExecutionError(ValueError):
    """A frozen public G3 cell cannot be prepared without changing its inputs."""


@dataclass(frozen=True)
class G3PreparationContext:
    """Shared validated inputs reused across the 1,120-cell public grid."""

    root: Path
    manifest: Mapping[str, Any]
    trusted_frozen_manifest_sha256: str
    instruction: str
    prompt_sha256: str
    raw_ids: tuple[str, ...]
    corpus_snapshot: Mapping[str, Any]
    static_traces: Mapping[str, Mapping[str, Any]]
    temporal_traces: Mapping[str, Mapping[str, Any]]
    static_lab_sha256: str
    temporal_lab_sha256: str
    episode_blocks: Mapping[str, str]


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _memory_prompt(root: Path) -> tuple[str, str]:
    path = root / MEMORY_PROMPT_RELATIVE_PATH
    try:
        raw = path.read_bytes()
        instruction = raw.decode("utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise G3ExecutionError("cannot read memory_answer_v1 prompt") from exc
    if not instruction:
        raise G3ExecutionError("memory_answer_v1 prompt is empty")
    return instruction, hashlib.sha256(raw).hexdigest()


def load_memory_answer_instruction(root: Path | None = None) -> str:
    """Load the exact system instruction committed by the canonical G3 freeze."""

    instruction, _digest = _memory_prompt((root or repository_root()).resolve())
    return instruction


def validate_prepared_public_g3_cell(
    value: Mapping[str, Any], *, root: Path | None = None
) -> None:
    """Validate a saved provider-free cell before it can be generated or graded."""

    expected_fields = {
        "schema_version",
        "frozen_manifest_sha256",
        "trusted_frozen_manifest_sha256",
        "run_spec",
        "run_spec_sha256",
        "corpus_snapshot_sha256",
        "source_r0_lab_sha256",
        "source_r0_trace_id",
        "source_r0_trace_sha256",
        "task_snapshot_time",
        "task_as_of_time",
        "observable_event_ids",
        "memory_read_status",
        "memory_read",
        "memory_snapshot",
        "memory_snapshot_id",
        "decision_ledger",
        "memory_trace",
        "rendered_context",
        "rendered_context_sha256",
        "rendered_context_tokens",
        "generation_spec",
        "artifact_sha256",
    }
    body = {key: item for key, item in value.items() if key != "artifact_sha256"}
    if (
        not isinstance(value, Mapping)
        or set(value) != expected_fields
        or value.get("schema_version") != G3_PREPARED_CELL_SCHEMA
        or value.get("artifact_sha256") != sha256_json(body)
    ):
        raise G3ExecutionError("prepared G3 cell envelope or hash is invalid")
    for field in (
        "frozen_manifest_sha256",
        "trusted_frozen_manifest_sha256",
        "run_spec_sha256",
        "corpus_snapshot_sha256",
        "source_r0_lab_sha256",
        "source_r0_trace_sha256",
        "rendered_context_sha256",
        "artifact_sha256",
    ):
        if not _is_sha256(value.get(field)):
            raise G3ExecutionError(f"prepared G3 cell {field} is invalid")

    spec = value.get("run_spec")
    trace = value.get("memory_trace")
    generation_spec = value.get("generation_spec")
    snapshot = value.get("memory_snapshot")
    decisions = value.get("decision_ledger")
    rendered = value.get("rendered_context")
    if not all(
        isinstance(item, Mapping) for item in (spec, trace, generation_spec, snapshot)
    ) or not isinstance(decisions, list):
        raise G3ExecutionError("prepared G3 cell nested artifacts are invalid")
    spec_body = {key: item for key, item in spec.items() if key != "run_spec_sha256"}
    if (
        spec.get("run_spec_sha256") != sha256_json(spec_body)
        or value.get("run_spec_sha256") != spec.get("run_spec_sha256")
        or value.get("corpus_snapshot_sha256") != spec.get("corpus_snapshot_sha256")
        or value.get("frozen_manifest_sha256")
        != value.get("trusted_frozen_manifest_sha256")
    ):
        raise G3ExecutionError("prepared G3 cell run commitments changed")
    try:
        validate_memory_trace(trace)
        validate_generation_spec(generation_spec)
    except ValueError as exc:
        raise G3ExecutionError(
            "prepared G3 trace or generation spec is invalid"
        ) from exc
    task = spec.get("task")
    generation_task = generation_spec.get("task")
    if not isinstance(task, Mapping) or not isinstance(generation_task, Mapping):
        raise G3ExecutionError("prepared G3 cell task identity is invalid")
    if (
        trace.get("run_id") != spec.get("run_id")
        or trace.get("run_spec_sha256") != spec.get("run_spec_sha256")
        or generation_spec.get("run_id") != spec.get("run_id")
        or generation_spec.get("reasoning_effort") != spec.get("reasoning_effort")
        or generation_spec.get("max_tokens") != spec.get("output_token_limit")
        or generation_spec.get("temperature") != 0.0
        or generation_spec.get("rendered_context") != rendered
        or any(
            generation_task.get(field) != task.get(field)
            for field in ("task_id", "suite", "question_text", "question_sha256")
        )
    ):
        raise G3ExecutionError("prepared G3 cell execution identity changed")
    instruction, prompt_sha256 = _memory_prompt((root or repository_root()).resolve())
    if (
        generation_spec.get("system_instruction") != instruction
        or spec.get("prompt_sha256") != prompt_sha256
    ):
        raise G3ExecutionError("prepared G3 cell prompt commitment changed")
    if (
        not isinstance(rendered, str)
        or not rendered
        or value.get("rendered_context_sha256")
        != hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        or value.get("rendered_context_tokens") != estimate_tokens(rendered)
    ):
        raise G3ExecutionError("prepared G3 rendered context changed")
    observable = value.get("observable_event_ids")
    if (
        not isinstance(observable, list)
        or observable != list(dict.fromkeys(observable))
        or any(not isinstance(item, str) or not item for item in observable)
    ):
        raise G3ExecutionError("prepared G3 observable event list is invalid")
    try:
        restored = MemoryEngine.from_snapshot_record(snapshot).snapshot_record()
    except ValueError as exc:
        raise G3ExecutionError("prepared G3 memory snapshot is invalid") from exc
    if (
        restored != snapshot
        or snapshot.get("snapshot_id") != value.get("memory_snapshot_id")
        or snapshot.get("policy") != spec.get("policy")
        or snapshot.get("decision_ledger") != decisions
    ):
        raise G3ExecutionError("prepared G3 memory snapshot commitment changed")
    if not isinstance(value.get("source_r0_trace_id"), str) or not value.get(
        "source_r0_trace_id"
    ):
        raise G3ExecutionError("prepared G3 source R0 trace is invalid")


def _frozen_spec(
    manifest: Mapping[str, Any], run_spec: Mapping[str, Any]
) -> dict[str, Any]:
    if not isinstance(run_spec, Mapping):
        raise G3ExecutionError("G3 run spec must be an object")
    run_id = run_spec.get("run_id")
    matches = [
        row
        for row in manifest["run_specs"]
        if isinstance(row, Mapping) and row.get("run_id") == run_id
    ]
    if len(matches) != 1 or dict(matches[0]) != dict(run_spec):
        raise G3ExecutionError("G3 run spec is outside the trusted frozen manifest")
    return dict(matches[0])


def _require_approved_retriever(manifest: Mapping[str, Any]) -> None:
    binding = manifest.get("retriever_binding")
    if not isinstance(binding, Mapping) or (
        binding.get("retriever_id"),
        binding.get("retriever_disposition"),
        binding.get("g2_approval_status"),
        binding.get("g2_gate_disposition"),
    ) != ("R0", "retain-simple", "approved", "retain-simple"):
        raise G3ExecutionError(
            "G3 execution requires the approved retain-simple/R0 G2 binding"
        )


def _prompt_task(task: Mapping[str, Any]) -> dict[str, str]:
    projected = {
        "schema_version": "contextlab.prompt-task.v1",
        "task_id": str(task.get("task_id", "")),
        "suite": str(task.get("suite", "")),
        "question_text": str(task.get("question_text", "")),
        "question_sha256": str(task.get("question_sha256", "")),
    }
    try:
        validate_prompt_safe_task(projected)
    except ValueError as exc:
        raise G3ExecutionError("G3 run task is not prompt-safe") from exc
    return projected


def _observable_temporal_events(
    task_id: str,
) -> tuple[tuple[CorpusEvent, ...], str, str, str, str, str]:
    question = next(
        (row for row in temporal_question_catalog() if row.task_id == task_id), None
    )
    if (
        question is None
        or question.is_sealed
        or question.subject is None
        or question.predicate is None
        or question.question_text is None
    ):
        raise G3ExecutionError("temporal run task is not on the public G3 surface")
    snapshot_time = temporal_task_snapshot_time(task_id)
    observable = observable_temporal_events(task_id)
    return (
        observable,
        snapshot_time,
        question.as_of_time or snapshot_time,
        str(question.subject),
        str(question.predicate),
        question.scenario_id,
    )


def _memory_state(
    spec: Mapping[str, Any],
) -> tuple[MemoryEngine, MemoryRead | None, str, str | None, str | None, list[str]]:
    policy = str(spec["policy"])
    task = spec["task"]
    if task["suite"] == "static":
        return MemoryEngine(policy), None, "not_applicable_static", None, None, []

    events, snapshot_time, as_of_time, subject, predicate, _scenario_id = (
        _observable_temporal_events(str(task["task_id"]))
    )
    engine = MemoryEngine.rebuilt(policy, events)
    if not events:
        return (
            engine,
            None,
            "empty_observable_snapshot",
            snapshot_time,
            as_of_time,
            [],
        )
    read = engine.read(
        subject,
        predicate,
        observed_through=snapshot_time,
        as_of_time=as_of_time,
        task_family=str(task["task_family"]),
        task_signature=sha256_json(
            {
                "suite": task["suite"],
                "task_family": task["task_family"],
                "question_sha256": task["question_sha256"],
            }
        ),
        query_text=str(task["question_text"]),
    )
    return (
        engine,
        read,
        "ready",
        snapshot_time,
        as_of_time,
        [event.event_id for event in events],
    )


def _episode_blocks(
    manifest: Mapping[str, Any],
) -> dict[str, str]:
    grades = {
        str(row["grade_artifact_id"]): row
        for row in manifest["trusted_grade_artifacts"]
    }
    blocks: dict[str, str] = {}
    for seed in manifest["m4_episode_seed"]:
        grade = grades[str(seed["grade_artifact_id"])]
        episode = Episode(
            episode_id=str(seed["episode_id"]),
            task_signature=str(seed["task_signature"]),
            category=str(seed["task_family"]),
            selected_strategy=str(seed["selected_strategy"]),
            evidence_path=tuple(str(value) for value in seed["evidence_path"]),
            graded_outcome={
                "objective": True,
                "accepted": True,
                "outcome": seed["grade_outcome"],
                "source": grade["grader_id"],
                "grade_artifact_id": seed["grade_artifact_id"],
            },
            cost_usd=0.0,
            latency_ms=0,
            failure_mode=(
                None if seed["grade_outcome"] == "success" else "objective_failure"
            ),
            source_run_id=str(seed["source_run_id"]),
            trace_id=str(seed["trace_id"]),
            retention_decision="retain",
            promotion_decision="promoted",
        )
        blocks[episode.episode_id] = episode_card_block(episode)
    return blocks


def build_g3_preparation_context(
    manifest: Mapping[str, Any],
    *,
    trusted_frozen_manifest_sha256: str,
    static_r0_lab: Mapping[str, Any],
    temporal_r0_lab: Mapping[str, Any],
    root: Path | None = None,
) -> G3PreparationContext:
    """Validate expensive shared artifacts once for a batch of frozen cells."""

    root = (root or repository_root()).resolve()
    try:
        validate_memory_experiment_manifest(
            manifest,
            trusted_frozen_manifest_sha256=trusted_frozen_manifest_sha256,
        )
    except ValueError as exc:
        raise G3ExecutionError("G3 manifest is not the trusted v3 freeze") from exc
    _require_approved_retriever(manifest)
    instruction, prompt_sha256 = _memory_prompt(root)
    if (
        manifest.get("prompt_version") != G3_PROMPT_VERSION
        or manifest.get("prompt_sha256") != prompt_sha256
    ):
        raise G3ExecutionError(
            "G3 manifest does not bind the committed memory_answer_v1 prompt"
        )
    raw_ids = public_raw_evidence_ids(root)
    if manifest.get("available_raw_evidence_ids") != raw_ids:
        raise G3ExecutionError("G3 manifest raw evidence registry is not canonical")
    try:
        corpus_snapshot = build_g3_corpus_snapshot(
            static_lab=static_r0_lab,
            temporal_lab=temporal_r0_lab,
            raw_evidence_ids=raw_ids,
        )
        static_traces = static_r0_trace_index(static_r0_lab)
    except ValueError as exc:
        raise G3ExecutionError("G3 R0 evidence artifacts are invalid") from exc
    if manifest.get("corpus_snapshot_sha256") != corpus_snapshot["artifact_sha256"]:
        raise G3ExecutionError("G3 manifest points to a different corpus snapshot")
    retriever_sha256 = manifest["retriever_binding"]["retriever_protocol_sha256"]
    if (
        static_r0_lab.get("protocol_sha256") != retriever_sha256
        or temporal_r0_lab.get("retriever_protocol_sha256") != retriever_sha256
    ):
        raise G3ExecutionError("saved R0 traces use a different retriever protocol")
    temporal_traces = {
        str(row["task"]["task_id"]): row for row in temporal_r0_lab["traces"]
    }
    return G3PreparationContext(
        root=root,
        manifest=manifest,
        trusted_frozen_manifest_sha256=trusted_frozen_manifest_sha256,
        instruction=instruction,
        prompt_sha256=prompt_sha256,
        raw_ids=tuple(raw_ids),
        corpus_snapshot=corpus_snapshot,
        static_traces=static_traces,
        temporal_traces=temporal_traces,
        static_lab_sha256=str(static_r0_lab["artifact_sha256"]),
        temporal_lab_sha256=str(temporal_r0_lab["artifact_sha256"]),
        episode_blocks=_episode_blocks(manifest),
    )


def _context_source_trace(
    context: G3PreparationContext, task: Mapping[str, Any]
) -> tuple[Mapping[str, Any], str]:
    task_id = str(task["task_id"])
    if task["suite"] == "static":
        trace = context.static_traces.get(task_id)
        lab_sha256 = context.static_lab_sha256
    elif task["suite"] == "temporal":
        trace = context.temporal_traces.get(task_id)
        lab_sha256 = context.temporal_lab_sha256
    else:
        trace = None
        lab_sha256 = ""
    if trace is None or trace.get("task") != _prompt_task(task):
        raise G3ExecutionError("saved R0 trace does not match the frozen run task")
    return trace, lab_sha256


def prepare_public_g3_cell(
    manifest: Mapping[str, Any],
    run_spec: Mapping[str, Any],
    *,
    trusted_frozen_manifest_sha256: str,
    static_r0_lab: Mapping[str, Any],
    temporal_r0_lab: Mapping[str, Any],
    root: Path | None = None,
) -> dict[str, Any]:
    """Prepare one approved public cell without a provider or paid side effect."""

    context = build_g3_preparation_context(
        manifest,
        trusted_frozen_manifest_sha256=trusted_frozen_manifest_sha256,
        static_r0_lab=static_r0_lab,
        temporal_r0_lab=temporal_r0_lab,
        root=root,
    )
    return prepare_public_g3_cell_with_context(context, run_spec)


def prepare_public_g3_cell_with_context(
    context: G3PreparationContext, run_spec: Mapping[str, Any]
) -> dict[str, Any]:
    """Prepare one cell after the batch context has validated shared artifacts."""

    manifest = context.manifest
    root = context.root
    trusted_frozen_manifest_sha256 = context.trusted_frozen_manifest_sha256
    instruction = context.instruction
    raw_ids = list(context.raw_ids)
    corpus_snapshot = context.corpus_snapshot
    spec = _frozen_spec(manifest, run_spec)
    if (
        spec["prompt_version"] != G3_PROMPT_VERSION
        or spec["prompt_sha256"] != context.prompt_sha256
        or spec["corpus_snapshot_sha256"] != corpus_snapshot["artifact_sha256"]
    ):
        raise G3ExecutionError("run spec changed after G3 batch validation")
    source_trace, source_lab_sha256 = _context_source_trace(context, spec["task"])
    corpus_rows, corpus_blocks = trace_corpus_evidence(source_trace)
    if spec["task"]["suite"] == "temporal":
        verification_rows, verification_blocks = temporal_verification_corpus_evidence(
            str(spec["task"]["task_id"])
        )
        represented_raw_ids = {
            raw_id for row in corpus_rows for raw_id in row["raw_evidence_ids"]
        }
        for row in verification_rows:
            if not set(row["raw_evidence_ids"]).issubset(represented_raw_ids):
                corpus_rows.append(row)
                corpus_blocks[row["evidence_id"]] = verification_blocks[
                    row["evidence_id"]
                ]
                represented_raw_ids.update(row["raw_evidence_ids"])
    engine, read, read_status, snapshot_time, as_of_time, event_ids = _memory_state(
        spec
    )
    if read is None:
        memory_rows: list[dict[str, Any]] = []
        memory_blocks: dict[str, str] = {}
    else:
        memory_rows, memory_blocks = memory_read_evidence(read)

    try:
        trace = build_memory_trace(
            spec,
            corpus_evidence=corpus_rows,
            memory_evidence=memory_rows,
            m4_episode_seed=(
                manifest["m4_episode_seed"] if spec["policy"] == "M4" else ()
            ),
            available_raw_evidence_ids=raw_ids,
            trusted_grade_artifacts=manifest["trusted_grade_artifacts"],
        )
        rendered_context = render_selected_context(
            trace,
            corpus_blocks=corpus_blocks,
            memory_blocks=memory_blocks,
            episode_blocks=context.episode_blocks,
        )
    except ValueError as exc:
        raise G3ExecutionError(
            "G3 evidence cannot fit the frozen trace contract"
        ) from exc

    generation_input = {
        "task": _prompt_task(spec["task"]),
        "strategy_id": spec["policy"],
        "rendered_context": rendered_context,
    }
    try:
        generation_spec = build_generation_spec(
            generation_input,
            str(spec["reasoning_effort"]),
            trial=1,
            max_tokens=int(spec["output_token_limit"]),
            temperature=0.0,
            system_instruction=instruction,
            campaign_id=str(spec["campaign_id"]),
        )
        generation_spec["run_id"] = spec["run_id"]
        validate_generation_spec(generation_spec)
    except (ValueError, RuntimeError) as exc:
        raise G3ExecutionError(
            "cannot build the gateway-compatible G3 generation spec"
        ) from exc

    snapshot = engine.snapshot_record()
    if MemoryEngine.from_snapshot_record(snapshot).snapshot_record() != snapshot:
        raise G3ExecutionError("prepared memory snapshot is not replayable")
    decisions = [decision.to_record() for decision in engine.decision_ledger]
    if snapshot["decision_ledger"] != decisions:
        raise G3ExecutionError(
            "prepared decision ledger differs from the memory snapshot"
        )

    payload: dict[str, Any] = {
        "schema_version": G3_PREPARED_CELL_SCHEMA,
        "frozen_manifest_sha256": manifest["frozen_manifest_sha256"],
        "trusted_frozen_manifest_sha256": trusted_frozen_manifest_sha256,
        "run_spec": spec,
        "run_spec_sha256": spec["run_spec_sha256"],
        "corpus_snapshot_sha256": corpus_snapshot["artifact_sha256"],
        "source_r0_lab_sha256": source_lab_sha256,
        "source_r0_trace_id": source_trace["run_id"],
        "source_r0_trace_sha256": sha256_json(source_trace),
        "task_snapshot_time": snapshot_time,
        "task_as_of_time": as_of_time,
        "observable_event_ids": event_ids,
        "memory_read_status": read_status,
        "memory_read": read.to_record() if read is not None else None,
        "memory_snapshot": snapshot,
        "memory_snapshot_id": snapshot["snapshot_id"],
        "decision_ledger": decisions,
        "memory_trace": trace,
        "rendered_context": rendered_context,
        "rendered_context_sha256": hashlib.sha256(
            rendered_context.encode("utf-8")
        ).hexdigest(),
        "rendered_context_tokens": estimate_tokens(rendered_context),
        "generation_spec": generation_spec,
    }
    payload["artifact_sha256"] = sha256_json(payload)
    validate_prepared_public_g3_cell(payload, root=root)
    return payload
