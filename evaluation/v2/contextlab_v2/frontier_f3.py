"""Deterministic public seams for the F3 virtual-context paging experiment.

Pure paging seams accept explicit fixture page records.  The production seam
reads only an exact preapproved source manifest and its hash-bound public source
artifacts; it never discovers corpus, grading, protected, or sealed inputs.  The
persistence boundary remains separate so every state transition can be tested
and replayed without external services.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from .contracts import validate_instance
from .credentials import redact
from .baseline import repository_root
from .costs import canonical_ledger_path
from .costs import CostLedger
from .frontier import (
    FrontierError,
    load_approved_frontier_entry_gate,
    require_approved_f3_source_commitment,
    require_frontier_experiment_approved,
)
from .gateway import run_paid_generation_to_file
from .generations import validate_saved_generation_result
from .immutable_io import ImmutableIOError, write_bytes_once_or_verify
from .provider import (
    ALLOWED_REASONING_EFFORTS,
    CANONICAL_MODEL_ID,
    FRONTIER_PROVIDER_SLUG,
    MODEL_ID,
    PINNED_ROUTE_INPUT_USD_PER_MILLION,
    PINNED_ROUTE_OUTPUT_USD_PER_MILLION,
)
from .retrieval import estimate_tokens
from .tasking import sha256_json


F3_CATALOG_SCHEMA = "contextlab.f3-page-catalog.v1"
F3_SOURCE_MANIFEST_SCHEMA = "contextlab.f3-public-source-manifest.v1"
F3_WORKING_SET_SCHEMA = "contextlab.f3-working-set.v1"
F3_PREPARATION_SCHEMA = "contextlab.f3-preparation.v2"
F3_STRATEGY_PREPARATION_SCHEMA = "contextlab.f3-strategy-preparation.v1"
F3_EXPERIMENT_SCHEMA = "contextlab.f3-experiment.v2"
F3_SOURCE_MANIFEST_PATH = Path("results/v2/frontier/f3/public_source_manifest.json")
F3_RESULT_PATH = Path(
    "results/v2/frontier/f3/virtual_context_paging.attempt-06.json"
)
F3_FINAL_SCHEMA = "contextlab.f3-final-result.v2"
F3_FINAL_RESULT_PATH = Path(
    "results/v2/frontier/f3/virtual_context_paging.attempt-06.final.json"
)
F3_COMPLETION_INPUT_PATH = Path(
    "results/v2/frontier/f3/completions.attempt-06.json"
)
F3_GENERATOR_RECEIPT_SCHEMA = "contextlab.f3-generator-receipt.v2"
F3_ANSWER_QUALITY_RESULT_SCHEMA = "contextlab.f3-answer-quality-result.v2"
F3_PUBLIC_OUTPUT_SCHEMA = "contextlab.f3-public-generation-output.v1"
F3_PUBLIC_METRIC_SCHEMA = "contextlab.f3-public-metric.v1"
F3_FRONTIER_PROTOCOL_SCHEMA = "contextlab.frontier-protocol.v2"
F3_MINIMUM_COMPLETE_TRIALS = 5
F3_PROVIDER_REPEAT_SAMPLE_COUNT = 5
F3_TEMPERATURE = 0.0
F3_MAX_COMPLETION_TOKENS = 8_192
F3_EXECUTION_ATTEMPT = "f3a05"
F3_SYSTEM_INSTRUCTION = (
    "Use only the supplied active public context. Produce the requested concise "
    "memo, cite the public page pointer and NovaLearn source identifier for each "
    "material claim, and state uncertainty when the context is insufficient."
)
F3_TRIAL_IDS = tuple(
    f"f3-trial-{index:02d}" for index in range(1, F3_MINIMUM_COMPLETE_TRIALS + 1)
)
F3_PROVIDER_REPEAT_SAMPLE_IDS = tuple(
    f"f3-provider-repeat-{index:02d}"
    for index in range(1, F3_PROVIDER_REPEAT_SAMPLE_COUNT + 1)
)

MAX_PAGES = 256
MAX_PAGE_CONTENT_CHARS = 100_000
MAX_ACTIONS = 512
MAX_PUBLIC_ARTIFACT_BYTES = 2_000_000

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_LEDGER_RESERVATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_FORBIDDEN_REFERENCE_TOKENS = frozenset(
    {
        "sealed",
        "protected",
        "evaluation_only",
        "evaluation-only",
        "canonical_fact_ledger",
        "gold",
        "grade",
        "scoring",
    }
)
_PAGE_SPEC_FIELDS = frozenset(
    {
        "pointer",
        "kind",
        "parent_pointer",
        "child_pointers",
        "content",
        "evidence_ids",
        "dense_rank",
        "episode_rank",
    }
)
_PAGE_FIELDS = frozenset(
    {
        *_PAGE_SPEC_FIELDS,
        "content_sha256",
        "token_count",
    }
)
_CATALOG_FIELDS = frozenset(
    {"schema_version", "corpus_snapshot_id", "pages", "artifact_sha256"}
)
_SOURCE_MANIFEST_FIELDS = frozenset(
    {"schema_version", "corpus_snapshot_id", "pages", "artifact_sha256"}
)
_SOURCE_MANIFEST_PAGE_FIELDS = frozenset(
    {
        "pointer",
        "source_path",
        "source_sha256",
        "content_sha256",
        "page_spec_sha256",
    }
)
_APPROVED_SOURCE_COMMITMENT_FIELDS = frozenset({"path", "sha256"})
_WORKING_SET_FIELDS = frozenset(
    {
        "schema_version",
        "task_id",
        "strategy_id",
        "corpus_snapshot_id",
        "catalog_sha256",
        "token_budget",
        "instructions_hash",
        "initial_active_pointers",
        "active_pointers",
        "context_actions",
        "state_sha256",
    }
)
_PREPARATION_FIELDS = frozenset(
    {
        "schema_version",
        "task_id",
        "corpus_snapshot_id",
        "catalog_sha256",
        "requested_token_budget",
        "instructions_hash",
        "required_evidence_ids",
        "managed_actions",
        "execution_controls",
        "strategies",
        "artifact_sha256",
    }
)
_STRATEGY_FIELDS = frozenset(
    {
        "schema_version",
        "strategy_id",
        "preparation_status",
        "failure",
        "requested_token_budget",
        "working_set",
        "context_pack",
        "span",
        "metrics",
        "answer_quality_cells",
        "record_sha256",
    }
)
_METRIC_FIELDS = frozenset(
    {
        "active_tokens",
        "recovered_evidence_count",
        "required_evidence_count",
        "recovered_evidence_recall",
        "context_churn_tokens",
        "context_churn_ratio",
        "compression_loss",
        "preparation_latency_ms",
    }
)
_STRATEGIES = (
    "full_history",
    "dense_retrieval",
    "episodic_memory",
    "managed_working_set",
)
_EXPERIMENT_FIELDS = frozenset(
    {
        "schema_version",
        "experiment_id",
        "frontier_entry_gate_sha256",
        "approved_source_commitment",
        "source_manifest_artifact_sha256",
        "catalog",
        "preparation",
        "status",
        "answer_quality_status_counts",
        "artifact_sha256",
    }
)
_FINAL_FIELDS = frozenset(
    {
        "schema_version",
        "experiment_id",
        "frontier_entry_gate_sha256",
        "prepared_experiment_sha256",
        "task_id",
        "catalog_sha256",
        "status",
        "cells",
        "trial_ids",
        "provider_repeat_sample_ids",
        "ledger_reservation_ids",
        "generator_status_counts",
        "total_billed_cost_usd",
        "artifact_sha256",
    }
)
_TERMINAL_CELL_FIELDS = frozenset(
    {
        "schema_version",
        "strategy_id",
        "trial_id",
        "provider_repeat_sample_id",
        "reasoning_effort",
        "run_spec_sha256",
        "generator_receipt",
        "answer_quality_result",
        "evidence_result",
        "cell_result_sha256",
    }
)
_GENERATOR_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "run_spec_sha256",
        "trial_id",
        "provider_repeat_sample_id",
        "temperature",
        "requested_model",
        "resolved_model",
        "reasoning_effort",
        "provider",
        "request_id",
        "ledger_reservation_id",
        "native_usage",
        "billed_cost_usd",
        "price",
        "latency_ms",
        "retry_count",
        "status",
        "error",
        "output_sha256",
        "output_artifact",
        "receipt_sha256",
    }
)
_NATIVE_USAGE_FIELDS = frozenset(
    {"prompt_tokens", "completion_tokens", "reasoning_tokens", "cached_prompt_tokens"}
)
_PRICE_FIELDS = frozenset(
    {
        "currency",
        "input_usd_per_million",
        "output_usd_per_million",
        "source",
    }
)
_ARTIFACT_REFERENCE_FIELDS = frozenset({"path", "sha256"})
_PUBLIC_OUTPUT_FIELDS = frozenset(
    {
        "schema_version",
        "run_spec_sha256",
        "strategy_id",
        "reasoning_effort",
        "answer",
        "record_sha256",
    }
)
_PUBLIC_METRIC_FIELDS = frozenset(
    {
        "schema_version",
        "run_spec_sha256",
        "strategy_id",
        "reasoning_effort",
        "metric",
        "value",
        "output_sha256",
        "record_sha256",
    }
)
_F3_EVIDENCE_ROOT = Path("results/v2/frontier/f3/evidence-attempt-05")
_F3_REVIEWS_ROOT = Path("results/v2/frontier/f3/reviews-attempt-05")
_F3_PROVIDER_ROOT = Path("results/v2/frontier/f3/provider-attempt-05")


def _f3_execution_controls() -> dict[str, Any]:
    return {
        "frontier_protocol_schema": F3_FRONTIER_PROTOCOL_SCHEMA,
        "stochastic_trial_plan": {
            "stochastic": True,
            "minimum_complete_trials": F3_MINIMUM_COMPLETE_TRIALS,
            "trial_ids": list(F3_TRIAL_IDS),
        },
        "temperature_zero_provider_repeat_sample_plan": {
            "temperature": F3_TEMPERATURE,
            "minimum_provider_repeat_samples": F3_PROVIDER_REPEAT_SAMPLE_COUNT,
            "sample_ids": list(F3_PROVIDER_REPEAT_SAMPLE_IDS),
            "trial_sample_pairing": [
                {
                    "trial_id": trial_id,
                    "provider_repeat_sample_id": sample_id,
                }
                for trial_id, sample_id in zip(
                    F3_TRIAL_IDS, F3_PROVIDER_REPEAT_SAMPLE_IDS, strict=True
                )
            ],
        },
    }


class F3Error(ValueError):
    """Public F3 input, transition, replay, gate, or artifact is invalid."""


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise F3Error(f"{label} must be non-empty text")
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise F3Error(f"{label} must be a lowercase SHA-256")
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise F3Error(f"{label} must be an integer >= {minimum}")
    return value


def _optional_rank(value: Any, label: str) -> int | None:
    if value is None:
        return None
    return _integer(value, label, minimum=1)


def _public_pointer(value: Any, label: str) -> str:
    pointer = _text(value, label)
    lowered = pointer.casefold()
    if (
        not pointer.startswith("public/")
        or pointer.startswith("/")
        or ".." in Path(pointer).parts
        or any(token in lowered for token in _FORBIDDEN_REFERENCE_TOKENS)
    ):
        raise F3Error(f"{label} must be a repository-safe public pointer")
    return pointer


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise F3Error(f"{label} must be a list")
    rows = [_text(item, f"{label}[{index}]") for index, item in enumerate(value)]
    if len(rows) != len(set(rows)):
        raise F3Error(f"{label} must not contain duplicates")
    return rows


def _content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _without_hash(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != field}


def _public_source_path(value: Any, label: str) -> Path:
    path_value = _text(value, label)
    relative = Path(path_value)
    normalized = path_value.casefold().replace("-", "_")
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or "\\" in path_value
        or relative.as_posix() != path_value
        or any(token in normalized for token in _FORBIDDEN_REFERENCE_TOKENS)
    ):
        raise F3Error(f"{label} must be a repository-safe public path")
    return relative


def _read_public_source_bytes(repository: Path, relative: Path, label: str) -> bytes:
    cursor = repository
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise F3Error(f"{label} must not traverse a symlink")
    path = repository / relative
    if not path.is_file() or path.is_symlink():
        raise F3Error(f"{label} is missing or unsafe")
    raw = path.read_bytes()
    if len(raw) > MAX_PUBLIC_ARTIFACT_BYTES:
        raise F3Error(f"{label} exceeds the public artifact size bound")
    return raw


def _approved_source_commitment(value: Any) -> dict[str, str]:
    if (
        not isinstance(value, Mapping)
        or set(value) != _APPROVED_SOURCE_COMMITMENT_FIELDS
    ):
        raise F3Error("approved F3 source commitment fields changed")
    return {
        "path": _public_source_path(
            value.get("path"), "approved F3 source manifest path"
        ).as_posix(),
        "sha256": _sha(value.get("sha256"), "approved F3 source manifest file hash"),
    }


def _load_committed_f3_source_manifest(
    repository: Path, commitment: Mapping[str, Any]
) -> dict[str, Any]:
    """Read the exact approved manifest and replay its current public sources."""

    normalized = _approved_source_commitment(commitment)
    manifest_bytes = _read_public_source_bytes(
        repository,
        Path(normalized["path"]),
        "approved F3 source manifest",
    )
    if hashlib.sha256(manifest_bytes).hexdigest() != normalized["sha256"]:
        raise F3Error("approved F3 source manifest file hash mismatch")
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise F3Error("approved F3 source manifest must contain UTF-8 JSON") from exc
    if not isinstance(manifest, Mapping):
        raise F3Error("approved F3 source manifest must be an object")
    committed = deepcopy(dict(manifest))
    validate_f3_public_source_manifest(committed, root=repository)
    return committed


def build_page_catalog(
    page_specs: Sequence[Mapping[str, Any]], *, corpus_snapshot_id: str
) -> dict[str, Any]:
    """Build a pure fixture catalog; this seam does not authorize an F3 run."""

    if isinstance(page_specs, (str, bytes)) or not isinstance(page_specs, Sequence):
        raise F3Error("page specs must be a sequence")
    if not 1 <= len(page_specs) <= MAX_PAGES:
        raise F3Error(f"page catalog must contain 1..{MAX_PAGES} pages")
    pages: list[dict[str, Any]] = []
    for index, raw in enumerate(page_specs):
        if not isinstance(raw, Mapping) or set(raw) != _PAGE_SPEC_FIELDS:
            raise F3Error(f"page spec {index} fields changed")
        pointer = _public_pointer(raw.get("pointer"), f"page spec {index} pointer")
        kind = raw.get("kind")
        if kind not in {"summary", "raw", "episode"}:
            raise F3Error(f"{pointer}: unsupported page kind")
        parent = raw.get("parent_pointer")
        if parent is not None:
            parent = _public_pointer(parent, f"{pointer} parent pointer")
        children = [
            _public_pointer(child, f"{pointer} child pointer")
            for child in _string_list(raw.get("child_pointers"), f"{pointer} children")
        ]
        evidence_ids = sorted(
            _string_list(raw.get("evidence_ids"), f"{pointer} evidence IDs")
        )
        content = _text(raw.get("content"), f"{pointer} content")
        if len(content) > MAX_PAGE_CONTENT_CHARS:
            raise F3Error(f"{pointer}: content exceeds the public page bound")
        token_count = estimate_tokens(content)
        if token_count < 1:
            raise F3Error(f"{pointer}: content has no countable tokens")
        pages.append(
            {
                "pointer": pointer,
                "kind": kind,
                "parent_pointer": parent,
                "child_pointers": sorted(children),
                "content": content,
                "content_sha256": _content_sha256(content),
                "token_count": token_count,
                "evidence_ids": evidence_ids,
                "dense_rank": _optional_rank(
                    raw.get("dense_rank"), f"{pointer} dense rank"
                ),
                "episode_rank": _optional_rank(
                    raw.get("episode_rank"), f"{pointer} episode rank"
                ),
            }
        )
    catalog: dict[str, Any] = {
        "schema_version": F3_CATALOG_SCHEMA,
        "corpus_snapshot_id": _text(corpus_snapshot_id, "corpus snapshot ID"),
        "pages": sorted(pages, key=lambda page: page["pointer"]),
    }
    catalog["artifact_sha256"] = sha256_json(catalog)
    validate_page_catalog(catalog)
    return catalog


def validate_page_catalog(value: Mapping[str, Any]) -> None:
    """Reject non-canonical, unsafe, cyclic, or tampered public page catalogs."""

    if not isinstance(value, Mapping) or set(value) != _CATALOG_FIELDS:
        raise F3Error("F3 page catalog fields changed")
    if value.get("schema_version") != F3_CATALOG_SCHEMA:
        raise F3Error("unsupported F3 page catalog schema")
    _text(value.get("corpus_snapshot_id"), "corpus snapshot ID")
    pages = value.get("pages")
    if not isinstance(pages, list) or not 1 <= len(pages) <= MAX_PAGES:
        raise F3Error(f"page catalog must contain 1..{MAX_PAGES} pages")
    pointers: list[str] = []
    by_pointer: dict[str, Mapping[str, Any]] = {}
    dense_ranks: list[int] = []
    episode_ranks: list[int] = []
    for index, page in enumerate(pages):
        if not isinstance(page, Mapping) or set(page) != _PAGE_FIELDS:
            raise F3Error(f"page {index} fields changed")
        pointer = _public_pointer(page.get("pointer"), f"page {index} pointer")
        pointers.append(pointer)
        by_pointer[pointer] = page
        kind = page.get("kind")
        if kind not in {"summary", "raw", "episode"}:
            raise F3Error(f"{pointer}: unsupported page kind")
        parent = page.get("parent_pointer")
        if parent is not None:
            _public_pointer(parent, f"{pointer} parent pointer")
        children = _string_list(page.get("child_pointers"), f"{pointer} children")
        if children != sorted(children):
            raise F3Error(f"{pointer}: children are not canonical")
        for child in children:
            _public_pointer(child, f"{pointer} child pointer")
        if kind == "raw" and children:
            raise F3Error(f"{pointer}: raw pages cannot have children")
        if kind != "raw" and not children:
            raise F3Error(f"{pointer}: compressed pages need raw pointers")
        evidence_ids = _string_list(page.get("evidence_ids"), f"{pointer} evidence IDs")
        if evidence_ids != sorted(evidence_ids):
            raise F3Error(f"{pointer}: evidence IDs are not canonical")
        content = _text(page.get("content"), f"{pointer} content")
        if len(content) > MAX_PAGE_CONTENT_CHARS:
            raise F3Error(f"{pointer}: content exceeds the public page bound")
        if page.get("content_sha256") != _content_sha256(content):
            raise F3Error(f"{pointer}: content hash mismatch")
        if (
            page.get("token_count") != estimate_tokens(content)
            or page["token_count"] < 1
        ):
            raise F3Error(f"{pointer}: token count mismatch")
        dense_rank = _optional_rank(page.get("dense_rank"), f"{pointer} dense rank")
        episode_rank = _optional_rank(
            page.get("episode_rank"), f"{pointer} episode rank"
        )
        if dense_rank is not None:
            if kind != "raw":
                raise F3Error(f"{pointer}: dense ranks are only valid on raw pages")
            dense_ranks.append(dense_rank)
        if episode_rank is not None:
            if kind != "episode":
                raise F3Error(
                    f"{pointer}: episode ranks are only valid on episode pages"
                )
            episode_ranks.append(episode_rank)
    if pointers != sorted(pointers) or len(pointers) != len(set(pointers)):
        raise F3Error("page pointers must be unique and canonical")
    if len(dense_ranks) != len(set(dense_ranks)):
        raise F3Error("dense ranks must be unique")
    if len(episode_ranks) != len(set(episode_ranks)):
        raise F3Error("episode ranks must be unique")
    for pointer, page in by_pointer.items():
        parent = page["parent_pointer"]
        if parent is not None and (parent == pointer or parent not in by_pointer):
            raise F3Error(f"{pointer}: parent pointer is invalid")
        for child in page["child_pointers"]:
            if child == pointer or child not in by_pointer:
                raise F3Error(f"{pointer}: child pointer is invalid")
            if (
                page["kind"] == "summary"
                and by_pointer[child]["parent_pointer"] != pointer
            ):
                raise F3Error(f"{pointer}: summary hierarchy is inconsistent")
    for origin in pointers:
        seen: set[str] = set()
        cursor: str | None = origin
        while cursor is not None:
            if cursor in seen:
                raise F3Error("page hierarchy contains a cycle")
            seen.add(cursor)
            parent = by_pointer[cursor]["parent_pointer"]
            cursor = str(parent) if parent is not None else None
    if value.get("artifact_sha256") != sha256_json(
        _without_hash(value, "artifact_sha256")
    ):
        raise F3Error("F3 page catalog hash mismatch")


def build_approved_page_catalog(
    page_specs: Sequence[Mapping[str, Any]],
    *,
    corpus_snapshot_id: str,
    source_manifest: Mapping[str, Any],
    approved_source_commitment: Mapping[str, Any],
    root: Path | None = None,
) -> dict[str, Any]:
    """Build a catalog only from page specs frozen in an approved source manifest.

    The external commitment binds the exact manifest file bytes.  The manifest
    then binds each full page specification and the bytes of its public source
    artifact.  A public-looking path alone is never treated as authorization.
    """

    repository = (root or repository_root()).resolve()
    commitment = _approved_source_commitment(approved_source_commitment)
    committed_manifest = _load_committed_f3_source_manifest(repository, commitment)
    if committed_manifest != dict(source_manifest):
        raise F3Error("supplied F3 source manifest differs from its approved artifact")

    if (
        not isinstance(source_manifest, Mapping)
        or set(source_manifest) != _SOURCE_MANIFEST_FIELDS
    ):
        raise F3Error("F3 source manifest fields changed")
    if source_manifest.get("schema_version") != F3_SOURCE_MANIFEST_SCHEMA:
        raise F3Error("unsupported F3 source manifest schema")
    snapshot = _text(corpus_snapshot_id, "corpus snapshot ID")
    if source_manifest.get("corpus_snapshot_id") != snapshot:
        raise F3Error("F3 source manifest corpus snapshot changed")
    if source_manifest.get("artifact_sha256") != sha256_json(
        _without_hash(source_manifest, "artifact_sha256")
    ):
        raise F3Error("F3 source manifest artifact hash mismatch")

    catalog = build_page_catalog(page_specs, corpus_snapshot_id=snapshot)
    raw_specs = {str(raw["pointer"]): dict(raw) for raw in page_specs}
    catalog_pages = {str(page["pointer"]): page for page in catalog["pages"]}
    manifest_pages = source_manifest.get("pages")
    if (
        not isinstance(manifest_pages, list)
        or not 1 <= len(manifest_pages) <= MAX_PAGES
    ):
        raise F3Error(f"F3 source manifest must contain 1..{MAX_PAGES} pages")

    pointers: list[str] = []
    observed_sources: dict[str, str] = {}
    for index, raw in enumerate(manifest_pages):
        if not isinstance(raw, Mapping) or set(raw) != _SOURCE_MANIFEST_PAGE_FIELDS:
            raise F3Error(f"F3 source manifest page {index} fields changed")
        pointer = _public_pointer(
            raw.get("pointer"), f"F3 source manifest page {index} pointer"
        )
        pointers.append(pointer)
        source_path = _public_source_path(
            raw.get("source_path"), f"{pointer} approved source path"
        )
        source_sha = _sha(raw.get("source_sha256"), f"{pointer} source hash")
        content_sha = _sha(raw.get("content_sha256"), f"{pointer} content hash")
        page_spec_sha = _sha(raw.get("page_spec_sha256"), f"{pointer} page-spec hash")
        page = catalog_pages.get(pointer)
        page_spec = raw_specs.get(pointer)
        if page is None or page_spec is None:
            raise F3Error(f"{pointer}: page is not in the approved source manifest")
        if page["content_sha256"] != content_sha:
            raise F3Error(
                f"{pointer}: page content is not approved by the source manifest"
            )
        if sha256_json(page_spec) != page_spec_sha:
            raise F3Error(f"{pointer}: page specification is not approved")

        path_value = source_path.as_posix()
        previous_source_sha = observed_sources.setdefault(path_value, source_sha)
        if previous_source_sha != source_sha:
            raise F3Error(f"{pointer}: approved source path has conflicting hashes")
        source_bytes = _read_public_source_bytes(
            repository, source_path, f"{pointer} approved source artifact"
        )
        if hashlib.sha256(source_bytes).hexdigest() != source_sha:
            raise F3Error(f"{pointer}: approved source artifact hash mismatch")

    if pointers != sorted(pointers) or len(pointers) != len(set(pointers)):
        raise F3Error("F3 source manifest pointers must be unique and canonical")
    if set(pointers) != set(catalog_pages):
        raise F3Error("F3 page specs differ from the approved source manifest")
    return catalog


def _replay_catalog_from_committed_sources(
    repository: Path,
    *,
    commitment: Mapping[str, Any],
    source_manifest_artifact_sha256: Any,
    catalog: Mapping[str, Any],
) -> None:
    """Prove a saved F3 catalog still derives from Kevin's exact manifest bytes."""

    normalized = _approved_source_commitment(commitment)
    committed_manifest = _load_committed_f3_source_manifest(repository, normalized)
    if committed_manifest["artifact_sha256"] != _sha(
        source_manifest_artifact_sha256, "F3 source manifest artifact hash"
    ):
        raise F3Error("F3 result source manifest binding changed")
    validate_page_catalog(catalog)
    specs = [
        {field: deepcopy(page[field]) for field in _PAGE_SPEC_FIELDS}
        for page in catalog["pages"]
    ]
    replayed = build_approved_page_catalog(
        specs,
        corpus_snapshot_id=str(catalog["corpus_snapshot_id"]),
        source_manifest=committed_manifest,
        approved_source_commitment=normalized,
        root=repository,
    )
    if dict(catalog) != replayed:
        raise F3Error("F3 saved catalog differs from the approved source manifest")


