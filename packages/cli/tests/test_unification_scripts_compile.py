"""Compile-check UNIFICATION graphify scripts so an unparseable extractor cannot ship."""
from __future__ import annotations

import py_compile
import unittest
from pathlib import Path

SCRIPTS = Path("/Volumes/Cloud Storage/Claude/UNIFICATION/scripts")
NAMES = (
    "graphify_nightly.py",
    "build_graph.py",
    "code_ast_extractor.py",
    "code_semantic_extractor.py",
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
            self.skipTest("UNIFICATION scripts not on disk")


if __name__ == "__main__":
    unittest.main()
