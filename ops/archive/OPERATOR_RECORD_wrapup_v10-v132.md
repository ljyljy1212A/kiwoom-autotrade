# Operator Record Wrap-Up and Next-AI Instructions: v10–v132

## Purpose

This is the consolidated English handoff for the Windows Kiwoom autotrade repository. It records verified implementation history, live evidence, the converged explanation for the US mock fixed-port HTTP failure, unresolved limits, safety events, and read-only operating rules. It is documentation only; it does not authorize source changes, worker actions, firewall changes, registry changes, staging, or commits.

## Executive summary

- The balance-monitor stall was fixed and verified.
- Quote-health observability was added and verified in commit `c045350`.
- Closed-session balance polling was changed and verified in commit `37ea0e71a77ee813c9c908ddb9fbf87c28ae9c65`.
- KR mock WebSocket source-port separation from `10000` to `10001` was unit-tested, but live verification failed with `WinError 10013`.
- The live US mock HTTP failure is now a converged, evidence-backed explanation rather than an open set of equally likely hypotheses: graceful `SHUT_WR` behavior can leave the short-lived HTTP connection in `TIME_WAIT`, and the observed `121–126` second lifetime overlaps the approximately `60–64` second closed-session polling interval. Reconnects to the same fixed local/remote tuple then produce recurring `WinError 10048` bursts.
- This explanation is strongly consistent with the live evidence but is not packet-confirmed certainty. Runtime OS-handle identity and direct FIN-versus-RST evidence remain unconfirmed.
- No remediation option is authorized or implemented. The current worktree remains intentionally dirty and must be preserved.

## Root-cause synthesis

### Close ordering and live `SO_LINGER` result

The fixed-port HTTP close path sets `SO_LINGER(1,0)` on the retained raw socket, then delegates to the installed AnyIO TCP close path. The literal source order is:

```text
SO_LINGER set
→ write_eof()
→ socket.shutdown(SHUT_WR) when the write buffer is empty
→ transport.close()
→ transport.abort()
→ eventual socket close
```

The `SHUT_WR` is a graceful half-close and is distinct from what `SO_LINGER` controls at the later `close()` operation. Consequently, setting linger first does not reliably prevent the graceful teardown from producing `TIME_WAIT` on the live path. The live implementation continued to report `WinError 10048` and `TIME_WAIT` despite the linger change.

An isolated close/rebind test passed `15/15` with `SO_LINGER(1,0)` and observed no `TIME_WAIT`, but its timing and socket lifecycle were different from the live worker. Its iteration spacing was not recorded in the handoff evidence, so it does not refute the live result.

### `TIME_WAIT` versus closed-session polling

The account balance monitor uses a five-second engine interval during an open session and sleeps at most 60 seconds while the market is closed. Live monitor timestamps showed approximately `60–64` second cycles.

A passive five-second netstat capture on 2026-08-20 directly observed the HTTP flow:

```text
local 192.168.0.10:443 → remote 112.175.65.18:443: TIME_WAIT
observed continuously from at least 06:17:35 through 06:19:31
absent by 06:19:36
ESTABLISHED again by 06:19:41
```

The observed lifetime bracket was approximately `121–126` seconds. During the same capture, `local:443 → remote:10000` remained `ESTABLISHED` under the US mock worker PID `12704`.

The overlapping log evidence was literal:

```text
06:16:32 HTTP REST success
06:17:32, 06:17:33, 06:17:36 WinError 10048
06:18:36, 06:18:37, 06:18:39 WinError 10048
06:19:39 HTTP REST success
06:20:39, 06:20:40, 06:20:42 WinError 10048
```

The pattern is consistent with retries occurring every approximately 60 seconds while the prior fixed-tuple connection remains in `TIME_WAIT`. The three-attempt bursts are also consistent with the existing application-level retry policy: three attempts with approximately one-second and two-second exponential waits.

### Firewall-driven fixed-port constraint

The current firewall rule was re-read in v132:

```text
Rule Name:  codex_sandbox_offline_block_kiwoom_other_ports
Enabled:    Yes
Direction:  Out
RemoteIP:   112.175.65.18/32
Protocol:   TCP
LocalPort:  1-442,444-9999,10001-65535
RemotePort: Any
Action:     Block
```

The relevant local ports excluded from that block are `443` and `10000`. The separate allow rule is:

```text
Rule Name:  codex_sandbox_allow_mockapi_tcp_10000
Enabled:    Yes
Direction:  Out
RemoteIP:   112.175.65.18/32
Protocol:   TCP
LocalPort:  Any
RemotePort: 10000
Action:     Allow
```

Therefore the fixed local-port arrangement is constrained by the shared-machine firewall state, not solely an independent application design choice.

### What remains unconfirmed

- H2: exact runtime OS-handle identity between the raw socket receiving `SO_LINGER` and the socket ultimately closed by the transport. Static source tracing found no `dup()` or replacement socket, but runtime `fileno()` identity was not instrumented.
- Direct packet evidence showing whether the local side sends FIN or RST for the `local:443 ↔ remote:443` flow. No packet capture was run because the available tooling required elevation, which was correctly declined.
- The exact machine-specific `TIME_WAIT` duration. `TcpTimedWaitDelay` is unset; the `121–126` second bracket is an observed live interval, not a registry-derived setting.

## Remediation options — proposals only

No option is authorized for implementation as of this document.

1. Widen the closed-session polling interval beyond approximately 126 seconds, with additional margin. This is a behavior/configuration change and would reduce balance freshness while the market is closed.

