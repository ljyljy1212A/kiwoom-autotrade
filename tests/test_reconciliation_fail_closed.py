import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from src.core.broker_http import (
    clear_fixed_port_degraded_state,
    enter_fixed_port_degraded_state,
    get_fixed_port_degraded_state,
)
from src.core.control_state import (
    FIXED_PORT_DEGRADED_PAUSE_REASON,
    read_control_state,
    write_pause_clear_event,
    write_reconciliation_clear_event,
)
from src.core.engine import AccountEngine, _AccountBalanceGate
from src.utils.exceptions import RetryableError


def _engine(account, symbol, data_dir, reason=""):
    engine = object.__new__(AccountEngine)
    engine.ctx = SimpleNamespace(account_id=account, strategy=SimpleNamespace(symbol=symbol), logger=Mock())
    engine.data_dir = Path(data_dir)
    engine._trading_paused = bool(reason)
    engine._pause_reason = reason
    engine._tranche_sell_paused = False
    engine._balance_gate = _AccountBalanceGate()
    engine._balance_gate.configure_reconciliation({
        "mode": "manual",
        "consecutive_failure_threshold": 3,
        "session_failure_ceiling": 3,
    })
    engine._balance_gate.engines.add(engine)
    return engine


def _sync_engine(account, data_dir, *, balance_only, reason=""):
    engine = _engine(account, "005930", data_dir, reason=reason)
    engine._sync_lock = asyncio.Lock()
    engine._balance_only = balance_only
    engine._balance_sync_blocked = False
    engine._last_balance_request_at = 0.0
    engine._last_balance_reconciliation = 0.0
    engine._last_execution_query_at = 0.0
    engine._last_execution_unavailable_symbol = ""
    engine.balance_reconcile_sec = 0.0
    engine.execution_query_min_interval_sec = 0.0
    engine.ctx.client = SimpleNamespace(
        market="KR",
        mode="mock",
        token_mgr=SimpleNamespace(appkey="test-key"),
    )
    return engine


def test_below_threshold_does_not_pause():
    engine = _engine("kr_mock", "033320", ".")
    engine._record_reconciliation_failure(RuntimeError("x"))
    assert engine._trading_paused is False
    assert engine._balance_gate.reconciliation_failure_count == 1


def test_threshold_pauses_all_active_engines():
    first = _engine("kr_mock", "033320", ".")
    second = _engine("kr_mock", "003480", ".")
    first._balance_gate = second._balance_gate = _AccountBalanceGate()
    first._balance_gate.configure_reconciliation({"mode": "manual", "consecutive_failure_threshold": 3})
    first._balance_gate.engines.update({first, second})
    for _ in range(3):
        first._record_reconciliation_failure(RuntimeError("x"))
    assert first._pause_reason == second._pause_reason == "broker_reconciliation_unavailable"


def test_success_resets_consecutive_counter_and_does_not_use_ceiling():
    engine = _engine("kr_mock", "033320", ".")
    engine._record_reconciliation_failure(RuntimeError("x"))
    engine._record_reconciliation_success()
    assert engine._balance_gate.reconciliation_failure_count == 0
    assert engine._balance_gate.session_failure_ceiling == 3


def test_unrelated_pause_reason_is_preserved():
    engine = _engine("kr_mock", "033320", ".", reason="broker_quantity_unattributed")
    engine._record_reconciliation_failure(RuntimeError("x"))
    engine._record_reconciliation_failure(RuntimeError("x"))
    engine._record_reconciliation_failure(RuntimeError("x"))
    assert engine._pause_reason == "broker_quantity_unattributed"


def test_persisted_clear_clears_only_reconciliation_pause(tmp_path):
    first = _engine("kr_mock", "033320", tmp_path)
    second = _engine("kr_mock", "003480", tmp_path, reason="broker_quantity_unattributed")
    first._balance_gate = second._balance_gate = _AccountBalanceGate()
    first._balance_gate.configure_reconciliation({"mode": "manual", "consecutive_failure_threshold": 1})
    first._balance_gate.engines.update({first, second})
    first._record_reconciliation_failure(RuntimeError("x"))
    write_reconciliation_clear_event("kr_mock", data_dir=tmp_path)
    first._apply_reconciliation_clear_event()
    assert first._pause_reason == ""
    assert second._pause_reason == "broker_quantity_unattributed"


