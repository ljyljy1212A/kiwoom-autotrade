"""Injected observation-only boundary; no Engine wiring or storage implementation.

Caller-supplied identity snapshots are checked for consistency, not authenticated
here. No synthetic journal identity is promoted. A sink must commit the whole
validated cycle atomically and preserve diagnostic conflicts without economics.
Receipt validation is an interface contract, not proof of backend behavior.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Protocol

from src.data.order_identity import OrderIdentity, validate_order_date
from src.data.us_cumulative_execution import CumulativeObservation, _decimal, _timestamp, normalize_cumulative_observations


@dataclass(frozen=True)
class UsTrackedObservationOrder:
    identity: OrderIdentity
    requested_quantity: str


@dataclass(frozen=True)
class UsObservationResponse:
    query_order_date: str
    observed_at_utc: str
    body: dict


@dataclass(frozen=True)
class UsObservationBinding:
    order_uid: str
    order_date: str
    ord_no: str
    symbol: str
    side: str
    requested_quantity: str


@dataclass(frozen=True)
class ValidatedUsObservationCycle:
    account_id: str
    market: str
    bindings: tuple[UsObservationBinding, ...]
    observations: tuple[CumulativeObservation, ...]
    query_contexts: tuple[tuple[str, str], ...]
    cycle_token: str
    required_conflicts: tuple[tuple[str, str], ...]
    economic_ingestion_allowed: bool = False


@dataclass(frozen=True)
class UsObservationReceipt:
    cycle_token: str
    state: str
    committed: bool
    conflicts: tuple[tuple[str, str], ...] = ()
    economic_writes: bool = False


class UsObservationSink(Protocol):
    def record_cycle(self, cycle: ValidatedUsObservationCycle) -> UsObservationReceipt:
        """Own one transaction for the whole cycle; never write economic state."""
        ...


@dataclass(frozen=True)
class UsObservationDecision:
    state: str
    reasons: tuple[str, ...]
    allow_sync_continue: bool = False
    persistence_confirmed: bool = False
    cycle_token: str | None = None
    conflicts: tuple[tuple[str, str], ...] = ()
    economic_ingestion_allowed: bool = False
    operational_trading_allowed: bool = False


def _cycle(account, market, orders, responses):
    if not isinstance(orders, tuple) or not orders or not isinstance(responses, tuple) or not responses:
        raise ValueError("Explicit immutable identity and response tuples are required")
    bindings, by_key, uids = [], {}, set()
    for tracked in orders:
        if not isinstance(tracked, UsTrackedObservationOrder) or not isinstance(tracked.identity, OrderIdentity):
            raise ValueError("Existing ledger identity snapshot is required")
        identity = tracked.identity
        quantity = _decimal(tracked.requested_quantity, "requested quantity")
        if (identity.account_id != account or identity.market != market or identity.identity_status != "confirmed"
                or not isinstance(identity.order_uid, str) or not identity.order_uid.strip() or len(identity.order_uid) > 128
                or not isinstance(identity.ord_no, str) or not re.fullmatch(r"[0-9]{9}", identity.ord_no)
                or not isinstance(identity.symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,11}", identity.symbol)
                or identity.side not in ("BUY", "SELL") or quantity <= 0
                or quantity != quantity.to_integral_value() or quantity > 2 ** 53):
            raise ValueError("Unconfirmed or invalid US observation identity")
        date = validate_order_date(identity.broker_order_date)
        _timestamp(identity.submitted_at_utc)
        key = (date, identity.ord_no)
        if key in by_key or identity.order_uid in uids:
            raise ValueError("Duplicate or ambiguous observation identity")
        binding = UsObservationBinding(identity.order_uid, date, identity.ord_no, identity.symbol,
                                       identity.side, tracked.requested_quantity)
        bindings.append(binding)
        by_key[key] = binding
        uids.add(identity.order_uid)
    observations, contexts, dates, conflicts = [], [], set(), []
    for response in responses:
        if not isinstance(response, UsObservationResponse) or response.query_order_date in dates:
            raise ValueError("Exactly one complete response per query date is required")
        items = normalize_cumulative_observations(response.body, account_id=account,
                                                 query_order_date=response.query_order_date,
                                                 observed_at_utc=response.observed_at_utc)
        dates.add(response.query_order_date)
        contexts.append((response.query_order_date, _timestamp(response.observed_at_utc).isoformat()))
        for item in items:
            binding = by_key.get((item.query_order_date, item.ord_no))
            if binding is None or (binding.symbol, binding.side) != (item.symbol, item.side):
                raise ValueError("Response row has no exact tracked identity")
            if item.quantity > _decimal(binding.requested_quantity, "request"):
                conflicts.append((binding.order_uid, "quantity_exceeds_requested"))
            observations.append(item)
    if dates != {binding.order_date for binding in bindings}:
        raise ValueError("Response date coverage does not match tracked orders")
    bindings.sort(key=lambda item: item.order_uid)
    observations.sort(key=lambda item: (item.query_order_date, item.ord_no))
    contexts.sort()
    payload = {"scope": [account, market], "bindings": [item.__dict__ for item in bindings],
               "contexts": contexts,
               "observations": [{key: str(value) for key, value in item.__dict__.items()} for item in observations]}
    token = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return ValidatedUsObservationCycle(account, market, tuple(bindings), tuple(observations), tuple(contexts),
                                       token, tuple(sorted(conflicts)))


class UsObservationAdapter:
    """Disabled by default. Active failures never permit sync continuation.

    OBSERVED is a storage receipt only. DISABLED preserves the caller's existing
    path and makes no observation claim. No retries, repairs or alternate sink.
    """

    def __init__(self, *, account_id, market, enabled=False, sink: UsObservationSink | None = None):
        if account_id != "us_mock" or market != "US" or type(enabled) is not bool:
            raise ValueError("Explicit US mock observation scope and boolean activation are required")
        self.account_id, self.market, self.enabled, self.sink = account_id, market, enabled, sink

    def observe_cycle(self, *, orders, responses):
        if not self.enabled:
            return UsObservationDecision("DISABLED", (), allow_sync_continue=True)
        if self.sink is None:
            return UsObservationDecision("INCOMPLETE", ("observation_sink_missing",))
        try:
            cycle = _cycle(self.account_id, self.market, orders, responses)
        except Exception:
            return UsObservationDecision("INCOMPLETE", ("observation_input_invalid",))
        try:
            receipt = self.sink.record_cycle(cycle)
        except Exception:
            # Never expose raw exceptions, response bodies or credentials.
            return UsObservationDecision("INCOMPLETE", ("observation_sink_failed",), cycle_token=cycle.cycle_token)
        if (not isinstance(receipt, UsObservationReceipt) or receipt.cycle_token != cycle.cycle_token
                or type(receipt.committed) is not bool or receipt.economic_writes is not False
                or not isinstance(receipt.conflicts, tuple)
                or any(not isinstance(item, tuple) or len(item) != 2
                       or not isinstance(item[0], str)
                       or item[0] not in {binding.order_uid for binding in cycle.bindings}
                       or not isinstance(item[1], str) or not item[1].strip() for item in receipt.conflicts)
                or len(set(receipt.conflicts)) != len(receipt.conflicts)):
            return UsObservationDecision("INCOMPLETE", ("observation_receipt_invalid",), cycle_token=cycle.cycle_token)
        if receipt.state == "OBSERVED" and receipt.committed and not receipt.conflicts and not cycle.required_conflicts:
            return UsObservationDecision("OBSERVED", (), True, True, cycle.cycle_token)
        if (receipt.state == "CONFLICT" and receipt.committed and receipt.conflicts
                and set(cycle.required_conflicts).issubset(receipt.conflicts)):
            return UsObservationDecision("CONFLICT", ("observation_conflict",), persistence_confirmed=True,
                                         cycle_token=cycle.cycle_token, conflicts=receipt.conflicts)
        return UsObservationDecision("INCOMPLETE", ("observation_not_committed_or_inconsistent",), cycle_token=cycle.cycle_token)
