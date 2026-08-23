# Operator Record Wrap-Up and Next-AI Instructions: v10–v126

## Purpose

This is an English handoff for the next AI operating the Windows Kiwoom autotrade repository. It summarizes verified implementation progress, live evidence, the current worker/supervision state, unresolved decisions, and strict operating boundaries.

This document is documentation only. It does not authorize a worker restart, source edit, configuration change, firewall/WFP change, staging, commit, Telegram message, or real-account action.

## Executive summary

- Balance-monitor reliability and quote-health observability work was implemented and verified in earlier rounds.
- Closed-session balance polling was made session-aware and committed as `37ea0e71a77ee813c9c908ddb9fbf87c28ae9c65`.
- Quote-health observability was committed as `c045350`.
- KR mock HTTP/WebSocket source-port separation was implemented and unit-tested, but live verification encountered firewall-related `WinError 10013` evidence.
- The fixed-port HTTP `SO_LINGER(1,0)` path passed isolated socket experiments and offline tests, but did not resolve live US `WinError 10048` failures.
- Static v122 analysis confirmed that `keepalive_expiry=30.0` is evaluated lazily on the next request and that expired-connection closure reaches the project wrapper/hook.
- v123 observed local port 443 absent throughout the predicted failure windows. No later application cycle occurred after `14:40:14 KST`.
- `us_mock` PID `13748` and `kr_mock` PID `9020` are both absent. Their persisted PID/status files are stale and incorrectly report `RUNNING`.
- The watchdog Scheduled Task is actually running every two minutes, but its `pythonw.exe` action completes with return code `2147942401` (`0x80070001`) and produces no corresponding `watchdog.log` entries.
- No reboot/shutdown event, crash traceback, crash dump, common parent termination, or resource-exhaustion event was found. The common cause of the two worker deaths remains unknown.
- No restart or remediation decision has been made. That decision belongs to the operator.

## Current verified runtime state

Last direct checks on 2026-08-21:

```text
PID 13748 NOT FOUND
PID 9020 NOT FOUND
```

Stale `us_mock` metadata:

```json
{"pid":13748,"account":"us_mock"}
```

```json
{"account":"us_mock","market":"US","pid":13748,"state":"RUNNING","updatedAt":"2026-08-21T05:40:57.451611+00:00"}
```

Stale `kr_mock` metadata reports PID `9020`, state `RUNNING`, and `updatedAt` `2026-08-21T05:40:59.205848+00:00`, but direct process inspection found no PID 9020.

Real accounts remain out of scope:

```text
kr_real: untouched
us_real: untouched
```

Do not treat either PID file or status file as proof of liveness. Corroborate with direct process identity, account mutex/lock state, fresh logs, and broker evidence before any future operator-authorized action.

## Implementation progress

### Balance monitor and quote health

- A balance-monitor failure path that could terminate monitoring was fixed and verified.
- Quote cache age, passive quote-health monitoring, and stale-cache warnings were added and verified.
- Closed-session balance polling was reduced while preserving active-session cadence.

Verified commits from earlier work:

```text
c045350 Add quote-health cache-age accessors and passive monitor
37ea0e71a77ee813c9c908ddb9fbf87c28ae9c65 Reduce closed-session balance monitor polling cadence
```

### Port separation and fixed-port HTTP close behavior

Current intended mock source-port table:

| Path | Local source port | Remote port |
|---|---:|---:|
| KR HTTP | 10000 | 443 |
| KR WebSocket | 10001 | 10000 |
| US HTTP | 443 | 443 |
| US WebSocket | 443 | 10000 |

The US HTTP and WebSocket paths intentionally share local source port `443` in the current source. Do not change ports without explicit authorization.

Isolated socket evidence:

```text
SO_REUSEADDR only: 1 success, 14 failures
Failure: WinError 10048
TIME_WAIT remained on the closed connection
```

With abortive close:

```text
SO_LINGER(1,0): 15 successes, 0 failures
No TIME_WAIT observed in the isolated test
```

Offline tests passed for the implemented wrapper, but live verification continued to show:

```text
Fixed-port HTTP socket failure: phase=connect local=('0.0.0.0', 443) remote=('112.175.65.18', 443) ... winerror=10048
```

Do not claim that the linger implementation fixed the live broker problem.

