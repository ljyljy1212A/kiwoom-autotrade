import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src import main as main_module
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


if __name__ == "__main__":
    unittest.main()
