"""The sole supported launcher/status command for one account worker.

The dashboard and the Windows batch files delegate here.  This module is the
only place outside ``src.main`` that may create a worker process; the worker's
OS-level account lock remains the final authority if two start commands race.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from src.core.process_lock import ProcessLock
from src.core.control_state import read_auto_trading_enabled
from src.core.runtime_paths import DATA_DIR
from src.core.account_catalog import is_real_account
from src.utils.logger import get_logger


ROOT = Path(__file__).resolve().parents[1]
_GRACEFUL_STOP_TIMEOUT_SEC = 10.0
_FORCE_STOP_TIMEOUT_SEC = 5.0
_STARTUP_ACK_TIMEOUT_SEC = 30.0


def _worker_lock(account: str) -> ProcessLock:
    return ProcessLock(account, DATA_DIR)


def _pid_path(account: str) -> Path:
    return DATA_DIR / f"worker_{account}.pid"


def _status_path(account: str) -> Path:
    return DATA_DIR / f"worker_{account}.status.json"


def _stop_request_path(account: str) -> Path:
    return DATA_DIR / f"worker_{account}.stop.request.json"


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{time.time_ns()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))
        kernel32.GetExitCodeProcess.restype = ctypes.c_bool
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_bool
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_uint32()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259)
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _process_creation_time(pid: int):
    if os.name != "nt" or pid <= 0:
        return None
    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetProcessTimes.argtypes = (
        ctypes.c_void_p, ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
    )
    kernel32.GetProcessTimes.restype = ctypes.c_bool
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_bool
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        created, exited, kernel_time, user_time = (FILETIME(), FILETIME(), FILETIME(), FILETIME())
        if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                        ctypes.byref(kernel_time), ctypes.byref(user_time)):
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return datetime.fromtimestamp((ticks - 116444736000000000) / 10_000_000, timezone.utc)
    finally:
        kernel32.CloseHandle(handle)


def _corroborate_suspect(account: str, metadata: dict) -> dict:
    pid = int(metadata.get("pid", 0) or 0)
    pid_alive = _pid_alive(pid)
    creation_time = _process_creation_time(pid) if pid_alive else None
    try:
        started_at = datetime.fromisoformat(str(metadata.get("startedAt", "")).replace("Z", "+00:00"))
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        timestamp_match = creation_time is not None and abs((creation_time - started_at).total_seconds()) <= 5
    except (TypeError, ValueError):
        timestamp_match = False
    return {
        "suspectPidAlive": pid_alive,
        "suspectIdentityMatch": metadata.get("account") == account and bool(metadata.get("instanceId")),
        "suspectCreationTimeMatch": timestamp_match,
    }
def status(account: str) -> dict:
    metadata = {}
    try:
        candidate = json.loads(_status_path(account).read_text(encoding="utf-8"))
        metadata = candidate if isinstance(candidate, dict) else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    try:
        payload = json.loads(_pid_path(account).read_text(encoding="utf-8"))
        pid = int(payload.get("pid", 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pid = 0
    if not pid:
        pid = int(metadata.get("pid", 0) or 0)
    lock = _worker_lock(account)
    liveness = lock.liveness_result()
    if liveness["liveness"] == "suspect":
        liveness.update(_corroborate_suspect(account, {**metadata, "pid": pid}))
    return {
        "account": account, "pid": pid,
        **liveness,
        "instanceId": metadata.get("instanceId"), "startedAt": metadata.get("startedAt"),
        "state": metadata.get("state"), "market": metadata.get("market"),
    }


def _owned_identity(account: str) -> tuple[int, str] | None:
    """Return a PID/instance only when both worker metadata sources agree."""
    try:
        pid_payload = json.loads(_pid_path(account).read_text(encoding="utf-8"))
        status_payload = json.loads(_status_path(account).read_text(encoding="utf-8"))
        pid = int(pid_payload.get("pid", 0))
        instance_id = str(status_payload.get("instanceId", ""))
        if (pid <= 0 or int(status_payload.get("pid", 0)) != pid or not instance_id
                or str(status_payload.get("account", "")) != account):
            return None
        return pid, instance_id
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _wait_for_stopped(account: str, pid: int, timeout_sec: float) -> bool:
    """A stop is authoritative only after both OS liveness authorities agree."""
    deadline = time.monotonic() + timeout_sec
    lock = _worker_lock(account)
    while time.monotonic() < deadline:
        if not _pid_alive(pid) and not lock.is_alive():
            return True
        time.sleep(0.1)
    return not _pid_alive(pid) and not lock.is_alive()


def _remove_pid_after_confirmed_exit(account: str, pid: int) -> None:
    """PID metadata is removable only after its owned process is demonstrably dead."""
    if _pid_alive(pid) or _worker_lock(account).is_alive():
        return
    try:
        payload = json.loads(_pid_path(account).read_text(encoding="utf-8"))
        if int(payload.get("pid", 0)) == pid:
            _pid_path(account).unlink()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass


def _record_stopped_status(account: str, pid: int) -> None:
    """Publish STOPPED only after the owned process and mutex are gone."""
    if _pid_alive(pid) or _worker_lock(account).is_alive():
        return
    payload: dict = {"account": account, "pid": pid}
    try:
        candidate = json.loads(_status_path(account).read_text(encoding="utf-8"))
        if isinstance(candidate, dict) and int(candidate.get("pid", 0) or 0) == pid:
            payload = candidate
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    payload["account"] = account
    payload["pid"] = pid
    payload["state"] = "STOPPED"
    payload["updatedAt"] = datetime.now(timezone.utc).isoformat()
    _write_atomic(_status_path(account), payload)


def start(account: str, market: str) -> tuple[int, dict]:
    current = status(account)
    if current.get("liveness") == "suspect":
        return 8, {**current, "started": False, "reason": "status-indeterminate"}
    if current["running"]:
        return 3, {**current, "started": False, "reason": "already-running"}

    env = os.environ.copy()
    env["ACCOUNT_FILTER"] = account
    env["MARKET_INSTANCE"] = market
    env.setdefault("KIWOOM_ENV", "mock")
    env["PRICE_FEED_MODE"] = "auto"
    env["TELEGRAM_APPROVAL_REQUIRED"] = "false"
    # Auto-trading is an explicit launch-time decision. Preserve the
    # configured value so the supervisor does not silently force every worker
    # back into monitor-only mode after a restart.
    control_enabled = read_auto_trading_enabled(account)
    if control_enabled is None:
        env["AUTO_TRADING_ENABLED"] = os.environ.get("AUTO_TRADING_ENABLED", "false").lower()
    else:
        env["AUTO_TRADING_ENABLED"] = "true" if control_enabled else "false"
    popen_kwargs = {
        "cwd": ROOT,
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        # The supervisor command is intentionally short-lived (batch/API).
        # Detach the worker from that caller's console/job so it remains owned
        # by its account lock and status file, not by a dashboard request.
        creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        popen_kwargs["creationflags"] = creationflags
    command = [sys.executable, "-m", "src.main", "--market", market]
    child = subprocess.Popen(
        command,
        **popen_kwargs,
    )
    # ``src.main`` claims its OS account mutex and publishes PID/status
    # metadata before the launcher can acknowledge success.  Imports,
    # configuration loading, and a prior worker's shutdown can make that
    # handoff exceed a few seconds without indicating a failed child.
    deadline = time.monotonic() + _STARTUP_ACK_TIMEOUT_SEC
    while time.monotonic() < deadline:
        current = status(account)
        if current.get("liveness") == "suspect":
            return 8, {**current, "started": False, "reason": "status-indeterminate"}
        if current["running"]:
            if current["pid"] == child.pid:
                return 0, {**current, "started": True}
            # The child claims the mutex before it can atomically publish its
            # new PID/status files.  Do not mistake that short metadata window
            # for another worker and abandon a successfully launched child.
            # Continue until its identity appears or it exits.
        if child.poll() is not None:
            current["exitCode"] = child.returncode
            current["started"] = False
            current["reason"] = "worker-refused-or-exited"
            current["failureClass"] = (
                "lock-conflict" if current.get("running") and current.get("pid") != child.pid
                else "worker-exited-before-start"
            )
            return 3, current
        time.sleep(0.05)
    final = status(account)
    if final.get("liveness") == "suspect":
        return 8, {**final, "started": False, "reason": "status-indeterminate"}
    if final["running"]:
        return 3, {**final, "started": False, "reason": "already-running"}
    return 4, {**final, "started": False, "reason": "startup-timeout"}


def _reject_real_account(account: str):
    """Refuse to act on a real-mode account unless explicitly allowed.

    Mirrors the dashboard's default-block real-account guard, but for the
    raw supervisor CLI/process-control surface, which the dashboard's own
    guard does not cover. Uses a distinct env var (ALLOW_LIVE_SUPERVISOR)
    rather than the dashboard's ALLOW_LIVE_DASHBOARD, since these are
    different risk surfaces and enabling one should not silently enable
    the other.
    """
    if is_real_account(account) and os.environ.get("ALLOW_LIVE_SUPERVISOR", "false").lower() != "true":
        return 7, {
            "account": account,
            "mode": "blocked",
            "reason": "real-account-guard",
            "error": (
                f"Refusing to act on real account '{account}' without "
                "ALLOW_LIVE_SUPERVISOR=true"
            ),
        }
    return None


def stop(account, timeout: float | None = None):
    guard = _reject_real_account(account)
    if guard is not None:
        return guard
    curr = status(account)
    if curr.get("liveness") == "suspect":
        return 8, {**curr, "mode": "unknown", "reason": "status-indeterminate"}
    if not curr.get("running"):
        return 0, {**curr, "mode": "already_stopped"}

    identity = _owned_identity(account)
    if identity is None:
        # Lock is alive but PID/status metadata hasn't converged yet
        # (e.g. a start() is still publishing its files). We cannot target
        # a specific PID safely, so report this rather than crash or guess.
        return 5, {**curr, "mode": "unknown", "reason": "identity-unresolved"}
    pid, instance_id = identity

    graceful_timeout = timeout if timeout is not None else _GRACEFUL_STOP_TIMEOUT_SEC
    force_timeout = _FORCE_STOP_TIMEOUT_SEC

    # 1. Ask the worker to stop cooperatively, then wait.
    _write_atomic(_stop_request_path(account), {
        "account": account,
        "pid": pid,
        "instanceId": instance_id,
        "requestedAt": datetime.now(timezone.utc).isoformat(),
    })

    if _wait_for_stopped(account, pid, graceful_timeout):
        mode = "graceful"
    else:
        # 2. Timeout exceeded: escalate to a platform-appropriate force-kill.
        mode = "forced"
        if sys.platform == "win32":
            logger = get_logger(account, ROOT / "logs" / f"{account}.log")
            logger.info(f"Graceful stop timed out for {account}, escalating to taskkill: pid={pid} instance={instance_id}")
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info(f"taskkill result for {account}: pid={pid} instance={instance_id} returncode={result.returncode}")
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass

        stopped = _wait_for_stopped(account, pid, force_timeout)

    # 3. Clean up metadata now that exit is confirmed (or best-effort if not).
    try:
        _stop_request_path(account).unlink(missing_ok=True)
    except OSError:
        pass
    _remove_pid_after_confirmed_exit(account, pid)
    _record_stopped_status(account, pid)
    final_st = status(account)
    if mode == "forced":
        return (0 if stopped else 6), {**final_st, "mode": mode, "stopped": stopped}
    return 0, {**final_st, "mode": mode}


def kill(account: str):
    """Immediately terminate one worker without waiting for graceful shutdown."""
    guard = _reject_real_account(account)
    if guard is not None:
        return guard
    curr = status(account)
    if curr.get("liveness") == "suspect":
        return 8, {**curr, "mode": "unknown", "reason": "status-indeterminate"}
    if not curr.get("running"):
        return 0, {**curr, "mode": "already_stopped"}

    identity = _owned_identity(account)
    if identity is None:
        return 5, {**curr, "mode": "unknown", "reason": "identity-unresolved"}
    pid, instance_id = identity

    if sys.platform == "win32":
        logger = get_logger(account, ROOT / "logs" / f"{account}.log")
        logger.info(f"Force-killing {account} via taskkill: pid={pid} instance={instance_id}")
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info(f"taskkill result for {account}: pid={pid} instance={instance_id} returncode={result.returncode}")
        if _pid_alive(pid):
            try:
                logger.info(f"taskkill did not fully terminate {account}, falling back to SIGTERM: pid={pid} instance={instance_id}")
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass

    stopped = _wait_for_stopped(account, pid, _FORCE_STOP_TIMEOUT_SEC)
    if stopped:
        _remove_pid_after_confirmed_exit(account, pid)
        _record_stopped_status(account, pid)
    final_st = status(account)
    return (0 if stopped else 6), {**final_st, "mode": "killed", "stopped": stopped}
            
def main() -> int:
    parser = argparse.ArgumentParser(description="Single-account Kiwoom worker supervisor")
    parser.add_argument("action", choices=("start", "stop", "kill", "status"))
    parser.add_argument("--account", required=True)
    parser.add_argument("--market", choices=("KR", "US"), required=True)
    args = parser.parse_args()
    account = args.account.strip()
    if not account:
        parser.error("--account must not be empty")
    if args.action == "status":
        code, payload = 0, status(account)
    elif args.action == "start":
        code, payload = start(account, args.market)
    elif args.action == "stop":
        code, payload = stop(account)
    else:
        code, payload = kill(account)

    if code in (3, 4):
        result_logger = get_logger(account, ROOT / "logs" / f"{account}.log")
        result_payload = json.dumps(payload, ensure_ascii=False)
        message = f"worker supervisor result: exit_code={code} payload={result_payload}"
        if payload.get("reason") == "already-running":
            result_logger.info(message)
        else:
            result_logger.warning(message)
        result_logger.complete()

    print(json.dumps(payload, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
