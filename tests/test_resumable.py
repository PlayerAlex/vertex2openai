import asyncio
import json
import unittest
from unittest.mock import patch

from resumable import (
    DONE_FRAME,
    ResumableBuffer,
    ResumableCapacityError,
    ResumableStore,
    pump_stream,
)


class ResumableStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_capacity_never_evicts_running_or_read_buffers(self):
        store = ResumableStore()
        with patch("resumable.max_streams", return_value=2):
            running, created = await store.create_if_absent("running")
            self.assertTrue(created)
            reading, _ = await store.create_if_absent("reading")
            await reading.finish()
            reading.readers = 1

            with self.assertRaises(ResumableCapacityError) as caught:
                await store.create_if_absent("third")

            self.assertEqual(caught.exception.status_code, 503)
            self.assertIs(await store.get("running"), running)
            self.assertIs(await store.get("reading"), reading)
            self.assertIsNone(await store.get("third"))
            same, is_new = await store.create_if_absent("running")
            self.assertIs(same, running)
            self.assertFalse(is_new)

    async def test_capacity_reclaims_oldest_finished_unread_buffer(self):
        store = ResumableStore()
        with patch("resumable.max_streams", return_value=1):
            old, _ = await store.create_if_absent("old")
            await old.finish()
            new, is_new = await store.create_if_absent("new")
        self.assertTrue(is_new)
        self.assertIs(await store.get("new"), new)
        self.assertIsNone(await store.get("old"))

    async def test_stale_pump_callback_does_not_remove_replacement(self):
        store = ResumableStore()
        first = asyncio.create_task(asyncio.sleep(60))
        second = asyncio.create_task(asyncio.sleep(60))
        store.register_pump("same", first)
        store.register_pump("same", second)
        await asyncio.sleep(0)
        self.assertIs(store._pumps.get("same"), second)
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)

    async def test_replace_if_same_does_not_replace_a_concurrent_replacement(self):
        store = ResumableStore()
        old, _ = await store.create_if_absent("same")
        await old.finish()
        replacement, created = await store.replace_if_same("same", old)
        observed, created_again = await store.replace_if_same("same", old)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertIs(observed, replacement)


class PumpStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_error_sse_marks_buffer_error_and_preserves_code(self):
        async def chunks():
            payload = {"error": {"message": "rate limited", "code": 429}}
            yield f"data: {json.dumps(payload)}\n\n"

        buf = ResumableBuffer("err")
        await pump_stream(buf, chunks())

        self.assertEqual(buf.status, "error")
        self.assertEqual(buf.status_code, 429)
        self.assertEqual(buf.error, "rate limited")
        self.assertTrue(buf.events[-1].strip().endswith("[DONE]"))

    async def test_cancelled_pump_finishes_buffer_and_wakes_reader(self):
        entered = asyncio.Event()

        async def chunks():
            entered.set()
            await asyncio.Event().wait()
            yield "unreachable"

        buf = ResumableBuffer("cancel")
        pump = asyncio.create_task(pump_stream(buf, chunks()))
        await entered.wait()
        waiter = asyncio.create_task(buf.wait_for(0, 10))
        pump.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pump
        await asyncio.wait_for(waiter, 1)
        self.assertEqual(buf.status, "error")


if __name__ == "__main__":
    unittest.main()
