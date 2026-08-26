# Proposal v574 — Phase 4 Dispatch Blocking Classifiers

## Scope and decision boundary

This is a design-only proposal for the first engine-owned dispatch-clearance
seam.  It does not add a direct `KiwoomClient.place_order()` gate.  Existing
engine order authority, dashboard controls, and reconciliation continue to be
the only runtime enforcement until a separately authorized implementation.

The Round 575 delta is only:

```python
TRANCHE_REBUILD_AMBIGUOUS = "tranche-rebuild ambiguous"
```

The clearance snapshot/evaluator surrounding that enum predates the delta.

## 1. Condition 3: incomplete-reconciliation classifier mapping

`ReconciliationClearanceSnapshot.incomplete_reasons` is a
`frozenset[ReconciliationIncompleteReason]` (`src/core/engine.py:95-106`), and
the existing evaluator fails condition 3 whenever it is nonempty
(`src/core/engine.py:167-169`).  Snapshot construction must add the following
reason before returning from the corresponding incomplete branch.

| Existing `_reconcile_balance()` branch | Literal existing condition/action | Condition-3 enum member |
| --- | --- | --- |
| Unrecognized balance | `if holding is None:` then `return` (`src/core/engine.py:1558-1564`).  US uses `holding = ... if balance_recognized else None` (`1552-1555`); KR obtains it through `_balance_holding` (`1556-1557`). | `UNRECOGNIZED_BALANCE` |
| Broker-fill catch-up | `expected_qty = self._broker_fill_catchup_qty.get(symbol_key)`; when `qty + 1e-9 < expected_qty`, the method preserves tranche state and `return`s (`1642-1661`). | `BROKER_FILL_CATCHUP` |
| Pending quantity deferral | `pending_for_symbol = self.ledger.pending_orders(self.ctx.strategy.symbol)` and `if pending_for_symbol and abs(float(self.ctx.position.qty) - qty) > 1e-9:` then `return` (`1669-1676`). | `PENDING_QUANTITY_DEFERRAL` |
| Stale open lifecycle | The open lifecycle minimum is `manual_qty + confirmed_qty`; when `lifecycle_min_qty > 0 and qty + 1e-9 < lifecycle_min_qty`, it marks broker catch-up and `return`s (`1696-1712`). | `STALE_LIFECYCLE_HOLD` |
| Unattributed broker quantity | A complete zero snapshot sets `self._trading_paused = True` and `self._pause_reason = "broker_quantity_unattributed"` before `return` (`1680-1689`).  The nonzero unattributed remainder does the same before `return` (`1833-1844`). | `UNATTRIBUTED_QUANTITY_PAUSE` |
| Ambiguous tranche rebuild | If there is no confirmed step 1 and broker quantity remains positive, it sets `self._trading_paused = True`, `self._tranche_sell_paused = True`, and `self._pause_reason = "tranche_rebuild_ambiguous"` before `return` (`1735-1747`). | `TRANCHE_REBUILD_AMBIGUOUS` |

Review confirmation: the other `_reconcile_balance()` returns are not
unmapped incomplete reconciliation paths.  The two `_balance_only` returns
(`1534-1541`, `1609-1610`) are passive account monitoring; `classification ==
"cleaned"` is terminal orphan lifecycle cleanup (`1622-1641`); and the exits
after manual-tranche restoration/isolation (`1807-1809`, `1810-1832`) complete
a successful reconciliation.  No further incomplete early-return branch
remains unmapped.

## 2. Condition 4: unresolved broker order IDs

The snapshot builder for symbol `symbol` must use actual ledger order numbers
only, merge both existing views, remove duplicates, discard empty IDs, and use
a deterministic order:

```python
unresolved_order_ids = tuple(sorted({
    order.ord_no
    for order in (
        ledger.pending_orders(symbol)
        + ledger.execution_recovery_orders(symbol)
    )
    if order.ord_no
}))
```

`PendingOrder.ord_no` is the persisted broker order field
(`src/data/trade_ledger.py:17-28`), so this definition does not synthesize
attempt IDs or execution row IDs.

Both queries deliberately retain the same unresolved states:

```python
# pending_orders
"AND (status='open' OR (status='filled' AND filled_qty<=0) "
"OR status='awaiting_execution_history')"

# execution_recovery_orders
"AND (status IN ('open','awaiting_execution_history') "
"OR (status='filled' AND filled_qty<=0))"
```

