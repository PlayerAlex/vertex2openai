import json
import os
import tempfile
import unittest
from unittest.mock import patch

import auth
import runtime_state


class ClientApiKeyRotationTests(unittest.TestCase):
    def test_rotated_key_survives_restart_and_replaces_old_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "web_state.json")
            with patch.object(runtime_state, "STATE_FILE", state_file), patch.object(
                runtime_state.app_config, "API_KEY", "legacy-key"
            ):
                state = runtime_state.AppState()
                new_key = state.rotate_client_api_key()

                self.assertTrue(new_key.startswith("ksv-"))
                self.assertNotEqual("ksv-legacy-key", new_key)
                self.assertEqual(new_key, runtime_state.AppState().get_client_api_key())
                with open(state_file, "r", encoding="utf-8") as handle:
                    self.assertEqual(new_key, json.load(handle)["client_api_key"])

    def test_auth_reads_current_key_for_every_request(self):
        current = {"key": "ksv-old"}
        with patch.object(
            auth.app_state, "get_client_api_key", side_effect=lambda: current["key"]
        ):
            self.assertTrue(auth.validate_api_key("ksv-old"))
            current["key"] = "ksv-new"
            self.assertFalse(auth.validate_api_key("ksv-old"))
            self.assertTrue(auth.validate_api_key("ksv-new"))

    def test_failed_write_does_not_switch_in_memory_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "web_state.json")
            with patch.object(runtime_state, "STATE_FILE", state_file), patch.object(
                runtime_state.app_config, "API_KEY", "legacy-key"
            ):
                state = runtime_state.AppState()
                first_key = state.rotate_client_api_key()
                with patch.object(runtime_state.os, "replace", side_effect=OSError("disk full")), patch(
                    "builtins.print"
                ):
                    with self.assertRaises(OSError):
                        state.rotate_client_api_key()

                self.assertEqual(first_key, state.get_client_api_key())
                self.assertEqual(first_key, runtime_state.AppState().get_client_api_key())


if __name__ == "__main__":
    unittest.main()
