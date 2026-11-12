"""An owner's change to one account's priority entry and the pauses and
holds on other accounts (2026-11-12): changing Claude's words was refused
with "say end or keep in holds for: <id>", where the id was the owner's
pause on Grok. A pause is an account's state, not a row of the holds list,
so the owner found the id nowhere, and the held request he confirmed failed
the same way with only a generic error.

Now an amend keeps every other account's pause or hold as it is; one the
amendment names, or one on the amended account, still needs an answer, and
the refusal says what each id is. Hermetic: the routing acceptance fixtures."""
from __future__ import annotations

import pytest

from khipu import owner_direction, routing, routing_say
from tests.test_routing_acceptance import AUTHOR  # noqa: F401  (fixture module import)

pytest_plugins = ("tests.test_routing_acceptance",)


def _priority():
    routing.set_priority([{"account": "Claude", "role": "main work"}, {"account": "Grok", "role": "reviews"},
                          {"account": "Codex", "role": "builds"}], via="app", expect=routing.priority_version())


def _pause_id(label: str) -> str:
    entry = next(a for a in routing.read_view()["accounts"] if a["label"] == label)
    assert entry["state"] == routing.PAUSED
    return entry["witness"]["id"]


def _roles() -> dict:
    return {e["label"]: e["role"] for e in routing.read_view()["priority"]["entries"]}


def test_amending_one_entry_keeps_a_pause_on_another_account(replay):
    _priority()
    routing.pause("Grok", via="app")
    out = routing.amend_priority("Claude", append="and the docs", via="app", expect=routing.priority_version())
    assert out["ok"] is True, out
    assert _roles()["Claude"] == "main work and the docs"
    grok = next(a for a in routing.read_view()["accounts"] if a["label"] == "Grok")
    assert grok["state"] == routing.PAUSED  # kept, not lifted


def test_an_amend_naming_a_paused_account_still_asks_and_names_the_pause(replay):
    _priority()
    routing.pause("Grok", via="app")
    pause = _pause_id("Grok")
    with pytest.raises(routing.HoldsNeeded) as err:
        routing.amend_priority("Claude", append="Grok reviews first", via="app", expect=routing.priority_version())
    assert err.value.ids == [pause] and err.value.code == "holds_needed"
    text = str(err.value)
    assert text.startswith(f"say end or keep in holds for: {pause}"), text
    assert "the pause on Grok" in text and "no end set" in text, text
    out = routing.amend_priority("Claude", append="Grok reviews first", via="app",
                                 expect=routing.priority_version(), holds={pause: "keep"})
    assert out["ok"] is True, out


def test_a_pause_on_the_amended_account_still_needs_an_answer(replay):
    _priority()
    routing.pause("Claude", via="app")
    with pytest.raises(routing.HoldsNeeded, match="the pause on Claude"):
        routing.amend_priority("Claude", append="and the docs", via="app", expect=routing.priority_version())


def test_a_hold_on_another_account_is_kept_and_a_hold_on_all_still_asks(replay):
    _priority()
    routing.add_hold("wait for the review", scope="account", account="Codex", reason="keep", owner=True, via="app")
    out = routing.amend_priority("Claude", append="and the docs", via="app", expect=routing.priority_version())
    assert out["ok"] is True, out
    assert len(routing.read_view()["holds"]) == 1
    routing.add_hold("everything waits", scope="all", reason="keep", owner=True, via="app")
    with pytest.raises(routing.HoldsNeeded, match="a hold on every account: everything waits"):
        routing.amend_priority("Claude", append="and more", via="app", expect=routing.priority_version())


def test_a_whole_priority_set_still_asks_for_every_account_it_holds(replay):
    _priority()
    routing.pause("Grok", via="app")
    with pytest.raises(routing.HoldsNeeded, match="the pause on Grok"):
        routing.set_priority([{"account": "Claude", "role": "x"}, {"account": "Grok", "role": "reviews"}],
                             via="app", expect=routing.priority_version())


def test_spoken_changes_still_read_the_ids_to_keep(replay):
    _priority()
    routing.pause("Grok", via="app")
    kept = routing_say._keep_holds({"type": "priority_set", "entries": [{"account": "grok|", "role": "r"}]},
                                   routing._now())
    assert kept == {_pause_id("Grok"): "keep"}


def test_a_failed_held_change_says_why_when_a_pause_was_in_the_way(replay, monkeypatch):
    request = {"id": "req-test", "state": "applying", "action": "route.priority.amend", "args": {}}
    path = owner_direction.requests_path()
    with owner_direction._requests_locked():
        owner_direction._save(path, {"requests": [request], "erased": 0}, 0.0)
    owner_direction.settle("req-test", False, {"ok": False, "code": "holds_needed"})
    (row,) = owner_direction._read_requests(path)
    assert row["state"] == "failed" and row["error_code"] == "holds_needed"
    assert "pause or a hold" in row["error"]
