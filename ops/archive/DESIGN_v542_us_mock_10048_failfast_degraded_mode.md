# Round 542 — `us_mock` fixed-port `WinError 10048` fail-fast/degraded-mode design

## Status and scope

This is a design record only.  It makes no source, firewall, process, Git, or
network-policy change.  It is intended to guide a separately authorized
implementation after review.

## 1. Current exhaustion path and its effect

The fixed-port connector in `src/core/broker_http.py` retries a bind/connect
collision for a bounded 2.5-second window.  It retries only address-in-use
errors (`WinError 10048` on Windows or `errno 98` on POSIX), then raises the
last socket error when the time budget expires.  The AnyIO backend maps that
`OSError` to `httpx.ConnectError`.

The token manager and REST client convert `httpx.RequestError` into
`RetryableError`; the REST post path has its own three-attempt retry wrapper.
In the engine, reconciliation failures return `False`, and `_tick()` returns
before strategy/order processing.  The outer engine run loop also isolates an
unexpected tick exception, emits an error notification, records heartbeat
work, and continues to the next scheduled tick.

Therefore an exhausted collision does **not** presently terminate the worker
process.  It already has a fail-closed effect when reconciliation cannot be
completed, but it is not represented as a named, account-scoped degraded mode.
Later operations can spend the bounded retry budget again on later ticks, and
ordinary failure notifications can recur.

An order-path request that failed after transmission is potentially ambiguous.
The proposed mode must not queue or blindly replay such an order; recovery must
instead use authoritative reconciliation as defined by Section 4's five-part
clearance predicate to decide what occurred.

## 2. Fail-fast rule

Keep the existing 2.5-second connector window as the short-lived-collision
filter.  Do not replace it with a 150-second wait: the recorded close/release
measurements show that the long collision is materially longer than the
connector budget, while a separate observed recovery completed in about
375 milliseconds after three retries.

When that existing budget exhausts with a classified fixed-port collision,
enter a dedicated degraded state instead of treating it as another generic
per-operation retry.  This design deliberately selects no new recovery interval
or retry budget; those are operational policy choices requiring separate review.

## 3. Proposed account-scoped degraded state

Use state shared by all broker operations for the affected account, containing
at least:

- entry time and last classified collision time;
- triggering operation/category;
- next eligible controlled recovery probe; and
- one-shot entry alert plus rate-limited ongoing status.

Enter only for the narrow fixed-port signature (`WinError 10048` / `errno 98`)
after the connector's current bounded retry is exhausted.  Do not classify
unrelated connection errors as this condition.

While degraded:

- skip normal broker-dependent work until the controlled recovery probe is due;
- keep reconciliation incomplete, so the existing fail-closed order boundary
  prevents new strategy order decisions;
- do not enqueue, replay, or synthesize orders from failed requests;
- retain heartbeat and control-loop activity so the worker remains observable
  and can accept an operator pause/stop action; and
- preserve existing account/order authority and reconciliation safeguards.

This is a worker-health degradation, not evidence that the account's broker
state is known.  A `RUNNING` process alone must not be displayed as healthy.

## 4. Recovery detection and controlled re-entry

Recovery should be a single shared, bounded probe rather than every caller
independently retrying. A successful socket connection, token refresh, or quote
alone is insufficient to clear the state.

### Clearance predicate

Clear the degraded marker only when all five conditions below hold for the
affected account and strategy symbol:

1. The probe obtained a **fresh, non-cached** US `ust21070` balance response.
   The existing shared-balance cache is not sufficient for degraded clearance.
2. The response is recognized and normalized for the account and symbol. An
   unrecognized response, or malformed/unusable target holding, is incomplete;
   a recognized authoritative absence is the explicit zero-holding result.
3. The reconciliation completed without an incomplete-state early return:
   unrecognized balance response, broker-fill catch-up marker,
   pending-quantity deferral, stale-lifecycle hold, or
   unattributed-quantity pause all prevent clearance.
4. Every known pending/recovery order is resolved by broker execution or
   unfilled-order data and reflected in the durable ledger; a request that has
   merely been sent, or a balance delta without that resolution, is not enough.
5. No unattributed collision-period order remains outstanding. If none was
   flagged for the episode, this condition is trivially satisfied and requires
   no operator action.

The existing `_reconcile_balance()` call is not this predicate by itself: it
returns normally on several incomplete-state paths, and its caller currently
treats a non-exceptional return as a successful sync. An implementation must
therefore expose an explicit predicate result rather than infer clearance from
the absence of an exception.

