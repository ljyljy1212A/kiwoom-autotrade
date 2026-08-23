# Operator Record Wrap-up v10–v138

## 1. Executive summary

`WinError 10048` remains unresolved.

Option 1 widened the closed-session balance-monitor cap from 60 seconds to
180 seconds. The change was implemented, focused-tested, full-tested, and
live-verified. It did not fix the problem. It changed the observed pattern
from intermittent failures under the old cadence to highly repeatable
transient failures after the longer idle interval.

The root-cause understanding has been revised, rather than merely extended.
The earlier `TIME_WAIT`/cadence mechanism (H5) correctly explained the
pre-Option-1 pattern. Post-Option-1 live evidence instead shows a different
operative mechanism: the remote side closes an idle HTTP connection, leaving
the local HTTP socket in `CLOSE_WAIT`; httpcore then lazily retires that
connection while the project's asynchronous transport close remains
deferred (server-initiated disconnect through H4). A replacement fixed-port
connection can collide during that close sequence.

This server-disconnect form of H4 is now strongly evidenced and has a fully
traced source call chain. It is no longer only a structurally possible race
as it was in v127.

No real-account worker was started or changed. `kr_mock` remains stopped,
`us_mock` remains running with auto-trading disabled, and the source change
remains unstaged.

## 2. Fully confirmed mechanism

### Remote idle disconnect

Existing v136/v137 timestamps bracket the remote close. A successful REST
cycle completed at approximately 06:58:42. Netstat still showed
`local:443 -> remote:443 ESTABLISHED` at 06:59:53, and showed
`CLOSE_WAIT` by 07:00:03. The remote FIN was therefore bracketed to roughly
71–81 seconds after the previous successful request. The exact FIN instant
was not directly observed.

This estimate is from v138 and should not be treated as a precise protocol
timeout value.

### Lazy detection through `has_expired()`

In the installed httpcore source, `AsyncHTTP11Connection.has_expired()`
computes:

```python
keepalive_expired = self._expire_at is not None and now > self._expire_at
server_disconnected = (
    self._state == HTTPConnectionState.IDLE
    and self._network_stream.get_extra_info("is_readable")
)
return keepalive_expired or server_disconnected
```

An idle socket made readable by the remote FIN therefore takes the
`server_disconnected` branch. The v137 `CLOSE_WAIT` observations strongly
support this branch, although the runtime does not currently emit a
branch-specific label.

### Identical cleanup path

The two `has_expired()` causes do not diverge after the boolean result. The
call chain is:

```text
AsyncHTTP11Connection.has_expired()
  -> AsyncConnectionPool._assign_requests_to_connections()
  -> remove connection from pool and append to closing_connections
  -> AsyncConnectionPool._close_connections()
  -> await connection.aclose()
  -> AsyncConnection.aclose()
  -> AsyncHTTP11Connection.aclose()
  -> network_stream.aclose()
  -> project _LingerOnCloseByteStream.aclose()
  -> SO_LINGER(1, 0)
  -> wrapped AnyIO stream aclose()
```

The project SO_LINGER wrapper is reached for both triggers. Therefore the
wrapper is not being bypassed. SO_LINGER is not a remedy for the observed
pre-close `CLOSE_WAIT` state; it only takes effect when the project reaches
its close wrapper.

### Deferred socket close

The installed AnyIO asyncio backend closes the transport with this sequence:

```python
self._transport.write_eof()
self._transport.close()
await sleep(0)
self._transport.abort()
```

The underlying asyncio transport schedules the actual callback with
`loop.call_soon(self._call_connection_lost, ...)`. The socket's
`self._sock.close()` is inside `_call_connection_lost`, so the OS-level close
is deferred rather than synchronous. `abort()` also schedules that callback
through `_force_close()`.

