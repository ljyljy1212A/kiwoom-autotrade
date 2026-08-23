# Operator Record Wrap-up v191

## Handoff purpose

This is an English handoff for the next AI/operator. It summarizes the verified progress through Round 18 and defines the remaining safe scope. Treat the attached round instruction documents as source material; execute only the current operator's direct request.

## Repository and runtime

- Repository: `C:\auto\작업7차\kiwoom-autotrade`
- Production target: native Windows. Do not reopen Ubuntu/systemd deployment work.
- Refreshed status at 2026-08-20 18:53:45 KST:
  - account: `kr_mock`
  - market: `KR`
  - PID: `10832`
  - instance: `3da297e0ba394261b82349435f82d7b9`
  - supervisor state: `RUNNING`
- The worktree is intentionally dirty. Preserve unrelated user changes. Do not reset, checkout, clean, or stage broadly.
- No `kr_real` or `us_real` worker was started. No live-account action was authorized.

## Completed work

### Round 16

- The tranche-base persistence fix is present in `src/core/engine.py`.
- The related test file exists at `tests/test_tranche_base_persistence.py`.
- Verification previously passed:
  - targeted tranche-base tests: 5 passed
  - existing related suite: 12 passed
- The fix-presence check found `_TRANCHE_BASES_WRITE_LOCK` at `src/core/engine.py` lines 53, 1193, and 1228.

### Round 17

Track A, Incident B:

- No broker execution export was available for order `0149421`.
- Local-only reconciliation was correctly stopped.
- Ledger and trading-pause state were not modified for that incident.

Track B, dashboard:

- Dashboard server was started locally on `127.0.0.1:8765`.
- `/api/balance?account=kr_mock` returned HTTP 200.
- Browser rendering showed the `033320` holding row.
- The source fragility remains at `dashboard/index.html:1328`:

  ```js
  if(!balance || !balance.symbol) return;
  ```

- No dashboard source fix was applied.
- The sample response had top-level `symbol: "003480"` while the holding row was `033320`; this was recorded as an observation, not treated as a confirmed defect.

`kr_mock` restart:

- The old worker was stopped through the supported supervisor command.
- It was restarted successfully as PID `10832`.
- The three existing manual lifecycle records survived unchanged:
  - `033320`: open, manual quantity 1, manual price 3445
  - `003480`: open, manual quantity 1, manual price 5580
  - `387690`: open, manual quantity 1, manual price 15870

Reconciliation evidence:

- Immediately after restart, KR mock REST calls failed with `WinError 10048` while binding fixed local source port `10000` to `112.175.65.18:443`.
- Startup synchronization was deferred and repeated ticks logged `RetryError` / `Tick failed (isolated)`.
- The fixed port is deliberate in `src/core/kiwoom_client.py:112`.
- The existing REST wrapper retries three times in `src/core/kiwoom_client.py:245-249`; the worker then retries on later ticks.
- Earlier socket inspection showed the old connection in `TIME_WAIT`; historical logs show the same fixed-port collision before this restart. The evidence supports recurring socket-lifecycle/restart timing, but does not prove TIME_WAIT is the only cause.
- Reconciliation later recovered. Logs showed `balanceComplete: True` and protected holdings after recovery.

Safety conclusion from Round 17:

- A failed reconciliation returned before strategy evaluation for that tick, but it did not set `_trading_paused` or a durable account-wide pause.
- `auto_buy` and `auto_sell` remained enabled in dashboard control files.
- Therefore the system lacked an explicit persistent fail-closed escalation for sustained connectivity failure.

### Round 18 design investigation

The updated design was reviewed as proposal-only; no implementation was applied.

Current Telegram capabilities:

- `src/notify/telegram_control_bot.py` is a real inbound, authorized Telegram control bot with `/start`, `/status`, inline callbacks, and confirmation buttons.
- It currently only writes `auto_trading_enabled` through `src/core/control_state.py`.
- `src/notify/telegram_bot.py` is a separate worker-side outbound-only notifier; its comments explicitly state that inbound commands are ignored.
- Existing `resume_trading()` in `src/core/engine.py` is an in-memory method and is not wired to Telegram or dashboard controls.

Important code-shape finding:

