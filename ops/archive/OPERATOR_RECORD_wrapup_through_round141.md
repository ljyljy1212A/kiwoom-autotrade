# Operator Record Wrap-up Through Round 141

## Handoff purpose

This is an English handoff for the next AI/operator. Treat attached round
instruction documents as scoped source material. Execute only the current
operator's direct request and the active round's explicit authorization.

This record summarizes the verified progress through Round 141 and separates
confirmed evidence from unresolved runtime questions.

## Repository and safety boundary

- Repository: `C:\auto\작업7차\kiwoom-autotrade`
- Platform: native Windows.
- Worktree: intentionally dirty. Preserve all unrelated changes. Do not use
  `git reset`, `git checkout`, `git clean`, broad staging, or broad formatting.
- Real accounts: `kr_real` and `us_real` are out of scope and must not be
  started, queried, or changed.
- Mock workers during Rounds 137–141:
  - `kr_mock`: PID `9980`, instance `6d6d4776d7b34514932745516c2a5cba`
  - `us_mock`: PID `5372`
- Rounds 138–141 were observation/source-query only after the Round 137
  `kr_mock` restart. No orders, pause clears, edits, commits, or further
  restarts were authorized in those rounds.

## Executive summary

Two investigation tracks progressed materially:

1. The fixed-port `WinError 10048` recovery sequence is confirmed. After the
   Round 137 `kr_mock` restart, the first failure was logged at `16:31:08`,
   `033320` reached 14 consecutive reconciliation failures by `16:32:51`,
   successful token/REST I/O appeared at `16:32:57`, and the measured recovery
   interval was approximately 109 seconds. This is consistent with the prior
   approximately 106-second baseline.
2. The two old unresolved SELL rows were safely retired locally after the
   recovery path ran. The orders were not confirmed filled; both ended with
   `filled_qty=0.0` and `status='cancelled'`.

The `003480` pause question is narrowed but not directly observable from the
current status interface. The zero-balance branch explicitly sets
`_trading_paused=True` and `_pause_reason='broker_quantity_unattributed'` at
the first zero snapshot. The subsequent orphan-cleanup path archives the
symbol state but does not reference either pause field. There is no live
`resume_trading` call site and no post-resolution automated-order attempt from
which to observe the pause gate directly.

Round 141 also identified a distinct logging issue: orphan-cleanup audit JSON
payloads for `003480` were emitted under other task tags (`387690` and
`033320`). Do not treat the tag column as the payload symbol without checking
both fields.

## Fixed-port reconciliation investigation

### Current evidence

The first observed failure after PID `9980` started:

```text
2026-08-21 16:31:08 ... [WinError 10048] ...
```

The retry sequence kept the token diagnostic in `fetching-new-token` and
logged repeated broker reconciliation failures. At `16:32:57`, the log
contained successful token and REST I/O diagnostics, including:

```text
HTTP I/O diagnostic: client=token operation=write state=after ... outcome=success
HTTP I/O diagnostic: client=token operation=read state=after ... outcome=success
Token diagnostic: state=cached-token-valid
```

The source log is:
`C:\auto\작업7차\kiwoom-autotrade\logs\kr_mock.log`.

### Mechanism status from earlier rounds

The current best-supported mechanism remains a fixed local REST source-port
collision during asynchronous socket cleanup:

- The project deliberately uses a fixed local REST source port.
- Earlier live observations showed HTTP `CLOSE_WAIT` while the WebSocket side
  remained established, followed by `WinError 10048` and later recovery.
- Historical `TIME_WAIT`/cadence evidence explains the earlier intermittent
  pattern but should not be merged with the later `CLOSE_WAIT` evidence as one
  confirmed cause.
- The deferred close race remains the best-supported remediation candidate,
  but it has not been implemented or authorized in this record.

No firewall, WFP, source-port, or network-allowlist change is authorized.

## Order-resolution path

The actual code path is `AccountEngine._cancel_stale_orders()` in:

`C:\auto\작업7차\kiwoom-autotrade\src\core\engine.py`

Relevant source lines:

```text
948:    async def _cancel_stale_orders(self) -> None:
968:            if (now - created_at).total_seconds() < self.pending_order_cancel_after_sec:
978:            if order.status == "awaiting_execution_history" and age >= self.pending_order_cancel_after_sec:
979:                self.ledger.mark_cancelled(order.ord_no)
980:                self.ctx.logger.warning(
981:                    f"Force-closed unresolved {order.side} after {age:g}s without confirmed fill: {order.ord_no}"
982:                )
```

