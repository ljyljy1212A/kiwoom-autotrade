"""Transaction evidence cannot invent an account-order-execution join."""
from decimal import Decimal

import pytest

from src.core.us_market import normalize_us_execution_rows
from src.core.us_transaction_evidence import normalize_us_transaction_evidence


def row(**changes):
    return {"deal_dt": "20261003", "deal_no": "000000252",
            "orig_deal_no": "000000000", "stk_cd": "NVDA", "crnc_code": "USD",
            "deal_qty": "000000000002", "uv_exrt": "201.3147",
            "deal_kind_nm": "trade", "rmrk_nm": "buy", **changes}


def data(*rows):
    return {"_transaction_pages_complete": True, "_query_start_date": "20261002",
            "_query_end_date": "20261003", "_query_symbol": "NVDA",
            "_query_exchange": "ND", "result_list": list(rows)}


def test_transaction_date_and_decimal_evidence_stay_separate_from_orders():
    raw = row(orig_deal_no="000000111")
    result = normalize_us_transaction_evidence(data(raw))
    assert len(result) == 1
    candidate = result[0]
    assert candidate["transaction_date"] == "20261003"
    assert candidate["transaction_no"] == "000000252"
    assert candidate["original_transaction_no"] == "000000111"
    assert candidate["quantity"] == Decimal("2")
    assert candidate["unit_price"] == Decimal("201.3147")
    assert candidate["order_identity_status"] == "unresolved"
    assert candidate["source_api"] == "ust21100"
    assert "ord_no" not in candidate and "execution_date" not in candidate
    assert candidate["raw"] == raw and candidate["raw"] is not raw


def test_same_numeric_order_and_transaction_ids_do_not_establish_a_join():
    # A recent execution price cannot price all fills missed between polls.
    order = {"ord_no": "000000252", "cntr_qty": "5", "cntr_uv": "210.0000"}
    normalized_order = normalize_us_execution_rows({"result_list": [order]}, query_order_date="20261002")[0]
    evidence = normalize_us_transaction_evidence(data(row(ord_no="000000252", ord_dt="20261002")))[0]
    assert normalized_order["execution_date"] == ""
    assert evidence["order_identity_status"] == "unresolved"
    assert "ord_no" not in evidence and "execution_date" not in evidence


def test_same_transaction_number_on_two_dates_is_not_collapsed():
    result = normalize_us_transaction_evidence(data(row(deal_dt="20261002"), row()))
    assert [r["transaction_date"] for r in result] == ["20261002", "20261003"]


def test_identical_page_overlap_is_idempotent_but_conflicting_identity_is_rejected():
    original = row()
    assert len(normalize_us_transaction_evidence(data(original, dict(original)))) == 1
    with pytest.raises(ValueError, match="Conflicting US transaction"):
        normalize_us_transaction_evidence(data(original, row(deal_qty="3")))


@pytest.mark.parametrize("change", [
    {"deal_dt": "20261004"}, {"deal_dt": "20260230"}, {"deal_dt": None},
    {"deal_no": ""}, {"deal_no": "000000000"}, {"deal_no": 252},
    {"orig_deal_no": None}, {"orig_deal_no": "123"}, {"stk_cd": "AAPL"},
    {"crnc_code": "KRW"}, {"deal_qty": "-1"}, {"deal_qty": "NaN"},
    {"deal_qty": "0"}, {"deal_qty": 2}, {"uv_exrt": "Infinity"},
    {"uv_exrt": "0"}, {"uv_exrt": "-201.3"}, {"uv_exrt": ""},
])
def test_malformed_transaction_rejects_entire_batch(change):
    with pytest.raises(ValueError):
        normalize_us_transaction_evidence(data(row(), row(**{"deal_no": "000000253", **change})))


@pytest.mark.parametrize("changes", [
    {"_transaction_pages_complete": False}, {"_transaction_pages_complete": 1},
    {"_query_start_date": ""}, {"_query_end_date": "20261001"},
    {"_query_symbol": ""}, {"_query_exchange": "invalid"},
    {"result_list": None}, {"result_list": [None]},
])
def test_incomplete_or_invalid_query_context_is_rejected(changes):
    with pytest.raises(ValueError):
        normalize_us_transaction_evidence({**data(row()), **changes})


def test_empty_complete_history_is_empty_evidence_and_establishes_no_order_finality():
    assert normalize_us_transaction_evidence(data()) == []
