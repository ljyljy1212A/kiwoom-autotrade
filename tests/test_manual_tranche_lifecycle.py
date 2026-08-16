"""Safety test for the normal manual-tranche-1 workflow.

This is an isolated broker-shaped KR test.  It submits no broker orders: the
fake balance response represents an HTS manual buy, then a broker-confirmed
automated tranche-2 fill.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.core.account_manager import AccountContext
from src.core.engine import AccountEngine
from src.data.trade_ledger import PendingOrder
from src.strategy.base import Action, MarketSnapshot, PositionState
from src.strategy.infinite_grid import InfiniteGridStrategy


def _config() -> dict:
    return {
        "symbol": "000490", "market": "KR", "commission_rate": 0.0,
        "auto_buy": {"enabled": True, "order_type": "00"},
        "auto_sell": {"enabled": True, "order_type": "00"},
        "first_buy": {"mode": "manual", "amount": 10_000_000},
        "buy_steps": [
            {"step": 2, "drop_pct": -1.0, "amount": 99_000},
            {"step": 3, "drop_pct": -1.0, "amount": 98_000},
        ],
        "sell_steps": [
            {"step": 1, "profit_pct": 50.0}, {"step": 2, "profit_pct": 1.0},
            {"step": 3, "profit_pct": 1.0},
        ],
    }


class _Client:
    market = "KR"
    mode = "mock"

    def __init__(self):
        self.symbol, self.qty, self.avg = "000490", 1, 10_000

    async def get_balance(self):
        return {"acnt_evlt_remn_indv_tot": [{
            "stk_cd": self.symbol, "stk_nm": "TEST", "rmnd_qty": str(self.qty),
            "pur_pric": str(self.avg), "cur_prc": str(self.avg),
        }]}

    async def get_executed_orders(self, _symbol):
        return {"cntr": []}


class _Notifier:
    async def notify_fill(self, *_args):
        return None

    async def notify_error(self, *_args):
        return None

    async def notify_balance_change(self, *_args):
        return None


class _Logger:
    def info(self, *_args): pass
    def warning(self, *_args): pass
    def error(self, *_args): pass
    def exception(self, *_args): pass


class ManualTrancheLifecycleTest(unittest.TestCase):
    def test_sync_confirms_immediate_t2_before_balance_reconciliation(self):
        """A fill seen on the activation pass must be T2 before UI refresh."""
        async def scenario():
            class FillClient(_Client):
                async def get_executed_orders(self, _symbol):
                    return {"cntr": [{"ord_no": "NEW-STEP2", "cntr_qty": "14", "cntr_pric": "7020",
                                      "cntr_dt": "20260814"}]}

            client = FillClient()
            client.symbol = "001210"
            cfg = _config()
            cfg["symbol"] = "001210"
            ctx = AccountContext(
                account_id="kr_immediate_t2_test", display_name="KR immediate T2 test",
                client=client, strategy=InfiniteGridStrategy(cfg), risk_manager=None,
                dedup=None, logger=_Logger(), position=PositionState(symbol="001210"),
            )
            engine = AccountEngine(ctx, _Notifier(), SimpleNamespace(safe_send=lambda *_: None), None,
                                   lambda _symbol: None, poll_interval_sec=60, control_symbol="001210")
            engine.balance_min_interval_sec = 0
            try:
                Path("data/dashboard_settings_kr_immediate_t2_test.json").write_text(
                    json.dumps({"profiles": [{"enabled": True, "config": cfg}]}), encoding="utf-8"
                )
                Path("data/dashboard_control_kr_immediate_t2_test_001210.json").write_text(
                    json.dumps({"symbol": "001210", "config": cfg, "auto_buy": True, "auto_sell": True}),
                    encoding="utf-8",
                )
                engine._refresh_dashboard_controls()
                await engine.sync_broker_state(force_balance=True)  # adopts manual T1
                self.assertEqual({step: qty for step, qty in ctx.strategy.step_qty.items() if qty > 0}, {1: 1})

                pending = PendingOrder("NEW-STEP2", "001210", "BUY", 14, 7040, "BUY", 2, {})
                engine.ledger.add_pending(pending)
                # The broker balance and execution history arrive together.
                client.qty, client.avg = 15, (10_000 + 14 * 7_020) / 15
                engine._balance_gate.raw_balance = None
                await engine.sync_broker_state()
                await asyncio.sleep(0.05)

                self.assertEqual(ctx.strategy.step_qty, {1: 1, 2: 14})
                self.assertEqual(ctx.strategy.step_prices[1], 10_000.0)
                self.assertEqual(ctx.strategy.step_prices[2], 7_020.0)
                self.assertEqual(ctx.position.qty, 15)
                self.assertFalse(engine._trading_paused)
                event = json.loads(Path("data/dashboard_event_kr_immediate_t2_test.json").read_text(encoding="utf-8"))
                self.assertEqual(event["orderNo"], "NEW-STEP2")
                self.assertEqual(event["type"], "buy-fill")
            finally:
                engine.ledger.close()

        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            os.chdir(directory)
            try:
                asyncio.run(scenario())
            finally:
                os.chdir(previous)

    def test_manual_first_buy_then_immediate_enable_keeps_t1_and_assigns_t2(self):
        async def scenario():
            client = _Client()
            cfg = _config()
            ctx = AccountContext(
                account_id="kr_manual_lifecycle_test", display_name="KR lifecycle test",
                client=client, strategy=InfiniteGridStrategy(cfg), risk_manager=None,
                dedup=None, logger=_Logger(), position=PositionState(symbol="000490"),
            )
            engine = AccountEngine(ctx, _Notifier(), SimpleNamespace(safe_send=lambda *_: None), None,
                                   lambda _symbol: None, poll_interval_sec=60, control_symbol="000490")
            engine.balance_min_interval_sec = 0

            # Prior closed-cycle data is intentionally left in SQLite. It must
            # not affect the new manual position adopted at enablement.
            old = PendingOrder("OLD-STEP2", "000490", "BUY", 50, 9_000, "BUY", 2, {})
            engine.ledger.add_pending(old)
            engine.ledger.record_fill(old, 50, 9_000, "2026-01-01")

            try:
                Path("data/dashboard_settings_kr_manual_lifecycle_test.json").write_text(
                    json.dumps({"profiles": [{"enabled": True, "config": cfg}]}), encoding="utf-8"
                )
                Path("data/dashboard_control_kr_manual_lifecycle_test_000490.json").write_text(
                    json.dumps({"symbol": "000490", "config": cfg, "auto_buy": True, "auto_sell": True}),
                    encoding="utf-8",
                )
                engine._refresh_dashboard_controls()  # immediate enable after HTS buy
                await engine.sync_broker_state(force_balance=True)

                self.assertEqual(ctx.position.qty, 1)
                self.assertEqual(ctx.position.step, 1)
                self.assertEqual(ctx.strategy.step_qty, {1: 1})
                self.assertEqual(ctx.strategy.step_prices, {1: 10_000.0})
                self.assertEqual(engine.ledger.ledger_rows("000490"), [])
                # Even a very large configured first-buy amount cannot generate T1.
                self.assertIsNone(ctx.strategy.check_buy(MarketSnapshot("000490", 9_900, "t"), ctx.position.__class__("000490")))

                # Broker confirms the next program buy. It is attached to T2 only.
                order = PendingOrder("NEW-STEP2", "000490", "BUY", 10, 9_900, "BUY", 2, {})
                engine.ledger.add_pending(order)
                row = engine.ledger.record_fill(order, 10, 9_900, "2026-08-13")
                await engine._apply_confirmed_fill(order, row)
                # A broker may report a blended average changed by its own
                # realized-sale accounting. It cannot be used to reverse the
                # original HTS manual lot after a partial tranche exit.
                client.qty, client.avg = 11, 9_500
                engine._balance_gate.raw_balance = None
                await engine.sync_broker_state(force_balance=True)
                await asyncio.sleep(0.05)  # allow the non-blocking UI event writer to finish

                self.assertEqual(ctx.strategy.step_qty[1], 1)
                self.assertEqual(ctx.strategy.step_qty[2], 10)
                self.assertEqual(ctx.strategy.step_qty.get(3, 0), 0)
                self.assertEqual(ctx.position.step, 2)
                self.assertEqual(ctx.strategy.step_prices[1], 10_000.0)
                self.assertEqual(ctx.strategy.step_prices[2], 9_900.0)
                self.assertEqual(ctx.position.qty, 11)
                self.assertTrue(Path("data/dashboard_event_kr_manual_lifecycle_test.json").exists())
                event = json.loads(Path("data/dashboard_event_kr_manual_lifecycle_test.json").read_text(encoding="utf-8"))
                self.assertEqual(event["type"], "buy-fill")
            finally:
                engine.ledger.close()

        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            os.chdir(directory)
            try:
                asyncio.run(scenario())
            finally:
                os.chdir(previous)

    def test_open_lifecycle_restart_recovers_manual_t1_and_program_t2_t3(self):
        async def scenario():
            client = _Client()
            cfg = _config()
            account = "kr_open_lifecycle_restart_test"

            def engine_for_restart():
                context = AccountContext(
                    account_id=account, display_name="KR restart lifecycle test", client=client,
                    strategy=InfiniteGridStrategy(cfg), risk_manager=None, dedup=None,
                    logger=_Logger(), position=PositionState(symbol="000490"),
                )
                instance = AccountEngine(context, _Notifier(), SimpleNamespace(safe_send=lambda *_: None), None,
                                         lambda _symbol: None, poll_interval_sec=60, control_symbol="000490")
                instance.balance_min_interval_sec = 0
                return context, instance

            # Historical data from a closed predecessor is left in the DB.
            ctx, first = engine_for_restart()
            old = PendingOrder("OLD-CYCLE-T2", "000490", "BUY", 50, 9_000, "BUY", 2, {})
            first.ledger.add_pending(old)
            first.ledger.record_fill(old, 50, 9_000, "2026-01-01")
            try:
                Path(f"data/dashboard_settings_{account}.json").write_text(
                    json.dumps({"profiles": [{"enabled": True, "config": cfg}]}), encoding="utf-8"
                )
                Path(f"data/dashboard_control_{account}_000490.json").write_text(
                    json.dumps({"symbol": "000490", "config": cfg, "auto_buy": True, "auto_sell": True}),
                    encoding="utf-8",
                )
                first._refresh_dashboard_controls()
                await first.sync_broker_state(force_balance=True)  # adopt manual 1 @ 10,000

                for ord_no, step, price in (("NEW-T2", 2, 9_900), ("NEW-T3", 3, 9_800)):
                    order = PendingOrder(ord_no, "000490", "BUY", 10, price, "BUY", step, {})
                    first.ledger.add_pending(order)
                    row = first.ledger.record_fill(order, 10, price, "2026-08-13")
                    await first._apply_confirmed_fill(order, row)
                client.qty, client.avg = 21, (10_000 + 10 * 9_900 + 10 * 9_800) / 21
                first._balance_gate.raw_balance = None
                await first.sync_broker_state(force_balance=True)
            finally:
                first.ledger.close()

            # Simulate a stale cached base surviving the stop. Restart recovery
            # must reject it against the broker quantity/average plus T2/T3.
            Path(f"data/tranche_bases_{account}.json").write_text(
                json.dumps({"000490": 7_770}), encoding="utf-8"
            )
            ctx, restarted = engine_for_restart()
            try:
                lifecycle = json.loads(Path(f"data/symbol_lifecycles_{account}.json").read_text(encoding="utf-8"))
                self.assertEqual(lifecycle["000490"]["status"], "open")
                self.assertFalse(restarted._lifecycle_pending_adoption)
                restarted._restore_from_ledger()
                # Only current-lifecycle program fills are restored before the
                # manual broker remainder is reconciled.
                self.assertEqual(ctx.strategy.step_qty, {2: 10, 3: 10})
                self.assertEqual(ctx.strategy.step_prices, {2: 9_900.0, 3: 9_800.0})
                # A temporarily stale lower snapshot after restart must hold
                # the restored T2/T3 map and fail closed, not rebuild it.
                client.qty, client.avg = 11, 9_950
                await restarted.sync_broker_state(force_balance=True)
                self.assertEqual(ctx.strategy.step_qty, {2: 10, 3: 10})
                self.assertEqual(restarted._broker_fill_catchup_qty["000490"], 21)
                client.qty, client.avg = 21, (10_000 + 10 * 9_900 + 10 * 9_800) / 21
                restarted._balance_gate.raw_balance = None
                await restarted.sync_broker_state(force_balance=True)

                self.assertEqual(ctx.strategy.step_qty, {1: 1, 2: 10, 3: 10})
                self.assertEqual(ctx.strategy.step_prices[1], 10_000.0)
                self.assertEqual(ctx.strategy.step_prices[2], 9_900.0)
                self.assertEqual(ctx.strategy.step_prices[3], 9_800.0)
                self.assertEqual(ctx.position.qty, 21)
                saved = json.loads(Path(f"data/tranche_bases_{account}.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["000490"], 10_000.0)
            finally:
                restarted.ledger.close()

        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            os.chdir(directory)
            try:
                asyncio.run(scenario())
            finally:
                os.chdir(previous)

    def test_restart_preserves_manual_t1_sell_basis_after_later_partial_sell(self):
        """A partial T3 exit must not turn Line 1 into the broker average."""
        async def scenario():
            client = _Client()
            cfg = _config()
            account = "kr_manual_partial_sell_restart_test"

            def make_engine():
                context = AccountContext(
                    account_id=account, display_name="manual partial sell restart", client=client,
                    strategy=InfiniteGridStrategy(cfg), risk_manager=None, dedup=None,
                    logger=_Logger(), position=PositionState(symbol="000490"),
                )
                instance = AccountEngine(context, _Notifier(), SimpleNamespace(safe_send=lambda *_: None), None,
                                         lambda _symbol: None, poll_interval_sec=60, control_symbol="000490")
                instance.balance_min_interval_sec = 0
                return context, instance

            ctx, first = make_engine()
            try:
                Path(f"data/dashboard_settings_{account}.json").write_text(
                    json.dumps({"profiles": [{"enabled": True, "config": cfg}]}), encoding="utf-8"
                )
                Path(f"data/dashboard_control_{account}_000490.json").write_text(
                    json.dumps({"symbol": "000490", "config": cfg, "auto_buy": True, "auto_sell": True}),
                    encoding="utf-8",
                )
                first._refresh_dashboard_controls()
                await first.sync_broker_state(force_balance=True)  # manual T1: 1 @ 10,000
                for ord_no, step, price in (("T2", 2, 9_900), ("T3", 3, 9_800)):
                    order = PendingOrder(ord_no, "000490", "BUY", 10, price, "BUY", step, {})
                    first.ledger.add_pending(order)
                    await first._apply_confirmed_fill(order, first.ledger.record_fill(order, 10, price, "2026-08-13"))
                sell = PendingOrder("SELL-T3", "000490", "SELL", 10, 9_900, "SELL", 3, {"sell_only_step": True})
                first.ledger.add_pending(sell)
                await first._apply_confirmed_fill(sell, first.ledger.record_fill(sell, 10, 9_900, "2026-08-14"))
                client.qty, client.avg = 11, (10_000 + 10 * 9_900) / 11
                first._balance_gate.raw_balance = None
                await first.sync_broker_state(force_balance=True)
            finally:
                first.ledger.close()

            ctx, restarted = make_engine()
            try:
                restarted._refresh_dashboard_controls()
                restarted._restore_from_ledger()
                await restarted.sync_broker_state(force_balance=True)
                self.assertEqual(ctx.strategy.step_qty[1], 1)
                self.assertEqual(ctx.strategy.step_qty[2], 10)
                self.assertEqual(ctx.strategy.step_qty.get(3, 0), 0)
                self.assertEqual(ctx.position.step, 2)
                self.assertEqual(ctx.strategy.step_prices[1], 10_000.0)
                self.assertEqual(ctx.strategy.step_prices[2], 9_900.0)
                target = ctx.strategy.sell_target_price(1)
                self.assertEqual(target, 15_000.0)
                intent = ctx.strategy.check_sell(
                    MarketSnapshot("000490", 15_000.0, "t"),
                    PositionState("000490", qty=11, avg_price=client.avg, step=1),
                )
                self.assertIsNotNone(intent)
                self.assertEqual(intent.meta["step"], 1)
                self.assertEqual(intent.qty, 1)
            finally:
                restarted.ledger.close()

        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            os.chdir(directory)
            try:
                asyncio.run(scenario())
            finally:
                os.chdir(previous)

    def test_restart_after_all_automated_tranches_sell_keeps_manual_t1_basis(self):
        """A broker moving average after a fully exited grid cannot reset T1."""
        async def scenario():
            client = _Client()
            cfg = _config()
            account = "kr_manual_all_sold_restart_test"

            def make_engine():
                context = AccountContext(
                    account_id=account, display_name="manual all sold restart", client=client,
                    strategy=InfiniteGridStrategy(cfg), risk_manager=None, dedup=None,
                    logger=_Logger(), position=PositionState(symbol="000490"),
                )
                instance = AccountEngine(context, _Notifier(), SimpleNamespace(safe_send=lambda *_: None), None,
                                         lambda _symbol: None, poll_interval_sec=60, control_symbol="000490")
                instance.balance_min_interval_sec = 0
                return context, instance

            ctx, first = make_engine()
            try:
                Path(f"data/dashboard_settings_{account}.json").write_text(
                    json.dumps({"profiles": [{"enabled": True, "config": cfg}]}), encoding="utf-8"
                )
                Path(f"data/dashboard_control_{account}_000490.json").write_text(
                    json.dumps({"symbol": "000490", "config": cfg, "auto_buy": True, "auto_sell": True}),
                    encoding="utf-8",
                )
                first._refresh_dashboard_controls()
                await first.sync_broker_state(force_balance=True)  # manual T1: 1 @ 10,000
                buy = PendingOrder("T2", "000490", "BUY", 10, 9_900, "BUY", 2, {})
                first.ledger.add_pending(buy)
                await first._apply_confirmed_fill(buy, first.ledger.record_fill(buy, 10, 9_900, "2026-08-13"))
                sell = PendingOrder("SELL-T2", "000490", "SELL", 10, 10_000, "SELL", 2, {"sell_only_step": True})
                first.ledger.add_pending(sell)
                await first._apply_confirmed_fill(sell, first.ledger.record_fill(sell, 10, 10_000, "2026-08-14"))
                # Kiwoom can retain a moving average that differs from the
                # remaining manual share after all program lots close.
                client.qty, client.avg = 1, 9_900
            finally:
                first.ledger.close()

            ctx, restarted = make_engine()
            try:
                restarted._refresh_dashboard_controls()
                restarted._restore_from_ledger()
                await restarted.sync_broker_state(force_balance=True)
                self.assertEqual(
                    {step: qty for step, qty in ctx.strategy.step_qty.items() if qty > 0}, {1: 1}
                )
                self.assertEqual(ctx.strategy.step_prices, {1: 10_000.0})
                intent = ctx.strategy.check_buy(MarketSnapshot("000490", 9_900.0, "t"), ctx.position)
                self.assertIsNotNone(intent)
                self.assertEqual(intent.meta["step"], 2)
            finally:
                restarted.ledger.close()

        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            os.chdir(directory)
            try:
                asyncio.run(scenario())
            finally:
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
