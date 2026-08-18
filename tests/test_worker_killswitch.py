import importlib
import subprocess
import sys
import tempfile
import textwrap
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from src.core.process_lock import ProcessLock


class SupervisorKillTests(unittest.TestCase):
    def test_kill_stops_mock_lock_holder_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            account_id = f"kill_{uuid.uuid4().hex}"
            script = textwrap.dedent(
                """
                import os
                import sys
                import time
                from src.core.process_lock import ProcessLock

                lock = ProcessLock(sys.argv[1], sys.argv[2])
                lock.acquire()
                print(os.getpid(), flush=True)
                time.sleep(60)
                """
            )
            child = subprocess.Popen(
                [sys.executable, "-c", script, account_id, str(data_dir)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                pid = int(child.stdout.readline().strip())
                supervisor = importlib.import_module("src.worker_supervisor")
                with patch.object(supervisor, "DATA_DIR", data_dir):
                    supervisor._write_atomic(supervisor._pid_path(account_id), {"pid": pid, "account": account_id})
                    supervisor._write_atomic(
                        supervisor._status_path(account_id),
                        {"pid": pid, "account": account_id, "instanceId": "test-instance", "state": "RUNNING"},
                    )
                    code, payload = supervisor.kill(account_id)

                self.assertEqual(code, 0, payload)
                self.assertTrue(payload["stopped"])
                self.assertFalse(child.poll() is None)
                self.assertFalse(ProcessLock(account_id, data_dir).is_alive())
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
