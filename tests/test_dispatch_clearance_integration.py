import asyncio
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.core.broker_http import clear_fixed_port_degraded_state, enter_fixed_port_degraded_state, get_fixed_port_degraded_state
from src.core import dashboard_control_snapshot as control_snapshot
from src.core.engine import (
    AccountEngine, DispatchClearanceService, NormalizedBalanceHolding,
    ReconciliationClearanceSnapshot,
)
from src.data.order_attempts import OrderAttemptStore
from src.data.trade_ledger import FillQuantityExceededError, PendingOrder, TradeLedgerStore
from src.data.trade_ledger_migration import create_identity_ledger_copy
from src.utils.exceptions import OrderDispatchBlockedError
from src.strategy.base import Action, OrderIntent
from tests.support.telegram_double import make_telegram_double


SESSION = "1" * 32


def _snapshot(*, clear):
    return ReconciliationClearanceSnapshot(
        account_id="us_mock", symbol="SOXL", market="US", balance_api_id="ust21070",
        balance_fetched_fresh=clear, balance_from_shared_cache=False,
        balance_recognized=True, holding=NormalizedBalanceHolding("SOXL", 0, 0),
        balance_received_at=time.monotonic(), max_balance_age_sec=1.0,
    )


def _engine(service, snapshot, *, enabled, data_dir):
    engine = object.__new__(AccountEngine)
    engine.data_dir = data_dir
    place_order = AsyncMock(
        side_effect=AssertionError("must not submit") if enabled else None,
        return_value=SimpleNamespace(ord_no="ORDER-1"),
    )
    engine.ctx = SimpleNamespace(
        account_id="us_mock", client=SimpleNamespace(
            market="US", mode="mock", place_order=place_order,
            mark_order_pending_recorded=Mock(),
        ), logger=Mock(), position=SimpleNamespace(step=0),
    )
    engine._dashboard_profile_allowed = True
    engine._blocked_order_until = {}
    engine._last_auto_buy_price = {}
    engine._dispatch_clearance_enabled = enabled
    engine._balance_gate = SimpleNamespace(dispatch_clearance_service=service)
    engine._control_authority = control_snapshot.ControlAuthority("us_mock", SESSION)
    engine.telegram = make_telegram_double()
    engine.ledger = SimpleNamespace(schema_version=2, add_pending=Mock(), quantity_conflict_order_ids=lambda _symbol: ())
    engine.sync_broker_state = AsyncMock()
    engine._build_reconciliation_clearance_snapshot = AsyncMock(return_value=snapshot)
    control_snapshot.initialize(data_dir, "us_mock", {}, None)
    control_snapshot.update(data_dir, "us_mock", {
        "symbol": "SOXL",
        "instance_id": SESSION,
        "auto_buy": True,
        "auto_sell": True,
        "config": {"symbol": "SOXL", "market": "US", "mode": "mock"},
    })
    return engine


def _identity_ledger(path):
    """Prepare a real v2 ledger from an empty synthetic legacy store."""
    source = path.with_suffix(".legacy.db")
    legacy = TradeLedgerStore(source, "us_mock")
    legacy.close()
    create_identity_ledger_copy(source, path, account_markets={"us_mock": "US"})
    return TradeLedgerStore(path, "us_mock", market="US")


def _persist_quantity_conflict(store, symbol="SOXL"):
    order = PendingOrder("CONFLICT", symbol, "BUY", 2, 10, "BUY", 1, {})
    store.add_pending(order)
    if store.schema_version == 2:
        store.confirm_us_order_date(order.order_uid, "20261001", evidence={
            "source_tr": "synthetic", "record_ref": "quantity-conflict-fixture",
            "verified_at_utc": "2026-10-01T00:00:00+00:00",
        })
    with pytest.raises(FillQuantityExceededError):
        store.record_fill(order, 3, 10, "2026-10-01")
    return order


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("action", [Action.BUY, Action.SELL])
def test_persisted_conflict_blocks_dispatch_after_reopen_with_clearance_on_or_off(tmp_path, enabled, action):
    path = tmp_path / "trades_us_mock.db"
    store = _identity_ledger(path)
    try:
        order = _persist_quantity_conflict(store)
        with pytest.raises(ValueError, match="Durable order conflict blocks fill attribution"):
            store.record_fill(order, 2, 10, "2026-10-01", execution_date="20261001")
    finally:
        store.close()
    engine = _engine(None, _snapshot(clear=True), enabled=enabled, data_dir=tmp_path)
    engine.ledger = TradeLedgerStore(path, "us_mock", market="US")
    try:
        with patch.dict(os.environ, {"US_PAPER_ORDER_SUBMISSION_ENABLED": "true"}, clear=False):
            asyncio.run(engine._execute_order(OrderIntent(action, "SOXL", 1, 10.0)))
        engine.ctx.client.place_order.assert_not_awaited()
        assert engine.ledger.quantity_conflict_order_ids("SOXL") == (order.order_uid,)
    finally:
        engine.ledger.close()


