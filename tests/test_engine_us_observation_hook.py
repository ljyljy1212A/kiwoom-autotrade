"""Injected Engine hook fixtures; no runtime worker, network or order delivery."""
import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import src.core.engine as engine_module
from src.core.account_manager import AccountContext
from src.core.engine import AccountEngine
from src.core.us_observation_interface import UsObservationAdapter, UsObservationReceipt
from src.data.trade_ledger import TradeLedgerStore
from src.data.trade_ledger_migration import create_identity_ledger_copy
from src.strategy.base import Action, OrderIntent, PositionState
from src.strategy.infinite_grid import InfiniteGridStrategy
from tests.support.telegram_double import make_telegram_double
from tests.test_execution_row_skip_logging import _Client, _Logger, _config
from tests.test_order_identity_runtime import add_order, dump
from tests.test_us_synthetic_response_adapter import response


class SinkDouble:
    def __init__(self, reply=None, error=None):
        self.calls, self.reply, self.error = [], reply, error

    def record_cycle(self, cycle):
        self.calls.append(cycle)
        if self.error:
            raise self.error
        if self.reply:
            return self.reply(cycle)
        return UsObservationReceipt(cycle.cycle_token, "OBSERVED", True)


@pytest.fixture
def factory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(engine_module, "_ACCOUNT_BALANCE_GATES", {})
    engines = []

    def create(adapter=None, *, symbol="AAPL"):
        root = tmp_path / f"engine-{len(engines)}"
        root.mkdir()
        source = root / "empty.db"
        legacy = TradeLedgerStore(source, "us_mock")
        legacy.close()
        candidate = root / "identity.db"
        create_identity_ledger_copy(source, candidate, account_markets={"us_mock": "US"})
        monkeypatch.setattr(engine_module, "DATA_DIR", root / "data")
        config = dict(_config(), symbol=symbol, market="US")
        client = _Client([])
        client.market = "US"
        client.place_order = AsyncMock()
        ctx = AccountContext(account_id="us_mock", display_name="scratch observation fixture",
                             client=client, strategy=InfiniteGridStrategy(config), risk_manager=None,
                             dedup=None, logger=_Logger(), position=PositionState(symbol=symbol))
        engine = AccountEngine(ctx, make_telegram_double(), None, lambda _: None,
                               control_symbol=symbol, us_observation_adapter=adapter)
        engine.ledger.close()
        engine.ledger = TradeLedgerStore(candidate, "us_mock", market="US")
        engine.execution_query_min_interval_sec = 0
        engine._apply_reconciliation_clear_event = AsyncMock()
        engine._apply_confirmed_fill = AsyncMock()
        engine._cancel_stale_orders = AsyncMock()
        engine._run_balance_reconciliation_cycle = AsyncMock(return_value=True)
        engines.append(engine)
        return engine, client

    try:
        yield create
    finally:
        for engine in engines:
            engine.ledger.close()


def adapter(sink):
    return UsObservationAdapter(account_id="us_mock", market="US", enabled=True, sink=sink)


def history(client, *, quantity="2", average="100.0000", empty=False):
    calls = []

    async def executed(symbol, *, order_date):
        calls.append((symbol, order_date))
        data = dict(response(quantity, average), _query_order_date=order_date)
        if empty:
            data["result_list"] = []
        return data

    client.get_executed_orders = executed
    return calls


@pytest.mark.parametrize("injected", [False, True])
def test_default_or_disabled_hook_retains_existing_path(factory, injected):
    sink = SinkDouble(error=AssertionError("disabled sink must not run"))
    observer = UsObservationAdapter(account_id="us_mock", market="US", sink=sink) if injected else None
    engine, client = factory(observer)
    add_order(engine.ledger, "20261002")
    history(client, empty=True)
    assert asyncio.run(engine.sync_broker_state(force_balance=True))
    assert not sink.calls and not engine._balance_gate.us_observation_states
    assert not engine._us_observation_blocks_order("AAPL")
    if observer:
        observer.enabled = True
        assert not engine._us_observation_active()


def test_raw_all_dates_observed_once_before_float_normalization(factory, monkeypatch):
    sink = SinkDouble()
    engine, client = factory(adapter(sink))
    first = add_order(engine.ledger, "20261001")
    add_order(engine.ledger, "20261002")
    calls = history(client)
    original = engine_module.normalize_us_execution_rows
    normalized = []

    def normalize(data, *, query_order_date):
        assert len(calls) == 2 and len(sink.calls) == 1
        normalized.append(query_order_date)
        return original(data, query_order_date=query_order_date)

    monkeypatch.setattr(engine_module, "normalize_us_execution_rows", normalize)
    before = dump(engine.ledger)
    # Observing the average does not satisfy the legacy actual-execution-date gate.
    assert not asyncio.run(engine.sync_broker_state(force_balance=True))
    assert calls == [("AAPL", "20261001"), ("AAPL", "20261002")]
    assert normalized == ["20261001", "20261002"] and len(sink.calls) == 1
    observed = sink.calls[0]
    assert {item.order_uid for item in observed.bindings} == {
        order.order_uid for order in engine.ledger.pending_orders("AAPL")
    }
    assert first.order_uid in {item.order_uid for item in observed.bindings}
    assert observed.observations[0].amount == Decimal(200)
    assert '"cntr_uv": "100.0000"' in observed.observations[0].raw_json
    assert dump(engine.ledger) == before and engine._us_observation_blocks_order("AAPL")
    engine._apply_confirmed_fill.assert_not_awaited()
    engine._cancel_stale_orders.assert_not_awaited()


