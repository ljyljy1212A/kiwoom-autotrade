"""Synthetic ust21100 pagination and input-boundary tests; no broker I/O."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from src.core.kiwoom_client import KiwoomClient

SCOPE = {"start_date": "20261002", "end_date": "20261003", "exchange": "ND"}


def client_with_pages(pages, *, market="US"):
    client = object.__new__(KiwoomClient)
    client.market = market
    client._resolve_exchange = AsyncMock(side_effect=AssertionError("Unexpected discovery"))
    calls = []

    async def post(path, api_id, body, **kwargs):
        calls.append((path, api_id, dict(body), kwargs["cont_yn"], kwargs["next_key"]))
        payload, headers = pages[len(calls) - 1]
        kwargs["response_headers"].update(headers)
        body.clear()  # The next page must still receive the original filters.
        return payload

    client._post = post
    return client, calls


def page(rows=(), headers=None):
    return {"return_code": 0, "result_list": list(rows)}, {"cont-yn": "N"} if headers is None else headers


def test_trade_pages_keep_filters_and_do_not_promote_order_identifiers():
    rows = [{"deal_dt": "20261002", "deal_no": "000000252"},
            {"deal_dt": "20261003", "deal_no": "000000252"}]
    client, calls = client_with_pages([
        page(rows[:1], {"cont-yn": "Y", "next-key": "cursor"}), page(rows[1:]),
    ])
    result = asyncio.run(client.get_us_trade_history("nvda", **SCOPE))
    assert result["result_list"] == rows
    assert result["_transaction_pages_complete"] is True
    assert result["_query_start_date"] == "20261002"
    assert result["_query_end_date"] == "20261003"
    assert result["_query_symbol"] == "NVDA"
    assert result["_query_exchange"] == "ND"
    expected = {"strt_dt": "20261002", "end_dt": "20261003", "tp": "3",
                "stex_tp": "ND", "stk_cd": "NVDA", "krw_repl_skip_yn": "Y"}
    assert calls == [("/api/us/acnt", "ust21100", expected, "N", ""),
                     ("/api/us/acnt", "ust21100", expected, "Y", "cursor")]
    assert all("ord_no" not in row for row in result["result_list"])
    client._resolve_exchange.assert_not_awaited()


@pytest.mark.parametrize("override", [
    {"start_date": None}, {"end_date": 20261003}, {"start_date": ""},
    {"start_date": "20260230"}, {"end_date": "20261001"},
    {"exchange": ""}, {"exchange": "ny"}, {"exchange": "bad"},
])
def test_invalid_scope_fails_before_broker_or_discovery(override):
    client, calls = client_with_pages([])
    with pytest.raises(ValueError):
        asyncio.run(client.get_us_trade_history("NVDA", **{**SCOPE, **override}))
    assert calls == []
    client._resolve_exchange.assert_not_awaited()


@pytest.mark.parametrize("symbol", ["", None, 123, "A005930", "NVDA/OTHER"])
def test_invalid_ticker_fails_before_broker(symbol):
    client, calls = client_with_pages([])
    with pytest.raises(ValueError):
        asyncio.run(client.get_us_trade_history(symbol, **SCOPE))
    assert calls == []


def test_non_us_market_fails_before_broker():
    client, calls = client_with_pages([], market="KR")
    with pytest.raises(ValueError, match="market US"):
        asyncio.run(client.get_us_trade_history("NVDA", **SCOPE))
    assert calls == []


@pytest.mark.parametrize("payload", [None, {}, {"return_code": 7, "result_list": []},
    {"result_list": []}, {"return_code": False, "result_list": []},
    {"return_code": True, "result_list": []}, {"return_code": 0, "result_list": None},
    {"return_code": 0, "result_list": [None]},
])
def test_malformed_or_unsuccessful_pages_never_return_partial_evidence(payload):
    client, calls = client_with_pages([
        page([{"deal_no": "000000001"}], {"cont-yn": "Y", "next-key": "first"}),
        (payload, {"cont-yn": "N"}),
    ])
    with pytest.raises(ValueError, match="transaction page"):
        asyncio.run(client.get_us_trade_history("NVDA", **SCOPE))
    assert len(calls) == 2


@pytest.mark.parametrize("headers", [{}, {"cont-yn": "?"}, {"cont-yn": "Y"},
                                       {"cont-yn": "Y", "next-key": ""},
                                       {"cont-yn": "Y", "next-key": "   "},
                                       {"cont-yn": "Y", "next-key": 123}])
def test_cursor_is_required_even_for_empty_pages(headers):
    client, _ = client_with_pages([page(headers=headers)])
    with pytest.raises(ValueError, match="continuation"):
        asyncio.run(client.get_us_trade_history("NVDA", **SCOPE))


def test_repeated_cursor_rejects_partial_evidence():
    repeated = page(headers={"cont-yn": "Y", "next-key": "same"})
    client, calls = client_with_pages([repeated, repeated])
    with pytest.raises(ValueError, match="continuation key"):
        asyncio.run(client.get_us_trade_history("NVDA", **SCOPE))
    assert len(calls) == 2


def test_transaction_page_limit_is_bounded():
    client, calls = client_with_pages([
        page(headers={"cont-yn": "Y", "next-key": str(i)}) for i in range(100)
    ])
    with pytest.raises(ValueError, match="page limit"):
        asyncio.run(client.get_us_trade_history("NVDA", **SCOPE))
    assert len(calls) == 100


def test_later_page_exception_discards_earlier_pages():
    client, calls = client_with_pages([
        page([{"deal_no": "000000001"}], {"cont-yn": "Y", "next-key": "first"}),
    ])
    successful_post = client._post
    attempts = []

    async def fail_second_page(path, api_id, body, **kwargs):
        attempts.append(kwargs["next_key"])
        if len(attempts) == 2:
            raise RuntimeError("synthetic later-page failure")
        return await successful_post(path, api_id, body, **kwargs)

    client._post = fail_second_page
    with pytest.raises(RuntimeError, match="later-page failure"):
        asyncio.run(client.get_us_trade_history("NVDA", **SCOPE))
    assert attempts == ["", "first"]
    assert len(calls) == 1
    client._resolve_exchange.assert_not_awaited()
