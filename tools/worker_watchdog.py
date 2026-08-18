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
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env", override=False)

from src import worker_supervisor
from src.core.runtime_paths import DATA_DIR, LOG_DIR
from src.utils.logger import get_logger

WATCHDOG_LOG = get_logger("watchdog", str(LOG_DIR / "watchdog.log"))
WATCHDOG_STATE = DATA_DIR / "watchdog_state.json"
MAX_CONSECUTIVE_FAILURES = 3

# account_id -> market. Update this alongside adding a new Task Scheduler
# entry for that account -- see module docstring.
MONITORED_ACCOUNTS: dict[str, str] = {
    "kr_mock": "KR",
    "us_mock": "US",
}


def _load_state() -> dict[str, dict[str, int | bool]]:
    try:
        payload = json.loads(WATCHDOG_STATE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, dict[str, int | bool]]) -> None:
    WATCHDOG_STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = WATCHDOG_STATE.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    temporary.replace(WATCHDOG_STATE)


def _send_breaker_alert(account: str, market: str, failures: int) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        WATCHDOG_LOG.error(f"[{account}] circuit breaker tripped; Telegram credentials unavailable")
        return
    message = (
        "URGENT: worker watchdog circuit breaker tripped\n"
        f"Account: {account}\nMarket: {market}\n"
        f"Consecutive failures: {failures}\n"
        "Automatic restarts are paused. Investigate before restarting."
    )
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=urlencode({"chat_id": chat_id, "text": message}).encode("utf-8"),
        method="POST",
    )
    try:
        with urlopen(request, timeout=10):
            pass
        WATCHDOG_LOG.error(f"[{account}] circuit breaker alert sent")
    except Exception as exc:  # noqa: BLE001 - alert failure must not stop other accounts
        WATCHDOG_LOG.error(f"[{account}] circuit breaker alert failed: {exc}")


def check_and_restart(account: str, market: str) -> None:
    state = _load_state()
    account_state = state.get(account, {})
    if not isinstance(account_state, dict):
        account_state = {}
    account_state = {
        "consecutive_failures": int(account_state.get("consecutive_failures", 0) or 0),
        "alerted": bool(account_state.get("alerted", False)),
    }
    state[account] = account_state
    try:
        current = worker_supervisor.status(account)
    except Exception as exc:  # noqa: BLE001 - a broken status read should not stop the sweep
        WATCHDOG_LOG.warning(f"[{account}] status check failed: {exc}; attempting restart")
        current = {"running": False}

    if current.get("running"):
        if account_state["consecutive_failures"] or account_state["alerted"]:
            state[account] = {"consecutive_failures": 0, "alerted": False}
            _save_state(state)
        WATCHDOG_LOG.debug(f"[{account}] alive (pid={current.get('pid')})")
        return

    failures = int(account_state["consecutive_failures"]) + 1
    account_state["consecutive_failures"] = failures
    if failures >= MAX_CONSECUTIVE_FAILURES:
        WATCHDOG_LOG.critical(f"[{account}] circuit breaker tripped after {failures} consecutive failures")
        if not account_state["alerted"]:
            _send_breaker_alert(account, market, failures)
            account_state["alerted"] = True
        _save_state(state)
        return

    WATCHDOG_LOG.warning(f"[{account}] not running (last status: {current}); restarting")
    try:
        code, payload = worker_supervisor.start(account, market)
    except Exception as exc:  # noqa: BLE001 - log and move on to the next account
        WATCHDOG_LOG.error(f"[{account}] restart raised an exception: {exc}")
        _save_state(state)
        return

    if code == 0:
        WATCHDOG_LOG.info(f"[{account}] restarted: {payload}")
    elif payload.get("failureClass") == "lock-conflict":
        account_state["consecutive_failures"] = 0
        WATCHDOG_LOG.warning(
            f"[{account}] restart blocked by an existing worker; lock protection is active: {payload}"
        )
    else:
        WATCHDOG_LOG.error(f"[{account}] restart failed (code={code}): {payload}")
    _save_state(state)


def main() -> int:
    for account, market in MONITORED_ACCOUNTS.items():
        check_and_restart(account, market)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