@pytest.mark.parametrize("fault", ["missing_sink", "exception", "bad_receipt", "conflict"])
def test_observation_failure_precedes_legacy_writes_and_cancellation(factory, monkeypatch, fault):
    sink = SinkDouble()
    if fault == "exception":
        sink.error = RuntimeError("sensitive synthetic error")
    elif fault == "bad_receipt":
        sink.reply = lambda c: UsObservationReceipt("wrong", "OBSERVED", True)
    elif fault == "conflict":
        sink.reply = lambda c: UsObservationReceipt(c.cycle_token, "CONFLICT", True,
                                                   ((c.bindings[0].order_uid, "synthetic_conflict"),))
    engine, client = factory(adapter(None if fault == "missing_sink" else sink))
    add_order(engine.ledger, "20261002")
    calls = history(client)
    before = dump(engine.ledger)
    writes = Mock(side_effect=AssertionError("legacy write must not run"))
    monkeypatch.setattr(engine.ledger, "record_fill", writes)
    normalizer = Mock(side_effect=AssertionError("normalization must not run"))
    monkeypatch.setattr(engine_module, "normalize_us_execution_rows", normalizer)
    assert not asyncio.run(engine.sync_broker_state(force_balance=True))
    assert len(calls) == 1 and dump(engine.ledger) == before and engine._balance_sync_blocked
    writes.assert_not_called()
    normalizer.assert_not_called()
    engine._apply_confirmed_fill.assert_not_awaited()
    engine._cancel_stale_orders.assert_not_awaited()
    engine._run_balance_reconciliation_cycle.assert_not_awaited()
    assert engine._us_observation_blocks_order("AAPL")
    assert all("sensitive synthetic error" not in message for message in engine.ctx.logger.errors)


def test_failed_second_date_never_calls_sink_or_normalizes_first(factory, monkeypatch):
    sink = SinkDouble()
    engine, client = factory(adapter(sink))
    add_order(engine.ledger, "20261001")
    add_order(engine.ledger, "20261002")

    async def executed(symbol, *, order_date):
        if order_date == "20261002":
            raise ValueError("synthetic incomplete pages")
        return dict(response(), _query_order_date=order_date)

    client.get_executed_orders = executed
    normalizer = Mock()
    monkeypatch.setattr(engine_module, "normalize_us_execution_rows", normalizer)
    before = dump(engine.ledger)
    assert not asyncio.run(engine.sync_broker_state(force_balance=True))
    assert not sink.calls and dump(engine.ledger) == before
    normalizer.assert_not_called()
    engine._cancel_stale_orders.assert_not_awaited()


def test_complete_sync_readies_only_that_engine_and_no_order_is_submitted(factory):
    sink = SinkDouble()
    engine, client = factory(adapter(sink))
    add_order(engine.ledger, "20261002")
    history(client, empty=True)
    assert engine._us_observation_blocks_order("AAPL")
    assert asyncio.run(engine.sync_broker_state(force_balance=True))
    assert not engine._us_observation_blocks_order("AAPL")
    client.place_order.assert_not_awaited()


def test_balance_failure_keeps_observed_receipt_from_unblocking_orders(factory):
    engine, client = factory(adapter(SinkDouble()))
    add_order(engine.ledger, "20261002")
    history(client, empty=True)
    engine._run_balance_reconciliation_cycle = AsyncMock(return_value=False)
    assert not asyncio.run(engine.sync_broker_state(force_balance=True))
    assert engine._us_observation_blocks_order("AAPL")


def test_empty_tracking_set_cannot_clear_startup_blocker(factory):
    sink = SinkDouble()
    engine, client = factory(adapter(sink))
    assert not asyncio.run(engine.sync_broker_state(force_balance=True))
    assert not sink.calls and client.execution_history_calls == 0
    assert engine._us_observation_blocks_order("AAPL")


@pytest.mark.parametrize("fault", ["market", "account", "mode", "adapter_disabled"])
def test_changed_scope_or_adapter_blocks_before_history_request(factory, fault):
    observer = adapter(SinkDouble())
    engine, client = factory(observer)
    add_order(engine.ledger, "20261002")
    if fault == "market":
        client.market = "KR"
    elif fault == "account":
        engine.ctx.account_id = "other"
    elif fault == "mode":
        client.mode = "real"
    else:
        observer.enabled = False
    assert not asyncio.run(engine.sync_broker_state(force_balance=True))
    assert client.execution_history_calls == 0
    assert engine._us_observation_blocks_order("AAPL")


