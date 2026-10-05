"""Exact average cost and order-bound tranche isolation on memory fixtures."""
from dataclasses import replace
from fractions import Fraction
import json
import sqlite3

import pytest

from src.data.us_synthetic_ledger import apply_synthetic_cumulative, restore_synthetic_tranche_cost
from tests.test_us_synthetic_ledger import db, dump, evidence, observe  # noqa: F401 -- imported fixture
from tests.test_us_cumulative_execution import DATE, STAMP

CYCLE = "synthetic-cycle-1"


def new_order(db, uid, number, side, step=2, cycle=CYCLE):
    db.execute("INSERT INTO order_identities VALUES (?,?,?,?,?,?,?,?,?)",
               (uid, "us_mock", "US", number, "AAPL", side, DATE, "confirmed", STAMP))
    db.execute("INSERT INTO pending_orders "
               "(order_uid,account_id,ord_no,symbol,side,requested_qty,filled_qty,status,step,lifecycle_id) "
               "VALUES (?,?,?,?,?,5,0,'open',?,?)", (uid, "us_mock", number, "AAPL", side, step, cycle))
    db.commit()


def apply(db, uid, number, side, q, average, amount, sequence, previous_q="0", previous_amount="0"):
    stamp = f"2026-10-04T01:{sequence:02d}:00+00:00"
    observe(db, q, average, stamp, ord_no=number, slby_tp_nm="매수" if side == "BUY" else "매도")
    proof = replace(evidence(previous_q, previous_amount, q, amount), order_uid=uid,
                    execution_sequence=sequence)
    return apply_synthetic_cumulative(db, uid, proof), proof


def state(db, step=2):
    return restore_synthetic_tranche_cost(db, "AAPL", step, CYCLE)


def test_partial_sale_and_new_tranche_never_blend_costs(db):
    apply(db, "first", "000000042", "BUY", "2", "105", "210", 1)
    new_order(db, "exit", "000000044", "SELL")
    result, proof = apply(db, "exit", "000000044", "SELL", "1", "120", "120", 2)
    assert result.allocated_gross_cost == Fraction(105)
    assert result.realized_gross_profit == Fraction(15)
    remaining = state(db)
    assert (remaining.quantity, remaining.remaining_gross_cost) == (1, Fraction(105))
    new_order(db, "next", "000000045", "BUY", step=3)
    apply(db, "next", "000000045", "BUY", "1", "90", "90", 3)
    assert state(db).average_gross_cost == Fraction(105)
    assert state(db, 3).average_gross_cost == Fraction(90)
    before = dump(db)
    assert apply_synthetic_cumulative(db, "exit", proof).duplicate
    assert dump(db) == before


def test_another_buy_order_cannot_join_existing_tranche(db):
    apply(db, "first", "000000042", "BUY", "2", "105", "210", 1)
    new_order(db, "other", "000000045", "BUY")
    observe(db, "1", "90", "2026-10-04T01:02:00+00:00", ord_no="000000045")
    proof = replace(evidence(q="1", amount="90"), order_uid="other", execution_sequence=2)
    before = dump(db)
    with pytest.raises(ValueError, match="Different buy order"):
        apply_synthetic_cumulative(db, "other", proof)
    assert dump(db) == before


def test_same_order_partial_fills_after_partial_sale_use_remaining_cost(db):
    apply(db, "first", "000000042", "BUY", "2", "100", "200", 1)
    new_order(db, "exit", "000000044", "SELL")
    apply(db, "exit", "000000044", "SELL", "1", "120", "120", 2)
    apply(db, "first", "000000042", "BUY", "5", "106", "530", 3, "2", "200")
    remaining = state(db)
    assert (remaining.quantity, remaining.remaining_gross_cost) == (4, Fraction(430))
    assert remaining.average_gross_cost == Fraction(215, 2)
    assert remaining.realized_gross_profit == Fraction(20)


def test_repeating_cost_allocation_conserves_total_cost_and_closes_exactly(db):
    apply(db, "first", "000000042", "BUY", "2", "100", "200", 1)
    new_order(db, "exit", "000000044", "SELL")
    apply(db, "exit", "000000044", "SELL", "1", "120", "120", 2)
    apply(db, "first", "000000042", "BUY", "4", "100.5", "402", 3, "2", "200")
    assert state(db).average_gross_cost == Fraction(302, 3)
    second, _ = apply(db, "exit", "000000044", "SELL", "2", "130", "260", 4, "1", "120")
    assert second.allocated_gross_cost == Fraction(302, 3)
    assert state(db).remaining_gross_cost == Fraction(604, 3)
    final, _ = apply(db, "exit", "000000044", "SELL", "4", "130", "520", 5, "2", "260")
    assert final.allocated_gross_cost == Fraction(604, 3)
    remaining = state(db)
    assert (remaining.quantity, remaining.remaining_gross_cost, remaining.average_gross_cost) == (0, Fraction(0), None)
    assert remaining.realized_gross_profit == Fraction(118)
    assert not remaining.operational_ingestion_allowed


