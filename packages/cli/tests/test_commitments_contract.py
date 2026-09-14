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
from khipu import decisions as de
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


# ---- O2: decisions registry -------------------------------------------------

class _DecisionsCursor:
    """In-memory stand-in for the ``decisions`` table (+ a minimal ``episodes``
    table for the backfill test). ``migrated`` says whether migration 0019
    has been applied on this fake hub."""

    def __init__(self, *, migrated: bool = True):
        self.rows: dict[int, dict] = {}
        self.episodes: list[tuple] = []  # (id, project, session_id, decisions, ts)
        self.migrated = migrated
        self.next_id = 1
        self.rowcount = 0
        self._result: list[tuple] = []
        from khipu import db as _db

        _db._TABLE_COLUMNS_CACHE.pop("decisions", None)

    def _within_window(self, existing_at: str, decided_at: str) -> bool:
        from datetime import datetime, timedelta

        def _parse(s):
            return datetime.fromisoformat(str(s).replace("Z", "+00:00"))

        a, b = _parse(existing_at), _parse(decided_at)
        return a <= b and (b - a) <= timedelta(days=de.DEDUP_WINDOW_DAYS)

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        params = params or ()
        if s.startswith("SELECT column_name FROM information_schema.columns"):
            cols = (
                ["id", "project", "text", "rationale", "decided_at", "episode_id",
                 "session_id", "superseded_by", "created_at"]
                if self.migrated else []
            )
            self._result = [(c,) for c in cols]
            return
        if s.startswith("SELECT id FROM decisions WHERE project IS NOT DISTINCT FROM"):
            project, norm_text, decided_at, _decided_at2 = params
            hit = None
            for r in self.rows.values():
                if r["project"] != project:
                    continue
                if de.normalize_text(r["text"]) != norm_text:
                    continue
                if self._within_window(r["decided_at"], decided_at):
                    hit = r["id"]
                    break
            self._result = [(hit,)] if hit is not None else []
            return
        if s.startswith("INSERT INTO decisions"):
            project, text, episode_id, session_id, decided_at = params
            cid = self.next_id
            self.next_id += 1
            self.rows[cid] = {
                "id": cid, "project": project, "text": text, "rationale": None,
                "decided_at": decided_at, "episode_id": episode_id,
                "session_id": session_id, "superseded_by": None,
                "created_at": decided_at,
            }
            self.rowcount = 1
            return
        if s.startswith("SELECT id, project, text, rationale, decided_at"):
            *clause_params, limit = params
            out = list(self.rows.values())
            i = 0
            if "project = %s" in s:
                out = [r for r in out if r["project"] == clause_params[i]]
                i += 1
            if "decided_at >= %s" in s:
                since = clause_params[i]
                out = [r for r in out if str(r["decided_at"]) >= str(since)]
                i += 1
            if "superseded_by IS NULL" in s:
                out = [r for r in out if r["superseded_by"] is None]
            out.sort(key=lambda r: r["decided_at"], reverse=True)
            out = out[:limit]
            cols = ("id", "project", "text", "rationale", "decided_at", "episode_id",
                    "session_id", "superseded_by", "created_at")
            self._result = [tuple(r[c] for c in cols) for r in out]
            return
        if s.startswith("UPDATE decisions SET superseded_by"):
            new_id, old_id = params
            r = self.rows.get(old_id)
            if r and r["superseded_by"] is None:
                r["superseded_by"] = new_id
                self.rowcount = 1
            else:
                self.rowcount = 0
            return
        if s.startswith("SELECT episode_id, COUNT(*) FILTER"):
            (episode_ids,) = params
            counts: dict[int, list[int]] = {}
            for r in self.rows.values():
                if r["episode_id"] not in episode_ids:
                    continue
                bucket = counts.setdefault(r["episode_id"], [0, 0])
                if r["superseded_by"] is None:
                    bucket[0] += 1
                else:
                    bucket[1] += 1
            self._result = [(eid, cur_n, sup_n) for eid, (cur_n, sup_n) in counts.items()]
            return
        if s.startswith("SELECT id, project, session_id, decisions, ts FROM episodes"):
            rows = list(self.episodes)
            if "LIMIT %s" in s:
                rows = rows[: params[0]]
            self._result = rows
            return
        raise AssertionError(f"unexpected SQL: {s[:120]}")

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None


