# Operator Record Wrap-Up and Next-AI Instructions: v10–v124

## Purpose

This document is an English handoff for the next AI operating the Windows Kiwoom autotrade repository. It summarizes verified progress, live runtime evidence, unresolved technical questions, worktree boundaries, and safe verification rules. It is documentation only; it does not authorize source changes, worker actions, firewall changes, staging, or commits.

## Executive summary

- The balance-monitor stall was fixed and verified.
- Quote-health observability was added and verified; commit `c045350` contains that work.
- Session-aware closed-session balance polling was verified and committed as `37ea0e7`.
- KR mock WebSocket source-port separation from `10000` to `10001` was implemented and unit-tested, but live verification failed with `WinError 10013`.
- Firewall evidence shows outbound access for source port `443` and broker remote port `10000`, while the rule `codex_sandbox_offline_block_kiwoom_other_ports` blocks local ports `10001-65535` and other listed ranges for `112.175.65.18`.
- Ordinary same-port close/rebind churn reproduced `WinError 10048` and `TIME_WAIT` in isolated testing. Applying `SO_LINGER(1,0)` made 15/15 isolated iterations pass.
- The HTTP `SO_LINGER(1,0)` implementation passed focused and full tests but failed live verification: `us_mock` continued to produce fixed-port HTTP `WinError 10048` at local `0.0.0.0:443` to broker `112.175.65.18:443`.
- v120–v124 read-only diagnostics established that failures continue after startup, occur at `phase=connect`, and that US HTTP and US WebSocket intentionally share local port `443` while using different remote ports.
- The cause remains unresolved. Do not claim that the linger change fixed the live issue.

## Verified implementation history

### Balance monitor

The scheduled balance-monitor stall was fixed after a dead failure-handler call could terminate monitoring while output was discarded. Earlier live observation recorded approximately 20 minutes and 156 clean iterations.

### Quote health

Quote cache-age and subscription observability, a passive quote-health monitor, and stale-cache warnings were added and verified. Commit:

```text
c045350 Add quote-health cache-age accessors and passive monitor
```

### Session-aware polling

The balance-only monitor remains at five seconds during an active session and sleeps at most 60 seconds while closed. Both mock workers were observed with approximately 60–64 second closed-session gaps.

Commit:

```text
37ea0e71a77ee813c9c908ddb9fbf87c28ae9c65
Reduce closed-session balance monitor polling cadence
```

## Current port configuration

The current source contains:

```python
# src/core/kiwoom_client.py
http_port = 10000 if mode == "mock" and market == "KR" else 443 if mode == "mock" and market == "US" else None
self._http_gate = BrokerHTTPGate(http_port, logger)
```

```python
# src/core/realtime_feed.py
if self.client.market == "KR":
    return 10001
if self.client.market == "US":
    return 443
```

```python
DEFAULT_WS_MOCK = "wss://mockapi.kiwoom.com:10000/api/dostk/websocket"
```

Therefore the current mock source-port table is:

| Path | Local source port | Remote port |
|---|---:|---:|
| KR HTTP | 10000 | 443 |
| KR WebSocket | 10001 | 10000 |
| US HTTP | 443 | 443 |
| US WebSocket | 443 | 10000 |

The US HTTP and WebSocket same-local-port arrangement is intentional in the current source. Do not change it without explicit authorization.

## Firewall evidence

Raw rules previously verified with `netsh`:

```text
Rule Name: codex_sandbox_allow_mockapi_tcp_10000
Enabled: Yes
Direction: Out
Profiles: Domain,Private,Public
RemoteIP: 112.175.65.18/32
Protocol: TCP
LocalPort: Any
RemotePort: 10000
Action: Allow
```

```text
Rule Name: codex_sandbox_offline_block_kiwoom_other_ports
Enabled: Yes
Direction: Out
Profiles: Domain,Private,Public
RemoteIP: 112.175.65.18/32
Protocol: TCP
LocalPort: 1-442,444-9999,10001-65535
RemotePort: Any
Action: Block
```

`Get-NetFirewallRule` previously returned `Access is denied`. Do not infer rule origin from Codex-style names, and do not modify firewall/WFP state without a separate explicit decision.

## Socket experiments and HTTP implementation

An isolated same-local-port/different-remote-endpoint test succeeded with `SO_REUSEADDR`. Ordinary close/rebind churn produced:

```text
SO_REUSEADDR only: 1 success, 14 failures
Failure: WinError 10048
TIME_WAIT remained on the closed connection
```

With `SO_LINGER(1,0)` before close:

```text
15 successes
0 failures
No TIME_WAIT observed in the isolated test
```

The pending HTTP implementation in `src/core/broker_http.py` contains:

```python
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind((bind_address, local_port))
sock.connect(remote_address)
```

```python
async def aclose(self) -> None:
    self._raw_socket.setsockopt(
        socket.SOL_SOCKET,
        socket.SO_LINGER,
        struct.pack("ii", 1, 0),
    )
    await super().aclose()
```

The failed-connect path still uses:

```python
sock.close()
```

The current HTTP pool values are:

```python
max_connections=1,
max_keepalive_connections=1,
keepalive_expiry=30.0,
retries=0,
```

Offline verification before the live test:

```text
Baseline: 81 passed, 4 skipped, 6 warnings
Focused broker HTTP tests: 2 passed, 1 warning
Full suite after implementation: 83 passed, 4 skipped, 6 warnings
```

## Live v119–v124 evidence

### Runtime state

Last verified state:

```text
kr_mock: STOPPED, PID 10876
us_mock: RUNNING, PID 14808, instance 55858a566ee54e90bcc9a30392f78f20
us_mock auto_trading_enabled: false
kr_real: untouched and out of scope
us_real: untouched and out of scope
```

