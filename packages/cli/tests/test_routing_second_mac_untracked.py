"""A Mac that keeps no account list of its own, but has accounts the owner defined (from his other Mac),
must not call the shared priority's accounts "untracked". Only his own word (a stored list that omits an
account, an untracked note, a removed definition) does."""
from __future__ import annotations

import pytest

from khipu import routing
from tests.test_routing_acceptance import AUTHOR  # noqa: F401
from tests.test_routing_s9a_fix1 import catalog

pytest_plugins = ("tests.test_routing_acceptance",)


def _define_deepseek():
    routing.set_definition("provider", catalog()["deepseek"], via="app")
    routing.set_definition("account", {"id": "deepseek:main", "label": "DeepSeek", "provider": "deepseek"}, via="app")


def _priority(replay):
    routing.set_priority([{"account": "Codex"}, {"account": "Grok"}], via="app", expect=routing.priority_version())
    return {e["label"]: e["in_settings"] for e in replay.view()["priority"]["entries"]}


def test_defined_accounts_without_a_list_do_not_make_the_priority_untracked(replay):
    replay.config.pop("usage_accounts", None)
    replay.configure()
    _define_deepseek()
    assert _priority(replay) == {"Codex": True, "Grok": True}
    assert replay.view()["contradictions"] == []


def test_a_stored_list_that_omits_an_account_still_warns(replay):
    _define_deepseek()
    replay.configure(usage_accounts=[a for a in replay.config["usage_accounts"] if a["harness"] != "codex"])
    assert _priority(replay) == {"Codex": False, "Grok": True}
    assert [c["state"] for c in replay.view()["contradictions"]] == ["untracked"]
