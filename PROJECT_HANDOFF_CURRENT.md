# 키움 자동매매 대시보드 — 추가 작업 인수인계

작성일: 2026-08-11 (국내주식/KRX 매매 지원 + 실시간 시세 연동 반영)

## 이번 작업에서 반영한 사항 (2026-08-11, 세 번째 — KRX 캘린더 폴백 검증/수정)

**요청 사항**: KRX 캘린더 폴백 로직 검증 및 수정

1. **실제 검증**: `pandas-market-calendars==5.4.0`(요구 버전 `>=4.6.1` 충족)을 설치해
   `XKRX` 캘린더가 정상 로드되는 것을 확인했습니다. 2026년 설날(2/17), 추석 연휴(9/24~27),
   광복절(8/15) 등이 거래일에서 정상적으로 제외되는 것도 직접 확인했습니다
   (`requirements.txt`의 버전 핀은 이미 올바르게 설정되어 있었습니다).
2. **버그 발견 및 수정**: `is_market_open_now()`의 캘린더 로딩 실패 폴백 분기가
   **KR이 아닌 시장(사실상 US)에서는 시간 체크 없이 항상 `True`를 반환**하고 있었습니다.
   즉 XKRX/NYSE 캘린더 로딩에 실패하면(버전 문제, 네트워크 문제 등) 미국 계좌는 새벽에도
   "정규장"으로 판단해 매매 로직이 돌 수 있는 상태였습니다 — 이전 라운드에서 "고쳤다"고
   기록된 것과 **같은 종류의 버그가 US 폴백 경로에 그대로 남아있던 것**입니다.
   (`src/calendar_utils/market_calendar.py`)
   - 수정: US 폴백용 정규장 시간(09:30~16:00 America/New_York)을 추가하고,
     KR/US 각각 자신의 타임존·정규장 시간으로 체크하도록 `_FALLBACK_HOURS` 딕셔너리로 일반화.
   - 추가 안전장치: `market` 값이 `"US"`/`"KR"` 둘 다 아닌 경우(향후 확장 등) 캘린더도
     폴백 시간표도 없으므로 `is_trading_day()`/`is_market_open_now()` 모두 **항상 휴장(`False`)**으로
     판단하도록 변경했습니다 (이전에는 알 수 없는 시장이 `is_trading_day()`에서 평일이면 `True`,
     `is_market_open_now()`에서도 `True`가 나올 수 있는 fail-open 상태였습니다 → fail-safe로 전환).
   - 로그 메시지도 시장별로 다르게 나오도록 정리 (기존에는 US 폴백 시에도 "설/추석 등 국내
     공휴일"이라는 문구가 찍혀 혼란의 소지가 있었음).
3. **검증 방법**: `unittest.mock`으로 `mcal.get_calendar()`가 예외를 던지도록 강제해
   폴백 경로를 인위적으로 활성화한 뒤, 시각을 고정(monkeypatch)해 새벽 시간(03:00)과
   장중 시간(11:00)에 대해 KR/US 각각 `is_market_open_now()`가 올바르게 `False`/`True`를
   반환하는지, 그리고 알 수 없는 market 값이 항상 `False`를 반환하는지 확인했습니다.
   (이 테스트 스크립트도 실시간 시세 라운드와 마찬가지로 저장소에 커밋하지 않았습니다 —
   `tests/test_market_calendar.py`로 정식 테스트를 추가하는 것을 권장합니다.)
   기존 `tests/test_infinite_grid.py`도 계속 통과합니다.

## 이번 작업에서 반영한 사항 (2026-08-11, 두 번째 — 실시간 시세 연동)

**요청 사항**: `src/main.py`의 `make_price_feed()` 자리 구현 (`NotImplementedError` 제거)

1. **`src/core/realtime_feed.py` 신규 작성**
   - `KiwoomRealtimeFeed`: 계좌(=`KiwoomClient`)별로 WebSocket에 연결해 `LOGIN` → `REG`(종목 구독)
     → `REAL`(체결 푸시) 프로토콜을 처리하고, 최신 체결가를 메모리 캐시에 저장합니다.
     연결이 끊기면 지수 백오프로 자동 재연결하며, 재연결 시 기존 구독 종목을 자동으로 다시 등록합니다.
     PING 주기 전송으로 연결을 유지합니다.
   - `PriceFeed`: 위 WebSocket 캐시를 우선 사용하고, 캐시가 없거나 오래되면(`KIWOOM_PRICE_MAX_STALENESS_SEC`)
     REST 폴백(`KiwoomClient.get_quote_price()`)으로 전환하는 합성 시세 소스. `AccountEngine`에 넘기는
     `price_feed(symbol) -> float` 콜러블을 만듭니다. `PRICE_FEED_MODE` 환경변수로 `auto`(기본,
     WS+REST) / `ws`(WS 전용) / `rest`(REST 폴링 전용) 중 선택 가능합니다.
   - 페이크 WebSocket 서버로 LOGIN/REG/REAL 왕복, 재연결 후 재구독, auto 모드의 REST 폴백 전환을
     로컬에서 검증했습니다 (단위 테스트는 저장소에 포함하지 않았고, 이 인수인계 작업 중 임시로 실행 후 삭제함 —
     필요하면 `tests/`에 재작성해 상시 검증하는 것을 권장합니다).
2. **`src/core/kiwoom_client.py`에 REST 시세 폴백 추가**
   - `get_quote(symbol)` / `get_quote_price(symbol)` 신규 메서드. 국내는 `ka10001`(주식기본정보요청,
     `/api/dostk/stkinfo`), 해외는 `ust30001`(가칭, `/api/us/mrkcond`)을 사용하도록 구현했습니다.
   - 응답 필드명이 명세서 미대조 상태라 `cur_prc`/`prpr`/`stck_prpr`/`price`/`last_price` 순으로
     후보를 시도하는 `_extract_price()`를 뒀습니다. 실제 응답을 확인한 뒤 후보 목록을 정리하세요.
3. **`src/main.py`**
   - `make_price_feed()`가 계좌별 `PriceFeed`를 생성·시작하고 `ctx.price_feed_obj`에 보관합니다.
   - `main()` 종료(`finally`) 시 모든 계좌의 `PriceFeed.stop()`(WS 연결 종료)과
     `KiwoomClient.close()`(토큰 폐기)를 호출하도록 정리 로직을 추가했습니다 (이전에는 토큰 폐기도
     호출되지 않고 있었습니다).
4. **`src/core/account_manager.py`**: `AccountContext`에 `price_feed_obj` 필드 추가 (종료 시 정리용).
5. **`.env.example`**: `PRICE_FEED_MODE`, `KIWOOM_PRICE_MAX_STALENESS_SEC`,
   `KIWOOM_WS_FIRST_TICK_WAIT_SEC`, `KIWOOM_WS_PING_INTERVAL_SEC`, 그리고 WS URL/실시간타입/FID
   오버라이드용 변수(주석 처리된 기본값) 추가.

### ⚠️ 실계좌 투입 전 반드시 검증해야 하는 것 (이번 구현의 최대 리스크)

이 저장소에는 원본 `kiwoom-rest-api-spec.json`이 포함되어 있지 않습니다. 아래 값들은 기존 코드의
명명 규칙(`kt1xxxx`/`ka1xxxx`=국내, `ust2xxxx`=해외, `/api/dostk/*`, `/api/us/*`)을 따라 **추정**한
것이며 명세서 대조 검증 전까지는 틀릴 수 있습니다:

- WebSocket URL (`wss://api.kiwoom.com:10000/api/dostk/websocket`, mock 도메인 동일 패턴)
- WebSocket 프로토콜 자체 (`LOGIN`/`REG`/`REAL`/`PING` trnm 값들)
- 실시간 타입 `"0B"`(주식체결 가정)와 시세 FID `"10"`(현재가 가정)
- REST 시세 조회 TR ID/경로: 국내 `ka10001`+`/api/dostk/stkinfo`, 해외 `ust30001`+`/api/us/mrkcond`

**검증 방법**: 모의투자 계좌로 `PRICE_FEED_MODE=rest`부터 켜서 `get_quote()` 원본 응답을 로그로
찍어 필드명을 확인하고, 그다음 `PRICE_FEED_MODE=auto`(또는 `ws`)로 전환해 WS 연결 로그
(`실시간 시세 WS 로그인 완료`)와 실제 체결가 갱신 여부를 확인하세요. 틀린 값은 `.env`의
`KIWOOM_WS_URL_REAL`/`KIWOOM_REALTIME_TYPE`/`KIWOOM_REALTIME_PRICE_FID` 또는
`kiwoom_client.py`의 `get_quote()`만 고치면 되고, `engine.py`/`main.py` 등 나머지 코드는
`price_feed(symbol) -> float` 인터페이스만 보고 있어 영향받지 않습니다.

## 이전 작업 (2026-08-11, 첫 번째 — 국내주식 매매 지원)

**요청 사항**: 한국주식(KRX)도 매매 가능하게 수정 (실제매매 + 모의투자 둘 다 지원)

`src/core/kiwoom_client.py`/`account_manager.py`는 이전 버전부터 이미 `market="KR"`,
`mode="real"/"mock"` 분기를 갖고 있었지만(국내 TR: `kt1xxxx`), 아래 항목들은 미흡하거나
실제로는 동작하지 않는 상태였어서 이번에 고쳤습니다.

1. **거래일/개장시간 판단 버그 수정** (`src/calendar_utils/market_calendar.py`)
   - 기존 코드는 국내(KR) 종목일 때 `is_market_open_now()`가 무조건 `True`를 반환했습니다.
     즉 새벽 시간에도 "정규장"으로 판단해 매매 로직이 돌 수 있는 상태였습니다.
   - `XKRX`(한국거래소) 캘린더를 `pandas_market_calendars`에서 로드하도록 수정했고,
     로드 실패 시(버전 문제 등)에는 최소한 평일 09:00~15:30(KST)만 개장으로 보는 폴백으로 동작합니다.
     이 폴백 상태에서는 설/추석 등 국내 공휴일이 자동 반영되지 않으니, 운영 전
     `pandas-market-calendars>=4.6.1`(XKRX 지원 버전)로 맞춰주세요. (`requirements.txt` 갱신함)
   - `src/core/engine.py`에서 `MarketCalendar(market="US")`로 하드코딩되어 있던 버그도 수정해
     이제 `ctx.client.market`(계좌별 실제 시장)을 그대로 사용합니다. **이전 코드에서는 계좌를
     KR로 설정해도 내부적으로는 항상 NYSE 캘린더로 거래일을 판단하고 있었습니다.**

2. **계좌 설정 예시를 실계좌/모의투자 × 해외/국내 4종으로 명확히 분리** (`config/accounts.yaml.example`)
   - `us_real` / `us_mock` / `kr_real` / `kr_mock` 4개 예시 계좌로 정리했습니다.
   - 실계좌와 모의투자는 키움 개발자센터에서 **앱키를 서로 다르게 발급**받아야 하는 점을
     주석으로 명시했습니다 (앱키 종류와 `mode` 값이 어긋나면 토큰 발급 단계에서 인증 오류 발생).
   - `.env.example`도 계좌 A/B/C/D(위 4종)에 맞춰 갱신했습니다.
   - `docker-compose.yml`의 다계좌 격리 예시 서비스명을 `autotrade-sub` → `autotrade-kr-mock`으로
     바꾸고 `ACCOUNT_FILTER=kr_mock`을 사용하도록 갱신했습니다.

3. **국내주식 예시 전략 설정 추가** (`config/strategy_config_kr.example.json`)
   - 기존 `accounts.yaml.example`이 참조하던 `strategy_config_sub.example.json`은 실제로는
     저장소에 존재하지 않는 파일이었습니다(참조만 있고 실체 없음). 국내주식용으로
     원화(KRW) 정수 금액 기준의 실제 예시 파일을 새로 만들었습니다 (심볼 `005930` 삼성전자 예시).
   - `strategy_config.example.json`, `src/dashboard/config_schema.json`에도 `market`/`mode`
     필드를 추가했습니다.

4. **통화 중립적인 필드명으로 정리**
   - `risk.max_position_usd` → `risk.max_position_amount`로 변경 (`risk_manager.py`,
     `account_manager.py`, 스키마/예시 json). 기존 `max_position_usd` 값이 남아있는 설정 파일도
     `account_manager.py`에서 폴백으로 계속 읽도록 해뒀습니다.
   - `cycle.total_invest_target_usd` → `total_invest_target_amount`로 이름만 정리
     (코드에서 참조하는 곳 없음, 문서적 통일).

5. **대시보드(`dashboard/index.html`) — 시장/환경 선택 UI 추가**
   - "매매설정 미리보기" 화면 상단에 **시장(해외주식 US / 국내주식 KRX)**, **환경(모의투자 / 실계좌)**
     선택 드롭다운과 현재 환경을 보여주는 상태 배지를 추가했습니다.
   - 통화 표시가 시장에 따라 자동 전환됩니다: US는 `$`+소수점 2자리, KR은 `₩`+정수(원 단위,
     `Math.round` 후 `ko-KR` 로케일 콤마 표기)로 모든 금액 표시(`fmtUsd()` 내부에서 분기)가 바뀝니다.
     1차 매수 금액, 차수별 매수 금액, 재매수 금액, 시뮬레이션 현재가/전일종가, 잔고 패널의
     매입/평가/손익/자산 금액 전부 여기에 해당합니다.
   - KR로 전환하면(종목코드를 아직 안 바꾼 기본 상태에 한해) 예시 종목코드를 `SOXL` → `005930`,
     예시 현재가/전일종가를 원화 스케일 값으로 자동 전환해 미리보기가 바로 그럴듯하게 보이도록
     했습니다. 실계좌 연동 후에는 `setTradeLedger()`/`setRealtimeQuote()`로 실제 값을 넣으면
     이 예시값은 자연히 덮어써집니다.
   - `stateToConfig()`/`configToState()`에 `market`/`mode` 필드를 포함시켜 프로필 저장/불러오기
     시에도 시장 선택이 함께 저장됩니다.
   - **주의**: 이 화면의 시장/환경 선택은 어디까지나 화면 표시·미리보기용입니다. 실제로 어느
     계좌(KiwoomClient)가 국내/해외, 실계좌/모의 중 어디로 주문을 내는지는 서버의
     `config/accounts.yaml`에 정의된 계좌별 `market`/`mode`/앱키가 결정합니다. 화면에서 시장을
     바꾼다고 실제 서버 쪽 계좌가 바뀌는 것은 아니므로, 백엔드 연동 시 화면의 계좌 선택 UI와
     서버의 다계좌 라우팅을 연결하는 작업이 필요합니다 (아래 "아직 미완료인 핵심 작업" 참고).

## 이전 작업 (2026-08-10, 유지됨)

1. 자동전략 설정 탭 정리 — `1) 자동매매 보호 설정` / `2) 자동매매 조건 설정` / `NXT 자동매매 설정` /
   `기타 설정` 탭 삭제.
2. 실시간 잔고 / 매매내역 화면(WIN2) — 보유 수량/평단가/현재가/평가손익/수익률/매입금액/평가금액/
   자동 여부/1~10차 상태 표시, 매수 체결 아래 해당 매도 체결 배치, 전체/진행중/종료 필터.

## 가장 중요한 현재 동작 원칙 (변경 없음)

매매내역의 손익은 설정된 목표 수익률이나 추정 매수가로 계산하지 않습니다. `tradeLedger`의 실제 체결
레코드(날짜, 수량, 체결단가)를 사용합니다.

- 매수 레코드는 `id`, `type:'buy'`, `step`, `filledAt`, `qty`, `price`를 가집니다.
- 매도 레코드는 `type:'sell'` 및 자신이 청산하는 매수 레코드의 `buyId`를 가집니다.
- `buyId` 연결을 기반으로 매수 행 아래에 매도 행을 배치하고, 손익은 `(매도가 - 매수가) × 매도수량`으로 계산합니다.
- 미청산 수량은 현재가로 평가합니다.
- 국내주식(KR)이든 해외주식(US)이든 이 계산 로직 자체는 동일합니다. 화면 표시 단위(₩ 정수 vs $ 소수점)만 다릅니다.

현재 HTML에는 화면 확인용 예시 체결 원장이 들어 있습니다. 이것은 실계좌 데이터가 아닙니다.

## 백엔드 / 키움 API 연동 시 사용할 공개 함수 (변경 없음)

```javascript
setTradeLedger(filledOrders)
setRealtimeQuote({ currentPrice, prevClose })
```

`setTradeLedger()` 입력 예시:

```javascript
[
  { id:'B-1001', type:'buy',  step:1, filledAt:'2026-08-01', qty:4, price:28.40 },
  { id:'S-1001', type:'sell', step:1, filledAt:'2026-08-05', qty:4, price:29.60, buyId:'B-1001' }
]
```

실제 연동에서는 키움 체결/잔고 API 또는 WebSocket 체결 이벤트를 정규화하여 위 형태로 전달해야 합니다.
현재가/전일종가는 WebSocket 시세를 받아 `setRealtimeQuote()`로 갱신하면 됩니다. 국내(KR) 종목은
호가/체결가가 정수(원) 단위이므로 실연동 시 `price`에 소수점 없는 정수값을 넣어도 화면 표시(₩)에는
문제 없습니다.

## 아직 미완료인 핵심 작업

- ~~키움 REST/WebSocket에서 실시간 현재가를 받아오는 백엔드 구현~~ → 반영
  (`src/core/realtime_feed.py`). 단, 위 "⚠️ 실계좌 투입 전 반드시 검증해야 하는 것" 항목은
  아직 실제 명세서 대조 검증이 안 된 상태입니다.
- ~~`MarketCalendar`가 폴백 모드로 동작할 경우 국내 공휴일이 반영되지 않는 문제~~ →
  `pandas-market-calendars>=4.6.1`로 XKRX 캘린더가 정상 로드되는 것을 확인했고, 폴백 경로
  자체의 버그(US 폴백이 항상 "개장"으로 판단되던 문제)도 수정했습니다. 다만 **캘린더 로딩이
  실패하는 배포 환경에서는 여전히 공휴일이 반영되지 않는 것 자체는 근본적으로 남아있는 한계**입니다
  (이제는 최소한 정규장 시간 밖에는 안전하게 휴장으로 판단합니다). 배포 시 로그에
  "캘린더를 불러오지 못했습니다" 경고가 찍히지 않는지 한 번은 확인하세요.
- 키움 REST/WebSocket에서 실시간 **체결·잔고**를 받아오는 백엔드 구현 (현재가 스트림과는 별개 —
  주문 체결 통보(`00`)·잔고 통보(`04`) 실시간 구독 및 `setTradeLedger()`용 정규화는 아직 미구현)
- 체결 원장 DB 저장 및 서버 재시작 후 복구
- `dashboard/index.html`과 백엔드 API 연결 (현재 HTML은 예시 데이터 및 localStorage 중심).
  특히 대시보드의 시장(US/KR)·환경(real/mock) 선택 UI를 **서버의 다계좌(`accounts.yaml`) 중 어떤
  계좌를 보고 있는지**와 연결하는 작업이 필요합니다 (현재는 화면 표시 전용, 계좌 전환 API 없음).
- 계좌/여러 종목 선택 UI와 실제 계좌별 데이터 표시 (지금은 화면에 계좌 하나만 표시)
- `MarketCalendar`가 폴백 모드로 동작할 경우(XKRX 캘린더 로드 실패) 국내 공휴일이 반영되지 않는
  문제 — `requirements.txt`를 `pandas-market-calendars>=4.6.1`로 맞추면 해결됩니다. 배포 환경에서
  실제로 XKRX 캘린더가 로드되는지 한 번은 확인해보시길 권장합니다.

## 인계 파일

- `dashboard_index_KRX매매지원.html`: 단독 실행본 (국내주식 매매 UI 반영)
- `kiwoom-autotrade_KRX매매지원.zip`: 전체 프로젝트. 이 문서가 프로젝트 루트에 포함되어 있음.
