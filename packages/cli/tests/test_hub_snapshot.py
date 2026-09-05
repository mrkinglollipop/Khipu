"""Unit tests for hub_snapshot — schema, search, stale flags; connect stays PG-only."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import hub_snapshot as hs


class SnapshotSchemaTest(unittest.TestCase):
    def test_create_schema_and_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            snap = data / "hub_snapshot.sqlite"
            with (
                mock.patch.object(hs, "snapshot_path", return_value=snap),
                mock.patch.object(hs, "meta_path", return_value=data / "meta.json"),
            ):
                con = sqlite3.connect(str(snap))
                hs._create_schema(con)
                con.execute(
                    "INSERT INTO episodes (id, ts, summary, topics, people, decisions, preferences) "
                    "VALUES (1, '2026-08-27T00:00:00Z', 'alpha join kit', '[]', '[]', '[]', '[]')"
                )
                con.execute(
                    "INSERT INTO topics (slug, title, body) VALUES ('join-kit', 'Join', 'hub join passphrase')"
                )
                con.execute(
                    "INSERT INTO nodes (id, type, name) VALUES ('node:alpha', 'topic', 'Alpha')"
                )
                con.execute(
                    "INSERT INTO edges (src, dst, type, weight) VALUES ('node:alpha', 'node:beta', 'related', 1.0)"
                )
                con.execute(
                    "INSERT INTO nodes (id, type, name) VALUES ('node:beta', 'topic', 'Beta')"
                )
                con.commit()
                con.close()

                results = hs.search_snapshot("join kit", 12)
                kinds = {r["kind"] for r in results}
                self.assertTrue(kinds & {"episode", "topic", "node"})
                graph = hs.graph_neighbors_snapshot("node:alpha", 1, 25)
                self.assertTrue(
                    graph.get("neighbors") or graph.get("edges") or graph.get("nodes")
                )


class StaleSearchTest(unittest.TestCase):
    def test_search_stale_when_connect_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            snap = data / "hub_snapshot.sqlite"
            meta = data / "hub_snapshot.sqlite.meta.json"
            meta.write_text(
                '{"refreshed_at":"2026-08-27T12:00:00+00:00","bytes":123}',
                encoding="utf-8",
            )
            con = sqlite3.connect(str(snap))
            hs._create_schema(con)
            con.execute(
                "INSERT INTO episodes (id, ts, summary, topics, people, decisions, preferences) "
                "VALUES (7, '2026-08-27T00:00:00Z', 'offline recall chimichanga', '[]', '[]', '[]', '[]')"
            )
            con.commit()
            con.close()
            with (
                mock.patch.object(hs, "snapshot_path", return_value=snap),
                mock.patch.object(hs, "meta_path", return_value=meta),
                mock.patch.object(
                    hs,
                    "meta",
                    return_value={
                        "exists": True,
                        "refreshed_at": "2026-08-27T12:00:00+00:00",
                    },
                ),
            ):
                payload = hs.search_stale_payload("chimichanga", 10)
            self.assertTrue(payload.get("stale"))
            self.assertTrue(any(r.get("kind") == "episode" for r in payload["results"]))

    def test_cmd_snapshot_refresh_uses_ok_not_failed(self) -> None:
        import io
        from argparse import Namespace

        from khipu.cli import cmd_snapshot

        fake = {
            "ok": True,
            "path": "/tmp/hub_snapshot.sqlite",
            "refreshed_at": "2026-08-27T12:00:00+00:00",
            "size_bytes": 12,
            "counts": {},
        }
        with (
            mock.patch("khipu.hub_snapshot.refresh", return_value=fake),
            mock.patch("sys.stdout", io.StringIO()),
        ):
            rc = cmd_snapshot(Namespace(snapshot_cmd="refresh"))
        self.assertEqual(rc, 0)

    def test_maybe_refresh_skips_when_snapshot_is_young(self) -> None:
        with (
            mock.patch.object(
                hs,
                "snapshot_health",
                return_value={"exists": True, "age_seconds": 12},
            ),
            mock.patch.object(hs, "refresh") as refresh,
            mock.patch.object(hs, "try_hub_connect") as connect,
        ):
            out = hs.maybe_refresh()
        self.assertIsNone(out)
        refresh.assert_not_called()
        connect.assert_not_called()

    def test_maybe_refresh_force_ignores_age(self) -> None:
        cur = mock.MagicMock()
        conn = mock.MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn
        fake = {"ok": True}
        with (
            mock.patch.object(
                hs,
                "snapshot_health",
                return_value={"exists": True, "age_seconds": 12},
            ),
            mock.patch.object(hs, "try_hub_connect", return_value=cm),
            mock.patch.object(hs, "refresh", return_value=fake) as refresh,
        ):
            out = hs.maybe_refresh(force=True)
        self.assertEqual(out, fake)
        refresh.assert_called_once()

    def test_maybe_refresh_runs_when_snapshot_is_stale(self) -> None:
        cur = mock.MagicMock()
        conn = mock.MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn
        fake = {"ok": True}
        with (
            mock.patch.object(
                hs,
                "snapshot_health",
                return_value={"exists": True, "age_seconds": hs.AUTO_REFRESH_MIN_AGE_S + 1},
            ),
            mock.patch.object(hs, "try_hub_connect", return_value=cm),
            mock.patch.object(hs, "refresh", return_value=fake) as refresh,
        ):
            out = hs.maybe_refresh()
        self.assertEqual(out, fake)
        refresh.assert_called_once()

    def test_refresh_returns_error_when_already_running(self) -> None:
        with mock.patch.object(hs, "_acquire_refresh_lock", return_value=None):
            out = hs.refresh()
        self.assertFalse(out.get("ok"))
        self.assertIn("already in progress", str(out.get("error") or "").lower())

    def test_maybe_refresh_skips_when_already_running(self) -> None:
        # Lock lives in refresh(); maybe_refresh must not dump around it.
        cur = mock.MagicMock()
        conn = mock.MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cm = mock.MagicMock()
        cm.__enter__.return_value = conn
        with (
            mock.patch.object(hs, "try_hub_connect", return_value=cm),
            mock.patch.object(hs, "_acquire_refresh_lock", return_value=None),
        ):
            out = hs.maybe_refresh(force=True)
        self.assertIsNone(out)

    def test_maybe_refresh_skips_young_file_with_bad_meta(self) -> None:
        """Existing young sqlite with missing/unparseable refreshed_at must skip."""
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            snap = data / "hub_snapshot.sqlite"
            snap.write_bytes(b"sqlite-placeholder")
            meta = data / "hub_snapshot.sqlite.meta.json"
            meta.write_text('{"refreshed_at": "not-a-timestamp"}', encoding="utf-8")
            with (
                mock.patch.object(hs, "snapshot_path", return_value=snap),
                mock.patch.object(hs, "meta_path", return_value=meta),
                mock.patch.object(hs, "refresh") as refresh,
                mock.patch.object(hs, "try_hub_connect") as connect,
            ):
                health = hs.snapshot_health()
                self.assertTrue(health.get("exists"))
                self.assertIsInstance(health.get("age_seconds"), int)
                self.assertGreaterEqual(health["age_seconds"], 0)
                self.assertLess(health["age_seconds"], hs.AUTO_REFRESH_MIN_AGE_S)
                out = hs.maybe_refresh()
            self.assertIsNone(out)
            refresh.assert_not_called()
            connect.assert_not_called()

    def test_cmd_status_does_not_dump_snapshot(self) -> None:
        import io
        from argparse import Namespace

        from khipu.cli import cmd_status

        with (
            mock.patch(
                "khipu.drift.status_payload",
                return_value={"ok": True, "counts": {}},
            ),
            mock.patch("khipu.hub_snapshot.maybe_refresh") as refresh,
            mock.patch(
                "khipu.hub_snapshot.snapshot_health",
                return_value={"exists": False},
            ),
            mock.patch("sys.stdout", io.StringIO()),
        ):
            rc = cmd_status(Namespace(memory_root=None, sample=0, drift=False))
        self.assertEqual(rc, 0)
        refresh.assert_not_called()

    def test_connect_never_opens_snapshot(self) -> None:
        """db.connect must stay Postgres-only (regression guard)."""
        from khipu import db

        src = Path(db.__file__).read_text(encoding="utf-8")
        self.assertNotIn("hub_snapshot", src)
        self.assertNotIn("sqlite3.connect", src)


class MergeOutboxTest(unittest.TestCase):
    def test_merge_outbox_episodes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"KHIPU_OUTBOX": td}):
                from khipu import outbox

                outbox.enqueue(
                    {"ts": "2026-08-27T18:00:00Z", "summary": "queued while offline"},
                    reason="test",
                )
                merged = hs.merge_outbox_episodes([])
                self.assertTrue(
                    any(
                        "queued while offline" in (r.get("label") or "") for r in merged
                    )
                )


def _make_snapshot(data: Path) -> Path:
    snap = data / "hub_snapshot.sqlite"
    con = sqlite3.connect(str(snap))
    hs._create_schema(con)
    con.execute(
        "INSERT INTO episodes (id, ts, summary, session_id, scope, topics, people, "
        "decisions, preferences) VALUES "
        "(1, '2020-01-01T00:00:00+00:00', 'old alpha episode', 'claude_code:a', 'x', "
        "'[]', '[]', '[]', '[]')"
    )
    con.execute(
        "INSERT INTO episodes (id, ts, summary, session_id, scope, topics, people, "
        "decisions, preferences) VALUES "
        "(2, '2026-08-30T00:00:00+00:00', 'fresh alpha episode', 'claude_code:b', 'x', "
        "'[]', '[]', '[]', '[]')"
    )
    con.execute(
        "INSERT INTO topics (slug, title, body) VALUES ('alpha-topic', 'Alpha', 'alpha topic body')"
    )
    con.execute(
        "INSERT INTO nodes (id, type, name) VALUES ('module:alpha', 'module', 'alpha node')"
    )
    con.commit()
    con.close()
    return snap


class SearchSnapshotFilterTest(unittest.TestCase):
    def _open(self, data: Path):
        return (
            mock.patch.object(hs, "snapshot_path", return_value=_make_snapshot(data)),
            mock.patch.object(hs, "meta_path", return_value=data / "meta.json"),
        )

    def test_nodes_excluded_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            p1, p2 = self._open(data)
            with p1, p2:
                results = hs.search_snapshot("alpha", 10)
        self.assertNotIn("node", {r["kind"] for r in results})

    def test_kind_node_returns_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            p1, p2 = self._open(data)
            with p1, p2:
                results = hs.search_snapshot("alpha", 10, kind="node")
        self.assertTrue(results)
        self.assertTrue(all(r["kind"] == "node" for r in results))

    def test_kind_topic_restricts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            p1, p2 = self._open(data)
            with p1, p2:
                results = hs.search_snapshot("alpha", 10, kind="topic")
        self.assertTrue(all(r["kind"] == "topic" for r in results))

    def test_since_excludes_older_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            p1, p2 = self._open(data)
            with p1, p2:
                results = hs.search_snapshot("alpha", 10, kind="episode", since="7d")
        ids = {r["id"] for r in results}
        self.assertIn("2", ids)
        self.assertNotIn("1", ids)


def _make_identity_snapshot(data: Path) -> Path:
    """fix 7/9: episodes carrying project/session_id/harness, for the
    snapshot-mode metadata filter tests."""
    snap = data / "hub_snapshot.sqlite"
    con = sqlite3.connect(str(snap))
    hs._create_schema(con)
    con.execute(
        "INSERT INTO episodes (id, ts, summary, session_id, scope, project, harness, "
        "topics, people, decisions, preferences) VALUES "
        "(1, '2026-08-30T00:00:00+00:00', 'alpha in acme widget', 'claude_code:host-1', "
        "'x', 'acme/widget', 'claude_code', '[]', '[]', '[]', '[]')"
    )
    con.execute(
        "INSERT INTO episodes (id, ts, summary, session_id, scope, project, harness, "
        "topics, people, decisions, preferences) VALUES "
        "(2, '2026-08-30T00:00:00+00:00', 'alpha in other repo', 'cursor:host-2', "
        "'y', 'other/repo', 'cursor', '[]', '[]', '[]', '[]')"
    )
    con.execute(
        "INSERT INTO topics (slug, title, body) VALUES ('alpha-topic', 'Alpha', 'alpha topic body')"
    )
    con.commit()
    con.close()
    return snap


class SearchSnapshotIdentityFilterTest(unittest.TestCase):
    """fix 7: project/session_id/harness on the sqlite fallback, same
    episode-only semantics as embed._apply_search_filters."""

    def _open(self, data: Path):
        return (
            mock.patch.object(hs, "snapshot_path", return_value=_make_identity_snapshot(data)),
            mock.patch.object(hs, "meta_path", return_value=data / "meta.json"),
        )

    def test_project_filter_matches_only_that_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            p1, p2 = self._open(data)
            with p1, p2:
                results = hs.search_snapshot("alpha", 10, project="acme/widget")
        ids = {r["id"] for r in results}
        self.assertEqual(ids, {"1"})

    def test_harness_filter_uses_the_harness_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            p1, p2 = self._open(data)
            with p1, p2:
                results = hs.search_snapshot("alpha", 10, harness="cursor")
        ids = {r["id"] for r in results}
        self.assertEqual(ids, {"2"})

    def test_session_id_prefix_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            p1, p2 = self._open(data)
            with p1, p2:
                results = hs.search_snapshot("alpha", 10, session_id="claude_code:host-1")
        ids = {r["id"] for r in results}
        self.assertEqual(ids, {"1"})

    def test_identity_filter_excludes_topics_and_nodes(self) -> None:
        """project/session_id/harness only exist on episodes — matches the
        PG-path rule (embed._apply_search_filters): a topic hit is dropped
        outright rather than guessed at."""
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            p1, p2 = self._open(data)
            with p1, p2:
                results = hs.search_snapshot("alpha", 10, project="acme/widget")
        self.assertTrue(all(r["kind"] == "episode" for r in results))

    def test_explicit_non_episode_kind_with_identity_filter_yields_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            p1, p2 = self._open(data)
            with p1, p2:
                results = hs.search_snapshot("alpha", 10, kind="topic", project="acme/widget")
        self.assertEqual(results, [])


class SearchStalePayloadFiltersDroppedTest(unittest.TestCase):
    def test_filters_dropped_is_present_and_empty_on_a_current_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            p1, p2 = (
                mock.patch.object(hs, "snapshot_path", return_value=_make_identity_snapshot(data)),
                mock.patch.object(hs, "meta_path", return_value=data / "meta.json"),
            )
            with p1, p2:
                payload = hs.search_stale_payload(
                    "alpha", 10, project="acme/widget", session_id="claude_code:host-1"
                )
        self.assertIn("filters_dropped", payload)
        self.assertEqual(payload["filters_dropped"], [])

    def test_filters_dropped_names_session_id_on_a_pre_migration_snapshot(self) -> None:
        """A snapshot dumped before this fix has no session_id-adjacent
        columns at all (simulated here by dropping the column outright) —
        the filter must be named, never silently ignored."""
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            snap = data / "hub_snapshot.sqlite"
            con = sqlite3.connect(str(snap))
            con.execute(
                "CREATE TABLE episodes (id INTEGER PRIMARY KEY, ts TEXT, summary TEXT NOT NULL, "
                "topics TEXT, people TEXT, decisions TEXT, preferences TEXT, scope TEXT)"
            )
            con.execute(
                "CREATE TABLE topics (slug TEXT PRIMARY KEY, title TEXT, body TEXT, "
                "status TEXT, created_at TEXT, updated_at TEXT, links TEXT, frontmatter TEXT, "
                "source_path TEXT, content_hash TEXT, deleted_at TEXT)"
            )
            con.execute(
                "CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT, bucket TEXT, name TEXT, "
                "payload TEXT, source_path TEXT, built_at TEXT, frozen INTEGER, source_id TEXT)"
            )
            con.commit()
            con.close()
            p1, p2 = (
                mock.patch.object(hs, "snapshot_path", return_value=snap),
                mock.patch.object(hs, "meta_path", return_value=data / "meta.json"),
            )
            with p1, p2:
                payload = hs.search_stale_payload("alpha", 10, session_id="claude_code:x")
        self.assertIn("session_id", payload["filters_dropped"])


class UpsertEpisodeTest(unittest.TestCase):
    def test_upsert_adds_episode_and_embedding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            snap = _make_snapshot(data)
            with (
                mock.patch.object(hs, "snapshot_path", return_value=snap),
                mock.patch.object(hs, "meta_path", return_value=data / "meta.json"),
            ):
                out = hs.upsert_episode(
                    {
                        "id": 99,
                        "ts": "2026-09-03T12:00:00+00:00",
                        "session_id": "claude_code:z",
                        "summary": "brand new upserted episode",
                        "topics": ["khipu"],
                        "scope": "khipu",
                    },
                    [
                        {
                            "profile": "gemini-embedding-2@768",
                            "kind": "episode",
                            "ref": "99",
                            "chunk_idx": 0,
                            "chunk_text": "brand new upserted episode",
                            "content_hash": "deadbeef",
                            "embedding": [0.1, 0.2, 0.3],
                            "built_at": "2026-09-03T12:00:01+00:00",
                        }
                    ],
                )
                self.assertTrue(out["ok"])
                con = sqlite3.connect(str(snap))
                row = con.execute(
                    "SELECT summary FROM episodes WHERE id = 99"
                ).fetchone()
                self.assertEqual(row[0], "brand new upserted episode")
                emb = con.execute(
                    "SELECT COUNT(*) FROM memory_embeddings WHERE ref = '99'"
                ).fetchone()
                self.assertEqual(emb[0], 1)
                con.close()

    def test_upsert_replaces_existing_episode_row(self) -> None:
        """A re-embed of the same episode must not duplicate the row."""
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            snap = _make_snapshot(data)
            with (
                mock.patch.object(hs, "snapshot_path", return_value=snap),
                mock.patch.object(hs, "meta_path", return_value=data / "meta.json"),
            ):
                for summary in ("first pass", "second pass"):
                    hs.upsert_episode(
                        {"id": 1, "ts": "2020-01-01T00:00:00+00:00", "summary": summary},
                        [],
                    )
                con = sqlite3.connect(str(snap))
                rows = con.execute("SELECT summary FROM episodes WHERE id = 1").fetchall()
                con.close()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0][0], "second pass")

    def test_missing_snapshot_is_a_clean_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            with (
                mock.patch.object(hs, "snapshot_path", return_value=data / "missing.sqlite"),
                mock.patch.object(hs, "meta_path", return_value=data / "meta.json"),
            ):
                out = hs.upsert_episode({"id": 1, "summary": "x"}, [])
        self.assertFalse(out["ok"])

    def test_refresh_lock_held_elsewhere_is_a_clean_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            snap = _make_snapshot(data)
            with (
                mock.patch.object(hs, "snapshot_path", return_value=snap),
                mock.patch.object(hs, "meta_path", return_value=data / "meta.json"),
                mock.patch.object(hs, "_acquire_refresh_lock", return_value=None),
            ):
                out = hs.upsert_episode({"id": 1, "summary": "x"}, [])
        self.assertFalse(out["ok"])


class SnapshotFreshnessTest(unittest.TestCase):
    def test_behind_ingest_seconds_computed(self) -> None:
        health = {"ok": True, "refreshed_at": "2026-09-03T12:00:00+00:00"}
        out = hs.snapshot_freshness("2026-09-03T12:10:00+00:00", health)
        self.assertEqual(out["behind_ingest_seconds"], 600.0)
        self.assertTrue(out["ok"])

    def test_ok_false_past_the_max(self) -> None:
        health = {"ok": True, "refreshed_at": "2026-09-03T00:00:00+00:00"}
        out = hs.snapshot_freshness("2026-09-03T01:00:00+00:00", health)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "snapshot_behind_ingest")

    def test_missing_timestamps_are_none_not_crash(self) -> None:
        out = hs.snapshot_freshness(None, {"ok": True, "refreshed_at": None})
        self.assertIsNone(out["behind_ingest_seconds"])


if __name__ == "__main__":
    unittest.main()


def _make_forgotten_snapshot(data: Path) -> Path:
    """One live episode and one tombstoned (`khipu episode --forget`) episode,
    both matching the query, plus a vector for the tombstone."""
    snap = data / "hub_snapshot.sqlite"
    con = sqlite3.connect(str(snap))
    hs._create_schema(con)
    con.execute(
        "INSERT INTO episodes (id, ts, summary, session_id, scope, topics, people, "
        "decisions, preferences) VALUES "
        "(1, '2026-08-30T00:00:00+00:00', 'alpha still remembered', 'claude_code:a', 'x', "
        "'[]', '[]', '[]', '[]')"
    )
    con.execute(
        "INSERT INTO episodes (id, ts, summary, session_id, scope, topics, people, "
        "decisions, preferences, deleted_at) VALUES "
        "(2, '2026-08-30T00:00:00+00:00', 'alpha deliberately forgotten', 'claude_code:b', 'x', "
        "'[]', '[]', '[]', '[]', '2026-09-01T00:00:00+00:00')"
    )
    con.commit()
    con.close()
    return snap


class SnapshotNeverResurrectsAForgottenEpisodeTest(unittest.TestCase):
    """Audit 2026-09-04: topics were excluded when tombstoned but episodes were
    not, so `khipu episode --forget` was undone by the next offline search."""

    def _open(self, data: Path):
        return (
            mock.patch.object(hs, "snapshot_path", return_value=_make_forgotten_snapshot(data)),
            mock.patch.object(hs, "meta_path", return_value=data / "meta.json"),
        )

    def test_keyword_search_skips_the_tombstone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            p1, p2 = self._open(data)
            with p1, p2:
                results = hs.search_snapshot("alpha", 10, kind="episode")
        ids = {r["id"] for r in results}
        self.assertIn("1", ids)
        self.assertNotIn("2", ids, "a forgotten episode came back from the snapshot")
