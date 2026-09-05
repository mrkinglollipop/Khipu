"""Tests for the maintainer's graphify code_semantic_extractor.code_roots_from_resolved."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _scripts_dir() -> Path | None:
    raw = (os.environ.get("KHIPU_GRAPHIFY_SCRIPTS_DIR") or "").strip()
    if raw:
        return Path(raw)
    nightly = (os.environ.get("KHIPU_GRAPHIFY_NIGHTLY") or "").strip()
    if nightly:
        return Path(nightly).parent
    return None


_SCRIPTS = _scripts_dir()
EXTRACTOR_PATH = (_SCRIPTS / "code_semantic_extractor.py") if _SCRIPTS is not None else None


def _load_extractor():
    spec = importlib.util.spec_from_file_location(
        "code_semantic_extractor_under_test", EXTRACTOR_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {EXTRACTOR_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@unittest.skipUnless(
    EXTRACTOR_PATH is not None and EXTRACTOR_PATH.is_file(),
    "maintainer graphify scripts not configured",
)
class CodeRootsFromResolvedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_extractor()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._old = os.environ.get("KHIPU_GRAPH_SOURCES_RESOLVED")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("KHIPU_GRAPH_SOURCES_RESOLVED", None)
        else:
            os.environ["KHIPU_GRAPH_SOURCES_RESOLVED"] = self._old
        self.tmp.cleanup()

    def _write_resolved(self, payload) -> Path:
        path = self.dir / "graph_sources.resolved.json"
        if isinstance(payload, str):
            path.write_text(payload, encoding="utf-8")
        else:
            path.write_text(json.dumps(payload), encoding="utf-8")
        os.environ["KHIPU_GRAPH_SOURCES_RESOLVED"] = str(path)
        return path

    def test_missing_file_falls_back_to_workspace(self):
        os.environ["KHIPU_GRAPH_SOURCES_RESOLVED"] = str(
            self.dir / "does-not-exist.json"
        )
        self.assertEqual(
            self.mod.code_roots_from_resolved(), [self.mod.WORKSPACE]
        )

    def test_code_semantic_false_returns_empty(self):
        self._write_resolved(
            {
                "schema_version": 1,
                "collectors": {"code_semantic": False},
                "code_roots": [str(self.dir)],
            }
        )
        self.assertEqual(self.mod.code_roots_from_resolved(), [])

    def test_corrupt_present_raises_runtime_error(self):
        self._write_resolved("{not json")
        with self.assertRaises(RuntimeError):
            self.mod.code_roots_from_resolved()

    def test_non_object_present_raises_runtime_error(self):
        self._write_resolved([1, 2])
        with self.assertRaises(RuntimeError):
            self.mod.code_roots_from_resolved()

    def test_invalid_only_roots_returns_empty(self):
        self._write_resolved(
            {
                "schema_version": 1,
                "collectors": {"code_semantic": True},
                "code_roots": ["/nope/khipu-invalid-only-root"],
            }
        )
        self.assertEqual(self.mod.code_roots_from_resolved(), [])

    def test_empty_code_roots_falls_back_to_workspace(self):
        self._write_resolved(
            {
                "schema_version": 1,
                "collectors": {"code_semantic": True},
                "code_roots": [],
            }
        )
        self.assertEqual(
            self.mod.code_roots_from_resolved(), [self.mod.WORKSPACE]
        )

    def test_main_skips_write_when_no_roots(self):
        self._write_resolved({"collectors": {"code_semantic": False}})
        out_path = mock.Mock()
        api_key = mock.Mock()
        api_key.read_text.side_effect = AssertionError(
            "API key must not be read when extract is skipped"
        )
        cache_dir = mock.Mock()
        with mock.patch.object(self.mod, "OUT_PATH", out_path):
            with mock.patch.object(self.mod, "API_KEY_FILE", api_key):
                with mock.patch.object(self.mod, "CACHE_DIR", cache_dir):
                    with mock.patch.object(
                        sys, "argv", ["code_semantic_extractor.py"]
                    ):
                        self.mod.main()
        out_path.write_text.assert_not_called()
        cache_dir.mkdir.assert_not_called()


if __name__ == "__main__":
    unittest.main()
