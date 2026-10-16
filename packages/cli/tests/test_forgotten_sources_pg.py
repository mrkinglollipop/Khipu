"""Migration 0030 (forgotten_sources and its triggers) and the hub side of the
durable forget, against a real PostgreSQL that tests/hermetic_pg.py starts for
the session and removes afterwards. Never the live hub. Every row is synthetic.

Scope: docs/plans/2026-10-11-capture-identity.md ("Durable forget: one memory
lock"). The cases here are the interleavings review rounds found one by one:
a forget committing between the backfill and the triggers, a normal writer
and the migration taking the two tables in opposite orders, and a forget
publishing after a writer's own check but before its episode row.
"""
from __future__ import annotations

import json
import threading
from unittest import mock

import psycopg
import pytest

from khipu import capture, db, mirror, tombstones
from khipu import capture_identity as ci
from tests.hermetic_pg import MIGRATIONS, chain_dir, hub, migration_sql, pg_cluster  # noqa: F401

VERSION = "0030_forgotten_sources"
P = {"ts": "2026-10-15T09:00:00Z", "summary": "scratch forgotten source",
     "harness": "codex", "session_id": "codex:scratch", "transcript_range": "0:10"}
# No session or range, so no natural key: the payload hash is its only identity.
HASH_ONLY = {"ts": "2026-10-15T12:00:00Z", "summary": "hash only sentinel"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.setenv("KHIPU_TEST", "1")
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "state"))
    db._TABLE_COLUMNS_CACHE.clear()
    yield
    db._TABLE_COLUMNS_CACHE.clear()


def insert_episode(cur, payload: dict, *, deleted: bool = False, raw: dict | None = None) -> int:
    cur.execute(
        "INSERT INTO episodes (ts, session_id, summary, raw, harness, transcript_range, deleted_at) "
        "VALUES (%s::timestamptz, %s, %s, %s::jsonb, %s, %s, CASE WHEN %s THEN now() END) RETURNING id",
        (payload["ts"], payload.get("session_id"), payload["summary"],
         json.dumps(raw if raw is not None else payload), payload.get("harness"),
         payload.get("transcript_range"), deleted))
    return cur.fetchone()[0]


def insert_source(cur, episode_id: int, payload: dict, role: str = "origin") -> None:
    s = ci.source_identity(payload)
    cur.execute("INSERT INTO episode_sources (source_key, episode_id, role, payload_sha256) "
                "VALUES (%s, %s, %s, %s)", (s["source_key"], episode_id, role, s["payload_sha256"]))


def identities(cur, episode_id: int | None = None) -> set[str]:
    if episode_id is None:
        cur.execute("SELECT identity FROM forgotten_sources")
    else:
        cur.execute("SELECT identity FROM forgotten_sources WHERE episode_id = %s", (episode_id,))
    return {r[0] for r in cur.fetchall()}


def forget(cur, payload: dict, episode_id: int) -> None:
    """What forget.py does on the hub: tombstone, then publish the identities."""
    cur.execute("UPDATE episodes SET deleted_at = now() WHERE id = %s", (episode_id,))
    tombstones.publish_hub_forget(cur, episode_id, [ci.source_identity(payload)])


def run_in_thread(fn):
    """Start ``fn``; returns (thread, errors)."""
    errors: list[BaseException] = []

    def target():
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 - reported by the test
            errors.append(exc)

    t = threading.Thread(target=target)
    t.start()
    return t, errors


@pytest.fixture
def at_0030(hub):  # noqa: F811
    conn = hub.connect()
    result = hub.migrate(conn=conn)
    assert result["ran"] == [VERSION], result
    db._TABLE_COLUMNS_CACHE.clear()
    return hub


# ---------------------------------------------------------------------------
# the migration chain
# ---------------------------------------------------------------------------

def test_the_full_chain_applies_and_0030_is_safe_to_run_twice(hub, tmp_path):  # noqa: F811
    conn = hub.connect()
    first = hub.migrate(directory=chain_dir(tmp_path, "0030"), conn=conn)
    assert first["ran"] == [VERSION] and first["pending"] == []
    assert hub.migrate(conn=conn)["ran"] == []        # recorded, so the runner skips it
    cur = conn.cursor()
    cur.execute(migration_sql())                        # the SQL itself, applied again
    cur.execute(migration_sql())
    conn.commit()
    cur.execute("SELECT count(*) FROM schema_migrations WHERE version = %s", (VERSION,))
    assert cur.fetchone()[0] == 1
    cur.execute("SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'khipu_episode%forgotten%' "
                "AND NOT tgisinternal")
    assert cur.fetchone()[0] == 3
    cur.execute("SELECT to_regclass('public.forgotten_sources') IS NOT NULL")
    assert cur.fetchone()[0]


