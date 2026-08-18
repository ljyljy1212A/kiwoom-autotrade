from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock

from src.core.kiwoom_client import KiwoomClient
from src.core.process_lock import AccountOrderAuthority
from src.utils.exceptions import OrderAuthorityError


class OrderAuthorityTest(unittest.IsolatedAsyncioTestCase):
    async def test_order_without_authority_fails_before_exchange_lookup(self):
        client = KiwoomClient("key", "secret", "unauthorized", market="US", exchange="ND", mode="mock")
        client._resolve_exchange = AsyncMock(side_effect=AssertionError("lookup must not run"))
        client._post_once = AsyncMock(side_effect=AssertionError("order must not run"))

        with self.assertRaises(OrderAuthorityError):
            await client.place_order("BUY", "NVDA", 1, 10.5)

        client._resolve_exchange.assert_not_awaited()
        client._post_once.assert_not_awaited()

    async def test_authority_loss_before_post_blocks_order(self):
        client = KiwoomClient("key", "secret", "kr-account", market="KR", exchange="KRX", mode="mock")
        lock = Mock()
        lock.owned_by_current_process.side_effect = [True, False]
        client.bind_order_authority(AccountOrderAuthority("test", lock))
        client._post_once = AsyncMock(side_effect=AssertionError("order must not run"))

        with self.assertRaises(OrderAuthorityError):
            await client.place_order("BUY", "005930", 1, 70000)

        client._post_once.assert_not_awaited()
