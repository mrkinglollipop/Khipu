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


class FairFillTest(unittest.TestCase):
    """_fair_fill is pure — no DB needed."""

    def test_backfills_a_kind_missing_from_the_top_slice(self):
        top_episodes = [{"kind": "episode", "id": str(i), "score": 1.0 - i * 0.01} for i in range(8)]
        one_topic = [{"kind": "topic", "id": "t1", "score": 0.05}]
        rows = top_episodes + one_topic
        out = em._fair_fill(rows, 8)
        self.assertIn("topic", {r["kind"] for r in out})
        self.assertLessEqual(len(out), 8)

    def test_no_backfill_when_every_kind_already_present(self):
        rows = [
            {"kind": "episode", "id": "1", "score": 0.9},
            {"kind": "topic", "id": "t1", "score": 0.8},
        ]
        out = em._fair_fill(rows, 2)
        self.assertEqual(out, rows)

    def test_backfill_capped_at_limit_over_four(self):
        episodes = [{"kind": "episode", "id": str(i), "score": 1.0 - i * 0.001} for i in range(20)]
        topics = [{"kind": "topic", "id": f"t{i}", "score": 0.01} for i in range(20)]
        out = em._fair_fill(episodes + topics, 20)
        topic_count = sum(1 for r in out if r["kind"] == "topic")
        self.assertLessEqual(topic_count, 20 // 4)
        self.assertLessEqual(len(out), 20)


@unittest.skipUnless(PG_AVAILABLE and KEY_AVAILABLE, "PG or Gemini key unavailable")
class LiveHybridSearchTest(unittest.TestCase):
    def test_default_mode_is_hybrid_and_scores_present(self):
        out = em.hybrid_search("khipu", limit=6)
        self.assertEqual(out["mode"], "hybrid")
        for r in out["results"]:
            self.assertIn("score", r)

    def test_kind_node_only_returns_nodes_even_in_hybrid_mode(self):
        """Regression: cosine sub-list used to ignore kind='node' and pull in
        every other kind because 'node' is not a semantic-search kind."""
        out = em.hybrid_search("module", limit=5, kind="node")
        self.assertTrue(out["results"], "expected at least one node hit")
        self.assertTrue(all(r["kind"] == "node" for r in out["results"]))

    def test_kind_topic_restricts_hybrid_results(self):
        out = em.hybrid_search("khipu", limit=6, kind="topic")
        self.assertTrue(all(r["kind"] == "topic" for r in out["results"]))

    def test_since_filter_excludes_old_episodes(self):
        from khipu.db import connect

        out = em.hybrid_search("khipu", limit=8, kind="episode", since="7d")
        ids = [int(r["id"]) for r in out["results"]]
        if not ids:
            self.skipTest("no episode hits for 'khipu' to check dates against")
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM episodes WHERE id = ANY(%s) AND ts < now() - interval '7 days'",
                (ids,),
            )
            self.assertEqual(cur.fetchall(), [])

    def test_mode_literal_has_no_semantic_score_field_confusion(self):
        out = em.hybrid_search("khipu", limit=5, mode="literal")
        self.assertEqual(out["mode"], "literal")
        for r in out["results"]:
            self.assertIn("score", r)

    def test_mode_semantic_matches_legacy_two_list_fuse(self):
        out = em.hybrid_search("khipu memory", limit=5, mode="semantic")
        self.assertEqual(out["mode"], "semantic")
        self.assertTrue(all(r["kind"] in {"episode", "topic", "media"} for r in out["results"]))

    def test_bad_kind_for_mode_raises(self):
        with self.assertRaises(ValueError):
            em.hybrid_search("x", kind="node", mode="semantic")
        with self.assertRaises(ValueError):
            em.hybrid_search("x", kind="media", mode="hybrid")

    def test_bad_mode_raises(self):
        with self.assertRaises(ValueError):
            em.hybrid_search("x", mode="bogus")


