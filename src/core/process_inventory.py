"""Read-only Windows process inventory helpers."""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace


def query_win32_processes() -> list[SimpleNamespace]:
    """Return raw Win32_Process records needed by process observers."""
    command = (
        "$ErrorActionPreference = 'Stop'; "
        "Get-CimInstance Win32_Process | "
        "Select-Object Name,ProcessId,ParentProcessId,CommandLine,CreationDate | "
        "ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
            or result.stdout.strip()
            or "Win32_Process query failed"
        )
    if not result.stdout.strip():
        return []
    payload = json.loads(result.stdout)
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise ValueError("Win32_Process query returned a non-list result")
    return [SimpleNamespace(**item) for item in payload if isinstance(item, dict)]
