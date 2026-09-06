# Proposal Addendum — Round 2294

Date: 2026-09-05  
Subject: Unified correction for dashboard-activation lifecycle ordering and pause scoping

## Scope

This is a design-only correction. No source or test file was modified, and no
staging, commit, push, runtime, Scheduler, or real-account action was
performed.

## Two diagnosed defects

The Round 2289-applied block currently performs the fresh clearance check at
lines 1138–1161, clears pause state at line 1163, and only then prepares the
lifecycle at lines 1169–1171:

```text
1138:            matching_engines = [
1139:                engine for engine in list(self._balance_gate.engines)
1140:                if engine._trading_paused and engine._pause_reason == self._pause_reason
1141:            ]
1142:            if not matching_engines:
1143:                matching_engines = [self]
1144:            for engine in matching_engines:
1145:                symbol = strategy.symbol if engine is self else engine.ctx.strategy.symbol
1146:                try:
1147:                    snapshot = await engine._build_reconciliation_clearance_snapshot(
1148:                        symbol, max_balance_age_sec=1.0,
1149:                    )
1150:                    result = evaluate_reconciliation_clearance(snapshot)
1151:                except Exception as exc:
1152:                    self.ctx.logger.warning(
1153:                        f"Dashboard profile activation pause clear deferred for {symbol}: clearance check failed: {exc}"
1154:                    )
1155:                    return
1156:                if not result.cleared:
1157:                    details = "; ".join(failure.detail for failure in result.failures)
1158:                    self.ctx.logger.warning(
1159:                        f"Dashboard profile activation pause clear deferred for {symbol}: {details}"
1160:                    )
1161:                    return
1162:            for engine in matching_engines:
1163:                engine._trading_paused = False
1164:            self.ctx.logger.info(f"Cleared stale trading pause after dashboard profile activation for {strategy.symbol}")
1165:            # A profile re-enabled after a full close is a fresh manual-first
1166:            # lifecycle. Only an already-open lifecycle may restore fills;
1167:            # otherwise the imminent broker snapshot adopts tranche 1 and old
1168:            # reporting rows cannot blend into it.
1169:            self._prepare_lifecycle_scope(strategy.symbol)
1170:            if self._lifecycle_pending_adoption:
1171:                self._begin_manual_lifecycle_activation(strategy.symbol)
```

### Failure 1: lifecycle ordering

`_prepare_lifecycle_scope()` determines whether broker-adoption mode is needed
at lines 1857–1868. `_begin_manual_lifecycle_activation()` creates the
`activation_id` at lines 1870–1888. Neither method reads the fresh clearance
result, and neither method clears `_trading_paused`.

The corrected design therefore runs lines 1169–1172, including lifecycle
preparation, before the fresh predicate block. A failed or incomplete fresh
predicate may defer only the dashboard-authorized pause clear; it must not
return before lifecycle preparation and activation have established the new
manual-first boundary.

This resolves the baseline-confirmed `activation_id` regression without
making lifecycle activation a pause-clear side channel.

### Failure 2: pause-reason scoping

After lifecycle preparation, the fresh-check block may clear only engines whose
pause reason is exactly `broker_reconciliation_unavailable`, the stale broker-
mismatch reason described by the activation comment at lines 1135–1137.

The corrected matching predicate is conceptually:

```text
engine._trading_paused is True
engine._pause_reason == "broker_reconciliation_unavailable"
```

The fallback that treats `self` as a match when no authorized engine exists
must not clear a paused engine with an empty, unrelated, or unknown reason.
If no dashboard-authorized engine matches, no pause state is changed. If
multiple authorized engines match, all fresh checks must pass before any of
them is cleared, preserving the existing all-or-nothing behavior.

## Corrected control-flow design

The next Draft should make `_refresh_dashboard_controls` asynchronous and
retain the two production `await` call sites. Within its profile-activation
branch, the order should be:

1. Construct and install the validated strategy and position state.
2. Run `_prepare_lifecycle_scope(strategy.symbol)`.
3. If `_lifecycle_pending_adoption` is true, run
   `_begin_manual_lifecycle_activation(strategy.symbol)`.
4. Perform the fresh reconciliation checks for the narrowly authorized
   `broker_reconciliation_unavailable` matching engines.
5. Clear `_trading_paused` only after every authorized matching engine passes.
6. Continue the existing ledger restore, dashboard fingerprint, subscription,
   and per-side control updates.

The fresh broker calls remain outside `_sync_lock`, as established by the
current async callers. Lifecycle preparation and pause clearing are two
independent concerns in one function: lifecycle setup must not depend on the
predicate result, while pause clearing must depend on both the exact authorized
reason and a successful fresh predicate.

## Test fixture impact

The five Round 2290 dashboard-clearance scenarios do not require weakened
assertions:

- all-matching-pass and partial-failure fixtures use
  `broker_reconciliation_unavailable` and remain valid;
- the exception fixture uses the same authorized reason and remains valid;
- the no-match fixture has no paused matching engine and must continue leaving
  state unchanged;
- the lock-interference fixture continues to verify the network call is not
  made under `_sync_lock`; and
- the fixed-port scenario remains an independent regression guard.

The lifecycle tests require no semantic fixture change for the corrected
ordering. Their existing async-call migration remains part of the Test gate;
the `activation_id` test must again reach lifecycle preparation even when the
fresh predicate cannot clear a prior reconciliation pause.

## Explicit exclusions

`resume_trading()`, `_apply_reconciliation_clear_event`, and
`_apply_fixed_port_pause_clear_event` remain untouched. No third production
site or broader restructuring is required by this unified design.

## Verification boundary

Current evidence confirms the two defects share the dashboard-activation
branch but require separate safeguards: lifecycle preparation before any
predicate early return, and exact reason-scoping before any pause clear. This
addendum is sufficient to produce a corrected Draft in the next round.
