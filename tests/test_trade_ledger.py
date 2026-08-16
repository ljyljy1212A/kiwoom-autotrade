from src.data.trade_ledger import PendingOrder, TradeLedgerStore


def test_partial_fill_is_idempotent_and_dashboard_shaped(tmp_path):
    store = TradeLedgerStore(str(tmp_path / "trades.db"), "a")
    pending = PendingOrder("42", "NVDA", "BUY", 5, 100, "BUY", 1, {})
    store.add_pending(pending)
    assert store.record_fill(pending, 2, 100, "2026-08-11")["qty"] == 2
    pending = store.get_pending("42")
    assert store.record_fill(pending, 2, 100, "2026-08-11") is None
    pending = store.get_pending("42")
    store.record_fill(pending, 5, 101, "2026-08-11")
    assert store.ledger_rows() == [
        {"id": "B-42-2", "type": "buy", "step": 1, "filledAt": "2026-08-11", "qty": 2.0, "price": 100.0},
        {"id": "B-42-5", "type": "buy", "step": 1, "filledAt": "2026-08-11", "qty": 3.0, "price": 101.0},
    ]
    store.close()


def test_backup_preserves_account_scoped_confirmed_fills(tmp_path):
    store = TradeLedgerStore(str(tmp_path / "trades.db"), "account-a")
    pending = PendingOrder("43", "SOXL", "BUY", 3, 20, "BUY", 1, {})
    store.add_pending(pending)
    store.record_fill(pending, 3, 20, "2026-08-12")

    backup_path = store.backup_to(tmp_path / "backup" / "startup.db")
    restored = TradeLedgerStore(str(backup_path), "account-a")

    assert backup_path.exists()
    assert restored.ledger_rows() == [
        {"id": "B-43-3", "type": "buy", "step": 1,
         "filledAt": "2026-08-12", "qty": 3.0, "price": 20.0},
    ]
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
    store.add_pending(foreign_buy); store.record_fill(foreign_buy, 5, 100, "2026-08-14")
    store.add_pending(local_buy); store.record_fill(local_buy, 5, 90, "2026-08-14")
    sell = PendingOrder("local-sell", "LOCAL", "SELL", 5, 95, "SELL", 2, {"sell_only_step": True})
    store.add_pending(sell); store.record_fill(sell, 5, 95, "2026-08-14")
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
