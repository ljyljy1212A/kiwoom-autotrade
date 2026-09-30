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


ATTESTATION_REASONS = {
    "verified_broker_app",
    "verified_account_statement",
    "no_evidence_assume_absent",
}


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
    reason: str | None
    dispatch_state: str = "unknown"


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
                dispatch_state TEXT NOT NULL DEFAULT 'unknown',
                unattributed_at TEXT, attested_by TEXT, attested_at TEXT,
                attested_outcome TEXT
            )"""
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_attempt_account_unattributed "
            "ON order_dispatch_attempts(account_id, unattributed_at, attested_at)"
        )
        self._ensure_reason_column()
        self._ensure_dispatch_state_column()
        self.db.commit()

    def _ensure_reason_column(self) -> None:
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(order_dispatch_attempts)")}
        if "reason" not in columns:
            self.db.execute("ALTER TABLE order_dispatch_attempts ADD COLUMN reason TEXT")

    def _ensure_dispatch_state_column(self) -> None:
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(order_dispatch_attempts)")}
        if "dispatch_state" not in columns:
            self.db.execute(
                "ALTER TABLE order_dispatch_attempts "
                "ADD COLUMN dispatch_state TEXT NOT NULL DEFAULT 'unknown'"
            )

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
               (attempt_id, account_id, side, symbol, qty, price, order_type,
                created_at, dispatch_state, unattributed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'in_flight', ?)""",
            (attempt_id, self.account_id, side, symbol, qty, price, order_type, created_at, created_at),
        )
        self.db.commit()
        return self.get_attempt(attempt_id)

    def _transition_dispatch_state(
        self,
        attempt_id: str,
        expected_state: str,
        dispatch_state: str,
        *,
        clear_unattributed: bool = False,
    ) -> OrderDispatchAttempt:
        assignments = "dispatch_state=?"
        if clear_unattributed:
            assignments += ", unattributed_at=NULL"
        try:
            cursor = self.db.execute(
                f"UPDATE order_dispatch_attempts SET {assignments} "
                "WHERE account_id=? AND attempt_id=? AND dispatch_state=? AND attested_at IS NULL",
                (dispatch_state, self.account_id, attempt_id, expected_state),
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        if cursor.rowcount != 1:
            raise ValueError(f"Cannot transition order attempt {attempt_id} from {expected_state}")
        return self.get_attempt(attempt_id)

    def mark_rejected(self, attempt_id: str) -> OrderDispatchAttempt:
        return self._transition_dispatch_state(
            attempt_id, "in_flight", "rejected", clear_unattributed=True
        )

    def mark_accepted_unlinked(self, attempt_id: str) -> OrderDispatchAttempt:
        return self._transition_dispatch_state(attempt_id, "in_flight", "accepted_unlinked")

    def mark_pending_recorded(self, attempt_id: str) -> OrderDispatchAttempt:
        return self._transition_dispatch_state(
            attempt_id, "accepted_unlinked", "pending_recorded", clear_unattributed=True
        )

    def mark_unknown(self, attempt_id: str) -> OrderDispatchAttempt:
        return self._transition_dispatch_state(attempt_id, "in_flight", "unknown")

    def mark_unattributed(self, attempt_id: str) -> OrderDispatchAttempt:
        # Legacy callers may classify an in-flight or unmarked migrated attempt.
        # A resolved attempt must never be reopened.
        try:
            cursor = self.db.execute(
                """UPDATE order_dispatch_attempts
                   SET unattributed_at=COALESCE(unattributed_at, ?), dispatch_state='unknown'
                   WHERE account_id=? AND attempt_id=? AND attested_at IS NULL
                     AND (dispatch_state='in_flight'
                          OR (dispatch_state='unknown' AND unattributed_at IS NULL))""",
                (_now(), self.account_id, attempt_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"No eligible order attempt {attempt_id} for account {self.account_id}")
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return self.get_attempt(attempt_id)

    def attest_unattributed(
        self,
        attempt_id: str,
        authenticated_operator_id: str,
        outcome: OrderAttestationOutcome,
        reason: str,
    ) -> OrderDispatchAttempt:
        if not authenticated_operator_id.strip():
            raise ValueError("authenticated_operator_id is required")
        if not isinstance(outcome, OrderAttestationOutcome):
            raise ValueError("outcome must be an OrderAttestationOutcome")
        if reason not in ATTESTATION_REASONS:
            raise ValueError(f"reason must be one of {sorted(ATTESTATION_REASONS)}")
        cursor = self.db.execute(
            """UPDATE order_dispatch_attempts
               SET attested_by=?, attested_at=?, attested_outcome=?, reason=?
               WHERE account_id=? AND attempt_id=?
                 AND unattributed_at IS NOT NULL AND attested_at IS NULL""",
            (
                authenticated_operator_id,
                _now(),
                outcome.value,
                reason,
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
            dispatch_state=row["dispatch_state"],
            unattributed_at=row["unattributed_at"],
            attested_by=row["attested_by"],
            attested_at=row["attested_at"],
            attested_outcome=OrderAttestationOutcome(outcome) if outcome else None,
            reason=row["reason"],
        )


def order_attempt_store(account_id: str, data_dir: Path = DATA_DIR) -> OrderAttemptStore:
    return OrderAttemptStore(_path(account_id, data_dir), account_id)


def unattributed_attempt_ids(account_id: str, data_dir: Path = DATA_DIR) -> list[str]:
    store = order_attempt_store(account_id, data_dir)
    try:
        return store.unattributed_attempt_ids()
    finally:
        store.close()


def existing_unattributed_attempt_ids(account_id: str, data_dir: Path = DATA_DIR) -> list[str]:
    """Read a previous account namespace without creating or changing its database."""
    path = _path(account_id, data_dir)
    try:
        path.lstat()
    except FileNotFoundError:
        return []
    db = sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True, timeout=1.0)
    try:
        return [
            row[0]
            for row in db.execute(
                """SELECT attempt_id FROM order_dispatch_attempts
                   WHERE account_id=? AND unattributed_at IS NOT NULL AND attested_at IS NULL
                   ORDER BY created_at, attempt_id""",
                (account_id,),
            )
        ]
    finally:
        db.close()


def list_unattributed_attempts(account_id: str, data_dir: Path = DATA_DIR) -> list[OrderDispatchAttempt]:
    store = order_attempt_store(account_id, data_dir)
    try:
        return [store.get_attempt(attempt_id) for attempt_id in store.unattributed_attempt_ids()]
    finally:
        store.close()
