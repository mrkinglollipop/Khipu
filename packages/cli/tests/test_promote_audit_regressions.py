"""Promotion audit regressions using disposable slots and synthetic side effects."""
import json
import subprocess
from pathlib import Path

import pytest

from khipu import promote as pm

REAL_AFTER_FLIP = pm._after_flip


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('KHIPU_KEYCHAIN', '0')
    monkeypatch.setenv('KHIPU_DATA_DIR', str(home / '.config' / 'khipu'))
    monkeypatch.setattr(pm, 'retry_cleanups', lambda: [])
    monkeypatch.setattr(pm, 'precompile', lambda *_a, **_k: {'ok': True, 'ms': 0})
    monkeypatch.setattr(pm, '_before_flip', lambda: {'ok': True, 'ms': 0})
    return home


def test_stale_retry_cannot_replace_newer_manual_success(isolated, monkeypatch):
    """Interleave a newer command while the old retry probes the hub."""
    pm._queue('old-ref', 'old-commit', 'hub unavailable', None)
    applied = []

    def fake_promote(ref, _t0):
        applied.append(ref)
        return {'ok': True, 'action': 'promote', 'commit': ref, 'gate': []}

    monkeypatch.setattr(pm, '_promote_locked', fake_promote)

    def probe():
        # Equivalent to another process finishing while this probe is in flight.
        assert pm.promote('new-commit')['ok']
        assert pm.queued() is None
        return True

    monkeypatch.setattr(pm, 'hub_reachable', probe)
    result = pm.retry_queued()
    assert result['ok']
    assert applied == ['new-commit']
    assert result['cancelled'] is True
    assert pm.queued() is None


def test_kicker_cannot_resurrect_cancelled_queue(isolated, monkeypatch):
    pm._queue('old-ref', 'old-commit', 'hub unavailable', None)
    read_queue = pm.queued
    applied = []

    def fake_promote(ref, _t0):
        applied.append(ref)
        return {'ok': True, 'action': 'promote', 'commit': ref, 'gate': []}

    monkeypatch.setattr(pm, '_promote_locked', fake_promote)
    first_read = True

    def interleaved_read():
        nonlocal first_read
        entry = read_queue()
        if first_read:
            first_read = False
            assert pm.promote('new-commit')['ok']
            assert read_queue() is None
        return entry

    monkeypatch.setattr(pm, 'queued', interleaved_read)
    started = []
    monkeypatch.setattr(pm.subprocess, 'Popen', lambda *a, **kw: started.append(a))
    result = pm.kick_queued_retry()
    assert not result or result.get('kicked') is False
    assert applied == ['new-commit']
    assert read_queue() is None
    assert started == []


def test_kicker_writes_under_promotion_lock(isolated, monkeypatch):
    pm._queue('old-ref', 'old-commit', 'hub unavailable', None)
    write_queue = pm._write_queue

    def protected_write(entry):
        competing = pm.promote('new-commit')
        assert competing['ok'] is False
        assert 'another promotion' in competing['error']
        return write_queue(entry)

    monkeypatch.setattr(pm, '_write_queue', protected_write)
    monkeypatch.setattr(pm, '_promote_locked', lambda *_: pytest.fail('lock must exclude promotion'))
    monkeypatch.setattr(pm.subprocess, 'Popen', lambda *_a, **_kw: type('Fake', (), {'pid': 12345})())
    assert pm.kick_queued_retry()['kicked'] is True


def setup_slots(monkeypatch):
    heads = {}
    for slot, commit in zip(pm.slots(), ['known-good-A', 'known-good-B']):
        (slot / 'packages' / 'cli' / 'khipu').mkdir(parents=True)
        heads[slot] = commit
    monkeypatch.setattr(pm, '_head', lambda root: heads.get(Path(root)) if root else None)
    monkeypatch.setattr(pm, '_after_flip', lambda: {'kicked': [], 'kick_failed': []})
    monkeypatch.setattr(pm, '_git', lambda argv, **kw: subprocess.CompletedProcess(argv, 0, '', ''))
    assert pm.flip(pm.slots()[0])['ok']
    assert pm.flip(pm.slots()[1])['ok']
    return heads


