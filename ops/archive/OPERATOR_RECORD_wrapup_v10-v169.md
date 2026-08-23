# Operator Record Wrap-up v10-v169

## Purpose

This English document summarizes the investigation through v169 and provides
a safe handoff. It is an operator record, not authorization for further
implementation or live activity.

## Executive conclusion

The US mock networking issue is split into two distinct findings:

- The US WebSocket local-port `10002` failed with `WinError 10013` on every
  observed cycle in v153 and v166. It never successfully bound to `10002`.
- The US HTTP local-port `443` produced intermittent `WinError 10048` in v166:
  four failures across seven observed cycles, with successful REST I/O in
  other cycles. This self-recovering shape is consistent with the previously
  diagnosed transient same-4-tuple `TIME_WAIT` collision.

The persistent-environment-variable theory was tested in v169 and ruled out:
`CODEX_SANDBOX_NETWORK_DISABLED` was absent at User and Machine scope and
absent from both registry locations checked. The launcher batch file does not
set or reference it. The WebSocket cause therefore remains open, with a
machine-level firewall/WFP policy still a leading explanation, but no policy
was changed or conclusively identified.

## Progress by session

### v91-v105

Rate-limit and balance/quote observability work was implemented and tested.
Earlier KR mock work moved the KR WebSocket local port to `10001` while KR
HTTP remained on `10000`.

### v116-v147

Fixed-port HTTP close behavior and deferred-close waiting were investigated.
Focused tests passed, but v147 live evidence showed one successful US cycle
followed by three `WinError 10048` failures. Deferred close waiting was
insufficient as a live fix.

### v148-v153

US WebSocket source-port separation moved the US WebSocket from local `443`
to local `10002`, while US HTTP remained on local `443`. Unit tests passed:

    89 passed, 4 skipped, 6 warnings, 2 subtests passed in 17.13s

The v153 live test was not successful. Its recorded summary reports:

    WebSocket 10013 on 4/4 cycles; no successful bind to 10002
    HTTP 10048 on cycles 2-4 of 4

The v153 raw capture file was not located in later sessions; the comparison
uses the existing v153 summary rather than pretending it is raw log evidence.

### v156-v166

The operator started `us_mock` externally. v166 gathered literal evidence for
PID `16128`, instance `61d55585978644adaebac6435d088bec`:

    startedAt=2026-08-20T00:59:27.175107+00:00
    WebSocket 10013 on 7/7 observed cycles
    HTTP 10048 on 4/7 observed cycles
    REST I/O succeeded on intervening/later cycles
    no local WebSocket bind on 10002

The worker remained running and `auto_trading_enabled` remained `false`.

### v167-v169

Parent-process provenance checks were inconclusive:

    Get-CimInstance Win32_Process: ProcessId=16128 NOT_RESOLVABLE
    tasklist: ERROR: Access denied
    Get-Process owner: UserName blank
    scheduled-task lookup: no candidates / task info query failed
    Task Scheduler event query: no matching output

v169 checked the persistent environment-variable theory:

    User scope: blank
    Machine scope: blank
    Process scope in the observer: 1
    HKCU registry lookup: NOT_FOUND
    HKLM registry lookup: NOT_FOUND

The inspected launcher was:

    @echo off
    cd /d "%~dp0.."
    set "PYTHON_EXE=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
    set "AUTO_TRADING_ENABLED=false"
    if not exist "%PYTHON_EXE%" (
      echo Python 3.14 runtime was not found: %PYTHON_EXE%
      pause
      exit /b 1
    )
    "%PYTHON_EXE%" -m src.worker_supervisor start --account us_mock --market US

It does not reference `CODEX_SANDBOX_NETWORK_DISABLED`, `.env`, an activation
script, or another environment setup file.

## Current verified safety state

The latest v169 status was:

    {"account": "us_mock", "pid": 16128, "running": true, "instanceId": "61d55585978644adaebac6435d088bec", "state": "RUNNING", "market": "US"}
    {"account": "us_mock", "auto_trading_enabled": false, "updated_by": "telegram"}

`kr_mock` was not started or touched. `kr_real` and `us_real` were not
started or touched. No worker was stopped or restarted during v167-v169. No
registry, firewall/WFP, configuration, source, credential, or control-state
change was made.

The worktree contains known pre-existing unstaged and untracked changes.
Preserve them; do not reset, clean, stage, or commit them without explicit
authorization.

## Instructions for the next AI

1. Keep the WebSocket and HTTP findings analytically separate.
2. Treat the US WebSocket `10002` / `WinError 10013` live verification as a
   failure. Do not describe port separation as successful merely because unit
   tests passed.
3. Treat the HTTP `10048` behavior separately; v166 showed intermittent,
   self-recovering failures consistent with the already-closed transient
   collision finding.
4. Do not start, stop, or restart `kr_mock`, `us_mock`, `kr_real`, or
   `us_real` without separate explicit authorization.
5. Do not change source, configuration, registry, firewall/WFP policy,
   environment variables, staging, or commits unless explicitly authorized.
6. If future read-only investigation is authorized, preserve literal logs,
   netstat output, worker status, control state, and exact timestamps.
7. Do not claim the sandbox hypothesis is conclusively confirmed or disproved:
   persistent User/Machine environment inheritance is ruled out, but process
   ancestry and the effective blocking policy remain unverified.
8. Every future handoff must distinguish unit-test success, WebSocket bind
   success, REST-cycle success, and overall live-verification success.

## Operator decision requested

Do not authorize a commit or declare the US WebSocket issue fixed from the
current evidence. Any future policy investigation or live test needs its own
explicit authorization and must retain the fail-closed state.
