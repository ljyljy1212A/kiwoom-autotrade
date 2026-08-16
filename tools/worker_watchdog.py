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

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.logger import get_logger
from src.core.runtime_paths import LOG_DIR
from src import worker_supervisor

WATCHDOG_LOG = get_logger("watchdog", str(LOG_DIR / "watchdog.log"))

# account_id -> market. Update this alongside adding a new Task Scheduler
# entry for that account -- see module docstring.
MONITORED_ACCOUNTS: dict[str, str] = {
    "kr_mock": "KR",
    "us_mock": "US",
}


def check_and_restart(account: str, market: str) -> None:
    try:
        current = worker_supervisor.status(account)
    except Exception as exc:  # noqa: BLE001 - a broken status read should not stop the sweep
        WATCHDOG_LOG.warning(f"[{account}] status check failed: {exc}; attempting restart")
        current = {"running": False}

    if current.get("running"):
        WATCHDOG_LOG.debug(f"[{account}] alive (pid={current.get('pid')})")
        return

    WATCHDOG_LOG.warning(f"[{account}] not running (last status: {current}); restarting")
    try:
        code, payload = worker_supervisor.start(account, market)
    except Exception as exc:  # noqa: BLE001 - log and move on to the next account
        WATCHDOG_LOG.error(f"[{account}] restart raised an exception: {exc}")
        return

    if code == 0:
        WATCHDOG_LOG.info(f"[{account}] restarted: {payload}")
    else:
        WATCHDOG_LOG.error(f"[{account}] restart failed (code={code}): {payload}")


def main() -> int:
    for account, market in MONITORED_ACCOUNTS.items():
        check_and_restart(account, market)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
