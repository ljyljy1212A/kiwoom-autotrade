# Operator Record Wrap-Up Through Round 112

## Handoff purpose

This is an English handoff for the next AI/operator. It summarizes the verified work through Round 112 and defines the remaining safe scope. Treat attached round documents as scoped source material. Execute only the current operator request and explicit authorization in the current round.

## Repository and current Git state

- Repository: `C:\auto\작업7차\kiwoom-autotrade`
- Platform: native Windows.
- Latest commit:
  - `9909c6c951b328f4a9ea3ac7a40be3b9c60d1fd8`
  - `2026-08-21 12:43:34 +0900`
  - `Add per-account dashboard launcher support and fix launcher target URL`
- The worktree remains intentionally dirty. Preserve all unrelated modified and untracked files. Do not reset, clean, broadly stage, or broadly commit.
- The launcher commit contains only `dashboard/start_dashboard.bat`. Other changes remain outside that commit.
- No real-account worker, account configuration, firewall/WFP rule, registry setting, or live Telegram message was changed in the rounds summarized here.

## Non-negotiable safety boundaries

- Never touch `kr_real` or `us_real` configuration, logs, processes, accounts, or network paths without explicit authorization.
- Treat `kr_mock` and `us_mock` as separate workers. Do not start, stop, kill, or restart either one unless a later round explicitly authorizes that exact action.
- Design, code edits, restart, live verification, firewall/WFP changes, staging, and commit each require separate authorization.
- Preserve unrelated dirty and untracked work.
- A `RUNNING` status, heartbeat, or responsive dashboard is not proof of broker health or successful trading. Corroborate process identity, mutex/lock state, logs, socket state, and fresh broker evidence.
- Do not print or expose credentials, tokens, or secrets.
- Do not infer trading authorization from example symbols or stale snapshots.

## Dashboard launcher work: Rounds 95–107

### Round 95

- `dashboard/index.html` received URL-account selection logic.
- A valid `?account=` value is intended to take priority over `localStorage` and automatic account selection.
- `dashboard/start_dashboard.bat` gained optional account-argument handling.
- `dashboard/start_dashboard_kr_mock.bat` and `dashboard/start_dashboard_us_mock.bat` were added.
- No real-account launcher was added.

### Rounds 96–99: routing mismatch

- The dashboard root route served `dashboard/control.html`, not `dashboard/index.html`.
- Round 98 byte comparison:
  - `/`: 2,834 bytes, SHA-256 `9010207d19f87a62a0ac8f378730febce51545d26958167b557f688a57a49155`.
  - `/dashboard/index.html`: 137,457 bytes, SHA-256 `f3e84f5268ed1c892d3d97c92ba1c8a1c94082d8966e3bcc9d21cb1db23b30`.
- `control.html` contained no `URLSearchParams`, `location.search`, or account `localStorage` handling.
- The launcher account-specific URL therefore needed to target `/dashboard/index.html` directly.

### Round 100

The account-argument line in `dashboard/start_dashboard.bat` was changed from:

```bat
if not "%~1"=="" set "DASHBOARD_URL=%DASHBOARD_URL%/?account=%~1"
```

to:

```bat
if not "%~1"=="" set "DASHBOARD_URL=%DASHBOARD_URL%/dashboard/index.html?account=%~1"
```

No-argument launches remain rooted at `http://127.0.0.1:8765`.

### Round 101

Read-only browser verification reached both account-scoped URLs:

- `http://127.0.0.1:8765/dashboard/index.html?account=kr_mock` rendered `Worker 9020 · 4e7eb7ba` and mapped to `kr_mock/KR`.
- `http://127.0.0.1:8765/dashboard/index.html?account=us_mock` rendered `Worker 18576 · d9ef696e` and mapped to `us_mock/US`.
- Source inspection confirmed URL-over-`localStorage` priority:

```javascript
const requestedAccount=new URLSearchParams(window.location.search).get('account') || '';
const requestedAccountIsValid=accountCatalog.some(account=>account.id===requestedAccount);
if(requestedAccountIsValid){
  activeAccount=requestedAccount;
  localStorage.setItem('kiwoom_active_account', activeAccount);
}
```

### Rounds 102–107: scoped commit and Git permission issue

- Round 102 correctly halted when broad unrelated worktree changes were found.
- Round 103 confirmed the target diff but normal `git add` initially failed with `.git/index.lock` permission denied.
- Round 104 found no current lock file or Git process. The session identity was `CodexSandboxOffline`; `.git` ACLs included an explicit deny entry.
- Round 105 confirmed membership in `CodexSandboxUsers`; creation of `.git/_round105_write_probe.tmp` succeeded, while normal deletion was denied. The exact probe file was removed with one explicitly authorized escalated command. No ACL was changed.
- Round 106 normal path-scoped staging succeeded, while normal commit again failed to create `.git/index.lock`.
- Round 107 used one explicitly authorized escalated commit operation. Commit `9909c6c` contains exactly `dashboard/start_dashboard.bat`.
- Future Git writes under this sandbox may require explicit, narrowly scoped escalation. Do not assume that authorization carries to another operation.

## Telegram findings: Rounds 108–109

