"""The per-Mac memory lock: forget exclusive for its whole run, every local
writer shared for its whole write.

Seven review rounds each found one more interleaving of a forget with a
writer or with another forget. These tests pin the model that removes the
class: two forgets serialize, a writer waits for a whole forget, a claimed
native job cannot outlive a completed forget, and the episode row and its
source row are one write the hub can refuse together. Scope:
docs/plans/2026-10-11-capture-identity.md ("Durable forget: one memory lock").
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from unittest import mock

import psycopg
import pytest

from khipu import capture, db, forget, memory_lock, mirror, outbox, paths, session_capture, tombstones
from khipu import capture_identity as ci
from khipu import hub_snapshot as hs
from tests import test_forget_ownership as fo
from tests import test_forget_write_gate as fw


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith(("KHIPU_", "ALZY_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.setenv("KHIPU_TEST", "1")
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KHIPU_OUTBOX", str(tmp_path / "outbox"))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: pytest.fail("live DB forbidden"))
    monkeypatch.setattr(outbox, "_queued_origin", lambda: {"origin": None})
    db._TABLE_COLUMNS_CACHE.clear()
    yield
    db._TABLE_COLUMNS_CACHE.clear()


def _two_episode_world(a, b):
    return {"episodes": {i: dict(p, id=i, ts=fo.BASE, raw=dict(p), deleted=False)
                         for i, p in [(1, a), (2, b)]},
            "sources": [fo.ForgetOwnershipTest._source(None, i, p) for i, p in [(1, a), (2, b)]],
            "sources_enabled": True}


def test_two_forgets_cannot_rewrite_a_completed_forgets_source(tmp_path, monkeypatch):
    """Forget B pauses inside its legacy log rewrite. Forget A must wait for
    the whole of B, not rewrite the log alongside it: before the lock, A
    completed, then B's rewrite (from its stale read) restored A's line."""
    a = {"ts": "2026-10-01T09:00:00Z", "summary": "forgotten A sentinel",
         "harness": "codex", "session_id": "codex:probe-A", "transcript_range": "0:10"}
    b = {**a, "summary": "forgotten B sentinel", "session_id": "codex:probe-B"}
    root = tmp_path / "memory"
    root.mkdir()
    fo.write_lines(root, [a, b])
    world = _two_episode_world(a, b)
    monkeypatch.setattr(db, "connect", lambda *x, **k: fo._Conn(fo._WorldCur(world)))
    monkeypatch.setattr(hs, "snapshot_path", lambda: tmp_path / "replica/hub_snapshot.sqlite")
    paused, resume = threading.Event(), threading.Event()
    original_read = forget._read_lines

    def read_lines(path):
        lines = original_read(path)
        if (threading.current_thread().name == "forget-B" and path == root / "episodes.jsonl"
                and not paused.is_set()):
            paused.set()
            assert resume.wait(10), "test scheduler timed out"
        return lines

    monkeypatch.setattr(forget, "_read_lines", read_lines)
    results, errors = {}, []

    def run(name, eid):
        try:
            results[name] = forget.forget_everywhere(eid, memory_root=root)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    thread_b = threading.Thread(target=run, args=("B", 2), name="forget-B")
    thread_b.start()
    assert paused.wait(10), "B did not reach its legacy rewrite"
    thread_a = threading.Thread(target=run, args=("A", 1), name="forget-A")
    thread_a.start()
    try:
        thread_a.join(0.5)
        assert thread_a.is_alive(), "forget A ran while forget B was mid-rewrite"
    finally:
        resume.set()
        thread_b.join(10)
        thread_a.join(10)
    assert not thread_a.is_alive() and not thread_b.is_alive()
    assert not errors, errors
    assert results["A"]["complete"] and results["B"]["complete"], results
    text = (root / "episodes.jsonl").read_text()
    assert a["summary"] not in text and b["summary"] not in text
    assert tombstones.is_forgotten(a) and tombstones.is_forgotten(b)


def _blocks_during_forget(write) -> None:
    """``write`` must not finish while a forget holds the lock exclusive."""
    held, release, done = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def forget_holder():
        with memory_lock.exclusive():
            held.set()
            release.wait(10)

    def writer():
        try:
            write()
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
        done.set()

    holder = threading.Thread(target=forget_holder)
    holder.start()
    assert held.wait(5)
    worker = threading.Thread(target=writer)
    worker.start()
    try:
        assert not done.wait(0.3), "a local writer ran inside a forget"
    finally:
        release.set()
        holder.join(10)
        worker.join(10)
    assert done.is_set() and not errors, errors


P = {"ts": "2026-10-01T09:00:00Z", "summary": "memory lock writer",
     "harness": "codex", "session_id": "codex:lock", "transcript_range": "0:10"}


class _Connected(Exception):
    pass


def _expect_connect(call):
    """The hub writers take the lock before they connect: reaching connect
    is the write starting."""
    def connect(*a, **k):
        raise _Connected

    with mock.patch("khipu.db.connect", side_effect=connect), mock.patch("khipu.mirror._ensure_path"):
        with pytest.raises(_Connected):
            call()


@pytest.mark.parametrize("writer", ["outbox_enqueue", "native_checkpoint", "backup_import",
                                    "replica_scrub", "write_pg", "mirror"])
def test_every_kind_of_local_writer_waits_for_a_whole_forget(tmp_path, monkeypatch, writer):
    if writer == "replica_scrub":
        # The scrub returns early when there is no replica to scrub.
        fw._stub_dump(monkeypatch)
        assert hs.refresh()["ok"]
    claimed_job = None
    if writer == "native_checkpoint":
        job = {"ts": P["ts"], "harness": "codex", "session_id": "lock", "transcript_range": "0:10"}
        claimed_job = (session_capture._claim(session_capture.enqueue(dict(job))), job)
    copy = tmp_path / "copy"
    copy.mkdir()
    (copy / "note.txt").write_text("x")
    writes = {
        "outbox_enqueue": lambda: outbox.enqueue(dict(P)),
        "native_checkpoint": lambda: session_capture._save_claimed_job(*claimed_job),
        "backup_import": lambda: paths.import_local(source=copy),
        "replica_scrub": hs.scrub_forgotten_snapshot,
        "write_pg": lambda: _expect_connect(lambda: capture.write_pg(dict(P))),
        "mirror": lambda: _expect_connect(lambda: mirror.mirror_episode(dict(P))),
    }
    _blocks_during_forget(writes[writer])


def test_a_forget_cannot_start_inside_a_local_write():
    with memory_lock.shared():
        with pytest.raises(memory_lock.MemoryLockError):
            with memory_lock.exclusive():
                pass
        out = forget.forget_everywhere(1, memory_root=None)
    assert out["ok"] is False and out["complete"] is False
    assert "forget not started" in out["error"]


def test_the_lock_is_reentrant_for_a_writer_and_for_a_forget():
    with memory_lock.shared(), memory_lock.shared():
        pass
    with memory_lock.exclusive(), memory_lock.exclusive(), memory_lock.shared():
        pass


def test_a_claimed_native_job_cannot_outlive_a_completed_forget(tmp_path):
    """A drainer claimed the job and checkpointed the extracted episode. A
    complete forget must remove that copy, and the drainer's next checkpoint
    must not write it back."""
    payload = {"ts": "2026-10-01T09:00:00Z", "summary": "claimed source sentinel",
               "harness": "codex", "session_id": "codex:A", "transcript_range": "0:10"}
    source = {"episode_id": 1, "role": "origin", "ts": fo.BASE,
              "summary_md5": fo.md5(payload["summary"]),
              "source_key": ci.source_key(payload), "payload_sha256": ci.payload_sha256(payload)}
    world = {"episodes": {1: {"id": 1, "ts": fo.BASE, "summary": payload["summary"],
                              "session_id": payload["session_id"], "raw": dict(payload),
                              "deleted": False}},
             "sources": [source], "lines": [payload], "sources_enabled": True}
    fo.write_lines(tmp_path, [payload])
    job = {"ts": payload["ts"], "harness": "codex", "session_id": "A",
           "transcript_range": "0:10", "transcript": "source transcript"}
    with mock.patch.object(hs, "snapshot_path", return_value=tmp_path / "replica/hub_snapshot.sqlite"):
        claimed = session_capture._claim(session_capture.enqueue(job))
        assert claimed is not None
        job["extracted_payload"] = dict(payload)
        session_capture._save_claimed_job(claimed, job)
        result = fo.run_forget(world, 1, tmp_path)
        job["checkpoint_after_forget"] = True
        with pytest.raises(session_capture.JobForgotten):
            session_capture._save_claimed_job(claimed, job)
    assert result["complete"], result
    assert result["native_jobs"]["removed"] == 1
    assert not claimed.exists(), "a claimed copy of the forgotten source survived the forget"
    assert not any(p.name.endswith((".json", ".tmp")) or ".claimed." in p.name
                   for p in session_capture.queue_dir().iterdir())


def test_a_native_job_from_another_source_is_kept(tmp_path):
    payload = {"ts": "2026-10-01T09:00:00Z", "summary": "forgotten one",
               "harness": "codex", "session_id": "codex:A", "transcript_range": "0:10"}
    world = {"episodes": {1: dict(payload, id=1, ts=fo.BASE, raw=dict(payload), deleted=False)},
             "sources": [fo.ForgetOwnershipTest._source(None, 1, payload)], "sources_enabled": True}
    fo.write_lines(tmp_path, [payload])
    other = session_capture.enqueue({"ts": payload["ts"], "harness": "codex", "session_id": "A",
                                     "transcript_range": "10:20", "transcript": "later span"})
    with mock.patch.object(hs, "snapshot_path", return_value=tmp_path / "replica/hub_snapshot.sqlite"):
        result = fo.run_forget(world, 1, tmp_path)
    assert result["complete"], result
    assert other.exists()


def test_an_unreadable_native_job_keeps_the_forget_open(tmp_path):
    payload = {"ts": "2026-10-01T09:00:00Z", "summary": "forgotten one",
               "harness": "codex", "session_id": "codex:A", "transcript_range": "0:10"}
    world = {"episodes": {1: dict(payload, id=1, ts=fo.BASE, raw=dict(payload), deleted=False)},
             "sources": [fo.ForgetOwnershipTest._source(None, 1, payload)], "sources_enabled": True}
    fo.write_lines(tmp_path, [payload])
    session_capture.queue_dir().mkdir(parents=True)
    (session_capture.queue_dir() / "torn.claimed.123").write_text("{torn")
    with mock.patch.object(hs, "snapshot_path", return_value=tmp_path / "replica/hub_snapshot.sqlite"):
        result = fo.run_forget(world, 1, tmp_path)
    assert not result["complete"]
    assert "native_jobs" in result["partial"]


class _RaceCursor:
    """The hub refuses the source row of a payload with no natural key: the
    forget published after the writer's own check and its episode INSERT."""
    rowcount = 0

    def __init__(self):
        self.inserted = False
        self.rolled_back = False

    def execute(self, sql, params=()):
        if "INSERT INTO episodes" in sql:
            self.inserted = True
            self.rowcount = 1
        elif "INSERT INTO episode_sources" in sql:
            raise RuntimeError("khipu: forgotten source payload-hash")
        elif sql.startswith("ROLLBACK TO SAVEPOINT khipu_episode_write"):
            self.inserted = False
            self.rolled_back = True

    def fetchone(self):
        return (777,) if self.inserted else None


