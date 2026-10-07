# --bypass-harness (sonnet lane) — authored directly by the dispatched on-sub
# Sonnet build agent (brief: do not delegate); no further agent to route to.
"""Library search and `khipu get library:...` (Session C).

A fake hub cursor answers exactly the statements ``khipu.library_search``
issues; the query embedding, the memory legs and the enrichers are patched
out, so nothing here touches Postgres or a model provider.
"""
from __future__ import annotations

import argparse
import contextlib
import inspect
import io
import json
import unittest
from unittest import mock

from khipu import embed as em
from khipu import library_search as ls

# patch targets resolved by dotted name must be imported first
import khipu.activity  # noqa: F401
import khipu.cli  # noqa: F401
import khipu.db  # noqa: F401
import khipu.decisions  # noqa: F401
import khipu.hub_snapshot  # noqa: F401
import khipu.query_log  # noqa: F401
import khipu.topic_graph  # noqa: F401

PROFILE = "voyage-3@1024"
QVEC = [0.1, 0.2, 0.3]


class FakeCur:
    """Cursor over canned library rows; records every statement + params."""

    def __init__(self, *, libs=None, cosine=None, chunks=None, titles=None,
                 fail_chunks=False, doc=None, chunk_rows=None, chunk_count=3,
                 ilike=None, fail_fts=False, index_present=True, cosine_error=None):
        self.libs = [("biblical", PROFILE)] if libs is None else libs
        self.cosine = cosine or []
        self.chunks = chunks or []
        self.titles = titles or []
        self.fail_chunks = fail_chunks      # the ILIKE substring scan times out
        self.fail_fts = fail_fts            # the full-text statement times out
        self.ilike = ilike or []
        self.index_present = index_present  # the profile's HNSW index exists
        self.cosine_error = cosine_error    # raised by the cosine statement
        self.doc = doc
        self.chunk_rows = chunk_rows or []
        self.chunk_count = chunk_count
        self.executed: list[tuple[str, object]] = []
        self._res: list[tuple] = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.executed.append((s, params))
        if "to_regclass('public.library_sources')" in s:
            self._res = [(True,)]
        elif "FROM library_sources" in s:
            src = (params or {}).get("src")
            self._res = [r for r in self.libs if src is None or r[0] == src]
        elif "FROM pg_indexes" in s:
            self._res = [(1,)] if self.index_present else []
        elif "FROM library_embeddings" in s:
            if self.cosine_error:
                raise self.cosine_error
            self._res = list(self.cosine)
        elif "FROM library_chunks c JOIN library_documents d" in s:
            fts = "plainto_tsquery" in s
            if (self.fail_fts if fts else self.fail_chunks):
                raise RuntimeError("canceling statement due to statement timeout")
            self._res = list(self.chunks if fts else self.ilike)
        elif "FROM library_documents d WHERE" in s:
            self._res = list(self.titles)
        elif "FROM library_documents d JOIN library_sources s" in s:
            self._res = [self.doc] if self.doc else []
        elif "SELECT COUNT(*) FROM library_chunks" in s:
            self._res = [(self.chunk_count,)]
        elif "SELECT chunk_idx, chunk_text FROM library_chunks" in s:
            wanted = set(params[1])
            self._res = [r for r in self.chunk_rows if r[0] in wanted]
        else:
            self._res = []

    def fetchall(self):
        return list(self._res)

    def fetchone(self):
        return self._res[0] if self._res else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def sql(self, needle):
        return [(s, p) for s, p in self.executed if needle in s]


class FakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.rollbacks = 0

    def cursor(self):
        return self._cur

    def commit(self):
        pass

    def rollback(self):
        self.rollbacks += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _cos_row(doc=7, idx=3, score=0.82, title="The Unseen Realm", author="Heiser",
             text="the divine council of Deuteronomy 32", rel="Heiser/Unseen Realm.txt"):
    return ("biblical", doc, idx, score, text, text, title, author, rel)


def _chunk_row(doc=7, idx=3, text="the divine council of Deuteronomy 32", hits=2,
               title="The Unseen Realm", author="Heiser", rel="Heiser/Unseen Realm.txt"):
    return ("biblical", doc, idx, text, title, author, rel, hits)


def _title_row(doc=9, title="What Does God Want", author="Heiser",
               rel="Heiser/What Does God Want.md", hits=2):
    return ("biblical", doc, title, author, rel, hits)


@contextlib.contextmanager
def _hub(cur, *, query_vec=None):
    """Patch the hub cursor, the memory legs and the enrichers."""
    conn = FakeConn(cur)

    @contextlib.contextmanager
    def _connect(*a, **k):
        yield conn

    qv = query_vec if query_vec is not None else mock.Mock(return_value=(QVEC, "off"))
    ls._TABLES_SEEN = False
    ls._INDEX_SEEN.clear()
    with mock.patch("khipu.hub_snapshot.try_hub_connect", _connect), \
            mock.patch.object(em, "_cosine_candidates", return_value=[]), \
            mock.patch.object(em, "_query_vec", qv), \
            mock.patch.object(em, "uses_task_prefixes", return_value=False), \
            mock.patch("khipu.cli._literal_candidates", return_value=[]), \
            mock.patch.object(em, "_apply_search_filters", side_effect=lambda c, r, **k: r), \
            mock.patch("khipu.topic_graph.enrich_search_results", side_effect=lambda c, r: list(r)), \
            mock.patch("khipu.decisions.enrich_search_results", side_effect=lambda c, r: list(r)):
        yield conn


def _ids(payload):
    return [r["id"] for r in payload["results"]]


