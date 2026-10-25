# ruff: noqa: F811 - pytest fixtures imported from test_provenance_forget
"""Fact provenance, phase 4: the replica change stream.

Scope: docs/plans/2026-10-23-fact-provenance.md, sections 2 (I1 on a
replica), 6 and 8 (phase 4). Every database is a hermetic one
(tests/hermetic_pg.py) and every replica a temp file.

Ported review reproductions (jobs/2026-10-23/astra-mig/report.md):

* first review #4: a decision change whose transaction started first and
  committed last fell behind the timestamp watermark forever. Here with
  2,000 decisions, through the fact stream's snapshot-xmin cursor;
* first review #7: changes made on a hub in an intermediate state were never
  reached by an already-consumed replica cursor. 0047 is one file, so the
  0045-only state cannot exist; its analog is a replica dumped before 0047,
  which has no stream cursor. It asks for a full dump instead of guessing.

Also: scrub-then-pull through the stream (the durable erasure registry fence
of #356 holds for stream rows), the I1 replica variant, the prune floor, the
local apply of ``forget_everywhere`` and the forget's bounded deadlock retry.
"""
from __future__ import annotations

import json
import sqlite3
import threading

import psycopg
import pytest

from khipu import db, forget, hub_snapshot as hs, offline_reads, provenance, tombstones
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.fact_seed import announce
from tests.test_provenance_forget import (  # noqa: F401
    ALT, CANON, PROJECT, _capture, _forget, _rows, env, pg,
)

DECISION = "Keep the release train weekly"


@pytest.fixture
def replica(monkeypatch, tmp_path):
    """Point the replica code at the hermetic hub (``env`` already set
    KHIPU_DATA_DIR to a temp dir and patched ``db.connect``)."""
    def attach(h):
        monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
        monkeypatch.setattr(hs, "try_hub_connect", lambda **kw: h.connect())
        return h
    return attach


def _replica(sql, params=()):
    with sqlite3.connect(f"file:{hs.snapshot_path()}?mode=ro", uri=True) as con:
        return con.execute(sql, params).fetchall()


def _replica_fact_text() -> str:
    """Every fact table's text in the replica, as one string."""
    return json.dumps([_replica("SELECT * FROM decisions"), _replica("SELECT * FROM deliverables"),
                       _replica("SELECT * FROM commitments")], default=str)


def _replica_readers() -> str:
    with sqlite3.connect(f"file:{hs.snapshot_path()}?mode=ro", uri=True) as con:
        deliv = hs.recent_deliverables_snapshot(con, project=PROJECT)
    return json.dumps([offline_reads.owed_rows(None, "open"), offline_reads.owed_rows(None, "closed"),
                       offline_reads.decisions(None, None, "all", 500), deliv], default=str)


def _pull():
    out = hs.pull_changes(include_ledger=False)
    assert out["ok"], out
    return out


# ---- first review #4: inverted commit order --------------------------------------


def _seed_decisions(h, n):  # noqa: F811
    with h.connect() as conn:
        announce(conn)
        eid = conn.execute(
            "INSERT INTO episodes (ts, session_id, summary, raw, harness, project, transcript_range) "
            "VALUES (now(), 'codex:prov4-bulk', 'bulk', '{}'::jsonb, 'codex', %s, '0:1') RETURNING id",
            (PROJECT,)).fetchone()[0]
        ids = [r[0] for r in conn.execute(
            "INSERT INTO decisions (project, text, episode_id) "
            "SELECT %s, 'bulk decision ' || g, %s FROM generate_series(1, %s) g RETURNING id",
            (PROJECT, eid, n)).fetchall()]
        conn.execute("INSERT INTO fact_claims (kind, fact_id, field, value, claimant, episode_id) "
                     "SELECT 'decision', id, 'text', text, 'episode', episode_id FROM decisions")
        conn.execute("SELECT count(khipu_project_fact('decision', id)) FROM decisions")
    return sorted(ids)


