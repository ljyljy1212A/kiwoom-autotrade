"""Read-only scratch conflict recovery; no Engine connection or order authority.

Coverage is limited to the explicitly prepared synthetic journal. No current
order list, status, lifecycle or date filter determines which conflicts exist.
A no-conflict result never clears a sticky Engine blocker or proves finality.
No cache/revision/token claims, initialization, migration, repair or retry.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from src.data.us_synthetic_observation_journal import (
    SyntheticObservationJournal, _POLICY, _schema_hash,
)


@dataclass(frozen=True)
class SyntheticRecoveredConflict:
    order_uid: str
    symbol: str
    order_date: str
    ord_no: str
    reason: str
    first_seen_at_utc: str
    last_seen_at_utc: str
    raw_json: str


@dataclass(frozen=True)
class SyntheticScopeRecovery:
    account_id: str
    market: str
    symbol: str
    state: str
    blocking_scope: str
    scope_complete: bool
    conflicts: tuple[SyntheticRecoveredConflict, ...] = ()
    journal_conflict_count: int | None = None
    reasons: tuple[str, ...] = ()
    coverage: str = "unverified-scratch-journal"
    execution_date_status: str = "unresolved"
    economic_ingestion_allowed: bool = False
    operational_trading_allowed: bool = False


class SyntheticObservationRecoveryReader:
    def __init__(self, journal):
        if not isinstance(journal, SyntheticObservationJournal):
            raise ValueError("Explicit prepared synthetic journal is required")
        self.journal = journal

    def recover_scope(self, *, account_id, market, symbol):
        if (account_id != "us_mock" or market != "US" or not isinstance(symbol, str)
                or not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,11}", symbol)):
            raise ValueError("Explicit canonical US mock account/market/symbol is required")
        journal = self.journal
        try:
            # This refuses a caller-owned transaction before BEGIN. Failures
            # here must not roll back unrelated caller work.
            journal._metadata()
            journal.db.execute("BEGIN")
            try:
                metadata = journal.db.execute(
                    "SELECT policy,account_id,market,schema_sha256 FROM synthetic_journal_meta WHERE singleton=1",
                ).fetchone()
                if (metadata != (_POLICY, "us_mock", "US", _schema_hash(journal.db))
                        or journal.db.execute("PRAGMA user_version").fetchone() != (2,)):
                    raise ValueError("Scratch schema changed before recovery snapshot")
                # Validate the entire journal first. Broken ownership anywhere
                # makes symbol-level exclusion unsafe and blocks the account.
                recovered = journal._recover_in_transaction()
                identities = dict((row[0], row) for row in journal.db.execute("SELECT * FROM order_identities"))
                relevant = []
                for row in recovered.conflicts:
                    identity = identities[row[0]]
                    if identity[4] == symbol:
                        relevant.append(SyntheticRecoveredConflict(
                            row[0], identity[4], identity[6], identity[3],
                            row[1], row[2], row[3], row[4],
                        ))
                result = SyntheticScopeRecovery(
                    account_id, market, symbol,
                    "CONFLICT" if relevant else "RECOVERED_NO_CONFLICT",
                    "symbol" if relevant else "none", True,
                    tuple(relevant), len(recovered.conflicts),
                    coverage="validated-scratch-journal-only",
                )
            finally:
                # Read-only recovery owns no commit and does not modify data.
                journal.db.rollback()
        except Exception:
            # No partial no-conflict result, raw exception, fallback or retry.
            return SyntheticScopeRecovery(account_id, market, symbol, "INCOMPLETE", "account", False,
                                          reasons=("scratch_recovery_failed",))
        return result
