"""Fact provenance, phase 2: capture through claims.

Scope: docs/plans/2026-10-23-fact-provenance.md, sections 2 (I2, I4), 3.4 and
8 (phase 2). Every database is a hermetic one (tests/hermetic_pg.py).

* I4: a fact step that meets a deadlock, a serialization failure, a lock
  timeout or a statement timeout, or the hub's commit gate, rolls the whole
  capture back as ``FactStepDeferred``; the outbox keeps the job, never buries
  it, names the reason, and the retry lands it with its claims. Any other
  fact-step error fails open as before and leaves a ``fact_gaps`` row in the
  same transaction. Insert and merge paths both.
* I2 (capture only): generated schedules of three captures restating one
  commitment, decision and deliverable, with mixed kinds and owners, an owner
  reassign and a refused old-code writer; after every commit each projected
  field is one of its live claims' values and every capturing episode holds a
  claim on what it derived.
"""
from __future__ import annotations

import itertools
import json
from unittest import mock

import psycopg
import pytest
from psycopg import errors as pg_errors

from khipu import capture, commitments as co, decisions, deliverables, outbox, provenance
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401

PROJECT = "acme/widget"
_N = itertools.count(1)


@pytest.fixture
def pg(hub, monkeypatch):  # noqa: F811
    from khipu import db, embed, topic_graph

    hub.migrate()
    db._TABLE_COLUMNS_CACHE.clear()
    monkeypatch.setattr(db, "connect", lambda *a, **kw: hub.connect())
    monkeypatch.setattr(embed, "embed_on_capture", lambda *a, **kw: None)
    monkeypatch.setattr(embed, "embed_one", mock.Mock(side_effect=RuntimeError("no provider")))
    monkeypatch.setattr(topic_graph, "persist_capture_graph", lambda *a, **kw: {})
    monkeypatch.setattr(outbox, "_queued_origin", lambda: {"origin": None})
    monkeypatch.setattr(co, "_has_commitment_embeddings", lambda cur, ids: False)
    monkeypatch.setattr(capture, "_dedup_candidates", lambda cur, payload: [])
    return hub


def _payload(*, loop="Review the deployment checklist before release", kind="followup", owner="assistant",
             decision="Keep the release train weekly", title="the checklist", **extra) -> dict:
    n = next(_N)
    payload = {
        "ts": f"2026-10-23T12:{n // 60:02d}:{n % 60:02d}Z",
        "summary": f"synthetic capture {n} {'x' * (n % 7)} about release {n}",
        "project": PROJECT,
        "session_id": f"codex:prov-{n}",
        "harness": "codex",
        "transcript_range": f"0:{n}",
    }
    if loop is not None:
        payload["open_loops"] = [{"text": loop, "kind": kind, "owner": owner}]
    if decision is not None:
        payload["decisions"] = [decision]
    if title is not None:
        payload["deliverables"] = [{"kind": "file", "path": "docs/checklist.md", "title": title}]
    payload.update(extra)
    return payload


def _scalar(hub, sql, params=()):  # noqa: F811
    with hub.connect() as conn:
        return conn.execute(sql, params).fetchone()[0]


def _counts(hub):  # noqa: F811
    with hub.connect() as conn:
        return tuple(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                     for t in ("episodes", "commitments", "decisions", "deliverables", "fact_claims"))


# ---- I4: deferral, never a silent loss -------------------------------------------

STEP_KIND = {"commitments": "commitment", "decisions": "decision", "deliverables": "deliverable"}
TRANSIENT = [pg_errors.DeadlockDetected, pg_errors.SerializationFailure, pg_errors.LockNotAvailable,
             pg_errors.QueryCanceled]


def _failing_claims(monkeypatch, kind, exc_type):
    real = provenance.record_claim

    def record_claim(cur, k, *args, **kwargs):
        if k == kind:
            raise exc_type("synthetic fact-step failure")
        return real(cur, k, *args, **kwargs)

    monkeypatch.setattr(provenance, "record_claim", record_claim)
    return real


@pytest.mark.parametrize("exc_type", TRANSIENT, ids=lambda e: e.__name__)
@pytest.mark.parametrize("step", sorted(STEP_KIND))
def test_a_transient_fact_step_error_defers_the_whole_capture(pg, monkeypatch, step, exc_type):
    _failing_claims(monkeypatch, STEP_KIND[step], exc_type)
    with pytest.raises(capture.FactStepDeferred) as err:
        capture.write_pg(_payload())
    assert step in str(err.value) and exc_type.__name__ in str(err.value)
    assert _counts(pg) == (0, 0, 0, 0, 0)


