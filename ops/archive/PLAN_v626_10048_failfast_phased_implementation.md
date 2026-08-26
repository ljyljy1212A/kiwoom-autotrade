# Round 626 — Phased implementation plan for fixed-port `10048`

## Status and planning boundary

This is a planning document only. It does not authorize source edits, test
edits, worker actions, Git operations, firewall changes, or real-account
access.

The current checkout must be treated as a dirty baseline. Read-only source
inspection already found several v542-shaped pieces present in the working
tree: `FixedPortCollisionError` and account-scoped degraded-state helpers in
`src/core/broker_http.py`, reconciliation-clearance and dispatch seams in
`src/core/engine.py`, order-attempt persistence in
`src/data/order_attempts.py`, and focused tests including
`tests/test_reconciliation_clearance.py`, `tests/test_order_attempts.py`, and
`tests/test_dispatch_clearance_integration.py`. Therefore every implementation
phase begins with a fresh file/diff baseline and must extend or verify the
existing behavior rather than assume the v542 design is absent.

## Phase 0 — Baseline and seam reconciliation

This is a read-only gate before coding. Inventory the current implementation
against v542, identify which design requirements are already present, and
record exact gaps. Do not replace existing dirty work or infer that existing
helpers are production-complete from their names alone.

Likely files to inspect:

- `src/core/broker_http.py`
- `src/core/engine.py`
- `src/core/kiwoom_client.py`
- `src/data/order_attempts.py`
- `src/worker_supervisor.py`
- `dashboard/dashboard_server.py`
- `dashboard/index.html`
- Existing focused tests under `tests/`

Verification must establish the current call paths from connector failure to
reconciliation, order dispatch, worker status, dashboard status, and alerts.
The result must distinguish already implemented behavior, untested behavior,
and missing behavior. No source or test file is changed in this phase.

Dependency: mandatory before every later phase because the working tree already
contains relevant implementation seams.

## Phase 1 — Connector collision classification

Implement or complete only the narrow classification boundary for an
exhausted fixed-port collision: Windows `winerror == 10048` and POSIX
`errno == 98`, including wrapped exceptions. Preserve the existing bounded
2.5-second connector behavior. This phase must not add degraded behavior,
change retry budgets, alter ports, or touch order submission.

Likely files:

- `src/core/broker_http.py`
- `tests/test_broker_http.py`

Deterministic tests must cover direct and wrapped collision errors, unrelated
socket errors remaining unclassified, exhaustion preserving the original
failure context, and no change to successful connections. This phase covers
the classification prerequisite for degraded-state entry, not entry itself.

Risk: lowest implementation risk; no order-adjacent code.

## Phase 2 — Account-scoped degraded-state model

Implement or complete the shared state model independently of engine behavior.
It should represent account, entry time, last collision, triggering operation,
next controlled recovery probe, and alert-rate state. State must be scoped to
one account and synchronized across concurrent callers. It must not clear from
connectivity, a token refresh, a quote, or a worker restart alone.

Likely files:

- `src/core/broker_http.py`, or a new narrowly scoped shared-state module if
  the baseline audit shows that module ownership is clearer there
- `tests/test_broker_http.py` or a new focused state test module

Deterministic tests must cover first entry, repeated entry updating collision
metadata without resetting the episode, account isolation, concurrent entry,
explicit clear, and refusal to infer recovery from a process/status value.

Dependency: Phase 1 classification should be complete; this phase remains
independently testable and does not wire state into the engine.

## Phase 3 — Engine fail-closed degraded entry and suppression

Wire classified connector exhaustion into the `us_mock` account engine's
fail-closed path. On entry, reconciliation remains incomplete and ordinary
broker-dependent strategy/order work is suppressed. Preserve heartbeat,
control-loop, and operator stop/pause observability. Do not add recovery
clearance yet, and do not modify order transmission in this phase.

Likely files:

- `src/core/engine.py`
- `src/core/broker_http.py` only if the seam requires a narrow integration hook
- `tests/test_dispatch_clearance_integration.py`
- focused engine/reconciliation tests as required by the baseline audit

Deterministic tests must cover entry on the exact classified exhaustion,
normal work suppression while degraded, continued heartbeat/control behavior,
account isolation, no order call before the existing dispatch boundary, and
unrelated errors following their existing path. This phase maps the v542
requirements for degraded entry and suppression.

Risk: medium; it changes engine control flow but still must not change order
submission or replay behavior.

## Phase 4 — Explicit five-part reconciliation clearance predicate

Implement or complete the independently testable predicate from v542 Section
4. Clearance requires: a fresh non-cached `ust21070` balance; recognized and
normalized account/symbol data; no incomplete reconciliation path; all pending
or recovery orders resolved; and no unattributed collision-period order.
The predicate must return explicit condition-level failures rather than infer
success from the absence of an exception.

Likely files:

- `src/core/engine.py` or a dedicated reconciliation-clearance module if the
  baseline audit identifies a cleaner ownership boundary
- `src/data/order_attempts.py` only for read-only marker lookup integration
- `tests/test_reconciliation_clearance.py`

Deterministic tests must cover one passing snapshot, each of the five failure
conditions independently, simultaneous failures, account scoping, symbol
normalization including `A`-prefixed US symbols, shared-cache rejection, and
fresh authoritative zero holdings. This phase directly maps the v542
incomplete-predicate test requirement.

Dependency: Phase 3 supplies the degraded entry context, but the predicate
itself must remain callable and testable without a live worker.

## Phase 5 — Controlled recovery probe and state clearing

Add one shared, bounded recovery probe for the degraded account and clear the
state only after Phase 4's complete predicate succeeds. A successful socket,
token refresh, quote, or restart is not sufficient. A repeated classified
collision retains degraded state; a different failure remains separately
classified.

