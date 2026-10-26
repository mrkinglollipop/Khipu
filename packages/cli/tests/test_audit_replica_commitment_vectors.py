"""A delayed embedding publication cannot restore a forgotten commitment."""
import sqlite3

import pytest

from khipu import hub_snapshot as hs
from khipu.embed import _md5


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
    # The publication shape since 2026-10-24: each row carries the wording
    # it was built from (design section 6, fourth Astra review #1).
    rows = [dict(profile='synthetic', kind='commitment', ref=str(cid), chunk_idx=0,
                 chunk_text=text, embedding=[1.0, 0.0], fact_wording=_md5(text))
            for cid, text in [(70, 'FORGOTTEN synthetic obligation'), (80, 'live obligation')]]
    result = hs.upsert_embeddings(rows)
    assert result['ok'], result
    with sqlite3.connect(path) as con:
        retained = con.execute("SELECT ref FROM memory_embeddings WHERE kind='commitment' ORDER BY ref").fetchall()
    assert ('70',) not in retained, 'delayed publication recreated a non-live commitment vector'
    # Since phase 7B a fact vector comes only with the fact copy (design
    # 6.2): no publication outside it writes one, the live control's either.
    assert retained == []


def test_open_standalone_commitment_keeps_its_vector(tmp_path, monkeypatch):
    """The copy's vector check: an open commitment with no opening episode
    keeps a vector built from its wording."""
    monkeypatch.setenv('KHIPU_DATA_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('KHIPU_KEYCHAIN', '0')
    import psycopg
    monkeypatch.setattr(psycopg, 'connect', lambda *a, **kw: pytest.fail('Network forbidden'))
    path = hs.snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = 'standalone synthetic obligation'
    read = {"commitments": (["id", "text", "status", "opened_episode"], [(90, text, 'open', None)]),
            "decisions": [], "deliverables": [], "incarnation": "1:1:1", "snapshot": "1:1:",
            "vector_keys": {("synthetic", "90", 0): ("2026-10-23T00:00:00+00:00", _md5(text))},
            "vectors": [dict(profile='synthetic', kind='commitment', ref='90', chunk_idx=0, chunk_text=text,
                             embedding=[1.0, 0.0], built_at="2026-10-23T00:00:00+00:00",
                             fact_wording=_md5(text))]}
    with sqlite3.connect(path) as con:
        hs._create_schema(con)
        counts = hs._write_copy(con, {"fingerprint": "f", "destination": "d", "held_vectors": {}}, read)
    assert counts["vectors"] == 1, counts
    with sqlite3.connect(path) as con:
        retained = con.execute("SELECT ref FROM memory_embeddings WHERE kind='commitment'").fetchall()
    assert retained == [('90',)], 'an open standalone commitment is valid without an opening episode'