class LibraryLegTest(unittest.TestCase):
    def test_cosine_and_literal_rows_fuse_with_ids_labels_and_paths(self):
        cur = FakeCur(
            cosine=[_cos_row(7, 3, 0.82), _cos_row(7, 4, 0.74, text="Psalm 82 elohim")],
            chunks=[_chunk_row(7, 3)],
        )
        with _hub(cur):
            out = em.hybrid_search("divine council Deuteronomy", kind="library", limit=5)
        self.assertEqual(out["libraries"], ["biblical"])
        top = out["results"][0]
        self.assertEqual(top["id"], "library:biblical:7#3")   # in both legs
        self.assertEqual(top["kind"], "library")
        self.assertEqual(top["label"], "Heiser, The Unseen Realm")
        self.assertEqual(top["path"], "Heiser/Unseen Realm.txt")
        self.assertEqual(top["source"], "biblical")
        self.assertIn("Deuteronomy 32", top["snippet"])
        self.assertEqual(top["cosine"], 0.82)
        self.assertGreaterEqual(top["lexical_hits"], 1)
        self.assertNotIn("rank_text", top)
        self.assertIn("library:biblical:7#4", _ids(out))
        for key in ("library_embed_ms", "library_cosine_ms", "library_literal_ms"):
            self.assertIn(key, out["timing"])

    def test_title_only_hit_has_no_chunk_suffix(self):
        cur = FakeCur(titles=[_title_row(9)])
        with _hub(cur):
            out = em.hybrid_search("What Does God Want", kind="library", mode="literal")
        self.assertEqual(_ids(out), ["library:biblical:9"])
        row = out["results"][0]
        self.assertEqual(row["label"], "Heiser, What Does God Want")
        self.assertEqual(row["path"], "Heiser/What Does God Want.md")
        self.assertNotIn("chunk_idx", row)

    def test_semantic_mode_runs_only_the_cosine_leg(self):
        cur = FakeCur(cosine=[_cos_row()], chunks=[_chunk_row()], titles=[_title_row()])
        with _hub(cur):
            out = em.hybrid_search("divine council", kind="library", mode="semantic")
        self.assertEqual(_ids(out), ["library:biblical:7#3"])
        self.assertFalse(cur.sql("FROM library_chunks c JOIN"))

    def test_literal_mode_never_embeds_the_query(self):
        cur = FakeCur(chunks=[_chunk_row()])
        qv = mock.Mock(return_value=(QVEC, "off"))
        with _hub(cur, query_vec=qv):
            out = em.hybrid_search("divine council", kind="library", mode="literal")
        qv.assert_not_called()
        self.assertEqual(_ids(out), ["library:biblical:7#3"])
        self.assertFalse(cur.sql("FROM library_embeddings"))

    def test_the_memory_legs_do_not_run_for_kind_library(self):
        cur = FakeCur(cosine=[_cos_row()])
        with _hub(cur):
            with mock.patch("khipu.cli._literal_candidates") as lit, \
                    mock.patch.object(em, "_cosine_candidates") as cos:
                em.hybrid_search("divine council", kind="library")
        lit.assert_not_called()
        cos.assert_not_called()

    def test_default_search_includes_libraries_beside_memory(self):
        cur = FakeCur(cosine=[_cos_row(score=0.82)])
        mem = {"kind": "episode", "id": "11", "label": "ep", "snippet": "ep", "score": 0.9,
               "rank_text": "divine council"}
        with _hub(cur):
            with mock.patch.object(em, "_cosine_candidates", return_value=[mem]):
                out = em.hybrid_search("divine council", limit=5)
        self.assertEqual({r["kind"] for r in out["results"]}, {"episode", "library"})

    def test_default_search_keeps_a_weak_library_neighbour_out_of_fusion(self):
        weak = _cos_row(7, 9, 0.30, text="unrelated gardening notes")
        strong = _cos_row(7, 3, 0.82)
        cur = FakeCur(cosine=[strong, weak])
        with _hub(cur):
            default = em.hybrid_search("divine council", limit=8)
            explicit = em.hybrid_search("divine council", kind="library", limit=8)
        self.assertEqual(_ids(default), ["library:biblical:7#3"])
        self.assertIn("library:biblical:7#9", _ids(explicit))   # explicit keeps every candidate

    def test_include_libraries_false_never_touches_the_library_tables(self):
        cur = FakeCur(cosine=[_cos_row()])
        with _hub(cur):
            em.hybrid_search("divine council", include_libraries=False)
        self.assertFalse(cur.sql("library_"))

    def test_kind_library_with_include_libraries_false_is_refused(self):
        with self.assertRaises(ValueError):
            em.hybrid_search("x", kind="library", include_libraries=False)

    def test_an_episode_shaped_filter_switches_the_library_legs_off(self):
        cur = FakeCur(cosine=[_cos_row()])
        with _hub(cur):
            em.hybrid_search("divine council", project="acme/widget")
            em.hybrid_search("divine council", since="7d")
        self.assertFalse(cur.sql("library_"))

    def test_no_library_tables_means_no_libraries_and_no_error(self):
        class NoTables(FakeCur):
            def execute(self, sql, params=None):
                if "to_regclass('public.library_sources')" in " ".join(sql.split()):
                    self.executed.append((sql, params))
                    self._res = [(False,)]
                    return
                super().execute(sql, params)

        cur = NoTables()
        with _hub(cur):
            out = em.hybrid_search("divine council", limit=3)
        self.assertNotIn("libraries", out)
        self.assertFalse(cur.sql("FROM library_sources"))

    def test_a_cursor_that_cannot_run_the_statements_just_has_no_libraries(self):
        class Bare:           # no execute(), like the rerank tests' fake hub
            def __enter__(self): return self
            def __exit__(self, *a): return False

        conn = FakeConn(Bare())
        ls._TABLES_SEEN = False
        self.assertEqual(ls.enabled_libraries(Bare(), conn), [])
        self.assertEqual(conn.rollbacks, 1)


