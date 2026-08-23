# Proposal v91: Kiwoom Rate-Limit Observability

Status: design proposal only. No implementation is authorized by this
document.

## Purpose

Add lightweight, structured observability for rate-limit and existing
throttle/backoff events while leaving the current per-component gates and
backoff behavior unchanged. The proposal is limited to `kr_mock` and
`us_mock`, matching the scope used for the Part 3 balance-monitor change.

The three documented quota tiers remain settled: `1700` is per-TR,
`1701` is total, and `1702` is group quota. The evidence narrows, but does
not eliminate, the residual possibility of an undocumented host/IP layer.
The mock workers have distinct app keys. By contrast, `kr_real` and `us_real`
currently share an app key; that fact is a future reopen trigger, not a design
requirement or implementation target in this proposal.

## 1. Event data to record

Every rate-limit-relevant response or existing throttle/backoff trigger that
is already handled by the current code should emit one structured warning
event or increment one equivalent counter at the point where the existing
handling occurs. The event must identify the event without creating a new
decision or retry path.

The canonical event name is `kiwoom_rate_limit_event`. Its fields are:

| Field | Required value |
|---|---|
| `event` | `kiwoom_rate_limit_event` |
| `timestamp` | Logger-generated timestamp in the existing log timezone/format |
| `market` | `KR` or `US` |
| `mode` | `mock` for this proposal's enabled scope |
| `account_id` | The client account identifier (`account_no`); do not include unrelated account data |
| `api_id` | Kiwoom TR/API ID from the exception or request, when available |
| `status_code` | HTTP/status or broker return code, when available; preserve `429` as `429` |
| `quota_tier` | `1700`, `1701`, `1702`, or `none` when the event is a throttle/backoff trigger without a documented tier |
| `trigger` | Stable category such as `http_429`, `quota_1700`, `quota_1701`, `quota_1702`, `token_backoff`, `retryable_backoff`, or `balance_reconciliation_deferred` |
| `cooldown_sec` | Existing cooldown selected by the current handler, when one exists; otherwise `null` |
| `appkey_fingerprint` | First 12 hexadecimal characters of `SHA-256(appkey UTF-8 bytes)` |

The event may also include a bounded, sanitized symbol or endpoint field when
useful for diagnosis, but it must not include request bodies, authorization
headers, access tokens, secret keys, raw app keys, or unbounded response text.
The documented quota fields should be parsed only for classification; failure
to parse them must leave `quota_tier` as `none` and must not alter handling.

Classification rules are deliberately narrow:

