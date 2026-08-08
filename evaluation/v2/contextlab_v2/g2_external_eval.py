"""Standalone evaluator for the externally-held G2 sealed task bundle.

This module deliberately has no repository output path for task text, traces,
answers, or gold.  The only artifact it returns is the content-free contract
accepted by :mod:`contextlab_v2.g2_sealed`.
"""

from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
from typing import Any, Callable, Iterable, Mapping, Sequence

from .answer_metrics import score_generated_answer
from .baseline import repository_root
from .costs import CostLedger, canonical_ledger_path
from .embeddings import embed_texts, load_embedding_cache, load_extension_cache
from .experiments import (
    ExperimentError,
    FROZEN_RAW_CHUNKS_SHA256,
    TRACE_SCHEMA,
    load_frozen_chunks,
    load_protocol,
    load_structured_chunks,
    run_task_ladder,
    score_trace,
)
from .g2_sealed import G2_SEALED_TASK_IDS, G2_STRATEGY_IDS
from .gateway import run_paid_generation_to_file
from .generations import (
    GenerationBatchError,
    build_generation_spec,
    load_answer_instruction,
    validate_saved_generation_result,
)
from .statistics import distribution_summary
from .tasking import prompt_safe_task, sha256_json


EXTERNAL_BUNDLE_SCHEMA = "contextlab.external-static-sealed.v1"
G2_RETURN_SCHEMA = "contextlab.g2-sealed-return.v1"
_BUNDLE_FIELDS = frozenset(
    {"authoring_version", "corpus_snapshot_sha256", "schema", "tasks"}
)
_TASK_FIELDS = frozenset(
    {
        "acceptable_alternative_evidence",
        "difficulty",
        "expected_answer",
        "question_text",
        "required_evidence",
        "scoring_notes",
        "task_family",
        "task_id",
    }
)
_FORBIDDEN_RETURN_KEYS = frozenset(
    {
        "question_text",
        "expected_answer",
        "required_evidence",
        "scoring_notes",
        "answer",
        "rendered_context",
        "retrieved_text",
    }
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class G2ExternalEvaluationError(ValueError):
    """The externally-held bundle or work location violates the sealed boundary."""


EmbeddingRunner = Callable[..., Any]
RetrievalRunner = Callable[..., Sequence[Mapping[str, Any]]]
GenerationRunner = Callable[..., Mapping[str, Any]]


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise G2ExternalEvaluationError(
                "external sealed JSON contains a duplicate field"
            )
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise G2ExternalEvaluationError("external sealed JSON contains a non-finite number")


def _load_strict_json(path: Path, label: str) -> tuple[Any, bytes]:
    try:
        raw = path.read_bytes()
        return (
            json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_strict_object,
                parse_constant=_reject_constant,
            ),
            raw,
        )
    except G2ExternalEvaluationError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise G2ExternalEvaluationError(f"cannot read strict {label} JSON") from exc


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _require_external_file(path: Path, repository: Path, label: str) -> Path:
    resolved = path.resolve()
    if _is_relative_to(resolved, repository):
        raise G2ExternalEvaluationError(f"{label} must stay outside the repository")
    return resolved


def _require_external_work_root(path: Path, repository: Path) -> Path:
    root = _require_external_file(path, repository, "external work root")
    if root == Path(root.anchor) or root in {
        Path("/", "tmp").resolve(),
        Path("/", "var", "tmp").resolve(),
        Path("/", "private", "tmp").resolve(),
    }:
        raise G2ExternalEvaluationError("external work root is too broad")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _external_destination(work_root: Path, relative_path: Path) -> Path:
    """Create safe parents and reject descendant symlinks before a sensitive write."""
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or any(part in {"", ".", ".."} for part in relative_path.parts)
    ):
        raise G2ExternalEvaluationError("external destination is invalid")
    current = work_root
    for part in relative_path.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise G2ExternalEvaluationError(
                "external work directory contains a symlink"
            )
        if current.exists():
            if not current.is_dir():
                raise G2ExternalEvaluationError(
                    "external work parent is not a directory"
                )
        else:
            current.mkdir()
        if not _is_relative_to(current.resolve(), work_root):
            raise G2ExternalEvaluationError(
                "external work destination escaped its root"
            )
    destination = current / relative_path.name
    if destination.is_symlink():
        raise G2ExternalEvaluationError("external work file is a symlink")
    if destination.exists() and not destination.is_file():
        raise G2ExternalEvaluationError("external work destination is not a file")
    if not _is_relative_to(destination.parent.resolve(), work_root):
        raise G2ExternalEvaluationError("external work destination escaped its root")
    return destination


