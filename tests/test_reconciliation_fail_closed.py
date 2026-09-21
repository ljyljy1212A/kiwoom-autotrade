import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.core.broker_http import (
    clear_fixed_port_degraded_state,
    enter_fixed_port_degraded_state,
    get_fixed_port_degraded_state,
)
from src.core import dashboard_control_snapshot as control_snapshot
from src.core.control_state import (
    FIXED_PORT_DEGRADED_PAUSE_REASON,
    read_control_state,
    write_pause_clear_event,
    write_reconciliation_clear_event,
)
from src.core.engine import (
    AccountEngine,
    NormalizedBalanceHolding,
    ReconciliationIncompleteReason,
    _AccountBalanceGate,
)
from src.core.reconciliation import _ReconciliationCoordinator
from src.strategy.infinite_grid import InfiniteGridStrategy
from src.utils.exceptions import KiwoomAPIError, RetryableError


SESSION = "1" * 32


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
    engine._reconciliation_coordinator = _ReconciliationCoordinator()
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


def _clearance_snapshot(account, symbol, *, incomplete=False):
    return SimpleNamespace(
        account_id=account,
        symbol=symbol,
        market="KR",
        balance_api_id="kt00018",
        balance_fetched_fresh=True,
        balance_from_shared_cache=False,
        balance_recognized=True,
        balance_received_at=time.monotonic(),
        max_balance_age_sec=1.0,
        holding=NormalizedBalanceHolding(symbol, 0.0, 0.0),
        incomplete_reasons=(
            frozenset({ReconciliationIncompleteReason.BROKER_FILL_CATCHUP})
            if incomplete else frozenset()
        ),
        unresolved_order_ids=(),
        unattributed_collision_order_ids=(),
    )


def _dashboard_config(symbol="033320"):
    return {
        "symbol": symbol,
        "market": "KR",
        "commission_rate": 0.0,
        "auto_buy": {"enabled": True, "order_type": "00"},
        "auto_sell": {"enabled": True, "order_type": "00"},
        "first_buy": {"mode": "manual", "amount": 10_000},
        "buy_steps": [{"step": 2, "drop_pct": -1.0, "amount": 1_000}],
        "sell_steps": [{"step": 1, "profit_pct": 1.0}],
    }


