import asyncio
import unittest

from src.main import EngineState, SymbolEngineRegistry


class SymbolEngineRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_claim_rejects_duplicate_until_task_completes(self):
        registry = SymbolEngineRegistry()
        account, symbol = "kr_mock", "000490"
        gate = asyncio.Event()
        entered = asyncio.Event()
        constructed = 0

        async def simulated_engine():
            nonlocal constructed
            task = asyncio.current_task()
            assert task is not None
            registry.mark_running(account, symbol, task)
            constructed += 1
            entered.set()
            await gate.wait()

        async def start_attempt():
            # Yield once so both callers race through the same event loop turn.
            await asyncio.sleep(0)
            return registry.claim(account, symbol)

        first_claim, second_claim = await asyncio.gather(start_attempt(), start_attempt())
        self.assertEqual(1, sum((first_claim, second_claim)))
        self.assertTrue(first_claim or second_claim)
        first = asyncio.create_task(simulated_engine())
        registry.bind_task(account, symbol, first)
        first.add_done_callback(lambda task: registry.release_from_task(account, symbol, task))

        # A further rapid watcher/main-loop start attempt also does not
        # construct a second AccountEngine because its claim is rejected.
        self.assertFalse(registry.claim(account, symbol))
        await entered.wait()
        self.assertEqual(EngineState.RUNNING, registry.state(account, symbol))
        self.assertEqual(1, constructed)

        gate.set()
        await first
        self.assertEqual(EngineState.STOPPED, registry.state(account, symbol))

        # A legitimate later start is allowed only after the done callback.
        self.assertTrue(registry.claim(account, symbol))


if __name__ == "__main__":
    unittest.main()
