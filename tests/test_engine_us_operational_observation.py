"""Operational observation receipts never reach economic fills or new orders."""
import asyncio
from unittest.mock import Mock

import pytest

from src.core import engine as engine_module
from tests.test_engine_us_observation_hook import (
    SinkDouble, factory as factory, history,
)
from tests.test_order_identity_runtime import add_order, dump
from tests.test_us_observation_startup import (
    opened, prepared as prepared,
    db as db, deny_network as deny_network, async_runner as async_runner,
)
from tests.test_us_operational_observation_store import count


@pytest.mark.parametrize("empty", [False, True])
def test_persisted_operational_cycle_preserves_ledger_and_order_block(factory, prepared, db, monkeypatch, empty, async_runner):
    session = opened(prepared)
    try:
        engine, client = factory(session.adapter)
        add_order(engine.ledger, "20261002")
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
        add_order(engine.ledger, "20261002")
        history(client)
        before = dump(engine.ledger)
        replacement = SinkDouble()
        session.adapter.sink = replacement
        assert not async_runner(engine.sync_broker_state(force_balance=True))
        assert engine._us_observation_blocks_order("AAPL")
        assert not replacement.calls and count(db) == 0 and dump(engine.ledger) == before
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
