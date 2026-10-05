"""Synthetic US mock observations; no broker, runtime paths, or economic writes."""
import json
import sqlite3
from dataclasses import replace
from decimal import Decimal, localcontext

import pytest

from src.data.order_identity import IDENTITY_INDEX, IDENTITY_SCHEMA
from src.data.us_cumulative_execution import (
    UsCumulativeObservationStore,
    initialize_cumulative_observation_schema,
    normalize_cumulative_observations,
)

DATE = "20261002"
STAMP = "2026-10-04T01:00:00+00:00"
NEXT = "2026-10-04T01:01:00+00:00"
NUMBER = "000000042"


def database(path=":memory:"):
    db = sqlite3.connect(path)
    db.execute(IDENTITY_SCHEMA)
    db.execute(IDENTITY_INDEX)
    db.execute("CREATE TABLE pending_orders (order_uid TEXT PRIMARY KEY,account_id TEXT,"
               "ord_no TEXT,symbol TEXT,side TEXT,requested_qty REAL,filled_qty REAL,status TEXT)")
    db.execute("CREATE TABLE trade_ledger (id TEXT PRIMARY KEY,qty REAL,price REAL)")
    db.execute("INSERT INTO trade_ledger VALUES ('sentinel',1,99)")
    db.execute("PRAGMA user_version=2")
    db.commit()
    return db


def order(db, uid="first", *, date=DATE, number=NUMBER, account="us_mock", market="US",
          status="confirmed", symbol="AAPL", side="BUY"):
    db.execute("INSERT INTO order_identities VALUES (?,?,?,?,?,?,?,?,?)",
               (uid, account, market, number, symbol, side, date, status, STAMP))
    db.execute("INSERT INTO pending_orders VALUES (?,?,?,?,?,?,?,?)",
               (uid, account, number, symbol, side, 5, 0, "open"))
    db.commit()


def observations(qty="2", average="100.0000", *, stamp=STAMP, date=DATE, **changes):
    row = {"ord_no": NUMBER, "stk_cd": "AAPL", "slby_tp_nm": "매수", "crnc_code": "USD",
           "cntr_qty": qty, "cntr_uv": average, "cntr_time": "10:00:00"}
    row.update(changes)
    return normalize_cumulative_observations({
        "return_code": 0, "_execution_pages_complete": True, "_query_order_date": date,
        "result_list": [row],
    }, account_id="us_mock", query_order_date=date, observed_at_utc=stamp)


@pytest.fixture
def store():
    db = database()
    order(db)
    initialize_cumulative_observation_schema(db)
    try:
        yield UsCumulativeObservationStore(db)
    finally:
        db.close()


def checkpoints(db):
    return db.execute("SELECT * FROM us_cumulative_observations ORDER BY order_uid").fetchall()


def economic_state(db):
    return (db.execute("SELECT * FROM pending_orders ORDER BY order_uid").fetchall(),
            db.execute("SELECT * FROM trade_ledger ORDER BY id").fetchall())


def test_weighted_average_uses_monetary_delta_without_writing_a_fill(store):
    before = economic_state(store.db)
    first = store.observe(observations())
    second = store.observe(observations("5", "106.0000", stamp=NEXT))
    assert first.deltas[0].quantity == Decimal("2")
    assert first.deltas[0].amount == Decimal("200")
    assert second.deltas[0].quantity == Decimal("3")
    assert second.deltas[0].amount == Decimal("330")
    assert not second.economic_ingestion_allowed
    assert second.deltas[0].execution_date_status == "unresolved"
    assert not second.deltas[0].economic_ingestion_allowed
    assert checkpoints(store.db)[0][4:7] == ("5", "106", "530")
    assert economic_state(store.db) == before


def test_first_late_snapshot_stays_an_unattributed_aggregate(store):
    result = store.observe(observations("5", "106.0000"))
    assert result.deltas[0].amount == Decimal("530")
    assert result.deltas[0].execution_date_status == "unresolved"
    assert not result.economic_ingestion_allowed


