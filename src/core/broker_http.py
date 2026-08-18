"""Broker HTTP transport with worker-scoped source-port binding."""
from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager
from typing import AsyncIterator

import anyio
import httpcore
import httpx

from httpcore._backends.anyio import AnyIOBackend, AnyIOStream
from httpcore._backends.base import SOCKET_OPTION
from httpcore._exceptions import ConnectError, ConnectTimeout, map_exceptions


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
        return AnyIOStream(stream)


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
        async with self.lock:
            if self._client is None:
                transport = FixedPortAsyncHTTPTransport(self.local_port, self.logger)
                self._client = httpx.AsyncClient(timeout=timeout, transport=transport)
            yield self._client

    async def close(self) -> None:
        if self.local_port is None:
            return
        assert self.lock is not None
        async with self.lock:
            if self._client is not None:
                await self._client.aclose()
                self._client = None
