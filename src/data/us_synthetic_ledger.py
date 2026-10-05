"""In-memory US mock ledger prototype; no Engine or operational DB integration.

Date evidence is caller-supplied synthetic evidence covering the entire delta.
It is never authenticated broker evidence or permission to ingest live fills.
Amounts are calculated gross amounts, not settlement amounts or fees.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction

from src.data.order_identity import validate_order_date
from src.data.us_cumulative_execution import (
    CONTRACT, _calculate, _decimal, _subtract, _text, _timestamp,
)


def _memory_only(db: sqlite3.Connection) -> None:
    if any(row[2] for row in db.execute("PRAGMA database_list")):
        raise ValueError("Synthetic ledger requires exclusively in-memory databases")


def initialize_synthetic_ledger(db: sqlite3.Connection) -> None:
    """Explicit preparation; existing tables or a caller transaction refuse."""
    _memory_only(db)
    if db.in_transaction:
        raise ValueError("Synthetic schema requires its own transaction")
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("SELECT order_uid,step,filled_qty,status FROM pending_orders LIMIT 0")
        db.execute("SELECT lifecycle_id FROM pending_orders LIMIT 0")
        db.execute("SELECT order_uid,quantity,amount,contract FROM us_cumulative_observations LIMIT 0")
        db.execute("SELECT order_uid FROM us_cumulative_observation_conflicts LIMIT 0")
        db.execute("""CREATE TABLE synthetic_us_applied (
            order_uid TEXT PRIMARY KEY, quantity TEXT NOT NULL, amount TEXT NOT NULL,
            contract TEXT NOT NULL)""")
        db.execute("""CREATE TABLE synthetic_us_fills (
            order_uid TEXT NOT NULL, cumulative_quantity TEXT NOT NULL,
            account_id TEXT NOT NULL CHECK(account_id='us_mock'),
            symbol TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
            step INTEGER NOT NULL, execution_date TEXT NOT NULL,
            quantity TEXT NOT NULL, gross_amount TEXT NOT NULL,
            evidence_json TEXT NOT NULL, lifecycle_id TEXT NOT NULL,
            PRIMARY KEY(order_uid,cumulative_quantity))""")
        db.execute("""CREATE TABLE synthetic_us_cost_allocations (
            order_uid TEXT NOT NULL, cumulative_quantity TEXT NOT NULL,
            ordinal INTEGER NOT NULL, state_json TEXT NOT NULL,
            PRIMARY KEY(order_uid,cumulative_quantity))""")
        db.commit()
    except Exception:
        db.rollback()
        raise


@dataclass(frozen=True)
class SyntheticDateEvidence:
    """Explicit full-delta attribution; order/query/observation dates are insufficient."""
    order_uid: str
    execution_date: str
    previous_quantity: str
    previous_amount: str
    cumulative_quantity: str
    cumulative_amount: str
    evidence_id: str
    kind: str = "synthetic-full-delta"
    # Caller-supplied order within one execution date and tranche lifecycle.
    # This must cover the full delta; polling order is never such evidence.
    execution_sequence: int | None = None


@dataclass(frozen=True)
class SyntheticAppliedDelta:
    quantity: Decimal
    gross_amount: Decimal
    duplicate: bool
    operational_ingestion_allowed: bool = False
    allocated_gross_cost: Fraction | None = None
    realized_gross_profit: Fraction | None = None


@dataclass(frozen=True)
class SyntheticTrancheCost:
    quantity: int = 0
    remaining_gross_cost: Fraction = Fraction(0)
    realized_gross_profit: Fraction = Fraction(0)
    buy_order_uid: str | None = None
    fills: int = 0
    operational_ingestion_allowed: bool = False
    cumulative_buy_quantity: int = 0
    cumulative_buy_gross_amount: Fraction = Fraction(0)

    @property
    def average_gross_cost(self) -> Fraction | None:
        return self.remaining_gross_cost / self.quantity if self.quantity else None

    @property
    def entry_reference_price(self) -> Fraction | None:
        """Owning buy order's cumulative average; sales never change its basis."""
        if not self.quantity or not self.cumulative_buy_quantity:
            return None
        return self.cumulative_buy_gross_amount / self.cumulative_buy_quantity