Consequently, a replacement fixed-port connection can reach `bind/connect`
before the deferred close callback has completed. The live v137 evidence
showed the HTTP side in `CLOSE_WAIT` while the WebSocket side remained
`local:443 -> remote:10000 ESTABLISHED`, followed by the fixed-port
`WinError 10048` and then successful REST I/O after cleanup progressed.

### Effect of the 180-second interval

The 180-second cap reliably exceeds the observed roughly 71–81-second remote
idle-close interval. The next balance poll therefore generally encounters a
server-disconnected idle HTTP socket rather than sometimes arriving before
the remote close. This explains why the transient failure became more
consistent after Option 1 instead of disappearing.

What remains not fully proven is whether the continuously established
WebSocket sharing local port 443 is itself causally required, or whether the
deferred close race alone is sufficient to explain all failures. The shared
port is strongly implicated by the simultaneous netstat state, but this
round did not isolate those variables.

## 3. Historical mechanisms

These findings supersede the historical mechanisms for the current symptom;
they do not make the earlier observations false.

- H1/H3: setting SO_LINGER before `write_eof()`/`SHUT_WR` remains an accurate
  description of why SO_LINGER does not prevent TIME_WAIT on a
  client-initiated close. It is not the operative explanation for the
  current remote-initiated `CLOSE_WAIT` failures.
- H5: the approximately 121–126-second TIME_WAIT observation colliding with
  the old approximately 60–64-second cadence correctly explained the
  pre-Option-1 intermittent pattern. It is historical for the current
  post-Option-1 symptom.

## 4. Remediation options and status

### Option 1 — widen closed-session interval

Status: IMPLEMENTED, TESTED, LIVE-VERIFIED, CONFIRMED INSUFFICIENT.

Literal source diff:

```diff
-    return min(60.0, _seconds_until_next_regular_open(monitor.calendar))
+    return min(180.0, _seconds_until_next_regular_open(monitor.calendar))
```

Verification:

- Focused realtime-feed tests: 2 passed.
- Full suite: 83 passed, 4 skipped.
- v136 live verification: five closed-session cycles over 15 minutes;
  transient fixed-port failures recurred, while REST cycles ultimately
  completed.
- v137 live netstat: failures occurred with HTTP `:443` in `CLOSE_WAIT` and
  WS `:10000` in `ESTABLISHED`, not HTTP `TIME_WAIT`.

### Option 2 — fix the close race

Status: NOT IMPLEMENTED. This is now the best-evidenced candidate.

An implementation would need to ensure that the deferred socket close has
actually completed, or is explicitly awaited to completion, before a
replacement connection attempts to bind/connect on the same local port. The
behavior must cover both `keepalive_expired` and `server_disconnected`,
because both triggers share the same downstream close path.

This is a real code change with re-verification risk, not a trivial cleanup.
It requires explicit authorization, a surgical implementation, focused
tests, and live verification against a `CLOSE_WAIT`-triggered cycle.

### Option 3 — firewall change

Status: NOT IMPLEMENTED; lowest priority and unaffected by the v138 source
trace. No firewall changes are authorized by this record.

The previously floated proactive-cleanup idea from v139 can be dropped or
deprioritized. The evidence now points to the close race itself, not merely
to lazy versus proactive expiry checking.

## 5. Round-by-round history, v119–v138

- v119–v124: established the fixed-port HTTP/WS collision context, observed
  `WinError 10048`, recorded socket-close behavior, and preserved the
  Windows live/mock safety boundaries.
- v125: statically traced the close order through SO_LINGER, AnyIO
  `write_eof()`, `SHUT_WR`, transport close, and abort; runtime socket
  identity was not yet measured.
- v126: confirmed AnyIO `write_eof()` calls `socket.shutdown(SHUT_WR)` and
  that transport close eventually reaches socket close.
- v127: confirmed deferred asyncio `call_soon()` connection-loss cleanup and
  identified H4 as structurally possible, without causal proof.
- v128: traced httpcore `has_expired()` and distinguished keepalive expiry
  from server-readable/EOF detection.
