"""Hermetic regression probes; expected invariants fail on audited main."""
import json
from pathlib import Path

import psycopg
import pytest
from khipu import capture_identity as ci, db, forget, owner_turns, routing_directions as rd
from khipu import session_capture as sc
from tests import test_forget_ownership as fo
from tests.test_forget_write_gate import P

@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('KHIPU_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'capture'))
    monkeypatch.setenv('KHIPU_OUTBOX', str(tmp_path / 'outbox'))
    monkeypatch.setenv('KHIPU_KEYCHAIN', '0')
    monkeypatch.setenv('KHIPU_TEST', '1')
    monkeypatch.setattr(psycopg, 'connect', lambda *a, **k: pytest.fail('live PG forbidden'))
    db._TABLE_COLUMNS_CACHE.clear()


def _complete_forget(tmp_path, monkeypatch):
    root = tmp_path / 'memory'
    root.mkdir()
    fo.write_lines(root, [P])
    world = {'episodes': {1: dict(P, id=1, ts=fo.BASE, raw=dict(P), deleted=False)},
             'sources': [fo.ForgetOwnershipTest._source(None, 1, P)],
             'sources_enabled': True}
    monkeypatch.setattr(db, 'connect', lambda *a, **kw: fo._Conn(fo._WorldCur(world)))
    return forget.forget_everywhere(1, memory_root=root)


def write_direction(session, words):
    path = rd.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'directions': {'rd-test': {
        'id': 'rd-test', 'session': session, 'words': words, 'status': 'open',
        'occurrence': 1, 'target': None}}}))
    return path


def test_complete_forget_swallows_routing_direction_cleanup_failure(tmp_path, monkeypatch):
    """Invariant: inability to remove a source session's stored words must be incomplete."""
    words = 'SYNTHETIC-PRIVATE-DIRECTION-LOCK-FAILURE'
    path = write_direction('old', words)
    # A real filesystem failure, no cleanup mock: the lock name is an unreadable file kind.
    path.with_suffix('.lock').mkdir()
    result = _complete_forget(tmp_path, monkeypatch)
    assert words in path.read_text()
    assert not result['owner_words']['ok']
    assert not result['complete'], 'forget must report incomplete when owner direction cleanup fails'


def test_complete_forget_ignores_corrupt_routing_direction_store(tmp_path, monkeypatch):
    """Invariant: unreadable/undecodable stored source words cannot be reported clean."""
    words = 'SYNTHETIC-PRIVATE-DIRECTION-TORN-JSON'
    path = write_direction('old', words)
    path.write_text(path.read_text()[:-2])
    result = _complete_forget(tmp_path, monkeypatch)
    assert words in path.read_text()
    assert not result['owner_words']['ok']
    assert not result['complete'], 'forget must report incomplete for undecodable direction words'


def test_merged_source_session_words_survive_complete_forget(tmp_path, monkeypatch):
    """Invariant: known merged sources' session words need cleanup or explicit retention."""
    words = 'SYNTHETIC-PRIVATE-MERGED-SOURCE-WORDS'
    merged = {**P, 'session_id': 'codex:merged', 'transcript_range': '10:20',
              'ts': '2026-10-01T09:01:00Z', 'summary': words}
    raw = {**P, 'merged_from': [{'source_id': ci.payload_sha256(merged), 'payload': merged}]}
    root = tmp_path / 'memory'
    root.mkdir()
    fo.write_lines(root, [P, merged])
    world = {'episodes': {1: dict(P, id=1, ts=fo.BASE, raw=raw, deleted=False)},
             'sources': [fo.ForgetOwnershipTest._source(None, 1, P),
                         {**fo.ForgetOwnershipTest._source(None, 1, merged), 'role': 'merged'}],
             'sources_enabled': True}
    path = write_direction('merged', words)
    result = fo.run_forget(world, 1, root)
    assert result['complete'] is True, result
    assert words not in (root / 'episodes.jsonl').read_text()
    assert words not in path.read_text(), 'forget must scrub known merged source session words before completion'


