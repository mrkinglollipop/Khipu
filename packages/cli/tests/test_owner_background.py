"""Prompt confirmations and durable completion, without live owner state."""
import json
import time

import pytest

from khipu import owner_act, owner_background, owner_direction
from tests.test_t3_owner_confirm import world, _hold_in_t3, _t3_hook_turn, THREAD

REAL_SPAWN = owner_background._spawn


def test_long_confirmation_returns_before_work_and_records_the_final_count(world, monkeypatch):
    request = _hold_in_t3(world)
    monkeypatch.setattr(owner_background, "LONG_ACTIONS", {"route.pause"})
    world["t3"].message(THREAD, time.time() + 1, text="yes")
    _t3_hook_turn(prompt="yes")
    started = time.monotonic()
    ok, out = owner_act.act("direction.confirm", {"request": request})
    assert ok and out["started"] and not out["applied"]
    assert time.monotonic() - started < 2
    assert "finish on its own" in out["line"]
    row = next(r for r in owner_direction._read_requests(owner_direction.requests_path()) if r["id"] == request)
    assert row["state"] == "applying"
    real = owner_act._run
    def counted(action, argv):
        ok, result = real(action, argv)
        return ok, dict(result, retired=107)
    monkeypatch.setattr(owner_act, "_run", counted)
    ok, result = owner_background.run(request)
    assert ok and result["retired"] == 107
    row = next(r for r in owner_direction._read_requests(owner_direction.requests_path()) if r["id"] == request)
    assert row["state"] == "applied" and row["result"]["retired"] == 107
    assert owner_background.run(request)[1]["retired"] == 107


def test_real_detached_process_finishes_after_the_confirmation_returns(world, monkeypatch):
    request = _hold_in_t3(world)
    monkeypatch.setattr(owner_background, "LONG_ACTIONS", {"route.pause"})
    monkeypatch.setattr(owner_background, "_spawn", REAL_SPAWN)
    world["t3"].message(THREAD, time.time() + 1, text="yes")
    _t3_hook_turn(prompt="yes")
    ok, out = owner_act.act("direction.confirm", {"request": request})
    assert ok and out["started"], out
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = json.loads(owner_background.path(request).read_text())
        if job["state"] == "finished":
            break
        time.sleep(0.02)
    assert job["state"] == "finished" and job["ok"], job
    assert next(r for r in owner_direction._read_requests(owner_direction.requests_path()) if r["id"] == request)["state"] == "applied"
    action = next(r for r in owner_direction.actions() if r["action"] == "route.pause")
    assert action["state"] == "applied"


def test_worker_failure_records_failed_instead_of_unconfirmed(world, monkeypatch):
    request = _hold_in_t3(world)
    monkeypatch.setattr(owner_background, "LONG_ACTIONS", {"route.pause"})
    world["t3"].message(THREAD, time.time() + 1, text="yes")
    _t3_hook_turn(prompt="yes")
    assert owner_act.act("direction.confirm", {"request": request})[1]["started"]
    def broken(action, argv):
        owner_direction.gate(owner_direction.intent("route.pause", "Codex"))
        raise RuntimeError("fixture failure")
    monkeypatch.setattr(owner_act, "_run", broken)
    ok, result = owner_background.run(request)
    assert not ok and result["code"] == "failed"
    assert next(r for r in owner_direction._read_requests(owner_direction.requests_path()) if r["id"] == request)["state"] == "failed"
    assert next(r for r in owner_direction.actions() if r["action"] == "route.pause")["state"] == "failed"


def test_a_direct_owner_turn_starts_long_work_without_an_extra_confirmation(world, monkeypatch):
    from tests.test_owed_paging_bulk import _direction
    _direction(world["tmp"], monkeypatch)
    def never_inline(*args):
        raise AssertionError("long work ran inside the conversation")
    monkeypatch.setattr(owner_act, "_run", never_inline)
    ok, out = owner_act.act("ledger.set", {"id": "task", "project": "example/widget", "state": "done"})
    assert ok and out["started"] and out["applied"] is False, out


