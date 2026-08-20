"""Lexical token coverage and hybrid RRF rerank."""
from __future__ import annotations

import unittest

from khipu.search_text import hybrid_rerank, search_tokens, token_hit_count


class SearchTokensTest(unittest.TestCase):
    def test_openbot_phrase_keeps_content_tokens(self) -> None:
        self.assertEqual(
            search_tokens("openbot ingest PR 36"),
            ["openbot", "ingest", "36"],
        )

    def test_stopwords_and_short_noise_drop(self) -> None:
        self.assertEqual(search_tokens("why did the e"), [])
        self.assertEqual(search_tokens("%"), [])

    def test_recap_query_keeps_the_load_bearing_words(self) -> None:
        toks = search_tokens(
            "why did the recap chip silently produce nothing in a Team session"
        )
        for need in ("recap", "chip", "silently", "produce", "nothing", "team", "session"):
            self.assertIn(need, toks)


class TokenHitCountTest(unittest.TestCase):
    def test_word_boundary_not_substring(self) -> None:
        self.assertEqual(token_hit_count("earth art", ["art"]), 1)
        self.assertEqual(token_hit_count("earth", ["art"]), 0)


class HybridRerankTest(unittest.TestCase):
    def test_recap_chip_query_lifts_the_lexical_hit(self) -> None:
        rows = [
            {
                "id": "9286",
                "score": 0.715,
                "label": "keyboard path",
                "snippet": "keyboard path recap chip silent fail",
            },
            {
                "id": "9313",
                "score": 0.686,
                "label": "PR 601 Team",
                "snippet": (
                    "recap chip silently produce nothing in a Team session "
                    "root cause"
                ),
            },
        ]
        out = hybrid_rerank(
            rows,
            "why did the recap chip silently produce nothing in a Team session",
            limit=2,
        )
        self.assertEqual(out[0]["id"], "9313")

    def test_empty_tokens_keep_input_order(self) -> None:
        rows = [{"id": "a", "snippet": "x"}, {"id": "b", "snippet": "y"}]
        out = hybrid_rerank(rows, "the a", limit=2)
        self.assertEqual([r["id"] for r in out], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
