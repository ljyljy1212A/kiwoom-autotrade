import json
from pathlib import Path
from unittest.mock import call, patch

import pytest

from src.core.atomic_write import atomic_write_json


def _temporary_files(path: Path) -> list[Path]:
    return list(path.parent.glob(f"{path.name}.*.tmp"))


def test_atomic_write_json_recovers_on_final_allowed_attempt(tmp_path: Path) -> None:
    destination = tmp_path / "result.json"
    payload = {"status": "ok", "count": 3}
    replace_calls = []
    real_replace = Path.replace

    def replace_with_two_transient_failures(source: Path, target: Path) -> Path:
        replace_calls.append((source, target))
        if len(replace_calls) < 3:
            raise PermissionError("transient replacement failure")
        return real_replace(source, target)

    with patch(
        "src.core.atomic_write.Path.replace",
        autospec=True,
        side_effect=replace_with_two_transient_failures,
    ) as replace, patch("src.core.atomic_write.time.sleep") as sleep:
        atomic_write_json(destination, payload)

    assert replace.call_count == 3
    sleep.assert_has_calls([call(0.075), call(0.075)])
    assert sleep.call_count == 2
    assert json.loads(destination.read_text(encoding="utf-8")) == payload
    assert _temporary_files(destination) == []


def test_atomic_write_json_exhausts_exactly_three_attempts(tmp_path: Path) -> None:
    destination = tmp_path / "result.json"
    replace_calls = []

    def always_fail(source: Path, target: Path) -> Path:
        replace_calls.append((source, target))
        raise PermissionError("persistent replacement failure")

    with patch(
        "src.core.atomic_write.Path.replace",
        autospec=True,
        side_effect=always_fail,
    ) as replace, patch("src.core.atomic_write.time.sleep") as sleep:
        with pytest.raises(PermissionError, match="persistent replacement failure"):
            atomic_write_json(destination, {"status": "failed"})

    assert replace.call_count == 3
    assert len(replace_calls) == 3
    sleep.assert_has_calls([call(0.075), call(0.075)])
    assert sleep.call_count == 2
    assert not destination.exists()
    assert _temporary_files(destination) == []


def test_atomic_write_json_preserves_existing_destination_on_exhaustion(tmp_path: Path) -> None:
    destination = tmp_path / "result.json"
    original_bytes = b'{"status":"existing","value":7}'
    destination.write_bytes(original_bytes)
    replace_calls = []

    def always_fail(source: Path, target: Path) -> Path:
        replace_calls.append((source, target))
        raise PermissionError("persistent replacement failure")

    with patch(
        "src.core.atomic_write.Path.replace",
        autospec=True,
        side_effect=always_fail,
    ) as replace, patch("src.core.atomic_write.time.sleep") as sleep:
        with pytest.raises(PermissionError, match="persistent replacement failure"):
            atomic_write_json(destination, {"status": "new", "value": 8})

    assert replace.call_count == 3
    assert len(replace_calls) == 3
    sleep.assert_has_calls([call(0.075), call(0.075)])
    assert sleep.call_count == 2
    assert destination.read_bytes() == original_bytes
    assert _temporary_files(destination) == []