# ---------------------------------------------------------------------------
# the four triggers' behaviour (ported from the old scratch-database test)
# ---------------------------------------------------------------------------

def test_an_insert_of_a_forgotten_natural_key_is_refused(at_0030):
    conn = at_0030.connect()
    cur = conn.cursor()
    forget(cur, P, insert_episode(cur, P))
    cur.execute("SAVEPOINT refused")
    with pytest.raises(psycopg.errors.CheckViolation) as caught:
        insert_episode(cur, {**P, "summary": "the same span, synthesized again"})
    assert "khipu: forgotten source" in str(caught.value)
    cur.execute("ROLLBACK TO SAVEPOINT refused")


def test_un_forgetting_an_episode_is_refused(at_0030):
    conn = at_0030.connect()
    cur = conn.cursor()
    eid = insert_episode(cur, P)
    forget(cur, P, eid)
    cur.execute("SAVEPOINT refused")
    with pytest.raises(psycopg.errors.CheckViolation):
        cur.execute("UPDATE episodes SET deleted_at = NULL WHERE id = %s", (eid,))
    cur.execute("ROLLBACK TO SAVEPOINT refused")
    # Metadata on a forgotten row still updates (hygiene, origin stamping).
    cur.execute("UPDATE episodes SET project = 'acme/scratch' WHERE id = %s", (eid,))
    assert cur.rowcount == 1


def test_an_unrelated_insert_passes(at_0030):
    cur = at_0030.connect().cursor()
    forget(cur, P, insert_episode(cur, P))
    other = {**P, "session_id": "codex:other", "summary": "unrelated"}
    assert isinstance(insert_episode(cur, other), int)


def test_the_hub_gate_reads_the_table(at_0030):
    cur = at_0030.connect().cursor()
    forget(cur, P, insert_episode(cur, P))
    db._TABLE_COLUMNS_CACHE.clear()
    assert tombstones.hub_source_forgotten(cur, {**P, "summary": "x"})
    assert not tombstones.hub_source_forgotten(cur, {**P, "session_id": "codex:other"})


# ---------------------------------------------------------------------------
# the backfill
# ---------------------------------------------------------------------------

def test_a_forget_committed_before_the_migration_is_backfilled(hub):  # noqa: F811
    conn = hub.connect()
    cur = conn.cursor()
    eid = insert_episode(cur, P)
    insert_source(cur, eid, P)
    cur.execute("UPDATE episodes SET deleted_at = now() WHERE id = %s", (eid,))
    conn.commit()
    hub.migrate(conn=conn)
    s = ci.source_identity(P)
    assert {s["source_key"], s["payload_sha256"]} <= identities(conn.cursor(), eid)


def test_backfill_covers_historic_forgotten_episode_without_source_row(hub):  # noqa: F811
    conn = hub.connect()
    cur = conn.cursor()
    eid = insert_episode(cur, {**P, "session_id": "codex:old"}, deleted=True)
    conn.commit()
    hub.migrate(conn=conn)
    natural = ci.source_key({**P, "session_id": "codex:old"})
    assert natural in identities(conn.cursor(), eid)
    # The SQL formula and the Python one are the same function.
    cur = conn.cursor()
    cur.execute("SELECT khipu_natural_source_key('codex', 'codex:old', '0:10', NULL, NULL)")
    assert cur.fetchone()[0] == natural


def test_backfill_covers_the_merge_receipt_of_a_forgotten_episode(hub):  # noqa: F811
    conn = hub.connect()
    cur = conn.cursor()
    merged = {**P, "session_id": "codex:merged", "summary": "merged-in capture"}
    receipt = {"source_id": ci.payload_sha256(merged), "session_id": merged["session_id"],
               "ts": merged["ts"], "payload": merged}
    eid = insert_episode(cur, P, deleted=True, raw={**P, "merged_from": [receipt]})
    conn.commit()
    hub.migrate(conn=conn)
    found = identities(conn.cursor(), eid)
    assert ci.payload_sha256(merged) in found
    assert ci.source_key(merged) in found
    assert ci.source_key(P) in found        # the target's own natural key as well


