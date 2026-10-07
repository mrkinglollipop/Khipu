# --bypass-harness (sonnet lane) — authored directly by the dispatched on-sub
# agent (brief: do not delegate); no further agent to route to.
"""What the Embeddings screen asks of the CLI: estimate, the price table,
`embed profiles list|delete`, test-key, the query-timing log, and chunk-level
memory coverage (stale / samples). Fake cursors and a fake transport; no
database, no network, no Keychain."""
from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from khipu import cli, db, embed, embed_jobs, embed_ops, library, profiles
from khipu.profiles import ProfileSpec
from tests import test_library as tl
from tests import test_profiles as tp


def fake_conn(cur=None):
    conn = mock.MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cur if cur is not None else mock.MagicMock()
    return conn


class _Env(unittest.TestCase):
    def setUp(self):
        db._TABLE_COLUMNS_CACHE.clear()
        profiles.clear_learned()
        self.addCleanup(profiles.clear_learned)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": self.tmp.name})
        patch.start()
        self.addCleanup(patch.stop)

    def write_job(self, job_id, **over):
        row = {"job": job_id, "kind": "embed-backfill", "profile": "gemini-embedding-2@768",
               "space": "memory", "state": "done", "done": 50, "total": 50, "failed": 0,
               "started_at": "2026-10-07T10:00:00+00:00", "updated_at": "2026-10-07T10:01:40+00:00",
               "error": None, "pid": os.getpid()}
        row.update(over)
        d = Path(self.tmp.name) / "jobs"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{job_id}.json").write_text(json.dumps(row))


# ---- price table ------------------------------------------------------------------

class PriceTableTest(unittest.TestCase):
    def test_list_prices_by_provider_and_model(self):
        cases = [
            (ProfileSpec("gemini-embedding-2@768", "gemini", "gemini-embedding-2", 768), 0.20),
            (ProfileSpec("gemini-embedding-001@768", "gemini", "gemini-embedding-001", 768), 0.20),
            (ProfileSpec("voyage-3@1024", "voyage", "voyage-3", 1024), 0.06),
            (ProfileSpec("voyage-3-large@1024", "voyage", "voyage-3-large", 1024), 0.18),
            (ProfileSpec("voyage-3-lite@512", "voyage", "voyage-3-lite", 512), 0.02),
            (ProfileSpec("text-embedding-3-small@1536", "openai-compatible", "text-embedding-3-small",
                         1536, endpoint="https://api.openai.com"), 0.02),
            (ProfileSpec("text-embedding-3-large@3072", "openai-compatible", "text-embedding-3-large",
                         3072, endpoint="https://api.openai.com"), 0.13),
        ]
        for spec, price in cases:
            with self.subTest(spec=spec.id):
                got, note = profiles.price_info(spec)
                self.assertEqual(got, price)
                self.assertEqual(note, profiles.PRICE_TABLE_NOTE)
        self.assertIn("list prices as of 2026-10", profiles.PRICE_TABLE_NOTE)
        self.assertIn("informational", profiles.PRICE_TABLE_NOTE)

    def test_a_loopback_openai_compatible_server_is_free(self):
        for endpoint in ("http://localhost:11434", "http://127.0.0.1:1234", "http://[::1]:8080"):
            with self.subTest(endpoint=endpoint):
                spec = ProfileSpec("nomic@768", "openai-compatible", "nomic", 768, endpoint=endpoint)
                self.assertEqual(profiles.price_info(spec), (0.0, "local server, no charge"))

    def test_anything_else_is_unknown_never_guessed(self):
        for spec in (
            ProfileSpec("voyage-3.5@1024", "voyage", "voyage-3.5", 1024),
            ProfileSpec("gemini-embedding-9@768", "gemini", "gemini-embedding-9", 768),
            ProfileSpec("text-embedding-3-small@1536", "openai-compatible", "text-embedding-3-small",
                        1536, endpoint="https://proxy.example.com"),
            ProfileSpec("custom@8", "openai-compatible", "custom", 8, endpoint="https://api.openai.com"),
        ):
            with self.subTest(spec=spec.id):
                self.assertEqual(profiles.price_info(spec), (None, "price unknown for this provider"))


# ---- estimate ---------------------------------------------------------------------

