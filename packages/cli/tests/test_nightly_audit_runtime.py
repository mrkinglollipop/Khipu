"""Offline regressions adapted from /tmp/auditnightly runtime reproductions."""
import argparse
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import plistlib
import socket
import subprocess
import sys
from types import ModuleType
from unittest.mock import Mock

import pytest


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith(('KHIPU_', 'ALZY_', 'XDG_', 'GIT_')):
            monkeypatch.delenv(key)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('KHIPU_KEYCHAIN', '0')
    monkeypatch.setenv('KHIPU_TEST', '1')
    monkeypatch.setenv('KHIPU_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setenv('KHIPU_CAPTURE_HOME', str(tmp_path / 'capture'))
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    def deny(*a, **kw): raise AssertionError('network forbidden')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)


@pytest.mark.parametrize('key', ['KHIPU_DATA_DIR', 'ALZY_DATA_DIR'])
def test_launchd_preserves_scoped_data_dir(tmp_path, monkeypatch, key):
    from khipu import launchd_gen, paths
    monkeypatch.delenv('KHIPU_DATA_DIR')
    scoped = tmp_path / 'scoped-data'
    monkeypatch.setenv(key, str(scoped))
    baked = plistlib.loads(launchd_gen.render_plist('nightly'))['EnvironmentVariables']
    for k in list(os.environ): monkeypatch.delenv(k)
    monkeypatch.setenv('HOME', str(tmp_path))
    for k, v in baked.items(): monkeypatch.setenv(k, v)
    assert paths.data_dir() == scoped


def test_promote_reports_post_flip_refresh_failure(tmp_path, monkeypatch):
    from khipu import promote as pm, jobs
    base = tmp_path / 'slots'
    for name in pm.SLOT_NAMES: (base / name / 'packages/cli/khipu').mkdir(parents=True)
    (base / 'live').symlink_to(base / 'live-a')
    for name, fn in {
        'config_dir': lambda: base, '_repo': lambda: base, '_head': lambda p: 'abc123' if p else None,
        'retry_cleanups': lambda: [], '_fetch_and_resolve': lambda *a: {'ok': True, 'commit': 'abc123'},
        '_prepare_slot': lambda *a: {'ok': True}, 'precompile': lambda *a, **kw: {'ok': True},
        'run_gate': lambda *a: [{'step': 'doctor', 'ok': True, 'ms': 1, 'detail': 'synthetic'}],
    }.items(): monkeypatch.setattr(pm, name, fn)
    def run(argv, **kw):
        assert argv[-2:] == ['jobs', 'refresh']
        return subprocess.CompletedProcess(argv, 2, '{"ok":false,"error":"install_partial_failure"}', '')
    monkeypatch.setattr(pm, '_run', run)
    monkeypatch.setattr(jobs, '_plist_path', lambda label: tmp_path / 'missing-plist')
    result = pm.promote('synthetic')
    assert result['ok'] is False
    assert result['live_changed'] is True
    assert (base / 'live').resolve() == base / 'live-b'
    text = pm.format_result(result)
    assert 'install_partial_failure' in text
    assert 'live is unchanged' not in text
    assert 'live is now live-b' in text
    assert pm.queued() is None


@pytest.mark.parametrize('dirty', ['tracked', 'untracked'])
def test_rollback_refuses_dirty_target(tmp_path, monkeypatch, dirty):
    from khipu import promote as pm
    repo, base = tmp_path / 'repo', tmp_path / 'slots'
    repo.mkdir()
    def git(*args):
        return subprocess.run(['git', '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid',
            '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null', *args], cwd=repo,
            check=True, capture_output=True, text=True).stdout.strip()
    git('init', '-q')
    pkg = repo / 'packages/cli/khipu'
    pkg.mkdir(parents=True)
    (pkg / '__init__.py').write_text("__version__ = 'known-good-a'\n")
    git('add', '.')
    git('commit', '-qm', 'first')
    first = git('rev-parse', 'HEAD')
    (pkg / '__init__.py').write_text("__version__ = 'known-good-b'\n")
    git('commit', '-qam', 'second')
    second = git('rev-parse', 'HEAD')
    monkeypatch.setattr(pm, 'config_dir', lambda: base)
    monkeypatch.setattr(pm, '_before_flip', lambda: None)
    monkeypatch.setattr(pm, '_after_flip', lambda: {'kicked': []})
    a, b = pm.slots()
    assert pm._prepare_slot(a, first, repo)['ok']
    assert pm._prepare_slot(b, second, repo)['ok']
    assert pm.flip(a)['ok'] and pm.flip(b)['ok']
    filename = '__init__.py' if dirty == 'tracked' else 'extra.py'
    (a / 'packages/cli/khipu' / filename).write_text('never_gated = True\n')
    result = pm.rollback()
    assert result['ok'] is False
    assert 'dirty' in result['error']
    assert pm.live_link().resolve() == b


