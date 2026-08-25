from __future__ import annotations

import re


_KR_PREFIXED_CASH_EQUITY = re.compile(r"A\d{6}\Z")


def canonical_symbol_key(market: str, symbol: object) -> str:
    """Return the shared market-aware key for runtime symbol state."""
    value = str(symbol or "").strip().upper()
    if str(market).upper() == "KR" and _KR_PREFIXED_CASH_EQUITY.fullmatch(value):
        return value[1:]
    return value


def legacy_symbol_key(symbol: object) -> str:
    """Return the historical unsafe key solely to identify migration candidates."""
    return str(symbol or "").strip().upper().lstrip("A")
