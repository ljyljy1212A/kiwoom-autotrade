"""Export an account's local SQLite report history as an Excel-friendly CSV."""
from __future__ import annotations

import argparse
from pathlib import Path

from src.data.local_report_store import LocalReportStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or Path("data/exports") / f"reports_{args.account}.csv"
    print(LocalReportStore(args.account).export_csv(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
