"""백테스트 엔진.

실시간 매매(src/core/engine.py)와 완전히 분리되어 있으며,
동일한 Strategy 인터페이스(src/strategy/base.py)를 재사용해 로직 일관성을 보장합니다.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.strategy.base import Action, MarketSnapshot, PositionState, Strategy


@dataclass
class Trade:
    date: str
    action: str
    qty: int
    price: float
    reason: str


@dataclass
class BacktestResult:
    trades: list[Trade]
    final_position: PositionState
    equity_curve: pd.DataFrame


class Backtester:
    def __init__(self, strategy: Strategy, symbol: str, initial_cash: float = 200_000):
        self.strategy = strategy
        self.symbol = symbol
        self.cash = initial_cash
        self.position = PositionState(symbol=symbol)
        self.trades: list[Trade] = []
        self.equity_rows = []

    def run(self, price_df: pd.DataFrame) -> BacktestResult:
        """price_df: columns=[date, close] (일/분봉 어느 쪽이든 사용 가능)."""
        for _, row in price_df.iterrows():
            snapshot = MarketSnapshot(symbol=self.symbol, last_price=float(row["close"]),
                                       timestamp=str(row["date"]))
            intent = self.strategy.evaluate(snapshot, self.position)
            if intent is not None:
                self._apply(intent, snapshot)

            equity = self.cash + self.position.qty * snapshot.last_price
            self.equity_rows.append({"date": row["date"], "equity": equity, "close": snapshot.last_price})

        equity_curve = pd.DataFrame(self.equity_rows)
        return BacktestResult(trades=self.trades, final_position=self.position, equity_curve=equity_curve)

    def _apply(self, intent, snapshot: MarketSnapshot):
        cost = intent.qty * (intent.price or snapshot.last_price)
        if intent.action in (Action.BUY,):
            if cost > self.cash:
                return  # 현금 부족 시 매수 스킵 (실거래의 RiskManager 역할을 간이 대체)
            self.cash -= cost
            total_cost = self.position.qty * self.position.avg_price + cost
            self.position.qty += intent.qty
            self.position.avg_price = total_cost / self.position.qty if self.position.qty else 0
            self.position.step = intent.meta.get("step", self.position.step + 1)
            self.strategy.on_filled(Action.BUY, self.position.step, intent.qty, snapshot.last_price)
        else:
            self.cash += cost
            realized = (snapshot.last_price - self.position.avg_price) * intent.qty
            self.position.realized_pnl += realized
            self.position.qty = max(0, self.position.qty - intent.qty)
            step = intent.meta.get("step", self.position.step)
            if intent.meta.get("sell_only_step"):
                self.position.step = max(0, self.position.step - 1)
            if self.position.qty == 0:
                self.position.step = 0
            self.strategy.on_filled(Action.SELL, step, intent.qty, snapshot.last_price)

        self.trades.append(Trade(date=snapshot.timestamp, action=intent.action.value,
                                  qty=intent.qty, price=intent.price or snapshot.last_price,
                                  reason=intent.reason))
