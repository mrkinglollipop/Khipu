"""Focused regressions from the 2026-10-10 recall evidence audit."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import recall_eval, recall_rule, recency, relevance, search_text, topic_hits


class StaleSliceTest(unittest.TestCase):
    def test_forgotten_snapshot_row_is_excluded_before_limit(self):
        con = sqlite3.connect(":memory:")
        con.execute(
            "CREATE TABLE episodes "
            "(id TEXT, summary TEXT, raw TEXT, ingested_at TEXT, ts TEXT, deleted_at TEXT)"
        )
        con.executemany("INSERT INTO episodes VALUES (?, ?, ?, ?, ?, ?)", [
            ("forgotten", "private sentinel", '{"project":"acme"}', "2026-10-10", None, "2026-10-10"),
            ("live", "safe summary", '{"project":"acme"}', "2026-10-09", None, None),
        ])
        with mock.patch("khipu.identity.resolve_repo_root", return_value={"project": "acme", "repo_root": "/repo"}), \
                mock.patch("khipu.activity.project_slice", side_effect=RuntimeError("hub down")), \
                mock.patch("khipu.query_log.log_query"), \
                mock.patch("khipu.hub_snapshot.open_snapshot", return_value=con), \
                mock.patch("khipu.snippets.clip_snippet", side_effect=lambda s, _: s):
            out = recall_rule.session_start_context("/repo")
        self.assertNotIn("private sentinel", out)
        self.assertNotIn("episode `forgotten`", out)
        self.assertIn("safe summary", out)

    def test_more_than_limit_forgotten_rows_do_not_crowd_out_live_row(self):
        con = sqlite3.connect(":memory:")
        con.execute(
            "CREATE TABLE episodes "
            "(id TEXT, summary TEXT, raw TEXT, ingested_at TEXT, ts TEXT, deleted_at TEXT)"
        )
        con.executemany("INSERT INTO episodes VALUES (?, ?, ?, ?, ?, ?)", [
            (f"forgotten-{i}", f"private-{i}", '{"project":"acme"}',
             f"2026-10-10T{i % 24:02}:00:00", None, "2026-10-10")
            for i in range(205)
        ] + [("live", "live sentinel", '{"project":"acme"}', "2026-01-01", None, None)])
        with mock.patch("khipu.hub_snapshot.open_snapshot", return_value=con), \
                mock.patch("khipu.snippets.clip_snippet", side_effect=lambda s, _: s):
            out = recall_rule._stale_project_slice("acme", None)
        self.assertIn("live sentinel", out)
        self.assertNotIn("private-", out)

    def test_legacy_snapshot_without_deleted_at_remains_readable(self):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE episodes (id TEXT, summary TEXT, raw TEXT, ingested_at TEXT, ts TEXT)")
        con.execute("INSERT INTO episodes VALUES (?, ?, ?, ?, ?)",
                    ("legacy", "legacy summary", '{"project":"acme"}', "2026-01-01", None))
        with mock.patch("khipu.hub_snapshot.open_snapshot", return_value=con), \
                mock.patch("khipu.snippets.clip_snippet", side_effect=lambda s, _: s):
            out = recall_rule._stale_project_slice("acme", None)
        self.assertIn("legacy summary", out)

    def test_non_schema_query_failure_never_retries_unfiltered(self):
        class BrokenSnapshot:
            def __init__(self):
                self.calls = []

            def execute(self, sql):
                self.calls.append(sql)
                raise sqlite3.OperationalError("disk I/O error")

            def close(self):
                pass

        con = BrokenSnapshot()
        with mock.patch("khipu.hub_snapshot.open_snapshot", return_value=con):
            self.assertEqual(recall_rule._stale_project_slice("acme", None), "")
        self.assertEqual(len(con.calls), 1)
        self.assertIn("deleted_at IS NULL", con.calls[0])

    def test_hub_failure_does_not_retry_live_cwd_or_recents(self):
        with mock.patch("khipu.identity.resolve_repo_root", return_value={"project": "acme", "repo_root": "/repo"}), \
                mock.patch("khipu.activity.project_slice", side_effect=RuntimeError("hub down")), \
                mock.patch("khipu.hub_snapshot.open_snapshot", side_effect=OSError("missing")), \
                mock.patch("khipu.db.connect") as connect, \
                mock.patch("khipu.activity.recent_episodes") as recents:
            self.assertEqual(recall_rule._pushed_memory_slice("/repo"), "")
        connect.assert_not_called()
        recents.assert_not_called()


class FuseDedupTest(unittest.TestCase):
    def test_duplicate_votes_once_and_does_not_consume_the_next_unique_rank(self):
        a = {"kind": "episode", "id": "a", "snippet": "canonical"}
        out = search_text.fuse_ranked_lists([[a, {**a, "snippet": "chunk"},
                                              {"kind": "episode", "id": "b"}]], limit=10)
        by_id = {row["id"]: row for row in out}
        self.assertEqual(by_id["a"]["snippet"], "canonical")
        self.assertEqual(by_id["a"]["score"], round(1 / (search_text.RRF_K + 1), 6))
        self.assertEqual(by_id["b"]["score"], round(1 / (search_text.RRF_K + 2), 6))

    def test_later_evidence_maxima_survive_fusion_and_relevance_gate(self):
        cosine = {"kind": "library", "id": "doc", "snippet": "first", "cosine": 0.3,
                  "profile": "p1"}
        stronger = {"kind": "library", "id": "doc", "cosine": 0.9, "profile": "p2"}
        lexical = {"kind": "library", "id": "doc", "lexical_hits": 2}
        out = search_text.fuse_ranked_lists([[cosine, stronger], [lexical]], limit=10)
        self.assertEqual(out[0]["snippet"], "first")
        self.assertEqual((out[0]["cosine"], out[0]["profile"], out[0]["lexical_hits"]),
                         (0.9, "p2", 2))
        gated, info = relevance.gate(out, 4, floor=0.95)
        self.assertEqual([row["id"] for row in gated], ["doc"])
        self.assertFalse(info["abstained"])

    def test_cosine_winner_without_profile_clears_weaker_profile(self):
        out = search_text.fuse_ranked_lists([[
            {"kind": "library", "id": "doc", "cosine": 0.3, "profile": "weak"},
            {"kind": "library", "id": "doc", "cosine": 0.9},
            {"kind": "library", "id": "doc", "cosine": 0.4, "profile": "later"},
        ]], limit=10)
        self.assertEqual(out[0]["cosine"], 0.9)
        self.assertNotIn("profile", out[0])


class TopicCurrentnessTest(unittest.TestCase):
    def test_superseded_by_deranks_without_project_boost(self):
        rows = [
            {"kind": "topic", "id": "old", "score": 0.5, "superseded_by": "new"},
            {"kind": "topic", "id": "new", "score": 0.3},
        ]
        out = recency.apply_project_and_status(rows)
        self.assertEqual([r["id"] for r in out], ["new", "old"])


class RecallEvalContractTest(unittest.TestCase):
    def test_completed_no_match_is_a_successful_abstention(self):
        payload = {"hits": [], "reason": "ok", "legs": ["lexical"],
                   "prior_work_meta": {"outcome": "no_match", "reason": "ok",
                                       "legs": ["lexical"], "degraded": None}}
        with mock.patch("khipu.recall_prompt.prior_work_for_prompt", return_value=payload):
            row = recall_eval.eval_one_path({"query": "q", "expect_none": True}, "status")
        self.assertTrue(row["abstain_correct"])
        self.assertEqual(row["outcome"], "no_match")

    def test_degraded_no_leg_prompt_is_not_successful_abstention(self):
        payload = {"hits": [], "reason": "ok", "prior_work_meta": {"degraded": "no-leg"}}
        with mock.patch("khipu.recall_prompt.prior_work_for_prompt", return_value=payload):
            row = recall_eval.eval_one_path({"query": "q", "expect_none": True}, "status")
        self.assertFalse(row["abstain_correct"])
        self.assertEqual(row["outcome"], "no_match")

    def test_structured_timeout_wins_over_ok_reason(self):
        payload = {"hits": [], "reason": "ok", "prior_work_meta": {
            "outcome": "timeout", "reason": "ok", "legs": [], "degraded": None}}
        with mock.patch("khipu.recall_prompt.prior_work_for_prompt", return_value=payload):
            row = recall_eval.eval_one_path({"query": "q", "expect_none": True}, "status")
        self.assertTrue(row["timeout"])
        self.assertFalse(row["abstain_correct"])

    def test_gated_and_dedup_are_not_abstentions(self):
        for outcome in ("gated", "dedup"):
            payload = {"hits": [], "outcome": outcome, "reason": "ok", "legs": []}
            with self.subTest(outcome=outcome), mock.patch(
                    "khipu.recall_prompt.prior_work_for_prompt", return_value=payload):
                row = recall_eval.eval_one_path({"query": "q", "expect_none": True}, "prompt")
                self.assertFalse(row["abstain_correct"])

    def test_unknown_legacy_empty_prompt_gets_no_abstention_credit(self):
        with mock.patch("khipu.recall_prompt.prior_work_for_prompt",
                        return_value={"hits": [], "reason": "ok"}):
            row = recall_eval.eval_one_path({"query": "q", "expect_none": True}, "prompt")
        self.assertFalse(row["abstain_correct"])

    def test_malformed_structured_outcome_is_conservative_error(self):
        payload = {"hits": [], "reason": "ok", "legs": ["lexical"], "outcome": {"bad": True}}
        with mock.patch("khipu.recall_prompt.prior_work_for_prompt", return_value=payload):
            row = recall_eval.eval_one_path({"query": "q", "expect_none": True}, "prompt")
        self.assertEqual(row["outcome"], "error")
        self.assertTrue(row["error"])
        self.assertFalse(row["abstain_correct"])

    def test_prompt_forwards_project_only_when_entry_specifies_it(self):
        payload = {"hits": [], "reason": "ok"}
        with mock.patch("khipu.recall_prompt.prior_work_for_prompt", return_value=payload) as call:
            recall_eval.eval_one_path({"query": "q", "expect": ["x"], "project": "acme"}, "prompt")
        call.assert_called_once_with("q", cwd=None, session_id=None, budget_ms=None, project="acme")

    def test_prompt_preserves_top_level_degradation_and_error_outcome(self):
        payload = {"hits": [], "reason": "ok", "outcome": "error",
                   "degraded": "no-leg", "legs": []}
        with mock.patch("khipu.recall_prompt.prior_work_for_prompt", return_value=payload):
            row = recall_eval.eval_one_path({"query": "q", "expect_none": True}, "prompt")
        self.assertEqual((row["outcome"], row["degraded"], row["error"]),
                         ("error", "no-leg", True))
        self.assertFalse(row["abstain_correct"])

    def test_compare_counts_reorder(self):
        entries = [{"query": "q", "expect": ["a"]}]
        with mock.patch("khipu.embed.hybrid_search",
                        return_value={"results": [{"id": "a"}, {"id": "b"}]}):
            control = recall_eval.build_record(entries, ("explicit",))
        with mock.patch("khipu.embed.hybrid_search",
                        return_value={"results": [{"id": "b"}, {"id": "a"}]}):
            result = recall_eval.compare_record(entries, ("explicit",), control)
        self.assertEqual(result["changed"], 1)
        self.assertEqual(result["diffs"][0]["status"], "reordered")

    def test_compare_same_ids_but_timeout_is_contract_change(self):
        entries = [{"query": "q", "expect_none": True}]
        healthy = {"hits": [], "reason": "ok", "prior_work_meta": {
            "outcome": "no_match", "reason": "ok", "legs": ["lexical"], "degraded": None}}
        timeout = {"hits": [], "reason": "ok", "prior_work_meta": {
            "outcome": "timeout", "reason": "ok", "legs": [], "degraded": None}}
        with mock.patch("khipu.recall_prompt.prior_work_for_prompt", return_value=healthy):
            control = recall_eval.build_record(entries, ("status",))
        with mock.patch("khipu.recall_prompt.prior_work_for_prompt", return_value=timeout):
            result = recall_eval.compare_record(entries, ("status",), control)
        self.assertEqual((result["changed"], result["diffs"][0]["status"]), (1, "changed"))
        self.assertTrue(result["diffs"][0]["contract_changed"])

    def test_compare_filter_identity_and_exact_duplicates_do_not_overwrite(self):
        entries = [
            {"query": "q", "expect": ["a"], "project": "one"},
            {"query": "q", "expect": ["b"], "project": "two"},
            {"query": "same", "expect": ["x"]},
            {"query": "same", "expect": ["x"]},
        ]
        first = [{"results": [{"id": value}]} for value in ("a", "b", "x1", "x2")]
        with mock.patch("khipu.embed.hybrid_search", side_effect=first):
            control = recall_eval.build_record(entries, ("explicit",))
        second = [{"results": [{"id": value}]} for value in ("a", "b", "x1", "x2")]
        with mock.patch("khipu.embed.hybrid_search", side_effect=second):
            result = recall_eval.compare_record(entries, ("explicit",), control)
        self.assertEqual(result["changed"], 0)
        self.assertEqual([e["identity"]["project"] for e in control["entries"][:2]], ["one", "two"])

    def test_compare_reports_removed_and_degradation_only_changes(self):
        entries = [{"query": "keep", "expect_none": True}, {"query": "remove", "expect_none": True}]
        healthy = {"hits": [], "reason": "ok", "prior_work_meta": {
            "outcome": "no_match", "reason": "ok", "legs": ["lexical"], "degraded": None}}
        with mock.patch("khipu.recall_prompt.prior_work_for_prompt", return_value=healthy):
            control = recall_eval.build_record(entries, ("status",))
        degraded = {"hits": [], "reason": "ok", "prior_work_meta": {
            "outcome": "no_match", "reason": "ok", "legs": ["lexical"], "degraded": "late"}}
        with mock.patch("khipu.recall_prompt.prior_work_for_prompt", return_value=degraded):
            result = recall_eval.compare_record(entries[:1], ("status",), control)
        self.assertEqual(result["changed"], 2)
        self.assertEqual({d["status"] for d in result["diffs"]}, {"changed", "removed"})

    def test_expectation_changes_do_not_change_request_identity(self):
        with mock.patch("khipu.embed.hybrid_search", return_value={"results": [{"id": "a"}]}):
            control = recall_eval.build_record([{"query": "q", "expect": ["a"]}], ("explicit",))
        with mock.patch("khipu.embed.hybrid_search", return_value={"results": [{"id": "a"}]}):
            result = recall_eval.compare_record(
                [{"query": "q", "expect": ["different"]}], ("explicit",), control
            )
        self.assertEqual(result["changed"], 0)

    def test_ambiguous_legacy_duplicates_become_new_and_removed(self):
        legacy = {"entries": [
            {"query": "q", "path": "explicit", "got": ["a"]},
            {"query": "q", "path": "explicit", "got": ["b"]},
        ]}
        with mock.patch("khipu.embed.hybrid_search", return_value={"results": [{"id": "a"}]}):
            result = recall_eval.compare_record([{"query": "q", "expect": ["a"]}],
                                                ("explicit",), legacy)
        self.assertEqual(result["changed"], 3)
        self.assertEqual([d["status"] for d in result["diffs"]], ["new", "removed", "removed"])

    def test_unique_legacy_same_ids_timeout_requires_control_regeneration(self):
        legacy = {"entries": [{"query": "q", "path": "status", "got": [], "reason": "ok"}]}
        timeout = {"hits": [], "reason": "ok", "prior_work_meta": {
            "outcome": "timeout", "reason": "ok", "legs": [], "degraded": None}}
        with mock.patch("khipu.recall_prompt.prior_work_for_prompt", return_value=timeout):
            result = recall_eval.compare_record([{"query": "q", "expect_none": True}],
                                                ("status",), legacy)
        self.assertEqual((result["changed"], result["diffs"][0]["status"]), (1, "changed"))
        self.assertTrue(result["diffs"][0]["contract_changed"])

    def test_removed_legacy_control_is_counted(self):
        legacy = {"entries": [{"query": "gone", "path": "explicit", "got": ["a"]}]}
        result = recall_eval.compare_record([], ("explicit",), legacy)
        self.assertEqual((result["changed"], result["diffs"][0]["status"]), (1, "removed"))


class RelevanceEvalRegressionTest(unittest.TestCase):
    def test_emptied_positive_tracks_lost_expected_hit_when_unrelated_result_survives(self):
        searches = [
            {"results": [{"id": "wanted"}, {"id": "other"}]},
            {"results": [{"id": "other"}]},
        ]
        with mock.patch("khipu.embed.hybrid_search", side_effect=searches):
            report = recall_eval.run_relevance_eval(
                [{"query": "q", "expect": ["wanted"]}], ("explicit",), floor=True
            )
        self.assertEqual(report["paths"]["explicit"]["emptied_positives"], ["q"])


class TopicHitsReadFailureTest(unittest.TestCase):
    def test_recompute_read_failure_writes_nothing_and_keeps_checkpoint(self):
        with tempfile.TemporaryDirectory() as td:
            active = Path(td) / "query_log.jsonl"
            active.write_text("{}\n", encoding="utf-8")
            cur = mock.Mock()
            cur.fetchall.return_value = [("existing-topic",)]
            with mock.patch("khipu.query_log.log_path", return_value=active), \
                    mock.patch.object(Path, "read_text", side_effect=OSError("unreadable")), \
                    mock.patch.object(topic_hits, "_write_offset") as checkpoint:
                out = topic_hits.recompute(cur)
        self.assertFalse(out["ok"])
        writes = [call for call in cur.execute.call_args_list
                  if str(call.args[0]).lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
        self.assertEqual(writes, [])
        checkpoint.assert_not_called()


def recall_rule_ceiling() -> int:
    from tests.test_recall_rule import RULE_MD_CEILING

    return RULE_MD_CEILING


class RuleDescriptionTruthTest(unittest.TestCase):
    def test_forget_text_names_current_retention_limits_and_fits_ceiling(self):
        self.assertIn("keep\n  its text marked forgotten", recall_rule.RULE_MD)
        self.assertIn("no command purges or restores it yet", recall_rule.RULE_MD)
        self.assertIn("Topic graph\n  nodes stay", recall_rule.RULE_MD)
        self.assertLess(len(recall_rule.RULE_MD.strip()), recall_rule_ceiling())

