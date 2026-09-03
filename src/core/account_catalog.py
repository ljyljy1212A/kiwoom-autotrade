"""Shared, side-effect-free account catalog helpers.

Reads config/accounts.yaml directly. Deliberately does NOT import
dashboard_server.py, whose module-level `_load_dotenv()` mutates
`os.environ` as a process-wide side effect — this module must be safely
importable from process-control code such as worker_supervisor.py without
triggering that.
"""
from __future__ import annotations

from collections.abc import Mapping

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


class StrictBooleanLoader(yaml.SafeLoader):
    """Accept only exact lowercase YAML boolean spellings true and false."""


def _construct_strict_boolean(loader, node):
    if node.value == "true":
        return True
    if node.value == "false":
        return False
    return node.value


StrictBooleanLoader.add_constructor(
    "tag:yaml.org,2002:bool",
    _construct_strict_boolean,
)


def reconciliation_clearance_eligible(
    account: str,
    market: str,
    mode: str,
) -> bool:
    config_path = PROJECT_ROOT / "config" / "accounts.yaml"

    try:
        document = yaml.load(
            config_path.read_text(encoding="utf-8"),
            Loader=StrictBooleanLoader,
        )
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError("accounts configuration is unavailable or invalid") from exc

    if not isinstance(document, Mapping):
        raise ValueError("accounts configuration must be a mapping")

    accounts = document.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise ValueError("accounts configuration must contain a non-empty list")

    if not isinstance(account, str):
        raise ValueError("account must be a string")
    if not isinstance(market, str):
        raise ValueError("market must be a string")
    if not isinstance(mode, str):
        raise ValueError("mode must be a string")

    requested_market = market.upper()
    requested_mode = mode.lower()
    entries: list[dict[str, object]] = []
    seen_ids: set[str] = set()

    for item in accounts:
        if not isinstance(item, Mapping):
            raise ValueError("each account entry must be a mapping")

        for field in ("id", "market", "mode", "emergency_stop_eligible"):
            if field not in item:
                raise ValueError(f"account entry is missing {field}")

        account_id = item["id"]
        entry_market = item["market"]
        entry_mode = item["mode"]
        marker = item["emergency_stop_eligible"]

        if not isinstance(account_id, str) or not account_id:
            raise ValueError("account id must be a non-empty string")
        if account_id in seen_ids:
            raise ValueError(f"duplicate account id: {account_id}")
        seen_ids.add(account_id)

        if not isinstance(entry_market, str) or not entry_market:
            raise ValueError(f"invalid market for account: {account_id}")
        if not isinstance(entry_mode, str) or not entry_mode:
            raise ValueError(f"invalid mode for account: {account_id}")
        if type(marker) is not bool:
            raise ValueError(
                f"emergency_stop_eligible must be exactly true or false "
                f"for account: {account_id}"
            )

        normalized_market = entry_market.upper()
        normalized_mode = entry_mode.lower()

        if marker and normalized_mode != "mock":
            raise ValueError(
                f"eligible account must use mock mode: {account_id}"
            )

        entries.append(
            {
                "id": account_id,
                "market": normalized_market,
                "mode": normalized_mode,
                "emergency_stop_eligible": marker,
            }
        )

    selected = next((item for item in entries if item["id"] == account), None)
    if selected is None:
        raise ValueError(f"requested account not found: {account}")

    if selected["mode"] != "mock":
        raise ValueError(f"real account is not eligible: {account}")

    if selected["market"] != requested_market:
        raise ValueError(f"market mismatch for account: {account}")

    if selected["mode"] != requested_mode:
        raise ValueError(f"mode mismatch for account: {account}")

    return selected["emergency_stop_eligible"]
