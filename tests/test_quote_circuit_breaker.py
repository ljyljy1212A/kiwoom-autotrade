"""Mock-only regression coverage for quota-safe KR/US quote lookups."""
from __future__ import annotations

import asyncio
import unittest

from src.core import kiwoom_client as client_module
from src.core.kiwoom_client import KiwoomClient
from src.utils.exceptions import KiwoomAPIError, QuoteCircuitOpenError, RetryableError


class QuoteCircuitBreakerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        client_module._QUOTE_GATES.clear()
        self.us = KiwoomClient("key", "secret", "us-account", market="US", exchange="NA", mode="mock")
        self.kr = KiwoomClient("key", "secret", "kr-account", market="KR", exchange="KRX", mode="mock")
        for client in (self.us, self.kr):
            client._quote_min_interval_sec = 0.5
            client._quote_error_threshold = 3
            client._quote_circuit_sec = 60.0

    @staticmethod
    def _gate(client):
        return client_module._quote_gate(f"{client.domain}|{client.account_no}|{client.market}")

    async def test_concurrent_same_symbol_uses_one_request(self):
        calls = 0

        async def post(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return {"cur_prc": "10"}

        self.us._post = post
        first, second = await asyncio.gather(self.us.get_quote("SOXL"), self.us.get_quote("SOXL"))
        self.assertEqual(first, second)
        self.assertEqual(calls, 1)

    async def test_429_and_1700_backoff_from_30_to_300_seconds(self):
        async def limited(*_args, **_kwargs):
            raise KiwoomAPIError("usa20100", 429, "1700: request limit exceeded")

        self.us._post = limited
        expected = (30.0, 60.0, 120.0, 240.0, 300.0)
        for cooldown in expected:
            with self.assertRaises(KiwoomAPIError):
                await self.us.get_quote("SOXL")
            gate = self._gate(self.us)
            self.assertGreaterEqual(gate.not_before - asyncio.get_running_loop().time(), cooldown - 1.0)
            self.assertEqual(gate.backoff_sec, min(cooldown * 2, 300.0))
            gate.not_before = 0.0  # simulate the expiry without sleeping

    async def test_three_1903_responses_open_60_second_circuit(self):
        calls = 0

        async def invalid_symbol(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise KiwoomAPIError("usa20100", 7, "1903: symbol information not found")

        self.us._post = invalid_symbol
        for _ in range(3):
            with self.assertRaises(KiwoomAPIError):
                await self.us.get_quote("SOXL")
        with self.assertRaises(QuoteCircuitOpenError):
            await self.us.get_quote("KORU")
        self.assertEqual(calls, 3)
        self.assertGreaterEqual(self._gate(self.us).not_before - asyncio.get_running_loop().time(), 59.0)

    async def test_success_resets_symbol_error_counter(self):
        responses = [
            KiwoomAPIError("usa20100", 7, "1903"),
            KiwoomAPIError("usa20100", 7, "1903"),
            {"cur_prc": "10"},
            KiwoomAPIError("usa20100", 7, "1903"),
        ]

        async def post(*_args, **_kwargs):
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        self.us._post = post
        for _ in range(2):
            with self.assertRaises(KiwoomAPIError):
                await self.us.get_quote("SOXL")
        self.assertEqual(self._gate(self.us).consecutive_symbol_errors, 2)
        self.assertEqual(await self.us.get_quote("SOXL"), {"cur_prc": "10"})
        self.assertEqual(self._gate(self.us).consecutive_symbol_errors, 0)
        with self.assertRaises(KiwoomAPIError):
            await self.us.get_quote("SOXL")
        self.assertEqual(self._gate(self.us).consecutive_symbol_errors, 1)

    async def test_timeout_and_5xx_open_bounded_cooldown(self):
        for failure in (RetryableError("timeout"), RetryableError("usa20100 server error: 503")):
            client_module._QUOTE_GATES.clear()

            async def post(*_args, _failure=failure, **_kwargs):
                raise _failure

            self.us._post = post
            with self.assertRaises(RetryableError):
                await self.us.get_quote("SOXL")
            with self.assertRaises(QuoteCircuitOpenError):
                await self.us.get_quote("KORU")
            self.assertGreaterEqual(self._gate(self.us).not_before - asyncio.get_running_loop().time(), 29.0)

    async def test_us_and_kr_gate_failures_are_isolated(self):
        async def us_limited(*_args, **_kwargs):
            raise KiwoomAPIError("usa20100", 429, "1700: request limit exceeded")

        async def kr_success(*_args, **_kwargs):
            return {"cur_prc": "70000"}

        self.us._post = us_limited
        self.kr._post = kr_success
        with self.assertRaises(KiwoomAPIError):
            await self.us.get_quote("SOXL")
        self.assertEqual(await self.kr.get_quote("005930"), {"cur_prc": "70000"})
        self.assertGreater(self._gate(self.us).not_before, 0.0)
        self.assertEqual(self._gate(self.kr).not_before, 0.0)

