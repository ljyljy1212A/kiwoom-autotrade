# Operator Record Wrap-up v10-v176

## Purpose

This English document summarizes the investigation through session v176 and
provides a safe handoff for another AI. It is an operator record and does not
authorize implementation, worker actions, firewall/WFP changes, AhnLab
changes, staging, or commits.

## Executive conclusion

Two separate mock-networking findings remain:

1. The US mock WebSocket path repeatedly produced `WinError 10013` after the
   WebSocket local source port was changed to `10002`. The live verification
   failed; no successful WebSocket connection on `10002` was observed.
2. US mock HTTP on local source port `443` produced intermittent
   `WinError 10048`, while REST I/O succeeded in other cycles. This remains
   analytically separate from the WebSocket `10013` result.

The exact failing WebSocket syscall in the externally launched worker is still
not proven. The current `_connect_ws_socket()` implementation wraps `bind()`
and `connect()` in the same `try/except OSError`, so its existing warning does
not distinguish a bind failure from a connect failure.

The v175 and v176 raw-connect tests do **not** resolve that uncertainty. They
were launched as Codex child processes and inherited:

    CODEX_SANDBOX_NETWORK_DISABLED=1

In v176, both fixed-port and ephemeral connections failed with
`WinError 10013` to both Kiwoom and `example.com`. This demonstrates a general
outbound-connect restriction in the Codex-launched test process. It is not
valid evidence that AhnLab, Windows Firewall/WFP, the Kiwoom destination, or a
fixed local port caused the externally launched worker's failure.

Do not declare the WebSocket issue fixed, and do not attribute it to AhnLab or
firewall/WFP from the present evidence.

## Progress by session

### v91-v105

Rate-limit and balance/quote observability work was implemented and tested.
KR mock WebSocket local-port separation moved the WebSocket to `10001` while
KR HTTP remained on `10000`.

### v116-v147

Fixed-port HTTP close behavior and deferred-close waiting were investigated.
The working tree now contains scoped HTTP `SO_LINGER(1, 0)` behavior and a
bounded close-completion wait, with focused tests present in the workspace.
These changes address HTTP `10048` lifecycle behavior, not WebSocket `10013`.

Live evidence in v147 showed the deferred-close wait was insufficient by
itself: one US cycle succeeded, followed by three HTTP `WinError 10048`
failures while the WebSocket shared local port `443`.

### v148-v153

US mock WebSocket local-port separation changed the local source port from
`443` to `10002`, leaving US HTTP on `443`. Focused tests reported:

    89 passed, 4 skipped, 6 warnings, 2 subtests passed in 17.13s

The v153 live summary reported:

    WebSocket 10013 on 4/4 cycles; no successful connection on 10002
    HTTP 10048 on cycles 2-4 of 4

The v153 raw capture was not located later. Preserve the distinction between
the retained summary and raw evidence.

### v156-v166

The operator started `us_mock` externally. v166 recorded PID `16128`, instance
`61d55585978644adaebac6435d088bec`, and start time
`2026-08-20T00:59:27.175107+00:00`:

    WebSocket 10013 on 7/7 observed cycles
    HTTP 10048 on 4/7 observed cycles
    REST I/O succeeded on intervening/later cycles
    no successful local WebSocket connection on 10002

The latest status captured in the v169 record showed the worker running with
`auto_trading_enabled=false`. That is a historical v169 snapshot, not a
current-state assertion.

### v167-v169

Process-parent and scheduled-task provenance checks were inconclusive because
queries returned access denied, blank, or no matching output.

Persistent environment-variable checks established:

    User scope: blank
    Machine scope: blank
    Process scope in the Codex observer: 1
    HKCU registry value: NOT_FOUND
    HKLM registry value: NOT_FOUND

The operator-confirmed launcher does not set or reference
`CODEX_SANDBOX_NETWORK_DISABLED`, `.env`, an activation script, or another
environment setup file. Therefore a normal Explorer-launched worker does not
inherit the variable from User/Machine persistence. This finding does not
make network tests launched from the Codex observer valid: Codex child
processes inherit the observer's process-scoped value `1`.

