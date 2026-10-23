"""Regression probes for the combined local-recovery failure modes.

These tests use only local files and fake hub boundaries.
"""
from __future__ import annotations

import argparse
import errno
import json
import subprocess
from pathlib import Path

import pytest

from khipu import cli, hub_snapshot as hs, jobs, owner_direction, owner_writes as ow
from khipu import promote as pm, replica_heal as rh, storage


@pytest.fixture(autouse=True)
def fake_world(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)
    monkeypatch.setattr(owner_direction, "timer", lambda: {})
    monkeypatch.setattr(jobs, "_write_job_state", lambda *a, **k: None)
    import psycopg

    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: pytest.fail("unexpected real hub connect"))
    return tmp_path


@pytest.fixture
def owner_destination(fake_world, monkeypatch):
    """Only the owner replay probes model a configured (unreachable) hub."""
    monkeypatch.setenv("KHIPU_DATABASE_URL", "postgresql://synthetic@127.0.0.1:5432/owner_fixture")


def test_enospc_heal_must_still_reach_storage_cleanup(fake_world, monkeypatch):
    data = fake_world / "data"
    state = data / "state"
    state.mkdir(parents=True)
    junk = state / storage.LEGACY_QUERY_CACHE
    junk.write_bytes(b"obsolete cache" * 1000)
    monkeypatch.setattr(hs, "snapshot_health", lambda: {
        "exists": True, "age_seconds": 1, "refreshed_at": rh._now().isoformat(),
        "schema_version": hs.SNAPSHOT_SCHEMA_VERSION})
    real_mkstemp = rh.tempfile.mkstemp

    def full(*a, **kw):
        if kw.get("prefix") == ".replica-heal.":
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_mkstemp(*a, **kw)

    monkeypatch.setattr(rh.tempfile, "mkstemp", full)
    failures = []
    for _ in range(2):
        try:
            cli.cmd_snapshot(argparse.Namespace(snapshot_cmd="heal"))
        except OSError as exc:
            failures.append(exc.errno)
    assert not junk.exists(), f"two timer ticks raised {failures}; obsolete cache still present={junk.exists()}"


def test_log_daemon_remains_capped_after_disk_full_fallback(fake_world, monkeypatch):
    logs = fake_world / "logs"
    logs.mkdir()
    log = logs / "daemon.log"
    log.write_bytes(b"x" * 4096)
    monkeypatch.setattr(storage, "log_dirs", lambda: [logs])
    monkeypatch.setattr(storage, "settings", lambda: {
        **{k: v[1] for k, v in storage.SETTINGS.items()}, "log_max_mb": 0.001})
    with log.open("ab", buffering=0) as daemon:
        with monkeypatch.context() as fault:
            def full(*a, **kw):
                raise OSError(errno.ENOSPC, "No space left on device")

            fault.setattr(storage.shutil, "copyfile", full)
            storage.sweep()
        daemon.write(b"y" * 4096)
        for _ in range(2):
            storage.sweep()
        older = log.with_name(log.name + ".1")
        assert older.stat().st_size <= 4096, (
            f"rotated daemon file grew to {older.stat().st_size}; "
            f"future sweep sees logs={storage.log_files()}")


def test_promotion_retry_survives_transient_git_failure(fake_world, monkeypatch):
    monkeypatch.setattr(pm, "_repo", lambda: fake_world)
    monkeypatch.setattr(pm, "active_slot", lambda: None)
    monkeypatch.setattr(pm, "retry_cleanups", lambda: [])
    monkeypatch.setattr(pm, "hub_reachable", lambda **kw: True)
    monkeypatch.setattr(pm, "_git_out", lambda args, **kw: "origin" if args == ["remote"] else "a" * 40)
    monkeypatch.setattr(pm, "_git", lambda args, **kw:
        subprocess.CompletedProcess(args, 128, "", "Could not resolve host: fake.invalid"))
    pm._queue("origin/main", "a" * 40, "hub unreachable", None)
    result = pm.retry_queued()
    assert not result["ok"] and "git fetch" in result["error"]
    assert pm.queued() is not None, f"retry discarded promotion: {result}"


