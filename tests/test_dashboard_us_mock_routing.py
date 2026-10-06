import subprocess
from unittest.mock import Mock

import pytest

from dashboard import dashboard_server as dashboard


@pytest.fixture
def route(tmp_path, monkeypatch):
    runtime, source = tmp_path / "runtime", tmp_path / "source"
    runtime.mkdir()
    (source / "tools").mkdir(parents=True)
    (source / "tools/us_mock_launch_policy.py").write_text("# synthetic\n")
    monkeypatch.setattr(dashboard, "ROOT", runtime)
    monkeypatch.setenv("KIWOOM_WORKER_ROOT_US_MOCK", str(source))
    monkeypatch.setenv("KIWOOM_WORKER_REVISION_US_MOCK", "a" * 40)
    run = Mock(return_value=subprocess.CompletedProcess([], 0, '{"ok": true}\n', ""))
    monkeypatch.setattr(dashboard.subprocess, "run", run)
    return runtime, source, run


@pytest.mark.parametrize("action", ["start", "status", "stop"])
def test_us_lifecycle_uses_one_pinned_runtime(route, action):
    runtime, source, run = route
    assert dashboard._supervisor(action, "us_mock", "US")[0] == 0
    command = run.call_args.args[0]
    assert command[3] == str(source / "tools/us_mock_launch_policy.py")
    assert command[4] == action
    assert command[command.index("--runtime-root") + 1] == str(runtime)
    assert run.call_args.kwargs["cwd"] == runtime
    assert run.call_args.kwargs["timeout"] >= 60


@pytest.mark.parametrize("fault", ["missing_root", "missing_revision", "wrong_market", "same_root"])
def test_us_refusal_precedes_any_supervisor_process(route, monkeypatch, fault):
    runtime, _, run = route
    market = "US"
    if fault == "missing_root":
        monkeypatch.delenv("KIWOOM_WORKER_ROOT_US_MOCK")
    elif fault == "missing_revision":
        monkeypatch.delenv("KIWOOM_WORKER_REVISION_US_MOCK")
    elif fault == "wrong_market":
        market = "KR"
    else:
        monkeypatch.setenv("KIWOOM_WORKER_ROOT_US_MOCK", str(runtime))
    assert dashboard._supervisor("start", "us_mock", market)[0] == 9
    run.assert_not_called()


def test_kr_retains_original_supervisor_command_and_timeout(route):
    runtime, _, run = route
    dashboard._supervisor("stop", "kr_mock", "KR")
    assert run.call_args.args[0][1:] == ["-m", "src.worker_supervisor", "stop",
                                       "--account", "kr_mock", "--market", "KR"]
    assert run.call_args.kwargs["cwd"] == runtime
    assert run.call_args.kwargs["timeout"] == 20


def test_dotenv_rotates_settings_without_setting_or_replacing_pins(route, monkeypatch):
    runtime, source, _ = route
    monkeypatch.setenv("KIWOOM_RUNTIME_ROOT", str(runtime))
    monkeypatch.delenv("KIWOOM_DIAGNOSTICS_DIR", raising=False)
    (runtime / ".env").write_text(
        "KIWOOM_WORKER_ROOT_US_MOCK=synthetic-other-source\n"
        "KIWOOM_WORKER_REVISION_US_MOCK=bad\n"
        "KIWOOM_RUNTIME_ROOT=synthetic-other-runtime\n"
        "KIWOOM_DIAGNOSTICS_DIR=synthetic-other-diagnostics\n"
        "SYNTHETIC_ROTATING_SETTING=new-value\n")
    monkeypatch.setenv("SYNTHETIC_ROTATING_SETTING", "old-value")
    dashboard._load_dotenv()
    assert dashboard.os.environ["KIWOOM_WORKER_ROOT_US_MOCK"] == str(source)
    assert dashboard.os.environ["KIWOOM_WORKER_REVISION_US_MOCK"] == "a" * 40
    assert dashboard.os.environ["KIWOOM_RUNTIME_ROOT"] == str(runtime)
    assert "KIWOOM_DIAGNOSTICS_DIR" not in dashboard.os.environ
    assert dashboard.os.environ["SYNTHETIC_ROTATING_SETTING"] == "new-value"


def test_us_timeout_is_unresolved_and_is_not_retried(route):
    _, _, run = route
    run.side_effect = subprocess.TimeoutExpired("synthetic-bootstrap", 120)
    code, payload = dashboard._supervisor("start", "us_mock", "US")
    assert code == 4 and payload["running"] is True and payload["stopped"] is False
    assert run.call_count == 1


def test_unrouted_dashboard_retains_legacy_dotenv_state_configuration(route, monkeypatch):
    runtime, _, _ = route
    monkeypatch.delenv("KIWOOM_RUNTIME_ROOT", raising=False)
    monkeypatch.setenv("KIWOOM_DATA_DIR", "synthetic-before")
    (runtime / ".env").write_text("KIWOOM_DATA_DIR=synthetic-legacy-data\n")
    dashboard._load_dotenv()
    assert dashboard.os.environ["KIWOOM_DATA_DIR"] == "synthetic-legacy-data"
