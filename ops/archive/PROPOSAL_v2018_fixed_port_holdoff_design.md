# Proposal v2018 — Fixed-Port Holdoff and Fail-Fast Design

Status: design only. This document does not authorize implementation, tests,
configuration changes, worker actions, firewall actions, staging, commit, or
push.

## Scope and evidence boundary

This design applies only to mock fixed-port HTTP: KR mock local port `10000`
and US mock local port `443`. It does not change either port and does not add a
fallback port. The measured release-lag evidence supplied by Round 2019 is
80–160 seconds; the current retry budget is approximately nine seconds.

The exact Option B text from
`ops/archive/PROPOSAL_v540_us_mock_10048_residual_mitigation.md` is:

> ## Option B — retain the fixed port but avoid immediate reuse
>
> Record the last fixed-port HTTP close and, while the release state is plausibly
> unavailable, avoid starting a blocking connect/retry loop. The caller would
> need a separately designed interim policy: queue the request, use an approved
> alternate transport/port, or return a classified unavailable result to the
> existing failure-handling path.
>
> The measurement corpus shows that a conservative fixed-port holdoff could be
> on the order of minutes, not seconds. Therefore this is not a transparent
> latency improvement: it trades `WinError 10048` for explicit delayed or
> degraded behavior.

The proposal's alternate transport/port branch is not part of this design.

## Current implementation facts

- `FixedPortCollisionError` is raised only after `_connect_with_reuseaddr()`
  sees address-in-use errors and the bounded retry budget expires:
  `src/core/broker_http.py:434-480`, `:484-517`.
- The fixed backend waits for the prior close-completion event, then invokes
  `_connect_with_reuseaddr()` under `_connect_lock`:
  `src/core/broker_http.py:558-613`.
- `FixedPortAsyncHTTPTransport` owns a one-connection pool, and
  `BrokerHTTPGate` serializes the worker's fixed-port client lifecycle:
  `src/core/broker_http.py:616-654`.
- Mock port selection is in `KiwoomClient.__init__`; non-mock mode passes
  `None` to `BrokerHTTPGate`: `src/core/kiwoom_client.py:101-131`.
- The current network-error boundary records account-scoped degraded state
  only for an eligible mock account and a causally wrapped
  `FixedPortCollisionError`, then raises `RetryableError`:
  `src/core/kiwoom_client.py:213-238`.
- Existing degraded state is account-keyed, persisted, and has a recovery-probe
  timestamp: `src/core/broker_http.py:247-259`, `:273-375`, `:378-426`.

## Proposed mechanism

1. At the existing `FixedPortCollisionError` boundary, after retry exhaustion
   and before another request is allowed to enter the fixed-port connect loop,
   enter a holdoff record for the account and its selected local port.
2. The recommended holdoff duration is exactly **160 seconds** from the
   collision timestamp. This is the conservative upper edge of the measured
   80–160 second release-lag range and is long enough to avoid treating a
   nine-second retry budget as a substitute for the observed release window.
   It is a design recommendation, not a claim that 160 seconds is a proven OS
   release guarantee.
3. Track the state per account/fixed-port lifecycle, using the existing
   account-scoped degraded-state model rather than a process-wide or
   port-global flag. The state must include the account, local port, collision
   time, holdoff expiry, and operation label. Persistence should remain
   account-scoped so a worker restart cannot silently erase a known unavailable
   interval; the current persistence helpers are at
   `src/core/broker_http.py:273-375`.
4. Before opening the transport connection, consult the holdoff state. If the
   holdoff is active, do not call `_connect_with_reuseaddr()` and do not enter
   its blocking retry loop. If it has expired, clear only the active holdoff
   marker and permit one normal bounded probe. A new exhausted collision starts
   a fresh 160-second interval.
5. The trigger must remain narrowly classified: address-in-use exhaustion
   represented by `FixedPortCollisionError` or its causal chain. Bind,
   DNS, timeout, TLS, HTTP-status, and unrelated `OSError` failures must not
   enter this holdoff. The existing classifier and causal-chain walk are at
   `src/core/broker_http.py:193-203`, `:262-270`, and the non-collision branch is
   visible at `src/core/broker_http.py:535-555`.

## Degraded-cycle handling

An active-holdoff call returns a classified fixed-port-unavailable outcome to
the existing failure path; it does not return an empty successful response and
does not submit or replay an order. The implementation should preserve the
current external `RetryableError` contract unless a separately authorized
implementation round proves a distinct internal result type is necessary.

The log should be a warning containing `account`, `market`, `local_port`,
`operation`, `holdoff_until`, and `skipped=true`, explicitly stating that no
broker request was sent. Holdoff entry should log the collision and the same
expiry. Repeated skips should be rate-limited by the existing ongoing-status
cadence rather than emitting one alert per polling tick.

For reconciliation, `sync_broker_state()` already treats `RetryableError` as
an incomplete cycle, records reconciliation failure, and returns `False` in
both the passive and active balance paths:
`src/core/engine.py:1292-1339` and `:1413-1448`. That is the correct degraded
semantics: preserve the last confirmed state and fail closed for decisions.
The outer passive monitor catches the exception and continues to its next
sleep interval: `src/main.py:297-315`.

