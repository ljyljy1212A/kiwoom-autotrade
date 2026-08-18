"""Broker HTTP transport with worker-scoped source-port binding."""
from __future__ import annotations

import asyncio
import socket
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import AsyncIterator

import anyio
import httpcore
import httpx

from httpcore._backends.anyio import AnyIOBackend, AnyIOStream
from httpcore._backends.base import SOCKET_OPTION
from httpcore._exceptions import ConnectError, ConnectTimeout, map_exceptions


_HTTP_OPERATION_CONTEXT: ContextVar[str] = ContextVar("http_operation", default="unknown")


@contextmanager
def http_operation(label: str):
    token = _HTTP_OPERATION_CONTEXT.set(label)
    try:
        yield
    finally:
        _HTTP_OPERATION_CONTEXT.reset(token)


class _DiagnosticByteStream:
    def __init__(self, stream, logger):
        self._stream = stream
        self._logger = logger

    def __getattr__(self, name):
        return getattr(self._stream, name)

    def _debug(self, operation: str, state: str, started: float, **fields) -> None:
        debug = getattr(self._logger, "debug", None)
        if debug is None:
            return
        elapsed_ms = (time.monotonic() - started) * 1000
        details = " ".join(f"{key}={value}" for key, value in fields.items())
        suffix = f" {details}" if details else ""
        debug(
            f"HTTP I/O diagnostic: client={_HTTP_OPERATION_CONTEXT.get()} "
            f"operation={operation} state={state} elapsed_ms={elapsed_ms:.1f}{suffix}"
        )

    async def send(self, item: bytes) -> None:
        started = time.monotonic()
        self._debug("write", "before", started, bytes=len(item))
        try:
            await self._stream.send(item)
        except BaseException as exc:
            self._debug("write", "after", started, outcome=type(exc).__name__)
            raise
        self._debug("write", "after", started, outcome="success")

    async def receive(self, max_bytes: int = 65536) -> bytes:
        started = time.monotonic()
        self._debug("read", "before", started, max_bytes=max_bytes)
        try:
            data = await self._stream.receive(max_bytes)
        except BaseException as exc:
            self._debug("read", "after", started, outcome=type(exc).__name__)
            raise
        self._debug("read", "after", started, outcome="eof" if data == b"" else "success", bytes=len(data))
        return data

    async def aclose(self) -> None:
        await self._stream.aclose()


@asynccontextmanager
async def _diagnostic_lock(lock: asyncio.Lock, lock_name: str, logger):
    debug = getattr(logger, "debug", None)
    if debug is None:
        async with lock:
            yield
        return
    task = asyncio.current_task()
    task_name = task.get_name() if task is not None else "-"
    timestamp = datetime.now(timezone.utc).isoformat()
    debug(f"Lock diagnostic: lock={lock_name} state=acquiring task={task_name} timestamp={timestamp}")
    async with lock:
        timestamp = datetime.now(timezone.utc).isoformat()
        debug(f"Lock diagnostic: lock={lock_name} state=acquired task={task_name} timestamp={timestamp}")
        try:
            yield
        finally:
            timestamp = datetime.now(timezone.utc).isoformat()
            debug(f"Lock diagnostic: lock={lock_name} state=released task={task_name} timestamp={timestamp}")


def _connect_with_reuseaddr(
    host: str,
    port: int,
    local_address: str | None,
    local_port: int,
    timeout: float | None,
    socket_options: list[SOCKET_OPTION],
    logger=None,
) -> socket.socket:
    last_error: OSError | None = None
    for family, socktype, proto, _, remote_address in socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    ):
        sock = socket.socket(family, socktype, proto)
        phase = "socket_options"
        bind_address = local_address or ("::" if family == socket.AF_INET6 else "0.0.0.0")
        try:
            sock.settimeout(timeout)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            for option in socket_options:
                sock.setsockopt(*option)
            phase = "bind"
            sock.bind((bind_address, local_port))
            phase = "connect"
            sock.connect(remote_address)
            sock.setblocking(False)
            return sock
        except OSError as exc:
            if logger is not None:
                logger.warning(
                    "Fixed-port HTTP socket failure: "
                    f"phase={phase} local={(bind_address, local_port)!r} "
                    f"remote={remote_address!r} exception={type(exc).__name__} "
                    f"errno={getattr(exc, 'errno', None)!r} "
                    f"winerror={getattr(exc, 'winerror', None)!r}: {exc}"
                )
            last_error = exc
            sock.close()
    if last_error is not None:
        raise last_error
    raise OSError(f"Could not resolve {host}:{port}")


class _FixedPortAnyIOBackend(AnyIOBackend):
    def __init__(self, local_port: int, logger=None):
        self.local_port = local_port
        self.logger = logger

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: list[SOCKET_OPTION] | None = None,
    ) -> AnyIOStream:
        if socket_options is None:
            socket_options = []
        exc_map = {
            TimeoutError: ConnectTimeout,
            OSError: ConnectError,
            anyio.BrokenResourceError: ConnectError,
        }
        with map_exceptions(exc_map):
            with anyio.fail_after(timeout):
                raw_socket = await asyncio.to_thread(
                    _connect_with_reuseaddr,
                    host,
                    port,
                    local_address,
                    self.local_port,
                    timeout,
                    socket_options,
                    self.logger,
                )
                stream = await anyio.abc.SocketStream.from_socket(raw_socket)
        return AnyIOStream(_DiagnosticByteStream(stream, self.logger))


class FixedPortAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """Direct HTTP transport that binds every TCP connection to one port."""

    def __init__(self, local_port: int, logger=None):
        ssl_context = httpx.create_ssl_context(verify=True, cert=None, trust_env=True)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=1,
            max_keepalive_connections=1,
            keepalive_expiry=5.0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=_FixedPortAnyIOBackend(local_port, logger),
        )


class BrokerHTTPGate:
    """Serialize one worker's broker HTTP lifecycle and bind its source port."""

    def __init__(self, local_port: int | None, logger=None):
        self.local_port = local_port
        self.logger = logger
        self.lock = asyncio.Lock() if local_port is not None else None
        self._client: httpx.AsyncClient | None = None

    @asynccontextmanager
    async def client(self, timeout: float) -> AsyncIterator[httpx.AsyncClient]:
        if self.local_port is None:
            async with httpx.AsyncClient(timeout=timeout) as client:
                yield client
            return

        assert self.lock is not None
        async with _diagnostic_lock(self.lock, "http_gate.lock", self.logger):
            if self._client is None:
                transport = FixedPortAsyncHTTPTransport(self.local_port, self.logger)
                self._client = httpx.AsyncClient(timeout=timeout, transport=transport)
            yield self._client

    async def close(self) -> None:
        if self.local_port is None:
            return
        assert self.lock is not None
        async with _diagnostic_lock(self.lock, "http_gate.lock", self.logger):
            if self._client is not None:
                await self._client.aclose()
                self._client = None
