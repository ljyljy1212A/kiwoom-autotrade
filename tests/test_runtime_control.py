from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from src.core.control_state import (
    read_control_state,
    write_control_state,
    write_fixed_port_degraded_event,
    write_pause_clear_event,
)
from src.core.engine import AccountEngine


class RuntimeControlRefreshTest(unittest.TestCase):
    def test_account_engine_refreshes_auto_trading_from_control_file(self):
        engine = object.__new__(AccountEngine)
        engine.ctx = SimpleNamespace(account_id="kr_mock")
        engine.ctx.logger = Mock()
        engine.data_dir = Path(tempfile.mkdtemp())
        engine._auto_trading_enabled = False

        write_control_state("kr_mock", auto_trading_enabled=True, data_dir=engine.data_dir)
        engine._refresh_runtime_control()

        self.assertTrue(engine._auto_trading_enabled)
        engine.ctx.logger.info.assert_called_once_with(
            "auto_trading_enabled changed: false -> true (source: control file)"
        )

        engine._refresh_runtime_control()
        engine.ctx.logger.info.assert_called_once()

    def test_control_switch_preserves_existing_account_events(self):
        data_dir = Path(tempfile.mkdtemp())
        fixed_port_event = write_fixed_port_degraded_event(
            "kr_mock", "entered", "test", datetime.now(timezone.utc), data_dir=data_dir,
        )
        pause_clear_event = write_pause_clear_event(
            "kr_mock", "broker_quantity_unattributed", data_dir=data_dir,
        )

        write_control_state("kr_mock", auto_trading_enabled=False, data_dir=data_dir)

        state = read_control_state("kr_mock", data_dir=data_dir)
        self.assertFalse(state["auto_trading_enabled"])
        self.assertEqual(state["fixed_port_event"], fixed_port_event)
        self.assertEqual(state["pause_clear_event"], pause_clear_event)


if __name__ == "__main__":
    unittest.main()
