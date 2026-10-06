from pathlib import Path

import pytest

from src.core.us_mock_launch_command import USMockCommandError, bootstrap_command


@pytest.fixture
def roots(tmp_path):
    runtime = tmp_path / "runtime"
    source = tmp_path / "source"
    runtime.mkdir()
    (source / "tools").mkdir(parents=True)
    (source / "tools/us_mock_launch_policy.py").write_text("# synthetic\n")
    return runtime, source


@pytest.mark.parametrize("action", ["start", "status", "stop", "watchdog"])
def test_command_keeps_source_and_runtime_distinct(roots, action):
    runtime, source = roots
    command = bootstrap_command("synthetic-python", action, runtime, source, "a" * 40)
    assert command[1:3] == ["-B", "-P"]
    assert Path(command[3]) == source / "tools/us_mock_launch_policy.py"
    assert command[4] == action
    assert command[command.index("--runtime-root") + 1] == str(runtime)
    assert command[command.index("--source-root") + 1] == str(source)


@pytest.mark.parametrize("fault", ["relative", "missing", "same", "revision", "action", "script"])
def test_invalid_routes_have_no_legacy_fallback(roots, fault):
    runtime, source = roots
    action, revision = "start", "a" * 40
    if fault == "relative":
        source = Path("relative")
    elif fault == "missing":
        source /= "missing"
    elif fault == "same":
        runtime = source
    elif fault == "revision":
        revision = "short"
    elif fault == "action":
        action = "kill"
    else:
        (source / "tools/us_mock_launch_policy.py").unlink()
    with pytest.raises(USMockCommandError):
        bootstrap_command("synthetic-python", action, runtime, source, revision)


@pytest.mark.parametrize("revision", [None, 123, "a" * 39])
def test_malformed_revision_is_a_structured_route_refusal(roots, revision):
    runtime, source = roots
    with pytest.raises(USMockCommandError):
        bootstrap_command("synthetic-python", "start", runtime, source, revision)