class MemoryEstimateTest(_Env):
    SOURCES = [("topic", "a", "A" * 400, "A"), ("episode", "7", "B" * 100, "")]

    def estimate(self, have=None, profile="gemini-embedding-2@768", **kw):
        with mock.patch("khipu.db.connect", return_value=fake_conn()), \
                mock.patch.object(embed, "_iter_sources", return_value=iter(self.SOURCES)), \
                mock.patch.object(embed, "_existing_hashes", return_value=have or {}):
            return embed_ops.estimate(profile, "memory", **kw)

    def plan(self):
        with mock.patch.object(embed, "_iter_sources", return_value=iter(self.SOURCES)):
            return embed.memory_chunk_plan(None)

    def test_nothing_embedded_counts_every_chunk_and_prices_it(self):
        out = self.estimate()
        self.assertEqual((out["chunks"], out["chars"], out["approx_tokens"]), (2, 500, 125))
        self.assertAlmostEqual(out["price_usd"], 125 / 1_000_000 * 0.20, places=9)
        self.assertEqual(out["price_note"], profiles.PRICE_TABLE_NOTE)
        self.assertEqual((out["profile"], out["provider"], out["space"]),
                         ("gemini-embedding-2@768", "gemini", "memory"))
        self.assertEqual((out["approx_seconds"], out["rate_source"]), (None, None))
        self.assertEqual(
            set(out),
            {"profile", "provider", "space", "chunks", "chars", "approx_tokens", "price_usd",
             "price_note", "approx_seconds", "rate_source"},
        )

    def test_tokens_round_up(self):
        # --bypass-harness (sonnet lane): dispatched agent, no delegation.
        sources = [("topic", "a", "A" * 5, "A")]
        with mock.patch("khipu.db.connect", return_value=fake_conn()), \
                mock.patch.object(embed, "_iter_sources", return_value=iter(sources)), \
                mock.patch.object(embed, "_existing_hashes", return_value={}):
            out = embed_ops.estimate("gemini-embedding-2@768", "memory")
        self.assertEqual((out["chars"], out["approx_tokens"]), (5, 2))

    def test_up_to_date_chunks_are_not_counted_stale_ones_are(self):
        plan = self.plan()
        k1, k2 = list(plan)
        out = self.estimate(have={k1: plan[k1][0], k2: "hash-of-older-text"})
        self.assertEqual(out["chunks"], 1)
        self.assertEqual(out["chars"], plan[k2][1])
        done = self.estimate(have={k1: plan[k1][0], k2: plan[k2][0]})
        self.assertEqual((done["chunks"], done["chars"], done["approx_tokens"], done["price_usd"]),
                         (0, 0, 0, 0.0))

    def test_a_profile_with_no_known_price_says_so(self):
        profiles.register_spec(ProfileSpec("mystery@4", "openai-compatible", "mystery", 4,
                                           endpoint="https://example.com"))
        out = self.estimate(profile="mystery@4")
        self.assertIsNone(out["price_usd"])
        self.assertEqual(out["price_note"], "price unknown for this provider")

    def test_a_local_server_is_free(self):
        profiles.register_spec(ProfileSpec("nomic@4", "openai-compatible", "nomic", 4,
                                           endpoint="http://localhost:11434"))
        out = self.estimate(profile="nomic@4")
        self.assertEqual((out["price_usd"], out["price_note"]), (0.0, "local server, no charge"))

    def test_time_comes_from_the_measured_rate_of_finished_jobs(self):
        # 50 chunks in 100 s = 0.5 chunks/s; 2 chunks to do = 4 s
        self.write_job("m1", done=50)
        out = self.estimate()
        self.assertEqual(out["approx_seconds"], 4)
        self.assertEqual(out["rate_source"], "1 finished job for this profile")

    def test_an_unknown_space_is_refused(self):
        with self.assertRaises(ValueError):
            embed_ops.estimate("gemini-embedding-2@768", "elsewhere")


class MeasuredRateTest(_Env):
    def test_only_finished_jobs_of_this_profile_big_enough_to_mean_something(self):
        self.write_job("tiny", done=5)                                    # below MIN_RATE_CHUNKS
        self.write_job("other", profile="voyage-3@1024", done=500)        # other profile
        self.write_job("cancelled", state="cancelled", done=500)          # not finished clean
        self.assertEqual(embed_ops.measured_rate("gemini-embedding-2@768"), (None, None))
        self.write_job("good1", done=100)                                 # 100 chunks / 100 s
        self.write_job("good2", done=300, started_at="2026-10-07T11:00:00+00:00",
                       updated_at="2026-10-07T11:01:40+00:00")            # 300 chunks / 100 s
        rate, source = embed_ops.measured_rate("gemini-embedding-2@768")
        self.assertAlmostEqual(rate, 2.0)
        self.assertEqual(source, "2 finished jobs for this profile")


class OpsCursor(tl.FakeCursor):
    """FakeHub's cursor plus the library estimate's COUNT/SUM statement."""

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        if s.startswith("SELECT COUNT(*), COALESCE(SUM(LENGTH(c.chunk_text)), 0)"):
            self.h.executed.append((s, tuple(params)))
            profile, source = params
            with_stale = "e.document IS NULL OR" in s
            texts = []
            for _doc, _idx, text, hsh, emb in self.h.state(source, profile):
                is_stale = emb is not None and emb[1] is not None and emb[1] != hsh
                if emb is None or (with_stale and is_stale):
                    texts.append(text)
            self._r = [(len(texts), sum(len(t) for t in texts))]
            return
        super().execute(sql, params)


class OpsHub(tl.FakeHub):
    def cursor(self):
        return OpsCursor(self)


def _hub_with_library():
    hub = OpsHub()
    hub.sources["lib1"] = {"root": "/x", "profile": tl.TINY.id, "enabled": True}
    hub.docs[1] = {"source": "lib1", "rel_path": "a.txt", "title": "A", "author": None,
                   "tags": [], "bytes": 0, "content_hash": "d"}
    hub.chunks[(1, 0)] = ("x" * 400, "h0")
    hub.chunks[(1, 1)] = ("y" * 100, "h1")
    return hub


class LibraryEstimateTest(_Env):
    def run_estimate(self, hub, profile=tl.TINY.id, **kw):
        with mock.patch("khipu.db.connect", return_value=hub):
            return embed_ops.estimate(profile, "library:lib1", **kw)

    def test_counts_the_chunks_with_no_vector_under_the_asked_profile(self):
        out = self.run_estimate(_hub_with_library())
        self.assertEqual((out["space"], out["chunks"], out["chars"], out["approx_tokens"]),
                         ("library:lib1", 2, 500, 125))
        self.assertIsNone(out["price_usd"])  # voyage-t is not in the price table

    def test_the_profile_asked_for_need_not_be_the_librarys_own(self):
        hub = _hub_with_library()
        hub.profile_rows["voyage-3@4"] = ("voyage", "voyage-3", 4, "l2", None)
        hub.embs[(tl.TINY.id, 1, 0)] = ("[0,0,0,0]", "h0")  # embedded under its own profile
        out = self.run_estimate(hub, profile="voyage-3@4")
        self.assertEqual(out["chunks"], 2)
        self.assertAlmostEqual(out["price_usd"], 125 / 1_000_000 * 0.06, places=9)

    def test_stale_chunks_count_only_with_the_flag(self):
        hub = _hub_with_library()
        hub.embs[(tl.TINY.id, 1, 0)] = ("[0,0,0,0]", "older-hash")
        self.assertEqual(self.run_estimate(hub)["chunks"], 1)
        with_stale = self.run_estimate(hub, stale=True)
        self.assertEqual((with_stale["chunks"], with_stale["chars"]), (2, 500))

    def test_an_unknown_library_is_a_library_error(self):
        hub = OpsHub()
        with mock.patch("khipu.db.connect", return_value=hub), self.assertRaises(library.LibraryError):
            embed_ops.estimate(tl.TINY.id, "library:ghost")


