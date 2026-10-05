"""Read-only transition assessment against synthetic legacy rows."""
import sqlite3
from dataclasses import replace
from decimal import Decimal

import pytest

from src.data.us_synthetic_transition import SyntheticLegacyFillProof, assess_synthetic_transition
from tests.test_us_synthetic_ledger import db as db, dump


@pytest.fixture
def legacy(db):
    # Add a separate production-shaped legacy table; preserve the existing sentinel.
    for name, datatype in (("order_uid", "TEXT"), ("account_id", "TEXT"), ("ord_no", "TEXT"),
                           ("symbol", "TEXT"), ("type", "TEXT"), ("step", "INTEGER")):
        db.execute(f"ALTER TABLE trade_ledger ADD COLUMN {name} {datatype}")
    db.execute("CREATE TABLE execution_quantity_conflicts (order_uid TEXT PRIMARY KEY)")
    db.commit()
    return db


def old_fill(db):
    db.execute("INSERT INTO trade_ledger "
               "(id,qty,price,order_uid,account_id,ord_no,symbol,type,step) "
               "VALUES ('old',2,100,'first','us_mock','000000042','AAPL','buy',2)")
    db.execute("UPDATE pending_orders SET filled_qty=2")
    db.commit()


def proof():
    return SyntheticLegacyFillProof("old", "first", "2", "200.0001", "20261002",
                                    "100.0", "synthetic-evidence")


def test_zero_baseline_requires_no_legacy_fills(legacy):
    before = dump(legacy)
    report = assess_synthetic_transition(legacy, "first")
    assert report.state == "ZERO_BASELINE_CANDIDATE"
    assert (report.quantity, report.calculated_gross_amount) == (Decimal(0), Decimal(0))
    assert not report.operational_transition_allowed
    assert dump(legacy) == before


def test_float_price_never_establishes_exact_amount_or_date(legacy):
    old_fill(legacy)
    before = dump(legacy)
    report = assess_synthetic_transition(legacy, "first")
    assert report.state == "HELD"
    assert "exact_amount_evidence_missing" in report.reasons
    assert "execution_date_evidence_missing" in report.reasons
    assert report.calculated_gross_amount is None and dump(legacy) == before


def test_exact_synthetic_proof_produces_candidate_without_writes(legacy):
    old_fill(legacy)
    before = dump(legacy)
    report = assess_synthetic_transition(legacy, "first", (proof(),))
    assert report.state == "EXACT_BASELINE_CANDIDATE"
    assert report.calculated_gross_amount == Decimal("200.0001")
    assert not report.operational_transition_allowed
    assert dump(legacy) == before


@pytest.mark.parametrize("changes", [{"quantity": "3"}, {"execution_date": ""},
                                    {"evidence_id": ""}, {"kind": "price-times-quantity"},
                                    {"order_uid": "other"}, {"legacy_price": "101.0"}])
def test_invalid_or_unbound_proof_is_held(legacy, changes):
    old_fill(legacy)
    before = dump(legacy)
    report = assess_synthetic_transition(legacy, "first", (replace(proof(), **changes),))
    assert report.state == "HELD" and dump(legacy) == before


def test_duplicate_proofs_are_held(legacy):
    old_fill(legacy)
    report = assess_synthetic_transition(legacy, "first", (proof(), proof()))
    assert report.state == "HELD"


@pytest.mark.parametrize("sql", [
    "UPDATE pending_orders SET filled_qty=1", "UPDATE pending_orders SET step=3",
    "UPDATE order_identities SET identity_status='unresolved'",
    "UPDATE trade_ledger SET order_uid=NULL WHERE id='old'",
    "INSERT INTO execution_quantity_conflicts VALUES ('first')",
])
def test_legacy_conflicts_never_produce_candidate(legacy, sql):
    old_fill(legacy)
    legacy.execute(sql)
    legacy.commit()
    before = dump(legacy)
    report = assess_synthetic_transition(legacy, "first", (proof(),))
    assert report.state == "HELD" and dump(legacy) == before


def test_missing_schema_is_unresolved_and_read_snapshot_ends(db):
    before = dump(db)
    with pytest.raises(sqlite3.OperationalError):
        assess_synthetic_transition(db, "first")
    assert dump(db) == before and not db.in_transaction


def test_caller_transaction_survives_refusal(legacy):
    legacy.execute("UPDATE pending_orders SET filled_qty=1")
    with pytest.raises(ValueError, match="read snapshot"):
        assess_synthetic_transition(legacy, "first")
    assert legacy.in_transaction
    assert legacy.execute("SELECT filled_qty FROM pending_orders").fetchone()[0] == 1
    legacy.rollback()


def test_file_db_refused_before_inspection(tmp_path):
    connection = sqlite3.connect(tmp_path / "synthetic-transition.sqlite")
    try:
        with pytest.raises(ValueError, match="in-memory"):
            assess_synthetic_transition(connection, "first")
    finally:
        connection.close()
