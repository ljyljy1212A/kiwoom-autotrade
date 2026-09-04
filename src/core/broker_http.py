"""Broker HTTP transport with worker-scoped source-port binding."""
from __future__ import annotations

import asyncio
import json
import random
import socket
import struct
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator

import anyio
import httpcore
import httpx

from httpcore._backends.anyio import AnyIOBackend, AnyIOStream
from httpcore._backends.base import SOCKET_OPTION
from httpcore._exceptions import ConnectError, ConnectTimeout, map_exceptions

from src.core.runtime_paths import DATA_DIR


_HTTP_OPERATION_CONTEXT: ContextVar[str] = ContextVar("http_operation", default="unknown")
_FIXED_PORT_CLOSE_WAIT_TIMEOUT_SEC = 1.0
# Windows can retain a fixed source-port tuple briefly after a pooled connection
# retires. Nine seconds covers the observed release tail without turning an
# unavailable broker into an unbounded wait.
_FIXED_PORT_CONNECT_RETRY_BUDGET_SEC = 9.0
_FIXED_PORT_HOLDOFF_SEC = 160.0
_FIXED_PORT_RECOVERY_PROBE_INTERVAL_SEC = 90.0
_FIXED_PORT_CONNECT_RETRY_INITIAL_DELAY_SEC = 0.05
_FIXED_PORT_CONNECT_RETRY_MAX_DELAY_SEC = 0.4
_FIXED_PORT_CONNECT_RETRY_JITTER_MAX_SEC = 0.025
_FIXED_PORT_RECONNECT_QUARANTINE_DELAY_SEC = 0.2
_FIXED_PORT_RECONNECT_QUARANTINE_JITTER_MAX_SEC = 0.05


class _CloseCompletionState:
    def __init__(self):
        self.event = asyncio.Event()
        self.event.set()
        self.hook_installed = False
        self.close_started = False
        self.completion_scheduled = False
        self.completion_count = 0

    def begin_close(self) -> bool:
        if self.close_started:
            return False
        self.close_started = True
        self.event.clear()
        return True

    def schedule_completion(self, loop: asyncio.AbstractEventLoop) -> None:
        if self.completion_scheduled:
            return
        self.completion_scheduled = True
        loop.call_soon(self._complete)

    def resolve_fallback(self) -> None:
        self._complete()

    def _complete(self) -> None:
        if self.completion_count:
            return
        self.completion_count = 1
        self.event.set()


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


class _LingerOnCloseByteStream(_DiagnosticByteStream):
    """Use an abortive close when the fixed-port HTTP pool retires a stream."""

    def __init__(self, stream, logger, raw_socket: socket.socket, close_state: _CloseCompletionState):
        super().__init__(stream, logger)
        self._raw_socket = raw_socket
        self._close_state = close_state

    async def aclose(self) -> None:
        if getattr(self._stream, "_closed", False):
            self._close_state.resolve_fallback()
            await super().aclose()
            return
        self._close_state.begin_close()
        self._raw_socket.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_LINGER,
            struct.pack("ii", 1, 0),
        )
        try:
            await super().aclose()
        finally:
            if not self._close_state.hook_installed:
                self._close_state.resolve_fallback()


def _install_close_completion_hook(stream, close_state: _CloseCompletionState) -> None:
    protocol = getattr(stream, "_protocol", None)
    original_connection_lost = getattr(protocol, "connection_lost", None)
    if protocol is None or not callable(original_connection_lost):
        raise RuntimeError("AnyIO stream does not expose a connection_lost callback")
    loop = asyncio.get_running_loop()

    def connection_lost(exc):
        try:
            original_connection_lost(exc)
        finally:
            close_state.schedule_completion(loop)

    protocol.connection_lost = connection_lost
    close_state.hook_installed = True


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


def _is_address_in_use(exc: OSError) -> bool:
    return getattr(exc, "winerror", None) == 10048 or getattr(exc, "errno", None) == 98