def _dashboard_engine(account, symbol, data_dir, reason=""):
    engine = _engine(account, symbol, data_dir, reason=reason)
    config = _dashboard_config(symbol)
    config["mode"] = "mock"
    engine.ctx.client = SimpleNamespace(market="KR")
    engine.ctx.strategy = InfiniteGridStrategy(config)
    engine._control_authority = control_snapshot.ControlAuthority(account, SESSION)
    engine.ctx.position = SimpleNamespace()
    engine._control_symbol = None
    engine._closed_symbols_blocked = set()
    engine._symbol_lifecycles = {symbol: {"status": "open"}}
    engine._dashboard_config_fingerprint = ""
    engine._dashboard_symbol = ""
    engine._dashboard_strategy_changed = False
    engine._dashboard_auto_buy = False
    engine._dashboard_auto_sell = False
    engine._dashboard_profile_allowed = False
    engine._last_allowlist_warning_symbol = ""
    engine._last_execution_unavailable_symbol = ""
    engine._lifecycle_pending_adoption = False
    engine._prepare_lifecycle_scope = Mock()
    engine._begin_manual_lifecycle_activation = Mock()
    engine._restore_from_ledger = Mock()
    settings = Path(data_dir) / f"dashboard_settings_{account}.json"
    profiles = []
    if settings.exists():
        try:
            existing = json.loads(settings.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
        if isinstance(existing, dict) and isinstance(existing.get("profiles"), list):
            profiles = [
                profile for profile in existing["profiles"]
                if not (
                    isinstance(profile, dict)
                    and isinstance(profile.get("config"), dict)
                    and profile["config"].get("symbol") == symbol
                )
            ]
    profiles.append({"enabled": True, "config": config})
    settings.write_text(json.dumps({"profiles": profiles}), encoding="utf-8")
    engine._control_symbol = symbol
    snapshot_path = control_snapshot.path_for(data_dir, account)
    if not snapshot_path.exists():
        control_snapshot.initialize(data_dir, account, {}, None)
    control_snapshot.update(data_dir, account, {
        "symbol": symbol,
        "instance_id": SESSION,
        "auto_buy": True,
        "auto_sell": True,
        "config": config,
    })
    assert not (Path(data_dir) / f"dashboard_control_{account}.json").exists()
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
    first._build_reconciliation_clearance_snapshot = AsyncMock(
        return_value=_clearance_snapshot("kr_mock", "033320")
    )
    asyncio.run(first._apply_reconciliation_clear_event())
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
    second._build_reconciliation_clearance_snapshot = AsyncMock(
        return_value=_clearance_snapshot("kr_mock", "003480")
    )
    asyncio.run(first._apply_reconciliation_clear_event())

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

    engine._build_reconciliation_clearance_snapshot = AsyncMock(
        return_value=_clearance_snapshot("kr_mock", "033320")
    )
    asyncio.run(engine._apply_reconciliation_clear_event())

    assert engine._pause_reason == ""
    assert engine._trading_paused is False


def test_pause_clear_writer_rejects_unknown_reason(tmp_path):
    with unittest.TestCase().assertRaises(ValueError):
        write_pause_clear_event("kr_mock", "not_a_pause_reason", data_dir=tmp_path)


def test_pause_clear_history_uses_atomic_write_and_preserves_warning_contract(tmp_path):
    with patch("src.core.control_state.atomic_write_text", side_effect=OSError("disk full")) as atomic_write, \
         patch("src.core.control_state.logger.warning") as warning:
        event = write_pause_clear_event(
            "kr_mock",
            FIXED_PORT_DEGRADED_PAUSE_REASON,
            data_dir=tmp_path,
        )

        assert event["reason"] == FIXED_PORT_DEGRADED_PAUSE_REASON
        assert atomic_write.call_count == 1
        history_path = atomic_write.call_args.args[0]
        assert history_path.parent.name == "kr_mock"
        assert history_path.name == f"{event['event_id']}.json"
        assert atomic_write.call_args.args[1] == json.dumps(event, ensure_ascii=False)
        assert "Pause-clear history write failed for kr_mock" in warning.call_args.args[0]
        assert event["event_id"] in warning.call_args.args[0]


def test_fixed_port_pause_reason_is_allowlisted(tmp_path):
    write_pause_clear_event("kr_mock", FIXED_PORT_DEGRADED_PAUSE_REASON, data_dir=tmp_path)

    control_state = read_control_state("kr_mock", data_dir=tmp_path)

    assert control_state["pause_clear_event"]["reason"] == FIXED_PORT_DEGRADED_PAUSE_REASON


def test_normal_fixed_port_clear_event_does_not_bypass_clearance(tmp_path):
    matching_engine = _engine("kr_mock", "005930", tmp_path)
    other_engine = _engine("us_mock", "AAPL", tmp_path)
    enter_fixed_port_degraded_state("kr_mock", "matching-operation")
    enter_fixed_port_degraded_state("us_mock", "other-operation")
    try:
        write_pause_clear_event("kr_mock", FIXED_PORT_DEGRADED_PAUSE_REASON, data_dir=tmp_path)

        asyncio.run(matching_engine._apply_reconciliation_clear_event())

        assert get_fixed_port_degraded_state("kr_mock") is not None
        assert get_fixed_port_degraded_state("us_mock") is not None
        asyncio.run(matching_engine._apply_reconciliation_clear_event())
        assert other_engine.ctx.account_id == "us_mock"
    finally:
        clear_fixed_port_degraded_state("kr_mock")
        clear_fixed_port_degraded_state("us_mock")


def test_clear_event_fresh_check_clears_all_matching_engines(tmp_path):
    async def check():
        first = _engine("kr_mock", "033320", tmp_path, reason="broker_quantity_unattributed")
        second = _engine("kr_mock", "003480", tmp_path, reason="broker_quantity_unattributed")
        first._trading_paused = second._trading_paused = True
        first._tranche_sell_paused = second._tranche_sell_paused = True
        gate = _AccountBalanceGate()
        first._balance_gate = second._balance_gate = gate
        gate.engines.update({first, second})
        first._build_reconciliation_clearance_snapshot = AsyncMock(
            return_value=_clearance_snapshot("kr_mock", "033320")
        )
        second._build_reconciliation_clearance_snapshot = AsyncMock(
            return_value=_clearance_snapshot("kr_mock", "003480")
        )
        event = write_pause_clear_event(
            "kr_mock", "broker_quantity_unattributed", data_dir=tmp_path,
        )

        await first._apply_reconciliation_clear_event()

        assert gate.pause_clear_event_id == event["event_id"]
        assert first._pause_reason == second._pause_reason == ""
        assert first._trading_paused is False
        assert second._trading_paused is False
        assert first._tranche_sell_paused is False
        assert second._tranche_sell_paused is False
    asyncio.run(check())


def test_clear_event_fresh_check_failure_is_all_or_nothing(tmp_path):
    async def check():
        first = _engine("kr_mock", "033320", tmp_path, reason="broker_quantity_unattributed")
        second = _engine("kr_mock", "003480", tmp_path, reason="broker_quantity_unattributed")
        gate = _AccountBalanceGate()
        first._balance_gate = second._balance_gate = gate
        gate.engines.update({first, second})
        first._build_reconciliation_clearance_snapshot = AsyncMock(
            return_value=_clearance_snapshot("kr_mock", "033320")
        )
        second._build_reconciliation_clearance_snapshot = AsyncMock(
            return_value=_clearance_snapshot("kr_mock", "003480", incomplete=True)
        )
        write_pause_clear_event("kr_mock", "broker_quantity_unattributed", data_dir=tmp_path)

        await first._apply_reconciliation_clear_event()

        assert gate.pause_clear_event_id == ""
        assert first._trading_paused is True
        assert second._trading_paused is True
        assert first._pause_reason == second._pause_reason == "broker_quantity_unattributed"
        assert any("003480" in call.args[0] for call in first.ctx.logger.warning.call_args_list)
    asyncio.run(check())


def test_clear_event_fresh_check_exception_is_logged_and_retried(tmp_path):
    async def check():
        engine = _engine("kr_mock", "033320", tmp_path, reason="broker_quantity_unattributed")
        engine._build_reconciliation_clearance_snapshot = AsyncMock(
            side_effect=RuntimeError("fresh balance unavailable")
        )
        write_pause_clear_event("kr_mock", "broker_quantity_unattributed", data_dir=tmp_path)

        await engine._apply_reconciliation_clear_event()

        assert engine._balance_gate.pause_clear_event_id == ""
        assert engine._trading_paused is True
        assert engine._pause_reason == "broker_quantity_unattributed"
        assert any("033320" in call.args[0] for call in engine.ctx.logger.warning.call_args_list)
        assert any("fresh balance unavailable" in call.args[0] for call in engine.ctx.logger.warning.call_args_list)
    asyncio.run(check())


def test_dashboard_activation_fresh_check_clears_all_matching_engines(tmp_path):
    async def check():
        first = _dashboard_engine("kr_mock", "033320", tmp_path, reason="broker_reconciliation_unavailable")
        second = _dashboard_engine("kr_mock", "003480", tmp_path, reason="broker_reconciliation_unavailable")
        gate = _AccountBalanceGate()
        first._balance_gate = second._balance_gate = gate
        gate.engines.update({first, second})
        first._build_reconciliation_clearance_snapshot = AsyncMock(
            return_value=_clearance_snapshot("kr_mock", "033320")
        )
        second._build_reconciliation_clearance_snapshot = AsyncMock(
            return_value=_clearance_snapshot("kr_mock", "003480")
        )

        await first._refresh_dashboard_controls()

        assert first._trading_paused is False
        assert second._trading_paused is False
        first._build_reconciliation_clearance_snapshot.assert_awaited_once()
        second._build_reconciliation_clearance_snapshot.assert_awaited_once()
    asyncio.run(check())


def test_dashboard_activation_fresh_check_failure_is_all_or_nothing(tmp_path):
    async def check():
        first = _dashboard_engine("kr_mock", "033320", tmp_path, reason="broker_reconciliation_unavailable")
        second = _dashboard_engine("kr_mock", "003480", tmp_path, reason="broker_reconciliation_unavailable")
        gate = _AccountBalanceGate()
        first._balance_gate = second._balance_gate = gate
        gate.engines.update({first, second})
        first._build_reconciliation_clearance_snapshot = AsyncMock(
            return_value=_clearance_snapshot("kr_mock", "033320")
        )
        second._build_reconciliation_clearance_snapshot = AsyncMock(
            return_value=_clearance_snapshot("kr_mock", "003480", incomplete=True)
        )

        await first._refresh_dashboard_controls()

        assert first._trading_paused is True
        assert second._trading_paused is True
        assert any("003480" in call.args[0] for call in first.ctx.logger.warning.call_args_list)
    asyncio.run(check())


def test_dashboard_activation_fresh_check_exception_is_handled(tmp_path):
    async def check():
        engine = _dashboard_engine("kr_mock", "033320", tmp_path, reason="broker_reconciliation_unavailable")
        engine._build_reconciliation_clearance_snapshot = AsyncMock(
            side_effect=RuntimeError("fresh balance unavailable")
        )

        await engine._refresh_dashboard_controls()

        assert engine._trading_paused is True
        assert any("fresh balance unavailable" in call.args[0] for call in engine.ctx.logger.warning.call_args_list)
    asyncio.run(check())


def test_dashboard_activation_with_no_matching_engine_leaves_state_unchanged(tmp_path):
    async def check():
        engine = _dashboard_engine("kr_mock", "033320", tmp_path)
        engine._balance_gate.engines.clear()
        engine._build_reconciliation_clearance_snapshot = AsyncMock(
            side_effect=RuntimeError("should not be needed")
        )

        await engine._refresh_dashboard_controls()

        assert engine._trading_paused is False
        assert engine._build_reconciliation_clearance_snapshot.await_count == 0
    asyncio.run(check())


def test_dashboard_activation_fresh_check_does_not_interfere_with_sync_lock(tmp_path):
    async def check():
        engine = _dashboard_engine("kr_mock", "033320", tmp_path, reason="broker_reconciliation_unavailable")
        engine._sync_lock = asyncio.Lock()

        async def snapshot(symbol, *, max_balance_age_sec):
            assert engine._sync_lock.locked() is False
            return _clearance_snapshot("kr_mock", symbol)

        engine._build_reconciliation_clearance_snapshot = snapshot
        await engine._refresh_dashboard_controls()

        assert engine._trading_paused is False
    asyncio.run(check())


def test_clear_event_with_no_matching_engines_remains_unconsumed(tmp_path):
    async def check():
        engine = _engine("kr_mock", "033320", tmp_path)
        write_pause_clear_event("kr_mock", "broker_quantity_unattributed", data_dir=tmp_path)

        await engine._apply_reconciliation_clear_event()

        assert engine._balance_gate.pause_clear_event_id == ""
        assert any("matched no engines" in call.args[0] for call in engine.ctx.logger.warning.call_args_list)
    asyncio.run(check())


def test_fixed_port_clear_event_still_short_circuits_fresh_check(tmp_path):
    async def check():
        engine = _engine("kr_mock", "033320", tmp_path)
        engine._build_reconciliation_clearance_snapshot = AsyncMock()
        write_pause_clear_event("kr_mock", FIXED_PORT_DEGRADED_PAUSE_REASON, data_dir=tmp_path)

        await engine._apply_reconciliation_clear_event()

        assert engine._balance_gate.pause_clear_event_id == ""
        engine._build_reconciliation_clearance_snapshot.assert_not_awaited()
    asyncio.run(check())


def test_sync_broker_state_runs_clearance_before_sync_lock(tmp_path):
    async def check():
        engine = _sync_engine("kr_mock", tmp_path, balance_only=True)
        observed_lock_states = []

        async def apply_clear_event():
            observed_lock_states.append(engine._sync_lock.locked())

        engine._apply_reconciliation_clear_event = apply_clear_event
        engine._reconcile_balance = AsyncMock()

        assert await engine.sync_broker_state() is True
        assert observed_lock_states == [False]
    asyncio.run(check())


class BalanceReconciliationCycleCharacterizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_resets_shared_state_and_flushes_dashboard_fills_when_requested(self):
        engine = _sync_engine("kr_mock", ".", balance_only=False)
        engine._balance_gate.balance_backoff_sec = 20.0
        engine._record_reconciliation_failure(RetryableError("prior failure"))
        engine._reconcile_balance = AsyncMock()
        engine._flush_dashboard_fills = Mock()

        self.assertTrue(await engine._run_balance_reconciliation_cycle(flush_dashboard_fills=True))

        engine._reconcile_balance.assert_awaited_once()
        engine._flush_dashboard_fills.assert_called_once_with()
        self.assertGreater(engine._last_balance_request_at, 0.0)
        self.assertEqual(engine._last_balance_request_at, engine._last_balance_reconciliation)
        self.assertEqual(engine._balance_gate.balance_backoff_sec, 5.0)
        self.assertEqual(engine._balance_gate.reconciliation_failure_count, 0)

    async def test_success_for_passive_worker_does_not_flush_dashboard_fills(self):
        engine = _sync_engine("kr_mock", ".", balance_only=True)
        engine._reconcile_balance = AsyncMock()
        engine._flush_dashboard_fills = Mock()

        self.assertTrue(await engine._run_balance_reconciliation_cycle(flush_dashboard_fills=False))

        engine._reconcile_balance.assert_awaited_once()
        engine._flush_dashboard_fills.assert_not_called()

    async def test_retryable_failure_records_failure_without_success_side_effects(self):
        engine = _sync_engine("kr_mock", ".", balance_only=True)
        engine._last_balance_request_at = 11.0
        engine._last_balance_reconciliation = 12.0
        engine._reconcile_balance = AsyncMock(side_effect=RetryableError("balance unavailable"))
        engine._flush_dashboard_fills = Mock()

        self.assertFalse(await engine._run_balance_reconciliation_cycle(flush_dashboard_fills=True))

        engine._flush_dashboard_fills.assert_not_called()
        self.assertEqual(engine._last_balance_request_at, 11.0)
        self.assertEqual(engine._last_balance_reconciliation, 12.0)
        self.assertEqual(engine._balance_gate.reconciliation_failure_count, 1)

    async def test_rate_limit_is_deferred_without_failure_or_success_side_effects(self):
        engine = _sync_engine("kr_mock", ".", balance_only=True)
        engine._last_balance_request_at = 11.0
        engine._last_balance_reconciliation = 12.0
        engine._balance_gate.balance_backoff_sec = 5.0
        engine._reconcile_balance = AsyncMock(
            side_effect=KiwoomAPIError("kt00018", "429", "rate limited")
        )
        engine._flush_dashboard_fills = Mock()

        with patch("src.core.engine.emit_rate_limit_event") as emit_rate_limit:
            self.assertFalse(await engine._run_balance_reconciliation_cycle(flush_dashboard_fills=True))

        emit_rate_limit.assert_called_once()
        engine._flush_dashboard_fills.assert_not_called()
        self.assertEqual(engine._last_balance_request_at, 11.0)
        self.assertEqual(engine._last_balance_reconciliation, 12.0)
        self.assertEqual(engine._balance_gate.reconciliation_failure_count, 0)
        self.assertEqual(engine._balance_gate.balance_backoff_sec, 10.0)
        self.assertTrue(engine._balance_sync_blocked)

    async def test_non_rate_limit_api_error_is_reraised(self):
        engine = _sync_engine("kr_mock", ".", balance_only=False)
        api_error = KiwoomAPIError("kt00018", "500", "broker error")
        engine._reconcile_balance = AsyncMock(side_effect=api_error)
        engine._flush_dashboard_fills = Mock()

        with self.assertRaisesRegex(KiwoomAPIError, "broker error"):
            await engine._run_balance_reconciliation_cycle(flush_dashboard_fills=True)

        engine._flush_dashboard_fills.assert_not_called()
        self.assertEqual(engine._balance_gate.reconciliation_failure_count, 0)


class SyncBrokerStateIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_passive_holdoff_failure_returns_false_and_preserves_prior_state(self):
        engine = _sync_engine("kr_mock", ".", balance_only=True)
        engine._last_balance_reconciliation = 123.0
        enter_fixed_port_degraded_state("kr_mock", "rest", local_port=10000)
        try:
            engine._reconcile_balance = AsyncMock(
                side_effect=RetryableError("fixed-port holdoff active")
            )
            self.assertFalse(await engine.sync_broker_state())
            self.assertEqual(engine._last_balance_reconciliation, 123.0)
            self.assertEqual(engine._balance_gate.reconciliation_failure_count, 1)
        finally:
            clear_fixed_port_degraded_state("kr_mock")

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
            engine._build_reconciliation_clearance_snapshot = AsyncMock(
                return_value=_clearance_snapshot("kr_mock", "005930")
            )
            engine._reconcile_balance = AsyncMock(side_effect=RetryableError("still unavailable"))

            self.assertFalse(await engine.sync_broker_state())
            self.assertEqual(engine._pause_reason, "")
            self.assertEqual(engine._balance_gate.reconciliation_failure_count, 1)
