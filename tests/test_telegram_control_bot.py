from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.core.control_state import write_control_state
from src.notify import telegram_control_bot as bot_module
from src.notify.telegram_control_bot import (
    AccountInfo,
    TelegramControlBot,
    _is_mock_account,
    _write_startup_status,
    load_operator_labels,
)


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
    bot.operator_labels = {"111": "Johon"}
    return bot


class TelegramControlBotTests(unittest.IsolatedAsyncioTestCase):
    def test_write_startup_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            accounts = [
                AccountInfo("kr_mock", "KR Mock", "KR"),
                AccountInfo("us_mock", "US Mock", "US"),
            ]
            with patch.object(bot_module, "DATA_DIR", data_dir), patch.object(bot_module.os, "getpid", return_value=4321):
                _write_startup_status(accounts)

            payload = json.loads((data_dir / "telegram_control_bot.status.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["pid"], 4321)
            self.assertEqual(payload["role"], "telegram_control_bot")
            self.assertEqual(payload["account_scope"], ["kr_mock", "us_mock"])
            self.assertTrue(payload["started_at"].endswith("+00:00"))

    def test_main_writes_startup_status_before_polling(self):
        accounts = [
            AccountInfo("kr_mock", "KR Mock", "KR"),
            AccountInfo("us_mock", "US Mock", "US"),
        ]
        fake_bot = Mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            with patch.object(bot_module, "DATA_DIR", data_dir), \
                 patch.object(bot_module, "load_dotenv"), \
                 patch.object(bot_module, "get_logger", return_value=Mock()), \
                 patch.object(bot_module, "load_account_info", return_value=accounts), \
                 patch.object(bot_module, "_allowed_chat_ids_from_env", return_value={"111"}), \
                 patch.object(bot_module, "TelegramControlBot", return_value=fake_bot), \
                 patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token"}, clear=False):
                self.assertEqual(bot_module.main(), 0)

            payload = json.loads((data_dir / "telegram_control_bot.status.json").read_text(encoding="utf-8"))

        self.assertGreater(payload["pid"], 0)
        self.assertEqual(payload["role"], "telegram_control_bot")
        self.assertEqual(payload["account_scope"], ["kr_mock", "us_mock"])
        self.assertTrue(payload["started_at"].endswith("+00:00"))
        fake_bot.run.assert_called_once_with()

    def _callback_update(self, data):
        query = _CallbackQuery(data)
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=111),
            effective_message=query.message,
            callback_query=query,
        )
        return query, update

    def test_mock_account_gate(self):
        self.assertTrue(_is_mock_account("kr_mock"))
        self.assertTrue(_is_mock_account("us_mock"))
        self.assertFalse(_is_mock_account("kr_real"))
        self.assertFalse(_is_mock_account("us_real"))

    def test_load_operator_labels(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "accounts.yaml"
            path.write_text('operators:\n  - chat_id: 8648973973\n    label: "Johon"\n', encoding="utf-8")
            self.assertEqual(load_operator_labels(path), {"8648973973": "Johon"})

    def test_attestation_menu_is_mock_only(self):
        logger = _Logger()
        bot = _bot({"111"}, [AccountInfo("kr_mock", "KR Mock", "KR"), AccountInfo("kr_real", "KR Real", "KR")], logger)
        mock_buttons = [button.callback_data for row in bot._account_markup("kr_mock").inline_keyboard for button in row]
        real_buttons = [button.callback_data for row in bot._account_markup("kr_real").inline_keyboard for button in row]
        self.assertIn("attest_menu|kr_mock", mock_buttons)
        self.assertNotIn("attest_menu|kr_real", real_buttons)
        self.assertIn("clear_pause|kr_mock|fixed_port_degraded", mock_buttons)
        self.assertNotIn("clear_pause|kr_real|fixed_port_degraded", real_buttons)

    async def test_real_attestation_callbacks_do_not_open_store(self):
        logger = _Logger()
        bot = _bot({"111"}, [AccountInfo("kr_real", "KR Real", "KR")], logger)
        for data in (
            "attest_pick|kr_real|0",
            "attest_outcome|kr_real|0|filled",
            "attest_reason|kr_real|0|filled|verified_broker_app",
        ):
            query = _CallbackQuery(data)
            update = SimpleNamespace(effective_chat=SimpleNamespace(id=111), effective_message=query.message, callback_query=query)
            with patch.object(bot_module, "order_attempt_store") as store:
                await bot._handle_callback(update, SimpleNamespace())
            store.assert_not_called()
            self.assertTrue(any("mock accounts" in text for text, _ in query.message.edits))

    async def test_unknown_attestation_callbacks_do_not_query_attempts(self):
        logger = _Logger()
        bot = _bot({"111"}, [AccountInfo("kr_mock", "KR Mock", "KR")], logger)
        for data in (
            "attest_menu|unknown",
            "attest_pick|unknown|0",
            "attest_outcome|unknown|0|filled",
            "attest_reason|unknown|0|filled|verified_broker_app",
        ):
            query = _CallbackQuery(data)
            update = SimpleNamespace(effective_chat=SimpleNamespace(id=111), effective_message=query.message, callback_query=query)
            with patch.object(bot_module, "list_unattributed_attempts") as attempts, \
                 patch.object(bot_module, "order_attempt_store") as store:
                await bot._handle_callback(update, SimpleNamespace())
            attempts.assert_not_called()
            store.assert_not_called()
            self.assertTrue(any("Unknown account" in text for text, _ in query.message.edits))

    async def test_attest_pick_excludes_accepted_and_handles_out_of_range(self):
        logger = _Logger()
        bot = _bot({"111"}, [AccountInfo("kr_mock", "KR Mock", "KR")], logger)
        attempt = SimpleNamespace(attempt_id="a" * 32, symbol="SOXL", side="BUY", qty=2)
        query = _CallbackQuery("attest_pick|kr_mock|0")
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=111), effective_message=query.message, callback_query=query)
        with patch.object(bot_module, "list_unattributed_attempts", return_value=[attempt]):
            await bot._handle_callback(update, SimpleNamespace())
        callbacks = [button.callback_data for row in query.message.edits[-1][1].inline_keyboard for button in row]
        self.assertNotIn("attest_outcome|kr_mock|0|accepted", callbacks)

        query = _CallbackQuery("attest_pick|kr_mock|1")
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=111), effective_message=query.message, callback_query=query)
        with patch.object(bot_module, "list_unattributed_attempts", return_value=[attempt]):
            await bot._handle_callback(update, SimpleNamespace())
        self.assertTrue(any("list changed" in text for text, _ in query.message.edits))

    async def test_attest_reason_rejects_unsupported_reason_without_store_write(self):
        logger = _Logger()
        bot = _bot({"111"}, [AccountInfo("kr_mock", "KR Mock", "KR")], logger)
        attempt = SimpleNamespace(attempt_id="a" * 32, symbol="SOXL", side="BUY", qty=2)
        query = _CallbackQuery("attest_reason|kr_mock|0|filled|not_allowed")
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=111), effective_message=query.message, callback_query=query)
        with patch.object(bot_module, "list_unattributed_attempts", return_value=[attempt]), \
             patch.object(bot_module, "order_attempt_store") as store:
            await bot._handle_callback(update, SimpleNamespace())
        store.assert_not_called()
        self.assertTrue(any("Unsupported attestation selection" in text for text, _ in query.message.edits))

    async def test_attest_reason_happy_path_uses_operator_label(self):
        logger = _Logger()
        bot = _bot({"111"}, [AccountInfo("kr_mock", "KR Mock", "KR")], logger)
        attempt = SimpleNamespace(attempt_id="a" * 32, symbol="SOXL", side="BUY", qty=2)
        store = Mock()
        query = _CallbackQuery("attest_reason|kr_mock|0|filled|verified_broker_app")
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=111), effective_message=query.message, callback_query=query)
        with patch.object(bot_module, "list_unattributed_attempts", return_value=[attempt]), \
             patch.object(bot_module, "order_attempt_store", return_value=store):
            await bot._handle_callback(update, SimpleNamespace())
        store.attest_unattributed.assert_called_once_with(
            attempt.attempt_id,
            "Johon",
            bot_module.OrderAttestationOutcome.FILLED,
            "verified_broker_app",
        )
        self.assertTrue(any("Attested SOXL BUY" in text for text, _ in query.message.edits))

    async def test_attest_reason_store_error_is_handled(self):
        logger = _Logger()
        bot = _bot({"111"}, [AccountInfo("kr_mock", "KR Mock", "KR")], logger)
        attempt = SimpleNamespace(attempt_id="a" * 32, symbol="SOXL", side="BUY", qty=2)
        store = Mock()
        store.attest_unattributed.side_effect = RuntimeError("disk failure")
        query = _CallbackQuery("attest_reason|kr_mock|0|filled|verified_broker_app")
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=111), effective_message=query.message, callback_query=query)
        with patch.object(bot_module, "list_unattributed_attempts", return_value=[attempt]), \
             patch.object(bot_module, "order_attempt_store", return_value=store):
            await bot._handle_callback(update, SimpleNamespace())
        self.assertTrue(any("Failed to persist attestation" in text for text, _ in query.message.edits))

    async def test_attest_reason_replay_is_friendly(self):
        logger = _Logger()
        bot = _bot({"111"}, [AccountInfo("kr_mock", "KR Mock", "KR")], logger)
        attempt = SimpleNamespace(attempt_id="a" * 32, symbol="SOXL", side="BUY", qty=2)
        store = Mock()
        store.attest_unattributed.side_effect = ValueError("already attested")
        query = _CallbackQuery("attest_reason|kr_mock|0|filled|verified_broker_app")
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=111), effective_message=query.message, callback_query=query)
        with patch.object(bot_module, "list_unattributed_attempts", return_value=[attempt]), \
             patch.object(bot_module, "order_attempt_store", return_value=store):
            await bot._handle_callback(update, SimpleNamespace())
        self.assertTrue(any("already attested" in text for text, _ in query.message.edits))
        self.assertTrue(query.answered)

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

    async def test_mock_account_selection_and_action_preview_are_allowed(self):
        logger = _Logger()
        bot = _bot({"111"}, [AccountInfo("kr_mock", "KR Mock", "KR")], logger)

        query, update = self._callback_update("acct|kr_mock")
        await bot._handle_callback(update, SimpleNamespace())
        self.assertTrue(any("KR Mock" in text for text, _ in query.message.edits))

        query, update = self._callback_update("action|kr_mock|start")
        await bot._handle_callback(update, SimpleNamespace())
        self.assertTrue(any("Confirm enable auto-trading" in text for text, _ in query.message.edits))

    async def test_mock_reconciliation_clear_is_allowed(self):
        logger = _Logger()
        bot = _bot({"111"}, [AccountInfo("kr_mock", "KR Mock", "KR")], logger)
        with patch.object(bot_module, "write_reconciliation_clear_event") as clear_event:
            query, update = self._callback_update("clear_reconciliation_pause|kr_mock")
            await bot._handle_callback(update, SimpleNamespace())
        clear_event.assert_called_once_with("kr_mock", updated_by="telegram", data_dir=bot_module.DATA_DIR)
        self.assertTrue(any("Requested reconciliation-pause clear" in text for text, _ in query.message.edits))

    async def test_real_account_mutations_are_rejected_and_logged(self):
        logger = _Logger()
        bot = _bot({"111"}, [AccountInfo("kr_real", "KR Real", "KR")], logger)
        for data, callback_kind in (
            ("action|kr_real|start", "action"),
            ("clear_reconciliation_pause|kr_real", "clear_reconciliation_pause"),
            ("clear_pause|kr_real|tranche_rebuild_ambiguous", "clear_pause"),
            ("confirm|kr_real|start|yes", "confirm"),
        ):
            query, update = self._callback_update(data)
            with patch.object(bot_module, "write_control_state") as control_write, \
                 patch.object(bot_module, "write_reconciliation_clear_event") as reconciliation_write, \
                 patch.object(bot_module, "write_pause_clear_event") as pause_write:
                await bot._handle_callback(update, SimpleNamespace())
            control_write.assert_not_called()
            reconciliation_write.assert_not_called()
            pause_write.assert_not_called()
            self.assertTrue(any("not available through Telegram control" in text for text, _ in query.message.edits))
            self.assertTrue(any("account=kr_real" in text and f"callback={callback_kind}" in text for text in logger.warning_messages))

    async def test_real_account_selection_is_rejected_to_root_menu(self):
        logger = _Logger()
        bot = _bot({"111"}, [AccountInfo("kr_real", "KR Real", "KR")], logger)
        query, update = self._callback_update("acct|kr_real")

        await bot._handle_callback(update, SimpleNamespace())

        self.assertTrue(any("not available through Telegram control" in text for text, _ in query.message.edits))
        markup = query.message.edits[-1][1]
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertIn("acct|kr_real", callbacks)
        self.assertNotIn("action|kr_real|start", callbacks)

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

    async def test_authorized_reason_scoped_clear_writes_allowlisted_event(self):
        logger = _Logger()
        bot = _bot({"111"}, [AccountInfo("kr_mock", "KR Mock", "KR")], logger)
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            with patch.object(bot_module, "DATA_DIR", data_dir):
                query = _CallbackQuery("clear_pause|kr_mock|tranche_rebuild_ambiguous")
                update = SimpleNamespace(
                    effective_chat=SimpleNamespace(id=111),
                    effective_message=query.message,
                    callback_query=query,
                )

                await bot._handle_callback(update, SimpleNamespace())

                payload = json.loads(
                    (data_dir / "control" / "kr_mock.control.json").read_text(encoding="utf-8")
                )

        self.assertEqual(payload["pause_clear_event"]["reason"], "tranche_rebuild_ambiguous")
        self.assertTrue(query.answered)

    async def test_fixed_port_clear_callback_writes_durable_allowlisted_reason(self):
        logger = _Logger()
        bot = _bot({"111"}, [AccountInfo("kr_mock", "KR Mock", "KR")], logger)
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            with patch.object(bot_module, "DATA_DIR", data_dir):
                query = _CallbackQuery("clear_pause|kr_mock|fixed_port_degraded")
                update = SimpleNamespace(
                    effective_chat=SimpleNamespace(id=111),
                    effective_message=query.message,
                    callback_query=query,
                )

                await bot._handle_callback(update, SimpleNamespace())

                payload = json.loads(
                    (data_dir / "control" / "kr_mock.control.json").read_text(encoding="utf-8")
                )

        self.assertEqual(payload["pause_clear_event"]["reason"], "fixed_port_degraded")
        self.assertEqual(payload["pause_clear_event"]["updated_by"], "telegram")
        self.assertTrue(query.answered)

    async def test_invalid_reason_callback_does_not_write(self):
        logger = _Logger()
        bot = _bot({"111"}, [AccountInfo("kr_mock", "KR Mock", "KR")], logger)
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            with patch.object(bot_module, "DATA_DIR", data_dir):
                query = _CallbackQuery("clear_pause|kr_mock|not_allowed")
                update = SimpleNamespace(
                    effective_chat=SimpleNamespace(id=111),
                    effective_message=query.message,
                    callback_query=query,
                )

                await bot._handle_callback(update, SimpleNamespace())

        self.assertFalse((data_dir / "control" / "kr_mock.control.json").exists())
        self.assertTrue(query.answered)


if __name__ == "__main__":
    unittest.main()
