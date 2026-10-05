"""Independent scratch recovery gate fixtures; no Engine or order calls."""
from dataclasses import replace

import pytest

from src.core.us_synthetic_recovery_gate import SyntheticRecoveryGate
from src.data.us_synthetic_observation_journal import create_synthetic_journal
from src.data.us_synthetic_observation_recovery import SyntheticObservationRecoveryReader
from tests.test_us_synthetic_observation_recovery import seed, dump


@pytest.fixture
def prepared(tmp_path):
    root = tmp_path / "scratch-gate"
    root.mkdir()
    journal = create_synthetic_journal(root, allow_root=tmp_path)
    reader = SyntheticObservationRecoveryReader(journal)
    gate = SyntheticRecoveryGate(account_id="us_mock", market="US", enabled=True,
                                 reader=reader, expected_journal=journal)
    try:
        yield journal, reader, gate
    finally:
        journal.close()


def result(reader, symbol="AAPL"):
    return reader.recover_scope(account_id="us_mock", market="US", symbol=symbol)


def test_default_disabled_does_not_touch_reader_or_grant_authority():
    gate = SyntheticRecoveryGate(account_id="us_mock", market="US", reader=object(), expected_journal=object())
    decision = gate.check_scope(None)
    assert decision.state == "DISABLED" and not decision.check_complete
    assert not decision.account_blocked and not decision.symbol_blocked
    assert not decision.economic_ingestion_allowed and not decision.operational_trading_allowed


def test_missing_active_reader_blocks_without_backend_creation():
    gate = SyntheticRecoveryGate(account_id="us_mock", market="US", enabled=True)
    decision = gate.check_scope("AAPL")
    assert decision.state == "INCOMPLETE" and decision.account_blocked and not decision.check_complete


def test_clean_recovery_is_check_only_and_does_not_change_db(prepared):
    journal, _, gate = prepared
    before = dump(journal)
    decision = gate.check_scope("AAPL")
    assert decision.state == "RECOVERY_CHECKED" and decision.check_complete
    assert not decision.account_blocked and not decision.symbol_blocked
    assert not decision.economic_ingestion_allowed and not decision.operational_trading_allowed
    assert dump(journal) == before


def test_symbol_conflict_does_not_become_another_symbol_blocker(prepared):
    journal, _, gate = prepared
    seed(journal)
    blocked = gate.check_scope("AAPL")
    other = gate.check_scope("MSFT")
    assert blocked.state == "CONFLICT" and blocked.symbol_blocked and not blocked.account_blocked
    assert blocked.conflicts[0].order_uid == "old-generation"
    assert other.state == "RECOVERY_CHECKED" and not other.symbol_blocked and not other.account_blocked


def test_latched_conflict_is_not_cleared_by_a_later_clean_receipt(prepared, monkeypatch):
    journal, reader, gate = prepared
    clean = result(reader)
    seed(journal)
    first = gate.check_scope("AAPL")
    monkeypatch.setattr(reader, "recover_scope", lambda **kwargs: clean)
    later = gate.check_scope("AAPL")
    assert later.state == "CONFLICT" and later.check_complete and later.symbol_blocked
    assert later.conflicts == first.conflicts and not later.operational_trading_allowed


@pytest.mark.parametrize("field,value", [
    ("account_id", "other"), ("market", "KR"), ("symbol", "MSFT"),
    ("coverage", "production-authority"), ("scope_complete", 1),
    ("journal_conflict_count", True), ("journal_conflict_count", -1),
    ("state", "READY_TO_TRADE"), ("blocking_scope", "symbol"),
    ("economic_ingestion_allowed", True), ("operational_trading_allowed", True),
    ("execution_date_status", "broker_confirmed"),
])
def test_forged_receipt_blocks_account_without_accepting_partial_success(prepared, monkeypatch, field, value):
    journal, reader, gate = prepared
    forged = replace(result(reader), **{field: value})
    before = dump(journal)
    monkeypatch.setattr(reader, "recover_scope", lambda **kwargs: forged)
    decision = gate.check_scope("AAPL")
    assert decision.state == "INCOMPLETE" and decision.account_blocked and not decision.check_complete
    assert dump(journal) == before and not decision.operational_trading_allowed


def test_account_failure_remains_latched_after_clean_recovery_of_another_symbol(prepared, monkeypatch):
    _, reader, gate = prepared
    original = reader.recover_scope
    monkeypatch.setattr(reader, "recover_scope", lambda **kwargs: object())
    assert gate.check_scope("AAPL").account_blocked
    monkeypatch.setattr(reader, "recover_scope", original)
    later = gate.check_scope("MSFT")
    assert later.state == "INCOMPLETE" and later.account_blocked and later.check_complete
    assert later.reasons == ("account_recovery_failure_latched",)


