import inspect
import socket
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from src.core import broker_http
from src.core.kiwoom_client import KiwoomClient


class Round1586DryRunTest(unittest.TestCase):
    def _collision(self):
        failure = OSError(10048, "address already in use")
        failure.winerror = 10048
        return failure

    def _fake_socket(self, side_effect):
        fake_socket = Mock()
        fake_socket.connect.side_effect = side_effect
        return fake_socket

    def test_01_exhausts_exactly_at_nine_seconds(self):
        fake_socket = self._fake_socket([self._collision()])
        with patch(
            "src.core.broker_http.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 443))],
        ), patch("src.core.broker_http.socket.socket", return_value=fake_socket), patch(
            "src.core.broker_http.time.monotonic", side_effect=[0.0, 9.0]
        ):
            with self.assertRaises(broker_http.FixedPortCollisionError):
                broker_http._connect_with_reuseaddr("127.0.0.1", 443, "0.0.0.0", 443, 1.0, [], None)

    def test_02_recovers_before_nine_seconds(self):
        fake_socket = self._fake_socket([self._collision(), None])
        with patch(
            "src.core.broker_http.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 443))],
        ), patch("src.core.broker_http.socket.socket", return_value=fake_socket), patch(
            "src.core.broker_http.time.sleep"
        ), patch("src.core.broker_http.random.uniform", return_value=0.0), patch(
            "src.core.broker_http.time.monotonic", side_effect=[0.0, 0.0, 0.0, 8.9]
        ):
            result = broker_http._connect_with_reuseaddr("127.0.0.1", 443, "0.0.0.0", 443, 1.0, [], None)
        self.assertIs(result, fake_socket)

    def test_03_non_10048_errors_do_not_retry(self):
        failure = ConnectionRefusedError(10061, "connection refused")
        fake_socket = self._fake_socket([failure])
        with patch(
            "src.core.broker_http.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 443))],
        ), patch("src.core.broker_http.socket.socket", return_value=fake_socket), patch(
            "src.core.broker_http.time.sleep"
        ) as sleep:
            with self.assertRaises(ConnectionRefusedError):
                broker_http._connect_with_reuseaddr("127.0.0.1", 443, "0.0.0.0", 443, 1.0, [], None)
        fake_socket.connect.assert_called_once()
        sleep.assert_not_called()

    def test_04_rapid_reconnects_recover_below_2_4_seconds(self):
        fake_socket = self._fake_socket([self._collision(), self._collision(), None])
        with patch(
            "src.core.broker_http.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 443))],
        ), patch("src.core.broker_http.socket.socket", return_value=fake_socket), patch(
            "src.core.broker_http.time.sleep"
        ), patch("src.core.broker_http.random.uniform", return_value=0.0), patch(
            "src.core.broker_http.time.monotonic", side_effect=[0.0, 0.0, 0.0, 0.0, 0.0]
        ):
            result = broker_http._connect_with_reuseaddr("127.0.0.1", 443, "0.0.0.0", 443, 1.0, [], None)
        self.assertIs(result, fake_socket)
        self.assertEqual(fake_socket.connect.call_count, 3)

    def test_05_mock_port_selection_is_unchanged(self):
        kr = KiwoomClient("key", "secret", "kr", market="KR", mode="mock")
        us = KiwoomClient("key", "secret", "us", market="US", mode="mock")
        self.assertEqual(kr._http_gate.local_port, 10000)
        self.assertEqual(us._http_gate.local_port, 443)

    def test_06_rest_caller_keeps_fifteen_second_timeout(self):
        source = inspect.getsource(KiwoomClient._post_once)
        self.assertIn("client(timeout=15)", source)
        self.assertIn("timeout=15", source)

    def test_07_token_callers_keep_ten_second_timeout(self):
        from src.core.token_manager import TokenManager

        source = inspect.getsource(TokenManager._issue) + inspect.getsource(TokenManager.revoke)
        self.assertGreaterEqual(source.count("timeout=10"), 4)

    def test_08_hourly_counters_include_first_success_and_retry_outcomes(self):
        diagnostics = broker_http._FixedPortConnectDiagnostics(443)
        logger = Mock()
        diagnostics.record("sequences", logger)
        diagnostics.record("first_attempt_successes", logger)
        diagnostics.record("retry_sequences", logger)
        diagnostics.record("retry_connect_calls", logger)
        diagnostics.record("recovered_sequences", logger)
        diagnostics.record("exhausted_sequences", logger)
        diagnostics.record("non_collision_failures", logger)
        self.assertEqual(
            diagnostics._counts,
            {
                "sequences": 1,
                "first_attempt_successes": 1,
                "retry_sequences": 1,
                "recovered_sequences": 1,
                "exhausted_sequences": 1,
                "non_collision_failures": 1,
                "retry_connect_calls": 1,
            },
        )

    def test_09_hourly_summary_is_not_spammed(self):
        hour0 = datetime(2026, 9, 1, 0, tzinfo=timezone.utc)
        hour1 = datetime(2026, 9, 1, 1, tzinfo=timezone.utc)
        logger = Mock()
        with patch.object(
            broker_http._FixedPortConnectDiagnostics,
            "_utc_hour_start",
            side_effect=[hour0, hour0, hour1, hour1],
        ):
            diagnostics = broker_http._FixedPortConnectDiagnostics(443)
            diagnostics.record("sequences", logger)
            diagnostics.record("first_attempt_successes", logger)
            diagnostics.record("sequences", logger)
        logger.info.assert_called_once()
        self.assertIn("Fixed-port HTTP connect summary:", logger.info.call_args.args[0])

    def test_10_existing_retry_close_rebind_diagnostics_remain_present(self):
        source = inspect.getsource(broker_http._connect_with_reuseaddr)
        backend_source = inspect.getsource(broker_http._FixedPortAnyIOBackend.connect_tcp)
        self.assertIn("Fixed-port HTTP connect retry", source)
        self.assertIn("Fixed-port HTTP connect recovered", source)
        self.assertIn("Fixed-port HTTP socket failure", source)
        self.assertIn("_FIXED_PORT_CLOSE_WAIT_TIMEOUT_SEC", backend_source)
        self.assertIn("SO_REUSEADDR", source)


if __name__ == "__main__":
    unittest.main()
