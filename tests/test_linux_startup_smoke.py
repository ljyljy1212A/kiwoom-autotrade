import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src import main as app_main


@unittest.skipUnless(os.name == "posix", "Linux startup smoke is only relevant on POSIX")
class LinuxStartupSmokeTests(unittest.TestCase):
    def test_main_reaches_non_trading_startup_and_cleans_up(self):
        logger = MagicMock()
        logger.bind.return_value = logger
        logger.info = MagicMock()
        logger.warning = MagicMock()
        client = SimpleNamespace(market="US", close=AsyncMock(return_value=None), token_mgr=None)
        ctx = SimpleNamespace(account_id="us_mock", client=client, logger=logger,
                              risk_manager=SimpleNamespace(logger=None))
        telegram = SimpleNamespace(
            start_polling=AsyncMock(return_value=None),
            stop=AsyncMock(return_value=None),
        )
        worker_lock = MagicMock()
        worker_lock.acquire = MagicMock()
        worker_lock.release = MagicMock()
        worker_lock.is_alive.return_value = True

        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "linux-smoke-test-token",
            "TELEGRAM_CHAT_ID": "linux-smoke-test-chat",
        }), \
             patch.object(app_main.argparse.ArgumentParser, "parse_args",
                          return_value=SimpleNamespace(market="US")), \
             patch.object(app_main, "load_accounts", return_value=[ctx]), \
             patch.object(app_main, "TelegramController", return_value=telegram), \
             patch.object(app_main, "DiscordNotifier", return_value=MagicMock()), \
             patch.object(app_main, "run_symbol_engines", new=AsyncMock(return_value=None)), \
             patch.object(app_main, "ProcessLock", return_value=worker_lock), \
             patch.object(app_main, "_acquire_worker_pid"), \
             patch.object(app_main, "_write_worker_status"):
            asyncio.run(app_main.main())

        worker_lock.acquire.assert_called_once()
        worker_lock.release.assert_called_once()
        telegram.start_polling.assert_awaited_once()
        telegram.stop.assert_awaited_once()
        client.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
