"""Seventh Astra review (jobs/2026-10-23/astra-prov7/report.md), ported, onto
the fact copy of design section 6.2 (phase 7B), which replaced the id wait
rule.

P1: the identity-matching wait query missed refused identities (a 0027
surrogate receipt, a column-only natural key, a legacy merged payload), so a
rebuild, pull or dump imported a refused contributor's wording. Now a
refusal the hub lacks is listed and keeps this Mac's facts hidden until the
owner sends it, and the hub matches it to its carriers by its own index.

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
from tests.replica_copy import mark_ready
from tests.test_provenance_replica import _replica, _replica_readers, replica  # noqa: F401
from tests.fact_seed import announce


def _import(method):
    if method == "copy":
        return hs.copy_facts()
    if method == "dump":
        return hs.refresh()
    return hs.pull_changes(include_ledger=False)


def _held():
    return _replica("SELECT opened_episode, text, latest_text FROM commitments")


def _assert_listed_without_alt(result):
    assert ALT not in _replica_readers(), result
    assert hs.facts_hidden() and forget.unsent_forgets()["count"] >= 1, result


def _send_and_recover(pg, a, b):  # noqa: F811
    out = forget.send_to_hub()
    assert out["ok"] and out["pending"] == 0 and out["facts"].get("copied"), out
    assert _rows(pg, "SELECT deleted_at IS NOT NULL FROM episodes WHERE id = %s", (b,)) == [(True,)]
    assert not hs.facts_hidden()
    assert _held() == [(a, CANON, None)]


# ---- P1: refusals the hub lacks keep the replica's facts hidden until sent -------
#
# Since phase 7B the replica matches nothing: each refusal is matched to its
# carriers on the hub (design 6.1), by every identity the hub's index holds
# for them, here a 0027 surrogate receipt, a column-only natural key and a
# legacy merged payload.


@pytest.mark.parametrize("method", ["copy", "pull", "dump"])
def test_legacy_hash_only_receipt_refusal_waits_until_sent(pg, replica, method):  # noqa: F811
    replica(pg)
    a = _capture(loop=CANON)
    b = capture.write_pg(dict(_payload(loop=ALT, session_id=None, transcript_range=None)))["episode_id"]
    with pg.connect() as c:
        # 0027's backfilled origin: an episode key and no payload hash. History
        # from before 0047, which now refuses moving a receipt's identity.
        c.execute("SET LOCAL session_replication_role = replica")
        c.execute("UPDATE episode_sources SET source_key = %s, payload_sha256 = NULL WHERE episode_id = %s",
                  (episode_key(b), b))
    assert hs.refresh()["ok"]
    stored = _rows(pg, "SELECT raw FROM episodes WHERE id = %s", (b,))[0][0]
    tombstones.publish_payloads([stored])
    result = _import(method)
    _assert_listed_without_alt(result)
    _send_and_recover(pg, a, b)


@pytest.mark.xfail(strict=True, reason=(
    "stopped (phase 7B): the hub's identity index (design 6.1) computes an episode's natural key "
    "with its raw 'part'; a registry key built from the columns alone (part left out) matches no "
    "carrier there, so the send refuses the string but leaves the episode live: the accepted limit "
    "of section 6.5 (a capture held under no identity the hub knows)"))
@pytest.mark.parametrize("method", ["copy", "pull", "dump"])
def test_column_only_identity_refusal_waits_until_sent(pg, replica, method):  # noqa: F811
    replica(pg)
    a = _capture(loop=CANON)
    payload = _payload(loop=ALT, part="2")
    b = capture.write_pg(dict(payload))["episode_id"]
    assert hs.refresh()["ok"]
    column = {k: payload.get(k) for k in ("harness", "session_id", "transcript_range", "window_id")}
    tombstones.publish_identities({source_key(column)}, set())
    _assert_listed_without_alt(_import(method))
    _send_and_recover(pg, a, b)


@pytest.mark.parametrize("method", ["copy", "pull", "dump"])
def test_legacy_merged_payload_refusal_waits_until_sent(env, replica, method):  # noqa: F811
    # A merge from before 0047: the identity index pass computes its
    # payload's identities with the registry's own functions (design 6.1).
    replica(env, ready=False)
    a = _capture(loop=CANON)
    b = _capture(loop=ALT)
    merged = {"ts": "2026-10-20T10:00:00Z", "summary": "legacy merged source without range"}
    with env.connect() as c:
        c.execute("UPDATE episodes SET raw = jsonb_set(raw, '{merged_from}', %s::jsonb) WHERE id = %s",
                  (json.dumps([{"payload": merged}]), b))
    env.to_0047()
    mark_ready(env)
    pg = env
    assert hs.refresh()["ok"]
    tombstones.publish_payloads([merged])
    _assert_listed_without_alt(_import(method))
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
        replica(h, ready=False)
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
        mark_ready(h)
        assert hs.refresh()["ok"]
        tombstones.publish_payloads([raw])
        result = hs.copy_facts()
        _assert_listed_without_alt(result)
        assert not result["ok"] and result.get("unsent"), result
        _send_and_recover(h, a, b)
    finally:
        pg_cluster.drop(h)


def test_real_0027_surrogate_receipt_refusal_is_matched_on_the_hub(pg_cluster, tmp_path):  # noqa: F811
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
            assert provenance.index_identities(c)["state"] == "ready"
            tombstones.publish_payloads([raw])
            forms = sorted({f for t in tombstones.read_gate().tags if (f := tombstones.hub_form(t))})
            # The hub matches the refusal to its carrier by the identities
            # its index computed from the stored row; the replica reads none.
            out = provenance.refuse(c.cursor(), forms)
            c.commit()
            assert out["tombstoned"] == [eid], out
    finally:
        pg_cluster.drop(h)


# ---- a forget's registry is exactly what the hub refuses -----------------------------


def test_forget_everywhere_publishes_only_what_the_hub_refuses(pg, replica):  # noqa: F811
    replica(pg)
    a = _capture(loop=CANON)
    b = _capture(loop=ALT)
    assert hs.refresh()["ok"]
    pg.memory.mkdir(exist_ok=True)
    out = forget.forget_everywhere(b, memory_root=pg.memory, gateway=False)
    assert out["soft_deleted"] and out["snapshot"]["facts"].get("copied"), out
    gate = tombstones.read_gate()
    held = {r[0] for r in _rows(pg, "SELECT identity FROM forgotten_sources")}
    assert gate.tags and {tombstones.hub_form(t) for t in gate.tags} <= held
    assert gate.episode_ids == {b}
    assert forget.unsent_forgets()["count"] == 0 and not hs.facts_hidden()
    assert _held() == [(a, CANON, None)]


def test_a_forget_whose_hub_step_rolled_back_is_listed_until_sent(pg, replica, monkeypatch):  # noqa: F811
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
    result = hs.copy_facts()
    assert not result["ok"] and result.get("unsent"), result
    status = forget.unsent_forgets()
    assert status["strings"] and [i["id"] for i in status["ids"]] == [b], status
    assert hs.facts_hidden() and ALT not in _replica_readers()
    _send_and_recover(pg, a, b)


def test_a_registry_written_with_confirmations_still_reads(pg, replica):  # noqa: F811
    """Rollout: a registry written by the retired wait rule (with its
    ``confirmations``) is read as its refusals alone; a forget that reached
    the hub needs nothing more."""
    replica(pg)
    a = _capture(loop=CANON)
    b = _capture(loop=ALT)
    assert hs.refresh()["ok"]
    pg.memory.mkdir(exist_ok=True)
    assert forget.forget_everywhere(b, memory_root=pg.memory, gateway=False)["soft_deleted"]
    path = tombstones.registry_path()
    old = json.loads(path.read_text())
    old["confirmations"] = {f"e:{b}": [b]}
    path.write_text(json.dumps(old))
    assert tombstones.read_gate().episode_ids == {b}
    result = hs.copy_facts()
    assert result["ok"], result
    assert not hs.facts_hidden() and _held() == [(a, CANON, None)]


def test_a_backup_refusal_the_hub_never_held_is_sent_and_refused_there(pg, replica):  # noqa: F811
    replica(pg)
    a = _capture(loop=CANON)
    assert hs.refresh()["ok"]
    elsewhere = _payload(loop=ALT, session_id="codex:never-uploaded")
    tombstones.merge_registry({"version": 1, "source_keys": [source_key(elsewhere)], "payload_hashes": [],
                               "legacy_identities": [], "episode_ids": []})
    assert not hs.copy_facts()["ok"] and hs.facts_hidden()
    assert forget.unsent_forgets()["count"] == 1
    out = forget.send_to_hub()
    assert out["ok"] and out["sent"] == 1, out
    # The hub now refuses a later upload of that source.
    assert _rows(pg, "SELECT episode_id FROM forgotten_sources WHERE identity = %s",
                 (source_key(elsewhere),)) == [(None,)]
    assert _held() == [(a, CANON, None)]


def test_the_owner_actions_on_the_list_are_owner_only(pg, replica, capsys):  # noqa: F811
    from khipu import cli, owner_act, owner_direction

    for action in ("forget.send", "forget.here", "forget.not_same"):
        assert action in owner_direction.ACTIONS
    assert owner_act.SPECS["forget.send"] == ((), ())
    assert owner_act.argv("forget.send", {}) == (["episode", "forget-pending", "--send"], [])
    assert owner_act.argv("forget.here", {"id": "42"}) == (["episode", "forget-pending", "--forget-here=42"], [])
    assert owner_act.argv("forget.not_same", {"id": "42"}) == (["episode", "forget-pending", "--not-same=42"], [])
    for argv in (["--send"], ["--forget-here", "42"], ["--not-same", "42"]):
        args = cli.build_parser().parse_args(["episode", "forget-pending", *argv])
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
    # Every schema's forget now reads and hands over its set first (7A review 4).
    monkeypatch.setattr(forget, "_forget_read", lambda cur, eid, **kw: {"hub": set()})
    monkeypatch.setattr(forget, "publish_local", lambda *a, **kw: None)
    with pytest.raises(psycopg.errors.DeadlockDetected):
        forget._forget_on_hub(lambda **kw: _Conn(), b, memory_root=pg.memory)
    # Attempts at 0, 3, 6 and 9 s; the next would start at 12 s, past the
    # budget, so it is not started (design 6.4) and the last failure is raised.
    assert len(calls) == 4
