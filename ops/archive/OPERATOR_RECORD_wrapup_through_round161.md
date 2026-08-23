# Operator Record Wrap-up Through Round 161

Date: 2026-08-21  
Repository: `C:\auto\작업7차\kiwoom-autotrade`

## Purpose and scope

This document summarizes the verified progress through Round 161 and provides a safe handoff for the next AI. It is an operator record, not authorization to trade, restart processes, change firewall/WFP policy, remove files, stage unrelated work, or commit changes.

The current worktree is intentionally dirty. Preserve all unrelated modified and untracked files.

## Executive status

- Phase 2 verification (v1-v12 and v110) was closed across Rounds 146-149.
- Phase 3 began in Round 150 with two findings: recurring `broker_quantity_unattributed` incidents without a direct automatic-clear path, and a dashboard account-routing exposure through `?account=`.
- Rounds 151-153 mapped the dashboard exposure and drafted the safe Option B fix.
- Round 154 applied a dashboard-only live-account guard to `dashboard/dashboard_server.py`.
- Rounds 155-159 performed controlled dashboard activation and runtime checks. No worker restart or real-account request was performed.
- Round 160 passed the fresh runtime gate, then the commit gate failed because Git could not create `.git/index.lock` (`Permission denied`). No files were staged and no commit was made.
- Round 161 was interrupted before its requested read-only lock diagnosis began. No lock was deleted or modified.

## Verified implementation and evidence

### Dashboard live-account guard

Before the fix, only `/api/start` had a real mode guard. The following routes could otherwise accept a catalog account selected through the dashboard:

- `/api/events`
- `/api/balance`
- `/api/trades`
- GET and POST `/api/settings`
- `/api/control`
- `/api/stop`

Round 154 added `_reject_real_account` and guards at the six `_account` callers plus the inline stop handler. The exact existing error response is preserved:

```json
{"error": "Live accounts are disabled by the dashboard"}
```

The guard returns HTTP 403 unless `ALLOW_LIVE_DASHBOARD=true`. The change was verified as 22 insertions and 0 deletions. Syntax/AST validation passed. `py_compile` could not write `__pycache__` because of a permission error; independent AST/compile validation passed instead.

No request was sent to `kr_real` or `us_real`, and no live-dashboard HTTP test was performed.

### Dashboard runtime checks

- The dashboard was initially absent during the Round 155 check.
- Later controlled activation produced PID 19244.
- Rounds 157-159 observed a listener through `netstat`, two dashboard threads, and no crash evidence.
- `Get-NetTCPConnection` produced a false-negative in Round 159 while `netstat` showed the listener; use `netstat` as the corroborating check.
- No socket-option evidence was found in `dashboard_server.py`.
- The latest check during creation of this record showed PIDs 5372, 9980, and 19244 responding, but did not show `:8765` in the latest netstat snapshot. Recheck the listener before any future commit or runtime operation.

Do not kill or restart PID 19244, and do not restart workers 5372 or 9980, without separate explicit authorization.

### Broker HTTP lock review

`src/core/broker_http.py` uses one `asyncio.Lock` in `BrokerHTTPGate`; both `client()` and `close()` use that same lock. The reviewed request-failure and retry paths release their async context before retrying or reporting the error. `close()` is reached through normal `KiwoomClient.close()` shutdown via `TokenManager.revoke`, not from the reviewed failure/retry cleanup paths.

The actual `_post_once` signature has one `self` parameter. Any earlier statement describing duplicate `self` parameters was a transcription error and should not be reused as evidence.

### Account-balance monitor

The monitor starts only when `_enabled_symbol_configs` has no wanted configurations. The current `kr_mock` configuration has enabled auto buy/sell for `033320`, so the monitor is inactive there; `us_mock` has no profile and the monitor is active.

In the sampled Round 149 `us_mock` session, PID 5372 recorded 42 iterations from approximately 15:23:53 through 17:27:31. The maximum cadence was 182 seconds, consistent with the closed-session cap of 180 seconds after scheduling overhead. There was no `Account balance monitor deferred:` message and no `RetryableError`/`RetryError` evidence. One `WinError 10048` warning was followed by lock release and successful HTTP I/O. That sample did not establish a monitor stall.

### Overnight and market-hours behavior

