"""Temporary DB/checkpoint coordination tests with network access denied."""
import json
from pathlib import Path
import sqlite3

import pytest

from src.core import us_observation_coordinator as coordinator_module
from src.core.us_observation_coordinator import UsObservationCoordinator
from src.core.us_observation_interface import UsObservationAdapter
from src.data import us_observation_checkpoint as checkpoint_module
from src.data.us_observation_checkpoint import LOCK_CONTENT, ObservationCheckpointFile, checkpoint_bytes
from src.data.us_operational_observation_store import OperationalObservationHead
from tests.test_us_operational_observation_store import (
    BINDING, JOURNAL, LATER, count, cycle, empty_head, inputs, ready, store,
    db as db, deny_network as deny_network,
)


@pytest.fixture
def checkpoint(tmp_path):
    path = tmp_path / "checkpoint.json"
    path.write_bytes(checkpoint_bytes(empty_head()))
    path.with_name(path.name + ".lock").write_bytes(LOCK_CONTENT)
    return ObservationCheckpointFile(path=path, journal_id=JOURNAL, binding_id=BINDING)


def coordinated(db, checkpoint, **kwargs):
    return UsObservationCoordinator(store=store(db), checkpoint=checkpoint, enabled=True, **kwargs)


def read_head(checkpoint):
    with checkpoint.exclusive():
        return checkpoint.read()


def next_head(sequence=1):
    return OperationalObservationHead(JOURNAL, BINDING, sequence, "c" * 64)


def test_import_origins_are_the_selected_checkout():
    root = Path(__file__).resolve().parents[1]
    assert Path(checkpoint_module.__file__).resolve() == root / "src/data/us_observation_checkpoint.py"
    assert Path(coordinator_module.__file__).resolve() == root / "src/core/us_observation_coordinator.py"


def test_checkpoint_requires_a_lease_and_rejects_reentry(checkpoint):
    with pytest.raises(ValueError):
        checkpoint.read()
    with pytest.raises(ValueError):
        checkpoint.advance(previous=empty_head(), current=next_head())
    with checkpoint.exclusive():
        assert checkpoint.read() == empty_head()
        with pytest.raises(ValueError):
            with checkpoint.exclusive():
                pytest.fail("Lease must not be reentrant")


def test_advance_duplicate_and_lock_release_preserve_lock_content(checkpoint):
    with checkpoint.exclusive():
        checkpoint.advance(previous=empty_head(), current=next_head())
        baseline = checkpoint.path.read_bytes()
        checkpoint.advance(previous=next_head(), current=next_head())
        assert checkpoint.path.read_bytes() == baseline
    assert read_head(checkpoint) == next_head()
    assert checkpoint.lock_path.read_bytes() == LOCK_CONTENT
    assert not list(checkpoint.path.parent.glob("*.pending"))


def test_competing_lock_is_refused_once_without_replacement(checkpoint):
    competitor = ObservationCheckpointFile(path=checkpoint.path, journal_id=JOURNAL, binding_id=BINDING)
    with checkpoint.exclusive():
        with pytest.raises((OSError, ValueError)):
            with competitor.exclusive():
                pytest.fail("Competing writer acquired the lock")
    assert read_head(competitor) == empty_head()


@pytest.mark.parametrize("value", [b"", b"{}\n", b"not-json", b"\xff", b"x" * 4097])
def test_malformed_checkpoint_is_not_repaired(checkpoint, value):
    checkpoint.path.write_bytes(value)
    with checkpoint.exclusive():
        with pytest.raises((ValueError, TypeError, KeyError, UnicodeError)):
            checkpoint.read()
    assert checkpoint.path.read_bytes() == value


@pytest.mark.parametrize("field,value", [("version", True), ("market", "KR"),
                                        ("account_id", "foreign"), ("policy", "foreign")])
def test_checkpoint_metadata_is_strict(checkpoint, field, value):
    payload = json.loads(checkpoint.path.read_bytes())
    payload[field] = value
    checkpoint.path.write_text(json.dumps(payload), encoding="utf-8")
    with checkpoint.exclusive():
        with pytest.raises(ValueError):
            checkpoint.read()


