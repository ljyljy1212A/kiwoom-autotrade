"""Durable, account-scoped local reporting storage.

This store deliberately has no network dependency.  It is separate from the
confirmed-fill ledger: broker-confirmed fills remain in ``trades_<account>.db``
and report snapshots live in ``reports_<account>.db``.
"""
from __future__ import annotations

import csv
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping


REPORT_COLUMNS = (
    "recorded_at", "close", "avg_price", "star_price", "qty", "qty_change",
    "realized_pnl", "cum_pnl", "cum_invest", "cur_invest", "potential_pnl_pct",
)


class LocalReportStore:
    def __init__(self, account_id: str, data_dir: str | Path = "data"):
        self.account_id = account_id
        self.path = Path(data_dir) / f"reports_{account_id}.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            # Exports/report reads must never wait behind a live writer for a
            # long time or accidentally create a database file.
            uri = f"file:{self.path.resolve().as_posix()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=0.25)
            conn.execute("PRAGMA query_only=ON")
            return conn
        conn = sqlite3.connect(self.path, timeout=1.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=1000")
        return conn

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS report_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_at TEXT NOT NULL,
                    close REAL NOT NULL, avg_price REAL NOT NULL, star_price REAL NOT NULL,
                    qty REAL NOT NULL, qty_change REAL NOT NULL, realized_pnl REAL NOT NULL,
                    cum_pnl REAL NOT NULL, cum_invest REAL NOT NULL, cur_invest REAL NOT NULL,
                    potential_pnl_pct REAL NOT NULL,
                    source TEXT NOT NULL DEFAULT 'local'
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_report_snapshots_at ON report_snapshots(recorded_at)")

    def append_snapshot(
        self, *, close: float, avg_price: float, star_price: float, qty: float, qty_change: float,
        realized_pnl: float, cum_pnl: float, cum_invest: float, cur_invest: float,
        potential_pnl_pct: float, recorded_at: str | None = None, source: str = "local",
    ) -> None:
        values = (
            recorded_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"), close, avg_price, star_price,
            qty, qty_change, realized_pnl, cum_pnl, cum_invest, cur_invest, potential_pnl_pct, source,
        )
        with self._connect() as db:
            db.execute("""
                INSERT INTO report_snapshots
                (recorded_at,close,avg_price,star_price,qty,qty_change,realized_pnl,cum_pnl,
                 cum_invest,cur_invest,potential_pnl_pct,source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, values)

    def import_rows(self, rows: Iterable[Mapping[str, object]], source: str = "google-sheets") -> int:
        """Import report rows atomically; source rows must use REPORT_COLUMNS keys."""
        normalized = []
        for row in rows:
            try:
                normalized.append(tuple(
                    str(row["recorded_at"]) if name == "recorded_at" else float(row[name])
                    for name in REPORT_COLUMNS
                ) + (source,))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Invalid report row: {row!r}") from exc
        with self._connect() as db:
            db.executemany("""
                INSERT INTO report_snapshots
                (recorded_at,close,avg_price,star_price,qty,qty_change,realized_pnl,cum_pnl,
                 cum_invest,cur_invest,potential_pnl_pct,source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, normalized)
        return len(normalized)

    def export_csv(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._connect(read_only=True) as db, target.open("w", encoding="utf-8-sig", newline="") as out:
            writer = csv.writer(out)
            writer.writerow((*REPORT_COLUMNS, "source"))
            writer.writerows(db.execute("""
                SELECT recorded_at,close,avg_price,star_price,qty,qty_change,realized_pnl,cum_pnl,
                       cum_invest,cur_invest,potential_pnl_pct,source
                FROM report_snapshots ORDER BY id
            """))
        return target
