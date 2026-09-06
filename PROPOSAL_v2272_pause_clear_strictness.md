# Proposal v2272: Pause-Clear Predicate Strictness

Date: 2026-09-05

Status: Design proposal only. No implementation or test change is authorized
by this document.

## 1. Problem statement

Round 2271 confirmed that the fixed-port recovery path is already freshness-
gated and is excluded from this proposal. The following three paths can clear
`_trading_paused` without the same complete fresh-reconciliation predicate.

### `_refresh_dashboard_controls` — `src/core/engine.py:1041`, clear at line 1140

Literal current logic:

```python
1126:         if (symbol != self._symbol_key(self.ctx.strategy.symbol)
1127:                 or fingerprint != self._dashboard_config_fingerprint
1128:                 or (not lifecycle_is_open and not lifecycle_is_pending)):
1129:             try:
1130:                 strategy = InfiniteGridStrategy(config)
1131:             except (KeyError, TypeError, ValueError) as exc:
1132:                 self.ctx.logger.warning(f"Ignoring invalid dashboard strategy configuration: {exc}")
1133:                 self._dashboard_auto_buy = self._dashboard_auto_sell = False
1134:                 return
1135:             self.ctx.strategy = strategy
1136:             self.ctx.position = PositionState(symbol=strategy.symbol)
1137:             # Dashboard profile activation is an explicit operator action.
1138:             # Clear a stale account-level pause left by an earlier broker
1139:             # mismatch; current balance and tranche safety gates still apply.
1140:             self._trading_paused = False
```

The shown gate checks profile/strategy/lifecycle transition and successful
strategy construction. It does not itself require a fresh broker balance or a
fresh reconciliation result.

### `_apply_reconciliation_clear_event` — `src/core/engine.py:1593`, clear at line 1605

Literal current logic:

```python
1593:     def _apply_reconciliation_clear_event(self) -> None:
1594:         event_id, reason = self._pause_clear_event()
1595:         if not event_id or not reason or event_id == self._balance_gate.pause_clear_event_id:
1596:             return
1597:         self._balance_gate.pause_clear_event_id = event_id
1598:         if reason == FIXED_PORT_DEGRADED_PAUSE_REASON:
1599:             return
1600:         matched = False
1601:         for engine in list(self._balance_gate.engines):
1602:             if engine._pause_reason != reason:
1603:                 continue
1604:             matched = True
1605:             engine._trading_paused = False
1606:             if reason in {"broker_quantity_unattributed", "tranche_rebuild_ambiguous"}:
1607:                 engine._tranche_sell_paused = False
1608:             engine._pause_reason = ""
```

The literal predicate is event ID present, reason present, event ID not
already consumed, and exact pause-reason equality. No fresh reconciliation,
staleness bound, or broker response is checked here.

### `resume_trading` — `src/core/engine.py:2688`, clear at line 2689

Literal current logic:

```python
2688:     def resume_trading(self):
2689:         self._trading_paused = False
2690:         self.ctx.logger.info(f"Resuming trading for {self.ctx.strategy.symbol}; clearing pause reason: {self._pause_reason}")
2691:         self._pause_reason = ""
```

This clear is unconditional when the method is called.

## 2. Reusable predicate assessment

The existing helper is asynchronous and performs a broker request:

```python
798:     async def _build_reconciliation_clearance_snapshot(
799:         self, symbol: str, *, max_balance_age_sec: float,
800:     ) -> ReconciliationClearanceSnapshot:
801:         raw_balance = await self.ctx.client.get_balance()
```

The fixed-port service uses it with a one-second freshness bound and then
evaluates the result:

```python
311:                     snapshot = await engine._build_reconciliation_clearance_snapshot(
312:                         symbol, max_balance_age_sec=1.0,
313:                     )
314:                     result = evaluate_reconciliation_clearance(snapshot)
```

### `_apply_reconciliation_clear_event`

- Async/sync status: synchronous (`def`, not `async def`).
- Lock status: called from `sync_broker_state()` while `self._sync_lock` is
  already held:

  ```python
  1351:     async def sync_broker_state(self, force_balance: bool = False) -> bool:
  1352:         """Apply cumulative REST fills as idempotent deltas, then reconcile balance."""
  1353:         async with _diagnostic_lock(self._sync_lock, "AccountEngine._sync_lock", self.ctx.logger):
  1354:             self._apply_reconciliation_clear_event()
  ```

- Context available: the method has `self.ctx.account_id`, the account's
  balance gate, and the engines set; each matching engine has its strategy
  symbol and broker client.
- Reuse feasibility: not as a direct call. The helper requires `await`, so
  this site would require making the method asynchronous and changing its
  caller. A synchronous wrapper around the coroutine would be unsafe inside
  the already-running event loop. The proposed async call would also hold
  `_sync_lock` while performing fresh broker requests unless the call is
  deliberately moved outside that lock.
- Reentrant-lock risk: the exact Round 2261 failure was lock reacquisition by
  a caller inside a held lock. The snapshot helper itself does not acquire
  `self._sync_lock`, but a future implementation must not call a clearance
  service or helper that reacquires the same lock while this method remains
  inside the lock.

### `_refresh_dashboard_controls`