def test_the_hub_gate_refusal_defers_the_capture(pg, monkeypatch):
    """A writer whose projection did not run (a stale readiness answer, or a
    bug) is refused at COMMIT; nothing lands and the retry is deferred."""
    monkeypatch.setattr(provenance, "project", lambda cur, kind, fact_id: "projected")
    with pytest.raises(capture.FactStepDeferred) as err:
        capture.write_pg(_payload())
    assert "refused unprojected write" in str(err.value)
    assert isinstance(err.value.__cause__, psycopg.OperationalError)
    assert provenance.is_old_writer_refusal(err.value.__cause__)
    assert _counts(pg) == (0, 0, 0, 0, 0)


@pytest.mark.parametrize("step", sorted(STEP_KIND))
def test_a_plain_bug_fails_open_and_leaves_a_fact_gap(pg, monkeypatch, step):
    _failing_claims(monkeypatch, STEP_KIND[step], RuntimeError)
    out = capture.write_pg(_payload())
    assert out["episode_inserted"]
    eid = out["episode_id"]
    with pg.connect() as conn:
        gaps = conn.execute("SELECT episode_id, step, error, resolved_at FROM fact_gaps").fetchall()
        assert gaps == [(eid, step, "RuntimeError: synthetic fact-step failure", None)]
        table = {"commitments": "commitments", "decisions": "decisions", "deliverables": "deliverables"}[step]
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        # The other steps landed, each with this episode's claims.
        for other, kind in STEP_KIND.items():
            if other == step:
                continue
            assert conn.execute("SELECT count(*) FROM fact_claims WHERE kind = %s AND episode_id = %s",
                                (kind, eid)).fetchone()[0] >= 1, other


@pytest.mark.parametrize("exc_type", [pg_errors.DeadlockDetected, pg_errors.QueryCanceled],
                         ids=lambda e: e.__name__)
def test_the_merge_path_defers_and_gaps_the_same_way(pg, monkeypatch, exc_type):
    target = capture.write_pg(_payload(loop=None, decision=None, title=None))["episode_id"]
    real = _failing_claims(monkeypatch, "commitment", exc_type)
    with pytest.raises(capture.FactStepDeferred):
        with pg.connect() as conn, conn.cursor() as cur:
            capture._merge_into_episode(cur, target, _payload(), matched_via="jaccard", score=0.9)
            conn.commit()
    assert _counts(pg)[1:] == (0, 0, 0, 0)

    monkeypatch.setattr(provenance, "record_claim", real)
    _failing_claims(monkeypatch, "commitment", RuntimeError)
    with pg.connect() as conn, conn.cursor() as cur:
        assert capture._merge_into_episode(cur, target, _payload(), matched_via="jaccard", score=0.9)
        conn.commit()
    with pg.connect() as conn:
        assert conn.execute("SELECT episode_id, step FROM fact_gaps").fetchall() == [(target, "merge commitments")]
        assert conn.execute("SELECT count(*) FROM fact_claims WHERE kind = 'decision' AND episode_id = %s",
                            (target,)).fetchone()[0] == 1


def test_a_lock_timeout_on_a_dedup_target_is_deferred_not_lost(pg, monkeypatch):
    """A real LockNotAvailable: another transaction holds the item a
    restatement folds into."""
    first = capture.write_pg(_payload(decision=None, title=None))
    assert first["episode_inserted"]
    with pg.connect() as holder:
        holder.execute("SELECT id FROM commitments FOR UPDATE")
        real_connect = pg.connect

        def connect(*a, **kw):
            conn = real_connect()
            conn.execute("SET lock_timeout = '200ms'")
            return conn

        from khipu import db

        monkeypatch.setattr(db, "connect", connect)
        with pytest.raises(capture.FactStepDeferred) as err:
            capture.write_pg(_payload(decision=None, title=None))
        assert "LockNotAvailable" in str(err.value)
        holder.rollback()
    assert _counts(pg)[0] == 1
    second = capture.write_pg(_payload(decision=None, title=None))
    assert second["episode_inserted"]
    with pg.connect() as conn:
        cid = conn.execute("SELECT id FROM commitments").fetchone()[0]
        got = conn.execute("SELECT field, episode_id FROM fact_claims WHERE kind = 'commitment' AND fact_id = %s "
                           "ORDER BY id", (cid,)).fetchall()
    assert got == [("text", first["episode_id"]), ("restatement", second["episode_id"])]


def test_the_outbox_keeps_a_deferred_capture_past_its_attempts_then_lands_it(pg, monkeypatch):
    real = _failing_claims(monkeypatch, "decision", pg_errors.DeadlockDetected)
    monkeypatch.setattr(outbox, "MAX_ATTEMPTS", 1)
    payload = _payload()
    outbox.enqueue(payload, reason="FactStepDeferred")
    for _ in range(3):
        report = outbox.drain()
        assert report["failed"] == 1 and report.get("deferred") == 1 and not report.get("buried"), report
        assert not report.get("stopped_early"), report
    (job_path,) = outbox.jobs()
    job = json.loads(job_path.read_text())
    assert job["fact_deferred"] is True and job["attempts"] == 3
    assert job["deferral_reason"].startswith("decisions: DeadlockDetected")
    assert not outbox.dead_jobs()
    assert _counts(pg) == (0, 0, 0, 0, 0)

    monkeypatch.setattr(provenance, "record_claim", real)
    assert outbox.drain()["replayed"] == 1
    assert outbox.jobs() == []
    episodes, commitments, decided, delivered, claims = _counts(pg)
    assert (episodes, commitments, decided, delivered) == (1, 1, 1, 1) and claims >= 3