@pytest.mark.parametrize("mode", ["missing_checkpoint", "missing_lock", "bad_lock", "pretty_json"])
def test_preparation_is_explicit_and_never_automatic(checkpoint, mode):
    if mode == "missing_checkpoint":
        checkpoint.path.unlink()
    elif mode == "missing_lock":
        checkpoint.lock_path.unlink()
    elif mode == "bad_lock":
        checkpoint.lock_path.write_bytes(b"bad")
    else:
        checkpoint.path.write_text(json.dumps(json.loads(checkpoint.path.read_bytes()), indent=2), encoding="utf-8")
    with pytest.raises((ValueError, FileNotFoundError)):
        instance = ObservationCheckpointFile(path=checkpoint.path, journal_id=JOURNAL, binding_id=BINDING)
        with instance.exclusive():
            instance.read()
    if mode == "missing_checkpoint":
        assert not checkpoint.path.exists()
    if mode == "missing_lock":
        assert not checkpoint.lock_path.exists()


@pytest.mark.parametrize("current", [next_head(2), OperationalObservationHead(JOURNAL, BINDING, True, "c" * 64),
    OperationalObservationHead("d" * 32, BINDING, 1, "c" * 64),
    OperationalObservationHead(JOURNAL, BINDING, 1, "0" * 64)])
def test_invalid_checkpoint_advancement_preserves_original(checkpoint, current):
    before = checkpoint.path.read_bytes()
    with checkpoint.exclusive():
        with pytest.raises(ValueError):
            checkpoint.advance(previous=empty_head(), current=current)
    assert checkpoint.path.read_bytes() == before


@pytest.mark.parametrize("failure", ["fsync", "replace"])
def test_failed_checkpoint_write_retains_pending_file_without_retry(checkpoint, monkeypatch, failure):
    calls = []
    def failed(*args):
        calls.append(args)
        raise OSError("synthetic checkpoint failure")
    monkeypatch.setattr(checkpoint_module.os, failure, failed)
    with checkpoint.exclusive():
        with pytest.raises(OSError):
            checkpoint.advance(previous=empty_head(), current=next_head())
    assert len(calls) == 1 and read_head(checkpoint) == empty_head()
    pending = list(checkpoint.path.parent.glob("*.pending"))
    assert len(pending) == 1 and pending[0].read_bytes() == checkpoint_bytes(next_head())


def test_coordinator_requires_both_commits_before_adapter_success(db, checkpoint):
    coordinator = coordinated(db, checkpoint)
    adapter = UsObservationAdapter(account_id="us_mock", market="US", enabled=True, sink=coordinator)
    orders, responses = inputs()
    result = adapter.observe_cycle(orders=orders, responses=responses)
    assert result.state == "OBSERVED" and result.persistence_confirmed
    assert not result.economic_ingestion_allowed and not result.operational_trading_allowed
    assert count(db) == 1 and read_head(checkpoint).sequence == 1
    assert coordinator.recover().state == "OBSERVATION_VALIDATED"


def test_duplicate_coordinated_cycle_does_not_advance_checkpoint(db, checkpoint):
    coordinator = coordinated(db, checkpoint)
    first = coordinator.record_cycle(cycle())
    second = coordinator.record_cycle(cycle())
    assert first == second and read_head(checkpoint) == first.head
    assert count(db) == 1


def test_coordinator_is_disabled_by_default(db, checkpoint):
    coordinator = UsObservationCoordinator(store=store(db), checkpoint=checkpoint)
    with pytest.raises(ValueError):
        coordinator.record_cycle(cycle())
    assert count(db) == 0 and read_head(checkpoint) == empty_head()


def test_checkpoint_failure_after_db_commit_blocks_adapter_and_restart(db, checkpoint, monkeypatch):
    coordinator = coordinated(db, checkpoint)
    calls = []
    def failed(*args, **kwargs):
        calls.append(1)
        raise OSError("synthetic checkpoint failure")
    monkeypatch.setattr(checkpoint, "advance", failed)
    adapter = UsObservationAdapter(account_id="us_mock", market="US", enabled=True, sink=coordinator)
    orders, responses = inputs()
    result = adapter.observe_cycle(orders=orders, responses=responses)
    assert result.state == "INCOMPLETE" and not result.allow_sync_continue
    assert not result.persistence_confirmed and count(db) == 1
    assert len(calls) == 1 and read_head(checkpoint) == empty_head()
    with pytest.raises(ValueError):
        coordinator.record_cycle(cycle(stamp=LATER))
    assert coordinated(db, checkpoint).recover().state == "INCOMPLETE"
    assert count(db) == 1 and read_head(checkpoint) == empty_head()


