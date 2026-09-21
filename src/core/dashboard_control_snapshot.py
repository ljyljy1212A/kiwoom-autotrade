"""Mock-only snapshot candidate. Initialization requires an explicit baseline.

Every writer holds an account file lock across read/validate/modify/replace.
The persistent lock file is never deleted or reclaimed based on its age.
Readers see one complete old or new snapshot and never consult legacy files.
Path checks refuse observed reparse points; they are not a defense against an
adversary replacing directory entries between checks and filesystem operations.
"""
from __future__ import annotations

import copy
import json
import os
import re
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from src.core.atomic_write import atomic_write_json

if os.name == "nt":
    import msvcrt
else:
    import fcntl

MOCK_ACCOUNTS = {"kr_mock": "KR", "us_mock": "US"}
_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9.-]{0,11}")
_THREAD_LOCK = threading.RLock()
_INSTANCE = re.compile(r"[0-9a-f]{32}")


class InvalidSnapshot(RuntimeError):
    """Persisted authority is missing, malformed, or unsafe."""


@dataclass
class ControlAuthority:
    """One object per worker lifetime, shared by every engine recreation."""
    account: str
    instance_id: str
    retirement_pending: set[str] = field(default_factory=set)


def valid_instance(value: object) -> bool:
    return isinstance(value, str) and _INSTANCE.fullmatch(value) is not None


def validate_authority(authority: object, account: str) -> ControlAuthority:
    if (not isinstance(authority, ControlAuthority) or authority.account != account
            or account not in MOCK_ACCOUNTS or not valid_instance(authority.instance_id)):
        raise InvalidSnapshot("Current worker authority unavailable")
    return authority


def require_authority(authority: object, account: str, control: dict) -> None:
    checked = validate_authority(authority, account)
    if (control.get("instance_id") != checked.instance_id
            or control.get("symbol") in checked.retirement_pending):
        raise InvalidSnapshot("Control belongs to an old instance or pending retirement")


def _safe_path(path: Path, *, leaf_missing: bool = True) -> None:
    absolute = path.absolute()
    for entry in (*reversed(absolute.parents), absolute):
        try:
            info = entry.lstat()
        except FileNotFoundError:
            if entry == absolute and leaf_missing:
                continue
            raise InvalidSnapshot(f"Missing parent: {entry}")
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise InvalidSnapshot(f"Reparse point refused: {entry}")
        if entry != absolute and not stat.S_ISDIR(info.st_mode):
            raise InvalidSnapshot(f"Parent is not a directory: {entry}")
        if entry == absolute and not stat.S_ISREG(info.st_mode):
            raise InvalidSnapshot(f"Snapshot/lock is not a regular file: {entry}")


def path_for(data_dir: Path, account: str) -> Path:
    if account not in MOCK_ACCOUNTS:
        raise InvalidSnapshot("Unsupported account")
    return Path(data_dir) / f"dashboard_control_snapshot_{account}.json"


def _validate_control(control: object, account: str) -> dict:
    if not isinstance(control, dict):
        raise ValueError("Control must be an object")
    symbol = control.get("symbol")
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise ValueError("Invalid control symbol")
    if any(type(control.get(key)) is not bool for key in ("auto_buy", "auto_sell")):
        raise ValueError("Control flags must be booleans")
    instance = control.get("instance_id")
    if not valid_instance(instance) and (instance is not None or control["auto_buy"] or control["auto_sell"]):
        raise ValueError("Enabled controls require a valid instance ID")
    config = control.get("config")
    if not isinstance(config, dict) or config.get("symbol") != symbol:
        raise ValueError("Config identity mismatch")
    if config.get("market") != MOCK_ACCOUNTS[account] or config.get("mode") != "mock":
        raise ValueError("Mock market/mode mismatch")
    return copy.deepcopy(control)


