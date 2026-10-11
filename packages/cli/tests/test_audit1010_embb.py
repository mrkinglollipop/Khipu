"""Hermetic regressions for audit 1010 embedding findings."""
from __future__ import annotations

import io
import json
import math
import struct
import unittest
from unittest import mock

from khipu import embed_jobs, embed_ops, modelcheck, profiles, vector_scan


class ModelCheckResponseTest(unittest.TestCase):
    def test_http_200_requires_usable_gemini_text(self):
        for response in ("{}", "{bad", "<html>oops</html>", '{"candidates": []}', '{"candidates": null}',
                         '{"candidates": {}}', '{"candidates": [{"content": {"parts": null}}]}',
                         '{"candidates": [{"content": {"parts": [{"text": "  "}]}}]}'):
            with self.subTest(response=response), \
                    mock.patch.object(modelcheck.keychain, "resolve_gemini_key", return_value="k"), \
                    mock.patch.object(modelcheck, "_post_once", return_value=(200, response)):
                self.assertFalse(modelcheck._check_gemini_generate(timeout=1)["ok"])

    def test_http_200_requires_usable_openai_text(self):
        settings = {"provider": "local", "endpoint": "http://localhost:9", "model_id": "local"}
        for response in ("{}", "{bad", "<html>oops</html>", '{"choices": []}', '{"choices": null}',
                         '{"choices": {}}', '{"choices": [{"message": null}]}',
                         '{"choices": [{"message": {"content": "  "}}]}'):
            with self.subTest(response=response), \
                    mock.patch.object(modelcheck.models_mod, "synth_settings", return_value=settings), \
                    mock.patch.object(modelcheck, "_post_once", return_value=(200, response)):
                self.assertFalse(modelcheck._check_openai_compat_generate(timeout=1)["ok"])


class GeminiEmbedProfileTest(unittest.TestCase):
    def tearDown(self):
        profiles.clear_learned()

    def test_warm_non_gemini_active_profile_dispatches_gemini(self):
        custom = profiles.ProfileSpec("voyage-3@1024", "voyage", "voyage-3", 1024)
        profiles.register_spec(custom)
        with mock.patch.object(modelcheck.keychain, "resolve_gemini_key", return_value="k"), \
                mock.patch.object(modelcheck, "_active_embed_profile", return_value=custom.id), \
                mock.patch.object(modelcheck.embed_mod, "embed_one", return_value=[1.0]) as embed_one:
            result = modelcheck._check_gemini_embed(timeout=1)
        self.assertTrue(result["ok"])
        self.assertEqual(embed_one.call_args.kwargs["profile"], modelcheck.embed_mod.PROFILE_2)

    def test_cold_custom_profile_resolution_error_falls_back_to_gemini(self):
        with mock.patch.object(modelcheck.keychain, "resolve_gemini_key", return_value="k"), \
                mock.patch.object(modelcheck, "_active_embed_profile", return_value="custom@4"), \
                mock.patch.object(modelcheck.embed_mod._profiles, "resolve_spec", side_effect=ValueError("cold")), \
                mock.patch.object(modelcheck.embed_mod, "embed_one", return_value=[1.0]) as embed_one:
            result = modelcheck._check_gemini_embed(timeout=1)
        self.assertTrue(result["ok"])
        self.assertEqual(embed_one.call_args.kwargs["profile"], modelcheck.embed_mod.PROFILE_2)

    def test_embed_failure_does_not_abort_following_checks(self):
        with mock.patch.object(modelcheck.keychain, "resolve_gemini_key", return_value="k"), \
                mock.patch.object(modelcheck, "_active_embed_profile", return_value=modelcheck.embed_mod.PROFILE_2), \
                mock.patch.object(modelcheck.embed_mod, "embed_one", side_effect=ValueError("bad payload")), \
                mock.patch.object(modelcheck, "_check_gemini_generate", return_value={"id": "gemini_generate", "ok": True}) as generate, \
                mock.patch.object(modelcheck, "_check_openai_compat_generate", return_value={"id": "openai_compat_generate", "ok": True}) as openai:
            result = modelcheck.check_model_keys()
        self.assertFalse(result["ok"])
        self.assertEqual([check["id"] for check in result["checks"]], ["gemini_embed", "gemini_generate", "openai_compat_generate"])
        generate.assert_called_once()
        openai.assert_called_once()


