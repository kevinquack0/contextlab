"""Pinned OpenRouter request contract for the ContextLab answer generator."""

from __future__ import annotations

import json
import math
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

from .baseline import repository_root


MODEL_ID = "deepseek/deepseek-v4-flash-0731"
CANONICAL_MODEL_ID = "deepseek/deepseek-v4-flash-20260731"
PROVIDER_SLUG = "deepseek"
FRONTIER_PROVIDER_SLUG = "deepinfra"
ALLOWED_REASONING_EFFORTS = ("low", "high")
MODEL_CONTEXT_TOKENS = 1_048_576
MODEL_MAX_COMPLETION_TOKENS = 65_536
PINNED_ROUTE_INPUT_USD_PER_MILLION = "0.14"
PINNED_ROUTE_OUTPUT_USD_PER_MILLION = "0.28"
# Reserve against a 10 percent price buffer. OpenRouter's max_price filter then
# rejects the request if the pinned route becomes more expensive than this.
INPUT_USD_PER_MILLION = "0.154"
OUTPUT_USD_PER_MILLION = "0.308"


class ProviderContractError(ValueError):
    """A request could reroute, change model, or change the reasoning factor."""


def build_generation_request(
    messages: Iterable[Mapping[str, str]],
    *,
    effort: str,
    max_tokens: int,
    temperature: float = 0.0,
    provider_slug: str = PROVIDER_SLUG,
) -> dict[str, Any]:
    if effort not in ALLOWED_REASONING_EFFORTS:
        raise ProviderContractError(
            f"reasoning effort must be one of {ALLOWED_REASONING_EFFORTS}, got {effort!r}"
        )
    if not 1 <= max_tokens <= MODEL_MAX_COMPLETION_TOKENS:
        raise ProviderContractError(
            f"max_tokens must be within 1..{MODEL_MAX_COMPLETION_TOKENS}"
        )
    request = {
        "model": MODEL_ID,
        "messages": [dict(message) for message in messages],
        "reasoning": {"effort": effort, "exclude": True},
        "provider": {
            "only": [provider_slug],
            "order": [provider_slug],
            "allow_fallbacks": False,
            "require_parameters": True,
            "max_price": {
                "prompt": float(INPUT_USD_PER_MILLION),
                "completion": float(OUTPUT_USD_PER_MILLION),
            },
        },
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    validate_generation_request(request, provider_slug=provider_slug)
    return request


def validate_generation_request(
    request: Mapping[str, Any], *, provider_slug: str = PROVIDER_SLUG
) -> None:
    expected_fields = {
        "model",
        "messages",
        "reasoning",
        "provider",
        "max_tokens",
        "temperature",
        "stream",
    }
    if set(request) != expected_fields:
        raise ProviderContractError("generation request fields differ from the fixed paid contract")
    if request.get("model") != MODEL_ID:
        raise ProviderContractError("generator model is not the pinned DeepSeek V4 Flash 0731 slug")
    if "models" in request:
        raise ProviderContractError("model fallbacks are prohibited")
    provider = request.get("provider")
    expected_provider = {
        "only": [provider_slug],
        "order": [provider_slug],
        "allow_fallbacks": False,
        "require_parameters": True,
        "max_price": {
            "prompt": float(INPUT_USD_PER_MILLION),
            "completion": float(OUTPUT_USD_PER_MILLION),
        },
    }
    if provider != expected_provider:
        raise ProviderContractError(
            f"request is not pinned to the {provider_slug} provider without fallback"
        )
    reasoning = request.get("reasoning")
    if not isinstance(reasoning, Mapping):
        raise ProviderContractError("reasoning configuration is required")
    if reasoning.get("effort") not in ALLOWED_REASONING_EFFORTS:
        raise ProviderContractError("request uses an unapproved reasoning effort")
    if reasoning.get("exclude") is not True:
        raise ProviderContractError("hidden reasoning must be excluded from saved responses")
    if set(reasoning) != {"effort", "exclude"}:
        raise ProviderContractError("reasoning fields differ from the fixed paid contract")
    messages = request.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ProviderContractError("generation request requires at least one message")
    for message in messages:
        if not isinstance(message, Mapping) or set(message) != {"role", "content"}:
            raise ProviderContractError("message fields differ from the fixed paid contract")
        if message.get("role") not in {"system", "user", "assistant"}:
            raise ProviderContractError("message role is invalid")
        if not isinstance(message.get("content"), str):
            raise ProviderContractError("message content must be text")
    max_tokens = request.get("max_tokens")
    if (
        isinstance(max_tokens, bool)
        or not isinstance(max_tokens, int)
        or not 1 <= max_tokens <= MODEL_MAX_COMPLETION_TOKENS
    ):
        raise ProviderContractError("max_tokens is outside the pinned model limit")
    temperature = request.get("temperature")
    if (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not math.isfinite(float(temperature))
        or not 0.0 <= float(temperature) <= 2.0
    ):
        raise ProviderContractError("temperature must be a finite number from 0 to 2")
    if request.get("stream") is not False:
        raise ProviderContractError("the frozen v2 request contract uses non-streaming responses")


def validate_provider_snapshot(root: Path | None = None) -> dict[str, Any]:
    root = (root or repository_root()).resolve()
    path = root / "evaluation" / "v2" / "provider" / "openrouter_model_snapshot.json"
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "model_id": MODEL_ID,
        "canonical_model_id": CANONICAL_MODEL_ID,
        "context_length": MODEL_CONTEXT_TOKENS,
        "max_completion_tokens": MODEL_MAX_COMPLETION_TOKENS,
        "pinned_route_input_usd_per_million": PINNED_ROUTE_INPUT_USD_PER_MILLION,
        "pinned_route_output_usd_per_million": PINNED_ROUTE_OUTPUT_USD_PER_MILLION,
        "budget_input_usd_per_million": INPUT_USD_PER_MILLION,
        "budget_output_usd_per_million": OUTPUT_USD_PER_MILLION,
    }
    for key, value in expected.items():
        if snapshot.get(key) != value:
            raise ProviderContractError(f"provider snapshot mismatch for {key}")
    supported = snapshot.get("provider_supported_reasoning_efforts")
    if not isinstance(supported, list) or not set(ALLOWED_REASONING_EFFORTS).issubset(supported):
        raise ProviderContractError("provider snapshot does not support both low and high reasoning")
    if snapshot.get("experiment_reasoning_efforts") != list(ALLOWED_REASONING_EFFORTS):
        raise ProviderContractError("snapshot does not freeze exactly low and high")
    if snapshot.get("provider_route") != PROVIDER_SLUG:
        raise ProviderContractError("snapshot provider route is not DeepSeek")
    if snapshot.get("pricing_scope") != "pinned_provider_endpoint":
        raise ProviderContractError("snapshot pricing is not for the pinned provider endpoint")
    if snapshot.get("price_buffer_percent") != 10:
        raise ProviderContractError("provider price buffer is not the frozen 10 percent")
    expected_routing = {
        "only": [PROVIDER_SLUG],
        "order": [PROVIDER_SLUG],
        "allow_fallbacks": False,
        "require_parameters": True,
        "max_price": {
            "prompt": float(INPUT_USD_PER_MILLION),
            "completion": float(OUTPUT_USD_PER_MILLION),
        },
    }
    if snapshot.get("routing_contract") != expected_routing:
        raise ProviderContractError("provider snapshot routing contract differs")
    return snapshot


