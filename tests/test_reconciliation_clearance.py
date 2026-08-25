import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.core.engine import (
    AccountEngine,
    _manual_tranche_allocation,
    NormalizedBalanceHolding,
    ReconciliationClearanceSnapshot,
    ReconciliationIncompleteReason,
    evaluate_reconciliation_clearance,
    with_unattributed_collision_order_ids,
)
from src.data.order_attempts import OrderAttemptStore


def _clear_snapshot(**changes):
    snapshot = ReconciliationClearanceSnapshot(
        account_id="us_mock",
        symbol="SOXL",
        balance_api_id="ust21070",
        balance_fetched_fresh=True,
        balance_from_shared_cache=False,
        balance_recognized=True,
        holding=NormalizedBalanceHolding("SOXL", 0.0, 0.0),
    )
    return replace(snapshot, **changes)


def test_clearance_passes_for_fresh_recognized_authoritative_zero_holding():
    result = evaluate_reconciliation_clearance(_clear_snapshot())

    assert result.cleared is True
    assert result.failures == ()


def test_snapshot_wiring_has_no_unattributed_orders_when_store_is_empty(tmp_path):
    snapshot = with_unattributed_collision_order_ids(
        _clear_snapshot(account_id="account-a"), tmp_path
    )

    assert snapshot.unattributed_collision_order_ids == ()
    assert evaluate_reconciliation_clearance(snapshot).cleared is True


def test_snapshot_wiring_is_account_scoped_and_fails_condition_five(tmp_path):
    account_a = OrderAttemptStore(tmp_path / "order_attempts_account-a.db", "account-a")
    account_b = OrderAttemptStore(tmp_path / "order_attempts_account-b.db", "account-b")
    try:
        own_attempt = account_a.record_attempt("BUY", "SOXL", 1, 10.0, "00")
        other_attempt = account_b.record_attempt("BUY", "SOXL", 1, 10.0, "00")
        account_a.mark_unattributed(own_attempt.attempt_id)
        account_b.mark_unattributed(other_attempt.attempt_id)

        snapshot = with_unattributed_collision_order_ids(
            _clear_snapshot(account_id="account-a"), tmp_path
        )

        assert snapshot.unattributed_collision_order_ids == (own_attempt.attempt_id,)
        assert [failure.condition for failure in evaluate_reconciliation_clearance(snapshot).failures] == [5]
    finally:
        account_a.close()
        account_b.close()


def test_clearance_rejects_shared_balance_cache():
    result = evaluate_reconciliation_clearance(_clear_snapshot(balance_from_shared_cache=True))

    assert [failure.condition for failure in result.failures] == [1]
    assert result.failures[0].detail == "condition 1: requires a fresh, non-cached ust21070 balance response"


def test_clearance_rejects_unusable_target_holding():
    result = evaluate_reconciliation_clearance(
        _clear_snapshot(holding=NormalizedBalanceHolding("SOXL", 1.0, 0.0))
    )

    assert [failure.condition for failure in result.failures] == [2]
    assert result.failures[0].detail == "condition 2: positive target holding has no usable average price"


def test_clearance_rejects_genuinely_mismatched_symbol():
    result = evaluate_reconciliation_clearance(
        _clear_snapshot(holding=NormalizedBalanceHolding("MSFT", 0.0, 0.0))
    )

    assert [failure.condition for failure in result.failures] == [2]
    assert result.failures[0].detail == "condition 2: normalized holding symbol does not match the target symbol"


def test_clearance_does_not_strip_a_from_symbol_comparison():
    result = evaluate_reconciliation_clearance(
        _clear_snapshot(symbol="APL", holding=NormalizedBalanceHolding("AAPL", 0.0, 0.0))
    )

    assert [failure.condition for failure in result.failures] == [2]


def test_clearance_rejects_each_reconcile_incomplete_path():
    for reason in ReconciliationIncompleteReason:
        result = evaluate_reconciliation_clearance(_clear_snapshot(incomplete_reasons=frozenset({reason})))

        assert [failure.condition for failure in result.failures] == [3]
        assert reason.value in result.failures[0].detail


