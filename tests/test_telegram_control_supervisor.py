from __future__ import annotations

from types import SimpleNamespace

from tools import telegram_control_supervisor as supervisor


class _Logger:
    def __init__(self, log_path):
        self.log_path = log_path
        self.messages = []

    def exception(self, message):
        self.messages.append(message)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_text(message, encoding="utf-8")


def test_supervisor_dispatches_stubbed_bot_main(monkeypatch, tmp_path):
    logger = _Logger(tmp_path / "telegram_control_supervisor.log")
    monkeypatch.setattr(supervisor, "DATA_DIR", tmp_path)
    monkeypatch.setattr(supervisor, "get_logger", lambda _name, _path: logger)
    calls = []
    monkeypatch.setattr(
        supervisor.importlib,
        "import_module",
        lambda name: calls.append(name) or SimpleNamespace(main=lambda: 0),
    )

    assert supervisor.main() == 0
    assert calls == ["src.notify.telegram_control_bot"]
    assert logger.messages == []


def test_supervisor_logs_and_returns_one_for_forced_import_exception(monkeypatch, tmp_path):
    log_path = tmp_path / "telegram_control_supervisor.log"
    logger = _Logger(log_path)
    monkeypatch.setattr(supervisor, "DATA_DIR", tmp_path)
    monkeypatch.setattr(supervisor, "get_logger", lambda _name, _path: logger)

    def raise_import(_name):
        raise RuntimeError("forced import failure")

    monkeypatch.setattr(supervisor.importlib, "import_module", raise_import)

    assert supervisor.main() == 1
    assert logger.messages == ["Telegram control supervisor failed"]
    assert log_path.read_text(encoding="utf-8") == "Telegram control supervisor failed"
