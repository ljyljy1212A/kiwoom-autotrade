"""Regression coverage for logger startup when diagnostic sinks are unavailable."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from src.utils import logger as logger_module


def test_get_logger_survives_file_and_stderr_fallback_failures(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(logger_module, "_INITIALIZED", False)
    monkeypatch.setattr(logger_module, "_REGISTERED_FILES", set())
    monkeypatch.setattr(logger_module._logger, "add", Mock(side_effect=OSError("sink unavailable")))
    monkeypatch.setattr(logger_module.sys, "stderr", None)

    result = logger_module.get_logger("kr_mock", tmp_path / "app.log")

    assert result is not None
    assert callable(result.info)