- v129: measured the old retry/poll timing and identified the likely
  TIME_WAIT/cadence interaction; the registry timeout was not set.
- v130: found `kr_mock` unexpectedly running after reboot and began the
  safety investigation.
- v131: stopped `kr_mock` gracefully and measured an approximately
  121–126-second TIME_WAIT observation under the old conditions.
- v132: synthesized the historical H5 explanation and recorded the
  candidate options.
- v133: created `OPERATOR_RECORD_wrapup_v10-v132.md` as the prior reference
  point.
- v134: implemented only Option 1, changing the closed-session cap from 60
  to 180 seconds; focused and full tests passed.
- v136: restarted only `us_mock` during a confirmed closed session and ran
  the required 15-minute passive verification. The longer cadence produced
  recurring transient fixed-port failures.
- v137: extracted the complete cycle record and captured 40 live netstat
  snapshots. At failure, HTTP was `CLOSE_WAIT` while WS remained
  `ESTABLISHED`; no HTTP `TIME_WAIT` was present.
- v138: traced the server-disconnect branch through the identical cleanup
  path, confirmed the deferred socket close, and estimated the remote idle
  close at roughly 71–81 seconds.

The v134→v136→v137→v138 arc is therefore: implement the longer interval →
live-test it → discover `CLOSE_WAIT` rather than `TIME_WAIT` at failure →
trace the exact server-disconnect and deferred-close call chain.

## 6. Safety-relevant event log

During v129–v131, `kr_mock` was found running unexpectedly after a reboot,
despite the intended stopped state. The process was stopped through the
normal supervisor path after status verification. A passive netstat capture
then confirmed the old TIME_WAIT behavior. The event remains a safety
boundary: do not assume a stopped status file alone proves the worker is
absent; verify supervisor state and process identity independently.

No real-account worker was started. `kr_real` and `us_real` remain out of
scope.

## 7. Current verified state

Verified at the start of v140 before this document was written:

- `us_mock`: RUNNING, PID 6644, instance
  `0d5945c672f7465191e37809c70e891f`.
- `kr_mock`: STOPPED, recorded PID 9172 not running.
- `data/control/us_mock.control.json`: `auto_trading_enabled` is `false`.
- `src/main.py:287`: the unstaged `60.0`→`180.0` change is present.
- Existing modified and untracked worktree state was preserved.
- No source, worker, firewall, registry, or control-file changes were made
  by v140.

## 8. Instructions for the next AI

- `WinError 10048` remains open. Option 1 alone does not close it.
- Do not implement Option 2 without explicit authorization.
- Any Option 2 attempt must be surgical, tested first with focused tests,
  and live-reverified with a `CLOSE_WAIT`-triggered cycle rather than only a
  generic reconnect.
- Keep `kr_mock` stopped and confirm that state in every session.
- Keep `us_mock` auto-trading disabled unless separately authorized.
- Keep `kr_real` and `us_real` entirely out of scope.
- Preserve unrelated dirty and untracked worktree state.

## 9. Read-only verification checklist

Before any future implementation:

```powershell
git status --short
git diff --stat
git diff -- src\main.py
python -m src.worker_supervisor status --account kr_mock --market KR
python -m src.worker_supervisor status --account us_mock --market US
Get-Content data\control\us_mock.control.json -Raw
```

For any authorized Option 2 implementation, require all of the following:

1. Confirm `kr_mock` is stopped and `us_mock` is the expected PID.
2. Confirm `auto_trading_enabled` remains false.
3. Make only the explicitly authorized source change.
4. Run focused close/rebind tests and the full relevant test suite.
5. Verify no unrelated worktree changes were introduced.
6. Restart only under explicit authorization and only in a closed session.
7. Capture cycle timestamps, HTTP/WS netstat states, and literal failure or
   success logs around a `CLOSE_WAIT`-triggered cycle.
8. Leave real-account workers and shared firewall controls untouched.

