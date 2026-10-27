"""Unit tests for khipu.jobs — subprocess wrappers and doctor metadata."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from khipu import jobs


def _driver_process(run_fn):
    """Adapt existing driver assertions to the deadline-aware Popen contract."""
    def launch(cmd, *, stdout, stderr, env, start_new_session):
        assert start_new_session is True
        result = run_fn(cmd, stdout=stdout, stderr=stderr, env=env)
        process = mock.Mock()
        def wait(*, timeout):
            assert timeout == jobs.DRIVER_TIMEOUT_SECONDS
            return result.returncode
        process.wait.side_effect = wait
        return process
    return launch


class JobsRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.script = self.root / "job.py"
        self.script.write_text("import sys; sys.exit(3)\n", encoding="utf-8")
        self.state_dir = self.root / "state"
        self.data_dir = self.root / "data"
        self.log_dir = self.root / "logs"

        # The rename sweep reaches for the hub and gh: never in these tests.
        patcher = mock.patch.object(jobs, "_project_renames_sweep", return_value={"ok": True})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    @contextmanager
    def _patch_run(self, script_attr: str, run_fn, log_stem: str):
        with mock.patch.object(jobs, script_attr, self.script), \
             mock.patch.object(jobs, "ensure_data_dir", return_value=self.data_dir), \
             mock.patch.object(jobs, "_log_paths", return_value=(
                 self.log_dir / f"{log_stem}.out.log",
                 self.log_dir / f"{log_stem}.err.log",
             )), \
             mock.patch.object(jobs.subprocess, "Popen", side_effect=_driver_process(run_fn)) as run_mock, \
             mock.patch("khipu.session_capture.drain", return_value={"failed": 0}), \
             mock.patch("khipu.embed.backfill", return_value={"embedded": 0}), \
             mock.patch.object(jobs, "_commitment_close_rules", return_value={"ok": True}), \
             mock.patch("khipu.embed.prune_query_cache", return_value=0):
            # The nightly's own embed sweep and cache prune reach the hub;
            # they are unit-tested below and never run for real here.
            yield run_mock

    def test_run_nightly_invokes_consolidate_script(self):
        def _run(cmd, stdout, stderr, env):  # noqa: ARG001
            self.assertEqual(cmd[1], str(self.script))
            stdout.write(b"ok\n")
            return mock.Mock(returncode=0)

        # W4.3: run_nightly also piggybacks khipu.notes.reconcile (real
        # filesystem walk + a real hub write) — never let that run for real
        # in a unit test; the real reconcile has its own tests (test_notes.py).
        with self._patch_run("CONSOLIDATE_NIGHTLY", _run, "khipu-nightly") as run_mock, \
                mock.patch("khipu.notes.reconcile", return_value={"ok": True}) as m_reconcile, \
                mock.patch.object(jobs, "_mark_stale_commitments") as m_stale, \
                mock.patch.object(jobs, "_hygiene_commitments") as m_hygiene:
            rc = jobs.run_nightly()
        self.assertEqual(rc, 0)
        run_mock.assert_called_once()
        m_reconcile.assert_called_once_with(dry_run=False)
        m_stale.assert_called_once_with()
        m_hygiene.assert_called_once_with()
        state = json.loads((self.data_dir / "state" / "job-nightly.json").read_text())
        self.assertEqual(state["exit"], 0)

    def test_failed_drain_sets_return_code_without_blocking_other_steps(self):
        for consolidate_rc, expected in ((0, 1), (3, 3)):
            with self.subTest(consolidate_rc=consolidate_rc), \
                    mock.patch.object(jobs, "_drain_sessions", return_value={"ok": False, "failed": 1}), \
                    mock.patch.object(jobs, "_run_script", return_value=consolidate_rc) as script, \
                    mock.patch.object(jobs, "_record_nightly_step") as record, \
                    mock.patch.object(jobs, "_reconcile_notes_if_due"), \
                    mock.patch.object(jobs, "_embed_backfill"), \
                    mock.patch.object(jobs, "_prune_query_cache"), \
                    mock.patch.object(jobs, "_commitment_close_rules"), \
                    mock.patch.object(jobs, "_mark_stale_commitments"), \
                    mock.patch.object(jobs, "_hygiene_commitments"), \
                    mock.patch.object(jobs, "_ledger_nightly"), \
                    mock.patch.object(jobs, "_refresh_snapshot"), \
                    mock.patch.object(jobs, "_briefs_build_if_on", return_value=None):
                self.assertEqual(jobs.run_nightly(), expected)
            script.assert_called_once()
            self.assertFalse(record.call_args_list[0].kwargs["ok"])
            self.assertEqual(record.call_count, 11)

    def test_run_nightly_writes_nightly_last_json_with_every_step(self):
        """F3/D1: nightly-last.json is the persisted evidence Phase 6 will
        read — one entry per step, not just free text in the log."""
        def _run(cmd, stdout, stderr, env):  # noqa: ARG001
            stdout.write(b"ok\n")
            return mock.Mock(returncode=0)

        with self._patch_run("CONSOLIDATE_NIGHTLY", _run, "khipu-nightly"), \
                mock.patch("khipu.notes.reconcile", return_value={"ok": True, "written": 2}), \
                mock.patch.object(jobs, "_mark_stale_commitments"), \
                mock.patch.object(jobs, "_hygiene_commitments"):
            jobs.run_nightly()
        payload = json.loads((self.data_dir / "nightly-last.json").read_text())
        names = [s["name"] for s in payload["steps"]]
        self.assertEqual(
            names,
            ["sessions_drain", "consolidate_nightly", "notes_reconcile", "embed_backfill",
             "query_cache_prune", "commitments_close_rules", "commitments_mark_stale",
             "commitments_hygiene", "ledger", "project_renames",
             "snapshot_refresh"],
        )
        for step in payload["steps"]:
            self.assertIn("ok", step)
            self.assertIn("counts", step)
            self.assertIn("error", step)
            self.assertIn("ts", step)
        notes_step = next(s for s in payload["steps"] if s["name"] == "notes_reconcile")
        self.assertTrue(notes_step["ok"])
        self.assertEqual(notes_step["counts"]["written"], 2)

    def test_run_nightly_refreshes_the_replica_last_and_a_miss_cannot_fail_the_night(self):
        """Phase 0 of docs/plans/2026-10-11-replica-freshness.md: the nightly
        is the only scheduled full replica dump. It runs after the steps whose
        output the replica carries, its outcome is on record, and a failed
        dump (hub down) leaves the exit code alone."""
        def _run(cmd, stdout, stderr, env):  # noqa: ARG001
            stdout.write(b"ok\n")
            return mock.Mock(returncode=0)

        with self._patch_run("CONSOLIDATE_NIGHTLY", _run, "khipu-nightly"), \
                mock.patch("khipu.notes.reconcile", return_value={"ok": True}), \
                mock.patch.object(jobs, "_mark_stale_commitments"), \
                mock.patch.object(jobs, "_hygiene_commitments"), \
                mock.patch("khipu.hub_snapshot.refresh",
                            return_value={"ok": True, "path": "/x", "counts": {"episodes": 3}}) as dump:
            self.assertEqual(jobs.run_nightly(), 0)
        dump.assert_called_once_with()
        payload = json.loads((self.data_dir / "nightly-last.json").read_text())
        self.assertEqual(payload["steps"][-1]["name"], "snapshot_refresh")
        self.assertTrue(payload["steps"][-1]["ok"])
        self.assertEqual(payload["steps"][-1]["counts"]["counts"], {"episodes": 3})

        with self._patch_run("CONSOLIDATE_NIGHTLY", _run, "khipu-nightly"), \
                mock.patch("khipu.notes.reconcile", return_value={"ok": True}), \
                mock.patch.object(jobs, "_mark_stale_commitments"), \
                mock.patch.object(jobs, "_hygiene_commitments"), \
                mock.patch("khipu.hub_snapshot.refresh", side_effect=OSError("hub unreachable")):
            self.assertEqual(jobs.run_nightly(), 0)
        payload = json.loads((self.data_dir / "nightly-last.json").read_text())
        step = payload["steps"][-1]
        self.assertEqual((step["name"], step["ok"]), ("snapshot_refresh", False))
        self.assertIn("hub unreachable", step["error"])

    def test_nightly_last_json_reports_a_missing_embed_key_once(self):
        """F3: a missing/expired embedding key must not abort the whole
        backfill — it shows up once as embed_provider in the persisted
        step, not as a raise that takes the rest of the nightly with it."""
        def _run(cmd, stdout, stderr, env):  # noqa: ARG001
            stdout.write(b"ok\n")
            return mock.Mock(returncode=0)

        with self._patch_run("CONSOLIDATE_NIGHTLY", _run, "khipu-nightly"), \
                mock.patch("khipu.notes.reconcile", return_value={"ok": True}), \
                mock.patch.object(jobs, "_mark_stale_commitments"), \
                mock.patch.object(jobs, "_hygiene_commitments"), \
                mock.patch("khipu.embed.backfill",
                            return_value={"embedded": 3, "embed_provider": "missing key"}):
            jobs.run_nightly()
        payload = json.loads((self.data_dir / "nightly-last.json").read_text())
        backfill_step = next(s for s in payload["steps"] if s["name"] == "embed_backfill")
        self.assertEqual(backfill_step["counts"]["embed_provider"], "missing key")

    def test_run_nightly_ages_open_commitments_into_stale(self):
        """W3: commitments.mark_stale had NO caller at all (audit 2026-09-04),
        so nothing ever aged and `khipu owed --status stale` stayed empty."""
        def _run(cmd, stdout, stderr, env):  # noqa: ARG001
            stdout.write(b"ok\n")
            return mock.Mock(returncode=0)

        cur = mock.Mock()
        cur.__enter__ = mock.Mock(return_value=cur)
        cur.__exit__ = mock.Mock(return_value=False)
        cur.rowcount = 4
        conn = mock.Mock()
        conn.cursor.return_value = cur
        conn.__enter__ = mock.Mock(return_value=conn)
        conn.__exit__ = mock.Mock(return_value=False)
        with self._patch_run("CONSOLIDATE_NIGHTLY", _run, "khipu-nightly"), \
                mock.patch("khipu.notes.reconcile", return_value={"ok": True}), \
                mock.patch("khipu.db.connect", return_value=conn), \
                mock.patch("khipu.commitments.mark_stale", return_value=4) as m_stale, \
                mock.patch.object(jobs, "_commitment_close_rules"), \
                mock.patch.object(jobs, "_hygiene_commitments"):
            rc = jobs.run_nightly()
        self.assertEqual(rc, 0)
        m_stale.assert_called_once_with(cur)
        conn.commit.assert_called_once()

    def test_run_nightly_survives_a_commitment_staling_failure(self):
        def _run(cmd, stdout, stderr, env):  # noqa: ARG001
            stdout.write(b"ok\n")
            return mock.Mock(returncode=0)

        with self._patch_run("CONSOLIDATE_NIGHTLY", _run, "khipu-nightly"), \
                mock.patch("khipu.notes.reconcile", return_value={"ok": True}), \
                mock.patch("khipu.db.connect", side_effect=RuntimeError("hub down")):
            self.assertEqual(jobs.run_nightly(), 0)

    def test_run_nightly_survives_a_notes_reconcile_failure(self):
        """The external nightly script's exit code is the one that gates
        doctor/status — a notes.reconcile exception must not change it."""
        def _run(cmd, stdout, stderr, env):  # noqa: ARG001
            stdout.write(b"ok\n")
            return mock.Mock(returncode=0)

        with self._patch_run("CONSOLIDATE_NIGHTLY", _run, "khipu-nightly"), \
                mock.patch("khipu.notes.reconcile", side_effect=RuntimeError("boom")), \
                mock.patch.object(jobs, "_mark_stale_commitments"), \
                mock.patch.object(jobs, "_hygiene_commitments"):
            rc = jobs.run_nightly()
        self.assertEqual(rc, 0)

    def test_run_nightly_runs_the_embed_backfill_itself(self):
        """Vectors no longer depend on the legacy driver's reconcile: the
        backfill runs from run_nightly regardless of the script's outcome,
        and a failure in it is logged, never fatal (2026-09-05)."""
        def _run(cmd, stdout, stderr, env):  # noqa: ARG001
            return mock.Mock(returncode=2)

        with self._patch_run("CONSOLIDATE_NIGHTLY", _run, "khipu-nightly"), \
                mock.patch("khipu.notes.reconcile", return_value={"ok": True}), \
                mock.patch.object(jobs, "_mark_stale_commitments"), \
                mock.patch.object(jobs, "_hygiene_commitments"), \
                mock.patch("khipu.embed.backfill", return_value={"embedded": 7}) as m_bf, \
                mock.patch("khipu.embed.prune_query_cache", side_effect=RuntimeError("hub down")):
            rc = jobs.run_nightly()
        self.assertEqual(rc, 2)
        m_bf.assert_called_once_with()
        log = (self.log_dir / "khipu-nightly.out.log").read_text()
        self.assertIn("[khipu-embed] backfill ok", log)
        self.assertIn("query cache prune skipped: RuntimeError", log)

        with self._patch_run("CONSOLIDATE_NIGHTLY", _run, "khipu-nightly"), \
                mock.patch("khipu.notes.reconcile", return_value={"ok": True}), \
                mock.patch.object(jobs, "_mark_stale_commitments"), \
                mock.patch.object(jobs, "_hygiene_commitments"), \
                mock.patch("khipu.embed.backfill", side_effect=RuntimeError("quota")):
            self.assertEqual(jobs.run_nightly(), 2)
        self.assertIn("backfill skipped: RuntimeError: quota",
                      (self.log_dir / "khipu-nightly.out.log").read_text())

    def test_run_nightly_runs_the_commitments_hygiene_pass(self):
        """W3 Owed quality (2026-09-05): the two hygiene passes run every
        night, not only on demand via `khipu hygiene commitments`."""
        def _run(cmd, stdout, stderr, env):  # noqa: ARG001
            stdout.write(b"ok\n")
            return mock.Mock(returncode=0)

        conn = mock.MagicMock()
        conn.__enter__.return_value = conn
        conn.__exit__.return_value = False
        cur = mock.MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn.cursor.return_value = cur

        with self._patch_run("CONSOLIDATE_NIGHTLY", _run, "khipu-nightly"), \
                mock.patch("khipu.notes.reconcile", return_value={"ok": True}), \
                mock.patch.object(jobs, "_mark_stale_commitments"), \
                mock.patch.object(jobs, "_commitment_close_rules"), \
                mock.patch("khipu.db.connect", return_value=conn), \
                mock.patch("khipu.hygiene.backup_commitments", return_value=Path("/tmp/backup")) as m_backup, \
                mock.patch(
                    "khipu.hygiene.run_session_ended_pass",
                    return_value={"counts": {"keep": 1}},
                ) as m_session, \
                mock.patch(
                    "khipu.hygiene.run_commitments_hygiene",
                    return_value={"counts": {"drop": 2}},
                ) as m_rejudge:
            rc = jobs.run_nightly()
        self.assertEqual(rc, 0)
        m_backup.assert_called_once_with(conn)
        m_session.assert_called_once_with(cur, apply=True)
        m_rejudge.assert_called_once_with(cur, apply=True)
        conn.commit.assert_called_once()
        log = (self.log_dir / "khipu-nightly.out.log").read_text()
        self.assertIn("[khipu-hygiene] session-ended", log)
        self.assertIn("rejudge", log)

    def test_run_nightly_survives_a_commitments_hygiene_failure(self):
        """A failure in the hygiene pass must never flip a good nightly to
        failing — the external driver's exit code is the one that gates
        job_status/doctor."""
        def _run(cmd, stdout, stderr, env):  # noqa: ARG001
            stdout.write(b"ok\n")
            return mock.Mock(returncode=0)

        with self._patch_run("CONSOLIDATE_NIGHTLY", _run, "khipu-nightly"), \
                mock.patch("khipu.notes.reconcile", return_value={"ok": True}), \
                mock.patch.object(jobs, "_mark_stale_commitments"), \
                mock.patch("khipu.db.connect", side_effect=RuntimeError("hub down")):
            rc = jobs.run_nightly()
        self.assertEqual(rc, 0)
        log = (self.log_dir / "khipu-nightly.out.log").read_text()
        self.assertIn("[khipu-hygiene] skipped: RuntimeError: hub down", log)

    def test_run_monthly_passes_dry_run(self):
        captured: list[list[str]] = []

        def _run(cmd, stdout, stderr, env):  # noqa: ARG001
            captured.append(cmd)
            return mock.Mock(returncode=0)

        cursor_script = self.root / "consolidate_monthly.py"
        cursor_script.write_text("import sys; sys.exit(0)\n", encoding="utf-8")
        with mock.patch.object(jobs, "CONSOLIDATE_MONTHLY", cursor_script), \
             mock.patch.object(jobs, "ensure_data_dir", return_value=self.data_dir), \
             mock.patch.object(jobs, "_log_paths", return_value=(
                 self.log_dir / "khipu-monthly.out.log",
                 self.log_dir / "khipu-monthly.err.log",
             )), \
             mock.patch.object(jobs.subprocess, "Popen", side_effect=_driver_process(_run)):
            jobs.run_monthly(dry_run=True)
        self.assertIn("--dry-run", captured[0])

    def test_run_monthly_refuses_dry_run_on_live_driver(self):
        with mock.patch.object(
            jobs,
            "CONSOLIDATE_MONTHLY",
            Path("/tmp/conversation-memory-monthly.py"),
        ):
            rc = jobs.run_monthly(dry_run=True)
        self.assertEqual(rc, 2)

    def test_default_monthly_script_is_none_without_env(self):
        self.assertIsNone(jobs.CONSOLIDATE_MONTHLY)

    def test_run_graph_build_invokes_graphify_script(self):
        captured: list[list[str]] = []

        def _run(cmd, stdout, stderr, env):  # noqa: ARG001
            captured.append(cmd)
            return mock.Mock(returncode=0)

        with self._patch_run("GRAPHIFY_NIGHTLY", _run, "khipu-graph"):
            jobs.run_graph_build()
        self.assertEqual(captured[0][1], str(self.script))


class OffsiteIfDueTest(unittest.TestCase):
    """The weekly offsite copy rides the graph job; nothing else schedules it."""

    def _graph_build(self, *, rc: int):
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "graphify.py"
            script.write_text("pass\n", encoding="utf-8")
            with mock.patch.object(jobs, "GRAPHIFY_NIGHTLY", script), \
                 mock.patch.object(jobs, "_run_script", return_value=rc), \
                 mock.patch.object(jobs, "_offsite_if_due") as off:
                out = jobs.run_graph_build()
        return out, off

    def test_successful_build_runs_offsite(self):
        rc, off = self._graph_build(rc=0)
        self.assertEqual(rc, 0)
        off.assert_called_once_with()

    def test_failed_build_still_schedules_offsite(self):
        rc, off = self._graph_build(rc=1)
        self.assertEqual(rc, 1)
        off.assert_called_once_with()

    def test_offsite_runs_only_when_due_and_never_raises(self):
        from khipu import graph_backup
        with tempfile.TemporaryDirectory() as td:
            logs = (Path(td) / "g.out.log", Path(td) / "g.err.log")
            with mock.patch.object(jobs, "_log_paths", return_value=logs):
                with mock.patch.object(graph_backup, "_last_ok_time", return_value=None), \
                     mock.patch.object(graph_backup, "run_offsite",
                                       return_value={"ok": True, "dest": "r2:x"}) as run:
                    jobs._offsite_if_due()   # no prior ok -> due
                    run.assert_called_once()
                self.assertIn("offsite:", logs[0].read_text())
                from datetime import datetime, timezone
                with mock.patch.object(graph_backup, "_last_ok_time",
                                       return_value=datetime.now(timezone.utc)), \
                     mock.patch.object(graph_backup, "run_offsite") as run:
                    jobs._offsite_if_due()   # fresh ok -> not due
                    run.assert_not_called()
                with mock.patch.object(graph_backup, "_last_ok_time",
                                       side_effect=RuntimeError("db down")):
                    jobs._offsite_if_due()   # errors are logged, never raised
                self.assertIn("db down", logs[0].read_text())


class PlistLoadedTest(unittest.TestCase):
    def test_launchctl_nonzero_is_not_loaded_even_if_plist_exists(self):
        with tempfile.TemporaryDirectory() as td:
            agents = Path(td)
            label = "com.matt.khipu-nightly"
            (agents / f"{label}.plist").write_text("<plist/>\n", encoding="utf-8")
            with mock.patch.object(jobs, "_launchagents_dir", return_value=agents), \
                 mock.patch.object(jobs.os, "getuid", return_value=501), \
                 mock.patch.object(
                     jobs.subprocess, "run", return_value=mock.Mock(returncode=1),
                 ) as run_mock:
                self.assertFalse(jobs._plist_loaded(label))
            run_mock.assert_called_once()
            self.assertEqual(
                run_mock.call_args[0][0],
                ["launchctl", "print", f"gui/501/{label}"],
            )

    def test_launchctl_zero_is_loaded(self):
        with mock.patch.object(jobs.os, "getuid", return_value=501), \
             mock.patch.object(
                 jobs.subprocess, "run", return_value=mock.Mock(returncode=0),
             ):
            self.assertTrue(jobs._plist_loaded("com.matt.khipu-nightly"))


class JobsMetadataTest(unittest.TestCase):
    def test_nightly_log_prefers_khipu_when_newer(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            khipu = td_path / "khipu-nightly.out.log"
            legacy = td_path / "conversation-memory-nightly.out.log"
            khipu.write_text("new\n")
            legacy.write_text("old\n")
            os.utime(legacy, (1, 1))
            os.utime(khipu, (100, 100))
            with mock.patch.object(jobs, "LOG_DIR_LEGACY", td_path):
                chosen = jobs.nightly_log_path()
            self.assertEqual(chosen.name, "khipu-nightly.out.log")

    def _entry_with(self, *, log_age_s: float | None, receipt_ts: str | None):
        """Build the nightly entry with a stale launchd stdout log (the app's
        CLI has no KHIPU_LEGACY_LOG_DIR, so it reads this one) and an optional
        run receipt from ``_run_script``."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            log_dir = td_path / "logs"
            log_dir.mkdir()
            data_dir = td_path / "data"
            if log_age_s is not None:
                out = log_dir / "khipu-nightly.out.log"
                out.write_text("old\n")
                t = __import__("time").time() - log_age_s
                os.utime(out, (t, t))
            with mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(data_dir)}), \
                 mock.patch.object(jobs, "LOG_DIR", log_dir), \
                 mock.patch.object(jobs, "LOG_DIR_LEGACY", None), \
                 mock.patch.object(jobs, "_plist_loaded", return_value=True):
                if receipt_ts is not None:
                    state = data_dir / "state"
                    state.mkdir(parents=True)
                    (state / "job-nightly.json").write_text(
                        json.dumps({"exit": 0, "ts": receipt_ts}) + "\n"
                    )
                return jobs._job_entry("nightly")

    def test_last_run_prefers_a_newer_run_receipt_over_a_stale_log(self):
        # Live 2026-10-15: the nightly ran at 06:09Z (receipt, exit 0) but its
        # output went to the legacy log dir, so the app read a 6-day-old log.
        from datetime import datetime, timedelta, timezone

        recent = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        entry = self._entry_with(log_age_s=6 * 86400, receipt_ts=recent)
        self.assertEqual(entry["last_run_iso"], recent)
        self.assertEqual(entry["last_run_source"], "receipt")

    def test_last_run_falls_back_to_the_log_without_a_receipt(self):
        entry = self._entry_with(log_age_s=3600, receipt_ts=None)
        self.assertIsNotNone(entry["last_run_iso"])
        self.assertEqual(entry["last_run_source"], "log")

    def test_last_run_is_unknown_without_any_evidence(self):
        entry = self._entry_with(log_age_s=None, receipt_ts=None)
        self.assertIsNone(entry["last_run_iso"])
        self.assertIsNone(entry["last_run_source"])

    def test_log_paths_come_from_the_installed_plist(self):
        # Codex 2026-10-15: notes_watch writes no receipt, and its plist sent
        # output to a legacy dir the desktop CLI's environment never names.
        import plistlib

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            agents = td_path / "LaunchAgents"
            agents.mkdir()
            legacy = td_path / "legacy"
            legacy.mkdir()
            out = legacy / "khipu-notes-watch.out.log"
            err = legacy / "khipu-notes-watch.err.log"
            out.write_text("ran\n")
            with open(agents / f"{jobs.PLIST_NOTES_WATCH}.plist", "wb") as f:
                plistlib.dump({"Label": jobs.PLIST_NOTES_WATCH,
                               "StandardOutPath": str(out),
                               "StandardErrorPath": str(err)}, f)
            with mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(td_path / "data")}), \
                 mock.patch.object(jobs, "_launchagents_dir", return_value=agents), \
                 mock.patch.object(jobs, "LOG_DIR", td_path / "logs"), \
                 mock.patch.object(jobs, "LOG_DIR_LEGACY", None), \
                 mock.patch.object(jobs, "_plist_loaded", return_value=True):
                entry = jobs._job_entry("notes_watch")
        self.assertEqual(entry["log_path"], str(out))
        self.assertEqual(entry["err_log_path"], str(err))
        self.assertEqual(entry["last_run_source"], "log")
        self.assertIsNotNone(entry["last_run_iso"])

    def test_job_status_shape(self):
        with mock.patch.object(jobs, "_job_entry", side_effect=lambda name: {"name": name}):
            out = jobs.job_status()
        self.assertEqual(
            set(out),
            {"nightly", "monthly", "graph_build", "notes_watch", "queue_drain",
             "recall_daemon", "replica_refresh", "embed_media_backfill"},
        )


