"""Probes for #560: verbatim.owner_lines hold only what the owner typed.

A job's capture (lineage, a recorded origin of ``job``, or a T3 delegated
thread) keeps none, decided once when the window is queued. Request-log
copies and another session's queued messages are never typed; the owner's
own queued message is, once. Synthetic transcripts only; the hub is the
hermetic one."""
import json
from unittest import mock

import pytest

from khipu import embed, extract, session_capture
from khipu import t3 as _t3
from tests.test_audit_recall_search import configure_hub
from tests.test_search_his_words import (  # noqa: F401
    _assistant_row, _drain_job, _land, _queue, _user_row, at_the_key, hub, pg, pg_cluster)

SKILL_BODY = ('<command-message>workflow-authoring</command-message>\n'
              '<command-name>workflow-authoring</command-name>\n'
              '# Workflow authoring reference: the chartreuse falcon rule. ' + 'skill text ' * 400)
REPLY = 'Fixing the docs now. ' + 'y' * 300


def _blob_row(role, text):
    """Claude Code's api-request-blob: a copy of what went to the API."""
    return {'type': 'api-request-blob', 'sessionId': 's', 'hash': 'h' + str(len(text)),
            'message': {'role': role, 'content': [{'type': 'text', 'text': text}]}}


def _blob_transcript():
    return [_user_row('Yes, fix docs'),
            _blob_row('system', 'system prompt text ' * 20),
            _blob_row('user', 'Yes, fix docs\n' + SKILL_BODY),
            _blob_row('user', 'Tool loaded.'),
            _blob_row('user', 'The user sent a new message while you were working:\nLocal '
                              'conversation not cloud\n\nThis is how Claude Code surfaces it.'),
            _assistant_row(REPLY)]


def _write(tmp_path, name, rows):
    path = tmp_path / name
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    return path


def _typed(path):
    stats = {}
    session_capture.read_window(path, 0, seen=(), stats=stats)
    return stats['typed']


# ---- request-log copies --------------------------------------------------------

def test_request_log_copies_never_reach_typed_or_owner_lines(tmp_path, monkeypatch):
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'cap'))
    assert _typed(_write(tmp_path, 't.jsonl', _blob_transcript())) == ['Yes, fix docs']
    job = json.loads(_queue(tmp_path, _blob_transcript()).read_text())
    assert job.get('owner_lines') == ['Yes, fix docs'], [t[:40] for t in job.get('owner_lines') or []]
    assert 'chartreuse falcon' not in json.dumps(job)
    assert 'Local conversation not cloud' not in json.dumps(job)


# ---- a Workflow agent's SubagentStop capture -----------------------------------

FRAME_REQUEST = ('[Workflow harness — user request] The harness relays, verbatim and indented below, '
                 'the user request that triggered this workflow run.\n  Yes, fix docs')
FRAME_TASK = ('[Workflow harness — computed task] The task text below was computed at runtime by a '
              'workflow script.\n  HARD RULES\n  - never touch the vermilion lantern')
# A brief with no automated opening: only the job label keeps it out.
PLAIN_BRIEF = 'Read the scope doc first, then fix the onyx turbine in capture.'


def _subagent_job(tmp_path):
    """The real SubagentStop capture of a Workflow agent's transcript."""
    path = _write(tmp_path, 'agent-a1.jsonl', [
        _user_row(FRAME_REQUEST), _user_row(FRAME_TASK), _user_row(PLAIN_BRIEF),
        _blob_row('user', FRAME_REQUEST + '\n' + FRAME_TASK),
        _assistant_row('Read the files. ' + 'y' * 300)])
    out = session_capture.hook_main(json.dumps({
        'hook_event_name': 'SubagentStop', 'session_id': '99999999-2222-3333-4444-555555555555',
        'agent_id': 'a1', 'agent_transcript_path': str(path), 'cwd': str(tmp_path),
        'transcript_path': str(tmp_path / 'parent.jsonl')}), 'claude_code')
    assert out.get('due'), out
    jobs = session_capture.queued_jobs()
    assert len(jobs) == 1, jobs
    return jobs[0]


def test_a_subagent_job_capture_queues_no_owner_lines(tmp_path, monkeypatch):
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'cap'))
    job_path = _subagent_job(tmp_path)
    job = json.loads(job_path.read_text())
    assert session_capture._job_origin(job) == 'job'
    assert 'owner_lines' not in job, job.get('owner_lines')
    payload, _ = _drain_job(job_path)
    assert 'owner_lines' not in payload['verbatim'], payload['verbatim'].get('owner_lines')


def test_only_my_words_never_matches_a_workflow_brief(at_the_key, monkeypatch, tmp_path):
    configure_hub(monkeypatch, at_the_key)
    monkeypatch.setattr(embed, '_cosine_candidates', lambda q, **kw: [])
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'cap'))
    payload, _ = _drain_job(_subagent_job(tmp_path))
    _land(payload)
    for mode in ('literal', 'hybrid'):
        for query in ('vermilion lantern', 'Yes, fix docs', 'onyx turbine'):
            out = embed.hybrid_search(query, mode=mode, owner_words_only=True)['results']
            assert not out, (mode, query, [r.get('quote') for r in out])