class FixedPortCollisionError(OSError):
    """An exhausted fixed-port address-in-use collision."""

    def __init__(self, original: OSError, *, holdoff_active: bool = False):
        super().__init__(*original.args)
        self.original = original
        self.holdoff_active = holdoff_active


class _FixedPortConnectDiagnostics:
    _COUNTER_NAMES = (
        "sequences",
        "first_attempt_successes",
        "retry_sequences",
        "recovered_sequences",
        "exhausted_sequences",
        "non_collision_failures",
        "retry_connect_calls",
    )

    def __init__(self, local_port: int):
        self.local_port = local_port
        self._lock = threading.Lock()
        self._window_start = self._utc_hour_start()
        self._counts = {name: 0 for name in self._COUNTER_NAMES}

    @staticmethod
    def _utc_hour_start() -> datetime:
        now = datetime.now(timezone.utc)
        return now.replace(minute=0, second=0, microsecond=0)

    def _roll_window(self, logger) -> None:
        current_start = self._utc_hour_start()
        if current_start <= self._window_start:
            return
        if logger is not None:
            values = " ".join(f"{name}={self._counts[name]}" for name in self._COUNTER_NAMES)
            logger.info(
                "Fixed-port HTTP connect summary: "
                f"window_start={self._window_start.isoformat()} "
                f"local_port={self.local_port} {values}"
            )
        self._window_start = current_start
        self._counts = {name: 0 for name in self._COUNTER_NAMES}

    def record(self, counter: str, logger) -> None:
        with self._lock:
            self._roll_window(logger)
            self._counts[counter] += 1


@dataclass(frozen=True)
class FixedPortDegradedState:
    account_id: str
    entered_at: datetime
    last_collision_at: datetime
    operation: str
    next_recovery_probe_at: datetime
    entry_alert_fired: bool = False
    last_ongoing_status_at: datetime | None = None
    local_port: int | None = None
    holdoff_until: datetime | None = None


_FIXED_PORT_DEGRADED_STATES: dict[str, FixedPortDegradedState] = {}
_FIXED_PORT_DEGRADED_STATES_LOCK = threading.Lock()


