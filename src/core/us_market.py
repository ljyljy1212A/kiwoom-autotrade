"""US-market validation and broker-response normalization.

Kiwoom uses different field names between domestic and US TRs.  This module
turns only documented/sanctioned US response fixtures into the small common
shape used by the engine; unknown responses remain unrecognized rather than
being mistaken for an empty position.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_DOWN
import re
from typing import Any


US_EXCHANGES = {"ND", "NY", "NA"}
US_ORDER_TYPES = {"00", "03", "26", "27", "30", "36", "37"}
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,11}$")


def normalize_us_symbol(symbol: str) -> str:
    value = str(symbol or "").strip().upper()
    if value.startswith("A") and value[1:].isdigit():
        raise ValueError("US symbols must not use the Korean A-prefixed ticker format")
    if not _SYMBOL_RE.fullmatch(value):
        raise ValueError(f"Invalid US ticker: {symbol!r}")
    return value


def validate_us_order(symbol: str, exchange: str, qty: float, price: float | None, order_type: str) -> str:
    symbol = normalize_us_symbol(symbol)
    if str(exchange).upper() not in US_EXCHANGES:
        raise ValueError(f"Invalid US exchange {exchange!r}; use ND, NY, or NA")
    if str(order_type) not in US_ORDER_TYPES:
        raise ValueError(f"Unsupported US order type {order_type!r}")
    if Decimal(str(qty)) != Decimal(str(qty)).to_integral_value():
        raise ValueError("US fractional-share orders are disabled until broker support is verified")
    if qty <= 0:
        raise ValueError("Order quantity must be positive")
    if str(order_type) in {"00", "26", "27", "30"} and (price is None or price <= 0):
        raise ValueError("This US order type requires a positive limit price")
    return symbol


def usd_price(value: Any) -> float:
    """Parse and round a broker USD price without binary-float drift."""
    try:
        number = Decimal(str(value).replace(",", "").replace("+", "").strip())
        return float(abs(number).quantize(Decimal("0.0001"), rounding=ROUND_DOWN))
    except (InvalidOperation, ValueError):
        return 0.0


def number(value: Any) -> float:
    return usd_price(value)


def _first(row: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _rows(data: dict, keys: tuple[str, ...]) -> list[dict]:
    for key in keys:
        value = data.get(key)
        if isinstance(value, dict):
            return [value]
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def normalize_us_holdings(data: dict) -> list[dict]:
    """Return US broker holdings in the dashboard/engine common shape.

    The aliases cover Kiwoom's US account TR variants.  A response that has no
    recognizable holding list deliberately returns [] so callers can preserve
    state instead of treating it as an authoritative empty balance.
    """
    rows = _rows(data, ("acnt_evlt_remn_indv_tot", "stk_cntr_remn", "result_list", "holdings", "acnt_bal"))
    holdings = []
    for row in rows:
        raw_symbol = _first(row, ("stk_cd", "ovrs_pdno", "ovrs_item_cd", "symbol", "code"))
        if raw_symbol is None:
            continue
        try:
            symbol = normalize_us_symbol(str(raw_symbol))
        except ValueError:
            continue
        qty = number(_first(row, ("rmnd_qty", "ovrs_cblc_qty", "poss_qty", "hold_qty", "qty", "cur_qty")))
        if qty <= 0:
            continue
        holdings.append({
            "symbol": symbol,
            "name": str(_first(row, ("stk_nm", "frgn_stk_nm", "ovrs_item_name", "symbol_name", "name")) or "").strip(),
            "qty": qty,
            "avgPrice": usd_price(_first(row, ("buy_uv", "frgn_stk_book_uv", "avg_prc", "pur_pric", "pchs_avg_pric"))),
            "currentPrice": usd_price(_first(row, ("cur_prc", "now_pric", "ovrs_now_pric", "last_price", "price"))),
            "prevClose": usd_price(_first(row, ("pred_close_pric", "ovrs_pred_pre", "prev_close", "base_price"))),
            "currency": "USD",
        })
    return holdings


def us_balance_recognized(data: dict) -> bool:
    return any(key in data for key in ("acnt_evlt_remn_indv_tot", "stk_cntr_remn", "result_list", "holdings", "acnt_bal"))


def normalize_us_execution_rows(data: dict) -> list[dict]:
    rows = _rows(data, ("cntr", "result_list", "acnt_ord_cntr_prps_dtl", "ord_cntr_list", "ordr_cntr"))
    normalized = []
    for row in rows:
        ord_no = _first(row, ("ord_no", "odno", "ordr_no"))
        if not ord_no:
            continue
        normalized.append({
            "ord_no": str(ord_no),
            "cntr_qty": number(_first(row, ("cntr_qty", "tot_ccld_qty", "exec_qty", "filled_qty"))),
            "cntr_pric": usd_price(_first(row, ("cntr_pric", "cntr_uv", "exec_price", "filled_price"))),
            "ord_dt": _first(row, ("ord_dt", "ordr_dt", "cntr_dt", "exec_date")) or "",
        })
    return normalized


def extract_us_fx_rate(data: dict) -> float | None:
    value = _first(data, ("aplc_exrt", "buy_aplc_exrt", "sell_aplc_exrt", "exrt", "exrt_pric", "fx_rate", "base_exrt"))
    rate = number(value)
    return rate if rate > 0 else None
