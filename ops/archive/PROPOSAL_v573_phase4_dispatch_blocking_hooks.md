# Round 573 - Phase 4 dispatch-blocking hook proposal

## Status and evidence boundary

This is a design-only proposal. It creates no source, test, configuration,
worker, Git, or account behavior. It is based only on the current source paths
listed below and requires a separately authorized implementation round.

The current source has a pure
`ReconciliationClearanceSnapshot`/`evaluate_reconciliation_clearance()` model,
but no production caller constructs that snapshot. The condition-5 helper states
verbatim that it is an integration point "until real snapshot construction is
wired." No live path assigns `incomplete_reasons` or
`unresolved_order_ids`.

## 1. Proposed snapshot-builder contract

Add an `AccountEngine` instance method in a separately authorized
implementation:

```python
async def _build_reconciliation_clearance_snapshot(
    self,
    symbol: str,
    *,
    max_balance_age_sec: float,
) -> ReconciliationClearanceSnapshot:
```

The dispatch-clearance service calls it only for the degraded mock account,
with `max_balance_age_sec=1.0`. The method obtains a raw balance response
through the parameterized shared-balance method proposed in Section 3, and
must record whether that response was obtained within the requested age bound.

For US holdings, the existing live path already provides:
`normalize_us_holdings(raw_balance)`, `us_balance_recognized(raw_balance)`,
and the current-symbol lookup using `_same_symbol(..., self.ctx.strategy.symbol)`
in `AccountEngine._reconcile_balance()`. The builder should reuse those
normalizers and construct `NormalizedBalanceHolding` for the requested
symbol, preserving the requested symbol rather than treating a missing row as
a different symbol.

The following existing state is related to condition 3 but is not currently
assembled into a clearance result:

- `_broker_fill_catchup_qty[symbol_key]` identifies the broker-fill catch-up
  hold;
- `ledger.pending_orders(self.ctx.strategy.symbol)` and a quantity mismatch
  identify the pending-quantity deferral in `_reconcile_balance()`;
- `_symbol_lifecycles[symbol_key]` plus the lifecycle minimum comparison
  identifies the stale-lifecycle hold; and
- `_trading_paused` with
  `_pause_reason == "broker_quantity_unattributed"` identifies the
  unattributed-quantity pause.

The implementation should extract one shared, side-effect-free classifier used
by both `_reconcile_balance()` and the new builder. It returns
`frozenset[ReconciliationIncompleteReason]` without mutating positions,
dashboard state, lifecycle files, or ledger state. This avoids a second,
independently maintained interpretation of the same branches.

Condition 4 requires a new explicit per-symbol collection function. Its input
must combine existing `ledger.pending_orders(symbol)` with existing
`ledger.execution_recovery_orders(symbol)`, returning stable order IDs for
all orders not resolved by broker execution/unfilled-order data. The present
source has both ledger queries in the ordinary sync path but no single live
`unresolved_order_ids` value. Condition 5 remains the existing
`with_unattributed_collision_order_ids(snapshot, data_dir=DATA_DIR)` call.

## 2. Proposed active-symbol interface and reset owner

Extend `SymbolEngineRegistry` with an account-wide method:

```python
def running_symbols(self, account_id: str) -> frozenset[str]:
```

It returns canonical symbols whose slots are `EngineState.RUNNING`, using the
existing `key()` normalization. This exactly represents the running strategy
engines specified by Round 570; it excludes STARTING, STOPPING, STOPPED, the
balance-only monitor, configured-but-not-running symbols, and historical
holdings.

Avoid importing `src.main` from `engine.py`. Instead, add an optional
`active_symbols_provider: Callable[[], frozenset[str]] | None` constructor
argument to `AccountEngine`. `run_symbol_engines()` owns the
`SymbolEngineRegistry` instance and passes an account-bound provider when it
constructs each symbol engine. Balance-only engines receive no provider and
cannot participate in dispatch clearance.

The account-scoped dispatch-clearance service owns the active/cleared-set reset.
Immediately before every degraded dispatch, it calls the provider and compares
the returned set to the episode's `active_symbols`. A difference resets
`cleared_symbols` to empty and replaces `active_symbols`; it does not clear
the degraded marker. Registry claim/mark-running/request-stop/release events
therefore need no direct callback into every engine.

The current source does not expose an account-wide order-capable-profile
fingerprint. `AccountEngine._refresh_dashboard_controls()` holds per-engine
profile state, while the registry only owns task state. Therefore, a profile
change that keeps an engine running cannot yet be detected account-wide by the
proposed provider. This needs an explicit decision before implementation:
either define active symbols solely as RUNNING engines (the concrete source
definition above), or add a worker-owned account profile-version provider and
reset when that version changes. The latter is required if Round 570's broader
"order-capable profile change" reset rule must apply even without an engine
membership change.

## 3. Balance-gate resolution

Recommend extending the existing account-scoped
`AccountEngine._shared_broker_balance()`, not adding an independent transport
seam. Its existing `_AccountBalanceGate.lock`, `raw_balance`, and
`received_at` already serialize and share account balance requests among
symbol engines. Its ordinary behavior is controlled by
`self.balance_min_interval_sec`, sourced from
`KIWOOM_BALANCE_MIN_INTERVAL_SEC` with current default `1.5`.

Use a parameterized signature:

```python
async def _shared_broker_balance(
    self,
    *,
    max_age_sec: float | None = None,
) -> tuple[dict, float]:
```

With `max_age_sec is None`, retain the current normal-reconciliation
`balance_min_interval_sec` behavior exactly. With `max_age_sec=1.0`, use
the same account lock and monotonic `received_at`, but reuse a response only
when its monotonic age is at most one second; otherwise fetch
`self.ctx.client.get_balance()` once while waiters share the in-flight task.
Return the monotonic receive time so the builder can enforce condition 1 rather
than infer freshness from a generic cache flag.

This does not create a second broker transport or alter non-degraded callers.
A fetch, age check, malformed response, task cancellation, or waiter error
must produce no usable response for the dispatch-clearance path and must be
reported as a typed fail-closed block.

## 4. Open decisions and mechanical details

### Explicit operator decisions still required

1. Approve the exact condition-3 classifier mapping from existing live branches
   to `ReconciliationIncompleteReason`, particularly whether any additional
   `_reconcile_balance()` early returns count as incomplete.
2. Approve the condition-4 definition of "unresolved" and its use of
   `pending_orders()` plus `execution_recovery_orders()`; current source
   does not expose one canonical unresolved-ID collection.
3. Decide whether active-symbol reset means only RUNNING registry membership or
   also profile/config changes that keep an engine RUNNING. The latter needs a
   new worker-owned profile-version interface.
4. Decide whether direct future `KiwoomClient.place_order()` callers require
   a clearance capability in the first implementation increment. Current
   automatic dispatch has one engine seam, but no current client-level gate.

### Mechanical implementation details after those decisions

- Add `active_symbols` and `cleared_symbols` to the immutable degraded
  episode state and update them atomically under the account service lock.
- Add the registry enumeration method and inject the provider from
  `run_symbol_engines()`.
- Parameterize `_shared_broker_balance()` so normal callers retain the
  existing interval while degraded clearance uses one second.
- Add the builder, typed block error, dispatch seam, and tests only after the
  three data/ownership decisions above are approved.

