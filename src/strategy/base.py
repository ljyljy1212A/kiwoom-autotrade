"""전략 인터페이스. 매수/매도/손절/익절 조건을 분리해서 구현하도록 강제합니다."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    TAKE_PROFIT = "TAKE_PROFIT"
    HOLD = "HOLD"


@dataclass
class OrderIntent:
    action: Action
    symbol: str
    qty: int
    price: float | None = None
    order_type: str = "00"
    reason: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class MarketSnapshot:
    symbol: str
    last_price: float
    timestamp: str


@dataclass
class PositionState:
    symbol: str
    qty: float = 0.0
    avg_price: float = 0.0
    step: int = 0  # 현재 그리드 단계 (0 = 미보유)
    realized_pnl: float = 0.0


class Strategy(ABC):
    """모든 전략은 이 인터페이스를 구현합니다."""

    @abstractmethod
    def check_buy(self, snapshot: MarketSnapshot, position: PositionState) -> OrderIntent | None:
        ...

    @abstractmethod
    def check_sell(self, snapshot: MarketSnapshot, position: PositionState) -> OrderIntent | None:
        ...

    @abstractmethod
    def check_take_profit(self, snapshot: MarketSnapshot, position: PositionState) -> OrderIntent | None:
        ...

    def evaluate(self, snapshot: MarketSnapshot, position: PositionState) -> OrderIntent | None:
        """우선순위: 손절 > 익절(그리드매도) > 추가매수. 필요 시 하위 클래스에서 override."""
        for check in (self.check_take_profit, self.check_sell, self.check_buy):
            intent = check(snapshot, position)
            if intent is not None:
                return intent
        return None
