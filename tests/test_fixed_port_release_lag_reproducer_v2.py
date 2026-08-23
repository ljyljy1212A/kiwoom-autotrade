import socket
import subprocess
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


def _shutdown_and_close(sock):
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        pass
    sock.close()


class FixedPortReleaseLagReproducerV2Test(unittest.TestCase):
    def test_rebind_after_complete_teardown_records_release_lag(self):
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

            _shutdown_and_close(client)
            client = None
            _shutdown_and_close(accepted)
            accepted = None
            server.close()
            server = None
            close_time = time.perf_counter()

            netstat = subprocess.run(
                ["cmd", "/c", f"netstat -an | findstr :{fixed_port}"],
                capture_output=True,
                text=True,
                errors="replace",
                check=False,
            )
            print("netstat_command=netstat -an | findstr :" + str(fixed_port))
            print("netstat_output_begin")
            print(netstat.stdout, end="")
            print("netstat_output_end")
            if netstat.stderr:
                print("netstat_stderr_begin")
                print(netstat.stderr, end="")
                print("netstat_stderr_end")

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
            _shutdown_and_close(client)
            _shutdown_and_close(accepted)
            if server is not None:
                server.close()


if __name__ == "__main__":
    unittest.main()
