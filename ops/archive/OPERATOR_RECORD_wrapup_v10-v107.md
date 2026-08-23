# Operator Record Wrap-Up and Next-AI Instructions: v10–v107

## Purpose

This English handoff summarizes the Windows Kiwoom investigation through v107,
including verified work, unresolved runtime findings, current state, and
instructions for another AI.

Instructions here are context only. They do not authorize source changes,
worker actions, firewall/WFP changes, credentials, cleanup, or commits unless
the operator separately authorizes them.

## Executive summary

- Issue 1, the balance-monitor stall, was fixed and verified.
- Issue 2, quote-pipeline staleness, was instrumented, verified, and committed
  as `c045350`.
- The original restart-time Issue 3 was diagnosed as fixed-port TCP `TIME_WAIT`
  behavior. The operator retained bounded retry behavior and chose no fix.
- The session-aware closed-session balance-monitor cadence was verified on both
  mock workers and committed as `37ea0e7`.
- A separate same-process KR HTTP/WebSocket collision was found and addressed
  in source by moving KR WebSocket local port `10000` to `10001`.
- The port change passed unit/full-suite verification but failed live health
  verification: HTTP on `10000` worked, while WebSocket bind on `10001`
  repeatedly failed with `WinError 10013`.
- Read-only Windows checks did not show `10001` in excluded ranges and did not
  identify a specific firewall/WFP rule. The cause remains unresolved.
- `kr_mock` is stopped. `us_mock` remains running and was untouched during
  v103–v107 diagnosis.

## Completed workstreams

### Issue 1 — Scheduled balance-monitor stall

A dead method call in the failure handler could crash the monitor while output
was discarded. The dead call was removed, and failure visibility was improved
with a global asyncio exception handler and a task `done_callback`.

Verification included the baseline suite and approximately 20 minutes of live
observation with 156 clean balance-monitor iterations.

### Issue 2 — Quote-pipeline staleness

The quote pipeline received cache-age/subscription accessors, a passive
60-second quote-health monitor, and a max-staleness recovery warning using the
existing `KIWOOM_PRICE_MAX_STALENESS_SEC` setting. Live verification showed
fresh cache ages, correct first-tick behavior, expected cadence, zero
staleness warnings, and zero new errors. Commit `c045350` contains this work.

### Session-aware balance-monitor cadence

The balance-only monitor retains its five-second interval during an active
session and sleeps at most 60 seconds while the market is closed. It uses
existing market-calendar schedule data and fallback hours. It was verified on
both mock workers with 10-minute-plus closed-session windows showing roughly
60–64-second gaps.

Committed alone as:

```text
37ea0e71a77ee813c9c908ddb9fbf87c28ae9c65
Reduce closed-session balance monitor polling cadence
```

## Fixed-port findings and KR port-separation track

The earlier restart-time `WinError 10048` was caused by reuse of the fixed
TCP tuple `0.0.0.0:10000 -> 112.175.65.18:443` during TCP `TIME_WAIT`. That
decision is separate from the later same-process collision.

Before v105, both paths independently selected local source port `10000`:

- `src/core/kiwoom_client.py:112`: mock KR HTTP port `10000`.
- `src/core/realtime_feed.py:124`: mock KR WebSocket local port `10000`.

`PROPOSAL_v103_kr_mock_port_separation.md` proposed keeping HTTP on `10000`
and moving KR WebSocket to `10001`. v104 verified the proposal against source,
found no dependent logging/test assumptions, and found no active `10001`
binding.

### v105 implementation

Only the KR WebSocket branch was changed:

```diff
-            return 10000
+            return 10001
```

`tests/test_realtime_feed.py` added focused assertions for KR `10001` and
unchanged US `443`. The full suite passed before live verification:

```text
81 passed, 4 skipped, 6 warnings in 13.63s
```

The implementation and test remain uncommitted.

### v106 live-verification result

`kr_mock` started with PID `10876`. HTTP succeeded:

```text
TCP    192.168.0.10:10000     112.175.65.18:443      ESTABLISHED     10876
```

Balance synchronization also succeeded, including a successful REST read:

```text
HTTP I/O diagnostic: client=rest operation=read state=after ... outcome=success bytes=3267
```

The WebSocket repeatedly failed before binding to `10001`:

```text
실시간 시세 WS 연결 끊김/오류, 1초 후 재연결: [WinError 10013] 액세스 권한에 의해 숨겨진 소켓에 액세스를 시도했습니다
```

