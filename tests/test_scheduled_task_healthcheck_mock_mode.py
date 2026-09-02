import json
from pathlib import Path

import pytest

from tools import scheduled_task_healthcheck as healthcheck


REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_config(tmp_path, names):
    payload = {
        "tasks": [
            {
                "task_name": name,
                "task_path": "\\",
                "target_path": str(REPO_ROOT / "tools" / "scheduled_task_healthcheck.py"),
            }
            for name in names
        ]
    }
    path = tmp_path / "healthcheck.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_default_mode_all_eight_existing_tasks_load_successfully():
    tasks, dashboard = healthcheck._load_config(
        REPO_ROOT / "config" / "scheduled_task_healthcheck.json"
    )
    assert len(tasks) == 8
    assert dashboard is not None


def test_default_mode_missing_task_still_fails(tmp_path):
    names = [
        "Kiwoom Worker - KR Mock",
        "Kiwoom Worker - US Mock",
        "Kiwoom Worker Watchdog",
        "Kiwoom Worker Watchdog",
        "Kiwoom Heartbeat Alert",
        "Kiwoom Telegram Control Bot",
        "Kiwoom Project Database Backup",
    ]
    with pytest.raises(ValueError, match="exactly the inventoried eight tasks"):
        healthcheck._load_config(_write_config(tmp_path, names))


@pytest.mark.parametrize(
    "names",
    [
        ["Kiwoom Worker Watchdog"],
        ["Kiwoom Worker Watchdog", "Kiwoom Worker KR Mock"],
        ["Kiwoom Worker Watchdog", "Kiwoom Worker KR Mock", "Kiwoom Worker US Mock"],
    ],
)
def test_mock_only_mode_accepts_watchdog_and_selected_workers(tmp_path, names):
    tasks, dashboard = healthcheck._load_config(
        _write_config(tmp_path, names), mode="mock-only"
    )
    assert [task.task_name for task in tasks] == names
    assert dashboard is None


@pytest.mark.parametrize(
    "rejected_name",
    [
        "Kiwoom Project Database Backup",
        "Kiwoom Telegram Control Bot",
        "Unexpected Task",
    ],
)
def test_mock_only_mode_rejects_non_mock_task_names(tmp_path, rejected_name):
    with pytest.raises(ValueError, match=f"{rejected_name!r}"):
        healthcheck._load_config(
            _write_config(tmp_path, ["Kiwoom Worker Watchdog", rejected_name]),
            mode="mock-only",
        )


def test_mock_only_mode_rejects_duplicate_task_names(tmp_path):
    with pytest.raises(ValueError, match="duplicate"):
        healthcheck._load_config(
            _write_config(tmp_path, ["Kiwoom Worker Watchdog", "Kiwoom Worker Watchdog"]),
            mode="mock-only",
        )
