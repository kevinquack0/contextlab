"""JSON Schema Draft 2020-12 contracts and a dependency-free fixture validator."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .baseline import repository_root


DRAFT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_PREFIX = "https://contextlab.local/schemas/"
SHA256_PATTERN = "^[0-9a-f]{64}$"


def _object_schema(
    name: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "$schema": DRAFT,
        "$id": f"{SCHEMA_PREFIX}{name}.schema.json",
        "title": name,
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


STRING = {"type": "string", "minLength": 1}
NULLABLE_STRING = {"type": ["string", "null"]}
SHA256 = {"type": "string", "pattern": SHA256_PATTERN}
NULLABLE_SHA256 = {"type": ["string", "null"], "pattern": SHA256_PATTERN}
DATETIME = {"type": "string", "format": "date-time"}
NULLABLE_DATETIME = {"type": ["string", "null"], "format": "date-time"}
NONNEGATIVE_INT = {"type": "integer", "minimum": 0}
NONNEGATIVE_NUMBER = {"type": "number", "minimum": 0}

CONTEXT_ACTION = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "schema_version": {"const": "contextlab.context-action.v1"},
        "action_id": STRING,
        "sequence": NONNEGATIVE_INT,
        "operation": {"enum": ["page_in", "page_out", "expand", "quote_recovery"]},
        "pointer": STRING,
        "content_sha256": SHA256,
        "token_delta": {"type": "integer"},
    },
    "required": [
        "schema_version",
        "action_id",
        "sequence",
        "operation",
        "pointer",
        "content_sha256",
        "token_delta",
    ],
}


SCHEMAS: dict[str, dict[str, Any]] = {
    "EvidenceCandidate": _object_schema(
        "EvidenceCandidate",
        {
            "schema_version": {"const": "contextlab.evidence-candidate.v1"},
            "candidate_id": STRING,
            "source_id": STRING,
            "section_id": NULLABLE_STRING,
            "content_hash": SHA256,
            "retrieval_stage": STRING,
            "rank": {"type": "integer", "minimum": 1},
            "raw_score": {"type": ["number", "null"]},
            "normalized_score": {
                "type": ["number", "null"],
                "minimum": 0,
                "maximum": 1,
            },
            "authority": NULLABLE_STRING,
            "effective_time": NULLABLE_DATETIME,
            "text_reference": STRING,
            "removal_reason": NULLABLE_STRING,
        },
        [
            "schema_version",
            "candidate_id",
            "source_id",
            "section_id",
            "content_hash",
            "retrieval_stage",
            "rank",
            "raw_score",
            "normalized_score",
            "authority",
            "effective_time",
            "text_reference",
            "removal_reason",
        ],
    ),
    "ContextPack": _object_schema(
        "ContextPack",
        {
            "schema_version": {"const": "contextlab.context-pack.v1"},
            "task_id": STRING,
            "strategy_id": STRING,
            "corpus_snapshot_id": STRING,
            "selected_candidate_ids": {
                "type": "array",
                "items": STRING,
                "uniqueItems": True,
            },
            "token_budget": NONNEGATIVE_INT,
            "rendered_context_hash": SHA256,
            "instructions_hash": SHA256,
            "build_time_ms": NONNEGATIVE_INT,
            "context_actions": {
                "type": "array",
                "items": CONTEXT_ACTION,
                "uniqueItems": True,
            },
        },
        [
            "schema_version",
            "task_id",
            "strategy_id",
            "corpus_snapshot_id",
            "selected_candidate_ids",
            "token_budget",
            "rendered_context_hash",
            "instructions_hash",
            "build_time_ms",
        ],
    ),
    "CorpusEvent": _object_schema(
        "CorpusEvent",
        {
            "schema_version": {"const": "contextlab.corpus-event.v1"},
            "event_id": STRING,
            "scenario_id": STRING,
            "source_id": STRING,
            "section_id": STRING,
            "content_hash": SHA256,
            "observed_time": DATETIME,
            "effective_time": DATETIME,
            "published_time": NULLABLE_DATETIME,
            "valid_from": DATETIME,
            "valid_to": NULLABLE_DATETIME,
            "authority_level": {"type": "integer", "minimum": 1, "maximum": 5},
            "status": {
                "enum": [
                    "draft",
                    "final",
                    "corrected",
                    "retracted",
                    "expired",
                    "tombstone",
                ]
            },
            "subject": STRING,
            "predicate": STRING,
            "value": {},
            "supersedes_event_id": NULLABLE_STRING,
            "tombstone_for_event_id": NULLABLE_STRING,
            "source_text_reference": STRING,
        },
        [
            "schema_version",
            "event_id",
            "scenario_id",
            "source_id",
            "section_id",
            "content_hash",
            "observed_time",
            "effective_time",
            "published_time",
            "valid_from",
            "valid_to",
            "authority_level",
            "status",
            "subject",
            "predicate",
            "value",
            "supersedes_event_id",
            "tombstone_for_event_id",
            "source_text_reference",
        ],
    ),
    "Claim": _object_schema(
        "Claim",
        {
            "schema_version": {"const": "contextlab.claim.v1"},
            "claim_id": STRING,
            "subject": STRING,
            "predicate": STRING,
            "value": {},
            "supporting_event_ids": {
                "type": "array",
                "items": STRING,
                "minItems": 1,
                "uniqueItems": True,
            },
            "valid_from": DATETIME,
            "valid_to": NULLABLE_DATETIME,
            "superseded_claim_id": NULLABLE_STRING,
            "tombstone_event_id": NULLABLE_STRING,
            "authority_level": {"type": "integer", "minimum": 1, "maximum": 5},
            "observed_time": DATETIME,
            "effective_time": DATETIME,
            "published_time": NULLABLE_DATETIME,
            "state": {
                "enum": [
                    "candidate",
                    "current",
                    "superseded",
                    "expired",
                    "retracted",
                    "conflicted",
                ]
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "write_policy_decision": {
                "enum": ["write", "ignore", "merge", "conflict", "tombstone"]
            },
        },
        [
            "schema_version",
            "claim_id",
            "subject",
            "predicate",
            "value",
            "supporting_event_ids",
            "valid_from",
            "valid_to",
            "superseded_claim_id",
            "tombstone_event_id",
            "authority_level",
            "observed_time",
            "effective_time",
            "published_time",
            "state",
            "confidence",
            "write_policy_decision",
        ],
    ),
    "Episode": _object_schema(
        "Episode",
        {
            "schema_version": {"const": "contextlab.episode.v1"},
            "episode_id": STRING,
            "task_signature": SHA256,
            "category": STRING,
            "selected_strategy": STRING,
            "evidence_path": {"type": "array", "items": STRING, "uniqueItems": True},
            "graded_outcome": {"type": "object"},
            "cost_usd": NONNEGATIVE_NUMBER,
            "latency_ms": NONNEGATIVE_INT,
            "failure_mode": NULLABLE_STRING,
            "source_run_id": STRING,
            "trace_id": STRING,
            "retention_decision": {"enum": ["retain", "expire", "remove"]},
            "promotion_decision": {"enum": ["pending", "promoted", "rejected"]},
        },
        [
            "schema_version",
            "episode_id",
            "task_signature",
            "category",
            "selected_strategy",
            "evidence_path",
            "graded_outcome",
            "cost_usd",
            "latency_ms",
            "failure_mode",
            "source_run_id",
            "trace_id",
            "retention_decision",
            "promotion_decision",
        ],
    ),
    "Run": _object_schema(
        "Run",
        {
            "schema_version": {"const": "contextlab.run.v1"},
            "run_id": STRING,
            "adapter_id": STRING,
            "configuration_hash": SHA256,
            "corpus_snapshot_id": STRING,
            "memory_snapshot_id": NULLABLE_STRING,
            "task_id": STRING,
            "context_pack_hash": SHA256,
            "answer": {"type": "string"},
            "cost_usd": NONNEGATIVE_NUMBER,
            "input_tokens": NONNEGATIVE_INT,
            "output_tokens": NONNEGATIVE_INT,
            "latency_ms": NONNEGATIVE_INT,
            "outcome": {"enum": ["succeeded", "failed", "blocked"]},
            "requested_model": STRING,
            "resolved_model": NULLABLE_STRING,
            "provider": NULLABLE_STRING,
            "reasoning_effort": {"enum": ["low", "high"]},
            "request_id": NULLABLE_STRING,
            "retry_count": NONNEGATIVE_INT,
            "error": NULLABLE_STRING,
        },
        [
            "schema_version",
            "run_id",
            "adapter_id",
            "configuration_hash",
            "corpus_snapshot_id",
            "memory_snapshot_id",
            "task_id",
            "context_pack_hash",
            "answer",
            "cost_usd",
            "input_tokens",
            "output_tokens",
            "latency_ms",
            "outcome",
            "requested_model",
            "resolved_model",
            "provider",
            "reasoning_effort",
            "request_id",
            "retry_count",
            "error",
        ],
    ),
    "Span": _object_schema(
        "Span",
        {
            "schema_version": {"const": "contextlab.span.v1"},
            "trace_id": STRING,
            "span_id": STRING,
            "parent_span_id": NULLABLE_STRING,
            "operation_name": STRING,
            "start_time": DATETIME,
            "end_time": DATETIME,
            "input_hash": NULLABLE_SHA256,
            "output_hash": NULLABLE_SHA256,
            "attributes": {"type": "object"},
            "events": {"type": "array", "items": {"type": "object"}},
            "context_actions": {
                "type": "array",
                "items": CONTEXT_ACTION,
                "uniqueItems": True,
            },
            "status": {"enum": ["unset", "ok", "error"]},
        },
        [
            "schema_version",
            "trace_id",
            "span_id",
            "parent_span_id",
            "operation_name",
            "start_time",
            "end_time",
            "input_hash",
            "output_hash",
            "attributes",
            "events",
            "status",
        ],
    ),
}


HASH_A = "a" * 64
HASH_B = "b" * 64

VALID_FIXTURES: dict[str, dict[str, Any]] = {
    "EvidenceCandidate": {
        "schema_version": "contextlab.evidence-candidate.v1",
        "candidate_id": "cand-001",
        "source_id": "NL-003",
        "section_id": "NL-003-S02",
        "content_hash": HASH_A,
        "retrieval_stage": "dense",
        "rank": 1,
        "raw_score": 0.82,
        "normalized_score": 0.91,
        "authority": "official_policy",
        "effective_time": "2026-06-01T00:00:00Z",
        "text_reference": "NL-003#NL-003-S02",
        "removal_reason": None,
    },
    "ContextPack": {
        "schema_version": "contextlab.context-pack.v1",
        "task_id": "S001",
        "strategy_id": "rag",
        "corpus_snapshot_id": "corpus-v1",
        "selected_candidate_ids": ["cand-001"],
        "token_budget": 8000,
        "rendered_context_hash": HASH_A,
        "instructions_hash": HASH_B,
        "build_time_ms": 12,
    },
    "CorpusEvent": {
        "schema_version": "contextlab.corpus-event.v1",
        "event_id": "event-001",
        "scenario_id": "TL-01",
        "source_id": "NLV2-001",
        "section_id": "NLV2-001-S01",
        "content_hash": HASH_A,
        "observed_time": "2026-08-01T12:00:00Z",
        "effective_time": "2026-08-01T00:00:00Z",
        "published_time": "2026-08-01T09:00:00Z",
        "valid_from": "2026-08-01T00:00:00Z",
        "valid_to": None,
        "authority_level": 5,
        "status": "final",
        "subject": "Starter",
        "predicate": "monthly_price_usd",
        "value": 1200,
        "supersedes_event_id": None,
        "tombstone_for_event_id": None,
        "source_text_reference": "novalearn_synthetic_corpus/v2/NLV2-001.md#NLV2-001-S01",
    },
    "Claim": {
        "schema_version": "contextlab.claim.v1",
        "claim_id": "claim-001",
        "subject": "Starter",
        "predicate": "monthly_price_usd",
        "value": 1200,
        "supporting_event_ids": ["event-001"],
        "valid_from": "2026-06-01T00:00:00Z",
        "valid_to": None,
        "superseded_claim_id": None,
        "tombstone_event_id": None,
        "authority_level": 5,
        "observed_time": "2026-08-01T12:00:00Z",
        "effective_time": "2026-06-01T00:00:00Z",
        "published_time": "2026-08-01T09:00:00Z",
        "state": "current",
        "confidence": 1.0,
        "write_policy_decision": "write",
    },
    "Episode": {
        "schema_version": "contextlab.episode.v1",
        "episode_id": "episode-001",
        "task_signature": HASH_A,
        "category": "authority_conflict",
        "selected_strategy": "hybrid",
        "evidence_path": ["cand-001"],
        "graded_outcome": {"accepted": True, "score": 3},
        "cost_usd": 0.001,
        "latency_ms": 320,
        "failure_mode": None,
        "source_run_id": "run-001",
        "trace_id": "trace-001",
        "retention_decision": "retain",
        "promotion_decision": "promoted",
    },
    "Run": {
        "schema_version": "contextlab.run.v1",
        "run_id": "run-001",
        "adapter_id": "rag",
        "configuration_hash": HASH_A,
        "corpus_snapshot_id": "corpus-v1",
        "memory_snapshot_id": None,
        "task_id": "S001",
        "context_pack_hash": HASH_B,
        "answer": "Starter is $1,200 [NL-003#NL-003-S02].",
        "cost_usd": 0.001,
        "input_tokens": 4000,
        "output_tokens": 80,
        "latency_ms": 500,
        "outcome": "succeeded",
        "requested_model": "deepseek/deepseek-v4-flash-0731",
        "resolved_model": "deepseek/deepseek-v4-flash-20260731",
        "provider": "DeepSeek",
        "reasoning_effort": "low",
        "request_id": "req-001",
        "retry_count": 0,
        "error": None,
    },
    "Span": {
        "schema_version": "contextlab.span.v1",
        "trace_id": "trace-001",
        "span_id": "span-001",
        "parent_span_id": None,
        "operation_name": "retrieve",
        "start_time": "2026-08-01T12:00:00Z",
        "end_time": "2026-08-01T12:00:00.100000Z",
        "input_hash": HASH_A,
        "output_hash": HASH_B,
        "attributes": {"strategy": "rag"},
        "events": [{"name": "candidate_selected"}],
        "status": "ok",
    },
}

INVALID_FIXTURES: dict[str, dict[str, Any]] = {
    "EvidenceCandidate": {**VALID_FIXTURES["EvidenceCandidate"], "rank": 0},
    "ContextPack": {**VALID_FIXTURES["ContextPack"], "token_budget": -1},
    "CorpusEvent": {
        **VALID_FIXTURES["CorpusEvent"],
        "observed_time": "2026-08-01T12:00:00",
    },
    "Claim": {**VALID_FIXTURES["Claim"], "confidence": 1.1},
    "Episode": {**VALID_FIXTURES["Episode"], "cost_usd": -0.001},
    "Run": {**VALID_FIXTURES["Run"], "reasoning_effort": "max"},
    "Span": {**VALID_FIXTURES["Span"], "start_time": "not-a-date"},
}


def _matches_type(instance: Any, type_name: str) -> bool:
    if type_name == "null":
        return instance is None
    if type_name == "object":
        return isinstance(instance, dict)
    if type_name == "array":
        return isinstance(instance, list)
    if type_name == "string":
        return isinstance(instance, str)
    if type_name == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if type_name == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if type_name == "boolean":
        return isinstance(instance, bool)
    return False


def _validate(
    instance: Any, schema: dict[str, Any], path: str, errors: list[str]
) -> None:
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: must equal {schema['const']!r}")
        return
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: value is not in enum")
        return
    expected_type = schema.get("type")
    if expected_type:
        choices = expected_type if isinstance(expected_type, list) else [expected_type]
        if not any(_matches_type(instance, choice) for choice in choices):
            errors.append(f"{path}: expected type {choices}")
            return
    if instance is None:
        return
    if isinstance(instance, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in instance:
                errors.append(f"{path}.{key}: required property is missing")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in instance:
                if key not in properties:
                    errors.append(f"{path}.{key}: additional property is not allowed")
        for key, value in instance.items():
            if key in properties:
                _validate(value, properties[key], f"{path}.{key}", errors)
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: needs at least {schema['minItems']} items")
        if schema.get("uniqueItems") and len(
            {_json_identity(item) for item in instance}
        ) != len(instance):
            errors.append(f"{path}: items must be unique")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(instance):
                _validate(item, item_schema, f"{path}[{index}]", errors)
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: string is too short")
        if "pattern" in schema and re.fullmatch(schema["pattern"], instance) is None:
            errors.append(f"{path}: string does not match pattern")
        if schema.get("format") == "date-time" and not _is_datetime(instance):
            errors.append(f"{path}: invalid date-time")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: value is below minimum")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: value is above maximum")


def _json_identity(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _is_datetime(value: str) -> bool:
    if (
        re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
            value,
        )
        is None
    ):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def validate_instance(schema_name: str, instance: Any) -> list[str]:
    try:
        schema = SCHEMAS[schema_name]
    except KeyError as exc:
        raise ValueError(f"unknown ContextLab schema: {schema_name}") from exc
    errors: list[str] = []
    _validate(instance, schema, "$", errors)
    return errors


def build_contract_artifacts(root: Path | None = None) -> dict[str, int]:
    root = (root or repository_root()).resolve()
    schema_dir = root / "evaluation" / "v2" / "schemas"
    fixture_dir = root / "evaluation" / "v2" / "fixtures" / "contracts"
    schema_dir.mkdir(parents=True, exist_ok=True)
    fixture_dir.mkdir(parents=True, exist_ok=True)
    for name, schema in SCHEMAS.items():
        (schema_dir / f"{name}.schema.json").write_text(
            json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (fixture_dir / f"{name}.valid.json").write_text(
            json.dumps(VALID_FIXTURES[name], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (fixture_dir / f"{name}.invalid.json").write_text(
            json.dumps(INVALID_FIXTURES[name], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return {
        "schemas": len(SCHEMAS),
        "valid_fixtures": len(SCHEMAS),
        "invalid_fixtures": len(SCHEMAS),
    }


def verify_contract_artifacts(root: Path | None = None) -> dict[str, int]:
    root = (root or repository_root()).resolve()
    schema_dir = root / "evaluation" / "v2" / "schemas"
    fixture_dir = root / "evaluation" / "v2" / "fixtures" / "contracts"
    valid_count = 0
    invalid_count = 0
    for name, expected_schema in SCHEMAS.items():
        schema = json.loads(
            (schema_dir / f"{name}.schema.json").read_text(encoding="utf-8")
        )
        if schema != expected_schema or schema.get("$schema") != DRAFT:
            raise ValueError(f"{name}: saved schema differs from the contract")
        valid = json.loads(
            (fixture_dir / f"{name}.valid.json").read_text(encoding="utf-8")
        )
        invalid = json.loads(
            (fixture_dir / f"{name}.invalid.json").read_text(encoding="utf-8")
        )
        valid_errors = validate_instance(name, valid)
        invalid_errors = validate_instance(name, invalid)
        if valid_errors:
            raise ValueError(f"{name}: valid fixture failed: {valid_errors}")
        if not invalid_errors:
            raise ValueError(f"{name}: invalid fixture passed")
        valid_count += 1
        invalid_count += 1
    return {
        "schemas": len(SCHEMAS),
        "valid_fixtures_passed": valid_count,
        "invalid_fixtures_failed": invalid_count,
    }
