from types import SimpleNamespace
import asyncio
import unittest

from src.notify.telegram_bot import TelegramController


class _Logger:
    def __init__(self):
        self.info_messages = []
        self.warning_messages = []

    def info(self, message):
        self.info_messages.append(message)

    def warning(self, message):
        self.warning_messages.append(message)


class _Bot:
    def __init__(self, failure=None):
        self.failure = failure

    async def send_message(self, **_kwargs):
        if self.failure:
            raise self.failure


class TelegramNotificationTest(unittest.TestCase):
    def test_confirmed_fill_logs_attempt_and_api_acceptance_with_order_id(self):
        async def scenario():
            logger = _Logger()
            controller = object.__new__(TelegramController)
            controller.chat_id = "test-chat"
            controller.logger = logger
            controller.app = SimpleNamespace(bot=_Bot())

            await controller.notify_fill("SELL", "IREN", 21, 46.76, "000003457")

            self.assertTrue(any("attempt: fill-confirmed side=SELL symbol=IREN order_id=000003457" in m for m in logger.info_messages))
            self.assertTrue(any("accepted by API: fill-confirmed side=SELL symbol=IREN order_id=000003457" in m for m in logger.info_messages))

        asyncio.run(scenario())

    def test_notification_failure_is_logged_and_remains_fail_open(self):
        async def scenario():
            logger = _Logger()
            controller = object.__new__(TelegramController)
            controller.chat_id = "test-chat"
            controller.logger = logger
            controller.app = SimpleNamespace(bot=_Bot(RuntimeError("network unavailable")))

            delivered = await controller.safe_send("test", event="fill-confirmed side=SELL symbol=IREN order_id=42")

            self.assertFalse(delivered)
            self.assertTrue(any("trading continues: fill-confirmed side=SELL symbol=IREN order_id=42" in m for m in logger.warning_messages))

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
