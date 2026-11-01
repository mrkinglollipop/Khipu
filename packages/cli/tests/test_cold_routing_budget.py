"""A cold one-shot prompt child gets a bigger routing-table build budget than
the warm helper: it folds the whole routing events log before it can build."""
from __future__ import annotations

import inspect
import time

import pytest

from khipu import agent_view, recall_prompt


@pytest.fixture
def slow_build(monkeypatch):
    for key in list(agent_view._TABLE):
        monkeypatch.delitem(agent_view._TABLE, key)

    def slow_signature(keys):
        time.sleep(0.35)
        return ("sig", "inventory-key")

    monkeypatch.setattr(agent_view, "_signature", slow_signature)
    monkeypatch.setattr(agent_view, "_inventory_signature", lambda keys, key: ("inventory",))
    monkeypatch.setattr(agent_view, "_rules", lambda keys: {"table": True})


def test_the_warm_budget_still_gives_up_on_a_slow_build(slow_build):
    agent_view.prepare_routing_table(refresh=True)
    assert agent_view._TABLE.get("helper_table") is None


def test_the_cold_budget_lets_a_slow_build_finish(slow_build):
    agent_view.prepare_routing_table(refresh=True, budget=agent_view.COLD_BUILD_BUDGET_S)
    assert agent_view._TABLE.get("helper_table") == {"table": True}


def test_the_cold_budget_stays_inside_the_hook_time_limit():
    assert 0.25 < agent_view.COLD_BUILD_BUDGET_S < recall_prompt.TIMEOUT_S


def test_the_one_shot_prompt_child_asks_for_the_cold_budget():
    source = inspect.getsource(recall_prompt._prompt_routing_child)
    assert "prepare_routing_table(budget=agent_view.COLD_BUILD_BUDGET_S)" in source