### v170

Read-only firewall inspection found:

- `Get-NetFirewallRule` failed with access denied.
- `netsh wfp show filters` failed with `ERROR_ACCESS_DENIED` and required
  elevation; no elevation was attempted.
- Non-elevated `netsh advfirewall` output contained generic outbound `443`
  allow rules but no visible `10002` or worker-Python-path match.

These incomplete queries neither confirmed nor ruled out a specific
firewall/WFP policy.

### v171

Excluded-port and occupancy checks found:

- The only IPv4/IPv6 TCP/UDP excluded range was `50000-50059`.
- Ports `443`, `10000`, `10001`, and `10002` were outside that range.
- No TCP entry was present on `10001` or `10002` at the observation time.
- `hns`, `vmcompute`, and `WSLService` were running; Docker Desktop Service
  was not found.

This ruled out the visible excluded range and observed port occupancy as the
cause at that time.

### v172

All Domain, Private, and Public firewall profiles were ON with
`BlockInbound,AllowOutbound`. `Get-NetConnectionProfile` and the
`SecurityCenter2` antivirus CIM query returned access denied.

Service enumeration showed Microsoft Defender components and
`AhnLab Safe Transaction Service` running. AhnLab's presence is environmental
context, not proof of causality. The operator later established a standing
condition: whenever the trading program is running, Kiwoom HTS and AhnLab
Safe Transaction are running together.

### v173

A temporary raw-bind reproducer was executed outside the repository with the
worker's Python interpreter. IPv4 and IPv6 `bind()` succeeded on `10002`,
`10001`, and baseline `10099`. The temporary file was deleted.

This shows that a cold local bind is permitted in the Codex child process.
It does not test outbound connectivity and does not by itself identify the
worker's failing syscall.

### v174

Source review established that the worker path performs more than the v173
reproducer:

    socket.getaddrinfo(...)
    socket.socket(...)
    sock.settimeout(10)
    sock.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
    sock.bind((wildcard_address, local_port))
    sock.connect(remote_address)
    sock.setblocking(False)
    websockets.connect(..., sock=sock)

Each ordinary retry creates a new socket and closes the failed socket in the
caught `OSError` path. No clear repeated-retry handle leak was identified.
The existing log does not record whether `bind()` or `connect()` raised the
error.

The existing v103/v116/v143/v148 proposals address earlier HTTP `10048` or
port-separation work. None diagnoses the current WebSocket `10013` issue.

### v175

A Codex-launched bind-plus-connect reproducer reported `BIND OK` followed by
`CONNECT FAILED` / `WinError 10013` for local ports `10002` and `10001` to
`mockapi.kiwoom.com:10000` across three runs. The temporary script was deleted.

This result was initially interpreted as connection-level blocking. That
interpretation is superseded by v176 because the reproducer inherited the
Codex process's outbound-network restriction.

### v176

A two-pass matrix tested:

| Case | Local source | Destination | Result in both passes |
|---|---|---|---|
| A | fixed `10002` | `mockapi.kiwoom.com:10000` | bind OK, connect `10013` |
| B | ephemeral | `mockapi.kiwoom.com:10000` | connect `10013` |
| C | fixed `10002` | `example.com:443` | bind OK, connect `10013` |
| D | ephemeral | `example.com:443` | connect `10013` |

The executing environment was then freshly confirmed to contain:

    CODEX_SANDBOX_NETWORK_DISABLED=1

Because even the ordinary ephemeral `example.com:443` baseline failed, the
matrix measures the Codex child-process restriction rather than the normal
worker environment. The temporary script was deleted and left no repository
trace.

## What is ruled out or established

Established:

- US WebSocket port-separation live verification failed with repeated
  `WinError 10013`.
- US HTTP `WinError 10048` is a distinct, intermittent finding.
- `10001` and `10002` are outside the visible excluded-port ranges.
- No listener/connection held `10001` or `10002` during the v171/v173 checks.
- A cold local bind succeeds on both ports.
- Persistent User/Machine `CODEX_SANDBOX_NETWORK_DISABLED` is absent.
- Codex-launched child processes inherit process-scoped
  `CODEX_SANDBOX_NETWORK_DISABLED=1`, invalidating their outbound-connect
  results as evidence about an Explorer-launched worker.