@pytest.mark.usefixtures("owner_destination")
def test_owner_replay_preserves_write_after_transient_enospc(fake_world, monkeypatch):
    ow._enqueue("owed_update", {"id": 1, "action": "close"}, "hub away")
    monkeypatch.setattr(ow, "_landed", lambda *a: None)
    calls = []
    monkeypatch.setattr(ow, "_apply", lambda *a: calls.append(a) or {"ok": True})
    write = ow._write

    def disk_fault(path, job):
        if job.get("state") == "sending":
            raise OSError(errno.ENOSPC, "No space left on device")
        write(path, job)

    with monkeypatch.context() as fault:
        fault.setattr(ow, "_write", disk_fault)
        first = ow.drain()
    second = ow.drain()
    assert calls, f"owner action never applied after space recovered: first={first}, second={second}, status={ow.status()}"


# Every timer stage is a best-effort local recovery action. A failure must be
# visible, but no stage gets to starve the rest of the timer tick.
@pytest.mark.parametrize("failure", [RuntimeError("timer fault"), OSError(errno.ENOSPC, "disk full")])
@pytest.mark.parametrize("failed_stage", ["storage", "replica", "owner", "job_state"])
def test_heal_timer_stages_are_isolated_and_storage_runs_first(fake_world, monkeypatch, capsys, failure, failed_stage):
    calls = []

    def stage(name, result):
        def run(*_a, **_kw):
            calls.append(name)
            if name == failed_stage:
                raise failure
            return result
        return run

    monkeypatch.setattr(storage, "sweep_if_due", stage("storage", {"ok": True}))
    monkeypatch.setattr(rh, "run", stage("replica", {"ok": True, "action": "none"}))
    monkeypatch.setattr(owner_direction, "timer", stage("owner", {"ok": True}))
    monkeypatch.setattr(jobs, "_write_job_state", stage("job_state", None))

    rc = cli.cmd_snapshot(argparse.Namespace(snapshot_cmd="heal"))
    out = json.loads(capsys.readouterr().out)

    assert calls == ["storage", "replica", "owner", "job_state"]
    key = {"storage": "storage_sweep", "replica": "replica_heal",
           "owner": "owner_direction", "job_state": "job_state"}[failed_stage]
    if failed_stage == "replica":
        assert rc == 1
    else:
        assert rc == 0
    assert out[key]["ok"] is False and out[key]["error"]


@pytest.mark.parametrize("failure_point", ["read", "landed_check", "mark_sending", "apply"])
@pytest.mark.usefixtures("owner_destination")
def test_owner_local_io_failure_keeps_the_queued_write(fake_world, monkeypatch, failure_point):
    ow._enqueue("owed_update", {"id": 2, "action": "close"}, "hub away")
    monkeypatch.setattr(ow, "_landed", lambda *_: None)
    monkeypatch.setattr(ow, "_apply", lambda *_: {"ok": True})
    if failure_point == "read":
        real_read_text = Path.read_text

        def full_when_read(path, *args, **kwargs):
            if path.parent == ow.queue_dir() and path.suffix == ".json":
                raise OSError(errno.ENOSPC, "disk full")
            return real_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", full_when_read)
    elif failure_point == "landed_check":
        monkeypatch.setattr(ow, "_landed", lambda *_: (_ for _ in ()).throw(OSError(errno.ENOSPC, "disk full")))
    elif failure_point == "mark_sending":
        real_write = ow._write

        def full_when_sending(path, job):
            if job.get("state") == "sending":
                raise OSError(errno.ENOSPC, "disk full")
            real_write(path, job)

        monkeypatch.setattr(ow, "_write", full_when_sending)
    else:
        monkeypatch.setattr(ow, "_apply", lambda *_: (_ for _ in ()).throw(OSError(errno.ENOSPC, "disk full")))

    out = ow.drain()

    assert out["stopped_early"] is True
    status = ow.status()
    assert status["pending"] == 1 and status["refused"] == 0