For order execution, `AccountEngine._dispatch_order()` currently logs and
notifies `Order failed` for `RetryableError`, without changing position state:
`src/core/engine.py:1233-1263`. The design must not convert a skipped order
into an accepted or pending order. A later implementation may improve the
message to say `fixed-port holdoff`, but callers do not need a success-versus-
failure semantic change for this design.

## Affected files and function-level scope

- `src/core/broker_http.py`: holdoff state representation and lookup; the
  `FixedPortCollisionError` exhaustion boundary; `_FixedPortAnyIOBackend.connect_tcp()`
  before `_connect_with_reuseaddr()`; `FixedPortAsyncHTTPTransport`; and
  `BrokerHTTPGate` state ownership. Preserve the existing non-collision error
  path and one-connection/lock behavior.
- `src/core/kiwoom_client.py`: retain the existing causal collision gate in
  `_post_once()` and ensure the classified holdoff outcome continues to raise
  `RetryableError`; `place_order()` must retain its unattributed-attempt safety
  handling at `src/core/kiwoom_client.py:280-349`.
- `src/core/engine.py`: no behavior change is required by this design. The
  relevant compatibility points are `_shared_broker_balance()`
  (`:674-685`), `sync_broker_state()` (`:1292-1448`), and order dispatch
  (`:1233-1263`). A future implementation may add only explicit degraded-skip
  observability here.
- `src/main.py`: no polling-policy change is authorized. The passive monitor
  boundary is `:297-315` and must continue to survive a skipped/deferred cycle.

## Balance-monitor cadence discrepancy

The literal constructor default is `poll_interval_sec: int = 5` at
`src/core/engine.py:420-425`. The reconciliation interval literal is:

```python
os.environ.get("BALANCE_RECONCILE_SEC", str(max(poll_interval_sec, 10)))
```

at `src/core/engine.py:552-557`, so its default is 10 seconds when the poll
default is 5 seconds. The monitor log prints `monitor.poll_interval_sec` at
`src/main.py:307-309`, and the open-session sleep returns that value at
`src/main.py:318-321`; closed-session sleep is capped at 180 seconds.

Therefore the previously observed approximately three-minute exhaustion
cadence is not explained by the five-second poll value alone. It remains an
unresolved outer scheduling/runtime discrepancy. A 160-second holdoff would
not be guaranteed to expire before the next open-session five-second poll; the
next polls must skip cleanly until expiry. No cadence change is proposed here.

## Shared transport call-site assessment

The same `BrokerHTTPGate` is shared by token requests in
`src/core/token_manager.py:65-69` and `:132-135`, general REST calls in
`src/core/kiwoom_client.py:213-238`, orders in `:280-349`, cancellation in
`:351-362`, balances in `:367-375`, and quotes through `_post()` at
`src/core/kiwoom_client.py:441-475`. Consequently, a holdoff placed at the
transport/gate boundary affects more than `*-balance-monitor`; it also covers
token, quote, order, cancel, and other REST calls using that account's fixed
transport.

The five reviewed exhaustion events were balance-monitor events, but that is
an observation about the reviewed logs, not proof that other callers can never
collide. The holdoff therefore must be transport/account aware, while order
callers must remain fail-closed and must not replay an ambiguous order.

## Test plan for a later implementation round

- Enter holdoff only after `FixedPortCollisionError` retry exhaustion.
- Do not enter holdoff for bind, DNS, timeout, TLS, HTTP-status, or unrelated
  socket errors.
- Skip an active-holdoff connection without calling
  `_connect_with_reuseaddr()` and emit the required structured warning.
- Verify the exact 160-second expiry boundary using an injected clock.
- Permit one normal bounded probe after expiry; re-enter holdoff on a new
  collision.
- Verify account/port isolation and persistence/restore behavior.
- Verify passive balance reconciliation returns `False`, preserves the prior
  confirmed state, and continues on the next monitor iteration.
- Verify order failure remains fail-closed, marks any required ambiguous
  attempt state, and never creates an accepted pending order from a skipped
  call.
- Verify token, quote, balance, order, and cancellation call sites share the
  intended holdoff boundary.
- Verify non-mock mode still passes `None` to `BrokerHTTPGate` and never
  instantiates `FixedPortAsyncHTTPTransport`.

## Risk assessment and exclusions

The design is mock-only because the fixed-port selection is conditional on
`mode == "mock"` in `src/core/kiwoom_client.py:112-121`; real mode receives
`None`, and `BrokerHTTPGate.client()` selects a normal `httpx.AsyncClient` at
`src/core/broker_http.py:642-647`. No real-mode behavior is intended to change.

The design retains local ports `10000` and `443`, introduces no port fallback,
and does not relocate or remove a fixed port. Port relocation/removal and
alternate-port fallback are outside this Option B design.

