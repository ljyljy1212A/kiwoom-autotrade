"""Broker HTTP transport with worker-scoped source-port binding."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

import anyio
import httpcore
import httpx

from httpcore._backends.anyio import AnyIOBackend, AnyIOStream
from httpcore._backends.base import SOCKET_OPTION
from httpcore._exceptions import ConnectError, ConnectTimeout, map_exceptions


class _FixedPortAnyIOBackend(AnyIOBackend):
    def __init__(self, local_port: int):
        self.local_port = local_port

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
                stream = await anyio.connect_tcp(
                    remote_host=host,
                    remote_port=port,
                    local_host=local_address or "0.0.0.0",
                    local_port=self.local_port,
                )
                for option in socket_options:
                    stream._raw_socket.setsockopt(*option)  # type: ignore[attr-defined]
        return AnyIOStream(stream)


class FixedPortAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """Direct HTTP transport that binds every TCP connection to one port."""

    def __init__(self, local_port: int):
        ssl_context = httpx.create_ssl_context(verify=True, cert=None, trust_env=True)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=1,
            max_keepalive_connections=1,
            keepalive_expiry=5.0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=_FixedPortAnyIOBackend(local_port),
        )


class BrokerHTTPGate:
    """Serialize one worker's broker HTTP lifecycle and bind its source port."""

    def __init__(self, local_port: int | None):
        self.local_port = local_port
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
                transport = FixedPortAsyncHTTPTransport(self.local_port)
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
