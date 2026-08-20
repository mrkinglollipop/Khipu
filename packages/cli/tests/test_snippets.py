"""Word-boundary clip for search/status teasers."""
from __future__ import annotations

import unittest

from khipu.snippets import ELLIPSIS, SNIPPET_LIMIT, clip_snippet


class ClipSnippetTest(unittest.TestCase):
    def test_short_text_is_unchanged(self):
        self.assertEqual(clip_snippet("hello world", 80), "hello world")

    def test_empty_and_none(self):
        self.assertEqual(clip_snippet("", 80), "")
        self.assertEqual(clip_snippet(None, 80), "")
        self.assertEqual(clip_snippet("x", 0), "")

    def test_does_not_cut_mid_word_the_way_sql_left_did(self):
        # khipu_status used left(summary, 280) and turned "included" into "include".
        prefix = (
            "The user requested to scope a project, which resulted in a scope "
            "document being written and submitted as a PR. After discussing the "
            "value of the project, the user decided to table it for now, making "
            "the scope document durable in the repository and memory. The session "
            "also included a discussion about the effectiveness of Khipu for "
            "memory retrieval."
        )
        self.assertGreater(len(prefix), 280)
        sql_left = prefix[:280]
        self.assertTrue(sql_left.endswith("include"), sql_left[-20:])
        clipped = clip_snippet(prefix, 280)
        self.assertTrue(clipped.endswith(ELLIPSIS))
        # Must not reproduce SQL left(280) mid-word "include".
        self.assertFalse(clipped.rstrip(ELLIPSIS).endswith("include"))
        self.assertEqual(clip_snippet(prefix, SNIPPET_LIMIT), prefix)

    def test_ellipsis_only_when_truncated(self):
        self.assertNotIn(ELLIPSIS, clip_snippet("abc", 10))
        self.assertTrue(clip_snippet("one two three four five", 12).endswith(ELLIPSIS))

    def test_unicode_nbsp_is_a_word_boundary(self):
        clipped = clip_snippet("alpha\u00a0beta gamma", 10)
        self.assertTrue(clipped.endswith(ELLIPSIS))
        self.assertFalse(clipped.rstrip(ELLIPSIS).endswith("bet"))
        self.assertEqual(clipped.rstrip(ELLIPSIS), "alpha")


if __name__ == "__main__":
    unittest.main()
