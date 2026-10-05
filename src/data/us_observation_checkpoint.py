"""Pinned external observation checkpoints with a cooperative single-writer lock.

Checkpoint and lock files must be separately prepared; neither is created here.
Replacement is attempted once. Failed temporary files remain for inspection.
File fsync is used, but Windows directory/power-loss durability is not claimed.
The lock coordinates cooperating callers only; it is not an authentication or
protection boundary against an actor that can replace files in the directory.
"""
from __future__ import annotations

import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from src.data.us_operational_observation_store import (
    POLICY, OperationalObservationHead, _identifier,
)

CHECKPOINT_VERSION = 1
LOCK_CONTENT = b"us-mock-observation-checkpoint-lock-v1\n"


def _regular(path):
    if not path.is_absolute():
        raise ValueError("An explicit absolute file path is required")
    for component in (path, *path.parents):
        info = component.lstat()
        if component.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Checkpoint paths must not contain reparse points")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("A separately prepared regular file without hard links is required")
    return info


def _head(head, journal_id, binding_id):
    if (type(head) is not OperationalObservationHead
            or (head.journal_id, head.binding_id) != (journal_id, binding_id)
            or type(head.sequence) is not int or head.sequence < 0
            or not isinstance(head.digest, str) or not re.fullmatch(r"[0-9a-f]{64}", head.digest)
            or (head.sequence == 0) != (head.digest == "0" * 64)):
        raise ValueError("Checkpoint must contain one valid pinned observation head")
    return head


def checkpoint_bytes(head):
    """Encoding contract for separately authorized initial file preparation."""
    _identifier(head.journal_id)
    _identifier(head.binding_id)
    _head(head, head.journal_id, head.binding_id)
    payload = {"version": CHECKPOINT_VERSION, "policy": POLICY,
               "account_id": "us_mock", "market": "US", "head": asdict(head)}
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


class ObservationCheckpointFile:
    """No mkdir, initialization, repair, retries, or fallback storage.

    All reads and advances require exclusive(). The supplied lock file must be
    unique to this checkpoint and shared by every cooperating writer. Its name
    is fixed to the checkpoint filename plus '.lock'.
    """

    def __init__(self, *, path, journal_id, binding_id):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.journal_id, self.binding_id = _identifier(journal_id), _identifier(binding_id)
        self._lease_fd = None
        _regular(self.path)
        _regular(self.lock_path)

    def _lease(self):
        if self._lease_fd is None:
            raise ValueError("An exclusive checkpoint lease is required")
        info = _regular(self.lock_path)
        opened = os.fstat(self._lease_fd)
        if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("Checkpoint lock file identity changed")

    @contextmanager
    def exclusive(self):
        if self._lease_fd is not None:
            raise ValueError("Checkpoint lease is not reentrant")
        _regular(self.lock_path)
        descriptor = os.open(self.lock_path, os.O_RDWR | getattr(os, "O_BINARY", 0))
        acquired = False
        try:
            if os.fstat(descriptor).st_size != len(LOCK_CONTENT):
                raise ValueError("Separately prepared checkpoint lock content is required")
            if os.read(descriptor, len(LOCK_CONTENT)) != LOCK_CONTENT:
                raise ValueError("Unexpected checkpoint lock policy")
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            self._lease_fd = descriptor
            self._lease()
            yield self
        finally:
            try:
                if acquired:
                    if os.name == "nt":
                        import msvcrt
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                if acquired:
                    self._lease_fd = None
                os.close(descriptor)

    def read(self):
        self._lease()
        info = _regular(self.path)
        if info.st_size > 4096:
            raise ValueError("Checkpoint exceeds its fixed size limit")
        with self.path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError("Checkpoint file changed during open")
            encoded = stream.read(4097)
        if len(encoded) > 4096:
            raise ValueError("Checkpoint exceeds its fixed size limit")
        payload = json.loads(encoded.decode("utf-8", errors="strict"))
        if (type(payload) is not dict
                or set(payload) != {"version", "policy", "account_id", "market", "head"}
                or type(payload["version"]) is not int or payload["version"] != CHECKPOINT_VERSION
                or (payload["policy"], payload["account_id"], payload["market"]) != (POLICY, "us_mock", "US")
                or type(payload["head"]) is not dict):
            raise ValueError("Checkpoint metadata is invalid")
        head = _head(OperationalObservationHead(**payload["head"]), self.journal_id, self.binding_id)
        if checkpoint_bytes(head) != encoded:
            raise ValueError("Checkpoint must use the exact canonical UTF-8 encoding")
        return head

    def advance(self, *, previous, current):
        self._lease()
        _head(previous, self.journal_id, self.binding_id)
        _head(current, self.journal_id, self.binding_id)
        if self.read() != previous:
            raise ValueError("Checkpoint no longer matches the expected previous head")
        if current == previous:
            return  # An exact duplicate requires no file replacement.
        if current.sequence != previous.sequence + 1:
            raise ValueError("Checkpoint may advance by exactly one committed cycle")
        temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".pending")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
        # No cleanup on failure: the attempted file is retained as evidence.
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(checkpoint_bytes(current))
            stream.flush()
            os.fsync(stream.fileno())
        self._lease()
        if self.read() != previous:
            raise ValueError("Checkpoint changed before replacement")
        _regular(temporary)
        os.replace(temporary, self.path)
        if self.read() != current:
            raise ValueError("Checkpoint replacement could not be verified")
