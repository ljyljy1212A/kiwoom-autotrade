import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import tools.worker_watchdog as watchdog


class WorkerWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_patch = patch.object(watchdog, "WATCHDOG_LOG", MagicMock())
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)
        self.state_path = Path(self.tmp.name) / "watchdog_state.json"
        self.status_dir = Path(self.tmp.name) / "status"

    def _patch_state_path(self):
        return patch.object(watchdog, "WATCHDOG_STATE", self.state_path)

    def _write_status(self, account="kr_mock", market="KR", pid=222, state="RUNNING", updated_at=None):
        self.status_dir.mkdir(exist_ok=True)
        payload = {
            "account": account,
            "market": market,
            "pid": pid,
            "instanceId": "instance",
            "startedAt": "started",
            "state": state,
            "updatedAt": (updated_at or datetime.now(timezone.utc)).isoformat(),
        }
        (self.status_dir / f"worker_{account}.status.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def _patch_paths(self):
        return self._patch_state_path(), patch.object(watchdog, "STATUS_DIR", self.status_dir)

    def _marker_path(self, account="kr_mock"):
        marker_dir = Path(self.tmp.name) / "markers"
        marker_dir.mkdir(exist_ok=True)
        return marker_dir / f"intentional_stop_{account}.json", marker_dir

    def _write_marker(self, account="kr_mock", expires_at=None, **fields):
        path, marker_dir = self._marker_path(account)
        payload = {"account": account, "instanceId": "instance"}
        payload.update(fields)
        if expires_at is not None:
            payload["expiresAt"] = expires_at.isoformat()
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path, marker_dir

    def test_intentional_stop_marker_absent_is_inactive(self):
        marker_dir = Path(self.tmp.name) / "markers"
        with patch.object(watchdog, "DATA_DIR", marker_dir):
            active, detail = watchdog._intentional_stop_active("kr_mock")

        self.assertFalse(active)
        self.assertIn("absent", detail)

    def test_intentional_stop_marker_future_expiry_is_active(self):
        _, marker_dir = self._write_marker(
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5)
        )
        with patch.object(watchdog, "DATA_DIR", marker_dir):
            active, detail = watchdog._intentional_stop_active("kr_mock")

        self.assertTrue(active)
        self.assertIn("expiresAt=", detail)

    def test_intentional_stop_marker_past_expiry_is_inactive(self):
        _, marker_dir = self._write_marker(
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=5)
        )
        with patch.object(watchdog, "DATA_DIR", marker_dir):
            active, detail = watchdog._intentional_stop_active("kr_mock")

        self.assertFalse(active)
        self.assertEqual(detail, "marker expired")

    def test_intentional_stop_marker_malformed_or_missing_expiry_is_inactive(self):
        path, marker_dir = self._marker_path()
        with patch.object(watchdog, "DATA_DIR", marker_dir):
            path.write_text("{not-json", encoding="utf-8")
            self.assertFalse(watchdog._intentional_stop_active("kr_mock")[0])
            path.write_text(json.dumps({"account": "kr_mock"}), encoding="utf-8")
            self.assertFalse(watchdog._intentional_stop_active("kr_mock")[0])

    def test_intentional_stop_marker_mismatched_account_is_inactive(self):
        path, marker_dir = self._marker_path("kr_mock")
        path.write_text(
            json.dumps({
                "account": "us_mock",
                "instanceId": "instance",
                "expiresAt": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            }),
            encoding="utf-8",
        )
        with patch.object(watchdog, "DATA_DIR", marker_dir):
            active, detail = watchdog._intentional_stop_active("kr_mock")

        self.assertFalse(active)
        self.assertEqual(detail, "marker account mismatch")

    def test_dead_worker_with_active_marker_skips_relaunch_without_counting_failure(self):
        state = {"kr_mock": {"consecutive_failures": 2, "alerted": False}}
        _, marker_dir = self._write_marker(
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5)
        )
        with patch.object(watchdog, "DATA_DIR", marker_dir), \
             patch.object(watchdog, "_load_state", return_value=state), \
             patch.object(watchdog, "_account_state", return_value=state["kr_mock"]), \
             patch.object(watchdog.worker_supervisor, "status", return_value={"running": False, "pid": 333}), \
             patch.object(watchdog, "check_duplicate_live_process", return_value=False), \
             patch.object(watchdog, "_classify", return_value=("dead", "account mutex is not alive")), \
             patch.object(watchdog.worker_supervisor, "start") as start, \
             patch.object(watchdog, "_send_notification"):
            watchdog.check_and_restart("kr_mock", "KR")

        start.assert_not_called()
        self.assertEqual(state["kr_mock"]["consecutive_failures"], 2)
        self.assertNotIn("cooldown_until", state["kr_mock"])

    def test_dead_worker_without_marker_preserves_relaunch_behavior(self):
        marker_dir = Path(self.tmp.name) / "markers"
        state = {}
        with patch.object(watchdog, "DATA_DIR", marker_dir), \
             patch.object(watchdog, "_load_state", return_value=state), \
             patch.object(watchdog.worker_supervisor, "status", return_value={"running": False, "pid": 333}), \
             patch.object(watchdog, "check_duplicate_live_process", return_value=False), \
             patch.object(watchdog, "_classify", return_value=("dead", "account mutex is not alive")), \
             patch.object(watchdog.worker_supervisor, "start", return_value=(0, {"started": True})) as start, \
             patch.object(watchdog, "_send_notification"):
            watchdog.check_and_restart("kr_mock", "KR")

        start.assert_called_once_with("kr_mock", "KR")

    def test_healthy_matching_fresh_heartbeat_resets_prior_failure_state(self):
        self._write_status()
        self.state_path.write_text(json.dumps({"kr_mock": {"consecutive_failures": 2, "alerted": True}}), encoding="utf-8")
        state_patch, status_patch = self._patch_paths()
        with state_patch, status_patch, \
             patch.object(watchdog.worker_supervisor, "status", return_value={"running": True, "pid": 222}), \
             patch.object(watchdog.worker_supervisor, "start") as start, \
             patch.object(watchdog, "_send_notification") as notify:
            watchdog.check_and_restart("kr_mock", "KR")

        start.assert_not_called()
        notify.assert_called_once()
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["kr_mock"]["consecutive_failures"], 0)
        self.assertFalse(payload["kr_mock"]["alerted"])

    def test_dead_classification_relaunches_once_without_kill_path(self):
        state_patch, status_patch = self._patch_paths()
        with state_patch, status_patch, \
             patch.object(watchdog.worker_supervisor, "status", return_value={"running": False, "pid": 333}), \
             patch.object(watchdog.worker_supervisor, "start", return_value=(0, {"started": True})) as start, \
             patch.object(watchdog, "_send_notification"):
            watchdog.check_and_restart("us_mock", "US")

        start.assert_called_once_with("us_mock", "US")

    def test_lock_conflict_does_not_count_as_failure_or_notify_relaunch_failure(self):
        self.state_path.write_text(
            json.dumps({"kr_mock": {"consecutive_failures": 1}}), encoding="utf-8"
        )
        state_patch, status_patch = self._patch_paths()
        with state_patch, status_patch, \
             patch.object(watchdog.worker_supervisor, "status", return_value={"running": False, "pid": 333}), \
             patch.object(
                 watchdog.worker_supervisor,
                 "start",
                 return_value=(3, {"started": False, "failureClass": "lock-conflict"}),
             ) as start, \
             patch.object(watchdog, "_send_notification") as notify:
            watchdog.check_and_restart("kr_mock", "KR")

        start.assert_called_once_with("kr_mock", "KR")
        notify.assert_not_called()
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["kr_mock"]["consecutive_failures"], 1)

    def test_stale_live_worker_is_suspect_and_never_relaunched(self):
        self._write_status(updated_at=datetime.now(timezone.utc) - timedelta(seconds=121))
        state_patch, status_patch = self._patch_paths()
        with state_patch, status_patch, \
             patch.object(watchdog.worker_supervisor, "status", return_value={"running": True, "pid": 222}), \
             patch.object(watchdog.worker_supervisor, "start") as start, \
             patch.object(watchdog, "_send_notification") as notify:
            watchdog.check_and_restart("kr_mock", "KR")

        start.assert_not_called()
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[2], "suspect")

    def test_third_nonhealthy_classification_trips_breaker_and_cooldown_blocks_relaunch(self):
        state_patch, status_patch = self._patch_paths()
        with state_patch, status_patch, \
             patch.object(watchdog.worker_supervisor, "status", return_value={"running": False, "pid": 111}), \
             patch.object(watchdog.worker_supervisor, "start", return_value=(0, {"started": True})) as start, \
             patch.object(watchdog, "_send_notification") as notify:
            watchdog.check_and_restart("kr_mock", "KR")
            watchdog.check_and_restart("kr_mock", "KR")
            watchdog.check_and_restart("kr_mock", "KR")
            watchdog.check_and_restart("kr_mock", "KR")

        self.assertEqual(start.call_count, 2)
        breaker_calls = [call for call in notify.call_args_list if call.args[2] == "circuit-breaker"]
        self.assertEqual(len(breaker_calls), 1)
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["kr_mock"]["consecutive_failures"], 4)
        self.assertTrue(payload["kr_mock"]["cooldown_until"])

    def test_healthy_status_is_required_to_reset_cooldown_state(self):
        self.state_path.write_text(json.dumps({"kr_mock": {"consecutive_failures": 2, "alerted": True}}), encoding="utf-8")
        self._write_status(state="DEGRADED_FIXED_PORT")
        state_patch, status_patch = self._patch_paths()
        with state_patch, status_patch, \
             patch.object(watchdog.worker_supervisor, "status", return_value={"running": True, "pid": 222}), \
             patch.object(watchdog.worker_supervisor, "start") as start, \
             patch.object(watchdog, "_send_notification"):
            watchdog.check_and_restart("kr_mock", "KR")

        start.assert_not_called()
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["kr_mock"]["consecutive_failures"], 3)
        self.assertTrue(payload["kr_mock"]["alerted"])

    def test_unallowlisted_target_is_rejected_before_status_or_start(self):
        with patch.object(watchdog.worker_supervisor, "status") as status, \
             patch.object(watchdog.worker_supervisor, "start") as start:
            watchdog.check_and_restart("kr_real", "KR")

        status.assert_not_called()
        start.assert_not_called()

    def test_duplicate_process_short_circuits_before_classification(self):
        account_state = {
            "consecutive_failures": 0,
            "alerted": False,
            "suspect_alerted": False,
            "last_attempt_at": "",
            "cooldown_until": "",
        }
        with patch.object(watchdog, "_load_state", return_value={}), \
             patch.object(watchdog, "_account_state", return_value=account_state), \
             patch.object(watchdog.worker_supervisor, "status", return_value={"pid": 222, "running": True}), \
             patch.object(watchdog, "check_duplicate_live_process", return_value=True) as duplicate, \
             patch.object(watchdog, "_classify") as classify, \
             patch.object(watchdog, "_save_state") as save_state:
            watchdog.check_and_restart("kr_mock", "KR")

        duplicate.assert_called_once_with("kr_mock", 222, watchdog.enumerate_worker_processes)
        classify.assert_not_called()
        save_state.assert_not_called()

    def test_duplicate_process_false_preserves_classification_flow(self):
        account_state = {
            "consecutive_failures": 0,
            "alerted": False,
            "suspect_alerted": False,
            "last_attempt_at": "",
            "cooldown_until": "",
        }
        with patch.object(watchdog, "_load_state", return_value={}), \
             patch.object(watchdog, "_account_state", return_value=account_state), \
             patch.object(watchdog.worker_supervisor, "status", return_value={"pid": 222, "running": True}), \
             patch.object(watchdog, "check_duplicate_live_process", return_value=False), \
             patch.object(watchdog, "_classify", return_value=("suspect", "test")) as classify, \
             patch.object(watchdog, "_save_state"):
            watchdog.check_and_restart("kr_mock", "KR")

        classify.assert_called_once_with("kr_mock", "KR", {"pid": 222, "running": True})

    def test_duplicate_process_check_exception_is_logged_and_sweep_continues(self):
        account_state = {
            "consecutive_failures": 0,
            "alerted": False,
            "suspect_alerted": False,
            "last_attempt_at": "",
            "cooldown_until": "",
        }
        with patch.object(watchdog, "_load_state", return_value={}), \
             patch.object(watchdog, "_account_state", return_value=account_state), \
             patch.object(watchdog.worker_supervisor, "status", return_value={"pid": 222, "running": True}), \
             patch.object(watchdog, "check_duplicate_live_process", side_effect=RuntimeError("query failed")), \
             patch.object(watchdog, "_classify", return_value=("suspect", "test")) as classify, \
             patch.object(watchdog, "_send_notification") as notify, \
             patch.object(watchdog, "_save_state"), \
             patch.object(watchdog.WATCHDOG_LOG, "error") as error:
            watchdog.check_and_restart("kr_mock", "KR")

        classify.assert_called_once_with("kr_mock", "KR", {"pid": 222, "running": True})
        notify.assert_called_once()
        error.assert_called_once_with("[kr_mock] duplicate-process check failed: query failed")


if __name__ == "__main__":
    unittest.main()
