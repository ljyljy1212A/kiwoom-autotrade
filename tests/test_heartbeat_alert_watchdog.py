import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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

    def _degraded_payload(self, *, entered_at="2026-09-05T00:00:00+00:00"):
        return {
            "account": "us_mock",
            "state": "DEGRADED_FIXED_PORT",
            "entered_at": entered_at,
            "last_collision_at": "2026-09-05T00:00:00+00:00",
            "pid": 16684,
        }

    def test_new_degraded_state_alerts(self):
        payload = self._degraded_payload()
        self.assertTrue(
            heartbeat_alert_watchdog._should_alert_degraded(
                "us_mock", payload, {}, datetime.now(timezone.utc)
            )
        )

    def test_unchanged_degraded_state_is_suppressed(self):
        now = datetime.now(timezone.utc)
        payload = self._degraded_payload()
        state = {
            "us_mock": {
                "identity": list(heartbeat_alert_watchdog._degraded_identity(payload)),
                "last_alerted_at": now.isoformat(),
            }
        }
        self.assertFalse(
            heartbeat_alert_watchdog._should_alert_degraded("us_mock", payload, state, now)
        )

    def test_changed_entered_at_realerts(self):
        now = datetime.now(timezone.utc)
        previous_payload = self._degraded_payload()
        payload = self._degraded_payload(entered_at="2026-09-05T01:00:00+00:00")
        state = {
            "us_mock": {
                "identity": list(heartbeat_alert_watchdog._degraded_identity(previous_payload)),
                "last_alerted_at": now.isoformat(),
            }
        }
        self.assertTrue(
            heartbeat_alert_watchdog._should_alert_degraded("us_mock", payload, state, now)
        )

    def test_unchanged_degraded_state_realerts_after_six_hours(self):
        now = datetime.now(timezone.utc)
        payload = self._degraded_payload()
        state = {
            "us_mock": {
                "identity": list(heartbeat_alert_watchdog._degraded_identity(payload)),
                "last_alerted_at": (now - timedelta(hours=6)).isoformat(),
            }
        }
        self.assertTrue(
            heartbeat_alert_watchdog._should_alert_degraded("us_mock", payload, state, now)
        )

    def test_missing_malformed_or_stale_status_remains_unsuppressed(self):
        now = datetime.now(timezone.utc)
        state = {"us_mock": {"identity": ["DEGRADED_FIXED_PORT"], "last_alerted_at": now.isoformat()}}
        missing = heartbeat_alert_watchdog._problem("us_mock", Path(tempfile.mkdtemp()), now)
        self.assertIsNotNone(missing)
        with tempfile.TemporaryDirectory() as tmpdir:
            status_dir = Path(tmpdir)
            status_path = status_dir / "worker_us_mock.status.json"
            status_path.write_text("not-json", encoding="utf-8")
            malformed = heartbeat_alert_watchdog._problem("us_mock", status_dir, now)
            self.assertIsNotNone(malformed)
            status_path.write_text(
                json.dumps({
                    "account": "us_mock",
                    "market": "US",
                    "pid": 16684,
                    "state": "RUNNING",
                    "updatedAt": "2020-01-01T00:00:00+00:00",
                }),
                encoding="utf-8",
            )
            stale = heartbeat_alert_watchdog._problem("us_mock", status_dir, now)
            self.assertIsNotNone(stale)
        self.assertTrue(state["us_mock"]["identity"])


if __name__ == "__main__":
    unittest.main()
