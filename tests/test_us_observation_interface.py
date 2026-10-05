"""Injected observation interface contract tests; no Engine, sink DB or network."""
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from src.core.us_observation_interface import (
    UsObservationAdapter, UsObservationReceipt, UsObservationResponse, UsTrackedObservationOrder,
)
from src.data.order_identity import OrderIdentity
from src.data.us_synthetic_observation_journal import SyntheticJournalIdentity
from tests.test_us_cumulative_execution import DATE, STAMP, NUMBER
from tests.test_us_synthetic_response_adapter import response


def order():
    return UsTrackedObservationOrder(OrderIdentity("first", "us_mock", "US", NUMBER, "AAPL", "BUY",
                                                   DATE, "confirmed", STAMP), "5")


def batch(body=None):
    return UsObservationResponse(DATE, STAMP, response() if body is None else body)


class SinkDouble:
    def __init__(self, reply=None, error=None):
        self.calls = []
        self.reply, self.error = reply, error

    def record_cycle(self, cycle):
        self.calls.append(cycle)
        if self.error:
            raise self.error
        return self.reply(cycle) if self.reply else UsObservationReceipt(cycle.cycle_token, "OBSERVED", True)


def adapter(sink):
    return UsObservationAdapter(account_id="us_mock", market="US", enabled=True, sink=sink)


def test_default_disabled_never_validates_or_calls_sink():
    sink = SinkDouble(error=RuntimeError("must not call"))
    result = UsObservationAdapter(account_id="us_mock", market="US", sink=sink).observe_cycle(orders=None, responses=None)
    assert result.state == "DISABLED" and result.allow_sync_continue
    assert not result.persistence_confirmed and not sink.calls
    assert not result.operational_trading_allowed


def test_missing_active_sink_blocks_without_fallback():
    result = adapter(None).observe_cycle(orders=(order(),), responses=(batch(),))
    assert result.state == "INCOMPLETE" and not result.allow_sync_continue


def test_complete_input_calls_sink_once_with_immutable_decimal_observation():
    sink = SinkDouble()
    tracked, supplied = order(), batch()
    result = adapter(sink).observe_cycle(orders=(tracked,), responses=(supplied,))
    assert result.state == "OBSERVED" and result.allow_sync_continue and result.persistence_confirmed
    assert not result.economic_ingestion_allowed and not result.operational_trading_allowed
    assert len(sink.calls) == 1
    cycle = sink.calls[0]
    assert cycle.observations[0].amount == Decimal(200)
    assert '"cntr_uv": "100.0000"' in cycle.observations[0].raw_json
    with pytest.raises(FrozenInstanceError):
        tracked.identity.symbol = "MSFT"
    supplied.body["result_list"][0]["cntr_uv"] = "999"
    assert cycle.bindings[0].symbol == "AAPL" and cycle.observations[0].average_price == Decimal(100)


@pytest.mark.parametrize("changes", [{"identity_status": "unresolved"}, {"broker_order_date": None},
                                      {"account_id": "foreign"}, {"market": "KR"}, {"symbol": "MSFT"}])
def test_invalid_identity_blocks_before_sink(changes):
    tracked = order()
    sink = SinkDouble()
    result = adapter(sink).observe_cycle(orders=(replace(tracked, identity=replace(tracked.identity, **changes)),),
                                         responses=(batch(),))
    assert result.state == "INCOMPLETE" and not sink.calls


def test_synthetic_identity_is_not_promoted():
    fake = SyntheticJournalIdentity("first", DATE, NUMBER, "AAPL", "BUY", "5", STAMP)
    sink = SinkDouble()
    result = adapter(sink).observe_cycle(orders=(UsTrackedObservationOrder(fake, "5"),), responses=(batch(),))
    assert result.state == "INCOMPLETE" and not sink.calls


def test_all_dates_prevalidated_before_single_sink_call():
    sink = SinkDouble()
    tracked = order()
    another = replace(tracked, identity=replace(tracked.identity, order_uid="second", broker_order_date="20261001"))
    other_body = dict(response(), _query_order_date="20261001", _execution_pages_complete=False)
    result = adapter(sink).observe_cycle(orders=(tracked, another),
                                         responses=(batch(), UsObservationResponse("20261001", STAMP, other_body)))
    assert result.state == "INCOMPLETE" and not sink.calls


