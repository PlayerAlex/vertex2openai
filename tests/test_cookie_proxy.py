import json
import unittest

import httpx

from upstreams.cookie_proxy import (
    _execute_stream_request_generator_once,
    _make_sse_error,
)


async def _events_for_response(status_code, body):
    def handler(request):
        return httpx.Response(status_code, text=body, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return [event async for event in _execute_stream_request_generator_once(
            client, {}, {"query": "test"})]


class CookieProxyErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_status_is_preserved_in_retryable_event(self):
        events = await _events_for_response(503, "temporarily unavailable")
        self.assertEqual(events, [(
            "retryable_error",
            {"status_code": 503, "message": "temporarily unavailable"},
        )])

    async def test_graphql_4xx_error_is_fatal_not_assistant_text(self):
        body = json.dumps({"results": [{"errors": [{
            "message": "invalid request", "code": 400,
        }]}]})
        events = await _events_for_response(200, body)
        self.assertEqual(events, [(
            "fatal_error", {"status_code": 400, "message": "invalid request"},
        )])

    async def test_graphql_transient_code_is_retryable_even_without_keyword(self):
        body = json.dumps({"results": [{"errors": [{
            "message": "backend failure", "code": 503,
        }]}]})
        events = await _events_for_response(200, body)
        self.assertEqual(events, [(
            "retryable_error", {"status_code": 503, "message": "backend failure"},
        )])

    async def test_midstream_error_is_not_marked_retryable(self):
        body = "".join([
            json.dumps({"results": [{"data": {"candidates": [{
                "content": {"parts": [{"text": "partial"}]},
            }]}}]}),
            json.dumps({"results": [{"errors": [{
                "message": "quota exhausted", "code": 429,
            }]}]}),
        ])
        events = await _events_for_response(200, body)
        self.assertEqual(events[0], ("text", "partial"))
        self.assertEqual(events[1][0], "api_error_text")
        self.assertEqual(events[1][1]["status_code"], 429)

    async def test_cookie_auth_error_preserves_403(self):
        events = await _events_for_response(403, "permission denied")
        self.assertEqual(events[0][0], "cookie_error")
        self.assertEqual(events[0][1]["status_code"], 403)

    async def test_sse_error_has_openai_shape_and_real_code(self):
        frame = _make_sse_error({"status_code": 504, "message": "timed out"})
        payload = json.loads(frame.removeprefix("data: ").strip())
        self.assertEqual(payload["error"]["code"], 504)
        self.assertEqual(payload["error"]["type"], "timeout_error")
        self.assertEqual(payload["error"]["message"], "timed out")


if __name__ == "__main__":
    unittest.main()
