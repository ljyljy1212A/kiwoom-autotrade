import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from src.core.process_lock import ProcessLock


@unittest.skipUnless(os.name == "posix", "POSIX lock semantics are verified on Linux")
class ProcessLockTests(unittest.TestCase):
    def test_lock_releases_when_owner_process_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            script = textwrap.dedent(
                """
                import sys
                import time
                from pathlib import Path
                from src.core.process_lock import ProcessLock

                lock = ProcessLock("kr_mock", Path(sys.argv[1]))
                lock.acquire()
                print("ready", flush=True)
                time.sleep(30)
                """
            )
            with subprocess.Popen(
                [sys.executable, "-c", script, str(base_dir)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ) as proc:
                try:
                    self.assertEqual(proc.stdout.readline().strip(), "ready")
                    probe = ProcessLock("kr_mock", base_dir)
                    self.assertTrue(probe.is_alive())
                    proc.terminate()
                    proc.wait(timeout=10)
                    self.assertFalse(probe.is_alive())
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
