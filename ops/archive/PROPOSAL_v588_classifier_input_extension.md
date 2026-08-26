# Proposal v588 — Classifier Input Extension for Unattributed Quantity

## Scope and decision boundary

This is a design-only proposal. It does not modify reconciliation, dispatch,
or tests. Its sole purpose is to make the condition-3 classifier capable of
representing the existing positive unmatched-remainder path without duplicating
or changing manual-tranche behavior. It does not alter broker-fill catch-up or
stale-lifecycle handling.

## 1. Current reconstruction inventory

The live path calculates `unmatched_qty = qty - known_tranche_qty` after the
confirmed program allocation (`src/core/engine.py:1928`). If an open lifecycle
has an immutable manual quantity, it restores at most that amount into step 1
and recalculates the remainder (`1941-1959`). If no step 1 exists, the remaining
positive quantity is adopted as manual step 1, including lifecycle persistence,
tranche-base storage, strategy maps, position state, logging, and return
(`1960-1982`). A still-positive remainder then sets the unattributed pause and
returns (`1983-1994`).

The reconstruction has material side effects: `_refresh_open_lifecycle_manual_basis`
writes lifecycle data (`1482-1498`); `_store_tranche_base`, `step_qty`,
`step_prices`, `PositionState`, and pause flags are changed in `1946-1977` and
`1987-1989`. The classifier must not perform any of these actions.

The output needed for condition 3 is the final positive remainder after the
manual-restoration/adoption decision: `unattributed_remainder > 1e-9`.

## 2. Proposed pure extraction

Introduce a pure helper returning an immutable `ManualTrancheAllocation`:

```python
@dataclass(frozen=True)
class ManualTrancheAllocation:
    restored_manual_qty: float
    adopt_manual_qty: float
    unattributed_remainder: float
```

Inputs are broker `qty`, confirmed `known_tranche_qty`, whether step 1 is
already present, lifecycle `manual_qty`, and whether the lifecycle is open.
The helper applies the current order exactly: restore up to immutable manual
quantity first; if no step 1 remains, adopt the remaining quantity as manual;
otherwise expose the positive remainder as unattributed. It neither reads nor
writes ledger, lifecycle, strategy, position, or dashboard state.

`_reconcile_balance()` will call it, then retain the current side effects only
for the returned restore/adopt quantities. The snapshot builder will call it
with the same derived inputs and will not apply side effects. Equivalence must
be tested against the current restart/manual-tranche cases before replacement.

## 3. Classifier contract extension

Extend `_reconciliation_incomplete_reasons()` with
`unattributed_remainder: float = 0.0` and `complete_zero_balance: bool = False`.
`open_rows` remains exclusively the ambiguous-rebuild input; it is not an
allocation result. Add `UNATTRIBUTED_QUANTITY_PAUSE` when either input is true
under the existing `1e-9` threshold.

## 4. Both unattributed paths

1. A recognized complete zero sets `complete_zero_balance=True`; live code
   retains its current dashboard disable/pause behavior and the snapshot blocks
   condition 3 before dispatch.
2. A positive post-allocation remainder sets `unattributed_remainder`; live
   code retains its current tranche-sell/trading pause and snapshot blocks the
   same condition. A restored or newly adopted manual quantity with no remaining
   exposure remains non-blocking, as today.

## 5. Regression surface and verification

Existing live-path coverage includes restart recovery and manual T1 restoration
in `tests/test_manual_tranche_lifecycle.py:371-448`, ambiguous restart handling
at `461-513`, all-automated-tranches-sold manual-basis behavior at `526-580`,
and unattributed broker quantity pause at `619-623`. `tests/test_tranche_rebuild_ambiguous.py:80-179`
guards the reconstruction branch. The implementation must add pure-helper
equivalence tests using these same input states, then rerun those live tests;
classifier-only tests are insufficient.

## 6. Explicit exclusions

This proposal only extracts the determination of unattributed quantity and
routes it into condition 3. It does not redesign or otherwise modify
broker-fill catch-up, stale-lifecycle hold, order dispatch, direct client
gating, or any real-account path.
