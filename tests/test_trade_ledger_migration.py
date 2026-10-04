"""Synthetic-only identity migration fixtures; no broker or runtime access."""
from contextlib import closing
import sqlite3

import pytest

from src.data.trade_ledger import PendingOrder, TradeLedgerStore
from src.data.trade_ledger_migration import create_identity_ledger_copy

TABLES = ('pending_orders', 'trade_ledger', 'execution_quantity_conflicts')


def legacy_fixture(path):
    store = TradeLedgerStore(str(path), 'synthetic-us')
    buy = PendingOrder('000000042', 'NVDA', 'BUY', 5, 100, 'BUY', 1, {})
    store.add_pending(buy)
    row = store.record_fill(buy, 2, 100, '2026-10-02')
    sell = PendingOrder('000000043', 'NVDA', 'SELL', 1, 101, 'SELL', 1,
                        {'buy_id': row['id']})
    store.add_pending(sell)
    store.record_fill(sell, 1, 101, '2026-10-02')
    store.db.execute("UPDATE trade_ledger SET buy_id=? WHERE type='sell'", (row['id'],))
    store.db.execute("INSERT INTO execution_quantity_conflicts VALUES (?,?,?,?,?,?,?,?,?)",
                     ('synthetic-us', buy.ord_no, 'NVDA', 5, 2, 6, 'excess', 'first', 'last'))
    store.db.commit()
    store.close()
    kr = TradeLedgerStore(str(path), 'synthetic-kr')
    kr.add_pending(PendingOrder('000000042', '005930', 'BUY', 2, 100, 'BUY', 1, {}))
    kr.close()


def rows(path):
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        return {table: [dict(r) for r in db.execute(f'SELECT * FROM {table}')]
                for table in TABLES}


def test_copy_preserves_legacy_values_links_conflicts_and_kr(tmp_path):
    source, destination = tmp_path/'legacy.db', tmp_path/'candidate.db'
    legacy_fixture(source)
    before_bytes, before = source.read_bytes(), rows(source)
    create_identity_ledger_copy(source, destination,
                                account_markets={'synthetic-us': 'US', 'synthetic-kr': 'KR'})
    after = rows(destination)
    for table in TABLES:
        assert len(after[table]) == len(before[table])
        for old, new in zip(before[table], after[table]):
            assert {key: new[key] for key in old} == old
            assert new['order_uid']
    assert source.read_bytes() == before_bytes
    with sqlite3.connect(destination) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 2
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        identities = db.execute('SELECT broker_order_date,identity_status FROM order_identities').fetchall()
        assert identities == [(None, 'unresolved')]*3
        assert db.execute('SELECT COUNT(*) FROM order_identity_evidence').fetchone()[0] == 0
        assert db.execute('SELECT DISTINCT execution_date_status FROM trade_ledger').fetchall() == [('legacy_unverified',)]
    with pytest.raises(ValueError, match='explicit market'):
        TradeLedgerStore(str(destination), 'synthetic-us')
    adapter = TradeLedgerStore(str(destination), 'synthetic-us', market='US')
    try:
        assert adapter.pending_orders('NVDA')[0].order_uid
        assert adapter.pending_orders('NVDA')[0].identity_status == 'unresolved'
    finally:
        adapter.close()


@pytest.mark.parametrize('fault', ['missing_market', 'orphan_trade', 'broken_buy'])
def test_failed_copy_rolls_back_schema_and_preserves_original(tmp_path, fault):
    source, destination = tmp_path/'legacy.db', tmp_path/'candidate.db'
    legacy_fixture(source)
    with sqlite3.connect(source) as db:
        if fault == 'orphan_trade':
            db.execute("UPDATE trade_ledger SET ord_no='missing' WHERE type='sell'")
        elif fault == 'broken_buy':
            db.execute("UPDATE trade_ledger SET buy_id='missing' WHERE type='sell'")
    before_bytes, before = source.read_bytes(), rows(source)
    markets = {'synthetic-us': 'US', 'synthetic-kr': 'KR'} if fault != 'missing_market' else {}
    with pytest.raises(ValueError):
        create_identity_ledger_copy(source, destination, account_markets=markets)
    assert source.read_bytes() == before_bytes
    assert rows(destination) == before
    with sqlite3.connect(destination) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 0
        assert db.execute("SELECT name FROM sqlite_master WHERE name='order_identities'").fetchall() == []


