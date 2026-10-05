"""Synthetic strategy projection contracts; no broker or Engine execution."""
from fractions import Fraction
import sqlite3

import pytest

from src.data.us_synthetic_strategy import project_synthetic_strategy
from tests.test_us_synthetic_cost import CYCLE, apply, new_order
from tests.test_us_synthetic_ledger import db, dump, observe  # noqa: F401 -- imported fixture


def projection(db, **changes):
    values = dict(account_id="us_mock", market="US", symbol="AAPL", step=2,
                  lifecycle_id=CYCLE, drop_pct="-3", profit_pct="4", commission_rate="0")
    values.update(changes)
    return project_synthetic_strategy(db, **values)


def test_reference_and_remaining_cost_drive_separate_thresholds(db):
    apply(db, "first", "000000042", "BUY", "2", "100", "200", 1)
    new_order(db, "exit", "000000044", "SELL")
    apply(db, "exit", "000000044", "SELL", "1", "120", "120", 2)
    apply(db, "first", "000000042", "BUY", "5", "106", "530", 3, "2", "200")
    before = dump(db)
    result = projection(db)
    assert result.quantity == 4 and result.remaining_gross_cost == Fraction(430)
    assert result.entry_reference_price == Fraction(106)
    assert result.average_gross_cost == Fraction(215, 2)
    assert result.next_buy_trigger == Fraction(5141, 50)
    assert result.sell_target_price == Fraction(559, 5)
    assert result.realized_gross_profit == Fraction(20)
    assert result.applied_fill_count == 3 and not result.operational_trading_allowed
    assert dump(db) == before and not db.in_transaction


def test_partial_sale_and_other_tranche_leave_buy_trigger_unchanged(db):
    apply(db, "first", "000000042", "BUY", "2", "105", "210", 1)
    original = projection(db)
    new_order(db, "exit", "000000044", "SELL")
    apply(db, "exit", "000000044", "SELL", "1", "120", "120", 2)
    new_order(db, "next", "000000045", "BUY", step=3)
    apply(db, "next", "000000045", "BUY", "1", "90", "90", 3)
    assert projection(db).next_buy_trigger == original.next_buy_trigger
    assert projection(db).realized_gross_profit == Fraction(15)
    assert projection(db, step=3).entry_reference_price == Fraction(90)


def test_fresh_connection_restore_and_duplicate_preserve_projection(db):
    _, proof = apply(db, "first", "000000042", "BUY", "2", "100", "200", 1)
    original = projection(db)
    from src.data.us_synthetic_ledger import apply_synthetic_cumulative
    assert apply_synthetic_cumulative(db, "first", proof).duplicate
    restored_db = sqlite3.connect(":memory:")
    try:
        db.backup(restored_db)
        assert projection(restored_db) == original == projection(db)
    finally:
        restored_db.close()


def test_modeled_commission_target_does_not_change_gross_profit(db):
    apply(db, "first", "000000042", "BUY", "2", "100", "200", 1)
    result = projection(db, commission_rate="0.001")
    assert result.sell_target_price == Fraction(100) * Fraction(1001, 1000) * Fraction(104, 100) / Fraction(999, 1000)
    assert result.realized_gross_profit == 0


def test_empty_and_closed_tranches_have_no_thresholds(db):
    empty = projection(db)
    assert empty.next_buy_trigger is None and empty.sell_target_price is None
    apply(db, "first", "000000042", "BUY", "2", "100", "200", 1)
    new_order(db, "exit", "000000044", "SELL")
    apply(db, "exit", "000000044", "SELL", "2", "120", "240", 2)
    closed = projection(db)
    assert closed.quantity == 0 and closed.remaining_gross_cost == 0
    assert closed.next_buy_trigger is None and closed.sell_target_price is None
    assert closed.realized_gross_profit == Fraction(40)


def test_no_next_transition_and_unapplied_observation(db):
    apply(db, "first", "000000042", "BUY", "2", "100", "200", 1)
    previous = projection(db, drop_pct=None)
    observe(db, "5", "106", "2026-10-04T01:02:00+00:00")
    assert projection(db, drop_pct=None) == previous
    assert previous.next_buy_trigger is None and previous.sell_target_price == Fraction(104)


@pytest.mark.parametrize("changes", [
    {"account_id": "kr_mock"}, {"market": "KR"}, {"symbol": "aapl"},
    {"step": True}, {"step": 0}, {"lifecycle_id": " "},
    {"drop_pct": "-100"}, {"drop_pct": "1"}, {"drop_pct": "NaN"},
    {"profit_pct": "-1"}, {"profit_pct": "1e2"}, {"commission_rate": "1"},
    {"commission_rate": 0.001},
])
def test_invalid_configuration_refuses_without_writes(db, changes):
    before = dump(db)
    with pytest.raises(ValueError):
        projection(db, **changes)
    assert dump(db) == before and not db.in_transaction


def test_caller_transaction_is_preserved(db):
    db.execute("BEGIN")
    before = dump(db)
    with pytest.raises(ValueError, match="own read snapshot"):
        projection(db)
    assert db.in_transaction and dump(db) == before
    db.rollback()


def test_corrupt_audit_refuses_without_repair(db):
    apply(db, "first", "000000042", "BUY", "2", "100", "200", 1)
    db.execute("UPDATE synthetic_us_cost_allocations SET state_json='{}'")
    db.commit()
    before = dump(db)
    with pytest.raises(ValueError, match="disagrees with replay"):
        projection(db)
    assert dump(db) == before and not db.in_transaction


def test_attached_file_database_refuses(db, tmp_path):
    db.execute("ATTACH DATABASE ? AS external", (str(tmp_path / "external.sqlite"),))
    with pytest.raises(ValueError, match="in-memory"):
        projection(db)


def test_missing_schema_refuses_without_initialization():
    missing = sqlite3.connect(":memory:")
    try:
        with pytest.raises(sqlite3.OperationalError):
            projection(missing)
        assert missing.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
        assert not missing.in_transaction
    finally:
        missing.close()
