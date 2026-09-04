import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src import main as worker_main


def test_worker_heartbeat_publishes_running_when_account_is_not_degraded():
    identity = worker_main.WorkerIdentity("kr_mock", "KR", 123, "instance", "started")
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    writer = Mock()
    with patch.object(worker_main.asyncio, "sleep", sleep), \
         patch.object(worker_main, "get_fixed_port_degraded_state", return_value=None), \
         patch.object(worker_main, "_write_worker_status", writer):
        try:
            asyncio.run(worker_main._publish_worker_heartbeat(identity, SimpleNamespace(running_symbols=lambda _: ("005930",)), interval_sec=0))
        except asyncio.CancelledError:
            pass

    writer.assert_called_once_with(identity, "RUNNING", ["005930"])


def test_worker_heartbeat_publishes_degraded_state_without_changing_liveness():
    identity = worker_main.WorkerIdentity("kr_mock", "KR", 123, "instance", "started")
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    writer = Mock()
    with patch.object(worker_main.asyncio, "sleep", sleep), \
         patch.object(worker_main, "get_fixed_port_degraded_state", return_value=object()), \
         patch.object(worker_main, "_write_worker_status", writer):
        try:
            asyncio.run(worker_main._publish_worker_heartbeat(identity, SimpleNamespace(running_symbols=lambda _: ()), interval_sec=0))
        except asyncio.CancelledError:
            pass

    writer.assert_called_once_with(identity, "DEGRADED_FIXED_PORT", [])


def test_worker_status_schema_includes_active_symbols_and_writes_atomically():
    identity = worker_main.WorkerIdentity("kr_mock", "KR", 123, "instance", "started")
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        with patch.object(worker_main, "DATA_DIR", data_dir), patch.object(worker_main, "SYS_LOG"):
            worker_main._write_worker_status(identity, "RUNNING", ["005930"])

        payload = json.loads((data_dir / "worker_kr_mock.status.json").read_text(encoding="utf-8"))

    assert payload["account"] == "kr_mock"
    assert payload["market"] == "KR"
    assert payload["pid"] == 123
    assert payload["instanceId"] == "instance"
    assert payload["startedAt"] == "started"
    assert payload["state"] == "RUNNING"
    assert payload["active_symbols"] == ["005930"]
    assert "updatedAt" in payload
