"""khipu.config holds capture_mode — the SSOT for *who writes* — and the gateway
URL, which carries a bearer token and must therefore be https. Untested until the
2026-08-17 audit.

Runs entirely under a temp KHIPU_DATA_DIR.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import config


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._env = mock.patch.dict(
            os.environ,
            {"KHIPU_DATA_DIR": str(self.dir), "KHIPU_CAPTURE_MODE": "", "KHIPU_GATEWAY_URL": ""},
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    # --- capture_mode ------------------------------------------------------------

    def test_default_is_dual_when_nothing_is_set(self):
        self.assertEqual(config.capture_mode(), "dual")

    def test_stored_mode_is_read_back(self):
        config.set_capture_mode("hub")
        self.assertEqual(config.capture_mode(), "hub")
        self.assertEqual(json.loads(config.config_file().read_text())["capture_mode"], "hub")

    def test_env_overrides_the_stored_mode(self):
        config.set_capture_mode("legacy")
        with mock.patch.dict(os.environ, {"KHIPU_CAPTURE_MODE": "hub"}):
            self.assertEqual(config.capture_mode(), "hub")

    def test_an_unknown_env_value_is_ignored_not_obeyed(self):
        config.set_capture_mode("hub")
        with mock.patch.dict(os.environ, {"KHIPU_CAPTURE_MODE": "banana"}):
            self.assertEqual(config.capture_mode(), "hub")

    def test_an_unknown_stored_value_falls_back_to_the_default(self):
        config.save_config({"capture_mode": "banana"})
        self.assertEqual(config.capture_mode(), "dual")

    def test_setting_an_invalid_mode_raises_and_writes_nothing(self):
        with self.assertRaises(ValueError):
            config.set_capture_mode("banana")
        self.assertFalse(config.config_file().exists())

    def test_mode_is_case_and_space_insensitive(self):
        config.set_capture_mode("  HUB  ")
        self.assertEqual(config.capture_mode(), "hub")

    # --- gateway_url -------------------------------------------------------------

    def test_gateway_url_defaults_empty_and_round_trips(self):
        self.assertEqual(config.gateway_url(), "")
        config.set_gateway_url("https://khipu.example.test/")
        self.assertEqual(config.gateway_url(), "https://khipu.example.test")

    def test_gateway_url_must_be_https_because_it_carries_a_bearer_token(self):
        with self.assertRaises(ValueError):
            config.set_gateway_url("http://khipu.example.test")

    def test_empty_gateway_url_clears_the_key(self):
        config.set_gateway_url("https://khipu.example.test")
        config.set_gateway_url("")
        self.assertNotIn("gateway_url", json.loads(config.config_file().read_text()))

    def test_env_gateway_url_wins(self):
        config.set_gateway_url("https://stored.example.test")
        with mock.patch.dict(os.environ, {"KHIPU_GATEWAY_URL": "https://env.example.test/"}):
            self.assertEqual(config.gateway_url(), "https://env.example.test")

    # --- file handling -----------------------------------------------------------

    def test_corrupt_config_reads_as_empty_rather_than_raising(self):
        config.config_file().parent.mkdir(parents=True, exist_ok=True)
        config.config_file().write_text("{not json")
        self.assertEqual(config.load_config(), {})
        self.assertEqual(config.capture_mode(), "dual")

    def test_non_dict_config_reads_as_empty(self):
        config.config_file().parent.mkdir(parents=True, exist_ok=True)
        config.config_file().write_text('["a", "b"]')
        self.assertEqual(config.load_config(), {})

    def test_save_is_atomic_and_leaves_no_temp_file(self):
        config.set_capture_mode("hub")
        self.assertEqual([p.name for p in self.dir.glob("*.tmp")], [])
        self.assertTrue(config.config_file().is_file())

    def test_setting_one_key_preserves_the_other(self):
        config.set_gateway_url("https://khipu.example.test")
        config.set_capture_mode("hub")
        data = json.loads(config.config_file().read_text())
        self.assertEqual(data["gateway_url"], "https://khipu.example.test")
        self.assertEqual(data["capture_mode"], "hub")


class AegisCompatShimTest(unittest.TestCase):
    """The shim used to copy module values at import time, so a patched constant
    was invisible through the old name (audit 2026-08-17)."""

    def test_the_shim_resolves_against_the_live_module(self):
        from khipu import aegis_capture, session_capture

        self.assertIs(aegis_capture.drain, session_capture.drain)
        self.assertIs(aegis_capture._mint_ts, session_capture._mint_ts)
        with mock.patch.object(session_capture, "MIN_TURNS", 99):
            self.assertEqual(aegis_capture.MIN_TURNS, 99)

    def test_unknown_attributes_still_raise_attribute_error(self):
        from khipu import aegis_capture

        with self.assertRaises(AttributeError):
            aegis_capture.definitely_not_a_real_name


if __name__ == "__main__":
    unittest.main()
