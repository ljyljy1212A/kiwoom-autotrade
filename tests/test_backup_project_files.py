from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

from tools import backup_project_files as backup


def _write(root: Path, relative: str, content: str = "test") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_backup_creates_manifest_archive_applies_scope_and_prunes(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    destination = tmp_path / "backups"
    _write(project, "src/kept.py")
    _write(project, "diagnostics/kept.md")
    _write(project, "diagnostics/generated.json")
    _write(project, "config/accounts.yaml")
    _write(project, "data/runtime.json")
    _write(project, ".git/HEAD")
    for stamp in ("20200101_000000", "20200102_000000", "20200103_000000"):
        (destination / stamp).mkdir(parents=True)

    monkeypatch.setattr(backup, "PROJECT_ROOT", project)
    with patch.object(sys, "argv", ["backup_project_files.py", "--destination", str(destination), "--retention-count", "2"]):
        assert backup.main() == 0

    payload = json.loads(capsys.readouterr().out)
    run_dir = Path(payload["run_dir"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    paths = {item["path"] for item in manifest["files"]}
    with zipfile.ZipFile(payload["archive"]) as archive:
        archive_paths = set(archive.namelist())

    assert (run_dir / "manifest.json").is_file()
    assert Path(payload["archive"]).is_file()
    assert {"src/kept.py", "diagnostics/kept.md"} <= paths
    assert {"src/kept.py", "diagnostics/kept.md"} <= archive_paths
    assert "diagnostics/generated.json" not in paths
    assert "config/accounts.yaml" not in paths
    assert "data/runtime.json" not in paths
    assert ".git/HEAD" not in archive_paths
    assert not (destination / "20200101_000000").exists()
    assert not (destination / "20200102_000000").exists()
    assert (destination / "20200103_000000").is_dir()


def test_backup_returns_nonzero_when_destination_is_a_file(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    _write(project, "src/kept.py")
    destination = tmp_path / "not-a-directory"
    destination.write_text("forced failure", encoding="utf-8")
    monkeypatch.setattr(backup, "PROJECT_ROOT", project)

    with patch.object(sys, "argv", ["backup_project_files.py", "--destination", str(destination)]):
        assert backup.main() == 1

    assert "error" in json.loads(capsys.readouterr().err)
