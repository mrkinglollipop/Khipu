# ruff: noqa: F811 - pytest fixtures imported from test_provenance_forget and replica_copy
"""Fact provenance, phase 7B review (``jobs/2026-10-23/astra-7b/report.md``,
findings R1 to R8): each test is the review's reproduction, ported, or the
case its required correction names. Every database is a hermetic one
(tests/hermetic_pg.py) and every replica a temp file.
"""
from __future__ import annotations

import sqlite3
import time

import psycopg

from khipu import capture, db, forget, hub_snapshot as hs, owner_act, provenance, tombstones
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.replica_copy import readers, replica, rows, shown, stamp  # noqa: F401
from tests.test_provenance_copy import _decision, _locks_free, _offline
from tests.test_provenance_forget import _capture, _payload, env, pg  # noqa: F401


# ---- R1: a forget refuses only what the selected episode carries ----------------------


def test_part_forget_does_not_refuse_distinct_partless_source(pg, replica):
    replica(pg)
    common = {"session_id": "codex:distinct-parts", "transcript_range": "0:10", "window_id": "window1"}
    ea = capture.write_pg(_payload(decision="PART TWO WORDING", part="2", **common))["episode_id"]
    eb = capture.write_pg(_payload(decision="PARTLESS WORDING", **common))["episode_id"]
    assert ea != eb
    with pg.connect() as conn:
        ids = provenance.stored_identities(conn.cursor(), [ea, eb])
        assert not ids[ea] & ids[eb]
        carried = {r[0] for r in conn.execute(
            "SELECT identity FROM episode_identities WHERE episode_id = %s", (ea,))}
    pg.memory.mkdir(exist_ok=True)
    assert hs.refresh()["facts"].get("copied")
    out = forget.forget_everywhere(ea, memory_root=pg.memory, gateway=False)
    assert out["soft_deleted"] and out["carriers"] == [], out
    with pg.connect() as conn:
        deleted = conn.execute("SELECT deleted_at FROM episodes WHERE id = %s", (eb,)).fetchone()[0]
        refused = {r[0] for r in conn.execute("SELECT identity FROM forgotten_sources")}
    assert deleted is None, "a forget of part 2 tombstoned the independent partless capture"
    assert not refused & ids[eb]
    # Every hub refusal the forget made is an identity the hub records for E,
    # and every string this Mac's registry gained is one of them.
    assert refused and refused <= carried, sorted(refused - carried)
    assert out["snapshot"]["facts"].get("copied") and forget.unsent_forgets()["count"] == 0
    assert "PARTLESS WORDING" in readers() and "PART TWO WORDING" not in readers()


# ---- R2: a backup's file holds no fact bytes ------------------------------------------


def test_export_has_no_deleted_fact_bytes(pg, replica, tmp_path):
    replica(pg)
    _decision(pg, "ordinary prefix longer than twenty characters FACTONLY_" + "private owner decision xyz " * 100)
    with pg.connect() as conn:
        conn.execute("UPDATE episodes SET summary = 'ordinary anchor'")
    assert hs.refresh()["facts"].get("copied")
    assert b"FACTONLY_" in hs.snapshot_path().read_bytes()
    dest = tmp_path / "export.sqlite"
    hs.export_replica_copy(hs.snapshot_path(), dest)
    with sqlite3.connect(dest) as con:
        assert con.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM episodes").fetchone()[0] > 0
    assert b"FACTONLY_" not in dest.read_bytes(), "deleted fact bytes remain in the exported file"


# ---- R3: a configured hub change hides the old hub's facts at once --------------------


def test_endpoint_switch_hides_before_any_copy(pg, replica, monkeypatch):
    replica(pg)
    _decision(pg, "SECRET FROM OLD HUB")
    assert hs.refresh()["facts"].get("copied")
    monkeypatch.setattr(db, "resolve_dsn", lambda: pg.dsn + "_replacement")
    _offline(monkeypatch)
    assert "SECRET FROM OLD HUB" not in readers()
    assert hs.facts_hidden()


def test_writing_the_endpoint_invalidates_the_stamp(pg, replica):
    from khipu import join

    replica(pg)
    _decision(pg, "OLD ENDPOINT FACT")
    assert hs.refresh()["facts"].get("copied")
    before = int(rows("SELECT value FROM snapshot_meta WHERE key = ?", (hs._FACT_REVISION_KEY,))[0][0])
    join._write_dsn_file(pg.dsn)
    assert stamp() is None and "OLD ENDPOINT FACT" not in readers()
    after = int(rows("SELECT value FROM snapshot_meta WHERE key = ?", (hs._FACT_REVISION_KEY,))[0][0])
    assert after == before + 1
    assert hs.copy_facts().get("copied") and "OLD ENDPOINT FACT" in readers()


