"""Dedicated temporary SQLite tests; no runtime configuration or broker calls."""
from dataclasses import replace
from pathlib import Path
import socket
import sqlite3

import pytest

from src.core.us_observation_interface import (
    UsObservationAdapter, UsObservationResponse, UsTrackedObservationOrder, _cycle,
)
from src.data.order_identity import OrderIdentity
from src.data import us_operational_observation_store as module
from src.data.us_cumulative_execution import CONTRACT
from src.data.us_operational_observation_store import (
    POLICY, SCHEMA, SCHEMA_VERSION, OperationalObservationHead,
    OperationalUsObservationStore,
)

DATE = "20261002"
STAMP = "2026-10-02T20:00:00+00:00"
LATER = "2026-10-02T20:01:00+00:00"
JOURNAL = "a" * 32
BINDING = "b" * 32


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Network is outside this temporary database test")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)


@pytest.fixture
def db(tmp_path):
    connection = sqlite3.connect(tmp_path / "observations.sqlite")
    for statement in SCHEMA:
        connection.execute(statement)
    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    connection.execute("INSERT INTO us_observation_meta VALUES (1,?,?,'us_mock','US',?,?)",
                       (JOURNAL, BINDING, POLICY, CONTRACT))
    connection.commit()
    yield connection
    connection.close()


def store(db, **kwargs):
    path = db.execute("PRAGMA database_list").fetchone()[2]
    options = dict(expected_path=path, journal_id=JOURNAL, binding_id=BINDING, enabled=True)
    options.update(kwargs)
    return OperationalUsObservationStore(db, **options)


def empty_head():
    return OperationalObservationHead(JOURNAL, BINDING, 0, "0" * 64)


def ready(db):
    sink = store(db)
    result = sink.recover(expected_head=empty_head())
    assert result.state == "OBSERVATION_VALIDATED" and result.anchor_verified
    assert not result.economic_ingestion_allowed and not result.operational_trading_allowed
    return sink


def inputs(*, qty="2", price="100.0000", stamp=STAMP, uid="buy-1", date=DATE,
           number="000000100", request="5", symbol="AAPL"):
    tracked = UsTrackedObservationOrder(OrderIdentity(
        uid, "us_mock", "US", number, symbol, "BUY", date, "confirmed", STAMP,
    ), request)
    row = {"ord_no": number, "stk_cd": symbol, "slby_tp_nm": "매수", "crnc_code": "USD",
           "cntr_qty": qty, "cntr_uv": price, "ord_dt": date}
    response = UsObservationResponse(date, stamp, {
        "return_code": 0, "result_list": [row], "_execution_pages_complete": True, "_query_order_date": date,
    })
    return (tracked,), (response,)


def cycle(**kwargs):
    orders, responses = inputs(**kwargs)
    return _cycle("us_mock", "US", orders, responses)


def count(db):
    return db.execute("SELECT count(*) FROM us_observation_cycles").fetchone()[0]


def test_import_origin_is_the_selected_source_checkout():
    expected = Path(__file__).resolve().parents[1] / "src" / "data" / "us_operational_observation_store.py"
    assert Path(module.__file__).resolve() == expected


def test_constructor_only_inspects_prepared_database(db):
    before = db.iterdump()
    baseline = tuple(before)
    sink = store(db)
    assert tuple(db.iterdump()) == baseline
    assert not db.in_transaction
    with pytest.raises(ValueError, match="restart inspection"):
        sink.record_cycle(cycle())
    assert count(db) == 0


def test_disabled_sink_never_writes_even_with_verified_anchor(db):
    sink = store(db, enabled=False)
    assert sink.recover(expected_head=empty_head()).anchor_verified
    with pytest.raises(ValueError, match="activation"):
        sink.record_cycle(cycle())
    assert count(db) == 0


def test_record_receipt_and_duplicate_are_observation_only(db):
    sink = ready(db)
    first = sink.record_cycle(cycle())
    duplicate = sink.record_cycle(cycle())
    assert first == duplicate and count(db) == 1
    assert first.state == "OBSERVED" and first.committed and not first.economic_writes
    assert first.head.sequence == 1
    result = sink.recover(expected_head=first.head)
    assert result.state == "OBSERVATION_VALIDATED" and result.anchor_verified
    assert result.execution_date_status == "unresolved"
    assert not result.economic_ingestion_allowed and not result.operational_trading_allowed
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"us_observation_meta", "us_observation_cycles"}


