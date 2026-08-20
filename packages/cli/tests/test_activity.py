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


if __name__ == "__main__":
    unittest.main()
