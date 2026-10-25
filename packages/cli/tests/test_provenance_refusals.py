# ruff: noqa: F811 - the hermetic fixtures are imported by name
"""Fact provenance, phase 7A: hub refusals (design section 6.1 and 6.4).

Scope: docs/plans/2026-10-23-fact-provenance.md, section 6 and phase 7A's
required tests (the base list, the re-review's N2, N4, N5 and N6 and the
third review's D1, D3 and the forget half of D4). Every database is a
hermetic one (tests/hermetic_pg.py); races are schedules with barriers.

Every match of a refusal to an episode happens on the hub, in one READ
COMMITTED transaction under the per-identity advisory lock 0030's triggers
take: ``provenance.refuse()``, the forget built on it, and the index pass
that indexes history and sweeps the carriers of refused identities.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time

import psycopg
import pytest
from psycopg import IsolationLevel

from khipu import capture, db, forget, memory_lock, mirror, outbox, provenance, tombstones
from khipu.capture_identity import payload_sha256, record_source, source_key
from tests.fact_seed import announce
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.test_provenance_forget import (  # noqa: F401
    ALT, CANON, PROJECT, _capture, _payload, _rows, env, pg,
)

# ---- helpers -----------------------------------------------------------------------


def _write(payload: dict) -> int:
    out = capture.write_pg(dict(payload))
    assert out["episode_inserted"], out
    return int(out["episode_id"])


def _refused(h) -> set[str]:
    return {r[0] for r in _rows(h, "SELECT identity FROM forgotten_sources")}


def _live(h, eid: int) -> bool:
    return _rows(h, "SELECT deleted_at IS NULL FROM episodes WHERE id = %s", (eid,)) == [(True,)]


def _identities(h, eid: int) -> set[str]:
    return {r[0] for r in _rows(h, "SELECT identity FROM episode_identities WHERE episode_id = %s", (eid,))}


def _refuse(h, identities, **kw) -> dict:
    with h.connect() as conn, conn.cursor() as cur:
        out = provenance.refuse(cur, identities, **kw)
        conn.commit()
    return out


def _merge(h, eid: int, payload: dict, *, declare: bool = True) -> str:
    """Add ``payload`` to an episode's merge receipts with raw SQL, as
    history or another writer would, announcing its identities. Returns the
    receipt id (the payload hash)."""
    sha = payload_sha256(payload)
    with h.connect() as conn:
        if declare:
            provenance.declare_writer(conn.cursor())
        announce(conn, sha, *sorted(tombstones.hub_identity_set(payload)))
        conn.execute("UPDATE episodes SET raw = jsonb_set(raw, '{merged_from}', "
                     "coalesce(raw -> 'merged_from', '[]'::jsonb) || %s::jsonb) WHERE id = %s",
                     (json.dumps([{"source_id": sha, "payload": payload}]), eid))
    return sha


def _refused_by(constraint: str, fn) -> psycopg.Error:
    with pytest.raises(psycopg.errors.CheckViolation) as info:
        fn()
    assert info.value.diag.constraint_name == constraint, info.value
    return info.value


def _waiting_on_lock(h, pid: int | None = None, *, timeout: float = 10.0) -> int:
    """Wait until a backend (``pid``, else any) waits on a lock; its pid."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _rows(h, "SELECT pid FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                        "AND datname = current_database()")
        pids = [r[0] for r in rows if pid is None or r[0] == pid]
        if pids:
            return pids[0]
        time.sleep(0.02)
    raise AssertionError("no backend started waiting on a lock")


def _ready(h) -> tuple:
    return _rows(h, "SELECT state, target_id, indexed_through FROM fact_copy_ready")[0]


def _index(h, **kw) -> dict:
    with h.connect() as conn:
        return provenance.index_identities(conn, **kw)


def _without_triggers(h, sql: str, params=()) -> int:
    """History from before 0030: a row the hub's triggers would refuse now."""
    with h.connect() as conn:
        conn.execute("SET LOCAL session_replication_role = replica")
        eid = conn.execute(sql, params).fetchone()[0]
    return int(eid)


# ---- the codec ----------------------------------------------------------------------


@pytest.mark.parametrize("summary", [
    "plain ascii summary",
    "non-BMP \U0001F680 rocket and \U0001D518 fraktur",
    "normalised: café (NFC)",
    "normalised: café (NFD)",
])
def test_the_sql_legacy_form_equals_hub_form(pg, summary):
    payload = {"ts": "2026-10-24T10:00:00Z", "summary": summary}
    (tag,) = [t for t in tombstones.identity_strings(payload) if t.startswith("l:")]
    (sql,) = _rows(pg, "SELECT khipu_payload_legacy(%s::jsonb)", (json.dumps(payload),))[0]
    assert sql == tombstones.hub_form(tag)
    assert tombstones.hub_form("k:abc") == tombstones.hub_form("h:abc") == "abc"
    assert tombstones.hub_form("e:42") is None
    assert tombstones.tags_for_hub(sql) == {tag}


# ---- review 8's three probes (astra-prov8/probes/identity), on the hub ----------------


def _remote(monkeypatch):
    """Another Mac: no local refusal; the hub's own refusals still apply."""
    monkeypatch.setattr(tombstones, "is_forgotten", lambda payload: False)


def test_r8_a_refusal_serializes_with_a_concurrent_capture_of_its_source(pg, monkeypatch):
    """Review 8 P1-1: a carrier committed between a scan and the refusal.
    The refusal and the capture now take the same identity lock: the
    capture commits first and the refusal, waiting, tombstones it."""
    _remote(monkeypatch)
    a = _capture(loop=CANON)
    payload = _payload(loop=ALT)
    key = source_key(payload)
    held, release = threading.Event(), threading.Event()
    real = capture.run_fact_steps

    def hold(cur, p, eid):
        real(cur, p, eid)
        if p.get("session_id") == payload["session_id"]:
            held.set()
            release.wait(10)

    monkeypatch.setattr(capture, "run_fact_steps", hold)
    out: dict = {}
    remote = threading.Thread(target=lambda: out.update(capture=capture.write_pg(dict(payload))))
    remote.start()
    assert held.wait(10)
    refusal = pg.connect()
    pid = refusal.info.backend_pid

    def run_refusal():
        with refusal.cursor() as cur:
            out["refuse"] = provenance.refuse(cur, [key])
        refusal.commit()

    worker = threading.Thread(target=run_refusal)
    worker.start()
    _waiting_on_lock(pg, pid)
    release.set()
    remote.join(10)
    worker.join(10)
    b = out["capture"]["episode_id"]
    assert out["capture"]["episode_inserted"] and out["refuse"]["tombstoned"] == [b], out
    assert not _live(pg, b) and _live(pg, a)
    assert key in _refused(pg)
    assert _rows(pg, "SELECT text, latest_text FROM commitments WHERE status = 'open'") == [(CANON, None)]


