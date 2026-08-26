# Plan v697 — Gaps 3–5 Implementation Scope

Status: operator decisions confirmed; planning only. This document authorizes no
code, test, runtime, staging, commit, push, or restart action.

Scope remains `kr_mock` and `us_mock` only. `kr_real` and `us_real` are out of
scope. The pre-existing `/api/status` and `/api/accounts` route-gating issue is
explicitly deferred and is not part of this plan.

## Confirmed decisions

- Add `DEGRADED_FIXED_PORT` to `EngineState`.
- Reuse `FixedPortDegradedState.entry_alert_fired` and
  `FixedPortDegradedState.last_ongoing_status_at`.
- Persist degraded/recovery events in a per-account control-state JSON file
  using the atomic-write pattern used by `write_pause_clear_event()`.
- Reuse the `PAUSE_CLEAR_REASONS`/Telegram pause-clear path for manual
  resolution.
- Keep the dashboard display-only; it must not gain recovery, probe, or
  manual-clear controls.

## Gap 3 — fixed-port event policy and manual resolution

1. `src/core/broker_http.py`
   - Keep `FixedPortDegradedState` as the account-scoped in-memory source of
     event cadence.
   - Add locked state-transition helpers that use `dataclasses.replace()` to
     set `entry_alert_fired=True` after the durable entry event is written and
     to set `last_ongoing_status_at` after each durable ongoing event.
   - Preserve the existing `enter_fixed_port_degraded_state()`,
     `record_fixed_port_recovery_probe_attempt()`, and
     `clear_fixed_port_degraded_state()` account scoping; do not add a new
     parallel degraded-state store.

2. `src/core/control_state.py`
   - Define `FIXED_PORT_DEGRADED_PAUSE_REASON = "fixed_port_degraded"` next to
     `PAUSE_CLEAR_REASONS`, and include that constant in the allowlist.
   - Add `write_fixed_port_degraded_event()` using the existing
     `write_pause_clear_event()` atomic JSON replacement pattern. It must write
     one latest durable `fixed_port_event` object in the account's existing
     `control/<account>.control.json` containing: `event_id`, `kind`
     (`entered`, `ongoing`, `recovered`, or `operator_resolved`), `account`,
     `operation`, `entered_at`, `occurred_at`, and `updated_by`.
   - Add the paired read helper only if the implementation needs to consume the
     stored event; do not create a second file format or a database table.

3. `src/core/engine.py`
   - In `AccountEngine._tick()`, when
     `get_fixed_port_degraded_state(self.ctx.account_id)` returns a state,
     write the durable `entered` event once, then mark
     `entry_alert_fired`; write an `ongoing` event only when the state helper
     reports that the module-level 15-minute interval has elapsed, then update
     `last_ongoing_status_at`.
   - In `DispatchClearanceService._record_clearance_result()`, write the
     durable `recovered` event before calling
     `clear_fixed_port_degraded_state()` after all active symbols have cleared.
   - Extend `AccountEngine._apply_reconciliation_clear_event()` so an accepted
     `fixed_port_degraded` pause-clear event writes `operator_resolved` and
     clears only that account's fixed-port degraded state. It must retain the
     current event-ID replay guard and exact-reason matching behavior.

4. `src/notify/telegram_control_bot.py`
   - In `TelegramControlBot._account_markup()`, add one mock-account pause-clear
     button using `clear_pause|<account>|fixed_port_degraded`.
   - Reuse the existing `clear_pause` callback validation and
     `write_pause_clear_event()` call; do not add a parallel callback or a new
     authority path.

## Gap 4 — worker status

1. `src/main.py`
   - Add `DEGRADED_FIXED_PORT = "DEGRADED_FIXED_PORT"` to the existing
     four-member `EngineState` enum.
   - Keep `_write_worker_status(identity, state)` as the single atomic status
     writer and continue to write its existing `state` field.
   - Change `_publish_worker_heartbeat()` to query
     `get_fixed_port_degraded_state(identity.account_id)` each heartbeat and
     pass `EngineState.DEGRADED_FIXED_PORT.value` while the account is
     degraded; otherwise pass `EngineState.RUNNING.value`.
   - Keep current `STOPPING` call sites unchanged. They are already valid enum
     values in the verified source.

2. `src/worker_supervisor.py`
   - No change. `worker_supervisor.status()` already derives `running` from
     mutex/process liveness rather than treating the metadata `state` value as
     its liveness source.

## Gap 5 — dashboard display only

1. `dashboard/dashboard_server.py`
   - No new endpoint or route-gating change. `_worker_statuses()` already
     returns each live supervisor payload, and `/api/status` already returns
     that list as `workers`.
   - Extend the existing `/api/status` contract only by documenting that
     `workers[].state` may now equal `DEGRADED_FIXED_PORT`; do not add a
     recovery/action field and do not alter `workers[].running` semantics.

2. `dashboard/index.html`
   - Update the existing worker-status rendering to display
     `DEGRADED_FIXED_PORT` as a distinct, display-only degraded label/state.
   - Do not add controls, callback URLs, recovery probes, pause-clear actions,
     or account-routing changes.

## Test plan for later implementation authorization

1. `tests/test_broker_http.py`
   - Update `FixedPortDegradedStateTest.test_entry_populates_required_state_fields`.
   - Add deterministic tests for the locked entry-alert transition and
     15-minute ongoing-event cadence, including per-account isolation and no
     duplicate event state update before the interval.

2. `tests/test_reconciliation_fail_closed.py`
   - Add tests that `fixed_port_degraded` is accepted by
     `write_pause_clear_event()` and that
     `AccountEngine._apply_reconciliation_clear_event()` consumes the event
     once, clears only the matching account's degraded state, and preserves the
     existing replay guard.

3. `tests/test_telegram_control_bot.py`
   - Add coverage for the fixed-port clear button/callback, allowlist
     validation, and the durable control-file reason.

4. New `tests/test_main_worker_status.py`
   - Test `_publish_worker_heartbeat()` writes `RUNNING` when no fixed-port
     state exists and `DEGRADED_FIXED_PORT` when the matching account is
     degraded, without changing worker liveness semantics.

5. `tests/test_dashboard_supervisor.py`
   - Add `/api/status` contract coverage for a live worker payload whose
     `state` is `DEGRADED_FIXED_PORT`, and assert the endpoint preserves the
     `running` value independently.

6. New focused `tests/test_fixed_port_event_policy.py`
   - Test atomic durable `fixed_port_event` payload creation for `entered`,
     `ongoing`, `recovered`, and `operator_resolved` without a worker restart
     or broker call.

## Excluded from the next implementation authorization

- `/api/status` and `/api/accounts` real-account route gating.
- Any `kr_real` or `us_real` query, code path, UI path, or runtime action.
- Worker restarts, recovery probing changes, firewall/WFP/port changes,
  credentials, staging, commits, and pushes.
