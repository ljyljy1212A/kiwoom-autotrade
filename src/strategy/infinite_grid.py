"""무한매수 그리드 전략 (요구사항 [8]).

예: 1차 매수 이후 -3% 하락 시 2차 매수, 2차 매수값 대비 -4% 하락 시 3차 매수,
    3차 매수 후 +4% 상승 시 3차 매수 물량만 매도.. 정규장 동안 반복.

첨부된 "자동전략 설정" 대시보드 화면과 동일한 구조(1~10차, 단계별 하락률/매도익절률)를
config/strategy_config.example.json 스키마로 그대로 반영합니다.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.strategy.base import Action, MarketSnapshot, OrderIntent, PositionState, Strategy


@dataclass
class GridStep:
    step: int
    drop_pct: float  # 이전 단계 대비 하락률 (음수)
    amount: float     # 이 단계에서 매수할 금액


@dataclass
class SellStep:
    step: int
    profit_pct: float  # 해당 단계 매수가 대비 목표 수익률


class InfiniteGridStrategy(Strategy):
    def __init__(self, config: dict):
        self.symbol = config["symbol"]
        # The standard workflow is an HTS/manual first tranche. Treat the
        # mode as an execution permission, not as a display-only preference.
        self.first_buy_mode = str(config.get("first_buy", {}).get("mode", "manual")).strip().lower()
        self.first_buy_amount = config["first_buy"]["amount"]
        self.buy_steps = [GridStep(**s) for s in config["buy_steps"]]
        self.sell_steps = [SellStep(**s) for s in config["sell_steps"]]
        # buy_steps describes transitions (1→2, 2→3, ...), so N transitions
        # permit N + 1 purchase tranches.
        self.max_step = len(self.buy_steps) + 1
        self.risk = dict(config.get("risk", {}))
        # Decimal, charged per side. A requested profit percentage means net
        # profit after the entry and exit commissions, not gross price change.
        self.commission_rate = max(0.0, float(config.get("commission_rate", 0.0) or 0.0))
        self._buy_order_type = str(config.get("auto_buy", {}).get("order_type", "00"))
        self._sell_order_type = str(config.get("auto_sell", {}).get("order_type", "00"))
        # step 별 매수가를 기록해 매도 시 "해당 단계 물량만" 청산할 수 있도록 함
        self.step_prices: dict[int, float] = {}
        self.step_qty: dict[int, int] = {}

    # ------------------------------------------------------------------
    def check_buy(self, snapshot: MarketSnapshot, position: PositionState) -> OrderIntent | None:
        price = snapshot.last_price

        # 최초 진입 (수동/자동)
        if position.step == 0:
            # A manual first tranche must never be synthesized by the grid,
            # regardless of the configured amount.
            if self.first_buy_mode != "auto":
                return None
            qty = self._amount_to_qty(self.first_buy_amount, price)
            if qty <= 0:
                return None
            return OrderIntent(
                action=Action.BUY, symbol=self.symbol, qty=qty, price=price, order_type=self._buy_order_type,
                reason="최초(1차) 매수", meta={"step": 1},
            )

        if position.step >= self.max_step:
            return None  # 마지막 단계까지 도달, 추가 매수 없음

        # buy_steps[i] 는 "현재 i+1차 보유 상태에서 i+2차로 넘어가기 위한 하락률"을 의미합니다.
        # 즉 position.step=1(1차 보유)일 때 다음 트리거는 buy_steps[0](= step 필드값 1, "1차 매수 이후 -3%").
        next_step = self.buy_steps[position.step - 1]
        # Every next tranche is measured from the immediately preceding
        # tranche's confirmed buy/fill price.  Never fall back to the account
        # average, because that would change the intended grid trigger after
        # multiple purchases.
        base_price = self.step_prices.get(position.step)
        if base_price is None or base_price <= 0:
            return None
        trigger_price = base_price * (1 + next_step.drop_pct / 100)

        if price <= trigger_price:
            qty = self._amount_to_qty(next_step.amount, price)
            if qty <= 0:
                return None
            target_step = position.step + 1
            return OrderIntent(
                action=Action.BUY, symbol=self.symbol, qty=qty, price=price, order_type=self._buy_order_type,
                reason=f"{target_step}차 매수 (기준가 {base_price:.2f} 대비 {next_step.drop_pct}%)",
                meta={"step": target_step},
            )
        return None

    # ------------------------------------------------------------------
    def check_sell(self, snapshot: MarketSnapshot, position: PositionState) -> OrderIntent | None:
        """가장 최근 단계 물량만 목표 수익률 도달 시 매도 (그리드 특성상 최신 단계부터 청산)."""
        if position.step == 0:
            return None

        current_step_idx = position.step - 1
        if current_step_idx < 0 or current_step_idx >= len(self.sell_steps):
            return None

        sell_cfg = self.sell_steps[current_step_idx]
        step_buy_price = self.step_prices.get(position.step)
        step_qty = self.step_qty.get(position.step, 0)
        if not step_buy_price or step_qty <= 0:
            return None

        target_price = self.sell_target_price(position.step)
        if target_price is None:
            return None
        if snapshot.last_price >= target_price:
            return OrderIntent(
                action=Action.SELL, symbol=self.symbol, qty=step_qty, price=snapshot.last_price, order_type=self._sell_order_type,
                reason=f"{position.step}차 물량 익절 (+{sell_cfg.profit_pct}%)",
                meta={"step": position.step, "sell_only_step": True},
            )
        return None

    def sell_target_price(self, step: int) -> float | None:
        """Return the immutable per-tranche profit target, if it is known."""
        idx = step - 1
        price = self.step_prices.get(step)
        if idx < 0 or idx >= len(self.sell_steps) or not price or price <= 0:
            return None
        gross_profit = self.sell_steps[idx].profit_pct / 100
        # net proceeds = target * (1 - fee); total entry cost = buy * (1 + fee)
        # Solve net proceeds = entry cost * (1 + requested net profit).
        denominator = 1 - self.commission_rate
        if denominator <= 0:
            return None
        return price * (1 + self.commission_rate) * (1 + gross_profit) / denominator

    # ------------------------------------------------------------------
    def check_stop_loss(self, snapshot: MarketSnapshot, position: PositionState) -> OrderIntent | None:
        # Legacy hook retained for compatibility with older callers; automatic
        # stop-loss order generation has been removed.
        return None
        max_loss_pct = None
        if max_loss_pct is None or position.qty <= 0 or position.avg_price <= 0:
            return None
        loss_pct = (snapshot.last_price - position.avg_price) / position.avg_price * 100
        if loss_pct <= -abs(max_loss_pct):
            return OrderIntent(
                action=Action.STOP_LOSS, symbol=self.symbol, qty=int(position.qty), price=snapshot.last_price,
                reason=f"전체 손절 (평단 대비 {loss_pct:.2f}%)",
            )
        return None

    def check_take_profit(self, snapshot: MarketSnapshot, position: PositionState) -> OrderIntent | None:
        # 무한 그리드는 단계별 매도(check_sell)가 익절 역할을 겸함
        return None

    # ------------------------------------------------------------------
    def on_filled(self, action: Action, step: int, qty: int, price: float):
        """주문 체결 후 전략 내부 상태(단계별 매수가/수량) 갱신. OrderExecutor 가 호출."""
        if action == Action.BUY:
            old_qty = self.step_qty.get(step, 0)
            old_price = self.step_prices.get(step, 0)
            self.step_qty[step] = old_qty + qty
            self.step_prices[step] = ((old_qty * old_price) + qty * price) / self.step_qty[step]
        elif action == Action.SELL:
            remaining = max(0, self.step_qty.get(step, 0) - qty)
            self.step_qty[step] = remaining
            if remaining == 0:
                self.step_prices.pop(step, None)

    @staticmethod
    def _amount_to_qty(amount: float, price: float) -> int:
        if price <= 0:
            return 0
        return int(amount // price)
