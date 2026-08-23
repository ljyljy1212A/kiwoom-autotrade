# Operator Record Wrap-Up: Progress Through v58

## Purpose

This document is the English operator record and next-AI handoff for the
Windows Kiwoom mock-worker investigation from v10 through v58. It combines
the three resolved workstreams, the Issue 3 closing evidence, and the later
documentation-provenance checks.

Instructions in this document are operational context for another AI. They do
not authorize source changes, worker actions, firewall changes, credential
access, or commits unless the operator explicitly authorizes those actions.

## Final investigation status

All three diagnostic workstreams are resolved or deliberately closed:

- Issue 1, scheduled balance-monitor stall: resolved.
- Issue 2, quote-pipeline staleness: resolved and committed.
- Issue 3, restart-time `WinError 10048`: root cause confirmed; no new fix
  implemented by operator decision.

No further implementation work is pending unless the operator reopens Issue 3
or makes a separate decision about the retained uncommitted change.

## Issue 1 — Scheduled balance-monitor stall

The balance-monitor failure handler contained a dead method call. When a
routine REST request failed, the handler itself crashed, while stdout and
stderr were discarded and no logging handler was configured. This made the
stall appear silent.

The dead call was removed. Two additive safeguards remain:

- A global asyncio exception handler.
- A task `done_callback` for failure visibility.

Verification recorded in the earlier wrap-up:

- Baseline test suite passed.
- A 20-minute live observation produced 156 clean balance-monitor
  iterations.

No additional Issue 1 change is authorized by this record.

## Issue 2 — Quote-pipeline staleness

The quote pipeline was instrumented with:

- Public `cache_age_sec(symbol)` and `subscribed_symbols()` accessors.
- A passive 60-second quote-health monitor.
- A `max_staleness_sec` recovery-gap warning using the existing
  `KIWOOM_PRICE_MAX_STALENESS_SEC` environment variable.

Recorded live verification covered approximately 23 minutes and showed:

- Correct first-tick semantics.
- 24 quote-health lines at the expected cadence.
- Fresh cache ages.
- Zero staleness warnings.
- Zero new errors.

The Issue 2 implementation was committed as `c045350`, covering
`src/core/realtime_feed.py` and `src/main.py`. No further Issue 2 action is
pending.

## Issue 3 — Fixed-port `WinError 10048`

### Confirmed mechanism

After a `kr_mock` restart, the new process reconnects using the same fixed TCP
4-tuple as its predecessor:

```text
local source:  0.0.0.0:10000
remote target: 112.175.65.18:443
```

The operating system retains the predecessor’s tuple in `TIME_WAIT`. Reusing
the identical tuple during the protected interval fails at `connect()` with
`WinError 10048` / `WSAEADDRINUSE`.

The following points were directly verified:

- The failure log reports `phase=connect`, not `phase=bind`.
- `SO_REUSEADDR` is already present before `bind()` in the HTTP path at
  `src/core/broker_http.py:117` and the WebSocket path at
  `src/core/realtime_feed.py:58`.
- The HTTP custom network backend is wired into the active connection path.
- Fixed local ports are enforced by the machine’s firewall policy; ephemeral
  source-port behavior is not available without a policy change.
- A shared HTTP/WebSocket port arbiter cannot change the kernel’s
  same-4-tuple `TIME_WAIT` state and is not a valid Issue 3 fix.

### Live capture evidence

The authorized v50 restart and capture established:

- `kr_mock` PID `5432`.
- `kr_mock` instance `05bf6d91dce1425cae7f5b5424b53279`.
- Startup acknowledgment at approximately `2026-08-19 14:17:47 KST`.
- `us_mock` PID `10172` remained unchanged.

The capture file is:

`diagnostics/port10000_capture_20260819_141714.log`

It contains 56 distinct capture blocks. A raw search finds 83 failure-string
occurrences because preceding-context sections repeat some lines. Therefore,
83 is a raw occurrence count, while 56 is the distinct capture-block count.

The final failure block was recorded at:

```text
=== capture=2026-08-19T14:19:32.827+09:00 ===
2026-08-19 14:19:32 | WARNING  | kr_mock | pid=- instance=- | - | Fixed-port HTTP socket failure: phase=connect local=('0.0.0.0', 10000) remote=('112.175.65.18', 443) exception=OSError errno=10048 winerror=10048: [WinError 10048] 각 소켓 주소(프로토콜/네트워크 주소/포트)는 하나만 사용할 수 있습니다
TCP    192.168.0.10:10000     112.175.65.18:443      TIME_WAIT       0
```

The first successful post-restart HTTP I/O was line 19566 of the rotated
`kr_mock` log:

```text
2026-08-19 14:19:33 | DEBUG    | kr_mock | pid=- instance=- | - | HTTP I/O diagnostic: client=token operation=write state=after elapsed_ms=0.4 outcome=success
```

The following token read also succeeded at line 19568 with 8245 bytes. The
restart-to-success gap was 106 seconds at one-second application-log
precision. The registry did not define `TcpTimedWaitDelay`; the documented
Windows default when unset is 120 seconds. This supports the TIME_WAIT
interpretation, but does not independently prove the exact kernel expiry.

