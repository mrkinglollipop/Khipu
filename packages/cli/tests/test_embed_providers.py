# --bypass-harness (sonnet lane) — authored directly by the dispatched on-sub
# Sonnet build agent (brief: do not delegate); no further agent to route to.
"""Provider dispatch for embed_batch / embed_one (library sources + BYO
embeddings, Session A): the voyage and openai-compatible adapters, keyed on
the profile's provider, over a fake transport. No network, no database, no
Keychain. The Gemini path is covered by test_embed.py / test_recall_daemon.py;
the Gemini cases here only pin that dispatch did not move it.

Voyage request shape: docs.voyageai.com/reference/embeddings-api (POST
https://api.voyageai.com/v1/embeddings, bearer key, body input/model/input_type,
response data[].embedding + index; up to 1000 texts per request).
"""
from __future__ import annotations

import io
import json
import math
import unittest
import urllib.error
from unittest import mock

from khipu import embed, profiles
from khipu.profiles import ProfileSpec

VOYAGE = ProfileSpec("voyage-3@1024", "voyage", "voyage-3", 1024)
VOYAGE_512 = ProfileSpec("voyage-3.5@512", "voyage", "voyage-3.5", 512)
VOYAGE_RAW = ProfileSpec("voyage-3@4", "voyage", "voyage-3", 4, normalize="none")
LOCAL = ProfileSpec(
    "nomic-embed-text@4", "openai-compatible", "nomic-embed-text", 4,
    endpoint="http://localhost:11434",
)


def _vec(dim: int, hot: int = 0, mag: float = 3.0) -> list[float]:
    v = [0.0] * dim
    v[hot] = mag
    return v


def _voyage_body(vectors: list[list[float]], *, shuffled: bool = False) -> bytes:
    data = [{"object": "embedding", "embedding": v, "index": i} for i, v in enumerate(vectors)]
    if shuffled:
        data.reverse()
    return json.dumps({"object": "list", "data": data, "usage": {"total_tokens": 1}}).encode()


