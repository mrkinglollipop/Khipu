"""Round 10: a native job archived to empty/, and a backup that restores a
forgotten episode id under an older payload.

Each earlier round found one more place a copy of a source can sit. Forget
now scans one declared inventory of every local capture store
(khipu/capture_stores.py), so an archive folder is covered by being listed,
and test_capture_store_inventory.py fails when a writer appears outside it.
A backup import scrubs by every identity the registry holds, which now
includes forgotten episode ids: a payload-keyed source whose raw changed
after the backup no longer matches by hash, but its id still does.
"""
from __future__ import annotations

import json
import os
import sqlite3

import psycopg
import pytest

from khipu import capture_identity as ci, db, forget, hub_snapshot as hs, outbox, paths
from khipu import session_capture as sc, tombstones
from tests import test_forget_ownership as fo
from tests.test_forget_write_gate import P, _complete_forget


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith(("KHIPU_", "ALZY_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "native"))
    monkeypatch.setenv("KHIPU_OUTBOX", str(tmp_path / "outbox"))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.setenv("KHIPU_TEST", "1")
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: pytest.fail("live PG forbidden"))
    monkeypatch.setattr(outbox, "_queued_origin", lambda: {"origin": None})
    db._TABLE_COLUMNS_CACHE.clear()


def _empty_job():
    return {"ts": P["ts"], "harness": "codex", "session_id": "old",
            "transcript_range": P["transcript_range"],
            "transcript": "PRIVATE SOURCE TEXT THAT MUST BE ERASED", "disposition": "empty"}


def test_job_archived_to_empty_is_removed_by_a_completed_forget(tmp_path, monkeypatch):
    job = _empty_job()
    queue = sc.queue_dir()
    queue.mkdir(parents=True)
    claimed = queue / f"same-source.claimed.{os.getpid()}"
    claimed.write_text(json.dumps(job), encoding="utf-8")
    sc._archive_empty(claimed, job)
    archived = sc.khipu_home() / "empty" / "same-source.json"
    assert archived.exists()

    result = _complete_forget(tmp_path, monkeypatch, payload=P)

    assert result["complete"]
    assert not archived.exists(), "completed forget left the source transcript in empty/"
    assert {"place": "native empty archive", "path": str(archived), "state": "removed"} \
        in result["locations"]


def test_unreadable_empty_archive_keeps_the_forget_open(tmp_path, monkeypatch):
    archive = sc.khipu_home() / "empty"
    archive.mkdir(parents=True)
    (archive / "torn.json").write_text("{not json", encoding="utf-8")
    root = tmp_path / "memory"
    root.mkdir()
    fo.write_lines(root, [P])
    world = {"episodes": {1: dict(P, id=1, ts=fo.BASE, raw=dict(P), deleted=False)},
             "sources": [fo.ForgetOwnershipTest._source(None, 1, P)], "sources_enabled": True}
    monkeypatch.setattr(db, "connect", lambda *a, **kw: fo._Conn(fo._WorldCur(world)))

    result = forget.forget_everywhere(1, memory_root=root)

    assert not result["complete"]
    assert "native_jobs" in result["partial"]
    assert (archive / "torn.json").exists()


def _replica(path, raw, summary=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        hs._create_schema(con)
        con.execute("INSERT INTO episodes (id, ts, summary, session_id, raw) VALUES (1, ?, ?, ?, ?)",
                    (raw["ts"], summary or raw["summary"], raw.get("session_id"), json.dumps(raw)))


def test_backup_cannot_restore_a_forgotten_episode_id_under_older_raw(tmp_path, monkeypatch):
    # write_pg snapshots the source before topics, tags and scope are added,
    # and a later merge changes raw again, so the backup's raw hashes to
    # nothing the registry holds. Only the episode id ties it to the forget.
    original = {"ts": "2026-10-01T09:00:00Z", "summary": "original private summary",
                "session_id": "codex:original", "harness": "codex"}
    stored_before_merge = {**original, "topics": [], "tags": [], "scope": None}
    incoming = {"ts": "2026-10-01T09:01:00Z", "summary": "later details",
                "session_id": "codex:later", "harness": "codex", "transcript_range": "0:10"}
    current_raw = {**stored_before_merge, "verbatim": {}, "merged_from": [{
        "source_id": ci.payload_sha256(incoming), "payload": incoming,
        "session_id": incoming["session_id"], "ts": incoming["ts"],
        "matched_via": "jaccard", "score": .9}]}
    world = {"episodes": {1: {"id": 1, "ts": fo.BASE,
                              "summary": original["summary"] + "\n" + incoming["summary"],
                              "session_id": original["session_id"], "raw": current_raw,
                              "deleted": False}},
             "sources": [fo.ForgetOwnershipTest._source(None, 1, original),
                         {**fo.ForgetOwnershipTest._source(None, 1, incoming), "role": "merged"}],
             "sources_enabled": True}
    archive = tmp_path / "backup"
    _replica(archive / "hub_snapshot.sqlite", stored_before_merge)
    _replica(hs.snapshot_path(), current_raw, world["episodes"][1]["summary"])
    root = tmp_path / "memory"
    root.mkdir()
    fo.write_lines(root, [original, incoming])
    monkeypatch.setattr(db, "connect", lambda *a, **k: fo._Conn(fo._WorldCur(world)))

    result = forget.forget_everywhere(1, memory_root=root)
    assert result["complete"], result
    assert 1 in tombstones.forgotten_episode_ids()

    imported = paths.import_local(source=archive)
    assert imported["ok"], imported
    with sqlite3.connect(hs.snapshot_path()) as con:
        live = con.execute("SELECT id FROM episodes WHERE id = 1 AND deleted_at IS NULL").fetchall()
    assert live == [], f"backup restored the forgotten episode id: {live}"


def test_registry_merge_keeps_forgotten_episode_ids_and_reads_old_files(tmp_path):
    path = tombstones.registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "source_keys": ["k"], "payload_hashes": [],
                                "legacy_identities": []}), encoding="utf-8")
    assert tombstones.forgotten_episode_ids() == set()
    tombstones.merge_registry({"version": 1, "source_keys": [], "payload_hashes": [],
                               "legacy_identities": [], "episode_ids": [7, 9]})
    assert tombstones.forgotten_episode_ids() == {7, 9}
    with pytest.raises(tombstones.RegistryError):
        tombstones.merge_registry({"version": 1, "source_keys": [], "payload_hashes": [],
                                   "legacy_identities": [], "episode_ids": ["7"]})
