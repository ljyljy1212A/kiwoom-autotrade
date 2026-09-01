from types import SimpleNamespace

import tools.worker_watchdog as watchdog


def process(pid, account="kr_mock", market="KR", live=True, command_line=None):
    return SimpleNamespace(
        pid=pid,
        account=account,
        market=market,
        live=live,
        command_line=command_line or f"pythonw.exe -m src.main --market {market}",
    )


def fakes():
    logger = SimpleNamespace(warnings=[])
    logger.warning = lambda message: logger.warnings.append(message)
    notifier = SimpleNamespace(calls=[])
    notifier.send = lambda *args: notifier.calls.append(args)
    return logger, notifier


def test_single_live_process_does_not_alert():
    logger, notifier = fakes()
    result = watchdog.check_duplicate_live_process(
        "kr_mock", 101, lambda account, market: [process(101)],
        logger=logger, notification_fn=notifier.send,
    )
    assert result is False
    assert logger.warnings == []
    assert notifier.calls == []


def test_genuine_duplicate_alerts_once_without_remediation():
    logger, notifier = fakes()
    result = watchdog.check_duplicate_live_process(
        "us_mock", 202,
        lambda account, market: [process(202, "us_mock", "US"), process(303, "us_mock", "US")],
        logger=logger, notification_fn=notifier.send,
    )
    assert result is True
    assert len(logger.warnings) == 1
    assert len(notifier.calls) == 1
    assert notifier.calls[0][2] == "duplicate-live-process"
    assert "No process termination or restart was attempted." in notifier.calls[0][3]


def test_stale_or_ambiguous_query_is_indeterminate_without_alert():
    logger, notifier = fakes()
    assert watchdog.check_duplicate_live_process(
        "kr_mock", 404, lambda account, market: [],
        logger=logger, notification_fn=notifier.send,
    ) is False
    assert watchdog.check_duplicate_live_process(
        "kr_mock", 404, lambda account, market: [SimpleNamespace(pid=404)],
        logger=logger, notification_fn=notifier.send,
    ) is False
    assert logger.warnings == []
    assert notifier.calls == []


def test_non_mock_account_is_structurally_rejected_before_query():
    logger, notifier = fakes()
    queried = []

    def query(account, market):
        queried.append((account, market))
        raise AssertionError("non-mock account reached process query")

    assert watchdog.check_duplicate_live_process(
        "kr_real", 505, query, logger=logger, notification_fn=notifier.send,
    ) is False
    assert watchdog.check_duplicate_live_process(
        "us_real", 606, query, logger=logger, notification_fn=notifier.send,
    ) is False
    assert queried == []
    assert logger.warnings == []
    assert notifier.calls == []
