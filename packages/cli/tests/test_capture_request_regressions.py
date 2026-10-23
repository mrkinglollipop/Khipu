"""Capture requests use synthetic process identities and a disposable queue."""
import json
import time

import pytest

from tests.test_capture_audit_regressions import isolated, write, hook  # noqa: F401


def mixed_sessions(tmp_path, monkeypatch):
    from khipu import session_capture as sc

    monkeypatch.setenv('CODEX_THREAD_ID', 'cx1')
    monkeypatch.setenv('CLAUDE_CODE_SESSION_ID', 'cl1')
    for harness, sid in [('codex', 'cx1'), ('claude_code', 'cl1')]:
        sc.save_state(harness, sid, {'cwd': str(tmp_path), 'host_pids': [20, 30]})
    monkeypatch.setattr(sc, '_latest_capture_turn', lambda c: {
        'ts': time.time(), 'issuer': {
            'pid': 20 if c['harness'] == 'claude_code' else 30, 'start': 100.0}})


@pytest.mark.parametrize('pids,expected', [([20, 30], ('claude_code', 'cl1')),
                                          ([30, 20], ('codex', 'cx1'))])
def test_mixed_markers_use_nearest_verified_issuer(tmp_path, monkeypatch, pids, expected):
    from khipu import session_capture as sc, owner_turns

    mixed_sessions(tmp_path, monkeypatch)
    monkeypatch.setattr(owner_turns, '_proc_info', lambda pid: (1, '/fake/harness', 100.0))
    assert sc.resolve_session_ref(host_pids=pids, cwd=str(tmp_path)) == (*expected, 'process')


@pytest.mark.parametrize('birth', [None, 101.0])
def test_mixed_markers_refuse_unverifiable_nearest_issuer(tmp_path, monkeypatch, birth):
    from khipu import session_capture as sc, owner_turns

    mixed_sessions(tmp_path, monkeypatch)
    monkeypatch.setattr(owner_turns, '_proc_info', lambda pid:
                        None if pid == 20 and birth is None else
                        (1, '/fake/harness', birth if pid == 20 else 100.0))
    with pytest.raises(ValueError, match='cannot tell which session is yours'):
        sc.resolve_session_ref(host_pids=[20, 30], cwd=str(tmp_path))


def test_mixed_markers_refuse_without_process_proof(tmp_path, monkeypatch):
    from khipu import session_capture as sc

    mixed_sessions(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match='cannot tell which session is yours'):
        sc.resolve_session_ref(host_pids=[], cwd=str(tmp_path))


def test_mixed_markers_do_not_select_outer_session_before_inner_first_hook(tmp_path, monkeypatch):
    from khipu import session_capture as sc, owner_turns

    mixed_sessions(tmp_path, monkeypatch)
    sc._state_file('claude_code', 'cl1').unlink()
    monkeypatch.setattr(owner_turns, '_proc_info', lambda pid: (1, '/fake/harness', 100.0))
    with pytest.raises(ValueError, match='cannot tell which session is yours'):
        sc.resolve_session_ref(host_pids=[20, 30], cwd=str(tmp_path))


def test_mixed_markers_do_not_skip_a_nearer_harness_without_prompt_records(tmp_path, monkeypatch):
    from khipu import session_capture as sc, owner_turns

    mixed_sessions(tmp_path, monkeypatch)
    original = sc._latest_capture_turn
    monkeypatch.setattr(sc, '_latest_capture_turn', lambda c:
                        {} if c['harness'] == 'claude_code' else original(c))
    monkeypatch.setattr(owner_turns, '_proc_info', lambda pid: (1, '/fake/harness', 100.0))
    with pytest.raises(ValueError, match='cannot tell which session is yours'):
        sc.resolve_session_ref(host_pids=[20, 30], cwd=str(tmp_path))


def test_mixed_markers_pass_over_shells_to_nearest_harness(tmp_path, monkeypatch):
    from khipu import session_capture as sc, owner_turns

    mixed_sessions(tmp_path, monkeypatch)
    monkeypatch.setattr(owner_turns, '_proc_info', lambda pid:
                        (20, '/bin/zsh', 101.0) if pid == 10 else (1, '/fake/harness', 100.0))
    assert sc.resolve_session_ref(host_pids=[10, 20, 30], cwd=str(tmp_path)) == ('claude_code', 'cl1', 'process')


def test_enqueued_unacknowledged_note_is_not_merged_again(tmp_path, monkeypatch):
    from khipu import session_capture as sc

    path = write(tmp_path / 'notes.jsonl', [])
    sc.save_state('codex', 'fake-session', {'offset': 0, 'transcript_path': str(path)})
    sc.request_capture_now('codex', 'fake-session', 'NOTE-X')
    old_request = sc._pending_capture_now('codex', 'fake-session')
    monkeypatch.setattr(sc, '_ack_capture_now', lambda *a: None)
    assert hook(path)['due']
    assert sc.load_state('codex', 'fake-session')['queued_capture_request_id'] == old_request['request_id']
    assert sc._pending_capture_now('codex', 'fake-session') is None
    sc.request_capture_now('codex', 'fake-session', 'NOTE-Y')
    assert sc._pending_capture_now('codex', 'fake-session')['request_id'] != old_request['request_id']
    assert hook(path)['due']
    # Jobs queued in the same second have no guaranteed ordering.
    assert sorted(json.loads(p.read_text())['capture_note'] for p in sc.queued_jobs()) == ['NOTE-X', 'NOTE-Y']


def test_exact_repeated_notes_keep_original_boundaries(tmp_path):
    from khipu import session_capture as sc

    for note in ['SAME\n\nMULTILINE', 'OTHER', 'SAME\n\nMULTILINE', None]:
        sc.request_capture_now('codex', 'fake-session', note)
    assert sc._pending_capture_now('codex', 'fake-session')['note'] == 'SAME\n\nMULTILINE\n\nOTHER'


def test_merged_note_is_bounded_and_keeps_newest_text(tmp_path):
    from khipu import session_capture as sc

    notes = [f'{i:02d} ' + 'x' * 997 for i in range(50)]
    for note in notes:
        sc.request_capture_now('codex', 'fake-session', note)
    pending = sc._pending_capture_now('codex', 'fake-session')
    # No capture-note limit existed; match the native transcript's 14,000-character budget.
    assert pending['note'] == '\n\n'.join(notes)[-14_000:]


def test_one_oversized_unicode_note_keeps_its_tail(tmp_path):
    from khipu import session_capture as sc

    note = '😀' * 14_000 + 'NEWEST'
    sc.request_capture_now('codex', 'fake-session', note)
    assert sc._pending_capture_now('codex', 'fake-session')['note'] == note[-14_000:]
    sc.request_capture_now('codex', 'fake-session', note)
    assert sc._pending_capture_now('codex', 'fake-session')['note'] == note[-14_000:]


def test_legacy_pending_note_merges_and_deduplicates(tmp_path):
    from khipu import session_capture as sc

    flag = sc._capture_now_flag_path('codex', 'fake-session')
    flag.parent.mkdir(parents=True)
    flag.write_text(json.dumps({'note': 'LEGACY\n\nTEXT', 'requested_at': 'fake-old-timestamp'}))
    sc.request_capture_now('codex', 'fake-session', 'LEGACY\n\nTEXT')
    sc.request_capture_now('codex', 'fake-session', 'NEW')
    assert sc._pending_capture_now('codex', 'fake-session')['note'] == 'LEGACY\n\nTEXT\n\nNEW'
