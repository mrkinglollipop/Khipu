"""A recreation may clear only the exact purge version it observed."""
from datetime import datetime, timedelta, timezone
import fcntl
import os
from pathlib import Path
import sqlite3
import threading
import time

import pytest
from khipu import hub_snapshot as hs


@pytest.fixture
def replica(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    path = hs.snapshot_path()
    with sqlite3.connect(path) as con:
        hs._create_schema(con)
    yield path
    hs._MATRIX_CACHE.clear()


def _row(stamp):
    return {"slug": "race", "title": "synthetic", "body": "STALE synthetic plaintext",
            "updated_at": stamp}


def test_refresh_during_marker_read_keeps_newer_purge(replica, monkeypatch):
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert hs._mark_purged_topic("race", replica, hub_at=old)
    marker = hs._purged_topic_marker(replica, "race")
    real_open = Path.open
    interleaved = []
    reads = []

    class InterleavedRead:
        def __init__(self, file): self.file = file
        def __enter__(self): return self
        def __exit__(self, *args): return self.file.__exit__(*args)
        def fileno(self): return self.file.fileno()
        def read(self, *args):
            text = self.file.read(*args)
            reads.append(True)
            if len(reads) == 2:
                interleaved.append(True)
                # Pull holds the replica lock, but a purge must still publish
                # its fence before reporting that it cannot clean sqlite.
                assert not hs.purge_topic_in_snapshot("race")["ok"]
            return text

    def open_with_refresh(path, *args, **kwargs):
        file = real_open(path, *args, **kwargs)
        return InterleavedRead(file) if path == marker else file

    monkeypatch.setattr(Path, "open", open_with_refresh)
    result = hs._write_pulled_tables([_row(old + timedelta(seconds=10))], None,
                                     read_at=time.time())
    assert interleaved and result["topics"]["ok"], result
    assert marker.exists(), "a later local purge must survive recreation cleanup"
    assert hs._read_marker(marker) == ("race", None)
    assert hs._purge_marker_stamp(replica, "race") > old + timedelta(seconds=10)
    assert "race" in hs._purged_topic_slugs(replica)


@pytest.mark.parametrize("clear_kind", ["recreation", "full_dump"])
def test_purge_published_at_unlink_cannot_be_removed(replica, monkeypatch, clear_kind):
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert hs._mark_purged_topic("race", replica, hub_at=old)
    marker = hs._purged_topic_marker(replica, "race")
    real_unlink = Path.unlink
    lock_attempted = threading.Event()
    finished = threading.Event()
    blocked = []
    real_flock = fcntl.flock
    results = []
    threads = []
    attempted = []

    def publish():
        results.append(hs._mark_purged_topic("race", replica))
        finished.set()

    def observe_publication_lock(fd, operation):
        if threads and threading.current_thread() is threads[0]:
            try:
                real_flock(fd, operation | fcntl.LOCK_NB)
            except BlockingIOError:
                blocked.append(True)
                lock_attempted.set()
                return real_flock(fd, operation)
            else:
                blocked.append(False)
                lock_attempted.set()
                return
        return real_flock(fd, operation)

    def unlink_with_purge(path, *args, **kwargs):
        if path == marker and not attempted:
            attempted.append(True)
            thread = threading.Thread(target=publish, daemon=True)
            threads.append(thread)
            thread.start()
            # Observe a real nonblocking acquisition attempt while the
            # cleaner is about to unlink; no scheduler-delay assumption.
            assert lock_attempted.wait(2)
            assert blocked == [True], "publication must wait through compare/unlink"
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(fcntl, "flock", observe_publication_lock)
    monkeypatch.setattr(Path, "unlink", unlink_with_purge)
    if clear_kind == "recreation":
        result = hs._write_pulled_tables([_row(old + timedelta(seconds=10))], None,
                                         read_at=time.time())
        assert result["topics"]["ok"], result
    else:
        hs._clear_purged_topics(replica, dumped_after=time.time())
    for thread in threads:
        thread.join(2)
    assert attempted and finished.is_set() and results == [True]
    assert marker.exists(), "compare then unlink must not erase a newer publication"
    assert hs._read_marker(marker) == ("race", None)
    assert hs._purge_marker_stamp(replica, "race") > old + timedelta(seconds=10)
    assert "race" in hs._purged_topic_slugs(replica)


def test_atomic_publication_leaves_open_marker_version_unchanged(replica):
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert hs._mark_purged_topic("race", replica, hub_at=old)
    marker = hs._purged_topic_marker(replica, "race")
    with marker.open() as held:
        before = held.read()
        before_stat = os.fstat(held.fileno())
        assert hs._mark_purged_topic("race", replica, hub_at=old + timedelta(seconds=20))
        held.seek(0)
        assert held.read() == before, "an open marker must be an immutable version"
        assert os.fstat(held.fileno()).st_mtime_ns == before_stat.st_mtime_ns
    assert hs._purge_marker_stamp(replica, "race") == old + timedelta(seconds=20)


@pytest.mark.parametrize("file_time, incoming_seconds, allowed", [
    (1, -1, False), (1_900_000_000, 1, True),
])
def test_hub_clock_takes_precedence_over_local_marker_time(
        replica, file_time, incoming_seconds, allowed):
    hub = datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert hs._mark_purged_topic("race", replica, hub_at=hub)
    marker = hs._purged_topic_marker(replica, "race")
    os.utime(marker, (file_time, file_time))
    result = hs._write_pulled_tables([_row(hub + timedelta(seconds=incoming_seconds))],
                                     None, read_at=time.time())
    assert result["topics"]["ok"], result
    assert (hs.topic_detail_snapshot("race") is not None) is allowed
    assert marker.exists() is not allowed
