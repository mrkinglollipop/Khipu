"""A forgotten episode (``episodes.deleted_at`` set) is a tombstone: every
reader that shows, counts for display, re-derives or re-embeds episodes must
leave it out. Each test seeds one forgotten episode and one live control
episode in a small in-memory table whose fake cursor applies the tombstone
predicate only when the reader's SQL asks for it, so a reader that forgets
the filter shows the forgotten row.

Readers that must see tombstones (capture dedup, the forget cascade, drift
parity, replica upserts, row-count parity on copies) are deliberately left
unfiltered and are not covered here."""
from __future__ import annotations

import datetime as dt
import re
import unittest
from unittest import mock

from khipu import db

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 15, 12, 0, tzinfo=UTC)

_EPISODE_COLUMNS = (
    "id", "ts", "ingested_at", "session_id", "scope", "summary", "topics",
    "people", "decisions", "preferences", "edges", "raw", "harness", "repo_root",
    "project", "parent_session_id", "transcript_range", "tags", "deleted_at",
    "window_id", "truncated_chars", "verbatim",
)


def _episodes():
    """The forgotten episode is the NEWER one, so any recency-ordered reader
    that skips the filter shows it first."""
    return [
        {"id": 1, "ts": NOW - dt.timedelta(hours=2), "ingested_at": NOW - dt.timedelta(hours=2),
         "session_id": "claude_code:live", "scope": "repo", "summary": "live control",
         "topics": ["live-topic"], "decisions": ["keep the live decision"], "preferences": [],
         "people": [], "project": "acme", "deleted_at": None},
        {"id": 2, "ts": NOW - dt.timedelta(hours=1), "ingested_at": NOW - dt.timedelta(hours=1),
         "session_id": "claude_code:gone", "scope": "repo", "summary": "forgotten secret",
         "topics": ["forgotten-topic"], "decisions": ["the forgotten decision"], "preferences": [],
         "people": [], "project": "acme", "deleted_at": NOW - dt.timedelta(minutes=5)},
    ]


def _live_only(sql: str) -> bool:
    """Whether the episodes part of ``sql`` excludes tombstones."""
    return re.search(r"(?:\be\.)?deleted_at IS NULL", sql) is not None


class _EpisodeTableCur:
    """Answers the episode reads the readers under test issue, against
    ``_episodes()``. Unknown statements return no rows."""

    def __init__(self, episodes=None):
        self.episodes = episodes if episodes is not None else _episodes()
        self.statements: list[str] = []
        self._rows: list[tuple] = []
        self.rowcount = 0

    def _visible(self, sql: str) -> list[dict]:
        rows = self.episodes
        if _live_only(sql):
            rows = [e for e in rows if e["deleted_at"] is None]
        return rows

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "information_schema.columns" in s:
            table = (params or ("",))[0]
            self._rows = [(c,) for c in _EPISODE_COLUMNS] if table == "episodes" else []
            return
        self.statements.append(s)
        self._rows = self._answer(s, params)

    def _answer(self, s: str, params) -> list[tuple]:
        if s.startswith("SELECT 'episodes' AS t, COUNT(*) FROM episodes"):
            episode_part = s.split(" UNION ALL ")[0]
            return [("episodes", len(self._visible(episode_part))), ("topics", 0),
                    ("topics_tombstoned", 0), ("nodes", 0), ("edges", 0), ("embeddings", 0),
                    ("embeddings_legacy_nodes", 0), ("topic_revisions", 0)]
        if s.startswith("SELECT COUNT(*) FROM episodes"):
            return [(len(self._visible(s)),)]
        if s.startswith("SELECT MAX(ts), MAX(ingested_at), now() - MAX(ingested_at) FROM episodes"):
            rows = self._visible(s)
            return [(max(e["ts"] for e in rows), max(e["ingested_at"] for e in rows),
                     dt.timedelta(seconds=60))]
        if s.startswith("SELECT now(), MAX(ts), MAX(ingested_at) FROM episodes"):
            rows = self._visible(s)
            return [(NOW, max(e["ts"] for e in rows), max(e["ingested_at"] for e in rows))]
        if s.startswith("SELECT ts, ingested_at FROM episodes"):
            rows = sorted(self._visible(s), key=lambda e: e["ts"], reverse=True)
            return [(rows[0]["ts"], rows[0]["ingested_at"])] if rows else []
        if s.startswith("SELECT id, ts, ingested_at, session_id, scope, summary,"):
            rows = sorted(self._visible(s), key=lambda e: e["ingested_at"], reverse=True)
            return [(e["id"], e["ts"], e["ingested_at"], e["session_id"], e["scope"], e["summary"],
                     e["topics"], e["decisions"], e["preferences"], NOW - e["ingested_at"])
                    for e in rows[: params[0]]]
        if s.startswith("SELECT topics FROM episodes WHERE"):
            return [(e["topics"],) for e in self._visible(s) if str(e["id"]) == str(params[0])]
        if s.startswith("SELECT slug, title, body, updated_at, created_at FROM topics"):
            return [(slug, slug, "body", NOW, NOW) for slug in params["s"]]
        if s.startswith("SELECT id, project, session_id, decisions, ts FROM episodes"):
            rows = sorted(self._visible(s), key=lambda e: e["ts"])
            return [(e["id"], e["project"], e["session_id"], e["decisions"], e["ts"]) for e in rows]
        if s.startswith("SELECT summary, decisions, preferences, topics, people"):
            return [(e["summary"], e["decisions"], e["preferences"], e["topics"], e["people"], None)
                    for e in self._visible(s) if e["id"] == params[0]]
        if s.startswith("SELECT to_regclass"):
            return [(False,)]
        return []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _CacheCase(unittest.TestCase):
    def setUp(self):
        db._TABLE_COLUMNS_CACHE.clear()
        self.addCleanup(db._TABLE_COLUMNS_CACHE.clear)


