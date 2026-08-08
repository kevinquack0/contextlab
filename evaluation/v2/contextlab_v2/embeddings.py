"""Resumable, auditable OpenRouter embeddings backed by a JSONL cache.

The cache is intentionally simple: every row contains one frozen-model vector
under ``openai/text-embedding-3-small:<sha1(text)>``.  Cache reads and appends
share a file lock so concurrent runners cannot duplicate a paid batch.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .costs import CostLedger, canonical_ledger_path
from .credentials import redact, require_runtime_credential
from .gateway import KEY_STATUS_URL, validate_key_status


EMBEDDING_MODEL = "openai/text-embedding-3-small"
EMBEDDING_RESPONSE_MODEL_ALIASES = frozenset(
    {EMBEDDING_MODEL, "text-embedding-3-small"}
)
EMBEDDING_DIMENSIONS = 1536
EMBEDDINGS_URL = "https://openrouter.ai/api/v1/embeddings"
DEFAULT_BATCH_SIZE = 64

JsonPost = Callable[[str, Mapping[str, Any], str], Mapping[str, Any]]
AuthorizedJsonGet = Callable[[str, str], Mapping[str, Any]]


class EmbeddingGatewayError(RuntimeError):
    """An embedding cache operation cannot complete safely."""


@dataclass(frozen=True)
class EmbeddingResult:
    """Aligned vectors plus provider-measured metadata for new paid batches."""

    vectors: list[list[float]]
    batches: list[dict[str, Any]]


def embedding_key(text: str) -> str:
    """Return the stable cache key for one exact Unicode string."""
    if not isinstance(text, str):
        raise EmbeddingGatewayError("embedding input must be text")
    return f"{EMBEDDING_MODEL}:{hashlib.sha1(text.encode('utf-8')).hexdigest()}"


def _validate_expected_hash(expected_base_sha256: str) -> None:
    if re.fullmatch(r"[0-9a-f]{64}", expected_base_sha256 or "") is None:
        raise EmbeddingGatewayError("expected base cache SHA-256 is invalid")


def _validate_vector(value: Any, *, context: str) -> list[float]:
    if not isinstance(value, list) or len(value) != EMBEDDING_DIMENSIONS:
        raise EmbeddingGatewayError(
            f"{context} must contain exactly {EMBEDDING_DIMENSIONS} dimensions"
        )
    vector: list[float] = []
    for dimension in value:
        if isinstance(dimension, bool) or not isinstance(dimension, (int, float)):
            raise EmbeddingGatewayError(f"{context} contains a non-numeric dimension")
        number = float(dimension)
        if not math.isfinite(number):
            raise EmbeddingGatewayError(f"{context} contains a non-finite dimension")
        vector.append(number)
    return vector


def _cache_rows(handle: Any) -> dict[str, list[float]]:
    handle.seek(0)
    cache: dict[str, list[float]] = {}
    for line_number, line in enumerate(handle, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EmbeddingGatewayError(
                f"invalid embedding cache row {line_number}"
            ) from exc
        if not isinstance(row, Mapping):
            raise EmbeddingGatewayError(
                f"embedding cache row {line_number} is not an object"
            )
        text_sha1 = row.get("text_sha1")
        key = row.get("key")
        if (
            row.get("model") != EMBEDDING_MODEL
            or not isinstance(text_sha1, str)
            or re.fullmatch(r"[0-9a-f]{40}", text_sha1) is None
            or key != f"{EMBEDDING_MODEL}:{text_sha1}"
        ):
            raise EmbeddingGatewayError(
                f"embedding cache row {line_number} has an invalid identity"
            )
        if key in cache:
            raise EmbeddingGatewayError(
                f"embedding cache row {line_number} duplicates a key"
            )
        cache[key] = _validate_vector(
            row.get("embedding"), context=f"embedding cache row {line_number}"
        )
    return cache


def _checked_cache_hash(path: Path, expected_base_sha256: str) -> None:
    _validate_expected_hash(expected_base_sha256)
    actual = hashlib.sha256(path.read_bytes() if path.exists() else b"").hexdigest()
    if actual != expected_base_sha256:
        raise EmbeddingGatewayError(
            "embedding cache SHA-256 does not match the expected base"
        )


def load_embedding_cache(
    path: Path, *, expected_base_sha256: str
) -> dict[str, list[float]]:
    """Load a pinned JSONL cache after verifying its exact starting digest."""
    _checked_cache_hash(path, expected_base_sha256)
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            return _cache_rows(handle)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_extension_cache(path: Path) -> dict[str, list[float]]:
    """Load a locally-created extension cache; its contents are validated in full."""
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            return _cache_rows(handle)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _post_json(
    url: str, payload: Mapping[str, Any], credential: str
) -> Mapping[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        ),
        headers={
            "Authorization": f"Bearer {credential}",
            "Content-Type": "application/json",
            "X-OpenRouter-Metadata": "enabled",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise EmbeddingGatewayError(
            f"OpenRouter embeddings returned HTTP {exc.code}"
        ) from exc
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise EmbeddingGatewayError(
            "OpenRouter embeddings failed without a usable response"
        ) from exc
    if not isinstance(value, Mapping):
        raise EmbeddingGatewayError("OpenRouter embeddings response is not an object")
    return value


def _get_authorized_json(url: str, credential: str) -> Mapping[str, Any]:
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {credential}"}, method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise EmbeddingGatewayError(
            f"OpenRouter key metadata returned HTTP {exc.code}"
        ) from exc
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise EmbeddingGatewayError("cannot fetch OpenRouter key metadata") from exc
    if not isinstance(value, Mapping):
        raise EmbeddingGatewayError("OpenRouter key metadata is not an object")
    return value


def _validate_run_id(run_id: str) -> None:
    if (
        not isinstance(run_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,112}", run_id) is None
    ):
        raise EmbeddingGatewayError(
            "embedding run ID must be a bounded safe identifier"
        )


def _batch_reservation_id(run_id: str, batch_number: int) -> str:
    return f"{run_id}.embed-{batch_number:04d}"


def _input_token_ceiling(texts: Sequence[str]) -> int:
    # UTF-8 bytes are a safe token ceiling; allowance covers JSON and provider framing.
    return sum(len(text.encode("utf-8")) for text in texts) + 4096


def _response_vectors(
    response: Mapping[str, Any], expected_count: int
) -> list[list[float]]:
    if (
        response.get("model") is not None
        and response.get("model") not in EMBEDDING_RESPONSE_MODEL_ALIASES
    ):
        raise EmbeddingGatewayError("OpenRouter resolved a different embedding model")
    data = response.get("data")
    if not isinstance(data, list) or len(data) != expected_count:
        raise EmbeddingGatewayError(
            "OpenRouter embeddings response has the wrong vector count"
        )
    indexed: dict[int, list[float]] = {}
    for item in data:
        if not isinstance(item, Mapping):
            raise EmbeddingGatewayError(
                "OpenRouter embeddings data item is not an object"
            )
        index = item.get("index")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index < expected_count
        ):
            raise EmbeddingGatewayError(
                "OpenRouter embeddings response index is invalid"
            )
        if index in indexed:
            raise EmbeddingGatewayError(
                "OpenRouter embeddings response repeats an index"
            )
        indexed[index] = _validate_vector(
            item.get("embedding"), context="provider embedding"
        )
    if set(indexed) != set(range(expected_count)):
        raise EmbeddingGatewayError("OpenRouter embeddings response omits an index")
    return [indexed[index] for index in range(expected_count)]


def _response_cost(response: Mapping[str, Any]) -> Decimal:
    usage = response.get("usage")
    reported = usage.get("cost") if isinstance(usage, Mapping) else None
    if reported is None:
        reported = response.get("cost")
    if reported is None:
        raise EmbeddingGatewayError("OpenRouter embeddings response has no billed cost")
    try:
        cost = Decimal(str(reported))
    except Exception as exc:
        raise EmbeddingGatewayError(
            "OpenRouter embeddings reported an invalid cost"
        ) from exc
    if not cost.is_finite() or cost < 0:
        raise EmbeddingGatewayError("OpenRouter embeddings reported an invalid cost")
    return cost


def _response_request_id(response: Mapping[str, Any]) -> str:
    request_id = response.get("id", response.get("request_id"))
    if not isinstance(request_id, str) or not request_id:
        raise EmbeddingGatewayError("OpenRouter embeddings response has no request ID")
    return request_id


def _provider_latency(response: Mapping[str, Any]) -> int | None:
    value = response.get("latency", response.get("latency_ms"))
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        return None
    if value < 0:
        return None
    return int(value)


def _append_rows(handle: Any, rows: Sequence[Mapping[str, Any]]) -> None:
    payload = "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows
    )
    handle.seek(0, 2)
    handle.write(payload)
    handle.flush()
    os.fsync(handle.fileno())


def embed_texts(
    texts: Sequence[str],
    *,
    base_cache_path: Path,
    extension_cache_path: Path,
    expected_base_sha256: str,
    ledger: CostLedger,
    run_id: str,
    environment: Mapping[str, str] | None = None,
    post_json: JsonPost = _post_json,
    get_authorized_json: AuthorizedJsonGet = _get_authorized_json,
    batch_size: int = DEFAULT_BATCH_SIZE,
    root: Path | None = None,
) -> EmbeddingResult:
    """Return vectors in input order, paying only for distinct missing strings.

    The immutable base cache digest is checked before use; only the extension
    file is opened for append. Once a POST starts, its reservation remains
    active on every failure because the provider may have accepted and billed
    that request.
    """
    if isinstance(texts, (str, bytes)) or not isinstance(texts, Sequence):
        raise EmbeddingGatewayError(
            "embedding inputs must be a sequence of text values"
        )
    if not all(isinstance(text, str) for text in texts):
        raise EmbeddingGatewayError("embedding inputs must be text")
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or batch_size < 1
    ):
        raise EmbeddingGatewayError("embedding batch size must be a positive integer")
    _validate_run_id(run_id)
    expected_ledger = canonical_ledger_path(root)
    if ledger.path.resolve() != expected_ledger:
        raise EmbeddingGatewayError(
            f"embeddings must use the canonical ledger: {expected_ledger}"
        )
    base_cache = load_embedding_cache(
        base_cache_path, expected_base_sha256=expected_base_sha256
    )
    ordered_keys = [embedding_key(text) for text in texts]
    if not extension_cache_path.exists() and all(
        key in base_cache for key in ordered_keys
    ):
        return EmbeddingResult(
            vectors=[list(base_cache[key]) for key in ordered_keys], batches=[]
        )
    extension_cache_path.parent.mkdir(parents=True, exist_ok=True)
    with extension_cache_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            extension_cache = _cache_rows(handle)
            overlap = set(base_cache).intersection(extension_cache)
            if overlap:
                raise EmbeddingGatewayError(
                    "extension cache duplicates an immutable base key"
                )
            cache = {**base_cache, **extension_cache}
            missing: list[tuple[str, str]] = []
            seen_missing: set[str] = set()
            for text, key in zip(texts, ordered_keys):
                if key not in cache and key not in seen_missing:
                    missing.append((text, key))
                    seen_missing.add(key)
            if not missing:
                return EmbeddingResult(
                    vectors=[list(cache[key]) for key in ordered_keys], batches=[]
                )

            credential = require_runtime_credential(environment)
            batches: list[dict[str, Any]] = []
            for offset in range(0, len(missing), batch_size):
                batch = missing[offset : offset + batch_size]
                batch_number = offset // batch_size + 1
                reservation_id = _batch_reservation_id(run_id, batch_number)
                batch_texts = [text for text, _ in batch]
                input_limit = _input_token_ceiling(batch_texts)
                # Embedding responses do not bill output tokens.  Reserving one
                # output token per returned dimension is deliberately stricter
                # than the shared ledger's generation-oriented price formula.
                output_limit = EMBEDDING_DIMENSIONS * len(batch)
                try:
                    key_status = validate_key_status(
                        get_authorized_json(KEY_STATUS_URL, credential)
                    )
                except Exception as exc:
                    safe_message = str(redact(str(exc), known_secret=credential))
                    raise EmbeddingGatewayError(
                        safe_message or "OpenRouter key status validation failed"
                    ) from exc
                ledger.reserve(
                    reservation_id,
                    input_tokens=input_limit,
                    output_tokens=output_limit,
                )
                sent = False
                stage = "provider_post"
                failure_metadata: dict[str, Any] = {
                    "request_id": None,
                    "requested_model": EMBEDDING_MODEL,
                    "input_count": len(batch),
                    "input_byte_token_ceiling": input_limit,
                    "actual_usd": None,
                    "latency_ms": None,
                    "retry_count": 0,
                    "error": None,
                }
                try:
                    sent = True
                    started = time.perf_counter()
                    response = post_json(
                        EMBEDDINGS_URL,
                        {
                            "model": EMBEDDING_MODEL,
                            "input": batch_texts,
                            "encoding_format": "float",
                        },
                        credential,
                    )
                    local_latency_ms = int((time.perf_counter() - started) * 1000)
                    stage = "response_validation"
                    vectors = _response_vectors(response, len(batch))
                    request_id = _response_request_id(response)
                    actual_usd = _response_cost(response)
                    provider_latency_ms = _provider_latency(response)
                    usage = (
                        response.get("usage")
                        if isinstance(response.get("usage"), Mapping)
                        else {}
                    )
                    failure_metadata.update(
                        {
                            "request_id": request_id,
                            "actual_usd": str(actual_usd),
                            "latency_ms": local_latency_ms,
                        }
                    )
                    stage = "provider_acknowledgment"
                    ledger.acknowledge(
                        reservation_id,
                        metadata={
                            "request_id": request_id,
                            "requested_model": EMBEDDING_MODEL,
                            "resolved_model": response.get("model", EMBEDDING_MODEL),
                            "input_count": len(batch),
                            "usage": dict(usage),
                            "reported_cost": str(actual_usd),
                            "local_round_trip_ms": local_latency_ms,
                        },
                    )
                    metadata = {
                        "request_id": request_id,
                        "requested_model": EMBEDDING_MODEL,
                        "resolved_model": response.get("model", EMBEDDING_MODEL),
                        "input_count": len(batch),
                        "input_byte_token_ceiling": input_limit,
                        "usage": dict(usage),
                        "actual_usd": str(actual_usd),
                        "cost_source": "openrouter_response_usage",
                        "latency_ms": (
                            provider_latency_ms
                            if provider_latency_ms is not None
                            else local_latency_ms
                        ),
                        "latency_source": (
                            "openrouter_response"
                            if provider_latency_ms is not None
                            else "local_round_trip"
                        ),
                        "local_round_trip_ms": local_latency_ms,
                        "key_status_before_call": key_status,
                        "retry_count": 0,
                    }
                    stage = "settlement"
                    ledger.settle(
                        reservation_id, actual_usd=actual_usd, metadata=metadata
                    )
                    rows = [
                        {
                            "embedding": vector,
                            "key": key,
                            "model": EMBEDDING_MODEL,
                            "text_sha1": key.rsplit(":", 1)[1],
                        }
                        for (_, key), vector in zip(batch, vectors)
                    ]
                    _append_rows(handle, rows)
                    cache.update({row["key"]: row["embedding"] for row in rows})
                    batches.append(metadata)
                except Exception as exc:
                    safe_message = str(redact(str(exc), known_secret=credential))
                    failure_metadata["error"] = safe_message
                    if sent:
                        try:
                            ledger.fail(
                                reservation_id,
                                stage=stage,
                                reason=safe_message,
                                metadata=failure_metadata,
                            )
                        except Exception:
                            pass
                    else:
                        ledger.cancel(reservation_id, reason=safe_message)
                    raise EmbeddingGatewayError(
                        safe_message or "embedding batch failed"
                    ) from exc
            return EmbeddingResult(
                vectors=[list(cache[key]) for key in ordered_keys], batches=batches
            )
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
