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
