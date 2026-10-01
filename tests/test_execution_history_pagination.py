"""Execution pages must be complete before the engine receives any rows."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from src.core.kiwoom_client import KiwoomClient


def _client(market, pages):
    client = object.__new__(KiwoomClient)
    client.market = market
    client._resolve_exchange = AsyncMock(return_value="ND")
    calls = []

    async def post(path, api_id, body, **kwargs):
        calls.append((path, api_id, dict(body), kwargs["cont_yn"], kwargs["next_key"]))
        payload, headers = pages[len(calls) - 1]
        kwargs["response_headers"].update(headers)
        return payload

    client._post = post
    return client, calls


@pytest.mark.parametrize("market,key", [("KR", "cntr"), ("US", "result_list")])
def test_execution_pages_are_combined_and_cursor_forwarded(market, key):
    client, calls = _client(market, [
        ({key: [{"ord_no": "one"}]}, {"cont-yn": "Y", "next-key": "cursor"}),
        ({key: [{"ord_no": "two"}]}, {"cont-yn": "N"}),
    ])
    data = asyncio.run(client.get_executed_orders("AAPL" if market == "US" else "005930"))
    assert data[key] == [{"ord_no": "one"}, {"ord_no": "two"}]
    assert data["_execution_pages_complete"] is True
    assert calls[0][3:] == ("N", "")
    assert calls[1][3:] == ("Y", "cursor")
    if market == "US":
        assert calls[0][2]["query_tp"] == "5"
        assert calls[0][2]["slby_tp"] == "0"


@pytest.mark.parametrize("headers", [{}, {"cont-yn": "?"}, {"cont-yn": "Y"}])
def test_nonempty_page_requires_valid_complete_cursor(headers):
    client, _ = _client("KR", [({"cntr": [{"ord_no": "one"}]}, headers)])
    with pytest.raises(ValueError, match="continuation"):
        asyncio.run(client.get_executed_orders("005930"))


def test_repeated_cursor_rejects_partial_result():
    page = ({"cntr": [{"ord_no": "one"}]}, {"cont-yn": "Y", "next-key": "same"})
    client, calls = _client("KR", [page, page])
    with pytest.raises(ValueError, match="continuation key"):
        asyncio.run(client.get_executed_orders())
    assert len(calls) == 2


@pytest.mark.parametrize("payload", [{}, {"cntr": None}, {"cntr": [None]}])
def test_malformed_page_is_rejected(payload):
    client, _ = _client("KR", [(payload, {"cont-yn": "N"})])
    with pytest.raises(ValueError, match="execution list"):
        asyncio.run(client.get_executed_orders())


def test_page_limit_rejects_partial_result():
    pages = [({"cntr": []}, {"cont-yn": "Y", "next-key": str(i)}) for i in range(100)]
    client, calls = _client("KR", pages)
    with pytest.raises(ValueError, match="page limit"):
        asyncio.run(client.get_executed_orders())
    assert len(calls) == 100


def test_empty_headerless_response_is_terminal():
    client, _ = _client("KR", [({"cntr": []}, {})])
    assert asyncio.run(client.get_executed_orders())["_execution_pages_complete"] is True
