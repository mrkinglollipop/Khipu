"""Round 8: backup member aliases and orphaned legacy grandchildren.

A zip member is judged by its normalised name, so nested/../ cannot reach
the registry or the replica by a route that skips merge or staging. A legacy
writer's grandchild keeps the memory lock held through its process group, so
killing the intermediary cannot let a forget complete before it writes.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

from khipu import capture, db, hub_snapshot, outbox, paths, tombstones
from tests.test_forget_write_gate import P, _complete_forget, _target


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith(("KHIPU_", "ALZY_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.setenv("KHIPU_TEST", "1")
    monkeypatch.setattr(outbox, "_queued_origin", lambda: {"origin": None})
    db._TABLE_COLUMNS_CACHE.clear()


def _zip(path: Path, members: dict[str, bytes | str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def test_registry_alias_is_refused_and_forget_stays_fenced(tmp_path, monkeypatch):
    _complete_forget(tmp_path, monkeypatch)
    empty = json.dumps({"version": 1, "source_keys": [], "payload_hashes": [],
                        "legacy_identities": []})
    backup = _zip(tmp_path / "alias-registry.zip", {"nested/../forgotten_sources.json": empty})
    with pytest.raises(RuntimeError, match="unsafe zip path"):
        paths.import_local(source=backup)
    assert tombstones.is_forgotten(P)
    with pytest.raises(tombstones.ForgottenSource):
        outbox.enqueue(dict(P), reason="post-forget replay")


def test_replica_alias_is_refused_and_replica_stays_scrubbed(tmp_path, monkeypatch):
    replica = hub_snapshot.snapshot_path()
    replica.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(replica) as con:
        hub_snapshot._create_schema(con)
    assert hub_snapshot.upsert_episode(_target(P), [])["ok"]
    old_replica = replica.read_bytes()
    _complete_forget(tmp_path, monkeypatch)
    backup = _zip(tmp_path / "alias-replica.zip", {"nested/../hub_snapshot.sqlite": old_replica})
    with pytest.raises(RuntimeError, match="unsafe zip path"):
        paths.import_local(source=backup)
    with sqlite3.connect(hub_snapshot.snapshot_path()) as con:
        rows = con.execute("SELECT id FROM episodes WHERE deleted_at IS NULL").fetchall()
    assert rows == []


@pytest.mark.parametrize("members", [
    {"/abs/x.txt": "x"},
    {"a\\b.txt": "x"},
    {"C:/x.txt": "x"},
    {"c:x.txt": "x"},
    {"../x.txt": "x"},
    {"a/../../x.txt": "x"},
    {"x.txt": "1", "./x.txt": "2"},
    {"a/b.txt": "1", "a//b.txt": "2"},
])
def test_unsafe_or_duplicate_members_refuse_the_whole_import(tmp_path, members):
    backup = _zip(tmp_path / "bad.zip", {"keep.txt": "kept", **members})
    with pytest.raises(RuntimeError, match="refusing"):
        paths.import_local(source=backup)
    assert not (paths.ensure_data_dir() / "keep.txt").exists()


def test_symlinked_folder_cannot_alias_the_registry(tmp_path):
    target = paths.ensure_data_dir()
    (target / "link").symlink_to(target)
    backup = _zip(tmp_path / "link.zip", {"link/forgotten_sources.json": "{}"})
    with pytest.raises(RuntimeError, match="protected file"):
        paths.import_local(source=backup)
    assert not (target / "forgotten_sources.json").exists()


def test_safe_nested_members_still_import(tmp_path):
    backup = _zip(tmp_path / "ok.zip", {"notes/a.txt": "a", "./b.txt": "b"})
    result = paths.import_local(source=backup)
    assert result["imported"] == 2
    target = paths.ensure_data_dir()
    assert (target / "notes" / "a.txt").read_text() == "a"
    assert (target / "b.txt").read_text() == "b"


def _wait_file(path: Path, seconds: float = 10) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists(), f"timed out waiting for {path}"


def _fake_legacy(tmp_path: Path, episodes: Path, *, exit_at_once: bool) -> dict[str, Path]:
    """A legacy script that, like the real one, starts the mechanical writer
    without forwarding any lock descriptor, here as a detached grandchild."""
    files = {name: tmp_path / name for name in ("ready", "resume", "parent_pid", "grandchild_pid")}
    grandchild = tmp_path / "mechanical.py"
    grandchild.write_text(
        "import os, sys, time\n"
        "from pathlib import Path\n"
        f"Path({str(files['grandchild_pid'])!r}).write_text(str(os.getpid()))\n"
        f"Path({str(files['ready'])!r}).write_text('ready')\n"
        f"while not Path({str(files['resume'])!r}).exists(): time.sleep(0.01)\n"
        f"with open({str(episodes)!r}, 'a') as f: f.write(sys.argv[1] + '\\n')\n",
        encoding="utf-8")
    legacy = tmp_path / "legacy_v2.py"
    legacy.write_text(
        "import json, os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "payload = json.load(sys.stdin)\n"
        f"subprocess.Popen([sys.executable, {str(grandchild)!r}, json.dumps(payload)],\n"
        "                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
        "                 stderr=subprocess.DEVNULL, close_fds=True)\n"
        f"Path({str(files['parent_pid'])!r}).write_text(str(os.getpid()))\n"
        + ("" if exit_at_once else "time.sleep(60)\n"),
        encoding="utf-8")
    files["legacy"] = legacy
    return files


def test_orphaned_grandchild_holds_the_lock_until_it_has_written(tmp_path, monkeypatch):
    case = tmp_path / "case"
    case.mkdir()
    episodes = case / "memory" / "episodes.jsonl"
    files = _fake_legacy(tmp_path, episodes, exit_at_once=False)
    monkeypatch.setattr(capture, "_capture_v2", lambda: files["legacy"])
    results, failures, forgets = [], [], []

    def writer():
        try:
            results.append(capture.run_capture_v2(dict(P), suppress_mirror=True))
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)

    def forget_now():
        try:
            forgets.append(_complete_forget(case, monkeypatch))
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)

    capture_thread = threading.Thread(target=writer)
    forget_thread = threading.Thread(target=forget_now)
    capture_thread.start()
    try:
        _wait_file(files["ready"])
        _wait_file(files["parent_pid"])
        os.kill(int(files["parent_pid"].read_text()), 9)
        forget_thread.start()
        time.sleep(1.0)
        assert not failures, failures
        assert capture_thread.is_alive(), "capture released its locks with the grandchild still running"
        assert forget_thread.is_alive() and not forgets, "forget completed before the grandchild wrote"
    finally:
        files["resume"].write_text("resume")
        capture_thread.join(15)
        if forget_thread.ident is not None:
            forget_thread.join(15)
    assert not failures, failures
    assert results and forgets and forgets[0]["complete"]
    assert P["summary"] not in episodes.read_text(), "grandchild appended after forget completed"


def test_timeout_kills_the_whole_group_before_returning(tmp_path):
    files = _fake_legacy(tmp_path, tmp_path / "episodes.jsonl", exit_at_once=True)
    with pytest.raises(subprocess.TimeoutExpired):
        capture._run_legacy_group([sys.executable, str(files["legacy"])],
                                  input=json.dumps({"summary": "s"}), env=dict(os.environ),
                                  timeout=2, pass_fds=())
    pid = int(files["grandchild_pid"].read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not (tmp_path / "episodes.jsonl").exists()
