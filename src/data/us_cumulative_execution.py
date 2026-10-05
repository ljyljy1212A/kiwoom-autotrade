"""US mock cumulative observations; never authorize or write economic fills.

The contract is the user-supplied direct support reply for ust21150/ust21510:
cntr_qty is cumulative quantity and cntr_uv is cumulative weighted average.
This first adapter accepts complete, explicitly dated ust21150 responses only.
Displayed-price multiplication is a calculated amount, not a settlement amount.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext

from src.data.order_identity import validate_order_date

CONTRACT = "user-supplied-direct-support-20261004-cumulative-average-v1"
_SCHEMA = (
    """CREATE TABLE us_cumulative_observations (
        order_uid TEXT PRIMARY KEY REFERENCES order_identities(order_uid),
        account_id TEXT NOT NULL CHECK(account_id='us_mock'),
        order_date TEXT NOT NULL, ord_no TEXT NOT NULL,
        quantity TEXT NOT NULL, average_price TEXT NOT NULL, amount TEXT NOT NULL,
        observed_at_utc TEXT NOT NULL, contract TEXT NOT NULL
    )""",
    """CREATE TABLE us_cumulative_observation_audit (
        observation_uid TEXT PRIMARY KEY, batch_uid TEXT NOT NULL,
        order_uid TEXT NOT NULL REFERENCES order_identities(order_uid),
        observed_at_utc TEXT NOT NULL, outcome TEXT NOT NULL,
        observation_json TEXT NOT NULL
    )""",
    """CREATE TABLE us_cumulative_observation_conflicts (
        order_uid TEXT PRIMARY KEY REFERENCES order_identities(order_uid),
        reason TEXT NOT NULL, first_seen_at_utc TEXT NOT NULL,
        last_seen_at_utc TEXT NOT NULL, observation_json TEXT NOT NULL
    )""",
)


def _decimal(value: str, name: str, *, max_length: int = 64) -> Decimal:
    if (not isinstance(value, str) or len(value) > max_length
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value)):
        raise ValueError(f"{name} must be a nonnegative plain decimal string")
    return Decimal(value)


def _text(value: Decimal) -> str:
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Observation time must be explicit UTC")
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
        raise ValueError("Observation time must be explicit UTC")
    return stamp


def _calculate(quantity: Decimal, average: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 300
        return quantity * average


def _subtract(current: Decimal, previous: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 300
        return current - previous


@dataclass(frozen=True)
class CumulativeObservation:
    account_id: str
    query_order_date: str
    ord_no: str
    symbol: str
    side: str
    quantity: Decimal
    average_price: Decimal
    amount: Decimal
    observed_at_utc: str
    raw_json: str
    source_api: str = "ust21150"
    contract: str = CONTRACT


@dataclass(frozen=True)
class ObservationDelta:
    order_uid: str
    quantity: Decimal
    amount: Decimal
    outcome: str
    economic_ingestion_allowed: bool = False
    execution_date_status: str = "unresolved"


@dataclass(frozen=True)
class ObservationBatch:
    state: str
    deltas: tuple[ObservationDelta, ...]
    conflicts: tuple[tuple[str, str], ...]
    economic_ingestion_allowed: bool = False


def normalize_cumulative_observations(
    data: dict, *, account_id: str, query_order_date: str, observed_at_utc: str,
) -> tuple[CumulativeObservation, ...]:
    """Validate a complete dated response without inventing an execution date.

    Account and pagination labels are supplied by the owning client. They do
    not authenticate the response or guarantee a consistent broker snapshot.
    Conflicting rows for one order reject the complete batch; never sort them
    into an invented sequence of individual executions.
    """
    if account_id != "us_mock":
        raise ValueError("Only the explicit us_mock scope is supported")
    date = validate_order_date(query_order_date)
    stamp = _timestamp(observed_at_utc).isoformat()
    if (not isinstance(data, dict) or data.get("_execution_pages_complete") is not True
            or data.get("_query_order_date") != date
            or type(data.get("return_code")) not in (int, str)
            or data["return_code"] not in (0, "0")
            or not isinstance(data.get("result_list"), list)):
        raise ValueError("Complete successful dated ust21150 response is required")
    observations: dict[str, CumulativeObservation] = {}
    for row in data["result_list"]:
        if not isinstance(row, dict):
            raise ValueError("Invalid ust21150 order row")
        number = row.get("ord_no")
        symbol = row.get("stk_cd")
        side_name = row.get("slby_tp_nm")
        side = {"매수": "BUY", "매도": "SELL"}.get(side_name) if isinstance(side_name, str) else None
        if (not isinstance(number, str) or not re.fullmatch(r"[0-9]{9}", number)
                or not isinstance(symbol, str) or not symbol.strip()
                or symbol != symbol.strip() or side is None or row.get("crnc_code") != "USD"):
            raise ValueError("Order number, ticker, side and USD currency are required")
        if row.get("ord_dt") not in (None, "", date):
            raise ValueError("Response order date conflicts with explicit query date")
        quantity = _decimal(row.get("cntr_qty"), "cntr_qty")
        average = _decimal(row.get("cntr_uv"), "cntr_uv")
        if quantity > 0 and average <= 0:
            raise ValueError("Positive cumulative quantity requires a positive average")
        if quantity == 0 and average != 0:
            raise ValueError("Zero cumulative quantity requires a zero average")
        raw = {key: row[key] for key in (
            "ord_no", "stk_cd", "slby_tp_nm", "crnc_code", "cntr_qty", "cntr_uv",
            "ord_dt", "cntr_time", "ord_stat_nm",
        ) if key in row}
        encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True, allow_nan=False)
        item = CumulativeObservation(account_id, date, number, symbol, side,
                                     quantity, average, _calculate(quantity, average), stamp, encoded)
        previous = observations.get(number)
        if previous is not None and previous != item:
            raise ValueError("Conflicting rows for one order in the complete response")
        observations[number] = item
    return tuple(observations.values())


def initialize_cumulative_observation_schema(db: sqlite3.Connection) -> None:
    """Explicit candidate-schema preparation, never called by worker startup.

    Refuse existing targets and caller transactions. Do not migrate the ledger,
    change user_version, open a path, repair a schema, or replace existing data.
    """
    if db.in_transaction or db.execute("PRAGMA user_version").fetchone()[0] != 2:
        raise ValueError("An explicitly prepared idle v2 identity connection is required")
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("SELECT order_uid,account_id,market,ord_no,symbol,side,"
                   "broker_order_date,identity_status FROM order_identities LIMIT 0")
        db.execute("SELECT order_uid,account_id,ord_no,symbol,side,requested_qty FROM pending_orders LIMIT 0")
        for statement in _SCHEMA:
            db.execute(statement)
        db.commit()
    except Exception:
        db.rollback()
        raise


class UsCumulativeObservationStore:
    """Persist observation checkpoints only, never an applied-fill baseline.

    A later ledger integration must maintain its own atomic *applied* baseline.
    Advancing these observations must never cause that integration to skip an
    unapplied fill. All response candidates are checked before any checkpoint
    advances. A conflict latches and blocks the complete observation batch.
    """

    def __init__(self, db: sqlite3.Connection):
        if db.execute("PRAGMA user_version").fetchone()[0] != 2:
            raise ValueError("Explicit identity schema preparation is required")
        # Inspection only; no automatic initialization or connection PRAGMAs.
        for table in ("us_cumulative_observations", "us_cumulative_observation_audit",
                      "us_cumulative_observation_conflicts"):
            db.execute(f"SELECT * FROM {table} LIMIT 0")
        self.db = db

    def observe(self, observations: tuple[CumulativeObservation, ...]) -> ObservationBatch:
        if self.db.in_transaction:
            raise ValueError("Observation requires its own transaction")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            result = self._observe_in_transaction(observations)
            self.db.commit()
            return result
        except Exception:
            self.db.rollback()
            raise

    def _observe_in_transaction(self, observations, *, blocking_conflicts=(), required_conflicts=(), audit_context=None):
        """Internal whole-cycle operation; the caller owns commit and rollback."""
        if not self.db.in_transaction:
            raise ValueError("Observation requires an existing owned transaction")
        candidates = []
        conflicts = list(blocking_conflicts)
        seen = set()
        for item in observations:
            # Revalidate public dataclass instances; do not trust construction.
            raw = json.loads(item.raw_json)
            verified = normalize_cumulative_observations({
                "return_code": 0, "_execution_pages_complete": True,
                "_query_order_date": item.query_order_date, "result_list": [raw],
            }, account_id=item.account_id, query_order_date=item.query_order_date,
                observed_at_utc=item.observed_at_utc)[0]
            if verified != item:
                raise ValueError("Cumulative observation contract or values were altered")
            key = (item.query_order_date, item.ord_no)
            if key in seen:
                raise ValueError("Duplicate order in observation batch")
            seen.add(key)
            row = self.db.execute(
                "SELECT i.order_uid,i.symbol,i.side,p.requested_qty FROM order_identities i "
                "JOIN pending_orders p ON p.order_uid=i.order_uid "
                "AND p.account_id=i.account_id AND p.ord_no=i.ord_no "
                "AND p.symbol=i.symbol AND p.side=i.side "
                "WHERE i.account_id=? AND i.market='US' AND i.broker_order_date=? "
                "AND i.ord_no=? AND i.identity_status='confirmed'",
                (item.account_id, item.query_order_date, item.ord_no),
            ).fetchall()
            if len(row) != 1 or row[0][1:3] != (item.symbol, item.side):
                # sqlite3.Row slicing also returns a tuple.
                raise ValueError("Confirmed account/date/order/symbol/side identity is required")
            uid, _, _, requested = row[0]
            requested = _decimal(str(requested), "stored requested quantity")
            if requested <= 0:
                raise ValueError("Stored requested quantity must be positive")
            previous = self.db.execute(
                "SELECT account_id,order_date,ord_no,quantity,average_price,amount,"
                "observed_at_utc,contract FROM us_cumulative_observations WHERE order_uid=?",
                (uid,),
            ).fetchone()
            previous_qty, previous_amount = Decimal(0), Decimal(0)
            reason = None
            if previous is not None:
                if tuple(previous[:3]) != (item.account_id, item.query_order_date, item.ord_no) or previous[7] != CONTRACT:
                    raise ValueError("Stored checkpoint identity or contract is inconsistent")
                previous_qty = _decimal(previous[3], "stored quantity")
                previous_average = _decimal(previous[4], "stored average")
                previous_amount = _decimal(previous[5], "stored amount", max_length=130)
                if (previous_amount != _calculate(previous_qty, previous_average)
                        or previous_qty > requested or (previous_qty > 0 and previous_average <= 0)
                        or (previous_qty == 0 and previous_average != 0)):
                    raise ValueError("Stored checkpoint economics are inconsistent")
                if _timestamp(item.observed_at_utc) < _timestamp(previous[6]):
                    reason = "out_of_order_observation"
                elif (_timestamp(item.observed_at_utc) == _timestamp(previous[6])
                      and (item.quantity != previous_qty or item.average_price != previous_average)):
                    reason = "conflicting_observation_at_same_time"
            delta_qty = _subtract(item.quantity, previous_qty)
            delta_amount = _subtract(item.amount, previous_amount)
            if self.db.execute("SELECT 1 FROM us_cumulative_observation_conflicts WHERE order_uid=?", (uid,)).fetchone():
                reason = "durable_conflict_already_present"
            elif item.quantity > requested:
                reason = "quantity_exceeds_requested"
            elif delta_qty < 0:
                reason = "cumulative_quantity_decreased"
            elif delta_qty == 0 and delta_amount != 0:
                reason = "amount_changed_without_quantity"
            elif delta_qty > 0 and delta_amount <= 0:
                reason = "nonpositive_incremental_amount"
            if reason:
                conflicts.append((uid, reason))
            conflicts.extend(pair for pair in required_conflicts if pair[0] == uid)
            candidates.append((uid, item, delta_qty, delta_amount, reason))
        batch_uid = uuid.uuid4().hex
        deltas = []
        for uid, item, delta_qty, delta_amount, reason in candidates:
            outcome = "conflict" if reason else ("batch_blocked" if conflicts else
                                                   "duplicate" if delta_qty == 0 else "observed")
            self.db.execute("INSERT INTO us_cumulative_observation_audit VALUES (?,?,?,?,?,?)",
                            (uuid.uuid4().hex, batch_uid, uid, item.observed_at_utc, outcome,
                             json.dumps({"contract": item.contract, "source_api": item.source_api,
                                         "query_order_date": item.query_order_date,
                                         "raw": json.loads(item.raw_json),
                                         **({"cycle_context": audit_context} if audit_context is not None else {})},
                                        ensure_ascii=False, sort_keys=True)))
            if reason:
                existing = self.db.execute(
                    "SELECT last_seen_at_utc FROM us_cumulative_observation_conflicts WHERE order_uid=?",
                    (uid,),
                ).fetchone()
                last_seen = item.observed_at_utc
                if existing and _timestamp(existing[0]) > _timestamp(last_seen):
                    last_seen = existing[0]
                self.db.execute(
                    "INSERT INTO us_cumulative_observation_conflicts VALUES (?,?,?,?,?) "
                    "ON CONFLICT(order_uid) DO UPDATE SET last_seen_at_utc=excluded.last_seen_at_utc",
                    (uid, reason, item.observed_at_utc, last_seen, item.raw_json),
                )
            if not conflicts:
                self.db.execute(
                    "INSERT INTO us_cumulative_observations VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(order_uid) DO UPDATE SET quantity=excluded.quantity,"
                    "average_price=excluded.average_price,amount=excluded.amount,"
                    "observed_at_utc=excluded.observed_at_utc",
                    (uid, item.account_id, item.query_order_date, item.ord_no, _text(item.quantity),
                     _text(item.average_price), _text(item.amount), item.observed_at_utc, item.contract),
                )
                deltas.append(ObservationDelta(uid, delta_qty, delta_amount, outcome))
        return ObservationBatch("conflict" if conflicts else "observed", tuple(deltas), tuple(dict.fromkeys(conflicts)))
