"""Back up selected project files into timestamped ZIP snapshots."""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.runtime_paths import backup_dir


EXCLUDED_DIRS = {".git", "secrets", "data", "logs", "__pycache__", ".venv", ".pytest_cache", ".pytest-tmp", "pytest_tmp", ".pytest_tmp"}


def _matches(path: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(path, pattern)


def _excluded(relative: str) -> bool:
    parts = relative.split("/")
    if any(part in EXCLUDED_DIRS for part in parts):
        return True
    patterns = (
        ".env", "config/accounts.yaml", "*.db", "*.db-shm", "*.db-wal", "*.pyc",
        "CURRENT_STATE.md", "round*.py", "round*.txt", "round*.diff", "round*.bin",
        "r7[0-9][0-9]_*.txt", "diagnostics/round*.py", "diagnostics/round*.txt",
        "diagnostics/backup_*", "diagnostics/fixed_port_release_measurement_*.jsonl",
        "diagnostics/main_py_HEAD_reference*.txt", "ops/emergency_stop.ps1", "src/*.diff",
        "*.lnk", "pytest_round*.txt", "*_PRE_FIX_BACKUP_*.xml", "wd_test2.py",
        ".pytest_tmp*", ".pytest-tmp*", "ops/scratch", "ops/scratch/*",
        ".DS_Store", "Thumbs.db", "tools/*_diag_r*.py", "tools/*_PYTHON*.txt",
        "tools/_r*_committed_blob.py",
    )
    return any(_matches(relative, pattern) for pattern in patterns)


def _included(relative: str) -> bool:
    if _excluded(relative):
        return False
    root = relative.split("/", 1)[0]
    if root in {"src", "tools", "dashboard", "tests", "config", "ops"}:
        return True
    if root == "diagnostics":
        return Path(relative).suffix.lower() in {".py", ".md", ".txt"}
    return root == ".gitignore" or _matches(relative, "*.md") or _matches(relative, "*.txt") or _matches(relative, "README*")


def selected_files(project_root: Path) -> list[Path]:
    result: list[Path] = []
    for directory, dirnames, filenames in os.walk(project_root):
        current = Path(directory)
        relative_dir = current.relative_to(project_root).as_posix() if current != project_root else ""
        dirnames[:] = [
            name for name in dirnames
            if not _excluded(f"{relative_dir}/{name}".strip("/"))
        ]
        for filename in filenames:
            path = current / filename
            relative = path.relative_to(project_root).as_posix()
            if _included(relative):
                result.append(path)
    return sorted(result)


def _prune(destination: Path, keep: int, current_run: Path) -> list[str]:
    runs: list[tuple[dt.datetime, Path]] = []
    for child in destination.iterdir():
        if not child.is_dir() or child == current_run:
            continue
        try:
            stamp = dt.datetime.strptime(child.name, "%Y%m%d_%H%M%S")
        except ValueError:
            continue
        runs.append((stamp, child))
    runs.sort(reverse=True, key=lambda item: item[0])
    removed: list[str] = []
    for _, path in runs[max(keep - 1, 0):]:
        shutil.rmtree(path)
        removed.append(str(path))
    return removed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, default=backup_dir())
    parser.add_argument("--retention-count", type=int, default=7)
    args = parser.parse_args()
    if args.retention_count < 1:
        parser.error("--retention-count must be at least 1")

    try:
        destination = args.destination.resolve()
        destination.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = destination / stamp
        run_dir.mkdir()
        archive_path = run_dir / "project_files.zip"
        manifest_files: list[dict[str, object]] = []
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for source in selected_files(PROJECT_ROOT):
                relative = source.relative_to(PROJECT_ROOT).as_posix()
                payload = source.read_bytes()
                archive.writestr(relative, payload)
                manifest_files.append({
                    "path": relative,
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                })
        manifest_path = run_dir / "manifest.json"
        manifest_path.write_text(json.dumps({
            "created_at": dt.datetime.now().isoformat(),
            "project_root": str(PROJECT_ROOT),
            "archive": archive_path.name,
            "files": manifest_files,
        }, indent=2), encoding="utf-8")
        removed = _prune(destination, args.retention_count, run_dir)
        print(json.dumps({"run_dir": str(run_dir), "archive": str(archive_path), "file_count": len(manifest_files), "removed_runs": removed}))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