def test_r8_an_empty_legacy_refusal_refuses_a_later_upload(pg, monkeypatch):
    """Review 8 P1-2: a legacy ts/summary refusal with no carrier is a
    durable hub row, and a later upload with that ts and summary is refused."""
    _remote(monkeypatch)
    _capture(loop=CANON)
    payload = _payload(loop=ALT)
    alias = "legacy:" + payload["ts"] + "\x1f" + hashlib.md5(payload["summary"].encode()).hexdigest()
    assert _refuse(pg, [alias])["tombstoned"] == []
    assert alias in _refused(pg)
    out = capture.write_pg(dict(payload))
    assert not out["episode_inserted"] and out.get("forgotten_source"), out
    assert _rows(pg, "SELECT count(*) FROM episodes WHERE summary = %s", (payload["summary"],)) == [(0,)]


def test_r8_a_refusal_with_two_carriers_is_all_or_nothing(pg, monkeypatch):
    """Review 8 P1-3: two carriers of one identity are tombstoned in one
    transaction; a failure on the second leaves both live and nothing refused."""
    _capture(loop=CANON)
    b = _capture(loop=ALT)
    c = _capture(loop=ALT + " soon")
    shared = _payload(loop=None, summary="historical source retained by two episodes")
    sha = _merge(pg, b, shared)
    _merge(pg, c, shared)
    real = forget.tombstone_on_hub

    def fail_on_c(cur, eid):
        if eid == c:
            raise RuntimeError("injected failure on the second carrier")
        return real(cur, eid)

    monkeypatch.setattr(forget, "tombstone_on_hub", fail_on_c)
    with pytest.raises(RuntimeError):
        _refuse(pg, [sha])
    assert _live(pg, b) and _live(pg, c) and sha not in _refused(pg)
    monkeypatch.setattr(forget, "tombstone_on_hub", real)
    assert _refuse(pg, [sha])["tombstoned"] == [b, c]
    assert not _live(pg, b) and not _live(pg, c)


# ---- the carrier gate and the identity locks ------------------------------------------


def test_the_carrier_gate_refuses_a_direct_refusal_while_a_carrier_is_live(pg):
    payload = _payload(loop=ALT)
    b = _write(payload)
    key = source_key(payload)
    assert key in _identities(pg, b)

    def direct():
        with pg.connect() as conn:
            conn.execute("INSERT INTO forgotten_sources (identity) VALUES (%s)", (key,))

    _refused_by("khipu_live_carrier", direct)
    assert _live(pg, b) and key not in _refused(pg)


@pytest.mark.parametrize("first", ["refusal", "capture"])
def test_a_direct_refusal_and_a_capture_of_one_source_never_both_commit(pg, monkeypatch, first):
    """Two sessions, each paused before its commit: a direct SQL refusal of
    K that takes no lock itself, and a capture of K. Exactly one commits."""
    payload = _payload(loop=ALT)
    key = source_key(payload)
    out: dict = {}
    held, release = threading.Event(), threading.Event()
    real = capture.run_fact_steps

    def hold(cur, p, eid):
        real(cur, p, eid)
        if p.get("session_id") == payload["session_id"]:
            held.set()
            release.wait(10)

    monkeypatch.setattr(capture, "run_fact_steps", hold)
    refusal = pg.connect()

    def run_capture():
        out["capture"] = capture.write_pg(dict(payload))

    def run_refusal():
        try:
            refusal.execute("INSERT INTO forgotten_sources (identity) VALUES (%s)", (key,))
            refusal.commit()
            out["refusal"] = "committed"
        except psycopg.Error as exc:
            out["refusal"] = exc

    if first == "refusal":
        refusal.execute("INSERT INTO forgotten_sources (identity) VALUES (%s)", (key,))
        writer = threading.Thread(target=run_capture)
        writer.start()
        _waiting_on_lock(pg)
        refusal.commit()
        release.set()
        writer.join(10)
        assert not out["capture"]["episode_inserted"] and out["capture"].get("forgotten_source"), out
        assert key in _refused(pg)
        assert _rows(pg, "SELECT count(*) FROM episodes WHERE session_id = %s",
                     (payload["session_id"],)) == [(0,)]
    else:
        writer = threading.Thread(target=run_capture)
        writer.start()
        assert held.wait(10)
        other = threading.Thread(target=run_refusal)
        other.start()
        _waiting_on_lock(pg, refusal.info.backend_pid)
        release.set()
        writer.join(10)
        other.join(10)
        assert out["capture"]["episode_inserted"], out
        assert isinstance(out["refusal"], psycopg.errors.CheckViolation), out
        assert out["refusal"].diag.constraint_name == "khipu_live_carrier"
        assert key not in _refused(pg) and _live(pg, out["capture"]["episode_id"])


# ---- restrictions ---------------------------------------------------------------------


def test_rewrites_removals_unannounced_writes_and_other_isolation_are_refused(pg):
    payload = _payload(loop=ALT)
    b = _write(payload)
    _refuse(pg, ["0" * 64])

    def run(sql, params=(), *, level=None, announced=False):
        def go():
            with pg.connect() as conn:
                if level is not None:
                    conn.isolation_level = level
                if announced:
                    announce(conn)
                conn.execute(sql, params)
        return go

    for sql in ("UPDATE forgotten_sources SET episode_id = 1",
                "DELETE FROM forgotten_sources",
                "UPDATE episode_identities SET identity = identity || 'x'",
                "DELETE FROM episode_identities"):
        _refused_by("khipu_append_only", run(sql))
    _refused_by("khipu_identity_rows", run("DELETE FROM episodes WHERE id = %s", (b,)))
    _refused_by("khipu_unannounced", run("INSERT INTO episodes (ts, summary, raw) VALUES (now(), 'x', '{}')"))
    _refused_by("khipu_read_committed", run("INSERT INTO forgotten_sources (identity) VALUES ('1')",
                                            level=IsolationLevel.REPEATABLE_READ))
    _refused_by("khipu_read_committed", run("INSERT INTO episodes (ts, summary, raw) VALUES (now(), 'y', '{}')",
                                            level=IsolationLevel.REPEATABLE_READ, announced=True))
    assert _live(pg, b) and _refused(pg) == {"0" * 64}


def test_truncates_and_the_readiness_row_delete_are_refused(pg):
    """N6."""
    for table in ("forgotten_sources", "episode_identities", "forgotten_at_cutover", "fact_copy_ready"):
        def go(table=table):
            with pg.connect() as conn:
                conn.execute(f"TRUNCATE {table} CASCADE")
        _refused_by("khipu_append_only", go)

    def delete_ready():
        with pg.connect() as conn:
            conn.execute("DELETE FROM fact_copy_ready")
    _refused_by("khipu_append_only", delete_ready)
    assert len(_rows(pg, "SELECT * FROM fact_copy_ready")) == 1


