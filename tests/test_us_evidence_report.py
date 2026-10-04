"""Offline comparison must never imply an economic execution confirmation."""
import copy
from datetime import datetime, timezone
import hashlib
import json

import pytest

from src.core.us_evidence_report import build_us_evidence_report, main
from src.data.us_ws_evidence import F5EvidenceJournal, read_f5_observations

ACCOUNT = "test_us_mock"
BROKER = "1234567801"


def snapshot():
    return {"account_id": ACCOUNT, "market": "US", "order_history": [{
        "account_id": ACCOUNT, "source_api": "ust21150",
        "data": {"_execution_pages_complete": True, "_query_order_date": "20261002",
                 "result_list": [{"ord_no": "000000252", "stk_cd": "NVDA", "cntr_qty": "5"}]},
    }], "transaction_history": [{
        "account_id": ACCOUNT, "source_api": "ust21100",
        "data": {"_transaction_pages_complete": True, "_query_start_date": "20261002",
                 "_query_end_date": "20261003", "_query_symbol": "NVDA", "_query_exchange": "ND",
                 "result_list": [{"deal_dt": "20261003", "deal_no": "000000010",
                     "orig_deal_no": "000000000", "stk_cd": "NVDA", "crnc_code": "USD",
                     "deal_qty": "2", "uv_exrt": "100.0000"}]},
    }]}


def event():
    return {"account_id": ACCOUNT, "market": "US", "observation_id": "synthetic",
            "observed_at": "2026-10-03T00:30:00+00:00",
            "raw_event": {"type": "F5", "item": "NVDA",
                          "values": {"9201": BROKER, "9203": "000000252", "909": "000000010",
                                     "908": "233000", "910": "100.0000", "911": "2"}}}


def build(data, events):
    return build_us_evidence_report(data, events, account_id=ACCOUNT, broker_account=BROKER)


def test_equal_ids_and_receipt_time_do_not_confirm_attribution_or_execution_date():
    data, observed = snapshot(), [event()]
    before = copy.deepcopy((data, observed))
    result = build(data, observed)
    assert (data, observed) == before
    assert result["state"] == "INCOMPLETE"
    assert result["economic_ingestion_allowed"] is False
    assert result["execution_date_status"] == "unresolved"
    assert result["counts"]["CONFIRMED"] > 0
    join = [c for c in result["checks"] if c["scope"] == "f5_to_transaction"]
    assert join[0]["numerically_equal_transaction_count"] == 1
    assert join[0]["status"] == "UNATTRIBUTED"
    assert not any(c["status"] == "CONFIRMED" and c["scope"] in ("transaction_to_order", "f5_to_transaction", "f5_to_dated_order") for c in result["checks"])


def test_reused_order_number_across_dates_is_reported_as_unattributed():
    data = snapshot()
    second = copy.deepcopy(data["order_history"][0])
    second["data"]["_query_order_date"] = "20261003"
    data["order_history"].append(second)
    result = build(data, [event()])
    assert any(c["reason"] == "order_number_reused_across_candidate_dates" for c in result["checks"])


def test_conflicting_order_date_is_a_mismatch_not_a_fill():
    data = snapshot()
    data["order_history"][0]["data"]["result_list"][0]["ord_dt"] = "20261003"
    result = build(data, [event()])
    assert result["counts"]["MISMATCH"] == 1
    assert result["economic_ingestion_allowed"] is False


@pytest.mark.parametrize("collection", ["order_history", "transaction_history"])
def test_incomplete_pages_are_reported_without_promoting_rows(collection):
    data = snapshot()
    context = data[collection][0]["data"]
    flag = "_execution_pages_complete" if collection == "order_history" else "_transaction_pages_complete"
    context[flag] = False
    result = build(data, [event()])
    assert any(c["status"] == "UNATTRIBUTED" and c["scope"].endswith("snapshot") for c in result["checks"])


@pytest.mark.parametrize("target", ["snapshot", "order", "transaction", "observation", "broker"])
def test_account_scope_mismatches_fail_closed(target):
    data, observed = snapshot(), [event()]
    if target == "snapshot":
        data["account_id"] = "other"
    elif target == "order":
        data["order_history"][0]["account_id"] = "other"
    elif target == "transaction":
        data["transaction_history"][0]["account_id"] = "other"
    elif target == "broker":
        observed[0]["raw_event"]["values"]["9201"] = "9999999901"
    else:
        observed[0]["account_id"] = "other"
    with pytest.raises(ValueError, match="scope"):
        build(data, observed)


def test_missing_evidence_and_no_fill_event_do_not_establish_health():
    data = {"account_id": ACCOUNT, "market": "US", "order_history": [], "transaction_history": []}
    result = build(data, [])
    assert result["state"] == "INCOMPLETE"
    assert result["counts"].get("CONFIRMED", 0) == 0
    incomplete = event()
    incomplete["raw_event"]["values"]["909"] = "000000000"
    assert any(c["reason"] == "missing_or_invalid_order_execution_identifier" for c in build(snapshot(), [incomplete])["checks"])


def test_cli_reads_archived_inputs_without_modifying_files_or_creating_outputs(tmp_path, capsys):
    journal = F5EvidenceJournal.create(tmp_path / "observations.sqlite", account_id=ACCOUNT, broker_account=BROKER)
    journal.record_frame(json.dumps({"trnm": "REAL", "data": [event()["raw_event"]]}),
                         observed_at=datetime(2026, 10, 3, tzinfo=timezone.utc))
    path = tmp_path / "snapshots.json"
    path.write_text(json.dumps(snapshot()), encoding="utf-8")
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()}
    assert main(["--journal", str(journal.path), "--snapshots", str(path),
                 "--account-id", ACCOUNT, "--broker-account", BROKER]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["economic_ingestion_allowed"] is False
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()} == before
    assert len(read_f5_observations(journal.path, account_id=ACCOUNT, broker_account=BROKER)) == 1
