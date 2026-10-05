"""Memory-only multi-scope generation storage and read-only recovery prototype.

Stores generation economics only; no bridge checkpoint adoption or operational
schema migration. Recovery reconstructs a disposable memory registry using fixed
schema, then validates every generation event/cost/finality before projection.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager

from src.data.us_synthetic_generations import (
    SyntheticGenerationRegistry, _identifier, initialize_synthetic_generations,
)
from src.data.us_synthetic_ledger import _memory_only

_TABLES = {
    "generations": ("synthetic_generations", 6),
    "orders": ("synthetic_generation_orders", 4),
    "events": ("synthetic_generation_events", 6),
    "holds": ("synthetic_generation_holds", 2),
}


def _scope_key(scope):
    if (not isinstance(scope, tuple) or len(scope) != 4
            or scope[:2] != ("us_mock", "US")
            or not isinstance(scope[2], str)
            or not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,11}", scope[2])):
        raise ValueError("Explicit canonical US mock scope is required")
    _identifier(scope[3])
    return json.dumps(scope, separators=(",", ":"))


def initialize_synthetic_scope_store(db):
    """Explicit fresh schema only. No paths, repair, migration or adoption."""
    _memory_only(db)
    if db.in_transaction:
        raise ValueError("Scope schema requires its own transaction")
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("CREATE TABLE synthetic_scoped_state (scope_key TEXT PRIMARY KEY,"
                   "revision INTEGER NOT NULL,policy TEXT NOT NULL)")
        db.execute("CREATE TABLE synthetic_scoped_generations (scope_key TEXT NOT NULL,"
                   "generation_id TEXT NOT NULL,step INTEGER NOT NULL,buy_order_uid TEXT NOT NULL,"
                   "predecessor_id TEXT,status TEXT NOT NULL,closure_json TEXT,"
                   "PRIMARY KEY(scope_key,generation_id),UNIQUE(scope_key,buy_order_uid),"
                   "UNIQUE(scope_key,predecessor_id))")
        db.execute("CREATE UNIQUE INDEX synthetic_scoped_active ON synthetic_scoped_generations"
                   "(scope_key,step) WHERE status!='CLOSED'")
        db.execute("CREATE TABLE synthetic_scoped_orders (scope_key TEXT NOT NULL,order_uid TEXT NOT NULL,"
                   "generation_id TEXT NOT NULL,side TEXT NOT NULL,finality_json TEXT,"
                   "PRIMARY KEY(scope_key,order_uid))")
        db.execute("CREATE TABLE synthetic_scoped_events (scope_key TEXT NOT NULL,event_id TEXT NOT NULL,"
                   "generation_id TEXT NOT NULL,order_uid TEXT NOT NULL,ordinal INTEGER NOT NULL,"
                   "proof_json TEXT NOT NULL,cost_json TEXT NOT NULL,PRIMARY KEY(scope_key,event_id),"
                   "UNIQUE(scope_key,generation_id,ordinal))")
        db.execute("CREATE TABLE synthetic_scoped_holds (scope_key TEXT NOT NULL,generation_id TEXT NOT NULL,"
                   "reason TEXT NOT NULL,PRIMARY KEY(scope_key,generation_id))")
        db.commit()
    except Exception:
        db.rollback()
        raise


def _validated_snapshot(scope, revision, rows):
    """Use only fixed schema; persisted content never supplies executable SQL."""
    if type(revision) is not int or revision < 0:
        raise ValueError("Invalid generation revision")
    child = sqlite3.connect(":memory:")
    try:
        initialize_synthetic_generations(child, account_id=scope[0], market=scope[1],
                                         symbol=scope[2], lifecycle_id=scope[3])
        for name, (table, width) in _TABLES.items():
            child.executemany(f"INSERT INTO {table} VALUES ({','.join('?' for _ in range(width))})",
                              rows[name])
        child.execute("UPDATE synthetic_generation_scope SET revision=? WHERE singleton=1", (revision,))
        child.commit()
        registry = SyntheticGenerationRegistry(child, account_id=scope[0], market=scope[1],
                                               symbol=scope[2], lifecycle_id=scope[3])
        # Validate malformed holds and chain state even when snapshot is HELD.
        for gid, *_ in rows["generations"]:
            registry._validate_existing_generation(gid)
        for gid, reason in rows["holds"]:
            registry._row(gid)
            _identifier(reason)
        return registry.snapshot()
    finally:
        child.close()


class SyntheticScopeStore:
    operational_transition_allowed = False

    def __init__(self, db):
        _memory_only(db)
        self.db = db
        for name in ("state", *_TABLES):
            db.execute(f"SELECT * FROM synthetic_scoped_{name} LIMIT 0")

    @contextmanager
    def _transaction(self, write=False):
        _memory_only(self.db)
        if self.db.in_transaction:
            raise ValueError("Scope store requires its own transaction")
        self.db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield
            if write:
                self.db.commit()
            else:
                self.db.rollback()
        except Exception:
            self.db.rollback()
            raise

    def _read(self, key):
        header = self.db.execute("SELECT revision,policy FROM synthetic_scoped_state WHERE scope_key=?",
                                 (key,)).fetchone()
        if header is None or header[1] != "synthetic-scope-store-v1":
            raise ValueError("Missing scope or incompatible storage policy")
        rows = {name: [tuple(row[1:]) for row in self.db.execute(
            f"SELECT * FROM synthetic_scoped_{name} WHERE scope_key=? ORDER BY 2", (key,),
        )] for name in _TABLES}
        return header[0], rows

    def recover(self, scope):
        key = _scope_key(scope)
        with self._transaction():
            revision, rows = self._read(key)
            return _validated_snapshot(scope, revision, rows)

    def publish(self, registry, *, expected_revision):
        """Atomically checkpoint one explicit synthetic registry scope.

        None means a new scope, never overwrite. Same revision requires identical
        rows; lower revisions and conflicting content refuse without repair.
        """
        if not isinstance(registry, SyntheticGenerationRegistry) or registry.db is self.db:
            raise ValueError("A separate prepared synthetic registry is required")
        key = _scope_key(registry.scope)
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
            raise ValueError("Explicit previous stored revision is required")
        with registry._transaction() as revision:
            rows = {name: [tuple(row) for row in registry.db.execute(
                f"SELECT * FROM {table} ORDER BY 1",
            )] for name, (table, _) in _TABLES.items()}
            snapshot = _validated_snapshot(registry.scope, revision, rows)
        with self._transaction(True):
            existing = self.db.execute("SELECT revision FROM synthetic_scoped_state WHERE scope_key=?", (key,)).fetchone()
            if existing is None:
                if expected_revision is not None:
                    raise ValueError("Expected existing scope is missing")
                if any(self.db.execute(f"SELECT 1 FROM synthetic_scoped_{name} WHERE scope_key=?", (key,)).fetchone()
                       for name in _TABLES):
                    raise ValueError("Orphan scope rows prevent initialization")
            else:
                old_revision, old_rows = self._read(key)
                _validated_snapshot(registry.scope, old_revision, old_rows)
                if expected_revision != old_revision or revision < old_revision:
                    raise ValueError("Stored scope revision conflict")
                if revision == old_revision:
                    if rows != old_rows:
                        raise ValueError("Same revision has different generation content")
                    return snapshot
                # A higher revision cannot erase or replace accepted history.
                if (not set(old_rows["events"]).issubset(rows["events"])
                        or not set(old_rows["holds"]).issubset(rows["holds"])):
                    raise ValueError("Published event/hold history cannot be replaced")
                for name, prefix in (("generations", 4), ("orders", 3)):
                    incoming = {row[0]: row for row in rows[name]}
                    for old in old_rows[name]:
                        new = incoming.get(old[0])
                        if (new is None or new[:prefix] != old[:prefix]
                                or (name == "generations" and old[4] == "CLOSED" and new != old)
                                or (name == "orders" and old[3] is not None and new[3] != old[3])):
                            raise ValueError("Published ownership/finality history cannot be replaced")
            for name in _TABLES:
                self.db.execute(f"DELETE FROM synthetic_scoped_{name} WHERE scope_key=?", (key,))
            self.db.execute("INSERT INTO synthetic_scoped_state VALUES (?,?,'synthetic-scope-store-v1') "
                            "ON CONFLICT(scope_key) DO UPDATE SET revision=excluded.revision", (key, revision))
            for name, (_, width) in _TABLES.items():
                self.db.executemany(f"INSERT INTO synthetic_scoped_{name} VALUES ({','.join('?' for _ in range(width + 1))})",
                                    [(key, *row) for row in rows[name]])
        return snapshot
