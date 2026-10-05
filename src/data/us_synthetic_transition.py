"""Read-only legacy transition assessment on an isolated in-memory US mock DB.

Proofs are caller-supplied synthetic fixtures, never authenticated broker data.
No baselines, migration, inferred prices, or operational permissions are written.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal, localcontext

from src.data.order_identity import validate_order_date
from src.data.us_cumulative_execution import _decimal
from src.data.us_synthetic_ledger import _memory_only


@dataclass(frozen=True)
class SyntheticLegacyFillProof:
    fill_id: str
    order_uid: str
    quantity: str
    gross_amount: str
    execution_date: str
    legacy_price: str
    evidence_id: str
    kind: str = "synthetic-decimal-gross-and-date"


@dataclass(frozen=True)
class SyntheticTransitionAssessment:
    state: str
    reasons: tuple[str, ...]
    quantity: Decimal | None = None
    calculated_gross_amount: Decimal | None = None
    operational_transition_allowed: bool = False


def assess_synthetic_transition(
    db: sqlite3.Connection, order_uid: str,
    proofs: tuple[SyntheticLegacyFillProof, ...] = (),
) -> SyntheticTransitionAssessment:
    """A candidate requires complete exact amount/date proofs for every old fill.

    Missing/malformed schema refuses before returning a candidate. A caller's
    existing transaction is refused, never committed or rolled back. This is
    one read snapshot; it cannot promote its result into migration authority.
    """
    _memory_only(db)
    if db.in_transaction:
        raise ValueError("Transition assessment requires its own read snapshot")
    db.execute("BEGIN")
    try:
        result = _assess(db, order_uid, proofs)
    finally:
        db.rollback()
    return result


def _assess(db, order_uid, proofs):
    rows = db.execute(
        "SELECT i.account_id,i.market,i.ord_no,i.symbol,i.side,i.broker_order_date,"
        "i.identity_status,p.account_id,p.ord_no,p.symbol,p.side,p.step,p.filled_qty,p.requested_qty "
        "FROM order_identities i LEFT JOIN pending_orders p ON p.order_uid=i.order_uid WHERE i.order_uid=?",
        (order_uid,),
    ).fetchall()
    if len(rows) != 1:
        return SyntheticTransitionAssessment("HELD", ("pending_identity_missing_or_ambiguous",))
    row = rows[0]
    account, market, number, symbol, side, date, state = row[:7]
    if (account != "us_mock" or market != "US" or state != "confirmed"
            or tuple(row[7:11]) != (account, number, symbol, side)
            or side not in ("BUY", "SELL") or type(row[11]) is not int or row[11] < 1):
        return SyntheticTransitionAssessment("HELD", ("identity_or_tranche_unconfirmed",))
    validate_order_date(date)
    filled = _decimal(str(row[12]), "legacy filled quantity")
    requested = _decimal(str(row[13]), "legacy requested quantity")
    if requested <= 0 or filled > requested or filled != filled.to_integral_value():
        return SyntheticTransitionAssessment("HELD", ("legacy_pending_quantity_invalid",))
    if db.execute("SELECT 1 FROM us_cumulative_observation_conflicts WHERE order_uid=?", (order_uid,)).fetchone():
        return SyntheticTransitionAssessment("HELD", ("durable_observation_conflict",))
    if db.execute("SELECT 1 FROM execution_quantity_conflicts WHERE order_uid=?", (order_uid,)).fetchone():
        return SyntheticTransitionAssessment("HELD", ("durable_quantity_conflict",))
    if (db.execute("SELECT 1 FROM synthetic_us_applied WHERE order_uid=?", (order_uid,)).fetchone()
            or db.execute("SELECT 1 FROM synthetic_us_fills WHERE order_uid=?", (order_uid,)).fetchone()):
        return SyntheticTransitionAssessment("HELD", ("synthetic_baseline_already_started",))
    if db.execute("SELECT 1 FROM trade_ledger WHERE account_id=? AND ord_no=? AND order_uid IS NULL",
                  (account, number)).fetchone():
        return SyntheticTransitionAssessment("HELD", ("legacy_order_uid_missing",))
    fills = db.execute(
        "SELECT id,account_id,ord_no,symbol,type,step,qty,price FROM trade_ledger WHERE order_uid=?",
        (order_uid,),
    ).fetchall()
    if not fills:
        if proofs or filled != 0:
            return SyntheticTransitionAssessment("HELD", ("missing_legacy_fill_history",))
        return SyntheticTransitionAssessment("ZERO_BASELINE_CANDIDATE", (), Decimal(0), Decimal(0))
    by_id = {}
    for proof in proofs:
        if (not isinstance(proof, SyntheticLegacyFillProof) or proof.fill_id in by_id
                or proof.order_uid != order_uid or proof.kind != "synthetic-decimal-gross-and-date"
                or not isinstance(proof.evidence_id, str) or not proof.evidence_id.strip()):
            return SyntheticTransitionAssessment("HELD", ("synthetic_proof_invalid_or_duplicate",))
        by_id[proof.fill_id] = proof
    reasons = set()
    sum_q, sum_amount = Decimal(0), Decimal(0)
    seen = set()
    with localcontext() as context:
        context.prec = 300
        for fill in fills:
            fill_id, owner, ord_no, ticker, direction, step, quantity, price = fill
            if (fill_id in seen or (owner, ord_no, ticker, direction, step)
                    != (account, number, symbol, side.lower(), row[11])):
                reasons.add("legacy_fill_identity_mismatch")
            seen.add(fill_id)
            q = _decimal(str(quantity), "legacy fill quantity")
            if q <= 0 or q != q.to_integral_value():
                reasons.add("legacy_fill_quantity_invalid")
            sum_q += q
            proof = by_id.get(fill_id)
            if proof is None:
                reasons.update(("exact_amount_evidence_missing", "execution_date_evidence_missing"))
                continue
            if (_decimal(proof.quantity, "proof quantity") != q or proof.legacy_price != str(price)):
                reasons.add("synthetic_proof_does_not_bind_legacy_row")
            try:
                validate_order_date(proof.execution_date)
            except ValueError:
                reasons.add("execution_date_evidence_invalid")
            amount = _decimal(proof.gross_amount, "proof amount", max_length=130)
            if amount <= 0:
                reasons.add("exact_amount_evidence_invalid")
            sum_amount += amount
    if set(by_id) - seen:
        reasons.add("synthetic_proof_has_unmatched_rows")
    if sum_q != filled:
        reasons.add("legacy_fills_disagree_with_pending")
    if reasons:
        return SyntheticTransitionAssessment("HELD", tuple(sorted(reasons)))
    return SyntheticTransitionAssessment("EXACT_BASELINE_CANDIDATE", (), sum_q, sum_amount)
