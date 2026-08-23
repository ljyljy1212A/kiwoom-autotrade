# Operator Record — Wrap-up Through Round 183

Repository: `C:\auto\작업7차\kiwoom-autotrade`

Date: 2026-08-21 (Asia/Seoul)

## Scope and authorization

Rounds 177–183 continued the controlled `us_mock` fixed-port HTTP/WebSocket investigation. The attached Round 183 instructions authorized removal of Round 182-only logging instrumentation, a restart of `us_mock`, and test execution. The Round 183 addendum authorized reporting and reconciliation only; it explicitly prohibited further restart, code changes, and commits.

No real account was accessed. `kr_real`, `us_real`, dashboard controls, firewall/WFP settings, ACLs, and broker safety-state controls were not changed.

## Verified progress

### Fixed-port HTTP implementation

The current working-tree content of `src/core/broker_http.py` is byte-identical to blob `40014d7`, the pre-instrumentation state identified during Round 182.

Verified commands and output:

```text
git hash-object -- src/core/broker_http.py
40014d744b8f0530015ca3d2426925b18e84606a

git cat-file blob 40014d7 | git hash-object --stdin
40014d744b8f0530015ca3d2426925b18e84606a
```

The current diff against `HEAD` contains the pre-existing fixed-port changes only:

- `SO_LINGER(1, 0)` support through `_LingerOnCloseByteStream`.
- `_CloseCompletionState` and the `connection_lost` completion hook.
- `_connect_lock` and the close-completion wait boundary.
- `_FIXED_PORT_CLOSE_WAIT_TIMEOUT_SEC = 1.0`.
- `keepalive_expiry=30.0`.

Round 182-specific `Fixed-port close instrumentation` logging and logger plumbing were removed. A source search returned no remaining instrumentation matches.

`git diff --check -- src/core/broker_http.py` reported no whitespace errors. Git emitted only its existing LF/CRLF conversion warning.

### `us_mock` lifecycle

The previously running worker PID `6116` was stopped as authorized by Round 183. The supervisor reported it already stopped when the stop command completed. `us_mock` was then started once from the reverted working tree.

Current verified state:

```json
{"account":"us_mock","pid":20164,"running":true,"instanceId":"f549c782d5274d098859ffbf5ae9218c","startedAt":"2026-08-21T11:19:43.023019+00:00","state":"RUNNING","market":"US"}
```

New-PID startup evidence included successful token issuance, REST reads, clean zero-holding orphan audits, WebSocket login, and registration of `SOXL` market subscriptions. The old log area still contains historical Round 182 instrumentation entries under PID `6116`; those are historical log records, not current source or new-PID output.

### Test verification and baseline reconciliation

Round 183 initially ran this narrower unittest discovery command:

```powershell
$env:PYTHONPATH='.'; python -m unittest discover -s tests -p 'test_*.py'
```

Result:

```text
Ran 95 tests in 18.950s

OK (skipped=4)
```

The historical full-suite convention is pytest with cache-provider disabled:

```powershell
$env:PYTHONPATH='C:\auto\작업7차\kiwoom-autotrade'; pytest -q -p no:cacheprovider
```

This was rerun after the revert. Complete result:

```text
...................s......
..........................ss..........
...... [ 60%]
...................................s.........                            [100%]
============================== warnings summary ===============================
tests/test_manual_tranche_lifecycle.py: 10 warnings
  C:\Users\jhkhjk\AppData\Roaming\Python\Python314\site-packages\pandas_market_calendars\market_calendar.py:145: UserWarning: ['break_start', 'break_end'] are discontinued, the dictionary `.discontinued_market_times` has the dates on which these were discontinued. The times as of those dates are incorrect, use .remove_time(market_time) to ignore a market_time.
    self._prepare_regular_market_times()

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
111 passed, 4 skipped, 10 warnings, 2 subtests passed in 20.40s
```

The discrepancy is explained by different runners and discovery behavior: unittest reported 95 tests, while the project’s pytest baseline covered 111 passing tests. The baseline remains passing. The historical record said 11 warnings; the current run produced 10 warnings, with no failures.

## Current dirty-worktree state

The worktree was already substantially dirty before this record was created. Existing modified files include:

- `dashboard/index.html`
- `src/core/broker_http.py`
- `src/core/engine.py`
- `src/core/kiwoom_client.py`
- `src/core/realtime_feed.py`
- `src/core/token_manager.py`
- `src/data/trade_ledger.py`
- `src/main.py`
- `tests/test_manual_tranche_lifecycle.py`

There are also many pre-existing untracked handoffs, proposals, diagnostics, scripts, and tests. Preserve them. Do not use broad `git add`, reset, clean, stash, or commit commands.

This wrap-up file is newly created and is not staged or committed.

## Open issues and restrictions

- The root cause of recurring `us_mock` fixed-port HTTP `WinError 10048` and separate WebSocket `WinError 10013` symptoms remains unresolved.
- Keep HTTP local port `443` evidence separate from WebSocket local port `10002` evidence.
- Do not merge historical `TIME_WAIT` evidence with later `CLOSE_WAIT`/deferred-close evidence without new proof.
- Do not attribute remaining symptoms to keepalive eviction without new direct evidence.
- Do not bypass `broker_quantity_unattributed`, `manual_review_required`, reconciliation pauses, or other fail-closed safety states.
- Do not touch `kr_real`, `us_real`, live credentials, real orders, dashboard controls, firewall/WFP configuration, ACLs, or registry settings without separate explicit authorization.
- Do not restart or kill any worker merely from stale status data. Corroborate process identity, mutex/lock state, logs, TCP state, and fresh broker evidence.

## Instructions for the next AI

1. Read this record and `OPERATOR_RECORD_wrapup_through_round176.md` before acting.
2. Begin with a read-only baseline: `git status --short`, supervisor status for the explicitly scoped account, relevant process identity, and fresh log evidence.
3. Leave PID `20164` running unless a later instruction explicitly authorizes an operational change.
4. Treat the fixed-port implementation as verified through the current pytest baseline; do not modify it without a new proposal and authorization.
5. If investigating the socket symptom, record timestamp, endpoint, local port, process identity, TCP state, and whether evidence is HTTP or WebSocket.
6. If a staged check says any deviation stops the sequence, capture the baseline and stop. Do not retry, normalize, or create duplicate trading state.
7. Keep any future implementation surgical and path-scoped. Do not stage or commit this record unless explicitly requested.

## Out of scope

- Round 184 mitigation design or implementation.
- Further worker restarts or live verification.
- Firewall/WFP, ACL, registry, routing, or socket-policy changes.
- Real-account operation or order activity.
- Automatic clearing or weakening of fail-closed safety states.
- Staging or committing any current worktree changes.
