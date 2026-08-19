"""git_sync_health: the memory tree's nightly git auto-sync is judged from a
heartbeat + local repo evidence, red only on evidence (state-of-play item 7).

Also drives the real ``git_sync.py`` (outside this repo, in the Memory tree)
against a throwaway repo — clean tree and secret-gate paths, no network — to pin
the heartbeat contract the reader depends on. Skipped if that tree is absent.
"""

from __future__ import annotations

import calendar
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from khipu import git_sync_health as gh
from khipu import jobs

# The live git_sync.py only exists on a machine with the legacy memory tree;
# tests that need it are skipped elsewhere.
_MEM = os.environ.get("KHIPU_MEMORY_ROOT", "")
GIT_SYNC = Path(_MEM) / "scripts" / "git_sync.py" if _MEM else Path("/nonexistent/git_sync.py")


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    (root / "README.md").write_text("x\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True)


class GitSyncHealthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "Memory"
        self.repo.mkdir()
        _init_repo(self.repo)
        self.hb = self.root / "git-sync.json"
        self.marker = self.root / "blocked.json"
        # Pin the nightly log too: unpinned, the REAL log on this Mac leaks into
        # the test and every synthetic old heartbeat looks like a failed sync.
        self.nightly_log = self.root / "nightly.out.log"
        self.env = mock.patch.dict(
            os.environ,
            {
                "KHIPU_GIT_SYNC_HOST": "1",
                "KHIPU_GIT_SYNC_HEARTBEAT": str(self.hb),
                "KHIPU_MEMORY_REPO": str(self.repo),
            },
        )
        self.env.start()
        # Module constants are read at import; pin the marker for this test.
        self._marker_patch = mock.patch.object(gh, "BLOCKED_MARKER", self.marker)
        self._marker_patch.start()
        self._log_patch = mock.patch.object(gh, "nightly_log_path", return_value=self.nightly_log)
        self._log_patch.start()

    def _nightly_ran_at(self, iso: str):
        """Stamp the nightly log as having run at `iso` (UTC)."""
        self.nightly_log.write_text("run\n")
        t = calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))
        os.utime(self.nightly_log, (t, t))

    def tearDown(self):
        self._log_patch.stop()
        self._marker_patch.stop()
        self.env.stop()
        self.tmp.cleanup()

    def _write_hb(self, **kw):
        base = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "exit": 0,
                "outcome": "synced", "note": "synced 3 dirty path(s)", "repo": str(self.repo)}
        base.update(kw)
        self.hb.write_text(json.dumps(base))

    def test_not_host_is_not_applicable_and_green(self):
        with mock.patch.dict(os.environ, {"KHIPU_GIT_SYNC_HOST": "0"}):
            s = gh.status()
        self.assertTrue(s["ok"])
        self.assertFalse(s["applicable"])

    def test_no_heartbeat_yet_is_green_and_unseen(self):
        s = gh.status()
        self.assertTrue(s["ok"], s)
        self.assertFalse(s["seen"])
        self.assertIn("first run", s["note"])
        self.assertEqual(s["repo"]["branch"], "main")

    def test_clean_success_is_green(self):
        self._write_hb()
        s = gh.status()
        self.assertTrue(s["ok"], s)
        self.assertTrue(s["seen"])

    def test_nothing_to_sync_is_a_success(self):
        self._write_hb(outcome="nothing_to_sync", note="nothing to sync")
        self.assertTrue(gh.status()["ok"])

    def test_failed_run_is_red_with_the_note(self):
        self._write_hb(exit=1, outcome="failed", note="FAILED at 'gh pr merge': timed out")
        s = gh.status()
        self.assertFalse(s["ok"])
        self.assertIn("exited 1", s["reasons"][0])
        self.assertIn("gh pr merge", s["reasons"][0])

    def test_secret_gate_marker_newer_than_last_ok_is_red(self):
        self._write_hb(ts="2026-08-10T06:05:00Z")
        self.marker.write_text(json.dumps({"timestamp": "2026-08-11T06:05:00+00:00",
                                           "files": {"conversations/x.md": ["openai-sk"]}}))
        s = gh.status(now=time.mktime(time.strptime("2026-08-11 12:00", "%Y-%m-%d %H:%M")))
        self.assertFalse(s["ok"])
        self.assertTrue(any("secret gate" in r and "x.md" in r for r in s["reasons"]), s["reasons"])

    def test_secret_gate_marker_older_than_last_ok_is_forgiven(self):
        self.marker.write_text(json.dumps({"timestamp": "2026-08-10T06:05:00+00:00", "files": {}}))
        self._write_hb(ts="2026-08-11T06:05:00Z")
        s = gh.status(now=time.mktime(time.strptime("2026-08-11 12:00", "%Y-%m-%d %H:%M")))
        self.assertTrue(s["ok"], s["reasons"])

    def test_nightly_ran_after_last_sync_is_red(self):
        """The nightly demonstrably ran and the sync recorded nothing — a real miss."""
        self._write_hb(ts="2026-08-10T06:05:00Z")
        self._nightly_ran_at("2026-08-13T06:05:00Z")
        s = gh.status(now=time.mktime(time.strptime("2026-08-13 12:00", "%Y-%m-%d %H:%M")))
        self.assertFalse(s["ok"])
        self.assertTrue(any("did not record a result" in r for r in s["reasons"]), s["reasons"])

    def test_old_heartbeat_with_equally_old_nightly_is_green(self):
        """Machine asleep or off for days: heartbeat is stale, nothing is broken.
        The old wall-clock rule cried wolf here (audit 2026-08-17)."""
        self._write_hb(ts="2026-08-10T06:05:00Z")
        self._nightly_ran_at("2026-08-10T06:05:00Z")
        s = gh.status(now=time.mktime(time.strptime("2026-08-17 12:00", "%Y-%m-%d %H:%M")))
        self.assertTrue(s["ok"], s["reasons"])

    def test_no_nightly_log_never_invents_staleness(self):
        self._write_hb(ts="2026-08-01T06:05:00Z")
        s = gh.status(now=time.mktime(time.strptime("2026-08-17 12:00", "%Y-%m-%d %H:%M")))
        self.assertTrue(s["ok"], s["reasons"])
        self.assertIsNone(s["nightly_log_age_s"])

    def test_stranded_branch_is_red_even_without_heartbeat(self):
        subprocess.run(["git", "-C", str(self.repo), "checkout", "-q", "-b", "memory-autosync-2026-08-17-0205"], check=True)
        s = gh.status()
        self.assertFalse(s["ok"])
        self.assertTrue(any("not main" in r for r in s["reasons"]), s["reasons"])
        self.assertTrue(any("stray sync branch" in r for r in s["reasons"]), s["reasons"])

    def test_prefers_khipu_nightly_plist_when_present(self):
        with tempfile.TemporaryDirectory() as td:
            agents = Path(td)
            khipu = agents / "com.matt.khipu-nightly.plist"
            legacy = agents / "com.matt.conversation-memory-nightly.plist"
            khipu.write_text("plist")
            with mock.patch.object(jobs, "_launchagents_dir", return_value=agents):
                self.assertEqual(gh.nightly_plist_path(), khipu)
            legacy.write_text("plist")
            khipu.unlink()
            with mock.patch.object(jobs, "_launchagents_dir", return_value=agents):
                self.assertEqual(gh.nightly_plist_path(), legacy)


