"""Real-process checks for throwaway PostgreSQL cleanup."""
from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.hermetic_pg import (
    INITDB, PG_CTL, Cluster, _free_port, _process_start, pg_cluster,
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


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_interrupted_start_stops_server(tmp_path, monkeypatch, failure):
    original_start = Cluster.start
    captured = {}

    def interrupted_start(cluster):
        original_start(cluster)
        captured["cluster"] = cluster
        captured["pid"] = int((cluster.data / "postmaster.pid").read_text().splitlines()[0])
        raise failure("interrupted before start returned")

    monkeypatch.setattr(Cluster, "start", interrupted_start)
    fixture = pg_cluster.__wrapped__(None)
    try:
        with pytest.raises(failure):
            next(fixture)
        _wait_for(lambda: _process_start(captured["pid"]) is None, timeout=5)
        assert not captured["cluster"].root.exists()
    finally:
        fixture.close()
        # The red version deletes PGDATA while its server is still running.
        if "pid" in captured and _process_start(captured["pid"]) is not None:
            os.kill(captured["pid"], signal.SIGQUIT)
            _wait_for(lambda: _process_start(captured["pid"]) is None)


def test_cluster_uses_small_posix_shared_memory(tmp_path):
    cluster = Cluster(tmp_path, _free_port())
    try:
        cluster.start()
        with cluster.admin() as conn:
            assert conn.execute("SHOW dynamic_shared_memory_type").fetchone()[0] == "posix"
            assert conn.execute("SELECT pg_size_bytes(current_setting('shared_buffers'))").fetchone()[0] <= 16 * 1024**2
    finally:
        cluster.stop()
        _cleanup(cluster.root)


def test_watchdog_cleans_up_after_sigkill(tmp_path):
    temp_root = tmp_path / "child"
    temp_root.mkdir()
    ready = tmp_path / "ready.json"
    child = subprocess.Popen(
        [sys.executable, "-c", """
import json, sys, time
from pathlib import Path
from tests.hermetic_pg import pg_cluster
fixture = pg_cluster.__wrapped__(None)
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


def test_normal_fixture_teardown_reaps_watchdog():
    fixture = pg_cluster.__wrapped__(None)
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


@pytest.mark.parametrize("death", ["SIGTERM", "SIGKILL", "SIGINT", "failure", "exit"])
def test_pytest_process_cleanup(tmp_path, death):
    ready = tmp_path / "ready.json"
    test = tmp_path / "test_child.py"
    test.write_text(f"""
import json, time
from pathlib import Path
from tests.hermetic_pg import Cluster, pg_cluster

# The probe needs a running server, not a migrated database.
Cluster.build_template = lambda self: None

def test_child(pg_cluster):
    Path({str(ready)!r}).write_text(json.dumps({{
        "root": str(pg_cluster.root), "watchdog": pg_cluster.watchdog.pid,
        "server": int((pg_cluster.data / "postmaster.pid").read_text().splitlines()[0]),
    }}))
    if {death!r} == "failure":
        assert False, "deliberate child test failure"
    if {death!r} != "exit":
        time.sleep(120)
""")
    with (tmp_path / "child.log").open("w") as log:
        child = subprocess.Popen(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(test)],
            env={**os.environ, "HOME": str(tmp_path)},
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        info = None
        try:
            _wait_for(lambda: ready.exists() or child.poll() is not None, timeout=60)
            assert ready.exists(), (tmp_path / "child.log").read_text()
            info = json.loads(ready.read_text())
            if death.startswith("SIG"):
                child.send_signal(getattr(signal, death))
            code = child.wait(timeout=30)
            if death in ("SIGTERM", "SIGKILL"):
                assert code == -getattr(signal, death)
            if death == "failure":
                assert code == 1
            if death == "exit":
                assert code == 0
            _wait_for(lambda: not Path(info["root"]).exists()
                      and _process_start(info["server"]) is None
                      and _process_start(info["watchdog"]) is None)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
            if info is not None:
                _cleanup(Path(info["root"]))
                _wait_for(lambda: _process_start(info["watchdog"]) is None)


@pytest.mark.parametrize("death", [signal.SIGTERM, signal.SIGKILL])
def test_killed_during_pg_ctl_startup(tmp_path, death):
    ready = tmp_path / "ready.json"
    wrapper = tmp_path / "pg_ctl"
    wrapper.write_text(f"""#!{sys.executable}
import json, os, subprocess, sys, time
from pathlib import Path
result = subprocess.run([{PG_CTL!r}, *sys.argv[1:]])
if sys.argv[-1] == "start" and result.returncode == 0:
    data = Path(sys.argv[sys.argv.index("-D") + 1])
    Path({str(ready)!r}).write_text(json.dumps({{
        "root": str(data.parent), "watchdog": os.getppid(),
        "server": int((data / "postmaster.pid").read_text().splitlines()[0]),
    }}))
    time.sleep(3)
sys.exit(result.returncode)
""")
    wrapper.chmod(0o700)
    child = subprocess.Popen(
        [sys.executable, "-c", f"""
import tempfile
from pathlib import Path
from tests import hermetic_pg as pg
pg.PG_CTL = {str(wrapper)!r}
cluster = pg.Cluster(Path(tempfile.mkdtemp(prefix="khipu-hermetic-pg-")), pg._free_port())
try:
    cluster.start()
finally:
    cluster.stop()
"""], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    info = None
    try:
        _wait_for(lambda: ready.exists() or child.poll() is not None, timeout=60)
        assert ready.exists(), f"startup child exited with {child.poll()}"
        info = json.loads(ready.read_text())
        child.send_signal(death)
        assert child.wait(timeout=10) == -death
        _wait_for(lambda: not Path(info["root"]).exists()
                  and _process_start(info["server"]) is None
                  and _process_start(info["watchdog"]) is None)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
        if info is not None:
            _cleanup(Path(info["root"]))
            _wait_for(lambda: _process_start(info["watchdog"]) is None)


def test_atexit_stops_cluster_without_fixture(tmp_path):
    ready = tmp_path / "ready.json"
    root = tmp_path / "cluster"
    root.mkdir()
    result = subprocess.run(
        [sys.executable, "-c", f"""
import json
from pathlib import Path
from tests.hermetic_pg import Cluster, _free_port
cluster = Cluster(Path({str(root)!r}), _free_port())
cluster.start()
Path({str(ready)!r}).write_text(json.dumps({{
    "server": int((cluster.data / "postmaster.pid").read_text().splitlines()[0]),
    "watchdog": cluster.watchdog.pid,
}}))
"""], capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr.decode()
    info = json.loads(ready.read_text())
    assert not root.exists()
    assert _process_start(info["server"]) is None
    assert _process_start(info["watchdog"]) is None


def test_modules_share_one_session_cluster(tmp_path):
    records = tmp_path / "clusters.jsonl"
    (tmp_path / "conftest.py").write_text("""
from tests.hermetic_pg import Cluster

def pytest_configure(config):
    Cluster.build_template = lambda self: None
""")
    for name in ("first", "second"):
        (tmp_path / f"test_{name}.py").write_text(f"""
import json
from pathlib import Path
from tests.hermetic_pg import pg_cluster

def test_cluster(pg_cluster):
    with Path({str(records)!r}).open("a") as out:
        out.write(json.dumps({{
            "root": str(pg_cluster.root),
            "server": int((pg_cluster.data / "postmaster.pid").read_text().splitlines()[0]),
        }}) + "\\n")
""")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tmp_path)],
        env={**os.environ, "HOME": str(tmp_path)}, capture_output=True, timeout=180)
    assert result.returncode == 0, result.stdout.decode() + result.stderr.decode()
    clusters = [json.loads(line) for line in records.read_text().splitlines()]
    assert len(clusters) == 2
    assert len({c["root"] for c in clusters}) == 1
    for cluster in clusters:
        assert not Path(cluster["root"]).exists()
        assert _process_start(cluster["server"]) is None


@pytest.mark.parametrize("diagnostic, skipped", [
    ("FATAL: could not create shared memory segment: No space left on device", True),
    ("FATAL: could not create semaphores: No space left on device", True),
    ("FATAL: could not write file: No space left on device", False),
    ("FATAL: could not create shared memory segment: Permission denied", False),
])
def test_resource_exhaustion_skips_clearly(tmp_path, monkeypatch, diagnostic, skipped):
    import tests.hermetic_pg as pg

    def cannot_start(cluster):
        (cluster.root / "log").write_text(diagnostic)
        raise subprocess.CalledProcessError(1, [PG_CTL, "start"], "", "see server log")

    monkeypatch.setattr(Cluster, "start", cannot_start)
    fixture = pg_cluster.__wrapped__(None)
    error = pytest.skip.Exception if skipped else subprocess.CalledProcessError
    with pytest.raises(error, match="resources exhausted" if skipped else None):
        next(fixture)


def test_stop_failure_preserves_data(tmp_path, monkeypatch):
    import tests.hermetic_pg as pg

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "postmaster.pid").write_text("123")
    monkeypatch.setattr(pg.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a, 1, b"", b"stop failed"))
    with pytest.raises(RuntimeError, match="could not stop test PostgreSQL"):
        pg._stop_orphan(tmp_path, PG_CTL)
    assert (tmp_path / "data" / "postmaster.pid").exists()
