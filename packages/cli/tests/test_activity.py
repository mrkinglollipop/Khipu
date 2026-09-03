"""khipu.activity backs the desktop Activity pane. Untested until the
2026-08-17 audit, which found activity_payload opening a second connection for
its own recent-episode list.

Postgres is always faked here.
"""

from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

from khipu import activity
from tests.test_revisions import FakeConn, FakeCursor


def _episode_row(**kw):
    base = {
        "id": 1,
        "ts": dt.datetime(2026, 8, 17, 12, 0),
        "ingested_at": dt.datetime(2026, 8, 17, 12, 0, 30),
        "session_id": "claude_code:abc",
        "scope": "repo",
        "summary": "did a thing",
        "topics": ["khipu"],
        "decisions": [],
        "preferences": [],
        "mirror_age": dt.timedelta(seconds=90),
    }
    base.update(kw)
    return tuple(base.values())


class RecentEpisodesTest(unittest.TestCase):
    def test_rows_are_shaped_and_timestamps_isoformatted(self):
        cur = FakeCursor([[_episode_row()]])
        with mock.patch.object(activity, "connect", return_value=FakeConn(cur)):
            out = activity.recent_episodes(limit=5)
        self.assertEqual(out[0]["ts"], "2026-08-17T12:00:00")
        self.assertEqual(out[0]["mirror_age_seconds"], 90.0)
        self.assertEqual(cur.params[0], (5,))

    def test_null_timestamps_survive(self):
        cur = FakeCursor([[_episode_row(ts=None, ingested_at=None, mirror_age=None)]])
        with mock.patch.object(activity, "connect", return_value=FakeConn(cur)):
            out = activity.recent_episodes()
        self.assertIsNone(out[0]["ts"])
        self.assertIsNone(out[0]["mirror_age_seconds"])

    def test_an_empty_table_is_an_empty_list(self):
        cur = FakeCursor([[]])
        with mock.patch.object(activity, "connect", return_value=FakeConn(cur)):
            self.assertEqual(activity.recent_episodes(), [])


class EpisodeDetailTest(unittest.TestCase):
    def test_a_missing_id_is_none(self):
        cur = FakeCursor([[]])
        with mock.patch.object(activity, "connect", return_value=FakeConn(cur)):
            self.assertIsNone(activity.episode_detail(404))

    def test_detail_carries_raw_and_edges(self):
        row = (1, None, None, "s", "repo", "sum", [], [], [], [], [{"a": 1}], {"r": 2})
        cur = FakeCursor([[row]])
        with mock.patch.object(activity, "connect", return_value=FakeConn(cur)):
            out = activity.episode_detail(1)
        self.assertEqual(out["edges"], [{"a": 1}])
        self.assertEqual(out["raw"], {"r": 2})


class TopicDetailTest(unittest.TestCase):
    def test_a_missing_slug_is_none(self):
        cur = FakeCursor([[]])
        with mock.patch.object(activity, "connect", return_value=FakeConn(cur)):
            self.assertIsNone(activity.topic_detail("nope"))

    def test_detail_shape(self):
        created = dt.datetime(2026, 8, 17, 12, 0)
        row = ("khipu", "Khipu", "body text", "active", created, created, [], "/x.md")
        cur = FakeCursor([[row]])
        with mock.patch.object(activity, "connect", return_value=FakeConn(cur)):
            out = activity.topic_detail("khipu")
        self.assertEqual(out["slug"], "khipu")
        self.assertEqual(out["body"], "body text")
        self.assertEqual(out["created_at"], "2026-08-17T12:00:00")


class RecentClipTest(unittest.TestCase):
    def test_long_summaries_are_clipped_on_a_word(self):
        long = (
            "The session also included a discussion about Khipu retrieval. "
            * 20
        )
        cur = FakeCursor([[_episode_row(summary=long)]])
        with mock.patch.object(activity, "connect", return_value=FakeConn(cur)):
            out = activity.recent_episodes(limit=1)
        s = out[0]["summary"]
        self.assertTrue(s.endswith("…"))
        self.assertLess(len(s), len(long))
        self.assertFalse(s.rstrip("…").endswith("include"))


