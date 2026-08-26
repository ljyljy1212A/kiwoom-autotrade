"""Per-account trading loop with REST-authoritative order synchronization."""
from __future__ import annotations

import asyncio
import json
import math
import os
import sqlite3
import threading
import time
import uuid
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from src.calendar_utils.market_calendar import MarketCalendar
from src.data.trade_ledger import PendingOrder, TradeLedgerStore
from src.data.order_attempts import unattributed_attempt_ids
from src.core.us_market import (
    extract_us_fx_rate,
    normalize_us_execution_rows,
    normalize_us_holdings,
    us_balance_recognized,
)
from src.core.orphan_cleanup import OrphanStateCleaner
from src.core.control_state import (
    FIXED_PORT_DEGRADED_PAUSE_REASON,
    read_auto_trading_enabled,
    read_control_state,
    write_fixed_port_degraded_event,
)
from src.core.broker_http import (
    clear_fixed_port_degraded_state,
    get_fixed_port_degraded_state,
    mark_fixed_port_entry_alert_fired,
    record_fixed_port_ongoing_status,
    record_fixed_port_recovery_probe_attempt,
)
from src.core.runtime_paths import DATA_DIR
from src.core.symbol_keys import canonical_symbol_key
from src.strategy.base import Action, MarketSnapshot, OrderIntent, PositionState
from src.strategy.infinite_grid import InfiniteGridStrategy
from src.utils.exceptions import KiwoomAPIError, OrderDispatchBlockedError, OrderRejectedError, RetryableError
from src.core.rate_limit_observability import emit_rate_limit_event


_FIXED_PORT_ONGOING_EVENT_INTERVAL_SEC = 15 * 60