def test_same_order_number_on_different_dates_has_separate_uid_binding():
    sink = SinkDouble()
    tracked = order()
    another = replace(tracked, identity=replace(tracked.identity, order_uid="second", broker_order_date="20261001"))
    other_body = dict(response(), _query_order_date="20261001")
    result = adapter(sink).observe_cycle(orders=(tracked, another),
                                         responses=(batch(), UsObservationResponse("20261001", STAMP, other_body)))
    assert result.state == "OBSERVED" and len(sink.calls) == 1
    assert {binding.order_uid for binding in sink.calls[0].bindings} == {"first", "second"}


@pytest.mark.parametrize("mode", ["missing_date", "duplicate_date", "unknown_row", "duplicate_identity"])
def test_ambiguous_or_incomplete_cycle_never_reaches_sink(mode):
    sink = SinkDouble()
    orders, responses = (order(),), (batch(),)
    if mode == "missing_date":
        orders += (replace(order(), identity=replace(order().identity, order_uid="second", broker_order_date="20261001")),)
    elif mode == "duplicate_date":
        responses += (batch(),)
    elif mode == "unknown_row":
        responses = (batch(response(ord_no="000000099")),)
    else:
        orders += (order(),)
    result = adapter(sink).observe_cycle(orders=orders, responses=responses)
    assert result.state == "INCOMPLETE" and not sink.calls


def test_sink_exception_is_sanitized_and_not_retried():
    sink = SinkDouble(error=RuntimeError("synthetic-sensitive-placeholder"))
    result = adapter(sink).observe_cycle(orders=(order(),), responses=(batch(),))
    assert result.state == "INCOMPLETE" and not result.allow_sync_continue
    assert len(sink.calls) == 1 and "synthetic-sensitive-placeholder" not in repr(result)


@pytest.mark.parametrize("reply", [
    lambda cycle: None,
    lambda cycle: UsObservationReceipt("stale", "OBSERVED", True),
    lambda cycle: UsObservationReceipt(cycle.cycle_token, "OBSERVED", False),
    lambda cycle: UsObservationReceipt(cycle.cycle_token, "OBSERVED", True, economic_writes=True),
    lambda cycle: UsObservationReceipt(cycle.cycle_token, "CONFLICT", True),
    lambda cycle: UsObservationReceipt(cycle.cycle_token, "CONFLICT", True, (("foreign", "conflict"),)),
    lambda cycle: UsObservationReceipt(cycle.cycle_token, "CONFLICT", True, (([], "conflict"),)),
])
def test_invalid_receipt_blocks_continuation(reply):
    result = adapter(SinkDouble(reply=reply)).observe_cycle(orders=(order(),), responses=(batch(),))
    assert result.state == "INCOMPLETE" and not result.allow_sync_continue


def test_persisted_conflict_blocks_without_economic_authority():
    sink = SinkDouble(reply=lambda cycle: UsObservationReceipt(cycle.cycle_token, "CONFLICT", True,
                                                               (("first", "amount_changed_without_quantity"),)))
    result = adapter(sink).observe_cycle(orders=(order(),), responses=(batch(),))
    assert result.state == "CONFLICT" and result.persistence_confirmed and not result.allow_sync_continue
    assert not result.economic_ingestion_allowed


def test_requested_quantity_conflict_cannot_be_hidden_by_observed_receipt():
    sink = SinkDouble()
    result = adapter(sink).observe_cycle(orders=(order(),), responses=(batch(response("6")),))
    assert result.state == "INCOMPLETE" and not result.allow_sync_continue
    assert sink.calls[0].required_conflicts == (("first", "quantity_exceeds_requested"),)
    sink = SinkDouble(reply=lambda cycle: UsObservationReceipt(cycle.cycle_token, "CONFLICT", True, cycle.required_conflicts))
    result = adapter(sink).observe_cycle(orders=(order(),), responses=(batch(response("6")),))
    assert result.state == "CONFLICT" and result.persistence_confirmed


@pytest.mark.parametrize("scope", [{"account_id": "foreign", "market": "US"},
                                  {"account_id": "us_mock", "market": "KR"}])
def test_scope_refuses_before_activation(scope):
    with pytest.raises(ValueError):
        UsObservationAdapter(**scope)
