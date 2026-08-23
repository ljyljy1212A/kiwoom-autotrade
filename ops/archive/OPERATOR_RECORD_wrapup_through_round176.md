# Operator Record Wrap-up Through Round 176

Date: 2026-08-21  
Repository: `C:\auto\작업7차\kiwoom-autotrade`

## Purpose and scope

This document summarizes the verified progress through Round 176 and provides a safe English handoff for the next AI. It is an operator record, not authorization to trade, restart or kill processes, access real accounts, change firewall/WFP policy, modify ACLs, stage unrelated work, or commit changes.

The worktree is intentionally dirty. Preserve all unrelated modified and untracked files.

## Executive status

- Phase 2 verification (v1-v12 and v110) was closed across Rounds 146-149.
- Phase 3 investigated recurring `broker_quantity_unattributed` incidents and dashboard account routing.
- The dashboard real-account guard was implemented and committed as `54e49c5` (`dashboard: block real-account access via account-mode guard`). It adds a server-side 403 guard for catalog accounts whose mode is `real`, unless `ALLOW_LIVE_DASHBOARD=true` is explicitly set.
- The dashboard listener `SO_REUSEADDR` fix was implemented and committed as `3d8b81f` (`dashboard: enable SO_REUSEADDR to avoid restart bind conflicts`). It introduces `ReusableThreadingHTTPServer` with `allow_reuse_address = True`.
- The dashboard guard and socket-reuse commits were independently verified. The dashboard was restarted twice, including a back-to-back restart, and continued serving HTTP 200 on port `8765`.
- The `tranche_bases` same-process write race is closed for the supported architecture: one account worker process runs multiple symbol tasks, and a process-global `RLock` protects the account-wide read/merge/write/replace sequence.
- Quote-pipeline instrumentation is source-confirmed and committed in `c045350`. It includes cache-age reporting, a passive 60-second quote-health logger, and WebSocket recovery-gap warnings.
- The installed `httpcore 1.0.9` keepalive-eviction path was traced directly. It calls the wrapped stream's async close, so the leading hypothesis that keepalive eviction bypasses `_LingerOnCloseByteStream` is refuted.

## Current verified runtime state

The latest read-only snapshot showed:

| Component | PID | Start time | State |
|---|---:|---|---|
| `us_mock` worker | `5372` | 2026-08-21 15:23:48 local | Responding |
| `kr_mock` worker | `9980` | 2026-08-21 16:31:04 local | Responding |
| Dashboard | `9976` | 2026-08-21 18:56:15 local | Responding; listening on `127.0.0.1:8765` |

The dashboard GET probe returned `HTTP/1.0 200 OK`. No real-account request was made.

## Verified implementation and evidence

### Dashboard real-account guard

Before `54e49c5`, catalog membership validation did not itself reject real-mode accounts on several dashboard endpoints. The committed change adds `_reject_real_account()` and calls it from the affected read/write handlers and the stop handler. The preserved response is:

```json
{"error": "Live accounts are disabled by the dashboard"}
```

The guard returns HTTP 403 unless `ALLOW_LIVE_DASHBOARD=true`. The committed diff was one file, `dashboard/dashboard_server.py`, with 22 insertions and 0 deletions.

### Dashboard socket reuse

The committed `3d8b81f` change is:

```python
class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
```

The main block now constructs `ReusableThreadingHTTPServer` without changing the bind address, port, or request handler. The back-to-back stop/restart verification succeeded, and the new process listened immediately on `127.0.0.1:8765`.

### `tranche_bases` persistence

Current `src/core/engine.py` uses:

```python
_TRANCHE_BASES_WRITE_LOCK = threading.RLock()
self._tranche_bases_path = self.data_dir / f"tranche_bases_{ctx.account_id}.json"
```

`_store_tranche_base()` and `_remove_tranche_base()` both acquire the lock, reread the account-wide JSON file, merge/remove one symbol, write a unique temporary file, and atomically replace the target. `src/main.py` launches one account worker process and runs per-symbol `AccountEngine` instances as asyncio tasks in that process.

The current test suite includes concurrent multi-engine writes and concurrent delete/write coverage in `tests/test_tranche_base_persistence.py`. The full suite result was `111 passed, 4 skipped, 11 warnings`.

Conclusion: the originally described same-process clobber race is closed for the supported `kr_mock` architecture. Do not reopen this item unless the process architecture changes or new direct evidence shows multiple independent processes sharing the same account file.

### Quote-pipeline instrumentation

Committed in `c045350` (`Add quote-health cache-age accessors and passive monitor`):

