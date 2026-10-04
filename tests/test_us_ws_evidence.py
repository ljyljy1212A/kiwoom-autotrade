"""Synthetic F5 capture, scoped persistence and failure boundaries."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from src.core.realtime_feed import KiwoomRealtimeFeed
from src.data.us_ws_evidence import F5EvidenceJournal, read_f5_observations

ACCOUNT = "test_us_mock"
BROKER = "1234567801"
OBSERVED = datetime(2026, 10, 3, 0, 30, tzinfo=timezone.utc)


def item(**values):
    return {"type": "F5", "name": "US execution", "item": "NVDA",
            "values": {"9201": BROKER, "9203": "000000252", "909": "000000010",
                       "908": "233000", "910": "100.0000", "911": "2", **values}}


def frame(*items):
    return json.dumps({"trnm": "REAL", "data": list(items)})


def create(tmp_path):
    return F5EvidenceJournal.create(tmp_path / "evidence.sqlite", account_id=ACCOUNT, broker_account=BROKER)


def read(journal):
    return read_f5_observations(journal.path, account_id=ACCOUNT, broker_account=BROKER)


class FakeWS:
    def __init__(self, frames):
        self.frames = frames

    def __aiter__(self):
        async def messages():
            for raw in self.frames:
                yield raw
        return messages()


def test_complete_f5_items_and_observation_time_survive_reopen(tmp_path):
    journal = create(tmp_path)
    raw_item = item(extra="untouched", execution_date="untrusted")
    wire = frame(raw_item, {"type": "FE", "item": "NVDA", "values": {"22": "20261002"}})
    assert journal.record_frame(wire, observed_at=OBSERVED) == 1
    assert journal.record_frame(wire.encode(), observed_at=OBSERVED) == 1
    reopened = F5EvidenceJournal(journal.path, account_id=ACCOUNT, broker_account=BROKER)
    rows = read(reopened)
    assert len(rows) == 2
    assert rows[0]["raw_event"] == raw_item
    assert rows[0]["observed_at"] == OBSERVED.isoformat()
    assert rows[0]["frame_sha256"] == hashlib.sha256(wire.encode()).hexdigest()
    assert rows[0]["observation_id"] != rows[1]["observation_id"]
    assert all("execution_date" not in row for row in rows)
    assert all("broker_order_date" not in row for row in rows)
    assert "FE" not in journal.path.read_bytes().decode("latin-1")


def test_wrong_account_in_later_item_rejects_entire_frame(tmp_path):
    journal = create(tmp_path)
    with pytest.raises(ValueError, match="account scope"):
        journal.record_frame(frame(item(), item(**{"9201": "9999999901"})), observed_at=OBSERVED)
    assert read(journal) == []


def test_mid_frame_insert_failure_rolls_back_all_observations(tmp_path):
    journal = create(tmp_path)
    with sqlite3.connect(journal.path) as db:
        db.execute("CREATE TRIGGER fail_second BEFORE INSERT ON f5_observations WHEN NEW.item_index=1 BEGIN SELECT RAISE(ABORT,'synthetic insert failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        journal.record_frame(frame(item(), item(**{"909": "000000011"})), observed_at=OBSERVED)
    assert read(journal) == []


@pytest.mark.parametrize("wire", [
    "null", frame({"type": "F5", "values": {}}),
    '{"trnm":"REAL","data":[null]}',
    '{"trnm":"REAL","data":[{"type":"F5","values":{"9201":"1234567801","9201":"1234567801"}}]}',
    '{"trnm":"REAL","data":[{"type":"F5","values":{"9201":"1234567801","911":NaN}}]}',
    '{"trnm":"LOGIN","token":"synthetic-not-a-credential"}',
])
def test_malformed_or_authentication_frames_do_not_write(tmp_path, wire):
    journal = create(tmp_path)
    with pytest.raises(ValueError):
        journal.record_frame(wire, observed_at=OBSERVED)
    assert read(journal) == []


def test_naive_observation_time_is_not_guessed(tmp_path):
    journal = create(tmp_path)
    with pytest.raises(ValueError, match="timezone"):
        journal.record_frame(frame(item()), observed_at=OBSERVED.replace(tzinfo=None))
    assert read(journal) == []


def test_existing_unrelated_database_is_preserved(tmp_path):
    path = tmp_path / "economic.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE trades (qty INTEGER)")
        db.execute("INSERT INTO trades VALUES (5)")
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        F5EvidenceJournal.create(path, account_id=ACCOUNT, broker_account=BROKER)
    with pytest.raises(ValueError, match="Not an F5"):
        F5EvidenceJournal(path, account_id=ACCOUNT, broker_account=BROKER)
    assert path.read_bytes() == before


def test_reader_does_not_create_missing_evidence_or_modify_existing_journal(tmp_path):
    journal = create(tmp_path)
    journal.record_frame(frame(item()), observed_at=OBSERVED)
    before = journal.path.read_bytes()
    assert len(read(journal)) == 1
    assert journal.path.read_bytes() == before
    absent = tmp_path / "absent.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        read_f5_observations(absent, account_id=ACCOUNT, broker_account=BROKER)
    assert not absent.exists()


def test_scope_mismatch_and_corrupt_evidence_fail_closed(tmp_path):
    journal = create(tmp_path)
    with pytest.raises(ValueError, match="scope"):
        read_f5_observations(journal.path, account_id="other_mock", broker_account=BROKER)
    journal.record_frame(frame(item()), observed_at=OBSERVED)
    with sqlite3.connect(journal.path) as db:
        db.execute("UPDATE f5_observations SET observed_at='missing timezone'")
    with pytest.raises(ValueError):
        read(journal)


def test_feed_capture_is_explicit_and_does_not_decode_fills(tmp_path):
    async def scenario():
        journal = create(tmp_path)
        feed = KiwoomRealtimeFeed(SimpleNamespace(mode="mock", market="US"), f5_evidence_journal=journal)
        calls = []
        feed.add_doorbell_callback(lambda: calls.append("doorbell"))
        await feed._receive_loop(FakeWS([frame(item(**{"910": "0", "911": "bad"}))]))
        assert calls == ["doorbell"]
        assert feed._cache == {}
        assert len(read(journal)) == 1
        assert feed.f5_evidence_status["state"] == "configured"
    asyncio.run(scenario())


def test_capture_error_is_latched_without_retry_and_doorbells_still_work():
    async def scenario():
        class FailedJournal:
            calls = 0
            def record_frame(self, raw, *, observed_at):
                self.calls += 1
                raise OSError("synthetic disk failure")
        journal = FailedJournal()
        feed = KiwoomRealtimeFeed(SimpleNamespace(mode="mock", market="US"), f5_evidence_journal=journal)
        calls = []
        feed.add_doorbell_callback(lambda: calls.append("doorbell"))
        await feed._receive_loop(FakeWS([frame(item()), frame(item())]))
        assert journal.calls == 1
        assert len(calls) == 2
        assert feed.f5_evidence_status == {"state": "INCOMPLETE", "error_type": "OSError"}
    asyncio.run(scenario())


def test_default_feed_does_not_capture_and_non_us_attachment_is_rejected(tmp_path):
    async def scenario():
        feed = KiwoomRealtimeFeed(SimpleNamespace(mode="mock", market="US"))
        await feed._receive_loop(FakeWS([frame(item())]))
        assert feed.f5_evidence_status["state"] == "disabled"
        assert list(tmp_path.iterdir()) == []
    asyncio.run(scenario())
    with pytest.raises(ValueError, match="US evidence"):
        KiwoomRealtimeFeed(SimpleNamespace(mode="mock", market="KR"), f5_evidence_journal=create(tmp_path))

def test_mixed_quote_and_f5_frame_preserves_price_cache_and_raw_account_event(tmp_path):
    async def scenario():
        journal = create(tmp_path)
        feed = KiwoomRealtimeFeed(SimpleNamespace(mode="mock", market="US"), f5_evidence_journal=journal)
        original = item(**{"10": "9999"})
        quote = {"type": "0B", "item": "NVDA", "values": {"10": "201.5000"}}
        calls = []
        feed.add_doorbell_callback(lambda: calls.append("doorbell"))
        await feed._receive_loop(FakeWS([frame(original, quote)]))
        assert calls == ["doorbell"]
        assert feed.get_cached("NVDA", 10) == 201.5
        assert read(journal)[0]["raw_event"] == original
    asyncio.run(scenario())


@pytest.mark.parametrize("malformed", ["{", "null", '{"trnm":"REAL","data":{}}', '{"trnm":"REAL","data":[null]}'])
def test_malformed_frame_latches_capture_gap_and_does_not_retry(tmp_path, malformed):
    async def scenario():
        journal = create(tmp_path)
        feed = KiwoomRealtimeFeed(SimpleNamespace(mode="mock", market="US"), f5_evidence_journal=journal)
        calls = []
        feed.add_doorbell_callback(lambda: calls.append("doorbell"))
        await feed._receive_loop(FakeWS([malformed, frame(item())]))
        assert feed.f5_evidence_status == {"state": "INCOMPLETE", "error_type": "MalformedWebSocketFrame"}
        assert read(journal) == []
        assert calls == ["doorbell"]
    asyncio.run(scenario())


def test_timezone_conversion_preserves_observation_instant_without_execution_date(tmp_path):
    from datetime import timedelta
    journal = create(tmp_path)
    received = datetime(2026, 10, 3, 9, 30, tzinfo=timezone(timedelta(hours=9)))
    journal.record_frame(frame(item()), observed_at=received)
    record = read(journal)[0]
    assert record["observed_at"] == OBSERVED.isoformat()
    assert "execution_date" not in record


def test_eight_digit_configured_account_requires_exact_event_identity(tmp_path):
    number = "12345678"
    journal = F5EvidenceJournal.create(tmp_path / "eight.sqlite", account_id=ACCOUNT, broker_account=number)
    journal.record_frame(frame(item(**{"9201": number})), observed_at=OBSERVED)
    assert len(read_f5_observations(journal.path, account_id=ACCOUNT, broker_account=number)) == 1
    with pytest.raises(ValueError, match="account scope"):
        journal.record_frame(frame(item(**{"9201": number + "01"})), observed_at=OBSERVED)
    assert len(read_f5_observations(journal.path, account_id=ACCOUNT, broker_account=number)) == 1


@pytest.mark.parametrize("number", ["1234567", "123456789", "12345678-01", "abcdefgh"])
def test_invalid_account_format_does_not_create_journal(tmp_path, number):
    path = tmp_path / "invalid.sqlite"
    with pytest.raises(ValueError, match="broker account scope"):
        F5EvidenceJournal.create(path, account_id=ACCOUNT, broker_account=number)
    assert not path.exists()


@pytest.mark.parametrize("malformed", [
    '{"trnm":"REAL","trnm":"REG","data":[{"type":"F5"}]}',
    '{"trnm":"REAL","data":[{"type":"F5"}],"data":[]}',
    '{"trnm":"REAL","data":[{"type":"F5","type":"0B","item":"NVDA","values":{"10":"9999"}}]}',
    '{"trnm":"REAL","data":[{"type":"0B","type":"F5"}]}',
    '{"trnm":"REAL","data":[{"type":"0B","values":{"10":NaN}}]}',
    '{"trnm":"REAL","data":[],"extra":Infinity}',
    '{"trnm":"REAL","data":[],"extra":-Infinity}',
    '{"trnm":"REAL"}',
    b"\xff",
])
def test_strict_receive_validation_cannot_hide_f5_or_a_capture_gap(tmp_path, malformed):
    async def scenario():
        journal = create(tmp_path)
        feed = KiwoomRealtimeFeed(SimpleNamespace(mode="mock", market="US"), f5_evidence_journal=journal)
        calls = []
        feed.add_doorbell_callback(lambda: calls.append("doorbell"))
        await feed._receive_loop(FakeWS([malformed, frame(item())]))
        assert feed.f5_evidence_status == {"state": "INCOMPLETE", "error_type": "MalformedWebSocketFrame"}
        assert read(journal) == []
        assert feed._cache == {}
        assert calls == ["doorbell"]
    asyncio.run(scenario())


@pytest.mark.parametrize("capture", [False, True])
def test_f5_never_becomes_a_quote_when_doorbell_configuration_excludes_it(tmp_path, monkeypatch, capture):
    monkeypatch.setenv("KIWOOM_WS_DOORBELL_TYPES", "")
    async def scenario():
        journal = create(tmp_path) if capture else None
        feed = KiwoomRealtimeFeed(SimpleNamespace(mode="mock", market="US"), f5_evidence_journal=journal)
        calls = []
        feed.add_doorbell_callback(lambda: calls.append("doorbell"))
        quote = {"type": "0B", "item": "NVDA", "values": {"10": "201.5"}}
        await feed._receive_loop(FakeWS([frame(quote, item(**{"10": "9999"}))]))
        assert feed.get_cached("NVDA", 10) == 201.5
        assert calls == []
        if journal is not None:
            assert len(read(journal)) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["connect", "CREATE TABLE f5_observations", "INSERT INTO evidence_meta"])
def test_failed_initialization_preserves_reserved_path_without_partial_schema(tmp_path, monkeypatch, failure):
    from src.data import us_ws_evidence as evidence
    path = tmp_path / "failed.sqlite"
    original_connect = evidence._connect
    class FailedInitialization(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if failure in sql:
                raise sqlite3.OperationalError("synthetic initialization failure")
            return super().execute(sql, parameters)
    def failing_connect(path, mode):
        if mode == "rw":
            if failure == "connect":
                raise sqlite3.OperationalError("synthetic connection failure")
            return sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, factory=FailedInitialization)
        return original_connect(path, mode)
    monkeypatch.setattr(evidence, "_connect", failing_connect)
    with pytest.raises(sqlite3.OperationalError, match="synthetic"):
        F5EvidenceJournal.create(path, account_id=ACCOUNT, broker_account=BROKER)
    assert path.is_file()
    before = path.read_bytes()
    db = original_connect(path, "ro")
    try:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    finally:
        db.close()
    with pytest.raises(ValueError, match="Not an F5"):
        F5EvidenceJournal(path, account_id=ACCOUNT, broker_account=BROKER)
    with pytest.raises(FileExistsError):
        F5EvidenceJournal.create(path, account_id=ACCOUNT, broker_account=BROKER)
    assert path.read_bytes() == before


def test_idle_account_registration_repeats_per_connection_and_avoids_quote_duplicates(tmp_path, monkeypatch):
    async def scenario():
        feed = KiwoomRealtimeFeed(SimpleNamespace(mode="mock", market="US"), f5_evidence_journal=create(tmp_path))
        class RegistrationWS:
            def __init__(self):
                self.messages = []
            async def send(self, value):
                self.messages.append(json.loads(value))
        async def end_loop(delay):
            feed._stop.set()
        monkeypatch.setattr(asyncio, "sleep", end_loop)
        idle = RegistrationWS()
        await feed._register_loop(idle)
        assert idle.messages == [{"trnm": "REG", "grp_no": "2", "refresh": "1", "data": [{"item": [""], "type": ["F5"]}]}]
        feed._stop.clear()
        feed.subscribe("NVDA")
        reconnect = RegistrationWS()
        await feed._register_loop(reconnect)
        assert reconnect.messages[0] == idle.messages[0]
        quote_registration = reconnect.messages[1]
        assert quote_registration["grp_no"] == "1"
        assert quote_registration["data"][0]["item"] == ["NVDA"]
        assert "F5" not in quote_registration["data"][0]["type"]
        assert feed.subscribed_symbols() == ("NVDA",)
    asyncio.run(scenario())
