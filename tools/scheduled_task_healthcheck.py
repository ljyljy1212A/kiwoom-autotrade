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


@dataclass(frozen=True)
class TaskSpec:
    task_name: str
    task_path: str
    target_path: Path


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


def _load_tasks(config_path: Path) -> list[TaskSpec]:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("healthcheck config must be a JSON list")

    tasks: list[TaskSpec] = []
    for item in payload:
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
    if actual_names != EXPECTED_TASK_NAMES:
        raise ValueError(
            "healthcheck config must contain exactly the inventoried eight "
            f"tasks; expected={dict(EXPECTED_TASK_NAMES)!r} "
            f"actual={dict(actual_names)!r}"
        )
    return tasks


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--log-path", type=Path, default=DEFAULT_LOG_PATH)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="log alert messages without sending them to ntfy",
    )
    args = parser.parse_args()

    logger = _logger(args.log_path)
    try:
        tasks = _load_tasks(args.config)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        message = f"Scheduled-task healthcheck configuration invalid: {exc}"
        logger.error(message)
        if args.dry_run:
            logger.error("[DRY-RUN] would alert: %s", message)
        else:
            _send_alert(message, logger)
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
        logger.error(message)
        if args.dry_run:
            logger.error("[DRY-RUN] would alert: %s", message)
        else:
            _send_alert(message, logger)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