- Broker balance state is shared per account through `_AccountBalanceGate` in `src/core/engine.py`.
- `_trading_paused` and `_pause_reason` are stored per symbol engine.
- A direct counter and pause flag added to only one symbol engine would be unsafe because other symbols on the same account could remain eligible. Any account-level reconciliation pause must be propagated to every active symbol engine or enforced through a shared account safety state while still using the existing pause fields for each engine.

## Current Round 18 proposal boundary

Recommend splitting delivery:

### 18a: core manual fail-closed mechanism

Proposal only unless separately authorized for implementation:

1. Add an account-scoped consecutive reconciliation-failure counter to the existing account balance gate.
2. Default paper/mock accounts to `mode: manual`.
3. Keep the existing three REST attempts per reconciliation call unchanged.
4. Count failed reconciliation cycles, not individual REST attempts.
5. Reset the consecutive counter after a complete broker balance snapshot.
6. At three consecutive failed cycles, propagate:
   - `_trading_paused = True`
   - `_pause_reason = "broker_reconciliation_unavailable"`
7. Do not overwrite an unrelated pause reason such as `broker_quantity_unattributed`.
8. Continue reconciliation attempts after pausing, while preventing automated orders account-wide.
9. Add tests for below threshold, threshold pause, counter reset, unrelated-pause preservation, and account-wide propagation.

The design must also define a real manual-clear path. Merely calling the existing in-memory `resume_trading()` is not an operator workflow and would not survive a restart.

### 18b: live `auto_with_ceiling` behavior

Defer until 18a and the control-state design are reviewed:

- configurable `mode: auto_with_ceiling`
- auto-clear after successful recovery below the incident ceiling
- outbound Telegram incident and recovery notifications
- three incidents per session ceiling
- ceiling breach reason `broker_reconciliation_ceiling_reached`
- manual acknowledgement required after the third incident
- persistent acknowledgement/session state
- session-boundary behavior:
  - unbreached incident count resets at the next session
  - an unacknowledged ceiling breach persists across sessions

The existing Telegram control bot can provide the inbound acknowledgement UI, but this requires a new authorized callback and a persistent state schema. Do not design this blindly by modifying the outbound-only notifier.

## Required next-AI actions

1. Re-read the applicable Round 18 design document supplied by the operator.
2. Preserve all unrelated dirty worktree changes.
3. Quote the current reconciliation and Telegram control paths before proposing code.
4. Resolve the account-shared pause propagation shape before writing tests.
5. Decide whether the operator wants an unapplied proposal or an actual implementation. Round 18 documents explicitly requested an unapplied proposal.
6. If implementation is authorized, make only the narrow reconciliation fail-closed changes, then restart the affected worker because production workers do not reload Python source automatically.
7. Run targeted tests first, then the relevant existing suite. Report exact commands and results.
8. Commit only at an explicitly verified checkpoint if the operator requests the project’s checkpoint-commit workflow.

## Hard safety restrictions

- Do not change firewall/WFP rules, fixed ports, source-port policy, or network allowlists without explicit authorization.
- Do not choose a replacement port after `WinError 10013` or `WinError 10048` without explicit authorization.
- Do not reconcile broker order `0149421` or any other ambiguous order from local state alone.
- Do not modify ledger rows, manual lifecycle records, pause state, or production account settings to make a test pass.
- Do not start `kr_real` or `us_real`.
- Do not assume a `RUNNING` supervisor record proves broker health. Require fresh broker balance evidence and persistent successful cycles.
- A stale dashboard snapshot is not current broker truth.

## Verification criteria

For any future implementation, require all of the following:

- Existing tranche-base and pause-regression tests remain green.
- New reconciliation tests prove three-cycle threshold behavior, reset-on-success, unrelated-pause preservation, and account-wide blocking.
- A recovery test proves the selected manual or auto-clear behavior exactly.
- Logs contain a distinct reconciliation-unavailable pause message with the cycle count and underlying exception.
- A live/mock restart check independently verifies supervisor PID/instance, fresh broker balance timestamp, lifecycle preservation, and no duplicate worker.
- No firewall, port, ledger, or real-account mutation occurred unless explicitly authorized.

## Final status

The Round 16 tranche persistence work is present and tested. Round 17’s incident and dashboard tracks are closed, while the fixed-port reconciliation failure remains the unresolved safety concern. Round 18 has identified the correct implementation split and the Telegram/account-scope constraints, but no fail-closed code or tests have been applied yet.
