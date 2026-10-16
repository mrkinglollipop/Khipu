import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg
import pytest

from khipu import db, forget, hub_snapshot as hs, outbox, tombstones
from tests import test_forget_ownership as fo
from tests.test_forget_write_gate import P, _complete_forget


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith(('KHIPU_', 'ALZY_')):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('KHIPU_DATA_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'native'))
    monkeypatch.setenv('KHIPU_OUTBOX', str(tmp_path / 'outbox'))
    monkeypatch.setenv('KHIPU_KEYCHAIN', '0')
    monkeypatch.setenv('KHIPU_TEST', '1')
    monkeypatch.setattr(psycopg, 'connect', lambda *a, **k: pytest.fail('live PG forbidden'))
    monkeypatch.setattr(outbox, '_queued_origin', lambda: {'origin': None})
    db._TABLE_COLUMNS_CACHE.clear()


@pytest.mark.parametrize('writer', ['incremental', 'bulk_scrub'])
def test_replica_checks_column_identity_when_raw_lacks_it(tmp_path, monkeypatch, writer):
    _complete_forget(tmp_path, monkeypatch)
    assert tombstones.is_forgotten(P)
    dest = hs.snapshot_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(dest)
    hs._create_schema(con)
    con.commit()
    con.close()
    # A second, historical copy under another id. The source identity is in
    # its columns, while an older/edit-rewritten raw no longer carries it.
    row = {**P, 'id': 2, 'raw': {'summary': 'historical rewritten raw'},
           'summary': 'a new synthesis of the forgotten transcript'}
    if writer == 'incremental':
        result = hs.upsert_episode(row, [])
    else:
        con = sqlite3.connect(dest)
        con.execute('INSERT INTO episodes (id, ts, summary, session_id, harness, transcript_range, raw) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (2, P['ts'], row['summary'], P['session_id'], P['harness'],
                     P['transcript_range'], json.dumps(row['raw'])))
        con.commit()
        con.close()
        result = hs.scrub_forgotten_snapshot()
    con = sqlite3.connect(dest)
    try:
        live = con.execute('SELECT id FROM episodes WHERE id=2 AND deleted_at IS NULL').fetchall()
    finally:
        con.close()
    assert live == [], f'{writer} restored forgotten column source: {result}; rows={live}'


def test_refresh_waits_for_forget_before_it_reads_the_hub(tmp_path, monkeypatch):
    root = tmp_path / 'memory'
    root.mkdir()
    fo.write_lines(root, [P])
    world = {'episodes': {1: dict(P, id=1, ts=fo.BASE, raw=dict(P), deleted=False)},
             'sources': [fo.ForgetOwnershipTest._source(None, 1, P)], 'sources_enabled': True}
    monkeypatch.setattr(db, 'connect', lambda *a, **k: fo._Conn(fo._WorldCur(world)))
    dumping, release = threading.Event(), threading.Event()
    results, errors = {}, []

    class Pg:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def cursor(self): return self
        def execute(self, *args): pass
        def fetchone(self): return (datetime.now(timezone.utc),)
        def fetchall(self):
            return [(1, P['ts'], P['summary'], json.dumps(P), '2026-10-16T00:00:00Z')]

    @contextmanager
    def fake_connect(**kwargs):
        yield Pg()

    def pause_after_actual_episode_insert(cur, con):
        # Real _insert_episodes has written the soft-deleted hub row into
        # the staging transaction. A later table is slow or the writer dies.
        assert con.execute('SELECT summary FROM episodes WHERE id=1').fetchone() == (P['summary'],)
        dumping.set()
        assert release.wait(10)
        return 0

    monkeypatch.setattr(hs, 'try_hub_connect', fake_connect)
    monkeypatch.setattr(hs, '_pg_columns', lambda *a: {'id', 'ts', 'summary', 'raw', 'deleted_at'})
    monkeypatch.setattr(hs, '_insert_topics', pause_after_actual_episode_insert)
    for name in ('_insert_topic_revisions', '_insert_nodes', '_insert_edges',
                 '_insert_profiles', '_insert_memory_embeddings', '_insert_decisions',
                 '_insert_deliverables', '_insert_ledger'):
        monkeypatch.setattr(hs, name, lambda *a: 0)

    def refresh():
        try:
            results['refresh'] = hs.refresh()
        except BaseException as exc:
            errors.append(exc)

    original_scan = forget._native_job_remainder
    thread = threading.Thread(target=refresh)

    def scan_after_replica_scan(*args, **kwargs):
        # Every earlier replica check is complete. A concurrent process
        # starts its refresh while forget still holds the exclusive lock:
        # it must wait for the forget before it reads anything.
        thread.start()
        assert not dumping.wait(0.5), 'refresh dumped while forget held the memory lock'
        return original_scan(*args, **kwargs)

    monkeypatch.setattr(forget, '_native_job_remainder', scan_after_replica_scan)
    try:
        result = forget.forget_everywhere(1, memory_root=root)
        assert result.get('complete'), result
        assert not dumping.is_set()
        # Only now may the refresh read, and its publication scrubs the row.
        assert dumping.wait(5)
    finally:
        release.set()
        thread.join(10)
    assert not errors, errors
    assert results['refresh']['ok'], results
    con = sqlite3.connect(hs.snapshot_path())
    try:
        live = con.execute('SELECT id FROM episodes WHERE id=1 AND deleted_at IS NULL').fetchall()
    finally:
        con.close()
    assert live == []
