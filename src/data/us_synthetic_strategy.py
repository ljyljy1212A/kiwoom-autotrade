"""Read-only single-tranche projection for synthetic US mock fixtures.

No Engine wiring, strategy mutation, order generation or operational authority.
Targets are unrounded modeled prices, not executable quotes or settlement.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from fractions import Fraction

from src.data.us_cumulative_execution import _decimal
from src.data.us_synthetic_ledger import restore_synthetic_tranche_cost


@dataclass(frozen=True)
class SyntheticStrategyProjection:
    account_id: str
    market: str
    symbol: str
    step: int
    lifecycle_id: str
    buy_order_uid: str | None
    quantity: int
    remaining_gross_cost: Fraction
    realized_gross_profit: Fraction
    entry_reference_price: Fraction | None
    average_gross_cost: Fraction | None
    next_buy_trigger: Fraction | None
    sell_target_price: Fraction | None
    applied_fill_count: int
    operational_trading_allowed: bool = False


def project_synthetic_strategy(
    db: sqlite3.Connection, *, account_id: str, market: str, symbol: str,
    step: int, lifecycle_id: str, drop_pct: str | None,
    profit_pct: str, commission_rate: str,
) -> SyntheticStrategyProjection:
    """Project one explicit tranche from a validated read snapshot.

    drop_pct=None means no next-buy transition for this tranche. The caller
    supplies the intended tranche; this reader never chooses an active step.
    Missing/corrupt records refuse through the ledger reader without repair.
    """
    parameters = _projection_parameters(
        account_id, market, symbol, step, lifecycle_id, drop_pct, profit_pct, commission_rate,
    )
    state = restore_synthetic_tranche_cost(db, symbol, step, lifecycle_id)
    return _project_tranche_state(state, account_id, market, symbol, step, lifecycle_id, parameters)


def _projection_parameters(account_id, market, symbol, step, lifecycle_id,
                           drop_pct, profit_pct, commission_rate):
    if account_id != "us_mock" or market != "US":
        raise ValueError("Explicit US mock projection scope is required")
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,11}", symbol):
        raise ValueError("Explicit canonical US symbol is required")
    if type(step) is not int or step < 1:
        raise ValueError("Explicit positive tranche is required")
    if not isinstance(lifecycle_id, str) or not lifecycle_id.strip():
        raise ValueError("Explicit lifecycle is required")
    profit = Fraction(_decimal(profit_pct, "profit percentage")) / 100
    fee = Fraction(_decimal(commission_rate, "commission rate"))
    if fee >= 1:
        raise ValueError("Commission rate must be less than one")
    drop = None
    if drop_pct is not None:
        if not isinstance(drop_pct, str):
            raise ValueError("Drop percentage must be a decimal string")
        magnitude = drop_pct[1:] if drop_pct.startswith("-") else drop_pct
        drop = Fraction(_decimal(magnitude, "drop percentage")) / 100
        if drop_pct.startswith("-"):
            drop = -drop
        if not -1 < drop <= 0:
            raise ValueError("Drop percentage must be greater than -100 and at most zero")
    return drop, profit, fee


def _project_tranche_state(state, account_id, market, symbol, step, lifecycle_id, parameters):
    drop, profit, fee = parameters
    reference, cost = state.entry_reference_price, state.average_gross_cost
    trigger = reference * (1 + drop) if reference is not None and drop is not None else None
    target = cost * (1 + fee) * (1 + profit) / (1 - fee) if cost is not None else None
    return SyntheticStrategyProjection(
        account_id, market, symbol, step, lifecycle_id, state.buy_order_uid,
        state.quantity, state.remaining_gross_cost, state.realized_gross_profit,
        reference, cost, trigger, target, state.fills,
    )
