"""Dashboard ledger reads must include committed fills in SQLite WAL."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dashboard.dashboard_server import (
    _open_lifecycle_starts, _overlay_authoritative_tranche_metadata,
    _trade_history_cache, _trade_history_payload,
)
from src.data.trade_ledger import PendingOrder, TradeLedgerStore


class DashboardTradeHistoryTest(unittest.TestCase):
    def test_balance_metadata_prefers_open_lifecycle_manual_basis(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "tranche_bases_kr_mock.json").write_text(
                json.dumps({"125490": 13340}), encoding="utf-8"
            )
            (data_dir / "symbol_lifecycles_kr_mock.json").write_text(json.dumps({
                "125490": {"status": "open", "manual_qty": 1, "manual_price": 13340},
            }), encoding="utf-8")
            result = _overlay_authoritative_tranche_metadata(
                "kr_mock", {"trancheBases": {"125490": 13201}}, data_dir
            )
            self.assertEqual(result["trancheBases"]["125490"], 13340)
            self.assertEqual(result["manualTrancheBases"]["125490"], 13340)

    def test_open_lifecycle_boundaries_exclude_prior_cycle_from_tranches(self):
        with tempfile.TemporaryDirectory() as directory:
            lifecycle_path = Path(directory) / "lifecycles.json"
            lifecycle_path.write_text(json.dumps({
                "142210": {"status": "open", "started_at": "2026-08-14T03:32:29+00:00"},
                "001210": {"status": "closed", "started_at": "2026-08-14T03:10:00+00:00"},
            }), encoding="utf-8")
            self.assertEqual(_open_lifecycle_starts(lifecycle_path), {
                "142210": "2026-08-14T03:32:29+00:00",
            })

    def test_read_only_dashboard_sees_uncheckpointed_wal_fill(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trades_kr_dashboard_wal_test.db"
            ledger = TradeLedgerStore(str(path), "kr_dashboard_wal_test")
            try:
                order = PendingOrder("T2-WAL", "001210", "BUY", 14, 7020, "BUY", 2, {})
                ledger.add_pending(order)
                ledger.record_fill(order, 14, 7020, "2026-08-14")
                self.assertTrue(Path(f"{path}-wal").exists())
                _trade_history_cache.pop("kr_dashboard_wal_test", None)

                payload = _trade_history_payload(
                    "kr_dashboard_wal_test", path, {"001210": "2000-01-01T00:00:00+00:00"},
                )

                self.assertEqual([(row["symbol"], row["step"], row["qty"])
                                  for row in payload["tranches"]], [("001210", 2, 14.0)])
            finally:
                ledger.close()

    def test_same_symbol_reentry_uses_only_the_new_lifecycle_tranches(self):
        """A prior manually closed cycle cannot create an unassigned-share warning."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trades_kr_same_symbol_cycle_test.db"
            account = "kr_same_symbol_cycle_test"
            ledger = TradeLedgerStore(str(path), account)
            try:
                old = PendingOrder("OLD-T2", "142210", "BUY", 14, 7080, "BUY", 2, {})
                current_buy = PendingOrder("CURRENT-T2", "142210", "BUY", 13, 7230, "BUY", 2, {})
                ledger.add_pending(old)
                ledger.record_fill(old, 14, 7080, "2026-08-14")
                ledger.add_pending(current_buy)
                ledger.record_fill(current_buy, 13, 7230, "2026-08-14")
                # The earlier automated position was fully sold manually;
                # its retained historical BUY must not be part of the new
                # manual-T1 lifecycle's open T2 summary.
                ledger.db.execute(
                    "UPDATE trade_ledger SET created_at=? WHERE ord_no='OLD-T2'",
                    ("2026-08-14T03:30:00+00:00",),
                )
                ledger.db.execute(
                    "UPDATE trade_ledger SET created_at=? WHERE ord_no='CURRENT-T2'",
                    ("2026-08-14T03:33:00+00:00",),
                )
                ledger.db.commit()
                _trade_history_cache.pop(account, None)

                payload = _trade_history_payload(
                    account, path, {"142210": "2026-08-14T03:32:29+00:00"},
                )

                self.assertEqual(payload["tranches"], [{
                    "symbol": "142210", "step": 2, "qty": 13.0, "avgPrice": 7230.0,
                }])
                self.assertEqual(len(payload["trades"]), 2)  # history/P&L remains intact
            finally:
                ledger.close()


if __name__ == "__main__":
    unittest.main()
