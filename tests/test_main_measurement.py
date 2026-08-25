import asyncio
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src import main as main_module


class MeasurementCycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_connect_deadline_logs_and_reraises_oserror(self):
        failure = OSError(10048, "address already in use")
        failure.winerror = 10048
        logged = Mock()

        with patch.object(
            main_module,
            "_measurement_open_initial_connect",
            new=AsyncMock(side_effect=failure),
        ), patch.object(
            main_module.time,
            "monotonic",
            side_effect=[0.0, main_module._MEASUREMENT_INITIAL_CONNECT_MAX_WAIT_SEC],
        ), patch.object(main_module.asyncio, "sleep", new=AsyncMock()) as sleep, patch.object(
            main_module.SYS_LOG, "opt", return_value=logged
        ) as log_opt:
            with self.assertRaises(OSError) as raised:
                await main_module._run_measurement_cycle(
                    cycle_id="test",
                    schedule=(),
                    gate=Mock(),
                    host="127.0.0.1",
                    port=443,
                    local_port=443,
                    path=Path("unused.jsonl"),
                    worker_identity=Mock(),
                )

        self.assertIs(raised.exception, failure)
        log_opt.assert_called_once_with(exception=failure)
        logged.error.assert_called_once_with(
            "Fixed-port measurement initial connect failed"
        )
        sleep.assert_not_awaited()
