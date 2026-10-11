"""Hermetic regression coverage for the audit1010 hub-health repairs."""
from __future__ import annotations

import os
import json
import sqlite3
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from khipu import graph_backup, git_sync_health


class GraphSnapshotHealthTest(unittest.TestCase):
    def test_unrelated_healthy_sqlite_is_not_a_graph_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snap = root / "graph-20261010T000000Z.sqlite"
            con = sqlite3.connect(snap)
            try:
                con.execute("CREATE TABLE unrelated (id INTEGER)")
                con.commit()
            finally:
                con.close()
            out = graph_backup.local_health(
                now=datetime.now(timezone.utc), snapshot_dir=root, producer=True
            )
        self.assertFalse(out["ok"], out)
        self.assertIn("nodes", out["reason"])

    def test_invalid_fresh_snapshot_is_red_and_never_uploaded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snap = root / "graph-20261010T000000Z.sqlite"
            snap.write_bytes(b"this is not sqlite")
            live = root / "graph.sqlite"
            live.write_bytes(b"live")
            now = datetime.now(timezone.utc)
            health = graph_backup.local_health(now=now, snapshot_dir=root, producer=True)
            self.assertFalse(health["ok"], health)
            self.assertIn("integrity", health["reason"])
            with (
                mock.patch.object(graph_backup, "latest_snapshot", return_value=snap),
                mock.patch.object(graph_backup, "_live_graph", return_value=live),
                mock.patch.object(graph_backup, "resolve_rclone", return_value="rclone"),
                mock.patch.object(graph_backup, "has_r2_remote", return_value=True),
                mock.patch.object(graph_backup, "_integrity_check", return_value="error: malformed database"),
                mock.patch("khipu.graph_backup.subprocess.run", side_effect=AssertionError("must not upload")),
            ):
                out = graph_backup.run_offsite()
            self.assertFalse(out["ok"], out)
            self.assertIn("integrity", out["reason"])


class GitSyncHealthEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.hb = self.root / "git-sync.json"
        self.log = self.root / "nightly.out.log"
        self.log.write_text("nightly ran\n", encoding="utf-8")
        self.env = mock.patch.dict(
            os.environ,
            {"KHIPU_GIT_SYNC_HOST": "1", "KHIPU_GIT_SYNC_HEARTBEAT": str(self.hb)},
            clear=False,
        )
        self.env.start()
        self.log_patch = mock.patch.object(git_sync_health, "nightly_log_path", return_value=self.log)
        self.log_patch.start()

    def tearDown(self):
        self.log_patch.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_nightly_without_heartbeat_is_red_after_installation_boundary(self):
        plist = self.root / "nightly.plist"
        plist.write_text("installed", encoding="utf-8")
        now = time.time()
        os.utime(plist, (now - 60, now - 60))
        os.utime(self.log, (now, now))
        with mock.patch.object(git_sync_health, "nightly_plist_path", return_value=plist):
            out = git_sync_health.status()
        self.assertFalse(out["ok"], out)
        self.assertTrue(any("did not record a heartbeat" in r for r in out["reasons"]), out)

    def test_preinstall_log_does_not_fail_first_run(self):
        plist = self.root / "nightly.plist"
        plist.write_text("installed", encoding="utf-8")
        now = time.time()
        os.utime(self.log, (now - 60, now - 60))
        os.utime(plist, (now, now))
        with mock.patch.object(git_sync_health, "nightly_plist_path", return_value=plist):
            out = git_sync_health.status()
        self.assertTrue(out["ok"], out)

    def test_nightly_without_heartbeat_and_without_install_boundary_is_unchecked(self):
        missing_plist = self.root / "missing-nightly.plist"
        with mock.patch.object(git_sync_health, "nightly_plist_path", return_value=missing_plist):
            out = git_sync_health.status()
        self.assertFalse(out["ok"], out)
        self.assertTrue(any("installation boundary" in r for r in out["reasons"]), out)

    def test_corrupt_heartbeat_is_red(self):
        self.hb.write_text("{truncated", encoding="utf-8")
        out = git_sync_health.status()
        self.assertFalse(out["ok"], out)
        self.assertTrue(any("heartbeat unreadable" in r for r in out["reasons"]), out)

    def test_heartbeat_object_without_required_fields_is_red(self):
        self.hb.write_text('{"outcome":"synced"}', encoding="utf-8")
        out = git_sync_health.status()
        self.assertFalse(out["ok"], out)
        self.assertTrue(any("expected ts and exit" in r for r in out["reasons"]), out)

    def test_heartbeat_with_null_exit_is_unchecked(self):
        self.hb.write_text('{"ts":"2026-10-10T00:00:00Z","exit":null}', encoding="utf-8")
        with mock.patch.object(git_sync_health, "nightly_log_path", return_value=self.root / "absent.log"):
            out = git_sync_health.status()
        self.assertFalse(out["ok"], out)
        self.assertTrue(any("heartbeat unreadable" in r for r in out["reasons"]), out)

    def test_git_probe_failure_is_reported_and_red(self):
        repo = self.root / "Memory"
        repo.mkdir()
        (repo / ".git").mkdir()
        with mock.patch("khipu.git_sync_health.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 3)):
            state = git_sync_health.repo_state(repo)
        self.assertTrue(state["errors"])
        with mock.patch("khipu.git_sync_health.repo_state", return_value=state):
            out = git_sync_health.status()
        self.assertFalse(out["ok"], out)
        self.assertTrue(any("git probe failed" in r for r in out["reasons"]), out)

    def test_git_nonzero_exit_is_reported_and_red(self):
        repo = self.root / "Memory"
        repo.mkdir()
        (repo / ".git").mkdir()
        completed = mock.Mock(returncode=128, stdout="", stderr="fatal: broken repo")
        with mock.patch("khipu.git_sync_health.subprocess.run", return_value=completed):
            state = git_sync_health.repo_state(repo)
        self.assertTrue(any("exited 128" in e for e in state["errors"]), state)


class ConnectBoundsTest(unittest.TestCase):
    def test_rv4_05_migration_does_not_inherit_interactive_statement_timeout(self):
        from khipu import db, migrate

        def connection(*args, **kwargs):
            options = kwargs.get("options", "")
            conn = mock.MagicMock()
            cur = conn.cursor.return_value.__enter__.return_value
            def execute(sql, *unused):
                if "CREATE INDEX" in sql and "statement_timeout=15000" in options:
                    raise RuntimeError("canceling statement due to statement timeout")
            cur.execute.side_effect = execute
            conn.__enter__.return_value = conn
            return conn

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "0026_library.sql"
            path.write_text("CREATE INDEX library_hnsw ON library_embeddings USING hnsw (embedding vector_cosine_ops)")
            with mock.patch.object(db, "resolve_dsn", return_value="postgres://x"), \
                    mock.patch.object(db, "conninfo_with_local_root_cert", side_effect=lambda d: d), \
                    mock.patch("psycopg.connect", side_effect=connection), \
                    mock.patch.object(migrate, "plan", return_value={"_pending_paths": [("0026_library", path)]}):
                self.assertEqual(migrate.run()["ran"], ["0026_library"])

    def test_connect_sets_finite_connect_timeout_without_sql_policy(self):
        from khipu import db

        with (
            mock.patch.object(db, "resolve_dsn", return_value="postgres://x"),
            mock.patch.object(db, "conninfo_with_local_root_cert", side_effect=lambda d: d),
            mock.patch("psycopg.connect") as pg,
        ):
            db.connect()
        self.assertEqual(pg.call_args.kwargs["connect_timeout"], db.CONNECT_TIMEOUT_SECONDS)
        self.assertNotIn("options", pg.call_args.kwargs)

    def test_connect_preserves_existing_dsn_options(self):
        from khipu import db

        conninfo = db.make_conninfo(host="hub", options="-c search_path=private")
        with (
            mock.patch.object(db, "resolve_dsn", return_value="postgres://x"),
            mock.patch.object(db, "conninfo_with_local_root_cert", return_value=conninfo),
            mock.patch("psycopg.connect") as pg,
        ):
            db.connect()
        options = db.conninfo_to_dict(pg.call_args.args[0])["options"]
        self.assertIn("search_path=private", options)
        self.assertNotIn("statement_timeout", options)


class LegacyDriftEvidenceTest(unittest.TestCase):
    def test_missing_or_rejected_legacy_inputs_are_errors_not_clean_drift(self):
        from khipu.drift import sample_drift

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertIn("memory root missing", sample_drift(root / "absent")["error"])
            root.mkdir(exist_ok=True)
            (root / "topics").mkdir()
            (root / "episodes.jsonl").write_text("{broken\n", encoding="utf-8")
            self.assertIn("rejected", sample_drift(root)["error"])

    def test_missing_topics_and_mixed_valid_invalid_logs_are_errors(self):
        from khipu.drift import sample_drift

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "episodes.jsonl").write_text("", encoding="utf-8")
            self.assertIn("topics directory missing", sample_drift(root)["error"])
            (root / "topics").mkdir()
            (root / "episodes.jsonl").write_text(
                '{"ts":"2026-10-10T00:00:00Z","summary":"ok"}\n{broken\n',
                encoding="utf-8",
            )
            self.assertIn("rejected", sample_drift(root)["error"])

    def test_valid_empty_sources_reach_pg_and_are_not_evidence_errors(self):
        from khipu import drift

        class Cur:
            def __init__(self): self.rows = []
            def execute(self, sql, params=None):
                flat = " ".join(sql.split())
                if "FILTER" in flat: self.rows = [(0, 0)]
                elif "MAX(ts)" in flat: self.rows = [(None, None)]
                else: self.rows = [(0,)]
            def fetchone(self): return self.rows[0]
            def fetchall(self): return list(self.rows)
            def __enter__(self): return self
            def __exit__(self, *args): return False
        class Conn:
            def cursor(self): return Cur()
            def __enter__(self): return self
            def __exit__(self, *args): return False

        with tempfile.TemporaryDirectory() as td:
            root = Path(td); (root / "topics").mkdir()
            (root / "episodes.jsonl").write_text("\n", encoding="utf-8")
            with mock.patch.object(drift, "connect", return_value=Conn()):
                out = drift.sample_drift(root)
        self.assertNotIn("error", out)
        self.assertEqual(out["episodes_file"], 0)