def test_in_place_and_existing_destination_are_rejected(tmp_path):
    source, destination = tmp_path/'legacy.db', tmp_path/'candidate.db'
    legacy_fixture(source)
    before = source.read_bytes()
    with pytest.raises(ValueError, match='In-place'):
        create_identity_ledger_copy(source, source, account_markets={})
    destination.write_bytes(b'preserve-existing-content')
    with pytest.raises(FileExistsError):
        create_identity_ledger_copy(source, destination, account_markets={})
    assert destination.read_bytes() == b'preserve-existing-content'
    assert source.read_bytes() == before


def test_repeated_migration_cannot_overwrite_or_remigrate_candidate(tmp_path):
    source, destination = tmp_path/'legacy.db', tmp_path/'candidate.db'
    legacy_fixture(source)
    markets = {'synthetic-us': 'US', 'synthetic-kr': 'KR'}
    create_identity_ledger_copy(source, destination, account_markets=markets)
    before = destination.read_bytes()
    with pytest.raises(FileExistsError):
        create_identity_ledger_copy(source, destination, account_markets=markets)
    with pytest.raises(ValueError, match='already migrated'):
        create_identity_ledger_copy(destination, tmp_path/'another.db', account_markets=markets)
    assert destination.read_bytes() == before
    assert not (tmp_path/'another.db').exists()


@pytest.mark.parametrize("child_name,parent_name,action", [
    ("cascade_child", "trade_ledger", "CASCADE"),
    ('audit "rows', "TRADE_LEDGER", "SET NULL"),
    ("restrict_child", "trade_ledger", "RESTRICT"),
    ("no_action_child", "trade_ledger", "NO ACTION"),
])
def test_copy_rejects_inbound_foreign_keys_before_rebuilding_parent(
        tmp_path, child_name, parent_name, action):
    source, destination = tmp_path / "legacy.db", tmp_path / "candidate.db"
    legacy_fixture(source)
    quoted_child = '"' + child_name.replace('"', '""') + '"'
    with closing(sqlite3.connect(source)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        with db:
            db.execute(
                f"CREATE TABLE {quoted_child} (id TEXT PRIMARY KEY, "
                f'trade_id TEXT REFERENCES "{parent_name}"(id) ON DELETE {action})'
            )
            trade_id = db.execute("SELECT id FROM trade_ledger WHERE type='buy'").fetchone()[0]
            db.execute(f"INSERT INTO {quoted_child} VALUES (?,?)", ("preserve-me", trade_id))
        before_child = db.execute(f"SELECT * FROM {quoted_child}").fetchall()
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    before_bytes, before_rows = source.read_bytes(), rows(source)
    with pytest.raises(ValueError, match="inbound foreign key"):
        create_identity_ledger_copy(
            source, destination,
            account_markets={"synthetic-us": "US", "synthetic-kr": "KR"},
        )
    assert source.read_bytes() == before_bytes
    assert rows(destination) == before_rows
    with closing(sqlite3.connect(destination)) as db:
        assert db.execute(f"SELECT * FROM {quoted_child}").fetchall() == before_child
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE name='order_identities'"
        ).fetchall() == []


def test_copy_preserves_an_unrelated_extra_table(tmp_path):
    source, destination = tmp_path / "legacy.db", tmp_path / "candidate.db"
    legacy_fixture(source)
    with closing(sqlite3.connect(source)) as db:
        with db:
            db.execute("CREATE TABLE audit_notes (id TEXT PRIMARY KEY, note TEXT NOT NULL)")
            db.execute("INSERT INTO audit_notes VALUES (?,?)", ("keep", "unrelated evidence"))
    before_bytes = source.read_bytes()
    create_identity_ledger_copy(
        source, destination,
        account_markets={"synthetic-us": "US", "synthetic-kr": "KR"},
    )
    assert source.read_bytes() == before_bytes
    with closing(sqlite3.connect(destination)) as db:
        assert db.execute("SELECT * FROM audit_notes").fetchall() == [
            ("keep", "unrelated evidence"),
        ]
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
