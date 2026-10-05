"""Standalone memory-only reentry registry using caller-supplied synthetic proofs.

No production order identity, broker finality, legacy migration or Engine wiring.
This registry does not promote the existing synthetic ledger's records.
"""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction

from src.data.order_identity import validate_order_date
from src.data.us_cumulative_execution import _decimal
from src.data.us_synthetic_ledger import (
    SyntheticTrancheCost, _cost_order, _cost_record, _cost_step, _memory_only,
)


@dataclass(frozen=True)
class SyntheticGenerationDelta:
    event_id: str
    generation_id: str
    order_uid: str
    quantity: str
    gross_amount: str
    execution_date: str
    execution_sequence: int
    kind: str = "synthetic-generation-delta"


@dataclass(frozen=True)
class SyntheticOrderFinality:
    order_uid: str
    quantity: str
    gross_amount: str
    evidence_id: str
    kind: str = "synthetic-order-finality"


@dataclass(frozen=True)
class SyntheticGenerationClosure:
    generation_id: str
    buy_order_uid: str
    expected_revision: int
    evidence_id: str
    kind: str = "synthetic-generation-closure"


@dataclass(frozen=True)
class SyntheticGenerationView:
    generation_id: str
    step: int
    status: str
    predecessor_id: str | None
    cost: SyntheticTrancheCost


@dataclass(frozen=True)
class SyntheticGenerationSnapshot:
    scope: tuple[str, str, str, str]
    revision: int
    state: str
    blocked_reasons: tuple[str, ...]
    generations: tuple[SyntheticGenerationView, ...] = ()
    active_step: int | None = None
    quantity: int | None = None
    remaining_gross_cost: Fraction | None = None
    realized_gross_profit: Fraction | None = None
    operational_trading_allowed: bool = False


def _identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError("Explicit bounded synthetic identifier is required")
    return value


def initialize_synthetic_generations(db, *, account_id, market, symbol, lifecycle_id):
    """Explicit standalone fixture preparation; existing schema refuses."""
    if account_id != "us_mock" or market != "US":
        raise ValueError("US mock generation scope is required")
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,11}", symbol):
        raise ValueError("Canonical US symbol is required")
    scope = (account_id, market, symbol, _identifier(lifecycle_id))
    _memory_only(db)
    if db.in_transaction:
        raise ValueError("Generation initialization requires its own transaction")
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("CREATE TABLE synthetic_generation_scope (singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                   "scope_json TEXT NOT NULL,revision INTEGER NOT NULL,policy TEXT NOT NULL)")
        db.execute("INSERT INTO synthetic_generation_scope VALUES (1,?,0,'synthetic-generations-v1')",
                   (json.dumps(scope),))
        db.execute("CREATE TABLE synthetic_generations (generation_id TEXT PRIMARY KEY,step INTEGER NOT NULL,"
                   "buy_order_uid TEXT NOT NULL UNIQUE,predecessor_id TEXT UNIQUE,status TEXT NOT NULL,closure_json TEXT)")
        db.execute("CREATE UNIQUE INDEX synthetic_one_active_generation ON synthetic_generations(step) WHERE status!='CLOSED'")
        db.execute("CREATE TABLE synthetic_generation_orders (order_uid TEXT PRIMARY KEY,generation_id TEXT NOT NULL,"
                   "side TEXT NOT NULL,finality_json TEXT)")
        db.execute("CREATE TABLE synthetic_generation_events (event_id TEXT PRIMARY KEY,generation_id TEXT NOT NULL,"
                   "order_uid TEXT NOT NULL,ordinal INTEGER NOT NULL,proof_json TEXT NOT NULL,cost_json TEXT NOT NULL,"
                   "UNIQUE(generation_id,ordinal))")
        db.execute("CREATE TABLE synthetic_generation_holds (generation_id TEXT PRIMARY KEY,reason TEXT NOT NULL)")
        db.commit()
    except Exception:
        db.rollback()
        raise