# ---- a T3 delegated thread -----------------------------------------------------

def test_a_t3_delegated_thread_queues_no_owner_lines(tmp_path, monkeypatch):
    """Through the real hook and enqueue: the thread T3 reports for the
    session starts with the delegated prefix, and the session has no lineage."""
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'cap'))
    thread = _t3.DELEGATED_THREAD_PREFIX + 'command%3Amcp%3Aabc%3Adelegate-task%3Adef'
    with mock.patch('khipu.t3.thread_for_session', return_value=thread):
        job_path = _queue(tmp_path, [
            _user_row('One addition from testing the live commands just now, please also do X'),
            _assistant_row('Doing X. ' + 'y' * 300)])
    job = json.loads(job_path.read_text())
    assert job.get('t3_thread_id') == thread and not job.get('parent_session_id')
    assert job.get('origin') == 'job'
    assert 'owner_lines' not in job, job.get('owner_lines')
    payload, _ = _drain_job(job_path)
    assert 'owner_lines' not in payload['verbatim']


def test_a_recorded_job_origin_queues_no_owner_lines(tmp_path, monkeypatch):
    """KHIPU_JOB=1 (state origin ``job``) with no lineage and no T3 thread."""
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'cap'))
    monkeypatch.setenv('KHIPU_JOB', '1')
    job = json.loads(_queue(tmp_path, [_user_row('Build the walnut gauge from the brief'),
                                       _assistant_row('Building. ' + 'y' * 300)]).read_text())
    assert job.get('origin') == 'job' and not job.get('parent_session_id')
    assert 'owner_lines' not in job, job.get('owner_lines')


# ---- messages queued while a turn was running ----------------------------------

QUEUED = 'Also check the idle sweep path for the copper kettle before you finish.'
PEER_BODY = 'Your brief changed: also run the doctor on the slate harbour. ' * 3


def _queued_att(prompt, origin):
    return {'type': 'attachment', 'uuid': 'u-' + str(len(prompt)), 'sessionId': 's',
            'attachment': {'type': 'queued_command', 'prompt': prompt, 'commandMode': 'prompt',
                           'timestamp': 't', 'origin': origin}}


def _queued_rows():
    return [_user_row('Please fix the capture window first.'),
            _assistant_row('Reading the capture code now. ' + 'y' * 300),
            _queued_att(QUEUED, {'kind': 'human'}),
            _blob_row('user', 'The user sent a new message while you were working:\n' + QUEUED),
            _queued_att(f'<agent-message from="orch" name="lane">{PEER_BODY}</agent-message>',
                        {'kind': 'peer', 'from': 'orch', 'name': 'lane'}),
            _assistant_row('Checked the idle sweep as well.')]


def test_the_owners_queued_message_is_typed_once(tmp_path, monkeypatch):
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'cap'))
    typed = _typed(_write(tmp_path, 'q.jsonl', _queued_rows()))
    assert sum(t.count(QUEUED) for t in typed) == 1, typed
    job = json.loads(_queue(tmp_path, _queued_rows()).read_text())
    assert sum(t.count(QUEUED) for t in job.get('owner_lines') or []) == 1, job.get('owner_lines')
    payload, _ = _drain_job(_queue_again(tmp_path, job))
    assert QUEUED in payload['verbatim']['owner_lines']


def _queue_again(tmp_path, job):
    path = tmp_path / 'requeued.json'
    path.write_text(json.dumps(job))
    return path


def test_another_sessions_queued_message_is_not_typed(tmp_path, monkeypatch):
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'cap'))
    typed = _typed(_write(tmp_path, 'q.jsonl', _queued_rows()))
    assert not any('slate harbour' in t for t in typed), typed
    job = json.loads(_queue(tmp_path, _queued_rows()).read_text())
    assert 'slate harbour' in job['transcript']           # kept as a relayed message is
    assert not any('slate harbour' in t for t in job.get('owner_lines') or []), job.get('owner_lines')


# ---- an ordinary conversation still keeps its typed lines ----------------------

def test_an_ordinary_conversation_keeps_its_typed_lines(tmp_path, monkeypatch):
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'cap'))
    job_path = _queue(tmp_path, [_user_row('look at the juniper ledger first'), _assistant_row(REPLY),
                                 _user_row('then report back on the hazel spindle'),
                                 _assistant_row('Reported.')])
    job = json.loads(job_path.read_text())
    assert session_capture._job_origin(job) == 'conversation' and 'origin' not in job
    want = ['look at the juniper ledger first', 'then report back on the hazel spindle']
    assert job['owner_lines'] == want
    payload, _ = _drain_job(job_path)
    assert payload['verbatim']['owner_lines'] == want
    assert extract.owner_lines(want) == want


# ---- a T3 thread launched as another thread's child ------------------------------

from tests.test_t3_owner_confirm import T3  # noqa: E402

T3_SID = '11111111-2222-3333-4444-555555555555'


