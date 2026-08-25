import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from src.core.broker_http import FixedPortCollisionError
from src.core.kiwoom_client import KiwoomClient
from src.core.process_lock import AccountOrderAuthority
from src.data.order_attempts import (
    OrderAttestationOutcome,
    OrderAttemptStore,
    unattributed_attempt_ids,
)
from src.utils.exceptions import KiwoomAPIError, OrderRejectedError, RetryableError


class OrderAttemptStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)
        self.store = OrderAttemptStore(self.data_dir / "order_attempts_account-a.db", "account-a")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_attestation_is_durable_and_requires_known_outcome(self):
        attempt = self.store.record_attempt("BUY", "SOXL", 2, 10.5, "00")
        self.store.mark_unattributed(attempt.attempt_id)

        with self.assertRaises(ValueError):
            self.store.attest_unattributed(attempt.attempt_id, "operator-1", "filled")

        attested = self.store.attest_unattributed(
            attempt.attempt_id,
            "operator-1",
            OrderAttestationOutcome.FILLED,
        )

        self.store.close()
        self.store = OrderAttemptStore(self.data_dir / "order_attempts_account-a.db", "account-a")
        persisted = self.store.get_attempt(attested.attempt_id)
        self.assertEqual(persisted.attested_by, "operator-1")
        self.assertIsNotNone(persisted.attested_at)
        self.assertEqual(persisted.attested_outcome, OrderAttestationOutcome.FILLED)

    def test_unattributed_query_needs_explicit_attestation_to_remove_marker(self):
        attempt = self.store.record_attempt("BUY", "SOXL", 2, 10.5, "00")
        self.store.mark_unattributed(attempt.attempt_id)

        self.assertEqual(self.store.unattributed_attempt_ids(), [attempt.attempt_id])
        self.assertEqual(self.store.unattributed_attempt_ids(), [attempt.attempt_id])

        self.store.attest_unattributed(attempt.attempt_id, "operator-1", OrderAttestationOutcome.ABSENT)
        self.assertEqual(self.store.unattributed_attempt_ids(), [])

    def test_account_query_excludes_attested_attempts(self):
        second = OrderAttemptStore(self.data_dir / "order_attempts_account-b.db", "account-b")
        try:
            first_attempt = self.store.record_attempt("BUY", "SOXL", 2, 10.5, "00")
            second_attempt = second.record_attempt("SELL", "NVDA", 1, 100, "00")
            self.store.mark_unattributed(first_attempt.attempt_id)
            second.mark_unattributed(second_attempt.attempt_id)
            second.attest_unattributed(second_attempt.attempt_id, "operator-2", OrderAttestationOutcome.REJECTED)

            self.assertEqual(unattributed_attempt_ids("account-a", self.data_dir), [first_attempt.attempt_id])
            self.assertEqual(unattributed_attempt_ids("account-b", self.data_dir), [])
        finally:
            second.close()


class OrderAttemptRecordingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = OrderAttemptStore(Path(self.temp.name) / "attempts.db", "account-a")
        self.client = KiwoomClient("key", "secret", "account-a", market="US", exchange="ND", mode="mock")
        lock = Mock()
        lock.owned_by_current_process.return_value = True
        self.client.bind_order_authority(AccountOrderAuthority("test", lock))
        self.client._order_attempt_store = self.store
        self.client._exchange_cache["NVDA"] = "ND"
        self.client._order_min_interval_sec = 0.0

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_attempt_is_recorded_before_successful_dispatch(self):
        async def post_once(*_args, **_kwargs):
            row = self.store.db.execute("SELECT * FROM order_dispatch_attempts").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["account_id"], "account-a")
            self.assertEqual(row["side"], "BUY")
            self.assertEqual(row["symbol"], "NVDA")
            self.assertEqual(row["qty"], 2.0)
            self.assertEqual(row["price"], 10.5)
            self.assertEqual(row["order_type"], "00")
            return {"ord_no": "ORD-1"}

        self.client._post_once = post_once

        result = await self.client.place_order("BUY", "NVDA", 2, 10.5)

        self.assertEqual(result.ord_no, "ORD-1")
        self.assertEqual(self.store.unattributed_attempt_ids(), [])

    async def test_fixed_port_failure_marks_attempt_unattributed(self):
        async def fixed_port_failure(*_args, **_kwargs):
            try:
                raise FixedPortCollisionError(OSError(98, "address already in use"))
            except FixedPortCollisionError as collision:
                raise RetryableError("fixed-port collision") from collision

        self.client._post_once = fixed_port_failure

        with self.assertRaises(RetryableError):
            await self.client.place_order("BUY", "NVDA", 2, 10.5)

        self.assertEqual(len(self.store.unattributed_attempt_ids()), 1)

    async def test_non_collision_rejection_does_not_mark_attempt_unattributed(self):
        self.client._post_once = AsyncMock(side_effect=KiwoomAPIError("ust20000", 7, "rejected"))

        with self.assertRaises(OrderRejectedError):
            await self.client.place_order("BUY", "NVDA", 2, 10.5)

        self.assertEqual(self.store.unattributed_attempt_ids(), [])
