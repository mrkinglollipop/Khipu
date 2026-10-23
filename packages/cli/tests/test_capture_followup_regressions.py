"""Second capture-audit pass: fake sessions and local queue evidence only."""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from tests.test_capture_audit_regressions import isolated, write, codex, claude, hook  # noqa: F401


def test_claude_started_in_codex_shell_refuses_without_issuer_proof(tmp_path, monkeypatch):
    from khipu import session_capture as sc
    sc.save_state('claude_code', 'mine', {'offset': 0, 'cwd': str(tmp_path)})
    monkeypatch.setenv('CODEX_THREAD_ID', 'outer-codex')
    monkeypatch.setenv('CLAUDE_CODE_SESSION_ID', 'mine')
    with pytest.raises(ValueError, match='cannot tell which session is yours'):
        sc.resolve_session_ref(host_pids=[], cwd=str(tmp_path))


@pytest.mark.parametrize('parallel', [False, True])
def test_pending_notes_all_reach_one_capture(tmp_path, monkeypatch, parallel):
    from khipu import session_capture as sc, extract, capture
    path = write(tmp_path / 'notes.jsonl', [])
    sc.save_state('codex', 'fake-session', {'offset': 0, 'transcript_path': str(path)})
    notes = ['NOTE-ONE decision alpha', 'NOTE-TWO decision beta']
    if parallel:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda note: sc.request_capture_now('codex', 'fake-session', note), notes))
    else:
        for note in notes:
            sc.request_capture_now('codex', 'fake-session', note)
    assert hook(path)['due']
    assert not sc._capture_now_flag_path('codex', 'fake-session').exists()
    monkeypatch.setattr(extract, 'extract_memory', lambda *a, **kw: None)
    sink = Mock(return_value=0)
    monkeypatch.setattr(capture, 'capture', sink)
    assert sc.drain()['captured'] == 1
    saved = sink.call_args.args[0]['verbatim']['note']
    assert all(saved.count(note) == 1 for note in notes), saved


@pytest.mark.parametrize('relocate', [False, True])
@pytest.mark.parametrize('sweep', [False, True])
def test_replaced_history_does_not_repeat_short_turns_or_tools(tmp_path, relocate, sweep):
    from khipu import session_capture as sc
    rows = [claude('user', 'yes do it'), claude('assistant', 'done'),
            {'type': 'assistant', 'message': {'role': 'assistant', 'content': [
                {'type': 'tool_use', 'name': 'FakeOldTool', 'input': {}}]}},
            claude('user', 'ship it'), claude('assistant', 'already shipped'),
            claude('assistant', 'Previously stored synthetic explanation. ' * 30)]
    path = write(tmp_path / 'old.jsonl', rows)
    assert hook(path, harness='claude_code')['due']
    new = write(tmp_path / 'replacement.jsonl', rows + [claude('user', 'NEW_REQUIREMENT ' * 40),
                claude('assistant', 'NEW_PLAN ' * 50), claude('user', 'ship it')])
    if relocate:
        assert sc.load_state('claude_code', 'fake-session')['transcript_path'] == str(path)
        path = new
    else:
        os.replace(new, path)
    before = set(sc.queued_jobs())
    if sweep:
        st = sc.load_state('claude_code', 'fake-session')
        st.update(seen_ts=time.time() - 7200, transcript_path=str(path))
        sc.save_state('claude_code', 'fake-session', st)
        os.utime(path, (time.time() - 7200,) * 2)
        result = sc.sweep_idle(now=time.time())
        assert result['jobs'] == 1, result
    else:
        assert hook(path, harness='claude_code')['due']
    job = json.loads((set(sc.queued_jobs()) - before).pop().read_text())
    assert 'yes do it' not in job['transcript']
    assert 'already shipped' not in job['transcript']
    assert 'FakeOldTool' not in job['transcript']
    assert job['transcript'].count('ship it') == 1  # the genuine new repeat survives
    assert job['turns'] == 2
    assert not hook(path, harness='claude_code')['due']


@pytest.mark.parametrize('role,event', [('assistant', 'agent_message'), ('user', 'user_message')])
def test_codex_pairs_differing_boundary_whitespace(tmp_path, role, event):
    from khipu import session_capture as sc
    path = write(tmp_path / 'paired.jsonl', [codex(role, 'same turn\n'),
                 {'type': 'event_msg', 'payload': {'type': event, 'message': 'same turn'}}])
    msgs, _, turns = sc.read_window(path, 0, seen=())
    assert msgs == [(role, 'same turn')]
    assert turns == (1 if role == 'user' else 0)


def test_missing_process_birth_still_drains_as_legacy_claim(tmp_path, monkeypatch):
    from khipu import session_capture as sc, extract, capture
    path = write(tmp_path / 'claim.jsonl', [codex('user', 'Synthetic pending capture ' * 20)])
    assert hook(path)['due']
    monkeypatch.setattr(sc, '_claim_process_start', lambda pid: None)
    monkeypatch.setattr(extract, 'extract_memory', lambda *a, **kw: {'summary': 'Synthetic capture'})
    sink = Mock(return_value=0)
    monkeypatch.setattr(capture, 'capture', sink)
    report = sc.drain()
    assert report['captured'] == 1, report
    sink.assert_called_once()
    assert not sc.queued_jobs()
    assert not list(sc.queue_dir().glob('.owner-*.meta'))