class CosineCandidatesExcludesCommitmentsTest(unittest.TestCase):
    """Regression: memory_embeddings.kind widened to include 'commitment'
    (migration 0009) — a generic search (kind=None) must never surface a
    commitment row (its label lookup is episode-shaped and resolves wrong,
    and commitments have their own surface, khipu_owed). Caught live: real
    commitment embeddings started existing and 'commitment' leaked into
    hybrid_search/semantic_search results with no kind filter."""

    def test_sql_always_excludes_commitment_kind(self):
        from unittest import mock

        class FakeCur:
            def execute(self, sql, params=None):
                self.sql = " ".join(sql.split())
                self.params = params

            def fetchone(self):
                return (False,)

            def fetchall(self):
                return []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class FakeConn:
            def __init__(self, cur):
                self._cur = cur

            def cursor(self):
                return self._cur

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        cur = FakeCur()
        with mock.patch("khipu.db.connect", return_value=FakeConn(cur)), \
                mock.patch.object(em, "_active_profile", return_value="prof-1"), \
                mock.patch.object(em, "embed_one", return_value=[0.0] * em.DIM):
            em._cosine_candidates("query text", limit=5, kind=None)
        # Two execute() calls happen (media_assets check, then the main
        # SELECT) — the fake cursor only remembers the last, which is the
        # one that matters here.
        self.assertIn("m.kind != 'commitment'", cur.sql)


class ApplySearchFiltersHarnessTest(unittest.TestCase):
    """fix 6: episodes.harness (migration 0008) is the primary harness
    signal when it exists; the session_id-prefix split is a fallback for a
    pre-migration hub only."""

    def test_uses_the_harness_column_when_it_exists(self):
        from unittest import mock

        class _Cur:
            def execute(self, sql, params=None):
                self.sql = " ".join(sql.split())

            def fetchall(self):
                return [
                    ("1", None, "claude_code:abc", "acme/widget", "claude_code"),
                    ("2", None, "cursor:xyz", "acme/widget", "cursor"),
                ]

        cur = _Cur()
        rows = [{"kind": "episode", "id": "1", "score": 0.9},
                {"kind": "episode", "id": "2", "score": 0.8}]
        with mock.patch.object(em, "_episode_schema_flags", return_value={
            "project": True, "deleted_at": False, "harness": True, "parent_session_id": True,
        }):
            out = em._apply_search_filters(cur, rows, harness="cursor")
        self.assertEqual({r["id"] for r in out}, {"2"})
        self.assertIn(", harness", cur.sql)

    def test_falls_back_to_session_id_split_when_harness_column_absent(self):
        from unittest import mock

        class _Cur:
            def execute(self, sql, params=None):
                self.sql = " ".join(sql.split())

            def fetchall(self):
                return [
                    ("1", None, "claude_code:abc", "acme/widget"),
                    ("2", None, "cursor:xyz", "acme/widget"),
                ]

        cur = _Cur()
        rows = [{"kind": "episode", "id": "1", "score": 0.9},
                {"kind": "episode", "id": "2", "score": 0.8}]
        with mock.patch.object(em, "_episode_schema_flags", return_value={
            "project": True, "deleted_at": False, "harness": False, "parent_session_id": True,
        }):
            out = em._apply_search_filters(cur, rows, harness="claude_code")
        self.assertEqual({r["id"] for r in out}, {"1"})
        self.assertNotIn(", harness", cur.sql)


