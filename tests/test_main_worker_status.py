import asyncio
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
            asyncio.run(worker_main._publish_worker_heartbeat(identity, interval_sec=0))
        except asyncio.CancelledError:
            pass

    writer.assert_called_once_with(identity, "RUNNING")


def test_worker_heartbeat_publishes_degraded_state_without_changing_liveness():
    identity = worker_main.WorkerIdentity("kr_mock", "KR", 123, "instance", "started")
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    writer = Mock()
    with patch.object(worker_main.asyncio, "sleep", sleep), \
         patch.object(worker_main, "get_fixed_port_degraded_state", return_value=object()), \
         patch.object(worker_main, "_write_worker_status", writer):
        try:
            asyncio.run(worker_main._publish_worker_heartbeat(identity, interval_sec=0))
        except asyncio.CancelledError:
            pass

    writer.assert_called_once_with(identity, "DEGRADED_FIXED_PORT")
