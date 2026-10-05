# US mock observation startup

The routed `us_mock` / `US` / `mock` worker can attach the PR #101 coordinator
to its existing per-symbol REST observation hook. Activation is disabled by
default. Startup validates the dedicated database, external checkpoint and
cooperative lock before acquiring the worker lock or starting Telegram.

## Explicit launch configuration

| Variable | Required value when active |
| --- | --- |
| `US_MOCK_OBSERVATION_ENABLED` | Exact `true`; unset or exact `false` disables the feature |
| `US_MOCK_OBSERVATION_DB_PATH` | Absolute path to a separately prepared dedicated observation database |
| `US_MOCK_OBSERVATION_CHECKPOINT_PATH` | Absolute path to its separately prepared canonical checkpoint |
| `US_MOCK_OBSERVATION_JOURNAL_ID` | Independently pinned lowercase UUID hex, 32 characters |
| `US_MOCK_OBSERVATION_BINDING_ID` | Independently pinned lowercase UUID hex, 32 characters |

The existing routed source root/revision and runtime contract still apply.
These five values are captured from the launch environment. Runtime dotenv
loading cannot add an unset activation flag or change any supplied pin.
Unrouted launches cannot activate this feature. No credentials are stored in
the observation configuration.

## Preparation and recovery

The database must already match `SCHEMA`, `SCHEMA_VERSION`, `POLICY` and
`CONTRACT` in `us_operational_observation_store.py`, with one metadata row
matching the independent journal and binding pins. Startup uses SQLite
`mode=rw`; it never creates a missing database or migrates an existing one.

The checkpoint must use `checkpoint_bytes()` and its fixed `.lock` companion
must contain `LOCK_CONTENT`. Paths must name distinct existing regular files,
with no hard links or reparse points. Startup compares database replay with
the external head. Missing, mismatched, corrupt or conflicted evidence refuses
startup without repair, retry or automatic head adoption. The worker owns one
connection until its engine tasks have stopped, then closes it.

Per-symbol engines also need the already prepared version 2 identity ledger
and confirmed order/date identities required by the existing observation hook.
No ledger migration or identity authentication is supplied by this wiring.
No tracked orders means no validated cycle; it does not prove finality.

## Observation boundary

An active operational coordinator persists the complete existing REST cycle
and its checkpoint. After a verified receipt, the engine records
`OBSERVED_ONLY`, retains the synchronization blocker, and returns before
legacy economic normalization, fill application, cancellation or balance
reconciliation. New Engine order dispatch remains blocked even after a
successful receipt or attempted backend replacement. Other explicitly
injected synthetic test sinks retain their existing behavior.

This mode collects evidence; it does not allocate execution dates, authorize
economic ingestion, or enable trading. F5 receipt/persistence is a separate
evidence path. File preparation, source deployment and worker activation
require their own concrete operational scope. This change does not prepare
production files, activate launch settings, change Scheduler tasks or restart
a worker.
