"""Bootstrap binding and dispatch contracts without operational side effects."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from tools import us_mock_launch_policy as bootstrap


SOURCE = Path(bootstrap.__file__).resolve().parents[1]
REVISION = "a" * 40


@pytest.fixture
def bound_bootstrap(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    routes = SimpleNamespace(
        resolve_worker_root=Mock(return_value=SOURCE),
        WorkerLaunchRouteError=type("SyntheticRouteError", (RuntimeError,), {}),
    )
    supervisor = SimpleNamespace(
        __file__=str(SOURCE / "src/worker_supervisor.py"), ROOT=SOURCE,
        start=Mock(return_value=(0, {"started": True})),
        maintenance=Mock(return_value=(0, {"changed": True, "workerAction": "none"})),
        status=Mock(return_value={"running": False}),
    )
    controls = SimpleNamespace(read_auto_trading_enabled=Mock(return_value=False))
    watchdog = SimpleNamespace(__file__=str(SOURCE / "tools/worker_watchdog.py"),
                               check_and_restart=Mock())
    modules = {"src.core.worker_launch_routes": routes, "src.worker_supervisor": supervisor,
               "src.core.control_state": controls, "tools.worker_watchdog": watchdog}
    imports = Mock(side_effect=lambda name: modules[name])
    # main deliberately refuses application modules already bound in the caller.
    clean_modules = {name: module for name, module in sys.modules.items()
                     if name != "src" and not name.startswith("src.")}
    environment = dict(bootstrap.os.environ)
    verify = Mock()

    def invoke(action, *extra):
        argv = [str(SOURCE / "tools/us_mock_launch_policy.py"), action,
                "--runtime-root", str(runtime), "--source-root", str(SOURCE),
                "--revision", REVISION, *extra]
        output = io.StringIO()
        with patch.object(bootstrap.sys, "modules", clean_modules), \
             patch.object(bootstrap.sys, "argv", argv), \
             patch.object(bootstrap.sys, "path", list(sys.path)), \
             patch.dict(bootstrap.os.environ, environment, clear=True), \
             patch.object(bootstrap, "_verify_source", verify), \
             patch.object(bootstrap.importlib, "import_module", imports), \
             redirect_stdout(output):
            code = bootstrap.main()
            captured_environment = dict(bootstrap.os.environ)
        return code, json.loads(output.getvalue()), captured_environment

    return SimpleNamespace(invoke=invoke, runtime=runtime, routes=routes,
                           supervisor=supervisor, controls=controls, watchdog=watchdog,
                           imports=imports, verify=verify)


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_maintenance_dispatch_never_starts_worker(bound_bootstrap, action):
    bound = bound_bootstrap
    code, payload, env = bound.invoke(action, "--reason", "isolated test",
                                      "--expected-generation", "b" * 32)
    assert code == 0 and payload["workerAction"] == "none"
    bound.supervisor.maintenance.assert_called_once_with(
        "us_mock", "US", paused=action == "pause", reason="isolated test",
        expected_generation="b" * 32)
    bound.supervisor.start.assert_not_called()
    bound.controls.read_auto_trading_enabled.assert_not_called()
    assert bound.supervisor.ROOT == bound.runtime
    assert env["KIWOOM_RUNTIME_ROOT"] == str(bound.runtime)
    assert env["KIWOOM_DATA_DIR"] == str(bound.runtime / "data")
    assert env["KIWOOM_WORKER_ROOT_US_MOCK"] == str(SOURCE)
    assert env["KIWOOM_WORKER_REVISION_US_MOCK"] == REVISION
    assert env["AUTO_TRADING_ENABLED"] == "false"


@pytest.mark.parametrize("value", [None, True, 0, "false"])
@pytest.mark.parametrize("action", ["start", "watchdog"])
def test_start_requires_explicit_boolean_false(bound_bootstrap, action, value):
    bound = bound_bootstrap
    bound.controls.read_auto_trading_enabled.return_value = value
    code, payload, _ = bound.invoke(action)
    assert code == 10
    assert payload["reason"] == "explicit-auto-trading-disabled-required"
    bound.supervisor.start.assert_not_called()
    bound.watchdog.check_and_restart.assert_not_called()


@pytest.mark.parametrize("action", ["status", "start", "watchdog"])
def test_exact_us_dispatch(bound_bootstrap, action):
    bound = bound_bootstrap
    code, payload, _ = bound.invoke(action)
    assert code == 0
    if action == "status":
        bound.supervisor.status.assert_called_once_with("us_mock")
        bound.supervisor.start.assert_not_called()
    elif action == "start":
        bound.supervisor.start.assert_called_once_with("us_mock", "US")
    else:
        bound.watchdog.check_and_restart.assert_called_once_with("us_mock", "US")
        assert payload["action"] == "watchdog-sweep-completed"
        assert "started" not in payload


def test_verification_failure_precedes_application_imports(bound_bootstrap):
    bound = bound_bootstrap
    bound.verify.side_effect = ValueError("synthetic source mismatch")
    assert bound.invoke("start")[0] == 9
    bound.imports.assert_not_called()


def test_route_mismatch_precedes_supervisor_import(bound_bootstrap):
    bound = bound_bootstrap
    bound.routes.resolve_worker_root.return_value = bound.runtime
    assert bound.invoke("start")[0] == 9
    bound.imports.assert_called_once_with("src.core.worker_launch_routes")


@pytest.mark.parametrize("extra", [
    ("--runtime-root", "relative"),
    ("--source-root", "relative"),
    ("--reason", "not-valid-for-start"),
])
def test_invalid_bootstrap_arguments_refused(bound_bootstrap, extra):
    bound = bound_bootstrap
    with pytest.raises(SystemExit) as exc:
        bound.invoke("start", *extra)
    assert exc.value.code == 2
    bound.verify.assert_not_called()
    bound.imports.assert_not_called()


def test_bootstrap_must_run_from_its_selected_source(bound_bootstrap):
    bound = bound_bootstrap
    with pytest.raises(SystemExit) as exc:
        bound.invoke("start", "--source-root", str(bound.runtime))
    assert exc.value.code == 2
    bound.verify.assert_not_called()
    bound.imports.assert_not_called()


@pytest.mark.parametrize("action", ["start", "watchdog"])
def test_import_origin_mismatch_refused(bound_bootstrap, action):
    bound = bound_bootstrap
    if action == "start":
        bound.supervisor.__file__ = bootstrap.__file__
    else:
        bound.watchdog.__file__ = bootstrap.__file__
    with pytest.raises(RuntimeError, match="import origin mismatch"):
        bound.invoke(action)
    bound.supervisor.start.assert_not_called()
    bound.watchdog.check_and_restart.assert_not_called()


def test_preloaded_application_modules_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["bootstrap", "start", "--runtime-root", str(tmp_path),
                                     "--source-root", str(SOURCE), "--revision", REVISION])
    monkeypatch.setitem(sys.modules, "src.synthetic_preloaded", SimpleNamespace())
    with patch.object(bootstrap, "_verify_source") as verify, pytest.raises(SystemExit) as exc:
        bootstrap.main()
    assert exc.value.code == 2
    verify.assert_not_called()


@pytest.mark.parametrize("fault", ["valid", "bad_revision", "wrong_root", "wrong_revision",
                                  "dirty_tool", "git_error", "git_stderr", "timeout"])
def test_source_verification_contract(tmp_path, monkeypatch, fault):
    replies = [str(tmp_path), REVISION, ""]
    if fault == "wrong_root":
        replies[0] = str(SOURCE)
    elif fault == "wrong_revision":
        replies[1] = "b" * 40
    elif fault == "dirty_tool":
        replies[2] = " M tools/worker_watchdog.py"
    results = [SimpleNamespace(returncode=0, stdout=value, stderr="") for value in replies]
    if fault == "git_error":
        results[0].returncode = 1
    elif fault == "git_stderr":
        results[0].stderr = "synthetic warning"
    monkeypatch.setenv("GIT_DIR", "synthetic-invalid-override")
    with patch.object(bootstrap.subprocess, "run", side_effect=results) as run:
        if fault == "timeout":
            run.side_effect = subprocess.TimeoutExpired("git", 5)
        revision = "invalid" if fault == "bad_revision" else REVISION
        if fault == "valid":
            bootstrap._verify_source(tmp_path, revision)
            assert run.call_count == 3
            args, kwargs = run.call_args
            assert "tools/worker_watchdog.py" in args[0]
            assert "tools/us_mock_launch_policy.py" in args[0]
            assert not any(key.startswith("GIT_") for key in kwargs["env"])
        else:
            with pytest.raises((ValueError, subprocess.TimeoutExpired)):
                bootstrap._verify_source(tmp_path, revision)
            if fault == "bad_revision":
                run.assert_not_called()
