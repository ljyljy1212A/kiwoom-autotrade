# Proposal v116: Fixed-Port HTTP Close Behavior and KR Shared-Port Evaluation

## Status

Design proposal only. No source implementation, control-file change, worker
restart, firewall/WFP change, live test, or commit is authorized by this
proposal.

`kr_mock` must remain stopped, and `us_mock` must remain untouched, until a
separate implementation and restart-and-verify decision is made.

## Evidence and root cause

The fixed-port HTTP transport creates a raw socket, sets `SO_REUSEADDR`, binds
the selected local source port, and connects to the broker:

```python
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind((bind_address, local_port))
sock.connect(remote_address)
```

The transport uses an `httpcore.AsyncConnectionPool` with one connection and a
30-second keepalive expiry. When the long-lived WebSocket continues to hold
the same local source port, a closed HTTP connection can leave the port in
`TIME_WAIT`; the next HTTP bind then fails with `WinError 10048`.

The v114 isolated loopback test showed that setting `SO_LINGER` to
`(1, 0)` on the short-lived socket before close avoided `TIME_WAIT` and
allowed all 15 close/rebind iterations to succeed. This is diagnostic evidence
only, not live-trading verification.

## Part 1 — `us_mock` HTTP close behavior

### Proposed change

Add narrowly scoped `SO_LINGER(1, 0)` handling to the fixed-port HTTP
connection close path used when the HTTP connection pool expires or replaces a
connection. The intended behavior is to prevent the short-lived HTTP socket
from leaving a local-port `TIME_WAIT` entry that blocks the next fixed-port
HTTP connection while the WebSocket remains established.

### Exact implementation targets

The implementation review must focus on these existing locations in
`src/core/broker_http.py`:

- `_connect_with_reuseaddr()` around lines 99–139, where the raw fixed-port
  socket is created and failed connection attempts currently call `sock.close()`
  at line 136.
- `_DiagnosticByteStream.aclose()` around lines 74–75 and the
  `AnyIOStream.from_socket(raw_socket)` handoff around lines 164–175, because
  the normal pool-expiry close is performed through the wrapped HTTP stream,
  not only through the failed-connect cleanup branch.
- `FixedPortAsyncHTTPTransport` around lines 178–192, which owns the
  single-connection pool and its `keepalive_expiry=30.0` policy.

The implementation must identify the pool-triggered close/rebind lifecycle
explicitly. It must not merely add a broad linger option to every socket close
without proving which close path is being changed.

### WebSocket exclusion

Do not apply this proposal's HTTP linger behavior to
`src/core/realtime_feed.py`. The WebSocket socket is long-lived and is not the
short-lived connection-pool socket that produces the observed close/rebind
churn. Applying RST-based closure broadly to WebSocket shutdown would add an
unnecessary abrupt-close behavior under different lifecycle conditions.

### Risk and order-submission impact

`SO_LINGER(1, 0)` changes close from graceful FIN behavior to an RST-style
abort. If data is in flight, the remote endpoint may observe a connection
reset or lose unsent data.

The current order path has an important asymmetry:

- `place_order()` calls `_post_once()` directly with
  `allow_reauth_retry=False`; it does not use the generic `_post()` retry
  decorator.
- A network error becomes `RetryableError`, and the engine logs `Order failed`
  and drops that attempt without creating a pending-order ledger row.
- `cancel_order()` calls `_post()`, so it receives the generic three-attempt
  retry behavior.

The observed `WinError 10048` is a connect-phase bind/connect failure, not a
mid-flight close. Therefore the current lost-order-attempt risk already exists
when the fixed-port bind fails; adding linger to the pool-expiry close does not
create that specific connect-phase failure. Nevertheless, an RST could create
an additional remote-side reset risk if it is applied while an HTTP request is
actively transmitting. Implementation must therefore prove that the linger
setting is applied only to the intended idle-pool expiry/rebind close path, not
to an in-flight request.

### Verification plan for Part 1

After separate implementation authorization:

1. Run focused unit tests for socket-close behavior and ensure no WebSocket
   source or close behavior changes.
