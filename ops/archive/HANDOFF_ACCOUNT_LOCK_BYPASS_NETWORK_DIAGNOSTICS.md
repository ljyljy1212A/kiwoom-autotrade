# Account Lock Bypass and Network Diagnostics Handoff

## User Request

Polished English translation:

> I am going to have another AI continue this work based on our progress so far. Please generate an English Markdown file and translate this sentence into English as well.

## Scope and Safety Constraints

- Production target is native Windows.
- Only `kr_mock` and `us_mock` may be touched for this work.
- Do not touch `kr_real` or `us_real`.
- Do not submit, cancel, or modify broker orders.
- Do not stop or restart `us_mock` without explicit authorization from a follow-up task.
- Do not retry `kr_mock` until outbound connectivity is restored and verified.
- Preserve unrelated worktree changes.
- Do not use destructive Git commands or force-push.
- Fail closed when process identity, mutex state, broker evidence, or quote freshness is ambiguous.

## Repository

- Checkout: `C:\auto\작업7차\kiwoom-autotrade`
- Current worktree was clean at the last check.
- No source-code changes were made during the latest network investigation.
- Existing unrelated stash remains preserved: `stash@{0}`.

## Completed Database Work

The authorized `kr_mock` reclassification was completed in `data\trades_kr_mock.db`:

- Eight stale `open` pending orders were changed to `stale_unresolved_no_reservation`.
- Eight rows were added to `manual_reconciliation_audit`.
- Audit evidence records:
  - Broker order-ID status unavailable.
  - Historical order query unavailable.
  - Original August 14 cancellation returned RC4058 timeout.
  - August 18 holdings showed no reservation:
    - `014280`: `rmnd_qty=149`, `trde_able_qty=149`
    - `080580`: `rmnd_qty=37`, `trde_able_qty=37`
  - This was manual policy closure, not broker-confirmed terminal status.
  - It is distinct from the existing RC4032 `closed_unconfirmed` precedent.
- Pre-change backup:
  - `data\backup\kr_mock\reclass_pre_20260818_1550.db`
- Verification:
  - SQLite integrity: `ok`
  - `nonterminal_count=0`
  - Audit rows: `8`
  - Status counts: `filled=212`, `cancelled=16`, `stale_unresolved_no_reservation=8`, `closed_unconfirmed=7`

## `kr_mock` Restart Outcome

- Old worker PID: `15696`
- New worker PID: `6236`
- New instance: `9503b67ed8bb4db1aa729f2f9d4d9a30`
- KRX session was confirmed closed before restart.
- Passive balance monitoring started.
- The worker repeatedly failed token issuance before WebSocket login:

```text
httpx.ConnectError: All connection attempts failed
src.utils.exceptions.RetryableError: ... token issuance network error: All connection attempts failed
```

- The worker was stopped cleanly.
- Final supervisor state: `STOPPED`.
- PID `6236` was absent after stopping.
- External mutex probe showed:
  - `is_alive_before=False`
  - Probe acquire succeeded.
  - Probe release succeeded.
  - `is_alive_after=False`

## Root Cause Evidence

Source defaults:

- Mock REST: `https://mockapi.kiwoom.com`
- Mock WebSocket: `wss://mockapi.kiwoom.com:10000/api/dostk/websocket`
- `us_mock` uses the same mock host and WebSocket port.

Network checks on August 18, 2026 around `16:10 KST`:

- `mockapi.kiwoom.com` DNS resolved to `112.175.65.18`.
- TCP `112.175.65.18:443`: failed.
- TCP `112.175.65.18:10000`: failed.
- HTTPS to `mockapi.kiwoom.com`: failed.
- HTTPS to `example.com`: failed repeatedly.
- HTTPS to `www.microsoft.com`: failed repeatedly.
- DNS resolution for unrelated domains worked.
- WinHTTP proxy: direct access, no proxy configured.

The decisive firewall rule is present and enabled:

```text
codex_sandbox_offline_block_outbound
Description: Codex Sandbox Offline - Block Non-Loopback Outbound
Action: Block
RemoteIP: all non-loopback addresses
```

This explains the failed outbound tests and is an execution-environment restriction, not a code or broker-credential finding. Do not remove or modify it from this task.

Additional machine evidence:

- LAN adapter had IPv4 `192.168.0.10`, gateway `192.168.0.1`, and configured DNS servers.
- Microsoft Defender services were running.
- `UnicornHttpsService` was installed but stopped.
- `WinDivert64.sys` installation was recorded around `12:33 KST`.
- Windows Update download activity was recorded around `12:08 KST`.
- WMI queries for boot time and hotfix history were denied.

## Current `us_mock` State

Supervisor status at the latest check:

```json
{"account":"us_mock","pid":16740,"running":true,"state":"RUNNING","market":"US"}
```

- PID `16740` was not stopped, restarted, or modified.
- Startup at `12:48:14 KST` logged successful token issuance, WebSocket login, and REG response `return_code=0`.
- Later logs show continuous orphan cleanup audits and no explicit WebSocket disconnect error.
- Current quote timestamps are stale; the latest observed quote was from August 17, 2026 at approximately `15:14 KST`.
- No established TCP session was visible for PID `16740` during the diagnostic check.

Conclusion: `us_mock` is **unclear/degraded**, not confirmed healthy. It may be running with stale or absent live market data while supervisor heartbeats and orphan audits continue.

## Watchdog Coverage Gap

The current evidence indicates that process liveness and periodic orphan audits can remain healthy while WebSocket connectivity or quote freshness is stale. A future, separately scoped change should consider detecting and alerting on stale quote/WebSocket freshness rather than relying only on PID, heartbeat, or supervisor `RUNNING` state.

Do not implement that adjacent improvement in the handoff task unless explicitly requested.

## Safe Next Steps

1. Restore or permit outbound connectivity in the execution environment or host network, outside this code task.
2. Re-run non-mutating checks for:
   - `mockapi.kiwoom.com:443`
   - `mockapi.kiwoom.com:10000`
   - An unrelated HTTPS host
3. Confirm the outbound-blocking rule is no longer active for the execution context.
4. Re-check `us_mock` quote freshness and TCP/WebSocket state without restarting it unless explicitly authorized.
5. Only after connectivity is confirmed, obtain explicit authorization before retrying `kr_mock`.
6. If `kr_mock` is retried, require a new PID, released old PID/mutex, successful token issuance, WebSocket login and REG response, passive monitoring, clean orphan audits, and an approximately 15-minute stable observation window.

## Final Safety State

- `kr_mock`: stopped cleanly; no retry pending.
- `us_mock`: still running but connectivity/quote freshness unclear; do not restart without authorization.
- Real accounts: untouched.
- Broker orders: untouched.
- Database reclassification: complete and backed up.
- Code changes: none from the network-diagnostics work.
