from __future__ import annotations

import io
import json
import multiprocessing
import sqlite3
import tempfile
import unittest
import urllib.error
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from khipu import embed
from khipu.profiles import ProfileSpec


def _budget_racer(path: str, start, ready, result) -> None:
    """Force the old split read/reserve path to take the same stale snapshot."""
    original = embed.budget_status

    def synchronized_status():
        status = original()
        import time
        time.sleep(0.1)
        return status

    try:
        ready.put(1)
        start.wait(10)
        with mock.patch.object(embed, "_budget_path", return_value=Path(path)), \
                mock.patch.object(embed, "budget_status", side_effect=synchronized_status), \
                mock.patch.object(embed, "EMBED_DAILY_CALL_BUDGET", 1):
            embed._budget_take()
        result.put("accepted")
    except RuntimeError:
        result.put("rejected")


class ProviderDiagnosticsTest(unittest.TestCase):
    def test_text_http_error_redacts_credential_echo(self) -> None:
        err = urllib.error.HTTPError(
            "https://example.invalid", 401, "no", {},
            io.BytesIO(b"Authorization: Bearer text-secret and key=text-secret"),
        )
        with mock.patch.object(embed, "_budget_take"), self.assertRaises(RuntimeError) as ctx:
            embed._post_json(
                "https://example.invalid", {}, {"Authorization": "Bearer text-secret"},
                provider="voyage", retries=0, timeout=1, delay=0,
                transport=mock.Mock(side_effect=err),
            )
        self.assertNotIn("text-secret", str(ctx.exception))

    def test_image_http_error_redacts_credential_echo(self) -> None:
        err = urllib.error.HTTPError(
            "https://example.invalid", 401, "no", {},
            io.BytesIO(b"x-goog-api-key: image-secret"),
        )
        with mock.patch.object(embed, "_budget_take"), \
                mock.patch.object(embed, "_gemini_key", return_value="image-secret"), \
                mock.patch.object(embed.urllib.request, "urlopen", side_effect=err), \
                self.assertRaises(RuntimeError) as ctx:
            embed.embed_batch_images([(b"png", "image/png")], retries=0)
        self.assertNotIn("image-secret", str(ctx.exception))

    def test_header_credential_stays_redacted_when_memory_redaction_is_disabled(self) -> None:
        with mock.patch.dict("os.environ", {"KHIPU_REDACT_SECRETS": "0"}):
            self.assertNotIn(
                "header-secret",
                embed._redact_provider_diagnostic(
                    "provider echoed header-secret", {"Authorization": "Bearer header-secret"}
                ),
            )


class QueryIdentityTest(unittest.TestCase):
    def test_negation_and_direction_are_distinct_provider_inputs(self) -> None:
        self.assertNotEqual(
            embed._query_hash(embed.PROFILE_2, embed.prefix_query("do not deploy west")),
            embed._query_hash(embed.PROFILE_2, embed.prefix_query("deploy west")),
        )
        self.assertNotEqual(
            embed._query_hash(embed.PROFILE_2, embed.prefix_query("ship to east")),
            embed._query_hash(embed.PROFILE_2, embed.prefix_query("ship from east")),
        )

    def test_case_and_whitespace_are_distinct_provider_inputs(self) -> None:
        self.assertNotEqual(
            embed._query_hash(embed.PROFILE_2, "Need exact Case"),
            embed._query_hash(embed.PROFILE_2, "need exact case"),
        )
        self.assertNotEqual(
            embed._query_hash(embed.PROFILE_2, "one space"),
            embed._query_hash(embed.PROFILE_2, "one  space"),
        )

    def test_query_cache_misses_send_each_exact_provider_input(self) -> None:
        class Cur:
            def execute(self, *_args, **_kwargs):
                pass

            def fetchone(self):
                return (None,)

        class Conn:
            def rollback(self):
                pass

        seen = []
        def record(text, **_kwargs):
            seen.append(text)
            return [1.0] * embed.DIM

        with mock.patch.object(embed, "embed_one", side_effect=record):
            embed._query_vec(Cur(), Conn(), embed.PROFILE_2, "Ship EAST")
            embed._query_vec(Cur(), Conn(), embed.PROFILE_2, "ship east")
        self.assertEqual(seen, ["Ship EAST", "ship east"])

    def test_invalid_cached_vectors_fall_through_and_are_replaced(self) -> None:
        class Cur:
            def __init__(self, cached): self.cached, self.query = cached, ""
            def execute(self, sql, params=None): self.query = " ".join(sql.split())
            def fetchone(self):
                if "to_regclass" in self.query:
                    return (True,)
                if "SELECT embedding::text" in self.query:
                    return (self.cached,)
                return None
        class Conn:
            def commit(self): pass
            def rollback(self): pass
        spec = ProfileSpec("cache@2", "voyage", "cache", 2, "none")
        for cached in ("[0,0]", "[NaN,1]", "[1]", '["1","2"]'):
            with self.subTest(cached=cached), \
                    mock.patch.object(embed, "resolve_spec", return_value=spec), \
                    mock.patch.object(embed, "embed_one", return_value=[1.0, 0.0]) as provider:
                vec, state = embed._query_vec(Cur(cached), Conn(), spec.id, "query")
            self.assertEqual((vec, state), ([1.0, 0.0], "miss"))
            provider.assert_called_once()


