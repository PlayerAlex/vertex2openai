import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import api_helpers
from express_key_manager import ExpressKeyManager
from models import OpenAIRequest
from upstreams.express_sdk import _close_sdk_clients, _stream_with_client_cleanup


class StatusError(Exception):
    def __init__(self, status_code):
        super().__init__(f"upstream HTTP {status_code}")
        self.status_code = status_code


def response_obj():
    return SimpleNamespace(candidates=[])


def sdk_client(*, generate=None, stream=None):
    models = SimpleNamespace(
        generate_content=generate or AsyncMock(return_value=response_obj()),
        generate_content_stream=stream or AsyncMock(),
    )
    return SimpleNamespace(aio=SimpleNamespace(models=models))


class CandidateOrderTests(unittest.TestCase):
    def test_round_robin_candidates_keep_order_and_deduplicate_keys(self):
        manager = ExpressKeyManager()
        records = [
            {"key": "key-a", "project_id": "project-a"},
            {"key": "key-b", "project_id": "project-b"},
            {"key": "key-a", "project_id": "duplicate"},
        ]
        with patch("express_key_manager.app_state.get_express_keys", return_value=records), patch(
                "express_key_manager.app_state.get_setting", return_value=True):
            first = manager.get_express_candidates()
            second = manager.get_express_candidates()

        self.assertEqual([r["key"] for r in first], ["key-a", "key-b"])
        self.assertEqual([r["key"] for r in second], ["key-b", "key-a"])


