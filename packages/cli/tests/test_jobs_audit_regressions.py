"""Synthetic regression coverage for scheduled-job audit findings 6–9."""
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from khipu import graph_backup, jobs

MAINTENANCE = ('_reconcile_notes_if_due', '_embed_backfill', '_prune_query_cache',
               '_commitment_close_rules', '_mark_stale_commitments',
               '_hygiene_commitments', '_ledger_nightly', '_project_renames_sweep',
               '_refresh_snapshot', '_briefs_build_if_on')


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('external operation forbidden in synthetic regression')
    monkeypatch.setattr(socket, 'socket', forbidden)
    from khipu import db
    monkeypatch.setattr(db, 'connect', forbidden)
    monkeypatch.setattr(jobs, '_plist_loaded', forbidden)
    monkeypatch.setattr(jobs, 'ensure_data_dir', lambda: tmp_path)
    monkeypatch.setattr(jobs, '_log_paths', lambda stem: (tmp_path / 'job.out', tmp_path / 'job.err'))
    monkeypatch.setattr(jobs, '_drain_sessions', lambda: {'ok': True, 'failed': 0})
    for name in MAINTENANCE:
        monkeypatch.setattr(jobs, name, Mock(return_value={'ok': True}))


def test_unique_completion_receipts(tmp_path, monkeypatch):
    original_replace = Path.replace
    barrier = threading.Barrier(2)
    def replace(path, target):
        barrier.wait(timeout=5)
        return original_replace(path, target)
    monkeypatch.setattr(Path, 'replace', replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(jobs._write_job_state, 'nightly', rc) for rc in (0, 3)]
        assert [f.exception(timeout=5) for f in futures] == [None, None]
    assert json.loads((tmp_path / 'state/job-nightly.json').read_text())['exit'] in (0, 3)
    assert list((tmp_path / 'state').iterdir()) == [tmp_path / 'state/job-nightly.json']


@pytest.mark.parametrize('name', ['nightly', 'monthly'])
def test_wrapper_excludes_overlap_without_overwriting_completion(tmp_path, monkeypatch, name):
    entered, release = threading.Event(), threading.Event()
    jobs._write_job_state(name, 9)
    original_receipt = (tmp_path / f'state/job-{name}.json').read_bytes()
    calls = []
    def driver(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        jobs._write_job_state(name, 0)
        return 0
    monkeypatch.setattr(jobs, '_run_script', driver)
    run = getattr(jobs, f'run_{name}')
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run)
        try:
            assert entered.wait(2)
            second = pool.submit(run)
            assert second.result(timeout=1) == 75
            assert calls == [1]
            assert (tmp_path / f'state/job-{name}.json').read_bytes() == original_receipt
        finally:
            release.set()
        assert first.result(timeout=5) == 0
    assert json.loads((tmp_path / f'state/job-{name}.json').read_text())['exit'] == 0
    assert run() == 0  # completed wrapper released the lock


@pytest.mark.parametrize('error', ['missing', 'launch', 'timeout'])
def test_driver_failure_keeps_failure_and_runs_independent_steps(tmp_path, monkeypatch, error):
    script = tmp_path / 'driver.py'
    if error != 'missing':
        script.write_text('pass\n')
    monkeypatch.setattr(jobs, 'CONSOLIDATE_NIGHTLY', script)
    if error == 'launch':
        monkeypatch.setattr(subprocess, 'Popen', Mock(side_effect=OSError('synthetic launch failure')))
    elif error == 'timeout':
        monkeypatch.setattr(jobs, '_run_script', Mock(side_effect=subprocess.TimeoutExpired(['synthetic'], 1)))
    expected = 124 if error == 'timeout' else 2
    assert jobs.run_nightly() == expected
    assert all(getattr(jobs, name).call_count == 1 for name in MAINTENANCE)
    assert json.loads((tmp_path / 'state/job-nightly.json').read_text())['exit'] == expected
    step = jobs.nightly_last()['steps'][1]
    assert step['name'] == 'consolidate_nightly' and step['ok'] is False
    assert step['error']