def test_a_refused_source_row_rolls_back_its_episode_row():
    cur, out = _RaceCursor(), {}
    with mock.patch("khipu.db.has_columns", return_value=True), \
            mock.patch("khipu.db.has_table", return_value=True), \
            mock.patch("khipu.tombstones.hub_source_forgotten", return_value=False), \
            mock.patch("khipu.mirror._log"):
        inserted = mirror._upsert_episode(cur, {"ts": "2026-10-15T12:00:00Z",
                                                "summary": "forget-race sentinel"}, out=out)
    assert inserted is False, "a source refused after forget must abort the episode insert"
    assert out.get("forgotten_source") is True and "episode_id" not in out
    assert cur.rolled_back and not cur.inserted


def _replica_rows(path: Path) -> list:
    con = sqlite3.connect(str(path))
    try:
        return con.execute("SELECT id, summary FROM episodes WHERE deleted_at IS NULL").fetchall()
    finally:
        con.close()


def test_an_imported_replica_is_never_readable_before_its_scrub(tmp_path, monkeypatch):
    """The backup's replica is scrubbed in a staging copy; the replica path
    never holds the forgotten row, not even between the copy and the scrub."""
    fw._stub_dump(monkeypatch)
    assert hs.refresh()["ok"]
    archive = Path(paths.backup_local(dest=tmp_path / "before-forget.zip")["archive"])
    hs.snapshot_path().unlink()
    fw._complete_forget(tmp_path, monkeypatch)
    real_scrub = hs._scrub_forgotten
    exposed = []

    def inspect_then_scrub(con, gate):
        if hs.snapshot_path().exists():
            exposed.extend(_replica_rows(hs.snapshot_path()))
        return real_scrub(con, gate)

    monkeypatch.setattr(hs, "_scrub_forgotten", inspect_then_scrub)
    result = paths.import_local(source=archive)
    assert result["snapshot_scrub"]["ok"], result
    assert exposed == [], f"forgotten episode was readable before import scrub: {exposed}"
    assert _replica_rows(hs.snapshot_path()) == []
    assert not [p for p in hs.snapshot_path().parent.iterdir()
                if p.name.startswith(".hub_snapshot.") and p.name.endswith(".sqlite")]


