"""Fact provenance, phase 3: forget, witness backfill, close rules.

Scope: docs/plans/2026-10-23-fact-provenance.md, sections 2 (I1, I2, I3), 3.4,
4 and 8 (phase 3). Every database is a hermetic one (tests/hermetic_pg.py).

The review reproductions ported here (scenarios from the three reviews of the
abandoned fact-support attempt, jobs/2026-10-23/astra-mig, review-fix-astra-mig
and astra-mig2):

* first review #1 capture during the uncommitted migration, #2 legacy
  alternate wording (forgotten after and before the migration), #3 negative
  readiness cached, #5 owner wording and fields survive a forget, #6
  owner-separated twins; #4 and #7 are replica-stream shapes (phase 4: the
  replica code is read-only in this phase);
* second review P2-1 concurrent captures and forgets lose no fact step, P2-3
  old-code writers after the migration;
* third review #2 (mixed kind, owner reassigned, live mixed-kind witness),
  #3 (a real statement timeout through the outbox), #5 (a 100k-episode
  legacy forget's peak memory).

Legacy (pre-0047) data is written by this code on a hub at 0044, which
behaves exactly as main (tests/test_provenance_capture.py pins that).
"""
from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
import textwrap
import random
import threading
import time
from pathlib import Path
from unittest import mock

import psycopg
import pytest

from khipu import capture, close_rules, commitments as co, decisions, deliverables, forget, outbox, provenance
from tests.hermetic_pg import chain_dir, hub, migration_sql, pg_cluster  # noqa: F401
from tests.fact_seed import announce

PROJECT = "acme/widget"
BEFORE = "0044"
_N = itertools.count(1)

CANON = "Review the deployment checklist before release"
ALT = "Review the deployment checklist before the release goes out"


@pytest.fixture
def env(hub, monkeypatch, tmp_path):  # noqa: F811
    """A hub at main's schema (0044) with capture's side effects stubbed.
    ``env.migrate()`` applies 0047."""
    from khipu import db, embed, topic_graph

    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KHIPU_TEST", "1")
    hub.migrate(directory=chain_dir(tmp_path, BEFORE))
    db._TABLE_COLUMNS_CACHE.clear()
    monkeypatch.setattr(db, "connect", lambda *a, **kw: hub.connect())
    monkeypatch.setattr(embed, "embed_on_capture", lambda *a, **kw: None)
    monkeypatch.setattr(embed, "embed_one", mock.Mock(side_effect=RuntimeError("no provider")))
    monkeypatch.setattr(topic_graph, "persist_capture_graph", lambda *a, **kw: {})
    monkeypatch.setattr(outbox, "_queued_origin", lambda: {"origin": None})
    monkeypatch.setattr(co, "_has_commitment_embeddings", lambda cur, ids: False)
    monkeypatch.setattr(capture, "_dedup_candidates", lambda cur, payload: [])

    def migrate():
        db._TABLE_COLUMNS_CACHE.clear()
        result = hub.migrate()
        assert result["ran"] == ["0047_fact_provenance"], result
        db._TABLE_COLUMNS_CACHE.clear()

    hub.to_0047 = migrate
    hub.memory = tmp_path / "memory"
    return hub


@pytest.fixture
def pg(env):
    env.to_0047()
    return env


def _payload(*, loop=CANON, kind="followup", owner="assistant", decision=None, title=None,
             path="docs/checklist.md", **extra) -> dict:
    n = next(_N)
    payload = {
        "ts": f"2026-10-23T13:{n // 60:02d}:{n % 60:02d}Z",
        "summary": f"synthetic capture {n} about release step {n}",
        "project": PROJECT,
        "session_id": f"codex:prov3-{n}",
        "harness": "codex",
        "transcript_range": f"0:{n}",
    }
    if loop is not None:
        payload["open_loops"] = [{"text": loop, "kind": kind, "owner": owner}]
    if decision is not None:
        payload["decisions"] = [decision]
    if title is not None:
        payload["deliverables"] = [{"kind": "file", "path": path, "title": title}]
    payload.update(extra)
    return payload


def _capture(**kw) -> int:
    out = capture.write_pg(_payload(**kw))
    assert out["episode_inserted"], out
    return int(out["episode_id"])


def _forget(hub, eid) -> dict:  # noqa: F811
    with hub.connect() as conn, conn.cursor() as cur:
        out = forget.forget_episode(cur, eid, memory_root=hub.memory)
        conn.commit()
    assert out["ok"] and out["soft_deleted"], out
    return out


def _rows(hub, sql, params=()):  # noqa: F811
    with hub.connect() as conn:
        return conn.execute(sql, params).fetchall()


