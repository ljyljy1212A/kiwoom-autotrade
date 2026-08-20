import unittest

import httpx

from src.core.kiwoom_client import KiwoomClient
from src.utils.exceptions import RetryableError


class _FailingHTTPClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *args, **kwargs):
        raise httpx.ConnectError("simulated transport failure")


class _FailingHTTPGate:
    def client(self, **kwargs):
        return _FailingHTTPClient()


class RetryBoundaryReproductionTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_post_retry_exhaustion_exception_type(self):
        client = object.__new__(KiwoomClient)
        client.domain = "https://example.invalid"
        client._http_gate = _FailingHTTPGate()
        async def headers(_api_id):
            return {}

        client._headers = headers

        with self.assertRaises(Exception) as captured:
            await client._post("/api/test", "test-api", {})

        print(f"escaped_exception_type={type(captured.exception).__name__}")
        self.assertIsInstance(captured.exception, RetryableError)


if __name__ == "__main__":
    unittest.main()
