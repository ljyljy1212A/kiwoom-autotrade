"""Default-disabled scratch recovery gate; never Engine or submission authority.

Reader/journal/connection object binding is process-local fixture provenance,
not persistent database identity or broker authentication. Each enabled check
reads again. Sticky blockers have no reset/resolution API. No backend creation,
write, retry, cache-based clearance or production activation occurs here.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from src.data.order_identity import validate_order_date
from src.data.us_cumulative_execution import _timestamp, normalize_cumulative_observations
from src.data.us_synthetic_generations import _identifier
from src.data.us_synthetic_observation_journal import SyntheticObservationJournal
from src.data.us_synthetic_observation_recovery import (
    SyntheticObservationRecoveryReader, SyntheticRecoveredConflict, SyntheticScopeRecovery,
)


@dataclass(frozen=True)
class SyntheticRecoveryDecision:
    account_id: str
    market: str
    symbol: str | None
    state: str
    account_blocked: bool
    symbol_blocked: bool
    check_complete: bool
    conflicts: tuple[SyntheticRecoveredConflict, ...] = ()
    reasons: tuple[str, ...] = ()
    economic_ingestion_allowed: bool = False
    operational_trading_allowed: bool = False


class SyntheticRecoveryGate:
    def __init__(self, *, account_id, market, enabled=False, reader=None, expected_journal=None):
        if account_id != "us_mock" or market != "US" or type(enabled) is not bool:
            raise ValueError("Explicit US mock scope and boolean activation are required")
        self._account_id, self._market, self._enabled = account_id, market, enabled
        self._reader, self._journal = reader, expected_journal
        self._connection = (expected_journal.db if enabled and type(expected_journal) is SyntheticObservationJournal else None)
        self._account_failure = False
        self._conflicts = {}

    def _binding_matches(self):
        return (type(self._reader) is SyntheticObservationRecoveryReader
                and type(self._journal) is SyntheticObservationJournal
                and self._reader.journal is self._journal
                and self._connection is not None and self._journal.db is self._connection)

    def _validate(self, result, symbol):
        if (type(result) is not SyntheticScopeRecovery
                or (result.account_id, result.market, result.symbol) != (self._account_id, self._market, symbol)
                or result.economic_ingestion_allowed is not False or result.operational_trading_allowed is not False
                or result.execution_date_status != "unresolved"
                or not isinstance(result.conflicts, tuple) or not isinstance(result.reasons, tuple)):
            raise ValueError("Invalid recovery receipt")
        if result.state == "INCOMPLETE":
            if (result.scope_complete is not False or result.blocking_scope != "account"
                    or result.conflicts or result.journal_conflict_count is not None
                    or result.coverage != "unverified-scratch-journal" or not result.reasons
                    or any(not isinstance(reason, str) or not reason.strip() for reason in result.reasons)):
                raise ValueError("Inconsistent incomplete receipt")
            return None
        if (result.scope_complete is not True or result.coverage != "validated-scratch-journal-only"
                or type(result.journal_conflict_count) is not int
                or result.journal_conflict_count < len(result.conflicts) or result.reasons):
            raise ValueError("Incomplete recovery coverage")
        if result.state == "RECOVERED_NO_CONFLICT":
            if result.blocking_scope != "none" or result.conflicts:
                raise ValueError("Conflict concealed by clean receipt")
        elif result.state != "CONFLICT" or result.blocking_scope != "symbol" or not result.conflicts:
            raise ValueError("Invalid recovery state")
        staged = dict(self._conflicts)
        seen = set()
        for item in result.conflicts:
            if type(item) is not SyntheticRecoveredConflict or item.symbol != symbol or item.order_uid in seen:
                raise ValueError("Ambiguous recovered conflict")
            seen.add(item.order_uid)
            _identifier(item.order_uid)
            _identifier(item.reason)
            date = validate_order_date(item.order_date)
            if _timestamp(item.first_seen_at_utc) > _timestamp(item.last_seen_at_utc):
                raise ValueError("Invalid recovered timestamps")
            raw = json.loads(item.raw_json)
            verified = normalize_cumulative_observations({
                "return_code": 0, "_execution_pages_complete": True,
                "_query_order_date": date, "result_list": [raw],
            }, account_id=self._account_id, query_order_date=date, observed_at_utc=item.first_seen_at_utc)[0]
            if (verified.ord_no, verified.symbol) != (item.ord_no, item.symbol):
                raise ValueError("Recovered raw evidence identity mismatch")
            previous = staged.get(item.order_uid)
            if previous is not None:
                if ((previous.symbol, previous.order_date, previous.ord_no, previous.reason,
                     previous.first_seen_at_utc, previous.raw_json) !=
                        (item.symbol, item.order_date, item.ord_no, item.reason, item.first_seen_at_utc, item.raw_json)
                        or _timestamp(item.last_seen_at_utc) < _timestamp(previous.last_seen_at_utc)):
                    raise ValueError("Previously latched identity/evidence changed or regressed")
            staged[item.order_uid] = item
        return staged

    def check_scope(self, symbol):
        if not self._enabled:
            return SyntheticRecoveryDecision(self._account_id, self._market, None, "DISABLED", False, False, False)
        canonical = symbol if isinstance(symbol, str) and re.fullmatch(r"[A-Z][A-Z0-9.-]{0,11}", symbol) else None
        complete = False
        reason = None
        try:
            if canonical is None or not self._binding_matches():
                raise ValueError("Missing or changed scratch recovery binding")
            result = self._reader.recover_scope(account_id=self._account_id, market=self._market, symbol=canonical)
            if not self._binding_matches():
                raise ValueError("Scratch backend changed during recovery")
            staged = self._validate(result, canonical)
            if staged is None:
                self._account_failure = True
                reason = "recovery_incomplete"
            else:
                self._conflicts = staged
                complete = True
        except Exception:
            self._account_failure = True
            reason = "recovery_check_failed"
        retained = tuple(item for _, item in sorted(self._conflicts.items()) if item.symbol == canonical)
        if self._account_failure:
            state, reasons = "INCOMPLETE", (reason or "account_recovery_failure_latched",)
        elif retained:
            state, reasons = "CONFLICT", ("historical_conflict_latched",)
        else:
            state, reasons = "RECOVERY_CHECKED", ()
        return SyntheticRecoveryDecision(self._account_id, self._market, canonical, state,
                                         self._account_failure, bool(retained), complete, retained, reasons)
