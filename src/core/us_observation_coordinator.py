"""Observation-only DB/checkpoint coordination; no startup wiring or orders.

DB commit and external checkpoint replacement are separate commits. An
interruption between them requires explicit reconciliation, never head adoption.
The existing Adapter receives OBSERVED only after both commits are verified.
"""
from __future__ import annotations

from src.core.us_observation_interface import UsObservationReceipt, ValidatedUsObservationCycle
from src.data.us_observation_checkpoint import ObservationCheckpointFile
from src.data.us_operational_observation_store import (
    OperationalObservationHead, OperationalObservationReceipt,
    OperationalObservationRecovery, OperationalUsObservationStore,
)


class UsObservationCoordinator:
    """Explicit sink wrapper; failures remain latched for this instance.

    Independently pinned metadata is a consistency boundary, not proof of a
    broker account. The writer lease spans recovery, DB commit, and checkpoint
    replacement. All other writers must use the same prepared checkpoint lock.
    """

    def __init__(self, *, store, checkpoint, enabled=False):
        if (type(store) is not OperationalUsObservationStore
                or type(checkpoint) is not ObservationCheckpointFile
                or type(enabled) is not bool
                or (store.journal_id, store.binding_id) != (checkpoint.journal_id, checkpoint.binding_id)
                or checkpoint.path.resolve(strict=True) == store.expected_path.resolve(strict=True)
                or checkpoint.lock_path.resolve(strict=True) == store.expected_path.resolve(strict=True)):
            raise ValueError("One dedicated store and independently pinned checkpoint are required")
        self.store, self.checkpoint, self.enabled = store, checkpoint, enabled
        self._blocked = False

    def recover(self):
        if self._blocked:
            return OperationalObservationRecovery("INCOMPLETE", ("coordinator_failure_latched",))
        try:
            with self.checkpoint.exclusive():
                result = self.store.recover(expected_head=self.checkpoint.read())
            if result.state != "OBSERVATION_VALIDATED" or result.anchor_verified is not True:
                self._blocked = True
            return result
        except Exception:
            self._blocked = True
            return OperationalObservationRecovery("INCOMPLETE", ("checkpoint_restart_validation_failed",))

    def record_cycle(self, cycle):
        if (self.enabled is not True or self.store.enabled is not True or self._blocked
                or type(cycle) is not ValidatedUsObservationCycle):
            raise ValueError("Observation coordination is disabled, blocked, or has invalid input")
        db_committed = False
        try:
            with self.checkpoint.exclusive():
                previous = self.checkpoint.read()
                recovery = self.store.recover(expected_head=previous)
                if recovery.state != "OBSERVATION_VALIDATED" or recovery.anchor_verified is not True:
                    raise ValueError("Observation recovery does not match the independent checkpoint")
                receipt = self.store.record_cycle(cycle)
                if (type(receipt) is not OperationalObservationReceipt or receipt.committed is not True
                        or receipt.economic_writes is not False or receipt.cycle_token != cycle.cycle_token
                        or type(receipt.head) is not OperationalObservationHead
                        or receipt.state not in ("OBSERVED", "CONFLICT")):
                    raise ValueError("Store returned an invalid committed observation receipt")
                db_committed = True
                self.checkpoint.advance(previous=previous, current=receipt.head)
            if receipt.state == "CONFLICT":
                self._blocked = True
            return receipt
        except Exception:
            self._blocked = True
            # A committed DB cycle is never reported as rolled back. The Adapter
            # rejects INCOMPLETE even when committed=True, so synchronization
            # cannot continue while the independent checkpoint is unresolved.
            return UsObservationReceipt(cycle.cycle_token, "INCOMPLETE", db_committed)
