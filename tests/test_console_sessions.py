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

    def test_login_accepts_legacy_key_and_ordinary_cookie_is_session_only(self):
        response = self.client.post("/api/login", json={"password": "test-console-key"})
        self.assertEqual(200, response.status_code)
        cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=lax", cookie)
        self.assertNotIn("Max-Age", cookie)
        self.assertNotIn("Expires=", cookie)

        self.client.post("/api/logout")
        response = self.client.post("/api/login", json={"password": "ksv-test-console-key"})
        self.assertEqual(200, response.status_code)

    def test_non_ascii_wrong_password_is_rejected_without_server_error(self):
        response = self.client.post("/api/login", json={"password": "错误密钥"})
        self.assertEqual(401, response.status_code)

    def test_prefixed_config_rejects_key_without_prefix(self):
        with patch.object(main.config, "API_KEY", "ksv-strict-key"):
            rejected = self.client.post("/api/login", json={"password": "strict-key"})
            accepted = self.client.post("/api/login", json={"password": "ksv-strict-key"})
        self.assertEqual(401, rejected.status_code)
        self.assertEqual(200, accepted.status_code)

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
        self.assertEqual(response.json(), {"api_key": "ksv-test-console-key", "local_access": False})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.client.post("/api/logout")
        self.assertEqual(self.client.get("/api/client-config").status_code, 401)

    def test_client_config_does_not_double_prefix_canonical_key(self):
        with patch.object(main.config, "API_KEY", "ksv-ready"):
            self.client.post("/api/login", json={"password": "ksv-ready"})
            response = self.client.get("/api/client-config")
        self.assertEqual(200, response.status_code)
        self.assertEqual("ksv-ready", response.json()["api_key"])
        self.assertEqual("no-store", response.headers["cache-control"])

    def test_direct_lan_console_is_password_free_but_api_still_needs_key(self):
        with patch.object(main.config, "CONSOLE_AUTH", "local"), TestClient(
                main.app, base_url="http://192.168.1.2", client=("192.168.1.8", 5678)) as client:
            self.assertIn('id="connection-card"', client.get("/").text)
            self.assertTrue(client.get("/api/client-config").json()["local_access"])
            self.assertIn(client.get("/v1/models").status_code, (401, 403))
            with patch.object(main.config, "CONSOLE_AUTH", "required"):
                self.assertEqual(client.get("/api/client-config").status_code, 401)

    def test_local_browser_link_and_reload_open_console_without_unlocking_cross_site_apis(self):
        navigation = {"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}
        with patch.object(main.config, "CONSOLE_AUTH", "local"), TestClient(
                main.app, base_url="http://127.0.0.1:8050", client=("127.0.0.1", 5678)) as client:
            for _ in range(2):
                response = client.get("/", headers=navigation)
                self.assertIn('id="connection-card"', response.text)
                self.assertNotIn("set-cookie", response.headers)
            # Loading HTML does not grant a session or waive API request checks.
            for path in ("/api/stats", "/api/client-config", "/api/settings"):
                self.assertEqual(client.get(path, headers=navigation).status_code, 401)
            self.assertEqual(client.post("/api/settings", json={}, headers=navigation).status_code, 401)
            self.assertEqual(client.get("/api/stats", headers={"sec-fetch-site": "same-origin"}).status_code, 200)
            self.assertIn(client.get("/v1/models").status_code, (401, 403))
            for extra in ({"sec-fetch-dest": "iframe"}, {"sec-fetch-mode": "cors"},
                          {"x-forwarded-for": "127.0.0.1"}):
                self.assertIn('id="f"', client.get("/", headers={**navigation, **extra}).text)
            with patch.object(main.config, "CONSOLE_AUTH", "required"):
                self.assertIn('id="f"', client.get("/", headers=navigation).text)
        with patch.object(main.config, "CONSOLE_AUTH", "local"), TestClient(
                main.app, base_url="http://127.0.0.1:8050", client=("8.8.8.8", 5678)) as client:
            self.assertIn('id="f"', client.get("/", headers=navigation).text)
        with patch.object(main.config, "CONSOLE_AUTH", "local"), TestClient(
                main.app, base_url="http://public.example", client=("127.0.0.1", 5678)) as client:
            self.assertIn('id="f"', client.get("/", headers=navigation).text)

    def test_console_access_diagnostic_is_uncached_and_contains_no_credentials(self):
        with patch.object(main.config, "CONSOLE_AUTH", "local"), TestClient(
                main.app, base_url="http://127.0.0.1:8050", client=("127.0.0.1", 5678)) as client:
            response = client.get("/api/console-access")
            self.assertEqual(response.json(), {"authenticated": True, "local_access": True, "reason": "local"})
            self.assertEqual(response.headers["cache-control"], "no-store")
            for headers, reason in (({"x-forwarded-for": "secret-header-value"}, "proxy_headers"),
                                    ({"sec-fetch-site": "cross-site"}, "cross_site"),
                                    ({"origin": "https://other.example"}, "foreign_origin")):
                response = client.get("/api/console-access", headers=headers)
                self.assertEqual(response.json(), {"authenticated": False, "local_access": False, "reason": reason})
                self.assertNotIn("test-console-key", response.text)
                self.assertNotIn("secret-header-value", response.text)
                self.assertNotIn("127.0.0.1", response.text)

    def test_old_login_page_can_continue_without_a_password_on_local_access(self):
        with patch.object(main.config, "CONSOLE_AUTH", "local"), TestClient(
                main.app, base_url="http://127.0.0.1:8050", client=("127.0.0.1", 5678)) as client:
            response = client.post("/api/login", json={"password": ""})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("set-cookie", response.headers)
            response = client.post("/api/login", json={"password": "wrong"}, headers={"sec-fetch-site": "cross-site"})
            self.assertEqual(response.status_code, 401)


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