class DecisionsInsertTest(unittest.TestCase):
    def test_each_string_becomes_a_row(self):
        cur = _DecisionsCursor()
        payload = {"project": "acme/widget", "session_id": "claude:s1",
                   "ts": "2026-09-14T10:00:00+00:00",
                   "decisions": ["Ship 0.4.4 with the seal fix", "Use AGPL-3.0 + CLA"]}
        n = de.insert_decisions_from_episode(cur, payload, 42)
        self.assertEqual(n, 2)
        self.assertEqual(len(cur.rows), 2)
        texts = {r["text"] for r in cur.rows.values()}
        self.assertEqual(texts, {"Ship 0.4.4 with the seal fix", "Use AGPL-3.0 + CLA"})

    def test_dedups_the_same_text_in_the_same_project_within_30_days(self):
        cur = _DecisionsCursor()
        payload1 = {"project": "acme/widget", "ts": "2026-09-01T10:00:00+00:00",
                    "decisions": ["Ship 0.4.4 with the seal fix"]}
        payload2 = {"project": "acme/widget", "ts": "2026-09-10T10:00:00+00:00",
                    "decisions": ["Ship 0.4.4 with the seal fix"]}
        self.assertEqual(de.insert_decisions_from_episode(cur, payload1, 1), 1)
        self.assertEqual(de.insert_decisions_from_episode(cur, payload2, 2), 0)
        self.assertEqual(len(cur.rows), 1)

    def test_outside_the_30_day_window_is_a_new_row(self):
        cur = _DecisionsCursor()
        payload1 = {"project": "acme/widget", "ts": "2026-01-01T10:00:00+00:00",
                    "decisions": ["Ship 0.4.4 with the seal fix"]}
        payload2 = {"project": "acme/widget", "ts": "2026-09-10T10:00:00+00:00",
                    "decisions": ["Ship 0.4.4 with the seal fix"]}
        self.assertEqual(de.insert_decisions_from_episode(cur, payload1, 1), 1)
        self.assertEqual(de.insert_decisions_from_episode(cur, payload2, 2), 1)
        self.assertEqual(len(cur.rows), 2)

    def test_different_projects_do_not_dedup_each_other(self):
        cur = _DecisionsCursor()
        payload1 = {"project": "acme/widget", "ts": "2026-09-01T10:00:00+00:00",
                    "decisions": ["Ship 0.4.4 with the seal fix"]}
        payload2 = {"project": "acme/other", "ts": "2026-09-02T10:00:00+00:00",
                    "decisions": ["Ship 0.4.4 with the seal fix"]}
        self.assertEqual(de.insert_decisions_from_episode(cur, payload1, 1), 1)
        self.assertEqual(de.insert_decisions_from_episode(cur, payload2, 2), 1)
        self.assertEqual(len(cur.rows), 2)

    def test_pre_migration_hub_is_a_noop(self):
        cur = _DecisionsCursor(migrated=False)
        payload = {"project": "acme/widget", "ts": "2026-09-14T10:00:00+00:00",
                   "decisions": ["Ship 0.4.4 with the seal fix"]}
        self.assertEqual(de.insert_decisions_from_episode(cur, payload, 1), 0)
        self.assertEqual(len(cur.rows), 0)

    def test_no_decisions_is_a_noop(self):
        cur = _DecisionsCursor()
        self.assertEqual(de.insert_decisions_from_episode(cur, {"project": "acme/widget"}, 1), 0)


