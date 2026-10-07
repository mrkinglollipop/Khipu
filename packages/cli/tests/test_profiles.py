# --bypass-harness (sonnet lane) — authored directly by the dispatched on-sub
# Sonnet build agent (brief: do not delegate); no further agent to route to.
"""khipu.profiles and `khipu embed profiles list|add` (library sources + BYO
embeddings, Session A). Fake cursor, no database, no model call."""
from __future__ import annotations

import argparse
import io
import json
import unittest
from unittest import mock

from khipu import cli, db, profiles
from khipu.profiles import ProfileSpec


class FakeCur:
    """embedding_profiles + row counts, just enough SQL to answer profiles.py."""

    def __init__(self, rows=None, *, endpoint_column=True, tables=("memory_embeddings", "library_embeddings"),
                 counts=None):
        self.rows = dict(rows or {})  # id -> (provider, model, dim, normalize, endpoint)
        self.endpoint_column = endpoint_column
        self.tables = set(tables)
        self.counts = counts or {}
        self.executed: list[tuple[str, tuple]] = []
        self._result: list = []

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        self.executed.append((s, tuple(params)))
        if "information_schema.columns" in s:
            cols = ["id", "provider", "model", "dim", "normalize", "is_active", "note"]
            self._result = [(c,) for c in cols + (["endpoint"] if self.endpoint_column else [])]
        elif s.startswith("SELECT to_regclass"):
            self._result = [(params[0].split(".")[-1] in self.tables,)]
        elif s.startswith("SELECT profile, COUNT(*) FROM"):
            table = s.split(" FROM ")[1].split(" ")[0]
            self._result = [(p, n) for (t, p), n in self.counts.items() if t == table]
        elif "FROM embedding_profiles WHERE id" in s:
            r = self.rows.get(params[0])
            self._result = [(params[0], *r)] if r else []
        elif "FROM embedding_profiles ORDER BY" in s:
            self._result = [(i, *r, i == "a@1") for i, r in self.rows.items()]
        elif s.startswith("INSERT INTO embedding_profiles"):
            pid, provider, model, dim, norm = params[:5]
            endpoint = params[6] if len(params) > 6 else None
            self.rows[pid] = (provider, model, dim, norm, endpoint)
            self._result = []
        else:
            self._result = []

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class ValidateSpecTest(unittest.TestCase):
    def ok(self, pid="voyage-3@1024", **kw):
        base = dict(provider="voyage", model="voyage-3", dim=1024)
        return profiles.validate_spec(pid, **{**base, **kw})

    def test_a_good_spec_round_trips(self):
        spec = self.ok()
        self.assertEqual(spec, ProfileSpec("voyage-3@1024", "voyage", "voyage-3", 1024, "l2", None))

    def test_an_id_that_is_not_model_at_dim_is_refused(self):
        for bad in ("voyage-3", "voyage-3@", "@1024", "voyage-3@0", "voyage 3@1024",
                    "voyage-3@1024'; DROP TABLE x;--", "-x@10", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.ok(bad)

    def test_the_id_must_match_the_model_and_dim_given(self):
        with self.assertRaises(ValueError):
            self.ok("voyage-3@1024", dim=512)
        with self.assertRaises(ValueError):
            self.ok("voyage-3@1024", model="voyage-3.5")

    def test_an_unknown_provider_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self.ok(provider="cohere")
        self.assertIn("unknown provider", str(ctx.exception))

    def test_normalize_is_l2_or_none(self):
        self.assertEqual(self.ok(normalize="none").normalize, "none")
        with self.assertRaises(ValueError):
            self.ok(normalize="l1")

    def test_openai_compatible_needs_an_endpoint_and_others_refuse_one(self):
        with self.assertRaises(ValueError):
            self.ok("m@8", provider="openai-compatible", model="m", dim=8)
        with self.assertRaises(ValueError):
            self.ok(endpoint="https://example.com")


class EndpointTest(unittest.TestCase):
    def test_https_is_accepted_and_canonicalised(self):
        self.assertEqual(profiles.normalize_endpoint("https://api.example.com/"), "https://api.example.com")
        self.assertEqual(profiles.normalize_endpoint("https://api.example.com/v1"), "https://api.example.com")
        self.assertEqual(profiles.normalize_endpoint("https://h.example.com/api/v1/"), "https://h.example.com/api")

    def test_plain_http_only_on_loopback(self):
        for good in ("http://localhost:11434", "http://127.0.0.1:1234/v1", "http://[::1]:8080"):
            with self.subTest(good=good):
                self.assertTrue(profiles.normalize_endpoint(good).startswith("http://"))
        for bad in ("http://example.com", "http://10.0.0.5:8080", "http://localhost.evil.com"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                profiles.normalize_endpoint(bad)

    def test_credentials_queries_and_other_schemes_are_refused(self):
        for bad in ("https://user:pw@example.com", "https://example.com?key=1",
                    "https://example.com#x", "ftp://example.com", "example.com", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                profiles.normalize_endpoint(bad)


class HubTest(unittest.TestCase):
    def setUp(self):
        db._TABLE_COLUMNS_CACHE.clear()

    def test_add_inserts_inactive_and_never_overwrites(self):
        cur = FakeCur()
        spec = profiles.validate_spec("voyage-3@1024", provider="voyage", model="voyage-3", dim=1024)
        self.assertEqual(profiles.add_profile(cur, spec)["created"], True)
        insert = [s for s, _ in cur.executed if s.startswith("INSERT")][0]
        self.assertIn("false", insert)
        self.assertFalse(any("is_active = true" in s for s, _ in cur.executed))
        self.assertEqual(profiles.add_profile(cur, spec), {"ok": True, "created": False, "id": spec.id})
        clash = ProfileSpec("voyage-3@1024", "voyage", "voyage-3", 1024, "none")
        with self.assertRaises(ValueError) as ctx:
            profiles.add_profile(cur, clash)
        self.assertIn("never overwritten", str(ctx.exception))

    def test_an_endpoint_needs_the_migrated_column(self):
        cur = FakeCur(endpoint_column=False)
        spec = profiles.validate_spec(
            "m@8", provider="openai-compatible", model="m", dim=8, endpoint="http://localhost:1")
        with self.assertRaises(RuntimeError) as ctx:
            profiles.add_profile(cur, spec)
        self.assertIn("0026_library", str(ctx.exception))
        plain = profiles.validate_spec("voyage-3@1024", provider="voyage", model="voyage-3", dim=1024)
        self.assertTrue(profiles.add_profile(cur, plain)["created"])

    def test_list_reports_fields_active_flag_and_rows_per_space(self):
        cur = FakeCur(
            rows={
                "a@1": ("gemini", "a", 1, "l2", None),
                "m@8": ("openai-compatible", "m", 8, "l2", "http://localhost:1"),
            },
            counts={("memory_embeddings", "a@1"): 10, ("library_embeddings", "m@8"): 7},
        )
        out = {p["id"]: p for p in profiles.list_profiles(cur)}
        self.assertEqual(set(out["m@8"]), {"id", "provider", "model", "dim", "normalize",
                                           "endpoint", "is_active", "rows"})
        self.assertTrue(out["a@1"]["is_active"])
        self.assertEqual(out["a@1"]["rows"], {"memory": 10, "library": 0})
        self.assertEqual(out["m@8"]["rows"], {"memory": 0, "library": 7})
        self.assertEqual(out["m@8"]["endpoint"], "http://localhost:1")

    def test_list_survives_a_hub_without_the_library_table_or_endpoint(self):
        cur = FakeCur(rows={"a@1": ("gemini", "a", 1, "l2", None)}, endpoint_column=False,
                      tables=("memory_embeddings",))
        (only,) = profiles.list_profiles(cur)
        self.assertIsNone(only["endpoint"])
        self.assertEqual(only["rows"]["library"], 0)

    def test_resolve_reads_an_unknown_id_through_the_cursor_once(self):
        profiles.clear_learned()
        cur = FakeCur(rows={"m@8": ("openai-compatible", "m", 8, "l2", "http://localhost:1")})
        spec = profiles.resolve_spec("m@8", cur)
        self.assertEqual((spec.provider, spec.endpoint), ("openai-compatible", "http://localhost:1"))
        n = len(cur.executed)
        self.assertEqual(profiles.resolve_spec("m@8", cur), spec)
        self.assertEqual(len(cur.executed), n, "second lookup must hit the process cache")
        profiles.clear_learned()


class LibraryIndexTest(unittest.TestCase):
    def _cur(self, exists=False):
        cur = mock.Mock()
        cur.fetchone.return_value = (1,) if exists else None
        return cur

    def _ddl(self, cur):
        return [" ".join(c.args[0].split()) for c in cur.execute.call_args_list
                if c.args[0].lstrip().startswith("CREATE INDEX")]

    def test_the_index_is_a_partial_expression_hnsw_for_the_profile(self):
        cur = self._cur()
        name = profiles.ensure_library_index(cur, "voyage-3@1024", dim=1024)
        self.assertEqual(name, "idx_library_hnsw_voyage_3_1024")
        self.assertEqual(
            self._ddl(cur),
            ["CREATE INDEX IF NOT EXISTS idx_library_hnsw_voyage_3_1024 ON library_embeddings "
             "USING hnsw ((embedding::vector(1024)) vector_cosine_ops) WHERE profile = 'voyage-3@1024'"],
        )

    def test_the_memory_table_gets_its_own_index_family(self):
        cur = self._cur()
        name = profiles.ensure_profile_index(cur, "voyage-3@1024", "memory_embeddings", dim=1024)
        self.assertEqual(name, "idx_memory_hnsw_voyage_3_1024")
        self.assertIn("ON memory_embeddings USING hnsw ((embedding::vector(1024))", self._ddl(cur)[0])

    def test_the_two_gemini_profiles_reuse_the_names_0026_recreates(self):
        self.assertEqual(
            profiles.profile_index_name("gemini-embedding-001@768", "memory_embeddings"),
            "idx_memory_embeddings_hnsw_gemini768")
        self.assertEqual(
            profiles.profile_index_name("gemini-embedding-2@768", "memory_embeddings"),
            "idx_memory_embeddings_hnsw_gemini2_768")
        self.assertEqual(
            profiles.profile_index_name("gemini-embedding-2@768", "library_embeddings"),
            "idx_library_hnsw_gemini_embedding_2_768")

    def test_an_existing_index_means_no_ddl(self):
        cur = self._cur(exists=True)
        profiles.ensure_profile_index(cur, "voyage-3@1024", "memory_embeddings", dim=1024)
        self.assertEqual(self._ddl(cur), [])

    def test_the_table_is_allow_listed_and_the_id_quote_escaped(self):
        cur = self._cur()
        with self.assertRaises(ValueError):
            profiles.ensure_profile_index(cur, "x@8", "episodes; DROP TABLE episodes", dim=8)
        cur.execute.assert_not_called()
        profiles.ensure_library_index(cur, "x@8' OR '1'='1", dim=8)
        ddl = self._ddl(cur)[0]
        self.assertTrue(ddl.endswith("WHERE profile = 'x@8'' OR ''1''=''1'"))

    def test_the_id_is_never_parsed_an_odd_id_works_with_a_dim(self):
        cur = self._cur()
        name = profiles.ensure_profile_index(cur, "prof-1", "memory_embeddings", dim=768)
        self.assertRegex(name, r"^idx_memory_hnsw_prof_1_[0-9a-f]{9}$")
        self.assertIn("embedding::vector(768)", self._ddl(cur)[0])
        self.assertIn("WHERE profile = 'prof-1'", self._ddl(cur)[0])

    def test_odd_ids_that_sanitise_alike_get_different_names(self):
        self.assertNotEqual(profiles.profile_index_name("a.b"), profiles.profile_index_name("a_b"))
        self.assertRegex(profiles.profile_index_name("A B!"), r"^idx_library_hnsw_a_b_[0-9a-f]{9}$")

    def test_the_dim_comes_from_the_profile_row_when_not_given(self):
        profiles.clear_learned()
        cur = FakeCur(rows={"odd-id": ("openai-compatible", "m", 8, "l2", "http://localhost:1")})
        db._TABLE_COLUMNS_CACHE.clear()
        profiles.ensure_profile_index(cur, "odd-id", "library_embeddings")
        ddl = [s for s, _ in cur.executed if s.startswith("CREATE INDEX")]
        self.assertEqual(len(ddl), 1)
        self.assertIn("embedding::vector(8)", ddl[0])
        profiles.clear_learned()

    def test_a_missing_row_raises_loudly_but_quietly_skips_on_the_job_paths(self):
        profiles.clear_learned()
        db._TABLE_COLUMNS_CACHE.clear()
        cur = FakeCur()
        with self.assertRaises(ValueError):
            profiles.ensure_profile_index(cur, "prof-1", "memory_embeddings")
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertIsNone(
                profiles.ensure_profile_index(cur, "prof-1", "memory_embeddings", quiet=True))
        self.assertEqual(err.getvalue().count("\n"), 1)
        self.assertFalse([s for s, _ in cur.executed if s.startswith("CREATE INDEX")])

    def test_dims_pgvector_cannot_index_are_refused(self):
        with self.assertRaises(ValueError):
            profiles.ensure_library_index(self._cur(), "big-model@3072", dim=3072)

    def test_quiet_mode_swallows_a_failure_under_a_savepoint(self):
        cur = self._cur()
        statements = []

        def execute(sql, params=None):
            statements.append(" ".join(sql.split()))
            if sql.lstrip().startswith("CREATE INDEX"):
                raise RuntimeError("access method hnsw does not exist")

        cur.execute.side_effect = execute
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertIsNone(profiles.ensure_profile_index(
                cur, "voyage-3@1024", "memory_embeddings", dim=1024, quiet=True))
        self.assertEqual(statements[0], "SAVEPOINT khipu_profile_index")
        self.assertEqual(statements[-1], "ROLLBACK TO SAVEPOINT khipu_profile_index")
        self.assertIn("no index for voyage-3@1024", err.getvalue())

    def test_quiet_mode_releases_the_savepoint_on_success(self):
        cur = self._cur()
        self.assertEqual(
            profiles.ensure_profile_index(cur, "voyage-3@1024", "library_embeddings", dim=1024, quiet=True),
            "idx_library_hnsw_voyage_3_1024")
        self.assertEqual(" ".join(cur.execute.call_args_list[-1].args[0].split()),
                         "RELEASE SAVEPOINT khipu_profile_index")

    def test_a_long_id_still_yields_a_legal_identifier(self):
        name = profiles.library_index_name("a" * 90 + "@1024")
        self.assertLessEqual(len(name), 63)
        self.assertNotEqual(name, profiles.library_index_name("a" * 91 + "@1024"))


class CliTest(unittest.TestCase):
    def _run(self, **kw):
        base = dict(embed_cmd="profiles", profiles_cmd="add", normalize="l2", endpoint=None)
        args = argparse.Namespace(**{**base, **kw})
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = cli.cmd_embed(args)
        return rc, json.loads(out.getvalue())

    def test_a_refusal_is_exit_2_with_the_reason_and_never_connects(self):
        with mock.patch("khipu.db.connect", side_effect=AssertionError("must not connect")):
            for kw in (
                dict(id="voyage-3", provider="voyage", model="voyage-3", dim=1024),
                dict(id="voyage-3@1024", provider="nope", model="voyage-3", dim=1024),
                dict(id="m@8", provider="openai-compatible", model="m", dim=8,
                     endpoint="http://example.com"),
            ):
                with self.subTest(kw=kw):
                    rc, out = self._run(**kw)
                    self.assertEqual(rc, 2)
                    self.assertFalse(out["ok"])

    def test_add_commits_the_new_profile(self):
        db._TABLE_COLUMNS_CACHE.clear()
        cur = FakeCur()
        conn = mock.MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value = cur
        with mock.patch("khipu.db.connect", return_value=conn):
            rc, out = self._run(id="voyage-3@1024", provider="voyage", model="voyage-3", dim=1024)
        self.assertEqual((rc, out["created"]), (0, True))
        conn.commit.assert_called_once()
        ddl = [sql for sql, _ in cur.executed if sql.startswith("CREATE INDEX")]
        self.assertEqual(len(ddl), 2)
        self.assertTrue(any("ON memory_embeddings" in d for d in ddl))
        self.assertTrue(any("ON library_embeddings" in d for d in ddl))

    def test_list_prints_the_profiles(self):
        db._TABLE_COLUMNS_CACHE.clear()
        cur = FakeCur(rows={"a@1": ("gemini", "a", 1, "l2", None)})
        conn = mock.MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value = cur
        args = argparse.Namespace(embed_cmd="profiles", profiles_cmd="list")
        with mock.patch("khipu.db.connect", return_value=conn), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = cli.cmd_embed(args)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["profiles"][0]["id"], "a@1")

    def test_the_argparse_surface_exists(self):
        ns = cli.build_parser().parse_args(["embed", "profiles", "add", "voyage-3@1024", "--provider", "voyage",
                                "--model", "voyage-3", "--dim", "1024"])
        self.assertEqual((ns.profiles_cmd, ns.dim, ns.normalize), ("add", 1024, "l2"))


if __name__ == "__main__":
    unittest.main()