def test_no_forget_can_commit_between_the_backfill_scan_and_the_triggers(hub):  # noqa: F811
    seed = hub.connect()
    cur = seed.cursor()
    eid = insert_episode(cur, P)
    insert_source(cur, eid, P)
    seed.commit()
    rows = [ci.source_identity(P)]
    m = hub.connect()
    mcur = m.cursor()
    mcur.execute(migration_sql())          # the whole migration, not yet committed

    def forgetter():
        conn = hub.connect()
        c = conn.cursor()
        c.execute("UPDATE episodes SET deleted_at = now() WHERE id = %s", (eid,))
        db._TABLE_COLUMNS_CACHE.clear()
        tombstones.publish_hub_forget(c, eid, rows)
        conn.commit()

    thread, errors = run_in_thread(forgetter)
    thread.join(0.5)
    assert thread.is_alive(), "a forget committed while the migration held its locks"
    m.commit()
    thread.join(10)
    assert not thread.is_alive() and not errors, errors
    found = identities(seed.cursor(), eid)
    assert {rows[0]["source_key"], rows[0]["payload_sha256"]} <= found


def test_migration_does_not_deadlock_normal_two_table_writer(hub):  # noqa: F811
    writer = hub.connect()
    wcur = writer.cursor()
    eid = insert_episode(wcur, P)              # episodes row first, uncommitted

    migration = hub.connect()

    def migrate_it():
        migration.cursor().execute(migration_sql())
        migration.commit()

    thread, errors = run_in_thread(migrate_it)
    thread.join(0.5)
    assert thread.is_alive(), "the migration did not wait for the open writer"
    insert_source(wcur, eid, P)                 # then its source row: the other table
    writer.commit()
    thread.join(10)
    assert not thread.is_alive(), "migration and writer deadlocked"
    assert not errors, [type(e).__name__ for e in errors]
    cur = hub.connect().cursor()
    cur.execute("SELECT count(*) FROM schema_migrations WHERE version = %s", (VERSION,))
    assert cur.fetchone()[0] == 1


@pytest.mark.parametrize("reapply", [False, True])
def test_migration_does_not_deadlock_a_merge_holding_its_row_lock(hub, reapply):  # noqa: F811
    """The merge writer locks its target row (SELECT ... FOR UPDATE) before
    it updates it. A migration that took a weaker table lock and then needed
    a stronger one for DROP TRIGGER waited on that row lock while the merge
    waited on the migration's table lock. Re-applied, the triggers exist, so
    DROP TRIGGER really drops one."""
    if reapply:
        assert hub.migrate(conn=hub.connect())["ran"] == [VERSION]
    seed = hub.connect()
    target = insert_episode(seed.cursor(), {**P, "session_id": "codex:target"})
    seed.commit()
    writer = hub.connect()
    wcur = writer.cursor()
    wcur.execute("SELECT id FROM episodes WHERE id = %s FOR UPDATE", (target,))
    migration = hub.connect()

    def migrate_it():
        migration.cursor().execute(migration_sql())
        migration.commit()

    thread, errors = run_in_thread(migrate_it)
    thread.join(0.5)
    merged = {**P, "session_id": "codex:merged", "summary": "merged while migrating"}
    with _not_forgotten_by_the_python_check():
        assert capture._merge_into_episode(wcur, target, merged, matched_via="test",
                                           score=0.9) is True
    writer.commit()
    thread.join(15)
    assert not thread.is_alive(), "migration and merge writer deadlocked"
    assert not errors, [f"{type(e).__name__}: {e}" for e in errors]


def test_migration_runs_while_a_merge_writer_loops_without_deadlock(hub):  # noqa: F811
    """A second connection runs the normal merge writer in a loop while the
    migration is applied, then re-applied, several times. Neither side may
    see a deadlock, and every merge either lands or waits its turn."""
    import time

    seed = hub.connect()
    target = insert_episode(seed.cursor(), {**P, "session_id": "codex:target"})
    seed.commit()
    stop = threading.Event()
    merges = [0]

    def writer_loop():
        conn = hub.connect()
        cur = conn.cursor()
        i = 0
        while not stop.is_set():
            i += 1
            merged = {**P, "session_id": f"codex:merged-{i}", "summary": f"merged {i}"}
            with _not_forgotten_by_the_python_check():
                assert capture._merge_into_episode(cur, target, merged, matched_via="loop",
                                                   score=0.9) is True
            conn.commit()
            merges[0] += 1

    thread, errors = run_in_thread(writer_loop)
    migration = hub.connect()
    deadline = time.monotonic() + 1.0
    while merges[0] < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        first = hub.migrate(conn=migration)
        assert first["ran"] == [VERSION], first
        cur = migration.cursor()
        for _ in range(8):
            cur.execute(migration_sql())
            migration.commit()
    finally:
        stop.set()
        thread.join(15)
    assert not thread.is_alive(), "the merge writer never finished"
    assert not errors, [f"{type(e).__name__}: {e}" for e in errors]
    assert merges[0] > 3, merges


