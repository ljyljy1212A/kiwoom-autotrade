import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src import main as worker_main


def test_worker_heartbeat_publishes_running_when_account_is_not_degraded():
    identity = worker_main.WorkerIdentity("kr_mock", "KR", 123, "instance", "started")
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    writer = Mock()
    with patch.object(worker_main.asyncio, "sleep", sleep), \
         patch.object(worker_main, "get_fixed_port_degraded_state", return_value=None), \
         patch.object(worker_main, "_write_worker_status", writer):
        try:
            asyncio.run(worker_main._publish_worker_heartbeat(
                identity,
                SimpleNamespace(
                    running_symbols=lambda _: ("005930",),
                    controller_cycle_at=lambda _: None,
                ),
                interval_sec=0,
            ))
        except asyncio.CancelledError:
            pass

    writer.assert_called_once_with(identity, "RUNNING", ["005930"], None, lock=None)


def test_worker_heartbeat_publishes_degraded_state_without_changing_liveness():
    identity = worker_main.WorkerIdentity("kr_mock", "KR", 123, "instance", "started")
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    writer = Mock()
    with patch.object(worker_main.asyncio, "sleep", sleep), \
         patch.object(worker_main, "get_fixed_port_degraded_state", return_value=object()), \
         patch.object(worker_main, "_write_worker_status", writer):
        try:
            asyncio.run(worker_main._publish_worker_heartbeat(
                identity,
                SimpleNamespace(
                    running_symbols=lambda _: (),
                    controller_cycle_at=lambda _: None,
                ),
                interval_sec=0,
            ))
        except asyncio.CancelledError:
            pass

    writer.assert_called_once_with(identity, "DEGRADED_FIXED_PORT", [], None, lock=None)


def test_worker_status_schema_includes_active_symbols_and_writes_atomically():
    identity = worker_main.WorkerIdentity("kr_mock", "KR", 123, "instance", "started")
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        with patch.object(worker_main, "DATA_DIR", data_dir), patch.object(worker_main, "SYS_LOG"):
            worker_main._write_worker_status(identity, "RUNNING", ["005930"])

        payload = json.loads((data_dir / "worker_kr_mock.status.json").read_text(encoding="utf-8"))

    assert payload["account"] == "kr_mock"
    assert payload["market"] == "KR"
    assert payload["pid"] == 123
    assert payload["instanceId"] == "instance"
    assert payload["startedAt"] == "started"
    assert payload["state"] == "RUNNING"
    assert payload["active_symbols"] == ["005930"]
    assert payload["activityState"] == "active"
    assert "updatedAt" in payload
    assert payload["processHeartbeatAt"] == payload["updatedAt"]
    assert payload["sourceRoot"] is None
    assert payload["sourceModule"] is None
    assert payload["sourceRevision"] is None
    assert payload["sourceWorkingTree"] == "INCOMPLETE"
    assert payload["sourceVerifiedAt"] is None


def test_worker_status_marks_no_active_symbols_as_expected_idle_and_records_controller_cycle():
    identity = worker_main.WorkerIdentity("kr_mock", "KR", 123, "instance", "started")
    controller_cycle_at = "2026-09-06T00:00:00+00:00"
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        with patch.object(worker_main, "DATA_DIR", data_dir), patch.object(worker_main, "SYS_LOG"):
            worker_main._write_worker_status(identity, "RUNNING", [], controller_cycle_at)

        payload = json.loads((data_dir / "worker_kr_mock.status.json").read_text(encoding="utf-8"))

    assert payload["activityState"] == "expected-idle"
    assert payload["lastControllerCycleAt"] == controller_cycle_at


def test_worker_status_includes_mutex_owner_evidence_when_available():
    identity = worker_main.WorkerIdentity("kr_mock", "KR", 123, "instance", "started")
    lock = Mock()
    lock.observe_mutex_owner.return_value = {
        "state": "CONFIRMED", "account": "kr_mock", "ownerPid": 123,
        "ownerThreadId": 456, "observedAt": "2026-09-28T00:00:00+00:00",
    }
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        with patch.object(worker_main, "DATA_DIR", data_dir), patch.object(worker_main, "SYS_LOG"):
            worker_main._write_worker_status(identity, "RUNNING", [], lock=lock)
        payload = json.loads((data_dir / "worker_kr_mock.status.json").read_text(encoding="utf-8"))
    assert payload["mutexOwnership"]["state"] == "CONFIRMED"
    assert payload["mutexOwnership"]["account"] == identity.account_id
    assert payload["mutexOwnership"]["ownerPid"] == identity.pid
    assert payload["mutexOwnership"]["ownerThreadId"] == 456


def test_worker_status_downgrades_mutex_owner_identity_mismatch():
    identity = worker_main.WorkerIdentity("kr_mock", "KR", 123, "instance", "started")
    lock = Mock()
    lock.observe_mutex_owner.return_value = {
        "state": "CONFIRMED", "account": "other_mock", "ownerPid": 999,
        "ownerThreadId": 456, "observedAt": "2026-09-28T00:00:00+00:00",
    }
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        with patch.object(worker_main, "DATA_DIR", data_dir), patch.object(worker_main, "SYS_LOG"):
            worker_main._write_worker_status(identity, "RUNNING", [], lock=lock)
        payload = json.loads((data_dir / "worker_kr_mock.status.json").read_text(encoding="utf-8"))
    assert payload["mutexOwnership"] == {"state": "INCOMPLETE", "reason": "identity_mismatch"}


