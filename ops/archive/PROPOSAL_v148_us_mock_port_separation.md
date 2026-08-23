# Proposal v148: Separate `us_mock` HTTP and WebSocket Source Ports

## Status

Design proposal only. No source implementation, unit-test execution for this proposal, worker restart, live traffic, firewall/WFP change, staging, or commit is authorized by v148. `kr_mock` and `us_mock` must remain stopped until separately authorized. Real-account workers are out of scope.

## Problem statement

V147 live verification showed that Option 2 (deferred HTTP close completion) is insufficient. The first post-`CLOSE_WAIT` balance cycle succeeded, but cycles at `08:22:08`, `08:25:09`, and `08:28:10` failed at HTTP `connect()` with `WinError 10048`. The wait completed normally, no wait-timeout log appeared, and the long-lived US WebSocket remained `ESTABLISHED` while sharing local port `443` with HTTP.

This proposal addresses that suspected same-process local-port collision by leaving US HTTP on local port `443` and assigning US WebSocket a distinct local source port. It does not change remote broker ports or trading behavior.

## Exact current configuration

### US HTTP

Literal current lines from `src/core/kiwoom_client.py:112-113`:

```python
http_port = 10000 if mode == "mock" and market == "KR" else 443 if mode == "mock" and market == "US" else None
self._http_gate = BrokerHTTPGate(http_port, logger)
```

For mock US, HTTP local source port is therefore `443`. The fixed-port transport binds with:

```python
sock.bind((bind_address, local_port))
```

### US WebSocket

Literal current lines from `src/core/realtime_feed.py:120-126`:

```python
@property
def ws_local_port(self) -> int | None:
    if self.client.mode != "mock":
        return None
    if self.client.market == "KR":
        return 10001
    if self.client.market == "US":
        return 443
    return None
```

The WebSocket binding path is:

```python
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
bind_address = "::" if family == socket.AF_INET6 else "0.0.0.0"
sock.bind((bind_address, local_port))
sock.connect(remote_address)
```

Thus both US HTTP and US WebSocket currently bind local source port `443`. The remote mock WebSocket remains `wss://mockapi.kiwoom.com:10000/api/dostk/websocket`; its remote port is independent of the local source port.

## Proposed design

Keep US HTTP at `443`; assign US WebSocket local source port `10002`:

| Path | Current | Proposed |
|---|---:|---:|
| `us_mock` REST HTTP | `443` | `443` |
| `us_mock` WebSocket | `443` | `10002` |

`10002` is distinct from the existing managed mock selections (`10000` KR HTTP and `10001` KR WebSocket). Before implementation, verify read-only that it is not used by an explicitly managed local service and is not Windows-excluded:

```powershell
Get-NetTCPConnection -LocalPort 10002 -ErrorAction SilentlyContinue
netsh interface ipv4 show excludedportrange protocol=tcp
netsh interface ipv6 show excludedportrange protocol=tcp
```

Also determine whether the `10001–65535` firewall/WFP blocked-range caveat documented for KR applies to US on this machine. If `10002` is blocked or reserved, stop for an operator decision; do not silently choose another port.

## Narrowness and preserved behavior

The implementation, if separately authorized, must change only the local source port passed to the US WebSocket `bind()` call. It must preserve:

- US HTTP local port `443`;
- remote HTTP endpoint and port;
- remote WebSocket URL and port `10000`;
- WebSocket `SO_REUSEADDR` behavior;
- HTTP/WebSocket retry and backoff behavior;
- account, strategy, order, quote, balance, and reconciliation logic; and
- real-account behavior and port selection.

No credentials, control state, market-hours gate, or order semantics may change.

## Relationship to Option 2

The port split is **still useful but not sufficient alone**. Option 2 addresses a genuine short-lived HTTP close/reconnect timing hazard. Port separation addresses a different structural hazard: the long-lived WebSocket holding the same local port as HTTP. Removing that sharing does not prove an old HTTP socket can never need deferred-close handling, so Option 2 must remain unchanged and may still be useful. Neither option is a complete fix without focused tests and separately authorized live evidence.

## V147 evidence supporting the hypothesis

V147 recorded:

- HTTP `CLOSE_WAIT` with WebSocket `ESTABLISHED` at approximately `08:17:25`, persisting through `08:18:54`;
- successful first follow-up balance cycle at `08:19:08`;
- failures at `08:22:08`, `08:25:09`, and `08:28:10`;
- each failure at `phase=connect`, local `('0.0.0.0', 443)`, remote `('112.175.65.18', 443)`, `errno=10048`/`winerror=10048`;
- no deferred-close timeout message; and
- WebSocket `ESTABLISHED` on local port `443` during and after the failure period.

Because the wait completed and the error surfaced at `connect()`, the evidence points away from the wait itself and is consistent with HTTP and WebSocket sharing local port `443`. This is a leading hypothesis, not proof; the verification plan below must test it.

## Operator risk assessment

In plain language, this gives the US mock worker two separate local doors: one for ordinary web requests and one for the price stream. The expected benefit is that the price stream cannot occupy the door needed by an HTTP reconnect.

If `10002` is already reserved or blocked, the WebSocket may fail to connect or reconnect, and the mock worker may lose price updates. A wrong port can produce another `10048`. The KR proposal's possible `10001–65535` exclusion must be checked for US rather than assumed to apply or not apply. No firewall, WFP, registry, or exclusion-range change is proposed.

## Verification plan

After separate implementation authorization, run focused tests confirming US HTTP=`443`, US WebSocket=`10002`, KR remains `10000`/`10001`, real-account behavior is unchanged, the WebSocket bind path preserves `SO_REUSEADDR` and remote port `10000`, and the existing HTTP close/rebind tests—including `test_delayed_close_loopback_churn_waits_before_each_rebind`—pass with every individual test reported.

A later, separate authorization is required for live verification. It must confirm a CLOSED US session, `kr_mock` stopped, auto-trading disabled, restart only `us_mock`, record timestamp/PID/instance, verify HTTP local `443` and WebSocket local `10002`, reproduce natural HTTP `CLOSE_WAIT` with WS `ESTABLISHED`, observe the next balance cycle, then observe at least two or three more complete cycles. Every cycle must include literal logs and netstat before/after; any `WinError 10048` stops the test for review. Final worker, control, socket, and git state must be reported literally.

## Explicit exclusions

This proposal does not touch `kr_real`, `us_real`, `kr_mock`, or live `us_mock`; change firewall/WFP/registry/TCP exclusions; change remote endpoints, credentials, account configuration, trading/order/quote/balance/retry behavior; revert or modify Option 2; change control state; authorize implementation, tests, restart, live traffic, staging, or commit.

## Decision requested

Approve or decline only this design direction: keep `us_mock` HTTP on local source port `443` and give its WebSocket a machine-verified distinct local source port, proposed as `10002`. Implementation, focused tests, and later closed-session restart-and-verify each require separate authorization.

## Not authorized to implement

This document is **not authorized to implement** the port separation. It authorizes no source edit, test run, worker restart, live traffic, firewall/WFP action, staging, or commit. Only this proposal document is produced in v148.
