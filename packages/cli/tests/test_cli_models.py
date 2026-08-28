"""CLI-level tests for `khipu models` (show / set replace / set --role)."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import cli, config


_VALID = {
    "synth": {
        "provider": "local",
        "endpoint": "http://127.0.0.1:11434",
        "model_id": "llama3",
    },
    "embed": {"provider": "cloud", "endpoint": "", "model_id": "keep-me"},
    "vision": {"provider": "off", "endpoint": "", "model_id": ""},
}


def _run(argv: list[str]) -> tuple[int, str]:
    parser = cli.build_parser()
    args = parser.parse_args(argv)
    with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
        rc = args.func(args)
    return rc, out.getvalue()


class ModelsCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._env = mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(self.dir)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    def test_show_defaults(self):
        rc, raw = _run(["models"])
        self.assertEqual(rc, 0)
        doc = json.loads(raw)
        self.assertEqual(doc["synth"]["provider"], "cloud")
        self.assertEqual(doc["synth"]["model_id"], "gemini-2.5-flash")
        self.assertEqual(doc["vision"]["provider"], "off")
        self.assertIsNone(doc["models_error"])

    def test_set_json_replace(self):
        rc, raw = _run(["models", "set", json.dumps(_VALID)])
        self.assertEqual(rc, 0)
        payload = json.loads(raw)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["models"]["synth"]["model_id"], "llama3")
        stored = json.loads((self.dir / "config.json").read_text())
        self.assertEqual(stored["models"]["synth"]["model_id"], "llama3")

    def test_set_role_merge(self):
        rc, _ = _run(["models", "set", json.dumps(_VALID)])
        self.assertEqual(rc, 0)
        rc, raw = _run([
            "models",
            "set",
            "--role",
            "synth",
            "--provider",
            "local",
            "--endpoint",
            "http://127.0.0.1:11434",
            "--model-id",
            "mistral",
        ])
        self.assertEqual(rc, 0)
        payload = json.loads(raw)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["models"]["synth"]["model_id"], "mistral")
        self.assertEqual(payload["models"]["embed"]["model_id"], "keep-me")

    def test_invalid_json_nonzero(self):
        rc, raw = _run(["models", "set", "{not-json"])
        self.assertEqual(rc, 2)
        payload = json.loads(raw)
        self.assertFalse(payload["ok"])
        self.assertIn("invalid JSON", payload["error"])
        self.assertFalse((self.dir / "config.json").exists())

    def test_corrupt_merge_refuses_no_write(self):
        config.save_config({"models": "not-an-object"})
        rc, raw = _run([
            "models",
            "set",
            "--role",
            "synth",
            "--provider",
            "cloud",
        ])
        self.assertEqual(rc, 2)
        payload = json.loads(raw)
        self.assertFalse(payload["ok"])
        stored = json.loads((self.dir / "config.json").read_text())
        self.assertEqual(stored["models"], "not-an-object")

    def test_welcome_cloud_activates_embed(self):
        payload = json.dumps(
            {"synth_choice": "cloud", "embed_choice": "cloud"}
        )
        with mock.patch(
            "khipu.embed.activate_welcome_embed",
            return_value={"ok": True, "active_profile": "gemini-embedding-2@768"},
        ) as act:
            rc, raw = _run(["models", "welcome", payload])
        self.assertEqual(rc, 0)
        body = json.loads(raw)
        self.assertTrue(body["ok"])
        self.assertEqual(body["models"]["embed"]["model_id"], "gemini-embedding-2")
        act.assert_called_once_with(provider="cloud")

    def test_welcome_skip_does_not_activate(self):
        payload = json.dumps(
            {"synth_choice": "skip", "embed_choice": "skip"}
        )
        with mock.patch("khipu.embed.activate_welcome_embed") as act:
            rc, raw = _run(["models", "welcome", payload])
        self.assertEqual(rc, 0)
        act.assert_not_called()

    def test_welcome_activate_error_is_nonzero(self):
        payload = json.dumps(
            {"synth_choice": "cloud", "embed_choice": "cloud"}
        )
        with mock.patch(
            "khipu.embed.activate_welcome_embed",
            side_effect=RuntimeError("hub profile missing"),
        ):
            rc, raw = _run(["models", "welcome", payload])
        self.assertEqual(rc, 2)
        body = json.loads(raw)
        self.assertFalse(body["ok"])
        self.assertIn("hub profile missing", body["error"])

    def test_welcome_embed_ok_false_does_not_keep_outer_ok(self):
        payload = json.dumps(
            {"synth_choice": "cloud", "embed_choice": "cloud"}
        )
        with mock.patch(
            "khipu.embed.activate_welcome_embed",
            return_value={"ok": False, "error": "vectors missing"},
        ):
            rc, raw = _run(["models", "welcome", payload])
        self.assertEqual(rc, 2)
        body = json.loads(raw)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "vectors missing")


if __name__ == "__main__":
    unittest.main()
