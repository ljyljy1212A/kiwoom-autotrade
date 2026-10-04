"""Validate ust21100 transaction evidence without inferring an order link."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import re

from src.core.us_market import US_EXCHANGES, normalize_us_symbol


def validate_us_history_scope(start_date: str, end_date: str, symbol: str, exchange: str) -> str:
    """Require explicit dates, ticker and exchange before any broker work."""
    for value in (start_date, end_date):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{8}", value):
            raise ValueError("US history dates must be YYYYMMDD")
        try:
            datetime.strptime(value, "%Y%m%d")
        except ValueError as exc:
            raise ValueError("US history dates must be YYYYMMDD") from exc
    if start_date > end_date:
        raise ValueError("US history start date exceeds end date")
    if not isinstance(symbol, str):
        raise ValueError("US history requires a ticker")
    ticker = normalize_us_symbol(symbol)
    if not isinstance(exchange, str) or exchange not in US_EXCHANGES:
        raise ValueError("US history requires exchange ND, NY, or NA")
    return ticker


def _identifier(value, *, optional: bool = False) -> str:
    if optional and value in ("", "000000000"):
        return ""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{9}", value) or value == "000000000":
        raise ValueError("Invalid US transaction identifier")
    return value


def _positive_decimal(value) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(r"[+]?[0-9]+(?:\.[0-9]+)?", value):
        raise ValueError("Invalid US transaction quantity or price")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Invalid US transaction quantity or price") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError("US transaction quantity and price must be positive")
    return result


def normalize_us_transaction_evidence(data: dict) -> list[dict]:
    """Return validated transaction candidates, never order-attributed fills.

    deal_no and orig_deal_no are transaction identifiers. Numeric equality
    with an order number does not establish that they share an ID namespace.
    No transaction date or price from this adapter can bypass the Engine gate.
    """
    if not isinstance(data, dict) or data.get("_transaction_pages_complete") is not True:
        raise ValueError("US transaction pages are incomplete")
    start, end = data.get("_query_start_date"), data.get("_query_end_date")
    symbol = validate_us_history_scope(start, end, data.get("_query_symbol"), data.get("_query_exchange"))
    rows = data.get("result_list")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Invalid US transaction list")
    seen: dict[tuple[str, str], dict] = {}
    normalized = []
    for row in rows:
        date = row.get("deal_dt")
        validate_us_history_scope(date, date, symbol, data["_query_exchange"])
        if not start <= date <= end:
            raise ValueError("US transaction date is outside query range")
        if not isinstance(row.get("stk_cd"), str) or normalize_us_symbol(row["stk_cd"]) != symbol:
            raise ValueError("US transaction ticker conflicts with query")
        if row.get("crnc_code") != "USD":
            raise ValueError("US trade evidence requires USD")
        transaction_no = _identifier(row.get("deal_no"))
        original_no = _identifier(row.get("orig_deal_no"), optional=True)
        quantity, price = _positive_decimal(row.get("deal_qty")), _positive_decimal(row.get("uv_exrt"))
        key = (date, transaction_no)
        if key in seen:
            if seen[key] != row:
                raise ValueError("Conflicting US transaction identity across pages")
            continue
        seen[key] = dict(row)
        normalized.append({
            "source_api": "ust21100",
            "transaction_date": date,
            "transaction_no": transaction_no,
            "original_transaction_no": original_no,
            "symbol": symbol,
            "quantity": quantity,
            "unit_price": price,
            "order_identity_status": "unresolved",
            "raw": dict(row),
        })
    return normalized
