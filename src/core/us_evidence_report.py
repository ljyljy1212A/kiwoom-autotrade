"""Offline US evidence comparison. Confirmed facts never authorize fills."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re

from src.core.us_market import normalize_us_symbol
from src.core.us_transaction_evidence import normalize_us_transaction_evidence
from src.data.us_ws_evidence import decode_evidence_json, read_f5_observations


def _id(value) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9]{9}", value)) and value != "000000000"


def _date(value) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{8}", value):
        return False
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return False
    return True


def _quantity(value) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        return False
    try:
        return Decimal(value).is_finite() and Decimal(value) >= 0
    except InvalidOperation:
        return False


def build_us_evidence_report(snapshot: dict, observations: list[dict], *, account_id: str, broker_account: str) -> dict:
    """Compare explicitly supplied, account-scoped archived inputs only.

    CONFIRMED describes within-source field checks. Cross-source execution
    attribution remains UNATTRIBUTED even when identifiers happen to be equal.
    """
    if not isinstance(snapshot, dict) or snapshot.get("account_id") != account_id or snapshot.get("market") != "US":
        raise ValueError("REST snapshot scope is inconsistent")
    checks = []
    orders = {}

    def add(status, scope, reason, **fields):
        checks.append({"status": status, "scope": scope, "reason": reason, **fields})

    for name in ("order_history", "transaction_history"):
        if not isinstance(snapshot.get(name), list):
            raise ValueError("REST snapshot collections must be lists")
    if not isinstance(observations, list):
        raise ValueError("F5 observations must be a list")
    for source in snapshot["order_history"]:
        if not isinstance(source, dict) or source.get("account_id") != account_id or source.get("source_api") != "ust21150":
            raise ValueError("Order snapshot scope or API is inconsistent")
        data = source.get("data")
        if not isinstance(data, dict) or data.get("_execution_pages_complete") is not True:
            add("UNATTRIBUTED", "order_snapshot", "pages_incomplete")
            continue
        query_date, rows = data.get("_query_order_date"), data.get("result_list")
        if not _date(query_date) or not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            add("UNATTRIBUTED", "order_snapshot", "invalid_query_context_or_rows")
            continue
        for row in rows:
            number, response_date = row.get("ord_no"), row.get("ord_dt")
            if not _id(number) or not _quantity(row.get("cntr_qty")):
                add("UNATTRIBUTED", "order_fields", "invalid_order_number_or_quantity")
                continue
            if response_date not in (None, "", query_date):
                add("MISMATCH", "order_fields", "response_order_date_conflicts_with_query", order_number=number)
                continue
            try:
                ticker = normalize_us_symbol(row.get("stk_cd", ""))
            except ValueError:
                add("UNATTRIBUTED", "order_fields", "invalid_ticker", order_number=number)
                continue
            key = (query_date, number)
            if key in orders and orders[key] != row:
                add("MISMATCH", "order_fields", "conflicting_order_rows", order_number=number, broker_order_date=query_date)
                continue
            orders[key] = dict(row)
            add("CONFIRMED", "dated_order_fields", "valid_dated_order_reference",
                broker_order_date=query_date, order_number=number, symbol=ticker)
    transactions = []
    for source in snapshot["transaction_history"]:
        if not isinstance(source, dict) or source.get("account_id") != account_id or source.get("source_api") != "ust21100":
            raise ValueError("Transaction snapshot scope or API is inconsistent")
        try:
            candidates = normalize_us_transaction_evidence(source.get("data"))
        except (ValueError, TypeError):
            add("UNATTRIBUTED", "transaction_snapshot", "incomplete_or_invalid_transaction_evidence")
            continue
        for row in candidates:
            transactions.append(row)
            add("CONFIRMED", "transaction_fields", "valid_transaction_candidate",
                transaction_number=row["transaction_no"], transaction_date=row["transaction_date"],
                symbol=row["symbol"], quantity=str(row["quantity"]), unit_price=str(row["unit_price"]))
            add("UNATTRIBUTED", "transaction_to_order", "explicit_join_and_execution_date_contract_missing",
                transaction_number=row["transaction_no"], transaction_date=row["transaction_date"])
    for record in observations:
        if not isinstance(record, dict) or record.get("account_id") != account_id or record.get("market") != "US":
            raise ValueError("F5 observation scope is inconsistent")
        item = record.get("raw_event")
        values = item.get("values") if isinstance(item, dict) and item.get("type") == "F5" else None
        if not isinstance(values, dict):
            add("UNATTRIBUTED", "f5_fields", "malformed_event")
            continue
        if values.get("9201") != broker_account:
            raise ValueError("F5 broker account scope is inconsistent")
        number, execution = values.get("9203"), values.get("909")
        if not _id(number) or not _id(execution):
            add("UNATTRIBUTED", "f5_fields", "missing_or_invalid_order_execution_identifier",
                observation_id=record.get("observation_id"))
            continue
        add("CONFIRMED", "within_f5_event", "order_and_execution_identifiers_co_present",
            observation_id=record.get("observation_id"), order_number=number,
            websocket_execution_number=execution, observed_at=record.get("observed_at"))
        candidate_dates = sorted({date for date, order_no in orders if order_no == number})
        add("UNATTRIBUTED", "f5_to_dated_order",
            "broker_order_date_missing" if len(candidate_dates) < 2 else "order_number_reused_across_candidate_dates",
            observation_id=record.get("observation_id"), candidate_order_dates=candidate_dates)
        add("UNATTRIBUTED", "f5_to_transaction", "identifier_namespace_mapping_not_guaranteed",
            observation_id=record.get("observation_id"),
            numerically_equal_transaction_count=sum(r["transaction_no"] == execution for r in transactions))
    if not observations:
        add("UNATTRIBUTED", "f5_coverage", "no_observations_not_proof_of_no_executions")
    if not snapshot["order_history"] or not snapshot["transaction_history"]:
        add("UNATTRIBUTED", "rest_coverage", "missing_history_sources")
    return {"account_id": account_id, "market": "US", "state": "INCOMPLETE",
            "economic_ingestion_allowed": False, "execution_date_status": "unresolved",
            "confirmed_meaning": "within-source field checks only; not economic execution confirmation",
            "counts": dict(Counter(check["status"] for check in checks)), "checks": checks}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read existing scoped evidence and archived REST snapshots; print a report.")
    parser.add_argument("--journal", required=True)
    parser.add_argument("--snapshots", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--broker-account", required=True)
    args = parser.parse_args(argv)
    observations = read_f5_observations(args.journal, account_id=args.account_id, broker_account=args.broker_account)
    snapshot = decode_evidence_json(Path(args.snapshots).read_text(encoding="utf-8"))
    result = build_us_evidence_report(snapshot, observations, account_id=args.account_id, broker_account=args.broker_account)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
