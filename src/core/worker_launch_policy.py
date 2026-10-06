"""Persistent, operator-controlled launch maintenance for exact US mock scope."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import uuid


class WorkerLaunchPolicyError(RuntimeError):
    """A launch policy is blocked or cannot be established safely."""


def maintenance_scope(account: str, market: str) -> bool:
    return (account, market) == ("us_mock", "US")


def maintenance_path(data_dir: Path) -> Path:
    return Path(data_dir) / "worker_us_mock.maintenance.json"


def _checked_path(path: Path, *, directory: bool = False) -> os.stat_result:
    """Reject symlinks/reparse points before accessing policy or lock files."""
    if not path.is_absolute():
        raise WorkerLaunchPolicyError("maintenance-path-not-absolute")
    for current in (*reversed(path.parents), path):
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise WorkerLaunchPolicyError("maintenance-path-reparse-point")
        if current != path or directory:
            if not stat.S_ISDIR(info.st_mode):
                raise WorkerLaunchPolicyError("maintenance-parent-not-directory")
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise WorkerLaunchPolicyError("maintenance-file-not-single-regular-file")
    return info


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorkerLaunchPolicyError("maintenance-duplicate-key")
        result[key] = value
    return result


def read_maintenance(data_dir: Path) -> dict:
    """Read a required record without creating or repairing runtime state."""
    path = maintenance_path(data_dir)
    try:
        before = _checked_path(path)
        if before.st_size > 16384:
            raise WorkerLaunchPolicyError("maintenance-record-too-large")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_nlink) != (
                before.st_dev, before.st_ino, 1
            ):
                raise WorkerLaunchPolicyError("maintenance-record-changed")
            payload = json.loads(stream.read(16385), object_pairs_hook=_unique_object)
        if not isinstance(payload, dict):
            raise WorkerLaunchPolicyError("maintenance-record-not-object")
        if (type(payload.get("schemaVersion")) is not int
                or payload["schemaVersion"] != 1
                or (payload.get("account"), payload.get("market")) != ("us_mock", "US")
                or payload.get("state") not in ("PAUSED", "RESUMED")):
            raise WorkerLaunchPolicyError("maintenance-record-scope-or-version-invalid")
        generation = payload.get("generation")
        if not isinstance(generation, str) or uuid.UUID(generation).hex != generation:
            raise WorkerLaunchPolicyError("maintenance-generation-invalid")
        updated = payload.get("updatedAtUtc")
        if not isinstance(updated, str):
            raise WorkerLaunchPolicyError("maintenance-timestamp-invalid")
        timestamp = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        if timestamp.tzinfo is None or timestamp.utcoffset() != timezone.utc.utcoffset(timestamp):
            raise WorkerLaunchPolicyError("maintenance-timestamp-not-utc")
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 512:
            raise WorkerLaunchPolicyError("maintenance-reason-invalid")
        return payload
    except WorkerLaunchPolicyError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        raise WorkerLaunchPolicyError(
            f"maintenance-record-unresolved:{type(exc).__name__}"
        ) from exc


def require_launch_resumed(data_dir: Path) -> dict:
    record = read_maintenance(data_dir)
    if record["state"] != "RESUMED":
        raise WorkerLaunchPolicyError("worker-maintenance-paused")
    return record


@contextmanager
def launch_policy_lock(data_dir: Path):
    """Order policy changes and spawning; refuse contention without retry."""
    try:
        _checked_path(Path(data_dir), directory=True)
        if os.name == "nt":
            with _windows_lock():
                yield
        else:
            with _posix_lock(Path(data_dir)):
                yield
    except WorkerLaunchPolicyError:
        raise
    except (OSError, ValueError) as exc:
        raise WorkerLaunchPolicyError(
            f"maintenance-lock-unresolved:{type(exc).__name__}"
        ) from exc


@contextmanager
def _windows_lock():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.ReleaseMutex.argtypes = (ctypes.c_void_p,)
    kernel.ReleaseMutex.restype = ctypes.c_bool
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel.CloseHandle.restype = ctypes.c_bool
    handle = kernel.CreateMutexW(None, False, "Global\\KiwoomAutotradeLaunchPolicy_us_mock")
    if not handle:
        raise WorkerLaunchPolicyError(f"maintenance-lock-create-failed:{ctypes.get_last_error()}")
    acquired = False
    try:
        result = kernel.WaitForSingleObject(handle, 0)
        acquired = result in (0, 0x80)
        if result != 0:
            raise WorkerLaunchPolicyError(f"maintenance-lock-not-confirmed:{result}")
        yield
    finally:
        released = not acquired or bool(kernel.ReleaseMutex(handle))
        closed = bool(kernel.CloseHandle(handle))
        if not released or not closed:
            raise WorkerLaunchPolicyError("maintenance-lock-release-unresolved")


@contextmanager
def _posix_lock(data_dir: Path):
    import fcntl

    path = data_dir / "worker_us_mock.launch-policy.lock"
    try:
        _checked_path(path)
    except FileNotFoundError:
        pass
    descriptor = os.open(
        path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    acquired = False
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise WorkerLaunchPolicyError("maintenance-lock-not-single-regular-file")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        yield
    finally:
        try:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def change_maintenance(
    account: str, market: str, data_dir: Path, *, state: str,
    reason: str, expected_generation: str | None = None,
) -> dict:
    """Explicitly pause/resume with a generation check, never start or stop."""
    if not maintenance_scope(account, market) or state not in ("PAUSED", "RESUMED"):
        raise WorkerLaunchPolicyError("maintenance-action-scope-invalid")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 512:
        raise WorkerLaunchPolicyError("maintenance-reason-invalid")
    with launch_policy_lock(data_dir):
        path = maintenance_path(data_dir)
        try:
            _checked_path(path)
        except FileNotFoundError:
            if state != "PAUSED" or expected_generation is not None:
                raise WorkerLaunchPolicyError("maintenance-initialization-requires-pause") from None
            previous = None
        else:
            previous = read_maintenance(data_dir)
            if expected_generation != previous["generation"]:
                raise WorkerLaunchPolicyError("maintenance-generation-mismatch")
        record = {
            "schemaVersion": 1, "account": account, "market": market, "state": state,
            "generation": uuid.uuid4().hex,
            "updatedAtUtc": datetime.now(timezone.utc).isoformat(), "reason": reason.strip(),
        }
        temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Refuse an observed out-of-band replacement before publication.
        if previous is None:
            try:
                _checked_path(path)
            except FileNotFoundError:
                pass
            else:
                raise WorkerLaunchPolicyError("maintenance-record-appeared-before-publication")
        elif read_maintenance(data_dir)["generation"] != previous["generation"]:
            raise WorkerLaunchPolicyError("maintenance-generation-changed-before-publication")
        # One publication attempt; preserve a failed temporary file as evidence.
        os.replace(temporary, path)
        return {**record, "previousGeneration": previous["generation"] if previous else None}
