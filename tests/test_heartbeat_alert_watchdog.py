import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import heartbeat_alert_watchdog


class HeartbeatAlertWatchdogTests(unittest.TestCase):
    def test_write_startup_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            status_dir = Path(tmpdir)
            with patch.object(heartbeat_alert_watchdog.os, "getpid", return_value=4321):
                heartbeat_alert_watchdog._write_startup_status(status_dir)

            payload = json.loads(
                (status_dir / "heartbeat_alert_watchdog.status.json").read_text(encoding="utf-8")
            )

        self.assertEqual(payload["pid"], 4321)
        self.assertEqual(payload["role"], "heartbeat_alert_watchdog")
        self.assertEqual(payload["account_scope"], ["kr_mock", "us_mock"])
        self.assertTrue(payload["started_at"].endswith("+00:00"))


if __name__ == "__main__":
    unittest.main()
