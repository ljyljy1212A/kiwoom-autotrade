from types import SimpleNamespace
import unittest

from src.core.realtime_feed import KiwoomRealtimeFeed


class RealtimeFeedPortTest(unittest.TestCase):
    def test_mock_kr_websocket_uses_separate_local_port(self):
        feed = KiwoomRealtimeFeed(SimpleNamespace(mode="mock", market="KR"))

        self.assertEqual(feed.ws_local_port, 10001)

    def test_mock_us_websocket_uses_separate_local_port(self):
        feed = KiwoomRealtimeFeed(SimpleNamespace(mode="mock", market="US"))

        self.assertEqual(feed.ws_local_port, 10002)
