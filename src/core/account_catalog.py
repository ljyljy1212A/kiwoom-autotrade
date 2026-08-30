"""Shared, side-effect-free account catalog helpers.

Reads config/accounts.yaml directly. Deliberately does NOT import
dashboard_server.py, whose module-level `_load_dotenv()` mutates
`os.environ` as a process-wide side effect — this module must be safely
importable from process-control code such as worker_supervisor.py without
triggering that.
"""
from __future__ import annotations

import yaml

from src.core.runtime_paths import PROJECT_ROOT


def account_catalog() -> list[dict]:
    """Public account metadata only; secrets never leave the caller's process."""
    try:
        raw = yaml.safe_load(
            (PROJECT_ROOT / "config" / "accounts.yaml").read_text(encoding="utf-8")
        ) or {}
    except (OSError, yaml.YAMLError):
        return []
    return [
        {
            "id": str(item.get("id", "")),
            "displayName": str(item.get("display_name", item.get("id", ""))),
            "market": str(item.get("market", "")).upper(),
            "mode": str(item.get("mode", "mock")).lower(),
        }
        for item in raw.get("accounts", [])
        if item.get("id")
    ]


def is_real_account(account_id: str) -> bool:
    """True if `account_id` is catalogued with mode: real. Unknown IDs are treated as not-real (fail toward matching existing default-mock behavior elsewhere, not toward blocking unknown/test account IDs)."""
    catalog = {item["id"]: item for item in account_catalog()}
    return catalog.get(account_id, {}).get("mode") == "real"
