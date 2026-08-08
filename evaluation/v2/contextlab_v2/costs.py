"""Race-safe paid-call audit ledger and optional diagnostic quote."""

from __future__ import annotations

import fcntl
import json
from decimal import Decimal, ROUND_UP
from pathlib import Path
from typing import Any

from .credentials import redact
from .provider import INPUT_USD_PER_MILLION, OUTPUT_USD_PER_MILLION
from .baseline import repository_root


HARD_CAP_USD = Decimal("15.00")
WARNING_USD = Decimal("12.00")
MILLION = Decimal("1000000")


class CostCapError(RuntimeError):
    """A paid-call ledger operation is invalid or unsafe."""


def canonical_ledger_path(root: Path | None = None) -> Path:
    return (root or repository_root()).resolve() / "results/v2/cost/paid_calls.jsonl"


def estimate_cost(input_tokens: int, output_tokens: int, *, calls: int = 1) -> Decimal:
    if input_tokens < 0 or output_tokens < 0 or calls < 1:
        raise CostCapError("token counts must be non-negative and calls must be positive")
    per_call = (
        Decimal(input_tokens) * Decimal(INPUT_USD_PER_MILLION)
        + Decimal(output_tokens) * Decimal(OUTPUT_USD_PER_MILLION)
    ) / MILLION
    return (per_call * calls).quantize(Decimal("0.000001"), rounding=ROUND_UP)


