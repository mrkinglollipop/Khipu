"""A hub that stops answering must not hold a timer run: each hub stage of `khipu snapshot heal` has a wall-clock limit."""
from __future__ import annotations

import argparse
import json
import threading
import time

import pytest

from khipu import cli, deadline, hub_snapshot, jobs, owner_direction, replica_heal as rh, routing, storage
from tests.test_combined_recovery import fake_world  # noqa: F401 - fixture


def test_the_limit_stops_a_blocked_call():
    started = time.monotonic()
    with pytest.raises(deadline.StageTimeout, match="the stage"):
        with deadline.limit(0.3, "the stage"):
            time.sleep(30)
    assert time.monotonic() - started < 3


def test_a_stage_that_swallows_the_first_stop_is_stopped_on_its_next_call():
    started = time.monotonic()
    with pytest.raises(deadline.StageTimeout):
        with deadline.limit(0.3, "the stage"):
            try:
                time.sleep(30)
            except Exception:  # noqa: BLE001 - the shape of sync(): it reports the failure and goes on
                pass
            time.sleep(30)
    assert time.monotonic() - started < 6


def test_the_limit_does_nothing_off_the_main_thread_and_leaves_no_timer_behind():
    import signal

    seen = []
    thread = threading.Thread(target=lambda: seen.append(_run_quick_limit()))
    thread.start()
    thread.join(5)
    assert seen == ["ran"]
    with deadline.limit(5, "quick"):
        pass
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def _run_quick_limit():
    with deadline.limit(0.01, "off the main thread"):
        time.sleep(0.1)
    return "ran"


def test_a_hub_that_answers_the_handshake_then_goes_silent_is_called_unreachable(monkeypatch):
    class Silent:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return self

        def execute(self, _sql):
            time.sleep(60)

    monkeypatch.setattr(hub_snapshot, "HUB_CONNECT_TIMEOUT_S", 0.2)
    monkeypatch.setattr(hub_snapshot, "try_hub_connect", lambda **_kw: Silent())
    started = time.monotonic()
    reachable, why = rh._hub_reachable()
    assert not reachable and "stopped" in why
    assert time.monotonic() - started < 6


def test_a_stalled_routing_copy_is_cut_off_and_the_other_timer_stages_still_finish(fake_world, monkeypatch, capsys):  # noqa: F811
    calls = []
    monkeypatch.setattr(storage, "sweep_if_due", lambda: calls.append("storage") or {"ok": True})
    monkeypatch.setattr(rh, "run", lambda: calls.append("replica") or {"ok": True, "action": "none"})
    monkeypatch.setattr(owner_direction, "timer", lambda: calls.append("owner") or {"ok": True})
    monkeypatch.setattr(jobs, "_write_job_state", lambda *a: calls.append("job_state"))
    monkeypatch.setattr(cli, "ROUTING_COPY_MAX_S", 0.4)
    monkeypatch.setattr(routing, "poll_refold", lambda: time.sleep(60))
    started = time.monotonic()
    rc = cli.cmd_snapshot(argparse.Namespace(snapshot_cmd="heal"))
    out = json.loads(capsys.readouterr().out)
    assert time.monotonic() - started < 8
    assert rc == 0 and calls == ["storage", "replica", "owner", "job_state"]
    assert out["routing_copy"]["ok"] is False and "stopped" in out["routing_copy"]["error"]
    assert out["owner_jobs"] == {"interrupted": []}