def test_spawn_failure_is_reported_and_not_left_applying(world, monkeypatch):
    request = _hold_in_t3(world)
    monkeypatch.setattr(owner_background, "LONG_ACTIONS", {"route.pause"})
    def failed_spawn(request):
        raise OSError("fixture launch failure")
    monkeypatch.setattr(owner_background, "_spawn", failed_spawn)
    world["t3"].message(THREAD, time.time() + 1, text="yes")
    _t3_hook_turn(prompt="yes")
    ok, out = owner_act.act("direction.confirm", {"request": request})
    assert not ok and out["code"] == "start_failed"
    assert next(r for r in owner_direction._read_requests(owner_direction.requests_path()) if r["id"] == request)["state"] == "failed"
    assert json.loads(owner_background.path(request).read_text())["state"] == "finished"


def test_queued_work_keeps_no_second_copy_of_the_owners_words(world, monkeypatch):
    from tests.test_t3_owner_confirm import T3_SESSION
    request = _hold_in_t3(world)
    monkeypatch.setattr(owner_background, "LONG_ACTIONS", {"route.pause"})
    world["t3"].message(THREAD, time.time() + 1, text="yes")
    _t3_hook_turn(prompt="yes")
    assert owner_act.act("direction.confirm", {"request": request})[1]["started"]
    job = json.loads(owner_background.path(request).read_text())
    assert job["state"] == "started" and set(job) <= {"state", "started_ts", "pid", "worker"}  # ids and times, no words
    owner_direction.forget_requests({T3_SESSION})
    def never_run(*args):
        raise AssertionError("forgotten work ran")
    monkeypatch.setattr(owner_act, "_run", never_run)
    ok, result = owner_background.run(request)
    assert not ok and result["code"] == "request_gone"


def _started(world, monkeypatch):
    """A confirmed long request whose worker has been launched (the launch itself is stubbed)."""
    request = _hold_in_t3(world)
    monkeypatch.setattr(owner_background, "LONG_ACTIONS", {"route.pause"})
    launched = []
    monkeypatch.setattr(owner_background, "_spawn", lambda ident: launched.append(ident) or 0)
    world["t3"].message(THREAD, time.time() + 1, text="yes")
    _t3_hook_turn(prompt="yes")
    assert owner_act.act("direction.confirm", {"request": request})[1]["started"]
    return request


def _row(request):
    return next(r for r in owner_direction._read_requests(owner_direction.requests_path()) if r["id"] == request)


def _worker(seconds):
    import subprocess, sys
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


def _die(world, monkeypatch, *, entry: bool):
    """A confirmed request whose worker died; with ``entry``, after writing its owner-actions line ahead."""
    request = _started(world, monkeypatch)
    if entry:
        owner_direction._append({"kind": "action", "id": "a-1", "at": owner_direction._iso(owner_direction._clock()),
                                 "action": "route.pause", "target": "Codex", "state": "pending",
                                 "confirmed": {"request": request}})
    worker = _worker(60)
    owner_background._save(request, {"state": "started", "started_ts": time.time(), "pid": worker.pid,
                                     "worker": owner_background._identity(worker.pid)})
    assert owner_background.heal() == []  # alive: left alone
    worker.kill()
    worker.wait()
    assert owner_background.heal() == [request]
    row = _row(request)
    assert row["state"] == "interrupted" and row["error_code"] == "interrupted"
    assert owner_background.heal() == []  # settled once
    assert json.loads(owner_background.path(request).read_text())["state"] == "finished"
    return request


def test_a_worker_killed_after_its_action_entry_is_in_owner_actions_and_not_on_the_list(world, monkeypatch):
    from khipu import needs_you
    _die(world, monkeypatch, entry=True)
    # Not replayed, and the owner-actions log shows it unfinished: nothing more for the owner to answer.
    assert next(r for r in owner_direction.actions() if r["action"] == "route.pause")["state"] == "unconfirmed"
    assert needs_you.waiting(record_read=False)["items"] == []


def test_a_worker_killed_before_any_action_entry_is_still_listed(world, monkeypatch):
    from khipu import needs_you
    request = _die(world, monkeypatch, entry=False)
    assert owner_direction.actions() == []  # the log has no trace of it
    (item,) = needs_you.waiting(record_read=False)["items"]
    assert item["kind"] == "interrupted" and item["key"] == f"interrupted:{request}"
    assert "didn't finish" in item["words"] and "pause an account" in item["words"]
    assert "Say it again" in item["do"] and request not in item["words"]


def test_a_live_slow_worker_is_never_called_interrupted(world, monkeypatch):
    request = _started(world, monkeypatch)
    worker = _worker(60)
    try:
        owner_background._save(request, {"state": "started", "started_ts": time.time() - 30 * 24 * 3600, "pid": worker.pid,
                                         "worker": owner_background._identity(worker.pid)})
        assert owner_background.heal() == []
        assert _row(request)["state"] == "applying"
    finally:
        worker.kill()
        worker.wait()


