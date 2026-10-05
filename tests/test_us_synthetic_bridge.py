"""Cumulative-to-generation transaction contracts, entirely synthetic."""
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
import json
import sqlite3

import pytest

from src.data.us_synthetic_bridge import (
    SyntheticBridgeAttribution, SyntheticCumulativeGenerationBridge, initialize_synthetic_bridge,
)
from src.data.us_synthetic_generations import (
    SyntheticGenerationClosure, SyntheticGenerationRegistry, SyntheticOrderFinality,
    initialize_synthetic_generations,
)
from tests.test_us_cumulative_execution import DATE, STAMP, NEXT
from tests.test_us_synthetic_cost import CYCLE, new_order
from tests.test_us_synthetic_ledger import db, dump, observe  # noqa: F401 -- imported fixture

SCOPE = dict(account_id="us_mock", market="US", symbol="AAPL", lifecycle_id=CYCLE)


@pytest.fixture
def bridge(db):
    initialize_synthetic_generations(db, **SCOPE)
    initialize_synthetic_bridge(db)
    registry = SyntheticGenerationRegistry(db, **SCOPE)
    registry.reserve("a", 2, "first", predecessor_id=None, expected_revision=0)
    return SyntheticCumulativeGenerationBridge(db, **SCOPE)


def proof(**changes):
    original = SyntheticBridgeAttribution("first", "a", "0", "0", "2", "200", DATE, 1,
                                          STAMP, "synthetic-bridge-case")
    return replace(original, **changes)


def test_latest_observation_applies_from_economic_zero_not_observation_delta(db, bridge):
    observe(db, "2", "100", STAMP)
    observe(db, "5", "106", NEXT)
    result = bridge.apply(proof(cumulative_quantity="5", cumulative_amount="530", observed_at_utc=NEXT))
    assert (result.quantity, result.gross_amount) == (Decimal(5), Decimal(530))
    assert not result.operational_ingestion_allowed
    assert db.execute("SELECT filled_qty,status FROM pending_orders WHERE order_uid='first'").fetchone() == (5, "filled")
    assert db.execute("SELECT COUNT(*) FROM synthetic_us_fills").fetchone()[0] == 0
    state, _ = bridge.registry._replay("a")
    assert state.entry_reference_price == Fraction(106) and state.remaining_gross_cost == Fraction(530)


def test_second_delta_is_exact_monetary_difference_and_duplicate_is_read_only(db, bridge):
    observe(db)
    bridge.apply(proof())
    observe(db, "5", "106", NEXT)
    attribution = proof(previous_quantity="2", previous_amount="200", cumulative_quantity="5",
                        cumulative_amount="530", execution_sequence=2, observed_at_utc=NEXT)
    result = bridge.apply(attribution)
    assert (result.quantity, result.gross_amount) == (Decimal(3), Decimal(330))
    before = dump(db)
    assert bridge.apply(attribution).state == "DUPLICATE"
    assert dump(db) == before and not db.in_transaction


