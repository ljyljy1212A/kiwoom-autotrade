"""Scratch recovery fixtures only; no operational status or order authority."""
from dataclasses import FrozenInstanceError
import hashlib
import sqlite3

import pytest

from src.data.us_synthetic_observation_journal import (
    SyntheticJournalIdentity, create_synthetic_journal, reopen_synthetic_journal,
)
from src.data.us_synthetic_observation_recovery import SyntheticObservationRecoveryReader
from tests.test_us_cumulative_execution import DATE, STAMP, NUMBER
from tests.test_us_synthetic_response_adapter import response


def seed(journal, *, uid="old-generation", symbol="AAPL", date="20260901", number=NUMBER, conflict=True):
    journal.bind(SyntheticJournalIdentity(uid, date, number, symbol, "BUY", "5", STAMP))
    data = dict(response("6" if conflict else "2", stk_cd=symbol, ord_no=number), _query_order_date=date)
    return journal.observe(data, query_order_date=date, observed_at_utc=STAMP)


def recover(reader, symbol="AAPL"):
    return reader.recover_scope(account_id="us_mock", market="US", symbol=symbol)


def dump(journal):
    return "\n".join(journal.db.iterdump())


@pytest.fixture
def prepared(tmp_path):
    root = tmp_path / "scratch-recovery"
    root.mkdir()
    journal = create_synthetic_journal(root, allow_root=tmp_path)
    try:
        yield journal, root, tmp_path
    finally:
        journal.close()


def test_historical_conflict_recovered_without_current_tracking_list(prepared):
    journal, _, _ = prepared
    seed(journal)
    seed(journal, uid="current-generation", date=DATE, number="000000043", conflict=False)
    result = recover(SyntheticObservationRecoveryReader(journal))
    assert result.state == "CONFLICT" and result.blocking_scope == "symbol" and result.scope_complete
    assert len(result.conflicts) == 1 and result.journal_conflict_count == 1
    retained = result.conflicts[0]
    assert (retained.order_uid, retained.order_date, retained.ord_no) == ("old-generation", "20260901", NUMBER)
    assert retained.reason == "quantity_exceeds_requested"
    assert '"cntr_uv": "100.0000"' in retained.raw_json
    assert not result.economic_ingestion_allowed and not result.operational_trading_allowed
    assert result.execution_date_status == "unresolved"
    with pytest.raises(FrozenInstanceError):
        retained.reason = "cleared"


def test_other_symbol_conflict_is_validated_but_not_misassigned(prepared):
    journal, _, _ = prepared
    seed(journal, symbol="MSFT")
    reader = SyntheticObservationRecoveryReader(journal)
    clean = recover(reader)
    blocked = recover(reader, "MSFT")
    assert clean.state == "RECOVERED_NO_CONFLICT" and clean.scope_complete
    assert clean.blocking_scope == "none" and not clean.conflicts and clean.journal_conflict_count == 1
    assert blocked.state == "CONFLICT" and blocked.conflicts[0].symbol == "MSFT"
    assert not clean.operational_trading_allowed and not blocked.operational_trading_allowed


def test_same_order_number_on_different_dates_retains_both_uids(prepared):
    journal, _, _ = prepared
    seed(journal, uid="first", date="20260901")
    seed(journal, uid="second", date=DATE)
    result = recover(SyntheticObservationRecoveryReader(journal))
    assert {item.order_uid for item in result.conflicts} == {"first", "second"}
    assert {item.order_date for item in result.conflicts} == {"20260901", DATE}
    assert result.journal_conflict_count == 2


def test_empty_journal_is_not_trading_or_finality_permission(prepared):
    journal, _, _ = prepared
    before = dump(journal)
    result = recover(SyntheticObservationRecoveryReader(journal))
    assert result.state == "RECOVERED_NO_CONFLICT" and result.scope_complete
    assert result.journal_conflict_count == 0 and result.coverage == "validated-scratch-journal-only"
    assert not result.operational_trading_allowed and not result.economic_ingestion_allowed
    assert result.execution_date_status == "unresolved" and dump(journal) == before


def test_new_conflict_is_read_again_without_cache(prepared):
    journal, _, _ = prepared
    reader = SyntheticObservationRecoveryReader(journal)
    assert recover(reader).state == "RECOVERED_NO_CONFLICT"
    seed(journal)
    assert recover(reader).state == "CONFLICT"


def test_recovery_uses_one_read_transaction_and_keeps_bytes_unchanged(prepared):
    journal, root, _ = prepared
    seed(journal)
    target = root / "synthetic_observation_journal.sqlite"
    before_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    before = dump(journal)
    trace = []
    journal.db.set_trace_callback(trace.append)
    result = recover(SyntheticObservationRecoveryReader(journal))
    journal.db.set_trace_callback(None)
    assert result.state == "CONFLICT"
    assert sum(sql == "BEGIN" for sql in trace) == 1
    assert sum(sql == "ROLLBACK" for sql in trace) == 1
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "COMMIT"))
                   for sql in trace)
    assert dump(journal) == before and hashlib.sha256(target.read_bytes()).hexdigest() == before_hash
    assert not journal.db.in_transaction


