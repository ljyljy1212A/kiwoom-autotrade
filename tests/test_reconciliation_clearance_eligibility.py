from unittest.mock import patch

import pytest

from src.core import account_catalog


def _write(tmp_path, text):
    config = tmp_path / "config"
    config.mkdir()
    (config / "accounts.yaml").write_text(text, encoding="utf-8")
    return config


def _entry(account="us_mock", market="US", mode="mock", marker="true"):
    return (
        f"  - id: {account}"
        f"\n    market: {market}"
        f"\n    mode: {mode}"
        f"\n    emergency_stop_eligible: {marker}"
        f"\n"
    )


def _call(tmp_path, account="us_mock", market="US", mode="mock"):
    with patch.object(account_catalog, "PROJECT_ROOT", tmp_path):
        return account_catalog.reconciliation_clearance_eligible(account, market, mode)


def test_eligible_mock_account(tmp_path):
    _write(tmp_path, "accounts:\n" + _entry())
    assert _call(tmp_path) is True


def test_ineligible_mock_account(tmp_path):
    _write(tmp_path, "accounts:\n" + _entry(marker="false"))
    assert _call(tmp_path) is False


def test_real_account_is_rejected(tmp_path):
    _write(tmp_path, "accounts:\n" + _entry("us_real", mode="real", marker="false"))
    with pytest.raises(ValueError):
        _call(tmp_path, "us_real", "US", "real")


def test_mode_mismatch_raises(tmp_path):
    _write(tmp_path, "accounts:\n" + _entry())
    with pytest.raises(ValueError):
        _call(tmp_path, mode="real")


def test_market_mismatch_raises(tmp_path):
    _write(tmp_path, "accounts:\n" + _entry())
    with pytest.raises(ValueError):
        _call(tmp_path, market="KR")


def test_missing_config_raises(tmp_path):
    with pytest.raises(ValueError):
        _call(tmp_path)


def test_malformed_config_raises(tmp_path):
    _write(tmp_path, "accounts: [")
    with pytest.raises(ValueError):
        _call(tmp_path)


def test_empty_accounts_raises(tmp_path):
    _write(tmp_path, "accounts: []\n")
    with pytest.raises(ValueError):
        _call(tmp_path)


def test_missing_marker_raises(tmp_path):
    _write(tmp_path, "accounts:\n  - id: us_mock\n    market: US\n    mode: mock\n")
    with pytest.raises(ValueError):
        _call(tmp_path)


def test_non_boolean_marker_raises(tmp_path):
    _write(tmp_path, "accounts:\n" + _entry(marker="yes"))
    with pytest.raises(ValueError):
        _call(tmp_path)


def test_duplicate_account_entry_raises(tmp_path):
    _write(tmp_path, "accounts:\n" + _entry() + _entry())
    with pytest.raises(ValueError):
        _call(tmp_path)


def test_non_mapping_entry_raises(tmp_path):
    _write(tmp_path, "accounts:\n  - bad\n")
    with pytest.raises(ValueError):
        _call(tmp_path)


def test_missing_account_raises(tmp_path):
    _write(tmp_path, "accounts:\n" + _entry("other"))
    with pytest.raises(ValueError):
        _call(tmp_path)


def test_missing_market_raises(tmp_path):
    _write(tmp_path, "accounts:\n  - id: us_mock\n    mode: mock\n    emergency_stop_eligible: true\n")
    with pytest.raises(ValueError):
        _call(tmp_path)


def test_missing_mode_raises(tmp_path):
    _write(tmp_path, "accounts:\n  - id: us_mock\n    market: US\n    emergency_stop_eligible: true\n")
    with pytest.raises(ValueError):
        _call(tmp_path)
