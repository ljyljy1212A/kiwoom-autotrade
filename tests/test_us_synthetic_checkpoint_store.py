"""Full memory checkpoint tests; observation is never automatic fill recovery."""
import hashlib
import json
import sqlite3
from decimal import Decimal

import pytest

from src.data.us_synthetic_bridge import SyntheticCumulativeGenerationBridge
from src.data.us_synthetic_checkpoint_store import SyntheticCheckpointStore, initialize_synthetic_checkpoint_store
from tests.test_us_cumulative_execution import STAMP, NEXT
from tests.test_us_synthetic_bridge import bridge, proof  # noqa: F401 -- fixture
from tests.test_us_synthetic_ledger import db, dump, observe  # noqa: F401 -- fixture


@pytest.fixture
def store():
    connection = sqlite3.connect(":memory:")
    initialize_synthetic_checkpoint_store(connection)
    try:
        yield SyntheticCheckpointStore(connection)
    finally:
        connection.close()


def test_observation_only_update_advances_storage_not_generation_revision(db, bridge, store):
    first = store.publish(bridge, expected_storage_revision=None)
    observe(db)
    second = store.publish(bridge, expected_storage_revision=first.storage_revision)
    assert second.storage_revision == first.storage_revision + 1
    assert second.generation.revision == first.generation.revision
    assert second.state == "HELD" and second.unapplied_order_uids == ("first",)
    before = dump(store.db)
    assert store.recover(bridge.registry.scope) == second
    assert dump(store.db) == before
    assert db.execute("SELECT COUNT(*) FROM synthetic_generation_events").fetchone()[0] == 0


def test_applied_chain_and_exact_duplicate_survive_recovery(db, bridge, store):
    observe(db)
    bridge.apply(proof())
    first = store.publish(bridge, expected_storage_revision=None)
    before = dump(store.db)
    assert store.publish(bridge, expected_storage_revision=first.storage_revision) == first
    assert dump(store.db) == before
    assert store.recover(bridge.registry.scope) == first
    assert not first.unapplied_order_uids and not first.operational_ingestion_allowed
    # Finality is unresolved even though applied economics are consistent.
    assert first.state == "HELD"


def test_unapplied_latest_delta_is_preserved_after_applied_checkpoint(db, bridge, store):
    observe(db)
    bridge.apply(proof())
    first = store.publish(bridge, expected_storage_revision=None)
    observe(db, "5", "106", NEXT)
    pending = store.publish(bridge, expected_storage_revision=first.storage_revision)
    assert pending.unapplied_order_uids == ("first",)
    assert store.recover(bridge.registry.scope) == pending
    result = bridge.apply(proof(previous_quantity="2", previous_amount="200", cumulative_quantity="5",
                                cumulative_amount="530", execution_sequence=2, observed_at_utc=NEXT))
    assert (result.quantity, result.gross_amount) == (Decimal(3), Decimal(330))
    applied = store.publish(bridge, expected_storage_revision=pending.storage_revision)
    assert not applied.unapplied_order_uids


def test_conflict_and_bridge_hold_are_preserved(db, bridge, store):
    observe(db)
    bridge.apply(proof())
    first = store.publish(bridge, expected_storage_revision=None)
    observe(db, "2", "101", NEXT)
    assert bridge.apply(proof()).state == "HELD"
    held = store.publish(bridge, expected_storage_revision=first.storage_revision)
    assert held.conflicted_order_uids == ("first",) and held.state == "HELD"
    before = dump(store.db)
    assert store.recover(bridge.registry.scope) == held and dump(store.db) == before


@pytest.mark.parametrize("sql", [
    "UPDATE synthetic_bridge_applied SET amount='201'",
    "UPDATE synthetic_bridge_applied SET last_event_id='foreign'",
    "UPDATE synthetic_bridge_audit SET attribution_json='{}'",
    "UPDATE pending_orders SET filled_qty=1",
    "DELETE FROM us_cumulative_observation_audit",
    "DELETE FROM synthetic_bridge_audit",
])
def test_bad_source_is_refused_without_changing_store(db, bridge, store, sql):
    observe(db)
    bridge.apply(proof())
    first = store.publish(bridge, expected_storage_revision=None)
    db.execute(sql)
    db.commit()
    before = dump(store.db)
    with pytest.raises((ValueError, TypeError)):
        store.publish(bridge, expected_storage_revision=first.storage_revision)
    assert dump(store.db) == before