- Dashboard settings are persisted in `dashboard_settings_{account}.json` and loaded at startup.
- `Engine.run()` calls `sync_broker_state(force_balance=True)` before the tick loop and before the market-hours gate.
- The market-hours gate is after controls and reconciliation. It gates quote/strategy/order evaluation, not reconciliation, monitoring, or pause logic.

## Outstanding `broker_quantity_unattributed` issue

Assignments were reviewed at `src/core/engine.py:1554` and `src/core/engine.py:1710`.

The log search found 10,300 `Complete zero balance observed...` lines; 5,175 remained after excluding the current `003480` case. These grouped into 18 episodes separated by more than ten minutes. Counts in the reviewed evidence were:

| Account | Symbol | Count |
|---|---:|---:|
| `kr_mock` | `007820` | 3 |
| `kr_mock` | `024840` | 17 |
| `kr_mock` | `033320` | 1 |
| `kr_mock` | `118990` | 3 |
| `kr_mock` | `226340` | 3 |
| `kr_mock` | `333430` | 21 |
| `kr_mock` | `387690` | 5,085 |
| `kr_mock` | `483350` | 1 |
| `us_mock` | `IREN` | 40 |
| `us_mock` | `KORU` | 1 |
| `us_mock` | `003480` | 5,125, excluded from the historical grouping |

Historical one-off events were often classified as `clean`. `483350` repeatedly appeared as `manual_review_required` because `unresolvedOrders=True`.

`orphan_cleanup.py:75-100` classifies unresolved orders as `manual_review_required`. `sweep()` at approximately lines 110-129 resets zero confirmations but does not call `_apply` for that classification. There is therefore no verified automatic-clear path for the retained manual-review state. Do not invent a clear operation or bypass this state.

## Dashboard account-routing finding

`dashboard/index.html` derives `activeAccount` from local storage and gives the URL query parameter precedence. Catalog validation checks the account ID, and the request wrapper appends `account` to `/api/*` requests. `dashboard_server.py::_account` accepts any catalog-known account.

The current routing change is uncommitted and mixed with other worktree changes. Do not broaden it or treat account IDs shown in examples as authorization. Real-account access remains disabled by the new server-side guard unless the explicit environment override is present.

## Current Git and worktree state

`dashboard/dashboard_server.py` remains modified and unstaged. The Round 160 command:

```powershell
git add dashboard/dashboard_server.py
```

failed with:

```text
fatal: Unable to create 'C:/auto/작업7차/kiwoom-autotrade/.git/index.lock': Permission denied
```

No files were staged and no commit was created. The worktree also contains unrelated modified and untracked files; do not use broad staging, reset, clean, or commit commands.

## Instructions for the next AI

1. Read this document and the prior operator record before acting.
2. Begin with read-only diagnosis of the Git lock/permission problem. Do not delete, move, or overwrite `.git/index.lock`; do not retry `git add`; do not commit.
3. Check, using read-only commands:
   - `.git\index.lock` existence and metadata with `Test-Path` and `Get-Item`.
   - File and directory permissions with `icacls` and `Get-Acl`.
   - Current identity with `whoami`.
   - `git`/`git-lfs` processes and relevant IDE processes.
   - `git status --short` and `git log -1 --oneline`.
   - Defender/antivirus evidence if available.
4. If a future commit is explicitly authorized after the permission issue is understood, first require a fresh `netstat` listener check and confirmation that workers 5372 and 9980 are unchanged.
5. Stage only `dashboard/dashboard_server.py`, then verify the staged diff is exactly 22 insertions and 0 deletions before committing only that file.
6. Do not restart or kill the dashboard/workers, access `kr_real`/`us_real`, change firewall/WFP policy, or alter the unresolved-order safety state without separate explicit authorization.
7. Preserve every unrelated dirty or untracked file.

## Verification criteria for this wrap-up

- `Get-Content OPERATOR_RECORD_wrapup_through_round161.md` displays this handoff.
- `git diff --check -- OPERATOR_RECORD_wrapup_through_round161.md` returns no errors.
- `git status --short -- OPERATOR_RECORD_wrapup_through_round161.md` shows only the new untracked file.

## Out of scope

- Resolving or automatically clearing `broker_quantity_unattributed`.
- Diagnosing or repairing `.git/index.lock` in this document-creation task.
- Restarting processes, sending broker requests, modifying firewall/WFP rules, staging, or committing code.
