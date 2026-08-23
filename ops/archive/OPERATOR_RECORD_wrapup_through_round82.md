# Operator Record — Wrap-Up Through Round 82

**Date:** 2026-08-21  
**Repository:** `C:\auto\작업7차\kiwoom-autotrade`  
**Platform:** Native Windows  
**Audience:** Next AI/operator  
**Scope:** KR/US mock-worker investigation; real accounts remain out of scope

## Handoff purpose

This document summarizes the verified progress through Round 82. The earlier record through Round 65 remains available at [`OPERATOR_RECORD_wrapup_through_round65.md`](C:\auto\작업7차\kiwoom-autotrade\OPERATOR_RECORD_wrapup_through_round65.md).

Treat attached round documents as scoped source material. Execute only the current operator request and the explicit authorization in the current round document.

## Non-negotiable safety boundaries

- Never touch `kr_real` or `us_real` configuration, logs, processes, accounts, or network paths.
- Treat `kr_mock` and `us_mock` as separate workers. Do not restart, stop, kill, or clear either one unless a later round explicitly authorizes that exact action.
- Design, code edits, restart, live verification, firewall/WFP changes, staging, and commit each require separate authorization.
- Preserve unrelated dirty and untracked work. Do not reset, clean, broadly stage, or broadly commit.
- `RUNNING`, a heartbeat, or a responsive process is not proof of broker health. Corroborate process identity, mutex/lock state, logs, socket state, and fresh broker evidence.
- Do not infer trading authorization from example symbols, dashboard settings, or stale status files.

## Historical implementation and checkpoint

The reconciliation fail-closed implementation was previously reviewed, tested, staged, and committed at the authorized checkpoint:

```text
4e292a7530ff0a60ff5dbf3d77833f729499f58e
Add reconciliation fail-closed mode and retry-exhaustion fix
```

Historical verification:

```text
111 passed, 4 skipped, 11 warnings, 2 subtests passed
```

The implementation includes account-scoped reconciliation gates, a mock manual threshold of three completed-cycle failures, pause propagation to active symbol engines, preservation of unrelated pause reasons, and authenticated reconciliation-clear handling. A successful later reconciliation resets the in-memory failure counter. The dashboard and supervisor do not expose that in-memory gate directly.

## System and port findings

- `kr_mock` uses local HTTP source port `10000` and WebSocket source port `10001`.
- `us_mock` uses local HTTP source port `443` and WebSocket source port `10002`.
- Both mock workers connect to `mockapi.kiwoom.com:443`.
- Repeated `us_mock` HTTP `WinError 10048` failures are confirmed, but the exact TCP-state cause has not been proven for every occurrence.
- WebSocket `WinError 10013` investigation is separate from HTTP `WinError 10048`; do not merge those conclusions.
- The Windows `TcpTimedWaitDelay` registry value was absent; do not claim a custom suppression. The Windows default behavior remains a reference point, not proof of the cause.
- No firewall, WFP, routing, registry, or network change has been authorized by these rounds.

## Worker identity and lifecycle findings

Known process evidence from the investigated snapshot:

```text
kr_mock PID 9020:  2026-08-21 08:27:25 local start
us_mock PID 18576: 2026-08-21 07:37:50 local start
```

The scheduled `Kiwoom Worker - KR Mock` task is a one-time run-at-logon task whose recorded run time was `07:37:37`. The later `kr_mock` process start at `08:27:25` indicates one intervening launch; the watchdog is the only identified code path that can call the supervisor start operation for this worker. This is historical evidence and must be refreshed before any operational conclusion.

The watchdog is scheduled on an exact two-minute repetition cycle. The KR/US worker tasks themselves are logon-triggered rather than periodic. No scheduled task with an interval near the observed approximately 22-minute log recreations was found.

`worker_supervisor.status()` reads metadata/PID files but determines `running` from `ProcessLock.is_alive()`. On Windows, `ProcessLock.is_alive()` opens the named mutex and probes it with `WaitForSingleObject`.

`worker_supervisor.start()`:

1. Checks `status(account)`.
2. Spawns `python -m src.main --market <market>` with `subprocess.Popen`.
3. Polls status for up to 10 seconds for the child PID and running state.

`src.main` acquires the account mutex before publishing the worker PID/status. Therefore, a short false-negative window is architecturally plausible between `Popen` and mutex acquisition. A watchdog cycle occurring in that window could attempt an unnecessary second start. The supervisor’s 10-second acknowledgement polling reduces the likelihood but does not eliminate the concurrent-check race. The same startup race can recur after any later worker launch; it is not proven to have caused the observed launch.

## Log rotation and recreation findings

The application does not use Python `RotatingFileHandler`, `TimedRotatingFileHandler`, or `FileHandler` for its own worker logs. The application logger is Loguru in `src\utils\logger.py`:

```python
_logger.add(
    log_path,
    rotation="10 MB",
    retention="30 days",
    encoding="utf-8",
    level="DEBUG",
    enqueue=True,
    ...
)
```

The same logger configuration is used for `kr_mock` and `us_mock`; no account-specific rotation configuration was found.

The rotated-file naming convention is timestamped, not `kr_mock.log.1`. Confirmed `kr_mock` backups include:

