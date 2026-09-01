import os
import socket
import time
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


@unittest.skipUnless(os.name == "nt", "Windows-only fixed-port release-lag timing test")
class FixedPortReleaseLagReproducerTest(unittest.TestCase):
    def test_rebind_after_close_records_release_lag(self):
        fixed_port = _select_fixed_port()
        print(f"fixed_port={fixed_port}")
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        accepted = None
        try:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            client.bind(("127.0.0.1", fixed_port))
            client.connect(server.getsockname())
            accepted, _ = server.accept()

            client.close()
            client = None
            close_time = time.perf_counter()
            success_elapsed_ms = None

            for attempt in range(1, 201):
                candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                elapsed_ms = (time.perf_counter() - close_time) * 1000
                try:
                    candidate.bind(("127.0.0.1", fixed_port))
                except OSError as error:
                    print(
                        f"attempt={attempt} elapsed_ms={elapsed_ms:.3f} "
                        f"result=error exception={error!r} "
                        f"winerror={getattr(error, 'winerror', None)!r} "
                        f"errno={getattr(error, 'errno', None)!r}"
                    )
                    candidate.close()
                    time.sleep(0.005)
                else:
                    success_elapsed_ms = (time.perf_counter() - close_time) * 1000
                    print(
                        f"attempt={attempt} elapsed_ms={success_elapsed_ms:.3f} "
                        "result=success"
                    )
                    candidate.close()
                    break

            self.assertIsNotNone(
                success_elapsed_ms,
                "fixed local port did not become bindable within 200 attempts",
            )
        finally:
            if client is not None:
                client.close()
            if accepted is not None:
                accepted.close()
            server.close()


if __name__ == "__main__":
    unittest.main()
