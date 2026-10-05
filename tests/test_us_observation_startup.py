"""Prepared temporary files, synthetic worker calls, and denied network access."""
import asyncio
import os
import socket
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.core import us_observation_startup as module, worker_environment
from src.core.us_observation_startup import OBSERVATION_KEYS, open_us_observation_session
from src.data.us_observation_checkpoint import LOCK_CONTENT, checkpoint_bytes
from src.data.trade_ledger import TradeLedgerStore
from src.data.trade_ledger_migration import create_identity_ledger_copy
from tests.test_us_operational_observation_store import (
    BINDING, JOURNAL, count, empty_head, inputs,
    db as db,
)
from tests.test_worker_runtime_root import routed_environment as routed_environment


@pytest.fixture
def async_runner():
    # Windows builds its internal socketpair when creating the loop. Prepare
    # that local test infrastructure before denying all test-time connects.
    with asyncio.Runner() as runner:
        runner.get_loop()
        yield runner.run


@pytest.fixture(autouse=True)
def deny_network(monkeypatch, async_runner):
    def denied(*args, **kwargs):
        raise AssertionError("Network is outside the observation startup test")

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)


@pytest.fixture
def prepared(db, tmp_path):
    source = tmp_path / "legacy.db"
    TradeLedgerStore(source, "us_mock").close()
    identity = tmp_path / "trades_us_mock.db"
    create_identity_ledger_copy(source, identity, account_markets={"us_mock": "US"})
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_bytes(checkpoint_bytes(empty_head()))
    checkpoint.with_name(checkpoint.name + ".lock").write_bytes(LOCK_CONTENT)
    return {
        "KIWOOM_RUNTIME_ROOT": str(tmp_path),
        "synthetic_identity_path": str(identity),
        OBSERVATION_KEYS[0]: "true",
        OBSERVATION_KEYS[1]: db.execute("PRAGMA database_list").fetchone()[2],
        OBSERVATION_KEYS[2]: str(checkpoint),
        OBSERVATION_KEYS[3]: JOURNAL, OBSERVATION_KEYS[4]: BINDING,
    }


def opened(environment, **scope):
    return open_us_observation_session(
        account_id=scope.get("account", "us_mock"), market=scope.get("market", "US"),
        mode=scope.get("mode", "mock"), environment=environment,
        identity_ledger_path=environment.get("synthetic_identity_path"),
    )


@pytest.mark.parametrize("environment", [{}, {OBSERVATION_KEYS[0]: "false"}])
def test_disabled_startup_opens_nothing(environment, monkeypatch):
    monkeypatch.setattr(module.sqlite3, "connect", Mock(side_effect=AssertionError("unexpected open")))
    assert opened(environment, account="kr_mock", market="KR") is None
    module.sqlite3.connect.assert_not_called()


@pytest.mark.parametrize("scope", [{"account": "other"}, {"market": "KR"}, {"mode": "real"}])
def test_scope_mismatch_never_opens_database(prepared, monkeypatch, scope):
    monkeypatch.setattr(module.sqlite3, "connect", Mock(side_effect=AssertionError("unexpected open")))
    with pytest.raises(RuntimeError, match="startup refused"):
        opened(prepared, **scope)
    module.sqlite3.connect.assert_not_called()


@pytest.mark.parametrize("key,value", [
    (OBSERVATION_KEYS[0], "TRUE"), (OBSERVATION_KEYS[0], "yes"),
    (OBSERVATION_KEYS[1], "relative.sqlite"), (OBSERVATION_KEYS[2], "relative.json"),
    (OBSERVATION_KEYS[3], "invalid"), (OBSERVATION_KEYS[4], ""),
    ("KIWOOM_RUNTIME_ROOT", ""),
])
def test_invalid_configuration_preserves_prepared_files(prepared, db, key, value):
    before = tuple(db.iterdump())
    prepared[key] = value
    with pytest.raises(RuntimeError, match="startup refused"):
        opened(prepared)
    assert tuple(db.iterdump()) == before


def test_missing_database_is_not_created(prepared, tmp_path):
    missing = tmp_path / "missing.sqlite"
    prepared[OBSERVATION_KEYS[1]] = str(missing)
    with pytest.raises(RuntimeError):
        opened(prepared)
    assert not missing.exists()


def test_prepared_session_records_and_reopens_against_checkpoint(prepared, db):
    session = opened(prepared)
    orders, responses = inputs()
    result = session.adapter.observe_cycle(orders=orders, responses=responses)
    assert result.state == "OBSERVED" and result.persistence_confirmed
    assert not result.economic_ingestion_allowed and not result.operational_trading_allowed
    assert count(db) == 1
    connection = session._connection
    session.close()
    session.close()
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")
    reopened = opened(prepared)
    assert reopened.adapter.sink.recover().anchor_verified
    reopened.close()


