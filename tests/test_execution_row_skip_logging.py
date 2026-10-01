"""Regression coverage for skipped broker execution rows."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

import src.core.engine as engine_module
from src.core.account_manager import AccountContext
from src.core.engine import AccountEngine
from src.data.trade_ledger import PendingOrder
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
