"""액면분할/병합 자동 감시 (요구사항 [7]).

매일 08:00: 종목 기본정보(ka10001)의 기준가/발행주식수 등을 전일 스냅샷과 비교해
비율이 크게 어긋나면(분할/병합 의심) -> 주문 정지 + 알림 -> 평단/수량 자동 보정 -> 사용자 확인 후 재개.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SplitEvent:
    symbol: str
    ratio: float  # 예: 1:4 분할이면 4.0, 1:1 병합(1주->0.5주 개념의 병합)이면 0.5 등


class SplitMonitor:
    def __init__(self, kiwoom_client, position_store, notifier, logger, threshold_pct: float = 30.0):
        """
        threshold_pct: 전일 종가 대비 이 비율(%) 이상 괴리가 발생하면 분할/병합 의심으로 판단.
        실제로는 거래소 공시(회사정보/이벤트 API)를 함께 조회하는 것을 강력히 권장합니다.
        """
        self.client = kiwoom_client
        self.position_store = position_store
        self.notifier = notifier
        self.logger = logger
        self.threshold_pct = threshold_pct
        self._last_close: dict[str, float] = {}

    async def check_symbol(self, symbol: str, prev_close: float, today_open: float) -> SplitEvent | None:
        if prev_close <= 0:
            return None
        gap_pct = abs(today_open - prev_close) / prev_close * 100
        if gap_pct < self.threshold_pct:
            return None

        ratio = round(prev_close / today_open, 4) if today_open > 0 else None
        self.logger.warning(f"[{symbol}] 액면분할/병합 의심: 전일종가 {prev_close} -> 금일시가 {today_open} "
                             f"(추정비율 {ratio})")
        return SplitEvent(symbol=symbol, ratio=ratio or 1.0)

    async def handle_event(self, event: SplitEvent, halt_callback, resume_callback):
        """1) 주문 정지 -> 2) 알림 -> 3) 평단/수량 보정 -> 4) 사용자 확인 후 재개."""
        await halt_callback(event.symbol)
        await self.notifier.notify_error(
            f"⚠️ {event.symbol} 액면분할/병합 감지 (추정비율 {event.ratio}).\n"
            f"매수 주문을 정지했습니다. 평단/수량 보정 후 텔레그램으로 재개 승인을 요청드립니다."
        )

        pos = self.position_store.get(event.symbol)
        if pos:
            pos.qty = pos.qty * event.ratio
            pos.avg_price = pos.avg_price / event.ratio
            self.position_store.save(pos)
            self.logger.info(f"{event.symbol} 평단/수량 보정 완료: qty={pos.qty}, avg_price={pos.avg_price}")

        # 실제 재개는 텔레그램 승인 콜백(resume_callback)을 통해 사용자가 명시적으로 트리거
        await self.notifier.safe_send(f"{event.symbol} 보정 완료. '▶️ 매수 재개' 버튼으로 재개해주세요.")
