"""Durable order/fill state used by the account synchronizer.

The broker is the source of truth.  This store only records submitted order
intent and broker-confirmed fills, so it can safely rebuild in-memory strategy
state after a restart.
"""
from __future__ import annotations

import json
import math
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.data.order_identity import OrderIdentityStore, validate_order_date


@dataclass
class PendingOrder:
    ord_no: str
    symbol: str
    side: str
    requested_qty: float
    requested_price: float | None
    action: str
    step: int
    meta: dict
    filled_qty: float = 0.0
    created_at: str = ""
    status: str = "open"
    order_uid: str | None = None
    broker_order_date: str | None = None
    identity_status: str = "unresolved"


class FillQuantityExceededError(ValueError):
    """A cumulative quantity conflicts with the durable order's request."""

    def __init__(self, order: PendingOrder, cumulative_qty: float, reason: str):
        self.ord_no = order.ord_no
        self.requested_qty = order.requested_qty
        self.filled_qty = order.filled_qty
        self.cumulative_qty = cumulative_qty
        self.reason = reason
        super().__init__(
            f"{reason}: order={self.ord_no}, requested={self.requested_qty}, "
            f"stored={self.filled_qty}, observed={self.cumulative_qty}"
        )


class TradeLedgerStore:
    def __init__(self, path: str, account_id: str, *, market: str | None = None):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.account_id = account_id
        # WAL lets the dashboard's read-only reporting connection run beside
        # confirmed-fill writes without readers taking a blocking read lock.
        self.db = sqlite3.connect(path, timeout=1.0)
        self.db.row_factory = sqlite3.Row
        self.schema_version = self.db.execute("PRAGMA user_version").fetchone()[0]
        self.market = market
        try:
            if self.schema_version not in (0, 2):
                raise ValueError("Unsupported ledger schema")
            if self.schema_version == 2:
                if market not in ("US", "KR"):
                    raise ValueError("Identity ledger requires an explicit market")
                self.db.execute("PRAGMA foreign_keys=ON")
                if self.db.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("Identity ledger has broken foreign keys")
                for table in ("pending_orders", "trade_ledger", "execution_quantity_conflicts"):
                    invalid = self.db.execute(
                        f"SELECT 1 FROM {table} p LEFT JOIN order_identities i ON p.order_uid=i.order_uid "
                        "WHERE i.order_uid IS NULL OR p.account_id!=i.account_id OR p.ord_no!=i.ord_no "
                        "OR p.symbol!=i.symbol LIMIT 1"
                    ).fetchone()
                    if invalid:
                        raise ValueError("Identity ledger has inconsistent order linkage")
                if self.db.execute("SELECT 1 FROM order_identities WHERE account_id=? AND market!=? LIMIT 1",
                                   (account_id, market)).fetchone():
                    raise ValueError("Identity ledger account market mismatch")
        except Exception:
            self.db.close()
            raise
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=1000")
        # Runtime recovery supplies this boundary. Historical rows stay in
        # SQLite for reports but cannot be re-attributed to a new lifecycle.
        self._lifecycle_started_at: str | None = None
        if self.schema_version == 0:
            self._create_tables()

    def set_lifecycle_started_at(self, started_at: str | None) -> None:
        self._lifecycle_started_at = str(started_at) if started_at else None

    def _lifecycle_clause(self, column: str = "created_at") -> tuple[str, list[str]]:
        if not self._lifecycle_started_at:
            return "", []
        return f" AND {column}>=?", [self._lifecycle_started_at]

    def _create_tables(self) -> None:
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS pending_orders (
            account_id TEXT NOT NULL, ord_no TEXT NOT NULL, symbol TEXT NOT NULL,
            side TEXT NOT NULL, requested_qty REAL NOT NULL, requested_price REAL,
            action TEXT NOT NULL, step INTEGER NOT NULL, meta_json TEXT NOT NULL,
            filled_qty REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY (account_id, ord_no)
        );
        CREATE TABLE IF NOT EXISTS trade_ledger (
            id TEXT PRIMARY KEY, account_id TEXT NOT NULL, ord_no TEXT NOT NULL,
            symbol TEXT NOT NULL, type TEXT NOT NULL, step INTEGER NOT NULL,
            filled_at TEXT NOT NULL, qty REAL NOT NULL, price REAL NOT NULL,
            buy_id TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS execution_quantity_conflicts (
            account_id TEXT NOT NULL, ord_no TEXT NOT NULL, symbol TEXT NOT NULL,
            requested_qty REAL NOT NULL, stored_filled_qty REAL NOT NULL,
            observed_qty REAL NOT NULL, reason TEXT NOT NULL,
            first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
            PRIMARY KEY (account_id, ord_no)
        );
        CREATE INDEX IF NOT EXISTS idx_quantity_conflict_account_symbol
            ON execution_quantity_conflicts(account_id, symbol);
        CREATE INDEX IF NOT EXISTS idx_ledger_account_symbol ON trade_ledger(account_id, symbol);
        CREATE INDEX IF NOT EXISTS idx_ledger_account_created ON trade_ledger(account_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_ledger_account_symbol_step_type
            ON trade_ledger(account_id, symbol, step, type);
        CREATE INDEX IF NOT EXISTS idx_pending_account_status_symbol
            ON pending_orders(account_id, status, symbol, side);
        """)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def backup_to(self, destination: str | Path) -> Path:
        """Create a consistent SQLite backup without copying a live database file.

        The desktop prototype performed an account-scoped backup before it began
        loading automated orders.  Keep the same recovery point while retaining
        this project's confirmed-fill-only ledger semantics.
        """
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        backup_db = sqlite3.connect(target, timeout=1.0)
        try:
            self.db.backup(backup_db)
        finally:
            # sqlite3.Connection's context manager commits/rolls back but does
            # not close the handle; explicitly close it for Windows backups.
            backup_db.close()
        return target

    def add_pending(self, order: PendingOrder) -> None:
        now = _now()
        if self.schema_version == 2:
            if order.order_uid is not None:
                raise ValueError("A persisted order intent cannot be replaced")
            if order.side not in ("BUY", "SELL") or not order.ord_no or not order.symbol:
                raise ValueError("Order number, symbol and side are required")
            if not math.isfinite(order.requested_qty) or order.requested_qty <= 0:
                raise ValueError("Requested quantity must be finite and positive")
            if order.filled_qty != 0 or order.broker_order_date is not None or order.identity_status != "unresolved":
                raise ValueError("New order identity must be unresolved and unfilled")
            uid = uuid.uuid4().hex
            encoded = json.dumps(order.meta, allow_nan=False)
            if self.db.in_transaction:
                raise ValueError("Order intent requires its own transaction")
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute("INSERT INTO order_identities VALUES (?,?,?,?,?,?,NULL,'unresolved',?)",
                                (uid, self.account_id, self.market, order.ord_no, order.symbol, order.side, now))
                self.db.execute("""INSERT INTO pending_orders
                    (order_uid,account_id,ord_no,symbol,side,requested_qty,requested_price,action,step,
                     meta_json,filled_qty,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,0,'open',?,?)""",
                    (uid, self.account_id, order.ord_no, order.symbol, order.side, order.requested_qty,
                     order.requested_price, order.action, order.step, encoded, now, now))
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            order.order_uid = uid
            return
        self.db.execute("""INSERT OR REPLACE INTO pending_orders
            (account_id,ord_no,symbol,side,requested_qty,requested_price,action,step,meta_json,filled_qty,status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,COALESCE((SELECT status FROM pending_orders WHERE account_id=? AND ord_no=?),'open'),?,?)""",
            (self.account_id, order.ord_no, order.symbol, order.side, order.requested_qty,
             order.requested_price, order.action, order.step, json.dumps(order.meta), order.filled_qty,
             self.account_id, order.ord_no, now, now))
        self.db.commit()

    def _order_filter(self, ord_no: str, order_uid: str | None = None) -> tuple[str, tuple]:
        if self.schema_version == 2:
            if not order_uid:
                raise ValueError("Identity ledger requires order_uid; order number alone is ambiguous")
            return "account_id=? AND ord_no=? AND order_uid=?", (self.account_id, ord_no, order_uid)
        return "account_id=? AND ord_no=?", (self.account_id, ord_no)

    def confirm_us_order_date(self, order_uid: str, date: str, *, evidence: dict):
        identity = OrderIdentityStore(self.db).get(order_uid)
        if identity.account_id != self.account_id:
            raise ValueError("Order identity belongs to another account")
        return OrderIdentityStore(self.db).confirm_us_date(order_uid, date, evidence=evidence)

    def get_pending(self, ord_no: str, *, order_uid: str | None = None) -> PendingOrder | None:
        clause, args = self._order_filter(ord_no, order_uid)
        row = self.db.execute("SELECT * FROM pending_orders WHERE " + clause, args).fetchone()
        return self._pending_from_row(row) if row else None

    def pending_orders(self, symbol: str | None = None) -> list[PendingOrder]:
        # A filled row with no durable quantity is a recoverable attribution
        # invariant violation, not a completed order. Keep it in the fill
        # polling set until the broker execution history supplies the fill.
        sql = ("SELECT * FROM pending_orders WHERE account_id=? "
               "AND (status='open' OR (status='filled' AND filled_qty<=0) "
               "OR status='awaiting_execution_history')")
        args: list[str] = [self.account_id]
        if symbol:
            sql += " AND symbol=?"
            args.append(symbol)
        rows = self.db.execute(sql, args).fetchall()
        return [self._pending_from_row(r) for r in rows]

    def has_pending_buy_at_price(self, symbol: str, price: float, tolerance: float = 0.0) -> bool:
        """Check for an unfilled BUY already submitted at this price level."""
        if tolerance > 0:
            row = self.db.execute(
                "SELECT 1 FROM pending_orders WHERE account_id=? AND status='open' "
                "AND side='BUY' AND symbol=? AND requested_price BETWEEN ? AND ? LIMIT 1",
                (self.account_id, symbol, price - tolerance, price + tolerance),
            ).fetchone()
        else:
            row = self.db.execute(
                "SELECT 1 FROM pending_orders WHERE account_id=? AND status='open' "
                "AND side='BUY' AND symbol=? AND requested_price=? LIMIT 1",
                (self.account_id, symbol, price),
            ).fetchone()
        return row is not None

    def has_pending_buy(self, symbol: str) -> bool:
        """A buy must be reconciled before another grid buy may be submitted.

        A broker that rejects cancellation as already-terminal has not proved
        that the order was unfilled.  Retain that order as an execution-history
        recovery candidate and keep the next grid buy blocked.
        """
        row = self.db.execute(
            "SELECT 1 FROM pending_orders WHERE account_id=? AND status IN ('open','awaiting_execution_history') "
            "AND side='BUY' AND symbol=? LIMIT 1", (self.account_id, symbol),
        ).fetchone()
        return row is not None

    def has_pending_sell(self, symbol: str, step: int) -> bool:
        """A tranche can have at most one unresolved profit-taking SELL.

        Acceptance by the broker is not a fill, so another tick must not
        submit a second SELL for that same symbol/line while the first order
        is still awaiting execution-history reconciliation.
        """
        row = self.db.execute(
            "SELECT 1 FROM pending_orders WHERE account_id=? "
            "AND status IN ('open','awaiting_execution_history') "
            "AND side='SELL' AND symbol=? AND step=? LIMIT 1",
            (self.account_id, symbol, int(step)),
        ).fetchone()
        return row is not None

    def record_fill(self, pending: PendingOrder, cumulative_qty: float, price: float, filled_at: str,
                    *, observation_only: bool = False, execution_date: str | None = None) -> dict | None:
        """Record only the newly-confirmed quantity from a cumulative broker value."""
        cumulative_qty = float(cumulative_qty)
        if not math.isfinite(cumulative_qty) or cumulative_qty < 0:
            raise ValueError("Cumulative fill quantity must be finite and nonnegative")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            clause, order_args = self._order_filter(pending.ord_no, pending.order_uid)
            current_row = self.db.execute("SELECT * FROM pending_orders WHERE " + clause, order_args).fetchone()
            if current_row is None:
                raise ValueError(f"No pending order {pending.ord_no} for account {self.account_id}")
            current = self._pending_from_row(current_row)
            if not math.isfinite(current.filled_qty) or current.filled_qty < 0:
                raise ValueError("Stored cumulative fill quantity must be finite and nonnegative")
            if not math.isfinite(current.requested_qty) or current.requested_qty <= 0:
                raise ValueError("Stored requested quantity must be finite and positive")
            # The persisted request is the attribution boundary. Never clip an
            # excess quantity or hide a corrupt counter as a duplicate fill.
            quantity_error = None
            if current.filled_qty > current.requested_qty:
                quantity_error = FillQuantityExceededError(
                    current, cumulative_qty, "stored_fill_exceeds_requested_quantity",
                )
            elif cumulative_qty > current.requested_qty:
                quantity_error = FillQuantityExceededError(
                    current, cumulative_qty, "cumulative_fill_exceeds_requested_quantity",
                )
            if quantity_error is not None:
                # Commit only the diagnostic latch before reporting rejection.
                # A restart must not forget a conflict just because the order
                # later fills, closes, or belongs to an earlier lifecycle.
                now = _now()
                columns = "account_id,ord_no,symbol,requested_qty,stored_filled_qty,observed_qty,reason,first_seen_at,last_seen_at"
                values = (self.account_id, current.ord_no, current.symbol, current.requested_qty,
                          current.filled_qty, cumulative_qty, quantity_error.reason, now, now)
                conflict_key = "account_id,ord_no"
                if self.schema_version == 2:
                    columns += ",order_uid"
                    values += (current.order_uid,)
                    conflict_key = "order_uid"
                self.db.execute(f"INSERT INTO execution_quantity_conflicts ({columns}) "
                                f"VALUES ({','.join('?' for _ in values)}) ON CONFLICT({conflict_key}) "
                                "DO UPDATE SET last_seen_at=excluded.last_seen_at", values)
                self.db.commit()
                # The exception handler's rollback cannot undo this committed
                # latch. Pending orders and confirmed trades were not written.
                raise quantity_error
            if observation_only:
                # Check the latest persisted quantities under the same lock,
                # independently of price. Never add a trade in this mode.
                self.db.rollback()
                return None
            if self.schema_version == 2:
                if current.identity_status == "conflict" or self.quantity_conflict_order_ids(current.symbol):
                    raise ValueError("Durable order conflict blocks fill attribution")
                if self.market == "US":
                    if current.identity_status != "confirmed" or current.broker_order_date is None:
                        raise ValueError("Broker order date identity is unconfirmed")
                    date = validate_order_date(execution_date)
                    if not isinstance(filled_at, str) or filled_at[:10] != datetime.strptime(date, "%Y%m%d").strftime("%Y-%m-%d"):
                        raise ValueError("Fill timestamp does not match authoritative execution date")
            price = float(price)
            if not math.isfinite(price) or price <= 0:
                raise ValueError("Execution price must be finite and positive")
            delta = cumulative_qty - float(current.filled_qty)
            if delta <= 0:
                self.db.rollback()
                return None

            # Read the persisted cumulative quantity inside the write
            # transaction. Callers can process several rows from one broker
            # response while holding the same stale PendingOrder snapshot.
            row_id = f"{'B' if current.side == 'BUY' else 'S'}-{current.order_uid or current.ord_no}-{_num(cumulative_qty)}"
            buy_id = self._buy_id_for_sell_order(current) if current.side == 'SELL' else None
            columns = "id,account_id,ord_no,symbol,type,step,filled_at,qty,price,buy_id,created_at"
            values = (row_id, self.account_id, current.ord_no, current.symbol, current.side.lower(), current.step,
                      filled_at, delta, price, buy_id, _now())
            if self.schema_version == 2:
                columns += ",order_uid,execution_date_status"
                values += (current.order_uid, "broker_confirmed" if self.market == "US" else "legacy_unverified")
            inserted = self.db.execute(f"INSERT INTO trade_ledger ({columns}) VALUES ({','.join('?' for _ in values)})", values)
            if inserted.rowcount != 1:
                raise RuntimeError(f"Could not record unique fill {row_id}")
            if cumulative_qty >= current.requested_qty:
                status = 'filled'
            elif current.status == 'awaiting_execution_history':
                status = 'awaiting_execution_history'
            else:
                status = 'open'
            updated = self.db.execute(
                "UPDATE pending_orders SET filled_qty=?, status=?, updated_at=? "
                "WHERE " + clause + " AND filled_qty=?",
                (cumulative_qty, status, _now(), *order_args, current.filled_qty),
            )
            if updated.rowcount != 1:
                raise RuntimeError(f"Pending order changed while recording fill {current.ord_no}")
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        row = self.db.execute("SELECT * FROM trade_ledger WHERE id=?", (row_id,)).fetchone()
        return dict(row) if row else None

    def mark_cancelled(self, ord_no: str, *, order_uid: str | None = None) -> None:
        """Close a broker-accepted cancellation without treating it as a fill."""
        clause, args = self._order_filter(ord_no, order_uid)
        self.db.execute(
            "UPDATE pending_orders SET status=?, updated_at=? WHERE " + clause,
            ("cancelled", _now(), *args),
        )
        self.db.commit()

    def mark_closed_unconfirmed(self, ord_no: str, *, order_uid: str | None = None) -> None:
        """Stop retrying an order the broker says has no open quantity."""
        clause, args = self._order_filter(ord_no, order_uid)
        self.db.execute(
            "UPDATE pending_orders SET status=?, updated_at=? WHERE " + clause,
            ("closed_unconfirmed", _now(), *args),
        )
        self.db.commit()

    def mark_awaiting_execution_history(self, ord_no: str, *, order_uid: str | None = None) -> None:
        """Keep a terminal broker order eligible for later fill recovery."""
        clause, args = self._order_filter(ord_no, order_uid)
        self.db.execute(
            "UPDATE pending_orders SET status=?, updated_at=? WHERE " + clause,
            ("awaiting_execution_history", _now(), *args),
        )
        self.db.commit()

    def execution_recovery_orders(self, symbol: str | None = None) -> list[PendingOrder]:
        """Return open and terminal-but-unconfirmed orders for REST matching."""
        sql = ("SELECT * FROM pending_orders WHERE account_id=? "
               "AND (status IN ('open','awaiting_execution_history') "
               "OR (status='filled' AND filled_qty<=0))")
        args: list[str] = [self.account_id]
        if symbol:
            sql += " AND symbol=?"
            args.append(symbol)
        return [self._pending_from_row(row) for row in self.db.execute(sql, args).fetchall()]

    def completed_orders_for_execution_observation(self, symbol: str) -> list[PendingOrder]:
        """Keep completed orders visible for rejection-only late-fill checks.

        Historical orders are observed but never applied to a new lifecycle.
        Absence from today's broker history is not finality evidence.
        """
        rows = self.db.execute(
            "SELECT * FROM pending_orders WHERE account_id=? AND symbol=? "
            "AND status='filled' AND filled_qty>0",
            (self.account_id, symbol),
        ).fetchall()
        return [self._pending_from_row(row) for row in rows]

    def quantity_conflict_order_ids(self, symbol: str) -> tuple[str, ...]:
        """Read sticky conflicts without filtering by order status or lifecycle.

        There is deliberately no automatic resolution method. Evidence-based
        operator resolution requires a separate reviewed policy.
        """
        column = "order_uid" if self.schema_version == 2 else "ord_no"
        rows = self.db.execute(
            f"SELECT {column} FROM execution_quantity_conflicts "
            f"WHERE account_id=? AND symbol=? ORDER BY {column}",
            (self.account_id, symbol),
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def identity_conflict_order_ids(self, symbol: str) -> tuple[str, ...]:
        if self.schema_version != 2:
            return ()
        return tuple(str(row[0]) for row in self.db.execute(
            "SELECT order_uid FROM order_identities WHERE account_id=? AND symbol=? AND identity_status='conflict'",
            (self.account_id, symbol),
        ))

    def has_unresolved_orders(self, symbol: str) -> bool:
        """True for any order that still needs broker fill attribution."""
        row = self.db.execute(
            "SELECT 1 FROM pending_orders WHERE account_id=? AND symbol=? "
            "AND (status IN ('open','awaiting_execution_history') "
            "OR (status='filled' AND filled_qty<=0)) "
            "UNION ALL SELECT 1 FROM execution_quantity_conflicts "
            "WHERE account_id=? AND symbol=? LIMIT 1",
            (self.account_id, symbol, self.account_id, symbol),
        ).fetchone()
        return row is not None or bool(self.identity_conflict_order_ids(symbol))

    def close_open_orders_for_symbol(self, symbol: str) -> int:
        """Retire pending strategy orders after a broker-confirmed full close."""
        cur = self.db.execute(
            "UPDATE pending_orders SET status='closed_unconfirmed', updated_at=? "
            "WHERE account_id=? AND symbol=? AND status='open'",
            (_now(), self.account_id, symbol),
        )
        self.db.commit()
        return int(cur.rowcount or 0)

    def ledger_rows(self, symbol: str | None = None) -> list[dict]:
        # Keep the durable order/timestamp identifiers in recovered rows.  The
        # restart reconciler groups partial fills by broker order and must be
        # able to choose the newest order rather than blend historical trading
        # cycles that happen to use the same tranche number.
        sql = (
            "SELECT id,ord_no,created_at,type,step,filled_at AS filledAt,qty,price,"
            "buy_id AS buyId FROM trade_ledger WHERE account_id=?"
        )
        if self.schema_version == 2:
            sql = sql.replace("SELECT id,", "SELECT order_uid,execution_date_status,id,", 1)
        args: list = [self.account_id]
        if symbol:
            sql += " AND symbol=?"
            args.append(symbol)
        clause, lifecycle_args = self._lifecycle_clause()
        sql += clause
        args.extend(lifecycle_args)
        rows = []
        for row in self.db.execute(sql + " ORDER BY created_at, id", args):
            item = dict(row)
            if item["buyId"] is None:
                item.pop("buyId")
            rows.append(item)
        return rows

    def tranche_summaries(self, symbol: str | None = None) -> list[dict]:
        """Confirmed BUY fills grouped by immutable symbol/strategy tranche."""
        sql = """
            SELECT symbol, step, SUM(qty) AS qty,
                   SUM(qty * price) / NULLIF(SUM(qty), 0) AS avg_price
            FROM trade_ledger
            WHERE account_id=? AND type='buy'
        """
        args: list = [self.account_id]
        if symbol:
            sql += " AND symbol=?"
            args.append(symbol)
        clause, lifecycle_args = self._lifecycle_clause()
        sql += clause
        args.extend(lifecycle_args)
        sql += " GROUP BY symbol, step ORDER BY symbol, step"
        return [dict(row) for row in self.db.execute(sql, args)]

    def open_tranche_qty(self, symbol: str, step: int) -> float:
        """Confirmed quantity still owned by one symbol/tranche."""
        clause, lifecycle_args = self._lifecycle_clause()
        row = self.db.execute(
            """SELECT COALESCE(SUM(CASE WHEN type='buy' THEN qty WHEN type='sell' THEN -qty ELSE 0 END), 0)
               AS qty
               FROM trade_ledger
               WHERE account_id=? AND symbol=? AND step=?""" + clause,
            [self.account_id, symbol, step, *lifecycle_args],
        ).fetchone()
        return max(0.0, float(row["qty"] if row else 0.0))

    def _buy_id_for_sell(self, symbol: str, step: int) -> str | None:
        # A grid sell closes its own tranche.  Partial sell fills retain the same buyId.
        clause, lifecycle_args = self._lifecycle_clause("b.created_at")
        row = self.db.execute("""SELECT b.id FROM trade_ledger b
            WHERE b.account_id=? AND b.symbol=? AND b.type='buy' AND b.step=?
              AND NOT EXISTS (SELECT 1 FROM trade_ledger s WHERE s.account_id=b.account_id
                              AND s.symbol=b.symbol AND s.type='sell' AND s.buy_id=b.id)
            """ + clause + " ORDER BY b.created_at DESC LIMIT 1",
            [self.account_id, symbol, step, *lifecycle_args],
        ).fetchone()
        return row["id"] if row else None

    def _buy_id_for_sell_order(self, pending: PendingOrder) -> str | None:
        """Keep every partial fill of one sell order linked to the same buy."""
        clause, args = self._order_filter(pending.ord_no, pending.order_uid)
        row = self.db.execute(
            "SELECT buy_id FROM trade_ledger WHERE " + clause +
            " AND type='sell' AND buy_id IS NOT NULL ORDER BY created_at LIMIT 1", args,
        ).fetchone()
        return str(row["buy_id"]) if row else self._buy_id_for_sell(pending.symbol, pending.step)

    def repair_cross_symbol_sell_buy_links(self, symbol: str | None = None) -> list[dict]:
        """Repair only provably-invalid legacy SELL links.

        A link is eligible only when it points to a different symbol and there
        is exactly one prior, still-open BUY in the same symbol and tranche
        with enough quantity to cover that SELL.  Ambiguous rows remain
        untouched for manual review.
        """
        sql = """SELECT s.id, s.ord_no, s.symbol, s.step, s.qty, s.created_at, s.buy_id,
                         b.symbol AS buy_symbol, b.type AS buy_type
                  FROM trade_ledger s
                  LEFT JOIN trade_ledger b ON b.id=s.buy_id
                  WHERE s.account_id=? AND s.type='sell'
                    AND (b.id IS NULL OR b.account_id<>s.account_id
                         OR b.symbol<>s.symbol OR b.type<>'buy')"""
        args: list[str] = [self.account_id]
        if symbol:
            sql += " AND s.symbol=?"
            args.append(symbol)
        repaired: list[dict] = []
        for sell in self.db.execute(sql, args).fetchall():
            candidates = self.db.execute(
                """SELECT b.id, b.qty - COALESCE(SUM(CASE WHEN s.id<>? THEN s.qty ELSE 0 END), 0) AS available
                   FROM trade_ledger b
                   LEFT JOIN trade_ledger s ON s.account_id=b.account_id
                        AND s.symbol=b.symbol AND s.type='sell' AND s.buy_id=b.id
                   WHERE b.account_id=? AND b.symbol=? AND b.type='buy' AND b.step=?
                     AND b.created_at<=?
                   GROUP BY b.id, b.qty
                   HAVING available>=?
                   ORDER BY b.created_at DESC""",
                (sell["id"], self.account_id, sell["symbol"], sell["step"], sell["created_at"], sell["qty"]),
            ).fetchall()
            if len(candidates) != 1:
                continue
            new_buy_id = str(candidates[0]["id"])
            self.db.execute("UPDATE trade_ledger SET buy_id=? WHERE id=?", (new_buy_id, sell["id"]))
            repaired.append({"sellId": sell["id"], "ordNo": sell["ord_no"], "symbol": sell["symbol"],
                             "step": int(sell["step"]), "buyId": new_buy_id})
        self.db.commit()
        return repaired

    def repair_partial_sell_buy_links(self, symbol: str | None = None) -> int:
        """Repair legacy partial fills only when their order already has one explicit link.

        This never guesses a source lot: an order with no explicit linked fill
        remains untouched for manual review.
        """
        order_column = 'order_uid' if self.schema_version == 2 else 'ord_no'
        sql = f"""SELECT {order_column}, MIN(buy_id) AS buy_id
                 FROM trade_ledger
                 WHERE account_id=? AND type='sell' AND buy_id IS NOT NULL"""
        args: list[str] = [self.account_id]
        if symbol:
            sql += " AND symbol=?"
            args.append(symbol)
        sql += f" GROUP BY {order_column} HAVING COUNT(DISTINCT buy_id)=1"
        repaired = 0
        for row in self.db.execute(sql, args).fetchall():
            update_sql = f"""UPDATE trade_ledger SET buy_id=?
                            WHERE account_id=? AND {order_column}=? AND type='sell' AND buy_id IS NULL"""
            update_args: list = [row["buy_id"], self.account_id, row[order_column]]
            if symbol:
                update_sql += " AND symbol=?"
                update_args.append(symbol)
            repaired += int(self.db.execute(update_sql, update_args).rowcount or 0)
        self.db.commit()
        return repaired

    def _pending_from_row(self, row: sqlite3.Row) -> PendingOrder:
        order = PendingOrder(row['ord_no'], row['symbol'], row['side'], row['requested_qty'],
                             row['requested_price'], row['action'], row['step'], json.loads(row['meta_json']),
                             row['filled_qty'], row['created_at'], row['status'])
        if self.schema_version == 2:
            identity = OrderIdentityStore(self.db).get(row['order_uid'])
            if (identity.account_id, identity.ord_no, identity.symbol, identity.side) != (
                self.account_id, order.ord_no, order.symbol, order.side
            ):
                raise ValueError("Pending order identity linkage is inconsistent")
            order.order_uid = identity.order_uid
            order.broker_order_date = identity.broker_order_date
            order.identity_status = identity.identity_status
        return order


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)
