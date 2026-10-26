# ruff: noqa: F811 - pytest fixtures imported from test_provenance_forget and replica_copy
"""Fact provenance, phase 7B: the replica's fact copy (design section 6.2).

Scope: docs/plans/2026-10-23-fact-provenance.md, sections 6.2 to 6.4 and
section 8, phase 7B: the base acceptance list, the re-review's N1, N3, N6
and skip items, and the third review's D2 and D4. Every database is a
hermetic one (tests/hermetic_pg.py) and every replica a temp file. A copy
is paused after its hub read by wrapping ``hub_snapshot._copy_read``; the
read has already rolled back its hub transaction by then.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest

from khipu import forget, hub_snapshot as hs, memory_lock, paths, provenance, tombstones
from tests.fact_seed import announce
from tests.hermetic_pg import PG_CTL, hub, pg_cluster  # noqa: F401
from tests.replica_copy import fact_text, mark_ready, point, readers, replica, rows, shown, stamp  # noqa: F401
from tests.test_provenance_forget import (  # noqa: F401
    ALT, PROJECT, _capture, _forget, env, pg,
)

DECISION = "Keep the release train weekly"


class Pause:
    """Pause the next ``times`` copies after their hub read, until ``go``."""

    def __init__(self, monkeypatch, *, times: int = 1):
        self.read, self.go, self.left = threading.Event(), threading.Event(), times
        real = hs._copy_read

        def wrapped(tx, local):
            out = real(tx, local)
            if self.left > 0:
                self.left -= 1
                self.read.set()
                assert self.go.wait(120)
            return out

        monkeypatch.setattr(hs, "_copy_read", wrapped)


def _run(fn, results, key):
    def target():
        try:
            results[key] = fn()
        except BaseException as exc:  # noqa: BLE001 - asserted by the test
            results[key] = exc
    thread = threading.Thread(target=target)
    thread.start()
    return thread


def _decision(h, words, *, episode=None) -> int:
    """A hub decision claimed by its own new episode (or ``episode``)."""
    with h.connect() as conn:
        announce(conn)
        if episode is None:
            episode = conn.execute(
                "INSERT INTO episodes (ts, session_id, summary, raw, harness, project, transcript_range) "
                "VALUES (now(), %s, %s, '{}'::jsonb, 'codex', %s, '0:1') RETURNING id",
                (f"codex:copy-{words[:20]}", f"summary for {words}", PROJECT)).fetchone()[0]
        fid = conn.execute("INSERT INTO decisions (project, text, episode_id) VALUES (%s, %s, %s) RETURNING id",
                           (PROJECT, words, episode)).fetchone()[0]
        provenance.record_claim(conn.cursor(), "decision", fid, "text", words, episode_id=episode)
        provenance.project(conn.cursor(), "decision", fid)
    return fid


def _new_hub(pg_cluster, *, ready: bool = True):
    h = pg_cluster.new_database()
    h.migrate()
    if ready:
        mark_ready(h)
    return h


def _file_state() -> tuple:
    path = hs.snapshot_path()
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


# ---- one copy, one mutex (F1): two copies, every pair of entry points ------------

ENTRIES = ("pull", "dump", "drain", "forget")


def _enter(name, h, victim):
    if name == "pull":
        return hs.pull_changes(include_ledger=False)["facts"]
    if name == "dump":
        return hs.refresh()["facts"]
    if name == "drain":
        return hs.sync_decision_changes()
    return forget.forget_everywhere(victim, memory_root=h.memory, gateway=False)["snapshot"]["facts"]


@pytest.mark.parametrize("first,second", [(a, b) for a in ENTRIES for b in ENTRIES if a != b])
def test_two_interleaved_copies_publish_the_later_snapshot(pg, replica, monkeypatch, first, second):
    replica(pg)
    pg.memory.mkdir(exist_ok=True)
    kept = _decision(pg, DECISION)
    victims = [_capture(loop=f"victim loop {i}", decision=f"victim decision {i}") for i in range(2)]
    assert hs.refresh()["facts"].get("copied")
    # An unrelated transaction holds the hub's xmin where it is throughout.
    hold = pg.connect()
    hold.execute("SELECT pg_current_xact_id()")
    pause = Pause(monkeypatch)
    results: dict = {}
    one = _run(lambda: _enter(first, pg, victims[0]), results, "first")
    assert pause.read.wait(60), results
    with pg.connect() as conn:
        conn.execute("UPDATE decisions SET retract_reason = 'LATER' WHERE id = %s", (kept,))
    two = _run(lambda: _enter(second, pg, victims[1]), results, "second")
    time.sleep(0.5)
    pause.go.set()
    one.join(120)
    two.join(120)
    hold.rollback()
    hold.close()
    assert not any(isinstance(v, BaseException) for v in results.values()), results
    assert results["second"].get("copied"), results
    assert rows("SELECT retract_reason FROM decisions WHERE id = ?", (kept,)) == [("LATER",)]
    assert stamp() is not None and shown() > 0
    for i, victim in enumerate(victims):
        forgotten = (first, second)[i] == "forget"
        assert (f"victim decision {i}" in readers()) is not forgotten


def test_no_entry_point_takes_the_copy_lock_inside_the_memory_lock_or_write_gate(pg, replica, monkeypatch, tmp_path):
    replica(pg)
    pg.memory.mkdir(exist_ok=True)
    victim = _capture(decision=DECISION)
    seen: list = []
    real = hs._fact_copy_lock

    @contextmanager
    def spy(path, deadline):
        seen.append((memory_lock._HELD.get(), tombstones._OWNED_WRITER_LOCK.get()))
        with real(path, deadline):
            yield

    monkeypatch.setattr(hs, "_fact_copy_lock", spy)
    assert hs.refresh()["ok"]
    assert hs.pull_changes(include_ledger=False)["ok"]
    hs.sync_decision_changes()
    forget.forget_everywhere(victim, memory_root=pg.memory, gateway=False)
    forget.send_to_hub()
    backup = paths.backup_local(dest=tmp_path / "backup.zip")
    paths.import_local(source=Path(backup["archive"]))
    assert len(seen) >= 6, seen
    assert all(held == (None, None) for held in seen), seen
    for outer in (tombstones.write_gate, memory_lock.shared):
        with outer(), pytest.raises(RuntimeError, match="fact-copy.lock"):
            hs.copy_facts()


# ---- a learned remote forget hides facts (F5) ---------------------------------------


def test_a_tombstone_learned_by_a_pull_hides_facts_when_the_copy_fails(pg, replica, monkeypatch):
    replica(pg)
    a = _capture(decision=DECISION + " for A")
    _capture(loop=ALT, decision=DECISION)
    assert hs.refresh()["facts"].get("copied")
    assert shown() > 0
    _forget(pg, a)  # another Mac's forget

    def fail(tx, local):
        raise psycopg.OperationalError("the copy's hub read failed")

    monkeypatch.setattr(hs, "_copy_read", fail)
    pulled = hs.pull_changes(include_ledger=False)
    assert pulled["ok"] and pulled["tombstones"] == 1 and not pulled["facts"]["ok"], pulled
    assert stamp() is None and shown() == 0
    assert DECISION not in readers()


# ---- readiness: no copy before the hub is ready, the last good copy kept -------------


def test_a_hub_still_indexing_publishes_no_copy_and_the_last_good_copy_stays(pg, replica):
    replica(pg)
    fid = _decision(pg, DECISION)
    assert hs.refresh()["facts"].get("copied")
    before = stamp()
    with pg.connect() as conn:
        conn.execute("UPDATE fact_copy_ready SET state = 'indexing'")
        conn.execute("UPDATE decisions SET retract_reason = 'NEWER' WHERE id = %s", (fid,))
    out = hs.copy_facts()
    assert not out["ok"] and out.get("not_ready"), out
    assert stamp() == before
    assert rows("SELECT retract_reason FROM decisions") == [(None,)]
    assert DECISION in readers()


def test_a_hub_with_0030_but_no_0047_publishes_no_copy_and_the_last_good_copy_stays(env, replica):
    replica(env, ready=False)
    with env.connect() as conn:
        assert conn.execute("SELECT to_regclass('public.forgotten_sources') IS NOT NULL").fetchone()[0]
        assert conn.execute("SELECT to_regclass('public.episode_identities') IS NULL").fetchone()[0]
        incarnation = hs._hub_incarnation(conn.cursor())
        snapshot = conn.execute("SELECT pg_current_snapshot()::text").fetchone()[0]
    out = hs.refresh()
    assert out["ok"] and not out["facts"]["ok"] and out["facts"].get("not_ready"), out
    # A copy this Mac took from this database earlier (written by hand: the
    # schema it came from is gone).
    with sqlite3.connect(hs.snapshot_path()) as con:
        con.execute("INSERT INTO decisions (id, project, text) VALUES (1, ?, 'LAST GOOD COPY')", (PROJECT,))
        hs._meta_put(con, hs._FACT_DIGEST_KEY, hs._current_refusal_digest())
        hs._meta_put(con, hs._FACT_STAMP_KEY, json.dumps({
            "incarnation": incarnation, "destination": hs._hub_destination(), "snapshot": snapshot}))
    before = stamp()
    out = hs.copy_facts()
    assert not out["ok"] and out.get("not_ready"), out
    assert stamp() == before and "LAST GOOD COPY" in readers()


# ---- a crash just before and just after the publication -------------------------------

_CHILD = r"""
import os, sys
import psycopg
from khipu import db, hub_snapshot as hs
dsn, stage = sys.argv[1], sys.argv[2]
hs.try_hub_connect = lambda **kw: psycopg.connect(dsn)
db.resolve_dsn = lambda: dsn
def hook(at):
    if at == stage:
        os._exit(9)
