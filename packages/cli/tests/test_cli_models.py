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


if __name__ == "__main__":
    unittest.main()
