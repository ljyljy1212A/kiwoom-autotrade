from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.data.trade_ledger import PendingOrder, TradeLedgerStore


class TradingPauseIncidentTest(unittest.TestCase):
    def test_filled_zero_quantity_remains_recoverable_and_unresolved(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TradeLedgerStore(str(Path(directory) / "ledger.sqlite"), "test-account")
            try:
                order = PendingOrder("0149421", "483350", "BUY", 1, 100, "BUY", 2, {})
                store.add_pending(order)
                store.db.execute(
                    "UPDATE pending_orders SET status='filled', filled_qty=0 "
                    "WHERE account_id=? AND ord_no=?",
                    ("test-account", order.ord_no),
                )
                store.db.commit()

                self.assertTrue(store.has_unresolved_orders("483350"))
                self.assertEqual([item.ord_no for item in store.execution_recovery_orders("483350")], [order.ord_no])
                self.assertEqual([item.ord_no for item in store.pending_orders("483350")], [order.ord_no])
            finally:
                store.close()

    def test_record_fill_persists_ledger_and_terminal_state_together(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TradeLedgerStore(str(Path(directory) / "ledger.sqlite"), "test-account")
            try:
                order = PendingOrder("ORDER-1", "033320", "BUY", 1, 100, "BUY", 2, {})
                store.add_pending(order)
                row = store.record_fill(order, 1, 101, "2026-08-20T00:00:00+00:00")

                self.assertIsNotNone(row)
                self.assertEqual(store.get_pending(order.ord_no).status, "filled")
                self.assertEqual(store.get_pending(order.ord_no).filled_qty, 1)
                self.assertEqual(len(store.ledger_rows("033320")), 1)
                self.assertFalse(store.has_unresolved_orders("033320"))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
