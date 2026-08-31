from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import urlopen

import pytest

from dashboard.dashboard_server import Handler, ReusableThreadingHTTPServer
from tools import scheduled_task_healthcheck as healthcheck


TASKS = [
    {"task_name": "Kiwoom Worker - KR Mock", "task_path": "\\", "target_path": "C:\\scratch\\worker_kr.py"},
    {"task_name": "Kiwoom Worker - US Mock", "task_path": "\\", "target_path": "C:\\scratch\\worker_us.py"},
    {"task_name": "Kiwoom Worker Watchdog", "task_path": "\\", "target_path": "C:\\scratch\\watchdog.py"},
    {"task_name": "Kiwoom Worker Watchdog", "task_path": "\\WD_Test\\", "target_path": "C:\\scratch\\wd_test2.py"},
    {"task_name": "Kiwoom Heartbeat Alert", "task_path": "\\", "target_path": "C:\\scratch\\heartbeat.py"},
    {"task_name": "Kiwoom Telegram Control Bot", "task_path": "\\", "target_path": "C:\\scratch\\telegram.py"},
    {"task_name": "Kiwoom Project Database Backup", "task_path": "\\", "target_path": "C:\\scratch\\database_backup.py"},
    {"task_name": "Kiwoom Project Files Backup", "task_path": "\\", "target_path": "C:\\scratch\\files_backup.py"},
]


@contextmanager
def _server(server_class, handler):
    server = server_class(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


class _ProbeHandler(BaseHTTPRequestHandler):
    mode = "healthy"

    def do_GET(self):  # noqa: N802
        if self.mode == "timeout":
            time.sleep(0.2)
        if self.mode == "non_200":
            self.send_response(500)
            self.end_headers()
            return
        body = b'{"service":"dashboard","status":"wrong"}' if self.mode == "wrong_json" else b'{"service":"dashboard","status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def log_message(self, *_args):
        return


class _Logger:
    def __init__(self):
        self.messages = []

    def error(self, message, *args):
        self.messages.append(message % args if args else message)


def test_real_dashboard_health_route_returns_exact_payload_on_ephemeral_port():
    with _server(ReusableThreadingHTTPServer, Handler) as server:
        url = f"http://127.0.0.1:{server.server_address[1]}/api/health"
        with urlopen(url, timeout=1) as response:
            assert response.status == 200
            assert json.loads(response.read()) == {"service": "dashboard", "status": "ok"}


def test_dashboard_probe_accepts_healthy_real_http_response():
    with _server(ThreadingHTTPServer, _ProbeHandler) as server:
        spec = healthcheck.DashboardSpec(f"http://127.0.0.1:{server.server_address[1]}/api/health", 1)
        assert healthcheck._dashboard_problems(spec) == []


@pytest.mark.parametrize("mode, expected", [
    ("timeout", "HTTP probe failed"),
    ("non_200", "HTTP status is 500"),
    ("wrong_json", "unexpected health payload"),
])
def test_dashboard_probe_reports_real_scratch_failure_modes(mode, expected):
    _ProbeHandler.mode = mode
    try:
        with _server(ThreadingHTTPServer, _ProbeHandler) as server:
            spec = healthcheck.DashboardSpec(f"http://127.0.0.1:{server.server_address[1]}/api/health", 0.05)
            assert expected in healthcheck._dashboard_problems(spec)[0]
    finally:
        _ProbeHandler.mode = "healthy"


def test_dashboard_probe_reports_forced_refused_connection_and_uses_alert_format():
    spec = healthcheck.DashboardSpec("http://127.0.0.1:1/api/health", 0.05)
    with patch.object(healthcheck, "urlopen", side_effect=URLError(ConnectionRefusedError("forced refusal"))):
        problems = healthcheck._dashboard_problems(spec)
    logger = _Logger()
    message = "DASHBOARD HEALTHCHECK ALERT\nTarget: " + spec.url + "\nProblems:\n- " + "\n- ".join(problems)
    with patch.object(healthcheck, "_send_alert") as send_alert:
        healthcheck._emit_alert(message, logger, dry_run=True)
    assert "forced refusal" in problems[0]
    assert logger.messages == [message, "[DRY-RUN] would alert: " + message]
    send_alert.assert_not_called()


def test_load_config_accepts_legacy_and_dashboard_object_formats(tmp_path):
    legacy_path = Path(tmp_path) / "legacy.json"
    object_path = Path(tmp_path) / "object.json"
    legacy_path.write_text(json.dumps(TASKS), encoding="utf-8")
    object_path.write_text(json.dumps({
        "tasks": TASKS,
        "dashboard": {"url": "http://127.0.0.1:39999/api/health", "timeout_seconds": 2},
    }), encoding="utf-8")

    legacy_tasks, legacy_dashboard = healthcheck._load_config(legacy_path)
    object_tasks, dashboard = healthcheck._load_config(object_path)

    assert legacy_dashboard is None
    assert [task.task_name for task in legacy_tasks] == [task.task_name for task in object_tasks]
    assert len(object_tasks) == 8
    assert dashboard == healthcheck.DashboardSpec("http://127.0.0.1:39999/api/health", 2.0)
    assert healthcheck._load_tasks(object_path) == object_tasks
