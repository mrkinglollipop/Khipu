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


@pytest.mark.parametrize('fault', [None, 'invalid', 'copy', 'replace'])
def test_embedding_crash_recovers_committed_work(graph, tmp_path, monkeypatch, capsys, fault):
    import tempfile
    _, e, live = graph
    before = live.read_bytes()
    stages = tmp_path / 'stages'
    stages.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(stages))
    def run(args, stage, key):
        with sqlite3.connect(stage) as conn:
            conn.execute("INSERT INTO embeddings(node_id,chunk_text) VALUES ('paid','saved')")
        if fault == 'invalid':
            stage.write_bytes(b'corrupt stage')
        raise RuntimeError('synthetic worker crash')
    monkeypatch.setattr(e, '_run_main', run)
    monkeypatch.setattr(e, '_load_api_key', lambda: 'synthetic')
    monkeypatch.setattr(sys, 'argv', ['embed_corpus.py', '--db', str(live)])
    if fault == 'copy':
        def copy(*a, **kw): raise OSError('synthetic publication copy failure')
        monkeypatch.setattr(shutil, 'copyfileobj', copy)
    elif fault == 'replace':
        def replace(*a): raise OSError('synthetic replace failure')
        monkeypatch.setattr(os, 'replace', replace)
    assert e.main() == 1
    err = capsys.readouterr().err
    assert 'synthetic worker crash' in err
    retained = list(stages.glob('embed_stage_*.sqlite'))
    if fault is None:
        assert count(live, 'paid') == 1
        assert retained == []
    else:
        assert live.read_bytes() == before
        assert len(retained) == 1
        assert str(retained[0]) in err
        assert retained[0].stat().st_mode & 0o077 == 0
        if fault != 'invalid':
            assert count(retained[0], 'paid') == 1
    assert count(live) == 1
    assert list(live.parent.glob('.graph.sqlite.publish-*')) == []


def test_dry_run_crash_never_publishes(graph, tmp_path, monkeypatch):
    import tempfile
    _, e, live = graph
    before = live.read_bytes()
    stages = tmp_path / 'stages'
    stages.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(stages))
    def run(args, stage, key):
        with sqlite3.connect(stage) as conn:
            conn.execute("INSERT INTO embeddings(node_id,chunk_text) VALUES ('dry','private')")
        raise RuntimeError('synthetic dry-run crash')
    monkeypatch.setattr(e, '_run_main', run)
    monkeypatch.setattr(e, '_load_api_key', lambda: None)
    monkeypatch.setattr(sys, 'argv', ['embed_corpus.py', '--db', str(live), '--dry-run'])
    assert e.main() == 1
    assert live.read_bytes() == before
    assert list(stages.iterdir()) == []


@pytest.mark.parametrize('mode', [0o644, 0o640, 0o600, None])
def test_publication_preserves_database_mode(graph, tmp_path, mode):
    from graph_db import publish
    _, _, live = graph
    stage = tmp_path / 'stage.sqlite'
    shutil.copyfile(live, stage)
    destination = live if mode is not None else tmp_path / 'new.sqlite'
    if mode is not None:
        live.chmod(mode)
    publish(stage, destination)
    assert destination.stat().st_mode & 0o777 == (mode if mode is not None else 0o600)
    assert count(destination) == 1


@pytest.mark.parametrize('corpus_rc,edges_rc', [(1, 0), (0, 7), (1, 7), (0, 3), (1, 3), (-1, 0), (0, 0)])
def test_nightly_embedding_failure_reaches_job_receipt(tmp_path, monkeypatch, capsys,
                                                     corpus_rc, edges_rc):
    from types import SimpleNamespace
    from khipu import jobs
    g = load('graphify_nightly')
    resolved = tmp_path / 'sources.json'
    resolved.write_text(json.dumps({'code_roots': ['synthetic']}))
    monkeypatch.setattr(g, '_export_sources', lambda env: resolved)
    monkeypatch.setattr(g, '_khipu_root', lambda: None)
    steps = []
    def step(label, *a, **kw):
        steps.append(label)
        return {'embed corpus': corpus_rc, 'embed edges': edges_rc}.get(label, 0)
    monkeypatch.setattr(g, 'run_step', step)
    monkeypatch.setattr(jobs, 'GRAPHIFY_NIGHTLY', GRAPH / 'graphify_nightly.py')
    monkeypatch.setattr(jobs, 'ensure_data_dir', lambda: tmp_path / 'data')
    monkeypatch.setattr(jobs, '_log_paths', lambda stem: (tmp_path / 'out.log', tmp_path / 'err.log'))
    def launch(argv, *, stdout, stderr, env, start_new_session):
        assert argv == [sys.executable, str(GRAPH / 'graphify_nightly.py')]
        assert start_new_session is True
        def wait(*, timeout):
            assert timeout == jobs.DRIVER_TIMEOUT_SECONDS
            return g.main()
        return SimpleNamespace(wait=wait)
    monkeypatch.setattr(jobs.subprocess, 'Popen', launch)
    offsite = []
    monkeypatch.setattr(jobs, '_offsite_if_due', lambda: offsite.append(True))
    # Edge rc=3 is deferred work, not failure; backup scheduling must remain
    # independent of genuine embedding failures as well.
    expected = corpus_rc or (edges_rc if edges_rc != 3 else 0)
    assert jobs.run_graph_build() == expected
    assert jobs._read_job_state('graph_build')['exit'] == expected
    assert steps == ['semantic extract', 'build graph', 'embed corpus', 'embed edges']
    output = capsys.readouterr().out
    if expected:
        assert 'nightly FAILED' in output
        for label, rc in [('embed corpus', corpus_rc), ('embed edges', edges_rc)]:
            if rc and not (label == 'embed edges' and rc == 3):
                assert f'{label} rc={rc}' in output
        assert 'nightly complete' not in output
    else:
        assert 'nightly complete' in output
    assert offsite == [True]
    if edges_rc == 3:
        assert 'edge-embed step deferred — will retry next run' in output


