import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
import runtime_state


class ExpressKeyAutosaveTests(unittest.TestCase):
    def test_failed_save_preserves_previous_keys_in_memory_and_on_disk(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            runtime_state, "STATE_FILE", os.path.join(directory, "web_state.json")
        ), patch("builtins.print"):
            state = runtime_state.AppState()
            client_key = state.rotate_client_api_key()
            old_keys = state.set_express_keys([{"key": "test-google-key", "enabled": True}])
            with patch.object(runtime_state.os, "replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    state.set_express_keys([])
            self.assertEqual(old_keys, state.get_express_keys())
            self.assertEqual(old_keys, runtime_state.AppState().get_express_keys())
            state.set_express_keys([])
            restarted = runtime_state.AppState()
            self.assertEqual([], restarted.get_express_keys())
            self.assertEqual(client_key, restarted.get_client_api_key())

    def test_endpoint_reports_storage_failure_without_refreshing_active_keys(self):
        client = TestClient(main.app, raise_server_exceptions=False)
        try:
            with patch.object(main, "_is_authed", return_value=True), patch.object(
                main.app_state, "set_express_keys", side_effect=OSError("private details")
            ), patch.object(main.express_key_manager, "refresh_keys") as refresh:
                response = client.post("/api/express-keys", json={"keys": []})
                self.assertEqual(500, response.status_code)
                self.assertNotIn("private details", response.text)
                refresh.assert_not_called()
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