- Async/sync status: synchronous (`def`).
- Lock status: called under `self._sync_lock` during startup and every tick:

  ```python
  948:     async def _tick(self):
  951:         self._refresh_runtime_control()
  952:         async with _diagnostic_lock(self._sync_lock, "AccountEngine._sync_lock", self.ctx.logger):
  953:             self._refresh_dashboard_controls()
  ```

- Context available: one engine's account ID, broker client, current strategy
  symbol, lifecycle state, ledger, and strategy tranche state.
- Reuse feasibility: not directly. Making the dashboard refresh async would
  require changing both startup and tick call chains. A synchronous wrapper
  would be unsafe in the running event loop. A fresh broker request while the
  sync lock is held would also make dashboard-control refresh hold that lock
  across network I/O.
- Ordering risk: startup calls dashboard refresh before the first startup
  broker synchronization, so a fresh check here would change startup ordering
  or require deferring the clear until after reconciliation.

### `resume_trading`

- Async/sync status: synchronous (`def`).
- Lock status: the method itself has no lock acquisition and no shown caller
  contract guaranteeing that `self._sync_lock` is held.
- Context available: the current engine's account ID, strategy symbol, broker
  client, ledger, and in-memory reconciliation state.
- Reuse feasibility: a full fresh check would require an async API or an
  asynchronous caller. A synchronous wrapper is not safe when invoked from
  the event loop. A local staleness check would not establish broker-side
  safety.
- Operator-control risk: this method is named and shaped as an explicit
  operator resume action. Applying an automatic broker predicate could block
  or delay a deliberate manual intervention. Whether it should remain an
  unconditional manual override is therefore an operator decision, not an
  assumption for implementation.

## 3. Per-site design options

### `_apply_reconciliation_clear_event`

The minimal direction is to make the clear operation asynchronous at the
point where it is called, or to separate event consumption from a later async
clear phase. The later clear phase would evaluate a fresh snapshot for each
matching engine before changing `_trading_paused` or `_pause_reason`.

A full fresh check is technically feasible only with an async call-chain
change. The implementation must avoid holding `_sync_lock` across a network
round trip where possible and must not reacquire any already-held lock. A
staleness-only check would be lighter but would not prove the current broker
quantity and tranche state are safe.

The existing event ID must not be consumed before the fresh checks succeed,
otherwise a failed check could permanently consume the operator's clear event.
That sequencing point is an implementation concern for a later authorized
round, not a change made here.

### `_refresh_dashboard_controls`

The minimal direction is to stop treating profile activation alone as
sufficient proof for clearing an account-level pause. A later implementation
could defer clearing until the normal reconciliation pass has produced a
fresh safe result, or introduce an explicitly asynchronous activation path.

A full fresh check is not convenient at this synchronous, lock-held site. A
staleness-only check would not establish that the current broker quantity is
consistent with the tranche ledger. Tightening this path can delay a newly
enabled profile, so the design must preserve the existing explicit operator
activation semantics while preventing stale state from being treated as a
complete reconciliation.

### `resume_trading`

The minimal direction is intentionally unresolved. Two legitimate designs
remain:

1. Keep it as an unconditional manual override and require the caller/operator
   to perform reconciliation separately.
2. Convert it to an asynchronous, fresh-reconciliation-gated operation, which
   changes its operator-facing behavior and may delay a deliberate resume.

The first option preserves direct operator control; the second provides a
stronger automatic safety predicate. No choice is made in this proposal.

## 4. Scope boundary

This round changes only this proposal document:

```text
PROPOSAL_v2272_pause_clear_strictness.md
```

A future implementation, if authorized, would be expected to touch
`src/core/engine.py` and the narrowly relevant existing tests. The exact test
file set should be selected only after the operator resolves the open design
questions below.

The fixed-port path is explicitly excluded because
`clear_current_active_profile()` already requires a fresh one-second-bounded
reconciliation snapshot for every active symbol. No `kr_real` or `us_real`
code path is touched or included. No configuration, dashboard real-account
launcher, runtime process, Scheduler, staging, commit, or push action is in
scope.

## 5. Six-step gate plan

Any implementation must use the separately authorized six-step gate used by
the BrokerHTTPGate arc:

1. Draft — produce and verify a single-pass implementation diff.
2. Dry-run — apply temporarily, perform syntax/import checks, and restore the
   exact pre-round working-tree state.
3. Apply — apply the authorized implementation and leave it in the working
   tree.
4. Test — run the narrowly relevant tests, then the authorized regression
   suite, with literal results.
5. Stage — explicitly stage only the authorized implementation/test files and
   verify the staged diff.
6. Commit — create the local commit only after the prior evidence is accepted.

Push remains a separate operator-direct action and is not part of this gate.

## 6. Open questions for operator/reviewer decision

1. Should `resume_trading()` remain intentionally unconditional as a manual
   override, with reconciliation handled separately, or should it become an
   asynchronous fresh-reconciliation-gated operation?
2. For `_apply_reconciliation_clear_event`, should the event be retained until
   fresh checks pass, and should the clear be moved outside the held
   `_sync_lock` before any network request?
3. For `_refresh_dashboard_controls`, should profile activation defer pause
   clearing until the next successful reconciliation, or should it use a
   separate asynchronous activation check?
4. Is a full fresh broker reconciliation required for the two automated/event
   paths, or is a defined local staleness bound acceptable as an interim
   predicate?

No fix is proposed or authorized until these questions are resolved.
