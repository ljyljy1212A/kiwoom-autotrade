from unittest.mock import create_autospec

from src.notify.telegram_bot import TelegramController


def make_telegram_double():
    return create_autospec(TelegramController, instance=True, spec_set=True)
