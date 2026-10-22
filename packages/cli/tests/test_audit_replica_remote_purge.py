"""Expected invariant: successful catch-up reflects hub topic purge.
The fake hub represents another Mac having tombstoned then purged a topic
between pulls; the local replica never observed the transient tombstone.
"""
from datetime import datetime, timedelta, timezone
import sqlite3
import time

import pytest
from khipu import hub_snapshot as hs


class Hub:
    def __init__(self, topics):
        self.topics = topics
        self.rows = []
        self.queries = []
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def cursor(self): return self
    def execute(self, sql, params=()):
        self.queries.append(sql)
        if sql == 'SELECT now()':
            self.rows = [(datetime.now(timezone.utc),)]
        elif sql.startswith(('SAVEPOINT ', 'RELEASE SAVEPOINT ', 'ROLLBACK TO ')):
            self.rows = []
        elif ' FROM episodes ' in sql:
            self.rows = []
        elif sql == 'SELECT slug FROM topics WHERE slug = ANY(%s)':
            self.rows = [(row['slug'],) for row in self.topics if row['slug'] in params[0]]
        elif ' FROM topics ' in sql:
            assert 'WHERE updated_at >' in sql, sql
            cols = sql[len('SELECT '):sql.index(' FROM ')].split(', ')
            self.rows = [tuple(row.get(c) for c in cols) for row in self.topics
                         if row['updated_at'] > params[0]]
        else:
            raise AssertionError(f'unexpected fake-hub SQL: {sql}')
    def fetchone(self): return self.rows[0] if self.rows else None
    def fetchall(self): return self.rows