class IndexFreshnessTest(unittest.TestCase):
    def test_not_sync_host_is_green(self):
        with mock.patch("khipu.git_sync_health.is_sync_host", return_value=False):
            out = jobs.index_freshness(memory_root=Path("/tmp/mem"))
        self.assertTrue(out["ok"])
        self.assertFalse(out["applicable"])

    def test_stale_when_nightly_log_newer_than_index(self):
        with tempfile.TemporaryDirectory() as td:
            mem = Path(td)
            memory_md = mem / "MEMORY.md"
            memory_md.write_text("# Memory\n", encoding="utf-8")
            os.utime(memory_md, (100, 100))
            with mock.patch("khipu.git_sync_health.is_sync_host", return_value=True), \
                 mock.patch.object(jobs, "nightly_log_path") as nlog, \
                 mock.patch.object(jobs, "INDEX_SLACK_S", 60):
                nlog.return_value = mem / "nightly.log"
                (mem / "nightly.log").write_text("ran\n")
                os.utime(mem / "nightly.log", (500, 500))
                out = jobs.index_freshness(memory_root=mem)
        self.assertFalse(out["ok"])
        self.assertTrue(out["reasons"])

    def _night(self, mem: Path, *, consolidate_ago: float, steps_after: bool):
        """Index written when consolidate finished; later steps log afterwards."""
        from datetime import datetime, timezone

        now = time.time()
        done = now - consolidate_ago
        index = mem / "MEMORY.md"
        index.write_text("# Memory\n", encoding="utf-8")
        os.utime(index, (done, done))
        log = mem / "nightly.log"
        log.write_text("consolidate done\n[khipu-embed] backfill ok\n")
        os.utime(log, (now, now))
        iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()  # noqa: E731
        steps = [{"name": "sessions_drain", "ok": True, "ts": iso(done - 600)},
                 {"name": "consolidate_nightly", "ok": True, "ts": iso(done + 5)}]
        if steps_after:
            steps.append({"name": "embed_backfill", "ok": True, "ts": iso(now)})
        return log, {"steps": steps}

    def test_later_nightly_steps_do_not_make_the_index_look_stale(self):
        with tempfile.TemporaryDirectory() as td:
            mem = Path(td)
            log, last = self._night(mem, consolidate_ago=4000, steps_after=True)
            with mock.patch("khipu.git_sync_health.is_sync_host", return_value=True), \
                 mock.patch.object(jobs, "nightly_log_path", return_value=log), \
                 mock.patch.object(jobs, "nightly_last", return_value=last):
                out = jobs.index_freshness(memory_root=mem)
        self.assertTrue(out["ok"], out["reasons"])

    def test_a_log_newer_than_the_last_receipt_still_judges_by_the_log(self):
        # The log moved on without a matching receipt (a run that recorded no
        # steps): the receipt is from an older night, so the log decides.
        with tempfile.TemporaryDirectory() as td:
            mem = Path(td)
            log, last = self._night(mem, consolidate_ago=4000, steps_after=False)
            with mock.patch("khipu.git_sync_health.is_sync_host", return_value=True), \
                 mock.patch.object(jobs, "nightly_log_path", return_value=log), \
                 mock.patch.object(jobs, "nightly_last", return_value=last):
                out = jobs.index_freshness(memory_root=mem)
        self.assertFalse(out["ok"])
        self.assertIn("older than the last nightly", out["reasons"][0])