# ---- the index pass and readiness -----------------------------------------------------


def test_a_refusal_before_the_pass_reaches_its_legacy_carrier(env):
    """Incomplete backfill: a merged legacy carrier of K above the cursor.
    refuse(K) commits, readiness is refused, the pass's batch tombstones the
    carrier with its index rows, then readiness is accepted."""
    a = _capture(loop=CANON)
    b = _capture(loop=ALT)
    merged = _payload(loop=None, summary="a merged legacy source")
    sha = payload_sha256(merged)
    with env.connect() as conn:
        conn.execute("UPDATE episodes SET raw = jsonb_set(raw, '{merged_from}', %s::jsonb) WHERE id = %s",
                     (json.dumps([{"source_id": sha, "payload": merged}]), b))
    env.to_0047()
    assert _ready(env) == ("indexing", b, 0)
    assert _refuse(env, [sha])["tombstoned"] == []
    assert _live(env, b)

    def ready_now():
        with env.connect() as conn:
            conn.execute("UPDATE fact_copy_ready SET state = 'ready'")
    _refused_by("khipu_not_indexed", ready_now)

    out = _index(env, batch=1)
    assert out["ok"] and out["tombstoned"] == [b] and out["state"] == "ready", out
    assert not _live(env, b) and _live(env, a)
    assert sha in _identities(env, b)
    assert _ready(env) == ("ready", b, b)
    assert _rows(env, "SELECT text FROM commitments WHERE status = 'open'") == [(CANON,)]


def test_the_pass_sweeps_a_pre_0030_duplicate_and_the_dry_run_counts_it(env):
    """Existing-row sweep: a live duplicate, from before 0030, of a source
    forgotten before this design."""
    payload = _payload(loop=None, summary="the forgotten source")
    x = _write(payload)
    with env.connect() as conn, conn.cursor() as cur:
        assert forget.forget_episode(cur, x)["soft_deleted"]
        conn.commit()
    assert source_key(payload) in _refused(env)
    z = _without_triggers(
        env, "INSERT INTO episodes (ts, summary, raw, harness, session_id, transcript_range, project) "
             "VALUES (now(), 'a duplicate written before 0030', '{}', %s, %s, %s, %s) RETURNING id",
        (payload["harness"], payload["session_id"], payload["transcript_range"], PROJECT))
    keep = _capture(loop=CANON)
    env.to_0047()
    dry = _index(env, dry_run=True)
    assert dry["ok"] and dry["episodes"] == [z], dry
    out = _index(env, batch=2)
    assert out["ok"] and out["tombstoned"] == [z], out
    assert dry["would_tombstone"] == len(out["tombstoned"])
    assert not _live(env, z) and _live(env, keep)


def test_n2_only_the_forgets_at_the_cutover_add_refusals(env, monkeypatch):
    """N2: collateral tombstones below and above the cursor, a later batch,
    a crash and resume, and a concurrent explicit forget during the pass."""
    def legacy_alias_of(payload):
        return {"ts": payload["ts"], "summary": payload["summary"]}

    low = _capture(loop="An unrelated low item")
    xp = _payload(loop=None, summary="forgotten before the cutover")
    x = _write(xp)
    high = _capture(loop="An unrelated high item")
    unrelated = _capture(loop="Forgotten during the pass")
    # Both collaterals hold x's legacy alias in a merged payload (as old
    # history can); 0030's forget never recorded legacy aliases.
    with env.connect() as conn:
        for eid in (low, high):
            conn.execute("UPDATE episodes SET raw = jsonb_set(raw, '{merged_from}', %s::jsonb) WHERE id = %s",
                         (json.dumps([{"source_id": hashlib.sha256(str(eid).encode()).hexdigest(),
                                       "payload": legacy_alias_of(xp)}]), eid))
    with env.connect() as conn, conn.cursor() as cur:
        assert forget.forget_episode(cur, x)["soft_deleted"]
        conn.commit()
    env.to_0047()
    own = {eid: _identities_now(env, eid) for eid in (low, high)}
    with env.connect() as conn, conn.cursor() as cur:
        x_ids = provenance.stored_identities(cur, [x])[x]
    alias = tombstones.hub_form("l:" + xp["ts"] + "\x1f" + hashlib.md5(xp["summary"].encode()).hexdigest())
    assert alias in x_ids and alias not in _refused(env)
    before = _refused(env)
    dry = _index(env, dry_run=True)
    assert dry["episodes"] == [low, high], dry

    # Batch 1 indexes low; the crash hits x's batch, which advances nothing.
    real = forget.tombstone_on_hub
    crashed = []

    def crash_once(cur, eid):
        if not crashed:
            crashed.append(eid)
            raise RuntimeError("injected crash mid-batch")
        return real(cur, eid)

    monkeypatch.setattr(forget, "tombstone_on_hub", crash_once)
    out = _index(env, batch=1)
    assert not out["ok"] and out["batches"] == 1, out
    assert _ready(env)[2] == low and alias not in _refused(env)
    monkeypatch.setattr(forget, "tombstone_on_hub", real)

    # The resumed pass meets an explicit forget committed between batches.
    real_stored = provenance.stored_identities
    explicit: dict = {}

    def forget_between(cur, ids):
        if not explicit and high in ids:
            explicit.update(forget._forget_on_hub(db.connect, unrelated, memory_root=env.memory))
        return real_stored(cur, ids)

    monkeypatch.setattr(provenance, "stored_identities", forget_between)
    out = _index(env, batch=1)
    assert out["ok"] and out["state"] == "ready", out
    assert explicit.get("ok") and explicit.get("soft_deleted"), explicit
    assert out["tombstoned"] == [low, high], out
    assert dry["would_tombstone"] == len(out["tombstoned"])
    assert not _live(env, low) and not _live(env, high) and not _live(env, unrelated)
    added = _refused(env) - before
    with env.connect() as conn, conn.cursor() as cur:
        unrelated_ids = real_stored(cur, [unrelated])[unrelated]
    assert added - unrelated_ids == x_ids - before, "only the cutover set adds refusals"
    for eid in (low, high):
        assert not (own[eid] - x_ids) & _refused(env), "a collateral's own identities are never refused"


def _identities_now(h, eid):
    with h.connect() as conn, conn.cursor() as cur:
        return provenance.stored_identities(cur, [eid])[eid]


