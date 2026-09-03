"""Alert-only heartbeat monitor for the mock workers.

This script reads worker status files and sends ntfy alerts for invalid or
stale status. It never starts, stops, or modifies a worker.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATUS_DIR = PROJECT_ROOT / "data"
DEFAULT_LOG_PATH = PROJECT_ROOT / "diagnostics" / "heartbeat_alert_watchdog.log"
NTFY_URL = "https://ntfy.sh/kiwoom-alert-9885xloihafe"
STALE_AFTER_SECONDS = 120
ACCOUNTS = ("kr_mock", "us_mock")
REQUIRED_FIELDS = ("account", "market", "pid", "state", "updatedAt")


def _logger(path: Path) -> logging.Logger:
    logger = logging.getLogger("heartbeat_alert_watchdog")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def _problem(account: str, status_dir: Path, now: datetime) -> str | None:
    path = status_dir / f"worker_{account}.status.json"
    if not path.is_file():
        return f"{account} status file missing"

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return f"{account} status file invalid: {exc}"

    if not isinstance(payload, dict):
        return f"{account} status is not an object"
    missing = [field for field in REQUIRED_FIELDS if field not in payload]
    if missing:
        return f"{account} status missing fields: {', '.join(missing)}"
    if payload["state"] != "RUNNING":
        return f"{account} state is {payload['state']!r}"

    try:
        updated = datetime.fromisoformat(str(payload["updatedAt"]).replace("Z", "+00:00"))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError) as exc:
        return f"{account} heartbeat timestamp invalid: {exc}"

    age = (now - updated.astimezone(timezone.utc)).total_seconds()
    if age > STALE_AFTER_SECONDS:
        return f"{account} stale: last heartbeat {int(age)}s ago"
    return None


def _send_alert(message: str, logger: logging.Logger) -> None:
    request = Request(
        NTFY_URL,
        data=message.encode("utf-8"),
        headers={"Content-Type": "text/plain; charset=utf-8"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            response.read()
            logger.info("ntfy alert sent: status=%s message=%r", response.status, message)
    except (HTTPError, URLError, OSError) as exc:
        logger.error("ntfy alert failed for %r: %s", message, exc)


def _write_startup_status(status_dir: Path) -> None:
    status_path = status_dir / "heartbeat_alert_watchdog.status.json"
    payload = {
        "pid": os.getpid(),
        "role": "heartbeat_alert_watchdog",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "account_scope": list(ACCOUNTS),
    }
    status_dir.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status-dir", type=Path, default=DEFAULT_STATUS_DIR)
    parser.add_argument("--log-path", type=Path, default=DEFAULT_LOG_PATH)
    args = parser.parse_args()

    _write_startup_status(args.status_dir)
    logger = _logger(args.log_path)
    now = datetime.now(timezone.utc)
    for account in ACCOUNTS:
        message = _problem(account, args.status_dir, now)
        if message is not None:
            _send_alert(message, logger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