def _readers(hub) -> str:  # noqa: F811
    """Everything the hub's readers return for the project, as one string."""
    with hub.connect() as conn, conn.cursor() as cur:
        out = {
            "open": co.list_owed(cur, project=PROJECT, limit=None),
            "closed": co.list_owed(cur, project=PROJECT, status="closed", limit=None),
            "decisions": decisions.list_decisions(cur, project=PROJECT, status="all", limit=500),
            "deliverables": deliverables.recent_deliverables(cur, project=PROJECT),
        }
        conn.rollback()
    return json.dumps(out, default=str)


def _visible_commitments(hub):  # noqa: F811
    with hub.connect() as conn, conn.cursor() as cur:
        rows = co.list_owed(cur, project=PROJECT, limit=None)
        conn.rollback()
    return sorted((r["id"], r["text"], r.get("latest_text")) for r in rows)


# ---- the five tests phase 2 left red are in their own files; this one is the
# ---- forget cascade's own contract ----------------------------------------------


def test_forget_keeps_a_fact_another_live_episode_claims(pg):
    a = _capture(decision="Keep the release train weekly", title="the checklist")
    b = _capture(loop=ALT, decision="Keep the release train weekly", title="The checklist v2")
    out = _forget(pg, a)
    assert (out["commitments_closed"], out["decisions_retracted"], out["deliverables_removed"]) == (0, 0, 0)
    assert out["facts_reprojected"] == 3
    (cid, text, latest), = _visible_commitments(pg)
    assert (text, latest) == (ALT, None)
    assert _rows(pg, "SELECT opened_episode FROM commitments") == [(b,)]
    assert _rows(pg, "SELECT episode_id, retracted_at FROM decisions") == [(b, None)]
    assert _rows(pg, "SELECT episode_id, title FROM deliverables") == [(b, "The checklist v2")]
    assert _rows(pg, "SELECT count(*) FROM fact_claims WHERE episode_id = %s", (a,)) == [(0,)]
    out = _forget(pg, b)
    assert (out["commitments_closed"], out["decisions_retracted"], out["deliverables_removed"]) == (1, 1, 1)
    assert _visible_commitments(pg) == []
    assert CANON not in _readers(pg) and ALT not in _readers(pg)


def test_an_old_code_forget_is_still_refused(pg):
    """Second review P2-3 / section 5.2: main's cascade, without claims."""
    a = _capture()
    with pg.connect() as conn:
        conn.execute("UPDATE episodes SET deleted_at = now() WHERE id = %s", (a,))
        conn.execute("UPDATE commitments SET status = 'closed', close_reason = 'forgotten' "
                     "WHERE opened_episode = %s", (a,))
        with pytest.raises(psycopg.OperationalError) as err:
            conn.commit()
    assert provenance.is_old_writer_refusal(err.value)
    assert _rows(pg, "SELECT deleted_at IS NULL FROM episodes WHERE id = %s", (a,)) == [(True,)]


# ---- first review --------------------------------------------------------------------


def test_r1_a_capture_during_the_uncommitted_migration_waits_then_claims(env):
    a = _capture()
    sql = migration_sql("0047_fact_provenance.sql")
    m = env.connect()
    m.execute(sql)  # holds ACCESS EXCLUSIVE on episodes and the fact tables
    result: dict = {}

    def write():
        try:
            result["out"] = capture.write_pg(_payload(loop=ALT))
        except Exception as exc:  # noqa: BLE001 — reported below
            result["exc"] = exc

    t = threading.Thread(target=write)
    t.start()
    t.join(1.0)
    assert t.is_alive(), "the capture must wait for the migration's locks"
    m.commit()
    t.join(30)
    from khipu import db

    db._TABLE_COLUMNS_CACHE.clear()
    if "exc" in result:
        # A stale readiness answer is refused at COMMIT and deferred; the retry lands.
        assert isinstance(result["exc"], capture.FactStepDeferred), result
        result["out"] = capture.write_pg(_payload(loop=ALT))
    b = int(result["out"]["episode_id"])
    assert _rows(env, "SELECT field FROM fact_claims WHERE episode_id = %s AND kind = 'commitment'",
                 (b,)) == [("restatement",)]
    assert [r[2] for r in _visible_commitments(env)] == [ALT]
    _forget(env, b)
    assert [r[1:] for r in _visible_commitments(env)] == [(CANON, None)]
    assert ALT not in _readers(env)
    assert a