class SourceFilterTest(unittest.TestCase):
    def test_source_restricts_the_search_to_that_library(self):
        cur = FakeCur(libs=[("biblical", PROFILE), ("hymns", PROFILE)],
                      cosine=[_cos_row()])
        with _hub(cur):
            out = em.hybrid_search("divine council", kind="library", source="biblical")
        (_, params), = cur.sql("FROM library_sources WHERE")
        self.assertEqual(params["src"], "biblical")
        (_, cos_params), = cur.sql("FROM library_embeddings")
        self.assertEqual(cos_params["sources"], ["biblical"])
        self.assertEqual(out["libraries"], ["biblical"])

    def test_source_alone_implies_kind_library(self):
        cur = FakeCur(cosine=[_cos_row()])
        mem = {"kind": "episode", "id": "11", "label": "ep", "snippet": "ep", "score": 0.9,
               "rank_text": "divine council"}
        with _hub(cur):
            with mock.patch.object(em, "_cosine_candidates", return_value=[mem]) as cos:
                out = em.hybrid_search("divine council", source="biblical")
        cos.assert_not_called()
        self.assertEqual({r["kind"] for r in out["results"]}, {"library"})

    def test_unknown_source_is_a_value_error(self):
        cur = FakeCur(libs=[("biblical", PROFILE)])
        with _hub(cur):
            with self.assertRaises(ValueError) as ctx:
                em.hybrid_search("divine council", source="nope")
        self.assertIn("nope", str(ctx.exception))

    def test_source_with_another_kind_is_refused(self):
        with self.assertRaises(ValueError):
            em.hybrid_search("x", kind="episode", source="biblical")


class DegradedLegTest(unittest.TestCase):
    def test_a_failed_query_embedding_degrades_to_literal_and_names_the_leg(self):
        cur = FakeCur(cosine=[_cos_row()], chunks=[_chunk_row(7, 3)])
        qv = mock.Mock(side_effect=RuntimeError("embed HTTP 503: unavailable"))
        with _hub(cur, query_vec=qv):
            out = em.hybrid_search("divine council", kind="library")
        self.assertIn(f"library_embed:{PROFILE}", out["degraded_legs"])
        self.assertEqual(_ids(out), ["library:biblical:7#3"])        # literal still answered
        self.assertFalse(cur.sql("FROM library_embeddings"))
        self.assertIn("library_embed_error", out["timing"])

    def test_a_timed_out_full_text_leg_drops_only_that_leg(self):
        cur = FakeCur(titles=[_title_row(9)], fail_fts=True)
        with _hub(cur):
            out = em.hybrid_search("What Does God Want", kind="library", mode="literal")
        self.assertIn("library_fts", out["degraded_legs"])
        self.assertEqual(_ids(out), ["library:biblical:9"])

    def test_a_timed_out_substring_scan_drops_only_that_leg(self):
        cur = FakeCur(chunks=[_chunk_row(7, 3)], fail_chunks=True)
        with _hub(cur):
            out = em.hybrid_search('"divine council"', kind="library", mode="literal")
        self.assertIn("library_literal", out["degraded_legs"])
        self.assertNotIn("library_fts", out["degraded_legs"])
        self.assertEqual(_ids(out), ["library:biblical:7#3"])

    def test_the_full_text_scan_runs_under_a_statement_timeout_in_a_savepoint(self):
        cur = FakeCur(chunks=[_chunk_row()])
        with _hub(cur):
            em.hybrid_search("divine council", kind="library", mode="literal")
        stmts = [s for s, _ in cur.executed]
        i = next(n for n, s in enumerate(stmts) if s.startswith("SAVEPOINT khipu_lib_fts"))
        self.assertTrue(stmts[i + 1].startswith("SET LOCAL statement_timeout = "))
        self.assertIn("FROM library_chunks c", stmts[i + 2])
        self.assertTrue(stmts[i + 3].startswith("ROLLBACK TO SAVEPOINT khipu_lib_fts"))