def test_tampered_stored_bundle_refuses_without_repair(db, bridge, store):
    observe(db)
    bridge.apply(proof())
    store.publish(bridge, expected_storage_revision=None)
    row = store.db.execute("SELECT bundle_json FROM synthetic_full_checkpoints").fetchone()
    bundle = json.loads(row[0])
    bundle["rows"]["applied"][0][3] = "201"
    encoded = json.dumps(bundle)
    # Recompute the hash to demonstrate that content validation is independent.
    store.db.execute("UPDATE synthetic_full_checkpoints SET bundle_json=?,sha256=?",
                     (encoded, hashlib.sha256(encoded.encode()).hexdigest()))
    store.db.commit()
    before = dump(store.db)
    with pytest.raises(ValueError):
        store.recover(bridge.registry.scope)
    assert dump(store.db) == before


@pytest.mark.parametrize("action", ["INSERT", "UPDATE"])
def test_sql_failure_rolls_back_full_checkpoint(db, bridge, store, action):
    first = store.publish(bridge, expected_storage_revision=None) if action == "UPDATE" else None
    observe(db)
    store.db.execute(f"CREATE TRIGGER reject BEFORE {action} ON synthetic_full_checkpoints "
                     "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    store.db.commit()
    before = dump(store.db)
    with pytest.raises(sqlite3.IntegrityError):
        store.publish(bridge, expected_storage_revision=first.storage_revision if first else None)
    assert dump(store.db) == before


def test_stale_expected_revision_does_not_replace_checkpoint(db, bridge, store):
    first = store.publish(bridge, expected_storage_revision=None)
    observe(db)
    store.publish(bridge, expected_storage_revision=first.storage_revision)
    before = dump(store.db)
    with pytest.raises(ValueError, match="revision conflict"):
        store.publish(bridge, expected_storage_revision=first.storage_revision)
    assert dump(store.db) == before


@pytest.mark.parametrize("scope_changes", [("MSFT", "synthetic-cycle-1"), ("AAPL", "other-cycle")])
def test_symbol_and_lifecycle_checkpoints_are_isolated(db, bridge, store, scope_changes):
    original = store.publish(bridge, expected_storage_revision=None)
    symbol, lifecycle = scope_changes
    other = sqlite3.connect(":memory:")
    try:
        db.backup(other)
        scope = ("us_mock", "US", symbol, lifecycle)
        other.execute("UPDATE synthetic_generation_scope SET scope_json=?", (json.dumps(scope),))
        other.execute("UPDATE order_identities SET symbol=?", (symbol,))
        other.execute("UPDATE pending_orders SET symbol=?,lifecycle_id=?", (symbol, lifecycle))
        other.commit()
        candidate = SyntheticCumulativeGenerationBridge(other, account_id=scope[0], market=scope[1],
                                                        symbol=symbol, lifecycle_id=lifecycle)
        separate = store.publish(candidate, expected_storage_revision=None)
        assert store.recover(scope) == separate
        assert store.recover(bridge.registry.scope) == original
        assert store.db.execute("SELECT COUNT(*) FROM synthetic_full_checkpoints").fetchone()[0] == 2
    finally:
        other.close()


def test_caller_transaction_is_preserved(db, bridge, store):
    store.db.execute("BEGIN")
    with pytest.raises(ValueError, match="own transaction"):
        store.publish(bridge, expected_storage_revision=None)
    assert store.db.in_transaction
    store.db.rollback()
    db.execute("BEGIN")
    with pytest.raises(ValueError, match="own transaction"):
        store.publish(bridge, expected_storage_revision=None)
    assert db.in_transaction
    db.rollback()


def test_file_database_refuses_schema_creation(tmp_path):
    connection = sqlite3.connect(tmp_path / "synthetic-only.sqlite")
    try:
        with pytest.raises(ValueError, match="in-memory"):
            initialize_synthetic_checkpoint_store(connection)
        assert connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
    finally:
        connection.close()
