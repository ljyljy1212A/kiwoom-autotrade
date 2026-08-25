"""Broker-authoritative unattended cleanup for closed symbol runtime state.

This module deliberately never deletes accounting history.  It only retires
re-creatable automation state after two complete broker snapshots confirm that
a symbol has no position and no unresolved order attribution.
"""
from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src.core.runtime_paths import DATA_DIR
from src.core.symbol_keys import canonical_symbol_key, legacy_symbol_key


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
        temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

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
        """Atomically rekey uniquely attributable legacy state; retain ambiguity."""
        canonical_candidates = {self._symbol(symbol) for symbol in candidates if symbol}
        owners: dict[str, set[str]] = {}
        for symbol in canonical_candidates:
            owners.setdefault(legacy_symbol_key(symbol), set()).add(symbol)
        stores = (self.bases_path, self.lifecycle_path, self.state_path)
        changed = False
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
                    changed = True
                    events.append({"status": "migrated", "oldKey": key, "newKey": replacement, "store": path.name})
                elif len(candidate_owners) > 1:
                    self._manual_review.update(candidate_owners | {key})
                    events.append({"status": "manual_review", "key": key, "owners": sorted(candidate_owners)})
                elif key != self._symbol(key):
                    self._manual_review.add(key)
                    events.append({"status": "manual_review", "key": key, "owners": []})
            if path == self.state_path:
                data["zeroConfirmations"] = target
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

    def sweep(self, broker_qty_by_symbol: dict[str, float], balance_complete: bool,
              has_unresolved_orders: Callable[[str], bool], apply: bool = True) -> list[dict]:
        """Run the same evaluator for startup and live balance reconciliation."""
        quantities = {self._symbol(key): float(value) for key, value in broker_qty_by_symbol.items()}
        state = self._read(self.state_path, {})
        previous = state.get("zeroConfirmations", {}) if isinstance(state, dict) else {}
        confirmations: dict[str, int] = {}
        results: list[dict] = []
        for symbol in sorted(self._symbols() | set(quantities)):
            if symbol in self._manual_review:
                results.append({"symbol": symbol, "classification": "manual_review_symbol_key"})
                continue
            result = self.evaluate(symbol, quantities.get(symbol, 0.0), balance_complete, has_unresolved_orders)
            if result["classification"] == "orphan_candidate":
                count = int(previous.get(symbol, 0)) + 1
                confirmations[symbol] = count
                result["zeroConfirmations"] = count
                if count >= 2 and apply:
                    result["classification"] = "cleaned"
                    result["removed"] = self._apply(symbol)
                elif count >= 2:
                    result["classification"] = "eligible_dry_run"
            elif result["classification"] in {"protected_nonzero_holding", "manual_review_required"}:
                # A nonzero holding or unresolved order invalidates any old zero observation.
                result["zeroConfirmations"] = 0
            else:
                result["zeroConfirmations"] = 0
            results.append(result)
            self._audit(result)
        self._write_atomic(self.state_path, {"zeroConfirmations": confirmations,
                                             "updatedAt": datetime.now(timezone.utc).isoformat()})
        return results

    def _apply(self, symbol: str) -> list[str]:
        """Archive controls and retire runtime state without touching ledger history."""
        removed: list[str] = []
        bases = self._read(self.bases_path, {})
        if symbol in bases:
            bases.pop(symbol, None)
            self._write_atomic(self.bases_path, bases)
            removed.append("tranche_base")
        lifecycles = self._read(self.lifecycle_path, {})
        current = lifecycles.get(symbol, {})
        lifecycles[symbol] = {"status": "closed", "started_at": current.get("started_at") if isinstance(current, dict) else None,
                              "closed_at": datetime.now(timezone.utc).isoformat(), "reason": "automatic_orphan_cleanup"}
        self._write_atomic(self.lifecycle_path, lifecycles)
        removed.append("lifecycle_closed")
        archive_root = self.data_dir / "archive" / "orphan_cleanup" / self.account_id / datetime.now(timezone.utc).strftime("%Y%m%d")
        for path in self._controls().get(symbol, []):
            archive_root.mkdir(parents=True, exist_ok=True)
            destination = archive_root / f"{path.stem}_{uuid.uuid4().hex[:8]}{path.suffix}"
            shutil.move(str(path), str(destination))
            removed.append(f"control_archived:{path.name}")
        settings = self._read(self.settings_path, {})
        profiles = settings.get("profiles", []) if isinstance(settings, dict) else []
        retained = [profile for profile in profiles if self._symbol((profile.get("config") or {}).get("symbol")) != symbol]
        if len(retained) != len(profiles):
            settings["profiles"] = retained
            self._write_atomic(self.settings_path, settings)
            removed.append("dashboard_profile")
        account_control = self.data_dir / f"dashboard_control_{self.account_id}.json"
        control = self._read(account_control, {})
        if self._symbol(control.get("symbol")) == symbol:
            self._write_atomic(account_control, {"symbol": "", "auto_buy": False, "auto_sell": False})
            removed.append("account_control")
        return removed

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
