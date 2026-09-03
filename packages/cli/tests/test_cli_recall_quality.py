"""Tests for the W6 CLI wiring: `khipu doctor --probe`, `recall_probe_ok` /
`recall_quality` in doctor+status output, and `khipu recall eval`. Every
dependency cmd_doctor/cmd_status pulls in is mocked — no live database, no
real probe write. A separate file so this does not collide with the
concurrent agent's cmd_doctor/cmd_status internals (search/drift/etc.) or
with tests/test_cli_memory_reliability.py's owed/forget/purge coverage.
"""
from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

from khipu import cli


def _green_doctor_patches():
    """Every dependency cmd_doctor pulls in, mocked to an all-green baseline,
    so a test can layer ONE override on top and see just that effect."""
    return [
        mock.patch("khipu.drift.status_payload", return_value={"latest_ingested_at": None}),
        mock.patch("khipu.hub_snapshot.maybe_refresh", return_value=None),
        mock.patch("khipu.hub_snapshot.snapshot_freshness", return_value={"ok": True}),
        mock.patch("khipu.hub_snapshot.snapshot_health", return_value={"ok": True}),
        mock.patch("khipu.keychain.secrets_status", return_value={"dsn_file": {"ok": True}}),
        mock.patch("khipu.drift.backup_health", return_value={"ok": True}),
        mock.patch("khipu.graph_backup.local_health", return_value={"ok": True}),
        mock.patch("khipu.graph_backup.offsite_health", return_value={"ok": True}),
        mock.patch("khipu.config.path_setting", return_value=None),  # graph_sqlite not configured
        mock.patch("khipu.outbox.status", return_value={"pending": 0}),
        mock.patch("khipu.outbox.drain", return_value={"failed": 0}),
        mock.patch("khipu.session_capture.queued_jobs", return_value=False),
        mock.patch("khipu.session_capture.drain", return_value=None),
        mock.patch("khipu.session_capture.liveness_all", return_value={"ok": True, "red": [], "harnesses": {}}),
        mock.patch("khipu.git_sync_health.status", return_value={"ok": True}),
        mock.patch("khipu.jobs.job_status", return_value={}),
        mock.patch("khipu.jobs.index_freshness", return_value={"ok": True}),
        mock.patch("khipu.embed.coverage", return_value={
            "episodes": {"missing": 0}, "topics": {"missing": 0},
        }),
    ]


class _Patched(unittest.TestCase):
    def setUp(self):
        for p in _green_doctor_patches():
            p.start()
            self.addCleanup(p.stop)


class DoctorProbeFlagTest(_Patched):
    def test_plain_doctor_never_calls_run_probe(self):
        parser = cli.build_parser()
        args = parser.parse_args(["doctor"])
        with mock.patch("khipu.probe.run_probe") as m_run, \
             mock.patch("khipu.probe.status", return_value={"ok": True, "reason": None}), \
             mock.patch("khipu.drift.recall_quality", return_value={}):
            with redirect_stdout(io.StringIO()):
                cli.cmd_doctor(args)
        m_run.assert_not_called()

    def test_probe_flag_calls_run_probe_then_reads_status(self):
        parser = cli.build_parser()
        args = parser.parse_args(["doctor", "--probe", "--harness", "claude_code"])
        with mock.patch("khipu.probe.run_probe") as m_run, \
             mock.patch("khipu.probe.status", return_value={"ok": True, "reason": None}) as m_status, \
             mock.patch("khipu.drift.recall_quality", return_value={}):
            with redirect_stdout(io.StringIO()):
                cli.cmd_doctor(args)
        m_run.assert_called_once_with("claude_code")
        m_status.assert_called_once()

    def test_probe_harness_defaults_to_env_or_doctor(self):
        parser = cli.build_parser()
        args = parser.parse_args(["doctor", "--probe"])
        with mock.patch.dict(os.environ, {"KHIPU_HARNESS": ""}, clear=False), \
             mock.patch("khipu.probe.run_probe") as m_run, \
             mock.patch("khipu.probe.status", return_value={"ok": True, "reason": None}), \
             mock.patch("khipu.drift.recall_quality", return_value={}):
            with redirect_stdout(io.StringIO()):
                cli.cmd_doctor(args)
        m_run.assert_called_once_with("doctor")