def test_registry_records_controller_cycle_per_account():
    registry = worker_main.SymbolEngineRegistry()
    registry.mark_controller_cycle("kr_mock")

    assert registry.controller_cycle_at("kr_mock") is not None
    assert registry.controller_cycle_at("us_mock") is None


def test_worker_source_identity_reports_module_path_revision_and_clean_state():
    source_root = Path(worker_main.__file__).resolve().parents[1]
    outputs = [str(source_root), "a" * 40, ""]
    results = [SimpleNamespace(returncode=0, stdout=value, stderr="") for value in outputs]
    with patch.object(worker_main.subprocess, "run", side_effect=results) as run:
        source = worker_main._worker_source_identity()

    assert source.source_root == str(source_root)
    assert source.source_module == str(Path(worker_main.__file__).resolve())
    assert source.revision == "a" * 40
    assert source.working_tree == "CLEAN"
    assert source.observed_at
    assert run.call_count == 3


def test_worker_source_identity_marks_dirty_source_tree():
    source_root = Path(worker_main.__file__).resolve().parents[1]
    outputs = [str(source_root), "b" * 40, " M src/main.py"]
    results = [SimpleNamespace(returncode=0, stdout=value, stderr="") for value in outputs]
    with patch.object(worker_main.subprocess, "run", side_effect=results):
        source = worker_main._worker_source_identity()

    assert source.revision == "b" * 40
    assert source.working_tree == "DIRTY"


def test_worker_source_identity_fails_closed_when_git_is_unavailable():
    with patch.object(worker_main.subprocess, "run", side_effect=FileNotFoundError):
        source = worker_main._worker_source_identity()

    assert source.source_module == str(Path(worker_main.__file__).resolve())
    assert source.revision is None
    assert source.working_tree == "INCOMPLETE"


@pytest.mark.parametrize("revision,working_tree,root_matches", [
    ("a" * 40, "CLEAN", True), ("b" * 40, "CLEAN", True),
    ("a" * 40, "DIRTY", True), ("a" * 40, "INCOMPLETE", True),
    ("a" * 40, "CLEAN", False),
])
def test_routed_source_identity_checks_expected_pin(revision, working_tree, root_matches, tmp_path):
    expected_root = tmp_path / "source"
    actual_root = expected_root if root_matches else tmp_path / "other"
    expected_module = expected_root / "src" / "main.py"
    actual_module = actual_root / "src" / "main.py"
    expected_module.parent.mkdir(parents=True)
    expected_module.write_text("# expected synthetic source\n", encoding="utf-8")
    if not root_matches:
        actual_module.parent.mkdir(parents=True)
        actual_module.write_text("# other synthetic source\n", encoding="utf-8")
    identity = worker_main.WorkerSourceIdentity(
        source_root=str(actual_root), source_module=str(actual_module),
        revision=revision, working_tree=working_tree, observed_at="synthetic",
    )
    with patch.dict(os.environ, {
        "KIWOOM_EXPECTED_WORKER_ROOT": str(expected_root),
        "KIWOOM_EXPECTED_WORKER_REVISION": "a" * 40,
    }):
        if revision == "a" * 40 and working_tree == "CLEAN" and root_matches:
            worker_main._validate_routed_worker_source_identity(identity)
        else:
            with pytest.raises(RuntimeError, match="Worker launch refused"):
                worker_main._validate_routed_worker_source_identity(identity)


def test_routed_source_mismatch_stops_before_account_loading(monkeypatch, tmp_path):
    source_root = tmp_path / "source"
    source_module = source_root / "src" / "main.py"
    source_module.parent.mkdir(parents=True)
    source_module.write_text("# synthetic source\n", encoding="utf-8")
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    monkeypatch.setenv("KIWOOM_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setenv("KIWOOM_EXPECTED_WORKER_ROOT", str(source_root))
    monkeypatch.setenv("KIWOOM_EXPECTED_WORKER_REVISION", "a" * 40)
    monkeypatch.setenv("ACCOUNT_FILTER", "kr_mock")
    monkeypatch.setenv("MARKET_INSTANCE", "KR")
    monkeypatch.setattr(sys, "argv", ["worker", "--market", "KR"])
    identity = worker_main.WorkerSourceIdentity(
        source_root=str(source_root), source_module=str(source_module),
        revision="a" * 40, working_tree="DIRTY", observed_at="synthetic",
    )
    loader = Mock()
    monkeypatch.setattr(worker_main, "validate_routed_account", Mock())
    monkeypatch.setattr(worker_main, "_worker_source_identity", lambda: identity)
    monkeypatch.setattr(worker_main, "load_accounts", loader)

    with pytest.raises(RuntimeError, match="source tree is not clean"):
        asyncio.run(worker_main.main())

    loader.assert_not_called()
