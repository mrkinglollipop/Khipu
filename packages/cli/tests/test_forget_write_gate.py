"""Every episode write goes through the one forgotten-source gate.

Four writers were found, one review round at a time, that could bring a
forgotten source back after ``forget_everywhere`` reported it complete: a
capture merged between forget's source read and its registry publish, the
direct mirror, the full replica refresh, and backup import. Each test below
reproduces one of them. Scope: docs/plans/2026-10-11-capture-identity.md,
"Durable forget: the write gate".
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import psycopg
import pytest

from khipu import capture, db, forget, mirror, outbox, paths, tombstones
from khipu import capture_identity as ci
from khipu import hub_snapshot as hs
from tests import test_forget_ownership as fo
from tests.test_capture import _FakeConn
from tests.test_capture_identity import _SourcesFakeCursor


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith(("KHIPU_", "ALZY_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.setenv("KHIPU_TEST", "1")
    monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: pytest.fail("live PG forbidden"))
    monkeypatch.setattr(outbox, "_queued_origin", lambda: {"origin": None})
    db._TABLE_COLUMNS_CACHE.clear()
    yield
    db._TABLE_COLUMNS_CACHE.clear()


P = {"ts": "2026-10-01T09:00:00Z", "summary": "writer race source",
     "harness": "codex", "session_id": "codex:old", "transcript_range": "0:10",
     "project": "acme/khipu"}
Q = {"ts": "2026-10-01T09:00:30Z", "summary": "writer race source plus later details",
     "harness": "codex", "session_id": "codex:new", "transcript_range": "10:20",
     "project": "acme/khipu"}


def _target(payload):
    return {"id": 1, "ts": payload["ts"], "session_id": payload["session_id"],
            "summary": payload["summary"], "topics": [], "decisions": [],
            "preferences": [], "people": [], "raw": dict(payload),
            "harness": payload["harness"], "repo_root": None,
            "project": payload["project"], "parent_session_id": None,
            "transcript_range": payload["transcript_range"], "tags": []}


def _source(eid, payload):
    ident = ci.source_identity(payload)
    return {"episode_id": eid, "role": "origin", "ts": fo.BASE,
            "summary_md5": fo.md5(payload["summary"]),
            "source_key": ident["source_key"], "payload_sha256": ident["payload_sha256"]}


def _complete_forget(tmp_path, monkeypatch, payload=P):
    root = tmp_path / "memory"
    root.mkdir()
    fo.write_lines(root, [payload])
    world = {"episodes": {1: dict(payload, id=1, ts=fo.BASE, raw=dict(payload), deleted=False)},
             "sources": [fo.ForgetOwnershipTest._source(None, 1, payload)],
             "sources_enabled": True}
    monkeypatch.setattr(db, "connect", lambda *a, **kw: fo._Conn(fo._WorldCur(world)))
    result = forget.forget_everywhere(1, memory_root=root)
    assert result["complete"], result
    assert tombstones.is_forgotten(payload)
    return result


def test_merge_committed_after_forget_reads_sources_is_fenced(tmp_path, monkeypatch):
    """Hole 1: a capture merges into the episode after forget read its
    sources and before the registry is published. Forget must re-read after
    publishing, so the merged source is fenced before forget reports complete."""
    monkeypatch.setenv("KHIPU_OUTBOX", str(tmp_path / "outbox"))
    root = tmp_path / "memory"
    root.mkdir()
    fo.write_lines(root, [P])
    world = {"episodes": {1: dict(_target(P), deleted=False, raw=dict(P))},
             "sources": [_source(1, P)], "sources_enabled": True}
    writer = _SourcesFakeCursor({1: _target(P)})
    writer.sources[ci.source_key(P)] = _source(1, P)
    original_publish = tombstones.publish_forget
    calls = []

    def merge_then_publish(stale_sources, stale_payloads, stale_identities):
        if not calls:
            # forget_episode has read raw and the source rows. A capture now
            # merges and commits before the registry names its source.
            assert capture._merge_into_episode(writer, 1, dict(Q), matched_via="jaccard", score=.9)
            world["episodes"][1]["raw"] = writer.episodes[1]["raw"]
            world["episodes"][1]["summary"] = writer.episodes[1]["summary"]
            merged = writer.sources[ci.source_key(Q)]
            world["sources"].append({"source_key": ci.source_key(Q), **merged})
            assert all(row.get("source_key") != ci.source_key(Q) for row in stale_sources)
        calls.append(stale_sources)
        original_publish(stale_sources, stale_payloads, stale_identities)

    db._TABLE_COLUMNS_CACHE.clear()
    with mock.patch("khipu.db.connect", side_effect=lambda *a, **k: fo._Conn(fo._WorldCur(world))), \
            mock.patch("khipu.hub_snapshot.forget_episode_in_snapshot", return_value={"ok": True}), \
            mock.patch("khipu.tombstones.publish_forget", side_effect=merge_then_publish):
        out = forget.forget_everywhere(1, memory_root=root)
    assert out["complete"], out
    assert tombstones.is_forgotten(Q), "forget completed without fencing the late merged source"
    child = tmp_path / "legacy_writer.py"
    child.write_text("import json,sys\nfrom pathlib import Path\np=json.load(sys.stdin)\n"
                     "with Path(" + repr(str(root / "episodes.jsonl")) + ").open('a') as f:"
                     "f.write(json.dumps(p)+'\\n')\n")
    with mock.patch.object(capture, "_capture_v2", return_value=child):
        assert capture.run_capture_v2(dict(Q), suppress_mirror=True) == 0
    assert Q["summary"] not in (root / "episodes.jsonl").read_text(), \
        "legacy writer appended a merged source after forget completed"


def test_direct_mirror_refuses_a_forgotten_natural_source(tmp_path, monkeypatch):
    """Hole 2: a changed summary with the same natural source must not insert
    a new live hub row after the tombstone."""
    tombstones.publish_payloads([P])
    cur = _SourcesFakeCursor({1: {**_target(P), "deleted_at": "2026-10-01T09:01:00Z"}})
    cur.sources[ci.source_key(P)] = _source(1, P)
    changed = {**P, "summary": "writer race source amended after forget"}
    db._TABLE_COLUMNS_CACHE.clear()
    graph = mock.MagicMock(return_value={})
    with mock.patch("khipu.db.connect", return_value=_FakeConn(cur)), \
            mock.patch("khipu.mirror._ensure_path"), \
            mock.patch("khipu.capture._stamp_origin", return_value=None), \
            mock.patch("khipu.mirror.persist_capture_graph", graph):
        assert mirror.mirror_episode(changed)
    assert len(cur.episodes) == 1, "mirror inserted a new live row for a tombstoned natural source"
    assert ci.episode_key(2) not in cur.sources
    graph.assert_not_called()


def test_write_pg_refuses_a_forgotten_natural_source(tmp_path, monkeypatch):
    """The hub capture path uses the same gate as the mirror."""
    tombstones.publish_payloads([P])
    cur = _SourcesFakeCursor({1: {**_target(P), "deleted_at": "2026-10-01T09:01:00Z"}})
    cur.sources[ci.source_key(P)] = _source(1, P)
    changed = {**P, "summary": "writer race source amended after forget"}
    db._TABLE_COLUMNS_CACHE.clear()
    with mock.patch("khipu.db.connect", return_value=_FakeConn(cur)), \
            mock.patch("khipu.capture._stamp_origin", return_value=None), \
            mock.patch("khipu.topic_graph.persist_capture_graph", return_value={}), \
            mock.patch("khipu.config.path_setting", return_value=None):
        out = capture.write_pg(dict(changed))
    assert out.get("forgotten_source") is True, out
    assert out["episode_inserted"] is False
    assert len(cur.episodes) == 1


def test_hub_forgotten_sources_table_refuses_a_write_this_mac_never_saw(tmp_path):
    """A forget made on another Mac reaches this one only through the hub's
    forgotten_sources table (migration 0030); the gate reads it."""
    class Cur:
        def __init__(self):
            self.calls = []
            self._one = None
            self._all = []

        def execute(self, sql, params=None):
            s = " ".join(sql.split())
            self.calls.append((s, params))
            if "information_schema.columns" in s:
                self._all = [("identity",)] if params[0] == "forgotten_sources" else []
            elif s.startswith("SELECT 1 FROM forgotten_sources"):
                self._one = (1,) if ci.source_key(P) in params[0] else None

        def fetchone(self):
            return self._one

        def fetchall(self):
            return self._all

    db._TABLE_COLUMNS_CACHE.clear()
    assert not tombstones.is_forgotten(P)
    assert tombstones.hub_source_forgotten(Cur(), {**P, "summary": "other words"})
    db._TABLE_COLUMNS_CACHE.clear()
    assert not tombstones.hub_source_forgotten(Cur(), Q)


def test_backup_import_does_not_restore_forgotten_outbox(tmp_path, monkeypatch):
    """Hole 4a: a backup taken before the forget holds the queued job."""
    queued = outbox.enqueue(dict(P))
    archive = paths.backup_local(dest=tmp_path / "before-forget.zip")["archive"]
    _complete_forget(tmp_path, monkeypatch)
    assert not queued.exists()
    paths.import_local(source=Path(archive))
    assert not queued.exists(), "backup import restored a forgotten outbox payload"


def test_directory_import_does_not_restore_forgotten_outbox(tmp_path, monkeypatch):
    queued = outbox.enqueue(dict(P))
    copy = tmp_path / "copy"
    copy.mkdir()
    (copy / "outbox").mkdir()
    (copy / "outbox" / queued.name).write_bytes(queued.read_bytes())
    _complete_forget(tmp_path, monkeypatch)
    out = paths.import_local(source=copy)
    assert not queued.exists(), "directory import restored a forgotten outbox payload"
    assert out["skipped_forgotten"] == 1


def test_backup_import_preserves_forgotten_registry(tmp_path, monkeypatch):
    """Hole 4b: an older registry in the backup must merge, never replace."""
    old = {"ts": "2026-09-01T09:00:00Z", "summary": "unrelated old tombstone"}
    tombstones.publish_payloads([old])
    archive = paths.backup_local(dest=tmp_path / "before-forget.zip")["archive"]
    _complete_forget(tmp_path, monkeypatch)
    paths.import_local(source=Path(archive))
    assert tombstones.is_forgotten(P), "backup import erased a completed forget from the registry"
    assert tombstones.is_forgotten(old)


def test_backup_import_adds_the_archives_forgotten_sources(tmp_path, monkeypatch):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    other = tmp_path / "other-state"
    monkeypatch.setenv("KHIPU_DATA_DIR", str(other))
    tombstones.publish_payloads([Q])
    (archive_dir / "forgotten_sources.json").write_bytes(
        (other / "forgotten_sources.json").read_bytes())
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "state"))
    tombstones.publish_payloads([P])
    paths.import_local(source=archive_dir)
    assert tombstones.is_forgotten(P) and tombstones.is_forgotten(Q)


def test_backup_import_refuses_a_corrupt_registry(tmp_path, monkeypatch):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / "forgotten_sources.json").write_text("{not json", encoding="utf-8")
    tombstones.publish_payloads([P])
    with pytest.raises(tombstones.RegistryError):
        paths.import_local(source=archive_dir)
    assert tombstones.is_forgotten(P)


class _DumpCursor:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchone(self):
        return (datetime.now(timezone.utc),)

    def fetchall(self):
        cols = [s.strip() for s in self.sql[7:self.sql.index(" FROM")].split(",")]
        row = dict(P, id=2, raw=dict(P), deleted_at=None)
        return [tuple(row.get(c) for c in cols)]


class _DumpConnection:
    def cursor(self):
        return _DumpCursor()


@contextmanager
def _fake_dump(**kw):
    yield _DumpConnection()


def _stub_dump(monkeypatch):
    monkeypatch.setattr(hs, "try_hub_connect", _fake_dump)
    monkeypatch.setattr(hs, "_pg_columns", lambda cur, table: {
        "id", "ts", "summary", "session_id", "harness", "transcript_range", "raw", "deleted_at",
    } if table == "episodes" else set())
    for name in ["topics", "topic_revisions", "nodes", "edges", "profiles",
                 "memory_embeddings", "decisions", "deliverables", "ledger"]:
        monkeypatch.setattr(hs, "_insert_" + name, lambda *a: 0)


def test_full_refresh_does_not_restore_forgotten_source_under_new_id(tmp_path, monkeypatch):
    """Hole 3: the hub holds a copy of the forgotten source under another id."""
    _complete_forget(tmp_path, monkeypatch)
    _stub_dump(monkeypatch)
    assert hs.refresh()["ok"]
    with hs.open_snapshot() as con:
        rows = con.execute("SELECT id, summary FROM episodes").fetchall()
    assert rows == [], f"full refresh restored forgotten source: {rows}"


def test_full_refresh_fails_closed_on_a_corrupt_registry(tmp_path, monkeypatch):
    _stub_dump(monkeypatch)
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "forgotten_sources.json").write_text("{not json", encoding="utf-8")
    out = hs.refresh()
    assert not out["ok"]
    assert not hs.snapshot_path().exists()


def test_imported_snapshot_is_scrubbed_of_forgotten_sources(tmp_path, monkeypatch):
    _stub_dump(monkeypatch)
    assert hs.refresh()["ok"]
    archive = paths.backup_local(dest=tmp_path / "before-forget.zip")["archive"]
    hs.snapshot_path().unlink()
    tombstones.publish_payloads([P])
    paths.import_local(source=Path(archive))
    con = sqlite3.connect(str(hs.snapshot_path()))
    try:
        rows = con.execute("SELECT id FROM episodes WHERE deleted_at IS NULL").fetchall()
    finally:
        con.close()
    assert rows == [], "backup import restored a forgotten source into the replica"
