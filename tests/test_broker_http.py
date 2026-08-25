import asyncio
import socket
import struct
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from httpcore._async.http11 import AsyncHTTP11Connection, HTTPConnectionState
from httpcore._models import Origin

from src.core.broker_http import (
    FixedPortCollisionError,
    clear_fixed_port_degraded_state,
    enter_fixed_port_degraded_state,
    FixedPortAsyncHTTPTransport,
    get_fixed_port_degraded_state,
    _CloseCompletionState,
    _FixedPortAnyIOBackend,
    _FIXED_PORT_CLOSE_WAIT_TIMEOUT_SEC,
    _LingerOnCloseByteStream,
    _connect_with_reuseaddr,
    _install_close_completion_hook,
)


class _ResponseHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class _HoldServer:
    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _ResponseHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def address(self):
        return self.server.server_address

    def start(self):
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class _AcceptedSocketServer:
    def __init__(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.server.settimeout(2)
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.connection = None

    @property
    def address(self):
        return self.server.getsockname()

    def _accept(self):
        try:
            self.connection, _ = self.server.accept()
            self.connection.settimeout(0.2)
            while True:
                try:
                    if self.connection.recv(1) == b"":
                        return
                except socket.timeout:
                    continue
        except OSError:
            return

    def start(self):
        self.thread.start()

    def close(self):
        self.server.close()
        if self.connection is not None:
            self.connection.close()
        self.thread.join(timeout=2)


class _FakeStream:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


class _SocketSpy:
    def __init__(self):
        self.options = []

    def setsockopt(self, *option):
        self.options.append(option)


class _FakeProtocol:
    def __init__(self):
        self.calls = 0

    def connection_lost(self, exc):
        self.calls += 1


class _ProtocolStream(_FakeStream):
    def __init__(self):
        super().__init__()
        self._protocol = _FakeProtocol()


class _ExpiryStream:
    def __init__(self, readable):
        self.readable = readable

    def get_extra_info(self, info):
        return self.readable if info == "is_readable" else None


class BrokerHTTPCloseTest(unittest.IsolatedAsyncioTestCase):
    async def test_linger_is_set_only_at_fixed_http_stream_close(self):
        stream = _FakeStream()
        raw_socket = _SocketSpy()
        close_state = _CloseCompletionState()
        wrapped = _LingerOnCloseByteStream(stream, None, raw_socket, close_state)
        await wrapped.aclose()
        self.assertEqual(
            raw_socket.options,
            [(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))],
        )
        self.assertTrue(stream.closed)
        self.assertTrue(close_state.event.is_set())
        self.assertEqual(close_state.completion_count, 1)

    async def test_close_completion_event_starts_set(self):
        self.assertTrue(_CloseCompletionState().event.is_set())

    async def test_connection_lost_hook_completes_once_after_callback(self):
        stream = _ProtocolStream()
        close_state = _CloseCompletionState()
        _install_close_completion_hook(stream, close_state)
        close_state.begin_close()
        stream._protocol.connection_lost(None)
        self.assertEqual(stream._protocol.calls, 1)
        self.assertFalse(close_state.event.is_set())
        await asyncio.sleep(0)
        self.assertTrue(close_state.event.is_set())
        stream._protocol.connection_lost(None)
        await asyncio.sleep(0)
        self.assertEqual(stream._protocol.calls, 2)
        self.assertTrue(close_state.event.is_set())
        self.assertEqual(close_state.completion_count, 1)

    async def test_backend_waits_before_connect_and_times_out_closed(self):
        from unittest.mock import patch

        backend = __import__("src.core.broker_http", fromlist=["_FixedPortAnyIOBackend"])._FixedPortAnyIOBackend(43210)
        backend._close_state.event.clear()
        with patch("src.core.broker_http._connect_with_reuseaddr") as connect:
            with self.assertRaises(Exception) as raised:
                await backend.connect_tcp("127.0.0.1", 1, timeout=0.2)
        self.assertIn("Timed out waiting", str(raised.exception))
        connect.assert_not_called()

    async def test_backend_waits_until_close_completion_before_connect(self):
        from unittest.mock import AsyncMock, patch

        backend = __import__("src.core.broker_http", fromlist=["_FixedPortAnyIOBackend"])._FixedPortAnyIOBackend(43211)
        backend._close_state.event.clear()
        raw_socket = _SocketSpy()
        fake_stream = _ProtocolStream()
        connect_started = asyncio.Event()

        def connect_socket(*_args):
            connect_started.set()
            return raw_socket

        with patch("src.core.broker_http._connect_with_reuseaddr", side_effect=connect_socket), patch(
            "src.core.broker_http.anyio.abc.SocketStream.from_socket",
            new=AsyncMock(return_value=fake_stream),
        ):
            task = asyncio.create_task(backend.connect_tcp("127.0.0.1", 1, timeout=1))
            await asyncio.sleep(0.05)
            self.assertFalse(connect_started.is_set())
            backend._close_state.event.set()
            await task
        self.assertTrue(connect_started.is_set())

    async def test_keepalive_and_server_disconnect_use_common_expiry_path(self):
        for trigger in ("keepalive_expired", "server_disconnected"):
            with self.subTest(trigger=trigger):
                stream = _ExpiryStream(readable=trigger == "server_disconnected")
                connection = AsyncHTTP11Connection(
                    Origin("http", "127.0.0.1", 80),
                    stream,
                    keepalive_expiry=30.0,
                )
                connection._state = HTTPConnectionState.IDLE
                connection._expire_at = (
                    time.monotonic() - 1.0 if trigger == "keepalive_expired" else time.monotonic() + 30.0
                )
                self.assertTrue(connection.has_expired())

    async def test_delayed_close_loopback_churn_waits_before_each_rebind(self):
        from unittest.mock import patch

        http_servers = [_HoldServer() for _ in range(6)]
        for http_server in http_servers:
            http_server.start()
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", 0))
            local_port = probe.getsockname()[1]
        finally:
            probe.close()

        backend = _FixedPortAnyIOBackend(local_port)
        connect_calls = []
        try:
            for cycle in range(3):
                http_server = http_servers[cycle * 2]
                reconnect_server = http_servers[cycle * 2 + 1]
                with patch(
                    "src.core.broker_http._connect_with_reuseaddr",
                    side_effect=lambda *args: (connect_calls.append(time.monotonic()), _connect_with_reuseaddr(*args))[1],
                ):
                    stream = await backend.connect_tcp(
                        "127.0.0.1",
                        http_server.address[1],
                        timeout=1,
                    )
                await stream.aclose()
                close_state = backend._close_state
                await asyncio.sleep(0)
                close_state.completion_count = 0
                close_state.completion_scheduled = False
                close_state.event.clear()
                release_at = time.monotonic() + 0.05
                release_task = asyncio.create_task(asyncio.sleep(0.05))

                async def release_after_delay():
                    await release_task
                    close_state.event.set()

                delayed_release = asyncio.create_task(release_after_delay())
                before_wait = len(connect_calls)
                with patch(
                    "src.core.broker_http._connect_with_reuseaddr",
                    side_effect=lambda *args: (connect_calls.append(time.monotonic()), _connect_with_reuseaddr(*args))[1],
                ):
                    reconnect = asyncio.create_task(
                        backend.connect_tcp("127.0.0.1", reconnect_server.address[1], timeout=1)
                    )
                    await asyncio.sleep(0.01)
                    self.assertEqual(len(connect_calls), before_wait)
                    reconnected = await reconnect
                await delayed_release
                self.assertGreaterEqual(connect_calls[-1], release_at)
                await reconnected.aclose()
        finally:
            for http_server in http_servers:
                http_server.close()

    async def test_fixed_http_transport_rebinds_after_pool_close(self):
        hold_server = _AcceptedSocketServer()
        http_server = _HoldServer()
        hold_server.start()
        http_server.start()
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", 0))
            local_port = probe.getsockname()[1]
        finally:
            probe.close()

        held_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            held_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            held_socket.bind(("127.0.0.1", local_port))
            held_socket.connect(hold_server.address)
            for _ in range(15):
                transport = FixedPortAsyncHTTPTransport(local_port)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url=f"http://127.0.0.1:{http_server.address[1]}",
                ) as client:
                    response = await client.get("/")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.text, "ok")
        finally:
            held_socket.close()
            hold_server.close()
            http_server.close()


