import asyncio

async def test_heartbeat_conflict():
    done_event = asyncio.Event()

    async def _heartbeat():
        try:
            print("Heartbeat 1: waiting for 2s")
            try:
                await asyncio.wait_for(done_event.wait(), timeout=2.0)
                print("Heartbeat 1: wait finished, done event set")
                return
            except TimeoutError:
                pass

            print("Heartbeat 1: timeout elapsed. Doing edit_text (simulated)")
            await asyncio.sleep(0.5) # simulate network call for edit_text
            print("Heartbeat 1: edit_text done!")

        except asyncio.CancelledError:
            print("Heartbeat cancelled")

    heartbeat_task = asyncio.create_task(_heartbeat())

    async def main_task():
        print("Main task starting")
        await asyncio.sleep(2.0) # exactly the timeout
        print("Main task done, sending response (simulated)")
        await asyncio.sleep(0.1) # Simulate sending response
        print("Main task response sent.")

        done_event.set()
        heartbeat_task.cancel()
        print("Main task finished cancelling heartbeat.")

    await main_task()
    await asyncio.sleep(1.0) # give heartbeat time to react to cancel

asyncio.run(test_heartbeat_conflict())
