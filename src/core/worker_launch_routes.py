"""Resolve an explicitly pinned worker source root for a mock account."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


class WorkerLaunchRouteError(RuntimeError):
    """Raised when an explicit worker launch route cannot be trusted."""


_MOCK_ROUTES = {
    ("kr_mock", "KR"): ("KIWOOM_WORKER_ROOT_KR_MOCK", "KIWOOM_WORKER_REVISION_KR_MOCK"),
    ("us_mock", "US"): ("KIWOOM_WORKER_ROOT_US_MOCK", "KIWOOM_WORKER_REVISION_US_MOCK"),
}
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


def worker_route_environment_keys(account: str | None, market: str | None) -> tuple[str, str] | None:
    """Return source pin keys only for the two supported mock scopes."""
    return _MOCK_ROUTES.get((account, market))


def worker_route_configured(
    account: str,
    market: str,
    environ: dict[str, str] | None = None,
) -> bool:
    """Identify an explicit mock route independently of source-path equality."""
    keys = worker_route_environment_keys(account, market)
    if keys is None:
        return False
    values = os.environ if environ is None else environ
    return any(values.get(key, "").strip() for key in keys)


def resolve_worker_root(
    account: str,
    market: str,
    default_root: Path,
    environ: dict[str, str] | None = None,
) -> Path:
    """Require US mock pins; retain local defaults for other unconfigured routes."""
    if account == "us_mock" and market != "US":
        raise WorkerLaunchRouteError("worker launch route account and market mismatch")
    keys = worker_route_environment_keys(account, market)
    if keys is None:
        for routed_account, routed_market in _MOCK_ROUTES:
            if account == routed_account and worker_route_configured(account, routed_market, environ):
                raise WorkerLaunchRouteError("worker launch route account and market mismatch")
        return default_root
    if not worker_route_configured(account, market, environ):
        if (account, market) == ("us_mock", "US"):
            raise WorkerLaunchRouteError("US mock worker launch requires both root and revision")
        return default_root

    values = os.environ if environ is None else environ
    raw_root = values.get(keys[0], "").strip()
    revision = values.get(keys[1], "").strip()
    if not raw_root and not revision:
        if (account, market) == ("us_mock", "US"):
            raise WorkerLaunchRouteError("US mock worker launch requires both root and revision")
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
