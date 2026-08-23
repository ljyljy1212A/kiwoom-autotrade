# Operator Record Wrap-Up: Issue 3 Through v55

## Purpose

This is the English closing handoff for the `kr_mock` fixed-port
`WinError 10048` investigation from v36 Part B through v55. It is written
for the next AI and operator. The facts below are evidence-backed; any
remaining causal statement marked as inferred must not be presented as a
direct measurement.

## Final status

**Issue 3 is closed. The root mechanism is confirmed, the impact is bounded
and self-healing, and no new fix is implemented.**

The confirmed failure occurs when a restarted `kr_mock` attempts to reuse the
same TCP 4-tuple immediately after its predecessor closed:

```text
local source:  0.0.0.0:10000
remote target: 112.175.65.18:443
```

The operating system rejects the `connect()` while the previous 4-tuple is
still represented by `TIME_WAIT`, producing `WinError 10048` / `WSAEADDRINUSE`.
The exact `TIME_WAIT` expiry mechanism is inferred from the evidence; the
failure location, fixed endpoints, restart timing, and self-resolution are
confirmed.

## Progress and decisions

### v36 Part B: initial framing

The issue was initially described generically as a source-port conflict after
restart. The early evidence did not distinguish a competing process from
kernel TCP state.

### v37–v44: bounded keepalive experiment

The working hypothesis was that a short HTTP keepalive expiry caused a
connection eviction and a new fixed-port bind while the prior socket was in
`TIME_WAIT`. The bounded change was applied and tested:

```diff
-            keepalive_expiry=5.0,
+            keepalive_expiry=30.0,
```

The change did not remove the restart-time failure. It remains intentionally
uncommitted and is unrelated to the confirmed same-4-tuple mechanism.

### v49–v50: live capture and authorized restart

A standalone diagnostic harness was created under `diagnostics/` and used
only to tail logs and collect `netstat` output. One graceful `kr_mock`
restart was explicitly authorized. `us_mock` was not restarted, and the real
workers were not touched.

The restart evidence was:

- New `kr_mock` PID: `5432`.
- New instance: `05bf6d91dce1425cae7f5b5424b53279`.
- Startup acknowledged at approximately `2026-08-19 14:17:47 KST`.
- `us_mock` PID `10172` remained unchanged.

The capture artifact is:

`diagnostics/port10000_capture_20260819_141714.log`

### v51–v53: source-port and socket-path verification

Read-only inspection established that:

- The fixed HTTP source port `10000` and fixed WebSocket source port `443`
  are application-configured.
- Active firewall rules permit those source-port exceptions to the broker
  host and block the other local source-port ranges.
- `SO_REUSEADDR` is already applied to both HTTP and WebSocket socket paths,
  before `bind()`.
- The HTTP custom network backend is actually wired into the active
  `httpcore` connection pool.
- The failure is not a dead socket-option path or a bind-order bug.

The port-arbiter proposal in the older handoff is not applicable to this
failure: coordinating application owners cannot change the kernel's
same-4-tuple `TIME_WAIT` state.

### v54–v55: self-resolution and evidence reconciliation

The full capture contains **56 distinct capture blocks**. A separate raw
search finds **83 occurrences** of the failure text because preceding-context
sections repeat some failure lines. Therefore, `83` is a raw occurrence count,
not a distinct-event/block count.

The last capture block records:

```text
=== capture=2026-08-19T14:19:32.827+09:00 ===
--- triggering log line ---
2026-08-19 14:19:32 | WARNING  | kr_mock | pid=- instance=- | - | Fixed-port HTTP socket failure: phase=connect local=('0.0.0.0', 10000) remote=('112.175.65.18', 443) exception=OSError errno=10048 winerror=10048: [WinError 10048] 각 소켓 주소(프로토콜/네트워크 주소/포트)는 하나만 사용할 수 있습니다
--- netstat -ano entries containing :10000 ---
  TCP    192.168.0.10:443       112.175.65.18:10000    ESTABLISHED     10172
  TCP    192.168.0.10:10000     112.175.65.18:443      TIME_WAIT       0
```

