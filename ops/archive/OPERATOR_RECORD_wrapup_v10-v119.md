# Operator Record Wrap-Up and Next-AI Instructions: v10–v119

## Purpose

This English handoff summarizes the Windows Kiwoom investigation through v119:
verified work, live findings, unresolved decisions, current runtime state,
pending worktree changes, and safety boundaries for the next AI.

This document is context and handoff guidance. It does not authorize source
changes, worker actions, firewall/WFP changes, credentials, cleanup, or commits
unless the operator separately authorizes them.

## Executive summary

- The balance-monitor stall was fixed and verified.
- Quote-pipeline staleness was instrumented and verified; commit c045350
  contains that work.
- Session-aware closed-session balance-monitor cadence was verified on both
  mock workers and committed as 37ea0e7.
- The original restart-time WinError 10048/TCP TIME_WAIT issue was diagnosed
  and intentionally left without a code fix.
- KR WebSocket port separation from local 10000 to 10001 was implemented and
  unit-tested, but live verification failed with WinError 10013.
- Firewall evidence shows local source ports 443 and 10000 are permitted to
  the broker host while the range containing 10001 is blocked. Firewall API
  enumeration still returned Access is denied.
- Git history shows the repository root used ephemeral WebSocket ports. The
  first explicit pinning commit assigned KR 10000 and US 443; no tracked KR
  443 implementation exists in this checkout.
- Simultaneous same-port loopback sharing can work for different endpoints,
  but ordinary close/rebind churn fails with WinError 10048 and TIME_WAIT.
- Isolated SO_LINGER(1, 0) testing made 15/15 close/rebind iterations pass.
- Part 1 added HTTP-stream SO_LINGER(1, 0) handling and passed the full test
  suite, but live us_mock restart-and-verify failed: fresh WinError 10048
  errors appeared immediately after restart.
- us_mock auto-trading is false. kr_mock is stopped. No real account has
  been touched.

## Completed work

### Issue 1 — Scheduled balance-monitor stall

A dead failure-handler method call could terminate the monitor while output was
discarded. The call was removed, and failure visibility was improved with an
asyncio exception handler and task completion callback. Earlier live
observation recorded approximately 20 minutes and 156 clean iterations.

### Issue 2 — Quote-pipeline staleness

Quote cache-age/subscription accessors, a passive quote-health monitor, and a
max-staleness recovery warning were added and verified. Earlier live evidence
showed fresh cache ages, correct first-tick behavior, expected cadence, zero
staleness warnings, and zero new errors.

Commit:

~~~text
c045350 Add quote-health cache-age accessors and passive monitor
~~~

### Session-aware balance-monitor cadence

The balance-only monitor remains at five seconds during an active session and
sleeps at most 60 seconds while closed. Both mock workers were observed for
10 minutes or more with approximately 60–64 second closed-session gaps.

Commit:

~~~text
37ea0e71a77ee813c9c908ddb9fbf87c28ae9c65
Reduce closed-session balance monitor polling cadence
~~~

## Port and socket findings

### Current source-port selections

| Path | Local source port | Remote endpoint behavior |
|---|---:|---|
| KR mock HTTP | 10000 | broker host remote 443 |
| KR mock WebSocket | 10001 | mock WebSocket remote 10000 |
| US mock HTTP | 443 | broker host remote 443 |
| US mock WebSocket | 443 | mock WebSocket remote 10000 |

The KR 10001 source-port change remains uncommitted and failed live
verification with WinError 10013. Do not choose another port without a new
operator decision.

### Firewall evidence

Relevant raw rules identified with netsh:

~~~text
Rule Name: codex_sandbox_allow_mockapi_tcp_10000
Direction: Out
RemoteIP: 112.175.65.18/32
Protocol: TCP
LocalPort: Any
RemotePort: 10000
Action: Allow
~~~

~~~text
Rule Name: codex_sandbox_offline_block_kiwoom_other_ports
Direction: Out
RemoteIP: 112.175.65.18/32
Protocol: TCP
LocalPort: 1-442,444-9999,10001-65535
RemotePort: Any
Action: Block
~~~

