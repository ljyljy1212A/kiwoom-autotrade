from __future__ import annotations

import ctypes
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.core.runtime_paths import DATA_DIR

if os.name != "nt":
    import errno
    import fcntl


LOCK_LOG = logging.getLogger(__name__)


class ProcessLockError(RuntimeError):
    pass


@dataclass
class AccountOrderAuthority:
    """Capability to submit orders while owning an account ProcessLock."""

    account_id: str
    lock: "ProcessLock"

    def assert_owned(self) -> None:
        if not self.lock.owned_by_current_process():
            from src.utils.exceptions import OrderAuthorityError
            raise OrderAuthorityError(
                f"Order authority is not owned for account {self.account_id}"
            )


@dataclass
class ProcessLock:
    account_id: str
    base_dir: Path = DATA_DIR

    def __post_init__(self) -> None:
        self.account_id = str(self.account_id)
        self.base_dir = Path(self.base_dir)
        self._handle = None
        self._owner_pid: int | None = None
        self._acquired = False

    @property
    def lock_path(self) -> Path:
        return self.base_dir / f"worker_{self.account_id}.lock"

    @property
    def mutex_name(self) -> str:
        return f"Global\\KiwoomAutotradeWorker_{self.account_id}"

    def acquire(self) -> None:
        if self._acquired:
            return
        if os.name == "nt":
            self._acquire_windows()
        else:
            self._acquire_posix()
        self._acquired = True
        self._owner_pid = os.getpid()

    def release(self) -> None:
        if not self._acquired:
            return
        if os.name == "nt":
            self._release_windows()
        else:
            self._release_posix()
        self._acquired = False
        self._owner_pid = None

    def is_alive(self) -> bool:
        return bool(self.liveness_result()["running"])

    def liveness_result(self) -> dict:
        if self._acquired and self._owner_pid == os.getpid():
            return {"running": True, "liveness": "confirmed", "livenessError": None}
        if os.name == "nt":
            return self._liveness_windows()
        running = self._is_alive_posix()
        return {
            "running": running,
            "liveness": "confirmed" if running else "dead",
            "livenessError": None,
        }

    def owned_by_current_process(self) -> bool:
        """Return whether this lock handle is owned by this process."""
        return bool(self._acquired and self._owner_pid == os.getpid())

    def observe_mutex_owner(self) -> dict:
        """Diagnose Windows mutex ownership in the calling thread.

        This is evidence only. It does not replace the order-authority guard.
        An unavailable native query is reported as incomplete.
        """
        observed_at = datetime.now(timezone.utc).isoformat()
        if os.name != "nt" or not self._acquired or self._handle is None:
            return {"state": "INCOMPLETE", "observedAt": observed_at, "reason": "no_windows_handle"}

        class MutantBasicInformation(ctypes.Structure):
            _fields_ = [
                ("CurrentCount", ctypes.c_long),
                ("OwnedByCaller", ctypes.c_ubyte),
                ("AbandonedState", ctypes.c_ubyte),
            ]

        try:
            ntdll = ctypes.WinDLL("ntdll")
            query = ntdll.NtQueryMutant
            query.argtypes = (
                ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
            )
            query.restype = ctypes.c_long
            kernel32 = ctypes.WinDLL("kernel32")
            kernel32.GetCurrentThreadId.restype = ctypes.c_uint32
            info = MutantBasicInformation()
            returned_length = ctypes.c_ulong()
            status = query(
                self._handle, 0, ctypes.byref(info), ctypes.sizeof(info),
                ctypes.byref(returned_length),
            )
            if status != 0 or returned_length.value < ctypes.sizeof(info):
                return {
                    "state": "INCOMPLETE", "observedAt": observed_at,
                    "reason": "native_query_failed", "ntstatus": int(status),
                }
            caller_thread = int(kernel32.GetCurrentThreadId())
        except (AttributeError, OSError, ValueError) as exc:
            return {
                "state": "INCOMPLETE", "observedAt": observed_at,
                "reason": type(exc).__name__,
            }

        owned_by_caller = bool(info.OwnedByCaller)
        consistent = not bool(info.AbandonedState) and info.CurrentCount <= 0
        confirmed = owned_by_caller and consistent and self._owner_pid == os.getpid()
        state = "CONFIRMED" if confirmed else "NOT_OWNED" if not owned_by_caller else "INCOMPLETE"
        result = {
            "state": state, "observedAt": observed_at,
            "currentCount": int(info.CurrentCount),
            "abandoned": bool(info.AbandonedState),
            "callerThreadId": caller_thread,
        }
        if confirmed:
            result["account"] = self.account_id
            result["ownerPid"] = os.getpid()
            result["ownerThreadId"] = caller_thread
        return result

    def _acquire_windows(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

        class SecurityAttributes(ctypes.Structure):
            _fields_ = [
                ("nLength", ctypes.c_uint32),
                ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", ctypes.c_int),
            ]

        convert_sddl = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
        convert_sddl.argtypes = (
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_uint32),
        )
        convert_sddl.restype = ctypes.c_bool
        local_free = kernel32.LocalFree
        local_free.argtypes = (ctypes.c_void_p,)
        local_free.restype = ctypes.c_void_p
        kernel32.OpenMutexW.argtypes = (ctypes.c_uint32, ctypes.c_bool, ctypes.c_wchar_p)
        kernel32.OpenMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = (
            ctypes.POINTER(SecurityAttributes),
            ctypes.c_bool,
            ctypes.c_wchar_p,
        )
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_bool

        # Keep mutex access available to the authenticated task/supervisor
        # principals while retaining mutex ownership as the authority to
        # release it.  The explicit ACL avoids the default descriptor mismatch
        # seen between a scheduled worker and its later supervisor probes.
        descriptor = ctypes.c_void_p()
        descriptor_size = ctypes.c_uint32()
        sddl = "D:P(A;;0x100001;;;OW)(A;;0x100001;;;AU)(A;;0x100001;;;SY)"
        if not convert_sddl(sddl, 1, ctypes.byref(descriptor), ctypes.byref(descriptor_size)):
            error = ctypes.get_last_error()
            raise ProcessLockError(
                f"Worker launch refused: could not create account mutex security descriptor for {self.account_id} (winerror={error})."
            )
        attributes = SecurityAttributes(
            ctypes.sizeof(SecurityAttributes), descriptor, False
        )
        try:
            existing = kernel32.OpenMutexW(0x00100001, False, self.mutex_name)
            if existing:
                kernel32.CloseHandle(existing)
                raise ProcessLockError(
                    f"Worker launch refused: {self.account_id} is already running."
                )
            if ctypes.get_last_error() != 2:
                error = ctypes.get_last_error()
                raise ProcessLockError(
                    f"Worker launch refused: could not inspect account mutex for {self.account_id} (winerror={error})."
                )
            handle = kernel32.CreateMutexW(ctypes.byref(attributes), True, self.mutex_name)
        finally:
            local_free(descriptor)
        if not handle:
            raise ProcessLockError(f"Worker launch refused: could not create account mutex for {self.account_id}.")
        if ctypes.get_last_error() == 183:
            kernel32.CloseHandle(handle)
            raise ProcessLockError(f"Worker launch refused: {self.account_id} is already running.")
        self._handle = handle

    def _release_windows(self) -> None:
        if self._handle is None:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.ReleaseMutex.argtypes = (ctypes.c_void_p,)
        kernel32.ReleaseMutex.restype = ctypes.c_bool
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_bool
        try:
            kernel32.ReleaseMutex(self._handle)
        finally:
            kernel32.CloseHandle(self._handle)
            self._handle = None

    def _liveness_windows(self) -> dict:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenMutexW.argtypes = (ctypes.c_uint32, ctypes.c_bool, ctypes.c_wchar_p)
        kernel32.OpenMutexW.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.ReleaseMutex.argtypes = (ctypes.c_void_p,)
        kernel32.ReleaseMutex.restype = ctypes.c_bool
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_bool
        handle = kernel32.OpenMutexW(0x00100000, False, self.mutex_name)
        if not handle:
            error = ctypes.get_last_error()
            classification = "dead" if error == 2 else "suspect"
            result = {
                "running": classification == "suspect",
                "liveness": classification,
                "livenessError": error,
            }
            LOCK_LOG.warning(
                "ProcessLock liveness account=%s mutex=%s operation=OpenMutexW "
                "winerror=%s classification=%s",
                self.account_id, self.mutex_name, error, classification,
            )
            return result
        try:
            state = kernel32.WaitForSingleObject(handle, 0)
            if state == 258:
                return {"running": True, "liveness": "confirmed", "livenessError": None}
            if state in (0, 0x80):
                kernel32.ReleaseMutex(handle)
            error = ctypes.get_last_error()
            LOCK_LOG.warning(
                "ProcessLock liveness account=%s mutex=%s operation=WaitForSingleObject "
                "winerror=%s classification=suspect",
                self.account_id, self.mutex_name, error,
            )
            return {"running": True, "liveness": "suspect", "livenessError": error}
        finally:
            kernel32.CloseHandle(handle)

    def _is_alive_windows(self) -> bool:
        return bool(self._liveness_windows()["running"])

    def _acquire_posix(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT)
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise ProcessLockError(f"Worker launch refused: {self.account_id} is already running.") from exc
            raise
        self._handle = fd

    def _release_posix(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.lockf(self._handle, fcntl.LOCK_UN)
        finally:
            os.close(self._handle)
            self._handle = None

    def _is_alive_posix(self) -> bool:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT)
        try:
            try:
                fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    return True
                raise
            fcntl.lockf(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)