def test_clearance_rejects_unresolved_pending_or_recovery_order():
    result = evaluate_reconciliation_clearance(_clear_snapshot(unresolved_order_ids=("ORD-1", "ORD-2")))

    assert [failure.condition for failure in result.failures] == [4]
    assert result.failures[0].detail == "condition 4: 2 pending/recovery order(s) unresolved: ORD-1, ORD-2"


def test_clearance_rejects_unattributed_collision_order_marker():
    result = evaluate_reconciliation_clearance(
        _clear_snapshot(unattributed_collision_order_ids=("ATTEMPT-1",))
    )

    assert [failure.condition for failure in result.failures] == [5]
    assert result.failures[0].detail == "condition 5: unattributed collision-period order(s) unresolved: ATTEMPT-1"


def test_clearance_names_all_simultaneous_failures():
    result = evaluate_reconciliation_clearance(
        _clear_snapshot(
            balance_from_shared_cache=True,
            incomplete_reasons=frozenset({ReconciliationIncompleteReason.BROKER_FILL_CATCHUP}),
            unresolved_order_ids=("ORD-1",),
            unattributed_collision_order_ids=("ATTEMPT-1",),
        )
    )

    assert result.cleared is False
    assert [failure.condition for failure in result.failures] == [1, 3, 4, 5]


class _Ledger:
    def __init__(self, pending=(), recovery=(), open_qty=0):
        self.pending = list(pending)
        self.recovery = list(recovery)
        self.open_qty = open_qty

    def pending_orders(self, _symbol):
        return self.pending

    def execution_recovery_orders(self, _symbol):
        return self.recovery

    def open_tranche_qty(self, _symbol, _step):
        return self.open_qty


def _classifier_engine(*, symbol="SOXL", pending=(), recovery=(), catchup=None, lifecycle=None, pause_reason="", market="US"):
    engine = object.__new__(AccountEngine)
    engine.ctx = SimpleNamespace(
        position=SimpleNamespace(qty=1.0),
        strategy=SimpleNamespace(max_step=3),
        client=SimpleNamespace(market=market),
    )
    engine.ledger = _Ledger(pending, recovery)
    engine._broker_fill_catchup_qty = {symbol: catchup} if catchup is not None else {}
    engine._symbol_lifecycles = lifecycle or {}
    engine._pause_reason = pause_reason
    return engine


def test_incomplete_classifier_maps_all_documented_reasons():
    holding = NormalizedBalanceHolding("SOXL", 1.0, 10.0)
    assert _classifier_engine()._reconciliation_incomplete_reasons(
        "SOXL", balance_recognized=False, holding=None, qty=0,
    ) == frozenset({ReconciliationIncompleteReason.UNRECOGNIZED_BALANCE})
    assert ReconciliationIncompleteReason.BROKER_FILL_CATCHUP in _classifier_engine(catchup=2)._reconciliation_incomplete_reasons(
        "SOXL", balance_recognized=True, holding=holding, qty=1,
    )
    assert ReconciliationIncompleteReason.PENDING_QUANTITY_DEFERRAL in _classifier_engine(pending=[object()])._reconciliation_incomplete_reasons(
        "SOXL", balance_recognized=True, holding=holding, qty=2,
    )
    assert ReconciliationIncompleteReason.STALE_LIFECYCLE_HOLD in _classifier_engine(
        lifecycle={"SOXL": {"status": "open", "manual_qty": 2}},
    )._reconciliation_incomplete_reasons("SOXL", balance_recognized=True, holding=holding, qty=1)
    assert ReconciliationIncompleteReason.UNATTRIBUTED_QUANTITY_PAUSE in _classifier_engine(
        pause_reason="broker_quantity_unattributed",
    )._reconciliation_incomplete_reasons("SOXL", balance_recognized=True, holding=holding, qty=1)
    assert ReconciliationIncompleteReason.TRANCHE_REBUILD_AMBIGUOUS in _classifier_engine()._reconciliation_incomplete_reasons(
        "SOXL", balance_recognized=True, holding=holding, qty=2,
        known_tranche_qty=3, open_rows=[(2, 1.0, 10.0)],
    )