def test_rollback_cancels_update_and_inflight_retry(isolated, monkeypatch):
    setup_slots(monkeypatch)
    pm._queue('pending-update', 'queued-commit', 'hub unavailable', None)
    back = pm.rollback()
    assert back['ok'] and pm.active_slot() == pm.slots()[0]
    assert pm.queued() is None
    monkeypatch.setattr(pm, 'hub_reachable', lambda: True)
    assert pm.retry_queued()['queued'] is None
    assert pm.active_slot() == pm.slots()[0]


def test_failed_rollback_retains_queue_and_working_slot(isolated, monkeypatch):
    setup_slots(monkeypatch)
    pending = pm._queue('pending-update', 'queued-commit', 'hub unavailable', None)
    monkeypatch.setattr(pm, 'flip', lambda *_: {'ok': False, 'error': 'synthetic rename failure'})
    out = pm.rollback()
    assert out['ok'] is False
    assert pm.queued() == pending
    assert pm.active_slot() == pm.slots()[1]


def test_new_same_ref_request_cannot_be_mistaken_for_old_retry(isolated, monkeypatch):
    monkeypatch.setattr(pm, '_now', lambda: '2026-10-22T00:00:00Z')
    old = pm._queue('same-ref', 'same-commit', 'hub unavailable', None)
    new = pm._queue('same-ref', 'same-commit', 'hub unavailable', None)
    monkeypatch.setattr(pm, '_promote_locked', lambda *_: pytest.fail('old intent must be refused'))
    out = pm.promote('same-commit', retry_of=old)
    assert out['ok'] and out['cancelled']
    assert pm.queued() == new


def test_rollback_invalidates_a_retry_already_probing(isolated, monkeypatch):
    setup_slots(monkeypatch)
    pm._queue('pending-update', 'queued-commit', 'hub unavailable', None)

    def probe():
        assert pm.rollback()['ok']
        return True

    monkeypatch.setattr(pm, 'hub_reachable', probe)
    monkeypatch.setattr(pm, '_promote_locked', lambda *_: pytest.fail('rollback cancelled this retry'))
    out = pm.retry_queued()
    assert out['ok'] and out['cancelled']
    assert pm.queued() is None
    assert pm.active_slot() == pm.slots()[0]


def test_refresh_failure_preserves_cause_and_reports_changed_live(isolated, monkeypatch):
    heads = setup_slots(monkeypatch)
    monkeypatch.setattr(pm, '_after_flip', REAL_AFTER_FLIP)
    monkeypatch.setattr(pm, '_repo', lambda: isolated)
    monkeypatch.setattr(pm, '_fetch_and_resolve', lambda _r, _f: {'ok': True, 'commit': 'new-commit'})

    def prepare(slot, commit, _repo):
        heads[slot] = commit
        return {'ok': True}

    monkeypatch.setattr(pm, '_prepare_slot', prepare)
    monkeypatch.setattr(pm, 'run_gate', lambda _c: [
        {'step': s, 'ok': True, 'ms': 0, 'detail': 'fake gate passed'} for s in pm.GATE_ORDER])
    failure = {'ok': False, 'refreshed': [], 'current': [], 'external': [],
               'results': [{'job': 'queue_drain', 'ok': False,
                            'error': 'launchctl bootstrap failed: synthetic denial'}]}

    def run(argv, **_kw):
        assert argv[-2:] == ['jobs', 'refresh'], argv
        return subprocess.CompletedProcess(argv, 1, json.dumps(failure), '')

    monkeypatch.setattr(pm, '_run', run)
    result = pm.promote('new-commit')
    assert result['ok'] is False
    assert result['live_changed'] is True and result['partial'] is True
    assert pm.active_slot() == pm.slots()[0]
    assert pm.queued() is None
    assert result['after']['jobs_refresh']['ok'] is False
    assert result['after']['jobs_refresh']['results'] == failure['results']
    text = pm.format_result(result)
    assert 'failed' in text.lower() and 'live is now' in text
    assert 'queue_drain' in text and 'synthetic denial' in text
