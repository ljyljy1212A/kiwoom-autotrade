"""Regression coverage for skipped broker execution rows."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import src.core.engine as engine_module
from src.core.account_manager import AccountContext
from src.core.engine import AccountEngine
from src.data.trade_ledger import PendingOrder, TradeLedgerStore
from src.data.trade_ledger_migration import create_identity_ledger_copy
from src.strategy.base import PositionState
from src.strategy.infinite_grid import InfiniteGridStrategy
from tests.support.telegram_double import make_telegram_double


def _config() -> dict:
    return {
        "symbol": "000490",
        "market": "KR",
        "commission_rate": 0.0,
        "first_buy": {"mode": "manual", "amount": 10_000},
        "buy_steps": [{"step": 2, "drop_pct": -1.0, "amount": 1_000}],
        "sell_steps": [{"step": 1, "profit_pct": 1.0}],
    }


class _Client:
    market = "KR"
    mode = "mock"

    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.execution_history_calls = 0

    async def get_executed_orders(self, _symbol: str, **_kwargs) -> dict:
        self.execution_history_calls += 1
        return {"cntr": self.rows}


class _Logger:
    def __init__(self):
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def info(self, *_args):
        pass

    def warning(self, message: str, *_args):
        self.warnings.append(message)

    def error(self, message: str, *_args):
        self.errors.append(message)

    def exception(self, *_args):
        pass


class ExecutionRowSkipLoggingTest(unittest.TestCase):
    def test_skipped_rows_log_reason_without_mutating_ledger(self):
        cases = [
            (
                "no_matching_pending_or_recovery_order",
                {"ord_no": "UNKNOWN", "cntr_qty": "3", "cntr_pric": "7010"},
                PendingOrder("PENDING", "000490", "BUY", 10, 7000, "BUY", 2, {}),
                None,
                None,
            ),
            (
                "non_incremental_cumulative_quantity",
                {"ord_no": "PENDING", "cntr_qty": "5", "cntr_pric": "7010"},
                PendingOrder("PENDING", "000490", "BUY", 10, 7000, "BUY", 2, {}, filled_qty=5),
                5.0,
                7010.0,
            ),
            (
                "non_positive_execution_price",
                {"ord_no": "PENDING", "cntr_qty": "6", "cntr_pric": "0"},
                PendingOrder("PENDING", "000490", "BUY", 10, 7000, "BUY", 2, {}),
                6.0,
                0.0,
            ),
        ]
        for value in ("NaN", "Infinity", "-Infinity", "1e9999"):
            cases.extend([
                ("non_finite_cumulative_quantity",
                 {"ord_no": "PENDING", "cntr_qty": value, "cntr_pric": "7010"},
                 PendingOrder("PENDING", "000490", "BUY", 10, 7000, "BUY", 2, {}),
                 None, 7010.0),
                ("non_finite_execution_price",
                 {"ord_no": "PENDING", "cntr_qty": "6", "cntr_pric": value},
                 PendingOrder("PENDING", "000490", "BUY", 10, 7000, "BUY", 2, {}),
                 6.0, None),
            ])
        for reason, raw, pending, expected_total, expected_price in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                asyncio.run(self._assert_case(
                    Path(directory), reason, raw, pending, expected_total, expected_price
                ))

    def test_us_unconfirmed_order_identity_blocks_before_history_query(self):
        with tempfile.TemporaryDirectory() as directory:
            original_data_dir = engine_module.DATA_DIR
            previous_cwd = os.getcwd()
            engine_module.DATA_DIR = Path(directory) / "data"
            os.chdir(directory)
            try:
                symbol = "AAPL"
                client = _Client([{"ord_no": "PENDING", "cntr_qty": "2", "cntr_pric": "201.25"}])
                client.market = "US"
                logger = _Logger()
                config = _config()
                config.update(symbol=symbol, market="US")
                ctx = AccountContext(
                    account_id="us-unconfirmed-date", display_name="US identity gate", client=client,
                    strategy=InfiniteGridStrategy(config), risk_manager=None, dedup=None,
                    logger=logger, position=PositionState(symbol=symbol),
                )
                engine = AccountEngine(
                    ctx, make_telegram_double(), None, lambda _symbol: None,
                    poll_interval_sec=60, control_symbol=symbol,
                )
                try:
                    engine.execution_query_min_interval_sec = 0.0
                    engine.ledger.add_pending(PendingOrder(
                        "PENDING", symbol, "BUY", 10, 200, "BUY", 2, {},
                    ))
                    engine._last_balance_reconciliation = float("inf")
                    engine.balance_reconcile_sec = 60
                    before = "\\n".join(engine.ledger.db.iterdump())
                    self.assertFalse(asyncio.run(engine.sync_broker_state()))
                    self.assertEqual("\\n".join(engine.ledger.db.iterdump()), before)
                    self.assertEqual(client.execution_history_calls, 0)
                    self.assertTrue(engine._balance_sync_blocked)
                    self.assertEqual(ctx.position.qty, 0)
                    self.assertTrue(any("broker order date identity is unconfirmed" in item for item in logger.errors))
                finally:
                    engine.ledger.close()
            finally:
                engine_module.DATA_DIR = original_data_dir
                os.chdir(previous_cwd)

    def test_us_nonfinite_rows_preserve_order_and_position(self):
        cases = [
            ("NaN", "201.25", True, "non_finite_cumulative_quantity", None, 201.25),
            ("2", "NaN", False, "authoritative_execution_date_missing", 2.0, None),
            ("Infinity", "201.25", False, "invalid_execution_date", None, None),
            ("2", "Infinity", False, "authoritative_execution_date_missing", 2.0, 0.0),
        ]
        for qty, price, success, reason, total, parsed_price in cases:
            with self.subTest(qty=qty, price=price), tempfile.TemporaryDirectory() as directory:
                asyncio.run(self._assert_us_nonfinite_case(
                    Path(directory), qty, price, success, reason, total, parsed_price,
                ))

    def test_excess_fills_stop_sync_without_applying_or_completing_reconciliation(self):
        for market in ("KR", "US"):
            for side in ("BUY", "SELL"):
                for corrupt_stored in (False, True):
                    with self.subTest(market=market, side=side, corrupt_stored=corrupt_stored):
                        with tempfile.TemporaryDirectory() as directory:
                            asyncio.run(self._assert_excess_fill(
                                Path(directory), market, side, corrupt_stored,
                            ))

    def test_conflict_write_failure_stops_sync_without_applying_fill(self):
        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(self._assert_excess_fill(Path(directory), "KR", "BUY", False, fail_write=True))

    def test_invalid_price_cannot_bypass_quantity_conflict(self):
        for market in ("KR", "US"):
            for side in ("BUY", "SELL"):
                for completed in (False, True):
                    for corrupt_stored in (False, True):
                        for price in ("0", "NaN", "Infinity", "-Infinity", "bad", ""):
                            with self.subTest(market=market, side=side, completed=completed,
                                              corrupt_stored=corrupt_stored, price=price):
                                with tempfile.TemporaryDirectory() as directory:
                                    asyncio.run(self._assert_excess_fill(
                                        Path(directory), market, side, corrupt_stored,
                                        completed=completed, execution_price=price,
                                    ))

    def test_invalid_price_conflict_write_failure_stops_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(self._assert_excess_fill(
                Path(directory), "KR", "BUY", False, fail_write=True, execution_price="NaN",
            ))

    def test_completed_order_late_excess_is_latched_after_reopen(self):
        for market in ("KR", "US"):
            for side in ("BUY", "SELL"):
                with self.subTest(market=market, side=side), tempfile.TemporaryDirectory() as directory:
                    asyncio.run(self._assert_excess_fill(
                        Path(directory), market, side, False, completed=True,
                    ))

    def test_completed_duplicate_or_stale_history_never_applies_fill(self):
        for observed in (2, 5):
            with self.subTest(observed=observed), tempfile.TemporaryDirectory() as directory:
                asyncio.run(self._assert_excess_fill(
                    Path(directory), "KR", "BUY", False, completed=True, benign_observed=observed,
                ))

    def test_incomplete_execution_pages_stop_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(self._assert_excess_fill(
                Path(directory), "KR", "BUY", False, incomplete_history=True,
            ))

    async def _assert_excess_fill(self, directory, market, side, corrupt_stored, *,
                                  fail_write=False, completed=False, benign_observed=None,
                                  incomplete_history=False, execution_price="101"):
        original_data_dir = engine_module.DATA_DIR
        previous_cwd = os.getcwd()
        engine_module.DATA_DIR = directory / "data"
        os.chdir(directory)
        symbol = "AAPL" if market == "US" else "000490"
        observed = 2 if corrupt_stored else 6
        if benign_observed is not None:
            observed = benign_observed
        raw = {"ord_no": "EXCESS", "cntr_qty": str(observed), "cntr_pric": execution_price}
        client = _Client([raw])
        client.market = market
        logger = _Logger()
        config = _config()
        config.update(symbol=symbol, market=market)
        try:
            ctx = AccountContext(
                account_id="excess-fill", display_name="excess fill", client=client,
                strategy=InfiniteGridStrategy(config), risk_manager=None, dedup=None,
                logger=logger, position=PositionState(symbol=symbol),
            )
            engine = AccountEngine(
                ctx, make_telegram_double(), None, lambda _symbol: None,
                poll_interval_sec=60, control_symbol=symbol,
            )
            try:
                # All responses are local fixtures; do not wait on the shared
                # broker quota interval between parameter combinations.
                engine.execution_query_min_interval_sec = 0.0
                order = PendingOrder("EXCESS", symbol, side, 5, 100, side, 2, {})
                engine.ledger.add_pending(order)
                if market == "US":
                    source = engine.data_dir / "trades_excess-fill.db"
                    candidate = engine.data_dir / "identity_candidate.db"
                    engine.ledger.close()
                    create_identity_ledger_copy(source, candidate, account_markets={"excess-fill": "US"})
                    engine.ledger = TradeLedgerStore(str(candidate), "excess-fill", market="US")
                    order = engine.ledger.pending_orders(symbol)[0]
                    engine.ledger.confirm_us_order_date(order.order_uid, "20261001", evidence={
                        "source_tr": "synthetic", "record_ref": "fixture", "verified_at_utc": "2026-10-03T00:00:00+00:00",
                    })
                    order = engine.ledger.get_pending(order.ord_no, order_uid=order.order_uid)
                    raw["cntr_dt"] = "20261001"
                ledger_path = engine.ledger.db.execute("PRAGMA database_list").fetchone()[2]
                identity_args = {"order_uid": order.order_uid} if order.order_uid else {}
                engine.ledger.mark_awaiting_execution_history(order.ord_no, **identity_args)
                if completed:
                    engine.ledger.record_fill(order, 5, 100, "2026-10-01", **({"execution_date": "20261001"} if market == "US" else {}))
                    engine.ledger.close()
                    engine.ledger = TradeLedgerStore(
                        ledger_path, "excess-fill", market=market,
                    )
                    self.assertEqual(engine.ledger.pending_orders(symbol), [])
                    self.assertEqual(engine.ledger.execution_recovery_orders(symbol), [])
                if corrupt_stored:
                    engine.ledger.db.execute(
                        "UPDATE pending_orders SET filled_qty=6 WHERE ord_no=?", (order.ord_no,),
                    )
                    engine.ledger.db.commit()
                if fail_write:
                    engine.ledger.db.execute("""CREATE TRIGGER reject_quantity_conflict
                        BEFORE INSERT ON execution_quantity_conflicts
                        BEGIN SELECT RAISE(ABORT, 'diagnostic write blocked'); END""")
                    engine.ledger.db.commit()
                before = "\n".join(engine.ledger.db.iterdump())
                position_before = dict(vars(ctx.position))
                pending_before = engine.ledger.get_pending(order.ord_no, **identity_args)
                trades_before = engine.ledger.ledger_rows(symbol)
                engine._apply_confirmed_fill = AsyncMock()
                engine._cancel_stale_orders = AsyncMock()
                engine._run_balance_reconciliation_cycle = AsyncMock(return_value=True)
                strategy_fill = unittest.mock.Mock()
                ctx.strategy.on_filled = strategy_fill
                if incomplete_history:
                    client.get_executed_orders = AsyncMock(side_effect=ValueError("incomplete pages"))
                if benign_observed is not None:
                    engine.ledger.set_lifecycle_started_at("2099-01-01")
                    self.assertTrue(await engine.sync_broker_state(force_balance=True))
                    self.assertEqual("\n".join(engine.ledger.db.iterdump()), before)
                    self.assertEqual(vars(ctx.position), position_before)
                    engine._apply_confirmed_fill.assert_not_awaited()
                    strategy_fill.assert_not_called()
                    return
                self.assertFalse(await engine.sync_broker_state(force_balance=True))
                if fail_write or incomplete_history:
                    self.assertEqual("\n".join(engine.ledger.db.iterdump()), before)
                    self.assertFalse(engine.ledger.db.in_transaction)
                    self.assertTrue(engine._balance_sync_blocked)
                    engine._apply_confirmed_fill.assert_not_awaited()
                    engine._cancel_stale_orders.assert_not_awaited()
                    engine._run_balance_reconciliation_cycle.assert_not_awaited()
                    strategy_fill.assert_not_called()
                    return
                self.assertNotEqual("\n".join(engine.ledger.db.iterdump()), before)
                self.assertEqual(engine.ledger.get_pending(order.ord_no, **identity_args), pending_before)
                self.assertEqual(engine.ledger.ledger_rows(symbol), trades_before)
                self.assertEqual(engine.ledger.quantity_conflict_order_ids(symbol), (order.order_uid or order.ord_no,))
                self.assertEqual(vars(ctx.position), position_before)
                self.assertFalse(engine.ledger.db.in_transaction)
                self.assertTrue(engine._balance_sync_blocked)
                engine._apply_confirmed_fill.assert_not_awaited()
                engine._cancel_stale_orders.assert_not_awaited()
                engine._run_balance_reconciliation_cycle.assert_not_awaited()
                strategy_fill.assert_not_called()
                event = json.loads(logger.warnings[-1])
                self.assertEqual(event["reason"], "stored_fill_exceeds_requested_quantity"
                                 if corrupt_stored else "cumulative_fill_exceeds_requested_quantity")
                self.assertEqual(event["requestedQty"], 5)
                self.assertEqual(event["storedFilledQty"], 6 if corrupt_stored else 5 if completed else 0)
                self.assertEqual(event["total"], observed)
                # A later normal response and a fresh ledger connection do not
                # resolve the persisted conflict or permit generic pause clear.
                engine.ledger.close()
                engine.ledger = TradeLedgerStore(
                    ledger_path, "excess-fill", market=market,
                )
                client.rows = [{"ord_no": order.ord_no, "cntr_qty": "2", "cntr_pric": "101"}]
                self.assertFalse(await engine.sync_broker_state(force_balance=True))
                engine._trading_paused = True
                engine._pause_reason = "execution_quantity_conflict"
                engine.resume_trading()
                self.assertTrue(engine._trading_paused)
                self.assertEqual(engine._pause_reason, "execution_quantity_conflict")
                self.assertEqual(engine.ledger.get_pending(order.ord_no, **identity_args), pending_before)
                engine._apply_confirmed_fill.assert_not_awaited()
                engine._run_balance_reconciliation_cycle.assert_not_awaited()
            finally:
                engine.ledger.close()
        finally:
            engine_module.DATA_DIR = original_data_dir
            os.chdir(previous_cwd)

    async def _assert_us_nonfinite_case(
        self, directory, qty, price, success, reason, total, parsed_price,
    ):
        original_data_dir = engine_module.DATA_DIR
        previous_cwd = os.getcwd()
        engine_module.DATA_DIR = directory / "data"
        os.chdir(directory)
        symbol, number, order_date = "AAPL", "000000252", "20261001"
        calls = []
        client = _Client([])
        client.market = "US"

        async def history(requested_symbol, *, order_date):
            calls.append((requested_symbol, order_date))
            return {"result_list": [{
                "ord_no": number, "ord_dt": order_date, "cntr_qty": qty,
                "cntr_uv": price, "cntr_dt": "20261003",
            }], "_execution_pages_complete": True, "_query_order_date": order_date}

        client.get_executed_orders = history
        logger = _Logger()
        config = _config()
        config.update(symbol=symbol, market="US")
        try:
            ctx = AccountContext(
                account_id="synthetic-us-nonfinite", display_name="US nonfinite regression",
                client=client, strategy=InfiniteGridStrategy(config), risk_manager=None,
                dedup=None, logger=logger, position=PositionState(symbol=symbol),
            )
            engine = AccountEngine(
                ctx, make_telegram_double(), None, lambda _symbol: None,
                poll_interval_sec=60, control_symbol=symbol,
            )
            try:
                source = engine.data_dir / "trades_synthetic-us-nonfinite.db"
                candidate = engine.data_dir / "identity_candidate.db"
                engine.ledger.close()
                create_identity_ledger_copy(
                    source, candidate, account_markets={ctx.account_id: "US"},
                )
                engine.ledger = TradeLedgerStore(candidate, ctx.account_id, market="US")
                order = PendingOrder(number, symbol, "BUY", 10, 200, "BUY", 2, {})
                engine.ledger.add_pending(order)
                engine.ledger.confirm_us_order_date(order.order_uid, order_date, evidence={
                    "source_tr": "synthetic", "record_ref": "nonfinite-fixture",
                    "verified_at_utc": "2026-10-03T00:00:00+00:00",
                })
                engine.ledger.mark_awaiting_execution_history(number, order_uid=order.order_uid)
                pending_before = engine.ledger.get_pending(number, order_uid=order.order_uid)
                self.assertEqual(pending_before.identity_status, "confirmed")
                engine.execution_query_min_interval_sec = 0.0
                engine._apply_confirmed_fill = AsyncMock()
                engine._cancel_stale_orders = AsyncMock()
                engine._run_balance_reconciliation_cycle = AsyncMock(return_value=True)
                before = "\n".join(engine.ledger.db.iterdump())
                position_before = dict(vars(ctx.position))
                strategy_before = (dict(ctx.strategy.step_qty), dict(ctx.strategy.step_prices))

                self.assertEqual(await engine.sync_broker_state(force_balance=True), success)

                self.assertEqual(calls, [(symbol, order_date)])
                self.assertEqual("\n".join(engine.ledger.db.iterdump()), before)
                self.assertEqual(engine.ledger.get_pending(number, order_uid=order.order_uid), pending_before)
                self.assertEqual(engine.ledger.ledger_rows(symbol), [])
                self.assertEqual(vars(ctx.position), position_before)
                self.assertEqual((ctx.strategy.step_qty, ctx.strategy.step_prices), strategy_before)
                self.assertFalse(engine.ledger.db.in_transaction)
                self.assertEqual(engine.ledger.db.execute("PRAGMA foreign_key_check").fetchall(), [])
                engine._apply_confirmed_fill.assert_not_awaited()
                if not success:
                    self.assertTrue(engine._balance_sync_blocked)
                    engine._cancel_stale_orders.assert_not_awaited()
                    engine._run_balance_reconciliation_cycle.assert_not_awaited()
                if reason == "invalid_execution_date":
                    self.assertTrue(any("Broker order date must be YYYYMMDD" in item for item in logger.errors))
                else:
                    events = [json.loads(item) for item in logger.warnings]
                    self.assertEqual(len(events), 1)
                    event = events[0]
                    self.assertEqual(event["event"], "execution_row_skipped")
                    self.assertEqual(event["reason"], reason)
                    self.assertEqual(event["total"], total)
                    self.assertEqual(event["price"], parsed_price)
                    self.assertEqual(event["raw"]["execution_date"], "")
            finally:
                engine.ledger.close()
        finally:
            engine_module.DATA_DIR = original_data_dir
            os.chdir(previous_cwd)

    async def _assert_case(
        self,
        directory: Path,
        reason: str,
        raw: dict,
        pending: PendingOrder,
        expected_total: float | None,
        expected_price: float | None,
        *,
        market: str = "KR",
        expected_raw: dict | None = None,
    ) -> None:
        original_data_dir = engine_module.DATA_DIR
        previous_cwd = os.getcwd()
        os.chdir(directory)
        engine_module.DATA_DIR = directory / "data"
        logger = _Logger()
        config = _config()
        config.update(symbol=pending.symbol, market=market)
        client = _Client([raw])
        client.market = market
        try:
            ctx = AccountContext(
                account_id=f"skip_{reason}",
                display_name="skip logging",
                client=client,
                strategy=InfiniteGridStrategy(config),
                risk_manager=None,
                dedup=None,
                logger=logger,
                position=PositionState(symbol=pending.symbol),
            )
            engine = AccountEngine(
                ctx, make_telegram_double(), None, lambda _symbol: None,
                poll_interval_sec=60, control_symbol=pending.symbol,
            )
            try:
                engine.ledger.add_pending(pending)
                engine._last_balance_reconciliation = asyncio.get_running_loop().time()
                engine.balance_reconcile_sec = 60
                before = "\n".join(engine.ledger.db.iterdump())

                self.assertTrue(await engine.sync_broker_state())

                after = "\n".join(engine.ledger.db.iterdump())
                self.assertEqual(before, after)
                self.assertEqual(ctx.position.qty, 0)
                self.assertEqual(len(logger.warnings), 1)
                event = json.loads(logger.warnings[0])
                self.assertEqual(event["event"], "execution_row_skipped")
                self.assertEqual(event["reason"], reason)
                self.assertEqual(event["orderNo"], raw["ord_no"])
                self.assertEqual(event["total"], expected_total)
                self.assertEqual(event["price"], expected_price)
                self.assertEqual(event["raw"], raw if expected_raw is None else expected_raw)
            finally:
                engine.ledger.close()
        finally:
            engine_module.DATA_DIR = original_data_dir
            os.chdir(previous_cwd)
