from __future__ import annotations

import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from src.core.control_state import (
    read_control_state,
    write_control_state,
    write_fixed_port_degraded_event,
    write_pause_clear_event,
)
from src.core.engine import AccountEngine
from src.core.orphan_cleanup import account_control_state_lock


class RuntimeControlRefreshTest(unittest.TestCase):
    def test_account_engine_refreshes_auto_trading_from_control_file(self):
        engine = object.__new__(AccountEngine)
        engine.ctx = SimpleNamespace(account_id="kr_mock")
        engine.ctx.logger = Mock()
        engine.data_dir = Path(tempfile.mkdtemp())
        engine._auto_trading_enabled = False

        write_control_state("kr_mock", auto_trading_enabled=True, data_dir=engine.data_dir)
        engine._refresh_runtime_control()

        self.assertTrue(engine._auto_trading_enabled)
        engine.ctx.logger.info.assert_called_once_with(
            "auto_trading_enabled changed: false -> true (source: control file)"
        )

        engine._refresh_runtime_control()
        engine.ctx.logger.info.assert_called_once()

    def test_control_switch_preserves_existing_account_events(self):
        data_dir = Path(tempfile.mkdtemp())
        fixed_port_event = write_fixed_port_degraded_event(
            "kr_mock", "entered", "test", datetime.now(timezone.utc), data_dir=data_dir,
        )
        pause_clear_event = write_pause_clear_event(
            "kr_mock", "broker_quantity_unattributed", data_dir=data_dir,
        )

        write_control_state("kr_mock", auto_trading_enabled=False, data_dir=data_dir)

        state = read_control_state("kr_mock", data_dir=data_dir)
        self.assertFalse(state["auto_trading_enabled"])
        self.assertEqual(state["fixed_port_event"], fixed_port_event)
        self.assertEqual(state["pause_clear_event"], pause_clear_event)

    def test_control_state_lock_times_out_for_contending_thread(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            locked = threading.Event()
            release = threading.Event()
            outcomes = queue.Queue()

            def hold_lock():
                with account_control_state_lock(data_dir, "kr_mock"):
                    locked.set()
                    release.wait(timeout=5)

            def contend_for_lock():
                try:
                    with account_control_state_lock(data_dir, "kr_mock"):
                        outcomes.put(("acquired", None))
                except Exception as exc:
                    outcomes.put(("error", exc))

            holder = threading.Thread(target=hold_lock, daemon=True)
            holder.start()
            contender = None
            try:
                self.assertTrue(locked.wait(timeout=2), "holder did not acquire the control-state lock")
                started = time.monotonic()
                contender = threading.Thread(target=contend_for_lock, daemon=True)
                contender.start()
                contender.join(timeout=4)
                elapsed = time.monotonic() - started

                self.assertFalse(contender.is_alive(), "contending lock request did not return within 4 seconds")
                outcome, error = outcomes.get(timeout=1)
                self.assertEqual(outcome, "error")
                self.assertIsInstance(error, TimeoutError)
                self.assertIn("within 2 seconds", str(error))
                self.assertLess(elapsed, 4)
            finally:
                release.set()
                holder.join(timeout=1)
                if contender is not None:
                    contender.join(timeout=1)

            self.assertFalse(holder.is_alive(), "control-state lock holder did not exit")
            self.assertFalse(contender.is_alive(), "control-state lock contender did not exit")

    def test_control_state_lock_times_out_for_contending_process(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            ready_path = data_dir / "holder-ready"
            release_path = data_dir / "release-holder"
            worker_code = """\
import sys
import time
from pathlib import Path
from src.core.orphan_cleanup import account_control_state_lock

data_dir = Path(sys.argv[1])
ready_path = Path(sys.argv[2])
release_path = Path(sys.argv[3])
with account_control_state_lock(data_dir, "kr_mock"):
    ready_path.write_text("ready", encoding="ascii")
    deadline = time.monotonic() + 10
    while not release_path.exists():
        if time.monotonic() >= deadline:
            raise SystemExit(2)
        time.sleep(0.01)
"""
            worker = subprocess.Popen(
                [sys.executable, "-c", worker_code, str(data_dir), str(ready_path), str(release_path)],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            outcomes = queue.Queue()

            def contend_for_lock():
                try:
                    with account_control_state_lock(data_dir, "kr_mock"):
                        outcomes.put(("acquired", None))
                except Exception as exc:
                    outcomes.put(("error", exc))

            contender = None
            try:
                startup_deadline = time.monotonic() + 5
                while not ready_path.exists() and worker.poll() is None and time.monotonic() < startup_deadline:
                    time.sleep(0.01)
                self.assertTrue(ready_path.exists(), f"lock holder failed to start; exit={worker.poll()}")

                started = time.monotonic()
                contender = threading.Thread(target=contend_for_lock, daemon=True)
                contender.start()
                contender.join(timeout=4)
                elapsed = time.monotonic() - started

                self.assertFalse(contender.is_alive(), "cross-process lock request did not return within 4 seconds")
                outcome, error = outcomes.get(timeout=1)
                self.assertEqual(outcome, "error")
                self.assertIsInstance(error, TimeoutError)
                self.assertIn("within 2 seconds", str(error))
                self.assertLess(elapsed, 4)
            finally:
                release_path.write_text("release", encoding="ascii")
                try:
                    stdout, stderr = worker.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    stdout, stderr = worker.communicate(timeout=3)
                if contender is not None:
                    contender.join(timeout=1)

            self.assertEqual(worker.returncode, 0, f"lock holder failed: {stdout}\n{stderr}")
            self.assertFalse(contender.is_alive(), "cross-process lock contender did not exit")


if __name__ == "__main__":
    unittest.main()