@unittest.skipUnless(GIT_SYNC.is_file(), "Memory tree not mounted")
class GitSyncScriptHeartbeatTest(unittest.TestCase):
    """Run the shipped git_sync.py, the way launchd does, against a temp repo."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "Memory"
        self.repo.mkdir()
        _init_repo(self.repo)
        self.hb = self.root / "hb.json"
        self.marker = self.root / "blocked.json"
        self.env = {
            **os.environ,
            "GIT_SYNC_REPO_ROOT": str(self.repo),
            "KHIPU_GIT_SYNC_HEARTBEAT": str(self.hb),
            "KHIPU_GIT_SYNC_BLOCKED_MARKER": str(self.marker),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self):
        return subprocess.run([sys.executable, str(GIT_SYNC)], env=self.env,
                              capture_output=True, text=True, timeout=60)

    def test_clean_tree_writes_ok_heartbeat(self):
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        hb = json.loads(self.hb.read_text())
        self.assertEqual(hb["exit"], 0)
        self.assertEqual(hb["outcome"], "nothing_to_sync")
        self.assertEqual(hb["repo"], str(self.repo))
        with mock.patch.dict(os.environ, {"KHIPU_GIT_SYNC_HOST": "1", "KHIPU_GIT_SYNC_HEARTBEAT": str(self.hb)}), \
             mock.patch.object(gh, "BLOCKED_MARKER", self.marker):
            self.assertTrue(gh.status()["ok"])

    def test_secret_gate_writes_blocked_heartbeat_and_reader_goes_red(self):
        (self.repo / "leak.md").write_text("token sk-" + "a" * 32 + "\n")
        r = self._run()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        hb = json.loads(self.hb.read_text())
        self.assertEqual(hb["exit"], 2)
        self.assertEqual(hb["outcome"], "blocked")
        self.assertTrue(self.marker.is_file())
        # Nothing was committed: the tree is still dirty and on main.
        st = subprocess.run(["git", "-C", str(self.repo), "status", "--porcelain"], capture_output=True, text=True)
        self.assertIn("leak.md", st.stdout)
        with mock.patch.dict(os.environ, {"KHIPU_GIT_SYNC_HOST": "1", "KHIPU_GIT_SYNC_HEARTBEAT": str(self.hb)}), \
             mock.patch.object(gh, "BLOCKED_MARKER", self.marker):
            s = gh.status()
        self.assertFalse(s["ok"])
        self.assertTrue(any("exited 2" in x for x in s["reasons"]), s["reasons"])
        self.assertTrue(any("secret gate" in x for x in s["reasons"]), s["reasons"])


if __name__ == "__main__":
    unittest.main()
