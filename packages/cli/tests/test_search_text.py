"""Lexical token coverage and hybrid RRF rerank."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from khipu.search_text import (
    fuse_ranked_lists,
    hybrid_rerank,
    parse_time_filter,
    search_tokens,
    token_hit_count,
)


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

    def test_rank_text_lifts_extract_hit_when_snippet_misses(self) -> None:
        rows = [
            {
                "id": "high-cosine",
                "score": 0.90,
                "snippet": "keyboard path fail silently",
                "rank_text": "keyboard path fail silently",
            },
            {
                "id": "tagged",
                "score": 0.68,
                "snippet": "PR 601 Team auto routing",
                "rank_text": "PR 601 Team auto routing topics: recap-chip; team",
            },
        ]
        out = hybrid_rerank(
            rows,
            "why did the recap chip silently produce nothing in a Team session",
            limit=2,
        )
        self.assertEqual(out[0]["id"], "tagged")

    def test_empty_tokens_keep_input_order(self) -> None:
        rows = [{"id": "a", "snippet": "x"}, {"id": "b", "snippet": "y"}]
        out = hybrid_rerank(rows, "the a", limit=2)
        self.assertEqual([r["id"] for r in out], ["a", "b"])


class FuseRankedListsTest(unittest.TestCase):
    def test_row_in_two_lists_outranks_row_in_one(self) -> None:
        a = [{"kind": "episode", "id": "1"}, {"kind": "episode", "id": "2"}]
        b = [{"kind": "episode", "id": "1"}, {"kind": "episode", "id": "3"}]
        out = fuse_ranked_lists([a, b], limit=10)
        self.assertEqual(out[0]["id"], "1")
        ids = [r["id"] for r in out]
        self.assertIn("2", ids)
        self.assertIn("3", ids)

    def test_two_list_score_matches_hybrid_rerank(self) -> None:
        """A two-list fuse reproduces hybrid_rerank's score for the same rows."""
        cosine = [{"kind": "e", "id": "a"}, {"kind": "e", "id": "b"}]
        lexical = [{"kind": "e", "id": "b"}, {"kind": "e", "id": "a"}]
        out = fuse_ranked_lists([cosine, lexical], limit=10)
        by_id = {r["id"]: r["score"] for r in out}
        # rank(a) = 1st in cosine (i=0), 2nd in lexical (rank=1)
        expected_a = 1.0 / (20 + 1) + 1.0 / (20 + 2)
        expected_b = 1.0 / (20 + 2) + 1.0 / (20 + 1)
        self.assertAlmostEqual(by_id["a"], round(expected_a, 6))
        self.assertAlmostEqual(by_id["b"], round(expected_b, 6))

    def test_empty_lists_yield_empty_result(self) -> None:
        self.assertEqual(fuse_ranked_lists([], limit=5), [])
        self.assertEqual(fuse_ranked_lists([[], []], limit=5), [])

    def test_limit_is_respected(self) -> None:
        rows = [{"kind": "e", "id": str(i)} for i in range(10)]
        out = fuse_ranked_lists([rows], limit=3)
        self.assertEqual(len(out), 3)

    def test_custom_key_function(self) -> None:
        a = [{"slug": "x"}]
        b = [{"slug": "x"}, {"slug": "y"}]
        out = fuse_ranked_lists([a, b], limit=10, key=lambda r: r["slug"])
        self.assertEqual({r["slug"] for r in out}, {"x", "y"})

    def test_first_list_fields_win_but_score_is_overwritten(self) -> None:
        a = [{"kind": "e", "id": "1", "label": "from-a", "score": 0.9}]
        b = [{"kind": "e", "id": "1", "label": "from-b"}]
        out = fuse_ranked_lists([a, b], limit=10)
        self.assertEqual(out[0]["label"], "from-a")
        self.assertNotEqual(out[0]["score"], 0.9)


class ParseTimeFilterTest(unittest.TestCase):
    def test_relative_days(self) -> None:
        before = datetime.now(timezone.utc) - timedelta(days=7, seconds=5)
        got = parse_time_filter("7d")
        after = datetime.now(timezone.utc) - timedelta(days=7, seconds=-5)
        self.assertTrue(before <= got <= after)

    def test_relative_hours_and_minutes(self) -> None:
        self.assertIsInstance(parse_time_filter("24h"), datetime)
        self.assertIsInstance(parse_time_filter("30m"), datetime)

    def test_iso_date_is_utc(self) -> None:
        got = parse_time_filter("2026-08-01")
        self.assertEqual(got.tzinfo, timezone.utc)
        self.assertEqual(got.year, 2026)

    def test_iso_datetime_with_z(self) -> None:
        got = parse_time_filter("2026-08-01T12:00:00Z")
        self.assertEqual(got.hour, 12)
        self.assertIsNotNone(got.tzinfo)

    def test_empty_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_time_filter("")

    def test_garbage_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_time_filter("not-a-date")


if __name__ == "__main__":
    unittest.main()
