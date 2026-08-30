import pytest

from tests.support.telegram_double import make_telegram_double


@pytest.fixture(autouse=True)
def isolated_fixed_port_degraded_state(monkeypatch, tmp_path):
    from src.core import broker_http

    monkeypatch.setattr(broker_http, "DATA_DIR", tmp_path)
    with broker_http._FIXED_PORT_DEGRADED_STATES_LOCK:
        broker_http._FIXED_PORT_DEGRADED_STATES.clear()
    yield
    with broker_http._FIXED_PORT_DEGRADED_STATES_LOCK:
        broker_http._FIXED_PORT_DEGRADED_STATES.clear()


@pytest.fixture
def telegram_double():
    return make_telegram_double()
