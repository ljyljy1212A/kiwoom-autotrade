"""진입점: 계좌별 엔진 기동 + 5분 주기 리포팅 스케줄러 + 텔레그램 리스너.

실행: python -m src.main
환경변수 ACCOUNT_FILTER 가 설정되면 해당 계좌만 구동 (docker-compose.yml 의 sub 컨테이너 참고).
"""
from __future__ import annotations

import asyncio
import argparse
import functools
import os
import json
import socket
import sqlite3
import time
import uuid
from urllib.parse import urlparse
from dataclasses import dataclass, replace
from enum import Enum
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from src.core.broker_http import _connect_with_reuseaddr
from src.core.process_lock import AccountOrderAuthority, ProcessLock
from src.core.runtime_paths import DATA_DIR, LOG_DIR, PROJECT_ROOT
from src.core.account_manager import load_accounts, run_all
from src.core.engine import AccountEngine
from src.core.realtime_feed import PriceFeed
from src.calendar_utils.market_calendar import _FALLBACK_HOURS
from src.strategy.base import PositionState
from src.strategy.infinite_grid import InfiniteGridStrategy
from src.notify.discord_notify import DiscordNotifier
from src.notify.telegram_bot import TelegramController
from src.utils.logger import get_logger

# The project .env is the source of truth.  Override inherited/stale shell
# variables so a dashboard restarted after a credential rotation uses the new
# App Key and Secret Key.
load_dotenv(override=True)

# A dashboard and KR/US workers are independent Windows processes.  Do not
# make a fresh worker contend for a shared Loguru file handle during restart.
# Keep the legacy system log name for an unscoped invocation, but isolate the
# normal market instances into their own system logs.
_MARKET_LOG_SUFFIX = os.environ.get("MARKET_INSTANCE", "").strip().lower()
SYS_LOG = get_logger(
    "system",
    str(LOG_DIR / f"system_{_MARKET_LOG_SUFFIX}.log") if _MARKET_LOG_SUFFIX else str(LOG_DIR / "system.log"),
)
_WORKER_LOCKS: dict[str, ProcessLock] = {}