def _cost_step(state, uid, side, quantity, amount):
    q, a = Fraction(quantity), Fraction(amount)
    if q <= 0 or q.denominator != 1 or a <= 0:
        raise ValueError("Invalid synthetic cost economics")
    q = int(q)
    allocated, profit = Fraction(0), Fraction(0)
    buy_q, buy_amount = state.cumulative_buy_quantity, state.cumulative_buy_gross_amount
    if side == "BUY":
        if state.buy_order_uid is not None and state.buy_order_uid != uid:
            raise ValueError("Different buy order cannot enter the same tranche lifecycle")
        next_q, next_cost, owner = state.quantity + q, state.remaining_gross_cost + a, uid
        buy_q, buy_amount = buy_q + q, buy_amount + a
    elif side == "SELL":
        if q > state.quantity:
            raise ValueError("Sell delta exceeds owned tranche quantity")
        allocated = state.remaining_gross_cost * q / state.quantity
        profit = a - allocated
        next_q, next_cost, owner = state.quantity - q, state.remaining_gross_cost - allocated, state.buy_order_uid
    else:
        raise ValueError("Invalid synthetic cost direction")
    result = SyntheticTrancheCost(
        quantity=next_q, remaining_gross_cost=next_cost,
        realized_gross_profit=state.realized_gross_profit + profit,
        buy_order_uid=owner, fills=state.fills + 1,
        cumulative_buy_quantity=buy_q, cumulative_buy_gross_amount=buy_amount,
    )
    return result, allocated, profit


def _cost_record(state, allocated, profit):
    def pair(value):
        return [str(value.numerator), str(value.denominator)]
    return {"policy": "one-buy-order-per-tranche-average-reference-v2", "quantity": state.quantity,
            "remaining_gross_cost": pair(state.remaining_gross_cost),
            "realized_gross_profit": pair(state.realized_gross_profit),
            "buy_order_uid": state.buy_order_uid, "fills": state.fills,
            "cumulative_buy_quantity": state.cumulative_buy_quantity,
            "cumulative_buy_gross_amount": pair(state.cumulative_buy_gross_amount),
            "entry_reference_price": (pair(state.entry_reference_price)
                                      if state.entry_reference_price is not None else None),
            "allocated_gross_cost": pair(allocated), "delta_gross_profit": pair(profit)}


def _cost_order(previous, side, date, sequence):
    validate_order_date(date)
    for old_side, old_date, old_sequence in previous:
        if old_date > date:
            raise ValueError("Later dated tranche fills block historical cost application")
        if old_date == date and (side == "SELL" or old_side == "SELL"):
            if (type(sequence) is not int or type(old_sequence) is not int
                    or sequence < 1 or old_sequence < 1 or old_sequence >= sequence):
                raise ValueError("Same-day cost application requires an execution sequence")


def _replay_tranche_cost(db, account, symbol, step, lifecycle):
    rows = db.execute(
        "SELECT f.order_uid,f.side,f.quantity,f.gross_amount,f.execution_date,f.evidence_json,"
        "a.ordinal,a.state_json FROM synthetic_us_fills f "
        "LEFT JOIN synthetic_us_cost_allocations a ON a.order_uid=f.order_uid "
        "AND a.cumulative_quantity=f.cumulative_quantity "
        "WHERE f.account_id=? AND f.symbol=? AND f.step=? AND f.lifecycle_id=? ORDER BY a.ordinal",
        (account, symbol, step, lifecycle),
    ).fetchall()
    if db.execute("SELECT 1 FROM synthetic_us_cost_allocations a LEFT JOIN synthetic_us_fills f "
                  "ON a.order_uid=f.order_uid AND a.cumulative_quantity=f.cumulative_quantity "
                  "WHERE f.order_uid IS NULL LIMIT 1").fetchone():
        raise ValueError("Orphan synthetic cost allocation")
    state, chronology, owners = SyntheticTrancheCost(), [], {}
    for uid, side, quantity, amount, date, raw, ordinal, record in rows:
        identity = db.execute(
            "SELECT i.account_id,i.market,i.symbol,i.side,i.identity_status,p.step,p.lifecycle_id,"
            "p.account_id,p.symbol,p.side,p.ord_no,i.ord_no,p.filled_qty,p.requested_qty "
            "FROM order_identities i JOIN pending_orders p ON p.order_uid=i.order_uid WHERE i.order_uid=?",
            (uid,),
        ).fetchone()
        if (identity is None or tuple(identity[:10]) !=
                (account, "US", symbol, side, "confirmed", step, lifecycle, account, symbol, side)
                or identity[10] != identity[11]):
            raise ValueError("Synthetic cost owner identity mismatch")
        if db.execute("SELECT 1 FROM us_cumulative_observation_conflicts WHERE order_uid=?", (uid,)).fetchone():
            raise ValueError("Synthetic cost owner conflict blocks replay")
        proof = json.loads(raw)
        if (not isinstance(proof, dict) or proof.get("execution_date") != date
                or proof.get("order_uid") != uid or proof.get("kind") != "synthetic-full-delta"):
            raise ValueError("Synthetic cost date evidence mismatch")
        sequence = proof.get("execution_sequence")
        _cost_order(chronology, side, date, sequence)
        q = _decimal(quantity, "cost quantity")
        a = _decimal(amount, "cost amount", max_length=130)
        state, allocated, profit = _cost_step(state, uid, side, q, a)
        if type(ordinal) is not int or ordinal != state.fills or json.loads(record) != _cost_record(state, allocated, profit):
            raise ValueError("Stored synthetic cost allocation disagrees with replay")
        chronology.append((side, date, sequence))
        totals = owners.setdefault(uid, [Fraction(0), Fraction(0), identity[12], identity[13]])
        totals[0] += Fraction(q)
        totals[1] += Fraction(a)
    for uid, (q, a, filled, requested) in owners.items():
        baseline = db.execute("SELECT quantity,amount,contract FROM synthetic_us_applied WHERE order_uid=?", (uid,)).fetchone()
        if (baseline is None or baseline[2] != CONTRACT
                or Fraction(_decimal(baseline[0], "cost baseline quantity")) != q
                or Fraction(_decimal(baseline[1], "cost baseline amount", max_length=130)) != a
                or Fraction(_decimal(str(filled), "cost pending quantity")) != q
                or q > Fraction(_decimal(str(requested), "cost requested quantity"))):
            raise ValueError("Synthetic cost owner baseline mismatch")
    return state, chronology


