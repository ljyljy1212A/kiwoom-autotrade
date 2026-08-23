# Proposal v148 Addendum - Exact Diff Draft v150

## Status

Text-only implementation draft. No source, test, configuration, worker, or
git-index change is authorized by v150. This file records the proposed
change; it is not authorization to apply it.

## Part A - Impact scan

The scan covered source, tests, configuration, dashboard, diagnostics, and
Markdown references for US WebSocket local-port 443, local-port binding, and
HTTP/WebSocket shared-port wording.

### Runtime and test matches

#### src/core/realtime_feed.py:123-126

Current content:

    if self.client.market == "KR":
        return 10001
    if self.client.market == "US":
        return 443

Relevant because 443 is the current us_mock WebSocket local source port.
The return 443 line is the one runtime value v148 proposes changing.

#### src/core/realtime_feed.py:130-141

Current path:

    local_port = self.ws_local_port
    ...
    remote_port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    ...
    _connect_ws_socket(..., remote_port, local_port, 10)

The local_port value flows into the WebSocket bind path. The fallback 443 at
line 136 is a remote-port fallback derived from the URL scheme; it is not
the US WebSocket local source port and must not change.

#### src/core/kiwoom_client.py:112-113

Current content:

    http_port = 10000 if mode == "mock" and market == "KR" else 443 if mode == "mock" and market == "US" else None
    self._http_gate = BrokerHTTPGate(http_port, logger)

Relevant because it confirms US HTTP uses local source port 443. It is
outside the proposed edit: v148 explicitly keeps US HTTP at 443.

#### tests/test_realtime_feed.py:13-16

Current content:

    def test_mock_us_websocket_port_is_unchanged(self):
        feed = KiwoomRealtimeFeed(SimpleNamespace(mode="mock", market="US"))

        self.assertEqual(feed.ws_local_port, 443)

Relevant because it directly asserts the old US WebSocket local source port.
The test name and expected value would both need to change.

#### tests/test_realtime_feed.py:8-11

Current content:

    def test_mock_kr_websocket_uses_separate_local_port(self):
        ...
        self.assertEqual(feed.ws_local_port, 10001)

This is a KR assertion and remains unchanged. It confirms that the proposed
US value must not disturb the existing KR 10001 selection.

### Historical documentation matches

The broad scan also found old explanatory references in:

- OPERATOR_RECORD_wrapup_v10-v119.md:83 - historical table showing US
  WebSocket local 443 and remote 10000;
- OPERATOR_RECORD_wrapup_v10-v124.md:75 - historical statement that the
  same-local-port arrangement was intentional;
- PROPOSAL_v103_kr_mock_port_separation.md:107 - KR proposal describing
  the then-unfixed US pattern;
- PROPOSAL_v148_us_mock_port_separation.md:9,55,103,105,117,125 -
  current v148 design and evidence.

These are historical/design records, not runtime assumptions or direct
implementation references. They should not be rewritten as part of this
implementation draft. The v149 erratum remains the authoritative correction
for the earlier remote-port/local-port wording.

No source comment, docstring, monitoring task, quote-health task, balance
monitor, configuration constant, or diagnostic code was found that requires
an additional 443 to 10002 edit. The remote WebSocket URL port 10000 is not
a local source-port assumption.

## Part B - Exact diff drafts

### src/core/realtime_feed.py

    --- a/src/core/realtime_feed.py
    +++ b/src/core/realtime_feed.py
    @@ -123,7 +123,7 @@ class KiwoomRealtimeFeed:
             if self.client.market == "KR":
                 return 10001
             if self.client.market == "US":
    -            return 443
    +            return 10002
             return None

Only the US WebSocket local-port property value changes. The bind call,
SO_REUSEADDR, remote URL, remote port, and retry behavior remain unchanged.

### tests/test_realtime_feed.py

    --- a/tests/test_realtime_feed.py
    +++ b/tests/test_realtime_feed.py
    @@ -10,7 +10,7 @@ class RealtimeFeedPortTest(unittest.TestCase):
     
             self.assertEqual(feed.ws_local_port, 10001)
     
    -    def test_mock_us_websocket_port_is_unchanged(self):
    +    def test_mock_us_websocket_uses_separate_local_port(self):
             feed = KiwoomRealtimeFeed(SimpleNamespace(mode="mock", market="US"))
     
    -        self.assertEqual(feed.ws_local_port, 443)
    +        self.assertEqual(feed.ws_local_port, 10002)

The KR assertion remains unchanged. This direct test update makes the
proposed US value explicit and removes the stale "unchanged" name.

### No other file diffs

No additional runtime, test, monitoring, diagnostic, configuration, comment,
or docstring diff is proposed. Historical Markdown files are intentionally
not edited.

## Part C - Scope statement

The scan found no change outside the narrow v148 scope of one WebSocket
property value plus its direct test reference. The US HTTP value 443 and the
WebSocket remote-port fallback 443 are intentionally preserved. KR
selection, real-account behavior, remote endpoints, retry behavior, and all
trading/account logic require no diff.

## Not authorized to apply

This addendum is a reviewable text draft only. It is NOT authorized to
apply the proposed changes. No source or test file may be edited, no test
may be run as implementation verification, no worker may be restarted, and
nothing may be staged or committed without separate authorization.