def test_adapter_accepts_extended_committed_receipt(db):
    adapter = UsObservationAdapter(account_id="us_mock", market="US", enabled=True, sink=ready(db))
    orders, responses = inputs()
    result = adapter.observe_cycle(orders=orders, responses=responses)
    assert result.state == "OBSERVED" and result.persistence_confirmed
    assert not result.economic_ingestion_allowed and not result.operational_trading_allowed


def test_reopen_requires_independent_committed_head(db):
    receipt = ready(db).record_cycle(cycle())
    path = db.execute("PRAGMA database_list").fetchone()[2]
    with sqlite3.connect(f"{Path(path).as_uri()}?mode=rw", uri=True) as reopened:
        sink = store(reopened)
        assert sink.recover(expected_head=receipt.head).state == "OBSERVATION_VALIDATED"
        second = sink.record_cycle(cycle(qty="5", price="101", stamp=LATER))
        assert second.head.sequence == 2 and count(reopened) == 2


@pytest.mark.parametrize("anchor", [None, empty_head(),
    OperationalObservationHead("c" * 32, BINDING, 1, "0" * 64),
    OperationalObservationHead(JOURNAL, "c" * 32, 1, "0" * 64),
    OperationalObservationHead(JOURNAL, BINDING, True, "0" * 64),
])
def test_missing_stale_or_invalid_anchor_latches_failure(db, anchor):
    receipt = ready(db).record_cycle(cycle())
    sink = store(db)
    result = sink.recover(expected_head=anchor)
    assert result.state == "INCOMPLETE" and not result.anchor_verified
    assert sink.recover(expected_head=receipt.head).state == "INCOMPLETE"
    with pytest.raises(ValueError):
        sink.record_cycle(cycle(stamp=LATER))
    assert count(db) == 1


def test_complete_suffix_removal_is_detected_by_external_head(db):
    sink = ready(db)
    sink.record_cycle(cycle())
    receipt = sink.record_cycle(cycle(qty="5", price="101", stamp=LATER))
    db.execute("DELETE FROM us_observation_cycles WHERE sequence=2")
    db.commit()
    result = store(db).recover(expected_head=receipt.head)
    assert result.state == "INCOMPLETE" and not result.anchor_verified


@pytest.mark.parametrize("column,value", [
    ("sequence", 3), ("cycle_token", "f" * 64), ("previous_digest", "f" * 64),
    ("digest", "f" * 64), ("payload_json", "{}"), ("conflicts_json", '[ ["buy-1","fake"] ]'),
])
def test_corrupt_audit_is_rejected_on_reopen(db, column, value):
    ready(db).record_cycle(cycle())
    db.execute(f"UPDATE us_observation_cycles SET {column}=?", (value,))
    db.commit()
    with pytest.raises((ValueError, TypeError, KeyError)):
        store(db)
    assert not db.in_transaction


@pytest.mark.parametrize("changes", [
    {"journal_id": "c" * 32}, {"binding_id": "c" * 32}, {"journal_id": "bad"}, {"enabled": 1},
])
def test_pinned_metadata_and_boolean_activation_are_required(db, changes):
    with pytest.raises(ValueError):
        store(db, **changes)
    assert count(db) == 0


def test_wrong_path_does_not_create_replacement(db, tmp_path):
    absent = tmp_path / "absent.sqlite"
    with pytest.raises((ValueError, FileNotFoundError)):
        store(db, expected_path=absent)
    assert not absent.exists() and count(db) == 0


@pytest.mark.parametrize("statement", [
    "PRAGMA user_version=99", "DROP TABLE us_observation_cycles",
    "CREATE TABLE foreign_table (value TEXT)",
    "CREATE VIEW foreign_view AS SELECT * FROM us_observation_meta",
    "CREATE TRIGGER foreign_trigger AFTER INSERT ON us_observation_cycles BEGIN SELECT 1; END",
    "UPDATE us_observation_meta SET binding_id='changed'",
])
def test_missing_or_changed_schema_and_metadata_are_not_repaired(db, statement):
    db.execute(statement)
    db.commit()
    before = tuple(db.iterdump())
    with pytest.raises(ValueError):
        store(db)
    assert tuple(db.iterdump()) == before