def test_malformed_job_does_not_block_healthy_job(monkeypatch):
    from khipu import session_capture as sc, config
    q = sc.queue_dir()
    q.mkdir(parents=True)
    (q / '00-corrupt-codex.json').write_text('[]')
    (q / '99-good-codex.json').write_text(json.dumps({'harness': 'codex', 'session_id': 'good',
        'transcript': 'synthetic', 'extracted_payload': {'summary': 'synthetic durable fact'}}))
    cap, ext, emb = ModuleType('khipu.capture'), ModuleType('khipu.extract'), ModuleType('khipu.embed')
    cap.capture = Mock(return_value=0)
    ext.extract_memory = Mock(side_effect=AssertionError('cached payload only'))
    ext.extract_verbatim = lambda s: {}
    ext.extract_deliverables = lambda s: {}
    emb.BudgetExhausted = type('BudgetExhausted', (Exception,), {})
    for name, mod in [('capture', cap), ('extract', ext), ('embed', emb)]:
        monkeypatch.setitem(sys.modules, 'khipu.' + name, mod)
    monkeypatch.setattr(config, 'capture_mode', lambda: 'hub')
    monkeypatch.setattr(sc, '_record_drain', lambda *a, **kw: None)
    # No post-capture provider work in this local queue test.
    from khipu import hub_snapshot
    monkeypatch.setattr(hub_snapshot, 'sync_decision_changes', lambda: {'ok': True})
    result = sc.drain()
    assert result['failed'] == 1
    assert result['captured'] == 1
    assert not (q / '99-good-codex.json').exists()
    cap.capture.assert_called_once()
    again = sc.drain()
    assert again['failed'] == 0
    assert sc.queued_jobs() == []
    assert (sc.empty_dir() / '00-corrupt-codex.json').read_bytes() == b'[]'
    reason = json.loads((sc.empty_dir() / '00-corrupt-codex.error').read_text())
    assert reason['disposition'] == 'malformed' and reason['reason']
    cap.capture.assert_called_once()


def test_replica_failure_remains_failed_in_cooldown(monkeypatch):
    from khipu import replica_heal as rh, hub_snapshot as hs
    state = {}
    monkeypatch.setattr(rh, 'read_state', lambda: dict(state))
    monkeypatch.setattr(rh, '_write_state', lambda s: state.update(s))
    monkeypatch.setattr(hs, 'snapshot_health', lambda: {'exists': False, 'age_seconds': None, 'refreshed_at': None})
    monkeypatch.setattr(hs, '_read_sidecar', lambda: {})
    monkeypatch.setattr(hs, '_read_last_known', lambda: None)
    monkeypatch.setattr(rh, 'settings', lambda: {'replica_refresh_after': 900})
    monkeypatch.setattr(rh, '_hub_reachable', lambda: (True, None))
    dump = Mock(return_value={'ok': False, 'error': 'synthetic ENOSPC'})
    monkeypatch.setattr(rh, '_full_dump', dump)
    assert rh.run()['ok'] is False
    second = rh.run()
    assert second['ok'] is False
    assert second['reason'] == 'recently_failed'
    assert 'synthetic ENOSPC' in second['error']
    dump.assert_called_once()


def test_owner_write_exception_fails_cli_drain(monkeypatch):
    from khipu import cli, session_capture as sc
    for name, attrs in {
        'outbox': {'drain': lambda **kw: {'failed': 0}},
        'owner_writes': {'waiting': lambda: True, 'drain': Mock(side_effect=OSError('synthetic owner write fault'))},
        'routing': {'refresh_plan_figures': lambda: {'skipped': 'synthetic'}},
        'promote': {'kick_queued_retry': lambda: None},
    }.items():
        mod = ModuleType('khipu.' + name)
        for k, v in attrs.items(): setattr(mod, k, v)
        monkeypatch.setitem(sys.modules, 'khipu.' + name, mod)
        import khipu
        monkeypatch.setattr(khipu, name, mod, raising=False)
    monkeypatch.setattr(sc, 'sweep_idle', lambda **kw: {'swept': 0})
    monkeypatch.setattr(sc, '_reclaim_stale', lambda: 0)
    monkeypatch.setattr(sc, 'queued_jobs', lambda: [])
    stream = io.StringIO()
    with redirect_stdout(stream):
        rc = cli.cmd_sessions(argparse.Namespace(aegis_cmd='drain', limit=None, dry_run=False))
    result = json.loads(stream.getvalue())
    assert rc != 0
    assert result['failed'] == 1
    assert 'synthetic owner write fault' in result['owner_writes']['error']


def test_missing_driver_continues_independent_maintenance(tmp_path, monkeypatch):
    from khipu import jobs
    monkeypatch.setattr(jobs, 'CONSOLIDATE_NIGHTLY', tmp_path / 'missing.py')
    monkeypatch.setattr(jobs, '_drain_sessions', lambda: {'ok': True})
    later = ['_reconcile_notes_if_due', '_embed_backfill', '_prune_query_cache',
        '_commitment_close_rules', '_mark_stale_commitments', '_hygiene_commitments',
        '_ledger_nightly', '_project_renames_sweep', '_refresh_snapshot', '_briefs_build_if_on']
    mocks = []
    for name in later:
        fn = Mock(return_value={'ok': True})
        monkeypatch.setattr(jobs, name, fn)
        mocks.append(fn)
    rc = jobs.run_nightly()
    assert rc != 0
    for fn in mocks: fn.assert_called_once()
    steps = jobs.nightly_last()['steps']
    step = next(s for s in steps if s['name'] == 'consolidate_nightly')
    assert not step['ok']
    assert 'FileNotFoundError' in step['error']
