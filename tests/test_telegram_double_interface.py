from src.notify.telegram_bot import TelegramController
from tests.support.telegram_double import make_telegram_double


EXPECTED_PUBLIC_METHODS = frozenset(
    {
        "safe_send",
        "notify_order",
        "notify_fill",
        "notify_error",
        "notify_balance_change",
        "notify_symbol_closed",
        "notify_symbol_reopened",
        "notify_worker_started",
        "start_polling",
        "stop",
    }
)


def test_telegram_double_matches_public_controller_interface():
    public_methods = frozenset(
        name
        for name, value in vars(TelegramController).items()
        if callable(value) and not name.startswith("_")
    )
    assert public_methods == EXPECTED_PUBLIC_METHODS

    double = make_telegram_double()
    missing = sorted(name for name in EXPECTED_PUBLIC_METHODS if not hasattr(double, name))
    assert not missing, f"telegram double missing methods: {missing}"