class _InterceptingConn:
    """A connection whose cursors run ``before(sql)`` ahead of each statement."""

    def __init__(self, conn, before):
        self._conn, self._before = conn, before

    def cursor(self):
        conn, before = self._conn, self._before

        class _Cur:
            def __init__(self):
                self._cur = conn.cursor()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._cur.close()
                return False

            def execute(self, sql, params=None):
                before(sql)
                return self._cur.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._cur, name)

        return _Cur()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_an_index_batch_deadlocked_by_a_merge_advances_nothing(env):
    """Item 7: the pass holds a batch's identity locks and wants its rows; a
    merge holds a row and wants one of those identities. The aborted batch
    advances neither the cursor nor readiness; the resumed pass adds no
    refusal outside the frozen set."""
    target = _capture(loop=CANON)
    x = _capture(loop=ALT)
    with env.connect() as conn, conn.cursor() as cur:
        forget.forget_episode(cur, x)
        conn.commit()
    env.to_0047()
    target_key = next(iter(_rows(env, "SELECT source_key FROM episode_sources WHERE episode_id = %s",
                                 (target,))))[0]
    merger = env.connect()
    merger.execute("SET deadlock_timeout = '10s'")
    merger.execute("SELECT id FROM episodes WHERE id = %s FOR UPDATE", (target,))
    merge_error: list = []

    def merge():
        try:
            announce(merger, target_key)
            merger.execute("UPDATE episodes SET raw = jsonb_set(raw, '{merged_from}', %s::jsonb) WHERE id = %s",
                           (json.dumps([{"source_id": "d" * 64, "payload": {"note": "late"}}]), target))
            merger.commit()
        except psycopg.Error as exc:
            merge_error.append(exc)
            merger.rollback()

    started: list = []

    def before(sql):
        if "FOR UPDATE" in sql and "ORDER BY id FOR UPDATE" in sql and not started:
            started.append(True)
            t = threading.Thread(target=merge)
            t.start()
            started.append(t)
            _waiting_on_lock(env, merger.info.backend_pid)

    before_refused = _refused(env)
    raw = env.connect()
    raw.execute("SET deadlock_timeout = '50ms'")
    raw.commit()
    out = provenance.index_identities(_InterceptingConn(raw, before), batch=10)
    started[1].join(15)
    assert not out["ok"] and "Deadlock" in out["error"], out
    assert not merge_error, merge_error
    assert _ready(env) == ("indexing", x, 0)
    assert _refused(env) == before_refused
    out = _index(env)
    assert out["ok"] and out["state"] == "ready", out
    with env.connect() as conn, conn.cursor() as cur:
        x_ids = provenance.stored_identities(cur, [x])[x]
    assert _refused(env) - before_refused <= x_ids
    assert _live(env, target)


# ---- overlapping carriers -------------------------------------------------------------


def _pair(h):
    pe, pf = _payload(loop=None), _payload(loop=None)
    e, f = _write(pe), _write(pf)
    shared = _payload(loop=None, summary="a source both episodes merged")
    sha = _merge(h, e, shared)
    _merge(h, f, shared)
    return e, f, source_key(pe), sha, source_key(pf), pf


def test_overlapping_carriers_refuse_exactly_what_was_asked(pg):
    """E carries K and H, F carries H and G: refuse(K) tombstones E only and
    refuses nothing else; forget_episode(E) tombstones E and F and leaves G
    unrefused."""
    e, f, k, h, g, _pf = _pair(pg)
    out = _refuse(pg, [k])
    assert out == {**out, "refused": [k], "tombstoned": [e]}
    assert not _live(pg, e) and _live(pg, f)
    assert _refused(pg) == {k}

    e2, f2, k2, h2, g2, _pf2 = _pair(pg)
    out = forget._forget_on_hub(db.connect, e2, memory_root=pg.memory)
    assert out["ok"] and out["soft_deleted"] and out["carriers"] == [f2], out
    assert not _live(pg, e2) and not _live(pg, f2)
    assert {k2, h2} <= _refused(pg) and g2 not in _refused(pg)


def test_overlapping_carriers_an_upload_of_g_afterwards_lands(pg):
    """Owner ruling: the refusal table is the one authority, so G, never
    refused, lands beside its collateral tombstone F, and lands once."""
    e, f, _k, _h, g, pf = _pair(pg)
    assert forget._forget_on_hub(db.connect, e, memory_root=pg.memory)["ok"]
    assert g not in _refused(pg)
    out = capture.write_pg(dict(pf))
    assert out["episode_inserted"], out
    assert out["episode_id"] != f and _live(pg, out["episode_id"]) and not _live(pg, f)
    again = capture.write_pg(dict(pf))
    assert not again["episode_inserted"] and again["episode_id"] == out["episode_id"], again


def test_overlapping_carriers_new_evidence_under_g_lands_with_its_claims(pg):
    """The review's variant: new evidence under G's natural identity is a
    capture of its own, not consumed as forgotten with its outbox job."""
    e, f, _k, _h, g, pf = _pair(pg)
    assert forget._forget_on_hub(db.connect, e, memory_root=pg.memory)["ok"]
    newer = {**pf, "summary": "New evidence collected after collateral forget",
             "open_loops": [{"text": CANON, "kind": "followup", "owner": "assistant"}]}
    assert source_key(newer) == g
    outbox.enqueue(newer, reason="new evidence under G")
    assert outbox.drain()["replayed"] == 1 and not outbox.jobs()
    assert _rows(pg, "SELECT count(*) FROM commitments WHERE text = %s",
                 (newer["open_loops"][0]["text"],)) == [(1,)]
    assert not _live(pg, f)


def test_a_distinct_capture_on_a_collateral_tombstones_legacy_key_lands(pg):
    """The review's probe: a distinct source colliding only on F's legacy key
    is retained as a conflict while F is live, and lands with its claims
    once F is a collateral tombstone, never consumed without them."""
    e, f, _k, _h, g, pf = _pair(pg)
    distinct = {k: v for k, v in pf.items()
                if k not in ("harness", "session_id", "transcript_range", "window_id", "part")}
    distinct["open_loops"] = [{"text": ALT, "kind": "followup", "owner": "assistant"}]
    assert source_key(distinct) != g
    outbox.enqueue(distinct, reason="distinct source legacy collision")
    first = outbox.drain()
    assert first["failed"] == 1 and len(outbox.jobs()) == 1
    assert forget._forget_on_hub(db.connect, e, memory_root=pg.memory)["ok"]
    assert outbox.drain()["replayed"] == 1 and not outbox.jobs()
    assert _rows(pg, "SELECT count(*) FROM commitments WHERE text = %s",
                 (distinct["open_loops"][0]["text"],)) == [(1,)]


def _recover(h, line: dict, entry: str) -> dict:
    h.memory.mkdir(parents=True, exist_ok=True)
    (h.memory / "episodes.jsonl").write_text(json.dumps(line) + "\n")
    if entry == "tail":
        return mirror.sync_recent_episodes(h.memory)
    return mirror.reconcile_memory_root(h.memory)


def _live_ids(h) -> list[int]:
    return [r[0] for r in _rows(h, "SELECT id FROM episodes WHERE deleted_at IS NULL ORDER BY id")]


