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
from unittest.mock import patch

import src.core.engine as engine_module
from src.core.account_manager import AccountContext
from src.core.engine import AccountEngine
from src.core.kiwoom_client import KiwoomClient
from src.data.trade_ledger import PendingOrder
from src.strategy.base import Action, MarketSnapshot, OrderIntent, PositionState
from src.strategy.infinite_grid import InfiniteGridStrategy
from tests.support.telegram_double import make_telegram_double


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
        self.balance_calls = 0

    async def get_balance(self, *, exchange=None):
        if exchange is None:
            self.balance_calls += 1
        elif exchange != "KRX":
            raise ValueError("mock KR supports only KRX")
        return {"return_code": 0, "_balance_pages_complete": True, "acnt_evlt_remn_indv_tot": [{
            "stk_cd": self.symbol, "stk_nm": "TEST", "rmnd_qty": str(self.qty),
            "pur_pric": str(self.avg), "cur_prc": str(self.avg),
        }]}

    async def get_executed_orders(self, _symbol):
        return {"cntr": []}


class _Logger:
    def info(self, *_args): pass
    def warning(self, *_args): pass
    def error(self, *_args): pass
    def exception(self, *_args): pass


class ManualTrancheLifecycleTest(unittest.TestCase):
    def test_explicit_balance_exchange_preserves_default_request(self):
        async def scenario():
            client = KiwoomClient.__new__(KiwoomClient)
            client.market = "KR"
            requests = []

            async def post(_path, _api_id, body, **kwargs):
                requests.append(dict(body))
                kwargs["response_headers"].update({"cont-yn": "N", "next-key": None})
                return {"return_code": 0, "acnt_evlt_remn_indv_tot": []}

            client._post = post
            await client.get_balance()
            await client.get_balance(exchange="NXT")
            self.assertEqual(
                [body["dmst_stex_tp"] for body in requests], ["KRX", "NXT"],
            )
            async def no_continuation_header(_path, _api_id, _body, **_kwargs):
                return {"return_code": 0, "acnt_evlt_remn_indv_tot": []}

            client._post = no_continuation_header
            self.assertTrue((await client.get_balance(exchange="NXT"))["_balance_pages_complete"])

            async def nonempty_without_continuation(_path, _api_id, _body, **_kwargs):
                return {
                    "return_code": 0,
                    "acnt_evlt_remn_indv_tot": [{"stk_cd": "A000490", "rmnd_qty": "1"}],
                }

            client._post = nonempty_without_continuation
            with self.assertRaises(ValueError):
                await client.get_balance(exchange="NXT")

            async def missing_page_status(_path, _api_id, _body, **kwargs):
                kwargs["response_headers"]["cont-yn"] = "N"
                return {"acnt_evlt_remn_indv_tot": []}

            client._post = missing_page_status
            with self.assertRaises(ValueError):
                await client.get_balance(exchange="NXT")
            with self.assertRaises(ValueError):
                await client.get_balance(exchange="SOR")

            client.market = "US"
            requests.clear()

            async def us_post(_path, _api_id, body, **kwargs):
                requests.append(dict(body))
                kwargs["response_headers"].update({"cont-yn": "N", "next-key": None})
                return {"return_code": 0, "result_list": []}

            client._post = us_post
            await client.get_balance()
            await client.get_balance(exchange="NY")
            self.assertEqual([body["stex_tp"] for body in requests], ["", "NY"])

        asyncio.run(scenario())

    def test_orphan_observation_checks_every_real_exchange_without_summing_overlap(self):
        class VenueClient:
            market = "KR"
            mode = "real"

            def __init__(self):
                self.calls = []
                self.rows = {
                    "KRX": [{"stk_cd": "A000490", "rmnd_qty": "2"}],
                    "NXT": [{"stk_cd": "A000490", "rmnd_qty": "2"}],
                }

            async def get_balance(self, *, exchange=None):
                self.calls.append(exchange)
                return {
                    "return_code": 0,
                    "_balance_pages_complete": True,
                    "acnt_evlt_remn_indv_tot": self.rows[exchange],
                }

        async def scenario():
            client = VenueClient()
            engine = AccountEngine.__new__(AccountEngine)
            engine.ctx = SimpleNamespace(client=client, logger=_Logger())
            engine._balance_gate = engine_module._AccountBalanceGate()
            engine.balance_min_interval_sec = 3600

            first, complete, first_generation, fresh, _ = await engine._shared_orphan_balance()
            self.assertTrue(complete and fresh)
            self.assertEqual(first, {"000490": 2.0})
            self.assertEqual(client.calls, ["KRX", "NXT"])

            cached, complete, cached_generation, fresh, _ = await engine._shared_orphan_balance()
            self.assertTrue(complete)
            self.assertFalse(fresh)
            self.assertEqual(cached, first)
            self.assertEqual(cached_generation, first_generation)
            self.assertEqual(client.calls, ["KRX", "NXT"])

            client.rows["KRX"] = []
            client.rows["NXT"] = []
            zero, complete, zero_generation, fresh, _ = await engine._shared_orphan_balance(force=True)
            self.assertEqual(zero, {})
            self.assertTrue(complete and fresh)
            self.assertNotEqual(zero_generation, first_generation)

            client.rows["NXT"] = [{"stk_cd": "A000490", "rmnd_qty": "bad"}]
            blocked, complete, blocked_generation, fresh, _ = await engine._shared_orphan_balance(force=True)
            self.assertEqual(blocked, {})
            self.assertFalse(complete or fresh)
            self.assertEqual(blocked_generation, "")
            self.assertEqual(engine._balance_gate.balance_generation, 2)

            for malformed_symbol in (["A000490"], {"code": "A000490"}, "A000490?"):
                client.rows["NXT"] = [{"stk_cd": malformed_symbol, "rmnd_qty": "2"}]
                blocked, complete, blocked_generation, fresh, _ = await engine._shared_orphan_balance(force=True)
                self.assertEqual(blocked, {})
                self.assertFalse(complete or fresh)
                self.assertEqual(blocked_generation, "")
                self.assertEqual(engine._balance_gate.balance_generation, 2)

        asyncio.run(scenario())

    def test_orphan_observation_checks_us_venues_and_mock_kr_scope(self):
        class VenueClient:
            def __init__(self, market, mode, rows):
                self.market, self.mode, self.rows = market, mode, rows
                self.calls = []

            async def get_balance(self, *, exchange=None):
                self.calls.append(exchange)
                key = "result_list" if self.market == "US" else "acnt_evlt_remn_indv_tot"
                return {"return_code": 0, "_balance_pages_complete": True, key: self.rows[exchange]}

        async def observe(client):
            engine = AccountEngine.__new__(AccountEngine)
            engine.ctx = SimpleNamespace(client=client, logger=_Logger())
            engine._balance_gate = engine_module._AccountBalanceGate()
            engine.balance_min_interval_sec = 0
            return await engine._shared_orphan_balance()

        async def scenario():
            us = VenueClient("US", "real", {
                "ND": [], "NY": [{"stk_cd": "AAPL", "poss_qty": "1"}], "NA": [],
            })
            quantities, complete, _, fresh, _ = await observe(us)
            self.assertTrue(complete and fresh)
            self.assertEqual(quantities, {"AAPL": 1.0})
            self.assertEqual(us.calls, ["ND", "NY", "NA"])

            malformed_us = VenueClient("US", "real", {
                "ND": [], "NY": [{"stk_cd": ["AAPL"], "poss_qty": "1"}], "NA": [],
            })
            _, complete, _, fresh, _ = await observe(malformed_us)
            self.assertFalse(complete or fresh)

            kr_mock = VenueClient("KR", "mock", {"KRX": []})
            quantities, complete, _, fresh, _ = await observe(kr_mock)
            self.assertTrue(complete and fresh)
            self.assertEqual(quantities, {})
            self.assertEqual(kr_mock.calls, ["KRX"])

        asyncio.run(scenario())

    def test_pending_cleanup_forces_new_broker_balance_in_passive_monitor(self):
        async def scenario():
            client = _Client()
            account = "kr_pending_cleanup_fresh_balance_test"
            ctx = AccountContext(
                account_id=account, display_name="pending cleanup fresh balance", client=client,
                strategy=InfiniteGridStrategy(_config()), risk_manager=None, dedup=None,
                logger=_Logger(), position=PositionState(symbol="000490"),
            )
            engine = AccountEngine(
                ctx, make_telegram_double(), None, lambda _symbol: None,
                poll_interval_sec=60, control_symbol="000490", balance_only=True,
            )
            engine.balance_min_interval_sec = 3600
            await engine._shared_broker_balance()
            self.assertEqual(client.balance_calls, 1)
            state = engine._orphan_cleaner._read_state()
            state["pendingCleanup"]["000490"] = engine._orphan_cleaner._build_intent(
                "000490", ["earlier:1", "earlier:2"],
            )
            engine._orphan_cleaner._write_state(state)
            await engine._reconcile_balance()
            self.assertEqual(client.balance_calls, 2)
            retained = engine._orphan_cleaner._read_state()
            self.assertIn("000490", retained["pendingCleanup"])

        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            original_data_dir = engine_module.DATA_DIR
            os.chdir(directory)
            engine_module.DATA_DIR = Path(directory) / "data"
            try:
                asyncio.run(scenario())
            finally:
                engine_module.DATA_DIR = original_data_dir
                os.chdir(previous)

    def test_unrecognized_normal_balance_records_blocked_cleanup(self):
        async def scenario():
            engine = AccountEngine.__new__(AccountEngine)
            engine.ctx = SimpleNamespace(
                client=SimpleNamespace(market="KR"),
                logger=_Logger(),
                strategy=SimpleNamespace(symbol="000490"),
            )
            engine._balance_gate = engine_module._AccountBalanceGate()
            engine._balance_only = False
            engine.ledger = SimpleNamespace()
            calls = []
            engine._orphan_cleaner = SimpleNamespace(
                has_pending_cleanup=lambda: True,
                sweep=lambda quantities, complete, _unresolved, **kwargs:
                    calls.append((quantities, complete, kwargs)),
            )

            async def normal_balance(*, max_age_sec=None):
                self.assertEqual(max_age_sec, 0)
                return {"unexpected": []}, 0.0, "run:1", True

            async def orphan_balance(*, force=False):
                self.assertTrue(force)
                return {}, False, "", False, "2026-09-23T00:00:00+00:00"

            engine._shared_broker_balance = normal_balance
            engine._shared_orphan_balance = orphan_balance
            await engine._reconcile_balance()
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], {})
            self.assertFalse(calls[0][1])

        asyncio.run(scenario())

    def test_zero_balance_interval_does_not_reuse_same_tick_cache(self):
        async def scenario():
            client = _Client()
            account = "kr_zero_balance_interval_cache_test"
            ctx = AccountContext(
                account_id=account, display_name="zero balance interval cache", client=client,
                strategy=InfiniteGridStrategy(_config()), risk_manager=None, dedup=None,
                logger=_Logger(), position=PositionState(symbol="000490"),
            )
            engine = AccountEngine(
                ctx, make_telegram_double(), None,
                lambda _symbol: None, poll_interval_sec=60, control_symbol="000490",
            )
            engine.balance_min_interval_sec = 0
            fixed_loop = SimpleNamespace(time=lambda: 123.0)
            try:
                self.assertTrue(engine._orphan_balance_complete(await client.get_balance()))
                self.assertFalse(engine._orphan_balance_complete({
                    "acnt_evlt_remn_indv_tot": [],
                }))
                self.assertFalse(engine._orphan_balance_complete({
                    "_balance_pages_complete": True,
                    "acnt_evlt_remn_indv_tot": [{"stk_cd": "000490", "rmnd_qty": "bad"}],
                }))
                first, _, first_generation, first_fresh = await engine._shared_broker_balance()
                self.assertEqual(first["acnt_evlt_remn_indv_tot"][0]["rmnd_qty"], "1")
                self.assertTrue(first_fresh)
                client.qty = 2
                engine._balance_gate.received_at = 123.0
                with patch.object(
                    engine_module.asyncio,
                    "get_running_loop",
                    return_value=fixed_loop,
                ):
                    second, _, second_generation, second_fresh = await engine._shared_broker_balance()
                self.assertEqual(second["acnt_evlt_remn_indv_tot"][0]["rmnd_qty"], "2")
                self.assertTrue(second_fresh)
                self.assertNotEqual(first_generation, second_generation)
            finally:
                engine.ledger.close()

        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            original_data_dir = engine_module.DATA_DIR
            os.chdir(directory)
            engine_module.DATA_DIR = Path(directory) / "data"
            try:
                asyncio.run(scenario())
            finally:
                engine_module.DATA_DIR = original_data_dir
                os.chdir(previous)

    def test_balance_notification_waits_for_dashboard_position_initialization(self):
        async def scenario():
            client = _Client()
            cfg = _config()
            account = "kr_balance_notification_order_test"
            ctx = AccountContext(
                account_id=account, display_name="balance notification order", client=client,
                strategy=InfiniteGridStrategy(cfg), risk_manager=None, dedup=None,
                logger=_Logger(), position=PositionState(symbol="000490", qty=0),
            )
            telegram = make_telegram_double()
            observed = []

            async def observe_notification(_message):
                observed.append((ctx.position.qty, ctx.position.step, dict(ctx.strategy.step_qty)))

            telegram.notify_balance_change.side_effect = observe_notification
            engine = AccountEngine(ctx, telegram, None,
                                   lambda _symbol: None, poll_interval_sec=60, control_symbol="000490")
            engine._auto_trading_enabled = True
            engine.balance_min_interval_sec = 0
            engine._orphan_cleaner.sweep = lambda *_args, **_kwargs: []
            engine._dashboard_symbol = "000490"
            engine._lifecycle_pending_adoption = False
            try:
                await engine._reconcile_balance()
                self.assertEqual(observed, [(1, 1, {1: 1})])
                self.assertEqual(ctx.position.qty, 1)
                self.assertEqual(ctx.position.step, 1)
                telegram.notify_balance_change.assert_awaited_once()
            finally:
                engine.ledger.close()

        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            original_data_dir = engine_module.DATA_DIR
            os.chdir(directory)
            engine_module.DATA_DIR = Path(directory) / "data"
            try:
                asyncio.run(scenario())
            finally:
                engine_module.DATA_DIR = original_data_dir
                os.chdir(previous)

    def test_repeated_dashboard_refresh_keeps_one_pending_activation(self):
        """Refreshes before the first balance must not replace the adoption boundary."""
        async def scenario():
            client = _Client()
            cfg = _config()
            account = "kr_pending_activation_race_test"
            ctx = AccountContext(
                account_id=account, display_name="pending activation race", client=client,
                strategy=InfiniteGridStrategy(cfg), risk_manager=None, dedup=None,
                logger=_Logger(), position=PositionState(symbol="000490"),
            )
            engine = AccountEngine(ctx, make_telegram_double(), None,
                                   lambda _symbol: None, poll_interval_sec=60, control_symbol="000490")
            engine._auto_trading_enabled = True
            engine.balance_min_interval_sec = 0
            try:
                Path(f"data/dashboard_settings_{account}.json").write_text(
                    json.dumps({"profiles": [{"enabled": True, "config": cfg}]}), encoding="utf-8"
                )
                Path(f"data/dashboard_control_{account}_000490.json").write_text(
                    json.dumps({"symbol": "000490", "config": cfg, "auto_buy": True, "auto_sell": True}),
                    encoding="utf-8",
                )
                await engine._refresh_dashboard_controls()
                first = engine._symbol_lifecycles["000490"]["started_at"]
                await engine._refresh_dashboard_controls()
                self.assertEqual(engine._symbol_lifecycles["000490"]["started_at"], first)
                await engine.sync_broker_state(force_balance=True)
                self.assertFalse(engine._trading_paused)
                self.assertEqual(ctx.strategy.step_qty, {1: 1})
            finally:
                engine.ledger.close()

        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            original_data_dir = engine_module.DATA_DIR
            os.chdir(directory)
            engine_module.DATA_DIR = Path(directory) / "data"
            try:
                asyncio.run(scenario())
            finally:
                engine_module.DATA_DIR = original_data_dir
                os.chdir(previous)

    def test_immediate_t2_trigger_submits_exactly_one_order(self):
        async def scenario():
            class OrderClient(_Client):
                def __init__(self):
                    super().__init__()
                    self.submissions = []

                async def place_order(self, **kwargs):
                    self.submissions.append(dict(kwargs))
                    return SimpleNamespace(ord_no="T2-ACCEPTED")

            client = OrderClient()
            cfg = _config()
            account = "kr_exactly_one_t2_order_test"
            ctx = AccountContext(
                account_id=account, display_name="one T2 order", client=client,
                strategy=InfiniteGridStrategy(cfg),
                risk_manager=SimpleNamespace(approve=lambda *_args: (True, "")), dedup=None,
                logger=_Logger(), position=PositionState(symbol="000490"),
            )
            engine = AccountEngine(ctx, make_telegram_double(), None,
                                   lambda _symbol: None, poll_interval_sec=60, control_symbol="000490")
            engine.balance_min_interval_sec = 0
            try:
                Path(f"data/dashboard_settings_{account}.json").write_text(
                    json.dumps({"profiles": [{"enabled": True, "config": cfg}]}), encoding="utf-8"
                )
                Path(f"data/dashboard_control_{account}_000490.json").write_text(
                    json.dumps({"symbol": "000490", "config": cfg, "auto_buy": True, "auto_sell": True}),
                    encoding="utf-8",
                )
                await engine._refresh_dashboard_controls()
                await engine.sync_broker_state(force_balance=True)
                intent = OrderIntent(Action.BUY, "000490", 2, 9_900, meta={"step": 2})
                await engine._handle_intent(intent, 9_900)
                self.assertEqual(len(client.submissions), 1)
                self.assertEqual(client.submissions[0]["qty"], 2)
                self.assertEqual(client.submissions[0]["symbol"], "000490")
            finally:
                engine.ledger.close()

        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            original_data_dir = engine_module.DATA_DIR
            os.chdir(directory)
            engine_module.DATA_DIR = Path(directory) / "data"
            try:
                asyncio.run(scenario())
            finally:
                engine_module.DATA_DIR = original_data_dir
                os.chdir(previous)

    def test_archive_reenable_adopts_fresh_lifecycle(self):
        async def scenario():
            client = _Client()
            cfg = _config()
            account = "kr_archive_reenable_test"
            ctx = AccountContext(
                account_id=account, display_name="archive re-enable", client=client,
                strategy=InfiniteGridStrategy(cfg), risk_manager=None, dedup=None,
                logger=_Logger(), position=PositionState(symbol="000490"),
            )
            engine = AccountEngine(ctx, make_telegram_double(), None,
                                   lambda _symbol: None, poll_interval_sec=60, control_symbol="000490")
            engine.balance_min_interval_sec = 0
            settings = Path(f"data/dashboard_settings_{account}.json")
            control = Path(f"data/dashboard_control_{account}_000490.json")
            lifecycle = Path(f"data/symbol_lifecycles_{account}.json")
            try:
                settings.write_text(json.dumps({"profiles": [{"enabled": True, "config": cfg}]}), encoding="utf-8")
                control.write_text(json.dumps({"symbol": "000490", "config": cfg, "auto_buy": True, "auto_sell": True}), encoding="utf-8")
                await engine._refresh_dashboard_controls()
                await engine.sync_broker_state(force_balance=True)
                old_id = engine._symbol_lifecycles["000490"]["activation_id"]

                client.qty = 0
                engine._balance_gate.raw_balance = None
                await engine.sync_broker_state(force_balance=True)
                engine._balance_gate.raw_balance = None
                await engine.sync_broker_state(force_balance=True)
                archived_controls = list((Path("data") / "archive").rglob(f"dashboard_control_{account}_000490_*.json"))
                self.assertTrue(archived_controls)
                self.assertFalse(control.exists())
                self.assertEqual(json.loads(settings.read_text(encoding="utf-8"))["profiles"], [])
                self.assertEqual(json.loads(lifecycle.read_text(encoding="utf-8"))["000490"]["status"], "closed")

                client.qty, client.avg = 2, 20_000
                settings.write_text(json.dumps({"profiles": [{"enabled": True, "config": cfg}]}), encoding="utf-8")
                control.write_text(json.dumps({"symbol": "000490", "config": cfg, "auto_buy": True, "auto_sell": True}), encoding="utf-8")
                await engine._refresh_dashboard_controls()
                new_id = engine._symbol_lifecycles["000490"]["activation_id"]
                await engine.sync_broker_state(force_balance=True)
                self.assertNotEqual(new_id, old_id)
                self.assertEqual(engine._symbol_lifecycles["000490"]["manual_qty"], 2.0)
                self.assertEqual(engine._symbol_lifecycles["000490"]["activation_id"], new_id)
            finally:
                engine.ledger.close()

        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            original_data_dir = engine_module.DATA_DIR
            os.chdir(directory)
            engine_module.DATA_DIR = Path(directory) / "data"
            try:
                asyncio.run(scenario())
            finally:
                engine_module.DATA_DIR = original_data_dir
                os.chdir(previous)

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
            engine = AccountEngine(ctx, make_telegram_double(), None,
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
                await engine._refresh_dashboard_controls()
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
            original_data_dir = engine_module.DATA_DIR
            os.chdir(directory)
            engine_module.DATA_DIR = Path(directory) / "data"
            try:
                asyncio.run(scenario())
            finally:
                engine_module.DATA_DIR = original_data_dir
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
            engine = AccountEngine(ctx, make_telegram_double(), None,
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
                await engine._refresh_dashboard_controls()  # immediate enable after HTS buy
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
            original_data_dir = engine_module.DATA_DIR
            os.chdir(directory)
            engine_module.DATA_DIR = Path(directory) / "data"
            try:
                asyncio.run(scenario())
            finally:
                engine_module.DATA_DIR = original_data_dir
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
                instance = AccountEngine(context, make_telegram_double(), None,
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
                await first._refresh_dashboard_controls()
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
            original_data_dir = engine_module.DATA_DIR
            os.chdir(directory)
            engine_module.DATA_DIR = Path(directory) / "data"
            try:
                asyncio.run(scenario())
            finally:
                engine_module.DATA_DIR = original_data_dir
                os.chdir(previous)

    def test_restart_pauses_ambiguous_manual_t1_rebuild_after_later_partial_sell(self):
        """Missing ledger-confirmed Line 1 provenance must fail closed on restart."""
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
                instance = AccountEngine(context, make_telegram_double(), None,
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
                await first._refresh_dashboard_controls()
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
                await restarted._refresh_dashboard_controls()
                restarted._restore_from_ledger()
                await restarted.sync_broker_state(force_balance=True)
                self.assertTrue(restarted._trading_paused)
                self.assertEqual(restarted._pause_reason, "tranche_rebuild_ambiguous")
                self.assertEqual(ctx.strategy.step_qty, {2: 20, 3: 0})
                self.assertEqual(ctx.position.qty, 20.0)
            finally:
                restarted.ledger.close()

        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            original_data_dir = engine_module.DATA_DIR
            os.chdir(directory)
            engine_module.DATA_DIR = Path(directory) / "data"
            try:
                asyncio.run(scenario())
            finally:
                engine_module.DATA_DIR = original_data_dir
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
                instance = AccountEngine(context, make_telegram_double(), None,
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
                await first._refresh_dashboard_controls()
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
                await restarted._refresh_dashboard_controls()
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
            original_data_dir = engine_module.DATA_DIR
            os.chdir(directory)
            engine_module.DATA_DIR = Path(directory) / "data"
            try:
                asyncio.run(scenario())
            finally:
                engine_module.DATA_DIR = original_data_dir
                os.chdir(previous)

    def test_mismatched_broker_quantity_stays_paused_with_reason(self):
        async def scenario():
            client = _Client()
            cfg = _config()
            account = "kr_mismatched_quantity_pause_test"
            ctx = AccountContext(
                account_id=account, display_name="mismatched quantity", client=client,
                strategy=InfiniteGridStrategy(cfg), risk_manager=None, dedup=None,
                logger=_Logger(), position=PositionState(symbol="000490"),
            )
            engine = AccountEngine(ctx, make_telegram_double(), None,
                                   lambda _symbol: None, poll_interval_sec=60, control_symbol="000490")
            engine.balance_min_interval_sec = 0
            try:
                Path(f"data/dashboard_settings_{account}.json").write_text(
                    json.dumps({"profiles": [{"enabled": True, "config": cfg}]}), encoding="utf-8"
                )
                Path(f"data/dashboard_control_{account}_000490.json").write_text(
                    json.dumps({"symbol": "000490", "config": cfg, "auto_buy": True, "auto_sell": True}),
                    encoding="utf-8",
                )
                await engine._refresh_dashboard_controls()
                await engine.sync_broker_state(force_balance=True)
                order = PendingOrder("T2-MISMATCH", "000490", "BUY", 2, 9_900, "BUY", 2, {})
                engine.ledger.add_pending(order)
                await engine._apply_confirmed_fill(order, engine.ledger.record_fill(order, 2, 9_900, "2026-08-20"))
                client.qty, client.avg = 4, 9_500  # expected 1 manual + 2 confirmed = 3
                engine._balance_gate.raw_balance = None
                await engine.sync_broker_state(force_balance=True)
                self.assertTrue(engine._trading_paused)
                self.assertEqual(engine._pause_reason, "broker_quantity_unattributed")
            finally:
                engine.ledger.close()

        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            original_data_dir = engine_module.DATA_DIR
            os.chdir(directory)
            engine_module.DATA_DIR = Path(directory) / "data"
            try:
                asyncio.run(scenario())
            finally:
                engine_module.DATA_DIR = original_data_dir
                os.chdir(previous)

    def test_reentrant_three_way_activation_creates_one_adoption(self):
        async def scenario():
            client = _Client()
            cfg = _config()
            account = "kr_three_way_activation_race_test"
            ctx = AccountContext(
                account_id=account, display_name="three-way activation", client=client,
                strategy=InfiniteGridStrategy(cfg), risk_manager=None, dedup=None,
                logger=_Logger(), position=PositionState(symbol="000490"),
            )
            engine = AccountEngine(ctx, make_telegram_double(), None,
                                   lambda _symbol: None, poll_interval_sec=60, control_symbol="000490")
            engine._auto_trading_enabled = True
            engine.balance_min_interval_sec = 0
            try:
                Path(f"data/dashboard_settings_{account}.json").write_text(
                    json.dumps({"profiles": [{"enabled": True, "config": cfg}]}), encoding="utf-8"
                )
                Path(f"data/dashboard_control_{account}_000490.json").write_text(
                    json.dumps({"symbol": "000490", "config": cfg, "auto_buy": True, "auto_sell": True}),
                    encoding="utf-8",
                )
                original_write = engine._write_lifecycles
                reentered = False
                reentered_task = None

                def reentrant_write():
                    nonlocal reentered, reentered_task
                    if not reentered:
                        reentered = True
                        reentered_task = asyncio.create_task(engine._refresh_dashboard_controls())
                    original_write()

                engine._write_lifecycles = reentrant_write
                await engine._refresh_dashboard_controls()
                await reentered_task
                activation_id = engine._symbol_lifecycles["000490"]["activation_id"]
                await engine.sync_broker_state(force_balance=True)
                self.assertTrue(reentered)
                self.assertEqual(engine._manual_lifecycle_adoptions, 1)
                self.assertEqual({activation_id}, {engine._symbol_lifecycles["000490"]["activation_id"]})
                self.assertFalse(engine._trading_paused)
            finally:
                engine.ledger.close()

        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            original_data_dir = engine_module.DATA_DIR
            os.chdir(directory)
            engine_module.DATA_DIR = Path(directory) / "data"
            try:
                asyncio.run(scenario())
            finally:
                engine_module.DATA_DIR = original_data_dir
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
