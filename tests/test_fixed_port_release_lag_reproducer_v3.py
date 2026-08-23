import socket
import subprocess
import time
import unittest


def _select_port(candidates):
    for port in candidates:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            continue
        finally:
            probe.close()
        return port
    raise AssertionError(f"Could not reserve any candidate port: {candidates}")


def _shutdown_and_close(sock):
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        pass
    sock.close()


def _connect_baseline(server_address):
    for port in (443, 18443, 28443, 38443, 48443, 58443):
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.bind(("127.0.0.1", port))
            client.connect(server_address)
        except OSError:
            client.close()
            continue
        return port, client
    raise AssertionError("Could not establish a baseline connection on any client port")


class FixedPortReleaseLagReproducerV3Test(unittest.TestCase):
    def test_exact_four_tuple_reconnect_records_release_lag(self):
        server_fixed_port = _select_port((19443, 20443))
        print(f"server_fixed_port={server_fixed_port}")

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        first_client = None
        accepted = None
        try:
            server.bind(("127.0.0.1", server_fixed_port))
            server.listen(1)
            remote_address = ("127.0.0.1", server_fixed_port)

            client_fixed_port, first_client = _connect_baseline(remote_address)
            print(f"client_fixed_port={client_fixed_port}")
            accepted, _ = server.accept()

            _shutdown_and_close(first_client)
            first_client = None
            _shutdown_and_close(accepted)
            accepted = None
            close_time = time.perf_counter()

            netstat = subprocess.run(
                ["cmd", "/c", f"netstat -an | findstr :{client_fixed_port}"],
                capture_output=True,
                text=True,
                errors="replace",
                check=False,
            )
            print(
                "netstat_command=netstat -an | findstr :"
                + str(client_fixed_port)
            )
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
                    candidate.bind(("127.0.0.1", client_fixed_port))
                    candidate.connect(remote_address)
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
                    accepted_again, _ = server.accept()
                    _shutdown_and_close(accepted_again)
                    candidate.close()
                    break

            self.assertIsNotNone(
                success_elapsed_ms,
                "exact four-tuple reconnect did not succeed within 200 attempts",
            )
        finally:
            _shutdown_and_close(first_client)
            _shutdown_and_close(accepted)
            server.close()


if __name__ == "__main__":
    unittest.main()
