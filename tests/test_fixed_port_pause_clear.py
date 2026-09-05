import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from src.core import broker_http
from src.core.control_state import FIXED_PORT_DEGRADED_PAUSE_REASON, write_pause_clear_event
from src.core.engine import (
    AccountEngine,
    DispatchClearanceService,
    NormalizedBalanceHolding,
    ReconciliationClearanceSnapshot,
    ReconciliationIncompleteReason,
    _AccountBalanceGate,
)
from tests.support.telegram_double import make_telegram_double


def _snapshot(account_id, symbol, *, incomplete=False):
    return ReconciliationClearanceSnapshot(
        account_id=account_id,
        symbol=symbol,
        market="US",
        balance_api_id="ust21070",
        balance_fetched_fresh=True,
        balance_from_shared_cache=False,
        balance_recognized=True,
        holding=NormalizedBalanceHolding(symbol, 0.0, 0.0),
        balance_received_at=asyncio.get_running_loop().time(),
        max_balance_age_sec=1.0,
        incomplete_reasons=(frozenset({ReconciliationIncompleteReason.BROKER_FILL_CATCHUP}) if incomplete else frozenset()),
    )


def _engine(account_id, symbol, tmp_path, snapshot, gate=None):
    engine = object.__new__(AccountEngine)
    engine.ctx = SimpleNamespace(account_id=account_id, strategy=SimpleNamespace(symbol=symbol), logger=Mock())
    engine.data_dir = tmp_path
    engine.telegram = make_telegram_double()
    engine._balance_gate = gate or _AccountBalanceGate()
    engine._balance_gate.engines.add(engine)
    engine._build_reconciliation_clearance_snapshot = AsyncMock(return_value=snapshot)
    return engine


def test_batch_clear_requires_fresh_pass_for_every_active_symbol_and_notifies(tmp_path):
    async def check():
        service = DispatchClearanceService("us_mock")
        service.observe_active_profile(("AAPL", "MSFT"), 1)
        gate = _AccountBalanceGate()
        gate.dispatch_clearance_service = service
        first = _engine("us_mock", "AAPL", tmp_path, _snapshot("us_mock", "AAPL"), gate)
        second = _engine("us_mock", "MSFT", tmp_path, _snapshot("us_mock", "MSFT"), gate)
        broker_http.enter_fixed_port_degraded_state("us_mock", "rest")
        write_pause_clear_event("us_mock", FIXED_PORT_DEGRADED_PAUSE_REASON, data_dir=tmp_path)

        await first._apply_fixed_port_pause_clear_event()

        assert broker_http.get_fixed_port_degraded_state("us_mock") is None
        assert not (tmp_path / "fixed_port_degraded_us_mock.json").exists()
        first.telegram.safe_send.assert_awaited_once()
        assert first._build_reconciliation_clearance_snapshot.awaited
        assert second._build_reconciliation_clearance_snapshot.awaited
    asyncio.run(check())


def test_batch_refusal_preserves_state_and_rechecks_despite_stale_cumulative_state(tmp_path):
    async def check():
        service = DispatchClearanceService("us_mock")
        service.observe_active_profile(("AAPL", "MSFT"), 1)
        service._cleared_symbols = frozenset({"AAPL", "MSFT"})
        gate = _AccountBalanceGate()
        gate.dispatch_clearance_service = service
        first = _engine("us_mock", "AAPL", tmp_path, _snapshot("us_mock", "AAPL"), gate)
        second = _engine("us_mock", "MSFT", tmp_path, _snapshot("us_mock", "MSFT", incomplete=True), gate)
        broker_http.enter_fixed_port_degraded_state("us_mock", "rest")
        marker = tmp_path / "fixed_port_degraded_us_mock.json"
        write_pause_clear_event("us_mock", FIXED_PORT_DEGRADED_PAUSE_REASON, data_dir=tmp_path)

        await first._apply_fixed_port_pause_clear_event()

        assert broker_http.get_fixed_port_degraded_state("us_mock") is not None
        assert marker.exists()
        assert first._build_reconciliation_clearance_snapshot.awaited
        assert second._build_reconciliation_clearance_snapshot.awaited
        first.telegram.safe_send.assert_awaited_once()
        assert "MSFT: condition 3" in first.telegram.safe_send.await_args.args[0]
    asyncio.run(check())


def test_pre_guard_runs_while_degraded_and_event_is_not_double_processed(tmp_path):
    async def check():
        service = DispatchClearanceService("us_mock")
        service.observe_active_profile(("AAPL",), 1)
        gate = _AccountBalanceGate()
        gate.dispatch_clearance_service = service
        engine = _engine("us_mock", "AAPL", tmp_path, _snapshot("us_mock", "AAPL", incomplete=True), gate)
        engine._sync_lock = asyncio.Lock()
        engine._refresh_runtime_control = Mock()
        engine._refresh_dashboard_controls = Mock()
        broker_http.enter_fixed_port_degraded_state("us_mock", "rest")
        write_pause_clear_event("us_mock", FIXED_PORT_DEGRADED_PAUSE_REASON, data_dir=tmp_path)

        await engine._tick()
        await engine._apply_reconciliation_clear_event()

        assert broker_http.get_fixed_port_degraded_state("us_mock") is not None
        engine.telegram.safe_send.assert_awaited_once()
    asyncio.run(check())


def test_preexisting_event_is_not_replayed_and_non_fixed_handler_is_unchanged(tmp_path):
    async def check():
        service = DispatchClearanceService("us_mock")
        service.observe_active_profile(("AAPL",), 1)
        gate = _AccountBalanceGate()
        gate.dispatch_clearance_service = service
        engine = _engine("us_mock", "AAPL", tmp_path, _snapshot("us_mock", "AAPL"), gate)
        event = write_pause_clear_event("us_mock", FIXED_PORT_DEGRADED_PAUSE_REASON, data_dir=tmp_path)
        gate.pause_clear_event_id = event["event_id"]
        broker_http.enter_fixed_port_degraded_state("us_mock", "rest")

        await engine._apply_fixed_port_pause_clear_event()

        assert broker_http.get_fixed_port_degraded_state("us_mock") is not None
        engine.telegram.safe_send.assert_not_awaited()

        engine._pause_reason = "broker_quantity_unattributed"
        engine._trading_paused = True
        write_pause_clear_event("us_mock", "broker_quantity_unattributed", data_dir=tmp_path)
        await engine._apply_reconciliation_clear_event()
        assert engine._pause_reason == ""
        assert engine._trading_paused is False
    asyncio.run(check())
