"""Explicit routed US mock observation startup using separately prepared files."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from src.core.us_observation_coordinator import UsObservationCoordinator
from src.core.us_observation_interface import UsObservationAdapter
from src.data.us_observation_checkpoint import ObservationCheckpointFile, _regular
from src.data.us_operational_observation_store import OperationalUsObservationStore, _identifier


OBSERVATION_KEYS = (
    "US_MOCK_OBSERVATION_ENABLED", "US_MOCK_OBSERVATION_DB_PATH",
    "US_MOCK_OBSERVATION_CHECKPOINT_PATH", "US_MOCK_OBSERVATION_JOURNAL_ID",
    "US_MOCK_OBSERVATION_BINDING_ID",
)


class UsObservationSession:
    """One worker-owned connection shared by its serial observation cycles."""

    def __init__(self, connection, coordinator):
        self._connection = connection
        self.adapter = UsObservationAdapter(
            account_id="us_mock", market="US", enabled=True, sink=coordinator,
        )

    def close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None


def open_us_observation_session(*, account_id, market, mode, environment):
    """Validate existing files and their independent head before worker startup.

    No defaults for storage paths or identity, mkdir, schema preparation,
    migration, checkpoint adoption, retries, or fallback database. An unset
    activation flag retains the existing worker path without opening files.
    """
    flag = environment.get(OBSERVATION_KEYS[0], "false")
    if flag == "false":
        return None
    connection = None
    try:
        if flag != "true" or (account_id, market, mode) != ("us_mock", "US", "mock"):
            raise ValueError("Invalid observation activation scope")
        runtime = Path(environment.get("KIWOOM_RUNTIME_ROOT", ""))
        if not runtime.is_absolute() or not runtime.is_dir():
            raise ValueError("A routed worker is required")
        if any(not environment.get(key) for key in OBSERVATION_KEYS[1:]):
            raise ValueError("All independent observation pins are required")
        journal = _identifier(environment[OBSERVATION_KEYS[3]])
        binding = _identifier(environment[OBSERVATION_KEYS[4]])
        database = Path(environment[OBSERVATION_KEYS[1]])
        checkpoint_path = Path(environment[OBSERVATION_KEYS[2]])
        checkpoint = ObservationCheckpointFile(
            path=checkpoint_path, journal_id=journal, binding_id=binding,
        )
        before = _regular(database)
        if database.resolve(strict=True) in (
            checkpoint.path.resolve(strict=True), checkpoint.lock_path.resolve(strict=True),
        ):
            raise ValueError("Dedicated database and checkpoint files are required")
        # mode=rw cannot create a missing database. Zero timeout rejects
        # contention immediately rather than retrying behind another writer.
        connection = sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=0)
        store = OperationalUsObservationStore(
            connection, expected_path=database, journal_id=journal, binding_id=binding, enabled=True,
        )
        after = _regular(database)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError("Database identity changed during startup")
        coordinator = UsObservationCoordinator(store=store, checkpoint=checkpoint, enabled=True)
        recovery = coordinator.recover()
        if (recovery.state != "OBSERVATION_VALIDATED" or recovery.anchor_verified is not True
                or recovery.economic_ingestion_allowed is not False
                or recovery.operational_trading_allowed is not False):
            raise ValueError("Observation restart validation is incomplete")
        return UsObservationSession(connection, coordinator)
    except Exception:
        if connection is not None:
            connection.close()
        # Do not expose environment values, raw DB errors, or payloads.
        raise RuntimeError("US mock observation startup refused: prepared pins/head validation failed") from None
