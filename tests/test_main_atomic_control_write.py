import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src import main as main_module


class _StopLoop(Exception):
    pass


class MainAtomicControlWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_mock_startup_does_not_create_or_import_legacy_controls(self):
        with self.subTest("mock startup grants no dashboard authority"):
            await self._run_atomic_control_test()

    async def _run_atomic_control_test(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            data_dir.mkdir()
            account_id = "us_mock"
            symbol = "SOXL"
            config = {
                "market": "US",
                "symbol": symbol,
                "auto_buy": {"enabled": True},
                "auto_sell": {"enabled": False},
                "first_buy": {"amount": 1},
                "buy_steps": [],
                "sell_steps": [],
            }
            settings_path = data_dir / f"dashboard_settings_{account_id}.json"
            settings_path.write_text(
                json.dumps({"profiles": [{"enabled": True, "config": config}]}),
                encoding="utf-8",
            )
            control_path = data_dir / f"dashboard_control_{account_id}_{symbol}.json"
            legacy_bytes = b'{"legacy":"preserve"}\n'
            control_path.write_bytes(legacy_bytes)

            ctx = SimpleNamespace(
                account_id=account_id,
                client=SimpleNamespace(market="US"),
                price_feed_obj=object(),
                logger=Mock(),
            )
            ctx.logger.bind.return_value = ctx.logger

            registry = Mock()
            registry.running_symbols.return_value = ()
            registry.claim.return_value = False
            registry.request_stop.return_value = False

            with patch.object(main_module, "DATA_DIR", data_dir), \
                 patch.object(main_module, "make_price_feed", new=AsyncMock(return_value=object())), \
                 patch.object(main_module, "run_quote_health_monitor", new=AsyncMock(return_value=None)), \
                 patch.object(main_module, "DispatchClearanceService", return_value=Mock()), \
                 patch.object(main_module, "atomic_write_json", autospec=True) as atomic_write_json, \
                 patch.object(main_module.asyncio, "sleep", new=AsyncMock(side_effect=_StopLoop)):
                with self.assertRaises(_StopLoop):
                    await main_module.run_symbol_engines(ctx, Mock(), registry)

            atomic_write_json.assert_not_called()
            self.assertEqual(control_path.read_bytes(), legacy_bytes)
            self.assertFalse((data_dir / f"dashboard_control_snapshot_{account_id}.json").exists())
