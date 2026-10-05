"""Operational observation receipts never reach economic fills or new orders."""
import asyncio
from unittest.mock import AsyncMock, Mock
import sqlite3

import pytest

from src.core import engine as engine_module
from tests.test_engine_us_observation_hook import (
    SinkDouble, history,
)
from src.core.account_manager import AccountContext
from src.core.engine import AccountEngine
from src.data.trade_ledger import TradeLedgerStore
from src.data.trade_ledger_migration import create_identity_ledger_copy
from src.strategy.base import PositionState
from src.strategy.infinite_grid import InfiniteGridStrategy
from tests.support.telegram_double import make_telegram_double
from tests.test_execution_row_skip_logging import _Client, _Logger, _config
from tests.test_order_identity_runtime import add_order, dump
from tests.test_us_observation_startup import (
    opened, prepared as prepared,
    db as db, deny_network as deny_network, async_runner as async_runner,
)
from tests.test_us_operational_observation_store import count


@pytest.fixture
def factory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(engine_module, "_ACCOUNT_BALANCE_GATES", {})
    engines = []

    def create(adapter):
        root = tmp_path / f"engine-{len(engines)}"
        root.mkdir()
        source = root / "legacy.db"
        TradeLedgerStore(source, "us_mock").close()
        candidate = root / "trades_us_mock.db"
        create_identity_ledger_copy(source, candidate, account_markets={"us_mock": "US"})
        writer = TradeLedgerStore(candidate, "us_mock", market="US")
        add_order(writer, "20261002")
        writer.close()
        monkeypatch.setattr(engine_module, "DATA_DIR", root)
        client = _Client([])
        client.market = "US"
        client.place_order = AsyncMock()
        ctx = AccountContext(account_id="us_mock", display_name="synthetic readonly observation",
                             client=client, strategy=InfiniteGridStrategy(dict(_config(), market="US", symbol="AAPL")),
                             risk_manager=None, dedup=None, logger=_Logger(), position=PositionState(symbol="AAPL"))
        engine = AccountEngine(ctx, make_telegram_double(), None, lambda _: None,
                               control_symbol="AAPL", us_observation_adapter=adapter)
        engine.execution_query_min_interval_sec = 0
        engine._apply_reconciliation_clear_event = AsyncMock()
        engine._apply_confirmed_fill = AsyncMock()
        engine._cancel_stale_orders = AsyncMock()
        engine._run_balance_reconciliation_cycle = AsyncMock(return_value=True)
        engines.append(engine)
        return engine, client

    yield create
    for engine in engines:
        engine.ledger.close()


@pytest.mark.parametrize("empty", [False, True])
def test_persisted_operational_cycle_preserves_ledger_and_order_block(factory, prepared, db, monkeypatch, empty, async_runner):
    session = opened(prepared)
    try:
        engine, client = factory(session.adapter)
        history(client, empty=empty)
        before = dump(engine.ledger)
        normalizer = Mock(side_effect=AssertionError("economic normalization must not run"))
        monkeypatch.setattr(engine_module, "normalize_us_execution_rows", normalizer)
        assert engine._us_observation_blocks_order("AAPL")
        assert not async_runner(engine.sync_broker_state(force_balance=True))
        assert count(db) == 1 and dump(engine.ledger) == before
        assert engine._balance_sync_blocked and engine._us_observation_blocks_order("AAPL")
        normalizer.assert_not_called()
        engine._apply_confirmed_fill.assert_not_awaited()
        engine._cancel_stale_orders.assert_not_awaited()
        engine._run_balance_reconciliation_cycle.assert_not_awaited()
        client.place_order.assert_not_awaited()
    finally:
        session.close()


def test_backend_replacement_cannot_remove_operational_order_block(factory, prepared, db, async_runner):
    session = opened(prepared)
    try:
        engine, client = factory(session.adapter)
        history(client)
        before = dump(engine.ledger)
        replacement = SinkDouble()
        session.adapter.sink = replacement
        assert not async_runner(engine.sync_broker_state(force_balance=True))
        assert engine._us_observation_blocks_order("AAPL")
        assert not replacement.calls and count(db) == 0 and dump(engine.ledger) == before
    finally:
        session.close()


def test_readonly_ledger_and_tick_reject_economic_mutation(factory, prepared, async_runner):
    session = opened(prepared)
    try:
        engine, _ = factory(session.adapter)
        before = dump(engine.ledger)
        with pytest.raises(sqlite3.OperationalError):
            engine.ledger.db.execute("DELETE FROM pending_orders")
        engine.sync_broker_state = AsyncMock(return_value=False)
        engine._refresh_runtime_control = Mock(side_effect=AssertionError("control refresh forbidden"))
        engine._refresh_dashboard_controls = AsyncMock(side_effect=AssertionError("control refresh forbidden"))
        async_runner(engine._tick())
        engine.sync_broker_state.assert_awaited_once()
        assert dump(engine.ledger) == before
    finally:
        session.close()


def test_run_skips_startup_economic_repair_and_control_refresh(factory, prepared, async_runner):
    session = opened(prepared)
    try:
        engine, _ = factory(session.adapter)
        engine._backup_ledger_at_startup = Mock(side_effect=AssertionError("backup forbidden"))
        engine._restore_from_ledger = Mock(side_effect=AssertionError("repair forbidden"))
        engine._refresh_runtime_control = Mock(side_effect=AssertionError("control forbidden"))
        engine._refresh_dashboard_controls = AsyncMock(side_effect=AssertionError("control forbidden"))
        engine.sync_broker_state = AsyncMock(return_value=False)
        engine._tick = AsyncMock()
        engine._heartbeat = Mock()
        engine._wait_for_next_tick_or_control_change = AsyncMock(side_effect=asyncio.CancelledError)
        with pytest.raises(asyncio.CancelledError):
            async_runner(engine.run())
        engine._restore_from_ledger.assert_not_called()
        engine._backup_ledger_at_startup.assert_not_called()
        engine._refresh_dashboard_controls.assert_not_awaited()
    finally:
        session.close()


def test_shutdown_drains_pending_sync_and_refuses_late_doorbells(factory, prepared, async_runner):
    session = opened(prepared)
    try:
        engine, _ = factory(session.adapter)

        async def exercise():
            entered = asyncio.Event()

            async def pending():
                entered.set()
                await asyncio.Future()

            task = asyncio.create_task(pending())
            engine._sync_task = task
            await entered.wait()
            await engine.stop_us_observation_tasks()
            assert task.cancelled()
            engine.request_sync()
            assert engine._sync_task is task

        async_runner(exercise())
    finally:
        session.close()
