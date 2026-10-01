import sqlite3

import pytest

from src.data.trade_ledger import FillQuantityExceededError, PendingOrder, TradeLedgerStore


def _economic_state(store):
    return (
        [dict(row) for row in store.db.execute("SELECT * FROM pending_orders ORDER BY account_id,ord_no")],
        [dict(row) for row in store.db.execute("SELECT * FROM trade_ledger ORDER BY id")],
    )


@pytest.mark.parametrize("price", [0, float("nan"), float("inf"), float("-inf"), "bad", None])
@pytest.mark.parametrize("observation_only", [False, True])
@pytest.mark.parametrize("corrupt_stored", [False, True])
def test_quantity_conflict_precedes_invalid_price(tmp_path, price, observation_only, corrupt_stored):
    path = str(tmp_path / "invalid-price-conflict.db")
    store = TradeLedgerStore(path, "account-a")
    order = PendingOrder("conflict", "NVDA", "BUY", 5, 100, "BUY", 3, {})
    store.add_pending(order)
    if corrupt_stored:
        store.db.execute("UPDATE pending_orders SET filled_qty=6 WHERE ord_no='conflict'")
        store.db.commit()
    before = _economic_state(store)
    with pytest.raises(FillQuantityExceededError):
        store.record_fill(order, 2 if corrupt_stored else 6, price, "2026-10-01",
                          observation_only=observation_only)
    assert _economic_state(store) == before
    assert store.quantity_conflict_order_ids("NVDA") == ("conflict",)
    assert not store.db.in_transaction
    store.close()
    reopened = TradeLedgerStore(path, "account-a")
    assert reopened.quantity_conflict_order_ids("NVDA") == ("conflict",)
    assert _economic_state(reopened) == before
    reopened.close()


@pytest.mark.parametrize("price", [0, float("nan"), "bad", None])
def test_quantity_observation_ignores_price_without_recording_fill(tmp_path, price):
    store = TradeLedgerStore(str(tmp_path / "observation.db"), "account-a")
    order = PendingOrder("normal", "NVDA", "BUY", 5, 100, "BUY", 3, {})
    store.add_pending(order)
    before = "\n".join(store.db.iterdump())
    assert store.record_fill(order, 2, price, "2026-10-01", observation_only=True) is None
    assert "\n".join(store.db.iterdump()) == before
    assert not store.db.in_transaction
    store.close()


def test_partial_fill_is_idempotent_and_dashboard_shaped(tmp_path):
    store = TradeLedgerStore(str(tmp_path / "trades.db"), "a")
    pending = PendingOrder("42", "NVDA", "BUY", 5, 100, "BUY", 1, {})
    store.add_pending(pending)
    assert store.record_fill(pending, 2, 100, "2026-08-11")["qty"] == 2
    pending = store.get_pending("42")
    assert store.record_fill(pending, 2, 100, "2026-08-11") is None
    pending = store.get_pending("42")
    store.record_fill(pending, 5, 101, "2026-08-11")
    rows = store.ledger_rows()
    assert [{key: row[key] for key in ("id", "type", "step", "filledAt", "qty", "price")} for row in rows] == [
        {"id": "B-42-2", "type": "buy", "step": 1, "filledAt": "2026-08-11", "qty": 2.0, "price": 100.0},
        {"id": "B-42-5", "type": "buy", "step": 1, "filledAt": "2026-08-11", "qty": 3.0, "price": 101.0},
    ]
    assert [(row["ord_no"], bool(row["created_at"])) for row in rows] == [("42", True), ("42", True)]
    store.close()


def test_cumulative_fills_use_database_quantity_with_a_stale_pending_snapshot(tmp_path):
    store = TradeLedgerStore(str(tmp_path / "trades.db"), "account-a")
    stale_pending = PendingOrder("42", "NVDA", "BUY", 5, 100, "BUY", 3, {})
    store.add_pending(stale_pending)

    first = store.record_fill(stale_pending, 2, 100, "2026-08-11")
    second = store.record_fill(stale_pending, 5, 101, "2026-08-11")
    duplicate = store.record_fill(stale_pending, 5, 101, "2026-08-11")

    assert first["qty"] == 2
    assert second["qty"] == 3
    assert duplicate is None
    assert store.get_pending("42").filled_qty == 5
    assert [row["qty"] for row in store.ledger_rows("NVDA")] == [2, 3]
    assert store.open_tranche_qty("NVDA", 3) == 5
    store.close()


