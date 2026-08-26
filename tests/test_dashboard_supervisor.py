import subprocess
import unittest
from unittest.mock import Mock, patch

from dashboard import dashboard_server


class DashboardSupervisorTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
