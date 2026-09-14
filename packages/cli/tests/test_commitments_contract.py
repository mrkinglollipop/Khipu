"""Contract tests for Phase 3 — commitments and decisions that survive
(O1, O2, O3, O4, K6).

Each section targets one gap the phase brief named. No live database: the
commitments sections reuse the in-memory fake cursor from test_commitments.py
(same posture as that module — cosine matching forced to fail so paraphrase
dedup falls back to the pure-Python Jaccard path).
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from khipu import commitments as co
from tests.test_commitments import _CommitmentsCursor, _alias_env_patch


def setUpModule():
    _alias_env_patch.start()
    co.reset_user_patterns()


def tearDownModule():
    _alias_env_patch.stop()
    co.reset_user_patterns()


# ---- O1: deferrals survive --------------------------------------------------

class FutureTriggerGrammarTest(unittest.TestCase):
    """Every phrase the brief lists must flip has_future_trigger to True; a
    same-session phrase must stay False."""

    def test_until_clause_is_a_trigger(self):
        self.assertTrue(
            co.has_future_trigger("Run the oracle again until the ledger closes.")
        )

    def test_before_clause_is_a_trigger(self):
        self.assertTrue(
            co.has_future_trigger("Ship the notes before the wave lands.")
        )

    def test_once_is_done_is_a_trigger(self):
        self.assertTrue(
            co.has_future_trigger("Re-run notarisation once the seal fix is done.")
        )

    def test_when_is_ready_is_a_trigger(self):
        self.assertTrue(
            co.has_future_trigger("Send the SHAs when the build is ready.")
        )

    def test_after_it_resolves_is_a_trigger(self):
        self.assertTrue(
            co.has_future_trigger("Reply with the numbers after the incident resolves.")
        )

    def test_once_it_wraps_up_is_a_trigger(self):
        self.assertTrue(
            co.has_future_trigger("Close this out once the migration wraps up.")
        )

    def test_deferred_until_is_a_trigger(self):
        self.assertTrue(
            co.has_future_trigger("Build the export screen — deferred until Q4.")
        )

    def test_deferred_to_is_a_trigger(self):
        self.assertTrue(
            co.has_future_trigger("Ship the redesign, deferred to next quarter.")
        )

    def test_parked_until_is_a_trigger(self):
        self.assertTrue(
            co.has_future_trigger("The Cursor lane is parked until the wall lifts.")
        )

    def test_not_until_is_a_trigger(self):
        self.assertTrue(
            co.has_future_trigger("Do not ship this, not until legal signs off.")
        )

    def test_same_session_sequencing_is_not_a_trigger(self):
        for text in (
            "I will do it now.",
            "Fix HIGH 1, before and after the mid-stream WS kill.",
            "Rebuild the index and measure the timings.",
        ):
            with self.subTest(text=text):
                self.assertFalse(co.has_future_trigger(text), text)


class TriggerClauseTest(unittest.TestCase):
    def test_extracts_the_clause_after_the_trigger_word(self):
        clause = co.trigger_clause("Run the oracle again until the ledger closes.")
        self.assertIsNotNone(clause)
        assert clause is not None
        self.assertTrue(clause.lower().startswith("until"))
        self.assertIn("closes", clause)

    def test_none_when_there_is_no_trigger(self):
        self.assertIsNone(co.trigger_clause("Rebuild the index and measure it."))

    def test_stops_at_the_next_comma(self):
        clause = co.trigger_clause(
            "Send the numbers once the seal fix ships, then close this out."
        )
        assert clause is not None
        self.assertNotIn("then close this out", clause)


class DeferralNeverClosesAtSessionEndTest(unittest.TestCase):
    """close_session_plan must never close a row whose text carries a future
    trigger — including the O1 shapes that used to slip through
    (has_future_trigger was False for them, so 'session-ended' fired)."""

    def test_a_deferred_until_row_survives_sessionend(self):
        cur = _CommitmentsCursor(migrated=True)
        cur._seed(
            "Run the oracle again until the ledger closes.",
            owner="assistant", kind="followup", session_id="claude:s1",
        )
        payload = {"project": "acme/widget", "session_id": "claude:s1", "event": "sessionend"}
        closed = co.close_session_plan(cur, payload, 9)
        self.assertEqual(closed, 0)
        self.assertTrue(all(r["status"] == "open" for r in cur.rows.values()))

    def test_a_before_clause_row_survives_sessionend(self):
        cur = _CommitmentsCursor(migrated=True)
        cur._seed(
            "Ship the notes before the wave lands.",
            owner="assistant", kind="followup", session_id="claude:s1",
        )
        payload = {"project": "acme/widget", "session_id": "claude:s1", "event": "sessionend"}
        closed = co.close_session_plan(cur, payload, 9)
        self.assertEqual(closed, 0)


class OpenFromEpisodeStoresTriggerTextTest(unittest.TestCase):
    def test_trigger_text_is_stored_when_the_column_exists(self):
        cur = _CommitmentsCursor(migrated=True, trigger=True, trigger_text=True)
        payload = {"project": "acme/widget", "open_loops": [
            {"text": "Run the oracle again until the ledger closes.",
             "kind": "followup", "owner": "assistant"},
        ]}
        self.assertEqual(co.open_from_episode(cur, payload, 1), 1)
        row = next(iter(cur.rows.values()))
        self.assertTrue(row["future_trigger"])
        self.assertIsNotNone(row["trigger_text"])
        assert row["trigger_text"] is not None
        self.assertTrue(row["trigger_text"].lower().startswith("until"))

    def test_a_pre_migration_hub_opens_without_the_column(self):
        cur = _CommitmentsCursor(migrated=True)  # no trigger/trigger_text columns
        payload = {"project": "acme/widget", "open_loops": [
            "Run the oracle again until the ledger closes.",
        ]}
        self.assertEqual(co.open_from_episode(cur, payload, 1), 1)


class ListOwedShowsUntilLineTest(unittest.TestCase):
    def test_list_owed_carries_an_until_line_for_a_deferred_row(self):
        cur = _CommitmentsCursor(migrated=True)
        cur._seed("Run the oracle again until the ledger closes.", owner="assistant")
        row = co.list_owed(cur, project="acme/widget")[0]
        self.assertIsNotNone(row["until"])
        assert row["until"] is not None
        self.assertTrue(row["until"].startswith("until:"))

    def test_list_owed_has_no_until_line_when_there_is_no_trigger(self):
        cur = _CommitmentsCursor(migrated=True)
        cur._seed("Matt must merge the stack himself")
        row = co.list_owed(cur, project="acme/widget")[0]
        self.assertIsNone(row["until"])