# ---- R4: no complete stamp, no facts ----------------------------------------------------


def test_legacy_digest_without_7b_stamp_is_not_certified(pg, replica):
    replica(pg)
    _decision(pg, "UNCERTIFIED LEGACY FACT")
    assert hs.refresh()["facts"].get("copied")
    with sqlite3.connect(hs.snapshot_path()) as con:
        hs._meta_put(con, hs._FACT_STAMP_KEY, None)
    assert stamp() is None
    assert "UNCERTIFIED LEGACY FACT" not in readers() and hs.facts_hidden()


def test_a_stamp_missing_a_field_is_not_certified(pg, replica):
    replica(pg)
    _decision(pg, "HALF STAMPED FACT")
    assert hs.refresh()["facts"].get("copied")
    with sqlite3.connect(hs.snapshot_path()) as con:
        hs._meta_put(con, hs._FACT_STAMP_KEY, '{"incarnation": "x"}')
    assert "HALF STAMPED FACT" not in readers() and hs.facts_hidden()


def test_a_new_schema_shows_no_fact_row(pg, replica, tmp_path):
    replica(pg)
    path = tmp_path / "fresh.sqlite"
    with sqlite3.connect(path) as con:
        hs._create_schema(con)
        con.execute("INSERT INTO decisions (id, project, text) VALUES (1, 'p', 'UNCOPIED FACT')")
    with sqlite3.connect(path) as con:
        assert hs._meta_get(con, hs._FACT_DIGEST_KEY) is None
        sql = "SELECT text FROM decisions WHERE 1 = 1" + hs._fact_epoch_sql(con)
        assert con.execute(sql).fetchall() == []
    assert hs.facts_hidden(path)


# ---- R5: a failed forget lists what it published ---------------------------------------


def test_failed_forget_immediately_lists_published_strings(pg, replica, monkeypatch):
    replica(pg)
    victim = _capture(decision="FORGET FAILED AT HUB")
    pg.memory.mkdir(exist_ok=True)
    assert hs.refresh()["facts"].get("copied")
    assert forget.unsent_forgets()["count"] == 0

    def refused(*_a, **_kw):
        raise provenance.NotDone("injected failure after local publication")

    monkeypatch.setattr(provenance, "refuse", refused)
    out = forget.forget_everywhere(victim, memory_root=pg.memory, gateway=False)
    tags = set(tombstones.read_gate().tags)
    listed = forget.unsent_forgets()
    assert not out["ok"] and tags and hs.facts_hidden()
    assert listed["count"] > 0 and set(listed["strings"]) <= tags, listed
    # A copy that cannot run (offline) keeps the list.
    _offline(monkeypatch)
    assert not hs.copy_facts()["ok"]
    assert forget.unsent_forgets()["strings"] == listed["strings"]
    # Nothing was sent: the hub has none of them.
    with pg.connect() as conn:
        assert conn.execute("SELECT count(*) FROM forgotten_sources").fetchone()[0] == 0


def test_the_owner_send_resolves_a_failed_forget(pg, replica, monkeypatch):
    replica(pg)
    victim = _capture(decision="FORGET THEN SEND")
    pg.memory.mkdir(exist_ok=True)
    assert hs.refresh()["facts"].get("copied")
    real = provenance.refuse
    monkeypatch.setattr(provenance, "refuse", lambda *a, **kw: (_ for _ in ()).throw(
        provenance.NotDone("injected")))
    assert not forget.forget_everywhere(victim, memory_root=pg.memory, gateway=False)["ok"]
    assert forget.unsent_forgets()["count"] > 0
    monkeypatch.setattr(provenance, "refuse", real)
    out = forget.send_to_hub()
    assert out["ok"] and out["pending"] == 0, out
    assert not hs.facts_hidden() and "FORGET THEN SEND" not in readers()


# ---- R6: one operation deadline covers the copy after it -------------------------------


