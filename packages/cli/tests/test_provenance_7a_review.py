# ruff: noqa: F811 - the hermetic fixtures are imported by name
"""Fact provenance, phase 7A: the code review's findings 2-8, each as its
reproduction (jobs/2026-10-23/astra-7a/report.md, probes/). Finding 1 is in
test_provenance_refusals.py beside the overlapping-carrier tests. Every
database is a hermetic one (tests/hermetic_pg.py)."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
import threading
import time

import psycopg
import pytest

from khipu import capture, capture_identity, db, forget, memory_lock, tombstones
from khipu.capture_identity import payload_sha256, source_key
from tests.fact_seed import announce
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.test_provenance_forget import (  # noqa: F401
    ALT, CANON, _capture, _payload, _rows, env, pg,
)
from tests.test_provenance_refusals import (
    _Proxy, _identities, _index, _live, _refuse, _refused, _refused_by, _write,
)


def test_f2_a_client_without_safe_cancel_still_returns_at_its_deadline(pg):
    """Without libpq 17's cancel the fallback blocks the interpreter on a
    stalled network, so no cancel is sent; the caller still returns."""
    proxy = _Proxy(pg)
    script = textwrap.dedent("""\
        import sys, time
        import psycopg.connection
        from khipu import provenance
        psycopg.connection.capabilities.has_cancel_safe = lambda: False
        def work(tx):
            print('armed', flush=True)
            time.sleep(0.4)
            tx.cur.execute('SELECT pg_sleep(30)')
        start = time.monotonic()
        try:
            provenance.hub_call(work, deadline=start + 2.5, connect=lambda **kw:
                psycopg.Connection.connect(sys.argv[1], connect_timeout=kw['connect_timeout']))
        except BaseException as e:
            print('returned', type(e).__name__, round(time.monotonic() - start, 2), flush=True)
        """)
    proc = subprocess.Popen([sys.executable, "-c", script, proxy.dsn],
                            env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "armed"
        proxy.blackhole(up=True)
        try:
            stdout, _ = proc.communicate(timeout=7)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=5)
            pytest.fail("the caller froze past its deadline")
    finally:
        for s in proxy.held:
            try:
                s.close()
            except OSError:
                pass
        proxy.sock.close()
    assert stdout.startswith("returned NotDone"), stdout


def test_f3_the_local_memory_lock_wait_counts_against_the_deadline(pg, monkeypatch):
    e = _capture(loop=CANON)
    monkeypatch.setattr(forget, "FORGET_RETRY_BUDGET_S", 2.5)
    code = ("import fcntl,sys; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_SH); "
            "print('held',flush=True); sys.stdin.readline()")
    child = subprocess.Popen([sys.executable, "-c", code, str(memory_lock.lock_path())],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    result: dict = {}
    try:
        assert child.stdout.readline().strip() == "held"
        start = time.monotonic()
        thread = threading.Thread(target=lambda: result.update(
            forget.forget_everywhere(e, memory_root=pg.memory)))
        thread.start()
        thread.join(3.5)
        assert not thread.is_alive(), "the forget still waited on the local lock past its deadline"
        assert time.monotonic() - start < 3.5
    finally:
        child.communicate("\n", timeout=3)
    assert not result["ok"] and "not started" in result["error"], result
    assert _live(pg, e)


def test_f4_a_pre_0047_worker_abandoned_after_its_read_never_publishes(env, monkeypatch):
    e = _capture(loop=CANON)
    monkeypatch.setattr(forget, "FORGET_RETRY_BUDGET_S", 2.5)
    real_read, real_publish = forget._forget_read, forget.publish_local
    ready, release, published = threading.Event(), threading.Event(), threading.Event()

    def hold(*a, **kw):
        out = real_read(*a, **kw)
        ready.set()
        release.wait(5)
        return out

    def publish(*a, **kw):
        published.set()
        return real_publish(*a, **kw)

    monkeypatch.setattr(forget, "_forget_read", hold)
    monkeypatch.setattr(forget, "publish_local", publish)
    result = forget._forget_on_hub(db.connect, e, memory_root=env.memory)
    assert ready.is_set() and not result["ok"]
    release.set()
    assert not published.wait(3)
    assert f"e:{e}" not in tombstones.read_gate().refusals and _live(env, e)


def test_f5_a_tombstoned_episodes_identity_inputs_are_frozen(env):
    """A cutover tombstone's raw cannot be rewritten to a live episode's
    legacy identity, so the index pass never refuses what it did not carry."""
    e = _capture(loop=CANON)
    pf = _payload(loop=ALT)
    f = _write(pf)
    with env.connect() as conn, conn.cursor() as cur:
        assert forget.forget_episode(cur, e)["ok"]
    env.to_0047()
    alias = next(x for x in tombstones.hub_identity_set(pf) if x.startswith("legacy:"))

    def rewrite():
        with env.connect() as conn:
            conn.execute("UPDATE episodes SET raw = raw || %s::jsonb WHERE id = %s",
                         (json.dumps({"ts": pf["ts"], "summary": pf["summary"]}), e))

    _refused_by("khipu_tombstoned_target", rewrite)
    for column, value in (("session_id", "codex:moved"), ("transcript_range", "9:9")):
        _refused_by("khipu_tombstoned_target", lambda: env.connect().execute(
            f"UPDATE episodes SET {column} = %s WHERE id = %s", (value, e)))
    assert _index(env)["ok"]
    assert alias not in _refused(env) and _live(env, f)


def test_f6_an_insert_carrying_receipts_is_checked_and_indexed_by_them(pg):
    assert _index(pg)["state"] == "ready"
    refused_payload = _payload(loop=None)
    h = payload_sha256(refused_payload)
    _refuse(pg, [h])

    def insert(receipt_payload):
        p = _payload(loop=None)
        p["merged_from"] = [{"source_id": payload_sha256(receipt_payload), "payload": receipt_payload}]
        with pg.connect() as conn:
            announce(conn, source_key(p))
            return conn.execute(
                "INSERT INTO episodes(ts,summary,raw,harness,session_id,transcript_range) "
                "VALUES (%s,%s,%s::jsonb,%s,%s,%s) RETURNING id",
                (p["ts"], p["summary"], json.dumps(p), p["harness"], p["session_id"],
                 p["transcript_range"])).fetchone()[0]

    with pytest.raises(psycopg.Error) as refused:
        insert(refused_payload)
    assert tombstones.is_forgotten_refusal(refused.value), refused.value
    other = _payload(loop=None)
    eid = insert(other)
    assert payload_sha256(other) in _identities(pg, eid)
    assert _refuse(pg, [payload_sha256(other)])["tombstoned"] == [eid]


def test_f6_a_source_receipt_never_moves_to_another_identity_or_episode(pg):
    e = _capture(loop=None)
    f = _capture(loop=CANON)
    assert _index(pg)["state"] == "ready"
    h = "b" * 64
    _refuse(pg, [h])
    for sql, params in (
            ("UPDATE episode_sources SET source_key = %s, payload_sha256 = %s, episode_id = %s "
             "WHERE episode_id = %s", (h, h, f, e)),
            ("UPDATE episode_sources SET episode_id = %s WHERE episode_id = %s", (f, e)),
            ("UPDATE episode_sources SET payload_sha256 = %s WHERE episode_id = %s", (h, e))):
        _refused_by("khipu_append_only", lambda: pg.connect().execute(sql, params))
    assert _live(pg, f) and _rows(pg, "SELECT count(*) FROM episode_sources WHERE source_key = %s",
                                  (h,)) == [(0,)]


def test_f7_a_timeout_in_the_source_row_defers_the_whole_capture(pg, monkeypatch):
    real = capture_identity._insert_source
    calls = []

    def blocked(cur, *args, **kw):
        calls.append(1)
        cur.execute("SET LOCAL statement_timeout = '50ms'")
        cur.execute("SELECT pg_sleep(0.2)")
        return real(cur, *args, **kw)

    monkeypatch.setattr(capture_identity, "_insert_source", blocked)
    payload = _payload(loop=CANON)
    with pytest.raises(capture.FactStepDeferred):
        capture.write_pg(payload)
    assert calls
    assert _rows(pg, "SELECT count(*) FROM episodes WHERE summary = %s", (payload["summary"],)) == [(0,)]


def test_f8_a_directory_fsync_failure_after_the_replace_is_a_publication(pg, monkeypatch):
    """The replace installed the registry, so the forget goes on to the hub
    instead of reporting the registry unpublished while it is listed."""
    e = _capture(loop=CANON)
    tombstones.publish_identities({"f" * 64}, set())
    before = tombstones.registry_path().read_bytes()
    real = os.fsync

    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("injected directory fsync failure after replace")
        return real(fd)

    with monkeypatch.context() as m:
        m.setattr(tombstones.os, "fsync", fail_directory)
        result = forget._forget_on_hub(db.connect, e, memory_root=pg.memory)
    assert tombstones.registry_path().read_bytes() != before
    assert f"e:{e}" in tombstones.read_gate().refusals
    assert result["ok"], result
    assert not _live(pg, e)
