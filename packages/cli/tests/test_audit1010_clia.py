"""Focused CLI regressions from audit1010-clia; all dependencies are mocked."""
from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from khipu import cli
from tests.test_cli_recall_quality import _green_doctor_patches


class ImportLocalSemanticsTest(unittest.TestCase):
    def test_default_skips_existing_files_and_replace_overwrites_them(self):
        base = argparse.Namespace(source="/tmp/source", replace=False)
        replace = argparse.Namespace(source="/tmp/source", replace=True)
        with mock.patch("khipu.paths.import_local", return_value={"ok": True}) as imported, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_import_local(base), 0)
            self.assertEqual(cli.cmd_import_local(replace), 0)
        self.assertEqual([call.kwargs["merge"] for call in imported.call_args_list], [False, True])

    def test_directory_and_zip_collisions_preserve_by_default_and_replace_on_request(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source"
            source.mkdir()
            (source / "collision.txt").write_text("source", encoding="utf-8")
            archive = root / "source.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("collision.txt", "zip-source")
            for incoming, expected in ((source, "source"), (archive, "zip-source")):
                data_dir = root / f"target-{incoming.suffix or 'directory'}"
                data_dir.mkdir()
                target = data_dir / "collision.txt"
                target.write_text("existing", encoding="utf-8")
                with mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(data_dir)}), redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(["import-local", "--source", str(incoming)]), 0)
                self.assertEqual(target.read_text(encoding="utf-8"), "existing")
                with mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(data_dir)}), redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(["import-local", "--source", str(incoming), "--replace"]), 0)
                self.assertEqual(target.read_text(encoding="utf-8"), expected)


class EmbedJobForwardingTest(unittest.TestCase):
    def test_job_backfill_forwards_kind_and_dry_run(self):
        args = argparse.Namespace(profile="p@3", job_id="audit", limit=7, kind="topic", dry_run=True)
        seen = {}

        def backfill(**kwargs):
            seen.update(kwargs)
            return {"embedded": 0, "failed_chunks": 0}

        with mock.patch("khipu.embed.backfill", side_effect=backfill), \
                mock.patch("khipu.embed_jobs.run_job", side_effect=lambda **kwargs: kwargs["work"](object())), \
                redirect_stdout(io.StringIO()):
            cli.cmd_embed_backfill_job(args)
        self.assertEqual((seen["kind"], seen["dry_run"]), ("topic", True))


class DatabaseMoveExclusivityTest(unittest.TestCase):
    def test_parser_rejects_new_local_with_explicit_target(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["db", "move", "--new-local", "--to", "postgres://target"])

    def test_new_local_rejects_target_environment_before_provisioning(self):
        args = cli.build_parser().parse_args(["db", "move", "--new-local"])
        with mock.patch.dict(os.environ, {"KHIPU_DB_TARGET_DSN": "postgres://target"}, clear=False), \
                mock.patch("khipu.setup.provision_local_move_target") as provision, \
                redirect_stdout(io.StringIO()):
            rc = cli.cmd_db(args)
        self.assertEqual(rc, 2)
        provision.assert_not_called()


