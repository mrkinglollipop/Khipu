"""Routing lines reach prompts (2026-10-27 diagnosis of skipped routing):
a prompt waits for another fold up to its hook's deadline; a view with pace
readings is not refolded every second; a delivery point is an answer the
hook took; doctor's prompt check counts only skips for lack of time, plus
a conversation kept waiting several prompts in a row. Hermetic."""
from __future__ import annotations

import fcntl
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from khipu import recall_prompt, routing, routing_delivery as delivery, routing_health as health
from tests import test_routing_acceptance as acceptance
from tests import test_routing_audit_fold as audit
from tests import test_routing_push_prompt as push

home = push.home
replay = acceptance.replay
world = audit.world
T0 = audit.T0


# ---- 1: the per-prompt child waits for another fold up to the hook's deadline ----------------


def test_a_fold_that_holds_the_lock_briefly_is_waited_for_and_delivered(home, monkeypatch):
    """The recall daemon's helper path (its children count against the cap;
    a one-shot hook's child keeps the half-budget wait)."""
    push._record()
    home["clock"].now = datetime.now(timezone.utc).replace(microsecond=0)
    push._set(home, routing_prompt_budget_ms=50)  # the shipped budget: half of it was the old wait
    home["search"]["delay"] = 0.6  # the hook takes an answer finished while it searched
    push._push_on(monkeypatch)
    svc, thread = push._fresh_daemon(home, "wait")
    try:
        assert svc.routing_forkserver.wait_ready(10)
        routing.view_file().unlink()  # a refold is due
        held, done = threading.Event(), threading.Event()

        def other_fold():
            with routing._fold_turn():  # another prompt's (or the poll's) fold
                held.set()
                time.sleep(0.15)
                routing.read_view()  # it saves the view, then lets go
            done.set()

        holder = threading.Thread(target=other_fold)
        holder.start()
        assert held.wait(5)
        first = len(push._log_lines()) + 1
        push._timed(svc)
        holder.join(10)
        assert done.is_set()
        assert push._statuses(first, 1) == ["full"], push._log_lines()[-1:]
    finally:
        push._end(svc, thread)


def test_the_routing_lock_keeps_its_short_wait(monkeypatch):
    """Only the fold lock's wait is lengthened: the routing lock (the save)
    keeps the budget's half."""
    seen = {}

    def fake_view(now, on_fold=None, clock=None):
        seen["deadline"] = routing._held.deadline - time.monotonic()
        seen["fold"] = routing._held.fold_deadline - time.monotonic()
        raise routing.FoldBusy("stop here")

    monkeypatch.setattr(routing, "_prompt_view", fake_view)
    with pytest.raises(routing.FoldBusy):
        routing.prompt_push(None, budget=0.05, fold_wait=0.8)
    assert seen["deadline"] <= 0.025 + 1e-3 and 0.7 < seen["fold"] <= 0.8 + 1e-3
    with pytest.raises(routing.FoldBusy):
        routing.prompt_push(None, budget=0.05)  # no fold_wait: half the budget, as before
    assert seen["fold"] <= 0.025 + 1e-3


def test_a_burst_behind_a_held_fold_lock_stays_within_the_child_cap(home, monkeypatch):
    push._record()
    home["clock"].now = datetime.now(timezone.utc).replace(microsecond=0)
    push._set(home, routing_prompt_budget_ms=50)
    routing.view_file().unlink()  # every child wants to fold
    push._push_on(monkeypatch)
    svc, thread = push._fresh_daemon(home, "burst")
    lock = open(routing.routing_dir() / routing.PROMPT_FOLD_LOCK, "a")
    try:
        assert svc.routing_forkserver.wait_ready(10)
        helper = svc.routing_forkserver._proc
        fcntl.flock(lock, fcntl.LOCK_EX)  # a fold that outlasts every prompt's deadline
        first = len(push._log_lines()) + 1
        count = recall_prompt._FORKSERVER_CHILDREN_MOST + 6
        most = 0
        for i in range(count):
            push._timed(svc, f"{push.PROMPT} {i}")
            most = max(most, len(push._live_children(helper.pid)))
        statuses = push._statuses(first, count)
        assert all(s in ("skipped:budget", "skipped:busy", "skipped:fold-busy") for s in statuses), statuses
        assert most <= recall_prompt._FORKSERVER_CHILDREN_MOST
        fcntl.flock(lock, fcntl.LOCK_UN)
        push._wait_children_ended(helper, 30)
    finally:
        lock.close()
        push._end(svc, thread)


