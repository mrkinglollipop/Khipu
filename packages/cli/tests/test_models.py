"""khipu.models — per-role Settings schema + show/set semantics."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import models


class ModelsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._env = mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(self.dir)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    def test_defaults_when_key_missing(self):
        doc = models.show_models()
        self.assertEqual(doc["synth"]["provider"], "cloud")
        self.assertEqual(doc["synth"]["model_id"], "gemini-2.5-flash")
        self.assertEqual(doc["embed"]["model_id"], "")
        self.assertEqual(doc["vision"]["provider"], "off")
        self.assertIsNone(doc["models_error"])

    def test_loopback_http_ok_non_loopback_http_rejected(self):
        models.validate_endpoint("http://127.0.0.1:11434")
        models.validate_endpoint("http://localhost:8080/v1")
        with self.assertRaises(ValueError):
            models.validate_endpoint("http://example.com")

    def test_chat_completions_url_does_not_double_v1(self):
        self.assertEqual(
            models.chat_completions_url("http://127.0.0.1:11434"),
            "http://127.0.0.1:11434/v1/chat/completions",
        )
        self.assertEqual(
            models.chat_completions_url("http://127.0.0.1:11434/v1"),
            "http://127.0.0.1:11434/v1/chat/completions",
        )

    def test_round_trip_replace_and_show(self):
        payload = {
            "synth": {
                "provider": "local",
                "endpoint": "http://127.0.0.1:11434",
                "model_id": "llama3",
            },
            "embed": {"provider": "cloud", "endpoint": "", "model_id": ""},
            "vision": {"provider": "off", "endpoint": "", "model_id": ""},
        }
        out = models.set_models_replace(payload)
        self.assertEqual(out["synth"]["provider"], "local")
        self.assertEqual(out["synth"]["model_id"], "llama3")
        again = models.show_models()
        self.assertEqual(again["synth"]["model_id"], "llama3")
        self.assertIsNone(again["models_error"])

    def test_flag_merge_leaves_other_roles(self):
        models.set_models_replace(
            {
                "synth": {
                    "provider": "cloud",
                    "endpoint": "",
                    "model_id": "gemini-2.5-flash",
                },
                "embed": {"provider": "cloud", "endpoint": "", "model_id": "keep-me"},
                "vision": {"provider": "off", "endpoint": "", "model_id": ""},
            }
        )
        out = models.set_models_merge_role(
            "synth",
            provider="local",
            endpoint="http://127.0.0.1:11434",
            model_id="mistral",
        )
        self.assertEqual(out["synth"]["model_id"], "mistral")
        self.assertEqual(out["embed"]["model_id"], "keep-me")

    def test_json_replace_requires_all_three_roles(self):
        with self.assertRaises(ValueError):
            models.set_models_replace(
                {
                    "synth": {
                        "provider": "cloud",
                        "endpoint": "",
                        "model_id": "gemini-2.5-flash",
                    }
                }
            )
        self.assertFalse((self.dir / "config.json").exists())

    def test_corrupt_models_key_show_defaults_with_error_set_refuses(self):
        from khipu import config

        config.save_config({"models": "not-an-object"})
        doc = models.show_models()
        self.assertEqual(doc["synth"]["provider"], "cloud")
        self.assertIsNotNone(doc["models_error"])
        with self.assertRaises(ValueError):
            models.set_models_replace({"synth": "nope"})
        with self.assertRaises(ValueError):
            models.set_models_merge_role(
                "synth",
                provider="cloud",
                endpoint="",
                model_id="gemini-2.5-flash",
            )
        # Corrupt key still on disk — set did not write
        stored = json.loads((self.dir / "config.json").read_text())
        self.assertEqual(stored["models"], "not-an-object")

    def test_corrupt_store_refuses_valid_three_role_replace(self):
        from khipu import config

        config.save_config({"models": "not-an-object"})
        payload = {
            "synth": {
                "provider": "cloud",
                "endpoint": "",
                "model_id": "gemini-2.5-flash",
            },
            "embed": {"provider": "cloud", "endpoint": "", "model_id": ""},
            "vision": {"provider": "off", "endpoint": "", "model_id": ""},
        }
        with self.assertRaises(ValueError) as ctx:
            models.set_models_replace(payload)
        self.assertIn("stored models key is invalid", str(ctx.exception))
        stored = json.loads((self.dir / "config.json").read_text())
        self.assertEqual(stored["models"], "not-an-object")

    def test_vision_off_does_not_require_endpoint_or_model_id(self):
        out = models.set_models_replace(
            {
                "synth": {
                    "provider": "cloud",
                    "endpoint": "",
                    "model_id": "gemini-2.5-flash",
                },
                "embed": {"provider": "cloud", "endpoint": "", "model_id": ""},
                "vision": {"provider": "off", "endpoint": "", "model_id": ""},
            }
        )
        self.assertEqual(out["vision"]["provider"], "off")

    def test_khipu_synth_provider_env_overrides_when_set(self):
        models.set_models_merge_role(
            "synth",
            provider="local",
            endpoint="http://127.0.0.1:11434",
            model_id="x",
        )
        with mock.patch.dict(os.environ, {"KHIPU_SYNTH_PROVIDER": "cloud"}):
            self.assertEqual(models.synth_settings()["provider"], "local")
        with mock.patch.dict(
            os.environ, {"KHIPU_TEST": "1", "KHIPU_SYNTH_PROVIDER": "cloud"}
        ):
            self.assertEqual(models.synth_settings()["provider"], "cloud")
        self.assertEqual(models.synth_settings()["provider"], "local")

    def test_apply_welcome_cloud_activates_embed(self):
        with mock.patch(
            "khipu.embed.activate_welcome_embed",
            return_value={"ok": True, "active_profile": "gemini-embedding-2@768"},
        ) as act:
            out = models.apply_welcome_models(
                synth_choice="cloud", embed_choice="cloud"
            )
        self.assertTrue(out["ok"])
        self.assertEqual(out["models"]["synth"]["model_id"], "gemini-2.5-flash")
        self.assertEqual(out["models"]["embed"]["model_id"], "gemini-embedding-2")
        self.assertEqual(out["embed"]["active_profile"], "gemini-embedding-2@768")
        act.assert_called_once_with(provider="cloud")

    def test_apply_welcome_skip_does_not_activate(self):
        with mock.patch("khipu.embed.activate_welcome_embed") as act:
            out = models.apply_welcome_models(
                synth_choice="skip", embed_choice="skip"
            )
        self.assertTrue(out["ok"])
        self.assertEqual(out["models"]["embed"]["model_id"], "")
        self.assertTrue(out["embed"].get("skipped"))
        act.assert_not_called()


if __name__ == "__main__":
    unittest.main()
