"""Seventh Astra review (jobs/2026-10-23/astra-prov7/report.md), ported, and
the replica's id wait rule that replaced identity matching (owner decision,
Matt, 2026-10-24; design section 6, "The wait rule").

P1: the identity-matching wait query missed refused identities (a 0027
surrogate receipt, a column-only natural key, a legacy merged payload), so a
rebuild, pull or dump imported a refused contributor's wording. Under the id
rule a refusal no hub forget confirmed keeps the replica fact-less, whatever
identities the hub rows carry, until the owner sends it to the hub.

P2: a PR restatement re-inserted an earlier restater's claim after the fact
lock, taking that episode's row after the fact's (the reverse of forget's
order), and forget's retry was a count of attempts, not the documented time
budget."""
from __future__ import annotations

import hashlib
import json
import threading
import uuid

import pytest

from khipu import capture, forget, provenance, tombstones
from khipu import hub_snapshot as hs
from khipu.capture_identity import episode_key, source_key
from tests.hermetic_pg import Hub, chain_dir, hub, pg_cluster  # noqa: F401
from tests.test_provenance_forget import ALT, CANON, _capture, _payload, _rows, env, pg  # noqa: F401
from tests.test_provenance_replica import _replica, _replica_readers, replica  # noqa: F401
from tests.fact_seed import announce


def _import(method):
    if method == "rebuild":
        return hs.rebuild_facts()
    if method == "dump":
        return hs.refresh()
    return hs.pull_changes(include_ledger=False)


def _held():
    return _replica("SELECT opened_episode, text, latest_text FROM commitments")


def _assert_waiting_without_alt(result):
    assert ALT not in _replica_readers() and ALT not in str(_held()), result
    assert hs.facts_rebuilding()


def _send_and_recover(pg, a, b):  # noqa: F811
    pg.memory.mkdir(exist_ok=True)
    out = forget.send_pending(memory_root=pg.memory)
    assert out["ok"] and out["pending"] == 0, out
    assert _rows(pg, "SELECT deleted_at IS NOT NULL FROM episodes WHERE id = %s", (b,)) == [(True,)]
    assert not hs.facts_rebuilding()
    assert _held() == [(a, CANON, None)]


# ---- P1: refusals the hub never confirmed keep the replica waiting ----------------


@pytest.mark.parametrize("method", ["rebuild", "pull", "dump"])
def test_legacy_hash_only_receipt_refusal_waits_until_sent(pg, replica, method):  # noqa: F811
    replica(pg)
    a = _capture(loop=CANON)
    b = capture.write_pg(dict(_payload(loop=ALT, session_id=None, transcript_range=None)))["episode_id"]
    with pg.connect() as c:
        # 0027's backfilled origin: an episode key and no payload hash.
        c.execute("UPDATE episode_sources SET source_key = %s, payload_sha256 = NULL WHERE episode_id = %s",
                  (episode_key(b), b))
    assert hs.refresh()["ok"]
    stored = _rows(pg, "SELECT raw FROM episodes WHERE id = %s", (b,))[0][0]
    tombstones.publish_payloads([stored])
    with pg.connect() as c, c.cursor() as cur:
        wait = hs.read_refusal_wait(cur)
    result = _import(method)
    _assert_waiting_without_alt(result)
    assert wait["unconfirmed"] and wait["waiting"] == []
    if method == "rebuild":
        assert result["unconfirmed"] == wait["unconfirmed"]
    _send_and_recover(pg, a, b)


@pytest.mark.parametrize("method", ["rebuild", "pull", "dump"])
def test_column_only_identity_refusal_waits_until_sent(pg, replica, method):  # noqa: F811
    replica(pg)
    a = _capture(loop=CANON)
    payload = _payload(loop=ALT, part="2")
    b = capture.write_pg(dict(payload))["episode_id"]
    assert hs.refresh()["ok"]
    column = {k: payload.get(k) for k in ("harness", "session_id", "transcript_range", "window_id")}
    tombstones.publish_identities({source_key(column)}, set())
    _assert_waiting_without_alt(_import(method))
    _send_and_recover(pg, a, b)


