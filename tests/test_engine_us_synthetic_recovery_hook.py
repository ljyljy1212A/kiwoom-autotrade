"""Injected scratch Engine recovery blockers; no worker or broker delivery."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import src.core.engine as engine_module
import tests.test_engine_us_observation_hook as observation_fixtures
from src.core.engine import AccountEngine
from src.core.us_synthetic_recovery_gate import SyntheticRecoveryGate
from src.data.order_identity import OrderIdentityStore
from src.data.us_synthetic_observation_journal import SyntheticJournalIdentity, create_synthetic_journal
from src.data.us_synthetic_observation_recovery import SyntheticObservationRecoveryReader
from src.data.us_synthetic_observation_sink import SyntheticObservationSink
from src.strategy.base import Action, OrderIntent
from tests.test_engine_us_observation_hook import adapter, factory, history
from tests.test_order_identity_runtime import add_order, dump as dump_ledger
from tests.test_us_synthetic_observation_recovery import dump, seed


@pytest.fixture
def recovery(tmp_path):
    root = tmp_path / "engine-recovery-journal"
    root.mkdir()
    journal = create_synthetic_journal(root, allow_root=tmp_path)
    reader = SyntheticObservationRecoveryReader(journal)
    gate = SyntheticRecoveryGate(account_id="us_mock", market="US", enabled=True,
                                 reader=reader, expected_journal=journal)
    try:
        yield journal, reader, gate
    finally:
        journal.close()


def inject(factory, monkeypatch, gate, observer=None, *, symbol="AAPL"):
    monkeypatch.setattr(observation_fixtures, "AccountEngine",
                        lambda *args, **kwargs: AccountEngine(*args, **kwargs, us_recovery_gate=gate))
    return factory(observer, symbol=symbol)


def test_disabled_recovery_never_reads_or_changes_default_path(factory, monkeypatch):
    gate = SyntheticRecoveryGate(account_id="us_mock", market="US")
    gate.check_scope = Mock(side_effect=AssertionError("disabled reader must not run"))
    engine, client = inject(factory, monkeypatch, gate)
    history(client, empty=True)
    assert asyncio.run(engine.sync_broker_state(force_balance=True))
    assert not engine._us_observation_blocks_order("AAPL")
    gate._enabled = True
    assert not engine._us_recovery_enabled
    gate.check_scope.assert_not_called()
    client.place_order.assert_not_awaited()


def test_clean_recovery_does_not_clear_observation_startup_or_grant_authority(factory, monkeypatch, recovery):
    journal, _, gate = recovery
    engine, client = inject(factory, monkeypatch, gate, adapter(SyntheticObservationSink(journal)))
    before = dump(journal)
    assert not engine._us_recovery_blocks_order("AAPL")
    assert engine._us_observation_blocks_order("AAPL")
    asyncio.run(engine._execute_order(OrderIntent(Action.BUY, "AAPL", 1, 100)))
    client.place_order.assert_not_awaited()
    assert dump(journal) == before


def test_historical_untracked_conflict_stops_sync_before_clearance_or_history(factory, monkeypatch, recovery):
    journal, _, gate = recovery
    seed(journal)
    engine, client = inject(factory, monkeypatch, gate, adapter(SyntheticObservationSink(journal)))
    before = dump(journal)
    assert not asyncio.run(engine.sync_broker_state(force_balance=True))
    engine._apply_reconciliation_clear_event.assert_not_awaited()
    engine._apply_confirmed_fill.assert_not_awaited()
    engine._cancel_stale_orders.assert_not_awaited()
    engine._run_balance_reconciliation_cycle.assert_not_awaited()
    assert client.execution_history_calls == 0 and dump(journal) == before
    assert engine._balance_sync_blocked


def test_historical_symbol_conflict_is_shared_but_does_not_block_other_symbol(factory, monkeypatch, recovery):
    journal, _, gate = recovery
    seed(journal)
    first, _ = inject(factory, monkeypatch, gate, adapter(SyntheticObservationSink(journal)))
    assert first._us_recovery_blocks_order("AAPL")
    monkeypatch.setattr(observation_fixtures, "AccountEngine", AccountEngine)
    sibling, client = factory()
    other, _ = factory(symbol="MSFT")
    assert sibling._us_observation_blocks_order("AAPL")
    assert not other._us_recovery_blocks_order("MSFT")
    asyncio.run(sibling._execute_order(OrderIntent(Action.BUY, "AAPL", 1, 100)))
    client.place_order.assert_not_awaited()


def test_account_failure_stays_shared_after_reader_recovers(factory, monkeypatch, recovery):
    journal, reader, gate = recovery
    original = reader.recover_scope
    engine, _ = inject(factory, monkeypatch, gate, adapter(SyntheticObservationSink(journal)))
    monkeypatch.setattr(reader, "recover_scope", Mock(side_effect=RuntimeError("sensitive fixture failure")))
    assert engine._us_recovery_blocks_order("AAPL")
    monkeypatch.setattr(reader, "recover_scope", original)
    assert engine._us_recovery_blocks_order("AAPL")
    monkeypatch.setattr(observation_fixtures, "AccountEngine", AccountEngine)
    other, _ = factory(symbol="MSFT")
    assert other._us_recovery_blocks_order("MSFT")
    assert all("sensitive fixture failure" not in message for message in engine.ctx.logger.errors)


@pytest.mark.parametrize("fault", ["gate_replaced", "gate_disabled", "sink_replaced", "market", "mode", "account"])
def test_changed_binding_or_scope_latches_account_before_sync(factory, monkeypatch, recovery, fault):
    journal, _, gate = recovery
    observer = adapter(SyntheticObservationSink(journal))
    engine, client = inject(factory, monkeypatch, gate, observer)
    if fault == "gate_replaced":
        engine._us_recovery_gate = SyntheticRecoveryGate(account_id="us_mock", market="US")
    elif fault == "gate_disabled":
        gate._enabled = False
    elif fault == "sink_replaced":
        observer.sink = object()
    elif fault == "market":
        client.market = "KR"
    elif fault == "mode":
        client.mode = "real"
    else:
        engine.ctx.account_id = "other"
    assert not asyncio.run(engine.sync_broker_state(force_balance=True))
    assert engine._balance_gate.us_recovery_account_blocked
    engine._apply_reconciliation_clear_event.assert_not_awaited()
    client.place_order.assert_not_awaited()
    assert client.execution_history_calls == 0


def test_forged_authority_decision_blocks_account(factory, monkeypatch, recovery):
    journal, _, gate = recovery
    clean = gate.check_scope("AAPL")
    monkeypatch.setattr(gate, "check_scope", lambda _: replace(clean, operational_trading_allowed=True))
    engine, _ = inject(factory, monkeypatch, gate, adapter(SyntheticObservationSink(journal)))
    assert engine._us_recovery_blocks_order("AAPL")
    assert engine._balance_gate.us_recovery_account_blocked


def test_clean_receipt_cannot_clear_previously_shared_symbol_conflict(factory, monkeypatch, recovery):
    journal, _, gate = recovery
    clean = gate.check_scope("AAPL")
    seed(journal)
    engine, _ = inject(factory, monkeypatch, gate, adapter(SyntheticObservationSink(journal)))
    assert engine._us_recovery_blocks_order("AAPL")
    monkeypatch.setattr(gate, "check_scope", lambda _: clean)
    assert engine._us_recovery_blocks_order("AAPL")
    assert not engine._balance_gate.us_recovery_account_blocked


def test_missing_active_backend_blocks_without_creating_one(factory, monkeypatch, recovery):
    journal, _, _ = recovery
    gate = SyntheticRecoveryGate(account_id="us_mock", market="US", enabled=True)
    engine, client = inject(factory, monkeypatch, gate, adapter(SyntheticObservationSink(journal)))
    before = dump(journal)
    assert not asyncio.run(engine.sync_broker_state(force_balance=True))
    assert dump(journal) == before and engine._balance_gate.us_recovery_account_blocked
    client.place_order.assert_not_awaited()


def test_conflict_arriving_during_clearance_is_read_at_final_submission_boundary(factory, monkeypatch, recovery):
    journal, _, gate = recovery
    engine, client = inject(factory, monkeypatch, gate, adapter(SyntheticObservationSink(journal)))
    # Fixture-only observation readiness does not bypass other submission guards.
    engine._record_us_observation_state("OBSERVED")
    engine._dashboard_profile_allowed = True
    engine._symbol_key_migration_complete = True
    engine._symbol_key_manual_review = Mock(return_value=False)
    engine._quantity_conflict_blocks_order = Mock(return_value=False)
    monkeypatch.setenv("US_PAPER_ORDER_SUBMISSION_ENABLED", "true")
    monkeypatch.setattr(engine_module.control_snapshot, "read_control", lambda *args: {"auto_buy": True})
    monkeypatch.setattr(engine_module.control_snapshot, "require_authority", lambda *args: None)

    async def check(*args):
        seed(journal)

    engine._dispatch_clearance_enabled = True
    engine._balance_gate.dispatch_clearance_service = SimpleNamespace(check=check)
    asyncio.run(engine._execute_order(OrderIntent(Action.BUY, "AAPL", 1, 100)))
    client.place_order.assert_not_awaited()
    assert engine._us_recovery_blocks_order("AAPL")


@pytest.mark.parametrize("fault", ["gate_type", "no_observer", "balance_only", "account"])
def test_invalid_recovery_injection_refuses_before_filesystem_setup(tmp_path, monkeypatch, recovery, fault):
    journal, _, gate = recovery
    target = tmp_path / "must-not-create-engine-data"
    monkeypatch.setattr(engine_module, "DATA_DIR", target)
    ctx = SimpleNamespace(account_id="us_mock", client=SimpleNamespace(market="US", mode="mock"))
    observer = adapter(SyntheticObservationSink(journal))
    if fault == "gate_type":
        gate = object()
    elif fault == "no_observer":
        observer = None
    elif fault == "account":
        ctx.account_id = "kr_mock"
    with pytest.raises(ValueError):
        AccountEngine(ctx, None, None, None, balance_only=fault == "balance_only",
                      us_observation_adapter=observer, us_recovery_gate=gate)
    assert not target.exists()


def bind_current_order(engine, journal):
    order = add_order(engine.ledger, "20261002")
    identity = OrderIdentityStore(engine.ledger.db).get(order.order_uid)
    journal.bind(SyntheticJournalIdentity(
        identity.order_uid, identity.broker_order_date, identity.ord_no,
        identity.symbol, identity.side, str(int(order.requested_qty)), identity.submitted_at_utc,
    ))
    return order


@pytest.mark.parametrize("fault", ["historical_conflict", "sink_changed", "reader_failure"])
def test_change_during_history_wait_blocks_before_sink_normalization_or_ledger(
        factory, monkeypatch, recovery, fault):
    journal, reader, gate = recovery
    sink = SyntheticObservationSink(journal)
    observer = adapter(sink)
    engine, client = inject(factory, monkeypatch, gate, observer)
    current = bind_current_order(engine, journal)
    history(client, empty=True)
    original = client.get_executed_orders
    captured = {}

    async def executed(symbol, *, order_date):
        data = await original(symbol, order_date=order_date)
        await asyncio.sleep(0)
        if fault == "historical_conflict":
            seed(journal)
        elif fault == "sink_changed":
            observer.sink = object()
        else:
            monkeypatch.setattr(reader, "recover_scope", Mock(side_effect=RuntimeError("synthetic read failure")))
        captured["journal"] = dump(journal)
        return data

    client.get_executed_orders = executed
    sink_call = Mock(wraps=sink.record_cycle)
    monkeypatch.setattr(sink, "record_cycle", sink_call)
    normalization = Mock(side_effect=AssertionError("blocked rows must not be normalized"))
    monkeypatch.setattr(engine_module, "normalize_us_execution_rows", normalization)
    writes = Mock(side_effect=AssertionError("blocked rows must not reach the legacy ledger"))
    monkeypatch.setattr(engine.ledger, "record_fill", writes)
    before = dump_ledger(engine.ledger)
    assert not asyncio.run(engine.sync_broker_state(force_balance=True))
    assert engine._balance_sync_blocked and dump_ledger(engine.ledger) == before
    assert dump(journal) == captured["journal"]
    sink_call.assert_not_called()
    normalization.assert_not_called()
    writes.assert_not_called()
    engine._apply_confirmed_fill.assert_not_awaited()
    engine._cancel_stale_orders.assert_not_awaited()
    engine._run_balance_reconciliation_cycle.assert_not_awaited()
    client.place_order.assert_not_awaited()
    if fault == "historical_conflict":
        assert current.order_uid != "old-generation"
        assert engine._balance_gate.us_recovery_states["AAPL"][engine._us_observation_owner] == "CONFLICT"
    else:
        assert engine._balance_gate.us_recovery_account_blocked


@pytest.mark.parametrize("fault", ["historical_conflict", "shared_account_failure", "sink_changed"])
def test_change_during_balance_wait_prevents_success_and_observed_publication(
        factory, monkeypatch, recovery, fault):
    journal, _, gate = recovery
    observer = adapter(SyntheticObservationSink(journal))
    engine, client = inject(factory, monkeypatch, gate, observer)
    bind_current_order(engine, journal)
    history(client, empty=True)
    captured = {}

    async def balance(*, flush_dashboard_fills):
        await asyncio.sleep(0)
        if fault == "historical_conflict":
            seed(journal)
        elif fault == "shared_account_failure":
            engine._balance_gate.us_recovery_account_blocked = True
        else:
            observer.sink = object()
        captured["journal"] = dump(journal)
        return True

    engine._run_balance_reconciliation_cycle = AsyncMock(side_effect=balance)
    before = dump_ledger(engine.ledger)
    assert not asyncio.run(engine.sync_broker_state(force_balance=True))
    engine._run_balance_reconciliation_cycle.assert_awaited_once()
    assert engine._balance_sync_blocked and dump_ledger(engine.ledger) == before
    assert dump(journal) == captured["journal"]
    assert engine._balance_gate.us_observation_states["AAPL"][engine._us_observation_owner] != "OBSERVED"
    assert engine._us_observation_blocks_order("AAPL")
    client.place_order.assert_not_awaited()


def test_unchanged_recovery_allows_existing_observation_and_balance_sync(factory, monkeypatch, recovery):
    journal, _, gate = recovery
    sink = SyntheticObservationSink(journal)
    engine, client = inject(factory, monkeypatch, gate, adapter(sink))
    bind_current_order(engine, journal)
    history(client, empty=True)
    sink_call = Mock(wraps=sink.record_cycle)
    monkeypatch.setattr(sink, "record_cycle", sink_call)
    before = dump_ledger(engine.ledger)
    assert asyncio.run(engine.sync_broker_state(force_balance=True))
    sink_call.assert_called_once()
    engine._run_balance_reconciliation_cycle.assert_awaited_once()
    assert not engine._balance_sync_blocked and dump_ledger(engine.ledger) == before
    assert engine._balance_gate.us_observation_states["AAPL"][engine._us_observation_owner] == "OBSERVED"
    client.place_order.assert_not_awaited()
