# US Mock Launch Maintenance

This document describes the source contract; runtime adoption requires direct
deployment evidence. Only `us_mock / US` uses this policy. It does not change
KR launch behavior.

## Launch eligibility

Every upgraded US supervisor start requires a valid
`data/worker_us_mock.maintenance.json` with `state: RESUMED`. A `PAUSED` record
does not expire. Missing, unreadable, malformed, duplicate-key, mismatched,
hard-linked, or reparsed records refuse startup. Status reads and successful
starts never remove this record.

US starts also require `KIWOOM_WORKER_ROOT_US_MOCK` and
`KIWOOM_WORKER_REVISION_US_MOCK`. The revision is a full Git SHA, the selected
source must match that revision and pass existing source cleanliness checks,
and missing pins cannot fall back to the runtime checkout. The launch
environment is snapshotted and rechecked immediately before spawning.

The supervisor serializes the final maintenance/route check and process spawn
with pause/resume publication using an account launch-policy lock. This lock
is separate from the worker's long-lived account mutex and is released before
the startup acknowledgement wait. Contention and abandoned Windows mutexes
refuse the operation without retry. POSIX uses a nonblocking file lock whose
file is retained. Runtime directories must already exist.

## Explicit maintenance actions

The upgraded supervisor accepts `pause` and `resume` with the existing exact
`--account us_mock --market US` scope. Optional `--reason` describes the
operator action. These actions never stop or start a worker.

The first explicit `pause` creates the maintenance record. Every later pause
or resume requires `--expected-generation` equal to the current record's
UUID hex generation. A new generation and `previousGeneration` are returned.
Keep that output with the operator action evidence. Resume cannot initialize
a missing record. Malformed records require investigation; the commands do
not repair them. Publication is a single atomic replacement attempt and a
failed temporary file is retained.

`changed: null` means an operation did not establish completion; publication
or lock release may have occurred. Reobserve before considering another
separately authorized operation. A startup refusal with `spawned: true`
means the final lock release was unresolved after spawning; do not infer that
the worker is stopped and do not automatically retry.

## Pinned US bootstrap

`tools/us_mock_launch_policy.py` supports `status`, `pause`, `resume`, `start`, `stop`,
and one `watchdog` sweep. It requires explicit absolute `--runtime-root`,
`--source-root`, and `--revision`. Run the script from the reviewed source
checkout; it verifies that its own path matches the selected source and
loads that supervisor/watchdog while keeping data/logs and worker cwd in the
runtime root. The watchdog action targets US mock only. Start/watchdog require
the existing account control to explicitly disable automatic trading.

Stop delegates to the existing supervisor ownership checks and shutdown protocol
using the same runtime as status/start. It does not require RESUMED or clear the
persistent policy. The bootstrap loads missing settings from the runtime dotenv
before application imports and binds explicit US/mock source and state paths
afterward. A routed watchdog uses the runtime dotenv, not the source dotenv.

The common source routing check covers the existing `src` / `src.py` manifest.
The bootstrap additionally checks the watchdog, bootstrap, dispatcher, and
dashboard files for changes
before importing application code. Delivery must establish the reviewed
bootstrap/watchdog contents and hashes, including the deployed entry point.
Deploying a modified development checkout as a supposedly clean pin is not
permitted.

The existing pinned launcher still reaches the shared supervisor gate when
it starts a stopped worker. An already-running launcher check is observation,
and cannot clear the maintenance record or prove that the runtime loaded the
upgraded gate.

## Dashboard routing

Launch the delivered dashboard with `KIWOOM_RUNTIME_ROOT` already bound to the
runtime directory and both US source pin variables explicitly configured. These
bindings precede application imports; setting them in runtime dotenv is not a
deployment route. The dashboard uses that runtime for configuration and files.
Its US start/status/stop commands invoke the pinned bootstrap from the source
checkout, with runtime cwd and bounded timeouts. Invalid/missing US pins refuse
before subprocess execution. A timeout has an unresolved worker outcome and is
not retried. KR supervisor command selection and its existing timeout remain
unchanged. An unrouted dashboard retains its legacy dotenv state configuration.

## Shared watchdog dispatcher

Run `tools/mock_worker_watchdog_dispatcher.py` from the reviewed source with
explicit `--kr-root`, `--kr-python`, `--runtime-root`, `--source-root`, and
`--revision`. The dispatcher and US bootstrap must come from that same clean pin.
The dispatcher verifies its source before launching either child; integrity
failure refuses all work and requires operator attention.

The KR child uses the legacy interpreter, cwd, and unchanged inherited environment,
checks watchdog/supervisor import origins, and invokes only
`check_and_restart("kr_mock", "KR")`. It never runs the legacy all-account main
loop. The US child invokes the pinned bootstrap's `watchdog` action with runtime
cwd. A US child refusal or timeout does not skip or repeat the KR sweep. No source
or runtime bindings are installed in the parent process. Child stdout/stderr is
not copied into the dispatcher report. Each child has a bounded 120-second wait;
a timeout may leave a detached worker and is explicitly unresolved. No worker is
killed or relaunched as a timeout remedy.

A successful dispatcher result establishes only completion of both sweep commands,
not worker health. Actual task principals, configuration loading, source hashes,
and KR behavior require separately authorized deployment evidence. Existing
duplicate-process diagnostics still rely on observable supervisor-parent command
signatures; do not infer absence from an empty inventory after a launcher exits.

## Deployment prerequisites and limits

Legacy supervisors and watchdog copies do not honor the new persistent
record. Before claiming suppression, identify every actual US Scheduler,
watchdog, dashboard, Telegram, and CLI launch authority and upgrade its
supervisor import path. Changing the US worker Scheduler action alone does
not change a watchdog's direct `supervisor.start` call.

Install an initial PAUSED record as part of an explicitly approved deployment.
Control any transition interval in which legacy launchers remain active using
a separately approved US-scoped measure; do not disable shared KR monitoring.
Confirm named-mutex access under the actual Windows task principals. An
access error is unresolved, not permission to change ACLs.

Worker graceful stop, baseline broker query, fresh-ledger promotion, resume,
worker start, Scheduler changes, Git delivery, and CI verification remain
separate operations. The legacy 15-minute intentional-stop marker is retained
for its existing shutdown protocol; it is not persistent maintenance state.

Validation before delivery must cover malformed/missing/reparsed records, CAS generation
mismatch, cross-process pause/start ordering, Windows lock contention and
abandonment, missing/dirty/mismatched US pins, dotenv substitution, runtime
path ownership, all supported launch entry points, and preserved KR behavior.
