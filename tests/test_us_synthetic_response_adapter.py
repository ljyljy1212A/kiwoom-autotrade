"""Offline response adapter contracts; no network, Engine or operational DB."""
from dataclasses import replace
from decimal import Decimal
import sqlite3

import pytest

from src.data.us_synthetic_response_adapter import (
    SyntheticResponseBinding, SyntheticUsResponseAdapter,
)
from tests.test_us_cumulative_execution import DATE, STAMP, NEXT, NUMBER
from tests.test_us_synthetic_bridge import SCOPE, bridge, proof  # noqa: F401 -- fixture
from tests.test_us_synthetic_ledger import db, dump  # noqa: F401 -- fixture


def response(quantity="2", average="100.0000", **changes):
    row = {"ord_no": NUMBER, "stk_cd": "AAPL", "slby_tp_nm": "매수",
           "crnc_code": "USD", "cntr_qty": quantity, "cntr_uv": average,
           "cntr_time": "10:00:00"}
    row.update(changes)
    return {"return_code": 0, "_execution_pages_complete": True,
            "_query_order_date": DATE, "result_list": [row]}


@pytest.fixture
def adapter(db, bridge):
    return SyntheticUsResponseAdapter(db, **SCOPE)


def ingest(adapter, data=None, *, stamp=STAMP, binding=None, attribution=None):
    return adapter.ingest(response() if data is None else data, query_order_date=DATE,
                          observed_at_utc=stamp,
                          bindings=(binding or SyntheticResponseBinding(NUMBER, "first", "a"),),
                          attribution=attribution)


def test_missing_evidence_only_observes_preserving_raw_strings(db, adapter):
    result = ingest(adapter)
    assert result.state == "OBSERVED_ONLY" and result.application is None
    assert not result.operational_ingestion_allowed
    assert db.execute("SELECT COUNT(*) FROM synthetic_generation_events").fetchone()[0] == 0
    assert db.execute("SELECT filled_qty FROM pending_orders WHERE order_uid='first'").fetchone()[0] == 0
    raw = db.execute("SELECT observation_json FROM us_cumulative_observation_audit").fetchone()[0]
    assert '"cntr_uv": "100.0000"' in raw


def test_proof_applies_full_economic_delta_not_latest_observation_increment(db, adapter):
    ingest(adapter)
    ingest(adapter, response("5", "106.0000"), stamp=NEXT)
    result = ingest(adapter, response("5", "106.0000"), stamp=NEXT,
                    attribution=proof(cumulative_quantity="5", cumulative_amount="530",
                                      observed_at_utc=NEXT))
    assert result.state == "APPLIED"
    assert (result.application.quantity, result.application.gross_amount) == (Decimal(5), Decimal(530))
    assert db.execute("SELECT COUNT(*) FROM synthetic_us_fills").fetchone()[0] == 0


def test_duplicate_response_and_proof_do_not_duplicate_economics(db, adapter):
    ingest(adapter, attribution=proof())
    before = db.execute("SELECT * FROM synthetic_generation_events").fetchall()
    result = ingest(adapter, attribution=proof())
    assert result.state == "DUPLICATE"
    assert db.execute("SELECT * FROM synthetic_generation_events").fetchall() == before


@pytest.mark.parametrize("binding", [
    SyntheticResponseBinding(NUMBER, "unknown", "a"),
    SyntheticResponseBinding(NUMBER, "first", "other"),
    SyntheticResponseBinding("999", "first", "a"),
])
def test_foreign_binding_refuses_before_observation(db, adapter, binding):
    before = dump(db)
    with pytest.raises(ValueError):
        ingest(adapter, binding=binding)
    assert dump(db) == before


@pytest.mark.parametrize("changes", [{"stk_cd": "MSFT"}, {"slby_tp_nm": "매도"},
                                    {"ord_dt": "20261001"}, {"cntr_qty": 2}])
def test_bad_response_scope_or_numeric_type_refuses_without_writes(db, adapter, changes):
    before = dump(db)
    with pytest.raises(ValueError):
        ingest(adapter, response(**changes))
    assert dump(db) == before


def test_partial_pages_refuse_without_observation(db, adapter):
    data = response()
    data["_execution_pages_complete"] = False
    before = dump(db)
    with pytest.raises(ValueError):
        ingest(adapter, data)
    assert dump(db) == before


def test_missing_date_proof_refuses_economics_but_retains_observation(db, adapter):
    with pytest.raises(ValueError):
        ingest(adapter, attribution=replace(proof(), execution_date=""))
    assert db.execute("SELECT COUNT(*) FROM us_cumulative_observations").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM synthetic_bridge_applied").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM synthetic_generation_events").fetchone()[0] == 0


def test_conflicting_observation_blocks_application(db, adapter):
    ingest(adapter, attribution=proof())
    before = db.execute("SELECT * FROM synthetic_generation_events").fetchall()
    result = ingest(adapter, response(average="101"), stamp=NEXT,
                    attribution=proof(cumulative_amount="202", observed_at_utc=NEXT))
    assert result.state == "HELD" and result.application is None
    assert db.execute("SELECT * FROM synthetic_generation_events").fetchall() == before


def test_caller_transaction_is_preserved(db, adapter):
    db.execute("BEGIN")
    with pytest.raises(ValueError, match="own transaction"):
        ingest(adapter)
    assert db.in_transaction
    db.rollback()


def test_file_backed_database_is_refused(tmp_path):
    connection = sqlite3.connect(tmp_path / "offline.sqlite")
    try:
        with pytest.raises(ValueError, match="in-memory"):
            SyntheticUsResponseAdapter(connection, **SCOPE)
    finally:
        connection.close()
