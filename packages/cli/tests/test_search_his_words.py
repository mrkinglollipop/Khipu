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


def test_title_uses_the_first_typed_line_when_there_is_no_summary():
    from khipu import memory_rows as mr

    auto = "Lane check (scheduled, not Matt): look at billing"
    assert mr.title_of("", [auto, "what the owner typed"]) == "what the owner typed"
    assert mr.title_of("anything", [auto, "what the owner typed"], verbatim_only=True) == "what the owner typed"
    assert mr.title_of("", [auto]) == auto
    assert mr.title_of("Work.", [auto, "what the owner typed"]) == "Work."


# ---- owner_lines: every typed line of the window is kept ---------------------

from copy import deepcopy  # noqa: E402

from khipu import capture, extract, forget, hub_snapshot, session_capture  # noqa: E402
from tests.test_capture_replay_duplicate import at_the_key, pg  # noqa: E402,F401

FAKE_KEY = 'ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd'
TYPED_ONE = f'we have to test everything ourselves, token {FAKE_KEY}\n\nand then report back'
TYPED_TWO = 'dont stop until we are done with the purple harbour'
TYPED_THREE = 'one more typed line about the amber lighthouse'
RELAYED = 'please re-run the lexical parity harness on the fresh copy'
RELAYED_TWO = 'second relay: check the tangerine bridge results'
# Every user-role message capture rewrites or drops, each with words no typed
# line has; none may ever reach owner_lines.
MACHINE = {
    'cross-session message': f'<cross-session-message from="aegis-1">{RELAYED}</cross-session-message>',
    'cross-session idle': '<cross-session-idle from="aegis-2">idle teammate cobalt quarry</cross-session-idle>',
    'cross-session delivery': '<cross-session-delivery>delivered note saffron meadow</cross-session-delivery>',
    'task notification': '<task-notification><task-id>t1</task-id><summary>job finished: crimson '
                         'orchard</summary><result>' + 'r' * 300 + '</result></task-notification>',
    'local command output': '<local-command-stdout>local output velvet canyon</local-command-stdout>',
    'slash command': '<command-name>/clear</command-name><command-args>granite tundra</command-args>',
    'hook context only': '<system-reminder>reminder text ivory delta</system-reminder>',
    'scheduled prompt': 'Lane check (scheduled, not Matt). Quick and quiet; read every lane, emerald '
                        'ridge. ' + 'detail ' * 400,
}
MACHINE_WORDS = ('lexical parity harness', 'tangerine bridge', 'magenta glacier', 'cobalt quarry', 'saffron meadow', 'crimson orchard',
                 'velvet canyon', 'granite tundra', 'ivory delta', 'emerald ridge', 'continued from')


def _user_row(text, **extra):
    return {'type': 'user', 'message': {'role': 'user', 'content': text}, **extra}


def _assistant_row(text):
    return {'type': 'assistant', 'message': {'role': 'assistant', 'content': text}}


def _orchestrator_rows():
    rows = [_user_row('This session is being continued from a previous conversation that ran out of '
                      'context. Summary: the owner wants the lanes checked.', isCompactSummary=True),
            _assistant_row('Picking up where we left off.')]
    for n, (kind, text) in enumerate(MACHINE.items()):
        rows += [_user_row(text), _assistant_row(f'noted {n}')]
        if kind == 'cross-session message':
            rows += [_user_row(TYPED_ONE), _assistant_row('Running it.')]
        if kind == 'local command output':
            rows += [_user_row(TYPED_TWO), _assistant_row('On it.')]
    # a typed line followed straight away by a relayed message: one rendered block
    rows += [_user_row(TYPED_THREE),
             _user_row(f'<cross-session-message from="aegis-3">{RELAYED_TWO}</cross-session-message>'),
             _assistant_row('Both read.'),
             _user_row('<task-notification><summary>last job: magenta glacier</summary></task-notification>')]
    return rows


def _queue(tmp_path, rows, sid='11111111-2222-3333-4444-555555555555', event='SessionEnd'):
    """Write a Claude Code transcript and run the real capture hook over it;
    return the queued job."""
    path = tmp_path / '.claude' / 'projects' / 'p' / f'{sid}.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    out = session_capture.hook_main(json.dumps({'hook_event_name': event, 'session_id': sid,
                                                'cwd': str(tmp_path), 'transcript_path': str(path)}),
                                    'claude_code')
    assert out.get('due'), out
    jobs = session_capture.queued_jobs()
    assert len(jobs) == 1, jobs
    return jobs[0]