def test_duplicate_and_reopen_preserve_observed_baseline(tmp_path):
    path = tmp_path / "synthetic-observations.sqlite"
    db = database(path)
    order(db)
    initialize_cumulative_observation_schema(db)
    UsCumulativeObservationStore(db).observe(observations())
    db.close()
    db = sqlite3.connect(path)
    try:
        result = UsCumulativeObservationStore(db).observe(observations(stamp=NEXT))
        assert result.deltas[0].outcome == "duplicate"
        assert result.deltas[0].quantity == 0
        assert result.deltas[0].amount == 0
        assert db.execute("SELECT COUNT(*) FROM trade_ledger").fetchone()[0] == 1
    finally:
        db.close()


@pytest.mark.parametrize("qty,average,reason", [
    ("1", "100.0000", "cumulative_quantity_decreased"),
    ("2", "101.0000", "amount_changed_without_quantity"),
    ("3", "50.0000", "nonpositive_incremental_amount"),
    ("6", "100.0000", "quantity_exceeds_requested"),
])
def test_regression_and_correction_latch_without_advancing_checkpoint(store, qty, average, reason):
    store.observe(observations())
    baseline = checkpoints(store.db)
    result = store.observe(observations(qty, average, stamp=NEXT))
    assert result.state == "conflict"
    assert result.conflicts == (("first", reason),)
    assert result.deltas == ()
    assert checkpoints(store.db) == baseline
    replay = store.observe(observations("5", "106.0000", stamp="2026-10-04T01:02:00Z"))
    assert replay.conflicts == (("first", "durable_conflict_already_present"),)
    assert checkpoints(store.db) == baseline
    assert store.db.execute("SELECT reason FROM us_cumulative_observation_conflicts").fetchone()[0] == reason


def test_one_conflict_blocks_all_other_checkpoint_advances(store):
    order(store.db, "second", number="000000043")
    store.observe(observations())
    baseline = checkpoints(store.db)
    batch = (*observations("6", "100.0000", stamp=NEXT),
             *observations("1", "90.0000", stamp=NEXT, ord_no="000000043"))
    assert store.observe(batch).state == "conflict"
    assert checkpoints(store.db) == baseline
    assert store.db.execute("SELECT outcome FROM us_cumulative_observation_audit "
                            "WHERE order_uid='second'").fetchone()[0] == "batch_blocked"


def test_order_number_reuse_across_dates_is_separate(store):
    order(store.db, "second", date="20261003")
    result = store.observe((*observations(), *observations("3", "90", date="20261003")))
    assert {delta.order_uid for delta in result.deltas} == {"first", "second"}
    assert len(checkpoints(store.db)) == 2


@pytest.mark.parametrize("identity", [
    {"account": "another_mock"}, {"market": "KR"}, {"status": "unresolved"},
    {"status": "conflict"}, {"symbol": "MSFT"}, {"side": "SELL"}, {"date": "20261003"},
])
def test_foreign_or_unconfirmed_identity_is_rejected_atomically(identity):
    db = database()
    try:
        order(db, **identity)
        initialize_cumulative_observation_schema(db)
        before = "\n".join(db.iterdump())
        with pytest.raises(ValueError, match="identity"):
            UsCumulativeObservationStore(db).observe(observations())
        assert "\n".join(db.iterdump()) == before
    finally:
        db.close()


@pytest.mark.parametrize("qty,price", [
    (2.0, "100"), (True, "100"), ("NaN", "100"), ("-1", "100"),
    ("2", "Infinity"), ("2", "1e2"), ("2", "0"), ("0", "100"),
])
def test_decimal_inputs_reject_lossy_or_invalid_values(qty, price):
    with pytest.raises(ValueError):
        observations(qty, price)


@pytest.mark.parametrize("changes", [
    {"ord_dt": "20261003"}, {"crnc_code": "KRW"}, {"slby_tp_nm": "unknown"},
    {"slby_tp_nm": []}, {"ord_no": "42"}, {"stk_cd": ""},
])
def test_response_scope_and_order_fields_are_required(changes):
    with pytest.raises(ValueError):
        observations(**changes)


@pytest.mark.parametrize("changes", [
    {"_execution_pages_complete": False}, {"_query_order_date": ""},
    {"return_code": 7}, {"return_code": False}, {"result_list": {}},
])
def test_incomplete_response_is_not_a_checkpoint_input(changes):
    data = {"return_code": 0, "_execution_pages_complete": True,
            "_query_order_date": DATE, "result_list": []}
    data.update(changes)
    with pytest.raises(ValueError):
        normalize_cumulative_observations(data, account_id="us_mock", query_order_date=DATE,
                                          observed_at_utc=STAMP)


