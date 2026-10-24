"""Expected invariant: a full refresh must not serve a source the registry refused.

Since the owner ruling of 2026-10-24 (the wait rule, design section 6) a
replica keeps no fact row until the hub shows every confirmed refusal's
episode id tombstoned (a forgotten episode id confirms itself), and serves
the live facts once the hub's forget has landed."""
from datetime import datetime, timezone
import sqlite3

import pytest
from khipu import hub_snapshot as hs, offline_reads, tombstones


class FullHub:
    def __init__(self, deleted=False):
        self.data = {
            'episodes': [{'id': 7, 'summary': 'FORGOTTEN source',
                          'deleted_at': datetime.now(timezone.utc) if deleted else None},
                         {'id': 8, 'summary': 'live source'}],
            'commitments': [
                {'id': 70, 'text': 'FORGOTTEN synthetic obligation', 'project': 'scratch',
                 'opened_episode': 7, 'status': 'open', 'owner': 'user', 'kind': 'task'},
                {'id': 80, 'text': 'live control obligation', 'project': 'scratch',
                 'opened_episode': 8, 'status': 'open', 'owner': 'user', 'kind': 'task'}],
        }
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def cursor(self): return self
    def execute(self, sql, params=()):
        if sql == 'SELECT now()':
            self.rows = [(datetime.now(timezone.utc),)]
            return
        columns, table = sql[len('SELECT '):].split(' FROM ', 1)
        table = table.split()[0]
        rows = self.data.get(table, [])
        # The wait rule's read: which confirmed ids the hub shows tombstoned.
        if table == 'episodes' and 'deleted_at IS NOT NULL' in sql:
            rows = [r for r in rows if r.get('deleted_at') is not None]
        elif 'deleted_at IS NULL' in sql:
            rows = [r for r in rows if r.get('deleted_at') is None]
        if 'id = ANY(%s)' in sql:
            rows = [r for r in rows if r.get('id') in params[0]]
        cols = [c.strip().removeprefix('e.') for c in columns.split(', ')]
        self.rows = [tuple(row.get(c) for c in cols) for row in rows]
    def fetchone(self): return self.rows[0] if self.rows else None
    def fetchall(self): return self.rows


def test_full_refresh_scrubs_episode_but_serves_forgotten_commitment(tmp_path, monkeypatch):
    monkeypatch.setenv('KHIPU_DATA_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('KHIPU_TEST', '1')
    import psycopg
    monkeypatch.setattr(psycopg, 'connect', lambda *a, **kw: pytest.fail('Network forbidden'))
    tombstones.publish_episode_ids({7})
    columns = {'episodes': {'id', 'summary', 'deleted_at'},
               'commitments': {'id', 'text', 'project', 'opened_episode', 'status', 'owner', 'kind'}}
    monkeypatch.setattr(hs, '_pg_columns', lambda cur, table: columns.get(table, set()))
    # The hub still holds episode 7 live: no fact row until it catches up.
    monkeypatch.setattr(hs, 'try_hub_connect', lambda **kw: FullHub())
    result = hs.refresh()
    assert result['ok'] and result['counts']['forgotten_scrubbed'] == 1, result
    with sqlite3.connect(hs.snapshot_path()) as con:
        assert con.execute('SELECT id FROM episodes').fetchall() == [(8,)]
    waiting = offline_reads.owed('scratch', 'open', 20)
    assert waiting is not None and waiting['results'] == [] and waiting['facts_rebuilding'], waiting
    # The hub's forget lands: the live commitment is served, 70 never.
    monkeypatch.setattr(hs, 'try_hub_connect', lambda **kw: FullHub(deleted=True))
    result = hs.refresh()
    assert result['ok'], result
    served = offline_reads.owed('scratch', 'open', 20)
    assert served is not None and any(r['id'] == 80 for r in served['results']), served
    ids = [r['id'] for r in served['results']]
    assert ids == [80], 'full refresh must not serve forgotten commitment 70'