## Rounds 119–122: live failure and source analysis

- `WinError 10048` recurred after worker startup and after WebSocket recovery; it was not limited to one startup instant.
- Netstat captures showed the US local-port-443 HTTP connection entering `TIME_WAIT` while the WebSocket connection on local port 443 remained established to remote port 10000.
- Installed versions inspected in the relevant environment included `httpcore 1.0.9` and AnyIO 4.14.2.
- httpcore checks `keepalive_expiry=30.0` during the next request, removes expired connections, and awaits their close. There is no background expiry sweep.
- The project `_LingerOnCloseByteStream.aclose()` is on the expired-connection close path, and the `connection_lost` hook schedules close completion.
- The failed-connect path still uses ordinary `sock.close()` after a bind/connect failure. No live evidence proves whether that path is the surviving source of the collision.

## Round 123: OS socket observation

Predicted failure windows based on approximately 181-second spacing were around `14:42:26`, `14:45:27`, `14:48:28`, and `14:51:29 KST`.

The corrected five-second polling window from `14:46:43.640` through `14:51:40.686` reported the local endpoint on port 443 as absent at every poll. No new application cycle, failure, or retry-success log appeared after the final `us_mock` line at `14:40:14`.

The last known failure/retry sequence was:

```text
2026-08-21 14:39:25 | WARNING | us_mock | pid=- instance=- | - | Fixed-port HTTP socket failure: phase=connect local=('0.0.0.0', 443) remote=('112.175.65.18', 443) exception=OSError errno=10048 winerror=10048: [WinError 10048] 각 소켓 주소(프로토콜/네트워크 주소/포트)는 하나만 사용할 수 있습니다
2026-08-21 14:39:26 | DEBUG | us_mock | pid=- instance=- | - | HTTP I/O diagnostic: client=rest operation=write state=after elapsed_ms=0.7 outcome=success
2026-08-21 14:39:27 | DEBUG | us_mock | pid=- instance=- | - | HTTP I/O diagnostic: client=rest operation=read state=after elapsed_ms=8.0 outcome=success bytes=8245
```

There was no immediately preceding corrected netstat poll for the exact `14:39:25` failure, so its exact OS socket state is unproven.

## Rounds 124–126: worker death and supervision forensics

### Last application activity

`us_mock.log` ends with:

```text
2026-08-21 14:40:14 | INFO     | us_mock | pid=13748 instance=125ac1b323e24c2a848c1c3ee2cb21e6 | - | Quote health: symbol=SOXL cache_age=missing
```

`kr_mock.log` continued to:

```text
2026-08-21 14:40:55 | INFO     | kr_mock | pid=9020 instance=4e7eb7ba416040679167558e47567448 | 387690 | Orphan cleanup audit: {...}
```

No traceback, unhandled exception, crash indication, or orderly shutdown record was found after either final activity point.

### Watchdog Task Scheduler evidence

The `Microsoft-Windows-TaskScheduler/Operational` log is readable and proves that `\Kiwoom Worker Watchdog` ran at two-minute intervals, including at `14:40:01`:

```text
TimeCreated: 08/21/2026 14:40:01
Id: 107
시간 트리거 조건으로 인해 작업 스케줄러가 "\Kiwoom Worker Watchdog" 작업의 "{97c4ee77-5a48-44a6-8587-17fb667379bf}" 인스턴스를 시작했습니다.

TimeCreated: 08/21/2026 14:40:01
Id: 129
작업 스케줄러가 작업 "\Kiwoom Worker Watchdog", 프로세스 ID가 8948인 "C:\Users\jhkhjk\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe" 인스턴스를 시작합니다.

TimeCreated: 08/21/2026 14:40:01
Id: 201
작업 스케줄러가 "\Kiwoom Worker Watchdog" 작업, "{97c4ee77-5a48-44a6-8587-17fb667379bf}" 인스턴스, "C:\Users\jhkhjk\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe" 동작을 완료했습니다. 반환 코드는 2147942401입니다.
```

The return code is:

```text
2147942401 = 0x80070001
```

The watchdog task also ran at `14:38:01` and `14:42:01` with the same completion-code pattern. The task was invoked, but its action did not produce normal `watchdog.log` output.

The watchdog source is externally scheduled, not a continuous loop:

