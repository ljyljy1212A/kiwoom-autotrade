"""Identity contracts with synthetic local SQLite only."""
import sqlite3

import pytest

from src.data.order_identity import (
    EVIDENCE_SCHEMA, IDENTITY_INDEX, IDENTITY_SCHEMA, IdentityConflictError,
    OrderIdentityStore, validate_order_date,
)


@pytest.fixture
def identity_store(tmp_path):
    db = sqlite3.connect(tmp_path/'synthetic.db')
    db.execute(IDENTITY_SCHEMA)
    db.execute(IDENTITY_INDEX)
    db.execute(EVIDENCE_SCHEMA)
    db.execute('PRAGMA user_version=2')
    db.commit()
    yield OrderIdentityStore(db)
    db.close()


def new_order(store, account='synthetic-us', market='US'):
    return store.add_unresolved(account, market, '000000042', 'NVDA', 'BUY',
                                '2026-10-02T15:30:00+00:00')


def confirm(store, order, date):
    return store.confirm_us_date(order.order_uid, date, evidence={
        'source_tr': 'ust21180', 'record_ref': 'synthetic:fixture',
        'verified_at_utc': '2026-10-03T00:00:00+00:00'})


def test_unknown_date_is_preserved_without_calendar_inference(identity_store):
    order = new_order(identity_store)
    assert order.broker_order_date is None
    assert order.identity_status == 'unresolved'
    assert identity_store.get(order.order_uid) == order
    # Neither UTC nor KST calendar date becomes the broker order date.
    assert order.submitted_at_utc == '2026-10-02T15:30:00+00:00'


def test_same_number_on_different_dates_and_accounts_is_distinct(identity_store):
    first, second = new_order(identity_store), new_order(identity_store)
    other = new_order(identity_store, account='synthetic-other')
    assert confirm(identity_store, first, '20261002').broker_order_date == '20261002'
    assert confirm(identity_store, second, '20261003').broker_order_date == '20261003'
    assert confirm(identity_store, other, '20261002').identity_status == 'confirmed'
    assert len({first.order_uid, second.order_uid, other.order_uid}) == 3


def test_conflicting_date_is_sticky_and_keeps_original_date(identity_store):
    order = new_order(identity_store)
    confirm(identity_store, order, '20261002')
    with pytest.raises(IdentityConflictError):
        confirm(identity_store, order, '20261003')
    current = identity_store.get(order.order_uid)
    assert current.broker_order_date == '20261002'
    assert current.identity_status == 'conflict'
    with pytest.raises(IdentityConflictError):
        confirm(identity_store, order, '20261002')
    assert identity_store.get(order.order_uid).identity_status == 'conflict'
    assert identity_store.db.execute('SELECT COUNT(*) FROM order_identity_evidence').fetchone()[0] == 3


def test_duplicate_broker_identity_latches_both_orders(identity_store):
    first, second = new_order(identity_store), new_order(identity_store)
    confirm(identity_store, first, '20261002')
    with pytest.raises(IdentityConflictError):
        confirm(identity_store, second, '20261002')
    assert identity_store.get(first.order_uid).identity_status == 'conflict'
    assert identity_store.get(second.order_uid).identity_status == 'conflict'
    assert identity_store.get(second.order_uid).broker_order_date is None


@pytest.mark.parametrize('date', ['20260230', '20261301', '2026-10-03', '', None, '２０２６１００３'])
def test_invalid_dates_are_rejected(date):
    with pytest.raises((ValueError, TypeError)):
        validate_order_date(date)


def test_missing_evidence_and_kr_confirmation_do_not_change_identity(identity_store):
    order = new_order(identity_store)
    with pytest.raises(ValueError, match='evidence'):
        identity_store.confirm_us_date(order.order_uid, '20261002', evidence={})
    assert identity_store.get(order.order_uid) == order
    kr = new_order(identity_store, account='synthetic-kr', market='KR')
    with pytest.raises(ValueError, match='KR'):
        confirm(identity_store, kr, '20261002')
    assert identity_store.get(kr.order_uid) == kr


def test_confirmation_cannot_commit_callers_transaction(identity_store):
    order = new_order(identity_store)
    identity_store.db.execute('BEGIN')
    with pytest.raises(ValueError, match='own transaction'):
        confirm(identity_store, order, '20261002')
    assert identity_store.db.in_transaction
    identity_store.db.rollback()
