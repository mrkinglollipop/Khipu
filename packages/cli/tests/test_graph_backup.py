"""graph_backup — snapshot health, offsite argv, scratch drill (unittest + tempfile)."""

from __future__ import annotations

import os
import sqlite3
import subprocess
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


@mock.patch.dict(os.environ, {"KHIPU_GRAPH_BACKUP_BWLIMIT": ""})
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

    def test_a_recent_offsite_copy_reads_as_fresh(self):
        from datetime import datetime, timezone

        event = {"status": "ok", "created_at": datetime.now(timezone.utc), "age_seconds": 3600}
        with (
            mock.patch.dict(os.environ, {"KHIPU_GRAPH_BACKUP_REMOTE": "r2:my-bucket/graph"}),
            mock.patch("khipu.graph_backup.has_r2_remote", return_value=True),
            mock.patch("khipu.graph_backup._latest_ops_event", return_value=event),
        ):
            health = graph_backup.offsite_health(producer=True)
        self.assertTrue(health["ok"])
        self.assertEqual(health["latest"]["created_at"], event["created_at"].isoformat())

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
            ["rclone", "copyto", "--bwlimit", "2M", "--", str(snap), f"r2:khipu-backups/khipu-graph/{snap.name}"],
        )
        self.assertEqual(by_verb["lsf"][0], ["rclone", "lsf", "--", "r2:khipu-backups/khipu-graph/"])
        self.assertTrue(by_verb["deletefile"])
        for argv in by_verb["deletefile"]:
            self.assertEqual(argv[:3], ["rclone", "deletefile", "--"])