Firewall profile policy is BlockInbound,AllowOutbound; the Kiwoom-specific
restriction is source-port-based. Local source ports 443 and 10000 are outside
the block range. 10001 and higher ports in the listed range are blocked.
Get-NetFirewallRule continued to return Access is denied.

### Git-history result

The complete tracked history for src/core/realtime_feed.py is:

~~~text
c045350 Add quote-health cache-age accessors and passive monitor
22263c9 Enable SO_REUSEADDR for fixed broker sockets
b299fbc fix: use sock= instead of local_addr= for WinError 52
67cefa8 Bind mock websocket workers to assigned source ports
b6b221a chore: initial commit - Linux portability work
~~~

The root version used:

~~~python
async with websockets.connect(self.ws_url, ping_interval=None, max_size=2**22) as ws:
~~~

It had no bind, local_addr, local_port, source_address, or socket port
pinning. Commit 67cefa8 introduced explicit pinning with KR 10000 and US 443.
No tracked pre-67cefa8 state references KR local port 443.

### Binding mechanism

The WebSocket path explicitly creates, configures, binds, and connects a raw
socket:

~~~python
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind((bind_address, local_port))
sock.connect(remote_address)
~~~

The HTTP path uses httpx/httpcore with a custom AnyIO backend that performs
the same raw socket bind. It uses one pooled connection, one keepalive
connection, keepalive_expiry=30.0, and retries=0 at the transport pool.

## Shared-port experiments

### Basic simultaneous sharing

An isolated loopback test connected two sockets using the same local port to
different remote listeners. Both the plain SO_REUSEADDR case and the
SO_EXCLUSIVEADDRUSE=0 case succeeded.

This proves only that simultaneous different-endpoint sharing can work in
principle on this Windows environment.

### Ordinary close/rebind churn

With one socket held open and a second socket repeatedly closed and rebound to
the same local port:

~~~text
SO_REUSEADDR only: 1 success, 14 failures
SO_EXCLUSIVEADDRUSE=0: 1 success, 14 failures
Failure: WinError 10048
Netstat: closed connection remained TIME_WAIT
~~~

### Isolated SO_LINGER(1, 0) churn

Applying SO_LINGER with linger enabled and timeout zero to the short-lived
socket before close produced:

~~~text
15 successes
0 failures
No TIME_WAIT entry observed for the reused local port
~~~

This uses an RST-style abort rather than graceful FIN closure. A peer may see
a connection reset or lose in-flight data. This result was loopback-only and
not broker verification.

## Part 1 implementation and failed live verification

### Implemented source change

src/core/broker_http.py now wraps fixed-port HTTP streams so that
SO_LINGER(1, 0) is set immediately before the wrapped stream's aclose().
The failed-connect cleanup remains the ordinary sock.close() path. The
WebSocket path was not changed.

The implementation was tested with:

- a socket-option spy unit test;
- an actual FixedPortAsyncHTTPTransport loopback test with 15 close/reconnect
  cycles;
- the full suite.

Results:

~~~text
Baseline: 81 passed, 4 skipped, 6 warnings
Focused broker HTTP tests: 2 passed, 1 warning
Full suite after implementation: 83 passed, 4 skipped, 6 warnings
~~~

### us_mock restart-and-verify, v119

Before restart:

~~~text
PID 13260, RUNNING
auto_trading_enabled=false
~~~

The worker stopped cleanly and restarted as:

~~~text
PID 14808, RUNNING
instanceId 55858a566ee54e90bcc9a30392f78f20
auto_trading_enabled=false
~~~

Fresh post-restart log evidence immediately failed the primary criterion:

~~~text
FRESH_10048=16
~~~

Representative failure:

~~~text
Fixed-port HTTP socket failure:
phase=connect local=('0.0.0.0', 443)
remote=('112.175.65.18', 443)
winerror=10048
~~~

The balance monitor then logged:

~~~text
Account balance monitor deferred: RetryError
~~~

