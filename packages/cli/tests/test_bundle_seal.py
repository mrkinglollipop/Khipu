# --bypass-harness (sonnet lane) — dispatched on-sub Sonnet build agent.
"""Tests for khipu.bundle_seal — the tripwire for the 0.3.15 "Khipu is
damaged" incident (bundled Python wrote __pycache__ inside a signed .app,
breaking the code signature; see khipu/bundle_seal.py docstring).
"""
from __future__ import annotations

import io
import json
import subprocess
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from khipu import bundle_seal, cli


class AppBundleRootTest(unittest.TestCase):
    def test_no_dot_app_ancestor_is_none(self):
        root = Path("/Users/matt/Code/khipu/packages/cli")
        self.assertIsNone(bundle_seal._app_bundle_root(root))

    def test_finds_enclosing_app_three_levels_up(self):
        root = Path("/Applications/Khipu.app/Contents/Resources/khipu")
        app = bundle_seal._app_bundle_root(root)
        self.assertEqual(app, Path("/Applications/Khipu.app"))

    def test_the_root_itself_can_be_the_app(self):
        root = Path("/Applications/Khipu.app")
        self.assertEqual(bundle_seal._app_bundle_root(root), root)


class CheckNaTest(unittest.TestCase):
    def test_na_when_not_inside_a_bundle(self):
        out = bundle_seal.check(root=Path("/Users/matt/Code/khipu"))
        self.assertTrue(out["ok"])
        self.assertTrue(out.get("na"))

    def test_na_when_codesign_missing(self):
        with mock.patch("khipu.bundle_seal.subprocess.run", side_effect=FileNotFoundError()):
            out = bundle_seal.check(root=Path("/Applications/Khipu.app/Contents/Resources/khipu"))
        self.assertTrue(out["ok"])
        self.assertTrue(out.get("na"))


class CheckSealTest(unittest.TestCase):
    APP_ROOT = Path("/Applications/Khipu.app/Contents/Resources/khipu")

    def test_ok_when_codesign_exits_zero(self):
        with mock.patch(
            "khipu.bundle_seal.subprocess.run",
            return_value=mock.Mock(returncode=0, stdout="", stderr=""),
        ):
            out = bundle_seal.check(root=self.APP_ROOT)
        self.assertTrue(out["ok"])
        self.assertNotIn("na", out)
        self.assertEqual(out["app"], "/Applications/Khipu.app")

    def test_red_extracts_first_file_added_line(self):
        stderr = (
            "Executable=/Applications/Khipu.app/Contents/MacOS/Khipu\n"
            "file added: /Applications/Khipu.app/Contents/Resources/khipu/"
            "packages/cli/khipu/__pycache__/cli.cpython-311.pyc\n"
            "some other trailing noise\n"
        )
        with mock.patch(
            "khipu.bundle_seal.subprocess.run",
            return_value=mock.Mock(returncode=1, stdout="", stderr=stderr),
        ):
            out = bundle_seal.check(root=self.APP_ROOT)
        self.assertFalse(out["ok"])
        self.assertIn("file added", out["error"])
        self.assertIn("__pycache__", out["error"])

    def test_red_on_timeout(self):
        with mock.patch(
            "khipu.bundle_seal.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="codesign", timeout=20),
        ):
            out = bundle_seal.check(root=self.APP_ROOT)
        self.assertFalse(out["ok"])
        self.assertIn("timed out", out["error"])


def _green_doctor_patches():
    """Mirrors test_cli_recall_quality._green_doctor_patches — kept local
    (not imported) so this file has no cross-file coupling."""
    return [
        mock.patch("khipu.drift.status_payload", return_value={"latest_ingested_at": None}),
        mock.patch("khipu.hub_snapshot.maybe_refresh", return_value=None),
        mock.patch("khipu.hub_snapshot.snapshot_freshness", return_value={"ok": True}),
        mock.patch("khipu.hub_snapshot.snapshot_health", return_value={"ok": True}),
        mock.patch("khipu.keychain.secrets_status", return_value={"dsn_file": {"ok": True}}),
        mock.patch("khipu.drift.backup_health", return_value={"ok": True}),
        mock.patch("khipu.graph_backup.local_health", return_value={"ok": True}),
        mock.patch("khipu.graph_backup.offsite_health", return_value={"ok": True}),
        mock.patch("khipu.config.path_setting", return_value=None),
        mock.patch("khipu.outbox.status", return_value={"pending": 0}),
        mock.patch("khipu.outbox.drain", return_value={"failed": 0}),
        mock.patch("khipu.session_capture.queued_jobs", return_value=False),
        mock.patch("khipu.session_capture.drain", return_value=None),
        mock.patch(
            "khipu.session_capture.liveness_all",
            return_value={"ok": True, "red": [], "harnesses": {}},
        ),
        mock.patch("khipu.git_sync_health.status", return_value={"ok": True}),
        mock.patch("khipu.jobs.job_status", return_value={}),
        mock.patch("khipu.jobs.index_freshness", return_value={"ok": True}),
        mock.patch(
            "khipu.embed.coverage",
            return_value={"episodes": {"missing": 0}, "topics": {"missing": 0}},
        ),
        mock.patch("khipu.probe.status", return_value={"ok": True, "reason": None}),
        mock.patch("khipu.drift.recall_quality", return_value={}),
    ]


class DoctorFoldsBundleSealTest(unittest.TestCase):
    """Doctor's overall `ok` must go red the moment bundle_seal does — this is
    the actual regression guard: a fake subprocess simulates the 0.3.15
    scenario (codesign finds an added __pycache__ file) and asserts doctor
    surfaces it rather than staying green."""

    def setUp(self):
        for p in _green_doctor_patches():
            p.start()
            self.addCleanup(p.stop)

    def _run_doctor(self):
        parser = cli.build_parser()
        args = parser.parse_args(["doctor"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.cmd_doctor(args)
        return rc, json.loads(buf.getvalue())

    def test_clean_signature_keeps_doctor_green(self):
        with mock.patch(
            "khipu.bundle_seal.check",
            return_value={"ok": True, "app": "/Applications/Khipu.app"},
        ):
            rc, out = self._run_doctor()
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])
        self.assertTrue(out["bundle_seal_ok"])

    def test_na_off_a_maintainer_checkout_keeps_doctor_green(self):
        with mock.patch(
            "khipu.bundle_seal.check",
            return_value={"ok": True, "na": True, "reason": "not a bundle"},
        ):
            rc, out = self._run_doctor()
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])

    def test_broken_seal_via_fake_subprocess_fails_doctor(self):
        fake_run = mock.Mock(
            return_value=mock.Mock(
                returncode=1,
                stdout="",
                stderr=(
                    "file added: /Applications/Khipu.app/Contents/Resources/khipu/"
                    "packages/cli/khipu/__pycache__/cli.cpython-311.pyc\n"
                ),
            )
        )
        with mock.patch(
            "khipu.paths.repo_root",
            return_value=Path("/Applications/Khipu.app/Contents/Resources/khipu"),
        ), mock.patch("khipu.bundle_seal.subprocess.run", fake_run):
            rc, out = self._run_doctor()
        self.assertEqual(rc, 2)
        self.assertFalse(out["ok"])
        self.assertFalse(out["bundle_seal_ok"])
        self.assertIn("file added", out["bundle_seal"]["error"])
        fake_run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
