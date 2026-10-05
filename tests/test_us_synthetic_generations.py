"""Standalone generation registry regression contracts; synthetic finality only."""
from dataclasses import replace
from fractions import Fraction
import json
import sqlite3

import pytest

from src.data.us_synthetic_generations import (
    SyntheticGenerationClosure, SyntheticGenerationDelta, SyntheticGenerationRegistry,
    SyntheticOrderFinality, initialize_synthetic_generations,
)

SCOPE = dict(account_id="us_mock", market="US", symbol="AAPL", lifecycle_id="symbol-cycle")


@pytest.fixture
def registry():
    db = sqlite3.connect(":memory:")
    initialize_synthetic_generations(db, **SCOPE)
    try:
        yield SyntheticGenerationRegistry(db, **SCOPE)
    finally:
        db.close()


def dump(registry):
    return "\n".join(registry.db.iterdump())


def reserve(registry, gid, step, uid, predecessor=None):
    return registry.reserve(gid, step, uid, predecessor_id=predecessor,
                            expected_revision=registry.snapshot().revision)


def delta(event, gid, uid, qty, amount, sequence):
    return SyntheticGenerationDelta(event, gid, uid, qty, amount, "20261004", sequence)


def final(registry, uid, qty, amount):
    return registry.finalize_order(SyntheticOrderFinality(uid, qty, amount, "final-" + uid))


def buy(registry, gid, step, uid, amount, predecessor=None):
    reserve(registry, gid, step, uid, predecessor)
    proof = delta("buy-" + gid, gid, uid, "2", amount, 1)
    registry.apply_delta(proof)
    final(registry, uid, "2", amount)
    return proof


def sell_and_close(registry, gid, buy_uid, sell_uid, amount):
    registry.bind_sell(gid, sell_uid)
    proof = delta("sell-" + gid, gid, sell_uid, "2", amount, 2)
    registry.apply_delta(proof)
    final(registry, sell_uid, "2", amount)
    closure = SyntheticGenerationClosure(gid, buy_uid, registry.snapshot().revision, "closed-" + gid)
    registry.close(closure)
    return proof, closure


def test_reentry_keeps_lower_tranche_cost_and_closed_generation_profit(registry):
    buy(registry, "two", 2, "buy2", "210")
    buy(registry, "three-a", 3, "buy3a", "180")
    sell_and_close(registry, "three-a", "buy3a", "sell3a", "240")
    assert registry.snapshot().active_step == 2
    buy(registry, "three-b", 3, "buy3b", "160", predecessor="three-a")
    snapshot = registry.snapshot()
    views = {view.generation_id: view for view in snapshot.generations}
    assert snapshot.state == "SYNTHETIC_VALIDATED" and snapshot.active_step == 3
    assert snapshot.quantity == 4 and snapshot.remaining_gross_cost == Fraction(370)
    assert snapshot.realized_gross_profit == Fraction(60)
    assert views["two"].cost.entry_reference_price == Fraction(105)
    assert views["three-a"].cost.entry_reference_price is None
    assert views["three-b"].cost.entry_reference_price == Fraction(80)
    assert not snapshot.operational_trading_allowed


def test_partial_sell_completion_does_not_prove_closure(registry):
    buy(registry, "a", 3, "buy", "180")
    registry.bind_sell("a", "sell")
    registry.apply_delta(delta("partial", "a", "sell", "1", "120", 2))
    final(registry, "sell", "1", "120")
    proof = SyntheticGenerationClosure("a", "buy", registry.snapshot().revision, "closure")
    before = dump(registry)
    with pytest.raises(ValueError, match="zero economics"):
        registry.close(proof)
    assert dump(registry) == before and registry.snapshot().active_step == 3


def test_zero_quantity_without_finality_blocks_close_and_reentry(registry):
    reserve(registry, "a", 3, "buy")
    proof = SyntheticGenerationClosure("a", "buy", registry.snapshot().revision, "closure")
    with pytest.raises(ValueError, match="Unresolved order"):
        registry.close(proof)
    with pytest.raises(ValueError, match="Unresolved order"):
        reserve(registry, "b", 3, "newbuy", "a")
    assert registry.snapshot().state == "HELD"


def test_zero_fill_reservation_can_close_only_with_explicit_finality(registry):
    reserve(registry, "a", 3, "buy")
    final(registry, "buy", "0", "0")
    registry.close(SyntheticGenerationClosure("a", "buy", registry.snapshot().revision, "closure"))
    assert reserve(registry, "b", 3, "newbuy", "a") == "RESERVED"


