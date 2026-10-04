import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.us_market import (
    extract_us_fx_rate,
    normalize_us_execution_rows,
    normalize_us_holdings,
    normalize_us_symbol,
    validate_us_order,
)


def test_us_symbol_and_order_validation():
    assert normalize_us_symbol(" brk.b ") == "BRK.B"
    assert validate_us_order("AAPL", "ND", 2, 201.125, "00") == "AAPL"
    for invalid in (lambda: normalize_us_symbol("A005930"),
                    lambda: validate_us_order("AAPL", "ND", 0.5, 201, "00")):
        try:
            invalid()
        except ValueError:
            pass
        else:
            raise AssertionError("invalid US order input was accepted")


def test_us_holdings_normalization_uses_usd_fields():
    raw = {"result_list": [{
        "ovrs_pdno": "AAPL", "ovrs_item_name": "Apple", "ovrs_cblc_qty": "3",
        "pchs_avg_pric": "200.125", "ovrs_now_pric": "201.5", "prev_close": "199.0",
    }]}
    assert normalize_us_holdings(raw) == [{
        "symbol": "AAPL", "name": "Apple", "qty": 3.0,
        "avgPrice": 200.125, "currentPrice": 201.5, "prevClose": 199.0, "currency": "USD",
    }]


def test_us_execution_and_fx_normalization():
    rows = normalize_us_execution_rows({"ord_cntr_list": [{
        "odno": "123", "tot_ccld_qty": "2", "exec_price": "201.25", "exec_date": "20260811",
    }]})
    assert rows == [{"ord_no": "123", "cntr_qty": 2.0, "cntr_pric": 201.25,
                     "broker_order_date": "",
                     "query_order_date": "", "execution_date": ""}]
    assert extract_us_fx_rate({"aplc_exrt": "1,385.42"}) == 1385.42


def test_us_execution_preserves_order_query_dates_without_accepting_undocumented_execution_date():
    rows = normalize_us_execution_rows({"_query_order_date": "20261003", "result_list": [{
        "ord_no": "123", "ord_dt": "20261002", "cntr_dt": "20261004",
        "cntr_qty": "2", "cntr_uv": "201.25",
    }]})
    assert rows == [{"ord_no": "123", "cntr_qty": 2.0, "cntr_pric": 201.25,
                     "broker_order_date": "20261002",
                     "query_order_date": "20261003", "execution_date": ""}]


if __name__ == "__main__":
    test_us_symbol_and_order_validation()
    test_us_holdings_normalization_uses_usd_fields()
    test_us_execution_and_fx_normalization()
    print("US market normalization checks passed")


@pytest.mark.parametrize("extra", [
    {},
    {"cntr_dt": "20261003"},
    {"exec_date": "20261003"},
    {"execution_date": "20261003"},
    {"cntr_dt": "20261003", "exec_date": "20261004"},
    {"22": "20261003", "20": "123456"},
])
def test_us_rest_execution_date_requires_a_documented_account_contract(extra):
    # Official REST fields contain a KST time, not an execution date.
    row = {"ord_no": "000000252", "stk_cd": "NVDA", "ord_qty": "000000000005",
           "cntr_qty": "000000000002", "cntr_uv": "201.3147", "cntr_time": "21:04:41"}
    row.update(extra)
    normalized = normalize_us_execution_rows({"result_list": [row]}, query_order_date="20261002")
    assert normalized[0]["execution_date"] == ""
    assert normalized[0]["query_order_date"] == "20261002"
    assert normalized[0]["broker_order_date"] == ""
