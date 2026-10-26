"""Replica findings from Astra's 2026-10-26 memory-pipeline audit
(jobs/2026-10-26/astra-memory-pipeline/report.md, findings 2 and 4): the
commitments, decisions and deliverables of an episode the replica refused
never reach a replica reader, and a stalled refresh cannot hold forget's
memory lock without bound. Every row is synthetic; the hub is a fake."""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from khipu import hub_snapshot as hs, offline_reads, tombstones
from tests.test_replica_pull_integrity import replica  # noqa: F401

SOURCE = "FORGOTTEN synthetic source"


def _refused_episode(replica):  # noqa: F811
    tombstones.publish_payloads([{"summary": SOURCE}])
    replica.episodes.append({"id": 7, "summary": SOURCE, "ingested_at": datetime.now(timezone.utc),
                             "deleted_at": None, "raw": {"summary": SOURCE}})


def test_pull_does_not_publish_the_commitment_of_a_refused_episode(replica):  # noqa: F811
    _refused_episode(replica)
    replica.commitments.append({"id": 70, "text": "FORGOTTEN synthetic commitment",
                                "project": "scratch", "status": "open", "opened_episode": 7})
    result = hs.pull_changes()
    assert result["ok"] and result["forgotten_skipped"] == 1, result
    assert hs.episode_detail_snapshot(7) is None
    served = offline_reads.owed(None, "open", 20)
    assert not any(r["id"] == 70 for r in served["results"]), served["results"]
    # Nothing of it is in the replica file either, for a reader that skips the fence.
    import sqlite3

    con = sqlite3.connect(hs.snapshot_path())
    try:
        assert con.execute("SELECT count(*) FROM commitments WHERE opened_episode = 7").fetchone()[0] == 0
    finally:
        con.close()


def test_a_stalled_dump_never_holds_forgets_memory_lock_and_ends_at_its_deadline(replica, monkeypatch):  # noqa: F811
    from khipu import memory_lock, provenance

    monkeypatch.setattr(hs, "REFRESH_DEADLINE_S", 1.0)
    entered, release = threading.Event(), threading.Event()
    abandoned: list = []
    outcomes: list = []

    def stall(cur, con):
        entered.set()
        release.wait(30)
        raise RuntimeError("synthetic read on a socket that was shut down")

    def abandon(conn):
        # What shutting the socket down does to a blocked read: it fails now.
        abandoned.append(conn)
        release.set()

    monkeypatch.setattr(hs, "_insert_episodes", stall)
    monkeypatch.setattr(provenance, "_abandon", abandon)

    def refresh():
        try:
            outcomes.append(hs.refresh())
        except Exception as exc:  # noqa: BLE001
            outcomes.append(exc)

    dump = threading.Thread(target=refresh)
    dump.start()
    try:
        assert entered.wait(5)
        # The hub read holds no memory lock: a forget takes it at once.
        with memory_lock.exclusive(wait=0.5):
            pass
        dump.join(10)
        assert not dump.is_alive()
        assert abandoned == [replica], abandoned
        assert len(outcomes) == 1 and isinstance(outcomes[0], hs.DumpDeadline), outcomes
        lock = hs._acquire_refresh_lock()
        assert lock is not None, "the refresh lock outlived the dump's deadline"
        hs._release_refresh_lock(lock)
        assert hs.episode_detail_snapshot(1)["summary"] == "old summary"
    finally:
        release.set()
        dump.join(10)


def test_a_dump_within_its_deadline_abandons_nothing(replica, monkeypatch):  # noqa: F811
    from khipu import provenance

    abandoned: list = []
    monkeypatch.setattr(provenance, "_abandon", abandoned.append)
    monkeypatch.setattr(hs, "copy_facts", lambda **kw: {"ok": True, "skipped": True})
    # The fake hub answers pulls, not a dump's whole-table reads.
    for name in ("_insert_episodes", "_insert_topics", "_insert_topic_revisions", "_insert_nodes",
                 "_insert_edges", "_insert_profiles", "_insert_memory_embeddings",
                 "_insert_project_aliases", "_insert_ledger"):
        monkeypatch.setattr(hs, name, lambda cur, con: 0)
    assert hs.refresh()["ok"]
    assert abandoned == []


def test_snapshot_connections_bound_every_statement(monkeypatch):
    import psycopg

    from khipu import db, hub_reach

    captured = {}
    sentinel = object()

    def connect(*args, **kwargs):
        captured.update(kwargs, conninfo=args[0])
        return sentinel

    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setattr(db, "resolve_dsn", lambda: "host=synthetic")
    monkeypatch.setattr(db, "conninfo_with_local_root_cert", lambda value: value)
    monkeypatch.setattr(hub_reach, "note_reachable", lambda: None)
    assert hs.try_hub_connect(connect_timeout=30) is sentinel
    assert captured["connect_timeout"] == 30
    assert "statement_timeout=" in captured["conninfo"], captured


def test_an_older_topic_read_never_replaces_a_newer_one(replica):  # noqa: F811
    """Re-check round 1 (prior finding 9): an embed that read its topic before
    a newer copy landed keeps the newer body and its vectors."""
    vec = [0.1] * 4

    def vector(body, at):
        return {"profile": "p", "kind": "topic", "ref": "probe", "chunk_idx": 0, "chunk_text": body,
                "content_hash": body, "embedding": vec, "built_at": at}

    assert hs.upsert_embeddings([vector("NEW", "2026-10-26T10:00:00Z")],
                                topic_rows=[{"slug": "probe", "body": "NEW"}],
                                replace_refs=[("p", "topic", "probe")], read_at=200)["ok"]
    assert hs.upsert_embeddings([vector("OLD", "2026-10-25T10:00:00Z")],
                                topic_rows=[{"slug": "probe", "body": "OLD"}],
                                replace_refs=[("p", "topic", "probe")], read_at=100)["ok"]
    import sqlite3

    with sqlite3.connect(hs.snapshot_path()) as con:
        assert con.execute("SELECT body FROM topics WHERE slug='probe'").fetchone() == ("NEW",)
        assert con.execute("SELECT chunk_text FROM memory_embeddings WHERE kind='topic' AND ref='probe'"
                           ).fetchall() == [("NEW",)]


def test_an_older_updated_at_never_replaces_a_newer_topic(replica):  # noqa: F811
    assert hs.upsert_embeddings([], topic_rows=[{"slug": "probe", "body": "NEW",
                                                 "updated_at": "2026-10-26T10:00:00Z"}], read_at=100)["ok"]
    assert hs.upsert_embeddings([], topic_rows=[{"slug": "probe", "body": "OLD",
                                                 "updated_at": "2026-10-25T10:00:00Z"}], read_at=200)["ok"]
    assert hs.topic_detail_snapshot("probe")["body"] == "NEW"