def test_cancelled_order_stays_recoverable_through_partial_fill(tmp_path):
    store = TradeLedgerStore(str(tmp_path / "trades.db"), "account-a")
    order = PendingOrder("cancel-1", "NVDA", "BUY", 5, 100, "BUY", 3, {})
    store.add_pending(order)
    store.mark_awaiting_execution_history(order.ord_no)

    stale_order = store.get_pending(order.ord_no)
    first = store.record_fill(stale_order, 2, 100, "2026-08-11")
    current = store.get_pending(order.ord_no)

    assert first["qty"] == 2
    assert current.status == "awaiting_execution_history"
    assert [item.ord_no for item in store.execution_recovery_orders("NVDA")] == [order.ord_no]
    assert store.has_pending_buy("NVDA")

    second = store.record_fill(current, 5, 101, "2026-08-11")
    assert second["qty"] == 3
    assert store.get_pending(order.ord_no).status == "filled"
    assert store.execution_recovery_orders("NVDA") == []
    store.close()


@pytest.mark.parametrize("quantity,price", [
    (float("nan"), 100), (float("inf"), 100), (float("-inf"), 100), (-1, 100),
    (2, float("nan")), (2, float("inf")), (2, float("-inf")), (2, 0), (2, -100),
])
def test_invalid_fill_preserves_durable_order_and_ledger(tmp_path, quantity, price):
    path = str(tmp_path / "trades.db")
    store = TradeLedgerStore(path, "account-a")
    order = PendingOrder("invalid-fill", "NVDA", "BUY", 5, 100, "BUY", 3, {})
    try:
        store.add_pending(order)
        store.mark_awaiting_execution_history(order.ord_no)
        before = "\n".join(store.db.iterdump())
        with pytest.raises(ValueError):
            store.record_fill(order, quantity, price, "2026-10-01")
        assert "\n".join(store.db.iterdump()) == before
        assert not store.db.in_transaction
    finally:
        store.close()
    restored = TradeLedgerStore(path, "account-a")
    try:
        current = restored.get_pending(order.ord_no)
        assert current.status == "awaiting_execution_history"
        assert current.filled_qty == 0
        assert restored.ledger_rows("NVDA") == []
        assert restored.record_fill(current, 2, 100, "2026-10-01")["qty"] == 2
    finally:
        restored.close()


@pytest.mark.parametrize("column,value", [
    ("filled_qty", float("inf")), ("filled_qty", -1),
    ("requested_qty", float("inf")), ("requested_qty", 0),
])
def test_invalid_stored_quantity_rolls_back_fill(tmp_path, column, value):
    store = TradeLedgerStore(str(tmp_path / "trades.db"), "account-a")
    order = PendingOrder("invalid-counter", "NVDA", "BUY", 5, 100, "BUY", 3, {})
    try:
        store.add_pending(order)
        # Columns come only from this fixed parameter list.
        store.db.execute(f"UPDATE pending_orders SET {column}=? WHERE ord_no=?",
                         (value, order.ord_no))
        store.db.commit()
        before = "\n".join(store.db.iterdump())
        with pytest.raises(ValueError):
            store.record_fill(order, 2, 100, "2026-10-01")
        assert "\n".join(store.db.iterdump()) == before
        assert not store.db.in_transaction
    finally:
        store.close()


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("status", ["open", "awaiting_execution_history"])
def test_excess_fill_preserves_partial_fill_and_latches_after_reopen(tmp_path, side, status):
    path = str(tmp_path / "trades.db")
    store = TradeLedgerStore(path, "account-a")
    order = PendingOrder("excess-fill", "NVDA", side, 5, 100, side, 3, {})
    try:
        store.add_pending(order)
        if status == "awaiting_execution_history":
            store.mark_awaiting_execution_history(order.ord_no)
        assert store.record_fill(order, 2, 100, "2026-10-01")["qty"] == 2
        before = _economic_state(store)
        with pytest.raises(FillQuantityExceededError) as caught:
            store.record_fill(order, 6, 101, "2026-10-01")
        assert caught.value.requested_qty == 5
        assert caught.value.filled_qty == 2
        assert caught.value.cumulative_qty == 6
        assert _economic_state(store) == before
        assert store.quantity_conflict_order_ids("NVDA") == (order.ord_no,)
        assert not store.db.in_transaction
    finally:
        store.close()
    restored = TradeLedgerStore(path, "account-a")
    try:
        current = restored.get_pending(order.ord_no)
        assert current.filled_qty == 2
        assert current.status == status
        assert restored.quantity_conflict_order_ids("NVDA") == (order.ord_no,)
        assert restored.record_fill(current, 5, 101, "2026-10-01")["qty"] == 3
        assert restored.record_fill(current, 5, 101, "2026-10-01") is None
        assert restored.get_pending(order.ord_no).status == "filled"
        assert [row["qty"] for row in restored.ledger_rows("NVDA")] == [2, 3]
        assert restored.has_unresolved_orders("NVDA")
        restored.set_lifecycle_started_at("2099-01-01")
        assert restored.quantity_conflict_order_ids("NVDA") == (order.ord_no,)
    finally:
        restored.close()


