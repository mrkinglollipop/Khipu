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
            snapshot = d / "graph-20260819T000000Z.sqlite"
            con = sqlite3.connect(snapshot)
            try:
                con.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY)")
                con.execute("CREATE TABLE edges (src TEXT, dst TEXT, type TEXT)")
                con.commit()
            finally:
                con.close()
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


class DefaultSnapshotDirTest(unittest.TestCase):
    """Doctor runs from shells and from the app with no KHIPU_* env; the
    installed graph plist is the one place the snapshot dir is always recorded
    (2026-08-24: shell/app doctor red on the ~/.config default while the real
    dir held a fresh snapshot)."""

    def test_env_wins_then_plist_then_config_default(self):
        import plistlib
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            la = home / "Library" / "LaunchAgents"
            la.mkdir(parents=True)
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False), \
                 mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("KHIPU_GRAPH_SNAPSHOT_DIR", None)
                from khipu.jobs import PLIST_GRAPH
                with open(la / f"{PLIST_GRAPH}.plist", "wb") as fh:
                    plistlib.dump({"Label": PLIST_GRAPH, "EnvironmentVariables":
                                   {"KHIPU_GRAPH_SNAPSHOT_DIR": "/backups/graph"}}, fh)
                self.assertEqual(graph_backup._default_snapshot_dir(), Path("/backups/graph"))
                os.environ["KHIPU_GRAPH_SNAPSHOT_DIR"] = str(home / "env-dir")
                try:
                    self.assertEqual(graph_backup._default_snapshot_dir(), home / "env-dir")
                finally:
                    os.environ.pop("KHIPU_GRAPH_SNAPSHOT_DIR", None)
                (la / f"{PLIST_GRAPH}.plist").unlink()
                out = graph_backup._default_snapshot_dir()
                self.assertTrue(str(out).endswith("backups/graph"), out)


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


class ResolveRcloneTest(unittest.TestCase):
    def test_bare_name_resolves_or_falls_back_to_homebrew(self):
        """launchd has no /opt/homebrew/bin on PATH; a bare "rclone" must
        still resolve there (08-27..08-31: five nightly offsite attempts
        failed as 'remote not configured' because the binary wasn't found)."""
        with mock.patch("shutil.which", return_value=None), \
             mock.patch.object(graph_backup.Path, "is_file",
                               lambda self: str(self) == "/opt/homebrew/bin/rclone"):
            self.assertEqual(graph_backup.resolve_rclone("rclone"), "/opt/homebrew/bin/rclone")

    def test_nothing_found_returns_none_and_offsite_names_the_binary(self):
        with mock.patch.object(graph_backup, "resolve_rclone", return_value=None):
            out = graph_backup.run_offsite()
        self.assertFalse(out["ok"])
        self.assertIn("rclone binary not found", out["reason"])


class DestRemoteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(
            os.environ,
            {"KHIPU_DATA_DIR": self.tmp.name, "KHIPU_GRAPH_BACKUP_REMOTE": ""},
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_default_is_neutral(self):
        self.assertEqual(graph_backup.resolve_dest_remote(), "r2:khipu-backups/khipu-graph")
        out = graph_backup.offsite_health(producer=False)
        self.assertEqual(out["dest_remote"], "r2:khipu-backups/khipu-graph")

    def test_config_override_and_unset(self):
        from khipu.config import set_graph_backup_remote

        set_graph_backup_remote("r2:my-bucket/graph/")
        self.assertEqual(graph_backup.resolve_dest_remote(), "r2:my-bucket/graph")
        self.assertEqual(
            graph_backup.offsite_health(producer=False)["dest_remote"], "r2:my-bucket/graph"
        )
        set_graph_backup_remote(None)
        self.assertEqual(graph_backup.resolve_dest_remote(), "r2:khipu-backups/khipu-graph")

    def test_env_override_wins(self):
        from khipu.config import set_graph_backup_remote

        set_graph_backup_remote("r2:from-config/graph")
        with mock.patch.dict(os.environ, {"KHIPU_GRAPH_BACKUP_REMOTE": "r2:from-env/g"}):
            self.assertEqual(graph_backup.resolve_dest_remote(), "r2:from-env/g")

    def test_status_names_the_source(self):
        from khipu.config import graph_backup_remote_status, set_graph_backup_remote

        self.assertEqual(graph_backup_remote_status()["source"], "default")
        set_graph_backup_remote("r2:my-bucket/graph")
        self.assertEqual(
            graph_backup_remote_status(),
            {"value": "r2:my-bucket/graph", "source": "config",
             "default": "r2:khipu-backups/khipu-graph"},
        )
        with mock.patch.dict(os.environ, {"KHIPU_GRAPH_BACKUP_REMOTE": "r2:from-env/g"}):
            self.assertEqual(graph_backup_remote_status()["source"], "env")

    def test_override_without_remote_name_is_refused(self):
        from khipu.config import set_graph_backup_remote

        with self.assertRaises(ValueError):
            set_graph_backup_remote("just-a-bucket")


class DestRemoteSafetyTest(unittest.TestCase):
    """A destination must be a named rclone remote: never a local path, never
    flag-shaped, from whichever source it resolved."""

    BAD = ("/tmp/backup:graph", "--log-file=/tmp/x:y", "./local:dir", "r2 x:y", "-r2:x", "bucket")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(
            os.environ,
            {"KHIPU_DATA_DIR": self.tmp.name, "KHIPU_GRAPH_BACKUP_REMOTE": ""},
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_setter_refuses_local_and_flag_shaped_values(self):
        from khipu.config import set_graph_backup_remote

        for bad in self.BAD:
            with self.subTest(bad=bad), self.assertRaises(ValueError) as ctx:
                set_graph_backup_remote(bad)
            self.assertIn("named rclone remote", str(ctx.exception))

    def test_environment_value_is_validated_after_resolving(self):
        for bad in self.BAD:
            with self.subTest(bad=bad), mock.patch.dict(
                os.environ, {"KHIPU_GRAPH_BACKUP_REMOTE": bad}
            ):
                with self.assertRaises(ValueError):
                    graph_backup.resolve_dest_remote()
                health = graph_backup.offsite_health(producer=True)
                self.assertFalse(health["ok"])
                self.assertIn("named rclone remote", health["reason"])

    def test_hand_edited_config_value_is_validated(self):
        import json as _json

        Path(self.tmp.name, "config.json").write_text(
            _json.dumps({"graph_backup_remote": "/tmp/backup:graph"}), encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            graph_backup.resolve_dest_remote()

    def test_invalid_destination_never_reaches_rclone(self):
        with (
            mock.patch.dict(os.environ, {"KHIPU_GRAPH_BACKUP_REMOTE": "--log-file=/tmp/x:y"}),
            mock.patch("khipu.graph_backup.subprocess.run") as run,
        ):
            out = graph_backup.run_offsite()
        self.assertFalse(out["ok"])
        self.assertIn("named rclone remote", out["reason"])
        run.assert_not_called()

    def test_rclone_operands_follow_a_double_dash(self):
        d = Path(self.tmp.name)
        snap = d / "graph-20260819T000000Z.sqlite"
        snap.write_bytes(b"sqlite")
        live = d / "live-graph.sqlite"
        live.write_bytes(b"live")
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if argv[1] == "lsf":
                names = "\n".join(f"graph-2026081{i}T000000Z.sqlite" for i in range(5))
                return mock.Mock(returncode=0, stdout=names, stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with (
            mock.patch.object(graph_backup, "latest_snapshot", return_value=snap),
            mock.patch.object(graph_backup, "_integrity_check", return_value="ok"),
            mock.patch.object(graph_backup, "_required_graph_tables", return_value=None),
            mock.patch.object(graph_backup, "_live_graph", return_value=live),
            mock.patch.object(graph_backup, "resolve_rclone", return_value="rclone"),
            mock.patch.object(graph_backup, "has_r2_remote", return_value=True),
            mock.patch("khipu.graph_backup.subprocess.run", side_effect=fake_run),
            mock.patch("khipu.ops_events.record", return_value={"ok": True, "id": 1}),
        ):
            out = graph_backup.run_offsite()
        self.assertTrue(out["ok"], out)
        by_verb = {}
        for argv in calls:
            by_verb.setdefault(argv[1], []).append(argv)
        self.assertEqual(
            by_verb["copyto"][0],
            ["rclone", "copyto", "--", str(snap), f"r2:khipu-backups/khipu-graph/{snap.name}"],
        )
        self.assertEqual(by_verb["lsf"][0], ["rclone", "lsf", "--", "r2:khipu-backups/khipu-graph/"])
        self.assertTrue(by_verb["deletefile"])
        for argv in by_verb["deletefile"]:
            self.assertEqual(argv[:3], ["rclone", "deletefile", "--"])


class RunOffsiteTest(unittest.TestCase):
    def test_missing_r2_remote_fails_honest(self):
        with mock.patch.object(graph_backup, "resolve_rclone", return_value="rclone"), \
             mock.patch.object(graph_backup, "has_r2_remote", return_value=False):
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
                mock.patch.object(graph_backup, "_default_snapshot_dir", return_value=d),
                mock.patch.object(graph_backup, "_live_graph", return_value=live),
                mock.patch.object(graph_backup, "resolve_rclone", return_value="rclone"),
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
            self.assertEqual(argv[2], "--")
            self.assertEqual(argv[3], str(snap))
            self.assertTrue(argv[4].startswith("r2:khipu-backups/khipu-graph/"))
            joined = " ".join(argv)
            self.assertNotIn(str(live.resolve()), joined)

    def test_offsite_fail_uses_fixed_ops_error_when_record_raises(self):
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
                mock.patch.object(graph_backup, "_default_snapshot_dir", return_value=d),
                mock.patch.object(graph_backup, "_live_graph", return_value=live),
                mock.patch.object(graph_backup, "resolve_rclone", return_value="rclone"),
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
            self.assertEqual("offsite ops_events insert failed", out["ops_error"])
            self.assertNotIn("dsn down", str(out))


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
                mock.patch.object(graph_backup, "_live_graph", return_value=live),
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
