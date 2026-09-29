# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub build agent; there is no further agent to route this to.
"""The nightly's brief-building step: switch gating, the shared build lock,
the per-night cap and fail-open behavior. Everything is faked; no database."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import jobs


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _Conn()

    def commit(self):
        pass


class BriefsBuildStepTest(unittest.TestCase):
    def _run(self, *, enabled=True, lock=True, select=None, build=None):
        select = select or mock.Mock(return_value={
            "available": True, "topics": [{"topic": "a"}, {"topic": "b"}],
        })
        build = build or mock.Mock(return_value=[
            {"topic": "a", "status": "built"}, {"topic": "b", "status": "unchanged"},
        ])
        with mock.patch("khipu.features.enabled", return_value=enabled), \
                mock.patch("khipu.db.connect", return_value=_Conn()) as m_connect, \
                mock.patch("khipu.briefs.acquire_build_lock", return_value=lock) as m_acquire, \
                mock.patch("khipu.briefs.release_build_lock") as m_release, \
                mock.patch("khipu.briefs.select_for_build", select), \
                mock.patch("khipu.briefs.build_many", build), \
                mock.patch.object(jobs, "_nightly_log"):
            out = jobs._briefs_build_if_on()
        return out, m_connect, m_acquire, m_release, select, build

    def test_switch_off_returns_none_and_opens_no_connection(self):
        out, m_connect, m_acquire, _, select, build = self._run(enabled=False)
        self.assertIsNone(out)
        m_connect.assert_not_called()
        m_acquire.assert_not_called()
        select.assert_not_called()
        build.assert_not_called()

    def test_switch_on_reports_built_count_and_statuses(self):
        out, _, _, m_release, _, build = self._run()
        self.assertEqual(out, {"ok": True, "built": 1, "unchanged": 1})
        build.assert_called_once()
        m_release.assert_called_once()

    def test_lock_held_builds_nothing_and_is_ok(self):
        out, _, _, m_release, select, build = self._run(lock=False)
        self.assertTrue(out["ok"])
        self.assertEqual(out["built"], 0)
        select.assert_not_called()
        build.assert_not_called()
        m_release.assert_not_called()

    def test_build_raising_is_not_ok_and_releases_the_lock(self):
        out, _, _, m_release, _, _ = self._run(build=mock.Mock(side_effect=RuntimeError("boom")))
        self.assertFalse(out["ok"])
        self.assertIn("boom", out["error"])
        m_release.assert_called_once()

    def test_cap_is_passed_to_the_selection(self):
        _, _, _, _, select, _ = self._run()
        self.assertEqual(jobs.BRIEFS_NIGHTLY_LIMIT, 40)
        select.assert_called_once()
        self.assertEqual(select.call_args.args[1:], (None, jobs.BRIEFS_NIGHTLY_LIMIT))

    def test_unavailable_selection_builds_nothing_and_is_ok(self):
        select = mock.Mock(return_value={"available": False, "reason": "table missing"})
        out, _, _, m_release, _, build = self._run(select=select)
        self.assertTrue(out["ok"])
        self.assertEqual(out["built"], 0)
        build.assert_not_called()
        m_release.assert_called_once()


class RunNightlyBriefsStepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)

    def _nightly(self, briefs_patch):
        with mock.patch.object(jobs, "ensure_data_dir", return_value=self.data_dir), \
                mock.patch.object(jobs, "_run_script", return_value=0), \
                mock.patch.object(jobs, "_reconcile_notes_if_due", return_value={"ok": True}), \
                mock.patch.object(jobs, "_embed_backfill", return_value={"ok": True}), \
                mock.patch.object(jobs, "_prune_query_cache", return_value={"ok": True}), \
                mock.patch.object(jobs, "_mark_stale_commitments", return_value={"ok": True}), \
                mock.patch.object(jobs, "_hygiene_commitments", return_value={"ok": True}), \
                mock.patch.object(jobs, "_nightly_log"), \
                briefs_patch:
            rc = jobs.run_nightly()
        steps = json.loads((self.data_dir / "nightly-last.json").read_text())["steps"]
        return rc, steps

    def test_switch_off_records_no_briefs_step(self):
        rc, steps = self._nightly(mock.patch.object(jobs, "_briefs_build_if_on", return_value=None))
        self.assertEqual(rc, 0)
        self.assertNotIn("briefs_build", [s["name"] for s in steps])

    def test_switch_on_records_the_step_last_with_the_built_count(self):
        rc, steps = self._nightly(
            mock.patch.object(jobs, "_briefs_build_if_on", return_value={"ok": True, "built": 3})
        )
        self.assertEqual(rc, 0)
        self.assertEqual(steps[-1]["name"], "briefs_build")
        self.assertEqual(steps[-1]["counts"]["built"], 3)

    def test_build_many_raising_still_returns_the_exit_code(self):
        patches = mock.patch.multiple(
            "khipu.briefs",
            acquire_build_lock=mock.Mock(return_value=True),
            release_build_lock=mock.Mock(),
            select_for_build=mock.Mock(return_value={"available": True, "topics": [{"topic": "a"}]}),
            build_many=mock.Mock(side_effect=RuntimeError("boom")),
        )
        with mock.patch("khipu.features.enabled", return_value=True), \
                mock.patch("khipu.db.connect", return_value=_Conn()):
            rc, steps = self._nightly(patches)
        self.assertEqual(rc, 0)
        self.assertEqual(steps[-1]["name"], "briefs_build")
        self.assertFalse(steps[-1]["ok"])


if __name__ == "__main__":
    unittest.main()