class TopicChunkIdentityTest(unittest.TestCase):
    def test_equal_heading_occurrences_and_long_sections_have_unique_indexes(self) -> None:
        text = "## Repeat\nfirst\n## Repeat\nsecond\n## Long\n" + "x" * (embed.CHUNK_CHARS * 1001)
        chunks = embed.chunk_text_indexed(text)
        indexes = [idx for idx, _chunk in chunks]
        self.assertEqual(len(indexes), len(set(indexes)))
        self.assertGreater(len(chunks), 1000)

    def test_hash_bucket_collision_probes_to_a_distinct_index_range(self) -> None:
        with mock.patch.object(embed, "_section_bucket", return_value=7):
            indexes = [idx for idx, _chunk in embed.chunk_text_indexed("## One\na\n## Two\nb")]
        self.assertEqual(len(indexes), len(set(indexes)))

    def test_unrepresentable_section_is_rejected_before_indexes_overlap(self) -> None:
        with mock.patch.object(embed, "SECTION_HASH_SPACE", 2), \
                mock.patch.object(embed, "SECTION_LOCAL_SPACE", 2), \
                mock.patch.object(embed, "_window", return_value=["x"] * 5):
            with self.assertRaisesRegex(ValueError, "too many chunks"):
                embed.chunk_text_indexed("## One\na\n## Two\nb")

    def test_existing_unique_heading_keeps_legacy_bucket_identity(self) -> None:
        idx, _ = embed.chunk_text_indexed("## Stable\nbody\n## Other\nbody")[0]
        self.assertEqual(idx, embed._section_bucket("stable") * 1_000)

    def test_persistence_rejects_duplicate_chunk_indexes(self) -> None:
        cur = mock.Mock()
        rows = [
            ("topic", "a", 1, "first", "h1", [1.0]),
            ("topic", "a", 1, "second", "h2", [1.0]),
        ]
        with self.assertRaisesRegex(ValueError, "duplicate embedding chunk indexes"):
            embed._upsert_chunks(cur, "unknown@1", rows)
        cur.execute.assert_not_called()


class GeminiDimensionTest(unittest.TestCase):
    def test_gemini_request_uses_profile_dimension(self) -> None:
        spec = ProfileSpec("gemini-test@1536", "gemini", "gemini-test", 1536)
        with mock.patch.object(embed, "_gemini_key", return_value="key"):
            _url, body, _headers = embed._build_gemini(spec, ["text"], None)
        self.assertEqual(body["requests"][0]["outputDimensionality"], 1536)