def test_incomplete_classifier_preserves_a_prefixed_us_map_keys():
    holding = NormalizedBalanceHolding("AAPL", 1.0, 10.0)
    reasons = _classifier_engine(
        symbol="AAPL", catchup=2,
        lifecycle={"AAPL": {"status": "open", "manual_qty": 2}},
    )._reconciliation_incomplete_reasons(
        "AAPL", balance_recognized=True, holding=holding, qty=1,
    )
    assert ReconciliationIncompleteReason.BROKER_FILL_CATCHUP in reasons
    assert ReconciliationIncompleteReason.STALE_LIFECYCLE_HOLD in reasons


def test_clearance_snapshot_preserves_a_prefixed_us_lifecycle_key(tmp_path):
    engine = _classifier_engine(
        symbol="AAPL", lifecycle={"AAPL": {"status": "open", "manual_qty": 2}},
    )
    engine.ctx.account_id = "us_mock"
    engine.ctx.client.get_balance = AsyncMock(return_value={"result_list": [{
        "ovrs_pdno": "AAPL", "ovrs_cblc_qty": "1", "pchs_avg_pric": "10",
    }]})
    engine.ctx.strategy.step_qty = {}
    engine.data_dir = tmp_path

    snapshot = asyncio.run(engine._build_reconciliation_clearance_snapshot("AAPL", max_balance_age_sec=0))

    assert ReconciliationIncompleteReason.STALE_LIFECYCLE_HOLD in snapshot.incomplete_reasons


def test_unresolved_order_ids_are_deduplicated_nonempty_and_sorted():
    orders = [SimpleNamespace(ord_no="B"), SimpleNamespace(ord_no=""), SimpleNamespace(ord_no="A")]
    engine = _classifier_engine(pending=orders[:2], recovery=[orders[0], orders[2]])

    assert engine._unresolved_reconciliation_order_ids("SOXL") == ("A", "B")


def test_manual_tranche_allocation_and_guard_precedence_match_live_states():
    # test_open_lifecycle_restart_recovers_manual_t1_and_program_t2_t3:
    # one durable manual share beside restored T2/T3 program lots.
    allocation = _manual_tranche_allocation(
        qty=21, known_tranche_qty=20, has_step_one=False, lifecycle_open=True, lifecycle_manual_qty=1,
    )
    assert (allocation.restored_manual_qty, allocation.adopt_manual_qty, allocation.unattributed_remainder) == (1, 0, 0)

    # Guard-precedence regression state for
    # test_restart_pauses_ambiguous_manual_t1_rebuild_after_later_partial_sell:
    # both revisions pause at the ambiguity guard before allocation.
    allocation = _manual_tranche_allocation(
        qty=11, known_tranche_qty=20, has_step_one=False, lifecycle_open=True, lifecycle_manual_qty=1,
    )
    assert (allocation.restored_manual_qty, allocation.adopt_manual_qty, allocation.unattributed_remainder) == (0, 0, 0)

    # Fallback-precedence regression state for
    # test_restart_after_all_automated_tranches_sell_keeps_manual_t1_basis:
    # both revisions use the zero-known-quantity manual fallback, not allocation.
    allocation = _manual_tranche_allocation(
        qty=1, known_tranche_qty=0, has_step_one=False, lifecycle_open=True, lifecycle_manual_qty=1,
    )
    assert (allocation.restored_manual_qty, allocation.adopt_manual_qty, allocation.unattributed_remainder) == (1, 0, 0)

    # test_mismatched_broker_quantity_stays_paused_with_reason:
    # existing Line 1 plus one excess broker share remains unattributed.
    allocation = _manual_tranche_allocation(
        qty=4, known_tranche_qty=3, has_step_one=True, lifecycle_open=True, lifecycle_manual_qty=1,
    )
    assert (allocation.restored_manual_qty, allocation.adopt_manual_qty, allocation.unattributed_remainder) == (0, 0, 1)

    # Guard-precedence regression state for
    # test_ambiguous_rebuild_pauses_before_mutating_tranche_state:
    # both revisions pause at the ambiguity guard before allocation.
    allocation = _manual_tranche_allocation(
        qty=2, known_tranche_qty=3, has_step_one=False, lifecycle_open=False, lifecycle_manual_qty=0,
    )
    assert (allocation.restored_manual_qty, allocation.adopt_manual_qty, allocation.unattributed_remainder) == (0, 0, 0)