def validate_f3_public_source_manifest(
    value: Mapping[str, Any], *, root: Path | None = None
) -> None:
    """Validate one exact public-source manifest and its source byte hashes."""

    if not isinstance(value, Mapping) or set(value) != _SOURCE_MANIFEST_FIELDS:
        raise F3Error("F3 source manifest fields changed")
    if value.get("schema_version") != F3_SOURCE_MANIFEST_SCHEMA:
        raise F3Error("unsupported F3 source manifest schema")
    _text(value.get("corpus_snapshot_id"), "F3 source corpus snapshot ID")
    if value.get("artifact_sha256") != sha256_json(
        _without_hash(value, "artifact_sha256")
    ):
        raise F3Error("F3 source manifest artifact hash mismatch")
    pages = value.get("pages")
    if not isinstance(pages, list) or not 1 <= len(pages) <= MAX_PAGES:
        raise F3Error(f"F3 source manifest must contain 1..{MAX_PAGES} pages")
    pointers: list[str] = []
    observed_sources: dict[str, str] = {}
    repository = root.resolve() if root is not None else None
    for index, raw in enumerate(pages):
        if not isinstance(raw, Mapping) or set(raw) != _SOURCE_MANIFEST_PAGE_FIELDS:
            raise F3Error(f"F3 source manifest page {index} fields changed")
        pointer = _public_pointer(
            raw.get("pointer"), f"F3 source manifest page {index} pointer"
        )
        pointers.append(pointer)
        source_path = _public_source_path(
            raw.get("source_path"), f"{pointer} approved source path"
        )
        source_sha = _sha(raw.get("source_sha256"), f"{pointer} source hash")
        content_sha = _sha(raw.get("content_sha256"), f"{pointer} content hash")
        if source_sha != content_sha:
            raise F3Error(f"{pointer}: approved source bytes differ from page content")
        _sha(raw.get("page_spec_sha256"), f"{pointer} page-spec hash")
        path_value = source_path.as_posix()
        previous = observed_sources.setdefault(path_value, source_sha)
        if previous != source_sha:
            raise F3Error(f"{pointer}: approved source path has conflicting hashes")
        if repository is not None:
            source_bytes = _read_public_source_bytes(
                repository, source_path, f"{pointer} approved source artifact"
            )
            if hashlib.sha256(source_bytes).hexdigest() != source_sha:
                raise F3Error(f"{pointer}: approved source artifact hash mismatch")
    if pointers != sorted(pointers) or len(pointers) != len(set(pointers)):
        raise F3Error("F3 source manifest pointers must be unique and canonical")


def build_f3_public_source_manifest(
    root: Path | None = None,
    *,
    page_specs: Sequence[Mapping[str, Any]],
    source_paths: Mapping[str, str],
    corpus_snapshot_id: str,
) -> dict[str, Any]:
    """Build the pre-entry manifest Kevin and both AI reviewers will approve."""

    repository = (root or repository_root()).resolve()
    catalog = build_page_catalog(page_specs, corpus_snapshot_id=corpus_snapshot_id)
    if not isinstance(source_paths, Mapping):
        raise F3Error("F3 source paths must be an object keyed by page pointer")
    specs = {str(raw["pointer"]): dict(raw) for raw in page_specs}
    pointers = sorted(specs)
    if set(source_paths) != set(pointers):
        raise F3Error("F3 source paths must cover every page exactly once")
    catalog_pages = {str(page["pointer"]): page for page in catalog["pages"]}
    pages: list[dict[str, str]] = []
    for pointer in pointers:
        source_path = _public_source_path(
            source_paths[pointer], f"{pointer} source path"
        )
        source_bytes = _read_public_source_bytes(
            repository, source_path, f"{pointer} source artifact"
        )
        try:
            source_content = source_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise F3Error(f"{pointer}: source artifact must be UTF-8 text") from exc
        if source_content != str(specs[pointer]["content"]):
            raise F3Error(f"{pointer}: source artifact differs from page content")
        pages.append(
            {
                "pointer": pointer,
                "source_path": source_path.as_posix(),
                "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
                "content_sha256": str(catalog_pages[pointer]["content_sha256"]),
                "page_spec_sha256": sha256_json(specs[pointer]),
            }
        )
    manifest: dict[str, Any] = {
        "schema_version": F3_SOURCE_MANIFEST_SCHEMA,
        "corpus_snapshot_id": catalog["corpus_snapshot_id"],
        "pages": pages,
    }
    manifest["artifact_sha256"] = sha256_json(manifest)
    validate_f3_public_source_manifest(manifest, root=repository)
    return manifest


