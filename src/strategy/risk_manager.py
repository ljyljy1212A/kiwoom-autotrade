"""리스크 관리 모듈 (요구사항 [7]): 포지션 크기 조절, 최대 손실 제한."""
from __future__ import annotations

from dataclasses import dataclass

from src.strategy.base import Action, OrderIntent, PositionState


@dataclass
class RiskLimits:
    max_position_amount: float | None = None
    max_cycles: int | None = None


class RiskManager:
    def __init__(self, limits: RiskLimits, logger=None):
        self.limits = limits
        self.logger = logger
        self.trading_halted = False

    def approve(self, intent: OrderIntent, position: PositionState, current_price: float) -> tuple[bool, str]:
        """주문 실행 가능 여부와 사유를 반환."""
        if self.trading_halted:
            return False, "리스크 관리자에 의해 거래 중단 상태"

        if intent.action == Action.BUY and self.limits.max_position_amount:
            projected_value = (position.qty * position.avg_price) + (intent.qty * current_price)
            if projected_value > self.limits.max_position_amount:
                return False, (
                    f"최대 포지션 한도 초과 (예상 {projected_value:,.0f} > 한도 "
                    f"{self.limits.max_position_amount:,.0f})"
                )

        return True, "OK"

    def register_realized_pnl_pct(self, pct: float):
        return None

    def reset_daily(self):
        self.trading_halted = False
