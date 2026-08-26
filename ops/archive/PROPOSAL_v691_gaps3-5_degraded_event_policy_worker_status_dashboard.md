# Proposal v691 — Gaps 3–5: degraded-event policy, worker status, and dashboard display

## Status and decision boundary

This is a design proposal only. It authorizes no application-code change, test,
configuration change, runtime action, worker action, or Git staging/commit/push.

Scope is limited to the two mock accounts. The proposal does not create an order,
recovery, clearance, attestation, or control path. Existing fail-closed behavior
must remain authoritative: a PID, heartbeat, socket, token, quote, dashboard
response, or status value is not broker-reconciliation proof.

## Confirmed seams

- `FixedPortDegradedState` already has account-scoped `entry_alert_fired` and
  `last_ongoing_status_at` fields, but production code does not use them.
- `write_pause_clear_event()` persists an authenticated, reason-scoped event, but
  `PAUSE_CLEAR_REASONS` has no fixed-port-related reason.
- `_publish_worker_heartbeat()` always writes `"RUNNING"`.
- `worker_supervisor.status()` derives `running` from `lock.is_alive()`, not from
  status metadata. A `DEGRADED_FIXED_PORT` metadata value therefore does not
  change worker liveness reporting.
- `/api/status` already returns worker payloads including `workers[].state`, but
  the current controller renders only one aggregate `running` boolean and account
  list. It does not render an individual worker state.

## Gap 3 — Structured degraded/recovery event policy

### Required behavior

The eventual policy must provide account-scoped, operator-visible events for
fixed-port degraded entry, ongoing degraded availability, manual-resolution
requirement, and verified recovery. It must rate-limit only repeated
operator-visible ongoing events; it must not suppress degraded entry, alter
dispatch blocking, clear degraded state, or treat event emission as recovery.

### Proposed use of existing state fields

**Recommendation, pending operator confirmation:** wire
`entry_alert_fired` and `last_ongoing_status_at` on the existing
`FixedPortDegradedState` rather than introduce another in-memory state carrier.
The fields already match the required entry-event and recurring-status-event
shape. Entry handling would emit one account-scoped event and record that the
entry event was emitted. While the account remains degraded, later checks would
emit an ongoing-status event only when the rate-limit interval has elapsed and
would update `last_ongoing_status_at`. Successful full clearance would emit a
separate recovery event before or when the existing degraded state is cleared;
the exact ordering must preserve the current successful-clearance condition.

**Operator decision required:** confirm that these two existing fields are the
approved event-rate state, rather than requiring a durable event store or a new
state object. The current fields are in-memory and no durable fixed-port event
sink has been confirmed by reconnaissance.

### Operator visibility and event content

Each event should be structured and account-scoped. The minimum proposed fields
are event type (`degraded_entered`, `degraded_ongoing`,
`manual_resolution_required`, or `degraded_recovered`), account identifier,
operation, event time, original degraded-entry time, last collision time, and
next recovery-probe time when available. The event destination is an open design
decision: existing structured logging is evidenced, while durable fixed-port
event persistence is not.

**Operator decision required:** choose the operator-visible destination and
durability requirement. No design should claim durable delivery unless a
specific existing persistence seam is selected and verified.

### Ongoing-event rate limit

**Recommendation, pending operator confirmation:** emit at most one
`degraded_ongoing` event per account every 15 minutes while that account remains
degraded. Keep the interval as a module-level constant beside the fixed-port
degraded-state policy, rather than add configuration in the first implementation
slice. The account-scoped `last_ongoing_status_at` field supports this comparison.

**Operator decision required:** approve the 15-minute interval and the
constant-versus-configuration choice. A configuration source, default, validation
rule, and deployment contract have not been established in the confirmed seams.

### Manual resolution

Two options are available:

1. Extend `PAUSE_CLEAR_REASONS` with one fixed-port-specific reason and reuse
   `write_pause_clear_event()` for an authenticated, reason-scoped operator
   record.
2. Create a parallel fixed-port-specific manual-clear record and consumer.

**Recommendation, pending operator confirmation:** reuse the existing
reason-scoped authenticated event mechanism by adding one fixed-port-specific
reason. This is the narrower documented seam. The resulting event must record
manual acknowledgement only; it must not clear degraded state, re-enable
dispatch, trigger a probe, or substitute for the complete recovery predicate.

