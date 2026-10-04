"""Synthetic settings exercise source/runtime separation without broker access."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.core import account_catalog, runtime_paths, worker_environment


@pytest.fixture(params=[("kr_mock", "KR"), ("us_mock", "US")])
def routed_environment(tmp_path, monkeypatch, request):
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    root = tmp_path / "runtime"
    root.mkdir()
    account, market = request.param
    launch = {
        "KIWOOM_RUNTIME_ROOT": str(root),
        "KIWOOM_DATA_DIR": str(root / "data"),
        "KIWOOM_LOG_DIR": str(root / "logs"),
        "KIWOOM_DIAGNOSTICS_DIR": str(root / "diagnostics"),
        "KIWOOM_BACKUP_BASE_DIR": str(root / "backups"),
        "KIWOOM_EXPECTED_WORKER_ROOT": str(Path(runtime_paths.__file__).resolve().parents[2]),
        "KIWOOM_EXPECTED_WORKER_REVISION": "a" * 40,
        "ACCOUNT_FILTER": account, "MARKET_INSTANCE": market,
        "KIWOOM_SUPERVISOR_LAUNCH_ID": "synthetic-launch",
        "KIWOOM_ENV": "mock", "AUTO_TRADING_ENABLED": "false",
        "PRICE_FEED_MODE": "auto", "TELEGRAM_APPROVAL_REQUIRED": "false",
        "PYTHONPATH": str(Path(runtime_paths.__file__).resolve().parents[2]),
    }
    for key, value in launch.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(worker_environment, "RUNTIME_ROOT", root)
    monkeypatch.chdir(root)
    return root, launch


def test_runtime_dotenv_cannot_retarget_launch(routed_environment, monkeypatch):
    root, launch = routed_environment
    (root / ".env").write_text(
        "\n".join(f"{key}=wrong" for key in launch) + "\nSYNTHETIC_ROUTE_SETTING=from_runtime\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SYNTHETIC_ROUTE_SETTING", "inherited")
    worker_environment.load_worker_environment()
    assert {key: os.environ[key] for key in launch} == launch
    assert os.environ["SYNTHETIC_ROUTE_SETTING"] == "from_runtime"


@pytest.mark.parametrize("fault", ["missing_env", "missing_launch", "wrong_market", "relative_data", "wrong_cwd", "invalid_expected_revision", "wrong_expected_source"])
def test_invalid_runtime_contract_stops_before_dotenv(routed_environment, monkeypatch, fault):
    root, launch = routed_environment
    if fault != "missing_env":
        (root / ".env").write_text("SYNTHETIC_ROUTE_SETTING=unused\n", encoding="utf-8")
    if fault == "missing_launch":
        monkeypatch.delenv("KIWOOM_SUPERVISOR_LAUNCH_ID")
    elif fault == "wrong_market":
        monkeypatch.setenv("MARKET_INSTANCE", "KR" if launch["MARKET_INSTANCE"] == "US" else "US")
    elif fault == "relative_data":
        monkeypatch.setenv("KIWOOM_DATA_DIR", "relative")
    elif fault == "wrong_cwd":
        monkeypatch.chdir(root.parent)
    elif fault == "invalid_expected_revision":
        monkeypatch.setenv("KIWOOM_EXPECTED_WORKER_REVISION", "invalid")
    elif fault == "wrong_expected_source":
        monkeypatch.setenv("KIWOOM_EXPECTED_WORKER_ROOT", str(root))
    calls = []
    monkeypatch.setattr(worker_environment, "load_dotenv", lambda **kwargs: calls.append(kwargs))
    with pytest.raises(RuntimeError):
        worker_environment.load_worker_environment()
    assert calls == []


def test_unrouted_dotenv_behavior_is_preserved(monkeypatch):
    monkeypatch.delenv("KIWOOM_RUNTIME_ROOT", raising=False)
    calls = []
    monkeypatch.setattr(worker_environment, "load_dotenv", lambda **kwargs: calls.append(kwargs))
    worker_environment.load_worker_environment()
    assert calls == [{"override": True}]


@pytest.mark.parametrize("mode,market,duplicates,accepted", [
    ("mock", "KR", 1, True), ("real", "KR", 1, False),
    ("mock", "US", 1, False), ("mock", "KR", 2, False),
])
def test_catalog_uses_runtime_root_and_requires_unique_mock(routed_environment, monkeypatch, mode, market, duplicates, accepted):
    root, _ = routed_environment
    (root / "config").mkdir()
    entry = f"  - id: kr_mock\n    market: {market}\n    mode: {mode}\n"
    (root / "config" / "accounts.yaml").write_text("accounts:\n" + entry * duplicates, encoding="utf-8")
    monkeypatch.setattr(account_catalog, "PROJECT_ROOT", root.parent / "source_without_config")
    if accepted:
        worker_environment.validate_routed_account("kr_mock", "KR")
    else:
        with pytest.raises(RuntimeError, match="account configuration"):
            worker_environment.validate_routed_account("kr_mock", "KR")


def test_runtime_paths_are_bound_before_loading_worker(routed_environment):
    root, _ = routed_environment
    code = (
        "import json; from src.core.runtime_paths import "
        "RUNTIME_ROOT, DATA_DIR, LOG_DIR, DIAGNOSTICS_DIR, backup_dir; "
        "print(json.dumps([str(p) for p in "
        "(RUNTIME_ROOT, DATA_DIR, LOG_DIR, DIAGNOSTICS_DIR, backup_dir())]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=Path(runtime_paths.__file__).resolve().parents[2],
        capture_output=True, text=True, check=False, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [str(root), str(root / "data"), str(root / "logs"), str(root / "diagnostics"), str(root / "backups")]


def test_runtime_root_rejects_relative_or_missing_paths(tmp_path, monkeypatch):
    for value in ("relative", str(tmp_path / "missing")):
        monkeypatch.setenv("KIWOOM_RUNTIME_ROOT", value)
        with pytest.raises((ValueError, FileNotFoundError)):
            runtime_paths.runtime_root()


@pytest.mark.parametrize("mode,market,duplicates,account", [
    ("real", "KR", 1, "kr_mock"), ("mock", "US", 1, "kr_mock"),
    ("mock", "KR", 2, "kr_mock"), ("mock", "KR", 1, "us_mock"),
])
def test_loader_rejects_changed_scope_before_credentials(tmp_path, monkeypatch, mode, market, duplicates, account):
    from src.core import account_manager

    config = tmp_path / "synthetic_accounts.yaml"
    entry = f"  - id: kr_mock\n    market: {market}\n    mode: {mode}\n"
    config.write_text("accounts:\n" + entry * duplicates, encoding="utf-8")
    credential_calls = []
    monkeypatch.setattr(account_manager, "_env", lambda *args: credential_calls.append(args))
    with pytest.raises(RuntimeError, match="routed account loader"):
        account_manager.load_accounts(str(config), account, "KR", require_mock_route=True)
    assert credential_calls == []


@pytest.mark.parametrize("account,market", [("kr_mock", "KR"), ("us_mock", "US")])
def test_loader_accepts_valid_route_before_synthetic_lookup(tmp_path, monkeypatch, account, market):
    from src.core import account_manager

    config = tmp_path / "synthetic_accounts.yaml"
    config.write_text(
        f"accounts:\n  - id: {account}\n    market: {market}\n    mode: mock\n    env_prefix: SYNTHETIC\n",
        encoding="utf-8",
    )
    class SyntheticLookupReached(Exception):
        pass

    def lookup(prefix, key):
        assert (prefix, key) == ("SYNTHETIC", "NO")
        raise SyntheticLookupReached

    monkeypatch.setattr(account_manager, "_env", lookup)
    with pytest.raises(SyntheticLookupReached):
        account_manager.load_accounts(str(config), account, market, require_mock_route=True)


@pytest.mark.parametrize("mode,market,count", [
    ("mock", "US", 1), ("real", "US", 1),
    ("mock", "KR", 1), ("mock", "US", 2), ("mock", "US", 0),
])
def test_us_catalog_requires_one_matching_mock(tmp_path, monkeypatch, mode, market, count):
    root = tmp_path / "runtime"
    (root / "config").mkdir(parents=True)
    monkeypatch.setenv("KIWOOM_RUNTIME_ROOT", str(root))
    entry = f"  - id: us_mock\n    market: {market}\n    mode: {mode}\n"
    (root / "config" / "accounts.yaml").write_text("accounts:\n" + entry * count, encoding="utf-8")
    if (mode, market, count) == ("mock", "US", 1):
        worker_environment.validate_routed_account("us_mock", "US")
    else:
        with pytest.raises(RuntimeError, match="account configuration"):
            worker_environment.validate_routed_account("us_mock", "US")


@pytest.mark.parametrize("document", [
    "accounts:\n  - id: us_mock\n    market: US\n    mode: real\n",
    "accounts:\n  - id: us_mock\n    market: KR\n    mode: mock\n",
    "accounts:\n  - id: us_mock\n    market: US\n",
    "accounts:\n  - id: us_mock\n    market: US\n    mode: mock\n  - id: us_mock\n    market: US\n    mode: mock\n",
    "accounts: []\n", "accounts: {}\n", "accounts: [null]\n", "null\n",
])
def test_us_loader_rejects_scope_before_credentials(tmp_path, monkeypatch, document):
    from src.core import account_manager
    config = tmp_path / "synthetic_accounts.yaml"
    config.write_text(document, encoding="utf-8")
    calls = []
    monkeypatch.setattr(account_manager, "_env", lambda *args: calls.append(args))
    with pytest.raises(RuntimeError, match="routed account loader"):
        account_manager.load_accounts(str(config), "us_mock", "US", require_mock_route=True)
    assert calls == []