def test_r4_a_late_commit_with_an_earlier_transaction_is_never_skipped(pg, replica):
    replica(pg)
    ids = _seed_decisions(pg, 2000)
    assert hs.refresh()["ok"]
    slow, fast = pg.connect(), pg.connect()
    # T1 changes decision 1 first (its transaction id is the older one) and
    # stays open; T2 changes the other 1,999 and commits.
    slow.execute("UPDATE decisions SET retracted_at = now(), retract_reason = 'SLOW OWNER CHANGE' "
                 "WHERE id = %s", (ids[0],))
    fast.execute("UPDATE decisions SET retracted_at = now(), retract_reason = 'FAST OWNER CHANGE' "
                 "WHERE id <> %s", (ids[0],))
    fast.commit()
    first = _pull()
    assert first["decisions"]["decisions"] == 1999, first
    slow.commit()
    second = _pull()
    assert second["decisions"]["decisions"] >= 1, second
    held = dict(_replica("SELECT id, retract_reason FROM decisions"))
    assert held[ids[0]] == "SLOW OWNER CHANGE"
    assert {held[i] for i in ids[1:]} == {"FAST OWNER CHANGE"}
    # Nothing left behind: a third pull reads nothing new.
    assert _pull()["decisions"]["decisions"] == 0


def test_an_older_stream_read_never_replaces_a_newer_one(pg, replica):
    replica(pg)
    ids = _seed_decisions(pg, 2)
    assert hs.refresh()["ok"]
    with sqlite3.connect(hs.snapshot_path()) as con:
        held = hs._held_fact_cursor(con)
    with pg.connect() as conn:
        conn.execute("UPDATE decisions SET retract_reason = 'newest' WHERE id = %s", (ids[0],))
    assert hs.sync_fact_changes()["decisions"] == 1
    stale = {"xmin": held, "decisions": [{"id": ids[0], "project": PROJECT, "text": "bulk decision 1",
                                          "retract_reason": "stale"}],
             "deliverables": [], "decisions_gone": [], "deliverables_gone": []}
    with sqlite3.connect(hs.snapshot_path()) as con:
        out = hs._apply_fact_stream(con, stale)
        con.commit()
    assert out.get("skipped"), out
    assert _replica("SELECT retract_reason FROM decisions WHERE id = ?", (ids[0],)) == [("newest",)]


# ---- first review #7: a replica dumped before 0047 -------------------------------


def test_r7_a_replica_dumped_before_0047_asks_for_a_full_dump(env, replica):
    replica(env)
    a = _capture(decision=DECISION)
    b = _capture(loop=ALT, decision=DECISION)
    assert hs.refresh()["ok"]
    assert _pull()["decisions"].get("ok")  # the old watermark, on the old schema
    env.to_0047()
    _forget(env, a)
    pulled = hs.pull_changes(include_ledger=False)
    assert not pulled["ok"] and pulled.get("needs_dump"), pulled
    assert _replica("SELECT episode_id FROM decisions") == [(a,)]  # nothing guessed
    assert hs.refresh()["ok"]
    _pull()
    assert _replica("SELECT episode_id, text FROM decisions") == [(b, DECISION)]


# ---- scrub, then pull through the stream (#356's fence) --------------------------


@pytest.mark.parametrize("kind", ["decision", "deliverable"])
def test_a_scrubbed_source_stays_out_through_the_stream(pg, replica, kind):
    replica(pg)
    payload = {"ts": "2026-10-20T12:00:00Z", "summary": "PRIVATE SOURCE", "harness": "codex",
               "session_id": "codex:prov4-forgotten-source", "transcript_range": "0:10"}
    tombstones.publish_payloads([payload])
    with pg.connect() as conn:
        announce(conn)
        eid = conn.execute(
            "INSERT INTO episodes (ts, summary, raw, session_id, harness, transcript_range, project) "
            "VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s) RETURNING id",
            (payload["ts"], payload["summary"], json.dumps(payload), payload["session_id"],
             payload["harness"], payload["transcript_range"], PROJECT)).fetchone()[0]
        if kind == "decision":
            fid = conn.execute("INSERT INTO decisions (project, text, episode_id) VALUES (%s, %s, %s) "
                               "RETURNING id", (PROJECT, "PRIVATE DECISION", eid)).fetchone()[0]
            provenance.record_claim(conn.cursor(), "decision", fid, "text", "PRIVATE DECISION", episode_id=eid)
        else:
            fid = conn.execute("INSERT INTO deliverables (project, kind, path, title, episode_id) "
                               "VALUES (%s, 'file', 'x.md', %s, %s) RETURNING id",
                               (PROJECT, "PRIVATE TITLE", eid)).fetchone()[0]
            provenance.record_claim(conn.cursor(), "deliverable", fid, "title", "PRIVATE TITLE", episode_id=eid)
        provenance.project(conn.cursor(), kind, fid)
    refreshed = hs.refresh()
    assert refreshed["ok"] and refreshed["counts"]["forgotten_scrubbed"] == 1, refreshed
    # Another Mac touches the fact: it is in the stream now.
    with pg.connect() as conn:
        table = "decisions" if kind == "decision" else "deliverables"
        col = "retract_reason" if kind == "decision" else "url"
        conn.execute(f"UPDATE {table} SET {col} = 'touched' WHERE id = %s", (fid,))
    _pull()
    assert hs.sync_fact_changes()["ok"]
    assert "PRIVATE" not in _replica_fact_text()
    assert "PRIVATE" not in _replica_readers()


