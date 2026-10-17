import json
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.requests import Request

import main
from session_auth import SessionStore
from console_access import is_local_console_request


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "console_sessions.json")
        self.now = [1_000.0]

    def tearDown(self):
        self.tmp.cleanup()

    def store(self):
        return SessionStore(
            self.path,
            session_ttl=60,
            remembered_ttl=300,
            max_sessions=4,
            time_fn=lambda: self.now[0],
        )

    def test_remembered_session_survives_restart_without_storing_secrets(self):
        token = self.store().issue("private-api-key", remember=True)
        restarted = self.store()
        self.assertTrue(restarted.is_valid(token, "private-api-key"))
        with open(self.path, "r", encoding="utf-8") as handle:
            raw = handle.read()
        self.assertNotIn(token, raw)
        self.assertNotIn("private-api-key", raw)
        self.assertEqual(1, len(json.loads(raw)["sessions"]))

    def test_ordinary_session_does_not_survive_restart(self):
        token = self.store().issue("key", remember=False)
        self.assertFalse(self.store().is_valid(token, "key"))

    def test_malformed_persistent_record_does_not_prevent_startup(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"sessions": {"bad": {"persistent": True, "expires_at": "broken"}}}, handle)
        store = self.store()
        self.assertFalse(store.is_valid("bad", "key"))
        self.assertTrue(store.is_valid(store.issue("key"), "key"))

    def test_expired_session_is_rejected_and_removed(self):
        store = self.store()
        token = store.issue("key", remember=True)
        self.now[0] += 301
        self.assertFalse(store.is_valid(token, "key"))
        self.assertFalse(self.store().is_valid(token, "key"))

    def test_api_key_rotation_invalidates_existing_session(self):
        store = self.store()
        token = store.issue("old-key", remember=True)
        self.assertFalse(store.is_valid(token, "new-key"))
        self.assertFalse(self.store().is_valid(token, "old-key"))

    def test_revoke_persists_across_restart(self):
        store = self.store()
        token = store.issue("key", remember=True)
        store.revoke(token)
        self.assertFalse(self.store().is_valid(token, "key"))

    def test_session_count_is_bounded_and_oldest_is_removed(self):
        store = self.store()
        tokens = []
        for index in range(5):
            self.now[0] += 1
            tokens.append(store.issue("key", remember=True))
        restarted = self.store()
        self.assertFalse(restarted.is_valid(tokens[0], "key"))
        self.assertTrue(restarted.is_valid(tokens[-1], "key"))
        with open(self.path, "r", encoding="utf-8") as handle:
            self.assertEqual(4, len(json.load(handle)["sessions"]))


class ConsoleLoginEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SessionStore(os.path.join(self.tmp.name, "sessions.json"))
        self.store_patch = patch.object(main, "_session_store", self.store)
        self.key_patch = patch.object(main.config, "API_KEY", "test-console-key")
        self.store_patch.start()
        self.key_patch.start()
        main._login_failures.clear()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        self.key_patch.stop()
        self.store_patch.stop()
        self.tmp.cleanup()

    def test_login_is_backward_compatible_and_ordinary_cookie_is_session_only(self):
        response = self.client.post("/api/login", json={"password": "test-console-key"})
        self.assertEqual(200, response.status_code)
        cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=lax", cookie)
        self.assertNotIn("Max-Age", cookie)
        self.assertNotIn("Expires=", cookie)

    def test_remember_cookie_has_30_day_lifetime_and_secure_proxy_attribute(self):
        response = self.client.post(
            "/api/login",
            json={"password": "test-console-key", "remember": True},
            headers={"x-forwarded-proto": "https"},
        )
        self.assertEqual(200, response.status_code)
        cookie = response.headers["set-cookie"]
        self.assertIn(f"Max-Age={main.REMEMBERED_SESSION_TTL_SECONDS}", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=lax", cookie)
        self.assertIn("Secure", cookie)

    def test_wrong_password_returns_401_without_cookie(self):
        response = self.client.post(
            "/api/login", json={"password": "wrong", "remember": True}
        )
        self.assertEqual(401, response.status_code)
        self.assertNotIn("set-cookie", response.headers)

    def test_logout_revokes_remembered_session(self):
        login = self.client.post(
            "/api/login",
            json={"password": "test-console-key", "remember": True},
        )
        token = login.cookies.get(main.AUTH_COOKIE)
        self.assertTrue(self.store.is_valid(token, "test-console-key"))
        logout = self.client.post("/api/logout")
        self.assertEqual(200, logout.status_code)
        self.assertFalse(self.store.is_valid(token, "test-console-key"))
        self.assertIn("Max-Age=0", logout.headers["set-cookie"])

    def test_client_config_requires_login_and_is_never_cached(self):
        self.assertEqual(self.client.get("/api/client-config").status_code, 401)
        self.client.post("/api/login", json={"password": "test-console-key"})
        response = self.client.get("/api/client-config")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"api_key": "test-console-key", "local_access": False})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.client.post("/api/logout")
        self.assertEqual(self.client.get("/api/client-config").status_code, 401)

    def test_direct_lan_console_is_password_free_but_api_still_needs_key(self):
        with patch.object(main.config, "CONSOLE_AUTH", "local"), TestClient(
                main.app, base_url="http://192.168.1.2", client=("192.168.1.8", 5678)) as client:
            self.assertIn('id="connection-card"', client.get("/").text)
            self.assertTrue(client.get("/api/client-config").json()["local_access"])
            self.assertIn(client.get("/v1/models").status_code, (401, 403))
            with patch.object(main.config, "CONSOLE_AUTH", "required"):
                self.assertEqual(client.get("/api/client-config").status_code, 401)


class LocalConsoleAccessTests(unittest.TestCase):
    def request(self, client="192.168.1.8", host="192.168.1.2", headers=None):
        return Request({"type": "http", "scheme": "http", "method": "GET", "path": "/", "root_path": "",
                        "query_string": b"", "client": (client, 5678), "server": (host, 80),
                        "headers": [(b"host", host.encode())] + [(k.encode(), v.encode()) for k, v in (headers or {}).items()]})

    def test_local_hosts_and_sources_are_both_required(self):
        for client, host in (("127.0.0.1", "localhost"), ("192.168.1.8", "192.168.1.2"), ("10.1.2.3", "10.2.3.4")):
            with self.subTest(client=client, host=host):
                self.assertTrue(is_local_console_request(self.request(client, host), "local"))
        for client, host in (("8.8.8.8", "192.168.1.2"), ("127.0.0.1", "proxy.example.com")):
            with self.subTest(client=client, host=host):
                self.assertFalse(is_local_console_request(self.request(client, host), "local"))

    def test_proxy_and_foreign_browser_origins_cannot_bypass_login(self):
        for headers in ({"cf-connecting-ip": "8.8.8.8"}, {"x-forwarded-for": "192.168.1.8"},
                        {"forwarded": "for=127.0.0.1"}, {"origin": "https://other.example"},
                        {"sec-fetch-site": "cross-site"}):
            with self.subTest(headers=headers):
                self.assertFalse(is_local_console_request(self.request(headers=headers), "local"))
        self.assertTrue(is_local_console_request(self.request(headers={"origin": "http://192.168.1.2"}), "local"))


if __name__ == "__main__":
    unittest.main()
