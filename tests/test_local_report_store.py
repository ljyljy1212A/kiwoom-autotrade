import csv
import sqlite3

from src.data.local_report_store import LocalReportStore
from tools.migrate_google_sheet import normalize_rows


def test_local_snapshots_are_durable_and_exportable(tmp_path):
    store = LocalReportStore("kr_mock", tmp_path)
    store.append_snapshot(
        close=5650, avg_price=5650, star_price=5700, qty=1, qty_change=1,
        realized_pnl=0, cum_pnl=0, cum_invest=5650, cur_invest=5650, potential_pnl_pct=0,
        recorded_at="2026-08-12 12:00:00",
    )
    exported = store.export_csv(tmp_path / "reports.csv")
    with exported.open(encoding="utf-8-sig", newline="") as source:
        rows = list(csv.reader(source))
    assert rows[1][0] == "2026-08-12 12:00:00"
    assert rows[1][4] == "1.0"


def test_google_column_order_import_preserves_tranche_report_values(tmp_path):
    raw = [["2026-08-12 12:00:00", "5650", "5650", "5700", "1", "1", "0", "0", "5650", "5650", "0"]]
    store = LocalReportStore("kr_mock", tmp_path)
    assert store.import_rows(normalize_rows(raw)) == 1
    with sqlite3.connect(store.path) as db:
        row = db.execute("SELECT qty, avg_price, star_price FROM report_snapshots").fetchone()
    assert row == (1.0, 5650.0, 5700.0)
