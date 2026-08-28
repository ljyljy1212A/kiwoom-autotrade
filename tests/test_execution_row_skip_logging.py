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


class _Notifier:
    async def notify_order(self, *_args):
        return None

    async def notify_fill(self, *_args):
        return None

    async def notify_error(self, *_args):
        return None

    async def notify_balance_change(self, *_args):
        return None

    async def notify_symbol_closed(self, *_args):
        return None

    async def notify_symbol_reopened(self, *_args):
        return None


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
        for reason, raw, pending, expected_total, expected_price in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                asyncio.run(self._assert_case(
                    Path(directory), reason, raw, pending, expected_total, expected_price
                ))

    async def _assert_case(
        self,
        directory: Path,
        reason: str,
        raw: dict,
        pending: PendingOrder,
        expected_total: float | None,
        expected_price: float | None,
    ) -> None:
        original_data_dir = engine_module.DATA_DIR
        previous_cwd = os.getcwd()
        os.chdir(directory)
        engine_module.DATA_DIR = directory / "data"
        logger = _Logger()
        try:
            ctx = AccountContext(
                account_id=f"skip_{reason}",
                display_name="skip logging",
                client=_Client([raw]),
                strategy=InfiniteGridStrategy(_config()),
                risk_manager=None,
                dedup=None,
                logger=logger,
                position=PositionState(symbol="000490"),
            )
            engine = AccountEngine(
                ctx, _Notifier(), None, lambda _symbol: None,
                poll_interval_sec=60, control_symbol="000490",
            )
            try:
                engine.ledger.add_pending(pending)
                engine._last_balance_reconciliation = asyncio.get_running_loop().time()
                engine.balance_reconcile_sec = 60
                before = "\n".join(engine.ledger.db.iterdump())

                self.assertTrue(await engine.sync_broker_state())

                after = "\n".join(engine.ledger.db.iterdump())
                self.assertEqual(before, after)
                self.assertEqual(len(logger.warnings), 1)
                event = json.loads(logger.warnings[0])
                self.assertEqual(event["event"], "execution_row_skipped")
                self.assertEqual(event["reason"], reason)
                self.assertEqual(event["orderNo"], raw["ord_no"])
                self.assertEqual(event["total"], expected_total)
                self.assertEqual(event["price"], expected_price)
                self.assertEqual(event["raw"], raw)
            finally:
                engine.ledger.close()
        finally:
            engine_module.DATA_DIR = original_data_dir
            os.chdir(previous_cwd)