@pytest.mark.parametrize("table,action", [
    ("synthetic_generation_events", "INSERT"), ("synthetic_bridge_audit", "INSERT"),
    ("synthetic_bridge_applied", "INSERT"), ("pending_orders", "UPDATE"),
])
def test_failure_rolls_back_every_economic_checkpoint_counter_and_revision(db, bridge, table, action):
    observe(db)
    db.execute(f"CREATE TRIGGER deny_write BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    db.commit()
    before = dump(db)
    with pytest.raises(sqlite3.IntegrityError):
        bridge.apply(proof())
    assert dump(db) == before and not db.in_transaction


@pytest.mark.parametrize("changes", [
    {"generation_id": "foreign"}, {"order_uid": "unknown"}, {"execution_date": ""},
    {"execution_sequence": None}, {"previous_amount": "1"}, {"cumulative_amount": "201"},
    {"kind": "query-date"}, {"evidence_id": ""}, {"observed_at_utc": NEXT},
])
def test_invalid_identity_or_full_delta_proof_refuses_without_writes(db, bridge, changes):
    observe(db)
    before = dump(db)
    with pytest.raises(ValueError):
        bridge.apply(proof(**changes))
    assert dump(db) == before and not db.in_transaction


def test_older_proof_cannot_apply_newer_observation(db, bridge):
    observe(db)
    stale = proof()
    observe(db, "5", "106", NEXT)
    before = dump(db)
    with pytest.raises(ValueError, match="latest full unapplied delta"):
        bridge.apply(stale)
    assert dump(db) == before


def test_changed_duplicate_attribution_is_not_success(db, bridge):
    observe(db)
    bridge.apply(proof())
    before = dump(db)
    with pytest.raises(ValueError, match="Duplicate attribution"):
        bridge.apply(proof(execution_sequence=2))
    assert dump(db) == before


def test_observation_conflict_latches_hold_without_new_economics(db, bridge):
    observe(db)
    bridge.apply(proof())
    observe(db, "2", "101", NEXT)
    events = db.execute("SELECT * FROM synthetic_generation_events").fetchall()
    checkpoint = db.execute("SELECT * FROM synthetic_bridge_applied").fetchall()
    result = bridge.apply(proof())
    assert result.state == "HELD" and bridge.registry.snapshot().state == "HELD"
    assert db.execute("SELECT * FROM synthetic_generation_events").fetchall() == events
    assert db.execute("SELECT * FROM synthetic_bridge_applied").fetchall() == checkpoint


def test_missing_baseline_after_existing_fill_is_not_guessed(db, bridge):
    observe(db)
    bridge.apply(proof())
    db.execute("DELETE FROM synthetic_bridge_applied")
    db.commit()
    before = dump(db)
    with pytest.raises(ValueError, match="explicit bridge baseline"):
        bridge.apply(proof())
    assert dump(db) == before


def test_corrupt_attribution_audit_refuses_even_duplicate(db, bridge):
    observe(db)
    bridge.apply(proof())
    raw = json.loads(db.execute("SELECT attribution_json FROM synthetic_bridge_audit").fetchone()[0])
    raw["execution_sequence"] = 99
    db.execute("UPDATE synthetic_bridge_audit SET attribution_json=?", (json.dumps(raw),))
    db.commit()
    before = dump(db)
    with pytest.raises(ValueError, match="audit disagrees"):
        bridge.apply(proof())
    assert dump(db) == before


def test_recovery_after_committed_application_does_not_reapply(db, bridge):
    observe(db)
    bridge.apply(proof())
    copy = sqlite3.connect(":memory:")
    try:
        db.backup(copy)
        reader = SyntheticCumulativeGenerationBridge(copy, **SCOPE)
        before = dump(copy)
        assert reader.apply(proof()).state == "DUPLICATE"
        assert dump(copy) == before
    finally:
        copy.close()


def test_full_cumulative_quantity_does_not_infer_finality(db, bridge):
    observe(db, "5", "100", STAMP)
    bridge.apply(proof(cumulative_quantity="5", cumulative_amount="500"))
    assert db.execute("SELECT finality_json FROM synthetic_generation_orders").fetchone()[0] is None
    assert bridge.registry.snapshot().state == "HELD"


def test_late_old_generation_delta_holds_without_touching_successor(db, bridge):
    observe(db)
    bridge.apply(proof())
    registry = bridge.registry
    registry.finalize_order(SyntheticOrderFinality("first", "2", "200", "buy-final"))
    new_order(db, "sell", "000000044", "SELL")
    db.execute("UPDATE pending_orders SET requested_qty=2 WHERE order_uid='sell'")
    db.commit()
    registry.bind_sell("a", "sell")
    observe(db, "2", "120", NEXT, ord_no="000000044", slby_tp_nm="매도")
    bridge.apply(proof(order_uid="sell", cumulative_amount="240", execution_sequence=2, observed_at_utc=NEXT))
    registry.finalize_order(SyntheticOrderFinality("sell", "2", "240", "sell-final"))
    registry.close(SyntheticGenerationClosure("a", "first", registry.snapshot().revision, "close-a"))
    new_order(db, "next", "000000045", "BUY")
    registry.reserve("b", 2, "next", predecessor_id="a", expected_revision=registry.snapshot().revision)
    observe(db, "2", "90", NEXT, ord_no="000000045")
    bridge.apply(proof(order_uid="next", generation_id="b", cumulative_amount="180", observed_at_utc=NEXT))
    before_events = db.execute("SELECT * FROM synthetic_generation_events ORDER BY event_id").fetchall()
    before_pending = db.execute("SELECT filled_qty FROM pending_orders WHERE order_uid='first'").fetchone()
    observe(db, "3", "100", NEXT)
    result = bridge.apply(proof(previous_quantity="2", previous_amount="200", cumulative_quantity="3",
                                cumulative_amount="300", execution_sequence=3, observed_at_utc=NEXT))
    assert result.state == "HELD"
    assert db.execute("SELECT * FROM synthetic_generation_events ORDER BY event_id").fetchall() == before_events
    assert db.execute("SELECT filled_qty FROM pending_orders WHERE order_uid='first'").fetchone() == before_pending


def test_caller_transaction_is_preserved(db, bridge):
    db.execute("BEGIN")
    with pytest.raises(ValueError, match="own transaction"):
        bridge.apply(proof())
    assert db.in_transaction
    db.rollback()


def test_internal_generation_operation_requires_transaction(bridge):
    from src.data.us_synthetic_generations import SyntheticGenerationDelta
    with pytest.raises(ValueError, match="caller transaction"):
        bridge.registry._apply_delta_in_transaction(SyntheticGenerationDelta("event", "a", "first", "2", "200", DATE, 1))
