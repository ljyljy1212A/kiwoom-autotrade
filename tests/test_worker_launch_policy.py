"""Persistent US mock launch-policy regression tests with isolated state."""
import ctypes
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import types
import unittest
import uuid
from unittest.mock import Mock, patch

from src.core import worker_launch_policy as policy
from src.core import worker_launch_routes as routes
from src import worker_supervisor as supervisor
from tools import worker_watchdog as watchdog

SOURCE = Path(__file__).resolve().parents[1]


def forbidden(*args, **kwargs):
    raise AssertionError("External process/network operation forbidden in isolated tests")


class FunctionProxy:
    def __init__(self, function, name=None):
        object.__setattr__(self, "function", function)
        object.__setattr__(self, "name", name)

    def __setattr__(self, key, value):
        setattr(self.function, key, value)

    def __call__(self, *args):
        if self.name is not None:
            assert args[2] == "Global\\KiwoomAutotradeLaunchPolicy_us_mock"
            args = (*args[:2], self.name)
        return self.function(*args)

class RelaunchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("KIWOOM_WORKER_")}
        self.enterContext(patch.dict(os.environ, environment, clear=True))
        self.enterContext(patch.object(supervisor, "ROOT", SOURCE))
        self.enterContext(patch.object(watchdog, "WATCHDOG_LOG", Mock()))
        self.enterContext(patch.object(subprocess, "run", side_effect=forbidden))
        self.enterContext(patch.object(socket.socket, "connect", side_effect=forbidden))
        if os.name == "nt":
            native = ctypes.WinDLL("kernel32", use_last_error=True)
            name = "Local\\CodexIsolatedLaunchPolicy_" + uuid.uuid4().hex
            kernel = types.SimpleNamespace(**{
                key: FunctionProxy(getattr(native, key), name if key == "CreateMutexW" else None)
                for key in ("CreateMutexW", "WaitForSingleObject", "ReleaseMutex", "CloseHandle")
            })
            self.enterContext(patch.object(policy.ctypes, "WinDLL", return_value=kernel))
        self.enterContext(patch.object(supervisor, "DATA_DIR", self.data))
        self.enterContext(patch.object(watchdog, "DATA_DIR", self.data))
        self.enterContext(patch.object(supervisor, "is_real_account", return_value=False))
        self.status = self.enterContext(patch.object(supervisor, "status", side_effect=forbidden))
        self.enterContext(patch.object(supervisor, "read_auto_trading_enabled", return_value=False))
        self.enterContext(patch.object(watchdog, "_send_notification", side_effect=forbidden))
        self.enterContext(patch.object(watchdog, "enumerate_worker_processes", side_effect=forbidden))
        self.spawn = self.enterContext(patch.object(supervisor.subprocess, "Popen", side_effect=forbidden))

    def change(self, state, generation=None):
        return policy.change_maintenance("us_mock", "US", self.data, state=state,
                                         reason="isolated validation", expected_generation=generation)

    def resumed(self):
        paused = self.change("PAUSED")
        return self.change("RESUMED", paused["generation"])

    def prepare_spawn(self):
        self.status.side_effect = [{"running": False}, {"running": True, "pid": 999}]
        self.enterContext(patch.object(supervisor, "_is_started_child", return_value=True))
        self.enterContext(patch.object(supervisor, "_clear_intentional_stop"))
        self.enterContext(patch.object(supervisor, "resolve_worker_root", return_value=SOURCE))
        self.enterContext(patch.dict(os.environ, {
            "KIWOOM_WORKER_ROOT_US_MOCK": str(SOURCE),
            "KIWOOM_WORKER_REVISION_US_MOCK": "a" * 40,
        }))
        self.spawn.side_effect = None
        self.spawn.return_value = types.SimpleNamespace(pid=999, poll=lambda: None)

    def test_missing_record_blocks_before_status_and_spawn(self):
        code, result = supervisor.start("us_mock", "US")
        self.assertEqual(code, 10)
        self.assertFalse(result["started"])
        self.status.assert_not_called()
        self.spawn.assert_not_called()
        self.assertEqual(list(self.data.iterdir()), [])

    def test_initial_resume_refused(self):
        with self.assertRaisesRegex(policy.WorkerLaunchPolicyError, "initialization-requires-pause"):
            self.change("RESUMED")
        self.assertFalse(policy.maintenance_path(self.data).exists())

    @unittest.skipUnless(os.name == "nt", "Windows kernel API contract")
    def test_windows_unconfirmed_wait_results_are_rejected(self):
        for wait_result in (0x80, 258, 0xFFFFFFFF):
            kernel = types.SimpleNamespace(CreateMutexW=Mock(return_value=123),
                WaitForSingleObject=Mock(return_value=wait_result),
                ReleaseMutex=Mock(return_value=True), CloseHandle=Mock(return_value=True))
            with patch.object(policy.ctypes, "WinDLL", return_value=kernel), \
                 self.assertRaisesRegex(policy.WorkerLaunchPolicyError, "lock-not-confirmed"):
                with policy._windows_lock():
                    self.fail("Unconfirmed lock must not admit the launch section")
            kernel.CloseHandle.assert_called_once_with(123)
            self.assertEqual(kernel.ReleaseMutex.call_count, int(wait_result == 0x80))

    @unittest.skipUnless(os.name == "nt", "Windows kernel API contract")
    def test_windows_release_failure_is_unresolved(self):
        kernel = types.SimpleNamespace(CreateMutexW=Mock(return_value=123),
            WaitForSingleObject=Mock(return_value=0), ReleaseMutex=Mock(return_value=False),
            CloseHandle=Mock(return_value=True))
        with patch.object(policy.ctypes, "WinDLL", return_value=kernel), \
             self.assertRaisesRegex(policy.WorkerLaunchPolicyError, "release-unresolved"):
            with policy._windows_lock():
                pass
        kernel.CloseHandle.assert_called_once_with(123)

    def test_pause_has_no_expiry_and_blocks_after_old_timestamp(self):
        self.change("PAUSED")
        path = policy.maintenance_path(self.data)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["updatedAtUtc"] = "2000-01-01T00:00:00+00:00"
        path.write_text(json.dumps(record), encoding="utf-8")
        self.assertNotIn("expiresAt", record)
        for _ in range(2):
            self.assertEqual(supervisor.start("us_mock", "US")[0], 10)
        self.spawn.assert_not_called()
        self.status.assert_not_called()

    def test_generation_required_and_stale_generation_preserves_bytes(self):
        initial = self.change("PAUSED")
        before = policy.maintenance_path(self.data).read_bytes()
        for generation in (None, "0" * 32):
            with self.assertRaisesRegex(policy.WorkerLaunchPolicyError, "generation-mismatch"):
                self.change("RESUMED", generation)
            self.assertEqual(policy.maintenance_path(self.data).read_bytes(), before)
        resumed = self.change("RESUMED", initial["generation"])
        self.assertEqual(resumed["previousGeneration"], initial["generation"])
        self.assertNotEqual(resumed["generation"], initial["generation"])

    def test_resume_never_spawns(self):
        paused = self.change("PAUSED")
        code, result = supervisor.maintenance("us_mock", "US", paused=False,
            reason="isolated validation", expected_generation=paused["generation"])
        self.assertEqual(code, 0)
        self.assertEqual(result["workerAction"], "none")
        self.spawn.assert_not_called()
        self.status.assert_not_called()

    def test_invalid_records_fail_closed(self):
        record = self.resumed()
        record.pop("previousGeneration")
        path = policy.maintenance_path(self.data)
        variants = ["{", "[]", '{"state":"RESUMED","state":"PAUSED"}', "x" * 16385]
        for key, value in (("schemaVersion", True), ("account", "kr_mock"),
            ("market", "KR"), ("generation", "invalid"), ("state", "UNKNOWN"),
            ("updatedAtUtc", "2026-10-06T00:00:00"), ("reason", "")):
            variants.append(json.dumps({**record, key: value}))
        for raw in variants:
            with self.subTest(raw=raw[:50]):
                path.write_text(raw, encoding="utf-8")
                self.assertEqual(supervisor.start("us_mock", "US")[0], 10)
        self.spawn.assert_not_called()
        self.status.assert_not_called()

    def test_wrong_scope_cannot_change_record(self):
        for account, market in (("kr_mock", "KR"), ("us_mock", "KR"), ("us_real", "US")):
            code, result = supervisor.maintenance(account, market, paused=True, reason="test")
            self.assertEqual(code, 10)
            self.assertFalse(result["changed"])
        self.assertEqual(list(self.data.iterdir()), [])

    def test_paused_watchdog_preserves_state_and_skips_status(self):
        self.change("PAUSED")
        with patch.object(watchdog, "_load_state", side_effect=forbidden), \
             patch.object(watchdog, "_save_state", side_effect=forbidden):
            watchdog.check_and_restart("us_mock", "US")
        self.status.assert_not_called()
        self.spawn.assert_not_called()

    def test_resumed_watchdog_invalid_route_skips_status(self):
        self.resumed()
        with patch.object(watchdog, "_load_state", side_effect=forbidden):
            watchdog.check_and_restart("us_mock", "US")
        self.status.assert_not_called()

    def test_us_missing_partial_invalid_and_wrong_market_pins(self):
        self.resumed()
        cases = ({}, {"KIWOOM_WORKER_ROOT_US_MOCK": str(SOURCE)},
            {"KIWOOM_WORKER_REVISION_US_MOCK": "a" * 40},
            {"KIWOOM_WORKER_ROOT_US_MOCK": str(SOURCE), "KIWOOM_WORKER_REVISION_US_MOCK": "invalid"},
            {"KIWOOM_WORKER_ROOT_US_MOCK": "relative", "KIWOOM_WORKER_REVISION_US_MOCK": "a" * 40})
        for values in cases:
            with patch.dict(os.environ, values), self.subTest(values=values):
                self.assertEqual(supervisor.start("us_mock", "US")[0], 9)
        self.assertEqual(supervisor.start("us_mock", "KR")[0], 9)
        self.spawn.assert_not_called()
        self.status.assert_not_called()

    def test_git_evidence_mismatch_dirty_missing_support(self):
        root = self.data / "synthetic-source"
        (root / "src" / "core").mkdir(parents=True)
        for path in ("src/main.py", "src/worker_supervisor.py", "src/core/worker_environment.py"):
            (root / path).write_text("# synthetic fixture\n", encoding="utf-8")
        env = {"KIWOOM_WORKER_ROOT_US_MOCK": str(root), "KIWOOM_WORKER_REVISION_US_MOCK": "a" * 40}
        for replies, message in (([str(root), "b" * 40], "revision mismatch"),
            ([str(root), "a" * 40, " M src/main.py"], "not clean"),
            ([str(root), "a" * 40, "?? src/new.py"], "not clean")):
            with patch.object(routes, "_git_output", side_effect=replies), \
                 self.assertRaisesRegex(routes.WorkerLaunchRouteError, message):
                routes.resolve_worker_root("us_mock", "US", SOURCE, env)
        (root / "src/core/worker_environment.py").unlink()
        with patch.object(routes, "_git_output", side_effect=[str(root), "a" * 40, ""]), \
             self.assertRaisesRegex(routes.WorkerLaunchRouteError, "lacks routed-worker"):
            routes.resolve_worker_root("us_mock", "US", SOURCE, env)

    def test_pause_between_initial_check_and_spawn_blocks_final_check(self):
        resumed = self.resumed()
        self.prepare_spawn()
        def route(*args, **kwargs):
            self.change("PAUSED", resumed["generation"])
            return SOURCE
        with patch.object(supervisor, "resolve_worker_root", side_effect=route):
            code, result = supervisor.start("us_mock", "US")
        self.assertEqual(code, 10)
        self.assertFalse(result["spawned"])
        self.spawn.assert_not_called()

    def test_source_change_at_final_check_blocks_spawn(self):
        self.resumed()
        self.prepare_spawn()
        with patch.object(supervisor, "resolve_worker_root", side_effect=[SOURCE, self.data]):
            self.assertEqual(supervisor.start("us_mock", "US")[0], 9)
        self.spawn.assert_not_called()

    def test_start_holds_native_test_lock_and_releases_before_ack(self):
        resumed = self.resumed()
        self.prepare_spawn()
        entered, release = threading.Event(), threading.Event()
        outcomes = []
        errors = []
        def spawn(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test spawn barrier timed out")
            return types.SimpleNamespace(pid=999, poll=lambda: None)
        def start():
            try:
                outcomes.append(supervisor.start("us_mock", "US"))
            except BaseException as exc:
                errors.append(exc)
        self.spawn.side_effect = spawn
        thread = threading.Thread(target=start)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            code, result = supervisor.maintenance("us_mock", "US", paused=True,
                reason="concurrent pause", expected_generation=resumed["generation"])
            self.assertEqual(code, 10)
            self.assertIsNone(result["changed"])
            self.assertEqual(policy.read_maintenance(self.data)["state"], "RESUMED")
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(outcomes[0][0], 0)
        paused = self.change("PAUSED", resumed["generation"])
        self.assertEqual(paused["state"], "PAUSED")

    def test_acknowledgement_occurs_after_lock_release(self):
        resumed = self.resumed()
        self.prepare_spawn()
        calls = []
        def status(account):
            calls.append(account)
            if len(calls) == 1:
                return {"running": False}
            # A different thread can acquire the same mutex during acknowledgement.
            outcomes = []
            def pause():
                outcomes.append(self.change("PAUSED", resumed["generation"])["state"])
            thread = threading.Thread(target=pause)
            thread.start()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcomes, ["PAUSED"])
            return {"running": True, "pid": 999}
        self.status.side_effect = status
        self.assertEqual(supervisor.start("us_mock", "US")[0], 0)

    def test_kr_default_route_and_already_running_behavior_preserved(self):
        self.assertEqual(routes.resolve_worker_root("kr_mock", "KR", SOURCE, {}), SOURCE)
        self.status.side_effect = None
        self.status.return_value = {"running": True, "pid": 888}
        with patch.object(supervisor, "require_launch_resumed", side_effect=forbidden), \
             patch.object(supervisor, "launch_policy_lock", side_effect=forbidden), \
             patch.object(supervisor, "_clear_intentional_stop") as clear:
            code, result = supervisor.start("kr_mock", "KR")
        self.assertEqual(code, 3)
        self.assertEqual(result["reason"], "already-running")
        clear.assert_called_once_with("kr_mock")
        self.spawn.assert_not_called()

    def test_kr_spawn_does_not_require_us_maintenance(self):
        self.prepare_spawn()
        with patch.object(supervisor, "require_launch_resumed", side_effect=forbidden), \
             patch.object(supervisor, "launch_policy_lock", side_effect=forbidden):
            code, result = supervisor.start("kr_mock", "KR")
        self.assertEqual(code, 0)
        self.assertTrue(result["started"])
        self.spawn.assert_called_once()

    def test_kr_watchdog_does_not_consult_us_policy(self):
        self.status.side_effect = None
        self.status.return_value = {"running": True, "pid": 888}
        with patch.object(watchdog, "require_launch_resumed", side_effect=forbidden), \
             patch.object(watchdog, "resolve_worker_root", side_effect=forbidden), \
             patch.object(watchdog, "_load_state", return_value={}), \
             patch.object(watchdog, "check_duplicate_live_process", return_value=False), \
             patch.object(watchdog, "_classify", return_value=("healthy", "synthetic")), \
             patch.object(watchdog, "_save_state") as save:
            watchdog.check_and_restart("kr_mock", "KR")
        save.assert_called_once()