@pytest.mark.parametrize("outcome", [("applied", None), ("refused", "owner denied")])
@pytest.mark.usefixtures("owner_destination")
def test_owner_replay_checks_hub_landing_before_sending_again(fake_world, monkeypatch, outcome):
    ow._enqueue("owed_update", {"id": 3, "action": "close"}, "hub away")
    monkeypatch.setattr(ow, "_landed", lambda *_: outcome)
    monkeypatch.setattr(ow, "_apply", lambda *_: pytest.fail("an already-recorded write was resent"))

    out = ow.drain()

    expected = "replayed" if outcome[0] == "applied" else "refused"
    assert out[expected] == 1
    assert ow.status()["pending"] == 0


@pytest.mark.usefixtures("owner_destination")
def test_owner_post_apply_ack_io_failure_keeps_the_write_for_landed_dedupe(fake_world, monkeypatch):
    ow._enqueue("owed_update", {"id": 4, "action": "close"}, "hub away")
    monkeypatch.setattr(ow, "_landed", lambda *_: None)
    sent = []
    monkeypatch.setattr(ow, "_apply", lambda *_: sent.append(True) or {"ok": True})
    real_remove = ow._remove
    attempts = []

    def fail_once(path):
        attempts.append(path)
        if len(attempts) == 1:
            raise OSError(errno.ENOSPC, "disk full")
        real_remove(path)

    monkeypatch.setattr(ow, "_remove", fail_once)
    first = ow.drain()
    assert first["stopped_early"] is True and ow.status()["pending"] == 1
    monkeypatch.setattr(ow, "_landed", lambda *_: ("applied", None))
    second = ow.drain()

    assert second["replayed"] == 1 and sent == [True] and ow.status()["pending"] == 0


@pytest.mark.usefixtures("owner_destination")
def test_only_an_explicit_hub_refusal_moves_an_owner_write_to_refused(fake_world, monkeypatch):
    ow._enqueue("owed_update", {"id": 5, "action": "close"}, "hub away")
    monkeypatch.setattr(ow, "_landed", lambda *_: None)
    monkeypatch.setattr(ow, "_apply", lambda *_: {"ok": False, "error": "owner denied"})

    out = ow.drain()

    assert out["refused"] == 1
    assert ow.status()["pending"] == 0 and ow.status()["refused"] == 1


@pytest.mark.parametrize("failure_point", ["git_fetch", "slot_prepare"])
def test_queued_promotion_keeps_retry_on_transient_local_failure(fake_world, monkeypatch, failure_point):
    monkeypatch.setattr(pm, "_repo", lambda: fake_world)
    monkeypatch.setattr(pm, "active_slot", lambda: None)
    monkeypatch.setattr(pm, "retry_cleanups", lambda: [])
    monkeypatch.setattr(pm, "hub_reachable", lambda **kw: True)
    queued = pm._queue("origin/main", "a" * 40, "hub unreachable", None)
    if failure_point == "git_fetch":
        monkeypatch.setattr(pm, "_git_out", lambda args, **kw: "origin" if args == ["remote"] else "a" * 40)
        monkeypatch.setattr(pm, "_git", lambda args, **kw:
            subprocess.CompletedProcess(args, 128, "", "Could not resolve host: fake.invalid"))
    else:
        monkeypatch.setattr(pm, "_fetch_and_resolve", lambda *_: {"ok": True, "commit": "a" * 40})
        monkeypatch.setattr(pm, "_prepare_slot", lambda *_: {"ok": False, "error": "temporary checkout failure"})

    out = pm.retry_queued()

    kept = pm.queued()
    assert not out["ok"] and kept is not None
    assert {key: kept[key] for key in ("ref", "commit", "queued_at")} == {
        key: queued[key] for key in ("ref", "commit", "queued_at")}


@pytest.mark.parametrize("outcome", [
    {"ok": True, "gate": []},
    {"ok": False, "cancelled": True, "gate": []},
    {"ok": False, "gate": [{"ok": False, "step": "doctor", "detail": "red"}]},
])
def test_only_terminal_promotion_outcomes_clear_a_queued_retry(fake_world, monkeypatch, outcome):
    pm._queue("origin/main", "a" * 40, "hub unreachable", None)
    monkeypatch.setattr(pm, "hub_reachable", lambda **kw: True)
    monkeypatch.setattr(pm, "_promote_locked", lambda *_: dict(outcome))

    pm.retry_queued()

    assert pm.queued() is None


