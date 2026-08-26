# Round 570 — Phase 4 dispatch-blocking proposal, per-symbol clearance revision

## Status and scope

This is a proposal only. It adds no source behavior, tests, configuration,
worker action, Git action, or account action. Any implementation requires a
separate source-change authorization.

This revision preserves Round 567 and adds per-symbol clearance tracking to
avoid clearing an account-wide fixed-port marker from only one symbol's
reconciliation result.

The proposal applies only to an account that has entered the existing
fixed-port degraded state. In the current design that is `us_mock`; it does
not authorize a `kr_real` or `us_real` path, and it does not make `kr_mock`
subject to a US fixed-port collision policy.

## Existing seam

The only current strategy order-dispatch call site is
`AccountEngine._execute_order()` in `src/core/engine.py`. It calls
`self.ctx.client.place_order(...)` after dashboard-profile, US-paper opt-in,
duplicate-order, and risk gates. `KiwoomClient.place_order()` then records an
attempt immediately before `_post_once()` transmits the order.

The implemented Phase 3 interface is a pure evaluator:

```text
evaluate_reconciliation_clearance(snapshot) -> ReconciliationClearanceResult
```

`ReconciliationClearanceResult.cleared` is the explicit outcome, and
`failures` identifies every unsatisfied condition. The condition-5 helper
`with_unattributed_collision_order_ids(snapshot, data_dir=DATA_DIR)` fills the
snapshot from the account-scoped durable attempt store. No current production
snapshot-construction call site exists.

## Per-symbol clearance state

In a separately authorized implementation, extend the account-scoped degraded
episode state with:

```text
active_symbols: frozenset[str]
cleared_symbols: frozenset[str]
```

Normalize symbols to the same canonical form used by the worker registry.
`active_symbols` is the snapshot, at degraded entry or reset, of symbols that
have an open, non-`balance_only` `AccountEngine` strategy context for the
account and can reach `_execute_order()`. It excludes merely configured or
historical holdings without a running strategy engine. It is not limited to a
symbol currently holding a position.

`cleared_symbols` begins empty for every degraded episode. Add a symbol only
after that symbol's own fresh Phase 3 snapshot passes all five conditions. The
account-level degraded marker may be lifted only when `active_symbols` is
non-empty and is a subset of `cleared_symbols`. An empty active set never
automatically clears a degraded marker.

While a marker is active, reset the episode's symbol tracking if the worker
registry adds or removes an eligible strategy engine, or if an account's
dashboard/profile configuration change makes a symbol newly order-capable or
not order-capable. Reset means replace `active_symbols` with the current set
and set `cleared_symbols` to empty; retain the degraded marker, entry time,
and collision history. This conservative reset ensures a new or changed symbol
does not inherit clearance earned under a different active-symbol set.

## Proposed insertion and flow

Add one account-scoped asynchronous dispatch-clearance service in a separately
authorized implementation. Insert its call in `_execute_order()` immediately
after the existing US-paper opt-in gate and before
`self.ctx.client.place_order(...)`.

1. If the account is not in `FixedPortDegradedState`, return without changing
   the existing order path.
2. If it is degraded, refresh the active-symbol set as described above. Build a
   fresh, non-cached `ust21070` reconciliation snapshot for the intended
   symbol; add durable condition-5 IDs through
   `with_unattributed_collision_order_ids()`; then call
   `evaluate_reconciliation_clearance()`.
3. Run this symbol-specific predicate on every dispatch attempt while the
   account remains degraded, including for a symbol already present in
   `cleared_symbols`. A later broker change, unresolved order, or new
   unattributed attempt can make a previously clear symbol unsafe again. A
   successful current evaluation adds the symbol to `cleared_symbols`; a failed
   evaluation removes it from that set if present.
4. If the intended symbol's current result is not cleared, retain degraded
   state and raise a new, typed `OrderDispatchBlockedError` carrying the
   account, symbol, and clearance failures. Do not call `place_order()`, create
   an attempt record, queue the intent, or replay a prior request.
5. If the intended symbol's current result is cleared but the active-symbol
   set is incomplete, retain the account marker but permit this checked symbol's
   current order to continue through the existing `place_order()` call and its
   pre-dispatch attempt record. Other symbols remain subject to their own
   per-dispatch checks.