def test_incomplete_receipt_retains_known_db_commit_status(db, checkpoint, monkeypatch):
    coordinator = coordinated(db, checkpoint)
    def failed(*args, **kwargs):
        raise OSError("synthetic external write failure")
    monkeypatch.setattr(checkpoint, "advance", failed)
    receipt = coordinator.record_cycle(cycle())
    assert receipt.state == "INCOMPLETE" and receipt.committed is True
    assert not receipt.economic_writes and count(db) == 1


def test_insert_followed_by_denied_commit_rolls_back_and_leaves_checkpoint(db, checkpoint):
    coordinator = coordinated(db, checkpoint)
    insert_seen, commit_denied = [], []
    def authorize(action, argument, second, database, trigger):
        if action == sqlite3.SQLITE_INSERT and argument == "us_observation_cycles":
            insert_seen.append(True)
        if action == sqlite3.SQLITE_TRANSACTION and argument == "COMMIT" and insert_seen:
            commit_denied.append(True)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    db.set_authorizer(authorize)
    try:
        receipt = coordinator.record_cycle(cycle())
    finally:
        db.set_authorizer(None)
    assert insert_seen and commit_denied
    assert receipt.state == "INCOMPLETE" and receipt.committed is False
    assert count(db) == 0 and not db.in_transaction
    assert read_head(checkpoint) == empty_head()
    with pytest.raises(ValueError):
        coordinator.record_cycle(cycle())


def test_reopen_after_both_commits_matches_checkpoint(db, checkpoint):
    receipt = coordinated(db, checkpoint).record_cycle(cycle())
    path = db.execute("PRAGMA database_list").fetchone()[2]
    with sqlite3.connect(Path(path).as_uri() + "?mode=rw", uri=True) as reopened:
        coordinator = coordinated(reopened, checkpoint)
        result = coordinator.recover()
        assert result.state == "OBSERVATION_VALIDATED" and result.head == receipt.head
        second = coordinator.record_cycle(cycle(qty="5", price="101", stamp=LATER))
        assert second.state == "OBSERVED" and read_head(checkpoint) == second.head


def test_coordinator_lock_contention_latches_without_db_write(db, checkpoint):
    coordinator = coordinated(db, checkpoint)
    competitor = ObservationCheckpointFile(path=checkpoint.path, journal_id=JOURNAL, binding_id=BINDING)
    with competitor.exclusive():
        receipt = coordinator.record_cycle(cycle())
        assert receipt.state == "INCOMPLETE" and receipt.committed is False
    assert count(db) == 0 and read_head(checkpoint) == empty_head()
    with pytest.raises(ValueError):
        coordinator.record_cycle(cycle())


def test_conflict_is_checkpointed_but_never_unblocks(db, checkpoint):
    coordinator = coordinated(db, checkpoint)
    coordinator.record_cycle(cycle())
    receipt = coordinator.record_cycle(cycle(qty="1", stamp=LATER))
    assert receipt.state == "CONFLICT" and receipt.committed and receipt.conflicts
    assert read_head(checkpoint) == receipt.head and count(db) == 2
    assert coordinated(db, checkpoint).recover().state == "CONFLICT"
    with pytest.raises(ValueError):
        coordinator.record_cycle(cycle())


def test_active_store_detects_a_changed_db_head_before_write(db):
    first = ready(db)
    second = ready(db)
    first.record_cycle(cycle())
    with pytest.raises(ValueError, match="head changed"):
        second.record_cycle(cycle(qty="5", price="101", stamp=LATER))
    assert count(db) == 1


def test_checkpoint_mismatch_never_adopts_database_head(db, checkpoint):
    ready(db).record_cycle(cycle())
    result = coordinated(db, checkpoint).recover()
    assert result.state == "INCOMPLETE" and not result.anchor_verified
    assert read_head(checkpoint) == empty_head() and count(db) == 1
