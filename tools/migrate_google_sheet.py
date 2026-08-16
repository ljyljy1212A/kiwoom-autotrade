"""One-time import of the old Google Sheet report tab into local SQLite.

Examples:
  python tools/migrate_google_sheet.py --account kr_mock --csv old_kr_mock.csv
  python tools/migrate_google_sheet.py --account kr_mock --sheet-id ID --tab kr_mock \
      --service-account secrets/google_service_account.json
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from src.data.local_report_store import LocalReportStore

def _number(value: object) -> float:
    text = str(value or "").replace(",", "").replace("₩", "").replace("$", "").replace("%", "").strip()
    return float(text or 0)


def normalize_rows(records: list[list[object]]) -> list[dict[str, object]]:
    """Map by column order so Korean/legacy-mojibake header text is harmless."""
    rows = []
    for record in records:
        values = list(record) + [""] * 11
        if not str(values[0]).strip():
            continue
        rows.append({
            "recorded_at": str(values[0]).strip(), "close": _number(values[1]), "avg_price": _number(values[2]),
            "star_price": _number(values[3]), "qty": _number(values[4]), "qty_change": _number(values[5]),
            "realized_pnl": _number(values[6]), "cum_pnl": _number(values[7]), "cum_invest": _number(values[8]),
            "cur_invest": _number(values[9]), "potential_pnl_pct": _number(values[10]),
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--sheet-id")
    parser.add_argument("--tab")
    parser.add_argument("--service-account", type=Path)
    parser.add_argument("--export", type=Path)
    args = parser.parse_args()
    if bool(args.csv) == bool(args.sheet_id):
        parser.error("provide exactly one of --csv or --sheet-id")
    if args.csv:
        with args.csv.open(encoding="utf-8-sig", newline="") as source:
            raw = list(csv.reader(source))[1:]
    else:
        if not args.tab or not args.service_account:
            parser.error("--sheet-id requires --tab and --service-account")
        try:
            import gspread
            from google.oauth2.service_account import Credentials
        except ImportError as exc:
            raise SystemExit("Google migration dependencies are missing. Install requirements-migration.txt first.") from exc
        credentials = Credentials.from_service_account_file(args.service_account, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
        raw = gspread.authorize(credentials).open_by_key(args.sheet_id).worksheet(args.tab).get_all_values()[1:]
    store = LocalReportStore(args.account)
    count = store.import_rows(normalize_rows(raw))
    export = store.export_csv(args.export or Path("data/exports") / f"reports_{args.account}.csv")
    print(f"Imported {count} report rows into {store.path}; exported {export}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