class SyntheticGenerationRegistry:
    """All mutations are fixture-only; every method owns a separate transaction."""
    operational_trading_allowed = False

    def __init__(self, db, *, account_id, market, symbol, lifecycle_id):
        self.db = db
        self.scope = (account_id, market, symbol, lifecycle_id)

    @contextmanager
    def _transaction(self, write=False):
        _memory_only(self.db)
        if self.db.in_transaction:
            raise ValueError("Generation operation requires its own transaction")
        self.db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            row = self.db.execute("SELECT scope_json,revision,policy FROM synthetic_generation_scope WHERE singleton=1").fetchone()
            if (row is None or tuple(json.loads(row[0])) != self.scope or self.scope[:2] != ("us_mock", "US")
                    or row[2] != "synthetic-generations-v1" or type(row[1]) is not int or row[1] < 0):
                raise ValueError("Generation registry scope/version mismatch")
            yield row[1]
            if write:
                self.db.commit()
            else:
                self.db.rollback()
        except Exception:
            self.db.rollback()
            raise

    def _advance(self):
        self.db.execute("UPDATE synthetic_generation_scope SET revision=revision+1 WHERE singleton=1")

    def _unheld(self):
        if self.db.execute("SELECT 1 FROM synthetic_generation_holds LIMIT 1").fetchone():
            raise ValueError("Durable generation hold blocks mutation")

    def _row(self, generation_id):
        row = self.db.execute("SELECT step,buy_order_uid,predecessor_id,status,closure_json FROM synthetic_generations "
                              "WHERE generation_id=?", (generation_id,)).fetchone()
        if row is None or row[3] not in ("RESERVED", "OPEN", "CLOSURE_PENDING", "CLOSED"):
            raise ValueError("Unknown or malformed generation")
        return row

    def _replay(self, generation_id):
        owner = self._row(generation_id)[1]
        self._validate_bindings(generation_id)
        state, chronology = SyntheticTrancheCost(), []
        rows = self.db.execute("SELECT event_id,order_uid,ordinal,proof_json,cost_json FROM synthetic_generation_events "
                               "WHERE generation_id=? ORDER BY ordinal", (generation_id,)).fetchall()
        for event_id, uid, ordinal, raw, audit in rows:
            proof = SyntheticGenerationDelta(**json.loads(raw))
            if proof.event_id != event_id or proof.order_uid != uid or proof.generation_id != generation_id:
                raise ValueError("Stored generation event identity mismatch")
            self._validate_delta(proof)
            binding = self.db.execute("SELECT generation_id,side FROM synthetic_generation_orders WHERE order_uid=?", (uid,)).fetchone()
            if binding is None or binding[0] != generation_id or binding[1] not in ("BUY", "SELL"):
                raise ValueError("Stored generation binding mismatch")
            if binding[1] == "BUY" and uid != owner:
                raise ValueError("Generation buy owner mismatch")
            _cost_order(chronology, binding[1], proof.execution_date, proof.execution_sequence)
            if any(date == proof.execution_date and sequence >= proof.execution_sequence
                   for _, date, sequence in chronology):
                raise ValueError("Generation chronology must be strictly increasing")
            state, allocated, profit = _cost_step(state, uid, binding[1], _decimal(proof.quantity, "quantity"),
                                                   _decimal(proof.gross_amount, "amount", max_length=130))
            if ordinal != state.fills or json.loads(audit) != _cost_record(state, allocated, profit):
                raise ValueError("Generation cost audit mismatch")
            chronology.append((binding[1], proof.execution_date, proof.execution_sequence))
        return state, chronology

    def _validate_bindings(self, generation_id, *, require_final=False):
        """Validate every binding, including orders with no economic events."""
        owner = self._row(generation_id)[1]
        _identifier(owner)
        rows = self.db.execute("SELECT order_uid,side,finality_json FROM synthetic_generation_orders "
                               "WHERE generation_id=?", (generation_id,)).fetchall()
        owner_seen = False
        for uid, side, raw in rows:
            _identifier(uid)
            if side not in ("BUY", "SELL") or (side == "BUY" and uid != owner) or (uid == owner and side != "BUY"):
                raise ValueError("Generation order binding side/owner mismatch")
            owner_seen = owner_seen or uid == owner
            if raw is None:
                if require_final:
                    raise ValueError("Unresolved order prevents closure")
            else:
                proof = SyntheticOrderFinality(**json.loads(raw))
                if proof.order_uid != uid:
                    raise ValueError("Stored finality identity mismatch")
                self._validate_finality(proof)
        if not owner_seen:
            raise ValueError("Generation owner binding is missing")

    def _validate_existing_generation(self, generation_id):
        """Idempotence requires valid persisted state, never automatic repair."""
        row = self._row(generation_id)
        if type(row[0]) is not int or row[0] < 1:
            raise ValueError("Invalid stored generation step")
        state, _ = self._replay(generation_id)
        if row[3] == "CLOSED":
            self._validate_closed(generation_id)
        elif row[4] is not None or (row[3] == "OPEN" and not state.quantity) or (
                row[3] == "RESERVED" and state.fills) or (
                row[3] == "CLOSURE_PENDING" and (state.quantity or not state.fills)):
            raise ValueError("Generation status disagrees with economics")
        chain, cursor = {generation_id}, row[2]
        while cursor is not None:
            if cursor in chain:
                raise ValueError("Cyclic predecessor chain")
            chain.add(cursor)
            parent = self._row(cursor)
            if parent[0] != row[0] or parent[3] != "CLOSED":
                raise ValueError("Broken predecessor chain")
            self._validate_closed(cursor)
            cursor = parent[2]

    @staticmethod
    def _validate_delta(proof):
        if not isinstance(proof, SyntheticGenerationDelta) or proof.kind != "synthetic-generation-delta":
            raise ValueError("Explicit synthetic delta proof is required")
        for value in (proof.event_id, proof.generation_id, proof.order_uid):
            _identifier(value)
        validate_order_date(proof.execution_date)
        if type(proof.execution_sequence) is not int or proof.execution_sequence < 1:
            raise ValueError("Explicit synthetic execution sequence is required")
        q = _decimal(proof.quantity, "quantity")
        a = _decimal(proof.gross_amount, "amount", max_length=130)
        if q <= 0 or q != q.to_integral_value() or q > 2**53 or a <= 0:
            raise ValueError("Invalid generation delta economics")

    def reserve(self, generation_id, step, buy_order_uid, *, predecessor_id, expected_revision):
        for value in (generation_id, buy_order_uid):
            _identifier(value)
        if predecessor_id is not None:
            _identifier(predecessor_id)
        if type(step) is not int or step < 1 or type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Explicit step and revision are required")
        with self._transaction(True) as revision:
            self._unheld()
            existing = self.db.execute("SELECT step,buy_order_uid,predecessor_id FROM synthetic_generations WHERE generation_id=?",
                                       (generation_id,)).fetchone()
            if existing is not None:
                if existing != (step, buy_order_uid, predecessor_id):
                    raise ValueError("Conflicting generation reservation")
                self._validate_existing_generation(generation_id)
                return "DUPLICATE"
            if revision != expected_revision:
                raise ValueError("Stale generation revision")
            if self.db.execute("SELECT 1 FROM synthetic_generation_orders WHERE finality_json IS NULL LIMIT 1").fetchone():
                raise ValueError("Unresolved order blocks reservation")
            previous = self.db.execute("SELECT generation_id,status FROM synthetic_generations WHERE step=?", (step,)).fetchall()
            if previous:
                leaves = [gid for gid, _ in previous if not self.db.execute(
                    "SELECT 1 FROM synthetic_generations WHERE predecessor_id=?", (gid,)).fetchone()]
                if leaves != [predecessor_id] or any(status != "CLOSED" for _, status in previous):
                    raise ValueError("Explicit closed predecessor is required")
                for previous_id, _ in previous:
                    self._validate_closed(previous_id)
            elif predecessor_id is not None:
                raise ValueError("Foreign predecessor cannot seed a new step")
            self.db.execute("INSERT INTO synthetic_generations VALUES (?,?,?,?,'RESERVED',NULL)",
                            (generation_id, step, buy_order_uid, predecessor_id))
            self.db.execute("INSERT INTO synthetic_generation_orders VALUES (?,?,'BUY',NULL)", (buy_order_uid, generation_id))
            self._advance()
            return "RESERVED"

    def bind_sell(self, generation_id, order_uid):
        _identifier(order_uid)
        with self._transaction(True):
            self._unheld()
            if self._row(generation_id)[3] != "OPEN":
                raise ValueError("Sell binding requires an open generation")
            self._replay(generation_id)
            existing = self.db.execute("SELECT generation_id,side FROM synthetic_generation_orders WHERE order_uid=?", (order_uid,)).fetchone()
            if existing is not None:
                if existing != (generation_id, "SELL"):
                    raise ValueError("Order UID already belongs to another owner")
                return "DUPLICATE"
            self.db.execute("INSERT INTO synthetic_generation_orders VALUES (?,?,'SELL',NULL)", (order_uid, generation_id))
            self._advance()
            return "BOUND"

    def apply_delta(self, proof):
        self._validate_delta(proof)
        with self._transaction(True):
            return self._apply_delta_in_transaction(proof)

    def _apply_delta_in_transaction(self, proof):
        """Internal fixture operation; caller owns commit/rollback and scope validation."""
        if not self.db.in_transaction:
            raise ValueError("Internal delta application requires a caller transaction")
        self._validate_delta(proof)
        raw = json.dumps(proof.__dict__, sort_keys=True)
        row = self._row(proof.generation_id)
        binding = self.db.execute("SELECT generation_id,side,finality_json FROM synthetic_generation_orders WHERE order_uid=?",
                                  (proof.order_uid,)).fetchone()
        if binding is None or binding[0] != proof.generation_id:
            raise ValueError("Exact generation order binding is required")
        prior = self.db.execute("SELECT proof_json FROM synthetic_generation_events WHERE event_id=?", (proof.event_id,)).fetchone()
        state, chronology = self._replay(proof.generation_id)
        if prior is not None and prior[0] == raw:
            return "DUPLICATE"
        if prior is not None or row[3] == "CLOSED" or binding[2] is not None:
            self.db.execute("INSERT OR IGNORE INTO synthetic_generation_holds VALUES (?,?)",
                            (proof.generation_id, "late_delta_or_conflicting_event"))
            self._advance()
            return "HELD"
        self._unheld()
        _cost_order(chronology, binding[1], proof.execution_date, proof.execution_sequence)
        if any(date == proof.execution_date and sequence >= proof.execution_sequence
               for _, date, sequence in chronology):
            raise ValueError("Generation chronology must be strictly increasing")
        state, allocated, profit = _cost_step(state, proof.order_uid, binding[1], _decimal(proof.quantity, "quantity"),
                                               _decimal(proof.gross_amount, "amount", max_length=130))
        self.db.execute("INSERT INTO synthetic_generation_events VALUES (?,?,?,?,?,?)",
                        (proof.event_id, proof.generation_id, proof.order_uid, state.fills, raw,
                         json.dumps(_cost_record(state, allocated, profit), sort_keys=True)))
        self.db.execute("UPDATE synthetic_generations SET status=? WHERE generation_id=?",
                        ("OPEN" if state.quantity else "CLOSURE_PENDING", proof.generation_id))
        self._advance()
        return "APPLIED"

    def finalize_order(self, proof):
        if not isinstance(proof, SyntheticOrderFinality) or proof.kind != "synthetic-order-finality":
            raise ValueError("Explicit synthetic finality proof is required")
        _identifier(proof.evidence_id)
        with self._transaction(True):
            self._unheld()
            binding = self.db.execute("SELECT generation_id,finality_json FROM synthetic_generation_orders WHERE order_uid=?",
                                      (proof.order_uid,)).fetchone()
            if binding is None:
                raise ValueError("Unknown order binding")
            self._replay(binding[0])
            self._validate_finality(proof)
            raw = json.dumps(proof.__dict__, sort_keys=True)
            if binding[1] is not None:
                if binding[1] != raw:
                    raise ValueError("Conflicting finality proof")
                return "DUPLICATE"
            self.db.execute("UPDATE synthetic_generation_orders SET finality_json=? WHERE order_uid=?", (raw, proof.order_uid))
            self._advance()
            return "FINALIZED"

    def _validate_finality(self, proof):
        if proof.kind != "synthetic-order-finality":
            raise ValueError("Invalid stored finality kind")
        _identifier(proof.evidence_id)
        proofs = [json.loads(row[0]) for row in self.db.execute(
            "SELECT proof_json FROM synthetic_generation_events WHERE order_uid=?", (proof.order_uid,))]
        q = sum((Fraction(_decimal(item["quantity"], "quantity")) for item in proofs), Fraction(0))
        a = sum((Fraction(_decimal(item["gross_amount"], "amount", max_length=130)) for item in proofs), Fraction(0))
        if (Fraction(_decimal(proof.quantity, "final quantity")),
                Fraction(_decimal(proof.gross_amount, "final amount", max_length=130))) != (q, a):
            raise ValueError("Finality proof does not cover applied order economics")

    def close(self, proof):
        if not isinstance(proof, SyntheticGenerationClosure) or proof.kind != "synthetic-generation-closure":
            raise ValueError("Explicit synthetic closure proof is required")
        _identifier(proof.evidence_id)
        if type(proof.expected_revision) is not int or proof.expected_revision < 0:
            raise ValueError("Explicit closure revision is required")
        with self._transaction(True) as revision:
            self._unheld()
            row = self._row(proof.generation_id)
            raw = json.dumps(proof.__dict__, sort_keys=True)
            if row[3] == "CLOSED" and row[4] == raw:
                self._validate_closed(proof.generation_id)
                return "DUPLICATE"
            state, _ = self._replay(proof.generation_id)
            if revision != proof.expected_revision or row[1] != proof.buy_order_uid or state.quantity or state.remaining_gross_cost:
                raise ValueError("Closure requires matching revision, owner and zero economics")
            self._finalities(proof.generation_id)
            self.db.execute("UPDATE synthetic_generations SET status='CLOSED',closure_json=? WHERE generation_id=?",
                            (raw, proof.generation_id))
            self._advance()
            return "CLOSED"

    def _finalities(self, generation_id):
        self._validate_bindings(generation_id, require_final=True)

    def _validate_closed(self, generation_id):
        row = self._row(generation_id)
        state, _ = self._replay(generation_id)
        proof = SyntheticGenerationClosure(**json.loads(row[4])) if row[4] is not None else None
        if (row[3] != "CLOSED" or state.quantity or state.remaining_gross_cost or proof is None
                or proof.generation_id != generation_id or proof.buy_order_uid != row[1]
                or proof.kind != "synthetic-generation-closure" or type(proof.expected_revision) is not int
                or not 0 <= proof.expected_revision < self.db.execute(
                    "SELECT revision FROM synthetic_generation_scope WHERE singleton=1").fetchone()[0]):
            raise ValueError("Stored closure is invalid")
        _identifier(proof.evidence_id)
        self._finalities(generation_id)

    def snapshot(self):
        with self._transaction() as revision:
            if self.db.execute("SELECT 1 FROM synthetic_generation_events e LEFT JOIN synthetic_generations g "
                               "ON g.generation_id=e.generation_id WHERE g.generation_id IS NULL LIMIT 1").fetchone():
                raise ValueError("Orphan generation event")
            reasons = {row[0] for row in self.db.execute("SELECT reason FROM synthetic_generation_holds")}
            rows = self.db.execute("SELECT generation_id,step,predecessor_id,status,buy_order_uid FROM synthetic_generations ORDER BY step,generation_id").fetchall()
            seen, views = set(), []
            for gid, step, predecessor, status, owner in rows:
                if type(step) is not int or step < 1:
                    raise ValueError("Invalid stored generation step")
                if predecessor is not None:
                    parent = self._row(predecessor)
                    if parent[0] != step or parent[3] != "CLOSED":
                        raise ValueError("Broken predecessor chain")
                    # Each node must reach a root without cycling.
                    chain, cursor = {gid}, predecessor
                    while cursor is not None:
                        if cursor in chain:
                            raise ValueError("Cyclic predecessor chain")
                        chain.add(cursor)
                        cursor = self._row(cursor)[2]
                if status != "CLOSED":
                    if step in seen:
                        raise ValueError("Overlapping active generations")
                    seen.add(step)
                binding = self.db.execute("SELECT generation_id,side FROM synthetic_generation_orders WHERE order_uid=?", (owner,)).fetchone()
                if binding != (gid, "BUY"):
                    raise ValueError("Generation owner binding mismatch")
                state, _ = self._replay(gid)
                if status == "CLOSED":
                    self._validate_closed(gid)
                elif status in ("RESERVED", "CLOSURE_PENDING"):
                    reasons.add("generation_not_reconciled")
                elif status != "OPEN" or not state.quantity:
                    raise ValueError("Generation status disagrees with economics")
                views.append(SyntheticGenerationView(gid, step, status, predecessor, state))
            for uid, gid, raw in self.db.execute("SELECT order_uid,generation_id,finality_json FROM synthetic_generation_orders"):
                self._row(gid)
                if raw is None:
                    reasons.add("unresolved_order")
                else:
                    proof = SyntheticOrderFinality(**json.loads(raw))
                    if proof.order_uid != uid:
                        raise ValueError("Stored finality identity mismatch")
                    self._validate_finality(proof)
            if reasons:
                return SyntheticGenerationSnapshot(self.scope, revision, "HELD", tuple(sorted(reasons)))
            active = max((view.step for view in views if view.cost.quantity), default=None)
            return SyntheticGenerationSnapshot(
                self.scope, revision, "SYNTHETIC_VALIDATED", (), tuple(views), active,
                sum(view.cost.quantity for view in views),
                sum((view.cost.remaining_gross_cost for view in views), Fraction(0)),
                sum((view.cost.realized_gross_profit for view in views), Fraction(0)),
            )