def _t3_thread(tmp_path, monkeypatch, *, parent=None):
    """A synthetic T3 record: an ordinary thread id for the session, with
    ``lineage.parentThreadId`` set when ``parent`` is given."""
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'cap'))
    monkeypatch.setattr(session_capture, 'ancestor_pids', lambda: [])
    path = tmp_path / 'synthetic-t3.sqlite'
    T3(path).thread('ordinary-thread-560', T3_SID, parent=parent)
    monkeypatch.setenv('KHIPU_T3_DB', str(path))
    assert _t3.thread_for_session(T3_SID) == 'ordinary-thread-560'
    assert _t3.delegated_thread_result('ordinary-thread-560') == (bool(parent), True)
    return 'ordinary-thread-560'


def test_launched_t3_child_does_not_keep_its_brief(tmp_path, monkeypatch):
    _t3_thread(tmp_path, monkeypatch, parent='parent-thread-560')
    brief = 'Read the scope and repair the cerulean windmill before reporting back.'
    job_path = _queue(tmp_path, [_user_row(brief), _assistant_row('Working. ' + 'x' * 350)])
    job = json.loads(job_path.read_text())
    assert job.get('origin') == 'job' and not job.get('parent_session_id'), job
    assert 'owner_lines' not in job, job.get('owner_lines')
    payload, _ = _drain_job(job_path)
    assert not payload['verbatim'].get('owner_lines'), payload['verbatim'].get('owner_lines')


def test_a_top_level_t3_thread_keeps_its_typed_lines(tmp_path, monkeypatch):
    thread = _t3_thread(tmp_path, monkeypatch)
    typed = 'Please keep the saffron lighthouse sentence I typed in my own T3 chat.'
    job = json.loads(_queue(tmp_path, [_user_row(typed), _assistant_row('Checking. ' + 'x' * 350)]).read_text())
    assert job.get('t3_thread_id') == thread and 'origin' not in job
    assert job.get('owner_lines') == [typed]


def test_an_unreadable_t3_record_keeps_the_typed_lines(tmp_path, monkeypatch):
    """T3's record cannot be read (no database file, or the read fails): not
    definitive, so the session stays a conversation and keeps its lines."""
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'cap'))
    monkeypatch.setenv('KHIPU_T3_DB', str(tmp_path / 'no-such-t3.sqlite'))
    typed = 'Keep the indigo harbour line even when T3 cannot be read.'
    rows = [_user_row(typed), _assistant_row('Checking. ' + 'x' * 350)]
    assert _t3.delegated_thread_result('ordinary-thread-560') == (False, False)
    with mock.patch('khipu.t3.thread_for_session', return_value='ordinary-thread-560'):
        job = json.loads(_queue(tmp_path, rows).read_text())
    assert job.get('t3_thread_id') == 'ordinary-thread-560' and 'origin' not in job
    assert job.get('owner_lines') == [typed]
    for p in session_capture.queued_jobs():
        p.unlink()
    with mock.patch('khipu.t3.thread_for_session', return_value='ordinary-thread-561'), \
            mock.patch('khipu.t3.delegated_thread_result', side_effect=RuntimeError('boom')):
        job = json.loads(_queue(tmp_path, rows, sid='22222222-2222-3333-4444-555555555555').read_text())
    assert 'origin' not in job and job.get('owner_lines') == [typed]


# ---- the 14,000-character split -------------------------------------------------

def _split_jobs(tmp_path, rows, sid='split-560'):
    path = tmp_path / f'{sid}.jsonl'
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    out = session_capture.hook_main(json.dumps({'hook_event_name': 'SessionEnd', 'session_id': sid,
                                                'cwd': str(tmp_path), 'transcript_path': str(path)}),
                                    'claude_code')
    assert out.get('due'), out
    return [json.loads(p.read_text()) for p in session_capture.queued_jobs()]


def test_split_between_adjacent_user_turns_does_not_drop_short_typed_turn(tmp_path, monkeypatch):
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'cap'))
    monkeypatch.setattr(session_capture, 'ancestor_pids', lambda: [])
    first = 'first typed message ' + 'a' * (13890 - len('first typed message '))
    second = 'Please keep the entire copper meadow sentence. ' + 'b' * 250
    jobs = _split_jobs(tmp_path, [_user_row(first), _user_row(second), _assistant_row('Checked. ' + 'x' * 350)])
    assert len(jobs) == 2
    assert second in [line for job in jobs for line in job.get('owner_lines', [])], \
        [job.get('owner_lines') for job in jobs]


def test_a_message_cut_at_the_split_appears_once_in_the_part_where_it_starts(tmp_path, monkeypatch):
    """The cut falls inside the message's first 200 characters: it is kept
    once, in the first part (where its opening is), and in no other."""
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'cap'))
    first = 'opening typed message ' + 'a' * (13900 - len('opening typed message '))
    second = 'Keep the whole amber orchard sentence exactly once. ' + 'c' * 180
    jobs = _split_jobs(tmp_path, [_user_row(first), _user_row(second), _assistant_row('Checked. ' + 'x' * 350)])
    assert len(jobs) == 2
    head = second.strip()[:200]
    assert head not in jobs[0]['transcript'] and head not in jobs[1]['transcript']  # the cut is in the probe
    per_part = [job.get('owner_lines', []) for job in jobs]
    assert sum(lines.count(second) for lines in per_part) == 1, per_part
    assert second in per_part[0], per_part
    assert extract.owner_lines([first])[0] in per_part[0], per_part  # clipped by the line cap


