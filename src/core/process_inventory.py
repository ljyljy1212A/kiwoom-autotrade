"""Read-only cross-platform process inventory helpers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace


_PROC_ROOT = Path("/proc")


def _query_win32_processes() -> list[SimpleNamespace]:
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


def _list_posix_pids() -> list[int]:
    return sorted(
        int(entry.name)
        for entry in _PROC_ROOT.iterdir()
        if entry.name.isdigit()
    )


def _read_proc_text(pid: int, name: str) -> str:
    return (_PROC_ROOT / str(pid) / name).read_text(encoding="utf-8", errors="replace")


def _read_posix_clk_tck() -> int | None:
    sysconf = getattr(os, "sysconf", None)
    if sysconf is None:
        return None
    try:
        value = int(sysconf("SC_CLK_TCK"))
    except (OSError, TypeError, ValueError):
        return None
    return value if value > 0 else None


def _parse_posix_stat(stat_text: str) -> tuple[int, int]:
    _, remainder = stat_text.rsplit(") ", 1)
    fields = remainder.split()
    if len(fields) <= 19:
        raise ValueError("/proc stat is missing required fields")
    parent_pid = int(fields[1])
    start_ticks = int(fields[19])
    if parent_pid <= 0 or start_ticks < 0:
        raise ValueError("/proc stat contains invalid process values")
    return parent_pid, start_ticks


def _read_posix_uptime() -> str:
    return (_PROC_ROOT / "uptime").read_text(encoding="utf-8", errors="replace")


def _posix_creation_time(start_ticks: int, clk_tck: int | None) -> str | None:
    if clk_tck is None:
        return None
    try:
        uptime = float(_read_posix_uptime().split()[0])
        created = time.time() - uptime + (start_ticks / clk_tck)
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created))
    except (IndexError, OSError, TypeError, ValueError):
        return None


def query_posix_processes() -> list[SimpleNamespace]:
    """Return best-effort read-only process records from procfs."""
    clk_tck = _read_posix_clk_tck()
    records: list[SimpleNamespace] = []
    try:
        pids = _list_posix_pids()
    except (OSError, ValueError):
        return records
    for pid in pids:
        try:
            parent_pid, start_ticks = _parse_posix_stat(_read_proc_text(pid, "stat"))
            command_line = " ".join(
                part for part in _read_proc_text(pid, "cmdline").split("\x00") if part
            )
            name = _read_proc_text(pid, "comm").strip()
            if not name or not command_line:
                continue
        except (OSError, UnicodeError, ValueError):
            continue
        records.append(
            SimpleNamespace(
                Name=name,
                ProcessId=pid,
                ParentProcessId=parent_pid,
                CommandLine=command_line,
                CreationDate=_posix_creation_time(start_ticks, clk_tck),
            )
        )
    return records


def query_win32_processes() -> list[SimpleNamespace]:
    """Return process records through the existing watchdog provider seam."""
    if os.name == "nt":
        return _query_win32_processes()
    return query_posix_processes()