def test_conflict_inspection_failure_blocks_dispatch(tmp_path):
    engine = _engine(None, _snapshot(clear=True), enabled=False, data_dir=tmp_path)
    engine.ledger.quantity_conflict_order_ids = Mock(side_effect=sqlite3.OperationalError("read blocked"))
    asyncio.run(engine._execute_order(OrderIntent(Action.BUY, "SOXL", 1, 10.0)))
    engine.ctx.client.place_order.assert_not_awaited()


def test_conflict_created_during_clearance_blocks_at_final_submission_boundary(tmp_path):
    store = _identity_ledger(tmp_path / "trades_us_mock.db")

    async def check(_engine_arg, _symbol):
        _persist_quantity_conflict(store)

    service = SimpleNamespace(check=AsyncMock(side_effect=check))
    engine = _engine(service, _snapshot(clear=True), enabled=True, data_dir=tmp_path)
    engine.ledger = store
    try:
        with patch.dict(os.environ, {"US_PAPER_ORDER_SUBMISSION_ENABLED": "true"}, clear=False):
            asyncio.run(engine._execute_order(OrderIntent(Action.BUY, "SOXL", 1, 10.0)))
        service.check.assert_awaited_once()
        engine.ctx.client.place_order.assert_not_awaited()
    finally:
        store.close()


def test_passive_clearance_and_generic_pause_clear_reject_conflict_only_order(tmp_path):
    store = TradeLedgerStore(tmp_path / "trades_us_mock.db", "us_mock")
    try:
        order = _persist_quantity_conflict(store)
        store.record_fill(order, 2, 10, "2026-10-01")
    finally:
        store.close()
    engine = _passive_snapshot_engine(tmp_path, balance_qty=0)
    engine.ctx.strategy.symbol = "SOXL"
    engine.ctx.logger = Mock()
    engine._trading_paused = True
    engine._pause_reason = "execution_quantity_conflict"
    engine._balance_gate = SimpleNamespace(engines=[engine], pause_clear_event_id="")
    engine._pause_clear_event = Mock(return_value=("CLEAR-1", "execution_quantity_conflict"))
    snapshot = asyncio.run(engine._build_reconciliation_clearance_snapshot("SOXL", max_balance_age_sec=1))
    assert snapshot.unresolved_order_ids == ("CONFLICT",)
    asyncio.run(engine._apply_reconciliation_clear_event())
    assert engine._trading_paused
    assert engine._pause_reason == "execution_quantity_conflict"
    assert engine._balance_gate.pause_clear_event_id == ""


def test_passive_clearance_does_not_treat_missing_conflict_table_as_clear(tmp_path):
    store = TradeLedgerStore(tmp_path / "trades_us_mock.db", "us_mock")
    try:
        store.db.execute("DROP TABLE execution_quantity_conflicts")
        store.db.commit()
    finally:
        store.close()
    engine = _passive_snapshot_engine(tmp_path, balance_qty=0)
    with pytest.raises(RuntimeError, match="read-only reconciliation clearance ledger unavailable"):
        asyncio.run(engine._build_reconciliation_clearance_snapshot("SOXL", max_balance_age_sec=1))


def _passive_snapshot_engine(data_dir, *, balance_qty=1.0):
    engine = object.__new__(AccountEngine)
    engine.data_dir = data_dir
    engine.ledger = None
    engine.ctx = SimpleNamespace(
        account_id="us_mock",
        client=SimpleNamespace(
            market="US",
            get_balance=AsyncMock(return_value={
                "result_list": [{
                    "ovrs_pdno": "SOXL",
                    "ovrs_item_name": "Direxion Daily Semiconductor Bull 3X Shares",
                    "ovrs_cblc_qty": str(balance_qty),
                    "pchs_avg_pric": "10.0",
                    "ovrs_now_pric": "10.0",
                    "prev_close": "10.0",
                }],
            }),
        ),
        position=SimpleNamespace(qty=balance_qty),
        strategy=SimpleNamespace(max_step=3, step_qty={}),
    )
    engine._broker_fill_catchup_qty = {}
    engine._symbol_lifecycles = {}
    engine._pause_reason = ""
    return engine


