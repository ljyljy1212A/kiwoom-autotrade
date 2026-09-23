"""Broker-authoritative unattended cleanup for closed symbol runtime state.

This module deliberately never deletes accounting history.  It only retires
re-creatable automation state after two complete broker snapshots confirm that
a symbol has no position and no unresolved order attribution.
"""
from __future__ import annotations

from src.core.atomic_write import atomic_write_json

import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src.core.runtime_paths import DATA_DIR
from src.core.symbol_keys import canonical_symbol_key, legacy_symbol_key


_FILE_LOCKS: dict[str, threading.RLock] = {}
_FILE_LOCKS_GUARD = threading.Lock()
_FILE_LOCK_DEPTH = threading.local()


@contextmanager
def _account_file_lock(path: Path):
    """Serialize account cleanup state and target writes across processes."""
    key = str(path.resolve())
    with _FILE_LOCKS_GUARD:
        thread_lock = _FILE_LOCKS.setdefault(key, threading.RLock())
    with thread_lock:
        depths = getattr(_FILE_LOCK_DEPTH, "values", None)
        if depths is None:
            depths = {}
            _FILE_LOCK_DEPTH.values = depths
        if depths.get(key, 0):
            depths[key] += 1
            try:
                yield
            finally:
                depths[key] -= 1
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(0.05)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            depths[key] = 1
            try:
                yield
            finally:
                depths.pop(key, None)
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def account_cleanup_lock(data_dir: Path, account_id: str):
    """Share the cleanup target lock with every account-state writer."""
    return _account_file_lock(Path(data_dir) / f"orphan_cleanup_{account_id}.lock")