@pytest.mark.parametrize("registry_exists", [False, True])
def test_explicit_empty_sources_export_respects_registry(tmp_path, monkeypatch,
                                                        registry_exists):
    from khipu import sources
    g = load("graphify_nightly")
    resolved = tmp_path / "standalone" / "graph_sources.resolved.json"
    resolved.parent.mkdir()
    original = json.dumps({
        "collectors": {key: False for key in sources.COLLECTOR_KEYS},
        "code_roots": [],
    })
    resolved.write_text(original)
    monkeypatch.setenv("KHIPU_GRAPH_SOURCES_RESOLVED", str(resolved))
    monkeypatch.setenv("KHIPU_ROOT", str(GRAPH.parents[1]))
    code = tmp_path / "code"
    code.mkdir()
    if registry_exists:
        sources.save_sources({"schema_version": 2, "sources": [
            {"id": "code:synthetic", "kind": "code_ast",
             "root": str(code), "enabled": True}]})
    exported = g._export_sources(g._khipu_env(os.environ.copy()))
    if registry_exists:
        assert exported == resolved
        payload = json.loads(resolved.read_text())
        assert payload["code_roots"] == [str(code)]
        assert payload["collectors"]["code_ast"] is True
        assert g._should_skip_build(resolved) == (False, "")
    else:
        assert exported is None
        assert resolved.read_text() == original
        assert not sources.sources_file().exists()
        assert g._should_skip_build(resolved) == (True, "no_sources")


def _run_nightly_with_mirror(tmp_path, monkeypatch, mirror, root_set=True):
    g = load('graphify_nightly')
    resolved = tmp_path / 'sources.json'
    resolved.write_text(json.dumps({'code_roots': ['synthetic']}))
    monkeypatch.setattr(g, '_export_sources', lambda env: resolved)
    monkeypatch.setattr(g, '_khipu_root', lambda: tmp_path if root_set else None)
    monkeypatch.setattr(g, '_log_path', lambda: tmp_path / 'nightly.log')
    monkeypatch.setattr(g, 'run_step', lambda *a, **kw: 0)
    mirror_calls = []
    def run(argv, **kw):
        mirror_calls.append(argv)
        return mirror()
    monkeypatch.setattr(g.subprocess, 'run', run)
    return g, mirror_calls


@pytest.mark.parametrize('failure,reason', [
    ('stderr', 'connection timeout expired'),
    ('json_error', 'graph.sqlite not found on the machine that builds it'),
    ('json_drift', 'nodes_content_drift=7, edges_missing_in_pg=2'),
    ('timeout', 'timed out'),
    ('oserror', 'synthetic spawn failure'),
])
def test_failed_graph_mirror_fails_the_nightly(tmp_path, monkeypatch, capsys, failure, reason):
    import subprocess
    drift = {'ok': False, 'nodes_content_drift': 7, 'edges_content_drift': 0,
             'edges_missing_in_pg': 2, 'sqlite_nodes': 100}
    def mirror():
        if failure == 'timeout':
            raise subprocess.TimeoutExpired('graph-sync', 600)
        if failure == 'oserror':
            raise OSError('synthetic spawn failure')
        if failure == 'json_error':
            return subprocess.CompletedProcess(
                [], 2, json.dumps({'ok': False, 'error': reason}, indent=2), '')
        if failure == 'json_drift':
            return subprocess.CompletedProcess(
                [], 2, json.dumps({'seconds': 1.0, 'drift': drift}, indent=2), '')
        return subprocess.CompletedProcess([], 1, '', reason)
    g, calls = _run_nightly_with_mirror(tmp_path, monkeypatch, mirror)
    assert g.main() != 0
    assert len(calls) == 1
    failed = [line for line in capsys.readouterr().out.splitlines() if 'nightly FAILED' in line]
    assert len(failed) == 1
    assert reason in failed[0]
    assert not failed[0].rstrip().endswith('}')


