"""Multi-tranche snapshot contracts on memory fixtures; no live strategy."""
from dataclasses import replace
from fractions import Fraction
import sqlite3

import pytest

from src.data.us_synthetic_snapshot import SyntheticTrancheSettings, read_synthetic_lifecycle_snapshot
from tests.test_us_synthetic_cost import CYCLE, apply, new_order
from tests.test_us_synthetic_ledger import db, dump, observe  # noqa: F401 -- imported fixture

SETTINGS = tuple(SyntheticTrancheSettings(step, "-3" if step < 3 else None, "4", "0")
                 for step in range(1, 4))


def snapshot(db, **changes):
    values = dict(account_id="us_mock", market="US", symbol="AAPL", lifecycle_id=CYCLE, settings=SETTINGS)
    values.update(changes)
    return read_synthetic_lifecycle_snapshot(db, **values)


def settled(db, uid):
    # Explicit synthetic cancellation: no supervisor or broker request.
    db.execute("UPDATE pending_orders SET status='cancelled' WHERE order_uid=?", (uid,))
    db.commit()


def holdings(db):
    apply(db, "first", "000000042", "BUY", "2", "105", "210", 1)
    settled(db, "first")
    new_order(db, "third", "000000045", "BUY", step=3)
    apply(db, "third", "000000045", "BUY", "2", "90", "180", 2)
    settled(db, "third")


def test_all_tranches_share_one_read_transaction_and_active_is_max_held(db):
    holdings(db)
    before = dump(db)
    statements = []
    db.set_trace_callback(statements.append)
    try:
        result = snapshot(db)
    finally:
        db.set_trace_callback(None)
    assert result.state == "SYNTHETIC_VALIDATED" and result.active_step == 3
    assert result.quantity == 4 and result.remaining_gross_cost == Fraction(390)
    assert result.realized_gross_profit == 0 and len(result.tranches) == 3
    assert sum(line == "BEGIN" for line in statements) == 1
    assert sum(line == "ROLLBACK" for line in statements) == 1
    assert not any(line.startswith(("INSERT", "UPDATE", "DELETE", "COMMIT")) for line in statements)
    assert dump(db) == before and not db.in_transaction
    assert not result.operational_trading_allowed


def test_completed_partial_sell_order_does_not_close_tranche(db):
    holdings(db)
    new_order(db, "exit", "000000046", "SELL", step=3)
    db.execute("UPDATE pending_orders SET requested_qty=1 WHERE order_uid='exit'")
    db.commit()
    apply(db, "exit", "000000046", "SELL", "1", "120", "120", 3)
    result = snapshot(db)
    assert result.active_step == 3 and result.quantity == 3
    assert result.tranches[2].quantity == 1 and result.realized_gross_profit == Fraction(30)


def test_full_closure_returns_to_previous_tranche_then_no_active_step(db):
    holdings(db)
    for uid, number, step, seq in [("exit3", "000000046", 3, 3), ("exit2", "000000047", 2, 4)]:
        new_order(db, uid, number, "SELL", step=step)
        db.execute("UPDATE pending_orders SET requested_qty=2 WHERE order_uid=?", (uid,))
        db.commit()
        apply(db, uid, number, "SELL", "2", "120", "240", seq)
        assert snapshot(db).active_step == (2 if step == 3 else None)
    result = snapshot(db)
    assert result.quantity == 0 and result.remaining_gross_cost == 0
    assert result.realized_gross_profit == Fraction(90)


def test_unresolved_order_holds_even_when_other_tranches_are_valid(db):
    holdings(db)
    new_order(db, "unresolved", "000000046", "SELL", step=3)
    result = snapshot(db)
    assert result.state == "HELD" and "unresolved_order" in result.blocked_reasons
    assert result.tranches == () and result.active_step is None and result.quantity is None


def test_other_generation_open_order_holds_but_terminal_history_is_excluded(db):
    holdings(db)
    new_order(db, "old", "000000046", "BUY", step=3, cycle="older-generation")
    assert snapshot(db).state == "HELD"
    settled(db, "old")
    assert snapshot(db).quantity == 4


def test_unapplied_observation_holds_whole_snapshot(db):
    holdings(db)
    observe(db, "5", "106", "2026-10-04T01:03:00+00:00")
    result = snapshot(db)
    assert "unapplied_observation" in result.blocked_reasons and result.tranches == ()


def test_fresh_connection_and_duplicate_have_identical_snapshot_token(db):
    _, proof = apply(db, "first", "000000042", "BUY", "5", "100", "500", 1)
    from src.data.us_synthetic_ledger import apply_synthetic_cumulative
    original = snapshot(db)
    assert apply_synthetic_cumulative(db, "first", proof).duplicate
    copy = sqlite3.connect(":memory:")
    try:
        db.backup(copy)
        assert snapshot(copy) == original == snapshot(db)
    finally:
        copy.close()


@pytest.mark.parametrize("changes", [
    {"settings": ()}, {"settings": SETTINGS[1:]}, {"settings": (SETTINGS[0], SETTINGS[0])},
    {"settings": [*SETTINGS]}, {"account_id": "kr_mock"}, {"lifecycle_id": " "},
    {"settings": (replace(SETTINGS[0], commission_rate="1"),)},
])
def test_invalid_settings_refuse_without_writes(db, changes):
    before = dump(db)
    with pytest.raises(ValueError):
        snapshot(db, **changes)
    assert dump(db) == before and not db.in_transaction


def test_unconfigured_recorded_tranche_refuses(db):
    holdings(db)
    before = dump(db)
    with pytest.raises(ValueError, match="no configured settings"):
        snapshot(db, settings=SETTINGS[:2])
    assert dump(db) == before


def test_missing_fill_history_refuses_instead_of_zero_projection(db):
    holdings(db)
    db.execute("DELETE FROM synthetic_us_fills WHERE order_uid='third'")
    db.commit()
    before = dump(db)
    with pytest.raises(ValueError, match="fill history"):
        snapshot(db)
    assert dump(db) == before


def test_corrupt_cost_in_lower_tranche_blocks_entire_projection(db):
    holdings(db)
    db.execute("UPDATE synthetic_us_cost_allocations SET state_json='{}' WHERE order_uid='first'")
    db.commit()
    before = dump(db)
    with pytest.raises(ValueError, match="disagrees with replay"):
        snapshot(db)
    assert dump(db) == before and not db.in_transaction


def test_caller_transaction_is_preserved(db):
    db.execute("BEGIN")
    with pytest.raises(ValueError, match="own read transaction"):
        snapshot(db)
    assert db.in_transaction
    db.rollback()


def test_empty_schema_refuses_without_initialization():
    empty = sqlite3.connect(":memory:")
    try:
        with pytest.raises(sqlite3.OperationalError):
            snapshot(empty)
        assert not empty.in_transaction
        assert empty.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    finally:
        empty.close()