## Firewall/source-port documentation re-check

Command executed from the repository root:

```text
rg -g '!.venv' -g '!pytest_tmp' -g '!.pytest_cache' -g '!pytest-tmp' -g '!node_modules' -i "firewall|router|nat\b|port.?forward|source.?port" -- .
```

Verbatim output:

```text
.\ops\archive\DESIGN_v542_us_mock_10048_failfast_degraded_mode.md:This is a design record only.  It makes no source, firewall, process, Git, or
.\ops\archive\DESIGN_v542_us_mock_10048_failfast_degraded_mode.md:with no firewall-policy change implied or authorized by this document.
.\ops\archive\HANDOFF_ACCOUNT_LOCK_BYPASS_NETWORK_DIAGNOSTICS.md:The decisive firewall rule is present and enabled:
.\ops\archive\HANDOFF_NEXT_AI_HTTP_WS_PORT_COLLISION.md:- Do not change firewall/WFP configuration.
.\ops\archive\HANDOFF_NEXT_AI_HTTP_WS_PORT_COLLISION.md:f72ca75 fix: bind mock broker HTTP source ports
.\ops\archive\HANDOFF_NEXT_AI_PROGRESS.md:Verify that the mock KR worker can use fixed local source port `10000` for HTTP
.\ops\archive\HANDOFF_NEXT_AI_PROGRESS.md:  firewall/WFP configuration.
.\ops\archive\HANDOFF_NEXT_AI_PROGRESS.md:credentials, broker orders, and firewall/WFP configuration were not touched.
.\ops\archive\INSTRUCTIONS_NEXT_AI_POST_ISSUE3_CLOSURE.md:workers. Do not change firewall/WFP settings. Do not access credentials. Do
.\ops\archive\INSTRUCTIONS_NEXT_AI_POST_ISSUE3_CLOSURE.md:- The fixed source ports are firewall-enforced requirements on this machine.
.\ops\archive\OPERATOR_RECORD_closing_issue3_v36-v55.md:- The fixed HTTP source port `10000` and fixed WebSocket source port `443`
.\ops\archive\OPERATOR_RECORD_closing_issue3_v36-v55.md:- Active firewall rules permit those source-port exceptions to the broker
.\ops\archive\OPERATOR_RECORD_closing_issue3_v36-v55.md:  host and block the other local source-port ranges.
.\ops\archive\OPERATOR_RECORD_wrapup_v10-v124.md:- Firewall evidence shows outbound access for source port `443` and broker remote port `10000`, while the rule `codex_sandbox_offline_block_kiwoom_other_ports` blocks local ports `10001-65535` and other listed ranges for `112.175.65.18`.
.\ops\archive\OPERATOR_RECORD_wrapup_v10-v119.md:- Firewall evidence shows local source ports 443 and 10000 are permitted to
.\ops\archive\OPERATOR_RECORD_wrapup_v10-v119.md:  the broker host while the range containing 10001 is blocked. Firewall API
.\ops\archive\OPERATOR_RECORD_wrapup_v10-v132.md:Therefore the fixed local-port arrangement is constrained by the shared-machine firewall state, not solely an independent application design choice.
.\ops\archive\PROPOSAL_v103_kr_mock_port_separation.md:The HTTP path therefore binds local source port `10000` for mock KR.
.\ops\archive\PROPOSAL_v148_us_mock_port_separation.md:For mock US, HTTP local source port is therefore `443`. The fixed-port transport binds with:
.\ops\archive\PROPOSAL_v217_us_mock_10048_residual_mitigation.md:Design proposal only. No source, configuration, worker, firewall, account,
.\ops\archive\PROPOSAL_v540_us_mock_10048_residual_mitigation.md:Current code selects local source port `443` for mock US HTTP in
.\ops\archive\PROPOSAL_v540_us_mock_10048_residual_mitigation.md:ports are firewall-enforced requirements on this machine." That is a documented
.\ops\archive\PROPOSAL_v540_us_mock_10048_residual_mitigation.md:operator-authorized confirmation that the relevant outbound source ports are
.\ops\archive\PROPOSAL_v69_session_aware_balance_monitor_interval.md:workers, WebSocket connections, firewall/WFP settings, credentials, staging,
.\src\core\broker_http.py:"""Broker HTTP transport with worker-scoped source-port binding."""
.\src\core\broker_http.py:# Windows can retain a fixed source-port tuple briefly after a pooled connection
.\src\core\broker_http.py:    """Serialize one worker's broker HTTP lifecycle and bind its source port."""
```

This repository evidence does change the narrow factual answer: archived
records document a firewall-enforced local-source-port constraint and explicitly
describe `443` and `10000` as permitted exceptions. It does not establish the
current live rule state, so operator confirmation remains required before any
network or firewall action. The holdoff design itself does not require such an
action.

## Completion boundary

This document is the only intended artifact for the design round. Existing
unrelated worktree modifications must remain untouched. Any implementation,
test execution, worker restart, firewall/network action, staging, commit, or
push requires a separate explicit authorization.