def test_native_shared_job_is_deleted_while_equivalent_outbox_job_is_protected(tmp_path, monkeypatch):
    """Invariant: retain a queued source another live episode also holds, in either queue."""
    from khipu import outbox
    from unittest.mock import patch
    monkeypatch.setattr(outbox, '_queued_origin', lambda: {'origin': None})
    root = tmp_path / 'memory'
    root.mkdir()
    other = {**P, 'session_id': 'codex:other', 'ts': '2026-10-01T09:02:00Z', 'summary': 'other live'}
    raw_other = {**other, 'merged_from': [{'source_id': ci.payload_sha256(P), 'payload': P}]}
    world = {'episodes': {
        1: dict(P, id=1, ts=fo.BASE, raw=dict(P), deleted=False),
        2: dict(other, id=2, ts=fo.BASE, raw=raw_other, deleted=False)},
        'sources': [fo.ForgetOwnershipTest._source(None, 1, P),
                    fo.ForgetOwnershipTest._source(None, 2, other)], 'sources_enabled': True}
    fo.write_lines(root, [P, other])
    job = sc.enqueue({'ts': P['ts'], 'harness': 'codex', 'session_id': 'old',
                      'transcript_range': P['transcript_range'],
                      'text': 'SYNTHETIC shared transcript'})
    with patch.dict('os.environ', {'KHIPU_OUTBOX': str(root / 'outbox')}):
        queued = outbox.enqueue(dict(P))
        result = fo.run_forget(world, 1, root)
    assert not world['episodes'][2]['deleted']
    assert queued.exists(), result
    assert result['outbox']['ok'] is False
    assert not result['native_jobs']['ok']
    assert job.exists(), 'native source held by another live episode must be retained like outbox source'


@pytest.mark.parametrize('failure', ['owners_unavailable', 'job_changed'])
def test_native_job_is_kept_when_ownership_check_cannot_cover_it(tmp_path, monkeypatch, failure):
    job = sc.enqueue({'ts': P['ts'], 'harness': 'codex', 'session_id': 'old',
                      'transcript_range': P['transcript_range'], 'text': 'synthetic transcript'})
    source = {**ci.source_identity(P), 'ts': P['ts'], 'summary_md5': fo.md5(P['summary'])}

    def ownership(*args, **kwargs):
        if failure == 'owners_unavailable':
            raise RuntimeError('synthetic unavailable ownership')
        data = json.loads(job.read_text())
        data['text'] = 'new transcript bytes after ownership read'
        job.write_text(json.dumps(data))
        return set(), set()

    monkeypatch.setattr(forget, '_job_owners', ownership)
    result = forget._native_job_remainder(P['session_id'], [source], episode_id=1)
    assert job.exists()
    assert not result['ok']
    assert result['removed'] == 0
    reason = 'RuntimeError' if failure == 'owners_unavailable' else 'changed'
    assert reason in str(result['remaining'])


def test_forgotten_native_job_takes_its_retry_state_with_it(tmp_path, monkeypatch):
    job = sc.enqueue({'ts': P['ts'], 'harness': 'codex', 'session_id': 'old',
                      'transcript_range': P['transcript_range'], 'text': 'synthetic transcript'})
    sc._write_retry_state(job, {'disposition': 'held', 'attempts': sc.CAPTURE_MAX_ATTEMPTS,
                                'last_error': 'extract: RuntimeError'})
    source = {**ci.source_identity(P), 'ts': P['ts'], 'summary_md5': fo.md5(P['summary'])}
    monkeypatch.setattr(forget, '_job_owners', lambda *a, **k: (set(), set()))
    result = forget._native_job_remainder(P['session_id'], [source], episode_id=1)
    assert result['removed'] == 1, result
    assert not job.exists()
    assert not sc._retry_state_path(job).exists()
    assert sc._retry_state(job) == {}


@pytest.mark.parametrize('data', [{'directions': []}, {'directions': {'rd-test': {'words': 'private'}}}])
def test_invalid_direction_schema_keeps_forget_incomplete(tmp_path, monkeypatch, data):
    path = write_direction('old', 'synthetic words')
    path.write_text(json.dumps(data))
    result = _complete_forget(tmp_path, monkeypatch)
    assert not result['complete']
    assert not result['owner_words']['ok']
    assert json.loads(path.read_text()) == data