def test_evidence_conflict_surfaces_in_doctor_outbox_status(monkeypatch):
    from khipu import outbox, capture
    from khipu.capture import CaptureEvidenceConflict
    path = outbox.enqueue({'ts': '2026-10-23T12:00:00Z', 'summary': 'Fake legacy collision'})
    before = json.loads(path.read_text())['payload']
    monkeypatch.setattr(capture, 'write_pg', Mock(side_effect=CaptureEvidenceConflict('fake conflict')))
    assert outbox.drain()['deferred'] == 1
    status = outbox.status()
    assert status['evidence_conflicts'] == 1
    assert status['offline_only'] is False
    assert 'evidence' in status['fix'].lower() and 'drain' not in status['fix'].lower()
    assert json.loads(path.read_text())['payload'] == before


def test_replay_prefix_survives_multiple_captures_and_not_due_tail(tmp_path):
    from khipu import session_capture as sc
    rows = [claude('user', 'old short turn'), claude('assistant', 'Original fake answer. ' * 40)]
    path = write(tmp_path / 'prefix.jsonl', rows)
    assert hook(path, harness='claude_code')['due']
    rows += [claude('user', 'second short turn'), claude('assistant', 'Second fake answer. ' * 40)]
    write(path, rows)
    assert hook(path, harness='claude_code')['due']
    # Claude can assign new UUIDs while retaining the same semantic messages.
    copied = [dict(row, uuid=f'new-{index}') for index, row in enumerate(rows)]
    new = write(tmp_path / 'new.jsonl', copied + [claude('user', 'fresh short turn')])
    os.replace(new, path)
    before = set(sc.queued_jobs())
    out = hook(path, harness='claude_code')
    assert not out['due'], out
    assert sc.load_state('claude_code', 'fake-session')['offset'] > 0
    write(path, copied + [claude('user', 'fresh short turn'),
                          claude('assistant', 'Fresh fake answer. ' * 40)])
    assert hook(path, harness='claude_code')['due']
    job = json.loads((set(sc.queued_jobs()) - before).pop().read_text())
    assert 'fresh short turn' in job['transcript']
    assert 'old short turn' not in job['transcript']
    assert 'second short turn' not in job['transcript']
    assert job['turns'] == 1


def test_changed_replacement_prefix_keeps_unseen_short_turns(tmp_path):
    from khipu import session_capture as sc
    path = write(tmp_path / 'changed.jsonl', [claude('user', 'old short turn'),
                 claude('assistant', 'Original fake answer. ' * 40)])
    assert hook(path, harness='claude_code')['due']
    new = write(tmp_path / 'new.jsonl', [claude('user', 'UNSEEN_SHORT_TURN'),
                claude('assistant', 'Replacement fake answer. ' * 40)])
    os.replace(new, path)
    out = hook(path, harness='claude_code')
    assert out['due'], out
    assert 'UNSEEN_SHORT_TURN' in json.loads((sc.queue_dir() / out['queued']).read_text())['transcript']


def test_aged_conflict_keeps_reconciliation_guidance_in_doctor(tmp_path, monkeypatch):
    from khipu import cli, outbox
    # Only the local diagnostic path runs: the fixture forbids external sockets.
    status = {'pending': 1, 'dead': 0, 'oldest_age_s': 7200,
              'evidence_conflicts': 1, 'fix': 'Inspect conflicting evidence; owner reconciliation required.'}
    monkeypatch.setattr(outbox, 'status', lambda: dict(status))
    result = cli._doctor_local_checks(tmp_path)
    assert result['outbox']['fix'] == status['fix']
    assert result['outbox_ok'] is False


def test_legacy_offset_learns_prefix_before_next_replacement(tmp_path):
    from khipu import session_capture as sc
    rows = [claude('user', 'legacy short turn'), claude('assistant', 'Synthetic old answer. ' * 40)]
    path = write(tmp_path / 'legacy.jsonl', rows)
    assert hook(path, harness='claude_code')['due']
    state = sc.load_state('claude_code', 'fake-session')
    state.pop('transcript_prefix', None)
    sc.save_state('claude_code', 'fake-session', state)
    assert not hook(path, harness='claude_code')['due']
    new = write(tmp_path / 'copy.jsonl', rows + [claude('user', 'Synthetic new requirement. ' * 40)])
    os.replace(new, path)
    out = hook(path, harness='claude_code')
    assert out['due'], out
    assert 'legacy short turn' not in json.loads((sc.queue_dir() / out['queued']).read_text())['transcript']


def test_conflict_with_offline_error_words_is_not_only_offline(monkeypatch):
    from khipu import outbox, capture
    outbox.enqueue({'ts': '2026-10-23T12:00:00Z', 'summary': 'Fake legacy collision'})
    monkeypatch.setattr(capture, 'write_pg', Mock(side_effect=capture.CaptureEvidenceConflict('connection refused')))
    assert outbox.drain()['deferred'] == 1
    assert outbox.status()['offline_only'] is False