class ActivityPayloadTest(unittest.TestCase):
    """Query order: COUNT, MAX/lag, ops_events regclass, [ops rows], recent."""

    def _results(self, *, has_ops: bool):
        res = [
            [(12,)],
            [(dt.datetime(2026, 8, 17, 12, 0), dt.datetime(2026, 8, 17, 12, 1),
              dt.timedelta(seconds=45))],
            [(has_ops,)],
        ]
        if has_ops:
            res.append([("reconcile", "ok", "622 topics", dt.datetime(2026, 8, 17, 6, 5))])
        res.append([_episode_row()])
        return res

    def _run(self, *, has_ops=True):
        cur = FakeCursor(self._results(has_ops=has_ops))
        with mock.patch.object(activity, "connect", return_value=FakeConn(cur)) as c, \
             mock.patch("khipu.keychain.secrets_status", return_value={"dsn": True}):
            out = activity.activity_payload(limit=3)
        return out, cur, c

    # --- regression: audit 2026-08-17 -----------------------------------------

    def test_one_connection_serves_the_whole_payload(self):
        """The recent list was fetched through a second connect() after the
        first block had already closed."""
        _, _, conn = self._run()
        self.assertEqual(conn.call_count, 1)

    def test_the_payload_carries_counts_lag_and_recent(self):
        out, _, _ = self._run()
        self.assertEqual(out["episode_count"], 12)
        self.assertEqual(out["ingest_lag_seconds"], 45.0)
        self.assertEqual(len(out["recent"]), 1)
        self.assertEqual(out["ops_events"][0]["kind"], "reconcile")

    def test_a_missing_ops_events_table_is_not_an_error(self):
        out, _, _ = self._run(has_ops=False)
        self.assertEqual(out["ops_events"], [])
        self.assertEqual(out["episode_count"], 12)

    def test_a_broken_keychain_degrades_instead_of_failing_the_pane(self):
        cur = FakeCursor(self._results(has_ops=False))
        with mock.patch.object(activity, "connect", return_value=FakeConn(cur)), \
             mock.patch("khipu.keychain.secrets_status", side_effect=RuntimeError("locked")):
            out = activity.activity_payload()
        self.assertIn("locked", out["secrets"]["error"])
        self.assertEqual(out["episode_count"], 12)

    def test_the_limit_reaches_the_recent_query(self):
        _, cur, _ = self._run()
        self.assertEqual(cur.params[-1], (3,))


