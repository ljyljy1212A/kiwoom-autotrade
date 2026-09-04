from __future__ import annotations

import asyncio
import socket
import ssl
import tempfile
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from src.core import broker_http
from src.core import kiwoom_client as client_module
from src.core.broker_http import (
    BrokerHTTPGate,
    FixedPortCollisionError,
    FixedPortDegradedState,
    _FixedPortAnyIOBackend,
    _connect_with_reuseaddr,
    clear_fixed_port_degraded_state,
    enter_fixed_port_degraded_state,
    fixed_port_holdoff_active,
    get_fixed_port_degraded_state,
    is_fixed_port_collision_error,
)
from src.core.kiwoom_client import KiwoomClient
from src.utils.exceptions import RetryableError


class FixedPortHoldoffTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        for account in ("holdoff-account", "caller-account", "shared-account"):
            clear_fixed_port_degraded_state(account)

    def test_fixed_port_collision_enters_160_second_account_port_holdoff(self):
        entered = datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(broker_http, "DATA_DIR", Path(temp_dir)):
                state = enter_fixed_port_degraded_state(
                    "holdoff-account", "rest", local_port=443, market="US", now=entered
                )

        self.assertEqual(state.holdoff_until, entered + timedelta(seconds=160))
        self.assertIsNotNone(
            fixed_port_holdoff_active(
                "holdoff-account", 443, now=state.holdoff_until - timedelta(microseconds=1)
            )
        )
        self.assertIsNone(
            fixed_port_holdoff_active(
                "holdoff-account", 443, now=state.holdoff_until + timedelta(microseconds=1)
            )
        )

    def test_holdoff_is_active_before_expiry_and_expires_at_boundary(self):
        entered = datetime(2026, 9, 4, 1, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(broker_http, "DATA_DIR", Path(temp_dir)):
                state = enter_fixed_port_degraded_state(
                    "holdoff-account", "rest", local_port=10000, market="KR", now=entered
                )
                self.assertEqual(state.holdoff_until, entered + timedelta(seconds=160))
                self.assertIsNotNone(
                    fixed_port_holdoff_active(
                        "holdoff-account", 10000, now=entered + timedelta(seconds=159.999)
                    )
                )
                self.assertIsNone(
                    fixed_port_holdoff_active(
                        "holdoff-account", 10000, now=entered + timedelta(seconds=160)
                    )
                )

    def test_bind_dns_timeout_tls_and_unrelated_oserror_do_not_enter_holdoff(self):
        failures = {
            "bind": OSError(98, "address already in use"),
            "dns": OSError(11001, "name or service not known"),
            "timeout": TimeoutError("timed out"),
            "tls": httpx.ConnectError("TLS handshake failed"),
            "unrelated": OSError(13, "permission denied"),
        }
        for name, failure in failures.items():
            with self.subTest(failure=name):
                clear_fixed_port_degraded_state("holdoff-account")
                if name == "tls":
                    failure.__cause__ = ssl.SSLError("certificate failure")
                    self.assertFalse(is_fixed_port_collision_error(failure))
                    self.assertIsNone(get_fixed_port_degraded_state("holdoff-account"))
                    continue

                fake_socket = Mock()
                fake_socket.bind.side_effect = failure if name == "bind" else None
                fake_socket.connect.side_effect = failure if name in {"timeout", "unrelated"} else None
                getaddrinfo = Mock(side_effect=failure) if name == "dns" else Mock(
                    return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 443))]
                )
                with patch.object(broker_http.socket, "getaddrinfo", getaddrinfo), patch.object(
                    broker_http.socket, "socket", return_value=fake_socket
                ):
                    with self.assertRaises(OSError):
                        _connect_with_reuseaddr("127.0.0.1", 443, "0.0.0.0", 443, 1.0, [], None)
                self.assertFalse(is_fixed_port_collision_error(failure))
                self.assertIsNone(get_fixed_port_degraded_state("holdoff-account"))

    async def test_active_holdoff_skips_connect_and_logs_structured_warning(self):
        entered = datetime.now(timezone.utc) - timedelta(seconds=5)
        logger = Mock()
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(broker_http, "DATA_DIR", Path(temp_dir)):
                state = enter_fixed_port_degraded_state(
                    "holdoff-account", "quotes", local_port=443, market="US", now=entered
                )
                backend = _FixedPortAnyIOBackend(443, logger, "holdoff-account", "US")
                with patch.object(broker_http, "map_exceptions", return_value=nullcontext()), patch.object(
                    broker_http.socket, "socket", side_effect=AssertionError("socket must not be created")
                ):
                    with self.assertRaises(FixedPortCollisionError) as raised:
                        await backend.connect_tcp("127.0.0.1", 443, timeout=1.0)

        self.assertTrue(raised.exception.holdoff_active)
        self.assertEqual(logger.warning.call_count, 1)
        warning = logger.warning.call_args.args[0]
        self.assertIn("account=holdoff-account", warning)
        self.assertIn("market=US", warning)
        self.assertIn("local_port=443", warning)
        self.assertIn("operation=unknown", warning)
        self.assertIn(f"holdoff_until={state.holdoff_until.isoformat()}", warning)
        self.assertIn("skipped=true", warning)

    async def test_skipped_balance_cycle_returns_retryable_failure_and_preserves_state(self):
        account = "caller-account"
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(broker_http, "DATA_DIR", Path(temp_dir)):
                state = enter_fixed_port_degraded_state(account, "rest", local_port=443, market="US")
                client = KiwoomClient("key", "secret", account, market="US", exchange="ND", mode="mock")
                client._headers = AsyncMock(return_value={})
                with patch.object(
                    client_module.account_catalog,
                    "reconciliation_clearance_eligible",
                    return_value=True,
                ):
                    with self.assertRaises(RetryableError):
                        await client._post_once("/api/us/acnt", "ust21070", {})
                self.assertEqual(get_fixed_port_degraded_state(account), state)
                await client._http_gate.close()

    async def test_skipped_order_remains_fail_closed_and_creates_no_pending_order(self):
        account = "caller-account"
        authority_lock = Mock()
        authority_lock.owned_by_current_process.return_value = True
        attempt_store = Mock()
        attempt_store.record_attempt.return_value = SimpleNamespace(attempt_id="attempt-1")
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(broker_http, "DATA_DIR", Path(temp_dir)):
                enter_fixed_port_degraded_state(account, "rest", local_port=443, market="US")
                client = KiwoomClient("key", "secret", account, market="US", exchange="ND", mode="mock")
                client.bind_order_authority(client_module.AccountOrderAuthority("test", authority_lock))
                client._exchange_cache["NVDA"] = "ND"
                client._order_attempt_store = attempt_store
                client._headers = AsyncMock(return_value={})
                with patch.object(
                    client_module.account_catalog,
                    "reconciliation_clearance_eligible",
                    return_value=True,
                ):
                    with self.assertRaises(RetryableError):
                        await client.place_order("BUY", "NVDA", 1, 10.5)
                attempt_store.mark_pending.assert_not_called()
                await client._http_gate.close()

    async def test_token_quote_order_and_cancel_share_account_transport_holdoff(self):
        account = "shared-account"
        authority_lock = Mock()
        authority_lock.owned_by_current_process.return_value = True
        attempt_store = Mock()
        attempt_store.record_attempt.return_value = SimpleNamespace(attempt_id="attempt-2")
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(broker_http, "DATA_DIR", Path(temp_dir)):
                enter_fixed_port_degraded_state(account, "rest", local_port=443, market="US")
                client = KiwoomClient("key", "secret", account, market="US", exchange="ND", mode="mock")
                client.bind_order_authority(client_module.AccountOrderAuthority("test", authority_lock))
                client._exchange_cache["NVDA"] = "ND"
                client._order_attempt_store = attempt_store
                client._headers = AsyncMock(return_value={})
                with patch.object(
                    client_module.account_catalog,
                    "reconciliation_clearance_eligible",
                    return_value=True,
                ):
                    with self.assertRaises(RetryableError):
                        await client.token_mgr._issue()
                    with self.assertRaises(RetryableError):
                        await client.get_quote("NVDA")
                    with self.assertRaises(RetryableError):
                        await client.place_order("BUY", "NVDA", 1, 10.5)
                    with self.assertRaises(RetryableError):
                        await client.cancel_order("NVDA", "orig-1", 1)
                self.assertIsNotNone(get_fixed_port_degraded_state(account))
                await client._http_gate.close()

    async def test_real_mode_constructs_gate_without_fixed_port_transport(self):
        with patch.object(broker_http, "FixedPortAsyncHTTPTransport") as transport:
            client = KiwoomClient("key", "secret", "real-account", market="US", exchange="ND", mode="real")
            self.assertIsInstance(client._http_gate, BrokerHTTPGate)
            self.assertIsNone(client._http_gate.local_port)
            async with client._http_gate.client(timeout=0.1):
                pass
            transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