Not established:

- Whether the external worker's `10013` occurs at `bind()` or `connect()`.
- Whether a specific Windows Firewall/WFP filter causes the worker failure.
- Whether AhnLab Safe Transaction causes or contributes to the failure.
- Whether Kiwoom HTS changes the relevant network behavior.
- Whether a clean, non-Codex raw TCP matrix succeeds or fails.
- Whether the current worker/control status still matches the v169 snapshot.

## Current source and worktree boundary

The current working tree contains known pre-existing modified and untracked
files. The six tracked modified files are:

    src/core/broker_http.py
    src/core/engine.py
    src/core/kiwoom_client.py
    src/core/realtime_feed.py
    src/core/token_manager.py
    src/main.py

The working source currently selects:

| Path | Local source port |
|---|---:|
| KR mock HTTP | `10000` |
| KR mock WebSocket | `10001` |
| US mock HTTP | `443` |
| US mock WebSocket | `10002` |

These changes and related tests/proposals remain unstaged or untracked. Do not
reset, clean, stage, or commit them without explicit authorization.

## Instructions for the next AI

1. Read this document and `OPERATOR_RECORD_wrapup_v10-v169.md` before acting.
2. Keep WebSocket `10013` and HTTP `10048` analytically separate.
3. Do not call the external worker's `10013` a proven bind failure; current
   worker logs combine `bind()` and `connect()` in one exception path.
4. Do not use a network test launched from Codex as evidence about normal
   outbound connectivity while `CODEX_SANDBOX_NETWORK_DISABLED=1` is inherited.
5. Do not attribute the issue to AhnLab, Kiwoom HTS, Windows Firewall, or WFP
   without new literal evidence. AhnLab and HTS are expected to be running
   together with the trading program.
6. Do not start, stop, or restart `kr_mock`, `us_mock`, `kr_real`, or `us_real`
   without separate explicit authorization.
7. Do not change source, configuration, environment variables, registry,
   firewall/WFP, AhnLab settings, control state, staging, or commits without
   explicit authorization for that specific action.
8. Preserve the dirty worktree. Never reset or clean accumulated operator
   records, proposals, diagnostics, tests, or source changes.
9. Treat the latest worker/control state in v169 as historical until refreshed
   with separately authorized read-only checks.
10. Keep unit-test success, local bind success, raw TCP connect success,
    WebSocket handshake success, REST-cycle success, and overall live
    verification as separate evidence categories.

## Recommended next diagnostic decision

The most informative next test is the v176 four-case raw TCP matrix executed
from a normal operator-launched PowerShell or Explorer process that is not a
Codex descendant and does not contain `CODEX_SANDBOX_NETWORK_DISABLED`.

This is not authorized by this document. Before proceeding, obtain explicit
operator authorization for the exact workflow. A safe future workflow should:

1. Keep Kiwoom HTS and AhnLab Safe Transaction running, matching the normal
   operating condition.
2. Confirm in the external console that
   `CODEX_SANDBOX_NETWORK_DISABLED` is absent without modifying it.
3. Run only raw TCP `bind()`/`connect()` attempts; send no application data,
   perform no TLS/WebSocket handshake, and use no credentials.
4. Test fixed `10002` and ephemeral local ports against both
   `mockapi.kiwoom.com:10000` and an unrelated baseline destination.
5. Preserve literal output and timestamps, then delete the throwaway script.
6. Do not infer a remediation until the non-Codex matrix result is known.

If the external matrix is later authorized, it may require the operator to
launch the prepared script manually because an AI-launched child process can
inherit the Codex network restriction.

## Verification criteria for this handoff

Run:

    git status --short

Expected result: the same six pre-existing tracked modifications remain, all
existing untracked artifacts remain, and this new file appears only as:

    ?? OPERATOR_RECORD_wrapup_v10-v176.md

No worker, configuration, source, registry, firewall/WFP, AhnLab, control,
staging, or commit change is part of this documentation task.

