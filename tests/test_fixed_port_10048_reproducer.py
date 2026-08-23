import socket
import unittest


def _select_fixed_port():
    for port in (443, 18443):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            continue
        finally:
            probe.close()
        return port
    raise AssertionError("Could not reserve either fixed test port 443 or 18443")


class FixedPort10048ReproducerTest(unittest.TestCase):
    def test_reusing_fixed_local_port_reports_address_in_use(self):
        fixed_port = _select_fixed_port()
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        first_client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        second_client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            server_address = server.getsockname()

            first_client.bind(("127.0.0.1", fixed_port))
            first_client.connect(server_address)
            accepted, _ = server.accept()
            try:
                with self.assertRaises(OSError) as captured:
                    second_client.bind(("127.0.0.1", fixed_port))
                error = captured.exception
                print(f"fixed_port={fixed_port} exception={error!r}")
                self.assertIn(getattr(error, "winerror", None) or getattr(error, "errno", None), (10048, 98))
            finally:
                accepted.close()
        finally:
            second_client.close()
            first_client.close()
            server.close()


if __name__ == "__main__":
    unittest.main()
