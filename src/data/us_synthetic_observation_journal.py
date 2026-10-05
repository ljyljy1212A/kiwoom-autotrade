"""Scratch-only file observation journal. Never economic or broker authority.

Callers must separately authorize the exact scratch roots and execution. The
allow_root argument constrains paths; it does not grant authorization. Identity
bindings are explicit synthetic fixtures, never authenticated account evidence.
No runtime paths, credentials, migration, order methods or automatic repair.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from src.data.order_identity import IDENTITY_INDEX, IDENTITY_SCHEMA, validate_order_date
from src.data.us_cumulative_execution import (
    CONTRACT, UsCumulativeObservationStore, _calculate, _decimal, _timestamp,
    initialize_cumulative_observation_schema, normalize_cumulative_observations,
)
from src.data.us_synthetic_generations import _identifier

_NAME = "synthetic_observation_journal.sqlite"
_POLICY = "scratch-us-mock-observations-v1"
_TABLES = {"synthetic_journal_meta", "order_identities", "pending_orders",
           "us_cumulative_observations", "us_cumulative_observation_audit", "us_cumulative_observation_conflicts"}


@dataclass(frozen=True)
class SyntheticJournalIdentity:
    order_uid: str
    order_date: str
    ord_no: str
    symbol: str
    side: str
    requested_quantity: str
    submitted_at_utc: str
    kind: str = "synthetic-journal-identity"


@dataclass(frozen=True)
class SyntheticJournalRecovery:
    observations: tuple
    conflicts: tuple
    state: str = "OBSERVATION_ONLY"
    execution_date_status: str = "unresolved"
    operational_ingestion_allowed: bool = False


def _path(scratch_root, allow_root):
    root, allowed = Path(scratch_root), Path(allow_root)
    if not root.is_absolute() or not allowed.is_absolute() or root == allowed:
        raise ValueError("Explicit absolute scratch child and allow root are required")
    # Refuse reparse points before resolution or directory enumeration.
    for path in (root, *root.parents, allowed, *allowed.parents):
        details = path.lstat()
        if path.is_symlink() or getattr(details, "st_file_attributes", 0) & 0x400:
            raise ValueError("Scratch reparse points are refused")
    if not root.is_dir() or not allowed.is_dir() or not root.resolve().is_relative_to(allowed.resolve()):
        raise ValueError("Scratch directory must stay inside the explicit allow root")
    target = root / _NAME
    if target.exists():
        details = target.lstat()
        if target.is_symlink() or getattr(details, "st_file_attributes", 0) & 0x400 or not target.is_file():
            raise ValueError("Journal must be a literal regular file")
    elif target.is_symlink():
        raise ValueError("Dangling journal link is refused")
    return target


def _schema_hash(db):
    entries = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    if (any(row[0] not in ("table", "index") for row in entries)
            or {row[1] for row in entries if row[0] == "table"} != _TABLES):
        raise ValueError("Unexpected journal schema objects")
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()


def _validate_journal_databases(databases):
    """Allow one file-backed main and only SQLite's pathless internal temp."""
    if (not isinstance(databases, (list, tuple)) or len(databases) not in (1, 2)
            or any(not isinstance(row, (list, tuple)) or len(row) != 3 for row in databases)):
        raise ValueError("One scratch file database is required")
    main = databases[0]
    if (type(main[0]) is not int or main[0] != 0 or main[1] != "main"
            or not isinstance(main[2], str) or not main[2]):
        raise ValueError("One scratch file database is required")
    if len(databases) == 2 and (
        type(databases[1][0]) is not int or tuple(databases[1]) != (1, "temp", "")
    ):
        raise ValueError("Only pathless internal temp is allowed beside main")


