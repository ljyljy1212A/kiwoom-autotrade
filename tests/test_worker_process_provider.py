from types import SimpleNamespace

import tools.worker_watchdog as watchdog
from src.core import process_inventory


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


def test_posix_provider_maps_python_worker(monkeypatch):
    files = {
        (101, "stat"): "101 (python3) S 100 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 123",
        (101, "cmdline"): "/usr/bin/python3\x00-m\x00src.main\x00--market\x00KR\x00",
        (101, "comm"): "python3\n",
    }
    monkeypatch.setattr(process_inventory, "_list_posix_pids", lambda: [101])
    monkeypatch.setattr(process_inventory, "_read_proc_text", lambda pid, name: files[(pid, name)])
    monkeypatch.setattr(process_inventory, "_read_posix_uptime", lambda: "100.0 0.0\n")
    monkeypatch.setattr(process_inventory, "_read_posix_clk_tck", lambda: 100)
    monkeypatch.setattr(process_inventory.time, "time", lambda: 1_700_000_100.0)

    result = process_inventory.query_posix_processes()

    assert result[0].Name == "python3"
    assert result[0].ParentProcessId == 100
    assert result[0].CommandLine == "/usr/bin/python3 -m src.main --market KR"
    assert result[0].CreationDate == "2023-11-14T22:13:20Z"


def test_watchdog_excludes_non_python_processes(monkeypatch):
    monkeypatch.setattr(
        watchdog,
        "query_win32_processes",
        lambda: [
            raw("bash", 201, 1, supervisor_command("kr_mock", "KR")),
            raw("bash", 202, 201, worker_command("KR")),
        ],
    )

    assert watchdog.enumerate_worker_processes("kr_mock", "KR") == []


def test_posix_provider_continues_after_disappearing_entry(monkeypatch):
    files = {
        (301, "stat"): "301 (python3) S 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 123",
        (301, "cmdline"): "/usr/bin/python3\x00-m\x00src.main\x00",
        (301, "comm"): "python3\n",
    }
    monkeypatch.setattr(process_inventory, "_list_posix_pids", lambda: [300, 301])

    def read_proc_text(pid, name):
        if pid == 300:
            raise OSError("process disappeared")
        return files[(pid, name)]

    monkeypatch.setattr(process_inventory, "_read_proc_text", read_proc_text)
    monkeypatch.setattr(process_inventory, "_read_posix_clk_tck", lambda: None)

    result = process_inventory.query_posix_processes()

    assert [record.ProcessId for record in result] == [301]
    assert result[0].CreationDate is None


def test_provider_uses_posix_without_subprocess(monkeypatch):
    monkeypatch.setattr(process_inventory.os, "name", "posix")
    monkeypatch.setattr(process_inventory, "query_posix_processes", lambda: ["posix"])
    monkeypatch.setattr(process_inventory.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError()))

    assert process_inventory.query_win32_processes() == ["posix"]
