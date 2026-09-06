"""Harmless Scheduled Task validation placeholder; draft only."""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_INFORMATION_CLASS_INTEGRITY = 25
SECURITY_MANDATORY_LABEL_RID_OFFSET = 16


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Sid", ctypes.c_void_p),
        ("Attributes", ctypes.wintypes.DWORD),
    ]


class TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", SID_AND_ATTRIBUTES)]


def integrity_level() -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    advapi32.OpenProcessToken.argtypes = [
        ctypes.c_void_p,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetTokenInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(ctypes.wintypes.DWORD),
    ]
    advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.wintypes.BYTE)
    advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, ctypes.wintypes.DWORD]
    advapi32.GetSidSubAuthority.restype = ctypes.POINTER(ctypes.wintypes.DWORD)

    token = ctypes.c_void_p()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = ctypes.wintypes.DWORD()
        advapi32.GetTokenInformation(
            token,
            TOKEN_INFORMATION_CLASS_INTEGRITY,
            None,
            0,
            ctypes.byref(needed),
        )
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(
            token,
            TOKEN_INFORMATION_CLASS_INTEGRITY,
            ctypes.byref(buffer),
            needed.value,
            ctypes.byref(needed),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        label = ctypes.cast(buffer, ctypes.POINTER(TOKEN_MANDATORY_LABEL)).contents
        count = advapi32.GetSidSubAuthorityCount(label.Label.Sid)[0]
        authority = advapi32.GetSidSubAuthority(label.Label.Sid, count - 1)[0]
        return int(authority)
    finally:
        kernel32.CloseHandle(token)


def main() -> int:
    output_path = Path(__file__).resolve().parents[1] / "data" / "WD_Test" / "relaunch_validation.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "integrityLevelRid": integrity_level(),
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    time.sleep(30)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