class CheckKeyVectorTest(unittest.TestCase):
    def _check(self, vectors):
        with mock.patch.dict(embed_ops.embed._BUILDERS, {"gemini": lambda *_: ("u", {}, {})}), \
                mock.patch.object(embed_ops.embed, "_post_json", return_value={}), \
                mock.patch.object(embed_ops.embed, "_parse_vectors", return_value=vectors):
            return embed_ops.check_key("gemini")

    def test_requires_one_finite_nonzero_numeric_vector(self):
        for vectors in (["not-numbers"], [["invalid", "invalid"]], [[None]], [[True]],
                        [[math.nan]], [[math.inf]], [[0.0, 0.0]], [[1.0], [2.0]]):
            with self.subTest(vectors=vectors):
                receipt = self._check(vectors)
                self.assertFalse(receipt["ok"])
                self.assertNotIn("dim", receipt)

    def test_valid_vector_still_reports_its_dimension(self):
        result = self._check([[3.0, 4.0]])
        self.assertEqual((result["ok"], result["dim"]), (True, 2))


class ProfileIndexNameTest(unittest.TestCase):
    def test_valid_sanitized_ids_get_distinct_hashed_names(self):
        a = profiles.profile_index_name("a-b@768")
        b = profiles.profile_index_name("a_b@768")
        self.assertNotEqual(a, b)
        self.assertRegex(a, r"_[0-9a-f]{9}$")
        self.assertRegex(b, r"_[0-9a-f]{9}$")

    def test_colliding_profiles_build_and_drop_independent_indexes(self):
        definitions = {}
        cur = mock.Mock()

        def execute(sql, params=()):
            if sql.startswith("SELECT 1 FROM pg_indexes"):
                cur.fetchone.return_value = (1,) if params[0] in definitions else None
            elif sql.startswith("CREATE INDEX"):
                definitions[sql.split()[5]] = sql
            elif sql.startswith("DROP INDEX"):
                definitions.pop(sql.rsplit(".", 1)[-1], None)

        cur.execute.side_effect = execute
        names = {}
        for table in profiles.INDEXED_TABLES:
            for profile in ("a-b@768", "a_b@768"):
                name = profiles.ensure_profile_index(cur, profile, table, dim=768)
                names[table, profile] = name
                self.assertTrue(definitions[name].endswith(f"WHERE profile = '{profile}'"))
        self.assertEqual(len(definitions), 4)
        profile = "a_b@768"
        spec = profiles.ProfileSpec(profile, "openai-compatible", "a_b", 768)
        with mock.patch.object(profiles, "load_spec", return_value=spec), \
                mock.patch.object(profiles, "profile_users", return_value=[]), \
                mock.patch.object(profiles, "_to_regclass", return_value=False):
            profiles.delete_profile(cur, profile)
        self.assertEqual(set(definitions), {
            names[table, "a-b@768"] for table in profiles.INDEXED_TABLES
        })


class LegacyIndexDefinitionTest(unittest.TestCase):
    profile = "gemini-embedding-2@768"
    table = "memory_embeddings"

    class Catalog:
        def __init__(self, indexdef):
            self.indexdef = indexdef
            self.executed = []
            self._row = None

        def execute(self, sql, params=()):
            self.executed.append((sql, params))
            if "SELECT indexdef" in sql:
                self._row = (self.indexdef,) if self.indexdef else None
            elif "SELECT 1 FROM pg_indexes" in sql:
                self._row = (1,)

        def fetchone(self):
            return self._row

    def _definition(self, *, profile=None, dim=768):
        profile = profile or self.profile
        return ("CREATE INDEX idx_memory_embeddings_hnsw_gemini2_768 ON public.memory_embeddings "
                f"USING hnsw ((embedding::vector({dim})) vector_cosine_ops) WHERE profile = '{profile}'")

    def test_only_matching_legacy_definition_is_reusable_or_present(self):
        good = self.Catalog(self._definition())
        self.assertTrue(profiles._legacy_index_matches(good, self.profile, self.table, 768))
        catalog_format = self._definition().replace(
            "(embedding::vector(768))", "(embedding)::vector(768)",
        ).replace(f"WHERE profile = '{self.profile}'", f"WHERE (profile = '{self.profile}'::text)")
        self.assertTrue(profiles._legacy_index_matches(
            self.Catalog(catalog_format), self.profile, self.table, 768,
        ))
        bad = self.Catalog(self._definition(profile="gemini-embedding-001@768"))
        self.assertFalse(profiles._legacy_index_matches(bad, self.profile, self.table, 768))
        status = profiles._index_status(
            bad, self.profile, 768, {"memory": 1},
            {profiles.profile_index_name(self.profile, self.table)},
        )
        self.assertNotEqual(status["memory"], "present")

    def test_wrong_legacy_definition_is_never_reused_or_dropped(self):
        cur = self.Catalog(self._definition(dim=32))
        with self.assertRaises(RuntimeError):
            profiles.ensure_profile_index(cur, self.profile, self.table, dim=768)
        self.assertIsNone(profiles.drop_profile_index(cur, self.profile, self.table))
        self.assertFalse(any("CREATE INDEX" in sql or "DROP INDEX" in sql for sql, _ in cur.executed))

    def test_legacy_predicate_and_expression_must_match_exactly(self):
        definitions = (
            self._definition() + " OR profile = 'another@768'",
            self._definition() + " AND embedding IS NOT NULL",
            self._definition().replace("embedding::vector", "other_vector::vector"),
        )
        for definition in definitions:
            with self.subTest(definition=definition):
                cur = self.Catalog(definition)
                with self.assertRaises(RuntimeError):
                    profiles.ensure_profile_index(cur, self.profile, self.table, dim=768)
                self.assertIsNone(profiles.drop_profile_index(cur, self.profile, self.table))

    def test_delete_does_not_drop_a_wrong_legacy_index(self):
        spec = profiles.SEED_PROFILES[self.profile]
        cur = mock.Mock()
        with mock.patch.object(profiles, "load_spec", return_value=spec), \
                mock.patch.object(profiles, "profile_users", return_value=[]), \
                mock.patch.object(profiles, "_to_regclass", return_value=False), \
                mock.patch.object(profiles, "_legacy_index_matches", return_value=False), \
                mock.patch.object(profiles, "_drop_index_quiet", return_value=True) as drop:
            profiles.delete_profile(cur, self.profile)
        self.assertNotIn(profiles.profile_index_name(self.profile, self.table),
                         [call.args[1] for call in drop.call_args_list])