def test_failed_head_validation_closes_connection_without_repair(prepared, db, monkeypatch):
    checkpoint = module.Path(prepared[OBSERVATION_KEYS[2]])
    checkpoint.write_bytes(b"invalid\n")
    connections = []
    connect = module.sqlite3.connect

    def tracked(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(module.sqlite3, "connect", tracked)
    with pytest.raises(RuntimeError):
        opened(prepared)
    assert len(connections) == 2 and count(db) == 0
    assert checkpoint.read_bytes() == b"invalid\n"
    with pytest.raises(sqlite3.ProgrammingError):
        connections[0].execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        connections[1].execute("SELECT 1")


@pytest.mark.parametrize("kind", ["missing", "legacy"])
def test_identity_ledger_is_required_without_creation_or_migration(prepared, tmp_path, kind):
    target = tmp_path / "unprepared.db"
    if kind == "legacy":
        TradeLedgerStore(target, "us_mock").close()
    prepared["synthetic_identity_path"] = str(target)
    with pytest.raises(RuntimeError, match="startup refused"):
        opened(prepared)
    if kind == "missing":
        assert not target.exists()
    else:
        connection = sqlite3.connect(target)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        connection.close()


@pytest.mark.parametrize("pinned", [False, True])
def test_runtime_dotenv_cannot_activate_or_change_observation_pins(routed_environment, monkeypatch, pinned):
    root, _ = routed_environment
    expected = {key: "launch-pin" for key in OBSERVATION_KEYS} if pinned else {}
    for key in OBSERVATION_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in expected.items():
        monkeypatch.setenv(key, value)
    (root / ".env").write_text(
        "\n".join(f"{key}=dotenv-value" for key in OBSERVATION_KEYS) + "\n", encoding="utf-8",
    )
    worker_environment.load_worker_environment()
    assert {key: os.environ[key] for key in OBSERVATION_KEYS if key in os.environ} == expected


def test_invalid_preparation_refuses_before_worker_lock_or_telegram(prepared, monkeypatch, async_runner):
    from src import main as entry

    for key, value in prepared.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ACCOUNT_FILTER", "us_mock")
    monkeypatch.setenv("MARKET_INSTANCE", "US")
    monkeypatch.setenv(OBSERVATION_KEYS[1], str(module.Path(prepared[OBSERVATION_KEYS[1]]).with_name("missing.sqlite")))
    monkeypatch.setattr(entry, "validate_routed_account", Mock())
    monkeypatch.setattr(entry, "_worker_source_identity", Mock())
    monkeypatch.setattr(entry, "_validate_routed_worker_source_identity", Mock())
    client = SimpleNamespace(market="US", mode="mock", close=AsyncMock())
    monkeypatch.setattr(entry, "load_accounts", Mock(return_value=[SimpleNamespace(account_id="us_mock", client=client)]))
    monkeypatch.setattr(entry, "configure_us_mock_f5_environment", Mock())
    lock = Mock()
    monkeypatch.setattr(entry, "_worker_lock", Mock(return_value=lock))
    telegram = Mock(side_effect=AssertionError("Telegram must not start"))
    monkeypatch.setattr(entry, "TelegramController", telegram)
    monkeypatch.setattr(entry.argparse.ArgumentParser, "parse_args", lambda _: SimpleNamespace(market="US"))
    with pytest.raises(RuntimeError, match="startup refused"):
        async_runner(entry.main())
    lock.acquire.assert_not_called()
    telegram.assert_not_called()
    client.close.assert_awaited_once()


def test_symbol_orchestration_injects_the_preflighted_adapter(prepared, monkeypatch, async_runner):
    from src import main as entry
    from tests.test_execution_row_skip_logging import _config

    session = opened(prepared)
    seen = []
    drained = []
    constructed = asyncio.Event()
    config = dict(_config(), market="US", symbol="AAPL")
    ctx = SimpleNamespace(account_id="us_mock", client=SimpleNamespace(market="US"))

    class Engine:
        def __init__(self, *args, **kwargs):
            seen.append(kwargs["us_observation_adapter"])
            constructed.set()

        async def run(self):
            await asyncio.Future()

        async def stop_us_observation_tasks(self):
            drained.append(True)

    # AccountContext is a dataclass in production. Supply a synthetic snapshot
    # here so orchestration cannot construct a broker client or load accounts.
    monkeypatch.setattr(entry, "replace", lambda original, **kwargs: SimpleNamespace(**kwargs))
    ctx.logger = Mock()
    monkeypatch.setattr(entry, "AccountEngine", Engine)
    monkeypatch.setattr(entry, "make_price_feed", AsyncMock(return_value=None))
    monkeypatch.setattr(entry, "run_quote_health_monitor", AsyncMock())
    monkeypatch.setattr(entry, "_enabled_symbol_configs", Mock(return_value=[config]))
    monkeypatch.setattr(entry, "_dashboard_settings_payload", Mock(return_value={}))
    monkeypatch.setattr(entry, "DispatchClearanceService", Mock())
    ctx.price_feed_obj = None

    async def exercise():
        task = asyncio.create_task(entry.run_symbol_engines(
            ctx, Mock(), entry.SymbolEngineRegistry(), observation_session=session,
        ))
        try:
            await asyncio.wait_for(constructed.wait(), timeout=2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    try:
        async_runner(exercise())
        assert seen == [session.adapter]
        assert drained == [True]
    finally:
        session.close()
