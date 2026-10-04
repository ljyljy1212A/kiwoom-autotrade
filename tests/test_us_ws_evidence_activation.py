"""Synthetic mock activation; no worker launch or network."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.realtime_feed import KiwoomRealtimeFeed
from src.core.us_ws_evidence_activation import MODE_ENV, PATH_ENV, JOURNAL_NAME, attach_us_mock_f5_journal

BROKER = "1234567801"


def context(**changes):
    client = SimpleNamespace(market=changes.get("market", "US"),
                             mode=changes.get("mode", "mock"), account_no=BROKER)
    return SimpleNamespace(account_id=changes.get("account_id", "us_mock"), client=client, logger=None)


def feed(ctx):
    return SimpleNamespace(realtime=KiwoomRealtimeFeed(ctx.client))


def test_default_mode_does_not_read_account_or_create_files(tmp_path):
    class ForbiddenAccount:
        market, mode = "US", "mock"
        @property
        def account_no(self):
            raise AssertionError("Unexpected account lookup")
    ctx = SimpleNamespace(account_id="us_mock", client=ForbiddenAccount())
    instance = feed(ctx)
    attach_us_mock_f5_journal(instance, ctx, {}, data_dir=tmp_path)
    assert instance.realtime.f5_evidence_status["state"] == "disabled"
    assert list(tmp_path.iterdir()) == []


def test_explicit_create_then_read_only_open_and_no_implicit_fallback(tmp_path):
    ctx = context()
    attach_us_mock_f5_journal(feed(ctx), ctx, {MODE_ENV: "create"}, data_dir=tmp_path)
    path = tmp_path / JOURNAL_NAME
    before = path.read_bytes()
    instance = feed(ctx)
    attach_us_mock_f5_journal(instance, ctx, {MODE_ENV: "open"}, data_dir=tmp_path)
    assert instance.realtime.f5_evidence_status["state"] == "configured"
    assert instance.realtime._task is None
    assert path.read_bytes() == before
    with pytest.raises(FileExistsError):
        attach_us_mock_f5_journal(feed(ctx), ctx, {MODE_ENV: "create"}, data_dir=tmp_path)
    assert path.read_bytes() == before


def test_missing_open_target_is_not_created(tmp_path):
    import sqlite3
    ctx = context()
    with pytest.raises(sqlite3.OperationalError):
        attach_us_mock_f5_journal(feed(ctx), ctx, {MODE_ENV: "open"}, data_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("changes", [
    {"mode": "real"}, {"market": "KR"}, {"account_id": "other"},
])
def test_scope_mismatch_fails_before_account_lookup_or_journal_access(tmp_path, changes):
    class ForbiddenAccount:
        market, mode = changes.get("market", "US"), changes.get("mode", "mock")
        @property
        def account_no(self):
            raise AssertionError("Unexpected account lookup")
    ctx = SimpleNamespace(account_id=changes.get("account_id", "us_mock"), client=ForbiddenAccount())
    with pytest.raises(ValueError, match="us_mock / US / mock"):
        attach_us_mock_f5_journal(feed(ctx), ctx, {MODE_ENV: "create"}, data_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("settings", [
    {MODE_ENV: "on"}, {MODE_ENV: None}, {MODE_ENV: True},
    {MODE_ENV: "create", PATH_ENV: ""}, {MODE_ENV: "open", PATH_ENV: "relative.sqlite"},
    {MODE_ENV: "create", PATH_ENV: 123}, {PATH_ENV: "unused.sqlite"},
])
def test_invalid_settings_fail_without_file_changes(tmp_path, settings):
    ctx = context()
    with pytest.raises(ValueError):
        attach_us_mock_f5_journal(feed(ctx), ctx, settings, data_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_rest_only_and_relative_default_path_fail_without_creation(tmp_path):
    ctx = context()
    with pytest.raises(ValueError, match="WebSocket"):
        attach_us_mock_f5_journal(SimpleNamespace(realtime=None), ctx, {MODE_ENV: "create"}, data_dir=tmp_path)
    with pytest.raises(ValueError, match="absolute path"):
        attach_us_mock_f5_journal(feed(ctx), ctx, {MODE_ENV: "create"}, data_dir=Path("relative"))
    assert list(tmp_path.iterdir()) == []


def test_explicit_path_and_wrong_journal_scope(tmp_path):
    ctx = context()
    path = tmp_path / "explicit.sqlite"
    attach_us_mock_f5_journal(feed(ctx), ctx, {MODE_ENV: "create", PATH_ENV: str(path)}, data_dir=tmp_path)
    before = path.read_bytes()
    ctx.client.account_no = "9999999901"
    with pytest.raises(ValueError, match="scope"):
        attach_us_mock_f5_journal(feed(ctx), ctx, {MODE_ENV: "open", PATH_ENV: str(path)}, data_dir=tmp_path)
    assert path.read_bytes() == before


def test_main_attaches_before_feed_start_without_importing_entrypoint(tmp_path):
    # Extract the function to avoid main environment and logging side effects.
    tree = ast.parse((Path(__file__).resolve().parents[1] / "src/main.py").read_text(encoding="utf-8"))
    function = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "make_price_feed")
    ctx = context()
    settings = {MODE_ENV: "create"}
    calls = []

    class FakePriceFeed:
        def __init__(self, client, **kwargs):
            self.realtime = KiwoomRealtimeFeed(client)
        def start(self):
            assert self.realtime.f5_evidence_status["state"] == "configured"
            calls.append("start")
        async def get_price(self, symbol):
            raise AssertionError("Unexpected price request")

    namespace = {"os": SimpleNamespace(environ=settings), "DATA_DIR": tmp_path,
                 "PriceFeed": FakePriceFeed, "attach_us_mock_f5_journal": attach_us_mock_f5_journal}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<isolated make_price_feed>", "exec"), namespace)

    async def scenario():
        result = await namespace["make_price_feed"](ctx)
        assert result == ctx.price_feed_obj.get_price
        assert calls == ["start"]
        with pytest.raises(FileExistsError):
            await namespace["make_price_feed"](ctx)
        assert calls == ["start"]
        settings[MODE_ENV] = "open"
        await namespace["make_price_feed"](ctx)
        assert calls == ["start", "start"]
    asyncio.run(scenario())


def test_idle_capture_registers_all_account_f5_once_per_connection(tmp_path, monkeypatch):
    import json
    ctx = context()
    instance = feed(ctx)
    attach_us_mock_f5_journal(instance, ctx, {MODE_ENV: "create"}, data_dir=tmp_path)
    realtime = instance.realtime

    class FakeSocket:
        def __init__(self):
            self.messages = []
        async def send(self, raw):
            self.messages.append(json.loads(raw))

    async def connection():
        socket = FakeSocket()
        realtime._stop.clear()
        loops = 0
        async def tick(_):
            nonlocal loops
            loops += 1
            if loops == 2:
                realtime._stop.set()
        monkeypatch.setattr("src.core.realtime_feed.asyncio.sleep", tick)
        await realtime._register_loop(socket)
        return socket.messages

    expected = {"trnm": "REG", "grp_no": "2", "refresh": "1",
                "data": [{"item": [""], "type": ["F5"]}]}
    assert asyncio.run(connection()) == [expected]
    assert asyncio.run(connection()) == [expected]
    assert realtime.subscribed_symbols() == ()


@pytest.mark.parametrize("capture", [False, True])
def test_quote_registration_preserves_default_and_avoids_duplicate_f5(tmp_path, monkeypatch, capture):
    import json
    from src.core.realtime_feed import REALTIME_TYPE
    ctx = context()
    instance = feed(ctx)
    if capture:
        attach_us_mock_f5_journal(instance, ctx, {MODE_ENV: "create"}, data_dir=tmp_path)
    realtime = instance.realtime
    realtime.subscribe("NVDA")
    messages = []
    class FakeSocket:
        async def send(self, raw):
            messages.append(json.loads(raw))
    async def tick(_):
        realtime._stop.set()
    monkeypatch.setattr("src.core.realtime_feed.asyncio.sleep", tick)
    asyncio.run(realtime._register_loop(FakeSocket()))
    quotes = [m for m in messages if m["grp_no"] == "1"]
    assert len(quotes) == 1
    assert quotes[0]["data"][0]["item"] == ["NVDA"]
    assert REALTIME_TYPE in quotes[0]["data"][0]["type"]
    assert ("F5" in quotes[0]["data"][0]["type"]) is (not capture)
    assert len(messages) == (2 if capture else 1)


@pytest.mark.parametrize(("account_id", "market"), [
    ("us_mock", "US"), ("us_mock", "us"), ("kr_mock", "US"), ("other_mock", "US"),
])
def test_startup_environment_enables_only_exact_us_mock_scope(tmp_path, account_id, market):
    from src.core.us_ws_evidence_activation import configure_us_mock_f5_environment
    settings = {MODE_ENV: "create", PATH_ENV: "stale.sqlite", "UNRELATED": "kept"}
    configure_us_mock_f5_environment(account_id, market, settings, data_dir=tmp_path)
    if (account_id, market) == ("us_mock", "US"):
        assert settings[MODE_ENV] == "open"
        assert settings[PATH_ENV] == str(tmp_path / JOURNAL_NAME)
    else:
        assert MODE_ENV not in settings
        assert PATH_ENV not in settings
    assert settings["UNRELATED"] == "kept"
    assert list(tmp_path.iterdir()) == []


def test_main_applies_account_capture_policy_before_worker_engines_start():
    tree = ast.parse((Path(__file__).resolve().parents[1] / "src/main.py").read_text(encoding="utf-8"))
    main = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "main")
    calls = [n for n in ast.walk(main) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)
             and n.func.id == "configure_us_mock_f5_environment"]
    assert len(calls) == 1
    call = calls[0]
    assert ast.unparse(call.args[0]) == "worker_account_id"
    assert ast.unparse(call.args[1]) == "worker_market"
    launch = next(n for n in ast.walk(main) if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Attribute) and n.func.attr == "create_task"
                  and n.args and isinstance(n.args[0], ast.Call)
                  and isinstance(n.args[0].func, ast.Name) and n.args[0].func.id == "_run_engines")
    assert call.lineno < launch.lineno