# ---- I1 / I2 on a replica ----------------------------------------------------------


def test_i1_a_forget_elsewhere_reaches_the_replica_at_its_next_pull(pg, replica):
    replica(pg)
    a = _capture(decision=DECISION + " for A", title="the checklist A")
    b = _capture(loop=ALT, decision=DECISION, title="the checklist B")
    a2 = _capture(decision=DECISION, title="the checklist B", path="docs/other.md")
    assert hs.refresh()["ok"]
    _forget(pg, a)  # on the hub only: another Mac's forget
    _pull()
    seen = _replica_readers()
    assert DECISION + " for A" not in seen and "the checklist A" not in seen
    assert DECISION + " for A" not in _replica_fact_text() and "the checklist A" not in _replica_fact_text()
    # I2: what live episodes still claim is still there.
    assert ALT in seen and DECISION in seen and "the checklist B" in seen
    _forget(pg, a2)
    _forget(pg, b)
    _pull()
    seen = _replica_readers()
    for gone in (ALT, CANON, DECISION, "the checklist"):
        assert gone not in seen, gone


def test_forget_everywhere_applies_the_reprojected_facts_at_once(pg, replica):
    replica(pg)
    a = _capture(decision=DECISION, title="the checklist")
    b = _capture(loop=ALT, decision=DECISION, title="The checklist v2")
    assert hs.refresh()["ok"]
    assert _replica("SELECT episode_id FROM decisions") == [(a,)]
    out = forget.forget_everywhere(a, memory_root=pg.memory, gateway=False)
    assert out["snapshot"]["ok"], out["snapshot"]
    assert out["snapshot"]["facts"]["ok"], out["snapshot"]
    # No pull: the replica already shows the decision and deliverable as B's.
    assert _replica("SELECT episode_id, text FROM decisions") == [(b, DECISION)]
    assert _replica("SELECT episode_id, title FROM deliverables") == [(b, "The checklist v2")]
    assert "the checklist\"" not in _replica_readers()
    # The forget rebuilt the replica's facts from the hub (design section 6,
    # owner decision 2026-10-24): its cursor is that read's, so the next pull
    # has nothing to bring and the replica still shows B's.
    assert out["snapshot"]["facts"].get("rebuilt"), out["snapshot"]
    assert _pull()["ok"]
    assert _replica("SELECT episode_id, text FROM decisions") == [(b, DECISION)]
    assert _replica("SELECT episode_id, title FROM deliverables") == [(b, "The checklist v2")]


# ---- the prune floor ----------------------------------------------------------------


def test_a_cursor_below_the_prune_floor_asks_for_a_full_dump(pg, replica):
    replica(pg)
    ids = _seed_decisions(pg, 3)
    assert hs.refresh()["ok"]
    with sqlite3.connect(hs.snapshot_path()) as con:
        cursor = hs._held_fact_cursor(con)
    # Rows below the cursor only: the replica has read past them, no dump.
    with pg.connect() as conn:
        conn.execute("UPDATE fact_changes SET at = now() - interval '31 days' WHERE xid < %s::text::xid8",
                     (str(cursor),))
        out = provenance.prune_changes(conn)
    assert out["pruned"] >= 3, out
    _pull()
    # A change after the cursor, then pruned: the replica may have missed it.
    with pg.connect() as conn:
        conn.execute("UPDATE decisions SET retract_reason = 'later' WHERE id = %s", (ids[0],))
    with pg.connect() as conn:
        conn.execute("UPDATE fact_changes SET at = now() - interval '31 days'")
        assert provenance.prune_changes(conn)["pruned"] >= 1
    pulled = hs.pull_changes(include_ledger=False)
    assert not pulled["ok"] and pulled.get("needs_dump"), pulled
    assert hs.refresh()["ok"]
    _pull()
    assert _replica("SELECT retract_reason FROM decisions WHERE id = ?", (ids[0],)) == [("later",)]


