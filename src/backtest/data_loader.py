"""과거 시세 로딩. 로컬 CSV 우선, 필요 시 ka10081(주식일봉차트조회) 연동 지점을 남겨둡니다."""
from __future__ import annotations

import pandas as pd


def load_csv(path: str) -> pd.DataFrame:
    """columns: date, open, high, low, close, volume 를 기대합니다."""
    df = pd.read_csv(path, parse_dates=["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df


async def load_from_kiwoom_daily_chart(client, symbol: str, base_date: str) -> pd.DataFrame:
    """ka10081(국내) 또는 미국 차트 API로 일봉을 가져와 DataFrame으로 변환하는 자리.
    실계좌 REST 호출 없이도 백테스트가 동작하도록 실거래 클라이언트와 완전히 분리되어 있습니다.
    """
    raise NotImplementedError("필요 시 KiwoomClient에 차트 조회 메서드를 추가해 연동하세요.")