@mock.patch.dict(os.environ, {"KHIPU_GRAPH_BACKUP_BWLIMIT": ""})
class RunOffsiteTest(unittest.TestCase):
    @staticmethod
    def _snapshot(path):
        with sqlite3.connect(path) as con:
            con.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY)")
            con.execute("CREATE TABLE edges (src TEXT, dst TEXT, type TEXT)")

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
            self._snapshot(snap)
            live = d / "live-graph.sqlite"
            live.write_bytes(b"live")

            calls: list[list[str]] = []
            real_run = graph_backup.subprocess.run

            def fake_run(argv, **kwargs):
                if argv[0] != "rclone":
                    return real_run(argv, **kwargs)
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
            self.assertEqual(argv[2:5], ["--bwlimit", "2M", "--"])
            self.assertEqual(argv[5], str(snap))
            self.assertTrue(argv[6].startswith("r2:khipu-backups/khipu-graph/"))
            joined = " ".join(argv)
            self.assertNotIn(str(live.resolve()), joined)

    def test_offsite_fail_uses_fixed_ops_error_when_record_raises(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            snap = d / "graph-20260819T000000Z.sqlite"
            self._snapshot(snap)
            live = d / "live-graph.sqlite"
            live.write_bytes(b"live")

            real_run = graph_backup.subprocess.run

            def fake_run(argv, **kwargs):
                if argv[0] != "rclone":
                    return real_run(argv, **kwargs)
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


@mock.patch.dict(os.environ, {"KHIPU_GRAPH_BACKUP_BWLIMIT": ""})
class OffsiteTimeoutTest(unittest.TestCase):
    def _timeout_for(self, size_bytes: int) -> int:
        with tempfile.TemporaryDirectory() as td:
            snap = Path(td, "graph-20260819T000000Z.sqlite")
            with open(snap, "wb") as f:
                f.truncate(size_bytes)  # sparse: no real disk used
            return graph_backup._offsite_timeout(snap)

    def test_small_file_gets_the_floor(self):
        self.assertEqual(3600, self._timeout_for(10 * 1024**2))
        self.assertEqual(3600, self._timeout_for(1024**3))

    def test_timeout_grows_ten_minutes_per_gb_above_the_first(self):
        self.assertEqual(3600 + 600, self._timeout_for(2 * 1024**3))
        self.assertEqual(3600 + 1500, self._timeout_for(7 * 1024**3 // 2))

    def test_timeout_is_capped_at_four_hours(self):
        self.assertEqual(4 * 3600, self._timeout_for(100 * 1024**3))

    def test_run_offsite_passes_the_scaled_timeout_to_rclone(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        snap = d / "graph-20260819T000000Z.sqlite"
        with open(snap, "wb") as f:
            f.truncate(3 * 1024**3)
        live = d / "live-graph.sqlite"
        live.write_bytes(b"live")
        timeouts = {}

        def fake_run(argv, **kwargs):
            timeouts[argv[1]] = kwargs.get("timeout")
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
            graph_backup.run_offsite()
        self.assertEqual(3600 + 2 * 600, timeouts["copyto"])


class IntegrityTimeoutScalingTest(unittest.TestCase):
    def test_default_timeout_scales_and_is_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            snap = Path(td, "graph-fixture.sqlite")
            for size, expected in ((1, 60), (1024**3, 60),
                                   (3 * 1024**3 + 1, 181), (20 * 1024**3, 900)):
                with self.subTest(size=size):
                    with snap.open("wb") as stream:
                        stream.truncate(size)
                    with mock.patch.object(graph_backup.subprocess, "run",
                                           return_value=mock.Mock(returncode=0, stdout="ok")) as run:
                        self.assertEqual(graph_backup._integrity_check(snap), "ok")
                    self.assertEqual(run.call_args.kwargs["timeout"], expected)

    def test_explicit_timeout_remains_exact_and_timeout_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            snap = Path(td, "graph-fixture.sqlite")
            with snap.open("wb") as stream:
                stream.truncate(4 * 1024**3)
            with mock.patch.object(graph_backup.subprocess, "run",
                                   side_effect=subprocess.TimeoutExpired("fixture", 7)) as run:
                self.assertEqual(graph_backup._integrity_check(snap, timeout=7), "timeout after 7s")
            self.assertEqual(run.call_args.kwargs["timeout"], 7)

    def test_unreadable_size_uses_floor_and_preserves_failure(self):
        with tempfile.TemporaryDirectory() as td:
            snap = Path(td, "missing.sqlite")
            with mock.patch.object(graph_backup.subprocess, "run",
                                   side_effect=OSError("fixture launch failed")) as run:
                self.assertEqual(graph_backup._integrity_check(snap),
                                 "snapshot integrity check failed to start")
            self.assertEqual(run.call_args.kwargs["timeout"], 60)

    def test_invalid_size_uses_floor_and_preserves_failure(self):
        for size in (mock.Mock(), mock.MagicMock(), None, "1024", -1):
            with self.subTest(size=size):
                snap = mock.Mock()
                snap.stat.return_value.st_size = size
                with mock.patch.object(graph_backup.subprocess, "run",
                                       return_value=mock.Mock(returncode=1, stdout="", stderr="")) as run:
                    self.assertEqual(graph_backup._integrity_check(snap),
                                     "snapshot integrity check failed")
                self.assertEqual(run.call_args.kwargs["timeout"], 60)

    def test_scaled_timeout_is_reported_without_success(self):
        with tempfile.TemporaryDirectory() as td:
            snap = Path(td, "graph-fixture.sqlite")
            with snap.open("wb") as stream:
                stream.truncate(4 * 1024**3)
            with mock.patch.object(graph_backup.subprocess, "run",
                                   side_effect=subprocess.TimeoutExpired("fixture", 240)):
                self.assertEqual(graph_backup._integrity_check(snap), "timeout after 240s")


class OffsiteBandwidthTest(unittest.TestCase):
    def _run(self, bwlimit, *, size=3 * 1024**3, failure=None, integrity="ok"):
        with tempfile.TemporaryDirectory() as td:
            snap = Path(td, "graph-fixture.sqlite")
            with snap.open("wb") as stream:
                stream.truncate(size)
            env = {"KHIPU_GRAPH_BACKUP_BWLIMIT": bwlimit}
            with (
                mock.patch.dict(os.environ, env),
                mock.patch.object(graph_backup, "latest_snapshot", return_value=snap),
                mock.patch.object(graph_backup, "_integrity_check", return_value=integrity),
                mock.patch.object(graph_backup, "_required_graph_tables", return_value=None),
                mock.patch.object(graph_backup, "_live_graph", return_value=Path(td, "live.sqlite")),
                mock.patch.object(graph_backup, "resolve_rclone", return_value="rclone"),
                mock.patch.object(graph_backup, "has_r2_remote", return_value=True),
                mock.patch.object(graph_backup, "_prune_remote_snapshots", return_value=[]),
                mock.patch.object(graph_backup.subprocess, "run", side_effect=failure,
                                  return_value=mock.Mock(returncode=0, stdout="", stderr="")) as run,
                mock.patch("khipu.ops_events.record", return_value={"ok": True}) as record,
            ):
                out = graph_backup.run_offsite(dest_remote="r2:fixture/graph")
            return out, run, record

    def test_default_and_configured_caps_precede_operand_separator(self):
        for configured, expected in (("", "2M"), ("1M", "1M"), ("512K", "512K")):
            with self.subTest(cap=configured):
                out, run, _ = self._run(configured)
                self.assertTrue(out["ok"], out)
                argv = run.call_args.args[0]
                self.assertEqual(argv[2:5], ["--bwlimit", expected, "--"])
                self.assertGreaterEqual(run.call_args.kwargs["timeout"],
                                        7740 if configured == "512K" else 4800)

    def test_invalid_and_too_slow_caps_fail_before_transfer(self):
        for cap in ("0", "-1M", "unlimited", "1M:2M", "256K"):
            with self.subTest(cap=cap):
                out, run, record = self._run(cap)
                self.assertFalse(out["ok"], out)
                self.assertIn("bandwidth", out["reason"])
                run.assert_not_called()
                self.assertEqual(record.call_args.args[1], "fail")

    def test_copy_timeout_names_elapsed_budget_and_preserves_failure_receipt(self):
        out, _, record = self._run("2M", failure=subprocess.TimeoutExpired("fixture", 4800))
        self.assertFalse(out["ok"])
        self.assertIn("timeout", out["reason"])
        self.assertEqual(out["timeout_seconds"], 4800)
        self.assertEqual(record.call_args.args[1], "fail")

    def test_integrity_timeout_never_uploads_or_records_success(self):
        out, run, record = self._run("2M", integrity="timeout after 180s")
        self.assertFalse(out["ok"])
        self.assertIn("integrity", out["reason"])
        run.assert_not_called()
        self.assertEqual(record.call_args.args[1], "fail")


if __name__ == "__main__":
    unittest.main()
