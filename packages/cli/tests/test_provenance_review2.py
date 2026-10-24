# ruff: noqa: F811 - pytest fixtures imported from test_provenance_forget
"""Second Codex Astra review of feat/fact-provenance (2026-10-23,
jobs/2026-10-23/astra-prov2): its reproductions, ported. Each failed on
bb0be6de.

1. P1 a forgotten merged source bypasses the anchor fence, including
   through an already-held commitment anchor;
2. P1 registry publication races between the source check and the
   derivative commit, after which the stream skips repair;
3. P2 concurrent gap replay duplicates a fact;
4. P2 a prefix of failing gap groups starves every later one;
5. P2 the writer gate refuses the current JSONL recovery writers.

FRESHNESS_OVERLAP_S=0 compresses an open capture older than the ordinary
overlap into one deterministic interleaving, as in the first review's port
(tests/test_provenance_review_replica.py).
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import pytest

from khipu import capture, deliverables, hub_snapshot as hs, mirror, outbox, provenance, tombstones
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.test_provenance_forget import env, pg, PROJECT, _capture, _payload  # noqa: F401
from tests.test_provenance_replica import replica, _replica, _replica_readers  # noqa: F401


def _seed_commitment(conn, eid, marker):
    fid = conn.execute(
        "INSERT INTO commitments (text,project,owner,kind,opened_episode,content_hash) "
        "VALUES (%s,%s,'agent','followup',%s,%s) RETURNING id",
        (marker, PROJECT, eid, marker),
    ).fetchone()[0]
    provenance.record_claim(conn.cursor(), "commitment", fid, "text", marker, episode_id=eid)
    provenance.project(conn.cursor(), "commitment", fid)
    return fid


# ---- 1. a held anchor gains a forgotten merged source ----------------------------


@pytest.mark.parametrize("kind", ["decision", "deliverable", "commitment"])
def test_held_anchor_gains_forgotten_merged_source_behind_episode_cursor(pg, replica, monkeypatch, kind):
    """A supported capture merge adds a private source to a public episode
    after the replica read that episode. The merge's change stamp falls
    behind the episode cursor, so only the facts carry the new source."""
    replica(pg)
    monkeypatch.setattr(hs, "FRESHNESS_OVERLAP_S", 0)
    eid = _capture(loop=None)
    marker = "Review PRIVATE-HELD-ANCHOR-WORDS before release"
    private = _payload(loop=marker if kind == "commitment" else None,
                       decision=marker if kind == "decision" else None,
                       title=marker if kind == "deliverable" else None,
                       summary="A private merged source", session_id="codex:held-private")
    slow = pg.connect()
    try:
        assert capture._merge_into_episode(slow.cursor(), eid, private, matched_via="review", score=1.0)
        assert hs.refresh()["ok"]
        assert marker not in _replica_readers()
        tombstones.publish_payloads([private])
        slow.commit()
        for _ in range(2):
            pulled = hs.pull_changes(include_ledger=False)
            assert pulled["ok"], pulled
        assert marker not in _replica_readers(), "merged source was omitted from the anchor identity gate"
    finally:
        slow.rollback()
        slow.close()


# ---- 2. publication between the source check and the derivative commit ------------


@pytest.mark.parametrize("kind", ["decision", "deliverable", "commitment"])
def test_source_publication_after_anchor_check_cannot_race_derivative_write(pg, replica, monkeypatch, kind):
    """The source is published right after the replica's registry check and
    before its SQLite commit. Public sync and pull paths only."""
    replica(pg)
    monkeypatch.setattr(hs, "FRESHNESS_OVERLAP_S", 0)
    payload = {"ts": "2026-10-20T12:00:00Z", "summary": "source forgotten during apply",
               "harness": "codex", "session_id": "codex:derivative-check-race",
               "transcript_range": "0:10", "project": PROJECT}
    marker = "PRIVATE-REGISTRY-CHECK-RACE"
    with pg.connect() as conn:
        eid = conn.execute(
            "INSERT INTO episodes (ts,summary,raw,harness,session_id,transcript_range,project) "
            "VALUES (%s,%s,%s::jsonb,%s,%s,%s,%s) RETURNING id",
            (payload["ts"], payload["summary"], json.dumps(payload), payload["harness"],
             payload["session_id"], payload["transcript_range"], PROJECT),
        ).fetchone()[0]
        if kind == "commitment":
            _seed_commitment(conn, eid, marker)
        elif kind == "decision":
            fid = conn.execute("INSERT INTO decisions(project,text,episode_id) VALUES(%s,%s,%s) RETURNING id",
                               (PROJECT, marker, eid)).fetchone()[0]
            provenance.record_claim(conn.cursor(), kind, fid, "text", marker, episode_id=eid)
            provenance.project(conn.cursor(), kind, fid)
        else:
            fid = conn.execute("INSERT INTO deliverables(project,kind,path,title,episode_id) "
                               "VALUES(%s,'file','race.md',%s,%s) RETURNING id", (PROJECT, marker, eid)).fetchone()[0]
            provenance.record_claim(conn.cursor(), kind, fid, "title", marker, episode_id=eid)
            provenance.project(conn.cursor(), kind, fid)
        # The capture stays open across the dump: its episode change stamp
        # falls behind the dump, its facts stay in the stream.
        assert hs.refresh()["ok"]
        assert _replica("SELECT id FROM episodes") == []
        conn.commit()
    # Since the 2026-10-24 ruling the check is the wait rule's hub read
    # (read_refusal_wait); the source is published right after it.
    checked = hs.read_refusal_wait
    intercepted = []

    def publish_after_check(*args, **kwargs):
        result = checked(*args, **kwargs)
        if not intercepted:
            intercepted.append(True)
            tombstones.publish_payloads([payload])
        return result

    monkeypatch.setattr(hs, "read_refusal_wait", publish_after_check)
    out = hs.pull_changes(include_ledger=False) if kind == "commitment" else hs.sync_fact_changes()
    assert out["ok"], out
    assert intercepted
    assert tombstones.is_forgotten(payload)
    assert marker not in _replica_readers(), "registry fence published before derivative commit was missed"
    # The cursor moved past this fact's change: later public reads must not
    # bring it back either.
    assert hs.sync_fact_changes()["ok"]
    assert hs.pull_changes(include_ledger=False)["ok"]
    assert marker not in _replica_readers()


# ---- 3 and 4. gap replay ----------------------------------------------------------


def _gap_capture(payload, kind):
    original = provenance.record_claim

    def broken(cur, item_kind, *a, **kw):
        if item_kind == kind:
            raise RuntimeError("synthetic derivation failure")
        return original(cur, item_kind, *a, **kw)

    with mock.patch.object(provenance, "record_claim", broken):
        return capture.write_pg(payload)["episode_id"]


def test_two_gap_replayers_do_not_duplicate_a_deliverable(pg, monkeypatch):
    """Two nightlies (two Macs) replay the same gap at once. The barrier
    holds each at the claim until the other arrives, or briefly when the
    other never does (it skipped the claimed gap)."""
    eid = _gap_capture(_payload(loop=None, title="one release checklist"), "deliverable")
    barrier = threading.Barrier(2)
    real = deliverables._claim

    def claim_after_both_insert(cur, fid, episode_id, title):
        try:
            barrier.wait(timeout=3)
        except threading.BrokenBarrierError:
            pass
        return real(cur, fid, episode_id, title)

    monkeypatch.setattr(deliverables, "_claim", claim_after_both_insert)

    def replay():
        with pg.connect() as conn:
            conn.execute("SET statement_timeout = '15s'")
            conn.commit()
            return provenance.replay_gaps(conn)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result(timeout=30) for f in [pool.submit(replay) for _ in range(2)]]
    with pg.connect() as conn:
        facts = conn.execute("SELECT id, episode_id, title FROM deliverables ORDER BY id").fetchall()
        gaps = conn.execute("SELECT resolved_at IS NOT NULL, resolution FROM fact_gaps").fetchall()
    assert all(r["failed"] == 0 for r in results), results
    assert sum(r["replayed"] for r in results) == 1, results
    assert len(facts) == 1 and facts[0][1] == eid, (facts, gaps)
    assert gaps == [(True, "replayed")]


def test_poison_gap_prefix_does_not_starve_healthy_new_gap(pg):
    real = provenance.record_claim

    def fail_all(cur, kind, *a, **kw):
        if kind == "decision":
            raise RuntimeError("synthetic first-pass failure")
        return real(cur, kind, *a, **kw)

    ids = []
    with mock.patch.object(provenance, "record_claim", fail_all):
        for i in range(provenance.GAP_REPLAY_LIMIT + 1):
            ids.append(capture.write_pg(_payload(
                loop=None, decision=f"Use release policy {i} for subsystem {i}"))["episode_id"])
    healthy = ids[-1]
    attempted = []

    def fail_prefix(cur, kind, *a, **kw):
        if kind == "decision":
            attempted.append(kw.get("episode_id"))
            if kw.get("episode_id") != healthy:
                raise RuntimeError("persistent malformed old input")
        return real(cur, kind, *a, **kw)

    with mock.patch.object(provenance, "record_claim", fail_prefix):
        with pg.connect() as conn:
            provenance.replay_gaps(conn)
            provenance.replay_gaps(conn)
    with pg.connect() as conn:
        facts = conn.execute("SELECT count(*) FROM decisions WHERE episode_id=%s", (healthy,)).fetchone()[0]
    assert healthy in attempted and facts == 1


# ---- 5. current JSONL recovery writers --------------------------------------------


@pytest.mark.parametrize("method", ["sync_recent_episodes", "reconcile_memory_root"])
def test_current_file_recovery_can_import_fact_bearing_episode(pg, method):
    """A current recovery writer imports a missing fact-bearing file row
    with its claims (the writer gate refused it on bb0be6de)."""
    pg.memory.mkdir()
    payload = _payload(loop=None, decision="Keep the release train weekly")
    (pg.memory / "episodes.jsonl").write_text(json.dumps(payload) + "\n")
    getattr(mirror, method)(pg.memory)
    with pg.connect() as conn:
        rows = conn.execute("SELECT summary FROM episodes").fetchall()
        claims = conn.execute("SELECT count(*) FROM fact_claims WHERE kind='decision'").fetchone()[0]
    assert rows == [(payload["summary"],)]
    assert claims == 1


def test_current_legacy_mirror_deferral_has_durable_outbox_recovery(pg):
    """Control: a bare mirror refusal is queued by mirror_after_capture."""
    payload = _payload(loop=None, decision="Keep the release train weekly")
    assert mirror.mirror_after_capture(payload) is None
    assert len(outbox.jobs()) == 1
    report = outbox.drain()
    assert report["replayed"] == 1 and not outbox.jobs(), report
    with pg.connect() as conn:
        assert conn.execute("SELECT count(*) FROM episodes").fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM fact_claims WHERE kind='decision'").fetchone() == (1,)