class ExpressFailoverTests(unittest.IsolatedAsyncioTestCase):
    async def _nonstream(self, first_error, second_error=None, retries=1, with_failover=True):
        first_generate = AsyncMock(side_effect=first_error)
        second_generate = AsyncMock(
            side_effect=second_error if second_error is not None else None,
            return_value=response_obj(),
        )
        first = sdk_client(generate=first_generate)
        second = sdk_client(generate=second_generate)
        replacements = [{"client": second, "model": "projects/b/model"}] if with_failover else []
        request = OpenAIRequest(model="gemini-3.1-pro-preview", messages=[], stream=False)

        with patch.object(api_helpers, "get_retry_settings", return_value=(retries, 0)), patch.object(
                api_helpers, "is_gemini_response_valid", return_value=True), patch.object(
                api_helpers, "convert_to_openai_format", return_value={"choices": []}):
            result = await api_helpers.execute_gemini_call(
                first, "projects/a/model", lambda _: [], {}, request,
                failover_factory=lambda: replacements.pop(0) if replacements else None,
            )
        return result, first_generate, second_generate

    async def test_429_switches_to_next_key_and_succeeds(self):
        result, first, second = await self._nonstream(StatusError(429))
        self.assertEqual(result.status_code, 200)
        self.assertEqual((first.await_count, second.await_count), (1, 1))
        self.assertEqual(second.await_args.kwargs["model"], "projects/b/model")

    async def test_403_switches_to_next_key_and_succeeds(self):
        result, first, second = await self._nonstream(StatusError(403))
        self.assertEqual(result.status_code, 200)
        self.assertEqual((first.await_count, second.await_count), (1, 1))

    async def test_all_keys_fail_with_global_attempt_budget(self):
        with self.assertRaises(StatusError) as caught:
            await self._nonstream(StatusError(429), StatusError(429), retries=1)
        self.assertEqual(caught.exception.status_code, 429)

    async def test_single_key_keeps_regular_retry_semantics(self):
        generate = AsyncMock(side_effect=[StatusError(429), response_obj()])
        client = sdk_client(generate=generate)
        request = OpenAIRequest(model="gemini-3.1-pro-preview", messages=[], stream=False)
        with patch.object(api_helpers, "get_retry_settings", return_value=(1, 0)), patch.object(
                api_helpers, "is_gemini_response_valid", return_value=True), patch.object(
                api_helpers, "convert_to_openai_format", return_value={"choices": []}):
            result = await api_helpers.execute_gemini_call(
                client, request.model, lambda _: [], {}, request,
                failover_factory=lambda: None,
            )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(generate.await_count, 2)

    async def test_retry_zero_makes_exactly_one_upstream_call(self):
        first_generate = AsyncMock(side_effect=StatusError(429))
        second_generate = AsyncMock(return_value=response_obj())
        first = sdk_client(generate=first_generate)
        second = sdk_client(generate=second_generate)
        switched = Mock(return_value={"client": second, "model": "second"})
        request = OpenAIRequest(model="gemini-3.1-pro-preview", messages=[], stream=False)
        with patch.object(api_helpers, "get_retry_settings", return_value=(0, 0)):
            with self.assertRaises(StatusError):
                await api_helpers.execute_gemini_call(
                    first, request.model, lambda _: [], {}, request,
                    failover_factory=switched,
                )
        self.assertEqual(first_generate.await_count, 1)
        second_generate.assert_not_awaited()
        switched.assert_not_called()

    async def test_real_stream_does_not_switch_after_content(self):
        async def partial_source():
            yield SimpleNamespace(candidates=[SimpleNamespace()])
            raise StatusError(429)

        first_generate = AsyncMock(side_effect=lambda **_: partial_source())
        second_generate = AsyncMock(side_effect=lambda **_: partial_source())
        first = sdk_client(stream=first_generate)
        second = sdk_client(stream=second_generate)
        switched = Mock(return_value={"client": second, "model": "second"})
        request = OpenAIRequest(model="gemini-3.1-pro-preview", messages=[], stream=True)
        with patch.object(api_helpers, "get_retry_settings", return_value=(2, 0)), patch.object(
                api_helpers.app_state, "get_setting", side_effect=lambda key, default=None: default), patch.object(
                api_helpers, "convert_chunk_to_openai", return_value='data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'):
            response = await api_helpers.execute_gemini_call(
                first, request.model, lambda _: [], {}, request, failover_factory=switched)
            frames = [frame async for frame in response.body_iterator]

        self.assertTrue(any("partial" in frame for frame in frames))
        self.assertEqual(first_generate.await_count, 1)
        second_generate.assert_not_awaited()
        switched.assert_not_called()

    async def test_real_stream_switches_when_iteration_fails_before_content(self):
        async def failing_source():
            raise StatusError(429)
            yield  # pragma: no cover

        async def success_source():
            yield SimpleNamespace(candidates=[SimpleNamespace()])

        first_generate = AsyncMock(side_effect=lambda **_: failing_source())
        second_generate = AsyncMock(side_effect=lambda **_: success_source())
        first = sdk_client(stream=first_generate)
        second = sdk_client(stream=second_generate)
        replacements = [{"client": second, "model": "second"}]
        request = OpenAIRequest(model="gemini-3.1-pro-preview", messages=[], stream=True)
        with patch.object(api_helpers, "get_retry_settings", return_value=(1, 0)), patch.object(
                api_helpers.app_state, "get_setting", side_effect=lambda key, default=None: default), patch.object(
                api_helpers, "convert_chunk_to_openai", return_value='data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'):
            response = await api_helpers.execute_gemini_call(
                first, request.model, lambda _: [], {}, request,
                failover_factory=lambda: replacements.pop(0) if replacements else None)
            frames = [frame async for frame in response.body_iterator]

        self.assertTrue(any('"ok"' in frame for frame in frames))
        self.assertEqual((first_generate.await_count, second_generate.await_count), (1, 1))

    async def test_real_stream_switches_when_initial_request_fails(self):
        async def success_source():
            yield SimpleNamespace(candidates=[SimpleNamespace()])

        first_generate = AsyncMock(side_effect=StatusError(403))
        second_generate = AsyncMock(side_effect=lambda **_: success_source())
        first = sdk_client(stream=first_generate)
        second = sdk_client(stream=second_generate)
        replacements = [{"client": second, "model": "second"}]
        request = OpenAIRequest(model="gemini-3.1-pro-preview", messages=[], stream=True)
        with patch.object(api_helpers, "get_retry_settings", return_value=(1, 0)), patch.object(
                api_helpers.app_state, "get_setting", side_effect=lambda key, default=None: default), patch.object(
                api_helpers, "convert_chunk_to_openai", return_value='data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'):
            response = await api_helpers.execute_gemini_call(
                first, request.model, lambda _: [], {}, request,
                failover_factory=lambda: replacements.pop(0) if replacements else None)
            frames = [frame async for frame in response.body_iterator]

        self.assertTrue(any('"ok"' in frame for frame in frames))
        self.assertEqual((first_generate.await_count, second_generate.await_count), (1, 1))

    async def test_image_fake_stream_switches_before_generated_content(self):
        first_generate = AsyncMock(side_effect=StatusError(429))
        second_generate = AsyncMock(return_value=response_obj())
        first = sdk_client(generate=first_generate)
        second = sdk_client(generate=second_generate)
        replacements = [{"client": second, "model": "second-image"}]
        request = OpenAIRequest(model="gemini-3-pro-image-preview", messages=[], stream=True)
        with patch.object(api_helpers, "get_retry_settings", return_value=(1, 0)), patch.object(
                api_helpers.app_state, "get_setting", side_effect=lambda key, default=None: 0 if key == "fake_streaming_interval" else default), patch.object(
                api_helpers, "convert_to_openai_format", return_value={"choices": []}):
            response = await api_helpers.execute_gemini_call(
                first, request.model, lambda _: [], {}, request,
                failover_factory=lambda: replacements.pop(0) if replacements else None)
            frames = [frame async for frame in response.body_iterator]

        self.assertTrue(any("[DONE]" in frame for frame in frames))
        self.assertEqual((first_generate.await_count, second_generate.await_count), (1, 1))

    async def test_image_fake_stream_retry_zero_never_creates_next_client(self):
        generate = AsyncMock(side_effect=StatusError(429))
        switched = Mock()
        request = OpenAIRequest(model="gemini-3-pro-image-preview", messages=[], stream=True)
        with patch.object(api_helpers, "get_retry_settings", return_value=(0, 0)), patch.object(
                api_helpers.app_state, "get_setting", side_effect=lambda key, default=None: 0 if key == "fake_streaming_interval" else default):
            response = await api_helpers.execute_gemini_call(
                sdk_client(generate=generate), request.model, lambda _: [], {}, request,
                failover_factory=switched)
            frames = [frame async for frame in response.body_iterator]
        self.assertTrue(any('"error"' in frame for frame in frames))
        generate.assert_awaited_once()
        switched.assert_not_called()