@pytest.mark.parametrize("entry", ["tail", "reconcile"])
def test_7a2_p2_1_file_recovery_imports_g_beside_its_collateral_tombstone(pg, entry):
    """Astra 7A re-review #1: a file line for G, which the forget of E never
    refused, is not taken as present or forgotten because its legacy key is
    held by F, tombstoned only as a collateral carrier. Recovery imports it
    with its claims, once."""
    e, f, _k, _h, g, pf = _pair(pg)
    assert _index(pg)["state"] == "ready"
    assert forget._forget_on_hub(db.connect, e, memory_root=pg.memory)["ok"]
    assert g not in _refused(pg)
    pf["open_loops"] = [{"text": CANON, "kind": "followup", "owner": "assistant"}]
    _recover(pg, pf, entry)
    live = _live_ids(pg)
    assert len(live) == 1 and f not in live and not outbox.jobs(), live
    assert _rows(pg, "SELECT count(*) FROM commitments WHERE text = %s", (CANON,)) == [(1,)]
    assert _rows(pg, "SELECT count(*) FROM fact_claims WHERE episode_id = %s", (live[0],)) != [(0,)]
    _recover(pg, pf, entry)
    assert _live_ids(pg) == live and not outbox.jobs()


def test_7a2_p2_1_before_history_is_indexed_recovery_keeps_g_in_the_outbox(env):
    """The same line before the index pass has covered history: neither
    imported nor dropped, it waits in the outbox and lands once the pass is
    done."""
    _capture(loop=None)
    env.to_0047()
    e, f, _k, _h, g, pf = _pair(env)
    assert forget._forget_on_hub(db.connect, e, memory_root=env.memory)["ok"]
    assert g not in _refused(env)
    pf["open_loops"] = [{"text": CANON, "kind": "followup", "owner": "assistant"}]
    result = _recover(env, pf, "tail")
    assert result["inserted"] == 0 and len(outbox.jobs()) == 1
    assert f not in _live_ids(env)
    assert _index(env)["state"] == "ready"
    assert outbox.drain()["replayed"] == 1 and not outbox.jobs()
    assert _rows(env, "SELECT count(*) FROM commitments WHERE text = %s", (CANON,)) == [(1,)]


def test_7a2_p2_2_an_exact_retry_of_a_payload_key_capture_finds_its_live_episode(pg):
    """Astra 7A re-review #2: G has no natural key and its source row stays
    on its collateral tombstone F, so the re-upload G2 is recorded under its
    own key. An exact retry of G must still find G2 (the identity index
    records G's identity on it), not raise a legacy-key conflict or stay
    queued."""
    pe = _payload(loop=None)
    pf = {"ts": "2026-10-23T17:01:02Z", "summary": "Synthetic payload-key capture for replay",
          "open_loops": [{"text": CANON, "kind": "followup", "owner": "assistant"}]}
    e, f = _write(pe), _write(dict(pf))
    shared = _payload(loop=None, summary="Source shared by both")
    _merge(pg, e, shared)
    _merge(pg, f, shared)
    assert _index(pg)["state"] == "ready"
    assert forget._forget_on_hub(db.connect, e, memory_root=pg.memory)["ok"]
    assert source_key(pf) not in _refused(pg)
    first = capture.write_pg(dict(pf))
    assert first["episode_inserted"] and _live(pg, first["episode_id"]), first
    again = capture.write_pg(dict(pf))
    assert not again["episode_inserted"] and again["episode_id"] == first["episode_id"], again
    outbox.enqueue(dict(pf), reason="exact replay after uncertain delivery")
    assert outbox.drain()["replayed"] == 1 and not outbox.jobs()
    assert _live_ids(pg) == [first["episode_id"]]
    assert _rows(pg, "SELECT count(*) FROM commitments WHERE text = %s AND status = 'open'",
                 (CANON,)) == [(1,)]


# ---- a merge deadlocked by refuse() -----------------------------------------------------


def test_a_merge_deadlocked_by_refuse_defers_and_lands_on_retry(pg, monkeypatch):
    target = _capture(loop=CANON)
    target_key = _rows(pg, "SELECT source_key FROM episode_sources WHERE episode_id = %s", (target,))[0][0]
    payload = _payload(loop=ALT)
    incoming = source_key(payload)
    candidate = {"id": target}
    monkeypatch.setattr(capture, "_dedup_candidates", lambda cur, p: [candidate])
    monkeypatch.setattr(capture, "_dedup_cosine_best", lambda cur, p, c, t: (target, 0.99, "cosine"))
    real_connect = pg.connect

    def connect(*a, **kw):
        conn = real_connect()
        conn.execute("SET deadlock_timeout = '50ms'")
        conn.commit()
        return conn

    monkeypatch.setattr(db, "connect", connect)
    refusal = real_connect()
    refusal.execute("SET deadlock_timeout = '10s'")
    refusal.commit()
    merge_locked, refusal_waiting = threading.Event(), threading.Event()
    real_check = tombstones.hub_source_forgotten
    calls = []

    def pause_in_merge(cur, p, source=None):
        found = real_check(cur, p, source)
        calls.append(1)
        if len(calls) == 2 and not refusal_waiting.is_set():
            merge_locked.set()
            assert refusal_waiting.wait(10)
        return found

    monkeypatch.setattr(tombstones, "hub_source_forgotten", pause_in_merge)
    monkeypatch.setattr(outbox, "MAX_ATTEMPTS", 1)
    outbox.enqueue(payload, reason="test")
    report: dict = {}
    drain = threading.Thread(target=lambda: report.update(outbox.drain()))
    drain.start()
    assert merge_locked.wait(10)
    errors: list = []

    def run_refusal():
        try:
            with refusal.cursor() as cur:
                cur.execute("SELECT khipu_lock_identities(%s::text[])", ([incoming],))
                provenance.refuse(cur, [incoming, target_key])
        except psycopg.Error as exc:
            errors.append(exc)
        finally:
            refusal.rollback()

    worker = threading.Thread(target=run_refusal)
    worker.start()
    _waiting_on_lock(pg, refusal.info.backend_pid)
    refusal_waiting.set()
    drain.join(20)
    worker.join(20)
    assert not errors, errors
    assert report["failed"] == 1 and report.get("deferred") == 1 and not report.get("buried"), report
    (job_path,) = outbox.jobs()
    job = json.loads(job_path.read_text())
    assert job["fact_deferred"] is True and job["attempts"] == 1 and "DeadlockDetected" in job["deferral_reason"]
    assert not outbox.dead_jobs()
    assert outbox.drain()["replayed"] == 1
    assert outbox.jobs() == []
    merged = _rows(pg, "SELECT raw -> 'merged_from' FROM episodes WHERE id = %s", (target,))[0][0]
    assert [m["session_id"] for m in merged] == [payload["session_id"]], merged


# ---- one operation deadline (6.4) -----------------------------------------------------


