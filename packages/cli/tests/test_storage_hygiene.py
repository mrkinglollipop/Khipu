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
