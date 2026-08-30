import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from unittest.mock import Mock

from src import main as worker_main
from src.core import broker_http
from src.core.control_state import FIXED_PORT_DEGRADED_PAUSE_REASON, write_pause_clear_event
from src.core.engine import (
    AccountEngine,
    DispatchClearanceService,
    ReconciliationClearanceResult,
)


def _marker_path(tmp_path, account_id):
    return tmp_path / f"fixed_port_degraded_{account_id}.json"


def test_degraded_state_transitions_persist_all_required_fields(tmp_path):
    entered_at = datetime(2026, 8, 30, tzinfo=timezone.utc)
    ongoing_at = entered_at + timedelta(minutes=15)
    state = broker_http.enter_fixed_port_degraded_state("us_mock", "rest", now=entered_at)
    broker_http.mark_fixed_port_entry_alert_fired("us_mock")
    broker_http.record_fixed_port_ongoing_status("us_mock", now=ongoing_at)
    broker_http.record_fixed_port_recovery_probe_attempt("us_mock", now=ongoing_at)

    payload = json.loads(_marker_path(tmp_path, "us_mock").read_text(encoding="utf-8"))

    assert payload == {
        "account": "us_mock",
        "entered_at": entered_at.isoformat(),
        "last_collision_at": entered_at.isoformat(),
        "operation": "rest",
        "next_recovery_probe_at": (ongoing_at + timedelta(seconds=90)).isoformat(),
        "entry_alert_fired": True,
        "last_ongoing_status_at": ongoing_at.isoformat(),
    }
    assert state.account_id == "us_mock"


def test_startup_rebuilds_persisted_state_and_reports_degraded(tmp_path):
    entered_at = datetime(2026, 8, 30, tzinfo=timezone.utc)
    broker_http.enter_fixed_port_degraded_state("us_mock", "rest", now=entered_at)
    broker_http.clear_fixed_port_degraded_state("us_mock")

    state = broker_http.restore_fixed_port_degraded_state("us_mock")

    assert state is not None
    assert state.entered_at == entered_at
    assert worker_main._startup_worker_status_state("us_mock") == "DEGRADED_FIXED_PORT"


def test_clearance_success_deletes_persisted_marker(tmp_path):
    broker_http.enter_fixed_port_degraded_state("us_mock", "rest")
    service = DispatchClearanceService("us_mock")
    service.observe_active_profile(("SOXL",), 0)
    result = ReconciliationClearanceResult("us_mock", "SOXL", True, ())

    service._record_clearance_result(SimpleNamespace(data_dir=tmp_path), "SOXL", result)

    assert broker_http.get_fixed_port_degraded_state("us_mock") is None
    assert not _marker_path(tmp_path, "us_mock").exists()


def test_telegram_pause_clear_keeps_persisted_marker(tmp_path):
    broker_http.enter_fixed_port_degraded_state("us_mock", "rest")
    write_pause_clear_event(
        "us_mock", FIXED_PORT_DEGRADED_PAUSE_REASON, data_dir=tmp_path,
    )
    engine = object.__new__(AccountEngine)
    engine.ctx = SimpleNamespace(
        account_id="us_mock", strategy=SimpleNamespace(symbol="SOXL"), logger=Mock(),
    )
    engine.data_dir = tmp_path
    engine._balance_gate = SimpleNamespace(pause_clear_event_id="", engines=[])

    engine._apply_reconciliation_clear_event()

    assert broker_http.get_fixed_port_degraded_state("us_mock") is None
    assert _marker_path(tmp_path, "us_mock").exists()


def test_corrupt_marker_restores_fail_closed_state_without_replacing_evidence(tmp_path):
    path = _marker_path(tmp_path, "us_mock")
    path.write_text("not-json", encoding="utf-8")
    now = datetime(2026, 8, 30, tzinfo=timezone.utc)

    state = broker_http.restore_fixed_port_degraded_state("us_mock", now=now)

    assert state is not None
    assert state.operation == "persisted-marker-corrupt"
    assert state.entered_at == now
    assert path.read_text(encoding="utf-8") == "not-json"
