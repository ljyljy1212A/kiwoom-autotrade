"""Scratch file journal fixtures. No operational databases or broker calls."""
from dataclasses import replace
import sqlite3

import pytest

from src.data.us_synthetic_observation_journal import (
    SyntheticJournalIdentity, _validate_journal_databases, create_synthetic_journal, reopen_synthetic_journal,
)
from tests.test_us_cumulative_execution import DATE, STAMP, NEXT, NUMBER
from tests.test_us_synthetic_response_adapter import response


def identity():
    return SyntheticJournalIdentity("first", DATE, NUMBER, "AAPL", "BUY", "5", STAMP)


def dump(journal):
    return "\n".join(journal.db.iterdump())


@pytest.fixture
def journal(tmp_path):
    root = tmp_path / "synthetic-journal-case"
    root.mkdir()
    instance = create_synthetic_journal(root, allow_root=tmp_path)
    instance.bind(identity())
    try:
        yield instance, root, tmp_path
    finally:
        instance.close()


def test_exact_decimals_and_restart_keep_unapplied_observation(journal):
    instance, root, allowed = journal
    instance.observe(response("2", "100.0000"), query_order_date=DATE, observed_at_utc=STAMP)
    instance.observe(response("5", "106.0000"), query_order_date=DATE, observed_at_utc=NEXT)
    before = instance.recover()
    instance.close()
    reopened = reopen_synthetic_journal(root, allow_root=allowed)
    try:
        assert reopened.recover() == before
        assert before.observations[0][4:7] == ("5", "106", "530")
        assert not before.operational_ingestion_allowed and before.execution_date_status == "unresolved"
        assert reopened.db.execute("SELECT filled_qty,status FROM pending_orders").fetchone() == (0, "open")
        raw = reopened.db.execute("SELECT observation_json FROM us_cumulative_observation_audit ORDER BY observed_at_utc DESC").fetchone()[0]
        assert '"cntr_uv": "106.0000"' in raw
        assert not reopened.db.execute("SELECT 1 FROM sqlite_master WHERE name='trade_ledger'").fetchone()
    finally:
        reopened.close()


def test_duplicate_does_not_advance_observation_or_economic_state(journal):
    instance, _, _ = journal
    instance.observe(response(), query_order_date=DATE, observed_at_utc=STAMP)
    before = instance.recover()
    result = instance.observe(response(), query_order_date=DATE, observed_at_utc=STAMP)
    assert result.deltas[0].outcome == "duplicate"
    assert instance.recover() == before and not result.economic_ingestion_allowed


def test_identity_duplicate_is_no_write_and_replacement_refuses(journal):
    instance, _, _ = journal
    before = dump(instance)
    assert instance.bind(identity()) == "DUPLICATE" and dump(instance) == before
    with pytest.raises(ValueError):
        instance.bind(replace(identity(), symbol="MSFT"))
    assert dump(instance) == before


def test_same_account_date_order_cannot_have_two_uids(journal):
    instance, _, _ = journal
    before = dump(instance)
    with pytest.raises(sqlite3.IntegrityError):
        instance.bind(replace(identity(), order_uid="other"))
    assert dump(instance) == before


def test_conflict_survives_close_and_reopen(journal):
    instance, root, allowed = journal
    instance.observe(response(), query_order_date=DATE, observed_at_utc=STAMP)
    result = instance.observe(response(average="101"), query_order_date=DATE, observed_at_utc=NEXT)
    assert result.state == "conflict"
    before = instance.recover()
    assert before.state == "HELD" and before.conflicts
    instance.close()
    reopened = reopen_synthetic_journal(root, allow_root=allowed)
    try:
        assert reopened.recover() == before
    finally:
        reopened.close()


@pytest.mark.parametrize("data", [dict(response(), _execution_pages_complete=False),
                                  response(stk_cd="MSFT"), response(ord_no="000000099")])
def test_bad_response_refuses_without_persistence(journal, data):
    instance, _, _ = journal
    before = dump(instance)
    with pytest.raises(ValueError):
        instance.observe(data, query_order_date=DATE, observed_at_utc=STAMP)
    assert dump(instance) == before


class FaultingConnection:
    def __init__(self, connection, fragment):
        self.connection = connection
        self.fragment = fragment

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, *args):
        if self.fragment in sql:
            raise sqlite3.OperationalError("synthetic injected SQL failure")
        return self.connection.execute(sql, *args)


@pytest.mark.parametrize("fragment", ["INSERT INTO us_cumulative_observation_audit",
                                      "INSERT INTO us_cumulative_observations",
                                      "INSERT INTO us_cumulative_observation_conflicts"])
