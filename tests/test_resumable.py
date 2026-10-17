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
    sse_from_buffer,
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


class BufferHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_heartbeats_keep_data_cursor_and_delivery_unchanged(self):
        buf = ResumableBuffer('heartbeat')
        reader = sse_from_buffer(buf, heartbeat_interval=0.01)
        for _ in range(2):
            self.assertEqual(await asyncio.wait_for(anext(reader), 1), ': keep-alive\n\n')
        self.assertEqual(buf.next_seq, 0)
        self.assertFalse(buf.fully_delivered)
        await buf.append('id: 0\ndata: {"choices":[]}\n\n')
        await buf.append('id: 1\n' + DONE_FRAME)
        await buf.finish()
        frames = [frame async for frame in reader]
        self.assertEqual(frames, buf.events)
        self.assertTrue(buf.fully_delivered)
        self.assertEqual(buf.readers, 0)
        self.assertEqual([frame async for frame in sse_from_buffer(buf, 1)], ['id: 1\n' + DONE_FRAME])

    async def test_reader_closing_during_heartbeat_releases_reader_only(self):
        buf = ResumableBuffer('disconnect')
        reader = sse_from_buffer(buf, heartbeat_interval=0.01)
        await anext(reader)
        self.assertEqual(buf.readers, 1)
        await reader.aclose()
        self.assertEqual(buf.readers, 0)
        self.assertFalse(buf.done)
        self.assertFalse(buf.fully_delivered)


class PumpStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_combined_heartbeat_and_data_keep_body_and_per_event_ids(self):
        async def chunks():
            yield ': keep-alive\n\ndata: {"choices":[{"delta":{"content":"hello"}}]}\n\ndata: [DONE]\n\n'

        buf = ResumableBuffer('combined')
        await pump_stream(buf, chunks())
        self.assertEqual(len(buf.events), 2)
        self.assertIn('hello', buf.events[0])
        self.assertTrue(buf.events[0].startswith('id: 0\ndata:'))
        self.assertEqual(buf.events[1], 'id: 1\n' + DONE_FRAME)
        self.assertEqual([frame async for frame in sse_from_buffer(buf, 1)], [buf.events[1]])

    async def test_utf8_frames_split_across_chunks_keep_text_and_error_status(self):
        payload = ': keep-alive\r\n\r\ndata: {"choices":[{"delta":{"content":"你好"}}]}\r\n\r\ndata: {"error":{"code":504,"message":"timeout"}}\r\n\r\ndata: [DONE]\r\n\r\n'

        async def chunks():
            for byte in payload.encode('utf-8'):
                yield bytes([byte])

        buf = ResumableBuffer('split')
        await pump_stream(buf, chunks())
        self.assertEqual(len(buf.events), 3)
        self.assertIn('你好', buf.events[0])
        self.assertEqual(buf.status_code, 504)
        self.assertEqual(buf.status, 'error')
        self.assertTrue(buf.events[1].startswith('id: 1\n'))
        self.assertEqual(buf.events[2], 'id: 2\n' + DONE_FRAME)

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