def create_synthetic_journal(scratch_root, *, allow_root):
    target = _path(scratch_root, allow_root)
    if any(target.parent.iterdir()):
        raise ValueError("Fresh empty scratch directory is required")
    # Exclusive claim prevents replacing any existing file. Failures retain the
    # attempted file; no deletion, fallback writer or automatic retry occurs.
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    db = sqlite3.connect(target)
    try:
        db.execute(IDENTITY_SCHEMA)
        db.execute(IDENTITY_INDEX)
        db.execute("CREATE TABLE pending_orders (order_uid TEXT PRIMARY KEY,account_id TEXT NOT NULL,"
                   "ord_no TEXT NOT NULL,symbol TEXT NOT NULL,side TEXT NOT NULL,requested_qty INTEGER NOT NULL,"
                   "filled_qty INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'open')")
        db.execute("CREATE TABLE synthetic_journal_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                   "policy TEXT NOT NULL,account_id TEXT NOT NULL,market TEXT NOT NULL,schema_sha256 TEXT NOT NULL)")
        db.execute("PRAGMA user_version=2")
        db.commit()
        initialize_cumulative_observation_schema(db)
        db.execute("INSERT INTO synthetic_journal_meta VALUES (1,?,'us_mock','US',?)", (_POLICY, _schema_hash(db)))
        db.commit()
        return SyntheticObservationJournal(db)
    except Exception:
        db.close()
        raise


def reopen_synthetic_journal(scratch_root, *, allow_root):
    target = _path(scratch_root, allow_root)
    # mode=rw refuses absence; reopening never initializes a replacement DB.
    db = sqlite3.connect(target.as_uri() + "?mode=rw", uri=True)
    try:
        journal = SyntheticObservationJournal(db)
        journal.recover()
        return journal
    except Exception:
        db.close()
        raise


