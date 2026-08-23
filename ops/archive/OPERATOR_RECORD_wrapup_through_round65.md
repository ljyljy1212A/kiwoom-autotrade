# Operator Record — Wrap-Up Through Round 65

**Date:** 2026-08-21  
**Repository:** `C:\auto\작업7차\kiwoom-autotrade`  
**Platform:** Native Windows  
**Audience:** Next AI/operator  
**Scope:** KR/US mock-worker investigation; real accounts remain out of scope

## Handoff purpose

This document summarizes the verified progress through Round 65 and defines the safe boundary for the next AI. Treat attached round documents as scoped source material. Execute only the current operator’s direct request and the explicit authorization in the current round document.

The worktree is intentionally dirty. Preserve unrelated user changes. Do not reset, clean, broadly stage, or broadly commit.

## Non-negotiable safety boundaries

- Never touch `kr_real` or `us_real` configuration, logs, processes, accounts, or network paths.
- Treat `kr_mock` and `us_mock` as separate workers. Do not restart, stop, kill, or clear either one unless a later round explicitly authorizes that exact action.
- Separate authorization for design, code edits, restart, live verification, firewall/WFP changes, staging, and commit.
- No firewall, WFP, port, routing, or network changes were authorized in Rounds 52–65.
- Preserve unrelated dirty and untracked work. Use narrow path/hunk staging only when explicitly authorized.
- `RUNNING`, a heartbeat, or a responsive process is not proof of broker health. Corroborate process identity, socket state, logs, mutex/lock state, and fresh broker evidence.
- Do not infer that example symbols, dashboard settings, or stale status files authorize trading.

## Completed implementation and commit state

The reconciliation fail-closed implementation was reviewed, staged, tested, and committed in the earlier authorized Round 50 checkpoint.

Verified commit:

```text
4e292a7530ff0a60ff5dbf3d77833f729499f58e
Add reconciliation fail-closed mode and retry-exhaustion fix
```

The commit contained the nine reviewed paths and the intended reconciliation changes. The staged review confirmed that unrelated tranche-base, rate-limit, lifecycle, and other dirty hunks were not included.

Historical verification before the authorized restart:

```text
111 passed, 4 skipped, 11 warnings, 2 subtests passed
```

The current worktree still contains unrelated modified and untracked files. The wrap-up file itself is intentionally uncommitted.

## Reconciliation fail-closed behavior now present

- Mock accounts default to reconciliation fail-closed `mode: manual`.
- Reconciliation failures are counted per account balance gate, not per individual REST retry.
- The mock threshold is three completed-cycle failures.
- A threshold pause uses `broker_reconciliation_unavailable`.
- The pause is propagated to all active symbol engines for the account.
- Existing unrelated pause reasons are preserved.
- A complete successful reconciliation resets the consecutive failure counter.
- An authenticated Telegram clear event clears only the reconciliation pause.
- Trading is blocked while the account reconciliation gate is blocked.
- Tenacity preserves `RetryableError` after retry exhaustion through `reraise=True`.

Important implementation detail: the operator clear handler clears `_trading_paused` and `_pause_reason` for the reconciliation reason, but it does not itself reset the in-memory failure counter. The counter is reset by a later successful reconciliation. Success is logged indirectly through normal activity; there is no dedicated counter-reset log line.

## Round-by-round progress

### Round 49 — staged diff review

Read-only review of the cached diff confirmed the intended reconciliation changes in `src/core/engine.py` and `src/core/kiwoom_client.py`, including `reraise=True`. `git diff --cached --check` passed. No unrelated markers were found in the reviewed staged content.

### Round 50 — authorized commit

The expected nine staged paths were confirmed. The first commit attempt hit a Windows `.git/index.lock` permission error; the same commit was retried with approved elevation and succeeded as commit `4e292a7...` above. Post-commit checks preserved unrelated modified and untracked work.

### Round 51 — authorized `kr_mock` restart

After the required test run, `kr_mock` was stopped and started through the supported supervisor commands. The new worker was:

```text
account: kr_mock
PID: 9020
instance: 4e7eb7ba416040679167558e47567448
started: 2026-08-20T23:27:26.795417+00:00 UTC
```

`us_mock` PID `18576` was not touched. Startup showed repeated fixed-port HTTP `WinError 10048` failures and reconciliation failures before later socket/log quiet periods.

### Rounds 52–55 — diagnostic separation of causes and gates