The default threshold is configured at `engine.py:214–217`:

```text
214:        self.pending_order_cancel_after_sec = float(
215:            os.environ.get("PENDING_ORDER_CANCEL_AFTER_SEC",
216:                           os.environ.get("PENDING_BUY_CANCEL_AFTER_SEC", "180"))
217:        )
```

The observed ages of approximately 91,896.6 seconds and 91,534.7 seconds
are the actual order ages when the bounded cleanup check eventually ran. The
configured eligibility threshold is 180 seconds by default.

The runtime lines were:

```text
2026-08-21 16:32:57 | WARNING | kr_mock | pid=9980 ... | 003480 | Force-closed unresolved SELL after 91896.6s without confirmed fill: 0146759
2026-08-21 16:33:01 | WARNING | kr_mock | pid=9980 ... | 387690 | Force-closed unresolved SELL after 91534.7s without confirmed fill: 0148351
```

`get_executed_orders`, `ka10076`, `_cancel_stale_orders`, and
`mark_cancelled` all exist in the current source. The pending-order branch
calls `get_executed_orders()` before `_cancel_stale_orders()`. In this case the
rows were already `awaiting_execution_history`, so the local force-close
branch retired them without a broker cancellation request.

Fresh ledger rows after recovery:

```text
('kr_mock', '0146759', '003480', 'SELL', 20.0, 4960.0, 'SELL', 24,
 '{"step": 24, "sell_only_step": true}', 0.0, 'cancelled',
 '2026-08-20T06:01:21.215143+00:00',
 '2026-08-21T07:32:57.790192+00:00')

('kr_mock', '0148351', '387690', 'SELL', 6.0, 15600.0, 'SELL', 8,
 '{"step": 8, "sell_only_step": true}', 0.0, 'cancelled',
 '2026-08-20T06:07:26.599625+00:00',
 '2026-08-21T07:33:01.283638+00:00')
```

These rows show no confirmed fills. Do not infer broker fills from the local
`cancelled` status alone.

## `033320` result

The `033320` reconciliation sequence logged 14 consecutive
`WinError 10048` failures from `16:31:11` through `16:32:51`.

After recovery, its audit state was:

```text
2026-08-21 16:32:57 ... | 033320 | Orphan cleanup audit: {... 'symbol': '033320', 'brokerQty': 1.0, 'balanceComplete': True, 'baseEntry': True, 'openLifecycle': True, 'unresolvedOrders': False, 'classification': 'protected_nonzero_holding', 'zeroConfirmations': 0}
```

This is different from the prior zero-balance cases. The broker quantity was
nonzero (`1.0`), so the zero-balance attribution branch did not apply. No
`external_broker_balance_change` line was observed for this restart.

Do not generalize this result to the prior zero-balance pattern; the scenario
was materially different.

## `003480` orphan-cleanup timeline

The accurate payload-symbol sequence was:

```text
16:32:57 — classification: 'manual_review_required', zeroConfirmations: 0, unresolvedOrders: True
16:32:57 — classification: 'orphan_candidate', zeroConfirmations: 1, unresolvedOrders: False
16:33:01 — classification: 'cleaned', zeroConfirmations: 2
16:33:11 — classification: 'clean', zeroConfirmations: 0
```

The cleanup line was emitted under a `387690` task tag:

```text
2026-08-21 16:33:01 ... | 387690 | Orphan cleanup audit: {... 'symbol': '003480', ... 'classification': 'cleaned', 'zeroConfirmations': 2, 'removed': ['tranche_base', 'lifecycle_closed', 'control_archived:dashboard_control_kr_mock_003480.json', 'dashboard_profile']}
```

The later clean audit was emitted under a `033320` task tag:

```text
2026-08-21 16:33:11 ... | 033320 | Orphan cleanup audit: {... 'symbol': '003480', ... 'classification': 'clean', 'zeroConfirmations': 0}
```

This is a real tag/payload mismatch finding. Future queries must inspect both
the task tag and the JSON `symbol` field.

The transition is implemented by `OrphanStateCleaner.sweep()` in:

`C:\auto\작업7차\kiwoom-autotrade\src\core\orphan_cleanup.py:102–129`