def test_source_row_session_words_are_scrubbed_without_receipt_payload(tmp_path, monkeypatch):
    merged = {**P, 'session_id': 'codex:source-row', 'transcript_range': '10:20',
              'ts': '2026-10-01T09:01:00Z', 'summary': 'synthetic merged source'}
    raw = {**P, 'merged_from': [{'source_id': ci.payload_sha256(merged)}]}
    root = tmp_path / 'memory'
    root.mkdir()
    fo.write_lines(root, [P, merged])
    world = {'episodes': {1: dict(P, id=1, ts=fo.BASE, raw=raw, deleted=False)},
             'sources': [{**fo.ForgetOwnershipTest._source(None, 1, P), 'session_id': P['session_id']},
                         {**fo.ForgetOwnershipTest._source(None, 1, merged),
                          'role': 'merged', 'session_id': merged['session_id']}],
             'sources_enabled': True}
    execute = fo._WorldCur.execute

    def source_sessions(cur, sql, params=None):
        if sql.startswith('SELECT session_id FROM episode_sources'):
            cur._all = [(r['session_id'],) for r in world['sources'] if r['episode_id'] == params[0]]
            return
        return execute(cur, sql, params)

    monkeypatch.setattr(fo._WorldCur, 'execute', source_sessions)
    path = write_direction('source-row', 'synthetic source-row words')
    result = fo.run_forget(world, 1, root)
    assert result['complete'], result
    assert 'synthetic source-row words' not in path.read_text()


@pytest.mark.parametrize('failure', ['corrupt', 'locked'])
def test_bad_directions_do_not_block_other_owner_word_scrubs(tmp_path, monkeypatch, failure):
    from khipu import owner_direction as od
    folder = owner_turns.folder()
    folder.mkdir(parents=True, exist_ok=True)
    briefs = folder / 'briefs.jsonl'
    briefs.write_text(json.dumps({'parent': 'old', 'sha': 'x', 'head': 'FAKE-BRIEF-WORDS'}) + '\n')
    log = od.log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps({'session': 'old', 'words': 'FAKE-LOG-WORDS'}) + '\n')
    temp = folder / '.synthetic.tmp'
    temp.write_text('old FAKE-TEMP-WORDS')
    pending = log.parent / od.OLD_PENDING_NAME
    pending.write_text('FAKE-OLD-WIDENING')
    path = write_direction('old', 'FAKE-DIRECTION-WORDS')
    if failure == 'corrupt':
        path.write_text('{"directions": {"a":')
    else:
        path.with_suffix('.lock').mkdir()
    result = _complete_forget(tmp_path, monkeypatch)
    assert not result['complete'] and not result['owner_words']['ok']
    assert 'FAKE-BRIEF-WORDS' not in briefs.read_text()
    assert 'FAKE-LOG-WORDS' not in log.read_text()
    assert not pending.exists()
    assert not temp.exists()
    assert result['owner_words']['removed']['log_lines'] == 1
    assert 'routing directions' in str(result['partial_errors']['owner_words'])