def restore_synthetic_tranche_cost(db, symbol, step, lifecycle):
    """Read-only exact replay for US mock fixtures; never initialize missing state."""
    _memory_only(db)
    if db.in_transaction:
        raise ValueError("Cost restore requires its own read snapshot")
    if not isinstance(symbol, str) or not symbol or type(step) is not int or step < 1 or not lifecycle:
        raise ValueError("Explicit tranche scope is required")
    db.execute("BEGIN")
    try:
        state, _ = _replay_tranche_cost(db, "us_mock", symbol, step, lifecycle)
        return state
    finally:
        db.rollback()


def _tranche_available_quantity(db, account, symbol, step, lifecycle, date, sequence):
    """Validate synthetic owners and count only this exact lifecycle/tranche."""
    rows = db.execute(
        "SELECT f.order_uid,f.side,f.quantity,f.gross_amount,i.account_id,i.market,"
        "i.symbol,i.side,i.identity_status,p.step,p.lifecycle_id,p.filled_qty,"
        "p.account_id,p.symbol,p.side,p.ord_no,i.ord_no,p.requested_qty,"
        "f.execution_date,f.evidence_json "
        "FROM synthetic_us_fills f LEFT JOIN order_identities i ON i.order_uid=f.order_uid "
        "LEFT JOIN pending_orders p ON p.order_uid=f.order_uid "
        "WHERE f.account_id=? AND f.symbol=? AND f.step=? AND f.lifecycle_id=?",
        (account, symbol, step, lifecycle),
    ).fetchall()
    with localcontext() as context:
        context.prec = 300
        available = Decimal(0)
        owners = {}
        same_day_sequences = set()
        for row in rows:
            uid, side, quantity, amount = row[:4]
            if (tuple(row[4:11]) != (account, "US", symbol, side, "confirmed", step, lifecycle)
                    or tuple(row[12:15]) != (account, symbol, side) or row[15] != row[16]
                    or side not in ("BUY", "SELL")):
                raise ValueError("Synthetic tranche owner identity mismatch")
            q = _decimal(quantity, "owned quantity")
            a = _decimal(amount, "owned amount", max_length=130)
            if q <= 0 or a <= 0 or q != q.to_integral_value():
                raise ValueError("Invalid tranche ownership economics")
            stored_date = validate_order_date(row[18])
            proof = json.loads(row[19])
            if (not isinstance(proof, dict) or proof.get("execution_date") != stored_date
                    or proof.get("order_uid") != uid or proof.get("kind") != "synthetic-full-delta"):
                raise ValueError("Stored tranche date evidence mismatch")
            if stored_date > date:
                raise ValueError("Later dated tranche fills cannot fund a historical sell")
            if stored_date == date:
                stored_sequence = proof.get("execution_sequence")
                if (type(stored_sequence) is not int or stored_sequence < 1
                        or sequence is None or stored_sequence >= sequence
                        or stored_sequence in same_day_sequences):
                    raise ValueError("Same-day sell requires an unambiguous execution sequence")
                same_day_sequences.add(stored_sequence)
            available += q if side == "BUY" else -q
            totals = owners.setdefault(uid, [Decimal(0), Decimal(0), row[11], row[17]])
            totals[0] += q
            totals[1] += a
        for uid, (q, a, filled, requested) in owners.items():
            applied = db.execute("SELECT quantity,amount,contract FROM synthetic_us_applied WHERE order_uid=?", (uid,)).fetchone()
            if (applied is None or applied[2] != CONTRACT
                    or (_decimal(applied[0], "owner baseline quantity"),
                        _decimal(applied[1], "owner baseline amount", max_length=130)) != (q, a)
                    or _decimal(str(filled), "owner pending quantity") != q
                    or q > _decimal(str(requested), "owner requested quantity")):
                raise ValueError("Synthetic owner baseline disagrees with fills")
            if db.execute("SELECT 1 FROM us_cumulative_observation_conflicts WHERE order_uid=?", (uid,)).fetchone():
                raise ValueError("Synthetic tranche owner conflict blocks sell")
        if available < 0:
            raise ValueError("Synthetic tranche ownership is negative")
        return available


