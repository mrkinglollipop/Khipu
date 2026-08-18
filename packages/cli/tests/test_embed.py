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


@unittest.skipUnless(PG_AVAILABLE, "Postgres unreachable; skipping live embed checks")
class LiveCorpusTest(unittest.TestCase):
    def test_active_profile_is_gemini768(self):
        from khipu.db import connect

        with connect() as conn, conn.cursor() as cur:
            self.assertEqual(em._active_profile(cur), em.PROFILE_ID)

    def test_coverage_shape(self):
        c = em.coverage()
        self.assertEqual(c["active_profile"], em.PROFILE_ID)
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
                cur.execute(
                    "SELECT COUNT(*) FROM memory_embeddings "
                    "WHERE kind='episode' AND ref=%s AND profile=%s",
                    (eid, em.PROFILE_ID),
                )
                self.assertEqual(cur.fetchone()[0], 1)
            hits = em.semantic_search(summary, limit=3, kind="episode")
            self.assertTrue(any(h["id"] == eid for h in hits), hits)
        finally:
            with connect() as conn, conn.cursor() as cur:
                if eid:
                    cur.execute(
                        "DELETE FROM memory_embeddings WHERE kind='episode' AND ref=%s", (eid,)
                    )
                cur.execute("DELETE FROM episodes WHERE md5(summary)=%s", (md,))
                conn.commit()


if __name__ == "__main__":
    unittest.main()
