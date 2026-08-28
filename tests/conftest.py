import pytest

from tests.support.telegram_double import make_telegram_double


@pytest.fixture
def telegram_double():
    return make_telegram_double()
