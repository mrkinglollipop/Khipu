"""graph_backup — snapshot health, offsite argv, scratch drill (unittest + tempfile)."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from khipu import graph_backup


class LocalHealthTest(unittest.TestCase):
    def test_non_producer_is_ok_skipped(self):
        out = graph_backup.local_health(producer=False)
        self.assertTrue(out["ok"])
        self.assertTrue(out["skipped"])

    def test_missing_dir_on_producer_is_red(self):
        out = graph_backup.local_health(
            producer=True,
            snapshot_dir=Path("/tmp/khipu-no-snaps-graph-backup-test"),
        )
        self.assertFalse(out["ok"])

    def test_fresh_snapshot_is_ok(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "graph-20260819T000000Z.sqlite").write_bytes(b"sqlite")
            out = graph_backup.local_health(
                producer=True,
                snapshot_dir=d,
                now=datetime(2026, 8, 19, tzinfo=timezone.utc),
            )
            self.assertTrue(out["ok"])

    def test_stale_snapshot_is_red(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            p = d / "graph-20260101T000000Z.sqlite"
            p.write_bytes(b"sqlite")
            os.utime(p, (0, 0))
            out = graph_backup.local_health(
                producer=True,
                snapshot_dir=d,
                now=datetime(2026, 8, 19, tzinfo=timezone.utc),
            )
            self.assertFalse(out["ok"])


class OffsiteDueTest(unittest.TestCase):
    def test_never_ok_is_due(self):
        now = datetime(2026, 8, 19, tzinfo=timezone.utc)
        self.assertTrue(graph_backup.offsite_due(last_ok=None, now=now))

    def test_recent_ok_is_not_due(self):
        now = datetime(2026, 8, 19, tzinfo=timezone.utc)
        last = datetime(2026, 8, 18, tzinfo=timezone.utc)
        self.assertFalse(graph_backup.offsite_due(last_ok=last, now=now, period_days=7))

    def test_old_ok_is_due(self):
        now = datetime(2026, 8, 19, tzinfo=timezone.utc)
        last = datetime(2026, 8, 1, tzinfo=timezone.utc)
        self.assertTrue(graph_backup.offsite_due(last_ok=last, now=now, period_days=7))


class DrillDueTest(unittest.TestCase):
    def test_never_drilled_is_due(self):
        now = datetime(2026, 8, 19, tzinfo=timezone.utc)
        self.assertTrue(graph_backup.drill_due(last_ok=None, now=now))

    def test_recent_drill_is_not_due(self):
        now = datetime(2026, 8, 19, tzinfo=timezone.utc)
        last = datetime(2026, 8, 18, tzinfo=timezone.utc)
        self.assertFalse(graph_backup.drill_due(last_ok=last, now=now, period_days=8))


class RunOffsiteTest(unittest.TestCase):
    def test_missing_r2_remote_fails_honest(self):
        with mock.patch.object(graph_backup, "has_r2_remote", return_value=False):
            out = graph_backup.run_offsite()
        self.assertFalse(out["ok"])
        self.assertIn("r2", out["reason"].lower())

    def test_copyto_uses_snapshot_not_live_db(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            snap = d / "graph-20260819T000000Z.sqlite"
            snap.write_bytes(b"sqlite")
            live = d / "live-graph.sqlite"
            live.write_bytes(b"live")

            calls: list[list[str]] = []

            def fake_run(argv, **kwargs):
                calls.append(list(argv))
                if argv[1] == "copyto":
                    return mock.Mock(returncode=0, stdout="", stderr="")
                if argv[1] == "lsf":
                    return mock.Mock(returncode=0, stdout=f"{snap.name}\n", stderr="")
                return mock.Mock(returncode=0, stdout="", stderr="")

            with (
                mock.patch.object(graph_backup, "DEFAULT_SNAPSHOT_DIR", d),
                mock.patch.object(graph_backup, "LIVE_GRAPH", live),
                mock.patch.object(graph_backup, "has_r2_remote", return_value=True),
                mock.patch("khipu.graph_backup.subprocess.run", side_effect=fake_run),
                mock.patch(
                    "khipu.ops_events.record", return_value={"ok": True, "id": 1}
                ),
            ):
                out = graph_backup.run_offsite()

            self.assertTrue(out["ok"])
            copy_calls = [c for c in calls if len(c) > 1 and c[1] == "copyto"]
            self.assertEqual(len(copy_calls), 1)
            argv = copy_calls[0]
            self.assertEqual(argv[2], str(snap))
            self.assertTrue(argv[3].startswith("r2:matt-db-backups/khipu-graph/"))
            joined = " ".join(argv)
            self.assertNotIn(str(live.resolve()), joined)

    def test_offsite_fail_surfaces_ops_error_when_record_raises(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            snap = d / "graph-20260819T000000Z.sqlite"
            snap.write_bytes(b"sqlite")
            live = d / "live-graph.sqlite"
            live.write_bytes(b"live")

            def fake_run(argv, **kwargs):
                if argv[1] == "copyto":
                    return mock.Mock(returncode=1, stdout="", stderr="rclone boom")
                return mock.Mock(returncode=0, stdout="", stderr="")

            with (
                mock.patch.object(graph_backup, "DEFAULT_SNAPSHOT_DIR", d),
                mock.patch.object(graph_backup, "LIVE_GRAPH", live),
                mock.patch.object(graph_backup, "has_r2_remote", return_value=True),
                mock.patch("khipu.graph_backup.subprocess.run", side_effect=fake_run),
                mock.patch(
                    "khipu.ops_events.record",
                    side_effect=RuntimeError("dsn down"),
                ),
            ):
                out = graph_backup.run_offsite()

            self.assertFalse(out["ok"])
            self.assertIn("ops_error", out)
            self.assertIn("dsn down", out["ops_error"])


class ScratchDrillTest(unittest.TestCase):
    def _tiny_db(self, path: Path) -> None:
        con = sqlite3.connect(path)
        try:
            con.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY)")
            con.execute("INSERT INTO nodes (id) VALUES ('n1')")
            con.execute(
                "CREATE TABLE embeddings (node_id TEXT, chunk_idx INTEGER, model TEXT)"
            )
            con.execute(
                "INSERT INTO embeddings (node_id, chunk_idx, model) VALUES ('n1', 0, 'test')"
            )
            con.commit()
        finally:
            con.close()

    def test_scratch_drill_on_fixture(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            snap = d / "graph-fixture.sqlite"
            self._tiny_db(snap)
            live = d / "live.sqlite"
            live.write_bytes(b"x")

            with (
                mock.patch.object(graph_backup, "LIVE_GRAPH", live),
                mock.patch(
                    "khipu.ops_events.record", return_value={"ok": True, "id": 7}
                ),
            ):
                out = graph_backup.scratch_drill(snapshot=snap, dest_dir=d)

            self.assertTrue(out["ok"])
            self.assertEqual(out["nodes"], 1)
            self.assertEqual(out["embeddings"], 1)
            self.assertFalse(
                any(p.name.startswith("khipu-graph-drill-") for p in d.iterdir())
            )


if __name__ == "__main__":
    unittest.main()