# ---- I2: capture-only schedules ------------------------------------------------------


def _live_claims(cur, kind, fact_id):
    cur.execute(
        "SELECT fc.field, fc.value, fc.claimant, fc.episode_id FROM fact_claims fc "
        "LEFT JOIN episodes e ON e.id = fc.episode_id "
        "WHERE fc.kind = %s AND fc.fact_id = %s AND (fc.claimant = 'owner' "
        "OR (fc.claimant = 'episode' AND e.deleted_at IS NULL))",
        (kind, fact_id))
    return cur.fetchall()


def _assert_projection(hub):  # noqa: F811
    """Every projected field of every fact is one of its live claims' values
    (section 3.2), and no fact with a live claim is retired."""
    with hub.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, text, latest_text, prs, pr_check, opened_episode, status, close_reason "
                    "FROM commitments")
        for cid, text, latest, prs, check, anchor, status, reason in cur.fetchall():
            live = _live_claims(cur, "commitment", cid)
            assert live, f"commitment {cid} has no live claim"
            words = {(v, e) for f, v, c, e in live if f in ("text", "restatement")}
            assert text in {v for v, _e in words}, (cid, text, live)
            assert anchor in {e for _v, e in words}, (cid, anchor, live)
            assert latest is None or latest in {v for f, v, c, e in live if f == "restatement"}, (cid, latest)
            assert sorted(prs or []) == sorted({int(v) for f, v, c, e in live if f == "pr"}), (cid, prs, live)
            refs = (check or {}).get("refs") or []
            assert sorted(refs) == sorted({v for f, v, c, e in live if f == "pr_ref"}), (cid, refs, live)
            assert reason != "forgotten", cid
        cur.execute("SELECT id, text, episode_id, retract_reason FROM decisions")
        for did, text, anchor, reason in cur.fetchall():
            live = _live_claims(cur, "decision", did)
            assert text in {v for f, v, c, e in live if f == "text"}, (did, text, live)
            assert anchor in {e for f, v, c, e in live if f == "text"}, (did, anchor, live)
            assert reason != "forgotten", did
        cur.execute("SELECT id, title, episode_id FROM deliverables")
        for vid, title, anchor in cur.fetchall():
            live = _live_claims(cur, "deliverable", vid)
            assert live, vid
            assert title is None or title in {v for f, v, c, e in live if f == "title"}, (vid, title, live)
            assert anchor in {e for f, v, c, e in live}, (vid, anchor, live)


def _claimed_by(hub, eid):  # noqa: F811
    with hub.connect() as conn:
        return {k for (k,) in conn.execute("SELECT DISTINCT kind FROM fact_claims WHERE episode_id = %s",
                                           (eid,)).fetchall()}


CAPTURES = {
    # The opening words, an exact repeat, a paraphrase naming PRs and a
    # pull link, and a promise of the same thing the user owes.
    "A": dict(loop="Review the deployment checklist before release"),
    "B": dict(loop="Review the deployment checklist before release", decision="keep the release train  weekly",
              title="The checklist"),
    "C": dict(loop="Review the deployment checklist before the release in PR #41 and "
                   "https://github.com/other/lib/pull/7", decision="Keep the release train weekly",
              title=None),
    "U": dict(loop="I will review the deployment checklist before release", kind="promise", owner="user"),
}


def _old_writer(hub):  # noqa: F811
    """main's restatement write, without claims: refused at COMMIT."""
    with hub.connect() as conn:
        cid = conn.execute("SELECT min(id) FROM commitments").fetchone()[0]
        if cid is None:
            return
        conn.execute("UPDATE commitments SET latest_text = 'old writer words' WHERE id = %s", (cid,))
        with pytest.raises(psycopg.OperationalError) as err:
            conn.commit()
        assert provenance.is_old_writer_refusal(err.value)


def _reassign(hub):  # noqa: F811
    with hub.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT min(id) FROM commitments WHERE status = 'open'")
        cid = cur.fetchone()[0]
        if cid is not None:
            co.reassign(cur, cid, "agent", "reviewer", by=co.BY_CONVERSATION, reason="schedule")
        conn.commit()


