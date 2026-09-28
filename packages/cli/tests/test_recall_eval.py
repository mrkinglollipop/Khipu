# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""Tests for khipu.recall_eval (W6.3 + Phase 1 session A) — golden-query
hit@k scoring across every query-driven recall path. khipu.embed.hybrid_search
and khipu.recall_prompt.prior_work_for_prompt are mocked throughout; no live
database, no real search."""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from khipu import recall_eval


class LoadGoldenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "golden.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_parses_valid_lines_skips_blanks_and_comments(self):
        self.path.write_text(
            '# a comment\n'
            '\n'
            '{"query": "a", "expect": ["1"], "k": 3, "note": "n"}\n'
            '{"query": "b", "expect": ["2"]}\n'
        )
        entries = recall_eval.load_golden(self.path)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["query"], "a")
        self.assertEqual(entries[1]["query"], "b")

    def test_invalid_json_raises_with_line_number(self):
        self.path.write_text('{"query": "a", "expect": ["1"]}\nnot json\n')
        with self.assertRaises(ValueError) as ctx:
            recall_eval.load_golden(self.path)
        self.assertIn(":2:", str(ctx.exception))

    def test_missing_required_fields_raises(self):
        self.path.write_text('{"query": "a"}\n')
        with self.assertRaises(ValueError):
            recall_eval.load_golden(self.path)

    # ---- Phase 1 session A: extended entry schema --------------------------

    def test_expect_none_entry_is_valid_without_expect(self):
        self.path.write_text('{"query": "a", "expect_none": true}\n')
        entries = recall_eval.load_golden(self.path)
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["expect_none"])

    def test_entry_with_neither_expect_nor_expect_none_raises(self):
        self.path.write_text('{"query": "a", "note": "no expectation at all"}\n')
        with self.assertRaises(ValueError):
            recall_eval.load_golden(self.path)

    def test_optional_keys_pass_through_unvalidated(self):
        self.path.write_text(
            '{"query": "a", "expect": ["1"], "stale": ["2"], "project": "khipu", '
            '"cwd": "/tmp/x", "since": "7d", "until": "1d", "paths": ["prompt", "status"]}\n'
        )
        entries = recall_eval.load_golden(self.path)
        self.assertEqual(entries[0]["paths"], ["prompt", "status"])
        self.assertEqual(entries[0]["stale"], ["2"])
        self.assertEqual(entries[0]["project"], "khipu")


class EvalOneTest(unittest.TestCase):
    def test_hit_when_expected_id_in_top_k(self):
        entry = {"query": "q", "expect": ["42"], "k": 3}
        results = {"results": [
            {"kind": "episode", "id": "1", "score": 0.5},
            {"kind": "episode", "id": "42", "score": 0.4},
            {"kind": "topic", "id": "some-topic", "score": 0.9},
        ]}
        with mock.patch("khipu.embed.hybrid_search", return_value=results) as m:
            row = recall_eval.eval_one(entry)
        self.assertTrue(row["hit"])
        # Every kind is scored (audit 2026-09-04): the top-k slice is what the
        # caller actually sees, so dropping non-episode rows from `got` both
        # made a topic-slug golden line unhittable and mis-reported the slice.
        self.assertEqual(row["got"], ["1", "42", "some-topic"])
        m.assert_called_once_with("q", mode="hybrid", limit=3)

    def test_a_topic_slug_can_be_a_golden_expectation(self):
        entry = {"query": "q", "expect": ["some-topic"], "k": 3}
        results = {"results": [
            {"kind": "episode", "id": "1", "score": 0.5},
            {"kind": "topic", "id": "some-topic", "score": 0.4},
        ]}
        with mock.patch("khipu.embed.hybrid_search", return_value=results):
            row = recall_eval.eval_one(entry)
        self.assertTrue(row["hit"], row)
        self.assertEqual(row["got"], ["1", "some-topic"])

    def test_miss_when_expected_id_absent(self):
        entry = {"query": "q", "expect": ["999"], "k": 3}
        results = {"results": [{"kind": "episode", "id": "1", "score": 0.5}]}
        with mock.patch("khipu.embed.hybrid_search", return_value=results):
            row = recall_eval.eval_one(entry)
        self.assertFalse(row["hit"])

    def test_search_failure_is_a_miss_not_a_crash(self):
        entry = {"query": "q", "expect": ["1"], "k": 3}
        with mock.patch("khipu.embed.hybrid_search", side_effect=RuntimeError("hub down")):
            row = recall_eval.eval_one(entry)
        self.assertFalse(row["hit"])
        self.assertIn("hub down", row["error"])

    def test_custom_mode_and_k_are_forwarded(self):
        entry = {"query": "q", "mode": "semantic", "expect": ["1"], "k": 5}
        with mock.patch("khipu.embed.hybrid_search", return_value={"results": []}) as m:
            recall_eval.eval_one(entry)
        m.assert_called_once_with("q", mode="semantic", limit=5)


class RunEvalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "golden.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, entries):
        with self.path.open("w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")

    def test_overall_hit_rate(self):
        self._write([
            {"query": "hit-one", "expect": ["1"], "k": 3},
            {"query": "hit-two", "expect": ["2"], "k": 3},
            {"query": "miss-one", "expect": ["999"], "k": 3},
            {"query": "miss-two", "expect": ["998"], "k": 3},
        ])

        def fake_search(query, *, mode="hybrid", limit=3):
            mapping = {
                "hit-one": ["1"], "hit-two": ["2"],
                "miss-one": ["1"], "miss-two": ["1"],
            }
            return {"results": [{"kind": "episode", "id": i, "score": 1.0}
                                 for i in mapping[query]]}

        with mock.patch("khipu.embed.hybrid_search", side_effect=fake_search):
            report = recall_eval.run_eval(self.path)
        self.assertEqual(report["total"], 4)
        self.assertEqual(report["hits"], 2)
        self.assertEqual(report["overall_hit_rate"], 0.5)
        self.assertEqual(len(report["rows"]), 4)

    def test_empty_golden_file_is_zero_not_a_crash(self):
        self.path.write_text("")
        report = recall_eval.run_eval(self.path)
        self.assertEqual(report["total"], 0)
        self.assertEqual(report["overall_hit_rate"], 0.0)

    def test_a_hit_with_none_confidence_is_flagged(self):
        """R4: a golden entry is a known positive by definition — if it
        hits but still reads confidence='none', the thresholds are
        miscalibrated, and this is the one place that gets checked."""
        self._write([{"query": "flagged", "expect": ["1"], "k": 3}])
        with mock.patch(
            "khipu.embed.hybrid_search",
            return_value={"results": [{"kind": "episode", "id": "1", "score": 1.0}], "confidence": "none"},
        ):
            report = recall_eval.run_eval(self.path)
        self.assertEqual(report["hit_none_confidence"], ["flagged"])

    def test_a_hit_with_real_confidence_is_not_flagged(self):
        self._write([{"query": "ok", "expect": ["1"], "k": 3}])
        with mock.patch(
            "khipu.embed.hybrid_search",
            return_value={"results": [{"kind": "episode", "id": "1", "score": 1.0}], "confidence": "strong"},
        ):
            report = recall_eval.run_eval(self.path)
        self.assertEqual(report["hit_none_confidence"], [])

    def test_a_miss_with_none_confidence_is_not_flagged(self):
        """confidence='none' on an actual MISS is the correct, honest
        answer — only a hit reading none is the miscalibration this flags."""
        self._write([{"query": "gibberish", "expect": ["999"], "k": 3}])
        with mock.patch(
            "khipu.embed.hybrid_search",
            return_value={"results": [{"kind": "episode", "id": "1", "score": 1.0}], "confidence": "none"},
        ):
            report = recall_eval.run_eval(self.path)
        self.assertEqual(report["hit_none_confidence"], [])


class LegacyOutputPinTest(unittest.TestCase):
    """Phase 1 session A must not change a single byte of the original W6.3
    surface: run_eval/eval_one/load_golden are untouched. This pins the
    exact report shape so a future edit here trips a test, not a
    maintainer's memory of what the output used to look like."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "golden.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_flag_output_shape_is_pinned(self):
        self.path.write_text('{"query": "pinned", "expect": ["1"], "k": 3, "note": "n"}\n')
        results = {"results": [{"kind": "episode", "id": "1", "score": 0.9}], "confidence": "strong"}
        with mock.patch("khipu.embed.hybrid_search", return_value=results):
            report = recall_eval.run_eval(self.path)
        self.assertEqual(report, {
            "path": str(self.path),
            "total": 1,
            "hits": 1,
            "overall_hit_rate": 1.0,
            "hit_none_confidence": [],
            "rows": [{
                "query": "pinned", "mode": "hybrid", "k": 3, "expect": ["1"],
                "note": "n", "got": ["1"], "hit": True, "confidence": "strong",
            }],
        })

    def test_no_new_flags_prints_exactly_the_legacy_report_through_cmd_recall(self):
        """Same guarantee, exercised through cmd_recall's real dispatch
        (an argparse.Namespace shaped exactly like `parser.parse_args(
        ["recall", "eval"])` would produce) instead of calling run_eval
        directly — the legacy fast path must still be the one that runs."""
        import argparse
        import io
        from contextlib import redirect_stdout

        from khipu import cli

        report = {
            "path": "x", "total": 1, "hits": 1, "overall_hit_rate": 1.0,
            "hit_none_confidence": [],
            "rows": [{"query": "q", "hit": True, "got": ["1"], "expect": ["1"]}],
        }
        ns = argparse.Namespace(
            recall_cmd="eval", golden=None, path="explicit", budget_ms=None,
            record=None, compare=None, allow_changes=False, replay=None,
            sample=None, seed=0,
        )
        with mock.patch("khipu.recall_eval.run_eval", return_value=report) as m:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.cmd_recall(ns)
        m.assert_called_once_with(None)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue()), report)