def test_injected_sql_failure_rolls_back_observation_audit_and_conflict(journal, fragment):
    instance, _, _ = journal
    data, stamp = response(), STAMP
    if "conflicts" in fragment:
        instance.observe(data, query_order_date=DATE, observed_at_utc=stamp)
        data, stamp = response(average="101"), NEXT
    before = dump(instance)
    instance.store.db = FaultingConnection(instance.db, fragment)
    with pytest.raises(sqlite3.OperationalError, match="injected SQL failure"):
        instance.observe(data, query_order_date=DATE, observed_at_utc=stamp)
    assert dump(instance) == before and not instance.db.in_transaction


@pytest.mark.parametrize("sql", ["UPDATE pending_orders SET filled_qty=1",
                                  "UPDATE us_cumulative_observations SET amount='201'",
                                  "DELETE FROM us_cumulative_observation_audit"])
def test_corrupt_recovery_refuses_without_repair(journal, sql):
    instance, _, _ = journal
    instance.observe(response(), query_order_date=DATE, observed_at_utc=STAMP)
    instance.db.execute(sql)
    instance.db.commit()
    before = dump(instance)
    with pytest.raises(ValueError):
        instance.recover()
    assert dump(instance) == before


def test_read_only_recovery_and_caller_transaction(journal):
    instance, _, _ = journal
    before = dump(instance)
    instance.recover()
    assert dump(instance) == before
    instance.db.execute("BEGIN")
    with pytest.raises(ValueError, match="own transaction"):
        instance.recover()
    assert instance.db.in_transaction
    instance.db.rollback()


def test_existing_directory_is_not_overwritten(journal):
    instance, root, allowed = journal
    before = dump(instance)
    with pytest.raises(ValueError, match="empty scratch"):
        create_synthetic_journal(root, allow_root=allowed)
    assert dump(instance) == before


def test_missing_reopen_does_not_create_file(tmp_path):
    root = tmp_path / "missing-journal"
    root.mkdir()
    with pytest.raises(sqlite3.OperationalError):
        reopen_synthetic_journal(root, allow_root=tmp_path)
    assert list(root.iterdir()) == []


def test_path_escape_is_refused(tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    with pytest.raises(ValueError, match="inside"):
        create_synthetic_journal(outside, allow_root=allowed)
    assert list(outside.iterdir()) == []


def test_quick_check_internal_temp_allows_repeated_observation_and_recovery(journal):
    instance, _, _ = journal
    instance.observe(response(), query_order_date=DATE, observed_at_utc=STAMP)
    assert instance.db.execute("PRAGMA database_list").fetchall()[1:] == [(1, "temp", "")]
    assert instance.db.execute("SELECT 1 FROM temp.sqlite_master LIMIT 1").fetchone() is None
    result = instance.observe(response("5", "106"), query_order_date=DATE, observed_at_utc=NEXT)
    assert result.state == "observed"
    assert instance.recover().observations[0][4:7] == ("5", "106", "530")


@pytest.mark.parametrize("memory", [False, True])
def test_additional_attached_file_or_memory_database_is_refused(journal, memory):
    instance, root, _ = journal
    instance.recover()  # Allocate legitimate temp before attaching another DB.
    path = ":memory:" if memory else str(root / "synthetic-extra.sqlite")
    instance.db.execute("ATTACH DATABASE ? AS extra", (path,))
    before = dump(instance)
    with pytest.raises(ValueError, match="scratch file database"):
        instance.recover()
    assert dump(instance) == before


def test_temp_objects_cannot_shadow_main_journal_tables(journal):
    instance, _, _ = journal
    instance.db.execute("CREATE TEMP TABLE pending_orders (shadow TEXT)")
    instance.db.commit()
    with pytest.raises(ValueError, match="no user objects"):
        instance.recover()


@pytest.mark.parametrize("databases", [
    [], [(0, "main", "")], [(0, "main", "scratch.sqlite"), (1, "temp", "extra.sqlite")],
    [(0, "main", "scratch.sqlite"), (2, "extra", "")],
    [(0, "main", "scratch.sqlite"), (1, "temp", ""), (2, "extra", "extra.sqlite")],
    [(0, "main", "scratch.sqlite"), (1, "temp", ""), (1, "temp", "")],
    [(1, "temp", ""), (0, "main", "scratch.sqlite")], [(0, "main")],
])
def test_connection_guard_refuses_unknown_or_file_backed_temp(databases):
    with pytest.raises(ValueError):
        _validate_journal_databases(databases)
