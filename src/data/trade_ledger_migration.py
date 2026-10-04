"""Prepare a separate identity ledger copy. Never migrate a runtime DB in place."""
from __future__ import annotations

import os
import re
import sqlite3
import uuid
from pathlib import Path

from src.data.order_identity import (
    EVIDENCE_SCHEMA, IDENTITY_INDEX, IDENTITY_SCHEMA, IDENTITY_SCHEMA_VERSION,
)

TABLES = ("pending_orders", "trade_ledger", "execution_quantity_conflicts")


def _migrate_copy(db: sqlite3.Connection, account_markets: dict[str, str]) -> None:
    if db.in_transaction:
        raise ValueError("Migration requires its own transaction")
    if db.execute("PRAGMA user_version").fetchone()[0] != 0:
        raise ValueError("Unsupported or already migrated schema")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("BEGIN IMMEDIATE")
    try:
        # Rebuilding a parent table can cascade into tables outside TABLES.
        # Inspect every table before any schema/data change in this transaction.
        legacy_names = {table.casefold() for table in TABLES}
        table_names = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )]
        for table_name in table_names:
            quoted_name = '"' + table_name.replace('"', '""') + '"'
            for foreign_key in db.execute(f"PRAGMA foreign_key_list({quoted_name})"):
                if str(foreign_key["table"]).casefold() in legacy_names:
                    raise ValueError("Unexpected legacy inbound foreign key")
        schemas = {}
        indexes = []
        for table in TABLES:
            row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if row is None:
                raise ValueError(f"Missing legacy table: {table}")
            schemas[table] = row[0]
            if db.execute(f"PRAGMA foreign_key_list({table})").fetchone():
                raise ValueError("Unexpected legacy foreign keys")
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)).fetchone():
                raise ValueError("Unexpected legacy triggers")
            indexes.extend(row[0] for row in db.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,)))
        for table in TABLES:
            accounts = {row[0] for row in db.execute(f"SELECT DISTINCT account_id FROM {table}")}
            if any(account_markets.get(account) not in ("US", "KR") for account in accounts):
                raise ValueError("Every legacy account requires an explicit market")
        pending = {(r['account_id'], r['ord_no']): dict(r) for r in db.execute("SELECT * FROM pending_orders")}
        identities = {key: uuid.uuid4().hex for key in pending}
        for table in ("trade_ledger", "execution_quantity_conflicts"):
            for row in db.execute(f"SELECT * FROM {table}"):
                key = (row['account_id'], row['ord_no'])
                if key not in pending or row['symbol'] != pending[key]['symbol']:
                    raise ValueError("Legacy order linkage is missing or ambiguous")
                if table == "trade_ledger" and row['type'].upper() != pending[key]['side']:
                    raise ValueError("Legacy order side conflicts with its trade")
        broken = db.execute("SELECT 1 FROM trade_ledger s LEFT JOIN trade_ledger b ON b.id=s.buy_id "
                            "WHERE s.buy_id IS NOT NULL AND (b.id IS NULL OR b.account_id!=s.account_id "
                            "OR b.symbol!=s.symbol OR b.type!='buy' OR s.type!='sell') LIMIT 1").fetchone()
        if broken:
            raise ValueError("Legacy buy linkage is invalid")
        db.execute(IDENTITY_SCHEMA)
        db.execute(IDENTITY_INDEX)
        db.execute(EVIDENCE_SCHEMA)
        for key, row in pending.items():
            db.execute("INSERT INTO order_identities VALUES (?,?,?,?,?,?,NULL,'unresolved',?)",
                       (identities[key], key[0], account_markets[key[0]], key[1], row['symbol'],
                        row['side'], row['created_at']))
        for table in TABLES:
            sql = schemas[table]
            replacement = (f'CREATE TABLE {table}_identity_candidate ('
                           'order_uid TEXT NOT NULL REFERENCES order_identities(order_uid), ')
            sql, count = re.subn(r'^CREATE TABLE\s+(?:IF NOT EXISTS\s+)?' + table + r'\s*\(',
                                 replacement, sql, count=1, flags=re.IGNORECASE)
            if count != 1 or not sql.rstrip().endswith(')'):
                raise ValueError("Unsupported legacy table definition")
            if table != 'trade_ledger':
                sql, count = re.subn(r'PRIMARY KEY\s*\(\s*account_id\s*,\s*ord_no\s*\)',
                                     'PRIMARY KEY (order_uid)', sql, flags=re.IGNORECASE)
                if count != 1:
                    raise ValueError("Unsupported legacy order primary key")
            if table == 'trade_ledger':
                sql = sql.replace('(order_uid', "(execution_date_status TEXT NOT NULL DEFAULT "
                                  "'legacy_unverified', order_uid", 1)
            db.execute(sql)
            rows = [dict(row) for row in db.execute(f'SELECT * FROM {table}')]
            columns = [row['name'] for row in db.execute(f'PRAGMA table_info({table})')]
            quoted = ','.join('"' + c.replace('"', '""') + '"' for c in columns)
            for row in rows:
                values = [row[c] for c in columns] + [identities[(row['account_id'], row['ord_no'])]]
                marks = ','.join('?' for _ in values)
                db.execute(f'INSERT INTO {table}_identity_candidate ({quoted},order_uid) VALUES ({marks})', values)
            db.execute(f'DROP TABLE {table}')
            db.execute(f'ALTER TABLE {table}_identity_candidate RENAME TO {table}')
        for sql in indexes:
            db.execute(sql)
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Migrated ledger has broken foreign keys')
        db.execute(f'PRAGMA user_version={IDENTITY_SCHEMA_VERSION}')
        db.commit()
    except Exception:
        db.rollback()
        raise


def create_identity_ledger_copy(source: str | Path, destination: str | Path,
                                *, account_markets: dict[str, str]) -> Path:
    """Exclusive destination; failures retain an unpromoted copy for inspection.

    Only schema version zero is supported. Repeating a request cannot overwrite
    any existing destination. No date backfill, runtime activation or retry.
    """
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination:
        raise ValueError('In-place migration is forbidden')
    source_db = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
    target_db = None
    try:
        if source_db.execute('PRAGMA user_version').fetchone()[0] != 0:
            raise ValueError('Unsupported or already migrated source')
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        target_db = sqlite3.connect(destination)
        source_db.backup(target_db)
        _migrate_copy(target_db, account_markets)
    finally:
        if target_db is not None:
            target_db.close()
        source_db.close()
    return destination
