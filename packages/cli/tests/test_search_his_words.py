"""Synthetic retrieval grades and M1 phase profile; never opens the live hub."""
import importlib
import json
import time
from unittest import mock

from khipu import cli, embed
from tests.hermetic_pg import hub, pg_cluster
from tests.test_audit_recall_search import configure_hub


def test_profile_and_grades(hub, monkeypatch, tmp_path):
    configure_hub(monkeypatch, hub)
    from tests.test_search_indexed_words import install_words
    install_words(hub, tmp_path)
    conn = hub.connect()
    conn.execute("INSERT INTO episodes (ts,summary,raw,session_id,project,verbatim) VALUES (now(),%s,'{}',%s,'invented',%s::jsonb)",
                 ('synthetic exact', 'codex:12345678-abcd-1234-abcd-123456789abc', json.dumps({'quotes': ['keep the violet reset intact']})))
    conn.execute("INSERT INTO episodes (ts,summary,raw,project) VALUES (now(),'preserve purple restart','{}','invented'), (now(),'unrelated sermon rules','{}','other')")
    conn.commit()
    ids = [str(r[0]) for r in conn.execute('SELECT id FROM episodes ORDER BY id')]
    def cosine(q, **kw):
        kw['timing'].update(embed_ms=0.0, cosine_ms=0.0, embed_cache='synthetic')
        return [dict(kind='episode', id=i, score=s, rank_text=t, label=t, snippet=t) for i,s,t in
                [(ids[2],.91,'unrelated sermon rules'), (ids[1],.87,'preserve purple restart'), (ids[0],.79,'synthetic exact')]]
    monkeypatch.setattr(embed, '_cosine_candidates', cosine)
    start = time.perf_counter()
    out = embed.hybrid_search('keep the violet reset intact', limit=3)
    print('PROFILE', json.dumps(out['timing']), 'wall_ms', round((time.perf_counter()-start)*1000,2))
    print('GRADE exact_top', out['results'][0]['id'] == ids[0], 'confidence', out['confidence'])
    assert out['results'][0]['id'] == ids[0]
    assert out['confidence'] == 'strong'
    for q in ('12345678-abcd-1234-abcd-123456789abc', 'codex:12345678-abcd-1234-abcd-123456789abc'):
        assert [r['id'] for r in embed.hybrid_search(q, mode='literal')['results']] == [ids[0]]
    assert [r['id'] for r in embed.hybrid_search('violet', mode='literal', session_id='12345678-abcd')['results']] == [ids[0]]
    conn.execute('UPDATE episodes SET deleted_at=now() WHERE id=%s', (int(ids[0]),))
    conn.commit()
    assert not embed.hybrid_search('keep the violet reset intact', mode='literal')['results']


def test_confidence_grades():
    sample = [('exact', [{'lexical_hits':4,'cosine':.79}], 'strong'),
              ('paraphrase', [{'lexical_hits':0,'cosine':.88}], 'weak'),
              ('off_topic_top', [{'lexical_hits':0,'cosine':.61},{'lexical_hits':4,'cosine':.92}], 'none')]
    grades = [(name, embed.search_confidence(rows, token_count=4), expected) for name,rows,expected in sample]
    print('CONFIDENCE_GRADES', grades)
    assert all(actual == expected for _,actual,expected in grades)


def test_default_excludes_library():
    from tests.test_library_search import FakeCur, _hub, _cos_row
    cur = FakeCur(cosine=[_cos_row()])
    with _hub(cur), mock.patch('khipu.library_search.library_candidates', side_effect=AssertionError('default library work')):
        assert embed.hybrid_search('divine council')['results'] == []


def test_project_agreement():
    rows = [dict(id=str(i), kind='episode', project=p, lexical_hits=h, score=s) for i,p,h,s in
            [(1,'other',0,.05),(2,'invented',2,.049),(3,'invented',2,.048)]]
    ranked = embed._rank_search_rows(rows, None)
    assert ranked[0]['project'] == 'invented'


def test_snapshot_session(hub, monkeypatch, tmp_path):
    from tests.test_audit_recall_search import snapshot
    from khipu import hub_snapshot as hs
    con = snapshot(monkeypatch, tmp_path)
    sid = 'codex:12345678-abcd-1234-abcd-123456789abc'
    con.execute("INSERT INTO episodes (id,ts,summary,session_id) VALUES (1,'2026-11-06','synthetic',?)", (sid,))
    con.commit()
    assert [r['id'] for r in hs.search_stale_payload(sid.split(':')[1],12)['results']] == ['1']
    con.execute("UPDATE episodes SET deleted_at='2026-11-06' WHERE id=1")
    con.commit()
    assert not hs.search_stale_payload(sid,12)['results']


