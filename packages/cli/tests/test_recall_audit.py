"""Regressions reproduced by the offline recall audit, using synthetic data."""
import copy
import json
import os
import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from khipu import close_rules as cr, commitments as co, db, hub_snapshot as hs
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401


def test_close_restatement_preserves_each_repository_pr(hub):
    conn = hub.connect()
    hub.migrate(conn=conn)
    db._TABLE_COLUMNS_CACHE.clear()
    base = 'Prepare the deployment release checklist for the next version'
    words = [base, base + ' once PR #41 merges',
             base + ' once https://github.com/other/repo/pull/41 merges']
    with conn.cursor() as cur:
        for idx, text in enumerate(words):
            cur.execute("INSERT INTO episodes (ts, summary, raw, project, session_id) "
                        "VALUES (now(), %s, '{}'::jsonb, 'acme/widget', 'owed-fake') RETURNING id", (f'fake {idx}',))
            eid = cur.fetchone()[0]
            with patch.object(co, '_has_commitment_embeddings', lambda *a: False):
                n = co.open_from_episode(cur, {'project': 'acme/widget', 'session_id': 'owed-fake',
                     'open_loops': [{'text': text, 'owner': 'assistant'}]}, eid)
                assert n == (1 if idx == 0 else 0)
        cur.execute('SELECT id, text, latest_text, prs, pr_check, status FROM commitments')
        cid, original, latest, prs, check, before = cur.fetchone()
        item = {'text': original, 'latest_text': latest, 'prs': prs, 'pr_check': check}
        refs = cr.item_refs(item, 'acme/widget')
        asked = []
        def state(self, repo, number):
            asked.append((repo, number))
            return ('open' if repo == 'acme/widget' else 'merged', None)
        with patch.object(cr, '_runner_states', lambda *_: {}), \
             patch.object(cr, '_ledger_states', lambda *_: {}), \
             patch.object(cr, '_episode_states', lambda *_: {}), \
             patch.object(cr.GitHub, 'state', state):
            report = cr.run(cur)
        cur.execute('SELECT status, close_rule_key, close_evidence FROM commitments WHERE id = %s', (cid,))
        status, key, evidence = cur.fetchone()
        assert status == 'open', (refs, asked, key, report)
        assert report['closed_pr'] == 0
        assert set(refs) == {('acme/widget', 41), ('other/repo', 41)}
        assert ('acme/widget', 41) in asked


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


@pytest.mark.parametrize('semantic', [False, True])
@pytest.mark.parametrize('filters', [
    {'kind': 'episode', 'project': 'fake/alpha', 'since': '2026-10-15'},
    {'kind': 'episode'},
    {'project': 'fake/alpha'},
    {'since': '2026-10-15'},
    {},
])
def test_recall_graph_expansion_obeys_requested_filters(monkeypatch, semantic, filters):
    from khipu import relevance

    con = sqlite3.connect(':memory:')
    hs._create_schema(con)
    con.execute("INSERT INTO episodes (id,ts,session_id,summary,topics,project,raw) VALUES (1,'2026-10-20T00:00:00+00:00','codex:fake','zebrafish shipment','[\"Shared guide\"]','fake/alpha','{}')")
    con.execute("INSERT INTO topics (slug,title,body,status,created_at,updated_at) VALUES ('shared-guide','Shared guide','unrelated reference','active','2020-01-01','2020-01-01')")
    con.commit()
    monkeypatch.setattr(hs, 'open_snapshot', lambda: con)
    monkeypatch.setattr(hs, 'stale_fields', lambda: {'stale': True})
    monkeypatch.setattr(relevance, 'enabled', lambda: False)
    monkeypatch.setenv('KHIPU_FEATURE_GRAPH_CANDIDATES', '1')
    if semantic:
        monkeypatch.setattr(hs, 'semantic_search_snapshot', lambda *a, **k: [
            {'kind': 'episode', 'id': '1', 'label': 'zebrafish shipment'}])
    result = hs.search_stale_payload('zebrafish', 12, semantic=semantic, **filters)
    expected = {('episode', '1')}
    if not filters:
        expected.add(('topic', 'shared-guide'))
    assert {(r['kind'], r['id']) for r in result['results']} == expected
    con.close()