def apply_synthetic_cumulative(
    db: sqlite3.Connection, order_uid: str, evidence: SyntheticDateEvidence,
) -> SyntheticAppliedDelta:
    """Apply the latest stored observation against the *applied* baseline atomically.

    Only fresh, zero-filled orders can establish a new baseline. Existing fills
    require their matching applied baseline; no migration or baseline guessing.
    Corrections and ambiguous dates refuse without altering economic state.
    """
    _memory_only(db)
    if db.in_transaction:
        raise ValueError("Synthetic apply requires its own transaction")
    if not isinstance(evidence, SyntheticDateEvidence):
        raise ValueError("Explicit synthetic date evidence is required")
    date = validate_order_date(evidence.execution_date)
    if (evidence.execution_sequence is not None
            and (type(evidence.execution_sequence) is not int or evidence.execution_sequence < 1)):
        raise ValueError("Synthetic execution sequence must be a positive integer")
    if (evidence.order_uid != order_uid or evidence.kind != "synthetic-full-delta"
            or not isinstance(evidence.evidence_id, str) or not evidence.evidence_id.strip()):
        raise ValueError("Synthetic evidence identity is invalid")
    db.execute("BEGIN IMMEDIATE")
    try:
        identity = db.execute(
            "SELECT i.account_id,i.market,i.ord_no,i.symbol,i.side,i.broker_order_date,"
            "i.identity_status,p.step,p.requested_qty,p.filled_qty,p.status,p.lifecycle_id "
            "FROM order_identities i JOIN pending_orders p ON p.order_uid=i.order_uid "
            "AND p.account_id=i.account_id AND p.ord_no=i.ord_no "
            "AND p.symbol=i.symbol AND p.side=i.side WHERE i.order_uid=?", (order_uid,),
        ).fetchall()
        if len(identity) != 1:
            raise ValueError("Exactly one matching pending identity is required")
        account, market, number, symbol, side, order_date, state, step, requested, filled, status, lifecycle = identity[0]
        if account != "us_mock" or market != "US" or state != "confirmed" or side not in ("BUY", "SELL"):
            raise ValueError("Confirmed US mock identity is required")
        validate_order_date(order_date)
        if type(step) is not int or step < 1 or not symbol:
            raise ValueError("Explicit valid tranche is required")
        if not isinstance(lifecycle, str) or not lifecycle.strip():
            raise ValueError("Explicit synthetic lifecycle is required")
        if status not in ("open", "filled", "awaiting_execution_history", "cancelled"):
            raise ValueError("Unsupported pending state")
        obs = db.execute(
            "SELECT account_id,order_date,ord_no,quantity,average_price,amount,"
            "observed_at_utc,contract FROM us_cumulative_observations WHERE order_uid=?", (order_uid,),
        ).fetchone()
        if obs is None or tuple(obs[:3]) != (account, order_date, number) or obs[7] != CONTRACT:
            raise ValueError("Matching cumulative observation is required")
        _timestamp(obs[6])
        q, average, amount = (_decimal(obs[3], "quantity"), _decimal(obs[4], "average"),
                              _decimal(obs[5], "amount", max_length=130))
        if amount != _calculate(q, average) or (q > 0 and average <= 0) or (q == 0 and average != 0):
            raise ValueError("Invalid observation economics")
        if db.execute("SELECT 1 FROM us_cumulative_observation_conflicts WHERE order_uid=?", (order_uid,)).fetchone():
            raise ValueError("Durable observation conflict blocks apply")
        requested_q = _decimal(str(requested), "requested quantity")
        filled_q = _decimal(str(filled), "filled quantity")
        # The existing pending fixture stores REAL counters. Restrict this
        # prototype to exactly representable whole-share counters.
        if any(value != value.to_integral_value() or value > 2**53
               for value in (q, requested_q, filled_q)):
            raise ValueError("Synthetic pending counters require exact whole shares")
        if requested_q <= 0 or q > requested_q or filled_q > requested_q:
            raise ValueError("Quantity exceeds request")
        applied = db.execute(
            "SELECT quantity,amount,contract FROM synthetic_us_applied WHERE order_uid=?", (order_uid,),
        ).fetchone()
        previous_q, previous_amount = Decimal(0), Decimal(0)
        if applied is not None:
            previous_q = _decimal(applied[0], "applied quantity")
            previous_amount = _decimal(applied[1], "applied amount", max_length=130)
            if applied[2] != CONTRACT:
                raise ValueError("Applied contract mismatch")
        elif filled_q != 0:
            raise ValueError("Existing fills require an explicit applied baseline")
        history = db.execute(
            "SELECT account_id,symbol,side,step,quantity,gross_amount,lifecycle_id FROM synthetic_us_fills WHERE order_uid=?",
            (order_uid,),
        ).fetchall()
        with localcontext() as context:
            context.prec = 300
            sum_q, sum_amount = Decimal(0), Decimal(0)
            for row in history:
                if tuple(row[:4]) != (account, symbol, side, step) or row[6] != lifecycle:
                    raise ValueError("Stored tranche identity changed")
                row_q = _decimal(row[4], "fill quantity")
                row_amount = _decimal(row[5], "fill amount", max_length=130)
                if row_q <= 0 or row_amount <= 0:
                    raise ValueError("Invalid stored fill economics")
                sum_q += row_q
                sum_amount += row_amount
        if filled_q != previous_q or (sum_q, sum_amount) != (previous_q, previous_amount):
            raise ValueError("Applied baseline disagrees with persisted fills")
        expected = (previous_q, previous_amount, q, amount)
        supplied = tuple(_decimal(value, "evidence amount or quantity", max_length=130) for value in (
            evidence.previous_quantity, evidence.previous_amount,
            evidence.cumulative_quantity, evidence.cumulative_amount,
        ))
        dq, da = _subtract(q, previous_q), _subtract(amount, previous_amount)
        cost_state, chronology = _replay_tranche_cost(db, account, symbol, step, lifecycle)
        if dq == 0 and da == 0:
            # A replay can carry the evidence for the already persisted delta.
            if supplied != expected:
                stored = db.execute(
                    "SELECT evidence_json FROM synthetic_us_fills WHERE order_uid=? AND cumulative_quantity=?",
                    (order_uid, _text(q)),
                ).fetchone()
                if stored is None or json.loads(stored[0]) != evidence.__dict__:
                    raise ValueError("Duplicate evidence disagrees with recorded attribution")
            db.rollback()
            return SyntheticAppliedDelta(dq, da, True)
        if supplied != expected or dq <= 0 or da <= 0:
            raise ValueError("Full delta evidence or monotonic economics required")
        if side == "SELL" and dq > _tranche_available_quantity(
            db, account, symbol, step, lifecycle, date, evidence.execution_sequence,
        ):
            raise ValueError("Sell delta exceeds owned tranche quantity")
        _cost_order(chronology, side, date, evidence.execution_sequence)
        next_cost, allocated, profit = _cost_step(cost_state, order_uid, side, dq, da)
        db.execute("INSERT INTO synthetic_us_fills VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            order_uid, _text(q), account, symbol, side, step, date, _text(dq), _text(da),
            json.dumps(evidence.__dict__, sort_keys=True), lifecycle,
        ))
        db.execute("INSERT INTO synthetic_us_applied VALUES (?,?,?,?) "
                   "ON CONFLICT(order_uid) DO UPDATE SET quantity=excluded.quantity,amount=excluded.amount",
                   (order_uid, _text(q), _text(amount), CONTRACT))
        db.execute("INSERT INTO synthetic_us_cost_allocations VALUES (?,?,?,?)",
                   (order_uid, _text(q), next_cost.fills,
                    json.dumps(_cost_record(next_cost, allocated, profit), sort_keys=True)))
        next_status = "filled" if q == requested_q else status
        updated = db.execute("UPDATE pending_orders SET filled_qty=?,status=? WHERE order_uid=? AND filled_qty=?",
                             (_text(q), next_status, order_uid, filled))
        if updated.rowcount != 1:
            raise ValueError("Pending state changed")
        db.commit()
        return SyntheticAppliedDelta(dq, da, False, False, allocated, profit)
    except Exception:
        db.rollback()
        raise
