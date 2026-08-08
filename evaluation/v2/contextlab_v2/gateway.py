"""The only ContextLab v2 paid OpenRouter generation path."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

from .costs import HARD_CAP_USD, CostLedger, canonical_ledger_path
from .credentials import redact, require_runtime_credential
from .immutable_io import (
    ImmutableIOError,
    read_bytes_snapshot,
    replace_json_atomically,
    write_json_once_or_verify,
)
from .provider import (
    INPUT_USD_PER_MILLION,
    MODEL_ID,
    OUTPUT_USD_PER_MILLION,
    PROVIDER_SLUG,
    build_generation_request,
    validate_generation_request,
    validate_live_provider_endpoint,
    validate_resolved_response,
)
from .tasking import validate_prompt_safe_task


ENDPOINTS_URL = (
    "https://openrouter.ai/api/v1/models/deepseek/deepseek-v4-flash-20260731/endpoints"
)
CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
KEY_STATUS_URL = "https://openrouter.ai/api/v1/key"
GENERATION_SPEC_SCHEMA = "contextlab.generation-spec.v1"
GENERATION_SPEC_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "task",
        "system_instruction",
        "rendered_context",
        "reasoning_effort",
        "max_tokens",
        "temperature",
    }
)

JsonGet = Callable[[str], Mapping[str, Any]]
JsonPost = Callable[[str, Mapping[str, Any], str], Mapping[str, Any]]
AuthorizedJsonGet = Callable[[str, str], Mapping[str, Any]]
_LEDGER_RESERVATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class GenerationGatewayError(RuntimeError):
    """A fixed paid generation could not pass preflight or complete safely."""


def _reservation_id(spec: Mapping[str, Any], supplied: str | None) -> str:
    """Use the public run ID by default, or one bounded caller-supplied alias."""
    value = str(spec["run_id"]) if supplied is None else supplied
    if not isinstance(value, str) or _LEDGER_RESERVATION_ID.fullmatch(value) is None:
        raise GenerationGatewayError("ledger reservation ID is invalid")
    return value


def _ledger_metadata(metadata: Mapping[str, Any], *, opaque: bool) -> dict[str, Any]:
    """Keep external sealed-cell labels and provider errors out of the shared ledger."""
    if not opaque:
        return dict(metadata)
    return {
        key: value
        for key, value in metadata.items()
        if key not in {"reasoning_effort", "error"}
    }


def _ledger_reason(message: str, *, opaque: bool) -> str:
    return "external sealed generation failed" if opaque else message


def _get_json(url: str) -> Mapping[str, Any]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            value = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise GenerationGatewayError(
            "cannot fetch the live provider endpoint record"
        ) from exc
    if not isinstance(value, Mapping):
        raise GenerationGatewayError("live provider endpoint response is not an object")
    return value


def _post_json(
    url: str, payload: Mapping[str, Any], credential: str
) -> Mapping[str, Any]:
    return post_json_with_timeout(url, payload, credential, timeout_seconds=300)


def post_json_with_timeout(
    url: str,
    payload: Mapping[str, Any],
    credential: str,
    *,
    timeout_seconds: float,
) -> Mapping[str, Any]:
    """POST one provider request with a caller-owned positive timeout."""

    if timeout_seconds <= 0 or timeout_seconds > 300:
        raise GenerationGatewayError("OpenRouter timeout is outside 0..300 seconds")
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
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise GenerationGatewayError(f"OpenRouter returned HTTP {exc.code}") from exc
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise GenerationGatewayError(
            "OpenRouter generation failed without a usable response"
        ) from exc
    if not isinstance(value, Mapping):
        raise GenerationGatewayError("OpenRouter generation response is not an object")
    return value


def _get_authorized_json(url: str, credential: str) -> Mapping[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {credential}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise GenerationGatewayError(
            f"OpenRouter metadata returned HTTP {exc.code}"
        ) from exc
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise GenerationGatewayError(
            "cannot fetch OpenRouter generation metadata"
        ) from exc
    if not isinstance(value, Mapping):
        raise GenerationGatewayError("OpenRouter generation metadata is not an object")
    return value


def _generation_metadata(
    url: str,
    credential: str,
    get_authorized_json: AuthorizedJsonGet,
) -> tuple[Mapping[str, Any] | None, str]:
    """Fetch optional asynchronous metadata without blocking the paid result."""
    try:
        return get_authorized_json(url, credential), "available"
    except Exception:
        return None, "pending"


def validate_generation_spec(spec: Mapping[str, Any]) -> None:
    if set(spec) != GENERATION_SPEC_FIELDS:
        raise GenerationGatewayError(
            "generation spec fields differ from the fixed paid contract"
        )
    if spec.get("schema_version") != GENERATION_SPEC_SCHEMA:
        raise GenerationGatewayError("unsupported generation spec schema")
    run_id = spec.get("run_id")
    if (
        not isinstance(run_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id) is None
    ):
        raise GenerationGatewayError("generation spec requires a bounded run ID")
    task = spec.get("task")
    if not isinstance(task, dict):
        raise GenerationGatewayError("generation spec requires a prompt-safe task")
    validate_prompt_safe_task(task)
    for field in ("system_instruction", "rendered_context"):
        if not isinstance(spec.get(field), str):
            raise GenerationGatewayError(f"{field} must be text")


def _request_from_spec(
    spec: Mapping[str, Any], *, provider_slug: str = PROVIDER_SLUG
) -> dict[str, Any]:
    validate_generation_spec(spec)
    task = spec["task"]
    question_and_context = (
        f"Question:\n{task['question_text']}\n\n"
        f"Evidence context:\n{spec['rendered_context']}"
    )
    request = build_generation_request(
        [
            {"role": "system", "content": str(spec["system_instruction"])},
            {"role": "user", "content": question_and_context},
        ],
        effort=str(spec["reasoning_effort"]),
        max_tokens=spec["max_tokens"],
        temperature=spec["temperature"],
        provider_slug=provider_slug,
    )
    validate_generation_request(request, provider_slug=provider_slug)
    return request


def preflight_live_provider(
    *, get_json: JsonGet = _get_json, provider_slug: str = PROVIDER_SLUG
) -> dict[str, str]:
    return validate_live_provider_endpoint(
        get_json(ENDPOINTS_URL), provider_slug=provider_slug
    )


def validate_key_status(payload: Mapping[str, Any]) -> dict[str, str | None]:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise GenerationGatewayError("OpenRouter key status has no data object")
    try:
        limit = Decimal(str(data.get("limit")))
        remaining = Decimal(str(data.get("limit_remaining")))
        usage = Decimal(str(data.get("usage")))
    except Exception as exc:
        raise GenerationGatewayError("OpenRouter key limit fields are invalid") from exc
    if any(not value.is_finite() or value < 0 for value in (limit, remaining, usage)):
        raise GenerationGatewayError("OpenRouter key limit fields are invalid")
    if limit != HARD_CAP_USD or data.get("limit_reset") is not None:
        raise GenerationGatewayError(
            "OpenRouter key lacks the fixed non-resetting US$15 limit"
        )
    if remaining > limit:
        raise GenerationGatewayError(
            "OpenRouter key remaining limit exceeds its configured limit"
        )
    if remaining == 0:
        raise GenerationGatewayError("OpenRouter key limit is exhausted")
    return {
        "limit_usd": str(limit),
        "remaining_usd": str(remaining),
        "usage_usd": str(usage),
        "limit_reset": None,
    }


def _input_token_reservation_ceiling(request: Mapping[str, Any]) -> int:
    # A valid text token cannot consume less than one encoded byte. The extra
    # allowance covers chat framing added by the gateway/provider tokenizer.
    serialized_bytes = len(
        json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    return serialized_bytes + 4096


def _response_parts(response: Mapping[str, Any]) -> tuple[Any, dict[str, Any]]:
    choices = response.get("choices")
    usage = response.get("usage")
    if (
        not isinstance(choices, list)
        or len(choices) != 1
        or not isinstance(usage, Mapping)
    ):
        raise GenerationGatewayError(
            "OpenRouter response lacks one choice or usage metadata"
        )
    choice = choices[0]
    if not isinstance(choice, Mapping) or not isinstance(
        choice.get("message"), Mapping
    ):
        raise GenerationGatewayError("OpenRouter response choice has no message")
    answer = choice["message"].get("content")
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    if (
        isinstance(prompt_tokens, bool)
        or not isinstance(prompt_tokens, int)
        or prompt_tokens < 0
        or isinstance(completion_tokens, bool)
        or not isinstance(completion_tokens, int)
        or completion_tokens < 0
    ):
        raise GenerationGatewayError("OpenRouter response token usage is invalid")
    reasoning_tokens = (
        usage.get("completion_tokens_details", {}).get("reasoning_tokens")
        if isinstance(usage.get("completion_tokens_details"), Mapping)
        else None
    )
    cached_tokens = (
        usage.get("prompt_tokens_details", {}).get("cached_tokens")
        if isinstance(usage.get("prompt_tokens_details"), Mapping)
        else None
    )
    if reasoning_tokens is not None:
        reasoning_tokens = _nonnegative_integer(
            reasoning_tokens, "reasoning token usage"
        )
    if cached_tokens is not None:
        cached_tokens = _nonnegative_integer(cached_tokens, "cached token usage")
    return answer, {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reported_cost": usage.get("cost"),
        "reasoning_tokens": reasoning_tokens,
        "cached_tokens": cached_tokens,
    }


def _selected_provider(response: Mapping[str, Any]) -> Any:
    if response.get("provider") is not None:
        return response.get("provider")
    router = response.get("openrouter_metadata")
    if not isinstance(router, Mapping):
        return None
    endpoints = router.get("endpoints")
    available = endpoints.get("available") if isinstance(endpoints, Mapping) else None
    if not isinstance(available, list):
        return None
    selected = [
        endpoint.get("provider")
        for endpoint in available
        if isinstance(endpoint, Mapping) and endpoint.get("selected") is True
    ]
    return selected[0] if len(selected) == 1 else None


def _nonnegative_number(value: Any, label: str) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not Decimal(str(value)).is_finite()
        or value < 0
    ):
        raise GenerationGatewayError(f"OpenRouter generation {label} is invalid")
    return value


def _nonnegative_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GenerationGatewayError(f"OpenRouter generation {label} is invalid")
    return value


def validate_generation_metadata(
    payload: Mapping[str, Any], generation_id: str
) -> dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, Mapping) or data.get("id") != generation_id:
        raise GenerationGatewayError("OpenRouter generation metadata ID mismatch")
    total_cost = data.get("total_cost")
    if total_cost is None:
        raise GenerationGatewayError(
            "OpenRouter generation metadata has no billed cost"
        )
    try:
        billed = Decimal(str(total_cost))
    except Exception as exc:
        raise GenerationGatewayError(
            "OpenRouter generation metadata cost is invalid"
        ) from exc
    if not billed.is_finite() or billed < 0:
        raise GenerationGatewayError("OpenRouter generation metadata cost is invalid")
    validated = dict(data)
    validated["latency"] = _nonnegative_number(data.get("latency"), "latency")
    validated["generation_time"] = _nonnegative_number(
        data.get("generation_time"), "generation time"
    )
    for field in (
        "native_tokens_prompt",
        "native_tokens_completion",
        "native_tokens_reasoning",
    ):
        validated[field] = _nonnegative_integer(data.get(field), field)
    validate_resolved_response(
        {
            "requested_model": MODEL_ID,
            "resolved_model": data.get("model"),
            "provider": data.get("provider_name"),
            "reasoning_effort": "metadata-validation",
        },
        requested_effort="metadata-validation",
    )
    return validated


def _atomic_write_json(root: Path, path: Path, value: Mapping[str, Any]) -> None:
    try:
        replace_json_atomically(root, path, value)
    except ImmutableIOError as exc:
        raise GenerationGatewayError("generation artifact write is unsafe") from exc


def run_paid_generation(
    spec: Mapping[str, Any],
    *,
    ledger: CostLedger,
    environment: Mapping[str, str] | None = None,
    get_json: JsonGet = _get_json,
    post_json: JsonPost = _post_json,
    get_authorized_json: AuthorizedJsonGet = _get_authorized_json,
    root: Path | None = None,
    ledger_reservation_id: str | None = None,
    provider_slug: str = PROVIDER_SLUG,
) -> dict[str, Any]:
    """Live-preflight, reserve, call once, validate, and settle without exposing the key."""
    request = _request_from_spec(spec, provider_slug=provider_slug)
    expected_ledger = canonical_ledger_path(root)
    if ledger.path.resolve() != expected_ledger:
        raise GenerationGatewayError(
            f"paid generation must use the canonical ledger: {expected_ledger}"
        )
    live_route = preflight_live_provider(
        get_json=get_json, provider_slug=provider_slug
    )
    input_limit = _input_token_reservation_ceiling(request)
    output_limit = int(request["max_tokens"])
    credential = require_runtime_credential(environment)
    key_status = validate_key_status(get_authorized_json(KEY_STATUS_URL, credential))
    reservation_id = _reservation_id(spec, ledger_reservation_id)
    opaque_ledger = ledger_reservation_id is not None
    sent = False
    reserved = False
    stage = "pre_send_reservation"
    failure_metadata: dict[str, Any] = {
        "requested_model": MODEL_ID,
        "resolved_model": None,
        "provider": None,
        "reasoning_effort": spec["reasoning_effort"],
        "request_id": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "actual_usd": None,
        "latency_ms": None,
        "retry_count": 0,
        "error": None,
    }
    try:
        reservation = ledger.reserve(
            reservation_id,
            input_tokens=input_limit,
            output_tokens=output_limit,
        )
        reserved = True
        if reservation.get("reservation_id") != reservation_id:
            raise GenerationGatewayError("ledger reservation identity is invalid")
        stage = "provider_post"
        sent = True
        started = time.perf_counter()
        response = post_json(CHAT_COMPLETIONS_URL, request, credential)
        local_latency_ms = int((time.perf_counter() - started) * 1000)
        stage = "response_validation"
        answer, usage = _response_parts(response)
        if usage["completion_tokens"] > output_limit:
            raise GenerationGatewayError(
                "provider completion usage exceeds the requested limit"
            )
        generation_id = response.get("id")
        if not isinstance(generation_id, str) or not generation_id:
            raise GenerationGatewayError("OpenRouter response has no generation ID")
        response_provider = _selected_provider(response)
        failure_metadata.update(
            {
                "request_id": generation_id,
                "resolved_model": response.get("model"),
                "provider": response_provider,
                "prompt_tokens": usage["prompt_tokens"],
                "completion_tokens": usage["completion_tokens"],
                "actual_usd": usage["reported_cost"],
                "latency_ms": local_latency_ms,
            }
        )
        stage = "provider_acknowledgment"
        ledger.acknowledge(
            reservation_id,
            metadata=_ledger_metadata(
                {
                    "request_id": generation_id,
                    "resolved_model": response.get("model"),
                    "provider": response_provider,
                    "prompt_tokens": usage["prompt_tokens"],
                    "completion_tokens": usage["completion_tokens"],
                    "reported_cost": usage["reported_cost"],
                    "local_round_trip_ms": local_latency_ms,
                },
                opaque=opaque_ledger,
            ),
        )
        stats_response, generation_metadata_status = _generation_metadata(
            "https://openrouter.ai/api/v1/generation?"
            + urllib.parse.urlencode({"id": generation_id}),
            credential,
            get_authorized_json,
        )
        stats: dict[str, Any] = {}
        if stats_response is not None:
            try:
                stats = validate_generation_metadata(stats_response, generation_id)
            except GenerationGatewayError:
                generation_metadata_status = "invalid"
        reported_cost = stats.get("total_cost", usage["reported_cost"])
        if reported_cost is None:
            raise GenerationGatewayError("OpenRouter response has no billed cost")
        actual_usd = Decimal(str(reported_cost))
        if not actual_usd.is_finite() or actual_usd < 0:
            raise GenerationGatewayError(
                "OpenRouter reported a non-finite or negative cost"
            )
        cost_source = (
            "openrouter_generation_metadata"
            if stats.get("total_cost") is not None
            else "openrouter_response_usage"
        )
        provider_latency = stats.get("latency")
        metadata = {
            "request_id": generation_id,
            "requested_model": MODEL_ID,
            "resolved_model": stats.get("model", response.get("model")),
            "provider": stats.get("provider_name", response_provider),
            "reasoning_effort": spec["reasoning_effort"],
            "prompt_tokens": usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
            "actual_usd": str(actual_usd),
            "cost_source": cost_source,
            "latency_ms": provider_latency
            if provider_latency is not None
            else local_latency_ms,
            "latency_source": (
                "openrouter_generation_metadata"
                if provider_latency is not None
                else "local_round_trip"
            ),
            "generation_time_ms": stats.get("generation_time"),
            "local_round_trip_ms": local_latency_ms,
            "native_prompt_tokens": stats.get(
                "native_tokens_prompt", usage["prompt_tokens"]
            ),
            "native_completion_tokens": stats.get(
                "native_tokens_completion", usage["completion_tokens"]
            ),
            "native_reasoning_tokens": stats.get(
                "native_tokens_reasoning", usage["reasoning_tokens"]
            ),
            "cached_prompt_tokens": usage["cached_tokens"],
            "input_price_ceiling_usd_per_million": INPUT_USD_PER_MILLION,
            "output_price_ceiling_usd_per_million": OUTPUT_USD_PER_MILLION,
            "live_route": live_route,
            "key_status_before_call": key_status,
            "retry_count": 0,
            "generation_metadata_status": generation_metadata_status,
            "error": None,
        }
        validate_resolved_response(
            metadata,
            requested_effort=str(spec["reasoning_effort"]),
            provider_slug=provider_slug,
        )
        stage = "settlement"
        ledger.settle(
            reservation_id,
            actual_usd=actual_usd,
            metadata=_ledger_metadata(metadata, opaque=opaque_ledger),
        )
        if not isinstance(answer, str) or not answer.strip():
            return {
                "schema_version": "contextlab.failed-generation-result.v2",
                "run_id": spec["run_id"],
                "task_id": spec["task"]["task_id"],
                "error": "provider_finished_without_text",
                "metadata": metadata,
            }
        return {
            "schema_version": "contextlab.generation-result.v1",
            "run_id": spec["run_id"],
            "task_id": spec["task"]["task_id"],
            "answer": answer,
            "metadata": metadata,
        }
    except Exception as exc:
        # Once the POST begins, keep the worst-case reservation active. This
        # prevents another call from spending money that may already be owed.
        if not reserved:
            raise
        if reserved and not sent:
            ledger.cancel(
                reservation_id,
                reason=_ledger_reason(
                    str(redact(str(exc), known_secret=credential)),
                    opaque=opaque_ledger,
                ),
            )
        safe_message = str(redact(str(exc), known_secret=credential))
        failure_metadata["error"] = safe_message
        if sent:
            try:
                ledger.fail(
                    reservation_id,
                    stage=stage,
                    reason=_ledger_reason(safe_message, opaque=opaque_ledger),
                    metadata=_ledger_metadata(failure_metadata, opaque=opaque_ledger),
                )
            except Exception:
                pass
        raise GenerationGatewayError(safe_message or "paid generation failed") from exc


def run_paid_generation_to_file(
    spec: Mapping[str, Any],
    output_path: Path,
    *,
    ledger: CostLedger,
    environment: Mapping[str, str] | None = None,
    get_json: JsonGet = _get_json,
    post_json: JsonPost = _post_json,
    get_authorized_json: AuthorizedJsonGet = _get_authorized_json,
    root: Path | None = None,
    output_root: Path | None = None,
    ledger_reservation_id: str | None = None,
    provider_slug: str = PROVIDER_SLUG,
) -> dict[str, Any]:
    """Reserve an output destination before the paid POST, then write atomically."""
    repository = (root or Path.cwd()).resolve()
    validate_generation_spec(spec)
    reservation_id = _reservation_id(spec, ledger_reservation_id)
    opaque_ledger = ledger_reservation_id is not None
    requested_output = Path(os.path.abspath(output_path))
    if requested_output.is_symlink():
        raise GenerationGatewayError("paid generation output path is unsafe")
    output = requested_output.resolve(strict=False)
    if output_root is None:
        destination_root = repository / "results/v2"
        persistence_root = repository
    else:
        requested_root = Path(os.path.abspath(output_root))
        if requested_root.is_symlink():
            raise GenerationGatewayError("paid generation output root is unsafe")
        destination_root = requested_root.resolve(strict=False)
        # A caller selecting a destination is responsible for keeping prompt
        # material out of the repository.  Do not accept a filesystem root or
        # an ordinary shared temporary directory as that destination.
        if destination_root == Path(destination_root.anchor) or destination_root in {
            Path("/", "tmp").resolve(),
            Path("/", "var", "tmp").resolve(),
        }:
            raise GenerationGatewayError("paid generation output root is too broad")
        try:
            destination_root.relative_to(repository)
        except ValueError:
            pass
        else:
            raise GenerationGatewayError(
                "an explicit paid generation output root must stay outside the repository"
            )
        persistence_root = destination_root
        while not persistence_root.exists():
            if persistence_root == persistence_root.parent:
                raise GenerationGatewayError(
                    "paid generation output root has no safe parent"
                )
            persistence_root = persistence_root.parent
        if persistence_root.is_symlink() or not persistence_root.is_dir():
            raise GenerationGatewayError("paid generation output root is unsafe")
    try:
        output.relative_to(destination_root)
    except ValueError as exc:
        label = "results/v2" if output_root is None else "the explicit output root"
        raise GenerationGatewayError(
            f"paid generation output must stay under {label}"
        ) from exc
    if output == destination_root:
        raise GenerationGatewayError(
            "paid generation output must be a file below its output root"
        )
    try:
        created = write_json_once_or_verify(
            persistence_root,
            output,
            {
                "schema_version": "contextlab.pending-generation-result.v1",
                "run_id": spec.get("run_id"),
            },
        )
        if not created:
            raise GenerationGatewayError(
                "paid generation output is not a fresh writable file"
            )
    except ImmutableIOError as exc:
        raise GenerationGatewayError(
            "paid generation output is not a fresh writable file"
        ) from exc
    try:
        result = run_paid_generation(
            spec,
            ledger=ledger,
            environment=environment,
            get_json=get_json,
            post_json=post_json,
            get_authorized_json=get_authorized_json,
            root=repository,
            ledger_reservation_id=ledger_reservation_id,
            provider_slug=provider_slug,
        )
        try:
            _atomic_write_json(persistence_root, output, result)
        except Exception as exc:
            safe_message = str(redact(str(exc)))
            try:
                ledger.fail(
                    reservation_id,
                    stage="result_persistence",
                    reason=_ledger_reason(safe_message, opaque=opaque_ledger),
                    metadata=_ledger_metadata(
                        {**result["metadata"], "error": safe_message},
                        opaque=opaque_ledger,
                    ),
                )
            except Exception:
                pass
            raise GenerationGatewayError("paid result could not be saved") from exc
        return result
    except Exception as exc:
        _atomic_write_json(
            persistence_root,
            output,
            {
                "schema_version": "contextlab.failed-generation-result.v1",
                "run_id": spec.get("run_id"),
                "error": str(redact(str(exc))),
            },
        )
        raise


def refresh_generation_metadata(
    result_path: Path,
    *,
    ledger: CostLedger,
    environment: Mapping[str, str] | None = None,
    get_authorized_json: AuthorizedJsonGet = _get_authorized_json,
    root: Path | None = None,
) -> dict[str, Any]:
    """Add OpenRouter's delayed provider timing record to a saved paid result."""
    repository = (root or Path.cwd()).resolve()
    requested_result = Path(os.path.abspath(result_path))
    if requested_result.is_symlink():
        raise GenerationGatewayError("generation result path is unsafe")
    result_path = requested_result.resolve(strict=False)
    try:
        result_path.relative_to(repository / "results/v2")
    except ValueError as exc:
        raise GenerationGatewayError(
            "generation result must stay under results/v2"
        ) from exc
    if ledger.path.resolve() != canonical_ledger_path(repository):
        raise GenerationGatewayError(
            "generation enrichment requires the canonical ledger"
        )
    try:
        result = json.loads(
            read_bytes_snapshot(repository, result_path).decode("utf-8")
        )
    except (ImmutableIOError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GenerationGatewayError("generation result cannot be read safely") from exc
    if result.get("schema_version") != "contextlab.generation-result.v1":
        raise GenerationGatewayError("unsupported generation result schema")
    metadata = result.get("metadata")
    if not isinstance(metadata, dict):
        raise GenerationGatewayError("generation result has no metadata object")
    generation_id = metadata.get("request_id")
    if not isinstance(generation_id, str) or not generation_id:
        raise GenerationGatewayError("generation result has no request ID")
    credential = require_runtime_credential(environment)
    response = get_authorized_json(
        "https://openrouter.ai/api/v1/generation?"
        + urllib.parse.urlencode({"id": generation_id}),
        credential,
    )
    stats = validate_generation_metadata(response, generation_id)
    if stats.get("total_cost") is None or metadata.get("actual_usd") is None:
        raise GenerationGatewayError("generation cost is missing from enrichment data")
    billed = Decimal(str(stats["total_cost"]))
    recorded = Decimal(str(metadata["actual_usd"]))
    if not billed.is_finite() or billed < 0 or billed != recorded:
        raise GenerationGatewayError(
            "delayed generation cost differs from the saved billed cost"
        )
    updated_metadata = {
        **metadata,
        **{
            "resolved_model": stats["model"],
            "provider": stats["provider_name"],
            "cost_source": "openrouter_generation_metadata",
            "latency_ms": stats["latency"],
            "latency_source": "openrouter_generation_metadata",
            "generation_time_ms": stats["generation_time"],
            "native_prompt_tokens": stats["native_tokens_prompt"],
            "native_completion_tokens": stats["native_tokens_completion"],
            "native_reasoning_tokens": stats["native_tokens_reasoning"],
            "generation_metadata_status": "available",
        },
    }
    validate_resolved_response(
        updated_metadata, requested_effort=str(metadata.get("reasoning_effort"))
    )
    run_id = result.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise GenerationGatewayError("generation result has no run ID")
    enrichment = {
        "request_id": generation_id,
        "resolved_model": updated_metadata["resolved_model"],
        "provider": updated_metadata["provider"],
        "latency_ms": updated_metadata["latency_ms"],
        "generation_time_ms": updated_metadata["generation_time_ms"],
        "native_prompt_tokens": updated_metadata["native_prompt_tokens"],
        "native_completion_tokens": updated_metadata["native_completion_tokens"],
        "native_reasoning_tokens": updated_metadata["native_reasoning_tokens"],
        "actual_usd": updated_metadata["actual_usd"],
    }
    ledger.enrich(
        run_id,
        metadata=enrichment,
    )
    updated_result = {**result, "metadata": updated_metadata}
    _atomic_write_json(repository, result_path, updated_result)
    return updated_result