### Manual resolution of an unattributed collision-period order

`place_order()` currently receives a generic `RetryableError` from a failed
transport call, and the engine only logs `Order failed`; it has no persistent
dispatch-phase marker and no ambiguity record. This is a small implementation
gap. The safe implementation must record an attempted order before dispatch and
mark a potential unattributed order when a classified fixed-port failure occurs
after that attempt enters the order path but no `ord_no` is returned. Because
the present transport error does not prove whether bytes reached the broker,
the marker must err on the safe side rather than claim it can distinguish
pre-send from post-send failure.

The entry alert and degraded dashboard state must show the account, episode
time, side, symbol, quantity, price/order type, operation/error details, and
that automation is blocked pending manual order resolution. The operator checks
the broker web/app order and execution history, plus the account holding, using
that recorded attempt data to determine whether the request was accepted,
filled, rejected, cancelled, or absent.

An explicit authenticated operator resolution, distinct from normal degraded
recovery, clears this block. It must durably record who confirmed it, when,
the recorded attempt identifier, and the attested broker outcome. It cannot
clear the block merely because connectivity returned. The ordinary five-part
predicate must still succeed afterwards; this confirmation never queues or
replays the original order.

On a satisfied clearance predicate:

1. clear the account's degraded marker atomically;
2. record a recovered event with duration and reconciliation result; and
3. resume the ordinary next-tick workflow, subject to all pre-existing gates.

If the probe fails with the same classified collision, retain degraded state and
schedule the next policy-controlled probe.  If it fails differently, preserve
the existing error handling and record the distinct failure reason rather than
misrepresenting it as a port collision.

## 5. Operator visibility

An implementation should expose explicit structured events such as:

- `fixed_port_degraded_entered` (account, operation, error code, entry time);
- `fixed_port_degraded_still_unavailable` (rate-limited elapsed duration and
  next probe); and
- `fixed_port_order_unattributed_flagged` (account, episode reference, side,
  symbol, quantity, price/order type, and timestamp);
- `fixed_port_order_manually_resolved` (who confirmed, when, recorded attempt
  identifier, and attested broker outcome); and
- `fixed_port_degraded_recovered` (duration and successful reconciliation).

The dashboard/status surface should distinguish `RUNNING / DEGRADED_FIXED_PORT`
from healthy `RUNNING`, and show that new order decisions are fail-closed while
reconciliation is unavailable. While an unattributed order is unresolved, it
must show `RUNNING / DEGRADED_FIXED_PORT / MANUAL_RESOLUTION_REQUIRED` as a
distinct sub-state. Alerts should fire on entry and recovery, with rate-limited
ongoing status rather than one notification per tick.

## 6. Risks and safeguards

- **Missed trading opportunity:** fail-fast deliberately sacrifices availability
  during a long local-port collision; this is preferable to operating without
  authoritative reconciliation (see Section 4's five-part clearance predicate).
- **Stale or unknown holdings:** no recovery is declared from connectivity alone;
  complete reconciliation is mandatory (see Section 4's five-part clearance
  predicate).
- **Ambiguous order transmission:** never replay failed order requests solely
  because the port later becomes available. Manual resolution is a deliberate
  scope boundary while broker-side automatic correlation is unconfirmed, not an
  oversight.
- **Manual confirmation error or delay:** an operator can misread broker history
  or delay recovery. Require the recorded attempt details and a durable,
  attributable attestation; leave automation blocked until it is supplied.
- **Concurrent callers:** degraded entry and recovery must use shared,
  account-scoped synchronization to avoid duplicate probes or premature clear.
- **Misclassification:** enter only on the exact address-in-use signature after
  the current connector budget exhausts.
- **Restart behavior:** decide explicitly whether degraded evidence is persisted
  or recovered by mandatory startup reconciliation; do not silently treat a
  restart as proof of recovery.
- **Alert fatigue:** rate-limit continuing notices but retain durable status and
  entry/recovery events.

## 7. Estimated implementation scope

This is a medium, cross-cutting change: connector error classification, a fresh
balance probe and explicit five-part reconciliation result, shared account
state, known-order resolution, potential-unattributed-order recording,
authenticated manual-resolution UI/control flow, worker/dashboard status,
operator alerts, and deterministic tests for entry, suppression, incomplete
predicate states, manual resolution, recovery, and ambiguous-order non-replay.
It should be implemented only under a separate source-change authorization,
with no firewall-policy change implied or authorized by this document.
