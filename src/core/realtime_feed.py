"""실시간 시세 소스: WebSocket 실시간체결 구독 + REST 폴백.

`main.py`의 `make_price_feed()`가 이 모듈의 `PriceFeed`를 사용해
`AccountEngine`에 넘길 `price_feed(symbol) -> float` 콜러블을 만듭니다.

⚠️ 검증 필요: 이 저장소에는 원본 `kiwoom-rest-api-spec.json`이 포함되어 있지 않아,
WebSocket URL/프로토콜(LOGIN·REG·REAL·PING)과 실시간 타입("0B")/FID("10")는 키움이
공개한 REST/WebSocket API의 일반적인 규격(계좌 체결/시세 스트림에서 널리 쓰이는 방식)을
기준으로 구현했습니다. 실계좌 투입 전 아래를 반드시 확인하세요:
  1. 모의투자 계좌로 먼저 연결해 LOGIN 응답과 REAL 데이터 payload 를 로그로 확인
  2. FID(PRICE_FID)가 실제 현재가 필드와 일치하는지 확인
  3. 필요 시 아래 환경변수로 코드 수정 없이 값 교체 가능:
     - KIWOOM_WS_URL_REAL / KIWOOM_WS_URL_MOCK
     - KIWOOM_REALTIME_TYPE (기본 "0B": 주식체결)
     - KIWOOM_REALTIME_PRICE_FID (기본 "10": 현재가)
     - KIWOOM_WS_PING_INTERVAL_SEC (기본 30)

동작 모드(PRICE_FEED_MODE, main.py 에서 읽음):
  - auto (권장 기본값): WebSocket 캐시를 우선 사용하고, 캐시가 없거나
    KIWOOM_PRICE_MAX_STALENESS_SEC 보다 오래되면 즉시 REST로 폴백합니다.
  - ws: WebSocket 전용. REST 폴백 없이 최초 체결 수신까지 짧게 대기 후 실패 시 예외.
  - rest: WebSocket을 아예 켜지 않고 REST 폴링만 사용합니다 (가장 단순/안전하지만 느림).
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import websockets

from src.utils.exceptions import FatalError

DEFAULT_WS_REAL = "wss://api.kiwoom.com:10000/api/dostk/websocket"
DEFAULT_WS_MOCK = "wss://mockapi.kiwoom.com:10000/api/dostk/websocket"

REALTIME_TYPE = os.environ.get("KIWOOM_REALTIME_TYPE", "0B")  # 0B: 주식체결(개별 종목 체결가)
PRICE_FID = os.environ.get("KIWOOM_REALTIME_PRICE_FID", "10")  # FID 10: 현재가

PING_INTERVAL_SEC = float(os.environ.get("KIWOOM_WS_PING_INTERVAL_SEC", "30"))
RECONNECT_MIN_SEC = 1.0
RECONNECT_MAX_SEC = 30.0
HEALTHY_SESSION_SEC = float(os.environ.get("KIWOOM_WS_HEALTHY_SESSION_SEC", "15"))


def _connect_ws_socket(host: str, port: int, local_port: int, timeout: float) -> socket.socket:
    last_error: OSError | None = None
    for family, socktype, proto, _, remote_address in socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    ):
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(timeout)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            bind_address = "::" if family == socket.AF_INET6 else "0.0.0.0"
            sock.bind((bind_address, local_port))
            sock.connect(remote_address)
            sock.setblocking(False)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    if last_error is not None:
        raise last_error
    raise OSError(f"Could not resolve {host}:{port}")


@dataclass
class _Tick:
    price: float
    ts: float


class KiwoomRealtimeFeed:
    """계좌(=KiwoomClient) 1개당 1개의 실시간 시세 WebSocket 연결을 유지합니다.

    - 계좌마다 앱키/토큰이 다르므로 연결도 계좌별로 독립적입니다.
    - 구독 종목은 동적으로 추가할 수 있고(subscribe), 재연결 시 자동으로 재등록됩니다.
    - 수신한 체결가는 메모리 캐시에 (가격, 수신시각)으로 저장되며 get_cached()로 조회합니다.
    """

    def __init__(self, client, logger=None, max_staleness_sec: float = 20.0):
        self.client = client
        self.logger = logger
        self.max_staleness_sec = max_staleness_sec
        self._ws = None
        self._cache: dict[str, _Tick] = {}
        self._subscribed: set[str] = set()
        self._pending_subscribe: set[str] = set()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.connected = asyncio.Event()
        self._doorbell_callbacks: list = []
        default_events = "F5" if client.market == "US" else "00,04"
        self._doorbell_types = {x.strip() for x in os.environ.get("KIWOOM_WS_DOORBELL_TYPES", default_events).split(",") if x.strip()}

    def add_doorbell_callback(self, callback) -> None:
        """Register a no-payload account event callback.

        Account order/balance messages are deliberately *not* decoded here.
        Their schema is not an accounting authority; they merely wake a REST
        synchronization pass.
        """
        self._doorbell_callbacks.append(callback)

    def remove_doorbell_callback(self, callback) -> None:
        self._doorbell_callbacks = [item for item in self._doorbell_callbacks if item != callback]

    @property
    def ws_url(self) -> str:
        if self.client.mode == "real":
            return os.environ.get("KIWOOM_WS_URL_REAL", DEFAULT_WS_REAL)
        return os.environ.get("KIWOOM_WS_URL_MOCK", DEFAULT_WS_MOCK)

    @property
    def ws_local_port(self) -> int | None:
        if self.client.mode != "mock":
            return None
        if self.client.market == "KR":
            return 10000
        if self.client.market == "US":
            return 443
        return None

    async def _prebound_ws_socket(self) -> socket.socket | None:
        local_port = self.ws_local_port
        if local_port is None:
            return None
        parsed = urlsplit(self.ws_url)
        if not parsed.hostname:
            raise ValueError(f"WebSocket URL has no hostname: {self.ws_url!r}")
        remote_port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        return await asyncio.to_thread(
            _connect_ws_socket,
            parsed.hostname,
            remote_port,
            local_port,
            10,
        )

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def subscribe(self, symbol: str) -> None:
        if symbol in self._subscribed or symbol in self._pending_subscribe:
            return
        self._pending_subscribe.add(symbol)

    def unsubscribe(self, symbol: str) -> None:
        """Do not retain a stopped symbol across a WebSocket reconnect."""
        self._subscribed.discard(symbol)
        self._pending_subscribe.discard(symbol)
        self._cache.pop(symbol, None)

    def get_cached(self, symbol: str, max_age_sec: float) -> float | None:
        tick = self._cache.get(symbol)
        if tick is None:
            return None
        if time.time() - tick.ts > max_age_sec:
            return None
        return tick.price

    def get_cached_tick(self, symbol: str, max_age_sec: float) -> _Tick | None:
        tick = self._cache.get(symbol)
        if tick is None or time.time() - tick.ts > max_age_sec:
            return None
        return tick

    def subscribed_symbols(self) -> tuple[str, ...]:
        """Return symbols currently active or waiting for subscription."""
        return tuple(sorted(self._subscribed | self._pending_subscribe))

    def cache_age_sec(self, symbol: str) -> float | None:
        """Return seconds since the last cached tick, or None if absent."""
        tick = self._cache.get(symbol)
        if tick is None:
            return None
        return max(0.0, time.time() - tick.ts)

    # ------------------------------------------------------------------
    # 연결 유지 루프 (지수 백오프 재연결)
    # ------------------------------------------------------------------
    async def _run_forever(self) -> None:
        backoff = RECONNECT_MIN_SEC
        while not self._stop.is_set():
            session_started_at = time.monotonic()
            try:
                await self._connect_once()
                if time.monotonic() - session_started_at < HEALTHY_SESSION_SEC:
                    raise RuntimeError(
                        f"WebSocket closed before {HEALTHY_SESSION_SEC:g}s healthy threshold"
                    )
                backoff = RECONNECT_MIN_SEC  # 정상적으로 한 번 붙었다 끊긴 경우 백오프 초기화
            except asyncio.CancelledError:
                raise
            except FatalError as e:
                # 인증 실패 등은 재시도해도 계속 실패할 가능성이 높으므로 로그만 남기고 계속 시도는 함
                # (앱키/토큰이 나중에 교체될 수 있는 배포 환경 고려). 필요 시 여기서 즉시 중단하도록 바꿀 수 있음.
                if self.logger:
                    self.logger.error(f"실시간 시세 WS 인증 오류: {e}")
                # Credential failures cannot self-heal; do not hammer au10001.
                break
            except Exception as e:
                if self.logger:
                    self.logger.warning(f"실시간 시세 WS 연결 끊김/오류, {backoff:.0f}초 후 재연결: {e}")
            finally:
                self.connected.clear()
                self._ws = None

            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_SEC)

    async def _connect_once(self) -> None:
        token = await self.client.token_mgr.get_token()
        raw_token = token.split(" ", 1)[1] if " " in token else token  # "Bearer xxx" -> "xxx"

        sock = await self._prebound_ws_socket()
        try:
            connect_kwargs = {"ping_interval": None, "max_size": 2**22}
            if sock is not None:
                connect_kwargs["sock"] = sock
            async with websockets.connect(self.ws_url, **connect_kwargs) as ws:
                self._ws = ws
                await ws.send(json.dumps({"trnm": "LOGIN", "token": raw_token}))
                resp = json.loads(await ws.recv())
                return_code = resp.get("return_code", 0)
                if resp.get("trnm") != "LOGIN" or str(return_code) not in ("0", "None"):
                    raise FatalError(f"실시간 시세 WS 로그인 실패: {resp}")
                if self.logger:
                    self.logger.info(f"실시간 시세 WS 로그인 완료 ({self.client.mode}, {self.ws_url})")

                self.connected.set()
                # 재연결 상황이면 기존에 구독 중이던 종목을 다시 등록 대상에 넣음
                self._pending_subscribe |= self._subscribed
                self._subscribed.clear()

                tasks = [
                    asyncio.create_task(self._register_loop(ws), name="ws_register"),
                    asyncio.create_task(self._receive_loop(ws), name="ws_receive"),
                    asyncio.create_task(self._ping_loop(ws), name="ws_ping"),
                ]
                try:
                    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for t in pending:
                        t.cancel()
                    for t in pending:
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):
                            pass
                    for t in done:
                        exc = t.exception()
                        if exc:
                            raise exc
                finally:
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    if self.logger and not self._stop.is_set():
                        self.logger.warning(
                            f"WebSocket session ended: close_code={getattr(ws, 'close_code', None)!r} "
                            f"close_reason={getattr(ws, 'close_reason', '')!r}"
                        )
        finally:
            if sock is not None:
                sock.close()

    async def _register_loop(self, ws) -> None:
        """새로 구독 요청된 종목이 있으면 REG(실시간 등록) 메시지를 전송."""
        while not self._stop.is_set():
            if self._pending_subscribe:
                symbols = list(self._pending_subscribe)
                self._pending_subscribe.clear()
                await ws.send(json.dumps({
                    "trnm": "REG",
                    "grp_no": "1",
                    "refresh": "1",
                    # 00/04 (or F5 for US) are subscribed only as a doorbell.
                    # Their payload is discarded; REST calls below remain authoritative.
                    "data": [{"item": symbols, "type": [REALTIME_TYPE, *sorted(self._doorbell_types)]}],
                }))
                self._subscribed.update(symbols)
                if self.logger:
                    self.logger.info(
                        f"WebSocket REG sent: symbols={symbols}, "
                        f"types={[REALTIME_TYPE, *sorted(self._doorbell_types)]}"
                    )
                if self.logger:
                    self.logger.info(f"실시간 시세 구독 등록: {symbols} (type={REALTIME_TYPE})")
            await asyncio.sleep(0.5)

    async def _ping_loop(self, ws) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(PING_INTERVAL_SEC)
            await ws.send(json.dumps({"trnm": "PING"}))

    async def _receive_loop(self, ws) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            trnm = msg.get("trnm")
            if trnm == "REG":
                if self.logger:
                    self.logger.info(
                        f"WebSocket REG response: return_code={msg.get('return_code')!r} "
                        f"return_msg={msg.get('return_msg', '')!r}"
                    )
                continue
            if trnm == "PING":
                # 서버가 keepalive PING을 보내는 규격이면 그대로 되돌려줌
                await ws.send(raw)
                continue
            if trnm != "REAL":
                continue

            for item in msg.get("data", []):
                if item.get("type") in self._doorbell_types:
                    for callback in self._doorbell_callbacks:
                        try:
                            result = callback()
                            if asyncio.iscoroutine(result):
                                asyncio.create_task(result)
                        except Exception as e:
                            if self.logger:
                                self.logger.warning(f"account WS doorbell callback failed: {e}")
                    # Never parse account-event fields: REST remains authoritative.
                    continue
                symbol = item.get("item")
                values = item.get("values", {}) or {}
                price_raw = values.get(PRICE_FID)
                if not symbol or price_raw in (None, ""):
                    continue
                try:
                    price = abs(float(str(price_raw).replace(",", "").strip()))
                except ValueError:
                    continue
                received_at = time.time()
                previous = self._cache.get(symbol)
                self._cache[symbol] = _Tick(price=price, ts=received_at)
                if (
                    self.logger
                    and previous is not None
                    and received_at - previous.ts > self.max_staleness_sec
                ):
                    self.logger.warning(
                        f"WebSocket quote gap recovered: symbol={symbol} "
                        f"gap_sec={received_at - previous.ts:.1f} "
                        f"threshold_sec={self.max_staleness_sec:g}"
                    )


class PriceFeed:
    """WebSocket 실시간 시세(우선) + REST 폴백을 결합한 시세 소스.

    `AccountEngine.price_feed` 로 넘기는 `async def get_price(symbol) -> float` 콜러블 역할.
    """

    def __init__(self, client, logger=None, mode: str = "auto", max_staleness_sec: float = 20.0):
        self.client = client
        self.logger = logger
        self.mode = mode  # "auto" | "ws" | "rest"
        self.max_staleness_sec = max_staleness_sec
        self.realtime = (
            KiwoomRealtimeFeed(client, logger, max_staleness_sec)
            if mode in ("auto", "ws") else None
        )
        self._first_tick_wait_sec = float(os.environ.get("KIWOOM_WS_FIRST_TICK_WAIT_SEC", "5"))
        # A dashboard user can switch holdings quickly.  Serialize REST quote
        # fallback across symbols so Kiwoom's ka10001 quota is never burst.
        self._rest_quote_lock = asyncio.Lock()
        self._last_rest_quote_at = 0.0
        self._rest_quote_min_interval_sec = float(os.environ.get("KIWOOM_REST_QUOTE_MIN_INTERVAL_SEC", "1.2"))

    def start(self) -> None:
        if self.realtime is not None:
            self.realtime.start()

    async def stop(self) -> None:
        if self.realtime is not None:
            await self.realtime.stop()

    async def get_quote(self, symbol: str) -> tuple[float, str, float]:
        """Return a usable quote together with its source and observation time."""
        if self.realtime is not None:
            self.realtime.subscribe(symbol)
            tick = self.realtime.get_cached_tick(symbol, self.max_staleness_sec)
            if tick is not None:
                return tick.price, "ws", tick.ts

            if self.mode == "ws":
                # 순수 WS 모드 (REST 폴백 없음): 최초 체결 수신까지 짧게 대기
                deadline = time.time() + self._first_tick_wait_sec
                while time.time() < deadline:
                    await asyncio.sleep(0.2)
                    tick = self.realtime.get_cached_tick(symbol, self.max_staleness_sec)
                    if tick is not None:
                        return tick.price, "ws", tick.ts
                raise RuntimeError(
                    f"{symbol} 실시간 시세를 아직 수신하지 못했습니다 (WS 전용 모드, "
                    f"{self._first_tick_wait_sec:.0f}초 대기 후 타임아웃)"
                )
            # auto 모드: WS 캐시가 비었거나 오래됐으면 즉시 REST로 폴백 (다음 틱에서 WS가 채워지면 다시 사용됨)

        async with self._rest_quote_lock:
            wait_for = self._rest_quote_min_interval_sec - (time.monotonic() - self._last_rest_quote_at)
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            try:
                price = await self.client.get_quote_price(symbol)
                return price, "rest", time.time()
            finally:
                self._last_rest_quote_at = time.monotonic()

    async def get_price(self, symbol: str) -> float:
        price, _source, _timestamp = await self.get_quote(symbol)
        return price