class _BackfillCursor:
    def __init__(self, rows):
        self.rows, self.rowcount, self._result = rows, 0, []

    def execute(self, sql, params=None):
        statement = " ".join(sql.split())
        if statement.startswith("SELECT id FROM embedding_profiles"):
            self._result = [("prof-1",)]
        elif statement.startswith("SELECT kind, ref, chunk_idx, content_hash"):
            self._result = []
        elif statement.startswith("SELECT slug, title, body FROM topics"):
            self._result = self.rows
        elif statement.startswith("SELECT slug FROM topics") and (
                "FOR SHARE" in statement or "slug = ANY" in statement):
            self._result = [(slug,) for slug, _title, _body in self.rows if slug in params[0]]
        elif statement.startswith("DELETE FROM memory_embeddings"):
            self.rowcount = 0
        else:
            self._result = []

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _BackfillConn:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self):
        return self.cur

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class BackfillOutcomeTest(unittest.TestCase):
    def test_actual_mixed_and_failed_backfills_report_outcomes(self) -> None:
        rows = [(f"topic-{i}", "title", "body") for i in range(embed.BATCH + 1)]

        def run(reply):
            cur = _BackfillCursor(rows)
            with mock.patch("khipu.db.connect", return_value=_BackfillConn(cur)), \
                    mock.patch.object(embed, "embed_batch", side_effect=reply), \
                    mock.patch.object(embed.time, "sleep"):
                return embed.backfill(kind="topic")

        calls = 0
        def mixed(api, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("embed HTTP 500")
            return [[1.0] * embed.DIM for _ in api]

        mixed_stats = run(mixed)
        self.assertEqual((mixed_stats["outcome"], mixed_stats["ok"]), ("partial", False))
        failed_stats = run(lambda _api, **_kwargs: (_ for _ in ()).throw(RuntimeError("embed HTTP 500")))
        self.assertEqual((failed_stats["outcome"], failed_stats["ok"]), ("failed", False))


class VectorValidationTest(unittest.TestCase):
    def _batch(self, values):
        spec = ProfileSpec("strict@2", "voyage", "strict", 2, "none")
        payload = json.dumps({"data": [{"index": 0, "embedding": values}]}).encode()
        with mock.patch.object(embed, "resolve_spec", return_value=spec), \
                mock.patch.object(embed, "_build_voyage", wraps=embed._build_voyage), \
                mock.patch.dict(embed._BUILDERS, {"voyage": embed._build_voyage}), \
                mock.patch.object(embed, "_voyage_key", return_value="key"), \
                mock.patch.object(embed, "_budget_take"):
            return embed.embed_batch(["text"], profile=spec.id, retries=0,
                                     transport=lambda *_args: payload)

    def test_actual_embed_rejects_strings_null_nonfinite_and_zero(self) -> None:
        for bad in (["1", "2"], [None, 1], [float("nan"), 1],
                    [float("inf"), 1], [0, 0]):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                self._batch(bad)
        self.assertEqual(self._batch([1, 2]), [[1.0, 2.0]])

    def test_parser_sorts_then_validates_provider_items(self) -> None:
        payload = {"data": [
            {"index": 1, "embedding": [3, 4]},
            {"index": 0, "embedding": [1, 2]},
        ]}
        self.assertEqual(embed._parse_vectors("voyage", payload), [[1.0, 2.0], [3.0, 4.0]])
        for bad in ({"data": [{"embedding": None}]},
                    {"data": [{"embedding": ["1", "2"]}]},
                    {"data": [{"embedding": [0, 0]}]}):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                embed._parse_vectors("voyage", bad)

    def test_check_key_rejects_a_malformed_provider_vector(self) -> None:
        from khipu import embed_ops

        with mock.patch.dict(embed_ops.embed._BUILDERS, {
                "voyage": lambda *_args: ("https://invalid", {}, {})}), \
                mock.patch.object(embed_ops.embed, "_post_json",
                                  return_value={"data": [{"embedding": ["1", "2"]}]}):
            out = embed_ops.check_key("voyage", model="v")
        self.assertFalse(out["ok"])


class ProviderInputRedactionTest(unittest.TestCase):
    def test_embed_transport_never_receives_vendor_key_or_dsn(self) -> None:
        spec = ProfileSpec("strict@2", "voyage", "strict", 2, "none")
        seen = []
        payload = json.dumps({"data": [{"embedding": [1, 2]}]}).encode()

        def transport(_url, data, _headers, _timeout):
            seen.extend(json.loads(data)["input"])
            return payload

        text = "api_key=sk-secretvalue dsn=postgres://user:pass@example/db"
        with mock.patch.object(embed, "resolve_spec", return_value=spec), \
                mock.patch.dict(embed._BUILDERS, {"voyage": embed._build_voyage}), \
                mock.patch.object(embed, "_voyage_key", return_value="key"), \
                mock.patch.object(embed, "_budget_take"):
            embed.embed_batch([text], profile=spec.id, retries=0, transport=transport)
        self.assertNotIn("sk-secretvalue", seen[0])
        self.assertNotIn("postgres://user:pass@example/db", seen[0])

    def test_outgoing_memory_redaction_optout_is_preserved(self) -> None:
        raw = "api_key=sk-user-opted-out"
        with mock.patch.dict("os.environ", {"KHIPU_REDACT_SECRETS": "0"}):
            self.assertEqual(embed._sanitize_provider_text(raw), raw)

    def test_query_cache_hashes_the_same_sanitized_text_sent_to_provider(self) -> None:
        class Cur:
            def __init__(self):
                self.calls = []
            def execute(self, sql, params=None):
                self.sql = sql
                self.calls.append((sql, params))
            def fetchone(self):
                return (True,) if "to_regclass" in self.sql else None
        cur, conn, seen = Cur(), mock.Mock(), []
        raw = "api_key=sk-querysecret"
        with mock.patch.object(embed, "embed_one", side_effect=lambda text, **_: seen.append(text) or [1.0]):
            _vec, state = embed._query_vec(cur, conn, embed.PROFILE_2, raw)
        self.assertEqual(state, "miss")
        self.assertNotIn("sk-querysecret", seen[0])
        import hashlib
        expected = hashlib.md5(f"{embed.QUERY_CACHE_SCHEME}\n{embed.PROFILE_2}\nquery\n{seen[0]}".encode()).hexdigest()
        keys = [params[1] for sql, params in cur.calls if sql.startswith(("SELECT embedding", "INSERT INTO memory_query_cache"))]
        self.assertEqual(keys, [expected, expected])


class BudgetReservationTest(unittest.TestCase):
    def test_rv3_budget_time_is_deducted_from_transport_timeout(self):
        transport = mock.Mock(return_value=b'{}')
        with mock.patch.object(embed, "_budget_take") as take, \
             mock.patch.object(embed.time, "monotonic", side_effect=[10., 10.25]):
            embed._post_json("unused", {}, {}, provider="local", retries=0,
                             timeout=1., delay=0, transport=transport)
        take.assert_called_once_with(deadline=11., scope="memory")
        self.assertEqual(transport.call_args.args[-1], 0.75)

    def test_rv3_busy_budget_lock_expires_without_provider_call(self):
        import fcntl
        def lock(_fd, flags):
            if not flags & fcntl.LOCK_NB:
                raise AssertionError("blocking lock acquisition")
            raise BlockingIOError("held")
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(embed, "_budget_path", return_value=Path(tmp) / "budget.json"), \
             mock.patch.object(fcntl, "flock", side_effect=lock), \
             mock.patch.object(embed.time, "monotonic", side_effect=[0., 0., 0.02, 0.02]), \
             mock.patch.object(embed.time, "sleep"):
            transport = mock.Mock()
            with self.assertRaisesRegex(RuntimeError, "accounting.*deadline"):
                embed._post_json("unused", {}, {}, provider="local", retries=0,
                                 timeout=0.01, delay=0, transport=transport)
            transport.assert_not_called()

    def test_windows_lock_backend_imports_and_enforces_budget(self) -> None:
        import os
        import subprocess
        import sys
        import textwrap

        script = textwrap.dedent(
            """
            import builtins, json, os, sys, types
            from pathlib import Path

            real_import = builtins.__import__
            def guarded_import(name, *args, **kwargs):
                if name == "fcntl":
                    raise ImportError("fcntl unavailable")
                return real_import(name, *args, **kwargs)

            events = []
            msvcrt = types.ModuleType("msvcrt")
            msvcrt.LK_NBLCK = 1
            msvcrt.LK_UNLCK = 2
            msvcrt.locking = lambda _fd, mode, count: events.append([mode, count])
            sys.modules["msvcrt"] = msvcrt
            builtins.__import__ = guarded_import

            from khipu import embed
            embed.EMBED_DAILY_CALL_BUDGET = 2
            embed._budget_path = lambda scope="memory": Path(os.environ["AUDIT_BUDGET_PATH"])
            embed._budget_take()
            embed._budget_take()
            try:
                embed._budget_take()
            except RuntimeError as exc:
                error = str(exc)
            else:
                error = ""
            print(json.dumps({"status": embed.budget_status(), "error": error,
                              "events": events}))
            """
        )
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["AUDIT_BUDGET_PATH"] = str(Path(tmp) / "budget.json")
            proc = subprocess.run(
                [sys.executable, "-c", script], env=env,
                text=True, capture_output=True, timeout=30,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["status"]["calls"], 2)
        self.assertEqual(result["status"]["remaining"], 0)
        self.assertTrue(result["status"]["exhausted"])
        self.assertIn("budget exhausted", result["error"])
        self.assertEqual(result["events"], [[1, 1], [2, 1]] * 3)

    def test_parallel_processes_reserve_atomically(self) -> None:
        ctx = multiprocessing.get_context("fork")
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "budget.json")
            start, ready, result = ctx.Event(), ctx.Queue(), ctx.Queue()
            workers = [ctx.Process(target=_budget_racer, args=(path, start, ready, result))
                       for _ in range(6)]
            for worker in workers:
                worker.start()
            for _ in workers:
                ready.get(timeout=10)
            start.set()
            outcomes = [result.get(timeout=10) for _ in workers]
            for worker in workers:
                worker.join(10)
                self.assertEqual(worker.exitcode, 0)
            self.assertEqual(outcomes.count("accepted"), 1)
            self.assertEqual(json.loads(Path(path).read_text())["calls"], 1)

    def test_malformed_and_nonobject_accounting_is_unavailable_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "budget.json"
            for raw in ("{", "[]"):
                path.write_text(raw)
                with mock.patch.object(embed, "_budget_path", return_value=path), \
                        mock.patch.object(embed, "EMBED_DAILY_CALL_BUDGET", 2):
                    status = embed.budget_status()
                    self.assertFalse(status["available"])
                    with self.assertRaisesRegex(RuntimeError, "accounting unavailable"):
                        embed._budget_take()

    def test_unlimited_budget_repairs_corrupt_accounting_and_still_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "budget.json"
            path.write_text("[]")
            with mock.patch.object(embed, "_budget_path", return_value=path), \
                    mock.patch.object(embed, "EMBED_DAILY_CALL_BUDGET", 0):
                embed._budget_take()
                status = embed.budget_status()
            self.assertTrue(status["unlimited"])
            self.assertTrue(status["available"])
            self.assertEqual(status["calls"], 1)

    def test_unavailable_accounting_never_claims_zero_usage_or_remaining_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "budget.json"
            path.write_text("[]")
            with mock.patch.object(embed, "_budget_path", return_value=path), \
                    mock.patch.object(embed, "EMBED_DAILY_CALL_BUDGET", 2):
                status = embed.budget_status()
        self.assertFalse(status["available"])
        self.assertTrue(status["exhausted"])
        self.assertIsNone(status["calls"])
        self.assertIsNone(status["remaining"])


