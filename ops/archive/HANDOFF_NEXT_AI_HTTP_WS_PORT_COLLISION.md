# Handoff: Kiwoom Mock HTTP/ WebSocket Fixed-Port Collision

## Mission

Continue the Kiwoom trading-system work from the current verified state. The immediate problem is a Windows `WinError 10048` collision between the fixed-port broker HTTP transport and the fixed-port WebSocket transport in the mock workers.

This handoff is an instruction document for the next AI. Distinguish these instructions from any later user request.

## Repository and production constraints

- Repository: `C:\auto\작업7차\kiwoom-autotrade`
- Production target: native Windows.
- `kr_real` and `us_real` are out of scope and must remain untouched.
- Do not change firewall/WFP configuration.
- Do not investigate the watchdog task result `1` or Telegram task result `2` in this task; they were explicitly deferred.
- Preserve unrelated dirty or untracked files, especially `HANDOFF_ACCOUNT_LOCK_BYPASS_NETWORK_DIAGNOSTICS.md` if present.
- Use surgical changes only. Do not refactor unrelated code.
- Do not claim live success without fresh process, log, heartbeat, and port evidence.
- Avoid duplicate workers. Before any manual launch, verify the scheduled-task process and PID identity.

## Work already completed

### Existing WebSocket repair

The WebSocket path was previously changed to use `sock=` rather than the unsupported `local_addr=` argument. The relevant code is in `src/core/realtime_feed.py`.

### HTTP fixed-port implementation

Committed in:

```text
f72ca75 fix: bind mock broker HTTP source ports
```

Files in that commit:

- `src/core/broker_http.py` — custom `httpcore`/AnyIO backend binds HTTP sockets to a fixed local port and serializes requests with an account/worker-scoped lock.
- `src/core/kiwoom_client.py` — mock KR uses local port `10000`; mock US uses local port `443`; all REST calls use the shared gate.
- `src/core/token_manager.py` — token issuance and revoke use the same gate; the mock HTTP client is persistent so sequential requests reuse the fixed-port connection instead of creating repeated `TIME_WAIT` sockets.

Covered broker HTTP operations include token issuance, revoke, balance, execution history, cancellation, quote, and order requests. Telegram and Discord HTTP paths were not changed.

### Verification already completed

- Full test suite with `PYTHONPATH=.`: `74 passed, 4 skipped`.
- Staged diff check passed.
- Standalone concurrent fixed-port HTTP test passed after correcting an initial TIME_WAIT/reconnect issue by keeping the HTTP client persistent under the lock.
- Port-mode guard passed: KR mock → `10000`, US mock → `443`, real → no fixed port.
- No workers were restarted during implementation or diagnosis.

## Reboot/runtime evidence

The PC was rebooted and automatic startup launched both mock workers.

Current observed workers:

- `kr_mock`: PID `1984`, command `python.exe -m src.main --market KR`, created `2026-08-19 06:50:37`.
- `us_mock`: PID `14308`, command `python.exe -m src.main --market US`, created `2026-08-19 06:50:37`.

Scheduled tasks:

- `Kiwoom Worker - KR Mock`: last run `06:50:50`, result `0`.
- `Kiwoom Worker - US Mock`: last run `06:50:50`, result `0`.

Port ownership at diagnosis time:

```text
192.168.0.10:10000 -> 112.175.65.18:443  CLOSE_WAIT  PID 1984
192.168.0.10:443   -> 112.175.65.18:443  CLOSE_WAIT  PID 14308
```

The status JSON files were current through approximately `06:55`, but the global `data/heartbeat.txt` was stale and must not be treated as sufficient worker proof by itself.

## Confirmed root cause

This is an active socket collision, not a `TIME_WAIT` collision.

Observed log ordering for both workers:

1. Startup initially failed DNS resolution with `[Errno 11001] getaddrinfo failed` while networking was coming up.
2. At `06:52:13`, token issuance succeeded.
3. Immediately afterward, WebSocket connection attempts began failing with `WinError 10048`.
4. `netstat` showed the fixed HTTP local port still occupied by the worker in `CLOSE_WAIT`.

The HTTP gate serializes HTTP requests, but the mock HTTP client is intentionally persistent:

```python
self._client = httpx.AsyncClient(timeout=timeout, transport=transport)
```

The HTTP transport binds the fixed port through:

```python
stream = await anyio.connect_tcp(
    remote_host=host,
    remote_port=port,
    local_host=local_address or "0.0.0.0",
    local_port=self.local_port,
)
```

The WebSocket path then tries to bind the same port independently:

```python
sock = await asyncio.to_thread(
    socket.create_connection,
    (parsed.hostname, remote_port),
    timeout=10,
    source_address=("0.0.0.0", local_port),
)
```

The sequence is therefore:

1. `realtime_feed._connect_once()` obtains a token through HTTP.
2. The HTTP response completes, but the persistent HTTP connection remains held on the fixed local port.
3. `_prebound_ws_socket()` attempts to bind that same local port.
4. Windows rejects the second active bind with `WinError 10048`.

Neither socket-creation site explicitly sets `SO_REUSEADDR` or `SO_EXCLUSIVEADDRUSE`. Adding `SO_REUSEADDR` alone is not considered sufficient because the collision is with an actively held socket, not merely a `TIME_WAIT` socket, and Windows reuse semantics differ from POSIX.

## Proposed next fix — not yet implemented

Introduce one account/worker-scoped fixed-port arbiter shared by both HTTP and WebSocket:

- HTTP acquires the arbiter for the complete request lifecycle.
- WebSocket acquires the same arbiter before `_prebound_ws_socket()` and holds it through the entire WebSocket session, releasing it when the session closes.
- Ensure the WebSocket socket is closed on every failed connection path.
- Keep the existing KR/US port assignments unless the user explicitly approves a different design.

This may block REST calls while a WebSocket session is active. A separate-port design would avoid that but would change the current fixed-port requirement and therefore needs explicit approval.

Do not implement the fix until the user requests implementation or provides equivalent authorization.

## Required verification after implementation

Run the automated suite with:

```powershell
$env:PYTHONPATH='.'; pytest -q
```

Expected baseline: at least `74 passed, 4 skipped`, subject to unrelated test changes.

For live verification, do not manually start a second worker. First inspect the automatically managed process and task. The expected evidence is:

- No `WinError 10048` in fresh `kr_mock` or `us_mock` logs.
- Token issuance succeeds.
- WebSocket login succeeds and remains connected or reconnects without fixed-port bind errors.
- HTTP continues to work through the shared arbiter.
- KR owns local port `10000`; US owns local port `443`.
- Fresh status/heartbeat evidence and PID identity match the active workers.

Any worker restart requires explicit user authorization unless the user clearly authorizes automatic-start validation as the restart event.

## Current repository state

The HTTP implementation commit is `f72ca75`. The diagnostic work made no source changes and no runtime changes after that commit. The only expected unrelated untracked handoff file is:

```text
HANDOFF_ACCOUNT_LOCK_BYPASS_NETWORK_DIAGNOSTICS.md
```