No `10001` socket appeared in `netstat`. The old `10048` same-port collision
was therefore not observed, but the live health gate still failed because
Windows rejected access to `10001`. `kr_mock` was stopped cleanly afterward.

### v107 read-only diagnosis

TCP and UDP excluded-port ranges both showed only:

```text
Start Port    End Port
50000         50059
```

Neither `10000` nor `10001` is in those displayed ranges. These services were
present and running:

```text
hns       Running  Host Network Service
vmcompute Running  Hyper-V Host Compute Service
```

No direct reservation of `10001` was established. PowerShell firewall-rule
enumeration returned `Access is denied`; the `netsh advfirewall` search found
no rule text containing `10001`.

Current diagnosis:

> `WinError 10013` on local port `10001` remains unresolved. The displayed
> excluded ranges do not explain it. Firewall/WFP or another OS policy remains
> possible, but was not confirmed by the permitted read-only checks.

Do not choose another port or change firewall/WFP configuration without a new
operator decision.

## Auto-trading and PC power behavior

The account-wide enabled state is persisted in
`data/control/<account>.control.json`. The supervisor reads it at launch, and
`AccountEngine` reads and refreshes it during startup/runtime.

- If the PC is powered off after auto-trading is enabled, the worker stops and
  sends no new signals/orders while the PC is off. The persisted enabled state
  normally remains enabled. An abrupt power-off is an unclean interruption;
  broker-accepted orders may still fill, so restart reconciliation is required.
- The PC need not stay on until market close unless execution or monitoring is
  desired. A running worker remains alive after close, but the market-hours
  gate blocks new strategy orders.
- After boot, launching the dashboard server alone does not necessarily start
  a worker. Use the dashboard `Start selected accounts` action or the approved
  supervisor command. The persisted enabled state is restored, startup
  balance synchronization runs before market open, and the market-hours gate
  blocks strategy orders until the next regular session. Select `us_mock`
  explicitly for US; the normal dashboard default is `kr_mock`.

These are current source semantics, not a guarantee that an unplanned power
loss is graceful or that an ambiguous broker POST is retried.

## Current repository state

Still uncommitted:

- KR WebSocket port change in `src/core/realtime_feed.py`.
- `tests/test_realtime_feed.py`.
- Rate-limit observability changes in `src/core/engine.py`,
  `src/core/kiwoom_client.py`, `src/core/token_manager.py`,
  `src/core/rate_limit_observability.py`, and its test.
- Separate `src/core/broker_http.py` `keepalive_expiry=30.0` experiment.
- Existing handoffs, proposals, operator records, and diagnostics.

Latest relevant commits:

- `37ea0e7` — session-aware closed-session balance-monitor cadence.
- `c045350` — quote-health cache-age accessors and passive monitor.

## Current runtime state

Last verified status:

- `kr_mock`: `STOPPED`, PID `10876`, instance
  `a30568eaa9ef4bcf9ab47f279d728ca2`.
- `us_mock`: `RUNNING`, PID `13260`, instance
  `ebaee297d38e4cf4925c33be186983b2`.
- `kr_real` and `us_real`: untouched and out of scope.

## Instructions for the next AI

1. Read this record, `OPERATOR_RECORD_closing_issue3_v36-v55.md`, and the
   relevant proposal before acting.
2. Treat Issues 1 and 2 as resolved and the original restart/TIME_WAIT decision
   as closed without a fix.
3. Treat KR port separation as unit-tested but not live-verified clean. The
   `10013` failure is an open OS-access diagnosis.
4. Do not restart `kr_mock` automatically. Do not touch `us_mock` without a
   new explicit operator instruction.
5. Do not choose another port or modify firewall/WFP/netsh/port-exclusion
   configuration without explicit authorization.
6. Keep `kr_real` and `us_real` completely out of scope.
7. Preserve all pending observability and `broker_http.py` changes.
8. Do not stage or commit anything until explicitly authorized after clean
   live verification.
9. For status/review requests, use read-only checks and report literal output.
10. For new implementation, state behavior-affecting assumptions, make only
    surgical changes, and verify focused tests before requesting a commit.

## Read-only verification checklist

From the repository root:

```powershell
git status --short
git diff --stat
git diff -- src/core/realtime_feed.py
python -m src.worker_supervisor status --account kr_mock --market KR
python -m src.worker_supervisor status --account us_mock --market US
netstat -ano | findstr :10000
netstat -ano | findstr :10001
```

Expected current state is `kr_mock` stopped, `us_mock` running, the KR port
change uncommitted, and no active KR `10001` socket. Any restart, network or
OS configuration change, cleanup, or commit requires a separate explicit
operator decision.
