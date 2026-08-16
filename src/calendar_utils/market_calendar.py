"""시장별(해외 US / 국내 KR) 거래일·개장시간 캘린더 - 주말/공휴일 자동 스킵 (요구사항 [7]).

- US: NYSE 캘린더 사용 (SOXL 등 NASDAQ 상장 종목도 NYSE 캘린더와 개장일이 동일).
- KR: KRX(한국거래소) 캘린더 사용. pandas_market_calendars 가 "XKRX" 캘린더를 제공하면 그것을 사용하고,
  버전 문제 등으로 못 찾으면 공휴일을 요일만으로는 알 수 없으므로 경고 로그를 남기고
  각 시장의 정규장 시간 + 주말 제외 폴백으로 동작한다 (KR: 09:00~15:30 KST, US: 09:30~16:00 ET).
  이 폴백 상태에서는 공휴일(설/추석/추수감사절 등)이 반영되지 않으므로 운영 전
  pandas_market_calendars 를 XKRX/NYSE 지원 버전으로 맞추는 것을 권장한다.
- 알 수 없는 market 값(향후 확장 등)은 캘린더도 폴백 시간표도 없으므로, 안전을 위해 항상
  "휴장"으로 판단한다 (실수로 매매 로직이 도는 것보다 거래를 못 하는 쪽이 안전).
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

_CALENDAR_NAME = {"US": "NYSE", "KR": "XKRX"}
# 폴백(캘린더 로딩 실패) 시 사용하는 시장별 정규장 시간 + 타임존. 공휴일은 반영되지 않는다.
_FALLBACK_HOURS = {
    "KR": (time(9, 0), time(15, 30), ZoneInfo("Asia/Seoul")),
    "US": (time(9, 30), time(16, 0), ZoneInfo("America/New_York")),
}
_log = logging.getLogger(__name__)


class MarketCalendar:
    def __init__(self, market: str = "US"):
        self.market = market
        # Regular session is the safe default.  Extended hours are broker and
        # instrument dependent, so they require an explicit deployment opt-in.
        self.allow_extended_hours = (
            market == "US" and os.environ.get("US_EXTENDED_HOURS_ENABLED", "false").lower() == "true"
        )
        self.calendar = None
        name = _CALENDAR_NAME.get(market)
        if name:
            try:
                self.calendar = mcal.get_calendar(name)
            except Exception:  # pandas_market_calendars 버전에 따라 XKRX/NYSE 미지원 가능
                _log.warning(
                    "'%s' 캘린더를 불러오지 못했습니다. 주말+정규장시간(%s) 폴백으로 동작하며 "
                    "공휴일은 반영되지 않을 수 있습니다. pandas_market_calendars 를 최신 버전으로 "
                    "업그레이드하세요.", name, market,
                )
        elif market not in _FALLBACK_HOURS:
            _log.warning(
                "알 수 없는 market='%s' — 캘린더/폴백 시간표가 없어 항상 휴장으로 판단합니다.", market,
            )

    def is_trading_day(self, d: date | None = None) -> bool:
        d = d or date.today()
        if self.calendar is not None:
            schedule = self.calendar.schedule(start_date=d, end_date=d)
            return not schedule.empty
        if self.market not in _FALLBACK_HOURS:
            return False  # 알 수 없는 시장은 안전하게 휴장 취급
        return d.weekday() < 5

    def is_market_open_now(self) -> bool:
        """정규장 시간 여부 (캘린더 로딩 성공 시 거래소 표준시로 정확히 판단)."""
        if self.calendar is not None:
            now = datetime.now(tz=self.calendar.tz)
            sched = self.calendar.schedule(start_date=now.date(), end_date=now.date())
            if sched.empty:
                return False
            open_t, close_t = sched.iloc[0]["market_open"], sched.iloc[0]["market_close"]
            return open_t <= now <= close_t

        # 캘린더 폴백: 공휴일은 반영 못하지만, 최소한 요일+정규장 시간 밖에는
        # 어떤 market 값이든 항상 매매 로직이 돌지 않도록 한다 (fail-safe).
        fallback = _FALLBACK_HOURS.get(self.market)
        if fallback is None:
            return False  # 알 수 없는 시장은 안전하게 휴장 취급
        if not self.is_trading_day():
            return False
        open_t, close_t, tz = fallback
        now = datetime.now(tz=tz).time()
        return open_t <= now <= close_t

    def session_name_now(self) -> str:
        """Return a display/safety session label; only REGULAR is tradable by default."""
        if self.market != "US":
            return "REGULAR" if self.is_market_open_now() else "CLOSED"
        now = datetime.now(tz=ZoneInfo("America/New_York"))
        if self.is_market_open_now():
            return "REGULAR"
        if now.weekday() < 5 and self.allow_extended_hours:
            current = now.time()
            if time(4, 0) <= current < time(9, 30):
                return "PRE"
            if time(16, 0) < current <= time(20, 0):
                return "POST"
        return "CLOSED"
