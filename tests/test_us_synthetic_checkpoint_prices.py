"""Checkpoint projection is read-only, revision-bound and generation-aware."""
from dataclasses import replace
from fractions import Fraction

import pytest

from src.data.us_synthetic_checkpoint_prices import read_synthetic_checkpoint_prices
from src.data.us_synthetic_generations import SyntheticGenerationClosure, SyntheticOrderFinality
from src.data.us_synthetic_snapshot import SyntheticTrancheSettings
from tests.test_us_cumulative_execution import STAMP, NEXT
from tests.test_us_synthetic_bridge import bridge as bridge, proof
from tests.test_us_synthetic_checkpoint_store import store as store
from tests.test_us_synthetic_cost import new_order
from tests.test_us_synthetic_ledger import db as db, dump, observe

SETTINGS = tuple(SyntheticTrancheSettings(step, "-10" if step < 3 else None, "10", "0")
                 for step in (1, 2, 3))


def publish_prices(bridge, store, settings=SETTINGS):
    recovery = store.publish(bridge, expected_storage_revision=None)
    return read_synthetic_checkpoint_prices(
        store, scope=bridge.registry.scope, settings=settings,
        expected_storage_revision=recovery.storage_revision,
        expected_generation_revision=recovery.generation.revision,
    )


def ready_buy(db, bridge):
    observe(db, "2", "105", STAMP)
    bridge.apply(proof(cumulative_amount="210"))
    bridge.registry.finalize_order(SyntheticOrderFinality("first", "2", "210", "buy-final"))


def sell(db, bridge, quantity):
    new_order(db, "sell", "000000044", "SELL")
    bridge.registry.bind_sell("a", "sell")
    observe(db, quantity, "120", NEXT, ord_no="000000044", slby_tp_nm="매도")
    amount = "120" if quantity == "1" else "240"
    bridge.apply(proof(order_uid="sell", cumulative_quantity=quantity, cumulative_amount=amount,
                       execution_sequence=2, observed_at_utc=NEXT))
    bridge.registry.finalize_order(SyntheticOrderFinality("sell", quantity, amount, "sell-final"))


def test_partial_sale_preserves_buy_reference_and_remaining_cost(db, bridge, store):
    ready_buy(db, bridge)
    sell(db, bridge, "1")
    before = dump(db)
    result = publish_prices(bridge, store)
    item = result.tranches[0]
    assert item.generation_id == "a"
    assert item.projection.quantity == 1
    assert item.projection.entry_reference_price == Fraction(105)
    assert item.projection.average_gross_cost == Fraction(105)
    assert item.projection.next_buy_trigger == Fraction(189, 2)
    assert item.projection.sell_target_price == Fraction(231, 2)
    assert result.realized_gross_profit == Fraction(15)
    assert not result.operational_trading_allowed and dump(db) == before


def test_new_generation_excludes_closed_prices_but_preserves_profit(db, bridge, store):
    ready_buy(db, bridge)
    sell(db, bridge, "2")
    registry = bridge.registry
    registry.close(SyntheticGenerationClosure("a", "first", registry.snapshot().revision, "closed-a"))
    new_order(db, "next", "000000045", "BUY")
    registry.reserve("b", 2, "next", predecessor_id="a", expected_revision=registry.snapshot().revision)
    observe(db, "1", "90", NEXT, ord_no="000000045")
    bridge.apply(proof(order_uid="next", generation_id="b", cumulative_quantity="1", cumulative_amount="90",
                       observed_at_utc=NEXT))
    registry.finalize_order(SyntheticOrderFinality("next", "1", "90", "next-final"))
    result = publish_prices(bridge, store)
    assert result.closed_generation_ids == ("a",)
    assert [item.generation_id for item in result.tranches] == ["b"]
    assert result.tranches[0].projection.entry_reference_price == Fraction(90)
    assert result.realized_gross_profit == Fraction(30)


