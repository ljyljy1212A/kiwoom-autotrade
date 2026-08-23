# Operator Record Wrap-up v10-v153

## Purpose

This English document summarizes the investigation through v153 and gives
the next AI a safe handoff. It is an operator record, not authorization for
further implementation or live activity.

## Executive conclusion

The US fixed-port HTTP/WebSocket problem remains unresolved.

Option 2, which waits for deferred HTTP socket close completion, was
live-tested in v147 and was insufficient: one cycle succeeded, then three
cycles reproduced WinError 10048.

The v151 change moved the US mock WebSocket local source port from 443 to
10002 while leaving US HTTP on 443. Unit tests passed, but v153 live
verification failed:

- the US WebSocket could not establish on 10002 and repeatedly returned
  WinError 10013;
- HTTP continued to fail at phase=connect on local port 443 with the
  original WinError 10048;
- the WebSocket never appeared as an established 10002 connection.

The port-separation implementation is therefore not live-verified as a
successful fix. Do not describe the issue as resolved or recommend a commit
without new evidence.

## Progress by session

### v91

Rate-limit and balance/quote observability was implemented and tested.
Those changes remain unstaged.

### v103-v105

The KR mock WebSocket was designed and implemented to move from local source
port 10000 to 10001, while KR HTTP remained on 10000. Operator records
identify this as an earlier uncommitted workstream.

Current source selections are:

- KR mock HTTP: 10000;
- KR mock WebSocket: 10001;
- US mock HTTP: 443;
- US mock WebSocket: 10002 after v151.

KR live verification is outside the current US investigation.

### v116

Fixed-port HTTP close behavior was changed to address an earlier TIME_WAIT
and rebind mechanism. It did not prove or resolve the later US
HTTP/WebSocket conflict.

### v134 and v143/v144

The balance-monitor interval was made session-aware. Option 2 added
deferred-close completion waiting in broker_http.py. It has focused tests,
including a fresh delayed-close churn test, but v147 showed it was
insufficient during live US verification.

### v147

Only us_mock was restarted in a confirmed CLOSED session:

    RESTART_COMMAND_TIME=2026-08-20T08:16:05.7642596+09:00
    pid=9124
    instance=0d5945c672f7465191e37809c70e891f

Natural idle-disconnect evidence included:

    TCP    192.168.0.10:443       112.175.65.18:443      CLOSE_WAIT      9124
    TCP    192.168.0.10:443       112.175.65.18:10000    ESTABLISHED     9124

The 10000 value was the remote WebSocket port, not the local source port.
The v149 erratum records this correction.

The first following balance cycle at 08:19:08 succeeded. Cycles at
08:22:08, 08:25:09, and 08:28:10 failed with phase=connect, local
0.0.0.0:443, remote 112.175.65.18:443, and WinError 10048. No deferred-close
wait timeout appeared. us_mock was stopped gracefully.

### v148-v150

The design kept US HTTP on local source port 443 and proposed US WebSocket
local source port 10002. Read-only checks found 10002 unused and outside the
displayed Windows excluded range 50000-50059 for both IPv4 and IPv6.
Firewall/WFP policy was intentionally not changed or conclusively tested.

The v150 impact scan found only two direct implementation edits:

- src/core/realtime_feed.py: US WebSocket local port 443 to 10002;
- tests/test_realtime_feed.py: rename the stale US test and expect 10002.

### v151

Exactly those two edits were applied. The full test suite result was:

    89 passed, 4 skipped, 6 warnings, 2 subtests passed in 17.13s

Important passing tests included:

- test_mock_kr_websocket_uses_separate_local_port;
- test_mock_us_websocket_uses_separate_local_port; and
- test_delayed_close_loopback_churn_waits_before_each_rebind.

### v152

The KR 10000 to 10001 diff visible against HEAD was confirmed to predate
v151. The v105 operator record identifies that KR change as an earlier
uncommitted implementation. v151 changed only the US line.

### v153

Pre-flight checks passed:

    US HTTP source port: 443
    US WebSocket source port: 10002
    kr_mock: STOPPED
    us_mock: STOPPED before restart
    auto_trading_enabled: false
    US calendar session: CLOSED

