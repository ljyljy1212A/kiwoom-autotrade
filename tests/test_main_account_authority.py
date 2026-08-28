import os
import sys
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
import uuid
from unittest.mock import AsyncMock, Mock, patch

from src import main as main_module
from src import worker_supervisor
from src.core.process_lock import AccountOrderAuthority


class MainAccountAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def _run_main(self, account_filter: str | None, contexts=None, acquire_pid=None, bind=None):
        env = {} if account_filter is None else {"ACCOUNT_FILTER": account_filter}
        with patch.dict(os.environ, env, clear=account_filter is None), \
             patch.object(sys, "argv", ["main.py", "--market", "US"]), \
             patch.object(main_module, "load_accounts", return_value=contexts) as load_accounts:
            if acquire_pid is None:
                acquire_pid = Mock()
            if bind is not None and contexts:
                contexts[0].client.bind_order_authority = bind
            with patch.object(main_module, "_acquire_worker_pid", side_effect=acquire_pid):
                result = await main_module.main()
        return result, load_accounts

    async def test_missing_account_filter_fails_before_account_loading(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(sys, "argv", ["main.py", "--market", "US"]), \
             patch.object(main_module, "load_accounts") as load_accounts:
            with self.assertRaisesRegex(RuntimeError, "exactly one account"):
                await main_module.main()
        load_accounts.assert_not_called()

    async def test_multiple_account_filter_fails_before_account_loading(self):
        with patch.dict(os.environ, {"ACCOUNT_FILTER": "us_mock,us_real"}, clear=True), \
             patch.object(sys, "argv", ["main.py", "--market", "US"]), \
             patch.object(main_module, "load_accounts") as load_accounts:
            with self.assertRaisesRegex(RuntimeError, "multi-account"):
                await main_module.main()
        load_accounts.assert_not_called()

    async def test_lock_is_released_when_pid_claim_fails_after_authority_binding(self):
        lock = Mock()
        context = SimpleNamespace(
            account_id="us_mock",
            client=SimpleNamespace(market="US", bind_order_authority=Mock(), close=AsyncMock()),
        )
        with patch.dict(os.environ, {"ACCOUNT_FILTER": "us_mock"}, clear=True), \
             patch.object(sys, "argv", ["main.py", "--market", "US"]), \
             patch.object(main_module, "load_accounts", return_value=[context]), \
             patch.object(main_module, "_worker_lock", return_value=lock), \
             patch.object(main_module, "_acquire_worker_pid", side_effect=RuntimeError("pid claim failed")):
            with self.assertRaisesRegex(RuntimeError, "pid claim failed"):
                await main_module.main()

        lock.acquire.assert_called_once_with()
        lock.release.assert_called_once_with()
        authority = context.client.bind_order_authority.call_args.args[0]
        self.assertIsInstance(authority, AccountOrderAuthority)
        self.assertIs(authority.lock, lock)

    async def test_lock_is_released_when_authority_binding_fails(self):
        lock = Mock()
        context = SimpleNamespace(
            account_id="us_mock",
            client=SimpleNamespace(
                market="US",
                bind_order_authority=Mock(side_effect=RuntimeError("bind failed")),
                close=AsyncMock(),
            ),
        )
        with patch.dict(os.environ, {"ACCOUNT_FILTER": "us_mock"}, clear=True), \
             patch.object(sys, "argv", ["main.py", "--market", "US"]), \
             patch.object(main_module, "load_accounts", return_value=[context]), \
             patch.object(main_module, "_worker_lock", return_value=lock):
            with self.assertRaisesRegex(RuntimeError, "bind failed"):
                await main_module.main()

        lock.acquire.assert_called_once_with()
        lock.release.assert_called_once_with()


class MainConcurrencyTests(unittest.TestCase):
    def test_concurrent_main_starts_fail_closed_on_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            account_id = f"race_{uuid.uuid4().hex}"
            script = textwrap.dedent(
                """
                import asyncio
                import os
                import sys
                from pathlib import Path
                from types import SimpleNamespace
                from unittest.mock import AsyncMock, patch

                base_dir = Path(sys.argv[1])
                account_id = sys.argv[2]
                os.environ["KIWOOM_DATA_DIR"] = str(base_dir)
                os.environ["KIWOOM_LOG_DIR"] = str(base_dir / "logs")
                sys.argv = ["main.py", "--market", "KR"]

                from src import main as main_module
                from src.core.process_lock import ProcessLock


                class DummyLogger:
                    def bind(self, **kwargs):
                        return self

                    def info(self, *args, **kwargs):
                        return None

                    def warning(self, *args, **kwargs):
                        return None

                    def error(self, *args, **kwargs):
                        return None

                    def exception(self, *args, **kwargs):
                        return None

                    def debug(self, *args, **kwargs):
                        return None


                class DummyClient:
                    def __init__(self, logger):
                        self.market = "KR"
                        self.logger = logger
                        self.token_mgr = SimpleNamespace(logger=logger)

                    def bind_order_authority(self, authority):
                        self.authority = authority

                    def set_exchange_alert_callback(self, callback):
                        self.callback = callback

                    async def close(self):
                        return None


                class DummyTelegram:
                    def __init__(self, *args, **kwargs):
                        pass

                    async def start_polling(self):
                        print("ready", flush=True)
                        await asyncio.Event().wait()

                    async def stop(self):
                        return None

                    async def notify_error(self, *args, **kwargs):
                        return None

                    async def notify_order(self, side, symbol, qty, price, ord_no):
                        return None

                    async def notify_fill(self, side, symbol, qty, price, ord_no):
                        return None

                    async def notify_balance_change(self, message):
                        return None

                    async def notify_symbol_closed(self, symbol, account_id, qty, avg_price, reason):
                        return None

                    async def notify_symbol_reopened(self, symbol, account_id, reason):
                        return None


                class NotifyingLock:
                    def __init__(self, account_id, base_dir):
                        self._lock = ProcessLock(account_id, base_dir)

                    def acquire(self):
                        self._lock.acquire()
                        print("lock-acquired", flush=True)

                    def release(self):
                        self._lock.release()

                    def owned_by_current_process(self):
                        return self._lock.owned_by_current_process()


                logger = DummyLogger()
                context = SimpleNamespace(
                    account_id=account_id,
                    client=DummyClient(logger),
                    logger=logger,
                    display_name="KR Mock",
                    currency="KRW",
                    reporting_currency="KRW",
                    strategy=SimpleNamespace(symbol="000490"),
                    position=SimpleNamespace(qty=0),
                    risk_manager=SimpleNamespace(logger=logger, approve=lambda intent, position, price: (True, "")),
                )

                async def run():
                    with patch.dict(
                        os.environ,
                        {
                            "ACCOUNT_FILTER": account_id,
                            "TELEGRAM_BOT_TOKEN": "token",
                            "TELEGRAM_CHAT_ID": "chat",
                        },
                        clear=False,
                    ), \
                    patch.object(main_module, "load_accounts", return_value=[context]), \
                    patch.object(main_module, "_worker_lock", side_effect=lambda account_id: NotifyingLock(account_id, base_dir)), \
                    patch.object(main_module, "_acquire_worker_pid"), \
                    patch.object(main_module, "_publish_worker_heartbeat", new=AsyncMock()), \
                    patch.object(main_module, "_watch_for_supervisor_stop", new=AsyncMock()), \
                    patch.object(main_module, "_write_worker_status"), \
                    patch.object(main_module, "run_symbol_engines", new=AsyncMock()), \
                    patch.object(main_module, "TelegramController", DummyTelegram):
                        await main_module.main()


                asyncio.run(run())
                """
            )
            first = subprocess.Popen(
                [sys.executable, "-c", script, str(base_dir), account_id],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                first_line = first.stdout.readline().strip()
                if first_line != "lock-acquired":
                    stderr = first.stderr.read()
                    self.fail(f"first subprocess did not acquire the lock: stdout={first_line!r}, stderr={stderr!r}")
                self.assertEqual(first.stdout.readline().strip(), "ready")

                second = subprocess.run(
                    [sys.executable, "-c", script, str(base_dir), account_id],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=30,
                )

                self.assertNotEqual(second.returncode, 0)
                self.assertIn("already running", second.stderr)
            finally:
                if first.poll() is None:
                    first.terminate()
                    try:
                        first.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        first.kill()
                        first.wait(timeout=10)


class SupervisorStartSignalTests(unittest.TestCase):
    def test_start_reports_lock_conflict_when_child_loses_race(self):
        child = Mock(pid=123, poll=Mock(return_value=1))
        with patch.object(worker_supervisor, "status", side_effect=[
            {"running": False, "pid": 0},
            {"running": True, "pid": 999},
        ]), \
             patch.object(worker_supervisor.subprocess, "Popen", return_value=child), \
             patch.object(worker_supervisor, "read_auto_trading_enabled", return_value=False):
            code, payload = worker_supervisor.start("kr_mock", "KR")

        self.assertEqual(code, 3)
        self.assertEqual(payload["failureClass"], "lock-conflict")
        self.assertEqual(payload["reason"], "worker-refused-or-exited")


if __name__ == "__main__":
    unittest.main()
