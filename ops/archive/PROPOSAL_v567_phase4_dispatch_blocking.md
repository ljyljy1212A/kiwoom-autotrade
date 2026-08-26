# Round 567 — Phase 4 dispatch-blocking proposal

## Status and scope

This is a proposal only. It adds no source behavior, tests, configuration,
worker action, Git action, or account action. Any implementation requires a
separate source-change authorization.

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

## Proposed insertion and flow

Add one account-scoped asynchronous dispatch-clearance service in a separately
authorized implementation. Insert its call in `_execute_order()` immediately
after the existing US-paper opt-in gate and before
`self.ctx.client.place_order(...)`.

1. If the account is not in `FixedPortDegradedState`, return without changing
   the existing order path.
2. If it is degraded, build a fresh, non-cached `ust21070` reconciliation
   snapshot for the intended symbol; add durable condition-5 IDs through
   `with_unattributed_collision_order_ids()`; then call
   `evaluate_reconciliation_clearance()`.
3. If `cleared` is false, retain degraded state and raise a new, typed
   `OrderDispatchBlockedError` carrying the account, symbol, and clearance
   failures. Do not call `place_order()`, create an attempt record, queue the
   intent, or replay a prior request.
4. If `cleared` is true, atomically clear the account's degraded marker and
   continue through the existing `place_order()` call and its pre-dispatch
   attempt record.

`_execute_order()` is the correct primary seam because it has the strategy
intent, account context, existing user-visible failure handling, and all
current automatic order paths pass through it. A later hardening increment may
also make direct `KiwoomClient.place_order()` callers provide an explicit
clearance token while degraded, but that is not required for the first wiring
increment and should not be silently combined with it.

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

The gate is per-account because `FixedPortDegradedState` and durable attempt
records are account-scoped. The predicate remains per intended symbol because
the normalized holding and reconciliation conditions are symbol-specific. A
shared account-scoped lock/service must serialize one clearance probe so two
symbols cannot independently clear the same account or race into dispatch.

If snapshot construction, durable-attempt lookup, or predicate evaluation
raises unexpectedly, fail closed: retain degraded state and raise
`OrderDispatchBlockedError` with an internal-check failure reason. A failure
to prove reconciliation is safe must not permit a mock order to bypass the
degraded-state control.

## Test plan for the wiring increment

The implementation test set should cover:

1. A non-degraded `us_mock` order still reaches `KiwoomClient.place_order()`.
2. A degraded account with every clearance condition satisfied calls
   `place_order()` once and atomically clears its marker.
3. Each individual condition-1 through condition-5 failure blocks before
   `place_order()` and does not create a new attempt record.
4. An unattested durable collision attempt specifically produces a condition-5
   block; an authenticated attestation alone remains insufficient until the
   full predicate clears.
5. A snapshot-builder, store, or predicate exception fails closed and retains
   degraded state.
6. Concurrent symbol intents share one account clearance operation and cannot
   submit while the result is unresolved.
7. Existing US-paper opt-in, dashboard-profile, duplicate-order, risk, order
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
3. account-scoped synchronization with symbol-specific snapshots; and
4. a mock-only, auditable startup feature flag for controlled rollback.
