"""Tests for khipu.embed — P3 step 3 (vectors).

Pure tests cover chunking, text assembly, L2, and the vector literal. Live tests
hit Postgres (and one hits the Gemini API for a single tiny batch): profile is
active, coverage reads, backfill dry-run is idempotent against the live corpus,
and a probe episode gets embedded on capture and is then removed. Live tests
skip when PG is unreachable; the API test additionally skips without a key.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import unittest

from khipu import embed as em


def _pg_available() -> bool:
    try:
        from khipu.db import connect

        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception:
        return False
    return True


def _key_available() -> bool:
    try:
        from khipu.keychain import resolve_gemini_key

        return bool(resolve_gemini_key())
    except Exception:
        return False


PG_AVAILABLE = _pg_available()
KEY_AVAILABLE = _key_available()


class ChunkTest(unittest.TestCase):
    def test_empty_and_short(self):
        self.assertEqual(em.chunk_text(""), [])
        self.assertEqual(em.chunk_text("   "), [])
        self.assertEqual(em.chunk_text("hello"), ["hello"])

    def test_exact_boundary_single_chunk(self):
        self.assertEqual(len(em.chunk_text("x" * em.CHUNK_CHARS)), 1)

    def test_long_text_overlaps_and_covers(self):
        text = "".join(chr(97 + (i % 26)) for i in range(em.CHUNK_CHARS * 2 + 500))
        chunks = em.chunk_text(text)
        self.assertGreaterEqual(len(chunks), 3)
        for c in chunks:
            self.assertLessEqual(len(c), em.CHUNK_CHARS)
        self.assertEqual(chunks[0][-em.CHUNK_OVERLAP :], chunks[1][: em.CHUNK_OVERLAP])
        self.assertTrue(text.endswith(chunks[-1]))


class TextAssemblyTest(unittest.TestCase):
    def test_episode_text_includes_context_lists(self):
        t = em.episode_text(
            {"summary": "S", "decisions": ["d1", "d2"], "preferences": [],
             "topics": ["k"], "people": None}
        )
        self.assertTrue(t.startswith("S"))
        self.assertIn("decisions: d1; d2", t)
        self.assertIn("topics: k", t)
        self.assertNotIn("preferences:", t)
        self.assertNotIn("people:", t)

    def test_topic_text_prefers_title(self):
        self.assertTrue(em.topic_text("slug", "Title", "body").startswith("Title\n\nbody"))
        self.assertTrue(em.topic_text("slug", None, "body").startswith("slug"))


class MathTest(unittest.TestCase):
    def test_l2_unit_norm_and_zero_safe(self):
        v = em._l2([3.0, 4.0])
        self.assertAlmostEqual(math.sqrt(sum(x * x for x in v)), 1.0, places=9)
        self.assertEqual(em._l2([0.0, 0.0]), [0.0, 0.0])

    def test_vec_literal_shape(self):
        lit = em._vec_literal([0.1, 0.25])
        self.assertTrue(lit.startswith("[") and lit.endswith("]"))
        self.assertEqual(lit.count(","), 1)


class ProfilePrefixTest(unittest.TestCase):
    def test_v2_document_and_query_prefixes(self):
        self.assertEqual(
            em.prefix_document("hello", title="T"),
            "title: T | text: hello",
        )
        self.assertEqual(
            em.prefix_query("why pgvector"),
            "task: search result | query: why pgvector",
        )

    def test_v2_profile_uses_prefixes_001_does_not(self):
        self.assertTrue(em.uses_task_prefixes(em.PROFILE_2))
        self.assertFalse(em.uses_task_prefixes(em.PROFILE_001))

    def test_model_for_profile_maps_known_ids(self):
        self.assertEqual(em.model_for_profile(em.PROFILE_001), em.MODEL_001)
        self.assertEqual(em.model_for_profile(em.PROFILE_2), em.MODEL_2)
        with self.assertRaises(ValueError):
            em.model_for_profile("nope@0")

    def test_api_texts_prefix_only_for_v2(self):
        pairs = [("Title", "body chunk")]
        self.assertEqual(em._api_texts(em.PROFILE_001, pairs), ["body chunk"])
        self.assertEqual(
            em._api_texts(em.PROFILE_2, pairs),
            ["title: Title | text: body chunk"],
        )


@unittest.skipUnless(PG_AVAILABLE, "Postgres unreachable; skipping live embed checks")
class LiveCorpusTest(unittest.TestCase):
    def test_active_profile_is_known_768(self):
        from khipu.db import connect

        with connect() as conn, conn.cursor() as cur:
            active = em._active_profile(cur)
        self.assertIn(active, {em.PROFILE_001, em.PROFILE_2})
        self.assertTrue(active.endswith("@768"))

    def test_coverage_shape(self):
        c = em.coverage()
        self.assertIn(c["active_profile"], {em.PROFILE_001, em.PROFILE_2})
        self.assertEqual(c["profile"], c["active_profile"])
        for kind in ("episodes", "topics"):
            self.assertIn("pct", c[kind])
            self.assertLessEqual(c[kind]["embedded"], c[kind]["total"])

    def test_backfill_dry_run_is_idempotent_after_full_run(self):
        """After the 2026-08-17 full backfill a dry run must want to embed ~nothing
        (allow a small tail for rows captured since — never a large one)."""
        stats = em.backfill(dry_run=True)
        self.assertLessEqual(stats["would_embed"], 50, stats)


@unittest.skipUnless(PG_AVAILABLE and KEY_AVAILABLE, "PG or Gemini key unavailable")
class LiveEmbedOnCaptureTest(unittest.TestCase):
    def test_probe_episode_gets_vector_then_cleanup(self):
        from khipu import capture as cap
        from khipu.db import connect

        summary = f"khipu-embed-test-probe {os.getpid()} — safe to delete"
        payload = cap.load_payload(json.dumps({"summary": summary}))
        md = hashlib.md5(summary.encode()).hexdigest()
        eid = None
        # hub reverse-mirrors to the legacy file (2026-08-17): never write a
        # probe line into the real episodes.jsonl from a test — stub the leg.
        from unittest import mock as _mock
        file_leg = _mock.patch.object(cap, "run_capture_v2", lambda p, suppress_mirror: 0)
        file_leg.start()
        self.addCleanup(file_leg.stop)
        try:
            self.assertEqual(cap.capture(payload, mode="hub"), 0)
            with connect() as conn, conn.cursor() as cur:
                cur.execute("SELECT id FROM episodes WHERE md5(summary)=%s", (md,))
                eid = str(cur.fetchone()[0])
                cur.execute("SELECT id FROM embedding_profiles WHERE is_active")
                active = cur.fetchone()[0]
                cur.execute(
                    "SELECT COUNT(*) FROM memory_embeddings "
                    "WHERE kind='episode' AND ref=%s AND profile=%s",
                    (eid, active),
                )
                self.assertEqual(cur.fetchone()[0], 1)
            hits = em.semantic_search(summary, limit=3, kind="episode")
            self.assertTrue(any(h["id"] == eid for h in hits), hits)
            for h in hits:
                self.assertNotIn("rank_text", h, h)
        finally:
            with connect() as conn, conn.cursor() as cur:
                if eid:
                    cur.execute(
                        "DELETE FROM memory_embeddings WHERE kind='episode' AND ref=%s", (eid,)
                    )
                cur.execute("DELETE FROM episodes WHERE md5(summary)=%s", (md,))
                conn.commit()


@unittest.skipUnless(PG_AVAILABLE and KEY_AVAILABLE, "PG or Gemini key unavailable")
class LiveSemanticRankTextContractTest(unittest.TestCase):
    """P1: RRF must not leak rank_text (needs a live embed query)."""

    def test_semantic_search_strips_rank_text(self):
        hits = em.semantic_search("khipu memory", limit=5, kind="episode")
        self.assertTrue(hits)
        for h in hits:
            self.assertNotIn("rank_text", h, h)


@unittest.skipUnless(PG_AVAILABLE, "Postgres unreachable; skipping live embed checks")
class LiveRankWindowContractTest(unittest.TestCase):
    """Rank window covers extract past FETCH_LIMIT; corpus is SQL-only."""

    def test_rank_window_covers_extract_past_fetch_limit(self):
        """Functional AC: with CHUNK_CHARS rank window, no live episode embedding
        still has extract headers past the rank window (re-count after fix)."""
        from khipu.db import connect
        from khipu.snippets import FETCH_LIMIT

        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM embedding_profiles WHERE is_active")
            profile = cur.fetchone()[0]
            cur.execute(
                """
                SELECT COUNT(*) FROM memory_embeddings m
                WHERE m.profile = %s AND m.kind = 'episode'
                  AND (
                    position(E'\\ntopics:' in m.chunk_text) > %s
                    OR position(E'\\ndecisions:' in m.chunk_text) > %s
                    OR position(E'\\npreferences:' in m.chunk_text) > %s
                    OR position(E'\\npeople:' in m.chunk_text) > %s
                  )
                """,
                (profile, FETCH_LIMIT, FETCH_LIMIT, FETCH_LIMIT, FETCH_LIMIT),
            )
            past_fetch = cur.fetchone()[0]
            cur.execute(
                """
                SELECT COUNT(*) FROM memory_embeddings m
                WHERE m.profile = %s AND m.kind = 'episode'
                  AND (
                    position(E'\\ntopics:' in m.chunk_text) > %s
                    OR position(E'\\ndecisions:' in m.chunk_text) > %s
                    OR position(E'\\npreferences:' in m.chunk_text) > %s
                    OR position(E'\\npeople:' in m.chunk_text) > %s
                  )
                """,
                (profile, em.CHUNK_CHARS, em.CHUNK_CHARS, em.CHUNK_CHARS, em.CHUNK_CHARS),
            )
            past_rank = cur.fetchone()[0]
            cur.execute(
                """
                SELECT COUNT(*) FROM memory_embeddings m
                WHERE m.profile = %s AND m.kind = 'episode' AND m.chunk_idx > 0
                """,
                (profile,),
            )
            extra_chunks = cur.fetchone()[0]
        # Geometry ACs always run; past_fetch==0 only skips the "hole still exists" check.
        self.assertEqual(past_rank, 0, f"extract still past CHUNK_CHARS rank window: {past_rank}")
        self.assertEqual(
            extra_chunks, 0,
            f"active-profile episode embeddings with chunk_idx > 0: {extra_chunks}",
        )
        if past_fetch == 0:
            self.skipTest("no live episode rows with extract past FETCH_LIMIT")


class SemanticSearchRankFetchWiringTest(unittest.TestCase):
    """Lock semantic_search SQL: rank window uses CHUNK_CHARS, teaser FETCH_LIMIT."""

    def test_rank_fetch_param_is_chunk_chars_not_fetch_limit(self):
        import sys
        from types import ModuleType
        from unittest import mock

        from khipu.snippets import FETCH_LIMIT

        captured_params: list[dict] = []
        captured_sql: list[str] = []

        class FakeCur:
            def execute(self, sql, params=None):
                if isinstance(params, dict) and "rank_fetch" in params:
                    captured_params.append(dict(params))
                    captured_sql.append(sql)

            def fetchone(self):
                return (True,)

            def fetchall(self):
                return []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class FakeConn:
            def cursor(self):
                return FakeCur()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        # semantic_search does `from khipu.db import connect` inside the body.
        # Inject a stub module so this AC runs without psycopg/PG (and without
        # AttributeError when the real khipu.db never imported).
        fake_db = ModuleType("khipu.db")
        fake_db.connect = lambda: FakeConn()
        with mock.patch.dict(sys.modules, {"khipu.db": fake_db}), mock.patch.object(
            em, "embed_one", return_value=[0.0] * em.DIM
        ), mock.patch.object(em, "_active_profile", return_value=em.PROFILE_2):
            out = em.semantic_search("wiring probe", limit=3, kind="episode")
        self.assertEqual(out, [])
        self.assertTrue(captured_params, "expected semantic_search SQL with rank_fetch")
        params = captured_params[-1]
        sql = " ".join(captured_sql[-1].split())
        self.assertEqual(params["rank_fetch"], em.CHUNK_CHARS)
        self.assertEqual(params["fetch"], FETCH_LIMIT)
        self.assertNotEqual(params["rank_fetch"], params["fetch"])
        self.assertIn("left(m.chunk_text, %(rank_fetch)s) AS rank_src", sql)
        self.assertIn("left(m.chunk_text, %(fetch)s) AS snippet", sql)


if __name__ == "__main__":
    unittest.main()