SCHEDULES = [
    "ABC", "ACB", "BAC", "CAB", "CBA", "AUC", "UAB", "A!BC", "AB~C", "A~U!C", "CU~AB", "ABCABC",
]


@pytest.mark.parametrize("schedule", SCHEDULES)
def test_capture_schedules_project_only_live_claims(pg, schedule):
    seen: list[int] = []
    for step in schedule:
        if step == "!":
            _old_writer(pg)
        elif step == "~":
            _reassign(pg)
        else:
            out = capture.write_pg(_payload(**CAPTURES[step]))
            assert out["episode_inserted"], (schedule, step)
            eid = out["episode_id"]
            seen.append(eid)
            titled = CAPTURES[step].get("title", "default") is not None
            want = {"commitment", "decision"} | ({"deliverable"} if titled else set())
            assert _claimed_by(pg, eid) == want, (schedule, step)
        _assert_projection(pg)
    with pg.connect() as conn:
        # One decision and one deliverable for the whole schedule: each repeat
        # was a claim, never a second row.
        assert conn.execute("SELECT count(*) FROM decisions").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM deliverables").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM fact_gaps").fetchone()[0] == 0
        claimed = {e for (e,) in conn.execute(
            "SELECT DISTINCT episode_id FROM fact_claims WHERE kind = 'decision' AND claimant = 'episode'")}
        assert claimed == set(seen)
        # The first item's text holds, and A's exact repeat B restated it.
        if schedule.startswith("AB"):
            cid = conn.execute("SELECT min(id) FROM commitments").fetchone()[0]
            got = conn.execute("SELECT field, episode_id FROM fact_claims WHERE kind = 'commitment' "
                               "AND fact_id = %s AND field IN ('text', 'restatement') ORDER BY id",
                               (cid,)).fetchall()
            assert got[:2] == [("text", seen[0]), ("restatement", seen[1])], got


def test_a_replayed_capture_writes_no_new_claim(pg):
    payload = _payload()
    capture.write_pg(dict(payload))
    before = _counts(pg)
    capture.write_pg(dict(payload))
    assert _counts(pg) == before


def test_create_decision_is_an_owner_claim_and_dedups_into_one(pg):
    with pg.connect() as conn, conn.cursor() as cur:
        did = decisions.create_decision(cur, project=PROJECT, text="Ship weekly", evidence={"by": "owner"})
        again = decisions.create_decision(cur, project=PROJECT, text="ship  weekly")
        conn.commit()
        assert again == did
        assert sorted(provenance.claims(cur, "decision", did), key=lambda c: (c["field"], c["value"])) == [
            {"field": "evidence", "value": '{"by": "owner"}', "claimant": "owner", "episode_id": None},
            {"field": "text", "value": "Ship weekly", "claimant": "owner", "episode_id": None},
            {"field": "text", "value": "ship  weekly", "claimant": "owner", "episode_id": None},
        ]
        cur.execute("SELECT text, episode_id, evidence FROM decisions WHERE id = %s", (did,))
        assert cur.fetchone() == ("Ship weekly", None, {"by": "owner"})


def test_a_deliverable_without_a_title_is_a_support_claim(pg):
    out = capture.write_pg(_payload(loop=None, decision=None, title=None,
                                    deliverables=[{"kind": "pr", "url": "https://github.com/acme/widget/pull/9"}]))
    with pg.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, title, episode_id FROM deliverables")
        vid, title, anchor = cur.fetchone()
        assert (title, anchor) == (None, out["episode_id"])
        assert provenance.claims(cur, "deliverable", vid) == [
            {"field": "support", "value": "", "claimant": "episode", "episode_id": out["episode_id"]}]


def test_writers_on_a_hub_before_0047_behave_as_main(hub, monkeypatch, tmp_path):  # noqa: F811
    """No fact_claims table: the writers record nothing and never fail."""
    from tests.hermetic_pg import chain_dir

    from khipu import db

    hub.migrate(directory=chain_dir(tmp_path, "0044"))
    db._TABLE_COLUMNS_CACHE.clear()
    monkeypatch.setattr(co, "_has_commitment_embeddings", lambda cur, ids: False)
    with hub.connect() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO episodes (ts, summary) VALUES (now(), 's') RETURNING id")
        eid = cur.fetchone()[0]
        payload = _payload()
        assert co.open_from_episode(cur, payload, eid) == 1
        assert decisions.insert_decisions_from_episode(cur, payload, eid) == 1
        assert deliverables.insert_deliverables_from_episode(cur, payload, eid) == 1
        assert co.open_from_episode(cur, dict(payload, open_loops=[
            {"text": "Review the deployment checklist before the release", "kind": "followup",
             "owner": "assistant"}]), eid) == 0
        conn.commit()
        cur.execute("SELECT latest_text FROM commitments")
        assert cur.fetchone() == ("Review the deployment checklist before the release",)
        assert provenance.ready(cur) is False