class _AccountBalanceGate:
    """One short-lived broker balance snapshot shared by an account's symbols."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.raw_balance: dict | None = None
        self.received_at = 0.0
        self.execution_lock = asyncio.Lock()
        # All symbol engines for one account share this gate.  A buy decision
        # must remain serial from its final duplicate check through pending
        # order recording, otherwise two concurrent engines can both submit
        # the same grid line before either sees the other's pending order.
        self.order_locks: dict[str, asyncio.Lock] = {}
        self.last_execution_request_at = 0.0
        self.cancel_lock = asyncio.Lock()
        self.last_cancel_request_at = 0.0
        self.balance_not_before = 0.0
        self.balance_backoff_sec = 5.0
        self.reconciliation_failure_count = 0
        self.pause_clear_event_id = ""
        self.reconciliation_mode = "off"
        self.reconciliation_failure_threshold = 3
        self.session_failure_ceiling = 3
        self.engines = weakref.WeakSet()
        self.dispatch_clearance_service: DispatchClearanceService | None = None

    @property
    def reconciliation_blocked(self) -> bool:
        return self.reconciliation_failure_count >= self.reconciliation_failure_threshold

    def configure_reconciliation(self, config: dict | None) -> None:
        config = config or {}
        self.reconciliation_mode = str(config.get("mode", "off")).lower()
        self.reconciliation_failure_threshold = max(
            1, int(config.get("consecutive_failure_threshold", 3))
        )
        # Forward-compatible only; manual mode does not use this value.
        self.session_failure_ceiling = max(1, int(config.get("session_failure_ceiling", 3)))


_ACCOUNT_BALANCE_GATES: dict[str, _AccountBalanceGate] = {}
_TRANCHE_BASES_WRITE_LOCK = threading.RLock()
_STARTUP_BACKUP_ACCOUNTS: set[str] = set()


class ReconciliationIncompleteReason(Enum):
    UNRECOGNIZED_BALANCE = "unrecognized balance response"
    BROKER_FILL_CATCHUP = "broker-fill catch-up marker"
    PENDING_QUANTITY_DEFERRAL = "pending-quantity deferral"
    STALE_LIFECYCLE_HOLD = "stale-lifecycle hold"
    UNATTRIBUTED_QUANTITY_PAUSE = "unattributed-quantity pause"
    TRANCHE_REBUILD_AMBIGUOUS = "tranche-rebuild ambiguous"


@dataclass(frozen=True)
class NormalizedBalanceHolding:
    symbol: str
    qty: float
    avg_price: float


@dataclass(frozen=True)
class ManualTrancheAllocation:
    restored_manual_qty: float = 0.0
    adopt_manual_qty: float = 0.0
    unattributed_remainder: float = 0.0


def _manual_tranche_allocation(
    *, qty: float, known_tranche_qty: float, has_step_one: bool,
    lifecycle_open: bool, lifecycle_manual_qty: float,
) -> ManualTrancheAllocation:
    """Purely classify how a broker remainder can be assigned to tranche 1."""
    remainder = qty - known_tranche_qty
    if remainder <= 1e-9:
        return ManualTrancheAllocation()
    if not has_step_one and lifecycle_open and lifecycle_manual_qty > 1e-9:
        restored = min(remainder, lifecycle_manual_qty)
        remainder -= restored
        return ManualTrancheAllocation(
            restored_manual_qty=restored,
            unattributed_remainder=max(0.0, remainder),
        )
    if not has_step_one:
        return ManualTrancheAllocation(adopt_manual_qty=remainder)
    return ManualTrancheAllocation(unattributed_remainder=remainder)


@dataclass(frozen=True)
class ReconciliationClearanceSnapshot:
    account_id: str
    symbol: str
    balance_api_id: str
    balance_fetched_fresh: bool
    balance_from_shared_cache: bool
    balance_recognized: bool
    holding: NormalizedBalanceHolding | None
    balance_received_at: float | None = None
    max_balance_age_sec: float | None = None
    incomplete_reasons: frozenset[ReconciliationIncompleteReason] = frozenset()
    unresolved_order_ids: tuple[str, ...] = ()
    unattributed_collision_order_ids: tuple[str, ...] = ()


def with_unattributed_collision_order_ids(
    snapshot: ReconciliationClearanceSnapshot,
    data_dir: Path = DATA_DIR,
) -> ReconciliationClearanceSnapshot:
    """Integration point for condition 5 until real snapshot construction is wired."""
    return replace(
        snapshot,
        unattributed_collision_order_ids=tuple(unattributed_attempt_ids(snapshot.account_id, data_dir)),
    )


@dataclass(frozen=True)
class ReconciliationClearanceFailure:
    condition: int
    detail: str


@dataclass(frozen=True)
class ReconciliationClearanceResult:
    account_id: str
    symbol: str
    cleared: bool
    failures: tuple[ReconciliationClearanceFailure, ...]


class DispatchClearanceService:
    """Serialize mock fixed-port recovery checks for one account."""

    def __init__(self, account_id: str):
        self.account_id = account_id
        self._lock = asyncio.Lock()
        self._active_symbols: frozenset[str] = frozenset()
        self._cleared_symbols: frozenset[str] = frozenset()
        self._profile: tuple[tuple[str, ...], int] | None = None

    def observe_active_profile(self, running_symbols: tuple[str, ...], profile_version: int) -> None:
        active_symbols = frozenset(running_symbols)
        profile = (tuple(sorted(active_symbols)), profile_version)
        if profile != self._profile:
            self._profile = profile
            self._active_symbols = active_symbols
            self._cleared_symbols = frozenset()

    def _record_clearance_result(
        self, engine: "AccountEngine", symbol: str, result: ReconciliationClearanceResult
    ) -> None:
        normalized = symbol.upper()
        if result.cleared:
            self._cleared_symbols = self._cleared_symbols | {normalized}
            if self._active_symbols and self._active_symbols.issubset(self._cleared_symbols):
                state = get_fixed_port_degraded_state(self.account_id)
                if state is not None:
                    write_fixed_port_degraded_event(
                        self.account_id, "recovered", state.operation, state.entered_at,
                        updated_by="engine", data_dir=engine.data_dir,
                    )
                clear_fixed_port_degraded_state(self.account_id)
            return
        self._cleared_symbols = self._cleared_symbols - {normalized}

    async def check(self, engine: "AccountEngine", symbol: str) -> None:
        if get_fixed_port_degraded_state(self.account_id) is None:
            return
        async with self._lock:
            if get_fixed_port_degraded_state(self.account_id) is None:
                return
            try:
                snapshot = await engine._build_reconciliation_clearance_snapshot(
                    symbol, max_balance_age_sec=1.0,
                )
                result = evaluate_reconciliation_clearance(snapshot)
            except Exception as exc:
                raise OrderDispatchBlockedError(
                    f"reconciliation clearance internal check failed for {self.account_id}/{symbol}: {exc}"
                ) from exc
            self._record_clearance_result(engine, symbol, result)
            if result.cleared:
                return
            details = "; ".join(failure.detail for failure in result.failures)
            raise OrderDispatchBlockedError(
                f"reconciliation clearance blocked {self.account_id}/{symbol}: {details}"
            )

    async def probe_if_due(self, engine: "AccountEngine", symbol: str) -> None:
        state = get_fixed_port_degraded_state(self.account_id)
        if state is None or datetime.now(timezone.utc) < state.next_recovery_probe_at:
            return
        async with self._lock:
            state = get_fixed_port_degraded_state(self.account_id)
            now = datetime.now(timezone.utc)
            if state is None or now < state.next_recovery_probe_at:
                return
            try:
                snapshot = await engine._build_reconciliation_clearance_snapshot(
                    symbol, max_balance_age_sec=1.0,
                )
                result = evaluate_reconciliation_clearance(snapshot)
                self._record_clearance_result(engine, symbol, result)
            except Exception as exc:
                engine.ctx.logger.error(
                    f"reconciliation clearance internal check failed for {self.account_id}/{symbol}: {exc}"
                )
            finally:
                record_fixed_port_recovery_probe_attempt(self.account_id, now=now)


def _clearance_holding_failure(snapshot: ReconciliationClearanceSnapshot) -> str | None:
    if not snapshot.balance_recognized:
        return "condition 2: unrecognized balance response"
    holding = snapshot.holding
    if holding is None:
        return "condition 2: target holding was not normalized"
    if holding.symbol.upper() != snapshot.symbol.upper():
        return "condition 2: normalized holding symbol does not match the target symbol"
    if not math.isfinite(holding.qty) or holding.qty < 0:
        return "condition 2: target holding quantity is unusable"
    if holding.qty > 0 and (not math.isfinite(holding.avg_price) or holding.avg_price <= 0):
        return "condition 2: positive target holding has no usable average price"
    return None


def _clearance_freshness_failure(snapshot: ReconciliationClearanceSnapshot) -> str | None:
    if (
        snapshot.balance_api_id != "ust21070"
        or not snapshot.balance_fetched_fresh
        or snapshot.balance_from_shared_cache
    ):
        return "condition 1: requires a fresh, non-cached ust21070 balance response"
    if snapshot.balance_received_at is None or snapshot.max_balance_age_sec is None:
        return "condition 1: balance receive time and maximum age are required"
    try:
        received_at = float(snapshot.balance_received_at)
        max_age_sec = float(snapshot.max_balance_age_sec)
        age_sec = time.monotonic() - received_at
    except (TypeError, ValueError, OverflowError):
        return "condition 1: balance receive time and maximum age are malformed"
    if not math.isfinite(received_at) or not math.isfinite(max_age_sec) or max_age_sec < 0:
        return "condition 1: balance receive time and maximum age are malformed"
    if age_sec < 0 or age_sec > max_age_sec:
        return "condition 1: balance response is stale"
    return None


def evaluate_reconciliation_clearance(
    snapshot: ReconciliationClearanceSnapshot,
) -> ReconciliationClearanceResult:
    failures: list[ReconciliationClearanceFailure] = []
    freshness_failure = _clearance_freshness_failure(snapshot)
    if freshness_failure:
        failures.append(ReconciliationClearanceFailure(1, freshness_failure))
    holding_failure = _clearance_holding_failure(snapshot)
    if holding_failure:
        failures.append(ReconciliationClearanceFailure(2, holding_failure))
    if snapshot.incomplete_reasons:
        reasons = ", ".join(sorted(reason.value for reason in snapshot.incomplete_reasons))
        failures.append(ReconciliationClearanceFailure(3, f"condition 3: incomplete reconciliation: {reasons}"))
    if snapshot.unresolved_order_ids:
        orders = ", ".join(snapshot.unresolved_order_ids)
        failures.append(
            ReconciliationClearanceFailure(
                4,
                f"condition 4: {len(snapshot.unresolved_order_ids)} pending/recovery order(s) unresolved: {orders}",
            )
        )
    if snapshot.unattributed_collision_order_ids:
        orders = ", ".join(snapshot.unattributed_collision_order_ids)
        failures.append(
            ReconciliationClearanceFailure(
                5,
                f"condition 5: unattributed collision-period order(s) unresolved: {orders}",
            )
        )
    return ReconciliationClearanceResult(
        account_id=snapshot.account_id,
        symbol=snapshot.symbol,
        cleared=not failures,
        failures=tuple(failures),
    )


def _balance_gate(account_id: str) -> _AccountBalanceGate:
    return _ACCOUNT_BALANCE_GATES.setdefault(account_id, _AccountBalanceGate())


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


class AccountEngine:
    def __init__(self, ctx, telegram, discord, report_store, price_feed, poll_interval_sec: int = 5,
                 control_symbol: str | None = None, balance_only: bool = False,
                 dispatch_clearance_service: DispatchClearanceService | None = None):
        self.ctx, self.telegram, self.discord = ctx, telegram, discord
        self.report_store, self.price_feed, self.poll_interval_sec = report_store, price_feed, poll_interval_sec
        self.data_dir = DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.calendar = MarketCalendar(market=ctx.client.market)
        self._buying_paused = False
        self._trading_paused = False
        self._pause_reason = ""
        self._tranche_sell_paused = False
        # Starting the program must be safe: monitoring/synchronization runs by
        # default, but no strategy intent can reach the broker without opt-in.
        self._auto_trading_enabled = os.environ.get("AUTO_TRADING_ENABLED", "false").lower() == "true"
        persisted_auto_trading = read_auto_trading_enabled(ctx.account_id, self.data_dir)
        if persisted_auto_trading is not None:
            self._auto_trading_enabled = persisted_auto_trading
        self._dashboard_auto_buy = False
        self._dashboard_auto_sell = False
        self._dashboard_profile_allowed = False
        self._last_allowlist_warning_symbol = ""
        self._last_execution_unavailable_symbol = ""
        self._last_auto_buy_price: dict[str, float] = {}
        # A confirmed grid BUY changes the active tranche base and the broker
        # balance can lag the execution feed briefly.  Prevent a new BUY until
        # one normal reconciliation window has elapsed; SELLs are never gated.
        self._buy_reentry_after: dict[str, float] = {}
        # The balance endpoint can lag execution history immediately after a
        # confirmed BUY.  Keep the locally confirmed tranche map authoritative
        # until the broker snapshot has caught up to this minimum quantity.
        # While it is behind, all automated intents are held: rebuilding from
        # the stale lower broker quantity would lose a confirmed tranche, and
        # selling against an unverified balance would be unsafe as well.
        self._broker_fill_catchup_qty: dict[str, float] = {}
        self._broker_fill_catchup_warned: set[str] = set()
        # A deliberate paper-order safety block must not cause the strategy to
        # retry and log the same intent on every quote tick.  This is only a
        # local throttle; it never creates pending orders or changes positions.
        self._blocked_order_until: dict[tuple[str, str], float] = {}
        # Confirmed SELL fills wait for the accompanying account balance
        # snapshot/closed-position cleanup before waking the dashboard.
        # Confirmed fills wake the dashboard only after the corresponding
        # broker-balance reconciliation has completed.
        self._pending_dashboard_fills: list[tuple[PendingOrder, dict]] = []
        self._dashboard_symbol = ""
        # Symbols confirmed fully closed during the current runtime are kept
        # blocked even before the dashboard profile-removal write is observed.
        self._closed_symbols_blocked: set[str] = set()
        self._dashboard_config_fingerprint = ""
        self._dashboard_strategy_changed = False
        self._dashboard_control_mtime_ns: int | None = None
        self._control_symbol = self._symbol_key(control_symbol)
        # A dashboard account must continue publishing its broker holdings even
        # when it has no enabled automation profiles.  This mode deliberately
        # stops after writing the account-wide snapshot, so it cannot adopt a
        # default strategy symbol or submit/affect strategy orders.
        self._balance_only = balance_only
        # One account can have several independent symbol strategies, but the
        # broker balance is account-wide. Share its fresh response so startup
        # and normal ticks do not multiply kt00018/ust21070 requests.
        self._balance_gate = _balance_gate(ctx.account_id)
        if dispatch_clearance_service is not None:
            self._balance_gate.dispatch_clearance_service = dispatch_clearance_service
        self._dispatch_clearance_enabled = (
            ctx.client.market == "US" and ctx.client.mode == "mock" and ctx.account_id == "us_mock"
            and os.environ.get("US_MOCK_RECONCILIATION_CLEARANCE_ENABLED", "false").lower() == "true"
        )
        if self._dispatch_clearance_enabled:
            self.ctx.logger.warning("US mock reconciliation dispatch clearance is enabled")
        self._balance_gate.configure_reconciliation(getattr(ctx, "reconciliation_fail_closed", None))
        initial_state = read_control_state(ctx.account_id, self.data_dir) or {}
        initial_event = initial_state.get("pause_clear_event")
        if not isinstance(initial_event, dict):
            legacy_event = initial_state.get("reconciliation_clear_event")
            if isinstance(legacy_event, dict):
                initial_event = {**legacy_event, "reason": "broker_reconciliation_unavailable"}
        if isinstance(initial_event, dict):
            self._balance_gate.pause_clear_event_id = str(initial_event.get("event_id", ""))
        self._balance_gate.engines.add(self)
        # A passive account monitor publishes broker holdings only. It must not
        # open, initialize, or mutate the confirmed-fill ledger.
        self.ledger = None if balance_only else TradeLedgerStore(self.data_dir / f"trades_{ctx.account_id}.db", ctx.account_id)
        self._tranche_bases_path = self.data_dir / f"tranche_bases_{ctx.account_id}.json"
        self._closure_absence_path = self.data_dir / f"closure_absence_{ctx.account_id}.json"
        try:
            raw_absence = json.loads(self._closure_absence_path.read_text(encoding="utf-8"))
            self._closure_absence_confirmations = {
                self._symbol_key(symbol): int(count)
                for symbol, count in raw_absence.items()
                if int(count) > 0
            } if isinstance(raw_absence, dict) else {}
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            self._closure_absence_confirmations: dict[str, int] = {}
        try:
            self._tranche_bases = json.loads(self._tranche_bases_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._tranche_bases = {}
        self._lifecycle_path = self.data_dir / f"symbol_lifecycles_{ctx.account_id}.json"
        try:
            raw_lifecycles = json.loads(self._lifecycle_path.read_text(encoding="utf-8"))
            self._symbol_lifecycles = raw_lifecycles if isinstance(raw_lifecycles, dict) else {}
        except (OSError, json.JSONDecodeError):
            self._symbol_lifecycles: dict[str, dict] = {}
        self._lifecycle_pending_adoption = False
        self._lifecycle_activation_lock = threading.RLock()
        self._manual_lifecycle_adoptions = 0
        self._orphan_cleaner = OrphanStateCleaner(ctx.account_id, data_dir=self.data_dir, logger=ctx.logger, market=ctx.client.market)
        self._symbol_key_migration_complete = False
        self._sync_lock = asyncio.Lock()
        self._sync_task: asyncio.Task | None = None
        self._last_balance_reconciliation = 0.0
        self._last_balance_request_at = 0.0
        self._balance_sync_blocked = False
        self._last_execution_query_at = 0.0
        self._last_cancel_request_at = 0.0
        self._last_fx_request_at = 0.0
        self._fx_rate_krw: float | None = None
        # Match the engine poll by default so manual/HTS trades are reflected
        # promptly. Raise this only if the broker rate limit requires it.
        self.balance_reconcile_sec = float(
            os.environ.get("BALANCE_RECONCILE_SEC", str(max(poll_interval_sec, 10)))
        )
        self.balance_min_interval_sec = float(os.environ.get("KIWOOM_BALANCE_MIN_INTERVAL_SEC", "1.5"))
        self.execution_query_min_interval_sec = float(os.environ.get("KIWOOM_EXECUTION_QUERY_MIN_INTERVAL_SEC", "1.5"))
        self.pending_order_cancel_after_sec = float(
            os.environ.get("PENDING_ORDER_CANCEL_AFTER_SEC", os.environ.get("PENDING_BUY_CANCEL_AFTER_SEC", "180"))
        )
        # A short reconciliation window prevents a second BUY from racing a
        # just-confirmed fill, without holding a valid next-tranche trigger
        # for the previous ten seconds.
        self.buy_reentry_delay_sec = max(0.0, float(os.environ.get("GRID_BUY_REENTRY_DELAY_SEC", "5")))
        feed_obj = getattr(ctx, "price_feed_obj", None)
        if feed_obj and getattr(feed_obj, "realtime", None):
            feed_obj.realtime.add_doorbell_callback(self.request_sync)
            # Register even while monitor-only mode skips quote evaluation.  The
            # account event types ride this subscription as REST-sync doorbells.
            feed_obj.realtime.subscribe(ctx.strategy.symbol)
        if self.ledger is not None:
            self._prepare_lifecycle_scope(ctx.strategy.symbol)

    def _symbol_key(self, symbol: object) -> str:
        return canonical_symbol_key(self.ctx.client.market, symbol)

    def _run_symbol_key_migration(self, broker_holdings: list[dict]) -> None:
        if getattr(self, "_symbol_key_migration_complete", False):
            return
        if not hasattr(self, "_orphan_cleaner") or not hasattr(self, "_lifecycle_path"):
            self._symbol_key_migration_complete = True
            return
        settings_path = self.data_dir / f"dashboard_settings_{self.ctx.account_id}.json"
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            profiles = settings.get("profiles", []) if isinstance(settings, dict) else []
        except (OSError, json.JSONDecodeError):
            profiles = []
        candidates = {item.get("symbol", "") for item in broker_holdings}
        candidates.update(
            (profile.get("config") or {}).get("symbol", "")
            for profile in profiles
            if isinstance(profile, dict)
            and str((profile.get("config") or {}).get("market", "")).upper() == self.ctx.client.market
        )
        manual_review = self._orphan_cleaner.migrate_legacy_keys(candidates)
        try:
            raw_lifecycles = json.loads(self._lifecycle_path.read_text(encoding="utf-8"))
            self._symbol_lifecycles = raw_lifecycles if isinstance(raw_lifecycles, dict) else {}
        except (OSError, json.JSONDecodeError):
            self._symbol_lifecycles = {}
        self._symbol_key_migration_complete = True
        if manual_review:
            self.ctx.logger.error(
                f"Symbol-key migration requires manual review; automation remains blocked for {sorted(manual_review)}"
            )

    def _symbol_key_manual_review(self, symbol: object) -> bool:
        cleaner = getattr(self, "_orphan_cleaner", None)
        if cleaner is None:
            return False
        symbols = cleaner.manual_review_symbols()
        return isinstance(symbols, (set, frozenset, list, tuple)) and self._symbol_key(symbol) in symbols

    async def run(self):
        self._backup_ledger_at_startup()
        self._restore_from_ledger()
        self._refresh_runtime_control()
        # Dashboard controls are an explicit execution authority, independent
        # of the worker-wide environment switch. Read them before reporting
        # startup mode so the log cannot falsely claim submissions are off.
        self._refresh_dashboard_controls()
        if self._auto_trading_enabled:
            mode = "worker-wide Auto Trading enabled"
        elif self._dashboard_auto_buy or self._dashboard_auto_sell:
            mode = "dashboard-controlled trading enabled (per-side controls will be refreshed before each intent)"
        else:
            mode = "monitoring only; no execution control is enabled"
        self.ctx.logger.info(f"Engine started: {self.ctx.display_name} ({self.ctx.strategy.symbol}) — {mode}")
        # Reconcile first, independently of the market-hours gate. This makes
        # the dashboard reflect HTS/manual holdings immediately after restart,
        # including when the regular market is closed.
        try:
            await self.sync_broker_state(force_balance=True)
            self.ctx.logger.info("Startup broker balance synchronization completed")
        except Exception as exc:
            self.ctx.logger.warning(f"Startup broker balance synchronization deferred: {exc}")
        try:
            while True:
                try:
                    await self._tick()
                except Exception as exc:
                    self.ctx.logger.exception(f"Tick failed (isolated): {exc}")
                    await self.telegram.notify_error(f"Tick failed: {exc}")
                finally:
                    self._heartbeat()
                await self._wait_for_next_tick_or_control_change()
        finally:
            feed_obj = getattr(self.ctx, "price_feed_obj", None)
            if feed_obj and getattr(feed_obj, "realtime", None):
                feed_obj.realtime.remove_doorbell_callback(self.request_sync)
                feed_obj.realtime.unsubscribe(self.ctx.strategy.symbol)

    def _backup_ledger_at_startup(self) -> None:
        """Mirror the prototype's per-account pre-run database backup."""
        account_id = self.ctx.account_id
        if account_id in _STARTUP_BACKUP_ACCOUNTS:
            return
        _STARTUP_BACKUP_ACCOUNTS.add(account_id)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        destination = self.data_dir / "backup" / account_id / f"trades_{stamp}.db"
        try:
            backup_path = self.ledger.backup_to(destination)
        except Exception as exc:
            _STARTUP_BACKUP_ACCOUNTS.discard(account_id)
            # A backup failure must not prevent broker reconciliation or leave a
            # newly started engine unavailable.
            self.ctx.logger.warning(f"Startup ledger backup deferred: {exc}")
            return
        self.ctx.logger.info(f"Startup ledger backup created: {backup_path}")

    async def _shared_broker_balance(self, *, max_age_sec: float | None = None) -> tuple[dict, float]:
        """Fetch at most one fresh account balance per shared interval."""
        gate = self._balance_gate
        async with _diagnostic_lock(gate.lock, "balance_gate.lock", self.ctx.logger):
            now = asyncio.get_running_loop().time()
            maximum_age = self.balance_min_interval_sec if max_age_sec is None else max_age_sec
            if gate.raw_balance is not None and now - gate.received_at <= maximum_age:
                return gate.raw_balance, gate.received_at
            raw_balance = await self.ctx.client.get_balance()
            gate.raw_balance = raw_balance
            gate.received_at = asyncio.get_running_loop().time()
            return raw_balance, gate.received_at

    def _reconciliation_incomplete_reasons(
        self, symbol: str, *, balance_recognized: bool, holding: NormalizedBalanceHolding | None,
        qty: float, known_tranche_qty: float = 0.0,
        open_rows: list[tuple[int, float, float]] | None = None,
        unattributed_remainder: float = 0.0, complete_zero_balance: bool = False,
    ) -> frozenset[ReconciliationIncompleteReason]:
        """Classify reconciliation holds without mutating engine or ledger state."""
        symbol_key = self._symbol_key(symbol)
        reasons: set[ReconciliationIncompleteReason] = set()
        if holding is None or not balance_recognized:
            reasons.add(ReconciliationIncompleteReason.UNRECOGNIZED_BALANCE)
            return frozenset(reasons)
        expected_qty = self._broker_fill_catchup_qty.get(symbol_key)
        if expected_qty is not None and qty + 1e-9 < expected_qty:
            reasons.add(ReconciliationIncompleteReason.BROKER_FILL_CATCHUP)
        if self.ledger.pending_orders(symbol) and abs(float(self.ctx.position.qty) - qty) > 1e-9:
            reasons.add(ReconciliationIncompleteReason.PENDING_QUANTITY_DEFERRAL)
        lifecycle = self._symbol_lifecycles.get(symbol_key, {})
        if isinstance(lifecycle, dict) and lifecycle.get("status") == "open":
            minimum = max(0.0, float(lifecycle.get("manual_qty", 0) or 0)) + sum(
                self.ledger.open_tranche_qty(symbol, step) for step in range(2, self.ctx.strategy.max_step + 1)
            )
            if minimum > 0 and qty + 1e-9 < minimum:
                reasons.add(ReconciliationIncompleteReason.STALE_LIFECYCLE_HOLD)
        if complete_zero_balance or unattributed_remainder > 1e-9 or self._pause_reason == "broker_quantity_unattributed":
            reasons.add(ReconciliationIncompleteReason.UNATTRIBUTED_QUANTITY_PAUSE)
        if self._pause_reason == "tranche_rebuild_ambiguous":
            reasons.add(ReconciliationIncompleteReason.TRANCHE_REBUILD_AMBIGUOUS)
        if open_rows is not None and known_tranche_qty > qty + 1e-9:
            if not any(row[0] == 1 for row in open_rows) and qty > 1e-9:
                reasons.add(ReconciliationIncompleteReason.TRANCHE_REBUILD_AMBIGUOUS)
        return frozenset(reasons)

    def _unresolved_reconciliation_order_ids(self, symbol: str) -> tuple[str, ...]:
        return tuple(sorted({
            order.ord_no
            for order in (self.ledger.pending_orders(symbol) + self.ledger.execution_recovery_orders(symbol))
            if order.ord_no
        }))

    def _reconciliation_open_rows(self, symbol: str, avg_price: float) -> list[tuple[int, float, float]]:
        rows = []
        for step in range(1, self.ctx.strategy.max_step + 1):
            open_qty = self.ledger.open_tranche_qty(symbol, step)
            if open_qty > 0:
                buys = [row for row in self.ledger.ledger_rows(symbol)
                        if row.get("type", "").lower() == "buy" and int(row.get("step", 0)) == step]
                total = sum(float(row.get("qty", 0)) for row in buys)
                weighted = sum(float(row.get("qty", 0)) * float(row.get("price", 0)) for row in buys)
                rows.append((step, min(float(open_qty), total), weighted / total if total else avg_price))
        return rows

    async def _build_reconciliation_clearance_snapshot(
        self, symbol: str, *, max_balance_age_sec: float,
    ) -> ReconciliationClearanceSnapshot:
        raw_balance = await self.ctx.client.get_balance()
        balance_received_at = time.monotonic()
        if self.ctx.client.market == "US":
            holdings = normalize_us_holdings(raw_balance)
            recognized = us_balance_recognized(raw_balance)
        else:
            holdings = _all_balance_holdings(self.ctx.client.market, raw_balance)
            recognized = _kr_balance_recognized(raw_balance)
        target = next((item for item in holdings if _same_symbol(self.ctx.client.market, item["symbol"], symbol)), None)
        holding = NormalizedBalanceHolding(symbol, float(target["qty"]), float(target["avgPrice"])) if target else NormalizedBalanceHolding(symbol, 0.0, 0.0)
        known_tranche_qty = sum(qty for qty in self.ctx.strategy.step_qty.values() if qty > 0)
        open_rows = self._reconciliation_open_rows(symbol, holding.avg_price) if known_tranche_qty > holding.qty + 1e-9 else None
        lifecycle = self._symbol_lifecycles.get(self._symbol_key(symbol), {})
        allocation = _manual_tranche_allocation(
            qty=holding.qty, known_tranche_qty=known_tranche_qty,
            has_step_one=bool(self.ctx.strategy.step_qty.get(1)),
            lifecycle_open=isinstance(lifecycle, dict) and lifecycle.get("status") == "open",
            lifecycle_manual_qty=float(lifecycle.get("manual_qty", 0) or 0) if isinstance(lifecycle, dict) else 0.0,
        )
        snapshot = ReconciliationClearanceSnapshot(
            account_id=self.ctx.account_id, symbol=symbol,
            balance_api_id="ust21070" if self.ctx.client.market == "US" else "kt00018",
            balance_fetched_fresh=True, balance_from_shared_cache=False,
            balance_recognized=recognized, holding=holding,
            balance_received_at=balance_received_at,
            max_balance_age_sec=max_balance_age_sec,
            incomplete_reasons=self._reconciliation_incomplete_reasons(
                symbol, balance_recognized=recognized, holding=holding, qty=holding.qty,
                known_tranche_qty=known_tranche_qty, open_rows=open_rows,
                unattributed_remainder=allocation.unattributed_remainder,
                complete_zero_balance=recognized and holding.qty <= 1e-9,
            ),
            unresolved_order_ids=self._unresolved_reconciliation_order_ids(symbol),
        )
        return with_unattributed_collision_order_ids(snapshot, data_dir=self.data_dir)

    def _publish_passive_balance_snapshot(self, broker_holdings: list[dict], balance_recognized: bool) -> None:
        """Publish all broker holdings without changing any strategy state."""
        if not balance_recognized:
            self.ctx.logger.warning("Passive balance monitor received an unrecognized balance response")
            return
        balance_path = self.data_dir / f"balance_{self.ctx.account_id}.json"
        try:
            previous = json.loads(balance_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
        # The passive monitor never infers a tranche basis from broker moving
        # averages. It may, however, publish the canonical values already
        # persisted by an active lifecycle so an old balance snapshot cannot
        # make the dashboard display a stale Line 1 basis.
        try:
            canonical_bases = json.loads(
                (self.data_dir / f"tranche_bases_{self.ctx.account_id}.json").read_text(encoding="utf-8")
            )
            canonical_bases = canonical_bases if isinstance(canonical_bases, dict) else {}
        except (OSError, json.JSONDecodeError):
            canonical_bases = previous.get("trancheBases", {})
        try:
            lifecycles = json.loads(
                (self.data_dir / f"symbol_lifecycles_{self.ctx.account_id}.json").read_text(encoding="utf-8")
            )
            lifecycles = lifecycles if isinstance(lifecycles, dict) else {}
        except (OSError, json.JSONDecodeError):
            lifecycles = {}
        primary = broker_holdings[0] if broker_holdings else {}
        snapshot = {
            "account": self.ctx.account_id,
            # The dashboard needs a non-empty legacy symbol field, but all
            # holdings remain authoritative for the actual display.
            "symbol": str(primary.get("symbol") or previous.get("symbol") or ""),
            "qty": float(primary.get("qty") or 0),
            "avgPrice": float(primary.get("avgPrice") or 0),
            "updatedAt": datetime.now().isoformat(),
            "holdings": broker_holdings,
            "balanceComplete": True,
            "trancheBases": canonical_bases,
            "manualTrancheQty": {
                self._symbol_key(symbol): float(value.get("manual_qty", 0) or 0)
                for symbol, value in lifecycles.items()
                if isinstance(value, dict) and value.get("status") == "open"
            },
            "manualTrancheBases": {
                self._symbol_key(symbol): float(value.get("manual_price", 0) or 0)
                for symbol, value in lifecycles.items()
                if isinstance(value, dict) and value.get("status") == "open"
                and float(value.get("manual_price", 0) or 0) > 0
            },
            "currency": previous.get("currency", self.ctx.currency),
            "reportingCurrency": previous.get("reportingCurrency", self.ctx.reporting_currency),
            "fxRateKrw": previous.get("fxRateKrw"),
        }
        balance_path.parent.mkdir(exist_ok=True)
        tmp_path = balance_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(snapshot), encoding="utf-8")
        tmp_path.replace(balance_path)

    def _has_unresolved_order_for_cleanup(self, symbol: str) -> bool:
        """Read pending attribution state even from a passive account monitor.

        Failure is intentionally treated as unresolved: cleanup must fail closed
        if the ledger cannot be inspected.
        """
        if self.ledger is not None:
            return self.ledger.has_unresolved_orders(symbol)
        path = self.data_dir / f"trades_{self.ctx.account_id}.db"
        if not path.exists():
            return False
        try:
            with sqlite3.connect(path, timeout=0.25) as db:
                return db.execute(
                    "SELECT 1 FROM pending_orders WHERE account_id=? AND symbol=? "
                    "AND status IN ('open','awaiting_execution_history') LIMIT 1",
                    (self.ctx.account_id, symbol),
                ).fetchone() is not None
        except sqlite3.Error:
            return True

    async def _wait_for_next_tick_or_control_change(self) -> None:
        deadline = asyncio.get_running_loop().time() + self.poll_interval_sec
        while True:
            if self._dashboard_control_changed():
                return
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.25, remaining))

    def _dashboard_control_changed(self) -> bool:
        """Return true once for each dashboard control-file update."""
        path = self._dashboard_control_path()
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            mtime_ns = None
        if self._dashboard_control_mtime_ns is None:
            self._dashboard_control_mtime_ns = mtime_ns
            return False
        if mtime_ns != self._dashboard_control_mtime_ns:
            self._dashboard_control_mtime_ns = mtime_ns
            return True
        return False

    def _dashboard_control_path(self) -> Path:
        suffix = f"_{self._control_symbol}" if self._control_symbol else ""
        return self.data_dir / f"dashboard_control_{self.ctx.account_id}{suffix}.json"

    async def _tick(self):
        # Baseline polling makes a wrong/silent WS subscription a latency issue,
        # never a source of silently stale financial state.
        self._refresh_runtime_control()
        self._refresh_dashboard_controls()
        state = get_fixed_port_degraded_state(self.ctx.account_id)
        if state is not None:
            now = datetime.now(timezone.utc)
            if not state.entry_alert_fired:
                write_fixed_port_degraded_event(
                    self.ctx.account_id, "entered", state.operation, state.entered_at,
                    occurred_at=now, updated_by="engine", data_dir=self.data_dir,
                )
                mark_fixed_port_entry_alert_fired(self.ctx.account_id)
            elif now - (state.last_ongoing_status_at or state.entered_at) >= timedelta(
                seconds=_FIXED_PORT_ONGOING_EVENT_INTERVAL_SEC
            ):
                write_fixed_port_degraded_event(
                    self.ctx.account_id, "ongoing", state.operation, state.entered_at,
                    occurred_at=now, updated_by="engine", data_dir=self.data_dir,
                )
                record_fixed_port_ongoing_status(self.ctx.account_id, now=now)
            service = self._balance_gate.dispatch_clearance_service
            if service is None:
                return
            await service.probe_if_due(self, self.ctx.strategy.symbol)
            if get_fixed_port_degraded_state(self.ctx.account_id) is not None:
                return
        # Controls may select a previously HTS-purchased ticker. Reconcile only
        # after loading them so its broker quantity/average are used immediately.
        force_balance = self._dashboard_strategy_changed
        if not await self.sync_broker_state(force_balance=force_balance):
            # A rate-limited or otherwise incomplete reconciliation is never a
            # valid basis for an order decision. Fail closed until a complete
            # broker snapshot succeeds.
            return
        if self._balance_gate.reconciliation_blocked:
            return
        self._dashboard_strategy_changed = False
        symbol_key = self._symbol_key(self.ctx.strategy.symbol)
        if symbol_key in self._broker_fill_catchup_qty:
            # _reconcile_balance has published the latest snapshot but has
            # deliberately not mutated tranche/position state while the broker
            # balance is behind a durable confirmed BUY fill.
            return
        if not self._dashboard_profile_allowed:
            return
        if not (self._auto_trading_enabled or self._dashboard_auto_buy or self._dashboard_auto_sell):
            return
        session = self.calendar.session_name_now()
        if not self.calendar.is_trading_day() or session == "CLOSED":
            return
        quote = await self._safe_get_quote()
        if quote is None:
            return
        price, quote_source, quote_timestamp = quote
        self._record_evaluated_quote(price, quote_source, quote_timestamp)
        trigger = self._next_buy_trigger()
        self.ctx.logger.info(
            f"Strategy quote evaluated: {self.ctx.strategy.symbol} price={price:g} source={quote_source} "
            f"observed_at={datetime.fromtimestamp(quote_timestamp, timezone.utc).isoformat()} "
            f"current_step={self.ctx.position.step} next_buy_trigger="
            f"{trigger if trigger is not None else 'n/a'}"
        )
        snapshot = MarketSnapshot(self.ctx.strategy.symbol, price, datetime.now().isoformat())
        intent = self.ctx.strategy.evaluate(snapshot, self.ctx.position)
        if intent is None:
            return
        if self._trading_paused:
            self.ctx.logger.warning(
                f"Auto condition suppressed by trading pause: {intent.action.value} {intent.symbol}"
            )
            return
        if intent.action == Action.SELL and intent.meta.get("sell_only_step") and self._tranche_sell_paused:
            self.ctx.logger.warning(
                f"Tranche-profit sell deferred for {intent.symbol}: broker quantity is awaiting tranche reconciliation"
            )
            return
        if self._order_block_cooldown_active(intent):
            return
        self.ctx.logger.info(
            f"Auto condition reached: {intent.action.value} {intent.symbol} x{intent.qty} @ {price} ({intent.reason})"
        )
        if intent.action == Action.BUY and (self._buying_paused or not (self._auto_trading_enabled or self._dashboard_auto_buy)):
            return
        if intent.action != Action.BUY and not (self._auto_trading_enabled or self._dashboard_auto_sell):
            return
        await self._handle_intent(intent, price)

    def _refresh_dashboard_controls(self) -> None:
        """Read the local dashboard's explicit per-side execution switches."""
        path = self._dashboard_control_path()
        if not path.exists():
            self._dashboard_auto_buy = self._dashboard_auto_sell = False
            self._dashboard_profile_allowed = False
            return
        try:
            control = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._dashboard_auto_buy = self._dashboard_auto_sell = False
            self._dashboard_profile_allowed = False
            return
        symbol = self._symbol_key(control.get("symbol", ""))
        config = control.get("config")
        if not symbol or not isinstance(config, dict) or self._symbol_key(config.get("symbol", "")) != symbol:
            self._dashboard_auto_buy = self._dashboard_auto_sell = False
            self._dashboard_profile_allowed = False
            return
        control_market = str(config.get("market", "")).strip().upper()
        if control_market != self.ctx.client.market:
            self._dashboard_auto_buy = self._dashboard_auto_sell = False
            self._dashboard_profile_allowed = False
            self.ctx.logger.error(
                f"Automation blocked: {symbol} profile market={control_market or 'missing'} "
                f"does not match {self.ctx.client.market} worker"
            )
            return
        # The persisted Trade Settings List is the sole allow-list for order
        # execution. A stale control file cannot trade a removed/unlisted stock.
        settings_path = self.data_dir / f"dashboard_settings_{self.ctx.account_id}.json"
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            profiles = settings.get("profiles", []) if isinstance(settings, dict) else []
        except (OSError, json.JSONDecodeError):
            profiles = []
        profile = next((p for p in profiles if isinstance(p, dict)
                        and self._symbol_key((p.get("config") or {}).get("symbol", "")) == symbol), None)
        if profile is None:
            self._dashboard_auto_buy = self._dashboard_auto_sell = False
            self._dashboard_profile_allowed = False
            if self._last_allowlist_warning_symbol != symbol:
                self.ctx.logger.warning(f"Automation blocked: {symbol} is not in the Trade Settings List")
                self._last_allowlist_warning_symbol = symbol
            return
        if profile.get("enabled", True) is False:
            self._dashboard_auto_buy = self._dashboard_auto_sell = False
            self._dashboard_profile_allowed = False
            return
        if symbol in self._closed_symbols_blocked:
            # A closed lifecycle removed its old profile/control. Seeing a new
            # valid profile is an explicit operator re-entry request, not a
            # stale tick from the closed cycle. Let broker adoption create a
            # new manual tranche 1 while still blocking the old state itself.
            lifecycle = self._symbol_lifecycles.get(symbol, {})
            if isinstance(lifecycle, dict) and lifecycle.get("status") == "closed":
                self._closed_symbols_blocked.discard(symbol)
            else:
                self._dashboard_auto_buy = self._dashboard_auto_sell = False
                self._dashboard_profile_allowed = False
                return
        self._last_allowlist_warning_symbol = ""
        saved_config = profile.get("config") or {}
        saved_buy = bool((saved_config.get("auto_buy") or {}).get("enabled", False))
        saved_sell = bool((saved_config.get("auto_sell") or {}).get("enabled", False))
        self._dashboard_profile_allowed = True
        # The durable list is authoritative for the strategy values too; a
        # stale dashboard_control file may only supply the current opt-in state.
        config = saved_config
        # The dashboard can operate a previously HTS-purchased holding.  Switch
        # the runtime strategy atomically to that holding's saved configuration;
        # it is never inferred from an arbitrary balance row.
        fingerprint = json.dumps(config, sort_keys=True, separators=(",", ":"))
        lifecycle_state = self._symbol_lifecycles.get(symbol, {})
        lifecycle_is_open = isinstance(lifecycle_state, dict) and lifecycle_state.get("status") == "open"
        lifecycle_is_pending = isinstance(lifecycle_state, dict) and lifecycle_state.get("status") == "pending"
        if (symbol != self._symbol_key(self.ctx.strategy.symbol)
                or fingerprint != self._dashboard_config_fingerprint
                or (not lifecycle_is_open and not lifecycle_is_pending)):
            try:
                strategy = InfiniteGridStrategy(config)
            except (KeyError, TypeError, ValueError) as exc:
                self.ctx.logger.warning(f"Ignoring invalid dashboard strategy configuration: {exc}")
                self._dashboard_auto_buy = self._dashboard_auto_sell = False
                return
            self.ctx.strategy = strategy
            self.ctx.position = PositionState(symbol=strategy.symbol)
            # Dashboard profile activation is an explicit operator action.
            # Clear a stale account-level pause left by an earlier broker
            # mismatch; current balance and tranche safety gates still apply.
            self._trading_paused = False
            # A profile re-enabled after a full close is a fresh manual-first
            # lifecycle. Only an already-open lifecycle may restore fills;
            # otherwise the imminent broker snapshot adopts tranche 1 and old
            # reporting rows cannot blend into it.
            self._prepare_lifecycle_scope(strategy.symbol)
            if self._lifecycle_pending_adoption:
                self._begin_manual_lifecycle_activation(strategy.symbol)
            self._restore_from_ledger()
            self._dashboard_symbol = symbol
            self._dashboard_config_fingerprint = fingerprint
            self._dashboard_strategy_changed = True
            feed_obj = getattr(self.ctx, "price_feed_obj", None)
            if feed_obj and getattr(feed_obj, "realtime", None):
                # Keep the active holding subscribed for low-latency quotes;
                # REST remains authoritative whenever WS is stale or silent.
                feed_obj.realtime.subscribe(strategy.symbol)
            self.ctx.logger.info(f"Dashboard strategy activated for existing holding: {symbol}")
        self._dashboard_auto_buy = bool(control.get("auto_buy")) and saved_buy
        self._dashboard_auto_sell = bool(control.get("auto_sell")) and saved_sell

    def _refresh_runtime_control(self) -> None:
        """Refresh the account-wide auto-trading switch from the control file."""
        persisted = read_auto_trading_enabled(self.ctx.account_id, self.data_dir)
        if persisted is not None and persisted != self._auto_trading_enabled:
            previous = str(self._auto_trading_enabled).lower()
            self._auto_trading_enabled = persisted
            current = str(persisted).lower()
            self.ctx.logger.info(
                f"auto_trading_enabled changed: {previous} -> {current} (source: control file)"
            )

    async def _handle_intent(self, intent: OrderIntent, price: float):
        # Kiwoom accepts only valid domestic price increments.  Quotes can be
        # an arbitrary last-trade value (for example 20,675), so normalize at
        # the single order-intent boundary before any target/risk/submission
        # decision uses the limit price.  BUYs round down; SELLs round up so a
        # profit target cannot be weakened by the tick adjustment.
        if self.ctx.client.market == "KR" and intent.price is not None and float(intent.price) > 0:
            side = "BUY" if intent.action == Action.BUY else "SELL"
            normalized_price = _kr_order_price(float(intent.price), side)
            if abs(normalized_price - float(intent.price)) > 1e-9:
                self.ctx.logger.info(
                    f"KR order price normalized: {intent.symbol} {side} "
                    f"{float(intent.price):g} -> {normalized_price:g}"
                )
                intent.price = normalized_price
            price = normalized_price
        if intent.action == Action.BUY:
            remaining = self._buy_reentry_after.get(intent.symbol, 0.0) - asyncio.get_running_loop().time()
            if remaining > 0:
                self.ctx.logger.info(
                    f"Next grid BUY deferred for {intent.symbol}: {remaining:.1f}s reconciliation cooldown remains"
                )
                return
        if intent.action == Action.SELL and intent.meta.get("sell_only_step"):
            step = int(intent.meta.get("step", 0))
            # The ledger is authoritative for program-confirmed fills. For an
            # HTS-adopted tranche (which has no program ledger rows yet), use
            # the runtime tranche quantity seeded from the broker balance.
            ledger_qty = self.ledger.open_tranche_qty(intent.symbol, step)
            # A later grid line can only be sold from a broker-confirmed fill
            # recorded for that exact line.  Runtime-only fallback is reserved
            # for manually held tranche 1, which has no program ledger row.
            if step > 1 and ledger_qty <= 0:
                self.ctx.logger.error(
                    f"Profit-taking sell blocked: tranche {step} has no confirmed ledger quantity ({intent.symbol})"
                )
                return
            tranche_qty = ledger_qty if ledger_qty > 0 else float(self.ctx.strategy.step_qty.get(step, 0))
            allowed_qty = min(int(tranche_qty), int(self.ctx.position.qty))
            if allowed_qty <= 0:
                self.ctx.logger.warning(f"Profit-taking sell blocked: no remaining quantity for tranche {step} ({intent.symbol})")
                return
            if intent.qty != allowed_qty:
                self.ctx.logger.warning(
                    f"Profit-taking sell quantity capped to tranche {step}: {intent.symbol} {intent.qty} -> {allowed_qty}"
                )
                intent.qty = allowed_qty
            # Recheck the target at the final broker-submission boundary.  A
            # stale quote, corrupted runtime map, or future strategy change
            # must never turn a profit-taking sell into a sale below its own
            # confirmed tranche purchase price/target.
            target = self.ctx.strategy.sell_target_price(step)
            order_price = float(intent.price if intent.price is not None else price)
            if target is None or order_price < target:
                self.ctx.logger.error(
                    f"Profit-taking sell blocked below target: tranche {step} {intent.symbol} "
                    f"price={order_price} target={target}"
                )
                return
        # Serialize the final decision and broker submission per account and
        # symbol.  This is deliberately shared by independently-created
        # engines in the same worker, and complements the process-level worker
        # lock in main.py for accidental duplicate worker launches.
        symbol_key = self._symbol_key(intent.symbol)
        order_lock = self._balance_gate.order_locks.setdefault(symbol_key, asyncio.Lock())
        async with order_lock:
            if intent.action == Action.SELL and intent.meta.get("sell_only_step"):
                step = int(intent.meta.get("step", 0) or 0)
                if step > 0 and self.ledger.has_pending_sell(intent.symbol, step):
                    self.ctx.logger.warning(
                        f"Duplicate profit-taking SELL blocked: {intent.symbol} tranche {step} "
                        "already has an unresolved order"
                    )
                    return
            if intent.action == Action.BUY:
                if self._duplicate_buy_at_price(intent.symbol, price):
                    self.ctx.logger.warning(
                        f"Duplicate Auto Buy blocked at the same price level: {intent.symbol} @ {price}"
                    )
                    return
                # Once a grid line has a current-lifecycle confirmed quantity,
                # it is already occupied.  This catches a second engine/tick
                # whose in-memory strategy map is stale after the first order
                # filled, even when the order prices differ by a KR tick.
                step = int(intent.meta.get("step", 0) or 0)
                if step > 1 and self.ledger.open_tranche_qty(intent.symbol, step) > 0:
                    self.ctx.logger.error(
                        f"Duplicate grid BUY blocked: {intent.symbol} tranche {step} already has "
                        "a confirmed current-lifecycle quantity"
                    )
                    return
            ok, reason = self.ctx.risk_manager.approve(intent, self.ctx.position, price)
            if not ok:
                self.ctx.logger.warning(f"Order blocked by risk manager: {reason}")
                return
            await self._execute_order(intent)

    async def _execute_order(self, intent: OrderIntent):
        if not getattr(self, "_symbol_key_migration_complete", True) or self._symbol_key_manual_review(intent.symbol):
            self.ctx.logger.error(f"Order blocked pending symbol-key migration review: {intent.symbol}")
            return
        if not self._dashboard_profile_allowed:
            self.ctx.logger.warning(f"Order blocked: {intent.symbol} is not allow-listed in Trade Settings")
            return
        # US mock accounts need a second, deliberate opt-in.  This prevents a
        # newly-added US profile from becoming order-capable merely because the
        # global dashboard controls were previously enabled for Korean trading.
        side = "BUY" if intent.action == Action.BUY else "SELL"
        block_key = (side, intent.symbol)
        now = asyncio.get_running_loop().time()
        if self._blocked_order_until.get(block_key, 0.0) > now:
            return
        if self.ctx.client.market == "US" and self.ctx.client.mode == "mock" and \
                os.environ.get("US_PAPER_ORDER_SUBMISSION_ENABLED", "false").lower() != "true":
            cooldown = max(1.0, float(os.environ.get("US_PAPER_BLOCK_COOLDOWN_SEC", "60")))
            self._blocked_order_until[block_key] = now + cooldown
            self.ctx.logger.warning("US paper order blocked: set US_PAPER_ORDER_SUBMISSION_ENABLED=true after validating the mock account")
            return
        self._blocked_order_until.pop(block_key, None)
        if self._dispatch_clearance_enabled:
            service = self._balance_gate.dispatch_clearance_service
            if service is None:
                self.ctx.logger.error("Order blocked: reconciliation clearance service is unavailable")
                return
            try:
                await service.check(self, intent.symbol)
            except OrderDispatchBlockedError as exc:
                self.ctx.logger.error(f"Order blocked: {exc}")
                await self.telegram.notify_error(f"Order blocked: {exc}")
                return
        try:
            result = await self.ctx.client.place_order(side=side, symbol=intent.symbol, qty=intent.qty,
                                                       price=intent.price, order_type=intent.order_type)
        except (OrderRejectedError, RetryableError) as exc:
            self.ctx.logger.error(f"Order failed: {exc}")
            await self.telegram.notify_error(f"Order failed: {exc}")
            return
        if not result.ord_no:
            self.ctx.logger.error("Broker accepted an order without ord_no; no state was changed")
            await self.telegram.notify_error("Broker accepted an order without an order ID; no state was changed")
            return
        if side == "BUY":
            self._last_auto_buy_price[intent.symbol] = float(intent.price or 0)
        # Order acceptance is intentionally the only outcome here.  No position or
        # strategy state changes until get_executed_orders confirms a fill.
        self.ledger.add_pending(PendingOrder(result.ord_no, intent.symbol, side, intent.qty, intent.price,
                                intent.action.value, int(intent.meta.get("step", self.ctx.position.step)), dict(intent.meta)))
        self.ctx.logger.info(f"Accepted order is pending confirmation: {result.ord_no}")
        await self.telegram.notify_order(side, intent.symbol, intent.qty, intent.price, result.ord_no)
        await self.sync_broker_state()

    def _order_block_cooldown_active(self, intent: OrderIntent) -> bool:
        """Return whether a safety-blocked intent is still being throttled."""
        side = "BUY" if intent.action == Action.BUY else "SELL"
        return self._blocked_order_until.get((side, intent.symbol), 0.0) > asyncio.get_running_loop().time()

    def _duplicate_buy_at_price(self, symbol: str, price: float) -> bool:
        """Prevent repeated buys caused by duplicate ticks during a VI burst."""
        if price <= 0:
            return False
        # A grid step cannot advance until the prior submitted BUY has been
        # confirmed through the authoritative execution query. This prevents
        # a fast VI tick burst from buying several tranches off one stale base.
        if self.ledger.has_pending_buy(symbol):
            return True
        tolerance = 1.0 if self.ctx.client.market == "KR" else 1e-6
        previous = self._last_auto_buy_price.get(symbol)
        if previous is not None and abs(previous - price) <= tolerance:
            return True
        return self.ledger.has_pending_buy_at_price(symbol, price, tolerance)

    def request_sync(self) -> None:
        """WebSocket doorbell target: do not parse/accept its payload."""
        # Many broker events can arrive in one burst. One in-flight REST sync is
        # enough because it always asks the broker for the latest full balance.
        if self._sync_task is None or self._sync_task.done():
            self._sync_task = asyncio.create_task(self.sync_broker_state(force_balance=True))

    async def sync_broker_state(self, force_balance: bool = False) -> bool:
        """Apply cumulative REST fills as idempotent deltas, then reconcile balance."""
        async with _diagnostic_lock(self._sync_lock, "AccountEngine._sync_lock", self.ctx.logger):
            self._apply_reconciliation_clear_event()
            # A broker-confirmed fill changes the durable tranche ledger.  The
            # broker balance snapshot and its dashboard event must follow that
            # transition in this same synchronization pass; otherwise the UI
            # can briefly compare a new broker quantity to an old tranche map
            # and report a false "unassigned shares" safety warning.
            confirmed_fill = False
            now = asyncio.get_running_loop().time()
            if now < self._balance_gate.balance_not_before:
                self._balance_sync_blocked = True
                return False
            if self._balance_only:
                # This worker owns no strategy/order state.  In particular, it
                # must not inspect or cancel pending orders belonging to an
                # automated symbol on the same account.
                try:
                    await self._reconcile_balance()
                except RetryableError as exc:
                    self._record_reconciliation_failure(exc)
                    return False
                except KiwoomAPIError as exc:
                    if "429" not in str(exc) and "1700" not in str(exc):
                        raise
                    emit_rate_limit_event(
                        self.ctx.logger,
                        market=self.ctx.client.market,
                        mode=self.ctx.client.mode,
                        account_id=self.ctx.account_id,
                        appkey=self.ctx.client.token_mgr.appkey,
                        api_id=exc.api_id,
                        return_code=exc.return_code,
                        error_text=str(exc),
                        trigger="balance_reconciliation_deferred",
                        cooldown_sec=self._balance_gate.balance_backoff_sec,
                    )
                    self._record_balance_rate_limit()
                    self.ctx.logger.warning("Broker balance rate-limited; reconciliation deferred to the next poll")
                    return False
                completed_at = asyncio.get_running_loop().time()
                self._last_balance_request_at = completed_at
                self._last_balance_reconciliation = completed_at
                self._balance_gate.balance_backoff_sec = 5.0
                self._record_reconciliation_success()
                self._balance_sync_blocked = False
                return True
            if self.ledger.pending_orders(self.ctx.strategy.symbol):
                try:
                    async with self._balance_gate.execution_lock:
                        now = asyncio.get_running_loop().time()
                        wait_for = self.execution_query_min_interval_sec - (now - self._balance_gate.last_execution_request_at)
                        if wait_for > 0:
                            await asyncio.sleep(wait_for)
                        data = await self.ctx.client.get_executed_orders(self.ctx.strategy.symbol)
                        completed_at = asyncio.get_running_loop().time()
                        self._balance_gate.last_execution_request_at = completed_at
                        self._last_execution_query_at = completed_at
                    self._last_execution_unavailable_symbol = ""
                except KiwoomAPIError as exc:
                    completed_at = asyncio.get_running_loop().time()
                    self._balance_gate.last_execution_request_at = completed_at
                    self._last_execution_query_at = completed_at
                    if exc.api_id == "ka10076" and ("429" in str(exc) or "1700" in str(exc)):
                        emit_rate_limit_event(
                            self.ctx.logger,
                            market=self.ctx.client.market,
                            mode=self.ctx.client.mode,
                            account_id=self.ctx.account_id,
                            appkey=self.ctx.client.token_mgr.appkey,
                            api_id=exc.api_id,
                            return_code=exc.return_code,
                            error_text=str(exc),
                            trigger="balance_reconciliation_deferred",
                        )
                        self.ctx.logger.warning("ka10076 rate-limited; fill reconciliation deferred to the next poll")
                        data = None
                    elif exc.api_id == "ust21150" and getattr(exc, "return_code", None) in (7, "7"):
                        # Some US mock symbols (for example SOXL) can be held
                        # and quoted but are not exposed by the execution-history
                        # endpoint. Do not fail the whole tick; balance polling
                        # remains authoritative and the query is retried later.
                        if self._last_execution_unavailable_symbol != self.ctx.strategy.symbol:
                            self.ctx.logger.warning(
                                f"ust21150 execution history unavailable for {self.ctx.strategy.symbol}; fill reconciliation deferred"
                            )
                            self._last_execution_unavailable_symbol = self.ctx.strategy.symbol
                        data = None
                    else:
                        raise
                execution_rows = (normalize_us_execution_rows(data or {})
                                  if self.ctx.client.market == "US" else _executed_rows(data or {}))
                recovery_orders = {order.ord_no: order for order in self.ledger.execution_recovery_orders(
                    self.ctx.strategy.symbol
                )}
                for raw in execution_rows:
                    order = recovery_orders.get(str(raw.get("ord_no", "")))
                    if not order:
                        continue
                    total, price = _number(raw.get("cntr_qty")), _number(raw.get("cntr_pric") or raw.get("cntr_uv"))
                    if total <= order.filled_qty or price <= 0:
                        continue
                    row = self.ledger.record_fill(order, total, price, _filled_at(raw))
                    if row:
                        await self._apply_confirmed_fill(order, row)
                        confirmed_fill = True
                await self._cancel_stale_orders()
            now = asyncio.get_running_loop().time()
            if confirmed_fill or force_balance or now - self._last_balance_reconciliation >= self.balance_reconcile_sec:
                try:
                    await self._reconcile_balance()
                except RetryableError as exc:
                    self._record_reconciliation_failure(exc)
                    return False
                except KiwoomAPIError as exc:
                    if "429" not in str(exc) and "1700" not in str(exc):
                        raise
                    # Quota exhaustion is transient. Keep this symbol's
                    # prior broker-confirmed state and retry on the next
                    # normal reconciliation without an exception traceback.
                    emit_rate_limit_event(
                        self.ctx.logger,
                        market=self.ctx.client.market,
                        mode=self.ctx.client.mode,
                        account_id=self.ctx.account_id,
                        appkey=self.ctx.client.token_mgr.appkey,
                        api_id=exc.api_id,
                        return_code=exc.return_code,
                        error_text=str(exc),
                        trigger="balance_reconciliation_deferred",
                        cooldown_sec=self._balance_gate.balance_backoff_sec,
                    )
                    self.ctx.logger.warning("Broker balance rate-limited; reconciliation deferred to the next poll")
                    self._record_balance_rate_limit()
                    return False
                completed_at = asyncio.get_running_loop().time()
                self._last_balance_request_at = completed_at
                self._last_balance_reconciliation = completed_at
                self._flush_dashboard_fills()
                self._balance_gate.balance_backoff_sec = 5.0
                self._record_reconciliation_success()
            self._balance_sync_blocked = False
            return True

    def _record_reconciliation_failure(self, exc: Exception) -> None:
        gate = self._balance_gate
        if gate.reconciliation_mode != "manual":
            return
        gate.reconciliation_failure_count += 1
        self.ctx.logger.warning(
            "Broker reconciliation unavailable: "
            f"consecutive_cycle_failures={gate.reconciliation_failure_count}; {exc}"
        )
        if gate.reconciliation_failure_count < gate.reconciliation_failure_threshold:
            return
        for engine in list(gate.engines):
            if not engine._pause_reason or engine._pause_reason == "broker_reconciliation_unavailable":
                engine._trading_paused = True
                engine._pause_reason = "broker_reconciliation_unavailable"

    def _record_reconciliation_success(self) -> None:
        gate = self._balance_gate
        if gate.reconciliation_mode == "manual":
            gate.reconciliation_failure_count = 0

    def _apply_reconciliation_clear_event(self) -> None:
        state = read_control_state(self.ctx.account_id, getattr(self, "data_dir", DATA_DIR)) or {}
        event = state.get("pause_clear_event")
        if not isinstance(event, dict):
            legacy_event = state.get("reconciliation_clear_event")
            if isinstance(legacy_event, dict):
                event = {**legacy_event, "reason": "broker_reconciliation_unavailable"}
        event_id = event.get("event_id") if isinstance(event, dict) else ""
        reason = str(event.get("reason", "")) if isinstance(event, dict) else ""
        if not event_id or not reason or event_id == self._balance_gate.pause_clear_event_id:
            return
        self._balance_gate.pause_clear_event_id = event_id
        if reason == FIXED_PORT_DEGRADED_PAUSE_REASON:
            fixed_port_state = get_fixed_port_degraded_state(self.ctx.account_id)
            if fixed_port_state is not None:
                write_fixed_port_degraded_event(
                    self.ctx.account_id, "operator_resolved", fixed_port_state.operation,
                    fixed_port_state.entered_at, updated_by="telegram",
                    data_dir=getattr(self, "data_dir", DATA_DIR),
                )
                clear_fixed_port_degraded_state(self.ctx.account_id)
        for engine in list(self._balance_gate.engines):
            if engine._pause_reason != reason:
                continue
            engine._trading_paused = False
            if reason in {"broker_quantity_unattributed", "tranche_rebuild_ambiguous"}:
                engine._tranche_sell_paused = False
            engine._pause_reason = ""
        self.ctx.logger.info(f"Applied operator clear for {reason}")

    def _record_balance_rate_limit(self) -> None:
        """Apply one shared exponential cooldown to all tasks on this account."""
        now = asyncio.get_running_loop().time()
        gate = self._balance_gate
        gate.balance_not_before = now + gate.balance_backoff_sec
        gate.balance_backoff_sec = min(gate.balance_backoff_sec * 2, 60.0)
        self._balance_sync_blocked = True

    async def _cancel_stale_orders(self) -> None:
        """Cancel one stale unfilled order, regardless of side, per sync cycle.

        SELLs need the same recovery discipline as BUYs. Leaving an accepted
        but unconfirmed sell open allowed repeated ticks to submit additional
        orders for the same tranche before the duplicate guard existed.
        """
        now = datetime.now(timezone.utc)
        # Include execution-history rows: once the broker has stopped exposing
        # an accepted order, an unfilled row must not block a symbol forever.
        # Confirmed fills are never touched because their status is ``filled``.
        for order in self.ledger.execution_recovery_orders(self.ctx.strategy.symbol):
            # A malformed terminal row must be recovered from execution
            # history, but must never be sent to the broker cancellation path.
            if order.status == "filled":
                continue
            try:
                created_at = datetime.fromisoformat(order.created_at)
            except ValueError:
                continue
            if (now - created_at).total_seconds() < self.pending_order_cancel_after_sec:
                continue
            remaining_qty = int(order.requested_qty - order.filled_qty)
            if remaining_qty <= 0:
                continue
            age = (now - created_at).total_seconds()
            # ``awaiting_execution_history`` means cancellation/fill lookup
            # already reached a terminal broker response.  After the bounded
            # grace period, retire it locally as unfilled; this is explicitly
            # auditable and still leaves confirmed-fill ledger rows untouched.
            if order.status == "awaiting_execution_history" and age >= self.pending_order_cancel_after_sec:
                self.ledger.mark_cancelled(order.ord_no)
                self.ctx.logger.warning(
                    f"Force-closed unresolved {order.side} after {age:g}s without confirmed fill: {order.ord_no}"
                )
                return
            # kt10003 is quota constrained.  Submit one cancellation per sync
            # cycle and keep it behind the same minimum request interval used
            # for other broker reconciliation calls.
            try:
                async with self._balance_gate.cancel_lock:
                    loop_now = asyncio.get_running_loop().time()
                    wait_for = self.execution_query_min_interval_sec - (loop_now - self._balance_gate.last_cancel_request_at)
                    if wait_for > 0:
                        await asyncio.sleep(wait_for)
                    await self.ctx.client.cancel_order(order.symbol, order.ord_no, remaining_qty)
                    completed_at = asyncio.get_running_loop().time()
                    self._balance_gate.last_cancel_request_at = completed_at
                    self._last_cancel_request_at = completed_at
            except (OrderRejectedError, RetryableError, KiwoomAPIError) as exc:
                completed_at = asyncio.get_running_loop().time()
                self._balance_gate.last_cancel_request_at = completed_at
                self._last_cancel_request_at = completed_at
                error_text = str(exc)
                if any(code in error_text for code in ("429", "1700", "1701", "1702")):
                    emit_rate_limit_event(
                        self.ctx.logger,
                        market=self.ctx.client.market,
                        mode=self.ctx.client.mode,
                        account_id=self.ctx.account_id,
                        appkey=self.ctx.client.token_mgr.appkey,
                        api_id=getattr(exc, "api_id", None),
                        return_code=getattr(exc, "return_code", None),
                        error_text=error_text,
                        trigger="cancellation_deferred",
                    )
                if "RC4033" in str(exc) or "RC4032" in str(exc):
                    # Neither response proves an unfilled order. Preserve it
                    # for matching against delayed execution history across
                    # future polls and worker restarts, while blocking a
                    # duplicate order for this side/tranche.
                    self.ledger.mark_awaiting_execution_history(order.ord_no)
                    self.ctx.logger.warning(
                        f"{order.side} cancellation is terminal but fill remains unconfirmed; "
                        f"awaiting execution-history recovery: {order.ord_no}"
                    )
                    continue
                self.ctx.logger.warning(f"Timed-out {order.side} cancellation deferred for {order.ord_no}: {exc}")
                # A failed request (especially a quota response) must not make
                # every other legacy pending order retry in the same tick.
                return
            self.ledger.mark_cancelled(order.ord_no)
            self.ctx.logger.info(
                f"Cancelled unfilled {order.side} after {self.pending_order_cancel_after_sec:g}s: {order.ord_no}"
            )
            return

    async def _apply_confirmed_fill(self, order: PendingOrder, row: dict):
        action = Action(order.action)
        meta = dict(order.meta)
        current = self.ledger.get_pending(order.ord_no)
        # A partially filled grid sale still owns its step until its tranche is gone.
        if order.side == "SELL":
            meta["sell_only_step"] = bool(current and current.filled_qty >= current.requested_qty)
        intent = OrderIntent(action, order.symbol, int(row["qty"]), row["price"], meta=meta)
        self._update_position(intent, order.side)
        self.ctx.strategy.on_filled(action, order.step, int(row["qty"]), row["price"])
        if order.side == "BUY":
            symbol = self._symbol_key(order.symbol)
            self._broker_fill_catchup_qty[symbol] = max(
                self._broker_fill_catchup_qty.get(symbol, 0.0),
                float(self.ctx.position.qty),
            )
            self._broker_fill_catchup_warned.discard(symbol)
        if order.side == "BUY" and self.buy_reentry_delay_sec > 0:
            self._buy_reentry_after[order.symbol] = (
                asyncio.get_running_loop().time() + self.buy_reentry_delay_sec
            )
            self.ctx.logger.info(
                f"Confirmed tranche {order.step} BUY; next BUY delayed {self.buy_reentry_delay_sec:g}s for reconciliation"
            )
        # UI refresh is deferred until the next complete broker-balance pass.
        # This applies equally to confirmed BUY and SELL fills.
        self._pending_dashboard_fills.append((order, dict(row)))
        await self.telegram.notify_fill(order.side, order.symbol, row["qty"], row["price"], order.ord_no)
        self.discord.safe_send(f"Confirmed fill: {order.side} {order.symbol} x{row['qty']} @ {row['price']}")

    def _queue_dashboard_fill(self, order: PendingOrder, row: dict) -> None:
        """Schedule an isolated UI notification without blocking the worker loop."""
        try:
            asyncio.create_task(
                self._publish_dashboard_fill(order, dict(row)),
                name=f"dashboard-{order.side.lower()}-fill-{self.ctx.account_id}-{order.ord_no}",
            )
        except Exception as exc:
            # Even task scheduling is UI-only and must never affect an already
            # confirmed broker fill or subsequent trading decisions.
            self.ctx.logger.warning(f"Dashboard {order.side}-fill refresh event deferred: {exc}")

    def _flush_dashboard_fills(self) -> None:
        """Wake the dashboard only after its broker snapshot is current."""
        pending, self._pending_dashboard_fills = self._pending_dashboard_fills, []
        for order, row in pending:
            self._queue_dashboard_fill(order, row)
            self.ctx.logger.info(
                f"Dashboard {order.side}-fill refresh event queued after balance reconciliation: "
                f"{order.symbol} ord_no={order.ord_no}"
            )

    async def _publish_dashboard_fill(self, order: PendingOrder, row: dict) -> None:
        """Run the small filesystem notification outside the trading event loop."""
        try:
            await asyncio.to_thread(self._write_dashboard_fill, order, row)
        except Exception as exc:
            # This is deliberately broader than OSError: payload conversion,
            # serialization, thread scheduling, or filesystem failures are all
            # non-critical dashboard concerns.
            self.ctx.logger.warning(f"Dashboard {order.side}-fill refresh event deferred: {exc}")

    def _write_dashboard_fill(self, order: PendingOrder, row: dict) -> None:
        """Publish one UI-only event after a broker-confirmed fill.

        The dashboard consumes this local notification and performs its normal
        read-only refresh. The event is emitted only after the confirmed ledger
        row and in-memory strategy state have been updated; it never calls the
        dashboard server or changes order processing.
        """
        try:
            side = str(order.side).upper()
            event_id = str(row.get("id") or f"{side[:1]}-{order.ord_no}-{row.get('qty')}")
            payload = {
                "id": event_id,
                "type": f"{side.lower()}-fill",
                "account": self.ctx.account_id,
                "symbol": order.symbol,
                "orderNo": order.ord_no,
                "qty": row.get("qty"),
                "price": row.get("price"),
                "filledAt": row.get("filled_at"),
            }
            path = self.data_dir / f"dashboard_event_{self.ctx.account_id}.json"
            path.parent.mkdir(exist_ok=True)
            # Each rapid fill gets its own temporary filename. The final
            # account event remains newest-event-wins by design, but parallel
            # background writers cannot collide on one .tmp file.
            temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            temporary.replace(path)
        except Exception as exc:
            # Dashboard notification is never allowed to affect a confirmed
            # fill, broker reconciliation, or the next trading decision.
            self.ctx.logger.warning(f"Dashboard {order.side}-fill refresh event deferred: {exc}")

    def _write_lifecycles(self) -> None:
        """Persist lifecycle markers without allowing cache I/O to affect trading."""
        try:
            self._lifecycle_path.parent.mkdir(exist_ok=True)
            temporary = self._lifecycle_path.with_name(f"{self._lifecycle_path.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_text(json.dumps(self._symbol_lifecycles, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self._lifecycle_path)
        except Exception as exc:
            self.ctx.logger.warning(f"Could not persist symbol lifecycle state: {exc}")

    def _prepare_lifecycle_scope(self, symbol: str) -> None:
        """Select only the currently open lifecycle for ledger recovery.

        A missing or closed marker deliberately starts in broker-adoption mode:
        the next complete broker holding becomes the new manual tranche 1 and
        all prior SQLite rows remain reporting history only.
        """
        symbol = self._symbol_key(symbol)
        state = self._symbol_lifecycles.get(symbol)
        started_at = state.get("started_at") if isinstance(state, dict) and state.get("status") == "open" else None
        self.ledger.set_lifecycle_started_at(started_at)
        self._lifecycle_pending_adoption = not bool(started_at)

    def _begin_manual_lifecycle_activation(self, symbol: str) -> None:
        """Create a fresh boundary before adopting an HTS manual holding."""
        symbol = self._symbol_key(symbol)
        if self._symbol_key_manual_review(symbol):
            return
        with self._lifecycle_activation_lock:
            existing = self._symbol_lifecycles.get(symbol)
            if isinstance(existing, dict) and existing.get("status") == "pending" and existing.get("started_at"):
                # Dashboard refreshes and the first balance sync can interleave.
                # Reusing the pending boundary keeps one activation identity and
                # prevents the broker snapshot from being classified as external.
                self.ledger.set_lifecycle_started_at(str(existing["started_at"]))
                self._lifecycle_pending_adoption = True
                return
            now = datetime.now(timezone.utc).isoformat()
            self._symbol_lifecycles[symbol] = {
                "status": "pending", "started_at": now, "activated_at": now,
                "activation_id": uuid.uuid4().hex,
            }
            self.ledger.set_lifecycle_started_at(now)
            self._lifecycle_pending_adoption = True
            self._write_lifecycles()

    def _adopt_manual_lifecycle(self, symbol: str, qty: float, avg_price: float) -> bool:
        """Initialize tranche 1 solely from the activation-time broker snapshot."""
        symbol = self._symbol_key(symbol)
        if self._symbol_key_manual_review(symbol):
            return False
        state = self._symbol_lifecycles.get(symbol, {})
        if isinstance(state, dict) and state.get("status") == "open":
            return False
        started_at = str(state.get("started_at") or datetime.now(timezone.utc).isoformat())
        self._symbol_lifecycles[symbol] = {
            "status": "open", "started_at": started_at,
            "activated_at": state.get("activated_at", started_at),
            "activation_id": state.get("activation_id", uuid.uuid4().hex),
            "manual_qty": float(qty), "manual_price": float(avg_price),
        }
        self.ledger.set_lifecycle_started_at(started_at)
        self._lifecycle_pending_adoption = False
        self.ctx.strategy.step_qty.clear()
        self.ctx.strategy.step_prices.clear()
        self.ctx.strategy.step_qty[1] = int(qty)
        self.ctx.strategy.step_prices[1] = float(avg_price)
        self.ctx.position = PositionState(
            symbol=self.ctx.strategy.symbol, qty=float(qty), avg_price=float(avg_price), step=1
        )
        self._store_tranche_base(symbol, float(avg_price))
        self._write_lifecycles()
        self._manual_lifecycle_adoptions += 1
        self.ctx.logger.info(
            f"Manual tranche 1 adopted at lifecycle activation: {symbol} qty={qty:g}, price={avg_price:g}"
        )
        return True

    def _refresh_open_lifecycle_manual_basis(self, symbol: str, qty: float, price: float) -> None:
        """Persist a manual lot only when no manual lot has been adopted yet.

        A manual Tranche 1 is immutable.  An unexplained broker quantity delta
        must never be treated as evidence that the manual lot increased.
        """
        symbol = self._symbol_key(symbol)
        state = self._symbol_lifecycles.get(symbol)
        if not isinstance(state, dict) or state.get("status") != "open":
            return
        if float(state.get("manual_qty", 0) or 0) > 0 and float(state.get("manual_price", 0) or 0) > 0:
            return
        state["manual_qty"] = float(qty)
        state["manual_price"] = float(price)
        self._write_lifecycles()
        self.ctx.logger.info(
            f"Open lifecycle manual tranche basis reconciled from broker: {symbol} qty={qty:g}, price={price:g}"
        )

    def _close_symbol_lifecycle(self, symbol: str) -> None:
        """Close the active lifecycle before deleting symbol controls/caches."""
        symbol = self._symbol_key(symbol)
        if self._symbol_key_manual_review(symbol):
            return
        state = self._symbol_lifecycles.get(symbol, {})
        self._symbol_lifecycles[symbol] = {
            "status": "closed", "started_at": state.get("started_at"),
            "closed_at": datetime.now(timezone.utc).isoformat(),
        }
        self.ledger.set_lifecycle_started_at(None)
        self._lifecycle_pending_adoption = True
        self._write_lifecycles()

    def _restore_from_ledger(self):
        """Rebuild volatile grid/position state from the durable confirmed-fill ledger."""
        # A new or closed lifecycle must never replay a prior trading cycle.
        if self._lifecycle_pending_adoption:
            return
        repaired = self.ledger.repair_cross_symbol_sell_buy_links(self.ctx.strategy.symbol)
        for item in repaired:
            self.ctx.logger.warning(
                f"Repaired invalid cross-symbol SELL linkage: {item['symbol']} "
                f"order={item['ordNo']} -> {item['buyId']}"
            )
        for row in self.ledger.ledger_rows(self.ctx.strategy.symbol):
            side = row["type"].upper()
            action = Action.BUY if side == "BUY" else Action.SELL
            intent = OrderIntent(action, self.ctx.strategy.symbol, int(row["qty"]), row["price"],
                                 meta={"step": row["step"], "sell_only_step": side == "SELL"})
            self._update_position(intent, side)
            self.ctx.strategy.on_filled(action, row["step"], int(row["qty"]), row["price"])

    def _store_tranche_base(
        self, symbol: str, price: float, *, only_if_absent: bool = False
    ) -> None:
        """Persist a validated tranche-1 basis so a stale value cannot return."""
        symbol = self._symbol_key(symbol)
        price = float(price)
        if price <= 0:
            return
        try:
            with _TRANCHE_BASES_WRITE_LOCK:
                try:
                    latest = json.loads(
                        self._tranche_bases_path.read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError):
                    latest = {}
                if not isinstance(latest, dict):
                    latest = {}
                if only_if_absent and symbol in latest:
                    self._tranche_bases = latest
                    return
                if not only_if_absent and latest.get(symbol) == price:
                    self._tranche_bases = latest
                    return
                latest[symbol] = price
                self._tranche_bases_path.parent.mkdir(exist_ok=True)
                temp_path = self._tranche_bases_path.with_name(
                    f"{self._tranche_bases_path.name}.{uuid.uuid4().hex}.tmp"
                )
                temp_path.write_text(
                    json.dumps(latest, ensure_ascii=False),
                    encoding="utf-8",
                )
                temp_path.replace(self._tranche_bases_path)
                self._tranche_bases = latest
        except OSError as exc:
            # The in-memory recovery result remains safe even if persistence is
            # temporarily unavailable; never let a cache-write issue affect the
            # broker reconciliation path.
            self.ctx.logger.warning(f"Could not persist validated tranche base for {symbol}: {exc}")

    def _remove_tranche_base(self, symbol: str) -> None:
        symbol = self._symbol_key(symbol)
        try:
            with _TRANCHE_BASES_WRITE_LOCK:
                try:
                    latest = json.loads(
                        self._tranche_bases_path.read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError):
                    latest = {}
                if not isinstance(latest, dict):
                    latest = {}
                latest.pop(symbol, None)
                self._tranche_bases_path.parent.mkdir(exist_ok=True)
                temp_path = self._tranche_bases_path.with_name(
                    f"{self._tranche_bases_path.name}.{uuid.uuid4().hex}.tmp"
                )
                temp_path.write_text(
                    json.dumps(latest, ensure_ascii=False),
                    encoding="utf-8",
                )
                temp_path.replace(self._tranche_bases_path)
                self._tranche_bases = latest
        except OSError as exc:
            self.ctx.logger.warning(f"Could not remove tranche base for {symbol}: {exc}")

    def _validated_manual_tranche_base(
        self, symbol: str, broker_qty: float, broker_avg: float,
        confirmed_lots: list[tuple[int, float, float]], manual_qty: float,
    ) -> float:
        """Use a saved manual basis only when it fits the live broker position.

        A simple symbol->price cache has no lifecycle identity.  Therefore a
        base from a prior fully-closed position must never override the current
        broker/confirmed-lot reconstruction merely because the symbol matches.
        """
        symbol = self._symbol_key(symbol)
        lot_qty = sum(float(qty) for _, qty, _ in confirmed_lots)
        lot_cost = sum(float(qty) * float(price) for _, qty, price in confirmed_lots)
        manual_qty = max(0.0, float(manual_qty))
        lifecycle = self._symbol_lifecycles.get(symbol, {})
        lifecycle_price = float(lifecycle.get("manual_price", 0) or 0) if isinstance(lifecycle, dict) else 0.0
        lifecycle_qty = float(lifecycle.get("manual_qty", 0) or 0) if isinstance(lifecycle, dict) else 0.0
        # An open lifecycle records the broker-confirmed HTS manual fill at
        # adoption time. After partial sells a broker's displayed average can
        # no longer be reverse-engineered into individual lots, so it must not
        # overwrite that immutable manual Tranche 1 price on restart.
        if lifecycle.get("status") == "open" and lifecycle_price > 0 and lifecycle_qty > 0 and manual_qty > 0:
            self._store_tranche_base(symbol, lifecycle_price)
            return lifecycle_price
        saved = float(self._tranche_bases.get(symbol, 0) or 0)
        tick_tolerance = float(_kr_tick_size(broker_avg)) if self.ctx.client.market == "KR" else 0.01
        # Broker average prices can be rounded. One price tick is enough to
        # accept a contemporaneous exact manual fill but rejects stale bases
        # such as 226340's 9,120 KRW against the current 7,596 KRW position.
        tolerance = max(tick_tolerance, abs(float(broker_avg)) * 0.0001)
        if saved > 0 and manual_qty > 0 and broker_qty > 0:
            implied_avg = (saved * manual_qty + lot_cost) / broker_qty
            if abs(implied_avg - float(broker_avg)) <= tolerance:
                return saved
            self.ctx.logger.warning(
                f"Discarded stale saved tranche base for {symbol}: saved={saved:g}, "
                f"broker_avg={broker_avg:g}, implied_avg={implied_avg:g}"
            )

        # No trustworthy saved manual basis remains. Infer the residual only
        # from the broker-held quantity and the newest confirmed program lots.
        # Persist this replacement so the rejected value cannot reappear on a
        # later restart. Exact manually adopted bases are persisted separately
        # at adoption time and pass the validation above.
        inferred = float(broker_avg)
        if manual_qty > 0 and broker_qty > 0:
            inferred = (float(broker_qty) * float(broker_avg) - lot_cost) / manual_qty
        if inferred <= 0:
            inferred = float(broker_avg)
        if self.ctx.client.market == "KR":
            tick = _kr_tick_size(inferred)
            inferred = round(inferred / tick) * tick
        else:
            inferred = round(inferred, 4)
        self._store_tranche_base(symbol, inferred)
        return inferred

    def _manual_lifecycle_basis_or_broker_average(self, qty: float, broker_avg: float) -> float:
        """Choose T1's immutable lifecycle basis before a broker moving average.

        A broker may retain a moving-average cost after every automated tranche
        has been sold.  That value is not a new HTS fill and must never reset
        an open lifecycle's manual Line 1 basis.
        """
        symbol = self._symbol_key(self.ctx.strategy.symbol)
        lifecycle = self._symbol_lifecycles.get(symbol, {})
        manual_qty = float(lifecycle.get("manual_qty", 0) or 0) if isinstance(lifecycle, dict) else 0.0
        if (isinstance(lifecycle, dict) and lifecycle.get("status") == "open"
                and manual_qty > 0 and qty > 0):
            return self._validated_manual_tranche_base(
                symbol, qty, broker_avg, [], min(float(qty), manual_qty)
            )
        return float(broker_avg)

    async def _reconcile_balance(self):
        balance_result = await self._shared_broker_balance()
        raw_balance = balance_result[0] if isinstance(balance_result, tuple) else balance_result
        if self.ctx.client.market == "US":
            broker_holdings = normalize_us_holdings(raw_balance)
            balance_recognized = us_balance_recognized(raw_balance)
        else:
            broker_holdings = _all_balance_holdings(self.ctx.client.market, raw_balance)
            balance_recognized = _kr_balance_recognized(raw_balance)
        if balance_recognized:
            self._run_symbol_key_migration(broker_holdings)
            if self._symbol_key_manual_review(self.ctx.strategy.symbol):
                self._trading_paused = True
                self._pause_reason = "symbol_key_manual_review"
                return
        if self._balance_only:
            self._publish_passive_balance_snapshot(broker_holdings, balance_recognized)
            quantities = {self._symbol_key(item.get("symbol", "")): float(item.get("qty", 0) or 0)
                          for item in broker_holdings}
            self._orphan_cleaner.sweep(
                quantities, balance_recognized, self._has_unresolved_order_for_cleanup, apply=True,
            )
            return
        for broker_holding in broker_holdings:
            symbol = broker_holding["symbol"]
            if broker_holding["avgPrice"] > 0:
                # Capture the original broker cost once. Later grid fills alter
                # account average cost, but must never rewrite tranche 1.
                self._store_tranche_base(
                    symbol,
                    broker_holding["avgPrice"],
                    only_if_absent=True,
                )
        if self.ctx.client.market == "US":
            holding_row = next((item for item in broker_holdings
                                if _same_symbol(self.ctx.client.market, item["symbol"], self.ctx.strategy.symbol)), None)
            holding = ((holding_row["qty"], holding_row["avgPrice"]) if holding_row else (0.0, 0.0)) if balance_recognized else None
        else:
            holding = _balance_holding(self.ctx.client.market, raw_balance, self.ctx.strategy.symbol)
        normalized_holding = (
            NormalizedBalanceHolding(self.ctx.strategy.symbol, float(holding[0]), float(holding[1]))
            if holding is not None else None
        )
        incomplete_reasons = self._reconciliation_incomplete_reasons(
            self.ctx.strategy.symbol,
            balance_recognized=balance_recognized,
            holding=normalized_holding,
            qty=normalized_holding.qty if normalized_holding is not None else 0.0,
        )
        if ReconciliationIncompleteReason.UNRECOGNIZED_BALANCE in incomplete_reasons:
            self.ctx.logger.warning(
                "Balance response could not be reconciled; preserving local state "
                f"(top-level fields: {sorted(raw_balance.keys())}; "
                f"holding summary: {_holding_summary(raw_balance)})"
            )
            return
        qty, avg_price = holding
        # The normal workflow is manual HTS tranche 1, then immediate strategy
        # enablement. At that boundary use only this complete broker snapshot;
        # never consult prior-lifecycle ledger rows or saved tranche bases.
        if self._lifecycle_pending_adoption and balance_recognized and qty > 1e-9:
            self._adopt_manual_lifecycle(self.ctx.strategy.symbol, qty, avg_price)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.ctx.client.market == "US":
            await self._refresh_fx_rate()
        balance_path = self.data_dir / f"balance_{self.ctx.account_id}.json"
        try:
            previous_balance = json.loads(balance_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous_balance = {}
        balance_snapshot = {
            "account": self.ctx.account_id, "symbol": self.ctx.strategy.symbol,
            "qty": qty, "avgPrice": avg_price, "updatedAt": datetime.now().isoformat(),
            "holdings": broker_holdings,
            "balanceComplete": bool(balance_recognized),
            "trancheBases": self._tranche_bases,
            "manualTrancheQty": {
                key: float(value.get("manual_qty", 0) or 0)
                for key, value in self._symbol_lifecycles.items()
                if isinstance(value, dict) and value.get("status") == "open"
            },
            # This is separate from the legacy tranche-base cache.  A cached
            # account average must never overwrite an immutable manually
            # adopted Line 1 basis in dashboard recovery.
            "manualTrancheBases": {
                key: float(value.get("manual_price", 0) or 0)
                for key, value in self._symbol_lifecycles.items()
                if isinstance(value, dict) and value.get("status") == "open"
                and float(value.get("manual_price", 0) or 0) > 0
            },
            "currency": self.ctx.currency,
            "reportingCurrency": self.ctx.reporting_currency,
            "fxRateKrw": self._fx_rate_krw,
        }
        # Dashboard readers run in a different process. Replace the snapshot
        # atomically so they either see the previous complete balance or this
        # complete balance, never a partly-written JSON document.
        balance_tmp = balance_path.with_suffix(".json.tmp")
        balance_tmp.write_text(json.dumps(balance_snapshot), encoding="utf-8")
        balance_tmp.replace(balance_path)
        if self._balance_only:
            return
        # One shared broker-authoritative orphan evaluator owns both startup
        # and live cleanup.  It requires two complete zero snapshots and never
        # touches unresolved orders or a nonzero holding.
        quantities = {self._symbol_key(item.get("symbol", "")): float(item.get("qty", 0) or 0)
                      for item in broker_holdings}
        orphan_results = self._orphan_cleaner.sweep(
            quantities, balance_recognized, self.ledger.has_unresolved_orders, apply=True,
        )
        orphan_by_symbol = {item["symbol"]: item for item in orphan_results}
        symbol_key = self._symbol_key(self.ctx.strategy.symbol)
        current_orphan = orphan_by_symbol.get(symbol_key, {})
        if current_orphan.get("classification") == "cleaned":
            self._closed_symbols_blocked.add(symbol_key)
            self._dashboard_auto_buy = self._dashboard_auto_sell = False
            self._dashboard_profile_allowed = False
            self.ctx.strategy.step_qty.clear()
            self.ctx.strategy.step_prices.clear()
            self.ctx.position = PositionState(symbol=self.ctx.strategy.symbol)
            # OrphanStateCleaner persists the closed marker in another read of
            # the lifecycle file. Refresh this engine's in-memory copy too;
            # otherwise re-enabling the same symbol can incorrectly reuse the
            # old open activation identity.
            current = self._symbol_lifecycles.get(symbol_key, {})
            self._symbol_lifecycles[symbol_key] = {
                "status": "closed",
                "started_at": current.get("started_at") if isinstance(current, dict) else None,
                "closed_at": datetime.now(timezone.utc).isoformat(),
            }
            self._prepare_lifecycle_scope(symbol_key)
            self.ctx.logger.info(f"Automatic orphan cleanup completed for {symbol_key}: {current_orphan.get('removed', [])}")
            return
        expected_qty = self._broker_fill_catchup_qty.get(symbol_key)
        if expected_qty is not None:
            if qty + 1e-9 >= expected_qty:
                self._broker_fill_catchup_qty.pop(symbol_key, None)
                self._broker_fill_catchup_warned.discard(symbol_key)
                self.ctx.logger.info(
                    f"Broker balance caught up after confirmed BUY: {self.ctx.strategy.symbol} "
                    f"broker={qty:g} confirmed={expected_qty:g}"
                )
            else:
                # Do not rebuild, adopt, or mark the lower snapshot as an
                # external trade.  A filled order has durable attribution; the
                # broker balance is simply not current yet.
                if symbol_key not in self._broker_fill_catchup_warned:
                    self._broker_fill_catchup_warned.add(symbol_key)
                    self.ctx.logger.warning(
                        f"Broker balance behind confirmed BUY fills for {self.ctx.strategy.symbol}: "
                        f"broker={qty:g} confirmed={expected_qty:g}; preserving tranche state and holding automation"
                    )
                return
        # A broker balance can reflect an accepted order before the execution
        # history endpoint exposes its fill. Do not classify that temporary
        # quantity delta as an unexplained HTS/manual trade: doing so pauses
        # tranche sells and can reset the grid before the pending fill is
        # applied to the ledger. The next sync will apply the confirmed fill
        # and reconcile the balance normally. This applies to both BUY and
        # SELL; the BUY re-entry delay is set only by _apply_confirmed_fill.
        pending_for_symbol = self.ledger.pending_orders(self.ctx.strategy.symbol)
        if ReconciliationIncompleteReason.PENDING_QUANTITY_DEFERRAL in incomplete_reasons:
            self.ctx.logger.info(
                f"Broker quantity change deferred for pending fill reconciliation: "
                f"{self.ctx.strategy.symbol} local={self.ctx.position.qty:g} broker={qty:g} "
                f"pending={len(pending_for_symbol)}"
            )
            return
        # A first complete zero snapshot is report-only. The orphan evaluator
        # performs automatic state cleanup only after a second complete zero
        # snapshot, preventing a transient/stale zero from deleting a symbol.
        if balance_recognized and qty <= 1e-9:
            self._dashboard_auto_buy = self._dashboard_auto_sell = False
            self._dashboard_profile_allowed = False
            self._trading_paused = True
            self._pause_reason = "broker_quantity_unattributed"
            self.ctx.logger.info(
                f"Complete zero balance observed for {symbol_key}; orphan cleanup confirmation "
                f"{current_orphan.get('zeroConfirmations', 0)}/2"
            )
            return
        # A restart has no in-memory fill-catchup marker, but an open lifecycle
        # has an equally strong durable minimum: its adopted manual tranche 1
        # plus its still-open confirmed program lots.  A lower broker snapshot
        # immediately after restart can be stale. Preserve the reconstructed
        # map and fail closed rather than dropping T2/T3 or blending anything
        # into T1; a later current broker snapshot releases this normal gate.
        lifecycle = self._symbol_lifecycles.get(symbol_key, {})
        if isinstance(lifecycle, dict) and lifecycle.get("status") == "open":
            manual_qty = max(0.0, float(lifecycle.get("manual_qty", 0) or 0))
            confirmed_qty = sum(
                self.ledger.open_tranche_qty(self.ctx.strategy.symbol, step)
                for step in range(2, self.ctx.strategy.max_step + 1)
            )
            lifecycle_min_qty = manual_qty + confirmed_qty
            if lifecycle_min_qty > 0 and qty + 1e-9 < lifecycle_min_qty:
                self._broker_fill_catchup_qty[symbol_key] = lifecycle_min_qty
                if symbol_key not in self._broker_fill_catchup_warned:
                    self._broker_fill_catchup_warned.add(symbol_key)
                    self.ctx.logger.warning(
                        f"Broker balance behind open lifecycle after restart for {self.ctx.strategy.symbol}: "
                        f"broker={qty:g} lifecycle-minimum={lifecycle_min_qty:g}; preserving tranches and blocking automation"
                    )
                return
        known_tranche_qty = sum(qty for qty in self.ctx.strategy.step_qty.values() if qty > 0)
        if known_tranche_qty > qty + 1e-9:
            # Historical runtime state exceeds the broker balance. Rebuild
            # from confirmed open ledger quantities first; never relabel the
            # whole broker balance as tranche 1. Preserve one manual HTS
            # share when no confirmed step-1 fill exists, then cap confirmed
            # later tranches to the live broker quantity.
            open_rows = self._reconciliation_open_rows(self.ctx.strategy.symbol, avg_price)
            remaining = float(qty)
            incomplete_reasons = self._reconciliation_incomplete_reasons(
                self.ctx.strategy.symbol, balance_recognized=balance_recognized,
                holding=normalized_holding, qty=qty,
                known_tranche_qty=known_tranche_qty, open_rows=open_rows,
            )
            if ReconciliationIncompleteReason.TRANCHE_REBUILD_AMBIGUOUS in incomplete_reasons:
                confirmed_steps = [step for step, _, _ in open_rows]
                message = (
                    f"Ambiguous tranche rebuild for {self.ctx.strategy.symbol}: "
                    f"broker_qty={qty:g}, known_qty={known_tranche_qty:g}, "
                    f"confirmed_steps={confirmed_steps}; automated orders paused"
                )
                self._trading_paused = True
                self._tranche_sell_paused = True
                self._pause_reason = "tranche_rebuild_ambiguous"
                self.ctx.logger.error(message)
                await self.telegram.notify_error(message)
                return
            self.ctx.strategy.step_qty.clear()
            self.ctx.strategy.step_prices.clear()
            self.ctx.position = PositionState(symbol=self.ctx.strategy.symbol)
            for step, open_qty, price in open_rows:
                if remaining <= 0:
                    break
                allocated = min(float(open_qty), remaining)
                self.ctx.strategy.step_qty[step] = int(allocated)
                self.ctx.strategy.step_prices[step] = price
                remaining -= allocated
            known_tranche_qty = sum(self.ctx.strategy.step_qty.values())
            # The tranche map and aggregate position must be restored as one
            # state transition. Leaving PositionState at zero makes the next
            # tick generate a new first-tranche BUY even though the broker
            # already owns the reconstructed position.
            self.ctx.position.qty = float(qty)
            self.ctx.position.avg_price = float(avg_price)
            self.ctx.position.step = max(
                (step for step, step_qty in self.ctx.strategy.step_qty.items() if step_qty > 0),
                default=0,
            )
            self._tranche_sell_paused = False
            self.ctx.logger.warning(
                f"Rebuilt stale tranche history for {self.ctx.strategy.symbol} from broker quantity {qty}; "
                f"preserved confirmed tranche attribution; "
                f"tranches={{{', '.join(f'{step}:{self.ctx.strategy.step_qty[step]:g}@{self.ctx.strategy.step_prices.get(step, 0):g}' for step in sorted(self.ctx.strategy.step_qty))}}}"
            )
        if abs(float(self.ctx.position.qty) - qty) <= 1e-9:
            self._tranche_sell_paused = False
        if abs(float(self.ctx.position.qty) - qty) > 1e-9 and known_tranche_qty > 0:
            unmatched_qty = qty - known_tranche_qty
            # A restored program ledger can describe later confirmed tranches
            # while an earlier manual tranche is absent from that ledger.  When
            # the broker owns exactly the known program quantity plus a positive
            # remainder, keep the program tranches intact and isolate the
            # remainder as tranche 1.  This lets a tranche-2 target sell its
            # own confirmed quantity (never the manual share) after restart.
            # Only a lifecycle with no adopted manual lot may use a positive
            # broker remainder to initialize Line 1.  Once a manual HTS lot
            # exists, a larger remainder is un-attributed exposure, not more
            # manual shares.
            lifecycle_manual_qty = max(0.0, float(lifecycle.get("manual_qty", 0) or 0)) if isinstance(lifecycle, dict) else 0.0
            lifecycle_manual_price = float(lifecycle.get("manual_price", 0) or 0) if isinstance(lifecycle, dict) else 0.0
            allocation = _manual_tranche_allocation(
                qty=qty, known_tranche_qty=known_tranche_qty,
                has_step_one=bool(self.ctx.strategy.step_qty.get(1)),
                lifecycle_open=isinstance(lifecycle, dict) and lifecycle.get("status") == "open",
                lifecycle_manual_qty=lifecycle_manual_qty,
            )
            if allocation.restored_manual_qty > 1e-9:
                # Restart recovery restores the immutable manual lot first.
                # Any broker quantity beyond that lot remains un-attributed and
                # is handled by the fail-closed branch below; it is never
                # merged into Line 1.
                restored_manual_qty = allocation.restored_manual_qty
                self.ctx.strategy.step_qty[1] = int(restored_manual_qty)
                self.ctx.strategy.step_prices[1] = lifecycle_manual_price or avg_price
                self._store_tranche_base(self.ctx.strategy.symbol, self.ctx.strategy.step_prices[1])
                self.ctx.position.qty = known_tranche_qty + restored_manual_qty
                self.ctx.position.avg_price = avg_price
                self.ctx.position.step = max(
                    (step for step, step_qty in self.ctx.strategy.step_qty.items() if step_qty > 0), default=1,
                )
                known_tranche_qty += restored_manual_qty
                unmatched_qty = qty - known_tranche_qty
                if unmatched_qty <= 1e-9:
                    self._tranche_sell_paused = False
                    return
            if allocation.adopt_manual_qty > 1e-9:
                confirmed_lots = [
                    (step, float(step_qty), float(self.ctx.strategy.step_prices.get(step, avg_price)))
                    for step, step_qty in self.ctx.strategy.step_qty.items() if step_qty > 0
                ]
                first_price = self._validated_manual_tranche_base(
                    self.ctx.strategy.symbol, qty, avg_price, confirmed_lots, allocation.adopt_manual_qty
                )
                self._refresh_open_lifecycle_manual_basis(self.ctx.strategy.symbol, allocation.adopt_manual_qty, first_price)
                self.ctx.strategy.step_qty[1] = int(allocation.adopt_manual_qty)
                self.ctx.strategy.step_prices[1] = first_price
                self.ctx.position.qty = qty
                self.ctx.position.avg_price = avg_price
                self.ctx.position.step = max(
                    (step for step, step_qty in self.ctx.strategy.step_qty.items() if step_qty > 0),
                    default=0,
                )
                self._tranche_sell_paused = False
                self.ctx.logger.info(
                    f"Broker remainder isolated as manual tranche 1 for {self.ctx.strategy.symbol}: "
                    f"qty={allocation.adopt_manual_qty:g}, base={first_price}"
                )
                return
            if allocation.unattributed_remainder <= 1e-9:
                return
            incomplete_reasons = self._reconciliation_incomplete_reasons(
                self.ctx.strategy.symbol, balance_recognized=balance_recognized,
                holding=normalized_holding, qty=qty,
                unattributed_remainder=allocation.unattributed_remainder,
            )
            if ReconciliationIncompleteReason.UNATTRIBUTED_QUANTITY_PAUSE not in incomplete_reasons:
                return
            # A broker quantity change that cannot be attributed to a confirmed
            # tranche fill must never be folded into tranche 1. Doing so could
            # make a tranche-profit sale submit the whole holding. Preserve the
            # known tranche map and require reconciliation before resuming.
            self._tranche_sell_paused = True
            self._trading_paused = True
            self._pause_reason = "broker_quantity_unattributed"
            self.ctx.logger.error(
                f"Broker quantity changed without tranche attribution for {self.ctx.strategy.symbol}; "
                "all automated orders paused to protect tranche-only quantities"
            )
            return
        if abs(float(self.ctx.position.qty) - qty) > 1e-9:
            # HTS/manual trades are real broker state, but have no strategy step or
            # ledger provenance. Reflect them immediately while pausing automation
            # so the strategy cannot make an unsafe assumption about the tranche.
            self.ctx.position.qty = qty
            self.ctx.position.avg_price = avg_price if qty else 0.0
            # HTS purchases have no program ledger history.  Once the user has
            # explicitly enabled this holding in the dashboard, treat the broker
            # average as tranche 1 so sell targets and later grid buys have a
            # known, broker-authoritative reference price.
            if qty > 0 and self._dashboard_symbol == self._symbol_key(self.ctx.strategy.symbol):
                tranche_one_price = self._manual_lifecycle_basis_or_broker_average(qty, avg_price)
                self.ctx.position.step = 1
                self.ctx.strategy.step_qty[1] = int(qty)
                self.ctx.strategy.step_prices[1] = tranche_one_price
                # A genuinely new manual Tier 1 uses the broker average. An
                # already-open lifecycle instead retains its recorded manual
                # basis even when broker moving-average accounting has shifted.
                symbol = self._symbol_key(self.ctx.strategy.symbol)
                self._store_tranche_base(symbol, tranche_one_price)
            if not self._auto_trading_enabled:
                self.ctx.logger.info(
                    f"Strategy-symbol broker balance synchronized: {self.ctx.strategy.symbol} qty={qty}, avg={avg_price}"
                )
            else:
                self._trading_paused = True
                self._pause_reason = "external_broker_balance_change"
                msg = (f"Broker balance adopted: qty={qty}, avg={avg_price}; "
                       "automated orders paused because the position changed outside this program")
                self.ctx.logger.error(msg)
                await self.telegram.notify_balance_change(msg)
        # A manually/HTS-held position can have already been adopted before a
        # dashboard profile becomes active. Seed tranche 1 regardless of a
        # quantity change so grid Auto Buy has the broker average as its base.
        if qty > 0 and self._dashboard_symbol == self._symbol_key(self.ctx.strategy.symbol):
            if known_tranche_qty <= 0 and (self.ctx.position.step == 0 or not self.ctx.strategy.step_prices.get(1)):
                tranche_one_price = self._manual_lifecycle_basis_or_broker_average(qty, avg_price)
                self.ctx.position.qty = qty
                self.ctx.position.avg_price = avg_price
                self.ctx.position.step = 1
                self.ctx.strategy.step_qty[1] = int(qty)
                self.ctx.strategy.step_prices[1] = tranche_one_price
                symbol = self._symbol_key(self.ctx.strategy.symbol)
                self._store_tranche_base(symbol, tranche_one_price)
                self.ctx.logger.info(
                    f"HTS holding initialized for automation: {self.ctx.strategy.symbol} qty={qty}, tranche-1 price={tranche_one_price}"
                )

    def _remove_settings_for_confirmed_closures(
        self, previous_balance: dict, broker_holdings: list[dict], balance_recognized: bool
    ) -> None:
        """Remove opted-in profiles only after a complete broker-confirmed closure."""
        if not balance_recognized:
            return
        current_symbols = {
            self._symbol_key(item.get("symbol", ""))
            for item in broker_holdings
            if float(item.get("qty", 0) or 0) > 0
        }
        settings_path = self.data_dir / f"dashboard_settings_{self.ctx.account_id}.json"
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not settings.get("auto_remove_closed_positions", True):
            return
        profiles = settings.get("profiles", [])
        configured_symbols = {
            self._symbol_key((profile.get("config") or {}).get("symbol", ""))
            for profile in profiles if isinstance(profile, dict)
        }
        configured_symbols.discard("")
        # A symbol that was held in the previous complete snapshot and is now
        # absent is a confirmed full close. Remove it immediately. For profiles
        # that were never held, retain the existing two-snapshot safeguard.
        previous_symbols = {
            self._symbol_key(item.get("symbol", ""))
            for item in (previous_balance.get("holdings") or [])
            if float(item.get("qty", 0) or 0) > 0
        }
        for symbol in configured_symbols:
            if symbol in current_symbols:
                self._closure_absence_confirmations.pop(symbol, None)
            elif symbol in previous_symbols:
                self._closure_absence_confirmations[symbol] = 2
            else:
                self._closure_absence_confirmations[symbol] = self._closure_absence_confirmations.get(symbol, 0) + 1
        for symbol in list(self._closure_absence_confirmations):
            if symbol not in configured_symbols:
                self._closure_absence_confirmations.pop(symbol, None)
        self._closure_absence_path.parent.mkdir(exist_ok=True)
        self._closure_absence_path.write_text(
            json.dumps(self._closure_absence_confirmations, ensure_ascii=False), encoding="utf-8"
        )
        closed_symbols = {
            symbol for symbol, count in self._closure_absence_confirmations.items() if count >= 2
        }
        if not closed_symbols:
            return
        retained = [profile for profile in profiles if self._symbol_key(
            (profile.get("config") or {}).get("symbol", "")
        ) not in closed_symbols]
        if len(retained) == len(profiles):
            return
        settings["profiles"] = retained
        settings_path.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
        for symbol in closed_symbols:
            self._closure_absence_confirmations.pop(symbol, None)
        self._closure_absence_path.write_text(
            json.dumps(self._closure_absence_confirmations, ensure_ascii=False), encoding="utf-8"
        )
        self.ctx.logger.info(
            f"Trade Settings removed after broker-confirmed position closure: {sorted(closed_symbols)}"
        )

    def _cleanup_fully_closed_symbol(self, symbol: str) -> None:
        """Atomically retire all per-symbol automation state after full close."""
        symbol = self._symbol_key(symbol)
        if self._symbol_key_manual_review(symbol):
            return
        account = self.ctx.account_id
        control_paths = [
            self.data_dir / f"dashboard_control_{account}_{symbol}.json",
        ]
        for path in control_paths:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                self.ctx.logger.warning(f"Could not remove closed-symbol control file {path}: {exc}")

        settings_path = self.data_dir / f"dashboard_settings_{account}.json"
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            profiles = settings.get("profiles", []) if isinstance(settings, dict) else []
            retained = [p for p in profiles if self._symbol_key((p.get("config") or {}).get("symbol", "")) != symbol]
            if len(retained) != len(profiles):
                settings["profiles"] = retained
                settings_path.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
        except (OSError, json.JSONDecodeError, TypeError):
            pass

        self._remove_tranche_base(symbol)
        self._closure_absence_confirmations.pop(symbol, None)
        self._closure_absence_path.write_text(
            json.dumps(self._closure_absence_confirmations, ensure_ascii=False), encoding="utf-8"
        )
        closed_orders = self.ledger.close_open_orders_for_symbol(symbol)
        self._buy_reentry_after.pop(symbol, None)
        self._last_auto_buy_price.pop(symbol, None)
        self._blocked_order_until = {
            key: value for key, value in self._blocked_order_until.items() if key[1] != symbol
        }
        self.ctx.logger.info(
            f"Fully closed symbol state cleaned: {symbol}; pending strategy orders retired={closed_orders}"
        )

    async def _refresh_fx_rate(self) -> None:
        """Refresh the dashboard-only USD/KRW reference rate at a safe cadence."""
        # Kiwoom currently rejects ust31301 on the US paper-trading domain
        # (RC9000: 업무 미제공).  FX is reporting-only, so do not repeatedly
        # call an unsupported endpoint or make it look like an engine fault.
        if self.ctx.client.market == "US" and self.ctx.client.mode == "mock":
            return
        now = asyncio.get_running_loop().time()
        refresh_sec = float(os.environ.get("KIWOOM_FX_REFRESH_SEC", "60"))
        if now - self._last_fx_request_at < refresh_sec:
            return
        self._last_fx_request_at = now
        try:
            rate = extract_us_fx_rate(await self.ctx.client.get_fx_rate())
            if rate:
                self._fx_rate_krw = rate
        except (KiwoomAPIError, RetryableError) as exc:
            self.ctx.logger.warning(f"USD/KRW reference FX refresh deferred: {exc}")

    def _update_position(self, intent: OrderIntent, side: str):
        pos = self.ctx.position
        if side == "BUY":
            total_cost = pos.qty * pos.avg_price + intent.qty * (intent.price or 0)
            pos.qty += intent.qty
            pos.avg_price = total_cost / pos.qty if pos.qty else 0
            pos.step = intent.meta.get("step", pos.step + 1)
        else:
            pos.realized_pnl += ((intent.price or 0) - pos.avg_price) * intent.qty
            pos.qty = max(0, pos.qty - intent.qty)
            if intent.meta.get("sell_only_step"):
                pos.step = max(0, pos.step - 1)
            if pos.qty == 0:
                pos.step = 0

    async def _safe_get_quote(self) -> tuple[float, str, float] | None:
        try:
            feed_obj = getattr(self.ctx, "price_feed_obj", None)
            if feed_obj is not None and hasattr(feed_obj, "get_quote"):
                return await feed_obj.get_quote(self.ctx.strategy.symbol)
            return await self.price_feed(self.ctx.strategy.symbol), "legacy", datetime.now().timestamp()
        except Exception as exc:
            self.ctx.logger.warning(f"Price lookup failed: {exc}")
            # Some US mock symbols (currently including SOXL) are present in
            # the balance feed but unavailable through usa20100. Use only a
            # recent, broker-supplied balance quote as a bounded fallback;
            # never use the stored average/tranche price as a market quote.
            if self.ctx.client.market == "US" and self.ctx.client.mode == "mock":
                try:
                    path = self.data_dir / f"balance_{self.ctx.account_id}.json"
                    snapshot = json.loads(path.read_text(encoding="utf-8"))
                    updated = datetime.fromisoformat(str(snapshot.get("updatedAt", "")))
                    age = (datetime.now() - updated).total_seconds()
                    if 0 <= age <= 30 and snapshot.get("balanceComplete"):
                        symbol = self._symbol_key(self.ctx.strategy.symbol)
                        row = next(
                            (item for item in snapshot.get("holdings", [])
                             if self._symbol_key(item.get("symbol", "")) == symbol),
                            None,
                        )
                        fallback = float((row or {}).get("currentPrice") or 0)
                        if fallback > 0:
                            self.ctx.logger.info(
                                f"Using recent broker balance quote for {symbol}: "
                                f"{fallback} (usa20100 unavailable, age={age:.1f}s)"
                            )
                            return fallback, "broker-balance-fallback", updated.timestamp()
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    pass
            return None

    async def _safe_get_price(self) -> float | None:
        quote = await self._safe_get_quote()
        return quote[0] if quote is not None else None

    def _next_buy_trigger(self) -> float | None:
        step = int(self.ctx.position.step)
        if step <= 0 or step >= self.ctx.strategy.max_step or step > len(self.ctx.strategy.buy_steps):
            return None
        base = self.ctx.strategy.step_prices.get(step)
        if not base or base <= 0:
            return None
        return float(base) * (1 + float(self.ctx.strategy.buy_steps[step - 1].drop_pct) / 100)

    def _record_evaluated_quote(self, price: float, source: str, observed_at: float) -> None:
        """Publish the exact quote used for a strategy decision, per symbol."""
        path = self.data_dir / f"worker_{self.ctx.account_id}.quotes.json"
        try:
            existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            if not isinstance(existing, dict):
                existing = {}
            trigger = self._next_buy_trigger()
            existing[self._symbol_key(self.ctx.strategy.symbol)] = {
                "price": float(price), "source": source,
                "observedAt": datetime.fromtimestamp(observed_at, timezone.utc).isoformat(),
                "evaluatedAt": datetime.now(timezone.utc).isoformat(),
                "currentStep": int(self.ctx.position.step), "nextBuyTrigger": trigger,
            }
            tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
            tmp.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self.ctx.logger.warning(f"Quote diagnostic publication deferred: {exc}")

    def pause_buying(self): self._buying_paused = True
    def resume_buying(self): self._buying_paused = False
    def resume_trading(self):
        self._trading_paused = False
        self._pause_reason = ""

    def _heartbeat(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "heartbeat.txt").write_text(datetime.now().isoformat())


def _executed_rows(data: dict) -> list[dict]:
    for key in ("cntr", "result_list", "result_lsit", "acnt_ord_cntr_prps_dtl"):
        if isinstance(data.get(key), list):
            return data[key]
    return []

def _number(value) -> float:
    try: return abs(float(str(value).replace(",", "").replace("+", "").strip()))
    except (TypeError, ValueError): return 0.0


def _kr_tick_size(price: float) -> int:
    """Return the domestic limit-order tick for the price bands used by Kiwoom."""
    if price < 1_000:
        return 1
    if price < 5_000:
        return 5
    if price < 10_000:
        return 10
    if price < 50_000:
        return 50
    if price < 100_000:
        return 100
    if price < 500_000:
        return 500
    return 1_000


def _kr_order_price(price: float, side: str) -> float:
    """Make a positive KR limit price valid without worsening order safety."""
    tick = _kr_tick_size(price)
    if str(side).upper() == "SELL":
        return float(math.ceil(price / tick) * tick)
    return float(math.floor(price / tick) * tick)

def _filled_at(row: dict) -> str:
    value = str(row.get("ord_dt") or "").replace("-", "")
    return f"{value[:4]}-{value[4:6]}-{value[6:]}" if len(value) == 8 and value.isdigit() else datetime.now().date().isoformat()

def _balance_holding(market: str, data: dict, symbol: str) -> tuple[float, float] | None:
    saw_holdings = False
    for key in ("acnt_evlt_remn_indv_tot", "stk_cntr_remn", "acnt_bal", "result_list", "result_lsit", "holdings"):
        rows = data.get(key)
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            continue
        saw_holdings = True
        for row in rows:
            if _same_symbol(market, row.get("stk_cd", ""), symbol):
                for qty_key in ("rmnd_qty", "poss_qty", "hold_qty", "cur_qty", "qty"):
                    if qty_key in row:
                        avg = next((_number(row[k]) for k in ("buy_uv", "avg_prc", "pur_pric") if k in row), 0.0)
                        return _number(row[qty_key]), avg
    # An authoritative empty holdings list means the account owns zero shares.
    # Returning None is reserved for an unrecognized/malformed response.
    if saw_holdings:
        return (0.0, 0.0)

    # Some mock responses wrap the holdings list in another object. Walk only
    # dictionaries that look like holding rows; never interpret arbitrary
    # account totals as a position quantity.
    def walk(value):
        if isinstance(value, dict):
            code = next((value.get(k) for k in ("stk_cd", "symbol", "code") if value.get(k) is not None), None)
            if code is not None and _same_symbol(market, code, symbol):
                qty_key = next((k for k in ("rmnd_qty", "cur_qty", "poss_qty", "hold_qty", "qty", "setl_remn") if k in value), None)
                if qty_key:
                    avg = next((_number(value[k]) for k in ("buy_uv", "avg_prc", "pur_pric", "cntr_uv") if k in value), 0.0)
                    return _number(value[qty_key]), avg
            for child in value.values():
                found = walk(child)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = walk(child)
                if found is not None:
                    return found
        return None

    found = walk(data)
    if found is not None:
        return found
    return None


def _same_symbol(market: str, value, symbol: str) -> bool:
    """Compare symbols with the shared market-aware runtime key."""
    return canonical_symbol_key(market, value) == canonical_symbol_key(market, symbol)


def _holding_summary(data: dict) -> str:
    """Safe diagnostic: structure/field names only, never account credentials."""
    rows = data.get("acnt_evlt_remn_indv_tot")
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return f"acnt_evlt_remn_indv_tot={type(rows).__name__}"
    if not rows:
        return "acnt_evlt_remn_indv_tot=[]"
    first = rows[0] if isinstance(rows[0], dict) else {}
    codes = [str(r.get("stk_cd", "")).strip() for r in rows if isinstance(r, dict)]
    return f"rows={len(rows)}, codes={codes}, row_fields={sorted(first.keys())}"


def _kr_balance_recognized(data: dict) -> bool:
    """Whether a domestic balance response contains an authoritative rows field."""
    return any(key in data for key in ("acnt_evlt_remn_indv_tot", "stk_cntr_remn", "acnt_bal", "result_list", "holdings"))


def _all_balance_holdings(market: str, data: dict) -> list[dict]:
    """Map the authoritative Kiwoom holdings list for dashboard display."""
    rows = data.get("acnt_evlt_remn_indv_tot", [])
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return []
    holdings = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("stk_cd"):
            continue
        qty = _number(row.get("rmnd_qty"))
        if qty <= 0:
            continue
        holdings.append({
            "symbol": canonical_symbol_key(market, row["stk_cd"]),
            "name": str(row.get("stk_nm", "")).strip(),
            "qty": qty,
            "avgPrice": _number(row.get("pur_pric")),
            "currentPrice": _number(row.get("cur_prc")),
            "prevClose": _number(row.get("pred_close_pric")),
        })
    return holdings
