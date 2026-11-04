"""Crash, corruption, retention and reconstruction regressions; no live state."""
import gzip
import json
import os

import pytest

from khipu import routing, routing_compact as rc
from tests import routing_compact_synth as synth
from tests.test_routing_compact import END, _log
from tests import test_routing_audit_fold as audit

world = audit.world


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
    rc.prune_archives(END)
    assert not temp.exists()


def test_byte_bound_removes_oldest_and_reports_unavailable_history(world, monkeypatch):
    path = routing.events_file()
    path.parent.mkdir(parents=True)
    path.write_bytes(b'A\nB\nC\nD\nE\n')
    paths = []
    original = [b'A', b'B', b'C', b'D', b'E']
    for day in (1, 2, 3):
        chosen = {"before": len(original), "positions": [1], "left_out": [original[1]],
                  "kept": original[:1] + original[2:]}
        name = f'events-2026110{day}T120000Z-aaaaaa.jsonl.gz'
        rc._replace(path, b''.join(line + b'\n' for line in chosen['kept']), name, rc._archive_bytes(chosen, END))
        paths.append(rc.archive_dir() / name)
        original = chosen['kept']
    budget = sum(p.stat().st_size for p in paths[1:])
    monkeypatch.setattr(rc, 'ARCHIVE_MAX_BYTES', budget)
    assert rc.prune_archives(END) == 1
    assert not paths[0].exists() and all(p.exists() for p in paths[1:])
    assert sum(p.stat().st_size for _, p in rc._archives()) <= budget
    with pytest.warns(RuntimeWarning, match='retention.*unavailable'):
        assert rc.full_log() == [b'A', b'C', b'D', b'E']


def test_owner_lines_never_enter_archive_even_when_byte_bound_prunes(world, monkeypatch):
    rows = _log(days=60)
    synth.write(routing.events_file(), rows)
    original = routing.events_file().read_bytes().splitlines()
    owners = [line for line in original if json.loads(line).get('written_by', {}).get('kind') == 'owner']
    assert owners
    out = rc.compact(END)
    assert out['result'] == 'compacted'
    archive = rc.archive_dir() / out['archive']
    dropped = gzip.decompress(archive.read_bytes()).splitlines()[1:]
    assert all(json.loads(line).get('written_by', {}).get('kind') != 'owner' for line in dropped)
    monkeypatch.setattr(rc, 'ARCHIVE_MAX_BYTES', 0)
    assert rc.prune_archives(END) == 1
    with pytest.warns(RuntimeWarning, match='unavailable'):
        restored = rc.full_log()
    assert [line for line in restored if line in owners] == owners


def test_identical_retained_prefix_is_not_a_commit(world):
    path = routing.events_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'A\nB\nB\n')
    chosen = {"before": 3, "positions": [1], "left_out": [b'B'], "kept": [b'A', b'B']}
    rc.archive_dir().mkdir()
    (rc.archive_dir() / 'events-20261102T120000Z-aaaaaa.jsonl.gz').write_bytes(rc._archive_bytes(chosen, END))
    assert rc.full_log() == [b'A', b'B', b'B']


def test_archive_restores_crlf_bytes_exactly(world):
    path = routing.events_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    chosen = {"before": 3, "positions": [1], "left_out": [b'B\r'], "kept": [b'A\r', b'C\r']}
    rc._replace(path, b'A\r\nC\r\n', 'events-20261102T120000Z-aaaaaa.jsonl.gz', rc._archive_bytes(chosen, END))
    assert rc.full_log() == [b'A\r', b'B\r', b'C\r']
