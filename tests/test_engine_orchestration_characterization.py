import unittest
from types import SimpleNamespace

from src.core import engine as engine_module


class AccountEngineRunCharacterizationTest(unittest.IsolatedAsyncioTestCase):
    async def test_run_characterizes_startup_restore_sync_tick_and_cleanup_order(self):
        order = []

        async def record_dashboard_controls():
            order.append("refresh_dashboard_controls")

        async def record_sync(force_balance):
            order.append(f"sync_broker_state(force_balance={force_balance})")
            return True

        async def record_tick():
            order.append("tick")

        async def record_wait():
            order.append("wait_for_next_tick_or_control_change")
            raise StopAsyncIteration

        engine = engine_module.AccountEngine.__new__(engine_module.AccountEngine)
        engine.ctx = SimpleNamespace(
            account_id="acct-1",
            display_name="acct-1",
            strategy=SimpleNamespace(symbol="005930"),
            logger=SimpleNamespace(
                info=lambda *args, **kwargs: None,
                warning=lambda *args, **kwargs: None,
                exception=lambda *args, **kwargs: None,
                error=lambda *args, **kwargs: None,
            ),
            price_feed_obj=SimpleNamespace(
                realtime=SimpleNamespace(
                    remove_doorbell_callback=lambda callback: order.append("realtime_remove_doorbell_callback"),
                    unsubscribe=lambda symbol: order.append(f"realtime_unsubscribe({symbol})"),
                )
            ),
        )
        engine.telegram = SimpleNamespace(notify_error=lambda *args, **kwargs: None)
        engine.data_dir = SimpleNamespace()
        engine._auto_trading_enabled = False
        engine._dashboard_auto_buy = False
        engine._dashboard_auto_sell = False
        engine._dashboard_profile_allowed = False

        engine._backup_ledger_at_startup = lambda: order.append("startup_backup")
        engine._restore_from_ledger = lambda: order.append("restore_from_ledger")
        engine._refresh_runtime_control = lambda: order.append("refresh_runtime_control")
        engine._refresh_dashboard_controls = record_dashboard_controls
        engine.sync_broker_state = record_sync
        engine._tick = record_tick
        engine._heartbeat = lambda: order.append("heartbeat")
        engine._wait_for_next_tick_or_control_change = record_wait

        with self.assertRaises(StopAsyncIteration):
            await engine.run()

        self.assertEqual(
            order,
            [
                "startup_backup",
                "restore_from_ledger",
                "refresh_runtime_control",
                "refresh_dashboard_controls",
                "sync_broker_state(force_balance=True)",
                "tick",
                "heartbeat",
                "wait_for_next_tick_or_control_change",
                "realtime_remove_doorbell_callback",
                "realtime_unsubscribe(005930)",
            ],
        )

    async def test_run_continues_after_startup_sync_failure_and_cleans_up(self):
        order = []

        async def record_dashboard_controls():
            order.append("refresh_dashboard_controls")

        async def fail_sync(force_balance):
            order.append(f"sync_broker_state(force_balance={force_balance})")
            raise RuntimeError("startup balance unavailable")

        async def record_tick():
            order.append("tick")

        async def record_wait():
            order.append("wait_for_next_tick_or_control_change")
            raise StopAsyncIteration

        engine = engine_module.AccountEngine.__new__(engine_module.AccountEngine)
        engine.ctx = SimpleNamespace(
            account_id="acct-1",
            display_name="acct-1",
            strategy=SimpleNamespace(symbol="005930"),
            logger=SimpleNamespace(
                info=lambda *args, **kwargs: None,
                warning=lambda *args, **kwargs: order.append("startup_sync_warning"),
                exception=lambda *args, **kwargs: None,
                error=lambda *args, **kwargs: None,
            ),
            price_feed_obj=SimpleNamespace(
                realtime=SimpleNamespace(
                    remove_doorbell_callback=lambda callback: order.append("realtime_remove_doorbell_callback"),
                    unsubscribe=lambda symbol: order.append(f"realtime_unsubscribe({symbol})"),
                )
            ),
        )
        engine.telegram = SimpleNamespace(notify_error=lambda *args, **kwargs: None)
        engine.data_dir = SimpleNamespace()
        engine._auto_trading_enabled = False
        engine._dashboard_auto_buy = False
        engine._dashboard_auto_sell = False
        engine._dashboard_profile_allowed = False

        engine._backup_ledger_at_startup = lambda: order.append("startup_backup")
        engine._restore_from_ledger = lambda: order.append("restore_from_ledger")
        engine._refresh_runtime_control = lambda: order.append("refresh_runtime_control")
        engine._refresh_dashboard_controls = record_dashboard_controls
        engine.sync_broker_state = fail_sync
        engine._tick = record_tick
        engine._heartbeat = lambda: order.append("heartbeat")
        engine._wait_for_next_tick_or_control_change = record_wait

        with self.assertRaises(StopAsyncIteration):
            await engine.run()

        self.assertEqual(
            order,
            [
                "startup_backup",
                "restore_from_ledger",
                "refresh_runtime_control",
                "refresh_dashboard_controls",
                "sync_broker_state(force_balance=True)",
                "startup_sync_warning",
                "tick",
                "heartbeat",
                "wait_for_next_tick_or_control_change",
                "realtime_remove_doorbell_callback",
                "realtime_unsubscribe(005930)",
            ],
        )
