"""Account-wide reconciliation failure coordination."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.core.symbol_keys import canonical_symbol_key
from src.core.us_market import normalize_us_holdings, us_balance_recognized

if TYPE_CHECKING:
    from src.core.engine import AccountEngine


def _number(value) -> float:
    try:
        return abs(float(str(value).replace(",", "").replace("+", "").strip()))
    except (TypeError, ValueError):
        return 0.0


def _same_balance_symbol(market: str, value, symbol: str) -> bool:
    return canonical_symbol_key(market, value) == canonical_symbol_key(market, symbol)


def _balance_holding(market: str, data: dict, symbol: str) -> tuple[float, float] | None:
    """Select one symbol from a broker balance response."""
    saw_holdings = False
    for key in ("acnt_evlt_remn_indv_tot", "stk_cntr_remn", "acnt_bal", "result_list", "result_lsit", "holdings"):
        rows = data.get(key)
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            continue
        saw_holdings = True
        for row in rows:
            if _same_balance_symbol(market, row.get("stk_cd", ""), symbol):
                for qty_key in ("rmnd_qty", "poss_qty", "hold_qty", "cur_qty", "qty"):
                    if qty_key in row:
                        avg = next((_number(row[k]) for k in ("buy_uv", "avg_prc", "pur_pric") if k in row), 0.0)
                        return _number(row[qty_key]), avg
    if saw_holdings:
        return (0.0, 0.0)

    def walk(value):
        if isinstance(value, dict):
            code = next((value.get(k) for k in ("stk_cd", "symbol", "code") if value.get(k) is not None), None)
            if code is not None and _same_balance_symbol(market, code, symbol):
                qty_key = next((k for k in ("rmnd_qty", "cur_qty", "poss_qty", "hold_qty", "qty", "setl_remn") if k in value), None)
                if qty_key:
                    avg = next((_number(value[k]) for k in ("buy_uv", "avg_prc", "pur_pric", "cntr_uv") if k in value), 0.0)
                    return _number(value[qty_key]), avg
            for child in value.values():
                found = walk(child)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = walk(child)
                if found is not None:
                    return found
        return None

    found = walk(data)
    if found is not None:
        return found
    return None


def _holding_summary(data: dict) -> str:
    """Safe diagnostic: structure/field names only, never account credentials."""
    rows = data.get("acnt_evlt_remn_indv_tot")
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return f"acnt_evlt_remn_indv_tot={type(rows).__name__}"
    if not rows:
        return "acnt_evlt_remn_indv_tot=[]"
    first = rows[0] if isinstance(rows[0], dict) else {}
    codes = [str(r.get("stk_cd", "")).strip() for r in rows if isinstance(r, dict)]
    return f"rows={len(rows)}, codes={codes}, row_fields={sorted(first.keys())}"


def _kr_balance_recognized(data: dict) -> bool:
    """Whether a domestic balance response contains an authoritative rows field."""
    return any(key in data for key in ("acnt_evlt_remn_indv_tot", "stk_cntr_remn", "acnt_bal", "result_list", "holdings"))


def _all_balance_holdings(market: str, data: dict) -> list[dict]:
    """Map the authoritative Kiwoom holdings list for dashboard display."""
    rows = data.get("acnt_evlt_remn_indv_tot", [])
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return []
    holdings = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("stk_cd"):
            continue
        qty = _number(row.get("rmnd_qty"))
        if qty <= 0:
            continue
        holdings.append({
            "symbol": canonical_symbol_key(market, row["stk_cd"]),
            "name": str(row.get("stk_nm", "")).strip(),
            "qty": qty,
            "avgPrice": _number(row.get("pur_pric")),
            "currentPrice": _number(row.get("cur_prc")),
            "prevClose": _number(row.get("pred_close_pric")),
        })
    return holdings


@dataclass(frozen=True)
class _NormalizedBrokerBalance:
    holdings: list[dict]
    recognized: bool


def _normalize_broker_balance(market: str, data: dict) -> _NormalizedBrokerBalance:
    """Return the common holdings shape and authoritative-response marker."""
    if market == "US":
        return _NormalizedBrokerBalance(
            holdings=normalize_us_holdings(data),
            recognized=us_balance_recognized(data),
        )
    return _NormalizedBrokerBalance(
        holdings=_all_balance_holdings(market, data),
        recognized=_kr_balance_recognized(data),
    )


class _ReconciliationCoordinator:
    """Keep account-wide manual reconciliation state and fail-closed propagation together."""

    def record_failure(self, engine: "AccountEngine", exc: Exception) -> None:
        gate = engine._balance_gate
        if gate.reconciliation_mode != "manual":
            return
        gate.reconciliation_failure_count += 1
        engine.ctx.logger.warning(
            "Broker reconciliation unavailable: "
            f"consecutive_cycle_failures={gate.reconciliation_failure_count}; {exc}"
        )
        if gate.reconciliation_failure_count < gate.reconciliation_failure_threshold:
            return
        for account_engine in list(gate.engines):
            if not account_engine._pause_reason or account_engine._pause_reason == "broker_reconciliation_unavailable":
                account_engine._trading_paused = True
                account_engine._pause_reason = "broker_reconciliation_unavailable"

    def record_success(self, engine: "AccountEngine") -> None:
        gate = engine._balance_gate
        if gate.reconciliation_mode == "manual":
            gate.reconciliation_failure_count = 0