@pytest.mark.parametrize("method", ["rebuild", "pull", "dump"])
def test_legacy_merged_payload_refusal_waits_until_sent(pg, replica, method):  # noqa: F811
    replica(pg)
    a = _capture(loop=CANON)
    b = _capture(loop=ALT)
    merged = {"ts": "2026-10-20T10:00:00Z", "summary": "legacy merged source without range"}
    with pg.connect() as c:
        provenance.declare_writer(c.cursor())
        announce(c)
        c.execute("UPDATE episodes SET raw = jsonb_set(raw, '{merged_from}', %s::jsonb) WHERE id = %s",
                  (json.dumps([{"payload": merged}]), b))
    assert hs.refresh()["ok"]
    tombstones.publish_payloads([merged])
    _assert_waiting_without_alt(_import(method))
    _send_and_recover(pg, a, b)


def test_real_legacy_upgrade_refusal_waits_until_sent(env, replica, pg_cluster, tmp_path, monkeypatch):  # noqa: F811
    from khipu import commitments, db

    name = "legacyfacts_" + uuid.uuid4().hex[:12]
    with pg_cluster.admin() as admin:
        admin.execute("CREATE DATABASE " + name + " TEMPLATE template0")
    h = Hub(pg_cluster, name)
    try:
        h.migrate(directory=chain_dir(tmp_path / "oldchain", "0026"))
        db._TABLE_COLUMNS_CACHE.clear()
        provenance._reset_ready_cache()
        monkeypatch.setattr(db, "connect", lambda *a, **kw: h.connect())
        replica(h)
        a = _capture(loop=CANON)
        b = capture.write_pg(_payload(loop=ALT, session_id=None, transcript_range=None))["episode_id"]
        h.migrate(directory=chain_dir(tmp_path / "middlechain", "0044"))
        db._TABLE_COLUMNS_CACHE.clear()
        provenance._reset_ready_cache()
        with h.connect() as c:
            raw = c.execute("SELECT raw FROM episodes WHERE id = %s", (b,)).fetchone()[0]
            commitments.open_from_episode(c.cursor(), raw, b)
        h.migrate()
        db._TABLE_COLUMNS_CACHE.clear()
        provenance._reset_ready_cache()
        with h.connect() as c:
            provenance.backfill(c)
            assert c.execute("SELECT source_key, payload_sha256 FROM episode_sources WHERE episode_id = %s",
                             (b,)).fetchall() == [(episode_key(b), None)]
            assert c.execute("SELECT opened_episode, text, latest_text FROM commitments").fetchall() \
                == [(a, CANON, ALT)]
        assert hs.refresh()["ok"]
        tombstones.publish_payloads([raw])
        result = hs.rebuild_facts()
        _assert_waiting_without_alt(result)
        assert result["ok"] and result["unconfirmed"], result
        h.memory = tmp_path / "memory"
        _send_and_recover(h, a, b)
    finally:
        pg_cluster.drop(h)


def test_real_0027_surrogate_receipt_refusal_is_unconfirmed(pg_cluster, tmp_path):  # noqa: F811
    from khipu import db

    name = "legacy_" + uuid.uuid4().hex[:12]
    with pg_cluster.admin() as admin:
        admin.execute("CREATE DATABASE " + name + " TEMPLATE template0")
    h = Hub(pg_cluster, name)
    try:
        h.migrate(directory=chain_dir(tmp_path, "0026"))
        raw = {"summary": "faithful pre-0027 summary-only synthetic episode"}
        with h.connect() as c:
            eid = c.execute("INSERT INTO episodes (ts, summary, raw) VALUES ('2026-01-01T00:00:00Z', %s, %s::jsonb) "
                            "RETURNING id", (raw["summary"], json.dumps(raw))).fetchone()[0]
            c.commit()
        h.migrate()
        db._TABLE_COLUMNS_CACHE.clear()
        with h.connect() as c:
            expected = hashlib.sha256(("khipu-episode-v1\x1f" + str(eid)).encode()).hexdigest()
            assert c.execute("SELECT source_key, payload_sha256 FROM episode_sources WHERE episode_id = %s",
                             (eid,)).fetchone() == (expected, None)
            tombstones.publish_payloads([raw])
            gate = tombstones.read_gate()
            wait = hs.read_refusal_wait(c.cursor(), gate)
            # The id rule reads no identity: an unconfirmed refusal waits,
            # whatever receipt the hub row carries.
            assert wait["unconfirmed"] == sorted(gate.tags) and wait["waiting"] == []
    finally:
        pg_cluster.drop(h)