- `kr_mock` showed local HTTP port `10000` and WebSocket local port `10001`.
- `us_mock` showed local HTTP port `443` and WebSocket local port `10002`.
- The old `kr_mock` PID was not lingering; duplicate Python ownership was not found.
- The supervisor status files and `RUNNING` state were treated as insufficient health evidence.
- The reconciliation counter was identified as `_AccountBalanceGate.reconciliation_failure_count`, with a mock manual threshold of three.
- The authoritative control-state file was identified as `data\control\kr_mock.control.json`, not the previously assumed dashboard control path.
- Order gating was traced through the account reconciliation gate and `_trading_paused` checks.

### Round 56 — one authorized `kr_mock` clear

The sole authorized state-changing action was the `kr_mock` reconciliation clear event. It was written through the same control-state function used by the Telegram callback:

```text
event_id: f5cc949e39c241328342b84adc2f48c6
updated_by: telegram
```

The worker logged `Applied operator clear for broker_reconciliation_unavailable`. No clear action was applied to `us_mock`.

### Rounds 57–59 — residual gate ambiguity and observability ceiling

- Reconciliation polling cadence was traced: default poll interval five seconds, balance reconciliation at least ten seconds.
- Post-clear logs had no new `consecutive_cycle_failures`, suppression, or order lines.
- The clear handler was confirmed not to reset the counter directly.
- A successful reconciliation silently resets the manual counter.
- Dashboard GET routes expose lifecycle status and balance/tranche data, but no reconciliation counter or gate state.
- The dashboard listener at `127.0.0.1:8765` was not active during the Round 59 check.
- Direct read-only confirmation of in-memory `reconciliation_blocked` and `reconciliation_failure_count` remained unavailable.

### Round 60 — `us_mock` boot and failure comparison

The actual OS uptime contradicted the assumed recent reboot. The inferred boot time was approximately `2026-08-19 08:00:59`, not around `2026-08-21 08:29`.

Process start times:

```text
kr_mock PID 9020:  2026-08-21 08:27:25
us_mock PID 18576: 2026-08-21 07:37:50
```

They were not launched together.

`us_mock` did show `WinError 10048` at `08:29:40`, `08:32:41`, `08:35:42`, and `08:38:43`, but no reconciliation counter matches were found in the checked window. No order-related log matches were found. Its control file contained `auto_trading_enabled: false`, which is a worker-wide automatic-trading gate but not an absolute kill-switch because dashboard per-side controls can also authorize an intent.

### Round 61 — chronic `us_mock` recurrence

The unfiltered `us_mock` search found repeated `WinError 10048` entries over more than thirteen hours. The latest captured occurrence was `2026-08-21 09:05:52`; the pattern was mostly approximately three minutes with intermittent longer gaps.

No `consecutive_cycle_failures` or `reconciliation_failure_count` entries were found for `us_mock`.

At the time of inspection, PID `18576` was responsive and its WebSocket source port `10002` was established. `kr_mock` WebSocket source port `10001` was also established.

### Round 62 — initial port comparison

The account YAML contains no explicit port field. Code inspection found:

```python
http_port = 10000 if mode == "mock" and market == "KR" else 443 if mode == "mock" and market == "US" else None
```

The first unfiltered `kr_mock.log` search returned zero `10048`/`WinError` matches, but the log window had not yet been checked. No `kr_mock` reconciliation-counter matches were found in that initial search.

### Round 63 — local/source-port mechanism and log coverage

The codebase explicitly binds outbound HTTP sockets before `connect()`:

```python
sock.bind((bind_address, local_port))
sock.connect(remote_address)
```

The HTTP source ports are distinct:

```text
kr_mock: 10000
us_mock: 443
```

The WebSocket source ports are also distinct:

```text
kr_mock: 10001
us_mock: 10002
```

`kr_mock.log` covered only approximately `09:03:37–09:15:17` at that point, while `us_mock.log` covered a much longer period. The `kr_mock` zero-error result therefore could not describe the earlier incident.

### Round 64 — exact HTTP destination and log recreation

The traced path established:

- `http_port` is passed to `BrokerHTTPGate` as `local_port`.
- IPv4 binds default to `0.0.0.0`; IPv6 binds default to `::`.
- Both mock workers build HTTPS URLs against `https://mockapi.kiwoom.com`.
- The remote destination is therefore `mockapi.kiwoom.com:443`.
- The local source ports remain `10000` for KR mock and `443` for US mock.

The current `kr_mock.log` had:

