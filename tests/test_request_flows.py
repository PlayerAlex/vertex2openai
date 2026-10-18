"""Exercise request/retry/stream boundaries with an offline HTTP transport."""

import asyncio
import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import FastAPI

import api_helpers
from api_helpers import is_retryable_exception
from models import OpenAIRequest
from resumable import ResumableBuffer, ResumableStore, pump_stream
from routes import chat_api
from upstreams import cookie_proxy


def upstream_body(text="hello", finish="STOP"):
    return json.dumps({"results": [{"data": {"candidates": [{
        "content": {"parts": [{"text": text}] if text else []},
        "finishReason": finish,
    }]}}]})


def error_body(code, message):
    return json.dumps({"results": [{"errors": [{"code": code, "message": message}]}]})


def sse_payloads(frames):
    return [json.loads(line[5:].strip()) for frame in frames for line in frame.splitlines()
            if line.startswith("data:") and line[5:].strip() != "[DONE]"]


class CookieRequestFlowTests(unittest.IsolatedAsyncioTestCase):
    async def call_cookie(self, handler, stream, retries=1, model="gemini-3.1-pro-preview"):
        real_client = httpx.AsyncClient
        stats = Mock()
        transport = httpx.MockTransport(handler)
        request = OpenAIRequest(model=model, stream=stream,
                                messages=[{"role": "user", "content": "hello"}])
        frontend = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
        with ExitStack() as stack:
            stack.enter_context(patch.object(cookie_proxy, "_get_cookie_string", return_value="test"))
            stack.enter_context(patch.object(cookie_proxy, "_get_project_id", return_value="test-project"))
            stack.enter_context(patch.object(cookie_proxy, "build_headers", return_value={"Cookie": "test"}))
            stack.enter_context(patch.object(cookie_proxy, "get_retry_settings", return_value=(retries, 0)))
            stack.enter_context(patch.object(cookie_proxy, "build_batch_graphql_body_async",
                                             new=AsyncMock(return_value={"variables": {}})))
            stack.enter_context(patch.object(cookie_proxy.app_state, "get_effective_settings", return_value={}))
            stack.enter_context(patch.object(cookie_proxy.app_state, "get_setting",
                                             side_effect=lambda key, default=None: default))
            stack.enter_context(patch.object(cookie_proxy, "stats", stats))
            stack.enter_context(patch.object(cookie_proxy.traceback, "print_exc"))
            stack.enter_context(patch.object(cookie_proxy.httpx, "AsyncClient",
                                             side_effect=lambda **kw: real_client(transport=transport)))
            response = await cookie_proxy.CookieProxyUpstream().chat_completions(request, frontend)
            frames = [frame async for frame in response.body_iterator] if stream else []
        return response, frames, stats

    async def test_timeout_retries_then_preserves_success(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                calls = []

                def handler(request):
                    calls.append(request)
                    if len(calls) == 1:
                        raise httpx.ReadTimeout("", request=request)
                    return httpx.Response(200, text=upstream_body())

                response, frames, stats = await self.call_cookie(handler, stream)
                self.assertEqual(len(calls), 2)
                self.assertEqual(response.status_code, 200)
                if stream:
                    payloads = sse_payloads(frames)
                    self.assertFalse(any("error" in p for p in payloads))
                    content = "".join(p["choices"][0]["delta"].get("content", "") for p in payloads)
                    self.assertEqual(content, "hello")
                else:
                    self.assertEqual(json.loads(response.body)["choices"][0]["message"]["content"], "hello")
                stats.add_retry.assert_called_once()
                stats.add_success.assert_called_once()

    async def test_retry_exhaustion_reports_504_without_success(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                calls = []

                def handler(request):
                    calls.append(request)
                    raise httpx.ReadTimeout("", request=request)

                response, frames, stats = await self.call_cookie(handler, stream)
                self.assertEqual(len(calls), 2)
                payload = sse_payloads(frames)[-1] if stream else json.loads(response.body)
                self.assertEqual(payload["error"]["code"], 504)
                if not stream:
                    self.assertEqual(response.status_code, 504)
                stats.add_success.assert_not_called()

    async def test_auth_failure_is_not_retried_or_returned_as_assistant_text(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                handler = Mock(return_value=httpx.Response(403, text="permission denied: quota"))
                response, frames, stats = await self.call_cookie(handler, stream)
                handler.assert_called_once()
                payloads = sse_payloads(frames) if stream else [json.loads(response.body)]
                self.assertEqual(len(payloads), 1)
                self.assertEqual(payloads[0]["error"]["code"], 403)
                stats.add_retry.assert_not_called()
                stats.add_success.assert_not_called()

    async def test_midstream_failure_keeps_partial_content_without_replay(self):
        handler = Mock(return_value=httpx.Response(
            200, text=upstream_body("partial", "") + error_body(503, "backend unavailable")))
        _, frames, stats = await self.call_cookie(handler, True)
        payloads = sse_payloads(frames)
        handler.assert_called_once()
        self.assertEqual(payloads[-1]["error"]["code"], 503)
        choices = [p["choices"][0] for p in payloads if "choices" in p]
        self.assertEqual("".join(p["delta"].get("content", "") for p in choices), "partial")
        self.assertFalse(any(p.get("finish_reason") == "stop" for p in choices))
        stats.add_success.assert_not_called()

    async def test_content_filter_remains_a_completion_finish_reason(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                handler = Mock(return_value=httpx.Response(200, text=upstream_body("", "SAFETY")))
                response, frames, _ = await self.call_cookie(handler, stream)
                handler.assert_called_once()
                payloads = sse_payloads(frames) if stream else [json.loads(response.body)]
                self.assertEqual(response.status_code, 200)
                self.assertFalse(any("error" in p for p in payloads))
                self.assertEqual(payloads[-1]["choices"][0]["finish_reason"], "content_filter")

    async def test_image_stream_preserves_upstream_failure(self):
        handler = Mock(return_value=httpx.Response(429, text="quota exhausted"))
        _, frames, stats = await self.call_cookie(handler, True, retries=0, model="gemini-3-pro-image-preview")
        payloads = sse_payloads(frames)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["error"]["code"], 429)
        stats.add_success.assert_not_called()


class SDKRequestFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_setting_above_fifty_is_used_without_clamping(self):
        generate = AsyncMock(side_effect=httpx.ReadTimeout(""))
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
            generate_content=generate)))
        request = OpenAIRequest(model="gemini-3.1-pro-preview", messages=[], stream=False)

        def setting(key, default=None):
            return {"retry_max": 75, "retry_backoff_seconds": 0}.get(key, default)

        with patch.object(api_helpers.app_state, "get_setting", side_effect=setting):
            with self.assertRaises(httpx.ReadTimeout):
                await api_helpers.execute_gemini_call(
                    client, request.model, lambda _: [], {}, request)

        # retry_max 表示失败后的重试次数，所以 75 对应首次请求 + 75 次重试。
        self.assertEqual(generate.await_count, 76)

    async def test_zero_retry_setting_still_makes_one_request(self):
        generate = AsyncMock(side_effect=httpx.ReadTimeout(""))
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
            generate_content=generate)))
        request = OpenAIRequest(model="gemini-3.1-pro-preview", messages=[], stream=False)

        def setting(key, default=None):
            return {"retry_max": 0, "retry_backoff_seconds": 0}.get(key, default)

        with patch.object(api_helpers.app_state, "get_setting", side_effect=setting):
            with self.assertRaises(httpx.ReadTimeout):
                await api_helpers.execute_gemini_call(
                    client, request.model, lambda _: [], {}, request)

        self.assertEqual(generate.await_count, 1)

    def test_negative_and_invalid_retry_settings_remain_safe(self):
        with patch.object(
                api_helpers.app_state, "get_setting",
                side_effect=lambda key, default=None: {
                    "retry_max": -7, "retry_backoff_seconds": 0,
                }.get(key, default)):
            self.assertEqual(api_helpers.get_retry_settings(), (0, 0.0))

        with patch.object(
                api_helpers.app_state, "get_setting",
                side_effect=lambda key, default=None: {
                    "retry_max": "not-an-integer", "retry_backoff_seconds": 0,
                }.get(key, default)):
            self.assertEqual(
                api_helpers.get_retry_settings()[0],
                api_helpers.app_config.DEFAULT_SETTINGS["retry_max"],
            )

    def test_cookie_channel_uses_the_same_unclamped_retry_setting(self):
        def setting(key, default=None):
            return {"retry_max": 75, "retry_backoff_seconds": 0}.get(key, default)

        with patch.object(api_helpers.app_state, "get_setting", side_effect=setting):
            self.assertEqual(cookie_proxy.get_retry_settings(), (75, 0.0))

    async def test_sdk_stream_failure_is_reported_and_only_retried_before_output(self):
        for partial in (False, True):
            with self.subTest(partial=partial):
                async def source():
                    if partial:
                        yield SimpleNamespace(candidates=[])
                    raise httpx.ReadTimeout("")

                generate = AsyncMock(side_effect=lambda **kw: source())
                client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
                    generate_content_stream=generate)))
                request = OpenAIRequest(model="gemini-3.1-pro-preview", messages=[], stream=True)
                with patch.object(api_helpers, "get_retry_settings", return_value=(1, 0)), patch.object(
                        api_helpers.app_state, "get_setting", side_effect=lambda key, default=None: default), patch.object(
                        api_helpers, "convert_chunk_to_openai", return_value='data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'):
                    response = await api_helpers.execute_gemini_call(client, request.model, lambda _: [], {}, request)
                    frames = [frame async for frame in response.body_iterator]
                self.assertEqual(generate.await_count, 1 if partial else 2)
                payloads = sse_payloads(frames)
                self.assertEqual(payloads[-1]["error"]["code"], 504)
                self.assertEqual(len([p for p in payloads if "choices" in p]), int(partial))


class FailureBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def test_programming_and_local_protocol_errors_are_not_retried(self):
        for error in (RuntimeError("unexpected bug"), TypeError("bad arguments"),
                      httpx.LocalProtocolError("missing host"), httpx.UnsupportedProtocol("ftp")):
            with self.subTest(error=type(error).__name__):
                self.assertFalse(is_retryable_exception(error))

    def test_explicit_4xx_overrides_transport_context(self):
        error = RuntimeError("quota config rejected")
        error.status_code = 400
        error.__cause__ = httpx.ConnectError("")
        self.assertFalse(is_retryable_exception(error))

    async def test_expired_resume_does_not_generate_and_skip_a_new_answer(self):
        store = ResumableStore()
        request = SimpleNamespace(headers={"last-event-id": "3"}, query_params={})
        with patch.object(chat_api, "resumable_store", store), patch.object(
                chat_api, "_call_upstream", new=AsyncMock()) as upstream:
            response = await chat_api._resumable_stream(
                request, OpenAIRequest(model="test", messages=[], stream=True), "key")
        self.assertEqual(response.status_code, 404)
        upstream.assert_not_awaited()
        self.assertEqual(await store.list_meta(), [])

    async def test_dropping_unstarted_pump_finishes_waiting_buffer(self):
        store = ResumableStore()
        buf, _ = await store.create_if_absent("not-started", kind="json")
        task = asyncio.create_task(asyncio.sleep(60))
        store.register_pump(buf.id, task)
        await store.drop(buf.id)
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.wait_for(buf.wait_for(0, 60), timeout=1)
        self.assertTrue(buf.done)
        self.assertEqual(buf.status_code, 499)
        self.assertIn("error", buf.payload)

    async def test_pump_exception_preserves_timeout_and_closes_source(self):
        closed = []

        async def source():
            try:
                raise httpx.ReadTimeout("")
                yield "unreachable"
            finally:
                closed.append(True)

        buf = ResumableBuffer("timeout")
        await pump_stream(buf, source())
        self.assertEqual(buf.status, "error")
        self.assertEqual(buf.status_code, 504)
        self.assertEqual(sse_payloads(buf.events)[0]["error"]["code"], 504)
        self.assertEqual(closed, [True])

    async def test_capacity_failure_is_an_openai_503_at_http_boundary(self):
        app = FastAPI()
        app.include_router(chat_api.router)
        app.dependency_overrides[chat_api.get_api_key] = lambda: "key"
        store = ResumableStore()
        with patch("resumable.max_streams", return_value=1):
            original, _ = await store.create_if_absent("busy")
            with patch.object(chat_api, "resumable_store", store), patch.object(
                    chat_api, "resumable_enabled", return_value=True), patch.object(
                    chat_api, "_call_upstream", new=AsyncMock()) as upstream:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                             base_url="http://test") as client:
                    response = await client.post("/v1/chat/completions", json={
                        "model": "test", "messages": [], "stream": True})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], 503)
        self.assertIs(await store.get("busy"), original)
        upstream.assert_not_awaited()