class ProjectSliceTest(unittest.TestCase):
    """W4 pushed-slice reads: commitments -> episodes -> linked topics, in
    that query order, on the connection commitments.list_owed and
    embed._episode_schema_flags already share with this module."""

    def _run(self, results, **kw):
        cur = FakeCursor(results)
        with mock.patch.object(activity, "connect", return_value=FakeConn(cur)):
            out = activity.project_slice(**kw)
        return out, cur

    def test_full_shape_commitments_episodes_and_topics(self):
        commitment_row = (
            1, "ship the fix", "acme/widget", None, "followup", 9,
            dt.datetime(2026, 9, 1), None, "open", None, None, None,
        )
        episode_row = (
            42, dt.datetime(2026, 9, 3, 10, 0), "shipped the fix", ["real-topic"],
        )
        topic_row = ("real-topic", "Real Topic", dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc))
        # _episode_schema_flags is mocked at the function level below (its
        # own unit tests in test_embed.py cover the information_schema
        # query), so the fake cursor sees four real queries here: commitments,
        # episodes, episode-linked topics, then note-topics (still under
        # topic_limit=3 after one episode-linked hit) — in that order.
        results = [[commitment_row], [episode_row], [topic_row], []]
        with mock.patch("khipu.embed._episode_schema_flags", return_value={
            "project": True, "deleted_at": True,
        }):
            out, cur = self._run(results, project="acme/widget")
        self.assertEqual(out["commitments"][0]["text"], "ship the fix")
        self.assertEqual(out["episodes"][0]["id"], 42)
        self.assertEqual(out["episodes"][0]["topics"], ["real-topic"])
        self.assertEqual(out["topics"][0]["slug"], "real-topic")
        self.assertIsInstance(out["topics"][0]["age_days"], int)
        # episodes filtered on project (or COALESCE(project, scope)) — verify
        # the project value actually reached the WHERE clause params.
        episodes_call = cur.statements[1]
        self.assertIn("COALESCE(project, scope) = %s", episodes_call)
        self.assertEqual(cur.params[1], ("acme/widget", 5))
        self.assertIn("note:%", cur.statements[3])

    def test_note_topics_fill_remaining_budget_after_episode_linked_ones(self):
        """W4.3: khipu.notes.reconcile-mirrored `note:` topics carry no
        episode link at all, so they can only surface via their
        frontmatter->>'project' — this fills the topic budget with them
        once the episode-linked slugs run out."""
        episode_row = (42, dt.datetime(2026, 9, 3, 10, 0), "shipped the fix", [])
        note_row = ("note:some-note", "Some Note", dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc))
        results = [[], [episode_row], [note_row]]
        with mock.patch("khipu.embed._episode_schema_flags", return_value={
            "project": True, "deleted_at": True,
        }):
            out, cur = self._run(results, project="acme/widget")
        self.assertEqual(out["topics"], [{"slug": "note:some-note", "title": "Some Note", "age_days": 2}])
        notes_call = cur.statements[-1]
        self.assertIn("slug LIKE 'note:%%'", notes_call)
        self.assertIn("frontmatter->>'project' = %s", notes_call)
        self.assertEqual(cur.params[-1], ("acme/widget", 3))

    def test_no_note_topics_query_when_topic_budget_already_full(self):
        episode_rows_topics = ["t1", "t2", "t3"]
        episode_row = (42, dt.datetime(2026, 9, 3, 10, 0), "shipped the fix", episode_rows_topics)
        topic_rows = [
            (f"t{i}", f"Topic {i}", dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)) for i in (1, 2, 3)
        ]
        results = [[], [episode_row], topic_rows]
        with mock.patch("khipu.embed._episode_schema_flags", return_value={
            "project": True, "deleted_at": True,
        }):
            out, cur = self._run(results, project="acme/widget")
        self.assertEqual(len(out["topics"]), 3)
        self.assertEqual(len(cur.statements), 3, "no fourth (notes) query once the budget is full")

    def test_no_project_no_commitments_query_at_all(self):
        """project=None: commitments.list_owed must never run (there is
        nothing to filter it to) — only the episodes query (widened by
        host_session_id) runs."""
        with mock.patch("khipu.embed._episode_schema_flags", return_value={
            "project": True, "deleted_at": True, "parent_session_id": True,
        }):
            out, cur = self._run([[], []], project=None, host_session_id="claude_code:host-1")
        self.assertEqual(out["commitments"], [])
        self.assertFalse(any("FROM commitments" in s for s in cur.statements))

    def test_host_session_id_widens_the_episode_match_via_or(self):
        with mock.patch("khipu.embed._episode_schema_flags", return_value={
            "project": True, "deleted_at": True, "parent_session_id": True,
        }):
            out, cur = self._run(
                [[], []],
                project=None,
                host_session_id="claude_code:host-1",
            )
        episodes_call = cur.statements[-1]
        self.assertIn("parent_session_id = %s", episodes_call)
        self.assertEqual(out["episodes"], [])

    def test_parent_session_id_gated_when_column_missing_pre_0008(self):
        """fix 10: a pre-migration hub has no episodes.parent_session_id —
        referencing it unconditionally raises UndefinedColumn; gated behind
        the schema flag it must degrade to no widening instead."""
        with mock.patch("khipu.embed._episode_schema_flags", return_value={
            "project": True, "deleted_at": True, "parent_session_id": False,
        }):
            out, cur = self._run(
                [[], []],
                project=None,
                host_session_id="claude_code:host-1",
            )
        self.assertEqual(out["episodes"], [])
        self.assertFalse(cur.statements, "no episodes query at all: no clause could be built")

    def test_falls_back_to_scope_column_when_project_column_is_missing(self):
        """A DB not yet migrated past 0007: no episodes.project column, so
        the filter must fall back to `scope` directly (matching embed.
        hybrid_search's own COALESCE(project, scope) / scope fallback)."""
        episode_row = (7, dt.datetime(2026, 9, 3), "pre-migration row", [])
        with mock.patch("khipu.embed._episode_schema_flags", return_value={
            "project": False, "deleted_at": False,
        }):
            # Third result: the note-topics query (project is set, no
            # episode-linked topic slugs, so the notes lookup still fires).
            out, cur = self._run([[], [episode_row], []], project="acme/widget")
        episodes_call = cur.statements[1]
        self.assertIn("scope = %s", episodes_call)
        self.assertNotIn("COALESCE", episodes_call)
        self.assertEqual(out["episodes"][0]["id"], 7)

    def test_note_topics_query_runs_even_with_no_episode_linked_topic_slugs(self):
        """No episode names a topic slug at all — the episode-linked topics
        SELECT never runs (nothing to look up) — but note-topics (matched by
        project alone, not by any episode link) still does."""
        episode_row = (7, dt.datetime(2026, 9, 3), "no topics here", [])
        with mock.patch("khipu.embed._episode_schema_flags", return_value={
            "project": True, "deleted_at": True,
        }):
            out, cur = self._run([[], [episode_row], []], project="acme/widget")
        self.assertEqual(out["topics"], [])
        # commitments, episodes, note-topics — three queries, none of them
        # the (skipped) episode-linked-slug lookup.
        self.assertEqual(len(cur.statements), 3)
        self.assertIn("note:%", cur.statements[-1])


if __name__ == "__main__":
    unittest.main()
