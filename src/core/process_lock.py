from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from pathlib import Path

from src.core.runtime_paths import DATA_DIR

if os.name != "nt":
    import errno
    import fcntl


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
        return f"Local\\KiwoomAutotradeWorker_{self.account_id}"

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
        if self._acquired and self._owner_pid == os.getpid():
            return True
        if os.name == "nt":
            return self._is_alive_windows()
        return self._is_alive_posix()

    def owned_by_current_process(self) -> bool:
        """Return whether this lock handle is owned by this process."""
        return bool(self._acquired and self._owner_pid == os.getpid())

    def _acquire_windows(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_bool
        handle = kernel32.CreateMutexW(None, True, self.mutex_name)
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

    def _is_alive_windows(self) -> bool:
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
            return False
        try:
            state = kernel32.WaitForSingleObject(handle, 0)
            if state == 258:
                return True
            if state == 0:
                kernel32.ReleaseMutex(handle)
            return False
        finally:
            kernel32.CloseHandle(handle)

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
