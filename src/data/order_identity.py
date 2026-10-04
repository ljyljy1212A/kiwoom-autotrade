"""Explicit order identities; no broker calls or automatic date inference."""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

IDENTITY_SCHEMA_VERSION = 2
IDENTITY_SCHEMA = """CREATE TABLE order_identities (
    order_uid TEXT PRIMARY KEY,
    account_id TEXT NOT NULL, market TEXT NOT NULL CHECK(market IN ('US','KR')),
    ord_no TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
    broker_order_date TEXT,
    identity_status TEXT NOT NULL CHECK(identity_status IN ('unresolved','confirmed','conflict')),
    submitted_at_utc TEXT NOT NULL,
    CHECK(identity_status!='confirmed' OR broker_order_date IS NOT NULL)
)"""
IDENTITY_INDEX = """CREATE UNIQUE INDEX idx_confirmed_broker_order
    ON order_identities(account_id,market,broker_order_date,ord_no)
    WHERE broker_order_date IS NOT NULL"""
EVIDENCE_SCHEMA = """CREATE TABLE order_identity_evidence (
    evidence_uid TEXT PRIMARY KEY,
    order_uid TEXT NOT NULL REFERENCES order_identities(order_uid),
    proposed_order_date TEXT NOT NULL, evidence_json TEXT NOT NULL,
    observed_at_utc TEXT NOT NULL, outcome TEXT NOT NULL
)"""


@dataclass(frozen=True)
class OrderIdentity:
    order_uid: str
    account_id: str
    market: str
    ord_no: str
    symbol: str
    side: str
    broker_order_date: str | None
    identity_status: str
    submitted_at_utc: str


class IdentityConflictError(ValueError):
    """An identity conflict has been durably latched."""


def validate_order_date(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{8}", value):
        raise ValueError("Broker order date must be YYYYMMDD")
    datetime.strptime(value, "%Y%m%d")
    return value


class OrderIdentityStore:
    """Use an explicitly prepared v2 connection; never initialize or migrate it."""

    def __init__(self, db: sqlite3.Connection):
        if db.execute("PRAGMA user_version").fetchone()[0] != IDENTITY_SCHEMA_VERSION:
            raise ValueError("Explicit identity ledger migration is required")
        self.db = db

    def get(self, order_uid: str) -> OrderIdentity:
        cursor = self.db.execute("SELECT * FROM order_identities WHERE order_uid=?", (order_uid,))
        row = cursor.fetchone()
        if row is None:
            raise ValueError("Unknown order identity")
        return OrderIdentity(**dict(zip((item[0] for item in cursor.description), row)))

    def _begin(self) -> None:
        if self.db.in_transaction:
            raise ValueError("Identity operation requires its own transaction")
        self.db.execute("BEGIN IMMEDIATE")

    def add_unresolved(self, account_id: str, market: str, ord_no: str,
                       symbol: str, side: str, submitted_at_utc: str) -> OrderIdentity:
        if market not in ("US", "KR") or side not in ("BUY", "SELL"):
            raise ValueError("Invalid market or side")
        if any(not isinstance(v, str) or not v.strip() for v in (account_id, ord_no, symbol)):
            raise ValueError("Account, order number and symbol are required")
        stamp = datetime.fromisoformat(submitted_at_utc)
        if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
            raise ValueError("Submission observation must be UTC")
        uid = uuid.uuid4().hex
        self._begin()
        try:
            self.db.execute("INSERT INTO order_identities VALUES (?,?,?,?,?,?,NULL,'unresolved',?)",
                            (uid, account_id, market, ord_no, symbol, side, submitted_at_utc))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return self.get(uid)

    def confirm_us_date(self, order_uid: str, broker_order_date: str, *, evidence: dict) -> OrderIdentity:
        """Caller must supply reviewed broker evidence; no candidate selection here."""
        date = validate_order_date(broker_order_date)
        if not isinstance(evidence, dict) or any(
            not isinstance(evidence.get(key), str) or not evidence[key].strip()
            for key in ("source_tr", "record_ref", "verified_at_utc")
        ):
            raise ValueError("Broker evidence requires source_tr, record_ref and verified_at_utc")
        encoded = json.dumps(evidence, sort_keys=True, allow_nan=False)
        self._begin()
        try:
            current = self.get(order_uid)
            if current.market != "US":
                raise ValueError("US date confirmation cannot change a KR identity")
            duplicate = self.db.execute(
                "SELECT order_uid FROM order_identities WHERE account_id=? AND market=? "
                "AND broker_order_date=? AND ord_no=? AND order_uid!=?",
                (current.account_id, current.market, date, current.ord_no, order_uid),
            ).fetchone()
            conflict = (current.identity_status == "conflict" or duplicate is not None
                        or current.broker_order_date not in (None, date))
            status = "conflict" if conflict else "confirmed"
            if conflict:
                # Keep the established date and latch both identities on a collision.
                self.db.execute("UPDATE order_identities SET identity_status='conflict' WHERE order_uid=?",
                                (order_uid,))
                if duplicate is not None:
                    self.db.execute("UPDATE order_identities SET identity_status='conflict' WHERE order_uid=?",
                                    (duplicate[0],))
            else:
                self.db.execute("UPDATE order_identities SET broker_order_date=?, identity_status=? "
                                "WHERE order_uid=?", (date, status, order_uid))
            now = datetime.now(timezone.utc).isoformat()
            for uid in ([order_uid, duplicate[0]] if duplicate is not None else [order_uid]):
                self.db.execute("INSERT INTO order_identity_evidence VALUES (?,?,?,?,?,?)",
                                (uuid.uuid4().hex, uid, date, encoded, now, status))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        if conflict:
            raise IdentityConflictError("Broker order identity conflict; attribution is blocked")
        return self.get(order_uid)