# ---------------------------------------------------------------------------
# the writers, with the real code against the real triggers
# ---------------------------------------------------------------------------

def _not_forgotten_by_the_python_check():
    """Simulate a forget that publishes after the writer's own check."""
    return mock.patch("khipu.tombstones.hub_source_forgotten", return_value=False)


def test_a_normal_upsert_inserts_the_episode_and_its_origin_source_row(at_0030):
    conn = at_0030.connect()
    cur = conn.cursor()
    out: dict = {}
    assert mirror._upsert_episode(cur, dict(P), out=out) is True
    conn.commit()
    assert "forgotten_source" not in out
    cur.execute("SELECT s.source_key, s.role FROM episode_sources s WHERE s.episode_id = %s",
                (out["episode_id"],))
    assert cur.fetchall() == [(ci.source_key(P), "origin")]


def test_hash_only_forget_committed_first_refuses_the_episode_row(at_0030):
    conn = at_0030.connect()
    cur = conn.cursor()
    cur.execute("INSERT INTO forgotten_sources (identity) VALUES (%s)", (ci.payload_sha256(HASH_ONLY),))
    conn.commit()
    out: dict = {}
    with _not_forgotten_by_the_python_check():
        assert mirror._upsert_episode(cur, dict(HASH_ONLY), out=out) is False
    assert out["forgotten_source"] is True and "episode_id" not in out
    cur.execute("SELECT 1")                      # the transaction is still usable
    assert cur.fetchone() == (1,)
    conn.commit()
    cur.execute("SELECT count(*) FROM episodes WHERE summary = %s AND deleted_at IS NULL",
                (HASH_ONLY["summary"],))
    assert cur.fetchone()[0] == 0


def test_hash_only_writer_first_holds_the_identity_until_it_commits(at_0030):
    writer = at_0030.connect()
    wcur = writer.cursor()
    identity = ci.payload_sha256(HASH_ONLY)
    out: dict = {}
    with _not_forgotten_by_the_python_check():
        assert mirror._upsert_episode(wcur, dict(HASH_ONLY), out=out) is True   # uncommitted

    def forgetter():
        conn = at_0030.connect()
        c = conn.cursor()
        c.execute("SELECT pg_advisory_xact_lock(hashtextextended('khipu-source:' || %s, 0))",
                  (identity,))
        c.execute("INSERT INTO forgotten_sources (identity, episode_id) VALUES (%s, %s) "
                  "ON CONFLICT DO NOTHING", (identity, out["episode_id"]))
        c.execute("UPDATE episodes SET deleted_at = now() WHERE id = %s", (out["episode_id"],))
        conn.commit()

    thread, errors = run_in_thread(forgetter)
    thread.join(0.5)
    assert thread.is_alive(), "a forget took the identity while the writer held it"
    writer.commit()
    thread.join(10)
    assert not thread.is_alive() and not errors, errors
    cur = at_0030.connect().cursor()
    cur.execute("SELECT count(*) FROM episodes WHERE id = %s AND deleted_at IS NULL", (out["episode_id"],))
    assert cur.fetchone()[0] == 0           # the forget covered the episode the writer committed


def test_a_merge_of_a_forgotten_source_returns_false_and_leaves_the_target(at_0030):
    conn = at_0030.connect()
    cur = conn.cursor()
    target = insert_episode(cur, {**P, "session_id": "codex:target"})
    merged = {**P, "session_id": "codex:merged", "summary": "merged-in capture"}
    cur.execute("INSERT INTO forgotten_sources (identity) VALUES (%s)", (ci.payload_sha256(merged),))
    conn.commit()
    cur.execute("SELECT summary, raw, topics, decisions FROM episodes WHERE id = %s", (target,))
    before = cur.fetchone()
    with _not_forgotten_by_the_python_check():
        assert capture._merge_into_episode(cur, target, dict(merged), matched_via="test",
                                           score=0.9) is False
    cur.execute("SELECT 1")                      # transaction still usable
    conn.commit()
    cur.execute("SELECT summary, raw, topics, decisions FROM episodes WHERE id = %s", (target,))
    assert cur.fetchone() == before
    cur.execute("SELECT count(*) FROM episode_sources WHERE episode_id = %s AND role = 'merged'",
                (target,))
    assert cur.fetchone()[0] == 0
    # Control: the same merge goes through once the source is not forgotten.
    cur.execute("DELETE FROM forgotten_sources WHERE identity = %s", (ci.payload_sha256(merged),))
    conn.commit()
    with _not_forgotten_by_the_python_check():
        assert capture._merge_into_episode(cur, target, dict(merged), matched_via="test",
                                           score=0.9) is True
