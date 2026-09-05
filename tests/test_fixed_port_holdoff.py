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
    record_fixed_port_ongoing_status,
    restore_fixed_port_degraded_state,
)
from src.core.kiwoom_client import KiwoomClient
from src.utils.exceptions import RetryableError


class FixedPortHoldoffTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        for account in ("holdoff-account", "caller-account", "shared-account"):
            clear_fixed_port_degraded_state(account)

    def _make_fake_gate(self, logger=None):
        clients = []

        def make_client(*args, **kwargs):
            client = Mock()
            client.aclose = AsyncMock()
            clients.append(client)
            return client

        gate = BrokerHTTPGate(
            443,
            logger=logger or Mock(),
            account_id="holdoff-account",
            market="US",
        )
        return gate, clients, make_client

    async def _raise_gate_failure(self, gate):
        with self.assertRaises(RuntimeError):
            async with gate.client(timeout=1):
                raise RuntimeError("request failed")

    async def test_client_recycle_below_threshold_preserves_client_identity(self):
        gate, clients, make_client = self._make_fake_gate()
        with patch.object(broker_http, "FixedPortAsyncHTTPTransport", return_value=Mock()), patch.object(
            broker_http, "httpx"
        ) as httpx_module, patch.object(broker_http, "fixed_port_holdoff_active", return_value=None):
            httpx_module.AsyncClient.side_effect = make_client
            await self._raise_gate_failure(gate)
            await self._raise_gate_failure(gate)
            self.assertEqual(len(clients), 1)
            self.assertEqual(clients[0].aclose.await_count, 0)
            async with gate.client(timeout=1) as client:
                self.assertIs(client, clients[0])

    async def test_client_recycle_at_threshold_without_holdoff_replaces_client(self):
        gate, clients, make_client = self._make_fake_gate()
        with patch.object(broker_http, "FixedPortAsyncHTTPTransport", return_value=Mock()), patch.object(
            broker_http, "httpx"
        ) as httpx_module, patch.object(broker_http, "fixed_port_holdoff_active", return_value=None):
            httpx_module.AsyncClient.side_effect = make_client
            for _ in range(3):
                await self._raise_gate_failure(gate)
            self.assertEqual(clients[0].aclose.await_count, 1)
            self.assertEqual(gate._consecutive_failures, 0)
            async with gate.client(timeout=1) as client:
                self.assertIsNot(client, clients[0])
                self.assertIs(client, clients[1])

    async def test_client_recycle_at_threshold_with_holdoff_preserves_client(self):
        gate, clients, make_client = self._make_fake_gate()
        now = datetime.now(timezone.utc)
        holdoff = FixedPortDegradedState(
            account_id="holdoff-account",
            entered_at=now,
            last_collision_at=now,
            operation="rest",
            next_recovery_probe_at=now,
            local_port=443,
            holdoff_until=now + timedelta(seconds=160),
        )
        with patch.object(broker_http, "FixedPortAsyncHTTPTransport", return_value=Mock()), patch.object(
            broker_http, "httpx"
        ) as httpx_module, patch.object(
            broker_http, "fixed_port_holdoff_active", return_value=holdoff
        ):
            httpx_module.AsyncClient.side_effect = make_client
            for _ in range(3):
                await self._raise_gate_failure(gate)
            self.assertEqual(clients[0].aclose.await_count, 0)
            self.assertEqual(gate._consecutive_failures, 3)
            async with gate.client(timeout=1) as client:
                self.assertIs(client, clients[0])

    async def test_client_recycle_failure_counter_resets_after_success(self):
        gate, clients, make_client = self._make_fake_gate()
        with patch.object(broker_http, "FixedPortAsyncHTTPTransport", return_value=Mock()), patch.object(
            broker_http, "httpx"
        ) as httpx_module, patch.object(broker_http, "fixed_port_holdoff_active", return_value=None):
            httpx_module.AsyncClient.side_effect = make_client
            await self._raise_gate_failure(gate)
            await self._raise_gate_failure(gate)
            async with gate.client(timeout=1):
                pass
            self.assertEqual(gate._consecutive_failures, 0)
            await self._raise_gate_failure(gate)
            self.assertEqual(gate._consecutive_failures, 1)
            self.assertEqual(clients[0].aclose.await_count, 0)

    async def test_close_still_closes_and_nulls_client_with_failure_counter(self):
        gate, clients, make_client = self._make_fake_gate()
        with patch.object(broker_http, "FixedPortAsyncHTTPTransport", return_value=Mock()), patch.object(
            broker_http, "httpx"
        ) as httpx_module:
            httpx_module.AsyncClient.side_effect = make_client
            async with gate.client(timeout=1):
                pass
            gate._consecutive_failures = 2
            await gate.close()
            self.assertEqual(clients[0].aclose.await_count, 1)
            self.assertIsNone(gate._client)

    async def test_client_recycle_warning_is_rate_limited(self):
        logger = Mock()
        gate, clients, make_client = self._make_fake_gate(logger=logger)
        with patch.object(broker_http, "FixedPortAsyncHTTPTransport", return_value=Mock()), patch.object(
            broker_http, "httpx"
        ) as httpx_module, patch.object(broker_http, "fixed_port_holdoff_active", return_value=None):
            httpx_module.AsyncClient.side_effect = make_client
            for _ in range(3):
                await self._raise_gate_failure(gate)
            for _ in range(3):
                await self._raise_gate_failure(gate)
            self.assertEqual(clients[0].aclose.await_count, 1)
            self.assertEqual(clients[1].aclose.await_count, 1)
            self.assertEqual(logger.warning.call_count, 1)

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
        self.assertIn("no broker request was sent", warning)

    async def test_exhausted_collision_enters_holdoff_but_other_connect_error_does_not(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(broker_http, "DATA_DIR", Path(temp_dir)):
                collision = FixedPortCollisionError(OSError(10048, "address already in use"))
                backend = _FixedPortAnyIOBackend(443, Mock(), "holdoff-account", "US")
                with patch.object(broker_http, "map_exceptions", return_value=nullcontext()), patch.object(
                    broker_http, "_connect_with_reuseaddr", side_effect=collision
                ) as connect:
                    with self.assertRaises(FixedPortCollisionError):
                        await backend.connect_tcp("127.0.0.1", 443, timeout=1.0)
                connect.assert_called_once()
                state = get_fixed_port_degraded_state("holdoff-account")
                self.assertIsNotNone(state)
                self.assertEqual(state.local_port, 443)

                clear_fixed_port_degraded_state("holdoff-account")
                backend = _FixedPortAnyIOBackend(443, Mock(), "holdoff-account", "US")
                ordinary = OSError(10061, "connection refused")
                with patch.object(broker_http, "map_exceptions", return_value=nullcontext()), patch.object(
                    broker_http, "_connect_with_reuseaddr", side_effect=ordinary
                ):
                    with self.assertRaises(OSError) as raised:
                        await backend.connect_tcp("127.0.0.1", 443, timeout=1.0)
                self.assertIs(raised.exception, ordinary)
                self.assertIsNone(get_fixed_port_degraded_state("holdoff-account"))

    async def test_expired_holdoff_allows_one_probe_and_restarts_interval_on_collision(self):
        entered = datetime.now(timezone.utc) - timedelta(seconds=161)
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(broker_http, "DATA_DIR", Path(temp_dir)):
                old_state = enter_fixed_port_degraded_state(
                    "holdoff-account", "rest", local_port=443, market="US", now=entered
                )
                collision = FixedPortCollisionError(OSError(10048, "address already in use"))
                backend = _FixedPortAnyIOBackend(443, Mock(), "holdoff-account", "US")
                with patch.object(broker_http, "fixed_port_holdoff_active", return_value=None), patch.object(
                    broker_http, "map_exceptions", return_value=nullcontext()
                ), patch.object(broker_http, "_connect_with_reuseaddr", side_effect=collision) as connect:
                    with self.assertRaises(FixedPortCollisionError):
                        await backend.connect_tcp("127.0.0.1", 443, timeout=1.0)
                connect.assert_called_once()
                new_state = get_fixed_port_degraded_state("holdoff-account")
                self.assertGreater(new_state.holdoff_until, old_state.holdoff_until)

    def test_holdoff_account_and_port_isolation(self):
        now = datetime(2026, 9, 4, 2, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(broker_http, "DATA_DIR", Path(temp_dir)):
                enter_fixed_port_degraded_state("holdoff-account", "rest", local_port=443, now=now)
                enter_fixed_port_degraded_state("caller-account", "rest", local_port=10000, now=now)
                self.assertIsNotNone(fixed_port_holdoff_active("holdoff-account", 443, now=now))
                self.assertIsNone(fixed_port_holdoff_active("holdoff-account", 10000, now=now))
                self.assertIsNotNone(fixed_port_holdoff_active("caller-account", 10000, now=now))
                self.assertIsNone(fixed_port_holdoff_active("caller-account", 443, now=now))

    def test_holdoff_persistence_restores_account_scoped_expiry(self):
        entered = datetime(2026, 9, 4, 3, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(broker_http, "DATA_DIR", Path(temp_dir)):
                expected = enter_fixed_port_degraded_state(
                    "holdoff-account", "rest", local_port=443, market="US", now=entered
                )
                clear_fixed_port_degraded_state("holdoff-account")
                restored = restore_fixed_port_degraded_state("holdoff-account", now=entered)
        self.assertEqual(restored, expected)

    async def test_repeated_active_skips_are_rate_limited_to_ongoing_interval(self):
        logger = Mock()
        entered = datetime.now(timezone.utc) - timedelta(seconds=5)
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(broker_http, "DATA_DIR", Path(temp_dir)):
                enter_fixed_port_degraded_state(
                    "holdoff-account", "quotes", local_port=443, market="US", now=entered
                )
                backend = _FixedPortAnyIOBackend(443, logger, "holdoff-account", "US")
                with patch.object(broker_http, "map_exceptions", return_value=nullcontext()), patch.object(
                    broker_http.socket, "socket", side_effect=AssertionError("socket must not be created")
                ):
                    for _ in range(2):
                        with self.assertRaises(FixedPortCollisionError):
                            await backend.connect_tcp("127.0.0.1", 443, timeout=1.0)
                self.assertEqual(logger.warning.call_count, 1)

                record_fixed_port_ongoing_status(
                    "holdoff-account", now=datetime.now(timezone.utc) - timedelta(minutes=16)
                )
                with patch.object(broker_http, "map_exceptions", return_value=nullcontext()), patch.object(
                    broker_http.socket, "socket", side_effect=AssertionError("socket must not be created")
                ):
                    with self.assertRaises(FixedPortCollisionError):
                        await backend.connect_tcp("127.0.0.1", 443, timeout=1.0)
                self.assertEqual(logger.warning.call_count, 2)

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
                updated = get_fixed_port_degraded_state(account)
                self.assertEqual(updated.account_id, state.account_id)
                self.assertEqual(updated.entered_at, state.entered_at)
                self.assertEqual(updated.operation, state.operation)
                self.assertEqual(updated.local_port, state.local_port)
                self.assertEqual(updated.holdoff_until, state.holdoff_until)
                self.assertIsNotNone(updated.last_ongoing_status_at)
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
                        await client.get_balance()
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
