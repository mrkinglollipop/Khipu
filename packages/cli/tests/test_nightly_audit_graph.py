"""Offline regressions adapted from /tmp/auditnightly graph reproductions."""
import builtins
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import sys

import pytest

GRAPH = Path(__file__).resolve().parents[3] / 'third_party' / 'graphify'


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith(('KHIPU_', 'ALZY_', 'XDG_')):
            monkeypatch.delenv(key)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('KHIPU_KEYCHAIN', '0')
    monkeypatch.setenv('KHIPU_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setenv('KHIPU_MEMORY_ROOT', str(tmp_path / 'memory'))
    monkeypatch.syspath_prepend(str(GRAPH))
    def deny(*a, **kw):
        raise AssertionError('network forbidden')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)


def load(name):
    spec = importlib.util.spec_from_file_location('audit_' + name, GRAPH / (name + '.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def graph(tmp_path):
    b, e = load('build_graph'), load('embed_corpus')
    live = tmp_path / 'graph.sqlite'
    with sqlite3.connect(live) as conn:
        conn.executescript(b.SCHEMA)
        conn.execute("INSERT INTO embeddings(node_id,chunk_text) VALUES ('sentinel','preserve-me')")
    return b, e, live


def count(live, key='sentinel'):
    with sqlite3.connect(live) as conn:
        return conn.execute('SELECT COUNT(*) FROM embeddings WHERE node_id=?', (key,)).fetchone()[0]


@pytest.mark.parametrize('fault', ['copy', 'replace', 'invalid'])
def test_embedding_publication_failure_preserves_original(graph, monkeypatch, fault):
    _, e, live = graph
    before = live.read_bytes()
    def run(args, stage, key):
        if fault == 'invalid':
            stage.write_bytes(b'not a sqlite database')
        else:
            with sqlite3.connect(stage) as conn:
                conn.execute("INSERT INTO embeddings(node_id,chunk_text) VALUES ('new','value')")
        return 0
    monkeypatch.setattr(e, '_run_main', run)
    monkeypatch.setattr(e, '_load_api_key', lambda: 'synthetic')
    monkeypatch.setattr(sys, 'argv', ['embed_corpus.py', '--db', str(live)])
    real_open = builtins.open
    class FailWrite:
        def __init__(self, handle): self.handle = handle
        def __enter__(self): return self
        def __exit__(self, *args): return self.handle.__exit__(*args)
        def write(self, data): raise OSError('synthetic disk full')
        def __getattr__(self, key): return getattr(self.handle, key)
    def opening(file, mode='r', *a, **kw):
        handle = real_open(file, mode, *a, **kw)
        # The original reproduction failed writes to live. Also fail its new
        # sibling publication copy, while allowing the local worker stage.
        if fault == 'copy' and mode == 'wb' and Path(file).parent == live.parent:
            return FailWrite(handle)
        return handle
    monkeypatch.setattr(builtins, 'open', opening)
    if fault == 'replace':
        def replace(*a): raise OSError('synthetic replace failure')
        monkeypatch.setattr(os, 'replace', replace)
    assert e.main() != 0
    assert live.read_bytes() == before
    assert count(live) == 1


def test_build_preservation_failure_aborts(graph, monkeypatch):
    b, _, live = graph
    before = live.read_bytes()
    real_copy = shutil.copyfile
    def copy(src, dst, **kw):
        if str(src) == str(live): raise OSError('synthetic source read failure')
        return real_copy(src, dst, **kw)
    monkeypatch.setattr(shutil, 'copyfile', copy)
    with pytest.raises(OSError, match='synthetic'):
        b.write_atomic(live, [], [])
    assert live.read_bytes() == before
    assert count(live) == 1


def test_dry_run_never_publishes_over_concurrent_commit(graph, monkeypatch):
    _, e, live = graph
    def run(args, stage, key):
        with sqlite3.connect(live) as conn:
            conn.execute("INSERT INTO embeddings(node_id,chunk_text) VALUES ('concurrent','committed')")
        return 0
    monkeypatch.setattr(e, '_run_main', run)
    monkeypatch.setattr(e, '_load_api_key', lambda: None)
    monkeypatch.setattr(sys, 'argv', ['embed_corpus.py', '--db', str(live), '--dry-run'])
    assert e.main() == 0
    assert count(live, 'concurrent') == 1


@pytest.mark.parametrize('cached', [False, True])
def test_nightly_exports_before_skip(tmp_path, monkeypatch, cached):
    from khipu import sources
    g = load('graphify_nightly')
    code = tmp_path / 'code'
    code.mkdir()
    (code / 'sample.py').write_text('answer = 42\n')
    sources.save_sources({'schema_version': 2, 'sources': [
        {'id': 'code:synthetic', 'kind': 'code_ast', 'root': str(code), 'enabled': True}]})
    resolved = sources.resolved_path()
    if cached:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(json.dumps({'collectors': {'code_ast': False}, 'code_roots': []}))
        monkeypatch.setenv('KHIPU_GRAPH_SOURCES_RESOLVED', str(resolved))
    exports, steps = [], []
    def export(env):
        exports.append(True)
        sources.export_resolved()
        return resolved
    monkeypatch.setattr(g, '_export_sources', export)
    monkeypatch.setattr(g, '_khipu_root', lambda: None)
    monkeypatch.setattr(g, 'run_step', lambda *a, **kw: steps.append((a, kw)) or 0)
    assert g.main() == 0
    assert exports == [True]
    assert any(a[0] == 'build graph' for a, kw in steps)
    assert all(kw['env']['KHIPU_GRAPH_SOURCES_RESOLVED'] == str(resolved) for a, kw in steps)


def test_failed_build_removes_private_stage(graph, tmp_path, monkeypatch):
    b, _, live = graph
    before = live.read_bytes()
    real_connect = sqlite3.connect
    stages = []
    modes = []
    def connect(path, *a, **kw):
        p = Path(path)
        if p != live:
            stages.append(p)
            modes.append(p.stat().st_mode & 0o077)
        return real_connect(path, *a, **kw)
    monkeypatch.setattr(sqlite3, 'connect', connect)
    with pytest.raises(sqlite3.ProgrammingError):
        b.write_atomic(live, [{'id': 'missing-required-fields'}], [])
    assert stages
    assert modes == [0]
    assert all(not p.exists() for p in stages)
    assert live.read_bytes() == before


@pytest.mark.parametrize('writer', ['build', 'embed'])
def test_graph_writers_hold_common_lock_for_read_modify_publish(graph, monkeypatch, writer):
    import fcntl
    b, e, live = graph
    lock = live.with_name('.' + live.name + '.lock')
    observed = []
    def check_lock():
        assert lock.exists()
        with open(lock, 'a+') as handle:
            with pytest.raises(BlockingIOError):
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        observed.append(True)
    if writer == 'embed':
        def run(args, stage, key):
            check_lock()
            with sqlite3.connect(stage) as conn:
                conn.execute("INSERT INTO embeddings(node_id,chunk_text) VALUES ('new','value')")
            return 0
        monkeypatch.setattr(e, '_run_main', run)
        monkeypatch.setattr(e, '_load_api_key', lambda: 'synthetic')
        monkeypatch.setattr(sys, 'argv', ['embed_corpus.py', '--db', str(live)])
        assert e.main() == 0
        assert count(live, 'new') == 1
    else:
        real_copy = shutil.copyfile
        def copy(src, dst, **kw):
            if str(src) == str(live): check_lock()
            return real_copy(src, dst, **kw)
        monkeypatch.setattr(shutil, 'copyfile', copy)
        b.write_atomic(live, [], [])
    assert observed == [True]
    assert count(live) == 1
    with open(lock, 'a+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_source_export_uses_the_cli_returned_path(tmp_path, monkeypatch):
    import subprocess
    g = load('graphify_nightly')
    resolved = tmp_path / 'scoped' / 'graph_sources.resolved.json'
    monkeypatch.setattr(g, '_khipu_root', lambda: tmp_path)
    monkeypatch.setenv('KHIPU_GRAPH_SOURCES_RESOLVED', str(tmp_path / 'old.json'))
    monkeypatch.setattr(g.subprocess, 'run', lambda *a, **kw:
        subprocess.CompletedProcess(a, 0, json.dumps({'ok': True, 'path': str(resolved)}), ''))
    assert g._export_sources({}) == resolved
