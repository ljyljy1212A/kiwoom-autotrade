"""Scratch sink regression fixtures; no Engine, broker or operational DB."""
from dataclasses import replace
from decimal import Decimal
import json
import sqlite3

import pytest

from src.core.us_observation_interface import (
    UsObservationAdapter, UsObservationResponse, UsTrackedObservationOrder, _cycle,
)
from src.data.order_identity import OrderIdentity
from src.data.us_synthetic_observation_journal import (
    SyntheticJournalIdentity, create_synthetic_journal, reopen_synthetic_journal,
)
from src.data.us_synthetic_observation_sink import SyntheticObservationSink
from tests.test_us_cumulative_execution import DATE, STAMP, NEXT, NUMBER
from tests.test_us_synthetic_response_adapter import response


def tracked(uid="first", date=DATE, number=NUMBER):
    # Explicit synthetic fixture construction, never promotion of live evidence.
    return UsTrackedObservationOrder(OrderIdentity(uid, "us_mock", "US", number,
                                                   "AAPL", "BUY", date, "confirmed", STAMP), "5")


def supplied(quantity="2", average="100.0000", *, date=DATE, number=NUMBER, stamp=STAMP):
    body = dict(response(quantity, average, ord_no=number), _query_order_date=date)
    return UsObservationResponse(date, stamp, body)


def cycle(orders=None, responses=None):
    return _cycle("us_mock", "US", (tracked(),) if orders is None else orders,
                  (supplied(),) if responses is None else responses)


def bind(journal, order):
    identity = order.identity
    journal.bind(SyntheticJournalIdentity(identity.order_uid, identity.broker_order_date,
                                          identity.ord_no, identity.symbol, identity.side,
                                          order.requested_quantity, identity.submitted_at_utc))


def dump(journal):
    return "\n".join(journal.db.iterdump())


@pytest.fixture
def prepared(tmp_path):
    root = tmp_path / "scratch-sink"
    root.mkdir()
    journal = create_synthetic_journal(root, allow_root=tmp_path)
    bind(journal, tracked())
    try:
        yield journal, root, tmp_path
    finally:
        journal.close()


def test_adapter_commits_exact_observation_without_economic_state(prepared):
    journal, root, allowed = prepared
    adapter = UsObservationAdapter(account_id="us_mock", market="US", enabled=True,
                                   sink=SyntheticObservationSink(journal))
    trace = []
    journal.db.set_trace_callback(trace.append)
    result = adapter.observe_cycle(orders=(tracked(),), responses=(supplied(),))
    journal.db.set_trace_callback(None)
    assert result.state == "OBSERVED" and result.persistence_confirmed and result.allow_sync_continue
    assert not result.economic_ingestion_allowed and not result.operational_trading_allowed
    assert sum(sql == "BEGIN IMMEDIATE" for sql in trace) == 1
    assert sum(sql == "COMMIT" for sql in trace) == 1
    assert journal.recover().observations[0][4:7] == ("2", "100", "200")
    assert journal.db.execute("SELECT filled_qty,status FROM pending_orders").fetchone() == (0, "open")
    assert not journal.db.execute("SELECT 1 FROM sqlite_master WHERE name='trade_ledger'").fetchone()
    audit = json.loads(journal.db.execute("SELECT observation_json FROM us_cumulative_observation_audit").fetchone()[0])
    assert audit["raw"]["cntr_uv"] == "100.0000"
    assert audit["cycle_context"]["cycle_token"] == result.cycle_token
    reopened = reopen_synthetic_journal(root, allow_root=allowed)
    try:
        assert reopened.recover() == journal.recover()
    finally:
        reopened.close()


def test_multiple_dates_share_one_commit(prepared):
    journal, _, _ = prepared
    second = tracked("second", "20261001", "000000043")
    bind(journal, second)
    trace = []
    journal.db.set_trace_callback(trace.append)
    receipt = SyntheticObservationSink(journal).record_cycle(cycle(
        (tracked(), second), (supplied(), supplied(date="20261001", number="000000043")),
    ))
    journal.db.set_trace_callback(None)
    assert receipt.state == "OBSERVED" and receipt.committed
    assert sum(sql == "COMMIT" for sql in trace) == 1
    assert len(journal.recover().observations) == 2


