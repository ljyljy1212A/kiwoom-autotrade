# Round 217 — `us_mock` post-fix `WinError 10048` mitigation proposal

## Status and scope

Design proposal only. No source, configuration, worker, firewall, account,
or network changes are authorized by this document. No implementation,
restart, staging, or commit is authorized; those require a separate approval
round.

## 1. Step 1 — post-fix sample and context

Round 184 commit `0702a06` was committed at `2026-08-22 06:31:37 +0900`.
Its own commit message says it added close-completion signaling, abortive
stream close, serialized reconnect, and a 30-second keepalive expiry, but did
not resolve the open `WinError 10048` issue.

The post-commit `logs/us_mock.log` contains 42 matching `10048` entries from
`2026-08-22 06:34:40` through `09:14:28`. Because the commit occurred on
2026-08-22, there are no post-commit entries on earlier dates; the sample is
therefore spread across the available post-commit time window, not across
multiple calendar days.

Representative sample:

| Time | Failure context | Follow-up |
|---|---|---|
| 06:34:40 | HTTP `phase=connect`, local `0.0.0.0:443`, remote `112.175.65.18:443`; PID field on the error is `-` | REST write/read success at 06:34:41; worker PID `16872`, instance `a7d11c49c3894d84ac631720c20ea039` |
| 06:46:44 | Same HTTP connect tuple | Another 10048 at 06:46:45 and 06:46:47; reconciliation recorded one consecutive cycle failure |
| 06:55:47 | Same HTTP connect tuple | REST write/read success at 06:55:48 |
| 07:10:52 | Same HTTP connect tuple | REST write/read success at 07:10:53 |
| 07:22:56 | Same HTTP connect tuple | REST write/read success at 07:22:57 |
| 07:38:01 | Same HTTP connect tuple | REST write/read success at 07:38:02 |
| 07:56:06 | Same HTTP connect tuple | REST write/read success at 07:56:07 |
| 08:14:10 | Same HTTP connect tuple | REST write/read success at 08:14:11 |
| 08:26:14 | Same HTTP connect tuple | REST write/read success at 08:26:15 |
| 08:44:19 | Same HTTP connect tuple | REST write/read success at 08:44:20 |
| 08:59:24 | Same HTTP connect tuple | REST write/read success at 08:59:25 |
| 09:14:28 | Same HTTP connect tuple | No later line was available in the bounded log snapshot to classify the follow-up |

The failures are distributed through the observation window rather than
clustered at one market-open or market-close boundary. The dominant pattern
is a transient single-cycle HTTP connect failure followed by successful REST
I/O approximately one second later. The 06:46:44–06:46:47 sequence is a
shorter failure run, not evidence of a permanently failed worker. The worker
identity remains stable where logged.

## 2. Step 2 — mechanism determination

### Round 184 relationship

The post-fix errors are the same broad fixed-port HTTP reconnect failure
category that Round 184 targeted, but the mitigation did not eliminate the
kernel-level release lag. The evidence is literal and unchanged in shape:

```text
Fixed-port HTTP socket failure: phase=connect
local=('0.0.0.0', 443) remote=('112.175.65.18', 443)
errno=10048 winerror=10048
```

The errors occur after the Round 184 close wait and still recover on a later
attempt. This is best classified as the same residual fixed-port/TCP tuple
reuse mechanism, not a newly identified failure category.

### US HTTP versus WebSocket port usage

The current code confirms separate US mock local source ports:

| Path | Current local source port |
|---|---:|
| US HTTP | `443` |
| US WebSocket | `10002` |

The WebSocket path still uses `SO_REUSEADDR`, binds its configured local port,
and then connects. Since it now uses `10002`, it does not share local port
`443` with US HTTP. The known KR same-process HTTP/WS collision therefore does
not explain this post-commit US HTTP sample.

## 3. Step 3 — mitigation design

### Candidate A — targeted bounded retry/backoff for fixed-port `10048`

Retry only the identified fixed-port connect failure with a small bounded
exponential delay, while preserving the existing request/reconciliation
failure gates and logging each attempt. Trade-off: smallest behavior change and
fits the observed transient recovery, but it masks rather than removes the
underlying kernel release delay and increases request latency during a failure.

### Candidate B — stop forcing US HTTP to local source port `443`

Use an OS-assigned ephemeral local source port for US mock HTTP while keeping
US WebSocket local port `10002`. Trade-off: structurally removes fixed-tuple
reuse for US HTTP, but changes the deliberate local-port contract and requires
separate verification that no operator or machine policy depends on local
`443`.

### Candidate C — further socket-option or close-timing changes

Investigate a different Windows socket policy (`SO_EXCLUSIVEADDRUSE`, option
ordering, or a longer close-completion wait) and apply it only after an
isolated reproducer distinguishes the relevant bind/connect state. Trade-off:
could address a specific Windows lifecycle detail, but current logs do not
justify the option choice, and more waiting would increase outage latency
without proving that the kernel release is observable from Python.

### Recommendation

Recommend Candidate A first, with a strict bounded retry budget and no change
to account, reconciliation, WebSocket, or real-account behavior. The sample
shows recovery on the next cycle/attempt, so a narrowly classified retry is
proportionate and can be verified without changing the current port topology.

Candidate B should remain the fallback structural option if separately
authorized tests show that retries are insufficient or the operator requires
elimination of fixed local-port reuse. Candidate C should not be implemented
speculatively.

If authorized, the likely implementation surface is the fixed-port HTTP
transport/request boundary in `src/core/broker_http.py`, plus focused tests in
`tests/test_broker_http.py`. No change should be made to `src/core/realtime_feed.py`
or to real-account port selection for this proposal.

## Required separate verification if implementation is authorized

1. Reproduce the fixed-port `10048` path in an isolated local test and verify
   that only the intended error is retried.
2. Verify bounded retry count, delay, cancellation, and final error behavior.
3. Run the focused broker HTTP tests and the repository's normal full-suite
   command, reporting literal results.
4. In a separately authorized closed-session `us_mock` observation, verify
   stable PID/instance, successful REST continuation, reconciliation safety,
   no repeated unclassified failures, and unchanged US WebSocket `10002`
   behavior. Do not involve `kr_real` or `us_real`.

This document is a design proposal only. A separate authorization round is
required before any implementation, test involving live workers, restart,
staging, or commit.

## 4. Blockers

No required read-only sub-step was blocked. No source/configuration/runtime
state was changed.
