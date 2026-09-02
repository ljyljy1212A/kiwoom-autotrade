"""Alert-only Windows Scheduled Task healthcheck for the mock-worker project.

Reads configured task metadata, checks each task's LastTaskResult through
Get-ScheduledTaskInfo, checks the configured target path through Test-Path,
and sends ntfy alerts through tools.heartbeat_alert_watchdog.

This script never starts, stops, registers, modifies, disables, or reruns a
scheduled task or worker.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.heartbeat_alert_watchdog import _logger, _send_alert

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "scheduled_task_healthcheck.json"
DEFAULT_LOG_PATH = PROJECT_ROOT / "diagnostics" / "scheduled_task_healthcheck.log"

EXPECTED_TASK_NAMES = Counter(
    {
        "Kiwoom Worker - KR Mock": 1,
        "Kiwoom Worker - US Mock": 1,
        "Kiwoom Worker Watchdog": 2,
        "Kiwoom Heartbeat Alert": 1,
        "Kiwoom Telegram Control Bot": 1,
        "Kiwoom Project Database Backup": 1,
        "Kiwoom Project Files Backup": 1,
    }
)

MOCK_ONLY_TASK_NAMES = frozenset(
    {
        "Kiwoom Worker Watchdog",
        "Kiwoom Worker KR Mock",
        "Kiwoom Worker US Mock",
    }
)


@dataclass(frozen=True)
class TaskSpec:
    task_name: str
    task_path: str
    target_path: Path


@dataclass(frozen=True)
class DashboardSpec:
    url: str
    timeout_seconds: float


def _powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _run_powershell(command: str) -> tuple[str | None, str | None]:
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or (
            f"PowerShell exited with code {result.returncode}"
        )
        return None, detail
    return result.stdout.strip(), None


def _load_config(config_path: Path, mode: str | None = None) -> tuple[list[TaskSpec], DashboardSpec | None]:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        raw_tasks, raw_dashboard = payload, None
    elif isinstance(payload, dict):
        raw_tasks, raw_dashboard = payload.get("tasks"), payload.get("dashboard")
    else:
        raise ValueError("healthcheck config must be a JSON list or object")
    if not isinstance(raw_tasks, list):
        raise ValueError("healthcheck config tasks must be a JSON list")

    tasks: list[TaskSpec] = []
    for item in raw_tasks:
        if not isinstance(item, dict):
            raise ValueError("each healthcheck config entry must be an object")

        task_name = item.get("task_name")
        task_path = item.get("task_path")
        target_path = item.get("target_path")
        if not all(
            isinstance(value, str) and value
            for value in (task_name, task_path, target_path)
        ):
            raise ValueError(
                "each healthcheck config entry requires non-empty "
                "task_name, task_path, and target_path strings"
            )

        tasks.append(
            TaskSpec(
                task_name=task_name,
                task_path=task_path,
                target_path=Path(target_path),
            )
        )

    actual_names = Counter(task.task_name for task in tasks)
    if mode == "mock-only":
        rejected = sorted(set(actual_names) - MOCK_ONLY_TASK_NAMES)
        if rejected:
            raise ValueError(
                f"mock-only config contains unsupported task name: {rejected[0]!r}"
            )
        if any(count != 1 for count in actual_names.values()):
            raise ValueError("mock-only config cannot contain duplicate task names")
        if actual_names.get("Kiwoom Worker Watchdog", 0) != 1:
            raise ValueError(
                "mock-only config must contain exactly one "
                "Kiwoom Worker Watchdog task"
            )
    elif mode is not None:
        raise ValueError(f"unsupported healthcheck mode: {mode!r}")
    elif actual_names != EXPECTED_TASK_NAMES:
        raise ValueError(
            "healthcheck config must contain exactly the inventoried eight "
            f"tasks; expected={dict(EXPECTED_TASK_NAMES)!r} "
            f"actual={dict(actual_names)!r}"
        )

    if raw_dashboard is None:
        return tasks, None
    if not isinstance(raw_dashboard, dict):
        raise ValueError("dashboard healthcheck config must be an object")
    url = raw_dashboard.get("url")
    timeout_seconds = raw_dashboard.get("timeout_seconds")
    if not isinstance(url, str) or not url:
        raise ValueError("dashboard healthcheck config requires a non-empty url")
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or not 0 < timeout_seconds <= 10:
        raise ValueError("dashboard healthcheck timeout_seconds must be greater than 0 and at most 10")
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
        raise ValueError("dashboard healthcheck URL must be an http://127.0.0.1 loopback URL with a port")
    return tasks, DashboardSpec(url=url, timeout_seconds=float(timeout_seconds))


def _load_tasks(config_path: Path) -> list[TaskSpec]:
    """Compatibility helper for callers that need only the existing task list."""
    return _load_config(config_path)[0]


def _last_task_result(task: TaskSpec) -> tuple[int | None, str | None]:
    command = (
        "$ErrorActionPreference = 'Stop'; "
        f"$info = Get-ScheduledTaskInfo -TaskName "
        f"{_powershell_literal(task.task_name)} "
        f"-TaskPath {_powershell_literal(task.task_path)}; "
        "[Console]::WriteLine([int64]$info.LastTaskResult)"
    )
    output, error = _run_powershell(command)
    if error is not None:
        return None, error

    try:
        return int(output), None
    except (TypeError, ValueError):
        return None, f"unexpected LastTaskResult output: {output!r}"


def _target_exists(task: TaskSpec) -> tuple[bool | None, str | None]:
    command = (
        "$ErrorActionPreference = 'Stop'; "
        f"if (Test-Path -LiteralPath {_powershell_literal(str(task.target_path))}) "
        "{ [Console]::WriteLine('True') } "
        "else { [Console]::WriteLine('False') }"
    )
    output, error = _run_powershell(command)
    if error is not None:
        return None, error
    if output == "True":
        return True, None
    if output == "False":
        return False, None
    return None, f"unexpected Test-Path output: {output!r}"


def _problems(task: TaskSpec) -> list[str]:
    problems: list[str] = []

    result, result_error = _last_task_result(task)
    if result_error is not None:
        problems.append(f"LastTaskResult query failed: {result_error}")
    elif result != 0:
        problems.append(f"LastTaskResult is non-zero: {result}")

    target_exists, target_error = _target_exists(task)
    if target_error is not None:
        problems.append(f"target-path check failed: {target_error}")
    elif not target_exists:
        problems.append(f"target path missing: {task.target_path}")

    return problems


def _dashboard_problems(dashboard: DashboardSpec) -> list[str]:
    try:
        request = Request(dashboard.url, headers={"Accept": "application/json"})
        with urlopen(request, timeout=dashboard.timeout_seconds) as response:
            if response.status != 200:
                return [f"HTTP status is {response.status}"]
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return [f"HTTP status is {exc.code}"]
    except (URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return [f"HTTP probe failed: {exc}"]
    if payload != {"service": "dashboard", "status": "ok"}:
        return [f"unexpected health payload: {payload!r}"]
    return []


def _emit_alert(message: str, logger, dry_run: bool) -> None:
    logger.error(message)
    if dry_run:
        logger.error("[DRY-RUN] would alert: %s", message)
    else:
        _send_alert(message, logger)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--log-path", type=Path, default=DEFAULT_LOG_PATH)
    parser.add_argument("--mode", choices=("mock-only",), default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="log alert messages without sending them to ntfy",
    )
    args = parser.parse_args()

    logger = _logger(args.log_path)
    try:
        tasks, dashboard = _load_config(args.config, mode=args.mode)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        message = f"Scheduled-task healthcheck configuration invalid: {exc}"
        _emit_alert(message, logger, args.dry_run)
        return 1

    for task in tasks:
        problems = _problems(task)
        if not problems:
            continue

        message = (
            "SCHEDULED TASK HEALTHCHECK ALERT\n"
            f"Task: {task.task_name}\n"
            f"TaskPath: {task.task_path}\n"
            f"Target: {task.target_path}\n"
            f"Problems:\n- " + "\n- ".join(problems)
        )
        _emit_alert(message, logger, args.dry_run)

    if dashboard is not None:
        problems = _dashboard_problems(dashboard)
        if problems:
            message = (
                "DASHBOARD HEALTHCHECK ALERT\n"
                f"Target: {dashboard.url}\n"
                "Problems:\n- " + "\n- ".join(problems)
            )
            _emit_alert(message, logger, args.dry_run)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
