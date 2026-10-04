"""Account-scoped F5 observations, separate from the economic ledger."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import stat
import uuid


_TABLES = {"evidence_meta", "f5_observations"}


def _scope(account_id: str, broker_account: str) -> None:
    if not isinstance(account_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", account_id):
        raise ValueError("Explicit evidence account scope is required")
    if not isinstance(broker_account, str) or not re.fullmatch(r"(?:[0-9]{8}|[0-9]{10,12})", broker_account):
        raise ValueError("Explicit broker account scope is required")


def _path(value) -> Path:
    path = Path(value).absolute()
    for component in (path, *path.parents):
        if component.exists() or component.is_symlink():
            metadata = component.lstat()
            if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                raise ValueError("Evidence paths must not traverse reparse points")
    if not path.parent.is_dir():
        raise ValueError("Evidence parent directory must already exist")
    return path


def _connect(path: Path, mode: str) -> sqlite3.Connection:
    return sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True)


def _check_scope(db: sqlite3.Connection, account_id: str, broker_account: str) -> None:
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if tables != _TABLES:
        raise ValueError("Not an F5 evidence journal")
    rows = db.execute("SELECT schema_version,account_id,broker_account,market FROM evidence_meta").fetchall()
    if rows != [(1, account_id, broker_account, "US")]:
        raise ValueError("F5 evidence journal scope is inconsistent")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON keys are invalid evidence")
        result[key] = value
    return result


def decode_evidence_json(value):
    return json.loads(value, parse_constant=_reject_constant, object_pairs_hook=_unique_object)


def _reject_constant(value):
    raise ValueError("Non-finite JSON values are invalid evidence")


def _event_scope(item: dict, broker_account: str) -> None:
    if not isinstance(item, dict) or item.get("type") != "F5":
        raise ValueError("Invalid F5 event")
    values = item.get("values")
    if not isinstance(values, dict) or values.get("9201") != broker_account:
        raise ValueError("F5 event account scope is inconsistent")


class F5EvidenceJournal:
    """Explicitly created observation journal; no default path or migration."""

    def __init__(self, path, *, account_id: str, broker_account: str):
        _scope(account_id, broker_account)
        self.path = _path(path)
        self.account_id, self.broker_account = account_id, broker_account
        db = _connect(self.path, "ro")
        try:
            _check_scope(db, account_id, broker_account)
        finally:
            db.close()

    @classmethod
    def create(cls, path, *, account_id: str, broker_account: str):
        _scope(account_id, broker_account)
        target = _path(path)
        # Exclusive creation preserves existing journals and unrelated databases.
        with target.open("xb"):
            pass
        db = _connect(target, "rw")
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("CREATE TABLE evidence_meta (schema_version INTEGER,account_id TEXT,broker_account TEXT,market TEXT)")
                db.execute("CREATE TABLE f5_observations (observation_id TEXT PRIMARY KEY,frame_id TEXT NOT NULL,item_index INTEGER NOT NULL,observed_at TEXT NOT NULL,frame_sha256 TEXT NOT NULL,raw_event_json TEXT NOT NULL)")
                db.execute("INSERT INTO evidence_meta VALUES (1,?,?, 'US')", (account_id, broker_account))
                _check_scope(db, account_id, broker_account)
        finally:
            db.close()
        return cls(target, account_id=account_id, broker_account=broker_account)

    def record_frame(self, raw_frame: str | bytes, *, observed_at: datetime) -> int:
        """Preserve every F5 item and its local observation time atomically.

        No broker execution date, order date, or economic fill is inferred.
        Exact wire bytes are fingerprinted; complete parsed F5 items are stored.
        Other event types and authentication messages are never persisted.
        """
        if not isinstance(observed_at, datetime) or observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("Observation time must include a timezone")
        if not isinstance(raw_frame, (str, bytes)):
            raise ValueError("WebSocket evidence must be a JSON frame")
        raw_bytes = raw_frame.encode("utf-8") if isinstance(raw_frame, str) else raw_frame
        message = decode_evidence_json(raw_bytes.decode("utf-8"))
        if not isinstance(message, dict) or message.get("trnm") != "REAL":
            raise ValueError("Only REAL frames can supply F5 evidence")
        items = message.get("data")
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError("Invalid REAL event list")
        frame_id = uuid.uuid4().hex
        fingerprint = hashlib.sha256(raw_bytes).hexdigest()
        observed = observed_at.astimezone(timezone.utc).isoformat()
        rows = []
        for index, item in enumerate(items):
            if item.get("type") != "F5":
                continue
            _event_scope(item, self.broker_account)
            rows.append((uuid.uuid4().hex, frame_id, index, observed, fingerprint,
                         json.dumps(item, ensure_ascii=False, allow_nan=False, separators=(",", ":"))))
        if not rows:
            return 0
        db = _connect(_path(self.path), "rw")
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                _check_scope(db, self.account_id, self.broker_account)
                db.executemany("INSERT INTO f5_observations VALUES (?,?,?,?,?,?)", rows)
        finally:
            db.close()
        return len(rows)


def read_f5_observations(path, *, account_id: str, broker_account: str) -> list[dict]:
    """Open existing evidence with SQLite mode=ro; do not repair or create it."""
    _scope(account_id, broker_account)
    db = _connect(_path(path), "ro")
    try:
        db.execute("BEGIN")
        _check_scope(db, account_id, broker_account)
        rows = db.execute("SELECT observation_id,frame_id,item_index,observed_at,frame_sha256,raw_event_json FROM f5_observations ORDER BY observed_at,frame_id,item_index").fetchall()
        records = []
        for identity, frame_id, index, observed, fingerprint, raw in rows:
            item = decode_evidence_json(raw)
            _event_scope(item, broker_account)
            date = datetime.fromisoformat(observed)
            if date.tzinfo is None or date.utcoffset() is None or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
                raise ValueError("Malformed F5 observation metadata")
            records.append({"account_id": account_id, "market": "US",
                            "observation_id": identity, "frame_id": frame_id,
                            "item_index": index, "observed_at": observed,
                            "frame_sha256": fingerprint, "raw_event": item})
        return records
    finally:
        db.close()
