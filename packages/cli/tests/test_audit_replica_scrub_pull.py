"""Real disposable Postgres -> refresh scrub -> incremental pull -> public readers."""
import json
import sqlite3

import pytest
from tests import fact_seed
from tests.hermetic_pg import hub, pg_cluster
from khipu import db, hub_snapshot as hs, offline_reads, tombstones


@pytest.mark.parametrize('refresh_first', [True, False])
def test_pull_cannot_restore_derivatives_of_scrubbed_source(hub, tmp_path, monkeypatch, refresh_first):
    monkeypatch.setenv('KHIPU_DATA_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'capture'))
    monkeypatch.setenv('KHIPU_KEYCHAIN', '0')
    monkeypatch.setattr(db, 'connect', lambda **kw: hub.connect())
    monkeypatch.setattr(hs, 'try_hub_connect', lambda **kw: hub.connect())
    hub.migrate()
    payload = {'ts': '2026-10-20T12:00:00Z', 'summary': 'PRIVATE SOURCE',
               'harness': 'codex', 'session_id': 'codex:fake-forgotten-source',
               'transcript_range': '0:10'}
    # A durable local forget followed by a hub backup/other-Mac duplicate.
    tombstones.publish_payloads([payload])
    assert tombstones.is_forgotten(payload)
    if not refresh_first:
        assert hs.refresh()['ok']
    with hub.connect() as con:
        eid = con.execute("INSERT INTO episodes(ts,summary,raw,session_id,harness,transcript_range) "
                          "VALUES (%s,%s,%s::jsonb,%s,%s,%s) RETURNING id",
                          (payload['ts'], payload['summary'], json.dumps(payload),
                           payload['session_id'], payload['harness'], payload['transcript_range'])).fetchone()[0]
        cid = con.execute("INSERT INTO commitments(text,opened_episode,content_hash) VALUES (%s,%s,%s) "
                          "RETURNING id", ('PRIVATE COMMITMENT', eid, 'fake-commitment-hash')).fetchone()[0]
        did = con.execute("INSERT INTO decisions(text,episode_id) VALUES (%s,%s) RETURNING id",
                          ('PRIVATE DECISION', eid)).fetchone()[0]
        # Fact provenance (0047): raw-seeded facts get their claims.
        fact_seed.adopt(con.cursor(), 'commitment', cid)
        fact_seed.adopt(con.cursor(), 'decision', did)
    if refresh_first:
        refreshed = hs.refresh()
        assert refreshed['ok'], refreshed
        assert refreshed['counts']['forgotten_scrubbed'] == 1
    assert hs.episode_detail_snapshot(eid) is None
    assert offline_reads.owed_rows(None, 'open') == []
    assert offline_reads.decisions(None, None, 'standing', 20)['results'] == []
    pulled = hs.pull_changes(include_ledger=False)
    assert pulled['ok'], pulled
    owed = offline_reads.owed_rows(None, 'open')
    decisions = offline_reads.decisions(None, None, 'standing', 20)['results']
    print('PULL', pulled)
    print('VISIBLE OWED', [r['text'] for r in owed])
    print('VISIBLE DECISIONS', [r['text'] for r in decisions])
    assert not owed and not decisions, 'Pull restored forgotten derivative text after successful scrub'
    with sqlite3.connect(hs.snapshot_path()) as con:
        assert con.execute('SELECT text FROM commitments').fetchall() == []
        assert con.execute('SELECT text FROM decisions').fetchall() == []


def test_rejected_source_keeps_missing_and_unlinked_controls(tmp_path, monkeypatch):
    monkeypatch.setenv('KHIPU_DATA_DIR', str(tmp_path))
    with sqlite3.connect(hs.snapshot_path()) as con:
        hs._create_schema(con)
        con.execute("INSERT INTO episodes(id,summary,deleted_at) VALUES (1,'','2026-10-20')")
        rows = [{'id': eid, 'text': text, 'episode_id': source} for eid, text, source in
                [(1, 'refused', 1), (2, 'partial replica', 2), (3, 'unlinked', None)]]
        assert hs._write_decision_rows(con, rows) == 2
        assert hs._write_commitment_rows(con, ['id', 'text', 'opened_episode', 'status'],
                                        [(r['id'], r['text'], r['episode_id'], 'open') for r in rows]) == 2
    assert {r['text'] for r in offline_reads.owed_rows(None, 'open')} == {'unlinked', 'partial replica'}
    assert {r['text'] for r in offline_reads.decisions(None, None, 'standing', 20)['results']} == {
        'partial replica', 'unlinked'}
