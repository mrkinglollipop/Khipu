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
