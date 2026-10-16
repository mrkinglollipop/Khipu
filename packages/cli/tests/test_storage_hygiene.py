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