class SyntheticObservationJournal:
    operational_ingestion_allowed = False

    def __init__(self, db):
        self.db = db
        self._metadata()
        self.store = UsCumulativeObservationStore(db)

    def _metadata(self):
        if self.db.in_transaction:
            raise ValueError("Journal operation requires its own transaction")
        databases = self.db.execute("PRAGMA database_list").fetchall()
        _validate_journal_databases(databases)
        if len(databases) == 2 and self.db.execute("SELECT 1 FROM temp.sqlite_master LIMIT 1").fetchone():
            raise ValueError("Internal temp schema must contain no user objects")
        if self.db.execute("PRAGMA user_version").fetchone()[0] != 2:
            raise ValueError("Unsupported synthetic journal version")
        row = self.db.execute("SELECT policy,account_id,market,schema_sha256 FROM synthetic_journal_meta WHERE singleton=1").fetchone()
        if row is None or row[:3] != (_POLICY, "us_mock", "US") or row[3] != _schema_hash(self.db):
            raise ValueError("Synthetic journal metadata/schema mismatch")

    def close(self):
        self.db.close()

    def bind(self, identity):
        self._metadata()
        if not isinstance(identity, SyntheticJournalIdentity) or identity.kind != "synthetic-journal-identity":
            raise ValueError("Explicit synthetic journal identity is required")
        _identifier(identity.order_uid)
        validate_order_date(identity.order_date)
        _timestamp(identity.submitted_at_utc)
        quantity = _decimal(identity.requested_quantity, "requested quantity")
        if (quantity <= 0 or quantity != quantity.to_integral_value() or quantity > 2 ** 53
                or not isinstance(identity.ord_no, str) or not re.fullmatch(r"[0-9]{9}", identity.ord_no)
                or not isinstance(identity.symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,11}", identity.symbol)
                or identity.side not in ("BUY", "SELL")):
            raise ValueError("Invalid synthetic order binding")
        values = (identity.order_uid, "us_mock", "US", identity.ord_no, identity.symbol, identity.side,
                  identity.order_date, "confirmed", identity.submitted_at_utc)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.db.execute("SELECT * FROM order_identities WHERE order_uid=?", (identity.order_uid,)).fetchone()
            if existing is not None:
                pending = self.db.execute("SELECT requested_qty FROM pending_orders WHERE order_uid=?", (identity.order_uid,)).fetchone()
                if existing != values or pending != (int(quantity),):
                    raise ValueError("Synthetic identity cannot be replaced")
                self.db.rollback()
                return "DUPLICATE"
            self.db.execute("INSERT INTO order_identities VALUES (?,?,?,?,?,?,?,?,?)", values)
            self.db.execute("INSERT INTO pending_orders VALUES (?,?,?,?,?,?,0,'open')",
                            (identity.order_uid, "us_mock", identity.ord_no, identity.symbol, identity.side, int(quantity)))
            self.db.commit()
            return "BOUND"
        except Exception:
            self.db.rollback()
            raise

    def observe(self, data, *, query_order_date, observed_at_utc):
        self.recover()  # Refuse corrupt existing state before any new audit write.
        items = normalize_cumulative_observations(data, account_id="us_mock", query_order_date=query_order_date,
                                                 observed_at_utc=observed_at_utc)
        return self.store.observe(items)

    def recover(self):
        self._metadata()
        self.db.execute("BEGIN")
        try:
            return self._recover_in_transaction()
        finally:
            self.db.rollback()

    def _recover_in_transaction(self):
        """Read-only validation inside a transaction owned by the caller."""
        if not self.db.in_transaction:
            raise ValueError("Journal validation requires an owned transaction")
        if self.db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("Journal integrity check failed")
        identities = dict((row[0], row) for row in self.db.execute("SELECT * FROM order_identities"))
        pending = dict((row[0], row) for row in self.db.execute("SELECT * FROM pending_orders"))
        if set(identities) != set(pending):
            raise ValueError("Identity/pending coverage mismatch")
        for uid, identity in identities.items():
            order = pending[uid]
            _identifier(uid)
            if (identity[1:3] != ("us_mock", "US") or identity[7] != "confirmed"
                    or not isinstance(identity[3], str) or not re.fullmatch(r"[0-9]{9}", identity[3])
                    or not isinstance(identity[4], str) or not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,11}", identity[4])
                    or identity[5] not in ("BUY", "SELL")
                    or order[:5] != (uid, "us_mock", identity[3], identity[4], identity[5])
                    or type(order[5]) is not int or not 0 < order[5] <= 2 ** 53
                    or order[6:] != (0, "open")):
                raise ValueError("Journal identity/state mismatch or economic mutation")
            validate_order_date(identity[6])
            _timestamp(identity[8])
        audits = []
        for row in self.db.execute("SELECT * FROM us_cumulative_observation_audit"):
            _identifier(row[0])
            _identifier(row[1])
            raw = json.loads(row[5])
            if (row[2] not in identities or row[4] not in ("observed", "duplicate", "conflict", "batch_blocked")
                    or raw["contract"] != CONTRACT or raw["source_api"] != "ust21150"):
                raise ValueError("Journal observation audit mismatch")
            identity = identities[row[2]]
            item = normalize_cumulative_observations(
                {"return_code": 0, "_execution_pages_complete": True, "_query_order_date": raw["query_order_date"],
                 "result_list": [raw["raw"]]}, account_id="us_mock", query_order_date=raw["query_order_date"],
                observed_at_utc=row[3],
            )[0]
            if (item.query_order_date, item.ord_no, item.symbol, item.side) != (identity[6], identity[3], identity[4], identity[5]):
                raise ValueError("Journal audit identity mismatch")
            audits.append((row[2], row[4], item))
        observations = tuple(self.db.execute("SELECT * FROM us_cumulative_observations ORDER BY order_uid"))
        for row in observations:
            if row[0] not in identities or row[8] != CONTRACT:
                raise ValueError("Foreign observation checkpoint")
            identity = identities[row[0]]
            q, average, amount = (_decimal(row[index], "observation", max_length=130) for index in (4, 5, 6))
            if (row[1:4] != ("us_mock", identity[6], identity[3]) or amount != _calculate(q, average)
                    or q > pending[row[0]][5] or not any(uid == row[0] and outcome in ("observed", "duplicate")
                        and (item.quantity, item.average_price, item.amount, item.observed_at_utc)
                        == (q, average, amount, row[7]) for uid, outcome, item in audits)):
                raise ValueError("Observation checkpoint has no consistent audit")
        conflicts = tuple(self.db.execute("SELECT * FROM us_cumulative_observation_conflicts ORDER BY order_uid"))
        for row in conflicts:
            if row[0] not in identities or _timestamp(row[2]) > _timestamp(row[3]):
                raise ValueError("Invalid journal conflict")
            _identifier(row[1])
            if not any(uid == row[0] and outcome == "conflict" and json.loads(item.raw_json) == json.loads(row[4])
                       for uid, outcome, item in audits):
                raise ValueError("Conflict checkpoint has no audit")
        return SyntheticJournalRecovery(observations, conflicts, "HELD" if conflicts else "OBSERVATION_ONLY")