- `1700`, `1701`, and `1702` are classified by substring-checking
  `str(exc)` (the exception's string representation); they map to the
  corresponding `quota_tier`.
- HTTP `429` is classified from `exc.return_code` and maps to
  `status_code=429`; if the same exception string also identifies a documented
  tier, retain that tier, otherwise use `quota_tier=none`. `KiwoomAPIError`
  does not expose a separate `.message` attribute.
- A current existing throttle/backoff action without one of those tiers is
  still observable with `quota_tier=none` and its stable `trigger` category.
- One event is emitted for the response/trigger already being handled. The
  instrumentation must not log every caller's subsequent circuit-open refusal
  as a new broker rate-limit response.

## 2. Existing hook locations

Instrumentation is added inside existing exception/branch paths; it does not
introduce a second detector.

### Quote REST path

The primary hook is
`src/core/kiwoom_client.py:436-449`, function
`KiwoomClient._record_quote_failure`. This function already receives the
`KiwoomAPIError`, builds `message = str(exc)`, reads `exc.return_code`, and
handles HTTP `429` or message code `1700` by opening the existing
account/domain-wide quote circuit and applying the existing exponential
backoff. The structured event is emitted immediately before or alongside the
existing warning, using the same calculated `cooldown` and then returning
through the same branch. Classification must continue to use the string
representation; there is no `.message` attribute to read.

The existing `RetryableError` branch in
`KiwoomClient._get_quote_limited`, approximately lines 426-430, already applies
a bounded cooldown for network/5xx failures. If this is included in the
rate-limit observability category, it emits the same schema with
`quota_tier=none` and `trigger=retryable_backoff`; it does not change the
existing `gate.not_before` calculation.

### Token issuance path

`src/core/token_manager.py:57-63`, function `TokenManager._issue`, already
handles HTTP `429`, records the existing token cooldown, logs a warning,
increases the existing bounded backoff, and raises the existing
`RetryableError`. The proposal adds the same structured event at that branch,
with `api_id=au10001`, `trigger=token_backoff`, `quota_tier=none` unless the
response identifies `1700`/`1701`/`1702`, and the existing cooldown value.
The exception and retry behavior remain unchanged.

### Balance reconciliation path

`src/core/engine.py:769-775` already catches `KiwoomAPIError` during execution
history reconciliation and treats `ka10076` with `429` or `1700` as a
rate-limited query, logs the deferral, and leaves the data unavailable for the
current poll. The event is added at that existing branch with
`trigger=balance_reconciliation_deferred`, the exception's `api_id` and
status/return code, and the parsed quota tier. The current defer-to-next-poll
behavior remains exactly the same.

`src/core/engine.py:745-750` is the balance-only reconciliation branch. It
checks `429`/`1700` in `str(exc)`, calls `_record_balance_rate_limit()`, logs a
deferral warning, and returns `False`. The structured event is added at this
existing branch with the same balance-deferral trigger and without changing
that behavior.

`src/core/engine.py:810-818` is the normal balance-reconciliation branch. It
also checks `429`/`1700` in `str(exc)`, logs a deferral warning, calls
`_record_balance_rate_limit()`, and returns `False`. The structured event is
added at this existing branch without changing its behavior.

### Order-cancellation path: explicitly scoped additional hook

`src/core/engine.py:880-898` catches
`(OrderRejectedError, RetryableError, KiwoomAPIError)` while cancelling stale
orders. Its current branch does not independently detect or classify `429` or
`1700`; it logs the failed cancellation, notes that a quota response should
not cause other pending cancellations to retry in the same tick, and returns.
If implementation is separately authorized, this location is an explicitly
scoped fourth hook: it must check the existing exception string for
`429`/`1700`/`1701`/`1702` and emit the structured event only when one is
present, without adding retry or cancellation behavior. The event must not be
emitted for unrelated cancellation failures.

The FX refresh deferral at `src/core/engine.py:1682-1683` remains out of scope:
it catches generic API/retry failures but has no rate-limit-specific
classification or backoff handling. The instrumentation should otherwise
remain at the existing broker-response handling branches so one response is
not counted once per higher-level caller.

## 3. Explicit non-changes

This proposal is instrumentation only.

- No cross-process coordination is added.
- No shared file, socket, database, IPC channel, or shared token bucket is
  added.
- No gate interval, cooldown, retry count, exponential-backoff limit, or
  circuit-open behavior changes.
- No new request suppression, retry, fallback, or order-control path is
  added.
- Normal operation produces no rate-limit event and behaves exactly as it does
  today.
- Existing per-component, account/market-isolated gates for `kr_mock` and
  `us_mock` remain unchanged.
- No worker is restarted or otherwise operated as part of this proposal.
- No firewall/WFP setting, credential, live account, or broker configuration
  is changed.

## 4. Credential safety

The only app-key value permitted in the event is
`appkey_fingerprint = SHA-256(appkey UTF-8 bytes)[:12]`, rendered as lowercase
hexadecimal. The raw app key and secret key are never interpolated into the
event, exception text, supplemental fields, or a log label. Access tokens and
authorization headers are also excluded. Request/response payloads are not
logged. A short one-way digest is for correlation only and is not a usable
Kiwoom credential.

The same redaction rule applies identically if this existing logging path is
ever reached by `kr_real` or `us_real` in the future. This proposal does not
enable or modify those real-account workers; it requires only that any future
reuse of the helper preserve the same no-raw-credential contract.

## 5. Scope boundary

Implementation, if separately authorized, is restricted to the mock worker
paths for `kr_mock` and `us_mock`, matching the Part 3 boundary that scoped the
balance-monitor change strictly to its one authorized loop. No real-account
runtime behavior is changed by this proposal, and no real-account worker is
started, stopped, or restarted.

The logging helper may be designed as a reusable, mode-aware utility so its
credential-safety contract cannot be bypassed accidentally, but the enabled
call sites and deployment target for this round remain mock-only.

## 6. Real-account reopen condition

The cross-process-coordination question remains closed for the current mock
deployment because `kr_mock` and `us_mock` use distinct app keys and this
proposal adds no shared state. It must be revisited if `kr_real` and `us_real`
are ever configured to run concurrently while sharing their current app key.
At that point, the shared-quota question becomes live rather than
hypothetical, and a separately authorized design decision is required before
changing coordination or limiter behavior. That condition is a documented
reopen trigger, not a requirement of this observability-only proposal.

## Verification criteria for a future implementation

This document is not an implementation and therefore requires no runtime
verification. If implementation is separately authorized, verification must
confirm all of the following without changing live-account state:

1. Unit tests exercise `429`, `1700`, `1701`, `1702`, token `429`, and the
   existing balance-deferral branches at `engine.py:745-750`,
   `engine.py:769-775`, and `engine.py:810-818`; they also exercise the
   explicitly scoped order-cancellation hook at `engine.py:880-898`. Each
   produces the expected structured fields and tier classification where
   applicable.
2. Existing quote, token, retry, and balance-gate tests continue to pass with
   identical cooldowns, retry counts, circuit state, and raised exceptions.
3. A log inspection confirms that no raw app key, secret key, token,
   authorization header, or request body is emitted.
4. Mock-only smoke verification confirms one event per handled response and no
   new cross-process file/socket/shared-state activity.
5. No `kr_real` or `us_real` worker is started or modified.

Implementation, any live-effect step, and any eventual commit remain separate
and individually authorized steps, following the same proposal-first
progression used for Part 3.