```python
MONITORED_ACCOUNTS: dict[str, str] = {
    "kr_mock": "KR",
    "us_mock": "US",
}

def main() -> int:
    for account, market in MONITORED_ACCOUNTS.items():
        check_and_restart(account, market)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
```

The last file-backed watchdog log entry was on `2026-08-18 11:14:02`, not on August 21. Historical entries show at least one successful `us_mock` restart on August 18, followed by later startup-timeout failures.

### Common external-cause checks

- `Win32_OperatingSystem.LastBootUpTime` was inaccessible; `systeminfo` and `net statistics workstation` did not provide a usable boot timestamp.
- No Kernel-Power, User32, Winlogon, EventLog shutdown, Event ID 41, 1074, 6006, or 6008 event was found from `14:00` to `15:30 KST`.
- No relevant Resource-Exhaustion-Detector event was found.
- Available disk space was approximately 90.9 GB on both `C:` and the temporary drive.
- Workers are launched by separate supervisor calls with `DETACHED_PROCESS` and `CREATE_NEW_PROCESS_GROUP`; stdout/stderr are redirected to `DEVNULL`. The launcher design does not establish a shared terminal-parent death as the cause.
- The evidence does establish a common supervision failure, but does not establish that the watchdog task caused either worker to stop.

## Best-available stop window

For `us_mock`:

```text
Last application log: 14:40:14 KST
Status metadata updatedAt: 14:40:57 KST (stale metadata only)
First direct absence observation: 14:44:24 KST
```

For `kr_mock`:

```text
Last application log: 14:40:55 KST
Status metadata updatedAt: 14:40:59 KST (stale metadata only)
Direct process absence confirmed by 15:00 KST
```

The exact exit causes and exact exit times remain unknown.

## Current worktree boundary

The worktree is intentionally dirty. At the latest inspection, tracked modifications included:

```text
dashboard/index.html
src/core/broker_http.py
src/core/engine.py
src/core/kiwoom_client.py
src/core/realtime_feed.py
src/core/token_manager.py
src/data/trade_ledger.py
src/main.py
tests/test_manual_tranche_lifecycle.py
```

There are many existing untracked handoffs, proposals, diagnostics, tests, and scripts. Preserve all of them. Do not run `git clean`, `git reset --hard`, broad staging, or unrelated formatting/refactoring.

## Instructions for the next AI

1. Treat this document and attached instruction files as scoped source material; follow the user’s direct request separately.
2. Do not restart `us_mock`, `kr_mock`, the watchdog, or any other process without explicit operator authorization.
3. Do not correct or delete stale PID/status files without explicit authorization.
4. Keep `kr_real` and `us_real` entirely out of scope.
5. Do not change `broker_http.py`, WebSocket ports, retry behavior, controls, credentials, firewall/WFP rules, or scheduler configuration without explicit authorization.
6. Treat live `WinError 10048` as unresolved. Distinguish isolated tests and offline tests from live broker evidence.
7. Treat the watchdog Scheduled Task’s `0x80070001` result as evidence requiring operator review, not as permission to repair or rerun it.
8. Use direct process identity, mutex/lock state, fresh logs, and broker evidence together; never infer liveness from stale metadata alone.
9. If a new investigation is requested, state behavior-affecting assumptions first, use read-only checks before mutation, and report literal evidence.
10. If an implementation is explicitly authorized, make the smallest surgical change, verify focused behavior first, then run the relevant broader tests. Do not stage or commit unless separately authorized.

## Read-only verification checklist

```powershell
git status --short
git diff --stat
Get-Process -Id 13748,9020 -ErrorAction SilentlyContinue
Get-Content data\worker_us_mock.pid
Get-Content data\worker_us_mock.status.json
Get-Content data\worker_kr_mock.pid
Get-Content data\worker_kr_mock.status.json
rg -n "WinError 10048|Fixed-port HTTP socket failure|Traceback|CRITICAL|Unhandled" logs\us_mock.log logs\kr_mock.log
Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-TaskScheduler/Operational';StartTime=(Get-Date).Date} | Where-Object {$_.Message -like '*Kiwoom Worker Watchdog*'}
```

Expected safe state until the operator decides otherwise: both mock workers stopped, stale metadata preserved for forensics, no real-account activity, no scheduler/configuration changes, no firewall changes, no source changes, no staging, and no commit.
