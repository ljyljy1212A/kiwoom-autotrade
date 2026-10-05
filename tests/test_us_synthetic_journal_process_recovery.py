"""Fresh Python process recovery of scratch-only observation journals.

Children receive an explicit minimal environment, not parent credentials or
runtime settings. These cases demonstrate orderly close/reopen, not power-loss
durability, broker evidence or actual worker restart behavior.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


_CHILD = r'''
import json
import os
from pathlib import Path
import sys

from src.data.us_synthetic_observation_journal import (
    SyntheticJournalIdentity, create_synthetic_journal, reopen_synthetic_journal,
)

root, allowed, action = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
date = '20261002'
stamp = '2026-10-04T01:00:00+00:00'
later = '2026-10-04T01:01:00+00:00'
def response(q='2', average='100.0000'):
    return {'return_code': 0, '_execution_pages_complete': True, '_query_order_date': date,
            'result_list': [{'ord_no': '000000042', 'stk_cd': 'AAPL', 'slby_tp_nm': '매수',
                             'crnc_code': 'USD', 'cntr_qty': q, 'cntr_uv': average, 'cntr_time': '10:00:00'}]}
journal = None
try:
    journal = (create_synthetic_journal(root, allow_root=allowed) if action == 'create'
               else reopen_synthetic_journal(root, allow_root=allowed))
    outcome = None
    if action == 'create':
        journal.bind(SyntheticJournalIdentity('first', date, '000000042', 'AAPL', 'BUY', '5', stamp))
        outcome = journal.observe(response(), query_order_date=date, observed_at_utc=stamp)
    elif action == 'advance':
        outcome = journal.observe(response('5', '106.0000'), query_order_date=date, observed_at_utc=later)
    elif action == 'duplicate':
        outcome = journal.observe(response(), query_order_date=date, observed_at_utc=stamp)
    elif action == 'conflict':
        outcome = journal.observe(response(average='101.0000'), query_order_date=date, observed_at_utc=later)
    elif action == 'corrupt':
        journal.db.execute("UPDATE us_cumulative_observations SET amount='201'")
        journal.db.commit()
        print(json.dumps({'pid': os.getpid(), 'state': 'SYNTHETIC_CORRUPTION_INJECTED'}))
        sys.exit(0)
    elif action != 'read':
        raise ValueError('Unknown synthetic child action')
    recovered = journal.recover()
    print(json.dumps({'pid': os.getpid(), 'state': recovered.state,
                      'observations': recovered.observations, 'conflicts': recovered.conflicts,
                      'operational_ingestion_allowed': recovered.operational_ingestion_allowed,
                      'execution_date_status': recovered.execution_date_status,
                      'pending': journal.db.execute('SELECT filled_qty,status FROM pending_orders').fetchall(),
                      'audit_count': journal.db.execute('SELECT COUNT(*) FROM us_cumulative_observation_audit').fetchone()[0],
                      'raw_audits': journal.db.execute('SELECT observation_json FROM us_cumulative_observation_audit').fetchall(),
                      'outcomes': [item.outcome for item in outcome.deltas] if outcome else [],
                      'parent_sentinel_present': 'SYNTHETIC_PARENT_SENTINEL' in os.environ}))
except Exception as exc:
    print(json.dumps({'pid': os.getpid(), 'error': type(exc).__name__, 'message': str(exc)}))
    sys.exit(3)
finally:
    if journal is not None:
        journal.close()
'''


@pytest.fixture
def scratch(tmp_path):
    root = tmp_path / "synthetic-process-journal"
    root.mkdir()
    return root, tmp_path


def run_child(scratch, action, expected_exit=0):
    root, allowed = scratch
    repository = Path(__file__).resolve().parents[1]
    environment = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR") if name in os.environ}
    environment.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
                        "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(repository),
                        "TEMP": str(allowed), "TMP": str(allowed)})
    completed = subprocess.run([sys.executable, "-B", "-c", _CHILD, str(root), str(allowed), action],
                               cwd=repository, env=environment, capture_output=True,
                               encoding="utf-8", timeout=30, check=False)
    assert completed.returncode == expected_exit, (completed.stdout, completed.stderr)
    assert completed.stderr == "", completed.stderr
    result = json.loads(completed.stdout)
    assert result["pid"] != os.getpid()
    return result


def file_hashes(root):
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in root.iterdir() if path.is_file()}


def test_fresh_process_recovery_preserves_latest_unapplied_observation(scratch, monkeypatch):
    monkeypatch.setenv("SYNTHETIC_PARENT_SENTINEL", "synthetic-only")
    created = run_child(scratch, "create")
    advanced = run_child(scratch, "advance")
    before = file_hashes(scratch[0])
    recovered = run_child(scratch, "read")
    assert recovered["observations"] == advanced["observations"]
    assert recovered["observations"][0][4:7] == ["5", "106", "530"]
    assert recovered["pending"] == [[0, "open"]]
    assert recovered["execution_date_status"] == "unresolved"
    assert not recovered["operational_ingestion_allowed"]
    assert not any(item["parent_sentinel_present"] for item in (created, advanced, recovered))
    assert any('"cntr_uv": "106.0000"' in raw[0] for raw in recovered["raw_audits"])
    assert file_hashes(scratch[0]) == before


def test_duplicate_after_process_restart_only_adds_observation_audit(scratch):
    first = run_child(scratch, "create")
    second = run_child(scratch, "duplicate")
    assert second["outcomes"] == ["duplicate"]
    assert second["observations"] == first["observations"]
    assert second["pending"] == first["pending"] == [[0, "open"]]
    assert second["audit_count"] == first["audit_count"] + 1


def test_conflict_survives_separate_process_restart(scratch):
    run_child(scratch, "create")
    conflict = run_child(scratch, "conflict")
    before = file_hashes(scratch[0])
    recovered = run_child(scratch, "read")
    assert recovered["state"] == conflict["state"] == "HELD"
    assert recovered["conflicts"] == conflict["conflicts"] and recovered["conflicts"]
    assert recovered["pending"] == [[0, "open"]]
    assert file_hashes(scratch[0]) == before


def test_corrupt_recovery_in_new_process_refuses_without_file_repair(scratch):
    run_child(scratch, "create")
    run_child(scratch, "corrupt")
    before = file_hashes(scratch[0])
    result = run_child(scratch, "read", expected_exit=3)
    assert result["error"] == "ValueError"
    assert file_hashes(scratch[0]) == before


def test_missing_journal_is_not_created_by_separate_process_reopen(scratch):
    result = run_child(scratch, "read", expected_exit=3)
    assert result["error"] == "OperationalError"
    assert list(scratch[0].iterdir()) == []