def test_new_promotion_replaces_an_old_queued_target(fake_world, monkeypatch):
    pm._queue("origin/old", "old" * 13 + "o", "hub unreachable", None)
    monkeypatch.setattr(pm, "_promote_locked", lambda *_: {
        "ok": False, "commit": "new" * 13 + "n", "error": "hub away",
        "gate": [{"ok": False, "step": "recall", "hub_unreachable": True}],
    })

    pm.promote("origin/new")

    assert pm.queued()["ref"] == "origin/new"
    assert pm.queued()["commit"] == "new" * 13 + "n"


@pytest.mark.parametrize("location", ["daemon", "promotion_retry"])
def test_sweep_inventories_and_truncates_rotated_logs_without_second_suffix(fake_world, monkeypatch, location):
    logs = fake_world / "logs"
    logs.mkdir()
    if location == "daemon":
        rotated = logs / "daemon.log.1"
        monkeypatch.setattr(storage, "log_dirs", lambda: [logs])
    else:
        state = fake_world / "state"
        state.mkdir()
        rotated = state / "promote-retry.log.1"
        monkeypatch.setattr(storage, "log_dirs", lambda: [])
        monkeypatch.setattr(pm, "config_dir", lambda: fake_world)
    rotated.write_bytes(b"x" * 4096)
    monkeypatch.setattr(storage, "settings", lambda: {
        **{k: v[1] for k, v in storage.SETTINGS.items()}, "log_max_mb": 0.001})

    assert rotated in storage.log_files()
    storage.sweep()

    assert rotated.stat().st_size == 0
    assert not rotated.with_name(rotated.name + ".1").exists()


def test_sweep_bounds_active_and_rotated_live_daemon_descriptors(fake_world, monkeypatch):
    logs = fake_world / "logs"
    logs.mkdir()
    active = logs / "daemon.log"
    rotated = logs / "daemon.log.1"
    active.write_bytes(b"a" * 4096)
    rotated.write_bytes(b"r" * 4096)
    monkeypatch.setattr(storage, "log_dirs", lambda: [logs])
    monkeypatch.setattr(storage, "settings", lambda: {
        **{k: v[1] for k, v in storage.SETTINGS.items()}, "log_max_mb": 0.001})

    with active.open("ab", buffering=0) as active_daemon, rotated.open("ab", buffering=0) as rotated_daemon:
        storage.sweep()
        active_daemon.write(b"a" * 4096)
        rotated_daemon.write(b"r" * 4096)
        storage.sweep()

    assert active.stat().st_size <= 1048 and rotated.stat().st_size <= 1048
    assert not (logs / "daemon.log.1.1").exists()


def test_sweep_does_not_follow_a_rotated_log_symlink(fake_world, monkeypatch):
    logs = fake_world / "logs"
    logs.mkdir()
    active = logs / "daemon.log"
    active.write_bytes(b"a" * 4096)
    outside = fake_world / "outside.log"
    outside.write_bytes(b"do not overwrite")
    (logs / "daemon.log.1").symlink_to(outside)
    monkeypatch.setattr(storage, "log_dirs", lambda: [logs])
    monkeypatch.setattr(storage, "settings", lambda: {
        **{k: v[1] for k, v in storage.SETTINGS.items()}, "log_max_mb": 0.001})

    out = storage.sweep()

    assert not out["ok"] and outside.read_bytes() == b"do not overwrite"
    assert active.stat().st_size == 0


def test_sweep_reports_archive_enospc_while_capping_the_active_log(fake_world, monkeypatch):
    logs = fake_world / "logs"
    logs.mkdir()
    active = logs / "daemon.log"
    active.write_bytes(b"a" * 4096)
    monkeypatch.setattr(storage, "log_dirs", lambda: [logs])
    monkeypatch.setattr(storage, "settings", lambda: {
        **{k: v[1] for k, v in storage.SETTINGS.items()}, "log_max_mb": 0.001})
    monkeypatch.setattr(storage.shutil, "copyfile", lambda *_a, **_kw:
                        (_ for _ in ()).throw(OSError(errno.ENOSPC, "disk full")))

    out = storage.sweep()

    assert not out["ok"] and any("disk is full" in error for error in out["errors"])
    assert active.stat().st_size == 0