class VectorScanExactnessTest(unittest.TestCase):
    def test_accelerated_path_does_not_discard_exact_winner(self):
        query = [1.0, 2.0 ** -14]
        rows = [(f"a{i}", struct.pack("2f", 1.0, 0.0)) for i in range(64)]
        rows.append(("winner", struct.pack("2f", 1.0, 2.0 ** -14)))
        calls = []

        def float32_tie(*args):
            calls.append(args)
            out = args[-2]
            for i in range(len(out)):
                out[i] = 1.0

        with mock.patch.object(vector_scan, "_cblas_sgemv", return_value=float32_tie):
            result = vector_scan.top_k_by_dot(query, rows, 1, use_blas=True)
        self.assertTrue(calls)
        self.assertEqual(result[0][0], "winner")
        self.assertEqual(result, vector_scan.top_k_by_dot(query, rows, 1, use_blas=False))


class PartialJobReceiptTest(unittest.TestCase):
    def test_partial_job_receipt_is_unsuccessful(self):
        stream = io.StringIO()
        with mock.patch.object(embed_jobs, "jobs_dir", return_value=mock.MagicMock()):
            # Avoid receipt I/O; the JSON receipt is the contract under test.
            rc = embed_jobs.run_job(
                kind="embed", profile="p@1", space="memory", job_id="partial",
                work=lambda _job: {"embedded": 1, "failed_chunks": 1}, stream=stream,
            )
        self.assertEqual(rc, 1)
        self.assertFalse(json.loads(stream.getvalue())["ok"])

    def test_remaining_work_is_unsuccessful_even_without_failed_callbacks(self):
        stream = io.StringIO()
        with mock.patch.object(embed_jobs, "jobs_dir", return_value=mock.MagicMock()):
            rc = embed_jobs.run_job(
                kind="embed", profile="p@1", space="memory", job_id="remaining",
                work=lambda _job: {"embedded": 0, "remaining": 1}, stream=stream,
            )
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(stream.getvalue())["state"], "failed")


class LibraryIndexCompatibilityTest(unittest.TestCase):
    def test_rv3_old_library_index_requires_matching_definition(self):
        from khipu import library_search
        profile = "gemini-embedding-001@768"
        old_name = "idx_library_hnsw_gemini_embedding_001_768"
        definition = (f"CREATE INDEX {old_name} ON public.library_embeddings USING hnsw "
                      "((embedding::vector(768)) vector_cosine_ops) "
                      "WHERE (profile = 'gemini-embedding-001@768'::text)")
        for sql, expected in [(definition, True), (definition.replace("001@768", "002@768"), False),
                              (definition.replace("vector(768)", "vector(2)"), False)]:
            class Cur:
                def execute(self, statement, params=None):
                    self.row = (sql,) if "SELECT indexdef" in statement and params == (old_name,) else None
                def fetchone(self):
                    return self.row
            library_search._INDEX_SEEN.clear()
            self.assertEqual(library_search.profile_has_index(Cur(), mock.Mock(), profile), expected)