def test_failed_mirror_is_reported_as_a_failed_graph_job(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    from khipu import jobs
    g, _ = _run_nightly_with_mirror(
        tmp_path, monkeypatch,
        lambda: subprocess.CompletedProcess([], 1, '', 'connection timeout expired'))
    monkeypatch.setattr(jobs, 'GRAPHIFY_NIGHTLY', GRAPH / 'graphify_nightly.py')
    monkeypatch.setattr(jobs, 'ensure_data_dir', lambda: tmp_path / 'data')
    monkeypatch.setattr(jobs, '_log_paths', lambda stem: (tmp_path / 'out.log', tmp_path / 'err.log'))
    monkeypatch.setattr(jobs, '_offsite_if_due', lambda: None)
    monkeypatch.setattr(jobs.subprocess, 'Popen',
        lambda argv, **kw: SimpleNamespace(wait=lambda *, timeout: g.main()))
    assert jobs.run_graph_build() != 0
    assert jobs._read_job_state('graph_build')['exit'] != 0


@pytest.mark.parametrize('mirror_rc', [0, 2])
def test_offsite_starts_after_mirror_finishes_and_preserves_its_exit(
    tmp_path, monkeypatch, mirror_rc,
):
    import subprocess
    from types import SimpleNamespace
    from khipu import jobs

    events = []
    def mirror():
        events.append('mirror finished')
        return subprocess.CompletedProcess([], mirror_rc, '{}', '')

    g, _ = _run_nightly_with_mirror(tmp_path, monkeypatch, mirror)
    monkeypatch.setattr(jobs, 'GRAPHIFY_NIGHTLY', GRAPH / 'graphify_nightly.py')
    monkeypatch.setattr(jobs, 'ensure_data_dir', lambda: tmp_path / 'data')
    monkeypatch.setattr(jobs, '_log_paths', lambda stem: (tmp_path / 'out.log', tmp_path / 'err.log'))
    def launch(argv, **kw):
        # Only the nightly driver may be spawned; offsite stays synchronous.
        assert argv == [sys.executable, str(GRAPH / 'graphify_nightly.py')]
        def wait(*, timeout):
            rc = g.main()
            events.append('nightly finished')
            return rc
        return SimpleNamespace(wait=wait)
    monkeypatch.setattr(jobs.subprocess, 'Popen', launch)
    monkeypatch.setattr(jobs, '_offsite_if_due', lambda: events.append('offsite started'))

    assert jobs.run_graph_build() == mirror_rc
    assert events == ['mirror finished', 'nightly finished', 'offsite started']
    assert jobs._read_job_state('graph_build')['exit'] == mirror_rc


def test_non_producer_mirror_skip_is_logged_and_exits_zero(tmp_path, monkeypatch, capsys):
    import subprocess
    skipped = json.dumps({'ok': True, 'skipped': 'not the graph producer; PG is read-only'})
    g, calls = _run_nightly_with_mirror(
        tmp_path, monkeypatch, lambda: subprocess.CompletedProcess([], 0, skipped, ''))
    assert g.main() == 0
    out = capsys.readouterr().out
    assert 'graph mirror skipped: not the graph producer' in out
    assert 'graph mirror ok' not in out


@pytest.mark.parametrize('root_set', [True, False])
def test_clean_or_disabled_mirror_keeps_exit_zero(tmp_path, monkeypatch, capsys, root_set):
    import subprocess
    g, calls = _run_nightly_with_mirror(
        tmp_path, monkeypatch,
        lambda: subprocess.CompletedProcess([], 0, '{}', ''), root_set=root_set)
    assert g.main() == 0
    assert len(calls) == (1 if root_set else 0)
    assert 'nightly complete' in capsys.readouterr().out


@pytest.mark.parametrize('mirror_rc', [0, 2])
def test_mirror_connection_retry_diagnostics_reach_nightly_log(
    tmp_path, monkeypatch, capsys, mirror_rc,
):
    import subprocess
    diagnostic = 'graph mirror connect: attempt 1/3 failed (OperationalError); retrying in 2s'
    g, _ = _run_nightly_with_mirror(
        tmp_path, monkeypatch,
        lambda: subprocess.CompletedProcess(
            [], mirror_rc, json.dumps({'error': 'synthetic connection failure'}),
            diagnostic + '\nunrelated private diagnostic\n',
        ),
    )
    assert g.main() == mirror_rc
    output = capsys.readouterr().out
    assert diagnostic in output
    assert 'unrelated private diagnostic' not in output