class EmbedExitStatusTest(unittest.TestCase):
    def test_nonjob_backfill_fails_when_chunks_fail(self):
        args = argparse.Namespace(embed_cmd="backfill", kind=None, limit=None, dry_run=False, profile=None)
        with mock.patch("khipu.embed.backfill", return_value={"embedded": 3, "failed_chunks": 1}), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_embed(args), 1)

    def test_nonjob_backfill_fails_when_budget_is_exhausted(self):
        args = argparse.Namespace(embed_cmd="backfill", kind=None, limit=None, dry_run=False, profile=None)
        with mock.patch("khipu.embed.backfill", return_value={"embedded": 3, "budget_exhausted": True}), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_embed(args), 1)

    def test_nonjob_backfill_honors_explicit_failed_status(self):
        args = argparse.Namespace(embed_cmd="backfill", kind=None, limit=None, dry_run=False, profile=None)
        with mock.patch("khipu.embed.backfill", return_value={"ok": False, "embedded": 0, "failed_chunks": 0}), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_embed(args), 1)

    def test_library_and_outbox_failures_propagate_without_changing_json(self):
        library_args = argparse.Namespace(library_cmd="backfill", name="lib", limit=None, stale=False, profile=None)
        conn = mock.MagicMock()
        with mock.patch("khipu.library.validate_name"), \
                mock.patch("khipu.db.connect", return_value=conn), \
                mock.patch("khipu.library.backfill", return_value={"embedded": 2, "failed": 1, "detail": "kept"}), \
                redirect_stdout(io.StringIO()) as library_out:
            self.assertEqual(cli.cmd_library(library_args), 1)
        self.assertEqual(json.loads(library_out.getvalue())["detail"], "kept")
        outbox_args = argparse.Namespace(outbox_cmd="drain", limit=None)
        with mock.patch("khipu.outbox.drain", return_value={"replayed": 2, "failed": 1}), \
                mock.patch("khipu.outbox.status", return_value={"pending": 3}), \
                redirect_stdout(io.StringIO()) as outbox_out:
            self.assertEqual(cli.cmd_outbox(outbox_args), 1)
        self.assertEqual(json.loads(outbox_out.getvalue())["remaining"], 3)


class DoctorReadOnlyAndProbeTest(unittest.TestCase):
    def setUp(self):
        for patch in _green_doctor_patches():
            patch.start()
            self.addCleanup(patch.stop)

    def test_plain_doctor_does_not_refresh_or_drain(self):
        args = cli.build_parser().parse_args(["doctor"])
        with mock.patch("khipu.hub_snapshot.maybe_refresh") as refresh, \
                mock.patch("khipu.outbox.status", return_value={"pending": 1}), \
                mock.patch("khipu.outbox.drain") as outbox_drain, \
                mock.patch("khipu.session_capture.queued_jobs", return_value=True), \
                mock.patch("khipu.session_capture.drain") as capture_drain, \
                mock.patch("khipu.probe.status", return_value={"ok": True}), \
                mock.patch("khipu.drift.recall_quality", return_value={}), \
                redirect_stdout(io.StringIO()):
            cli.cmd_doctor(args)
        refresh.assert_not_called()
        outbox_drain.assert_not_called()
        capture_drain.assert_not_called()

    def test_probe_uses_its_current_failure_over_a_stale_success_receipt(self):
        args = cli.build_parser().parse_args(["doctor", "--probe"])
        result = {"ok": False, "reason": "search timeout"}
        with mock.patch("khipu.probe.run_probe", return_value=result), \
                mock.patch("khipu.probe.status", return_value={"ok": True}), \
                mock.patch("khipu.drift.recall_quality", return_value={}), \
                redirect_stdout(io.StringIO()) as out:
            cli.cmd_doctor(args)
        self.assertFalse(json.loads(out.getvalue())["recall_probe_ok"])

    def test_probe_exception_is_a_current_failed_envelope_not_a_stale_success(self):
        args = cli.build_parser().parse_args(["doctor", "--probe"])
        with mock.patch("khipu.probe.run_probe", side_effect=RuntimeError("fresh failure")), \
                mock.patch("khipu.probe.status", return_value={"ok": True, "last_probe": {"seconds": 9}}) as status, \
                mock.patch("khipu.drift.recall_quality", return_value={}), \
                redirect_stdout(io.StringIO()) as out:
            rc = cli.cmd_doctor(args)
        payload = json.loads(out.getvalue())
        self.assertEqual(rc, 2)
        self.assertFalse(payload["recall_probe"]["ok"])
        status.assert_not_called()

    def test_doctor_uses_detailed_chunk_coverage(self):
        args = cli.build_parser().parse_args(["doctor"])
        for missing, stale in ((1, 0), (0, 1)):
            coverage = {
                "episodes": {"missing": 0}, "topics": {"missing": 0},
                "chunks": 2, "missing": missing, "stale": stale,
            }
            with self.subTest(missing=missing, stale=stale), \
                    mock.patch("khipu.embed.coverage", return_value=coverage) as covered, \
                    mock.patch("khipu.probe.status", return_value={"ok": True}), \
                    mock.patch("khipu.drift.recall_quality", return_value={}), \
                    redirect_stdout(io.StringIO()) as out:
                cli.cmd_doctor(args)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["embed_coverage"], coverage)
            self.assertFalse(payload["embed_coverage_ok"])
            covered.assert_called_once_with(detail=True)

    def test_known_scheduled_job_failure_fails_doctor(self):
        args = cli.build_parser().parse_args(["doctor"])
        jobs = {name: {"last_exit": 0, "next_schedule": "daily"} for name in cli._JOBS_CHOICES}
        jobs["nightly"]["last_exit"] = 1
        with mock.patch("khipu.jobs.job_status", return_value=jobs), \
                mock.patch("khipu.probe.status", return_value={"ok": True}), \
                mock.patch("khipu.drift.recall_quality", return_value={}), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.cmd_doctor(args), 2)
        self.assertFalse(json.loads(out.getvalue())["jobs_ok"])

    def test_malformed_secrets_status_becomes_a_failed_doctor_payload(self):
        args = cli.build_parser().parse_args(["doctor"])
        with mock.patch("khipu.keychain.secrets_status", side_effect=UnicodeDecodeError("utf-8", b"x", 0, 1, "bad")), \
                mock.patch("khipu.probe.status", return_value={"ok": True}), \
                mock.patch("khipu.drift.recall_quality", return_value={}), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.cmd_doctor(args), 2)
        self.assertFalse(json.loads(out.getvalue())["dsn_file_ok"])