def test_duplicate_creation_and_fill_are_idempotent(registry):
    proof = buy(registry, "a", 3, "buy", "180")
    before = dump(registry)
    assert registry.reserve("a", 3, "buy", predecessor_id=None, expected_revision=0) == "DUPLICATE"
    assert registry.apply_delta(proof) == "DUPLICATE"
    assert dump(registry) == before


def test_late_old_delta_holds_lifecycle_without_changing_economics(registry):
    buy(registry, "a", 3, "buy-a", "180")
    old, _ = sell_and_close(registry, "a", "buy-a", "sell-a", "240")
    buy(registry, "b", 3, "buy-b", "160", "a")
    events = registry.db.execute("SELECT * FROM synthetic_generation_events ORDER BY event_id").fetchall()
    assert registry.apply_delta(old) == "DUPLICATE"
    assert registry.apply_delta(delta("late", "a", "buy-a", "1", "95", 3)) == "HELD"
    result = registry.snapshot()
    assert result.state == "HELD" and result.generations == () and result.quantity is None
    assert registry.db.execute("SELECT * FROM synthetic_generation_events ORDER BY event_id").fetchall() == events
    with pytest.raises(ValueError, match="Durable generation hold"):
        reserve(registry, "other", 4, "other-buy")


def test_conflicting_event_id_is_durably_held(registry):
    proof = buy(registry, "a", 3, "buy", "180")
    assert registry.apply_delta(replace(proof, gross_amount="181")) == "HELD"
    assert "late_delta_or_conflicting_event" in registry.snapshot().blocked_reasons


def test_wrong_predecessor_owner_and_stale_revision_refuse(registry):
    buy(registry, "a", 3, "buy", "180")
    sell_and_close(registry, "a", "buy", "sell", "240")
    before = dump(registry)
    with pytest.raises(ValueError, match="Stale"):
        registry.reserve("b", 3, "new", predecessor_id="a", expected_revision=0)
    with pytest.raises(ValueError, match="Foreign predecessor"):
        reserve(registry, "b", 2, "new", "a")
    with pytest.raises(sqlite3.IntegrityError):
        reserve(registry, "b", 3, "buy", "a")
    assert dump(registry) == before


