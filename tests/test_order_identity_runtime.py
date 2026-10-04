"""UID-aware ledger and engine integration using synthetic broker evidence only."""
import asyncio
from unittest.mock import AsyncMock

import pytest

import src.core.engine as engine_module
from src.core.account_manager import AccountContext
from src.core.engine import AccountEngine
from src.data.order_identity import IdentityConflictError
from src.data.trade_ledger import FillQuantityExceededError, PendingOrder, TradeLedgerStore
from src.data.trade_ledger_migration import create_identity_ledger_copy
from src.strategy.base import PositionState
from src.strategy.infinite_grid import InfiniteGridStrategy
from tests.support.telegram_double import make_telegram_double
from tests.test_execution_row_skip_logging import _Client, _Logger, _config

EVIDENCE = {"source_tr": "synthetic", "record_ref": "fixture-only",
            "verified_at_utc": "2026-10-03T00:00:00+00:00"}


def identity_store(tmp_path):
    source = tmp_path / "empty_legacy.db"
    legacy = TradeLedgerStore(str(source), "synthetic")
    legacy.close()
    candidate = tmp_path / "candidate.db"
    create_identity_ledger_copy(source, candidate, account_markets={"synthetic": "US"})
    return TradeLedgerStore(str(candidate), "synthetic", market="US")


def add_order(store, date=None, *, side="BUY", step=1, number="000000042"):
    order = PendingOrder(number, "AAPL", side, 5, 100, side, step, {})
    store.add_pending(order)
    if date:
        store.confirm_us_order_date(order.order_uid, date, evidence=EVIDENCE)
    return store.get_pending(number, order_uid=order.order_uid)


def dump(store):
    return "\n".join(store.db.iterdump())


def test_reused_order_number_keeps_fills_and_status_separate(tmp_path):
    store = identity_store(tmp_path)
    try:
        first = add_order(store, "20261001")
        second = add_order(store, "20261002")
        assert first.order_uid != second.order_uid
        a = store.record_fill(first, 2, 100, "2026-10-03", execution_date="20261003")
        b = store.record_fill(second, 3, 101, "2026-10-03", execution_date="20261003")
        assert a["id"] != b["id"]
        assert a["order_uid"] == first.order_uid
        assert b["order_uid"] == second.order_uid
        assert a["execution_date_status"] == "broker_confirmed"
        store.mark_awaiting_execution_history(first.ord_no, order_uid=first.order_uid)
        assert store.get_pending(first.ord_no, order_uid=first.order_uid).status == "awaiting_execution_history"
        assert store.get_pending(second.ord_no, order_uid=second.order_uid).status == "open"
        before = dump(store)
        for operation in (store.get_pending, store.mark_cancelled, store.mark_closed_unconfirmed,
                          store.mark_awaiting_execution_history):
            with pytest.raises(ValueError, match="order_uid"):
                operation(first.ord_no)
        assert dump(store) == before
        assert not store.db.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        store.close()


