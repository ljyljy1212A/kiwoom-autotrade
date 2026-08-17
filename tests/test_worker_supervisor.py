import os
import unittest
from unittest.mock import MagicMock, patch

from src import worker_supervisor as supervisor


class WorkerSupervisorStopTests(unittest.TestCase):
    def test_wait_requires_process_exit_and_lock_release(self):
        lock = MagicMock()
        lock.is_alive.side_effect = [True, False]
        with patch.object(supervisor, "_worker_lock", return_value=lock), \
             patch.object(supervisor, "_pid_alive", side_effect=[False, False]), \
             patch.object(supervisor.time, "sleep"):
            self.assertTrue(supervisor._wait_for_stopped("kr_mock", 123, 1))

    def test_stop_escalates_to_taskkill_on_windows(self):
        running = {"account": "kr_mock", "pid": 123, "running": True,
                   "instanceId": "instance", "state": "RUNNING", "market": "KR"}
        stopped = {**running, "running": False, "state": "STOPPED"}
        with patch.object(supervisor, "status", side_effect=[running, stopped]), \
             patch.object(supervisor, "_owned_identity", return_value=(123, "instance")), \
             patch.object(supervisor, "_write_atomic"), \
             patch.object(supervisor, "_wait_for_stopped", side_effect=[False, True]), \
             patch.object(supervisor, "_remove_pid_after_confirmed_exit") as remove, \
             patch.object(supervisor.sys, "platform", "win32"), \
             patch.object(supervisor.subprocess, "run") as taskkill:
            taskkill.return_value.returncode = 0
            code, payload = supervisor.stop("kr_mock")
        self.assertEqual(code, 0)
        self.assertEqual(payload["mode"], "forced")
        taskkill.assert_called_once_with(["taskkill", "/PID", "123", "/T", "/F"], check=False,
                                         stdout=supervisor.subprocess.DEVNULL,
                                         stderr=supervisor.subprocess.DEVNULL)
        remove.assert_called_once_with("kr_mock", 123)

    @unittest.skipUnless(os.name != "nt", "POSIX-only signal test")
    def test_stop_escalates_to_sigkill_on_posix(self):
        running = {"account": "kr_mock", "pid": 123, "running": True,
                   "instanceId": "instance", "state": "RUNNING", "market": "KR"}
        stopped = {**running, "running": False, "state": "STOPPED"}
        with patch.object(supervisor, "status", side_effect=[running, stopped]), \
             patch.object(supervisor, "_owned_identity", return_value=(123, "instance")), \
             patch.object(supervisor, "_write_atomic"), \
             patch.object(supervisor, "_wait_for_stopped", side_effect=[False, True]), \
             patch.object(supervisor, "_remove_pid_after_confirmed_exit") as remove, \
             patch.object(supervisor.sys, "platform", "linux"), \
             patch.object(supervisor.os, "kill") as sigkill:
            code, payload = supervisor.stop("kr_mock")
        self.assertEqual(code, 0)
        self.assertEqual(payload["mode"], "forced")
        sigkill.assert_any_call(123, supervisor.signal.SIGKILL)
        remove.assert_called_once_with("kr_mock", 123)

    def test_stop_records_stopped_status_only_after_confirmed_exit(self):
        lock = MagicMock()
        lock.is_alive.return_value = False
        with patch.object(supervisor, "_worker_lock", return_value=lock), \
             patch.object(supervisor, "_pid_alive", return_value=False), \
             patch.object(supervisor, "_status_path") as status_path, \
             patch.object(supervisor, "_write_atomic") as write:
            status_path.return_value.read_text.return_value = (
                '{"account":"kr_mock","pid":123,"state":"RUNNING"}'
            )
            supervisor._record_stopped_status("kr_mock", 123)
        payload = write.call_args.args[1]
        self.assertEqual(payload["state"], "STOPPED")
        self.assertEqual(payload["pid"], 123)

    def test_start_prefers_persisted_control_state(self):
        child = MagicMock(pid=999, returncode=0)
        child.poll.return_value = None
        running = {"account": "kr_mock", "pid": 999, "running": True,
                   "instanceId": "instance", "state": "RUNNING", "market": "KR"}
        with patch.object(supervisor, "status", side_effect=[{"account": "kr_mock", "pid": 0, "running": False}, running]), \
             patch.object(supervisor, "read_auto_trading_enabled", return_value=True), \
             patch.object(supervisor.subprocess, "Popen", return_value=child) as popen, \
             patch.object(supervisor.time, "sleep"), \
             patch.object(supervisor.time, "monotonic", side_effect=[0, 0.1, 0.2]):
            code, payload = supervisor.start("kr_mock", "KR")

        self.assertEqual(code, 0)
        self.assertTrue(payload["started"])
        self.assertEqual(popen.call_args.kwargs["env"]["AUTO_TRADING_ENABLED"], "true")


if __name__ == "__main__":
    unittest.main()
