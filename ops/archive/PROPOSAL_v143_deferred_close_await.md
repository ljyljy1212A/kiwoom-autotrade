# Proposal v143: Await Deferred Fixed-Port HTTP Close Completion

## Status and scope

Design proposal only. This document does not authorize source changes, worker actions, firewall or registry changes, staging, or commits.

The target is the post-Option-1 WinError 10048 mechanism documented in OPERATOR_RECORD_wrapup_v10-v138.md: an idle HTTP connection is retired after either local keepalive expiry or a remote-initiated disconnect, while the underlying asyncio socket close is still deferred.

## Confirmed current sequence

The current project path is in src/core/broker_http.py:

1. FixedPortAsyncHTTPTransport.__init__() creates an httpcore.AsyncConnectionPool with one connection and the accepted keepalive_expiry=30.0 setting.
2. The project _FixedPortAnyIOBackend.connect_tcp() calls _connect_with_reuseaddr(). That function performs the replacement socket's sock.bind((bind_address, local_port)) and then sock.connect(remote_address).
3. In the installed httpcore pool, AsyncConnectionPool._assign_requests_to_connections() detects connection.has_expired() and removes the old connection. It can create the replacement connection object in the same assignment pass.
4. AsyncConnectionPool._close_connections() then awaits the old connection's aclose() before the queued request calls the replacement's handle_async_request(). That request eventually reaches the project's _FixedPortAnyIOBackend.connect_tcp() and opens the replacement socket.

Both keepalive_expired and server_disconnected are returned by the same httpcore AsyncHTTP11Connection.has_expired() method and therefore use the same removal and aclose() path. The design must not add a gate only to the keepalive-expiry branch.

## Where close completion actually occurs

The current project wrapper is _LingerOnCloseByteStream.aclose() in src/core/broker_http.py. It sets the accepted v116 SO_LINGER(1, 0) option and delegates to AnyIO's stream close.

In the installed AnyIO asyncio backend, SocketStream.aclose() calls transport.close(), yields once, and calls transport.abort(). Those calls request transport shutdown; they do not themselves prove that the operating system socket handle has been closed.

In the installed Python asyncio selector transport, _SelectorTransport._force_close() schedules _call_connection_lost() with loop.call_soon(). The callback invokes the protocol's connection_lost() and then executes self._sock.close() in its finally block. Therefore the meaningful completion boundary is the return from _call_connection_lost(), after the socket close statement has run.

## Proposed narrow mechanism

Add a project-owned per-transport completion signal and make the fixed-port backend wait on it before opening a replacement socket.

1. In src/core/broker_http.py, add a small close-completion state object owned by _FixedPortAnyIOBackend or by each returned stream. It should contain an asyncio.Event initially set for a new connection, plus a reference to the original AnyIO protocol callback.
2. In _FixedPortAnyIOBackend.connect_tcp(), after anyio.abc.SocketStream.from_socket(raw_socket) creates the stream, attach a narrow completion hook to that stream's asyncio protocol. The hook must call the original connection_lost() first, then schedule the event to be set with loop.call_soon(). That scheduled event runs only after asyncio's _call_connection_lost() returns, including its self._sock.close(). The original callback and exception behavior must remain unchanged.
3. In _LingerOnCloseByteStream.aclose(), clear the completion event before delegating to the existing close operation. The close method must remain idempotent and must set or otherwise resolve the event if the stream is already closed or if setup fails before a callback can be installed.
4. In _FixedPortAnyIOBackend.connect_tcp(), await the previous fixed-port close-completion event immediately before _connect_with_reuseaddr(). The wait must be scoped to the same account/transport/local-port lifecycle, not a process-wide gate that serializes unrelated real-account traffic.
5. Use a bounded wait or an explicit failure path. If completion is not observed within the chosen safety timeout, do not proceed with a risky replacement bind; surface a clear connection failure and preserve the existing no-retry semantics for ambiguous order POSTs. A timeout must not silently open the new fixed-port socket while the old one may still exist.

