"""Regression tests for broker-authoritative unattended orphan cleanup."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.core.orphan_cleanup import OrphanStateCleaner
from src.data.trade_ledger import PendingOrder, TradeLedgerStore


class OrphanCleanupTest(unittest.TestCase):
    account = "us_mock_test"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()
        self.cleaner = OrphanStateCleaner(self.account, self.data, market="US")

    def tearDown(self):
        self.tmp.cleanup()

    def _state(self, symbol="LEGACY"):
        (self.data / f"tranche_bases_{self.account}.json").write_text(json.dumps({symbol: 10.0}), encoding="utf-8")
        (self.data / f"symbol_lifecycles_{self.account}.json").write_text(
            json.dumps({symbol: {"status": "open", "started_at": "2026-08-01T00:00:00+00:00"}}), encoding="utf-8")
        (self.data / f"dashboard_control_{self.account}_{symbol}.json").write_text(
            json.dumps({"symbol": symbol, "auto_buy": True}), encoding="utf-8")
        (self.data / f"dashboard_settings_{self.account}.json").write_text(
            json.dumps({"profiles": [{"config": {"symbol": symbol}}]}), encoding="utf-8")

    def test_two_complete_zero_snapshots_clean_runtime_state_but_keep_ledger_history(self):
        self._state()
        ledger = TradeLedgerStore(str(self.data / f"trades_{self.account}.db"), self.account)
        try:
            order = PendingOrder("B1", "LEGACY", "BUY", 2, 10, "BUY", 2, {})
            ledger.add_pending(order)
            ledger.record_fill(order, 2, 10, "2026-08-01")
            first = self.cleaner.sweep({}, True, ledger.has_unresolved_orders)
            self.assertEqual(first[0]["classification"], "orphan_candidate")
            second = self.cleaner.sweep({}, True, ledger.has_unresolved_orders)
            self.assertEqual(second[0]["classification"], "cleaned")
            self.assertFalse((self.data / f"tranche_bases_{self.account}.json").read_text(encoding="utf-8").find("LEGACY") >= 0)
            self.assertEqual(ledger.ledger_rows("LEGACY")[0]["ord_no"], "B1")
        finally:
            ledger.close()

    def test_transient_zero_then_nonzero_never_cleans(self):
        self._state()
        self.cleaner.sweep({}, True, lambda _: False)
        result = self.cleaner.sweep({"LEGACY": 3}, True, lambda _: False)
        self.assertEqual(result[0]["classification"], "protected_nonzero_holding")
        self.assertIn("LEGACY", json.loads((self.data / f"tranche_bases_{self.account}.json").read_text()))

    def test_incomplete_balance_blocks_cleanup(self):
        self._state()
        result = self.cleaner.sweep({}, False, lambda _: False)
        self.assertEqual(result[0]["classification"], "blocked_incomplete_balance")
        self.assertTrue((self.data / f"dashboard_control_{self.account}_LEGACY.json").exists())

    def test_unresolved_order_and_nonzero_iren_style_state_require_review(self):
        self._state("IREN")
        result = self.cleaner.sweep({"IREN": 3}, True, lambda symbol: symbol == "IREN")
        self.assertEqual(result[0]["classification"], "manual_review_required")
        self.assertTrue((self.data / f"dashboard_control_{self.account}_IREN.json").exists())

    def test_closed_lifecycle_history_is_not_reactivated_by_cleanup(self):
        self._state("HISTORY")
        self.cleaner.sweep({}, True, lambda _: False)
        self.cleaner.sweep({}, True, lambda _: False)
        lifecycle = json.loads((self.data / f"symbol_lifecycles_{self.account}.json").read_text())
        self.assertEqual(lifecycle["HISTORY"]["status"], "closed")
        self.assertEqual(lifecycle["HISTORY"]["reason"], "automatic_orphan_cleanup")

    def test_manual_lifecycle_basis_is_not_an_orphan_when_broker_nonzero(self):
        self._state("IREN")
        lifecycles = json.loads((self.data / f"symbol_lifecycles_{self.account}.json").read_text())
        lifecycles["IREN"].update({"manual_qty": 1.0, "manual_price": 45.085})
        (self.data / f"symbol_lifecycles_{self.account}.json").write_text(json.dumps(lifecycles), encoding="utf-8")
        result = self.cleaner.evaluate("IREN", 3, True, lambda _: True)
        self.assertEqual(result["classification"], "manual_review_required")
        self.assertEqual(json.loads((self.data / f"symbol_lifecycles_{self.account}.json").read_text())["IREN"]["manual_price"], 45.085)

    def test_unambiguous_aapl_legacy_key_migrates_and_survives_restart(self):
        self._state("PL")
        ledger = TradeLedgerStore(str(self.data / f"trades_{self.account}.db"), self.account)
        try:
            order = PendingOrder("AAPL-1", "AAPL", "BUY", 1, 10, "BUY", 1, {})
            ledger.add_pending(order)
            self.assertEqual(self.cleaner.migrate_legacy_keys({"AAPL"}), frozenset())
            lifecycle = json.loads((self.data / f"symbol_lifecycles_{self.account}.json").read_text())
            self.assertIn("AAPL", lifecycle)
            self.assertNotIn("PL", lifecycle)
            self.assertTrue((self.data / f"dashboard_control_{self.account}_AAPL.json").exists())
            self.assertFalse((self.data / f"dashboard_control_{self.account}_PL.json").exists())
            self.assertEqual(ledger.pending_orders("AAPL")[0].ord_no, "AAPL-1")
            restarted = OrphanStateCleaner(self.account, self.data, market="US")
            self.assertEqual(restarted.migrate_legacy_keys({"AAPL"}), frozenset())
            self.assertEqual(json.loads((self.data / f"symbol_lifecycles_{self.account}.json").read_text()), lifecycle)
            self.assertTrue((self.data / "audit" / f"symbol_key_migration_{self.account}.jsonl").exists())
        finally:
            ledger.close()

    def test_aapl_pl_collision_remains_manual_review_and_orphan_cleanup_skips_it(self):
        self._state("PL")
        manual_review = self.cleaner.migrate_legacy_keys({"AAPL", "PL"})
        self.assertEqual(manual_review, frozenset({"AAPL", "PL"}))
        lifecycle = json.loads((self.data / f"symbol_lifecycles_{self.account}.json").read_text())
        self.assertIn("PL", lifecycle)
        result = self.cleaner.sweep({}, True, lambda _: False)
        self.assertEqual(result[0]["classification"], "manual_review_symbol_key")
        self.assertEqual(json.loads((self.data / f"symbol_lifecycles_{self.account}.json").read_text()), lifecycle)


if __name__ == "__main__":
    unittest.main()
