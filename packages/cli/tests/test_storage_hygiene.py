"""Storage hygiene (docs/plans/2026-10-16-offline-and-self-healing.md, slice 8):
Khipu never keeps files nobody reads."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from khipu import hub_snapshot as hs


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("KHIPU_DATA_DIR", str(state))
    monkeypatch.setenv("KHIPU_TEST", "1")
    return state


def _age(path: Path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


def _temps(directory: Path) -> list[str]:
    return sorted(n for n in os.listdir(directory) if hs.is_replica_temp(n))


def _hub_fails(exc: BaseException):
    def connect(*_a, **_k):
        raise exc
    return connect


def test_a_failed_refresh_removes_its_temp_and_any_left_by_an_earlier_crash(data_dir, monkeypatch):
    left = data_dir / ".hub_snapshot.49e_lyak.sqlite"
    journal = data_dir / ".hub_snapshot.49e_lyak.sqlite-journal"
    for path in (left, journal):
        path.write_bytes(b"pages")
        _age(path, hs.STALE_TEMP_AGE_S + 60)
    monkeypatch.setattr(hs, "try_hub_connect", _hub_fails(ConnectionError("hub away")))

    with pytest.raises(ConnectionError):
        hs.refresh()

    assert _temps(data_dir) == []


def test_an_interrupted_refresh_removes_its_temp_and_journal(data_dir, monkeypatch):
    monkeypatch.setattr(hs, "try_hub_connect", _hub_fails(KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt):
        hs.refresh()

    assert _temps(data_dir) == []


def test_the_sweep_keeps_a_young_temp_and_never_the_replica(data_dir):
    young = data_dir / ".hub_snapshot.staged1.sqlite"
    young.write_bytes(b"a staged import")
    old = data_dir / ".hub_snapshot.old1.sqlite-wal"
    old.write_bytes(b"pages")
    _age(old, hs.STALE_TEMP_AGE_S + 1)
    replica = data_dir / "hub_snapshot.sqlite"
    replica.write_bytes(b"the replica")
    _age(replica, 365 * 86400)
    meta = data_dir / ".hub_snapshot.meta.abc.json"
    meta.write_bytes(b"{}")
    _age(meta, 365 * 86400)

    out = hs.sweep_stale_temps()

    assert out == {"ok": True, "removed": [old.name]}
    assert young.exists() and replica.exists() and meta.exists()


def test_the_sweep_removes_the_replaced_query_cache_and_keeps_its_successor(data_dir):
    from khipu import storage

    state = data_dir / "state"
    state.mkdir()
    legacy = state / "prompt-query-embed-cache.json"
    legacy.write_text("{}" * 100)
    current = state / "prompt-query-embed-cache.sqlite3"
    current.write_bytes(b"cache")

    out = storage.sweep()

    assert out["ok"] and "state/prompt-query-embed-cache.json" in out["removed"]
    assert out["freed_bytes"] >= 200
    assert not legacy.exists() and current.exists()


def test_the_replica_job_sweeps_once_a_day(data_dir, monkeypatch, capsys):
    import argparse

    from khipu import cli, jobs, replica_heal, storage

    monkeypatch.setattr(replica_heal, "run", lambda: {"ok": True, "action": "none"})
    monkeypatch.setattr(jobs, "_write_job_state", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(storage, "sweep", lambda: calls.append(1) or {"ok": True, "removed": [],
                                                                      "freed_bytes": 0, "errors": []})
    args = argparse.Namespace(snapshot_cmd="heal")

    assert cli.cmd_snapshot(args) == 0
    assert cli.cmd_snapshot(args) == 0

    assert calls == [1]
    assert '"storage_sweep"' in capsys.readouterr().out


# ---- settings, logs and usage lines ------------------------------------------


@pytest.fixture
def logs(data_dir, tmp_path, monkeypatch):
    from khipu import storage

    folder = tmp_path / "logs"
    folder.mkdir()
    monkeypatch.setattr(storage, "log_dirs", lambda: [folder])
    return folder


def test_storage_settings_are_set_checked_and_listed(data_dir, capsys):
    import json

    from khipu import cli, storage

    assert storage.settings()["commitments_backup_keep_days"] == 30
    assert cli.main(["config", "--set", "log_max_mb", "2.5"]) == 0
    assert storage.settings()["log_max_mb"] == 2.5
    capsys.readouterr()
    assert cli.main(["config", "--set", "disk_warn_free_percent", "101"]) == 2
    assert "0 to 100" in capsys.readouterr().out
    assert cli.main(["config", "--unset=log_max_mb"]) == 0
    capsys.readouterr()
    assert cli.main(["config"]) == 0
    block = json.loads(capsys.readouterr().out)["storage"]
    assert set(block) == set(storage.SETTINGS)
    assert block["log_max_mb"] == {"value": 5, "default": 5, "kind": "number", "source": "default",
                                   "words": storage.SETTINGS["log_max_mb"][2]}


def test_a_log_over_the_cap_is_rotated_and_a_small_one_is_not(logs, monkeypatch):
    from khipu import storage

    monkeypatch.setattr(storage, "settings", lambda: {**{k: v[1] for k, v in storage.SETTINGS.items()},
                                                      "log_max_mb": 1})
    big = logs / "khipu-nightly.out.log"
    big.write_bytes(b"x" * (1024 * 1024 + 10))
    _age(big, 3600)
    when = big.stat().st_mtime
    (logs / "khipu-nightly.out.log.1").write_bytes(b"an older copy")
    small = logs / "stop-hook.log"
    small.write_bytes(b"a line\n")

    out = storage.sweep()

    assert out["rotated"] == ["khipu-nightly.out.log"]
    assert big.stat().st_size == 0 and abs(big.stat().st_mtime - when) < 1
    assert (logs / "khipu-nightly.out.log.1").stat().st_size == 1024 * 1024 + 10
    assert small.read_bytes() == b"a line\n" and not (logs / "stop-hook.log.1").exists()
    assert sorted(os.listdir(logs)) == ["khipu-nightly.out.log", "khipu-nightly.out.log.1", "stop-hook.log"]


def test_old_usage_lines_are_dropped_and_recent_ones_kept(data_dir, logs):
    import json
    from datetime import datetime, timedelta, timezone

    from khipu import extract, storage

    now = datetime.now(timezone.utc)
    def line(days):
        at = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return json.dumps({"ts": at, "provider": "gemini", "model": "m", "prompt_tokens": 1,
                           "output_tokens": 1, "thought_tokens": 0, "cached_tokens": 0, "caller": "t"})
    path = extract.synth_usage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([line(200), line(91), "not json", line(10), line(0)]) + "\n")

    out = storage.sweep()

    assert out["synth_usage_dropped"] == 3
    assert path.read_text().splitlines() == [line(10), line(0)]
    assert extract.synth_usage_today()["calls"] == 1


# ---- commitments backups -------------------------------------------------------


class _Copy:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        yield b"PGCOPY\n"


class _Cur:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def copy(self, _sql):
        return _Copy()


class _Conn:
    def cursor(self):
        return _Cur()


def test_a_new_commitments_backup_prunes_ones_older_than_the_setting(data_dir, tmp_path):
    from datetime import datetime, timedelta, timezone

    from khipu import hygiene

    root = tmp_path / "backups"
    root.mkdir()
    def stamp(days):
        return "commitments-" + (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y%m%dT%H%M%SZ")
    old, recent = root / stamp(31), root / stamp(29)
    for folder in (old, recent):
        folder.mkdir()
        (folder / "commitments.bin").write_bytes(b"rows")
    lookalike = root / (stamp(400) + "-mine")
    lookalike.mkdir()
    (root / stamp(400)).with_suffix(".txt").write_text("not a folder")

    dest = hygiene.backup_commitments(_Conn(), dest_root=root)

    assert dest.is_dir() and recent.is_dir() and lookalike.is_dir()
    assert not old.exists()


def test_the_sweep_prunes_commitments_backups_and_zero_keeps_them(data_dir, tmp_path, monkeypatch):
    from khipu import hygiene, storage

    root = tmp_path / "backups"
    (root / "commitments-20200101T000000Z").mkdir(parents=True)
    monkeypatch.setattr(hygiene, "commitments_backup_dir", lambda: root)
    monkeypatch.setattr(storage, "log_dirs", lambda: [])

    storage.set_setting("commitments_backup_keep_days", 0)
    assert storage.sweep()["removed"] == []
    storage.set_setting("commitments_backup_keep_days", None)
    out = storage.sweep()

    assert "commitments-20200101T000000Z" in out["removed"] and not any(root.iterdir())


# ---- the test suite's own temporary folders ---------------------------------


def test_the_suite_keeps_its_temporary_folders_under_one_root_removed_at_exit():
    import subprocess
    import sys
    import tempfile

    from tests import conftest

    root = conftest.TEMP_ROOT
    assert tempfile.gettempdir() == root == os.environ["TMPDIR"]
    # Short enough that a socket under a test's own temp folder stays legal.
    assert Path(root).name.startswith("kt-") and len(root) < 80
    assert Path(tempfile.mkdtemp(prefix="khipu-x-")).parent == Path(root)

    # A fresh interpreter loading the conftest: its root exists while it runs
    # and is gone when it exits.
    conftest_path = Path(conftest.__file__)
    code = ("import runpy, tempfile, os; runpy.run_path(%r); "
            "open(os.path.join(tempfile.mkdtemp(prefix='khipu-'), 'f'), 'w').close(); "
            "print(tempfile.gettempdir())" % str(conftest_path))
    env = {k: v for k, v in os.environ.items() if k != "TMPDIR"}
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                          cwd=str(conftest_path.parents[1]), timeout=120)
    assert done.returncode == 0, done.stderr
    child_root = done.stdout.strip().splitlines()[-1]
    assert Path(child_root).name.startswith("kt-")
    assert not Path(child_root).exists()


# ---- disk space ----------------------------------------------------------------


def _usage(free_gb, total_gb=400):
    import shutil as _sh

    return lambda _p: _sh._ntuple_diskusage(int(total_gb * 1e9), int((total_gb - free_gb) * 1e9),
                                            int(free_gb * 1e9))


@pytest.mark.parametrize("free_gb, level", [(200, "ok"), (8, "amber"), (2.1, "red")])
def test_disk_space_judges_free_space_by_the_settings(data_dir, monkeypatch, free_gb, level):
    from khipu import storage

    monkeypatch.setattr(storage.shutil, "disk_usage", _usage(free_gb))

    out = storage.disk_space()

    assert out["level"] == level and out["ok"] is (level != "red")
    assert out["volumes"] and out["volumes"][0]["label"] == "Khipu's data"
    assert out["fix"] == {"label": "Open storage report", "kind": "storage-report"}
    if level == "red":
        assert out["message"].startswith("Only 2.1 GB is free on the disk that holds Khipu's data.")
        assert "Khipu cannot save new memory when the disk is full" in out["message"]
    storage.set_setting("disk_fail_free_gb", 0)
    storage.set_setting("disk_fail_free_percent", 0)
    assert storage.disk_space()["ok"] is True


def test_a_percent_threshold_counts_on_a_small_disk(data_dir, monkeypatch):
    from khipu import storage

    monkeypatch.setattr(storage.shutil, "disk_usage", _usage(15, total_gb=1000))

    assert storage.disk_space()["level"] == "red"



# ---- a full disk said plainly ------------------------------------------------


def _full(*_a, **_k):
    import errno

    raise OSError(errno.ENOSPC, "No space left on device")


def test_a_refresh_on_a_full_disk_says_so(data_dir, monkeypatch):
    from khipu import replica_heal

    monkeypatch.setattr(hs, "try_hub_connect", _full)

    out = replica_heal._full_dump()

    assert out["ok"] is False and out["error"].startswith("the disk is full")
    assert hs._read_sidecar()["refresh_error"]["error"].startswith("the disk is full")
    assert _temps(data_dir) == []


def test_the_outbox_on_a_full_disk_says_so(data_dir, tmp_path, monkeypatch):
    from khipu import outbox

    monkeypatch.setenv("KHIPU_OUTBOX", str(tmp_path / "outbox"))
    monkeypatch.setattr(outbox, "_queued_origin", lambda: {"origin": None})
    monkeypatch.setattr(outbox, "_atomic_write", _full)

    with pytest.raises(OSError, match="the capture could not be queued: the disk is full"):
        outbox.enqueue({"summary": "a note", "project": "p", "session_id": "claude:1", "harness": "claude"})


def test_a_local_backup_on_a_full_disk_says_so_and_leaves_no_half_archive(data_dir, tmp_path, monkeypatch):
    import zipfile

    from khipu import paths

    (data_dir / "config.json").write_text("{}")
    monkeypatch.setattr(zipfile.ZipFile, "write", _full)
    dest = tmp_path / "out" / "b.zip"

    out = paths.backup_local(dest=dest)

    assert out["ok"] is False and "the disk is full" in out["error"] and not dest.exists()


def test_a_commitments_backup_on_a_full_disk_says_so(data_dir, tmp_path, monkeypatch):
    from khipu import hygiene

    class _FullCopy(_Copy):
        def __iter__(self):
            _full()
            yield b""

    class _FullCur(_Cur):
        def copy(self, _sql):
            return _FullCopy()

    class _FullConn:
        def cursor(self):
            return _FullCur()

    with pytest.raises(OSError, match="commitments backup could not be written: the disk is full"):
        hygiene.backup_commitments(_FullConn(), dest_root=tmp_path / "backups")
    assert list((tmp_path / "backups").iterdir()) == []