hs._copy_hook = hook
hs.copy_facts()
os._exit(0)
"""


@pytest.mark.parametrize("stage,new", [("commit", False), ("committed", True)])
def test_a_crash_around_the_publication_leaves_one_whole_copy(pg, replica, tmp_path, stage, new):
    replica(pg)
    fid = _decision(pg, DECISION)
    assert hs.refresh()["facts"].get("copied")
    before = stamp()
    with pg.connect() as conn:
        conn.execute("UPDATE decisions SET retract_reason = 'NEWER' WHERE id = %s", (fid,))
    script = tmp_path / "child.py"
    script.write_text(_CHILD)
    done = subprocess.run([sys.executable, str(script), pg.dsn, stage], env=dict(os.environ),
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 9, done.stderr
    after = stamp()
    if new:
        assert after is not None and after != before
        assert rows("SELECT retract_reason FROM decisions") == [("NEWER",)]
    else:
        assert after == before
        assert rows("SELECT retract_reason FROM decisions") == [(None,)]
    assert DECISION in readers()


# ---- offline -----------------------------------------------------------------------


def _offline(monkeypatch):
    def refused(**_kw):
        raise psycopg.OperationalError("connection refused")
    monkeypatch.setattr(hs, "try_hub_connect", refused)


def test_offline_with_an_intact_stamp_shows_the_last_copy(pg, replica, monkeypatch):
    replica(pg)
    _decision(pg, DECISION)
    assert hs.refresh()["facts"].get("copied")
    _offline(monkeypatch)
    out = hs.copy_facts()
    assert not out["ok"], out
    assert DECISION in readers() and not hs.facts_hidden()


def test_offline_with_a_listed_string_shows_none(pg, replica, monkeypatch):
    replica(pg)
    _decision(pg, DECISION)
    assert hs.refresh()["facts"].get("copied")
    _offline(monkeypatch)
    tombstones.publish_identities({"ab" * 32}, set())
    assert not hs.copy_facts()["ok"]
    assert shown() == 0 and hs.facts_hidden()


# ---- backups never carry facts -------------------------------------------------------


def test_a_backup_restore_carries_no_fact_row_and_lists_what_the_hub_lacks(pg, replica, tmp_path):
    replica(pg)
    _capture(loop=ALT, decision=DECISION, title="the checklist")
    assert hs.refresh()["facts"].get("copied")
    assert rows("SELECT count(*) FROM decisions") == [(1,)]
    exported = tmp_path / "export.sqlite"
    hs.export_replica_copy(hs.snapshot_path(), exported)
    with sqlite3.connect(exported) as con:
        assert [con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in hs._FACT_TABLES] == [0, 0, 0]
        assert con.execute("SELECT count(*) FROM memory_embeddings WHERE kind IN "
                           "('commitment', 'decision', 'deliverable')").fetchone()[0] == 0
        assert con.execute("SELECT value FROM snapshot_meta WHERE key = ?",
                           (hs._FACT_DIGEST_KEY,)).fetchone() is None
    # The backup's registry holds a forget the hub never received.
    tombstones.publish_identities({"cd" * 32}, set())
    backup = paths.backup_local(dest=tmp_path / "backup.zip")
    with hs._admitted(hs.snapshot_path(), time.monotonic() + 30) as (con, _gate):
        hs._drop_facts(con)  # not needed by the restore; checks it brings none
    out = paths.import_local(source=Path(backup["archive"]))
    scrub = out["snapshot_scrub"]
    assert scrub["imported"] and not scrub["facts"]["ok"] and scrub["facts"].get("unsent") == 1, scrub
    assert shown() == 0 and rows("SELECT count(*) FROM decisions") == [(0,)]
    listed = forget.unsent_forgets()
    assert listed["strings"] == ["k:" + "cd" * 32] and listed["facts_hidden"], listed


# ---- a replacement hub with an unrelated episode 42 (F4) -----------------------------


def test_a_replacement_hubs_unrelated_episode_42_is_listed_never_sent(pg, replica, pg_cluster, monkeypatch):
    replica(pg)
    _decision(pg, "OLD HUB FACT")
    tombstones.publish_episode_ids({42})  # forgotten on the old hub, which holds no 42 now
    assert hs.refresh()["facts"].get("copied")
    new = _new_hub(pg_cluster, ready=False)
    try:
        with new.connect() as conn:
            announce(conn)
            for i in range(1, 43):
                conn.execute("INSERT INTO episodes (id, ts, session_id, summary, raw, harness, project, "
                             "transcript_range) VALUES (%s, now(), %s, %s, '{}'::jsonb, 'codex', %s, '0:1')",
                             (i, f"codex:new-{i}", f"UNRELATED SUMMARY {i}", PROJECT))
            conn.execute("SELECT setval(pg_get_serial_sequence('episodes', 'id'), 42)")
        _decision(new, "NEW HUB FACT", episode=7)
        mark_ready(new)
        point(monkeypatch, new, dsn=pg.dsn)  # the same configured endpoint
        out = hs.copy_facts()
        assert not out["ok"] and out.get("incarnation_changed") and out.get("unsent") == 1, out
        assert shown() == 0 and stamp() is None
        listed = forget.unsent_forgets()
        assert listed["ids"] == [{"id": 42, "summary": "UNRELATED SUMMARY 42"}], listed
        with new.connect() as conn:
            assert conn.execute("SELECT count(*) FROM forgotten_sources").fetchone()[0] == 0
            assert conn.execute("SELECT deleted_at FROM episodes WHERE id = 42").fetchone()[0] is None
        done = forget.not_same_episode(42)
        assert done["ok"] and done["removed"] and done["facts"].get("copied"), done
        assert 42 not in tombstones.read_gate().episode_ids
        assert "NEW HUB FACT" in readers() and "OLD HUB FACT" not in readers()
    finally:
        pg_cluster.drop(new)


# ---- the owner send in batches, one batch timed out ------------------------------------


def test_the_owner_send_of_1000_strings_keeps_a_timed_out_batch_listed(pg, replica, monkeypatch):
    replica(pg)
    _decision(pg, DECISION)
    assert hs.refresh()["facts"].get("copied")
    strings = sorted(f"{i:064x}" for i in range(1000))
    tombstones.publish_identities(set(strings), set())
    listed = hs.copy_facts()
    assert listed.get("unsent") == 1000, listed
    real = provenance.refuse
    calls: list = []

    def refuse(cur, identities, **kw):
        calls.append(len(identities))
        if len(calls) == 3:
            cur.execute("SELECT pg_sleep(60)")
        return real(cur, identities, **kw)

    monkeypatch.setattr(provenance, "refuse", refuse)
    monkeypatch.setattr(forget, "FORGET_RETRY_BUDGET_S", 12)
    started = time.monotonic()
    out = forget.send_to_hub()
    # One deadline for the send and the copy after it (6.4).
    assert time.monotonic() - started < 12.7, out
    assert not out["ok"] and out["sent"] == 400 and out["pending"] == 600, out
    assert calls == [200, 200, 200], calls
    with pg.connect() as conn:
        held = {r[0] for r in conn.execute("SELECT identity FROM forgotten_sources")}
    assert held >= set(strings[:400]) and not held & set(strings[400:])
    assert forget.unsent_forgets()["strings"] == ["k:" + s for s in strings[400:]]
    assert shown() == 0


# ---- the skip (F8): exact, never false ------------------------------------------------


def test_an_unchanged_hub_writes_nothing(pg, replica):
    replica(pg)
    _decision(pg, DECISION)
    assert hs.refresh()["facts"].get("copied")
    assert hs.copy_facts().get("skipped")  # the first skip may record nothing new either
    before = _file_state()
    out = hs.copy_facts()
    assert out.get("skipped") and out["ok"], out
    assert _file_state() == before


def test_an_inverted_commit_order_change_is_not_skipped(pg, replica):
    replica(pg)
    x, y = _decision(pg, "decision X"), _decision(pg, "decision Y")
    assert hs.refresh()["facts"].get("copied")
    slow, fast = pg.connect(), pg.connect()
    slow.execute("UPDATE decisions SET retract_reason = 'SLOW' WHERE id = %s", (x,))
    fast.execute("UPDATE decisions SET retract_reason = 'FAST' WHERE id = %s", (y,))
    fast.commit()
    assert hs.copy_facts().get("copied")
    slow.commit()
    out = hs.copy_facts()
    assert out.get("copied"), out
    assert dict(rows("SELECT id, retract_reason FROM decisions")) == {x: "SLOW", y: "FAST"}


def test_a_pruned_prefix_past_the_stamp_or_an_empty_token_forces_a_copy(pg, replica):
    replica(pg)
    fid = _decision(pg, DECISION)
    assert hs.refresh()["facts"].get("copied")
    with pg.connect() as conn:
        conn.execute("UPDATE decisions SET retract_reason = 'C1' WHERE id = %s", (fid,))
    with pg.connect() as conn:
        c1 = conn.execute("SELECT max(xid)::text FROM fact_changes").fetchone()[0]
        conn.execute("UPDATE decisions SET retract_reason = 'C2' WHERE id = %s", (fid,))
    with pg.connect() as conn:
        conn.execute("UPDATE fact_changes SET at = now() - interval '31 days' WHERE xid <= %s::xid8", (c1,))
        assert provenance.prune_changes(conn)["pruned"] >= 1
    out = hs.copy_facts()
    assert out.get("copied"), out
    assert hs.copy_facts().get("skipped")
    # Empty-token initialization: no row at all never skips.
    with pg.connect() as conn:
        conn.execute("UPDATE fact_changes SET at = now() - interval '31 days'")
        provenance.prune_changes(conn)
        assert conn.execute("SELECT count(*) FROM fact_changes").fetchone()[0] == 0
    assert hs.copy_facts().get("copied")


def _commitment_vector(h) -> tuple[int, str]:
    with h.connect() as conn:
        cid, text = conn.execute("SELECT id, text FROM commitments").fetchone()
        profile, dim = conn.execute("SELECT id, dim FROM embedding_profiles ORDER BY id LIMIT 1").fetchone()
        vector = "[" + ",".join(["1"] + ["0"] * (int(dim) - 1)) + "]"
        conn.execute("INSERT INTO memory_embeddings (profile, kind, ref, chunk_idx, chunk_text, content_hash, "
                     "embedding) VALUES (%s, 'commitment', %s, 0, %s, md5(%s), %s)",
                     (profile, str(cid), text, text, vector))
    return cid, profile


def test_a_vector_only_update_forces_a_copy(pg, replica):
    replica(pg)
    _capture(loop=ALT)
    assert hs.refresh()["facts"].get("copied")
    cid, _profile = _commitment_vector(pg)
    out = hs.copy_facts()
    assert out.get("copied") and out["vectors"] == 1, out
    assert hs.copy_facts().get("skipped")
    with pg.connect() as conn:
        conn.execute("UPDATE memory_embeddings SET built_at = now() + interval '1 second' "
                     "WHERE kind = 'commitment'")
    out = hs.copy_facts()
    assert out.get("copied") and out["vectors"] == 1 and out["vectors_kept"] == 0, out
    assert rows("SELECT ref FROM memory_embeddings WHERE kind = 'commitment'") == [(str(cid),)]


def test_an_incarnation_change_forces_a_copy(pg, replica, pg_cluster, monkeypatch):
    replica(pg)
    _decision(pg, DECISION)
    assert hs.refresh()["facts"].get("copied")
    assert hs.copy_facts().get("skipped")
    clone = _clone(pg_cluster, pg)
    try:
        point(monkeypatch, clone, dsn=pg.dsn)
        out = hs.copy_facts()
        assert out.get("copied") and out.get("incarnation_changed"), out
        with clone.connect() as conn:
            assert stamp()["incarnation"] == hs._hub_incarnation(conn.cursor())
    finally:
        pg_cluster.drop(clone)


def _clone(pg_cluster, h):
    """``CREATE DATABASE ... TEMPLATE h``: a clone of one database."""
    from tests.hermetic_pg import Hub

    h.close()
    name = h.name + "_clone"
    with pg_cluster.admin() as admin:
        admin.execute(f"CREATE DATABASE {name} TEMPLATE {h.name}")
    return Hub(pg_cluster, name)


# ---- N1: a copy read before an invalidation never re-stamps ---------------------------


def _hub_forget(h, eid):
    """Another Mac's forget: the hub refusal transaction alone; this Mac's
    registry learns nothing."""
    with h.connect() as conn, conn.cursor() as cur:
        identities = provenance.stored_identities(cur, [eid])[eid]
        out = provenance.refuse(cur, identities, episode_id=eid, also=[eid])
        conn.commit()
    assert out["tombstoned"] == [eid], out


@pytest.mark.parametrize("learner", ["pull", "live patch", "dump"])
def test_a_copy_read_before_a_learned_tombstone_never_restamps(pg, replica, monkeypatch, learner):
    replica(pg)
    a = _capture(decision=DECISION + " for A")
    _capture(loop=ALT, decision=DECISION)
    assert hs.refresh()["facts"].get("copied")
    pause = Pause(monkeypatch)
    results: dict = {}
    old = _run(hs.copy_facts, results, "old")
    assert pause.read.wait(60)
    _hub_forget(pg, a)  # another Mac's forget, after the old copy's read
    real_copy = hs.copy_facts
    monkeypatch.setattr(hs, "copy_facts", lambda **kw: {"ok": False, "skipped_in_test": True})
    if learner == "pull":
        assert hs.pull_changes(include_ledger=False)["tombstones"] == 1
    elif learner == "live patch":
        with pg.connect() as conn:
            row = conn.execute("SELECT id, deleted_at, summary FROM episodes WHERE id = %s", (a,)).fetchone()
        patched = hs.upsert_episode({"id": row[0], "deleted_at": row[1], "summary": row[2]}, [])
        assert patched["ok"], patched
    else:
        assert hs.refresh()["ok"]
    assert stamp() is None and shown() == 0
    pause.go.set()
    old.join(120)
    assert results["old"].get("invalidated") or not results["old"]["ok"], results
    assert stamp() is None and shown() == 0
    assert DECISION + " for A" not in readers()
    # The newer copy fails: facts stay hidden.
    monkeypatch.setattr(hs, "copy_facts", real_copy)
    with monkeypatch.context() as m:
        m.setattr(hs, "_read_fact_rows", lambda cur: (_ for _ in ()).throw(RuntimeError("transfer failed")))
        assert not hs.copy_facts()["ok"]
    assert stamp() is None and shown() == 0
    assert hs.copy_facts().get("copied")
    assert DECISION in readers() and DECISION + " for A" not in readers()


# ---- N3: the incarnation tells two databases of one cluster apart ----------------------


def test_switching_hubs_restoring_or_cloning_forces_fresh_certification(pg, replica, pg_cluster, monkeypatch):
    replica(pg)
    _decision(pg, "FACT OF A")
    other = _new_hub(pg_cluster)
    _decision(other, "FACT OF B")
    assert hs.refresh()["facts"].get("copied")
    assert hs.copy_facts().get("skipped")
    made = [other]
    try:
        point(monkeypatch, other)  # the configured hub switched
        out = hs.copy_facts()
        assert out.get("copied"), out
        assert "FACT OF B" in readers() and "FACT OF A" not in readers()
        point(monkeypatch, pg)
        assert hs.copy_facts().get("copied") and "FACT OF A" in readers()
        # A logical restore into a new database behind the same endpoint.
        restored = pg_cluster.new_database()
        made.append(restored)
        tools = os.path.dirname(PG_CTL)
        with restored.connect() as conn:
            conn.autocommit = True
            for schema in conn.execute("SELECT nspname FROM pg_namespace WHERE nspname = 'public'"):
                conn.execute("DROP SCHEMA public CASCADE")
                conn.execute("CREATE SCHEMA public")
        restored.close()
        dumped = subprocess.run([os.path.join(tools, "pg_dump"), "-Fc", "-d", pg.dsn],
                                capture_output=True, timeout=120, check=True).stdout
        subprocess.run([os.path.join(tools, "pg_restore"), "--no-owner", "-d", restored.dsn],
                       input=dumped, capture_output=True, timeout=120, check=True)
        point(monkeypatch, restored, dsn=pg.dsn)
        out = hs.copy_facts()
        assert out.get("copied") and out.get("incarnation_changed"), out
        assert hs.copy_facts().get("skipped")
        # A TEMPLATE clone behind the same endpoint.
        clone = _clone(pg_cluster, pg)
        made.append(clone)
        point(monkeypatch, clone, dsn=pg.dsn)
        out = hs.copy_facts()
        assert out.get("copied") and out.get("incarnation_changed"), out
        assert "FACT OF A" in readers()
    finally:
        for h in made:
            pg_cluster.drop(h)


# ---- N6: no readiness row, no copy ----------------------------------------------------


def test_a_hub_without_its_readiness_row_admits_no_copy(pg, replica, pg_cluster):
    replica(pg)
    _decision(pg, DECISION)
    assert hs.refresh()["facts"].get("copied")
    before = stamp()
    with psycopg.Connection.connect(pg.dsn, autocommit=True) as conn:
        # Bypassing the triggers is outside the supported contract (6.1);
        # this is what such a hub looks like.
        conn.execute("ALTER TABLE fact_copy_ready DISABLE TRIGGER USER")
        conn.execute("DELETE FROM fact_copy_ready")
        conn.execute("ALTER TABLE fact_copy_ready ENABLE TRIGGER USER")
        conn.execute("UPDATE decisions SET retract_reason = 'NEWER'")
    out = hs.copy_facts()
    assert not out["ok"] and out.get("not_ready") and "missing" in out["error"], out
    assert stamp() == before and rows("SELECT retract_reason FROM decisions") == [(None,)]


# ---- D2: a detected new incarnation hides the old copy whatever happens next ----------


def test_a_detected_new_incarnation_hides_facts_even_when_the_copy_fails(pg, replica, pg_cluster, monkeypatch):
    replica(pg)
    _decision(pg, "FACT OF A")
    assert hs.refresh()["facts"].get("copied")
    other = _new_hub(pg_cluster, ready=False)
    try:
        _decision(other, "FACT OF B")

        def restamp_from_a():
            point(monkeypatch, pg)
            assert hs.copy_facts().get("copied")
            assert shown() == 1

        # Readiness failure.
        point(monkeypatch, other, dsn=pg.dsn)
        out = hs.copy_facts()
        assert not out["ok"] and out.get("incarnation_changed") and out.get("not_ready"), out
        assert stamp() is None and shown() == 0
        # Transfer failure.
        mark_ready(other)
        restamp_from_a()
        point(monkeypatch, other, dsn=pg.dsn)
        with monkeypatch.context() as m:
            m.setattr(hs, "_read_fact_rows", lambda cur: (_ for _ in ()).throw(RuntimeError("transfer failed")))
            assert not hs.copy_facts()["ok"]
        assert stamp() is None and shown() == 0
        # Load-rule failure: a forget the new database lacks (the old has it).
        restamp_from_a()
        with pg.connect() as conn:
            conn.execute("SELECT khipu_lock_identities(ARRAY['" + "ef" * 32 + "'])")
            conn.execute("INSERT INTO forgotten_sources (identity) VALUES (%s)", ("ef" * 32,))
        tombstones.publish_identities({"ef" * 32}, set())
        assert hs.copy_facts().get("copied")
        point(monkeypatch, other, dsn=pg.dsn)
        out = hs.copy_facts()
        assert not out["ok"] and out.get("unsent") == 1, out
        assert stamp() is None and shown() == 0
        # A copy paused after reading the old incarnation, then resumed while
        # the detection invalidated, does not re-stamp.
        point(monkeypatch, pg)
        assert hs.copy_facts().get("copied")
        pause = Pause(monkeypatch)
        results: dict = {}
        stale = _run(hs.copy_facts, results, "stale")
        assert pause.read.wait(60)
        with pg.connect() as conn:
            conn.execute("UPDATE decisions SET retract_reason = 'NEWER'")
        with hs._admitted(hs.snapshot_path(), time.monotonic() + 30) as (con, _gate):
            hs._invalidate_facts(con)  # what the detecting copy does (on_handoff)
        pause.go.set()
        stale.join(120)
        assert results["stale"].get("invalidated"), results
        assert stamp() is None and shown() == 0
        # A following successful copy restores visibility.
        with other.connect() as conn:
            conn.execute("SELECT khipu_lock_identities(ARRAY['" + "ef" * 32 + "'])")
            conn.execute("INSERT INTO forgotten_sources (identity) VALUES (%s)", ("ef" * 32,))
        point(monkeypatch, other, dsn=pg.dsn)
        assert hs.copy_facts().get("copied")
        assert "FACT OF B" in readers()
    finally:
        pg_cluster.drop(other)


# ---- D4: the publication contract ------------------------------------------------------


def _locks_free() -> bool:
    import fcntl

    with tombstones.lock_wait(0), tombstones.write_gate():
        pass
    fd = os.open(str(hs.snapshot_path().parent / hs.FACT_COPY_LOCK_NAME), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)
    return True


@pytest.mark.parametrize("stage", ["rows", "commit"])
def test_an_admitted_publication_stalled_past_the_deadline_completes(pg, replica, monkeypatch, stage):
    replica(pg)
    fid = _decision(pg, DECISION)
    assert hs.refresh()["facts"].get("copied")
    with pg.connect() as conn:
        conn.execute("UPDATE decisions SET retract_reason = 'NEWER' WHERE id = %s", (fid,))
    deadline = time.monotonic() + 6

    def stall(at):
        if at == stage:
            time.sleep(max(0.0, deadline - time.monotonic()) + 1.0)

    monkeypatch.setattr(hs, "_copy_hook", stall)
    out = hs.copy_facts(deadline=deadline)
    assert time.monotonic() > deadline
    assert out.get("copied"), out
    assert rows("SELECT retract_reason FROM decisions") == [("NEWER",)]
    assert _locks_free()


def test_a_publication_that_would_begin_after_the_deadline_is_not_admitted(pg, replica):
    replica(pg)
    fid = _decision(pg, DECISION)
    assert hs.refresh()["facts"].get("copied")
    before = stamp()
    with pg.connect() as conn:
        conn.execute("UPDATE decisions SET retract_reason = 'NEWER' WHERE id = %s", (fid,))
    deadline = time.monotonic() + 6
    held, release = threading.Event(), threading.Event()

    def hold_gate():
        with tombstones.write_gate():
            held.set()
            release.wait(60)

    holder = threading.Thread(target=hold_gate)
    holder.start()
    assert held.wait(10)
    try:
        out = hs.copy_facts(deadline=deadline)
        ended = time.monotonic()
    finally:
        release.set()
        holder.join(30)
    assert ended < deadline + 0.5, ended - deadline
    assert not out["ok"] and "deadline" in out["error"], out
    assert stamp() == before and rows("SELECT retract_reason FROM decisions") == [(None,)]


def test_a_commit_error_leaves_the_previous_copy_and_stamp(pg, replica, monkeypatch):
    replica(pg)
    fid = _decision(pg, DECISION)
    assert hs.refresh()["facts"].get("copied")
    before = stamp()
    with pg.connect() as conn:
        conn.execute("UPDATE decisions SET retract_reason = 'NEWER' WHERE id = %s", (fid,))

    def fail(at):
        if at == "commit":
            raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(hs, "_copy_hook", fail)
    out = hs.copy_facts()
    assert not out["ok"] and "disk I/O error" in out["error"], out
    assert stamp() == before and rows("SELECT retract_reason FROM decisions") == [(None,)]
    assert _locks_free()
