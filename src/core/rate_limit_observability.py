"""Safe, structured observability for already-handled Kiwoom throttles."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


_QUOTA_TIERS = ("1700", "1701", "1702")


def classify_quota_tier(error_text: str = "") -> str:
    """Classify documented quota tiers from the existing exception text."""
    text = str(error_text)
    for tier in _QUOTA_TIERS:
        if tier in text:
            return tier
    return "none"


def emit_rate_limit_event(
    logger,
    *,
    market: str,
    mode: str,
    account_id: str,
    appkey: str,
    api_id: str | None = None,
    return_code: Any = None,
    error_text: str = "",
    trigger: str,
    cooldown_sec: float | None = None,
) -> dict[str, Any]:
    """Emit one redacted rate-limit event and return its structured fields."""
    event = {
        "event": "kiwoom_rate_limit_event",
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "market": market,
        "mode": mode,
        "account_id": account_id,
        "api_id": api_id,
        "status_code": return_code,
        "quota_tier": classify_quota_tier(error_text),
        "trigger": trigger,
        "cooldown_sec": cooldown_sec,
        "appkey_fingerprint": hashlib.sha256(str(appkey).encode("utf-8")).hexdigest()[:12],
    }
    if logger is not None:
        structured_message = json.dumps(event, ensure_ascii=False, sort_keys=True)
        if hasattr(logger, "bind"):
            logger.bind(**event).warning(f"Kiwoom rate-limit event {structured_message}")
        else:
            logger.warning(f"Kiwoom rate-limit event {structured_message}")
    return event
