"""Memory-only cumulative observation bridge to explicit synthetic generations.

No Engine integration, real finality, broker date inference or legacy adoption.
The generation store is the sole economic destination of this bridge.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

from src.data.order_identity import validate_order_date
from src.data.us_cumulative_execution import CONTRACT, _calculate, _decimal, _subtract, _text, _timestamp
from src.data.us_synthetic_generations import SyntheticGenerationDelta, SyntheticGenerationRegistry, _identifier
from src.data.us_synthetic_ledger import _memory_only


@dataclass(frozen=True)
class SyntheticBridgeAttribution:
    order_uid: str
    generation_id: str
    previous_quantity: str
    previous_amount: str
    cumulative_quantity: str
    cumulative_amount: str
    execution_date: str
    execution_sequence: int
    observed_at_utc: str
    evidence_id: str
    kind: str = "synthetic-bridge-full-delta"


@dataclass(frozen=True)
class SyntheticBridgeResult:
    state: str
    quantity: Decimal = Decimal(0)
    gross_amount: Decimal = Decimal(0)
    operational_ingestion_allowed: bool = False


def _event_id(scope, uid, generation_id, quantity, amount):
    endpoint = [scope, CONTRACT, uid, generation_id, _text(quantity), _text(amount)]
    return "bridge-" + hashlib.sha256(json.dumps(endpoint, separators=(",", ":")).encode()).hexdigest()


def initialize_synthetic_bridge(db: sqlite3.Connection):
    """Explicit fixture schema preparation, without adoption of existing fills."""
    _memory_only(db)
    if db.in_transaction:
        raise ValueError("Bridge initialization requires its own transaction")
    db.execute("BEGIN IMMEDIATE")
    try:
        for query in (
            "SELECT order_uid,account_id,market,ord_no,symbol,side,broker_order_date,identity_status FROM order_identities LIMIT 0",
            "SELECT order_uid,account_id,ord_no,symbol,side,step,lifecycle_id,requested_qty,filled_qty,status FROM pending_orders LIMIT 0",
            "SELECT order_uid,account_id,order_date,ord_no,quantity,average_price,amount,observed_at_utc,contract FROM us_cumulative_observations LIMIT 0",
            "SELECT order_uid FROM us_cumulative_observation_conflicts LIMIT 0",
            "SELECT revision,policy FROM synthetic_generation_scope LIMIT 0",
            "SELECT generation_id,side,finality_json FROM synthetic_generation_orders LIMIT 0",
        ):
            db.execute(query)
        db.execute("CREATE TABLE synthetic_bridge_applied (order_uid TEXT PRIMARY KEY,generation_id TEXT NOT NULL,"
                   "quantity TEXT NOT NULL,amount TEXT NOT NULL,contract TEXT NOT NULL,last_event_id TEXT NOT NULL)")
        db.execute("CREATE TABLE synthetic_bridge_audit (event_id TEXT PRIMARY KEY,order_uid TEXT NOT NULL,"
                   "generation_id TEXT NOT NULL,attribution_json TEXT NOT NULL,observation_json TEXT NOT NULL)")
        db.execute("CREATE TABLE synthetic_bridge_holds (order_uid TEXT PRIMARY KEY,generation_id TEXT NOT NULL,"
                   "reason TEXT NOT NULL,attribution_json TEXT NOT NULL)")
        db.commit()
    except Exception:
        db.rollback()
        raise


class SyntheticCumulativeGenerationBridge:
    def __init__(self, db, *, account_id, market, symbol, lifecycle_id):
        self.db = db
        self.registry = SyntheticGenerationRegistry(db, account_id=account_id, market=market,
                                                    symbol=symbol, lifecycle_id=lifecycle_id)

    def _hold(self, proof, reason):
        changed = self.db.execute("INSERT OR IGNORE INTO synthetic_generation_holds VALUES (?,?)",
                                  (proof.generation_id, reason)).rowcount
        self.db.execute("INSERT OR IGNORE INTO synthetic_bridge_holds VALUES (?,?,?,?)",
                        (proof.order_uid, proof.generation_id, reason, json.dumps(proof.__dict__, sort_keys=True)))
        if changed:
            self.registry._advance()
        return SyntheticBridgeResult("HELD")

    def apply(self, proof: SyntheticBridgeAttribution):
        if not isinstance(proof, SyntheticBridgeAttribution) or proof.kind != "synthetic-bridge-full-delta":
            raise ValueError("Explicit full-delta synthetic attribution is required")
        for value in (proof.order_uid, proof.generation_id, proof.evidence_id):
            _identifier(value)
        validate_order_date(proof.execution_date)
        _timestamp(proof.observed_at_utc)
        if type(proof.execution_sequence) is not int or proof.execution_sequence < 1:
            raise ValueError("Explicit synthetic execution sequence is required")
        supplied = tuple(_decimal(value, "attribution endpoint", max_length=130) for value in (
            proof.previous_quantity, proof.previous_amount, proof.cumulative_quantity, proof.cumulative_amount,
        ))
        with self.registry._transaction(True):
            # Refuse missing schema even on duplicate/hold paths.
            self.db.execute("SELECT order_uid FROM synthetic_bridge_holds LIMIT 0")
            account, market, symbol, lifecycle = self.registry.scope
            rows = self.db.execute(
                "SELECT i.account_id,i.market,i.ord_no,i.symbol,i.side,i.broker_order_date,i.identity_status,"
                "p.account_id,p.ord_no,p.symbol,p.side,p.step,p.lifecycle_id,p.requested_qty,p.filled_qty,p.status "
                "FROM order_identities i JOIN pending_orders p ON p.order_uid=i.order_uid WHERE i.order_uid=?",
                (proof.order_uid,),
            ).fetchall()
            if len(rows) != 1:
                raise ValueError("Exactly one confirmed pending identity is required")
            row = rows[0]
            if (row[:2] != (account, market) or row[3] != symbol or row[6] != "confirmed"
                    or row[7:11] != (account, row[2], symbol, row[4]) or row[4] not in ("BUY", "SELL")
                    or type(row[11]) is not int or row[11] < 1 or row[12] != lifecycle
                    or row[15] not in ("open", "filled", "cancelled", "awaiting_execution_history")):
                raise ValueError("Bridge identity/lifecycle mismatch")
            validate_order_date(row[5])
            generation = self.registry._row(proof.generation_id)
            binding = self.db.execute("SELECT generation_id,side FROM synthetic_generation_orders WHERE order_uid=?",
                                      (proof.order_uid,)).fetchone()
            if binding != (proof.generation_id, row[4]) or generation[0] != row[11]:
                raise ValueError("Exact order/step/generation binding is required")
            self.registry._replay(proof.generation_id)
            observation = self.db.execute("SELECT account_id,order_date,ord_no,quantity,average_price,amount,"
                                          "observed_at_utc,contract FROM us_cumulative_observations WHERE order_uid=?",
                                          (proof.order_uid,)).fetchone()
            if observation is None or observation[:3] != (account, row[5], row[2]) or observation[7] != CONTRACT:
                raise ValueError("Matching stored cumulative observation is required")
            _timestamp(observation[6])
            q, average = _decimal(observation[3], "observed quantity"), _decimal(observation[4], "observed average")
            amount = _decimal(observation[5], "observed amount", max_length=130)
            if amount != _calculate(q, average) or (q > 0 and average <= 0) or (q == 0 and average != 0):
                raise ValueError("Invalid observation economics")
            request, filled = _decimal(str(row[13]), "request"), _decimal(str(row[14]), "filled")
            if any(value != value.to_integral_value() or value > 2**53 for value in (q, request, filled)) or request <= 0:
                raise ValueError("Bridge counters require exact whole shares")
            checkpoint = self.db.execute("SELECT generation_id,quantity,amount,contract,last_event_id "
                                         "FROM synthetic_bridge_applied WHERE order_uid=?", (proof.order_uid,)).fetchone()
            previous_q, previous_a = Decimal(0), Decimal(0)
            if checkpoint is not None:
                if checkpoint[0] != proof.generation_id or checkpoint[3] != CONTRACT:
                    raise ValueError("Bridge applied binding/contract mismatch")
                previous_q = _decimal(checkpoint[1], "applied quantity")
                previous_a = _decimal(checkpoint[2], "applied amount", max_length=130)
            elif filled != 0:
                raise ValueError("Existing fills require an explicit bridge baseline")
            events = self.db.execute("SELECT event_id,proof_json FROM synthetic_generation_events WHERE order_uid=? ORDER BY ordinal",
                                     (proof.order_uid,)).fetchall()
            sums = [Fraction(0), Fraction(0)]
            for event_id, raw in events:
                delta = json.loads(raw)
                audit = self.db.execute("SELECT order_uid,generation_id,attribution_json,observation_json "
                                        "FROM synthetic_bridge_audit WHERE event_id=?", (event_id,)).fetchone()
                if audit is None or audit[:2] != (proof.order_uid, proof.generation_id):
                    raise ValueError("Generation event has no matching bridge audit")
                attributed = SyntheticBridgeAttribution(**json.loads(audit[2]))
                old_observation = json.loads(audit[3])
                old_q = _decimal(attributed.cumulative_quantity, "audit quantity", max_length=130)
                old_a = _decimal(attributed.cumulative_amount, "audit amount", max_length=130)
                event_q = Fraction(_decimal(delta["quantity"], "event quantity"))
                event_a = Fraction(_decimal(delta["gross_amount"], "event amount", max_length=130))
                if (attributed.kind != "synthetic-bridge-full-delta"
                        or (attributed.order_uid, attributed.generation_id) != (proof.order_uid, proof.generation_id)
                        or [Fraction(_decimal(attributed.previous_quantity, "audit previous quantity", max_length=130)),
                            Fraction(_decimal(attributed.previous_amount, "audit previous amount", max_length=130))] != sums
                        or (Fraction(old_q), Fraction(old_a)) != (sums[0] + event_q, sums[1] + event_a)
                        or attributed.execution_date != delta["execution_date"]
                        or attributed.execution_sequence != delta["execution_sequence"]
                        or old_observation[:3] != [account, row[5], row[2]] or old_observation[7] != CONTRACT
                        or attributed.observed_at_utc != old_observation[6]
                        or (old_q, old_a) != (_decimal(old_observation[3], "audit observation quantity"),
                                             _decimal(old_observation[5], "audit observation amount", max_length=130))
                        or old_a != _calculate(old_q, _decimal(old_observation[4], "audit observation average"))
                        or event_id != _event_id(self.registry.scope, proof.order_uid, proof.generation_id, old_q, old_a)):
                    raise ValueError("Bridge attribution audit disagrees with generation event")
                _identifier(attributed.evidence_id)
                _timestamp(attributed.observed_at_utc)
                sums[0] += event_q
                sums[1] += event_a
            if sums != [Fraction(previous_q), Fraction(previous_a)] or filled != previous_q:
                raise ValueError("Applied checkpoint/pending disagrees with generation events")
            if checkpoint is not None and not any(event_id == checkpoint[4] for event_id, _ in events):
                raise ValueError("Bridge checkpoint event is missing")
            if self.db.execute("SELECT 1 FROM us_cumulative_observation_conflicts WHERE order_uid=?", (proof.order_uid,)).fetchone():
                return self._hold(proof, "observation_conflict")
            if self.db.execute("SELECT 1 FROM synthetic_generation_holds LIMIT 1").fetchone():
                return SyntheticBridgeResult("HELD")
            dq, da = _subtract(q, previous_q), _subtract(amount, previous_a)
            if q > request or filled > request or dq < 0 or (dq == 0 and da != 0) or (dq > 0 and da <= 0):
                return self._hold(proof, "cumulative_economics_conflict")
            event_id = _event_id(self.registry.scope, proof.order_uid, proof.generation_id, q, amount)
            raw_proof = json.dumps(proof.__dict__, sort_keys=True)
            if dq == 0 and da == 0:
                if checkpoint is None:
                    if supplied != (Decimal(0), Decimal(0), q, amount) or proof.observed_at_utc != observation[6]:
                        raise ValueError("Zero observation attribution mismatch")
                    return SyntheticBridgeResult("NO_CHANGE")
                prior = self.db.execute("SELECT attribution_json FROM synthetic_bridge_audit WHERE event_id=?", (event_id,)).fetchone()
                if checkpoint[4] != event_id or prior is None or prior[0] != raw_proof:
                    raise ValueError("Duplicate attribution disagrees with applied audit")
                return SyntheticBridgeResult("DUPLICATE")
            if supplied != (previous_q, previous_a, q, amount) or proof.observed_at_utc != observation[6]:
                raise ValueError("Attribution must cover the latest full unapplied delta")
            delta = SyntheticGenerationDelta(event_id, proof.generation_id, proof.order_uid, _text(dq), _text(da),
                                             proof.execution_date, proof.execution_sequence)
            result = self.registry._apply_delta_in_transaction(delta)
            if result == "HELD":
                return self._hold(proof, "late_generation_delta")
            if result != "APPLIED":
                raise ValueError("Unexpected generation event/checkpoint divergence")
            self.db.execute("INSERT INTO synthetic_bridge_audit VALUES (?,?,?,?,?)",
                            (event_id, proof.order_uid, proof.generation_id, raw_proof, json.dumps(observation)))
            self.db.execute("INSERT INTO synthetic_bridge_applied VALUES (?,?,?,?,?,?) ON CONFLICT(order_uid) "
                            "DO UPDATE SET quantity=excluded.quantity,amount=excluded.amount,last_event_id=excluded.last_event_id",
                            (proof.order_uid, proof.generation_id, _text(q), _text(amount), CONTRACT, event_id))
            status = "filled" if q == request else ("awaiting_execution_history" if row[15] in ("cancelled", "filled") else row[15])
            updated = self.db.execute("UPDATE pending_orders SET filled_qty=?,status=? WHERE order_uid=? AND filled_qty=?",
                                      (_text(q), status, proof.order_uid, row[14]))
            if updated.rowcount != 1:
                raise ValueError("Pending order changed during bridge application")
            return SyntheticBridgeResult("APPLIED", dq, da)
