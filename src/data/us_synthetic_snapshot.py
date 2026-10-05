"""One read snapshot across synthetic tranches; never authorize trading.

Only the explicit lifecycle is projected. Unresolved same-symbol orders,
including other generations, hold the result. No repair or schema fallback.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from fractions import Fraction

from src.data.order_identity import validate_order_date
from src.data.us_cumulative_execution import CONTRACT, _decimal
from src.data.us_synthetic_ledger import _memory_only, _replay_tranche_cost
from src.data.us_synthetic_strategy import (
    SyntheticStrategyProjection, _projection_parameters, _project_tranche_state,
)


@dataclass(frozen=True)
class SyntheticTrancheSettings:
    step: int
    drop_pct: str | None
    profit_pct: str
    commission_rate: str


@dataclass(frozen=True)
class SyntheticLifecycleSnapshot:
    account_id: str
    market: str
    symbol: str
    lifecycle_id: str
    state: str
    blocked_reasons: tuple[str, ...]
    tranches: tuple[SyntheticStrategyProjection, ...] = ()
    active_step: int | None = None
    quantity: int | None = None
    remaining_gross_cost: Fraction | None = None
    realized_gross_profit: Fraction | None = None
    snapshot_token: str | None = None
    operational_trading_allowed: bool = False


def read_synthetic_lifecycle_snapshot(
    db: sqlite3.Connection, *, account_id: str, market: str, symbol: str,
    lifecycle_id: str, settings: tuple[SyntheticTrancheSettings, ...],
) -> SyntheticLifecycleSnapshot:
    """Read all configured tranches in one transaction and select max held step.

    The content token compares synthetic results only; it is not a durable
    production revision, broker freshness proof or permission to place orders.
    Unknown settings/schema or corrupt ownership refuse; unresolved evidence
    returns HELD with no usable projection or targets.
    """
    if not isinstance(settings, tuple) or not settings:
        raise ValueError("Explicit immutable tranche settings are required")
    parameters = {}
    for item in settings:
        if not isinstance(item, SyntheticTrancheSettings) or item.step in parameters:
            raise ValueError("Tranche settings must be valid and unique")
        parameters[item.step] = _projection_parameters(
            account_id, market, symbol, item.step, lifecycle_id,
            item.drop_pct, item.profit_pct, item.commission_rate,
        )
    if sorted(parameters) != list(range(1, len(parameters) + 1)):
        raise ValueError("Settings must cover contiguous tranches starting at one")
    _memory_only(db)
    if db.in_transaction:
        raise ValueError("Lifecycle snapshot requires its own read transaction")
    db.execute("BEGIN")
    try:
        return _read(db, account_id, market, symbol, lifecycle_id, parameters)
    finally:
        db.rollback()


def _read(db, account, market, symbol, lifecycle, parameters):
    if db.execute("SELECT 1 FROM order_identities i LEFT JOIN pending_orders p "
                  "ON p.order_uid=i.order_uid WHERE i.account_id=? AND i.symbol=? "
                  "AND p.order_uid IS NULL LIMIT 1", (account, symbol)).fetchone():
        raise ValueError("Scoped identity has no pending ownership record")
    rows = db.execute(
        "SELECT p.order_uid,p.account_id,p.symbol,p.side,p.ord_no,p.step,p.lifecycle_id,"
        "p.requested_qty,p.filled_qty,p.status,i.account_id,i.market,i.symbol,i.side,"
        "i.ord_no,i.identity_status,i.broker_order_date FROM pending_orders p "
        "LEFT JOIN order_identities i ON i.order_uid=p.order_uid "
        "WHERE (p.account_id=? AND p.symbol=?) OR (i.account_id=? AND i.symbol=?)",
        (account, symbol, account, symbol),
    ).fetchall()
    reasons, current_uids, checkpoints = set(), set(), []
    for row in rows:
        uid, owner, ticker, side, number, step, generation, requested, filled, status = row[:10]
        if (tuple(row[10:15]) != (account, market, symbol, side, number)
                or (owner, ticker) != (account, symbol) or side not in ("BUY", "SELL")
                or not isinstance(generation, str) or not generation.strip()
                or type(step) is not int or step < 1):
            raise ValueError("Lifecycle order ownership is malformed or conflicting")
        if status not in ("open", "filled", "cancelled", "awaiting_execution_history"):
            raise ValueError("Unknown lifecycle pending status")
        if row[15] != "confirmed":
            reasons.add("unconfirmed_order_identity")
        else:
            validate_order_date(row[16])
        request = _decimal(str(requested), "snapshot requested quantity")
        qty = _decimal(str(filled), "snapshot filled quantity")
        if (request <= 0 or qty > request or any(v != v.to_integral_value() or v > 2**53
                                                for v in (request, qty))):
            raise ValueError("Invalid lifecycle order quantities")
        if status in ("open", "awaiting_execution_history"):
            reasons.add("unresolved_order")
        if status == "filled" and qty != request:
            raise ValueError("Completed order quantity disagrees with request")
        if db.execute("SELECT 1 FROM us_cumulative_observation_conflicts WHERE order_uid=?", (uid,)).fetchone():
            reasons.add("observation_conflict")
        if generation != lifecycle:
            continue
        current_uids.add(uid)
        if step not in parameters:
            raise ValueError("Recorded tranche has no configured settings")
        applied = db.execute("SELECT quantity,amount,contract FROM synthetic_us_applied WHERE order_uid=?", (uid,)).fetchone()
        if applied is None:
            if qty != 0:
                reasons.add("applied_baseline_missing")
            applied_q, applied_a = Fraction(0), Fraction(0)
        else:
            if applied[2] != CONTRACT:
                raise ValueError("Applied snapshot contract mismatch")
            applied_q = Fraction(_decimal(applied[0], "snapshot applied quantity"))
            applied_a = Fraction(_decimal(applied[1], "snapshot applied amount", max_length=130))
            if applied_q != Fraction(qty):
                raise ValueError("Applied checkpoint disagrees with pending quantity")
        history = db.execute("SELECT quantity,gross_amount FROM synthetic_us_fills WHERE order_uid=?", (uid,)).fetchall()
        sum_q = sum((Fraction(_decimal(q, "snapshot fill quantity")) for q, _ in history), Fraction(0))
        sum_a = sum((Fraction(_decimal(a, "snapshot fill amount", max_length=130)) for _, a in history), Fraction(0))
        if (sum_q, sum_a) != (applied_q, applied_a):
            raise ValueError("Snapshot checkpoint disagrees with fill history")
        observation = db.execute("SELECT account_id,order_date,ord_no,quantity,amount,contract "
                                 "FROM us_cumulative_observations WHERE order_uid=?", (uid,)).fetchone()
        if observation is not None:
            if observation[:3] != (account, row[16], number) or observation[5] != CONTRACT:
                raise ValueError("Snapshot observation identity mismatch")
            observed_q = Fraction(_decimal(observation[3], "snapshot observed quantity"))
            observed_a = Fraction(_decimal(observation[4], "snapshot observed amount", max_length=130))
            if (observed_q, observed_a) != (applied_q, applied_a):
                reasons.add("unapplied_observation")
        elif applied_q:
            reasons.add("observation_missing")
        checkpoints.append((uid, step, status, str(applied_q), str(applied_a)))
    recorded = db.execute("SELECT order_uid,step FROM synthetic_us_fills "
                          "WHERE account_id=? AND symbol=? AND lifecycle_id=?",
                          (account, symbol, lifecycle)).fetchall()
    if any(uid not in current_uids or step not in parameters for uid, step in recorded):
        raise ValueError("Snapshot contains orphan or unconfigured fills")
    if reasons:
        return SyntheticLifecycleSnapshot(account, market, symbol, lifecycle, "HELD", tuple(sorted(reasons)))
    projected = []
    for step in sorted(parameters):
        state, _ = _replay_tranche_cost(db, account, symbol, step, lifecycle)
        projected.append(_project_tranche_state(state, account, market, symbol, step, lifecycle, parameters[step]))
    owners = {item.buy_order_uid for item in projected if item.buy_order_uid is not None}
    if len(owners) != sum(item.buy_order_uid is not None for item in projected):
        raise ValueError("Buy owner is reused across tranches")
    active = max((item.step for item in projected if item.quantity > 0), default=None)
    total_q = sum(item.quantity for item in projected)
    total_cost = sum((item.remaining_gross_cost for item in projected), Fraction(0))
    profit = sum((item.realized_gross_profit for item in projected), Fraction(0))
    # Include settings and order checkpoints as well as all projected economics.
    payload = {"scope": [account, market, symbol, lifecycle], "active": active,
               "parameters": [(step, [str(v) for v in parameters[step]]) for step in sorted(parameters)],
               "checkpoints": sorted(checkpoints),
               "tranches": [item.__dict__ for item in projected]}
    token = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return SyntheticLifecycleSnapshot(account, market, symbol, lifecycle, "SYNTHETIC_VALIDATED", (),
                                      tuple(projected), active, total_q, total_cost, profit, token)