class _CatchupCursor:
    """Enough of a cursor for embed_recent_missing's commitment pass (fix
    5c): no episodes missing (isolates the commitments leg), N open
    commitments with no embedding yet, and a recorder for every INSERT."""

    def __init__(self, commitment_rows):
        self.commitment_rows = commitment_rows
        self.inserts: list[tuple] = []
        self._result: list[tuple] = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "FROM episodes e WHERE NOT EXISTS" in s:
            self._result = []
        elif "FROM commitments c WHERE c.status = 'open'" in s:
            self._result = self.commitment_rows
        elif s.startswith("INSERT INTO memory_embeddings"):
            self.inserts.append(params)
        else:
            self._result = []

    def fetchall(self):
        return list(self._result)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _CatchupConn:
    def __init__(self, cur):
        self._cur = cur
        self.commits = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class EmbedRecentMissingCommitmentsTest(unittest.TestCase):
    """fix 5c: the hook's bounded embed catch-up (embed_recent_missing) also
    embeds open commitments with no vector yet — not inline on the capture
    decision path (commitments.auto_close never calls the embed API itself
    for this)."""

    def _run(self, commitment_rows):
        from unittest import mock

        cur = _CatchupCursor(commitment_rows)
        conn = _CatchupConn(cur)
        with mock.patch("khipu.db.connect", return_value=conn), \
                mock.patch.object(em, "_active_profile", return_value="prof-1"), \
                mock.patch.object(em, "embed_batch",
                                   side_effect=lambda api, profile: [[0.0] * em.DIM for _ in api]):
            out = em.embed_recent_missing(limit=10)
        return out, cur

    def test_open_commitments_with_no_vector_get_embedded(self):
        out, cur = self._run([(1, "ship the fix"), (2, "follow up with Matt")])
        self.assertEqual(out["commitments_embedded"], 2)
        self.assertEqual(len(cur.inserts), 2)
        refs = {p[2] for p in cur.inserts}  # (profile, kind, ref, ...)
        self.assertEqual(refs, {"1", "2"})
        self.assertTrue(all(p[1] == "commitment" for p in cur.inserts))

    def test_no_open_commitments_is_a_noop(self):
        out, cur = self._run([])
        self.assertEqual(out["commitments_embedded"], 0)
        self.assertEqual(out["commitments_chunks"], 0)
        self.assertEqual(cur.inserts, [])

    def test_blank_commitment_text_is_skipped(self):
        out, _ = self._run([(1, "   ")])
        self.assertEqual(out["commitments_embedded"], 0)

    def test_missing_commitments_table_degrades_to_zero_not_a_raise(self):
        """Pre-0009 hub: the commitments table doesn't exist yet — the SELECT
        raises, and the catch-up must degrade, not blow up the whole call
        (episode catch-up already succeeded by this point)."""
        from unittest import mock

        class _RaisingCursor(_CatchupCursor):
            def execute(self, sql, params=None):
                s = " ".join(sql.split())
                if "FROM commitments c WHERE c.status = 'open'" in s:
                    raise RuntimeError('relation "commitments" does not exist')
                super().execute(sql, params)

        cur = _RaisingCursor([])
        conn = _CatchupConn(cur)
        with mock.patch("khipu.db.connect", return_value=conn), \
                mock.patch.object(em, "_active_profile", return_value="prof-1"):
            out = em.embed_recent_missing(limit=10)
        self.assertEqual(out["commitments_embedded"], 0)

    def test_snapshot_upsert_is_called_with_commitment_kind_rows(self):
        from unittest import mock

        cur = _CatchupCursor([(7, "renew the cert")])
        conn = _CatchupConn(cur)
        with mock.patch("khipu.db.connect", return_value=conn), \
                mock.patch.object(em, "_active_profile", return_value="prof-1"), \
                mock.patch.object(em, "embed_batch",
                                   side_effect=lambda api, profile: [[0.0] * em.DIM for _ in api]), \
                mock.patch("khipu.hub_snapshot.upsert_embeddings",
                           return_value={"ok": True}) as m_snap:
            em.embed_recent_missing(limit=10)
        m_snap.assert_called_once()
        (rows,), _ = m_snap.call_args
        self.assertTrue(rows)
        self.assertTrue(all(r["kind"] == "commitment" and r["ref"] == "7" for r in rows))


if __name__ == "__main__":
    unittest.main()