Likely files:

- `src/core/engine.py`
- `src/core/broker_http.py` or the Phase 4 clearance module
- `tests/test_dispatch_clearance_integration.py`
- `tests/test_reconciliation_clearance.py`

Deterministic tests must cover probe suppression before eligibility, one shared
probe under concurrent callers, successful five-part clearance, each failed
predicate retaining degraded state, repeated `10048` retaining state, a
different error retaining its own classification, and atomic clear/recovered
event ordering. This phase maps the v542 recovery test requirement.

## Phase 6 — Order-attempt ambiguity and authenticated manual resolution

This is the first explicitly order-adjacent phase and is higher risk. Record
an order attempt before dispatch, mark a potential unattributed attempt when a
classified fixed-port failure occurs after entering the order path without an
`ord_no`, and require authenticated durable operator attestation before that
marker can clear. Never queue or replay the original order. Normal degraded
recovery must remain blocked until the manual-resolution requirement and the
five-part predicate are both satisfied.

Likely files:

- `src/core/kiwoom_client.py`
- `src/data/order_attempts.py`
- `src/core/engine.py`
- the existing authenticated control/Telegram/dashboard control path identified
  by Phase 0
- `tests/test_order_attempts.py`
- `tests/test_order_submission_guard.py`
- focused manual-resolution tests

Deterministic tests must cover attempt persistence before dispatch, successful
dispatch leaving no marker, classified post-entry failure creating exactly one
unattributed marker, non-collision rejection creating no such marker,
account-scoped lookup, authenticated attestation persistence, unauthenticated
or mismatched resolution rejection, no replay, and no clearance until the
broker outcome and five-part predicate are satisfied.

Dependency: Phases 1–5 are required. This phase must not be bundled with an
unrelated order refactor.

## Phase 7 — Structured observability and dashboard/status surface

Expose the v542 events and state distinction without changing broker or order
semantics: `fixed_port_degraded_entered`,
`fixed_port_degraded_still_unavailable`,
`fixed_port_order_unattributed_flagged`,
`fixed_port_order_manually_resolved`, and
`fixed_port_degraded_recovered`. The worker/status surface must distinguish
`RUNNING / DEGRADED_FIXED_PORT` and, where applicable,
`MANUAL_RESOLUTION_REQUIRED` from healthy `RUNNING`.

Likely files:

- `src/core/engine.py`
- `src/worker_supervisor.py` if status payload extension is required
- `dashboard/dashboard_server.py`
- `dashboard/index.html`
- existing alert/control modules identified by Phase 0
- focused dashboard/status/observability tests

Deterministic tests must cover event payload account/operation/timestamps,
entry and recovery emission, ongoing-alert rate limiting, degraded status
serialization, manual-resolution sub-state, account filtering, and the
absence of real-account exposure. This phase maps the v542 observability and
dashboard requirements.

Dependency: state, suppression, clearance, and manual-resolution semantics must
be settled first. Presentation must not become an alternate recovery path.

## Phase 8 — Full integration and separately authorized closed-session
`us_mock` verification

Run the complete focused integration set, then the repository's normal full
suite, and perform the separately authorized closed-session `us_mock`
observation from v217. Verify stable PID/instance, fresh heartbeat, successful
REST continuation, complete reconciliation, no repeated unclassified
failures, unchanged US WebSocket `10002` behavior, and no real-account access.
This is verification, not proof that a restart itself recovered broker state.

Likely files changed by implementation before this phase are the files listed
above; the closed-session observation should change no source or configuration.

Deterministic coverage must include all v542 Section 7 categories: degraded
entry, suppression, every incomplete predicate state, manual resolution,
recovery, and ambiguous-order non-replay. The live observation must retain
literal status/log/heartbeat evidence and distinguish current evidence from
historical results.

Dependency: all implementation phases and their isolated tests must pass their
individual gates first. Worker observation requires separate runtime
authorization and must not be combined with source staging or commit.

## Recommended lowest-risk first slice

Start with Phase 1, after the mandatory Phase 0 baseline audit. It has the
smallest blast radius: one exact exception-classification boundary, no state
mutation, no engine control-flow change, no order path, no port change, and
deterministic unit tests using synthetic exceptions. It also resolves the
first correctness ambiguity required by every later phase without claiming
that classification alone improves live health.

## Authorization boundary for every phase

Each phase requires separate authorization for each action type; these gates
must not be combined:

1. authorize implementation of only that phase's named files and behavior;
2. authorize a `git apply --check` or equivalent patch dry-run, without applying
   it;
3. authorize applying the patch to the working tree;
4. authorize the exact test run for that phase, with no implied restart or
   broker access;
5. authorize exact path-scoped staging, if desired;
6. authorize the commit, with the staged diff rechecked immediately before it.

For Phase 8, closed-session worker observation, any start/stop/restart,
network access, broker verification, or dashboard runtime action is a separate
authorization gate. No phase authorizes push, merge, rebase, reset, restore,
cleanup, elevation, or broad staging.

## Non-goals and permanent safety boundaries

- No firewall, WFP, `netsh`, AhnLab, or machine-wide policy change.
- No change to `kr_mock` behavior or to US WebSocket local port `10002`.
- No change to real-account behavior, ports, dashboard access, or data.
- No order replay, blind retry, queueing, or synthetic fill after ambiguous
  transmission.
- No treating a worker restart, PID, `RUNNING` field, connectivity, token,
  quote, or dashboard response as proof of reconciliation recovery.
- No action, query, connection, or reference to `kr_real` or `us_real`.
- No unrelated refactor, cleanup, source restoration, staging, commit, or push.
- No implementation is authorized by this plan itself; each phase remains
  separately gated.