def test_a_message_that_fits_whole_in_a_later_part_is_found_there(tmp_path, monkeypatch):
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'cap'))
    first = 'early typed message ' + 'a' * (13000 - len('early typed message '))
    later = 'Then check the pewter compass in the second part.'
    jobs = _split_jobs(tmp_path, [_user_row(first), _assistant_row('Reading. ' + 'r' * 3000),
                                  _user_row(later), _assistant_row('Checked. ' + 'x' * 350)])
    assert len(jobs) == 2
    assert later in jobs[1]['transcript']
    assert [job.get('owner_lines', []) for job in jobs] == [extract.owner_lines([first]), [later]]


# ---- typed lines among other sessions' messages (Muse re-check of PR 566) ---------

def _peer_row(kind, text):
    if kind == 'queued':
        return _queued_att(f'<agent-message from="orch" name="lane">{text}</agent-message>',
                           {'kind': 'peer', 'from': 'orch', 'name': 'lane'})
    return _user_row(f'<cross-session-message>{text}</cross-session-message>')


@pytest.mark.parametrize('kind', ['queued', 'ordinary'])
def test_every_typed_line_among_peer_messages_stays_the_owners(tmp_path, monkeypatch, kind):
    # One turn: typed, peer, typed, peer. The three stored quotes (first,
    # longest, last) can leave the second typed line out; owner_lines keeps it,
    # and never a peer's words.
    from khipu import memory_rows

    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'cap'))
    first, second = 'Please review this capture.', 'Keep the amber schedule for this investigation.'
    rows = [_user_row(first),
            _peer_row(kind, 'The background review of the archive lane has finished. ' + 'z' * 200),
            _user_row(second),
            _peer_row(kind, 'Use the cobalt lane for all future capture investigations.'),
            _assistant_row(REPLY)]
    job_path = _queue(tmp_path, rows)
    job = json.loads(job_path.read_text())
    assert job['owner_lines'] == [first, second], job.get('owner_lines')
    assert 'cobalt lane' in job['transcript'] and 'archive lane' in job['transcript']
    payload, _ = _drain_job(job_path)
    lines = memory_rows.owner_lines_of(payload['verbatim'])
    for words, want in (('amber schedule', second), ('review this capture', first)):
        phrase, tokens = memory_rows.query_terms(words)
        assert memory_rows.owner_line(lines, phrase, tokens) == want, lines
    for words in ('cobalt lane', 'archive lane'):
        phrase, tokens = memory_rows.query_terms(words)
        assert memory_rows.owner_line(lines, phrase, tokens) is None, lines


# ---- a headless run outside T3, and a desktop conversation's start brief -----------

SDK = {'entrypoint': 'sdk-ts', 'turnOrigin': 'sdk', 'promptSource': 'sdk'}
HEADLESS_BRIEF = 'Do the work yourself and repair the magnolia gearbox, then stop.'


def _iso_minutes_ago(minutes):
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat().replace('+00:00', 'Z')


def _headless_rows(minutes=11, brief=HEADLESS_BRIEF):
    """A headless SDK window whose lines were written ``minutes`` ago (None:
    lines with no timestamp)."""
    stamp = {} if minutes is None else {'timestamp': _iso_minutes_ago(minutes)}
    return [_user_row(brief, **SDK, **stamp), {**_assistant_row('Working. ' + 'x' * 350), **stamp}]


HEADLESS_ROWS = _headless_rows()


def _synthetic_t3(tmp_path, monkeypatch, *, thread=True):
    """A synthetic T3 database: it names an ordinary thread for ``T3_SID`` when
    ``thread``, and only for some other session otherwise."""
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'cap'))
    monkeypatch.setattr(session_capture, 'ancestor_pids', lambda: [])
    path = tmp_path / 'synthetic-t3-headless.sqlite'
    db = T3(path)
    if thread:
        db.thread('ordinary-thread-561', T3_SID)
    monkeypatch.setenv('KHIPU_T3_DB', str(path))
    return db


def _assert_a_job_without_owner_lines(job):
    assert job.get('origin') == 'job' and 'owner_lines' not in job, job
    assert session_capture._job_origin(job) == 'job'


def test_a_headless_sdk_run_with_no_t3_database_keeps_no_owner_lines(tmp_path, monkeypatch):
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'cap'))
    monkeypatch.setenv('KHIPU_T3_DB', str(tmp_path / 'no-t3-here.sqlite'))
    job_path = _queue(tmp_path, HEADLESS_ROWS)
    _assert_a_job_without_owner_lines(json.loads(job_path.read_text()))
    payload, _ = _drain_job(job_path)
    assert not payload['verbatim'].get('owner_lines')