def _owner_corpus(hub, tmp_path):
    from tests.test_search_indexed_words import install_words
    install_words(hub, tmp_path)
    conn = hub.connect()
    quotes = json.dumps({'quotes': ['please keep the violet reset intact', 'second line']})
    agent_only = json.dumps({'commands': ['echo violet reset'], 'note': 'agent says violet reset',
                             'errors': ['violet reset failed'], 'quotes': ['unrelated owner line']})
    conn.execute("INSERT INTO episodes (ts,summary,raw,session_id,project,harness,origin,verbatim) VALUES "
                 "(now(),'Reset work. More detail follows here.','{}','codex:aaaaaaaa-0000-0000-0000-000000000001','khipu','codex','gateway:x',%s::jsonb),"
                 "(now(),'The violet reset was discussed by the agent','{}','claude_code:aaaaaaaa-0000-0000-0000-000000000002','khipu','claude_code',NULL,%s::jsonb)",
                 (quotes, agent_only))
    conn.commit()
    return [str(r[0]) for r in conn.execute('SELECT id FROM episodes ORDER BY id')]


def test_owner_words_only_matches_the_owners_lines(hub, monkeypatch, tmp_path):
    configure_hub(monkeypatch, hub)
    ids = _owner_corpus(hub, tmp_path)
    monkeypatch.setattr(embed, '_cosine_candidates', lambda q, **kw: kw['timing'].update(embed_ms=0.0, cosine_ms=0.0) or
                        [dict(kind='episode', id=i, score=.9 - n / 10, rank_text='x', label='x', snippet='x') for n, i in enumerate(ids)])
    for mode in ('literal', 'hybrid'):
        plain = embed.hybrid_search('violet reset', mode=mode)
        assert set(r['id'] for r in plain['results']) >= set(ids) or mode == 'literal'
        assert 'quote' not in plain['results'][0]  # callers without the option see the old keys
        out = embed.hybrid_search('violet reset', mode=mode, owner_words_only=True)
        assert [r['id'] for r in out['results']] == [ids[0]], mode
        row = out['results'][0]
        assert row['quote'] == 'please keep the violet reset intact'
        assert row['title'] == 'Reset work.'
        assert row['tool'] == 'Codex' and row['mac'] is None and row['project'] == 'khipu'
        assert {'quote', 'title', 'tool', 'mac', 'project'} <= set(row)


def test_owner_words_only_agent_line_never_matches(hub, monkeypatch, tmp_path):
    configure_hub(monkeypatch, hub)
    _owner_corpus(hub, tmp_path)
    monkeypatch.setattr(embed, '_cosine_candidates', lambda q, **kw: [])
    # the words appear only in an agent's command, error and note
    for mode in ('literal', 'hybrid'):
        assert not embed.hybrid_search('echo violet', mode=mode, owner_words_only=True)['results']
        assert not embed.hybrid_search('violet reset failed', mode=mode, owner_words_only=True)['results']
        assert not embed.hybrid_search('agent says', mode=mode, owner_words_only=True)['results']
    with __import__('pytest').raises(ValueError):
        embed.hybrid_search('violet', kind='topic', owner_words_only=True)


def test_search_rows_carry_memory_fields_only_when_asked(hub, monkeypatch, tmp_path):
    configure_hub(monkeypatch, hub)
    ids = _owner_corpus(hub, tmp_path)
    monkeypatch.setattr(embed, '_cosine_candidates', lambda q, **kw: [])
    plain = embed.hybrid_search('violet reset', mode='literal')['results']
    asked = embed.hybrid_search('violet reset', mode='literal', memory_fields=True)['results']
    assert all('quote' not in r and 'mac' not in r for r in plain)
    by_id = {r['id']: r for r in asked}
    assert by_id[ids[0]]['quote'] == 'please keep the violet reset intact'
    assert by_id[ids[1]]['quote'] == 'The violet reset was discussed by the agent'  # summary line, no owner line
    assert by_id[ids[1]]['tool'] == 'Claude Code'


def test_activity_rows_carry_memory_fields(hub, monkeypatch, tmp_path):
    from khipu import activity, db
    configure_hub(monkeypatch, hub)
    monkeypatch.setattr(activity, 'connect', lambda **kw: hub.connect())
    ids = _owner_corpus(hub, tmp_path)
    out = activity.activity_payload(limit=5)
    by_id = {str(r['id']): r for r in out['recent']}
    row = by_id[ids[0]]
    assert (row['quote'], row['title'], row['tool'], row['mac'], row['project']) == \
           ('please keep the violet reset intact', 'Reset work.', 'Codex', None, 'khipu')
    assert by_id[ids[1]]['quote'] == 'unrelated owner line'
    assert list(row)[-5:] == ['quote', 'title', 'tool', 'mac', 'project']


# ---- automated user-role lines are not the owner's words --------------------

AUTOMATED = {
    'scheduled': 'Lane check (scheduled, not Matt): remember the owner said "test everything ourselves" and act on it',
    'system notice': '[SYSTEM NOTIFICATION - NOT USER INPUT] background task finished: test everything ourselves',
    'delegated notice': 'Delegated task "audit" [completed] test everything ourselves',
    'task notification': '<task-notification><summary>test everything ourselves</summary></task-notification>',
    'hook text': '<system-reminder>test everything ourselves</system-reminder>',
    'compaction summary': 'This session is being continued from a previous conversation. The user said test everything ourselves.',
    'job brief': 'Act as the implementation sub-agent for this task. Test everything ourselves before you finish.',
    'machine stand-in': '[notification: task-notification] test everything ourselves',
}


