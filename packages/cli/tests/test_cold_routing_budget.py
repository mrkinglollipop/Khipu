"""A cold one-shot prompt child gets extra time to fold the routing events log,
which the warm helper pays once; the alias and inventory build keeps 250 ms."""
from __future__ import annotations

import inspect
import time

import pytest

from khipu import agent_view, recall_prompt, routing


@pytest.fixture
def build(monkeypatch):
    for key in list(agent_view._TABLE):
        monkeypatch.delitem(agent_view._TABLE, key)
    state = {"folded": False, "signature_s": 0.0}

    def slow_fold(*_a, **_k):
        time.sleep(0.35)
        state["folded"] = True
        return {}

    def signature(keys):
        # Without the warmed fold, the build itself pays it.
        time.sleep(state["signature_s"] if state["folded"] else 0.35)
        return ("sig", "inventory-key")

    monkeypatch.setattr(routing, "_entry_cache", slow_fold)
    monkeypatch.setattr(agent_view, "_signature", signature)
    monkeypatch.setattr(agent_view, "_inventory_signature", lambda keys, key: ("inventory",))
    monkeypatch.setattr(agent_view, "_rules", lambda keys: {"table": True})
    return state


def test_the_warm_helper_still_gives_up_on_a_slow_fold(build):
    agent_view.prepare_routing_table(refresh=True)
    assert agent_view._TABLE.get("helper_table") is None


def test_a_cold_child_warms_the_fold_and_then_builds(build):
    agent_view.prepare_routing_table(refresh=True, cold=True)
    assert agent_view._TABLE.get("helper_table") == {"table": True}


def test_a_cold_child_still_withholds_a_slow_alias_build(build):
    # A stalled volume or a huge view is slow after the fold: still 250 ms.
    build["signature_s"] = 0.35
    agent_view.prepare_routing_table(refresh=True, cold=True)
    assert agent_view._TABLE.get("helper_table") is None


def test_the_cold_budgets_stay_inside_the_hook_time_limit():
    assert agent_view._BUILD_BUDGET_S == 0.25
    assert agent_view.COLD_FOLD_BUDGET_S + agent_view._BUILD_BUDGET_S < recall_prompt.TIMEOUT_S


def test_the_one_shot_prompt_child_asks_for_the_cold_fold():
    source = inspect.getsource(recall_prompt._prompt_routing_child)
    assert "prepare_routing_table(cold=True)" in source