@pytest.fixture
def replica(tmp_path, monkeypatch):
    monkeypatch.setenv('KHIPU_DATA_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('KHIPU_TEST', '1')
    import psycopg
    monkeypatch.setattr(psycopg, 'connect', lambda *a, **kw: pytest.fail('Network forbidden'))
    now = datetime.now(timezone.utc)
    dump = now - timedelta(hours=1)
    path = hs.snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        hs._create_schema(con)
        con.executemany('INSERT INTO topics(slug,title,body,updated_at) VALUES (?, ?, ?, ?)',
                        [('purged', 'purged', 'PURGED synthetic topic', dump.isoformat()),
                         ('control', 'control', 'old control', dump.isoformat())])
        con.execute("INSERT INTO topics(slug,title,body,updated_at) VALUES "
                    "('historical','historical','unchanged live control', ?)", (dump.isoformat(),))
        con.executemany("INSERT INTO topic_revisions(slug,body) VALUES (?,?)",
                        [('purged', 'PURGED revision'), ('control', 'live revision')])
        con.executemany("INSERT INTO memory_embeddings(profile,kind,ref,chunk_idx,chunk_text) "
                        "VALUES ('synthetic', 'topic', ?, 0, ?)",
                        [('purged', 'PURGED vector text'), ('control', 'live vector text')])
        con.executemany('INSERT INTO snapshot_meta(key,value) VALUES (?,?)',
                        [(key, dump.isoformat()) for key in ('hub_dumped_at', 'refreshed_at')])
    hs._write_meta({'hub_dumped_at': dump.isoformat(), 'refreshed_at': dump.isoformat()})
    hub = Hub([{'slug': 'control', 'title': 'control', 'body': 'new live control', 'updated_at': now},
               {'slug': 'historical', 'title': 'historical', 'body': 'unchanged live control',
                'updated_at': dump - timedelta(hours=1)}])
    columns = {'episodes': {'id', 'ingested_at', 'deleted_at'},
               'topics': {'slug', 'title', 'body', 'updated_at', 'deleted_at'}}
    monkeypatch.setattr(hs, '_pg_columns', lambda cur, table: columns.get(table, set()))
    monkeypatch.setattr(hs, 'try_hub_connect', lambda **kw: hub)
    return hub, dump


def test_pull_claims_caught_up_but_retains_foreign_purged_topic(replica):
    hub, dump = replica
    result = hs.pull_changes()
    assert result['ok'], result
    assert hs.topic_detail_snapshot('control')['body'] == 'new live control'
    assert hs._utc(hs.caught_up_at()) > dump
    remaining = hs.topic_detail_snapshot('purged')
    print(f'pull={result}; caught_up={hs.caught_up_at()}; purged topic still served={remaining}')
    assert remaining is None, 'successful catch-up must stop serving the topic already purged on the hub'
    assert hs.topic_detail_snapshot('historical')['body'] == 'unchanged live control'
    with sqlite3.connect(hs.snapshot_path()) as con:
        assert con.execute('SELECT slug FROM topic_revisions').fetchall() == [('control',)]
        assert con.execute('SELECT ref FROM memory_embeddings').fetchall() == [('control',)]
        con.execute("INSERT INTO topics_fts(topics_fts) VALUES ('integrity-check')")
        assert con.execute("SELECT count(*) FROM topics_fts WHERE topics_fts MATCH 'PURGED'").fetchone() == (0,)


def test_purge_clears_an_old_fts_index_without_delete_trigger(replica):
    with sqlite3.connect(hs.snapshot_path()) as con:
        con.execute('DROP TRIGGER topics_fts_ad')
    assert hs.pull_changes()['ok']
    assert hs.topic_detail_snapshot('purged') is None
    with sqlite3.connect(hs.snapshot_path()) as con:
        con.execute("INSERT INTO topics_fts(topics_fts) VALUES ('integrity-check')")


def test_missing_key_query_failure_does_not_advance_caught_up(replica, monkeypatch):
    hub, dump = replica
    execute = hub.execute

    def fail(sql, params=()):
        if sql == 'SELECT slug FROM topics WHERE slug = ANY(%s)':
            raise OSError('synthetic key read failure')
        return execute(sql, params)

    monkeypatch.setattr(hub, 'execute', fail)
    result = hs.pull_changes()
    assert not result['ok'], result
    assert hs._utc(hs.caught_up_at()) == dump
    assert hs.topic_detail_snapshot('purged') is not None


def test_absent_key_result_cannot_erase_a_newer_topic_patch(replica, monkeypatch):
    real_write = hs._write_pulled_tables

    def interleave(topics, commitments, aliases, **kw):
        assert hs.upsert_embeddings([], topic_rows=[
            {'slug': 'purged', 'title': 'recreated', 'body': 'new live content',
             'updated_at': datetime.now(timezone.utc)}], read_at=time.time())['ok']
        return real_write(topics, commitments, aliases, **kw)

    monkeypatch.setattr(hs, '_write_pulled_tables', interleave)
    assert hs.pull_changes()['ok']
    assert hs.topic_detail_snapshot('purged')['body'] == 'new live content'
    assert 'purged' not in hs._purged_topic_slugs()


def test_topic_purges_obey_pull_limit_before_writes(replica):
    _hub, dump = replica
    result = hs.pull_changes(limit=1)
    assert not result['ok'] and result['needs_dump'], result
    assert hs._utc(hs.caught_up_at()) == dump
    assert hs.topic_detail_snapshot('purged') is not None
    assert hs.topic_detail_snapshot('control')['body'] == 'old control'


def test_recreated_topic_after_remote_purge(replica):
    hub, _dump = replica
    assert hs.pull_changes()['ok']
    assert hs.topic_detail_snapshot('purged') is None
    hub.topics.append({'slug': 'purged', 'title': 'purged', 'body': 'LEGIT NEW topic same slug',
                       'updated_at': datetime.now(timezone.utc) + timedelta(seconds=5)})
    assert hs.pull_changes()['ok']
    assert hs.topic_detail_snapshot('purged')['body'] == 'LEGIT NEW topic same slug'
    assert 'purged' not in hs._purged_topic_slugs()


@pytest.mark.parametrize('offset', [-5, 0])
def test_topic_row_not_newer_than_purge_marker_stays_hidden(replica, offset):
    assert hs.pull_changes()['ok']
    stamp = hs._purge_marker_stamp(hs.snapshot_path(), 'purged').timestamp() + offset
    row = {'slug': 'purged', 'title': 'stale', 'body': 'STALE PURGED topic',
           'updated_at': datetime.fromtimestamp(stamp, timezone.utc)}
    parts = hs._write_pulled_tables([row], None, None, read_at=time.time())
    assert parts['topics']['ok']
    assert hs.topic_detail_snapshot('purged') is None
    assert 'purged' in hs._purged_topic_slugs()


def test_failed_recreation_keeps_purge_marker(replica):
    assert hs.pull_changes()['ok']
    with sqlite3.connect(hs.snapshot_path()) as con:
        con.execute("CREATE TRIGGER refuse_recreation BEFORE INSERT ON topics "
                    "BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END")
    row = {'slug': 'purged', 'title': 'new', 'body': 'LEGIT NEW topic',
           'updated_at': datetime.now(timezone.utc) + timedelta(seconds=5)}
    result = hs._write_pulled_tables([row], None, None, read_at=time.time())
    assert not result['topics']['ok']
    assert 'purged' in hs._purged_topic_slugs()
    assert hs.topic_detail_snapshot('purged') is None


@pytest.mark.parametrize('delta_us', [0, 1])
def test_purge_comparison_uses_database_timestamp_precision(replica, delta_us):
    import os

    assert hs.pull_changes()['ok']
    marker = hs._purged_topic_marker(hs.snapshot_path(), 'purged')
    marker.write_text('purged', encoding='utf-8')  # an older marker: no hub time, the file's time stands in
    # This rounds up when represented as a PostgreSQL/Python timestamp.
    stamp_ns = 1_800_000_000_123456789
    os.utime(marker, ns=(stamp_ns, stamp_ns))
    stamp = datetime.fromtimestamp(marker.stat().st_mtime, timezone.utc)
    row = {'slug': 'purged', 'title': 'replacement', 'body': 'NEW topic',
           'updated_at': stamp + timedelta(microseconds=delta_us)}
    result = hs._write_pulled_tables([row], None, None, read_at=time.time())
    assert result['topics']['ok']
    topic = hs.topic_detail_snapshot('purged')
    if delta_us:
        assert topic['body'] == 'NEW topic'
        assert not marker.exists()
    else:
        assert topic is None
        assert marker.exists()