class NumericGraphTombstoneTest(unittest.TestCase):
    class Cursor:
        def __init__(self):
            self.result = []

        def execute(self, sql, params=None):
            if "information_schema.columns" in sql:
                self.result = [("deleted_at",)]
            elif "FROM episodes" in sql:
                self.result = [] if "deleted_at IS NULL" in sql else [(["private-topic"], "private path /secret")]
            else:
                self.result = []

        def fetchone(self):
            return self.result[0] if self.result else None

        def fetchall(self):
            return list(self.result)

    def test_numeric_graph_query_never_reads_tombstoned_episode_metadata(self):
        out = cli._graph_query(self.Cursor(), "42", 1, 10)
        self.assertTrue(out["episode"]["missing"])
        self.assertEqual(out["edges"], [])


class IntegrationOutcomeTest(unittest.TestCase):
    def _args(self, command, *, no_verify=False):
        return argparse.Namespace(integ_cmd=command, harness="codex", project=None, home=None,
                                  dry_run=False, no_verify=no_verify)

    def test_action_failure_wins_before_optional_verify_and_on_no_verify_or_uninstall(self):
        failed = [{"ok": False, "aborted": False}]
        with mock.patch("khipu.integrations.install", return_value=failed[0]), \
                mock.patch("khipu.integrations.verify", return_value={"ok": True, "detected": True}), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_integrations(self._args("install")), 1)
        with mock.patch("khipu.integrations.install", return_value=failed[0]), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_integrations(self._args("install", no_verify=True)), 1)
        with mock.patch("khipu.integrations.uninstall", return_value={"ok": True, "aborted": True}), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_integrations(self._args("uninstall")), 1)


