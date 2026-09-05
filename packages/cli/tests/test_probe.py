"""Tests for khipu.probe (W6.1) — the end-to-end "capture then search finds
it" probe. Everything here is fakes: khipu.capture.capture, khipu.embed.
hybrid_search, and khipu.db.connect are all mocked, and the state file lives
in a temp dir. This module must NEVER be exercised against a live database
from a build/test run — that is the orchestrator's call, not this agent's.
"""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from khipu import probe


class _FakeCur:
    """Just enough of a cursor for _find_episode_id / _forget_episode."""

    def __init__(self, episode_id):
        self.episode_id = episode_id
        self.rowcount = 0
        self.executed = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.executed.append((s, params))
        if s.startswith("UPDATE episodes SET deleted_at"):
            self.rowcount = 1
        elif s.startswith("DELETE FROM memory_embeddings"):
            self.rowcount = 1

    def fetchone(self):
        if self.episode_id is None:
            return None
        last_sql = self.executed[-1][0] if self.executed else ""
        if last_sql.startswith("SELECT ts, summary, session_id"):
            # khipu.forget.forget_episode's identity lookup, now used by the
            # probe's cleanup instead of a hand-rolled soft-delete.
            return ("2026-01-01T00:00:00+00:00", "probe episode", "claude_code:probe")
        return (self.episode_id,)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.committed = False

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class RunProbeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = pathlib.Path(self.tmp.name)
        self._patches = [
            mock.patch("khipu.paths.data_dir", return_value=self.data_dir),
            mock.patch("khipu.paths.ensure_data_dir", return_value=self.data_dir),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_success_finds_episode_immediately(self):
        cur = _FakeCur(episode_id=42)
        conn = _FakeConn(cur)
        found_result = {"results": [{"kind": "episode", "id": "42", "score": 0.9}]}
        with mock.patch("khipu.capture.capture", return_value=0) as m_capture, \
             mock.patch("khipu.embed.hybrid_search", return_value=found_result) as m_search, \
             mock.patch("khipu.db.connect", return_value=conn):
            result = probe.run_probe("claude_code", poll_interval_s=0.01, timeout_s=1.0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["episode_id"], 42)
        self.assertIsNone(result["error"])
        self.assertEqual(result["harness"], "claude_code")
        self.assertTrue(m_capture.called)
        m_search.assert_called()
        # Cleanup ran (soft-delete + embeddings drop), regardless of outcome.
        self.assertTrue(result["cleanup"]["ok"])
        self.assertTrue(result["cleanup"]["soft_deleted"])
        # State file was written with the same result.
        state = json.loads(probe.state_path().read_text())
        self.assertEqual(state["episode_id"], 42)
        self.assertTrue(state["ok"])

    def test_capture_failure_is_recorded_not_raised(self):
        with mock.patch("khipu.capture.capture", return_value=70), \
             mock.patch("khipu.embed.hybrid_search") as m_search:
            result = probe.run_probe("cursor", poll_interval_s=0.01, timeout_s=0.5)
        self.assertFalse(result["ok"])
        self.assertIn("capture exited 70", result["error"])
        m_search.assert_not_called()
        self.assertIsNone(result["episode_id"])
        self.assertNotIn("cleanup", result)  # nothing to clean up: no episode was found

    def test_episode_never_found_after_capture(self):
        cur = _FakeCur(episode_id=None)
        conn = _FakeConn(cur)
        with mock.patch("khipu.capture.capture", return_value=0), \
             mock.patch("khipu.db.connect", return_value=conn), \
             mock.patch("khipu.embed.hybrid_search") as m_search:
            result = probe.run_probe("codex", poll_interval_s=0.01, timeout_s=0.5)
        self.assertFalse(result["ok"])
        self.assertIn("no episode row was found", result["error"])
        m_search.assert_not_called()

    def test_search_never_surfaces_it_times_out(self):
        cur = _FakeCur(episode_id=7)
        conn = _FakeConn(cur)
        never_found = {"results": [{"kind": "episode", "id": "999", "score": 0.1}]}
        with mock.patch("khipu.capture.capture", return_value=0), \
             mock.patch("khipu.db.connect", return_value=conn), \
             mock.patch("khipu.embed.hybrid_search", return_value=never_found):
            result = probe.run_probe("aegis", poll_interval_s=0.01, timeout_s=0.05)
        self.assertFalse(result["ok"])
        self.assertIn("did not reach top-3", result["error"])
        # Cleanup still ran even though the probe itself failed.
        self.assertTrue(result["cleanup"]["ok"])

    def test_cleanup_failure_does_not_mask_probe_success(self):
        cur = _FakeCur(episode_id=5)
        conn = _FakeConn(cur)
        found_result = {"results": [{"kind": "episode", "id": "5", "score": 0.9}]}

        calls = {"n": 0}

        def connect_side_effect(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return conn
            raise RuntimeError("connection reset")

        with mock.patch("khipu.capture.capture", return_value=0), \
             mock.patch("khipu.embed.hybrid_search", return_value=found_result), \
             mock.patch("khipu.db.connect", side_effect=connect_side_effect):
            result = probe.run_probe("claude_code", poll_interval_s=0.01, timeout_s=1.0)
        self.assertTrue(result["ok"])
        self.assertFalse(result["cleanup"]["ok"])
        self.assertIn("connection reset", result["cleanup"]["error"])


class LegacyCaptureModeSkipTest(unittest.TestCase):
    """fix 12: capture_mode()=='legacy' means Khipu never writes PG — the
    probe must record 'skipped', not fail the PG assertion every time."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = pathlib.Path(self.tmp.name)
        self._patches = [
            mock.patch("khipu.paths.data_dir", return_value=self.data_dir),
            mock.patch("khipu.paths.ensure_data_dir", return_value=self.data_dir),
            mock.patch("khipu.config.capture_mode", return_value="legacy"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_legacy_mode_skips_without_touching_capture_or_search(self):
        with mock.patch("khipu.capture.capture") as m_capture, \
                mock.patch("khipu.embed.hybrid_search") as m_search:
            result = probe.run_probe("cursor", poll_interval_s=0.01, timeout_s=0.5)
        m_capture.assert_not_called()
        m_search.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "legacy-capture-mode")
        self.assertIsNone(result["episode_id"])
        self.assertIsNone(result["error"])

    def test_legacy_mode_skip_is_persisted_to_state(self):
        with mock.patch("khipu.capture.capture"), mock.patch("khipu.embed.hybrid_search"):
            probe.run_probe("cursor")
        state = json.loads(probe.state_path().read_text())
        self.assertEqual(state["status"], "skipped")
        self.assertTrue(state["ok"])

    def test_doctor_reads_a_skipped_probe_as_not_red(self):
        with mock.patch("khipu.capture.capture"), mock.patch("khipu.embed.hybrid_search"):
            probe.run_probe("cursor")
        st = probe.status()
        self.assertTrue(st["ok"])
        self.assertIsNone(st["reason"])


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = pathlib.Path(self.tmp.name)
        self._patches = [mock.patch("khipu.paths.data_dir", return_value=self.data_dir)]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, **fields):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / probe.STATE_FILE).write_text(json.dumps(fields))

    def test_no_probe_yet_is_red(self):
        st = probe.status()
        self.assertFalse(st["ok"])
        self.assertIn("no probe has ever run", st["reason"])

    def test_recent_success_is_green(self):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._write(ts=ts, harness="claude_code", ok=True, seconds=1.2, episode_id=1, error=None)
        st = probe.status()
        self.assertTrue(st["ok"])
        self.assertIsNone(st["reason"])

    def test_last_probe_failed_is_red(self):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._write(ts=ts, harness="claude_code", ok=False, seconds=1.2, episode_id=1,
                    error="timeout")
        st = probe.status()
        self.assertFalse(st["ok"])
        self.assertIn("last probe failed", st["reason"])

    def test_stale_probe_is_red_even_if_it_succeeded(self):
        old = datetime.now(timezone.utc) - timedelta(days=8)
        self._write(ts=old.strftime("%Y-%m-%dT%H:%M:%SZ"), harness="claude_code", ok=True,
                    seconds=1.0, episode_id=1, error=None)
        st = probe.status()
        self.assertFalse(st["ok"])
        self.assertIn("stale", st["reason"])

    def test_within_seven_days_is_not_stale(self):
        recent = datetime.now(timezone.utc) - timedelta(days=6, hours=23)
        self._write(ts=recent.strftime("%Y-%m-%dT%H:%M:%SZ"), harness="claude_code", ok=True,
                    seconds=1.0, episode_id=1, error=None)
        st = probe.status()
        self.assertTrue(st["ok"])


if __name__ == "__main__":
    unittest.main()
