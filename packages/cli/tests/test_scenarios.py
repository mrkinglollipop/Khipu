# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""The mandatory scenario suite (Phase 1, session B;
docs/plans/2026-09-27-memory-reasoning-scope.md, "Local evaluation and
acceptance"). One test (or small class) per scenario in that list, run on the
local recall path end to end against ``tests.fixtures.corpus`` — no database,
no network. A scenario two behind (commitments) uses the fake cursor from
``tests.test_commitments``, the same style ``test_commitments_contract.py``
already uses for hub-shaped reads.

A scenario whose feature does not exist yet is written against the NAMED
future contract the scope document gives (``khipu.validity``-shaped keys,
``prior_work_meta["outcome"]``, etc.) and marked ``xfail(strict=True)`` with
the phase that is expected to land it — never weakened to pass early.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from khipu import commitments as co
from khipu import extract
from khipu import hub_snapshot as hs
from khipu import outbox
from khipu import recall_prompt as rp
from tests.fixtures import corpus
from tests.test_commitments import _CommitmentsCursor


# ---- 1. prior approved work --------------------------------------------------

def test_prior_approved_work_is_recalled(tmp_path):
    with corpus.installed_corpus(tmp_path):
        result = rp.prior_work_for_prompt(
            "what did we approve for the widget-batching endpoint pagination", cwd=None
        )
    assert result["reason"].startswith("ok")
    assert any(h["kind"] == "episode" and h["id"] == "1" for h in result["hits"])


# ---- 2. explicit reversal ----------------------------------------------------

def test_explicit_reversal_marks_the_earlier_decision_superseded(tmp_path):
    with corpus.installed_corpus(tmp_path):
        result = rp.prior_work_for_prompt(
            "widget-batching endpoint rollout deployment strategy blue-green rolling",
            cwd=None,
        )
    hit = next(h for h in result["hits"] if h["id"] == "2")
    assert hit["validity"]["state"] == "superseded"


# ---- 3. historical / as-of question (xfail: time_interpretation unwired) ----

@pytest.mark.xfail(strict=True, reason="Phase 3A")
def test_an_as_of_question_returns_the_historical_value(tmp_path):
    as_of_now = datetime(2026, 10, 15, tzinfo=timezone.utc)
    with corpus.installed_corpus(tmp_path, now=as_of_now):
        result = rp.prior_work_for_prompt(
            "what was the rocket launch fee as of September 2026", cwd=None
        )
    assert result["time_interpretation"]["as_of"].startswith("2026-09")
    ids = [h["id"] for h in result["hits"]]
    assert "4" in ids and (ids.index("4") < ids.index("5") if "5" in ids else True)


# ---- 4. same wording in different projects (rank boost, not a hard filter) --

def test_same_wording_ranks_the_matching_project_first(tmp_path):
    with corpus.installed_corpus(tmp_path):
        result = rp._snapshot_search_hits(
            "three second retry backoff sync job", project="acme/widget"
        )
    hits = rp._apply_score_floor(result["hits"])[:3]
    assert hits, "expected at least one hit"
    assert hits[0]["kind"] == "episode" and hits[0]["id"] == "6"


# ---- 5. indirect graph relationship (xfail: graph_candidates unwired) ------

@pytest.mark.xfail(strict=True, reason="Phase 3A")
def test_an_indirect_wiki_linked_topic_surfaces_as_a_graph_candidate(tmp_path):
    with corpus.installed_corpus(tmp_path):
        result = rp.prior_work_for_prompt(
            "billing-service module dependencies", cwd=None
        )
    assert any(
        h["kind"] == "topic" and h["id"] == "rate-limits" and h.get("via") == "topic:billing-service"
        for h in result["hits"]
    )


# ---- 6. exact command, error text and path ----------------------------------

def test_an_exact_error_and_path_are_recalled_literally(tmp_path):
    with corpus.installed_corpus(tmp_path):
        result = rp.prior_work_for_prompt(
            "FileNotFoundError hub snapshot missing var khipu hub_snapshot.sqlite", cwd=None
        )
    assert any(h["id"] == "9" for h in result["hits"])


# ---- 7. no relevant memory ---------------------------------------------------

def test_no_relevant_memory_abstains_rather_than_fabricating(tmp_path):
    # A real nearest-neighbour cosine leg always returns SOMETHING (whatever
    # is least-dissimilar) — that is a property of nearest-neighbour search
    # itself, not a bug this suite is testing for. Abstention is exercised
    # with no active embedding profile, so the only leg is lexical, against a
    # query sharing no token with anything in the corpus.
    with corpus.installed_corpus(tmp_path, active_profile=False):
        result = rp.prior_work_for_prompt(
            "spreadsheet macro recalculation freeze pane workbook", cwd=None
        )
    assert result["hits"] == []
    assert result["context"] == ""


# ---- 8. ambiguous / conflicting evidence ------------------------------------

def test_conflicting_decisions_are_both_surfaced_not_silently_resolved(tmp_path):
    with corpus.installed_corpus(tmp_path):
        result = rp.prior_work_for_prompt(
            "legacy webhook endpoint decision acme rocket", cwd=None
        )
    ids = {h["id"] for h in result["hits"]}
    assert {"10", "11"} <= ids


# ---- 9. mixed current/superseded episode -------------------------------------

def test_a_mixed_episode_is_not_discarded_but_marked_mixed(tmp_path):
    with corpus.installed_corpus(tmp_path):
        result = rp.prior_work_for_prompt(
            "billing database migration cutover rolling", cwd=None
        )
    hit = next(h for h in result["hits"] if h["id"] == "12")
    assert hit["validity"]["state"] == "mixed"