class SearchFilterPushdownTest(unittest.TestCase):
    """Audit 2026-09-04: project/since/until/session_id/harness ran ONLY after
    fusion, over a ~50-row pool that had been chosen without them — so a
    filtered search was blind to every matching row outside that pool. The
    predicates now ride the candidate SQL; ``_apply_search_filters`` stays as
    the safety net."""

    TS = None  # set in setUp (needs datetime)

    def setUp(self):
        import datetime as dt

        self.TS = dt.datetime(2026, 9, 3, 12, 0, tzinfo=dt.timezone.utc)

    def _cursor(self):
        ts = self.TS

        class FakeCur:
            """Models a hub where the ONLY row matching project 'acme/widget'
            (episode 999) sorts far below the unfiltered candidate pool."""

            def __init__(self):
                self.statements: list[str] = []
                self._result: list[tuple] = []

            def execute(self, sql, params=None):
                s = " ".join(sql.split())
                self.statements.append(s)
                if "information_schema.columns" in s:
                    self._result = [(c,) for c in (
                        "id", "ts", "session_id", "summary", "topics", "people",
                        "decisions", "preferences", "scope", "project", "harness",
                        "parent_session_id", "deleted_at",
                    )]
                elif "FROM topics" in s:
                    self._result = []
                elif "FROM episodes" in s and "id::text = ANY" in s:
                    # _apply_search_filters' metadata read.
                    self._result = [
                        (rid, ts, "claude_code:abc",
                         "acme/widget" if rid == "999" else "other/repo",
                         False, "claude_code")
                        for rid in (params[0] if params else [])
                    ]
                elif "FROM episodes" in s:
                    # The literal candidate pool. Pushdown present -> the DB
                    # itself returns only the matching row, however deep it is.
                    if "kf_project" in s:
                        self._result = [
                            ("999", "khipu memory deep match", [], [], [], [], ts, 2)
                        ]
                    else:
                        self._result = [
                            (str(i), "khipu memory decoy", [], [], [], [], ts, 2)
                            for i in range(1, 51)
                        ]
                else:
                    self._result = []

            def fetchall(self):
                return list(self._result)

            def fetchone(self):
                return self._result[0] if self._result else None

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return FakeCur()

    def _search(self, cur, **kw):
        import contextlib
        from unittest import mock

        class FakeConn:
            def cursor(self_inner):
                return cur

        @contextlib.contextmanager
        def _hub():
            yield FakeConn()

        with mock.patch("khipu.hub_snapshot.try_hub_connect", _hub), \
                mock.patch("khipu.topic_graph.enrich_search_results",
                           side_effect=lambda c, rows: list(rows)):
            return em.hybrid_search("khipu memory", limit=5, mode="literal", **kw)

    def test_a_filtered_search_reaches_a_row_outside_the_unfiltered_pool(self):
        plain = self._search(self._cursor())
        self.assertNotIn("999", [str(r["id"]) for r in plain["results"]],
                         "999 is deliberately outside the unfiltered top pool")

        cur = self._cursor()
        filtered = self._search(cur, project="acme/widget")
        self.assertEqual([str(r["id"]) for r in filtered["results"]], ["999"])
        pool_sql = [s for s in cur.statements if "FROM episodes" in s and "ANY" not in s]
        self.assertTrue(pool_sql)
        self.assertIn("COALESCE(project, scope) ILIKE %(kf_project)s", pool_sql[0])

    def test_episode_only_filters_drop_the_topic_and_node_queries_entirely(self):
        cur = self._cursor()
        self._search(cur, session_id="claude_code:abc")
        self.assertFalse([s for s in cur.statements
                          if "FROM topics" in s and "SELECT slug" in s],
                         "session_id is episode-only; topics must not be queried")
        pool_sql = [s for s in cur.statements if "FROM episodes" in s and "ANY" not in s]
        self.assertIn("session_id LIKE %(kf_session)s", pool_sql[0])

    def test_time_bounds_reach_every_kind_that_has_a_timestamp(self):
        f = em._SearchFilters(since="7d", until=None)
        self.assertTrue(f.active)
        self.assertFalse(f.episode_only)
        self.assertIn("COALESCE(t.updated_at, t.created_at) >= %(kf_since)s", f.topic_sql("t"))
        self.assertIn("n.built_at >= %(kf_since)s", f.node_sql("n"))
        self.assertFalse(f.media_allowed, "media has no timestamp to bound")

    def test_no_filters_means_no_predicates_and_no_params(self):
        f = em._SearchFilters()
        self.assertFalse(f.active)
        self.assertEqual(f.params, {})
        self.assertEqual(f.topic_sql(), "TRUE")
        self.assertEqual(f.node_sql(), "TRUE")

    def test_harness_falls_back_to_the_session_id_prefix_without_the_column(self):
        class NoHarnessCur:
            def execute(self, sql, params=None):
                self._r = [(c,) for c in ("id", "ts", "session_id", "scope")]

            def fetchall(self):
                return list(self._r)

        f = em._SearchFilters(harness="cursor")
        sql = f.episode_sql(NoHarnessCur(), "e")
        self.assertIn("split_part(COALESCE(e.session_id, ''), ':', 1) = %(kf_harness)s", sql)
        self.assertNotIn("e.harness", sql)