class _RecordingCursor:
    def __init__(self):
        self.calls = []
        self.rowcount = 1
        self._rows = []

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.calls.append((flat, params))
        if flat.startswith("SELECT content_hash FROM topics"):
            self._rows = []
        elif "information_schema.columns" in flat:
            self._rows = []
        else:
            self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class MirrorBoundaryRedactionTest(unittest.TestCase):
    SECRET = "sk-" + "a" * 32

    def test_episode_sql_params_redact_nested_free_text_without_mutating_input(self):
        from khipu import mirror

        payload = {
            "ts": "2026-10-10T00:00:00Z",
            "session_id": "identity-sk-" + "b" * 32,
            "summary": f"token {self.SECRET}",
            "topics": [f"Topic {self.SECRET}"],
            "decisions": [{"decision_details": {"why": self.SECRET}}],
            "raw": {"nested": [f"raw {self.SECRET}"]},
        }
        original = json.loads(json.dumps(payload))
        cur = _RecordingCursor()
        mirror._upsert_episode(cur, payload)
        insert = next(params for sql, params in cur.calls if sql.startswith("INSERT INTO episodes"))
        rendered = json.dumps(insert)
        self.assertNotIn(self.SECRET, rendered)
        self.assertIn(payload["session_id"], rendered)
        self.assertEqual(payload, original)

    def test_topic_boundary_redacts_text_and_preserves_canonical_full_digest(self):
        from khipu import mirror

        parsed = {
            "slug": "safe-slug", "title": f"Title {self.SECRET}",
            "status": "active", "body": f"Body {self.SECRET}",
            "digest": mirror.topic_content_hash(f"full source Body {self.SECRET}"), "links": [],
            "frontmatter": {"description": f"Desc {self.SECRET}"},
        }
        original = json.loads(json.dumps(parsed))
        cur = _RecordingCursor()
        mirror._upsert_topic(cur, parsed, "/tmp/safe-slug.md", source="test", note=f"note {self.SECRET}", sync_graph=False)
        rendered = json.dumps(cur.calls)
        self.assertNotIn(self.SECRET, rendered)
        self.assertEqual(parsed, original)
        topic_params = next(params for sql, params in cur.calls if sql.startswith("INSERT INTO topics"))
        revision_params = next(params for sql, params in cur.calls if sql.startswith("INSERT INTO topic_revisions"))
        self.assertEqual(topic_params[8], revision_params[4])

    def test_note_dictionary_keeps_full_source_canonical_digest_at_upsert(self):
        from khipu import mirror, notes

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "note.md"
            path.write_text(f"---\nname: Useful note\n---\nBody {self.SECRET}\n", encoding="utf-8")
            parsed = notes._note_topic_dict(path, project="khipu")
            cur = _RecordingCursor()
            mirror._upsert_topic(cur, parsed, str(path), source="notes", note="sync", sync_graph=False)
        topic_params = next(params for sql, params in cur.calls if sql.startswith("INSERT INTO topics"))
        self.assertNotIn(self.SECRET, json.dumps(topic_params))
        self.assertEqual(topic_params[8], parsed["digest"])

    def test_parse_hash_and_drift_hash_share_redacted_canonical_text(self):
        from khipu import drift, mirror

        with tempfile.TemporaryDirectory() as td, mock.patch("khipu.config.path_setting", return_value=None):
            root = Path(td)
            topics = root / "topics"
            topics.mkdir()
            path = topics / "secret.md"
            path.write_text(f"---\ntitle: Safe\n---\n{self.SECRET}\n", encoding="utf-8")
            parsed = mirror.parse_topic_file(path)
            hashes, unreadable = drift.file_topic_hashes(root)
        self.assertEqual(unreadable, [])
        self.assertNotIn(self.SECRET, parsed["body"])
        self.assertEqual(hashes["secret"], parsed["digest"])


