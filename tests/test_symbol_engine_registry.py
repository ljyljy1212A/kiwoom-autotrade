import asyncio
import unittest

from src.main import DispatchProfileVersion, EngineState, SymbolEngineRegistry


class SymbolEngineRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_claim_rejects_duplicate_until_task_completes(self):
        registry = SymbolEngineRegistry()
        account, market, symbol = "kr_mock", "KR", "000490"
        gate = asyncio.Event()
        entered = asyncio.Event()
        constructed = 0

        async def simulated_engine():
            nonlocal constructed
            task = asyncio.current_task()
            assert task is not None
            registry.mark_running(account, market, symbol, task)
            constructed += 1
            entered.set()
            await gate.wait()

        async def start_attempt():
            # Yield once so both callers race through the same event loop turn.
            await asyncio.sleep(0)
            return registry.claim(account, market, symbol)

        first_claim, second_claim = await asyncio.gather(start_attempt(), start_attempt())
        self.assertEqual(1, sum((first_claim, second_claim)))
        self.assertTrue(first_claim or second_claim)
        first = asyncio.create_task(simulated_engine())
        registry.bind_task(account, market, symbol, first)
        first.add_done_callback(lambda task: registry.release_from_task(account, market, symbol, task))

        # A further rapid watcher/main-loop start attempt also does not
        # construct a second AccountEngine because its claim is rejected.
        self.assertFalse(registry.claim(account, market, symbol))
        await entered.wait()
        self.assertEqual(EngineState.RUNNING, registry.state(account, market, symbol))
        self.assertEqual(1, constructed)

        gate.set()
        await first
        self.assertEqual(EngineState.STOPPED, registry.state(account, market, symbol))

        # A legitimate later start is allowed only after the done callback.
        self.assertTrue(registry.claim(account, market, symbol))

    async def test_running_symbols_and_profile_version_reset_inputs(self):
        registry = SymbolEngineRegistry()
        account = "us_mock"
        task = asyncio.create_task(asyncio.sleep(0))
        self.assertTrue(registry.claim(account, "US", "AAPL"))
        registry.bind_task(account, "US", "AAPL", task)
        registry.mark_running(account, "US", "AAPL", task)
        self.assertEqual(("AAPL",), registry.running_symbols(account))
        self.assertEqual((account, "AAPL"), registry.key(account, "US", "AAPL"))
        self.assertEqual(("kr_mock", "005930"), registry.key("kr_mock", "KR", "A005930"))
        await task

        provider = DispatchProfileVersion()
        baseline = {"profiles": [{"enabled": True, "config": {"market": "US", "symbol": "AAPL", "x": 1}}]}
        self.assertEqual(0, provider.observe(account, "US", baseline))
        self.assertEqual(0, provider.observe(account, "US", baseline))
        changed = {"profiles": [{"enabled": False, "config": {"market": "US", "symbol": "AAPL", "x": 1}}]}
        self.assertEqual(1, provider.observe(account, "US", changed))


if __name__ == "__main__":
    unittest.main()
