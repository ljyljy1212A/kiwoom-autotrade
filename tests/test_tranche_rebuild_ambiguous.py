import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from src.core.engine import AccountEngine, _AccountBalanceGate
from src.strategy.base import PositionState


class _Ledger:
    def __init__(self, *, pending=None, open_qty=None, rows=None):
        self.pending = pending or []
        self.open_qty = open_qty or {}
        self.rows = rows or []

    def pending_orders(self, _symbol):
        return self.pending

    def open_tranche_qty(self, _symbol, step):
        return float(self.open_qty.get(step, 0))

    def ledger_rows(self, _symbol):
        return self.rows

    def has_unresolved_orders(self, _symbol):
        return False


class _Notifier:
    def __init__(self):
        self.notify_order = AsyncMock()
        self.notify_fill = AsyncMock()
        self.notify_error = AsyncMock()
        self.notify_balance_change = AsyncMock()
        self.notify_symbol_closed = AsyncMock()
        self.notify_symbol_reopened = AsyncMock()


def _engine(tmp_path, *, raw_balance=None, balance_only=False, position_qty=3,
            step_qty=None, ledger=None, lifecycle=None):
    engine = object.__new__(AccountEngine)
    strategy = SimpleNamespace(
        symbol="000490",
        step_qty=dict(step_qty or {2: 3}),
        step_prices={step: 100.0 + step for step in (step_qty or {2: 3})},
        max_step=3,
    )
    logger = Mock()
    engine.ctx = SimpleNamespace(
        account_id="isolated_kr_mock",
        strategy=strategy,
        client=SimpleNamespace(market="KR"),
        currency="KRW",
        reporting_currency="KRW",
        logger=logger,
    )
    engine.data_dir = Path(tmp_path)
    engine._balance_only = balance_only
    engine._balance_gate = _AccountBalanceGate()
    engine._balance_gate.engines.add(engine)
    engine._trading_paused = False
    engine._pause_reason = ""
    engine._tranche_sell_paused = False
    engine._dashboard_auto_buy = False
    engine._dashboard_auto_sell = False
    engine._dashboard_profile_allowed = True
    engine._dashboard_symbol = "000490"
    engine._lifecycle_pending_adoption = False
    engine._symbol_lifecycles = lifecycle or {}
    engine._tranche_bases = {}
    engine._fx_rate_krw = None
    engine._broker_fill_catchup_qty = {}
    engine._broker_fill_catchup_warned = set()
    engine._closed_symbols_blocked = set()
    engine.ctx.position = PositionState(symbol="000490", qty=position_qty, avg_price=101.0, step=2)
    engine.ledger = ledger or _Ledger()
    engine._store_tranche_base = Mock()
    engine._publish_passive_balance_snapshot = Mock()
    engine._refresh_open_lifecycle_manual_basis = Mock()
    engine._validated_manual_tranche_base = Mock(return_value=101.0)
    engine._orphan_cleaner = Mock()
    engine._orphan_cleaner.sweep.return_value = []
    engine.telegram = _Notifier()
    engine._shared_broker_balance = AsyncMock(return_value=(raw_balance if raw_balance is not None else {
        "acnt_evlt_remn_indv_tot": [{
            "stk_cd": "000490", "rmnd_qty": "2", "pur_pric": "100",
            "cur_prc": "100",
        }],
    }))
    return engine


def _run(engine):
    asyncio.run(engine._reconcile_balance())


def test_ambiguous_rebuild_pauses_before_mutating_tranche_state(tmp_path):
    engine = _engine(
        tmp_path,
        step_qty={2: 3},
        ledger=_Ledger(
            open_qty={2: 3},
            rows=[{"type": "buy", "step": 2, "qty": 3, "price": 103}],
        ),
    )
    before_qty = deepcopy(engine.ctx.strategy.step_qty)
    before_prices = deepcopy(engine.ctx.strategy.step_prices)
    before_position = deepcopy(engine.ctx.position)

    _run(engine)

    assert engine._pause_reason == "tranche_rebuild_ambiguous"
    assert engine._trading_paused is True
    assert engine._tranche_sell_paused is True
    assert engine.ctx.strategy.step_qty == before_qty
    assert engine.ctx.strategy.step_prices == before_prices
    assert engine.ctx.position == before_position
    engine.telegram.notify_error.assert_awaited_once()


def test_balance_only_does_not_trigger_ambiguous_pause(tmp_path):
    engine = _engine(tmp_path, balance_only=True)
    _run(engine)
    assert engine._pause_reason == ""


def test_unrecognized_balance_does_not_trigger_ambiguous_pause(tmp_path):
    engine = _engine(tmp_path, raw_balance={})
    _run(engine)
    assert engine._pause_reason == ""


def test_pending_fill_does_not_trigger_ambiguous_pause(tmp_path):
    engine = _engine(tmp_path, ledger=_Ledger(pending=[object()]))
    _run(engine)
    assert engine._pause_reason == ""


def test_catchup_snapshot_does_not_trigger_ambiguous_pause(tmp_path):
    engine = _engine(tmp_path)
    engine._broker_fill_catchup_qty["000490"] = 3
    _run(engine)
    assert engine._pause_reason == ""


def test_zero_broker_quantity_is_not_ambiguous_rebuild(tmp_path):
    engine = _engine(
        tmp_path,
        raw_balance={"acnt_evlt_remn_indv_tot": []},
    )
    _run(engine)
    assert engine._pause_reason == "broker_quantity_unattributed"


def test_stale_but_provable_snapshot_does_not_trigger_ambiguous_pause(tmp_path):
    engine = _engine(
        tmp_path,
        lifecycle={"000490": {"status": "open", "manual_qty": 3}},
    )
    _run(engine)
    assert engine._pause_reason == ""


def test_existing_step_one_row_does_not_trigger_ambiguous_pause(tmp_path):
    engine = _engine(
        tmp_path,
        step_qty={1: 1, 2: 3},
        ledger=_Ledger(
            open_qty={1: 1, 2: 3},
            rows=[
                {"type": "buy", "step": 1, "qty": 1, "price": 100},
                {"type": "buy", "step": 2, "qty": 3, "price": 103},
            ],
        ),
    )
    _run(engine)
    assert engine._pause_reason != "tranche_rebuild_ambiguous"


def test_known_quantity_not_above_broker_quantity_does_not_rebuild(tmp_path):
    engine = _engine(tmp_path, position_qty=1, step_qty={2: 1})
    _run(engine)
    assert engine._pause_reason != "tranche_rebuild_ambiguous"