@pytest.mark.parametrize("forgotten", ["after", "before"])
def test_r2_legacy_alternate_wording_is_never_readable_once_forgotten(env, forgotten):
    a = _capture()
    b = _capture(loop=ALT)
    assert [r[2] for r in _visible_commitments(env)] == [ALT]  # main stored B's words
    if forgotten == "before":
        _forget(env, b)  # main's cascade: B anchors nothing, its words stay
        assert ALT in _readers(env)
    env.to_0047()
    assert ALT not in _readers(env), "an unproven legacy value is never shown"
    with env.connect() as conn:
        assert provenance.backfill(conn)["ok"]
    if forgotten == "before":
        assert ALT not in _readers(env), "a forgotten episode is never a witness"
    else:
        assert [r[2] for r in _visible_commitments(env)] == [ALT], "the live restater is proven"
        _forget(env, b)
    assert ALT not in _readers(env)
    assert [r[1:] for r in _visible_commitments(env)] == [(CANON, None)]
    assert _rows(env, "SELECT opened_episode FROM commitments") == [(a,)]


def test_r2_forget_runs_the_backfill_first_for_a_project_not_yet_covered(env):
    """Forgetting the anchor of a legacy fact a live restater supports keeps
    the fact (the original audit's "forgetting one source loses facts")."""
    a = _capture()
    b = _capture(loop=ALT)
    env.to_0047()
    out = _forget(env, a)
    assert out["provenance_backfill"]["claims"] >= 1 and out["commitments_closed"] == 0
    assert [r[1:] for r in _visible_commitments(env)] == [(ALT, None)]
    assert _rows(env, "SELECT opened_episode FROM commitments") == [(b,)]
    assert _rows(env, "SELECT project FROM provenance_projects WHERE backfilled_at IS NOT NULL") == [(PROJECT,)]


def test_r3_a_negative_readiness_answer_is_not_kept(env):
    with env.connect() as conn, conn.cursor() as cur:
        assert provenance.ready(cur) is False
    _capture()
    env.to_0047()
    b = _capture(loop=ALT)
    assert _rows(env, "SELECT count(*) FROM fact_claims WHERE episode_id = %s", (b,)) == [(1,)]
    _forget(env, b)
    assert ALT not in _readers(env)


def test_r5_owner_wording_and_owner_fields_survive_a_forget(pg):
    """First review #5, in this model's terms: owner-authored wording is an
    owner claim (create_decision), and owner fields are never written."""
    a = _capture(decision="Keep the release train weekly")
    b = _capture()
    with pg.connect() as conn, conn.cursor() as cur:
        did = decisions.create_decision(cur, project=PROJECT, text="Keep the release train weekly")
        cid = cur.execute("SELECT id FROM commitments").fetchone()[0]
        assert co.reassign(cur, cid, "user", by=co.BY_APP, reason="mine")
        assert co.set_status(cur, cid, "stale")
        assert co.set_status(cur, cid, "open")
        conn.commit()
    snap = _owner_fields(pg, cid)
    _forget(pg, a)
    assert _owner_fields(pg, cid) == snap
    assert _rows(pg, "SELECT id, text, episode_id, retracted_at FROM decisions") == [
        (did, "Keep the release train weekly", None, None)]
    _forget(pg, b)
    # No live claim is left on the commitment: it retires, and only its
    # status moves; every other owner field stays on the row.
    after = _owner_fields(pg, cid)
    assert {k: v for k, v in after.items() if k not in ("status", "closed_at", "close_reason")} == \
        {k: v for k, v in snap.items() if k not in ("status", "closed_at", "close_reason")}
    assert after["close_reason"] == "forgotten"


_OWNER_COLS = ("status", "closed_at", "close_reason", "owner", "owner_label", "owner_reassigned_at",
               "due_after", "linked_ids", "reopened_rule_keys", "kind")


def _owner_fields(hub, cid) -> dict:  # noqa: F811
    with hub.connect() as conn:
        row = conn.execute(f"SELECT {', '.join(_OWNER_COLS)}, pr_check FROM commitments WHERE id = %s",
                           (cid,)).fetchone()
    out = dict(zip(_OWNER_COLS, row[:-1]))
    check = dict(row[-1] or {})
    check.pop("refs", None)
    check.pop("needs_owner", None)
    out["pr_check"] = check
    return out


def test_r6_owner_separated_twins_forget_without_a_key_conflict(pg):
    a = _capture(owner="user", kind="promise")
    b = _capture()
    c = _capture()
    rows = _rows(pg, "SELECT id, opened_episode, content_hash FROM commitments ORDER BY id")
    assert [r[1] for r in rows] == [a, b]
    _forget(pg, b)
    after = _rows(pg, "SELECT id, opened_episode, content_hash, status FROM commitments ORDER BY id")
    assert [(r[0], r[1], r[2]) for r in after] == [(rows[0][0], a, rows[0][2]), (rows[1][0], c, rows[1][2])]
    assert {r[3] for r in after} == {"open"}
    assert _rows(pg, "SELECT deleted_at IS NOT NULL FROM episodes WHERE id = %s", (b,)) == [(True,)]


