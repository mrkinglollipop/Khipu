"""Real-SQL integration tests against a separately provisioned disposable database.

Require KHIPU_LIVE_TESTS=1, a 32-character lowercase hexadecimal
KHIPU_SCRATCH_NONCE, and a loopback/temporary-socket KHIPU_SCRATCH_DSN whose
database is named khipu_scratch_<nonce>. Provision public.khipu_test_owner
with one text column, nonce, containing exactly the matching nonce row.
The shared guard validates that sentinel before any migration or deletion.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

import pytest

from tests import connect_scratch, validate_scratch_dsn

SCRATCH_DSN = os.environ.get("KHIPU_SCRATCH_DSN")



if SCRATCH_DSN and os.environ.get("KHIPU_LIVE_TESTS") == "1":
    validate_scratch_dsn(SCRATCH_DSN)

pytestmark = pytest.mark.skipif(
    not SCRATCH_DSN or os.environ.get("KHIPU_LIVE_TESTS") != "1",
    reason="requires KHIPU_LIVE_TESTS=1 and a provisioned nonce-scoped KHIPU_SCRATCH_DSN",
)


def _direct_connect():
    """A connection made with EXACTLY the given DSN — never khipu.db's own
    Keychain/config-file resolution, so there is no path by which this
    could silently reach a different database."""
    return connect_scratch(SCRATCH_DSN)


def _migration_0024_sql() -> str:
    from khipu import migrate

    for version, path in migrate.available():
        if version == "0024_decision_evidence":
            return path.read_text(encoding="utf-8")
    raise AssertionError("0024_decision_evidence.sql not found under ops/migrations")


def _apply_migration_0024() -> None:
    with _direct_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(_migration_0024_sql())
        conn.commit()


def _drop_migration_0024() -> None:
    """Undo 0024's additions for the "before the migration" tests. Only
    ever touches columns/table this migration itself added."""
    with _direct_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS decision_links")
            for col in ("source_kind", "evidence", "superseded_at", "supersede_source",
                        "supersede_reason", "retracted_at", "retract_reason"):
                cur.execute(f"ALTER TABLE decisions DROP COLUMN IF EXISTS {col}")
            cur.execute(
                "DELETE FROM schema_migrations WHERE version = '0024_decision_evidence'"
            )
        conn.commit()
        from khipu import db as _db

        _db._TABLE_COLUMNS_CACHE.clear()


@pytest.fixture(autouse=True)
def _ensure_migration_applied():
    """0024 is idempotent (IF NOT EXISTS / ON CONFLICT DO NOTHING
    throughout), so re-applying it before every test makes test order
    irrelevant — including after the dedicated "before migration" test,
    which restores it itself but this is a second, harmless guarantee."""
    _apply_migration_0024()
    from khipu import db as _db

    _db._TABLE_COLUMNS_CACHE.clear()
    yield


def _seed_project() -> str:
    """An invented, non-private project name (test_repo_hygiene.py enforces
    this) — never a real one."""
    return "acme/scratch-widget"


class DecisionsApiScratchTest(unittest.TestCase):
    """Every khipu.decisions API function against real Postgres."""

    def setUp(self):
        self.conn = _direct_connect()
        self.cur = self.conn.cursor()
        self.created_ids: list[int] = []
        self.episode_id: int | None = None

    def tearDown(self):
        # Never trust a half-finished test to have cleaned up: roll back any
        # aborted transaction first (a raised refusal can leave one), then
        # delete every row this test created, by id, regardless of what
        # happened above.
        self.conn.rollback()
        if self.created_ids:
            self.cur.execute(
                "DELETE FROM decisions WHERE id = ANY(%s)", (self.created_ids,)
            )
        if self.episode_id is not None:
            self.cur.execute("DELETE FROM episodes WHERE id = %s", (self.episode_id,))
        self.conn.commit()
        self.cur.close()
        self.conn.close()








class LinkLifecycleScratchTest(unittest.TestCase):
    def setUp(self):
        self.conn = _direct_connect()
        self.cur = self.conn.cursor()
        self.created_ids: list[int] = []

    def tearDown(self):
        self.conn.rollback()
        if self.created_ids:
            self.cur.execute("DELETE FROM decisions WHERE id = ANY(%s)", (self.created_ids,))
            self.conn.commit()
        self.cur.close()
        self.conn.close()





class DetectReversalsScratchTest(unittest.TestCase):
    """Real Postgres, real pg_trgm ``similarity()`` — the SQL half of
    historical reversal detection (BUILD item 3) the fake-cursor tests in
    test_decisions.py cannot exercise: that fake has no ``pg_extension``
    catalog at all, so it always takes the Python token-overlap fallback.
    ``decisions.episode_id`` has a real FK to ``episodes(id)``, so every
    test here needs an actual (invented) episode row, same pattern as
    ``DecisionsApiScratchTest.test_decision_states_for_episode_via_its_own_connection``.
    """

    def setUp(self):
        self.conn = _direct_connect()
        self.cur = self.conn.cursor()
        self.created_ids: list[int] = []
        self.episode_id: int | None = None

    def tearDown(self):
        self.conn.rollback()
        if self.created_ids:
            self.cur.execute("DELETE FROM decisions WHERE id = ANY(%s)", (self.created_ids,))
        if self.episode_id is not None:
            self.cur.execute("DELETE FROM episodes WHERE id = %s", (self.episode_id,))
        self.conn.commit()
        self.cur.close()
        self.conn.close()

    def _seed_episode(self, project: str) -> int:
        self.cur.execute(
            "INSERT INTO episodes (ts, session_id, summary, scope, project, topics, "
            "people, decisions, preferences) VALUES "
            "(now(), 'claude_code:scratch', 'detect_reversals scratch episode', "
            "%s, %s, '[]', '[]', '[]', '[]') RETURNING id",
            (project, project),
        )
        eid = self.cur.fetchone()[0]
        self.episode_id = eid
        return eid






class ForgetCascadeScratchTest(unittest.TestCase):
    """forget.forget_everywhere against real SQL: episode, commitment,
    deliverable and decision rows all cascade. Uses khipu.db.connect()
    (via KHIPU_DATABASE_URL) rather than a hand-held cursor, so this is the
    one test exercising the actual write path a real forget goes through."""

    def setUp(self):
        self.conn = _direct_connect()
        self.cur = self.conn.cursor()
        self.episode_id: int | None = None
        self.commitment_id: int | None = None
        self.deliverable_id: int | None = None
        self.decision_id: int | None = None

    def tearDown(self):
        self.conn.rollback()
        for table, col, val in (
            ("deliverables", "id", self.deliverable_id),
            ("commitments", "id", self.commitment_id),
            ("decisions", "id", self.decision_id),
            ("memory_embeddings", "ref", str(self.episode_id) if self.episode_id else None),
            ("episodes", "id", self.episode_id),
        ):
            if val is not None:
                self.cur.execute(f"DELETE FROM {table} WHERE {col} = %s", (val,))
        self.conn.commit()
        self.cur.close()
        self.conn.close()



class PreMigrationDegradeScratchTest(unittest.TestCase):
    """The same readers/writers, real SQL, with migration 0024 dropped —
    every gate degrades instead of raising. Restores 0024 in tearDown
    regardless of outcome (belt-and-suspenders with the autouse fixture on
    the NEXT test, which re-applies it unconditionally too)."""

    def setUp(self):
        _drop_migration_0024()
        self.conn = _direct_connect()
        self.cur = self.conn.cursor()
        self.created_ids: list[int] = []

    def tearDown(self):
        self.conn.rollback()
        if self.created_ids:
            self.cur.execute("DELETE FROM decisions WHERE id = ANY(%s)", (self.created_ids,))
            self.conn.commit()
        self.cur.close()
        self.conn.close()
        _apply_migration_0024()






class ToolPairScratchTest(unittest.TestCase):
    """archived registry reader / archived registry writer through handle_message —
    the actual JSON-RPC dispatch, against real Postgres."""

    def setUp(self):
        self.conn = _direct_connect()
        self.cur = self.conn.cursor()
        self.created_ids: list[int] = []
        self.env_patch = mock.patch.dict(os.environ, {"KHIPU_DATABASE_URL": SCRATCH_DSN})
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        self.conn.rollback()
        if self.created_ids:
            self.cur.execute("DELETE FROM decisions WHERE id = ANY(%s)", (self.created_ids,))
            self.conn.commit()
        self.cur.close()
        self.conn.close()

    def _call(self, name: str, arguments: dict) -> dict:
        import json

        from khipu.mcp_server import handle_message

        resp = handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": name, "arguments": arguments}}
        )
        body = json.loads(resp["result"]["content"][0]["text"])
        self.assertFalse(resp["result"]["isError"], body)
        return body




class ValidityScratchTest(unittest.TestCase):
    """Phase 2, session B: hub-side validity counts (decisions.enrich_
    search_results' three-way breakdown) and the replica's decision export/
    sync (hub_snapshot), both against real Postgres."""

    def setUp(self):
        self.conn = _direct_connect()
        self.cur = self.conn.cursor()
        self.created_ids: list[int] = []
        self.episode_id: int | None = None
        self.env_patch = mock.patch.dict(os.environ, {"KHIPU_DATABASE_URL": SCRATCH_DSN})
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        self.conn.rollback()
        if self.created_ids:
            self.cur.execute("DELETE FROM decisions WHERE id = ANY(%s)", (self.created_ids,))
        if self.episode_id is not None:
            self.cur.execute("DELETE FROM episodes WHERE id = %s", (self.episode_id,))
        self.conn.commit()
        self.cur.close()
        self.conn.close()


    def _seed_episode(self) -> int:
        project = _seed_project()
        self.cur.execute(
            "INSERT INTO episodes (ts, session_id, summary, scope, project, topics, "
            "people, decisions, preferences) VALUES "
            "(now(), 'claude_code:scratch', 'validity scratch episode', "
            "%s, %s, '[]', '[]', '[]', '[]') RETURNING id",
            (project, project),
        )
        episode_id = self.cur.fetchone()[0]
        self.episode_id = episode_id
        self.conn.commit()
        return episode_id


class GraphCandidatesScratchTest(unittest.TestCase):
    """Phase 3, session A: khipu.graph_candidates.hub_candidates against
    real Postgres — the one place this suite exercises the JSONB-aware
    ``jsonb_array_elements_text`` join (episodes.topics is JSONB, migration
    0001, not a native array) and the ``edges`` ANY(%s)/UNION query; a fake
    cursor could not stand in for either without re-implementing them."""

    def setUp(self):
        self.conn = _direct_connect()
        self.cur = self.conn.cursor()
        self.topic_slugs: list[str] = []
        self.episode_ids: list[int] = []

    def tearDown(self):
        self.conn.rollback()
        if self.topic_slugs:
            node_ids = [f"topic:{s}" for s in self.topic_slugs]
            self.cur.execute(
                "DELETE FROM edges WHERE src = ANY(%s) OR dst = ANY(%s)",
                (node_ids, node_ids),
            )
            self.cur.execute("DELETE FROM nodes WHERE id = ANY(%s)", (node_ids,))
            self.cur.execute("DELETE FROM topics WHERE slug = ANY(%s)", (self.topic_slugs,))
        if self.episode_ids:
            self.cur.execute("DELETE FROM episodes WHERE id = ANY(%s)", (self.episode_ids,))
        self.conn.commit()
        self.cur.close()
        self.conn.close()

    def _seed_topic(self, slug: str, title: str, body: str) -> None:
        self.cur.execute(
            "INSERT INTO topics (slug, title, body, created_at) VALUES (%s, %s, %s, now())",
            (slug, title, body),
        )
        self.topic_slugs.append(slug)
        self.conn.commit()

    def _seed_episode(self, summary: str, topics: list[str], *, days_ago: int = 0) -> int:
        import json

        project = _seed_project()
        self.cur.execute(
            "INSERT INTO episodes (ts, session_id, summary, scope, project, topics, "
            "people, decisions, preferences) VALUES "
            "(now() - (%s * interval '1 day'), 'claude_code:scratch', %s, %s, %s, %s::jsonb, "
            "'[]', '[]', '[]') RETURNING id",
            (days_ago, summary, project, project, json.dumps(topics)),
        )
        eid = self.cur.fetchone()[0]
        self.episode_ids.append(eid)
        self.conn.commit()
        return eid

    def test_topic_seed_finds_its_wiki_linked_neighbor(self):
        from khipu import graph_candidates as gc

        self._seed_topic("scratch-gc-billing", "Billing", "billing service body")
        self._seed_topic("scratch-gc-ratelimits", "Rate limits", "rate limit body")
        self.cur.execute(
            "INSERT INTO nodes (id, type, name) VALUES (%s, 'topic', %s), (%s, 'topic', %s) "
            "ON CONFLICT (id) DO NOTHING",
            ("topic:scratch-gc-billing", "scratch-gc-billing",
             "topic:scratch-gc-ratelimits", "scratch-gc-ratelimits"),
        )
        self.cur.execute(
            "INSERT INTO edges (src, dst, type, weight) VALUES (%s, %s, 'wiki_link', 1.0)",
            ("topic:scratch-gc-billing", "topic:scratch-gc-ratelimits"),
        )
        self.conn.commit()

        import time

        fused = [{"kind": "topic", "id": "scratch-gc-billing", "score": 1.0}]
        deadline = time.monotonic() + gc.HUB_LEG_DEADLINE_S
        candidates, missed = gc.hub_candidates(self.cur, fused, deadline=deadline)
        self.assertFalse(missed)
        hit = next(c for c in candidates if c["id"] == "scratch-gc-ratelimits")
        self.assertEqual(hit["kind"], "topic")
        self.assertEqual(
            hit["via"], {"seed": "topic:scratch-gc-billing", "relation": "wiki_link"}
        )

    def test_episode_seed_finds_the_topics_it_names(self):
        from khipu import graph_candidates as gc

        self._seed_topic("scratch-gc-syncjob", "Sync job", "sync job body")
        eid = self._seed_episode("episode naming the sync job", ["Scratch Gc Syncjob"])

        import time

        fused = [{"kind": "episode", "id": str(eid), "score": 1.0}]
        deadline = time.monotonic() + gc.HUB_LEG_DEADLINE_S
        candidates, missed = gc.hub_candidates(self.cur, fused, deadline=deadline)
        self.assertFalse(missed)
        self.assertTrue(
            any(c["kind"] == "topic" and c["id"] == "scratch-gc-syncjob" for c in candidates)
        )

    def test_topic_seed_finds_recent_episodes_naming_it_case_and_punctuation_insensitively(self):
        """The label an episode carries ("Scratch GC Webhook!!") is not the
        topic's slug ("scratch-gc-webhook") — this join only succeeds if the
        hub-side SQL slugification (regexp_replace mirror of
        topic_graph.topic_slug_from_label) actually folds them to the same
        string, which only real Postgres can prove."""
        from khipu import graph_candidates as gc

        self._seed_topic("scratch-gc-webhook", "Webhook", "webhook body")
        newest = self._seed_episode(
            "newest webhook mention", ["Scratch GC Webhook!!"], days_ago=1
        )
        self._seed_episode("older webhook mention", ["scratch_gc_webhook"], days_ago=5)
        self._seed_episode("oldest webhook mention", ["Scratch-GC-Webhook"], days_ago=10)

        import time

        fused = [{"kind": "topic", "id": "scratch-gc-webhook", "score": 1.0}]
        deadline = time.monotonic() + gc.HUB_LEG_DEADLINE_S
        candidates, missed = gc.hub_candidates(self.cur, fused, deadline=deadline)
        self.assertFalse(missed)
        episode_hits = [c for c in candidates if c["kind"] == "episode"]
        self.assertEqual({h["id"] for h in episode_hits}, {str(e) for e in self.episode_ids})
        # Most recent first (recency tiebreak).
        self.assertEqual(episode_hits[0]["id"], str(newest))

    def test_per_seed_cap_keeps_only_three_of_more_than_three_matches(self):
        from khipu import graph_candidates as gc

        self._seed_topic("scratch-gc-capped", "Capped", "capped body")
        for i in range(4):
            self._seed_episode(f"mention {i}", ["Scratch GC Capped"], days_ago=i)

        import time

        fused = [{"kind": "topic", "id": "scratch-gc-capped", "score": 1.0}]
        deadline = time.monotonic() + gc.HUB_LEG_DEADLINE_S
        candidates, missed = gc.hub_candidates(self.cur, fused, deadline=deadline)
        self.assertFalse(missed)
        self.assertEqual(len(candidates), gc.PER_SEED_CAP)

    def test_a_row_already_in_the_fused_list_is_never_offered_again(self):
        from khipu import graph_candidates as gc

        self._seed_topic("scratch-gc-billing2", "Billing2", "billing body")
        self._seed_topic("scratch-gc-ratelimits2", "Rate limits2", "rate limit body")
        self.cur.execute(
            "INSERT INTO nodes (id, type, name) VALUES (%s, 'topic', %s), (%s, 'topic', %s) "
            "ON CONFLICT (id) DO NOTHING",
            ("topic:scratch-gc-billing2", "scratch-gc-billing2",
             "topic:scratch-gc-ratelimits2", "scratch-gc-ratelimits2"),
        )
        self.cur.execute(
            "INSERT INTO edges (src, dst, type, weight) VALUES (%s, %s, 'wiki_link', 1.0)",
            ("topic:scratch-gc-billing2", "topic:scratch-gc-ratelimits2"),
        )
        self.conn.commit()

        import time

        fused = [
            {"kind": "topic", "id": "scratch-gc-billing2", "score": 1.0},
            {"kind": "topic", "id": "scratch-gc-ratelimits2", "score": 0.9},
        ]
        deadline = time.monotonic() + gc.HUB_LEG_DEADLINE_S
        candidates, missed = gc.hub_candidates(self.cur, fused, deadline=deadline)
        self.assertFalse(missed)
        self.assertFalse(any(c["id"] == "scratch-gc-ratelimits2" for c in candidates))

    def test_an_expired_deadline_drops_the_whole_leg(self):
        from khipu import graph_candidates as gc

        self._seed_topic("scratch-gc-deadline", "Deadline", "deadline body")
        self._seed_topic("scratch-gc-deadline-nb", "Neighbor", "neighbor body")
        self.cur.execute(
            "INSERT INTO nodes (id, type, name) VALUES (%s, 'topic', %s), (%s, 'topic', %s) "
            "ON CONFLICT (id) DO NOTHING",
            ("topic:scratch-gc-deadline", "scratch-gc-deadline",
             "topic:scratch-gc-deadline-nb", "scratch-gc-deadline-nb"),
        )
        self.cur.execute(
            "INSERT INTO edges (src, dst, type, weight) VALUES (%s, %s, 'wiki_link', 1.0)",
            ("topic:scratch-gc-deadline", "topic:scratch-gc-deadline-nb"),
        )
        self.conn.commit()

        import time

        fused = [{"kind": "topic", "id": "scratch-gc-deadline", "score": 1.0}]
        candidates, missed = gc.hub_candidates(self.cur, fused, deadline=time.monotonic() - 1)
        self.assertTrue(missed)
        self.assertEqual(candidates, [])


    def test_forgotten_episodes_are_neither_seeds_nor_mentions(self):
        from khipu import graph_candidates as gc

        self._seed_topic("scratch-gc-forgot", "Forgot", "forgot body")
        kept = self._seed_episode("kept mention", ["Scratch GC Forgot"], days_ago=1)
        gone = self._seed_episode("forgotten mention", ["Scratch GC Forgot"], days_ago=0)
        self.cur.execute("UPDATE episodes SET deleted_at = now() WHERE id = %s", (gone,))
        self.conn.commit()

        import time

        cands, missed = gc.hub_candidates(
            self.cur, [{"kind": "topic", "id": "scratch-gc-forgot"}],
            deadline=time.monotonic() + gc.HUB_LEG_DEADLINE_S)
        self.assertFalse(missed)
        self.assertEqual([c["id"] for c in cands if c["kind"] == "episode"], [str(kept)])
        cands, missed = gc.hub_candidates(
            self.cur, [{"kind": "episode", "id": str(gone)}],
            deadline=time.monotonic() + gc.HUB_LEG_DEADLINE_S)
        self.assertEqual((cands, missed), ([], False))

    def test_mixed_seeds_in_one_pipelined_statement_restore_the_timeout(self):
        from khipu import graph_candidates as gc

        self._seed_topic("scratch-gc-mix-a", "Mix A", "a body")
        self._seed_topic("scratch-gc-mix-b", "Mix B", "b body")
        self.cur.execute(
            "INSERT INTO nodes (id, type, name) VALUES (%s, 'topic', %s), (%s, 'topic', %s) "
            "ON CONFLICT (id) DO NOTHING",
            ("topic:scratch-gc-mix-a", "scratch-gc-mix-a",
             "topic:scratch-gc-mix-b", "scratch-gc-mix-b"),
        )
        self.cur.execute(
            "INSERT INTO edges (src, dst, type, weight) VALUES (%s, %s, 'wiki_link', 1.0)",
            ("topic:scratch-gc-mix-a", "topic:scratch-gc-mix-b"),
        )
        eid = self._seed_episode("names mix b", ["Scratch-GC-Mix-B"])
        self.cur.execute("SET statement_timeout = '7s'")

        import time

        fused = [{"kind": "topic", "id": "scratch-gc-mix-a"}, {"kind": "episode", "id": str(eid)},
                 {"kind": "topic", "id": "note:scratch-not-a-slug"}]
        cands, missed = gc.hub_candidates(
            self.cur, fused, deadline=time.monotonic() + gc.HUB_LEG_DEADLINE_S)
        self.assertFalse(missed)
        got = {(c["kind"], c["id"], c["via"]["seed"], c["via"]["relation"]) for c in cands}
        self.assertIn(("topic", "scratch-gc-mix-b", "topic:scratch-gc-mix-a", "wiki_link"), got)
        self.cur.execute("SELECT current_setting('statement_timeout')")
        self.assertEqual(self.cur.fetchone()[0], "7s")

    def test_a_statement_past_the_deadline_is_cancelled_and_reported_missed(self):
        from khipu import graph_candidates as gc

        self._seed_topic("scratch-gc-slow", "Slow", "slow body")
        import time

        sql, params = gc._hub_candidates_sql(
            gc._seeds([{"kind": "topic", "id": "scratch-gc-slow"}]), set(), live_episodes=True)
        slow = sql.replace("WITH ", "WITH slow AS MATERIALIZED (SELECT pg_sleep(1)), ", 1) + \
            " UNION ALL SELECT 0, 'topic', 'x', 'wiki_link', 'topic', 'y', NULL, NULL, 0 FROM slow"
        with mock.patch.object(gc, "_hub_candidates_sql", return_value=(slow, params)):
            cands, missed = gc.hub_candidates(
                self.cur, [{"kind": "topic", "id": "scratch-gc-slow"}],
                deadline=time.monotonic() + 0.2)
        self.assertEqual((cands, missed), ([], True))
        self.cur.execute("SELECT 1")
        self.assertEqual(self.cur.fetchone()[0], 1)


class EmbedHybridSearchGraphCandidatesScratchTest(unittest.TestCase):
    """khipu.embed.hybrid_search's graph_candidates wiring, end to end
    against real Postgres — not just khipu.graph_candidates.hub_candidates
    in isolation. Confirms the switch-off invariant (no neighbor, no new
    keys) alongside the switch-on behavior in the same real connection."""

    def setUp(self):
        self.conn = _direct_connect()
        self.cur = self.conn.cursor()
        self.topic_slugs: list[str] = []
        self.hub_patch = mock.patch("khipu.hub_snapshot.try_hub_connect", side_effect=_direct_connect)
        self.hub_patch.start()

    def tearDown(self):
        self.hub_patch.stop()
        self.conn.rollback()
        if self.topic_slugs:
            node_ids = [f"topic:{s}" for s in self.topic_slugs]
            self.cur.execute(
                "DELETE FROM edges WHERE src = ANY(%s) OR dst = ANY(%s)",
                (node_ids, node_ids),
            )
            self.cur.execute("DELETE FROM nodes WHERE id = ANY(%s)", (node_ids,))
            self.cur.execute("DELETE FROM topics WHERE slug = ANY(%s)", (self.topic_slugs,))
        self.conn.commit()
        self.cur.close()
        self.conn.close()

    _SEED_SLUG = "scratch-gc-hybrid-seed-9f2e"
    _NEIGHBOR_SLUG = "scratch-gc-hybrid-nbr-8a1c"

    def _seed_linked_topics(self) -> None:
        # The seed's marker token ("zzqwidgetalpha") must not appear ANYWHERE
        # on the neighbor row (slug included — a shared slug substring would
        # let plain literal ILIKE find it too, defeating the isolation this
        # test needs between "found via the graph leg" and "found anyway").
        self.cur.execute(
            "INSERT INTO topics (slug, title, body, created_at) VALUES "
            "(%s, 'Seed topic', 'zzqwidgetalpha marker body', now()), "
            "(%s, 'Unrelated neighbor topic', 'totally unrelated content', now())",
            (self._SEED_SLUG, self._NEIGHBOR_SLUG),
        )
        self.topic_slugs = [self._SEED_SLUG, self._NEIGHBOR_SLUG]
        self.cur.execute(
            "INSERT INTO nodes (id, type, name) VALUES (%s, 'topic', %s), (%s, 'topic', %s) "
            "ON CONFLICT (id) DO NOTHING",
            (f"topic:{self._SEED_SLUG}", self._SEED_SLUG,
             f"topic:{self._NEIGHBOR_SLUG}", self._NEIGHBOR_SLUG),
        )
        self.cur.execute(
            "INSERT INTO edges (src, dst, type, weight) VALUES (%s, %s, 'wiki_link', 1.0)",
            (f"topic:{self._SEED_SLUG}", f"topic:{self._NEIGHBOR_SLUG}"),
        )
        self.conn.commit()

    def test_switch_off_never_adds_the_neighbor_or_new_keys(self):
        from khipu import embed

        self._seed_linked_topics()
        with mock.patch("khipu.features.enabled", return_value=False):
            payload = embed.hybrid_search("zzqwidgetalpha marker", mode="literal", kind="topic", limit=5)
        ids = {r["id"] for r in payload["results"]}
        self.assertIn(self._SEED_SLUG, ids)
        self.assertNotIn(self._NEIGHBOR_SLUG, ids)
        self.assertNotIn("degraded_legs", payload)
        self.assertNotIn("time_interpretation", payload)


if __name__ == "__main__":
    unittest.main()