# ---- profiles list ----------------------------------------------------------------

class ListDetailedTest(_Env):
    def test_rows_carry_use_coverage_key_timing_and_price(self):
        cur = tp.FakeCur(
            rows={"a@1": ("gemini", "a", 1, "l2", None), "b@2": ("voyage", "b", 2, "l2", None)},
            counts={("memory_embeddings", "a@1"): 5},
        )
        plan = {("topic", "x", 0): ("h", 10), ("topic", "y", 0): ("h2", 20)}
        have = {"a@1": {("topic", "x", 0): "h"}, "b@2": {}}
        summary = [{"name": "lib1", "profile": "b@2", "enabled": True, "documents": 2, "chunks": 10,
                    "embedded": 8, "missing": 2, "stale": 1, "pct": 70.0}]
        with mock.patch.object(embed, "memory_chunk_plan", return_value=plan), \
                mock.patch.object(embed, "_existing_hashes_many",
                                  side_effect=lambda c, ps: {p: have[p] for p in ps}), \
                mock.patch.object(library, "summary", return_value=summary), \
                mock.patch.object(embed, "median_query_ms", return_value={"a@1": 123}), \
                mock.patch.object(profiles, "key_present", side_effect=lambda p: p == "gemini"):
            rows = {r["id"]: r for r in embed_ops.list_detailed(fake_conn(cur))}
        a, b = rows["a@1"], rows["b@2"]
        self.assertEqual(a["in_use_by"], ["memory"])
        self.assertEqual(b["in_use_by"], ["lib1"])
        self.assertEqual({k: a["coverage"]["memory"][k] for k in ("total", "embedded", "missing", "stale")},
                         {"total": 2, "embedded": 1, "missing": 1, "stale": 0})
        self.assertEqual({k: b["coverage"]["memory"][k] for k in ("total", "embedded", "missing", "stale")},
                         {"total": 2, "embedded": 0, "missing": 2, "stale": 0})
        self.assertEqual({k: b["coverage"]["library:lib1"][k] for k in ("total", "embedded", "missing", "stale")},
                         {"total": 10, "embedded": 8, "missing": 2, "stale": 1})
        self.assertNotIn("library:lib1", a["coverage"])
        self.assertEqual((a["key_present"], b["key_present"]), (True, False))
        self.assertEqual((a["median_query_ms_24h"], b["median_query_ms_24h"]), (123, None))
        self.assertEqual((a["price_per_million_usd"], b["price_per_million_usd"]), (None, None))
        self.assertEqual(a["rows"]["memory"], 5)  # the existing fields are still there
        # --bypass-harness (sonnet lane): dispatched agent, no delegation.
        for r in rows.values():
            self.assertEqual([k for k in r if "key" in k], ["key_present"])  # presence, never a value

    def test_a_local_profile_lists_a_zero_price(self):
        cur = tp.FakeCur(rows={"nomic@4": ("openai-compatible", "nomic", 4, "l2", "http://localhost:11434")})
        with mock.patch.object(embed, "memory_chunk_plan", return_value={}), \
                mock.patch.object(embed, "_existing_hashes_many", return_value={}), \
                mock.patch.object(library, "summary", return_value=[]), \
                mock.patch.object(embed, "median_query_ms", return_value={}), \
                mock.patch.object(profiles, "key_present", return_value=False):
            row = embed_ops.list_detailed(fake_conn(cur))[0]
        self.assertEqual(row["price_per_million_usd"], 0.0)
        self.assertEqual(row["coverage"]["memory"]["total"], 0)


class ListDetailedStatementsTest(_Env):
    """The screen's read costs the same number of statements for one profile
    or many, and its output shape is the one the screen already reads."""

    def run_list(self, n_profiles):
        db._TABLE_COLUMNS_CACHE.clear()  # the schema probe is once per process, not per profile
        rows = {f"p{i}@{i + 1}": ("voyage", f"p{i}", i + 1, "l2", None) for i in range(n_profiles)}
        cur = tp.FakeCur(rows=rows)
        plan = {("topic", "x", 0): ("h", 10), ("topic", "y", 0): ("h2", 20)}
        with mock.patch.object(embed, "memory_chunk_plan", return_value=plan), \
                mock.patch.object(library, "summary", return_value=[]), \
                mock.patch.object(embed, "median_query_ms", return_value={}), \
                mock.patch.object(profiles, "key_present", return_value=False):
            out = embed_ops.list_detailed(fake_conn(cur))
        return out, [sql for sql, _p in cur.executed]

    def test_statement_count_is_the_same_for_one_profile_and_for_many(self):
        out1, sql1 = self.run_list(1)
        out6, sql6 = self.run_list(6)
        self.assertEqual((len(out1), len(out6)), (1, 6))
        self.assertEqual(len(sql1), len(sql6))
        hashes = [s for s in sql6 if "chunk_idx, content_hash FROM memory_embeddings" in s]
        self.assertEqual(len(hashes), 1)

    def test_memory_hashes_of_every_profile_come_back_from_one_statement(self):
        cur = mock.MagicMock()
        cur.fetchall.return_value = [("a@1", "topic", "x", 0, "h"), ("b@2", "episode", "7", 0, "g"),
                                     ("a@1", "topic", "y", 0, "i")]
        got = embed._existing_hashes_many(cur, ["a@1", "b@2", "c@3"])
        self.assertEqual(cur.execute.call_count, 1)
        self.assertEqual(got, {"a@1": {("topic", "x", 0): "h", ("topic", "y", 0): "i"},
                               "b@2": {("episode", "7", 0): "g"}, "c@3": {}})
        self.assertEqual(embed._existing_hashes_many(cur, []), {})
        self.assertEqual(cur.execute.call_count, 1)  # nothing asked, nothing read

    def test_gaps_for_many_profiles_equal_the_per_profile_answer(self):
        plan = {("topic", "x", 0): ("h", 10), ("topic", "y", 0): ("h2", 20), ("topic", "z", 0): ("h3", 5)}
        have = {"a@1": {("topic", "x", 0): "h", ("topic", "y", 0): "old"}, "b@2": {}}
        many = embed.memory_gaps_many(None, ["a@1", "b@2"], plan, have)
        for pid in ("a@1", "b@2"):
            with mock.patch.object(embed, "_existing_hashes", return_value=have[pid]):
                _p, missing, stale = embed.memory_gaps(None, pid, plan)
            self.assertEqual(many[pid], (missing, stale))

    def test_the_row_keeps_every_field_the_screen_reads(self):
        (row,), _sql = self.run_list(1)
        self.assertEqual(
            set(row),
            {"id", "provider", "model", "dim", "normalize", "endpoint", "is_active", "rows", "index",
             "in_use_by", "coverage", "key_present", "median_query_ms_24h", "price_per_million_usd"})
        self.assertEqual(set(row["coverage"]["memory"]),
                         {"total", "embedded", "missing", "stale", "pct"})