def test_a_reused_pid_is_not_taken_for_the_worker(world, monkeypatch):
    request = _started(world, monkeypatch)
    worker = _worker(60)
    try:
        owner_background._save(request, {"state": "started", "started_ts": time.time(), "pid": worker.pid,
                                         "worker": "Mon Jan  1 00:00:00 2001"})
        assert owner_background.heal() == [request]
    finally:
        worker.kill()
        worker.wait()


def test_a_job_whose_worker_has_not_yet_recorded_itself_gets_time_to_start(world, monkeypatch):
    request = _started(world, monkeypatch)
    assert owner_background.heal() == []  # just started, no pid yet
    assert owner_background.heal(time.time() + owner_background.NO_WORKER_GRACE_S + 5) == [request]


def test_a_finished_job_is_untouched_by_the_timer(world, monkeypatch):
    request = _started(world, monkeypatch)
    ok, _ = owner_background.run(request)
    assert ok and owner_background.heal(time.time() + 10**7) == []
    assert _row(request)["state"] == "applied"


def test_crash_after_claim_before_start_is_recovered(world, monkeypatch):
    request = _hold_in_t3(world)
    monkeypatch.setattr(owner_background, "LONG_ACTIONS", {"route.pause"})
    world["t3"].message(THREAD, time.time() + 1, text="yes")
    _t3_hook_turn(prompt="yes")
    # Stop at the exact boundary: confirm saved applying, start never ran.
    owner_direction.confirm(request)
    assert _row(request)["state"] == "applying"
    assert owner_background.heal() == []
    assert owner_background.heal(time.time() + owner_background.NO_WORKER_GRACE_S + 5) == [request]
    assert _row(request)["state"] == "interrupted"
    assert owner_background.heal(time.time() + 10**7) == []


def test_crash_before_claim_leaves_request_confirmable(world, monkeypatch):
    request = _hold_in_t3(world)
    monkeypatch.setattr(owner_background, "LONG_ACTIONS", {"route.pause"})
    owner_background.prepare(_row(request))
    assert owner_background.heal(time.time() + 10**7) == []
    assert _row(request)["state"] == "pending"


@pytest.mark.parametrize("remote", [False, True])
def test_inline_claim_is_interrupted_only_after_its_process_dies(world, monkeypatch, remote):
    import select
    import subprocess
    import sys

    request = _hold_in_t3(world)
    # Remote confirmations run inline even for an otherwise long action.
    code = f"""
import time
from khipu import owner_direction, owner_background
if {remote!r}:
    owner_background.LONG_ACTIONS = {{"route.pause"}}
owner_direction.confirm({request!r}, terminal=True,
    remote={{"mac": "other", "id": "answer", "message_sha": "fixture"}} if {remote!r} else None)
print("claimed", flush=True)
time.sleep(60)
"""
    worker = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert select.select([worker.stdout], [], [], 10)[0], "claiming process did not respond"
        assert worker.stdout.readline() == b"claimed\n"
        job = json.loads(owner_background.path(request).read_text())
        assert job["pid"] == worker.pid and job["worker"] == owner_background._identity(worker.pid)
        assert _row(request)["state"] == "applying"
        assert owner_background.heal(time.time() + 10**7) == []
        assert _row(request)["state"] == "applying"  # slow, but still alive
        worker.kill()
        worker.wait(timeout=10)
        assert owner_background.heal() == [request]
        assert _row(request)["state"] == "interrupted"
        assert owner_background.heal() == []
        assert json.loads(owner_background.path(request).read_text())["state"] == "finished"
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.communicate(timeout=10)


def test_normally_finished_inline_action_closes_its_prepared_record(world):
    request = _hold_in_t3(world)
    world["t3"].message(THREAD, time.time() + 1, text="yes")
    _t3_hook_turn(prompt="yes")
    ok, result = owner_act.act("direction.confirm", {"request": request})
    assert ok and not result.get("started"), result
    assert _row(request)["state"] == "applied"
    assert json.loads(owner_background.path(request).read_text())["state"] == "started"
    assert owner_background.heal() == []
    job = json.loads(owner_background.path(request).read_text())
    assert job["state"] == "finished" and job["ok"] is True