def _external_directory(work_root: Path, relative_path: Path) -> Path:
    """Create a campaign directory without following descendant symlinks."""
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or any(part in {"", ".", ".."} for part in relative_path.parts)
    ):
        raise G2ExternalEvaluationError("external directory is invalid")
    current = work_root
    for part in relative_path.parts:
        current = current / part
        if current.is_symlink():
            raise G2ExternalEvaluationError(
                "external work directory contains a symlink"
            )
        if current.exists():
            if not current.is_dir():
                raise G2ExternalEvaluationError(
                    "external campaign path is not a directory"
                )
        else:
            current.mkdir()
        if not _is_relative_to(current.resolve(), work_root):
            raise G2ExternalEvaluationError(
                "external campaign directory escaped its root"
            )
    return current.resolve()


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
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
            json.dump(
                value,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _exact_fields(
    value: Mapping[str, Any], expected: frozenset[str], label: str
) -> None:
    if set(value) != expected:
        raise G2ExternalEvaluationError(
            f"{label} fields differ; missing={sorted(expected - set(value))}, "
            f"unknown={sorted(set(value) - expected)}"
        )


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise G2ExternalEvaluationError(f"{label} must be a list of non-empty text")
    if len(set(value)) != len(value):
        raise G2ExternalEvaluationError(f"{label} contains a duplicate value")
    return list(value)


def _freeze_contract(
    root: Path,
) -> tuple[dict[str, Any], str, str, dict[str, Mapping[str, Any]]]:
    freeze, _ = _load_strict_json(
        root / "results/v2/splits/static_g2_freeze.json", "freeze"
    )
    if (
        not isinstance(freeze, dict)
        or freeze.get("schema_version") != "contextlab.static-freeze.v1"
    ):
        raise G2ExternalEvaluationError("static G2 freeze schema is invalid")
    if freeze.get("benchmark_status") != "frozen":
        raise G2ExternalEvaluationError("static G2 benchmark is not frozen")
    manifest_hash = freeze.get("manifest_sha256")
    body = {key: value for key, value in freeze.items() if key != "manifest_sha256"}
    if not isinstance(manifest_hash, str) or manifest_hash != sha256_json(body):
        raise G2ExternalEvaluationError("static G2 freeze manifest hash mismatch")
    external = freeze.get("external_sealed_bundle")
    corpus = freeze.get("corpus_snapshot")
    if (
        not isinstance(external, Mapping)
        or external.get("task_count") != len(G2_SEALED_TASK_IDS)
        or not isinstance(external.get("bundle_sha256"), str)
        or not _SHA256.fullmatch(str(external.get("bundle_sha256")))
        or not isinstance(corpus, Mapping)
        or not isinstance(corpus.get("chunks_sha256"), str)
    ):
        raise G2ExternalEvaluationError("static G2 freeze commitments are invalid")
    metadata: dict[str, Mapping[str, Any]] = {}
    tasks = freeze.get("tasks")
    if not isinstance(tasks, list):
        raise G2ExternalEvaluationError("static G2 freeze lacks task metadata")
    for row in tasks:
        if isinstance(row, Mapping) and row.get("partition") == "sealed_capability":
            task_id = row.get("task_id")
            if not isinstance(task_id, str) or task_id in metadata:
                raise G2ExternalEvaluationError(
                    "static G2 sealed task metadata is invalid"
                )
            metadata[task_id] = row
    if tuple(sorted(metadata)) != G2_SEALED_TASK_IDS:
        raise G2ExternalEvaluationError("static G2 freeze does not define S081-S116")
    return freeze, manifest_hash, str(external["bundle_sha256"]), metadata


def _known_evidence(chunks: Iterable[Mapping[str, Any]]) -> set[str]:
    known: set[str] = set()
    for row in chunks:
        for field in ("source_id", "section_id", "chunk_id"):
            value = row.get(field)
            if isinstance(value, str) and value:
                known.add(value)
    return known


def validate_external_sealed_bundle(
    bundle_path: Path,
    *,
    root: Path | None = None,
    chunks: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Strictly validate the withheld bundle against only frozen public commitments."""
    repository = (root or repository_root()).resolve()
    path = _require_external_file(bundle_path, repository, "external sealed bundle")
    raw_bundle, raw_bytes = _load_strict_json(path, "external sealed bundle")
    if not isinstance(raw_bundle, dict):
        raise G2ExternalEvaluationError("external sealed bundle must be an object")
    _exact_fields(raw_bundle, _BUNDLE_FIELDS, "external sealed bundle")
    if raw_bundle.get("schema") != EXTERNAL_BUNDLE_SCHEMA:
        raise G2ExternalEvaluationError("unsupported external sealed bundle schema")
    if (
        not isinstance(raw_bundle.get("authoring_version"), str)
        or not raw_bundle["authoring_version"].strip()
    ):
        raise G2ExternalEvaluationError(
            "external sealed bundle authoring version is invalid"
        )
    freeze, manifest_hash, bundle_hash, metadata = _freeze_contract(repository)
    if hashlib.sha256(raw_bytes).hexdigest() != bundle_hash:
        raise G2ExternalEvaluationError("external sealed bundle SHA-256 mismatch")
    if (
        raw_bundle.get("corpus_snapshot_sha256")
        != freeze["corpus_snapshot"]["chunks_sha256"]
    ):
        raise G2ExternalEvaluationError("external sealed bundle corpus hash mismatch")
    rows = raw_bundle.get("tasks")
    if not isinstance(rows, list) or len(rows) != len(G2_SEALED_TASK_IDS):
        raise G2ExternalEvaluationError("external sealed bundle must have 36 tasks")
    known = _known_evidence(
        chunks if chunks is not None else load_frozen_chunks(repository)
    )
    checked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, task in enumerate(rows):
        if not isinstance(task, dict):
            raise G2ExternalEvaluationError(f"external task {index} must be an object")
        _exact_fields(task, _TASK_FIELDS, f"external task {index}")
        task_id = task.get("task_id")
        if (
            not isinstance(task_id, str)
            or task_id not in G2_SEALED_TASK_IDS
            or task_id in seen
        ):
            raise G2ExternalEvaluationError(
                f"external task {index} ID is invalid or duplicated"
            )
        seen.add(task_id)
        for field in (
            "difficulty",
            "expected_answer",
            "question_text",
            "scoring_notes",
            "task_family",
        ):
            if not isinstance(task.get(field), str) or not task[field].strip():
                raise G2ExternalEvaluationError(
                    f"external task {task_id} {field} is invalid"
                )
        for field in ("required_evidence", "acceptable_alternative_evidence"):
            evidence = _string_list(task.get(field), f"external task {task_id} {field}")
            unknown = set(evidence).difference(known)
            if unknown:
                raise G2ExternalEvaluationError(
                    f"external task {task_id} has unknown evidence"
                )
        frozen = metadata[task_id]
        if task["task_family"] != frozen.get("task_family") or task[
            "difficulty"
        ] != frozen.get("difficulty"):
            raise G2ExternalEvaluationError(
                f"external task {task_id} metadata disagrees with freeze"
            )
        checked.append(dict(task))
    if seen != set(G2_SEALED_TASK_IDS):
        raise G2ExternalEvaluationError(
            "external sealed bundle task IDs are incomplete"
        )
    checked.sort(key=lambda row: row["task_id"])
    return checked, {
        "static_freeze_manifest_sha256": manifest_hash,
        "external_bundle_sha256": bundle_hash,
    }


def _default_embed_queries(
    questions: Sequence[str],
    *,
    base_cache_path: Path,
    extension_cache_path: Path,
    expected_base_sha256: str,
    ledger: CostLedger,
    root: Path,
    environment: Mapping[str, str] | None,
) -> Mapping[str, Sequence[float]]:
    embed_texts(
        questions,
        base_cache_path=base_cache_path,
        extension_cache_path=extension_cache_path,
        expected_base_sha256=expected_base_sha256,
        ledger=ledger,
        run_id="g2-external-queries",
        environment=environment,
        root=root,
    )
    return {
        **load_embedding_cache(
            base_cache_path, expected_base_sha256=expected_base_sha256
        ),
        **load_extension_cache(extension_cache_path),
    }


def _trace_commitment(trace: Mapping[str, Any]) -> str:
    return sha256_json(dict(trace))


def _validate_frozen_trace(
    trace: Mapping[str, Any],
    *,
    task: Mapping[str, Any],
    strategy: str,
    protocol_sha256: str,
    context_token_budget: int,
) -> None:
    rendered = trace.get("rendered_context")
    if (
        trace.get("schema_version") != TRACE_SCHEMA
        or trace.get("task") != task
        or trace.get("strategy_id") != strategy
        or trace.get("protocol_sha256") != protocol_sha256
        or trace.get("corpus_snapshot_id") != FROZEN_RAW_CHUNKS_SHA256
        or not isinstance(rendered, str)
        or trace.get("rendered_context_sha256")
        != hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        or trace.get("context_token_budget") != context_token_budget
        or isinstance(trace.get("context_tokens"), bool)
        or not isinstance(trace.get("context_tokens"), int)
        or trace["context_tokens"] > context_token_budget
    ):
        raise G2ExternalEvaluationError("frozen retrieval trace commitment is invalid")


def _context_references(trace: Mapping[str, Any]) -> list[str]:
    candidates = trace.get("selected_candidates")
    if not isinstance(candidates, list):
        raise G2ExternalEvaluationError("retrieval trace lacks selected candidates")
    references: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise G2ExternalEvaluationError(
                "retrieval trace contains invalid candidate"
            )
        reference = candidate.get("section_id") or candidate.get("source_id")
        if not isinstance(reference, str) or not reference:
            raise G2ExternalEvaluationError(
                "retrieval trace candidate lacks evidence reference"
            )
        references.append(reference)
    return references


def _generation_paths(work_root: Path, run_id: str) -> tuple[Path, Path]:
    return (
        _external_destination(work_root, Path("generation_specs") / f"{run_id}.json"),
        _external_destination(work_root, Path("generation_results") / f"{run_id}.json"),
    )


def _generation_attempt_path(work_root: Path, run_id: str) -> Path:
    return _external_destination(
        work_root, Path("generation_attempts") / f"{run_id}.json"
    )


def _sealed_ledger_secret(campaign_work: Path) -> bytes:
    """Atomically create or load the private 32-byte per-campaign HMAC key."""
    destination = _external_destination(
        campaign_work, Path("sealed_ledger_reservation_hmac.key")
    )

    def read_existing() -> bytes:
        info = os.lstat(destination)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise G2ExternalEvaluationError("sealed ledger HMAC key permissions are invalid")
        value = destination.read_bytes()
        if len(value) != 32:
            raise G2ExternalEvaluationError("sealed ledger HMAC key length is invalid")
        return value

    if destination.exists():
        return read_existing()
    temporary = campaign_work / f".{destination.name}.{secrets.token_hex(16)}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        value = secrets.token_bytes(32)
        if os.write(descriptor, value) != len(value):
            raise G2ExternalEvaluationError("cannot create sealed ledger HMAC key")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            os.link(temporary, destination)
        except FileExistsError:
            pass
        else:
            directory_descriptor = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
            return value
        return read_existing()
    except OSError as exc:
        raise G2ExternalEvaluationError("cannot create sealed ledger HMAC key") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def _sealed_ledger_reservation_id(
    *, secret: bytes, campaign_id: str, protocol_sha256: str, run_id: str
) -> str:
    """Bind a sealed paid call without placing its clear cell identity in the ledger."""
    commitment = "\0".join((campaign_id, protocol_sha256, run_id))
    return "g2-sealed-" + hmac.new(
        secret, commitment.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _generation_status(path: Path) -> tuple[str, Mapping[str, Any] | None]:
    if not path.exists():
        return "pending", None
    value, _ = _load_strict_json(path, "external generation result")
    if not isinstance(value, Mapping):
        return "pending", None
    schema = value.get("schema_version")
    if schema == "contextlab.generation-result.v1" and isinstance(
        value.get("answer"), str
    ):
        return "completed", value
    if schema == "contextlab.failed-generation-result.v1":
        return "failed", value
    return "pending", value


def _validate_completed_result(
    result: Mapping[str, Any] | None,
    *,
    run_id: str,
    task_id: str,
    effort: str,
) -> None:
    if result is None:
        raise G2ExternalEvaluationError("completed external result is missing")
    try:
        validate_saved_generation_result(
            result,
            expected_run_id=run_id,
            expected_task_id=task_id,
            expected_effort=effort,
        )
    except GenerationBatchError as exc:
        raise G2ExternalEvaluationError(
            "completed external generation result is invalid"
        ) from exc


def _decimal_total(values: Iterable[Any]) -> str:
    total = Decimal("0")
    for value in values:
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise G2ExternalEvaluationError("generation cost is invalid") from exc
        if not number.is_finite() or number < 0:
            raise G2ExternalEvaluationError("generation cost is invalid")
        total += number
    return format(total, "f")


def _empty_latency() -> dict[str, int | float]:
    return {
        "n": 0,
        "mean": 0.0,
        "sample_stddev": 0.0,
        "min": 0.0,
        "median": 0.0,
        "p50": 0.0,
        "p95": 0.0,
        "max": 0.0,
    }


def _safe_generation_summary(
    cells: Sequence[Mapping[str, Any]],
    tasks: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    statuses = {"completed": 0, "failed": 0, "pending": 0}
    completed: list[Mapping[str, Any]] = []
    screens: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for cell in cells:
        status = str(cell["status"])
        statuses[status] += 1
        result = cell.get("result")
        if status != "completed" or not isinstance(result, Mapping):
            continue
        completed.append(result)
        task = tasks[str(cell["task_id"])]
        screen = score_generated_answer(
            str(result["answer"]),
            str(task["expected_answer"]),
            task["required_evidence"],
            cell["context_references"],
            abstention_task=task["task_family"] == "abstention",
        )
        screens[f"{cell['strategy_id']}:{cell['reasoning_effort']}"].append(screen)
    token_fields = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    costs: list[Any] = []
    latencies: list[float] = []
    for result in completed:
        metadata = result.get("metadata")
        if not isinstance(metadata, Mapping):
            raise G2ExternalEvaluationError("completed generation has no metadata")
        token_fields["prompt_tokens"] += int(metadata.get("prompt_tokens", 0))
        token_fields["completion_tokens"] += int(metadata.get("completion_tokens", 0))
        token_fields["reasoning_tokens"] += int(
            metadata.get("native_reasoning_tokens") or 0
        )
        costs.append(metadata.get("actual_usd", "0"))
        latency = metadata.get("latency_ms")
        if (
            isinstance(latency, (int, float))
            and not isinstance(latency, bool)
            and math.isfinite(float(latency))
            and latency >= 0
        ):
            latencies.append(float(latency))
        else:
            raise G2ExternalEvaluationError("completed generation latency is invalid")
    aggregates: dict[str, Any] = {}
    metric_fields = (
        "expected_content_token_recall",
        "critical_value_recall",
        "citation_precision",
        "required_evidence_citation_recall",
    )
    for strategy in G2_STRATEGY_IDS:
        for effort in ("low", "high"):
            values = screens[f"{strategy}:{effort}"]
            n = len(values)
            aggregates[f"{strategy}:{effort}"] = {
                "n": n,
                "accepted_proxy_rate": sum(
                    bool(row["accepted_proxy"]) for row in values
                )
                / n
                if n
                else 0.0,
                **{
                    field: sum(float(row[field]) for row in values) / n if n else 0.0
                    for field in metric_fields
                },
                "unsupported_citation_count": sum(
                    int(row["unsupported_citation_count"]) for row in values
                ),
                "abstention_task_count": sum(
                    row["abstention_quality"] != "not_applicable" for row in values
                ),
                "abstention_correct_count": sum(
                    row["abstention_quality"] == "correct" for row in values
                ),
            }
    return {
        "generation_count": len(G2_SEALED_TASK_IDS) * len(G2_STRATEGY_IDS) * 2,
        "status_counts": statuses,
        **token_fields,
        "cost_usd": _decimal_total(costs),
        "latency_ms": distribution_summary(latencies)
        if latencies
        else _empty_latency(),
        "screening_by_strategy_effort": aggregates,
    }


def _assert_content_free(value: Any) -> None:
    if isinstance(value, Mapping):
        forbidden = _FORBIDDEN_RETURN_KEYS.intersection(value)
        if forbidden:
            raise G2ExternalEvaluationError(
                f"safe return contains forbidden fields: {sorted(forbidden)}"
            )
        for child in value.values():
            _assert_content_free(child)
    elif isinstance(value, list):
        for child in value:
            _assert_content_free(child)


def run_g2_external_evaluation(
    bundle_path: Path,
    *,
    work_root: Path,
    return_path: Path | None = None,
    root: Path | None = None,
    base_cache_path: Path | None = None,
    ledger: CostLedger | None = None,
    environment: Mapping[str, str] | None = None,
    max_new_calls: int | None = None,
    concurrency: int = 4,
    chunks_loader: Callable[[Path], Sequence[Mapping[str, Any]]] = load_frozen_chunks,
    structured_chunks_loader: Callable[
        [Path], Sequence[Mapping[str, Any]]
    ] = load_structured_chunks,
    embedding_runner: EmbeddingRunner | None = None,
    retrieval_runner: RetrievalRunner = run_task_ladder,
    generation_runner: GenerationRunner = run_paid_generation_to_file,
) -> dict[str, Any]:
    """Evaluate the held-out G2 bundle and write only its safe return externally.

    Injected runners exist for hermetic tests.  Production defaults use the
    frozen corpus, pinned embedding base cache, canonical repository ledger,
    and the single paid gateway.
    """
    repository = (root or repository_root()).resolve()
    if max_new_calls is not None and (
        isinstance(max_new_calls, bool)
        or not isinstance(max_new_calls, int)
        or max_new_calls < 0
    ):
        raise G2ExternalEvaluationError("max new calls must be a non-negative integer")
    if (
        isinstance(concurrency, bool)
        or not isinstance(concurrency, int)
        or not 1 <= concurrency <= 16
    ):
        raise G2ExternalEvaluationError("generation concurrency must be within 1..16")
    external_work = _require_external_work_root(work_root, repository)
    protocol = load_protocol(repository)
    protocol_hash = sha256_json(protocol)
    campaign_id = str(protocol["fixed_comparison"]["generation_campaign_id"])
    campaign_work = _external_directory(external_work, Path(campaign_id))
    ledger_secret = _sealed_ledger_secret(campaign_work)
    requested_output = (
        return_path or campaign_work / "g2_sealed_return.json"
    ).resolve()
    if not _is_relative_to(requested_output, campaign_work):
        raise G2ExternalEvaluationError(
            "safe return must stay below the external campaign root"
        )
    output = _external_destination(
        campaign_work, requested_output.relative_to(campaign_work)
    )
    chunks = list(chunks_loader(repository))
    tasks, commitments = validate_external_sealed_bundle(
        bundle_path, root=repository, chunks=chunks
    )
    context_token_budget = int(protocol["fixed_comparison"]["context_token_budget"])
    prompt_tasks = [prompt_safe_task({**task, "suite": "static"}) for task in tasks]
    active_ledger = ledger or CostLedger(canonical_ledger_path(repository))
    base_cache = (
        base_cache_path
        or repository
        / "evaluation/build/embeddings_openai_text-embedding-3-small.jsonl"
    )
    extension_cache = _external_destination(
        campaign_work, Path("embedding_extension.jsonl")
    )
    expected_base = str(protocol["dense_control"]["base_cache_sha256"])
    query_texts = [task["question_text"] for task in prompt_tasks]
    embeddings = (
        embedding_runner(
            query_texts,
            base_cache_path=base_cache,
            extension_cache_path=extension_cache,
            expected_base_sha256=expected_base,
            ledger=active_ledger,
            root=repository,
            environment=environment,
        )
        if embedding_runner is not None
        else _default_embed_queries(
            query_texts,
            base_cache_path=base_cache,
            extension_cache_path=extension_cache,
            expected_base_sha256=expected_base,
            ledger=active_ledger,
            root=repository,
            environment=environment,
        )
    )
    structured = list(structured_chunks_loader(repository))

    # Gold answers are not touched in this phase.  Every R0-R7 trace is first
    # persisted under the external root, then components are scored from the
    # frozen traces and finally completed answers are screened.
    traces: dict[tuple[str, str], Mapping[str, Any]] = {}
    for task in prompt_tasks:
        existing: set[str] = set()
        for strategy in G2_STRATEGY_IDS:
            cell = (task["task_id"], strategy)
            trace_path = _external_destination(
                campaign_work,
                Path("retrieval_traces") / f"{cell[0]}_{cell[1]}.json",
            )
            if not trace_path.exists():
                continue
            trace, _ = _load_strict_json(trace_path, "external retrieval trace")
            if not isinstance(trace, Mapping):
                raise G2ExternalEvaluationError(
                    "frozen retrieval trace is not an object"
                )
            _validate_frozen_trace(
                trace,
                task=task,
                strategy=strategy,
                protocol_sha256=protocol_hash,
                context_token_budget=context_token_budget,
            )
            traces[cell] = trace
            existing.add(strategy)
        if len(existing) == len(G2_STRATEGY_IDS):
            continue
        generated = retrieval_runner(task, chunks, embeddings, structured, protocol)
        if len(generated) != len(G2_STRATEGY_IDS):
            raise G2ExternalEvaluationError("retrieval runner did not produce R0-R7")
        produced: set[str] = set()
        for trace in generated:
            strategy = trace.get("strategy_id")
            if strategy not in G2_STRATEGY_IDS or strategy in produced:
                raise G2ExternalEvaluationError(
                    "retrieval runner produced an invalid strategy"
                )
            produced.add(str(strategy))
            cell = (task["task_id"], str(strategy))
            _validate_frozen_trace(
                trace,
                task=task,
                strategy=str(strategy),
                protocol_sha256=protocol_hash,
                context_token_budget=context_token_budget,
            )
            if strategy in existing:
                continue
            trace_path = _external_destination(
                campaign_work,
                Path("retrieval_traces") / f"{cell[0]}_{cell[1]}.json",
            )
            _atomic_write_json(trace_path, dict(trace))
            traces[cell] = trace
        if produced != set(G2_STRATEGY_IDS):
            raise G2ExternalEvaluationError(
                "retrieval runner did not cover every strategy"
            )
    expected_cells = {
        (task_id, strategy)
        for task_id in G2_SEALED_TASK_IDS
        for strategy in G2_STRATEGY_IDS
    }
    if set(traces) != expected_cells:
        raise G2ExternalEvaluationError(
            "retrieval traces do not cover every G2 component cell"
        )

    by_task = {str(task["task_id"]): task for task in tasks}
    records: list[dict[str, Any]] = []
    for task_id in G2_SEALED_TASK_IDS:
        task = by_task[task_id]
        # Match the frozen public scorer: component retrieval metrics use only
        # required evidence. Alternatives remain evaluator-only grading notes.
        relevant = list(task["required_evidence"])
        for strategy in G2_STRATEGY_IDS:
            trace = traces[(task_id, strategy)]
            try:
                metrics = score_trace(trace, relevant)
            except (ExperimentError, KeyError, TypeError) as exc:
                raise G2ExternalEvaluationError(
                    "cannot score frozen retrieval trace"
                ) from exc
            records.append(
                {
                    "task_id": task_id,
                    "strategy_id": strategy,
                    "trace_commitment_sha256": _trace_commitment(trace),
                    "metrics": metrics,
                }
            )

    cells_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    new_work: list[
        tuple[
            tuple[str, str, str],
            dict[str, Any],
            Path,
            Path,
            list[str],
        ]
    ] = []
    comparison = protocol["fixed_comparison"]
    instruction = load_answer_instruction(repository)
    for task in prompt_tasks:
        for strategy in G2_STRATEGY_IDS:
            trace = traces[(task["task_id"], strategy)]
            for effort in ("low", "high"):
                spec = build_generation_spec(
                    trace,
                    effort,
                    trial=1,
                    max_tokens=int(comparison["output_token_limit"]),
                    temperature=float(comparison["temperature"]),
                    system_instruction=instruction,
                    campaign_id=str(comparison["generation_campaign_id"]),
                )
                run_id = str(spec["run_id"])
                spec_path, result_path = _generation_paths(campaign_work, run_id)
                if spec_path.exists():
                    saved_spec, _ = _load_strict_json(
                        spec_path, "external generation specification"
                    )
                    if saved_spec != spec:
                        raise G2ExternalEvaluationError(
                            "saved external generation specification changed"
                        )
                else:
                    _atomic_write_json(spec_path, spec)
                status, result = _generation_status(result_path)
                if result is not None and result.get("run_id") != run_id:
                    raise G2ExternalEvaluationError(
                        "external generation result identity changed"
                    )
                if status == "completed":
                    _validate_completed_result(
                        result,
                        run_id=run_id,
                        task_id=str(task["task_id"]),
                        effort=effort,
                    )
                attempt_path = _generation_attempt_path(campaign_work, run_id)
                key = (str(task["task_id"]), strategy, effort)
                references = _context_references(trace)
                cells_by_key[key] = {
                    "task_id": task["task_id"],
                    "strategy_id": strategy,
                    "reasoning_effort": effort,
                    "status": status,
                    "result": result,
                    "context_references": references,
                }
                if status == "pending" and result is None and not attempt_path.exists():
                    new_work.append((key, spec, result_path, attempt_path, references))

    if max_new_calls is not None:
        new_work = new_work[:max_new_calls]

    def run_one(
        work: tuple[
            tuple[str, str, str],
            dict[str, Any],
            Path,
            Path,
            list[str],
        ],
    ) -> tuple[tuple[str, str, str], dict[str, Any]]:
        key, spec, result_path, attempt_path, references = work
        run_id = str(spec["run_id"])
        ledger_reservation_id = _sealed_ledger_reservation_id(
            secret=ledger_secret,
            campaign_id=campaign_id,
            protocol_sha256=protocol_hash,
            run_id=run_id,
        )
        # This marker makes a process failure before the gateway can reserve its
        # result path visible and non-retryable.
        _atomic_write_json(
            attempt_path,
            {
                "schema_version": "contextlab.g2-sealed-generation-attempt.v1",
                "run_id": run_id,
            },
        )
        try:
            generation_runner(
                spec,
                result_path,
                ledger=active_ledger,
                environment=environment,
                root=repository,
                output_root=campaign_work,
                ledger_reservation_id=ledger_reservation_id,
            )
        except Exception:
            # The gateway persists a content-bearing failed result; no retry is
            # permitted. A pre-result failure remains pending for later audit.
            pass
        status, result = _generation_status(result_path)
        if result is not None and result.get("run_id") != run_id:
            raise G2ExternalEvaluationError(
                "external generation result identity changed"
            )
        if status == "completed":
            _validate_completed_result(
                result,
                run_id=run_id,
                task_id=key[0],
                effort=key[2],
            )
        return key, {
            "task_id": key[0],
            "strategy_id": key[1],
            "reasoning_effort": key[2],
            "status": status,
            "result": result,
            "context_references": references,
        }

    if new_work:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(run_one, work) for work in new_work]
            for future in as_completed(futures):
                key, row = future.result()
                cells_by_key[key] = row

    cells = [cells_by_key[key] for key in sorted(cells_by_key)]
    summary = _safe_generation_summary(cells, by_task)
    safe_return = {
        "schema_version": G2_RETURN_SCHEMA,
        **commitments,
        "retrieval_protocol_sha256": protocol_hash,
        "component_records": records,
        "generation_summary": summary,
    }
    _assert_content_free(safe_return)
    _atomic_write_json(output, safe_return)
    return safe_return


# A short alias keeps the module convenient for a standalone external runner.
evaluate_external_g2 = run_g2_external_evaluation
