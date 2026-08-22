"""Durable order/fill state used by the account synchronizer.

The broker is the source of truth.  This store only records submitted order
intent and broker-confirmed fills, so it can safely rebuild in-memory strategy
state after a restart.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


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


class TradeLedgerStore:
    def __init__(self, path: str, account_id: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.account_id = account_id
        # WAL lets the dashboard's read-only reporting connection run beside
        # confirmed-fill writes without readers taking a blocking read lock.
        self.db = sqlite3.connect(path, timeout=1.0)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=1000")
        # Runtime recovery supplies this boundary. Historical rows stay in
        # SQLite for reports but cannot be re-attributed to a new lifecycle.
        self._lifecycle_started_at: str | None = None
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
        self.db.execute("""INSERT OR REPLACE INTO pending_orders
            (account_id,ord_no,symbol,side,requested_qty,requested_price,action,step,meta_json,filled_qty,status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,COALESCE((SELECT status FROM pending_orders WHERE account_id=? AND ord_no=?),'open'),?,?)""",
            (self.account_id, order.ord_no, order.symbol, order.side, order.requested_qty,
             order.requested_price, order.action, order.step, json.dumps(order.meta), order.filled_qty,
             self.account_id, order.ord_no, now, now))
        self.db.commit()

    def get_pending(self, ord_no: str) -> PendingOrder | None:
        row = self.db.execute("SELECT * FROM pending_orders WHERE account_id=? AND ord_no=?",
                              (self.account_id, ord_no)).fetchone()
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

    def record_fill(self, pending: PendingOrder, cumulative_qty: float, price: float, filled_at: str) -> dict | None:
        """Record only the newly-confirmed quantity from a cumulative broker value."""
        delta = max(0.0, cumulative_qty - pending.filled_qty)
        if delta <= 0:
            return None
        # A cumulative quantity makes the id stable across repeated REST polling.
        row_id = f"{'B' if pending.side == 'BUY' else 'S'}-{pending.ord_no}-{_num(cumulative_qty)}"
        buy_id = self._buy_id_for_sell_order(pending) if pending.side == 'SELL' else None
        self.db.execute("""INSERT OR IGNORE INTO trade_ledger
            (id,account_id,ord_no,symbol,type,step,filled_at,qty,price,buy_id,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (row_id, self.account_id, pending.ord_no, pending.symbol, pending.side.lower(), pending.step,
             filled_at, delta, price, buy_id, _now()))
        status = 'filled' if cumulative_qty >= pending.requested_qty else 'open'
        self.db.execute("UPDATE pending_orders SET filled_qty=?, status=?, updated_at=? WHERE account_id=? AND ord_no=?",
                        (cumulative_qty, status, _now(), self.account_id, pending.ord_no))
        self.db.commit()
        row = self.db.execute("SELECT * FROM trade_ledger WHERE id=?", (row_id,)).fetchone()
        return dict(row) if row else None

    def mark_cancelled(self, ord_no: str) -> None:
        """Close a broker-accepted cancellation without treating it as a fill."""
        self.db.execute(
            "UPDATE pending_orders SET status='cancelled', updated_at=? WHERE account_id=? AND ord_no=?",
            (_now(), self.account_id, ord_no),
        )
        self.db.commit()

    def mark_closed_unconfirmed(self, ord_no: str) -> None:
        """Stop retrying an order the broker says has no open quantity."""
        self.db.execute(
            "UPDATE pending_orders SET status='closed_unconfirmed', updated_at=? WHERE account_id=? AND ord_no=?",
            (_now(), self.account_id, ord_no),
        )
        self.db.commit()

    def mark_awaiting_execution_history(self, ord_no: str) -> None:
        """Keep a terminal broker order eligible for later fill recovery."""
        self.db.execute(
            "UPDATE pending_orders SET status='awaiting_execution_history', updated_at=? "
            "WHERE account_id=? AND ord_no=?",
            (_now(), self.account_id, ord_no),
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

    def has_unresolved_orders(self, symbol: str) -> bool:
        """True for any order that still needs broker fill attribution."""
        row = self.db.execute(
            "SELECT 1 FROM pending_orders WHERE account_id=? AND symbol=? "
            "AND (status IN ('open','awaiting_execution_history') "
            "OR (status='filled' AND filled_qty<=0)) LIMIT 1",
            (self.account_id, symbol),
        ).fetchone()
        return row is not None

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
        args: list = [self.account_id]
        if symbol:
            sql += " AND symbol=?"; args.append(symbol)
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
            sql += " AND symbol=?"; args.append(symbol)
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
        row = self.db.execute(
            "SELECT buy_id FROM trade_ledger WHERE account_id=? AND ord_no=? "
            "AND type='sell' AND buy_id IS NOT NULL ORDER BY created_at LIMIT 1",
            (self.account_id, pending.ord_no),
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
        sql = """SELECT ord_no, MIN(buy_id) AS buy_id
                 FROM trade_ledger
                 WHERE account_id=? AND type='sell' AND buy_id IS NOT NULL"""
        args: list[str] = [self.account_id]
        if symbol:
            sql += " AND symbol=?"; args.append(symbol)
        sql += " GROUP BY ord_no"
        repaired = 0
        for row in self.db.execute(sql, args).fetchall():
            update_sql = """UPDATE trade_ledger SET buy_id=?
                            WHERE account_id=? AND ord_no=? AND type='sell' AND buy_id IS NULL"""
            update_args: list = [row["buy_id"], self.account_id, row["ord_no"]]
            if symbol:
                update_sql += " AND symbol=?"; update_args.append(symbol)
            repaired += int(self.db.execute(update_sql, update_args).rowcount or 0)
        self.db.commit()
        return repaired

    @staticmethod
    def _pending_from_row(row: sqlite3.Row) -> PendingOrder:
        return PendingOrder(row['ord_no'], row['symbol'], row['side'], row['requested_qty'],
                            row['requested_price'], row['action'], row['step'], json.loads(row['meta_json']),
                            row['filled_qty'], row['created_at'], row['status'])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)
