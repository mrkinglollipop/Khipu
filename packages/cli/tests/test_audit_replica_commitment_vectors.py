"""A delayed embedding publication cannot restore a forgotten commitment."""
import sqlite3

import pytest

from khipu import hub_snapshot as hs


@pytest.mark.parametrize('state', ['forgotten', 'pending', 'closed', 'missing_parent', 'missing_commitment'])
def test_delayed_commitment_vector_requires_live_open_parent(tmp_path, monkeypatch, state):
    monkeypatch.setenv('KHIPU_DATA_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('KHIPU_KEYCHAIN', '0')
    import psycopg
    monkeypatch.setattr(psycopg, 'connect', lambda *a, **kw: pytest.fail('Network forbidden'))
    path = hs.snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        hs._create_schema(con)
        con.executemany('INSERT INTO episodes(id,summary) VALUES (?,?)',
                        [(7, 'synthetic target'), (8, 'live control')])
        con.executemany("INSERT INTO commitments(id,text,status,opened_episode) VALUES (?,?,'open',?)",
                        [(70, 'FORGOTTEN synthetic obligation', 7), (80, 'live obligation', 8)])
        if state == 'closed':
            con.execute("UPDATE commitments SET status='closed' WHERE id=70")
        elif state == 'missing_parent':
            con.execute('DELETE FROM episodes WHERE id=7')
        elif state == 'missing_commitment':
            con.execute('DELETE FROM commitments WHERE id=70')
    if state == 'forgotten':
        assert hs.forget_episode_in_snapshot(7)['ok']
    elif state == 'pending':
        assert hs._queue_erasure(7)
    rows = [dict(profile='synthetic', kind='commitment', ref=str(cid), chunk_idx=0,
                 chunk_text=text, embedding=[1.0, 0.0])
            for cid, text in [(70, 'FORGOTTEN synthetic obligation'), (80, 'live obligation')]]
    result = hs.upsert_embeddings(rows)
    assert result['ok'], result
    with sqlite3.connect(path) as con:
        retained = con.execute("SELECT ref FROM memory_embeddings WHERE kind='commitment' ORDER BY ref").fetchall()
    assert retained == [('80',)], 'delayed publication recreated a non-live commitment vector'


def test_open_standalone_commitment_keeps_its_vector(tmp_path, monkeypatch):
    monkeypatch.setenv('KHIPU_DATA_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('KHIPU_KEYCHAIN', '0')
    import psycopg
    monkeypatch.setattr(psycopg, 'connect', lambda *a, **kw: pytest.fail('Network forbidden'))
    path = hs.snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        hs._create_schema(con)
        con.execute("INSERT INTO commitments(id,text,status,opened_episode) "
                    "VALUES (90,'standalone synthetic obligation','open',NULL)")
    result = hs.upsert_embeddings([
        dict(profile='synthetic', kind='commitment', ref='90', chunk_idx=0,
             chunk_text='standalone synthetic obligation', embedding=[1.0, 0.0])])
    assert result['ok'], result
    with sqlite3.connect(path) as con:
        retained = con.execute("SELECT ref FROM memory_embeddings WHERE kind='commitment'").fetchall()
    assert retained == [('90',)], 'an open standalone commitment is valid without an opening episode'