async def _blocked_dispatch(enabled, data_dir):
    clear_fixed_port_degraded_state("us_mock")
    enter_fixed_port_degraded_state("us_mock", "rest")
    service = DispatchClearanceService("us_mock")
    service.observe_active_profile(("SOXL",), 0)
    engine = _engine(service, _snapshot(clear=False), enabled=enabled, data_dir=data_dir)
    intent = OrderIntent(Action.BUY, "SOXL", 1, 10.0, "00", {})
    try:
        with patch.dict(os.environ, {"US_PAPER_ORDER_SUBMISSION_ENABLED": "true"}, clear=False):
            await engine._execute_order(intent)
        return engine
    finally:
        clear_fixed_port_degraded_state("us_mock")


def test_degraded_dispatch_seam_blocks_before_place_order_when_enabled(tmp_path):
    engine = asyncio.run(_blocked_dispatch(True, tmp_path))

    engine.ctx.client.place_order.assert_not_awaited()
    engine.telegram.notify_error.assert_awaited_once()


def test_kill_switch_disables_degraded_dispatch_seam(tmp_path):
    engine = asyncio.run(_blocked_dispatch(False, tmp_path))

    engine.ctx.client.place_order.assert_awaited_once()


def test_pending_ledger_failure_keeps_order_attempt_unresolved(tmp_path):
    store = OrderAttemptStore(tmp_path / "order_attempts.db", "us_mock")
    attempt = store.record_attempt("BUY", "SOXL", 1, 10.0, "00")
    store.mark_accepted_unlinked(attempt.attempt_id)
    engine = _engine(None, _snapshot(clear=True), enabled=False, data_dir=tmp_path)
    engine.ctx.client.place_order.return_value = SimpleNamespace(
        ord_no="ORDER-1", attempt_id=attempt.attempt_id,
    )
    engine.ctx.client.mark_order_pending_recorded.side_effect = (
        lambda result: store.mark_pending_recorded(result.attempt_id)
    )
    engine.ledger.add_pending.side_effect = RuntimeError("pending ledger unavailable")
    intent = OrderIntent(Action.BUY, "SOXL", 1, 10.0, "00", {})

    try:
        try:
            with patch.dict(os.environ, {"US_PAPER_ORDER_SUBMISSION_ENABLED": "true"}, clear=False):
                asyncio.run(engine._execute_order(intent))
        except RuntimeError as exc:
            assert str(exc) == "pending ledger unavailable"
        else:
            raise AssertionError("pending ledger failure did not propagate")

        engine.ctx.client.mark_order_pending_recorded.assert_not_called()
        assert engine._last_auto_buy_price == {}
        unresolved = store.get_attempt(attempt.attempt_id)
        assert unresolved.dispatch_state == "accepted_unlinked"
        assert store.unattributed_attempt_ids() == [attempt.attempt_id]
    finally:
        store.close()


def test_attempt_confirmation_failure_keeps_order_unresolved_after_pending_write(tmp_path):
    store = OrderAttemptStore(tmp_path / "order_attempts.db", "us_mock")
    attempt = store.record_attempt("BUY", "SOXL", 1, 10.0, "00")
    store.mark_accepted_unlinked(attempt.attempt_id)
    engine = _engine(None, _snapshot(clear=True), enabled=False, data_dir=tmp_path)
    engine.ctx.client.place_order.return_value = SimpleNamespace(
        ord_no="ORDER-1", attempt_id=attempt.attempt_id,
    )
    ledger_path = tmp_path / "trades_us_mock.db"
    engine.ledger = _identity_ledger(ledger_path)

    def fail_after_pending_commit(result):
        reader = TradeLedgerStore(ledger_path, "us_mock", market="US")
        try:
            orders = reader.pending_orders("SOXL")
            assert len(orders) == 1
            pending = reader.get_pending(result.ord_no, order_uid=orders[0].order_uid)
            assert pending is not None
            assert pending.symbol == "SOXL"
        finally:
            reader.close()
        raise RuntimeError("confirmation unavailable")

    engine.ctx.client.mark_order_pending_recorded.side_effect = fail_after_pending_commit
    intent = OrderIntent(Action.BUY, "SOXL", 1, 10.0, "00", {})

    try:
        with patch.dict(os.environ, {"US_PAPER_ORDER_SUBMISSION_ENABLED": "true"}, clear=False):
            asyncio.run(engine._execute_order(intent))

        orders = engine.ledger.pending_orders("SOXL")
        assert len(orders) == 1
        pending = engine.ledger.get_pending("ORDER-1", order_uid=orders[0].order_uid)
        assert pending is not None
        assert pending.symbol == "SOXL"
        engine.ctx.client.mark_order_pending_recorded.assert_called_once()
        engine.telegram.notify_error.assert_awaited_once()
        assert "confirmation unavailable" in engine.telegram.notify_error.await_args.args[0]
        engine.telegram.notify_order.assert_not_awaited()
        engine.sync_broker_state.assert_not_awaited()
        unresolved = store.get_attempt(attempt.attempt_id)
        assert unresolved.dispatch_state == "accepted_unlinked"
        assert store.unattributed_attempt_ids() == [attempt.attempt_id]
    finally:
        engine.ledger.close()
        store.close()


