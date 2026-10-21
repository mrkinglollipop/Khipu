"""Import-time routing metadata and search isolation, using synthetic notes only."""
import copy
import json
import socket
import sqlite3
import statistics
import subprocess
import time

import pytest

from khipu import hub_snapshot as hs, notes, recall_prompt as rp, routing_scanner as scanner


@pytest.fixture(autouse=True)
def hermetic(monkeypatch, tmp_path):
    from khipu import db
    monkeypatch.setenv('KHIPU_DATA_DIR', str(tmp_path / 'data'))

    def forbidden(*a, **kw):
        raise AssertionError('Unexpected external operation')

    monkeypatch.setattr(db, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)


def imported(tmp_path, body):
    path = tmp_path / 'synthetic.md'
    path.write_text(body, encoding='utf-8')
    topic = notes._note_topic_dict(path, project='synthetic/project')
    assert path.read_text(encoding='utf-8') == body
    return topic


def replica(monkeypatch, topic):
    """Use the actual full replica writer, including legacy-row derivation."""
    con = sqlite3.connect(':memory:')
    hs._create_schema(con)
    cols = [c for c in hs._TOPIC_COLS if c in topic]
    monkeypatch.setattr(hs, '_pg_columns', lambda *a: set(cols))

    class Cursor:
        def execute(self, sql):
            assert sql == f"SELECT {', '.join(cols)} FROM topics ORDER BY slug"

        def fetchall(self):
            return [tuple(topic[c] for c in cols)]

    assert hs._insert_topics(Cursor(), con) == 1
    return con


def no_body_reads(con):
    def authorize(action, table, column, *args):
        if action == sqlite3.SQLITE_READ and table == 'topics' and column == 'body':
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    con.set_authorizer(authorize)


def test_import_recomputes_only_boolean(tmp_path):
    for body, expected in [
        ('Codex is good for builds.', False),
        ('Synthetic context. ' * 400 + '\nCodex is paused until 2026-10-19', True),
        ('Codex is good for builds.', False),
    ]:
        topic = imported(tmp_path, body)
        assert topic['frontmatter']['_routing_state_claim'] is expected
        assert topic['body'] == body
        assert body not in json.dumps(topic['frontmatter'])


@pytest.mark.parametrize('legacy', [False, True])
def test_replica_write_and_search_only_carry_boolean(monkeypatch, tmp_path, caplog, capsys, legacy):
    body = 'Synthetic context. ' * 3400 + '\nCodex is paused until 2026-10-19\nBODY_ONLY_CANARY'
    topic = imported(tmp_path, body)
    if legacy:
        topic['frontmatter'].pop('_routing_state_claim')
    before = copy.deepcopy(topic)
    con = replica(monkeypatch, topic)
    assert topic == before
    stored_body, fm = con.execute('SELECT body, frontmatter FROM topics').fetchone()
    assert stored_body == body  # The existing body column is unchanged.
    assert json.loads(fm) == {**topic['frontmatter'], '_routing_state_claim': True}
    assert 'BODY_ONLY_CANARY' not in fm
    no_body_reads(con)
    calls = []

    def broken(*a, **kw):
        calls.append(True)
        raise RuntimeError(body)

    monkeypatch.setattr(scanner, 'scan_text', broken)
    rows = hs.snapshot_row_metadata(con, [dict(kind='topic', id=topic['slug'], snippet='Synthetic teaser')])
    assert rows[0]['_routing_state_claim'] is True
    assert scanner.SUPERSEDED_MARK in rp.render_block(rows)
    assert not calls, 'Search/render must not rescan an imported body'
    assert 'body' not in rows[0] and 'frontmatter' not in rows[0]
    assert 'BODY_ONLY_CANARY' not in json.dumps(rows)
    captured = capsys.readouterr()
    assert 'BODY_ONLY_CANARY' not in caplog.text + captured.out + captured.err
    con.close()


def test_scanner_failure_never_logs_body_or_blocks_import(monkeypatch, tmp_path, caplog, capsys):
    body = 'BODY_ONLY_CANARY\nCodex is paused until 2026-10-19'

    def broken(*a, **kw):
        raise RuntimeError(body)

    monkeypatch.setattr(scanner, 'scan_text', broken)
    topic = imported(tmp_path, body)
    assert topic['frontmatter']['_routing_state_claim'] is False
    topic['frontmatter'].pop('_routing_state_claim')
    con = replica(monkeypatch, topic)
    no_body_reads(con)
    rows = hs.snapshot_row_metadata(con, [dict(kind='topic', id=topic['slug'], snippet='Synthetic teaser')])
    assert rows[0]['_routing_state_claim'] is False
    assert topic['slug'] in rp.render_block(rows)
    assert 'BODY_ONLY_CANARY' not in json.dumps(rows)
    captured = capsys.readouterr()
    assert 'BODY_ONLY_CANARY' not in caplog.text + captured.out + captured.err
    con.close()


@pytest.mark.parametrize('flag', [True, False, None, 'false', {'body': 'BODY_ONLY_CANARY'}])
def test_hub_metadata_only_carries_boolean(monkeypatch, flag):
    from khipu import embed
    monkeypatch.setattr(embed, '_episode_schema_flags', lambda *a: {})

    class Cursor:
        def execute(self, sql, params):
            assert "frontmatter->'_routing_state_claim'" in sql
            assert 'body' not in sql

        def fetchall(self):
            return [('note:synthetic', None, 'active', None, None, None, flag)]

    def broken(*a, **kw):
        raise RuntimeError('Scanner must not run on search')

    monkeypatch.setattr(scanner, 'scan_text', broken)
    rows = embed._apply_search_filters(Cursor(), [dict(kind='topic', id='note:synthetic', snippet='Synthetic teaser')])
    if type(flag) is bool:
        assert rows[0]['_routing_state_claim'] is flag
        assert (scanner.SUPERSEDED_MARK in rp.render_block(rows)) is flag
    else:
        assert '_routing_state_claim' not in rows[0]
    assert 'BODY_ONLY_CANARY' not in json.dumps(rows)


def test_search_metadata_latency_is_independent_of_body(monkeypatch):
    con = sqlite3.connect(':memory:')
    con.execute('CREATE TABLE topics (slug TEXT PRIMARY KEY, body TEXT, frontmatter TEXT)')
    report = []
    for size in [500, 4096, 65536]:
        body = ('Synthetic capability evidence.\n' * 2200)[:size]
        con.executemany('INSERT OR REPLACE INTO topics VALUES (?, ?, ?)', [
            (f'note:s-{i}', body, json.dumps({'_routing_state_claim': True})) for i in range(50)
        ])
        no_body_reads(con)
        for count in [3, 8, 12, 50]:
            rows = [dict(kind='topic', id=f'note:s-{i}', snippet='Synthetic teaser') for i in range(count)]
            samples = []
            for _ in range(20):
                start = time.perf_counter()
                out = hs.snapshot_row_metadata(con, rows)
                samples.append((time.perf_counter() - start) * 1000)
            assert len(out) == count and all(r['_routing_state_claim'] is True for r in out)
            median = statistics.median(samples)
            report.append(dict(body_bytes=size, hits=count, median_ms=round(median, 3)))
            assert median < 5, f'Search metadata exceeded a few ms: {report[-1]}'
        con.set_authorizer(None)
    print('SEARCH_METADATA_LATENCY ' + json.dumps(report))
    con.close()
