"""Unit tests for khipu.jobs — subprocess wrappers and doctor metadata."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from khipu import jobs


class JobsRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.script = self.root / "job.py"
        self.script.write_text("import sys; sys.exit(3)\n", encoding="utf-8")
        self.state_dir = self.root / "state"
        self.data_dir = self.root / "data"
        self.log_dir = self.root / "logs"

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
             mock.patch.object(jobs.subprocess, "run", side_effect=run_fn) as run_mock:
            yield run_mock

    def test_run_nightly_invokes_consolidate_script(self):
        def _run(cmd, stdout, stderr, env):  # noqa: ARG001
            self.assertEqual(cmd[1], str(self.script))
            stdout.write(b"ok\n")
            return mock.Mock(returncode=0)

        with self._patch_run("CONSOLIDATE_NIGHTLY", _run, "khipu-nightly") as run_mock:
            rc = jobs.run_nightly()
        self.assertEqual(rc, 0)
        run_mock.assert_called_once()
        state = json.loads((self.data_dir / "state" / "job-nightly.json").read_text())
        self.assertEqual(state["exit"], 0)

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
             mock.patch.object(jobs.subprocess, "run", side_effect=_run):
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

    def test_default_monthly_script_is_claude_driver(self):
        self.assertEqual(jobs.CONSOLIDATE_MONTHLY.name, "conversation-memory-monthly.py")

    def test_run_graph_build_invokes_graphify_script(self):
        captured: list[list[str]] = []

        def _run(cmd, stdout, stderr, env):  # noqa: ARG001
            captured.append(cmd)
            return mock.Mock(returncode=0)

        with self._patch_run("GRAPHIFY_NIGHTLY", _run, "khipu-graph"):
            jobs.run_graph_build()
        self.assertEqual(captured[0][1], str(self.script))


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
            with mock.patch.object(jobs, "LOG_DIR_FROZEN", td_path):
                chosen = jobs.nightly_log_path()
            self.assertEqual(chosen.name, "khipu-nightly.out.log")

    def test_job_status_shape(self):
        with mock.patch.object(jobs, "_job_entry", side_effect=lambda name: {"name": name}):
            out = jobs.job_status()
        self.assertEqual(set(out), {"nightly", "monthly", "graph_build"})


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


if __name__ == "__main__":
    unittest.main()