def test_two_forgets_racing_a_first_backfill_both_land(pg):
    """Two forgets in a project the backfill has not covered both run it;
    if they deadlock, the refused one changed nothing and its retry lands."""
    eids = [_capture(loop=w) for w in (CANON, ALT, CANON + " today", ALT + " today")]
    results: list = []

    def run(eid):
        from khipu import db

        for attempt in range(30):
            try:
                with db.connect() as conn, conn.cursor() as cur:
                    out = forget.forget_episode(cur, eid, memory_root=pg.memory)
                    conn.commit()
                results.append((eid, out["soft_deleted"], attempt))
                return
            except psycopg.errors.DeadlockDetected:
                time.sleep(0.02 * attempt)
        results.append((eid, False, "gave up"))

    threads = [threading.Thread(target=run, args=(e,)) for e in eids[:2]]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert sorted((e, ok) for e, ok, _a in results) == [(eids[0], True), (eids[1], True)], results
    _assert_projection(pg)
    _assert_forgotten_unreadable(pg, {eids[0]: {CANON}, eids[1]: {ALT}})


# ---- second review -----------------------------------------------------------------------


def test_p2_1_concurrent_captures_and_forgets_lose_no_fact_step(pg):
    """Captures restating one item, decision and deliverable race forgets of
    earlier captures. Every capture either commits with a claim on each fact
    it derived or is deferred and retried; nothing fails open."""
    from khipu import db

    errors: list = []
    landed: list[int] = []
    lock = threading.Lock()
    words = [CANON, ALT, CANON + " today", ALT + " today"]
    # The steady state: the project's witness backfill has run (the nightly).
    # Two forgets that both run a project's first backfill can deadlock; one
    # is refused and retried (test_two_forgets_racing_a_first_backfill).
    with pg.connect() as conn:
        provenance.backfill(conn)

    def worker(n):
        for i in range(6):
            payload = _payload(loop=words[(n + i) % 4], decision="Keep the release train weekly",
                               title="the checklist")
            for _attempt in range(20):
                try:
                    out = capture.write_pg(dict(payload))
                    break
                except capture.FactStepDeferred:
                    continue
            else:
                errors.append(("never landed", n, i))
                continue
            with lock:
                landed.append(int(out["episode_id"]))
                victim = landed[-2] if i % 2 and len(landed) > 1 else None
            if victim is not None:
                # A forget refused by a deadlock or serialization failure is
                # retried (the documented contract); the timing assumption is
                # only how long the retries may take. A count of attempts
                # failed under full-suite load, so the bound is wall time,
                # with jittered backoff so the four workers stop colliding.
                deadline = time.monotonic() + 90
                attempt = 0
                while True:
                    try:
                        with db.connect() as conn, conn.cursor() as cur:
                            forget.forget_episode(cur, victim, memory_root=pg.memory)
                            conn.commit()
                        break
                    except (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure) as exc:
                        attempt += 1
                        if time.monotonic() > deadline:
                            errors.append(("forget never landed", victim, attempt, repr(exc)))
                            break
                        time.sleep(min(0.5, 0.02 * attempt) * (0.5 + random.random()))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(300)
    assert not any(t.is_alive() for t in threads)
    assert not errors, errors
    assert _rows(pg, "SELECT count(*) FROM fact_gaps") == [(0,)]
    live = {e for (e,) in _rows(pg, "SELECT id FROM episodes WHERE deleted_at IS NULL")}
    for eid in live:
        kinds = {k for (k,) in _rows(pg, "SELECT DISTINCT kind FROM fact_claims WHERE episode_id = %s", (eid,))}
        assert kinds == {"commitment", "decision", "deliverable"}, (eid, kinds)
    _assert_projection(pg)
    _assert_forgotten_unreadable(pg)


def test_p2_3_an_old_code_dedup_after_the_migration_is_refused(pg):
    _capture()
    b = _capture(loop=ALT)
    for statement in ("UPDATE commitments SET latest_text = 'old writer words'",
                      "UPDATE commitments SET prs = '{99}'",
                      "INSERT INTO commitments (text, project, owner, kind, content_hash) "
                      "VALUES ('old opener', 'acme/widget', 'assistant', 'followup', 'x')"):
        with pg.connect() as conn:
            conn.execute(statement)
            with pytest.raises(psycopg.OperationalError) as err:
                conn.commit()
        assert provenance.is_old_writer_refusal(err.value), statement
    _forget(pg, b)
    assert ALT not in _readers(pg) and "old writer words" not in _readers(pg)


# ---- third review #2: the witness rules capture's dedup never had -------------------------


