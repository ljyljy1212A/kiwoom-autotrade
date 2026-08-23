# Operator Record — Wrap-Up Through Round 96

**Date:** 2026-08-21  
**Repository:** `C:\auto\작업7차\kiwoom-autotrade`  
**Platform:** Native Windows  
**Audience:** Next AI/operator  
**Scope:** KR/US mock workers, dashboard investigation, and mock dashboard launcher implementation. Real-account workers remain out of scope.

## Handoff purpose

This document summarizes the verified progress after [`OPERATOR_RECORD_wrapup_through_round82.md`](OPERATOR_RECORD_wrapup_through_round82.md), through Round 96. Treat each attached round document as scoped source material. Execute only the current operator request and the explicit authorization in the current round document.

## Non-negotiable safety boundaries

- Never touch `kr_real` or `us_real` configuration, logs, processes, accounts, or network paths unless explicitly authorized.
- Treat `kr_mock` and `us_mock` as separate workers. Do not restart, stop, kill, or clear either one unless a later round explicitly authorizes that exact action.
- Design, code edits, restart, live verification, firewall/WFP changes, staging, and commit each require separate authorization.
- Preserve unrelated dirty and untracked work. Do not reset, clean, broadly stage, or broadly commit.
- A `RUNNING` status, heartbeat, or responsive dashboard is not proof of broker health or successful trading. Corroborate process identity, mutex/lock state, logs, socket state, and fresh broker evidence.
- Do not infer trading authorization from example symbols or stale snapshots.

## Current verified runtime state

The latest supervisor status checks were run from the repository root:

```text
{"account": "kr_mock", "pid": 9020, "running": true, "instanceId": "4e7eb7ba416040679167558e47567448", "startedAt": "2026-08-20T23:27:26.795417+00:00", "state": "RUNNING", "market": "KR"}
{"account": "us_mock", "pid": 18576, "running": true, "instanceId": "d9ef696e92474c74ab01db531b7e24c9", "startedAt": "2026-08-20T22:38:09.221977+00:00", "state": "RUNNING", "market": "US"}
{"account": "kr_real", "pid": 0, "running": false, "instanceId": null, "startedAt": null, "state": null, "market": null}
{"account": "us_real", "pid": 0, "running": false, "instanceId": null, "startedAt": null, "state": null, "market": null}
```

The dashboard backend is listening on `127.0.0.1:8765`, currently PID `10780`. `/api/status` returned HTTP 200 and reported both mock workers running.

## Progress by round

### Rounds 83–84 — watchdog evidence

- The watchdog history and literal evidence were reviewed.
- Three successful restart observations were identified, but no same-worker successful rapid restart pair was established.
- Failed watchdog attempts remain distinct from successful restarts; do not collapse them into one confirmed rapid-restart pattern.

### Rounds 85–87 — Incident A control flow and quantity handling

- `src/core/engine.py` was rechecked around `_adopt_manual_lifecycle()` and `_reconcile_balance()`.
- Manual lifecycle adoption clears the pending-adoption flag, updates the context position quantity/average price, writes lifecycle state, and records an activation ID.
- The reconciliation path calls adoption before its pause branch.
- No reassignment of the local reconciliation `qty` was found between the adoption call and the pause branch.
- No new code change was authorized or made for these investigations.

### Round 88 — tranche-base persistence

- `src/core/engine.py` contains the process-wide `_TRANCHE_BASES_WRITE_LOCK`.
- `_store_tranche_base()` performs the shared `tranche_bases_<account>.json` read-modify-write while holding that lock.
- The current same-process interleaving race was not reproduced; no change was made.

### Round 89 — dashboard balance and metadata

- `dashboard/dashboard_server.py` scopes `/api/balance` by account and overlays authoritative tranche metadata.
- Engine balance snapshots include a symbol for both passive and active snapshots.
- The frontend previously ignored a balance response without `balance.symbol`; the backend response itself was verified separately.

### Rounds 90–91 — dashboard backend start

- Round 90 attempted to start the dashboard with output redirected to `C:\temp`; launch failed because that path was denied.
- Round 91 retried using the existing repository `logs` directory:
  - `logs\dashboard_stdout.log` was empty.
  - `logs\dashboard_stderr.log` was empty.
  - Port `8765` listened as PID `8008`.
  - `/api/balance?account=kr_mock` returned HTTP 200.

### Round 92 — `kr_mock` start request

- The normal start mechanism was identified as:

```text
dashboard\start_kr_mock_worker.bat
"%PYTHON_EXE%" -m src.worker_supervisor start --account kr_mock --market KR
```

- The current supervisor status already reported `kr_mock` running as PID `9020`; no start or restart was attempted.
- No worker other than the status check was touched.

### Round 93 — `kr_mock` automatic-trading flag

- Current account control file:

```text
C:\auto\작업7차\kiwoom-autotrade\data\control\kr_mock.control.json
```

- Current literal value:

```json
{"account":"kr_mock","auto_trading_enabled":true,"updated_at":"2026-08-20T05:32:10.291413+00:00","updated_by":"telegram"}
```

