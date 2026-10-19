"""Real-process checks for throwaway PostgreSQL cleanup."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.hermetic_pg import (
    INITDB, OWNER, PG_CTL, Cluster, _free_port, _process_start, _sweep_orphans,
    _system_temp, pg_cluster,
)

pytestmark = pytest.mark.skipif(not INITDB or not PG_CTL,
                                reason="needs PostgreSQL's initdb and pg_ctl on PATH")


def _wait_for(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    assert predicate(), "cleanup did not finish before the deadline"


def _cleanup(root):
    if (root / "data" / "postmaster.pid").exists():
        result = subprocess.run([PG_CTL, "-D", str(root / "data"), "-m", "immediate", "stop"],
                                capture_output=True, timeout=60)
        assert result.returncode == 0, result.stderr.decode()
    shutil.rmtree(root, ignore_errors=True)


def test_watchdog_cleans_up_after_sigkill(tmp_path):
    temp_root = tmp_path / "child"
    temp_root.mkdir()
    ready = tmp_path / "ready.json"
    child = subprocess.Popen(
        [sys.executable, "-c", """
import json, sys, time
from pathlib import Path
from tests.hermetic_pg import pg_cluster
fixture = pg_cluster.__wrapped__()
cluster = next(fixture)
Path(sys.argv[1]).write_text(json.dumps({
    "root": str(cluster.root), "watchdog": cluster.watchdog.pid,
    "server": int((cluster.data / "postmaster.pid").read_text().splitlines()[0]),
}))
time.sleep(120)
""", str(ready)], env={**os.environ, "TMPDIR": str(temp_root)},
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    info = None
    try:
        _wait_for(lambda: ready.exists() or child.poll() is not None, timeout=60)
        assert child.poll() is None, f"cluster child exited with {child.returncode}"
        info = json.loads(ready.read_text())
        root = Path(info["root"])
        assert _process_start(info["server"]) is not None
        assert _process_start(info["watchdog"]) is not None
        child.kill()
        child.wait(timeout=10)
        _wait_for(lambda: not root.exists() and _process_start(info["server"]) is None
                  and _process_start(info["watchdog"]) is None)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
        for root in temp_root.glob("khipu-hermetic-pg-*"):
            _cleanup(root)
        if info is not None:
            _wait_for(lambda: _process_start(info["watchdog"]) is None)


@pytest.mark.parametrize("reused_pid", [False, True])
def test_sweep_only_stops_dead_owners(tmp_path, reused_pid):
    roots = [tmp_path / "khipu-hermetic-pg-dead",
             tmp_path / "kt-old" / "khipu-hermetic-pg-live",
             tmp_path / "kt-old" / "khipu-hermetic-pg-unowned"]
    clusters = []
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=10)
    try:
        for root in roots:
            root.mkdir(parents=True)
            cluster = Cluster(root, _free_port())
            clusters.append(cluster)
            cluster.start()
            # Exercise the sweep independently of the watchdog.
            cluster.watchdog.terminate()
            cluster.watchdog.wait(timeout=10)
        owner = {"pid": os.getpid() if reused_pid else dead.pid,
                 "start": "a different process start"}
        (roots[0] / OWNER).write_text(json.dumps(owner))
        (roots[2] / OWNER).unlink()
        server_pids = [int((c.data / "postmaster.pid").read_text().splitlines()[0])
                       for c in clusters]
        _sweep_orphans(tmp_path, PG_CTL)
        assert not roots[0].exists()
        assert _process_start(server_pids[0]) is None
        for cluster, pid in zip(clusters[1:], server_pids[1:]):
            assert cluster.root.exists()
            assert _process_start(pid) is not None
    finally:
        for cluster in clusters:
            _cleanup(cluster.root)
            if cluster.watchdog is not None:
                cluster.watchdog.wait(timeout=10)


def test_normal_fixture_teardown_reaps_watchdog():
    fixture = pg_cluster.__wrapped__()
    cluster = None
    try:
        cluster = next(fixture)
        watchdog = cluster.watchdog
        assert watchdog is not None
        assert watchdog.poll() is None
        fixture.close()
        assert not cluster.root.exists()
        assert watchdog.poll() == 0
        assert _process_start(watchdog.pid) is None
    finally:
        fixture.close()
        if cluster is not None:
            _cleanup(cluster.root)
            cluster.watchdog.wait(timeout=10)


def test_system_temp_unwraps_redirected_roots(tmp_path, monkeypatch):
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "kt-first" / "kt-second"))
    assert _system_temp() == tmp_path
