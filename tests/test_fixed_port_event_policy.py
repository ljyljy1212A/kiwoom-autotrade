import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.core.broker_http import clear_fixed_port_degraded_state, enter_fixed_port_degraded_state
from src.core.control_state import read_control_state, write_fixed_port_degraded_event
from src.core.engine import AccountEngine


@pytest.mark.parametrize("kind", ("entered", "ongoing", "recovered", "operator_resolved"))
def test_fixed_port_event_writer_persists_each_supported_kind_atomically(tmp_path, kind):
    entered_at = datetime(2026, 8, 25, tzinfo=timezone.utc)
    occurred_at = datetime(2026, 8, 25, 0, 15, tzinfo=timezone.utc)

    event = write_fixed_port_degraded_event(
        "kr_mock",
        kind,
        "test-operation",
        entered_at,
        occurred_at=occurred_at,
        data_dir=tmp_path,
    )

    control_file = tmp_path / "control" / "kr_mock.control.json"
    assert read_control_state("kr_mock", data_dir=tmp_path)["fixed_port_event"] == event
    assert event["kind"] == kind
    assert event["account"] == "kr_mock"
    assert event["operation"] == "test-operation"
    assert list(control_file.parent.glob("*.tmp")) == []


def test_tick_writes_one_entry_event_before_the_ongoing_interval(tmp_path):
    engine = object.__new__(AccountEngine)
    engine.ctx = SimpleNamespace(account_id="kr_mock", strategy=SimpleNamespace(symbol="005930"))
    engine.data_dir = tmp_path
    engine._refresh_runtime_control = Mock()
    engine._refresh_dashboard_controls = Mock()
    engine._balance_gate = SimpleNamespace(dispatch_clearance_service=None)
    enter_fixed_port_degraded_state("kr_mock", "test-operation")
    try:
        asyncio.run(engine._tick())
        first_event = read_control_state("kr_mock", data_dir=tmp_path)["fixed_port_event"]

        asyncio.run(engine._tick())

        assert first_event["kind"] == "entered"
        assert read_control_state("kr_mock", data_dir=tmp_path)["fixed_port_event"] == first_event
    finally:
        clear_fixed_port_degraded_state("kr_mock")