def _insert(hub, session, quotes):
    conn = hub.connect()
    conn.execute("INSERT INTO episodes (ts,summary,raw,session_id,project,harness,verbatim) VALUES "
                 "(now(),'Work.','{}',%s,'khipu','claude_code',%s::jsonb)", (session, json.dumps({'quotes': quotes})))
    conn.commit()
    return str(conn.execute('SELECT max(id) FROM episodes').fetchone()[0])


def test_each_automated_kind_is_not_matched_or_quoted_but_a_typed_line_is(hub, monkeypatch, tmp_path):
    from tests.test_search_indexed_words import install_words
    configure_hub(monkeypatch, hub)
    install_words(hub, tmp_path)
    monkeypatch.setattr(embed, '_cosine_candidates', lambda q, **kw: [])
    typed = 'we should test everything ourselves this time'
    for n, (kind, line) in enumerate(AUTOMATED.items()):
        only = _insert(hub, f'claude_code:00000000-0000-0000-0000-0000000001{n:02d}', [line])
        both = _insert(hub, f'claude_code:00000000-0000-0000-0000-0000000002{n:02d}', [line, typed])
        for mode in ('literal', 'hybrid'):
            got = {r['id']: r for r in embed.hybrid_search('test everything ourselves', mode=mode,
                                                           owner_words_only=True, limit=50)['results']}
            assert only not in got, (kind, mode)           # only automated lines: not returned
            assert both in got and got[both]['quote'] == typed, (kind, mode)


def test_a_typed_line_quoting_an_automated_phrase_still_matches(hub, monkeypatch, tmp_path):
    from tests.test_search_indexed_words import install_words
    configure_hub(monkeypatch, hub)
    install_words(hub, tmp_path)
    monkeypatch.setattr(embed, '_cosine_candidates', lambda q, **kw: [])
    line = 'why did "Lane check (scheduled, not Matt)" say test everything ourselves?'
    eid = _insert(hub, 'claude_code:00000000-0000-0000-0000-000000000301', [line])
    out = embed.hybrid_search('test everything ourselves', mode='literal', owner_words_only=True)['results']
    assert [r['id'] for r in out] == [eid] and out[0]['quote'] == line


def test_automated_matches_cannot_crowd_out_the_typed_one(hub, monkeypatch, tmp_path):
    from tests.test_search_indexed_words import install_words
    configure_hub(monkeypatch, hub)
    install_words(hub, tmp_path)
    monkeypatch.setattr(embed, '_cosine_candidates', lambda q, **kw: [])
    mine = _insert(hub, 'claude_code:00000000-0000-0000-0000-000000000401', ['please test everything ourselves'])
    for n in range(12):  # newer, so they come first under ORDER BY ts DESC
        _insert(hub, f'claude_code:00000000-0000-0000-0000-0000000005{n:02d}', [AUTOMATED['scheduled']])
    out = embed.hybrid_search('test everything ourselves', mode='literal', owner_words_only=True, limit=2)['results']
    assert [r['id'] for r in out] == [mine]


def test_without_the_flag_automated_lines_are_still_found(hub, monkeypatch, tmp_path):
    from tests.test_search_indexed_words import install_words
    configure_hub(monkeypatch, hub)
    install_words(hub, tmp_path)
    monkeypatch.setattr(embed, '_cosine_candidates', lambda q, **kw: [])
    eid = _insert(hub, 'claude_code:00000000-0000-0000-0000-000000000601', [AUTOMATED['scheduled']])
    out = embed.hybrid_search('test everything ourselves', mode='literal')['results']
    assert eid in [r['id'] for r in out]
    assert 'quote' not in out[0]


def test_activity_quote_skips_an_automated_first_line(hub, monkeypatch, tmp_path):
    from khipu import activity
    configure_hub(monkeypatch, hub)
    monkeypatch.setattr(activity, 'connect', lambda **kw: hub.connect())
    eid = _insert(hub, 'claude_code:00000000-0000-0000-0000-000000000701',
                  [AUTOMATED['system notice'], 'what the owner typed'])
    only = _insert(hub, 'claude_code:00000000-0000-0000-0000-000000000702', [AUTOMATED['task notification']])
    by_id = {str(r['id']): r for r in activity.activity_payload(limit=5)['recent']}
    assert by_id[eid]['quote'] == 'what the owner typed'
    assert by_id[only]['quote'] is None


def test_is_automated_kinds():
    from khipu.turn_text import is_automated
    for kind, line in AUTOMATED.items():
        assert is_automated(line), kind
    assert is_automated('<cross-session-message>hi</cross-session-message>')
    assert is_automated('<local-command-stdout>x</local-command-stdout>')
    for typed in ('test everything ourselves', 'dont stop until we are done',
                  'what does "This session is being continued" mean?', 'the Delegated task list looks off',
                  'run a check (scheduled for friday) please', 'use a < b'):
        assert not is_automated(typed), typed