@pytest.mark.parametrize("change", ["uid", "request", "token", "amount", "economic_flag"])
def test_forged_cycle_or_db_binding_refuses_without_writes(prepared, change):
    journal, _, _ = prepared
    candidate = cycle()
    if change == "uid":
        candidate = cycle((replace(tracked(), identity=replace(tracked().identity, order_uid="foreign")),))
    elif change == "request":
        candidate = cycle((replace(tracked(), requested_quantity="6"),))
    elif change == "token":
        candidate = replace(candidate, cycle_token="0" * 64)
    elif change == "amount":
        candidate = replace(candidate, observations=(replace(candidate.observations[0], amount=Decimal(999)),))
    else:
        candidate = replace(candidate, economic_ingestion_allowed=True)
    before = dump(journal)
    with pytest.raises(ValueError):
        SyntheticObservationSink(journal).record_cycle(candidate)
    assert dump(journal) == before and not journal.db.in_transaction


def test_new_conflict_blocks_all_dates_and_survives_reopen(prepared):
    journal, root, allowed = prepared
    second = tracked("second", "20261001", "000000043")
    bind(journal, second)
    candidate = cycle((tracked(), second), (supplied("6"), supplied(date="20261001", number="000000043")))
    result = SyntheticObservationSink(journal).record_cycle(candidate)
    assert result.state == "CONFLICT" and ("first", "quantity_exceeds_requested") in result.conflicts
    assert not journal.recover().observations
    assert {row[0] for row in journal.db.execute("SELECT outcome FROM us_cumulative_observation_audit")} == {
        "conflict", "batch_blocked",
    }
    reopened = reopen_synthetic_journal(root, allow_root=allowed)
    try:
        assert reopened.recover().state == "HELD" and not reopened.recover().observations
    finally:
        reopened.close()


def test_absent_tracked_conflict_blocks_other_observation(prepared):
    journal, _, _ = prepared
    second = tracked("second", "20261001", "000000043")
    bind(journal, second)
    sink = SyntheticObservationSink(journal)
    sink.record_cycle(cycle(responses=(supplied("6"),)))
    empty = replace(supplied(), body=dict(response(), result_list=[]))
    result = sink.record_cycle(cycle((tracked(), second), (
        empty, supplied(date="20261001", number="000000043"),
    )))
    assert result.state == "CONFLICT" and ("first", "quantity_exceeds_requested") in result.conflicts
    assert not journal.recover().observations
    assert journal.db.execute("SELECT outcome FROM us_cumulative_observation_audit WHERE order_uid='second'").fetchone() == (
        "batch_blocked",
    )


def test_existing_conflict_does_not_hide_required_conflict_reason(prepared):
    journal, _, _ = prepared
    sink = SyntheticObservationSink(journal)
    sink.record_cycle(cycle())
    sink.record_cycle(cycle(responses=(supplied(average="101", stamp=NEXT),)))
    adapter = UsObservationAdapter(account_id="us_mock", market="US", enabled=True, sink=sink)
    result = adapter.observe_cycle(orders=(tracked(),), responses=(supplied("6", stamp=NEXT),))
    assert result.state == "CONFLICT" and result.persistence_confirmed and not result.allow_sync_continue
    assert ("first", "amount_changed_without_quantity") in result.conflicts
    assert ("first", "quantity_exceeds_requested") in result.conflicts
    audit = json.loads(journal.db.execute(
        "SELECT observation_json FROM us_cumulative_observation_audit WHERE outcome='conflict' ORDER BY rowid DESC",
    ).fetchone()[0])
    assert ["first", "quantity_exceeds_requested"] in audit["cycle_context"]["required_conflicts"]
    assert journal.recover().observations[0][4:7] == ("2", "100", "200")


class FaultingConnection:
    def __init__(self, db, fragment, fail_at=1):
        self.db, self.fragment = db, fragment
        self.fail_at, self.matches = fail_at, 0

    def __getattr__(self, name):
        return getattr(self.db, name)

    def execute(self, sql, *args):
        if self.fragment in sql:
            self.matches += 1
            if self.matches == self.fail_at:
                raise sqlite3.OperationalError("synthetic sink SQL failure")
        return self.db.execute(sql, *args)

    def commit(self):
        if self.fragment == "COMMIT":
            raise sqlite3.OperationalError("synthetic sink commit failure")
        return self.db.commit()