def _direct(h):
    return lambda **kw: psycopg.Connection.connect(h.dsn, connect_timeout=kw["connect_timeout"])


def test_three_statements_within_their_limit_end_at_the_deadline(pg):
    start = time.monotonic()

    def work(tx):
        for _ in range(3):
            tx.cur.execute("SELECT pg_sleep(1.2)")

    with pytest.raises((psycopg.errors.QueryCanceled, provenance.NotDone)):
        provenance.hub_call(work, deadline=start + 3.0, connect=_direct(pg))
    assert 2.8 <= time.monotonic() - start <= 3.6


def test_a_lock_held_past_the_deadline_ends_at_the_deadline(pg):
    holder = pg.connect()
    holder.execute("SELECT pg_advisory_lock(4242)")
    start = time.monotonic()
    with pytest.raises((psycopg.errors.LockNotAvailable, psycopg.errors.QueryCanceled, provenance.NotDone)):
        provenance.hub_call(lambda tx: tx.cur.execute("SELECT pg_advisory_xact_lock(4242)"),
                            deadline=start + 3.0, connect=_direct(pg))
    assert 2.8 <= time.monotonic() - start <= 3.6
    holder.close()


def test_no_step_starts_with_less_time_than_its_floor(pg):
    opened = []
    with pytest.raises(provenance.NotDone):
        provenance.hub_call(lambda tx: None, deadline=time.monotonic() + 1.5,
                            connect=lambda **kw: opened.append(kw))
    assert opened == []

    class _Cur:
        sql: list = []

        def execute(self, sql, params=None):
            self.sql.append(sql)

    cur = _Cur()
    with pytest.raises(provenance.NotDone):
        provenance.BoundedCursor(cur, time.monotonic() - 0.01).execute("SELECT 1")
    assert cur.sql == []


def test_a_stalled_server_is_abandoned_and_cancelled_at_the_deadline(pg):
    box: dict = {}

    def work(tx):
        tx.cur.execute("SELECT pg_backend_pid()")
        box["pid"] = tx.cur.fetchone()[0]
        os.kill(box["pid"], signal.SIGSTOP)
        tx.cur.execute("SELECT 1")

    start = time.monotonic()
    try:
        with pytest.raises(provenance.NotDone):
            provenance.hub_call(work, deadline=start + 3.0, connect=_direct(pg))
        assert time.monotonic() - start <= 3.5
    finally:
        if "pid" in box:
            os.kill(box["pid"], signal.SIGCONT)
    gone = time.monotonic() + 10
    while _rows(pg, "SELECT count(*) FROM pg_stat_activity WHERE pid = %s", (box["pid"],)) != [(0,)]:
        assert time.monotonic() < gone, "the abandoned backend never ended"
        time.sleep(0.05)


# ---- a TCP proxy that loses bytes (N4, D3) ---------------------------------------------


class _Proxy:
    """Between a client and the hermetic hub. ``up``/``down``: pass bytes
    toward the server / the client. ``open``: accept new connections. A
    closed side is never passed on, so the server keeps its connection."""

    def __init__(self, h):
        self.target = ("127.0.0.1", int(h.dsn.rsplit(":", 1)[1].split("/")[0]))
        self.dsn = h.dsn.replace(f":{self.target[1]}/", ":{port}/")
        self.up = self.down = self.open = True
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.dsn = self.dsn.format(port=self.port)
        self.held: list = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            if not self.open:
                self.held.append(client)
                continue
            server = socket.create_connection(self.target)
            self.held += [client, server]
            threading.Thread(target=self._pump, args=(client, server, "up"), daemon=True).start()
            threading.Thread(target=self._pump, args=(server, client, "down"), daemon=True).start()

    def _pump(self, src, dst, way):
        while True:
            try:
                data = src.recv(65536)
            except OSError:
                return
            if not data:
                return
            if getattr(self, way):
                try:
                    dst.sendall(data)
                except OSError:
                    return

    def blackhole(self, *, up=False):
        self.up, self.down, self.open = up, False, False

    def connect(self, **kw):
        return psycopg.Connection.connect(self.dsn, connect_timeout=kw.get("connect_timeout", 5))


def test_d3_a_lost_response_frees_the_hub_locks_within_ten_seconds(pg):
    e = _capture(loop=CANON)
    key = "e" * 64
    proxy = _Proxy(pg)

    def work(tx):
        tx.cur.execute("SELECT id FROM episodes WHERE id = %s FOR UPDATE", (e,))
        tx.cur.execute("SELECT khipu_lock_identities(%s::text[])", ([key],))
        proxy.blackhole(up=True)  # the next statement runs; its response is lost
        tx.cur.execute("SELECT 1")

    start = time.monotonic()
    with pytest.raises(provenance.NotDone):
        provenance.hub_call(work, deadline=start + 3.0, connect=proxy.connect)
    probe = pg.connect()
    while True:
        free = probe.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended('khipu-source:' || %s, 0)) "
            "AND EXISTS (SELECT 1 FROM episodes WHERE id = %s FOR UPDATE SKIP LOCKED)", (key, e)).fetchone()[0]
        probe.rollback()
        if free:
            break
        assert time.monotonic() - start <= 3.0 + 10 + 2, "the server kept the abandoned locks"
        time.sleep(0.1)


def test_d3_a_session_whose_timeouts_cannot_be_set_is_not_used(pg, monkeypatch):
    monkeypatch.setattr(provenance, "SESSION_SETTINGS", ("SET idle_in_transaction_session_timeout = 'never'",))
    ran = []
    with pytest.raises(provenance.NotDone):
        provenance.hub_call(lambda tx: ran.append(tx), deadline=time.monotonic() + 5, connect=_direct(pg))
    assert ran == []


def _registry_bytes() -> bytes | None:
    path = tombstones.registry_path()
    return path.read_bytes() if path.exists() else None


def _local_locks_free() -> bool:
    with memory_lock.exclusive(), tombstones.lock_wait(0.5), tombstones._locked_registry():
        return True


