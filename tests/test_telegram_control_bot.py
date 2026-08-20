from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.core.control_state import write_control_state
from src.notify import telegram_control_bot as bot_module
from src.notify.telegram_control_bot import AccountInfo, TelegramControlBot


class _Logger:
    def __init__(self):
        self.info_messages = []
        self.warning_messages = []

    def info(self, message):
        self.info_messages.append(message)

    def warning(self, message):
        self.warning_messages.append(message)


class _Message:
    def __init__(self):
        self.replies = []
        self.edits = []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))


class _CallbackQuery:
    def __init__(self, data):
        self.data = data
        self.message = _Message()
        self.answered = False

    async def edit_message_text(self, text, reply_markup=None):
        self.message.edits.append((text, reply_markup))

    async def answer(self):
        self.answered = True


def _bot(chat_ids: set[str], accounts: list[AccountInfo], logger: _Logger) -> TelegramControlBot:
    bot = object.__new__(TelegramControlBot)
    bot.logger = logger
    bot.allowed_chat_ids = chat_ids
    bot.accounts = {account.account_id: account for account in accounts}
    bot._account_order = [account.account_id for account in accounts]
    return bot


class TelegramControlBotTests(unittest.IsolatedAsyncioTestCase):
    async def test_unauthorized_command_is_ignored(self):
        logger = _Logger()
        bot = _bot({"111"}, [AccountInfo("kr_mock", "KR Mock", "KR")], logger)
        message = _Message()
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=999),
            effective_message=message,
            callback_query=None,
        )

        await bot._handle_status(update, SimpleNamespace())

        self.assertEqual(message.replies, [])
        self.assertTrue(any("unauthorized chat_id=999" in item for item in logger.info_messages))

    async def test_unauthorized_callback_is_ignored(self):
        logger = _Logger()
        bot = _bot({"111"}, [AccountInfo("kr_mock", "KR Mock", "KR")], logger)
        query = _CallbackQuery("acct|kr_mock")
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=999),
            effective_message=query.message,
            callback_query=query,
        )

        await bot._handle_callback(update, SimpleNamespace())

        self.assertEqual(query.message.edits, [])
        self.assertFalse(query.answered)

    async def test_status_command_shows_accounts_and_effective_control_state(self):
        logger = _Logger()
        bot = _bot({"111"}, [AccountInfo("kr_mock", "KR Mock", "KR")], logger)
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            with patch.object(bot_module, "DATA_DIR", data_dir), \
                 patch.object(bot_module, "worker_status", return_value={"running": True}), \
                 patch.dict("os.environ", {"AUTO_TRADING_ENABLED": "false"}, clear=False):
                write_control_state("kr_mock", auto_trading_enabled=True, data_dir=data_dir)
                message = _Message()
                update = SimpleNamespace(
                    effective_chat=SimpleNamespace(id=111),
                    effective_message=message,
                    callback_query=None,
                )

                await bot._handle_status(update, SimpleNamespace())

        self.assertEqual(len(message.replies), 1)
        text, markup = message.replies[0]
        self.assertIn("kr_mock: RUNNING / auto_trading: ON", text)
        self.assertIsNotNone(markup)

    async def test_confirm_yes_writes_control_file(self):
        logger = _Logger()
        bot = _bot({"111"}, [AccountInfo("kr_mock", "KR Mock", "KR")], logger)
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            with patch.object(bot_module, "DATA_DIR", data_dir), \
                 patch.object(bot_module, "worker_status", return_value={"running": True}):
                query = _CallbackQuery("confirm|kr_mock|start|yes")
                update = SimpleNamespace(
                    effective_chat=SimpleNamespace(id=111),
                    effective_message=query.message,
                    callback_query=query,
                )

                await bot._handle_callback(update, SimpleNamespace())

                control_path = data_dir / "control" / "kr_mock.control.json"
                payload = json.loads(control_path.read_text(encoding="utf-8"))

        self.assertTrue(payload["auto_trading_enabled"])
        self.assertTrue(query.answered)
        self.assertTrue(any("auto_trading set to ON" in text for text, _ in query.message.edits))

    async def test_unauthorized_reconciliation_clear_callback_is_ignored(self):
        logger = _Logger()
        bot = _bot({"111"}, [AccountInfo("kr_mock", "KR Mock", "KR")], logger)
        query = _CallbackQuery("clear_reconciliation_pause|kr_mock")
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=999),
            effective_message=query.message,
            callback_query=query,
        )

        with patch.object(bot_module, "write_reconciliation_clear_event") as clear_event:
            await bot._handle_callback(update, SimpleNamespace())

        clear_event.assert_not_called()
        self.assertEqual(query.message.edits, [])
        self.assertFalse(query.answered)


if __name__ == "__main__":
    unittest.main()