def test_decision_alias_supersession_through_mcp():
    from khipu import mcp_server as srv, decisions, owner_writes

    con = sqlite3.connect(':memory:')
    con.executescript('''
    CREATE TABLE project_aliases(alias TEXT, project TEXT);
    INSERT INTO project_aliases VALUES('owner/old-name','owner/new-name');
    CREATE TABLE decisions(id INTEGER, project TEXT, text TEXT, rationale TEXT, decided_at TEXT, episode_id INTEGER, session_id TEXT, superseded_by INTEGER, created_at TEXT);
    INSERT INTO decisions VALUES(1,'owner/old-name','Use old implementation',NULL,'2026-10-20',NULL,NULL,NULL,'2026-10-20');
    INSERT INTO decisions VALUES(2,'owner/new-name','Use corrected implementation',NULL,'2026-10-21',NULL,NULL,NULL,'2026-10-21');
    ''')
    class Cursor:
        @property
        def rowcount(self): return self.inner.rowcount
        def __enter__(self): return self
        def __exit__(self,*_): pass
        def execute(self, sql, params=()):
            self.inner=con.execute(sql.replace('%s','?').replace(' FOR UPDATE',''),params)
            return self
        def fetchone(self): return self.inner.fetchone()
        def fetchall(self): return self.inner.fetchall()
    class Conn:
        def __enter__(self): return self
        def __exit__(self,*_): pass
        def cursor(self): return Cursor()

    with patch.object(db,'connect',return_value=Conn()), patch.object(decisions,'_evidence_ready',return_value=False), patch.object(decisions,'_links_ready',return_value=False), patch.object(decisions,'_mirror_to_snapshot'), patch.object(decisions,'_stale_briefs'), patch.object(decisions,'_not_forgotten_clause',return_value=None), patch.object(owner_writes,'annotate',return_value={}), patch.object(owner_writes,'run',side_effect=lambda _tool,args,**_:srv._decisions_update_on_hub(args)), patch.object(owner_writes,'apply_on_hub',side_effect=lambda conn,cur,tool,args,write,echo:write()):
        # Adapt PostgreSQL ANY without replacing the real alias resolver.
        original_execute=Cursor.execute
        def execute(self, sql, params=()):
            if 'project = ANY(%s)' in sql:
                params=list(params)
                group=params.pop(0)
                sql=sql.replace('project = ANY(%s)','project IN ('+','.join('%s' for _ in group)+')')
                params=[*group,*params]
            return original_execute(self,sql,params)
        Cursor.execute=execute
        result=srv._tool_decisions({'project':'owner/new-name','status':'all'})
        assert {r['project'] for r in result['results']} == {'owner/new-name'}
        srv._tool_decisions_update({'action':'supersede','id':1,'by':2})
        assert con.execute('SELECT superseded_by FROM decisions WHERE id=1').fetchone()[0] == 2


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
        def fetchall(self): return [tuple(rows[0].get(c.strip()) for c in self.sql.split('SELECT ', 1)[1].split(' FROM ', 1)[0].split(','))]
    cur = Cur()
    with patch.object(hs, '_pg_columns', lambda cur, table: set(rows[0]) if table == 'commitments' else set()):
        cols, exported = hs._hub_commitments(cur)
    con = sqlite3.connect(path)
    hs._create_schema(con, version=version)
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


@pytest.mark.parametrize('semantic', [False, True])
@pytest.mark.parametrize('filters,changed', [
    ({'project': 'fake/alpha'}, {'project': 'fake/beta'}),
    ({'session_id': 'codex:fake'}, {'session_id': 'codex:other'}),
    ({'harness': 'codex'}, {'harness': 'claude_code'}),
    ({'since': '2026-10-15'}, {'ts': '2020-01-01T00:00:00Z'}),
    ({'until': '2026-10-21'}, {'ts': '2026-10-25T00:00:00Z'}),
    ({'t3_thread': 'thread-1'}, {'raw': '{"t3_thread_id":"other","via":"t3"}'}),
    ({'via': 't3'}, {'raw': '{"t3_thread_id":"thread-1","via":"other"}'}),
])
def test_recall_graph_neighbors_recheck_each_metadata_filter(monkeypatch, semantic, filters, changed):
    from khipu import relevance

    con = sqlite3.connect(':memory:')
    hs._create_schema(con)
    fields = ('id', 'ts', 'session_id', 'harness', 'project', 'summary', 'raw')
    seed = dict(zip(fields, (1, '2026-10-20T00:00:00Z', 'codex:fake:123', 'codex',
                            'fake/alpha', 'zebrafish shipment', '{"t3_thread_id":"thread-1","via":"t3"}')))
    neighbor = dict(seed, id=2, summary='unrelated neighbor', **changed)
    for row in (seed, neighbor):
        con.execute(f"INSERT INTO episodes ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                    [row[f] for f in fields])
    con.commit()
    monkeypatch.setattr(hs, 'open_snapshot', lambda: con)
    monkeypatch.setattr(hs, 'stale_fields', lambda: {'stale': True})
    monkeypatch.setattr(relevance, 'enabled', lambda: False)
    monkeypatch.setattr(hs, '_graph_candidates_snapshot', lambda con, rows: (
        rows + [{'kind': 'episode', 'id': '2', 'label': 'unrelated neighbor'}], []))
    if semantic:
        monkeypatch.setattr(hs, 'semantic_search_snapshot', lambda *a, **k: [
            {'kind': 'episode', 'id': '1', 'label': 'zebrafish shipment'}])
    result = hs.search_stale_payload('zebrafish', 12, semantic=semantic, **filters)
    assert [(r['kind'], r['id']) for r in result['results']] == [('episode', '1')]
    con.close()


def test_close_foreign_only_requirement_does_not_invent_own_repository_pr(hub):
    conn = hub.connect()
    hub.migrate(conn=conn)
    db._TABLE_COLUMNS_CACHE.clear()
    base = 'Prepare the deployment release checklist for the next version'
    with conn.cursor() as cur:
        for idx, text in enumerate((base, base + ' once https://github.com/other/repo/pull/41 merges', base)):
            cur.execute("INSERT INTO episodes (ts, summary, raw, project, session_id) "
                        "VALUES (now(), %s, '{}'::jsonb, 'acme/widget', 'owed-fake') RETURNING id", (str(idx),))
            eid = cur.fetchone()[0]
            with patch.object(co, '_has_commitment_embeddings', lambda *a: False):
                co.open_from_episode(cur, {'project': 'acme/widget', 'session_id': 'owed-fake',
                                          'open_loops': [{'text': text, 'owner': 'assistant'}]}, eid)
        cur.execute('SELECT text, latest_text, prs, pr_check FROM commitments')
        item = dict(zip(('text', 'latest_text', 'prs', 'pr_check'), cur.fetchone()))
        assert cr.item_refs(item, 'acme/widget') == [('other/repo', 41)]
