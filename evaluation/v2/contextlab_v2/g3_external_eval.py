"""Standalone external evaluator for the sealed G3 temporal surface.

The raw bundle, candidate manifest, preparation artifacts, generation results,
grades, and safe return all stay outside the repository. Completed campaigns
emit only the exact content-free schema accepted by
``g3_sealed.validate_g3_sealed_return``. Capped incomplete runs emit a separate
content-free progress manifest and do not write a sealed return.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any

from .baseline import repository_root
from .costs import CostLedger, canonical_ledger_path
from .embeddings import (
    embed_texts,
    embedding_key,
    load_embedding_cache,
    load_extension_cache,
)
from .experiments import (
    TRACE_SCHEMA,
    chunk_embedding_text,
    load_protocol,
    run_task_ladder,
)
from .g3_evidence import (
    memory_read_evidence,
    render_selected_context,
    temporal_event_chunks,
    trace_corpus_evidence,
)
from .g3_execution import load_memory_answer_instruction
from .g3_freeze import validate_g3_freeze
from .g3_grading import _recover_invalid_temporal_footer, parse_answer_footer
from .g3_sealed import (
    G3_SEALED_CELL_COUNT,
    G3_SEALED_RETURN_SCHEMA,
    G3_SEALED_TASK_IDS,
    G3SealedError,
    validate_g3_sealed_candidate_manifest,
    validate_g3_sealed_return,
)
from .gateway import run_paid_generation_to_file, validate_generation_spec
from .generations import (
    GenerationBatchError,
    build_generation_spec,
    validate_saved_generation_result,
)
from .memory import MemoryEngine
from .memory_experiments import (
    MEMORY_RUN_SPEC_SCHEMA,
    build_memory_trace,
    validate_memory_experiment_manifest,
)
from .immutable_io import (
    ImmutableIOError,
    read_bytes_snapshot,
    replace_json_atomically,
    write_bytes_once_or_verify,
    write_json_once_or_verify,
)
from .provider import MODEL_ID, PROVIDER_SLUG
from .retrieval import estimate_tokens
from .tasking import sha256_json
from .temporal import (
    ANSWER_SCHEMA,
    all_events,
    build_as_of_answer,
    build_current_answer,
    event_history_sha256,
    sealed_temporal_references,
)


EXTERNAL_BUNDLE_SCHEMA = "contextlab.external-temporal-sealed.v1"
EXTERNAL_TEMPORAL_BUNDLE_SCHEMA = EXTERNAL_BUNDLE_SCHEMA
G3_EXTERNAL_EVALUATOR_VERSION = "contextlab-g3-external-evaluator-v1"
G3_EXTERNAL_PROGRESS_SCHEMA = "contextlab.g3-external-progress.v1"

_BUNDLE_FIELDS = frozenset(
    {"schema", "authoring_version", "temporal_event_history_sha256", "tasks"}
)
_TASK_FIELDS = frozenset(
    {
        "task_id",
        "scenario_id",
        "task_family",
        "difficulty",
        "question_text",
        "subject",
        "predicate",
        "as_of_time",
        "snapshot_time",
        "expected_answer",
        "required_evidence",
        "scoring_notes",
    }
)
_EXPECTED_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "value",
        "supporting_event_ids",
        "as_of_time",
        "snapshot_time",
    }
)
_OUTCOME_FIELDS = frozenset(
    {
        "status",
        "response_status",
        "is_correct",
        "stale",
        "provenance_complete",
        "correction_latency",
        "actual_usd",
        "latency_ms",
        "failure_labels",
        "replay_bindings",
    }
)
_REPLAY_FIELDS = frozenset(
    {
        "prepared_cell_sha256",
        "run_spec_sha256",
        "trace_sha256",
        "memory_snapshot_sha256",
        "decision_ledger_sha256",
        "generation_spec_sha256",
        "generation_result_sha256",
        "grade_sha256",
        "usage_sha256",
    }
)
_ATTEMPT_FIELDS = frozenset({"schema_version", "run_id", "cell_sha256"})
_FAILURE_LABELS = frozenset(
    {
        "wrong_value",
        "stale_value",
        "missing_provenance",
        "bad_abstention",
        "provider_failure",
        "other_permitted",
    }
)
_PROGRESS_FIELDS = frozenset(
    {
        "schema_version",
        "evaluation_id",
        "candidate_manifest_sha256",
        "g3_freeze_sha256",
        "external_bundle_sha256",
        "temporal_event_history_sha256",
        "cell_count",
        "completed_count",
        "failed_count",
        "missing_count",
        "new_call_count",
        "remaining_count",
        "actual_usd",
        "evaluator_version",
        "status",
        "sealed_return_sha256",
        "artifact_sha256",
    }
)
_FORBIDDEN_CONTENT_KEY = re.compile(
    r"(?:^|[_-])(?:question|gold|expected(?:[_-]?answer)?|answer|"
    r"rendered(?:[_-]?context)?|retrieved(?:[_-]?text)?|source(?:[_-]?text)?|"
    r"trace(?:[_-]?content)?|scoring(?:[_-]?notes)?)(?:$|[_-])",
    re.IGNORECASE,
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_CITATION = re.compile(r"\[([^\]\n]{1,200})\]")
_INSUFFICIENT = re.compile(
    r"\b(?:insufficient|cannot determine|can't determine|unable to determine|"
    r"not enough evidence|no active (?:claim|evidence|record)|"
    r"no current (?:claim|evidence|record)|evidence (?:is )?"
    r"(?:absent|unavailable)|does not establish|do not establish|abstain)\b",
    re.IGNORECASE,
)


class G3ExternalEvaluationError(ValueError):
    """The external G3 bundle, execution, or safe return is not sealed."""


CellEvaluator = Callable[..., Mapping[str, Any]]
EmbeddingRunner = Callable[..., Mapping[str, Sequence[float]]]
RetrievalRunner = Callable[..., Sequence[Mapping[str, Any]]]
GenerationRunner = Callable[..., Mapping[str, Any]]


class _ContentFreeEmbeddingLedger:
    """Keep provider error text for sealed queries out of the repository ledger."""

    def __init__(self, ledger: CostLedger):
        self._ledger = ledger
        self.path = ledger.path

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ledger, name)

    def fail(
        self,
        reservation_id: str,
        *,
        stage: str,
        reason: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        del reason
        safe_metadata = {
            key: value for key, value in dict(metadata or {}).items() if key != "error"
        }
        self._ledger.fail(
            reservation_id,
            stage=stage,
            reason="external sealed embedding failed",
            metadata=safe_metadata,
        )

    def cancel(self, reservation_id: str, *, reason: str) -> None:
        del reason
        self._ledger.cancel(
            reservation_id, reason="external sealed embedding cancelled"
        )


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise G3ExternalEvaluationError(
                "external sealed JSON contains a duplicate field"
            )
        value[key] = item
    return value


def _reject_constant(_: str) -> None:
    raise G3ExternalEvaluationError("external sealed JSON contains a non-finite number")


def _load_strict_json(
    path: Path, label: str, *, anchor: Path | None = None
) -> tuple[Any, bytes]:
    try:
        raw = read_bytes_snapshot(
            anchor or path.parent, path, max_bytes=128 * 1024 * 1024
        )
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except G3ExternalEvaluationError:
        raise
    except (
        ImmutableIOError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        raise G3ExternalEvaluationError(f"cannot read strict {label} JSON") from exc
    return value, raw


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _require_external_file(path: Path, repository: Path, label: str) -> Path:
    requested = Path(os.path.abspath(path))
    if requested.is_symlink():
        raise G3ExternalEvaluationError(f"{label} must be a regular file")
    resolved = requested.resolve()
    if _is_relative_to(resolved, repository):
        raise G3ExternalEvaluationError(f"{label} must stay outside the repository")
    try:
        read_bytes_snapshot(resolved.parent, resolved, max_bytes=128 * 1024 * 1024)
    except ImmutableIOError as exc:
        raise G3ExternalEvaluationError(
            f"{label} is not a readable regular file"
        ) from exc
    return resolved


def _ensure_absolute_directory(path: Path) -> Path:
    """Create one canonical absolute directory without following new symlinks."""

    resolved = path.resolve(strict=False)
    if not resolved.is_absolute() or resolved == Path(resolved.anchor):
        raise G3ExternalEvaluationError("external G3 directory path is invalid")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(resolved.anchor, flags)
    try:
        for component in resolved.parts[1:]:
            try:
                os.mkdir(component, 0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except OSError as exc:
        raise G3ExternalEvaluationError(
            "external G3 directory contains an unsafe component"
        ) from exc
    finally:
        os.close(descriptor)
    return resolved


def _require_external_work_root(path: Path, repository: Path) -> Path:
    resolved = path.resolve()
    if _is_relative_to(resolved, repository):
        raise G3ExternalEvaluationError(
            "external G3 work root must stay outside the repository"
        )
    broad = {
        Path(resolved.anchor),
        Path("/", "tmp").resolve(),
        Path("/", "var", "tmp").resolve(),
        Path("/", "private", "tmp").resolve(),
    }
    if resolved in broad:
        raise G3ExternalEvaluationError("external G3 work root is too broad")
    return _ensure_absolute_directory(resolved)


def _ensure_external_subdirectory(root: Path, relative: Path) -> None:
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise G3ExternalEvaluationError("external G3 destination is invalid")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(root, flags)
    except OSError as exc:
        raise G3ExternalEvaluationError("external G3 work root is unsafe") from exc
    try:
        for component in relative.parts:
            try:
                os.mkdir(component, 0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except OSError as exc:
        raise G3ExternalEvaluationError(
            "external G3 work directory contains an unsafe component"
        ) from exc
    finally:
        os.close(descriptor)


def _external_path(root: Path, relative_path: Path, *, directory: bool = False) -> Path:
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or any(part in {"", ".", ".."} for part in relative_path.parts)
    ):
        raise G3ExternalEvaluationError("external G3 destination is invalid")
    parent = relative_path if directory else relative_path.parent
    if parent.parts:
        _ensure_external_subdirectory(root, parent)
    destination = root / relative_path
    if not directory:
        try:
            info = os.stat(destination, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise G3ExternalEvaluationError(
                "external G3 destination is unsafe"
            ) from exc
        else:
            if not stat.S_ISREG(info.st_mode):
                raise G3ExternalEvaluationError(
                    "external G3 destination is not a regular file"
                )
    return destination


def _atomic_write_json(root: Path, path: Path, value: Mapping[str, Any]) -> None:
    try:
        replace_json_atomically(root, path, value)
    except (ImmutableIOError, ValueError) as exc:
        raise G3ExternalEvaluationError(
            "cannot replace external G3 JSON artifact"
        ) from exc


def _atomic_create_json(root: Path, path: Path, value: Mapping[str, Any]) -> bool:
    """Create one durable marker without replacing a concurrent winner."""

    try:
        return write_json_once_or_verify(root, path, value)
    except (ImmutableIOError, ValueError) as exc:
        raise G3ExternalEvaluationError(
            "cannot create immutable external G3 JSON artifact"
        ) from exc


def _exact_fields(
    value: Mapping[str, Any], expected: frozenset[str], label: str
) -> None:
    if set(value) != expected:
        raise G3ExternalEvaluationError(
            f"{label} fields differ; missing={sorted(expected - set(value))}, "
            f"unknown={sorted(set(value) - expected)}"
        )


def _sha(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise G3ExternalEvaluationError(f"{label} must be a lowercase SHA-256")
    return value


def _text(value: object, label: str, *, maximum: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise G3ExternalEvaluationError(f"{label} must be non-empty text")
    if maximum is not None and len(value) > maximum:
        raise G3ExternalEvaluationError(f"{label} is too long")
    return value


def _decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool):
        raise G3ExternalEvaluationError(f"{label} must be a finite decimal")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise G3ExternalEvaluationError(f"{label} must be a finite decimal") from exc
    if not number.is_finite() or number < 0:
        raise G3ExternalEvaluationError(
            f"{label} must be a non-negative finite decimal"
        )
    return number


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise G3ExternalEvaluationError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise G3ExternalEvaluationError(f"{label} must be finite and non-negative")
    return number


def _timestamp(value: object, label: str) -> str:
    text = _text(value, label, maximum=64)
    try:
        from datetime import datetime, timezone

        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise G3ExternalEvaluationError(f"{label} is not an ISO-8601 time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise G3ExternalEvaluationError(f"{label} requires an explicit timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _observable_events(snapshot_time: str) -> tuple[Any, ...]:
    cutoff = _timestamp(snapshot_time, "sealed snapshot time")
    return tuple(
        event
        for event in all_events()
        if _timestamp(event.observed_time, "event observed time") <= cutoff
        and event.published_time is not None
        and _timestamp(event.published_time, "event publication time") <= cutoff
    )


def _validate_expected_answer(
    task: Mapping[str, Any], observable: Sequence[Any]
) -> dict[str, Any]:
    expected = task.get("expected_answer")
    if not isinstance(expected, Mapping):
        raise G3ExternalEvaluationError("external temporal expected answer is invalid")
    _exact_fields(expected, _EXPECTED_FIELDS, "external temporal expected answer")
    snapshot = _timestamp(task.get("snapshot_time"), "task snapshot time")
    as_of_value = task.get("as_of_time")
    as_of = (
        snapshot if as_of_value is None else _timestamp(as_of_value, "task as-of time")
    )
    if expected.get("schema_version") != ANSWER_SCHEMA:
        raise G3ExternalEvaluationError(
            "external temporal expected answer schema changed"
        )
    if expected.get("status") not in {"answer", "abstain"}:
        raise G3ExternalEvaluationError(
            "external temporal expected answer status is invalid"
        )
    evidence = expected.get("supporting_event_ids")
    if (
        not isinstance(evidence, list)
        or any(not isinstance(item, str) or not item for item in evidence)
        or len(evidence) != len(set(evidence))
        or expected.get("as_of_time") != as_of
        or expected.get("snapshot_time") != snapshot
    ):
        raise G3ExternalEvaluationError(
            "external temporal expected answer is malformed"
        )
    derived = (
        build_current_answer(
            observable,
            str(task["subject"]),
            str(task["predicate"]),
            observed_through=snapshot,
        )
        if as_of_value is None
        else build_as_of_answer(
            observable,
            str(task["subject"]),
            str(task["predicate"]),
            as_of,
            observed_through=snapshot,
        )
    ).to_record()
    if dict(expected) != derived:
        raise G3ExternalEvaluationError(
            "external temporal expected answer differs from deterministic replay"
        )
    return dict(expected)


def validate_external_temporal_bundle(
    bundle_path: Path,
    candidate_manifest_path: Path,
    *,
    root: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, str]]:
    """Validate both external inputs against public, content-free commitments."""

    repository = (root or repository_root()).resolve()
    bundle_file = _require_external_file(
        bundle_path, repository, "external temporal sealed bundle"
    )
    candidate_file = _require_external_file(
        candidate_manifest_path, repository, "external G3 candidate manifest"
    )
    bundle, bundle_bytes = _load_strict_json(
        bundle_file,
        "external temporal sealed bundle",
        anchor=bundle_file.parent,
    )
    candidate, candidate_bytes = _load_strict_json(
        candidate_file,
        "external G3 candidate manifest",
        anchor=candidate_file.parent,
    )
    if not isinstance(bundle, dict) or not isinstance(candidate, dict):
        raise G3ExternalEvaluationError("external G3 inputs must be JSON objects")
    try:
        validate_g3_sealed_candidate_manifest(candidate)
    except (G3SealedError, TypeError, KeyError, ValueError) as exc:
        raise G3ExternalEvaluationError(
            "external G3 candidate manifest is invalid"
        ) from exc
    _exact_fields(bundle, _BUNDLE_FIELDS, "external temporal sealed bundle")
    if bundle.get("schema") != EXTERNAL_BUNDLE_SCHEMA:
        raise G3ExternalEvaluationError("external temporal bundle schema changed")
    _text(bundle.get("authoring_version"), "bundle authoring version", maximum=128)
    history_hash = event_history_sha256()
    if (
        bundle.get("temporal_event_history_sha256") != history_hash
        or candidate.get("temporal_event_history_sha256") != history_hash
    ):
        raise G3ExternalEvaluationError("temporal event history commitment changed")
    bundle_sha = hashlib.sha256(bundle_bytes).hexdigest()
    if candidate.get("external_bundle_sha256") != bundle_sha:
        raise G3ExternalEvaluationError("external temporal bundle SHA-256 mismatch")
    rows = bundle.get("tasks")
    if not isinstance(rows, list) or len(rows) != len(G3_SEALED_TASK_IDS):
        raise G3ExternalEvaluationError(
            "external temporal bundle must contain exactly 12 tasks"
        )
    references = {str(row["task_id"]): row for row in sealed_temporal_references()}
    known_events = {event.event_id: event for event in all_events()}
    checked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, task in enumerate(rows):
        if not isinstance(task, dict):
            raise G3ExternalEvaluationError(
                f"external temporal task {index} is invalid"
            )
        _exact_fields(task, _TASK_FIELDS, f"external temporal task {index}")
        task_id = task.get("task_id")
        reference = references.get(str(task_id))
        if (
            not isinstance(task_id, str)
            or task_id in seen
            or reference is None
            or any(
                task.get(field) != reference.get(field)
                for field in ("scenario_id", "task_family", "difficulty")
            )
        ):
            raise G3ExternalEvaluationError(
                f"external temporal task {index} identity changed or is duplicated"
            )
        seen.add(task_id)
        _text(task.get("question_text"), f"{task_id} question text")
        _text(task.get("subject"), f"{task_id} subject", maximum=256)
        _text(task.get("predicate"), f"{task_id} predicate", maximum=256)
        _text(task.get("scoring_notes"), f"{task_id} scoring notes")
        snapshot = _timestamp(task.get("snapshot_time"), f"{task_id} snapshot time")
        if task.get("as_of_time") is not None:
            _timestamp(task.get("as_of_time"), f"{task_id} as-of time")
        observable = _observable_events(snapshot)
        if not observable:
            raise G3ExternalEvaluationError(f"{task_id} snapshot has no visible events")
        _validate_expected_answer(task, observable)
        required = task.get("required_evidence")
        observable_ids = {event.event_id for event in observable}
        assertions = {
            event.event_id
            for event in known_events.values()
            if event.scenario_id == task["scenario_id"]
            and event.subject == task["subject"]
            and event.predicate == task["predicate"]
        }
        relevant_ids = assertions | {
            event.event_id
            for event in known_events.values()
            if event.scenario_id == task["scenario_id"]
            and event.tombstone_for_event_id in assertions
        }
        if (
            not isinstance(required, list)
            or any(not isinstance(item, str) or not item for item in required)
            or len(required) != len(set(required))
            or not assertions
            or not required
            or any(
                item not in known_events
                or item not in observable_ids
                or item not in relevant_ids
                for item in required
            )
        ):
            raise G3ExternalEvaluationError(
                f"{task_id} required evidence is invalid or not observable"
            )
        supporting = set(task["expected_answer"]["supporting_event_ids"])
        if not supporting.issubset(set(required)):
            raise G3ExternalEvaluationError(
                f"{task_id} required evidence omits answer provenance"
            )
        checked.append(dict(task))
    if tuple(sorted(seen)) != G3_SEALED_TASK_IDS:
        raise G3ExternalEvaluationError("external temporal task IDs are incomplete")
    checked.sort(key=lambda row: str(row["task_id"]))
    return (
        checked,
        candidate,
        {
            "external_bundle_sha256": bundle_sha,
            "candidate_manifest_file_sha256": hashlib.sha256(
                candidate_bytes
            ).hexdigest(),
            "candidate_manifest_sha256": str(candidate["artifact_sha256"]),
            "g3_freeze_sha256": str(candidate["g3_freeze_sha256"]),
            "temporal_event_history_sha256": history_hash,
        },
    )


# Keep the expected standalone spelling convenient without weakening the G3 name.
validate_external_sealed_bundle = validate_external_temporal_bundle


def _assert_content_free(value: object, path: str = "return") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or _FORBIDDEN_CONTENT_KEY.search(key):
                raise G3ExternalEvaluationError(
                    f"content-free G3 return contains a forbidden field at {path}"
                )
            _assert_content_free(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_content_free(item, f"{path}[{index}]")
    elif isinstance(value, str) and len(value) > 256:
        raise G3ExternalEvaluationError(
            "content-free G3 return contains an unbounded text value"
        )


def _validate_progress_report(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise G3ExternalEvaluationError("external G3 progress report is not an object")
    _exact_fields(value, _PROGRESS_FIELDS, "external G3 progress report")
    counts = {
        key: value.get(key)
        for key in (
            "cell_count",
            "completed_count",
            "failed_count",
            "missing_count",
            "new_call_count",
            "remaining_count",
        )
    }
    if any(
        isinstance(item, bool) or not isinstance(item, int) or item < 0
        for item in counts.values()
    ):
        raise G3ExternalEvaluationError("external G3 progress counts are invalid")
    status = value.get("status")
    sealed_return_sha = value.get("sealed_return_sha256")
    evaluation_id = value.get("evaluation_id")
    if (
        value.get("schema_version") != G3_EXTERNAL_PROGRESS_SCHEMA
        or not isinstance(evaluation_id, str)
        or _IDENTIFIER.fullmatch(evaluation_id) is None
        or value.get("evaluator_version") != G3_EXTERNAL_EVALUATOR_VERSION
        or counts["cell_count"] != G3_SEALED_CELL_COUNT
        or counts["completed_count"]
        + counts["failed_count"]
        + counts["remaining_count"]
        != counts["cell_count"]
        or counts["new_call_count"] > counts["missing_count"]
        or counts["missing_count"] > counts["cell_count"]
        or counts["remaining_count"] > counts["missing_count"]
        or status not in {"completed", "partial"}
        or (status == "completed") != (counts["remaining_count"] == 0)
        or (status == "completed") != (sealed_return_sha is not None)
    ):
        raise G3ExternalEvaluationError("external G3 progress report is inconsistent")
    for key in (
        "candidate_manifest_sha256",
        "g3_freeze_sha256",
        "external_bundle_sha256",
        "temporal_event_history_sha256",
        "artifact_sha256",
    ):
        _sha(value.get(key), f"external G3 progress {key}")
    if sealed_return_sha is not None:
        _sha(sealed_return_sha, "external G3 sealed return hash")
    _decimal(value.get("actual_usd"), "external G3 progress actual_usd")
    body = {key: item for key, item in value.items() if key != "artifact_sha256"}
    if value.get("artifact_sha256") != sha256_json(body):
        raise G3ExternalEvaluationError("external G3 progress hash is invalid")
    _assert_content_free(value, "progress")
    return dict(value)


def _progress_report(
    *,
    evaluation_id: str,
    candidate: Mapping[str, Any],
    commitments: Mapping[str, str],
    completed_count: int,
    failed_count: int,
    missing_count: int,
    new_call_count: int,
    remaining_count: int,
    actual_usd: Decimal,
    sealed_return_sha256: str | None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema_version": G3_EXTERNAL_PROGRESS_SCHEMA,
        "evaluation_id": evaluation_id,
        "candidate_manifest_sha256": candidate["artifact_sha256"],
        "g3_freeze_sha256": commitments["g3_freeze_sha256"],
        "external_bundle_sha256": commitments["external_bundle_sha256"],
        "temporal_event_history_sha256": commitments["temporal_event_history_sha256"],
        "cell_count": G3_SEALED_CELL_COUNT,
        "completed_count": completed_count,
        "failed_count": failed_count,
        "missing_count": missing_count,
        "new_call_count": new_call_count,
        "remaining_count": remaining_count,
        "actual_usd": format(actual_usd, "f"),
        "evaluator_version": G3_EXTERNAL_EVALUATOR_VERSION,
        "status": "completed" if remaining_count == 0 else "partial",
        "sealed_return_sha256": sealed_return_sha256,
    }
    report["artifact_sha256"] = sha256_json(report)
    return _validate_progress_report(report)


def _write_progress_report(campaign_root: Path, report: Mapping[str, Any]) -> None:
    path = _external_path(campaign_root, Path("g3_external_progress.json"))
    _atomic_write_json(campaign_root, path, _validate_progress_report(report))


def load_g3_external_progress(
    work_root: Path,
    *,
    evaluation_id: str = "g3-sealed-v1",
    root: Path | None = None,
) -> dict[str, Any]:
    """Load the content-free progress report for one external G3 campaign."""

    repository = (root or repository_root()).resolve()
    if _IDENTIFIER.fullmatch(evaluation_id) is None:
        raise G3ExternalEvaluationError("external G3 evaluation ID is invalid")
    external_root = _require_external_work_root(work_root, repository)
    campaign_path = external_root / evaluation_id
    if (
        campaign_path.is_symlink()
        or not campaign_path.is_dir()
        or not _is_relative_to(campaign_path.resolve(), external_root)
    ):
        raise G3ExternalEvaluationError("external G3 campaign root is invalid")
    campaign_root = campaign_path.resolve()
    path = _external_path(campaign_root, Path("g3_external_progress.json"))
    value, _ = _load_strict_json(
        path, "external G3 progress report", anchor=campaign_root
    )
    return _validate_progress_report(value)


def _validate_cell_outcome(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise G3ExternalEvaluationError("external cell evaluator returned no outcome")
    _exact_fields(value, _OUTCOME_FIELDS, "external G3 cell outcome")
    replay = value.get("replay_bindings")
    if not isinstance(replay, Mapping):
        raise G3ExternalEvaluationError("external G3 replay bindings are missing")
    _exact_fields(replay, _REPLAY_FIELDS, "external G3 replay bindings")
    canonical_replay = {
        key: _sha(replay.get(key), key) for key in sorted(_REPLAY_FIELDS)
    }
    status = value.get("status")
    response_status = value.get("response_status")
    labels = value.get("failure_labels")
    if (
        status not in {"completed", "failed"}
        or response_status not in {"answer", "abstain", "error"}
        or not all(
            isinstance(value.get(field), bool)
            for field in ("is_correct", "stale", "provenance_complete")
        )
        or value.get("is_correct") is True
        and value.get("stale") is True
        or not isinstance(labels, list)
        or any(not isinstance(label, str) for label in labels)
        or len(labels) != len(set(labels))
        or any(label not in _FAILURE_LABELS for label in labels)
    ):
        raise G3ExternalEvaluationError("external G3 cell outcome is invalid")
    latency = value.get("correction_latency")
    if latency is not None:
        _number(latency, "external correction latency")
    cost = _decimal(value.get("actual_usd"), "external actual_usd")
    wall_latency = _number(value.get("latency_ms"), "external latency_ms")
    if status == "completed":
        if response_status == "error":
            raise G3ExternalEvaluationError(
                "completed external cell has an error status"
            )
    elif (
        response_status != "error"
        or value.get("is_correct") is not False
        or value.get("stale") is not False
        or value.get("provenance_complete") is not False
        or latency is not None
        or not labels
    ):
        raise G3ExternalEvaluationError("failed external cell fabricates an outcome")
    return {
        "status": status,
        "response_status": response_status,
        "is_correct": bool(value["is_correct"]),
        "stale": bool(value["stale"]),
        "provenance_complete": bool(value["provenance_complete"]),
        "correction_latency": latency,
        "actual_usd": format(cost, "f"),
        "latency_ms": wall_latency,
        "failure_labels": list(labels),
        "replay_bindings": canonical_replay,
    }


def _record_from_outcome(
    cell: Mapping[str, Any],
    outcome: Mapping[str, Any],
    *,
    commitments: Mapping[str, str],
) -> dict[str, Any]:
    checked = _validate_cell_outcome(outcome)
    commitment = {
        "schema_version": "contextlab.g3-external-result-commitment.v1",
        "candidate_manifest_sha256": commitments["candidate_manifest_sha256"],
        "candidate_manifest_file_sha256": commitments["candidate_manifest_file_sha256"],
        "g3_freeze_sha256": commitments["g3_freeze_sha256"],
        "external_bundle_sha256": commitments["external_bundle_sha256"],
        "temporal_event_history_sha256": commitments["temporal_event_history_sha256"],
        "cell": dict(cell),
        "outcome": {
            key: checked[key]
            for key in (
                "status",
                "response_status",
                "is_correct",
                "stale",
                "provenance_complete",
                "correction_latency",
                "actual_usd",
                "latency_ms",
                "failure_labels",
            )
        },
        "replay_bindings": checked["replay_bindings"],
    }
    return {
        **dict(cell),
        "result_commitment_sha256": sha256_json(commitment),
        **{
            key: checked[key]
            for key in (
                "status",
                "response_status",
                "is_correct",
                "stale",
                "provenance_complete",
                "correction_latency",
                "actual_usd",
                "latency_ms",
                "failure_labels",
            )
        },
    }


def _sealed_ledger_secret(campaign_root: Path) -> bytes:
    destination = _external_path(
        campaign_root, Path("sealed_ledger_reservation_hmac.key")
    )

    def read_existing() -> bytes:
        try:
            value = read_bytes_snapshot(campaign_root, destination, max_bytes=32)
        except ImmutableIOError as exc:
            raise G3ExternalEvaluationError(
                "external G3 ledger HMAC key is invalid"
            ) from exc
        if len(value) != 32:
            raise G3ExternalEvaluationError("external G3 ledger HMAC key is invalid")
        return value

    try:
        return read_existing()
    except G3ExternalEvaluationError:
        pass
    value = secrets.token_bytes(32)
    try:
        created = write_bytes_once_or_verify(campaign_root, destination, value)
    except ImmutableIOError:
        # A concurrent process may have won with a different random key. Read
        # that exact regular file; unsafe targets still fail closed.
        return read_existing()
    return value if created else read_existing()


def _opaque_reservation_id(secret: bytes, cell_sha256: str) -> str:
    return (
        "g3-sealed-"
        + hmac.new(secret, cell_sha256.encode("ascii"), hashlib.sha256).hexdigest()
    )


def _event_chunks_at(snapshot_time: str) -> list[dict[str, Any]]:
    visible = {event.event_id for event in _observable_events(snapshot_time)}
    return [
        chunk for chunk in temporal_event_chunks() if str(chunk["chunk_id"]) in visible
    ]


def _prompt_task(task: Mapping[str, Any]) -> dict[str, str]:
    question = str(task["question_text"])
    return {
        "schema_version": "contextlab.prompt-task.v1",
        "task_id": str(task["task_id"]),
        "suite": "temporal",
        "question_text": question,
        "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
    }


def _load_freeze(repository: Path, candidate: Mapping[str, Any]) -> dict[str, Any]:
    path = repository / "results/v2/memory/g3_public_freeze.json"
    value, _ = _load_strict_json(path, "canonical G3 freeze", anchor=repository)
    if not isinstance(value, dict):
        raise G3ExternalEvaluationError("canonical G3 freeze must be an object")
    try:
        validate_g3_freeze(value)
    except ValueError as exc:
        raise G3ExternalEvaluationError("canonical G3 freeze is invalid") from exc
    if value.get("artifact_sha256") != candidate.get("g3_freeze_sha256"):
        raise G3ExternalEvaluationError("external candidate uses another G3 freeze")
    manifest = value.get("manifest")
    if not isinstance(manifest, dict):
        raise G3ExternalEvaluationError("canonical G3 freeze has no manifest")
    try:
        validate_memory_experiment_manifest(
            manifest,
            trusted_frozen_manifest_sha256=str(manifest["frozen_manifest_sha256"]),
        )
    except ValueError as exc:
        raise G3ExternalEvaluationError("canonical G3 manifest is invalid") from exc
    return value


def _default_embeddings(
    questions: Sequence[str],
    *,
    campaign_root: Path,
    repository: Path,
    protocol: Mapping[str, Any],
    ledger: CostLedger,
    environment: Mapping[str, str] | None,
) -> Mapping[str, Sequence[float]]:
    base = (
        repository / "evaluation/build/embeddings_openai_text-embedding-3-small.jsonl"
    )
    external_extension = _external_path(
        campaign_root, Path("sealed_query_embeddings.jsonl")
    )
    expected_base = str(protocol["dense_control"]["base_cache_sha256"])
    public_extension = repository / "results/v2/embeddings/g3_temporal_embeddings.jsonl"
    base_rows = load_embedding_cache(base, expected_base_sha256=expected_base)
    public = load_extension_cache(public_extension) if public_extension.exists() else {}
    if set(base_rows).intersection(public):
        raise G3ExternalEvaluationError("public G3 embedding caches overlap")
    available = {**base_rows, **public}
    missing = [
        question for question in questions if embedding_key(question) not in available
    ]
    if missing:
        embed_texts(
            missing,
            base_cache_path=base,
            extension_cache_path=external_extension,
            expected_base_sha256=expected_base,
            ledger=_ContentFreeEmbeddingLedger(ledger),
            run_id="g3-sealed-query-embeddings-v1",
            environment=environment,
            root=repository,
        )
    external = load_extension_cache(external_extension)
    overlap = set(base_rows).intersection(public) | set(base_rows).intersection(
        external
    )
    if overlap or set(public).intersection(external):
        raise G3ExternalEvaluationError("G3 embedding caches overlap")
    return {**base_rows, **public, **external}


def _validate_r0_trace(
    trace: Mapping[str, Any],
    *,
    task: Mapping[str, Any],
    chunks: Sequence[Mapping[str, Any]],
    protocol_sha256: str,
) -> None:
    prompt_task = _prompt_task(task)
    selected = trace.get("selected_candidates")
    passages = trace.get("candidate_passages")
    rendered = trace.get("rendered_context")
    visible = {str(chunk["chunk_id"]) for chunk in chunks}
    if (
        trace.get("schema_version") != TRACE_SCHEMA
        or trace.get("task") != prompt_task
        or trace.get("strategy_id") != "R0"
        or trace.get("protocol_sha256") != protocol_sha256
        or trace.get("corpus_snapshot_id") != sha256_json(list(chunks))
        or not isinstance(selected, list)
        or not selected
        or any(not isinstance(row, Mapping) for row in selected)
        or not isinstance(passages, Mapping)
        or not isinstance(rendered, str)
        or trace.get("rendered_context_sha256")
        != hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        or trace.get("context_tokens") != estimate_tokens(rendered)
        or {str(row.get("candidate_id")) for row in selected} != set(passages)
        or not {
            str(row.get("section_id") or row.get("source_id")) for row in selected
        }.issubset(visible)
    ):
        raise G3ExternalEvaluationError("external sealed R0 trace is invalid")


def _r0_trace(
    task: Mapping[str, Any],
    *,
    campaign_root: Path,
    embeddings: Mapping[str, Sequence[float]],
    protocol: Mapping[str, Any],
    retrieval_runner: RetrievalRunner,
) -> dict[str, Any]:
    path = _external_path(
        campaign_root, Path("retrieval_traces") / f"{task['task_id']}.json"
    )
    chunks = _event_chunks_at(str(task["snapshot_time"]))
    protocol_sha = sha256_json(protocol)
    if path.exists():
        value, _ = _load_strict_json(
            path, "external sealed R0 trace", anchor=campaign_root
        )
        if not isinstance(value, dict):
            raise G3ExternalEvaluationError("external sealed R0 trace is not an object")
        _validate_r0_trace(
            value, task=task, chunks=chunks, protocol_sha256=protocol_sha
        )
        return value
    generated = retrieval_runner(
        _prompt_task(task),
        chunks,
        embeddings,
        (),
        protocol,
        corpus_snapshot_id=sha256_json(chunks),
    )
    matches = [row for row in generated if row.get("strategy_id") == "R0"]
    if len(matches) != 1:
        raise G3ExternalEvaluationError("retrieval runner did not produce one R0 trace")
    trace = dict(matches[0])
    _validate_r0_trace(trace, task=task, chunks=chunks, protocol_sha256=protocol_sha)
    _atomic_write_json(campaign_root, path, trace)
    return trace


def _run_spec(
    task: Mapping[str, Any],
    cell: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    policy = str(cell["policy"])
    spec: dict[str, Any] = {
        "schema_version": MEMORY_RUN_SPEC_SCHEMA,
        "run_id": cell["run_id"],
        "campaign_id": "g3-sealed",
        "policy": policy,
        "reasoning_effort": cell["reasoning_effort"],
        "task": {
            **_prompt_task(task),
            "task_family": task["task_family"],
        },
        "retriever_binding_sha256": manifest["retriever_binding_sha256"],
        "acceptance_parameters_sha256": manifest["acceptance_parameters_sha256"],
        "generation_protocol_sha256": manifest["generation_protocol_sha256"],
        "requested_model": manifest["requested_model"],
        "provider": manifest["provider"],
        "prompt_version": manifest["prompt_version"],
        "prompt_sha256": manifest["prompt_sha256"],
        "corpus_snapshot_sha256": manifest["corpus_snapshot_sha256"],
        "output_token_limit": manifest["output_token_limit"],
        "context_budget_tokens": manifest["context_budget_tokens"],
        "available_raw_evidence_ids_sha256": manifest[
            "available_raw_evidence_ids_sha256"
        ],
        "trusted_grade_artifacts_sha256": manifest["trusted_grade_artifacts_sha256"],
        "m4_episode_seed_sha256": (
            manifest["m4_episode_seed_sha256"] if policy == "M4" else None
        ),
        "m4_episode_seed_count": (
            len(manifest["m4_episode_seed"]) if policy == "M4" else 0
        ),
    }
    spec["run_spec_sha256"] = sha256_json(spec)
    return spec


def _verification_evidence(
    task: Mapping[str, Any], trace: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    rows, blocks = trace_corpus_evidence(trace)
    represented = {
        str(raw_id) for row in rows for raw_id in row.get("raw_evidence_ids", [])
    }
    for rank, chunk in enumerate(
        _event_chunks_at(str(task["snapshot_time"])), start=1_000
    ):
        raw_id = str(chunk["section_id"])
        if raw_id in represented:
            continue
        evidence_id = f"raw-{raw_id}"
        reference = f"{chunk['source_id']}#{raw_id}"
        block = f"[{reference}]\n{chunk['text']}"
        rows.append(
            {
                "evidence_id": evidence_id,
                "token_count": max(1, estimate_tokens(block)),
                "rank": rank,
                "raw_evidence_ids": [raw_id],
            }
        )
        blocks[evidence_id] = block
        represented.add(raw_id)
    return rows, blocks


def _episode_blocks(manifest: Mapping[str, Any]) -> dict[str, str]:
    blocks: dict[str, str] = {}
    for seed in manifest["m4_episode_seed"]:
        evidence = " ".join(f"[{item}]" for item in seed["evidence_path"])
        blocks[str(seed["episode_id"])] = (
            f"[{seed['episode_id']}] Prior graded episode\n"
            f"Task family: {seed['task_family']}\n"
            f"Selected strategy: {seed['selected_strategy']}\n"
            f"Objective outcome: {seed['grade_outcome']}\n"
            f"Raw trace evidence: {evidence}\n"
            "Use this card only as strategy guidance; do not treat it as a current fact."
        )
    return blocks


def _prepare_cell(
    task: Mapping[str, Any],
    cell: Mapping[str, Any],
    *,
    candidate: Mapping[str, Any],
    commitments: Mapping[str, str],
    manifest: Mapping[str, Any],
    trace: Mapping[str, Any],
    campaign_root: Path,
    instruction: str,
) -> dict[str, Any]:
    spec = _run_spec(task, cell, manifest)
    visible = _observable_events(str(task["snapshot_time"]))
    engine = MemoryEngine.rebuilt(str(cell["policy"]), visible)
    as_of = task.get("as_of_time") or task["snapshot_time"]
    read = engine.read(
        str(task["subject"]),
        str(task["predicate"]),
        observed_through=str(task["snapshot_time"]),
        as_of_time=str(as_of),
        task_family=str(task["task_family"]),
        task_signature=sha256_json(
            {
                "suite": "temporal",
                "task_family": task["task_family"],
                "question_sha256": spec["task"]["question_sha256"],
            }
        ),
        query_text=str(task["question_text"]),
    )
    corpus_rows, corpus_blocks = _verification_evidence(task, trace)
    memory_rows, memory_blocks = memory_read_evidence(read)
    memory_trace = build_memory_trace(
        spec,
        corpus_evidence=corpus_rows,
        memory_evidence=memory_rows,
        m4_episode_seed=(manifest["m4_episode_seed"] if cell["policy"] == "M4" else ()),
        available_raw_evidence_ids=manifest["available_raw_evidence_ids"],
        trusted_grade_artifacts=manifest["trusted_grade_artifacts"],
    )
    rendered = render_selected_context(
        memory_trace,
        corpus_blocks=corpus_blocks,
        memory_blocks=memory_blocks,
        episode_blocks=_episode_blocks(manifest),
    )
    generation_spec = build_generation_spec(
        {
            "task": _prompt_task(task),
            "strategy_id": cell["policy"],
            "rendered_context": rendered,
        },
        str(cell["reasoning_effort"]),
        trial=1,
        max_tokens=int(manifest["output_token_limit"]),
        temperature=0.0,
        system_instruction=instruction,
        campaign_id="g3-sealed",
    )
    generation_spec["run_id"] = cell["run_id"]
    validate_generation_spec(generation_spec)
    snapshot = engine.snapshot_record()
    decisions = [decision.to_record() for decision in engine.decision_ledger]
    prepared: dict[str, Any] = {
        "schema_version": "contextlab.g3-external-prepared-cell.v1",
        "candidate_manifest_sha256": candidate["artifact_sha256"],
        "g3_freeze_sha256": commitments["g3_freeze_sha256"],
        "external_bundle_sha256": commitments["external_bundle_sha256"],
        "temporal_event_history_sha256": commitments["temporal_event_history_sha256"],
        "cell": dict(cell),
        "task": dict(task),
        "task_sha256": sha256_json(task),
        "run_spec": spec,
        "source_r0_trace": dict(trace),
        "source_r0_trace_sha256": sha256_json(trace),
        "observable_event_ids": [event.event_id for event in visible],
        "memory_read": read.to_record(),
        "memory_snapshot": snapshot,
        "decision_ledger": decisions,
        "memory_trace": memory_trace,
        "rendered_context": rendered,
        "rendered_context_sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        "generation_spec": generation_spec,
    }
    prepared["artifact_sha256"] = sha256_json(prepared)
    path = _external_path(
        campaign_root, Path("prepared_cells") / f"{cell['cell_sha256']}.json"
    )
    if path.exists():
        saved, _ = _load_strict_json(
            path, "external prepared G3 cell", anchor=campaign_root
        )
        if saved != prepared:
            raise G3ExternalEvaluationError("external prepared G3 cell changed")
    else:
        _atomic_write_json(campaign_root, path, prepared)
    return prepared


def _normalized_text(value: object) -> str:
    import unicodedata

    text = (
        value
        if isinstance(value, str)
        else json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    )
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _contains_value(answer_body: str, value: object) -> bool:
    needle = _normalized_text(value)
    haystack = _normalized_text(_CITATION.sub(" ", answer_body))
    return bool(
        needle
        and re.search(rf"(?<![\w]){re.escape(needle)}(?![\w])", haystack, re.UNICODE)
    )


def _relevant_events(task: Mapping[str, Any], prepared: Mapping[str, Any]) -> list[Any]:
    visible = set(prepared["observable_event_ids"])
    assertions = [
        event
        for event in all_events()
        if event.event_id in visible
        and event.subject == task["subject"]
        and event.predicate == task["predicate"]
    ]
    assertion_ids = {event.event_id for event in assertions}
    lifecycle = [
        event
        for event in all_events()
        if event.event_id in visible and event.tombstone_for_event_id in assertion_ids
    ]
    return sorted(
        {event.event_id: event for event in (*assertions, *lifecycle)}.values(),
        key=lambda event: (
            _timestamp(event.observed_time, "event time"),
            event.event_id,
        ),
    )


def _stale_answer(
    body: str,
    response_status: str,
    expected: Mapping[str, Any],
    relevant: Sequence[Any],
) -> bool:
    if response_status != "answer":
        return False
    expected_ids = set(expected["supporting_event_ids"])
    expected_index = max(
        (
            index
            for index, event in enumerate(relevant)
            if event.event_id in expected_ids
        ),
        default=len(relevant),
    )
    return any(
        index < expected_index
        and event.status in {"draft", "final", "corrected"}
        and (
            expected["value"] is None
            or _normalized_text(event.value) != _normalized_text(expected["value"])
        )
        and _contains_value(body, event.value)
        for index, event in enumerate(relevant)
    )


def _provenance_complete(
    footer: Mapping[str, Any],
    prepared: Mapping[str, Any],
    required: Sequence[str],
) -> bool:
    citations = set(footer["citations"])
    selected = {
        str(row["claim_id"]): row
        for row in prepared["memory_trace"]["selected_memory_evidence"]
    }
    selected_corpus_raw = {
        str(raw_id)
        for row in prepared["memory_trace"]["selected_corpus_evidence"]
        for raw_id in row["raw_evidence_ids"]
    }

    def cites(raw_id: str) -> bool:
        return any(
            value == raw_id or value.endswith(f"#{raw_id}") for value in citations
        )

    for claim_id in footer["used_memory_claim_ids"]:
        claim = selected.get(str(claim_id))
        if claim is None or claim_id not in citations:
            return False
        raw_ids = {str(item) for item in claim["raw_evidence_ids"]}
        if (
            not raw_ids
            or not raw_ids.issubset(selected_corpus_raw)
            or not all(cites(raw_id) for raw_id in raw_ids)
        ):
            return False
    return all(cites(str(raw_id)) for raw_id in required)


def _correction_latency(
    *,
    correct: bool,
    stale: bool,
    expected: Mapping[str, Any],
    relevant: Sequence[Any],
) -> float | None:
    if correct:
        return 0.0
    if not stale:
        return None
    determining = set(expected["supporting_event_ids"])
    if not determining:
        determining = {
            event.event_id
            for event in relevant
            if event.status in {"expired", "retracted", "tombstone"}
        }
    indices = [
        index for index, event in enumerate(relevant) if event.event_id in determining
    ]
    return float(max(1, len(relevant) - 1 - max(indices))) if indices else 1.0


def _completed_outcome(
    task: Mapping[str, Any],
    prepared: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    campaign_root: Path,
) -> dict[str, Any]:
    selected_claim_ids = sorted(
        str(row["claim_id"])
        for row in prepared["memory_trace"]["selected_memory_evidence"]
    )
    footer_valid = True
    try:
        footer = parse_answer_footer(str(result["answer"]), selected_claim_ids)
    except ValueError:
        footer_valid = False
        try:
            footer = _recover_invalid_temporal_footer(
                str(result["answer"]), selected_claim_ids
            )
        except ValueError as exc:
            raise G3ExternalEvaluationError(
                "external sealed answer footer is invalid"
            ) from exc
    expected = task["expected_answer"]
    expected_status = str(expected["status"])
    response_status = str(footer["answer_status"])
    if expected_status == "answer":
        correct = (
            footer_valid
            and response_status == "answer"
            and _contains_value(str(footer["body"]), expected["value"])
        )
    else:
        correct = (
            footer_valid
            and response_status == "abstain"
            and bool(_INSUFFICIENT.search(str(footer["body"])))
        )
    relevant = _relevant_events(task, prepared)
    stale = (
        False
        if correct
        else _stale_answer(str(footer["body"]), response_status, expected, relevant)
    )
    provenance = footer_valid and _provenance_complete(
        footer, prepared, task["required_evidence"]
    )
    labels: list[str] = []
    if not correct:
        if stale:
            labels.append("stale_value")
        elif response_status != expected_status:
            labels.append("bad_abstention")
        else:
            labels.append("wrong_value")
    if not provenance:
        labels.append("missing_provenance")
    metadata = result["metadata"]
    usage = {
        key: metadata.get(key)
        for key in (
            "request_id",
            "requested_model",
            "resolved_model",
            "provider",
            "reasoning_effort",
            "prompt_tokens",
            "completion_tokens",
            "native_prompt_tokens",
            "native_completion_tokens",
            "native_reasoning_tokens",
            "actual_usd",
            "latency_ms",
            "retry_count",
        )
    }
    grade: dict[str, Any] = {
        "schema_version": "contextlab.g3-external-grade.v1",
        "cell_sha256": prepared["cell"]["cell_sha256"],
        "prepared_cell_sha256": prepared["artifact_sha256"],
        "generation_result_sha256": sha256_json(result),
        "answer_sha256": hashlib.sha256(
            str(result["answer"]).encode("utf-8")
        ).hexdigest(),
        "expected_answer_sha256": sha256_json(expected),
        "required_evidence_sha256": sha256_json(task["required_evidence"]),
        "response_status": response_status,
        "is_correct": correct,
        "stale": stale,
        "provenance_complete": provenance,
        "correction_latency": _correction_latency(
            correct=correct, stale=stale, expected=expected, relevant=relevant
        ),
        "failure_labels": labels,
        "usage_sha256": sha256_json(usage),
    }
    grade["artifact_sha256"] = sha256_json(grade)
    grade_path = _external_path(
        campaign_root,
        Path("grades") / f"{prepared['cell']['cell_sha256']}.json",
    )
    if grade_path.exists():
        saved, _ = _load_strict_json(
            grade_path, "external G3 grade", anchor=campaign_root
        )
        if saved != grade:
            raise G3ExternalEvaluationError("external G3 grade changed")
    else:
        _atomic_write_json(campaign_root, grade_path, grade)
    return {
        "status": "completed",
        "response_status": response_status,
        "is_correct": correct,
        "stale": stale,
        "provenance_complete": provenance,
        "correction_latency": grade["correction_latency"],
        "actual_usd": metadata["actual_usd"],
        "latency_ms": metadata["latency_ms"],
        "failure_labels": labels,
        "replay_bindings": {
            "prepared_cell_sha256": prepared["artifact_sha256"],
            "run_spec_sha256": prepared["run_spec"]["run_spec_sha256"],
            "trace_sha256": prepared["memory_trace"]["trace_sha256"],
            "memory_snapshot_sha256": sha256_json(prepared["memory_snapshot"]),
            "decision_ledger_sha256": sha256_json(prepared["decision_ledger"]),
            "generation_spec_sha256": sha256_json(prepared["generation_spec"]),
            "generation_result_sha256": sha256_json(result),
            "grade_sha256": grade["artifact_sha256"],
            "usage_sha256": grade["usage_sha256"],
        },
    }


def _failure_accounting(
    ledger: CostLedger, reservation_id: str
) -> tuple[str, float, str]:
    """Read only the permitted accounting facts for one opaque failed call."""

    try:
        with ledger.path.open("r", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                rows = [json.loads(line) for line in handle if line.strip()]
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, json.JSONDecodeError) as exc:
        raise G3ExternalEvaluationError(
            "cannot replay external G3 failure accounting"
        ) from exc
    matching = [
        row
        for row in rows
        if isinstance(row, Mapping) and row.get("reservation_id") == reservation_id
    ]
    cost: object = "0"
    latency: object = 0.0
    for row in matching:
        metadata = row.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        if row.get("event") == "settle":
            cost = row.get("actual_usd", cost)
        elif metadata.get("actual_usd") is not None:
            cost = metadata["actual_usd"]
        elif metadata.get("reported_cost") is not None:
            cost = metadata["reported_cost"]
        if metadata.get("latency_ms") is not None:
            latency = metadata["latency_ms"]
        elif metadata.get("local_round_trip_ms") is not None:
            latency = metadata["local_round_trip_ms"]
    return (
        format(_decimal(cost, "failed external actual_usd"), "f"),
        _number(latency, "failed external latency_ms"),
        sha256_json(matching),
    )


def _failed_outcome(
    prepared: Mapping[str, Any],
    failure: Mapping[str, Any],
    *,
    actual_usd: str,
    latency_ms: float,
    usage_sha256: str,
) -> dict[str, Any]:
    failure_sha = sha256_json(failure)
    failure_grade = sha256_json(
        {
            "schema_version": "contextlab.g3-external-failed-grade.v1",
            "cell_sha256": prepared["cell"]["cell_sha256"],
            "generation_failure_sha256": failure_sha,
        }
    )
    return {
        "status": "failed",
        "response_status": "error",
        "is_correct": False,
        "stale": False,
        "provenance_complete": False,
        "correction_latency": None,
        "actual_usd": actual_usd,
        "latency_ms": latency_ms,
        "failure_labels": ["provider_failure"],
        "replay_bindings": {
            "prepared_cell_sha256": prepared["artifact_sha256"],
            "run_spec_sha256": prepared["run_spec"]["run_spec_sha256"],
            "trace_sha256": prepared["memory_trace"]["trace_sha256"],
            "memory_snapshot_sha256": sha256_json(prepared["memory_snapshot"]),
            "decision_ledger_sha256": sha256_json(prepared["decision_ledger"]),
            "generation_spec_sha256": sha256_json(prepared["generation_spec"]),
            "generation_result_sha256": failure_sha,
            "grade_sha256": failure_grade,
            "usage_sha256": usage_sha256,
        },
    }


def _generation_artifact_paths(
    campaign_root: Path, cell: Mapping[str, Any]
) -> tuple[Path, Path]:
    cell_sha = str(cell["cell_sha256"])
    result_path = _external_path(
        campaign_root, Path("generation_results") / f"{cell_sha}.json"
    )
    attempt_path = _external_path(
        campaign_root, Path("generation_attempts") / f"{cell_sha}.json"
    )
    return result_path, attempt_path


def _attempt_record(cell: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "contextlab.g3-external-generation-attempt.v1",
        "run_id": cell["run_id"],
        "cell_sha256": cell["cell_sha256"],
    }


def _validate_attempt_marker(
    path: Path, cell: Mapping[str, Any], *, campaign_root: Path
) -> None:
    value, _ = _load_strict_json(
        path, "external G3 generation attempt", anchor=campaign_root
    )
    if not isinstance(value, Mapping):
        raise G3ExternalEvaluationError(
            "external G3 generation attempt is not an object"
        )
    _exact_fields(value, _ATTEMPT_FIELDS, "external G3 generation attempt")
    if value != _attempt_record(cell):
        raise G3ExternalEvaluationError("external G3 generation attempt changed")


def _prepared_generation_state(prepared: Mapping[str, Any], campaign_root: Path) -> str:
    cell = prepared["cell"]
    result_path, attempt_path = _generation_artifact_paths(campaign_root, cell)
    if attempt_path.exists():
        _validate_attempt_marker(attempt_path, cell, campaign_root=campaign_root)
    if not result_path.exists():
        return "interrupted" if attempt_path.exists() else "missing"
    result, _ = _load_strict_json(
        result_path, "external G3 generation result", anchor=campaign_root
    )
    if not isinstance(result, Mapping) or result.get("run_id") != cell.get("run_id"):
        raise G3ExternalEvaluationError("external G3 generation identity changed")
    schema = result.get("schema_version")
    if schema in {
        "contextlab.generation-result.v1",
        "contextlab.failed-generation-result.v1",
    }:
        return "recorded"
    if schema == "contextlab.pending-generation-result.v1":
        return "interrupted"
    raise G3ExternalEvaluationError("external G3 generation result schema changed")


def _execute_prepared(
    task: Mapping[str, Any],
    prepared: Mapping[str, Any],
    *,
    campaign_root: Path,
    repository: Path,
    ledger: CostLedger,
    environment: Mapping[str, str] | None,
    generation_runner: GenerationRunner,
    ledger_secret: bytes,
    start_new_call: bool,
) -> tuple[dict[str, Any] | None, bool]:
    cell = prepared["cell"]
    reservation_id = _opaque_reservation_id(ledger_secret, str(cell["cell_sha256"]))
    result_path, attempt_path = _generation_artifact_paths(campaign_root, cell)
    started = False
    if start_new_call:
        started = _atomic_create_json(
            campaign_root, attempt_path, _attempt_record(cell)
        )
        if not started:
            _validate_attempt_marker(attempt_path, cell, campaign_root=campaign_root)
            return None, False
    elif attempt_path.exists():
        _validate_attempt_marker(attempt_path, cell, campaign_root=campaign_root)
    if not result_path.exists():
        if start_new_call:
            try:
                generation_runner(
                    prepared["generation_spec"],
                    result_path,
                    ledger=ledger,
                    environment=environment,
                    root=repository,
                    output_root=campaign_root,
                    ledger_reservation_id=reservation_id,
                )
            except Exception:
                if not result_path.exists():
                    _atomic_write_json(
                        campaign_root,
                        result_path,
                        {
                            "schema_version": "contextlab.failed-generation-result.v1",
                            "run_id": cell["run_id"],
                            "error": "external sealed generation failed before persistence",
                        },
                    )
        else:
            if not attempt_path.exists():
                raise G3ExternalEvaluationError(
                    "external G3 missing generation was not attempted"
                )
            _atomic_write_json(
                campaign_root,
                result_path,
                {
                    "schema_version": "contextlab.failed-generation-result.v1",
                    "run_id": cell["run_id"],
                    "error": "external sealed generation was interrupted after attempt",
                },
            )
    result, _ = _load_strict_json(
        result_path, "external G3 generation result", anchor=campaign_root
    )
    if not isinstance(result, dict) or result.get("run_id") != cell.get("run_id"):
        raise G3ExternalEvaluationError("external G3 generation identity changed")
    if result.get("schema_version") == "contextlab.pending-generation-result.v1":
        result = {
            "schema_version": "contextlab.failed-generation-result.v1",
            "run_id": cell["run_id"],
            "error": "external sealed generation did not persist a result",
        }
        _atomic_write_json(campaign_root, result_path, result)
    if result.get("schema_version") == "contextlab.failed-generation-result.v1":
        actual_usd, latency_ms, usage_sha256 = _failure_accounting(
            ledger, reservation_id
        )
        return (
            _failed_outcome(
                prepared,
                result,
                actual_usd=actual_usd,
                latency_ms=latency_ms,
                usage_sha256=usage_sha256,
            ),
            started,
        )
    try:
        validate_saved_generation_result(
            result,
            expected_run_id=str(cell["run_id"]),
            expected_task_id=str(cell["task_id"]),
            expected_effort=str(cell["reasoning_effort"]),
        )
    except GenerationBatchError as exc:
        raise G3ExternalEvaluationError(
            "external G3 generation result is invalid"
        ) from exc
    return (
        _completed_outcome(task, prepared, result, campaign_root=campaign_root),
        started,
    )


def run_g3_external_evaluation(
    bundle_path: Path,
    candidate_manifest_path: Path,
    *,
    work_root: Path,
    return_path: Path | None = None,
    evaluation_id: str = "g3-sealed-v1",
    root: Path | None = None,
    ledger: CostLedger | None = None,
    environment: Mapping[str, str] | None = None,
    max_new_calls: int | None = None,
    concurrency: int = 4,
    cell_evaluator: CellEvaluator | None = None,
    embedding_runner: EmbeddingRunner | None = None,
    retrieval_runner: RetrievalRunner = run_task_ladder,
    generation_runner: GenerationRunner = run_paid_generation_to_file,
) -> dict[str, Any]:
    """Run the exact external 12 x M0--M4 x low/high sealed campaign.

    ``max_new_calls`` limits only fresh generation attempts; terminal and
    interrupted artifacts are replayed without another paid call. A capped
    run returns a separately hashed progress report until all 120 cells have
    terminal outcomes. ``cell_evaluator`` is a strict hash-and-metric-only
    injection point for hermetic tests.
    """

    repository = (root or repository_root()).resolve()
    if _IDENTIFIER.fullmatch(evaluation_id) is None:
        raise G3ExternalEvaluationError("external G3 evaluation ID is invalid")
    if max_new_calls is not None and (
        isinstance(max_new_calls, bool)
        or not isinstance(max_new_calls, int)
        or max_new_calls < 0
    ):
        raise G3ExternalEvaluationError(
            "external G3 max new calls must be a non-negative integer"
        )
    if (
        isinstance(concurrency, bool)
        or not isinstance(concurrency, int)
        or not 1 <= concurrency <= 16
    ):
        raise G3ExternalEvaluationError("external G3 concurrency must be within 1..16")
    external_root = _require_external_work_root(work_root, repository)
    campaign_root = _external_path(external_root, Path(evaluation_id), directory=True)
    requested_return_path = return_path or campaign_root / "g3_sealed_return.json"
    if requested_return_path.is_symlink():
        raise G3ExternalEvaluationError("content-free G3 return is a symlink")
    requested_return = requested_return_path.resolve()
    if not _is_relative_to(requested_return, campaign_root):
        raise G3ExternalEvaluationError(
            "content-free G3 return must stay below its external campaign root"
        )
    output = _external_path(campaign_root, requested_return.relative_to(campaign_root))
    tasks, candidate, commitments = validate_external_temporal_bundle(
        bundle_path, candidate_manifest_path, root=repository
    )
    if candidate.get("task_count") != 12 or candidate.get("cell_count") != 120:
        raise G3ExternalEvaluationError("external G3 candidate surface changed")
    by_task = {str(task["task_id"]): task for task in tasks}
    cells = candidate.get("cells")
    if not isinstance(cells, list) or len(cells) != G3_SEALED_CELL_COUNT:
        raise G3ExternalEvaluationError("external G3 candidate cells are incomplete")

    # A terminal return is the immutable campaign commit point. Validate and
    # replay it before preparing work so a rerun can never spend calls and then
    # overwrite or discover a conflicting terminal artifact.
    if output.exists():
        existing, _ = _load_strict_json(
            output, "completed external G3 return", anchor=campaign_root
        )
        if not isinstance(existing, Mapping):
            raise G3ExternalEvaluationError(
                "completed external G3 return must be an object"
            )
        _assert_content_free(existing)
        try:
            validate_g3_sealed_return(existing, candidate)
        except (G3SealedError, TypeError, KeyError, ValueError) as exc:
            raise G3ExternalEvaluationError(
                "existing external G3 return differs from the immutable campaign"
            ) from exc
        if existing.get("evaluation_id") != evaluation_id:
            raise G3ExternalEvaluationError(
                "existing external G3 return uses a different evaluation ID"
            )
        return dict(existing)

    outcomes: dict[str, Mapping[str, Any]] = {}
    missing_count = 0
    new_call_count = 0
    if cell_evaluator is not None:
        for cell in cells:
            if not isinstance(cell, Mapping):
                raise G3ExternalEvaluationError("external G3 candidate cell is invalid")
            task = by_task[str(cell["task_id"])]
            value = cell_evaluator(
                task,
                dict(cell),
                work_root=campaign_root,
                candidate_manifest=candidate,
                commitments=dict(commitments),
            )
            outcomes[str(cell["cell_sha256"])] = value
    else:
        freeze = _load_freeze(repository, candidate)
        manifest = freeze["manifest"]
        instruction = load_memory_answer_instruction(repository)
        if (
            hashlib.sha256(
                (repository / "evaluation/v2/prompts/memory_answer_v1.md").read_bytes()
            ).hexdigest()
            != manifest["prompt_sha256"]
        ):
            raise G3ExternalEvaluationError("external G3 prompt commitment changed")
        protocol = load_protocol(repository)
        active_ledger = ledger or CostLedger(canonical_ledger_path(repository))
        questions = [str(task["question_text"]) for task in tasks]
        embeddings = (
            embedding_runner(
                questions,
                campaign_root=campaign_root,
                repository=repository,
                protocol=protocol,
                ledger=active_ledger,
                environment=environment,
            )
            if embedding_runner is not None
            else _default_embeddings(
                questions,
                campaign_root=campaign_root,
                repository=repository,
                protocol=protocol,
                ledger=active_ledger,
                environment=environment,
            )
        )
        required_embedding_keys = {
            embedding_key(str(task["question_text"])) for task in tasks
        } | {
            embedding_key(chunk_embedding_text(chunk))
            for chunk in temporal_event_chunks()
        }
        if not required_embedding_keys.issubset(embeddings):
            raise G3ExternalEvaluationError(
                "external G3 embeddings do not cover questions and event evidence"
            )
        traces = {
            str(task["task_id"]): _r0_trace(
                task,
                campaign_root=campaign_root,
                embeddings=embeddings,
                protocol=protocol,
                retrieval_runner=retrieval_runner,
            )
            for task in tasks
        }
        prepared = [
            (
                by_task[str(cell["task_id"])],
                _prepare_cell(
                    by_task[str(cell["task_id"])],
                    cell,
                    candidate=candidate,
                    commitments=commitments,
                    manifest=manifest,
                    trace=traces[str(cell["task_id"])],
                    campaign_root=campaign_root,
                    instruction=instruction,
                ),
            )
            for cell in cells
        ]
        ledger_secret = _sealed_ledger_secret(campaign_root)
        replay_work: list[tuple[Mapping[str, Any], Mapping[str, Any], bool]] = []
        missing_work: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        for task, prepared_cell in prepared:
            state = _prepared_generation_state(prepared_cell, campaign_root)
            if state == "missing":
                missing_work.append((task, prepared_cell))
            else:
                replay_work.append((task, prepared_cell, False))
            if state != "recorded":
                missing_count += 1
        budget = (
            len(missing_work)
            if max_new_calls is None
            else min(max_new_calls, len(missing_work))
        )
        new_work = [
            (task, prepared_cell, True) for task, prepared_cell in missing_work[:budget]
        ]
        work = replay_work + new_work
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {
                executor.submit(
                    _execute_prepared,
                    task,
                    prepared_cell,
                    campaign_root=campaign_root,
                    repository=repository,
                    ledger=active_ledger,
                    environment=environment,
                    generation_runner=generation_runner,
                    ledger_secret=ledger_secret,
                    start_new_call=start_new_call,
                ): str(prepared_cell["cell"]["cell_sha256"])
                for task, prepared_cell, start_new_call in work
            }
            for future in as_completed(futures):
                outcome, started = future.result()
                new_call_count += int(started)
                if outcome is not None:
                    outcomes[futures[future]] = outcome

    expected_outcomes = {str(cell["cell_sha256"]) for cell in cells}
    if not set(outcomes).issubset(expected_outcomes):
        raise G3ExternalEvaluationError("external G3 outcome coverage is invalid")
    records = [
        _record_from_outcome(
            cell,
            outcomes[str(cell["cell_sha256"])],
            commitments=commitments,
        )
        for cell in cells
        if str(cell["cell_sha256"]) in outcomes
    ]
    completed = sum(record["status"] == "completed" for record in records)
    failed = len(records) - completed
    remaining = G3_SEALED_CELL_COUNT - len(records)
    total_cost = sum(
        (Decimal(str(record["actual_usd"])) for record in records), Decimal("0")
    )
    if remaining:
        if output.exists():
            raise G3ExternalEvaluationError(
                "content-free G3 return already exists for an incomplete campaign"
            )
        progress = _progress_report(
            evaluation_id=evaluation_id,
            candidate=candidate,
            commitments=commitments,
            completed_count=completed,
            failed_count=failed,
            missing_count=missing_count,
            new_call_count=new_call_count,
            remaining_count=remaining,
            actual_usd=total_cost,
            sealed_return_sha256=None,
        )
        _write_progress_report(campaign_root, progress)
        return progress
    if set(outcomes) != expected_outcomes:
        raise G3ExternalEvaluationError("external G3 outcome coverage is incomplete")
    returned: dict[str, Any] = {
        "schema_version": G3_SEALED_RETURN_SCHEMA,
        "evaluation_id": evaluation_id,
        "candidate_manifest_sha256": candidate["artifact_sha256"],
        "g3_freeze_sha256": candidate["g3_freeze_sha256"],
        "external_bundle_sha256": candidate["external_bundle_sha256"],
        "temporal_event_history_sha256": candidate["temporal_event_history_sha256"],
        "requested_model": MODEL_ID,
        "provider": PROVIDER_SLUG,
        "records": records,
        "aggregate_metadata": {
            "task_count": len(G3_SEALED_TASK_IDS),
            "cell_count": len(records),
            "completed_count": completed,
            "failed_count": failed,
            "actual_usd": format(total_cost, "f"),
            "evaluator_version": G3_EXTERNAL_EVALUATOR_VERSION,
        },
    }
    returned["artifact_sha256"] = sha256_json(returned)
    _assert_content_free(returned)
    try:
        validate_g3_sealed_return(returned, candidate)
    except (G3SealedError, TypeError, KeyError, ValueError) as exc:
        raise G3ExternalEvaluationError(
            "content-free G3 return failed its repository import contract"
        ) from exc
    _atomic_create_json(campaign_root, output, returned)
    progress = _progress_report(
        evaluation_id=evaluation_id,
        candidate=candidate,
        commitments=commitments,
        completed_count=completed,
        failed_count=failed,
        missing_count=missing_count,
        new_call_count=new_call_count,
        remaining_count=0,
        actual_usd=total_cost,
        sealed_return_sha256=str(returned["artifact_sha256"]),
    )
    _write_progress_report(campaign_root, progress)
    return returned


evaluate_external_g3 = run_g3_external_evaluation


__all__ = [
    "EXTERNAL_BUNDLE_SCHEMA",
    "EXTERNAL_TEMPORAL_BUNDLE_SCHEMA",
    "G3_EXTERNAL_EVALUATOR_VERSION",
    "G3_EXTERNAL_PROGRESS_SCHEMA",
    "G3ExternalEvaluationError",
    "evaluate_external_g3",
    "load_g3_external_progress",
    "run_g3_external_evaluation",
    "validate_external_sealed_bundle",
    "validate_external_temporal_bundle",
]
