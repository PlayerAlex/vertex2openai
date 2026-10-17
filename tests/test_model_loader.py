import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
import model_loader


class ModelLoaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.local = Path(self.temp.name) / "vertexModels.json"
        self.local.write_text(json.dumps({"models": ["local-model"]}), encoding="utf-8")
        self.patches = [
            patch.object(model_loader, "_LOCAL_MODEL_FILE_CANDIDATES", [self.local]),
            patch.object(model_loader.app_config, "MODELS_CONFIG_URL", ""),
            patch.object(model_loader, "_model_cache", None),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    async def test_default_uses_local_file_without_network(self):
        with patch.object(model_loader.httpx, "AsyncClient") as client:
            result = await model_loader.get_models_config()
        self.assertEqual(result, {"models": ["local-model"]})
        client.assert_not_called()

    async def test_legacy_local_format_is_supported(self):
        self.local.write_text(json.dumps({"vertex_express_models": ["legacy-model"]}), encoding="utf-8")
        self.assertEqual(await model_loader.get_express_models(), ["legacy-model"])

    async def test_explicit_url_is_used_and_cached(self):
        remote = "https://example.test/custom-models.json"
        client = AsyncMock()
        client.get.return_value = httpx.Response(200, json={"models": ["remote-model"]}, request=httpx.Request("GET", remote))
        with patch.object(model_loader.app_config, "MODELS_CONFIG_URL", remote), patch.object(
                model_loader.httpx, "AsyncClient", return_value=client):
            client.__aenter__.return_value = client
            self.assertEqual(await model_loader.get_express_models(), ["remote-model"])
            self.assertEqual(await model_loader.get_express_models(), ["remote-model"])
        client.get.assert_awaited_once_with(remote)
        client.__aexit__.assert_awaited_once()

    async def test_failed_remote_falls_back_locally_without_other_urls(self):
        remote = "https://example.test/custom-models.json"
        for response in (
            httpx.ConnectError("offline"),
            httpx.Response(503, request=httpx.Request("GET", remote)),
            httpx.Response(200, text="invalid-json", request=httpx.Request("GET", remote)),
            httpx.Response(200, json={"invalid": []}, request=httpx.Request("GET", remote)),
        ):
            with self.subTest(response=type(response).__name__):
                client = AsyncMock()
                client.__aenter__.return_value = client
                if isinstance(response, Exception):
                    client.get.side_effect = response
                else:
                    client.get.return_value = response
                with patch.object(model_loader.app_config, "MODELS_CONFIG_URL", remote), patch.object(
                        model_loader.httpx, "AsyncClient", return_value=client):
                    result = await model_loader.fetch_and_parse_models_config()
                self.assertEqual(result, {"models": ["local-model"]})
                client.get.assert_awaited_once_with(remote)

    async def test_refresh_reloads_local_file(self):
        await model_loader.get_models_config()
        self.local.write_text(json.dumps({"models": ["updated-model"]}), encoding="utf-8")
        self.assertTrue(await model_loader.refresh_models_config_cache())
        self.assertEqual(await model_loader.get_express_models(), ["updated-model"])