2. Run an isolated loopback churn test with a held socket and repeated
   fixed-port HTTP-style connections; require zero bind failures and no
   `TIME_WAIT` entries for the reused local port.
3. Verify the transport still reports and propagates genuine connect errors.
4. Obtain a separate explicit decision before any `us_mock` restart or live
   observation. Do not restart it as part of this proposal.
5. During separately authorized runtime verification, confirm balance and
   quote requests succeed across multiple pool-expiry intervals and inspect
   for `10048`, connection-reset, and request-loss evidence.

## Part 2 — `kr_mock` shared local port 10000

### Proposed conditional design

Only after Part 1 is implemented and verified on the lower-risk `us_mock`
case, evaluate restoring KR WebSocket local port `10000`, so KR HTTP and KR
WebSocket use the same firewall-permitted local source port to different remote
endpoints. KR HTTP must receive the same narrowly scoped fixed-port HTTP close
behavior proposed in Part 1 before this design is considered viable.

The exact source change would be limited to the KR branch of
`src/core/realtime_feed.py`'s `ws_local_port` property, currently around lines
120–126. No remote WebSocket URL or remote port would change. No US branch
would be changed by this KR proposal.

### Closed alternatives

- Restoring KR WebSocket to local port `443` is not supported by this
  repository's tracked history: the root version used OS-assigned ephemeral
  ports, and the first explicit pinning commit assigned KR `10000`, not `443`.
  It would also share local `443` with `us_mock`'s HTTP and WebSocket paths.
- Choosing a third local port such as `10001` or `10101` is blocked by the
  current firewall/WFP source-port policy for the broker host. The blocked
  range includes `10001–65535`; changing that policy is outside this proposal.

### Risks and dependencies

This part is higher risk because it changes the blocked KR worker's socket
topology and relies on the Part 1 close behavior to survive HTTP connection
churn while the KR WebSocket remains bound.

The v112 basic loopback test showed simultaneous same-port connections to
different remote endpoints can succeed. The v113 churn test showed ordinary
FIN closure fails on the next bind with `WinError 10048`; the v114 linger test
showed RST closure avoids that specific loopback failure. Neither result is
live broker verification.

### Verification plan for Part 2

After Part 1 is accepted and after separate implementation authorization:

1. Make only the KR WebSocket local-port selection change described above.
2. Run focused tests confirming KR HTTP and WebSocket both select `10000`,
   while US selections and real-account behavior remain unchanged.
3. Run isolated socket and HTTP-transport churn tests before touching a worker.
4. Obtain a separate explicit authorization to restart `kr_mock`; do not
   restart it as part of this proposal.
5. Verify KR HTTP requests, WebSocket login/subscription, balance
   reconciliation, and repeated HTTP pool-expiry cycles.
6. Require no repeated `WinError 10048`, no unexplained connection resets,
   fresh broker balance evidence, and correct worker identity/heartbeat before
   accepting the result.
7. Commit only after implementation and separately authorized runtime evidence
   are accepted.

## Explicitly not proposing in this document

This proposal does not propose:

- changing `place_order()` retry behavior;
- adding retries to ambiguous order POSTs;
- changing cancellation or reconciliation semantics;
- changing auto-trading control state or market-hours gates;
- changing remote broker ports, credentials, or account configuration;
- changing firewall/WFP or port-exclusion policy;
- changing WebSocket close behavior;
- touching `kr_real` or `us_real`;
- restarting `kr_mock` or `us_mock`;
- implementing either part; or
- staging or committing this proposal.

## Required decision sequence

1. Operator reviews this proposal.
2. Operator separately authorizes Part 1 implementation and focused tests.
3. Part 1 is verified without an automatic worker restart.
4. Operator separately decides whether to authorize `us_mock` restart-and-
   verify, respecting the existing worker safety gates.
5. Only after Part 1 evidence is accepted, operator decides whether to
   authorize the conditional KR shared-port implementation.
6. `kr_mock` restart-and-verify remains a separate explicit decision.
7. Commit remains a final separate checkpoint.