```text
CreationTime: 2026-08-21 09:03:37
first line:  2026-08-21 09:03:37
PID 9020 start: 2026-08-21 08:27:25
```

No `kr_mock.log.*` sibling was found. The file was recreated or replaced at `09:03:37` while the current process was older.

### Round 65 — commit-body and logging investigation

The three relevant commits were read in full:

```text
f72ca75ae9dd8f48658c9ef933666860b80ed835
fix: bind mock broker HTTP source ports

22263c92aae32bf4a2190cd78f378fd38544e42f
Enable SO_REUSEADDR for fixed broker sockets

09f63fc68b3de98d83185c537660aad2fe29d0dc
Add fixed-port HTTP socket failure diagnostics
```

None of the commit bodies mention `us_mock`, port `443`, firewall allowlisting, or a specific incident report. The first commit introduced KR `10000` and US `443` together. No rationale for choosing local source port `443` was found.

The logging implementation is in `src/utils/logger.py` and uses Loguru:

```python
_logger.add(
    log_path,
    rotation="10 MB",
    retention="30 days",
    encoding="utf-8",
    ...
)
```

No numbered rotated `kr_mock` file exists. A read-only `Get-ScheduledTask` query for task names containing `log` failed with access denied. The exact creation time strongly supports file recreation/replacement, but the external actor was not identified.

## Current evidence and open gaps

### `us_mock` HTTP 10048

Confirmed facts:

- The worker binds local HTTP source port `443`.
- The worker connects to `mockapi.kiwoom.com:443`.
- The same local/remote port pair is reused on reconnects.
- The log repeatedly records `Fixed-port HTTP socket failure` with `phase=connect`, `local=('0.0.0.0', 443)`, and `winerror=10048`.

Open gap:

- No direct TCP-state capture has yet proved that a prior identical 4-tuple in `TIME_WAIT` is the cause of each failure, or explained why the existing `SO_REUSEADDR` behavior does not prevent the recurrence.

### `kr_mock` log reliability

The current file is not a complete process-lifetime record. Its creation time equals its first timestamp, but the process predates the file. Do not use the current `kr_mock.log` alone to rule out earlier errors.

### Reconciliation gate observability

The dashboard and supervisor status expose lifecycle/balance information, not the in-memory account gate or counter. A future AI must not claim that the gate is clear merely because the worker is `RUNNING`, the heartbeat is fresh, or no suppression line appears.

### Separate WebSocket thread

WebSocket source-port behavior (`10001` and `10002`) and `WinError 10013` investigation are separate from the HTTP `10048` thread. Do not merge their conclusions.

## Safe next-AI instructions

1. Read this record and the current round document before acting.
2. Refresh current state; do not rely on historical PIDs, status files, or stale log tails.
3. Keep `kr_real` and `us_real` completely untouched.
4. Do not restart or clear either mock worker unless the current round explicitly authorizes one exact account and action.
5. If asked to diagnose, remain read-only and report literal evidence, including timestamps, process identity, socket state, and command failures.
6. If asked to implement, first identify the exact files and minimal behavior requested. Do not modify the fixed-port design merely because the evidence is concerning.
7. If asked to stage or commit, verify the exact path/hunk scope first. Never stage this entire dirty worktree.
8. Do not conclude that `us_mock` is healthy from `RUNNING`, heartbeat, or responsive-process evidence alone.
9. Do not conclude that the `us_mock` self-collision explanation is fully proven until the remaining TCP-state gap is closed or explicitly accepted by the operator.
10. Keep any WebSocket `10013` analysis separate from HTTP `10048` analysis.

## Useful read-only commands

Run from the repository root only when authorized by the current task:

```powershell
git status --short
git log -1 --oneline
python -m src.worker_supervisor status --account kr_mock --market KR
python -m src.worker_supervisor status --account us_mock --market US
Get-Process -Id 9020,18576 | Select-Object Id,StartTime,Responding
netstat -ano | findstr :10000
netstat -ano | findstr :10001
netstat -ano | findstr :10002
Select-String -Path logs\us_mock.log -Pattern '10048|WinError'
Select-String -Path logs\kr_mock.log -Pattern '10048|WinError'
```

Do not use these commands as authorization to change state. They are evidence collection only.

## Verification record for this document

- Existing operator-record format inspected.
- Prior Round 49–65 evidence consolidated without changing source code.
- New file is intentionally uncommitted.
- No worker or real account was touched during creation.