# ---- 2: pace readings do not refold the view every second ------------------------------------


def _pace_world(witness: datetime, used: float = 40) -> None:
    rows = [{"id": f"sample-{i}", "type": "usage_sample", "written_at": routing._iso(witness - timedelta(minutes=m)),
             "account": "codex|", "window": "five_hour", "witness_at": routing._iso(witness - timedelta(minutes=m)),
             "resets_at": routing._iso(T0 + timedelta(hours=3)), "used_pct": used - m / 10,
             "mac": "synthetic", "source": "probe"} for i, m in enumerate((50, 40, 30, 20, 10, 0))]
    rows.append({"id": "cap", "type": "cap_set", "written_at": routing._iso(T0), "account": "codex|",
                 "window": "five_hour", "kind": "pace_target", "pct": 100, "slack": 0, "revision": "cap-r1",
                 "written_by": {"kind": "owner"}})
    path = routing.events_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _codex(view):
    return next(a for a in view["accounts"] if a["key"] == "codex|")


def test_pace_readings_do_not_refold_the_view_every_second(world, monkeypatch):
    _pace_world(T0)
    folds = []
    real = routing.fold
    monkeypatch.setattr(routing, "fold", lambda *a, **k: folds.append(1) or real(*a, **k))
    first = routing.read_view()
    assert routing._parse(first["valid_until"]) == T0 + timedelta(seconds=30)
    count = len(folds)
    for seconds in (1, 2, 5, 13, 29):
        world[0] = T0 + timedelta(seconds=seconds)
        cached = routing.read_view()
        assert cached["folded_at"] == first["folded_at"] and len(folds) == count, seconds
        direct = real(routing.read_events()[0], now=world[0], cfg=routing.settings(), known=routing.accounts())
        # The pace line the prompt builds from the cached view is the one a fresh fold gives.
        assert routing.pace_line_for(_codex(cached), world[0]) == routing.pace_line_for(_codex(direct), world[0])
        assert _codex(cached)["pace"][0]["pace_target"]["verdict"] == _codex(direct)["pace"][0]["pace_target"]["verdict"]
    world[0] = T0 + timedelta(seconds=30)
    assert routing.read_view()["folded_at"] != first["folded_at"]  # the floor refolds it


def test_a_reading_going_stale_inside_the_floor_refolds_at_its_second(world):
    # The newest reading is 19 min 50 s old: stale (over 2 x 10 min) 10 s from now.
    _pace_world(T0 - timedelta(minutes=19, seconds=50))
    first = routing.read_view()
    assert routing._parse(first["valid_until"]) == T0 + timedelta(seconds=11)
    assert _codex(first)["pace"][0]["pace_target"]["verdict"] != "unknown"
    world[0] = T0 + timedelta(seconds=11)
    cached = routing.read_view()
    direct = routing.fold(routing.read_events()[0], now=world[0], cfg=routing.settings(), known=routing.accounts())
    assert _codex(cached)["pace"][0]["pace_target"] == _codex(direct)["pace"][0]["pace_target"]
    assert _codex(cached)["pace"][0]["pace_target"]["verdict"] == "unknown"


# ---- 3: a delivery point is an answer the hook took ------------------------------------------


def _owed(replay):
    routing.report(account="Codex", state="open", author=acceptance.AUTHOR)
    out, _, info = replay.offer()
    assert out.get("routing_changes"), out
    return info


