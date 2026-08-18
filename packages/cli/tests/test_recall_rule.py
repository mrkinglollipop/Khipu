"""khipu.recall_rule is the prompt-time text that tells a model Khipu exists.

It ships in two shapes from one source (a Claude Code SessionStart hook and a
Cursor .mdc rule); these tests exist to keep the two from drifting apart and to
keep the rule honest about what it promises, since nothing else checks it.
"""

from __future__ import annotations

import unittest

from khipu import recall_rule as rr


class RuleTextTest(unittest.TestCase):
    def test_it_names_every_tool_the_mcp_server_exposes(self):
        for tool in ("khipu_search", "khipu_graph", "khipu_status", "khipu_capture"):
            with self.subTest(tool=tool):
                self.assertIn(tool, rr.RULE_MD)

    def test_it_states_the_cadence_rather_than_demanding_recall_every_turn(self):
        self.assertIn("on demand", rr.RULE_MD)

    def test_it_warns_that_capture_declines_where_a_hook_already_runs(self):
        """Without this a model reads a declined khipu_capture as a failure and
        retries it every turn."""
        self.assertIn("declines", rr.RULE_MD)
        self.assertIn("dual", rr.RULE_MD)

    def test_it_explains_the_semantic_flag_both_ways(self):
        self.assertIn("semantic", rr.RULE_MD)


class ClaudeShapeTest(unittest.TestCase):
    def test_the_hook_context_is_the_rule_with_no_frontmatter(self):
        out = rr.claude_additional_context()
        self.assertFalse(out.startswith("---"))
        self.assertTrue(out.startswith("# Khipu memory"))

    def test_it_is_stripped_so_the_harness_does_not_pad_the_prompt(self):
        out = rr.claude_additional_context()
        self.assertEqual(out, out.strip())


class CursorShapeTest(unittest.TestCase):
    def test_the_mdc_opens_with_frontmatter_cursor_can_parse(self):
        lines = rr.cursor_mdc().splitlines()
        self.assertEqual(lines[0], "---")
        self.assertIn("alwaysApply: true", lines[:5])

    def test_the_frontmatter_closes_before_the_body(self):
        body = rr.cursor_mdc().split("---", 2)[2]
        self.assertIn("# Khipu memory", body)

    def test_both_shapes_carry_the_identical_rule_body(self):
        """One source of truth: a fix to the rule must reach Cursor and Claude
        Code without either being edited separately."""
        self.assertIn(rr.RULE_MD.strip(), rr.cursor_mdc())
        self.assertIn(rr.claude_additional_context(), rr.cursor_mdc())


class AegisTest(unittest.TestCase):
    def test_the_module_documents_why_aegis_gets_no_rule(self):
        """Aegis's SessionStart is an Observe gate that discards stdout, so the
        absence of a third shape is a verified fact and not an oversight."""
        self.assertIn("Aegis", rr.__doc__)
        self.assertIn("Observe", rr.__doc__)


if __name__ == "__main__":
    unittest.main()