class DoctorAggregateOkTest(_Patched):
    def _run(self, *, recall_probe_ok, hub_snapshot_ok, extra_snapshot_patch=None):
        parser = cli.build_parser()
        args = parser.parse_args(["doctor"])
        patches = [
            mock.patch("khipu.probe.status",
                        return_value={"ok": recall_probe_ok, "reason": None if recall_probe_ok else "failed"}),
            mock.patch("khipu.drift.recall_quality", return_value={"one_episode_session_ratio": {"ok": True}}),
        ]
        if not hub_snapshot_ok:
            patches.append(mock.patch("khipu.hub_snapshot.snapshot_freshness",
                                        return_value={"ok": False, "reason": "snapshot_behind_ingest"}))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.cmd_doctor(args)
        return rc, json.loads(buf.getvalue())

    def test_everything_green_including_probe_and_snapshot_is_ok(self):
        rc, out = self._run(recall_probe_ok=True, hub_snapshot_ok=True)
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])
        self.assertTrue(out["recall_probe_ok"])
        self.assertIn("recall_quality", out)
        self.assertIn("recall_probe", out)

    def test_failed_recall_probe_fails_doctor_even_if_everything_else_is_green(self):
        rc, out = self._run(recall_probe_ok=False, hub_snapshot_ok=True)
        self.assertEqual(rc, 2)
        self.assertFalse(out["ok"])
        self.assertFalse(out["recall_probe_ok"])

    def test_stale_hub_snapshot_fails_doctor(self):
        rc, out = self._run(recall_probe_ok=True, hub_snapshot_ok=False)
        self.assertEqual(rc, 2)
        self.assertFalse(out["ok"])
        self.assertFalse(out["hub_snapshot"]["ok"])

    def test_ratio_metrics_alone_never_fail_doctor(self):
        """recall_quality ratios are warn-only (scope §W6.2) — a red ratio
        inside the block must not flip doctor's overall ok."""
        parser = cli.build_parser()
        args = parser.parse_args(["doctor"])
        with mock.patch("khipu.probe.status", return_value={"ok": True, "reason": None}), \
             mock.patch("khipu.drift.recall_quality",
                          return_value={"junk_path_ratio": {"value": 0.9, "threshold": 0.05, "ok": False}}):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.cmd_doctor(args)
        out = json.loads(buf.getvalue())
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])
        self.assertFalse(out["recall_quality"]["junk_path_ratio"]["ok"])


class StatusRecallQualityTest(unittest.TestCase):
    def test_status_includes_recall_quality_block(self):
        parser = cli.build_parser()
        args = parser.parse_args(["status"])
        with mock.patch("khipu.drift.status_payload", return_value={"latest_ingested_at": None}), \
             mock.patch("khipu.hub_snapshot.snapshot_freshness", return_value={"ok": True}), \
             mock.patch("khipu.drift.recall_quality", return_value={"one_episode_session_ratio": {"ok": True}}) as m_rq:
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_status(args)
        out = json.loads(buf.getvalue())
        self.assertIn("recall_quality", out)
        m_rq.assert_called_once()

    def test_recall_quality_failure_does_not_blank_status(self):
        parser = cli.build_parser()
        args = parser.parse_args(["status"])
        with mock.patch("khipu.drift.status_payload", return_value={"latest_ingested_at": None}), \
             mock.patch("khipu.hub_snapshot.snapshot_freshness", return_value={"ok": True}), \
             mock.patch("khipu.drift.recall_quality", side_effect=RuntimeError("boom")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.cmd_status(args)
        out = json.loads(buf.getvalue())
        self.assertEqual(rc, 0)
        self.assertIn("error", out["recall_quality"])


class RecallEvalCliTest(unittest.TestCase):
    def test_eval_subcommand_exits_zero_when_above_threshold(self):
        parser = cli.build_parser()
        args = parser.parse_args(["recall", "eval"])
        report = {"path": "x", "total": 4, "hits": 4, "overall_hit_rate": 1.0, "rows": []}
        with mock.patch("khipu.recall_eval.run_eval", return_value=report):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.cmd_recall(args)
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["overall_hit_rate"], 1.0)

    def test_eval_subcommand_exits_one_below_threshold(self):
        parser = cli.build_parser()
        args = parser.parse_args(["recall", "eval"])
        report = {"path": "x", "total": 4, "hits": 1, "overall_hit_rate": 0.25, "rows": []}
        with mock.patch("khipu.recall_eval.run_eval", return_value=report):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.cmd_recall(args)
        self.assertEqual(rc, 1)

    def test_eval_golden_flag_is_forwarded_as_a_path(self):
        parser = cli.build_parser()
        args = parser.parse_args(["recall", "eval", "--golden", "/tmp/custom.jsonl"])
        report = {"path": "/tmp/custom.jsonl", "total": 0, "hits": 0, "overall_hit_rate": 0.0, "rows": []}
        with mock.patch("khipu.recall_eval.run_eval", return_value=report) as m:
            with redirect_stdout(io.StringIO()):
                cli.cmd_recall(args)
        called_path = m.call_args[0][0]
        self.assertEqual(str(called_path), "/tmp/custom.jsonl")

    def test_bad_golden_file_reports_error_and_exits_two(self):
        parser = cli.build_parser()
        args = parser.parse_args(["recall", "eval"])
        with mock.patch("khipu.recall_eval.run_eval", side_effect=ValueError("bad line")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.cmd_recall(args)
        self.assertEqual(rc, 2)
        out = json.loads(buf.getvalue())
        self.assertFalse(out["ok"])


if __name__ == "__main__":
    unittest.main()