Only us_mock was restarted:

    RESTART_COMMAND_TIME=2026-08-20T08:55:26.6563749+09:00
    pid=4072
    instance=5a328c5c97fb4693bfe97c9c03417242

Natural idle-disconnect:

    IDLE_DISCONNECT_CAPTURE=2026-08-20T08:56:48.6939358+09:00
    TCP    192.168.0.10:443       112.175.65.18:443      CLOSE_WAIT      4072

The WebSocket did not establish on 10002. It repeatedly logged:

    [WinError 10013] 액세스 권한에 의해 숨겨진 소켓에 액세스를 시도했습니다

Four balance cycles were observed:

| Cycle | Timestamp | Result |
|---|---|---|
| 1 | 08:55:28 | REST balance work succeeded; WebSocket failed with 10013 |
| 2 | 08:58:29 | HTTP failed with 10048; WebSocket failed with 10013 |
| 3 | 09:01:30 | HTTP failed with 10048; WebSocket failed with 10013 |
| 4 | 09:04:31 | HTTP failed with 10048; WebSocket failed with 10013 |

The literal HTTP failure signature in cycles 2-4 was:

    Fixed-port HTTP socket failure:
    phase=connect
    local=('0.0.0.0', 443)
    remote=('112.175.65.18', 443)
    errno=10048
    winerror=10048

Cycle netstat showed HTTP on local 443 but no established WebSocket on
10002:

    TCP    192.168.0.10:443       112.175.65.18:443      ESTABLISHED     4072

us_mock was stopped gracefully and no PID 4072 sockets remained.

## Current verified safety state

At wrap-up creation:

    {"account": "kr_mock", "pid": 9172, "running": false, "instanceId": "9809f57875934293b1cec7f53fd8ec4b", "state": "STOPPED", "market": "KR"}
    {"account": "us_mock", "pid": 4072, "running": false, "instanceId": "5a328c5c97fb4693bfe97c9c03417242", "state": "STOPPED", "market": "US"}
    {"account": "us_mock", "auto_trading_enabled": false, "updated_at": "2026-08-17T16:24:23.645900+00:00", "updated_by": "telegram"}

No real-account worker was started or touched. No firewall, WFP, registry,
credential, account, or control-state change was made. No file was staged or
committed during v153.

The worktree contains known pre-existing unstaged and untracked changes.
Preserve unrelated changes.

## Current source and test state

Relevant current source:

    # src/core/kiwoom_client.py
    http_port = 10000 if mode == "mock" and market == "KR" else 443 if mode == "mock" and market == "US" else None

    # src/core/realtime_feed.py
    if self.client.market == "KR":
        return 10001
    if self.client.market == "US":
        return 10002

The current tests include the direct KR/US port assertions and the
delayed-close churn test. Unit-test success does not establish that local
port 10002 is permitted by the machine's effective firewall/WFP policy.

## Instructions for the next AI

1. Treat v153 as a literal FAIL, not successful live verification.
2. Do not restart kr_mock or us_mock without new explicit authorization.
   Both must remain stopped.
3. Do not change source, revert Option 2, change firewall/WFP policy, stage,
   or commit anything unless separately authorized.
4. Preserve the exact v153 evidence: WebSocket 10002 produced WinError
   10013; HTTP local 443 reproduced phase=connect WinError 10048.
5. The next decision is an operations/design review of the policy failure
   on 10002 and the continued HTTP 443 collision. Do not assume that port
   separation succeeded merely because unit tests pass.
6. If a future session is authorized to investigate, begin with read-only
   identification of the Windows firewall/WFP rule or policy blocking local
   source port 10002. Do not alter that policy under read-only authority.
7. Any future live test must use a confirmed CLOSED session, verify both
   worker states and auto_trading_enabled=false, capture literal logs and
   netstat for every cycle, stop on unexpected failure, and stop the worker
   cleanly at the end.
8. Every future handback must distinguish unit-test success, WebSocket
   startup success, HTTP balance-cycle success, and overall live-verification
   success.

## Operator decision requested

Do not authorize a commit or declare the US issue fixed from the current
evidence. The immediate decision is whether to authorize a read-only
investigation of the machine policy causing WinError 10013 on local port
10002, or to revise the port-separation design before any further live
restart. Any subsequent implementation or live verification requires its
own explicit authorization.
