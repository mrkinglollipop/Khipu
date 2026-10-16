"""The incremental pull (docs/plans/2026-10-16-offline-and-self-healing.md, slice 3)
against the throwaway PostgreSQL tests/hermetic_pg.py starts: another machine's
episode lands in the replica without a full dump and the freshness measurement
then reads in step; a patch missed during a dump is queued and retried; a
tombstone reaches the replica. Every row is synthetic; never the live hub."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from khipu import db, hub_snapshot as hs
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401

VEC = "[" + ",".join(["0.25"] * 768) + "]"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.setenv("KHIPU_TEST", "1")
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)
    db._TABLE_COLUMNS_CACHE.clear()
    yield
    db._TABLE_COLUMNS_CACHE.clear()


@pytest.fixture
def pg(hub, monkeypatch):  # noqa: F811
    monkeypatch.setenv("KHIPU_DATABASE_URL", hub.dsn)
    # The suite forbids psycopg.connect (the live hub); the throwaway cluster
    # is reached through the class constructor the fixture itself uses.
    monkeypatch.setattr(
        hs, "try_hub_connect",
        lambda *, connect_timeout=5: psycopg.Connection.connect(hub.dsn, connect_timeout=connect_timeout))
    conn = hub.connect()
    hub.migrate(conn=conn)
    cur = conn.cursor()
    cur.execute("SELECT id FROM embedding_profiles WHERE is_active")
    row = cur.fetchone()
    assert row, "the migrations seed an active embedding profile"
    conn.commit()
    return conn, row[0]


def add_episode(conn, profile: str, name: str, *, vector: bool = True) -> int:
    payload = {"ts": "2026-10-16T09:00:00Z", "summary": f"scratch {name}", "harness": "codex",
               "session_id": f"codex:{name}", "transcript_range": "0:10"}
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO episodes (ts, session_id, summary, raw, harness, transcript_range) "
        "VALUES (%s::timestamptz, %s, %s, %s::jsonb, %s, %s) RETURNING id",
        (payload["ts"], payload["session_id"], payload["summary"], json.dumps(payload),
         payload["harness"], payload["transcript_range"]))
    eid = cur.fetchone()[0]
    if vector:
        cur.execute(
            "INSERT INTO memory_embeddings (profile, kind, ref, chunk_idx, chunk_text, content_hash, embedding) "
            "VALUES (%s, 'episode', %s, 0, %s, %s, %s::vector)",
            (profile, str(eid), payload["summary"], f"h-{name}", VEC))
    conn.commit()
    return eid


def replica_rows(sql: str, *args):
    con = sqlite3.connect(f"file:{hs.snapshot_path()}?mode=ro", uri=True)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def test_a_foreign_episode_lands_without_a_full_dump_and_reads_in_step(pg, monkeypatch):
    conn, profile = pg
    add_episode(conn, profile, "mine")
    assert hs.refresh()["ok"]
    foreign = add_episode(conn, profile, "foreign")

    def no_dump():
        raise AssertionError("a full dump ran")

    monkeypatch.setattr(hs, "refresh", no_dump)
    out = hs.pull_changes()
    assert out["ok"] is True and out["episodes"] >= 1, out
    assert replica_rows("SELECT summary FROM episodes WHERE id = ?", foreign) == [("scratch foreign",)]
    assert replica_rows("SELECT chunk_idx FROM memory_embeddings WHERE kind = 'episode' AND ref = ?",
                        str(foreign)) == [(0,)]
    side = hs._read_sidecar()
    assert side["pull_dump"] == side["hub_dumped_at"] and side["last_pull_at"]

    lag = hs.measure_lag(conn.cursor())
    for table in ("episodes", "vectors"):
        assert lag["tables"][table]["missing"] == 0, lag["tables"][table]
        assert lag["tables"][table]["lag_seconds"] == 0
    assert hs.snapshot_freshness(None, lag=lag)["state"] == "fresh"


def test_a_missed_patch_is_queued_and_a_pull_retries_it(pg):
    conn, profile = pg
    assert hs.refresh()["ok"]
    eid = add_episode(conn, profile, "late")
    lock = hs._acquire_refresh_lock()
    try:
        out = hs.upsert_episode({"id": eid, "summary": "scratch late"}, [])
    finally:
        hs._release_refresh_lock(lock)
    assert out == {"ok": False, "error": "hub snapshot refresh in progress"}
    assert hs._missed_patch_ids() == {eid}
    assert replica_rows("SELECT id FROM episodes WHERE id = ?", eid) == []

    side = hs._read_sidecar()
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    side.update({"pull_dump": side["hub_dumped_at"], "pull_episodes_after": future, "pull_episodes_after_id": 0,
                 "pull_tombstones_after": future, "pull_vectors_after": future})
    hs._write_meta(side)

    out = hs.pull_changes()
    assert out["ok"] is True and out["missed_retried"] == 1, out
    assert replica_rows("SELECT summary FROM episodes WHERE id = ?", eid) == [("scratch late",)]
    assert hs._missed_patch_ids() == set()


def test_a_full_dump_clears_the_markers_written_before_it(pg):
    conn, profile = pg
    assert hs.refresh()["ok"]
    assert hs._queue_missed_patch(42)
    assert hs._missed_patch_ids() == {42}
    assert hs.refresh()["ok"]
    assert hs._missed_patch_ids() == set()


def test_a_tombstone_reaches_the_replica_through_a_pull(pg):
    conn, profile = pg
    eid = add_episode(conn, profile, "doomed")
    assert hs.refresh()["ok"]
    assert replica_rows("SELECT COUNT(*) FROM memory_embeddings WHERE ref = ?", str(eid)) == [(1,)]
    conn.cursor().execute("UPDATE episodes SET deleted_at = now() WHERE id = %s", (eid,))
    conn.commit()
    out = hs.pull_changes()
    assert out["ok"] is True and out["tombstones"] == 1, out
    assert replica_rows("SELECT deleted_at IS NOT NULL FROM episodes WHERE id = ?", eid) == [(1,)]
    assert replica_rows("SELECT COUNT(*) FROM memory_embeddings WHERE ref = ?", str(eid)) == [(0,)]


def test_a_tombstone_for_a_source_forgotten_elsewhere_still_erases(pg, monkeypatch):
    import contextlib

    from khipu import tombstones

    conn, profile = pg
    eid = add_episode(conn, profile, "forgotten on another mac")
    assert hs.refresh()["ok"]
    conn.cursor().execute("UPDATE episodes SET deleted_at = now() WHERE id = %s", (eid,))
    conn.commit()
    monkeypatch.setattr(tombstones, "write_guard", lambda *_p: contextlib.nullcontext(False))
    out = hs.pull_changes()
    assert out["ok"] is True and out["tombstones"] == 1, out
    assert replica_rows("SELECT deleted_at IS NOT NULL FROM episodes WHERE id = ?", eid) == [(1,)]
    assert replica_rows("SELECT COUNT(*) FROM memory_embeddings WHERE ref = ?", str(eid)) == [(0,)]


def test_more_changes_than_the_limit_write_nothing_and_ask_for_a_dump(pg):
    conn, profile = pg
    assert hs.refresh()["ok"]
    ids = [add_episode(conn, profile, f"bulk{i}") for i in range(3)]
    out = hs.pull_changes(limit=2)
    assert out["ok"] is False and out["needs_dump"] is True
    assert replica_rows(f"SELECT id FROM episodes WHERE id IN ({','.join(map(str, ids))})") == []
    assert "pull_dump" not in hs._read_sidecar()


def test_no_full_dump_on_record_needs_a_dump_without_connecting(monkeypatch):
    monkeypatch.setattr(hs, "try_hub_connect", lambda **_k: pytest.fail("connected"))
    out = hs.pull_changes()
    assert out["ok"] is False and out["needs_dump"] is True
