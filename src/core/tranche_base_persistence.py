"""Locked persistence helpers for validated per-symbol tranche bases."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path


JsonWriter = Callable[..., None]


def _read_latest(path: Path) -> dict:
    try:
        latest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return latest if isinstance(latest, dict) else {}


def store_tranche_base_locked(
    path: Path,
    symbol: str,
    price: float,
    *,
    only_if_absent: bool,
    write_json: JsonWriter,
) -> dict:
    """Merge one validated base while the caller holds both existing locks."""
    latest = _read_latest(path)
    if only_if_absent and symbol in latest:
        return latest
    if not only_if_absent and latest.get(symbol) == price:
        return latest

    latest[symbol] = price
    path.parent.mkdir(exist_ok=True)
    write_json(path, latest, ensure_ascii=False)
    return latest


def remove_tranche_base_locked(
    path: Path,
    symbol: str,
    *,
    write_json: JsonWriter,
) -> dict:
    """Remove one base while the caller holds both existing locks."""
    latest = _read_latest(path)
    latest.pop(symbol, None)
    path.parent.mkdir(exist_ok=True)
    write_json(path, latest, ensure_ascii=False)
    return latest
