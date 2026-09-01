import os
import tempfile
import unittest
from unittest.mock import patch

from src import worker_supervisor as supervisor
from src.core.process_lock import ProcessLock
import tools.worker_watchdog as watchdog


class FakeFunction:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


class FakeKernel32:
    def __init__(self, handle=None, wait_state=258):
        self.handle = handle
        self.wait_state = wait_state
        self.OpenMutexW = FakeFunction(lambda *_args: self.handle)
        self.WaitForSingleObject = FakeFunction(lambda *_args: self.wait_state)
        self.ReleaseMutex = FakeFunction(lambda *_args: True)
        self.CloseHandle = FakeFunction(lambda *_args: True)


@unittest.skipUnless(os.name == "nt", "Windows named mutex liveness semantics are verified on Windows")
class Round1577Tests(unittest.TestCase):
    def test_1_mutex_absent_error_2_is_dead(self):
        lock = ProcessLock("mock_a")
        fake = FakeKernel32(handle=None)
        with patch("src.core.process_lock.ctypes.WinDLL", return_value=fake), \
             patch("src.core.process_lock.ctypes.get_last_error", return_value=2), \
             patch("src.core.process_lock.LOCK_LOG.warning") as warning:
            result = lock.liveness_result()
        self.assertEqual(result["liveness"], "dead")
        self.assertFalse(result["running"])
        self.assertEqual(result["livenessError"], 2)
        self.assertIn("winerror=2 classification=dead", warning.call_args.args[0] % warning.call_args.args[1:])

    def test_2_access_denied_live_is_suspect_and_callers_refuse(self):
        lock = ProcessLock("mock_a")
        fake = FakeKernel32(handle=None)
        with patch("src.core.process_lock.ctypes.WinDLL", return_value=fake), \
             patch("src.core.process_lock.ctypes.get_last_error", return_value=5), \
             patch.object(supervisor, "_worker_lock", return_value=lock), \
             patch.object(supervisor, "_pid_alive", return_value=True), \
             patch.object(supervisor, "_process_creation_time", return_value=None), \
             patch.object(supervisor.subprocess, "Popen") as popen:
            with patch.object(supervisor, "status", return_value={"running": True, "liveness": "suspect", "livenessError": 5}):
                code, payload = supervisor.start("kr_mock", "KR")
            self.assertEqual((code, payload["reason"]), (8, "status-indeterminate"))
            popen.assert_not_called()

    def test_3_access_denied_dead_pid_remains_suspect(self):
        with patch.object(supervisor, "_pid_alive", return_value=False), \
             patch.object(supervisor, "_process_creation_time") as creation:
            result = supervisor._corroborate_suspect("kr_mock", {"account": "kr_mock", "pid": 123, "instanceId": "i", "startedAt": "started"})
        self.assertFalse(result["suspectPidAlive"])
        self.assertFalse(result["suspectCreationTimeMatch"])
        creation.assert_not_called()

    def test_4_absent_mutex_and_pid_preserves_already_stopped(self):
        with patch.object(supervisor, "status", return_value={"running": False, "liveness": "dead", "livenessError": 2, "pid": 0}):
            code, payload = supervisor.stop("kr_mock")
        self.assertEqual(code, 0)
        self.assertEqual(payload["mode"], "already_stopped")

    def test_5_pid_reuse_mismatch_remains_suspect(self):
        with patch.object(supervisor, "_pid_alive", return_value=True), \
             patch.object(supervisor, "_process_creation_time", return_value=supervisor.datetime.now(supervisor.timezone.utc)):
            result = supervisor._corroborate_suspect("kr_mock", {
                "account": "kr_mock", "pid": 123, "instanceId": "i",
                "startedAt": "2000-01-01T00:00:00+00:00",
            })
        self.assertFalse(result["suspectCreationTimeMatch"])

    def test_6_release_window_error_2_is_dead(self):
        lock = ProcessLock("mock_a")
        fake = FakeKernel32(handle=None)
        with patch("src.core.process_lock.ctypes.WinDLL", return_value=fake), \
             patch("src.core.process_lock.ctypes.get_last_error", return_value=2):
            result = lock.liveness_result()
        self.assertEqual(result["liveness"], "dead")

    def test_7_unexpected_wait_result_is_suspect_and_stop_kill_watchdog_do_not_act(self):
        lock = ProcessLock("mock_a")
        fake = FakeKernel32(handle=1, wait_state=1)
        with patch("src.core.process_lock.ctypes.WinDLL", return_value=fake), \
             patch("src.core.process_lock.ctypes.get_last_error", return_value=123), \
             patch("src.core.process_lock.LOCK_LOG.warning") as warning:
            result = lock.liveness_result()
        self.assertEqual(result["liveness"], "suspect")
        self.assertEqual(result["livenessError"], 123)
        self.assertIn("winerror=123 classification=suspect", warning.call_args.args[0] % warning.call_args.args[1:])

        suspect = {"running": True, "liveness": "suspect", "livenessError": 5, "pid": 123}
        with patch.object(supervisor, "status", return_value=suspect), patch.object(supervisor, "_write_atomic") as write:
            stop_code, stop_payload = supervisor.stop("kr_mock")
            kill_code, kill_payload = supervisor.kill("kr_mock")
        self.assertEqual((stop_code, stop_payload["reason"]), (8, "status-indeterminate"))
        self.assertEqual((kill_code, kill_payload["reason"]), (8, "status-indeterminate"))
        write.assert_not_called()

        state = {"kr_mock": {"consecutive_failures": 0, "alerted": False, "suspect_alerted": False}}
        with patch.object(watchdog, "_load_state", return_value=state), \
             patch.object(watchdog, "_account_state", return_value=state["kr_mock"]), \
             patch.object(watchdog.worker_supervisor, "status", return_value=suspect), \
             patch.object(watchdog, "check_duplicate_live_process", return_value=False), \
             patch.object(watchdog, "_send_notification"), \
             patch.object(watchdog.worker_supervisor, "start") as start, \
             patch.object(watchdog, "_save_state"):
            watchdog.check_and_restart("kr_mock", "KR")
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