class DecisionsListAndSupersedeTest(unittest.TestCase):
    def test_list_decisions_filters_by_project_and_since(self):
        cur = _DecisionsCursor()
        de.insert_decisions_from_episode(
            cur, {"project": "acme/widget", "ts": "2026-09-01T10:00:00+00:00",
                  "decisions": ["Old decision"]}, 1,
        )
        de.insert_decisions_from_episode(
            cur, {"project": "acme/widget", "ts": "2026-09-14T10:00:00+00:00",
                  "decisions": ["New decision"]}, 2,
        )
        de.insert_decisions_from_episode(
            cur, {"project": "acme/other", "ts": "2026-09-14T10:00:00+00:00",
                  "decisions": ["Other project decision"]}, 3,
        )
        rows = de.list_decisions(cur, project="acme/widget", since="2026-09-05T00:00:00+00:00")
        self.assertEqual([r["text"] for r in rows], ["New decision"])

    def test_supersede_marks_the_old_row_and_is_idempotent(self):
        cur = _DecisionsCursor()
        de.insert_decisions_from_episode(
            cur, {"project": "acme/widget", "ts": "2026-09-01T10:00:00+00:00",
                  "decisions": ["Use Apache 2.0"]}, 1,
        )
        de.insert_decisions_from_episode(
            cur, {"project": "acme/other", "ts": "2026-09-14T10:00:00+00:00",
                  "decisions": ["Use AGPL-3.0 + CLA"]}, 2,
        )
        old_id = next(r["id"] for r in cur.rows.values() if r["text"] == "Use Apache 2.0")
        new_id = next(r["id"] for r in cur.rows.values() if r["text"] == "Use AGPL-3.0 + CLA")
        self.assertTrue(de.supersede(cur, old_id, new_id))
        self.assertEqual(cur.rows[old_id]["superseded_by"], new_id)
        # a second call is a no-op (already superseded), not an error
        self.assertFalse(de.supersede(cur, old_id, new_id))

    def test_list_decisions_can_exclude_superseded(self):
        cur = _DecisionsCursor()
        de.insert_decisions_from_episode(
            cur, {"project": "acme/widget", "ts": "2026-09-01T10:00:00+00:00",
                  "decisions": ["Use Apache 2.0"]}, 1,
        )
        de.insert_decisions_from_episode(
            cur, {"project": "acme/widget", "ts": "2026-09-14T10:00:00+00:00",
                  "decisions": ["Use MIT instead"]}, 2,
        )
        old_id = next(r["id"] for r in cur.rows.values() if r["text"] == "Use Apache 2.0")
        new_id = next(r["id"] for r in cur.rows.values() if r["text"] == "Use MIT instead")
        de.supersede(cur, old_id, new_id)
        rows = de.list_decisions(cur, project="acme/widget", include_superseded=False)
        self.assertEqual([r["text"] for r in rows], ["Use MIT instead"])


class DecisionsSearchEnrichmentTest(unittest.TestCase):
    def test_episode_rows_get_current_and_superseded_counts(self):
        cur = _DecisionsCursor()
        de.insert_decisions_from_episode(
            cur, {"project": "acme/widget", "ts": "2026-09-01T10:00:00+00:00",
                  "decisions": ["Use Apache 2.0"]}, 101,
        )
        de.insert_decisions_from_episode(
            cur, {"project": "acme/widget", "ts": "2026-09-14T10:00:00+00:00",
                  "decisions": ["Use AGPL-3.0 + CLA"]}, 101,
        )
        old_id = next(r["id"] for r in cur.rows.values() if r["text"] == "Use Apache 2.0")
        new_id = next(r["id"] for r in cur.rows.values() if r["text"] == "Use AGPL-3.0 + CLA")
        de.supersede(cur, old_id, new_id)
        results = [{"kind": "episode", "id": "101", "label": "licence episode"},
                   {"kind": "topic", "id": "licensing"}]
        out = de.enrich_search_results(cur, results)
        episode_row = next(r for r in out if r["kind"] == "episode")
        self.assertEqual(episode_row["decisions_current"], 1)
        self.assertEqual(episode_row["decisions_superseded"], 1)
        topic_row = next(r for r in out if r["kind"] == "topic")
        self.assertNotIn("decisions_current", topic_row)


class DecisionsBackfillTest(unittest.TestCase):
    def test_dry_run_counts_without_writing(self):
        cur = _DecisionsCursor()
        cur.episodes = [
            (1, "acme/widget", "claude:s1", ["Ship 0.4.4 with the seal fix"],
             "2026-09-01T10:00:00+00:00"),
            (2, "acme/widget", "claude:s2", ["Use AGPL-3.0 + CLA"],
             "2026-09-02T10:00:00+00:00"),
        ]
        report = de.backfill_decisions(cur, apply=False)
        self.assertEqual(report["episodes_scanned"], 2)
        self.assertEqual(report["decisions_inserted"], 2)
        self.assertTrue(report["dry_run"])
        self.assertEqual(len(cur.rows), 0)  # dry run never writes

    def test_apply_actually_inserts_and_is_idempotent(self):
        cur = _DecisionsCursor()
        cur.episodes = [
            (1, "acme/widget", "claude:s1", ["Ship 0.4.4 with the seal fix"],
             "2026-09-01T10:00:00+00:00"),
        ]
        report1 = de.backfill_decisions(cur, apply=True)
        self.assertEqual(report1["decisions_inserted"], 1)
        self.assertEqual(len(cur.rows), 1)
        report2 = de.backfill_decisions(cur, apply=True)
        self.assertEqual(report2["decisions_inserted"], 0)  # already backfilled
        self.assertEqual(len(cur.rows), 1)

    def test_pre_migration_hub_refuses_cleanly(self):
        cur = _DecisionsCursor(migrated=False)
        report = de.backfill_decisions(cur, apply=False)
        self.assertFalse(report.get("ok", True))