class EmbedProviderFixTextTest(unittest.TestCase):
    """Doctor's embed_provider_ok fix follows the recorded embed_backfill
    failure instead of always blaming the provider key."""

    def _embed_provider_row(self, entry: dict) -> dict:
        from datetime import datetime, timezone

        entry.setdefault("name", "embed_backfill")
        entry.setdefault("ok", False)
        entry.setdefault("counts", {})
        entry.setdefault("error", None)
        entry.setdefault("ts", datetime.now(timezone.utc).isoformat())
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "nightly-last.json").write_text(
                json.dumps({"steps": [entry]}), encoding="utf-8"
            )
            with mock.patch.object(jobs, "_is_index_sync_host", return_value=True), \
                    mock.patch.object(jobs, "ensure_data_dir", return_value=tmp):
                out = jobs.nightly_step_health()
        return out["embed_provider_ok"]

    def test_fix_names_the_database_on_a_connection_timeout(self):
        row = self._embed_provider_row(
            {"error": "ConnectionTimeout: connection timeout expired"}
        )
        self.assertFalse(row["ok"])
        self.assertIn("hub database", row["fix"])
        self.assertIn("reachable", row["fix"])
        self.assertNotIn("provider key", row["fix"])

    def test_fix_names_the_database_on_a_library_connection_timeout(self):
        row = self._embed_provider_row({
            "counts": {"ok": False, "library": {
                "ok": False, "error": "ConnectionTimeout: connection timeout expired"}},
        })
        self.assertFalse(row["ok"])
        self.assertIn("hub database", row["fix"])
        self.assertNotIn("provider key", row["fix"])

    def test_fix_names_the_provider_on_an_http_auth_error(self):
        kinds = {"401": "unauthorized", "403": "forbidden"}
        for code, kind in kinds.items():
            with self.subTest(code=code):
                row = self._embed_provider_row({
                    "counts": {"last_provider_failure":
                               {"http_status": int(code), "kind": kind}},
                })
                self.assertIn("embedding provider", row["fix"])
                self.assertIn(code, row["fix"])
                self.assertNotIn("hub database", row["fix"])

    def test_fix_names_quota_or_spending_cap_on_http_429(self):
        # The structured shape the real batch-failure path records (driven
        # end-to-end in test_embed.py / test_library.py); no error string
        # is injected here because the real path records none.
        row = self._embed_provider_row({
            "counts": {"library": {"results": [{
                "last_provider_failure": {"http_status": 429, "kind": "rate_limited"},
            }]}},
        })
        self.assertIn("embedding provider", row["fix"])
        self.assertTrue("quota" in row["fix"] or "spending cap" in row["fix"])
        self.assertIn("429", row["fix"])

    def test_fix_falls_back_to_the_provider_hint_on_other_failures(self):
        row = self._embed_provider_row({"error": "missing key"})
        self.assertIn("check the embedding provider key/credentials", row["fix"])


if __name__ == "__main__":
    unittest.main()
