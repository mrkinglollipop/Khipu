"""Compile-check the maintainer's graphify scripts so an unparseable extractor cannot ship."""
from __future__ import annotations

import os
import py_compile
import unittest
from pathlib import Path


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
            py_compile.compile(str(path), doraise=True)
            compiled += 1
        if compiled == 0:
            self.skipTest("graphify scripts not on disk")


if __name__ == "__main__":
    unittest.main()