def test_close_reopen_retains_historical_conflict_and_read_does_not_clear(prepared):
    journal, root, allowed = prepared
    seed(journal)
    before = recover(SyntheticObservationRecoveryReader(journal))
    journal.close()
    reopened = reopen_synthetic_journal(root, allow_root=allowed)
    try:
        reader = SyntheticObservationRecoveryReader(reopened)
        assert recover(reader) == before and recover(reader) == before
        assert reopened.db.execute("SELECT COUNT(*) FROM us_cumulative_observation_conflicts").fetchone() == (1,)
    finally:
        reopened.close()


@pytest.mark.parametrize("sql", [
    "DELETE FROM order_identities WHERE order_uid='old-generation'",
    "UPDATE order_identities SET symbol='UNKNOWN?' WHERE order_uid='old-generation'",
    "DELETE FROM us_cumulative_observation_audit",
    "UPDATE pending_orders SET status='cancelled'",
    "UPDATE pending_orders SET status='filled',filled_qty=5",
    "UPDATE synthetic_journal_meta SET schema_sha256='wrong'",
])
def test_invalid_ownership_audit_status_or_schema_blocks_account_without_repair(prepared, sql):
    journal, _, _ = prepared
    seed(journal)
    journal.db.execute(sql)
    journal.db.commit()
    before = dump(journal)
    result = recover(SyntheticObservationRecoveryReader(journal))
    assert result.state == "INCOMPLETE" and result.blocking_scope == "account"
    assert not result.scope_complete and result.journal_conflict_count is None and not result.conflicts
    assert result.reasons == ("scratch_recovery_failed",) and dump(journal) == before
    assert not journal.db.in_transaction


def test_corrupt_other_symbol_cannot_be_excluded_as_unrelated(prepared):
    journal, _, _ = prepared
    seed(journal, symbol="MSFT")
    journal.db.execute("DELETE FROM us_cumulative_observation_audit")
    journal.db.commit()
    result = recover(SyntheticObservationRecoveryReader(journal))
    assert result.state == "INCOMPLETE" and result.blocking_scope == "account"


def test_caller_transaction_is_preserved_on_refusal(prepared):
    journal, _, _ = prepared
    journal.db.execute("BEGIN")
    try:
        result = recover(SyntheticObservationRecoveryReader(journal))
        assert result.state == "INCOMPLETE" and result.blocking_scope == "account"
        assert journal.db.in_transaction
    finally:
        journal.db.rollback()


class ReadFailure:
    def __init__(self, db):
        self.db, self.failures = db, 0

    def __getattr__(self, name):
        return getattr(self.db, name)

    def execute(self, sql, *args):
        if sql == "SELECT * FROM order_identities":
            self.failures += 1
            raise sqlite3.OperationalError("sensitive synthetic read error")
        return self.db.execute(sql, *args)


def test_read_failure_is_sanitized_without_retry_or_repair(prepared):
    journal, _, _ = prepared
    seed(journal)
    before = dump(journal)
    real = journal.db
    failing = ReadFailure(real)
    journal.db = failing
    try:
        result = recover(SyntheticObservationRecoveryReader(journal))
        assert result.state == "INCOMPLETE" and result.blocking_scope == "account"
        assert result.reasons == ("scratch_recovery_failed",) and failing.failures == 1
        assert dump(journal) == before and not real.in_transaction
    finally:
        journal.db = real


def test_closed_journal_is_unresolved_and_does_not_reopen(prepared):
    journal, root, _ = prepared
    reader = SyntheticObservationRecoveryReader(journal)
    journal.close()
    before = (root / "synthetic_observation_journal.sqlite").read_bytes()
    result = recover(reader)
    assert result.state == "INCOMPLETE" and result.blocking_scope == "account"
    assert (root / "synthetic_observation_journal.sqlite").read_bytes() == before


def test_no_backend_is_created_for_an_unprepared_input(tmp_path):
    with pytest.raises(ValueError):
        SyntheticObservationRecoveryReader(None)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("scope", [
    {"account_id": "other", "market": "US", "symbol": "AAPL"},
    {"account_id": "us_mock", "market": "KR", "symbol": "AAPL"},
    {"account_id": "us_mock", "market": "US", "symbol": "aapl"},
    {"account_id": "us_mock", "market": "US", "symbol": ""},
])
def test_invalid_requested_scope_refuses_without_read_or_write(prepared, scope):
    journal, _, _ = prepared
    before = dump(journal)
    trace = []
    journal.db.set_trace_callback(trace.append)
    with pytest.raises(ValueError):
        SyntheticObservationRecoveryReader(journal).recover_scope(**scope)
    journal.db.set_trace_callback(None)
    assert not trace and dump(journal) == before