```text
112:            if result["classification"] == "orphan_candidate":
113:                count = int(previous.get(symbol, 0)) + 1
114:                confirmations[symbol] = count
115:                result["zeroConfirmations"] = count
116:                if count >= 2 and apply:
117:                    result["classification"] = "cleaned"
118:                    result["removed"] = self._apply(symbol)
```

`OrphanStateCleaner.sweep()` and `_apply()` do not read, write, or reference
`_trading_paused` or `_pause_reason`. `_apply()` archives/removes recreatable
symbol state and closes the lifecycle; it does not clear the trading pause.

## Pause-state status

The zero-balance branch in `engine.py:1553–1557` explicitly does this:

```text
1553:            self._trading_paused = True
1554:            self._pause_reason = "broker_quantity_unattributed"
1555:            self.ctx.logger.info(
1556:                f"Complete zero balance observed for {symbol_key}; orphan cleanup confirmation "
1557:                f"{current_orphan.get('zeroConfirmations', 0)}/2"
```

The cleanup-completion path returns before that branch on the second zero
snapshot and does not clear either field.

Fresh repo-wide search for `resume_trading` found only the method definition
in live Python source:

```text
C:\auto\작업7차\kiwoom-autotrade\src\core\engine.py:1977:    def resume_trading(self):
```

Other matches were documentation or an unapplied proposal diff. There are no
live call sites, callback registrations, or dispatch-table references.

The dashboard backend exposes `/api/control`, but it only writes dashboard
auto-buy/auto-sell configuration. It has no resume, unpause, force-resume, or
pause-clear endpoint. The separate `reconciliation_clear_event` path clears
only `broker_reconciliation_unavailable`; it does not clear
`broker_quantity_unattributed`.

The current status file/API does not expose private per-symbol
`_trading_paused` and `_pause_reason` values. No post-resolution
`Auto condition suppressed by trading pause` line or other `003480`
automated-order attempt was observed. That absence is inconclusive because no
qualifying automated intent was logged.

Therefore, preserve the following distinction in future records:

- Confirmed: the zero-balance branch set `003480`'s pause fields at the first
  zero snapshot.
- Confirmed: orphan cleanup changed the durable symbol state to `cleaned`,
  then `clean`.
- Confirmed: orphan cleanup does not clear the pause fields.
- Not directly observed: the current in-memory values of `_trading_paused` and
  `_pause_reason` after cleanup.
- Not authorized: triggering an automated order merely to probe the pause.

## Worktree and implementation status

No implementation was authorized or applied in Rounds 138–141. The broader
checkout remains dirty with unrelated existing modifications and untracked
records/proposals. Do not stage or commit this wrap-up automatically.

No tests were run in the observation-only rounds. Runtime evidence came from
the worker log, ledger query, source inspection, and status files.

## Required instructions for the next AI

1. Read this record and the active attached instruction document before doing
   anything.
2. Preserve the dirty worktree. Resolve the Git root dynamically and inspect
   `git status --short` before any authorized edit.
3. Keep `kr_real` and `us_real` completely out of scope.
4. Do not restart `kr_mock` or `us_mock` unless a future round explicitly
   authorizes the exact worker and restart procedure.
5. Do not clear `_trading_paused`, `_pause_reason`, or any reconciliation
   control state unless explicitly authorized.
6. Do not place, cancel, modify, or infer broker orders from local ledger rows.
7. Treat `003480` audit lines by JSON payload symbol and task tag separately;
   the log stream has demonstrated tag/payload disagreement.
8. If implementing a fix, make the smallest authorized change, add or update
   targeted tests first, run the exact verification commands, and report raw
   output. Do not modify the fixed-port or firewall behavior without explicit
   authorization.
9. A running PID or supervisor `RUNNING` state does not establish broker
   health. Require fresh broker evidence and corroborate process identity,
   instance ID, logs, and relevant socket state.
10. If the goal is to resolve the remaining pause question, first obtain a
    direct, non-mutating source of the live engine fields. Do not use an
    automated order as a probe.

## Verification criteria for future authorized work

- State the behavior-affecting assumptions before implementation.
- Confirm exact target files and call paths before editing.
- Keep the diff surgical and limited to the request.
- Run targeted tests with `PYTHONPATH='.'` from the repository root when
  applicable.
- Report actual test/console output, not only a command name.
- Verify no real-account action, firewall change, broad staging, or unrelated
  worktree mutation occurred.