def test_an_import_whose_scrub_cannot_run_leaves_the_current_replica(tmp_path, monkeypatch):
    fw._stub_dump(monkeypatch)
    assert hs.refresh()["ok"]
    archive = Path(paths.backup_local(dest=tmp_path / "backup.zip")["archive"])
    before = hs.snapshot_path().read_bytes()
    monkeypatch.setattr(hs, "_acquire_refresh_lock", lambda: None)
    result = paths.import_local(source=archive)
    assert result["snapshot_scrub"]["ok"] is False
    assert hs.snapshot_path().read_bytes() == before
    assert not [p for p in hs.snapshot_path().parent.iterdir()
                if p.name.startswith(".hub_snapshot.") and p.name.endswith(".sqlite")]


# The registry probes from the same review already held; they stay pinned.

def test_two_registry_publications_preserve_union():
    barrier = threading.Barrier(3)
    errors = []

    def publish(key):
        try:
            barrier.wait()
            tombstones.publish_identities({key}, {key + "-hash"})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=publish, args=(key,)) for key in ("a", "b")]
    for t in threads:
        t.start()
    barrier.wait()
    for t in threads:
        t.join(5)
    assert not errors and not any(t.is_alive() for t in threads)
    value = json.loads(tombstones.registry_path().read_text())
    assert value["source_keys"] == ["a", "b"]
    assert value["payload_hashes"] == ["a-hash", "b-hash"]