def test_a_headless_sdk_run_t3_has_no_thread_for_keeps_no_owner_lines(tmp_path, monkeypatch):
    _synthetic_t3(tmp_path, monkeypatch, thread=False)
    _assert_a_job_without_owner_lines(json.loads(_queue(tmp_path, HEADLESS_ROWS).read_text()))


def test_the_same_lines_in_a_thread_t3_names_are_the_owners(tmp_path, monkeypatch):
    _synthetic_t3(tmp_path, monkeypatch, thread=True)
    job = json.loads(_queue(tmp_path, HEADLESS_ROWS).read_text())
    assert job.get('t3_thread_id') == 'ordinary-thread-561' and 'origin' not in job
    assert job.get('owner_lines') == [HEADLESS_BRIEF]


def test_an_unreadable_t3_database_keeps_a_headless_looking_runs_lines(tmp_path, monkeypatch):
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'cap'))
    broken = tmp_path / 'broken-t3.sqlite'
    broken.write_bytes(b'this is not a sqlite database' * 50)
    monkeypatch.setenv('KHIPU_T3_DB', str(broken))
    assert _t3.session_thread_ids_result(T3_SID) == (None, False)
    job = json.loads(_queue(tmp_path, HEADLESS_ROWS).read_text())
    assert 'origin' not in job and job.get('owner_lines') == [HEADLESS_BRIEF]


def test_a_headless_run_swept_while_idle_keeps_no_owner_lines(tmp_path, monkeypatch):
    import os
    import time

    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'cap'))
    monkeypatch.setenv('KHIPU_T3_DB', str(tmp_path / 'no-t3-here.sqlite'))
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('KHIPU_PARENT_SESSION', '')
    monkeypatch.setattr('khipu.identity.resolve_repo_root',
                        lambda cwd: {'repo_root': '/repo', 'project': 'acme/project', 'is_worktree': False})
    sid = 'aaaaaaaa-2222-3333-4444-555555555555'
    path = tmp_path / '.claude' / 'projects' / 'p' / f'{sid}.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r) + '\n' for r in HEADLESS_ROWS))
    old = time.time() - session_capture.MIN_MINUTES * 60 - 60
    os.utime(path, (old, old))
    session_capture.save_state('claude_code', sid, {
        'offset': 0, 'last_ts': old - 100, 'captures': 0, 'seen_ts': old,
        'seen_end': path.stat().st_size, 'transcript_path': str(path), 'cwd': str(tmp_path)})
    out = session_capture.sweep_idle(now=time.time())
    assert out['jobs'] == 1, out
    job = json.loads(session_capture.queued_jobs()[0].read_text())
    assert job['event'] == 'idle_sweep'
    _assert_a_job_without_owner_lines(job)


def test_in_a_desktop_conversation_only_the_humans_line_is_an_owner_line(tmp_path, monkeypatch):
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'cap'))
    monkeypatch.setenv('KHIPU_T3_DB', str(tmp_path / 'no-t3-here.sqlite'))
    desk = {'entrypoint': 'claude-desktop'}
    start = 'You are a dispatched agent on the quartz repo. Do the work yourself.'
    typed = 'Now also check the lapis hinge before you stop.'
    rows = [_user_row(start, turnOrigin='sdk', **desk), _assistant_row(REPLY),
            _user_row(typed, turnOrigin='human', **desk), _assistant_row('Checked.')]
    assert _typed(_write(tmp_path, 'desk.jsonl', rows)) == [typed]
    job = json.loads(_queue(tmp_path, rows).read_text())
    assert job.get('owner_lines') == [typed], job.get('owner_lines')


def test_lines_with_no_origin_fields_are_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'cap'))
    monkeypatch.setenv('KHIPU_T3_DB', str(tmp_path / 'no-t3-here.sqlite'))
    rows = [_user_row('Check the walnut valve for me.'), _assistant_row(REPLY)]
    job = json.loads(_queue(tmp_path, rows).read_text())
    assert 'origin' not in job and job.get('owner_lines') == ['Check the walnut valve for me.']


# ---- round 2 on #577: a "no thread" answer read before T3 could name the thread --

def test_a_young_sdk_window_t3_has_not_named_yet_keeps_its_lines(tmp_path, monkeypatch):
    """T3 present, no thread for the session yet, the window two minutes old:
    T3 may still write the row, so the lines stay and it is not a job."""
    _synthetic_t3(tmp_path, monkeypatch, thread=False)
    job = json.loads(_queue(tmp_path, _headless_rows(2)).read_text())
    assert 'origin' not in job and session_capture._job_origin(job) == 'conversation', job
    assert job.get('owner_lines') == [HEADLESS_BRIEF]


def test_the_same_window_eleven_minutes_old_is_a_job(tmp_path, monkeypatch):
    _synthetic_t3(tmp_path, monkeypatch, thread=False)
    _assert_a_job_without_owner_lines(json.loads(_queue(tmp_path, _headless_rows(11)).read_text()))


def test_a_window_of_unknown_age_keeps_its_lines_when_t3_is_installed(tmp_path, monkeypatch):
    _synthetic_t3(tmp_path, monkeypatch, thread=False)
    job = json.loads(_queue(tmp_path, _headless_rows(None)).read_text())
    assert 'origin' not in job and job.get('owner_lines') == [HEADLESS_BRIEF]