# ---- 10. stale / offline replica and reconnection ---------------------------

def test_a_stale_replica_is_unusable_and_a_refreshed_one_recovers(tmp_path):
    now = datetime.now(timezone.utc)
    with corpus.installed_corpus(tmp_path, now=now) as handle:
        corpus.rewrite_meta(handle, refreshed_at=now - timedelta(hours=30))
        fresh, _health = hs.snapshot_is_fresh()
        assert fresh is False
        with pytest.raises(rp._SnapshotUnusable):
            rp._snapshot_search_hits("widget-batching endpoint pagination", project=None)

        corpus.rewrite_meta(handle, refreshed_at=datetime.now(timezone.utc))
        fresh_again, _health2 = hs.snapshot_is_fresh()
        assert fresh_again is True
        result = rp._snapshot_search_hits("widget-batching endpoint pagination", project=None)
        assert any(h["id"] == "1" for h in result["hits"])


# ---- 11. changed revision under an existing id ------------------------------

def test_dedup_key_changes_when_a_rows_validity_revision_changes():
    row_v1 = {"kind": "episode", "id": "13", "validity": {"state": "current", "revision": 1}}
    row_v2 = {"kind": "episode", "id": "13", "validity": {"state": "current", "revision": 2}}
    assert rp._hit_ids([row_v1]) != rp._hit_ids([row_v2])


# ---- 12. deferred commitment -------------------------------------------------

def test_a_deferred_commitment_surfaces_as_owed_with_an_until_line():
    cur = _CommitmentsCursor(migrated=True)
    payload = {"project": "acme/widget", "open_loops": [
        {"text": "Ship the widget-batching rollout once the staging soak finishes.",
         "kind": "followup", "owner": "assistant"},
    ]}
    assert co.open_from_episode(cur, payload, 501) == 1
    row = co.list_owed(cur, project="acme/widget")[0]
    assert row["future_trigger"] is True
    assert row["until"] is not None and row["until"].startswith("until:")


# ---- 13. existing artifact ---------------------------------------------------

def test_an_existing_artifact_is_fetchable_by_id(tmp_path):
    with corpus.installed_corpus(tmp_path):
        episode = hs.episode_detail_snapshot(1)
        assert episode is not None
        assert "cursor-based pagination" in episode["summary"]
        topic = hs.topic_detail_snapshot("widget-batching")
        assert topic is not None and topic["slug"] == "widget-batching"


# ---- 14. duplicate / cross-harness recapture ---------------------------------

def test_a_recapture_from_a_different_harness_dedups_not_duplicates():
    cur = _CommitmentsCursor(migrated=True)
    text = "Ship the widget-batching rollout once the staging soak finishes."
    assert co.open_from_episode(cur, {"project": "acme/widget", "open_loops": [text]}, 601) == 1
    # A second harness (a different episode/session) restates the identical
    # text — must touch the existing row, never insert a second one.
    assert co.open_from_episode(cur, {"project": "acme/widget", "open_loops": [text]}, 602) == 0
    assert len(cur.rows) == 1
    row = next(iter(cur.rows.values()))
    assert row["seen_count"] == 2


# ---- 15. source deletion ----------------------------------------------------

def test_a_forgotten_episode_is_not_returned_by_any_reader(tmp_path):
    with corpus.installed_corpus(tmp_path) as handle:
        deleted = handle.by_id(15)
        assert deleted.deleted_at is not None
        assert hs.episode_detail_snapshot(15) is None


# ---- 16. quota failure --------------------------------------------------------

def test_an_embedding_quota_failure_degrades_to_lexical_only_not_empty(tmp_path):
    with corpus.installed_corpus(tmp_path):
        with mock.patch.object(rp, "_cached_query_embed", side_effect=RuntimeError("quota exceeded")):
            result = rp._snapshot_search_hits(
                "widget-batching endpoint cursor-based pagination", project=None
            )
    assert result["degraded"] == "embedding error"
    assert "cosine" not in result["legs"]
    assert any(h["id"] == "1" for h in result["hits"])


# ---- 17. malformed extraction -------------------------------------------------

def test_malformed_model_output_raises_instead_of_silently_dropping_the_window():
    with mock.patch.object(extract, "_generate", return_value="not a JSON object at all"):
        with pytest.raises(RuntimeError):
            extract.extract_memory("some transcript")


# ---- 18. crash / retry during consolidation -----------------------------------

def test_a_crash_between_enqueue_and_drain_neither_loses_nor_duplicates(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_OUTBOX", str(tmp_path / "outbox"))
    payload = {"ts": "2026-09-01T00:00:00Z", "summary": "durable capture across a crash"}
    outbox.enqueue(payload, reason="pg write failed")
    # A retry after a crash mid-drain re-queues the same payload before the
    # first job was ever drained — identity-keyed enqueue must not duplicate it.
    outbox.enqueue(payload, reason="retry after crash")
    assert outbox.status()["pending"] == 1

    calls: list[str] = []

    def fake_write_pg(p):
        calls.append(p["summary"])
        return {"episode_inserted": True, "topics_written": 0}

    with mock.patch("khipu.capture.write_pg", fake_write_pg), \
         mock.patch("khipu.embed.embed_on_capture", lambda p: True):
        out = outbox.drain()
    assert out == {"jobs": 1, "replayed": 1, "failed": 0, "stopped_early": False}
    assert calls == ["durable capture across a crash"]
    assert outbox.status()["pending"] == 0