class EvalOnePathTest(unittest.TestCase):
    """eval_one_path against each of the three retrieval paths."""

    def test_explicit_path_hits_like_eval_one(self):
        entry = {"query": "q", "expect": ["42"], "k": 3}
        results = {"results": [{"kind": "episode", "id": "42", "score": 0.5}], "confidence": "strong"}
        with mock.patch("khipu.embed.hybrid_search", return_value=results) as m:
            row = recall_eval.eval_one_path(entry, "explicit")
        self.assertTrue(row["hit"])
        self.assertEqual(row["reciprocal_rank"], 1.0)
        self.assertEqual(row["confidence"], "strong")
        m.assert_called_once_with("q", mode="hybrid", limit=3, project=None, since=None, until=None)

    def test_explicit_path_forwards_project_since_until(self):
        entry = {"query": "q", "expect": ["1"], "project": "khipu", "since": "7d", "until": "1d"}
        with mock.patch("khipu.embed.hybrid_search", return_value={"results": []}) as m:
            recall_eval.eval_one_path(entry, "explicit")
        m.assert_called_once_with("q", mode="hybrid", limit=3, project="khipu", since="7d", until="1d")

    def test_prompt_path_hits_from_hits_key(self):
        entry = {"query": "q", "expect": ["7"], "k": 3}
        payload = {"context": "...", "hits": [{"kind": "episode", "id": "7"}], "reason": "ok", "ms": 12.0}
        with mock.patch("khipu.recall_prompt.prior_work_for_prompt", return_value=payload) as m:
            row = recall_eval.eval_one_path(entry, "prompt")
        self.assertTrue(row["hit"])
        self.assertIsNone(row["confidence"])
        m.assert_called_once_with("q", cwd=None, session_id=None, budget_ms=None)

    def test_status_path_passes_budget_ms(self):
        entry = {"query": "q", "expect": ["7"]}
        payload = {
            "context": "", "hits": [], "reason": "ok", "ms": 1.0,
            "prior_work_meta": {"legs": [], "ms": 1.0, "degraded": None, "reason": "ok"},
        }
        with mock.patch("khipu.recall_prompt.prior_work_for_prompt", return_value=payload) as m:
            recall_eval.eval_one_path(entry, "status", budget_ms=600)
        m.assert_called_once_with("q", cwd=None, session_id=None, budget_ms=600)

    def test_prompt_and_status_k_is_capped_at_top_n(self):
        entry = {"query": "q", "expect": ["1"], "k": 50}
        payload = {
            "context": "", "reason": "ok", "ms": 1.0,
            "hits": [{"kind": "episode", "id": str(i)} for i in range(5)],
        }
        with mock.patch("khipu.recall_prompt.prior_work_for_prompt", return_value=payload):
            row = recall_eval.eval_one_path(entry, "prompt")
        self.assertLessEqual(row["k"], 3)
        self.assertLessEqual(len(row["got"]), 3)

    def test_entry_cwd_is_forwarded_to_prompt_path(self):
        entry = {"query": "q", "expect": ["1"], "cwd": "/repo"}
        with mock.patch(
            "khipu.recall_prompt.prior_work_for_prompt", return_value={"hits": [], "reason": "ok"}
        ) as m:
            recall_eval.eval_one_path(entry, "prompt")
        m.assert_called_once_with("q", cwd="/repo", session_id=None, budget_ms=None)

    def test_mrr_rewards_earlier_rank(self):
        entry = {"query": "q", "expect": ["2"], "k": 3}
        results = {"results": [{"kind": "episode", "id": "1"}, {"kind": "episode", "id": "2"}]}
        with mock.patch("khipu.embed.hybrid_search", return_value=results):
            row = recall_eval.eval_one_path(entry, "explicit")
        self.assertEqual(row["reciprocal_rank"], 0.5)
        summary = recall_eval.summarize_rows([row])
        self.assertEqual(summary["mrr"], 0.5)