# ---- O4: deliverables index -------------------------------------------------

from khipu import extract as _extract
from khipu import deliverables as dl


class ExtractDeliverablesTest(unittest.TestCase):
    def test_a_written_file_is_captured(self):
        out = _extract.extract_deliverables(
            "ASSISTANT: I wrote the migration to ops/migrations/0019_decisions.sql."
        )
        self.assertEqual(out, [{"kind": "file", "path": "ops/migrations/0019_decisions.sql",
                                 "url": None, "title": None}])

    def test_created_and_saved_and_added_all_count(self):
        for verb in ("created", "saved", "added"):
            with self.subTest(verb=verb):
                out = _extract.extract_deliverables(
                    f"ASSISTANT: I {verb} packages/cli/khipu/decisions.py for the module."
                )
                self.assertEqual(out[0]["path"], "packages/cli/khipu/decisions.py")

    def test_a_pr_url_is_captured(self):
        out = _extract.extract_deliverables(
            "ASSISTANT: Opened https://github.com/mrkinglollipop/Khipu/pull/82 for review."
        )
        self.assertEqual(out, [{"kind": "pr", "path": None,
                                 "url": "https://github.com/mrkinglollipop/Khipu/pull/82",
                                 "title": None}])

    def test_an_issue_url_is_captured(self):
        out = _extract.extract_deliverables(
            "ASSISTANT: Filed https://github.com/mrkinglollipop/Khipu/issues/45."
        )
        self.assertEqual(out[0]["kind"], "issue")

    def test_a_release_tag_is_captured(self):
        out = _extract.extract_deliverables("ASSISTANT: Tagged v0.4.4 and published it.")
        self.assertEqual(out, [{"kind": "release", "path": None, "url": None, "title": "v0.4.4"}])

    def test_a_path_with_no_verb_is_not_captured(self):
        """Only a path on the SAME line as wrote/created/added/saved counts —
        a path merely mentioned later is not something this window built."""
        out = _extract.extract_deliverables(
            "ASSISTANT: The config lives at packages/cli/khipu/config.py."
        )
        self.assertEqual(out, [])

    def test_an_unlisted_extension_is_ignored(self):
        out = _extract.extract_deliverables("ASSISTANT: I wrote notes.txt for later.")
        self.assertEqual(out, [])

    def test_duplicates_within_one_window_are_deduped(self):
        out = _extract.extract_deliverables(
            "ASSISTANT: I wrote khipu/decisions.py.\n\n"
            "ASSISTANT: As mentioned, I wrote khipu/decisions.py again."
        )
        self.assertEqual(len(out), 1)

    def test_empty_text_returns_empty_list(self):
        self.assertEqual(_extract.extract_deliverables(""), [])


class _DeliverablesCursor:
    """In-memory stand-in for the ``deliverables`` table."""

    def __init__(self, *, migrated: bool = True):
        self.rows: dict[int, dict] = {}
        self.migrated = migrated
        self.next_id = 1
        self.rowcount = 0
        self._result: list[tuple] = []
        from khipu import db as _db

        _db._TABLE_COLUMNS_CACHE.pop("deliverables", None)

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        params = params or ()
        if s.startswith("SELECT column_name FROM information_schema.columns"):
            cols = (
                ["id", "project", "kind", "path", "url", "title", "episode_id", "created_at"]
                if self.migrated else []
            )
            self._result = [(c,) for c in cols]
            return
        if s.startswith("SELECT id FROM deliverables WHERE project IS NOT DISTINCT FROM"):
            project, kind, path, url = params
            hit = next(
                (r["id"] for r in self.rows.values()
                 if r["project"] == project and r["kind"] == kind
                 and r["path"] == path and r["url"] == url),
                None,
            )
            self._result = [(hit,)] if hit is not None else []
            return
        if s.startswith("INSERT INTO deliverables"):
            project, kind, path, url, title, episode_id = params
            cid = self.next_id
            self.next_id += 1
            self.rows[cid] = {
                "id": cid, "project": project, "kind": kind, "path": path, "url": url,
                "title": title, "episode_id": episode_id, "created_at": "2026-09-14",
            }
            self.rowcount = 1
            return
        if s.startswith("SELECT id, project, kind, path, url, title, episode_id, created_at"):
            project, limit = params
            out = [r for r in self.rows.values() if r["project"] == project]
            cols = ("id", "project", "kind", "path", "url", "title", "episode_id", "created_at")
            self._result = [tuple(r[c] for c in cols) for r in out[:limit]]
            return
        raise AssertionError(f"unexpected SQL: {s[:120]}")

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None


