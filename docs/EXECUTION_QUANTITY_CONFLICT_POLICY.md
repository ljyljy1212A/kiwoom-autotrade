# Execution quantity conflicts

## Implemented safeguards

The persisted requested quantity is the fill attribution boundary. A finite
observed quantity above it, or a stored fill counter above it, creates an
account/order/symbol-scoped durable conflict. The transaction commits the
diagnostic only; it does not change pending-order counters or confirmed trades.
A failed diagnostic write stops synchronization without reporting success.

The first conflict evidence is retained. Repeated observations update only the
last-seen timestamp. Conflicts survive connection reopen, order completion,
cancellation, and a new strategy lifecycle. They block automated BUY and SELL
dispatch even when reconciliation clearance is disabled. A second dispatch
check covers conflicts discovered during awaited clearance. Generic resume and
pause-clear events cannot resolve a conflict. Read-only consumers treat a
missing conflict table or failed query as unresolved; they do not migrate it.

Completed orders remain observation candidates for later excess quantities.
Non-excess completed history is never applied to the strategy, including history
from an earlier lifecycle. Observation does not turn completed orders into
pending cancellation candidates or unresolved orders.

## Broker specification evidence

Reviewed on 2026-10-01:
[Kiwoom Securities official REST specification](https://github.com/Kiwoom-Securities/Kiwoom-REST-API/blob/main/kiwoom/_data/kiwoom_api_spec.json).
This is a retrieved specification snapshot, not operational evidence.

- Domestic `ka10076` and US `ust21150` expose response continuation headers.
  The client follows `cont-yn` and `next-key`, retaining all pages before
  returning any rows. Missing or repeated continuation keys, malformed lists,
  and more than 100 pages are errors. A nonempty headerless page is incomplete.
- US `ust21150` lists query types 1 through 6; type 5 selects executed orders in
  order sequence. Type 0 is not documented. Both sides use `slby_tp=0`.
- US `ust21150` defaults to today's order date when `ord_dt` is omitted.
  Current polling therefore does not prove recovery of prior trading dates.
- Domestic `ka10076.ord_no` is a history search boundary, not an exact-order
  lookup. Setting it to a specific order does not prove that order's final state.
- The specification names fill quantity and price fields but does not establish
  all row aggregation, cumulative-price, negative-sign, or finality semantics
  needed to resolve a contradictory fill safely.

## Resolution gate

No automatic resolution or conflict deletion API is implemented. A normal later
response, zero unfilled rows, equal account balance, elapsed time, cancellation
acceptance, or a generic resume request is insufficient to erase first evidence.

Before implementing a resolution operation, obtain broker evidence for:

1. Exact account, market, venue, symbol, side, original-order identity, and
   trading date. Order number alone is not assumed unique across trading dates.
2. Complete history across every page and relevant date, including amendments,
   cancellations, duplicate rows, and late execution reporting.
3. Whether quantity is per execution or cumulative, and whether its associated
   price is an individual execution price or a cumulative average.
4. Reconciliation of the conflicting observation with individual executions,
   confirmed ledger rows, and broker holdings without arbitrary clipping.
5. A broker-supported terminal condition that accounts for cancelled remainder
   and the possibility of a delayed fill.

A future operator resolution must use explicit authorization and an atomic
comparison against the exact retained conflict evidence, preserve a durable
audit record, and reject stale or contradictory evidence. It must not silently
edit confirmed trades, delete the conflict, or dispatch an order. None of these
conditions have been operationally validated here; resolution remains
`INCOMPLETE`.

## Limits

Completed-order checks only inspect history actually returned by current
polling; absence is not proof of finality. Retaining completed candidates can
increase execution-history polling. Page requests use the existing REST quota
handling and shared execution-query interval. More than 100 pages fails closed.
The existing cumulative-fill and numeric-sign normalization contracts are
unchanged pending broker evidence. No live account, credentials, broker
transactions, Scheduler, runtime launch, or Canonical publication is required
by the local mock regressions.
