"""Construct exact US mock bootstrap commands without importing a supervisor."""
from __future__ import annotations

from pathlib import Path
import re


class USMockCommandError(ValueError):
    """An explicit bootstrap route is missing or invalid."""


def canonical_directory(value: str | Path) -> Path:
    try:
        path = Path(value)
        if not path.is_absolute():
            raise USMockCommandError("US mock route requires absolute directories")
        resolved = path.resolve(strict=True)
        if resolved != path or not resolved.is_dir():
            raise USMockCommandError("US mock route requires canonical directories")
        return resolved
    except USMockCommandError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise USMockCommandError("US mock route directory unavailable") from exc


def bootstrap_command(
    executable: str, action: str, runtime_root: str | Path,
    source_root: str | Path, revision: str,
) -> list[str]:
    if action not in {"start", "status", "stop", "watchdog"}:
        raise USMockCommandError("unsupported US mock lifecycle action")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
        raise USMockCommandError("US mock route requires a full revision pin")
    runtime = canonical_directory(runtime_root)
    source = canonical_directory(source_root)
    if source == runtime:
        raise USMockCommandError("US mock source must be separate from runtime")
    script = source / "tools/us_mock_launch_policy.py"
    try:
        if not script.is_file() or script.resolve(strict=True) != script:
            raise USMockCommandError("US mock bootstrap unavailable")
    except (OSError, RuntimeError) as exc:
        raise USMockCommandError("US mock bootstrap unavailable") from exc
    return [executable, "-B", "-P", str(script), action,
            "--runtime-root", str(runtime), "--source-root", str(source),
            "--revision", revision]