class DeliverablesInsertTest(unittest.TestCase):
    def test_each_item_becomes_a_row(self):
        cur = _DeliverablesCursor()
        payload = {"project": "acme/widget", "deliverables": [
            {"kind": "file", "path": "khipu/decisions.py", "url": None, "title": None},
            {"kind": "pr", "path": None, "url": "https://x/pull/1", "title": None},
        ]}
        self.assertEqual(dl.insert_deliverables_from_episode(cur, payload, 1), 2)
        self.assertEqual(len(cur.rows), 2)

    def test_the_same_path_in_the_same_project_dedups(self):
        cur = _DeliverablesCursor()
        payload = {"project": "acme/widget", "deliverables": [
            {"kind": "file", "path": "khipu/decisions.py", "url": None, "title": None},
        ]}
        self.assertEqual(dl.insert_deliverables_from_episode(cur, payload, 1), 1)
        self.assertEqual(dl.insert_deliverables_from_episode(cur, payload, 2), 0)
        self.assertEqual(len(cur.rows), 1)

    def test_an_unknown_kind_is_skipped(self):
        cur = _DeliverablesCursor()
        payload = {"project": "acme/widget", "deliverables": [
            {"kind": "bogus", "path": "x.py"},
        ]}
        self.assertEqual(dl.insert_deliverables_from_episode(cur, payload, 1), 0)

    def test_pre_migration_hub_is_a_noop(self):
        cur = _DeliverablesCursor(migrated=False)
        payload = {"project": "acme/widget", "deliverables": [
            {"kind": "file", "path": "khipu/decisions.py"},
        ]}
        self.assertEqual(dl.insert_deliverables_from_episode(cur, payload, 1), 0)


class DeliverablesMatcherTest(unittest.TestCase):
    """The recall-hook matcher — pure logic, no DB."""

    def test_two_or_more_matching_tokens_is_a_match(self):
        rows = [{"path": "packages/cli/khipu/decisions.py", "title": None,
                 "created_at": "2026-09-14", "episode_id": 42}]
        match = dl.best_match_for_tokens(rows, ["decisions", "khipu", "registry"])
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match["episode_id"], 42)

    def test_one_matching_token_is_not_a_match(self):
        rows = [{"path": "packages/cli/khipu/decisions.py", "title": None,
                 "created_at": "2026-09-14", "episode_id": 42}]
        self.assertIsNone(dl.best_match_for_tokens(rows, ["decisions", "unrelated"]))

    def test_the_best_scoring_row_wins(self):
        rows = [
            {"path": "khipu/decisions.py", "title": None, "created_at": "2026-09-01",
             "episode_id": 1},
            {"path": "khipu/decisions.py", "title": "decisions registry", "created_at": "2026-09-14",
             "episode_id": 2},
        ]
        match = dl.best_match_for_tokens(rows, ["decisions", "registry"])
        assert match is not None
        self.assertEqual(match["episode_id"], 2)

    def test_format_produced_line(self):
        row = {"path": "khipu/decisions.py", "url": None, "title": None,
               "created_at": "2026-09-14T10:00:00+00:00", "episode_id": 42}
        line = dl.format_produced_line(row)
        self.assertEqual(line, "You produced khipu/decisions.py on 2026-09-14 (episode 42)")

    def test_deliverable_line_for_prompt_end_to_end(self):
        cur = _DeliverablesCursor()
        payload = {"project": "acme/widget", "deliverables": [
            {"kind": "file", "path": "khipu/decisions.py", "url": None, "title": None},
        ]}
        dl.insert_deliverables_from_episode(cur, payload, 42)
        line = dl.deliverable_line_for_prompt(
            cur, ["decisions", "registry", "khipu"], project="acme/widget"
        )
        self.assertIsNotNone(line)
        assert line is not None
        self.assertTrue(line.startswith("You produced khipu/decisions.py"))
        self.assertIn("episode 42", line)

    def test_no_project_never_queries(self):
        cur = _DeliverablesCursor()
        self.assertIsNone(dl.deliverable_line_for_prompt(cur, ["x", "y"], project=None))
