import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from models import OpenAIRequest
from resumable import DONE_FRAME, ResumableBuffer, ResumableStore, compute_stream_id
from routes import chat_api


class FakeRequest:
    def __init__(self, headers=None, query=None):
        self.headers = headers or {}
        self.query_params = query or {}


class ResumeCursorTests(unittest.TestCase):
    def test_last_event_id_means_last_received_but_resume_from_is_next(self):
        self.assertEqual(chat_api._resume_start_seq(FakeRequest(
            headers={"last-event-id": "4"}, query={"resume_from": "20"})), 5)
        self.assertEqual(chat_api._resume_start_seq(FakeRequest(
            query={"resume_from": "4"})), 4)


class ChatApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_cursor_replays_completed_delivered_stream(self):
        store = ResumableStore()
        request_obj = OpenAIRequest(model="test", messages=[], stream=True)
        request = FakeRequest(
            headers={"x-request-id": "stable", "last-event-id": "0"})
        stream_id, _ = compute_stream_id(
            request.headers, "key", chat_api._request_fingerprint(request_obj))
        buf, _ = await store.create_if_absent(stream_id, model="test", kind="stream")
        await buf.append("id: 0\ndata: first\n\n")
        await buf.append("id: 1\n" + DONE_FRAME)
        await buf.finish()
        buf.mark_delivered()

        with patch.object(chat_api, "resumable_store", store), patch.object(
                chat_api, "_call_upstream", new=AsyncMock()) as upstream:
            response = await chat_api._resumable_stream(request, request_obj, "key")
            frames = [frame async for frame in response.body_iterator]

        upstream.assert_not_awaited()
        self.assertEqual(frames, ["id: 1\n" + DONE_FRAME])
        self.assertEqual(response.headers["x-resumable-replay"], "1")

    async def test_json_exception_preserves_status_and_openai_shape(self):
        class RateLimitError(Exception):
            status_code = 429

        request_obj = OpenAIRequest(model="test", messages=[], stream=False)
        request = FakeRequest(headers={"x-request-id": "json-error"})
        store = ResumableStore()
        with patch.object(chat_api, "resumable_store", store), patch.object(
                chat_api, "_call_upstream", new=AsyncMock(
                    side_effect=RateLimitError("quota exceeded"))):
            response = await chat_api._resumable_json(request, request_obj, "key")

        self.assertEqual(response.status_code, 429)
        body = json.loads(response.body)
        self.assertEqual(body["error"]["code"], 429)
        self.assertIn("quota exceeded", body["error"]["message"])

    async def test_json_poll_returns_completed_payload_and_status(self):
        store = ResumableStore()
        buf, _ = await store.create_if_absent("json-id", kind="json")
        payload = {"error": {"message": "denied", "code": 403}}
        await buf.finish("error", payload=payload, status_code=403)

        with patch.object(chat_api, "resumable_store", store):
            response = await chat_api.resumable_poll(
                "json-id", FakeRequest(), api_key="key", from_=0, limit=200, wait=0)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(json.loads(response.body), payload)

    async def test_json_poll_reports_running_buffer_as_accepted(self):
        store = ResumableStore()
        await store.create_if_absent("running-json", kind="json")
        with patch.object(chat_api, "resumable_store", store):
            response = await chat_api.resumable_poll(
                "running-json", FakeRequest(), api_key="key", from_=0, limit=200, wait=0)
        self.assertEqual(response.status_code, 202)
        self.assertFalse(json.loads(response.body)["done"])


if __name__ == "__main__":
    unittest.main()
