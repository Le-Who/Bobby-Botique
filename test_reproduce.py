import asyncio


async def test_heartbeat_conflict():
    done_event = asyncio.Event()

    async def _heartbeat():
        try:
            print("Heartbeat 1")
            await asyncio.wait_for(done_event.wait(), timeout=1.0)
            print("Heartbeat finished gracefully")
            return
        except TimeoutError:
            pass

        print("Heartbeat 2")
        try:
            await asyncio.wait_for(done_event.wait(), timeout=1.0)
            print("Heartbeat finished gracefully")
            return
        except TimeoutError:
            pass

        print("Heartbeat 3")

    heartbeat_task = asyncio.create_task(_heartbeat())

    async def main_task():
        await asyncio.sleep(1.5) # Time for Heartbeat 1 to timeout and trigger Heartbeat 2

        # main task sending response here
        print("Main task sending response...")

        done_event.set()
        heartbeat_task.cancel()
        print("Main task finished.")

    await main_task()
    await asyncio.sleep(1.0) # give heartbeat time to react to cancel

asyncio.run(test_heartbeat_conflict())
