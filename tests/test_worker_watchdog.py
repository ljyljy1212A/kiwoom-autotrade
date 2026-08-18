import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import tools.worker_watchdog as watchdog


class WorkerWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "watchdog_state.json"

    def _patch_state_path(self):
        return patch.object(watchdog, "WATCHDOG_STATE", self.state_path)

    def test_restart_suppression_trips_after_three_failures(self):
        with self._patch_state_path(), \
             patch.object(watchdog.worker_supervisor, "status", return_value={"running": False, "pid": 111}), \
             patch.object(watchdog.worker_supervisor, "start", return_value=(3, {"started": False, "reason": "refused"})) as start, \
             patch.object(watchdog, "urlopen") as urlopen_mock, \
             patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "chat"}, clear=False), \
             patch.object(watchdog.WATCHDOG_LOG, "warning"), \
             patch.object(watchdog.WATCHDOG_LOG, "error"), \
             patch.object(watchdog.WATCHDOG_LOG, "info"), \
             patch.object(watchdog.WATCHDOG_LOG, "debug"), \
             patch.object(watchdog.WATCHDOG_LOG, "critical"):
            watchdog.check_and_restart("kr_mock", "KR")
            watchdog.check_and_restart("kr_mock", "KR")
            watchdog.check_and_restart("kr_mock", "KR")

        self.assertEqual(start.call_count, 2)
        urlopen_mock.assert_called_once()
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["kr_mock"]["consecutive_failures"], 3)
        self.assertTrue(payload["kr_mock"]["alerted"])

    def test_running_worker_resets_failure_state_without_restart(self):
        self.state_path.write_text(json.dumps({"kr_mock": {"consecutive_failures": 2, "alerted": True}}), encoding="utf-8")
        with self._patch_state_path(), \
             patch.object(watchdog.worker_supervisor, "status", return_value={"running": True, "pid": 222}), \
             patch.object(watchdog.worker_supervisor, "start") as start, \
             patch.object(watchdog.WATCHDOG_LOG, "debug"):
            watchdog.check_and_restart("kr_mock", "KR")

        start.assert_not_called()
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["kr_mock"], {"consecutive_failures": 0, "alerted": False})

    def test_restart_path_delegates_only_to_supervisor_start_once(self):
        with self._patch_state_path(), \
             patch.object(watchdog.worker_supervisor, "status", return_value={"running": False, "pid": 333}), \
             patch.object(watchdog.worker_supervisor, "start", return_value=(0, {"started": True})) as start, \
             patch.object(watchdog.WATCHDOG_LOG, "warning"), \
             patch.object(watchdog.WATCHDOG_LOG, "info"), \
             patch.object(watchdog.WATCHDOG_LOG, "debug"), \
             patch.object(watchdog.WATCHDOG_LOG, "error"):
            watchdog.check_and_restart("us_mock", "US")

        start.assert_called_once_with("us_mock", "US")

    def test_running_status_blocks_a_second_watchdog_start(self):
        with self._patch_state_path(), \
             patch.object(watchdog.worker_supervisor, "status", side_effect=[
                 {"running": False, "pid": 333},
                 {"running": True, "pid": 333},
             ]) as status, \
             patch.object(watchdog.worker_supervisor, "start", return_value=(0, {"started": True})) as start, \
             patch.object(watchdog.WATCHDOG_LOG, "warning"), \
             patch.object(watchdog.WATCHDOG_LOG, "info"), \
             patch.object(watchdog.WATCHDOG_LOG, "debug"), \
             patch.object(watchdog.WATCHDOG_LOG, "error"):
            watchdog.check_and_restart("kr_mock", "KR")
            watchdog.check_and_restart("kr_mock", "KR")

        self.assertEqual(status.call_count, 2)
        start.assert_called_once_with("kr_mock", "KR")

    def test_lock_conflict_is_distinguished_and_not_counted_as_crash(self):
        with self._patch_state_path(), \
             patch.object(watchdog.worker_supervisor, "status", return_value={"running": False}), \
             patch.object(
                 watchdog.worker_supervisor,
                 "start",
                 return_value=(3, {"started": False, "failureClass": "lock-conflict"}),
             ), \
             patch.object(watchdog.WATCHDOG_LOG, "warning") as warning, \
             patch.object(watchdog.WATCHDOG_LOG, "error"):
            watchdog.check_and_restart("kr_mock", "KR")

        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["kr_mock"]["consecutive_failures"], 0)
        self.assertGreaterEqual(warning.call_count, 1)
        self.assertIn("existing worker", warning.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