Those are the literal predicates at `src/data/trade_ledger.py:115-127` and
`218-227`.  An order is resolved for condition 4 when broker execution data is
durably recorded by `record_fill`: it writes `filled_qty` and assigns
`status = 'filled' if cumulative_qty >= pending.requested_qty else 'open'`
(`173-189`), so a fully filled positive-quantity order no longer matches.
Broker unfilled-order data resolves it when `mark_cancelled()` assigns
`status='cancelled'` (`193-199`) or `mark_closed_unconfirmed()` assigns
`status='closed_unconfirmed'` (`201-207`).  A terminal order explicitly kept
for later broker execution recovery remains unresolved because
`mark_awaiting_execution_history()` assigns `status='awaiting_execution_history'`
(`209-216`).

## 3. Active-symbol reset and account-owned profile version

### Provider ownership and API

Place an account-scoped `DispatchProfileVersion` beside
`SymbolEngineRegistry` in `src/main.py`, and create it inside
`run_symbol_engines()` beside the registry (`src/main.py:394-403`).  It owns:

```python
version: int
last_profile_fingerprint: str | None

observe(account_id, market, dashboard_settings_payload) -> int
```

`observe` canonicalizes the complete account/market-relevant Trade Settings
profile records—not merely the registry's `RUNNING` symbols.  On a changed
canonical fingerprint it increments `version`; its first observation establishes
the baseline.  The provider is account-owned, not an `AccountEngine` feature,
because profile selection and task ownership already live in `src.main`.

### Increment triggers

The provider observes every worker-loop read of the durable settings payload.
The current source confirms that `_enabled_symbol_configs` reads
`dashboard_settings_{account_id}.json` (`361-367`), iterates `profiles`
(`369-371`), limits profiles to the current market and a symbol (`373-375`),
and makes membership depend on `enabled`, recovery need, `auto_buy`,
`auto_sell`, and `monitor_only` (`376-390`).  Therefore a fingerprint change
must include each relevant profile's enabled state and full `config` contents,
including its symbol, market, auto-side settings, and `monitor_only` value.
This preserves the v570 commitment: an active/cleared set resets for a
profile-control/configuration change even when the same task remains RUNNING.

The existing per-engine confirmation of this broader trigger is:

```python
fingerprint = json.dumps(config, sort_keys=True, separators=(",", ":"))
if (symbol != self.ctx.strategy.symbol.lstrip("A")
        or fingerprint != self._dashboard_config_fingerprint
        or (not lifecycle_is_open and not lifecycle_is_pending)):
    ...
    self._dashboard_config_fingerprint = fingerprint
    self._dashboard_strategy_changed = True
```

(`src/core/engine.py:669-698`).  The provider must additionally observe the
profile enabled/list membership that this individual engine can no longer see
after it is stopped.

### Consumption with registry membership

Add `SymbolEngineRegistry.running_symbols(account_id) -> tuple[str, ...]`.
It returns normalized symbols whose slot state is exactly `EngineState.RUNNING`,
sorted deterministically.  This is intentionally narrower than `claim()`—a
claim creates `STARTING` (`107-113`), `mark_running()` makes the transition
(`121-125`), and `request_stop()` uses `STOPPING` (`127-132`).

At the one-second watcher boundary, immediately after obtaining the current
profiles and before dispatch, the account-owned dispatch-clearance service
receives:

```python
profile_version = profile_version_provider.observe(...)
running_symbols = registry.running_symbols(ctx.account_id)
service.observe_active_profile(running_symbols, profile_version)
```

It stores the last `(running_symbols, profile_version)` pair.  Any changed pair
clears both its active-symbol set and its cleared-symbol set; each currently
running symbol must then obtain a fresh clearance snapshot before dispatch.
`run_symbol_engines()` already rebuilds `wanted` each loop (`425-427`), starts
claimed engines (`458-476`), and requests stop for symbols no longer wanted
(`477-481`), so this observation point covers task lifecycle and profile-only
changes together.

This design requires no `engine.py` import of `src.main`: the account-owned
service receives only values (`running_symbols`, `profile_version`) through its
own seam.

## 4. Decision 4 confirmation

Decision 4 is accepted: the first increment is an engine-owned dispatch seam
only.  No direct `KiwoomClient.place_order()`-level reconciliation-clearance
gate is part of this proposal.
