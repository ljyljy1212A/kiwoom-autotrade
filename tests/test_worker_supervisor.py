import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from src import worker_supervisor as supervisor
from src.core import account_catalog


class WorkerSupervisorStopTests(unittest.TestCase):
    def _write_intentional_stop_marker(self, base_dir, account="kr_mock"):
        path = base_dir / f"intentional_stop_{account}.json"
        path.write_text(
            '{"account":"%s","instanceId":"instance","requestedAt":"2026-01-01T00:00:00+00:00","expiresAt":"2026-01-01T00:15:00+00:00"}' % account,
            encoding="utf-8",
        )
        return path

    def test_stop_writes_well_formed_intentional_stop_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            running = {"account": "kr_mock", "pid": 123, "running": True,
                       "instanceId": "instance", "state": "RUNNING", "market": "KR"}
            stopped = {**running, "running": False, "state": "STOPPED"}
            with patch.object(supervisor, "DATA_DIR", base_dir), \
                 patch.object(supervisor, "status", side_effect=[running, stopped]), \
                 patch.object(supervisor, "_owned_identity", return_value=(123, "instance")), \
                 patch.object(supervisor, "_wait_for_stopped", return_value=True), \
                 patch.object(supervisor, "_remove_pid_after_confirmed_exit"), \
                 patch.object(supervisor, "_record_stopped_status"):
                code, payload = supervisor.stop("kr_mock")

            self.assertEqual(code, 0)
            self.assertEqual(payload["mode"], "graceful")
            marker = json.loads(
                (base_dir / "intentional_stop_kr_mock.json").read_text(encoding="utf-8")
            )
            self.assertEqual(marker["account"], "kr_mock")
            self.assertEqual(marker["instanceId"], "instance")
            requested_at = datetime.fromisoformat(marker["requestedAt"])
            expires_at = datetime.fromisoformat(marker["expiresAt"])
            self.assertEqual(
                expires_at - requested_at,
                timedelta(seconds=supervisor.SUPPRESS_RELAUNCH_SECONDS),
            )

    def test_start_already_running_early_clears_intentional_stop_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            marker = self._write_intentional_stop_marker(base_dir)
            running = {"account": "kr_mock", "pid": 123, "running": True}
            with patch.object(supervisor, "DATA_DIR", base_dir), \
                 patch.object(supervisor, "status", return_value=running):
                code, payload = supervisor.start("kr_mock", "KR")

            self.assertEqual(code, 3)
            self.assertEqual(payload["reason"], "already-running")
            self.assertFalse(marker.exists())

    def test_start_new_child_confirmation_clears_intentional_stop_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            marker = self._write_intentional_stop_marker(base_dir)
            child = MagicMock(pid=999, returncode=0)
            child.poll.return_value = None
            running = {"account": "kr_mock", "pid": 999, "running": True}
            with patch.object(supervisor, "DATA_DIR", base_dir), \
                 patch.object(supervisor, "status", side_effect=[{"running": False}, running]), \
                 patch.object(supervisor, "read_auto_trading_enabled", return_value=False), \
                 patch.object(supervisor.subprocess, "Popen", return_value=child), \
                 patch.object(supervisor.time, "sleep"), \
                 patch.object(supervisor.time, "monotonic", side_effect=[0, 0.1]):
                code, payload = supervisor.start("kr_mock", "KR")

            self.assertEqual(code, 0)
            self.assertTrue(payload["started"])
            self.assertFalse(marker.exists())

    def test_start_final_running_confirmation_clears_intentional_stop_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            marker = self._write_intentional_stop_marker(base_dir)
            child = MagicMock(pid=999, returncode=0)
            child.poll.return_value = None
            running = {"account": "kr_mock", "pid": 888, "running": True}
            with patch.object(supervisor, "DATA_DIR", base_dir), \
                 patch.object(supervisor, "status", side_effect=[{"running": False}, running]), \
                 patch.object(supervisor, "read_auto_trading_enabled", return_value=False), \
                 patch.object(supervisor.subprocess, "Popen", return_value=child), \
                 patch.object(supervisor.time, "sleep"), \
                 patch.object(supervisor.time, "monotonic", side_effect=[0, 31]):
                code, payload = supervisor.start("kr_mock", "KR")

            self.assertEqual(code, 3)
            self.assertEqual(payload["reason"], "already-running")
            self.assertFalse(marker.exists())

    @unittest.skipUnless(os.name == "nt", "Windows abandoned-mutex semantics are verified on Windows")
    def test_wait_for_stopped_accepts_abandoned_mutex_after_worker_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            account = f"crash_{uuid.uuid4().hex}"
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
                pid = proc.pid
                self.assertTrue(supervisor.ProcessLock(account, base_dir).is_alive())
                proc.kill()
                proc.wait(timeout=10)
                with patch.object(supervisor, "DATA_DIR", base_dir):
                    self.assertTrue(supervisor._wait_for_stopped(account, pid, 1))
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)

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

    @unittest.skipUnless(os.name == "nt", "Windows named mutex semantics are verified on Windows")
    def test_concurrent_starts_report_one_started_and_one_lock_conflict(self):
        import threading

        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            account = f"concurrent_start_{uuid.uuid4().hex}"
            children = []
            results = []
            errors = []
            start_gate = threading.Barrier(3)
            launch_gate = threading.Barrier(2)
            real_popen = subprocess.Popen
            worker_script = textwrap.dedent(
                """
                import json
                import os
                import sys
                import time
                from pathlib import Path
                from src.core.process_lock import ProcessLock, ProcessLockError

                account = sys.argv[1]
                base_dir = Path(sys.argv[2])
                lock = ProcessLock(account, base_dir)
                status_path = base_dir / f"worker_{account}.status.json"
                try:
                    lock.acquire()
                except ProcessLockError:
                    deadline = time.monotonic() + 2
                    while not status_path.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    raise SystemExit(3)

                status_path.write_text(
                    json.dumps(
                        {
                            "account": account,
                            "pid": os.getpid(),
                            "instanceId": "concurrent-test",
                            "state": "RUNNING",
                            "market": "KR",
                        }
                    ),
                    encoding="utf-8",
                )
                try:
                    time.sleep(30)
                finally:
                    lock.release()
                """
            )

            def spawn_mutex_owner(_command, **_kwargs):
                launch_gate.wait(timeout=5)
                child = real_popen(
                    [sys.executable, "-c", worker_script, account, str(base_dir)],
                    cwd=supervisor.ROOT,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                children.append(child)
                return child

            def call_start():
                try:
                    start_gate.wait(timeout=5)
                    results.append(supervisor.start(account, "KR"))
                except Exception as exc:
                    errors.append(exc)

            with patch.object(supervisor, "DATA_DIR", base_dir), \
                 patch.object(supervisor, "read_auto_trading_enabled", return_value=False), \
                 patch.object(supervisor.subprocess, "Popen", side_effect=spawn_mutex_owner):
                threads = [threading.Thread(target=call_start) for _ in range(2)]
                for thread in threads:
                    thread.start()
                start_gate.wait(timeout=5)
                for thread in threads:
                    thread.join(timeout=15)

            try:
                self.assertEqual(errors, [])
                self.assertEqual(len(results), 2)

                started = [(code, payload) for code, payload in results if payload.get("started")]
                rejected = [(code, payload) for code, payload in results if not payload.get("started")]

                self.assertEqual(len(started), 1)
                self.assertEqual(started[0][0], 0)
                self.assertEqual(len(rejected), 1)
                self.assertEqual(rejected[0][0], 3)
                self.assertIn(
                    rejected[0][1].get("reason"),
                    ("already-running", "worker-refused-or-exited"),
                )
                if rejected[0][1].get("reason") == "worker-refused-or-exited":
                    self.assertEqual(rejected[0][1].get("failureClass"), "lock-conflict")
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=10)

    def test_stop_blocks_synthetic_real_account_without_allow_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir(parents=True)
            (root / "config" / "accounts.yaml").write_text(
                "accounts:\n"
                "  - id: synthetic_catalog_real\n"
                "    display_name: Synthetic Real Test Account\n"
                "    market: KR\n"
                "    mode: real\n",
                encoding="utf-8",
            )
            with patch.object(account_catalog, "PROJECT_ROOT", root), \
                 patch.dict(os.environ, {}, clear=False), \
                 patch.object(supervisor, "status") as status_mock, \
                 patch.object(supervisor.subprocess, "run") as taskkill_mock:
                os.environ.pop("ALLOW_LIVE_SUPERVISOR", None)
                code, payload = supervisor.stop("synthetic_catalog_real")
        self.assertEqual(code, 7)
        self.assertEqual(payload["mode"], "blocked")
        status_mock.assert_not_called()
        taskkill_mock.assert_not_called()

    def test_kill_blocks_synthetic_real_account_without_allow_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir(parents=True)
            (root / "config" / "accounts.yaml").write_text(
                "accounts:\n"
                "  - id: synthetic_catalog_real\n"
                "    display_name: Synthetic Real Test Account\n"
                "    market: KR\n"
                "    mode: real\n",
                encoding="utf-8",
            )
            with patch.object(account_catalog, "PROJECT_ROOT", root), \
                 patch.dict(os.environ, {}, clear=False), \
                 patch.object(supervisor, "status") as status_mock, \
                 patch.object(supervisor.subprocess, "run") as taskkill_mock:
                os.environ.pop("ALLOW_LIVE_SUPERVISOR", None)
                code, payload = supervisor.kill("synthetic_catalog_real")
        self.assertEqual(code, 7)
        self.assertEqual(payload["mode"], "blocked")
        status_mock.assert_not_called()
        taskkill_mock.assert_not_called()

    def test_stop_allows_synthetic_real_account_with_allow_env_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir(parents=True)
            (root / "config" / "accounts.yaml").write_text(
                "accounts:\n"
                "  - id: synthetic_catalog_real\n"
                "    display_name: Synthetic Real Test Account\n"
                "    market: KR\n"
                "    mode: real\n",
                encoding="utf-8",
            )
            with patch.object(account_catalog, "PROJECT_ROOT", root), \
                 patch.dict(os.environ, {"ALLOW_LIVE_SUPERVISOR": "true"}, clear=False), \
                 patch.object(supervisor, "status", return_value={"running": False}):
                code, payload = supervisor.stop("synthetic_catalog_real")
        self.assertEqual(code, 0)
        self.assertEqual(payload["mode"], "already_stopped")

    def test_stop_does_not_block_synthetic_mock_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir(parents=True)
            (root / "config" / "accounts.yaml").write_text(
                "accounts:\n"
                "  - id: synthetic_catalog_mock\n"
                "    display_name: Synthetic Mock Test Account\n"
                "    market: KR\n"
                "    mode: mock\n",
                encoding="utf-8",
            )
            with patch.object(account_catalog, "PROJECT_ROOT", root), \
                 patch.dict(os.environ, {}, clear=False), \
                 patch.object(supervisor, "status", return_value={"running": False}):
                os.environ.pop("ALLOW_LIVE_SUPERVISOR", None)
                code, payload = supervisor.stop("synthetic_catalog_mock")
        self.assertEqual(code, 0)
        self.assertEqual(payload["mode"], "already_stopped")


if __name__ == "__main__":
    unittest.main()
