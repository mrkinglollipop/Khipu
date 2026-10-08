"""Slice C fixtures are local SQLite files; hub and extraction are fakes."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

from khipu import capture, embed, hub_snapshot as hs, mcp_server as mcp
from khipu import recall_prompt as rp, session_capture as sc, t3
from tests.fixtures.t3 import THREAD, handoff_wrapper


@pytest.fixture
def t3_db(tmp_path, monkeypatch):
    path = tmp_path / 'statev2.sqlite'
    con = sqlite3.connect(path)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('CREATE TABLE orchestration_v2_projection_provider_threads '
                '(thread_id TEXT, payload_json TEXT, updated_at TEXT)')
    for sid in ('claude-one', 'codex-two', 'claude-secondary'):
        con.execute('INSERT INTO orchestration_v2_projection_provider_threads VALUES (?, ?, ?)',
                    (THREAD, json.dumps({'nativeThreadRef': {'nativeId': sid}}), '2026-10-08'))
    con.execute('INSERT INTO orchestration_v2_projection_provider_threads VALUES (?, ?, ?)',
                ('thread:delegated-task:command%3Achild',
                 json.dumps({'nativeThreadRef': {'nativeId': 'child'}}), '2026-10-08'))
    con.commit()
    con.close()
    monkeypatch.setenv('KHIPU_T3_DB', str(path))
    return path


@pytest.mark.parametrize('sid', ['claude-one', 'codex-two', 'claude-secondary'])
def test_mapping_across_providers_is_read_only(t3_db, sid):
    before = t3_db.read_bytes()
    assert t3.thread_for_session(sid) == THREAD
    assert t3_db.read_bytes() == before


def test_mapping_with_t3_closed_leaves_no_sidecar_files(t3_db):
    assert not Path(f'{t3_db}-wal').exists()
    assert t3.thread_for_session('claude-one') == THREAD
    assert sorted(p.name for p in t3_db.parent.iterdir()) == ['statev2.sqlite']


def test_mapping_child_and_miss(t3_db):
    assert t3.thread_for_session('child') == 'thread:delegated-task:command%3Achild'
    assert t3.thread_for_session('absent') is None


@pytest.mark.parametrize('payload', ['{', '{}', '[]', 'null', '{"nativeThreadRef":[]}'])
def test_mapping_malformed_row_does_not_poison_hits(t3_db, payload):
    with sqlite3.connect(t3_db) as con:
        con.execute('INSERT INTO orchestration_v2_projection_provider_threads VALUES (?, ?, ?)',
                    ('bad-thread', payload, '2026-10-09'))
    assert t3.thread_for_session('claude-one') == THREAD
    assert t3.thread_for_session('bad') is None


def test_mapping_missing_and_schema_change(tmp_path, monkeypatch):
    path = tmp_path / 'missing.sqlite'
    monkeypatch.setenv('KHIPU_T3_DB', str(path))
    assert t3.thread_for_session('x') is None
    assert not path.exists()
    with sqlite3.connect(path) as con:
        con.execute('CREATE TABLE renamed (id TEXT)')
    assert t3.thread_for_session('x') is None


def test_locked_db_fails_open_quickly(tmp_path, monkeypatch):
    # Rollback-journal EXCLUSIVE is stronger than a normal WAL writer lock.
    path = tmp_path / 'locked.sqlite'
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE orchestration_v2_projection_provider_threads '
                '(thread_id TEXT, payload_json TEXT, updated_at TEXT)')
    con.commit()
    con.execute('BEGIN EXCLUSIVE')
    monkeypatch.setenv('KHIPU_T3_DB', str(path))
    try:
        start = time.monotonic()
        assert t3.thread_for_session('x') is None
        assert time.monotonic() - start < .1
    finally:
        con.rollback()
        con.close()


def test_mapping_wal_writer_does_not_block_reader(t3_db):
    with sqlite3.connect(t3_db) as con:
        con.execute('BEGIN IMMEDIATE')
        con.execute('UPDATE orchestration_v2_projection_provider_threads SET thread_id = ?', ('uncommitted',))
        assert t3.thread_for_session('claude-one') == THREAD
        con.rollback()


def test_hit_and_negative_cache(t3_db):
    st = {}
    with mock.patch.object(t3, 'thread_for_session', wraps=t3.thread_for_session) as lookup:
        assert t3.cache_thread(st, 'claude-one') == THREAD
        assert t3.cache_thread(st, 'claude-one') == THREAD
        assert lookup.call_count == 1
        assert t3.cache_thread(st, 'absent') is None
        assert t3.cache_thread(st, 'absent') is None
        assert lookup.call_count == 2
        st['t3_lookup_at'] -= 301
        assert t3.cache_thread(st, 'absent') is None
        assert lookup.call_count == 3


@pytest.mark.parametrize('thread', [THREAD, 'thread:delegated-task:command%3Ax', 'thread.with.dots'])
def test_handoff_thread_parsing(thread):
    prompt = handoff_wrapper('decide recall budget').replace(THREAD, thread)
    assert t3.handoff_thread(prompt) == thread
    assert t3.handoff_thread(prompt.replace('\n', '\r\n')) == thread


@pytest.mark.parametrize('prompt', ['hello Thread: fake.', 'Context handoff (x):\nProvider context handoff.',
                                   'Context handoff (x):\nProvider context handoff. Thread: . Covered app runs: 1.',
                                   '[Historical user]\nProvider context handoff. Thread: fake.'])
def test_no_thread_header(prompt):
    assert t3.handoff_thread(prompt) is None


@pytest.mark.parametrize('sid,expected', [('claude-one', THREAD), ('codex-two', THREAD), ('absent', None)])
def test_capture_stamp_reaches_persisted_raw(t3_db, tmp_path, monkeypatch, sid, expected):
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'khipu'))
    path = tmp_path / 'transcript.jsonl'
    path.write_text(json.dumps({'type':'user','message':{'role':'user','content':'Implement thread continuity ' * 15}})+'\n')
    env = json.dumps({'session_id':sid,'hook_event_name':'SessionEnd','transcript_path':str(path),'cwd':str(tmp_path)})
    harness = 'codex' if sid.startswith('codex') else 'claude_code'
    with mock.patch('khipu.identity.resolve_repo_root', return_value={}):
        out = sc.hook_main(env, harness=harness)
    assert out['due']
    job = json.loads(sc.queued_jobs()[0].read_text())
    st = sc.load_state(harness, sid)
    assert st['t3_lookup_session'] == sid
    if expected:
        assert (job['t3_thread_id'], job['via']) == (expected, 't3')
    else:
        assert 'via' not in job and 't3_thread_id' not in job
    payloads = []
    with mock.patch('khipu.extract.extract_memory', return_value={'summary':'Thread capture'}), \
         mock.patch('khipu.capture.capture', side_effect=lambda p, **kw: payloads.append(p) or 0), \
         mock.patch('khipu.session_capture.land_transcript_images', return_value={}), \
         mock.patch('khipu.hub_snapshot.sync_decision_changes', return_value={'ok':True}):
        drained = sc.drain()
    assert drained['captured'] == 1
    payload = payloads[0]
    cur = mock.MagicMock()
    cur.rowcount = 1
    with mock.patch('khipu.db.has_columns', return_value=False):
        from khipu.mirror import _upsert_episode
        _upsert_episode(cur, payload)
    raw = json.loads(cur.execute.call_args.args[1][9])
    assert raw.get('t3_thread_id') == expected
    if expected:
        assert raw['via'] == 't3'
    else:
        assert 'via' not in raw


def test_split_and_sweep_stamp_use_same_cached_identity(t3_db, tmp_path, monkeypatch):
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'khipu'))
    monkeypatch.setattr(sc, 'MAX_TRANSCRIPT', 120)
    path = tmp_path / 'tail.jsonl'
    path.write_text('\n'.join(json.dumps({'type':role,'message':{'role':role,'content': 'pending work ' * 15}}) for role in ('user','assistant','user','assistant'))+'\n')
    old = time.time() - 3600
    os.utime(path, (old, old))
    st = {'session_id':'claude-one','offset':0,'last_ts':old-60,'seen_ts':old,
          'cwd':str(tmp_path),'transcript_path':str(path)}
    sc.save_state('claude_code', 'claude-one', st)
    with mock.patch('khipu.identity.resolve_repo_root', return_value={}):
        out = sc.sweep_idle(now=time.time())
    assert out['swept'] == 1
    assert out['jobs'] >= 2
    for p in sc.queued_jobs():
        job = json.loads(p.read_text())
        assert (job['t3_thread_id'], job['via']) == (THREAD, 't3')


@pytest.fixture
def replica(tmp_path, monkeypatch):
    path = tmp_path / 'replica.sqlite'
    con = sqlite3.connect(path)
    hs._create_schema(con)
    for i in range(1, 101):
        raw = json.dumps({'t3_thread_id':THREAD if i <= 2 else 'other', 'via':'t3' if i != 2 else 'fixture'})
        if i == 3: raw = '{bad'
        con.execute("INSERT INTO episodes (id, ts, session_id, summary, raw) VALUES (?, ?, ?, 'recall budget', ?)",
                    (i, f'2026-10-08T00:{i//60:02}:{i%60:02}+00:00', ('claude_code:' if i == 1 else 'codex:')+str(i), raw))
    con.execute("INSERT INTO topics (slug,title,body) VALUES ('recall','recall','recall budget')")
    con.execute("INSERT INTO embedding_profiles (id,provider,model,dim,is_active) VALUES ('test','test','test',2,1)")
    for i in range(1, 101):
        con.execute("INSERT INTO memory_embeddings (profile,kind,ref,chunk_idx,chunk_text,content_hash,embedding) "
                    "VALUES ('test','episode',?,0,'recall budget','hash',?)", (str(i), hs._vector_to_blob([1.,0.])))
    con.commit()
    con.close()
    monkeypatch.setattr(hs, 'snapshot_path', lambda: path)
    return path


@pytest.mark.parametrize('filters,ids', [({'t3_thread':THREAD}, {'1','2'}), ({'via':'fixture'},{'2'}),
                                       ({'t3_thread':THREAD,'via':'t3'},{'1'}), ({'t3_thread':'absent'},set())])
def test_replica_filters_before_candidate_limit(replica, filters, ids):
    assert {r['id'] for r in hs.search_snapshot('recall', 5, **filters)} == ids
    with mock.patch.object(hs, 'local_embed_configured', return_value=True), \
         mock.patch('khipu.models.show_models', return_value={'embed':{'endpoint':'fixture','model_id':'fixture'}}), \
         mock.patch.object(hs, '_embed_query_local', return_value=[1.,0.]):
        assert {r['id'] for r in hs.semantic_search_snapshot('recall', limit=5, **filters)} == ids


def test_stale_filters_graph_and_outbox_do_not_leak(replica, tmp_path):
    from khipu import outbox
    jobs=[]
    for name, thread in [('same',THREAD),('other','other')]:
        path=tmp_path/(name+'.json')
        path.write_text(json.dumps({'payload':{'summary':'recall budget','t3_thread_id':thread,'via':'t3'}}))
        jobs.append(path)
    def graph(con, rows):
        return [*rows, {'kind':'episode','id':'99','snippet':'recall'}, {'kind':'topic','id':'recall','snippet':'recall'}], []
    with mock.patch.object(outbox, 'jobs', return_value=jobs), mock.patch.object(hs,'_graph_candidates_snapshot',side_effect=graph):
        result=hs.search_stale_payload('recall', 10, t3_thread=THREAD, via='t3')
    assert {r['id'] for r in result['results']} == {'1','outbox:same'}


@pytest.mark.parametrize('mode', ['literal','hybrid','semantic'])
def test_mcp_and_gateway_forward_filters(mode):
    # The HTTPS gateway dispatches to this same handle_message function.
    args={'query':'recall budget','mode':mode,'t3_thread':THREAD,'via':'t3'}
    with mock.patch('khipu.embed.hybrid_search', return_value={'results':[]}) as search, \
         mock.patch('khipu.query_log.log_query'), mock.patch.object(mcp,'_ensure_path'), \
         mock.patch.object(mcp,'_GATEWAY_ACTIVE',True):
        response=mcp.handle_message({'jsonrpc':'2.0','id':1,'method':'tools/call',
                                     'params':{'name':'khipu_search','arguments':args}})
    assert not response['result'].get('isError')
    assert search.call_args.kwargs['t3_thread'] == THREAD
    assert search.call_args.kwargs['via'] == 't3'
    assert search.call_args.kwargs['mode'] == mode


@pytest.mark.parametrize('mode', ['literal','hybrid','semantic'])
def test_mcp_offline_forward_filters(mode, replica):
    with mock.patch('khipu.embed.hybrid_search', side_effect=ConnectionError('offline')), \
         mock.patch.object(hs, 'hub_connection_failed', return_value=True), \
         mock.patch.object(hs, 'search_stale_payload', return_value={'results':[]}) as fallback, \
         mock.patch('khipu.query_log.log_query'):
        mcp._tool_search({'query':'recall','mode':mode,'t3_thread':THREAD,'via':'t3'})
    assert fallback.call_args.kwargs['t3_thread'] == THREAD
    assert fallback.call_args.kwargs['via'] == 't3'
    assert fallback.call_args.kwargs['semantic'] == (mode == 'semantic')


def test_hub_sql_exact_filters_and_post_fusion_guard():
    f=embed._SearchFilters(t3_thread='thread%_x', via='t3')
    cur=mock.MagicMock()
    with mock.patch.object(embed,'_episode_schema_flags', return_value={'project':False,'harness':False,'deleted_at':False}):
        assert "e.raw->>'t3_thread_id' = %(kf_t3_thread)s" in f.episode_sql(cur,'e')
        assert "e.raw->>'via' = %(kf_via)s" in f.episode_sql(cur,'e')
        assert f.topic_sql() == f.node_sql() == 'FALSE'
        assert f.params['kf_t3_thread'] == 'thread%_x'
        cur.fetchall.return_value=[('1', datetime.now(timezone.utc),'claude:a','x',THREAD,'t3'),
                                   ('2', datetime.now(timezone.utc),'codex:b','x','other','t3')]
        rows=[{'kind':'episode','id':'1'},{'kind':'episode','id':'2'},{'kind':'media','id':'m'}]
        assert [r['id'] for r in embed._apply_search_filters(cur,rows,t3_thread=THREAD,via='t3')] == ['1']


def test_dedup_cannot_cross_thread_boundaries():
    cur=mock.MagicMock()
    cur.fetchall.return_value=[]
    capture._dedup_candidates(cur, {'ts':'2026-10-08','project':'fixture','t3_thread_id':THREAD})
    sql, params=cur.execute.call_args.args
    assert "raw->>'t3_thread_id' = %s" in sql and THREAD in params
    capture._dedup_candidates(cur, {'ts':'2026-10-08','project':'fixture'})
    assert "raw->>'t3_thread_id' IS NULL" in cur.execute.call_args.args[0]


def _ordinary(*args, **kwargs):
    return {'hits':[{'kind':'episode','id':'100','snippet':'ordinary recall'}], 'legs':['fixture'], 'degraded':None}


def _priority(*args, **kwargs):
    return [{'kind':'decision','id':'1','snippet':'Keep the budget','status':'standing'},
            {'kind':'commitment','id':'2','snippet':'Verify continuity','status':'open'}]


@pytest.mark.parametrize('budget', [None, 100])
def test_switch_recall_orders_thread_first_within_budget(budget):
    with mock.patch.object(rp,'_search_hits',side_effect=_ordinary), \
         mock.patch.object(rp,'_search_hits_budgeted',side_effect=_ordinary), \
         mock.patch.object(rp,'_thread_memory_hits',side_effect=_priority), \
         mock.patch.object(rp,'_deliverable_context_line',return_value='x'*600):
        out=rp.prior_work_for_prompt(handoff_wrapper('recall budget'), budget_ms=budget)
    assert [h['kind'] for h in out['hits']] == ['decision','commitment','episode']
    assert out['context'].index('decision') < out['context'].index('commitment') < out['context'].index('episode')
    assert len(out['context']) <= rp.BLOCK_CHAR_BUDGET


def test_switch_with_ack_still_recalls_thread():
    with mock.patch.object(rp,'_search_hits') as search, mock.patch.object(rp,'_thread_memory_hits',side_effect=_priority):
        out=rp.prior_work_for_prompt(handoff_wrapper('continue'))
    search.assert_not_called()
    assert out['hits'][0]['kind'] == 'decision'


def test_missing_header_thread_keeps_ordinary_recall():
    prompt=handoff_wrapper('recall budget').replace(f'Thread: {THREAD}.','Thread: .')
    with mock.patch.object(rp,'_search_hits',side_effect=_ordinary), mock.patch.object(rp,'_thread_memory_hits') as priority:
        out=rp.prior_work_for_prompt(prompt)
    priority.assert_not_called()
    assert out['hits'][0]['kind'] == 'episode'


def test_switch_priority_outage_does_not_hide_ordinary_hits():
    with mock.patch.object(rp,'_search_hits',side_effect=_ordinary), \
         mock.patch.object(rp,'_thread_memory_hits',side_effect=RuntimeError('offline')):
        out=rp.prior_work_for_prompt(handoff_wrapper('recall budget'))
    assert out['hits'][0]['kind'] == 'episode'


def test_switch_legs_share_deadline():
    release=threading.Event()
    def wait(*a,**kw):
        release.wait(1)
        return []
    with mock.patch.object(rp,'TIMEOUT_S',.05), \
         mock.patch.object(rp,'_thread_memory_hits',side_effect=wait), \
         mock.patch.object(rp,'_search_hits',side_effect=_ordinary):
        try:
            start=time.monotonic()
            out=rp.prior_work_for_prompt(handoff_wrapper('recall budget'))
            assert time.monotonic()-start < .15
            assert out['hits'][0]['kind'] == 'episode'
            assert 't3_thread' in out['degraded_legs']
        finally:
            release.set()


def test_thread_reader_uses_current_state_across_sessions():
    from tests.fixtures.t3_slice_c import memory_fixture
    con, Connection = memory_fixture()
    con.execute("INSERT INTO decisions VALUES (3, 'Superseded rule', 1, '2026-10-08', 1, NULL)")
    con.execute("INSERT INTO decisions VALUES (4, 'Retracted rule', 1, '2026-10-08', NULL, '2026-10-08')")
    con.execute("INSERT INTO decisions VALUES (5, 'Other thread', 30, '2026-10-08', NULL, NULL)")
    con.execute("INSERT INTO commitments VALUES (6, 'Already closed', 2, '2026-10-08', 'closed', NULL)")
    con.execute("INSERT INTO commitments VALUES (7, 'Snoozed', 2, '2026-10-08', 'open', '2999-01-01')")
    con.execute("INSERT INTO commitments VALUES (8, 'Other thread', 30, '2026-10-08', 'open', NULL)")
    con.execute("INSERT INTO commitments VALUES (9, 'Forgotten episode', 4, '2026-10-08', 'open', NULL)")
    con.execute("UPDATE episodes SET deleted_at='2026-10-08' WHERE id=4")
    with mock.patch.object(hs, 'try_hub_connect', return_value=Connection()), \
         mock.patch('khipu.db.has_columns', return_value=True):
        hits=rp._thread_memory_hits(THREAD)
    con.close()
    assert [(h['kind'],h['id']) for h in hits] == [('decision','1'),('commitment','2')]
    assert [h['episode_id'] for h in hits] == [1,2]


def test_thread_reader_replica_does_not_revive_raw_open_loops(replica):
    with sqlite3.connect(replica) as con:
        con.execute("INSERT INTO decisions (id,text,episode_id,decided_at) VALUES (1,'Keep budget',1,'2026-10-07')")
        con.execute("INSERT INTO decisions (id,text,episode_id,decided_at,superseded_by) VALUES (2,'Old budget',1,'2026-10-08',1)")
        con.execute("UPDATE episodes SET raw=? WHERE id=1", (json.dumps({'t3_thread_id':THREAD, 'open_loops':['already closed']}),))
    with mock.patch.object(hs,'try_hub_connect',side_effect=ConnectionError('offline')):
        hits=rp._thread_memory_hits(THREAD)
    assert [(h['kind'],h['id']) for h in hits] == [('decision','1')]
    assert hits[0]['status'] == 'standing (replica)'


def test_priority_survives_topical_search_timeout():
    release=threading.Event()
    def hang(*a,**kw):
        release.wait(1)
        return _ordinary()
    with mock.patch.object(rp,'TIMEOUT_S',.03), \
         mock.patch.object(rp,'_thread_memory_hits',side_effect=_priority), \
         mock.patch.object(rp,'_search_hits',side_effect=hang):
        try:
            out=rp.prior_work_for_prompt(handoff_wrapper('recall budget'))
            assert out['hits'][0]['kind'] == 'decision'
            assert out['reason'].startswith('ok')
        finally:
            release.set()


def test_t3_handoff_dedup_does_not_hide_continuity(tmp_path):
    with mock.patch.object(rp,'_search_hits',side_effect=_ordinary), \
         mock.patch.object(rp,'_thread_memory_hits',side_effect=_priority), \
         mock.patch.object(rp,'_dedup_path',return_value=tmp_path/'dedup.json'):
        a=rp.prior_work_for_prompt(handoff_wrapper('recall budget'), session_id='resumed-session')
        b=rp.prior_work_for_prompt(handoff_wrapper('recall budget'), session_id='resumed-session')
    assert a['context'] == b['context'] != ''


@pytest.mark.parametrize('mode', ['literal', 'semantic', 'hybrid'])
def test_hub_modes_use_thread_filters_on_each_candidate_leg(mode):
    candidates=[{'kind':'episode','id':'1','snippet':'recall budget','rank_text':'recall budget','score':.9}]
    cur=mock.MagicMock()
    cur.fetchall.return_value=[('1',datetime.now(timezone.utc),'claude:a','fixture',THREAD,'t3')]
    conn=mock.MagicMock()
    conn.__enter__.return_value.cursor.return_value.__enter__.return_value=cur
    filters_seen=[]
    def leg(*args,filters,**kw):
        filters_seen.append(filters)
        assert filters.episode_only
        assert "raw->>'t3_thread_id'" in filters.episode_sql(cur)
        assert "raw->>'via'" in filters.episode_sql(cur)
        return [dict(r) for r in candidates]
    with mock.patch.object(hs,'try_hub_connect',return_value=conn), \
         mock.patch.object(embed,'_cosine_candidates',side_effect=leg), \
         mock.patch('khipu.cli._literal_candidates',side_effect=leg), \
         mock.patch.object(embed,'_episode_schema_flags',return_value={'project':False,'harness':False,'deleted_at':False}), \
         mock.patch('khipu.topic_graph.enrich_search_results',side_effect=lambda c,rows:rows), \
         mock.patch('khipu.decisions.enrich_search_results',side_effect=lambda c,rows:rows), \
         mock.patch('khipu.features.enabled',return_value=False), \
         mock.patch('khipu.relevance.enabled',return_value=False):
        out=embed.hybrid_search('recall budget',mode=mode,t3_thread=THREAD,via='t3')
    assert [r['id'] for r in out['results']] == ['1']
    assert len(filters_seen) == (2 if mode == 'hybrid' else 1)


def test_replica_without_raw_fails_closed(replica):
    with sqlite3.connect(replica) as con:
        con.execute('ALTER TABLE episodes DROP COLUMN raw')
    assert hs.search_snapshot('recall', 5, t3_thread=THREAD) == []


def test_stale_semantic_graph_respects_thread(replica):
    def graph(con, rows):
        return [*rows, {'kind':'episode','id':'99','snippet':'recall'}], []
    with mock.patch.object(hs,'local_embed_configured',return_value=True), \
         mock.patch('khipu.models.show_models',return_value={'embed':{'endpoint':'fixture','model_id':'fixture'}}), \
         mock.patch.object(hs,'_embed_query_local',return_value=[1.,0.]), \
         mock.patch.object(hs,'_graph_candidates_snapshot',side_effect=graph):
        out=hs.search_stale_payload('recall',5,semantic=True,t3_thread=THREAD,via='t3')
    assert [r['id'] for r in out['results']] == ['1']


def test_priority_lines_and_footer_reserve_exact_budget():
    rows=[{'kind':'decision','id':str(i),'snippet':'x'*90,'episode_id':42,'status':'standing'} for i in range(20)]
    for budget in range(150,601):
        with mock.patch.object(rp,'BLOCK_CHAR_BUDGET',budget):
            assert len(rp.render_block(rows)) <= budget


@pytest.mark.parametrize('sid', ['claude-one', 'claude_code:claude-one', 'codex:codex-two'])
def test_explicit_capture_also_stamps(t3_db, sid):
    payload={'session_id':sid,'summary':'Thread memory'}
    with mock.patch.object(capture,'run_capture_v2',return_value=0) as persist:
        assert capture.capture(payload,mode='legacy') == 0
    stored=persist.call_args.args[0]
    assert (stored['t3_thread_id'], stored['via']) == (THREAD,'t3')


def test_large_projection_vm_is_bounded(t3_db):
    with sqlite3.connect(t3_db) as con:
        con.executemany('INSERT INTO orchestration_v2_projection_provider_threads VALUES (?, ?, ?)',
                        [('other',json.dumps({'nativeThreadRef':{'nativeId':'other'}}),'2026-10-07')]*1000)
    with mock.patch.object(t3.time,'monotonic',side_effect=[0, 1]):
        assert t3.thread_for_session('absent') is None