def _drain_job(job_path, *, drop_owner_lines=False):
    """Run the real drain over one queued job; return (capture payload, the
    summariser's input)."""
    if drop_owner_lines:
        job = json.loads(job_path.read_text())
        job.pop('owner_lines', None)
        job_path.write_text(json.dumps(job))
    with mock.patch.object(session_capture, 'queued_jobs', return_value=[job_path]), \
            mock.patch.object(session_capture, '_claim', side_effect=lambda p: p), \
            mock.patch.object(session_capture, '_reclaim_stale', return_value=0), \
            mock.patch.object(session_capture, '_record_drain'), \
            mock.patch.object(hub_snapshot, 'sync_decision_changes', return_value={}), \
            mock.patch('khipu.t3.thread_for_session', return_value=None), \
            mock.patch.object(extract, '_generate', return_value='{"summary":"Checked the lanes."}') as gen, \
            mock.patch.object(capture, 'capture', return_value=0) as cap:
        assert session_capture.drain()['captured'] == 1
    return cap.call_args.args[0], [c.args for c in gen.call_args_list]


def _captured(tmp_path, rows=None):
    job_path = _queue(tmp_path, rows or _orchestrator_rows())
    job = json.loads(job_path.read_text())
    payload, _ = _drain_job(job_path)
    return job, payload


def _land(payload):
    return str(capture.write_pg(deepcopy(payload))['episode_id'])


def test_orchestrator_window_keeps_typed_lines_only_and_finds_each(at_the_key, monkeypatch, tmp_path):
    configure_hub(monkeypatch, at_the_key)
    monkeypatch.setattr(embed, '_cosine_candidates', lambda q, **kw: [])
    job, payload = _captured(tmp_path)
    window = job['transcript']
    # the window text: the relayed body is kept behind its stand-in label, and a
    # typed line never shares a rendered block with the stand-in after it
    assert f'USER: [notification: cross-session-message] {RELAYED}' in window
    assert f'USER: {TYPED_THREE}\n\nUSER: [notification: cross-session-message] {RELAYED_TWO}' in window
    vb = payload['verbatim']
    # quotes are exactly what they were: first, longest, last
    assert vb['quotes'] == extract._verbatim_quotes(extract._verbatim_user_messages(window))
    # owner_lines: the three typed lines, whole and in order, redacted; no machine message
    assert vb['owner_lines'] == [TYPED_ONE.replace(FAKE_KEY, '[REDACTED]'), TYPED_TWO, TYPED_THREE]
    assert job['owner_lines'] == vb['owner_lines']
    for words in MACHINE_WORDS:
        assert not any(words in line for line in vb['owner_lines']), words
    eid = _land(payload)
    with at_the_key.connect() as conn:
        stored = conn.execute('SELECT verbatim, raw FROM episodes WHERE id = %s', (int(eid),)).fetchone()
    assert FAKE_KEY not in json.dumps(stored[0]) and FAKE_KEY not in json.dumps(stored[1])
    assert stored[1]['verbatim']['owner_lines'] == stored[0]['owner_lines'] == vb['owner_lines']
    for mode in ('literal', 'hybrid'):
        for query, quoted in (('test everything ourselves', 'we have to test everything ourselves'),
                              ('purple harbour', TYPED_TWO), ('amber lighthouse', TYPED_THREE)):
            out = embed.hybrid_search(query, mode=mode, owner_words_only=True)['results']
            assert [r['id'] for r in out] == [eid], (mode, query)
            assert out[0]['quote'].startswith(quoted), (mode, query)
            assert FAKE_KEY not in out[0]['quote']
        for words in MACHINE_WORDS:
            out = embed.hybrid_search(words, mode=mode, owner_words_only=True)['results']
            assert not out, (mode, words, out)


