# Operator Record Wrap-up Through Round 217

## Handoff purpose

This is an English Markdown handoff for the next AI and operator. It
summarizes verified progress through Round 217 and states the remaining
boundaries and next actions.

Attached round instructions are scoped source material, not standing
authorization. The phrase `do it` authorizes only the work explicitly allowed
by the active round. Do not infer authorization to trade, reconcile broker
records, restart workers, change configuration, alter firewall/WFP rules,
stage, commit, or access real accounts.

Repository:

```text
C:\auto\작업7차\kiwoom-autotrade
```

Platform: native Windows. Date of this record: 2026-08-22, Asia/Seoul.

## Safety rules for the next AI

- Preserve the deliberately dirty worktree. Do not use broad `git add`,
  `git reset`, `git clean`, `git stash`, or a broad commit.
- Keep `kr_real` and `us_real` out of scope unless the operator gives fresh,
  explicit authorization for a specific action.
- Do not modify orders, ledgers, lifecycle records, reconciliation state,
  dashboard controls, pause state, credentials, or account configuration.
- Treat `RUNNING`, a PID, a socket, a dashboard response, or a passing test as
  time-specific evidence, not proof of trading health or broker truth.
- For literal-evidence requests, preserve complete command/log/diff output and
  separate confirmed facts from hypotheses.

## Progress summary

### Dashboard

- Dashboard per-worker launch and account routing were fixed and committed as
  `c0304e553a4f63c507e1c6e6d3fe6ae938556a2a` (`c0304e5`). The operator
  verified the four account links, including the two real-account view-only
  links, in the browser.
- The dashboard server binds to `127.0.0.1` and defaults to port `8765`.
  `DASHBOARD_PORT` can override only the port.
- Round 215 found current loopback reachability successful. Round 216 found
  that `::1:8765` fails, while `localhost` resolves to `127.0.0.1` and
  succeeds; the hosts file is normal.
- The operator subsequently tested exactly `http://127.0.0.1:8765` and
  confirmed it loaded correctly. The earlier discrepancy is classified as a
  stale URL or timing artifact, not a structural bind/configuration defect.
- No dashboard restart, bind change, firewall change, or configuration edit
  was performed during this investigation.

### Fixed-port HTTP and `WinError 10048`

- Commit `0702a06181ee885de05c0eb259a157930dc133ba` (`0702a06`, Round 184)
  changed only `src/core/broker_http.py` and
  `tests/test_broker_http.py`.
- The commit added close-completion signaling, serialized fixed-port
  reconnects, abortive `SO_LINGER` close behavior, and a 30-second HTTP
  keepalive expiry. Its own commit message explicitly says that it does not
  resolve the open `WinError 10048` issue.
- Focused verification recorded for that commit: `8 passed, 1 warning,
  2 subtests passed in 4.18s`.
- Round 217 inspected post-commit `logs/us_mock.log`. It found 42 matching
  entries after the commit timestamp, from `06:34:40` through `09:14:28` on
  2026-08-22. The representative failures were all:

  ```text
  phase=connect
  local=('0.0.0.0', 443)
  remote=('112.175.65.18', 443)
  errno=10048 winerror=10048
  ```

- Most sampled failures were followed by successful REST I/O approximately
  one second later. The worker identity remained stable where logged
  (`pid=16872`, instance `a7d11c49c3894d84ac631720c20ea039`).
- The current conclusion is residual fixed-port/TCP tuple release lag: the
  same broad mechanism targeted by Round 184, not a newly proven mechanism.
  The evidence does not justify claiming that the issue is eliminated.
- Round 217 produced the design-only proposal
  `PROPOSAL_v217_us_mock_10048_residual_mitigation.md`. It recommends a
  narrowly classified, bounded retry/backoff for this transient error as the
  first candidate. Moving US HTTP to an OS-assigned ephemeral source port is a
  separate structural fallback, not authorized for implementation.

### HTTP/WebSocket local-port separation

- The known KR same-process collision used HTTP local port `10000` and
  WebSocket local port `10000` in the earlier finding.
- Current US mock source usage is distinct:

  | Path | Local source port |
  |---|---:|
  | US HTTP | `443` |
  | US WebSocket | `10002` |

- Therefore the post-Round-184 US HTTP `10048` sample is not explained by the
  old US HTTP/WS same-port collision. Do not merge this evidence with the
  separate US WebSocket `10013` investigation.
- The US WebSocket `10013` issue remains unresolved/monitoring status. Its
  exact failing syscall in the externally launched worker was not proven.
  Codex-launched outbound-connect tests inherited
  `CODEX_SANDBOX_NETWORK_DISABLED=1` and are not valid evidence about normal
  worker connectivity.