def test_only_highest_held_step_has_next_buy_trigger(db, bridge, store):
    ready_buy(db, bridge)
    registry = bridge.registry
    new_order(db, "next", "000000045", "BUY", step=3)
    registry.reserve("b", 3, "next", predecessor_id=None, expected_revision=registry.snapshot().revision)
    observe(db, "1", "90", NEXT, ord_no="000000045")
    bridge.apply(proof(order_uid="next", generation_id="b", cumulative_quantity="1", cumulative_amount="90",
                       observed_at_utc=NEXT))
    registry.finalize_order(SyntheticOrderFinality("next", "1", "90", "next-final"))
    settings = SETTINGS + (SyntheticTrancheSettings(4, None, "10", "0"),)
    settings = tuple(replace(item, drop_pct="-10") if item.step == 3 else item for item in settings)
    result = publish_prices(bridge, store, settings)
    assert result.active_step == 3
    assert result.tranches[0].projection.next_buy_trigger is None
    assert result.tranches[1].projection.next_buy_trigger == Fraction(81)


def test_closed_only_lifecycle_has_profit_but_no_active_prices(db, bridge, store):
    ready_buy(db, bridge)
    sell(db, bridge, "2")
    registry = bridge.registry
    registry.close(SyntheticGenerationClosure("a", "first", registry.snapshot().revision, "closed-a"))
    result = publish_prices(bridge, store)
    assert result.state == "SYNTHETIC_VALIDATED" and not result.tranches
    assert result.quantity == 0 and result.active_step is None
    assert result.realized_gross_profit == Fraction(30)


def test_fee_model_is_exact_and_last_configured_step_has_no_next_buy(db, bridge, store):
    ready_buy(db, bridge)
    settings = (SETTINGS[0], replace(SETTINGS[1], commission_rate="0.01"))
    result = publish_prices(bridge, store, settings)
    price = result.tranches[0].projection
    assert price.next_buy_trigger is None
    assert price.sell_target_price == Fraction(105) * Fraction(101, 100) * Fraction(11, 10) / Fraction(99, 100)


@pytest.mark.parametrize("reason", ["unresolved", "unapplied", "conflict"])
def test_held_checkpoint_never_returns_prices(db, bridge, store, reason):
    observe(db)
    if reason != "unapplied":
        bridge.apply(proof())
    if reason == "conflict":
        observe(db, "2", "101", NEXT)
    result = publish_prices(bridge, store)
    assert result.state == "HELD" and not result.tranches
    assert result.active_step is None and result.content_token is None


def test_revisions_settings_token_and_read_only_recovery(db, bridge, store):
    ready_buy(db, bridge)
    recovery = store.publish(bridge, expected_storage_revision=None)
    args = dict(scope=bridge.registry.scope, settings=SETTINGS,
                expected_storage_revision=recovery.storage_revision,
                expected_generation_revision=recovery.generation.revision)
    before = dump(store.db)
    first = read_synthetic_checkpoint_prices(store, **args)
    assert read_synthetic_checkpoint_prices(store, **args) == first and dump(store.db) == before
    changed = tuple(replace(item, profit_pct="20") for item in SETTINGS)
    second = read_synthetic_checkpoint_prices(store, **dict(args, settings=changed))
    assert second.content_token != first.content_token
    for key in ("expected_storage_revision", "expected_generation_revision"):
        with pytest.raises(ValueError, match="revision mismatch"):
            read_synthetic_checkpoint_prices(store, **dict(args, **{key: args[key] + 1}))
    assert dump(store.db) == before


@pytest.mark.parametrize("settings", [(), SETTINGS[:1], (SETTINGS[0], SETTINGS[0]),
                                      (replace(SETTINGS[0], commission_rate="1"),),
                                      (replace(SETTINGS[0], drop_pct="10"),)])
def test_invalid_or_uncovered_settings_refuse(db, bridge, store, settings):
    ready_buy(db, bridge)
    with pytest.raises(ValueError):
        publish_prices(bridge, store, settings)
