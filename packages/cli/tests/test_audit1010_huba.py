"""Regression coverage for the authoritative HUBA snapshot audit."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import hub_snapshot as hs
from khipu import profiles


class HubAuditRegressionTest(unittest.TestCase):
    def _snapshot(self, data: Path) -> Path:
        path = data / "hub_snapshot.sqlite"
        con = sqlite3.connect(path)
        hs._create_schema(con)
        con.commit()
        con.close()
        return path

    def test_episode_identity_filter_precedes_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap = self._snapshot(Path(tmp))
            con = sqlite3.connect(snap)
            con.executemany(
                "INSERT INTO episodes (id, ts, summary, project, session_id, harness) VALUES (?, ?, ?, ?, ?, ?)",
                [(1, "2026-10-10T10:00:00Z", "alpha", "wrong", "other:1", "other"),
                 (2, "2026-10-09T10:00:00Z", "alpha", "wanted", "claude:2", "claude")],
            )
            con.commit(); con.close()
            with mock.patch.object(hs, "snapshot_path", return_value=snap):
                out = hs.search_snapshot("alpha", 1, project="wanted", session_id="claude", harness="claude")
        self.assertEqual([r["id"] for r in out], ["2"])

    def test_incremental_upsert_cannot_resurrect_tombstone_or_erase_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp); snap = self._snapshot(data)
            con = sqlite3.connect(snap)
            con.execute("INSERT INTO episodes (id, summary, project, session_id, harness) VALUES (6, 'old live', 'p', 'claude:6', 'claude')")
            con.execute("INSERT INTO episodes (id, summary, project, session_id, harness, deleted_at) VALUES (7, 'old', 'p', 'claude:7', 'claude', '2026-10-01T00:00:00Z')")
            con.commit(); con.close()
            with (mock.patch.object(hs, "snapshot_path", return_value=snap), mock.patch.object(hs, "meta_path", return_value=data / "meta.json")):
                self.assertTrue(hs.upsert_episode({"id": 6, "summary": "new live", "project": None, "session_id": None, "harness": None}, [])["ok"])
                self.assertTrue(hs.upsert_episode({"id": 7, "summary": "new", "project": None, "session_id": None, "harness": None}, [{"profile": "p", "kind": "episode", "ref": "7", "chunk_idx": 0, "chunk_text": "new", "embedding": [1.0, 0.0]}])["ok"])
                hs._queue_erasure(8, snap)
                self.assertTrue(hs.upsert_episode({"id": 8, "summary": "late", "project": "p", "session_id": "claude:8"}, [{"profile": "p", "kind": "episode", "ref": "8", "chunk_idx": 0, "chunk_text": "late", "embedding": [1.0, 0.0]}])["ok"])
                hs.upsert_embeddings([{"profile": "p", "kind": "episode", "ref": "8", "chunk_idx": 1, "chunk_text": "later", "embedding": [1.0, 0.0]}])
                found = hs.search_snapshot("new live", 1, project="p", session_id="claude:6", harness="claude")
            con = sqlite3.connect(snap)
            row = con.execute("SELECT project, session_id, harness, deleted_at FROM episodes WHERE id = 7").fetchone()
            vectors = con.execute("SELECT COUNT(*) FROM memory_embeddings WHERE ref IN ('7', '8')").fetchone()[0]
            late_deleted = con.execute("SELECT deleted_at FROM episodes WHERE id = 8").fetchone()[0]
            con.close()
        self.assertEqual(row[:3], ("p", "claude:7", "claude")); self.assertIsNotNone(row[3])
        self.assertEqual([item["id"] for item in found], ["6"])
        self.assertIsNotNone(late_deleted); self.assertEqual(vectors, 0)

    def test_cached_generation_never_mixes_old_identity_with_new_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap = self._snapshot(Path(tmp))
            con = sqlite3.connect(snap)
            con.execute("INSERT INTO memory_embeddings VALUES ('p', 'topic', 'old', 0, 'old text', NULL, ?, NULL)", (hs._vector_to_blob([1.0, 0.0]),))
            con.commit(); con.close()
            hs._MATRIX_CACHE.clear()
            real_load = hs._matrix_rows

            def load_then_swap(path, profile, kind):
                generation = real_load(path, profile, kind)
                con = sqlite3.connect(snap)
                con.execute("DELETE FROM memory_embeddings")
                con.execute("INSERT INTO memory_embeddings VALUES ('p', 'topic', 'new', 0, 'new text', NULL, ?, NULL)", (hs._vector_to_blob([1.0, 0.0]),))
                con.commit(); con.close()
                return generation

            with mock.patch.object(hs, "snapshot_path", return_value=snap), \
                 mock.patch.object(hs, "_matrix_rows", side_effect=load_then_swap):
                out = hs.cosine_candidates_snapshot([1.0, 0.0], "p", limit=1)
        self.assertEqual((out[0]["id"], out[0]["rank_text"]), ("old", "old text"))

    def test_cosine_candidates_normalize_document_vectors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap = self._snapshot(Path(tmp))
            con = sqlite3.connect(snap)
            con.execute("INSERT INTO topics (slug, title, body) VALUES ('aligned', 'aligned', 'aligned')")
            con.execute("INSERT INTO topics (slug, title, body) VALUES ('scaled', 'scaled', 'scaled')")
            con.execute("INSERT INTO memory_embeddings VALUES ('p', 'topic', 'aligned', 0, 'aligned', NULL, ?, NULL)", (hs._vector_to_blob([1.0, 0.0]),))
            con.execute("INSERT INTO memory_embeddings VALUES ('p', 'topic', 'scaled', 0, 'scaled', NULL, ?, NULL)", (hs._vector_to_blob([2.0, 2.0]),))
            con.commit(); con.close()
            with mock.patch.object(hs, "snapshot_path", return_value=snap):
                hs._MATRIX_CACHE.clear()
                out = hs.cosine_candidates_snapshot([1.0, 0.0], "p", limit=2)
        self.assertEqual([row["id"] for row in out], ["aligned", "scaled"])
        self.assertAlmostEqual(out[0]["score"], 1.0, places=4)
        self.assertAlmostEqual(out[1]["score"], 2 ** -0.5, places=4)
        self.assertTrue(all(-1.0 <= row["score"] <= 1.0 for row in out))

    def test_outbox_merge_honors_episode_identity_and_query_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {"KHIPU_OUTBOX": tmp}):
            from khipu import outbox
            outbox.enqueue({"summary": "alpha wanted", "project": "wanted", "session_id": "claude:1", "harness": "claude"})
            outbox.enqueue({"summary": "alpha wrong", "project": "wrong", "session_id": "other:1", "harness": "other"})
            out = hs.merge_outbox_episodes([], query="alpha", project="wanted", session_id="claude", harness="claude")
        self.assertEqual([r["label"] for r in out], ["alpha wanted"])

    def test_snapshot_detail_never_exposes_raw_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap = self._snapshot(Path(tmp))
            con = sqlite3.connect(snap)
            con.execute("INSERT INTO episodes (id, summary, raw) VALUES (8, 'safe summary', '{\"secret\": \"SENTINEL\"}')")
            con.commit(); con.close()
            with mock.patch.object(hs, "snapshot_path", return_value=snap):
                out = hs.episode_detail_snapshot(8)
        self.assertIsNotNone(out)
        self.assertEqual(out["summary"], "safe summary")
        self.assertNotIn("raw", out)
        self.assertNotIn("SENTINEL", json.dumps(out))

    def test_pending_erasure_masks_locked_replica_readers_and_cached_cosine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp); snap = self._snapshot(data)
            con = sqlite3.connect(snap)
            con.execute("INSERT INTO episodes (id, summary, topics) VALUES (9, 'alpha', '[\"topic-a\"]')")
            con.execute("INSERT INTO topics (slug, title, body) VALUES ('topic-a', 'A', 'body')")
            con.execute("INSERT INTO memory_embeddings VALUES ('p', 'episode', '9', 0, 'alpha', NULL, ?, NULL)", (hs._vector_to_blob([1.0, 0.0]),))
            con.commit(); con.close()
            with (mock.patch.object(hs, "snapshot_path", return_value=snap), mock.patch.object(hs, "_acquire_refresh_lock", return_value=None)):
                hs.cosine_candidates_snapshot([1.0, 0.0], "p", limit=1)
                queued = hs.forget_episode_in_snapshot(9)
                self.assertTrue(queued["pending"])
                self.assertEqual(hs.search_snapshot("alpha", 10, kind="episode"), [])
                self.assertIsNone(hs.episode_detail_snapshot(9))
                self.assertTrue(hs.graph_neighbors_snapshot("9", 1, 5)["episode"]["missing"])
                self.assertEqual(hs.cosine_candidates_snapshot([1.0, 0.0], "p", limit=1), [])

    def test_internal_refresh_evidence_survives_missing_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp); snap = self._snapshot(data)
            con = sqlite3.connect(snap)
            con.execute("INSERT INTO snapshot_meta VALUES ('refreshed_at', '2026-10-10T00:00:00+00:00')")
            con.commit(); con.close()
            with (mock.patch.object(hs, "snapshot_path", return_value=snap), mock.patch.object(hs, "meta_path", return_value=data / "missing.json")):
                health = hs.snapshot_health()
        self.assertEqual(health["refreshed_at"], "2026-10-10T00:00:00+00:00")

    def test_active_snapshot_registers_complete_custom_profile_before_embedding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap = self._snapshot(Path(tmp))
            con = sqlite3.connect(snap)
            con.execute(
                "INSERT INTO embedding_profiles "
                "(id, provider, model, dim, normalize, is_active) "
                "VALUES ('local-model@2', 'openai-compatible', 'local-model', 2, 'l2', 1)"
            )
            con.execute(
                "INSERT INTO snapshot_meta VALUES (?, ?)",
                ("embedding_profile:local-model@2", '{"id":"local-model@2","provider":"openai-compatible","model":"local-model","dim":2,"normalize":"l2","endpoint":"http://127.0.0.1:11434"}'),
            )
            con.execute("INSERT INTO episodes (id, summary) VALUES (1, 'alpha')")
            con.execute(
                "INSERT INTO memory_embeddings "
                "(profile, kind, ref, chunk_idx, chunk_text, embedding) VALUES (?, 'episode', '1', 0, 'alpha', ?)",
                ("local-model@2", hs._vector_to_blob([1.0, 0.0])),
            )
            con.commit(); con.close()
            profiles.clear_learned()
            with mock.patch.object(hs, "snapshot_path", return_value=snap), \
                 mock.patch("khipu.db.connect", side_effect=AssertionError("hub used")), \
                 mock.patch("khipu.embed.embed_one", return_value=[1.0, 0.0]) as embed:
                from khipu import recall_prompt
                with mock.patch.object(recall_prompt, "_query_embed_cache_path", return_value=Path(tmp) / "query-cache.sqlite"):
                    profile_id = hs.active_snapshot_profile()
                    self.assertEqual(profile_id, "local-model@2")
                    self.assertEqual(recall_prompt._cached_query_embed("cold query", profile_id), [1.0, 0.0])
                    embed.assert_called_once_with(
                        "cold query", profile="local-model@2", retries=0,
                        timeout=recall_prompt.QUERY_EMBED_LOCAL_TIMEOUT_S,
                        delay=0, input_type="query",
                    )
                spec = profiles.resolve_spec("local-model@2")
                embed.reset_mock()
                out = hs.semantic_search_snapshot("alpha", limit=1)
        self.assertEqual(spec.endpoint, "http://127.0.0.1:11434")
        self.assertEqual(out[0]["id"], "1")
        embed.assert_called_once_with(
            "alpha", profile="local-model@2", retries=0, timeout=60.0,
            delay=0, input_type="query",
        )

    def test_snapshot_profile_width_mismatch_refuses_before_scoring(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap = self._snapshot(Path(tmp))
            con = sqlite3.connect(snap)
            con.execute(
                "INSERT INTO embedding_profiles "
                "(id, provider, model, dim, normalize, is_active) "
                "VALUES ('local-model@3', 'openai-compatible', 'local-model', 3, 'l2', 1)"
            )
            con.execute(
                "INSERT INTO snapshot_meta VALUES (?, ?)",
                ("embedding_profile:local-model@3", '{"id":"local-model@3","provider":"openai-compatible","model":"local-model","dim":3,"normalize":"l2","endpoint":"http://127.0.0.1:11434"}'),
            )
            con.execute(
                "INSERT INTO memory_embeddings "
                "(profile, kind, ref, chunk_idx, chunk_text, embedding) VALUES (?, 'episode', '1', 0, 'alpha', ?)",
                ("local-model@3", hs._vector_to_blob([1.0, 0.0])),
            )
            con.commit(); con.close()
            profiles.clear_learned()
            with mock.patch.object(hs, "snapshot_path", return_value=snap), \
                 mock.patch("khipu.embed.embed_one") as embed, \
                 mock.patch.object(hs, "_cosine") as cosine:
                with self.assertRaisesRegex(ValueError, "dimension"):
                    hs.semantic_search_snapshot("alpha", limit=1)
        embed.assert_not_called(); cosine.assert_not_called()

    def test_snapshot_profile_refuses_conflict_unsupported_and_missing_endpoint(self) -> None:
        cases = (
            ("gemini-embedding-001@768", "voyage", "gemini-embedding-001", 768, None, "seeded"),
            ("bad@2", "unknown", "bad", 2, None, "provider"),
            ("local@2", "openai-compatible", "local", 2, None, "endpoint"),
        )
        for profile_id, provider, model, dim, endpoint, message in cases:
            with self.subTest(profile_id=profile_id), tempfile.TemporaryDirectory() as tmp:
                snap = self._snapshot(Path(tmp)); con = sqlite3.connect(snap)
                con.execute(
                    "INSERT INTO embedding_profiles (id,provider,model,dim,normalize,is_active) VALUES (?,?,?,?, 'l2',1)",
                    (profile_id, provider, model, dim),
                )
                record = {"id": profile_id, "provider": provider, "model": model, "dim": dim, "normalize": "l2", "endpoint": endpoint}
                con.execute("INSERT INTO snapshot_meta VALUES (?, ?)", (f"embedding_profile:{profile_id}", __import__("json").dumps(record)))
                con.commit(); con.close()
                with mock.patch.object(hs, "snapshot_path", return_value=snap):
                    with self.assertRaisesRegex(ValueError, message):
                        hs.active_snapshot_profile_spec()

    def test_pending_erasure_masks_old_snapshot_without_deleted_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp); snap = data / "hub_snapshot.sqlite"
            con = sqlite3.connect(snap)
            con.execute("CREATE TABLE episodes (id INTEGER PRIMARY KEY, summary TEXT, raw TEXT, ingested_at TEXT, ts TEXT)")
            con.execute("INSERT INTO episodes VALUES (5, 'forgotten', '{\"project\":\"acme/khipu\"}', '2026-01-01', '2026-01-01')")
            con.commit(); con.close()
            with mock.patch.object(hs, "snapshot_path", return_value=snap):
                self.assertTrue(hs._queue_erasure(5, snap))
                opened = hs.open_snapshot()
                try:
                    self.assertEqual(opened.execute("SELECT id FROM episodes").fetchall(), [])
                finally:
                    opened.close()
                from khipu import recall_rule
                self.assertNotIn("forgotten", recall_rule._stale_project_slice("acme/khipu", None))

    def test_pending_erasure_read_error_does_not_fail_open(self) -> None:
        directory = mock.Mock()
        directory.iterdir.side_effect = PermissionError("denied")
        with mock.patch.object(hs, "_pending_erasure_dir", return_value=directory):
            with self.assertRaises(PermissionError):
                hs._pending_erasure_ids(Path("/tmp/snapshot"))

    def test_non_object_sidecar_is_reported_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp); snap = self._snapshot(data); sidecar = data / "meta.json"
            sidecar.write_text("[]", encoding="utf-8")
            with mock.patch.object(hs, "snapshot_path", return_value=snap), \
                 mock.patch.object(hs, "meta_path", return_value=sidecar):
                out = hs.meta()
        self.assertFalse(out["readable"])

    def test_outbox_query_uses_any_searchable_field_and_blank_does_not_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {"KHIPU_OUTBOX": tmp}):
            from khipu import outbox
            outbox.enqueue({"summary": "boring", "topics": ["needle"], "raw": {"external_id": "raw-42"}})
            by_topic = hs.merge_outbox_episodes([], query="needle zebra")
            by_raw = hs.merge_outbox_episodes([], query="raw-42")
            blank = hs.merge_outbox_episodes([], query="!!!")
        self.assertEqual(len(by_topic), 1)
        self.assertEqual(len(by_raw), 1)
        self.assertEqual(blank, [])

    def test_profile_export_keeps_endpoint_in_snapshot_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap = self._snapshot(Path(tmp)); con = sqlite3.connect(snap)
            row = ("local-model@2", "openai-compatible", "local-model", 2, "l2", True, "2026-01-01", None, "http://127.0.0.1:11434")
            cur = mock.Mock(); cur.fetchall.return_value = [row]
            with mock.patch.object(hs, "_pg_columns", return_value={*hs._PROFILE_COLS, "endpoint"}):
                self.assertEqual(hs._insert_profiles(cur, con), 1)
            saved = con.execute("SELECT value FROM snapshot_meta WHERE key = 'embedding_profile:local-model@2'").fetchone()[0]
            con.close()
        self.assertEqual(__import__("json").loads(saved)["endpoint"], "http://127.0.0.1:11434")

    def test_time_and_literal_identity_filters_precede_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap = self._snapshot(Path(tmp)); con = sqlite3.connect(snap)
            for i in range(1, 7):
                con.execute(
                    "INSERT INTO episodes (id, ts, summary, project, session_id, harness) VALUES (?, ?, 'alpha', ?, ?, 'claude')",
                    (i, f"2026-10-10T1{i}:00:00+00:00", "100XXproject", "Claude:wrong"),
                )
            con.execute(
                "INSERT INTO episodes (id, ts, summary, project, session_id, harness) VALUES (9, '2026-10-09T12:00:00+00:00', 'alpha', '100%_Project', 'claude:Right', 'claude')"
            )
            for i in range(1, 7):
                con.execute("INSERT INTO topics (slug,title,body,updated_at) VALUES (?,?, 'alpha',?)", (f"new-{i}", f"alpha {i}", f"2026-10-10T1{i}:00:00+00:00"))
                con.execute("INSERT INTO nodes (id,name,built_at) VALUES (?,?,?)", (f"new:{i}", f"alpha {i}", f"2026-10-10T1{i}:00:00+00:00"))
            con.execute("INSERT INTO topics (slug,title,body,updated_at) VALUES ('old-topic','alpha old','alpha','2026-10-09T12:00:00+00:00')")
            con.execute("INSERT INTO nodes (id,name,built_at) VALUES ('old:node','alpha old','2026-10-09T12:00:00+00:00')")
            con.commit(); con.close()
            with mock.patch.object(hs, "snapshot_path", return_value=snap):
                out = hs.search_snapshot(
                    "alpha", 1, kind="episode", project="100%_p",
                    session_id="claude:", harness="claude",
                    since="2026-10-09T00:00:00+00:00", until="2026-10-09T23:59:59+00:00",
                )
                topic_out = hs.search_snapshot("alpha", 1, kind="topic", since="2026-10-09T00:00:00+00:00", until="2026-10-09T23:59:59+00:00")
                node_out = hs.search_snapshot("alpha", 1, kind="node", since="2026-10-09T00:00:00+00:00", until="2026-10-09T23:59:59+00:00")
        self.assertEqual([row["id"] for row in out], ["9"])
        self.assertEqual([row["id"] for row in topic_out], ["old-topic"])
        self.assertEqual([row["id"] for row in node_out], ["old:node"])

    def test_invalid_snapshot_profile_degrades_recall_to_lexical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap = self._snapshot(Path(tmp)); con = sqlite3.connect(snap)
            con.execute("INSERT INTO embedding_profiles (id,provider,model,dim,normalize,is_active) VALUES ('local@2','openai-compatible','local',2,'l2',1)")
            con.execute("INSERT INTO episodes (id,summary) VALUES (1,'alpha lexical')")
            con.commit(); con.close()
            from khipu import recall_prompt

            def fused(_prompt, **kwargs):
                return {"hits": kwargs["lexical_rows"], "legs": kwargs["legs"], "degraded": kwargs["degraded"]}

            with mock.patch.object(hs, "snapshot_path", return_value=snap), \
                 mock.patch.object(hs, "snapshot_is_fresh", return_value=(True, {"exists": True, "age_seconds": 0})), \
                 mock.patch.object(recall_prompt, "_fuse_and_annotate", side_effect=fused):
                result = recall_prompt._snapshot_search_hits("alpha", project=None)
        self.assertEqual([row["id"] for row in result["hits"]], ["1"])
        self.assertEqual(result["legs"], ["lexical"])

    def test_weak_semantic_results_abstain_and_duplicate_chunks_collapse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap = self._snapshot(Path(tmp)); con = sqlite3.connect(snap)
            spec = {"id":"local-model@2","provider":"openai-compatible","model":"local-model","dim":2,"normalize":"l2","endpoint":"http://127.0.0.1:11434"}
            con.execute("INSERT INTO embedding_profiles (id,provider,model,dim,normalize,is_active) VALUES ('local-model@2','openai-compatible','local-model',2,'l2',1)")
            con.execute("INSERT INTO snapshot_meta VALUES (?, ?)", ("embedding_profile:local-model@2", __import__("json").dumps(spec)))
            con.execute("INSERT INTO episodes (id, summary) VALUES (1, 'unrelated memory')")
            con.execute("INSERT INTO episodes (id, summary) VALUES (2, 'second unrelated memory')")
            for idx in range(3):
                con.execute("INSERT INTO memory_embeddings (profile,kind,ref,chunk_idx,chunk_text,embedding) VALUES ('local-model@2','episode','1',?, 'unrelated memory', ?)", (idx, hs._vector_to_blob([0.0, 1.0])))
            con.execute("INSERT INTO memory_embeddings (profile,kind,ref,chunk_idx,chunk_text,embedding) VALUES ('local-model@2','episode','2',0, 'second unrelated memory', ?)", (hs._vector_to_blob([0.0, 1.0]),))
            con.commit(); con.close(); profiles.clear_learned()
            with mock.patch.object(hs, "snapshot_path", return_value=snap), \
                 mock.patch("khipu.embed.embed_one", return_value=[1.0, 0.0]), \
                 mock.patch("khipu.relevance.enabled", return_value=True):
                direct = hs.semantic_search_snapshot("never seen phrase", limit=2)
                payload = hs.search_stale_payload("never seen phrase", 2, semantic=True)
                hs._MATRIX_CACHE.clear()
                candidates = hs.cosine_candidates_snapshot([1.0, 0.0], "local-model@2", limit=2)
        self.assertEqual([row["id"] for row in direct], ["1", "2"])
        self.assertEqual([row["id"] for row in candidates], ["1", "2"])
        self.assertEqual(payload["results"], [])

    def test_low_cosine_exact_keyword_is_relevance_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap = self._snapshot(Path(tmp)); con = sqlite3.connect(snap)
            spec = {"id":"local-model@2","provider":"openai-compatible","model":"local-model","dim":2,"normalize":"l2","endpoint":"http://127.0.0.1:11434"}
            con.execute("INSERT INTO embedding_profiles (id,provider,model,dim,normalize,is_active) VALUES ('local-model@2','openai-compatible','local-model',2,'l2',1)")
            con.execute("INSERT INTO snapshot_meta VALUES (?, ?)", ("embedding_profile:local-model@2", json.dumps(spec)))
            con.execute("INSERT INTO episodes (id,summary) VALUES (1,'needle exact match')")
            con.execute("INSERT INTO memory_embeddings (profile,kind,ref,chunk_idx,chunk_text,embedding) VALUES ('local-model@2','episode','1',0,'needle exact match',?)", (hs._vector_to_blob([0.0, 1.0]),))
            con.commit(); con.close(); profiles.clear_learned()
            with mock.patch.object(hs, "snapshot_path", return_value=snap), \
                 mock.patch("khipu.embed.embed_one", return_value=[1.0, 0.0]), \
                 mock.patch("khipu.relevance.enabled", return_value=True):
                payload = hs.search_stale_payload("needle", 1, semantic=True)
        self.assertEqual([row["id"] for row in payload["results"]], ["1"])

    def test_missing_and_tombstoned_topic_vectors_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap = self._snapshot(Path(tmp)); con = sqlite3.connect(snap)
            spec = {"id":"local-model@2","provider":"openai-compatible","model":"local-model","dim":2,"normalize":"l2","endpoint":"http://127.0.0.1:11434"}
            con.execute("INSERT INTO embedding_profiles (id,provider,model,dim,normalize,is_active) VALUES ('local-model@2','openai-compatible','local-model',2,'l2',1)")
            con.execute("INSERT INTO snapshot_meta VALUES (?, ?)", ("embedding_profile:local-model@2", __import__("json").dumps(spec)))
            con.execute("INSERT INTO topics (slug,title,body,deleted_at) VALUES ('gone','gone','gone','2026-01-01')")
            for ref in ("gone", "missing"):
                con.execute("INSERT INTO memory_embeddings (profile,kind,ref,chunk_idx,chunk_text,embedding) VALUES ('local-model@2','topic',?,0,?,?)", (ref, ref, hs._vector_to_blob([1.0, 0.0])))
            con.commit(); con.close(); profiles.clear_learned(); hs._MATRIX_CACHE.clear()
            with mock.patch.object(hs, "snapshot_path", return_value=snap), mock.patch("khipu.embed.embed_one", return_value=[1.0, 0.0]):
                semantic = hs.semantic_search_snapshot("gone", limit=5)
                cosine = hs.cosine_candidates_snapshot([1.0, 0.0], "local-model@2", limit=5)
        self.assertEqual(semantic, []); self.assertEqual(cosine, [])

    def test_full_snapshot_tombstoned_episode_vectors_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp); snap = self._snapshot(data); con = sqlite3.connect(snap)
            spec = {"id":"local-model@2","provider":"openai-compatible","model":"local-model","dim":2,"normalize":"l2","endpoint":"http://127.0.0.1:11434"}
            con.execute("INSERT INTO embedding_profiles (id,provider,model,dim,normalize,is_active) VALUES ('local-model@2','openai-compatible','local-model',2,'l2',1)")
            con.execute("INSERT INTO snapshot_meta VALUES (?, ?)", ("embedding_profile:local-model@2", json.dumps(spec)))
            con.execute("INSERT INTO episodes (id,summary) VALUES (1,'public text')")
            con.execute("INSERT INTO episodes (id,summary,deleted_at) VALUES (2,'private text','2026-01-01T00:00:00+00:00')")
            con.execute("INSERT INTO memory_embeddings (profile,kind,ref,chunk_idx,chunk_text,embedding) VALUES ('local-model@2','episode','1',0,'public text',?)", (hs._vector_to_blob([1.0, 0.0]),))
            con.execute("INSERT INTO memory_embeddings (profile,kind,ref,chunk_idx,chunk_text,embedding) VALUES ('local-model@2','episode','2',0,'private text',?)", (hs._vector_to_blob([1.0, 0.0]),))
            con.commit(); con.close(); profiles.clear_learned(); hs._MATRIX_CACHE.clear()
            self.assertFalse((data / ".hub_snapshot.pending-erasure").exists())
            with mock.patch.object(hs, "snapshot_path", return_value=snap), mock.patch("khipu.embed.embed_one", return_value=[1.0, 0.0]):
                semantic = hs.semantic_search_snapshot("text", limit=5)
                cosine = hs.cosine_candidates_snapshot([1.0, 0.0], "local-model@2", limit=5)
        self.assertEqual([row["id"] for row in semantic], ["1"])
        self.assertEqual([row["id"] for row in cosine], ["1"])
        self.assertNotIn("private text", json.dumps([semantic, cosine]))

    def test_decision_sync_pages_through_equal_timestamps_with_tuple_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp); snap = self._snapshot(data); sidecar = data / "meta.json"
            columns = list(hs._DECISION_COLS)
            stamp = "2026-10-10T12:00:00+00:00"
            executed: list[tuple[str, tuple]] = []

            class Cursor:
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def execute(self, sql, params): executed.append((sql, tuple(params)))
                def fetchall(self):
                    since = executed[-1][1]
                    ids = (1, 2) if len(since) == 1 else (3,)
                    rows = []
                    for rid in ids:
                        record = {c: None for c in columns}
                        record.update({"id": rid, "text": f"d{rid}", "created_at": stamp})
                        rows.append(tuple(record[c] for c in columns) + (stamp,))
                    return rows

            class Pg:
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def cursor(self): return Cursor()

            with mock.patch.object(hs, "snapshot_path", return_value=snap), \
                 mock.patch.object(hs, "meta_path", return_value=sidecar), \
                 mock.patch.object(hs, "try_hub_connect", return_value=Pg()), \
                 mock.patch.object(hs, "_pg_columns", return_value=set(columns)):
                first = hs.sync_decision_changes(limit=2)
                second = hs.sync_decision_changes(limit=2)
                meta_data = json.loads(sidecar.read_text(encoding="utf-8"))
                meta_data["last_decision_sync_id"] = "malformed"
                sidecar.write_text(json.dumps(meta_data), encoding="utf-8")
                replay = hs.sync_decision_changes(limit=2)
            con = sqlite3.connect(snap)
            ids = [r[0] for r in con.execute("SELECT id FROM decisions ORDER BY id")]
            con.close()
        self.assertEqual((first["decisions"], second["decisions"]), (2, 1))
        self.assertTrue(replay["ok"])
        self.assertEqual(ids, [1, 2, 3])
        self.assertIn(", id) > (%s, %s)", executed[1][0])
        self.assertEqual(executed[1][1][:2], (stamp, 2))

    def test_incremental_write_cannot_create_refresh_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp); snap = self._snapshot(data); sidecar = data / "meta.json"
            old = "2020-01-01T00:00:00+00:00"
            con = sqlite3.connect(snap)
            con.execute("INSERT INTO snapshot_meta VALUES ('refreshed_at', ?)", (old,))
            con.commit(); con.close(); sidecar.write_text("{bad", encoding="utf-8")
            with mock.patch.object(hs, "snapshot_path", return_value=snap), \
                 mock.patch.object(hs, "meta_path", return_value=sidecar), \
                 mock.patch.object(hs, "_acquire_refresh_lock", return_value=object()), \
                 mock.patch.object(hs, "_release_refresh_lock"):
                hs.upsert_episode({"id": 1, "summary": "incremental"}, [])
                health = hs.snapshot_health()
            saved = __import__("json").loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(saved["refreshed_at"], old)
        self.assertEqual(health["refreshed_at"], old)
        self.assertGreater(health["age_seconds"], hs.SNAPSHOT_MAX_AGE_S)
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp); snap = data / "hub_snapshot.sqlite"; sidecar = data / "meta.json"
            con = sqlite3.connect(snap); hs._create_schema(con, version=1); con.commit(); con.close()
            sidecar.write_text("{bad", encoding="utf-8")
            with mock.patch.object(hs, "snapshot_path", return_value=snap), \
                 mock.patch.object(hs, "meta_path", return_value=sidecar), \
                 mock.patch.object(hs, "_acquire_refresh_lock", return_value=object()), \
                 mock.patch.object(hs, "_release_refresh_lock"):
                hs.upsert_episode({"id": 2, "summary": "incremental only"}, [])
                unknown = hs.snapshot_health()
        self.assertFalse(unknown["ok"]); self.assertIsNone(unknown["refreshed_at"])

    def test_refresh_forget_race_publishes_tombstone_and_masks_every_reader(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp); snap = self._snapshot(data); sidecar = data / "meta.json"

            class Pg:
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def cursor(self): return self
                def execute(self, *args, **kwargs): pass
                def fetchall(self): return []

            def episodes(_cur, con):
                con.execute("INSERT INTO episodes (id, summary, raw) VALUES (41, 'race secret', '{}')")
                queued = hs.forget_episode_in_snapshot(41)
                self.assertTrue(queued["pending"])
                return 1

            def vectors(_cur, con):
                con.execute("INSERT INTO memory_embeddings (profile,kind,ref,chunk_idx,chunk_text,embedding) VALUES ('p','episode','41',0,'race secret',?)", (hs._vector_to_blob([1.0, 0.0]),))
                return 1

            patches = [
                mock.patch.object(hs, "snapshot_path", return_value=snap),
                mock.patch.object(hs, "meta_path", return_value=sidecar),
                mock.patch("khipu.paths.ensure_data_dir", return_value=data),
                mock.patch.object(hs, "_acquire_refresh_lock", side_effect=[object(), None]),
                mock.patch.object(hs, "_release_refresh_lock"),
                mock.patch.object(hs, "try_hub_connect", return_value=Pg()),
                mock.patch.object(hs, "_insert_episodes", side_effect=episodes),
                mock.patch.object(hs, "_insert_memory_embeddings", side_effect=vectors),
            ]
            zeroes = ["_insert_topics", "_insert_topic_revisions", "_insert_nodes", "_insert_edges", "_insert_profiles", "_insert_decisions", "_insert_deliverables"]
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], \
                 mock.patch.multiple(hs, **{name: mock.Mock(return_value=0) for name in zeroes}):
                self.assertTrue(hs.refresh()["ok"])
                self.assertEqual(hs.search_snapshot("race secret", 5, kind="episode"), [])
                self.assertIsNone(hs.episode_detail_snapshot(41))
                self.assertTrue(hs.graph_neighbors_snapshot("41", 1, 5)["episode"]["missing"])
                hs._MATRIX_CACHE.clear()
                self.assertEqual(hs.cosine_candidates_snapshot([1.0, 0.0], "p", limit=5), [])
            con = sqlite3.connect(snap)
            deleted, vectors_left = con.execute("SELECT deleted_at, (SELECT COUNT(*) FROM memory_embeddings WHERE ref='41') FROM episodes WHERE id=41").fetchone()
            con.close()
        self.assertIsNotNone(deleted); self.assertEqual(vectors_left, 0)

    def test_stale_project_slice_uses_pending_filtered_snapshot_view(self) -> None:
        from khipu import recall_rule
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp); snap = self._snapshot(data)
            con = sqlite3.connect(snap)
            raw = '{"project":"acme/khipu"}'
            con.execute("INSERT INTO episodes (id,summary,raw,ingested_at) VALUES (1,'live',?,'2026-01-01')", (raw,))
            con.execute("INSERT INTO episodes (id,summary,raw,ingested_at) VALUES (2,'forgotten',?,'2026-01-02')", (raw,))
            con.commit(); con.close()
            with mock.patch.object(hs, "snapshot_path", return_value=snap):
                hs._queue_erasure(2, snap)
                out = recall_rule._stale_project_slice("acme/khipu", None)
        self.assertIn("live", out); self.assertNotIn("forgotten", out)