def test_offers_the_hook_discarded_are_not_delivery_points(replay):
    _owed(replay)
    for _ in range(12):  # the hook discarded every answer (late, stale, busy)
        replay.offer()
    state = delivery.snapshot("s1")
    assert state["delivery_points"] == 0
    assert "budget-stall" not in health.component_health(state, replay.now)["reasons"]
    replay.advance(6 * 3600)  # an idle conversation: nothing taken, nothing ages
    state = delivery.snapshot("s1")
    assert health.component_health(state, replay.now)["status"] == "green"
    _, _, info = replay.offer()
    replay.ack(info)  # taken and sent: one point, and the debt is paid
    state = delivery.snapshot("s1")
    assert state["delivery_points"] == 1
    assert health.component_health(state, replay.now)["status"] == "green"


def test_answers_taken_without_the_change_still_stall(replay):
    _owed(replay)
    for _ in range(4):
        _, _, info = replay.offer()
        replay.ack(info, delivered=False)  # taken, never shown
    state = delivery.snapshot("s1")
    assert state["delivery_points"] == 4
    assert "budget-stall" in health.component_health(state, replay.now)["reasons"]


def test_debt_aged_by_the_old_offer_count_starts_again_at_the_next_take(replay):
    from tests.test_routing_s16_health import planned

    for _ in range(6):
        delivery.offer(planned(), session="s1")  # the old count: every offer a point
    assert "budget-stall" in health.component_health(delivery.snapshot("s1"), T0)["reasons"]
    delivery.offer(planned(), session="s1", deferred=True)
    state = delivery.snapshot("s1")
    assert state["point_basis"] == "take"
    assert "budget-stall" not in health.component_health(state, T0)["reasons"]


def test_a_take_with_no_ledger_makes_none(replay):
    assert delivery.take_point("never-offered")
    assert not delivery._path("never-offered").exists()


# ---- 4: doctor's prompt check ----------------------------------------------------------------


def _log(rows):
    path = recall_prompt._log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = routing._iso(acceptance.T0) + " [khipu-prompt-recall] "
    path.write_text("".join(f"{prefix}session={s} reason=no_hits ms=1 hits=[] legs=[] degraded=None routing={st}\n"
                            for s, st in rows))


def test_only_out_of_time_skips_count_against_the_slow_read_share(replay):
    # 7% skipped for a changed record (sent with the next prompt), spread out.
    _log([(f"s{i}", "skipped:stale" if i % 16 == 0 else "none") for i in range(100)])
    result = health.prompt_health(acceptance.T0, 1.0)
    assert result["status"] == "green" and result["skipped"] == 7 and result["out_of_time"] == 0
    _log([(f"s{i}", "skipped:budget" if i in (10, 50) else "none") for i in range(100)])
    assert health.prompt_health(acceptance.T0, 1.0)["reasons"] == ["prompt-skips"]
    assert health.prompt_health(acceptance.T0, 5.0)["status"] == "green"
    assert health.prompt_health(acceptance.T0, 0)["status"] == "green"  # 0: never warns
    replay.configure(routing_slow_read_pct=5)
    block = routing.doctor_block(now=acceptance.T0)["routing_delivery"]
    assert "prompt-skips" not in block["reasons"] and block["prompts"]["warn_pct"] == 5


def test_three_skips_in_a_row_in_one_conversation_is_a_delay(replay):
    rows = [(f"s{i}", "none") for i in range(200)]
    # Two in a row, then routed, then two: never three. Other sessions between them do not join runs.
    for at, status in ((10, "skipped:stale"), (11, "skipped:fold-busy"), (12, "none"),
                       (13, "skipped:stale"), (14, "skipped:stale")):
        rows.insert(at * 2, ("one", status))
    rows.insert(100, ("two", "skipped:no-session"))
    rows.insert(101, ("two", "skipped:no-session"))
    rows.insert(102, ("two", "skipped:no-session"))
    _log(rows)
    result = health.prompt_health(acceptance.T0, 5.0)
    assert result["status"] == "green" and result["longest_skip_run"] == 2, result
    _log(rows + [("one", "skipped:unsaved")])
    result = health.prompt_health(acceptance.T0, 5.0)
    assert result["reasons"] == ["prompt-delay"] and result["longest_skip_run"] == 3


# ---- review round 1 (Grok leads, 2026-10-27) -------------------------------------------------