The exact code should be limited to src/core/broker_http.py, principally _FixedPortAnyIOBackend.connect_tcp(), _LingerOnCloseByteStream.aclose(), and transport/backend state initialization. No change is proposed to src/core/realtime_feed.py, WebSocket close behavior, order logic, or the accepted v91/v103 instrumentation and port-selection changes.

## Why this covers both triggers

The event is cleared whenever the old stream begins its common aclose() path. That path is entered after httpcore reports either keepalive_expired or server_disconnected; the design does not inspect or reimplement has_expired(). The replacement backend waits on completion at the common socket-opening boundary, so both causes receive the same ordering guarantee.

## Relationship to v116

The proposed wait can coexist with the accepted v116 changes:

- v116 SO_LINGER(1, 0) changes the close signal from graceful FIN behavior toward abortive RST behavior and addresses the earlier TIME_WAIT pattern.
- v143 changes ordering by waiting until the deferred close callback and socket close have completed before a replacement attempts bind() or connect().

They address different parts of the sequence. The v143 design must not rely on SO_LINGER to signal completion, and it must not broaden the linger behavior to WebSockets or in-flight requests. v116 remains in the tree but is not extended, replaced, or treated as sufficient for the current CLOSE_WAIT failure.

## Operator-facing risks

The safest expected outcome is that a replacement request waits briefly for the old connection to finish closing, eliminating the collision. The main risk is a programming error that never signals completion: the worker could pause before making HTTP requests, repeatedly report connection failures, or appear frozen while waiting. If the timeout is too long, balance and quote refreshes could become stale; if it is too short, the original collision could remain. A faulty callback wrapper could also interfere with normal connection cleanup, so the original callback must always run exactly once and failures must fail closed rather than opening a second fixed-port socket.

This design does not intentionally change orders, trading controls, broker credentials, firewall policy, or real-account behavior. Nevertheless, a close-path bug could prevent balance, token, quote, or order HTTP requests from completing, which is why implementation and live verification must be separate approval steps.

## Verification plan after separate implementation authorization

### Focused tests

- Test that the completion event is initially available for a new stream.
- Test that both an ordinary close and a simulated connection_lost path resolve the event exactly once.
- Test that the backend waits before _connect_with_reuseaddr() and does not call it while the prior completion event is unresolved.
- Test timeout behavior: the backend fails closed and does not attempt the replacement bind after the timeout.
- Test both keepalive_expired and server_disconnected through the common pool cleanup path.
- Test that v116 linger remains limited to the HTTP stream and that WebSocket source-port and close behavior are unchanged.

### Isolated churn test

Use a local loopback server and repeated fixed-port HTTP-style connections. Force the old stream through pool retirement, delay the close callback, and require that the next connection does not call bind() until the delayed callback has completed. Require zero fixed-port bind failures and verify that the timeout path does not create a replacement socket prematurely.

### Live verification

After explicit authorization to restart and observe us_mock in a confirmed closed session, reproduce the v137 condition specifically: let the remote side idle-close the HTTP connection so the local socket reaches CLOSE_WAIT while the WebSocket remains established. Capture timestamps, HTTP/WS netstat states, close-completion evidence, and logs around the next balance request.

Require no WinError 10048, successful REST continuation, no unexplained connection-reset or request-loss evidence, and unchanged auto-trading and worker safety state. A test that exercises only the historical TIME_WAIT case is insufficient.

## Decision boundary

This document is NOT AUTHORIZED TO IMPLEMENT. The operator is being asked to approve or decline one narrow design: add a close-completion signal to the fixed-port HTTP stream, wait for that signal before opening the replacement fixed-port connection, fail closed on a bounded timeout, and verify both keepalive-expiry and remote-CLOSE_WAIT scenarios.

Approval of this design does not by itself authorize source editing, worker restart, live traffic, firewall changes, or a commit; those remain separate decisions.
