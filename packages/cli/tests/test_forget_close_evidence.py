import json
import pytest
from khipu import commitments, close_rules, config, db, forget
from tests.hermetic_pg import hub, pg_cluster
from tests.fact_seed import announce

@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    monkeypatch.setenv('KHIPU_DATA_DIR',str(tmp_path/'data'))
    monkeypatch.setenv('KHIPU_CAPTURE_HOME',str(tmp_path/'capture'))
    monkeypatch.setenv('KHIPU_OUTBOX',str(tmp_path/'outbox'))
    monkeypatch.setenv('KHIPU_KEYCHAIN','0')
    db._TABLE_COLUMNS_CACHE.clear()
    config.set_choice_setting('owed_pr_check_github','off')

@pytest.mark.parametrize("field", ["summary", "decisions", "closed_loops"])
def test_forgotten_pr_evidence_cannot_close_kept_commitment(hub,tmp_path,field):
    conn=hub.connect(); hub.migrate(conn=conn); cur=conn.cursor()
    def episode(n,summary):
        p={'ts':f'2026-10-2{n}T09:00:00Z','summary':summary,'project':'audit/forgotten-evidence',
           'harness':'codex','session_id':f'codex:separate-{n}','transcript_range':'0:100'}
        announce(cur)
        cur.execute('INSERT INTO episodes (ts,summary,project,harness,session_id,transcript_range,raw) VALUES (%s::timestamptz,%s,%s,%s,%s,%s,%s::jsonb) RETURNING id',
                    (p['ts'],summary,p['project'],p['harness'],p['session_id'],p['transcript_range'],json.dumps(p)))
        return cur.fetchone()[0],p
    origin,p=episode(1,'Kept source for two still pending changes')
    p['open_loops']=[{'text':text,'owner':'assistant','kind':'blocker'} for text in ('Implement the two fixes before PR #42 can merge','Publish revised deployment documentation after PR #43 receives approval')]
    assert commitments.open_from_episode(cur,p,origin)==2
    merge_line = 'Merge pull request #42 from audit/private-branch'
    gone, gone_payload = episode(2, merge_line if field == 'summary' else 'A later capture')
    if field != 'summary':
        gone_payload[field] = [merge_line]
        cur.execute('UPDATE episodes SET raw=%s::jsonb WHERE id=%s', (json.dumps(gone_payload),gone))
    conn.commit()
    forgotten=forget.forget_episode(cur,gone,memory_root=tmp_path/'memory');conn.commit()
    assert forgotten['ok'] and forgotten['soft_deleted'] and forgotten['commitments_closed']==0
    print('forgotten source:',gone,'cascade did not touch kept items:',forgotten['commitments_closed'])
    cur.execute('SELECT id,text,status FROM commitments ORDER BY id')
    print('before nightly:',cur.fetchall())
    evidence=close_rules._episode_states(cur,p['project'],origin)
    print('nightly source evidence:',evidence)
    assert (None,42) not in evidence
    result=close_rules.run(cur);conn.commit()
    cur.execute('SELECT text,status,closed_episode,close_reason FROM commitments ORDER BY id')
    rows=cur.fetchall();print('nightly result:',result);print('after nightly:',rows)
    assert rows[0][1]=='open' and rows[0][2] is None
    assert rows[1][1]=='open',rows
    assert result['github_asked']==0 and result['closed_pr']==0
