# Proposal v103: Separate `kr_mock` HTTP and WebSocket Source Ports

## Status

Design proposal only. No source implementation, worker restart, test run
against live workers, firewall change, or commit is authorized by v103.

## Problem statement

`kr_mock` uses the same local source port for its REST HTTP transport and its
real-time WebSocket transport. When the WebSocket owns that port, an HTTP
connection attempt in the same process can fail during `bind()` with
`WinError 10048`. This is a same-process local-port collision and is distinct
from the previously documented restart/TCP `TIME_WAIT` condition.

`kr_mock` was stopped cleanly before this proposal was created and must remain
stopped until implementation and a separately authorized restart-and-verify
round are complete.

## Exact current configuration

### KR HTTP

`src/core/kiwoom_client.py:112` sets the mock KR HTTP source port directly:

```python
http_port = 10000 if mode == "mock" and market == "KR" else 443 if mode == "mock" and market == "US" else None
```

That value is passed to `BrokerHTTPGate` at `src/core/kiwoom_client.py:113`.
The gate passes its `local_port` to `FixedPortAsyncHTTPTransport` at
`src/core/broker_http.py:214`. The socket is bound at
`src/core/broker_http.py:121`:

```python
sock.bind((bind_address, local_port))
```

The HTTP path therefore binds local source port `10000` for mock KR.

### KR WebSocket

`src/core/realtime_feed.py:122-125` returns the WebSocket local source port:

```python
if self.client.market == "KR":
    return 10000
```

The socket is bound at `src/core/realtime_feed.py:60`:

```python
sock.bind((bind_address, local_port))
```

The WebSocket path therefore also binds local source port `10000` for mock KR.
The two values are not read from one shared configuration field; they are
separate hard-coded selections that happen to be identical.

### US comparison

The same constructor expression at `src/core/kiwoom_client.py:112` selects
local HTTP source port `443` for mock US. The WebSocket property at
`src/core/realtime_feed.py:124-126` selects local WebSocket source port `443`
for mock US. Therefore US has the same latent same-process collision pattern,
with `443` used by both paths.

The remote WebSocket defaults are independent of the local source-port
selection: `src/core/realtime_feed.py:38-39` use remote URL port `10000` for
both real and mock WebSocket endpoints. Changing a local source port must not
change those remote URLs.

## Proposed KR design

Keep the KR HTTP local source port at `10000` and assign the KR WebSocket a
different fixed local source port, proposed as `10001`:

| Path | Current local source port | Proposed local source port |
|---|---:|---:|
| `kr_mock` REST HTTP | `10000` | `10000` |
| `kr_mock` WebSocket | `10000` | `10001` |

The eventual implementation should make the two values explicit and
account/market-scoped rather than relying on one shared market selection. A
small centralized port-selection helper or clearly named configuration
constants is preferable to duplicating unexplained literals. The exact
implementation mechanism is intentionally left for the implementation round.

The proposed change must affect only the local source port passed to
`sock.bind()`. It must preserve:

- the remote HTTP endpoint and remote port;
- the remote WebSocket URL and remote port;
- `SO_REUSEADDR` behavior;
- HTTP/WebSocket retry behavior;
- account, strategy, order, and balance logic; and
- real-account behavior.

Before implementation, verify that `10001` is available for the intended
worker and does not conflict with another explicitly managed local service.
That verification must remain read-only unless the operator separately
authorizes implementation and runtime testing.

## Scope decision for `us_mock`

Do not modify `us_mock` in this KR proposal. Source inspection shows that
`us_mock` has the same latent pattern (`443` for both HTTP and WebSocket), so
it should receive a separate follow-up proposal or an explicitly broadened
implementation authorization. Keeping it out of this change avoids silently
expanding a KR incident fix into a cross-market runtime change.

The next proposal should decide whether to use a distinct US WebSocket local
source port as well, after considering the existing US worker's runtime
behavior and any machine-level port reservations.

## Explicit exclusions

This proposal does not:

- touch `kr_real` or `us_real`;
- change firewall or WFP policy;
- change remote Kiwoom endpoint ports;
- change credentials or account configuration;
- change trading, order, quote, balance, or retry behavior; or
- authorize implementation, testing, worker restart, or commit.

## Required future sequence

1. Obtain separate authorization to implement the KR port separation.
2. Make the smallest source change that gives KR HTTP and KR WebSocket
   distinct local source ports.
3. Run focused source/unit verification, including the selected port values
   and socket-binding path.
4. Obtain separate authorization to restart `kr_mock`.
5. Restart and verify that KR HTTP and KR WebSocket can coexist, that the
   collision no longer occurs, and that normal recovery/error handling remains
   intact.
6. Only commit after the implementation and runtime evidence are accepted.

This verify-first sequence follows the proposal → source verification →
implementation → restart-and-verify pattern used for earlier observability
work. It must remain separate from the pending observability restart-and-
commit sequence mentioned in the operator record.

## Current runtime boundary

- `kr_mock`: stopped cleanly by v103; do not restart in this round.
- `us_mock`: not restarted, stopped, or modified by v103.
- `kr_real`/`us_real`: untouched and out of scope.
- Firewall/WFP: untouched.
- Git commit: none created by v103.
