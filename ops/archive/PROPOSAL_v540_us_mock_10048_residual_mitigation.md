# Round 540 — `us_mock` residual `WinError 10048` mitigation options

## Status and decision requested

This is a proposal only. It makes no source, worker, network, firewall, or
account change. The operator is asked to choose a direction for a separately
authorized design/implementation round; no option below is selected by this
document.

## Problem restatement

The current production mitigation retries fixed-port HTTP `WinError 10048` for
about 2.5 seconds (50 ms exponential backoff, capped at 400 ms). Round 539
aggregated 153 measurement probes across 29 cycles: 20 cycles still failed at
about 80 seconds after close before their first success near 160 seconds, eight
fine cycles first observed success at about 155--156 seconds, and one partial
cycle failed through 40 seconds. No first success was observed at or before
2.5 seconds. The measurement probes do not identify the exact release instant,
but the scale difference makes a longer in-place HTTP retry unsuitable as a
normal trading-worker stall. The existing retry remains useful for short
transient collisions; it masks, rather than removes, the long fixed-tuple
release condition.

## Fixed-port constraint

Current code selects local source port `443` for mock US HTTP in
`src/core/kiwoom_client.py`; the HTTP backend then calls `bind()` on that port.
There is no source-level evidence that broker protocol syntax requires local
port `443`. However, the archived machine handoff states: "The fixed source
ports are firewall-enforced requirements on this machine." That is a documented
operating constraint, not a current firewall-policy revalidation.

Accordingly, OS-assigned ephemeral ports, a replacement fixed port, and a
multi-port fallback are not deployable assumptions. Each requires an
operator-authorized confirmation that the relevant outbound source ports are
permitted and that broker or machine policy does not depend on `443`.

## Option A — remove the fixed local HTTP port, subject to policy confirmation

Use an OS-assigned ephemeral source port for mock US HTTP while retaining the
existing US WebSocket local port. This structurally avoids repeatedly reusing
the same local/remote TCP tuple, so it directly targets the measured release
lag rather than waiting through it.

| Aspect | Assessment |
|---|---|
| Cost / complexity | Medium: transport selection, focused tests, and an operator-authorized closed-session verification. |
| Risk | Medium to high until firewall and broker-policy compatibility is demonstrated. It changes a deliberate source-port contract. |
| Fixed-port compatibility | No. Blocked unless the documented firewall requirement can be relaxed or updated. |
| Evidence strength | High for collision avoidance in principle; no current evidence that this machine permits it. |

## Option B — retain the fixed port but avoid immediate reuse

Record the last fixed-port HTTP close and, while the release state is plausibly
unavailable, avoid starting a blocking connect/retry loop. The caller would
need a separately designed interim policy: queue the request, use an approved
alternate transport/port, or return a classified unavailable result to the
existing failure-handling path.

The measurement corpus shows that a conservative fixed-port holdoff could be
on the order of minutes, not seconds. Therefore this is not a transparent
latency improvement: it trades `WinError 10048` for explicit delayed or
degraded behavior.

| Aspect | Assessment |
|---|---|
| Cost / complexity | Medium to high: close-state ownership, caller semantics, cancellation, and order/reconciliation safety all need design. |
| Risk | High if queued trading/reconciliation work is silently delayed or reordered. |
| Fixed-port compatibility | Yes, if no alternate port is used. |
| Evidence strength | High that immediate reuse is risky; insufficient to choose a safe holdoff duration from the coarse probe schedule alone. |

## Option C — keep short masking, add an explicit degraded/fail-fast path

Keep the existing short bounded retry for the fast-recovery case, but treat
exhaustion as an explicit fixed-port-unavailable outcome rather than trying to
wait for a minutes-long recovery inside the HTTP call. A separately designed
policy could route that outcome through existing retryable, reconciliation, or
fail-closed controls with visible observability and no hidden order submission.

| Aspect | Assessment |
|---|---|
| Cost / complexity | Medium: needs an exact error boundary and account/order-safe caller behavior. |
| Risk | Medium: preserves responsiveness, but may defer work and must not weaken fail-closed controls. |
| Fixed-port compatibility | Yes. |
| Evidence strength | High for avoiding a 150-second blocking call; does not eliminate the underlying collision. |

## Option D — change socket-close or Windows socket-option behavior

The current backend already sets `SO_REUSEADDR` before `bind()`, serializes
connects, waits for close completion, and uses abortive `SO_LINGER(1, 0)` when
the fixed-port HTTP stream closes. These controls did not prevent the measured
post-close unavailability. `SO_EXCLUSIVEADDRUSE` changes Windows bind-sharing
semantics, but the observed failure is at `connect()` on a reused tuple, so it
is not established as a remedy. Likewise, further linger or close-timing
tuning cannot be assumed to control Windows' TCP tuple-release policy.

| Aspect | Assessment |
|---|---|
| Cost / complexity | Medium to high: isolated reproducer work and Windows-specific validation are required. |
| Risk | Medium to high: can change close semantics or worsen availability without solving tuple reuse. |
| Fixed-port compatibility | Yes. |
| Evidence strength | Low for a specific option change; current evidence argues against speculative tuning. |

## Option E — approved alternate fixed-port or port-pool fallback

If policy requires fixed source ports but permits more than one, use a
pre-approved alternate port when the primary port is unavailable. This avoids
waiting for the old tuple, but only if every candidate port is allowed by the
machine firewall and accepted by the broker environment.

| Aspect | Assessment |
|---|---|
| Cost / complexity | High: port selection, persistence, observability, and policy verification. |
| Risk | High until policy compatibility is proven; fallback can create a new source-port contract. |
| Fixed-port compatibility | Conditional: compatible only with an operator-confirmed approved port set. |
| Evidence strength | High for avoiding a single-port reuse collision; no evidence that an alternate approved port exists. |

## Recommendations for operator decision

1. First decide whether the documented firewall-enforced source-port policy can
   permit ephemeral US HTTP ports or a small approved fixed-port set. If yes,
   Option A or Option E is the most direct way to avoid the collision.
2. If the `443` requirement is immutable, prefer a separately designed
   combination of Option C (short, visible fail-fast/degraded handling) and
   Option B (explicit no-immediate-reuse policy) over extending the current
   in-place retry to minutes.
3. Keep Option D investigative only unless an isolated Windows reproducer
   produces evidence for one specific socket/close change.

No implementation, firewall change, worker restart, or source-port change is
authorized by this proposal.
