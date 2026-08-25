import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.core.broker_http import clear_fixed_port_degraded_state, enter_fixed_port_degraded_state, get_fixed_port_degraded_state
from src.core.engine import (
    AccountEngine, DispatchClearanceService, NormalizedBalanceHolding,
    ReconciliationClearanceSnapshot,
)
from src.strategy.base import Action, OrderIntent


def _snapshot(*, clear):
    return ReconciliationClearanceSnapshot(
        account_id="us_mock", symbol="SOXL", balance_api_id="ust21070",
        balance_fetched_fresh=clear, balance_from_shared_cache=False,
        balance_recognized=True, holding=NormalizedBalanceHolding("SOXL", 0, 0),
        balance_received_at=time.monotonic(), max_balance_age_sec=1.0,
    )


def _engine(service, snapshot, *, enabled):
    engine = object.__new__(AccountEngine)
    place_order = AsyncMock(
        side_effect=AssertionError("must not submit") if enabled else None,
        return_value=SimpleNamespace(ord_no="ORDER-1"),
    )
    engine.ctx = SimpleNamespace(
        account_id="us_mock", client=SimpleNamespace(
            market="US", mode="mock", place_order=place_order,
        ), logger=Mock(), position=SimpleNamespace(step=0),
    )
    engine._dashboard_profile_allowed = True
    engine._blocked_order_until = {}
    engine._last_auto_buy_price = {}
    engine._dispatch_clearance_enabled = enabled
    engine._balance_gate = SimpleNamespace(dispatch_clearance_service=service)
    engine.telegram = SimpleNamespace(notify_error=AsyncMock(), notify_order=AsyncMock())
    engine.ledger = SimpleNamespace(add_pending=Mock())
    engine.sync_broker_state = AsyncMock()
    engine._build_reconciliation_clearance_snapshot = AsyncMock(return_value=snapshot)
    return engine


async def _blocked_dispatch(enabled):
    clear_fixed_port_degraded_state("us_mock")
    enter_fixed_port_degraded_state("us_mock", "rest")
    service = DispatchClearanceService("us_mock")
    service.observe_active_profile(("SOXL",), 0)
    engine = _engine(service, _snapshot(clear=False), enabled=enabled)
    intent = OrderIntent(Action.BUY, "SOXL", 1, 10.0, "00", {})
    try:
        with patch.dict(os.environ, {"US_PAPER_ORDER_SUBMISSION_ENABLED": "true"}, clear=False):
            await engine._execute_order(intent)
        return engine
    finally:
        clear_fixed_port_degraded_state("us_mock")


def test_degraded_dispatch_seam_blocks_before_place_order_when_enabled():
    engine = asyncio.run(_blocked_dispatch(True))

    engine.ctx.client.place_order.assert_not_awaited()
    engine.telegram.notify_error.assert_awaited_once()


def test_kill_switch_disables_degraded_dispatch_seam():
    engine = asyncio.run(_blocked_dispatch(False))

    engine.ctx.client.place_order.assert_awaited_once()


def test_a_prefixed_symbol_completes_active_clearance_cycle():
    async def check():
        clear_fixed_port_degraded_state("us_mock")
        enter_fixed_port_degraded_state("us_mock", "rest")
        service = DispatchClearanceService("us_mock")
        service.observe_active_profile(("AAPL",), 0)
        engine = SimpleNamespace(_build_reconciliation_clearance_snapshot=AsyncMock(
            return_value=ReconciliationClearanceSnapshot(
                account_id="us_mock", symbol="AAPL", balance_api_id="ust21070",
                balance_fetched_fresh=True, balance_from_shared_cache=False,
                balance_recognized=True, holding=NormalizedBalanceHolding("AAPL", 0, 0),
                balance_received_at=time.monotonic(), max_balance_age_sec=1.0,
            ),
        ))
        try:
            await service.check(engine, "AAPL")
            assert get_fixed_port_degraded_state("us_mock") is None
        finally:
            clear_fixed_port_degraded_state("us_mock")

    asyncio.run(check())


class _FixedDateTime(datetime):
    current = datetime(2026, 8, 26, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.current if tz is None else cls.current.astimezone(tz)


def test_recovery_probe_is_suppressed_before_its_due_time():
    async def probe():
        clear_fixed_port_degraded_state("us_mock")
        enter_fixed_port_degraded_state(
            "us_mock", "rest", now=datetime.now(timezone.utc) + timedelta(seconds=1),
        )
        service = DispatchClearanceService("us_mock")
        service.observe_active_profile(("SOXL",), 0)
        engine = _engine(service, _snapshot(clear=True), enabled=True)
        try:
            await service.probe_if_due(engine, "SOXL")
            engine._build_reconciliation_clearance_snapshot.assert_not_awaited()
        finally:
            clear_fixed_port_degraded_state("us_mock")

    asyncio.run(probe())


def test_recovery_probe_clears_when_due_and_fully_reconciled():
    async def probe():
        clear_fixed_port_degraded_state("us_mock")
        enter_fixed_port_degraded_state("us_mock", "rest", now=_FixedDateTime.current)
        service = DispatchClearanceService("us_mock")
        service.observe_active_profile(("SOXL",), 0)
        engine = _engine(service, _snapshot(clear=True), enabled=True)
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


def test_recovery_probe_retains_degraded_state_and_advances_schedule_when_blocked():
    async def probe():
        clear_fixed_port_degraded_state("us_mock")
        enter_fixed_port_degraded_state("us_mock", "rest", now=_FixedDateTime.current)
        service = DispatchClearanceService("us_mock")
        service.observe_active_profile(("SOXL",), 0)
        engine = _engine(service, _snapshot(clear=False), enabled=True)
        try:
            with patch("src.core.engine.datetime", _FixedDateTime):
                await service.probe_if_due(engine, "SOXL")
            state = get_fixed_port_degraded_state("us_mock")
            assert state is not None
            assert state.next_recovery_probe_at == _FixedDateTime.current + timedelta(seconds=90)
        finally:
            clear_fixed_port_degraded_state("us_mock")

    asyncio.run(probe())


def test_concurrent_recovery_probes_produce_one_attempt():
    async def build_snapshot(symbol, *, max_balance_age_sec):
        await asyncio.sleep(0)
        return _snapshot(clear=False)

    async def probe():
        clear_fixed_port_degraded_state("us_mock")
        enter_fixed_port_degraded_state("us_mock", "rest", now=_FixedDateTime.current)
        service = DispatchClearanceService("us_mock")
        service.observe_active_profile(("SOXL",), 0)
        engine = _engine(service, _snapshot(clear=False), enabled=True)
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


def test_recovery_probe_logs_snapshot_failures_without_propagating():
    async def probe():
        clear_fixed_port_degraded_state("us_mock")
        enter_fixed_port_degraded_state("us_mock", "rest", now=_FixedDateTime.current)
        service = DispatchClearanceService("us_mock")
        service.observe_active_profile(("SOXL",), 0)
        engine = _engine(service, _snapshot(clear=True), enabled=True)
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
