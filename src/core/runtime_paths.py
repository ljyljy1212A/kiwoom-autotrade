from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _resolve_path(env_name: str, default: Path) -> Path:
    raw = os.environ.get(env_name, "").strip()
    return Path(raw).expanduser() if raw else default


DATA_DIR = _resolve_path("KIWOOM_DATA_DIR", PROJECT_ROOT / "data")
LOG_DIR = _resolve_path("KIWOOM_LOG_DIR", PROJECT_ROOT / "logs")
DIAGNOSTICS_DIR = PROJECT_ROOT / "diagnostics"


def default_backup_dir() -> Path:
    if os.name == "nt":
        return Path(r"C:\Backups\ProjectDB")
    return PROJECT_ROOT / "backups" / "ProjectDB"


def backup_dir() -> Path:
    return _resolve_path("KIWOOM_BACKUP_BASE_DIR", default_backup_dir())
