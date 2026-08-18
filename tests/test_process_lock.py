import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
import uuid
from pathlib import Path

from src.core.process_lock import ProcessLock


@unittest.skipUnless(os.name == "posix", "POSIX lock semantics are verified on Linux")
class ProcessLockTests(unittest.TestCase):
    def test_owned_by_current_process_tracks_local_acquisition(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = ProcessLock("owned_test", Path(tmp))
            self.assertFalse(lock.owned_by_current_process())
            lock.acquire()
            try:
                self.assertTrue(lock.owned_by_current_process())
            finally:
                lock.release()
            self.assertFalse(lock.owned_by_current_process())

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


@unittest.skipUnless(os.name == "nt", "Windows named mutex semantics are verified on Windows")
class WindowsProcessLockTests(unittest.TestCase):
    def test_second_process_cannot_acquire_real_named_mutex(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            account = f"mutex_test_{uuid.uuid4().hex}"
            script = textwrap.dedent(
                """
                import sys
                import time
                from pathlib import Path
                from src.core.process_lock import ProcessLock

                lock = ProcessLock(sys.argv[1], Path(sys.argv[2]))
                lock.acquire()
                print("ready", flush=True)
                time.sleep(30)
                """
            )
            proc = subprocess.Popen(
                [sys.executable, "-c", script, account, str(base_dir)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(proc.stdout.readline().strip(), "ready")
                contender = ProcessLock(account, base_dir)
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    contender.acquire()
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
