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


if __name__ == "__main__":
    unittest.main()