def test_unprepared_database_is_not_initialized(tmp_path):
    path = tmp_path / "empty.sqlite"
    with sqlite3.connect(path) as connection:
        with pytest.raises(ValueError):
            store(connection)
        assert connection.execute("SELECT * FROM sqlite_master").fetchall() == []


def test_attached_database_is_refused(db, tmp_path):
    db.execute("ATTACH DATABASE ? AS foreign_db", (str(tmp_path / "attached.sqlite"),))
    with pytest.raises(ValueError):
        store(db)


def test_caller_transaction_is_not_rolled_back(db):
    db.execute("BEGIN")
    with pytest.raises(ValueError):
        store(db)
    assert db.in_transaction
    db.rollback()


@pytest.mark.parametrize("change", [
    {"cycle_token": "f" * 64}, {"economic_ingestion_allowed": True},
    {"required_conflicts": (("buy-1", "invented"),)}, {"account_id": "foreign"},
])
def test_forged_direct_cycles_never_write(db, change):
    sink = ready(db)
    with pytest.raises(ValueError):
        sink.record_cycle(replace(cycle(), **change))
    assert count(db) == 0


@pytest.mark.parametrize("next_cycle,reason", [
    ({"qty": "1", "stamp": LATER}, "cumulative_observation_conflict"),
    ({"price": "101", "stamp": LATER}, "cumulative_observation_conflict"),
    ({"qty": "3", "price": "50", "stamp": LATER}, "cumulative_observation_conflict"),
    ({"stamp": "2026-10-02T19:00:00+00:00"}, "cumulative_observation_conflict"),
    ({"symbol": "MSFT", "stamp": LATER}, "order_binding_changed"),
    ({"uid": "buy-2", "stamp": LATER}, "broker_order_binding_ambiguous"),
    ({"qty": "6", "stamp": LATER}, "quantity_exceeds_requested"),
])
def test_conflicts_are_committed_and_prevent_further_writes(db, next_cycle, reason):
    sink = ready(db)
    sink.record_cycle(cycle())
    receipt = sink.record_cycle(cycle(**next_cycle))
    assert receipt.state == "CONFLICT" and receipt.committed
    assert any(item[1] == reason for item in receipt.conflicts)
    assert count(db) == 2
    recovered = store(db).recover(expected_head=receipt.head)
    assert recovered.state == "CONFLICT" and recovered.conflicts
    assert not recovered.economic_ingestion_allowed and not recovered.operational_trading_allowed
    with pytest.raises(ValueError):
        sink.record_cycle(cycle(qty="5", stamp=LATER))


def test_order_number_reuse_on_other_date_has_distinct_binding(db):
    sink = ready(db)
    sink.record_cycle(cycle())
    receipt = sink.record_cycle(cycle(uid="buy-2", date="20261003", stamp="2026-10-03T20:00:00+00:00"))
    assert receipt.state == "OBSERVED" and count(db) == 2


def test_whole_cycle_rolls_back_on_insert_failure(db, monkeypatch):
    sink = ready(db)
    def failed(*args):
        raise sqlite3.OperationalError("synthetic insert preparation failure")
    monkeypatch.setattr(sink, "_digest", failed)
    adapter = UsObservationAdapter(account_id="us_mock", market="US", enabled=True, sink=sink)
    orders, responses = inputs()
    result = adapter.observe_cycle(orders=orders, responses=responses)
    assert result.state == "INCOMPLETE" and not result.persistence_confirmed
    assert count(db) == 0 and not db.in_transaction
    with pytest.raises(ValueError):
        sink.record_cycle(cycle())


def test_pagination_failure_is_rejected_before_storage(db):
    sink = ready(db)
    adapter = UsObservationAdapter(account_id="us_mock", market="US", enabled=True, sink=sink)
    orders, responses = inputs()
    body = dict(responses[0].body, _execution_pages_complete=False)
    result = adapter.observe_cycle(orders=orders, responses=(replace(responses[0], body=body),))
    assert result.state == "INCOMPLETE" and count(db) == 0