class ActivationCoverageTest(unittest.TestCase):
    def test_activate_refuses_stale_chunk_even_when_every_ref_has_chunk_zero(self) -> None:
        cov = {"episodes": {"missing": 0}, "topics": {"missing": 0},
               "missing": 0, "stale": 1}
        cur = mock.Mock()
        cur.fetchone.return_value = (embed.PROFILE_2,)
        conn = mock.MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        with mock.patch("khipu.db.connect", return_value=conn), \
                mock.patch.object(embed, "coverage", return_value=cov) as coverage:
            with self.assertRaisesRegex(RuntimeError, "stale"):
                embed.activate(embed.PROFILE_2)
        coverage.assert_called_once_with(profile=embed.PROFILE_2, detail=True)


class _PruneCursor:
    def __init__(self):
        self.rowcount = 0
        self.deletes = []
    def execute(self, sql, params=None):
        if "NOT (chunk_idx = ANY" in " ".join(sql.split()):
            self.deletes.append(params)
            self.rowcount = 2
        else:
            self.rowcount = 0
    def fetchone(self): return ("prof",)
    def fetchall(self): return []
    def __enter__(self): return self
    def __exit__(self, *_): return False


class _Conn:
    def __init__(self, cur): self.cur = cur
    def cursor(self): return self.cur
    def commit(self): pass
    def __enter__(self): return self
    def __exit__(self, *_): return False


class BackfillTailPruneTest(unittest.TestCase):
    @contextmanager
    def _bulk(self, *_args, **_kwargs):
        yield mock.Mock()

    def _run(self, failing=False, chunks=None, old=None):
        cur = _PruneCursor()
        old = old if old is not None else {
            ("episode", "7", 0): "old", ("episode", "7", 1): "tail1",
            ("episode", "7", 2): "tail2"}
        with mock.patch("khipu.db.connect", return_value=_Conn(cur)), \
                mock.patch.object(embed, "_resolve_profile", return_value="prof"), \
                mock.patch.object(embed, "_existing_hashes", return_value=old), \
                mock.patch.object(embed, "_iter_sources", return_value=iter([("episode", "7", "new", "")])), \
                mock.patch.object(embed, "_chunks_for", return_value=(chunks if chunks is not None else [(0, "new")])), \
                mock.patch.object(embed, "embed_batch", side_effect=(RuntimeError("fail") if failing else [[1.0]])), \
                mock.patch.object(embed, "_upsert_chunks"), \
                mock.patch.object(embed._profiles, "BulkLoad", self._bulk), \
                mock.patch.object(embed._profiles, "ensure_profile_index"):
            embed.backfill(kind="episode", profile="prof")
        return cur

    def test_successful_replacement_prunes_old_episode_tail(self) -> None:
        cur = self._run()
        self.assertEqual(cur.deletes[0][1:], ("episode", "7", [0]))

    def test_failed_replacement_preserves_old_episode_vectors(self) -> None:
        self.assertEqual(self._run(failing=True).deletes, [])

    def test_successful_empty_replacement_prunes_every_old_chunk(self) -> None:
        cur = self._run(chunks=[])
        self.assertEqual(cur.deletes[0][3], [-1])

    def test_a_source_with_no_stale_index_issues_no_delete(self) -> None:
        # The steady state of every nightly: nothing to prune, so no round trip.
        self.assertEqual(self._run(old={("episode", "7", 0): "old"}).deletes, [])
        self.assertEqual(self._run(old={}).deletes, [])
        # An unrelated source's stored indexes never trigger a delete here.
        self.assertEqual(self._run(old={("episode", "9", 4): "x"}).deletes, [])


