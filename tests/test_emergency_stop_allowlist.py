from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from src.core.orphan_cleanup import account_cleanup_lock, account_control_state_lock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_SCRIPT = PROJECT_ROOT / "ops" / "emergency_stop.ps1"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")

pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is required")


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _normalize_stderr(text: str) -> str:
    text = _ANSI_ESCAPE_RE.sub("", text)
    lines = (
        re.sub(r"^\s*(?:Line\s*\||\d+\s*\||\|)\s*", "", line)
        for line in text.splitlines()
    )
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def _config(entries: list[str]) -> str:
    return "accounts:\n" + "\n".join(entries) + "\n"


def _entry(account_id: str, *, eligible: bool = True) -> str:
    return "\n".join((
        f"  - id: {account_id}",
        "    mode: mock",
        f"    emergency_stop_eligible: {str(eligible).lower()}",
    ))


def _repo(tmp_path: Path, config_text: str | None) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    ops = repo / "ops"
    ops.mkdir(parents=True)
    script = ops / "emergency_stop.ps1"
    script.write_text(SOURCE_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    if config_text is not None:
        config_path = repo / "config" / "accounts.yaml"
        config_path.parent.mkdir()
        config_path.write_text(config_text, encoding="utf-8")
    return repo, script


def _targets(repo: Path, account: str, *, control: bool = True, settings: bool = True, valid_settings: bool = True) -> tuple[Path, Path]:
    control_path = repo / "data" / "control" / f"{account}.control.json"
    settings_path = repo / "data" / f"dashboard_settings_{account}.json"
    if control:
        control_path.parent.mkdir(parents=True, exist_ok=True)
        control_path.write_text('{"auto_trading_enabled": true}', encoding="utf-8")
    if settings:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        payload = ('{"profiles": [{"config": {"max_cycles": null}, "enabled": true, '
                   '"auto_buy": {"enabled": true}, "auto_sell": {"enabled": true}}]}')
        if not valid_settings:
            payload = '{"profiles": [{"enabled": true}]}'
        settings_path.write_text(payload, encoding="utf-8")
    return control_path, settings_path


def _run(
    script: Path,
    account: str,
    *,
    runtime_root: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    temp_dir = script.parent.parent / ".tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["TEMP"] = str(temp_dir)
    env["TMP"] = str(temp_dir)
    command = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Account", account]
    if runtime_root is not None:
        command.extend(["-RuntimeRoot", str(runtime_root)])
    return subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        env=env,
    )


def test_explicit_allowed_runtime_root_disables_control_and_profile(tmp_path: Path):
    account = "eligible_mock"
    repo, script = _repo(tmp_path, _config([_entry(account)]))
    control_path, settings_path = _targets(repo, account)

    result = _run(script, account, runtime_root=repo)

    assert result.returncode == 0, result.stderr
    assert json.loads(control_path.read_text(encoding="utf-8"))["auto_trading_enabled"] is False
    profile = json.loads(settings_path.read_text(encoding="utf-8"))["profiles"][0]
    assert profile["enabled"] is False
    assert profile["auto_buy"]["enabled"] is False
    assert profile["auto_sell"]["enabled"] is False


def test_runtime_root_outside_fixed_allowlist_is_rejected_before_writes(tmp_path: Path):
    account = "eligible_mock"
    repo, script = _repo(tmp_path, _config([_entry(account)]))
    control_path, settings_path = _targets(repo, account)
    original_control = control_path.read_bytes()
    original_settings = settings_path.read_bytes()

    result = _run(script, account, runtime_root=tmp_path / "unapproved-runtime-root")

    assert result.returncode != 0
    assert "fixed allowlist" in _normalize_stderr(result.stderr).lower()
    assert control_path.read_bytes() == original_control
    assert settings_path.read_bytes() == original_settings


def test_eligible_mock_account_disables_control_and_profile(tmp_path: Path):
    account = "eligible_mock"
    repo, script = _repo(tmp_path, _config([_entry(account)]))
    control_path, settings_path = _targets(repo, account)

    result = _run(script, account)

    assert result.returncode == 0, result.stderr
    assert json.loads(control_path.read_text(encoding="utf-8"))["auto_trading_enabled"] is False
    profile = json.loads(settings_path.read_text(encoding="utf-8"))["profiles"][0]
    assert profile["enabled"] is False
    assert profile["auto_buy"]["enabled"] is False
    assert profile["auto_sell"]["enabled"] is False


def test_eligible_mock_account_preserves_control_events(tmp_path: Path):
    account = "eligible_mock"
    repo, script = _repo(tmp_path, _config([_entry(account)]))
    control_path, _ = _targets(repo, account)
    initial = {
        "account": account,
        "auto_trading_enabled": True,
        "fixed_port_event": {"event_id": "fixed-port-event", "kind": "entered"},
        "pause_clear_event": {"event_id": "pause-clear-event", "reason": "fixed_port_degraded"},
    }
    control_path.write_text(json.dumps(initial), encoding="utf-8")

    result = _run(script, account)

    assert result.returncode == 0, result.stderr
    updated = json.loads(control_path.read_text(encoding="utf-8"))
    assert updated["auto_trading_enabled"] is False
    assert updated["fixed_port_event"] == initial["fixed_port_event"]
    assert updated["pause_clear_event"] == initial["pause_clear_event"]


def test_ineligible_mock_account_is_rejected_before_writes(tmp_path: Path):
    account = "not_opted_in_mock"
    repo, script = _repo(tmp_path, _config([_entry(account, eligible=False)]))
    control_path, settings_path = _targets(repo, account)
    before_control, before_settings = control_path.read_text(), settings_path.read_text()

    result = _run(script, account)

    assert result.returncode != 0
    assert "not explicitly eligible" in _normalize_stderr(result.stderr)
    assert control_path.read_text() == before_control
    assert settings_path.read_text() == before_settings


@pytest.mark.parametrize("config_text", [None, "accounts: ["])
def test_missing_or_malformed_config_terminates_before_writes(tmp_path: Path, config_text: str | None):
    account = "eligible_mock"
    repo, script = _repo(tmp_path, config_text)
    control_path, settings_path = _targets(repo, account)
    before_control, before_settings = control_path.read_text(), settings_path.read_text()

    result = _run(script, account)

    assert result.returncode != 0
    assert "allowlist" in result.stderr.lower()
    assert control_path.read_text() == before_control
    assert settings_path.read_text() == before_settings


def test_duplicate_id_config_terminates_before_writes(tmp_path: Path):
    account = "duplicate_mock"
    repo, script = _repo(tmp_path, _config([_entry(account), _entry(account)]))
    control_path, settings_path = _targets(repo, account)
    before_control, before_settings = control_path.read_text(), settings_path.read_text()

    result = _run(script, account)

    assert result.returncode != 0
    assert "unavailable or invalid" in result.stderr
    assert control_path.read_text() == before_control
    assert settings_path.read_text() == before_settings


def test_unknown_account_is_rejected_before_writes(tmp_path: Path):
    repo, script = _repo(tmp_path, _config([_entry("eligible_mock")]))
    control_path, settings_path = _targets(repo, "unknown_mock")
    before_control, before_settings = control_path.read_text(), settings_path.read_text()

    result = _run(script, "unknown_mock")

    assert result.returncode != 0
    assert "not explicitly eligible" in _normalize_stderr(result.stderr)
    assert control_path.read_text() == before_control
    assert settings_path.read_text() == before_settings


def test_both_targets_missing_terminates_with_paths(tmp_path: Path):
    account = "eligible_mock"
    repo, script = _repo(tmp_path, _config([_entry(account)]))

    result = _run(script, account)

    assert result.returncode != 0
    assert "both safety targets are missing" in result.stderr
    assert str(repo / "data" / "control" / f"{account}.control.json") in result.stderr
    assert str(repo / "data" / f"dashboard_settings_{account}.json") in result.stderr


def test_one_target_missing_reports_partial_failure(tmp_path: Path):
    account = "eligible_mock"
    repo, script = _repo(tmp_path, _config([_entry(account)]))
    control_path, _ = _targets(repo, account, settings=False)

    result = _run(script, account)

    assert result.returncode != 0
    assert "Emergency stop incomplete" in result.stderr
    assert "settings target missing" in result.stderr
    assert json.loads(control_path.read_text(encoding="utf-8"))["auto_trading_enabled"] is False


def test_anchor_mismatch_is_rejected(tmp_path: Path):
    account = "eligible_mock"
    repo, script = _repo(tmp_path, _config([_entry(account)]))
    control_path, settings_path = _targets(repo, account, valid_settings=False)

    result = _run(script, account)

    assert result.returncode != 0
    assert "anchor count is 0; expected exactly 1" in result.stderr
    assert json.loads(control_path.read_text(encoding="utf-8"))["auto_trading_enabled"] is False
    assert settings_path.read_text(encoding="utf-8") == '{"profiles": [{"enabled": true}]}'


def test_cleanup_lock_contention_disables_control_and_preserves_settings(tmp_path: Path):
    account = "eligible_mock"
    repo, script = _repo(tmp_path, _config([_entry(account)]))
    control_path, settings_path = _targets(repo, account)
    original_settings = settings_path.read_bytes()

    with account_cleanup_lock(repo / "data", account):
        blocked = _run(script, account)

    assert blocked.returncode != 0
    assert "Account cleanup lock unavailable" in _normalize_stderr(blocked.stderr)
    assert json.loads(control_path.read_text(encoding="utf-8"))["auto_trading_enabled"] is False
    assert settings_path.read_bytes() == original_settings

    after_release = _run(script, account)
    assert after_release.returncode == 0, after_release.stderr
    profile = json.loads(settings_path.read_text(encoding="utf-8"))["profiles"][0]
    assert profile["enabled"] is False
    assert profile["auto_buy"]["enabled"] is False
    assert profile["auto_sell"]["enabled"] is False


@pytest.mark.skipif(os.name != "nt", reason="Windows file sharing semantics are required")
def test_settings_replace_failure_preserves_original_bytes(tmp_path: Path):
    account = "eligible_mock"
    repo, script = _repo(tmp_path, _config([_entry(account)]))
    control_path, settings_path = _targets(repo, account)
    original_settings = settings_path.read_bytes()

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int

    generic_read = 0x80000000
    share_read_write = 0x00000001 | 0x00000002
    open_existing = 3
    file_attribute_normal = 0x00000080
    handle = create_file(
        str(settings_path),
        generic_read,
        share_read_write,
        None,
        open_existing,
        file_attribute_normal,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    assert handle not in (None, invalid_handle), f"CreateFileW failed: {ctypes.get_last_error()}"

    try:
        result = _run(script, account)
    finally:
        assert close_handle(handle), f"CloseHandle failed: {ctypes.get_last_error()}"

    assert result.returncode != 0
    assert "replace" in _normalize_stderr(result.stderr).lower()
    assert json.loads(control_path.read_text(encoding="utf-8"))["auto_trading_enabled"] is False
    assert settings_path.read_bytes() == original_settings
    assert not list(settings_path.parent.glob(settings_path.name + ".emergency_stop_*.tmp"))
    assert not list(settings_path.parent.glob(settings_path.name + ".emergency_stop_*.bak"))


def test_control_state_lock_contention_preserves_control_and_settings(tmp_path: Path):
    account = "eligible_mock"
    repo, script = _repo(tmp_path, _config([_entry(account)]))
    control_path, settings_path = _targets(repo, account)
    original_control = control_path.read_bytes()
    original_settings = settings_path.read_bytes()

    with account_control_state_lock(repo / "data", account):
        blocked = _run(script, account)

    assert blocked.returncode != 0
    assert "control-state lock unavailable" in _normalize_stderr(blocked.stderr)
    assert control_path.read_bytes() == original_control
    assert settings_path.read_bytes() == original_settings