The first successful post-restart HTTP I/O is line 19566 of the rotated
`kr_mock` log:

```text
2026-08-19 14:19:33 | DEBUG    | kr_mock | pid=- instance=- | - | HTTP I/O diagnostic: client=token operation=write state=after elapsed_ms=0.4 outcome=success
```

The following read also succeeded at line 19568:

```text
2026-08-19 14:19:33 | DEBUG    | kr_mock | pid=- instance=- | - | HTTP I/O diagnostic: client=token operation=read state=after elapsed_ms=7.8 outcome=success bytes=8245
```

The restart-to-success gap is 106 seconds at the application log's
one-second timestamp precision. The capture timestamp to the displayed
success timestamp is 0.173 seconds, subject to that one-second log precision.

The registry check found no explicit `TcpTimedWaitDelay` value under
`HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters`. The documented
Windows default when the value is unset is 120 seconds. The observed 106
seconds is approximately 14 seconds shorter than that default. This supports
the TIME_WAIT interpretation but does not independently prove the exact
kernel expiry duration.

## Current repository state

The expected working-tree state at close is:

- `src/core/broker_http.py`: one uncommitted intentional change,
  `keepalive_expiry=5.0` to `keepalive_expiry=30.0`.
- `diagnostics/port10000_capture.py`: standalone diagnostic harness.
- Existing untracked handoffs remain preserved:
  - `HANDOFF_ACCOUNT_LOCK_BYPASS_NETWORK_DIAGNOSTICS.md`
  - `HANDOFF_NEXT_AI_HTTP_WS_PORT_COLLISION.md`
  - `HANDOFF_NEXT_AI_INSTRUCTIONS_v30.md`
  - `OPERATOR_RECORD_wrapup_v10-v44.md`

No Issue 3 commit was made. Do not commit, remove, or revert these files
without separate explicit authorization.

## Safety boundaries for the next AI

- Do not restart or stop any worker automatically.
- Do not touch `us_mock`, `kr_real`, or `us_real`.
- Do not change firewall/WFP rules or local-port policy.
- Do not access live credentials or perform real-account actions.
- Do not implement a startup gate, port arbiter, socket change, or other fix
  unless the operator gives new explicit authorization.
- Treat the current conclusion as closed unless new evidence shows that the
  window is not bounded, does not self-resolve, or affects a real worker.
- Keep HTTP failures and WebSocket reconnect messages analytically separate;
  WebSocket token acquisition may depend on the HTTP path, but they are not
  interchangeable event counts.

## Recommended future direction if the operator reopens the issue

If restart log noise or retry waste becomes operationally significant, the
best-supported candidate is a bounded startup reconnect delay/gate for the
fixed-port HTTP client. It would reduce unnecessary attempts during the
known window but would not shorten the OS TCP state lifetime. A port arbiter
is not supported by the current evidence for this specific failure mode.

Any reopened implementation round must separately authorize source changes,
runtime restart, verification, and commit.

## Verification checklist

For a read-only recheck, run from the repository root:

```powershell
(Select-String -Path diagnostics\port10000_capture_20260819_141714.log -Pattern '^=== capture=').Count
rg -n 'keepalive_expiry' src\core\broker_http.py
rg -n 'Fixed-port HTTP socket failure: phase=connect|HTTP I/O diagnostic:.*outcome=success' logs\kr_mock.2026-08-19_13-52-17_305115.log
Get-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters' -Name TcpTimedWaitDelay -ErrorAction SilentlyContinue
git status --short
```

Expected evidence is 56 capture blocks, `keepalive_expiry=30.0`, a final
`phase=connect` failure followed by successful token HTTP I/O, no explicit
`TcpTimedWaitDelay` output, and no unauthorized source/runtime changes.

**Issue 3 is closed. No further action is pending unless the operator
explicitly reopens it.**
