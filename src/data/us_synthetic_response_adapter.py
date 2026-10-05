"""Offline, memory-only ust21150 adapter; never authenticated broker evidence.

Observation commits independently before the one optional economic application.
An application refusal can therefore leave a valid observation, but no new fill.
All schemas and generation bindings must be explicitly prepared by the caller.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.data.us_cumulative_execution import (
    ObservationBatch, UsCumulativeObservationStore, normalize_cumulative_observations,
)
from src.data.us_synthetic_bridge import (
    SyntheticBridgeAttribution, SyntheticBridgeResult, SyntheticCumulativeGenerationBridge,
)
from src.data.us_synthetic_ledger import _memory_only


@dataclass(frozen=True)
class SyntheticResponseBinding:
    ord_no: str
    order_uid: str
    generation_id: str


@dataclass(frozen=True)
class SyntheticResponseResult:
    state: str
    observation: ObservationBatch
    application: SyntheticBridgeResult | None = None
    operational_ingestion_allowed: bool = False


class SyntheticUsResponseAdapter:
    """Accept complete dated responses, explicit bindings and synthetic proof.

    No client calls, paths, migration, finality, closure or strategy mutation.
    A proof covers the entire unapplied delta, not the last polling increment.
    """

    def __init__(self, db, *, account_id, market, symbol, lifecycle_id):
        _memory_only(db)
        self.db = db
        self.bridge = SyntheticCumulativeGenerationBridge(
            db, account_id=account_id, market=market, symbol=symbol, lifecycle_id=lifecycle_id,
        )
        self.observations = UsCumulativeObservationStore(db)

    def ingest(self, data, *, query_order_date, observed_at_utc,
               bindings: tuple[SyntheticResponseBinding, ...],
               attribution: SyntheticBridgeAttribution | None = None):
        _memory_only(self.db)
        if self.db.in_transaction:
            raise ValueError("Response adapter requires its own transaction boundaries")
        account, market, symbol, lifecycle = self.bridge.registry.scope
        items = normalize_cumulative_observations(
            data, account_id=account, query_order_date=query_order_date,
            observed_at_utc=observed_at_utc,
        )
        if not isinstance(bindings, tuple) or any(
            not isinstance(binding, SyntheticResponseBinding) for binding in bindings
        ):
            raise ValueError("Explicit tuple of response bindings is required")
        by_number = {binding.ord_no: binding for binding in bindings}
        if (len(by_number) != len(bindings)
                or len({binding.order_uid for binding in bindings}) != len(bindings)
                or set(by_number) != {item.ord_no for item in items}):
            raise ValueError("Exactly one explicit binding per response order is required")
        # Validate the complete response's scope/bindings before observation writes.
        with self.bridge.registry._transaction():
            for item in items:
                binding = by_number[item.ord_no]
                rows = self.db.execute(
                    "SELECT i.account_id,i.market,i.broker_order_date,i.ord_no,i.symbol,i.side,"
                    "i.identity_status,p.account_id,p.ord_no,p.symbol,p.side,p.step,p.lifecycle_id,"
                    "o.generation_id,o.side,g.step "
                    "FROM order_identities i JOIN pending_orders p ON p.order_uid=i.order_uid "
                    "JOIN synthetic_generation_orders o ON o.order_uid=i.order_uid "
                    "JOIN synthetic_generations g ON g.generation_id=o.generation_id "
                    "WHERE i.order_uid=?", (binding.order_uid,),
                ).fetchall()
                expected = (account, market, query_order_date, item.ord_no, symbol, item.side,
                            "confirmed", account, item.ord_no, symbol, item.side)
                if (item.symbol != symbol or len(rows) != 1 or rows[0][:11] != expected
                        or rows[0][12:15] != (lifecycle, binding.generation_id, item.side)
                        or type(rows[0][11]) is not int or rows[0][11] < 1
                        or rows[0][11] != rows[0][15]):
                    raise ValueError("Response order identity/generation scope mismatch")
                self.bridge.registry._validate_existing_generation(binding.generation_id)
        if attribution is not None:
            if not isinstance(attribution, SyntheticBridgeAttribution):
                raise ValueError("One explicit synthetic full-delta attribution is required")
            matching = [binding for binding in bindings if binding.order_uid == attribution.order_uid
                        and binding.generation_id == attribution.generation_id]
            if len(matching) != 1:
                raise ValueError("Attribution must match an explicit response binding")
        observed = self.observations.observe(items)
        if observed.conflicts:
            return SyntheticResponseResult("HELD", observed)
        if attribution is None:
            return SyntheticResponseResult("OBSERVED_ONLY", observed)
        applied = self.bridge.apply(attribution)
        return SyntheticResponseResult(applied.state, observed, applied)