def test_owner_send_uses_one_operation_deadline(pg, replica, monkeypatch):
    replica(pg)
    _decision(pg, "ONE DEADLINE")
    assert hs.refresh()["facts"].get("copied")
    tombstones.publish_identities({"ab" * 32}, set())
    assert hs.copy_facts().get("unsent") == 1
    monkeypatch.setattr(forget, "FORGET_RETRY_BUDGET_S", 3)
    real_refuse = provenance.refuse

    def slow_refuse(cur, *a, **kw):
        cur.execute("SELECT pg_sleep(2.4)")
        return real_refuse(cur, *a, **kw)

    monkeypatch.setattr(provenance, "refuse", slow_refuse)
    real_read = hs._copy_read

    def slow_read(tx, local):
        time.sleep(4)
        return real_read(tx, local)

    monkeypatch.setattr(hs, "_copy_read", slow_read)
    started = time.monotonic()
    out = forget.send_to_hub()
    elapsed = time.monotonic() - started
    assert elapsed < 3.7, (elapsed, out)
    assert out["sent"] == 1 and not out["facts"]["ok"], out
    with pg.connect() as conn:
        assert conn.execute("SELECT count(*) FROM forgotten_sources WHERE identity = %s",
                            ("ab" * 32,)).fetchone()[0] == 1
    assert _locks_free()


def test_forget_copy_uses_the_forget_deadline(pg, replica, monkeypatch):
    replica(pg)
    victim = _capture(decision="FORGET ONE DEADLINE")
    pg.memory.mkdir(exist_ok=True)
    assert hs.refresh()["facts"].get("copied")
    monkeypatch.setattr(forget, "FORGET_RETRY_BUDGET_S", 8)
    real_refuse = provenance.refuse

    def slow_refuse(cur, *a, **kw):
        cur.execute("SELECT pg_sleep(5)")
        return real_refuse(cur, *a, **kw)

    monkeypatch.setattr(provenance, "refuse", slow_refuse)
    real_read = hs._copy_read

    def slow_read(tx, local):
        time.sleep(12)
        return real_read(tx, local)

    monkeypatch.setattr(hs, "_copy_read", slow_read)
    started = time.monotonic()
    out = forget.forget_everywhere(victim, memory_root=pg.memory, gateway=False)
    elapsed = time.monotonic() - started
    assert elapsed < 8.7, (elapsed, out)
    assert out["soft_deleted"] and not out["snapshot"]["facts"]["ok"], out
    assert hs.facts_hidden()


# ---- R7: an unchanged pull is cheap ------------------------------------------------------


def test_an_unchanged_pull_at_50ms_is_under_half_a_second(pg, replica, pg_cluster, monkeypatch):
    from tests.test_provenance_copy_bench import DelayProxy

    replica(pg)
    _decision(pg, "LATENCY FACT")
    assert hs.refresh()["facts"].get("copied")
    proxy = DelayProxy(pg_cluster.port)
    remote = pg.dsn.replace(f":{pg_cluster.port}/", f":{proxy.port}/")
    monkeypatch.setattr(hs, "try_hub_connect", lambda statement_timeout_s=None, **kw: psycopg.Connection.connect(
        db._with_statement_timeout(remote, statement_timeout_s) if statement_timeout_s else remote, **kw))
    monkeypatch.setattr(hs, "FRESHNESS_OVERLAP_S", 0)
    loads: list = []
    real = hs._held_commitment_vectors
    monkeypatch.setattr(hs, "_held_commitment_vectors", lambda con: loads.append(1) or real(con))
    try:
        assert hs.pull_changes(include_ledger=False)["ok"]
        times = []
        for _ in range(3):
            started = time.monotonic()
            out = hs.pull_changes(include_ledger=False)
            times.append(time.monotonic() - started)
            assert out["ok"] and out["facts"].get("skipped"), out
        assert not loads, "a skipped copy read the held vectors"
        assert max(times) < 0.5, times
    finally:
        proxy.close()


# ---- R8: the owner list through khipu_owner_act -----------------------------------------


def test_owner_act_reads_the_owner_list(pg, replica, monkeypatch):
    replica(pg)
    _decision(pg, "LISTED FACT")
    assert hs.refresh()["facts"].get("copied")
    tombstones.publish_identities({"cd" * 32}, set())
    assert hs.copy_facts().get("unsent") == 1
    sent: list = []
    monkeypatch.setattr(forget, "send_to_hub", lambda **kw: sent.append(1))
    ok, out = owner_act.act("forget.list", {})
    assert ok and out["strings"] == ["k:" + "cd" * 32] and out["count"] == 1, out
    assert out["facts_hidden"] and not sent
    with pg.connect() as conn:
        assert conn.execute("SELECT count(*) FROM forgotten_sources").fetchone()[0] == 0
    ok, out = owner_act.act("forget.list", {"id": 1})
    assert not ok and out["code"] == "bad_args"