- `KiwoomRealtimeFeed.cache_age_sec(symbol)` returns seconds since the last cached tick, or `None` when absent.
- `run_quote_health_monitor()` logs each subscribed symbol's cache age every 60 seconds and takes no corrective action.
- `max_staleness_sec` defaults to `20.0` and a warning is emitted when a newly received tick follows a gap beyond that threshold.

The implementation is source-confirmed. Dedicated tests for these three instrumentation behaviors were not found; no tests were added because that was outside the authorized scope.

### Installed `httpx`/`httpcore` keepalive path

Verified installed versions:

- `httpx 0.28.1`
- `httpcore 1.0.9`

The project uses `httpcore.AsyncConnectionPool` with `keepalive_expiry=30.0`. In the installed source, expired connections are removed by `_async/connection_pool.py`, then closed through:

```text
AsyncConnectionPool._close_connections()
 -> AsyncHTTPConnection.aclose()
 -> AsyncHTTP11Connection.aclose()
 -> AnyIOStream.aclose()
 -> _LingerOnCloseByteStream.aclose()
```

The wrapper applies `SO_LINGER(1, 0)` to the raw socket before delegating to the underlying stream close. Therefore, the hypothesis that idle keepalive eviction bypasses the wrapper is refuted.

## Outstanding items

### `broker_quantity_unattributed`

This remains a safety-state investigation item. Historical incidents include repeated zero-balance observations and states classified as `manual_review_required` when unresolved orders exist. Do not invent an automatic clear path, bypass manual review, or alter pause state without a separately authorized proposal and verification plan.

### `us_mock` fixed-port HTTP/WS symptoms

The historical `WinError 10013`/`10048` issue remains unresolved as a root-cause investigation despite:

- fixed-port `SO_REUSEADDR` in broker HTTP and WebSocket socket creation (`22263c9`),
- `_LingerOnCloseByteStream` with `SO_LINGER(1, 0)`, and
- direct confirmation that installed `httpcore` keepalive eviction reaches the wrapper.

Do not attribute the remaining symptom to keepalive eviction without new direct evidence. Future diagnostics must distinguish HTTP port `443` from WebSocket port `10002`, and must preserve the time-specific distinction between prior `TIME_WAIT` evidence and later `CLOSE_WAIT`/deferred-close evidence.

### Dirty worktree

The latest status includes unrelated modified files such as:

- `dashboard/index.html`
- `src/core/broker_http.py`
- `src/core/engine.py`
- `src/core/kiwoom_client.py`
- `src/core/realtime_feed.py`
- `src/core/token_manager.py`
- `src/data/trade_ledger.py`
- `src/main.py`
- `tests/test_manual_tranche_lifecycle.py`

There are also many unrelated untracked handoffs, proposals, diagnostics, scripts, and tests. Do not use broad `git add`, reset, clean, stash, or commit commands.

## Instructions for the next AI

1. Read this record and the prior operator record before acting.
2. Begin every new round with a read-only baseline: current PID/status evidence, relevant logs, and `git status --short`.
3. Keep `kr_real` and `us_real` completely out of scope unless separately authorized.
4. Do not restart or kill workers or the dashboard merely because a status file or PID snapshot looks stale. Corroborate process identity, mutex/lock state, logs, TCP state, and fresh HTTP/broker evidence.
5. Treat `RUNNING` metadata or a PID alone as insufficient proof of broker/dashboard health.
6. Preserve all unrelated dirty and untracked work. Stage only an explicitly authorized file, and use a path-scoped command.
7. Do not modify firewall/WFP rules, ACLs, socket policy, unresolved-order safety state, or real-account controls without separate explicit authorization.
8. For any future `us_mock` fixed-port diagnosis, record the exact endpoint, local port, socket state, timestamp, process identity, and whether the evidence is HTTP or WebSocket.
9. If a staged check says any deviation stops the sequence, capture the baseline and stop. Do not retry, normalize, or create duplicate trading state.

## Verification criteria for this wrap-up

- The file is readable in full with `Get-Content OPERATOR_RECORD_wrapup_through_round176.md`.
- `git diff --check -- OPERATOR_RECORD_wrapup_through_round176.md` returns no errors.
- `git status --short -- OPERATOR_RECORD_wrapup_through_round176.md` shows only this new untracked file.
- No process, broker, firewall, ACL, or Git state was changed while creating this record.

## Out of scope

- Real-account access or requests (`kr_real`, `us_real`).
- Worker or dashboard restart/kill beyond separately authorized operational checks.
- Automatic clearing of `broker_quantity_unattributed` or `manual_review_required` states.
- New quote-instrumentation tests unless separately authorized.
- Further fixes to the `us_mock` fixed-port/socket lifecycle issue unless separately proposed and authorized.
- Staging or committing this wrap-up or unrelated worktree changes.
