import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
import uuid
from pathlib import Path
from src.core.process_lock import ProcessLock


@unittest.skipUnless(os.name == "nt", "Windows-only worker supervisor kill/stop semantics")
class SupervisorKillTests(unittest.TestCase):
    def _assert_worker_command_stops_mock_lock_holder_and_releases_lock(self, action: str):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            account_id = f"{action}_{uuid.uuid4().hex}"
            script = textwrap.dedent(
                """
                import os
                import json
                import sys
                import time
                from pathlib import Path
                from src.core.process_lock import ProcessLock

                lock = ProcessLock(sys.argv[1], sys.argv[2])
                lock.acquire()
                Path(sys.argv[3]).write_text(
                    json.dumps({"state": "submitted-awaiting-confirmation", "order_id": "mock-order-1"}),
                    encoding="utf-8",
                )
                print(os.getpid(), flush=True)
                time.sleep(60)
                """
            )
            in_flight_path = data_dir / "mock-in-flight-order.json"
            child = subprocess.Popen(
                [sys.executable, "-c", script, account_id, str(data_dir), str(in_flight_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                pid = int(child.stdout.readline().strip())
                self.assertEqual(
                    json.loads(in_flight_path.read_text(encoding="utf-8"))["state"],
                    "submitted-awaiting-confirmation",
                )
                (data_dir / f"worker_{account_id}.pid").write_text(
                    json.dumps({"pid": pid, "account": account_id}), encoding="utf-8"
                )
                (data_dir / f"worker_{account_id}.status.json").write_text(
                    json.dumps({"pid": pid, "account": account_id, "instanceId": "test-instance", "state": "RUNNING"}),
                    encoding="utf-8",
                )
                self.assertTrue(ProcessLock(account_id, data_dir).is_alive())
                env = os.environ.copy()
                env["KIWOOM_DATA_DIR"] = str(data_dir)
                env["KIWOOM_LOG_DIR"] = str(data_dir / "logs")
                command = subprocess.run(
                    [sys.executable, "-m", "src.worker_supervisor", action, "--account", account_id, "--market", "KR"],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                code = command.returncode
                payload = json.loads(command.stdout)
                print(f"{action}_command_returncode={code} stdout={command.stdout.strip()}")

                if action == "kill":
                    self.assertEqual(code, 0, payload)
                    self.assertTrue(payload["stopped"])
                else:
                    self.assertNotEqual(code, 0, payload)
                    self.assertEqual(payload["account"], account_id)
                    self.assertEqual(payload["pid"], pid)
                    self.assertIsInstance(payload["running"], bool)
                    self.assertEqual(payload["instanceId"], "test-instance")
                    self.assertIn("state", payload)
                    self.assertEqual(payload["mode"], "forced")
                    self.assertFalse(payload["stopped"])
                if action == "kill":
                    self.assertFalse(child.poll() is None)
                    self.assertFalse(ProcessLock(account_id, data_dir).is_alive())
                else:
                    self.assertTrue(child.poll() is None)
                    self.assertTrue(ProcessLock(account_id, data_dir).is_alive())
                self.assertEqual(
                    json.loads(in_flight_path.read_text(encoding="utf-8"))["state"],
                    "submitted-awaiting-confirmation",
                )
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=10)

    def test_kill_stops_mock_lock_holder_and_releases_lock(self):
        self._assert_worker_command_stops_mock_lock_holder_and_releases_lock("kill")

    def test_stop_stops_mock_lock_holder_and_releases_lock(self):
        self._assert_worker_command_stops_mock_lock_holder_and_releases_lock("stop")


if __name__ == "__main__":
    unittest.main()