def test_reason_scoped_clear_clears_matching_engines_only(tmp_path):
    first = _engine("kr_mock", "033320", tmp_path, reason="broker_quantity_unattributed")
    second = _engine("kr_mock", "003480", tmp_path, reason="tranche_rebuild_ambiguous")
    third = _engine("kr_mock", "005930", tmp_path, reason="external_broker_balance_change")
    first._tranche_sell_paused = True
    second._tranche_sell_paused = True
    gate = _AccountBalanceGate()
    first._balance_gate = second._balance_gate = third._balance_gate = gate
    gate.engines.update({first, second, third})

    write_pause_clear_event("kr_mock", "tranche_rebuild_ambiguous", data_dir=tmp_path)
    first._apply_reconciliation_clear_event()

    assert first._pause_reason == "broker_quantity_unattributed"
    assert first._trading_paused is True
    assert second._pause_reason == ""
    assert second._trading_paused is False
    assert second._tranche_sell_paused is False
    assert third._pause_reason == "external_broker_balance_change"
    assert third._trading_paused is True


def test_legacy_reconciliation_event_is_inferred(tmp_path):
    engine = _engine("kr_mock", "033320", tmp_path, reason="broker_reconciliation_unavailable")
    control_path = tmp_path / "control" / "kr_mock.control.json"
    control_path.parent.mkdir()
    control_path.write_text(
        '{"account":"kr_mock","reconciliation_clear_event":{"event_id":"legacy-1"}}',
        encoding="utf-8",
    )

    engine._apply_reconciliation_clear_event()

    assert engine._pause_reason == ""
    assert engine._trading_paused is False


def test_pause_clear_writer_rejects_unknown_reason(tmp_path):
    with unittest.TestCase().assertRaises(ValueError):
        write_pause_clear_event("kr_mock", "not_a_pause_reason", data_dir=tmp_path)


def test_fixed_port_pause_reason_is_allowlisted(tmp_path):
    write_pause_clear_event("kr_mock", FIXED_PORT_DEGRADED_PAUSE_REASON, data_dir=tmp_path)

    control_state = read_control_state("kr_mock", data_dir=tmp_path)

    assert control_state["pause_clear_event"]["reason"] == FIXED_PORT_DEGRADED_PAUSE_REASON


def test_fixed_port_clear_event_consumes_once_and_scopes_to_matching_account(tmp_path):
    matching_engine = _engine("kr_mock", "005930", tmp_path)
    other_engine = _engine("us_mock", "AAPL", tmp_path)
    enter_fixed_port_degraded_state("kr_mock", "matching-operation")
    enter_fixed_port_degraded_state("us_mock", "other-operation")
    try:
        write_pause_clear_event("kr_mock", FIXED_PORT_DEGRADED_PAUSE_REASON, data_dir=tmp_path)

        matching_engine._apply_reconciliation_clear_event()

        assert get_fixed_port_degraded_state("kr_mock") is None
        assert get_fixed_port_degraded_state("us_mock") is not None
        first_event = read_control_state("kr_mock", data_dir=tmp_path)["fixed_port_event"]
        assert first_event["kind"] == "operator_resolved"
        assert first_event["account"] == "kr_mock"
        assert first_event["updated_by"] == "telegram"
        assert first_event["event_id"]

        matching_engine._apply_reconciliation_clear_event()

        assert read_control_state("kr_mock", data_dir=tmp_path)["fixed_port_event"] == first_event
        assert other_engine.ctx.account_id == "us_mock"
    finally:
        clear_fixed_port_degraded_state("kr_mock")
        clear_fixed_port_degraded_state("us_mock")


class SyncBrokerStateIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_balance_only_retryable_error_wiring_reaches_pause_threshold(self):
        engine = _sync_engine("kr_mock", ".", balance_only=True)
        engine._reconcile_balance = AsyncMock(side_effect=RetryableError("balance unavailable"))

        for _ in range(3):
            self.assertFalse(await engine.sync_broker_state())

        self.assertEqual(engine._pause_reason, "broker_reconciliation_unavailable")

    async def test_fill_reconciliation_retryable_error_wiring_reaches_pause_threshold(self):
        engine = _sync_engine("kr_mock", ".", balance_only=False)
        engine.ledger = SimpleNamespace(pending_orders=lambda _symbol: False)
        engine._reconcile_balance = AsyncMock(side_effect=RetryableError("balance unavailable"))

        for _ in range(3):
            self.assertFalse(await engine.sync_broker_state(force_balance=True))

        self.assertEqual(engine._pause_reason, "broker_reconciliation_unavailable")

    async def test_clear_is_applied_before_same_cycle_retryable_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            engine = _sync_engine(
                "kr_mock", data_dir, balance_only=True,
                reason="broker_reconciliation_unavailable",
            )
            write_reconciliation_clear_event("kr_mock", data_dir=data_dir)
            engine._reconcile_balance = AsyncMock(side_effect=RetryableError("still unavailable"))

            self.assertFalse(await engine.sync_broker_state())
            self.assertEqual(engine._pause_reason, "")
            self.assertEqual(engine._balance_gate.reconciliation_failure_count, 1)