class ActivityListTest(_CacheCase):
    def test_recent_episodes_leave_out_a_forgotten_episode(self):
        from khipu import activity

        cur = _EpisodeTableCur()
        with mock.patch.object(activity, "connect", return_value=_Conn(cur)):
            out = activity.recent_episodes(limit=10)
        self.assertEqual([r["id"] for r in out], [1])

    def test_activity_payload_counts_and_lists_only_live_episodes(self):
        from khipu import activity

        cur = _EpisodeTableCur()
        with mock.patch.object(activity, "connect", return_value=_Conn(cur)), \
                mock.patch("khipu.keychain.secrets_status", return_value={}):
            out = activity.activity_payload(limit=10)
        self.assertEqual([r["id"] for r in out["recent"]], [1])
        self.assertEqual(out["episode_count"], 1)

    def test_the_tombstone_filter_waits_for_the_column(self):
        """A pre-0010 hub has no deleted_at column: the list still answers."""
        from khipu import activity

        cur = _EpisodeTableCur(episodes=[_episodes()[0]])
        with mock.patch.object(activity, "connect", return_value=_Conn(cur)), \
                mock.patch.object(db, "table_columns",
                                  return_value=set(_EPISODE_COLUMNS) - {"deleted_at"}):
            out = activity.recent_episodes(limit=10)
        self.assertEqual([r["id"] for r in out], [1])
        self.assertFalse(any("deleted_at" in s for s in cur.statements))


class StatusRecentCapturesTest(_CacheCase):
    """doctor and the full khipu_status read drift.status_payload."""

    def _status(self):
        from khipu import activity, drift

        cur = _EpisodeTableCur()
        with mock.patch.object(drift, "connect", return_value=_Conn(cur)), \
                mock.patch.object(activity, "connect", return_value=_Conn(cur)):
            return drift.status_payload(None)

    def test_recent_captures_leave_out_a_forgotten_episode(self):
        out = self._status()
        self.assertEqual([r["id"] for r in out["recent_captures"]], [1])
        self.assertNotIn("forgotten secret", repr(out["recent_captures"]))

    def test_the_episode_count_is_live_episodes(self):
        """Same number the offline replica's status (its live view) and the
        embedding coverage total report."""
        self.assertEqual(self._status()["counts"]["episodes"], 1)

    def test_capture_liveness_still_sees_the_newest_capture(self):
        """Forgetting the newest capture must not make capture look stalled."""
        out = self._status()
        self.assertEqual(out["latest_ingested_at"], (NOW - dt.timedelta(hours=1)).isoformat())


class HubGraphEpisodeTopicsTest(_CacheCase):
    def test_a_forgotten_seed_episode_yields_no_topics(self):
        from khipu import graph_candidates as gc

        cur = _EpisodeTableCur()
        self.assertEqual(gc._hub_episode_topics(cur, "2", seed_rank=0, seed_key="episode:2"), [])
        live = gc._hub_episode_topics(cur, "1", seed_rank=0, seed_key="episode:1")
        self.assertEqual([r["id"] for r in live], ["live-topic"])


class DecisionBackfillTest(_CacheCase):
    def test_backfill_never_rederives_decisions_from_a_forgotten_episode(self):
        from khipu import decisions

        cur = _EpisodeTableCur()
        seen: list[int] = []

        def _insert(_cur, payload, eid):
            seen.append(eid)
            return len(payload["decisions"])

        with mock.patch.object(decisions, "_decisions_ready", return_value=True), \
                mock.patch.object(decisions, "insert_decisions_from_episode", side_effect=_insert):
            report = decisions.backfill_decisions(cur, apply=True)
        self.assertEqual(seen, [1])
        self.assertEqual(report["episodes_scanned"], 1)

    def test_the_dry_run_counts_only_live_episodes(self):
        from khipu import decisions

        cur = _EpisodeTableCur()
        with mock.patch.object(decisions, "_decisions_ready", return_value=True), \
                mock.patch.object(decisions, "_find_recent_duplicate", return_value=None):
            report = decisions.backfill_decisions(cur, apply=False)
        self.assertEqual(report["episodes_scanned"], 1)
        self.assertEqual(report["decisions_inserted"], 1)


class EpisodeEditReembedTest(_CacheCase):
    def _reembed(self, eid):
        from khipu import cli

        cur = _EpisodeTableCur()
        upserts: list = []
        with mock.patch("khipu.embed._active_profile", return_value="p"), \
                mock.patch("khipu.embed._api_texts", side_effect=lambda _p, items: items), \
                mock.patch("khipu.embed.embed_batch", side_effect=lambda texts, profile, **_kw: [[0.0]] * len(texts)), \
                mock.patch("khipu.embed._upsert_chunks", side_effect=lambda _c, _p, rows: upserts.extend(rows)):
            return cli._reembed_episode(cur, eid), upserts

    def test_a_forgotten_episode_gets_no_vectors_back(self):
        ok, upserts = self._reembed(2)
        self.assertFalse(ok)
        self.assertEqual(upserts, [])

    def test_a_live_episode_is_reembedded(self):
        ok, upserts = self._reembed(1)
        self.assertTrue(ok)
        self.assertTrue(upserts)
        self.assertTrue(all(r[1] == "1" for r in upserts))


if __name__ == "__main__":
    unittest.main()
