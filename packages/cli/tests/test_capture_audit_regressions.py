"""Capture audit regressions: invented transcripts and a loopback-only hub."""
import argparse
import json
import os
import socket
import subprocess
import threading
import time
from unittest.mock import Mock

import pytest

from tests.hermetic_pg import hub, pg_cluster  # noqa: F401


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from khipu import session_capture as sc, identity, synth_cli, config
    for name in list(os.environ):
        if name.startswith(('KHIPU_', 'ALZY_', 'CLAUDE_', 'CODEX_', 'GROK_', 'GIT_')):
            monkeypatch.delenv(name, raising=False)
    for name, value in {'HOME': str(tmp_path), 'KHIPU_KEYCHAIN': '0',
                        'KHIPU_CAPTURE_HOME': str(tmp_path / 'capture'),
                        'KHIPU_DATA_DIR': str(tmp_path / 'data'),
                        'KHIPU_HUB_FILE_MIRROR': '0', 'KHIPU_CAPTURE_MODE': 'hub',
                        'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_SYSTEM': '/dev/null'}.items():
        monkeypatch.setenv(name, value)
    original = socket.socket.connect
    def loopback_only(sock, address):
        if isinstance(address, tuple) and address[0] not in ('127.0.0.1', '::1', 'localhost'):
            raise AssertionError(f'network forbidden: {address}')
        return original(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', loopback_only)
    monkeypatch.setattr(synth_cli, 'session_ids', lambda: set())
    monkeypatch.setattr(identity, 'resolve_repo_root', lambda *a: {'repo_root': None, 'project': None})
    monkeypatch.setattr(identity, 'observe_checkout', lambda *a: None)
    monkeypatch.setattr(sc, 'cache_thread', lambda *a: None)
    monkeypatch.setattr(sc, 'ancestor_pids', lambda *a: [])
    monkeypatch.setattr(sc, 'sweep_login_homes', lambda *a: 0)
    monkeypatch.setattr(sc, '_record_drain', lambda *a, **kw: None)
    monkeypatch.setattr(config, 'capture_mode', lambda: 'hub')
    monkeypatch.setattr(config, 'path_setting', lambda *a: None)
    monkeypatch.setattr('khipu.hub_snapshot.sync_decision_changes', lambda: {})
    monkeypatch.setattr('khipu.routing_directions.capture_scan', lambda *a, **kw: {})


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    return path


def codex(role, text):
    return {'type': 'response_item', 'payload': {'type': 'message', 'role': role,
            'content': [{'type': 'input_text' if role == 'user' else 'output_text', 'text': text}]}}


def claude(role, text):
    return {'type': role, 'message': {'role': role, 'content': text}}


def hook(path, sid='fake-session', harness='codex'):
    from khipu import session_capture as sc
    return sc.hook_main(json.dumps({'session_id': sid, 'hook_event_name': 'SessionEnd',
                        'cwd': str(path.parent), 'transcript_path': str(path)}), harness)


def configure_pg(monkeypatch, hub):
    from khipu import db, embed, topic_graph, outbox
    hub.migrate()
    monkeypatch.setattr(db, 'connect', lambda *a, **kw: hub.connect())
    monkeypatch.setattr(embed, 'embed_on_capture', lambda *a, **kw: None)
    monkeypatch.setattr(embed, 'embed_one', Mock(side_effect=RuntimeError('no provider')))
    monkeypatch.setattr(topic_graph, 'persist_capture_graph', lambda *a, **kw: {})
    monkeypatch.setattr(outbox, '_queued_origin', lambda: {'origin': None})


def test_acp_whitespace_survives_chunk_reassembly(tmp_path):
    from khipu import session_capture as sc
    def chunk(text):
        return {'params': {'update': {'sessionUpdate': 'user_message_chunk',
                                      'content': {'type': 'text', 'text': text}}}}
    path = write(tmp_path / 'stream.jsonl', [chunk(t) for t in
                 ['The destination is /fake/my', ' ', 'file.\n', '\n', 'Keep the separator.']])
    msgs, _, users = sc.read_window(path, 0, seen=())
    assert msgs == [('user', 'The destination is /fake/my file.\n\nKeep the separator.')]
    assert users == 1


def test_codex_mixed_representations_preserve_unpaired_turn(tmp_path):
    from khipu import session_capture as sc
    q, a = 'Synthetic question ' * 20, 'UNPAIRED_ASSISTANT_DECISION ' * 20
    path = write(tmp_path / 'mixed.jsonl', [codex('user', q),
                 {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': q}},
                 codex('assistant', a)])
    out = hook(path)
    job = json.loads((sc.queue_dir() / out['queued']).read_text())
    assert a in job['transcript']
    assert job['transcript'].count(q) == 1


@pytest.mark.parametrize('paired', [False, True])
def test_codex_pairing_retains_repeated_genuine_short_messages(tmp_path, paired):
    from khipu import session_capture as sc
    rows = [codex('user', 'again'), codex('user', 'again')]
    if paired:
        rows.append({'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'again'}})
    path = write(tmp_path / 'repeats.jsonl', rows)
    msgs, _, _ = sc.read_window(path, 0, seen=())
    assert msgs == [('user', 'again\nagain')]


def test_relocated_transcript_resets_offset_keeps_seen_history(tmp_path, monkeypatch):
    from khipu import session_capture as sc
    sid, oldq = 'relocated', 'Previously captured synthetic question. ' * 7
    old = write(tmp_path / 'old/projects/p' / f'{sid}.jsonl', [claude('user', oldq),
                {'type': 'user', 'message': {'role': 'user', 'content': [
                 {'type': 'tool_result', 'content': 'old padding ' * 140}]}}])
    first = hook(old, sid, 'claude_code')
    before = sc.load_state('claude_code', sid)['offset']
    freshq, fresha = 'NEW_USER_REQUIREMENT ' * 14, 'NEW_ASSISTANT_PLAN ' * 170
    new = write(tmp_path / 'new/projects/p' / f'{sid}.jsonl',
                [claude('user', oldq), claude('user', freshq), claude('assistant', fresha)])
    assert new.stat().st_size > before
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'new'))
    out = hook(old, sid, 'claude_code')
    assert out['due'], out
    job = json.loads((sc.queue_dir() / out['queued']).read_text())
    assert freshq in job['transcript'] and fresha in job['transcript']
    assert oldq not in job['transcript']
    assert first['queued'] != out['queued']


def test_idle_sweep_reads_shorter_replacement(tmp_path):
    from khipu import session_capture as sc
    old = time.time() - 3600
    path = write(tmp_path / 'rotated.jsonl', [codex('user', 'ROTATED_NEW_REQUIREMENT ' * 18)])
    os.utime(path, (old, old))
    sc.save_state('codex', 'rotated', {'offset': path.stat().st_size + 1000, 'seen_ts': old,
                  'transcript_path': str(path), 'cwd': str(tmp_path)})
    out = sc.sweep_idle(now=time.time())
    assert out['read'] == 1 and out['jobs'] == 1, out


def test_unknown_own_session_never_targets_foreign_session(tmp_path, monkeypatch):
    from khipu import cli, session_capture as sc
    sc.save_state('claude_code', 'unrelated', {'offset': 0, 'seen_ts': time.time(),
                                           'cwd': str(tmp_path / 'other-project')})
    monkeypatch.setenv('CODEX_THREAD_ID', 'new-codex-session')
    args = argparse.Namespace(harness=None, session_id=None, note='fake own-session note')
    rc = cli.cmd_capture_now(args)
    assert not sc._capture_now_flag_path('claude_code', 'unrelated').exists()
    assert rc != 0 or sc._capture_now_flag_path('codex', 'new-codex-session').exists()


def test_explicit_note_survives_empty_extraction(tmp_path, monkeypatch):
    from khipu import session_capture as sc, extract, capture
    path = write(tmp_path / 'rollout.jsonl', [codex('user', 'hello')])
    note = 'The fake release decision must be retained verbatim.'
    sc.request_capture_now('codex', 'fake-session', note)
    assert hook(path)['due']
    monkeypatch.setattr(extract, 'extract_memory', lambda *a, **kw: None)
    sink = Mock(return_value=0)
    monkeypatch.setattr(capture, 'capture', sink)
    drained = sc.drain()
    assert drained['captured'] == 1 and drained['empty'] == 0, drained
    assert sink.call_args.args[0]['verbatim']['note'] == note


def test_repeated_zero_byte_notes_have_distinct_retry_stable_sources(tmp_path, monkeypatch, hub):
    from khipu import session_capture as sc, extract
    from khipu.capture_identity import source_key
    configure_pg(monkeypatch, hub)
    monkeypatch.setattr(extract, 'extract_memory', lambda text, **kw: {'summary': text})
    path = write(tmp_path / 'rollout.jsonl', [codex('user', 'Initial synthetic conversation. ' * 15)])
    assert hook(path)['due']
    assert sc.drain()['captured'] == 1
    notes = ['Amber preserves the fake export.', 'Violet cancels the invented launch.']
    keys = []
    for note in notes:
        sc.request_capture_now('codex', 'fake-session', note)
        assert hook(path)['due']
        job = json.loads(sc.queued_jobs()[0].read_text())
        key = source_key(sc.job_source_identity(job))
        assert key == source_key(sc.job_source_identity(json.loads(json.dumps(job))))
        keys.append(key)
        assert sc.drain()['captured'] == 1
    assert keys[0] != keys[1]
    with hub.connect() as conn:
        rows = conn.execute('SELECT verbatim FROM episodes').fetchall()
    for note in notes:
        assert any(note in str(row) for row in rows)


def test_inherited_claude_parent_keeps_namespace_and_project(tmp_path, monkeypatch, hub):
    from khipu import session_capture as sc, capture
    configure_pg(monkeypatch, hub)
    parent = {'ts': sc._mint_ts(), 'summary': 'Synthetic parent project.',
              'session_id': 'claude_code:fake-parent', 'harness': 'claude_code',
              'project': 'example/fake-alpha', 'transcript_range': '0:123'}
    assert capture.write_pg(parent)['episode_inserted']
    monkeypatch.setenv('CLAUDE_CODE_HOST_SESSION_ID', 'fake-parent')
    path = write(tmp_path / 'rollout.jsonl', [codex('user', 'Synthetic child work. ' * 24)])
    out = hook(path, 'fake-child')
    job = json.loads((sc.queue_dir() / out['queued']).read_text())
    assert job['parent_session_id'] == 'claude_code:fake-parent'
    assert capture.write_pg({'ts': sc._mint_ts(), 'summary': 'Synthetic child output.',
           'session_id': 'codex:fake-child', 'harness': 'codex', 'transcript_range': '0:200',
           'parent_session_id': job['parent_session_id']})['episode_inserted']
    with hub.connect() as conn:
        row = conn.execute("SELECT project FROM episodes WHERE session_id='codex:fake-child'").fetchone()
    assert row == ('example/fake-alpha',)


def test_live_legacy_claim_is_retained_without_birth_proof(tmp_path):
    from khipu import session_capture as sc
    unrelated = subprocess.Popen(['/bin/sleep', '30'])
    try:
        sc.queue_dir().mkdir(parents=True, exist_ok=True)
        dead = sc.queue_dir() / f'old-codex-orphan.claimed.{unrelated.pid}'
        dead.write_text(json.dumps({'session_id': 'orphan', 'harness': 'codex',
                         'claim_owner': {'pid': unrelated.pid, 'start': 'kernel:untrusted-job-value'}}))
        os.utime(dead, (1_600_000_000, 1_600_000_000))
        # Old enqueue time is not owner identity, and job-supplied ownership
        # fields are untrusted. Preserve this ambiguous legacy evidence.
        assert not sc._claimant_gone(dead)
        assert sc._reclaim_stale() == 0
        assert dead.exists()
        assert not (sc.queue_dir() / 'old-codex-orphan.json').exists()
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_reused_live_pid_with_sidecar_birth_mismatch_is_reclaimed(tmp_path):
    from khipu import session_capture as sc
    unrelated = subprocess.Popen(['/bin/sleep', '30'])
    try:
        sc.queue_dir().mkdir(parents=True, exist_ok=True)
        claim = sc.queue_dir() / f'versioned-codex-orphan.claimed.{unrelated.pid}'
        claim.write_text(json.dumps({'session_id': 'orphan', 'harness': 'codex'}))
        start = sc._claim_process_start(unrelated.pid)
        assert start
        with sc._claim_lock():
            sc._write_claim_owner(claim, {'pid': unrelated.pid,
                                 'start': start.split(':', 1)[0] + ':previous-birth'})
        assert sc._claimant_gone(claim)
        assert sc._reclaim_stale() == 1
        assert not claim.exists()
        assert not sc._claim_owner_path(claim).exists()
        assert (sc.queue_dir() / 'versioned-codex-orphan.json').exists()
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_claim_birth_identity_controls_reclamation(tmp_path):
    from khipu import session_capture as sc
    job = sc.enqueue({'ts': '2026-10-23T12:00:00Z', 'harness': 'codex', 'session_id': 'active',
                      'transcript': 'USER: Synthetic active work'})
    before = job.read_bytes()
    claimed = sc._claim(job)
    contents = json.loads(claimed.read_text())
    owner_path = sc._claim_owner_path(claimed)
    owner = json.loads(owner_path.read_text())
    assert claimed.read_bytes() == before
    assert owner['pid'] == os.getpid()
    assert owner['start'] == sc._claim_process_start(os.getpid())
    assert sc.queued_jobs() == []
    assert list(sc.queue_dir().glob('*.claimed.*')) == [claimed]
    os.utime(claimed, (1_600_000_000, 1_600_000_000))
    assert not sc._claimant_gone(claimed)
    assert sc._reclaim_stale() == 0
    # The reclaiming process itself can receive a previous owner's numeric PID.
    reused_self = sc.queue_dir() / f'reused-self.claimed.{os.getpid()}'
    reused_self.write_text(json.dumps(contents))
    with sc._claim_lock():
        sc._write_claim_owner(reused_self, {'pid': os.getpid(),
                             'start': owner['start'].split(':', 1)[0] + ':old-birth'})
    assert sc._claimant_gone(reused_self)
    assert sc._reclaim_stale() == 1
    unrelated = subprocess.Popen(['/bin/sleep', '30'])
    try:
        start = sc._claim_process_start(unrelated.pid)
        assert start
        orphan = sc.queue_dir() / f'new-format-orphan.claimed.{unrelated.pid}'
        orphan.write_text(json.dumps(contents))
        with sc._claim_lock():
            sc._write_claim_owner(orphan, {'pid': unrelated.pid,
                                 'start': start.split(':', 1)[0] + ':old-birth'})
        assert sc._claimant_gone(orphan)
        assert sc._reclaim_stale() == 1
        assert not sc._claim_owner_path(orphan).exists()
        assert not sc._claim_owner_path(reused_self).exists()
        assert claimed.exists()
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)
    sc._release(claimed)
    assert not owner_path.exists()
    assert job.read_bytes() == before


def test_note_request_identity_survives_failed_delivery(tmp_path, monkeypatch):
    from khipu import session_capture as sc, capture, extract
    from khipu.capture_identity import source_key
    path = write(tmp_path / 'rollout.jsonl', [])
    sc.save_state('codex', 'fake-session', {'offset': 0, 'transcript_path': str(path)})
    sc.request_capture_now('codex', 'fake-session', 'Retry this synthetic note.')
    assert hook(path)['due']
    original = json.loads(sc.queued_jobs()[0].read_text())
    original_key = source_key(sc.job_source_identity(original))
    monkeypatch.setattr(extract, 'extract_memory', lambda *a, **kw: None)
    sink = Mock(side_effect=[1, 0])
    monkeypatch.setattr(capture, 'capture', sink)
    assert sc.drain()['failed'] == 1
    retry = json.loads(sc.queued_jobs()[0].read_text())
    assert source_key(sc.job_source_identity(retry)) == original_key
    assert retry['extracted_payload']['summary'] == 'Capture note: Retry this synthetic note.'
    assert sc.drain()['captured'] == 1
    assert sink.call_args_list[0].args[0] == sink.call_args_list[1].args[0]


def test_malformed_job_does_not_starve_bounded_drain(tmp_path, monkeypatch):
    from khipu import session_capture as sc, capture, extract
    sc.queue_dir().mkdir(parents=True, exist_ok=True)
    bad = sc.queue_dir() / '0000-bad.json'
    bad.write_text('{')
    good = sc.enqueue({'ts': '2026-10-23T12:00:00Z', 'harness': 'codex', 'session_id': 'healthy',
                       'transcript': 'USER: Synthetic queue evidence', 'offset_before': 0, 'offset_after': 100})
    sink = Mock(return_value=0)
    monkeypatch.setattr(capture, 'capture', sink)
    monkeypatch.setattr(extract, 'extract_memory', lambda *a, **kw: {'summary': 'Synthetic valid summary'})
    reports = [sc.drain(limit=1) for _ in range(3)]
    assert not good.exists(), reports
    sink.assert_called_once()
    # Preserve the exact broken input for inspection, outside eligible jobs.
    assert not bad.exists()
    assert any(p.read_text() == '{' for p in sc.khipu_home().rglob('0000-bad.json'))


def test_malformed_archive_never_overwrites_earlier_evidence(tmp_path):
    from khipu import session_capture as sc
    sc.queue_dir().mkdir(parents=True, exist_ok=True)
    for raw in ['{', '[']:
        claim = sc.queue_dir() / f'0000-bad.claimed.{os.getpid()}'
        claim.write_text(raw)
        sc._quarantine_claim(claim, 'synthetic parse failure')
    assert sorted(p.read_text() for p in sc.empty_dir().glob('*.json')) == ['[', '{']
    assert len(list(sc.empty_dir().glob('*.error'))) == 2


def test_legacy_collision_keeps_conflicting_outbox_evidence(monkeypatch, hub):
    from khipu import outbox
    configure_pg(monkeypatch, hub)
    base = {'ts': '2026-10-23T12:00:00Z', 'summary': 'Synthetic identical summary'}
    red = dict(base, decisions=['Keep synthetic red widget'])
    blue = dict(base, decisions=['Keep synthetic blue widget'])
    assert outbox.enqueue(red) != outbox.enqueue(blue)
    report = outbox.drain()
    with hub.connect() as conn:
        rows = conn.execute('SELECT decisions, raw FROM episodes').fetchall()
    remaining = [p.read_text() for p in outbox.jobs()]
    for decision in [red['decisions'][0], blue['decisions'][0]]:
        assert decision in str(rows) or any(decision in text for text in remaining), report
    assert len(remaining) == 1 and report['failed'] == 1, report


def test_concurrent_exact_window_has_one_episode(monkeypatch, hub):
    from khipu import capture
    configure_pg(monkeypatch, hub)
    barrier = threading.Barrier(2)
    original = capture._dedup_exact_window
    def synchronized(*args, **kwargs):
        found = original(*args, **kwargs)
        # On serialized writes the first proceeds after the deadline while
        # the second waits on the transaction lock; no lock-dependent deadlock.
        try:
            barrier.wait(timeout=0.5)
        except threading.BrokenBarrierError:
            pass
        return found
    monkeypatch.setattr(capture, '_dedup_exact_window', synchronized)
    results, errors = [], []
    def writer(summary):
        payload = {'ts': '2026-10-23T13:00:00Z', 'summary': summary, 'harness': 'codex',
                   'session_id': 'codex:synthetic-race', 'transcript_range': '0:100',
                   'window_id': 'synthetic-window', 'part': '1/1'}
        try:
            results.append(capture.write_pg(payload))
        except Exception as exc:
            errors.append(repr(exc))
    threads = [threading.Thread(target=writer, args=(s,)) for s in ['Synthetic red capture', 'Synthetic blue capture']]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    assert not errors, errors
    with hub.connect() as conn:
        rows = conn.execute('SELECT id FROM episodes').fetchall()
    assert len(rows) == 1, results
    assert sum(bool(r['episode_inserted']) for r in results) == 1


@pytest.mark.parametrize('relocate', [False, True])
def test_equal_size_replacement_has_new_generation_and_retry_identity(tmp_path, monkeypatch, hub, relocate):
    from khipu import session_capture as sc, capture, extract
    from khipu.capture_identity import source_key
    configure_pg(monkeypatch, hub)
    monkeypatch.setattr(extract, 'extract_memory', lambda text, **kw: {'summary': text})
    original_text = 'First requirement: ' + 'a' * 400
    fresh_text = 'Fresh requirement: ' + 'b' * 400
    path = write(tmp_path / 'original.jsonl', [codex('user', original_text)])
    assert hook(path)['due']
    initial = json.loads(sc.queued_jobs()[0].read_text())
    initial_key = source_key(sc.job_source_identity(initial))
    assert sc.drain()['captured'] == 1
    replacement = write(tmp_path / 'replacement.jsonl', [codex('user', fresh_text)])
    assert replacement.stat().st_size == path.stat().st_size
    if not relocate:
        replacement.replace(path)
    else:
        path = replacement
    assert hook(path)['due']
    job = json.loads(sc.queued_jobs()[0].read_text())
    replacement_key = source_key(sc.job_source_identity(job))
    assert replacement_key != initial_key
    deliver = capture.capture
    monkeypatch.setattr(capture, 'capture', lambda *a, **kw: 1)
    assert sc.drain()['failed'] == 1
    retry = json.loads(sc.queued_jobs()[0].read_text())
    assert source_key(sc.job_source_identity(retry)) == replacement_key
    monkeypatch.setattr(capture, 'capture', deliver)
    assert sc.drain()['captured'] == 1
    with hub.connect() as conn:
        summaries = conn.execute('SELECT summary FROM episodes').fetchall()
    assert any(fresh_text in row[0] for row in summaries)
    assert any(original_text in row[0] for row in summaries)


@pytest.mark.parametrize('field,first,second', [
    ('people', ['Synthetic Red'], ['Synthetic Blue']),
    ('edges', [{'from': 'Red', 'to': 'Widget'}], [{'from': 'Blue', 'to': 'Widget'}]),
    ('parent_session_id', 'claude_code:synthetic-red', 'claude_code:synthetic-blue'),
])
def test_legacy_collision_requires_immutable_evidence_proof(monkeypatch, hub, field, first, second):
    from khipu import outbox
    configure_pg(monkeypatch, hub)
    base = {'ts': '2026-10-23T12:00:00Z', 'summary': 'Synthetic identical evidence summary'}
    outbox.enqueue(dict(base, **{field: first}))
    outbox.enqueue(dict(base, **{field: second}))
    report = outbox.drain()
    with hub.connect() as conn:
        rows = conn.execute('SELECT raw FROM episodes').fetchall()
    assert len(outbox.jobs()) == 1 and report['failed'] == 1, (report, rows)
    evidence = [row[0].get(field) for row in rows]
    evidence.extend(json.loads(p.read_text())['payload'].get(field) for p in outbox.jobs())
    assert first in evidence and second in evidence
