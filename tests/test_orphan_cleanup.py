"""Regression tests for broker-authoritative unattended orphan cleanup."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from multiprocessing import get_context
from pathlib import Path
from unittest.mock import patch

from src.core.orphan_cleanup import OrphanStateCleaner
from src.data.trade_ledger import PendingOrder, TradeLedgerStore


def _process_sweep(data_dir, account, generation, apply, ready, start):
    cleaner = OrphanStateCleaner(account, Path(data_dir), market="US")
    ready.put(True)
    if not start.wait(15):
        raise TimeoutError("cleaner process did not receive the start signal")
    if apply:
        cleaner._apply_intent = lambda _intent: (False, [], "hold pending intent")
    cleaner.sweep(
        {}, True, lambda _symbol: False,
        balance_generation=generation, fresh_balance=True,
        balance_fetch_started_at=datetime.now(timezone.utc).isoformat(),
        apply=apply,
    )


class OrphanCleanupTest(unittest.TestCase):
    account = "us_mock_test"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()
        self.cleaner = OrphanStateCleaner(self.account, self.data, market="US")

    def tearDown(self):
        self.tmp.cleanup()

    def _state(self, symbol="LEGACY"):
        (self.data / f"tranche_bases_{self.account}.json").write_text(json.dumps({symbol: 10.0}), encoding="utf-8")
        (self.data / f"symbol_lifecycles_{self.account}.json").write_text(
            json.dumps({symbol: {"status": "open", "started_at": "2026-08-01T00:00:00+00:00"}}), encoding="utf-8")
        (self.data / f"dashboard_control_{self.account}_{symbol}.json").write_text(
            json.dumps({"symbol": symbol, "auto_buy": True}), encoding="utf-8")
        (self.data / f"dashboard_control_{self.account}.json").write_text(
            json.dumps({"symbol": symbol, "auto_buy": True, "auto_sell": True}), encoding="utf-8")
        (self.data / f"dashboard_settings_{self.account}.json").write_text(
            json.dumps({"profiles": [{"config": {"symbol": symbol}}]}), encoding="utf-8")

    def _sweep(
        self, quantities=None, *, complete=True, generation="run:1",
        fresh=True, balance_fetch_started_at=None,
        unresolved=lambda _symbol: False, apply=True,
    ):
        if balance_fetch_started_at is None:
            balance_fetch_started_at = datetime.now(timezone.utc).isoformat()
        return self.cleaner.sweep(
            quantities or {},
            complete,
            unresolved,
            balance_generation=generation,
            fresh_balance=fresh,
            balance_fetch_started_at=balance_fetch_started_at,
            apply=apply,
        )

    def test_two_complete_zero_snapshots_clean_runtime_state_but_keep_ledger_history(self):
        self._state()
        ledger = TradeLedgerStore(str(self.data / f"trades_{self.account}.db"), self.account)
        try:
            order = PendingOrder("B1", "LEGACY", "BUY", 2, 10, "BUY", 2, {})
            ledger.add_pending(order)
            ledger.record_fill(order, 2, 10, "2026-08-01")
            first = self._sweep(generation="run:1", unresolved=ledger.has_unresolved_orders)
            self.assertEqual(first[0]["classification"], "orphan_candidate")
            second = self._sweep(generation="run:2", unresolved=ledger.has_unresolved_orders)
            self.assertEqual(second[0]["classification"], "cleaned")
            self.assertFalse((self.data / f"tranche_bases_{self.account}.json").read_text(encoding="utf-8").find("LEGACY") >= 0)
            self.assertEqual(ledger.ledger_rows("LEGACY")[0]["ord_no"], "B1")
        finally:
            ledger.close()

    def test_transient_zero_then_nonzero_never_cleans(self):
        self._state()
        self._sweep(generation="run:1")
        result = self._sweep({"LEGACY": 3}, generation="run:2")
        self.assertEqual(result[0]["classification"], "protected_nonzero_holding")
        self.assertIn("LEGACY", json.loads((self.data / f"tranche_bases_{self.account}.json").read_text()))

    def test_incomplete_balance_blocks_cleanup(self):
        self._state()
        result = self._sweep(complete=False)
        self.assertEqual(result[0]["classification"], "blocked_incomplete_balance")
        self.assertTrue((self.data / f"dashboard_control_{self.account}_LEGACY.json").exists())

    def test_unresolved_order_and_nonzero_iren_style_state_require_review(self):
        self._state("IREN")
        result = self._sweep({"IREN": 3}, unresolved=lambda symbol: symbol == "IREN")
        self.assertEqual(result[0]["classification"], "manual_review_required")
        self.assertTrue((self.data / f"dashboard_control_{self.account}_IREN.json").exists())

    def test_closed_lifecycle_history_is_not_reactivated_by_cleanup(self):
        self._state("HISTORY")
        self._sweep(generation="run:1")
        self._sweep(generation="run:2")
        lifecycle = json.loads((self.data / f"symbol_lifecycles_{self.account}.json").read_text())
        self.assertEqual(lifecycle["HISTORY"]["status"], "closed")
        self.assertEqual(lifecycle["HISTORY"]["reason"], "automatic_orphan_cleanup")

    def test_manual_lifecycle_basis_is_not_an_orphan_when_broker_nonzero(self):
        self._state("IREN")
        lifecycles = json.loads((self.data / f"symbol_lifecycles_{self.account}.json").read_text())
        lifecycles["IREN"].update({"manual_qty": 1.0, "manual_price": 45.085})
        (self.data / f"symbol_lifecycles_{self.account}.json").write_text(json.dumps(lifecycles), encoding="utf-8")
        result = self.cleaner.evaluate("IREN", 3, True, lambda _: True)
        self.assertEqual(result["classification"], "manual_review_required")
        self.assertEqual(json.loads((self.data / f"symbol_lifecycles_{self.account}.json").read_text())["IREN"]["manual_price"], 45.085)

    def test_unambiguous_aapl_legacy_key_migrates_and_survives_restart(self):
        self._state("PL")
        ledger = TradeLedgerStore(str(self.data / f"trades_{self.account}.db"), self.account)
        try:
            order = PendingOrder("AAPL-1", "AAPL", "BUY", 1, 10, "BUY", 1, {})
            ledger.add_pending(order)
            self.assertEqual(self.cleaner.migrate_legacy_keys({"AAPL"}), frozenset())
            lifecycle = json.loads((self.data / f"symbol_lifecycles_{self.account}.json").read_text())
            self.assertIn("AAPL", lifecycle)
            self.assertNotIn("PL", lifecycle)
            self.assertTrue((self.data / f"dashboard_control_{self.account}_AAPL.json").exists())
            self.assertFalse((self.data / f"dashboard_control_{self.account}_PL.json").exists())
            self.assertEqual(ledger.pending_orders("AAPL")[0].ord_no, "AAPL-1")
            restarted = OrphanStateCleaner(self.account, self.data, market="US")
            self.assertEqual(restarted.migrate_legacy_keys({"AAPL"}), frozenset())
            self.assertEqual(json.loads((self.data / f"symbol_lifecycles_{self.account}.json").read_text()), lifecycle)
            self.assertTrue((self.data / "audit" / f"symbol_key_migration_{self.account}.jsonl").exists())
        finally:
            ledger.close()

    def test_aapl_pl_collision_remains_manual_review_and_orphan_cleanup_skips_it(self):
        self._state("PL")
        manual_review = self.cleaner.migrate_legacy_keys({"AAPL", "PL"})
        self.assertEqual(manual_review, frozenset({"AAPL", "PL"}))
        lifecycle = json.loads((self.data / f"symbol_lifecycles_{self.account}.json").read_text())
        self.assertIn("PL", lifecycle)
        result = self._sweep()
        self.assertEqual(result[0]["classification"], "manual_review_symbol_key")
        self.assertEqual(json.loads((self.data / f"symbol_lifecycles_{self.account}.json").read_text()), lifecycle)

    def test_settings_only_profile_never_becomes_cleanup_candidate(self):
        settings = self.data / f"dashboard_settings_{self.account}.json"
        settings.write_text(
            json.dumps({"profiles": [{"config": {"symbol": "ONLY"}}]}), encoding="utf-8",
        )
        self.assertEqual(self._sweep(generation="run:1")[0]["classification"], "clean")
        self.assertEqual(self._sweep(generation="run:2")[0]["classification"], "clean")
        self.assertEqual(len(json.loads(settings.read_text())["profiles"]), 1)
        state = json.loads((self.data / f"orphan_cleanup_{self.account}.json").read_text())
        self.assertNotIn("ONLY", state["pendingCleanup"])

    def test_same_generation_counts_once_and_two_generations_persist_intent_first(self):
        self._state()
        first = self._sweep(generation="run:1", apply=False)[0]
        repeated = self._sweep(generation="run:1", apply=False)[0]
        self.assertEqual(first["zeroConfirmations"], 1)
        self.assertEqual(repeated["zeroConfirmations"], 1)
        targets = [
            self.data / f"tranche_bases_{self.account}.json",
            self.data / f"symbol_lifecycles_{self.account}.json",
            self.data / f"dashboard_control_{self.account}_LEGACY.json",
            self.data / f"dashboard_control_{self.account}.json",
            self.data / f"dashboard_settings_{self.account}.json",
        ]
        before = {path: path.read_bytes() for path in targets}

        def observe_persisted(intent):
            state = json.loads((self.data / f"orphan_cleanup_{self.account}.json").read_text())
            self.assertEqual(state["qualifyingGenerations"]["LEGACY"], ["run:1", "run:2"])
            self.assertEqual(state["pendingCleanup"]["LEGACY"], intent)
            self.assertEqual(before, {path: path.read_bytes() for path in targets})
            return False, [], "stop after intent"

        with patch.object(self.cleaner, "_apply_intent", side_effect=observe_persisted):
            second = self._sweep(generation="run:2")[0]
        self.assertEqual(second["zeroConfirmations"], 2)
        self.assertEqual(second["classification"], "manual_review_cleanup_conflict")

    def test_intent_write_failure_leaves_all_targets_unchanged(self):
        self._state()
        self._sweep(generation="run:1")
        targets = [
            self.data / f"tranche_bases_{self.account}.json",
            self.data / f"symbol_lifecycles_{self.account}.json",
            self.data / f"dashboard_control_{self.account}_LEGACY.json",
            self.data / f"dashboard_control_{self.account}.json",
            self.data / f"dashboard_settings_{self.account}.json",
        ]
        before = {path: path.read_bytes() for path in targets}
        state_before = self.cleaner.state_path.read_bytes()
        original = self.cleaner._write_state

        def fail_pending(state):
            if state.get("pendingCleanup"):
                raise OSError("intent write failed")
            return original(state)

        with patch.object(self.cleaner, "_write_state", side_effect=fail_pending):
            with self.assertRaisesRegex(OSError, "intent write failed"):
                self._sweep(generation="run:2")
        self.assertEqual(before, {path: path.read_bytes() for path in targets})
        self.assertEqual(self.cleaner.state_path.read_bytes(), state_before)

    def test_replay_uses_fixed_archive_and_changed_profile_is_preserved(self):
        self._state()
        self._sweep(generation="run:1")
        self._sweep(generation="run:2", apply=False)
        state_path = self.data / f"orphan_cleanup_{self.account}.json"
        state = json.loads(state_path.read_text())
        intent = self.cleaner._build_intent("LEGACY", ["run:1", "run:2"])
        state["pendingCleanup"]["LEGACY"] = intent
        self.cleaner._write_state(state)
        settings = self.data / f"dashboard_settings_{self.account}.json"
        changed = {"profiles": [{"enabled": False, "config": {"symbol": "LEGACY"}}]}
        settings.write_text(json.dumps(changed), encoding="utf-8")
        result = self._sweep(generation="restart:1", fresh=True)[0]
        self.assertEqual(result["classification"], "manual_review_cleanup_conflict")
        self.assertEqual(json.loads(settings.read_text()), changed)
        destination = Path(intent["controls"][0]["destination"])
        if destination.exists():
            self.assertEqual(len(list(destination.parent.glob(destination.name))), 1)
        self.assertIn("LEGACY", json.loads(state_path.read_text())["pendingCleanup"])

    def test_control_added_after_intent_is_preserved_for_manual_review(self):
        self._state()
        control = self.data / f"dashboard_control_{self.account}_LEGACY.json"
        control.unlink()
        self._sweep(generation="run:1")
        with patch.object(
            self.cleaner, "_apply_intent", return_value=(False, [], "pause after intent"),
        ):
            self._sweep(generation="run:2")
        state = self.cleaner._read_state()
        self.assertEqual(state["pendingCleanup"]["LEGACY"]["controls"], [])

        control.write_text(json.dumps({"symbol": "LEGACY", "auto_buy": True}), encoding="utf-8")
        targets = [
            self.data / f"tranche_bases_{self.account}.json",
            self.data / f"symbol_lifecycles_{self.account}.json",
            self.data / f"dashboard_settings_{self.account}.json",
            self.data / f"dashboard_control_{self.account}.json",
            control,
        ]
        before = {path: path.read_bytes() for path in targets}
        result = self._sweep(generation="restart:1", fresh=True)[0]
        self.assertEqual(result["classification"], "manual_review_cleanup_conflict")
        self.assertIn("unplanned control file", result["reason"])
        self.assertEqual(before, {path: path.read_bytes() for path in targets})
        self.assertIn("LEGACY", self.cleaner._read_state()["pendingCleanup"])

    def test_two_cleaners_do_not_double_count_one_generation(self):
        self._state()
        other = OrphanStateCleaner(self.account, self.data, market="US")
        barrier = threading.Barrier(2)
        errors = []

        def run(cleaner, generation="shared:1", apply=False):
            try:
                barrier.wait()
                cleaner.sweep(
                    {}, True, lambda _symbol: False,
                    balance_generation=generation, fresh_balance=True, apply=apply,
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(cleaner,)) for cleaner in (self.cleaner, other)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        state = json.loads((self.data / f"orphan_cleanup_{self.account}.json").read_text())
        self.assertEqual(state["zeroConfirmations"]["LEGACY"], 1)
        barrier = threading.Barrier(2)
        errors.clear()

        def retain_intent(_intent):
            return False, [], "hold pending intent"

        with (
            patch.object(self.cleaner, "_apply_intent", side_effect=retain_intent),
            patch.object(other, "_apply_intent", side_effect=retain_intent),
        ):
            threads = [
                threading.Thread(target=run, args=(cleaner, "shared:2", True))
                for cleaner in (self.cleaner, other)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(errors, [])
        state = json.loads((self.data / f"orphan_cleanup_{self.account}.json").read_text())
        self.assertEqual(state["zeroConfirmations"]["LEGACY"], 2)
        self.assertIn("LEGACY", state["pendingCleanup"])

    def test_separate_processes_preserve_account_confirmations_and_intent(self):
        self._state()
        context = get_context("spawn")

        def run_together(generation, apply):
            ready = context.Queue()
            start = context.Event()
            workers = [
                context.Process(
                    target=_process_sweep,
                    args=(str(self.data), self.account, generation, apply, ready, start),
                )
                for _ in range(2)
            ]
            try:
                for worker in workers:
                    worker.start()
                for _ in workers:
                    self.assertTrue(ready.get(timeout=15))
                start.set()
                for worker in workers:
                    worker.join(timeout=15)
                self.assertEqual([worker.exitcode for worker in workers], [0, 0])
            finally:
                for worker in workers:
                    if worker.is_alive():
                        worker.terminate()
                        worker.join(timeout=5)
                ready.close()

        run_together("shared-process:1", False)
        state = json.loads(self.cleaner.state_path.read_text())
        self.assertEqual(state["zeroConfirmations"]["LEGACY"], 1)
        run_together("shared-process:2", True)
        state = json.loads(self.cleaner.state_path.read_text())
        self.assertEqual(state["zeroConfirmations"]["LEGACY"], 2)
        self.assertIn("LEGACY", state["pendingCleanup"])

    def test_replay_is_blocked_by_each_fail_closed_broker_condition(self):
        scenarios = (
            ("incomplete", {}, False, lambda _symbol: False, "blocked_incomplete_balance"),
            ("nonzero", {"LEGACY": 1}, True, lambda _symbol: False, "protected_nonzero_holding"),
            ("unresolved", {}, True, lambda _symbol: True, "manual_review_required"),
            (
                "inspection_error", {}, True,
                lambda _symbol: (_ for _ in ()).throw(RuntimeError("ledger unavailable")),
                "manual_review_unresolved_order_inspection",
            ),
        )
        for name, quantities, complete, unresolved, expected in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                data = Path(directory)
                cleaner = OrphanStateCleaner(self.account, data, market="US")
                original_data, original_cleaner = self.data, self.cleaner
                self.data, self.cleaner = data, cleaner
                try:
                    self._state()
                    self._sweep(generation="run:1", apply=False)
                    state = cleaner._read_state()
                    state["pendingCleanup"]["LEGACY"] = cleaner._build_intent(
                        "LEGACY", ["run:1", "run:2"],
                    )
                    state["zeroConfirmations"]["LEGACY"] = 2
                    cleaner._write_state(state)
                    result = self._sweep(
                        quantities, complete=complete, generation="restart:1",
                        fresh=name != "incomplete", unresolved=unresolved,
                    )[0]
                    self.assertEqual(result["classification"], expected)
                    retained = json.loads(cleaner.state_path.read_text())
                    self.assertIn("LEGACY", retained["pendingCleanup"])
                    self.assertNotIn("LEGACY", retained["zeroConfirmations"])
                finally:
                    self.data, self.cleaner = original_data, original_cleaner

    def test_replay_reuses_one_fixed_archive_destination(self):
        self._state()
        self._sweep(generation="run:1", apply=False)
        state = self.cleaner._read_state()
        intent = self.cleaner._build_intent("LEGACY", ["run:1", "run:2"])
        state["pendingCleanup"]["LEGACY"] = intent
        self.cleaner._write_state(state)
        destination = Path(intent["controls"][0]["destination"])
        first = self._sweep(generation="restart:1", fresh=True)[0]
        self.assertEqual(first["classification"], "cleaned")
        self.assertTrue(destination.exists())
        state = self.cleaner._read_state()
        state["pendingCleanup"]["LEGACY"] = intent
        self.cleaner._write_state(state)
        second = self._sweep(generation="restart:2", fresh=True)[0]
        self.assertEqual(second["classification"], "cleaned")
        self.assertEqual(list(destination.parent.glob(f"{destination.stem}*")), [destination])

    def test_interruption_after_each_cleanup_stage_resumes(self):
        failure_points = ("base", "lifecycle", "controls", "settings", "account_control")
        for failure_point in failure_points:
            with self.subTest(failure_point=failure_point), tempfile.TemporaryDirectory() as directory:
                data = Path(directory)
                cleaner = OrphanStateCleaner(self.account, data, market="US")
                original_data, original_cleaner = self.data, self.cleaner
                self.data, self.cleaner = data, cleaner
                try:
                    self._state()
                    self._sweep(generation="run:1", apply=False)
                    state = cleaner._read_state()
                    intent = cleaner._build_intent("LEGACY", ["run:1", "run:2"])
                    state["pendingCleanup"]["LEGACY"] = intent
                    cleaner._write_state(state)
                    original_mapping = cleaner._apply_mapping_target
                    original_whole = cleaner._apply_whole_file_target
                    mapping_calls = 0
                    whole_calls = 0

                    def interrupted_mapping(*args, **kwargs):
                        nonlocal mapping_calls
                        mapping_calls += 1
                        result = original_mapping(*args, **kwargs)
                        if failure_point == "base" and mapping_calls == 1:
                            raise OSError("interrupted after base")
                        if failure_point == "lifecycle" and mapping_calls == 2:
                            raise OSError("interrupted after lifecycle")
                        return result

                    def interrupted_whole(*args, **kwargs):
                        nonlocal whole_calls
                        whole_calls += 1
                        if failure_point == "controls" and whole_calls == 1:
                            raise OSError("interrupted after controls")
                        result = original_whole(*args, **kwargs)
                        if failure_point == "settings" and whole_calls == 1:
                            raise OSError("interrupted after settings")
                        if failure_point == "account_control" and whole_calls == 2:
                            raise OSError("interrupted after account control")
                        return result

                    with (
                        patch.object(cleaner, "_apply_mapping_target", side_effect=interrupted_mapping),
                        patch.object(cleaner, "_apply_whole_file_target", side_effect=interrupted_whole),
                    ):
                        blocked = self._sweep(generation="restart:1", fresh=True)[0]
                    self.assertEqual(blocked["classification"], "manual_review_cleanup_conflict")
                    restarted = OrphanStateCleaner(self.account, data, market="US")
                    resumed = restarted.sweep(
                        {}, True, lambda _symbol: False,
                        balance_generation="restart:2", fresh_balance=True,
                        balance_fetch_started_at=datetime.now(timezone.utc).isoformat(),
                    )[0]
                    self.assertEqual(resumed["classification"], "cleaned")
                    self.assertNotIn(
                        "LEGACY", json.loads(restarted.state_path.read_text())["pendingCleanup"],
                    )
                finally:
                    self.data, self.cleaner = original_data, original_cleaner


if __name__ == "__main__":
    unittest.main()