class TopicPathContainmentTest(unittest.TestCase):
    def test_traversal_and_escaping_symlink_are_rejected_before_read(self):
        from khipu import mirror

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "memory"
            topics = root / "topics"
            topics.mkdir(parents=True)
            outside = Path(td) / "outside.md"
            outside.write_text("outside", encoding="utf-8")
            link = topics / "escape.md"
            link.symlink_to(outside)
            with mock.patch("khipu.config.path_setting", return_value=root), mock.patch.object(Path, "read_text", side_effect=AssertionError("outside read")):
                self.assertIsNone(mirror.parse_topic_file(topics / ".." / "outside.md"))
                self.assertIsNone(mirror.parse_topic_file(link))


class GraphDriftContentTest(unittest.TestCase):
    class _Cur:
        def __init__(self, *, payload='{"a":2}', weight=2.0, source_id="code:test"):
            self.payload, self.weight, self.source_id, self.rows = payload, weight, source_id, []
        def execute(self, sql, params=None):
            flat = " ".join(sql.split())
            if "SELECT n.id, n.type" in flat:
                self.rows = [("n1", "concept", "code", "Name", self.payload, "x.py", "2026-01-01T00:00:00Z", False, self.source_id)]
            elif "SELECT COUNT(*)" in flat:
                self.rows = [(0,)]
            elif "SELECT e.src" in flat:
                self.rows = [("n1", "n1", "uses", self.weight, '{"z":1}', "2026-01-01T00:00:00+00:00")]
        def fetchall(self): return list(self.rows)
        def fetchone(self): return self.rows[0]
        def __enter__(self): return self
        def __exit__(self, *args): return False
    class _Conn:
        def __init__(self, cur): self.cur = cur
        def cursor(self): return self.cur
        def __enter__(self): return self
        def __exit__(self, *args): return False

    def _sqlite(self, root):
        p = root / "graph.sqlite"
        con = sqlite3.connect(p)
        con.executescript("""
        CREATE TABLE nodes(id TEXT,type TEXT,bucket TEXT,name TEXT,payload TEXT,source_path TEXT,built_at TEXT,frozen INTEGER);
        CREATE TABLE edges(src TEXT,dst TEXT,type TEXT,weight REAL,payload TEXT,built_at TEXT);
        INSERT INTO nodes VALUES('n1','concept','code','Name','{"a":1}','x.py','2026-01-01T00:00:00+00:00',0);
        INSERT INTO edges VALUES('n1','n1','uses',1.0,'{"z":1}','2026-01-01T00:00:00Z');
        """)
        con.commit(); con.close(); return p

    def test_equal_ids_with_changed_content_are_red_and_normalized_values_are_clean(self):
        from khipu import graph_sync
        with tempfile.TemporaryDirectory() as td:
            p = self._sqlite(Path(td))
            common = [mock.patch.object(graph_sync, "is_graph_producer", return_value=True), mock.patch.object(graph_sync, "disabled_or_unreachable_ids", return_value=set()), mock.patch.object(graph_sync, "load_sources", return_value={"sources": [{"id":"code:test", "enabled":True}]}), mock.patch.object(graph_sync, "owned_source_ids", return_value={"code:test"}), mock.patch.object(graph_sync, "_changed_since_last_mirror", return_value=False)]
            with common[0], common[1], common[2], common[3], common[4], mock.patch("khipu.db.connect", return_value=self._Conn(self._Cur())):
                drifted = graph_sync.graph_drift(p)
            self.assertFalse(drifted["ok"], drifted)
            self.assertEqual(drifted["nodes_content_drift"], 1)
            self.assertEqual(drifted["edges_content_drift"], 1)
            with mock.patch.object(graph_sync, "is_graph_producer", return_value=True), mock.patch.object(graph_sync, "disabled_or_unreachable_ids", return_value=set()), mock.patch.object(graph_sync, "load_sources", return_value={"sources": []}), mock.patch.object(graph_sync, "owned_source_ids", return_value=set()), mock.patch.object(graph_sync, "_changed_since_last_mirror", return_value=False), mock.patch("khipu.db.connect", return_value=self._Conn(self._Cur(payload='{"a": 1}', weight=1.0, source_id=None))):
                clean = graph_sync.graph_drift(p)
            self.assertTrue(clean["ok"], clean)

    def test_corrupt_source_registry_is_explicitly_red(self):
        from khipu import graph_sync, sources
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); p = self._sqlite(root); registry = root / "sources.json"
            registry.write_text("{broken", encoding="utf-8")
            with mock.patch.object(graph_sync, "is_graph_producer", return_value=True), mock.patch.object(sources, "sources_file", return_value=registry):
                out = graph_sync.graph_drift(p)
        self.assertFalse(out["ok"]); self.assertIn("source registry", out["error"])