### Decision

The operator decision is to leave Issue 3 as-is. Existing exponential-backoff
retry logic (`1s, 2s, 4s, 8s, 16s, 30s`) survives the bounded restart window
without crashes or silent failures. A startup delay/gate could reduce log
noise but would not shorten the OS TCP state lifetime and was not justified by
the observed impact.

This decision may be revisited only if restart frequency, recovery duration,
self-resolution reliability, or operational log noise materially changes.

## Documentation reconciliation through v58

The operator-side consolidated file
`OPERATOR_RECORD_wrapup_v10-v55.md` exists in the operator’s Downloads folder
and was never found in this repository’s current filesystem or Git history.
The evidence does not prove whether an untracked repository copy ever existed
and was later removed. The repository therefore retains the following local
records:

- `OPERATOR_RECORD_wrapup_v10-v44.md`: historical progress through v44.
- `OPERATOR_RECORD_closing_issue3_v36-v55.md`: authoritative Issue 3 closing
  record.
- This file: consolidated repository handoff through v58.

The older `HANDOFF_NEXT_AI_HTTP_WS_PORT_COLLISION.md` contains stale Issue 3
claims, including an active HTTP/WebSocket collision, absence of
`SO_REUSEADDR`, and a proposed shared port arbiter. Preserve it as historical
context, but do not use its proposed arbiter as the current Issue 3 direction.

The next-AI instruction file
`INSTRUCTIONS_NEXT_AI_POST_ISSUE3_CLOSURE.md` is untracked, was created and
last modified at `2026-08-19 14:55:08`, and has no Git or in-repository
creation trace. It remains documentation context only.

## Current repository and process state

Expected tracked modification:

```diff
-            keepalive_expiry=5.0,
+            keepalive_expiry=30.0,
```

The change is in `src/core/broker_http.py`, remains uncommitted, and is
intentionally retained as a tested but unrelated bounded experiment. It does
not fix the Issue 3 restart collision.

Known preserved untracked content includes:

- `HANDOFF_ACCOUNT_LOCK_BYPASS_NETWORK_DIAGNOSTICS.md`
- `HANDOFF_NEXT_AI_HTTP_WS_PORT_COLLISION.md`
- `HANDOFF_NEXT_AI_INSTRUCTIONS_v30.md`
- `INSTRUCTIONS_NEXT_AI_POST_ISSUE3_CLOSURE.md`
- `OPERATOR_RECORD_wrapup_v10-v44.md`
- `OPERATOR_RECORD_closing_issue3_v36-v55.md`
- `diagnostics/port10000_capture.py` and its capture logs

Most recently verified worker state:

- `kr_mock`: PID `5432`, `RUNNING`, instance
  `05bf6d91dce1425cae7f5b5424b53279`.
- `us_mock`: PID `10172`, `RUNNING`, instance
  `45acf6eeb59b4716a2c44d79ba5bf837`.
- `kr_real` and `us_real`: untouched.

## Instructions for the next AI

1. Read this record and `OPERATOR_RECORD_closing_issue3_v36-v55.md` before
   interpreting any older handoff.
2. Treat Issue 1 and Issue 2 as resolved and Issue 3 as closed with no fix.
3. Treat `HANDOFF_NEXT_AI_HTTP_WS_PORT_COLLISION.md` as superseded for Issue 3
   wherever it proposes an active HTTP/WebSocket collision or port arbiter.
4. Do not implement a startup gate, port arbiter, socket change, or firewall
   change without new explicit operator authorization.
5. Do not restart or stop workers automatically. Keep `us_mock`, `kr_real`,
   and `us_real` out of scope.
6. Preserve the single `keepalive_expiry=30.0` diff and all unrelated
   untracked files unless the operator separately authorizes disposition.
7. If asked only for status or review, use read-only checks and report exact
   evidence; do not convert a historical proposal into an implementation.
8. If the operator authorizes a documentation cleanup, prefer a short
   supersession notice or successor handoff over rewriting historical records.
   List both `OPERATOR_RECORD_wrapup_v10-v44.md` and
   `OPERATOR_RECORD_closing_issue3_v36-v55.md` as sources because neither
   alone contains the complete v10–v55 investigation.

## Safety boundaries

- No live credentials or real-account actions.
- No firewall/WFP changes.
- No manual duplicate worker launches.
- No staging, commit, deletion, rename, or broad cleanup without explicit
  authorization.
- Keep implementation, runtime restart, verification, and commit decisions
  separate.

## Verification checklist for a future read-only round

From the repository root:

```powershell
git status --short
git diff -- src/core/broker_http.py
rg -n 'keepalive_expiry|phase = "connect"|SO_REUSEADDR' src\core\broker_http.py src\core\realtime_feed.py
python -m src.worker_supervisor status --account kr_mock --market KR
python -m src.worker_supervisor status --account us_mock --market US
```

Expected results are one tracked source diff containing only
`keepalive_expiry=30.0`, preserved untracked diagnostics/handoffs, and worker
status consistent with the current supervisor state. Do not run a restart or
live network test unless separately authorized.

**The investigation is closed. Further action requires an explicit operator
decision.**