class AbstentionAndStaleTest(unittest.TestCase):
    def test_abstention_correct_when_no_results(self):
        entry = {"query": "gibberish", "expect_none": True}
        with mock.patch("khipu.embed.hybrid_search", return_value={"results": []}):
            row = recall_eval.eval_one_path(entry, "explicit")
        self.assertTrue(row["abstain_correct"])
        self.assertFalse(row["hit"])

    def test_abstention_incorrect_when_results_come_back(self):
        entry = {"query": "gibberish", "expect_none": True}
        with mock.patch(
            "khipu.embed.hybrid_search",
            return_value={"results": [{"kind": "episode", "id": "1"}]},
        ):
            row = recall_eval.eval_one_path(entry, "explicit")
        self.assertFalse(row["abstain_correct"])

    def test_abstention_on_prompt_timeout_is_not_a_correct_abstention(self):
        """B10's exact failure shape: an empty result from a MISSED DEADLINE
        must never score as a correct abstention."""
        entry = {"query": "q", "expect_none": True}
        payload = {"context": "", "hits": [], "reason": "timeout>0.6s", "ms": 601.0}
        with mock.patch("khipu.recall_prompt.prior_work_for_prompt", return_value=payload):
            row = recall_eval.eval_one_path(entry, "status", budget_ms=600)
        self.assertTrue(row["timeout"])
        self.assertFalse(row["abstain_correct"])

    def test_stale_id_in_top_k_is_a_violation(self):
        entry = {"query": "q", "expect": ["1"], "stale": ["2"]}
        results = {"results": [{"kind": "episode", "id": "1"}, {"kind": "episode", "id": "2"}]}
        with mock.patch("khipu.embed.hybrid_search", return_value=results):
            row = recall_eval.eval_one_path(entry, "explicit")
        self.assertTrue(row["stale_violation"])

    def test_no_stale_id_present_is_not_a_violation(self):
        entry = {"query": "q", "expect": ["1"], "stale": ["999"]}
        with mock.patch(
            "khipu.embed.hybrid_search",
            return_value={"results": [{"kind": "episode", "id": "1"}]},
        ):
            row = recall_eval.eval_one_path(entry, "explicit")
        self.assertFalse(row["stale_violation"])


