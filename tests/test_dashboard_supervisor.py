import json
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dashboard import dashboard_server
from src import worker_supervisor


class DashboardSupervisorTests(unittest.TestCase):
    def test_write_startup_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            with patch.object(dashboard_server, "DATA_DIR", data_dir):
                dashboard_server._write_startup_status()

            payload = json.loads((data_dir / "dashboard_server.status.json").read_text(encoding="utf-8"))

        self.assertGreater(payload["pid"], 0)
        self.assertEqual(payload["role"], "dashboard_server")
        self.assertTrue(payload["started_at"].endswith("+00:00"))
        self.assertNotIn("account_scope", payload)

    def test_status_endpoint_preserves_degraded_state_and_liveness(self):
        handler = object.__new__(dashboard_server.Handler)
        handler._path_and_query = lambda: ("/api/status", {})
        handler._json = Mock()
        worker = {
            "account": "kr_mock", "market": "KR", "running": True,
            "state": "DEGRADED_FIXED_PORT",
        }
        with patch.object(dashboard_server, "_worker_statuses", return_value=[worker]):
            handler.do_GET()

        handler._json.assert_called_once_with({
            "running": True,
            "accounts": ["kr_mock"],
            "markets": ["KR"],
            "workers": [worker],
        })

    def test_status_endpoint_surfaces_activity_and_heartbeat_fields(self):
        class FakeLock:
            def liveness_result(self):
                return {"running": True, "liveness": "alive"}

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            (data_dir / "worker_kr_mock.status.json").write_text(
                json.dumps({
                    "account": "kr_mock",
                    "market": "KR",
                    "pid": 123,
                    "instanceId": "instance",
                    "startedAt": "started",
                    "state": "RUNNING",
                    "active_symbols": [],
                    "activityState": "expected-idle",
                    "updatedAt": "2026-09-07T00:00:00+00:00",
                    "processHeartbeatAt": "2026-09-07T00:00:00+00:00",
                    "lastControllerCycleAt": "2026-09-07T00:00:00+00:00",
                }),
                encoding="utf-8",
            )
            (data_dir / "worker_kr_mock.pid").write_text(json.dumps({"pid": 123}), encoding="utf-8")

            with patch.object(worker_supervisor, "_status_path", side_effect=lambda account: data_dir / f"worker_{account}.status.json"), \
                 patch.object(worker_supervisor, "_pid_path", side_effect=lambda account: data_dir / f"worker_{account}.pid"), \
                 patch.object(worker_supervisor, "_worker_lock", return_value=FakeLock()), \
                 patch.object(dashboard_server, "_account_catalog", return_value=[{"id": "kr_mock", "market": "KR"}]), \
                 patch.object(
                     dashboard_server,
                     "_supervisor",
                     side_effect=lambda action, account, market: (0, worker_supervisor.status(account)),
                 ):
                handler = object.__new__(dashboard_server.Handler)
                handler._path_and_query = lambda: ("/api/status", {})
                handler._json = Mock()
                handler.do_GET()

        payload = handler._json.call_args.args[0]
        worker = payload["workers"][0]
        self.assertTrue(payload["running"])
        self.assertEqual(worker["activityState"], "expected-idle")
        self.assertEqual(worker["active_symbols"], [])
        self.assertEqual(worker["processHeartbeatAt"], "2026-09-07T00:00:00+00:00")
        self.assertEqual(worker["lastControllerCycleAt"], "2026-09-07T00:00:00+00:00")

    def test_stop_uses_timeout_covering_graceful_and_forceful_windows(self):
        completed = subprocess.CompletedProcess(args=[], returncode=0,
                                                  stdout='{"stopped": true}\n', stderr='')
        with patch.object(dashboard_server.subprocess, "run", return_value=completed) as run:
            code, payload = dashboard_server._supervisor("stop", "kr_mock", "KR")
        self.assertEqual(code, 0)
        self.assertTrue(payload["stopped"])
        self.assertEqual(run.call_args.kwargs["timeout"], 20)

    def test_stop_timeout_returns_a_structured_failure(self):
        with patch.object(dashboard_server.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("worker_supervisor", 20)):
            code, payload = dashboard_server._supervisor("stop", "kr_mock", "KR")
        self.assertEqual(code, 4)
        self.assertFalse(payload["stopped"])
        self.assertEqual(payload["reason"], "dashboard-supervisor-timeout")

    def test_dashboard_start_propagates_supervisor_authorization(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"started": true}\n', stderr=""
        )
        with patch.dict(os.environ, {"ALLOW_LIVE_DASHBOARD": "true"}, clear=False), \
             patch.object(dashboard_server.subprocess, "run", return_value=completed) as run:
            dashboard_server._supervisor("start", "synthetic_catalog_real", "KR")
            start_env = run.call_args.kwargs["env"]
            self.assertEqual(start_env["ALLOW_LIVE_SUPERVISOR"], "true")

            dashboard_server._supervisor("stop", "synthetic_catalog_real", "KR")
            stop_env = run.call_args.kwargs["env"]
            self.assertNotIn("ALLOW_LIVE_SUPERVISOR", stop_env)

        with patch.dict(os.environ, {}, clear=False), \
             patch.object(dashboard_server.subprocess, "run", return_value=completed) as run:
            os.environ.pop("ALLOW_LIVE_DASHBOARD", None)
            dashboard_server._supervisor("start", "synthetic_catalog_real", "KR")
            self.assertNotIn("ALLOW_LIVE_SUPERVISOR", run.call_args.kwargs["env"])

    def test_dashboard_start_without_dashboard_authorization_returns_403(self):
        body = json.dumps({"accounts": ["synthetic_catalog_real"]}).encode()
        handler = object.__new__(dashboard_server.Handler)
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler._path_and_query = lambda: ("/api/start", {})
        response = Mock()
        handler._json = response
        accounts = [{"id": "synthetic_catalog_real", "market": "KR", "mode": "real"}]
        with patch.dict(os.environ, {}, clear=False), \
             patch.object(dashboard_server, "_account_catalog", return_value=accounts), \
             patch.object(dashboard_server, "_supervisor") as supervisor_call:
            os.environ.pop("ALLOW_LIVE_DASHBOARD", None)
            handler.do_POST()

        response.assert_called_once_with({"error": "Live accounts are disabled by the dashboard"}, 403)
        supervisor_call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
