"""Characterize AccountEngine lifecycle persistence before extraction."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core import engine as engine_module
from src.core import lifecycle_persistence as lifecycle_persistence_module


SYMBOL = "005930"
OTHER_SYMBOL = "000660"


def _engine_with_lifecycle(
    tmp_path: Path,
    *,
    disk: dict[str, dict] | None,
    pending: dict[str, dict],
) -> tuple[engine_module.AccountEngine, Path]:
    path = tmp_path / "symbol_lifecycles_characterization_mock.json"
    if disk is not None:
        path.write_text(json.dumps(disk), encoding="utf-8")

    engine = engine_module.AccountEngine.__new__(engine_module.AccountEngine)
    engine.ctx = SimpleNamespace(
        account_id="characterization_mock",
        client=SimpleNamespace(market="KR"),
        strategy=SimpleNamespace(symbol=SYMBOL),
    )
    engine.data_dir = tmp_path
    engine._lifecycle_path = path
    engine._symbol_lifecycles = copy.deepcopy(pending)
    engine._lifecycle_disk_present = disk is not None and SYMBOL in disk
    engine._lifecycle_disk_state = copy.deepcopy((disk or {}).get(SYMBOL))
    return engine, path


def test_merge_preserves_a_concurrent_change_to_another_symbol(tmp_path: Path):
    old = {"status": "pending", "activation_id": "old"}
    changed = {"status": "open", "activation_id": "old", "manual_qty": 2.0}
    other_old = {"status": "pending"}
    other_new = {"status": "closed"}
    engine, path = _engine_with_lifecycle(
        tmp_path,
        disk={SYMBOL: old, OTHER_SYMBOL: other_old},
        pending={SYMBOL: changed, OTHER_SYMBOL: other_old},
    )
    path.write_text(json.dumps({SYMBOL: old, OTHER_SYMBOL: other_new}), encoding="utf-8")

    engine._write_lifecycles()

    expected = {SYMBOL: changed, OTHER_SYMBOL: other_new}
    assert json.loads(path.read_text(encoding="utf-8")) == expected
    assert engine._symbol_lifecycles == expected
    assert engine._lifecycle_disk_present is True
    assert engine._lifecycle_disk_state == changed


def test_removal_preserves_another_symbol_and_clears_disk_markers(tmp_path: Path):
    old = {"status": "open", "activation_id": "old"}
    other = {"status": "pending"}
    engine, path = _engine_with_lifecycle(
        tmp_path,
        disk={SYMBOL: old, OTHER_SYMBOL: other},
        pending={OTHER_SYMBOL: other},
    )

    engine._write_lifecycles()

    assert json.loads(path.read_text(encoding="utf-8")) == {OTHER_SYMBOL: other}
    assert engine._symbol_lifecycles == {OTHER_SYMBOL: other}
    assert engine._lifecycle_disk_present is False
    assert engine._lifecycle_disk_state is None


def test_stale_same_symbol_change_is_rejected_without_overwriting(tmp_path: Path):
    old = {"status": "pending", "activation_id": "old"}
    engine, path = _engine_with_lifecycle(
        tmp_path,
        disk={SYMBOL: old},
        pending={SYMBOL: {"status": "open", "activation_id": "old"}},
    )
    path.write_text(
        json.dumps({SYMBOL: {"status": "closed", "activation_id": "other"}}),
        encoding="utf-8",
    )
    original_bytes = path.read_bytes()

    with pytest.raises(RuntimeError, match="Concurrent symbol lifecycle change"):
        engine._write_lifecycles()

    assert path.read_bytes() == original_bytes
    assert engine._lifecycle_disk_state == old


def test_missing_lifecycle_file_allows_first_symbol_write(tmp_path: Path):
    pending = {SYMBOL: {"status": "pending", "activation_id": "first"}}
    engine, path = _engine_with_lifecycle(tmp_path, disk=None, pending=pending)

    engine._write_lifecycles()

    assert json.loads(path.read_text(encoding="utf-8")) == pending
    assert engine._lifecycle_disk_present is True
    assert engine._lifecycle_disk_state == pending[SYMBOL]


def test_malformed_lifecycle_file_is_not_treated_as_missing(tmp_path: Path):
    old = {"status": "pending"}
    engine, path = _engine_with_lifecycle(
        tmp_path,
        disk={SYMBOL: old},
        pending={SYMBOL: {"status": "open"}},
    )
    path.write_text("{", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        engine._write_lifecycles()

    assert path.read_bytes() == b"{"
    assert engine._lifecycle_disk_state == old


def test_non_object_lifecycle_file_is_rejected(tmp_path: Path):
    engine, path = _engine_with_lifecycle(
        tmp_path,
        disk={SYMBOL: {"status": "pending"}},
        pending={SYMBOL: {"status": "open"}},
    )
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Symbol lifecycle file is not an object"):
        engine._write_lifecycles()

    assert path.read_bytes() == b"[]"


def test_failed_atomic_write_does_not_advance_disk_markers(tmp_path: Path, monkeypatch):
    old = {"status": "pending"}
    engine, path = _engine_with_lifecycle(
        tmp_path,
        disk={SYMBOL: old},
        pending={SYMBOL: {"status": "open"}},
    )
    original_bytes = path.read_bytes()

    def fail_write(*_args, **_kwargs):
        raise OSError("simulated atomic write failure")

    monkeypatch.setattr(lifecycle_persistence_module, "atomic_write_json", fail_write)
    with pytest.raises(OSError, match="simulated atomic write failure"):
        engine._write_lifecycles()

    assert path.read_bytes() == original_bytes
    assert engine._lifecycle_disk_present is True
    assert engine._lifecycle_disk_state == old


def test_stale_lifecycle_blocks_tranche_cache_write(tmp_path: Path):
    old = {"status": "open", "activation_id": "old"}
    engine, path = _engine_with_lifecycle(tmp_path, disk={SYMBOL: old}, pending={SYMBOL: old})
    path.write_text(
        json.dumps({SYMBOL: {"status": "closed", "activation_id": "old"}}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="Concurrent symbol lifecycle change"):
        engine._assert_lifecycle_current_for_cache_write(SYMBOL)