class TimeoutAndErrorAccountingTest(unittest.TestCase):
    def test_explicit_error_is_a_miss_and_counted(self):
        entry = {"query": "q", "expect": ["1"]}
        with mock.patch("khipu.embed.hybrid_search", side_effect=RuntimeError("hub down")):
            row = recall_eval.eval_one_path(entry, "explicit")
        self.assertFalse(row["hit"])
        self.assertTrue(row["error"])

    def test_prompt_timeout_reason_sets_timeout_flag_not_error(self):
        entry = {"query": "q", "expect": ["1"]}
        payload = {"hits": [], "reason": "timeout>1.2s", "ms": 1200.0}
        with mock.patch("khipu.recall_prompt.prior_work_for_prompt", return_value=payload):
            row = recall_eval.eval_one_path(entry, "prompt")
        self.assertTrue(row["timeout"])
        self.assertFalse(row["error"])
        self.assertFalse(row["hit"])

    def test_prompt_gate_error_reason_sets_error_flag(self):
        entry = {"query": "q", "expect": ["1"]}
        payload = {"hits": [], "reason": "gate error: boom", "ms": 0.0}
        with mock.patch("khipu.recall_prompt.prior_work_for_prompt", return_value=payload):
            row = recall_eval.eval_one_path(entry, "prompt")
        self.assertTrue(row["error"])
        self.assertFalse(row["hit"])

    def test_positive_entry_timeout_is_also_counted_in_summary(self):
        """A lane that misses its own deadline must be BOTH a miss (hits=0)
        AND visible in its own column (timeouts=1) — never just silence."""
        payload = {"hits": [], "reason": "timeout>0.6s", "ms": 601.0}
        with mock.patch("khipu.recall_prompt.prior_work_for_prompt", return_value=payload):
            rows = [recall_eval.eval_one_path({"query": "q", "expect": ["1"]}, "status", budget_ms=600)]
        summary = recall_eval.summarize_rows(rows)
        self.assertEqual(summary["timeouts"], 1)
        self.assertEqual(summary["hits"], 0)

    def test_status_degraded_flag_is_read_from_prior_work_meta(self):
        payload = {
            "hits": [], "reason": "ok", "ms": 5.0,
            "prior_work_meta": {"legs": ["lexical"], "ms": 5.0, "degraded": "timeout", "reason": "ok"},
        }
        with mock.patch("khipu.recall_prompt.prior_work_for_prompt", return_value=payload):
            row = recall_eval.eval_one_path({"query": "q", "expect": ["1"]}, "status", budget_ms=600)
        self.assertEqual(row["degraded"], "timeout")

    def test_explicit_degraded_flag_is_read_from_payload(self):
        results = {"results": [], "degraded": "no-embedding"}
        with mock.patch("khipu.embed.hybrid_search", return_value=results):
            row = recall_eval.eval_one_path({"query": "q", "expect": ["1"]}, "explicit")
        self.assertEqual(row["degraded"], "no-embedding")


