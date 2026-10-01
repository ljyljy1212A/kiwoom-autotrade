from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def runtime_root(default: Path = PROJECT_ROOT) -> Path:
    raw = os.environ.get("KIWOOM_RUNTIME_ROOT", "").strip()
    if not raw:
        return default
    root = Path(raw)
    if not root.is_absolute():
        raise ValueError("KIWOOM_RUNTIME_ROOT must be absolute")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("KIWOOM_RUNTIME_ROOT must be a directory")
    return root


RUNTIME_ROOT = runtime_root()


def _resolve_path(env_name: str, default: Path) -> Path:
    raw = os.environ.get(env_name, "").strip()
    return Path(raw).expanduser() if raw else default


DATA_DIR = _resolve_path("KIWOOM_DATA_DIR", RUNTIME_ROOT / "data")
LOG_DIR = _resolve_path("KIWOOM_LOG_DIR", RUNTIME_ROOT / "logs")
DIAGNOSTICS_DIR = _resolve_path("KIWOOM_DIAGNOSTICS_DIR", RUNTIME_ROOT / "diagnostics")


def default_backup_dir() -> Path:
    if os.name == "nt":
        return Path(r"C:\Backups\ProjectDB")
    return RUNTIME_ROOT / "backups" / "ProjectDB"


def backup_dir() -> Path:
    return _resolve_path("KIWOOM_BACKUP_BASE_DIR", default_backup_dir())