class CandidateDeadlineTest(unittest.TestCase):
    def test_sqlite_query_is_interrupted_and_connection_recovers(self):
        from khipu import graph_candidates as gc
        con = sqlite3.connect(":memory:")
        con.executescript("CREATE TABLE edges(src,dst,type); CREATE TABLE topics(slug,title,body,updated_at,created_at,deleted_at); CREATE TABLE episodes(id,ts,summary,topics,deleted_at);")
        seed = [{"kind": "topic", "id": "x"}]
        with mock.patch.object(gc, "_replica_topic_wiki", side_effect=lambda c,*a,**k: c.execute("WITH RECURSIVE x(v) AS (VALUES(0) UNION ALL SELECT v+1 FROM x WHERE v<100000000) SELECT max(v) FROM x").fetchall()):
            rows, missed = gc.replica_candidates(con, seed, deadline=time.monotonic() + 0.002)
        self.assertTrue(missed); self.assertEqual(rows, [])
        self.assertEqual(con.execute("SELECT 1").fetchone()[0], 1)

    def test_hub_applies_remaining_statement_timeout_and_restores_it(self):
        from khipu import graph_candidates as gc
        cur = mock.Mock()
        cur.fetchone.side_effect = [("9s",), ("9s",)]
        cur.fetchall.return_value = []
        rows, missed = gc.hub_candidates(cur, [{"kind":"topic", "id":"x"}], deadline=time.monotonic() + 1)
        self.assertFalse(missed); self.assertEqual(rows, [])
        calls = [(" ".join(c.args[0].split()), c.args[1] if len(c.args)>1 else None) for c in cur.execute.call_args_list]
        settings = [p for sql,p in calls if "set_config('statement_timeout'" in sql]
        self.assertTrue(settings)
        self.assertEqual(settings[-1], ("9s",))

    def test_hub_query_timeout_rolls_back_savepoint_and_restores_policy(self):
        from khipu import graph_candidates as gc

        class QueryCanceled(Exception): pass
        class Cur:
            def __init__(self): self.calls = []; self.rows = []
            def execute(self, sql, params=None):
                flat = " ".join(sql.split()); self.calls.append((flat, params))
                if "current_setting" in flat: self.rows = [("9s",)]
                elif flat.startswith("SELECT dst FROM edges"): raise QueryCanceled("statement timeout")
                else: self.rows = []
            def fetchone(self): return self.rows[0] if self.rows else None
            def fetchall(self): return list(self.rows)
        cur = Cur()
        rows, missed = gc.hub_candidates(
            cur, [{"kind":"topic", "id":"x"}], deadline=time.monotonic() + 1
        )
        self.assertTrue(missed); self.assertEqual(rows, [])
        sql = [c[0] for c in cur.calls]
        self.assertIn("ROLLBACK TO SAVEPOINT khipu_graph_candidates_deadline", sql)
        self.assertIn("RELEASE SAVEPOINT khipu_graph_candidates_deadline", sql)
        self.assertEqual(cur.calls[-2][1], ("9s",))


class EpisodeCollapseTest(unittest.TestCase):
    def test_duplicate_episode_chunks_keep_first_ranked_hit(self):
        from khipu.topic_graph import collapse_semantic_topic_hits

        first = {"kind": "episode", "id": "7", "score": 0.9, "chunk_idx": 2}
        duplicate = {"kind": "episode", "id": "7", "score": 0.8, "chunk_idx": 0}
        other = {"kind": "episode", "id": "8", "score": 0.7, "chunk_idx": 0}
        self.assertEqual(collapse_semantic_topic_hits([first, duplicate, other]), [first, other])

    def test_episode_rows_without_identity_are_preserved(self):
        from khipu.topic_graph import collapse_semantic_topic_hits

        rows = [{"kind": "episode", "snippet": "one"}, {"kind": "episode", "snippet": "two"}]
        self.assertEqual(collapse_semantic_topic_hits(rows), rows)

