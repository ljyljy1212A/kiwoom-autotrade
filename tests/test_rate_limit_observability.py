from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from src.core import engine as engine_module
from src.core import kiwoom_client as client_module
from src.core.kiwoom_client import KiwoomClient
from src.core.rate_limit_observability import emit_rate_limit_event
from src.core.token_manager import TokenManager
from src.utils.exceptions import KiwoomAPIError, OrderRejectedError, RetryableError


class _CaptureLogger:
    def __init__(self):
        self.events = []
        self.warning_messages = []

    def bind(self, **fields):
        bound = _CaptureLogger()
        bound.events = self.events
        bound.warning_messages = self.warning_messages
        bound._fields = fields
        return bound

    def warning(self, message):
        self.warning_messages.append(message)
        if hasattr(self, "_fields"):
            self.events.append((self._fields, message))


class _AsyncClientContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, *_args, **_kwargs):
        return self.response


class RateLimitObservabilityTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.logger = _CaptureLogger()

    def test_schema_classification_fingerprint_and_redaction(self):
        appkey = "APPKEY-ONLY-FOR-TEST"
        secret = "SECRET-MUST-NOT-APPEAR"
        token = "TOKEN-MUST-NOT-APPEAR"
        event = emit_rate_limit_event(
            self.logger,
            market="KR",
            mode="mock",
            account_id="account-1",
            appkey=appkey,
            api_id="ka10001",
            return_code=429,
            error_text=f"1702 response; {secret}; {token}",
            trigger="http_429",
            cooldown_sec=30.0,
        )

        self.assertEqual(event["event"], "kiwoom_rate_limit_event")
        self.assertEqual(event["quota_tier"], "1702")
        self.assertEqual(event["mode"], "mock")
        self.assertEqual(event["appkey_fingerprint"], hashlib.sha256(appkey.encode()).hexdigest()[:12])
        serialized = json.dumps(self.logger.events, ensure_ascii=False)
        self.assertNotIn(appkey, serialized)
        self.assertNotIn(secret, serialized)
        self.assertNotIn(token, serialized)

    async def test_quote_hooks_emit_without_changing_circuit_behavior(self):
        client_module._QUOTE_GATES.clear()
        client = KiwoomClient("quote-key", "quote-secret", "us-account", market="US", mode="mock")
        client._quote_min_interval_sec = 0.0
        client.logger = self.logger
        client.token_mgr.logger = self.logger
        gate = client_module._quote_gate(f"{client.domain}|{client.account_no}|{client.market}")
        exc = KiwoomAPIError("usa20100", 429, "1700: request limit exceeded")
        client._record_quote_failure(gate, exc)
        self.assertEqual(gate.backoff_sec, 60.0)
        self.assertGreater(gate.not_before, 0.0)
        self.assertEqual(self.logger.events[-1][0]["quota_tier"], "1700")
        self.assertEqual(self.logger.events[-1][0]["mode"], "mock")

        async def retryable(*_args, **_kwargs):
            raise RetryableError("temporary 503")

        client._post = retryable
        gate.not_before = 0.0
        with self.assertRaises(RetryableError):
            await client.get_quote("SOXL")
        self.assertEqual(self.logger.events[-1][0]["trigger"], "retryable_backoff")

    async def test_token_429_hook_preserves_backoff_and_fields(self):
        response = SimpleNamespace(status_code=429, text="1701 total quota")
        manager = TokenManager(
            "https://mockapi.kiwoom.com",
            "token-key",
            "token-secret",
            self.logger,
            account_id="us-account",
            market="US",
            mode="mock",
        )
        manager.http_gate.client = lambda **_kwargs: _AsyncClientContext(response)
        with self.assertRaises(RetryableError):
            await manager._issue()
        self.assertEqual(manager._issue_backoff_sec, 60.0)
        fields = self.logger.events[-1][0]
        self.assertEqual(fields["api_id"], "au10001")
        self.assertEqual(fields["quota_tier"], "1701")
        self.assertEqual(fields["status_code"], 429)

    def _engine(self, *, balance_only, account_id="account-1"):
        logger = self.logger
        client = SimpleNamespace(
            market="KR",
            mode="mock",
            token_mgr=SimpleNamespace(appkey="engine-key"),
        )
        ctx = SimpleNamespace(account_id=account_id, client=client, logger=logger, strategy=SimpleNamespace(symbol="005930"))
        obj = engine_module.AccountEngine.__new__(engine_module.AccountEngine)
        obj.ctx = ctx
        obj._sync_lock = asyncio.Lock()
        obj._balance_gate = engine_module._AccountBalanceGate()
        obj._balance_only = balance_only
        obj._balance_sync_blocked = False
        obj._last_balance_request_at = 0.0
        obj._last_balance_reconciliation = 0.0
        obj._last_execution_query_at = 0.0
        obj._last_execution_unavailable_symbol = ""
        obj.balance_reconcile_sec = 0.0
        obj.execution_query_min_interval_sec = 0.0
        return obj

    async def test_all_three_balance_hooks_emit_and_preserve_deferral(self):
        error = KiwoomAPIError("ka10076", 429, "1700 quota")

        balance_only = self._engine(balance_only=True)
        balance_only._reconcile_balance = AsyncMock(side_effect=error)
        self.assertFalse(await balance_only.sync_broker_state())

        pending = self._engine(balance_only=False)
        pending.ledger = SimpleNamespace(
            pending_orders=lambda _symbol: True,
            execution_recovery_orders=lambda _symbol: [],
        )
        pending.ctx.client.get_executed_orders = AsyncMock(side_effect=error)
        pending._cancel_stale_orders = AsyncMock()
        pending.balance_reconcile_sec = 10**9
        pending._last_balance_reconciliation = asyncio.get_running_loop().time()
        self.assertTrue(await pending.sync_broker_state())

        normal = self._engine(balance_only=False)
        normal.ledger = SimpleNamespace(pending_orders=lambda _symbol: False)
        normal._reconcile_balance = AsyncMock(side_effect=error)
        self.assertFalse(await normal.sync_broker_state(force_balance=True))

        balance_events = [fields for fields, _message in self.logger.events if fields["trigger"] == "balance_reconciliation_deferred"]
        self.assertEqual(len(balance_events), 3)
        self.assertTrue(all(event["quota_tier"] == "1700" for event in balance_events))

    async def test_order_cancellation_hook_filters_unrelated_errors(self):
        order = SimpleNamespace(
            created_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            requested_qty=1,
            filled_qty=0,
            status="pending",
            ord_no="order-1",
            symbol="005930",
            side="BUY",
        )
        engine = self._engine(balance_only=False)
        engine.pending_order_cancel_after_sec = 1.0
        engine.ledger = SimpleNamespace(execution_recovery_orders=lambda _symbol: [order])
        engine.ctx.client.cancel_order = AsyncMock(side_effect=OrderRejectedError("ordinary rejection"))
        await engine._cancel_stale_orders()
        self.assertFalse(self.logger.events)

        self.logger.events.clear()
        engine.ctx.client.cancel_order = AsyncMock(side_effect=RetryableError("429 cancellation quota"))
        await engine._cancel_stale_orders()
        self.assertEqual(len(self.logger.events), 1)
        self.assertEqual(self.logger.events[0][0]["quota_tier"], "none")
        self.assertEqual(self.logger.events[0][0]["trigger"], "cancellation_deferred")
