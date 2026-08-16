import unittest
import os
import json
import tempfile
from pathlib import Path

import src.main as main_module
from src.data.trade_ledger import TradeLedgerStore, PendingOrder


class RecoverySymbolConfigTests(unittest.TestCase):
    def test_disabled_profile_with_unresolved_order_starts_recovery_engine_only(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            data_dir.mkdir()

            original_data_dir = main_module.DATA_DIR
            main_module.DATA_DIR = data_dir
            try:
                config = {
                    "symbol": "014280", "market": "KR",
                    "auto_buy": {"enabled": False}, "auto_sell": {"enabled": False},
                }
                (data_dir / "dashboard_settings_kr_mock.json").write_text(
                    json.dumps({"profiles": [{"enabled": True, "config": config}]}), encoding="utf-8"
                )
                ledger = TradeLedgerStore(str(data_dir / "trades_kr_mock.db"), "kr_mock")
                ledger.add_pending(PendingOrder("sell-1", "014280", "SELL", 10, 100, "SELL", 2, {}))
                ledger.close()

                self.assertEqual(main_module._enabled_symbol_configs("kr_mock", "KR"), [config])
            finally:
                main_module.DATA_DIR = original_data_dir

    def test_disabled_profile_without_unresolved_order_is_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            data_dir.mkdir()

            original_data_dir = main_module.DATA_DIR
            main_module.DATA_DIR = data_dir
            try:
                config = {
                    "symbol": "005930", "market": "KR",
                    "auto_buy": {"enabled": False}, "auto_sell": {"enabled": False},
                }
                (data_dir / "dashboard_settings_kr_mock.json").write_text(
                    json.dumps({"profiles": [{"enabled": False, "config": config}]}), encoding="utf-8"
                )
                # No pending_orders DB is created at all — simulates a
                # disabled profile with nothing left to reconcile.
                self.assertEqual(main_module._enabled_symbol_configs("kr_mock", "KR"), [])
            finally:
                main_module.DATA_DIR = original_data_dir             