@pytest.mark.parametrize("schedule", ["mixed-kind", "reassigned"])
def test_t2_a_legacy_restater_is_proven_whatever_its_kind_or_the_current_owner(env, schedule):
    if schedule == "mixed-kind":
        _capture(kind="promise", owner="user", loop="I will review the deployment checklist before release")
        b = _capture(kind="followup", owner="user", loop="I will review the deployment checklist before the release")
    else:
        _capture(kind="promise", owner="user", loop="I will review the deployment checklist before release")
        b = _capture(kind="promise", owner="user", loop="I will review the deployment checklist before the release")
        with env.connect() as conn, conn.cursor() as cur:
            cid = cur.execute("SELECT id FROM commitments").fetchone()[0]
            assert co.reassign(cur, cid, "assistant", by=co.BY_APP, reason="schedule")
            conn.commit()
    assert _rows(env, "SELECT count(*) FROM commitments") == [(1,)]
    words = "I will review the deployment checklist before the release"
    env.to_0047()
    with env.connect() as conn:
        assert provenance.backfill(conn)["claims"] >= 1
    assert [r[2] for r in _visible_commitments(env)] == [words]
    _forget(env, b)
    with env.connect() as conn:
        provenance.backfill(conn)  # the nightly again: nothing to re-add
    assert words not in _readers(env)
    if schedule == "reassigned":
        assert _rows(env, "SELECT owner FROM commitments") == [("assistant",)]


def test_t2_live_mixed_kind_witness_keeps_its_wording(pg):
    _capture(kind="promise", owner="user", loop="I will review the deployment checklist before release")
    b = _capture(kind="promise", owner="user", loop="I will review the deployment checklist before the release")
    c = _capture(kind="followup", owner="user", loop="I will review the deployment checklist before the release")
    assert _rows(pg, "SELECT count(*) FROM commitments") == [(1,)]
    _forget(pg, b)
    assert [r[1:] for r in _visible_commitments(pg)] == [
        ("I will review the deployment checklist before release",
         "I will review the deployment checklist before the release")]
    assert c


# ---- third review #3: a real statement timeout through the outbox ---------------------------


def test_t3_a_statement_timeout_on_a_fact_lock_keeps_the_job(pg, monkeypatch):
    from khipu import db

    _capture()
    holder = pg.connect()
    holder.execute("SELECT id FROM commitments FOR UPDATE")
    real = pg.connect

    def connect(*a, **kw):
        conn = real()
        conn.execute("SET statement_timeout = '80ms'")
        conn.commit()
        return conn

    monkeypatch.setattr(db, "connect", connect)
    outbox.enqueue(_payload(loop=ALT, decision="Keep the release train weekly"), reason="test")
    report = outbox.drain()
    assert report["failed"] == 1 and report.get("deferred") == 1, report
    (job,) = outbox.jobs()
    assert "QueryCanceled" in json.loads(job.read_text())["deferral_reason"]
    assert _rows(pg, "SELECT count(*) FROM episodes") == [(1,)]
    assert _rows(pg, "SELECT count(*) FROM decisions") == [(0,)]
    holder.rollback()
    assert outbox.drain()["replayed"] == 1
    assert outbox.jobs() == []
    assert _rows(pg, "SELECT count(*) FROM episodes") == [(2,)]
    assert _rows(pg, "SELECT count(*) FROM decisions") == [(1,)]
    assert [r[2] for r in _visible_commitments(pg)] == [ALT]


# ---- third review #5: a legacy forget's memory is bounded by the batch -------------------


_RSS_PROBE = textwrap.dedent("""
    import json, resource, sys
    import psycopg
    from khipu import (capture, close_rules, commitments, decisions, deliverables, forget, provenance,
                       tombstones)
    dsn, eid, root = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    conn = psycopg.Connection.connect(dsn)
    conn.execute("SELECT 1").fetchall()
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    with conn.cursor() as cur:
        out = forget.forget_episode(cur, eid, memory_root=None)
    conn.commit()
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    scale = 1 if sys.platform == "darwin" else 1024
    print(json.dumps({"peak_delta_mib": (after - before) * scale / 2**20, "out": {
        k: out.get(k) for k in ("ok", "soft_deleted", "commitments_closed", "provenance_backfill")}}))
""")