class EngineState(str, Enum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


@dataclass(frozen=True)
class WorkerIdentity:
    account_id: str
    market: str
    pid: int
    instance_id: str
    started_at: str

    @property
    def log_value(self) -> str:
        return f"pid={self.pid} instance={self.instance_id}"


@dataclass
class _EngineSlot:
    state: EngineState
    task: asyncio.Task | None = None


def _strip_kr_symbol_prefix(symbol: str) -> str:
    """Kiwoom account/balance responses prefix KR cash-equity codes with a
    single leading 'A' (e.g. 'A005930'). Strip only that one character --
    never use str.lstrip('A'), which would also mangle US tickers like
    'AAPL' or 'AMD' that happen to start with A."""
    code = str(symbol).upper()
    if code.startswith("A") and len(code) > 1:
        return code[1:]
    return code


class SymbolEngineRegistry:
    """In-process ownership registry for one account/symbol engine task.

    The asyncio event loop runs these methods without an await, making claim
    and task binding atomic relative to other start triggers in this process.
    A slot is released only by the task's completion callback.
    """

    def __init__(self):
        self._slots: dict[tuple[str, str], _EngineSlot] = {}

    @staticmethod
    def key(account_id: str, symbol: str) -> tuple[str, str]:
        return str(account_id), _strip_kr_symbol_prefix(symbol)

    def claim(self, account_id: str, symbol: str) -> bool:
        key = self.key(account_id, symbol)
        slot = self._slots.get(key)
        if slot is not None and slot.state is not EngineState.STOPPED:
            return False
        self._slots[key] = _EngineSlot(EngineState.STARTING)
        return True

    def bind_task(self, account_id: str, symbol: str, task: asyncio.Task) -> None:
        slot = self._slots.get(self.key(account_id, symbol))
        if slot is None or slot.state is not EngineState.STARTING or slot.task is not None:
            raise RuntimeError(f"Cannot bind unclaimed engine slot: {account_id}/{symbol}")
        slot.task = task

    def mark_running(self, account_id: str, symbol: str, task: asyncio.Task) -> None:
        slot = self._slots.get(self.key(account_id, symbol))
        if slot is None or slot.task is not task or slot.state is not EngineState.STARTING:
            raise RuntimeError(f"Cannot run unclaimed engine slot: {account_id}/{symbol}")
        slot.state = EngineState.RUNNING

    def request_stop(self, account_id: str, symbol: str, task: asyncio.Task) -> bool:
        slot = self._slots.get(self.key(account_id, symbol))
        if slot is None or slot.task is not task or slot.state not in (EngineState.STARTING, EngineState.RUNNING):
            return False
        slot.state = EngineState.STOPPING
        return True

    def release_from_task(self, account_id: str, symbol: str, task: asyncio.Task) -> None:
        """The sole STOPPED transition: registered task completion callback."""
        slot = self._slots.get(self.key(account_id, symbol))
        if slot is not None and slot.task is task:
            slot.state = EngineState.STOPPED
            slot.task = None

    def state(self, account_id: str, symbol: str) -> EngineState | None:
        slot = self._slots.get(self.key(account_id, symbol))
        return slot.state if slot else None


def _worker_pid_path(account_id: str) -> Path:
    return DATA_DIR / f"worker_{account_id}.pid"


def _worker_status_path(account_id: str) -> Path:
    return DATA_DIR / f"worker_{account_id}.status.json"


def _worker_stop_request_path(account_id: str) -> Path:
    """Supervisor-to-worker graceful-stop request; it is not an ownership lock."""
    return DATA_DIR / f"worker_{account_id}.stop.request.json"


def _write_worker_status(identity: WorkerIdentity, state: str) -> None:
    """Atomically publish ownership metadata for dashboard/supervisor reads."""
    status_path = _worker_status_path(identity.account_id)
    payload = {
        "account": identity.account_id, "market": identity.market, "pid": identity.pid,
        "instanceId": identity.instance_id, "startedAt": identity.started_at, "state": state,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    quote_path = DATA_DIR / f"worker_{identity.account_id}.quotes.json"
    try:
        quotes = json.loads(quote_path.read_text(encoding="utf-8"))
        if isinstance(quotes, dict):
            payload["lastEvaluatedQuotes"] = quotes
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    try:
        status_path.parent.mkdir(parents=True, exist_ok=True)
        temp = status_path.with_name(f"{status_path.name}.{uuid.uuid4().hex}.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temp.replace(status_path)
    except Exception as exc:
        SYS_LOG.warning(f"Worker status publication deferred for {identity.account_id}: {exc}")


async def _publish_worker_heartbeat(identity: WorkerIdentity, interval_sec: float = 5.0) -> None:
    """Refresh worker liveness metadata while the account mutex is owned."""
    while True:
        await asyncio.sleep(interval_sec)
        _write_worker_status(identity, "RUNNING")


async def _watch_for_supervisor_stop(identity: WorkerIdentity, interval_sec: float = 0.2) -> None:
    """Return only after the supervisor has requested this exact worker stop."""
    request_path = _worker_stop_request_path(identity.account_id)
    while True:
        try:
            request = json.loads(request_path.read_text(encoding="utf-8"))
            if isinstance(request, dict) and int(request.get("pid", 0)) == identity.pid \
                    and str(request.get("instanceId", "")) == identity.instance_id:
                SYS_LOG.info(f"Graceful stop requested by supervisor for {identity.log_value}")
                return
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        await asyncio.sleep(interval_sec)


def _apply_worker_identity(ctx, identity: WorkerIdentity, symbol: str = "-"):
    logger = ctx.logger.bind(worker_identity=identity.log_value, symbol=symbol)
    ctx.logger = logger
    ctx.client.logger = logger
    if getattr(ctx.client, "token_mgr", None) is not None:
        ctx.client.token_mgr.logger = logger
    ctx.risk_manager.logger = logger
    return ctx


def _worker_lock(account_id: str) -> ProcessLock:
    lock = _WORKER_LOCKS.get(account_id)
    if lock is None:
        lock = ProcessLock(account_id, DATA_DIR)
        _WORKER_LOCKS[account_id] = lock
    return lock


def _acquire_worker_pid(path: Path, account_id: str) -> None:
    """Atomically claim one account worker slot across competing launches."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise RuntimeError(f"Worker launch refused: cannot clear stale PID file {path}: {exc}") from exc
    payload = json.dumps({"pid": os.getpid(), "account": account_id}).encode("utf-8")
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError as exc:
        raise RuntimeError(f"Worker launch refused: {account_id} was claimed concurrently") from exc
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)


async def make_price_feed(ctx):
    """계좌(ctx)별 `PriceFeed`를 만들고 시작한 뒤, AccountEngine에 넘길 콜러블을 반환합니다.

    PRICE_FEED_MODE 환경변수로 동작을 제어합니다 (기본 "auto"):
      - auto: WebSocket 실시간체결 캐시를 우선 사용, 없거나 오래되면(KIWOOM_PRICE_MAX_STALENESS_SEC) REST 폴백
      - ws:   WebSocket 전용 (REST 폴백 없음)
      - rest: REST 폴링 전용 (WebSocket 미사용, 가장 단순/느림)

    ⚠️ src/core/realtime_feed.py 상단 주석 참고: WS URL/프로토콜과 REST 시세 TR ID는
    원본 명세서(kiwoom-rest-api-spec.json, 이 저장소에는 미포함) 대조 검증이 필요합니다.
    """
    mode = os.environ.get("PRICE_FEED_MODE", "auto").lower()
    max_staleness = float(os.environ.get("KIWOOM_PRICE_MAX_STALENESS_SEC", "20"))
    feed = PriceFeed(ctx.client, logger=ctx.logger, mode=mode, max_staleness_sec=max_staleness)
    feed.start()
    ctx.price_feed_obj = feed  # main()에서 종료 시 정리(WS 연결 해제)하기 위해 ctx에 보관
    return feed.get_price


async def build_engine(ctx, telegram: TelegramController, discord: DiscordNotifier) -> AccountEngine:
    price_feed = await make_price_feed(ctx)
    engine = AccountEngine(ctx, telegram, discord, None, price_feed)
    return engine


async def run_account_balance_monitor(ctx, telegram: TelegramController, discord: DiscordNotifier) -> None:
    """Keep the account-wide holdings snapshot fresh without active strategies.

    Trade Settings controls automation, not whether manually placed or mock
    broker purchases appear in the dashboard.  The monitor shares the account
    balance gate with symbol engines, so enabled profiles do not multiply REST
    balance requests.
    """
    monitor = AccountEngine(ctx, telegram, discord, None, None, balance_only=True)
    while True:
        ctx.logger.debug(
            f"Account balance monitor iteration starting (interval={monitor.poll_interval_sec:g}s)"
        )
        try:
            monitor._refresh_runtime_control()
            await monitor.sync_broker_state(force_balance=True)
        except Exception as exc:
            ctx.logger.warning(f"Account balance monitor deferred: {exc}")
        await asyncio.sleep(_balance_monitor_sleep_seconds(monitor))


def _balance_monitor_sleep_seconds(monitor) -> float:
    if monitor.calendar.session_name_now() != "CLOSED":
        return monitor.poll_interval_sec
    return min(180.0, _seconds_until_next_regular_open(monitor.calendar))


def _seconds_until_next_regular_open(calendar) -> float:
    """Return seconds until the next regular open using the existing calendar data."""
    if calendar.calendar is not None:
        now = datetime.now(tz=calendar.calendar.tz)
        schedule = calendar.calendar.schedule(
            start_date=now.date(),
            end_date=now.date() + timedelta(days=7),
        )
        for open_at in schedule["market_open"]:
            if open_at > now:
                return max(0.0, (open_at - now).total_seconds())
        return 60.0

    fallback = _FALLBACK_HOURS.get(calendar.market)
    if fallback is None:
        return 60.0
    open_time, _close_time, timezone_info = fallback
    now = datetime.now(tz=timezone_info)
    for day_offset in range(8):
        candidate_date = now.date() + timedelta(days=day_offset)
        if candidate_date.weekday() >= 5:
            continue
        candidate = datetime.combine(candidate_date, open_time, tzinfo=timezone_info)
        if candidate > now:
            return max(0.0, (candidate - now).total_seconds())
    return 60.0


async def run_quote_health_monitor(ctx, feed: PriceFeed) -> None:
    """Periodically report the age of each subscribed WebSocket quote."""
    while True:
        realtime = feed.realtime
        if realtime is not None:
            for symbol in realtime.subscribed_symbols():
                age = realtime.cache_age_sec(symbol)
                age_text = "missing" if age is None else f"{age:.1f}s"
                ctx.logger.info(f"Quote health: symbol={symbol} cache_age={age_text}")
        await asyncio.sleep(60)


def _symbol_has_unresolved_orders(account_id: str, symbol: str) -> bool:
    """Whether a disabled profile still needs broker-order recovery.

    Execution controls must be allowed to turn off immediately, but disabling a
    profile must not also stop the only engine able to reconcile or cancel an
    already accepted order for that symbol.
    """
    path = DATA_DIR / f"trades_{account_id}.db"
    if not path.exists():
        return False
    try:
        db = sqlite3.connect(path, timeout=0.25)
        try:
            return db.execute(
                "SELECT 1 FROM pending_orders WHERE account_id=? AND symbol=? "
                "AND status IN ('open','awaiting_execution_history') LIMIT 1",
                (account_id, _strip_kr_symbol_prefix(symbol)),
            ).fetchone() is not None
        finally:
            db.close()
    except sqlite3.Error:
        # The profile remains execution-disabled; starting a recovery engine
        # is safer than silently abandoning a potentially live broker order.
        return True


def _enabled_symbol_configs(account_id: str, market: str) -> list[dict]:
    """Load enabled strategies plus disabled profiles with unresolved orders."""
    path = DATA_DIR / f"dashboard_settings_{account_id}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    result = []
    for profile in payload.get("profiles", []) if isinstance(payload, dict) else []:
        config = profile.get("config") if isinstance(profile, dict) else None
        if not isinstance(config, dict):
            continue
        if str(config.get("market", "")).upper() != market or not config.get("symbol"):
            continue
        symbol = _strip_kr_symbol_prefix(config["symbol"])
        needs_recovery = _symbol_has_unresolved_orders(account_id, symbol)
        if profile.get("enabled", True) is False and not needs_recovery:
            continue
        # Recovery tests and diagnostic monitoring may need a real per-symbol
        # engine without granting either order permission. `monitor_only` is
        # deliberately not an execution opt-in; AccountEngine still requires
        # an explicit Auto Buy/Auto Sell control before it can submit anything.
        if not (
            (config.get("auto_buy") or {}).get("enabled")
            or (config.get("auto_sell") or {}).get("enabled")
            or config.get("monitor_only") is True
            or needs_recovery
        ):
            continue
        result.append(config)
    return result


async def run_symbol_engines(ctx, telegram: TelegramController, discord: DiscordNotifier) -> None:
    """Keep one isolated engine/task per enabled symbol on an account."""
    price_feed = await make_price_feed(ctx)
    quote_health_monitor = asyncio.create_task(
        run_quote_health_monitor(ctx, ctx.price_feed_obj),
        name=f"{ctx.account_id}-quote-health-monitor",
    )
    workers: dict[str, asyncio.Task] = {}
    registry = SymbolEngineRegistry()
    balance_monitor: asyncio.Task | None = None

    async def run_registered_symbol(symbol: str, config: dict) -> None:
        task = asyncio.current_task()
        assert task is not None
        registry.mark_running(ctx.account_id, symbol, task)
        # Construction occurs only after the registry has atomically claimed
        # the slot. No watcher/recovery path can construct a second engine for
        # the same account/symbol while this task owns it.
        symbol_ctx = replace(ctx, strategy=InfiniteGridStrategy(config),
                             position=PositionState(symbol=symbol), logger=ctx.logger.bind(symbol=symbol))
        engine = AccountEngine(symbol_ctx, telegram, discord, None,
                               price_feed, control_symbol=symbol)
        await engine.run()

    def release_registered_symbol(symbol: str, task: asyncio.Task) -> None:
        registry.release_from_task(ctx.account_id, symbol, task)
        if workers.get(symbol) is task:
            workers.pop(symbol, None)
        ctx.logger.info(f"Symbol engine task completed: {symbol}; registry=STOPPED")

    try:
        while True:
            configs = _enabled_symbol_configs(ctx.account_id, ctx.client.market)
            wanted = {_strip_kr_symbol_prefix(config["symbol"]): config for config in configs}
            # The monitor exists only for an account with no active strategy.
            # When any symbol engine is running, that engine already owns the
            # fresh account snapshot through the shared balance gate.
            if not wanted and balance_monitor is None:
                balance_monitor = asyncio.create_task(
                    run_account_balance_monitor(ctx, telegram, discord),
                    name=f"{ctx.account_id}-balance-monitor",
                )

                def _log_balance_monitor_done(task: asyncio.Task) -> None:
                    if task.cancelled():
                        ctx.logger.warning("Account balance monitor task finished: cancelled")
                        return

                    exc = task.exception()
                    if exc is None:
                        ctx.logger.info("Account balance monitor task finished: completed")
                    else:
                        ctx.logger.opt(exception=exc).error(
                            f"Account balance monitor task finished: "
                            f"{type(exc).__name__}: {exc}"
                        )

                balance_monitor.add_done_callback(_log_balance_monitor_done)
                ctx.logger.info("Started passive account balance monitor (no enabled strategies)")
            elif wanted and balance_monitor is not None:
                balance_monitor.cancel()
                await asyncio.gather(balance_monitor, return_exceptions=True)
                balance_monitor = None
                ctx.logger.info("Stopped passive account balance monitor (strategy engine active)")
            for symbol, config in wanted.items():
                control_path = DATA_DIR / f"dashboard_control_{ctx.account_id}_{symbol}.json"
                if not control_path.exists():
                    control_path.write_text(json.dumps({
                        "symbol": symbol,
                        "auto_buy": bool((config.get("auto_buy") or {}).get("enabled")),
                        "auto_sell": bool((config.get("auto_sell") or {}).get("enabled")),
                        "config": config,
                    }, ensure_ascii=False), encoding="utf-8")
                if not registry.claim(ctx.account_id, symbol):
                    # The normal configuration scan sees an already-running
                    # symbol every second. Ownership is unchanged; avoid
                    # producing an unbounded debug log stream for that no-op.
                    continue
                task = asyncio.create_task(run_registered_symbol(symbol, config), name=f"{ctx.account_id}-{symbol}")
                registry.bind_task(ctx.account_id, symbol, task)
                task.add_done_callback(lambda done, item=symbol: release_registered_symbol(item, done))
                workers[symbol] = task
                ctx.logger.info(f"Started independent symbol engine: {symbol}")
            for symbol, task in list(workers.items()):
                if symbol not in wanted and not task.done():
                    if registry.request_stop(ctx.account_id, symbol, task):
                        task.cancel()
                        ctx.logger.info(f"Stopping independent symbol engine: {symbol}; registry=STOPPING")
            await asyncio.sleep(1)
    finally:
        quote_health_monitor.cancel()
        for symbol, task in list(workers.items()):
            if registry.request_stop(ctx.account_id, symbol, task):
                task.cancel()
        if balance_monitor is not None:
            balance_monitor.cancel()
            await asyncio.gather(
                *workers.values(), balance_monitor, quote_health_monitor,
                return_exceptions=True,
            )
        else:
            await asyncio.gather(
                *workers.values(), quote_health_monitor, return_exceptions=True,
            )


_MEASUREMENT_COARSE_DELAYS_SEC = (3.0, 5.0, 10.0, 20.0, 40.0, 80.0, 160.0)
_MEASUREMENT_COARSE_CYCLES = 10
_MEASUREMENT_FINE_CYCLES = 30
_MEASUREMENT_CONNECT_TIMEOUT_SEC = 10.0
_MEASUREMENT_INITIAL_CONNECT_MAX_WAIT_SEC = 200.0
_MEASUREMENT_INITIAL_CONNECT_RETRY_INTERVAL_SEC = 5.0


def _measurement_path() -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = PROJECT_ROOT / "diagnostics" / (
        f"fixed_port_release_measurement_us_mock_{timestamp}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_measurement_record(path: Path, payload: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _raw_single_attempt_connect(
    host: str,
    port: int,
    local_port: int,
    timeout: float | None,
):
    """Measurement-only connect: exactly one attempt, no retry-on-10048.

    ``_connect_with_reuseaddr`` in broker_http.py intentionally retries
    address-in-use errors for up to ~2.5s to make production connections
    resilient. That retry loop is correct for production but wrong for
    this measurement: it hides the true instant a probe first succeeds
    or fails, contaminating both the recorded elapsed time and the
    scheduled probe delay. This helper reproduces only the socket-setup
    and bind/connect steps, deliberately without any retry, so each
    probe reflects the real OS-level state at the scheduled instant.
    Used only by the measurement mode; the real broker transport in
    broker_http.py is untouched.
    """
    last_error: OSError | None = None
    for family, socktype, proto, _, remote_address in socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    ):
        sock = socket.socket(family, socktype, proto)
        bind_address = "::" if family == socket.AF_INET6 else "0.0.0.0"
        try:
            sock.settimeout(timeout)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((bind_address, local_port))
            sock.connect(remote_address)
            sock.setblocking(False)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    if last_error is not None:
        raise last_error
    raise OSError(f"Could not resolve {host}:{port}")


async def _measurement_connect(
    loop: asyncio.AbstractEventLoop,
    host: str,
    port: int,
    local_port: int,
):
    """Connect for a scheduled, timed probe: exactly one attempt, no retry.

    Timing precision matters here -- this is the data point being
    recorded -- so any retry-on-10048 would contaminate the result.
    """
    call = functools.partial(
        _raw_single_attempt_connect,
        host,
        port,
        local_port,
        _MEASUREMENT_CONNECT_TIMEOUT_SEC,
    )
    return await loop.run_in_executor(None, call)


async def _measurement_open_initial_connect(
    loop: asyncio.AbstractEventLoop,
    host: str,
    port: int,
    local_port: int,
):
    """Connect used only to open-then-close a socket and establish
    ``close_time`` for a measurement cycle. Its own timing is not part
    of the recorded data, so unlike ``_measurement_connect`` it is safe
    (and necessary for reliability) to retry on a transient 10048 here,
    the same way production connects do via ``_connect_with_reuseaddr``.
    Without this retry, a cycle can crash outright if the fixed port
    has not fully settled yet -- e.g. immediately after this worker
    itself started and bound it.
    """
    call = functools.partial(
        _connect_with_reuseaddr,
        host,
        port,
        None,
        local_port,
        _MEASUREMENT_CONNECT_TIMEOUT_SEC,
        [],
        SYS_LOG,
    )
    return await loop.run_in_executor(None, call)


async def _measurement_close(loop: asyncio.AbstractEventLoop, raw_socket) -> None:
    await loop.run_in_executor(None, raw_socket.close)


def _measurement_error(exc: BaseException) -> str:
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        return f"WinError {winerror}"
    return f"{type(exc).__name__}: {exc}"


async def _assert_measurement_isolated(worker_heartbeat: asyncio.Task) -> None:
    current = asyncio.current_task()
    unexpected = [
        task.get_name()
        for task in asyncio.all_tasks()
        if not task.done() and task not in {current, worker_heartbeat}
    ]
    if unexpected:
        raise RuntimeError(
            "Fixed-port measurement aborted: normal tasks already started: "
            + ", ".join(sorted(unexpected))
        )


async def _run_measurement_cycle(
    *,
    cycle_id: str,
    schedule: tuple[float, ...],
    gate,
    host: str,
    port: int,
    local_port: int,
    path: Path,
    worker_identity: WorkerIdentity,
) -> tuple[bool, float | None]:
    loop = asyncio.get_running_loop()
    initial_connect_deadline = (
        time.monotonic() + _MEASUREMENT_INITIAL_CONNECT_MAX_WAIT_SEC
    )
    try:
        while True:
            try:
                raw_socket = await _measurement_open_initial_connect(
                    loop, host, port, local_port
                )
                break
            except OSError:
                if time.monotonic() >= initial_connect_deadline:
                    raise
                await asyncio.sleep(_MEASUREMENT_INITIAL_CONNECT_RETRY_INTERVAL_SEC)
    except Exception as exc:
        SYS_LOG.opt(exception=exc).error(
            "Fixed-port measurement initial connect failed"
        )
        raise
    await _measurement_close(loop, raw_socket)
    close_time = time.monotonic()
    last_elapsed: float | None = None

    for delay in schedule:
        wait_for = delay - (time.monotonic() - close_time)
        if wait_for > 0:
            await asyncio.sleep(wait_for)

        probe_time = time.monotonic()
        try:
            raw_socket = await _measurement_connect(loop, host, port, local_port)
        except OSError as exc:
            last_elapsed = (probe_time - close_time) * 1000
            _write_measurement_record(
                path,
                {
                    "cycle_id": cycle_id,
                    "monotonic_close_time": close_time,
                    "monotonic_probe_time": probe_time,
                    "elapsed_since_close_ms": last_elapsed,
                    "local_tuple": ["0.0.0.0", local_port],
                    "remote_tuple": [host, port],
                    "success_or_WinError_10048": _measurement_error(exc),
                    "process_id": worker_identity.pid,
                    "worker_instance_id": worker_identity.instance_id,
                },
            )
            continue

        try:
            remote_tuple = list(raw_socket.getpeername()[:2])
        except OSError:
            remote_tuple = [host, port]
        last_elapsed = (probe_time - close_time) * 1000
        _write_measurement_record(
            path,
            {
                "cycle_id": cycle_id,
                "monotonic_close_time": close_time,
                "monotonic_probe_time": probe_time,
                "elapsed_since_close_ms": last_elapsed,
                "local_tuple": ["0.0.0.0", local_port],
                "remote_tuple": remote_tuple,
                "success_or_WinError_10048": "success",
                "process_id": worker_identity.pid,
                "worker_instance_id": worker_identity.instance_id,
            },
        )
        await _measurement_close(loop, raw_socket)
        return True, last_elapsed

    return False, last_elapsed


async def _measure_fixed_port_release(
    ctx,
    worker_identity: WorkerIdentity,
    worker_heartbeat: asyncio.Task,
) -> None:
    await _assert_measurement_isolated(worker_heartbeat)

    gate = ctx.client._http_gate
    if gate.local_port is None or gate.lock is None:
        raise RuntimeError("Fixed-port measurement aborted: us_mock HTTP gate is not fixed-port")

    parsed = urlparse(ctx.client.domain)
    host = parsed.hostname
    port = parsed.port or 443
    if not host:
        raise RuntimeError("Fixed-port measurement aborted: broker domain has no hostname")

    path = _measurement_path()
    coarse_results: list[tuple[bool, float | None]] = []

    async with gate.lock:
        for index in range(_MEASUREMENT_COARSE_CYCLES):
            coarse_results.append(
                await _run_measurement_cycle(
                    cycle_id=f"coarse-{index + 1:02d}",
                    schedule=_MEASUREMENT_COARSE_DELAYS_SEC,
                    gate=gate,
                    host=host,
                    port=port,
                    local_port=gate.local_port,
                    path=path,
                    worker_identity=worker_identity,
                )
            )

        successful = [
            elapsed for success, elapsed in coarse_results
            if success and elapsed is not None
        ]
        failed = [
            elapsed for success, elapsed in coarse_results
            if not success and elapsed is not None
        ]

        if successful and failed:
            lower = max(failed) / 1000
            upper = min(successful) / 1000
        elif successful:
            upper = min(successful) / 1000
            lower = max(2.5, upper - 5.0)
        else:
            lower, upper = 160.0, 180.0

        if upper <= lower:
            lower = max(2.5, upper - 1.0)

        step = (upper - lower) / max(1, _MEASUREMENT_FINE_CYCLES - 1)
        for index in range(_MEASUREMENT_FINE_CYCLES):
            delay = lower + index * step
            await _run_measurement_cycle(
                cycle_id=f"fine-{index + 1:02d}",
                schedule=(delay,),
                gate=gate,
                host=host,
                port=port,
                local_port=gate.local_port,
                path=path,
                worker_identity=worker_identity,
            )

    _write_measurement_record(
        path,
        {
            "event": "measurement_complete",
            "process_id": worker_identity.pid,
            "worker_instance_id": worker_identity.instance_id,
        },
    )


async def main():
    parser = argparse.ArgumentParser(description="Run one isolated Kiwoom market worker")
    parser.add_argument("--market", choices=("KR", "US"), help="market worker to run")
    parser.add_argument("--measure-fixed-port-release", action="store_true")
    args = parser.parse_args()
    account_filter = os.environ.get("ACCOUNT_FILTER")
    market_filter = args.market or os.environ.get("MARKET_INSTANCE")
    requested_accounts = [item.strip() for item in (account_filter or "").split(",") if item.strip()]
    measurement_requested = args.measure_fixed_port_release
    if measurement_requested and (
        requested_accounts != ["us_mock"] or market_filter != "US"
    ):
        SYS_LOG.error(
            "Fixed-port measurement refused: requires account=us_mock market=US; "
            f"received account={account_filter!r} market={market_filter!r}"
        )
        raise SystemExit(2)
    if len(requested_accounts) != 1:
        raise RuntimeError(
            "Worker launch refused: set ACCOUNT_FILTER to exactly one account ID "
            "(for example, us_mock or kr_mock); multi-account launches are not allowed."
        )
    contexts = load_accounts(
        "config/accounts.yaml", account_filter=account_filter, market_filter=market_filter,
    )
    if not contexts:
        SYS_LOG.error("No accounts matched the selected account/market filter; worker will not start")
        return
    worker_market = contexts[0].client.market
    if worker_market == "KR" and len(contexts) != 1:
        # Defense in depth for future account-loader changes.
        raise RuntimeError("KR worker launch refused: account filter did not resolve to exactly one account.")
    worker_account_id = contexts[0].account_id
    worker_lock = _worker_lock(worker_account_id)
    worker_heartbeat: asyncio.Task | None = None
    stop_watcher: asyncio.Task | None = None
    engines_task: asyncio.Task | None = None
    telegram: TelegramController | None = None
    discord: DiscordNotifier | None = None
    worker_identity: WorkerIdentity | None = None
    lock_acquired = False
    try:
        worker_lock.acquire()
        lock_acquired = True
        authority = AccountOrderAuthority(worker_account_id, worker_lock)
        for ctx in contexts:
            ctx.client.bind_order_authority(authority)
        worker_pid_path = _worker_pid_path(worker_account_id)
        _acquire_worker_pid(worker_pid_path, worker_account_id)
        worker_identity = WorkerIdentity(
            account_id=worker_account_id,
            market=worker_market,
            pid=os.getpid(),
            instance_id=uuid.uuid4().hex,
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        for ctx in contexts:
            _apply_worker_identity(ctx, worker_identity)
        # A request for a prior PID/instance must never stop this newly acquired
        # worker.  The supervisor also verifies both values before writing one.
        try:
            _worker_stop_request_path(worker_identity.account_id).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise RuntimeError(f"Worker launch refused: cannot clear stale stop request: {exc}") from exc
        _write_worker_status(worker_identity, "RUNNING")
        worker_heartbeat = asyncio.create_task(
            _publish_worker_heartbeat(worker_identity), name=f"{worker_identity.account_id}-worker-heartbeat"
        )
        if measurement_requested:
            await _measure_fixed_port_release(
                contexts[0], worker_identity, worker_heartbeat
            )
            return

        telegram = TelegramController(
            bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
            chat_id=os.environ["TELEGRAM_CHAT_ID"],
            logger=SYS_LOG,
        )
        for ctx in contexts:
            ctx.client.set_exchange_alert_callback(telegram.notify_error)
        discord = DiscordNotifier(os.environ.get("DISCORD_WEBHOOK_URL"), SYS_LOG)

        stop_watcher = asyncio.create_task(
            _watch_for_supervisor_stop(worker_identity), name=f"{worker_identity.account_id}-supervisor-stop"
        )
        await telegram.start_polling()
        contexts[0].logger.info(f"{len(contexts)} account worker(s) started")

        async def _run_engines():
            await asyncio.gather(*(run_symbol_engines(ctx, telegram, discord) for ctx in contexts))

        engines_task = asyncio.create_task(_run_engines(), name=f"{worker_identity.account_id}-engines")
        done, _ = await asyncio.wait((engines_task, stop_watcher), return_when=asyncio.FIRST_COMPLETED)
        if stop_watcher in done:
            _write_worker_status(worker_identity, "STOPPING")
            engines_task.cancel()
            await asyncio.gather(engines_task, return_exceptions=True)
        else:
            # Surface an unexpected engine orchestration failure normally.
            await engines_task
    finally:
        if engines_task is not None and not engines_task.done():
            engines_task.cancel()
            await asyncio.gather(engines_task, return_exceptions=True)
        if stop_watcher is not None:
            stop_watcher.cancel()
            await asyncio.gather(stop_watcher, return_exceptions=True)
        if worker_heartbeat is not None:
            worker_heartbeat.cancel()
            await asyncio.gather(worker_heartbeat, return_exceptions=True)
        if worker_identity is not None:
            _write_worker_status(worker_identity, "STOPPING")
        if telegram is not None:
            try:
                await telegram.stop()
            except Exception as exc:
                SYS_LOG.warning(f"Telegram shutdown error ignored: {exc}")
        for ctx in contexts:
            feed = getattr(ctx, "price_feed_obj", None)
            if feed is not None:
                try:
                    await feed.stop()
                except Exception as exc:
                    ctx.logger.warning(f"Price feed shutdown error ignored: {exc}")
            try:
                await ctx.client.close()
            except Exception as e:  # 종료 단계 오류가 다른 계좌 정리를 막지 않도록 격리
                ctx.logger.warning(f"클라이언트 종료 중 오류(무시 가능): {e}")
        if worker_identity is not None:
            # The supervisor removes PID metadata only after it has observed this
            # process dead *and* the account lock released.  Keeping it here
            # makes a clean STOPPED status distinguishable from a crash and avoids
            # any window where a live process loses its ownership metadata.
            _write_worker_status(worker_identity, "STOPPED")
        if lock_acquired:
            lock = _WORKER_LOCKS.pop(worker_account_id, None)
            if lock is None:
                lock = worker_lock
            lock.release()


def _install_asyncio_exception_handler() -> None:
    loop = asyncio.get_running_loop()

    def _handle(loop, context):
        task = context.get("task") or context.get("future")
        exc = context.get("exception")
        task_name = task.get_name() if task is not None else None

        SYS_LOG.opt(exception=exc).error(
            "Asyncio unhandled exception: message={!r} task={!r} "
            "exception_type={!r} context_keys={!r}",
            context.get("message"),
            task_name,
            type(exc).__name__ if exc is not None else None,
            sorted(context.keys()),
        )

    loop.set_exception_handler(_handle)


async def _run_main() -> None:
    _install_asyncio_exception_handler()
    await main()


if __name__ == "__main__":
    asyncio.run(_run_main())
