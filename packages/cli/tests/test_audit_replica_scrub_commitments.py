"""Expected invariant: publishing an imported replica cannot serve forgotten content.
Synthetic IDs/text only. No hub access, no source changes.
"""
import sqlite3

import pytest
from khipu import hub_snapshot as hs, offline_reads, tombstones


@pytest.fixture
def replica(tmp_path, monkeypatch):
    monkeypatch.setenv('KHIPU_DATA_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('KHIPU_KEYCHAIN', '0')
    monkeypatch.setenv('KHIPU_TEST', '1')
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'native'))
    import psycopg
    monkeypatch.setattr(psycopg, 'connect', lambda *a, **kw: pytest.fail('Network forbidden'))
    return tmp_path


def test_import_scrubs_episode_but_serves_its_forgotten_commitment(replica):
    # The registry already remembers episode 7, e.g. from an earlier forget.
    tombstones.publish_episode_ids({7})
    staged = hs.staging_path()
    with sqlite3.connect(staged) as con:
        hs._create_schema(con)
        con.executemany('INSERT INTO episodes(id, summary) VALUES (?, ?)',
                        [(7, 'FORGOTTEN synthetic source'), (8, 'live control')])
        con.executemany("INSERT INTO commitments(id,text,project,opened_episode,status,owner,kind) "
                        "VALUES (?, ?, 'scratch', ?, 'open', 'user', 'task')",
                        [(70, 'FORGOTTEN synthetic obligation', 7), (80, 'live control obligation', 8)])
        con.executemany("INSERT INTO memory_embeddings(profile,kind,ref,chunk_idx,chunk_text) "
                        "VALUES ('synthetic', 'commitment', ?, 0, ?)",
                        [('70', 'FORGOTTEN synthetic obligation'), ('80', 'live control obligation')])
    result = hs.publish_imported_snapshot(staged)
    assert result['ok'] and result['scrubbed'] == 1, result
    with sqlite3.connect(hs.snapshot_path()) as con:
        assert con.execute('SELECT id FROM episodes').fetchall() == [(8,)]
        # A restored replica carries no fact row (design 6.2): its facts come
        # with the next copy from the hub; this test has no hub, so it
        # serves no commitment.
        assert con.execute('SELECT id FROM commitments').fetchall() == []
        assert con.execute("SELECT ref FROM memory_embeddings WHERE kind='commitment'").fetchall() == []
    # Public offline owed surface: the hidden state is said plainly.
    served = offline_reads.owed('scratch', 'open', 20)
    assert served is not None and served.get("facts_hidden"), served
    ids = [r['id'] for r in served['results']]
    print(f'import={result}; offline owed ids={ids}')
    assert 70 not in ids, 'privacy invariant: imported replica must not serve forgotten commitment 70'