# ---- the id rule: confirmations recorded by the hub forget -------------------------


def test_forget_everywhere_confirms_each_refusal_with_the_tombstoned_id(pg, replica):  # noqa: F811
    replica(pg)
    a = _capture(loop=CANON)
    b = _capture(loop=ALT)
    assert hs.refresh()["ok"]
    pg.memory.mkdir(exist_ok=True)
    out = forget.forget_everywhere(b, memory_root=pg.memory, gateway=False)
    assert out["soft_deleted"], out
    gate = tombstones.read_gate()
    assert gate.refusals and not gate.unconfirmed
    assert all(gate.hub_ids(r) == [b] for r in gate.refusals)
    assert not hs.facts_rebuilding()
    assert _held() == [(a, CANON, None)]


def test_a_forget_whose_hub_step_rolled_back_waits_on_its_id_until_sent(pg, replica, monkeypatch):  # noqa: F811
    replica(pg)
    a = _capture(loop=CANON)
    b = _capture(loop=ALT)
    assert hs.refresh()["ok"]
    pg.memory.mkdir(exist_ok=True)
    real = forget._forget_claims

    def fail(cur, eid):
        real(cur, eid)
        raise RuntimeError("hub step failed after the registry was published")

    monkeypatch.setattr(forget, "_forget_claims", fail)
    with pytest.raises(RuntimeError):
        forget.forget_everywhere(b, memory_root=pg.memory, gateway=False)
    monkeypatch.setattr(forget, "_forget_claims", real)
    assert _rows(pg, "SELECT deleted_at IS NULL FROM episodes WHERE id = %s", (b,)) == [(True,)]
    status = forget.pending_forgets()
    assert status["waiting"] == [b] and status["unconfirmed"] == [] and status["count"] == 1
    result = hs.rebuild_facts()
    assert result["waiting"] == [b] and hs.facts_rebuilding(), result
    assert ALT not in _replica_readers()
    _send_and_recover(pg, a, b)


def test_refusals_already_forgotten_on_the_hub_are_confirmed_by_id_at_first_start(pg, replica):  # noqa: F811
    """Rollout 5.4: a registry written before confirmations (none recorded)
    whose forget reached the hub is confirmed by the hub's forget record."""
    replica(pg)
    a = _capture(loop=CANON)
    b = _capture(loop=ALT)
    assert hs.refresh()["ok"]
    pg.memory.mkdir(exist_ok=True)
    assert forget.forget_everywhere(b, memory_root=pg.memory, gateway=False)["soft_deleted"]
    path = tombstones.registry_path()
    old = json.loads(path.read_text())
    old.pop("confirmations")
    path.write_text(json.dumps(old))
    gate = tombstones.read_gate()
    # Episode ids confirm themselves; keys and hashes wait for the record.
    assert gate.unconfirmed and all(r.startswith(("k:", "h:", "l:")) for r in gate.unconfirmed)
    result = hs.rebuild_facts()
    assert result["ok"], result
    left = tombstones.read_gate().unconfirmed
    assert all(r.startswith("l:") for r in left), left
    if not left:
        assert not hs.facts_rebuilding()
        assert _held() == [(a, CANON, None)]


def test_a_backup_refusal_the_hub_never_held_is_sent_and_confirmed_empty(pg, replica):  # noqa: F811
    replica(pg)
    a = _capture(loop=CANON)
    assert hs.refresh()["ok"]
    elsewhere = _payload(loop=ALT, session_id="codex:never-uploaded")
    tombstones.merge_registry({"version": 1, "source_keys": [source_key(elsewhere)], "payload_hashes": [],
                               "legacy_identities": [], "episode_ids": []})
    assert forget.pending_forgets()["count"] == 1
    assert hs.rebuild_facts()["unconfirmed"] and hs.facts_rebuilding()
    pg.memory.mkdir(exist_ok=True)
    out = forget.send_pending(memory_root=pg.memory)
    assert out["ok"] and out["sent"] == [], out
    assert tombstones.read_gate().hub_ids(f"k:{source_key(elsewhere)}") == []
    # The hub now refuses a later upload of that source.
    assert _rows(pg, "SELECT episode_id FROM forgotten_sources WHERE identity = %s",
                 (source_key(elsewhere),)) == [(None,)]
    assert _held() == [(a, CANON, None)]