@pytest.mark.parametrize("stored_request,observed,rejected", [(5, 6, True), (7, 6, False)])
def test_excess_fill_check_uses_database_request_not_stale_snapshot(
    tmp_path, stored_request, observed, rejected,
):
    store = TradeLedgerStore(str(tmp_path / "trades.db"), "account-a")
    stale = PendingOrder("stale-request", "NVDA", "BUY", 100 if rejected else 5, 100, "BUY", 3, {})
    try:
        store.add_pending(stale)
        store.db.execute(
            "UPDATE pending_orders SET requested_qty=? WHERE ord_no=?",
            (stored_request, stale.ord_no),
        )
        store.db.commit()
        before = _economic_state(store)
        if rejected:
            with pytest.raises(FillQuantityExceededError):
                store.record_fill(stale, observed, 100, "2026-10-01")
            assert _economic_state(store) == before
            assert store.quantity_conflict_order_ids("NVDA") == (stale.ord_no,)
        else:
            assert store.record_fill(stale, observed, 100, "2026-10-01")["qty"] == observed
            assert store.get_pending(stale.ord_no).status == "open"
        assert not store.db.in_transaction
    finally:
        store.close()


@pytest.mark.parametrize("observed", [2, 6, 7])
def test_stored_excess_fill_is_rejected_before_duplicate_check(tmp_path, observed):
    store = TradeLedgerStore(str(tmp_path / "trades.db"), "account-a")
    order = PendingOrder("corrupt-excess", "NVDA", "BUY", 5, 100, "BUY", 3, {})
    try:
        store.add_pending(order)
        store.db.execute("UPDATE pending_orders SET filled_qty=6 WHERE ord_no=?", (order.ord_no,))
        store.db.commit()
        before = _economic_state(store)
        with pytest.raises(FillQuantityExceededError) as caught:
            store.record_fill(order, observed, 100, "2026-10-01")
        assert caught.value.reason == "stored_fill_exceeds_requested_quantity"
        assert _economic_state(store) == before
        assert store.quantity_conflict_order_ids("NVDA") == (order.ord_no,)
        assert not store.db.in_transaction
    finally:
        store.close()


def test_quantity_conflict_is_idempotent_and_scoped_to_account_and_symbol(tmp_path):
    path = str(tmp_path / "trades.db")
    store = TradeLedgerStore(path, "account-a")
    other = TradeLedgerStore(path, "account-b")
    order = PendingOrder("same-id", "NVDA", "BUY", 5, 100, "BUY", 3, {})
    try:
        store.add_pending(order)
        other.add_pending(order)
        for quantity in (6, 7):
            with pytest.raises(FillQuantityExceededError):
                store.record_fill(order, quantity, 100, "2026-10-01")
        rows = store.db.execute("SELECT * FROM execution_quantity_conflicts").fetchall()
        assert len(rows) == 1
        assert rows[0]["observed_qty"] == 6
        assert store.quantity_conflict_order_ids("NVDA") == ("same-id",)
        assert store.quantity_conflict_order_ids("AAPL") == ()
        assert other.quantity_conflict_order_ids("NVDA") == ()
        store.mark_cancelled(order.ord_no)
        assert store.has_unresolved_orders("NVDA")
    finally:
        other.close()
        store.close()


