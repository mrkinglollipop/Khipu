# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""Unit tests for khipu.graph_candidates' replica (SQLite) backend, against
the synthetic corpus (tests.fixtures.corpus) — no database, no network. The
hub (PostgreSQL) backend is covered separately, with real SQL, in
tests/test_pg_scratch.py::GraphCandidatesScratchTest (this backend's
``jsonb_array_elements_text``/regexp_replace mirror needs a real server; the
fake-cursor style used elsewhere in this suite could not meaningfully stand
in for it).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from khipu import graph_candidates as gc
from khipu import hub_snapshot as hs
from tests.fixtures import corpus


def test_topic_seed_finds_its_wiki_linked_neighbor(tmp_path):
    with corpus.installed_corpus(tmp_path):
        con = hs.open_snapshot()
        fused = [{"kind": "topic", "id": "billing-service", "score": 1.0}]
        deadline = time.monotonic() + gc.REPLICA_LEG_DEADLINE_S
        candidates, missed = gc.replica_candidates(con, fused, deadline=deadline)
    assert missed is False
    hit = next(c for c in candidates if c["id"] == "rate-limits")
    assert hit["kind"] == "topic"
    assert hit["via"] == {"seed": "topic:billing-service", "relation": "wiki_link"}


def test_episode_seed_finds_the_topic_it_names(tmp_path):
    with corpus.installed_corpus(tmp_path):
        con = hs.open_snapshot()
        fused = [{"kind": "episode", "id": "1", "score": 1.0}]
        deadline = time.monotonic() + gc.REPLICA_LEG_DEADLINE_S
        candidates, missed = gc.replica_candidates(con, fused, deadline=deadline)
    assert missed is False
    hit = next(c for c in candidates if c["id"] == "widget-batching")
    assert hit["kind"] == "topic"
    assert hit["via"] == {"seed": "episode:1", "relation": "capture_topic"}


def test_topic_seed_finds_the_episodes_that_name_it(tmp_path):
    with corpus.installed_corpus(tmp_path):
        con = hs.open_snapshot()
        fused = [{"kind": "topic", "id": "sync-job", "score": 1.0}]
        deadline = time.monotonic() + gc.REPLICA_LEG_DEADLINE_S
        candidates, missed = gc.replica_candidates(con, fused, deadline=deadline)
    assert missed is False
    episode_ids = {c["id"] for c in candidates if c["kind"] == "episode"}
    assert {"6", "7"} <= episode_ids  # both named episodes for the sync-job topic


def test_a_row_already_in_the_fused_list_is_never_offered_again(tmp_path):
    with corpus.installed_corpus(tmp_path):
        con = hs.open_snapshot()
        fused = [
            {"kind": "topic", "id": "billing-service", "score": 1.0},
            {"kind": "topic", "id": "rate-limits", "score": 0.9},
        ]
        deadline = time.monotonic() + gc.REPLICA_LEG_DEADLINE_S
        candidates, missed = gc.replica_candidates(con, fused, deadline=deadline)
    assert missed is False
    assert not any(c["id"] == "rate-limits" for c in candidates)


def test_a_forgotten_episode_is_never_offered_as_a_recent_mention(tmp_path):
    with corpus.installed_corpus(tmp_path) as handle:
        deleted = handle.by_id(15)  # deleted_at set — its only topic is scratch-notes
        assert deleted.deleted_at is not None
        con = hs.open_snapshot()
        fused = [{"kind": "topic", "id": "scratch-notes", "score": 1.0}]
        deadline = time.monotonic() + gc.REPLICA_LEG_DEADLINE_S
        candidates, missed = gc.replica_candidates(con, fused, deadline=deadline)
    assert missed is False
    # scratch-notes has no wiki links and its only naming episode is
    # tombstoned — the leg must find nothing, not silently substitute it.
    assert candidates == []


def test_per_seed_cap_keeps_only_three_of_more_than_three_matches(tmp_path):
    # webhook-endpoint carries no wiki_link edge (unlike sync-job, which is
    # itself wiki-linked from retry-policy) — isolates the (c) capture_topic
    # leg's own cap from the (a) wiki_link leg sharing the same seed slots.
    now = datetime.now(timezone.utc)
    extra = [
        corpus.Episode(
            id=2001 + i,
            ts=(now - timedelta(days=i)).isoformat(),
            summary=f"extra webhook-endpoint mention {i}",
            project="acme/rocket",
            session_id=f"claude_code:extra-{i}",
            harness="claude_code",
            topics=["Webhook Endpoint"],
        )
        for i in range(4)
    ]
    with corpus.installed_corpus(tmp_path, extra_episodes=extra):
        con = hs.open_snapshot()
        fused = [{"kind": "topic", "id": "webhook-endpoint", "score": 1.0}]
        deadline = time.monotonic() + gc.REPLICA_LEG_DEADLINE_S
        candidates, missed = gc.replica_candidates(con, fused, deadline=deadline)
    assert missed is False
    # 6 total episodes name webhook-endpoint now (2 named + 4 extra); capped at 3.
    assert len(candidates) == gc.PER_SEED_CAP
    # Recency wins the tiebreak: the freshest of the extras (days_ago=0) leads.
    assert candidates[0]["id"] == "2001"


def test_total_cap_across_multiple_seeds(tmp_path):
    with corpus.installed_corpus(tmp_path):
        con = hs.open_snapshot()
        # Five topic seeds, each with a wiki-linked neighbor and/or recent
        # episodes naming it — enough raw candidates to exceed TOTAL_CAP.
        fused = [
            {"kind": "topic", "id": "billing-service", "score": 1.0},
            {"kind": "topic", "id": "deployment-strategy", "score": 0.9},
            {"kind": "topic", "id": "pricing-policy", "score": 0.8},
            {"kind": "topic", "id": "retry-policy", "score": 0.7},
            {"kind": "topic", "id": "sync-job", "score": 0.6},
        ]
        deadline = time.monotonic() + gc.REPLICA_LEG_DEADLINE_S
        candidates, missed = gc.replica_candidates(con, fused, deadline=deadline)
    assert missed is False
    assert len(candidates) <= gc.TOTAL_CAP


def test_an_expired_deadline_drops_the_whole_leg(tmp_path):
    with corpus.installed_corpus(tmp_path):
        con = hs.open_snapshot()
        fused = [{"kind": "topic", "id": "billing-service", "score": 1.0}]
        candidates, missed = gc.replica_candidates(con, fused, deadline=time.monotonic() - 1)
    assert missed is True
    assert candidates == []


def test_no_topic_or_episode_seeds_is_a_no_op():
    candidates, missed = gc.replica_candidates(
        None, [{"kind": "node", "id": "path:foo/bar", "score": 1.0}],
        deadline=time.monotonic() + gc.REPLICA_LEG_DEADLINE_S,
    )
    assert missed is False
    assert candidates == []
