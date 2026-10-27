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
    push._record()
    routing.view_file().unlink()  # a refold is due
    push._set(home, routing_prompt_budget_ms=50)  # the shipped budget: half of it was the old wait
    home["search"]["delay"] = 0.6  # the hook takes an answer finished while it searched
    held, done = threading.Event(), threading.Event()

    def other_fold():
        with routing._fold_turn():  # another prompt's (or the poll's) fold
            held.set()
            time.sleep(0.15)
            routing.read_view()  # it saves the view, then lets go
        done.set()

    thread = threading.Thread(target=other_fold)
    thread.start()
    assert held.wait(5)
    push._push_on(monkeypatch)
    try:
        out = push._cold(monkeypatch)
    finally:
        thread.join(10)
    assert done.is_set()
    assert push._routing_part(out).startswith(push.FULL), push._log_lines()[-1:]
    assert push._last_log().endswith(" routing=full")


def test_the_routing_lock_keeps_its_short_wait(monkeypatch):
    """Only the fold lock's wait is lengthened: the routing lock (the save)
    keeps the budget's half."""
    seen = {}

    def fake_view(now, on_fold=None):
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