@pytest.mark.parametrize('role', ['primary', 'merged'])
@pytest.mark.parametrize('holder', ['column', 'raw', 'receipt', 'source'])
@pytest.mark.parametrize('deleted', [False, True])
@pytest.mark.parametrize('linked_action', [False, True])
def test_owner_words_retained_only_for_another_live_session_holder(
        tmp_path, monkeypatch, role, holder, deleted, linked_action):
    from khipu import owner_direction as od
    merged = {**P, 'session_id': 'codex:merged', 'transcript_range': '10:20',
              'ts': '2026-10-01T09:01:00Z', 'summary': 'merged synthetic'}
    shared = P if role == 'primary' else merged
    other = {**P, 'session_id': 'codex:unrelated', 'transcript_range': '20:30',
             'ts': '2026-10-01T09:02:00Z', 'summary': 'separate synthetic live episode'}
    other_raw = dict(other)
    other_session = other['session_id']
    if holder == 'column':
        other_session = shared['session_id'].split(':', 1)[1]
    elif holder == 'raw':
        other_raw['session_id'] = shared['session_id']
    elif holder == 'receipt':
        other_raw['merged_from'] = [{'payload': dict(shared)}]
    source = fo.ForgetOwnershipTest._source(None, 2, other)
    source['session_id'] = shared['session_id'] if holder == 'source' else other['session_id']
    root = tmp_path / 'memory'
    root.mkdir()
    fo.write_lines(root, [P, merged, other])
    world = {'episodes': {
        1: dict(P, id=1, ts=fo.BASE,
                raw={**P, 'merged_from': [{'payload': merged}]}, deleted=False),
        2: dict(other, id=2, ts=fo.BASE, session_id=other_session,
                raw=other_raw, deleted=deleted)},
        'sources': [fo.ForgetOwnershipTest._source(None, 1, P),
                    {**fo.ForgetOwnershipTest._source(None, 1, merged), 'role': 'merged'}, source],
        'sources_enabled': True}
    execute = fo._WorldCur.execute

    def session_holders(cur, sql, params=None):
        if 'AS owner_word_holders' in sql:
            values = [] if deleted else [other_session, other_raw.get('session_id'),
                                        source['session_id']]
            values += [r['payload']['session_id'] for r in other_raw.get('merged_from', [])] if not deleted else []
            cur._all = [(s,) for s in values]
            return
        return execute(cur, sql, params)

    monkeypatch.setattr(fo._WorldCur, 'execute', session_holders)
    session = shared['session_id'].split(':', 1)[1]
    words = 'SYNTHETIC-SHARED-OWNER-WORDS'
    direction = write_direction(session, words)
    folder = owner_turns.folder()
    folder.mkdir(parents=True, exist_ok=True)
    turn = owner_turns.session_file(session)
    turn.write_text(json.dumps({'session': session, 'text': words}) + '\n')
    brief = folder / 'briefs.jsonl'
    brief.write_text(json.dumps({'parent': session, 'sha': 'shared', 'head': words}) + '\n')
    log = od.log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    unique_session = 'merged' if role == 'primary' else 'old'
    action = {'session': session, 'words': words}
    if linked_action:
        action['via_session'] = unique_session
    log.write_text(json.dumps(action) + '\n')
    import os
    temp = folder / '.shared.tmp'
    temp.write_text(session + ' ' + words)
    os.utime(temp, (0, 0))
    unique = owner_turns.session_file(unique_session)
    unique.write_text(json.dumps({'session': unique_session, 'text': 'UNIQUE-OWNER-WORDS'}) + '\n')
    result = fo.run_forget(world, 1, root)
    assert not unique.exists()
    assert (temp.exists() and words in temp.read_text()) == (not deleted)
    assert world['episodes'][2]['deleted'] == deleted
    for path in (direction, turn, brief, log):
        assert (path.exists() and words in path.read_text()) == (not deleted), (path, result)
    assert 'in progress' not in str(result['owner_words'])
    assert result['owner_words']['ok'] == deleted, result
    assert result['complete'] == deleted, result


def test_all_owner_word_cleanup_errors_are_reported_after_attempts(tmp_path, monkeypatch):
    from khipu import owner_direction as od
    folder = owner_turns.folder()
    folder.mkdir(parents=True, exist_ok=True)
    temp = folder / '.synthetic.tmp'
    temp.write_text('old synthetic words')
    write_direction('old', 'synthetic words').write_text('{truncated')

    def fail():
        raise OSError('synthetic failure')

    monkeypatch.setattr(od, 'remove_old_widenings', fail)
    monkeypatch.setattr(od, 'scrub_words', lambda sessions: fail())
    result = forget._owner_words_scan('codex:old', gateway=False)
    assert not result['ok']
    assert not temp.exists()
    errors = str(result['locations'])
    for step in ('routing directions', 'old widenings', 'owner-actions log'):
        assert step in errors


def test_unavailable_session_ownership_preserves_words_and_reports_incomplete(tmp_path, monkeypatch):
    execute = fo._WorldCur.execute
    statements = []

    def unavailable(cur, sql, params=None):
        statements.append(sql)
        if 'AS owner_word_holders' in sql:
            raise RuntimeError('synthetic ownership unavailable')
        return execute(cur, sql, params)

    monkeypatch.setattr(fo._WorldCur, 'execute', unavailable)
    path = write_direction('old', 'SYNTHETIC-PRESERVED-WORDS')
    result = _complete_forget(tmp_path, monkeypatch)
    assert not result['complete'] and not result['owner_words']['ok']
    assert 'SYNTHETIC-PRESERVED-WORDS' in path.read_text()
    assert 'ROLLBACK TO SAVEPOINT owner_word_ownership' in statements
    assert 'ownership could not be checked' in str(result['partial_errors']['owner_words'])