def write_f3_public_source_manifest(
    root: Path | None = None, *, manifest: Mapping[str, Any]
) -> dict[str, str]:
    """Persist the canonical pre-entry source manifest once and return its commitment."""

    repository = (root or repository_root()).resolve()
    validate_f3_public_source_manifest(manifest, root=repository)
    path = _safe_result_path(repository, F3_SOURCE_MANIFEST_PATH)
    _write_immutable_result(
        repository, path, manifest, label="F3 public source manifest"
    )
    return {
        "path": F3_SOURCE_MANIFEST_PATH.as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _page_map(catalog: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    validate_page_catalog(catalog)
    return {str(page["pointer"]): page for page in catalog["pages"]}


def _state_hash(state: Mapping[str, Any]) -> str:
    return sha256_json(_without_hash(state, "state_sha256"))


def _new_state(
    *,
    task_id: str,
    strategy_id: str,
    catalog: Mapping[str, Any],
    token_budget: int,
    instructions_hash: str,
    initial_active_pointers: list[str],
    active_pointers: list[str],
    context_actions: list[dict[str, Any]],
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "schema_version": F3_WORKING_SET_SCHEMA,
        "task_id": task_id,
        "strategy_id": strategy_id,
        "corpus_snapshot_id": catalog["corpus_snapshot_id"],
        "catalog_sha256": catalog["artifact_sha256"],
        "token_budget": token_budget,
        "instructions_hash": instructions_hash,
        "initial_active_pointers": initial_active_pointers,
        "active_pointers": active_pointers,
        "context_actions": context_actions,
    }
    state["state_sha256"] = _state_hash(state)
    return state


def _active_tokens(
    active: Sequence[str], pages: Mapping[str, Mapping[str, Any]]
) -> int:
    return sum(int(pages[pointer]["token_count"]) for pointer in active)


def build_working_set(
    catalog: Mapping[str, Any],
    *,
    task_id: str,
    token_budget: int,
    instructions_hash: str,
    strategy_id: str = "managed_working_set",
    initial_active_pointers: Sequence[str] = (),
) -> dict[str, Any]:
    """Create an empty or explicitly seeded bounded working set."""

    pages = _page_map(catalog)
    task = _text(task_id, "F3 task ID")
    strategy = _text(strategy_id, "F3 strategy ID")
    budget = _integer(token_budget, "F3 token budget", minimum=1)
    instructions = _sha(instructions_hash, "F3 instructions hash")
    active = sorted(
        _public_pointer(pointer, "initial active pointer")
        for pointer in initial_active_pointers
    )
    if len(active) != len(set(active)) or not set(active).issubset(pages):
        raise F3Error("initial active pointers are invalid")
    if _active_tokens(active, pages) > budget:
        raise F3Error("initial working set exceeds its token budget")
    state = _new_state(
        task_id=task,
        strategy_id=strategy,
        catalog=catalog,
        token_budget=budget,
        instructions_hash=instructions,
        initial_active_pointers=active,
        active_pointers=list(active),
        context_actions=[],
    )
    validate_working_set(state, catalog)
    return state


def _action_id(action_without_id: Mapping[str, Any], previous_action_id: str) -> str:
    return sha256_json(
        {
            "previous_action_id": previous_action_id,
            "action": dict(action_without_id),
        }
    )


def _make_action(
    state: Mapping[str, Any],
    *,
    operation: str,
    pointer: str,
    content_sha256: str,
    token_delta: int,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": "contextlab.context-action.v1",
        "sequence": len(state["context_actions"]),
        "operation": operation,
        "pointer": pointer,
        "content_sha256": content_sha256,
        "token_delta": token_delta,
    }
    previous = (
        state["context_actions"][-1]["action_id"]
        if state["context_actions"]
        else "0" * 64
    )
    return {**body, "action_id": _action_id(body, previous)}


def _apply_page_in(
    active: list[str], pages: Mapping[str, Mapping[str, Any]], pointer: str, budget: int
) -> tuple[list[str], int]:
    if pointer not in pages:
        raise F3Error(f"unknown public page pointer: {pointer}")
    if pointer in active:
        raise F3Error(f"page is already active: {pointer}")
    updated = sorted([*active, pointer])
    if _active_tokens(updated, pages) > budget:
        raise F3Error("page_in would exceed the active token budget")
    return updated, int(pages[pointer]["token_count"])


def _apply_page_out(
    active: list[str], pages: Mapping[str, Mapping[str, Any]], pointer: str
) -> tuple[list[str], int]:
    if pointer not in pages:
        raise F3Error(f"unknown public page pointer: {pointer}")
    if pointer not in active:
        raise F3Error(f"page is not active: {pointer}")
    updated = sorted(item for item in active if item != pointer)
    return updated, -int(pages[pointer]["token_count"])


def _apply_expand(
    active: list[str],
    pages: Mapping[str, Mapping[str, Any]],
    pointer: str,
    budget: int,
) -> tuple[list[str], int]:
    if pointer not in pages:
        raise F3Error(f"unknown public page pointer: {pointer}")
    page = pages[pointer]
    if pointer not in active:
        raise F3Error(f"page is not active: {pointer}")
    if page["kind"] == "raw":
        raise F3Error("raw pages cannot be expanded")
    children = list(page["child_pointers"])
    if any(child in active for child in children):
        raise F3Error("expanded children must not already be active")
    updated = sorted([*(item for item in active if item != pointer), *children])
    if _active_tokens(updated, pages) > budget:
        raise F3Error("expand would exceed the active token budget")
    delta = sum(int(pages[child]["token_count"]) for child in children) - int(
        page["token_count"]
    )
    return updated, delta


def page_in(
    state: Mapping[str, Any], catalog: Mapping[str, Any], pointer: str
) -> dict[str, Any]:
    """Page one public catalog node into the active bounded context."""

    current = replay_working_set(state, catalog)
    pages = _page_map(catalog)
    target = _public_pointer(pointer, "page_in pointer")
    active, token_delta = _apply_page_in(
        list(current["active_pointers"]), pages, target, int(current["token_budget"])
    )
    action = _make_action(
        current,
        operation="page_in",
        pointer=target,
        content_sha256=str(pages[target]["content_sha256"]),
        token_delta=token_delta,
    )
    updated = _new_state(
        task_id=str(current["task_id"]),
        strategy_id=str(current["strategy_id"]),
        catalog=catalog,
        token_budget=int(current["token_budget"]),
        instructions_hash=str(current["instructions_hash"]),
        initial_active_pointers=list(current["initial_active_pointers"]),
        active_pointers=active,
        context_actions=[*deepcopy(current["context_actions"]), action],
    )
    validate_working_set(updated, catalog)
    return updated


def page_out(
    state: Mapping[str, Any], catalog: Mapping[str, Any], pointer: str
) -> dict[str, Any]:
    """Evict one exact public page while retaining its stable catalog pointer."""

    current = replay_working_set(state, catalog)
    pages = _page_map(catalog)
    target = _public_pointer(pointer, "page_out pointer")
    active, token_delta = _apply_page_out(
        list(current["active_pointers"]), pages, target
    )
    action = _make_action(
        current,
        operation="page_out",
        pointer=target,
        content_sha256=str(pages[target]["content_sha256"]),
        token_delta=token_delta,
    )
    updated = _new_state(
        task_id=str(current["task_id"]),
        strategy_id=str(current["strategy_id"]),
        catalog=catalog,
        token_budget=int(current["token_budget"]),
        instructions_hash=str(current["instructions_hash"]),
        initial_active_pointers=list(current["initial_active_pointers"]),
        active_pointers=active,
        context_actions=[*deepcopy(current["context_actions"]), action],
    )
    validate_working_set(updated, catalog)
    return updated


def expand(
    state: Mapping[str, Any], catalog: Mapping[str, Any], pointer: str
) -> dict[str, Any]:
    """Replace one active summary or episode with its declared child pages."""

    current = replay_working_set(state, catalog)
    pages = _page_map(catalog)
    target = _public_pointer(pointer, "expand pointer")
    active, token_delta = _apply_expand(
        list(current["active_pointers"]),
        pages,
        target,
        int(current["token_budget"]),
    )
    action = _make_action(
        current,
        operation="expand",
        pointer=target,
        content_sha256=str(pages[target]["content_sha256"]),
        token_delta=token_delta,
    )
    updated = _new_state(
        task_id=str(current["task_id"]),
        strategy_id=str(current["strategy_id"]),
        catalog=catalog,
        token_budget=int(current["token_budget"]),
        instructions_hash=str(current["instructions_hash"]),
        initial_active_pointers=list(current["initial_active_pointers"]),
        active_pointers=active,
        context_actions=[*deepcopy(current["context_actions"]), action],
    )
    validate_working_set(updated, catalog)
    return updated


def quote_recovery(
    state: Mapping[str, Any], catalog: Mapping[str, Any], pointer: str
) -> dict[str, Any]:
    """Recover the exact text of one raw page through its stable public pointer."""

    current = replay_working_set(state, catalog)
    pages = _page_map(catalog)
    target = _public_pointer(pointer, "quote_recovery pointer")
    if target not in pages or pages[target]["kind"] != "raw":
        raise F3Error("quote_recovery requires a raw public page")
    active, token_delta = _apply_page_in(
        list(current["active_pointers"]), pages, target, int(current["token_budget"])
    )
    action = _make_action(
        current,
        operation="quote_recovery",
        pointer=target,
        content_sha256=str(pages[target]["content_sha256"]),
        token_delta=token_delta,
    )
    updated = _new_state(
        task_id=str(current["task_id"]),
        strategy_id=str(current["strategy_id"]),
        catalog=catalog,
        token_budget=int(current["token_budget"]),
        instructions_hash=str(current["instructions_hash"]),
        initial_active_pointers=list(current["initial_active_pointers"]),
        active_pointers=active,
        context_actions=[*deepcopy(current["context_actions"]), action],
    )
    validate_working_set(updated, catalog)
    return updated


def _replay_actions(
    state: Mapping[str, Any], pages: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    active = list(state["initial_active_pointers"])
    previous = "0" * 64
    for sequence, raw in enumerate(state["context_actions"]):
        if not isinstance(raw, Mapping):
            raise F3Error(f"context action {sequence} must be an object")
        if raw.get("sequence") != sequence:
            raise F3Error("context actions are not sequence-complete")
        body = {key: item for key, item in raw.items() if key != "action_id"}
        if raw.get("action_id") != _action_id(body, previous):
            raise F3Error("context action chain hash mismatch")
        previous = str(raw["action_id"])
        pointer = _public_pointer(
            raw.get("pointer"), f"context action {sequence} pointer"
        )
        if raw.get("operation") == "page_in":
            active, delta = _apply_page_in(
                active, pages, pointer, int(state["token_budget"])
            )
        elif raw.get("operation") == "page_out":
            active, delta = _apply_page_out(active, pages, pointer)
        elif raw.get("operation") == "expand":
            active, delta = _apply_expand(
                active, pages, pointer, int(state["token_budget"])
            )
        elif raw.get("operation") == "quote_recovery":
            if pages[pointer]["kind"] != "raw":
                raise F3Error("quote_recovery requires a raw public page")
            active, delta = _apply_page_in(
                active, pages, pointer, int(state["token_budget"])
            )
        else:
            raise F3Error(f"unsupported context operation: {raw.get('operation')}")
        if (
            raw.get("content_sha256") != pages[pointer]["content_sha256"]
            or raw.get("token_delta") != delta
        ):
            raise F3Error("context action content binding changed")
    return active


def validate_working_set(value: Mapping[str, Any], catalog: Mapping[str, Any]) -> None:
    """Validate and replay every action in a hash-bound working set."""

    pages = _page_map(catalog)
    if not isinstance(value, Mapping) or set(value) != _WORKING_SET_FIELDS:
        raise F3Error("F3 working-set fields changed")
    if value.get("schema_version") != F3_WORKING_SET_SCHEMA:
        raise F3Error("unsupported F3 working-set schema")
    _text(value.get("task_id"), "F3 task ID")
    _text(value.get("strategy_id"), "F3 strategy ID")
    if value.get("corpus_snapshot_id") != catalog["corpus_snapshot_id"]:
        raise F3Error("working set corpus snapshot changed")
    if value.get("catalog_sha256") != catalog["artifact_sha256"]:
        raise F3Error("working set catalog binding changed")
    budget = _integer(value.get("token_budget"), "F3 token budget", minimum=1)
    _sha(value.get("instructions_hash"), "F3 instructions hash")
    initial = _string_list(
        value.get("initial_active_pointers"), "initial active pointers"
    )
    active = _string_list(value.get("active_pointers"), "active pointers")
    if initial != sorted(initial) or active != sorted(active):
        raise F3Error("working-set pointers are not canonical")
    for pointer in [*initial, *active]:
        _public_pointer(pointer, "working-set pointer")
    if not set(initial).issubset(pages) or not set(active).issubset(pages):
        raise F3Error("working set references an unknown page")
    if (
        _active_tokens(initial, pages) > budget
        or _active_tokens(active, pages) > budget
    ):
        raise F3Error("working set exceeds its token budget")
    actions = value.get("context_actions")
    if not isinstance(actions, list) or len(actions) > MAX_ACTIONS:
        raise F3Error(f"working set exceeds the {MAX_ACTIONS}-action bound")
    if value.get("state_sha256") != _state_hash(value):
        raise F3Error("F3 working-set hash mismatch")
    replayed = _replay_actions(value, pages)
    if replayed != active:
        raise F3Error("working-set replay differs from saved active context")
    materialize_context_records(value, catalog, _validated=True)


def replay_working_set(
    value: Mapping[str, Any], catalog: Mapping[str, Any]
) -> dict[str, Any]:
    """Return an exact copy only when the full saved transition history replays."""

    validate_working_set(value, catalog)
    return deepcopy(dict(value))


def _render_active(
    active: Sequence[str], pages: Mapping[str, Mapping[str, Any]]
) -> str:
    return "\n\n".join(
        f"[{pointer}@{pages[pointer]['content_sha256']}]\n{pages[pointer]['content']}"
        for pointer in active
    )


def materialize_context_records(
    state: Mapping[str, Any],
    catalog: Mapping[str, Any],
    *,
    build_time_ms: int = 0,
    _validated: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bind the exact action history into both ContextPack and Span contracts."""

    if not _validated:
        validate_working_set(state, catalog)
    pages = _page_map(catalog)
    measured_build_time = _integer(build_time_ms, "ContextPack build time", minimum=0)
    active = list(state["active_pointers"])
    rendered_hash = _content_sha256(_render_active(active, pages))
    actions = deepcopy(list(state["context_actions"]))
    context_pack: dict[str, Any] = {
        "schema_version": "contextlab.context-pack.v1",
        "task_id": state["task_id"],
        "strategy_id": state["strategy_id"],
        "corpus_snapshot_id": state["corpus_snapshot_id"],
        "selected_candidate_ids": active,
        "token_budget": state["token_budget"],
        "rendered_context_hash": rendered_hash,
        "instructions_hash": state["instructions_hash"],
        "build_time_ms": measured_build_time,
        "context_actions": actions,
    }
    span: dict[str, Any] = {
        "schema_version": "contextlab.span.v1",
        "trace_id": sha256_json(
            {"task_id": state["task_id"], "catalog": state["catalog_sha256"]}
        ),
        "span_id": sha256_json(
            {"state": state["state_sha256"], "operation": "f3.prepare_context"}
        ),
        "parent_span_id": None,
        "operation_name": "contextlab.f3.prepare_context",
        "start_time": "1970-01-01T00:00:00Z",
        "end_time": "1970-01-01T00:00:00Z",
        "input_hash": state["catalog_sha256"],
        "output_hash": state["state_sha256"],
        "attributes": {
            "task_id": state["task_id"],
            "strategy_id": state["strategy_id"],
        },
        "events": [],
        "context_actions": actions,
        "status": "ok",
    }
    context_errors = validate_instance("ContextPack", context_pack)
    span_errors = validate_instance("Span", span)
    if context_errors or span_errors:
        raise F3Error(
            f"F3 context-action contract failed: {context_errors + span_errors}"
        )
    return context_pack, span


def _normalize_managed_actions(
    actions: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    if isinstance(actions, (str, bytes)) or not isinstance(actions, Sequence):
        raise F3Error("managed action plan must be a sequence")
    if len(actions) > MAX_ACTIONS:
        raise F3Error(f"managed action plan exceeds the {MAX_ACTIONS}-action bound")
    normalized: list[dict[str, str]] = []
    for index, raw in enumerate(actions):
        if not isinstance(raw, Mapping) or set(raw) != {"operation", "pointer"}:
            raise F3Error(f"managed action {index} fields changed")
        operation = raw.get("operation")
        if operation not in {"page_in", "page_out", "expand", "quote_recovery"}:
            raise F3Error(f"managed action {index} operation is unsupported")
        normalized.append(
            {
                "operation": str(operation),
                "pointer": _public_pointer(
                    raw.get("pointer"), f"managed action {index} pointer"
                ),
            }
        )
    return normalized


def apply_context_action_plan(
    state: Mapping[str, Any],
    catalog: Mapping[str, Any],
    actions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Apply a bounded public operation plan through the four paging seams."""

    action_plan = _normalize_managed_actions(actions)
    current = replay_working_set(state, catalog)
    operations = {
        "page_in": page_in,
        "page_out": page_out,
        "expand": expand,
        "quote_recovery": quote_recovery,
    }
    for raw in action_plan:
        current = operations[raw["operation"]](current, catalog, raw["pointer"])
    return current


def _ranked_selection(
    pages: Mapping[str, Mapping[str, Any]],
    *,
    kind: str,
    rank_field: str,
    token_budget: int,
) -> list[str]:
    ranked = sorted(
        (
            page
            for page in pages.values()
            if page["kind"] == kind and page[rank_field] is not None
        ),
        key=lambda page: (int(page[rank_field]), str(page["pointer"])),
    )
    selected: list[str] = []
    used = 0
    for page in ranked:
        size = int(page["token_count"])
        if used + size <= token_budget:
            selected.append(str(page["pointer"]))
            used += size
    return sorted(selected)


def _clock_read(clock_ns: Callable[[], int], label: str) -> int:
    value = clock_ns()
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise F3Error(f"{label} must return non-negative integer nanoseconds")
    return value


def _measure(
    clock_ns: Callable[[], int], operation: Callable[[], dict[str, Any]]
) -> tuple[dict[str, Any], float]:
    started = _clock_read(clock_ns, "F3 monotonic clock")
    result = operation()
    ended = _clock_read(clock_ns, "F3 monotonic clock")
    if ended < started:
        raise F3Error("F3 monotonic clock moved backwards")
    return result, round((ended - started) / 1_000_000, 6)


def _context_churn_tokens(
    state: Mapping[str, Any], pages: Mapping[str, Mapping[str, Any]]
) -> int:
    churn = _active_tokens(state["initial_active_pointers"], pages)
    for action in state["context_actions"]:
        pointer = str(action["pointer"])
        operation = action["operation"]
        if operation in {"page_in", "page_out", "quote_recovery"}:
            churn += int(pages[pointer]["token_count"])
        elif operation == "expand":
            churn += int(pages[pointer]["token_count"])
            churn += sum(
                int(pages[child]["token_count"])
                for child in pages[pointer]["child_pointers"]
            )
    return churn


def _metrics(
    state: Mapping[str, Any],
    pages: Mapping[str, Mapping[str, Any]],
    required_evidence_ids: Sequence[str],
    latency_ms: float,
) -> dict[str, Any]:
    active = list(state["active_pointers"])
    active_tokens = _active_tokens(active, pages)
    recovered = {
        evidence_id
        for pointer in active
        if pages[pointer]["kind"] == "raw"
        for evidence_id in pages[pointer]["evidence_ids"]
    }
    required = set(required_evidence_ids)
    recovered_count = len(required & recovered)
    raw_pages = [page for page in pages.values() if page["kind"] == "raw"]
    raw_tokens = sum(int(page["token_count"]) for page in raw_pages)
    active_raw_tokens = sum(
        int(pages[pointer]["token_count"])
        for pointer in active
        if pages[pointer]["kind"] == "raw"
    )
    churn = _context_churn_tokens(state, pages)
    return {
        "active_tokens": active_tokens,
        "recovered_evidence_count": recovered_count,
        "required_evidence_count": len(required),
        "recovered_evidence_recall": round(recovered_count / len(required), 6),
        "context_churn_tokens": churn,
        "context_churn_ratio": round(churn / max(1, active_tokens), 6),
        "compression_loss": round(1 - (active_raw_tokens / raw_tokens), 6),
        "preparation_latency_ms": latency_ms,
    }


def _answer_quality_cells(
    *,
    task_id: str,
    strategy_id: str,
    requested_token_budget: int,
    catalog_sha256: str,
    instructions_hash: str,
    required_evidence_ids: Sequence[str],
    managed_actions: Sequence[Mapping[str, str]],
    working_set: Mapping[str, Any],
    context_pack: Mapping[str, Any],
    span: Mapping[str, Any],
) -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    if tuple(ALLOWED_REASONING_EFFORTS) != ("low", "high"):
        raise F3Error("F3 reasoning efforts changed")
    for trial_id, sample_id in zip(
        F3_TRIAL_IDS, F3_PROVIDER_REPEAT_SAMPLE_IDS, strict=True
    ):
        for effort in ALLOWED_REASONING_EFFORTS:
            spec = {
                "experiment_id": "F3",
                "frontier_protocol_schema": F3_FRONTIER_PROTOCOL_SCHEMA,
                "task_id": task_id,
                "strategy_id": strategy_id,
                "trial_id": trial_id,
                "provider_repeat_sample_id": sample_id,
                "temperature": F3_TEMPERATURE,
                "requested_token_budget": requested_token_budget,
                "reasoning_effort": effort,
                "requested_model": MODEL_ID,
                "required_provider": FRONTIER_PROVIDER_SLUG,
                "catalog_sha256": catalog_sha256,
                "instructions_hash": instructions_hash,
                "required_evidence_ids": list(required_evidence_ids),
                "managed_actions": deepcopy(list(managed_actions)),
                "working_set": deepcopy(dict(working_set)),
                "context_pack": deepcopy(dict(context_pack)),
                "span": deepcopy(dict(span)),
            }
            cells.append(
                {
                    "trial_id": trial_id,
                    "provider_repeat_sample_id": sample_id,
                    "temperature": F3_TEMPERATURE,
                    "reasoning_effort": effort,
                    "requested_model": MODEL_ID,
                    "required_provider": FRONTIER_PROVIDER_SLUG,
                    "run_spec_sha256": sha256_json(spec),
                    "status": "pending-generation",
                    "answer_quality": None,
                    "generation_result_sha256": None,
                }
            )
    return cells


def _strategy_row(
    *,
    state: Mapping[str, Any],
    catalog: Mapping[str, Any],
    required_evidence_ids: Sequence[str],
    managed_actions: Sequence[Mapping[str, str]],
    requested_token_budget: int,
    latency_ms: float,
    preparation_status: str = "prepared",
    failure: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    pages = _page_map(catalog)
    context_pack, span = materialize_context_records(
        state, catalog, build_time_ms=int(round(latency_ms))
    )
    row: dict[str, Any] = {
        "schema_version": F3_STRATEGY_PREPARATION_SCHEMA,
        "strategy_id": state["strategy_id"],
        "preparation_status": preparation_status,
        "failure": deepcopy(dict(failure)) if failure is not None else None,
        "requested_token_budget": requested_token_budget,
        "working_set": deepcopy(dict(state)),
        "context_pack": context_pack,
        "span": span,
        "metrics": _metrics(state, pages, required_evidence_ids, latency_ms),
        "answer_quality_cells": _answer_quality_cells(
            task_id=str(state["task_id"]),
            strategy_id=str(state["strategy_id"]),
            requested_token_budget=requested_token_budget,
            catalog_sha256=str(catalog["artifact_sha256"]),
            instructions_hash=str(state["instructions_hash"]),
            required_evidence_ids=required_evidence_ids,
            managed_actions=managed_actions,
            working_set=state,
            context_pack=context_pack,
            span=span,
        ),
    }
    row["record_sha256"] = sha256_json(row)
    return row


def prepare_f3_comparison(
    catalog: Mapping[str, Any],
    *,
    task_id: str,
    token_budget: int,
    instructions_hash: str,
    required_evidence_ids: Sequence[str],
    managed_actions: Sequence[Mapping[str, Any]],
    clock_ns: Callable[[], int] = time.perf_counter_ns,
) -> dict[str, Any]:
    """Prepare all four F3 contexts locally; answer generation remains pending."""

    pages = _page_map(catalog)
    task = _text(task_id, "F3 task ID")
    budget = _integer(token_budget, "F3 token budget", minimum=1)
    instructions = _sha(instructions_hash, "F3 instructions hash")
    required = sorted(
        _string_list(list(required_evidence_ids), "required evidence IDs")
    )
    action_plan = _normalize_managed_actions(managed_actions)
    if not required:
        raise F3Error("F3 comparison requires public evidence IDs")
    available_evidence = {
        evidence_id
        for page in pages.values()
        if page["kind"] == "raw"
        for evidence_id in page["evidence_ids"]
    }
    if not set(required).issubset(available_evidence):
        raise F3Error("required evidence is outside the public raw-page catalog")

    raw_pointers = sorted(
        pointer for pointer, page in pages.items() if page["kind"] == "raw"
    )
    if not raw_pointers:
        raise F3Error("F3 comparison requires at least one raw public page")
    full_tokens = _active_tokens(raw_pointers, pages)

    full_overflow = full_tokens > budget
    full_failure = (
        {
            "reason": "full_history_exceeds_token_budget",
            "token_budget": budget,
            "required_tokens": full_tokens,
            "overflow_tokens": full_tokens - budget,
        }
        if full_overflow
        else None
    )
    full_state, full_latency = _measure(
        clock_ns,
        lambda: build_working_set(
            catalog,
            task_id=task,
            token_budget=budget,
            instructions_hash=instructions,
            strategy_id="full_history",
            initial_active_pointers=[] if full_overflow else raw_pointers,
        ),
    )
    dense_state, dense_latency = _measure(
        clock_ns,
        lambda: build_working_set(
            catalog,
            task_id=task,
            token_budget=budget,
            instructions_hash=instructions,
            strategy_id="dense_retrieval",
            initial_active_pointers=_ranked_selection(
                pages, kind="raw", rank_field="dense_rank", token_budget=budget
            ),
        ),
    )
    episodic_state, episodic_latency = _measure(
        clock_ns,
        lambda: build_working_set(
            catalog,
            task_id=task,
            token_budget=budget,
            instructions_hash=instructions,
            strategy_id="episodic_memory",
            initial_active_pointers=_ranked_selection(
                pages,
                kind="episode",
                rank_field="episode_rank",
                token_budget=budget,
            ),
        ),
    )
    managed_state, managed_latency = _measure(
        clock_ns,
        lambda: apply_context_action_plan(
            build_working_set(
                catalog,
                task_id=task,
                token_budget=budget,
                instructions_hash=instructions,
                strategy_id="managed_working_set",
            ),
            catalog,
            action_plan,
        ),
    )
    state_rows = [
        (
            full_state,
            full_latency,
            "failed-overflow" if full_overflow else "prepared",
            full_failure,
        ),
        (dense_state, dense_latency, "prepared", None),
        (episodic_state, episodic_latency, "prepared", None),
        (managed_state, managed_latency, "prepared", None),
    ]
    comparison: dict[str, Any] = {
        "schema_version": F3_PREPARATION_SCHEMA,
        "task_id": task,
        "corpus_snapshot_id": catalog["corpus_snapshot_id"],
        "catalog_sha256": catalog["artifact_sha256"],
        "requested_token_budget": budget,
        "instructions_hash": instructions,
        "required_evidence_ids": required,
        "managed_actions": deepcopy(action_plan),
        "execution_controls": _f3_execution_controls(),
        "strategies": [
            _strategy_row(
                state=state,
                catalog=catalog,
                required_evidence_ids=required,
                managed_actions=action_plan,
                requested_token_budget=budget,
                latency_ms=latency,
                preparation_status=preparation_status,
                failure=failure,
            )
            for state, latency, preparation_status, failure in state_rows
        ],
    }
    comparison["artifact_sha256"] = sha256_json(comparison)
    validate_f3_comparison(comparison, catalog)
    return comparison


def _nonnegative_number(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise F3Error(f"{label} must be a finite non-negative number")
    return float(value)


def validate_f3_comparison(
    value: Mapping[str, Any], catalog: Mapping[str, Any]
) -> None:
    """Validate every F3 preparation by schema, hashes, and semantic replay."""

    pages = _page_map(catalog)
    if not isinstance(value, Mapping) or set(value) != _PREPARATION_FIELDS:
        raise F3Error("F3 preparation fields changed")
    if value.get("schema_version") != F3_PREPARATION_SCHEMA:
        raise F3Error("unsupported F3 preparation schema")
    task_id = _text(value.get("task_id"), "F3 task ID")
    if value.get("corpus_snapshot_id") != catalog["corpus_snapshot_id"]:
        raise F3Error("F3 preparation corpus snapshot changed")
    if value.get("catalog_sha256") != catalog["artifact_sha256"]:
        raise F3Error("F3 preparation catalog binding changed")
    budget = _integer(value.get("requested_token_budget"), "F3 token budget", minimum=1)
    instructions_hash = _sha(value.get("instructions_hash"), "F3 instructions hash")
    required = _string_list(value.get("required_evidence_ids"), "required evidence IDs")
    if not required or required != sorted(required):
        raise F3Error("required evidence IDs are not canonical")
    action_plan = _normalize_managed_actions(value.get("managed_actions"))
    if value.get("managed_actions") != action_plan:
        raise F3Error("managed action plan is not canonical")
    if value.get("execution_controls") != _f3_execution_controls():
        raise F3Error("F3 frontier repeat controls changed")
    available_evidence = {
        evidence_id
        for page in pages.values()
        if page["kind"] == "raw"
        for evidence_id in page["evidence_ids"]
    }
    if not set(required).issubset(available_evidence):
        raise F3Error("required evidence is outside the public raw-page catalog")
    if value.get("artifact_sha256") != sha256_json(
        _without_hash(value, "artifact_sha256")
    ):
        raise F3Error("F3 preparation hash mismatch")

    strategies = value.get("strategies")
    if not isinstance(strategies, list) or len(strategies) != len(_STRATEGIES):
        raise F3Error("F3 preparation must contain exactly four strategies")
    identifiers = [
        row.get("strategy_id") if isinstance(row, Mapping) else None
        for row in strategies
    ]
    if identifiers != list(_STRATEGIES):
        raise F3Error("F3 strategy order changed")

    raw_pointers = sorted(
        pointer for pointer, page in pages.items() if page["kind"] == "raw"
    )
    full_tokens = _active_tokens(raw_pointers, pages)
    full_overflow = full_tokens > budget
    expected_initial = {
        "full_history": [] if full_overflow else raw_pointers,
        "dense_retrieval": _ranked_selection(
            pages, kind="raw", rank_field="dense_rank", token_budget=budget
        ),
        "episodic_memory": _ranked_selection(
            pages,
            kind="episode",
            rank_field="episode_rank",
            token_budget=budget,
        ),
        "managed_working_set": [],
    }
    for index, raw in enumerate(strategies):
        if not isinstance(raw, Mapping) or set(raw) != _STRATEGY_FIELDS:
            raise F3Error(f"F3 strategy {index} fields changed")
        strategy_id = str(raw["strategy_id"])
        if raw.get("schema_version") != F3_STRATEGY_PREPARATION_SCHEMA:
            raise F3Error(f"{strategy_id}: strategy schema changed")
        if raw.get("requested_token_budget") != budget:
            raise F3Error(f"{strategy_id}: requested token budget changed")
        if raw.get("record_sha256") != sha256_json(_without_hash(raw, "record_sha256")):
            raise F3Error(f"{strategy_id}: strategy record hash mismatch")

        state = raw.get("working_set")
        if not isinstance(state, Mapping):
            raise F3Error(f"{strategy_id}: working set must be an object")
        validate_working_set(state, catalog)
        if (
            state.get("task_id") != task_id
            or state.get("strategy_id") != strategy_id
            or state.get("instructions_hash") != instructions_hash
            or state.get("initial_active_pointers") != expected_initial[strategy_id]
        ):
            raise F3Error(f"{strategy_id}: deterministic preparation changed")
        if state.get("token_budget") != budget:
            raise F3Error(f"{strategy_id}: working-set budget changed")
        if strategy_id != "managed_working_set" and (
            state.get("active_pointers") != expected_initial[strategy_id]
            or state.get("context_actions") != []
        ):
            raise F3Error(f"{strategy_id}: baseline preparation changed")
        if (
            strategy_id == "managed_working_set"
            and [
                {"operation": action["operation"], "pointer": action["pointer"]}
                for action in state["context_actions"]
            ]
            != action_plan
        ):
            raise F3Error("managed working-set actions differ from the approved plan")
        expected_status = (
            "failed-overflow"
            if strategy_id == "full_history" and full_overflow
            else "prepared"
        )
        expected_failure = (
            {
                "reason": "full_history_exceeds_token_budget",
                "token_budget": budget,
                "required_tokens": full_tokens,
                "overflow_tokens": full_tokens - budget,
            }
            if expected_status == "failed-overflow"
            else None
        )
        if (
            raw.get("preparation_status") != expected_status
            or raw.get("failure") != expected_failure
        ):
            raise F3Error(f"{strategy_id}: preparation outcome changed")

        metrics = raw.get("metrics")
        if not isinstance(metrics, Mapping) or set(metrics) != _METRIC_FIELDS:
            raise F3Error(f"{strategy_id}: metrics fields changed")
        latency = _nonnegative_number(
            metrics.get("preparation_latency_ms"),
            f"{strategy_id} preparation latency",
        )
        expected_metrics = _metrics(state, pages, required, latency)
        if dict(metrics) != expected_metrics:
            raise F3Error(f"{strategy_id}: metrics differ from replay")

        expected_context, expected_span = materialize_context_records(
            state, catalog, build_time_ms=int(round(latency))
        )
        if raw.get("context_pack") != expected_context:
            raise F3Error(f"{strategy_id}: ContextPack differs from replay")
        if raw.get("span") != expected_span:
            raise F3Error(f"{strategy_id}: Span differs from replay")
        expected_cells = _answer_quality_cells(
            task_id=task_id,
            strategy_id=strategy_id,
            requested_token_budget=budget,
            catalog_sha256=str(catalog["artifact_sha256"]),
            instructions_hash=instructions_hash,
            required_evidence_ids=required,
            managed_actions=action_plan,
            working_set=state,
            context_pack=expected_context,
            span=expected_span,
        )
        if raw.get("answer_quality_cells") != expected_cells:
            raise F3Error(f"{strategy_id}: answer-quality cells changed")


def _approved_f3_gate(root: Path) -> Mapping[str, Any]:
    try:
        gate = load_approved_frontier_entry_gate(root)
        require_frontier_experiment_approved(gate, "F3")
    except (FrontierError, OSError, json.JSONDecodeError) as exc:
        raise F3Error("approved F3 frontier entry is required") from exc
    return gate


def _require_approved_source(
    root: Path,
    gate: Mapping[str, Any],
    commitment: Mapping[str, Any],
) -> dict[str, str]:
    try:
        return require_approved_f3_source_commitment(root, gate, commitment)
    except (FrontierError, OSError, json.JSONDecodeError) as exc:
        raise F3Error(f"approved F3 source commitment is invalid: {exc}") from exc


def run_f3_experiment(
    root: Path | None = None,
    *,
    page_specs: Sequence[Mapping[str, Any]],
    source_manifest: Mapping[str, Any],
    approved_source_commitment: Mapping[str, Any],
    corpus_snapshot_id: str,
    task_id: str,
    token_budget: int,
    instructions_hash: str,
    required_evidence_ids: Sequence[str],
    managed_actions: Sequence[Mapping[str, Any]],
    clock_ns: Callable[[], int] = time.perf_counter_ns,
) -> dict[str, Any]:
    """Prepare F3 only after the canonical Kevin-approved frontier entry."""

    repository = (root or repository_root()).resolve()
    gate = _approved_f3_gate(repository)
    source_commitment = _require_approved_source(
        repository, gate, approved_source_commitment
    )
    catalog = build_approved_page_catalog(
        page_specs,
        corpus_snapshot_id=corpus_snapshot_id,
        source_manifest=source_manifest,
        approved_source_commitment=source_commitment,
        root=repository,
    )
    preparation = prepare_f3_comparison(
        catalog,
        task_id=task_id,
        token_budget=token_budget,
        instructions_hash=instructions_hash,
        required_evidence_ids=required_evidence_ids,
        managed_actions=managed_actions,
        clock_ns=clock_ns,
    )
    statuses = Counter(
        cell["status"]
        for strategy in preparation["strategies"]
        for cell in strategy["answer_quality_cells"]
    )
    result: dict[str, Any] = {
        "schema_version": F3_EXPERIMENT_SCHEMA,
        "experiment_id": "F3",
        "frontier_entry_gate_sha256": gate["artifact_sha256"],
        "approved_source_commitment": source_commitment,
        "source_manifest_artifact_sha256": source_manifest["artifact_sha256"],
        "catalog": catalog,
        "preparation": preparation,
        "status": "prepared-pending-generation",
        "answer_quality_status_counts": dict(sorted(statuses.items())),
    }
    result["artifact_sha256"] = sha256_json(result)
    validate_f3_experiment(result)
    return result


def validate_f3_experiment(value: Mapping[str, Any]) -> None:
    """Validate the complete public F3 result and all nested commitments."""

    if not isinstance(value, Mapping) or set(value) != _EXPERIMENT_FIELDS:
        raise F3Error("F3 experiment fields changed")
    if value.get("schema_version") != F3_EXPERIMENT_SCHEMA:
        raise F3Error("unsupported F3 experiment schema")
    if value.get("experiment_id") != "F3":
        raise F3Error("F3 experiment identity changed")
    _sha(value.get("frontier_entry_gate_sha256"), "frontier entry gate hash")
    _approved_source_commitment(value.get("approved_source_commitment"))
    _sha(
        value.get("source_manifest_artifact_sha256"),
        "F3 source manifest artifact hash",
    )
    if value.get("status") != "prepared-pending-generation":
        raise F3Error("F3 experiment status changed")
    catalog = value.get("catalog")
    if not isinstance(catalog, Mapping):
        raise F3Error("F3 experiment catalog must be an object")
    validate_page_catalog(catalog)
    preparation = value.get("preparation")
    if not isinstance(preparation, Mapping):
        raise F3Error("F3 experiment preparation must be an object")
    validate_f3_comparison(preparation, catalog)
    statuses = Counter(
        cell["status"]
        for strategy in preparation["strategies"]
        for cell in strategy["answer_quality_cells"]
    )
    if value.get("answer_quality_status_counts") != dict(sorted(statuses.items())):
        raise F3Error("F3 answer-quality status counts changed")
    expected_cell_count = (
        len(_STRATEGIES) * len(ALLOWED_REASONING_EFFORTS) * len(F3_TRIAL_IDS)
    )
    if statuses != {"pending-generation": expected_cell_count}:
        raise F3Error("F3 answer-quality cells must remain pending generation")
    if value.get("artifact_sha256") != sha256_json(
        _without_hash(value, "artifact_sha256")
    ):
        raise F3Error("F3 experiment hash mismatch")


def _decimal(value: Any, label: str) -> Decimal:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise F3Error(f"{label} must be a decimal string")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise F3Error(f"{label} must be a decimal string") from exc
    if not parsed.is_finite() or parsed < 0:
        raise F3Error(f"{label} must be finite and non-negative")
    return parsed


def _score(value: Any, label: str) -> float:
    score = _nonnegative_number(value, label)
    if score > 1:
        raise F3Error(f"{label} must be within 0..1")
    return score


def _public_artifact(
    repository: Path,
    reference: Any,
    *,
    allowed_root: Path,
    label: str,
) -> tuple[Mapping[str, Any], str]:
    if (
        not isinstance(reference, Mapping)
        or set(reference) != _ARTIFACT_REFERENCE_FIELDS
    ):
        raise F3Error(f"{label} must be a public artifact path and SHA")
    path_value = reference.get("path")
    if not isinstance(path_value, str) or not path_value.strip():
        raise F3Error(f"{label} path must be non-empty text")
    relative = Path(path_value)
    lowered = path_value.casefold()
    try:
        relative.relative_to(allowed_root)
    except ValueError as exc:
        raise F3Error(f"{label} must stay in {allowed_root.as_posix()}") from exc
    if (
        relative.is_absolute()
        or len(relative.parts) <= len(allowed_root.parts)
        or ".." in relative.parts
        or any(token in lowered for token in _FORBIDDEN_REFERENCE_TOKENS)
    ):
        raise F3Error(f"{label} is not a repository-safe public artifact")
    cursor = repository
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise F3Error(f"{label} is not a repository-safe public artifact")
    expected_sha = _sha(reference.get("sha256"), f"{label} SHA")
    path = repository / relative
    if not path.is_file() or path.is_symlink():
        raise F3Error(f"{label} is missing or unsafe")
    raw = path.read_bytes()
    if len(raw) > MAX_PUBLIC_ARTIFACT_BYTES:
        raise F3Error(f"{label} exceeds the public artifact size bound")
    observed_sha = hashlib.sha256(raw).hexdigest()
    if observed_sha != expected_sha:
        raise F3Error(f"{label} file hash mismatch")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise F3Error(f"{label} must contain valid UTF-8 JSON") from exc
    if not isinstance(value, Mapping):
        raise F3Error(f"{label} JSON must be an object")
    return value, observed_sha


def _validate_public_output_artifact(
    repository: Path,
    reference: Any,
    *,
    receipt: Mapping[str, Any],
    strategy: Mapping[str, Any],
    pending: Mapping[str, Any],
) -> None:
    value, observed_sha = _public_artifact(
        repository,
        reference,
        allowed_root=_F3_EVIDENCE_ROOT,
        label="F3 public output artifact",
    )
    if observed_sha != receipt.get("output_sha256"):
        raise F3Error("F3 public output artifact differs from output_sha256")
    if set(value) != _PUBLIC_OUTPUT_FIELDS:
        raise F3Error("F3 public output artifact fields changed")
    if (
        value.get("schema_version") != F3_PUBLIC_OUTPUT_SCHEMA
        or value.get("run_spec_sha256") != pending.get("run_spec_sha256")
        or value.get("strategy_id") != strategy.get("strategy_id")
        or value.get("reasoning_effort") != pending.get("reasoning_effort")
        or not isinstance(value.get("answer"), str)
        or not value["answer"].strip()
        or value.get("record_sha256")
        != sha256_json(_without_hash(value, "record_sha256"))
    ):
        raise F3Error("F3 public output artifact identity or content is invalid")


def _validate_generator_receipt(
    receipt: Mapping[str, Any],
    pending: Mapping[str, Any],
    strategy: Mapping[str, Any],
    repository: Path,
) -> Decimal:
    if not isinstance(receipt, Mapping):
        raise F3Error("F3 generator receipt must be an object")
    if receipt.get("status") == "completed" and "output_artifact" not in receipt:
        raise F3Error("completed F3 receipt requires a public output artifact")
    if set(receipt) != _GENERATOR_RECEIPT_FIELDS:
        raise F3Error("F3 generator receipt fields changed")
    if receipt.get("schema_version") != F3_GENERATOR_RECEIPT_SCHEMA:
        raise F3Error("unsupported F3 generator receipt schema")
    if receipt.get("receipt_sha256") != sha256_json(
        _without_hash(receipt, "receipt_sha256")
    ):
        raise F3Error("F3 generator receipt hash mismatch")
    if (
        receipt.get("run_spec_sha256") != pending.get("run_spec_sha256")
        or receipt.get("trial_id") != pending.get("trial_id")
        or receipt.get("provider_repeat_sample_id")
        != pending.get("provider_repeat_sample_id")
        or receipt.get("temperature") != F3_TEMPERATURE
        or receipt.get("requested_model") != MODEL_ID
        or receipt.get("requested_model") != pending.get("requested_model")
        or receipt.get("reasoning_effort") != pending.get("reasoning_effort")
    ):
        raise F3Error("F3 generator receipt run identity changed")
    effort = receipt.get("reasoning_effort")
    if effort not in ALLOWED_REASONING_EFFORTS:
        raise F3Error("F3 generator receipt reasoning effort is invalid")

    usage = receipt.get("native_usage")
    if not isinstance(usage, Mapping) or set(usage) != _NATIVE_USAGE_FIELDS:
        raise F3Error("F3 generator receipt native usage fields changed")
    for field in _NATIVE_USAGE_FIELDS:
        _integer(usage.get(field), f"F3 native {field}")

    price = receipt.get("price")
    if not isinstance(price, Mapping) or set(price) != _PRICE_FIELDS:
        raise F3Error("F3 generator receipt price fields changed")
    if price.get("currency") != "USD":
        raise F3Error("F3 generator receipt price currency changed")
    _decimal(price.get("input_usd_per_million"), "F3 input price")
    _decimal(price.get("output_usd_per_million"), "F3 output price")
    _text(price.get("source"), "F3 price source")
    billed = _decimal(receipt.get("billed_cost_usd"), "F3 billed cost")
    _nonnegative_number(receipt.get("latency_ms"), "F3 provider latency")
    _integer(receipt.get("retry_count"), "F3 retry count")
    _sha(receipt.get("output_sha256"), "F3 generator output commitment")

    status = receipt.get("status")
    resolved_model = receipt.get("resolved_model")
    provider = receipt.get("provider")
    request_id = receipt.get("request_id")
    reservation_id = receipt.get("ledger_reservation_id")
    error = receipt.get("error")
    if (
        not isinstance(reservation_id, str)
        or _LEDGER_RESERVATION_ID.fullmatch(reservation_id) is None
    ):
        raise F3Error("F3 generator receipt ledger reservation ID is invalid")
    if status == "completed":
        if (
            resolved_model not in {MODEL_ID, CANONICAL_MODEL_ID}
            or not isinstance(provider, str)
            or provider.casefold() != FRONTIER_PROVIDER_SLUG
            or not isinstance(request_id, str)
            or not request_id.strip()
            or error is not None
        ):
            raise F3Error("completed F3 generator receipt is malformed")
        _validate_public_output_artifact(
            repository,
            receipt.get("output_artifact"),
            receipt=receipt,
            strategy=strategy,
            pending=pending,
        )
    elif status == "failed":
        if resolved_model is not None and resolved_model not in {
            MODEL_ID,
            CANONICAL_MODEL_ID,
        }:
            raise F3Error("failed F3 generator receipt resolved model is invalid")
        if provider is not None and (
            not isinstance(provider, str)
            or provider.casefold() != FRONTIER_PROVIDER_SLUG
        ):
            raise F3Error("failed F3 generator receipt provider is invalid")
        if request_id is not None and (
            not isinstance(request_id, str) or not request_id.strip()
        ):
            raise F3Error("failed F3 generator receipt request ID is invalid")
        if not isinstance(error, str) or not error.strip() or redact(error) != error:
            raise F3Error("failed F3 generator receipt error is missing or unsafe")
        if receipt.get("output_artifact") is not None:
            raise F3Error("failed F3 generator receipt artifact must be null")
    else:
        raise F3Error("F3 generator receipt status must be terminal")
    return billed


def _paid_ledger_rows(root: Path) -> list[dict[str, Any]]:
    path = canonical_ledger_path(root)
    if (
        path.is_symlink()
        or not path.is_file()
        or path.resolve(strict=True).parent
        != (root / "results/v2/cost").resolve(strict=False)
    ):
        raise F3Error("F3 paid ledger evidence is missing or unsafe")
    rows: list[dict[str, Any]] = []
    try:
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise F3Error(f"F3 paid ledger row {line_number} must be an object")
            rows.append(row)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise F3Error("F3 paid ledger evidence cannot be read") from exc
    return rows


def _reservation_events(
    rows: Sequence[Mapping[str, Any]], reservation_id: str
) -> list[Mapping[str, Any]]:
    events = [row for row in rows if row.get("reservation_id") == reservation_id]
    if not events or any(
        row.get("schema_version") != "contextlab.cost-event.v1" for row in events
    ):
        raise F3Error("F3 paid ledger event schema changed")
    return events


def _validate_ledger_reservation(event: Mapping[str, Any]) -> Decimal:
    if set(event) != {
        "schema_version",
        "event",
        "reservation_id",
        "input_token_limit",
        "output_token_limit",
        "call_count",
        "estimated_usd",
    }:
        raise F3Error("F3 paid ledger reservation fields changed")
    if (
        event.get("event") != "reserve"
        or event.get("call_count") != 1
        or isinstance(event.get("input_token_limit"), bool)
        or not isinstance(event.get("input_token_limit"), int)
        or event["input_token_limit"] < 0
        or isinstance(event.get("output_token_limit"), bool)
        or not isinstance(event.get("output_token_limit"), int)
        or event["output_token_limit"] < 0
    ):
        raise F3Error("F3 paid ledger reservation is invalid")
    return _decimal(event.get("estimated_usd"), "F3 reserved cost")


def _validate_successful_ledger_receipt(
    receipt: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
) -> None:
    events = _reservation_events(rows, str(receipt["ledger_reservation_id"]))
    if [row.get("event") for row in events] != ["reserve", "acknowledge", "settle"]:
        raise F3Error("F3 completed call requires reserve, acknowledge, settle only")
    estimated = _validate_ledger_reservation(events[0])
    acknowledgment = events[1].get("metadata")
    settlement = events[2]
    metadata = settlement.get("metadata")
    if not isinstance(acknowledgment, Mapping) or not isinstance(metadata, Mapping):
        raise F3Error("F3 paid ledger provider metadata is missing")
    usage = receipt["native_usage"]
    price = receipt["price"]
    billed = _decimal(receipt["billed_cost_usd"], "F3 billed cost")
    if billed > estimated:
        raise F3Error("F3 billed cost exceeds its reservation")
    if (
        acknowledgment.get("request_id") != receipt["request_id"]
        or metadata.get("request_id") != receipt["request_id"]
        or str(acknowledgment.get("provider", "")).casefold()
        != FRONTIER_PROVIDER_SLUG
        or str(metadata.get("provider", "")).casefold() != FRONTIER_PROVIDER_SLUG
        or acknowledgment.get("resolved_model") != receipt["resolved_model"]
        or metadata.get("requested_model") != MODEL_ID
        or metadata.get("resolved_model") != receipt["resolved_model"]
        or acknowledgment.get("prompt_tokens") != usage["prompt_tokens"]
        or acknowledgment.get("completion_tokens") != usage["completion_tokens"]
        or metadata.get("native_prompt_tokens") != usage["prompt_tokens"]
        or metadata.get("native_completion_tokens") != usage["completion_tokens"]
        or metadata.get("native_reasoning_tokens") != usage["reasoning_tokens"]
        or _decimal(settlement.get("actual_usd"), "F3 settled cost") != billed
        or _decimal(metadata.get("actual_usd"), "F3 settlement cost") != billed
        or metadata.get("cost_source") != price["source"]
        or metadata.get("latency_ms") != receipt["latency_ms"]
        or metadata.get("retry_count") != receipt["retry_count"]
        or metadata.get("error") is not None
    ):
        raise F3Error("F3 paid ledger differs from the imported provider receipt")


def _validate_failed_ledger_receipt(
    receipt: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
) -> None:
    events = _reservation_events(rows, str(receipt["ledger_reservation_id"]))
    event_names = [row.get("event") for row in events]
    estimated = _validate_ledger_reservation(events[0])
    if event_names not in (
        ["reserve", "cancel"],
        ["reserve", "failure", "cancel"],
        ["reserve", "acknowledge", "failure", "cancel"],
        ["reserve", "acknowledge", "failure", "settle"],
        ["reserve", "acknowledge", "settle", "failure"],
        ["reserve", "acknowledge", "settle"],
    ):
        raise F3Error(
            "F3 failed call requires a terminal cancel or settle ledger lifecycle"
        )
    billed = _decimal(receipt["billed_cost_usd"], "F3 failed billed cost")
    if billed > estimated:
        raise F3Error("F3 failed-call cost exceeds its reservation")
    request_id = receipt.get("request_id")
    error = receipt["error"]
    has_acknowledgment = "acknowledge" in event_names
    if has_acknowledgment:
        acknowledgment = events[1].get("metadata")
        usage = receipt["native_usage"]
        if (
            request_id is None
            or not isinstance(acknowledgment, Mapping)
            or acknowledgment.get("request_id") != request_id
            or str(acknowledgment.get("provider", "")).casefold()
            != FRONTIER_PROVIDER_SLUG
            or acknowledgment.get("resolved_model") != receipt.get("resolved_model")
            or acknowledgment.get("prompt_tokens") != usage["prompt_tokens"]
            or acknowledgment.get("completion_tokens") != usage["completion_tokens"]
        ):
            raise F3Error("F3 paid ledger failed-call request binding changed")
    elif request_id is not None:
        raise F3Error("F3 failed call with a request ID requires acknowledgment")

    failures = [row for row in events if row.get("event") == "failure"]
    if failures:
        failure = failures[0]
        metadata = failure.get("metadata")
        if (
            failure.get("reason") != error
            or not isinstance(metadata, Mapping)
            or metadata.get("error") != error
            or metadata.get("request_id") != request_id
            or (
                receipt.get("provider") is not None
                and str(metadata.get("provider", "")).casefold()
                != FRONTIER_PROVIDER_SLUG
            )
            or metadata.get("resolved_model") != receipt.get("resolved_model")
        ):
            raise F3Error("F3 failed ledger receipt is not authoritative")

    terminal = next(row for row in events if row.get("event") in {"cancel", "settle"})
    if terminal.get("event") == "cancel":
        if (
            billed != Decimal("0")
            or not isinstance(terminal.get("reason"), str)
            or not terminal["reason"]
            or (not failures and terminal["reason"] != error)
        ):
            raise F3Error("F3 cancelled failed call must have zero billed cost")
        return

    if not has_acknowledgment or request_id is None:
        raise F3Error("F3 failed-call settlement requires provider acknowledgment")
    settlement_metadata = terminal.get("metadata")
    usage = receipt["native_usage"]
    price = receipt["price"]
    if not isinstance(settlement_metadata, Mapping) or (
        _decimal(terminal.get("actual_usd"), "F3 failed settled cost") != billed
        or _decimal(settlement_metadata.get("actual_usd"), "F3 failed settlement cost")
        != billed
        or settlement_metadata.get("request_id") != request_id
        or settlement_metadata.get("requested_model") != MODEL_ID
        or settlement_metadata.get("resolved_model") != receipt.get("resolved_model")
        or str(settlement_metadata.get("provider", "")).casefold()
        != FRONTIER_PROVIDER_SLUG
        or settlement_metadata.get("native_prompt_tokens") != usage["prompt_tokens"]
        or settlement_metadata.get("native_completion_tokens")
        != usage["completion_tokens"]
        or settlement_metadata.get("native_reasoning_tokens")
        != usage["reasoning_tokens"]
        or settlement_metadata.get("cost_source") != price["source"]
        or settlement_metadata.get("latency_ms") != receipt["latency_ms"]
        or settlement_metadata.get("retry_count") != receipt["retry_count"]
        or settlement_metadata.get("error") not in {None, error}
    ):
        raise F3Error(
            "F3 failed-call settlement differs from the imported provider receipt"
        )


def _validate_f3_paid_ledger(
    root: Path, cells: Sequence[Mapping[str, Any]]
) -> list[str]:
    """Replay every terminal F3 receipt against its canonical paid lifecycle."""

    rows = _paid_ledger_rows(root)
    receipts = [cell["generator_receipt"] for cell in cells]
    reservation_ids = [str(receipt["ledger_reservation_id"]) for receipt in receipts]
    if len(set(reservation_ids)) != len(reservation_ids):
        raise F3Error("F3 paid ledger reservation IDs must be unique")
    request_ids = [
        str(receipt["request_id"])
        for receipt in receipts
        if receipt.get("request_id") is not None
    ]
    if len(set(request_ids)) != len(request_ids):
        raise F3Error("F3 generator request IDs must be unique")
    for receipt in receipts:
        if receipt["status"] == "completed":
            _validate_successful_ledger_receipt(receipt, rows)
        else:
            _validate_failed_ledger_receipt(receipt, rows)
    return reservation_ids


def _validate_committed_score(
    value: Any,
    *,
    kind: str,
    receipt_status: str,
    receipt: Mapping[str, Any],
    strategy: Mapping[str, Any],
    pending: Mapping[str, Any],
    repository: Path,
) -> None:
    metric_field = "score" if kind == "answer quality" else "recovered_evidence_recall"
    metric_name = (
        "answer_quality" if kind == "answer quality" else "recovered_evidence_recall"
    )
    expected_fields = {
        "status",
        metric_field,
        "artifact_path",
        "artifact_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise F3Error(f"F3 {kind} result fields changed")
    if receipt_status == "completed":
        if value.get("status") != "scored":
            raise F3Error(f"completed F3 {kind} result is not scored")
        score = _score(value.get(metric_field), f"F3 {kind} result")
        artifact, _ = _public_artifact(
            repository,
            {
                "path": value.get("artifact_path"),
                "sha256": value.get("artifact_sha256"),
            },
            allowed_root=_F3_REVIEWS_ROOT,
            label=f"F3 public {kind} artifact",
        )
        if set(artifact) != _PUBLIC_METRIC_FIELDS:
            raise F3Error(f"F3 public {kind} artifact fields changed")
        artifact_value = _score(
            artifact.get("value"), f"F3 public {kind} artifact value"
        )
        if (
            artifact.get("schema_version") != F3_PUBLIC_METRIC_SCHEMA
            or artifact.get("run_spec_sha256") != pending.get("run_spec_sha256")
            or artifact.get("strategy_id") != strategy.get("strategy_id")
            or artifact.get("reasoning_effort") != pending.get("reasoning_effort")
            or artifact.get("metric") != metric_name
            or artifact_value != score
            or artifact.get("output_sha256") != receipt.get("output_sha256")
            or artifact.get("record_sha256")
            != sha256_json(_without_hash(artifact, "record_sha256"))
        ):
            raise F3Error(f"F3 public {kind} artifact binding changed")
    elif (
        value.get("status") != "unavailable"
        or value.get(metric_field) is not None
        or value.get("artifact_path") is not None
        or value.get("artifact_sha256") is not None
    ):
        raise F3Error(f"failed F3 {kind} result must use null artifact refs")


def _validate_terminal_cell(
    cell: Mapping[str, Any],
    strategy: Mapping[str, Any],
    pending: Mapping[str, Any],
    repository: Path,
) -> Decimal:
    if not isinstance(cell, Mapping) or set(cell) != _TERMINAL_CELL_FIELDS:
        raise F3Error("F3 terminal answer-quality cell fields changed")
    if cell.get("schema_version") != F3_ANSWER_QUALITY_RESULT_SCHEMA:
        raise F3Error("unsupported F3 terminal answer-quality schema")
    if cell.get("cell_result_sha256") != sha256_json(
        _without_hash(cell, "cell_result_sha256")
    ):
        raise F3Error("F3 terminal answer-quality cell hash mismatch")
    if (
        cell.get("strategy_id") != strategy.get("strategy_id")
        or cell.get("trial_id") != pending.get("trial_id")
        or cell.get("provider_repeat_sample_id")
        != pending.get("provider_repeat_sample_id")
        or cell.get("reasoning_effort") != pending.get("reasoning_effort")
        or cell.get("run_spec_sha256") != pending.get("run_spec_sha256")
    ):
        raise F3Error("F3 terminal answer-quality cell identity changed")
    receipt = cell.get("generator_receipt")
    if not isinstance(receipt, Mapping):
        raise F3Error("F3 terminal cell has no generator receipt")
    billed = _validate_generator_receipt(receipt, pending, strategy, repository)
    if (
        strategy.get("preparation_status") == "failed-overflow"
        and receipt.get("status") != "failed"
    ):
        raise F3Error("overflowed full history cannot report completed generation")
    _validate_committed_score(
        cell.get("answer_quality_result"),
        kind="answer quality",
        receipt_status=str(receipt["status"]),
        receipt=receipt,
        strategy=strategy,
        pending=pending,
        repository=repository,
    )
    _validate_committed_score(
        cell.get("evidence_result"),
        kind="evidence",
        receipt_status=str(receipt["status"]),
        receipt=receipt,
        strategy=strategy,
        pending=pending,
        repository=repository,
    )
    return billed


def _expected_quality_cells(
    prepared_result: Mapping[str, Any],
) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    preparation = prepared_result["preparation"]
    return [
        (strategy, pending)
        for strategy in preparation["strategies"]
        for pending in strategy["answer_quality_cells"]
    ]


def _build_f3_final_result(
    prepared_result: Mapping[str, Any],
    cell_results: Sequence[Mapping[str, Any]],
    repository: Path,
) -> dict[str, Any]:
    validate_f3_experiment(prepared_result)
    if isinstance(cell_results, (str, bytes)) or not isinstance(cell_results, Sequence):
        raise F3Error("F3 finalization cells must be a sequence")
    expected_count = (
        len(_STRATEGIES) * len(ALLOWED_REASONING_EFFORTS) * len(F3_TRIAL_IDS)
    )
    if len(cell_results) != expected_count:
        raise F3Error(
            f"F3 finalization requires exactly {expected_count} terminal cells"
        )
    supplied: dict[tuple[str, str, str, str], Mapping[str, Any]] = {}
    for index, cell in enumerate(cell_results):
        if not isinstance(cell, Mapping):
            raise F3Error(f"F3 finalization cell {index} must be an object")
        key = (
            str(cell.get("strategy_id")),
            str(cell.get("trial_id")),
            str(cell.get("provider_repeat_sample_id")),
            str(cell.get("reasoning_effort")),
        )
        if key in supplied:
            raise F3Error("F3 finalization contains duplicate cells")
        supplied[key] = cell

    ordered: list[dict[str, Any]] = []
    billed_costs: list[Decimal] = []
    request_ids: set[str] = set()
    for strategy, pending in _expected_quality_cells(prepared_result):
        key = (
            str(strategy["strategy_id"]),
            str(pending["trial_id"]),
            str(pending["provider_repeat_sample_id"]),
            str(pending["reasoning_effort"]),
        )
        cell = supplied.pop(key, None)
        if cell is None:
            raise F3Error("F3 finalization is missing a required low/high cell")
        billed_costs.append(
            _validate_terminal_cell(cell, strategy, pending, repository)
        )
        receipt = cell["generator_receipt"]
        request_id = receipt.get("request_id")
        if isinstance(request_id, str):
            if request_id in request_ids:
                raise F3Error("F3 generator request IDs must be unique")
            request_ids.add(request_id)
        ordered.append(deepcopy(dict(cell)))
    if supplied:
        raise F3Error("F3 finalization contains an unknown cell")

    reservation_ids = _validate_f3_paid_ledger(repository, ordered)

    statuses = Counter(cell["generator_receipt"]["status"] for cell in ordered)
    final: dict[str, Any] = {
        "schema_version": F3_FINAL_SCHEMA,
        "experiment_id": "F3",
        "frontier_entry_gate_sha256": prepared_result["frontier_entry_gate_sha256"],
        "prepared_experiment_sha256": prepared_result["artifact_sha256"],
        "task_id": prepared_result["preparation"]["task_id"],
        "catalog_sha256": prepared_result["catalog"]["artifact_sha256"],
        "status": "generated-pending-result-review",
        "cells": ordered,
        "trial_ids": list(F3_TRIAL_IDS),
        "provider_repeat_sample_ids": list(F3_PROVIDER_REPEAT_SAMPLE_IDS),
        "ledger_reservation_ids": reservation_ids,
        "generator_status_counts": dict(sorted(statuses.items())),
        "total_billed_cost_usd": str(sum(billed_costs, Decimal("0"))),
    }
    final["artifact_sha256"] = sha256_json(final)
    validate_f3_final_result(final, prepared_result, root=repository)
    return final


def validate_f3_final_result(
    value: Mapping[str, Any],
    prepared_result: Mapping[str, Any],
    *,
    root: Path | None = None,
) -> None:
    """Reject incomplete, malformed, unbound, or rehashed F3 final results."""

    repository = (root or repository_root()).resolve()
    validate_f3_experiment(prepared_result)
    if not isinstance(value, Mapping) or set(value) != _FINAL_FIELDS:
        raise F3Error("F3 final result fields changed")
    if value.get("schema_version") != F3_FINAL_SCHEMA:
        raise F3Error("unsupported F3 final result schema")
    if (
        value.get("experiment_id") != "F3"
        or value.get("status") != "generated-pending-result-review"
    ):
        raise F3Error("F3 final result status or identity changed")
    if (
        value.get("frontier_entry_gate_sha256")
        != prepared_result["frontier_entry_gate_sha256"]
        or value.get("prepared_experiment_sha256") != prepared_result["artifact_sha256"]
        or value.get("task_id") != prepared_result["preparation"]["task_id"]
        or value.get("catalog_sha256") != prepared_result["catalog"]["artifact_sha256"]
    ):
        raise F3Error("F3 final result preparation binding changed")
    if value.get("artifact_sha256") != sha256_json(
        _without_hash(value, "artifact_sha256")
    ):
        raise F3Error("F3 final result hash mismatch")
    cells = value.get("cells")
    expected_count = (
        len(_STRATEGIES) * len(ALLOWED_REASONING_EFFORTS) * len(F3_TRIAL_IDS)
    )
    if not isinstance(cells, list) or len(cells) != expected_count:
        raise F3Error(
            f"F3 final result requires exactly {expected_count} terminal cells"
        )
    if value.get("trial_ids") != list(F3_TRIAL_IDS) or value.get(
        "provider_repeat_sample_ids"
    ) != list(F3_PROVIDER_REPEAT_SAMPLE_IDS):
        raise F3Error("F3 final repeat identities changed")
    total = Decimal("0")
    statuses: Counter[str] = Counter()
    for index, (strategy, pending) in enumerate(
        _expected_quality_cells(prepared_result)
    ):
        cell = cells[index]
        if not isinstance(cell, Mapping):
            raise F3Error(f"F3 final result cell {index} must be an object")
        total += _validate_terminal_cell(cell, strategy, pending, repository)
        receipt = cell["generator_receipt"]
        statuses[str(receipt["status"])] += 1
    reservation_ids = _validate_f3_paid_ledger(repository, cells)
    if value.get("ledger_reservation_ids") != reservation_ids:
        raise F3Error("F3 final ledger reservation binding changed")
    if value.get("generator_status_counts") != dict(sorted(statuses.items())):
        raise F3Error("F3 final generator status counts changed")
    if value.get("total_billed_cost_usd") != str(total):
        raise F3Error("F3 final billed cost changed")


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _safe_result_path(repository: Path, relative_path: Path) -> Path:
    path = repository / relative_path
    relative = relative_path.parent
    cursor = repository
    for part in relative.parts:
        cursor = cursor / part
        if (cursor.exists() or cursor.is_symlink()) and (
            cursor.is_symlink() or not cursor.is_dir()
        ):
            raise F3Error("F3 result path is not a safe repository directory")
    return path


def _write_immutable_result(
    repository: Path, path: Path, value: Mapping[str, Any], *, label: str
) -> dict[str, Any]:
    data = _json_bytes(value)
    try:
        write_bytes_once_or_verify(repository, path, data)
    except ImmutableIOError as exc:
        raise F3Error(f"immutable {label} differs or is unsafe: {path}") from exc
    return deepcopy(dict(value))


def write_f3_experiment(
    root: Path | None = None, *, result: Mapping[str, Any]
) -> dict[str, Any]:
    """Persist one approved public F3 result once, or accept identical bytes."""

    repository = (root or repository_root()).resolve()
    gate = _approved_f3_gate(repository)
    validate_f3_experiment(result)
    source_commitment = _require_approved_source(
        repository, gate, result["approved_source_commitment"]
    )
    if result.get("frontier_entry_gate_sha256") != gate["artifact_sha256"]:
        raise F3Error("F3 result is bound to a different frontier entry gate")
    if result.get("approved_source_commitment") != source_commitment:
        raise F3Error("F3 result source commitment is not current")
    _replay_catalog_from_committed_sources(
        repository,
        commitment=source_commitment,
        source_manifest_artifact_sha256=result["source_manifest_artifact_sha256"],
        catalog=result["catalog"],
    )
    path = _safe_result_path(repository, F3_RESULT_PATH)
    return _write_immutable_result(repository, path, result, label="F3 result")


def _f3_task_text(prepared_result: Mapping[str, Any]) -> str:
    instructions_hash = str(prepared_result["preparation"]["instructions_hash"])
    matches = [
        page
        for page in prepared_result["catalog"]["pages"]
        if page.get("content_sha256") == instructions_hash
    ]
    if len(matches) != 1:
        raise F3Error("F3 execution requires one hash-bound public task prompt")
    return _text(matches[0].get("content"), "F3 public task prompt")


def _f3_generation_spec(
    prepared_result: Mapping[str, Any],
    strategy: Mapping[str, Any],
    pending: Mapping[str, Any],
    *,
    task_text: str,
) -> dict[str, Any]:
    pages = _page_map(prepared_result["catalog"])
    state = strategy["working_set"]
    run_id = (
        f"{F3_EXECUTION_ATTEMPT}-{strategy['strategy_id']}-{pending['trial_id']}-"
        f"{pending['reasoning_effort']}"
    )
    task = {
        "schema_version": "contextlab.prompt-task.v1",
        "task_id": prepared_result["preparation"]["task_id"],
        "suite": "static",
        "question_text": task_text,
        "question_sha256": hashlib.sha256(task_text.encode("utf-8")).hexdigest(),
    }
    return {
        "schema_version": "contextlab.generation-spec.v1",
        "run_id": run_id,
        "task": task,
        "system_instruction": F3_SYSTEM_INSTRUCTION,
        "rendered_context": _render_active(state["active_pointers"], pages),
        "reasoning_effort": pending["reasoning_effort"],
        "max_tokens": F3_MAX_COMPLETION_TOKENS,
        "temperature": F3_TEMPERATURE,
    }


def _f3_provider_path(
    repository: Path,
    strategy: Mapping[str, Any],
    pending: Mapping[str, Any],
) -> Path:
    return repository / _F3_PROVIDER_ROOT / (
        f"{strategy['strategy_id']}-{pending['trial_id']}-"
        f"{pending['reasoning_effort']}.json"
    )


def _load_saved_f3_generation(
    path: Path, *, spec: Mapping[str, Any]
) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise F3Error("saved F3 provider output is unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise F3Error("saved F3 provider output is invalid") from exc
    if not isinstance(value, dict):
        raise F3Error("saved F3 provider output must be an object")
    if value.get("schema_version") == "contextlab.failed-generation-result.v2":
        if (
            value.get("run_id") != spec["run_id"]
            or value.get("task_id") != spec["task"]["task_id"]
            or value.get("error") != "provider_finished_without_text"
            or not isinstance(value.get("metadata"), Mapping)
            or value["metadata"].get("reasoning_effort") != spec["reasoning_effort"]
        ):
            raise F3Error("saved failed F3 provider output identity changed")
        return value
    if value.get("schema_version") != "contextlab.generation-result.v1":
        raise F3Error("saved F3 provider output is not a terminal generation")
    try:
        validate_saved_generation_result(
            value,
            expected_run_id=str(spec["run_id"]),
            expected_task_id=str(spec["task"]["task_id"]),
            expected_effort=str(spec["reasoning_effort"]),
        )
    except ValueError as exc:
        raise F3Error("saved F3 provider output identity changed") from exc
    return value


def _write_f3_public_json(
    repository: Path, relative: Path, value: Mapping[str, Any]
) -> dict[str, str]:
    path = _safe_result_path(repository, relative)
    raw = _json_bytes(value)
    try:
        write_bytes_once_or_verify(repository, path, raw)
    except ImmutableIOError as exc:
        raise F3Error(f"immutable F3 public artifact differs or is unsafe: {relative}") from exc
    return {"path": relative.as_posix(), "sha256": hashlib.sha256(raw).hexdigest()}


def _f3_output_reference(
    repository: Path,
    *,
    strategy: Mapping[str, Any],
    pending: Mapping[str, Any],
    answer: str,
) -> dict[str, str]:
    record: dict[str, Any] = {
        "schema_version": F3_PUBLIC_OUTPUT_SCHEMA,
        "run_spec_sha256": pending["run_spec_sha256"],
        "strategy_id": strategy["strategy_id"],
        "reasoning_effort": pending["reasoning_effort"],
        "answer": answer,
    }
    record["record_sha256"] = sha256_json(record)
    relative = _F3_EVIDENCE_ROOT / (
        f"{strategy['strategy_id']}-{pending['trial_id']}-"
        f"{pending['reasoning_effort']}.json"
    )
    return _write_f3_public_json(repository, relative, record)


def _f3_metric_result(
    repository: Path,
    *,
    strategy: Mapping[str, Any],
    pending: Mapping[str, Any],
    metric: str,
    value: float,
    output_sha256: str,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": F3_PUBLIC_METRIC_SCHEMA,
        "run_spec_sha256": pending["run_spec_sha256"],
        "strategy_id": strategy["strategy_id"],
        "reasoning_effort": pending["reasoning_effort"],
        "metric": metric,
        "value": value,
        "output_sha256": output_sha256,
    }
    record["record_sha256"] = sha256_json(record)
    relative = _F3_REVIEWS_ROOT / (
        f"{strategy['strategy_id']}-{pending['trial_id']}-"
        f"{pending['reasoning_effort']}-{metric}.json"
    )
    reference = _write_f3_public_json(repository, relative, record)
    field = "score" if metric == "answer_quality" else "recovered_evidence_recall"
    # Legacy v2 naming: this nested artifact_sha256 binds the metric file's raw
    # bytes. The metric object's record_sha256 binds its canonical JSON content.
    return {
        "status": "scored",
        field: value,
        "artifact_path": reference["path"],
        "artifact_sha256": reference["sha256"],
    }


def _f3_answer_quality(answer: str, required_evidence_ids: Sequence[str]) -> float:
    markers = [value.split(":", 1)[-1] for value in required_evidence_ids]
    covered = sum(
        bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(marker)}(?![A-Za-z0-9])", answer))
        for marker in markers
    )
    return round(covered / len(markers), 6)


def _f3_completed_cell(
    repository: Path,
    *,
    prepared_result: Mapping[str, Any],
    strategy: Mapping[str, Any],
    pending: Mapping[str, Any],
    generation: Mapping[str, Any],
    reservation_id: str,
) -> dict[str, Any]:
    metadata = generation["metadata"]
    output = _f3_output_reference(
        repository,
        strategy=strategy,
        pending=pending,
        answer=str(generation["answer"]),
    )
    route = metadata.get("live_route")
    if not isinstance(route, Mapping):
        raise F3Error("F3 provider result has no live route price")
    receipt: dict[str, Any] = {
        "schema_version": F3_GENERATOR_RECEIPT_SCHEMA,
        "run_spec_sha256": pending["run_spec_sha256"],
        "trial_id": pending["trial_id"],
        "provider_repeat_sample_id": pending["provider_repeat_sample_id"],
        "temperature": F3_TEMPERATURE,
        "requested_model": MODEL_ID,
        "resolved_model": metadata.get("resolved_model"),
        "reasoning_effort": pending["reasoning_effort"],
        "provider": metadata.get("provider"),
        "request_id": metadata.get("request_id"),
        "ledger_reservation_id": reservation_id,
        "native_usage": {
            "prompt_tokens": int(metadata.get("native_prompt_tokens") or 0),
            "completion_tokens": int(metadata.get("native_completion_tokens") or 0),
            "reasoning_tokens": int(metadata.get("native_reasoning_tokens") or 0),
            "cached_prompt_tokens": int(metadata.get("cached_prompt_tokens") or 0),
        },
        "billed_cost_usd": str(metadata.get("actual_usd")),
        "price": {
            "currency": "USD",
            "input_usd_per_million": str(route.get("input_usd_per_million")),
            "output_usd_per_million": str(route.get("output_usd_per_million")),
            "source": str(metadata.get("cost_source")),
        },
        "latency_ms": metadata.get("latency_ms"),
        "retry_count": metadata.get("retry_count"),
        "status": "completed",
        "error": None,
        "output_sha256": output["sha256"],
        "output_artifact": output,
    }
    receipt["receipt_sha256"] = sha256_json(receipt)
    quality = _f3_answer_quality(
        str(generation["answer"]),
        prepared_result["preparation"]["required_evidence_ids"],
    )
    cell: dict[str, Any] = {
        "schema_version": F3_ANSWER_QUALITY_RESULT_SCHEMA,
        "strategy_id": strategy["strategy_id"],
        "trial_id": pending["trial_id"],
        "provider_repeat_sample_id": pending["provider_repeat_sample_id"],
        "reasoning_effort": pending["reasoning_effort"],
        "run_spec_sha256": pending["run_spec_sha256"],
        "generator_receipt": receipt,
        "answer_quality_result": _f3_metric_result(
            repository,
            strategy=strategy,
            pending=pending,
            metric="answer_quality",
            value=quality,
            output_sha256=output["sha256"],
        ),
        "evidence_result": _f3_metric_result(
            repository,
            strategy=strategy,
            pending=pending,
            metric="recovered_evidence_recall",
            value=float(strategy["metrics"]["recovered_evidence_recall"]),
            output_sha256=output["sha256"],
        ),
    }
    cell["cell_result_sha256"] = sha256_json(cell)
    return cell


def _f3_provider_failure_cell(
    *,
    strategy: Mapping[str, Any],
    pending: Mapping[str, Any],
    generation: Mapping[str, Any],
    reservation_id: str,
) -> dict[str, Any]:
    metadata = generation["metadata"]
    route = metadata.get("live_route")
    if not isinstance(route, Mapping):
        raise F3Error("failed F3 provider result has no live route price")
    error = str(generation["error"])
    receipt: dict[str, Any] = {
        "schema_version": F3_GENERATOR_RECEIPT_SCHEMA,
        "run_spec_sha256": pending["run_spec_sha256"],
        "trial_id": pending["trial_id"],
        "provider_repeat_sample_id": pending["provider_repeat_sample_id"],
        "temperature": F3_TEMPERATURE,
        "requested_model": MODEL_ID,
        "resolved_model": metadata.get("resolved_model"),
        "reasoning_effort": pending["reasoning_effort"],
        "provider": metadata.get("provider"),
        "request_id": metadata.get("request_id"),
        "ledger_reservation_id": reservation_id,
        "native_usage": {
            "prompt_tokens": int(metadata.get("native_prompt_tokens") or 0),
            "completion_tokens": int(metadata.get("native_completion_tokens") or 0),
            "reasoning_tokens": int(metadata.get("native_reasoning_tokens") or 0),
            "cached_prompt_tokens": int(metadata.get("cached_prompt_tokens") or 0),
        },
        "billed_cost_usd": str(metadata.get("actual_usd")),
        "price": {
            "currency": "USD",
            "input_usd_per_million": str(route.get("input_usd_per_million")),
            "output_usd_per_million": str(route.get("output_usd_per_million")),
            "source": str(metadata.get("cost_source")),
        },
        "latency_ms": metadata.get("latency_ms"),
        "retry_count": metadata.get("retry_count"),
        "status": "failed",
        "error": error,
        "output_sha256": sha256_json(
            {"error": error, "request_id": metadata.get("request_id")}
        ),
        "output_artifact": None,
    }
    receipt["receipt_sha256"] = sha256_json(receipt)
    cell: dict[str, Any] = {
        "schema_version": F3_ANSWER_QUALITY_RESULT_SCHEMA,
        "strategy_id": strategy["strategy_id"],
        "trial_id": pending["trial_id"],
        "provider_repeat_sample_id": pending["provider_repeat_sample_id"],
        "reasoning_effort": pending["reasoning_effort"],
        "run_spec_sha256": pending["run_spec_sha256"],
        "generator_receipt": receipt,
        "answer_quality_result": {
            "status": "unavailable",
            "score": None,
            "artifact_path": None,
            "artifact_sha256": None,
        },
        "evidence_result": {
            "status": "unavailable",
            "recovered_evidence_recall": None,
            "artifact_path": None,
            "artifact_sha256": None,
        },
    }
    cell["cell_result_sha256"] = sha256_json(cell)
    return cell


def _f3_overflow_cell(
    *,
    strategy: Mapping[str, Any],
    pending: Mapping[str, Any],
    ledger: CostLedger,
    reservation_id: str,
) -> dict[str, Any]:
    error = "full_history_exceeds_token_budget"
    ledger.reserve(reservation_id, input_tokens=0, output_tokens=0)
    ledger.cancel(reservation_id, reason=error)
    receipt: dict[str, Any] = {
        "schema_version": F3_GENERATOR_RECEIPT_SCHEMA,
        "run_spec_sha256": pending["run_spec_sha256"],
        "trial_id": pending["trial_id"],
        "provider_repeat_sample_id": pending["provider_repeat_sample_id"],
        "temperature": F3_TEMPERATURE,
        "requested_model": MODEL_ID,
        "resolved_model": None,
        "reasoning_effort": pending["reasoning_effort"],
        "provider": None,
        "request_id": None,
        "ledger_reservation_id": reservation_id,
        "native_usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
            "cached_prompt_tokens": 0,
        },
        "billed_cost_usd": "0",
        "price": {
            "currency": "USD",
            "input_usd_per_million": PINNED_ROUTE_INPUT_USD_PER_MILLION,
            "output_usd_per_million": PINNED_ROUTE_OUTPUT_USD_PER_MILLION,
            "source": "not_billed_overflow",
        },
        "latency_ms": 0,
        "retry_count": 0,
        "status": "failed",
        "error": error,
        "output_sha256": sha256_json({"error": error}),
        "output_artifact": None,
    }
    receipt["receipt_sha256"] = sha256_json(receipt)
    cell: dict[str, Any] = {
        "schema_version": F3_ANSWER_QUALITY_RESULT_SCHEMA,
        "strategy_id": strategy["strategy_id"],
        "trial_id": pending["trial_id"],
        "provider_repeat_sample_id": pending["provider_repeat_sample_id"],
        "reasoning_effort": pending["reasoning_effort"],
        "run_spec_sha256": pending["run_spec_sha256"],
        "generator_receipt": receipt,
        "answer_quality_result": {
            "status": "unavailable",
            "score": None,
            "artifact_path": None,
            "artifact_sha256": None,
        },
        "evidence_result": {
            "status": "unavailable",
            "recovered_evidence_recall": None,
            "artifact_path": None,
            "artifact_sha256": None,
        },
    }
    cell["cell_result_sha256"] = sha256_json(cell)
    return cell


def execute_f3_experiment(
    root: Path | None = None,
    *,
    prepared_result: Mapping[str, Any],
    generation_runner: Callable[..., Mapping[str, Any]] = run_paid_generation_to_file,
) -> dict[str, Any]:
    """Run or replay the exact public 40-cell F3 campaign and save import input."""

    repository = (root or repository_root()).resolve()
    gate = _approved_f3_gate(repository)
    validate_f3_experiment(prepared_result)
    if prepared_result.get("frontier_entry_gate_sha256") != gate["artifact_sha256"]:
        raise F3Error("F3 execution is bound to a different frontier gate")
    prepared_path = _safe_result_path(repository, F3_RESULT_PATH)
    if (
        not prepared_path.is_file()
        or prepared_path.is_symlink()
        or prepared_path.read_bytes() != _json_bytes(prepared_result)
    ):
        raise F3Error("F3 execution requires the exact saved prepared result")

    completion_path = _safe_result_path(repository, F3_COMPLETION_INPUT_PATH)
    if completion_path.exists():
        if completion_path.is_symlink() or not completion_path.is_file():
            raise F3Error("saved F3 completion input is unsafe")
        try:
            saved = json.loads(completion_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise F3Error("saved F3 completion input is invalid") from exc
        if not isinstance(saved, dict) or set(saved) != {
            "cell_results",
            "provider_call_count",
            "overflow_failure_count",
        }:
            raise F3Error("saved F3 completion input fields changed")
        _build_f3_final_result(prepared_result, saved["cell_results"], repository)
        return saved

    task_text = _f3_task_text(prepared_result)
    ledger = CostLedger(canonical_ledger_path(repository))
    cells: list[dict[str, Any]] = []
    for strategy, pending in _expected_quality_cells(prepared_result):
        reservation_id = (
            f"{F3_EXECUTION_ATTEMPT}-{strategy['strategy_id']}-{pending['trial_id']}-"
            f"{pending['reasoning_effort']}"
        )
        if strategy["preparation_status"] == "failed-overflow":
            cells.append(
                _f3_overflow_cell(
                    strategy=strategy,
                    pending=pending,
                    ledger=ledger,
                    reservation_id=reservation_id,
                )
            )
            continue
        spec = _f3_generation_spec(
            prepared_result, strategy, pending, task_text=task_text
        )
        provider_path = _f3_provider_path(repository, strategy, pending)
        generation = _load_saved_f3_generation(provider_path, spec=spec)
        if generation is None:
            runner_kwargs: dict[str, Any] = {
                "ledger": ledger,
                "root": repository,
                "ledger_reservation_id": reservation_id,
            }
            if generation_runner is run_paid_generation_to_file:
                runner_kwargs["provider_slug"] = FRONTIER_PROVIDER_SLUG
            generation = dict(
                generation_runner(
                    spec,
                    provider_path,
                    **runner_kwargs,
                )
            )
        if generation.get("schema_version") == "contextlab.failed-generation-result.v2":
            cells.append(
                _f3_provider_failure_cell(
                    strategy=strategy,
                    pending=pending,
                    generation=generation,
                    reservation_id=reservation_id,
                )
            )
        else:
            cells.append(
                _f3_completed_cell(
                    repository,
                    prepared_result=prepared_result,
                    strategy=strategy,
                    pending=pending,
                    generation=generation,
                    reservation_id=reservation_id,
                )
            )

    envelope = {
        "cell_results": cells,
        "provider_call_count": sum(
            cell["generator_receipt"]["request_id"] is not None for cell in cells
        ),
        "overflow_failure_count": sum(
            cell["generator_receipt"]["error"]
            == "full_history_exceeds_token_budget"
            for cell in cells
        ),
    }
    _write_immutable_result(
        repository, completion_path, envelope, label="F3 completion input"
    )
    return envelope


def finalize_f3_experiment(
    root: Path | None = None,
    *,
    prepared_result: Mapping[str, Any],
    cell_results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Import five complete low/high trials and create the terminal artifact once."""

    repository = (root or repository_root()).resolve()
    gate = _approved_f3_gate(repository)
    validate_f3_experiment(prepared_result)
    source_commitment = _require_approved_source(
        repository, gate, prepared_result["approved_source_commitment"]
    )
    if prepared_result.get("frontier_entry_gate_sha256") != gate["artifact_sha256"]:
        raise F3Error("F3 prepared result is bound to a different frontier gate")
    if prepared_result.get("approved_source_commitment") != source_commitment:
        raise F3Error("F3 prepared result source commitment is not current")
    _replay_catalog_from_committed_sources(
        repository,
        commitment=source_commitment,
        source_manifest_artifact_sha256=prepared_result[
            "source_manifest_artifact_sha256"
        ],
        catalog=prepared_result["catalog"],
    )
    prepared_path = _safe_result_path(repository, F3_RESULT_PATH)
    if (
        not prepared_path.is_file()
        or prepared_path.is_symlink()
        or prepared_path.read_bytes() != _json_bytes(prepared_result)
    ):
        raise F3Error("F3 finalization requires the exact saved prepared result")
    final = _build_f3_final_result(prepared_result, cell_results, repository)
    final_path = _safe_result_path(repository, F3_FINAL_RESULT_PATH)
    return _write_immutable_result(
        repository, final_path, final, label="F3 final result"
    )