def test_losing_sale_keeps_signed_profit(db):
    apply(db, "first", "000000042", "BUY", "2", "100", "200", 1)
    new_order(db, "exit", "000000044", "SELL")
    result, _ = apply(db, "exit", "000000044", "SELL", "1", "90", "90", 2)
    assert result.realized_gross_profit == Fraction(-10)
    assert state(db).remaining_gross_cost == Fraction(100)


def test_cost_write_failure_rolls_back_fill_baseline_and_pending(db):
    observe(db)
    db.execute("CREATE TRIGGER deny_cost BEFORE INSERT ON synthetic_us_cost_allocations "
               "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    db.commit()
    before = dump(db)
    with pytest.raises(sqlite3.IntegrityError):
        apply_synthetic_cumulative(db, "first", evidence())
    assert dump(db) == before and not db.in_transaction


def test_restore_is_read_only_and_corrupt_cost_record_is_refused(db):
    apply(db, "first", "000000042", "BUY", "2", "100", "200", 1)
    before = dump(db)
    assert state(db).remaining_gross_cost == Fraction(200)
    assert dump(db) == before
    db.execute("UPDATE synthetic_us_cost_allocations SET state_json='{}'")
    db.commit()
    before = dump(db)
    with pytest.raises(ValueError, match="disagrees with replay"):
        state(db)
    assert dump(db) == before


def test_late_buy_partial_fill_cannot_change_preceding_sell_basis(db):
    apply(db, "first", "000000042", "BUY", "2", "100", "200", 1)
    new_order(db, "exit", "000000044", "SELL")
    apply(db, "exit", "000000044", "SELL", "1", "120", "120", 3)
    observe(db, "5", "106", "2026-10-04T01:04:00+00:00")
    proof = replace(evidence("2", "200", "5", "530"), execution_sequence=2)
    before = dump(db)
    with pytest.raises(ValueError, match="Same-day cost"):
        apply_synthetic_cumulative(db, "first", proof)
    assert dump(db) == before


def test_reference_tracks_original_order_average_not_remaining_cost(db):
    apply(db, "first", "000000042", "BUY", "2", "100", "200", 1)
    assert state(db).entry_reference_price == Fraction(100)
    new_order(db, "exit", "000000044", "SELL")
    apply(db, "exit", "000000044", "SELL", "1", "120", "120", 2)
    assert state(db).entry_reference_price == Fraction(100)
    apply(db, "first", "000000042", "BUY", "5", "106", "530", 3, "2", "200")
    restored = state(db)
    assert restored.entry_reference_price == Fraction(106)
    assert restored.average_gross_cost == Fraction(215, 2)
    assert restored.cumulative_buy_quantity == 5
    assert restored.cumulative_buy_gross_amount == Fraction(530)
    assert restored.realized_gross_profit == Fraction(20)
    before = dump(db)
    assert state(db) == restored and dump(db) == before


def test_reference_is_independent_of_other_tranches_and_duplicates(db):
    _, proof = apply(db, "first", "000000042", "BUY", "2", "105", "210", 1)
    new_order(db, "next", "000000045", "BUY", step=3)
    apply(db, "next", "000000045", "BUY", "1", "90", "90", 2)
    assert state(db).entry_reference_price == Fraction(105)
    assert state(db, 3).entry_reference_price == Fraction(90)
    before = dump(db)
    assert apply_synthetic_cumulative(db, "first", proof).duplicate
    assert dump(db) == before and state(db).entry_reference_price == Fraction(105)


def test_observation_without_application_never_changes_reference(db):
    apply(db, "first", "000000042", "BUY", "2", "100", "200", 1)
    observe(db, "5", "106", "2026-10-04T01:02:00+00:00")
    assert state(db).entry_reference_price == Fraction(100)
    assert state(db).cumulative_buy_quantity == 2


def test_full_closure_has_no_active_reference(db):
    assert state(db).entry_reference_price is None
    apply(db, "first", "000000042", "BUY", "2", "100", "200", 1)
    new_order(db, "exit", "000000044", "SELL")
    apply(db, "exit", "000000044", "SELL", "2", "120", "240", 2)
    closed = state(db)
    assert closed.entry_reference_price is None and closed.average_gross_cost is None
    assert closed.cumulative_buy_quantity == 2
    assert closed.cumulative_buy_gross_amount == Fraction(200)


@pytest.mark.parametrize("field,value", [
    ("entry_reference_price", ["90", "1"]),
    ("cumulative_buy_quantity", 3),
    ("cumulative_buy_gross_amount", ["300", "1"]),
    ("policy", "one-buy-order-per-tranche-average-v1"),
])
def test_reference_audit_corruption_or_old_policy_refuses_restore(db, field, value):
    apply(db, "first", "000000042", "BUY", "2", "100", "200", 1)
    record = json.loads(db.execute("SELECT state_json FROM synthetic_us_cost_allocations").fetchone()[0])
    record[field] = value
    db.execute("UPDATE synthetic_us_cost_allocations SET state_json=?", (json.dumps(record),))
    db.commit()
    before = dump(db)
    with pytest.raises(ValueError, match="disagrees with replay"):
        state(db)
    assert dump(db) == before
