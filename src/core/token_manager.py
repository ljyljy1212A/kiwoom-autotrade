"""접근토큰 발급/캐싱/자동 갱신.

TR: au10001 (접근토큰 발급), au10002 (접근토큰폐기)
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx
from src.core.broker_http import BrokerHTTPGate, http_operation
from src.core.rate_limit_observability import emit_rate_limit_event
from src.utils.exceptions import FatalError, RetryableError


@dataclass
class Token:
    value: str
    token_type: str
    expires_at: float  # epoch seconds


class TokenManager:
    """appkey/secretkey 로 접근토큰을 발급받고, 만료 전에 자동 재발급합니다."""

    def __init__(
        self,
        domain: str,
        appkey: str,
        secretkey: str,
        logger,
        http_gate: BrokerHTTPGate | None = None,
        *,
        account_id: str = "-",
        market: str = "-",
        mode: str = "-",
    ):
        self.domain = domain.rstrip("/")
        self.appkey = appkey
        self.secretkey = secretkey
        self.logger = logger
        self.account_id = account_id
        self.market = market
        self.mode = mode
        self.http_gate = http_gate or BrokerHTTPGate(None)
        self._token: Token | None = None
        # The first REST call and WebSocket login commonly happen together.
        # One lock prevents them from issuing duplicate au10001 requests.
        self._issue_lock = asyncio.Lock()
        self._next_issue_at = 0.0
        self._issue_backoff_sec = 30.0

    async def _issue(self) -> Token:
        remaining = self._next_issue_at - time.monotonic()
        if remaining > 0:
            raise RetryableError(f"Token issuance cooling down for {remaining:.0f}s after Kiwoom rate limit")
        url = f"{self.domain}/oauth2/token"
        payload = {
            "grant_type": "client_credentials",
            "appkey": self.appkey,
            "secretkey": self.secretkey,
        }
        headers = {"Content-Type": "application/json;charset=UTF-8", "api-id": "au10001"}
        async with self.http_gate.client(timeout=10) as client:
            try:
                with http_operation("token"):
                    resp = await client.post(url, json=payload, headers=headers, timeout=10)
            except httpx.RequestError as e:
                raise RetryableError(f"토큰 발급 네트워크 오류: {e}") from e

        if resp.status_code == 429:
            cooldown = self._issue_backoff_sec
            self._next_issue_at = time.monotonic() + cooldown
            emit_rate_limit_event(
                self.logger,
                market=self.market,
                mode=self.mode,
                account_id=self.account_id,
                appkey=self.appkey,
                api_id="au10001",
                return_code=resp.status_code,
                error_text=resp.text,
                trigger="token_backoff",
                cooldown_sec=cooldown,
            )
            self.logger.warning(
                f"Token issuance rate-limited; cooling down for {cooldown:.0f}s before retry"
            )
            self._issue_backoff_sec = min(cooldown * 2, 300.0)
            raise RetryableError(f"Token issuance rate-limited (429): {resp.text}")
        if resp.status_code >= 500:
            raise RetryableError(f"토큰 발급 서버 오류: {resp.status_code}")
        if resp.status_code != 200:
            raise FatalError(f"토큰 발급 실패({resp.status_code}): {resp.text}")

        data = resp.json()
        token_value = data.get("token")
        if not token_value:
            raise FatalError(f"토큰 발급 응답에 token 없음: {data}")

        # expires_dt 형식은 보통 'YYYYMMDDHHMMSS' 이지만, 안전하게 24시간 - 5분 마진으로 처리
        expires_at = time.time() + 23.5 * 3600
        self._next_issue_at = 0.0
        self._issue_backoff_sec = 30.0
        self.logger.info("접근토큰 발급 완료 (만료 예정: 약 23.5시간 후)")
        return Token(value=token_value, token_type=data.get("token_type", "Bearer"), expires_at=expires_at)

    async def get_token(self) -> str:
        token_valid = self._token is not None and time.time() < self._token.expires_at
        debug = getattr(self.logger, "debug", None)
        if debug is not None:
            debug(
                "Token diagnostic: "
                f"state={'cached-token-valid' if token_valid else 'fetching-new-token'}"
            )
        if not token_valid:
            async with self._issue_lock:
                # Recheck after waiting: another concurrent caller may have
                # populated the cache while this caller was blocked on the lock.
                if self._token is None or time.time() >= self._token.expires_at:
                    self._token = await self._issue()
        return f"{self._token.token_type} {self._token.value}"

    async def revoke(self) -> None:
        if self._token is None:
            await self.http_gate.close()
            return
        url = f"{self.domain}/oauth2/revoke"
        headers = {"Content-Type": "application/json;charset=UTF-8", "api-id": "au10002"}
        payload = {"appkey": self.appkey, "secretkey": self.secretkey, "token": self._token.value}
        async with self.http_gate.client(timeout=10) as client:
            try:
                await client.post(url, json=payload, headers=headers, timeout=10)
            except httpx.RequestError as e:
                self.logger.warning(f"토큰 폐기 실패(무시 가능): {e}")
        self._token = None
        await self.http_gate.close()

    def invalidate(self) -> None:
        """Discard a server-rejected token without making another network call."""
        self._token = None