@pytest.mark.parametrize("fragment", ["INSERT INTO us_cumulative_observation_audit",
                                      "INSERT INTO us_cumulative_observations",
                                      "INSERT INTO us_cumulative_observation_conflicts", "COMMIT"])
def test_write_or_commit_failure_rolls_back_whole_cycle(prepared, fragment):
    journal, _, _ = prepared
    second = tracked("second", "20261001", "000000043")
    bind(journal, second)
    candidate = cycle((tracked(), second), (
        supplied("6" if "conflicts" in fragment else "2"),
        supplied(date="20261001", number="000000043"),
    ))
    before = dump(journal)
    real_db = journal.db
    journal.db = journal.store.db = FaultingConnection(real_db, fragment)
    adapter = UsObservationAdapter(account_id="us_mock", market="US", enabled=True,
                                   sink=SyntheticObservationSink(journal))
    try:
        result = adapter.observe_cycle(orders=(tracked(), second), responses=tuple(
            supplied("6" if "conflicts" in fragment else "2") if date == DATE else
            supplied(date=date, number="000000043") for date, _ in candidate.query_contexts
        ))
        assert result.state == "INCOMPLETE" and not result.persistence_confirmed and not result.allow_sync_continue
        assert dump(journal) == before and not journal.db.in_transaction
    finally:
        journal.db = journal.store.db = real_db


def test_empty_response_is_observation_only_and_keeps_pending_open(prepared):
    journal, _, _ = prepared
    empty = replace(supplied(), body=dict(response(), result_list=[]))
    receipt = SyntheticObservationSink(journal).record_cycle(cycle(responses=(empty,)))
    assert receipt.state == "OBSERVED" and receipt.committed and not receipt.economic_writes
    assert not journal.recover().observations
    assert journal.db.execute("SELECT filled_qty,status FROM pending_orders").fetchone() == (0, "open")


def test_failure_after_first_order_write_rolls_back_first_order_too(prepared):
    journal, _, _ = prepared
    second = tracked("second", "20261001", "000000043")
    bind(journal, second)
    before = dump(journal)
    real_db = journal.db
    journal.db = journal.store.db = FaultingConnection(
        real_db, "INSERT INTO us_cumulative_observations", fail_at=2,
    )
    try:
        with pytest.raises(sqlite3.OperationalError):
            SyntheticObservationSink(journal).record_cycle(cycle(
                (tracked(), second), (supplied(), supplied(date="20261001", number="000000043")),
            ))
        assert journal.db.matches == 2
        assert dump(journal) == before and not journal.db.in_transaction
    finally:
        journal.db = journal.store.db = real_db


def test_duplicate_preserves_checkpoint_and_adds_only_observation_audit(prepared):
    journal, _, _ = prepared
    sink = SyntheticObservationSink(journal)
    sink.record_cycle(cycle())
    before = journal.recover()
    assert sink.record_cycle(cycle()).state == "OBSERVED"
    assert journal.recover() == before
    assert journal.db.execute("SELECT COUNT(*) FROM us_cumulative_observation_audit").fetchone() == (2,)
    assert journal.db.execute("SELECT filled_qty,status FROM pending_orders").fetchone() == (0, "open")


def test_existing_transaction_is_not_rolled_back_by_sink(prepared):
    journal, _, _ = prepared
    journal.db.execute("BEGIN")
    try:
        with pytest.raises(ValueError):
            SyntheticObservationSink(journal).record_cycle(cycle())
        assert journal.db.in_transaction
    finally:
        journal.db.rollback()


def test_corrupt_journal_refuses_without_repair(prepared):
    journal, _, _ = prepared
    journal.db.execute("UPDATE pending_orders SET filled_qty=1")
    journal.db.commit()
    before = dump(journal)
    with pytest.raises(ValueError):
        SyntheticObservationSink(journal).record_cycle(cycle())
    assert dump(journal) == before


def test_unprepared_backend_and_unowned_helpers_refuse(prepared):
    journal, _, _ = prepared
    with pytest.raises(ValueError):
        SyntheticObservationSink(object())
    with pytest.raises(ValueError):
        journal.store._observe_in_transaction(())
    with pytest.raises(ValueError):
        journal._recover_in_transaction()
