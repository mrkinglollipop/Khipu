"""Regressions reproduced by the offline recall audit, using synthetic data."""
import copy
import json
import os
import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from khipu import commitments as co, hub_snapshot as hs
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.replica_copy import certify


def test_capture_resume_prefers_current_home_with_old_file_present(tmp_path):
    base = tmp_path
    from khipu import session_capture as sc

    old_home, new_home = base/'first', base/'second'
    sid = 'invented-resumed-session'
    old = old_home/'projects'/'invented-project'/f'{sid}.jsonl'
    current = new_home/'projects'/'invented-project'/f'{sid}.jsonl'
    def user(text):
        return json.dumps({'type':'user','message':{'role':'user','content':text}})+'\n'
    prior = user('An already captured invented conversation. ' * 4)
    fresh = user('Remember this: the invented project now uses cobalt widgets. ' * 6)
    for path, text in ((old, prior), (current, prior+fresh)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    with patch.dict(os.environ, {'KHIPU_ISOLATED_DIR':str(base/'state'),
                                'CLAUDE_CONFIG_DIR':str(new_home)}), \
         patch.object(sc, 'ancestor_pids', return_value=[]):
        sc.save_state('claude_code', sid, {'offset':len(prior.encode()), 'last_ts':0,
                                        'captures':1, 'transcript_path':str(old)})
        payload = {'session_id':sid, 'transcript_path':str(old), 'cwd':str(base),
                   'hook_event_name':'SessionEnd'}
        outcome = sc.hook_main(json.dumps(payload), 'claude_code')
        jobs = sc.queued_jobs()
        stored = sc.load_state('claude_code',sid)
        assert not outcome.get('error'), outcome
        assert outcome['new_turns'] == 1 and len(jobs) == 1, (outcome, jobs)
        assert stored['transcript_path'] == str(current), stored
        assert stored['seen_end'] == len(current.read_bytes())
        assert stored['offset'] == len(current.read_bytes())
        assert not sc._read_beat('claude_code').get('transcript_missing')






@pytest.mark.parametrize('version', [6, 8, hs.SNAPSHOT_SCHEMA_VERSION])
def test_owed_offline_preserves_close_rule_fields(tmp_path, version):
    path = tmp_path / 'owed.sqlite'
    from khipu import offline_reads as reads
    now = datetime.now(timezone.utc)
    rows = [dict(id=1, text='Matt to approve the export format after PR #41', project='acme/widget',
                 owner='user', kind='followup', opened_episode=None, opened_at=now, status='open',
                 origin='conversation', linked_ids=[], prs=[41, 42],
                 settle_suggestion={'looks_done': True, 'rule': 'pr', 'reason': 'both PRs merged',
                                    'key': 'pr:41=merged,42=merged', 'evidence': {'prs': {'41': 'merged', '42': 'merged'}}},
                 pr_check={'waiting': {}, 'states': {'41': 'merged', '42': 'merged'}})]
    online_done = co.page_owed(copy.deepcopy(rows), looks_done=True)['total']
    online_pr = co.page_owed(copy.deepcopy(rows), pr=42)['total']
    # Exactly the current dump/pull selects columns, no fabricated snapshot columns.
    class Cur:
        def execute(self, sql): self.sql = sql
        def fetchall(self): return [tuple(rows[0].get(c.strip().rsplit('.', 1)[-1]) for c in self.sql.split('SELECT ', 1)[1].split(' FROM ', 1)[0].split(','))]
    cur = Cur()
    with patch.object(hs, '_pg_columns', lambda cur, table: set(rows[0]) if table == 'commitments' else set()):
        cols, exported = hs._hub_commitments(cur)
    con = sqlite3.connect(path)
    hs._create_schema(con, version=version)
    certify(con)
    hs._write_commitment_rows(con, cols, exported)
    con.commit()
    con.close()
    with patch.object(hs, 'snapshot_path', lambda: path):
        offline = reads.owed_rows('acme/widget', 'open')
        offline_done = co.page_owed(copy.deepcopy(offline), looks_done=True)['total']
        offline_pr = co.page_owed(copy.deepcopy(offline), pr=42)['total']
    assert (online_done, online_pr) == (1, 1)
    assert (offline_done, offline_pr) == (online_done, online_pr)
    assert offline[0]['prs'] == [41, 42]
    assert offline[0]['settle_suggestion'] == rows[0]['settle_suggestion']
    assert offline[0]['pr_check'] == rows[0]['pr_check']


def test_topic_event_date_filtered_before_enrichment(hub, monkeypatch):
    from khipu import embed, db, hub_snapshot, features
    monkeypatch.setattr(db, 'connect', lambda **kw: hub.connect())
    monkeypatch.setattr(hub_snapshot, 'try_hub_connect', lambda **kw: hub.connect())
    monkeypatch.setattr(features, 'enabled', lambda name: False)
    conn = hub.connect()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO topics (slug,title,body,created_at,updated_at,event_at) VALUES ('zebrafish','Zebrafish','zebrafish reference','2026-10-01','2026-10-20','2026-10-15')")
    conn.commit()
    unfiltered = embed.hybrid_search('zebrafish', mode='literal', kind='topic', include_libraries=False)
    filtered = embed.hybrid_search('zebrafish', mode='literal', kind='topic', until='2026-10-16', include_libraries=False)
    assert [r['id'] for r in unfiltered['results']] == ['zebrafish']
    assert unfiltered['results'][0]['ts'].startswith('2026-10-15')
    assert [r['id'] for r in filtered['results']] == ['zebrafish']