def test_each_rewritten_machine_message_alone_gives_no_owner_lines(tmp_path, monkeypatch):
    for n, (kind, text) in enumerate(MACHINE.items()):
        home = tmp_path / f'k{n}'
        monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(home))
        rows = [_user_row(text), _assistant_row('noted ' + 'y' * 300), _user_row(text + ' again'),
                _assistant_row('noted again')]
        if kind == 'hook context only':   # dropped whole: nothing to capture without a reply
            rows = [rows[0], _assistant_row('reply ' + 'y' * 300)]
        job_path = _queue(home, rows, sid=f'00000000-0000-0000-0000-0000000010{n:02d}')
        job = json.loads(job_path.read_text())
        # a conversation's window always carries the list: empty, nothing typed
        assert job.get('owner_lines') == [], (kind, job.get('owner_lines'))
        payload, _ = _drain_job(job_path)
        assert (payload.get('verbatim') or {}).get('owner_lines') == [], kind


def test_idle_sweep_window_carries_owner_lines_too(tmp_path, monkeypatch):
    import os
    import time
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'sweep'))
    path = tmp_path / 'idle.jsonl'
    path.write_text(''.join(json.dumps(r) + '\n' for r in [
        _user_row(MACHINE['cross-session message']), _assistant_row('ok ' + 'y' * 300),
        _user_row(TYPED_TWO), _assistant_row('fine')]))
    raw = json.dumps({'hookEventName': 'Stop', 'session_id': 'idle', 'cwd': str(tmp_path),
                      'transcript_path': str(path)})
    assert not session_capture.hook_main(raw, 'claude_code')['due']
    old = time.time() - session_capture.MIN_MINUTES * 60 - 60
    state = session_capture.load_state('claude_code', 'idle')
    state['seen_ts'] = old
    session_capture.save_state('claude_code', 'idle', state)
    os.utime(path, (old, old))
    assert session_capture.sweep_idle(now=time.time())['swept'] == 1
    job = json.loads(session_capture.queued_jobs()[0].read_text())
    assert RELAYED in job['transcript']
    assert job['owner_lines'] == [TYPED_TWO]


def test_a_job_queued_without_the_marker_gets_no_owner_lines(tmp_path, monkeypatch):
    """A job from before this change carries no typed list; the rendered
    window is never re-read for one."""
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'old'))
    payload, _ = _drain_job(_queue(tmp_path, _orchestrator_rows()), drop_owner_lines=True)
    assert 'owner_lines' not in payload['verbatim'] and payload['verbatim']['quotes']
    assert 'owner_lines' not in extract.extract_verbatim('USER: ' + TYPED_TWO)


def test_a_forgotten_conversation_is_neither_matched_nor_quoted(at_the_key, monkeypatch, tmp_path):
    configure_hub(monkeypatch, at_the_key)
    monkeypatch.setattr(embed, '_cosine_candidates', lambda q, **kw: [])
    _, payload = _captured(tmp_path)
    eid = _land(payload)
    assert embed.hybrid_search('purple harbour', mode='literal', owner_words_only=True)['results']
    with at_the_key.connect() as conn, conn.cursor() as cur:
        assert forget.forget_episode(cur, int(eid))['ok']
        conn.commit()
    for mode in ('literal', 'hybrid'):
        out = embed.hybrid_search('purple harbour', mode=mode, owner_words_only=True)['results']
        assert eid not in [r['id'] for r in out], mode
        assert not any(TYPED_TWO in (r.get('quote') or '') for r in out)


def test_summariser_input_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'a'))
    with_lines, prompts = _drain_job(_queue(tmp_path, _orchestrator_rows()))
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'b'))     # same transcript, fresh capture state
    without, prompts_before = _drain_job(_queue(tmp_path, _orchestrator_rows()), drop_owner_lines=True)
    assert prompts == prompts_before                       # the model saw the same text
    assert 'owner_lines' in with_lines['verbatim'] and 'owner_lines' not in without['verbatim']
    assert {k: v for k, v in with_lines['verbatim'].items() if k != 'owner_lines'} == without['verbatim']
    volatile = {'verbatim', 'ts', 'checkout', 'repo_root', 'project', 'scope', 'transcript_range'}
    assert {k: v for k, v in with_lines.items() if k not in volatile} == \
           {k: v for k, v in without.items() if k not in volatile}


def test_embedded_and_ranked_text_ignore_owner_lines():
    row = {'summary': 'Checked the lanes.', 'topics': ['lanes'], 'verbatim': {'quotes': ['q one'], 'paths': ['/a/b.py']}}
    with_lines = deepcopy(row)
    with_lines['verbatim']['owner_lines'] = ['dont stop until we are done with the purple harbour']
    assert embed.episode_text(with_lines) == embed.episode_text(row)
    assert cli._episode_rank_text('s', [], [], [], [], with_lines['verbatim']) == \
           cli._episode_rank_text('s', [], [], [], [], row['verbatim'])


