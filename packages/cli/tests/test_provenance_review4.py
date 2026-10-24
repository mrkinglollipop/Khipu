# ruff: noqa: F811 - pytest fixtures imported from test_provenance_forget
"""Fourth Codex Astra review of feat/fact-provenance (2026-10-24,
jobs/2026-10-23/astra-prov4): its reproductions, ported, against the
replica rebuild rule (owner decision 2026-10-24, design section 6).

1. P1 a delayed commitment vector restores forgotten anchor wording after
   successful pulls (both the explicit-read_at and the catch-up shapes);
2. P1 a local forget of a non-anchor contributor leaves its restatement
   readable on the replica (with and without a prior registry);
3. P2 file recovery at a held legacy key drops a line whose decision
   details (rationale, reversal evidence) differ;
4. P2 concurrent replay of two different gap groups duplicates a
   deliverable.

Plus the review's PR/owner control with its overbroad final assertion
corrected as the review describes (it searched all of ``pr_check``,
including the waiting keys section 3.3 keeps): a refused contributor's PR
number and ref drop out, the owner's PR stays, the hub is unchanged.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest import mock

import pytest

from khipu import capture, deliverables, embed, forget, hub_snapshot as hs, mirror, outbox, provenance, tombstones
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.test_provenance_forget import ALT, CANON, env, pg, _capture, _payload, _readers  # noqa: F401
from tests.test_provenance_replica import hub_forget, replica, _replica, _replica_readers  # noqa: F401
from tests.test_provenance_review2 import _gap_capture


# ---- 1. P1 a delayed commitment vector --------------------------------------------


def _delayed_vector(cid, text):
    """The shape ``embed_recent_missing`` publishes, gathered before the
    forget: no wording hash from a current snapshot."""
    return [dict(profile="review-local", kind="commitment", ref=str(cid), chunk_idx=0,
                 chunk_text=text, content_hash=embed._md5(text), embedding=[1.0, 0.0])]


def test_delayed_vector_cannot_restore_old_anchor_wording(pg, replica):
    replica(pg)
    pg.memory.mkdir()
    a = _capture(loop=CANON)
    b = _capture(loop=ALT)
    with pg.connect() as c:
        cid, text = c.execute("SELECT id, text FROM commitments").fetchone()
    assert text == CANON
    delayed = _delayed_vector(cid, text)
    read_at = time.time()
    assert hs.refresh()["ok"]
    forgotten = forget.forget_everywhere(a, memory_root=pg.memory, gateway=False)
    assert forgotten["ok"], forgotten
    assert hs.pull_changes(include_ledger=False)["ok"]
    assert _replica("SELECT opened_episode, text FROM commitments") == [(b, ALT)]
    out = hs.upsert_embeddings(delayed, read_at=read_at)
    assert out["ok"], out
    pulls = [hs.pull_changes(include_ledger=False) for _ in range(2)]
    rows = _replica("SELECT ref, chunk_text FROM memory_embeddings WHERE kind = 'commitment'")
    assert all(p["ok"] for p in pulls), pulls
    assert CANON not in str(rows), "delayed vector revived forgotten anchor wording after successful pulls"


def test_catchup_delayed_publication_cannot_restore_forgotten_text(pg, replica):
    replica(pg)
    pg.memory.mkdir()
    a = _capture(loop=CANON)
    b = _capture(loop=ALT)
    assert hs.refresh()["ok"]
    with pg.connect() as c:
        cid, text = c.execute("SELECT id, text FROM commitments").fetchone()
    assert text == CANON
    rows = _delayed_vector(cid, text)
    # The catch-up's own shape now carries the wording it embedded; read
    # before the forget, it names the forgotten wording.
    stamped = [{**r, "fact_wording": embed._md5(text)} for r in rows]
    out = forget.forget_everywhere(a, memory_root=pg.memory, gateway=False)
    assert out["ok"], out
    assert hs.pull_changes(include_ledger=False)["ok"]
    assert _replica("SELECT opened_episode, text FROM commitments") == [(b, ALT)]
    for publication in (hs.upsert_embeddings(rows), hs.upsert_embeddings(stamped)):
        assert publication["ok"], publication
        assert publication["embeddings"] == 0, publication
    pulls = [hs.pull_changes(include_ledger=False) for _ in range(2)]
    assert all(x["ok"] for x in pulls), pulls
    spec = SimpleNamespace(id="review-local", provider="openai-compatible", model="review",
                           endpoint="http://127.0.0.1:9/v1", dim=2)
    with mock.patch.object(hs, "active_snapshot_profile_spec", return_value=spec), \
         mock.patch("khipu.models.show_models",
                    return_value={"embed": {"provider": "local", "endpoint": spec.endpoint, "model_id": spec.model}}), \
         mock.patch("khipu.profiles.register_spec"), mock.patch("khipu.embed.uses_task_prefixes", return_value=False), \
         mock.patch("khipu.embed.embed_one", return_value=[1.0, 0.0]):
        visible = hs.semantic_search_snapshot("deployment checklist", limit=20)
    assert CANON not in json.dumps(visible), "forgotten text returned by offline semantic reader after successful pulls"


def test_a_current_vector_publication_is_kept(pg, replica):
    """Control: a publication built from the wording the replica shows is
    stored, so the wording check refuses only stale ones."""
    replica(pg)
    _capture(loop=CANON)
    assert hs.refresh()["ok"]
    with pg.connect() as c:
        cid, text = c.execute("SELECT id, text FROM commitments").fetchone()
    rows = [{**r, "fact_wording": embed._md5(text)} for r in _delayed_vector(cid, text)]
    assert hs.upsert_embeddings(rows)["embeddings"] == 1
    assert _replica("SELECT chunk_text FROM memory_embeddings WHERE kind = 'commitment'") == [(CANON,)]


# ---- 2. P1 a local forget of a non-anchor contributor -----------------------------


@pytest.mark.parametrize("prior_registry", [False, True])
def test_forget_nonanchor_erases_its_wording_immediately(pg, replica, prior_registry):
    replica(pg)
    pg.memory.mkdir()
    _capture(loop=CANON)
    b = _capture(loop=ALT, decision="Independent contributor decision")
    if prior_registry:
        tombstones.publish_payloads([_payload(loop=None)])
    assert hs.refresh()["ok"]
    assert ALT in _replica_readers()
    result = forget.forget_everywhere(b, memory_root=pg.memory, gateway=False)
    before = _replica_readers()
    gate_result = hs.sync_fact_changes()
    after = _replica_readers()
    remote = _readers(pg)
    assert result["ok"], result
    assert gate_result["ok"], gate_result
    assert ALT not in remote
    assert CANON in before and CANON in after
    assert ALT not in before and ALT not in after, "forgotten contributor wording survives local forget and stream sync"


# ---- 3. P2 recovery at a held legacy key with different decision details ---------


@pytest.mark.parametrize("method", ["sync_recent_episodes", "reconcile_memory_root"])
def test_recovery_preserves_different_decision_details(pg, method):
    pg.memory.mkdir()
    text = "Use weekly release trains"
    a = _payload(loop=None, decision=text, decision_details=[{"text": text, "by": "user",
                                                               "rationale": "First rationale"}])
    b = {**a, "decision_details": [{"text": text, "by": "user", "rationale": "SECOND_RATIONALE_UNIQUE",
                                    "reverses": "Old policy"}]}
    (pg.memory / "episodes.jsonl").write_text(json.dumps(a) + "\n" + json.dumps(b) + "\n")
    for _ in range(2):
        getattr(mirror, method)(pg.memory)
    with pg.connect() as c:
        claims = c.execute("SELECT field, value FROM fact_claims WHERE kind = 'decision'").fetchall()
        gaps = c.execute("SELECT step FROM fact_gaps WHERE resolved_at IS NULL").fetchall()
    queued = [json.loads(p.read_text()) for p in outbox.jobs()]
    assert "SECOND_RATIONALE_UNIQUE" in str(claims) + str(queued) or gaps, \
        "distinct rationale/evidence was silently skipped"


# ---- 4. P2 two different gap groups, one deliverable ------------------------------


def test_distinct_gap_groups_do_not_duplicate_deliverable(pg, monkeypatch):
    """Two captures of the same deliverable each leave a gap; two replay
    workers run concurrently, one gap group each. The review held both
    workers at a barrier after each one's insert; a correct arbitration
    makes the second wait for the first, so here the barrier sits where
    the race opens instead: both workers reach the deliverable step (its
    identity claim) at the same moment."""
    for _ in range(2):
        _gap_capture(_payload(loop=None, title="Shared recovery checklist"), "deliverable")
    barrier = threading.Barrier(2)
    real = deliverables._claim_identities

    def together(cur, project, items):
        barrier.wait(timeout=10)
        return real(cur, project, items)

    monkeypatch.setattr(deliverables, "_claim_identities", together)

    def replay():
        with pg.connect() as c:
            c.execute("SET statement_timeout = '15s'")
            c.commit()
            return provenance.replay_gaps(c)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(replay) for _ in range(2)]
        results = [f.result(timeout=30) for f in futures]
    with pg.connect() as c:
        rows = c.execute("SELECT id, episode_id, title FROM deliverables ORDER BY id").fetchall()
        claims = c.execute("SELECT fact_id, episode_id FROM fact_claims WHERE kind = 'deliverable'").fetchall()
        open_gaps = c.execute("SELECT count(*) FROM fact_gaps WHERE resolved_at IS NULL").fetchone()[0]
    assert all(r["failed"] == 0 for r in results), results
    assert sum(r["replayed"] for r in results) == 2, results
    assert len(rows) == 1, "distinct gap groups duplicated the same logical deliverable"
    # Both episodes' claims sit on the one logical fact.
    assert {fid for fid, _e in claims} == {rows[0][0]} and len({e for _f, e in claims}) == 2, claims
    assert open_gaps == 0


# ---- the PR/owner control (corrected) ---------------------------------------------


def test_pr_and_owner_contributors_are_fenced_per_value(pg, replica):
    replica(pg)
    base = ("Review the deployment checklist before release and verify the staging deployment, release notes, "
            "rollback steps, monitoring checks, alerts, access controls, and release approval")
    _capture(loop=base)
    payload = _payload(loop=base + " in PR #41 and https://github.com/other/lib/pull/7")
    forgotten = capture.write_pg(payload)["episode_id"]
    with pg.connect() as c:
        facts = c.execute("SELECT id FROM commitments").fetchall()
        assert len(facts) == 1, facts
        fid = facts[0][0]
        # The supported claim API supplies owner support for exactly PR 7.
        provenance.record_claim(c.cursor(), "commitment", fid, "pr", "7", claimant="owner")
        provenance.project(c.cursor(), "commitment", fid)
    assert hs.refresh()["ok"]
    tombstones.publish_payloads([payload])
    assert hs.pull_changes(include_ledger=False)["ok"]
    # The wait rule (2026-10-24 ruling): no fact row while the hub still
    # holds the forgotten episode live.
    assert _replica("SELECT text FROM commitments") == [] and hs.facts_rebuilding()
    with pg.connect() as c:
        assert c.execute("SELECT prs FROM commitments").fetchone()[0] == [7, 41]
    # The hub's forget re-projects per value: the owner's PR 7 stays.
    hub_forget(pg, forgotten)
    assert hs.pull_changes(include_ledger=False)["ok"]
    after = _replica("SELECT text, latest_text, prs, pr_check FROM commitments")
    assert len(after) == 1 and after[0][0] == base and after[0][1] is None, after
    assert json.loads(after[0][2]) == [7], after
    refs = (json.loads(after[0][3]) or {}).get("refs") if after[0][3] else None
    assert not refs or all("other/lib" not in str(r) and "41" not in str(r) for r in refs), after
