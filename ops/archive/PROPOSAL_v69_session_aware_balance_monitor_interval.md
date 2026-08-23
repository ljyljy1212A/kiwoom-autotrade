# Proposal v69 — Session-Aware Balance-Monitor Interval

## Scope and decision boundary

This is a design proposal only. It does not change code, configuration,
workers, WebSocket connections, firewall/WFP settings, credentials, staging,
or commits.

The proposal applies only to the mock account workers `kr_mock` and
`us_mock`. `kr_real` and `us_real` are unaffected and remain out of scope.

## Proposed behavior

Use a 60-second balance-monitor interval while the market session is
`CLOSED`. Keep the existing 5-second interval whenever the session is not
`CLOSED`.

The closed-session arithmetic, based on the v68-confirmed 5-second baseline,
is:

```text
Existing overnight period: approximately 17.5 hours
Existing cadence: 17.5 * 3,600 / 5 = approximately 12,600 iterations
Proposed cadence: 17.5 * 3,600 / 60 = approximately 1,050 iterations
Reduction: approximately 91.7%
```

The 60-second value is a bounded proposal: it substantially reduces repeated
closed-session balance polling while retaining periodic reconciliation. It is
not a claim about broker quota sufficiency or an operator judgment that the
traffic is acceptable.

When the session is no longer `CLOSED`, the effective interval must be 5
seconds again. While the session is `CLOSED`, compute the next regular-session
opening boundary and sleep for:

```text
min(60 seconds, seconds_until_next_regular_open)
```

The `seconds_until_next_regular_open` value comes from the existing
`MarketCalendar` schedule data and timezone when the calendar is available.
When the calendar is unavailable, use the existing fallback market hours.
If the next opening is more than 60 seconds away, sleep for 60 seconds and
recompute on the next iteration. The final closed-session sleep is capped by
the remaining time until the opening boundary, so the next iteration observes
the non-`CLOSED` session and uses the 5-second interval. `session_name_now()`
itself is not modified; this proposal only adds a caller/helper using the
existing schedule data.

The arithmetic remains approximately 1,050 iterations per 17.5-hour closed
period:

```text
17.5 hours * 3,600 / 60 seconds = approximately 1,050 iterations
```

This is unchanged from the original estimate because the sleep is capped at
60 seconds on every iteration except the final one before each boundary; it
does not degrade into 5-second polling throughout the night.

The proposal applies equally to KR and US mock workers. It does not change
the real-account workers.

## Exact change location

The change would be localized to the balance-only loop in
`src/main.py`, currently structured as:

```python
monitor = AccountEngine(ctx, telegram, discord, None, None, balance_only=True)
while True:
    monitor._refresh_runtime_control()
    await monitor.sync_broker_state(force_balance=True)
    await asyncio.sleep(monitor.poll_interval_sec)
```

The proposed logic would obtain the current session from the existing
market-calendar accessor, `monitor.calendar.session_name_now()`, after the
balance synchronization. If the result is `CLOSED`, it would calculate
`seconds_until_next_regular_open` from the available `MarketCalendar`
schedule/timezone data, or from the existing fallback market hours, and use
`min(60, seconds_until_next_regular_open)` for the next wait. Otherwise, the
wait would remain 5 seconds. No new session-detection mechanism is needed,
and `session_name_now()` itself is not modified.

## Explicit non-scope and risk boundaries

### WebSocket feed

The proposal does not disconnect, reconnect, unsubscribe, or otherwise alter
the WebSocket quote feed. A daily disconnect/reconnect cycle could recreate
the known Issue 3 fixed-port `TIME_WAIT` collision on a predictable schedule.
Leaving the feed untouched avoids reintroducing that failure mode.

### Main trading loop

The proposal changes only the balance-only monitor loop. It does not alter the
per-tick `sync_broker_state()` call in the main symbol-engine loop, the
`CLOSED` hard-return gate in `AccountEngine._tick()`, quote evaluation, signal
generation, or order submission.

### Pending-order recovery

The proposal does not alter pending-order recovery, execution-history queries,
stale-order cancellation, or their existing request throttles. Those paths
remain governed by the current symbol-engine synchronization behavior.

## Rollback and verification plan

If separately authorized, the implementation should remain a small,
localized conditional and sleep-duration change in the balance-only loop. A
rollback would be a trivial revert of that conditional, restoring the
existing `await asyncio.sleep(monitor.poll_interval_sec)` behavior and the
5-second default.

Live verification after an authorized implementation would check:

1. A post-close log window shows balance-monitor iteration timestamps about
   60 seconds apart rather than about 5 seconds apart.
2. The log records the selected closed-session interval, if such a diagnostic
   line is added as part of the authorized implementation; no unrelated
   WebSocket reconnects should be introduced.
3. Around the next regular-session opening, the monitor resumes approximately
   5-second iterations without waiting for a full 60-second closed interval.
4. The existing balance-reconciliation success, rate-limit, and deferral
   diagnostics remain intact.
5. No `kr_real` or `us_real` process or control state is touched.

This verification plan is not being executed in v69.

## Review decision requested

The proposal is now fully specified for a single authorize, reject, or revise
decision before any implementation work is considered.
