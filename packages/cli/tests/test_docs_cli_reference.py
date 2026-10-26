"""Docs / desktop-hint leads: each test fails when the docs disagree with the code."""
from __future__ import annotations

import argparse
import re
import unittest
from pathlib import Path

from khipu import cli, embed

REPO = Path(__file__).resolve().parents[3]
CLI_MD = (REPO / "docs" / "cli.md").read_text(encoding="utf-8")


def _top_level() -> list[str]:
    parser = cli.build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return sorted(action.choices)
    raise AssertionError("no subparsers")


class CliReferenceTest(unittest.TestCase):
    def test_every_top_level_command_is_in_the_reference(self):
        missing = [c for c in _top_level() if not re.search(rf"`{re.escape(c)}[`\s]", CLI_MD)]
        self.assertEqual(missing, [])

    def test_command_count_matches_the_parser(self):
        claimed = int(re.search(r"There are (\d+) top-level commands", CLI_MD).group(1))
        self.assertEqual(claimed, len(_top_level()))

    def test_kind_filter_line_states_the_per_mode_split(self):
        line = next(l for l in CLI_MD.splitlines() if l.startswith("- Filters:"))
        self.assertNotEqual(set(embed._SEMANTIC_KINDS), set(embed._LITERAL_KINDS))
        self.assertTrue(re.search(r"semantic|literal|hybrid", line), line)

    def test_regen_memory_row_names_the_file_it_writes(self):
        row = next(l for l in CLI_MD.splitlines() if l.startswith("| `regen-memory`"))
        self.assertIn("MEMORY.from-khipu.md", row)


class DesktopHintTest(unittest.TestCase):
    def test_by_meaning_hint_does_not_say_vector_only(self):
        src = (REPO / "apps" / "desktop" / "src" / "App.tsx").read_text(encoding="utf-8")
        self.assertNotIn("Vector similarity only", src)
