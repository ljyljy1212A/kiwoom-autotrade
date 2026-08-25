"""Durable ambiguous fixed-port order-attempt records."""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from src.core.runtime_paths import DATA_DIR


class OrderAttestationOutcome(Enum):
    ACCEPTED = "accepted"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    ABSENT = "absent"


@dataclass(frozen=True)
class OrderDispatchAttempt:
    attempt_id: str
    account_id: str
    side: str
    symbol: str
    qty: float
    price: float | None
    order_type: str
    created_at: str
    unattributed_at: str | None
    attested_by: str | None
    attested_at: str | None
    attested_outcome: OrderAttestationOutcome | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(account_id: str, data_dir: Path) -> Path:
    return data_dir / f"order_attempts_{account_id}.db"


class OrderAttemptStore:
    def __init__(self, path: str | Path, account_id: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.account_id = account_id
        self.db = sqlite3.connect(path, timeout=1.0)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=1000")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS order_dispatch_attempts (
                attempt_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                side TEXT NOT NULL, symbol TEXT NOT NULL, qty REAL NOT NULL,
                price REAL, order_type TEXT NOT NULL, created_at TEXT NOT NULL,
                unattributed_at TEXT, attested_by TEXT, attested_at TEXT,
                attested_outcome TEXT
            )"""
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_attempt_account_unattributed "
            "ON order_dispatch_attempts(account_id, unattributed_at, attested_at)"
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def record_attempt(
        self,
        side: str,
        symbol: str,
        qty: float,
        price: float | None,
        order_type: str,
    ) -> OrderDispatchAttempt:
        attempt_id = uuid.uuid4().hex
        created_at = _now()
        self.db.execute(
            """INSERT INTO order_dispatch_attempts
               (attempt_id, account_id, side, symbol, qty, price, order_type, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (attempt_id, self.account_id, side, symbol, qty, price, order_type, created_at),
        )
        self.db.commit()
        return self.get_attempt(attempt_id)

    def mark_unattributed(self, attempt_id: str) -> OrderDispatchAttempt:
        cursor = self.db.execute(
            """UPDATE order_dispatch_attempts SET unattributed_at=?
               WHERE account_id=? AND attempt_id=? AND attested_at IS NULL""",
            (_now(), self.account_id, attempt_id),
        )
        self.db.commit()
        if cursor.rowcount != 1:
            raise ValueError(f"No unattested order attempt {attempt_id} for account {self.account_id}")
        return self.get_attempt(attempt_id)

    def attest_unattributed(
        self,
        attempt_id: str,
        authenticated_operator_id: str,
        outcome: OrderAttestationOutcome,
    ) -> OrderDispatchAttempt:
        if not authenticated_operator_id.strip():
            raise ValueError("authenticated_operator_id is required")
        if not isinstance(outcome, OrderAttestationOutcome):
            raise ValueError("outcome must be an OrderAttestationOutcome")
        cursor = self.db.execute(
            """UPDATE order_dispatch_attempts
               SET attested_by=?, attested_at=?, attested_outcome=?
               WHERE account_id=? AND attempt_id=?
                 AND unattributed_at IS NOT NULL AND attested_at IS NULL""",
            (
                authenticated_operator_id,
                _now(),
                outcome.value,
                self.account_id,
                attempt_id,
            ),
        )
        self.db.commit()
        if cursor.rowcount != 1:
            raise ValueError(f"No unattested unattributed attempt {attempt_id} for account {self.account_id}")
        return self.get_attempt(attempt_id)

    def unattributed_attempt_ids(self) -> list[str]:
        return [
            row["attempt_id"]
            for row in self.db.execute(
                """SELECT attempt_id FROM order_dispatch_attempts
                   WHERE account_id=? AND unattributed_at IS NOT NULL AND attested_at IS NULL
                   ORDER BY created_at, attempt_id""",
                (self.account_id,),
            )
        ]

    def get_attempt(self, attempt_id: str) -> OrderDispatchAttempt:
        row = self.db.execute(
            "SELECT * FROM order_dispatch_attempts WHERE account_id=? AND attempt_id=?",
            (self.account_id, attempt_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"No order attempt {attempt_id} for account {self.account_id}")
        outcome = row["attested_outcome"]
        return OrderDispatchAttempt(
            attempt_id=row["attempt_id"],
            account_id=row["account_id"],
            side=row["side"],
            symbol=row["symbol"],
            qty=row["qty"],
            price=row["price"],
            order_type=row["order_type"],
            created_at=row["created_at"],
            unattributed_at=row["unattributed_at"],
            attested_by=row["attested_by"],
            attested_at=row["attested_at"],
            attested_outcome=OrderAttestationOutcome(outcome) if outcome else None,
        )


def order_attempt_store(account_id: str, data_dir: Path = DATA_DIR) -> OrderAttemptStore:
    return OrderAttemptStore(_path(account_id, data_dir), account_id)


def unattributed_attempt_ids(account_id: str, data_dir: Path = DATA_DIR) -> list[str]:
    store = order_attempt_store(account_id, data_dir)
    try:
        return store.unattributed_attempt_ids()
    finally:
        store.close()