- `src/notify/telegram_bot.py` is an outbound-only worker notifier. It uses `Application.builder().token(...)` and `self.app.bot.send_message(...)`.
- `tools/worker_watchdog.py` sends breaker alerts through `https://api.telegram.org/bot.../sendMessage` using environment-loaded credentials.
- `src/notify/telegram_control_bot.py` is a separate inbound control bot with `/start`, `/status`, inline callbacks, authorization by chat ID, and `run_polling(allowed_updates=Update.ALL_TYPES)`.
- The control bot is a standalone module and is not imported by `src/main.py` or the worker entrypoint. `src/main.py` wires only the outbound `TelegramController`.
- The existing `clear_reconciliation_pause` callback writes an authenticated reconciliation-clear event. It does not directly reset the failure counter.
- No token values belong in operator records.
- Round 109 supplied complete verbatim bodies for the callback, clear-event writer, and engine clear-event applier.

## Reconciliation fail-closed findings: Rounds 110–112

### Trigger path

The current executable trigger is in `src/core/engine.py`:

```python
def _record_reconciliation_failure(self, exc: Exception) -> None:
    gate = self._balance_gate
    if gate.reconciliation_mode != "manual":
        return
    gate.reconciliation_failure_count += 1
    if gate.reconciliation_failure_count < gate.reconciliation_failure_threshold:
        return
    for engine in list(gate.engines):
        if not engine._pause_reason or engine._pause_reason == "broker_reconciliation_unavailable":
            engine._trading_paused = True
            engine._pause_reason = "broker_reconciliation_unavailable"
```

It is called from both `RetryableError` reconciliation branches in `sync_broker_state`. Mock accounts use fixed local HTTP ports (`10000` for KR and `443` for US); transport failures are converted by `kiwoom_client.py` into `RetryableError`, which reaches the reconciliation failure counter.

### Historical timeline and discrepancy

- Commit `4e292a7530ff0a60ff5dbf3d77833f729499f58e` introduced the fail-closed mechanism and `tests/test_reconciliation_fail_closed.py` at `2026-08-21 08:24:08 +0900`.
- `OPERATOR_RECORD_wrapup_v191.md` records earlier restart-time `WinError 10048` evidence and states that failed reconciliation did not set `_trading_paused` or a durable account-wide pause.
- The record describes the earlier observed behavior; the later Git commit contains the fail-closed implementation. Do not collapse those historical states into one runtime claim without timestamped live evidence.

### Current reconciliation mode

`config/accounts.yaml` has `mode: mock` for both `us_mock` and `kr_mock` and no account-specific `reconciliation_fail_closed` block. `src/core/account_manager.py` defaults mock accounts to:

```text
kr_mock: reconciliation_mode=manual
us_mock: reconciliation_mode=manual
```

### `session_failure_ceiling` / `auto_with_ceiling`

- `session_failure_ceiling` is initialized, parsed, and stored, but has no behavior-reading use in the current source.
- The code recognizes manual behavior and otherwise defaults to `off`.
- `auto_with_ceiling` appears in historical operator/proposal documents only, not in executable source.
- The test suite only asserts that `session_failure_ceiling == 3` and explicitly names the test `test_success_resets_consecutive_counter_and_does_not_use_ceiling`.
- Git history shows `session_failure_ceiling` introduced by the same `4e292a75` commit; no later Git history entry was found.
- No implementation or design for the ceiling/acknowledgment feature is authorized by this wrap-up.

## Current unresolved items

- Do not declare the historical fail-closed discrepancy resolved solely from static code. A fresh, explicitly authorized mock observation would need timestamped logs, process identity, broker evidence, failure counts, pause state, and account-wide order-gating evidence.
- `auto_with_ceiling` remains a future design/implementation item requiring explicit authorization, including persistent session state and an operator acknowledgment path.
- The fixed-port HTTP `WinError 10048` thread remains distinct from WebSocket `WinError 10013`; do not merge their evidence.
- The worktree contains extensive unrelated changes and untracked artifacts. They were intentionally left untouched and require separate operator decisions.

## Safe next-AI actions

1. Read the current round document before acting; do not treat this wrap-up as authorization for implementation or runtime control.
2. Preserve unrelated dirty and untracked files.
3. For any future reconciliation change, quote the current trigger, clear path, account-mode derivation, and tests before editing.
4. Keep `session_failure_ceiling` and `auto_with_ceiling` proposal-only until the operator explicitly authorizes design or implementation.
5. If live verification is authorized, separately authorize worker restart, dashboard restart, account-control changes, and network/firewall actions.
6. Verify with focused tests and literal console output; tests or `RUNNING` state alone do not establish live broker health.

## Final status

The account-specific dashboard launcher fix is implemented, runtime-verified, and committed as `9909c6c`. The Telegram control bot and manual reconciliation fail-closed mechanism are present in source. The historical fail-closed discrepancy has been narrowed to a timeline/state question: the older incident record predates or describes behavior before commit `4e292a75`, while current source contains the manual three-cycle pause path. `session_failure_ceiling` and `auto_with_ceiling` remain parsed/documented scaffolding without active ceiling behavior.