def test_send_is_an_owner_action(pg, replica, capsys):  # noqa: F811
    from khipu import cli, owner_act, owner_direction

    assert "forget.send" in owner_direction.ACTIONS and owner_act.SPECS["forget.send"] == ((), ())
    assert owner_act.argv("forget.send", {}) == (["episode", "forget-pending", "--send"], [])
    args = cli.build_parser().parse_args(["episode", "forget-pending", "--send"])
    assert cli.cmd_episode(args) == 2
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["code"] == "owner_only"
    args = cli.build_parser().parse_args(["episode", "forget-pending"])
    assert cli.cmd_episode(args) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 0


# ---- P2: a restatement never takes an episode lock after the fact lock --------------


def test_pr_restatements_do_not_deadlock_a_production_forget(pg, monkeypatch):  # noqa: F811
    from khipu import commitments as co

    _capture(loop=CANON)
    words = CANON + " for PR #41"
    victim = _capture(loop=words)
    with pg.connect() as conn:
        provenance.backfill(conn)
    assert _rows(pg, "SELECT latest_text FROM commitments") == [(words,)]
    original_claim, original_forget = co._claim_restatement, forget._forget_claims
    current = {}
    threads, results, errors = [], [], []

    def pause_after_fact_lock(cur, cid, eid, *args, **kw):
        if threading.current_thread().name == "scheduled-capture":
            cur.execute("SET LOCAL deadlock_timeout = '5s'")
            current["fact_locked"].set()
            current["episode_locked"].wait(5)
        return original_claim(cur, cid, eid, *args, **kw)

    def signal_after_episode_lock(cur, eid):
        # The forget runs on provenance.hub_call's worker (design 6.4).
        if threading.current_thread().name != "scheduled-capture":
            current["episode_locked"].set()
        return original_forget(cur, eid)

    monkeypatch.setattr(co, "_claim_restatement", pause_after_fact_lock)
    monkeypatch.setattr(forget, "_forget_claims", signal_after_episode_lock)

    def next_forget_connection(**kw):
        for t in threads:
            t.join(10)
        current.update(fact_locked=threading.Event(), episode_locked=threading.Event())

        def take_fact():
            try:
                results.append(capture.write_pg(_payload(loop=words)))
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))
                current["fact_locked"].set()

        t = threading.Thread(target=take_fact, name="scheduled-capture")
        threads.append(t)
        t.start()
        assert current["fact_locked"].wait(10)
        conn = pg.connect()
        conn.execute("SET deadlock_timeout = '50ms'")
        conn.execute("SET statement_timeout = '10s'")
        return conn

    out = forget._forget_on_hub(next_forget_connection, victim, memory_root=pg.memory)
    for t in threads:
        t.join(10)
        assert not t.is_alive()
    assert out["soft_deleted"] and not out.get("deadlock_retries"), out
    assert len(results) == 1 and not errors, (results, errors)
    assert _rows(pg, "SELECT deleted_at IS NOT NULL FROM episodes WHERE id = %s", (victim,)) == [(True,)]
    assert _rows(pg, "SELECT count(*) FROM fact_gaps") == [(0,)]


def test_forget_retry_is_time_bounded(pg, monkeypatch):  # noqa: F811
    import psycopg

    b = _capture(loop=ALT)
    calls = []
    clock = [0.0]
    monkeypatch.setattr(forget, "FORGET_RETRY_BUDGET_S", 10.0)
    monkeypatch.setattr(forget.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(forget.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + 3.0))

    def deadlock(cur, eid, **kw):
        calls.append(eid)
        raise psycopg.errors.DeadlockDetected("deadlock detected")

    class _Conn:  # no database: the clock is frozen for this test
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return self

        def execute(self, *a, **kw):
            pass

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(forget, "forget_episode", deadlock)
    with pytest.raises(psycopg.errors.DeadlockDetected):
        forget._forget_on_hub(lambda **kw: _Conn(), b, memory_root=pg.memory)
    # Attempts at 0, 3, 6 and 9 s; the next would start at 12 s, past the
    # budget, so it is not started (design 6.4) and the last failure is raised.
    assert len(calls) == 4