class MediaLimitTest(unittest.TestCase):
    def test_limit_counts_changed_images_after_unchanged_skips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = [root / "a.png", root / "b.png"]
            for i, path in enumerate(paths):
                path.write_bytes(f"png-{i}".encode())
            state, flushed = {}, []
            cur = mock.MagicMock()
            cur.__enter__.return_value = cur
            cur.fetchone.side_effect = lambda: ((state.get("last"),) if state.get("last") else None)

            def execute(_sql, params=None):
                rel = params[1] if params else None
                state["last"] = rel if rel in state else None
            cur.execute.side_effect = execute
            def hashes(_cur, _profile):
                return {("media", rel, 0): sha for rel, sha in state.items() if rel != "last"}
            def flush(_cur, _profile, batch, stats):
                for _sid, _path, rel, _mime, sha, _raw in batch:
                    state[rel] = sha
                    flushed.append(rel)
                    stats["embedded"] += 1
            conn = _Conn(cur)
            patches = (
                mock.patch("khipu.db.connect", return_value=conn),
                mock.patch("khipu.sources.sources_with_embed_media", return_value=[{"id": "s", "root": str(root)}]),
                mock.patch.object(embed, "_iter_media_files", side_effect=lambda _root: iter(paths)),
                mock.patch.object(embed, "_resolve_profile", return_value=embed.PROFILE_2),
                mock.patch.object(embed, "_existing_hashes", side_effect=hashes),
                mock.patch.object(embed, "_flush_media_batch", side_effect=flush),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                embed.backfill_media(limit=1, yes=True)
                embed.backfill_media(limit=1, yes=True)
            self.assertEqual(flushed, ["a.png", "b.png"])


class SearchValidityTest(unittest.TestCase):
    def test_missing_and_tombstoned_topics_never_reach_ranking(self) -> None:
        class Cur:
            def execute(self, sql, params=None):
                self.sql = " ".join(sql.split())
            def fetchall(self):
                return [("live", None, "active", None, None, None)]
        rows = [{"kind": "topic", "id": "live", "score": 1.0},
                {"kind": "topic", "id": "gone", "score": .9}]
        with mock.patch.object(embed, "_episode_schema_flags", return_value={
                "project": True, "deleted_at": True, "harness": True, "parent_session_id": True}):
            out = embed._apply_search_filters(Cur(), rows)
        self.assertEqual([r["id"] for r in out], ["live"])

    def test_cosine_shortlist_requires_live_episode_and_topic_sources(self) -> None:
        class Cur:
            def __init__(self):
                self.sql = ""
                self.result = []
            def execute(self, sql, params=None):
                self.sql = " ".join(sql.split())
                self.result = [(False,)] if "to_regclass" in self.sql else []
            def fetchone(self): return self.result[0] if self.result else None
            def fetchall(self): return list(self.result)
            def __enter__(self): return self
            def __exit__(self, *_): return False
        cur = Cur()
        with mock.patch("khipu.db.connect", return_value=_Conn(cur)), \
                mock.patch.object(embed, "_active_profile", return_value="prof"), \
                mock.patch.object(embed, "_query_vec", return_value=([1.0, 0.0], "hit")), \
                mock.patch.object(embed, "_episode_schema_flags", return_value={
                    "project": True, "deleted_at": True, "harness": True,
                    "parent_session_id": True}):
            embed._cosine_candidates("query", limit=5)
        self.assertIn("t.deleted_at IS NULL", cur.sql)
        self.assertIn("e.deleted_at IS NULL", cur.sql)

    def test_deleted_topic_never_reaches_actual_hybrid_reranker(self) -> None:
        rows = [{"kind": "topic", "id": "live", "rank_text": "needle", "score": 1.0},
                {"kind": "topic", "id": "gone", "rank_text": "needle", "score": .9}]
        class Cur:
            def execute(self, sql, params=None): self.sql = " ".join(sql.split())
            def fetchall(self):
                if "FROM topics WHERE deleted_at IS NULL" in self.sql:
                    return [("live", None, "active", None, None, None)]
                return []
            def __enter__(self): return self
            def __exit__(self, *_): return False
        class Conn:
            def cursor(self): return Cur()
        @contextmanager
        def hub(): yield Conn()
        reranked = []
        def stage(_query, candidates, **_kwargs):
            reranked.extend(r["id"] for r in candidates)
            return candidates, {"applied": True}, False
        with mock.patch("khipu.hub_snapshot.try_hub_connect", hub), \
                mock.patch("khipu.cli._literal_candidates", return_value=rows), \
                mock.patch("khipu.topic_graph.enrich_search_results", side_effect=lambda _c, r: r), \
                mock.patch("khipu.rerank.stage", side_effect=stage), \
                mock.patch("khipu.recency.apply_project_and_status", side_effect=lambda r, project=None: r), \
                mock.patch("khipu.recency.apply_recency", side_effect=lambda r: r), \
                mock.patch.object(embed, "_episode_schema_flags", return_value={
                    "project": True, "deleted_at": True, "harness": True,
                    "parent_session_id": True}), \
                mock.patch("khipu.features.enabled", side_effect=lambda name: name == "rerank"):
            embed.hybrid_search("needle", mode="literal", limit=2,
                                include_libraries=False)
        self.assertEqual(reranked, ["live"])


class HybridOrderingTest(unittest.TestCase):
    def test_hybrid_applies_status_without_project_and_fair_fills_after_enrichment(self) -> None:
        row = {"kind": "topic", "id": "x", "rank_text": "needle", "score": 1.0}
        class Cur:
            def __enter__(self): return self
            def __exit__(self, *_): return False
        class Conn:
            def cursor(self): return Cur()
        @contextmanager
        def hub(): yield Conn()
        status_calls = []
        def status(rows, project=None):
            status_calls.append(project)
            return rows
        def enrich(_cur, rows):
            return [{**r, "enriched": True} for r in rows]
        def fair(rows, limit):
            self.assertTrue(all(r.get("enriched") for r in rows))
            return rows[:limit]
        with mock.patch("khipu.hub_snapshot.try_hub_connect", hub), \
                mock.patch("khipu.cli._literal_candidates", return_value=[row]), \
                mock.patch("khipu.topic_graph.enrich_search_results", side_effect=enrich), \
                mock.patch("khipu.recency.apply_project_and_status", side_effect=status), \
                mock.patch("khipu.recency.apply_recency", side_effect=lambda rows: rows), \
                mock.patch.object(embed, "_apply_search_filters", side_effect=lambda _c, rows, **_k: rows), \
                mock.patch.object(embed, "_fair_fill", side_effect=fair), \
                mock.patch("khipu.features.enabled", return_value=False):
            out = embed.hybrid_search("needle", mode="literal", limit=1,
                                      include_libraries=False)
        self.assertEqual(status_calls, [None])
        self.assertTrue(out["results"][0]["enriched"])

    def test_short_library_literal_match_survives_default_evidence_filter(self) -> None:
        row = {"kind": "library", "id": "library:docs:1#0", "source": "docs",
               "rank_text": "AI", "score": 1.0}
        class Cur:
            def __enter__(self): return self
            def __exit__(self, *_): return False
        class Conn:
            def cursor(self): return Cur()
        @contextmanager
        def hub(): yield Conn()
        with mock.patch("khipu.hub_snapshot.try_hub_connect", hub), \
                mock.patch("khipu.cli._literal_candidates", return_value=[]), \
                mock.patch("khipu.library_search.library_candidates",
                           return_value=([], [row], ["docs"])), \
                mock.patch("khipu.topic_graph.enrich_search_results", side_effect=lambda _c, rows: rows), \
                mock.patch("khipu.recency.apply_project_and_status", side_effect=lambda rows, project=None: rows), \
                mock.patch("khipu.recency.apply_recency", side_effect=lambda rows: rows), \
                mock.patch.object(embed, "_apply_search_filters", side_effect=lambda _c, rows, **_k: rows), \
                mock.patch("khipu.features.enabled", return_value=False):
            out = embed.hybrid_search("AI", mode="literal", limit=1)
        self.assertEqual([r["id"] for r in out["results"]], ["library:docs:1#0"])

    def test_validity_ranking_runs_before_limit_and_current_episode_wins(self) -> None:
        rows = [{"kind": "episode", "id": "A", "score": .10, "rank_text": "needle"},
                {"kind": "episode", "id": "B", "score": .09, "rank_text": "needle"}]
        class Cur:
            def __enter__(self): return self
            def __exit__(self, *_): return False
        class Conn:
            def cursor(self): return Cur()
        @contextmanager
        def hub(): yield Conn()
        def decisions(_cur, fused):
            return [{**r,
                     "decisions_current": 0 if r["id"] == "A" else 1,
                     "decisions_superseded": 1 if r["id"] == "A" else 0,
                     "decisions_retracted": 0} for r in fused]
        with mock.patch("khipu.hub_snapshot.try_hub_connect", hub), \
                mock.patch("khipu.cli._literal_candidates", return_value=rows), \
                mock.patch("khipu.search_text.fuse_ranked_lists", return_value=rows), \
                mock.patch("khipu.topic_graph.enrich_search_results", side_effect=lambda _c, r: r), \
                mock.patch("khipu.decisions.enrich_search_results", side_effect=decisions), \
                mock.patch("khipu.recency.apply_project_and_status", side_effect=lambda r, project=None: r), \
                mock.patch("khipu.recency.apply_recency", side_effect=lambda r: r), \
                mock.patch.object(embed, "_apply_search_filters", side_effect=lambda _c, r, **_k: r), \
                mock.patch("khipu.features.enabled", side_effect=lambda name: name == "validity_ranking"):
            out = embed.hybrid_search("needle", mode="literal", limit=1,
                                      include_libraries=False)
        self.assertEqual([r["id"] for r in out["results"]], ["B"])


class CatchupStalenessTest(unittest.TestCase):
    def test_topics_lag_includes_stale_current_chunks(self) -> None:
        from datetime import datetime, timezone
        class Cur:
            def execute(self, sql, params=None): self.sql, self.params = " ".join(sql.split()), params
            def fetchone(self): return (datetime(2020, 1, 1, tzinfo=timezone.utc),)
            def __enter__(self): return self
            def __exit__(self, *_): return False
        cur = Cur()
        plan = {("topic", "stale", 0): ("new", 10)}
        with mock.patch("khipu.db.connect", return_value=_Conn(cur)), \
                mock.patch.object(embed, "_resolve_profile", return_value="prof"), \
                mock.patch.object(embed, "memory_gaps", return_value=(plan, [], [("topic", "stale", 0)])):
            out = embed.topics_embed_lag_minutes(profile="prof")
        self.assertFalse(out["ok"])
        self.assertIn("slug = ANY", cur.sql)
        self.assertEqual(cur.params, ("prof", ["stale"], "prof"))

    def test_recent_catchup_selects_stale_topic_even_when_a_vector_exists(self) -> None:
        class Cur:
            def __init__(self):
                self.result = []
                self.topic_sql = []
            def execute(self, sql, params=None):
                text = " ".join(sql.split())
                if text.startswith("SELECT 1 FROM topics") and "FOR SHARE" in text:
                    self.result = [(1,)]  # the topic is still live
                elif text.startswith("SELECT kind, ref, chunk_idx"):
                    self.result = [("topic", "stale", 0, "old")]
                elif "FROM episodes e" in text or "FROM commitments c" in text:
                    self.result = []
                elif "JOIN episodes e ON e.topics" in text:
                    self.result = []
                elif text.startswith("SELECT slug FROM topics WHERE slug = ANY"):
                    self.result = [(s,) for s in params[0]]
                elif "FROM topics t" in text:
                    self.topic_sql.append(text)
                    self.result = [("stale", "Title", "new body", {"slug": "stale", "title": "Title", "body": "new body"})] if "NOT EXISTS" not in text else []
                else:
                    self.result = []
            def fetchall(self): return list(self.result)
            def fetchone(self): return self.result[0] if self.result else None
            def __enter__(self): return self
            def __exit__(self, *_): return False
        cur = Cur()
        with mock.patch("khipu.db.connect", return_value=_Conn(cur)), \
                mock.patch.object(embed, "_active_profile", return_value="prof"), \
                mock.patch.object(embed, "embed_batch", return_value=[[1.0] * embed.DIM]), \
                mock.patch.object(embed, "_upsert_chunks") as upsert, \
                mock.patch("khipu.hub_snapshot.upsert_embeddings", return_value={"ok": True}):
            out = embed.embed_recent_missing(limit=1)
        self.assertEqual(out["topics_embedded"], 1)
        self.assertTrue(any("NOT EXISTS" not in sql for sql in cur.topic_sql))
        upsert.assert_called_once()

    def test_partial_episode_with_current_first_chunk_embeds_missing_tail_across_batches(self) -> None:
        class Cur:
            def __init__(self): self.result = []
            def execute(self, sql, params=None):
                text = " ".join(sql.split())
                if text.startswith("SELECT id FROM episodes") and "FOR SHARE" in text:
                    self.result = [(7,)] if 7 in params[0] else []
                elif "FROM episodes e" in text and "NOT EXISTS" not in text:
                    self.result = [(7, "summary", [], [], [], [])]
                else:
                    self.result = []
            def fetchall(self): return list(self.result)
            def __enter__(self): return self
            def __exit__(self, *_): return False
        chunks = [f"c{i}" for i in range(embed.BATCH + 1)]
        calls = []
        def provider(api, **_kwargs):
            calls.append(len(api))
            return [[1.0] * embed.DIM for _ in api]
        with mock.patch("khipu.db.connect", return_value=_Conn(Cur())), \
                mock.patch("khipu.db.has_columns", return_value=False), \
                mock.patch.object(embed, "_active_profile", return_value="prof"), \
                mock.patch.object(embed, "_existing_hashes_for_refs",
                                  side_effect=lambda _c, _p, kind, _r: ({("episode", "7", 0): embed._md5("c0")} if kind == "episode" else {})), \
                mock.patch.object(embed, "chunk_text", return_value=chunks), \
                mock.patch.object(embed, "embed_batch", side_effect=provider), \
                mock.patch.object(embed, "_upsert_chunks") as upsert:
            out = embed.embed_recent_missing(limit=1)
        self.assertEqual(out["chunks"], embed.BATCH + 1)
        self.assertEqual(calls, [embed.BATCH, 1])
        self.assertEqual(upsert.call_count, 2)


class CaptureEmbeddingConsistencyTest(unittest.TestCase):
    class Cur:
        def __init__(self, row): self.row, self.result = row, None
        def execute(self, sql, params=None):
            text = " ".join(sql.split())
            if "row_to_json(e)" in text:
                if "WHERE e.id" in text and self.row.get("deleted_at") is not None:
                    self.result = None
                else:
                    self.result = (dict(self.row),)
            elif text.startswith("SELECT id FROM episodes"):
                self.result = (self.row["id"],)
            else:
                self.result = None
        def fetchone(self): return self.result
        def __enter__(self): return self
        def __exit__(self, *_): return False

    def _row(self):
        return {"id": 7, "ts": "2026-01-01T00:00:00Z", "session_id": "s",
                "summary": "same summary", "topics": [], "people": [],
                "decisions": ["committed decision"], "preferences": [], "scope": "scope",
                "raw": {"source": "hub"}, "harness": "codex", "repo_root": "/repo",
                "project": "committed/project", "parent_session_id": "p",
                "transcript_range": "1-2", "deleted_at": None}

    def test_capture_uses_full_committed_row_for_vector_and_snapshot(self) -> None:
        cur, seen = self.Cur(self._row()), []
        payload = {"ts": cur.row["ts"], "summary": "same summary", "project": "payload/project"}
        with mock.patch("khipu.db.connect", return_value=_Conn(cur)), \
                mock.patch.object(embed, "_active_profile", return_value="prof"), \
                mock.patch.object(embed, "embed_batch", side_effect=lambda api, **_: seen.extend(api) or [[1.0]]), \
                mock.patch.object(embed, "_upsert_chunks"), \
                mock.patch("khipu.deliverables.deliverables_for_episode", return_value=[]), \
                mock.patch("khipu.hub_snapshot.upsert_episode", return_value={"ok": True}) as snapshot:
            self.assertTrue(embed.embed_on_capture(payload))
        self.assertIn("committed decision", seen[0])
        episode_row = snapshot.call_args.args[0]
        self.assertEqual(episode_row["project"], "committed/project")
        self.assertEqual(episode_row["raw"], {"source": "hub"})
        self.assertIn("deleted_at", episode_row)

    def test_capture_replica_keeps_committed_identity_filters(self) -> None:
        from khipu import hub_snapshot

        cur = self.Cur(self._row())
        payload = {"ts": cur.row["ts"], "summary": "same summary"}
        with tempfile.TemporaryDirectory() as td:
            data = Path(td)
            snapshot = data / "hub_snapshot.sqlite"
            con = sqlite3.connect(snapshot)
            hub_snapshot._create_schema(con)
            con.commit()
            con.close()
            with mock.patch.object(hub_snapshot, "snapshot_path", return_value=snapshot), \
                    mock.patch.object(hub_snapshot, "meta_path", return_value=data / "meta.json"), \
                    mock.patch("khipu.paths.ensure_data_dir", return_value=data), \
                    mock.patch("khipu.db.connect", return_value=_Conn(cur)), \
                    mock.patch.object(embed, "_active_profile", return_value="prof"), \
                    mock.patch.object(embed, "embed_batch", return_value=[[1.0]]), \
                    mock.patch.object(embed, "_upsert_chunks"), \
                    mock.patch("khipu.deliverables.deliverables_for_episode", return_value=[]):
                self.assertTrue(embed.embed_on_capture(payload))
                results = hub_snapshot.search_snapshot(
                    "same summary", 10, project="committed/project",
                    session_id="s", harness="codex",
                )
        self.assertEqual([r["id"] for r in results], ["7"])

    def test_deleted_during_provider_call_is_not_persisted_or_published(self) -> None:
        cur = self.Cur(self._row())
        def provider(_api, **_kwargs):
            cur.row["deleted_at"] = "now"
            return [[1.0]]
        with mock.patch("khipu.db.connect", return_value=_Conn(cur)), \
                mock.patch.object(embed, "_active_profile", return_value="prof"), \
                mock.patch.object(embed, "embed_batch", side_effect=provider), \
                mock.patch.object(embed, "_upsert_chunks") as upsert, \
                mock.patch("khipu.hub_snapshot.upsert_episode") as snapshot:
            self.assertFalse(embed.embed_on_capture({"ts": cur.row["ts"], "summary": "same summary"}))
        upsert.assert_not_called()
        snapshot.assert_not_called()


class ReplicaCatchupTest(unittest.TestCase):
    def _catchup(self, *, existing=False, empty=False):
        from khipu import hub_snapshot as hs
        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / "snapshot.sqlite"
            con = sqlite3.connect(snap)
            hs._create_schema(con)
            if existing:
                con.execute("INSERT INTO topics (slug, title, body) VALUES ('note', 'Title', 'old')")
                con.execute("INSERT INTO episodes (id, summary) VALUES (7, 'A')")
                for kind, ref in [("topic", "note"), ("episode", "7")]:
                    for i, text in enumerate(["A", "B"]):
                        con.execute("INSERT INTO memory_embeddings (profile,kind,ref,chunk_idx,chunk_text,embedding) VALUES ('prof',?,?,?,?,?)",
                                    (kind, ref, i, text, hs._vector_to_blob([1., 0.])))
                con.execute("INSERT INTO memory_embeddings (profile,kind,ref,chunk_idx,chunk_text,embedding) VALUES ('other','topic','note',0,'other',?)", (hs._vector_to_blob([1., 0.]),))
            con.commit()
            con.close()
            calls = []
            class Cur:
                def execute(self, sql, params=None):
                    calls.append((sql, params))
                    self.rows = []
                    if sql.startswith("SELECT slug FROM topics WHERE slug = ANY"):
                        self.rows = [(s,) for s in params[0]]
                    if "FROM topics t" in sql and "NOT EXISTS" not in sql and "JOIN episodes e" not in sql:
                        row = ("note", "Title", "A")
                        if "row_to_json(t)" in sql:
                            row += ({"slug": "note", "title": "Title", "body": "A", "status": "active", "frontmatter": {"project": "p"}},)
                        self.rows = [row]
                    if existing and "FROM episodes e" in sql and "NOT EXISTS" not in sql:
                        self.rows = [(7, "A", [], [], [], [])]
                    if sql.startswith("SELECT id FROM episodes") and "FOR SHARE" in sql:
                        self.rows = [(7,)] if existing and 7 in params[0] else []
                    if sql.startswith("SELECT 1 FROM topics") and "FOR SHARE" in sql:
                        self.rows = [(1,)]  # the topic is still live
                def fetchall(self): return self.rows
                def fetchone(self): return self.rows[0] if self.rows else None
                def __enter__(self): return self
                def __exit__(self, *_): return False
            def hashes(_cur, _profile, kind, refs):
                return {(kind, refs[0], i): embed._md5(t) for i, t in enumerate(["A", "B"])} if existing and refs else {}
            with mock.patch("khipu.db.connect", return_value=_Conn(Cur())), \
                 mock.patch("khipu.db.has_columns", return_value=False), \
                 mock.patch.object(embed, "_active_profile", return_value="prof"), \
                 mock.patch.object(embed, "_existing_hashes_for_refs", side_effect=hashes), \
                 mock.patch.object(embed, "_changed_topic_refs", return_value=["note"]), \
                 mock.patch.object(embed, "episode_text", return_value="" if empty else "A"), \
                 mock.patch.object(embed, "topic_text", return_value="" if empty else "A"), \
                 mock.patch.object(embed, "chunk_text", return_value=[] if empty else ["A"]), \
                 mock.patch.object(embed, "chunk_text_indexed", return_value=[] if empty else [(0, "A")]), \
                 mock.patch.object(embed, "embed_batch", side_effect=lambda texts, **kw: [[1., 0.] for t in texts]), \
                 mock.patch.object(embed, "_upsert_chunks"), \
                 mock.patch.object(hs, "snapshot_path", return_value=snap), \
                 mock.patch.object(hs, "meta_path", return_value=Path(tmp) / "meta.json"):
                embed.embed_recent_missing(limit=1)
                hs._MATRIX_CACHE.clear()
                found = hs.cosine_candidates_snapshot([1., 0.], "prof", limit=5)
            con = sqlite3.connect(snap)
            vectors = con.execute("SELECT profile,kind,ref,chunk_idx FROM memory_embeddings ORDER BY profile,kind,ref,chunk_idx").fetchall()
            con.close()
        return found, vectors, calls

    def test_rv3_new_topic_is_searchable_after_catchup(self):
        found, _, _ = self._catchup()
        self.assertEqual([r["id"] for r in found], ["note"])

    def test_rv3_removed_and_empty_chunks_are_pruned_in_both_stores(self):
        for empty in (False, True):
            with self.subTest(empty=empty):
                _, vectors, calls = self._catchup(existing=True, empty=empty)
                expected = [("other", "topic", "note", 0)]
                if not empty:
                    expected += [("prof", "episode", "7", 0), ("prof", "topic", "note", 0)]
                self.assertEqual(vectors, expected)
                deletes = [sql for sql, p in calls if sql.startswith("DELETE FROM memory_embeddings")]
                self.assertTrue(any("kind = 'episode'" in sql for sql in deletes))
                self.assertTrue(any("kind = 'topic'" in sql for sql in deletes))
