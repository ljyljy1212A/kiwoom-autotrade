# Handoff v30: Quote-Health Instrumentation Wrap-Up

## Purpose

This document is the operator record and continuation guide for the next AI.
The quote-health instrumentation authorized in v29 has been implemented and
verified in the working tree.

## Current repository state

Repository:

`C:\auto\작업7차\kiwoom-autotrade`

Implementation files changed:

- `src/core/realtime_feed.py`
- `src/main.py`

The changes are currently uncommitted. Preserve unrelated working-tree files
and changes. Existing untracked handoff files are unrelated and were not
modified:

- `HANDOFF_ACCOUNT_LOCK_BYPASS_NETWORK_DIAGNOSTICS.md`
- `HANDOFF_NEXT_AI_HTTP_WS_PORT_COLLISION.md`

## Progress completed

### Public WebSocket cache-health accessors

`KiwoomRealtimeFeed` now provides:

- `subscribed_symbols()` — returns active and pending subscription symbols.
- `cache_age_sec(symbol)` — returns the age of the latest cached tick, or
  `None` when no tick has been received.

The periodic monitor uses these public methods and does not reach into the
feed's private cache or subscription fields.

### Recovery-gap warning

`KiwoomRealtimeFeed.__init__` now accepts:

```python
max_staleness_sec: float = 20.0
```

When a newly received tick follows a gap greater than this threshold, the feed
logs a warning containing the symbol, gap duration, and threshold. It does not
log every tick.

The single `PriceFeed` construction path explicitly passes the configured
`max_staleness_sec`, ensuring custom `KIWOOM_PRICE_MAX_STALENESS_SEC` values
apply consistently. Existing direct callers that omit the new argument retain
the 20-second default.

### Passive periodic monitor

`src/main.py` now starts a passive monitor alongside `make_price_feed()` in
`run_symbol_engines()`.

Task name:

```text
{account_id}-quote-health-monitor
```

It reports each subscribed symbol's cache age every 60 seconds. The task is
cancelled and awaited in the existing `run_symbol_engines()` cleanup path.
It does not place orders, alter trading controls, or replace REST authority.

## Verification evidence

Requested test command:

```powershell
$env:PYTHONPATH='C:\auto\작업7차\kiwoom-autotrade'
pytest -q -p no:cacheprovider
```

Observed result:

```text
74 passed, 4 skipped, 5 warnings in 17.21s
```

Additional checks passed:

- No-write compilation of `src/core/realtime_feed.py` and `src/main.py`.
- Default constructor smoke check confirmed `max_staleness_sec == 20.0`.
- Missing cache smoke check confirmed `cache_age_sec()` returns `None`.
- `git diff --check` passed.
- The only `KiwoomRealtimeFeed(...)` construction site remains the shared
  `PriceFeed` path in `src/core/realtime_feed.py`.

The normal `py_compile` command attempted to write into an existing protected
`__pycache__` location and reported permission denied; the no-write compile
check passed and the source itself was not changed by that failure.

## Operator boundaries

- No worker was restarted or stopped.
- No process was touched, including `kr_mock` PID `6436` and `us_mock` PID
  `10172`.
- Do not touch `kr_real`, `us_real`, live credentials, broker orders, or
  firewall/WFP configuration.
- Do not remove or alter the existing v21 asyncio/task instrumentation or the
  v24 balance-monitor fix.
- Do not add unrelated refactors or operational changes.

## Next AI instructions

Treat the implementation and test evidence above as completed. First inspect
the working-tree diff and preserve all pre-existing edits. Do not reimplement
the instrumentation.

The next operational step is intentionally pending authorization: a separate
operator decision is required before restarting any worker so the new code can
be loaded. Until that authorization is given, perform read-only review only.

If restart authorization is later provided, follow the repository's Windows
worker procedures, verify process identity and fresh health evidence after the
restart, and report the exact process/task evidence. Do not infer restart
authorization from this document.

## Useful review commands

```powershell
Set-Location 'C:\auto\작업7차\kiwoom-autotrade'
git status --short
git diff -- src/core/realtime_feed.py src/main.py
rg -n -S "cache_age_sec|subscribed_symbols|max_staleness_sec|quote-health-monitor" src
```