def test_reservation_sql_failure_rolls_back_registry_binding_and_revision(registry):
    registry.db.execute("CREATE TRIGGER deny_binding BEFORE INSERT ON synthetic_generation_orders "
                        "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    registry.db.commit()
    before = dump(registry)
    with pytest.raises(sqlite3.IntegrityError):
        reserve(registry, "a", 3, "buy")
    assert dump(registry) == before and not registry.db.in_transaction


def test_restore_through_fresh_memory_connection_is_read_only(registry):
    buy(registry, "a", 3, "buy", "180")
    original = registry.snapshot()
    db = sqlite3.connect(":memory:")
    try:
        registry.db.backup(db)
        reader = SyntheticGenerationRegistry(db, **SCOPE)
        before = dump(reader)
        assert reader.snapshot() == original and dump(reader) == before
        assert not db.in_transaction
    finally:
        db.close()


def test_corrupt_audit_refuses_restore(registry):
    buy(registry, "a", 3, "buy", "180")
    registry.db.execute("UPDATE synthetic_generation_events SET cost_json='{}'")
    registry.db.commit()
    before = dump(registry)
    with pytest.raises(ValueError, match="audit mismatch"):
        registry.snapshot()
    assert dump(registry) == before


def test_cyclic_predecessor_refuses_restore(registry):
    buy(registry, "a", 3, "buy", "180")
    sell_and_close(registry, "a", "buy", "sell", "240")
    registry.db.execute("UPDATE synthetic_generations SET predecessor_id='a' WHERE generation_id='a'")
    registry.db.commit()
    before = dump(registry)
    with pytest.raises(ValueError, match="Cyclic predecessor"):
        registry.snapshot()
    assert dump(registry) == before and not registry.db.in_transaction


def test_competing_reservation_cannot_replace_open_owner(registry):
    buy(registry, "a", 3, "buy", "180")
    before = dump(registry)
    with pytest.raises(ValueError, match="closed predecessor"):
        reserve(registry, "b", 3, "newbuy", "a")
    assert dump(registry) == before


def test_attached_file_is_refused(registry, tmp_path):
    registry.db.execute("ATTACH DATABASE ? AS external", (str(tmp_path / "external.sqlite"),))
    with pytest.raises(ValueError, match="in-memory"):
        registry.snapshot()


def test_finality_must_cover_exact_economics(registry):
    reserve(registry, "a", 3, "buy")
    registry.apply_delta(delta("buy", "a", "buy", "2", "180", 1))
    before = dump(registry)
    with pytest.raises(ValueError, match="does not cover"):
        final(registry, "buy", "2", "181")
    assert dump(registry) == before


def test_same_order_partial_buys_update_reference_before_finality(registry):
    reserve(registry, "a", 3, "buy")
    registry.apply_delta(delta("one", "a", "buy", "1", "100", 1))
    registry.apply_delta(delta("two", "a", "buy", "1", "110", 2))
    final(registry, "buy", "2", "210")
    assert registry.snapshot().generations[0].cost.entry_reference_price == Fraction(105)


def test_invalid_scope_and_caller_transaction_refuse(registry):
    foreign = SyntheticGenerationRegistry(registry.db, **dict(SCOPE, lifecycle_id="foreign"))
    with pytest.raises(ValueError, match="scope/version"):
        foreign.snapshot()
    registry.db.execute("BEGIN")
    with pytest.raises(ValueError, match="own transaction"):
        registry.snapshot()
    assert registry.db.in_transaction
    registry.db.rollback()


def test_missing_schema_is_not_initialized():
    db = sqlite3.connect(":memory:")
    try:
        with pytest.raises(sqlite3.OperationalError):
            SyntheticGenerationRegistry(db, **SCOPE).snapshot()
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    finally:
        db.close()


@pytest.mark.parametrize("sql", [
    "UPDATE synthetic_generations SET status='UNKNOWN' WHERE generation_id='a'",
    "DELETE FROM synthetic_generation_orders WHERE order_uid='buy'",
    "UPDATE synthetic_generation_orders SET side='SELL' WHERE order_uid='buy'",
    "UPDATE synthetic_generation_events SET cost_json='{}'",
])
def test_duplicate_reservation_refuses_corrupt_persisted_state(registry, sql):
    buy(registry, "a", 3, "buy", "180")
    registry.db.execute(sql)
    registry.db.commit()
    before = dump(registry)
    with pytest.raises(ValueError):
        registry.reserve("a", 3, "buy", predecessor_id=None, expected_revision=0)
    assert dump(registry) == before and not registry.db.in_transaction


def test_duplicate_reserved_generation_is_read_validated_without_writes(registry):
    reserve(registry, "a", 3, "buy")
    before = dump(registry)
    assert registry.reserve("a", 3, "buy", predecessor_id=None, expected_revision=0) == "DUPLICATE"
    assert dump(registry) == before


def test_duplicate_closed_generation_requires_valid_closure(registry):
    buy(registry, "a", 3, "buy", "180")
    sell_and_close(registry, "a", "buy", "sell", "240")
    raw = json.loads(registry.db.execute("SELECT closure_json FROM synthetic_generations WHERE generation_id='a'").fetchone()[0])
    raw["kind"] = "unverified"
    registry.db.execute("UPDATE synthetic_generations SET closure_json=? WHERE generation_id='a'", (json.dumps(raw),))
    registry.db.commit()
    before = dump(registry)
    with pytest.raises(ValueError, match="closure is invalid"):
        registry.reserve("a", 3, "buy", predecessor_id=None, expected_revision=0)
    assert dump(registry) == before


@pytest.mark.parametrize("side", ["INVALID", "BUY"])
@pytest.mark.parametrize("operation", ["snapshot", "finalize", "close"])
def test_zero_event_nonowner_binding_must_have_valid_side_and_owner(registry, side, operation):
    reserve(registry, "a", 3, "buy")
    final(registry, "buy", "0", "0")
    proof = SyntheticOrderFinality("ghost", "0", "0", "synthetic-ghost")
    registry.db.execute("INSERT INTO synthetic_generation_orders VALUES ('ghost','a',?,?)",
                        (side, json.dumps(proof.__dict__, sort_keys=True)))
    registry.db.commit()
    before = dump(registry)
    revision = registry.db.execute("SELECT revision FROM synthetic_generation_scope").fetchone()[0]
    with pytest.raises(ValueError, match="binding side/owner mismatch"):
        if operation == "snapshot":
            registry.snapshot()
        elif operation == "finalize":
            registry.finalize_order(proof)
        else:
            registry.close(SyntheticGenerationClosure("a", "buy", revision, "closure"))
    assert dump(registry) == before and not registry.db.in_transaction


def test_valid_zero_event_sell_binding_remains_supported(registry):
    buy(registry, "a", 3, "buy", "180")
    registry.bind_sell("a", "sell")
    final(registry, "sell", "0", "0")
    result = registry.snapshot()
    assert result.state == "SYNTHETIC_VALIDATED" and result.quantity == 2
