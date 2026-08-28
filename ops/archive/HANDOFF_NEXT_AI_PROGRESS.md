# Handoff for the Next AI: Fixed-Port Mock Worker Investigation

## Mission

Verify that the mock KR worker can use fixed local source port `10000` for HTTP
and WebSocket traffic concurrently, without recurring `WinError 10048`, while
also retaining a functioning scheduled balance monitor.

## Safety and scope

- Never touch `kr_real`, `us_real`, live credentials, live broker orders, or
  firewall/WFP configuration.
- Do not restart or modify `us_mock` unless a later handoff explicitly
  authorizes it. The latest PC reboot restarted it automatically; that was not a
  manual action in this task.
- Do not restart or stop a worker without explicit authorization.
- Preserve unrelated worktree changes and existing untracked handoff files.
- Do not reopen the separate quote-pipeline issue unless explicitly requested.

## Implemented commits

- `22263c9 Enable SO_REUSEADDR for fixed broker sockets`
  - Enables `SO_REUSEADDR` for the manually constructed fixed-port HTTP socket.
- `09f63fc Add fixed-port HTTP socket failure diagnostics`
  - Logs the socket phase, local/remote addresses, exception type, `errno`, and
    Windows error number.
- `2b3bcc1 Add account monitor iteration diagnostics`
  - Adds a debug line at the start of each scheduled balance-monitor iteration.
  - Diagnostic only; scheduling, locking, exception handling, and polling were
    not changed.

Verification after the code change:

```text
74 passed, 4 skipped
```

The file logger captures `DEBUG`; console logging remains `INFO`.

## Earlier evidence

Before iteration logging existed, both mock processes showed passive-monitor
startup but no successful monitor/reconciliation activity after their own
startup:

- Old `kr_mock` PID `1984` started at `06:50:42` and showed no successful
  monitor activity afterward.
- `us_mock` PID `14308` started at `06:50:42` and showed the same pattern.

This was initially parked as a pre-existing, cross-instance monitor issue.

## Authorized `kr_mock` restart and findings

One later handoff authorized a `kr_mock`-only restart:

- PID `5796` stopped gracefully.
- New PID `12008`, instance
  `6096b9ad45514fe5971d46b564fad063`.

During approximately 100 seconds of observation:

- HTTP `WinError 10048` recurred, for example:

  ```text
  Fixed-port HTTP socket failure: phase=connect local=('0.0.0.0', 10000) remote=('112.175.65.18', 443) exception=OSError errno=10048 winerror=10048: [WinError 10048] 각 소켓 주소(프로토콜/네트워크 주소/포트)는 하나만 사용할 수 있습니다
  ```

- The monitor-entry log appeared once at `interval=5s` and then stopped.
- WebSocket token/login/registration eventually succeeded.
- Final TCP state showed:

  ```text
  192.168.0.10:10000 -> 112.175.65.18:443   CLOSE_WAIT
  192.168.0.10:10000 -> 112.175.65.18:10000 ESTABLISHED
  ```

- HTTP and WebSocket were not both observed as `ESTABLISHED` in the available
  final snapshot. No intermediate TCP capture was saved, so a brief earlier
  HTTP `ESTABLISHED` state cannot be confirmed.

## Post-reboot evidence — current state

The PC was rebooted after the above investigation. Both mock workers were
automatically started by the existing startup mechanism:

- `kr_mock`: PID `10156`, instance
  `3d34fa17841346b2a076f37689c77f0e`
- `us_mock`: PID `10172`, instance
  `45acf6eeb59b4716a2c44d79ba5bf837`
- Both started at approximately `2026-08-19 08:01:25` local time.
- Both were responsive and had fresh heartbeats through approximately `08:04:06`.

The failure pattern repeated, but with one important difference: after reboot,
both monitors reached a second iteration.

For each worker the log sequence was:

1. Monitor iteration at startup, `interval=5s`.
2. Second monitor iteration at `08:01:31`, also `interval=5s`.
3. Fixed-port HTTP `10048` failures.
4. No later monitor iteration or deferred-failure log during the read-only
   observation.

Current fixed-port diagnostics:

- `kr_mock`: local `('0.0.0.0', 10000)` to remote `('112.175.65.18', 443)`;
  `errno=10048`, `winerror=10048`.
- `us_mock`: local `('0.0.0.0', 443)` to remote `('112.175.65.18', 443)`;
  `errno=10048`, `winerror=10048`.

