"""Crash, corruption, retention and reconstruction regressions; no live state."""
import gzip
import json
import os
from datetime import timedelta

import pytest

from khipu import routing, routing_compact as rc
from tests import routing_compact_synth as synth
from tests.test_routing_compact import END, _log, world


def _chosen():
    return {"before": 3, "positions": [1], "left_out": [b'B'], "kept": [b'A', b'C']}


def test_uncommitted_archive_cannot_duplicate_live_lines(world):
    path = routing.events_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'A\nB\nC\n')
    folder = rc.archive_dir()
    folder.mkdir()
    (folder / 'events-20261102T120000Z-aaaaaa.jsonl.gz').write_bytes(rc._archive_bytes(_chosen(), END))
    assert rc.full_log() == [b'A', b'B', b'C']


def test_committed_generation_reconstructs_with_appended_tail(world):
    path = routing.events_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    rc._replace(path, b'A\nC\nD\n', 'events-20261102T120000Z-aaaaaa.jsonl.gz', rc._archive_bytes(_chosen(), END))
    assert rc.full_log() == [b'A', b'B', b'C', b'D']


def test_damaged_archive_reports_loss_and_returns_other_history(world):
    path = routing.events_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    rc._replace(path, b'A\nC\n', 'events-20261102T120000Z-aaaaaa.jsonl.gz', rc._archive_bytes(_chosen(), END))
    (rc.archive_dir() / 'events-20261103T120000Z-bbbbbb.jsonl.gz').write_bytes(b'\x1f\x8bpartial')
    with pytest.warns(RuntimeWarning, match='unavailable'):
        assert rc.full_log() == [b'A', b'B', b'C']


def test_archive_publish_is_atomic_and_failed_temps_are_removed(world, monkeypatch):
    folder = rc.archive_dir()
    folder.mkdir(parents=True)
    target = folder / 'events-20261102T120000Z-aaaaaa.jsonl.gz'
    real_fsync = os.fsync
    def fail(fd):
        assert not target.exists()
        assert not rc._archives()
        raise OSError('synthetic fsync failure')
    monkeypatch.setattr(os, 'fsync', fail)
    with pytest.raises(OSError, match='synthetic'):
        rc._write_archive(target, b'archive')
    assert list(folder.iterdir()) == []
    monkeypatch.setattr(os, 'fsync', real_fsync)


def test_abandoned_archive_temp_is_cleaned(world):
    folder = rc.archive_dir()
    folder.mkdir(parents=True)
    temp = folder / '.archive-abandoned.tmp'
    temp.write_bytes(b'partial')
    os.utime(temp, (0, 0))
    rc.prune_archives(END)
    assert not temp.exists()