def validate(payload: object, account: str) -> dict:
    if account not in MOCK_ACCOUNTS or not isinstance(payload, dict):
        raise InvalidSnapshot("Invalid snapshot account/object")
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 2:
        raise InvalidSnapshot("Unsupported schema")
    if payload.get("account") != account:
        raise InvalidSnapshot("Account mismatch")
    if type(payload.get("revision")) is not int or payload["revision"] < 0:
        raise InvalidSnapshot("Invalid revision")
    controls = payload.get("controls")
    if not isinstance(controls, dict):
        raise InvalidSnapshot("Controls must be a map")
    for symbol, control in controls.items():
        try:
            checked = _validate_control(control, account)
        except ValueError as exc:
            raise InvalidSnapshot(str(exc)) from exc
        if checked["symbol"] != symbol:
            raise InvalidSnapshot("Control key mismatch")
    selected = payload.get("selected_symbol")
    if selected is not None and (not isinstance(selected, str) or selected not in controls):
        raise InvalidSnapshot("Selected control missing")
    return copy.deepcopy(payload)


def load(data_dir: Path, account: str) -> dict:
    path = path_for(data_dir, account)
    _safe_path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise InvalidSnapshot("Malformed snapshot") from exc
    return validate(payload, account)


@contextmanager
def _writer(data_dir: Path, account: str):
    path = path_for(data_dir, account)
    lock_path = path.with_suffix(".lock")
    with _THREAD_LOCK:
        _safe_path(path)
        _safe_path(lock_path)
        with lock_path.open("a+b") as lock:
            # Lock byte zero on Windows; extra initializer bytes are harmless.
            if os.name == "nt":
                if lock.seek(0, os.SEEK_END) == 0:
                    lock.write(b"\0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                _safe_path(path)
                _safe_path(lock_path, leaf_missing=False)
                yield
            finally:
                if os.name == "nt":
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def initialize(data_dir: Path, account: str, controls: dict, selected: str | None) -> dict:
    """Explicit baseline only; no legacy import, overwrite, or auto-enable."""
    if selected is not None:
        raise InvalidSnapshot("Initial baseline must have no selected symbol")
    if not isinstance(controls, dict):
        raise InvalidSnapshot("Baseline controls must be a map")
    for control in controls.values():
        if (not isinstance(control, dict) or control.get("auto_buy") is not False
                or control.get("auto_sell") is not False or control.get("instance_id") is not None):
            raise InvalidSnapshot("Initial baseline must be disabled and unbound")
    with _writer(data_dir, account):
        path = path_for(data_dir, account)
        try:
            path.lstat()
        except FileNotFoundError:
            pass
        else:
            raise InvalidSnapshot("Snapshot already exists")
        payload = validate({"schema_version": 2, "account": account, "revision": 0,
                            "controls": controls, "selected_symbol": selected}, account)
        atomic_write_json(path, payload)
        return payload


def update(data_dir: Path, account: str, control: dict) -> dict:
    path_for(data_dir, account)
    checked = _validate_control(control, account)
    if not valid_instance(checked.get("instance_id")):
        raise ValueError("Explicit update requires a current instance ID")
    with _writer(data_dir, account):
        payload = load(data_dir, account)
        payload["controls"][checked["symbol"]] = checked
        payload["selected_symbol"] = checked["symbol"]
        payload["revision"] += 1
        atomic_write_json(path_for(data_dir, account), validate(payload, account))
        return payload


def remove(data_dir: Path, account: str, symbol: str) -> None:
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise ValueError("Invalid symbol")
    with _writer(data_dir, account):
        payload = load(data_dir, account)
        if symbol not in payload["controls"]:
            return
        del payload["controls"][symbol]
        if payload["selected_symbol"] == symbol:
            payload["selected_symbol"] = None
        payload["revision"] += 1
        atomic_write_json(path_for(data_dir, account), validate(payload, account))


def read_control(data_dir: Path, account: str, symbol: str | None = None) -> dict:
    payload = load(data_dir, account)
    selected = payload["selected_symbol"] if symbol is None else symbol
    if not isinstance(selected, str) or selected not in payload["controls"]:
        raise InvalidSnapshot("Requested control missing")
    return payload["controls"][selected]