class BrokerHTTPConnectRetryTest(unittest.TestCase):
    def test_tags_exhausted_windows_address_in_use_collision(self):
        self._assert_exhausted_collision_is_tagged(OSError(10048, "address already in use"), winerror=10048)

    def test_tags_exhausted_posix_address_in_use_collision(self):
        self._assert_exhausted_collision_is_tagged(OSError(98, "address already in use"))

    def test_does_not_tag_timeout_or_connection_refused(self):
        for failure in (TimeoutError("timed out"), ConnectionRefusedError(10061, "connection refused")):
            with self.subTest(failure=type(failure).__name__):
                self._assert_non_collision_error_is_unchanged(failure)

    def _assert_exhausted_collision_is_tagged(self, failure, winerror=None):
        from unittest.mock import Mock, patch

        if winerror is not None:
            failure.winerror = winerror
        fake_socket = Mock()
        fake_socket.connect.side_effect = [failure, failure]
        with patch(
            "src.core.broker_http.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 443))],
        ), patch("src.core.broker_http.socket.socket", return_value=fake_socket), patch(
            "src.core.broker_http.time.sleep"
        ), patch("src.core.broker_http.time.monotonic", side_effect=[0.0, 2.5]):
            with self.assertRaises(FixedPortCollisionError) as raised:
                _connect_with_reuseaddr("127.0.0.1", 443, "0.0.0.0", 443, 1.0, [], None)

        self.assertIs(raised.exception.original, failure)

    def _assert_non_collision_error_is_unchanged(self, failure):
        from unittest.mock import Mock, patch

        fake_socket = Mock()
        fake_socket.connect.side_effect = failure
        with patch(
            "src.core.broker_http.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 443))],
        ), patch("src.core.broker_http.socket.socket", return_value=fake_socket):
            with self.assertRaises(type(failure)) as raised:
                _connect_with_reuseaddr("127.0.0.1", 443, "0.0.0.0", 443, 1.0, [], None)

        self.assertIs(raised.exception, failure)

    def test_retries_address_in_use_connect_and_recovers(self):
        from unittest.mock import Mock, call, patch

        busy_one = OSError(10048, "address already in use")
        busy_two = OSError(10048, "address already in use")
        fake_socket = Mock()
        busy_one.winerror = 10048
        busy_two.winerror = 10048
        fake_socket.connect.side_effect = [busy_one, busy_two, None]
        logger = Mock()
        with patch(
            "src.core.broker_http.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 443))],
        ), patch("src.core.broker_http.socket.socket", return_value=fake_socket), patch(
            "src.core.broker_http.time.sleep"
        ) as sleep:
            result = _connect_with_reuseaddr(
                "127.0.0.1", 443, "0.0.0.0", 443, 1.0, [], logger
            )

        self.assertIs(result, fake_socket)
        self.assertEqual(fake_socket.connect.call_count, 3)
        sleep.assert_has_calls([call(0.05), call(0.1)])
        self.assertTrue(
            any("Fixed-port HTTP connect recovered" in call.args[0] for call in logger.warning.call_args_list)
        )

    def test_does_not_retry_non_address_in_use_connect_error(self):
        from unittest.mock import Mock, patch

        failure = OSError(10061, "connection refused")
        fake_socket = Mock()
        fake_socket.connect.side_effect = failure
        with patch(
            "src.core.broker_http.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 443))],
        ), patch("src.core.broker_http.socket.socket", return_value=fake_socket), patch(
            "src.core.broker_http.time.sleep"
        ) as sleep:
            with self.assertRaises(OSError) as raised:
                _connect_with_reuseaddr("127.0.0.1", 443, "0.0.0.0", 443, 1.0, [], None)

        self.assertIs(raised.exception, failure)
        fake_socket.connect.assert_called_once_with(("127.0.0.1", 443))
        sleep.assert_not_called()