class UnindexedProfileTest(unittest.TestCase):
    def test_default_search_skips_cosine_without_the_index_and_names_the_leg(self):
        cur = FakeCur(cosine=[_cos_row()], chunks=[_chunk_row(7, 3)], titles=[_title_row(9, title="Divine Council Primer", rel="Heiser/Primer.md")],
                      index_present=False)
        qv = mock.Mock(return_value=(QVEC, "off"))
        with _hub(cur, query_vec=qv):
            out = em.hybrid_search("divine council", limit=5)
        self.assertIn(f"library_cosine:{PROFILE}:no-index", out["degraded_legs"])
        self.assertFalse(cur.sql("FROM library_embeddings"))
        qv.assert_not_called()                                  # no wasted embed either
        self.assertIn("library:biblical:7#3", _ids(out))         # full-text leg still ran
        self.assertIn("library:biblical:9", _ids(out))           # so did title/author

    def test_default_search_runs_cosine_when_the_index_exists(self):
        cur = FakeCur(cosine=[_cos_row()], index_present=True)
        with _hub(cur):
            out = em.hybrid_search("divine council", limit=5)
        self.assertEqual(len(cur.sql("FROM library_embeddings")), 1)
        self.assertNotIn(f"library_cosine:{PROFILE}:no-index", out.get("degraded_legs", []))
        self.assertIn("library:biblical:7#3", _ids(out))

    def test_the_index_check_is_looked_up_by_the_profile_index_name(self):
        from khipu.profiles import profile_index_name
        cur = FakeCur(cosine=[_cos_row()])
        with _hub(cur):
            em.hybrid_search("divine council", limit=5)
        (_, params), = cur.sql("FROM pg_indexes")
        self.assertEqual(params, (profile_index_name(PROFILE, "library_embeddings"),))

    def test_the_answer_is_cached_per_process_for_sixty_seconds(self):
        cur = FakeCur(cosine=[_cos_row()], index_present=False)
        clock = [1000.0]
        with _hub(cur), mock.patch.object(ls.time, "monotonic", side_effect=lambda: clock[0]):
            em.hybrid_search("divine council", limit=5)
            em.hybrid_search("divine council again", limit=5)
            self.assertEqual(len(cur.sql("FROM pg_indexes")), 1)
            cur.index_present = True                  # the index is built meanwhile
            clock[0] += ls.INDEX_CHECK_TTL_S - 1
            em.hybrid_search("divine council third", limit=5)
            self.assertEqual(len(cur.sql("FROM pg_indexes")), 1)   # still the cached "no"
            self.assertFalse(cur.sql("FROM library_embeddings"))
            clock[0] += 2                              # past the TTL: re-asked, now present
            em.hybrid_search("divine council fourth", limit=5)
        self.assertEqual(len(cur.sql("FROM pg_indexes")), 2)
        self.assertEqual(len(cur.sql("FROM library_embeddings")), 1)

    def test_a_failed_index_check_fails_open_and_is_not_cached(self):
        cur = FakeCur(cosine=[_cos_row()])
        real = cur.execute

        def boom(sql, params=None):
            if "FROM pg_indexes" in sql:
                raise RuntimeError("permission denied")
            return real(sql, params)

        cur.execute = boom
        with _hub(cur):
            out = em.hybrid_search("divine council", limit=5)
        self.assertEqual(len(cur.sql("FROM library_embeddings")), 1)
        self.assertNotIn(f"library_cosine:{PROFILE}:no-index", out.get("degraded_legs", []))
        self.assertEqual(ls._INDEX_SEEN, {})

    def test_explicit_kind_library_still_runs_cosine_under_a_savepointed_timeout(self):
        cur = FakeCur(cosine=[_cos_row()], index_present=False)
        with _hub(cur):
            out = em.hybrid_search("divine council", kind="library")
        self.assertIn("library:biblical:7#3", _ids(out))
        self.assertNotIn(f"library_cosine:{PROFILE}:no-index", out.get("degraded_legs", []))
        stmts = [s for s, _ in cur.executed]
        i = next(n for n, s in enumerate(stmts) if s.startswith("SAVEPOINT khipu_lib_cos"))
        self.assertEqual(stmts[i + 1], f"SET LOCAL statement_timeout = {ls.UNINDEXED_COSINE_TIMEOUT_MS}")
        self.assertEqual(ls.UNINDEXED_COSINE_TIMEOUT_MS, 15000)
        j = next(n for n, s in enumerate(stmts) if "FROM library_embeddings" in s)
        self.assertGreater(j, i + 1)
        self.assertTrue(any(s.startswith("ROLLBACK TO SAVEPOINT khipu_lib_cos") for s in stmts[j:]))

    def test_explicit_search_names_a_timed_out_cosine_leg(self):
        cur = FakeCur(chunks=[_chunk_row(7, 3)], index_present=False,
                      cosine_error=RuntimeError("canceling statement due to statement timeout"))
        with _hub(cur):
            out = em.hybrid_search("divine council", kind="library")
        self.assertIn(f"library_cosine:{PROFILE}:timeout", out["degraded_legs"])
        self.assertEqual(_ids(out), ["library:biblical:7#3"])     # literal legs unaffected

    def test_an_indexed_explicit_search_sets_no_cosine_timeout(self):
        cur = FakeCur(cosine=[_cos_row()], index_present=True)
        with _hub(cur):
            em.hybrid_search("divine council", kind="library", mode="semantic")
        self.assertFalse([s for s, _ in cur.executed if s.startswith("SET LOCAL statement_timeout")])