class FakeTransport:
    """Records every request; replays scripted bytes / exceptions."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[tuple[str, dict, dict, float]] = []

    def __call__(self, url, data, headers, timeout):
        self.calls.append((url, json.loads(data.decode()), dict(headers), timeout))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _http_error(code: int, body: bytes = b"nope") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(body))


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        for spec in (VOYAGE, VOYAGE_512, VOYAGE_RAW, LOCAL):
            profiles.register_spec(spec)
        for patch in (
            mock.patch.object(embed, "_budget_take"),
            mock.patch.object(embed.time, "sleep"),
            mock.patch.object(embed, "_voyage_key", return_value="vk-secret"),
            mock.patch.object(embed, "_openai_compat_key", return_value=None),
        ):
            patch.start()
            self.addCleanup(patch.stop)


class VoyageAdapterTest(_Base):
    def test_request_shape_is_the_documented_one(self) -> None:
        t = FakeTransport(_voyage_body([_vec(1024), _vec(1024, 1)]))
        out = embed.embed_batch(["a", "b"], profile=VOYAGE.id, retries=0, transport=t)
        url, body, headers, _timeout = t.calls[0]
        self.assertEqual(url, "https://api.voyageai.com/v1/embeddings")
        self.assertEqual(body, {"input": ["a", "b"], "model": "voyage-3", "input_type": "document"})
        self.assertEqual(headers["Authorization"], "Bearer vk-secret")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertNotIn("x-goog-api-key", headers)
        self.assertEqual(len(out), 2)

    def test_the_key_never_appears_in_the_url_or_body(self) -> None:
        t = FakeTransport(_voyage_body([_vec(1024)]))
        embed.embed_batch(["a"], profile=VOYAGE.id, retries=0, transport=t)
        url, body, _h, _t = t.calls[0]
        self.assertNotIn("vk-secret", url)
        self.assertNotIn("vk-secret", json.dumps(body))

    def test_input_type_query_is_passed_through_and_defaults_to_document(self) -> None:
        t = FakeTransport(_voyage_body([_vec(1024)]), _voyage_body([_vec(1024)]))
        embed.embed_batch(["a"], profile=VOYAGE.id, retries=0, transport=t, input_type="query")
        embed.embed_batch(["a"], profile=VOYAGE.id, retries=0, transport=t)
        self.assertEqual(t.calls[0][1]["input_type"], "query")
        self.assertEqual(t.calls[1][1]["input_type"], "document")

    def test_vectors_come_back_l2_normalised_and_in_index_order(self) -> None:
        t = FakeTransport(_voyage_body([_vec(1024, 0), _vec(1024, 1)], shuffled=True))
        out = embed.embed_batch(["a", "b"], profile=VOYAGE.id, retries=0, transport=t)
        self.assertEqual(out[0][0], 1.0)  # first text -> index 0, normalised from 3.0
        self.assertEqual(out[1][1], 1.0)
        for v in out:
            self.assertAlmostEqual(math.sqrt(sum(x * x for x in v)), 1.0)

    def test_normalize_none_leaves_the_vector_alone(self) -> None:
        t = FakeTransport(_voyage_body([[3.0, 0.0, 4.0, 0.0]]))
        out = embed.embed_batch(["a"], profile=VOYAGE_RAW.id, retries=0, transport=t)
        self.assertEqual(out, [[3.0, 0.0, 4.0, 0.0]])

    def test_output_dimension_is_sent_only_for_a_flexible_model_off_the_default(self) -> None:
        t = FakeTransport(_voyage_body([_vec(1024)]), _voyage_body([_vec(512)]))
        embed.embed_batch(["a"], profile=VOYAGE.id, retries=0, transport=t)
        embed.embed_batch(["a"], profile=VOYAGE_512.id, retries=0, transport=t)
        self.assertNotIn("output_dimension", t.calls[0][1])
        self.assertEqual(t.calls[1][1]["output_dimension"], 512)

    def test_a_wrong_width_is_refused(self) -> None:
        t = FakeTransport(_voyage_body([_vec(8)]))
        with self.assertRaises(RuntimeError) as ctx:
            embed.embed_batch(["a"], profile=VOYAGE.id, retries=0, transport=t)
        self.assertIn("expected dim 1024, got 8", str(ctx.exception))

    def test_a_short_response_is_refused(self) -> None:
        t = FakeTransport(_voyage_body([_vec(1024)]))
        with self.assertRaises(RuntimeError) as ctx:
            embed.embed_batch(["a", "b"], profile=VOYAGE.id, retries=0, transport=t)
        self.assertIn("1 vectors for 2 texts", str(ctx.exception))

    def test_a_large_batch_is_split_to_the_provider_cap(self) -> None:
        cap = embed._MAX_PER_REQUEST["voyage"]
        n = cap + 3
        t = FakeTransport(
            _voyage_body([_vec(1024)] * cap), _voyage_body([_vec(1024)] * 3),
        )
        out = embed.embed_batch([f"t{i}" for i in range(n)], profile=VOYAGE.id, retries=0, transport=t)
        self.assertEqual(len(out), n)
        self.assertEqual([len(c[1]["input"]) for c in t.calls], [cap, 3])


class OpenAICompatibleAdapterTest(_Base):
    def test_request_shape_and_no_auth_header_without_a_key(self) -> None:
        t = FakeTransport(_voyage_body([_vec(4)]))
        embed.embed_batch(["hello"], profile=LOCAL.id, retries=0, transport=t)
        url, body, headers, _t = t.calls[0]
        self.assertEqual(url, "http://localhost:11434/v1/embeddings")
        self.assertEqual(body, {"input": ["hello"], "model": "nomic-embed-text"})
        self.assertNotIn("Authorization", headers)

    def test_a_stored_key_is_sent_as_a_bearer(self) -> None:
        t = FakeTransport(_voyage_body([_vec(4)]))
        with mock.patch.object(embed, "_openai_compat_key", return_value="sk-local"):
            embed.embed_batch(["hello"], profile=LOCAL.id, retries=0, transport=t)
        self.assertEqual(t.calls[0][2]["Authorization"], "Bearer sk-local")

    def test_the_endpoint_comes_from_the_profile_not_from_models_embed(self) -> None:
        other = ProfileSpec(
            "bge-m3@4", "openai-compatible", "bge-m3", 4, endpoint="https://emb.example.com/api",
        )
        profiles.register_spec(other)
        t = FakeTransport(_voyage_body([_vec(4)]))
        with mock.patch("khipu.models.show_models", side_effect=AssertionError("models.embed read")):
            embed.embed_batch(["x"], profile=other.id, retries=0, transport=t)
        self.assertEqual(t.calls[0][0], "https://emb.example.com/api/v1/embeddings")

    def test_vectors_are_normalised_and_dim_checked(self) -> None:
        t = FakeTransport(_voyage_body([[0.0, 3.0, 4.0, 0.0]]), _voyage_body([[1.0, 2.0]]))
        out = embed.embed_batch(["a"], profile=LOCAL.id, retries=0, transport=t)
        self.assertAlmostEqual(out[0][1], 0.6)
        self.assertAlmostEqual(out[0][2], 0.8)
        with self.assertRaises(RuntimeError):
            embed.embed_batch(["a"], profile=LOCAL.id, retries=0, transport=t)

    def test_key_cache_keeps_a_missing_key_and_drops_it_on_401(self) -> None:
        embed._OPENAI_KEY_CACHE[:] = [None]
        embed._note_auth_failure(500, "openai-compatible")
        self.assertEqual(embed._OPENAI_KEY_CACHE, [None])
        embed._note_auth_failure(401, "openai-compatible")
        self.assertEqual(embed._OPENAI_KEY_CACHE, [])


class RetryAndBudgetTest(_Base):
    def test_429_and_5xx_are_retried_on_every_provider(self) -> None:
        for spec, ok in ((VOYAGE, _voyage_body([_vec(1024)])), (LOCAL, _voyage_body([_vec(4)]))):
            for code in (429, 500, 502, 503, 504):
                with self.subTest(provider=spec.provider, code=code):
                    t = FakeTransport(_http_error(code), ok)
                    out = embed.embed_batch(["a"], profile=spec.id, retries=1, transport=t)
                    self.assertEqual(len(out), 1)
                    self.assertEqual(len(t.calls), 2)

    def test_a_400_is_not_retried(self) -> None:
        t = FakeTransport(_http_error(400, b"bad input"))
        with self.assertRaises(RuntimeError) as ctx:
            embed.embed_batch(["a"], profile=VOYAGE.id, retries=3, transport=t)
        self.assertEqual(len(t.calls), 1)
        self.assertIn("embed HTTP 400", str(ctx.exception))

    def test_the_ladder_doubles_the_delay_and_gives_up_after_the_retries(self) -> None:
        t = FakeTransport(*[_http_error(503)] * 3)
        with self.assertRaises(RuntimeError):
            embed.embed_batch(["a"], profile=VOYAGE.id, retries=2, delay=2.0, transport=t)
        self.assertEqual(len(t.calls), 3)
        self.assertEqual([c.args[0] for c in embed.time.sleep.call_args_list], [2.0, 4.0])

    def test_a_network_error_is_retried_then_named(self) -> None:
        t = FakeTransport(urllib.error.URLError("down"), urllib.error.URLError("down"))
        with self.assertRaises(RuntimeError) as ctx:
            embed.embed_batch(["a"], profile=LOCAL.id, retries=1, transport=t)
        self.assertEqual(len(t.calls), 2)
        self.assertIn("network error", str(ctx.exception))

    def test_each_request_takes_one_budget_unit(self) -> None:
        t = FakeTransport(_http_error(503), _voyage_body([_vec(1024)]))
        embed.embed_batch(["a"], profile=VOYAGE.id, retries=1, transport=t)
        self.assertEqual(embed._budget_take.call_count, 2)

    def test_a_401_drops_the_cached_voyage_key(self) -> None:
        embed._VOYAGE_KEY_CACHE[:] = ["stale"]
        t = FakeTransport(_http_error(401, b"bad key"))
        with self.assertRaises(RuntimeError):
            embed.embed_batch(["a"], profile=VOYAGE.id, retries=0, transport=t)
        self.assertEqual(embed._VOYAGE_KEY_CACHE, [])


class QueryBudgetTest(_Base):
    def test_the_query_budget_is_the_same_for_every_provider(self) -> None:
        for spec in (VOYAGE, LOCAL):
            with self.subTest(provider=spec.provider):
                t = FakeTransport(*[urllib.error.URLError("stall")] * (embed.QUERY_EMBED_RETRIES + 1))
                embed.set_transport(t)
                self.addCleanup(embed.set_transport, None)
                with self.assertRaises(RuntimeError):
                    embed.embed_one(
                        "q", profile=spec.id, retries=embed.QUERY_EMBED_RETRIES,
                        timeout=embed.QUERY_EMBED_TIMEOUT_S, input_type="query",
                    )
                self.assertEqual(len(t.calls), embed.QUERY_EMBED_RETRIES + 1)
                self.assertTrue(all(c[3] == embed.QUERY_EMBED_TIMEOUT_S for c in t.calls))

    def test_embed_one_forwards_input_type_only_when_given(self) -> None:
        with mock.patch.object(embed, "embed_batch", return_value=[[1.0]]) as m:
            embed.embed_one("q", profile=VOYAGE.id)
            self.assertNotIn("input_type", m.call_args.kwargs)
            embed.embed_one("q", profile=VOYAGE.id, input_type="query")
            self.assertEqual(m.call_args.kwargs["input_type"], "query")

    def test_the_query_vector_path_asks_voyage_for_a_query_embedding(self) -> None:
        class _Cur:
            def execute(self, *a, **k):
                pass

            def fetchone(self):
                return (False,)  # no memory_query_cache table

        with mock.patch.object(embed, "embed_one", return_value=[0.5]) as m:
            vec, state = embed._query_vec(_Cur(), mock.Mock(), VOYAGE.id, "what is x")
        self.assertEqual((vec, state), ([0.5], "off"))
        self.assertEqual(m.call_args.kwargs["input_type"], "query")
        self.assertEqual(m.call_args.kwargs["retries"], embed.QUERY_EMBED_RETRIES)
        self.assertEqual(m.call_args.kwargs["timeout"], embed.QUERY_EMBED_TIMEOUT_S)


class GeminiStaysPutTest(_Base):
    def test_a_gemini_profile_still_posts_the_gemini_request(self) -> None:
        body = json.dumps({"embeddings": [{"values": _vec(embed.DIM)}]}).encode()
        t = FakeTransport(body)
        with mock.patch.object(embed, "_gemini_key", return_value="g-key"):
            out = embed.embed_batch(["hi"], profile=embed.PROFILE_2, retries=0, transport=t)
        url, req, headers, _t = t.calls[0]
        self.assertEqual(
            url,
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-embedding-2:batchEmbedContents",
        )
        self.assertEqual(headers["x-goog-api-key"], "g-key")
        self.assertNotIn("Authorization", headers)
        self.assertEqual(req["requests"][0]["outputDimensionality"], embed.DIM)
        self.assertEqual(req["requests"][0]["content"], {"parts": [{"text": "hi"}]})
        self.assertEqual(out[0][0], 1.0)

    def test_gemini_is_never_split_by_the_provider_cap(self) -> None:
        self.assertIsNone(embed._MAX_PER_REQUEST["gemini"])

    def test_an_unknown_profile_still_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            embed.model_for_profile("nope@0")
        with mock.patch("khipu.db.connect", side_effect=RuntimeError("no hub")):
            with self.assertRaises(ValueError):
                embed.embed_batch(["a"], profile="nope@0")

    def test_the_two_gemini_ids_resolve_without_a_database(self) -> None:
        with mock.patch("khipu.db.connect", side_effect=AssertionError("no hub for a seed")):
            self.assertEqual(embed.model_for_profile(embed.PROFILE_001), embed.MODEL_001)
            self.assertEqual(profiles.resolve_spec(embed.PROFILE_2).provider, "gemini")


class MemorySpaceAnyDimTest(_Base):
    """memory_embeddings.embedding is untyped (0026): any profile width works,
    the vector length is checked in code, and every cosine query casts to the
    profile's dim so the per-profile expression index is the one the planner
    can use."""

    def test_upsert_refuses_a_vector_whose_length_is_not_the_profile_dim(self) -> None:
        cur = mock.Mock()
        with self.assertRaises(RuntimeError) as ctx:
            embed._upsert_chunks(cur, VOYAGE.id, [("topic", "t", 0, "x", "h", [0.1] * 768)])
        self.assertIn("length 768", str(ctx.exception))
        cur.execute.assert_not_called()

    def test_upsert_accepts_the_right_width_for_a_non_768_profile(self) -> None:
        cur = mock.Mock()
        embed._upsert_chunks(cur, VOYAGE.id, [("topic", "t", 0, "x", "h", [0.1] * 1024)])
        self.assertEqual(cur.execute.call_count, 1)

    def test_an_unknown_profile_is_not_length_checked_here(self) -> None:
        cur = mock.Mock()
        embed._upsert_chunks(cur, "prof-unseen", [("topic", "t", 0, "x", "h", [0.1, 0.2])])
        self.assertEqual(cur.execute.call_count, 1)

    def test_the_cosine_query_casts_both_sides_to_the_query_dim(self) -> None:
        executed: list[str] = []

        class _Cur:
            def execute(self, sql, params=None):
                executed.append(" ".join(sql.split()))

            def fetchone(self):
                return (False,)

            def fetchall(self):
                return []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _Conn(_Cur):
            def cursor(self):
                return _Cur()

        with mock.patch("khipu.db.connect", return_value=_Conn()), \
             mock.patch.object(embed, "_active_profile", return_value=VOYAGE.id), \
             mock.patch.object(embed, "_query_vec", return_value=([0.1] * 1024, "off")):
            embed._cosine_candidates("hello", limit=5)
        sql = next(q for q in executed if "FROM memory_embeddings m" in q)
        self.assertIn("1 - (m.embedding::vector(1024) <=> %(q)s::vector(1024)) AS score", sql)
        self.assertIn("ORDER BY m.embedding::vector(1024) <=> %(q)s::vector(1024)", sql)
        self.assertIn("WHERE m.profile = %(p)s", sql)

    def test_backfill_and_activate_no_longer_refuse_other_dims(self) -> None:
        import inspect

        self.assertNotIn("vector(768)", inspect.getsource(embed.activate))
        self.assertNotIn("is not dim", inspect.getsource(embed.backfill))
        self.assertIn("ensure_profile_index", inspect.getsource(embed.activate))
        self.assertIn("ensure_profile_index", inspect.getsource(embed.backfill))

    def test_activate_builds_the_index_before_flipping_the_pointer(self) -> None:
        order: list[str] = []

        class _Cur:
            def execute(self, sql, params=None):
                s = " ".join(sql.split())
                if s.startswith("UPDATE embedding_profiles SET is_active = true"):
                    order.append("flip")

            def fetchone(self):
                return (VOYAGE.id,)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _Conn(_Cur):
            def cursor(self):
                return _Cur()

            def commit(self):
                pass

        cov = {"episodes": {"missing": 0}, "topics": {"missing": 0}}
        with mock.patch("khipu.db.connect", return_value=_Conn()), \
             mock.patch.object(embed, "coverage", return_value=cov), \
             mock.patch.object(
                 embed._profiles, "ensure_profile_index",
                 side_effect=lambda cur, p, t, dim=None, quiet=False: order.append(f"index:{t}:{dim}:{quiet}")):
            out = embed.activate(VOYAGE.id)
        self.assertEqual(order, ["index:memory_embeddings:1024:True", "flip"])
        self.assertTrue(out["ok"])


if __name__ == "__main__":
    unittest.main()
