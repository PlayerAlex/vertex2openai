import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from session_auth import SessionStore


class ClientApiKeyEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SessionStore(os.path.join(self.tmp.name, "sessions.json"))
        self.current = {"key": "ksv-before"}
        self.store_patch = patch.object(main, "_session_store", self.store)
        self.getter_patch = patch.object(
            main.app_state, "get_client_api_key", side_effect=lambda: self.current["key"]
        )
        self.store_patch.start()
        self.getter_patch.start()
        main._login_failures.clear()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        self.getter_patch.stop()
        self.store_patch.stop()
        self.tmp.cleanup()

    def login(self):
        response = self.client.post(
            "/api/login", json={"password": self.current["key"], "remember": True}
        )
        self.assertEqual(200, response.status_code)
        return response.cookies.get(main.AUTH_COOKIE)

    def rotate_to(self, new_key):
        def rotate():
            self.current["key"] = new_key
            return new_key

        return rotate

    def test_regenerate_requires_console_authentication(self):
        with patch.object(main.app_state, "rotate_client_api_key") as rotate:
            response = self.client.post("/api/client-config/regenerate")
        self.assertEqual(401, response.status_code)
        rotate.assert_not_called()

    def test_regenerate_returns_new_key_invalidates_old_session_and_renews_caller(self):
        old_token = self.login()
        new_key = "ksv-after"
        with patch.object(
            main.app_state,
            "rotate_client_api_key",
            side_effect=self.rotate_to(new_key),
        ):
            response = self.client.post(
                "/api/client-config/regenerate",
                headers={"origin": "http://testserver", "sec-fetch-site": "same-origin"},
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual({"api_key": new_key}, response.json())
        self.assertEqual("no-store", response.headers["cache-control"])
        self.assertFalse(self.store.is_valid(old_token, new_key))
        renewed_token = response.cookies.get(main.AUTH_COOKIE)
        self.assertTrue(renewed_token)
        self.assertTrue(self.store.is_valid(renewed_token, new_key))
        self.assertEqual(200, self.client.get("/api/client-config").status_code)
        self.client.post("/api/logout")
        self.assertEqual(
            401,
            self.client.post("/api/login", json={"password": "ksv-before"}).status_code,
        )
        self.assertEqual(
            200,
            self.client.post("/api/login", json={"password": new_key}).status_code,
        )

    def test_cross_site_request_cannot_rotate_even_with_valid_cookie(self):
        self.login()
        with patch.object(main.app_state, "rotate_client_api_key") as rotate:
            response = self.client.post(
                "/api/client-config/regenerate",
                headers={"origin": "https://evil.example", "sec-fetch-site": "cross-site"},
            )
        self.assertEqual(403, response.status_code)
        rotate.assert_not_called()
        self.assertNotIn(self.current["key"], response.text)

    def test_rotation_does_not_turn_temporary_login_into_remembered_login(self):
        self.client.post("/api/login", json={"password": self.current["key"], "remember": False})
        with patch.object(main.app_state, "rotate_client_api_key", side_effect=self.rotate_to("ksv-new")):
            response = self.client.post("/api/client-config/regenerate")
        self.assertEqual(200, response.status_code)
        self.assertNotIn("Max-Age", response.headers["set-cookie"])
        self.assertFalse(self.store.is_remembered(response.cookies.get(main.AUTH_COOKIE)))

    def test_persistence_failure_is_safe_and_does_not_leak_exception_text(self):
        self.login()
        with patch.object(
            main.app_state,
            "rotate_client_api_key",
            side_effect=OSError("secret-key-that-must-not-leak"),
        ), patch("builtins.print"):
            response = self.client.post("/api/client-config/regenerate")

        self.assertEqual(500, response.status_code)
        self.assertIn("检查存储空间和文件权限", response.json()["error"])
        self.assertNotIn("secret-key-that-must-not-leak", response.text)
        self.assertEqual(200, self.client.get("/api/client-config").status_code)

    def test_session_renewal_failure_does_not_hide_successful_new_key(self):
        self.login()
        new_key = "ksv-persisted"
        with patch.object(
            main.app_state,
            "rotate_client_api_key",
            side_effect=self.rotate_to(new_key),
        ), patch.object(main, "_issue_session", side_effect=OSError("session write failed")), patch(
            "builtins.print"
        ):
            response = self.client.post("/api/client-config/regenerate")

        self.assertEqual(200, response.status_code)
        self.assertEqual({"api_key": new_key}, response.json())
        self.assertNotIn("session write failed", response.text)


if __name__ == "__main__":
    unittest.main()