class CommitmentCoverageTest(unittest.TestCase):
    """Audit 2026-09-04: commitments carry vectors (migration 0009) and
    commitments.auto_close depends on them, but `khipu embed status` reported
    nothing about them — a hub whose commitment catch-up had fallen behind
    looked perfectly green."""

    class _Cur:
        def __init__(self, *, has_commitments=True):
            self.has_commitments = has_commitments
            self._result: list[tuple] = []

        def execute(self, sql, params=None):
            s = " ".join(sql.split())
            if "to_regclass('public.media_assets')" in s:
                self._result = [(False,)]
            elif "to_regclass('public.commitments')" in s:
                self._result = [(self.has_commitments,)]
            elif "information_schema.columns" in s:
                self._result = [(c,) for c in ("id", "ts", "summary", "deleted_at")]
            elif "COUNT(*) FROM episodes" in s:
                self._result = [(10,)]
            elif "COUNT(*) FROM topics" in s:
                self._result = [(4,)]
            elif "COUNT(*) FROM commitments" in s:
                self._result = [(6,)]
            elif "JOIN commitments" in s:
                self._result = [(2, 3)]
            elif "JOIN episodes" in s:
                self._result = [(10, 12)]
            elif "GROUP BY kind" in s:
                self._result = [("episode", 10, 12), ("topic", 4, 5), ("commitment", 2, 3)]
            elif "FROM embedding_profiles ORDER BY" in s:
                self._result = [("p1", "m", 768, True)]
            elif "FROM embedding_profiles WHERE is_active" in s:
                self._result = [("p1",)]
            else:
                self._result = []

        def fetchone(self):
            return self._result[0] if self._result else None

        def fetchall(self):
            return list(self._result)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _coverage(self, cur):
        from unittest import mock

        class FakeConn:
            def cursor(self_inner):
                return cur

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        with mock.patch("khipu.db.connect", return_value=FakeConn()), \
                mock.patch.object(em, "_resolve_profile", return_value="p1"):
            return em.coverage()

    def test_commitments_are_reported(self):
        cov = self._coverage(self._Cur())
        self.assertEqual(cov["commitments"],
                         {"total": 6, "embedded": 2, "missing": 4, "chunks": 3, "pct": 33.3})

    def test_a_pre_0009_hub_reports_zeroes_not_an_error(self):
        cov = self._coverage(self._Cur(has_commitments=False))
        self.assertEqual(cov["commitments"]["total"], 0)
        self.assertEqual(cov["commitments"]["missing"], 0)


class BackfillCommitmentKindTest(unittest.TestCase):
    """`khipu embed backfill --kind commitment` heals commitment vectors the
    Stop-hook catch-up fell behind on. Opt-in only: never part of the default
    all-kinds sweep (their vectors serve auto_close, not generic search)."""

    class _Cur:
        def __init__(self):
            self.statements: list[str] = []
            self._result: list[tuple] = []

        def execute(self, sql, params=None):
            s = " ".join(sql.split())
            self.statements.append(s)
            if "information_schema.columns" in s:
                self._result = [(c,) for c in ("id", "summary", "deleted_at")]
            elif "FROM commitments" in s:
                self._result = [(7, "ship the merge fix"), (8, "   ")]
            elif "FROM episodes" in s:
                self._result = []
            elif "FROM topics" in s:
                self._result = []
            else:
                self._result = []

        def fetchall(self):
            return list(self._result)

        def fetchone(self):
            return self._result[0] if self._result else None

    def test_kind_commitment_yields_open_commitments(self):
        cur = self._Cur()
        rows = list(em._iter_sources(cur, kind="commitment"))
        self.assertEqual([(k, r) for k, r, _t, _ti in rows], [("commitment", "7")])
        self.assertTrue(any("status = 'open'" in s for s in cur.statements))

    def test_the_default_sweep_never_touches_commitments(self):
        cur = self._Cur()
        list(em._iter_sources(cur, kind=None))
        self.assertFalse([s for s in cur.statements if "FROM commitments" in s])

    def test_the_cli_accepts_the_new_kind(self):
        from khipu.cli import build_parser

        args = build_parser().parse_args(["embed", "backfill", "--kind", "commitment"])
        self.assertEqual(args.kind, "commitment")