class KeywordLegTest(unittest.TestCase):
    def test_full_text_search_is_the_chunk_keyword_leg(self):
        cur = FakeCur(chunks=[_chunk_row(7, 3)])
        with _hub(cur):
            out = em.hybrid_search("divine council", kind="library", mode="literal")
        (sql, params), = cur.sql("plainto_tsquery")
        self.assertIn("c.tsv @@ plainto_tsquery('simple', %(query)s)", sql)
        self.assertIn("ts_rank(c.tsv, plainto_tsquery('simple', %(query)s))", sql)
        self.assertIn("ORDER BY rank DESC", sql)
        self.assertTrue(sql.rstrip().endswith("LIMIT %(lim)s"))
        self.assertEqual(params["query"], "divine council")
        self.assertEqual(_ids(out), ["library:biblical:7#3"])

    def test_an_ordinary_query_never_runs_the_ilike_substring_scan(self):
        cur = FakeCur(chunks=[_chunk_row()])
        with _hub(cur):
            em.hybrid_search("Deuteronomy 32 worldview disinherited nations", kind="library")
        ilike = [s for s, _ in cur.executed
                 if "FROM library_chunks c JOIN" in s and "plainto_tsquery" not in s]
        self.assertEqual(ilike, [])
        self.assertFalse([s for s, _ in cur.executed if "c.chunk_text ILIKE" in s])

    def test_a_quoted_phrase_or_an_id_shaped_token_adds_the_substring_scan(self):
        for q in ('the "divine council" idea', "khipu:library", "a1b2c3d4e5f6", "src/khipu/embed.py"):
            with self.subTest(query=q):
                self.assertTrue(ls.needs_literal(q))
                cur = FakeCur(ilike=[_chunk_row(7, 5, text=f"has {q} inside")])
                with _hub(cur):
                    out = em.hybrid_search(q, kind="library", mode="literal")
                self.assertTrue([s for s, _ in cur.executed if "c.chunk_text ILIKE" in s])
                self.assertIn("library:biblical:7#5", _ids(out))

    def test_plain_queries_do_not_need_the_substring_scan(self):
        for q in ("divine council", "Deuteronomy 32 worldview", "What Does God Want", "love"):
            self.assertFalse(ls.needs_literal(q), q)

    def test_the_substring_scan_hits_come_before_the_full_text_order(self):
        cur = FakeCur(chunks=[_chunk_row(7, 3, text="fts hit")],
                      ilike=[_chunk_row(8, 1, text="exact \"divine council\" phrase")])
        with _hub(cur):
            out = em.hybrid_search('"divine council"', kind="library", mode="literal")
        self.assertEqual(_ids(out)[0], "library:biblical:8#1")
        self.assertIn("library:biblical:7#3", _ids(out))

    def test_migration_0026_adds_the_generated_tsvector_and_its_gin_index(self):
        import re
        from pathlib import Path

        sql = (Path(__file__).resolve().parents[3] / "ops/migrations/0026_library.sql").read_text()
        body = "\n".join(l for l in sql.splitlines() if not l.lstrip().startswith("--"))
        flat = " ".join(body.split())
        self.assertIn("ALTER TABLE library_chunks ADD COLUMN IF NOT EXISTS tsv tsvector "
                      "GENERATED ALWAYS AS (to_tsvector('simple', chunk_text)) STORED;", flat)
        self.assertRegex(flat, r"CREATE INDEX IF NOT EXISTS idx_library_chunks_tsv "
                               r"ON library_chunks USING gin \(tsv\)")
        self.assertIsNone(re.search(r"CREATE EXTENSION", body))


class BoundedLegTest(unittest.TestCase):
    def test_the_cosine_statement_is_an_index_shaped_nearest_neighbour_query(self):
        sql = " ".join(ls.cosine_sql(1024).split())
        self.assertEqual(sql.count("::vector(1024)"), 4)   # score + ORDER BY, both sides
        self.assertIn("WHERE e.profile = %(p)s", sql)
        self.assertIn("ORDER BY e.embedding::vector(1024) <=> %(q)s::vector(1024) LIMIT %(lim)s", sql)
        # the document join and the source restriction come AFTER the index scan
        self.assertLess(sql.index("LIMIT %(lim)s"), sql.index("JOIN library_documents"))

    def test_the_executed_cosine_statement_casts_to_the_query_vectors_width(self):
        cur = FakeCur(cosine=[_cos_row()])
        with _hub(cur):
            em.hybrid_search("divine council", kind="library")
        (sql, params), = cur.sql("FROM library_embeddings")
        self.assertIn("embedding::vector(3)", sql)       # len(QVEC)
        self.assertEqual(params["p"], PROFILE)
        self.assertTrue(params["q"].startswith("["))

    def test_the_default_search_keeps_libraries_on_a_short_leash(self):
        cur = FakeCur(cosine=[_cos_row()])
        with _hub(cur):
            em.hybrid_search("divine council", limit=12)
            em.hybrid_search("divine council", limit=12, kind="library")
        cos = cur.sql("FROM library_embeddings")
        self.assertEqual(cos[0][1]["lim"], ls.DEFAULT_LEG_LIMIT)
        self.assertEqual(cos[1][1]["lim"], min(50, ls.EXPLICIT_LEG_LIMIT))
        timeouts = [s for s, _ in cur.executed if s.startswith("SET LOCAL statement_timeout")]
        self.assertEqual(timeouts[0], f"SET LOCAL statement_timeout = {ls.LITERAL_TIMEOUT_MS_DEFAULT}")
        self.assertEqual(timeouts[1], f"SET LOCAL statement_timeout = {ls.LITERAL_TIMEOUT_MS_EXPLICIT}")

    def test_the_query_is_embedded_once_per_distinct_profile(self):
        cur = FakeCur(libs=[("a", PROFILE), ("b", PROFILE), ("c", "gemini-embedding-001@768")],
                      cosine=[_cos_row()])
        qv = mock.Mock(return_value=(QVEC, "off"))
        with _hub(cur, query_vec=qv):
            em.hybrid_search("divine council", kind="library")
        self.assertEqual(sorted(c.args[2] for c in qv.call_args_list),
                         sorted([PROFILE, "gemini-embedding-001@768"]))
        self.assertEqual(len(cur.sql("FROM library_embeddings")), 2)