class FixedPortDegradedStateTest(unittest.TestCase):
    def tearDown(self):
        clear_fixed_port_degraded_state("account-a")
        clear_fixed_port_degraded_state("account-b")

    def test_entry_populates_required_state_fields(self):
        now = datetime(2026, 8, 25, tzinfo=timezone.utc)

        state = enter_fixed_port_degraded_state("account-a", "rest", now=now)

        self.assertEqual(state.account_id, "account-a")
        self.assertEqual(state.entered_at, now)
        self.assertEqual(state.last_collision_at, now)
        self.assertEqual(state.operation, "rest")
        self.assertEqual(state.next_recovery_probe_at, now)
        self.assertFalse(state.entry_alert_fired)
        self.assertIsNone(state.last_ongoing_status_at)

    def test_concurrent_entry_keeps_one_account_state(self):
        now = datetime(2026, 8, 25, tzinfo=timezone.utc)
        with ThreadPoolExecutor(max_workers=8) as executor:
            states = list(executor.map(lambda _: enter_fixed_port_degraded_state("account-a", "rest", now=now), range(32)))

        state = get_fixed_port_degraded_state("account-a")
        self.assertIsNotNone(state)
        self.assertTrue(all(entry == state for entry in states))
        self.assertEqual(state.entered_at, now)
        self.assertEqual(state.last_collision_at, now)

    def test_clear_removes_state_when_called_directly(self):
        enter_fixed_port_degraded_state("account-a", "rest")

        clear_fixed_port_degraded_state("account-a")

        self.assertIsNone(get_fixed_port_degraded_state("account-a"))

    def test_entry_is_scoped_to_its_account(self):
        first = datetime(2026, 8, 25, tzinfo=timezone.utc)
        second = first + timedelta(seconds=1)
        enter_fixed_port_degraded_state("account-a", "rest", now=first)
        enter_fixed_port_degraded_state("account-b", "token", now=second)

        self.assertEqual(get_fixed_port_degraded_state("account-a").operation, "rest")
        self.assertEqual(get_fixed_port_degraded_state("account-b").operation, "token")


if __name__ == "__main__":
    unittest.main()