**Operator decision required:** approve reuse and specify the intended semantics
of the fixed-port event: acknowledgement, resolution request, or another
operator record. The engine-side consumer and its safety predicate are not yet
defined.

## Gap 4 — Worker status `DEGRADED_FIXED_PORT`

### Proposed mechanical change

`_publish_worker_heartbeat()` currently writes `"RUNNING"` on every interval.
The eventual implementation should read
`get_fixed_port_degraded_state(identity.account_id)` before each heartbeat
publication, write `"DEGRADED_FIXED_PORT"` while a state is present, and resume
writing `"RUNNING"` once that state is absent. The initial worker-status write
and any other status publication paths require a call-site inventory before code
changes, so the same state is not overwritten by another writer.

The worker supervisor requires no change for this status value: its `running`
field is `lock.is_alive()` and is independent of metadata `state`. A degraded
worker therefore remains live while publishing `DEGRADED_FIXED_PORT`.

### Enum decision

`EngineState` currently declares `STARTING`, `RUNNING`, `STOPPING`, and
`STOPPED`; status call sites already publish status strings. The worker-status
field is not enforced as an `EngineState` value by `worker_supervisor.status()`.

**Recommendation, pending operator confirmation:** add
`DEGRADED_FIXED_PORT` to `EngineState` for a documented canonical value, while
continuing to publish the string value through the existing status path.

**Operator decision required:** approve adding the enum member versus using a
literal string only. The confirmed current behavior permits either; this proposal
does not choose silently.

### Required tests before implementation approval

Tests should demonstrate that a degraded state changes the published metadata
state to `DEGRADED_FIXED_PORT`, clearing it restores `RUNNING`, and the
supervisor still reports `running: true` when the account mutex is alive. Tests
must also demonstrate that this status distinction does not clear degraded state
or weaken dispatch blocking.

## Gap 5 — Dashboard degraded/manual-resolution display

### Minimal display-only surface

The minimal new UI is a per-account status list rendered from the existing
`workers[].state` payload returned by `/api/status`. Each row should show the
account identifier and one explicit state label, distinguishing `RUNNING` from
`DEGRADED_FIXED_PORT`. When the backend supplies a confirmed
manual-resolution-required indicator, the same account row should display that
sub-state distinctly.

The UI must remain display-only. It must not add a dashboard recovery button,
manual-clear button, probe trigger, or attestation action. Any authenticated
operator action remains outside this dashboard proposal and must retain its own
server-side account, identity, reason, and authorization checks.

### Manual-resolution-required data contract

`workers[].state` alone does not encode manual-resolution-required status.

**Operator decision required:** choose the authoritative account-scoped backend
field and source for this indicator before implementation. The dashboard must not
infer it from a stale status file, a liveness value, an event, or a missing
recovery response.

### Account scope and route boundary

The display must be account-scoped and must not add paths or controls for
out-of-scope accounts. Existing reconnaissance found that `/api/status` and
`/api/accounts` do not use `_reject_real_account()`, unlike other account routes.

**Recommendation, pending operator confirmation:** defer that pre-existing route
boundary issue as separate housekeeping rather than combine it with the narrowly
scoped display work. Gap 5 should consume only the already-authorized mock
account payloads and should not broaden route behavior.

**Operator decision required:** confirm deferral, or explicitly authorize a
separate route-boundary review and change set. No route-gating change is proposed
or authorized by this document.

### Required tests before implementation approval

Tests should prove that the display renders one account's `RUNNING` and
`DEGRADED_FIXED_PORT` values distinctly, shows the manual-resolution-required
indicator only when its authoritative field is present, and adds no state-changing
dashboard request. They should also prove account-scoped rendering and retain the
existing aggregate liveness display without treating it as recovery proof.

## Implementation authorization gates

Before any implementation, the operator must separately authorize:

1. the selected Gap 3 decisions, including event destination, durability,
   rate-limit interval, and manual-resolution semantics;
2. the exact source, test, and configuration files for Gap 4;
3. the authoritative backend data contract and exact UI files for Gap 5;
4. a patch dry run, application, focused tests, path-scoped staging, and commit
   as separate actions.

No implementation may enable the reconciliation-clearance flag, submit an order,
restart a worker, alter fixed-port ownership, or treat a status transition as
recovery proof.