class CostLedger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    @staticmethod
    def _exposure(rows: list[dict[str, Any]]) -> Decimal:
        reservations: dict[str, Decimal] = {}
        actual = Decimal("0")
        for row in rows:
            event = row.get("event")
            reservation_id = str(row.get("reservation_id", ""))
            if event == "reserve":
                reservations[reservation_id] = Decimal(str(row["estimated_usd"]))
            elif event in {"acknowledge", "enrich", "failure"}:
                continue
            elif event == "settle":
                reservations.pop(reservation_id, None)
                actual += Decimal(str(row["actual_usd"]))
            elif event == "cancel":
                reservations.pop(reservation_id, None)
            else:
                raise CostCapError("cost ledger contains an unknown event")
        return actual + sum(reservations.values(), Decimal("0"))

    @staticmethod
    def _active_reservations(rows: list[dict[str, Any]]) -> dict[str, Decimal]:
        reservations: dict[str, Decimal] = {}
        for row in rows:
            reservation_id = str(row.get("reservation_id", ""))
            event = row.get("event")
            if event == "reserve":
                reservations[reservation_id] = Decimal(str(row["estimated_usd"]))
            elif event in {"acknowledge", "enrich", "failure"}:
                continue
            elif event in {"settle", "cancel"}:
                reservations.pop(reservation_id, None)
            else:
                raise CostCapError("cost ledger contains an unknown event")
        return reservations

    @staticmethod
    def _rows(handle: Any) -> list[dict[str, Any]]:
        handle.seek(0)
        rows: list[dict[str, Any]] = []
        for line_number, line in enumerate(handle, start=1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise CostCapError(f"invalid cost ledger row {line_number}") from exc
        return rows

    def quote(self, *, input_tokens: int, output_tokens: int, calls: int = 1) -> dict[str, Any]:
        estimate = estimate_cost(input_tokens, output_tokens, calls=calls)
        with self.path.open("r", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            exposure = self._exposure(self._rows(handle))
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        projected = exposure + estimate
        return {
            "current_exposure_usd": str(exposure),
            "worst_case_estimate_usd": str(estimate),
            "projected_exposure_usd": str(projected),
            "informational_warning": projected >= WARNING_USD,
            "projected_within_external_key_limit": projected <= HARD_CAP_USD,
            "external_key_limit_usd": str(HARD_CAP_USD),
        }

    def summary(self) -> dict[str, Any]:
        """Return measured call counts and actual cost from the append-only ledger."""
        with self.path.open("r", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            rows = self._rows(handle)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        settlements = [row for row in rows if row.get("event") == "settle"]
        return {
            "event_count": len(rows),
            "reserved_calls": sum(row.get("event") == "reserve" for row in rows),
            "settled_calls": len(settlements),
            "provider_acknowledged_calls": sum(
                row.get("event") == "acknowledge" for row in rows
            ),
            "recorded_failures": sum(row.get("event") == "failure" for row in rows),
            "active_reservations": len(self._active_reservations(rows)),
            "actual_usd": str(
                sum(
                    (Decimal(str(row["actual_usd"])) for row in settlements),
                    Decimal("0"),
                )
            ),
        }

    def reserve(
        self,
        reservation_id: str,
        *,
        input_tokens: int,
        output_tokens: int,
        calls: int = 1,
    ) -> dict[str, Any]:
        estimate = estimate_cost(input_tokens, output_tokens, calls=calls)
        with self.path.open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            rows = self._rows(handle)
            if any(row.get("reservation_id") == reservation_id for row in rows):
                raise CostCapError(f"duplicate reservation ID: {reservation_id}")
            exposure = self._exposure(rows)
            projected = exposure + estimate
            event = {
                "schema_version": "contextlab.cost-event.v1",
                "event": "reserve",
                "reservation_id": reservation_id,
                "input_token_limit": input_tokens,
                "output_token_limit": output_tokens,
                "call_count": calls,
                "estimated_usd": str(estimate),
            }
            handle.seek(0, 2)
            handle.write(json.dumps(event, sort_keys=True) + "\n")
            handle.flush()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return {
            **event,
            "informational_warning": projected >= WARNING_USD,
            "projected_exposure_usd": str(projected),
            "projected_within_external_key_limit": projected <= HARD_CAP_USD,
        }

    def settle(self, reservation_id: str, *, actual_usd: Decimal, metadata: dict[str, Any]) -> None:
        if not actual_usd.is_finite() or actual_usd < 0:
            raise CostCapError("actual cost cannot be negative")
        with self.path.open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            rows = self._rows(handle)
            reservations = self._active_reservations(rows)
            if reservation_id not in reservations:
                raise CostCapError(f"unknown reservation: {reservation_id}")
            if actual_usd > reservations[reservation_id]:
                raise CostCapError("actual cost exceeds its worst-case reservation")
            acknowledgments = [
                row
                for row in rows
                if row.get("event") == "acknowledge"
                and row.get("reservation_id") == reservation_id
            ]
            if len(acknowledgments) != 1:
                raise CostCapError("settlement requires exactly one provider acknowledgment")
            if acknowledgments[0].get("metadata", {}).get("request_id") != metadata.get(
                "request_id"
            ):
                raise CostCapError("settlement request ID differs from its acknowledgment")
            event = {
                "schema_version": "contextlab.cost-event.v1",
                "event": "settle",
                "reservation_id": reservation_id,
                "actual_usd": str(actual_usd),
                "metadata": redact(metadata),
            }
            handle.seek(0, 2)
            handle.write(json.dumps(event, sort_keys=True) + "\n")
            handle.flush()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def acknowledge(self, reservation_id: str, *, metadata: dict[str, Any]) -> None:
        """Persist the provider response ID before optional metadata enrichment."""
        with self.path.open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            rows = self._rows(handle)
            if reservation_id not in self._active_reservations(rows):
                raise CostCapError(f"unknown reservation: {reservation_id}")
            if any(
                row.get("event") == "acknowledge"
                and row.get("reservation_id") == reservation_id
                for row in rows
            ):
                raise CostCapError(f"duplicate provider acknowledgment: {reservation_id}")
            request_id = metadata.get("request_id")
            if not isinstance(request_id, str) or not request_id:
                raise CostCapError("provider acknowledgment requires a request ID")
            event = {
                "schema_version": "contextlab.cost-event.v1",
                "event": "acknowledge",
                "reservation_id": reservation_id,
                "metadata": redact(metadata),
            }
            handle.seek(0, 2)
            handle.write(json.dumps(event, sort_keys=True) + "\n")
            handle.flush()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def fail(
        self,
        reservation_id: str,
        *,
        stage: str,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record a redacted post-send failure while preserving cost exposure."""
        with self.path.open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            rows = self._rows(handle)
            if not any(row.get("reservation_id") == reservation_id for row in rows):
                raise CostCapError(f"unknown reservation: {reservation_id}")
            if any(
                row.get("event") == "failure"
                and row.get("reservation_id") == reservation_id
                for row in rows
            ):
                raise CostCapError(f"duplicate failure record: {reservation_id}")
            event = {
                "schema_version": "contextlab.cost-event.v1",
                "event": "failure",
                "reservation_id": reservation_id,
                "stage": stage,
                "reason": str(redact(reason)),
                "metadata": redact(metadata or {}),
            }
            handle.seek(0, 2)
            handle.write(json.dumps(event, sort_keys=True) + "\n")
            handle.flush()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def cancel(self, reservation_id: str, *, reason: str) -> None:
        with self.path.open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            rows = self._rows(handle)
            if reservation_id not in self._active_reservations(rows):
                raise CostCapError(f"unknown reservation: {reservation_id}")
            event = {
                "schema_version": "contextlab.cost-event.v1",
                "event": "cancel",
                "reservation_id": reservation_id,
                "reason": str(redact(reason)),
            }
            handle.seek(0, 2)
            handle.write(json.dumps(event, sort_keys=True) + "\n")
            handle.flush()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def enrich(self, reservation_id: str, *, metadata: dict[str, Any]) -> None:
        """Append delayed provider timing and token metadata to a settled call."""
        with self.path.open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            rows = self._rows(handle)
            settlements = [
                row
                for row in rows
                if row.get("event") == "settle"
                and row.get("reservation_id") == reservation_id
            ]
            if len(settlements) != 1:
                raise CostCapError(f"cannot enrich unsettled reservation: {reservation_id}")
            acknowledgments = [
                row
                for row in rows
                if row.get("event") == "acknowledge"
                and row.get("reservation_id") == reservation_id
            ]
            if len(acknowledgments) != 1:
                raise CostCapError("enrichment requires exactly one provider acknowledgment")
            if metadata.get("request_id") != acknowledgments[0].get("metadata", {}).get(
                "request_id"
            ):
                raise CostCapError("enrichment request ID differs from its acknowledgment")
            try:
                enriched_cost = Decimal(str(metadata.get("actual_usd")))
                settled_cost = Decimal(str(settlements[0].get("actual_usd")))
            except Exception as exc:
                raise CostCapError("enrichment cost is invalid") from exc
            if not enriched_cost.is_finite() or enriched_cost != settled_cost:
                raise CostCapError("enrichment cost differs from its settlement")
            safe_metadata = redact(metadata)
            enrichments = [
                row
                for row in rows
                if row.get("event") == "enrich"
                and row.get("reservation_id") == reservation_id
            ]
            if enrichments:
                if len(enrichments) == 1 and enrichments[0].get("metadata") == safe_metadata:
                    return
                raise CostCapError(f"conflicting provider enrichment: {reservation_id}")
            event = {
                "schema_version": "contextlab.cost-event.v1",
                "event": "enrich",
                "reservation_id": reservation_id,
                "metadata": safe_metadata,
            }
            handle.seek(0, 2)
            handle.write(json.dumps(event, sort_keys=True) + "\n")
            handle.flush()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
