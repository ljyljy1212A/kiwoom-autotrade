# Instructions for Next AI — Session v170

## Read first

Read `OPERATOR_RECORD_wrapup_v10-v169.md` in full. Treat it as an evidence
summary and safety handoff, not as authorization to start, stop, restart, or
modify anything.

## Current state

- `us_mock` was last verified running as PID `16128`.
- `auto_trading_enabled=false`.
- `kr_mock` remained stopped in the prior verified state.
- The US WebSocket failed to bind local port `10002` with `WinError 10013`
  on every observed cycle.
- HTTP local port `443` showed intermittent `WinError 10048` with successful
  REST recovery on other cycles.
- `CODEX_SANDBOX_NETWORK_DISABLED` was absent at User and Machine scope and
  absent from the checked registry locations.
- Parent-process and Task Scheduler provenance checks were inconclusive.

## Safety boundaries

This handoff grants no authority to:

- start, stop, or restart any worker;
- touch `kr_real` or `us_real`;
- edit source, tests, configuration, registry, environment variables, or
  firewall/WFP policy;
- stage or commit files; or
- propose or implement a remediation.

Preserve the existing dirty worktree. Do not infer authorization from the
operator record. If the next task requires a live test or a policy change,
stop and request explicit authorization.

## Required analytical separation

Report the WebSocket and HTTP paths independently:

1. WebSocket: `10002` / `WinError 10013`, 100% failure in the observed v153
   summary and v166 literal logs. This remains unresolved and may reflect a
   machine-level firewall/WFP rule, but that policy has not been identified.
2. HTTP: local `443` / `WinError 10048`, intermittent and self-recovering in
   v166. Keep it separate from the WebSocket result and do not reopen the
   already-closed transient collision investigation without new evidence.

## Verification requirements

For any authorized read-only follow-up, capture literal output for:

- fresh worker status and control state;
- exact timestamps and process identity;
- relevant application log lines;
- filtered netstat output for local ports `10002` and `443`; and
- `git status --short` before and after.

The handback must state which claims are literal current evidence, which come
from the v153 summary, and which remain unverified.