@pytest.mark.parametrize("when", ["query", "commit"])
def test_n4_a_blackholed_forget_ends_at_the_deadline_and_publishes_nothing_late(pg, monkeypatch, when):
    e = _capture(loop=CANON)
    proxy = _Proxy(pg)
    pg.memory.mkdir(exist_ok=True)
    with monkeypatch.context() as m:
        m.setattr(db, "connect", proxy.connect)
        m.setattr(forget, "FORGET_RETRY_BUDGET_S", 4.0)
        if when == "query":
            real = provenance.refuse

            def lose(cur, *a, **kw):
                proxy.blackhole()
                return real(cur, *a, **kw)

            m.setattr(provenance, "refuse", lose)
        else:
            real_commit = provenance.HubTx.commit

            def lose_commit(self):
                conn, send = self.conn, self.conn.commit

                def blackholed():
                    proxy.blackhole()
                    return send()

                conn.commit = blackholed
                return real_commit(self)

            m.setattr(provenance.HubTx, "commit", lose_commit)
        start = time.monotonic()
        out = forget.forget_everywhere(e, memory_root=pg.memory, gateway=False)
        assert time.monotonic() - start <= 4.6
    assert not out["ok"] and not out.get("complete"), out
    assert out["uncertain"] is (when == "commit"), out
    assert _local_locks_free()
    published = _registry_bytes()
    proxy.up = proxy.down = proxy.open = True
    time.sleep(1.0)
    assert _registry_bytes() == published, "nothing is published after the deadline"
    # The server rolls the abandoned transaction back; a later forget lands.
    settle = time.monotonic() + 15
    while True:
        again = forget._forget_on_hub(db.connect, e, memory_root=pg.memory)
        if again.get("ok"):
            break
        assert time.monotonic() < settle, again
        time.sleep(0.5)
    assert not _live(pg, e)


def test_n4_a_blackholed_connect_ends_at_the_deadline(pg):
    proxy = _Proxy(pg)
    proxy.blackhole()
    start = time.monotonic()
    # Its connect_timeout is the time left in whole seconds, rounded down.
    with pytest.raises((provenance.NotDone, psycopg.errors.ConnectionTimeout)):
        provenance.hub_call(lambda tx: None, deadline=start + 3.0, connect=proxy.connect)
    assert time.monotonic() - start <= 3.5


def test_n4_process_shutdown_does_not_join_an_abandoned_worker(tmp_path):
    script = tmp_path / "abandon.py"
    script.write_text(textwrap.dedent("""
        import time
        from khipu import provenance
        start = time.monotonic()
        try:
            provenance.hub_call(lambda tx: None, deadline=start + 2.5,
                                connect=lambda **kw: time.sleep(60))
        except provenance.NotDone:
            print("returned", round(time.monotonic() - start, 1), flush=True)
    """), encoding="utf-8")
    start = time.monotonic()
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=30,
                          env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.startswith("returned"), proc.stdout
    assert time.monotonic() - start < 15


# ---- D4: forget's registry publication against the deadline ------------------------------


def test_d4_an_admitted_publication_runs_to_its_end_past_the_deadline(pg, monkeypatch):
    e = _capture(loop=CANON)
    monkeypatch.setattr(forget, "FORGET_RETRY_BUDGET_S", 3.0)
    real = tombstones._atomic_write

    def slow(path, value):
        time.sleep(3.5)
        return real(path, value)

    monkeypatch.setattr(tombstones, "_atomic_write", slow)
    out = forget._forget_on_hub(db.connect, e, memory_root=pg.memory)
    assert not out["ok"], out
    gate = tombstones.read_gate()
    assert f"e:{e}" in gate.refusals, "the admitted publication completed"
    assert _local_locks_free()
    assert _live(pg, e)


def test_d4_a_publication_that_cannot_begin_before_the_deadline_writes_nothing(pg, monkeypatch):
    e = _capture(loop=CANON)
    monkeypatch.setattr(forget, "FORGET_RETRY_BUDGET_S", 3.0)
    before = _registry_bytes()
    holding, done = threading.Event(), threading.Event()

    def hold():
        with tombstones._locked_registry():
            holding.set()
            done.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    assert holding.wait(5)
    out = forget._forget_on_hub(db.connect, e, memory_root=pg.memory)
    done.set()
    holder.join(5)
    assert not out["ok"] and "busy" in out["error"], out
    assert _registry_bytes() == before and _live(pg, e)


def test_d4_a_late_worker_result_is_never_published(pg, monkeypatch):
    e = _capture(loop=CANON)
    monkeypatch.setattr(forget, "FORGET_RETRY_BUDGET_S", 3.0)
    before = _registry_bytes()
    real = forget._forget_read

    def late(cur, *a, **kw):
        time.sleep(3.5)
        return real(cur, *a, **kw)

    monkeypatch.setattr(forget, "_forget_read", late)
    out = forget._forget_on_hub(db.connect, e, memory_root=pg.memory)
    assert not out["ok"], out
    time.sleep(1.0)
    assert _registry_bytes() == before and _live(pg, e)


def test_d4_a_commit_error_leaves_the_previous_registry(pg, monkeypatch):
    e = _capture(loop=CANON)
    tombstones.publish_identities({"f" * 64}, set())
    before = _registry_bytes()

    def fail(src, dst):
        raise OSError("injected rename failure")

    with monkeypatch.context() as m:
        m.setattr(tombstones.os, "replace", fail)
        out = forget._forget_on_hub(db.connect, e, memory_root=pg.memory)
    assert not out["ok"] and "not published" in out["error"], out
    assert _registry_bytes() == before and _live(pg, e)


# ---- N5: identity additions serialize on the episode row -------------------------------


@pytest.mark.parametrize("attach", ["merge", "receipt"])
def test_n5_an_identity_added_during_a_forget_is_refused_or_restarts_it(pg, monkeypatch, attach):
    e = _capture(loop=CANON)
    late = _payload(loop=None, summary="attached while the forget runs")
    late_key = source_key(late)
    events: list = []
    real_publish, real_refuse = forget.publish_local, provenance.refuse

    def publish(read, eid, hub):
        events.append(("publish", frozenset(hub)))
        real_publish(read, eid, hub)
        if len(events) == 1:
            # After the enumeration, before the forget's locks.
            if attach == "merge":
                _merge(pg, e, late, declare=False)
            else:
                with pg.connect() as conn, conn.cursor() as cur:
                    record_source(cur, e, "merged", {"source_key": late_key,
                                                     "payload_sha256": payload_sha256(late)},
                                  ts=late["ts"], summary=late["summary"])
            events.append(("attached",))

    blocked: dict = {}

    def refuse(cur, identities, **kw):
        events.append(("refuse", frozenset(identities)))
        # A receipt insert after the stable read waits on the locked episode.
        def receipt():
            try:
                with pg.connect() as conn, conn.cursor() as c:
                    record_source(c, e, "merged", {"source_key": "9" * 64, "payload_sha256": None})
                blocked["result"] = "committed"
            except Exception as exc:  # noqa: BLE001
                blocked["result"] = exc
        t = threading.Thread(target=receipt)
        t.start()
        blocked["pid"] = _waiting_on_lock(pg)
        blocked["thread"] = t
        return real_refuse(cur, identities, **kw)

    monkeypatch.setattr(forget, "publish_local", publish)
    monkeypatch.setattr(provenance, "refuse", refuse)
    out = forget._forget_on_hub(db.connect, e, memory_root=pg.memory)
    blocked["thread"].join(10)
    assert out["ok"] and out["soft_deleted"], out
    kinds = [ev[0] for ev in events]
    assert kinds == ["publish", "attached", "publish", "refuse"], kinds
    grown, refused_set = events[2][1], events[3][1]
    assert late_key in grown and grown == refused_set, "the grown set is published before the retry"
    assert late_key in _refused(pg)
    assert tombstones.is_forgotten_refusal(blocked["result"]), blocked
    gate = tombstones.read_gate()
    assert all(tombstones.tags_for_hub(x) & gate.tags for x in _refused(pg)), \
        "the local registry covers every identity refused on the hub"


