# Round 571 — Phase 4 dispatch-blocking API-load addendum

## Status and scope

This is a design-only addendum to
`PROPOSAL_v570_phase4_dispatch_blocking_v2.md`. It changes no source, test,
configuration, worker, Git, or account behavior. Any implementation requires a
separate source-change authorization.

It applies only to the mock fixed-port degraded path described by the Round 570
proposal. It does not authorize a `kr_real` or `us_real` path.

## Problem restatement

Round 570 requires every order-dispatch attempt while an account remains
degraded to build a fresh, non-cached, per-symbol clearance snapshot. If the
snapshot builder obtains a new `ust21070` balance response for each attempt,
several active symbols can repeatedly request the same account balance during
one degraded burst.

For a representative upper-bound scenario, assume four active symbols and one
dispatch attempt per symbol every five seconds while degraded. That is 48
predicate evaluations and up to 48 fresh balance requests per minute. The
actual order gate may reject some later submissions, but it is after the
proposed clearance seam and does not by itself remove this potential load.

## Candidate options

### Option A — account-scoped single-flight balance snapshot with a one-second TTL

The first degraded dispatch starts one account-scoped fresh balance fetch.
Concurrent dispatches for any active symbol await that same in-flight request.
A successfully completed balance response may be reused only for dispatches
that begin within one second of its completion. Each dispatch still constructs
its own symbol-specific snapshot and independently evaluates conditions 2--5;
the cache contains only the balance response and its fetch time, never a
`ReconciliationClearanceResult`.

At the representative synchronized four-symbol burst every five seconds, this
reduces balance requests from up to 48 to about 12 per minute. It does not help
serial dispatches more than one second apart.

Fail-closed behavior: an expired, absent, malformed, or failed cache entry
cannot satisfy condition 1. The service must fetch again; if that fetch or a
wait on the in-flight request fails, every affected dispatch is blocked. The
implementation must expose the response age and enforce the one-second maximum
rather than treating a generic shared-balance cache as fresh.

Regression behavior: condition 2, condition 3, condition 4, condition 5, and
the per-symbol predicate result are not cached. A later dispatch can demote a
symbol immediately from `cleared_symbols` for those failures. A balance-only
regression can be detected no later than one second plus the next request's
completion time.

Complexity/risk: medium. It needs an account-scoped async single-flight
primitive, monotonic expiry, cancellation/error fan-out, and tests that prove
an existing shared cache cannot be substituted.

### Option B — same-symbol concurrent predicate coalescing only

For one symbol, concurrent dispatch attempts share one in-flight complete
predicate evaluation. No completed result is cached. Once that evaluation
finishes, a later dispatch runs a new full check.

Fail-closed behavior: if the shared evaluation fails, every waiter receives a
block; no waiter may fall back to an older successful result.

Regression behavior: there is no completed-result cache, so a later attempt
checks again immediately. A failure during the coalesced evaluation demotes the
symbol for every waiter.

Complexity/risk: low to medium. It is simpler than a balance cache but has
limited benefit: concurrent attempts for four different symbols can still make
four fresh balance requests, even though they share one account.

### Option C — longer per-symbol successful-clearance TTL

For symbols already in `cleared_symbols`, reuse a prior successful predicate
for a bounded interval such as five seconds; uncleared symbols still recheck on
every dispatch.

Fail-closed behavior requires a missing, expired, malformed, or failed entry
to block and re-fetch. However, it deliberately permits a previously successful
full result for the TTL rather than only sharing one fresh account balance.

Regression behavior is delayed by as much as the configured TTL plus the next
request completion time. A new unresolved order or an unattributed attempt can
therefore remain undetected during that interval.

Complexity/risk: medium. It reduces repeated checks for cleared symbols, but
the delayed demotion is a broader safety tradeoff than Options A or B.

## Recommendation

Recommend Option A with a one-second TTL, limited strictly to sharing the raw
fresh account balance response among concurrent or near-concurrent degraded
dispatches. It materially reduces the realistic burst load while retaining a
small, explicit maximum delay for a balance-only regression. It is preferable
to Option B because the broker balance is account-wide, and preferable to
Option C because it does not cache a successful clearance result or delay
condition-4/condition-5 detection.

The implementation must keep condition 2--5 and the symbol's
`ReconciliationClearanceResult` uncached per dispatch. It must use a
monotonic clock and a single account-scoped lock/task. The one-second bound is
an implementation policy to test, not a claim that a generic balance cache is
fresh.

## No weakening confirmation

This addendum does not alter the previously accepted fail-closed default, no
queue/replay rule, account-scoped synchronization, mock-only kill switch,
per-symbol `cleared_symbols` tracking, or regression demotion. It changes
only how a fresh account balance response may be shared for at most one second
during degraded dispatch checks. On every cache-read, cache-age, fetch, or
single-flight error, dispatch remains blocked. A later per-symbol failure still
removes that symbol from `cleared_symbols`.

## Test plan delta

A separately authorized implementation needs these additional tests:

1. Four concurrent degraded-symbol dispatch checks share one `ust21070`
   request, but each evaluates its own symbol-specific conditions and result.
2. A request starting after the one-second TTL performs a new balance fetch and
   cannot reuse the earlier response.
3. A failed, cancelled, malformed, or expired single-flight result blocks every
   waiter and leaves no usable cache entry.
4. A condition-4 or condition-5 failure after a shared balance response still
   demotes the affected symbol immediately.
5. A balance regression occurring after a successful shared response is detected
   by the next dispatch no later than the one-second TTL plus request
   completion.
6. Concurrent account dispatches cannot create duplicate fresh-balance fetches,
   and cancellation of one waiter does not allow another waiter to fail open.

