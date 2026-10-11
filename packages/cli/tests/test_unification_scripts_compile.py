"""Compile-check the maintainer's graphify scripts so an unparseable extractor cannot ship."""
from __future__ import annotations

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


SCRIPTS = _scripts_dir()
NAMES = (
    "graphify_nightly.py",
    "build_graph.py",
    "code_ast_extractor.py",
    "code_semantic_extractor.py",
)


@unittest.skipUnless(
    SCRIPTS is not None and SCRIPTS.is_dir(), "maintainer graphify scripts not configured"
)
class UnificationScriptsCompileTest(unittest.TestCase):
    def test_session_scripts_compile(self):
        compiled = 0
        for name in NAMES:
            path = SCRIPTS / name
            if not path.is_file():
                continue
            compile(path.read_bytes(), str(path), "exec")
            compiled += 1
        if compiled == 0:
            self.skipTest("graphify scripts not on disk")


if __name__ == "__main__":
    unittest.main()


class CompileIsolationTest(unittest.TestCase):
    def test_compile_creates_no_source_adjacent_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in NAMES:
                (root / name).write_text("value = 1\n")
            with mock.patch(__name__ + ".SCRIPTS", root), mock.patch.object(sys, "pycache_prefix", None):
                UnificationScriptsCompileTest().test_session_scripts_compile()
            self.assertEqual({p.name for p in root.iterdir()}, set(NAMES))