6. If the current success completes the active-symbol set, atomically clear the
   account's degraded marker, record recovery with the set snapshot and
   predicate results, and continue through the existing `place_order()` call.

The service must use an account-scoped asynchronous lock for the active/cleared
set, predicate evaluation state transition, and marker clear. It must not hold
that lock across unrelated strategy work. `_execute_order()` remains the
primary seam because it has the strategy intent, account context, existing
user-visible failure handling, and all current automatic order paths pass
through it. A later hardening increment may also make direct
`KiwoomClient.place_order()` callers provide an explicit clearance token while
degraded, but that is not required for the first wiring increment and should
not be silently combined with it.

## Rejection semantics

Recommend outright rejection through `OrderDispatchBlockedError`.

- Rejection preserves the current tick model: no unseen work survives into a
  later tick, and the normal strategy/risk gates run again if the account
  clears.
- Queueing is rejected because it can submit a stale strategy decision after
  price, position, controls, or broker state has changed.
- Retrying or replaying is rejected because an earlier collision-period order
  can be ambiguous; manual attestation and the five-part predicate are the
  recovery route.

The existing `_execute_order()` error path should log and notify the typed
block distinctly from an ordinary `RetryableError`. This is an implementation
detail for the separately authorized change, not a request to alter current
error handling now.

## Scope and failure mode

The fixed-port degraded marker and its clearance service are per-account. The
predicate is per intended symbol because normalized holding and reconciliation
conditions are symbol-specific; condition 5 remains account-scoped. The
active/cleared symbol sets prevent one symbol's successful predicate from
authorizing a different symbol.

If active-symbol discovery, snapshot construction, durable-attempt lookup, or
predicate evaluation raises unexpectedly, fail closed: retain degraded state
and raise `OrderDispatchBlockedError` with an internal-check failure reason. A
failure to prove reconciliation is safe must not permit a mock order to bypass
the degraded-state control.

## Test plan for the wiring increment

The implementation test set should cover:

1. A non-degraded `us_mock` order still reaches `KiwoomClient.place_order()`.
2. A degraded account with every active symbol cleared calls `place_order()`
   once and atomically clears its marker after the last active symbol passes.
3. With multiple active symbols, a cleared symbol's current passing dispatch is
   allowed while an uncleared symbol is blocked; the account marker remains
   active until the uncleared symbol passes.
4. Each individual condition-1 through condition-5 failure blocks before
   `place_order()` and does not create a new attempt record.
5. An unattested durable collision attempt specifically produces a condition-5
   block; an authenticated attestation alone remains insufficient until the
   full predicate clears.
6. A previously cleared symbol is re-evaluated on a later degraded dispatch;
   a new failure removes it from `cleared_symbols` and blocks that order.
7. Adding/removing an eligible engine or changing an order-capable profile
   while degraded resets the active/cleared sets, retains the marker, and
   requires every current active symbol to clear again before account recovery.
8. A snapshot-builder, store, active-symbol discovery, or predicate exception
   fails closed and retains degraded state.
9. Concurrent symbol intents share one account clearance operation and cannot
   race a marker clear or submit without their own current successful check.
10. Existing US-paper opt-in, dashboard-profile, duplicate-order, risk, order
    authority, and `place_order()` attempt-recording tests retain their current
    behavior.

## Rollback / kill switch

For a mock-only rollout, add a startup-read, account-specific feature flag in
the authorized implementation, defaulting disabled until the wiring is tested.
When explicitly enabled for `us_mock`, the gate behaves as above. A temporary
operator kill switch may disable the new dispatch block without a source
revert, but it must be narrowly scoped to `us_mock`, emit a durable/highly
visible warning, and require an explicit operator action. It must never apply
to real accounts and must not clear degraded state or unattributed attempts;
it only restores the prior observational behavior for controlled mock
diagnosis.

## Decision requested

Authorize a separate implementation round only if the operator accepts:

1. pre-dispatch rejection, not queueing or replay;
2. fail-closed behavior for clearance-evaluation errors;
3. account-scoped synchronization with symbol-specific snapshots;
4. a mock-only, auditable startup feature flag for controlled rollback; and
5. per-symbol clearance tracking and re-evaluation on every dispatch while the
   account remains degraded, with account recovery only after the active-symbol
   set is complete.