def _wait_behind(world, holder_work):
    """A prompt whose view is due waits for another fold's lock; ``holder_work``
    runs while it waits. Returns the (view, now) the prompt built its lines from."""
    import os

    seen = {}

    def text(marker, view, now, cfg, *, session_id=None):
        seen.update(view=view, now=now)
        return {"kind": "none", "_delivery_view": view}

    held, waiting = threading.Event(), threading.Event()

    def other():
        with routing._fold_turn():
            held.set()
            waiting.wait(5)
            time.sleep(0.05)  # the prompt is now inside its fold-lock wait
            holder_work()

    thread = threading.Thread(target=other)
    thread.start()
    assert held.wait(5)
    real_take = routing._take_fold_lock

    def take(fd):
        waiting.set()
        return real_take(fd)

    with pytest.MonkeyPatch.context() as m:
        m.setattr(routing, "_prompt_text", text)
        m.setattr(routing, "_take_fold_lock", take)
        routing._prompt_push(None, budget=0.05, fold_wait=2.0)
    thread.join(10)
    assert os.path.exists(routing.view_file())
    return seen["view"], seen["now"]


def test_a_view_that_falls_due_during_the_fold_wait_is_not_served(world):
    _pace_world(T0 - timedelta(minutes=19, seconds=50))
    routing.read_view()  # valid until T0 + 11 s
    world[0] = T0 + timedelta(seconds=12)  # due: the prompt waits for the other fold

    def holder():
        routing._start_fold(world[0], save=True)  # saves a view valid until T0 + 42 s
        world[0] = T0 + timedelta(seconds=50)  # and the clock moves on past it while the prompt waits

    view, now = _wait_behind(world, holder)
    assert now == T0 + timedelta(seconds=50)
    assert routing._parse(view["valid_until"]) > now, (view["valid_until"], now)


def test_a_waiter_past_its_routing_lock_deadline_still_folds_and_saves(world):
    _pace_world(T0)
    routing.read_view()
    world[0] = T0 + timedelta(seconds=31)  # due
    view, now = _wait_behind(world, lambda: time.sleep(0.1))  # the other fold saves nothing
    assert view.get("saved") is not False
    assert routing._load_view()["folded_at"] == routing._iso(now)


def test_one_shot_children_do_not_pile_up_behind_a_held_fold_lock(home, monkeypatch):
    import os

    push._record()
    push._set(home, routing_prompt_budget_ms=50)
    routing.view_file().unlink()
    push._push_on(monkeypatch)
    lock = open(routing.routing_dir() / routing.PROMPT_FOLD_LOCK, "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        most = 0
        for i in range(recall_prompt._FORKSERVER_CHILDREN_MOST + 6):
            push._cold(monkeypatch)
            most = max(most, len(push._live_children(os.getpid())))
        assert most <= recall_prompt._FORKSERVER_CHILDREN_MOST, most
    finally:
        lock.close()


def test_the_helpers_poll_counts_against_the_child_cap(home, monkeypatch):
    push._record()
    home["clock"].now = datetime.now(timezone.utc).replace(microsecond=0)
    push._set(home, routing_prompt_budget_ms=50, routing_poll_interval=1)
    routing.view_file().unlink()
    push._push_on(monkeypatch)
    svc, thread = push._fresh_daemon(home, "pollcap")
    lock = open(routing.routing_dir() / routing.PROMPT_FOLD_LOCK, "a")
    try:
        assert svc.routing_forkserver.wait_ready(10)
        helper = svc.routing_forkserver._proc
        fcntl.flock(lock, fcntl.LOCK_EX)  # every child and every poll waits
        most, end = 0, time.monotonic() + 4.0
        while time.monotonic() < end:
            push._timed(svc)
            most = max(most, len(push._live_children(helper.pid)))
        assert most <= recall_prompt._FORKSERVER_CHILDREN_MOST, most
        fcntl.flock(lock, fcntl.LOCK_UN)
        push._wait_children_ended(helper, 30)
    finally:
        lock.close()
        push._end(svc, thread)
