# Operator Record Wrap-Up: Progress Through v44

## Purpose

This document summarizes the Kiwoom mock-worker investigation and the work completed through handoff v44. It is intended as a compact English handoff for the next AI and operator. Instructions in this document are operational context; the operator must explicitly authorize any action beyond the boundaries stated below.

## Current outcome

The authorized code change was applied and tested:

```diff
-            keepalive_expiry=5.0,
+            keepalive_expiry=30.0,
```

The change is correctly indented, changes only the value, and remains inside the `httpcore.AsyncConnectionPool(...)` call in `src/core/broker_http.py`.

The test suite passed at the established baseline:

```text
74 passed, 4 skipped, 5 warnings
```

`kr_mock` was gracefully restarted as authorized. The fix did not resolve the observed fixed-port collision: immediately after restart, the worker continued to produce HTTP `WinError 10048` failures. The observation was stopped without further code changes or another restart.

## Timeline and progress

### Earlier investigation

- The system is a Windows-only production deployment. Real workers and live broker accounts must remain isolated from mock-worker diagnostics.
- `kr_mock` and `us_mock` use fixed local source ports because the shared machine's sandbox/firewall behavior made removing fixed ports out of scope.
- The relevant HTTP pool was found in `src/core/broker_http.py` with one maximum connection, one keepalive connection, and `keepalive_expiry=5.0`.
- Balance-monitor cadence was measured at roughly 5–8 seconds between requests. The original hypothesis was that the 5-second pool expiry evicted the connection just before the next request, causing a new bind while the prior socket was still in Windows `TIME_WAIT`.
- The bounded fix `keepalive_expiry=30.0` was selected over `None` because the remote broker's idle-close behavior had not been observed and a bounded first change was preferred.

### v42–v44 execution

- v42 initially stopped because three known, pre-existing untracked handoff documents were present. No source files were modified at that point.
- v43 clarified that those three documents are expected background state and authorized proceeding.
- The first v43 edit accidentally changed indentation as well as the value. The diff gate correctly stopped the task before restart.
- v44 authorized discarding that malformed edit and reapplying the value-only change. The repository's `git checkout` was blocked by `.git/index.lock` permission denied, so the malformed line was restored with a surgical patch instead. The corrected value-only diff then passed inspection.
- The test suite passed with the baseline result.
- `kr_mock` PID `9412` was stopped through `src.worker_supervisor` with `mode=graceful` and restarted through the same supervisor. The new PID is `5716`.
- `us_mock` PID `10172` was not targeted and retained its prior start time.

## Restart evidence

- Pre-restart `kr_mock`: PID `9412`, start time `2026-08-19 11:38:24` local.
- Pre-restart `us_mock`: PID `10172`, start time `2026-08-19 08:01:17` local.
- Graceful stop request: `2026-08-19T12:52:59.4527557+09:00`.
- Stop result: `2026-08-19T12:53:00.1466852+09:00`, `mode=graceful`.
- Graceful start request: `2026-08-19T12:53:00.1518234+09:00`.
- New `kr_mock`: PID `5716`, startup acknowledged at `2026-08-19T12:53:02.3737697+09:00`.
- Startup log: `2026-08-19 12:53:02 | ... | 1 account worker(s) started`.

## Failed observation

The post-restart observation was only approximately 17 seconds because the target failure appeared immediately. Between `12:53:02` and `12:53:19`, the `kr_mock` log contained 18 matching `WinError 10048` entries.

Representative failure:

```text
Fixed-port HTTP socket failure: phase=connect local=('0.0.0.0', 10000) remote=('112.175.65.18', 443) ... winerror=10048
```

Consequences observed:

- Balance-monitor requests did not complete normally.
- Token HTTP requests failed during startup/reconnect.
- The WebSocket path repeatedly reported reconnects, but those failures were downstream of the same HTTP token-request bind failure.
- No successful quote-health evidence was available during the short failed window.

Per v44, no further fix, restart, firewall change, or workaround was attempted.

## Current repository state

Expected tracked change:

- `src/core/broker_http.py`: one uncommitted value change, `5.0` to `30.0`.

Known pre-existing untracked files, intentionally preserved:

- `HANDOFF_ACCOUNT_LOCK_BYPASS_NETWORK_DIAGNOSTICS.md`
- `HANDOFF_NEXT_AI_HTTP_WS_PORT_COLLISION.md`
- `HANDOFF_NEXT_AI_INSTRUCTIONS_v30.md`

No commit was authorized. Do not sweep the handoff documents into a commit.

## Safety boundaries

- `us_mock` was not restarted or stopped; PID `10172` remained unchanged.
- `kr_real` and `us_real` were not touched.
- No live credentials, real-account actions, firewall rules, or WFP settings were changed.
- Do not restart or stop any worker without explicit operator authorization.
- Do not attempt another code change in response to the failed observation without a new operator decision.
- Do not infer that the test-suite pass proves runtime connectivity; the runtime observation disproved that conclusion for this round.

## Instructions for the next AI

1. Treat the current result as **implemented but runtime-failed**, not as a successful fix.
2. Preserve the single-line `keepalive_expiry=30.0` change unless the operator authorizes a different design.
3. First inspect the current source diff, process identities, worker status, and recent `kr_mock` logs. Do not restart workers automatically.
4. Investigate why the HTTP pool still attempts `local=('0.0.0.0', 10000)` after the expiry change. Confirm whether the connection is being closed for another reason, whether multiple HTTP clients are being created, and whether the fixed-port binding path is shared by token, REST, and WebSocket-related requests.
5. Keep HTTP `WinError 10048` and WebSocket reconnect failures analytically separate, while recognizing that WebSocket token acquisition currently depends on the failing HTTP path.
6. Require a new explicit authorization before modifying code, restarting `kr_mock`, touching `us_mock`, changing local-port behavior, or inspecting/modifying firewall/WFP configuration.
7. If a future round is authorized, use the existing supervisor's graceful mechanism for `kr_mock` only and compare `us_mock` PID/start time before and after.

## Verification criteria for any future round

- The source diff contains only the explicitly authorized change.
- Automated tests remain at `74 passed, 4 skipped, 5 warnings`, unless the operator approves a changed baseline.
- Runtime evidence must show successful balance-monitor requests and zero HTTP-path `WinError 10048` occurrences over at least 2–3 minutes.
- Any WebSocket reconnect behavior must be reported separately.
- The final report must include process continuity, exact restart evidence, observation duration, error counts, working-tree state, and explicit confirmation that real workers and firewall/WFP were untouched.

