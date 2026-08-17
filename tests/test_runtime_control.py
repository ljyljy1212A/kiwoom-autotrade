from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from src.core.control_state import write_control_state
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


if __name__ == "__main__":
    unittest.main()