- `src/core/engine.py` reads the persisted value at startup and refreshes it live through `_refresh_runtime_control()`.
- The dashboard control files observed for `003480`, `033320`, and `387690` had `auto_buy=true` and `auto_sell=true` at that inspection point. These are account/symbol controls and must not be generalized to other symbols without fresh evidence.
- No control value was modified.

### Operator request — start `kr_mock` automated trading

- A read-only check found `kr_mock` already running as PID `9020`.
- `auto_trading_enabled` was already `true`.
- No redundant state change was made.

### Round 94 — dashboard account-selection investigation

- The frontend account state was confirmed to use:

```javascript
let activeAccount=localStorage.getItem('kiwoom_active_account') || '';
```

- The account selector is intentionally removed from the visible UI by `renderAccountSelector()`.
- The backend `Handler._account()` accepts a valid `account` query parameter and otherwise falls back to `_default_accounts()`, whose normal default is `kr_mock`.
- The existing `start_dashboard.bat` starts one local dashboard backend on port `8765`; it does not start workers.
- At that time, `us_mock` was running as PID `18576`; `kr_real` and `us_real` were stopped.
- The reviewed design was to add URL-query account selection and mock-only launcher entry points.

### Round 95 — mock dashboard launcher implementation

The following requested changes were applied:

- `dashboard\index.html` now reads `?account=` and gives a valid account query parameter priority over `localStorage` and the automatic single-running-account/fresh-snapshot selection paths.
- `dashboard\start_dashboard.bat` accepts an optional first account argument and opens `http://127.0.0.1:8765/?account=<account>` when supplied.
- Added `dashboard\start_dashboard_kr_mock.bat`.
- Added `dashboard\start_dashboard_us_mock.bat`.
- No `kr_real` or `us_real` launcher was created.
- No worker, account, or trading configuration was touched.
- No files were staged or committed.

Relevant launcher contents:

```bat
:: dashboard\start_dashboard_kr_mock.bat
@echo off
call "%~dp0start_dashboard.bat" kr_mock
```

```bat
:: dashboard\start_dashboard_us_mock.bat
@echo off
call "%~dp0start_dashboard.bat" us_mock
```

### Round 96 — authorized dashboard restart

- Current dashboard PID `8008` was confirmed from `netstat` and stopped with:

```text
Stop-Process -Id 8008
```

- The authorized no-argument launcher was run:

```text
cmd /c dashboard\start_dashboard.bat
```

- The launcher emitted:

```text
ERROR: Input redirection is not supported, exiting the process immediately.
ExitCode=0
```

- Despite that message, a new dashboard listener appeared on port `8765` as PID `10780`, and `/api/status` returned HTTP 200.
- The worker records remained `us_mock` PID `18576`, `kr_mock` PID `9020`, with both real workers stopped.
- The fetched page at `http://127.0.0.1:8765/?account=us_mock` did not contain the new source markers:

```json
{"requestedAccount":false,"requestedAccountIsValid":false}
```

- Therefore the dashboard account-switch verification did not pass. No retry or workaround was performed in Round 96.

## Current code and Git state

The requested dashboard changes are present in the working tree:

- `dashboard\index.html` — modified
- `dashboard\start_dashboard.bat` — modified
- `dashboard\start_dashboard_kr_mock.bat` — untracked new file
- `dashboard\start_dashboard_us_mock.bat` — untracked new file

The worktree also contains many unrelated pre-existing modified and untracked files. Preserve them. Do not use reset, clean, broad staging, or broad commit operations.

The dashboard launcher changes remain uncommitted. Commit or staging requires a separate operator authorization.

## Unresolved issue for the next AI

The source file `dashboard\index.html` contains the Round 95 URL-selection code, but the restarted dashboard response did not contain that code. Determine why the running dashboard serves content that does not match the edited source before making another change. Keep the investigation read-only until a new authorization permits implementation or another restart.

Possible investigation boundaries:

- Confirm the exact static-file path used by `dashboard\dashboard_server.py`.
- Compare the served page bytes with `dashboard\index.html` without changing either.
- Check whether another dashboard checkout/process is serving port `8765`.
- Keep the `Input redirection is not supported` launcher message separate from the static-content mismatch.

Do not restart the dashboard again, touch workers, change account controls, or modify real-account files without a new explicit round authorization.

## Safe next commands

Run from `C:\auto\작업7차\kiwoom-autotrade`:

```powershell
python -m src.worker_supervisor status --account kr_mock --market KR
python -m src.worker_supervisor status --account us_mock --market US
python -m src.worker_supervisor status --account kr_real --market KR
python -m src.worker_supervisor status --account us_real --market US
netstat -ano | Select-String ':8765'
Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8765/api/status'
```

These are read-only checks. Do not run worker `start`, `stop`, or `kill` commands unless explicitly authorized.

## Verification record for this document

- Existing Round 82 operator record was read.
- Current worker statuses were refreshed from the repository root.
- Current dashboard listener and `/api/status` were refreshed read-only.
- Current Git status was inspected without staging or committing.
- This document is newly created and intentionally uncommitted.
- No worker process, account, trading configuration, real-account file, firewall, registry, or network setting was modified while creating this wrap-up.