def test_t5_a_100k_episode_legacy_forget_streams(env, tmp_path):
    a = _capture()
    b = _capture(loop=ALT)
    with env.connect() as conn:
        # 100,000 legacy episodes in one project, each with 4,000 bytes of
        # unrelated raw (incompressible), and a sprinkling of open loops.
        conn.execute(
            "INSERT INTO episodes (ts, session_id, harness, summary, project, raw) "
            "SELECT now(), 'codex:bulk-' || g, 'codex', 'bulk ' || g, %s, jsonb_build_object("
            "'note', (SELECT string_agg(md5(g::text || ':' || k), '') FROM generate_series(1, 125) k), "
            "'open_loops', CASE WHEN g %% 1000 = 0 THEN jsonb_build_array(jsonb_build_object("
            "'text', 'Unrelated loop ' || g, 'kind', 'followup')) ELSE '[]'::jsonb END) "
            "FROM generate_series(1, 100000) g", (PROJECT,))
        conn.commit()
    env.to_0047()
    script = tmp_path / "rss_probe.py"
    script.write_text(_RSS_PROBE, encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script), env.dsn, str(a), str(tmp_path)],
                          capture_output=True, text=True, timeout=600,
                          env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    assert proc.returncode == 0, proc.stderr[-2000:]
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    print(got)
    assert got["out"]["ok"] and got["out"]["soft_deleted"]
    assert got["out"]["provenance_backfill"]["episodes"] == 100001
    # The attempt's legacy forget grew 878 MiB on this shape; streaming is
    # bounded by one batch of loops, not the project's raw.
    assert got["peak_delta_mib"] < 64, got
    assert [r[1:] for r in _visible_commitments(env)] == [(ALT, None)]
    assert _rows(env, "SELECT opened_episode FROM commitments") == [(b,)]


# ---- close rules -----------------------------------------------------------------------------


def test_needs_owner_survives_the_pr_rule_and_blocks_auto_close(pg, monkeypatch):
    """A legacy PR value no live claim holds: the rule keeps needs_owner on
    pr_check and never closes the item, even when every PR it names merged."""
    _capture(loop="Land PR #41 before release")
    with pg.connect() as conn, conn.cursor() as cur:
        cid = cur.execute("SELECT id FROM commitments").fetchone()[0]
        provenance.record_claim(cur, "commitment", cid, "pr", 77, claimant="legacy_unproven")
        provenance.project(cur, "commitment", cid)
        conn.commit()
    (check,), = _rows(pg, "SELECT pr_check FROM commitments")
    assert check.get("needs_owner") is True and "refs" not in check, check
    monkeypatch.setattr(close_rules.GitHub, "state", lambda self, repo, n: ("merged", None))
    with pg.connect() as conn, conn.cursor() as cur:
        report = close_rules.run(cur)
        conn.commit()
    assert report["closed_pr"] == 0 and report.get("needs_owner") == 1, report
    (status, check), = _rows(pg, "SELECT status, pr_check FROM commitments")
    assert status == "open" and check.get("needs_owner") is True, check
    # The owner can still close it by hand.
    with pg.connect() as conn, conn.cursor() as cur:
        assert co.set_status(cur, cid, "closed", reason="done", by=co.BY_APP)
        conn.commit()


def test_a_forgotten_capture_is_no_close_rule_evidence(pg, monkeypatch):
    """The original audit's P2: the nightly closed items on a forgotten
    episode's git lines."""
    _capture(loop="Land PR #41 before release")
    with pg.connect() as conn:
        # Not a capture (that would run the capture-time rules on itself).
        announce(conn)
        e = conn.execute("INSERT INTO episodes (ts, session_id, harness, summary, project, raw) "
                         "VALUES (now(), 'codex:merge', 'codex', 'Merge pull request #41 from acme/fix', %s, "
                         "'{}'::jsonb) RETURNING id", (PROJECT,)).fetchone()[0]
        conn.commit()
    with pg.connect() as conn, conn.cursor() as cur:
        assert close_rules._episode_states(cur, PROJECT, 0), "the control: the line is evidence while live"
    _forget(pg, e)
    with pg.connect() as conn, conn.cursor() as cur:
        assert close_rules._episode_states(cur, PROJECT, 0) == {}
    monkeypatch.setattr(close_rules.GitHub, "state", lambda self, repo, n: (None, "GitHub not asked"))
    with pg.connect() as conn, conn.cursor() as cur:
        report = close_rules.run(cur)
        conn.commit()
    assert report["closed_pr"] == 0, report
    assert _rows(pg, "SELECT status FROM commitments") == [("open",)]


def test_backfill_prs_claims_a_never_parsed_row_after_0047(env):
    """prs NULL (before 0034) stays "not parsed" through 0047; backfill_prs
    then records the anchor's own numbers as its claims."""
    _capture()
    _capture(loop="Implement the two fixes before PR #366 can merge")
    with env.connect() as conn:
        conn.execute("UPDATE commitments SET prs = NULL")
        conn.commit()
    env.to_0047()
    assert sorted(_rows(env, "SELECT text, prs FROM commitments")) == [
        ("Implement the two fixes before PR #366 can merge", None), (CANON, [])]
    with env.connect() as conn, conn.cursor() as cur:
        assert close_rules.backfill_prs(cur) == 1
        conn.commit()
        assert close_rules.backfill_prs(cur) == 0
    assert sorted(_rows(env, "SELECT text, prs FROM commitments")) == [
        ("Implement the two fixes before PR #366 can merge", [366]), (CANON, [])]
    assert _rows(env, "SELECT claimant, value FROM fact_claims WHERE field = 'pr'") == [("episode", "366")]