def test_conflict_write_failure_rolls_back_without_changing_economic_state(tmp_path):
    store = TradeLedgerStore(str(tmp_path / "trades.db"), "account-a")
    order = PendingOrder("write-failed", "NVDA", "BUY", 5, 100, "BUY", 3, {})
    try:
        store.add_pending(order)
        store.db.execute("""CREATE TRIGGER reject_quantity_conflict
            BEFORE INSERT ON execution_quantity_conflicts
            BEGIN SELECT RAISE(ABORT, 'diagnostic write blocked'); END""")
        store.db.commit()
        before = "\n".join(store.db.iterdump())
        with pytest.raises(sqlite3.IntegrityError, match="diagnostic write blocked"):
            store.record_fill(order, 6, 100, "2026-10-01")
        assert "\n".join(store.db.iterdump()) == before
        assert not store.db.in_transaction
    finally:
        store.close()


def test_quantity_conflict_schema_upgrade_preserves_existing_order(tmp_path):
    path = str(tmp_path / "trades.db")
    store = TradeLedgerStore(path, "account-a")
    order = PendingOrder("legacy", "NVDA", "BUY", 5, 100, "BUY", 3, {})
    try:
        store.add_pending(order)
        store.db.execute("DROP TABLE execution_quantity_conflicts")
        store.db.commit()
    finally:
        store.close()
    reopened = TradeLedgerStore(path, "account-a")
    try:
        assert reopened.get_pending("legacy").requested_qty == 5
        assert reopened.quantity_conflict_order_ids("NVDA") == ()
        assert reopened.has_unresolved_orders("NVDA")
    finally:
        reopened.close()


def test_backup_preserves_account_scoped_confirmed_fills(tmp_path):
    store = TradeLedgerStore(str(tmp_path / "trades.db"), "account-a")
    pending = PendingOrder("43", "SOXL", "BUY", 3, 20, "BUY", 1, {})
    store.add_pending(pending)
    store.record_fill(pending, 3, 20, "2026-08-12")

    backup_path = store.backup_to(tmp_path / "backup" / "startup.db")
    restored = TradeLedgerStore(str(backup_path), "account-a")

    assert backup_path.exists()
    rows = restored.ledger_rows()
    assert [{key: row[key] for key in ("id", "type", "step", "filledAt", "qty", "price")} for row in rows] == [
        {"id": "B-43-3", "type": "buy", "step": 1,
         "filledAt": "2026-08-12", "qty": 3.0, "price": 20.0},
    ]
    assert rows[0]["ord_no"] == "43"
    assert rows[0]["created_at"]
    restored.close()
    store.close()


def test_partial_sell_fills_share_buy_link_and_legacy_rows_are_repaired(tmp_path):
    store = TradeLedgerStore(str(tmp_path / "trades.db"), "account-a")
    buy = PendingOrder("buy-1", "TEST", "BUY", 12, 100, "BUY", 5, {})
    store.add_pending(buy)
    store.record_fill(buy, 12, 100, "2026-08-12")
    sell = PendingOrder("sell-1", "TEST", "SELL", 12, 110, "SELL", 5, {})
    store.add_pending(sell)
    store.record_fill(sell, 1, 110, "2026-08-12")
    sell = store.get_pending("sell-1")
    store.record_fill(sell, 12, 110, "2026-08-12")
    links = [row["buyId"] for row in store.ledger_rows("TEST") if row["type"] == "sell"]
    assert links == ["B-buy-1-12", "B-buy-1-12"]

    store.db.execute("UPDATE trade_ledger SET buy_id=NULL WHERE id='S-sell-1-12'")
    store.db.commit()
    assert store.repair_partial_sell_buy_links("TEST") == 1
    assert [row["buyId"] for row in store.ledger_rows("TEST") if row["type"] == "sell"] == [
        "B-buy-1-12", "B-buy-1-12"
    ]
    store.close()


