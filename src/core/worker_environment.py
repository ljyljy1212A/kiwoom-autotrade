"""Load a routed mock worker's settings without replacing its launch contract."""
from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv

from src.core.account_catalog import account_catalog
from src.core.runtime_paths import RUNTIME_ROOT


_LAUNCH_KEYS = (
    "KIWOOM_RUNTIME_ROOT", "KIWOOM_DATA_DIR", "KIWOOM_LOG_DIR",
    "KIWOOM_DIAGNOSTICS_DIR", "KIWOOM_BACKUP_BASE_DIR",
    "KIWOOM_EXPECTED_WORKER_ROOT", "KIWOOM_EXPECTED_WORKER_REVISION",
    "ACCOUNT_FILTER", "MARKET_INSTANCE", "KIWOOM_SUPERVISOR_LAUNCH_ID",
    "KIWOOM_ENV", "AUTO_TRADING_ENABLED", "PRICE_FEED_MODE",
    "TELEGRAM_APPROVAL_REQUIRED",
    "PYTHONPATH",
)


def validate_routed_account(account: str | None, market: str | None) -> None:
    selected = [item for item in account_catalog() if item["id"] == "kr_mock"]
    if (account != "kr_mock" or market != "KR"
            or len(selected) != 1 or selected[0]["market"] != "KR"
            or selected[0]["mode"] != "mock"):
        raise RuntimeError("routed worker account configuration must be kr_mock / KR / mock")


def load_worker_environment() -> None:
    if not os.environ.get("KIWOOM_RUNTIME_ROOT", "").strip():
        load_dotenv(override=True)
        return

    launch = {key: os.environ.get(key) for key in _LAUNCH_KEYS}
    if any(value is None or not value.strip() for value in launch.values()):
        raise RuntimeError("routed worker launch environment is incomplete")
    if (launch["ACCOUNT_FILTER"] != "kr_mock"
            or launch["MARKET_INSTANCE"] != "KR"
            or launch["KIWOOM_ENV"] != "mock"):
        raise RuntimeError("routed worker launch must be kr_mock / KR / mock")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", launch["KIWOOM_EXPECTED_WORKER_REVISION"]):
        raise RuntimeError("routed worker expected source revision is invalid")
    try:
        expected_source_root = Path(launch["KIWOOM_EXPECTED_WORKER_ROOT"]).resolve(strict=True)
        python_source_root = Path(launch["PYTHONPATH"]).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError("routed worker expected source root is unavailable") from exc
    if not expected_source_root.is_dir() or python_source_root != expected_source_root:
        raise RuntimeError("routed worker source path differs from its expected root")
    if Path.cwd().resolve() != RUNTIME_ROOT:
        raise RuntimeError("routed worker working directory differs from runtime root")
    for key in (
        "KIWOOM_DATA_DIR", "KIWOOM_LOG_DIR", "KIWOOM_DIAGNOSTICS_DIR",
        "KIWOOM_BACKUP_BASE_DIR",
    ):
        if not Path(launch[key]).is_absolute():
            raise RuntimeError("routed worker state paths must be absolute")
    dotenv_path = RUNTIME_ROOT / ".env"
    if not dotenv_path.is_file():
        raise RuntimeError("routed worker runtime environment file is unavailable")
    try:
        load_dotenv(dotenv_path=dotenv_path, override=True)
    finally:
        # Settings may rotate credentials, but cannot retarget this launch.
        os.environ.update(launch)