class PercentileTest(unittest.TestCase):
    def test_empty_is_zero(self):
        self.assertEqual(recall_eval._percentile([], 50), 0.0)

    def test_p50_and_p95_nearest_rank(self):
        values = [10.0, 20.0, 30.0, 40.0]
        self.assertEqual(recall_eval._percentile(values, 50), 20.0)
        self.assertEqual(recall_eval._percentile(values, 95), 40.0)

    def test_summarize_rows_reports_latency_and_payload_size(self):
        entries = [{"query": f"q{i}", "expect": ["1"]} for i in range(2)]
        with mock.patch(
            "khipu.embed.hybrid_search",
            return_value={"results": [{"kind": "episode", "id": "1"}]},
        ):
            rows = [recall_eval.eval_one_path(e, "explicit") for e in entries]
        summary = recall_eval.summarize_rows(rows)
        self.assertIn("latency_p50_ms", summary)
        self.assertIn("latency_p95_ms", summary)
        self.assertGreater(summary["mean_payload_chars"], 0)


class RunEvalPathsTest(unittest.TestCase):
    def test_entry_paths_restriction_limits_which_paths_run(self):
        entry = {"query": "q", "expect": ["1"], "paths": ["explicit"]}
        with mock.patch(
            "khipu.embed.hybrid_search",
            return_value={"results": [{"kind": "episode", "id": "1"}]},
        ) as m_explicit, mock.patch("khipu.recall_prompt.prior_work_for_prompt") as m_prompt:
            report = recall_eval.run_eval_paths([entry], recall_eval.ALL_PATHS)
        m_explicit.assert_called_once()
        m_prompt.assert_not_called()
        self.assertEqual(report["paths"]["prompt"]["total"], 0)
        self.assertEqual(report["paths"]["explicit"]["total"], 1)

    def test_all_paths_run_when_entry_has_no_restriction(self):
        entry = {"query": "q", "expect": ["1"]}
        with mock.patch("khipu.embed.hybrid_search", return_value={"results": []}), mock.patch(
            "khipu.recall_prompt.prior_work_for_prompt", return_value={"hits": [], "reason": "ok"}
        ):
            report = recall_eval.run_eval_paths([entry], recall_eval.ALL_PATHS)
        for p in recall_eval.ALL_PATHS:
            self.assertEqual(report["paths"][p]["total"], 1)


class RecordCompareTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ctrl_path = pathlib.Path(self.tmp.name) / "control.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_then_compare_identical(self):
        entries = [{"query": "q", "expect": ["1"]}]
        results = {"results": [{"kind": "episode", "id": "1"}]}
        with mock.patch("khipu.embed.hybrid_search", return_value=results):
            record = recall_eval.build_record(entries, ("explicit",))
        recall_eval.write_record(self.ctrl_path, record)
        control = recall_eval.read_record(self.ctrl_path)
        with mock.patch("khipu.embed.hybrid_search", return_value=results):
            result = recall_eval.compare_record(entries, ("explicit",), control)
        self.assertEqual(result["changed"], 0)
        self.assertEqual(result["diffs"][0]["status"], "identical")

    def test_compare_reordered_is_flagged_but_not_counted_as_changed(self):
        entries = [{"query": "q", "expect": ["1", "2"]}]
        first = {"results": [{"kind": "episode", "id": "1"}, {"kind": "episode", "id": "2"}]}
        second = {"results": [{"kind": "episode", "id": "2"}, {"kind": "episode", "id": "1"}]}
        with mock.patch("khipu.embed.hybrid_search", return_value=first):
            record = recall_eval.build_record(entries, ("explicit",))
        with mock.patch("khipu.embed.hybrid_search", return_value=second):
            result = recall_eval.compare_record(entries, ("explicit",), record)
        self.assertEqual(result["diffs"][0]["status"], "reordered")
        self.assertEqual(result["changed"], 0)

    def test_compare_changed_ids_is_flagged_and_counted(self):
        entries = [{"query": "q", "expect": ["1"]}]
        first = {"results": [{"kind": "episode", "id": "1"}]}
        second = {"results": [{"kind": "episode", "id": "9"}]}
        with mock.patch("khipu.embed.hybrid_search", return_value=first):
            record = recall_eval.build_record(entries, ("explicit",))
        with mock.patch("khipu.embed.hybrid_search", return_value=second):
            result = recall_eval.compare_record(entries, ("explicit",), record)
        self.assertEqual(result["diffs"][0]["status"], "changed")
        self.assertEqual(result["changed"], 1)

    def test_compare_pair_missing_from_control_is_new(self):
        with mock.patch(
            "khipu.embed.hybrid_search",
            return_value={"results": [{"kind": "episode", "id": "1"}]},
        ):
            result = recall_eval.compare_record(
                [{"query": "unseen", "expect": ["1"]}], ("explicit",), {"entries": []}
            )
        self.assertEqual(result["diffs"][0]["status"], "new")
        self.assertEqual(result["changed"], 1)

    def test_compare_ignores_latency_differences(self):
        entries = [{"query": "q", "expect": ["1"]}]
        results = {"results": [{"kind": "episode", "id": "1"}]}
        with mock.patch("khipu.embed.hybrid_search", return_value=results):
            record = recall_eval.build_record(entries, ("explicit",))
            result = recall_eval.compare_record(entries, ("explicit",), record)
        self.assertEqual(result["diffs"][0]["status"], "identical")

    def test_record_includes_stamp_with_khipu_version_and_paths(self):
        with mock.patch("khipu.embed.hybrid_search", return_value={"results": []}):
            record = recall_eval.build_record([{"query": "q", "expect_none": True}], ("explicit",))
        self.assertIn("khipu_version", record["stamp"])
        self.assertEqual(record["stamp"]["paths"], ["explicit"])
        self.assertIn("utc", record["stamp"])

    def test_read_record_rejects_a_non_control_file(self):
        self.ctrl_path.write_text(json.dumps({"not": "a control"}))
        with self.assertRaises(ValueError):
            recall_eval.read_record(self.ctrl_path)


class ReplayEntriesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_path = pathlib.Path(self.tmp.name) / "query_log.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, lines):
        with self.log_path.open("w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line) + "\n")

    def test_skips_redacted_and_missing_queries(self):
        self._write([
            {"query": None, "mode": "hybrid"},
            {"mode": "hybrid"},
            {"query": "real one", "mode": "hybrid"},
        ])
        entries = recall_eval.load_replay_entries(self.log_path)
        self.assertEqual([e["query"] for e in entries], ["real one"])

    def test_skips_slice_pseudo_queries(self):
        self._write([{"query": "slice:khipu", "mode": "slice"}, {"query": "real", "mode": "hybrid"}])
        entries = recall_eval.load_replay_entries(self.log_path)
        self.assertEqual([e["query"] for e in entries], ["real"])

    def test_deduplicates_identical_queries(self):
        self._write([{"query": "dup", "mode": "hybrid"}, {"query": "dup", "mode": "semantic"}])
        entries = recall_eval.load_replay_entries(self.log_path)
        self.assertEqual(len(entries), 1)

    def test_sampling_is_deterministic_for_a_fixed_seed(self):
        self._write([{"query": f"q{i}", "mode": "hybrid"} for i in range(20)])
        first = recall_eval.load_replay_entries(self.log_path, sample=5, seed=7)
        second = recall_eval.load_replay_entries(self.log_path, sample=5, seed=7)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 5)

    def test_different_seeds_can_sample_differently(self):
        self._write([{"query": f"q{i}", "mode": "hybrid"} for i in range(20)])
        a = recall_eval.load_replay_entries(self.log_path, sample=5, seed=1)
        b = recall_eval.load_replay_entries(self.log_path, sample=5, seed=2)
        self.assertNotEqual(a, b)

    def test_no_sample_returns_every_deduplicated_entry(self):
        self._write([{"query": "a"}, {"query": "b"}])
        entries = recall_eval.load_replay_entries(self.log_path)
        self.assertEqual(len(entries), 2)

    def test_replay_entries_have_no_expectation(self):
        self._write([{"query": "a", "mode": "hybrid"}])
        entries = recall_eval.load_replay_entries(self.log_path)
        self.assertNotIn("expect", entries[0])
        self.assertNotIn("expect_none", entries[0])


if __name__ == "__main__":
    unittest.main()