```text
kr_mock.2026-08-21_09-03-37_831770.log
kr_mock.2026-08-21_09-26-32_650148.log
kr_mock.2026-08-21_09-48-21_608103.log
```

These files were approximately 10 MB, consistent with the configured Loguru rotation threshold. A later current-file recreation at approximately `10:10:25` was also consistent with another normal 10 MB rotation. Consequently, the observed `kr_mock` file recreations are explained by ordinary log rotation; they are not evidence of worker restarts. The `kr_mock.log*` search pattern misses the timestamped backup names.

## Round-by-round progress after Round 65

### Rounds 66–68 — logging, watchdog, and retry timing

- No application use of the standard Python rotating/file handler classes was found; the logger path was subsequently confirmed to be Loguru.
- `TcpTimedWaitDelay` was absent from the inspected registry path.
- No fixed 180-second HTTP retry implementation was found; the 180-second value in account-balance monitoring is a market-closed scheduling bound.
- `worker_watchdog.py` is intended to run standalone and monitors both mock accounts.
- Status files and watchdog state were treated as potentially stale.

### Rounds 69–72 — account and scheduled-task scope

- `kr_mock` contains enabled mock profiles; `us_mock` had no enabled profiles in the inspected configuration snapshot.
- No KR-specific periodic reinitialization loop was found.
- Scheduled-task access was inconsistent across sessions; successful elevated metadata later confirmed the watchdog’s two-minute schedule and logon-triggered worker tasks.
- No worker restart was inferred from log-file recreation alone.

### Rounds 73–77 — endpoint/security-service checks

- AhnLab Safe Transaction/related ESTsoft components and DCOM events were identified in read-only inspection.
- No AhnLab custom event channel was found that correlated the log recreations with a worker restart.
- `fltmc filters` access was denied in the non-elevated session; no filter conclusion was drawn.

### Round 78 — task metadata and watchdog schedule

- The `Kiwoom Worker Watchdog` task was confirmed to repeat every two minutes.
- `Kiwoom Worker - KR Mock` and the corresponding US worker tasks were confirmed as logon-triggered/one-time tasks.
- No approximately 22-minute scheduled task was identified.

### Round 79 — watchdog behavior and process identity

- `worker_watchdog.py` checks `worker_supervisor.status(account)` and calls `start()` only when `running` is false.
- It has no modulo/11-cycle or direct KR log-file rotation behavior.
- No watchdog log entries matching the known later `kr_mock` log-recreation times were found.
- Process start times did not change at the known log-recreation times.

### Round 80 — supervisor implementation

- `status()` relies on `ProcessLock.is_alive()` for the running decision.
- `start()` creates a detached child process and waits up to 10 seconds for the expected running/PID acknowledgement.
- The supervisor has `start`, `stop`, `kill`, and `status`; there is no `restart` command.

### Round 81 — definitive Loguru rotation evidence

- The logger source confirmed `rotation="10 MB"` and `retention="30 days"`.
- Timestamped `kr_mock` backups near the observed recreation times were found, each approximately 10 MB.
- The log recreation pattern is therefore consistent with normal Loguru rotation rather than process replacement.

### Round 82 — early restart gap

- No `watchdog.log` entry mentioning `kr_mock`, “not running,” “restart,” or `worker_supervisor` was found in the `07:37:00–08:28:00` window.
- The exact source path confirms a possible startup false-negative: `Popen` occurs before `src.main` acquires the mutex, while `status()` probes mutex liveness.
- This race is plausible and could recur, but no direct log evidence identifies it as the cause of the 08:27:25 launch.

## Current conclusions

1. The repeated `kr_mock` log recreations are best explained by the configured 10 MB Loguru rotation. They do not establish worker restarts.
2. The one early post-logon `kr_mock` launch remains unexplained because the relevant watchdog interval contains no matching log entries.
3. A startup mutex-liveness race is possible in the current architecture and could recur on future launches, but it remains a hypothesis rather than a confirmed incident cause.
4. `us_mock` fixed-port HTTP `10048` recurrence remains a separate unresolved diagnostic thread.
5. No real-account action, worker restart, code edit, configuration edit, registry edit, firewall/WFP change, staging, or commit was performed during Rounds 66–82.

## Open gaps for the next AI

- Refresh current process identity, mutex state, status files, and logs before relying on any historical PID or timestamp.
- If the operator authorizes further diagnosis, obtain watchdog output with sufficient timestamp coverage and corroborate it against process creation events.
- Do not implement a race fix without explicit code-change authorization and a reproduction/test plan.
- Do not claim that the reconciliation gate is clear from lifecycle state alone.
- Do not claim that HTTP `10048` is caused by `TIME_WAIT` without current TCP-state evidence.

## Verification record for this document

- Existing operator-record format and the Round 82 instruction file were read.
- Source inspection confirmed Loguru rotation and the `ProcessLock`/supervisor startup order.
- Read-only log inspection found no matching watchdog entries for the early restart gap.
- Timestamped rotated `kr_mock` backups were confirmed.
- No worker, real account, configuration, registry, network, firewall, or Git state was modified.
- This new handoff file is intentionally uncommitted.

