"""Prompt confirmations and durable completion, without live owner state."""
import json
import time

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
    assert json.loads(owner_background.path(request).read_text()) == {"state": "started"}
    owner_direction.forget_requests({T3_SESSION})
    def never_run(*args):
        raise AssertionError("forgotten work ran")
    monkeypatch.setattr(owner_act, "_run", never_run)
    ok, result = owner_background.run(request)
    assert not ok and result["code"] == "request_gone"