class SDKClientCleanupTests(unittest.IsolatedAsyncioTestCase):
    def _client(self):
        return SimpleNamespace(
            aio=SimpleNamespace(aclose=AsyncMock()),
            close=Mock(),
        )

    async def test_normal_and_error_cleanup_close_both_transports(self):
        for raises in (False, True):
            with self.subTest(raises=raises):
                client = self._client()

                async def source():
                    if raises:
                        raise RuntimeError("stream failed")
                    yield b"ok"

                wrapped = _stream_with_client_cleanup(source(), [client])
                if raises:
                    with self.assertRaises(RuntimeError):
                        await anext(wrapped)
                else:
                    self.assertEqual([item async for item in wrapped], [b"ok"])
                client.aio.aclose.assert_awaited_once()
                client.close.assert_called_once()

    async def test_cancelled_stream_closes_both_transports(self):
        client = self._client()
        entered = asyncio.Event()

        async def source():
            entered.set()
            await asyncio.Future()
            yield b"never"

        wrapped = _stream_with_client_cleanup(source(), [client])
        task = asyncio.create_task(anext(wrapped))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        client.aio.aclose.assert_awaited_once()
        client.close.assert_called_once()

    async def test_direct_cleanup_handles_unused_client(self):
        client = self._client()
        await _close_sdk_clients([client])
        client.aio.aclose.assert_awaited_once()
        client.close.assert_called_once()

    async def test_early_stream_close_also_closes_source(self):
        client = self._client()
        closed = []

        async def source():
            try:
                yield b"first"
                yield b"second"
            finally:
                closed.append(True)

        wrapped = _stream_with_client_cleanup(source(), [client])
        self.assertEqual(await anext(wrapped), b"first")
        await wrapped.aclose()
        self.assertEqual(closed, [True])
        client.aio.aclose.assert_awaited_once()
