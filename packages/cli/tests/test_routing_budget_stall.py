"""Budget-stall regressions from the October 28 diagnosis; synthetic ledgers only."""
import copy
import math
from datetime import timedelta

import pytest

from khipu import routing_delivery as delivery, routing_health as health
from tests.test_routing_acceptance import replay, T0  # noqa: F401
from tests.test_routing_paging_invariants import _item, _out


def _plan(items):
    out = _out(items, ["Synthetic account A " + "s" * 70,
                       "Synthetic account B " + "s" * 70], 400, force=True)
    out["delivery_plan"].update(at=delivery._iso(T0), lease=3600)
    return out


def _send(out, point, *, ack=True):
    now = T0 + timedelta(seconds=point)
    out = copy.deepcopy(out)
    out["delivery_plan"]["at"] = delivery._iso(now)
    answer = delivery.offer(out, session="budget-test")
    if ack and answer.get("routing_changes"):
        assert delivery.acknowledge("budget-test", answer["routing_changes"], now=now)
    return delivery.snapshot("budget-test")


@pytest.mark.parametrize("entity,length", [("rule:15", 757), ("priority", 6941)])
def test_default_cap_pages_remain_healthy_at_real_width(replay, entity, length):
    items = [_item("rule:14", "a" * 482, "Standing rule 14 for Claude, Codex, Grok", True),
             _item(entity, "b" * length, "Standing rule 15 for Claude, Codex, Grok", True)]
    out = _plan(items)
    for point in range(120):
        state = _send(out, point)
        assert "budget-stall" not in health.component_health(state, T0)["reasons"]
        if all(e["acked"] for e in state["entries"].values()):
            break
    else:
        pytest.fail("paging did not finish")
    assert state["entries"][entity]["budget"] <= 111


@pytest.mark.parametrize("ahead", [("priority",), ("priority", "rule:1", "rule:14")])
def test_revised_debt_ahead_is_credited_once_without_resetting_older_age(replay, ahead):
    items = [*[_item(entity, "a" * 200, entity, True) for entity in ahead],
             _item("rule:15", "b" * 757, "Standing rule 15", True)]
    out = _plan(items)
    first = _send(out, 0)
    old = first["entries"]["rule:15"]
    changed = [dict(item, text="c" * 482, revision="new-revision") for item in items[:-1]]
    out = _plan([*changed, items[-1]])
    state = _send(out, 1, ack=False)
    later = state["entries"]["rule:15"]
    assert later["owed_point"] == old["owed_point"]
    assert later["owed_chars"] == old["owed_chars"] + 482 * len(ahead)
    again = _send(out, 2, ack=False)
    assert again["entries"]["rule:15"]["owed_chars"] == later["owed_chars"]


def test_narrowest_recent_page_width_rescales_all_waiting_debt(replay):
    out = _plan([_item("rule:14", "a" * 482, "Standing rule 14 for Claude, Codex, Grok", True),
                 _item("rule:15", "b" * 757, "Standing rule 15", True)])
    state = _send(out, 0)
    measured = state["entries"]["rule:14"]["width"]
    later = state["entries"]["rule:15"]
    assert later["budget"] <= measured <= 111
    assert later["bound"] == math.ceil(later["owed_chars"] / later["budget"])


def test_legacy_seven_character_width_is_healed_to_the_scaled_floor(replay):
    out = _plan([_item("rule:5", "x" * 1000, "Standing rule 5", True)])
    _send(out, 0)
    with delivery._transaction("budget-test") as data:
        entry = data["main"]["entries"]["rule:5"]
        entry.update(width=7, budget=7, bound=143)
        data["main"].pop("flight", None)
    state = _send(out, 1)
    entry = state["entries"]["rule:5"]
    assert entry["width"] >= 80
    assert entry["budget"] >= 80
    assert entry["bound"] == math.ceil(entry["owed_chars"] / entry["budget"])


def test_progress_restarts_the_stall_clock_but_duplicate_batches_do_not(replay):
    out = _plan([_item("rule:5", "x" * 1000, "Standing rule 5", True)])
    state = _send(out, 0)
    # A long-lived ledger may already have aged past its original deadline.
    with delivery._transaction("budget-test") as data:
        data["main"]["delivery_points"] = 100
    state = _send(out, 1)
    assert "budget-stall" not in health.component_health(state, T0)["reasons"]
    bound = state["entries"]["rule:5"]["bound"]
    for point in range(bound + 2):
        state = _send(out, point + 2, ack=False)
    assert "budget-stall" in health.component_health(state, T0)["reasons"]


def test_delivery_with_no_new_content_stays_red(replay):
    out = _plan([_item("rule:5", "x" * 1000, "Standing rule 5", True)])
    state = _send(out, 0, ack=False)
    bound = state["entries"]["rule:5"]["bound"]
    for point in range(bound + 2):
        state = _send(out, point + 1, ack=False)
    assert "budget-stall" in health.component_health(state, T0)["reasons"]
