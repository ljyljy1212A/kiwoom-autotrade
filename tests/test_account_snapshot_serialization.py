"""Account snapshot publishers must not lose newer or sibling observations."""

from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.core import engine as engine_module
from src.core.engine import AccountEngine


def _engine(data_dir, symbol, gate):
    engine = object.__new__(AccountEngine)
    engine.data_dir = data_dir
    engine._balance_gate = gate
    engine.ctx = SimpleNamespace(
        account_id="us_mock",
        client=SimpleNamespace(market="US"),
        strategy=SimpleNamespace(symbol=symbol),
        position=SimpleNamespace(step=0),
        logger=Mock(),
        currency="USD",
        reporting_currency="KRW",
    )
    engine._next_buy_trigger = lambda: None
    return engine


def test_balance_snapshot_rejects_older_broker_observation(tmp_path):
    gate = SimpleNamespace(published_balance_received_at=0.0)
    active = _engine(tmp_path, "SOXL", gate)
    passive = _engine(tmp_path, "TQQQ", gate)
    newer = {"holdings": [{"symbol": "SOXL", "qty": 4}], "balanceComplete": True}

    passive._publish_passive_balance_snapshot(
        [{"symbol": "SOXL", "qty": 1}], True, 10.0,
    )
    assert gate.published_balance_received_at == 10.0
    assert active._publish_balance_snapshot(newer, 20.0) is True
    passive._publish_passive_balance_snapshot(
        [{"symbol": "SOXL", "qty": 1}], True, 10.0,
    )
    assert active._publish_balance_snapshot({"holdings": []}, 10.0) is False
    assert json.loads((tmp_path / "balance_us_mock.json").read_text(encoding="utf-8")) == newer
    assert gate.published_balance_received_at == 20.0


def test_failed_balance_write_does_not_advance_observation(tmp_path, monkeypatch):
    gate = SimpleNamespace(published_balance_received_at=0.0)
    engine = _engine(tmp_path, "SOXL", gate)
    original_write = engine_module.atomic_write_json

    def fail_write(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(engine_module, "atomic_write_json", fail_write)
    with pytest.raises(OSError, match="disk unavailable"):
        engine._publish_balance_snapshot({"holdings": []}, 20.0)
    assert gate.published_balance_received_at == 0.0

    monkeypatch.setattr(engine_module, "atomic_write_json", original_write)
    assert engine._publish_balance_snapshot({"holdings": []}, 10.0) is True


def test_concurrent_quote_publishers_preserve_both_symbols(tmp_path, monkeypatch):
    gate = SimpleNamespace(published_balance_received_at=0.0)
    first = _engine(tmp_path, "SOXL", gate)
    second = _engine(tmp_path, "TQQQ", gate)
    original_write = engine_module.atomic_write_json
    first_writing = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    second_finished = threading.Event()

    def delayed_write(path, value, **kwargs):
        if path.name == "worker_us_mock.quotes.json" and "SOXL" in value:
            first_writing.set()
            assert release_first.wait(3)
        return original_write(path, value, **kwargs)

    monkeypatch.setattr(engine_module, "atomic_write_json", delayed_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_result = pool.submit(first._record_evaluated_quote, 40.0, "test", time.time())
        assert first_writing.wait(3)

        def publish_second():
            second_started.set()
            second._record_evaluated_quote(50.0, "test", time.time())
            second_finished.set()

        second_result = pool.submit(publish_second)
        assert second_started.wait(3)
        completed_while_first_blocked = second_finished.wait(0.2)
        release_first.set()
        first_result.result(timeout=3)
        second_result.result(timeout=3)

    assert not completed_while_first_blocked
    quotes = json.loads((tmp_path / "worker_us_mock.quotes.json").read_text(encoding="utf-8"))
    assert set(quotes) == {"SOXL", "TQQQ"}
    assert quotes["SOXL"]["price"] == 40.0
    assert quotes["TQQQ"]["price"] == 50.0