def test_the_stream_records_lifecycle_writes_but_the_gate_still_wants_the_projector(pg):
    (fid,) = _seed_decisions(pg, 1)
    with pg.connect() as conn:
        conn.execute("UPDATE decisions SET retract_reason = 'owner' WHERE id = %s", (fid,))
        via = conn.execute("SELECT via FROM fact_changes WHERE fact_id = %s AND xid = pg_current_xact_id()",
                           (fid,)).fetchall()
    assert via == [("row",)]
    with pg.connect() as conn:
        conn.execute("UPDATE decisions SET text = 'old writer words' WHERE id = %s", (fid,))
        with pytest.raises(psycopg.OperationalError) as err:
            conn.commit()
    assert provenance.is_old_writer_refusal(err.value)


# ---- forget retries a deadlock ---------------------------------------------------


def test_forget_retries_a_deadlock_within_its_time_budget(pg, monkeypatch):
    a = _capture()
    real = forget.forget_episode
    calls = []

    def flaky(cur, eid, **kw):
        calls.append(eid)
        if len(calls) <= 2:
            raise psycopg.errors.DeadlockDetected("deadlock detected")
        return real(cur, eid, **kw)

    monkeypatch.setattr(forget, "forget_episode", flaky)
    monkeypatch.setattr(forget.time, "sleep", lambda s: None)
    out = forget._forget_on_hub(db.connect, a, memory_root=pg.memory)
    assert out["ok"] and out["soft_deleted"] and out["deadlock_retries"] == 2, out
    assert _rows(pg, "SELECT deleted_at IS NOT NULL FROM episodes WHERE id = %s", (a,)) == [(True,)]

    b = _capture(loop=ALT)
    calls.clear()
    monkeypatch.setattr(forget, "forget_episode",
                        lambda cur, eid, **kw: (calls.append(eid), (_ for _ in ()).throw(
                            psycopg.errors.DeadlockDetected("deadlock detected")))[1])
    # Time-bounded (design section 4): retried until the budget is spent. The
    # budget leaves room for the connect floor (6.4: no connect starts with
    # less than 2 s).
    monkeypatch.setattr(forget, "FORGET_RETRY_BUDGET_S", 2.5)
    with pytest.raises(psycopg.errors.DeadlockDetected):
        forget._forget_on_hub(db.connect, b, memory_root=pg.memory)
    assert len(calls) >= 2, calls
    calls.clear()
    monkeypatch.setattr(forget, "forget_episode",
                        lambda cur, eid, **kw: (calls.append(eid), (_ for _ in ()).throw(
                            psycopg.errors.UniqueViolation("not a deadlock")))[1])
    with pytest.raises(psycopg.errors.UniqueViolation):
        forget._forget_on_hub(db.connect, b, memory_root=pg.memory)
    assert len(calls) == 1
    assert _rows(pg, "SELECT deleted_at IS NULL FROM episodes WHERE id = %s", (b,)) == [(True,)]


def test_two_forgets_racing_a_first_backfill_land_without_the_caller_retrying(pg):
    """Phase 3's race (two forgets that both run a project's first backfill
    can deadlock), now through the forget's own retry."""
    eids = [_capture(loop=w) for w in (CANON, ALT, CANON + " today", ALT + " today")]
    results: list = []

    def run(eid):
        try:
            out = forget._forget_on_hub(db.connect, eid, memory_root=pg.memory)
            results.append((eid, out["soft_deleted"]))
        except Exception as exc:  # noqa: BLE001 - reported by the assertion
            results.append((eid, repr(exc)))

    threads = [threading.Thread(target=run, args=(e,)) for e in eids[:2]]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert sorted(results) == [(eids[0], True), (eids[1], True)], results


def hub_forget(pg, eid):
    """The hub half of a forget alone, no replica write: the hub tombstones
    the episode and re-projects what it supplied (this Mac's registry
    already names its source). The replica's wait rule then lets live
    wording back on its next import."""
    from khipu import db, forget

    pg.memory.mkdir(exist_ok=True)
    with db.connect() as conn, conn.cursor() as cur:
        out = forget.forget_episode(cur, eid, memory_root=pg.memory)
        assert out.get("ok") is not False, out
        conn.commit()
