"""InfiniteGridStrategy 기본 동작 단위 테스트 (실거래 없이 로직만 검증)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.strategy.base import MarketSnapshot, PositionState, Action
from src.strategy.infinite_grid import InfiniteGridStrategy


def _load_strategy():
    cfg_path = Path(__file__).resolve().parents[1] / "config" / "strategy_config.example.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    return InfiniteGridStrategy(cfg), cfg


def test_first_buy_triggers_when_no_position():
    strategy, cfg = _load_strategy()
    # This test exercises the explicit auto-first-tranche mode. The production
    # dashboard uses manual mode, which must not create tranche 1 itself.
    cfg["first_buy"]["mode"] = "auto"
    strategy = InfiniteGridStrategy(cfg)
    pos = PositionState(symbol=cfg["symbol"])
    snap = MarketSnapshot(symbol=cfg["symbol"], last_price=70.0, timestamp="t0")

    intent = strategy.evaluate(snap, pos)
    assert intent is not None
    assert intent.action == Action.BUY
    assert intent.meta["step"] == 1


def test_second_buy_triggers_on_drop():
    strategy, cfg = _load_strategy()
    pos = PositionState(symbol=cfg["symbol"], qty=60, avg_price=70.0, step=1)
    strategy.step_prices[1] = 70.0
    strategy.step_qty[1] = 60

    # -3% 하락 전: 매수 트리거 없음
    snap_before = MarketSnapshot(symbol=cfg["symbol"], last_price=69.0, timestamp="t1")
    assert strategy.check_buy(snap_before, pos) is None

    # -3% 하락 도달: 2차 매수 트리거
    snap_trigger = MarketSnapshot(symbol=cfg["symbol"], last_price=70.0 * 0.97, timestamp="t2")
    intent = strategy.check_buy(snap_trigger, pos)
    assert intent is not None
    assert intent.meta["step"] == 2


def test_sell_triggers_on_profit_target():
    strategy, cfg = _load_strategy()
    pos = PositionState(symbol=cfg["symbol"], qty=60, avg_price=70.0, step=1)
    strategy.step_prices[1] = 70.0
    strategy.step_qty[1] = 60

    target = strategy.sell_target_price(1)
    assert target is not None
    snap = MarketSnapshot(symbol=cfg["symbol"], last_price=target, timestamp="t3")
    intent = strategy.check_sell(snap, pos)
    assert intent is not None
    assert intent.action == Action.SELL
    assert intent.qty == 60


def test_second_tranche_never_sells_below_its_own_target():
    strategy, cfg = _load_strategy()
    pos = PositionState(symbol=cfg["symbol"], qty=17, avg_price=5884.0, step=2)
    strategy.step_prices = {1: 5950.0, 2: 5880.0}
    strategy.step_qty = {1: 1, 2: 16}

    # A price below the second tranche's entry is never a tranche-profit sell,
    # even though the account contains an older manual tranche as well.
    snap = MarketSnapshot(symbol=cfg["symbol"], last_price=5860.0, timestamp="t4")
    assert strategy.check_sell(snap, pos) is None


def test_sell_target_includes_entry_and_exit_commissions():
    cfg = {
        "symbol": "TEST", "commission_rate": 0.001,
        "first_buy": {"amount": 1000}, "buy_steps": [],
        "sell_steps": [{"step": 1, "profit_pct": 1}],
    }
    strategy = InfiniteGridStrategy(cfg)
    strategy.step_prices[1] = 100.0
    # Net proceeds after the exit fee equal entry cost after entry fee plus 1%.
    target = strategy.sell_target_price(1)
    assert target is not None
    assert round(target * (1 - 0.001), 8) == round(100 * (1 + 0.001) * 1.01, 8)


if __name__ == "__main__":
    test_first_buy_triggers_when_no_position()
    test_second_buy_triggers_on_drop()
    test_sell_triggers_on_profit_target()
    print("모든 테스트 통과")