def test_writer_holds_registry_lock_through_its_write():
    p = {"ts": "2026-10-01T09:00:00Z", "summary": "probe writer lock"}
    started, completed = threading.Event(), threading.Event()

    def publish():
        started.set()
        tombstones.publish_payloads([p])
        completed.set()

    with tombstones.write_guard(p) as allowed:
        assert allowed
        t = threading.Thread(target=publish)
        t.start()
        assert started.wait(2)
        assert not completed.wait(.15), "forget published inside an active writer guard"
    t.join(5)
    assert completed.is_set() and tombstones.is_forgotten(p)


@pytest.mark.parametrize("data", [b"{partial", b"\xff", b'{"version":1}'])
def test_corrupt_registry_fails_closed_for_publish_and_enqueue(data):
    registry = tombstones.registry_path()
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_bytes(data)
    p = {"ts": "2026-10-01T09:00:00Z", "summary": "probe corrupt"}
    with pytest.raises(tombstones.RegistryError):
        tombstones.publish_payloads([p])
    with pytest.raises(tombstones.RegistryError):
        outbox.enqueue(p)


def test_unreadable_registry_fails_closed(monkeypatch):
    p = {"ts": "2026-10-01T09:00:00Z", "summary": "probe unreadable"}
    tombstones.publish_payloads([p])
    registry = tombstones.registry_path()
    original = Path.read_bytes

    def deny(path):
        if path == registry:
            raise PermissionError("probe unreadable registry")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", deny)
    with pytest.raises(tombstones.RegistryError):
        outbox.enqueue(p)


@pytest.mark.parametrize("stage", ["before_replace", "after_replace"])
def test_interrupted_atomic_publication_retains_prior_fence(monkeypatch, stage):
    a = {"ts": "2026-10-01T09:00:00Z", "summary": "prior durable fence"}
    b = {"ts": "2026-10-01T09:00:01Z", "summary": "interrupted publication"}
    tombstones.publish_payloads([a])
    if stage == "before_replace":
        def fail_replace(*args):
            raise OSError("simulated interruption before rename")
        monkeypatch.setattr(tombstones.os, "replace", fail_replace)
    else:
        real_fsync = os.fsync
        calls = []

        def fail_directory_fsync(fd):
            calls.append(fd)
            if len(calls) == 2:
                raise OSError("simulated interruption after rename")
            return real_fsync(fd)
        monkeypatch.setattr(tombstones.os, "fsync", fail_directory_fsync)
    if stage == "before_replace":
        with pytest.raises(tombstones.RegistryError):
            tombstones.publish_payloads([b])
    else:
        # The replace is the publication (7A review finding 8); only its
        # durability across a power loss is uncertain.
        tombstones.publish_payloads([b])
    assert tombstones.is_forgotten(a)
    assert tombstones.is_forgotten(b) == (stage == "after_replace")


def test_memory_lock_refuses_a_data_folder_that_is_not_a_directory(tmp_path, monkeypatch):
    from unittest import mock

    from khipu import memory_lock

    not_a_folder = tmp_path / "data"
    not_a_folder.write_text("")
    monkeypatch.chdir(tmp_path)
    for folder in (not_a_folder, mock.MagicMock()):
        with mock.patch("khipu.paths.ensure_data_dir", return_value=folder):
            with pytest.raises(memory_lock.MemoryLockError):
                with memory_lock.shared():
                    pass
    assert sorted(p.name for p in tmp_path.iterdir()) == ["data"]