The WebSocket initially also logged WinError 10048, later logged successful
login and registration, and quote health logged cache_age=missing. No
successful balance synchronization was observed in the short failed window.
Per the v119 boundary, observation stopped as soon as fresh 10048 failures were
confirmed; no in-round fix was attempted.

## Current working-tree state

The current modified tracked files are:

~~~text
src/core/broker_http.py
src/core/engine.py
src/core/kiwoom_client.py
src/core/realtime_feed.py
src/core/token_manager.py
~~~

The current untracked work includes:

~~~text
HANDOFF_ACCOUNT_LOCK_BYPASS_NETWORK_DIAGNOSTICS.md
HANDOFF_NEXT_AI_HTTP_WS_PORT_COLLISION.md
HANDOFF_NEXT_AI_HTTP_WS_PORT_COLLISION_SUPERSEDED.md
HANDOFF_NEXT_AI_INSTRUCTIONS_v30.md
INSTRUCTIONS_NEXT_AI_POST_ISSUE3_CLOSURE.md
OPERATOR_RECORD_closing_issue3_v36-v55.md
OPERATOR_RECORD_wrapup_v10-v107.md
OPERATOR_RECORD_wrapup_v10-v119.md
OPERATOR_RECORD_wrapup_v10-v44.md
OPERATOR_RECORD_wrapup_v10-v58.md
OPERATOR_RECORD_wrapup_v10-v81.md
PROPOSAL_v103_kr_mock_port_separation.md
PROPOSAL_v116_fixed_port_socket_close_behavior.md
PROPOSAL_v69_session_aware_balance_monitor_interval.md
PROPOSAL_v91_ratelimit_observability.md
diagnostics/
src/core/rate_limit_observability.py
tests/test_broker_http.py
tests/test_rate_limit_observability.py
tests/test_realtime_feed.py
~~~

The rate-limit observability changes in engine.py, kiwoom_client.py,
token_manager.py, src/core/rate_limit_observability.py, and its test are
documented as Proposal v91 work: unit-tested but not live-verified. Preserve
them unless the operator separately authorizes review or cleanup.

No commit has been made for the pending changes.

## Current runtime state

Last verified after the v119 restart-and-verify:

~~~text
kr_mock: STOPPED, PID 10876, instance a30568eaa9ef4bcf9ab47f279d728ca2
us_mock: RUNNING, PID 14808, instance 55858a566ee54e90bcc9a30392f78f20
us_mock auto_trading_enabled: false
kr_real: untouched and out of scope
us_real: untouched and out of scope
~~~

## Instructions for the next AI

1. Treat the Part 1 live verification as failed. Do not claim that
   SO_LINGER(1,0) fixed the live us_mock issue.
2. Do not restart, stop, or otherwise touch us_mock without a new explicit
   operator decision. It is currently running with auto-trading disabled.
3. Do not restart or touch kr_mock; it remains stopped.
4. Do not implement Part 2 or change the KR WebSocket port.
5. Do not change broker_http.py, realtime_feed.py, control files,
   firewall/WFP rules, or retry behavior without explicit authorization.
6. Treat the current 10048 failure as unresolved. The linger fix passed
   isolated churn testing but failed at the live connect phase, so further
   diagnosis must distinguish initial same-port bind collision from
   close/rebind TIME_WAIT churn.
7. Preserve unrelated dirty worktree changes and do not stage or commit.
8. For status/review requests, use read-only checks and report literal output.
9. For any new implementation, state behavior-affecting assumptions, make
   surgical changes only, and run focused tests before requesting a checkpoint.

## Read-only verification checklist

From the repository root:

~~~powershell
git status --short
git diff --stat
python -m src.worker_supervisor status --account kr_mock --market KR
python -m src.worker_supervisor status --account us_mock --market US
Get-Content data\control\us_mock.control.json
rg -n "10048|Fixed-port HTTP socket failure|Account balance monitor deferred" logs\us_mock.log
~~~

Expected safety state is kr_mock stopped, us_mock running with
auto_trading_enabled=false, no real-account activity, and no automatic
restart, firewall change, source change, staging, or commit.

