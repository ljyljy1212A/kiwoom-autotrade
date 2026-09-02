from types import SimpleNamespace

import tools.worker_watchdog as watchdog


def raw(name, pid, parent_pid, command_line):
    return SimpleNamespace(
        Name=name,
        ProcessId=pid,
        ParentProcessId=parent_pid,
        CommandLine=command_line,
    )


def supervisor_command(account, market):
    return f"pythonw.exe -m src.worker_supervisor start --account {account} --market {market}"


def worker_command(market):
    return f"pythonw.exe -m src.main --market {market}"


def test_qualifying_supervisor_with_one_direct_child_is_normalized(monkeypatch):
    monkeypatch.setattr(
        watchdog,
        "query_win32_processes",
        lambda: [
            raw("pythonw.exe", 100, 1, supervisor_command("kr_mock", "KR")),
            raw("pythonw.exe", 101, 100, worker_command("KR")),
        ],
    )

    assert watchdog.enumerate_worker_processes("kr_mock", "KR") == [
        SimpleNamespace(
            pid=101,
            account="kr_mock",
            market="KR",
            live=True,
            command_line=worker_command("KR"),
        )
    ]


def test_qualifying_supervisor_with_two_direct_children_returns_both(monkeypatch):
    monkeypatch.setattr(
        watchdog,
        "query_win32_processes",
        lambda: [
            raw("python.exe", 200, 1, supervisor_command("us_mock", "US")),
            raw("pythonw.exe", 201, 200, worker_command("US")),
            raw("pythonw.exe", 202, 200, worker_command("US")),
        ],
    )

    result = watchdog.enumerate_worker_processes("us_mock", "US")

    assert [item.pid for item in result] == [201, 202]


def test_no_qualifying_supervisor_returns_empty(monkeypatch):
    monkeypatch.setattr(
        watchdog,
        "query_win32_processes",
        lambda: [
            raw("pythonw.exe", 301, 1, "pythonw.exe -m other.module"),
            raw("pythonw.exe", 302, 301, worker_command("KR")),
            raw("pythonw.exe", 303, 1, supervisor_command("us_mock", "US")),
        ],
    )

    assert watchdog.enumerate_worker_processes("kr_mock", "KR") == []


def test_child_signature_with_unrelated_parent_is_excluded(monkeypatch):
    monkeypatch.setattr(
        watchdog,
        "query_win32_processes",
        lambda: [
            raw("pythonw.exe", 400, 1, supervisor_command("kr_mock", "KR")),
            raw("pythonw.exe", 401, 999, worker_command("KR")),
        ],
    )

    assert watchdog.enumerate_worker_processes("kr_mock", "KR") == []


def test_multiple_qualifying_supervisors_return_children_from_both(monkeypatch):
    monkeypatch.setattr(
        watchdog,
        "query_win32_processes",
        lambda: [
            raw("pythonw.exe", 500, 1, supervisor_command("us_mock", "US")),
            raw("python.exe", 501, 2, supervisor_command("us_mock", "US")),
            raw("pythonw.exe", 502, 500, worker_command("US")),
            raw("pythonw.exe", 503, 501, worker_command("US")),
        ],
    )

    result = watchdog.enumerate_worker_processes("us_mock", "US")

    assert [item.pid for item in result] == [502, 503]


def test_unsupported_account_is_rejected_before_query(monkeypatch):
    def fail_query():
        raise AssertionError("unsupported account reached process query")

    monkeypatch.setattr(watchdog, "query_win32_processes", fail_query)

    assert watchdog.enumerate_worker_processes("unsupported_account", "KR") == []