class RecallAndForgetGateTest(unittest.TestCase):
    def test_compare_rejects_reordered_results(self):
        args = argparse.Namespace(recall_cmd="eval", golden=None, path="explicit", record=None, compare="control.json",
                                  replay=None, allow_changes=False, budget_ms=None, rerank=None, relevance_floor=None,
                                  sample=None, seed=0)
        result = {"changed": 0, "diffs": [{"status": "reordered", "path": "explicit", "query": "q",
                                               "control_reason": "ok", "live_reason": "ok"}]}
        with mock.patch("khipu.recall_eval.load_golden", return_value=[{"query": "q"}]), \
                mock.patch("khipu.recall_eval.read_record", return_value={"entries": []}), \
                mock.patch("khipu.recall_eval.compare_record", return_value=result), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_recall(args), 1)

    def test_compare_rejects_changed_reason_with_identical_rank(self):
        args = argparse.Namespace(recall_cmd="eval", golden=None, path="explicit", record=None, compare="control.json",
                                  replay=None, allow_changes=False, budget_ms=None, rerank=None, relevance_floor=None,
                                  sample=None, seed=0)
        result = {"changed": 0, "diffs": [{"status": "identical", "path": "explicit", "query": "q",
                                               "control_reason": "ok", "live_reason": "degraded"}]}
        with mock.patch("khipu.recall_eval.load_golden", return_value=[{"query": "q"}]), \
                mock.patch("khipu.recall_eval.read_record", return_value={"entries": []}), \
                mock.patch("khipu.recall_eval.compare_record", return_value=result), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_recall(args), 1)

    def test_relevance_requires_applicable_entries_and_expected_positive_hits(self):
        args = argparse.Namespace(recall_cmd="eval", golden=None, path="explicit", record=None, compare=None,
                                  replay=None, allow_changes=False, budget_ms=None, rerank=None, relevance_floor="on")
        report = {"paths": {"explicit": {"entries": 1, "abstain_total": 0, "abstain_correct": 0,
                                             "positives": 1, "positives_found": 0, "emptied_positives": []}},
                  "rows": [{"expect_none": False, "hit_with": False, "got_with": ["unrelated"],
                            "hit_without": True, "got_without": ["expected"]}]}
        with mock.patch("khipu.recall_eval.load_golden", return_value=[{"query": "q"}]), \
                mock.patch("khipu.recall_eval.run_relevance_eval", return_value=report), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_recall(args), 1)

    def test_relevance_rejects_zero_applicable_entries_and_empty_paths(self):
        args = argparse.Namespace(recall_cmd="eval", golden=None, path="explicit", record=None, compare=None,
                                  replay=None, allow_changes=False, budget_ms=None, rerank=None, relevance_floor="off")
        for report in ({"paths": {"explicit": {"entries": 0, "abstain_total": 0, "abstain_correct": 0}}, "rows": []},
                       {"paths": {}, "rows": []}):
            with self.subTest(report=report), \
                    mock.patch("khipu.recall_eval.load_golden", return_value=[{"query": "q"}]), \
                    mock.patch("khipu.recall_eval.run_relevance_eval", return_value=report), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(cli.cmd_recall(args), 1)

    def test_relevance_keeps_a_baseline_miss_outside_floor_regression(self):
        args = argparse.Namespace(recall_cmd="eval", golden=None, path="explicit", record=None, compare=None,
                                  replay=None, allow_changes=False, budget_ms=None, rerank=None, relevance_floor="on")
        report = {"paths": {"explicit": {"entries": 1, "abstain_total": 0, "abstain_correct": 0,
                                             "positives": 1, "positives_found": 0, "emptied_positives": []}},
                  "rows": [{"expect_none": False, "hit_without": False, "hit_with": False}]}
        with mock.patch("khipu.recall_eval.load_golden", return_value=[{"query": "q"}]), \
                mock.patch("khipu.recall_eval.run_relevance_eval", return_value=report), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_recall(args), 0)

    def test_forget_fails_when_snapshot_cleanup_is_partial(self):
        args = argparse.Namespace(episode_cmd="forget", id=7)
        with mock.patch("khipu.forget.forget_everywhere", return_value={"ok": True, "snapshot": {"ok": False}}), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_episode(args), 1)

    def test_forget_fails_when_legacy_cleanup_is_unreadable(self):
        args = argparse.Namespace(episode_cmd="forget", id=7)
        with mock.patch("khipu.forget.forget_everywhere", return_value={"ok": True, "legacy_file": {"reason": "unreadable: bad bytes"}}), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_episode(args), 1)


class PreflightHelpTest(unittest.TestCase):
    def test_preflight_help_discloses_remote_mutation(self):
        out = io.StringIO()
        with self.assertRaises(SystemExit), redirect_stdout(out):
            cli.main(["db", "preflight", "--help"])
        self.assertIn("may modify remote schema/privileges", out.getvalue())
