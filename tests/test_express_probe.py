import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

import express_key_probe as probe


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, *_args, **_kwargs):
        if self.error:
            raise self.error
        return self.response


def response(status_code, text=""):
    return SimpleNamespace(status_code=status_code, text=text)


class ProbeClassificationTests(unittest.IsolatedAsyncioTestCase):
    async def _probe(self, status_code, text=""):
        with patch.object(probe.httpx, "AsyncClient",
                          return_value=FakeClient(response(status_code, text))):
            return await probe.probe_project_id_detailed("AIza-secret-value")

    async def test_success_extracts_project(self):
        result = await self._probe(
            404,
            'Resource projects/my-project-123/locations/global/publishers/google/models/missing was not found',
        )
        self.assertEqual("success", result["status"])
        self.assertEqual("my-project-123", result["project_id"])
        self.assertTrue(result["ok"])

    async def test_http_failures_have_stable_categories(self):
        cases = [
            (400, "invalid_key"),
            (401, "invalid_key"),
            (403, "permission_denied"),
            (429, "rate_limited"),
            (503, "upstream_error"),
            (404, "unrecognized_response"),
        ]
        for status_code, expected in cases:
            with self.subTest(status_code=status_code):
                result = await self._probe(status_code, "response without a project path")
                self.assertEqual(expected, result["status"])
                self.assertFalse(result["ok"])

    async def test_timeout_and_network_errors_do_not_expose_key(self):
        secret = "AIza-very-private-value"
        request = httpx.Request("POST", f"https://example.test/probe?key={secret}")
        errors = [
            (asyncio.TimeoutError(), "timeout"),
            (httpx.ConnectError(f"failed URL key={secret}", request=request), "network_error"),
            (RuntimeError(f"unexpected key={secret}"), "probe_error"),
        ]
        for error, expected in errors:
            with self.subTest(expected=expected), patch.object(
                    probe.httpx, "AsyncClient", return_value=FakeClient(error=error)):
                result = await probe.probe_project_id_detailed(secret)
                self.assertEqual(expected, result["status"])
                self.assertNotIn(secret, result["message"])


class ProbeStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_project_is_never_overwritten_even_with_force(self):
        record = {
            "key": "key-a",
            "project_id": "manual-project",
            "project_source": "manual",
        }
        detailed = AsyncMock()
        update = Mock()
        with patch.object(probe.app_state, "get_express_keys", return_value=[record]), patch.object(
                probe.app_state, "update_express_key_project", update), patch.object(
                probe, "probe_project_id_detailed", detailed):
            result = await probe.probe_and_store("key-a", force=True)
        self.assertEqual("skipped_manual", result["status"])
        self.assertEqual("manual-project", result["project_id"])
        detailed.assert_not_awaited()
        update.assert_not_called()

    async def test_cached_auto_project_only_refreshes_when_forced(self):
        record = {
            "key": "key-a",
            "project_id": "old-project",
            "project_source": "auto",
        }
        detailed = AsyncMock(return_value={
            "ok": True,
            "status": "success",
            "project_id": "new-project",
            "message": "ok",
        })
        update = Mock(return_value=True)
        with patch.object(probe.app_state, "get_express_keys", return_value=[record]), patch.object(
                probe.app_state, "update_express_key_project", update), patch.object(
                probe, "probe_project_id_detailed", detailed):
            cached = await probe.probe_and_store("key-a")
            refreshed = await probe.probe_and_store("key-a", force=True)
        self.assertEqual("cached", cached["status"])
        self.assertEqual("success", refreshed["status"])
        detailed.assert_awaited_once()
        update.assert_called_once_with("key-a", "new-project", source="auto")

    async def test_unsaved_draft_is_probed_but_not_persisted(self):
        detailed = AsyncMock(return_value={
            "ok": True,
            "status": "success",
            "project_id": "draft-project",
            "message": "ok",
        })
        update = Mock()
        with patch.object(probe.app_state, "get_express_keys", return_value=[]), patch.object(
                probe.app_state, "update_express_key_project", update), patch.object(
                probe, "probe_project_id_detailed", detailed):
            result = await probe.probe_and_store("draft-key", allow_unknown=True)
        self.assertEqual("draft-project", result["project_id"])
        detailed.assert_awaited_once_with("draft-key")
        update.assert_not_called()


class ProbeBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_deduplicates_and_limits_concurrency(self):
        active = 0
        maximum = 0

        async def fake(key, **_kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
            return {"key": key, "ok": True, "status": "success"}

        keys = [f"key-{index}" for index in range(10)] + ["key-2", {"key": "key-3"}]
        with patch.object(probe, "probe_and_store", side_effect=fake) as mocked:
            results = await probe.probe_all(keys=keys, allow_unknown=True)
        self.assertEqual(10, len(results))
        self.assertEqual(10, mocked.await_count)
        self.assertLessEqual(maximum, probe.PROBE_CONCURRENCY)

    async def test_one_failure_does_not_cancel_other_keys(self):
        async def fake(key, **_kwargs):
            if key == "bad":
                raise RuntimeError("sensitive failure")
            return {"key": key, "ok": True, "status": "success"}

        with patch.object(probe, "probe_and_store", side_effect=fake):
            results = await probe.probe_all(keys=["good-a", "bad", "good-b"], allow_unknown=True)
        self.assertEqual(["success", "probe_error", "success"],
                         [item["status"] for item in results])
        self.assertNotIn("sensitive failure", results[1]["message"])


if __name__ == "__main__":
    unittest.main()