### v119 restart verification

The old worker was PID `13260`; the restarted worker was PID `14808`. Fresh `WinError 10048` appeared immediately after restart. Representative literal failure:

```text
Fixed-port HTTP socket failure: phase=connect local=('0.0.0.0', 443) remote=('112.175.65.18', 443) ... winerror=10048
```

The WebSocket initially logged `WinError 10048`, then later logged successful login and registration. The balance monitor logged `RetryError` and quote health reported `cache_age=missing` during the failed observation.

### v120–v121 observations

The old PID logged `WinError 10048` before shutdown, including during token-discard cleanup. The new PID also logged failures on both HTTP and WebSocket paths. Failures recurred for more than 14 minutes after the WebSocket recovery log, so the event was not limited to one startup instant.

The HTTP and WebSocket retry loops are independently scheduled. WebSocket backoff is `1, 2, 4, 8, 16, 30...` seconds. HTTP transport retries are `0`; higher-level request retries are separate.

### v122 netstat series

Three passive captures showed:

```text
21:24:44  local:443 -> remote:443  TIME_WAIT       0
21:25:49  local:443 -> remote:443  ESTABLISHED     14808
21:26:54  local:443 -> remote:443  TIME_WAIT       0
```

Throughout the series:

```text
local:443 -> remote:10000  ESTABLISHED  14808
```

### v123 source and runtime observations

The latest five HTTP failures all reported:

```text
phase=connect
local=('0.0.0.0', 443)
remote=('112.175.65.18', 443)
```

The installed `httpcore` pool closes expired connections through `connection.aclose()`, which reaches the wrapped stream close. The failed-connect path remains an ordinary `sock.close()` path. There is no SO_LINGER log marker.

### v124 library inspection

Installed versions:

```text
anyio 4.14.2
httpcore 1.0.9
```

Installed AnyIO asyncio TCP close code:

```python
async def aclose(self) -> None:
    self._closed = True
    if not self._transport.is_closing():
        try:
            self._transport.write_eof()
        except OSError:
            pass

        self._transport.close()
        await sleep(0)
        self._transport.abort()
```

The inspected TCP path does not directly call `socket.shutdown()`. The source passes the same `sock` variable into `asyncio.create_connection(StreamProtocol, sock=sock)`, and the application retains the same `raw_socket` reference for the SO_LINGER call. Exact OS-handle identity was not independently measured with `.fileno()`.

The v124 passive netstat loop was run at approximately 65-second intervals. The first loop filter emitted no matching lines because of the PowerShell `findstr` expression used. A subsequent direct broker-IP capture showed:

```text
TCP 192.168.0.10:443 112.175.65.18:443 TIME_WAIT 0
TCP 192.168.0.10:443 112.175.65.18:10000 ESTABLISHED 14808
```

The log continued to show `phase=connect` failures, including:

```text
2026-08-19 21:36:35 ... Fixed-port HTTP socket failure: phase=connect ... WinError 10048
2026-08-19 21:36:37 ... Fixed-port HTTP socket failure: phase=connect ... WinError 10048
2026-08-19 21:38:37 ... Fixed-port HTTP socket failure: phase=connect ... WinError 10048
2026-08-19 21:38:38 ... Fixed-port HTTP socket failure: phase=connect ... WinError 10048
2026-08-19 21:38:40 ... Fixed-port HTTP socket failure: phase=connect ... WinError 10048
```

No hypothesis has been confirmed or refuted.

## Current worktree state

At the latest v124 check, these tracked files were modified:

```text
src/core/broker_http.py
src/core/engine.py
src/core/kiwoom_client.py
src/core/realtime_feed.py
src/core/token_manager.py
```

The worktree also contains the previously documented untracked handoffs,
proposals, diagnostics, observability module, and tests, including:

```text
OPERATOR_RECORD_wrapup_v10-v119.md
PROPOSAL_v116_fixed_port_socket_close_behavior.md
diagnostics/
src/core/rate_limit_observability.py
tests/test_broker_http.py
tests/test_rate_limit_observability.py
tests/test_realtime_feed.py
```

Do not clean, stage, commit, reset, or overwrite these files without explicit authorization.

## Instructions for the next AI

1. Treat the live `SO_LINGER(1,0)` verification as failed. Do not claim that it fixed the live `us_mock` problem.
2. Keep `us_mock` running with `auto_trading_enabled=false` unless the operator explicitly authorizes a controlled action.
3. Keep `kr_mock` stopped. Do not start it.
4. Keep `kr_real` and `us_real` entirely out of scope.
5. Do not change `broker_http.py`, `realtime_feed.py`, retry behavior, controls, credentials, firewall/WFP rules, or ports without explicit authorization.
6. Preserve all existing modified and untracked worktree state.
7. Treat `WinError 10048` as unresolved. Report literal evidence and distinguish automated tests from live broker evidence.
8. For any new investigation, state only behavior-affecting assumptions, use read-only checks first, and do not infer current worker identity from stale files alone.
9. If implementation is explicitly authorized, make a surgical change, run focused tests first, then run the relevant broader verification before requesting a checkpoint.
10. Do not operate a real account on a second machine or start duplicate workers for the same broker account.

## Read-only verification checklist

```powershell
git status --short
git diff --stat
python -m src.worker_supervisor status --account kr_mock --market KR
python -m src.worker_supervisor status --account us_mock --market US
Get-Content data\control\us_mock.control.json
rg -n "10048|Fixed-port HTTP socket failure|Account balance monitor deferred" logs\us_mock.log
netstat -ano -p tcp | findstr "112.175.65.18"
```

Expected safety state: `kr_mock` stopped, `us_mock` running with automatic trading disabled, no real-account activity, no firewall changes, no source changes, no staging, and no commit.