def is_fixed_port_collision_error(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, FixedPortCollisionError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _fixed_port_degraded_state_path(account_id: str) -> Path:
    return DATA_DIR / f"fixed_port_degraded_{account_id}.json"


def _fixed_port_degraded_payload(state: FixedPortDegradedState) -> dict:
    return {
        "account": state.account_id,
        "local_port": state.local_port,
        "entered_at": state.entered_at.isoformat(),
        "last_collision_at": state.last_collision_at.isoformat(),
        "operation": state.operation,
        "next_recovery_probe_at": state.next_recovery_probe_at.isoformat(),
        "holdoff_until": state.holdoff_until.isoformat() if state.holdoff_until else None,
        "entry_alert_fired": state.entry_alert_fired,
        "last_ongoing_status_at": (
            state.last_ongoing_status_at.isoformat() if state.last_ongoing_status_at else None
        ),
    }


def _persist_fixed_port_degraded_state(state: FixedPortDegradedState) -> None:
    path = _fixed_port_degraded_state_path(state.account_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{time.time_ns()}.tmp")
    temporary.write_text(json.dumps(_fixed_port_degraded_payload(state), ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def delete_persisted_fixed_port_degraded_state(account_id: str) -> None:
    _fixed_port_degraded_state_path(account_id).unlink(missing_ok=True)


def _parse_fixed_port_degraded_timestamp(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def restore_fixed_port_degraded_state(
    account_id: str,
    *,
    now: datetime | None = None,
) -> FixedPortDegradedState | None:
    path = _fixed_port_degraded_state_path(account_id)
    if not path.exists():
        return None
    timestamp = now or datetime.now(timezone.utc)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("account") != account_id:
            raise ValueError("account does not match persisted marker")
        operation = payload.get("operation")
        if not isinstance(operation, str) or not operation:
            raise ValueError("operation is missing")
        entry_alert_fired = payload.get("entry_alert_fired")
        if not isinstance(entry_alert_fired, bool):
            raise ValueError("entry_alert_fired must be boolean")
        ongoing_raw = payload.get("last_ongoing_status_at")
        state = FixedPortDegradedState(
            account_id=account_id,
            entered_at=_parse_fixed_port_degraded_timestamp(payload.get("entered_at")),
            last_collision_at=_parse_fixed_port_degraded_timestamp(payload.get("last_collision_at")),
            operation=operation,
            next_recovery_probe_at=_parse_fixed_port_degraded_timestamp(payload.get("next_recovery_probe_at")),
            entry_alert_fired=entry_alert_fired,
            last_ongoing_status_at=(
                _parse_fixed_port_degraded_timestamp(ongoing_raw) if ongoing_raw is not None else None
            ),
            local_port=payload.get("local_port"),
            holdoff_until=(
                _parse_fixed_port_degraded_timestamp(payload["holdoff_until"])
                if payload.get("holdoff_until") is not None else None
            ),
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        state = FixedPortDegradedState(
            account_id=account_id,
            entered_at=timestamp,
            last_collision_at=timestamp,
            operation="persisted-marker-corrupt",
            next_recovery_probe_at=timestamp,
        )
    with _FIXED_PORT_DEGRADED_STATES_LOCK:
        _FIXED_PORT_DEGRADED_STATES[account_id] = state
    return state


def enter_fixed_port_degraded_state(
    account_id: str,
    operation: str,
    *,
    local_port: int | None = None,
    market: str | None = None,
    now: datetime | None = None,
) -> FixedPortDegradedState:
    timestamp = now or datetime.now(timezone.utc)
    holdoff_until = timestamp + timedelta(seconds=_FIXED_PORT_HOLDOFF_SEC)
    with _FIXED_PORT_DEGRADED_STATES_LOCK:
        existing = _FIXED_PORT_DEGRADED_STATES.get(account_id)
        if existing is None:
            state = FixedPortDegradedState(
                account_id=account_id,
                entered_at=timestamp,
                last_collision_at=timestamp,
                operation=operation,
                next_recovery_probe_at=timestamp,
                local_port=local_port,
                holdoff_until=holdoff_until,
            )
        else:
            state = replace(
                existing,
                last_collision_at=max(existing.last_collision_at, timestamp),
                local_port=local_port if local_port is not None else existing.local_port,
                holdoff_until=holdoff_until,
            )
        _FIXED_PORT_DEGRADED_STATES[account_id] = state
        _persist_fixed_port_degraded_state(state)
        return state


def is_fixed_port_holdoff_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, FixedPortCollisionError):
            return bool(getattr(current, "holdoff_active", False))
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def fixed_port_holdoff_active(
    account_id: str,
    local_port: int,
    *,
    now: datetime | None = None,
) -> FixedPortDegradedState | None:
    timestamp = now or datetime.now(timezone.utc)
    state = get_fixed_port_degraded_state(account_id)
    if (
        state is None
        or state.local_port != local_port
        or state.holdoff_until is None
        or state.holdoff_until <= timestamp
    ):
        return None
    return state


def get_fixed_port_degraded_state(account_id: str) -> FixedPortDegradedState | None:
    with _FIXED_PORT_DEGRADED_STATES_LOCK:
        return _FIXED_PORT_DEGRADED_STATES.get(account_id)


def mark_fixed_port_entry_alert_fired(account_id: str) -> FixedPortDegradedState | None:
    with _FIXED_PORT_DEGRADED_STATES_LOCK:
        existing = _FIXED_PORT_DEGRADED_STATES.get(account_id)
        if existing is None or existing.entry_alert_fired:
            return existing
        state = replace(existing, entry_alert_fired=True)
        _FIXED_PORT_DEGRADED_STATES[account_id] = state
        _persist_fixed_port_degraded_state(state)
        return state


def record_fixed_port_ongoing_status(
    account_id: str,
    *,
    now: datetime | None = None,
) -> FixedPortDegradedState | None:
    timestamp = now or datetime.now(timezone.utc)
    with _FIXED_PORT_DEGRADED_STATES_LOCK:
        existing = _FIXED_PORT_DEGRADED_STATES.get(account_id)
        if existing is None:
            return None
        state = replace(existing, last_ongoing_status_at=timestamp)
        _FIXED_PORT_DEGRADED_STATES[account_id] = state
        _persist_fixed_port_degraded_state(state)
        return state


def record_fixed_port_recovery_probe_attempt(
    account_id: str,
    *,
    now: datetime | None = None,
) -> FixedPortDegradedState | None:
    timestamp = now or datetime.now(timezone.utc)
    with _FIXED_PORT_DEGRADED_STATES_LOCK:
        existing = _FIXED_PORT_DEGRADED_STATES.get(account_id)
        if existing is None:
            return None
        state = replace(
            existing,
            next_recovery_probe_at=timestamp + timedelta(seconds=_FIXED_PORT_RECOVERY_PROBE_INTERVAL_SEC),
        )
        _FIXED_PORT_DEGRADED_STATES[account_id] = state
        _persist_fixed_port_degraded_state(state)
        return state


def clear_fixed_port_degraded_state(account_id: str) -> None:
    with _FIXED_PORT_DEGRADED_STATES_LOCK:
        _FIXED_PORT_DEGRADED_STATES.pop(account_id, None)


def _connect_with_reuseaddr(
    host: str,
    port: int,
    local_address: str | None,
    local_port: int,
    timeout: float | None,
    socket_options: list[SOCKET_OPTION],
    logger=None,
    diagnostics: _FixedPortConnectDiagnostics | None = None,
) -> socket.socket:
    if diagnostics is not None:
        diagnostics.record("sequences", logger)
    sequence_had_retry = False
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
            retry_count = 0
            retry_started = None
            retry_delay = _FIXED_PORT_CONNECT_RETRY_INITIAL_DELAY_SEC
            quarantine_applied = False
            while True:
                try:
                    sock.connect(remote_address)
                except OSError as exc:
                    if not _is_address_in_use(exc):
                        raise
                    if retry_started is None:
                        retry_started = time.monotonic()
                        sequence_had_retry = True
                        if diagnostics is not None:
                            diagnostics.record("retry_sequences", logger)
                    elapsed = time.monotonic() - retry_started
                    remaining = _FIXED_PORT_CONNECT_RETRY_BUDGET_SEC - elapsed
                    if remaining <= 0:
                        raise FixedPortCollisionError(exc) from exc
                    retry_count += 1
                    if diagnostics is not None:
                        diagnostics.record("retry_connect_calls", logger)
                    if not quarantine_applied:
                        quarantine_applied = True
                        quarantine_jitter = random.uniform(
                            0.0, _FIXED_PORT_RECONNECT_QUARANTINE_JITTER_MAX_SEC,
                        )
                        quarantine_delay = min(
                            _FIXED_PORT_RECONNECT_QUARANTINE_DELAY_SEC + quarantine_jitter,
                            remaining,
                        )
                        if logger is not None:
                            logger.warning(
                                "Fixed-port reconnect quarantine applied: "
                                "trigger=address_in_use "
                                f"delay_ms={quarantine_delay * 1000:.1f} "
                                f"local={(bind_address, local_port)!r} "
                                f"remote={remote_address!r}"
                            )
                        time.sleep(quarantine_delay)
                        elapsed = time.monotonic() - retry_started
                        remaining = _FIXED_PORT_CONNECT_RETRY_BUDGET_SEC - elapsed
                        if remaining <= 0:
                            raise FixedPortCollisionError(exc) from exc
                    jitter = random.uniform(0.0, _FIXED_PORT_CONNECT_RETRY_JITTER_MAX_SEC)
                    delay = min(retry_delay + jitter, remaining)
                    if logger is not None:
                        logger.warning(
                            "Fixed-port HTTP connect retry: "
                            f"attempt={retry_count} delay_ms={delay * 1000:.1f} "
                            f"jitter_ms={jitter * 1000:.1f} "
                            f"local={(bind_address, local_port)!r} "
                            f"remote={remote_address!r} winerror=10048"
                        )
                    time.sleep(delay)
                    retry_delay = min(retry_delay * 2, _FIXED_PORT_CONNECT_RETRY_MAX_DELAY_SEC)
                else:
                    break
            if retry_count and logger is not None:
                logger.warning(
                    "Fixed-port HTTP connect recovered: "
                    f"retries={retry_count} "
                    f"elapsed_ms={(time.monotonic() - retry_started) * 1000:.1f} "
                    f"local={(bind_address, local_port)!r} "
                    f"remote={remote_address!r}"
                )
            if diagnostics is not None:
                diagnostics.record(
                    "recovered_sequences" if sequence_had_retry else "first_attempt_successes",
                    logger,
                )
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
        if diagnostics is not None:
            diagnostics.record(
                "exhausted_sequences" if isinstance(last_error, FixedPortCollisionError) else "non_collision_failures",
                logger,
            )
        raise last_error
    if diagnostics is not None:
        diagnostics.record("non_collision_failures", logger)
    raise OSError(f"Could not resolve {host}:{port}")


class _FixedPortAnyIOBackend(AnyIOBackend):
    def __init__(self, local_port: int, logger=None, account_id: str | None = None, market: str | None = None):
        self.local_port = local_port
        self.logger = logger
        self.account_id = account_id
        self.market = market
        self._close_state = _CloseCompletionState()
        self._connect_lock = asyncio.Lock()
        self._connect_diagnostics = _FixedPortConnectDiagnostics(local_port)

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
            async with self._connect_lock:
                holdoff = (
                    fixed_port_holdoff_active(self.account_id, self.local_port)
                    if self.account_id is not None else None
                )
                if holdoff is not None:
                    operation = _HTTP_OPERATION_CONTEXT.get()
                    if self.logger is not None:
                        self.logger.warning(
                            "Fixed-port HTTP holdoff skip: "
                            f"account={self.account_id} market={self.market} "
                            f"local_port={self.local_port} operation={operation} "
                            f"holdoff_until={holdoff.holdoff_until.isoformat()} skipped=true"
                        )
                    raise FixedPortCollisionError(
                        OSError(10048, "fixed-port HTTP holdoff active"),
                        holdoff_active=True,
                    )
                try:
                    await asyncio.wait_for(
                        self._close_state.event.wait(),
                        timeout=_FIXED_PORT_CLOSE_WAIT_TIMEOUT_SEC,
                    )
                except asyncio.TimeoutError as exc:
                    raise OSError(
                        "Timed out waiting for the previous fixed-port HTTP socket to close "
                        f"after {_FIXED_PORT_CLOSE_WAIT_TIMEOUT_SEC:.1f}s"
                    ) from exc
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
                        self._connect_diagnostics,
                    )
                    try:
                        stream = await anyio.abc.SocketStream.from_socket(raw_socket)
                        close_state = _CloseCompletionState()
                        _install_close_completion_hook(stream, close_state)
                    except BaseException:
                        raw_socket.close()
                        raise
                self._close_state = close_state
        return AnyIOStream(_LingerOnCloseByteStream(stream, self.logger, raw_socket, close_state))


class FixedPortAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """Direct HTTP transport that binds every TCP connection to one port."""

    def __init__(self, local_port: int, logger=None, account_id: str | None = None, market: str | None = None):
        ssl_context = httpx.create_ssl_context(verify=True, cert=None, trust_env=True)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=1,
            max_keepalive_connections=1,
            keepalive_expiry=30.0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=_FixedPortAnyIOBackend(local_port, logger, account_id, market),
        )


class BrokerHTTPGate:
    """Serialize one worker's broker HTTP lifecycle and bind its source port."""

    def __init__(
        self,
        local_port: int | None,
        logger=None,
        account_id: str | None = None,
        market: str | None = None,
    ):
        self.local_port = local_port
        self.logger = logger
        self.account_id = account_id
        self.market = market
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
                transport = FixedPortAsyncHTTPTransport(self.local_port, self.logger, self.account_id, self.market)
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
