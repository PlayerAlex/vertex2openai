import unittest
from unittest.mock import patch

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import auth


class ApiKeyFormattingTests(unittest.TestCase):
    def test_format_api_key_is_stable_and_preserves_empty_values(self):
        self.assertIsNone(auth.format_api_key(None))
        self.assertEqual("", auth.format_api_key(""))
        self.assertEqual("ksv-legacy", auth.format_api_key("legacy"))
        self.assertEqual("ksv-ready", auth.format_api_key("ksv-ready"))

    def test_legacy_config_accepts_old_and_canonical_forms(self):
        self.assertTrue(auth.matches_api_key("legacy", "legacy"))
        self.assertTrue(auth.matches_api_key("ksv-legacy", "legacy"))

    def test_prefixed_config_requires_the_prefix(self):
        self.assertTrue(auth.matches_api_key("ksv-secret", "ksv-secret"))
        self.assertFalse(auth.matches_api_key("secret", "ksv-secret"))
        self.assertFalse(auth.matches_api_key("ksv-wrong", "ksv-secret"))
        self.assertFalse(auth.matches_api_key("", "ksv-secret"))
        self.assertFalse(auth.matches_api_key("ksv-secret", ""))

    def test_non_ascii_input_does_not_raise_during_comparison(self):
        self.assertFalse(auth.matches_api_key("错误密钥", "ksv-secret"))
        self.assertTrue(auth.matches_api_key("ksv-自定义密钥", "自定义密钥"))
        self.assertFalse(auth.matches_api_key("\ud800", "ksv-secret"))


class ApiBearerTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()

        @app.get("/protected")
        async def protected(api_key: str = Depends(auth.get_api_key)):
            return {"api_key": api_key}

        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()

    def test_bearer_accepts_legacy_and_canonical_key_during_migration(self):
        with patch.object(auth, "API_KEY", "legacy"):
            legacy = self.client.get("/protected", headers={"Authorization": "Bearer legacy"})
            canonical = self.client.get("/protected", headers={"Authorization": "Bearer ksv-legacy"})
        self.assertEqual(200, legacy.status_code)
        self.assertEqual(200, canonical.status_code)

    def test_bearer_for_prefixed_config_is_strict(self):
        with patch.object(auth, "API_KEY", "ksv-secret"):
            rejected = self.client.get("/protected", headers={"Authorization": "Bearer secret"})
            accepted = self.client.get("/protected", headers={"Authorization": "Bearer ksv-secret"})
            missing = self.client.get("/protected")
        self.assertEqual(401, rejected.status_code)
        self.assertEqual(200, accepted.status_code)
        self.assertEqual(401, missing.status_code)


if __name__ == "__main__":
    unittest.main()