def test_with_no_t3_installed_age_does_not_matter(tmp_path, monkeypatch):
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'cap'))
    monkeypatch.setenv('KHIPU_T3_DB', str(tmp_path / 'no-t3-here.sqlite'))
    for n, minutes in enumerate((2, None)):
        sid = f'bbbbbbbb-2222-3333-4444-55555555555{n}'
        _assert_a_job_without_owner_lines(json.loads(_queue(tmp_path, _headless_rows(minutes), sid=sid).read_text()))
        for p in session_capture.queued_jobs():
            p.unlink()


def test_a_new_t3_thread_named_after_its_first_capture_keeps_both_windows(tmp_path, monkeypatch):
    """The first due capture runs before T3 names the thread; the next runs
    after. Both windows are the owner's."""
    db = _synthetic_t3(tmp_path, monkeypatch, thread=False)
    first = 'Start on the garnet ledger for me, please.'
    job = json.loads(_queue(tmp_path, _headless_rows(1, first)).read_text())
    assert 'origin' not in job and job.get('owner_lines') == [first]
    for p in session_capture.queued_jobs():
        p.unlink()
    db.thread('late-thread-577', T3_SID)
    session_capture.save_state('claude_code', T3_SID, {
        **session_capture.load_state('claude_code', T3_SID), 't3_lookup_at': 0})  # past the negative cache
    second = 'Then check the garnet ledger totals too.'
    path = tmp_path / '.claude' / 'projects' / 'p' / f'{T3_SID}.jsonl'
    with path.open('a') as f:
        for r in _headless_rows(11, second):   # old enough that only the named thread keeps it
            f.write(json.dumps(r) + '\n')
    out = session_capture.hook_main(json.dumps({'hook_event_name': 'SessionEnd', 'session_id': T3_SID,
                                                'cwd': str(tmp_path), 'transcript_path': str(path)}),
                                    'claude_code')
    assert out.get('due'), out
    job = json.loads(session_capture.queued_jobs()[0].read_text())
    assert 'origin' not in job and job.get('owner_lines') == [second], job


def test_the_age_rule_reads_the_windows_first_line(tmp_path):
    path = _write(tmp_path, 'w.jsonl', _headless_rows(30) + _headless_rows(1, 'a later line'))
    stats = {}
    session_capture.read_window(path, 0, seen=(), stats=stats)
    import time
    assert 29 * 60 < time.time() - stats['first_line_ts'] < 31 * 60
    session_capture.read_window(_write(tmp_path, 'n.jsonl', _headless_rows(None)), 0, seen=(), stats=stats)
    assert 'first_line_ts' not in stats


# ---- round 2 on #577: quotes are not the owner's words when the row has the list --

from khipu import memory_rows  # noqa: E402

DESK = {'entrypoint': 'claude-desktop'}
DESK_START = 'You are a dispatched agent on the quartz anvil repo. Do the work yourself.'
DESK_TYPED = 'Now also check the lapis hinge before you stop.'
DESK_ROWS = [_user_row(DESK_START, turnOrigin='sdk', **DESK), _assistant_row(REPLY),
             _user_row(DESK_TYPED, turnOrigin='human', **DESK), _assistant_row('Checked.')]


def _owner_match(verbatim, words):
    phrase, tokens = memory_rows.query_terms(words)
    return memory_rows.owner_line(memory_rows.owner_lines_of(verbatim), phrase, tokens)


def test_a_desktop_start_brief_in_quotes_is_not_the_owners_words(tmp_path, monkeypatch):
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'cap'))
    monkeypatch.setenv('KHIPU_T3_DB', str(tmp_path / 'no-t3-here.sqlite'))
    payload, _ = _drain_job(_queue(tmp_path, DESK_ROWS))
    vb = payload['verbatim']
    assert DESK_START in vb['quotes'] and vb['owner_lines'] == [DESK_TYPED]
    assert _owner_match(vb, 'quartz anvil') is None
    assert _owner_match(vb, 'lapis hinge') == DESK_TYPED


def test_a_conversation_with_nothing_typed_stores_an_empty_list(tmp_path, monkeypatch):
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'cap'))
    monkeypatch.setenv('KHIPU_T3_DB', str(tmp_path / 'no-t3-here.sqlite'))
    rows = [_user_row(DESK_START, turnOrigin='sdk', **DESK), _assistant_row(REPLY)]
    job_path = _queue(tmp_path, rows)
    assert json.loads(job_path.read_text())['owner_lines'] == []
    payload, _ = _drain_job(job_path)
    assert payload['verbatim']['owner_lines'] == [] and DESK_START in payload['verbatim']['quotes']
    assert _owner_match(payload['verbatim'], 'quartz anvil') is None


def test_owner_lines_of_reads_the_list_when_the_row_has_it():
    typed_looking = 'please keep the violet reset intact'
    assert memory_rows.owner_lines_of({'quotes': [typed_looking], 'owner_lines': []}) == []
    assert _owner_match({'quotes': [typed_looking], 'owner_lines': []}, 'violet reset') is None
    # a row without the key: today's rule, its quotes
    assert _owner_match({'quotes': [typed_looking]}, 'violet reset') == typed_looking
    assert _owner_match(json.dumps({'quotes': [typed_looking]}), 'violet reset') == typed_looking


