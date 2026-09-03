from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


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


def _run(script: Path, account: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Account", account],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


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
