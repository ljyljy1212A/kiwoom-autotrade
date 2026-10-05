"""Multi-scope storage and recovery fixtures; no operational persistence."""
import sqlite3
from fractions import Fraction

import pytest

from src.data.us_synthetic_generations import SyntheticGenerationRegistry, initialize_synthetic_generations
from src.data.us_synthetic_scope_store import SyntheticScopeStore, initialize_synthetic_scope_store
from tests.test_us_synthetic_generations import SCOPE, buy, reserve, sell_and_close


def contents(db):
    return "\n".join(db.iterdump())


@pytest.fixture
def store():
    connection = sqlite3.connect(":memory:")
    initialize_synthetic_scope_store(connection)
    try:
        yield SyntheticScopeStore(connection)
    finally:
        connection.close()


@pytest.fixture
def registries():
    connections = []

    def create(symbol="AAPL", lifecycle="symbol-cycle"):
        connection = sqlite3.connect(":memory:")
        connections.append(connection)
        scope = dict(SCOPE, symbol=symbol, lifecycle_id=lifecycle)
        initialize_synthetic_generations(connection, **scope)
        return SyntheticGenerationRegistry(connection, **scope)

    yield create
    for connection in connections:
        connection.close()


def test_same_identifiers_are_isolated_between_symbols_and_lifecycles(store, registries):
    a = registries()
    b = registries("MSFT")
    c = registries(lifecycle="next-cycle")
    for registry, amount in ((a, "200"), (b, "300"), (c, "180")):
        buy(registry, "two", 2, "buy2", amount)
        store.publish(registry, expected_revision=None)
    before = contents(store.db)
    states = [store.recover(registry.scope) for registry in (a, b, c)]
    assert [state.remaining_gross_cost for state in states] == [Fraction(200), Fraction(300), Fraction(180)]
    assert all(state.quantity == 2 and state.active_step == 2 for state in states)
    assert all(not state.operational_trading_allowed for state in states)
    assert contents(store.db) == before


def test_closed_generation_profit_and_successor_cost_survive_recovery(store, registries):
    registry = registries()
    buy(registry, "old", 2, "buy-old", "200")
    sell_and_close(registry, "old", "buy-old", "sell-old", "240")
    buy(registry, "new", 2, "buy-new", "180", predecessor="old")
    before = contents(registry.db)
    store.publish(registry, expected_revision=None)
    snapshot = store.recover(registry.scope)
    assert snapshot.realized_gross_profit == Fraction(40)
    assert snapshot.remaining_gross_cost == Fraction(180)
    assert {view.generation_id for view in snapshot.generations} == {"old", "new"}
    assert contents(registry.db) == before


def test_revision_compare_and_update_and_exact_duplicate(store, registries):
    registry = registries()
    buy(registry, "two", 2, "buy2", "200")
    first = store.publish(registry, expected_revision=None)
    before = contents(store.db)
    store.publish(registry, expected_revision=first.revision)
    assert contents(store.db) == before
    buy(registry, "three", 3, "buy3", "180")
    with pytest.raises(ValueError, match="revision conflict"):
        store.publish(registry, expected_revision=first.revision + 99)
    assert contents(store.db) == before
    second = store.publish(registry, expected_revision=first.revision)
    assert second.quantity == 4 and store.recover(registry.scope).revision == second.revision


@pytest.mark.parametrize("sql", [
    "UPDATE synthetic_scoped_events SET cost_json='{}'",
    "UPDATE synthetic_scoped_orders SET side='SELL'",
    "UPDATE synthetic_scoped_generations SET status='CLOSED'",
    "UPDATE synthetic_scoped_events SET generation_id='foreign'",
    "UPDATE synthetic_scoped_state SET policy='foreign'",
])
def test_corrupt_recovery_refuses_without_repair(store, registries, sql):
    registry = registries()
    buy(registry, "two", 2, "buy2", "200")
    store.publish(registry, expected_revision=None)
    store.db.execute(sql)
    store.db.commit()
    before = contents(store.db)
    with pytest.raises(ValueError):
        store.recover(registry.scope)
    assert contents(store.db) == before and not store.db.in_transaction


def test_held_scope_does_not_block_another_scope(store, registries):
    held = registries()
    valid = registries("MSFT")
    reserve(held, "two", 2, "buy2")
    buy(valid, "two", 2, "buy2", "300")
    for registry in (held, valid):
        store.publish(registry, expected_revision=None)
    assert store.recover(held.scope).state == "HELD"
    assert store.recover(valid.scope).state == "SYNTHETIC_VALIDATED"


@pytest.mark.parametrize("table", ["generations", "orders", "events", "holds"])
def test_publication_sql_failure_rolls_back_whole_scope(store, registries, table):
    registry = registries()
    buy(registry, "two", 2, "buy2", "200")
    if table == "holds":
        registry.db.execute("INSERT INTO synthetic_generation_holds VALUES ('two','synthetic-hold')")
        registry.db.commit()
    store.db.execute(f"CREATE TRIGGER reject BEFORE INSERT ON synthetic_scoped_{table} "
                     "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    store.db.commit()
    before = contents(store.db)
    with pytest.raises(sqlite3.IntegrityError):
        store.publish(registry, expected_revision=None)
    assert contents(store.db) == before


def test_missing_scope_is_not_created_by_recovery(store):
    before = contents(store.db)
    with pytest.raises(ValueError, match="Missing scope"):
        store.recover(tuple(SCOPE.values()))
    assert contents(store.db) == before


def test_higher_revision_cannot_replace_accepted_generation_history(store, registries):
    original = registries()
    buy(original, "two", 2, "buy2", "200")
    first = store.publish(original, expected_revision=None)
    replacement = registries()
    buy(replacement, "foreign", 2, "foreign-buy", "180")
    buy(replacement, "three", 3, "buy3", "160")
    before = contents(store.db)
    with pytest.raises(ValueError, match="history cannot be replaced"):
        store.publish(replacement, expected_revision=first.revision)
    assert contents(store.db) == before


def test_caller_transaction_is_not_ended(store):
    store.db.execute("BEGIN")
    with pytest.raises(ValueError, match="own transaction"):
        store.recover(tuple(SCOPE.values()))
    assert store.db.in_transaction
    store.db.rollback()


def test_file_database_refuses_before_schema_creation(tmp_path):
    connection = sqlite3.connect(tmp_path / "synthetic-only.sqlite")
    try:
        with pytest.raises(ValueError, match="in-memory"):
            initialize_synthetic_scope_store(connection)
        assert connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
    finally:
        connection.close()