class PromptLaneTest(unittest.TestCase):
    def test_the_prompt_lane_passes_include_libraries_false(self):
        from khipu import recall_prompt as rp

        with mock.patch.object(rp, "_snapshot_search_hits",
                               side_effect=rp._SnapshotUnusable("missing")), \
                mock.patch("khipu.embed.hybrid_search",
                           return_value={"results": []}) as hub:
            rp._search_hits("a topical prompt", cwd=None)
        hub.assert_called_once()
        self.assertIs(hub.call_args.kwargs["include_libraries"], False)
        self.assertNotIn(hub.call_args.kwargs.get("kind"), ("library",))

    def test_the_prompt_modules_never_import_the_library_leg(self):
        from khipu import recall_daemon, recall_prompt

        for mod in (recall_prompt, recall_daemon):
            src = inspect.getsource(mod)
            self.assertNotIn("library_search", src, mod.__name__)
            self.assertNotIn("library_candidates", src, mod.__name__)

    def test_include_libraries_false_never_calls_the_library_leg(self):
        cur = FakeCur(cosine=[_cos_row()])
        with _hub(cur), mock.patch.object(ls, "library_candidates",
                                           side_effect=AssertionError("library leg ran")):
            em.hybrid_search("a topical prompt", mode="semantic", include_libraries=False)

    def test_reflect_and_probe_also_opt_out(self):
        from khipu import probe, reflect

        for mod in (reflect, probe):
            self.assertIn("include_libraries=False", inspect.getsource(mod), mod.__name__)


class GetTest(unittest.TestCase):
    DOC = (7, "biblical", "Heiser/Unseen Realm.txt", "The Unseen Realm", "Heiser",
           ["bible", "heiser"], 1234, None, "abc123", None, "/corpus")

    def _get(self, ident, **kw):
        cur = FakeCur(doc=kw.get("doc", self.DOC), chunk_rows=kw.get("chunks", []),
                      chunk_count=kw.get("count", 3))
        with mock.patch("khipu.db.connect", return_value=FakeConn(cur)):
            return ls.get(ident), cur

    def test_a_chunk_id_returns_the_chunk_the_document_and_both_neighbours(self):
        out, _ = self._get("library:biblical:7#1",
                           chunks=[(0, "before"), (1, "the hit"), (2, "after")])
        self.assertEqual(out["kind"], "library")
        self.assertEqual(out["id"], "library:biblical:7#1")
        self.assertEqual(out["text"], "the hit")
        self.assertEqual(out["previous"], {"chunk_idx": 0, "id": "library:biblical:7#0", "text": "before"})
        self.assertEqual(out["next"]["text"], "after")
        self.assertEqual(out["chunk_count"], 3)
        doc = out["document"]
        self.assertEqual((doc["title"], doc["author"], doc["tags"]),
                         ("The Unseen Realm", "Heiser", ["bible", "heiser"]))
        self.assertEqual((doc["rel_path"], doc["root"]), ("Heiser/Unseen Realm.txt", "/corpus"))

    def test_the_first_chunk_has_no_previous(self):
        out, _ = self._get("library:biblical:7#0", chunks=[(0, "first"), (1, "second")])
        self.assertIsNone(out["previous"])
        self.assertEqual(out["next"]["chunk_idx"], 1)

    def test_a_document_id_returns_the_document_and_its_chunk_count(self):
        out, cur = self._get("library:biblical:7", count=41)
        self.assertEqual(out["id"], "library:biblical:7")
        self.assertEqual(out["chunk_count"], 41)
        self.assertEqual(out["document"]["title"], "The Unseen Realm")
        self.assertNotIn("text", out)
        self.assertFalse(cur.sql("SELECT chunk_idx, chunk_text"))

    def test_a_missing_chunk_or_document_is_a_value_error(self):
        with self.assertRaises(ValueError):
            self._get("library:biblical:7#5", chunks=[(0, "x")])
        with self.assertRaises(ValueError):
            self._get("library:biblical:8", doc=None)

    def test_malformed_ids_are_refused(self):
        for bad in ("library:biblical", "library:Bib cal:7", "library:biblical:x", "topic-slug"):
            with self.assertRaises(ValueError, msg=bad):
                ls.parse_id(bad)

    def test_ids_round_trip(self):
        self.assertEqual(ls.parse_id(ls.format_id("biblical", 7, 3)), ("biblical", 7, 3))
        self.assertEqual(ls.parse_id(ls.format_id("biblical", 7)), ("biblical", 7, None))

    def test_the_cli_get_routes_library_ids(self):
        from khipu import cli

        args = argparse.Namespace(id="library:biblical:7#1", kind=None)
        buf = io.StringIO()
        with mock.patch.object(ls, "get", return_value={"kind": "library", "id": args.id}), \
                contextlib.redirect_stdout(buf):
            rc = cli.cmd_get(args)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue())["kind"], "library")

    def test_the_cli_get_reports_a_miss(self):
        from khipu import cli

        buf = io.StringIO()
        with mock.patch.object(ls, "get", side_effect=ValueError("library chunk not found")), \
                contextlib.redirect_stdout(buf):
            rc = cli.cmd_get(argparse.Namespace(id="library:biblical:7#99", kind=None))
        self.assertEqual(rc, 1)
        self.assertFalse(json.loads(buf.getvalue())["ok"])


