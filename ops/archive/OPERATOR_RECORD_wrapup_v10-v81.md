# Operator Record Wrap-Up and Next-AI Instructions: v10–v81

## Purpose

This document summarizes the Windows Kiwoom mock-worker investigation and
provides safe operating instructions for the next AI. The instructions in
this document are context only. They do not authorize runtime actions,
source changes, network-policy changes, credential access, or commits unless
the operator explicitly authorizes those actions.

## Current outcome

The investigation has reached a stable implemented state:

- Issue 1, the scheduled balance-monitor stall, was fixed and verified.
- Issue 2, quote-pipeline staleness, was instrumented, verified, and committed
  as `c045350`.
- Issue 3, restart-time `WinError 10048`, was diagnosed as fixed-port TCP
  `TIME_WAIT` behavior. The operator chose to retain the existing bounded
  retry behavior rather than add a startup gate, port arbiter, socket change,
  or firewall change.
- A separate session-aware balance-monitor cadence change was implemented,
  live-verified on both mock workers, and committed as `37ea0e7`.

The post-close broker-traffic investigation is therefore implemented and
committed. Other repository decisions remain separate and open.

## Progress by workstream

### Issue 1 — Balance-monitor stall

The failure handler contained a dead method call. When a REST request failed,
the handler could itself crash, while stdout/stderr were discarded and no
logging handler was configured. The dead call was removed. Additional failure
visibility was added through:

- a global asyncio exception handler; and
- a task `done_callback`.

Verification included the baseline test suite and approximately 20 minutes of
live observation with 156 clean balance-monitor iterations.

### Issue 2 — Quote-pipeline staleness

The quote pipeline received:

- public `cache_age_sec(symbol)` and `subscribed_symbols()` accessors;
- a passive 60-second quote-health monitor; and
- a `max_staleness_sec` recovery-gap warning using the existing
  `KIWOOM_PRICE_MAX_STALENESS_SEC` setting.

Live verification covered approximately 23 minutes, with correct first-tick
semantics, 24 expected quote-health lines, fresh cache ages, zero staleness
warnings, and zero new errors. Commit `c045350` contains this implementation.

### Issue 3 — Fixed-port `WinError 10048`

The confirmed mechanism is reuse of the same fixed TCP tuple after restart:

```text
local source:  0.0.0.0:10000
remote target: 112.175.65.18:443
```

The predecessor connection remains in `TIME_WAIT`, so the replacement
connection can fail with `WinError 10048` / `WSAEADDRINUSE`. The failure was
verified at the connect phase, not bind phase. `SO_REUSEADDR` is already
present in both the HTTP and WebSocket paths, and a shared port arbiter cannot
remove the kernel's `TIME_WAIT` restriction.

The selected operational decision is to keep the existing exponential retry
sequence (`1s, 2s, 4s, 8s, 16s, 30s`). It self-resolves within the observed
restart window without a worker crash or silent failure. Do not implement a
startup gate, port arbiter, socket option change, or firewall change unless
the operator reopens this decision.

### Session-aware balance-monitor cadence

The balance-only monitor now:

- retains the original `5`-second interval while the market session is open;
- sleeps at most `60` seconds while the session is `CLOSED`;
- uses existing `MarketCalendar` schedule data to calculate the next regular
  open; and
- uses the existing `_FALLBACK_HOURS` data if the calendar backend is absent.

The change is in `src/main.py` and was committed alone as:

```text
37ea0e71a77ee813c9c908ddb9fbf87c28ae9c65
Reduce closed-session balance monitor polling cadence
```

The commit touches exactly `src/main.py` with 37 insertions and 2 deletions.
It does not modify the WebSocket feed, trading loop, real accounts, or broker
network policy.

## Runtime verification

### `kr_mock`

- Restarted only during the authorized v77 verification round.
- Current PID: `15672`.
- Current instance: `aa3fc420eedd47579ee95acd2af353af`.
- Current state: `RUNNING`.
- Closed-session observation showed approximately 60–64-second monitor gaps
  over a 10-minute-plus window.

### `us_mock`

- Restarted only during the authorized v79 verification round.
- Current PID: `13260`.
- Current instance: `ebaee297d38e4cf4925c33be186983b2`.
- Current state: `RUNNING`.
- Recovery started at approximately `17:05:54 KST`.
- First successful post-restart REST I/O occurred at `17:08:01 KST`, a gap of
  approximately `127.174064` seconds.
- The recovery window contained repeated `WinError 10048` messages.
- The subsequent full 10-minute-plus closed-session window showed iteration
  gaps of 60–63 seconds.

The pre-success monitor iterations are expected: the loop logs an iteration,
attempts `sync_broker_state(force_balance=True)`, catches failures, and then
executes `_balance_monitor_sleep_seconds(monitor)`. Therefore a failed
iteration does not imply that broker synchronization succeeded.

## Current repository state

The current tracked working-tree modification is intentionally separate:

```diff
-            keepalive_expiry=5.0,
+            keepalive_expiry=30.0,
```

This `src/core/broker_http.py` change remains uncommitted. It is a bounded
experiment and is not part of the session-aware monitor commit. Do not stage,
commit, revert, or otherwise alter it without a separate operator decision.

Known preserved untracked documentation and diagnostic content includes the
older operator records, handoffs, the v69 proposal, and `diagnostics/`.
Historical records must be preserved; do not perform broad cleanup.

## Instructions for the next AI

1. Read this document and the older Issue 3 closing record before using any
   historical handoff as current guidance.
2. Treat Issues 1 and 2 as resolved. Treat Issue 3 as diagnosed and closed
   without a new fix.
3. Treat `HANDOFF_NEXT_AI_HTTP_WS_PORT_COLLISION.md` as superseded wherever it
   proposes an active HTTP/WebSocket collision or a shared port arbiter.
4. Do not restart, stop, or manually launch either worker automatically.
5. Keep `kr_real` and `us_real` completely out of scope.
6. Do not touch firewall/WFP configuration, credentials, or live-account state.
7. Preserve the uncommitted `keepalive_expiry=30.0` change unless the operator
   gives a separate disposition decision.
8. Do not stage or commit other files. The session-aware monitor change is
   already committed as `37ea0e7`.
9. For a status or review request, use read-only checks and report exact
   evidence. Do not infer authorization for implementation or runtime work.
10. If new implementation is requested, state the behavior-affecting
    assumptions, make only surgical changes, and verify them before proposing
    a commit.

## Read-only verification checklist

Run from the repository root:

```powershell
git status --short
git show --stat --oneline 37ea0e7
git diff -- src/core/broker_http.py
python -m src.worker_supervisor status --account kr_mock --market KR
python -m src.worker_supervisor status --account us_mock --market US
```

Expected results:

- `37ea0e7` contains only `src/main.py`.
- `src/core/broker_http.py` still shows only the separate
  `keepalive_expiry=30.0` modification.
- Both mock workers remain account-scoped and `RUNNING` with the current PIDs
  recorded above, unless an operator has explicitly changed runtime state.

The next AI must stop and request an explicit operator decision before any
restart, source change, network-policy change, credential action, cleanup, or
additional commit.
