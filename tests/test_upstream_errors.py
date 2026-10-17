import asyncio
import unittest

import httpx

from api_helpers import extract_upstream_error, is_retryable_exception


class UpstreamErrorTests(unittest.TestCase):
    def test_http_status_error_uses_response_status(self):
        request = httpx.Request("GET", "https://example.test")
        response = httpx.Response(429, request=request)
        error = httpx.HTTPStatusError("rate limited", request=request, response=response)

        self.assertEqual(extract_upstream_error(error), (429, "rate limited"))
        self.assertTrue(is_retryable_exception(error))

    def test_sdk_status_code_and_non_retryable_4xx_are_preserved(self):
        class SDKError(Exception):
            status_code = 403

        error = SDKError("permission denied")
        self.assertEqual(extract_upstream_error(error), (403, "permission denied"))
        self.assertFalse(is_retryable_exception(error))

    def test_sdk_code_503_is_retryable(self):
        class SDKError(Exception):
            code = 503

        error = SDKError("temporarily unavailable")
        self.assertEqual(extract_upstream_error(error)[0], 503)
        self.assertTrue(is_retryable_exception(error))

    def test_empty_timeout_and_network_errors_have_useful_fallbacks(self):
        request = httpx.Request("GET", "https://example.test")
        timeout = httpx.ReadTimeout("", request=request)
        protocol = httpx.RemoteProtocolError("", request=request)

        self.assertEqual(extract_upstream_error(timeout), (504, "ReadTimeout"))
        self.assertEqual(extract_upstream_error(protocol), (502, "RemoteProtocolError"))
        self.assertTrue(is_retryable_exception(timeout))
        self.assertTrue(is_retryable_exception(protocol))

    def test_cause_chain_is_used_and_cycles_are_safe(self):
        inner = asyncio.TimeoutError()
        outer = RuntimeError("wrapper")
        outer.__cause__ = inner
        self.assertEqual(extract_upstream_error(outer), (504, "wrapper"))
        self.assertTrue(is_retryable_exception(outer))

        first = RuntimeError("first")
        second = RuntimeError("second")
        first.__cause__ = second
        second.__cause__ = first
        self.assertEqual(extract_upstream_error(first), (500, "first"))
        self.assertFalse(is_retryable_exception(first))

    def test_status_embedded_in_plain_message_is_preserved(self):
        error = RuntimeError("upstream returned HTTP 429")
        self.assertEqual(extract_upstream_error(error)[0], 429)
        self.assertTrue(is_retryable_exception(error))


if __name__ == "__main__":
    unittest.main()