class CliSearchTest(unittest.TestCase):
    def test_the_parser_accepts_kind_library_and_source(self):
        from khipu import cli

        ns = cli.build_parser().parse_args(
            ["search", "divine council", "--kind", "library", "--source", "biblical"])
        self.assertEqual((ns.kind, ns.source), ("library", "biblical"))

    def test_cmd_search_forwards_source_and_prints_the_payload(self):
        from khipu import cli

        payload = {"query": "q", "mode": "hybrid", "results": [
            {"kind": "library", "id": "library:biblical:7#3"}]}
        args = argparse.Namespace(
            query="q", limit=5, kind="library", source="biblical", mode=None,
            semantic=False, project=None, since=None, until=None,
            session_id=None, harness=None)
        buf = io.StringIO()
        with mock.patch("khipu.embed.hybrid_search", return_value=payload) as hs, \
                mock.patch("khipu.query_log.log_query"), contextlib.redirect_stdout(buf):
            rc = cli.cmd_search(args)
        self.assertEqual(rc, 0)
        self.assertEqual(hs.call_args.kwargs["kind"], "library")
        self.assertEqual(hs.call_args.kwargs["source"], "biblical")
        self.assertEqual(json.loads(buf.getvalue()), payload)

    def test_an_unreachable_hub_refuses_library_search_instead_of_serving_memory(self):
        from khipu import cli

        args = argparse.Namespace(
            query="q", limit=5, kind="library", source=None, mode=None, semantic=False,
            project=None, since=None, until=None, session_id=None, harness=None)
        buf = io.StringIO()
        with mock.patch("khipu.embed.hybrid_search", side_effect=RuntimeError("refused")), \
                mock.patch("khipu.hub_snapshot.hub_connection_failed", return_value=True), \
                mock.patch("khipu.hub_snapshot.search_stale_payload") as stale, \
                contextlib.redirect_stdout(buf):
            rc = cli.cmd_search(args)
        self.assertEqual(rc, 2)
        stale.assert_not_called()


class McpTest(unittest.TestCase):
    def test_the_tool_descriptions_mention_libraries(self):
        from khipu.mcp_server import TOOLS

        tools = {t["name"]: t for t in TOOLS}
        search = tools["khipu_search"]
        self.assertIn("kind='library'", search["description"])
        self.assertIn("library:<name>:<doc>#<chunk>", search["description"])
        self.assertIn("source", search["inputSchema"]["properties"])
        self.assertIn("library", search["inputSchema"]["properties"]["kind"]["description"])
        get = tools["khipu_get"]
        self.assertIn("library:<name>:<doc>#<chunk>", get["description"])
        self.assertIn("previous/next", get["description"])

    def test_the_server_instructions_mention_libraries(self):
        from khipu.recall_rule import RULE_MD

        self.assertIn('kind: "library"', RULE_MD)
        self.assertIn("library:<name>:<doc>#<chunk>", RULE_MD)
        self.assertIn("chunks either side", RULE_MD)

    def test_tool_search_accepts_kind_library_and_forwards_source(self):
        from khipu import mcp_server as srv

        with mock.patch("khipu.embed.hybrid_search",
                        return_value={"results": []}) as hs, \
                mock.patch("khipu.query_log.log_query"):
            srv._tool_search({"query": "q", "kind": "library", "source": "biblical",
                              "mode": "semantic"})
        self.assertEqual(hs.call_args.kwargs["kind"], "library")
        self.assertEqual(hs.call_args.kwargs["source"], "biblical")

    def test_tool_search_refuses_source_with_another_kind(self):
        from khipu import mcp_server as srv

        with self.assertRaises(ValueError):
            srv._tool_search({"query": "q", "kind": "episode", "source": "biblical"})

    def test_tool_search_does_not_fall_back_to_the_snapshot_for_libraries(self):
        from khipu import mcp_server as srv

        with mock.patch("khipu.embed.hybrid_search", side_effect=RuntimeError("refused")), \
                mock.patch("khipu.hub_snapshot.hub_connection_failed", return_value=True), \
                mock.patch("khipu.hub_snapshot.search_stale_payload") as stale:
            with self.assertRaises(ValueError):
                srv._tool_search({"query": "q", "kind": "library"})
        stale.assert_not_called()

    def test_tool_get_resolves_library_ids(self):
        from khipu import mcp_server as srv

        with mock.patch.object(ls, "get", return_value={"kind": "library"}) as g, \
                mock.patch("khipu.activity.episode_detail") as ep, \
                mock.patch("khipu.activity.topic_detail") as tp:
            out = srv._tool_get({"id": "library:biblical:7#3"})
        self.assertEqual(out["kind"], "library")
        g.assert_called_once_with("library:biblical:7#3")
        ep.assert_not_called()
        tp.assert_not_called()

    def test_tool_get_kind_library_with_a_malformed_id_is_a_value_error(self):
        from khipu import mcp_server as srv

        with self.assertRaises(ValueError):
            srv._tool_get({"id": "not-a-library-id", "kind": "library"})


