"""Back up project SQLite databases and prune backups older than seven days."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sqlite3
import sys
from pathlib import Path

_PROJECT_ROOT_FOR_IMPORT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT_FOR_IMPORT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT_FOR_IMPORT))

from src.core.runtime_paths import backup_dir


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATABASES = (
    PROJECT_ROOT / "data" / "trades_kr_mock.db",
    PROJECT_ROOT / "data" / "trades_us_mock.db",
    PROJECT_ROOT / "data" / "reports_kr_mock.db",
    PROJECT_ROOT / "data" / "reports_us_mock.db",
    PROJECT_ROOT / "data" / "dedup_kr_mock.db",
    PROJECT_ROOT / "data" / "dedup_us_mock.db",
)


def backup_database(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as source_db, sqlite3.connect(target) as target_db:
        source_db.backup(target_db)
        result = target_db.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError(f"Integrity check failed for {target}: {result}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, default=backup_dir())
    parser.add_argument("--retention-days", type=int, default=7)
    parser.add_argument("--restore-test", action="store_true")
    args = parser.parse_args()

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = args.destination / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"created_at": dt.datetime.now().isoformat(), "databases": []}

    for source in DATABASES:
        if not source.exists():
            continue
        target = run_dir / source.name
        backup_database(source, target)
        manifest["databases"].append({"source": str(source), "backup": str(target)})

    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    if args.restore_test:
        restore_dir = run_dir / "restore_test"
        for item in manifest["databases"]:
            restored = restore_dir / Path(item["backup"]).name
            restored.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item["backup"], restored)
            with sqlite3.connect(restored) as db:
                result = db.execute("PRAGMA integrity_check").fetchone()
                if not result or result[0] != "ok":
                    raise RuntimeError(f"Restore integrity check failed: {restored}: {result}")

    cutoff = dt.datetime.now() - dt.timedelta(days=args.retention_days)
    for child in args.destination.iterdir():
        if not child.is_dir() or child == run_dir or child.name == "restore_test":
            continue
        try:
            created = dt.datetime.strptime(child.name, "%Y%m%d_%H%M%S")
        except ValueError:
            continue
        if created < cutoff:
            shutil.rmtree(child)

    print(json.dumps({"backup_run": str(run_dir), "databases": len(manifest["databases"]), "restore_test": args.restore_test}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
