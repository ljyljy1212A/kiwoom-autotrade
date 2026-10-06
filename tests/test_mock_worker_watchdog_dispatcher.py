import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools import mock_worker_watchdog_dispatcher as dispatcher
from src.core import us_mock_launch_command


@pytest.fixture
def dispatch(tmp_path, monkeypatch):
    source, runtime, legacy = (tmp_path / name for name in ("source", "runtime", "legacy"))
    runtime.mkdir()
    for root in (source, legacy):
        (root / "tools").mkdir(parents=True)
    (source / "tools/us_mock_launch_policy.py").write_text("# synthetic\n")
    (legacy / "tools/worker_watchdog.py").write_text("# synthetic\n")
    interpreter = tmp_path / "synthetic-python.exe"
    interpreter.write_text("synthetic")
    monkeypatch.setattr(dispatcher, "SOURCE", source)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "argv", ["dispatcher", "--kr-root", str(legacy),
        "--kr-python", str(interpreter), "--runtime-root", str(runtime),
        "--source-root", str(source), "--revision", "a" * 40])
    verify = Mock()
    monkeypatch.setattr(dispatcher, "_load_verifier", Mock(return_value=verify))
    monkeypatch.setattr(dispatcher, "_load_commands", Mock(return_value=us_mock_launch_command))
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "synthetic-private-output", ""))
    monkeypatch.setattr(dispatcher.subprocess, "run", run)

    def invoke():
        output = io.StringIO()
        with redirect_stdout(output):
            code = dispatcher.main()
        assert "synthetic-private-output" not in output.getvalue()
        return code, json.loads(output.getvalue())

    return SimpleNamespace(source=source, runtime=runtime, legacy=legacy,
                           interpreter=interpreter, verify=verify, run=run, invoke=invoke)


def test_children_keep_separate_sources_and_original_kr_environment(dispatch, monkeypatch):
    monkeypatch.setenv("SYNTHETIC_KR_SETTING", "unchanged")
    before = dict(dispatcher.os.environ)
    code, payload = dispatch.invoke()
    assert code == 0 and len(payload["children"]) == 2
    kr, us = dispatch.run.call_args_list
    assert kr.args[0] == [str(dispatch.interpreter), "-P", "-c", dispatcher._KR_SWEEP,
                          str(dispatch.legacy)]
    assert kr.kwargs["cwd"] == dispatch.legacy
    assert kr.kwargs["env"] == before
    assert us.args[0][3] == str(dispatch.source / "tools/us_mock_launch_policy.py")
    assert us.args[0][4] == "watchdog"
    assert us.kwargs["cwd"] == dispatch.runtime
    assert dict(dispatcher.os.environ) == before


@pytest.mark.parametrize("fault", ["refused", "timeout"])
def test_us_failure_does_not_skip_or_repeat_kr(dispatch, fault):
    failure = (subprocess.CompletedProcess([], 9, "", "") if fault == "refused"
               else subprocess.TimeoutExpired("synthetic-US", 120))
    dispatch.run.side_effect = [subprocess.CompletedProcess([], 0, "", ""), failure]
    code, payload = dispatch.invoke()
    assert code == 10 and dispatch.run.call_count == 2
    assert payload["children"][0]["returnCode"] == 0
    if fault == "timeout":
        assert payload["children"][1]["spawnCompletionUnknown"] is True


def test_invalid_us_runtime_keeps_kr_sweep_and_refuses_us(dispatch, monkeypatch):
    args = list(sys.argv)
    args[args.index("--runtime-root") + 1] = str(dispatch.source)
    monkeypatch.setattr(sys, "argv", args)
    code, payload = dispatch.invoke()
    assert code == 10 and dispatch.run.call_count == 1
    assert payload["children"][1]["completed"] is False


def test_dispatcher_integrity_failure_precedes_children(dispatch):
    dispatch.verify.side_effect = ValueError("synthetic integrity failure")
    assert dispatch.invoke()[0] == 9
    dispatch.run.assert_not_called()


@pytest.mark.parametrize("fault", [None, "watchdog", "supervisor"])
def test_kr_child_calls_only_kr_and_checks_both_import_origins(tmp_path, monkeypatch, fault):
    check = Mock()
    watchdog = SimpleNamespace(
        __file__=str(tmp_path / "tools/worker_watchdog.py"),
        worker_supervisor=SimpleNamespace(__file__=str(tmp_path / "src/worker_supervisor.py")),
        check_and_restart=check,
    )
    if fault == "watchdog":
        watchdog.__file__ = str(tmp_path / "other.py")
    elif fault == "supervisor":
        watchdog.worker_supervisor.__file__ = str(tmp_path / "other.py")
    monkeypatch.setitem(sys.modules, "tools", SimpleNamespace(worker_watchdog=watchdog))
    monkeypatch.setattr(sys, "argv", ["-c", str(tmp_path)])
    monkeypatch.setattr(sys, "path", list(sys.path))
    if fault is None:
        exec(dispatcher._KR_SWEEP, {})
        check.assert_called_once_with("kr_mock", "KR")
    else:
        with pytest.raises(RuntimeError, match="import origin mismatch"):
            exec(dispatcher._KR_SWEEP, {})
        check.assert_not_called()
