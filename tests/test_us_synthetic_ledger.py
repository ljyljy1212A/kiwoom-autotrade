"""Synthetic-only ledger regression cases; never use operational DBs."""
import sqlite3
from dataclasses import replace
from decimal import Decimal, localcontext

import pytest

from src.data.us_cumulative_execution import UsCumulativeObservationStore
from src.data.us_synthetic_ledger import (
    SyntheticDateEvidence, apply_synthetic_cumulative, initialize_synthetic_ledger,
)
from tests.test_us_cumulative_execution import (
    DATE, NEXT, STAMP, database, initialize_cumulative_observation_schema, observations, order,
)


@pytest.fixture
def db():
    connection = database()
    order(connection)
    connection.execute("ALTER TABLE pending_orders ADD COLUMN step INTEGER NOT NULL DEFAULT 2")
    connection.execute("ALTER TABLE pending_orders ADD COLUMN lifecycle_id TEXT NOT NULL DEFAULT 'synthetic-cycle-1'")
    connection.commit()
    initialize_cumulative_observation_schema(connection)
    initialize_synthetic_ledger(connection)
    yield connection
    connection.close()


def evidence(previous_q="0", previous_amount="0", q="2", amount="200"):
    return SyntheticDateEvidence("first", DATE, previous_q, previous_amount, q, amount, "synthetic-case-1")


def observe(db, q="2", average="100", stamp=STAMP, **changes):
    return UsCumulativeObservationStore(db).observe(observations(q, average, stamp=stamp, **changes))


def dump(db):
    return "\n".join(db.iterdump())


def test_cumulative_amount_delta_and_duplicate(db):
    observe(db)
    first = apply_synthetic_cumulative(db, "first", evidence())
    observe(db, "5", "106", NEXT)
    proof = evidence("2", "200", "5", "530")
    second = apply_synthetic_cumulative(db, "first", proof)
    before = dump(db)
    duplicate = apply_synthetic_cumulative(db, "first", proof)
    assert (first.quantity, first.gross_amount) == (Decimal(2), Decimal(200))
    assert (second.quantity, second.gross_amount) == (Decimal(3), Decimal(330))
    assert duplicate.duplicate and dump(db) == before
    assert not second.operational_ingestion_allowed
    assert db.execute("SELECT filled_qty,status FROM pending_orders").fetchone() == (5, "filled")
    assert db.execute("SELECT COUNT(*) FROM trade_ledger").fetchone()[0] == 1


def test_unapplied_observation_is_not_the_applied_baseline(db):
    observe(db)
    observe(db, "5", "106", NEXT)
    result = apply_synthetic_cumulative(db, "first", evidence(q="5", amount="530"))
    assert (result.quantity, result.gross_amount) == (Decimal(5), Decimal(530))


@pytest.mark.parametrize("changes", [
    {"execution_date": ""}, {"order_uid": "other"}, {"evidence_id": ""},
    {"kind": "query-date"}, {"cumulative_amount": "201"}, {"previous_quantity": "1"},
])
def test_invalid_full_delta_evidence_never_writes(db, changes):
    observe(db)
    before = dump(db)
    with pytest.raises(ValueError):
        apply_synthetic_cumulative(db, "first", replace(evidence(), **changes))
    assert dump(db) == before


