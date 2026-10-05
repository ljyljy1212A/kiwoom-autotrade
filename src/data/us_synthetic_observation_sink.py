"""Scratch journal sink for the injected interface; never Engine or broker authority.

Only an explicitly prepared synthetic journal is accepted. Fixture OrderIdentity
snapshots remain synthetic evidence. No creation, binding, migration or retry is
performed here. A receipt describes this committed call, not broker finality.
"""
from __future__ import annotations

import json

from src.core.us_observation_interface import (
    UsObservationBinding, UsObservationReceipt, UsObservationResponse,
    UsTrackedObservationOrder, ValidatedUsObservationCycle, _cycle,
)
from src.data.order_identity import OrderIdentity
from src.data.us_cumulative_execution import _decimal
from src.data.us_synthetic_observation_journal import (
    SyntheticObservationJournal, _POLICY, _schema_hash,
)


class SyntheticObservationSink:
    operational_ingestion_allowed = False

    def __init__(self, journal):
        if not isinstance(journal, SyntheticObservationJournal):
            raise ValueError("Explicit prepared scratch journal is required")
        self.journal = journal

    def _validate_cycle(self, cycle):
        if (not isinstance(cycle, ValidatedUsObservationCycle)
                or cycle.account_id != "us_mock" or cycle.market != "US"
                or cycle.economic_ingestion_allowed is not False
                or not isinstance(cycle.bindings, tuple) or not cycle.bindings
                or not isinstance(cycle.observations, tuple)
                or not isinstance(cycle.query_contexts, tuple)):
            raise ValueError("Explicit observation-only US mock cycle is required")
        tracked = []
        for binding in cycle.bindings:
            if not isinstance(binding, UsObservationBinding):
                raise ValueError("Invalid observation binding")
            row = self.journal.db.execute(
                "SELECT i.*,p.requested_qty FROM order_identities i JOIN pending_orders p "
                "ON p.order_uid=i.order_uid AND p.account_id=i.account_id "
                "AND p.ord_no=i.ord_no AND p.symbol=i.symbol AND p.side=i.side "
                "WHERE i.order_uid=?", (binding.order_uid,),
            ).fetchall()
            if len(row) != 1 or _decimal(binding.requested_quantity, "request") != row[0][9]:
                raise ValueError("Cycle identity/request does not match scratch database")
            tracked.append(UsTrackedObservationOrder(OrderIdentity(*row[0][:9]), binding.requested_quantity))
        responses = tuple(
            UsObservationResponse(date, stamp, {
                "return_code": 0, "_execution_pages_complete": True, "_query_order_date": date,
                "result_list": [json.loads(item.raw_json) for item in cycle.observations
                                if item.query_order_date == date],
            }) for date, stamp in cycle.query_contexts
        )
        # Rebuild from stored identities and raw observations. This checks all
        # bindings, normalized values, date coverage, required conflicts/token.
        if _cycle("us_mock", "US", tuple(tracked), responses) != cycle:
            raise ValueError("Cycle content/token is inconsistent with stored identities")

    def record_cycle(self, cycle):
        journal = self.journal
        if journal.store.db is not journal.db:
            raise ValueError("One shared journal connection is required")
        journal._metadata()  # Refuse an existing transaction without rolling it back.
        journal.db.execute("BEGIN IMMEDIATE")
        try:
            metadata = journal.db.execute(
                "SELECT policy,account_id,market,schema_sha256 FROM synthetic_journal_meta WHERE singleton=1",
            ).fetchone()
            if metadata != (_POLICY, "us_mock", "US", _schema_hash(journal.db)):
                raise ValueError("Scratch schema changed before transaction acquisition")
            recovery = journal._recover_in_transaction()
            self._validate_cycle(cycle)
            tracked_uids = {binding.order_uid for binding in cycle.bindings}
            # A tracked order's durable conflict blocks even if the response
            # has no row for that order. Absence never clears a conflict.
            latched = tuple((row[0], row[1]) for row in recovery.conflicts if row[0] in tracked_uids)
            result = journal.store._observe_in_transaction(
                cycle.observations, blocking_conflicts=latched,
                required_conflicts=cycle.required_conflicts,
                audit_context={"cycle_token": cycle.cycle_token,
                               "required_conflicts": cycle.required_conflicts,
                               "latched_conflicts": latched,
                               "query_contexts": cycle.query_contexts},
            )
            # Check the new audit/checkpoint state before committing. No economic
            # schema exists here; recovery also requires pending quantity zero.
            journal._recover_in_transaction()
            journal.db.commit()
        except Exception:
            journal.db.rollback()
            raise
        return UsObservationReceipt(cycle.cycle_token,
                                    "CONFLICT" if result.conflicts else "OBSERVED",
                                    True, result.conflicts, economic_writes=False)