def test_without_the_flag_owner_lines_change_nothing(hub, monkeypatch, tmp_path):
    """Plain search (and so per-prompt recall) reads the same rows, keys and
    text whether or not a row holds owner_lines."""
    from tests.test_search_indexed_words import install_words
    configure_hub(monkeypatch, hub)
    install_words(hub, tmp_path)
    monkeypatch.setattr(embed, '_cosine_candidates', lambda q, **kw: [])
    eid = _insert(hub, 'claude_code:00000000-0000-0000-0000-000000000801', ['first owner line about lanes'])
    queries = ('purple harbour', 'dont stop until we are done', 'owner line', 'lanes')

    def every_leg():
        out = {}
        with hub.connect() as conn, conn.cursor() as cur:
            for q in queries:
                for fast in (False, True):
                    out[(q, 'literal', fast)] = cli._literal_candidates(cur, q, 50, fast=fast)
                out[(q, 'phrase')] = cli._literal_candidates(cur, q, 50, fast=True, owner_words=True)
        for q in queries:
            for mode in ('literal', 'hybrid'):
                res = embed.hybrid_search(q, mode=mode)['results']
                out[(q, mode)] = [{k: v for k, v in r.items() if k not in ('timing',)} for r in res]
                out[(q, mode, 'memory')] = embed.hybrid_search(q, mode=mode, memory_fields=True)['results']
        return out

    before = every_leg()
    with hub.connect() as conn:
        conn.execute("UPDATE episodes SET verbatim = verbatim || %s::jsonb WHERE id = %s",
                     (json.dumps({'owner_lines': ['dont stop until we are done with the purple harbour']}), int(eid)))
        conn.commit()
    after = every_leg()
    assert after == before
    assert not before[('purple harbour', 'hybrid')]
    # ...while "only my words" finds it
    out = embed.hybrid_search('purple harbour', mode='literal', owner_words_only=True)['results']
    assert [r['id'] for r in out] == [eid]


def test_old_row_without_the_key_still_matches_through_its_typed_quotes(hub, monkeypatch, tmp_path):
    from tests.test_search_indexed_words import install_words
    configure_hub(monkeypatch, hub)
    install_words(hub, tmp_path)
    monkeypatch.setattr(embed, '_cosine_candidates', lambda q, **kw: [])
    old = _insert(hub, 'claude_code:00000000-0000-0000-0000-000000000901', ['please test everything ourselves'])
    new = _insert(hub, 'claude_code:00000000-0000-0000-0000-000000000902', [AUTOMATED['scheduled']])
    with hub.connect() as conn:
        conn.execute("UPDATE episodes SET verbatim = verbatim || %s::jsonb WHERE id = %s",
                     (json.dumps({'owner_lines': ['we should test everything ourselves again']}), int(new)))
        conn.commit()
    for mode in ('literal', 'hybrid'):
        got = {r['id']: r['quote'] for r in embed.hybrid_search(
            'test everything ourselves', mode=mode, owner_words_only=True)['results']}
        assert got == {old: 'please test everything ourselves',
                       new: 'we should test everything ourselves again'}, mode