def test_conflict_latch_survives_later_observed_receipt_and_disable_attempt(factory):
    sink = SinkDouble(reply=lambda c: UsObservationReceipt(c.cycle_token, "CONFLICT", True,
                                                          ((c.bindings[0].order_uid, "synthetic_conflict"),)))
    observer = adapter(sink)
    engine, client = factory(observer)
    add_order(engine.ledger, "20261002")
    history(client, empty=True)
    assert not asyncio.run(engine.sync_broker_state(force_balance=True))
    sink.reply = None
    assert not asyncio.run(engine.sync_broker_state(force_balance=True))
    observer.enabled = False
    assert engine._us_observation_active() and engine._us_observation_blocks_order("AAPL")


def test_shared_symbol_blocker_applies_to_another_engine_but_not_another_symbol(factory):
    first, _ = factory(adapter(SinkDouble()))
    second, client = factory()
    third, _ = factory(symbol="MSFT")
    first._record_us_observation_state("CONFLICT")
    assert second._us_observation_blocks_order("AAPL")
    assert not third._us_observation_blocks_order("MSFT")
    asyncio.run(second._execute_order(OrderIntent(Action.BUY, "AAPL", 1, 100)))
    client.place_order.assert_not_awaited()


def test_recreated_active_engine_begins_blocked_without_reusing_previous_success(factory):
    first, client = factory(adapter(SinkDouble()))
    add_order(first.ledger, "20261002")
    history(client, empty=True)
    assert asyncio.run(first.sync_broker_state(force_balance=True))
    second, _ = factory(adapter(SinkDouble()))
    assert second._us_observation_blocks_order("AAPL")
    assert first._us_observation_blocks_order("AAPL")


def test_final_submission_recheck_blocks_conflict_arriving_during_clearance(factory, monkeypatch):
    engine, client = factory(adapter(SinkDouble()))
    add_order(engine.ledger, "20261002")
    history(client, empty=True)
    assert asyncio.run(engine.sync_broker_state(force_balance=True))
    engine._dashboard_profile_allowed = True
    engine._symbol_key_migration_complete = True
    engine._symbol_key_manual_review = Mock(return_value=False)
    engine._quantity_conflict_blocks_order = Mock(return_value=False)
    monkeypatch.setenv("US_PAPER_ORDER_SUBMISSION_ENABLED", "true")
    monkeypatch.setattr(engine_module.control_snapshot, "read_control", lambda *args: {"auto_buy": True})
    monkeypatch.setattr(engine_module.control_snapshot, "require_authority", lambda *args: None)

    async def check(*args):
        engine._record_us_observation_state("CONFLICT")

    engine._dispatch_clearance_enabled = True
    engine._balance_gate.dispatch_clearance_service = SimpleNamespace(check=check)
    asyncio.run(engine._execute_order(OrderIntent(Action.BUY, "AAPL", 1, 100)))
    client.place_order.assert_not_awaited()


def test_tick_stops_before_quote_and_strategy_after_observation_failure(factory, monkeypatch):
    engine, client = factory(adapter(None))
    add_order(engine.ledger, "20261002")
    history(client, empty=True)
    engine._refresh_runtime_control = Mock()
    engine._refresh_dashboard_controls = AsyncMock()
    engine._apply_fixed_port_pause_clear_event = AsyncMock()
    monkeypatch.setattr(engine_module, "get_fixed_port_degraded_state", lambda *args: None)
    engine._safe_get_quote = AsyncMock()
    engine.ctx.strategy.evaluate = Mock()
    asyncio.run(engine._tick())
    engine._safe_get_quote.assert_not_awaited()
    engine.ctx.strategy.evaluate.assert_not_called()
    client.place_order.assert_not_awaited()


@pytest.mark.parametrize("fault", ["account", "market", "mode", "balance_only", "adapter_type"])
def test_invalid_injection_refuses_before_engine_filesystem_setup(tmp_path, monkeypatch, fault):
    target = tmp_path / "must-not-create"
    monkeypatch.setattr(engine_module, "DATA_DIR", target)
    ctx = SimpleNamespace(account_id="us_mock", client=SimpleNamespace(market="US", mode="mock"))
    observer = adapter(SinkDouble())
    if fault == "account":
        ctx.account_id = "kr_mock"
    elif fault == "market":
        ctx.client.market = "KR"
    elif fault == "mode":
        ctx.client.mode = "real"
    elif fault == "adapter_type":
        observer = object()
    with pytest.raises(ValueError):
        AccountEngine(ctx, None, None, None, balance_only=fault == "balance_only",
                      us_observation_adapter=observer)
    assert not target.exists()
