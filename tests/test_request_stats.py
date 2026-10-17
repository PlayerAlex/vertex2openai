import asyncio
import json
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from logger import ProxyStats, request_stats_managed
from request_stats import RequestStatsMiddleware, ResponseOutcome


class RequestStatsTests(unittest.IsolatedAsyncioTestCase):
    async def run_response(self, chunks, *, status=200, streaming=True,
                           before=None, finish=True, scope=None, counters=None):
        counters = counters or ProxyStats()
        sent = []

        async def app(scope, receive, send):
            if before:
                await before(counters)
            await send({'type': 'http.response.start', 'status': status, 'headers': [
                (b'content-type', b'text/event-stream' if streaming else b'application/json')]})
            for chunk in chunks:
                await send({'type': 'http.response.body', 'body': chunk, 'more_body': True})
            if finish:
                await send({'type': 'http.response.body', 'body': b'', 'more_body': False})

        async def receive():
            return {'type': 'http.request', 'body': b''}

        async def send(message):
            sent.append(message)

        scope = scope or {'type': 'http', 'method': 'POST', 'path': '/v1/chat/completions'}
        with patch('builtins.print') as log:
            await RequestStatsMiddleware(app, counters)(scope, receive, send)
        self.assertFalse(request_stats_managed.get())
        return counters.get_json_stats(), sent, log

    async def test_stream_success_without_usage_is_counted(self):
        data, sent, _ = await self.run_response([
            b': keep-alive\n\n', b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
            b'data: [DONE]\n\n'])
        self.assertEqual((data['total'], data['success'], data['error'], data['in_flight']), (1, 1, 0, 0))
        self.assertEqual(data['prompt_tokens'], 0)
        self.assertIn(b'keep-alive', sent[1]['body'])

    async def test_token_updates_and_legacy_signals_do_not_double_count(self):
        async def upstream(counters):
            counters.add_retry()
            counters.add_error()
            counters.add_tokens(10, 5)
            counters.add_tokens(2, 1)
            counters.add_success()

        data, _, _ = await self.run_response([b'data: [DONE]\n\n'], before=upstream)
        self.assertEqual((data['success'], data['error'], data['retries']), (1, 0, 1))
        self.assertEqual((data['prompt_tokens'], data['completion_tokens']), (12, 6))

    async def test_fragmented_sse_error_is_counted_even_with_http_200(self):
        frame = b'id: 0\r\ndata: {"error":{"code":429,"message":"secret-value"}}\r\n\r\ndata: [DONE]\n\n'
        data, _, log = await self.run_response([bytes([byte]) for byte in frame])
        self.assertEqual((data['success'], data['error']), (0, 1))
        self.assertEqual(data['error_breakdown'], {'rate_limit': 1})
        self.assertEqual(data['recent_errors'][0]['status'], 429)
        self.assertNotIn('secret-value', json.dumps(data) + str(log.call_args_list))

    async def test_auth_and_validation_failures_have_separate_categories(self):
        counters = ProxyStats()
        for status in (401, 422):
            data, _, log = await self.run_response([b'{"detail":"private-request"}'],
                status=status, streaming=False, counters=counters)
            self.assertIn(str(status), str(log.call_args_list))
        self.assertEqual(data['error_breakdown'], {'authentication': 1, 'invalid_request': 1})
        self.assertNotIn('private-request', json.dumps(data))

    async def test_multiline_sse_error_is_classified_as_one_event(self):
        data, _, _ = await self.run_response([
            b'event: message\ndata: {"error":\n',
            b'data: {"code":429,"message":"busy"}}\n\ndata: [DONE]\n\n'])
        self.assertEqual((data['success'], data['error']), (0, 1))
        self.assertEqual(data['error_breakdown'], {'rate_limit': 1})

    async def test_content_filter_and_incomplete_stream_are_not_successes(self):
        data, _, _ = await self.run_response([
            b'data: {"choices":[{"finish_reason":"content_filter"}]}\n\ndata: [DONE]\n\n'])
        self.assertEqual(data['error_breakdown'], {'content_filter': 1})
        data, _, _ = await self.run_response([b'data: {"choices":[]}\n\n'])
        self.assertEqual(data['error_breakdown'], {'stream_incomplete': 1})

    async def test_replayed_response_counts_delivery_without_repeating_usage(self):
        counters = ProxyStats()

        async def first(counters):
            counters.add_tokens(10, 5)

        await self.run_response([b'data: [DONE]\n\n'], counters=counters, before=first)
        data, _, _ = await self.run_response([b'data: [DONE]\n\n'], counters=counters)
        self.assertEqual((data['total'], data['success'], data['prompt_tokens']), (2, 2, 10))

    async def test_cancelled_reader_does_not_become_success_when_background_finishes(self):
        counters = ProxyStats()
        released = asyncio.Event()
        tasks = []

        async def app(scope, receive, send):
            async def background():
                await released.wait()
                counters.add_tokens(4, 2)
                counters.add_success()
                counters.add_error()
            tasks.append(asyncio.create_task(background()))
            raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError), patch('builtins.print'):
            await RequestStatsMiddleware(app, counters)(
                {'type': 'http', 'method': 'POST', 'path': '/v1/chat/completions'}, None, None)
        released.set()
        await asyncio.gather(*tasks)
        data = counters.get_json_stats()
        self.assertEqual((data['total'], data['success'], data['error'], data['cancelled']), (1, 0, 0, 1))
        self.assertEqual(data['prompt_tokens'], 4)
        self.assertFalse(request_stats_managed.get())

    async def test_non_chat_requests_are_not_counted(self):
        for method, path in [('GET', '/v1/models'), ('OPTIONS', '/v1/chat/completions'),
                             ('POST', '/api/express-keys/probe')]:
            data, _, _ = await self.run_response([b'{}'], streaming=False,
                scope={'type': 'http', 'method': method, 'path': path})
            self.assertEqual(data['total'], 0)

    def test_large_data_frame_is_bounded_and_later_error_still_detected(self):
        result = ResponseOutcome()
        result.streaming = True
        result.feed(b'data: ' + b'x' * (result.LIMIT * 2))
        self.assertLessEqual(len(result.pending), result.LIMIT)
        result.feed(b'\n\ndata: {"error":{"code":503}}\n\ndata: [DONE]\n\n')
        self.assertTrue(result.done)
        self.assertEqual(result.category, 'upstream_server')


class StreamingIntegrationTests(unittest.TestCase):
    def test_starlette_stream_error_and_success_settle_after_body(self):
        app = FastAPI()
        counters = ProxyStats()
        app.add_middleware(RequestStatsMiddleware, counters=counters)

        @app.post('/v1/chat/completions')
        async def chat(fail: bool = False):
            async def events():
                self.assertTrue(request_stats_managed.get())
                if fail:
                    counters.add_error()
                    yield 'data: {"error":{"code":503}}\n\n'
                else:
                    counters.add_success()
                    yield 'data: {"choices":[{"delta":{"content":"reply"}}]}\n\n'
                yield 'data: [DONE]\n\n'
            return StreamingResponse(events(), media_type='text/event-stream')

        with TestClient(app) as client, patch('builtins.print'):
            for fail in ('false', 'true'):
                response = client.post('/v1/chat/completions?fail=' + fail)
                self.assertEqual(response.status_code, 200)
                self.assertIn('[DONE]', response.text)
        data = counters.get_json_stats()
        self.assertEqual((data['total'], data['success'], data['error'], data['in_flight']), (2, 1, 1, 0))
        self.assertEqual(data['error_breakdown'], {'upstream_server': 1})


if __name__ == '__main__':
    unittest.main()
