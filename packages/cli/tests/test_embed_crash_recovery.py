"""Offline crash recovery must preserve live embeddings and close the worker."""
import importlib.util
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile

import pytest


GRAPH = Path(__file__).resolve().parents[3] / 'third_party' / 'graphify'


def load(name):
    spec = importlib.util.spec_from_file_location('recovery_' + name, GRAPH / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def graph(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.syspath_prepend(str(GRAPH))
    def deny(*args, **kwargs):
        raise AssertionError('network forbidden')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)
    embed = load('embed_corpus')
    live = tmp_path / 'graph.sqlite'
    with sqlite3.connect(live) as conn:
        conn.executescript(load('build_graph').SCHEMA)
        conn.executemany(
            'INSERT INTO embeddings(node_id,chunk_text) VALUES (?, ?)',
            [(f'live:{i}', 'synthetic') for i in range(100)],
        )
        conn.execute("INSERT INTO nodes(id,type,name) VALUES ('target','concept','synthetic')")
    stages = tmp_path / 'stages'
    stages.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(stages))
    monkeypatch.setattr(embed, '_load_api_key', lambda: 'synthetic')
    return embed, live, stages


def count(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute('SELECT COUNT(*) FROM embeddings').fetchone()[0]
    finally:
        conn.close()


@pytest.mark.parametrize('rebuild,delete', [(True, True), (True, False), (False, True)])
def test_crashed_destructive_stage_keeps_live_and_stage(graph, monkeypatch, capsys, rebuild, delete):
    embed, live, stages = graph
    before = live.read_bytes()
    def run(args, stage, key):
        conn = sqlite3.connect(stage)
        try:
            if delete:
                conn.execute('DELETE FROM embeddings')
                conn.commit()
        finally:
            conn.close()
        raise RuntimeError('synthetic committed-stage crash')
    monkeypatch.setattr(embed, '_run_main', run)
    monkeypatch.setattr(sys, 'argv', ['embed_corpus.py', '--db', str(live)] +
                        (['--rebuild'] if rebuild else []))
    assert embed.main() == 1
    assert count(live) == 100
    assert live.read_bytes() == before
    retained = list(stages.glob('embed_stage_*.sqlite'))
    assert len(retained) == 1
    assert count(retained[0]) == (0 if delete else 100)
    assert str(retained[0]) in capsys.readouterr().err


def track_worker_connections(embed, live, monkeypatch):
    real_connect = sqlite3.connect
    workers = []
    class TrackedConnection(sqlite3.Connection):
        closed = False
        rolled_back = False
        def rollback(self):
            self.rolled_back = True
            return super().rollback()
        def close(self):
            self.closed = True
            return super().close()
    def connect(database, *args, **kwargs):
        if Path(str(database)).name.startswith('embed_stage_'):
            kwargs['factory'] = TrackedConnection
            conn = real_connect(database, *args, **kwargs)
            workers.append(conn)
            return conn
        return real_connect(database, *args, **kwargs)
    monkeypatch.setattr(embed.sqlite3, 'connect', connect)
    return workers


def test_uncommitted_crash_rolls_back_and_closes_before_publication(graph, monkeypatch):
    embed, live, stages = graph
    workers = track_worker_connections(embed, live, monkeypatch)
    def crash(node, lookup):
        workers[0].execute('DELETE FROM embeddings')
        raise RuntimeError('synthetic uncommitted crash')
    monkeypatch.setattr(embed, 'extract_text_chunks', crash)
    real_publish = embed.publish
    published = []
    def publish(stage, destination):
        assert workers[0].closed
        assert workers[0].rolled_back
        assert count(stage) == 100
        published.append(True)
        return real_publish(stage, destination)
    monkeypatch.setattr(embed, 'publish', publish)
    monkeypatch.setattr(sys, 'argv', ['embed_corpus.py', '--db', str(live), '--types', 'concept'])
    assert embed.main() == 1
    assert published == [True]
    assert workers[0].closed and workers[0].rolled_back
    assert count(live) == 100
    assert list(stages.iterdir()) == []


@pytest.mark.parametrize('options', [['--node', 'missing'], ['--dry-run'], ['--edges']])
def test_worker_closes_on_early_return(graph, monkeypatch, options):
    embed, live, stages = graph
    workers = track_worker_connections(embed, live, monkeypatch)
    real_publish = embed.publish
    def publish(stage, destination):
        assert workers[0].closed
        return real_publish(stage, destination)
    monkeypatch.setattr(embed, 'publish', publish)
    monkeypatch.setattr(sys, 'argv', ['embed_corpus.py', '--db', str(live), *options])
    assert embed.main() == 0
    assert workers[0].closed
    assert count(live) == 100
    assert list(stages.iterdir()) == []
