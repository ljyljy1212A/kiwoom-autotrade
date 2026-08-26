# Round 631 — `10048` precondition-closure plan addendum

## Status and decision boundary

This is a planning document only. It authorizes no source, test, configuration,
worker, broker, account, or Git action.

`US_MOCK_RECONCILIATION_CLEARANCE_ENABLED` remains `false`. No phase in this
addendum authorizes changing it. Enabling the flag is a separate future
operator decision, gated on full completion of this addendum and the
operator's explicit sign-off.

This addendum supersedes only the obsolete portions of
`PLAN_v626_10048_failfast_phased_implementation.md`. Unaffected v626 phase
content remains the reference design and is not repeated here.

## Completed phases

- Phase 0 — Complete, execution-verified (Round 629): baseline and seam
  reconciliation.
- Phase 1 — Complete, execution-verified (Round 629): connector collision
  classification.
- Phase 2 — Complete, execution-verified (Round 629): account-scoped
  degraded-state model.
- Phase 3 — Complete, execution-verified (Round 629): engine fail-closed
  degraded entry and suppression.
- Phase 4 — Complete, execution-verified (Round 629) as a predicate seam and
  dispatch-clearance integration. Its freshness enforcement remains an open
  completion item listed below.

## Remaining gap set

### Gap 1 — Independent freshness enforcement

`max_balance_age_sec` is not independently enforced as a live age check.
This is a completion item under the original Phase 4, not a new phase.

The implementation must enforce the requested maximum age from a monotonic
receive timestamp, reject absent, stale, malformed, or failed balance data,
and preserve fail-closed dispatch behavior. The existing Phase 4 predicate
tests must be extended with deterministic age-boundary and stale-response
cases.

### Gap 2 — Controlled recovery probe

No shared, bounded recovery probe exists. This is the remaining implementation
scope of Phase 5 from v626.

The probe must be account-scoped and serialized, must be eligible only under
the defined recovery conditions, and must clear degraded state only after a
complete successful Phase 4 predicate. Connectivity, token refresh, quote
success, PID state, or worker restart alone must not clear the state.

### Gap 3 — Structured degraded/recovery event policy

No complete structured event policy exists for degraded entry, ongoing
unavailability, unattributed order marking, manual resolution, and recovery.
This belongs to Phase 7 from v626.

Events must be account-scoped, durable or otherwise operator-visible as
specified by the implementation design, rate-limited for ongoing failures,
and emitted without weakening fail-closed behavior.

### Gap 4 — `DEGRADED_FIXED_PORT` worker status

Worker status does not yet distinguish healthy `RUNNING` from fixed-port
degraded operation. This belongs to Phase 7 from v626.

The status payload and its tests must expose the degraded distinction without
representing a PID, heartbeat, or ordinary `RUNNING` value as recovery proof.

### Gap 5 — Dashboard degraded/manual-resolution display

The dashboard does not yet display fixed-port degraded state and the
manual-resolution-required sub-state. This belongs to Phase 7 from v626.

The display must remain account-scoped, must not expose real-account paths,
and must not become an alternate recovery or attestation path.

### Gap 6 — Authenticated operator attestation call site

Durable ambiguity storage exists, but no authenticated operator call site is
connected for manual resolution. This is the remaining completion item of
Phase 6 from v626.

The call site must validate account, order/attempt identity, reason, and
authorization server-side; persist a non-replaying attestation; and leave
dispatch blocked until both manual resolution and the complete five-part
predicate are satisfied. It must not queue or replay the ambiguous order.

## Revised execution order for open work

Only the following work remains open. Each work package has its own
authorization boundary and must not be implicitly combined with another:

1. Complete Phase 4 freshness enforcement (Gap 1).
2. Implement and verify the Phase 5 controlled recovery probe (Gap 2).
3. Complete the Phase 6 authenticated manual-resolution call site (Gap 6).
4. Implement Phase 7 structured events, worker status, and dashboard/manual-
   resolution surfacing (Gaps 3–5).
5. Perform the separately authorized Phase 8 focused/full integration and
   closed-session `us_mock` observation only after the preceding work passes
   its isolated gates.

For every implementation work package, the authorization sequence remains the
v626 model:

1. authorize implementation of only the named files and behavior;
2. authorize a patch dry-run such as `git apply --check`, without applying it;
3. authorize applying the patch to the working tree;
4. authorize the exact tests for that work package, with no implied worker or
   broker action;
5. authorize exact path-scoped staging, if desired; and
6. authorize the commit after the staged diff is rechecked.

The phase-specific tests must be deterministic and must demonstrate the
required fail-closed behavior before any later runtime observation is
considered. Phase 8 remains verification only and does not make a restart,
PID, heartbeat, connectivity, token, quote, or dashboard response sufficient
proof of reconciliation recovery.

## Permanent boundaries

- Keep the flag disabled; this plan does not authorize enabling it.
- Do not access `kr_real` or `us_real`.
- Do not change firewall/WFP/netsh policy, ports, or real-account behavior.
- Do not queue, replay, blindly retry, or synthesize a fill after ambiguous
  transmission.
- Preserve the dirty checkout and use only path-scoped Git actions when
  separately authorized.

## Recommended starting point

Request authorization for the Phase 4 freshness-enforcement completion first.
It is the lowest-risk remaining item because it is a narrow predicate-boundary
change with no new recovery path, order-adjacent control flow, worker status,
dashboard surface, or operator authority. It directly closes the condition
that currently prevents the five-part clearance result from proving that the
balance evidence is live and fresh.
