"""Regression coverage for account-wide order throttling and no-retry order posts."""
from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from src.core import kiwoom_client as client_module
from src.core.broker_http import (
    FixedPortCollisionError,
    clear_fixed_port_degraded_state,
    get_fixed_port_degraded_state,
)
from src.core.kiwoom_client import KiwoomClient
from src.core.process_lock import AccountOrderAuthority
from src.utils.exceptions import ExchangeResolutionError, KiwoomAPIError, OrderAuthorityError, RetryableError


class OrderSubmissionGuardTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        client_module._ORDER_GATES.clear()
        self.kr = KiwoomClient("key", "secret", "kr-account", market="KR", exchange="KRX", mode="mock")
        self.us = KiwoomClient("key", "secret", "us-account", market="US", exchange="ND", mode="mock")
        for client in (self.kr, self.us):
            lock = Mock()
            lock.owned_by_current_process.return_value = True
            client.bind_order_authority(AccountOrderAuthority("test", lock))
        self.kr._order_min_interval_sec = 0.2
        self.us._order_min_interval_sec = 0.2

    async def test_order_without_authority_fails_before_exchange_lookup(self):
        client = KiwoomClient("key", "secret", "unauthorized", market="US", exchange="ND", mode="mock")
        client._resolve_exchange = AsyncMock(side_effect=AssertionError("lookup must not run"))
        client._post_once = AsyncMock(side_effect=AssertionError("order must not run"))

        with self.assertRaises(OrderAuthorityError):
            await client.place_order("BUY", "NVDA", 1, 10.5)

        client._resolve_exchange.assert_not_awaited()
        client._post_once.assert_not_awaited()

    async def test_authority_loss_before_post_blocks_order(self):
        lock = Mock()
        lock.owned_by_current_process.side_effect = [True, False]
        self.kr.bind_order_authority(AccountOrderAuthority("test", lock))
        self.kr._post_once = AsyncMock(side_effect=AssertionError("order must not run"))

        with self.assertRaises(OrderAuthorityError):
            await self.kr.place_order("BUY", "005930", 1, 70000)

        self.kr._post_once.assert_not_awaited()

    async def test_concurrent_multi_symbol_orders_share_one_account_gate(self):
        call_started_at: list[float] = []

        async def post_once(*_args, **_kwargs):
            call_started_at.append(time.monotonic())
            await asyncio.sleep(0.01)
            return {"ord_no": f"{len(call_started_at):07d}"}

        self.kr._post_once = post_once

        first, second = await asyncio.gather(
            self.kr.place_order("BUY", "005930", 1, 70000),
            self.kr.place_order("BUY", "000660", 1, 80000),
        )

        self.assertEqual(first.ord_no, "0000001")
        self.assertEqual(second.ord_no, "0000002")
        self.assertEqual(len(call_started_at), 2)
        self.assertGreaterEqual(call_started_at[1] - call_started_at[0], 0.18)

    async def test_lost_response_does_not_retry_order_submission(self):
        calls = 0

        async def post_once(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise RetryableError("simulated lost response")

        self.us._post_once = post_once
        self.us._exchange_cache["NVDA"] = "ND"

        with self.assertRaises(RetryableError):
            await self.us.place_order("BUY", "NVDA", 1, 10.5)

        self.assertEqual(calls, 1)

    async def test_rest_fixed_port_collision_enters_account_degraded_state(self):
        account_id = "fixed-port-state-account"
        client = KiwoomClient("key", "secret", account_id, market="US", exchange="ND", mode="mock")
        client._headers = AsyncMock(return_value={})
        http_client = AsyncMock()

        async def fail_with_collision(*_args, **_kwargs):
            try:
                raise FixedPortCollisionError(OSError(98, "address already in use"))
            except FixedPortCollisionError as collision:
                raise httpx.ConnectError("fixed-port collision") from collision

        http_client.post.side_effect = fail_with_collision
        try:
            with patch.object(client_module.httpx, "AsyncClient", return_value=http_client):
                with self.assertRaises(RetryableError):
                    await client._post_once("/api/us/quote", "usa10001", {})
            state = get_fixed_port_degraded_state(account_id)
            self.assertIsNotNone(state)
            self.assertEqual(state.operation, "rest")
        finally:
            clear_fixed_port_degraded_state(account_id)
            await client._http_gate.close()

    async def test_exchange_cache_hit_does_not_lookup(self):
        self.us._post = AsyncMock(side_effect=AssertionError("cache hit must not call lookup"))
        self.assertEqual(await self.us._resolve_exchange("SPCX"), "ND")

    async def test_exchange_cache_miss_looks_up_and_populates_cache(self):
        self.us._post = AsyncMock(return_value={"list": [{"stex_tp": "NY", "stk_cd": "NEWCO"}]})
        self.assertEqual(await self.us._resolve_exchange("NEWCO"), "NY")
        self.assertEqual(self.us._exchange_cache["NEWCO"], "NY")
        self.us._post.assert_awaited_once_with("/api/us/stkinfo", "usa10098", {"stk_cd": "NEWCO"})

    async def test_exchange_lookup_failure_blocks_and_alerts(self):
        self.us._post = AsyncMock(side_effect=KiwoomAPIError("usa10098", 7, "not found"))
        alert = AsyncMock()
        self.us.set_exchange_alert_callback(alert)
        with self.assertRaises(ExchangeResolutionError):
            await self.us.place_order("BUY", "UNKNOWN", 1, 10.5)
        alert.assert_awaited_once()

    async def test_token_rejection_does_not_retry_order_submission(self):
        response = Mock(status_code=200, content=b"{}")
        response.json.return_value = {
            "return_code": 3,
            "return_msg": "8005 token rejected",
        }
        http_client = AsyncMock()
        http_client.__aenter__.return_value = http_client
        http_client.__aexit__.return_value = False
        http_client.post.return_value = response

        self.us._headers = AsyncMock(return_value={})
        self.us.token_mgr.invalidate = Mock()
        with patch.object(client_module.httpx, "AsyncClient", return_value=http_client):
            with self.assertRaises(KiwoomAPIError):
                await self.us._post_once(
                    "/api/us/ordr",
                    "ust20000",
                    {"stk_cd": "NVDA"},
                    allow_reauth_retry=False,
                )

        http_client.post.assert_awaited_once()
        self.us.token_mgr.invalidate.assert_not_called()
