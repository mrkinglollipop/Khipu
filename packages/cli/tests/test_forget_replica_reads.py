"""A forgotten episode never comes back from the local replica.

The replica keeps a forgotten episode's row and text with deleted_at set (the
hub does the same). Every reader must skip it: keyword and semantic search,
the stale-replica search payload, get, the graph walk, status counts, the
session-start slice, the per-prompt recall lane and the graph-candidates leg.

Each reader runs against a real sqlite replica holding a live control episode
(which must come back, so no check is vacuous) and the forgotten one, after
two kinds of forget: on this Mac (tombstone plus a pending-erasure marker),
and on another Mac followed by a full refresh that re-copies the deleted row
and, in the worst case, a vector the hub still held. Process caches are warmed
before the forget, so a reader that serves a stale generation is caught."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from khipu import graph_candidates as gc
from khipu import hub_snapshot as hs
from khipu import recall_prompt, recall_rule

LIVE = "alpha billing still remembered"
GONE = "alpha billing deliberately forgotten"
TS = "2026-10-01T09:00:00+00:00"


def _vector(values) -> bytes:
    return hs._vector_to_blob(list(values))


def _fill(con: sqlite3.Connection, *, gone_deleted_at: str | None) -> None:
    for eid, summary, deleted in ((1, LIVE, None), (2, GONE, gone_deleted_at)):
        con.execute(
            "INSERT INTO episodes (id, ts, ingested_at, summary, session_id, scope, topics, "
            "people, decisions, preferences, raw, deleted_at) VALUES "
            "(?, ?, ?, ?, 'codex:s', 'acme', '[\"Billing Service\"]', '[]', '[]', '[]', ?, ?)",
            (eid, TS, TS, summary, json.dumps({"project": "acme", "summary": summary}), deleted))
    con.execute("INSERT INTO topics (slug, title, body) VALUES "
                "('billing-service', 'Billing Service', 'alpha billing notes')")
    con.execute("INSERT INTO embedding_profiles (id, provider, model, dim, is_active) "
                "VALUES ('p', 'openai-compatible', 'm', 2, 1)")
    for ref, text, vec in (("1", LIVE, (0.8, 0.6)), ("2", GONE, (1.0, 0.0))):
        con.execute("INSERT INTO memory_embeddings (profile, kind, ref, chunk_idx, chunk_text, "
                    "embedding) VALUES ('p', 'episode', ?, 0, ?, ?)", (ref, text, _vector(vec)))


class ForgottenEpisodeStaysForgottenInTheReplicaTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        self.snap = self.data / "hub_snapshot.sqlite"
        con = sqlite3.connect(str(self.snap))
        hs._create_schema(con)
        _fill(con, gone_deleted_at=None)
        con.commit()
        con.close()
        stack = ExitStack()
        self.addCleanup(stack.close)
        for patch in (
            mock.patch.object(hs, "snapshot_path", return_value=self.snap),
            mock.patch.object(hs, "meta_path", return_value=self.data / "meta.json"),
            mock.patch("khipu.paths.ensure_data_dir", return_value=self.data),
            mock.patch.dict(os.environ, {"KHIPU_OUTBOX": str(self.data / "outbox")}),
            mock.patch("khipu.features.enabled", side_effect=lambda name: name == "graph_candidates"),
        ):
            stack.enter_context(patch)
        self.reset_caches()
        self.addCleanup(self.reset_caches)

    @staticmethod
    def reset_caches():
        with gc._SLUG_LOCK:
            gc._SLUG_CACHE.clear()
            gc._SLUG_RELOADING = False
        hs._MATRIX_CACHE.clear()

    def warm_caches(self):
        """Load the slug map and the vector matrix while episode 2 is live."""
        gc._rebuild_slug_map(gc._slug_map_key())
        self.assertIn("billing-service", gc._SLUG_CACHE["map"])
        self.assertEqual({r["id"] for r in hs.cosine_candidates_snapshot([1.0, 0.0], "p", limit=5)},
                         {"1", "2"})

    def forget_here(self):
        self.assertTrue(hs.forget_episode_in_snapshot(2)["ok"])

    def forget_elsewhere_then_refresh(self):
        """Another Mac forgot episode 2: this Mac has no marker, and a full
        refresh copies the hub row with deleted_at set. The hub still holding
        a vector for it is the worst case the readers must survive."""

        class Pg:
            def __init__(self):
                self._result = []

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def cursor(self):
                return self

            def execute(self, sql, params=None):
                if sql == "SELECT now()":
                    self._result = [(datetime(2026, 10, 3, tzinfo=timezone.utc),)]
                elif "FROM information_schema.columns" in sql and params in (("ledger_rows",), ("commitments",), ("project_aliases",)):
                    # This fixture has no optional ledger or commitments schema.
                    self._result = []
                else:
                    raise AssertionError(f"unexpected query: {sql}")

            def fetchone(self):
                return self._result.pop(0) if self._result else None

            def fetchall(self):
                rows, self._result = self._result, []
                return rows

        def episodes(_cur, con):
            _fill(con, gone_deleted_at="2026-10-02T00:00:00+00:00")
            return 2

        zero = {name: mock.Mock(return_value=0) for name in (
            "_insert_topics", "_insert_topic_revisions", "_insert_nodes", "_insert_edges",
            "_insert_profiles", "_insert_memory_embeddings", "_insert_decisions",
            "_insert_deliverables")}
        time.sleep(0.01)  # a new mtime, as any real dump has
        with mock.patch.object(hs, "try_hub_connect", return_value=Pg()), \
                mock.patch.object(hs, "_insert_episodes", side_effect=episodes), \
                mock.patch.multiple(hs, **zero):
            self.assertTrue(hs.refresh()["ok"])
        self.assertEqual(hs._pending_erasure_ids(self.snap), set())
        con = sqlite3.connect(str(self.snap))
        try:
            row = con.execute("SELECT summary, deleted_at FROM episodes WHERE id = 2").fetchone()
        finally:
            con.close()
        self.assertEqual(row[0], GONE)
        self.assertIsNotNone(row[1])

    def assert_only_the_live_episode(self, label, found, text):
        self.assertIn("1", found, (label, "the live control did not come back"))
        self.assertNotIn("2", found, (label, "the forgotten episode came back"))
        self.assertNotIn("deliberately forgotten", text, (label, "its text came back"))

    def check_every_reader(self):
        rows = hs.search_snapshot("alpha billing", 10, kind=None)
        self.assert_only_the_live_episode(
            "search_snapshot", {r["id"] for r in rows if r["kind"] == "episode"},
            json.dumps(rows, default=str))

        payload = hs.search_stale_payload("alpha billing", 10)
        self.assert_only_the_live_episode(
            "search_stale_payload", {r["id"] for r in payload["results"] if r["kind"] == "episode"},
            json.dumps(payload, default=str))

        spec = SimpleNamespace(id="p", provider="openai-compatible", model="m",
                               endpoint="http://127.0.0.1:9/v1", dim=2)
        with mock.patch.object(hs, "active_snapshot_profile_spec", return_value=spec), \
                mock.patch("khipu.models.show_models", return_value={"embed": {
                    "provider": "local", "endpoint": spec.endpoint, "model_id": "m"}}), \
                mock.patch("khipu.profiles.register_spec"), \
                mock.patch("khipu.embed.uses_task_prefixes", return_value=False), \
                mock.patch("khipu.embed.embed_one", return_value=[1.0, 0.0]):
            semantic = hs.semantic_search_snapshot("alpha billing", limit=10)
        self.assert_only_the_live_episode(
            "semantic_search_snapshot", {r["id"] for r in semantic if r["kind"] == "episode"},
            json.dumps(semantic, default=str))

        self.assertIsNotNone(hs.episode_detail_snapshot(1))
        self.assertIsNone(hs.episode_detail_snapshot(2))
        self.assertFalse(hs.graph_neighbors_snapshot("1", 1, 5)["episode"]["missing"])
        self.assertTrue(hs.graph_neighbors_snapshot("2", 1, 5)["episode"]["missing"])
        self.assertEqual(hs.status_payload_snapshot()["counts"]["episodes"], 1)

        pushed = recall_rule._stale_project_slice("acme", None)
        self.assertIn("still remembered", pushed)
        self.assertNotIn("deliberately forgotten", pushed)

        con = hs.open_snapshot()
        try:
            cands, missed = gc.replica_candidates(
                con, [{"kind": "topic", "id": "billing-service"}], deadline=time.monotonic() + 5)
        finally:
            con.close()
        self.assertFalse(missed)
        self.assert_only_the_live_episode(
            "graph_candidates replica leg", {r["id"] for r in cands if r["kind"] == "episode"},
            json.dumps(cands, default=str))

        with mock.patch.object(hs, "snapshot_is_fresh", return_value=(True, {"exists": True})), \
                mock.patch.object(hs, "active_snapshot_profile", return_value="p"), \
                mock.patch.object(recall_prompt, "_cached_query_embed", return_value=[1.0, 0.0]):
            lane = recall_prompt._snapshot_search_hits("alpha billing", project=None,
                                                       deadline=time.monotonic() + 30)
        self.assert_only_the_live_episode(
            "prompt recall lane", {str(h.get("id")) for h in lane["hits"] if h.get("kind") == "episode"},
            json.dumps(lane, default=str))

    def test_forgotten_on_this_mac(self):
        self.warm_caches()
        self.forget_here()
        self.check_every_reader()

    def test_forgotten_on_another_mac_then_a_full_refresh(self):
        self.warm_caches()
        self.forget_elsewhere_then_refresh()
        self.check_every_reader()


if __name__ == "__main__":
    unittest.main()


class PendingCaptureOfAForgottenSourceStaysHiddenOfflineTest(unittest.TestCase):
    """The offline answer adds captures still waiting in the outbox. A forget
    can leave its job there, so each pending capture passes the forgotten-
    source registry first, and none is shown when the registry is unreadable."""

    PENDING_LIVE = {"ts": TS, "summary": "alpha billing pending and kept",
                    "session_id": "codex:kept", "project": "acme"}
    PENDING_GONE = {"ts": TS, "summary": "alpha billing pending then forgotten",
                    "session_id": "codex:gone", "project": "acme"}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        self.snap = self.data / "hub_snapshot.sqlite"
        con = sqlite3.connect(str(self.snap))
        hs._create_schema(con)
        _fill(con, gone_deleted_at=None)
        con.commit()
        con.close()
        stack = ExitStack()
        self.addCleanup(stack.close)
        for patch in (
            mock.patch.object(hs, "snapshot_path", return_value=self.snap),
            mock.patch.object(hs, "meta_path", return_value=self.data / "meta.json"),
            mock.patch("khipu.paths.ensure_data_dir", return_value=self.data),
            mock.patch.dict(os.environ, {"KHIPU_OUTBOX": str(self.data / "outbox")}),
        ):
            stack.enter_context(patch)
        from khipu import outbox

        for payload in (self.PENDING_LIVE, self.PENDING_GONE):
            outbox.enqueue(dict(payload), reason="test")

    def summaries(self) -> str:
        out = hs.search_stale_payload("alpha billing pending", 20)
        return json.dumps([r for r in out["results"] if r.get("outbox")])

    def test_a_normal_pending_capture_still_appears(self):
        found = self.summaries()
        self.assertIn(self.PENDING_LIVE["summary"], found)
        self.assertIn(self.PENDING_GONE["summary"], found)

    def test_a_pending_capture_whose_source_was_forgotten_never_appears(self):
        from khipu import tombstones

        tombstones.publish_payloads([dict(self.PENDING_GONE)])
        found = self.summaries()
        self.assertIn(self.PENDING_LIVE["summary"], found)
        self.assertNotIn(self.PENDING_GONE["summary"], found)

    def test_an_unreadable_registry_shows_no_pending_capture(self):
        from khipu import tombstones

        path = tombstones.registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        found = self.summaries()
        self.assertNotIn(self.PENDING_LIVE["summary"], found)
        self.assertNotIn(self.PENDING_GONE["summary"], found)
        out = hs.search_stale_payload("alpha billing", 20)
        self.assertTrue(any(str(r.get("id")) == "1" for r in out["results"]), out["results"])