def test_sell_link_is_symbol_scoped_and_invalid_legacy_link_is_repaired(tmp_path):
    store = TradeLedgerStore(str(tmp_path / "trades.db"), "account-a")
    foreign_buy = PendingOrder("foreign-buy", "OTHER", "BUY", 5, 100, "BUY", 2, {})
    local_buy = PendingOrder("local-buy", "LOCAL", "BUY", 5, 90, "BUY", 2, {})
    store.add_pending(foreign_buy)
    store.record_fill(foreign_buy, 5, 100, "2026-08-14")
    store.add_pending(local_buy)
    store.record_fill(local_buy, 5, 90, "2026-08-14")
    sell = PendingOrder("local-sell", "LOCAL", "SELL", 5, 95, "SELL", 2, {"sell_only_step": True})
    store.add_pending(sell)
    store.record_fill(sell, 5, 95, "2026-08-14")
    row = next(row for row in store.ledger_rows("LOCAL") if row["type"] == "sell")
    assert row["buyId"] == "B-local-buy-5"

    store.db.execute("UPDATE trade_ledger SET buy_id=? WHERE id=?", ("B-foreign-buy-5", row["id"]))
    store.db.commit()
    assert store.repair_cross_symbol_sell_buy_links("LOCAL") == [{
        "sellId": row["id"], "ordNo": "local-sell", "symbol": "LOCAL", "step": 2, "buyId": "B-local-buy-5",
    }]
    repaired = next(row for row in store.ledger_rows("LOCAL") if row["type"] == "sell")
    assert repaired["buyId"] == "B-local-buy-5"
    store.close()


def test_pending_sell_guard_is_scoped_to_symbol_and_tranche(tmp_path):
    store = TradeLedgerStore(str(tmp_path / "trades.db"), "account-a")
    store.add_pending(PendingOrder("sell-2", "LOCAL", "SELL", 5, 100, "SELL", 2, {}))
    assert store.has_pending_sell("LOCAL", 2)
    assert not store.has_pending_sell("LOCAL", 3)
    assert not store.has_pending_sell("OTHER", 2)
    store.mark_awaiting_execution_history("sell-2")
    assert store.has_pending_sell("LOCAL", 2)
    store.close()


def test_completed_observation_is_account_and_symbol_scoped_across_lifecycle(tmp_path):
    path = str(tmp_path / "completed.db")
    store = TradeLedgerStore(path, "account-a")
    other = TradeLedgerStore(path, "account-b")
    for ledger, symbol, ord_no in ((store, "SOXL", "a"), (store, "AAPL", "b"),
                                  (other, "SOXL", "c")):
        order = PendingOrder(ord_no, symbol, "BUY", 3, 20, "BUY", 1, {})
        ledger.add_pending(order)
        ledger.record_fill(order, 3, 20, "2026-10-01")
    store.set_lifecycle_started_at("2099-01-01")
    assert [order.ord_no for order in store.completed_orders_for_execution_observation("SOXL")] == ["a"]
    assert not store.has_unresolved_orders("SOXL")
    other.close()
    store.close()


def test_observation_only_cannot_apply_delta_from_stale_completed_snapshot(tmp_path):
    store = TradeLedgerStore(str(tmp_path / "observation.db"), "account-a")
    order = PendingOrder("done", "SOXL", "BUY", 5, 20, "BUY", 1, {})
    store.add_pending(order)
    store.record_fill(order, 5, 20, "2026-10-01")
    snapshot = store.get_pending(order.ord_no)
    store.db.execute("UPDATE pending_orders SET requested_qty=10 WHERE ord_no='done'")
    store.db.commit()
    before = _economic_state(store)
    assert store.record_fill(snapshot, 6, 20, "2026-10-01", observation_only=True) is None
    assert _economic_state(store) == before
    store.db.execute("UPDATE pending_orders SET requested_qty=4 WHERE ord_no='done'")
    store.db.commit()
    before = _economic_state(store)
    with pytest.raises(FillQuantityExceededError):
        store.record_fill(snapshot, 4, 20, "2026-10-01", observation_only=True)
    assert _economic_state(store) == before
    assert store.quantity_conflict_order_ids("SOXL") == ("done",)
    store.close()