def test_a_prefixed_symbol_completes_active_clearance_cycle(tmp_path):
    async def check():
        clear_fixed_port_degraded_state("us_mock")
        enter_fixed_port_degraded_state("us_mock", "rest")
        service = DispatchClearanceService("us_mock")
        service.observe_active_profile(("AAPL",), 0)
        engine = SimpleNamespace(_build_reconciliation_clearance_snapshot=AsyncMock(
            return_value=ReconciliationClearanceSnapshot(
                account_id="us_mock", symbol="AAPL", market="US", balance_api_id="ust21070",
                balance_fetched_fresh=True, balance_from_shared_cache=False,
                balance_recognized=True, holding=NormalizedBalanceHolding("AAPL", 0, 0),
                balance_received_at=time.monotonic(), max_balance_age_sec=1.0,
            ),
        ), data_dir=tmp_path)
        try:
            await service.check(engine, "AAPL")
            assert get_fixed_port_degraded_state("us_mock") is None
        finally:
            clear_fixed_port_degraded_state("us_mock")

    asyncio.run(check())


def test_kr_recovery_probe_clears_when_due_and_fully_reconciled(tmp_path):
    async def probe():
        clear_fixed_port_degraded_state("kr_mock")
        enter_fixed_port_degraded_state("kr_mock", "rest", now=_FixedDateTime.current)
        service = DispatchClearanceService("kr_mock")
        service.observe_active_profile(("005930",), 0)
        engine = SimpleNamespace(
            _build_reconciliation_clearance_snapshot=AsyncMock(
                return_value=ReconciliationClearanceSnapshot(
                    account_id="kr_mock",
                    symbol="005930",
                    market="KR",
                    balance_api_id="kt00018",
                    balance_fetched_fresh=True,
                    balance_from_shared_cache=False,
                    balance_recognized=True,
                    holding=NormalizedBalanceHolding("005930", 0, 0),
                    balance_received_at=time.monotonic(),
                    max_balance_age_sec=1.0,
                ),
            ),
            data_dir=tmp_path,
        )
        try:
            await service.check(engine, "005930")
            assert get_fixed_port_degraded_state("kr_mock") is None
        finally:
            clear_fixed_port_degraded_state("kr_mock")

    asyncio.run(probe())