def test_owner_words_function_and_index_cover_owner_lines_and_old_rows_are_unchanged(hub, tmp_path):
    """0051's function reads every string in verbatim, so owner_lines are in
    the index with no new migration, and a row without the key gives the same
    value it always did (its index entry stays valid)."""
    from tests.test_search_indexed_words import install_words
    install_words(hub, tmp_path)
    old = {'quotes': ['Keep It'], 'paths': ['/A/b.py'], 'note': 'N'}
    with hub.connect() as conn:
        same = conn.execute(
            "SELECT public.khipu_owner_words(%s::jsonb), "
            "lower(COALESCE((SELECT string_agg(v #>> '{}', chr(10)) FROM "
            "jsonb_path_query(%s::jsonb, 'strict $.** ? (@.type() == \"string\")') AS v), ''))",
            (json.dumps(old), json.dumps(old))).fetchone()
        assert same[0] == same[1] and sorted(same[0].split('\n')) == ['/a/b.py', 'keep it', 'n']
        assert 'purple harbour' in conn.execute(
            'SELECT public.khipu_owner_words(%s::jsonb)',
            (json.dumps({**old, 'owner_lines': ['the Purple Harbour']}),)).fetchone()[0]
        names = [r[0] for r in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'episodes' AND indexdef LIKE '%khipu_owner_words%'")]
        assert names == ['idx_episodes_owner_words_trgm']


def test_owner_lines_caps_keep_the_oldest_and_the_newest():
    msgs = [f'typed line {i:03d} ' + 'w' * 20 for i in range(100)]
    lines = extract.owner_lines(msgs)
    assert len(lines) == extract.OWNER_LINES_MAX
    half = extract.OWNER_LINES_MAX // 2
    assert lines[:half] == msgs[:half] and lines[half:] == msgs[-half:]
    long = ['L%02d ' % i + 'x' * 3000 for i in range(30)]
    lines = extract.owner_lines(long)
    assert all(len(x) == extract.OWNER_LINE_CHARS for x in lines)
    assert sum(len(x) for x in lines) <= extract.OWNER_LINES_TOTAL_CHARS
    assert lines[0].startswith('L00') and lines[-1].startswith('L29')
    # duplicates kept once; untagged automated openings and capture-now's stand-in never
    assert extract.owner_lines(['same line', 'same line', AUTOMATED['job brief'],
                                'Capture note: remember this', AUTOMATED['compaction summary']]) == ['same line']


def test_owner_lines_never_fail_a_capture():
    with mock.patch('khipu.turn_text.is_automated', side_effect=RuntimeError('boom')):
        assert extract.owner_lines(['hello there']) == []
    with mock.patch.object(extract, 'owner_lines', side_effect=RuntimeError('boom')):
        assert session_capture._owner_lines_by_part([{'text': 'USER: hi'}], ['hi']) == [[]]
    parts = [{'text': 'USER: a first line'}, {'text': 'USER: a second line\n\nUSER: a first line'}]
    assert session_capture._owner_lines_by_part(parts, ['a first line', 'a second line', 'a first line']) == \
           [['a first line'], ['a second line', 'a first line']]


def test_redact_payload_treats_owner_lines_as_quotes():
    from khipu import redact
    payload = {'summary': 's', 'verbatim': {'quotes': [f'q {FAKE_KEY}'], 'owner_lines': [f'o {FAKE_KEY}']}}
    redact.redact_payload(payload)
    assert payload['verbatim'] == {'quotes': ['q [REDACTED]'], 'owner_lines': ['o [REDACTED]']}
    stored = capture._redact_capture_values({'verbatim': {'quotes': [FAKE_KEY], 'owner_lines': [FAKE_KEY]}})
    assert stored['verbatim']['quotes'] == stored['verbatim']['owner_lines'] == ['[REDACTED]']


def test_offline_replica_search_ignores_owner_lines_and_owner_words_needs_the_hub(monkeypatch, tmp_path, capsys):
    """The replica copies raw (owner_lines ride inside it, as quotes do) but
    its offline search reads summary/topics/decisions/preferences/people only,
    and "only my words" is refused offline, as before."""
    import argparse
    from tests.test_audit_recall_search import snapshot
    con = snapshot(monkeypatch, tmp_path)
    raw = json.dumps({'verbatim': {'quotes': ['q'], 'owner_lines': ['the purple harbour line']}})
    con.execute("INSERT INTO episodes (id,ts,summary,session_id,raw) VALUES (1,'2026-11-10','lanes checked','codex:x',?)",
                (raw,))
    con.commit()
    assert not hub_snapshot.search_stale_payload('purple harbour', 12)['results']
    assert [r['id'] for r in hub_snapshot.search_stale_payload('lanes checked', 12)['results']] == ['1']

    def offline(**kw):
        raise __import__('psycopg').OperationalError('connection refused')
    monkeypatch.setattr(embed, 'hybrid_search', offline)
    monkeypatch.setattr(hub_snapshot, 'hub_connection_failed', lambda exc: True)
    args = argparse.Namespace(query='purple harbour', limit=5, mode='literal', owner_words=True)
    rc = cli.cmd_search(args)
    out = capsys.readouterr().out
    assert rc == 2 and 'needs the hub' in out
