"""Expected invariant: a full refresh must not serve a source the registry refused."""
from datetime import datetime, timezone
import sqlite3

import pytest
from khipu import hub_snapshot as hs, offline_reads, tombstones


class FullHub:
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def cursor(self): return self
    def execute(self, sql, params=()):
        if sql == 'SELECT now()':
            self.rows = [(datetime.now(timezone.utc),)]
            return
        columns, table = sql[len('SELECT '):].split(' FROM ', 1)
        table = table.split()[0]
        rows = {
            'episodes': [{'id': 7, 'summary': 'FORGOTTEN source'}, {'id': 8, 'summary': 'live source'}],
            'commitments': [
                {'id': 70, 'text': 'FORGOTTEN synthetic obligation', 'project': 'scratch',
                 'opened_episode': 7, 'status': 'open', 'owner': 'user', 'kind': 'task'},
                {'id': 80, 'text': 'live control obligation', 'project': 'scratch',
                 'opened_episode': 8, 'status': 'open', 'owner': 'user', 'kind': 'task'}],
        }.get(table, [])
        self.rows = [tuple(row.get(c) for c in columns.split(', ')) for row in rows]
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
    monkeypatch.setattr(hs, 'try_hub_connect', lambda **kw: FullHub())
    result = hs.refresh()
    assert result['ok'] and result['counts']['forgotten_scrubbed'] == 1, result
    with sqlite3.connect(hs.snapshot_path()) as con:
        assert con.execute('SELECT id FROM episodes').fetchall() == [(8,)]
    served = offline_reads.owed('scratch', 'open', 20)
    assert served is not None and any(r['id'] == 80 for r in served['results']), served
    ids = [r['id'] for r in served['results']]
    print(f'refresh ok={result["ok"]}; scrubbed={result["counts"]["forgotten_scrubbed"]}; offline owed ids={ids}')
    assert ids == [80], 'full refresh must not serve forgotten commitment 70'