class _FixedDateTime(datetime):
    current = datetime(2026, 8, 26, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.current if tz is None else cls.current.astimezone(tz)


def test_recovery_probe_is_suppressed_before_its_due_time(tmp_path):
    async def probe():
        clear_fixed_port_degraded_state("us_mock")
        enter_fixed_port_degraded_state(
            "us_mock", "rest", now=datetime.now(timezone.utc) + timedelta(seconds=1),
        )
        service = DispatchClearanceService("us_mock")
        service.observe_active_profile(("SOXL",), 0)
        engine = _engine(service, _snapshot(clear=True), enabled=True, data_dir=tmp_path)
        try:
            await service.probe_if_due(engine, "SOXL")
            engine._build_reconciliation_clearance_snapshot.assert_not_awaited()
        finally:
            clear_fixed_port_degraded_state("us_mock")

    asyncio.run(probe())


def test_recovery_probe_clears_when_due_and_fully_reconciled(tmp_path):
    async def probe():
        clear_fixed_port_degraded_state("us_mock")
        enter_fixed_port_degraded_state("us_mock", "rest", now=_FixedDateTime.current)
        service = DispatchClearanceService("us_mock")
        service.observe_active_profile(("SOXL",), 0)
        engine = _engine(service, _snapshot(clear=True), enabled=True, data_dir=tmp_path)
        try:
            with patch("src.core.engine.datetime", _FixedDateTime):
                await service.probe_if_due(engine, "SOXL")
            engine._build_reconciliation_clearance_snapshot.assert_awaited_once_with(
                "SOXL", max_balance_age_sec=1.0,
            )
            assert get_fixed_port_degraded_state("us_mock") is None
        finally:
            clear_fixed_port_degraded_state("us_mock")

    asyncio.run(probe())


def test_recovery_probe_retains_degraded_state_and_advances_schedule_when_blocked(tmp_path):
    async def probe():
        clear_fixed_port_degraded_state("us_mock")
        enter_fixed_port_degraded_state("us_mock", "rest", now=_FixedDateTime.current)
        service = DispatchClearanceService("us_mock")
        service.observe_active_profile(("SOXL",), 0)
        engine = _engine(service, _snapshot(clear=False), enabled=True, data_dir=tmp_path)
        try:
            with patch("src.core.engine.datetime", _FixedDateTime):
                await service.probe_if_due(engine, "SOXL")
            state = get_fixed_port_degraded_state("us_mock")
            assert state is not None
            assert state.next_recovery_probe_at == _FixedDateTime.current + timedelta(seconds=90)
        finally:
            clear_fixed_port_degraded_state("us_mock")

    asyncio.run(probe())


def test_concurrent_recovery_probes_produce_one_attempt(tmp_path):
    async def build_snapshot(symbol, *, max_balance_age_sec):
        await asyncio.sleep(0)
        return _snapshot(clear=False)

    async def probe():
        clear_fixed_port_degraded_state("us_mock")
        enter_fixed_port_degraded_state("us_mock", "rest", now=_FixedDateTime.current)
        service = DispatchClearanceService("us_mock")
        service.observe_active_profile(("SOXL",), 0)
        engine = _engine(service, _snapshot(clear=False), enabled=True, data_dir=tmp_path)
        engine._build_reconciliation_clearance_snapshot.side_effect = build_snapshot
        try:
            with patch("src.core.engine.datetime", _FixedDateTime):
                await asyncio.gather(
                    service.probe_if_due(engine, "SOXL"),
                    service.probe_if_due(engine, "SOXL"),
                )
            assert engine._build_reconciliation_clearance_snapshot.await_count == 1
        finally:
            clear_fixed_port_degraded_state("us_mock")

    asyncio.run(probe())


def test_passive_engine_reads_existing_ledger_without_mutating_sqlite(tmp_path):
    ledger_path = tmp_path / "trades_us_mock.db"
    ledger = TradeLedgerStore(ledger_path, "us_mock")
    ledger.close()
    before = ledger_path.read_bytes()
    engine = _passive_snapshot_engine(tmp_path)

    snapshot = asyncio.run(
        engine._build_reconciliation_clearance_snapshot("SOXL", max_balance_age_sec=1.0)
    )

    assert snapshot.balance_recognized is True
    assert snapshot.unresolved_order_ids == ()
    assert ledger_path.read_bytes() == before


def test_passive_clearance_ledger_failure_keeps_degraded_marker(tmp_path):
    async def check():
        clear_fixed_port_degraded_state("us_mock")
        enter_fixed_port_degraded_state("us_mock", "rest")
        service = DispatchClearanceService("us_mock")
        service.observe_active_profile(("SOXL",), 0)
        engine = _passive_snapshot_engine(tmp_path)
        try:
            try:
                await engine._build_reconciliation_clearance_snapshot(
                    "SOXL", max_balance_age_sec=1.0,
                )
            except RuntimeError as exc:
                assert "read-only reconciliation clearance ledger unavailable" in str(exc)
            else:
                raise AssertionError("missing read-only ledger did not raise RuntimeError")
            try:
                await service.check(engine, "SOXL")
            except OrderDispatchBlockedError as exc:
                assert "read-only reconciliation clearance ledger unavailable" in str(exc)
            else:
                raise AssertionError("read-only ledger failure did not block clearance")
            assert get_fixed_port_degraded_state("us_mock") is not None
        finally:
            clear_fixed_port_degraded_state("us_mock")

    asyncio.run(check())


def test_recovery_probe_logs_snapshot_failures_without_propagating(tmp_path):
    async def probe():
        clear_fixed_port_degraded_state("us_mock")
        enter_fixed_port_degraded_state("us_mock", "rest", now=_FixedDateTime.current)
        service = DispatchClearanceService("us_mock")
        service.observe_active_profile(("SOXL",), 0)
        engine = _engine(service, _snapshot(clear=True), enabled=True, data_dir=tmp_path)
        engine._build_reconciliation_clearance_snapshot.side_effect = RuntimeError("snapshot failed")
        try:
            with patch("src.core.engine.datetime", _FixedDateTime):
                await service.probe_if_due(engine, "SOXL")
            engine.ctx.logger.error.assert_called_once()
            state = get_fixed_port_degraded_state("us_mock")
            assert state is not None
            assert state.next_recovery_probe_at == _FixedDateTime.current + timedelta(seconds=90)
        finally:
            clear_fixed_port_degraded_state("us_mock")

    asyncio.run(probe())


def test_empty_profile_expired_holdoff_clean_reconciliation_clears(tmp_path):
    async def clear():
        clear_fixed_port_degraded_state("us_mock")
        enter_fixed_port_degraded_state("us_mock", "rest", now=_FixedDateTime.current)
        service = DispatchClearanceService("us_mock")
        engine = _engine(service, _snapshot(clear=True), enabled=False, data_dir=tmp_path)
        try:
            result = await service.clear_empty_profile_if_safe(
                engine,
                "SOXL",
                mode="mock",
                profile_enabled=False,
                now=_FixedDateTime.current + timedelta(seconds=161),
            )
            assert result.cleared is True
            assert get_fixed_port_degraded_state("us_mock") is None
        finally:
            clear_fixed_port_degraded_state("us_mock")

    asyncio.run(clear())


def test_empty_profile_active_holdoff_does_not_clear(tmp_path):
    async def clear():
        clear_fixed_port_degraded_state("us_mock")
        enter_fixed_port_degraded_state("us_mock", "rest", now=_FixedDateTime.current)
        service = DispatchClearanceService("us_mock")
        engine = _engine(service, _snapshot(clear=True), enabled=False, data_dir=tmp_path)
        try:
            result = await service.clear_empty_profile_if_safe(
                engine,
                "SOXL",
                mode="mock",
                profile_enabled=False,
                now=_FixedDateTime.current,
            )
            assert result.cleared is False
            assert get_fixed_port_degraded_state("us_mock") is not None
        finally:
            clear_fixed_port_degraded_state("us_mock")

    asyncio.run(clear())


def test_empty_profile_unresolved_reconciliation_does_not_clear(tmp_path):
    async def clear():
        clear_fixed_port_degraded_state("us_mock")
        enter_fixed_port_degraded_state("us_mock", "rest", now=_FixedDateTime.current)
        service = DispatchClearanceService("us_mock")
        engine = _engine(service, _snapshot(clear=False), enabled=False, data_dir=tmp_path)
        try:
            result = await service.clear_empty_profile_if_safe(
                engine,
                "SOXL",
                mode="mock",
                profile_enabled=False,
                now=_FixedDateTime.current + timedelta(seconds=161),
            )
            assert result.cleared is False
            assert get_fixed_port_degraded_state("us_mock") is not None
        finally:
            clear_fixed_port_degraded_state("us_mock")

    asyncio.run(clear())


def test_out_of_scope_mode_is_rejected(tmp_path):
    async def clear():
        service = DispatchClearanceService("us_mock")
        engine = _engine(service, _snapshot(clear=True), enabled=False, data_dir=tmp_path)
        try:
            try:
                await service.clear_empty_profile_if_safe(
                    engine,
                    "SOXL",
                    mode="live",
                    profile_enabled=False,
                    now=_FixedDateTime.current + timedelta(seconds=161),
                )
            except ValueError:
                pass
            else:
                raise AssertionError("non-mock mode was not rejected")
        finally:
            clear_fixed_port_degraded_state("us_mock")

    asyncio.run(clear())


def test_enabled_profile_is_rejected(tmp_path):
    async def clear():
        clear_fixed_port_degraded_state("us_mock")
        enter_fixed_port_degraded_state("us_mock", "rest", now=_FixedDateTime.current)
        service = DispatchClearanceService("us_mock")
        engine = _engine(service, _snapshot(clear=True), enabled=False, data_dir=tmp_path)
        try:
            result = await service.clear_empty_profile_if_safe(
                engine,
                "SOXL",
                mode="mock",
                profile_enabled=True,
                now=_FixedDateTime.current + timedelta(seconds=161),
            )
            assert result.cleared is False
            assert get_fixed_port_degraded_state("us_mock") is not None
        finally:
            clear_fixed_port_degraded_state("us_mock")

    asyncio.run(clear())