@pytest.mark.parametrize('leader_ignores_term', [False, True])
def test_driver_deadline_kills_process_group_and_continues(tmp_path, monkeypatch, leader_ignores_term):
    """Real local Python parent/grandchild; emergency outer deadline prevents hangs."""
    script = tmp_path / 'driver.py'
    heartbeat, ids = tmp_path / 'heartbeat', tmp_path / 'pids'
    child_code = "import signal,time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); p=Path(%r); end=time.monotonic()+6;\nwhile time.monotonic()<end: p.write_text(str(time.monotonic())); time.sleep(.02)" % str(heartbeat)
    script.write_text('import os,signal,subprocess,sys,time\nfrom pathlib import Path\n'
                      + ('signal.signal(signal.SIGTERM, signal.SIG_IGN)\n' if leader_ignores_term else '')
                      + f'child=subprocess.Popen([sys.executable,"-c",{child_code!r}])\n'
                      f'Path({str(ids)!r}).write_text(str(os.getpid())+" "+str(child.pid))\n'
                      'time.sleep(6)\n')
    monkeypatch.setattr(jobs, 'CONSOLIDATE_NIGHTLY', script)
    monkeypatch.setattr(jobs, 'DRIVER_TIMEOUT_SECONDS', .4, raising=False)
    monkeypatch.setattr(jobs, 'DRIVER_TERMINATE_GRACE_SECONDS', .1, raising=False)
    start = time.monotonic()
    try:
        assert jobs.run_nightly() == 124
        assert time.monotonic() - start < 2
        assert ids.exists() and heartbeat.exists()
        before = heartbeat.read_text()
        time.sleep(.15)
        assert heartbeat.read_text() == before
        assert all(getattr(jobs, name).call_count == 1 for name in MAINTENANCE)
        assert json.loads((tmp_path / 'state/job-nightly.json').read_text())['exit'] == 124
    finally:
        if ids.exists():
            try:
                os.killpg(int(ids.read_text().split()[0]), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_due_offsite_preflight_failure_replaces_older_success(tmp_path, monkeypatch):
    from khipu import ops_events
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=7, hours=1)
    latest = {'status': 'ok', 'created_at': old, 'age_seconds': (now-old).total_seconds()}
    monkeypatch.setattr(graph_backup, '_latest_ops_event', lambda kind: latest)
    monkeypatch.setattr(graph_backup, 'resolve_dest_remote', lambda: 'r2:synthetic')
    monkeypatch.setattr(graph_backup, 'resolve_rclone', lambda name: '/fake/rclone')
    monkeypatch.setattr(graph_backup, 'has_r2_remote', lambda **kwargs: True)
    monkeypatch.setattr(graph_backup, 'latest_snapshot', lambda: None)
    def record(kind, status, detail):
        assert kind == 'graph_snapshot_offsite'
        latest.update(status=status, detail=detail, created_at=now, age_seconds=0)
        return {'id': 1}
    record_mock = Mock(side_effect=record)
    monkeypatch.setattr(ops_events, 'record', record_mock)
    jobs._offsite_if_due()
    record_mock.assert_called_once()
    assert latest['status'] == 'fail'
    assert 'no snapshot found' in (tmp_path / 'job.out').read_text()
    assert graph_backup.offsite_health(now=now, producer=True)['ok'] is False


@pytest.mark.parametrize('stage', ['destination', 'binary', 'remote', 'empty', 'stat', 'integrity', 'schema', 'live', 'same_path', 'exception'])
def test_every_offsite_preflight_failure_records_once(tmp_path, monkeypatch, stage):
    from khipu import ops_events
    snap = tmp_path / 'snapshot.sqlite'
    snap.write_bytes(b'synthetic')
    monkeypatch.setattr(graph_backup, 'resolve_dest_remote', lambda: 'r2:synthetic')
    monkeypatch.setattr(graph_backup, 'resolve_rclone', lambda name: '/fake/rclone')
    monkeypatch.setattr(graph_backup, 'has_r2_remote', lambda **kwargs: True)
    monkeypatch.setattr(graph_backup, 'latest_snapshot', lambda: snap)
    monkeypatch.setattr(graph_backup, '_integrity_check', lambda path: 'ok')
    monkeypatch.setattr(graph_backup, '_required_graph_tables', lambda path: None)
    monkeypatch.setattr(graph_backup, '_live_graph', lambda: tmp_path / 'live.sqlite')
    monkeypatch.setattr(subprocess, 'run', Mock(side_effect=AssertionError('copy forbidden')))
    if stage == 'destination':
        monkeypatch.setattr(graph_backup, 'resolve_dest_remote', Mock(side_effect=ValueError('invalid')))
    elif stage == 'binary':
        monkeypatch.setattr(graph_backup, 'resolve_rclone', lambda name: None)
    elif stage == 'remote':
        monkeypatch.setattr(graph_backup, 'has_r2_remote', lambda **kwargs: False)
    elif stage == 'empty':
        snap.write_bytes(b'')
    elif stage == 'stat':
        snap.unlink()
    elif stage == 'integrity':
        monkeypatch.setattr(graph_backup, '_integrity_check', lambda path: 'corrupt')
    elif stage == 'schema':
        monkeypatch.setattr(graph_backup, '_required_graph_tables', lambda path: 'missing nodes')
    elif stage == 'live':
        monkeypatch.setattr(graph_backup, '_live_graph', lambda: None)
    elif stage == 'same_path':
        monkeypatch.setattr(graph_backup, '_live_graph', lambda: snap)
    elif stage == 'exception':
        monkeypatch.setattr(graph_backup, 'latest_snapshot', Mock(side_effect=OSError('synthetic discovery error')))
    record = Mock(return_value={'id': 1})
    monkeypatch.setattr(ops_events, 'record', record)
    result = graph_backup.run_offsite()
    assert result['ok'] is False and result['reason']
    record.assert_called_once()
    assert record.call_args.args[:2] == ('graph_snapshot_offsite', 'fail')
    subprocess.run.assert_not_called()


