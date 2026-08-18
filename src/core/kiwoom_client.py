"""키움증권 REST API 클라이언트.

첨부된 kiwoom-rest-api-spec.json 의 실제 TR ID를 사용합니다.
- 실계좌: https://api.kiwoom.com
- 모의계좌: https://mockapi.kiwoom.com
market="US" 인 경우 해외주식(ust2xxxx) TR을, market="KR" 인 경우 국내주식(kt1xxxx) TR을 사용합니다.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from src.core.broker_http import BrokerHTTPGate
from src.core.token_manager import TokenManager
from src.core.process_lock import AccountOrderAuthority
from src.core.us_market import normalize_us_symbol, validate_us_order
from src.utils.exceptions import (
    ExchangeResolutionError,
    KiwoomAPIError,
    OrderAuthorityError,
    OrderRejectedError,
    QuoteCircuitOpenError,
    RetryableError,
)

REAL_DOMAIN = "https://api.kiwoom.com"
MOCK_DOMAIN = "https://mockapi.kiwoom.com"

Market = Literal["US", "KR"]
Side = Literal["BUY", "SELL"]


@dataclass
class OrderResult:
    ord_no: str
    raw: dict


@dataclass
class _QuoteGate:
    """Process-wide Kiwoom quote quota guard, keyed by account and market.

    The API specification identifies ``usa20100`` as the US quote endpoint and
    documents error 1700 as an API-request-limit breach, but does not publish a
    numeric quota.  We therefore use conservative spacing and let a broker 429
    open a bounded circuit instead of probing repeatedly.
    """
    lock: asyncio.Lock
    last_request_at: float = 0.0
    not_before: float = 0.0
    backoff_sec: float = 30.0
    consecutive_symbol_errors: int = 0


_QUOTE_GATES: dict[str, _QuoteGate] = {}

@dataclass
class _OrderGate:
    """Account-wide order throttle shared across all symbols and tranches."""

    lock: asyncio.Lock
    last_request_at: float = 0.0


_ORDER_GATES: dict[str, _OrderGate] = {}


def _quote_gate(domain: str) -> _QuoteGate:
    gate = _QUOTE_GATES.get(domain)
    if gate is None:
        gate = _QuoteGate(lock=asyncio.Lock())
        _QUOTE_GATES[domain] = gate
    return gate


def _order_gate(domain: str) -> _OrderGate:
    gate = _ORDER_GATES.get(domain)
    if gate is None:
        gate = _OrderGate(lock=asyncio.Lock())
        _ORDER_GATES[domain] = gate
    return gate


class KiwoomClient:
    """실계좌/모의계좌를 모두 지원하는 키움 REST API 래퍼."""

    def __init__(
        self,
        appkey: str,
        secretkey: str,
        account_no: str,
        market: Market = "US",
        exchange: str = "ND",  # US: ND(나스닥)/NY(뉴욕)/NA(아멕스), KR: KRX/NXT/SOR (실제 스펙값 확인 후 사용)
        mode: Literal["real", "mock"] = "mock",
        logger=None,
        order_authority: AccountOrderAuthority | None = None,
    ):
        self.domain = REAL_DOMAIN if mode == "real" else MOCK_DOMAIN
        self.account_no = account_no
        self.market = market
        self.exchange = exchange
        self.mode = mode
        self.logger = logger
        self._order_authority = order_authority
        http_port = 10000 if mode == "mock" and market == "KR" else 443 if mode == "mock" and market == "US" else None
        self._http_gate = BrokerHTTPGate(http_port)
        self.token_mgr = TokenManager(self.domain, appkey, secretkey, logger, self._http_gate)
        self._quote_min_interval_sec = max(0.5, float(os.environ.get("KIWOOM_REST_QUOTE_MIN_INTERVAL_SEC", "2.0")))
        self._order_min_interval_sec = max(
            0.0,
            float(os.environ.get("KIWOOM_REST_ORDER_MIN_INTERVAL_SEC", self._default_order_min_interval_sec())),
        )
        self._quote_error_threshold = max(1, int(os.environ.get("KIWOOM_QUOTE_SYMBOL_ERROR_THRESHOLD", "3")))
        self._quote_circuit_sec = max(5.0, float(os.environ.get("KIWOOM_QUOTE_CIRCUIT_SEC", "60")))
        self._quote_inflight: dict[str, asyncio.Task[dict]] = {}
        self._order_gate = _order_gate(f"{self.domain}|{self.account_no}|{self.market}")
        self._exchange_cache = {
            "KORU": "NY", "SOXL": "NY", "BIVI": "ND", "SMCI": "ND",
            "DFSC": "ND", "IREN": "ND", "SPCX": "ND",
        } if market == "US" else {}
        self._exchange_alert_callback = None
        self._exchange_alerted_symbols: set[str] = set()

    def _default_order_min_interval_sec(self) -> float:
        if self.mode == "mock":
            return 1.0
        if self.market == "US":
            return 1.0 / 3.0
        return 0.2

    def set_exchange_alert_callback(self, callback) -> None:
        """Attach the worker's notification path without coupling the client to Telegram."""
        self._exchange_alert_callback = callback

    def bind_order_authority(self, authority: AccountOrderAuthority) -> None:
        self._order_authority = authority

    async def _notify_exchange_failure(self, symbol: str, error: Exception) -> None:
        if symbol in self._exchange_alerted_symbols:
            return
        self._exchange_alerted_symbols.add(symbol)
        callback = self._exchange_alert_callback
        if callback is not None:
            try:
                result = callback(f"US exchange lookup failed; order/request blocked: {symbol}; {error}")
                if hasattr(result, "__await__"):
                    await result
            except Exception as alert_error:
                if self.logger:
                    self.logger.warning(f"US exchange lookup alert failed for {symbol}: {alert_error}")

    async def _resolve_exchange(self, symbol: str) -> str:
        if self.market != "US":
            return self.exchange
        normalized = normalize_us_symbol(symbol)
        cached = self._exchange_cache.get(normalized)
        if cached:
            return cached
        try:
            data = await self._post("/api/us/stkinfo", "usa10098", {"stk_cd": normalized})
            rows = data.get("list") if isinstance(data, dict) else None
            row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
            exchange = str(row.get("stex_tp", "")).strip().upper() if row else ""
            if exchange not in {"NA", "ND", "NY"}:
                raise ValueError(f"usa10098 returned no valid stex_tp for {normalized}")
        except Exception as exc:
            await self._notify_exchange_failure(normalized, exc)
            raise ExchangeResolutionError(
                f"US exchange lookup failed for {normalized}; order/request blocked: {exc}"
            ) from exc
        self._exchange_cache[normalized] = exchange
        return exchange

    async def _headers(self, api_id: str, cont_yn: str = "N", next_key: str = "") -> dict:
        token = await self.token_mgr.get_token()
        return {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": token,
            "api-id": api_id,
            "cont-yn": cont_yn,
            "next-key": next_key,
        }

    async def _post_once(
        self,
        path: str,
        api_id: str,
        body: dict,
        _reauth_attempt: bool = False,
        allow_reauth_retry: bool = True,
    ) -> dict:
        url = f"{self.domain}{path}"
        headers = await self._headers(api_id)
        async with self._http_gate.client(timeout=15) as client:
            try:
                resp = await client.post(url, json=body, headers=headers, timeout=15)
            except httpx.RequestError as e:
                raise RetryableError(f"{api_id} 네트워크 오류: {e}") from e

        if resp.status_code >= 500:
            raise RetryableError(f"{api_id} 서버 오류: {resp.status_code}")

        data = resp.json() if resp.content else {}
        if resp.status_code != 200:
            raise KiwoomAPIError(api_id, resp.status_code, data.get("return_msg", resp.text), data)

        return_code = data.get("return_code")
        if return_code not in (None, 0, "0"):
            # Kiwoom can revoke a token before its advertised expiry (8005),
            # especially after another process obtains/revokes credentials.
            # Invalidate the cached token and retry this exact request once;
            # never let a generic retry submit an order twice.
            message = str(data.get("return_msg", ""))
            if (allow_reauth_retry and not _reauth_attempt and str(return_code) == "3" and "8005" in message):
                self.logger.warning(f"{api_id}: server rejected cached token; issuing a fresh token and retrying once")
                self.token_mgr.invalidate()
                return await self._post_once(
                    path,
                    api_id,
                    body,
                    _reauth_attempt=True,
                    allow_reauth_retry=allow_reauth_retry,
                )
            raise KiwoomAPIError(api_id, return_code, data.get("return_msg", ""), data)

        return data

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type(RetryableError),
    )
    async def _post(self, path: str, api_id: str, body: dict, _reauth_attempt: bool = False) -> dict:
        return await self._post_once(path, api_id, body, _reauth_attempt=_reauth_attempt)

    # ------------------------------------------------------------------
    # 주문 (매수/매도/정정/취소)
    # ------------------------------------------------------------------
    async def place_order(
        self,
        side: Side,
        symbol: str,
        qty: int,
        price: float | None = None,
        order_type: str = "00",  # 00:지정가, 03:시장가 등 (spec 참고)
    ) -> OrderResult:
        """매수/매도 주문. market 에 따라 kt10000/kt10001 또는 ust20000/ust20001 사용."""
        if self._order_authority is None:
            raise OrderAuthorityError(
                f"Order authority is not configured for account {self.account_no}"
            )
        self._order_authority.assert_owned()
        if self.market == "US":
            exchange = await self._resolve_exchange(symbol)
            try:
                symbol = validate_us_order(symbol, exchange, qty, price, order_type)
            except ValueError as exc:
                raise OrderRejectedError(str(exc)) from exc
            api_id = "ust20000" if side == "BUY" else "ust20001"
            path = "/api/us/ordr"
            body = {
                "stex_tp": exchange,
                "stk_cd": symbol,
                "ord_qty": str(qty),
                "trde_tp": order_type,
            }
            if order_type in ("00", "30", "26", "27"):  # 지정가류는 단가 필수
                # Kiwoom US price precision is tiered: under $1 allows four
                # decimals, while $1 and above allows only two decimals.
                precision = 4 if price is not None and price < 1 else 2
                body["ord_uv"] = f"{price:.{precision}f}".rstrip("0").rstrip(".")
            if side == "SELL":
                body["stop_pric"] = ""
        else:  # KR
            api_id = "kt10000" if side == "BUY" else "kt10001"
            path = "/api/dostk/ordr"
            body = {
                "dmst_stex_tp": self.exchange or "KRX",
                "stk_cd": symbol,
                "ord_qty": str(qty),
                "ord_uv": str(int(price)) if price else "",
                "trde_tp": order_type,
                "cond_uv": "",
            }

        try:
            async with self._order_gate.lock:
                now = time.monotonic()
                wait_for = self._order_min_interval_sec - (now - self._order_gate.last_request_at)
                if wait_for > 0:
                    await asyncio.sleep(wait_for)
                self._order_authority.assert_owned()
                self._order_gate.last_request_at = time.monotonic()
                data = await self._post_once(path, api_id, body, allow_reauth_retry=False)
        except KiwoomAPIError as e:
            raise OrderRejectedError(str(e)) from e

        ord_no = data.get("ord_no", "")
        if self.logger:
            logger = self.logger.bind(symbol=symbol) if hasattr(self.logger, "bind") else self.logger
            logger.info(f"주문 완료: {side} {symbol} x{qty} @ {price} -> ord_no={ord_no}")
        return OrderResult(ord_no=ord_no, raw=data)

    async def cancel_order(self, symbol: str, orig_ord_no: str, qty: int) -> dict:
        if self.market == "US":
            api_id, path = "ust20003", "/api/us/ordr"
            exchange = await self._resolve_exchange(symbol)
            body = {"stex_tp": exchange, "stk_cd": symbol, "orig_ord_no": orig_ord_no, "ord_qty": str(qty)}
        else:
            api_id, path = "kt10003", "/api/dostk/ordr"
            body = {"dmst_stex_tp": self.exchange or "KRX", "stk_cd": symbol,
                    # kt10003 rejects ord_qty here.  Its required cancellation
                    # field is cncl_qty, even though regular orders use ord_qty.
                    "orig_ord_no": orig_ord_no, "cncl_qty": str(qty)}
        return await self._post(path, api_id, body)

    # ------------------------------------------------------------------
    # 잔고 / 예수금
    # ------------------------------------------------------------------
    async def get_balance(self) -> dict:
        """계좌평가잔고내역 (국내 kt00018) 또는 해외 예수금/평가 (ust21120)."""
        if self.market == "US":
            # ust21120 is only the currency/deposit summary. ust21070 is the
            # authoritative US ledger balance containing per-stock positions.
            data = await self._post("/api/us/acnt", "ust21070", {"stex_tp": "", "stk_cd": ""})
        else:
            data = await self._post("/api/dostk/acnt", "kt00018", {"qry_tp": "1", "dmst_stex_tp": "KRX"})
        return data

    async def get_cash(self) -> dict:
        if self.market == "US":
            return await self._post("/api/us/acnt", "ust21160", {})
        return await self._post("/api/dostk/acnt", "kt00001", {"qry_tp": "3"})

    async def get_fx_rate(self) -> dict:
        """Reference USD/KRW rate for dashboard reporting (not a conversion order)."""
        if self.market != "US":
            return {}
        # ust31301 requires the conversion direction even for a quote-only
        # request: 1 is KRW -> USD, which is the direction needed to derive
        # the USD/KRW reference rate used by the dashboard.
        return await self._post("/api/us/exchange", "ust31301", {"exch_tp": "1"})

    # ------------------------------------------------------------------
    # 체결 / 미체결
    # ------------------------------------------------------------------
    async def get_unfilled_orders(self, symbol: str = "") -> dict:
        """미체결요청 (국내: ka10075). 미국은 ust21510(당일 주문체결 확인)으로 필터링해서 사용."""
        if self.market == "US":
            return await self._post("/api/us/acnt", "ust21510", {"stk_cd": symbol})
        body = {"all_stk_tp": "1" if symbol else "0", "trde_tp": "0", "stk_cd": symbol, "stex_tp": "0"}
        return await self._post("/api/dostk/acnt", "ka10075", body)

    async def get_executed_orders(self, symbol: str = "") -> dict:
        """체결요청 (국내: ka10076). 미국은 ust21150(일별 주문체결내역)."""
        if self.market == "US":
            # ust21150 requires both query_tp and slby_tp even when filtering
            # by symbol. ``0`` requests all execution history and both sides.
            return await self._post(
                "/api/us/acnt", "ust21150",
                {"stk_cd": symbol, "query_tp": "0", "slby_tp": "0", "stex_tp": await self._resolve_exchange(symbol)},
            )
        body = {"stk_cd": symbol, "qry_tp": "1" if symbol else "0", "sell_tp": "0", "ord_no": "", "stex_tp": "0"}
        return await self._post("/api/dostk/acnt", "ka10076", body)

    # ------------------------------------------------------------------
    # 시세 (현재가) — REST 폴백용
    # ------------------------------------------------------------------
    async def get_quote(self, symbol: str) -> dict:
        """현재가/호가 REST 조회. 실시간 WebSocket이 끊겼거나 아직 첫 체결이 없을 때의 폴백입니다.

        ⚠️ 이 저장소에는 `kiwoom-rest-api-spec.json`(원본 명세서)이 포함되어 있지 않아
        아래 TR ID/경로는 코드베이스의 기존 명명 규칙(`kt1xxxx`/`ka1xxxx`=국내, `ust2xxxx`=해외,
        `/api/dostk/*`=국내 도메인, `/api/us/*`=해외 도메인)을 따라 추정한 값입니다.
        실계좌 투입 전 명세서와 대조해 확인하고, 다르면 이 메서드만 고치면 됩니다
        (`get_price()`를 호출하는 다른 코드는 영향 없음).
        - 국내(KR): ka10001(주식기본정보요청), `/api/dostk/stkinfo`
        - 해외(US): usa20100(미국주식 현재가 종목정보), `/api/us/mrkcond`
        """
        normalized = normalize_us_symbol(symbol) if self.market == "US" else str(symbol)
        # Coalesce concurrent dashboard/worker lookups for the exact same
        # symbol.  One broker request serves all waiters.
        existing = self._quote_inflight.get(normalized)
        if existing is not None:
            return await asyncio.shield(existing)
        task = asyncio.create_task(self._get_quote_limited(normalized))
        self._quote_inflight[normalized] = task
        try:
            return await asyncio.shield(task)
        finally:
            if self._quote_inflight.get(normalized) is task:
                self._quote_inflight.pop(normalized, None)

    async def _get_quote_limited(self, symbol: str) -> dict:
        # KR and US accounts must never consume one another's quote cooldown.
        gate = _quote_gate(f"{self.domain}|{self.account_no}|{self.market}")
        async with gate.lock:
            now = time.monotonic()
            if now < gate.not_before:
                remaining = gate.not_before - now
                raise QuoteCircuitOpenError(
                    f"quote circuit open for {remaining:.0f}s; no broker request was sent"
                )
            wait_for = self._quote_min_interval_sec - (now - gate.last_request_at)
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            gate.last_request_at = time.monotonic()
            try:
                if self.market == "US":
                    data = await self._post(
                        "/api/us/mrkcond", "usa20100",
                        {"stex_tp": await self._resolve_exchange(symbol), "stk_cd": symbol},
                    )
                else:
                    data = await self._post("/api/dostk/stkinfo", "ka10001", {"stk_cd": symbol})
            except KiwoomAPIError as exc:
                self._record_quote_failure(gate, exc)
                raise
            except RetryableError:
                # Network/5xx failures should pause briefly too; retries are
                # already bounded inside _post and must not turn into tick spam.
                gate.not_before = max(gate.not_before, time.monotonic() + min(gate.backoff_sec, self._quote_circuit_sec))
                raise
            else:
                gate.consecutive_symbol_errors = 0
                gate.backoff_sec = 30.0
                return data

    def _record_quote_failure(self, gate: _QuoteGate, exc: KiwoomAPIError) -> None:
        message = str(exc)
        now = time.monotonic()
        # Kiwoom spec error 1700 is the API request-limit response.  A 429 or
        # 1700 opens an account/domain-wide circuit immediately and backs off
        # exponentially, capped to avoid an unattended permanent lockout.
        if str(exc.return_code) == "429" or "1700" in message:
            cooldown = min(gate.backoff_sec, 300.0)
            gate.not_before = max(gate.not_before, now + cooldown)
            gate.backoff_sec = min(cooldown * 2, 300.0)
            gate.consecutive_symbol_errors = 0
            if self.logger:
                self.logger.warning(f"Quote quota circuit opened for {cooldown:g}s after {exc.api_id}: {exc}")
            return
        # A repeated 1903/venue-resolution failure is not retried on every
        # strategy tick.  It opens the same fail-closed circuit after a small,
        # configurable threshold; the code never guesses another exchange.
        if "1903" in message:
            gate.consecutive_symbol_errors += 1
            if gate.consecutive_symbol_errors >= self._quote_error_threshold:
                gate.not_before = max(gate.not_before, now + self._quote_circuit_sec)
                gate.consecutive_symbol_errors = 0
                if self.logger:
                    self.logger.warning(
                        f"Quote symbol-resolution circuit opened for {self._quote_circuit_sec:g}s after repeated 1903 responses"
                    )

    async def get_quote_price(self, symbol: str) -> float:
        """get_quote() 응답에서 현재가 필드를 추출합니다."""
        data = await self.get_quote(symbol)
        price = _extract_price(data)
        if price is None:
            raise KiwoomAPIError(
                "quote",
                data.get("return_code"),
                f"현재가 필드를 찾지 못했습니다(응답 필드명을 확인해 _PRICE_FIELD_CANDIDATES를 "
                f"맞춰주세요): keys={list(data.keys())}",
                data,
            )
        return price

    async def close(self):
        await self.token_mgr.revoke()


# 응답 필드명이 명세서 확인 전이라 불확실하므로, 흔히 쓰이는 후보들을 순서대로 시도합니다.
_PRICE_FIELD_CANDIDATES = ("cur_prc", "prpr", "stck_prpr", "price", "last_price")


def _extract_price(data: dict) -> float | None:
    for key in _PRICE_FIELD_CANDIDATES:
        raw = data.get(key)
        if raw in (None, ""):
            continue
        try:
            # 키움 응답은 "+71900"/"-71900"처럼 등락 부호가 붙거나 콤마가 포함될 수 있음
            return abs(float(str(raw).replace(",", "").strip()))
        except ValueError:
            continue
    return None