The current `netstat` snapshot showed Telegram connections for both worker
PIDs, but no worker-owned fixed-port broker socket. This does not prove the
broker sockets never existed; it only describes the captured final snapshot.

## Current diagnosis

The event loop is not completely frozen: worker heartbeats continue advancing
and both Python processes remain responsive. The stronger current description
is that the monitor coroutine becomes stuck after its second iteration.

The missing non-blocking-socket hypothesis was checked and ruled out:

- `src/core/broker_http.py` calls `sock.setblocking(False)` after the blocking
  connect and before `anyio.abc.SocketStream.from_socket(raw_socket)`.
- Installed AnyIO is `4.14.2`.
- AnyIO's `from_socket()` delegates to the event loop and does not itself set
  non-blocking mode; the caller's explicit call is correctly positioned.

No safe live task introspection exists in the application. There is no debug or
admin endpoint, `faulthandler` task dump, or exposed lock-status API. No
invasive debugger was attached.

## Relevant monitor call chain

```text
run_account_balance_monitor()
  -> _refresh_runtime_control()                  # synchronous
  -> await sync_broker_state(force_balance=True)
  -> await self._sync_lock
  -> _reconcile_balance()
  -> await _shared_broker_balance()
  -> await _balance_gate.lock
  -> await client.get_balance()
  -> await _post() / token_mgr.get_token()
  -> await http_gate.client()
```

There is no single generic `self.lock` in this path. Relevant locks include the
per-engine `_sync_lock`, account-wide `_balance_gate.lock`, and later the HTTP
gate lock. Current evidence does not identify which lock or operation is
responsible.

The WebSocket task is intentionally long-lived: `PriceFeed.start()` creates a
persistent background task. Startup success logs do not expose whether that
task is currently pending or done.

## Recommended next step

Obtain explicit authorization before changing code or restarting anything.

The next minimal diagnostic should add debug lines immediately before and
after acquisition of:

1. `AccountEngine._sync_lock` in the passive monitor path; and
2. the original startup/balance synchronization path.

If required, instrument the shared account balance gate and HTTP gate in the
same diagnostic-only manner. Then perform one explicitly authorized restart
and observe whether the coroutine waits on `_sync_lock`, `_balance_gate.lock`,
the HTTP gate, or a later HTTP operation.

Do not change timeout, scheduling, lock, socket, or retry behavior as part of
that diagnostic. Run the full test suite and commit only verified changes.

## Current repository state

The source tree is clean. Existing untracked files are preserved:

- `HANDOFF_ACCOUNT_LOCK_BYPASS_NETWORK_DIAGNOSTICS.md`
- `HANDOFF_NEXT_AI_HTTP_WS_PORT_COLLISION.md`

## Diagnostic lock logging round 1 — completed 2026-08-19

The operator-authorized diagnostic-only change was implemented and verified.

Current worker state was rechecked before editing:

- `kr_mock`: PID `10156`, instance `3d34fa17841346b2a076f37689c77f0e`, `RUNNING`.
- `us_mock`: PID `10172`, instance `45acf6eeb59b4716a2c44d79ba5bf837`, `RUNNING`.

Source tracing confirmed that both startup synchronization (`AccountEngine.run()`)
and the scheduled monitor call `run_account_balance_monitor()` enter
`AccountEngine.sync_broker_state()`, whose first lock is `_sync_lock`. The
balance path then acquires the account-wide balance gate, followed by the HTTP
gate in `BrokerHTTPGate.client()`.

Diagnostic logging added:

- `src/core/engine.py`: a reusable diagnostic async-lock wrapper around
  `AccountEngine._sync_lock` and `balance_gate.lock`.
- `src/core/broker_http.py`: the same wrapper around `http_gate.lock` in both
  client acquisition and close paths.
- Each production log line is debug-level and includes lock name, state
  (`acquiring`, `acquired`, or `released`), asyncio task name, and an explicit
  UTC ISO-8601 timestamp. Test loggers without `debug()` preserve the original
  lock behavior without emitting diagnostics.

Verification:

```text
PYTHONPATH=C:\auto\작업7차\kiwoom-autotrade pytest -q -p no:cacheprovider
74 passed, 4 skipped, 5 warnings
```

No timeout, scheduling, lock, socket, retry, or other runtime behavior was
changed. No worker was restarted or stopped. `kr_real`, `us_real`, live
credentials, broker orders, and firewall/WFP configuration were not touched.

The next action requires explicit authorization: restart `kr_mock` only, then
observe the new lock diagnostics. Do not restart `us_mock` yet.
