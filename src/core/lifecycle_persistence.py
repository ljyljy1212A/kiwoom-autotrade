"""Atomic persistence helpers for per-account symbol lifecycles."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from src.core.atomic_write import atomic_write_json


def merge_symbol_lifecycle_locked(
    path: Path,
    symbol: str,
    lifecycles: dict,
    *,
    expected_present: bool,
    expected_state: object,
) -> dict:
    """Merge one symbol into the latest lifecycle file while the caller holds the account lock."""
    try:
        latest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        latest = {}
    if not isinstance(latest, dict):
        raise RuntimeError("Symbol lifecycle file is not an object")
    if (symbol in latest) != expected_present or latest.get(symbol) != expected_state:
        raise RuntimeError(f"Concurrent symbol lifecycle change for {symbol}")

    if symbol in lifecycles:
        latest[symbol] = copy.deepcopy(lifecycles[symbol])
    else:
        latest.pop(symbol, None)

    path.parent.mkdir(exist_ok=True)
    atomic_write_json(path, latest, ensure_ascii=False)
    return latest