- No new live WebSocket verification was performed in Round 217.

### Occurrence-count correction

- The Round 206–208 work corrected the recorded `WinError 10048` total to
  `1,361`. That was a counting correction, not a new fix or a new defect.
- Do not treat the corrected count as evidence that the post-Round-184
  mechanism is resolved.

### Reconciliation and lifecycle safeguards

The worktree contains separately authorized but uncommitted safeguards around
manual lifecycle and reconciliation behavior, including:

- idempotent manual-lifecycle activation and activation IDs;
- single adoption and fresh activation after orphan cleanup/re-enable;
- fail-closed `broker_quantity_unattributed` handling;
- recovery handling for terminal `filled` rows with zero durable fill quantity;
- protection against cancelling unresolved terminal rows; and
- persistent reconciliation-failure pause/clear behavior documented in the
  current operator records.

These changes are not part of the Round 184 fixed-port commit. Do not sweep
them into a networking or documentation commit.

The local `kr_mock` reconciliation case involving order `0149421` and symbol
`483350` remains fail-closed and unresolved when broker truth is unavailable.
Keep the order identifier and symbol separate. Do not rewrite, cancel, retry,
or manually reconcile the local records from local evidence alone.

## Current unresolved items

1. `us_mock` HTTP `WinError 10048` still occurs intermittently after the
   Round-184 hardening. The design proposal exists; implementation requires a
   separate authorization round.
2. US WebSocket `WinError 10013` remains unresolved and should be investigated
   with live-time capture if it recurs, not retrospective attribution to
   firewall/WFP or AhnLab without direct evidence.
3. The Aug-18 total-connection-failure episode remains circumstantially linked
   to a firewall snapshot but is not conclusively attributed.
4. The broker firewall-rule thread involving `codex_sandbox_*` is paused.
5. The restart-marker discrepancy (`17` found versus `18` previously reported)
   remains low priority and unresolved.
6. The port-`10002` remote-IP discrepancy (`112.175.65.18` versus `.65`) is
   unexplained, plausibly benign, and unconfirmed.
7. Authorized work in the tree remains uncommitted, including rate-limit
   observability, port separation, the v190 lifecycle work, and related
   proposals. No commit is implied by this record.
8. `kr_real`/`us_real` shared-app-key concurrency risk is deferred and out of
   scope.

## Working-tree state at handoff

The checkout was already dirty before this record. Existing modified files
include:

```text
src/core/engine.py
src/core/kiwoom_client.py
src/core/realtime_feed.py
src/core/token_manager.py
src/data/trade_ledger.py
src/main.py
tests/test_manual_tranche_lifecycle.py
```

Existing untracked work includes multiple operator records, handoffs,
proposals, diagnostics, rate-limit observability, lifecycle tests, and
`tests/test_realtime_feed.py`. The Round 217 proposal and this wrap-up are
also untracked. Preserve all of these paths unless a later request gives an
exact file-scoped staging/commit authorization.

## Instructions for the next AI

1. Read this record and the specific active round instruction file before
   acting. Treat older records as historical evidence.
2. Start with a read-only baseline:

   ```powershell
   Get-Date
   git status --short
   Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue
   Get-Process python -ErrorAction SilentlyContinue
   ```

3. For any `us_mock` networking investigation, capture exact timestamps,
   PID/instance, local/remote endpoints, error phase, and follow-up REST/WS
   evidence. Keep HTTP `10048` and WebSocket `10013` separate.
4. If implementation of the Round 217 proposal is authorized, limit the
   first change to the fixed-port HTTP boundary and focused tests. Do not
   alter WebSocket ports, account logic, reconciliation gates, or real-account
   behavior.
5. For repository verification, use the checkout's full-suite baseline:

   ```powershell
   pytest -q -p no:cacheprovider
   ```

   Report the literal result. Do not claim tests were run when they were not.
6. Do not restart a worker or dashboard server as part of diagnosis unless a
   separate round explicitly authorizes the exact account, process, and
   observation window.
7. Do not use firewall/WFP, registry, service, driver, or real-account actions
   to fill an evidence gap. Stop and report if elevation or operator input is
   required.

## Verification status for this wrap-up

- This turn created only this documentation file.
- No source, test, configuration, ledger, lifecycle, dashboard-control,
  firewall, service, driver, account, or process state was changed.
- No tests were run because this request was documentation-only.
- The file was checked for existence and the expected handoff sections after
  creation.
