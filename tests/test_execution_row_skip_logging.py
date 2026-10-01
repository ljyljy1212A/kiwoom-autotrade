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

    async def get_executed_orders(self, _symbol: str) -> dict:
        return {"cntr": self.rows}


class _Logger:
    def __init__(self):
        self.warnings: list[str] = []

    def info(self, *_args):
        pass

    def warning(self, message: str, *_args):
        self.warnings.append(message)

    def error(self, *_args):
        pass

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

    def test_us_nonfinite_rows_preserve_order_and_position(self):
        cases = [
            ("NaN", "201.25", "non_finite_cumulative_quantity", None, 201.25,
             "nan", 201.25),
            ("2", "NaN", "non_finite_execution_price", 2.0, None, 2.0, "nan"),
            ("Infinity", "201.25", "non_incremental_cumulative_quantity", 0.0, 201.25,
             0.0, 201.25),
            ("2", "Infinity", "non_positive_execution_price", 2.0, 0.0, 2.0, 0.0),
        ]
        for qty, price, reason, total, parsed_price, logged_qty, logged_price in cases:
            with self.subTest(qty=qty, price=price), tempfile.TemporaryDirectory() as directory:
                asyncio.run(self._assert_case(
                    Path(directory), reason,
                    {"ord_no": "PENDING", "cntr_qty": qty, "cntr_pric": price},
                    PendingOrder("PENDING", "AAPL", "BUY", 10, 200, "BUY", 2, {}),
                    total, parsed_price, market="US",
                    expected_raw={"ord_no": "PENDING", "cntr_qty": logged_qty,
                                  "cntr_pric": logged_price, "ord_dt": ""},
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
                engine.ledger.mark_awaiting_execution_history(order.ord_no)
                if completed:
                    engine.ledger.record_fill(order, 5, 100, "2026-10-01")
                    engine.ledger.close()
                    engine.ledger = TradeLedgerStore(
                        str(engine.data_dir / "trades_excess-fill.db"), "excess-fill",
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
                pending_before = engine.ledger.get_pending(order.ord_no)
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
                self.assertEqual(engine.ledger.get_pending(order.ord_no), pending_before)
                self.assertEqual(engine.ledger.ledger_rows(symbol), trades_before)
                self.assertEqual(engine.ledger.quantity_conflict_order_ids(symbol), (order.ord_no,))
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
                    str(engine.data_dir / "trades_excess-fill.db"), "excess-fill",
                )
                client.rows = [{"ord_no": order.ord_no, "cntr_qty": "2", "cntr_pric": "101"}]
                self.assertFalse(await engine.sync_broker_state(force_balance=True))
                engine._trading_paused = True
                engine._pause_reason = "execution_quantity_conflict"
                engine.resume_trading()
                self.assertTrue(engine._trading_paused)
                self.assertEqual(engine._pause_reason, "execution_quantity_conflict")
                self.assertEqual(engine.ledger.get_pending(order.ord_no), pending_before)
                engine._apply_confirmed_fill.assert_not_awaited()
                engine._run_balance_reconciliation_cycle.assert_not_awaited()
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