@pytest.mark.parametrize("table", ["synthetic_us_fills", "synthetic_us_applied", "pending_orders"])
def test_every_economic_write_rolls_back_on_failure(db, table):
    observe(db)
    operation = "UPDATE" if table == "pending_orders" else "INSERT"
    db.execute(f"CREATE TRIGGER reject BEFORE {operation} ON {table} "
               "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    db.commit()
    before = dump(db)
    with pytest.raises(sqlite3.IntegrityError):
        apply_synthetic_cumulative(db, "first", evidence())
    assert dump(db) == before and not db.in_transaction


def test_existing_filled_order_cannot_guess_zero_amount(db):
    observe(db)
    db.execute("UPDATE pending_orders SET filled_qty=1")
    db.commit()
    before = dump(db)
    with pytest.raises(ValueError, match="baseline"):
        apply_synthetic_cumulative(db, "first", evidence())
    assert dump(db) == before


def test_observation_conflict_blocks_economic_apply(db):
    observe(db)
    observe(db, "1", "100", NEXT)
    before = dump(db)
    with pytest.raises(ValueError, match="conflict"):
        apply_synthetic_cumulative(db, "first", evidence())
    assert dump(db) == before


def test_decimal_amount_is_preserved_under_low_context(db):
    observe(db, "3", "100.6667")
    with localcontext() as context:
        context.prec = 3
        result = apply_synthetic_cumulative(db, "first", evidence(q="3", amount="302.0001"))
    assert result.gross_amount == Decimal("302.0001")


def test_tranche_change_and_corrupt_baseline_refuse(db):
    observe(db)
    apply_synthetic_cumulative(db, "first", evidence())
    observe(db, "5", "106", NEXT)
    db.execute("UPDATE pending_orders SET step=3")
    db.commit()
    before = dump(db)
    with pytest.raises(ValueError, match="tranche"):
        apply_synthetic_cumulative(db, "first", evidence("2", "200", "5", "530"))
    assert dump(db) == before


def test_file_database_refuses_before_any_schema_write(tmp_path):
    connection = sqlite3.connect(tmp_path / "synthetic.sqlite")
    try:
        with pytest.raises(ValueError, match="in-memory"):
            initialize_synthetic_ledger(connection)
        assert connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
    finally:
        connection.close()


def test_caller_transaction_is_preserved(db):
    observe(db)
    db.execute("UPDATE pending_orders SET status='caller-owned'")
    with pytest.raises(ValueError, match="own transaction"):
        apply_synthetic_cumulative(db, "first", evidence())
    assert db.in_transaction
    assert db.execute("SELECT status FROM pending_orders").fetchone()[0] == "caller-owned"
    db.rollback()


def test_corrupt_applied_amount_is_not_repaired(db):
    observe(db)
    apply_synthetic_cumulative(db, "first", evidence())
    db.execute("UPDATE synthetic_us_applied SET amount='199'")
    db.commit()
    before = dump(db)
    with pytest.raises(ValueError, match="persisted fills"):
        apply_synthetic_cumulative(db, "first", evidence())
    assert dump(db) == before


def test_sell_uses_its_bound_tranche_and_preserves_cancelled_partial_state(db):
    fund_tranche(db)
    db.execute("UPDATE order_identities SET side='SELL' WHERE order_uid='first'")
    db.execute("UPDATE pending_orders SET side='SELL',status='cancelled' WHERE order_uid='first'")
    db.commit()
    observe(db, slby_tp_nm="매도")
    result = apply_synthetic_cumulative(db, "first", evidence())
    assert result.quantity == Decimal(2)
    assert db.execute("SELECT side,step,gross_amount FROM synthetic_us_fills WHERE order_uid='first'").fetchone() == ("SELL", 2, "200")
    assert db.execute("SELECT status FROM pending_orders WHERE order_uid='first'").fetchone()[0] == "cancelled"


def fund_tranche(db, *, qty="5", step=2, cycle="synthetic-cycle-1", symbol="AAPL",
                 execution_date="20261001", sequence=None):
    db.execute("INSERT INTO order_identities VALUES (?,?,?,?,?,?,?,?,?)",
               ("funding", "us_mock", "US", "000000043", symbol, "BUY", execution_date, "confirmed", STAMP))
    db.execute("INSERT INTO pending_orders "
               "(order_uid,account_id,ord_no,symbol,side,requested_qty,filled_qty,status,step,lifecycle_id) "
               "VALUES (?,?,?,?,?,5,0,'open',?,?)",
               ("funding", "us_mock", "000000043", symbol, "BUY", step, cycle))
    db.commit()
    observe(db, qty, "100", ord_no="000000043", stk_cd=symbol, date=execution_date)
    proof = replace(evidence(q=qty, amount=str(Decimal(qty) * 100)), order_uid="funding",
                    execution_date=execution_date, execution_sequence=sequence)
    apply_synthetic_cumulative(db, "funding", proof)


@pytest.mark.parametrize("funding", [None, {"qty": "1"}, {"step": 3},
                                    {"cycle": "old-cycle"}, {"symbol": "MSFT"}])
def test_sell_cannot_borrow_missing_other_tranche_cycle_or_symbol_ownership(db, funding):
    if funding is not None:
        fund_tranche(db, **funding)
    db.execute("UPDATE order_identities SET side='SELL' WHERE order_uid='first'")
    db.execute("UPDATE pending_orders SET side='SELL' WHERE order_uid='first'")
    db.commit()
    observe(db, slby_tp_nm="매도")
    before = dump(db)
    with pytest.raises(ValueError, match="owned tranche"):
        apply_synthetic_cumulative(db, "first", evidence())
    assert dump(db) == before


def test_partial_sell_consumes_ownership_once_and_rejects_excess(db):
    fund_tranche(db, qty="3")
    db.execute("UPDATE order_identities SET side='SELL' WHERE order_uid='first'")
    db.execute("UPDATE pending_orders SET side='SELL' WHERE order_uid='first'")
    db.commit()
    observe(db, slby_tp_nm="매도")
    first_proof = replace(evidence(), execution_sequence=1)
    apply_synthetic_cumulative(db, "first", first_proof)
    apply_synthetic_cumulative(db, "first", first_proof)
    observe(db, "5", "106", NEXT, slby_tp_nm="매도")
    before = dump(db)
    with pytest.raises(ValueError, match="owned tranche"):
        apply_synthetic_cumulative(db, "first", replace(
            evidence("2", "200", "5", "530"), execution_sequence=2,
        ))
    assert dump(db) == before


def test_corrupt_owner_baseline_refuses_sell(db):
    fund_tranche(db)
    db.execute("UPDATE synthetic_us_applied SET amount='1' WHERE order_uid='funding'")
    db.execute("UPDATE order_identities SET side='SELL' WHERE order_uid='first'")
    db.execute("UPDATE pending_orders SET side='SELL' WHERE order_uid='first'")
    db.commit()
    observe(db, slby_tp_nm="매도")
    before = dump(db)
    with pytest.raises(ValueError, match="owner baseline"):
        apply_synthetic_cumulative(db, "first", evidence())
    assert dump(db) == before


def prepare_sell(db):
    db.execute("UPDATE order_identities SET side='SELL' WHERE order_uid='first'")
    db.execute("UPDATE pending_orders SET side='SELL' WHERE order_uid='first'")
    db.commit()
    observe(db, slby_tp_nm="매도")


def test_future_buy_cannot_fund_historical_sell(db):
    fund_tranche(db, execution_date="20261003", sequence=1)
    prepare_sell(db)
    before = dump(db)
    with pytest.raises(ValueError, match="Later dated"):
        apply_synthetic_cumulative(db, "first", replace(evidence(), execution_sequence=2))
    assert dump(db) == before


@pytest.mark.parametrize("buy_sequence,sell_sequence", [(None, None), (None, 2), (1, None), (2, 1), (2, 2)])
def test_same_day_sell_requires_strict_confirmed_sequence(db, buy_sequence, sell_sequence):
    fund_tranche(db, execution_date=DATE, sequence=buy_sequence)
    prepare_sell(db)
    before = dump(db)
    with pytest.raises(ValueError, match="Same-day"):
        apply_synthetic_cumulative(db, "first", replace(evidence(), execution_sequence=sell_sequence))
    assert dump(db) == before


def test_same_day_sell_with_synthetic_sequence_and_duplicate(db):
    fund_tranche(db, execution_date=DATE, sequence=1)
    prepare_sell(db)
    proof = replace(evidence(), execution_sequence=2)
    result = apply_synthetic_cumulative(db, "first", proof)
    assert result.quantity == Decimal(2)
    before = dump(db)
    assert apply_synthetic_cumulative(db, "first", proof).duplicate
    assert dump(db) == before


def test_prior_sell_also_requires_same_day_sequence(db):
    fund_tranche(db)
    prepare_sell(db)
    apply_synthetic_cumulative(db, "first", replace(evidence(), execution_sequence=2))
    observe(db, "5", "106", NEXT, slby_tp_nm="매도")
    before = dump(db)
    with pytest.raises(ValueError, match="Same-day"):
        apply_synthetic_cumulative(db, "first", replace(
            evidence("2", "200", "5", "530"), execution_sequence=1,
        ))
    assert dump(db) == before


def test_sell_delta_cannot_precede_an_already_applied_sell(db):
    fund_tranche(db)
    prepare_sell(db)
    apply_synthetic_cumulative(db, "first", replace(evidence(), execution_date="20261003"))
    observe(db, "5", "106", NEXT, slby_tp_nm="매도")
    before = dump(db)
    with pytest.raises(ValueError, match="Later dated"):
        apply_synthetic_cumulative(db, "first", evidence("2", "200", "5", "530"))
    assert dump(db) == before


@pytest.mark.parametrize("sequence", [True, 0, -1, 1.5, "2"])
def test_invalid_execution_sequence_refuses_without_writes(db, sequence):
    observe(db)
    before = dump(db)
    with pytest.raises(ValueError, match="positive integer"):
        apply_synthetic_cumulative(db, "first", replace(evidence(), execution_sequence=sequence))
    assert dump(db) == before