def test_the_backfill_proves_a_plain_pr_ref_and_clears_needs_owner(env):
    """Phase 1's note 8: a plain "PR #41" ref needs the project's repository,
    which only Python knows; the witness backfill proves it."""
    _capture(loop="Review the release checklist before the launch")
    _capture(loop="Review the release checklist before the launch in PR #41")
    (check,), = _rows(env, "SELECT pr_check FROM commitments")
    assert check["refs"] == ["acme/widget#41"]
    env.to_0047()
    (prs, check), = _rows(env, "SELECT prs, pr_check FROM commitments")
    assert prs == [] and check.get("needs_owner") is True and "refs" not in check
    with env.connect() as conn:
        provenance.backfill(conn)
    (prs, check), = _rows(env, "SELECT prs, pr_check FROM commitments")
    assert prs == [41] and check["refs"] == ["acme/widget#41"] and "needs_owner" not in check


def test_the_nightly_step_runs_the_backfill(pg, monkeypatch):
    from khipu import jobs

    monkeypatch.setattr(jobs, "_nightly_log", lambda msg: None)
    monkeypatch.setattr(close_rules.GitHub, "state", lambda self, repo, n: (None, "not asked"))
    _capture()
    out = jobs._commitment_close_rules()
    assert out["ok"] and out["provenance"]["ok"] and out["provenance"]["projects"] == 1, out
    assert jobs._commitment_close_rules()["provenance"]["projects"] == 0


def test_the_cli_runs_the_backfill(pg, capsys):
    import argparse

    from khipu import cli

    _capture()
    assert cli.cmd_provenance(argparse.Namespace(provenance_cmd="backfill")) == 0
    assert json.loads(capsys.readouterr().out.strip())["projects"] == 1


# ---- I1, I2, I3: generated schedules ----------------------------------------------------------


def _live_claims(cur, kind, fact_id):
    cur.execute(
        "SELECT fc.field, fc.value, fc.claimant, fc.episode_id FROM fact_claims fc "
        "LEFT JOIN episodes e ON e.id = fc.episode_id "
        "WHERE fc.kind = %s AND fc.fact_id = %s AND (fc.claimant = 'owner' "
        "OR (fc.claimant = 'episode' AND e.deleted_at IS NULL))",
        (kind, fact_id))
    return cur.fetchall()


def _assert_projection(hub):  # noqa: F811
    """I2: a fact with a live claim is not retired, and each projected field
    is one of its live claims' values."""
    with hub.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, text, latest_text, prs, pr_check, opened_episode, close_reason FROM commitments")
        for cid, text, latest, prs, check, anchor, reason in cur.fetchall():
            live = _live_claims(cur, "commitment", cid)
            if not [c for c in live if c[0] in ("text", "restatement")]:
                assert reason == "forgotten" or reason is not None, cid
                assert latest is None and not prs, cid
                continue
            assert reason != "forgotten", (cid, live)
            assert text in {v for f, v, c, e in live if f in ("text", "restatement")}, (cid, text, live)
            assert latest is None or latest in {v for f, v, c, e in live if f == "restatement"}, (cid, latest)
            assert sorted(prs or []) == sorted({int(v) for f, v, c, e in live if f == "pr"}), (cid, prs, live)
            refs = (check or {}).get("refs") or []
            assert sorted(refs) == sorted({v for f, v, c, e in live if f == "pr_ref"}), (cid, refs, live)
        cur.execute("SELECT id, text, episode_id, retract_reason FROM decisions")
        for did, text, anchor, reason in cur.fetchall():
            live = _live_claims(cur, "decision", did)
            if not [c for c in live if c[0] == "text"]:
                assert reason == "forgotten", did
                continue
            assert reason != "forgotten", did
            assert text in {v for f, v, c, e in live if f == "text"}, (did, text, live)
        cur.execute("SELECT id, title FROM deliverables")
        for vid, title in cur.fetchall():
            live = _live_claims(cur, "deliverable", vid)
            assert live, vid  # a deliverable with no live claim is deleted
            assert title is None or title in {v for f, v, c, e in live if f == "title"}, (vid, title, live)