def _plant(hub, verbatim, *, parent=None, raw=None, session='claude_code:cccccccc-0000-0000-0000-000000000001'):
    conn = hub.connect()
    conn.execute("INSERT INTO episodes (ts,summary,raw,session_id,project,harness,parent_session_id,verbatim) "
                 "VALUES (now(),'Work.',%s::jsonb,%s,'khipu','claude_code',%s,%s::jsonb)",
                 (json.dumps(raw or {}), session, parent, json.dumps(verbatim)))
    conn.commit()
    return str(conn.execute('SELECT max(id) FROM episodes').fetchone()[0])


def _owner_hits(monkeypatch, ids, words):
    """``hybrid_search(owner_words_only=True)`` ids in both modes: literal (the
    owner-words SQL leg) and hybrid, where every row is a cosine candidate too
    (so the check after fusion, ``keep_owner_matches``, sees each)."""
    cos = [dict(kind='episode', id=i, score=.9, rank_text='x', label='x', snippet='x') for i in ids]
    monkeypatch.setattr(embed, '_cosine_candidates', lambda q, **kw: kw['timing'].update(
        embed_ms=0.0, cosine_ms=0.0) or list(cos))
    return {mode: [r['id'] for r in embed.hybrid_search(words, mode=mode, owner_words_only=True)['results']]
            for mode in ('literal', 'hybrid')}


def test_only_my_words_on_the_hub_finds_the_typed_line_not_the_start_brief(at_the_key, monkeypatch, tmp_path):
    configure_hub(monkeypatch, at_the_key)
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'cap'))
    monkeypatch.setenv('KHIPU_T3_DB', str(tmp_path / 'no-t3-here.sqlite'))
    desk = _land(_drain_job(_queue(tmp_path, DESK_ROWS))[0])
    assert _owner_hits(monkeypatch, [desk], 'quartz anvil') == {'literal': [], 'hybrid': []}
    assert _owner_hits(monkeypatch, [desk], 'lapis hinge') == {'literal': [desk], 'hybrid': [desk]}


def _plant(hub, verbatim, n, *, parent=None, raw=None):
    conn = hub.connect()
    conn.execute("INSERT INTO episodes (ts,summary,raw,session_id,project,harness,parent_session_id,verbatim) "
                 "VALUES (now(),'Work.',%s::jsonb,%s,'khipu','claude_code',%s,%s::jsonb)",
                 (json.dumps(raw or {}), f'claude_code:cccccccc-0000-0000-0000-00000000000{n}', parent,
                  json.dumps(verbatim)))
    conn.commit()
    return str(conn.execute('SELECT max(id) FROM episodes').fetchone()[0])


def test_only_my_words_on_the_hub_reads_the_list_and_never_a_job(hub, monkeypatch, tmp_path):
    from tests.test_search_indexed_words import install_words

    configure_hub(monkeypatch, hub)
    install_words(hub, tmp_path)
    empty = _plant(hub, {'quotes': ['please keep the cobalt reset intact'], 'owner_lines': []}, 1)
    old = _plant(hub, {'quotes': ['please keep the amber reset intact']}, 2)
    child = _plant(hub, {'quotes': ['please keep the onyx reset intact']}, 3,
                   parent='claude_code:cccccccc-0000-0000-0000-000000000099')
    marked = _plant(hub, {'quotes': ['please keep the jade reset intact']}, 4, raw={'session_origin': 'job'})
    ids = [empty, old, child, marked]
    with hub.connect() as conn, conn.cursor() as cur:
        facts = memory_rows.episode_facts(cur, ids, owner_lines=True)
    assert {i: facts[i]['owner_lines'] for i in (empty, child, marked)} == {empty: [], child: [], marked: []}
    for words, want in (('cobalt reset', []), ('onyx reset', []), ('jade reset', []), ('amber reset', [old])):
        assert _owner_hits(monkeypatch, ids, words) == {'literal': want, 'hybrid': want}, words


def test_a_merge_keeps_each_sides_owner_words():
    """A row captured before the list was stored, merged with one that has it:
    the old side's owner words (its quotes) join the list, the new side's
    brief in quotes does not."""
    from khipu import capture as cap
    from tests.test_audit1010_capb import Cursor, episode

    old = episode(verbatim={'quotes': ['please keep the amber reset intact']})
    cur = Cursor({1: old})
    incoming = episode(ts='2026-10-10T00:01:00Z', session_id='codex:two', transcript_range='100:200',
                       verbatim={'quotes': [DESK_START, DESK_TYPED], 'owner_lines': [DESK_TYPED]})
    with mock.patch.object(cap, '_reembed_merged_episode'), \
            mock.patch('khipu.deliverables.insert_deliverables_from_episode', return_value=0), \
            mock.patch('khipu.topic_graph.persist_capture_graph', return_value={}), \
            mock.patch('khipu.embed.embed_one', side_effect=RuntimeError('offline')):
        assert cap._merge_into_episode(cur, 1, incoming, matched_via='jaccard', score=.9)
    vb = cur.episodes[1]['verbatim']
    assert vb['owner_lines'] == ['please keep the amber reset intact', DESK_TYPED]
    assert _owner_match(vb, 'amber reset') and _owner_match(vb, 'lapis hinge') == DESK_TYPED
    assert _owner_match(vb, 'quartz anvil') is None
    assert cur.episodes[1]['raw']['merged_from'][0]['payload']['verbatim'] == incoming['verbatim']


