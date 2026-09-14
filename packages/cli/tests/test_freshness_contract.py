"""Contract tests for Phase 4 — freshness within a minute (F1-F6, R7).

Each section targets one gap the phase brief named. No live database or
filesystem outside a temp dir: notes/state-file sections reuse a fake
Postgres cursor (same posture as test_notes.py); embed sections reuse the
fake cursors from test_embed.py.
"""
from __future__ import annotations

import contextlib
import json
import os
import plistlib
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import unittest

from khipu import embed as em
from khipu import jobs
from khipu import launchd_gen
from khipu import notes
from khipu import recency
from tests.test_notes import _FakeConn, _FakeTopicsCursor, _sample_note_text, _write


# ---- F1: mtime-gated changed-only reconcile ---------------------------------

class ChangedOnlyReconcileContractTest(unittest.TestCase):
    """A fixture dir with three notes; editing one reconciles only that one."""

    def test_editing_one_of_three_notes_reconciles_only_that_one(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as data_td:
            claude_root = Path(td) / "claude_projects"
            for name in ("one", "two", "three"):
                _write(
                    claude_root / "-repo-a" / "memory" / f"{name}.md",
                    _sample_note_text().replace("khipu-state-of-play", name),
                )
            cur = _FakeTopicsCursor()
            patches = (
                mock.patch.object(notes, "claude_projects_root", return_value=claude_root),
                mock.patch.object(notes, "codex_memories_root", return_value=Path(td) / "no-codex"),
                mock.patch.object(notes, "_project_for_slug", return_value="acme/repo-a"),
                mock.patch("khipu.paths.ensure_data_dir", return_value=Path(data_td)),
                mock.patch("khipu.db.connect", return_value=_FakeConn(cur)),
                mock.patch("khipu.mirror.persist_topic_graph",
                            return_value={"nodes_minted": 0, "edges_minted": 0}),
            )
            with contextlib.ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                first = notes.reconcile(dry_run=False, changed_only=True)
                self.assertEqual(first["written"], 3)
                two = claude_root / "-repo-a" / "memory" / "two.md"
                two.write_text(two.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")
                mtime = two.stat().st_mtime + 5
                os.utime(two, (mtime, mtime))
                second = notes.reconcile(dry_run=False, changed_only=True)
        self.assertEqual(second["candidates"], 1)
        self.assertEqual(second["written"], 1)
        self.assertTrue(second["slugs"][0].startswith("note:two"))


# ---- F1: state file round-trip -----------------------------------------------

class StateFileContractTest(unittest.TestCase):
    def test_state_round_trips_through_disk(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("khipu.paths.ensure_data_dir", return_value=Path(td)):
                notes._write_state({
                    "last_reconcile_at": "2026-09-14T00:00:00+00:00",
                    "files": {"/a.md": {"mtime": 5.0, "size": 12}},
                })
                state = notes._read_state()
        self.assertEqual(state["files"]["/a.md"], {"mtime": 5.0, "size": 12})
        self.assertEqual(state["last_reconcile_at"], "2026-09-14T00:00:00+00:00")


# ---- F1: WatchPaths plist rendering ------------------------------------------

class WatchPathsPlistContractTest(unittest.TestCase):
    def test_watch_paths_and_throttle_are_rendered(self):
        with mock.patch.object(notes, "memory_dirs", return_value=[
            Path("/a/memory"), Path("/b/memory"),
        ]):
            data = plistlib.loads(launchd_gen.render_plist("notes_watch"))
        self.assertEqual(data["WatchPaths"], ["/a/memory", "/b/memory"])
        self.assertEqual(data["ThrottleInterval"], 30)
        self.assertEqual(data["ProgramArguments"][-3:], ["notes", "reconcile", "--changed-only"])


# ---- F2: topics in the bounded embed catch-up --------------------------------

class TopicsCatchupContractTest(unittest.TestCase):
    def test_a_topic_with_no_vector_is_embedded_by_a_fake_embedder(self):
        class _Cur:
            def __init__(self):
                self.inserts = []
                self._result = []

            def execute(self, sql, params=None):
                s = " ".join(sql.split())
                if "FROM episodes e WHERE NOT EXISTS" in s:
                    self._result = []
                elif "FROM commitments c WHERE c.status = 'open'" in s:
                    self._result = []
                elif "FROM topics t WHERE t.deleted_at IS NULL" in s:
                    self._result = [("note:fresh", "Fresh", "body text")]
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

        class _Conn:
            def __init__(self, cur):
                self._cur = cur

            def cursor(self):
                return self._cur

            def commit(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        cur = _Cur()
        with mock.patch("khipu.db.connect", return_value=_Conn(cur)), \
                mock.patch.object(em, "_active_profile", return_value="prof-1"), \
                mock.patch.object(em, "embed_batch",
                                   side_effect=lambda api, profile: [[0.0] * em.DIM for _ in api]):
            out = em.embed_recent_missing(limit=10)
        self.assertEqual(out["topics_embedded"], 1)
        self.assertTrue(any(p[1] == "topic" and p[2] == "note:fresh" for p in cur.inserts))


# ---- R7: event_at, never now() -----------------------------------------------

class EventAtContractTest(unittest.TestCase):
    def test_falls_back_frontmatter_then_metadata_then_mtime_never_now(self):
        # frontmatter modified wins
        with tempfile.TemporaryDirectory() as td:
            p = _write(Path(td) / "dated.md", _sample_note_text())
            os.utime(p, (0, 0))
            parsed = notes._note_topic_dict(p, project=None)
        self.assertEqual(parsed["event_at"], "2026-08-17T17:13:10.321000+00:00")

        # no frontmatter date at all -> file mtime, never now()
        text = _sample_note_text().replace("  modified: 2026-08-17T17:13:10.321Z\n", "")
        with tempfile.TemporaryDirectory() as td:
            p = _write(Path(td) / "no-date.md", text)
            old = 1_700_000_000.0
            os.utime(p, (old, old))
            before_now = datetime.now(timezone.utc)
            parsed = notes._note_topic_dict(p, project=None)
        expected = datetime.fromtimestamp(old, tz=timezone.utc).isoformat()
        self.assertEqual(parsed["event_at"], expected)
        self.assertLess(
            datetime.fromisoformat(parsed["event_at"]),
            before_now,
            "event_at must never read as now()",
        )


class ApplyRecencyUsesEventAtContractTest(unittest.TestCase):
    """recency.apply_recency reads row["ts"] — embed._apply_search_filters
    (R7) now fills that field from COALESCE(event_at, updated_at,
    created_at), so a topic whose event_at is old scores as old even when
    it was just re-mirrored."""

    def test_an_old_event_at_scores_lower_than_a_fresh_one(self):
        now = datetime.now(timezone.utc)
        old_row = {"kind": "topic", "id": "old", "score": 0.5,
                   "ts": (now.replace(year=now.year - 1)).isoformat()}
        fresh_row = {"kind": "topic", "id": "fresh", "score": 0.5, "ts": now.isoformat()}
        out = recency.apply_recency([old_row, fresh_row], now=now)
        by_id = {r["id"]: r["score"] for r in out}
        self.assertGreater(by_id["fresh"], by_id["old"])


# ---- F3: per-chunk isolation --------------------------------------------------

class PerChunkIsolationContractTest(unittest.TestCase):
    def test_one_failing_batch_does_not_abort_the_rest(self):
        class _Cur:
            def __init__(self, rows):
                self.rows = rows
                self.inserts = []
                self._result = []

            def execute(self, sql, params=None):
                s = " ".join(sql.split())
                if s.startswith("SELECT id FROM embedding_profiles"):
                    self._result = [("prof-1",)]
                elif s.startswith("DELETE FROM memory_embeddings"):
                    self.rowcount = 0
                elif s.startswith("SELECT kind, ref, chunk_idx, content_hash"):
                    self._result = []
                elif s.startswith("SELECT slug, title, body FROM topics"):
                    self._result = self.rows
                elif s.startswith("INSERT INTO memory_embeddings"):
                    self.inserts.append(params)
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

        class _Conn:
            def __init__(self, cur):
                self._cur = cur

            def cursor(self):
                return self._cur

            def commit(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        rows = [(f"note:n{i}", f"T{i}", "short") for i in range(em.BATCH + 1)]
        cur = _Cur(rows)
        calls = {"n": 0}

        def _embed(api, profile, retries=None, delay=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("embed HTTP 503: unavailable")
            return [[0.0] * em.DIM for _ in api]

        with mock.patch("khipu.db.connect", return_value=_Conn(cur)), \
                mock.patch.object(em, "embed_batch", side_effect=_embed), \
                mock.patch.object(em.time, "sleep", lambda s: None):
            stats = em.backfill(kind="topic")
        self.assertEqual(calls["n"], 2)
        self.assertEqual(stats["failed_chunks"], em.BATCH)
        self.assertEqual(stats["embedded"], 1)


# ---- F3/D1: nightly-last.json -------------------------------------------------

class NightlyLastJsonContractTest(unittest.TestCase):
    def test_every_step_is_persisted(self):
        with tempfile.TemporaryDirectory() as data_td, tempfile.TemporaryDirectory() as log_td:
            script = Path(data_td) / "job.py"
            script.write_text("import sys; sys.exit(0)\n", encoding="utf-8")

            def _run(cmd, stdout, stderr, env):  # noqa: ARG001
                stdout.write(b"ok\n")
                return mock.Mock(returncode=0)

            with mock.patch.object(jobs, "CONSOLIDATE_NIGHTLY", script), \
                    mock.patch.object(jobs, "ensure_data_dir", return_value=Path(data_td)), \
                    mock.patch.object(jobs, "_log_paths", return_value=(
                        Path(log_td) / "out.log", Path(log_td) / "err.log")), \
                    mock.patch.object(jobs.subprocess, "run", side_effect=_run), \
                    mock.patch("khipu.notes.reconcile", return_value={"ok": True}), \
                    mock.patch("khipu.embed.backfill", return_value={"embedded": 0}), \
                    mock.patch("khipu.embed.prune_query_cache", return_value=0), \
                    mock.patch.object(jobs, "_mark_stale_commitments", return_value={"ok": True, "stale": 0}), \
                    mock.patch.object(jobs, "_hygiene_commitments", return_value={"ok": True}):
                jobs.run_nightly()
            payload = json.loads((Path(data_td) / "nightly-last.json").read_text())
        names = {s["name"] for s in payload["steps"]}
        self.assertEqual(
            names,
            {"consolidate_nightly", "notes_reconcile", "embed_backfill",
             "query_cache_prune", "commitments_mark_stale", "commitments_hygiene"},
        )
        for step in payload["steps"]:
            self.assertIn("ok", step)
            self.assertIn("ts", step)


# ---- F5: tombstone + 20% breaker ----------------------------------------------

class TombstoneContractTest(unittest.TestCase):
    def test_a_deleted_note_is_tombstoned_and_the_breaker_holds(self):
        cur = _FakeTopicsCursor()
        for i in range(10):
            cur.topics[f"note:n{i}"] = {"source_path": f"/no/such/n{i}.md", "deleted_at": None}
        # Isolate a SINGLE missing row by giving nine real files.
        with tempfile.TemporaryDirectory() as td:
            for i in range(1, 10):
                p = Path(td) / f"n{i}.md"
                p.write_text("x", encoding="utf-8")
                cur.topics[f"note:n{i}"]["source_path"] = str(p)
            single = notes._tombstone_missing_notes(cur)
        self.assertEqual(single["tombstoned"], 1)
        self.assertFalse(single["skipped"])
        self.assertEqual(cur.topics["note:n0"]["deleted_at"], "tombstoned")

        # Now a mount-blip shape: every source path missing -> the breaker
        # refuses rather than tombstoning the whole corpus.
        cur2 = _FakeTopicsCursor()
        for i in range(5):
            cur2.topics[f"note:m{i}"] = {"source_path": f"/no/such/m{i}.md", "deleted_at": None}
        bulk = notes._tombstone_missing_notes(cur2)
        self.assertTrue(bulk["skipped"])
        self.assertEqual(bulk["tombstoned"], 0)


# ---- D4/F4: status fields present ---------------------------------------------

class StatusFieldsContractTest(unittest.TestCase):
    def test_notes_freshness_and_degraded_count_shape(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as data_td:
            claude_root = Path(td) / "claude_projects"
            _write(claude_root / "-repo-a" / "memory" / "one.md", _sample_note_text())

            class _Cur:
                def execute(self, sql, params=None):
                    pass

                def fetchone(self):
                    return (None,)

            with mock.patch.object(notes, "claude_projects_root", return_value=claude_root), \
                    mock.patch.object(notes, "codex_memories_root", return_value=Path(td) / "no-codex"), \
                    mock.patch("khipu.paths.ensure_data_dir", return_value=Path(data_td)):
                freshness = notes.notes_freshness(_Cur())
        for key in ("newest_note_mtime", "newest_note_topic_event_at", "notes_last_reconcile_at"):
            self.assertIn(key, freshness)

        from khipu import query_log

        with tempfile.TemporaryDirectory() as data_td:
            with mock.patch("khipu.paths.ensure_data_dir", return_value=Path(data_td)), \
                    mock.patch("khipu.paths.data_dir", return_value=Path(data_td)):
                query_log.log_query("q", mode="hybrid", result_count=0, top=[], degraded="no-embedding")
                self.assertEqual(query_log.degraded_count(hours=24), 1)


if __name__ == "__main__":
    unittest.main()
