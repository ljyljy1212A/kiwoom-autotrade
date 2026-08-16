"""체결 중복 반영 차단 (요구사항 [7]).

체결마다 고유 키(계좌+주문번호+체결시퀀스 등)를 만들어 "이미 처리함"을 SQLite에 기록합니다.
조회가 여러 번 돌아도 딱 한 번만 포지션/잔고에 반영되도록 보장합니다.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path


class DedupStore:
    def __init__(self, db_path: str | Path = "data/dedup.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self):
        # This is a trading-side idempotency store. WAL avoids a short lookup
        # from blocking a concurrent confirmed-fill write in another worker.
        conn = sqlite3.connect(self.db_path, timeout=1.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=1000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self):
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_fills (
                    fill_key TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    qty REAL NOT NULL,
                    price REAL NOT NULL,
                    processed_at TEXT DEFAULT (datetime('now'))
                )
                """
            )

    @staticmethod
    def make_key(account_id: str, ord_no: str, cntr_seq: str = "") -> str:
        """체결 고유키 생성: 계좌ID + 주문번호(+체결시퀀스).

        키움 응답에 체결번호가 별도로 없는 TR도 있어, ord_no 만으로는 부족할 수 있으므로
        체결 조회 결과의 cntr_qty/cntr_pric 조합을 넘겨 받아 시퀀스를 만들 수도 있습니다.
        """
        return f"{account_id}:{ord_no}:{cntr_seq}"

    def is_processed(self, fill_key: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_fills WHERE fill_key = ?", (fill_key,)
            ).fetchone()
        return row is not None

    def mark_processed(self, fill_key: str, account_id: str, symbol: str, side: str, qty: float, price: float):
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO processed_fills (fill_key, account_id, symbol, side, qty, price)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (fill_key, account_id, symbol, side, qty, price),
            )

    def filter_new_fills(self, account_id: str, fills: list[dict]) -> list[dict]:
        """체결 내역 리스트에서 아직 처리 안 된 것만 걸러서 반환.

        fills 의 각 item 은 최소한 ord_no, cntr_qty, cntr_pric 를 가진다고 가정.
        """
        new_fills = []
        for f in fills:
            key = self.make_key(account_id, f.get("ord_no", ""), f.get("cntr_qty", ""))
            if not self.is_processed(key):
                f["_fill_key"] = key
                new_fills.append(f)
        return new_fills
