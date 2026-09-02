"""Detect a detached worker that crashed after a successful launch and
restart it.

Task Scheduler's "restart on failure" only catches a failure of the
`worker_supervisor start` launcher itself (see src/worker_supervisor.py,
`start()` returns after spawning a detached child process). It does NOT
detect the detached worker dying later. This script closes that gap: it
is meant to be run periodically (e.g. every 2 minutes) via its own Task
Scheduler entry, checks each monitored account's status once, and
restarts any account that isn't running.

Monitored accounts are hardcoded on purpose, not read from
config/accounts.yaml: the yaml lists all four defined accounts
(including kr_real/us_real) regardless of whether they are actually
being operated. Reading it dynamically would risk auto-starting a real
account before it's actually meant to go live. When kr_real/us_real are
promoted to live operation, add them to MONITORED_ACCOUNTS below AND
register their own Task Scheduler entries at the same time.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env", override=False)

from src import worker_supervisor
from src.core.process_inventory import query_win32_processes
from src.core.runtime_paths import DATA_DIR, LOG_DIR
from src.utils.logger import get_logger

WATCHDOG_LOG = get_logger("watchdog", str(LOG_DIR / "watchdog.log"))
WATCHDOG_STATE = DATA_DIR / "watchdog_state.json"
STATUS_DIR = DATA_DIR
MAX_CONSECUTIVE_FAILURES = 3
SUPPRESS_RELAUNCH_SECONDS = 15 * 60
STALE_AFTER_SECONDS = 120
COOLDOWN_SECONDS = 15 * 60

# account_id -> market. Update this alongside adding a new Task Scheduler
# entry for that account -- see module docstring.
MONITORED_ACCOUNTS: dict[str, str] = {
    "kr_mock": "KR",
    "us_mock": "US",
}


def _load_state() -> dict[str, dict]:
    try:
        payload = json.loads(WATCHDOG_STATE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def _intentional_stop_active(account: str) -> tuple[bool, str]:
    path = DATA_DIR / f"intentional_stop_{account}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False, "marker absent or invalid"
    if not isinstance(payload, dict) or payload.get("account") != account:
        return False, "marker account mismatch"
    try:
        expires_at = datetime.fromisoformat(
            str(payload["expiresAt"]).replace("Z", "+00:00")
        )
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return False, "marker expiry invalid"
    if datetime.now(timezone.utc) >= expires_at.astimezone(timezone.utc):
        return False, "marker expired"
    return True, f"expiresAt={expires_at.isoformat()}"


def _save_state(state: dict[str, dict]) -> None:
    WATCHDOG_STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = WATCHDOG_STATE.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    temporary.replace(WATCHDOG_STATE)


def _send_notification(account: str, market: str, event: str, message: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        WATCHDOG_LOG.error(f"[{account}] {event} notification unavailable: Telegram credentials unavailable")
        return
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=urlencode({"chat_id": chat_id, "text": message}).encode("utf-8"),
        method="POST",
    )
    try:
        with urlopen(request, timeout=10):
            pass
        WATCHDOG_LOG.info(f"[{account}] {event} notification sent")
    except Exception as exc:  # noqa: BLE001 - alert failure must not stop other accounts
        WATCHDOG_LOG.error(f"[{account}] {event} notification failed: {exc}")


def _status_metadata(account: str) -> tuple[dict | None, str | None]:
    path = STATUS_DIR / f"worker_{account}.status.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return None, f"status file invalid: {exc}"
    if not isinstance(payload, dict):
        return None, "status file is not an object"
    return payload, None


def check_duplicate_live_process(
    account: str,
    recorded_pid: int,
    process_query_fn,
    *,
    logger=WATCHDOG_LOG,
    notification_fn=_send_notification,
) -> bool:
    """Alert when two complete, matching mock-worker processes are live.

    ``process_query_fn`` is deliberately injected so this detector remains
    isolated from the operating-system process table. It must return an
    iterable of objects with ``pid``, ``account``, ``market``, ``live``, and
    ``command_line`` attributes. An incomplete or inconsistent result is
    indeterminate and produces no alert.
    """
    market = MONITORED_ACCOUNTS.get(account)
    if market is None:
        return False
    try:
        expected_pid = int(recorded_pid)
    except (TypeError, ValueError):
        return False
    if expected_pid <= 0:
        return False

    try:
        processes = process_query_fn(account, market)
        if not isinstance(processes, (list, tuple)):
            return False
        matching = []
        expected_signature = f"-m src.main --market {market}"
        for process in processes:
            pid = getattr(process, "pid", None)
            process_account = getattr(process, "account", None)
            process_market = getattr(process, "market", None)
            live = getattr(process, "live", None)
            command_line = getattr(process, "command_line", None)
            if not isinstance(pid, int) or pid <= 0:
                return False
            if not isinstance(process_account, str) or not isinstance(process_market, str):
                return False
            if not isinstance(live, bool) or not isinstance(command_line, str):
                return False
            if (
                live
                and process_account == account
                and process_market.upper() == market
                and expected_signature in command_line
            ):
                matching.append(pid)
    except Exception:  # noqa: BLE001 - an observation failure is indeterminate
        return False

    if matching.count(expected_pid) != 1 or len(set(matching)) < 2:
        return False
    duplicate_pids = sorted(set(matching))
    detail = f"matching live PIDs={duplicate_pids}; recorded PID={expected_pid}"
    logger.warning(f"[{account}] duplicate live worker detected: {detail}")
    notification_fn(
        account,
        market,
        "duplicate-live-process",
        f"WORKER DUPLICATE PROCESS DETECTED\n"
        f"Account: {account}\nMarket: {market}\nDetail: {detail}\n"
        "No process termination or restart was attempted.",
    )
    return True


def enumerate_worker_processes(account: str, market: str) -> list[SimpleNamespace]:
    """Enumerate matching mock-worker processes for the detector."""
    expected_market = MONITORED_ACCOUNTS.get(account)
    if expected_market != market:
        return []
    supervisor_signature = f"-m src.worker_supervisor start --account {account} --market {market}"
    child_signature = f"-m src.main --market {market}"
    records = query_win32_processes()
    worker_names = {"python.exe", "pythonw.exe"}
    supervisor_pids = set()
    for record in records:
        name = getattr(record, "Name", None)
        command_line = getattr(record, "CommandLine", None)
        pid = getattr(record, "ProcessId", None)
        if not isinstance(name, str) or name.lower() not in worker_names:
            continue
        if not isinstance(command_line, str) or supervisor_signature not in command_line:
            continue
        try:
            supervisor_pid = int(pid)
        except (TypeError, ValueError):
            continue
        if supervisor_pid > 0:
            supervisor_pids.add(supervisor_pid)

    result = []
    for record in records:
        command_line = getattr(record, "CommandLine", None)
        name = getattr(record, "Name", None)
        pid = getattr(record, "ProcessId", None)
        parent_pid = getattr(record, "ParentProcessId", None)
        if not isinstance(command_line, str) or not isinstance(name, str):
            continue
        if name.lower() not in worker_names or child_signature not in command_line:
            continue
        try:
            normalized_pid = int(pid)
            normalized_parent_pid = int(parent_pid)
        except (TypeError, ValueError):
            continue
        if normalized_pid <= 0 or normalized_parent_pid not in supervisor_pids:
            continue
        result.append(
            SimpleNamespace(
                pid=normalized_pid,
                account=account,
                market=market,
                live=True,
                command_line=command_line,
            )
        )
    return result


def _classify(account: str, market: str, current: dict) -> tuple[str, str]:
    if current.get("liveness") == "suspect":
        return "suspect", f"mutex liveness is indeterminate (winerror={current.get('livenessError')})"
    if not current.get("running"):
        return "dead", "account mutex is not alive"

    payload, error = _status_metadata(account)
    if error is not None:
        return "suspect", error
    assert payload is not None
    try:
        metadata_pid = int(payload.get("pid", 0))
        supervisor_pid = int(current.get("pid", 0))
    except (TypeError, ValueError):
        return "suspect", "status PID is invalid"
    if payload.get("account") != account or payload.get("market") != market:
        return "suspect", "status account or market does not match watchdog target"
    if metadata_pid <= 0 or metadata_pid != supervisor_pid:
        return "suspect", "status PID does not match supervisor PID"
    if payload.get("state") != "RUNNING":
        return "suspect", f"status state is {payload.get('state')!r}"
    try:
        updated = datetime.fromisoformat(str(payload["updatedAt"]).replace("Z", "+00:00"))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return "suspect", "status heartbeat timestamp is invalid"
    age = (datetime.now(timezone.utc) - updated.astimezone(timezone.utc)).total_seconds()
    if age > STALE_AFTER_SECONDS:
        return "suspect", f"status heartbeat is stale ({int(age)}s)"
    return "healthy", "mutex, metadata, and heartbeat are current"


def _account_state(state: dict[str, dict], account: str) -> dict:
    stored = state.get(account, {})
    if not isinstance(stored, dict):
        stored = {}
    result = {
        "consecutive_failures": int(stored.get("consecutive_failures", 0) or 0),
        "alerted": bool(stored.get("alerted", False)),
        "suspect_alerted": bool(stored.get("suspect_alerted", False)),
        "last_attempt_at": str(stored.get("last_attempt_at", "")),
        "cooldown_until": str(stored.get("cooldown_until", "")),
    }
    state[account] = result
    return result


def _in_cooldown(account_state: dict) -> bool:
    raw = account_state.get("cooldown_until", "")
    if not raw:
        return False
    try:
        until = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    return datetime.now(timezone.utc) < until.astimezone(timezone.utc)


def check_and_restart(account: str, market: str) -> None:
    if MONITORED_ACCOUNTS.get(account) != market:
        WATCHDOG_LOG.error(f"[{account}] rejected unallowlisted watchdog target for market {market!r}")
        return
    state = _load_state()
    account_state = _account_state(state, account)
    try:
        current = worker_supervisor.status(account)
    except Exception as exc:  # noqa: BLE001 - a broken status read should not stop the sweep
        WATCHDOG_LOG.error(f"[{account}] supervisor status check failed: {exc}; treating as suspect")
        current = {"running": True, "pid": 0}
    else:
        try:
            duplicate = check_duplicate_live_process(
                account,
                current.get("pid"),
                enumerate_worker_processes,
            )
        except Exception as exc:  # noqa: BLE001 - detector failure must not stop the sweep
            WATCHDOG_LOG.error(f"[{account}] duplicate-process check failed: {exc}")
            duplicate = False
        if duplicate:
            return

    classification, detail = _classify(account, market, current)
    WATCHDOG_LOG.info(f"[{account}] classification={classification}: {detail}")

    if classification == "dead":
        marker_active, marker_detail = _intentional_stop_active(account)
        if marker_active:
            WATCHDOG_LOG.info(
                f"[{account}] intentional stop marker present; skipping relaunch "
                f"({marker_detail})"
            )
            # Do not increment crash-relaunch failure/cooldown counters.
            return

    if classification == "healthy":
        if account_state["consecutive_failures"] or account_state["alerted"] or account_state["suspect_alerted"]:
            WATCHDOG_LOG.info(f"[{account}] recovery confirmed by fresh matching heartbeat")
            _send_notification(
                account, market, "recovery",
                f"WORKER RECOVERED\nAccount: {account}\nMarket: {market}\nFresh heartbeat and ownership metadata confirmed.",
            )
        state[account] = {
            "consecutive_failures": 0,
            "alerted": False,
            "suspect_alerted": False,
            "last_attempt_at": "",
            "cooldown_until": "",
        }
        _save_state(state)
        return

    failures = int(account_state["consecutive_failures"]) + 1
    account_state["consecutive_failures"] = failures
    if classification == "suspect" and not account_state["suspect_alerted"]:
        _send_notification(
            account, market, "suspect",
            f"WORKER SUSPECT\nAccount: {account}\nMarket: {market}\nReason: {detail}\nNo restart was attempted.",
        )
        account_state["suspect_alerted"] = True

    if _in_cooldown(account_state):
        WATCHDOG_LOG.warning(f"[{account}] cooldown active; no restart attempted")
        _save_state(state)
        return

    if failures >= MAX_CONSECUTIVE_FAILURES:
        cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=COOLDOWN_SECONDS)
        account_state["cooldown_until"] = cooldown_until.isoformat()
        WATCHDOG_LOG.critical(
            f"[{account}] circuit breaker tripped after {failures} consecutive non-healthy classifications; "
            f"cooldown until {account_state['cooldown_until']}"
        )
        if not account_state["alerted"]:
            _send_notification(
                account, market, "circuit-breaker",
                "URGENT: worker watchdog circuit breaker tripped\n"
                f"Account: {account}\nMarket: {market}\n"
                f"Consecutive non-healthy classifications: {failures}\n"
                "Automatic restarts are paused for 15 minutes. Investigate before restarting.",
            )
            account_state["alerted"] = True
        _save_state(state)
        return

    if classification != "dead":
        WATCHDOG_LOG.warning(f"[{account}] suspect ownership; no restart attempted")
        _save_state(state)
        return

    account_state["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
    WATCHDOG_LOG.warning(f"[{account}] dead worker; relaunch attempt {failures} via supervisor.start")
    try:
        code, payload = worker_supervisor.start(account, market)
    except Exception as exc:  # noqa: BLE001 - log and move on to the next account
        WATCHDOG_LOG.error(f"[{account}] restart raised an exception: {exc}")
        _send_notification(
            account, market, "relaunch-failed",
            f"WORKER RELAUNCH FAILED\nAccount: {account}\nMarket: {market}\nReason: {exc}",
        )
        _save_state(state)
        return

    if code == 0:
        WATCHDOG_LOG.info(f"[{account}] relaunch requested successfully: {payload}")
    elif payload.get("failureClass") == "lock-conflict":
        account_state["consecutive_failures"] = failures - 1
        WATCHDOG_LOG.warning(
            f"[{account}] relaunch blocked by an existing worker; lock protection is active: {payload}"
        )
    else:
        WATCHDOG_LOG.error(f"[{account}] relaunch failed (code={code}): {payload}")
        _send_notification(
            account, market, "relaunch-failed",
            f"WORKER RELAUNCH FAILED\nAccount: {account}\nMarket: {market}\nResult: {payload}",
        )
    _save_state(state)


def main() -> int:
    for account, market in MONITORED_ACCOUNTS.items():
        check_and_restart(account, market)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