# ---- round 3 on #577: the session's age, and a job side in a merge ---------------

def _second_window(tmp_path, rows):
    """Append ``rows`` to T3_SID's transcript and run a due SessionEnd: the
    queued job for that window."""
    for p in session_capture.queued_jobs():
        p.unlink()
    path = tmp_path / '.claude' / 'projects' / 'p' / f'{T3_SID}.jsonl'
    with path.open('a') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')
    out = session_capture.hook_main(json.dumps({'hook_event_name': 'SessionEnd', 'session_id': T3_SID,
                                                'cwd': str(tmp_path), 'transcript_path': str(path)}),
                                    'claude_code')
    assert out.get('due'), out
    return json.loads(session_capture.queued_jobs()[0].read_text())


def test_a_never_named_sdk_session_thirty_minutes_old_is_a_job_in_every_window(tmp_path, monkeypatch):
    """Muse's probe: the session started 30 minutes ago and T3 never named it.
    Window 1 is a job; window 2, whose first line is 2 minutes old, is too:
    the grace counts from the session's start, not the window's."""
    _synthetic_t3(tmp_path, monkeypatch, thread=False)
    _assert_a_job_without_owner_lines(json.loads(_queue(tmp_path, _headless_rows(30)).read_text()))
    later = 'Next, polish the hematite sprocket and report back.'
    _assert_a_job_without_owner_lines(_second_window(tmp_path, _headless_rows(2, later)))


def test_a_session_whose_head_is_two_minutes_old_keeps_its_lines_in_a_later_window(tmp_path, monkeypatch):
    _synthetic_t3(tmp_path, monkeypatch, thread=False)
    job = json.loads(_queue(tmp_path, _headless_rows(2)).read_text())
    assert 'origin' not in job and job.get('owner_lines') == [HEADLESS_BRIEF]
    later = 'Then tidy the hematite sprocket notes.'
    job = _second_window(tmp_path, _headless_rows(1, later))
    assert 'origin' not in job and job.get('owner_lines') == [later], job


def test_the_session_start_is_read_from_the_head_only(tmp_path):
    import time

    path = _write(tmp_path, 'h.jsonl', _headless_rows(None) + _headless_rows(30) + _headless_rows(1, 'later'))
    assert 29 * 60 < time.time() - session_capture._session_start_ts(path) < 31 * 60
    # past the first 50 lines the head has no timestamp: unknown, never a full read
    deep = _write(tmp_path, 'd.jsonl', _headless_rows(None) * 30 + _headless_rows(30))
    assert session_capture._session_start_ts(deep) is None
    assert session_capture._session_start_ts(tmp_path / 'missing.jsonl') is None


def _merge(target, incoming):
    from khipu import capture as cap
    from tests.test_audit1010_capb import Cursor

    cur = Cursor({1: target})
    with mock.patch.object(cap, '_reembed_merged_episode'), \
            mock.patch('khipu.deliverables.insert_deliverables_from_episode', return_value=0), \
            mock.patch('khipu.topic_graph.persist_capture_graph', return_value={}), \
            mock.patch('khipu.embed.embed_one', side_effect=RuntimeError('offline')):
        assert cap._merge_into_episode(cur, 1, incoming, matched_via='jaccard', score=.9)
    return cur.episodes[1]['verbatim']


JOB_BRIEF = 'Read the brief and fix the onyx turbine, then stop.'


def test_a_job_merged_into_a_conversation_gives_no_owner_words():
    from tests.test_audit1010_capb import episode

    typed = 'please keep the amber reset intact'
    conv = episode(verbatim={'quotes': [typed], 'owner_lines': [typed]})
    job = episode(ts='2026-10-10T00:01:00Z', session_id='claude_code:job', transcript_range='100:200',
                  session_origin='job', verbatim={'quotes': [JOB_BRIEF]})
    vb = _merge(conv, job)
    assert vb['owner_lines'] == [typed] and JOB_BRIEF in vb['quotes']
    assert _owner_match(vb, 'onyx turbine') is None
    # lineage marks a job too, and a row stored before the list keeps its quotes as its owner words
    old = episode(verbatim={'quotes': [typed]})
    child = episode(ts='2026-10-10T00:01:00Z', session_id='claude_code:child', transcript_range='100:200',
                    parent_session_id='claude_code:parent', verbatim={'quotes': [JOB_BRIEF]})
    vb = _merge(old, child)
    assert vb['owner_lines'] == [typed] and _owner_match(vb, 'onyx turbine') is None
