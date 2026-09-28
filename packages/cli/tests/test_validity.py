# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""khipu.validity — the one validity policy (Phase 2, session B). Pure
functions, no database, no network — every test here is in-process.
"""
from __future__ import annotations

import unittest
from unittest import mock

from khipu import validity as v


class NormalizeTopicStatusTest(unittest.TestCase):
    def test_the_three_not_current_values(self):
        for s in ("superseded", "retired", "abandoned"):
            self.assertEqual(v.normalize_topic_status(s), "not_current")

    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(v.normalize_topic_status("  SUPERSEDED  "), "not_current")

    def test_unknown_text_is_current(self):
        self.assertEqual(v.normalize_topic_status("archived"), "current")
        self.assertEqual(v.normalize_topic_status("draft"), "current")

    def test_empty_and_none_are_current(self):
        self.assertEqual(v.normalize_topic_status(""), "current")
        self.assertEqual(v.normalize_topic_status(None), "current")


class TopicStateTest(unittest.TestCase):
    def test_superseded_by_wins_outright(self):
        self.assertEqual(v.topic_state("active", "new-slug"), "superseded")

    def test_not_current_status_without_superseded_by(self):
        self.assertEqual(v.topic_state("retired", None), "superseded")

    def test_current_status_and_no_superseded_by(self):
        self.assertEqual(v.topic_state("active", None), "current")

    def test_unknown_status_and_no_superseded_by_is_current(self):
        self.assertEqual(v.topic_state("some-weird-value", None), "current")

    def test_empty_superseded_by_string_does_not_win(self):
        self.assertEqual(v.topic_state("active", ""), "current")


class EpisodeStateTest(unittest.TestCase):
    def test_no_decisions_is_current(self):
        self.assertEqual(v.episode_state(0, 0, 0), "current")

    def test_only_current(self):
        self.assertEqual(v.episode_state(2, 0, 0), "current")

    def test_only_superseded(self):
        self.assertEqual(v.episode_state(0, 1, 0), "superseded")

    def test_only_retracted(self):
        self.assertEqual(v.episode_state(0, 0, 1), "retracted")

    def test_current_and_superseded_is_mixed(self):
        self.assertEqual(v.episode_state(1, 1, 0), "mixed")

    def test_current_and_retracted_is_mixed(self):
        self.assertEqual(v.episode_state(1, 0, 1), "mixed")

    def test_current_superseded_and_retracted_is_mixed(self):
        self.assertEqual(v.episode_state(1, 1, 1), "mixed")

    def test_superseded_and_retracted_with_no_current_prefers_retracted(self):
        # Same precedence as decisions.state_of: retracting is the stronger
        # claim when both are true of the same episode's decisions and none
        # remain current.
        self.assertEqual(v.episode_state(0, 1, 1), "retracted")

    def test_none_like_inputs_default_to_zero(self):
        self.assertEqual(v.episode_state(None, None, None), "current")


class AnnotateTest(unittest.TestCase):
    def test_episode_counts_none_is_unknown_and_untouched_status(self):
        rows = [{"kind": "episode", "id": "1", "score": 0.5}]
        out = v.annotate(rows, None, None)
        self.assertEqual(out[0]["validity"], {
            "state": "unknown", "current": None, "superseded": None, "retracted": None,
        })
        self.assertNotIn("status", out[0])

    def test_episode_with_no_matching_counts_entry_is_current(self):
        rows = [{"kind": "episode", "id": "1", "score": 0.5}]
        out = v.annotate(rows, {}, None)
        self.assertEqual(out[0]["validity"]["state"], "current")
        self.assertNotIn("status", out[0])

    def test_episode_superseded_sets_status_label(self):
        rows = [{"kind": "episode", "id": "7", "score": 0.5}]
        out = v.annotate(rows, {"7": (0, 1, 0)}, None)
        self.assertEqual(out[0]["validity"]["state"], "superseded")
        self.assertEqual(out[0]["status"], "superseded")

    def test_episode_mixed_sets_partly_superseded_label(self):
        rows = [{"kind": "episode", "id": "7", "score": 0.5}]
        out = v.annotate(rows, {"7": (1, 1, 0)}, None)
        self.assertEqual(out[0]["validity"]["state"], "mixed")
        self.assertEqual(out[0]["status"], "partly superseded")

    def test_episode_retracted_sets_retracted_label(self):
        rows = [{"kind": "episode", "id": "7", "score": 0.5}]
        out = v.annotate(rows, {"7": (0, 0, 1)}, None)
        self.assertEqual(out[0]["validity"]["state"], "retracted")
        self.assertEqual(out[0]["status"], "retracted")

    def test_topic_meta_none_is_unknown(self):
        rows = [{"kind": "topic", "id": "t1", "status": "active"}]
        out = v.annotate(rows, None, None)
        self.assertEqual(out[0]["validity"], {"state": "unknown", "superseded_by": None})
        # The topic's own pre-existing status field is never touched.
        self.assertEqual(out[0]["status"], "active")

    def test_topic_current_from_meta(self):
        rows = [{"kind": "topic", "id": "t1", "status": "active"}]
        out = v.annotate(rows, None, {"t1": {"status": "active", "superseded_by": None}})
        self.assertEqual(out[0]["validity"], {"state": "current", "superseded_by": None})
        self.assertEqual(out[0]["status"], "active")

    def test_topic_superseded_from_meta_never_rewrites_status_field(self):
        rows = [{"kind": "topic", "id": "t1", "status": "superseded"}]
        out = v.annotate(rows, None, {"t1": {"status": "superseded", "superseded_by": None}})
        self.assertEqual(out[0]["validity"]["state"], "superseded")
        # annotate() never writes a topic row's `status` — it is already
        # "superseded" here because the ROW carried it, not because
        # annotate set it.
        self.assertEqual(out[0]["status"], "superseded")

    def test_other_kinds_pass_through_unchanged(self):
        rows = [{"kind": "node", "id": "n1", "score": 0.1}]
        out = v.annotate(rows, {}, {})
        self.assertEqual(out, rows)
        self.assertNotIn("validity", out[0])

    def test_rows_are_copied_not_mutated(self):
        row = {"kind": "episode", "id": "1", "score": 0.5}
        out = v.annotate([row], {"1": (0, 1, 0)}, None)
        self.assertNotIn("validity", row)
        self.assertIn("validity", out[0])

    def test_empty_rows_list(self):
        self.assertEqual(v.annotate([], {}, {}), [])


class IsHistoricalTest(unittest.TestCase):
    def test_since_or_until_forces_true(self):
        self.assertTrue(v.is_historical("anything", since="2026-01-01"))
        self.assertTrue(v.is_historical("anything", until="2026-01-01"))

    def test_history_cues(self):
        for cue in ("previously", "originally", "used to", "history", "earlier",
                    "back then", "as of", "at the time", "why did we"):
            with self.subTest(cue=cue):
                self.assertTrue(v.is_historical(f"what did we do {cue} do this"))

    def test_case_insensitive(self):
        self.assertTrue(v.is_historical("What did we do PREVIOUSLY"))

    def test_no_cue_and_no_filters_is_false(self):
        self.assertFalse(v.is_historical("what did we approve for the widget endpoint"))

    def test_empty_query_is_false(self):
        self.assertFalse(v.is_historical(""))
        self.assertFalse(v.is_historical(None))


class ApplyRankingTest(unittest.TestCase):
    def _row(self, state: str, score: float, kind: str = "episode") -> dict:
        return {"kind": kind, "id": "1", "score": score, "validity": {"state": state}}

    def test_switch_off_by_default_never_changes_rows(self):
        rows = [self._row("superseded", 1.0)]
        out = v.apply_ranking(rows, historical=False)
        self.assertEqual(out[0]["score"], 1.0)

    def test_switch_on_deranks_superseded_and_retracted(self):
        from khipu import features
        from khipu.recency import STATUS_DERANK

        rows = [self._row("superseded", 1.0), self._row("current", 0.9)]
        with mock.patch.object(features, "enabled", return_value=True):
            out = v.apply_ranking(rows, historical=False)
        superseded_row = next(r for r in out if r["validity"]["state"] == "superseded")
        self.assertAlmostEqual(superseded_row["score"], round(1.0 * STATUS_DERANK, 6))
        # Re-sorted: the de-ranked row must no longer lead.
        self.assertEqual(out[0]["validity"]["state"], "current")

    def test_mixed_is_never_deranked(self):
        from khipu import features

        rows = [self._row("mixed", 1.0)]
        with mock.patch.object(features, "enabled", return_value=True):
            out = v.apply_ranking(rows, historical=False)
        self.assertEqual(out[0]["score"], 1.0)

    def test_topic_rows_are_never_touched_here(self):
        from khipu import features

        rows = [self._row("superseded", 1.0, kind="topic")]
        with mock.patch.object(features, "enabled", return_value=True):
            out = v.apply_ranking(rows, historical=False)
        self.assertEqual(out[0]["score"], 1.0)

    def test_historical_query_skips_ranking_even_with_switch_on(self):
        from khipu import features

        rows = [self._row("superseded", 1.0)]
        with mock.patch.object(features, "enabled", return_value=True):
            out = v.apply_ranking(rows, historical=True)
        self.assertEqual(out[0]["score"], 1.0)

    def test_a_switch_lookup_failure_leaves_rows_unchanged(self):
        from khipu import features

        rows = [self._row("superseded", 1.0)]
        with mock.patch.object(features, "enabled", side_effect=RuntimeError("boom")):
            out = v.apply_ranking(rows, historical=False)
        self.assertEqual(out[0]["score"], 1.0)

    def test_empty_rows(self):
        self.assertEqual(v.apply_ranking([], historical=False), [])


class RevisionTokenTest(unittest.TestCase):
    def test_no_validity_key_is_empty(self):
        self.assertEqual(v.revision_token({"kind": "episode", "id": "1"}), "")

    def test_current_state_is_empty(self):
        self.assertEqual(v.revision_token({"validity": {"state": "current"}}), "")

    def test_unknown_state_is_empty(self):
        self.assertEqual(v.revision_token({"validity": {"state": "unknown"}}), "")

    def test_superseded_mixed_retracted_each_get_a_letter(self):
        self.assertEqual(v.revision_token({"validity": {"state": "superseded"}}), "s")
        self.assertEqual(v.revision_token({"validity": {"state": "mixed"}}), "m")
        self.assertEqual(v.revision_token({"validity": {"state": "retracted"}}), "r")

    def test_an_explicit_revision_above_one_wins_regardless_of_state(self):
        self.assertEqual(
            v.revision_token({"validity": {"state": "current", "revision": 2}}), "2"
        )

    def test_revision_of_one_or_less_is_the_default_empty(self):
        self.assertEqual(
            v.revision_token({"validity": {"state": "current", "revision": 1}}), ""
        )


if __name__ == "__main__":
    unittest.main()
