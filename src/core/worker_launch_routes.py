"""Resolve an explicitly pinned worker source root for a mock account."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


class WorkerLaunchRouteError(RuntimeError):
    """Raised when an explicit worker launch route cannot be trusted."""


_KR_MOCK_ACCOUNT = "kr_mock"
_KR_MARKET = "KR"
_ROOT_ENV = "KIWOOM_WORKER_ROOT_KR_MOCK"
_REVISION_ENV = "KIWOOM_WORKER_REVISION_KR_MOCK"
_REVISION_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def _git_output(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={root}", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkerLaunchRouteError(
            f"worker launch route Git verification failed ({type(exc).__name__})"
        ) from exc
    if result.returncode != 0 or result.stderr.strip():
        raise WorkerLaunchRouteError(
            f"worker launch route Git verification failed (exit={result.returncode})"
        )
    return result.stdout.strip()


def worker_route_configured(
    account: str,
    market: str,
    environ: dict[str, str] | None = None,
) -> bool:
    """Identify an explicit mock route independently of source-path equality."""
    if account != _KR_MOCK_ACCOUNT or market != _KR_MARKET:
        return False
    values = os.environ if environ is None else environ
    return bool(values.get(_ROOT_ENV, "").strip() or values.get(_REVISION_ENV, "").strip())


def resolve_worker_root(
    account: str,
    market: str,
    default_root: Path,
    environ: dict[str, str] | None = None,
) -> Path:
    """Use a pinned KR mock source root when configured; keep other routes local."""
    if not worker_route_configured(account, market, environ):
        return default_root

    values = os.environ if environ is None else environ
    raw_root = values.get(_ROOT_ENV, "").strip()
    revision = values.get(_REVISION_ENV, "").strip()
    if not raw_root and not revision:
        return default_root
    if not raw_root or not revision:
        raise WorkerLaunchRouteError("worker launch route requires both root and revision")
    if not isinstance(revision, str) or not _REVISION_RE.fullmatch(revision):
        raise WorkerLaunchRouteError("worker launch route expected_revision is invalid")

    try:
        requested_root = Path(raw_root)
        if not requested_root.is_absolute():
            raise WorkerLaunchRouteError("worker launch route project_root must be absolute")
        target_root = requested_root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, WorkerLaunchRouteError):
            raise
        raise WorkerLaunchRouteError(
            f"worker launch route project_root is unavailable ({type(exc).__name__})"
        ) from exc
    if (not target_root.is_dir()
            or not (target_root / "src" / "main.py").is_file()
            or not (target_root / "src" / "worker_supervisor.py").is_file()):
        raise WorkerLaunchRouteError("worker launch route is not a valid project root")

    git_root = _git_output(target_root, "rev-parse", "--show-toplevel")
    try:
        resolved_git_root = Path(git_root).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkerLaunchRouteError("worker launch route Git root is invalid") from exc
    if resolved_git_root != target_root:
        raise WorkerLaunchRouteError("worker launch route Git root mismatch")

    actual_revision = _git_output(target_root, "rev-parse", "HEAD")
    if actual_revision.lower() != revision.lower():
        raise WorkerLaunchRouteError("worker launch route revision mismatch")

    source_changes = _git_output(
        target_root,
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        "src",
        "src.py",
    )
    if source_changes:
        raise WorkerLaunchRouteError("worker launch route source tree is not clean")
    if not (target_root / "src" / "core" / "worker_environment.py").is_file():
        raise WorkerLaunchRouteError("worker launch route lacks routed-worker environment support")
    return target_root