def test_conflicting_same_order_rows_are_not_invented_individual_fills():
    a = json.loads(observations()[0].raw_json)
    b = {**a, "cntr_qty": "5", "cntr_uv": "106"}
    data = {"return_code": 0, "_execution_pages_complete": True,
            "_query_order_date": DATE, "result_list": [a, a]}
    assert len(normalize_cumulative_observations(data, account_id="us_mock", query_order_date=DATE,
                                               observed_at_utc=STAMP)) == 1
    data["result_list"] = [a, b]
    with pytest.raises(ValueError, match="Conflicting"):
        normalize_cumulative_observations(data, account_id="us_mock", query_order_date=DATE,
                                          observed_at_utc=STAMP)


def test_low_decimal_context_does_not_round_the_observation(store):
    with localcontext() as context:
        context.prec = 3
        first = store.observe(observations("2", "100.1234"))
        second = store.observe(observations("5", "106.2345", stamp=NEXT))
    assert first.deltas[0].amount == Decimal("200.2468")
    assert second.deltas[0].amount == Decimal("330.9257")


def test_rounded_average_amount_is_preserved_without_claiming_settlement(store):
    result = store.observe(observations("3", "100.6667"))
    assert result.deltas[0].amount == Decimal("302.0001")
    assert not result.economic_ingestion_allowed


@pytest.mark.parametrize("stamp,reason", [
    ("2026-10-04T00:59:00Z", "out_of_order_observation"),
    (STAMP, "conflicting_observation_at_same_time"),
])
def test_stale_or_conflicting_local_observation_time_latches(store, stamp, reason):
    store.observe(observations())
    result = store.observe(observations("3", "101", stamp=stamp))
    assert result.conflicts == (("first", reason),)


def test_public_dataclass_cannot_bypass_normalization(store):
    altered = replace(observations()[0], amount=Decimal("1"))
    with pytest.raises(ValueError, match="altered"):
        store.observe((altered,))
    assert not checkpoints(store.db)


def test_store_constructor_never_initializes_tables():
    db = database()
    try:
        before = "\n".join(db.iterdump())
        with pytest.raises(sqlite3.OperationalError):
            UsCumulativeObservationStore(db)
        assert "\n".join(db.iterdump()) == before
        initialize_cumulative_observation_schema(db)
        before = "\n".join(db.iterdump())
        with pytest.raises(sqlite3.OperationalError):
            initialize_cumulative_observation_schema(db)
        assert "\n".join(db.iterdump()) == before
    finally:
        db.close()


@pytest.mark.parametrize("table", ["us_cumulative_observations", "us_cumulative_observation_audit"])
def test_checkpoint_and_audit_are_atomic_on_sql_failure(store, table):
    store.db.execute(f"CREATE TRIGGER deny_write BEFORE INSERT ON {table} "
                     "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    store.db.commit()
    before = "\n".join(store.db.iterdump())
    with pytest.raises(sqlite3.IntegrityError):
        store.observe(observations())
    assert "\n".join(store.db.iterdump()) == before
    assert not store.db.in_transaction


def test_conflict_latch_failure_rolls_back_audit_and_preserves_checkpoint(store):
    store.observe(observations())
    store.db.execute("CREATE TRIGGER deny_conflict BEFORE INSERT ON us_cumulative_observation_conflicts "
                     "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    store.db.commit()
    before = "\n".join(store.db.iterdump())
    with pytest.raises(sqlite3.IntegrityError):
        store.observe(observations("6", "100", stamp=NEXT))
    assert "\n".join(store.db.iterdump()) == before


def test_caller_transaction_is_never_committed_or_rolled_back(store):
    store.db.execute("UPDATE pending_orders SET status='caller-owned'")
    with pytest.raises(ValueError, match="own transaction"):
        store.observe(observations())
    assert store.db.in_transaction
    assert store.db.execute("SELECT status FROM pending_orders").fetchone()[0] == "caller-owned"
    store.db.rollback()