2. Change the close sequence so abortive closure occurs before any graceful `SHUT_WR`. This requires a code change and live re-verification. It carries RST and possible in-flight-data risks.

3. Change firewall policy to allow additional local ports, potentially using a small rotating local-port pool for the relevant remote IP. This requires an explicit firewall decision and likely coordinated code changes; it changes shared-machine network controls.

These options are unranked and unscheduled. The operator must explicitly authorize any implementation or firewall action.

## Round-by-round history: v119–v132

- **v119:** Restart verification showed fresh `WinError 10048` immediately after the US mock restart. WebSocket later recovered, but HTTP failures continued.
- **v120–v121:** Failures continued after startup for more than 14 minutes. HTTP and WebSocket retry loops were independently scheduled; HTTP transport retries were zero.
- **v122:** Passive netstat samples showed `local:443 → remote:443` alternating between `TIME_WAIT` and `ESTABLISHED`, while `local:443 → remote:10000` remained established.
- **v123:** Failures were consistently at `phase=connect`, with local `0.0.0.0:443` and remote `112.175.65.18:443`. The HTTP and WebSocket paths intentionally share local port `443`.
- **v124:** AnyIO/httpcore source inspection found `write_eof()` before transport close/abort. The live linger implementation still failed to prevent `WinError 10048`. The exact OS-handle identity remained unmeasured.
- **v125:** Static tracing confirmed `write_eof()` invokes `SHUT_WR`; corrected 15-second netstat capture ran for approximately three minutes. Log lines overlapped the capture but used `pid=-`.
- **v126:** Static source confirmed the transport eventually calls `self._sock.close()`. Packet tools were present but required elevation; no capture was started. H3 remained undetermined.
- **v127:** `call_soon()` deferred `_call_connection_lost()`. AnyIO yields once with `sleep(0)` but does not await a close-completion future; a buffered-write close race was structurally possible. H4 was not established as the live cause.
- **v128:** httpcore source showed idle expiry, server disconnect, and error-driven close triggers. Normal bounded request writes await the write event. Failure timing did not match a simple 30-second keepalive cadence.
- **v129:** Source confirmed approximately 60-second closed-session polling. `TcpTimedWaitDelay` was unset. Existing log gaps were commonly 63 and 123 seconds, and three-attempt bursts matched one- and two-second retry waits. H5 became consistent with the evidence.
- **v130:** `kr_mock` was independently confirmed running after the operator-reported reboot. The boot-time query required access denied without elevation, so no elevation was attempted. No worker action was taken in that round.
- **v131:** The operator-reported reboot explained the simultaneous startup of both mock workers. The boot-time query still required elevation. `kr_mock` was then stopped through the explicitly authorized graceful supervisor path and verified stopped. A five-minute passive netstat capture directly bracketed a `TIME_WAIT` interval at approximately 121–126 seconds.
- **v132:** The evidence was synthesized. The firewall rules were re-read and confirmed to constrain usable local ports to `443` and `10000` for the relevant remote IP. No remediation was implemented.

## Safety-relevant event log

- `kr_mock` was unexpectedly found running in v129, despite the standing requirement that it remain stopped.
- v130 independently confirmed the state and found process startup approximately nine minutes after the operator-reported PC reboot.
- The operator confirmed the reboot. Both mock workers started within approximately one millisecond of each other, consistent with an automatic startup mechanism.
- v131 stopped `kr_mock` through the supervisor’s graceful stop path and verified it stopped. `us_mock` remained running with automatic trading disabled.
- Open operational question: if the automatic startup mechanism still includes `kr_mock`, it will recur after future reboots. This document does not resolve or change that configuration.

## Current verified state

Verified after the v133 read-only checks:

```text
kr_mock: STOPPED, PID 9172 metadata retained by supervisor
us_mock: RUNNING, PID 12704
us_mock auto_trading_enabled: false
```

Current worktree summary:

```text
Modified tracked files:
src/core/broker_http.py
src/core/engine.py
src/core/kiwoom_client.py
src/core/realtime_feed.py
src/core/token_manager.py
```

The repository also contains the previously documented untracked handoffs, proposals, diagnostics, observability module, and tests. Preserve all of them. This document is a new untracked file and is not staged or committed.

## Instructions for the next AI

1. Confirm each session that `kr_mock` remains stopped. Do not start it.
2. Keep `us_mock` running with `auto_trading_enabled=false`; independently verify worker identity and control state.
3. Keep `kr_real` and `us_real` entirely out of scope.
4. Treat the live explanation as strongly consistent, not packet-confirmed proof. Do not claim `SO_LINGER(1,0)` fixed the issue.
5. Do not change source, retry behavior, polling intervals, controls, credentials, ports, firewall/WFP rules, or registry values without separate explicit authorization.
6. Do not operate one broker account on multiple machines or start duplicate workers.
7. Preserve all modified and untracked worktree state. Do not clean, stage, commit, reset, or overwrite it.
8. Any implementation of the three remediation options requires a new explicit authorization and a surgical change with focused verification first.

## Read-only verification checklist

```powershell
git status --short
git diff --stat
python -m src.worker_supervisor status --account kr_mock --market KR
python -m src.worker_supervisor status --account us_mock --market US
Get-Content data\control\us_mock.control.json
rg -n "WinError 10048|Fixed-port HTTP socket failure|Account balance monitor deferred" logs\us_mock.log
netstat -ano -p tcp | findstr "112.175.65.18"
```

Expected safety state: `kr_mock` stopped, `us_mock` running with automatic trading disabled, no real-account activity, no firewall or registry changes, no source changes, no staging, and no commit.