class CachedMemoryPlanTest(_Env):
    """``memory_chunk_plan(cached=True)``: same plan, only changed rows re-read."""

    SOURCES = [("episode", "7", "E" * 50, ""), ("episode", "9", "F" * 70, ""),
               ("topic", "a", "A" * 400, "A"), ("topic", "b", "B" * 10, "B")]

    def setUp(self):
        super().setUp()
        self.fp = {(k, r): "v1" for k, r, _t, _ti in self.SOURCES}
        self.sources = list(self.SOURCES)
        self.reads: list = []
        self.cache_path = Path(self.tmp.name) / "plan.json"
        for patch in (
            mock.patch.object(embed, "_plan_cache_file", return_value=self.cache_path),
            mock.patch.object(embed, "_row_fingerprints", side_effect=self._fingerprints),
            mock.patch.object(embed, "_iter_sources", side_effect=self._iter),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def _fingerprints(self, cur):
        return [(k, r, self.fp[(k, r)]) for k, r, _t, _ti in self.sources]

    def _iter(self, cur, kind=None, refs=None):
        rows = [x for x in self.sources
                if x[2] and (kind is None or x[0] == kind) and (refs is None or x[1] in refs)]
        self.reads.append((kind, None if refs is None else list(refs)))
        return iter(rows)

    def uncached(self):
        self.reads.clear()
        plan = embed.memory_chunk_plan(None)
        self.reads.clear()
        return plan

    def test_the_cached_plan_is_the_uncached_plan_in_the_same_order(self):
        want = self.uncached()
        got = embed.memory_chunk_plan(None, cached=True)
        self.assertEqual(got, want)
        self.assertEqual(list(got), list(want))

    def test_a_second_look_at_unchanged_rows_reads_no_text(self):
        embed.memory_chunk_plan(None, cached=True)
        self.reads.clear()
        again = embed.memory_chunk_plan(None, cached=True)
        self.assertEqual(self.reads, [])
        self.assertEqual(again, self.uncached())

    def test_only_a_row_whose_fingerprint_moved_is_read_again(self):
        embed.memory_chunk_plan(None, cached=True)
        self.reads.clear()
        self.sources[2] = ("topic", "a", "A" * 9000, "A")     # grows to two chunks
        self.fp[("topic", "a")] = "v2"
        got = embed.memory_chunk_plan(None, cached=True)
        self.assertEqual(self.reads, [("topic", ["a"])])
        self.assertEqual(got, self.uncached())
        self.assertGreater(len([k for k in got if k[1] == "a"]), 1)

    def test_a_row_that_left_the_set_leaves_the_plan(self):
        embed.memory_chunk_plan(None, cached=True)
        self.sources = [x for x in self.sources if x[1] != "9"]
        got = embed.memory_chunk_plan(None, cached=True)
        self.assertNotIn(("episode", "9", 0), got)
        self.assertEqual(got, self.uncached())
        again = embed.memory_chunk_plan(None, cached=True)
        self.assertEqual(again, got)

    def test_a_row_with_no_text_costs_one_read_and_adds_no_chunks(self):
        self.sources.append(("topic", "z", "", "Z"))
        self.fp[("topic", "z")] = "v1"
        embed.memory_chunk_plan(None, cached=True)
        self.reads.clear()
        got = embed.memory_chunk_plan(None, cached=True)
        self.assertEqual(self.reads, [])
        self.assertFalse([k for k in got if k[1] == "z"])

    def test_a_changed_chunker_or_a_damaged_file_means_a_full_read_not_a_wrong_plan(self):
        embed.memory_chunk_plan(None, cached=True)
        for damage in ('{"stamp": "some-older-chunker", "rows": {}}', "not json", "[]"):
            with self.subTest(damage=damage):
                self.cache_path.write_text(damage)
                self.reads.clear()
                got = embed.memory_chunk_plan(None, cached=True)
                self.assertEqual(self.reads, [("episode", ["7", "9"]), ("topic", ["a", "b"])])
                self.assertEqual(got, self.uncached())

    def test_an_unwritable_cache_only_costs_speed(self):
        with mock.patch.object(embed, "_plan_cache_file", return_value=Path("/proc/nope/x/plan.json")):
            self.assertEqual(embed.memory_chunk_plan(None, cached=True), self.uncached())

    def test_the_uncached_call_never_touches_the_file(self):
        embed.memory_chunk_plan(None)
        self.assertFalse(self.cache_path.exists())


class RowFingerprintTest(_Env):
    def test_reads_a_version_stamp_not_text_and_keeps_plan_order(self):
        cur = mock.MagicMock()
        cur.fetchall.return_value = [("episode", 7, None, "501"), ("topic", None, "a", "77")]
        with mock.patch("khipu.db.has_columns", return_value=True):
            got = embed._row_fingerprints(cur)
        self.assertEqual(got, [("episode", "7", "501"), ("topic", "a", "77")])
        (sql,), _ = cur.execute.call_args
        self.assertIn("xmin", sql)
        self.assertNotIn("summary", sql)
        self.assertNotIn("body", sql)
        self.assertIn("ORDER BY kind, id, slug", sql)

    def test_iter_sources_narrows_to_the_refs_asked_for(self):
        cur = mock.MagicMock()
        cur.fetchall.return_value = []
        with mock.patch("khipu.db.has_columns", return_value=True):
            list(embed._iter_sources(cur, kind="episode", refs=["7", "9"]))
            list(embed._iter_sources(cur, kind="topic", refs=["a"]))
        (ep_sql, ep_args), (tp_sql, tp_args) = [c.args for c in cur.execute.call_args_list]
        self.assertIn("id = ANY(%s)", ep_sql)
        self.assertEqual(ep_args, ([7, 9],))
        self.assertIn("slug = ANY(%s)", tp_sql)
        self.assertEqual(tp_args, (["a"],))

    def test_iter_sources_without_refs_reads_everything_as_before(self):
        cur = mock.MagicMock()
        cur.fetchall.return_value = []
        with mock.patch("khipu.db.has_columns", return_value=True):
            list(embed._iter_sources(cur, kind="topic"))
        self.assertNotIn("ANY", cur.execute.call_args.args[0])


class KeyPresenceTest(unittest.TestCase):
    def test_presence_only_never_the_value(self):
        from khipu import keychain

        with mock.patch.object(keychain, "resolve_gemini_key", return_value="g-secret-value"), \
                mock.patch.object(keychain, "resolve_voyage_key", side_effect=RuntimeError("No Voyage key")), \
                mock.patch.object(keychain, "get_openai_compat_key", return_value=None):
            self.assertIs(profiles.key_present("gemini"), True)
            self.assertIs(profiles.key_present("voyage"), False)
            self.assertIs(profiles.key_present("openai-compatible"), False)
            self.assertIs(profiles.key_present("nonsense"), False)


# ---- profiles delete --------------------------------------------------------------

class DelCur(tp.FakeCur):
    """FakeCur plus the statements delete_profile issues."""

    def __init__(self, *a, active=None, libs=None, row_counts=None, **kw):
        kw.setdefault("tables", ("memory_embeddings", "library_embeddings", "memory_query_cache",
                                 "library_sources"))
        super().__init__(*a, **kw)
        self.active = active
        self.libs = libs or {}
        self.row_counts = row_counts or {}
        self.rowcount = 0

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        if s.startswith("SELECT is_active FROM embedding_profiles"):
            self.executed.append((s, tuple(params)))
            self._result = [(params[0] == self.active,)]
        elif s.startswith("SELECT name FROM library_sources WHERE profile"):
            self.executed.append((s, tuple(params)))
            self._result = [(n,) for n in self.libs.get(params[0], [])]
        elif s.startswith("DELETE FROM"):
            self.executed.append((s, tuple(params)))
            table = s.split()[2]
            self.rowcount = self.row_counts.get(table, 0)
            if table == "embedding_profiles":
                self.rows.pop(params[0], None)
            self._result = []
        elif s.startswith(("SAVEPOINT", "RELEASE", "ROLLBACK TO", "DROP INDEX")):
            self.executed.append((s, tuple(params)))
            self._result = []
        else:
            super().execute(sql, params)

    def statements(self, prefix):
        return [s for s, _ in self.executed if s.startswith(prefix)]


ROWS = {"a@1": ("gemini", "a", 1, "l2", None), "b@2": ("voyage", "b", 2, "l2", None)}
COUNTS = {"memory_embeddings": 5, "library_embeddings": 7, "memory_query_cache": 2}


class DeleteProfileTest(_Env):
    def cur(self, **kw):
        return DelCur(rows=dict(ROWS), row_counts=COUNTS, **kw)

    def test_the_active_memory_profile_is_refused(self):
        cur = self.cur(active="b@2")
        with self.assertRaises(ValueError) as cm:
            profiles.delete_profile(cur, "b@2")
        self.assertIn("in use by memory", str(cm.exception))
        self.assertEqual(cur.statements("DELETE"), [])

    def test_a_profile_a_library_uses_is_refused_and_the_libraries_are_named(self):
        cur = self.cur(libs={"b@2": ["biblical", "notes"]})
        with self.assertRaises(ValueError) as cm:
            profiles.delete_profile(cur, "b@2")
        self.assertIn("biblical, notes", str(cm.exception))
        self.assertEqual(cur.statements("DELETE"), [])

    def test_memory_and_a_library_are_both_named(self):
        cur = self.cur(active="b@2", libs={"b@2": ["biblical"]})
        with self.assertRaises(ValueError) as cm:
            profiles.delete_profile(cur, "b@2")
        self.assertIn("memory, biblical", str(cm.exception))

    def test_an_unknown_profile_is_refused(self):
        with self.assertRaises(ValueError):
            profiles.delete_profile(self.cur(), "ghost@3")

    def test_an_unused_profile_loses_vectors_cache_indexes_then_its_row(self):
        cur = self.cur(active="a@1")
        out = profiles.delete_profile(cur, "b@2")
        self.assertEqual(out["profile"], "b@2")
        self.assertEqual(out["deleted_rows"], {"memory": 5, "library": 7, "query_cache": 2, "total": 14})
        self.assertNotIn("b@2", cur.rows)
        self.assertIn("a@1", cur.rows)
        deletes = cur.statements("DELETE")
        self.assertEqual([d.split()[2] for d in deletes],
                         ["library_embeddings", "memory_embeddings", "memory_query_cache",
                          "embedding_profiles"])
        drops = cur.statements("DROP INDEX")
        self.assertEqual(drops, ["DROP INDEX IF EXISTS public.idx_memory_hnsw_b_2",
                                 "DROP INDEX IF EXISTS public.idx_library_hnsw_b_2"])
        self.assertEqual(out["dropped_indexes"], ["idx_memory_hnsw_b_2", "idx_library_hnsw_b_2"])
        # the row goes last: nothing may still point at it
        order = [s for s, _ in cur.executed if s.startswith(("DELETE", "DROP"))]
        self.assertTrue(order[-1].startswith("DELETE FROM embedding_profiles"))

    def test_a_hub_without_the_optional_tables_still_deletes(self):
        cur = DelCur(rows=dict(ROWS), row_counts=COUNTS, tables=("memory_embeddings",))
        out = profiles.delete_profile(cur, "b@2")
        self.assertEqual(out["deleted_rows"], {"memory": 5, "library": 0, "query_cache": 0, "total": 5})

    def test_a_failed_drop_is_swallowed_under_a_savepoint(self):
        cur = self.cur()
        real = cur.execute

        def boom(sql, params=()):
            if " ".join(sql.split()).startswith("DROP INDEX"):
                raise RuntimeError("must be owner of index")
            return real(sql, params)

        cur.execute = boom
        out = profiles.delete_profile(cur, "b@2")
        self.assertEqual(out["profile"], "b@2")
        self.assertTrue(cur.statements("ROLLBACK TO SAVEPOINT"))


class DeleteCliTest(_Env):
    def run_cli(self, cur, **kw):
        conn = fake_conn(cur)
        args = argparse.Namespace(embed_cmd="profiles", profiles_cmd="delete", id="b@2", yes=False)
        for k, v in kw.items():
            setattr(args, k, v)
        with mock.patch("khipu.db.connect", return_value=conn), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = cli.cmd_embed(args)
        return rc, json.loads(out.getvalue()), conn

    def cur(self, **kw):
        counts = {("memory_embeddings", "b@2"): 5, ("library_embeddings", "b@2"): 7}
        return DelCur(rows=dict(ROWS), counts=counts, row_counts=COUNTS, **kw)

    def test_without_yes_it_refuses_and_names_the_row_counts(self):
        cur = self.cur()
        rc, out, conn = self.run_cli(cur)
        self.assertEqual((rc, out["ok"]), (2, False))
        self.assertIn("--yes", out["error"])
        self.assertIn("5 memory vectors", out["error"])
        self.assertIn("7 library vectors", out["error"])
        self.assertEqual(cur.statements("DELETE"), [])
        conn.commit.assert_not_called()

    def test_in_use_is_refused_even_with_yes_and_names_the_users(self):
        cur = self.cur(libs={"b@2": ["biblical"]})
        rc, out, conn = self.run_cli(cur, yes=True)
        self.assertEqual(rc, 2)
        self.assertIn("biblical", out["error"])
        self.assertEqual(cur.statements("DELETE"), [])
        conn.commit.assert_not_called()

    def test_with_yes_it_deletes_commits_and_prints_the_receipt(self):
        cur = self.cur()
        rc, out, conn = self.run_cli(cur, yes=True)
        self.assertEqual(rc, 0)
        self.assertEqual((out["ok"], out["profile"], out["deleted_rows"]["total"]), (True, "b@2", 14))
        conn.commit.assert_called_once()

    def test_an_unknown_profile_is_exit_2(self):
        rc, out, _ = self.run_cli(self.cur(), id="ghost@3", yes=True)
        self.assertEqual((rc, out["ok"]), (2, False))


# ---- test-key ---------------------------------------------------------------------

def _voyage_body(vec):
    return json.dumps({"data": [{"embedding": vec, "index": 0}]}).encode()


class CheckKeyTest(unittest.TestCase):
    def setUp(self):
        for patch in (
            mock.patch.object(embed, "_budget_take"),
            mock.patch.object(embed.time, "sleep"),
            mock.patch.object(embed, "_voyage_key", return_value="vk-secret-1234"),
            mock.patch.object(embed, "_gemini_key", return_value="gk-secret-9999"),
            mock.patch.object(embed, "_openai_compat_key", return_value=None),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(embed.set_transport, None)

    def use(self, *script):
        calls = []
        queue = list(script)

        def transport(url, data, headers, timeout):
            calls.append((url, json.loads(data.decode()), dict(headers), timeout))
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        embed.set_transport(transport)
        return calls

    def test_voyage_reports_ok_ms_and_the_discovered_dim(self):
        calls = self.use(_voyage_body([0.1, 0.2, 0.3]))
        out = embed_ops.check_key("voyage")
        self.assertTrue(out["ok"])
        self.assertEqual(out["dim"], 3)
        self.assertIsInstance(out["ms"], int)
        self.assertEqual(calls[0][1]["model"], "voyage-3")
        self.assertEqual(calls[0][1]["input_type"], "query")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("vk-secret", json.dumps(out))

    def test_gemini_and_a_local_openai_compatible_server(self):
        calls = self.use(json.dumps({"embeddings": [{"values": [0.5] * 5}]}).encode())
        out = embed_ops.check_key("gemini")
        self.assertEqual((out["ok"], out["dim"]), (True, 5))
        self.assertIn("gemini-embedding-2", calls[0][0])
        calls = self.use(_voyage_body([0.1, 0.2]))
        out = embed_ops.check_key("openai-compatible", endpoint="http://localhost:11434/v1", model="nomic")
        self.assertEqual((out["ok"], out["dim"]), (True, 2))
        self.assertEqual(calls[0][0], "http://localhost:11434/v1/embeddings")
        self.assertNotIn("Authorization", calls[0][2])

    def test_a_rejected_key_is_reported_without_the_key(self):
        err = urllib.error.HTTPError("https://x", 401, "err", {}, io.BytesIO(b"bad key vk-secret-1234 rejected"))
        self.use(err)
        out = embed_ops.check_key("voyage")
        self.assertFalse(out["ok"])
        self.assertIn("401", out["error"])
        self.assertNotIn("vk-secret-1234", json.dumps(out))
        self.assertIn("***", out["error"])

    def test_a_missing_key_is_a_clean_error(self):
        with mock.patch.object(embed, "_voyage_key", side_effect=RuntimeError("No Voyage key.")):
            out = embed_ops.check_key("voyage")
        self.assertEqual((out["ok"], "No Voyage key" in out["error"]), (False, True))

    def test_a_network_failure_is_not_retried(self):
        calls = self.use(urllib.error.URLError("down"))
        out = embed_ops.check_key("voyage")
        self.assertFalse(out["ok"])
        self.assertEqual(len(calls), 1)

    def test_an_empty_answer_is_a_failure(self):
        self.use(json.dumps({"data": []}).encode())
        self.assertFalse(embed_ops.check_key("voyage")["ok"])

    def test_bad_arguments_never_reach_the_network(self):
        embed.set_transport(mock.Mock(side_effect=AssertionError("must not call")))
        for kw in (
            dict(provider="nope"),
            dict(provider="openai-compatible", model="m"),                               # no endpoint
            dict(provider="openai-compatible", endpoint="http://example.com", model="m"),  # plain http
            dict(provider="openai-compatible", endpoint="http://localhost:1"),            # no model
            dict(provider="gemini", endpoint="http://localhost:1"),                       # fixed endpoint
        ):
            with self.subTest(kw=kw):
                out = embed_ops.check_key(**kw)
                self.assertFalse(out["ok"])
                self.assertTrue(out["error"])


class TestKeyCliTest(unittest.TestCase):
    def test_exit_code_follows_ok_and_json_is_printed(self):
        for result, rc in (({"ok": True, "ms": 5, "dim": 3}, 0), ({"ok": False, "error": "x"}, 2)):
            with self.subTest(result=result):
                args = argparse.Namespace(embed_cmd="test-key", provider="voyage", endpoint=None, model=None)
                with mock.patch.object(embed_ops, "check_key", return_value=result) as ck, \
                        mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                    self.assertEqual(cli.cmd_embed(args), rc)
                self.assertEqual(json.loads(out.getvalue()), result)
                ck.assert_called_once_with("voyage", endpoint=None, model=None)


# ---- the other embed verbs --------------------------------------------------------

class EmbedVerbsCliTest(_Env):
    def run_cli(self, **kw):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = cli.cmd_embed(argparse.Namespace(**kw))
        return rc, json.loads(out.getvalue())

    def test_estimate_prints_the_receipt_and_refuses_with_exit_2(self):
        receipt = {"profile": "p@3", "chunks": 1}
        with mock.patch.object(embed_ops, "estimate", return_value=receipt) as est:
            rc, out = self.run_cli(embed_cmd="estimate", profile="p@3", space="library:b", stale=True)
        self.assertEqual((rc, out), (0, receipt))
        est.assert_called_once_with("p@3", "library:b", stale=True)
        with mock.patch.object(embed_ops, "estimate", side_effect=ValueError("unknown embedding profile")):
            rc, out = self.run_cli(embed_cmd="estimate", profile="x", space="memory", stale=False)
        self.assertEqual((rc, out["ok"]), (2, False))
        with mock.patch.object(embed_ops, "estimate", side_effect=library.LibraryError("no library")):
            rc, _ = self.run_cli(embed_cmd="estimate", profile="x", space="library:q", stale=False)
        self.assertEqual(rc, 2)

    def test_jobs_lists_and_clears(self):
        self.write_job("j1", state="done")
        self.write_job("j2", state="running")
        rc, out = self.run_cli(embed_cmd="jobs", clear=False)
        self.assertEqual((rc, sorted(j["job"] for j in out["jobs"])), (0, ["j1", "j2"]))
        rc, out = self.run_cli(embed_cmd="jobs", clear=True)
        self.assertEqual(out, {"ok": True, "removed": 1})
        self.assertEqual([j["job"] for j in embed_jobs.list_jobs()], ["j2"])

    def test_the_argparse_surface(self):
        p = cli.build_parser()
        ns = p.parse_args(["embed", "estimate", "--profile", "voyage-3@1024",
                           "--space", "library:biblical", "--stale"])
        self.assertEqual((ns.embed_cmd, ns.profile, ns.space, ns.stale),
                         ("estimate", "voyage-3@1024", "library:biblical", True))
        self.assertEqual(p.parse_args(["embed", "estimate", "--profile", "x"]).space, "memory")
        self.assertTrue(p.parse_args(["embed", "jobs", "--clear"]).clear)
        tk = p.parse_args(["embed", "test-key", "--provider", "openai-compatible",
                           "--endpoint", "http://localhost:11434", "--model", "nomic"])
        self.assertEqual((tk.provider, tk.endpoint, tk.model),
                         ("openai-compatible", "http://localhost:11434", "nomic"))
        d = p.parse_args(["embed", "profiles", "delete", "voyage-3@1024", "--yes"])
        self.assertEqual((d.profiles_cmd, d.id, d.yes), ("delete", "voyage-3@1024", True))
        bf = p.parse_args(["embed", "backfill", "--profile", "voyage-3@1024", "--stale"])
        self.assertTrue(bf.stale)
        self.assertFalse(bf.job)


# ---- the query-timing log ---------------------------------------------------------

class TimingLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "embed_timings.jsonl"
        patch = mock.patch.object(embed, "_timings_path", return_value=self.path)
        patch.start()
        self.addCleanup(patch.stop)

    def lines(self):
        return self.path.read_text().splitlines()

    def test_a_query_embedding_appends_ts_profile_ms_ok(self):
        with mock.patch.object(embed, "embed_batch", return_value=[[0.1, 0.2]]):
            vec = embed.embed_one("q", profile="p@3", input_type="query")
        self.assertEqual(vec, [0.1, 0.2])
        row = json.loads(self.lines()[0])
        self.assertEqual(set(row), {"ts", "profile", "ms", "ok"})
        self.assertEqual((row["profile"], row["ok"]), ("p@3", True))
        self.assertIsInstance(row["ms"], int)
        self.assertLess(abs(row["ts"] - time.time()), 5)

    def test_a_failure_is_logged_not_ok_and_still_raises(self):
        with mock.patch.object(embed, "embed_batch", side_effect=RuntimeError("embed HTTP 500")):
            with self.assertRaises(RuntimeError):
                embed.embed_one("q", profile="p@3", input_type="query")
        self.assertFalse(json.loads(self.lines()[0])["ok"])

    def test_batch_and_document_embedding_are_not_logged(self):
        with mock.patch.object(embed, "embed_batch", return_value=[[0.1]]):
            embed.embed_one("doc", profile="p@3")
            embed.embed_one("doc", profile="p@3", input_type="document")
        self.assertFalse(self.path.exists())

    def test_the_file_is_trimmed_to_the_last_500_only_once_it_passes_1000(self):
        for i in range(1000):
            embed._log_query_timing("p@3", float(i), True)
        self.assertEqual(len(self.lines()), 1000)
        embed._log_query_timing("p@3", 1000.0, True)
        lines = self.lines()
        self.assertEqual(len(lines), 500)
        self.assertEqual(json.loads(lines[-1])["ms"], 1000)   # newest kept
        self.assertEqual(json.loads(lines[0])["ms"], 501)     # oldest 501 dropped
        embed._log_query_timing("p@3", 1001.0, True)
        self.assertEqual(len(self.lines()), 501)

    def test_a_write_that_fails_never_raises(self):
        blocker = Path(self.tmp.name) / "file"
        blocker.write_text("x")
        with mock.patch.object(embed, "_timings_path", return_value=blocker / "child.jsonl"):
            embed._log_query_timing("p@3", 5.0, True)  # parent is a file: mkdir/open fail
        with mock.patch.object(embed, "_timings_path", side_effect=RuntimeError("no data dir")):
            embed._log_query_timing("p@3", 5.0, True)

    def test_median_is_over_the_last_day_of_successful_calls_per_profile(self):
        now = time.time()
        rows = [
            {"ts": now - 10, "profile": "a@1", "ms": 100, "ok": True},
            {"ts": now - 20, "profile": "a@1", "ms": 300, "ok": True},
            {"ts": now - 30, "profile": "a@1", "ms": 200, "ok": True},
            {"ts": now - 40, "profile": "a@1", "ms": 9999, "ok": False},          # failures excluded
            {"ts": now - 2 * 86400, "profile": "a@1", "ms": 1, "ok": True},       # too old
            {"ts": now - 10, "profile": "b@2", "ms": 50, "ok": True},
        ]
        self.path.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n{}\n")
        self.assertEqual(embed.median_query_ms("a@1"), {"a@1": 200})
        self.assertEqual(embed.median_query_ms("c@3"), {"c@3": None})
        self.assertEqual(embed.median_query_ms(), {"a@1": 200, "b@2": 50})

    def test_median_with_no_log_is_none_not_an_error(self):
        self.assertEqual(embed.median_query_ms("a@1"), {"a@1": None})


# ---- chunk-level memory coverage --------------------------------------------------

class MemoryCoverageTest(unittest.TestCase):
    SOURCES = [("topic", f"t{i}", f"text of topic {i}", f"T{i}") for i in range(8)]

    def gaps(self, have_fn):
        with mock.patch.object(embed, "_iter_sources", side_effect=lambda cur, kind=None: iter(self.SOURCES)), \
                mock.patch.object(embed, "_existing_hashes", return_value=have_fn()):
            return embed.memory_gaps(None, "p@3")

    def plan(self):
        with mock.patch.object(embed, "_iter_sources", side_effect=lambda cur, kind=None: iter(self.SOURCES)):
            return embed.memory_chunk_plan(None)

    def test_missing_and_stale_are_told_apart(self):
        plan = self.plan()
        keys = list(plan)
        have = {k: plan[k][0] for k in keys[:5]}          # 5 current
        have[keys[0]] = "older-hash"                       # one of them is stale
        have[("topic", "orphan", 0)] = "x"                 # a vector whose source is gone: not coverage
        p, missing, stale = self.gaps(lambda: have)
        self.assertEqual(set(missing), set(keys[5:]))
        self.assertEqual(stale, [keys[0]])
        fields = embed.memory_coverage_fields(p, missing, stale)
        self.assertEqual((fields["chunks"], fields["embedded"], fields["missing"], fields["stale"]),
                         (8, 5, 3, 1))
        self.assertLess(fields["pct"], 100.0)
        self.assertEqual(fields["pct"], embed.cov_pct(4, 8))

    def test_samples_are_distinct_refs_capped_at_five(self):
        plan = self.plan()
        p, missing, stale = self.gaps(lambda: {})
        fields = embed.memory_coverage_fields(p, missing, stale)
        self.assertEqual(len(fields["sample_missing"]), 5)
        self.assertEqual(fields["sample_missing"][0], "topic:t0")
        self.assertEqual(fields["sample_stale"], [])
        self.assertEqual(len(plan), 8)

    def test_full_coverage_reads_100_and_a_single_gap_never_does(self):
        plan = self.plan()
        have = {k: plan[k][0] for k in plan}
        p, missing, stale = self.gaps(lambda: have)
        self.assertEqual(embed.memory_coverage_fields(p, missing, stale)["pct"], 100.0)
        big = {("topic", f"t{i}", 0): ("h", 1) for i in range(5000)}
        self.assertEqual(embed.memory_coverage_fields(big, [("topic", "t0", 0)], [])["pct"], 99.9)

    def test_the_field_names_are_the_librarys_status_names(self):
        hub = _hub_with_library()
        status = library.source_status(hub, "lib1")  # --bypass-harness (sonnet lane): no delegation
        p, missing, stale = self.gaps(lambda: {})
        fields = embed.memory_coverage_fields(p, missing, stale)
        self.assertLessEqual(set(fields), set(status))


if __name__ == "__main__":
    unittest.main()