def test_new_intent_and_identity_are_atomic_and_cannot_replace(tmp_path):
    store = identity_store(tmp_path)
    try:
        order = add_order(store)
        before = dump(store)
        with pytest.raises(ValueError, match="cannot be replaced"):
            store.add_pending(order)
        assert dump(store) == before
        store.db.execute("CREATE TRIGGER deny_pending BEFORE INSERT ON pending_orders "
                         "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        store.db.commit()
        before = dump(store)
        import sqlite3
        failed = PendingOrder("new", "AAPL", "BUY", 5, 100, "BUY", 1, {})
        with pytest.raises(sqlite3.IntegrityError):
            store.add_pending(failed)
        assert failed.order_uid is None
        assert dump(store) == before
        assert not store.db.in_transaction
    finally:
        store.close()


@pytest.mark.parametrize("date,stamp", [(None, "2026-10-03"), ("20260230", "2026-02-30"),
                                        ("20261003", "2026-10-02")])
def test_economic_fill_requires_distinct_authoritative_execution_date(tmp_path, date, stamp):
    store = identity_store(tmp_path)
    try:
        order = add_order(store, "20261001")
        before = dump(store)
        with pytest.raises(ValueError):
            store.record_fill(order, 2, 100, stamp, execution_date=date)
        assert dump(store) == before
        assert not store.db.in_transaction
    finally:
        store.close()


def test_quantity_conflict_is_uid_scoped_and_precedes_date_price_checks(tmp_path):
    store = identity_store(tmp_path)
    try:
        first, second = add_order(store, "20261001"), add_order(store, "20261002")
        with pytest.raises(FillQuantityExceededError):
            store.record_fill(first, 6, float("nan"), "unknown")
        conflicts = store.db.execute("SELECT order_uid FROM execution_quantity_conflicts").fetchall()
        assert [row[0] for row in conflicts] == [first.order_uid]
        assert store.get_pending(second.ord_no, order_uid=second.order_uid).filled_qty == 0
        before = dump(store)
        with pytest.raises(ValueError, match="conflict"):
            store.record_fill(second, 2, 100, "2026-10-03", execution_date="20261003")
        assert dump(store) == before
    finally:
        store.close()


def test_identity_conflict_survives_cancelled_status_and_blocks_clearance(tmp_path):
    store = identity_store(tmp_path)
    try:
        first, second = add_order(store, "20261001"), add_order(store)
        with pytest.raises(IdentityConflictError):
            store.confirm_us_order_date(second.order_uid, "20261001", evidence=EVIDENCE)
        for order in (first, second):
            store.mark_cancelled(order.ord_no, order_uid=order.order_uid)
        assert store.pending_orders("AAPL") == []
        assert set(store.identity_conflict_order_ids("AAPL")) == {first.order_uid, second.order_uid}
        assert store.has_unresolved_orders("AAPL")
    finally:
        store.close()


def build_engine(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(engine_module, "DATA_DIR", tmp_path / "data")
    config = _config()
    config.update(symbol="AAPL", market="US")
    client = _Client([])
    client.market = "US"
    ctx = AccountContext(account_id="synthetic", display_name="synthetic identity",
                         client=client, strategy=InfiniteGridStrategy(config), risk_manager=None,
                         dedup=None, logger=_Logger(), position=PositionState(symbol="AAPL"))
    engine = AccountEngine(ctx, make_telegram_double(), None, lambda _: None,
                           poll_interval_sec=60, control_symbol="AAPL")
    engine.ledger.close()
    engine.ledger = identity_store(tmp_path)
    engine.execution_query_min_interval_sec = 0
    engine._apply_confirmed_fill = AsyncMock()
    engine._cancel_stale_orders = AsyncMock()
    engine._run_balance_reconciliation_cycle = AsyncMock(return_value=True)
    return engine, client


@pytest.mark.parametrize("fault", ["undocumented_execution_date", "official_fields_only", "second_query_fails", "date_conflict", "second_date_missing", "second_date_conflict"])
def test_engine_blocks_unverified_us_execution_dates_across_all_query_dates(tmp_path, monkeypatch, fault):
    engine, client = build_engine(tmp_path, monkeypatch)
    try:
        first = add_order(engine.ledger, "20261001")
        add_order(engine.ledger, "20261002")
        calls = []

        async def history(symbol, *, order_date):
            calls.append((symbol, order_date))
            if fault == "second_query_fails" and order_date == "20261002":
                raise ValueError("synthetic incomplete pages")
            row = {"ord_no": first.ord_no, "cntr_qty": "2", "cntr_uv": "100", "cntr_time": "21:04:41"}
            if fault != "official_fields_only":
                row["cntr_dt"] = "20261003"
            if fault == "second_date_missing" and order_date == "20261002":
                row.pop("cntr_dt")
            if fault == "date_conflict" or (fault == "second_date_conflict" and order_date == "20261002"):
                row["ord_dt"] = "20260930"
            return {"result_list": [row], "_execution_pages_complete": True}

        client.get_executed_orders = history
        before = dump(engine.ledger)
        success = asyncio.run(engine.sync_broker_state(force_balance=True))
        assert calls == [("AAPL", "20261001"), ("AAPL", "20261002")]
        assert not success
        assert dump(engine.ledger) == before
        assert engine.ctx.position.qty == 0
        assert engine._balance_sync_blocked
        engine._apply_confirmed_fill.assert_not_awaited()
        engine._cancel_stale_orders.assert_not_awaited()
        engine._run_balance_reconciliation_cycle.assert_not_awaited()
    finally:
        engine.ledger.close()


def test_v2_partial_sell_links_never_cross_reused_order_numbers(tmp_path):
    store = identity_store(tmp_path)
    try:
        buy_ids = []
        for step in (1, 2):
            buy = add_order(store, "20261001", step=step, number=f"BUY{step}")
            buy_ids.append(store.record_fill(buy, 5, 100, "2026-10-03", execution_date="20261003")["id"])
        sell1 = add_order(store, "20261001", side="SELL", step=1, number="SELL")
        sell2 = add_order(store, "20261002", side="SELL", step=2, number="SELL")
        for order, buy_id in zip((sell1, sell2), buy_ids):
            for qty in (1, 2):
                row = store.record_fill(order, qty, 101, "2026-10-03", execution_date="20261003")
                assert row["buy_id"] == buy_id
        assert store.repair_partial_sell_buy_links("AAPL") == 0
    finally:
        store.close()


def test_v2_fill_write_failure_rolls_back_trade_and_pending(tmp_path):
    import sqlite3
    store = identity_store(tmp_path)
    try:
        order = add_order(store, "20261001")
        store.db.execute("CREATE TRIGGER deny_fill_update BEFORE UPDATE ON pending_orders "
                         "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        store.db.commit()
        before = dump(store)
        with pytest.raises(sqlite3.IntegrityError):
            store.record_fill(order, 2, 100, "2026-10-03", execution_date="20261003")
        assert dump(store) == before
        assert not store.db.in_transaction
    finally:
        store.close()


def test_uid_cannot_mutate_another_account(tmp_path):
    store = identity_store(tmp_path)
    try:
        order = add_order(store)
        path = store.db.execute("PRAGMA database_list").fetchone()[2]
        other = TradeLedgerStore(path, "other-synthetic", market="US")
        try:
            before = dump(store)
            with pytest.raises(ValueError, match="another account"):
                other.confirm_us_order_date(order.order_uid, "20261001", evidence=EVIDENCE)
            assert other.get_pending(order.ord_no, order_uid=order.order_uid) is None
            with pytest.raises(ValueError, match="No pending order"):
                other.record_fill(order, 2, 100, "2026-10-03", execution_date="20261003")
            assert dump(store) == before
        finally:
            other.close()
    finally:
        store.close()


def test_engine_legacy_us_order_submission_is_blocked_before_broker(tmp_path, monkeypatch):
    from src.strategy.base import Action, OrderIntent
    engine, client = build_engine(tmp_path, monkeypatch)
    try:
        engine.ledger.close()
        engine.ledger = TradeLedgerStore(str(tmp_path / "legacy_only.db"), "synthetic", market="US")
        client.place_order = AsyncMock()
        before = dump(engine.ledger)
        asyncio.run(engine._execute_order(OrderIntent(Action.BUY, "AAPL", 1, 100)))
        client.place_order.assert_not_awaited()
        assert dump(engine.ledger) == before
    finally:
        engine.ledger.close()


def test_engine_does_not_cancel_us_orders_without_dated_contract(tmp_path, monkeypatch):
    engine, client = build_engine(tmp_path, monkeypatch)
    try:
        add_order(engine.ledger, "20261001")
        client.cancel_order = AsyncMock()
        before = dump(engine.ledger)
        asyncio.run(AccountEngine._cancel_stale_orders(engine))
        client.cancel_order.assert_not_awaited()
        assert dump(engine.ledger) == before
    finally:
        engine.ledger.close()


def test_passive_clearance_retains_uid_and_identity_conflicts(tmp_path):
    from src.core.engine import _ReadOnlyClearanceLedger
    store = identity_store(tmp_path)
    try:
        first, second = add_order(store, "20261001"), add_order(store)
        path = store.db.execute("PRAGMA database_list").fetchone()[2]
        with pytest.raises(IdentityConflictError):
            store.confirm_us_order_date(second.order_uid, "20261001", evidence=EVIDENCE)
        before = dump(store)
        passive = _ReadOnlyClearanceLedger(__import__("pathlib").Path(path), "synthetic")
        try:
            assert {r.order_uid for r in passive.pending_orders("AAPL")} == {first.order_uid, second.order_uid}
            assert set(passive.identity_conflict_order_ids("AAPL")) == {first.order_uid, second.order_uid}
        finally:
            passive.close()
        assert dump(store) == before
    finally:
        store.close()


def test_completed_us_history_needs_no_execution_date_for_observation(tmp_path, monkeypatch):
    engine, client = build_engine(tmp_path, monkeypatch)
    try:
        order = add_order(engine.ledger, "20261001")
        engine.ledger.record_fill(order, 5, 100, "2026-10-03", execution_date="20261003")
        client.rows = [{"ord_no": order.ord_no, "cntr_qty": "5", "cntr_uv": "100"}]
        before = dump(engine.ledger)
        assert asyncio.run(engine.sync_broker_state(force_balance=True))
        assert dump(engine.ledger) == before
        engine._apply_confirmed_fill.assert_not_awaited()
    finally:
        engine.ledger.close()