def test_backend_exception_is_sanitized_and_called_once(prepared, monkeypatch):
    journal, reader, gate = prepared
    calls = []

    def failed(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("sensitive synthetic backend error")

    before = dump(journal)
    monkeypatch.setattr(reader, "recover_scope", failed)
    decision = gate.check_scope("AAPL")
    assert decision.account_blocked and decision.reasons == ("recovery_check_failed",)
    assert len(calls) == 1 and dump(journal) == before


def test_every_check_reads_again_and_finds_new_conflict(prepared, monkeypatch):
    journal, reader, gate = prepared
    calls = []
    original = reader.recover_scope

    def counted(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(reader, "recover_scope", counted)
    assert gate.check_scope("AAPL").state == "RECOVERY_CHECKED"
    seed(journal)
    assert gate.check_scope("AAPL").state == "CONFLICT" and len(calls) == 2


def test_different_empty_journal_cannot_clear_expected_journal_binding(prepared, tmp_path):
    _, reader, gate = prepared
    root = tmp_path / "wrong-journal"
    root.mkdir()
    other = create_synthetic_journal(root, allow_root=tmp_path)
    original = reader.journal
    reader.journal = other
    try:
        decision = gate.check_scope("AAPL")
        assert decision.state == "INCOMPLETE" and decision.account_blocked
    finally:
        reader.journal = original
        other.close()


def test_changed_connection_is_refused_before_recovery(prepared, tmp_path, monkeypatch):
    journal, reader, gate = prepared
    root = tmp_path / "other-connection"
    root.mkdir()
    other = create_synthetic_journal(root, allow_root=tmp_path)
    connection = journal.db
    journal.db = other.db
    calls = []
    monkeypatch.setattr(reader, "recover_scope", lambda **kwargs: calls.append(kwargs))
    try:
        assert gate.check_scope("AAPL").account_blocked and not calls
    finally:
        journal.db = connection
        other.close()


def test_connection_change_during_reader_call_is_refused(prepared, monkeypatch):
    journal, reader, gate = prepared
    clean = result(reader)
    connection = journal.db

    def switched(**kwargs):
        journal.db = object()
        return clean

    monkeypatch.setattr(reader, "recover_scope", switched)
    try:
        decision = gate.check_scope("AAPL")
        assert decision.account_blocked and not decision.check_complete
    finally:
        journal.db = connection


@pytest.mark.parametrize("fault", ["duplicate", "wrong_symbol", "raw_mismatch", "changed_reason", "backwards_time"])
def test_malformed_or_regressed_conflict_does_not_replace_retained_evidence(prepared, monkeypatch, fault):
    journal, reader, gate = prepared
    seed(journal)
    first = gate.check_scope("AAPL")
    original = result(reader)
    item = original.conflicts[0]
    if fault == "duplicate":
        conflicts = (item, item)
    elif fault == "wrong_symbol":
        conflicts = (replace(item, symbol="MSFT"),)
    elif fault == "raw_mismatch":
        conflicts = (replace(item, ord_no="000000099"),)
    elif fault == "changed_reason":
        conflicts = (replace(item, reason="replacement_reason"),)
    else:
        conflicts = (replace(item, last_seen_at_utc="2026-10-03T00:00:00+00:00"),)
    forged = replace(original, conflicts=conflicts, journal_conflict_count=len(conflicts))
    monkeypatch.setattr(reader, "recover_scope", lambda **kwargs: forged)
    later = gate.check_scope("AAPL")
    assert later.account_blocked and later.symbol_blocked and not later.check_complete
    assert later.conflicts == first.conflicts


def test_reader_incomplete_result_blocks_all_symbols_and_keeps_database_unchanged(prepared):
    journal, _, gate = prepared
    seed(journal)
    journal.db.execute("DELETE FROM us_cumulative_observation_audit")
    journal.db.commit()
    before = dump(journal)
    first = gate.check_scope("AAPL")
    other = gate.check_scope("MSFT")
    assert first.account_blocked and other.account_blocked
    assert not first.check_complete and not other.check_complete and dump(journal) == before


def test_invalid_symbol_blocks_account_without_accessing_backend(prepared, monkeypatch):
    _, reader, gate = prepared
    calls = []
    monkeypatch.setattr(reader, "recover_scope", lambda **kwargs: calls.append(kwargs))
    decision = gate.check_scope("aapl")
    assert decision.account_blocked and decision.symbol is None and not calls


@pytest.mark.parametrize("kwargs", [
    {"account_id": "other", "market": "US"},
    {"account_id": "us_mock", "market": "KR"},
    {"account_id": "us_mock", "market": "US", "enabled": "true"},
])
def test_constructor_requires_explicit_scope_and_boolean(kwargs):
    with pytest.raises(ValueError):
        SyntheticRecoveryGate(**kwargs)
