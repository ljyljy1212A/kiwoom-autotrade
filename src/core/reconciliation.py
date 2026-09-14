"""Account-wide reconciliation failure coordination."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.engine import AccountEngine


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