def test_nightly_lock_covers_independent_maintenance(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(jobs, '_run_script', lambda *args, **kwargs: 0)
    def upkeep():
        entered.set()
        assert release.wait(5)
        return {'ok': True}
    monkeypatch.setattr(jobs, '_embed_backfill', upkeep)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(jobs.run_nightly)
        try:
            assert entered.wait(2)
            assert pool.submit(jobs.run_nightly).result(timeout=1) == 75
        finally:
            release.set()
        assert first.result(timeout=5) == 0


@pytest.mark.parametrize('name', ['nightly', 'monthly'])
def test_wrapper_exclusion_across_real_processes(tmp_path, name):
    """Two separate Python callers share one flock and one completion receipt."""
    driver = tmp_path / 'driver.py'
    driver.write_text('import os,time\nfrom pathlib import Path\n'
                      f'root=Path({str(tmp_path)!r})\n'
                      '(root/"active").write_text(str(os.getpid()))\n'
                      'deadline=time.monotonic()+5\n'
                      'while not (root/"release").exists() and time.monotonic()<deadline: time.sleep(.01)\n')
    runner = tmp_path / 'runner.py'
    runner.write_text('import sys,socket\nfrom pathlib import Path\n'
                      'from khipu import jobs\n'
                      'def forbidden(*a,**kw): raise AssertionError("external operation forbidden")\n'
                      'socket.socket=forbidden\n'
                      f'root=Path({str(tmp_path)!r})\n'
                      'jobs.ensure_data_dir=lambda: root\n'
                      'jobs._log_paths=lambda stem: (root/"driver.out",root/"driver.err")\n'
                      f'jobs.CONSOLIDATE_{name.upper()}=root/"driver.py"\n'
                      'jobs.DRIVER_TIMEOUT_SECONDS=3\n'
                      'jobs.DRIVER_TERMINATE_GRACE_SECONDS=.1\n'
                      'jobs._drain_sessions=lambda: {"ok":True,"failed":0}\n'
                      f'for attr in {MAINTENANCE!r}: setattr(jobs,attr,lambda: {{"ok":True}})\n'
                      f'raise SystemExit(jobs.run_{name}())\n')
    jobs._write_job_state(name, 9)
    receipt = tmp_path / f'state/job-{name}.json'
    before = receipt.read_bytes()
    env = dict(os.environ, PYTHONPATH=str(Path(jobs.__file__).resolve().parents[1]))
    processes = []
    with (tmp_path / 'runner.log').open('wb') as log:
        try:
            first = subprocess.Popen([sys.executable, str(runner)], env=env,
                                     stdout=log, stderr=log, start_new_session=True)
            processes.append(first)
            deadline = time.monotonic()+2
            while not (tmp_path/'active').exists() and time.monotonic()<deadline:
                time.sleep(.01)
            assert (tmp_path/'active').exists()
            second = subprocess.Popen([sys.executable, str(runner)], env=env,
                                      stdout=log, stderr=log, start_new_session=True)
            processes.append(second)
            assert second.wait(timeout=2) == 75
            assert first.poll() is None
            assert receipt.read_bytes() == before
            (tmp_path/'release').touch()
            assert first.wait(timeout=3) == 0
            assert json.loads(receipt.read_text())['exit'] == 0
        finally:
            for process in processes:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
            if (tmp_path/'active').exists():
                try:
                    os.killpg(int((tmp_path/'active').read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass


def test_monthly_missing_driver_keeps_exception_and_failure_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, 'CONSOLIDATE_MONTHLY', tmp_path/'missing.py')
    with pytest.raises(FileNotFoundError):
        jobs.run_monthly()
    assert json.loads((tmp_path/'state/job-monthly.json').read_text())['exit'] == 2