class OrphanStateCleaner:
    """Evaluate and safely retire stale per-symbol runtime state.

    ``apply`` is intentionally automatic once the caller supplies two
    consecutive complete zero-quantity snapshots.  The caller must provide a
    broker-authoritative quantity map; this class never guesses from a ledger.
    """

    def __init__(self, account_id: str, data_dir: Path = DATA_DIR, logger=None, market: str = "KR"):
        self.account_id = account_id
        self.data_dir = data_dir
        self.logger = logger
        self.market = str(market).upper()
        self.bases_path = data_dir / f"tranche_bases_{account_id}.json"
        self.lifecycle_path = data_dir / f"symbol_lifecycles_{account_id}.json"
        self.settings_path = data_dir / f"dashboard_settings_{account_id}.json"
        self.state_path = data_dir / f"orphan_cleanup_{account_id}.json"
        self.lock_path = data_dir / f"orphan_cleanup_{account_id}.lock"
        self.audit_path = data_dir / "audit" / f"orphan_cleanup_{account_id}.jsonl"
        self.migration_path = data_dir / f"symbol_key_migration_{account_id}.json"
        self.migration_audit_path = data_dir / "audit" / f"symbol_key_migration_{account_id}.jsonl"
        migration = self._read(self.migration_path, {})
        self._manual_review = set(migration.get("manualReview", [])) if isinstance(migration, dict) else set()

    def _symbol(self, value: object) -> str:
        return canonical_symbol_key(self.market, value)

    @staticmethod
    def _read(path: Path, default):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, type(default)) else default
        except (OSError, json.JSONDecodeError):
            return default

    @staticmethod
    def _write_atomic(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, value, ensure_ascii=False)

    @staticmethod
    def _identity(value: object) -> str:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _file_identity(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _target_path(self, value: str) -> Path:
        path = Path(value).resolve()
        root = self.data_dir.resolve()
        if os.path.commonpath((str(root), str(path))) != str(root):
            raise ValueError(f"cleanup target escapes account data directory: {value}")
        return path

    def _initial_state(self) -> dict:
        return {
            "version": 2,
            "zeroConfirmations": {},
            "lastProcessedGenerations": {},
            "qualifyingGenerations": {},
            "pendingCleanup": {},
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }

    def _read_state(self) -> dict:
        if not self.state_path.exists():
            state = self._initial_state()
            self._write_atomic(self.state_path, state)
            return state
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("orphan cleanup state is unreadable") from exc
        if not isinstance(state, dict):
            raise ValueError("orphan cleanup state is malformed")
        version = state.get("version")
        if version is None:
            state = {
                **self._initial_state(),
                **state,
                "version": 2,
                "lastProcessedGenerations": {},
                "qualifyingGenerations": {},
                "pendingCleanup": {},
            }
        elif version != 2:
            raise ValueError(f"unsupported orphan cleanup state version: {version}")
        for field in (
            "zeroConfirmations", "lastProcessedGenerations",
            "qualifyingGenerations", "pendingCleanup",
        ):
            if not isinstance(state.get(field), dict):
                raise ValueError(f"orphan cleanup state field is malformed: {field}")
        return state

    def _write_state(self, state: dict) -> None:
        state["version"] = 2
        state["updatedAt"] = datetime.now(timezone.utc).isoformat()
        self._write_atomic(self.state_path, state)

    def has_pending_cleanup(self) -> bool:
        with _account_file_lock(self.lock_path):
            try:
                return bool(self._read_state()["pendingCleanup"])
            except ValueError:
                return True

    def _controls(self) -> dict[str, list[Path]]:
        prefix = f"dashboard_control_{self.account_id}_"
        controls: dict[str, list[Path]] = {}
        for path in self.data_dir.glob(f"{prefix}*.json"):
            symbol = self._symbol(path.stem[len(prefix):])
            if symbol:
                controls.setdefault(symbol, []).append(path)
        return controls

    def _symbols(self) -> set[str]:
        bases = self._read(self.bases_path, {})
        lifecycles = self._read(self.lifecycle_path, {})
        settings = self._read(self.settings_path, {})
        symbols = {self._symbol(key) for key in bases} | {self._symbol(key) for key in lifecycles} | set(self._controls())
        for profile in settings.get("profiles", []) if isinstance(settings, dict) else []:
            if isinstance(profile, dict):
                symbols.add(self._symbol((profile.get("config") or {}).get("symbol")))
        return {symbol for symbol in symbols if symbol}

    def evaluate(self, symbol: str, broker_qty: float, balance_complete: bool,
                 has_unresolved_orders: Callable[[str], bool]) -> dict:
        symbol = self._symbol(symbol)
        bases = self._read(self.bases_path, {})
        lifecycles = self._read(self.lifecycle_path, {})
        controls = self._controls().get(symbol, [])
        lifecycle = lifecycles.get(symbol, {})
        has_open_lifecycle = isinstance(lifecycle, dict) and lifecycle.get("status") in {"open", "pending"}
        state_exists = symbol in bases or bool(controls) or has_open_lifecycle
        unresolved = bool(has_unresolved_orders(symbol))
        if not balance_complete:
            classification = "blocked_incomplete_balance"
        elif broker_qty > 1e-9:
            classification = "manual_review_required" if unresolved else "protected_nonzero_holding"
        elif unresolved:
            classification = "manual_review_required"
        elif state_exists:
            classification = "orphan_candidate"
        else:
            classification = "clean"
        return {
            "symbol": symbol, "brokerQty": float(broker_qty), "balanceComplete": bool(balance_complete),
            "baseEntry": symbol in bases, "controlFiles": [str(path) for path in controls],
            "openLifecycle": has_open_lifecycle, "unresolvedOrders": unresolved,
            "classification": classification,
        }

    def manual_review_symbols(self) -> frozenset[str]:
        return frozenset(self._manual_review)

    def migrate_legacy_keys(self, candidates: set[str]) -> frozenset[str]:
        with _account_file_lock(self.lock_path):
            return self._migrate_legacy_keys(candidates)

    def _migrate_legacy_keys(self, candidates: set[str]) -> frozenset[str]:
        """Atomically rekey uniquely attributable legacy state; retain ambiguity."""
        canonical_candidates = {self._symbol(symbol) for symbol in candidates if symbol}
        owners: dict[str, set[str]] = {}
        for symbol in canonical_candidates:
            owners.setdefault(legacy_symbol_key(symbol), set()).add(symbol)
        stores = (self.bases_path, self.lifecycle_path, self.state_path)
        events: list[dict] = []
        for path in stores:
            data = self._read(path, {})
            if not isinstance(data, dict):
                continue
            if path == self.state_path:
                data = dict(data)
                zeroes = data.get("zeroConfirmations", {})
                if not isinstance(zeroes, dict):
                    continue
                target = dict(zeroes)
            else:
                target = dict(data)
            for key in list(target):
                candidate_owners = owners.get(key, set())
                if len(candidate_owners) == 1:
                    replacement = next(iter(candidate_owners))
                    if replacement == key:
                        continue
                    if replacement in target:
                        self._manual_review.update({key, replacement})
                        events.append({"status": "manual_review", "key": key, "owners": sorted({key, replacement})})
                        continue
                    target[replacement] = target.pop(key)
                    events.append({"status": "migrated", "oldKey": key, "newKey": replacement, "store": path.name})
                elif len(candidate_owners) > 1:
                    self._manual_review.update(candidate_owners | {key})
                    events.append({"status": "manual_review", "key": key, "owners": sorted(candidate_owners)})
                elif key != self._symbol(key):
                    self._manual_review.add(key)
                    events.append({"status": "manual_review", "key": key, "owners": []})
            if path == self.state_path:
                data["zeroConfirmations"] = target
                for field in ("lastProcessedGenerations", "qualifyingGenerations", "pendingCleanup"):
                    values = data.get(field, {})
                    if not isinstance(values, dict):
                        continue
                    migrated = dict(values)
                    for key in list(migrated):
                        candidate_owners = owners.get(key, set())
                        if len(candidate_owners) == 1:
                            replacement = next(iter(candidate_owners))
                            if replacement != key and replacement not in migrated:
                                migrated[replacement] = migrated.pop(key)
                    data[field] = migrated
                target = data
            if target != self._read(path, {}):
                self._write_atomic(path, target)
        control_prefix = f"dashboard_control_{self.account_id}_"
        for path in self.data_dir.glob(f"{control_prefix}*.json"):
            legacy_key = path.stem[len(control_prefix):]
            candidate_owners = owners.get(legacy_key, set())
            if len(candidate_owners) != 1:
                if len(candidate_owners) > 1:
                    self._manual_review.update(candidate_owners | {legacy_key})
                    events.append({"status": "manual_review", "key": legacy_key, "owners": sorted(candidate_owners)})
                continue
            replacement = next(iter(candidate_owners))
            if replacement == legacy_key:
                continue
            destination = self.data_dir / f"{control_prefix}{replacement}.json"
            if destination.exists():
                self._manual_review.update({legacy_key, replacement})
                events.append({"status": "manual_review", "key": legacy_key, "owners": sorted({legacy_key, replacement})})
                continue
            control = self._read(path, {})
            if isinstance(control, dict):
                control["symbol"] = replacement
                self._write_atomic(destination, control)
                path.unlink()
                events.append({"status": "migrated", "oldKey": legacy_key, "newKey": replacement, "store": path.name})
        migration_state = {"manualReview": sorted(self._manual_review), "updatedAt": datetime.now(timezone.utc).isoformat()}
        self._write_atomic(self.migration_path, migration_state)
        for event in events:
            self._migration_audit(event)
        return frozenset(self._manual_review)

    @staticmethod
    def _read_json_object(path: Path, *, missing: dict | None = None) -> dict:
        if not path.exists():
            if missing is None:
                raise ValueError(f"required cleanup target is missing: {path}")
            return dict(missing)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cleanup target is unreadable: {path}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"cleanup target is malformed: {path}")
        return value

    def _build_intent(self, symbol: str, generations: list[str]) -> dict:
        created_at = datetime.now(timezone.utc).isoformat()
        bases = self._read_json_object(self.bases_path, missing={})
        lifecycles = self._read_json_object(self.lifecycle_path, missing={})
        current_lifecycle = lifecycles.get(symbol)
        final_lifecycle = {
            "status": "closed",
            "started_at": current_lifecycle.get("started_at") if isinstance(current_lifecycle, dict) else None,
            "closed_at": created_at,
            "reason": "automatic_orphan_cleanup",
        }
        controls = []
        archive_root = (
            self.data_dir / "archive" / "orphan_cleanup" / self.account_id
            / created_at[:10].replace("-", "")
        )
        for source in self._controls().get(symbol, []):
            digest = hashlib.sha256(
                f"{self.account_id}|{symbol}|{source.name}|{'|'.join(generations)}".encode("utf-8")
            ).hexdigest()[:12]
            destination = archive_root / f"{source.stem}_{digest}{source.suffix}"
            controls.append({
                "source": str(source),
                "destination": str(destination),
                "identity": self._file_identity(source),
            })
        settings = self._read_json_object(self.settings_path, missing={})
        profiles = settings.get("profiles", [])
        if not isinstance(profiles, list) or any(not isinstance(item, dict) for item in profiles):
            raise ValueError("dashboard settings profiles are malformed")
        retained = [
            profile for profile in profiles
            if self._symbol((profile.get("config") or {}).get("symbol")) != symbol
        ]
        settings_target = None
        if len(retained) != len(profiles):
            final_settings = dict(settings)
            final_settings["profiles"] = retained
            settings_target = {
                "path": str(self.settings_path),
                "expectedIdentity": self._identity(settings),
                "final": final_settings,
                "finalIdentity": self._identity(final_settings),
            }
        account_control_path = self.data_dir / f"dashboard_control_{self.account_id}.json"
        account_control = self._read_json_object(account_control_path, missing={})
        account_control_target = None
        if self._symbol(account_control.get("symbol")) == symbol:
            final_control = {"symbol": "", "auto_buy": False, "auto_sell": False}
            account_control_target = {
                "path": str(account_control_path),
                "expectedIdentity": self._identity(account_control),
                "final": final_control,
                "finalIdentity": self._identity(final_control),
            }
        return {
            "version": 1,
            "account": self.account_id,
            "market": self.market,
            "symbol": symbol,
            "createdAt": created_at,
            "qualifyingBalanceGenerations": list(generations),
            "closedAt": created_at,
            "plannedSteps": [
                "tranche_base", "lifecycle", "controls", "dashboard_profiles", "account_control",
            ],
            "base": {
                "expectedPresent": symbol in bases,
                "expectedIdentity": self._identity(bases[symbol]) if symbol in bases else None,
            },
            "lifecycle": {
                "expectedPresent": symbol in lifecycles,
                "expectedIdentity": self._identity(current_lifecycle) if symbol in lifecycles else None,
                "final": final_lifecycle,
                "finalIdentity": self._identity(final_lifecycle),
            },
            "controls": controls,
            "dashboardSettings": settings_target,
            "accountControl": account_control_target,
        }

    def _apply_mapping_target(
        self, path: Path, symbol: str, target: dict, *, remove: bool = False,
    ) -> tuple[bool, str | None]:
        values = self._read_json_object(path, missing={})
        present = symbol in values
        if remove:
            if not present:
                return True, None
            if not target["expectedPresent"] or self._identity(values[symbol]) != target["expectedIdentity"]:
                return False, f"changed target: {path.name}"
            values.pop(symbol)
            self._write_atomic(path, values)
            return True, "tranche_base"
        final = target["final"]
        if present and self._identity(values[symbol]) == target["finalIdentity"]:
            return True, None
        if present != bool(target["expectedPresent"]):
            return False, f"changed target: {path.name}"
        if present and self._identity(values[symbol]) != target["expectedIdentity"]:
            return False, f"changed target: {path.name}"
        values[symbol] = final
        self._write_atomic(path, values)
        return True, "lifecycle_closed"

    def _apply_whole_file_target(self, target: dict | None, label: str) -> tuple[bool, str | None]:
        if target is None:
            return True, None
        path = Path(target["path"])
        current = self._read_json_object(path)
        identity = self._identity(current)
        if identity == target["finalIdentity"]:
            return True, None
        if identity != target["expectedIdentity"]:
            return False, f"changed target: {path.name}"
        self._write_atomic(path, target["final"])
        return True, label

    def _preflight_intent(self, intent: dict) -> str | None:
        """Validate every expected target before the first destructive write."""
        if not isinstance(intent, dict) or intent.get("version") != 1:
            return "unsupported or malformed cleanup intent"
        if intent.get("account") != self.account_id or intent.get("market") != self.market:
            return "mismatched cleanup intent identity"
        symbol = self._symbol(intent.get("symbol"))
        if not symbol or symbol != intent.get("symbol") or not isinstance(intent.get("createdAt"), str):
            return "mismatched cleanup symbol"
        if not isinstance(intent.get("qualifyingBalanceGenerations"), list) or len(
            intent["qualifyingBalanceGenerations"]
        ) != 2 or len(set(intent["qualifyingBalanceGenerations"])) != 2:
            return "cleanup intent has invalid qualifying generations"
        if any(not isinstance(item, str) or not item for item in intent["qualifyingBalanceGenerations"]):
            return "cleanup intent has invalid qualifying generations"
        if not isinstance(intent.get("plannedSteps"), list) or not isinstance(intent.get("controls"), list):
            return "cleanup intent planned steps are malformed"
        try:
            bases = self._read_json_object(self.bases_path, missing={})
            lifecycles = self._read_json_object(self.lifecycle_path, missing={})
            base = intent["base"]
            lifecycle = intent["lifecycle"]
            if not isinstance(base, dict) or not isinstance(lifecycle, dict):
                return "cleanup intent target schema is malformed"
            if not isinstance(base.get("expectedPresent"), bool) or (
                base.get("expectedPresent") and not isinstance(base.get("expectedIdentity"), str)
            ):
                return "cleanup intent base target is malformed"
            if not isinstance(lifecycle.get("expectedPresent"), bool) or (
                lifecycle.get("expectedPresent") and not isinstance(lifecycle.get("expectedIdentity"), str)
            ):
                return "cleanup intent lifecycle target is malformed"
            if not isinstance(lifecycle.get("final"), dict) or self._identity(
                lifecycle["final"]
            ) != lifecycle.get("finalIdentity"):
                return "cleanup intent lifecycle final state is malformed"
            if base.get("expectedPresent"):
                if not self.bases_path.exists():
                    return f"missing target: {self.bases_path.name}"
                if symbol in bases and self._identity(bases[symbol]) != base.get("expectedIdentity"):
                    return f"changed target: {self.bases_path.name}"
            elif symbol in bases:
                return f"changed target: {self.bases_path.name}"
            current_lifecycle = lifecycles.get(symbol)
            if lifecycle.get("expectedPresent"):
                if current_lifecycle is None or self._identity(current_lifecycle) != lifecycle.get("expectedIdentity"):
                    if self._identity(current_lifecycle) != lifecycle.get("finalIdentity"):
                        return f"changed target: {self.lifecycle_path.name}"
            elif current_lifecycle is not None and self._identity(current_lifecycle) != lifecycle.get("finalIdentity"):
                return f"changed target: {self.lifecycle_path.name}"
            planned_controls: set[Path] = set()
            for control in intent.get("controls", []):
                if not isinstance(control, dict) or not all(
                    isinstance(control.get(key), str) and control.get(key)
                    for key in ("source", "destination", "identity")
                ):
                    return "cleanup intent control target is malformed"
                source = self._target_path(control["source"])
                destination = self._target_path(control["destination"])
                planned_controls.add(source)
                if source.exists() and destination.exists():
                    return f"archive conflict: {destination}"
                if not source.exists() and not destination.exists():
                    return f"missing source and archive: {source}"
                if source.exists() and self._file_identity(source) != control["identity"]:
                    return f"changed target: {source.name}"
                if destination.exists() and self._file_identity(destination) != control["identity"]:
                    return f"archive conflict: {destination}"
            unexpected_controls = {
                path.resolve() for path in self._controls().get(symbol, [])
            } - planned_controls
            if unexpected_controls:
                return f"unplanned control file: {sorted(unexpected_controls)[0]}"
            for target in (intent.get("dashboardSettings"), intent.get("accountControl")):
                if target is None:
                    continue
                if not isinstance(target, dict) or not all(
                    isinstance(target.get(key), str) and target.get(key)
                    for key in ("path", "expectedIdentity", "finalIdentity")
                ) or not isinstance(target.get("final"), dict):
                    return "cleanup intent whole-file target is malformed"
                if self._target_path(target["path"]) != Path(target["path"]).resolve():
                    return "cleanup intent target path is unstable"
                current = self._read_json_object(self._target_path(target["path"]))
                current_identity = self._identity(current)
                if self._identity(target["final"]) != target["finalIdentity"]:
                    return "cleanup intent whole-file final state is malformed"
                if current_identity not in {target["expectedIdentity"], target["finalIdentity"]}:
                    return f"changed target: {Path(target['path']).name}"
        except (KeyError, TypeError, ValueError, OSError) as exc:
            return str(exc)
        return None

    def _apply_intent(self, intent: dict) -> tuple[bool, list[str], str | None]:
        preflight_error = self._preflight_intent(intent)
        if preflight_error:
            return False, [], preflight_error
        if intent.get("version") != 1 or intent.get("account") != self.account_id:
            return False, [], "unsupported or mismatched cleanup intent"
        symbol = self._symbol(intent.get("symbol"))
        if not symbol or symbol != intent.get("symbol") or intent.get("market") != self.market:
            return False, [], "mismatched cleanup intent identity"
        removed: list[str] = []
        ok, changed = self._apply_mapping_target(
            self.bases_path, symbol, intent["base"], remove=True,
        )
        if not ok:
            return False, removed, changed
        if changed:
            removed.append(changed)
        ok, changed = self._apply_mapping_target(
            self.lifecycle_path, symbol, intent["lifecycle"],
        )
        if not ok:
            return False, removed, changed
        if changed:
            removed.append(changed)
        for control in intent.get("controls", []):
            source = self._target_path(control["source"])
            destination = self._target_path(control["destination"])
            source_exists, destination_exists = source.exists(), destination.exists()
            if destination_exists:
                try:
                    destination_matches = self._file_identity(destination) == control["identity"]
                except OSError:
                    destination_matches = False
                if source_exists or not destination_matches:
                    return False, removed, f"archive conflict: {destination}"
                continue
            if not source_exists:
                return False, removed, f"missing source and archive: {source}"
            try:
                source_matches = self._file_identity(source) == control["identity"]
            except OSError:
                source_matches = False
            if not source_matches:
                return False, removed, f"changed target: {source.name}"
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                source.rename(destination)
            except OSError as exc:
                return False, removed, f"archive conflict: {destination}: {exc}"
            removed.append(f"control_archived:{source.name}")
        for target, label in (
            (intent.get("dashboardSettings"), "dashboard_profile"),
            (intent.get("accountControl"), "account_control"),
        ):
            ok, changed = self._apply_whole_file_target(target, label)
            if not ok:
                return False, removed, changed
            if changed:
                removed.append(changed)
        return True, removed, None

    def _verify_intent(self, intent: dict) -> bool:
        symbol = intent["symbol"]
        bases = self._read_json_object(self.bases_path, missing={})
        lifecycles = self._read_json_object(self.lifecycle_path, missing={})
        if symbol in bases or self._identity(lifecycles.get(symbol)) != intent["lifecycle"]["finalIdentity"]:
            return False
        if self._controls().get(symbol):
            return False
        for control in intent.get("controls", []):
            source, destination = Path(control["source"]), Path(control["destination"])
            if source.exists() or not destination.exists():
                return False
            if self._file_identity(destination) != control["identity"]:
                return False
        for target in (intent.get("dashboardSettings"), intent.get("accountControl")):
            if target is not None:
                current = self._read_json_object(self._target_path(target["path"]))
                if self._identity(current) != target["finalIdentity"]:
                    return False
        return True

    def sweep(
        self, broker_qty_by_symbol: dict[str, float], balance_complete: bool,
        has_unresolved_orders: Callable[[str], bool], *, balance_generation: str,
        fresh_balance: bool, balance_fetch_started_at: str | None = None,
        apply: bool = True,
    ) -> list[dict]:
        """Evaluate one broker generation and replay durable cleanup intents."""
        quantities = {self._symbol(key): float(value) for key, value in broker_qty_by_symbol.items()}
        results: list[dict] = []
        with _account_file_lock(self.lock_path):
            try:
                state = self._read_state()
            except ValueError as exc:
                result = {"symbol": "", "classification": "manual_review_cleanup_state", "reason": str(exc)}
                self._audit(result)
                return [result]
            confirmations = dict(state["zeroConfirmations"])
            last_generations = dict(state["lastProcessedGenerations"])
            qualifying = {
                key: list(value) if isinstance(value, list) else []
                for key, value in state["qualifyingGenerations"].items()
            }
            pending = dict(state["pendingCleanup"])
            symbols = self._symbols() | set(quantities) | set(pending)
            for symbol in sorted(symbols):
                if symbol in self._manual_review:
                    result = {"symbol": symbol, "classification": "manual_review_symbol_key"}
                    results.append(result)
                    self._audit(result)
                    continue
                intent = pending.get(symbol)
                try:
                    unresolved = bool(has_unresolved_orders(symbol))
                    inspection_failed = False
                except Exception as exc:
                    unresolved = True
                    inspection_failed = True
                    inspection_reason = str(exc)
                qty = quantities.get(symbol, 0.0)
                if intent is not None:
                    intent_created_at = intent.get("createdAt") if isinstance(intent, dict) else None
                    fresh_for_intent = (
                        fresh_balance
                        and isinstance(intent_created_at, str)
                        and isinstance(balance_fetch_started_at, str)
                        and balance_fetch_started_at > intent_created_at
                    )
                    if not balance_complete:
                        result = {"symbol": symbol, "classification": "blocked_incomplete_balance"}
                    elif not fresh_for_intent:
                        result = {"symbol": symbol, "classification": "blocked_replay_requires_fresh"}
                    elif qty > 1e-9:
                        result = {"symbol": symbol, "classification": "protected_nonzero_holding"}
                    elif inspection_failed:
                        result = {
                            "symbol": symbol,
                            "classification": "manual_review_unresolved_order_inspection",
                            "reason": inspection_reason,
                        }
                    elif unresolved:
                        result = {"symbol": symbol, "classification": "manual_review_required"}
                    elif apply:
                        try:
                            ok, removed, reason = self._apply_intent(intent)
                            verified = ok and self._verify_intent(intent)
                        except (KeyError, TypeError, ValueError, OSError) as exc:
                            ok, removed, reason, verified = False, [], str(exc), False
                        if verified:
                            pending.pop(symbol, None)
                            confirmations.pop(symbol, None)
                            last_generations.pop(symbol, None)
                            qualifying.pop(symbol, None)
                            result = {"symbol": symbol, "classification": "cleaned", "removed": removed}
                        else:
                            result = {
                                "symbol": symbol,
                                "classification": "manual_review_cleanup_conflict",
                                "reason": reason or "cleanup final state could not be verified",
                            }
                    else:
                        result = {"symbol": symbol, "classification": "eligible_dry_run"}
                    if result["classification"] in {
                        "blocked_incomplete_balance", "protected_nonzero_holding",
                        "manual_review_required", "manual_review_unresolved_order_inspection",
                    }:
                        confirmations.pop(symbol, None)
                        last_generations.pop(symbol, None)
                        qualifying.pop(symbol, None)
                    results.append(result)
                    self._audit(result)
                    continue
                try:
                    result = self.evaluate(symbol, qty, balance_complete, lambda _symbol: unresolved)
                except ValueError as exc:
                    result = {
                        "symbol": symbol,
                        "classification": "manual_review_cleanup_target",
                        "reason": str(exc),
                    }
                if inspection_failed:
                    result["classification"] = "manual_review_unresolved_order_inspection"
                    result["reason"] = inspection_reason
                if result["classification"] == "orphan_candidate":
                    generations = qualifying.get(symbol, [])
                    if last_generations.get(symbol) != balance_generation:
                        generations = (generations + [balance_generation])[-2:]
                        qualifying[symbol] = generations
                        last_generations[symbol] = balance_generation
                    confirmations[symbol] = len(generations)
                    result["zeroConfirmations"] = len(generations)
                    result["qualifyingBalanceGenerations"] = generations
                    if len(generations) >= 2 and apply:
                        try:
                            intent = self._build_intent(symbol, generations)
                            pending[symbol] = intent
                            state.update({
                                "zeroConfirmations": confirmations,
                                "lastProcessedGenerations": last_generations,
                                "qualifyingGenerations": qualifying,
                                "pendingCleanup": pending,
                            })
                            self._write_state(state)
                        except (OSError, ValueError) as exc:
                            result["classification"] = "manual_review_intent_persistence"
                            result["reason"] = str(exc)
                        else:
                            try:
                                ok, removed, reason = self._apply_intent(intent)
                                verified = ok and self._verify_intent(intent)
                            except (KeyError, TypeError, ValueError, OSError) as exc:
                                ok, removed, reason, verified = False, [], str(exc), False
                            if verified:
                                pending.pop(symbol, None)
                                confirmations.pop(symbol, None)
                                last_generations.pop(symbol, None)
                                qualifying.pop(symbol, None)
                                result["classification"] = "cleaned"
                                result["removed"] = removed
                            else:
                                result["classification"] = "manual_review_cleanup_conflict"
                                result["reason"] = reason or "cleanup final state could not be verified"
                    elif len(generations) >= 2:
                        result["classification"] = "eligible_dry_run"
                else:
                    confirmations.pop(symbol, None)
                    last_generations.pop(symbol, None)
                    qualifying.pop(symbol, None)
                    result["zeroConfirmations"] = 0
                results.append(result)
                self._audit(result)
            state.update({
                "zeroConfirmations": confirmations,
                "lastProcessedGenerations": last_generations,
                "qualifyingGenerations": qualifying,
                "pendingCleanup": pending,
            })
            self._write_state(state)
        return results

    def _audit(self, result: dict) -> None:
        payload = {"timestamp": datetime.now(timezone.utc).isoformat(), "account": self.account_id, **result}
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        if self.logger:
            self.logger.info(f"Orphan cleanup audit: {payload}")

    def _migration_audit(self, result: dict) -> None:
        payload = {"timestamp": datetime.now(timezone.utc).isoformat(), "account": self.account_id, "market": self.market, **result}
        self.migration_audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.migration_audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
