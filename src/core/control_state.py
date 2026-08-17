"""Account-wide runtime control state stored as atomic JSON files."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from src.core.runtime_paths import DATA_DIR


def control_path(account_id: str, data_dir: Path | None = None) -> Path:
    root = data_dir or DATA_DIR
    return root / "control" / f"{account_id}.control.json"


def read_control_state(account_id: str, data_dir: Path | None = None) -> dict | None:
    path = control_path(account_id, data_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def read_auto_trading_enabled(account_id: str, data_dir: Path | None = None) -> bool | None:
    state = read_control_state(account_id, data_dir)
    if not isinstance(state, dict) or "auto_trading_enabled" not in state:
        return None
    return bool(state.get("auto_trading_enabled"))


def write_control_state(
    account_id: str,
    *,
    auto_trading_enabled: bool,
    updated_by: str = "telegram",
    data_dir: Path | None = None,
) -> dict:
    path = control_path(account_id, data_dir)
    payload = {
        "account": account_id,
        "auto_trading_enabled": bool(auto_trading_enabled),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": updated_by,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)
    return payload