def validate_live_provider_endpoint(
    payload: Mapping[str, Any], *, provider_slug: str = PROVIDER_SLUG
) -> dict[str, str]:
    """Validate a freshly fetched OpenRouter endpoint listing before paid calls."""
    data = payload.get("data")
    if not isinstance(data, Mapping) or data.get("id") != MODEL_ID:
        raise ProviderContractError("endpoint listing is not for the pinned model")
    endpoints = data.get("endpoints")
    if not isinstance(endpoints, list):
        raise ProviderContractError("endpoint listing has no endpoints")
    matches = [
        endpoint
        for endpoint in endpoints
        if isinstance(endpoint, Mapping)
        and str(endpoint.get("provider_name", "")).casefold() == provider_slug
    ]
    if len(matches) != 1:
        raise ProviderContractError(
            f"endpoint listing does not contain exactly one {provider_slug} route"
        )
    endpoint = matches[0]
    if endpoint.get("status") != 0:
        raise ProviderContractError(
            f"the pinned {provider_slug} route is not operational"
        )
    pricing = endpoint.get("pricing")
    if not isinstance(pricing, Mapping):
        raise ProviderContractError("the pinned DeepSeek route has no price record")
    prompt_per_million = Decimal(str(pricing.get("prompt"))) * Decimal("1000000")
    completion_per_million = Decimal(str(pricing.get("completion"))) * Decimal("1000000")
    if prompt_per_million > Decimal(INPUT_USD_PER_MILLION):
        raise ProviderContractError("DeepSeek input price exceeds the request and ledger ceiling")
    if completion_per_million > Decimal(OUTPUT_USD_PER_MILLION):
        raise ProviderContractError("DeepSeek output price exceeds the request and ledger ceiling")
    supported = endpoint.get("supported_parameters")
    if not isinstance(supported, list) or "reasoning_effort" not in supported:
        raise ProviderContractError("the pinned DeepSeek route lacks reasoning-effort support")
    return {
        "provider": str(endpoint["provider_name"]),
        "input_usd_per_million": str(prompt_per_million),
        "output_usd_per_million": str(completion_per_million),
    }


def validate_resolved_response(
    metadata: Mapping[str, Any],
    *,
    requested_effort: str,
    provider_slug: str = PROVIDER_SLUG,
) -> None:
    """Reject a completed call whose returned model or provider differs from the manifest."""
    if metadata.get("requested_model") != MODEL_ID:
        raise ProviderContractError("response metadata lost the requested model")
    if metadata.get("resolved_model") not in {MODEL_ID, CANONICAL_MODEL_ID}:
        raise ProviderContractError("OpenRouter resolved a different model")
    provider = str(metadata.get("provider", "")).casefold()
    if provider != provider_slug:
        raise ProviderContractError(
            f"OpenRouter resolved a non-{provider_slug} provider"
        )
    if metadata.get("reasoning_effort") != requested_effort:
        raise ProviderContractError("resolved reasoning effort differs from the request")