class FloorByProfileTest(unittest.TestCase):
    """relevance.cosine_floor_by_profile: a library profile's raw cosines sit on
    their own scale, so its rows are held to that profile's floor."""

    def _cfg(self, table, floor=None):
        rel = {"cosine_floor_by_profile": table}
        if floor is not None:
            rel["cosine_floor"] = floor
        return mock.patch("khipu.config.load_config", return_value={"relevance": rel})

    def test_the_profile_floor_wins_else_the_global_one(self):
        from khipu import relevance

        with self._cfg({PROFILE: 0.4}, floor=0.7):
            self.assertEqual(relevance.cosine_floor_for(PROFILE), 0.4)
            self.assertEqual(relevance.cosine_floor_for("gemini-embedding-001@768"), 0.7)
            self.assertEqual(relevance.cosine_floor_for(None), 0.7)

    def test_unusable_entries_are_ignored(self):
        from khipu import relevance

        bad = {PROFILE: 0, "x@1": True, "y@2": 5, "z@3": "0.5", "bad key": 0.5, "ok@4": 0.5}
        with self._cfg(bad):
            self.assertEqual(relevance.cosine_floor_by_profile(), {"ok@4": 0.5})

    def test_a_library_row_is_judged_by_its_own_profiles_floor(self):
        from khipu import relevance

        row = {"kind": "library", "cosine": 0.5, "profile": PROFILE, "lexical_hits": 0}
        self.assertFalse(relevance.is_evidence(row, 0.65, 2))
        self.assertTrue(relevance.is_evidence(row, 0.65, 2, {PROFILE: 0.45}))
        self.assertFalse(relevance.is_evidence(row, 0.65, 2, {"other@1": 0.1}))
        memory = {"kind": "episode", "cosine": 0.5, "lexical_hits": 0}
        self.assertFalse(relevance.is_evidence(memory, 0.65, 2, {PROFILE: 0.1}))

    def test_the_gate_reads_the_table_unless_given_an_explicit_floor(self):
        from khipu import relevance

        rows = [{"kind": "library", "cosine": 0.5, "profile": PROFILE, "lexical_hits": 0}]
        with self._cfg({PROFILE: 0.45}):
            kept, info = relevance.gate(rows, 3)
            self.assertEqual(len(kept), 1)
            dropped, _ = relevance.gate(rows, 3, floor=0.65)
            self.assertEqual(dropped, [])

    def test_the_default_search_applies_the_profile_floor_to_library_rows(self):
        weak = _cos_row(7, 9, 0.50, text="unrelated gardening notes")
        cur = FakeCur(cosine=[weak])
        with _hub(cur), self._cfg({}):
            self.assertEqual(_ids(em.hybrid_search("divine council", limit=5)), [])
        with _hub(cur), self._cfg({PROFILE: 0.45}):
            out = em.hybrid_search("divine council", limit=5)
        self.assertEqual(_ids(out), ["library:biblical:7#9"])
        self.assertEqual(out["results"][0]["profile"], PROFILE)


class FloorByProfileConfigTest(unittest.TestCase):
    """`khipu config --set/--unset relevance.cosine_floor_by_profile.<profile>`."""

    def setUp(self):
        import os
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def _cfg(self, **kw):
        from khipu import cli

        base = dict(set_capture_mode=None, set_gateway_url=None, set=None, unset=None)
        base.update(kw)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = cli.cmd_config(mock.Mock(**base))
        return rc, json.loads(out.getvalue())

    def _stored(self):
        from pathlib import Path

        return json.loads((Path(self.tmp.name) / "config.json").read_text())

    def test_set_stores_a_profile_id_with_at_and_dash_and_dot(self):
        from khipu import relevance

        for profile in (PROFILE, "voyage-3.5@1024", "gemini-embedding-001@768"):
            rc, p = self._cfg(set=[f"relevance.cosine_floor_by_profile.{profile}", "0.42"])
            self.assertEqual(rc, 0, profile)
            self.assertEqual(relevance.cosine_floor_by_profile()[profile], 0.42)
        self.assertEqual(self._stored()["relevance"]["cosine_floor_by_profile"][PROFILE], 0.42)
        self.assertEqual(p["relevance_cosine_floor_by_profile"]["voyage-3.5@1024"], 0.42)

    def test_it_sits_beside_the_global_floor_and_unset_removes_only_its_entry(self):
        self._cfg(set=["relevance.cosine_floor", "0.7"])
        self._cfg(set=[f"relevance.cosine_floor_by_profile.{PROFILE}", "0.4"])
        self._cfg(set=["relevance.cosine_floor_by_profile.other@8", "0.5"])
        rc, p = self._cfg(unset=f"relevance.cosine_floor_by_profile.{PROFILE}")
        self.assertEqual(rc, 0)
        self.assertEqual(self._stored()["relevance"],
                         {"cosine_floor": 0.7, "cosine_floor_by_profile": {"other@8": 0.5}})
        self._cfg(unset="relevance.cosine_floor_by_profile.other@8")
        self.assertEqual(self._stored()["relevance"], {"cosine_floor": 0.7})
        self._cfg(unset="relevance.cosine_floor")
        self.assertNotIn("relevance", self._stored())

    def test_khipu_config_shows_the_table(self):
        self._cfg(set=[f"relevance.cosine_floor_by_profile.{PROFILE}", "0.4"])
        rc, p = self._cfg()
        self.assertEqual(p["relevance_cosine_floor_by_profile"], {PROFILE: 0.4})
        self.assertEqual(p["relevance_cosine_floor"]["source"], "default")  # global untouched

    def test_bad_values_and_bad_ids_are_refused_and_nothing_is_written(self):
        from pathlib import Path

        for bad in ("0", "-0.1", "1.01", "abc", "", "nan", "inf"):
            rc, p = self._cfg(set=[f"relevance.cosine_floor_by_profile.{PROFILE}", bad])
            self.assertEqual(rc, 2, bad)
            self.assertFalse(p["ok"])
        for bad_key in ("relevance.cosine_floor_by_profile.", "relevance.cosine_floor_by_profile.a b",
                        "relevance.cosine_floor_by_profile.@@"):
            rc, p = self._cfg(set=[bad_key, "0.5"])
            self.assertEqual(rc, 2, bad_key)
        self.assertFalse((Path(self.tmp.name) / "config.json").exists())


class GatewayDocsTest(unittest.TestCase):
    def test_the_gateway_names_the_provider_keys_a_library_profile_needs(self):
        from khipu import gateway

        doc = gateway.__doc__
        self.assertIn("VOYAGE_API_KEY", doc)
        self.assertIn("library_embed:<profile>", doc)


if __name__ == "__main__":
    unittest.main()
