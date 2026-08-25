import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.core.engine import AccountEngine


class _Logger:
    def warning(self, *_args):
        pass


def _engine(path: Path, bases=None):
    engine = AccountEngine.__new__(AccountEngine)
    engine._tranche_bases_path = path
    engine._tranche_bases = dict(bases or {})
    engine.ctx = SimpleNamespace(logger=_Logger(), client=SimpleNamespace(market="KR"))
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
