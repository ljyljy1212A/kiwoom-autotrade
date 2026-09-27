import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.core.engine import AccountEngine


class _Logger:
    def warning(self, *_args):
        pass


def _engine(path: Path, bases=None):
    engine = AccountEngine.__new__(AccountEngine)
    engine.data_dir = path.parent
    engine._tranche_bases_path = path
    engine._tranche_bases = dict(bases or {})
    engine.ctx = SimpleNamespace(
        account_id="kr_mock", logger=_Logger(), client=SimpleNamespace(market="KR")
    )
    return engine


class TrancheBasePersistenceTest(unittest.TestCase):
    def test_concurrent_engines_preserve_different_symbols(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tranche_bases.json"
            path.write_text("{}", encoding="utf-8")
            first, second = _engine(path), _engine(path)
            start = threading.Barrier(2)
            errors = []

            def persist(engine, symbol, price):
                try:
                    start.wait(timeout=5)
                    engine._store_tranche_base(symbol, price)
                except BaseException as exc:
                    errors.append(exc)

            threads = [
                threading.Thread(
                    target=persist,
                    args=(first, "000490", 10_000.0),
                ),
                threading.Thread(
                    target=persist,
                    args=(second, "005930", 70_000.0),
                ),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            self.assertEqual(errors, [])
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"000490": 10_000.0, "005930": 70_000.0},
            )

    def test_stale_engine_merge_preserves_prior_symbol(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tranche_bases.json"
            path.write_text("{}", encoding="utf-8")
            stale_a, writer_b = _engine(path), _engine(path)

            writer_b._store_tranche_base("005930", 70_000.0)
            stale_a._store_tranche_base("000490", 10_000.0)

            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"000490": 10_000.0, "005930": 70_000.0},
            )

    def test_repeated_same_symbol_write_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tranche_bases.json"
            path.write_text("{}", encoding="utf-8")
            engine = _engine(path)

            engine._store_tranche_base("000490", 10_000.0)
            first = path.read_text(encoding="utf-8")
            engine._store_tranche_base("000490", 10_000.0)
            second = path.read_text(encoding="utf-8")

            self.assertEqual(first, second)
            self.assertEqual(json.loads(second), {"000490": 10_000.0})

    def test_concurrent_delete_and_write_preserve_both_operations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tranche_bases.json"
            initial = {"000490": 10_000.0}
            path.write_text(json.dumps(initial), encoding="utf-8")
            closer, writer = _engine(path, initial), _engine(path, initial)
            start = threading.Barrier(2)
            errors = []

            def remove_base():
                try:
                    start.wait(timeout=5)
                    closer._remove_tranche_base("000490")
                except BaseException as exc:
                    errors.append(exc)

            def write_base():
                try:
                    start.wait(timeout=5)
                    writer._store_tranche_base("005930", 70_000.0)
                except BaseException as exc:
                    errors.append(exc)

            threads = [
                threading.Thread(target=remove_base),
                threading.Thread(target=write_base),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            self.assertEqual(errors, [])
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"005930": 70_000.0},
            )

    def test_reconciliation_price_drift_does_not_rewrite_tranche_base(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tranche_bases.json"
            path.write_text(
                json.dumps({"000490": 10_000.0}),
                encoding="utf-8",
            )
            engine = _engine(path, {"000490": 10_000.0})

            engine._store_tranche_base(
                "000490",
                9_500.0,
                only_if_absent=True,
            )

            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"000490": 10_000.0},
            )

    def test_stale_lifecycle_anchor_rejects_tranche_base_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tranche_bases.json"
            initial_bases = {"000490": 10_000.0}
            path.write_text(json.dumps(initial_bases), encoding="utf-8")
            lifecycle_path = Path(directory) / "symbol_lifecycles.json"
            lifecycle_path.write_text(
                json.dumps({"000490": {"status": "open", "started_at": "new"}}),
                encoding="utf-8",
            )
            engine = _engine(path, initial_bases)
            engine.ctx.strategy = SimpleNamespace(symbol="000490")
            engine._lifecycle_path = lifecycle_path
            engine._lifecycle_disk_present = True
            engine._lifecycle_disk_state = {"status": "open", "started_at": "old"}
            original = path.read_bytes()

            with self.assertRaisesRegex(RuntimeError, "Concurrent symbol lifecycle change"):
                engine._store_tranche_base("000490", 11_000.0)

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(engine._tranche_bases, initial_bases)

    def test_lifecycle_read_failure_preserves_tranche_base_file_and_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tranche_bases.json"
            initial_bases = {"000490": 10_000.0}
            path.write_text(json.dumps(initial_bases), encoding="utf-8")
            lifecycle_path = Path(directory) / "symbol_lifecycles.json"
            lifecycle_path.write_text(
                json.dumps({"000490": {"status": "open", "started_at": "current"}}),
                encoding="utf-8",
            )
            engine = _engine(path, initial_bases)
            engine.ctx.strategy = SimpleNamespace(symbol="000490")
            engine._lifecycle_path = lifecycle_path
            engine._lifecycle_disk_present = True
            engine._lifecycle_disk_state = {"status": "open", "started_at": "stale"}
            original_bases = path.read_bytes()
            original_lifecycle = lifecycle_path.read_bytes()
            original_read_text = Path.read_text

            def fail_lifecycle_read(target: Path, *args, **kwargs):
                if target == lifecycle_path:
                    raise PermissionError("simulated lifecycle read denial")
                return original_read_text(target, *args, **kwargs)

            with patch.object(Path, "read_text", new=fail_lifecycle_read):
                with patch("src.core.engine.atomic_write_json") as write_json:
                    engine._store_tranche_base("000490", 11_000.0)

            self.assertEqual(path.read_bytes(), original_bases)
            self.assertEqual(lifecycle_path.read_bytes(), original_lifecycle)
            self.assertEqual(engine._tranche_bases, initial_bases)
            write_json.assert_not_called()

    def test_store_failure_preserves_file_and_memory_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tranche_bases.json"
            initial_bases = {"000490": 10_000.0}
            path.write_text(json.dumps(initial_bases), encoding="utf-8")
            engine = _engine(path, initial_bases)
            original = path.read_bytes()

            with patch("src.core.engine.atomic_write_json", side_effect=OSError("disk full")):
                engine._store_tranche_base("000490", 11_000.0)

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(engine._tranche_bases, initial_bases)

    def test_remove_failure_preserves_file_and_memory_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tranche_bases.json"
            initial_bases = {"000490": 10_000.0}
            path.write_text(json.dumps(initial_bases), encoding="utf-8")
            engine = _engine(path, initial_bases)
            original = path.read_bytes()

            with patch("src.core.engine.atomic_write_json", side_effect=OSError("disk full")):
                engine._remove_tranche_base("000490")

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(engine._tranche_bases, initial_bases)