# ---- D1: no new identity on a tombstoned episode ----------------------------------------


def test_d1_no_new_identity_attaches_to_a_tombstoned_episode(env, monkeypatch):
    ep = _payload(loop=None, summary="forgotten before the cutover")
    e = _write(ep)
    with env.connect() as conn, conn.cursor() as cur:
        forget.forget_episode(cur, e)
        conn.commit()
    hp = _payload(loop=None, summary="a live carrier of H")
    carrier = _write(hp)
    h = source_key(hp)
    env.to_0047()
    assert _rows(env, "SELECT episode_id FROM forgotten_at_cutover") == [(e,)]
    dry = _index(env, dry_run=True)["would_tombstone"]

    def attempts():
        def receipt_sql():
            with env.connect() as conn:
                conn.execute("INSERT INTO episode_sources (source_key, episode_id, role) "
                             "VALUES (%s, %s, 'merged')", (h, e))
        _refused_by("khipu_tombstoned_target", receipt_sql)
        with env.connect() as conn, conn.cursor() as cur:
            with pytest.raises(tombstones.ForgottenSourceRefused):
                record_source(cur, e, "merged", {"source_key": h, "payload_sha256": None})
        _refused_by("khipu_tombstoned_target", lambda: _merge(env, e, hp))

        def identity_row():
            with env.connect() as conn:
                conn.execute("INSERT INTO episode_identities (identity, episode_id) VALUES (%s, %s)", (h, e))
        _refused_by("khipu_tombstoned_target", identity_row)

    attempts()
    out = _index(env)
    assert out["ok"] and out["state"] == "ready", out
    assert _identities(env, e), "the pass adds the old identities to the tombstoned row"
    attempts()
    assert h not in _refused(env) and _live(env, carrier)
    assert _index(env, dry_run=True)["would_tombstone"] == dry == 0

    # A receipt writer queued behind the transaction that tombstones E2.
    e2 = _capture(loop=ALT)
    forgetting = env.connect()
    with forgetting.cursor() as cur:
        assert forget.forget_episode(cur, e2)["soft_deleted"]
    queued: dict = {}

    def receipt():
        try:
            with env.connect() as conn, conn.cursor() as cur:
                record_source(cur, e2, "merged", {"source_key": "8" * 64, "payload_sha256": None})
            queued["result"] = "committed"
        except Exception as exc:  # noqa: BLE001
            queued["result"] = exc

    t = threading.Thread(target=receipt)
    t.start()
    _waiting_on_lock(env)
    forgetting.commit()
    t.join(10)
    assert isinstance(queued["result"], tombstones.ForgottenSourceRefused), queued
    assert "8" * 64 not in _refused(env)

    def cutover_insert():
        with env.connect() as conn:
            conn.execute("INSERT INTO forgotten_at_cutover (episode_id) VALUES (%s)", (e2,))
    _refused_by("khipu_append_only", cutover_insert)

    # mirror.py surfaces the refusal: the episode write is rolled back whole.
    def refused_source(*a, **kw):
        raise tombstones.ForgottenSourceRefused("khipu: tombstoned target: episode 0 is forgotten")

    monkeypatch.setattr("khipu.capture_identity.record_source", refused_source)
    mp = _payload(loop=None, summary="a mirror line")
    got: dict = {}
    with env.connect() as conn, conn.cursor() as cur:
        assert mirror._upsert_episode(cur, mp, out=got) is False
        conn.commit()
    assert got.get("forgotten_source") and "episode_id" not in got
    assert _rows(env, "SELECT count(*) FROM episodes WHERE summary = %s", (mp["summary"],)) == [(0,)]


# ---- the change token -------------------------------------------------------------------


def test_the_change_token_sees_two_fact_commits_in_inverted_xid_order(pg):
    _capture(loop=None, decision="Keep the release train weekly")
    _capture(loop=None, decision="Move the standup to Tuesday mornings")
    first, second = (r[0] for r in _rows(pg, "SELECT id FROM decisions ORDER BY id"))
    older, newer = pg.connect(), pg.connect()
    older.execute("SELECT khipu_project_fact('decision', %s)", (first,))
    xid_older = older.execute("SELECT pg_current_xact_id()::text").fetchone()[0]
    newer.execute("SELECT khipu_project_fact('decision', %s)", (second,))
    xid_newer = newer.execute("SELECT pg_current_xact_id()::text").fetchone()[0]
    assert int(xid_older) < int(xid_newer)
    newer.commit()
    snapshot = _rows(pg, "SELECT pg_current_snapshot()::text")[0][0]
    older.commit()
    tokens = {r[0] for r in _rows(pg, "SELECT xid::text FROM fact_changes WHERE via = 'copy'")}
    assert {xid_older, xid_newer} <= tokens
    unseen = _rows(pg, "SELECT xid::text FROM fact_changes WHERE via = 'copy' "
                       "AND xid >= pg_snapshot_xmin(%s::pg_snapshot) "
                       "AND NOT pg_visible_in_snapshot(xid, %s::pg_snapshot)", (snapshot, snapshot))
    assert [r[0] for r in unseen] == [xid_older], "the skip query must see the late commit"
    seen = _rows(pg, "SELECT count(*) FROM fact_changes WHERE via = 'copy' AND xid = %s::text::xid8 "
                     "AND pg_visible_in_snapshot(xid, %s::pg_snapshot)", (xid_newer, snapshot))
    assert seen == [(1,)]


def test_the_prune_removes_only_an_xid_prefix(pg):
    """A row stamped long ago can carry a newer xid than a recent one (its
    transaction began first and wrote later): it stays, so ``min(xid)``
    says no row above it was removed."""
    _capture(loop=None, decision="Keep the release train weekly")
    (fact,) = _rows(pg, "SELECT id FROM decisions")[0]

    def project() -> str:
        with pg.connect() as conn:
            conn.execute("SELECT khipu_project_fact('decision', %s)", (fact,))
            return conn.execute("SELECT pg_current_xact_id()::text").fetchone()[0]

    recent, late = project(), project()
    with pg.connect() as conn:
        conn.execute("UPDATE fact_changes SET at = now() - interval '40 days' WHERE xid <> %s::text::xid8",
                     (recent,))
    with pg.connect() as conn:
        assert provenance.prune_changes(conn)["ok"]
    left = {r[0] for r in _rows(pg, "SELECT DISTINCT xid::text FROM fact_changes")}
    assert left == {recent, late}, left