def _assert_forgotten_unreadable(hub, held: dict[int, set[str]] | None = None):  # noqa: F811
    """I1: no reader returns wording whose only claimants are forgotten
    episodes. ``held`` maps a forgotten episode to the wording values it
    claimed before its forget (claims are deleted by the forget)."""
    out = _readers(hub)
    with hub.connect() as conn:
        live_values = {v for (v,) in conn.execute(
            "SELECT fc.value FROM fact_claims fc LEFT JOIN episodes e ON e.id = fc.episode_id "
            "WHERE fc.field IN ('text', 'restatement', 'title', 'rationale') "
            "AND (fc.claimant = 'owner' OR (fc.claimant = 'episode' AND e.deleted_at IS NULL))")}
    for eid, values in (held or {}).items():
        for v in values:
            if v in live_values or any(v in w for w in live_values):
                continue
            assert json.dumps(v)[1:-1] not in out, (eid, v)


SCHEDULE_CAPTURES = {
    "A": dict(loop=CANON, decision="Keep the release train weekly", title="the checklist"),
    "B": dict(loop=ALT + " in PR #41", kind="question", decision="Keep the release train weekly",
              title="The checklist (final)"),
    "C": dict(loop=CANON, kind="promise", owner="user", decision="Ship on Thursdays", title=None),
    "D": dict(loop=CANON + " and the rollback plan", decision="Keep the release train weekly",
              title="the checklist"),
}


def _wording_claims(hub, eid) -> set[str]:  # noqa: F811
    return {v for (v,) in _rows(hub, "SELECT value FROM fact_claims WHERE episode_id = %s "
                                     "AND field IN ('text', 'restatement', 'title', 'rationale')", (eid,))}


def _all_owner_fields(hub):  # noqa: F811
    with hub.connect() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM commitments ORDER BY id")]
        decided = conn.execute("SELECT id, superseded_by, retracted_at, retract_reason FROM decisions "
                               "WHERE retract_reason IS DISTINCT FROM 'forgotten' ORDER BY id").fetchall()
    return {i: _owner_fields(hub, i) for i in ids}, decided


_FORGET_ORDERS = [p for r in range(1, 5) for s in itertools.combinations("ABCD", r)
                  for p in [s, s[::-1]] if len(s) < 3 or p == s]


@pytest.mark.parametrize("legacy", [False, True], ids=["new", "legacy"])
@pytest.mark.parametrize("order", ["".join(o) for o in dict.fromkeys(_FORGET_ORDERS)])
def test_forget_schedules_hold_i1_i2_i3(env, order, legacy):
    """Captures A-D (mixed kinds and owners, an owner reassign, a refused old
    writer), with or without the migration in between, then forgets of the
    subset ``order`` in that order; I1, I2 and I3 after every commit."""
    if not legacy:
        env.to_0047()
    eids = {}
    for name in "ABCD":
        eids[name] = _capture(**SCHEDULE_CAPTURES[name])
        if name == "B":
            with env.connect() as conn, conn.cursor() as cur:
                cid = cur.execute("SELECT min(id) FROM commitments").fetchone()[0]
                co.reassign(cur, cid, "agent", "reviewer", by=co.BY_CONVERSATION, reason="schedule")
                conn.commit()
    if legacy:
        env.to_0047()
    with env.connect() as conn:
        conn.execute("UPDATE commitments SET latest_text = 'old writer words'")
        with pytest.raises(psycopg.OperationalError):
            conn.commit()
    _assert_projection(env)
    held: dict[int, set[str]] = {}
    for name in order:
        eid = eids[name]
        before_owner, before_decisions = _all_owner_fields(env)
        if not legacy or name != order[0]:
            held[eid] = _wording_claims(env, eid)
        else:
            # The first legacy forget runs the backfill first: what the
            # episode will have claimed is its capture's own words.
            spec = SCHEDULE_CAPTURES[name]
            payload_words = {spec["loop"], spec.get("title"), spec.get("decision")}
            held[eid] = {w for w in payload_words if w} | _wording_claims(env, eid)
        _forget(env, eid)
        _assert_projection(env)
        _assert_forgotten_unreadable(env, held)
        after_owner, after_decisions = _all_owner_fields(env)
        for cid, fields in before_owner.items():
            got = after_owner[cid]
            if got["close_reason"] == "forgotten" and fields["close_reason"] != "forgotten":
                # I3's one exception: a fact left with no live or owner claim retires.
                got = {k: v for k, v in got.items() if k not in ("status", "closed_at", "close_reason")}
                fields = {k: v for k, v in fields.items() if k not in ("status", "closed_at", "close_reason")}
            assert got == fields, (order, name, cid)
        assert {d[0]: d for d in after_decisions}.items() <= {d[0]: d for d in before_decisions}.items()
    assert "old writer words" not in _readers(env)
