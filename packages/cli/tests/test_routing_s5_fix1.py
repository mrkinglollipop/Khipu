"""Routing slice 5, fix round 1: the five Codex findings, each proved
through a real entry point (the MCP tool handlers, the owner writers and
the conversation read), never by injecting events or fields.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from khipu import mcp_server, routing

T0 = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
ACCOUNTS = [{"label": "Claude", "harness": "claude"},
            {"label": "Claude second", "harness": "claude", "home": "/logins/claude-second"},
            {"label": "Codex", "harness": "codex"},
            {"label": "Grok", "harness": "grok"}]
ME = {"kind": "conversation", "harness": "claude_code", "project": "project-a", "session": "s-one"}
OTHER = {"kind": "conversation", "harness": "codex", "project": "project-b", "session": "s-two"}


class Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> datetime:
        self.now = self.now + timedelta(**kw)
        return self.now


@pytest.fixture
def home(tmp_path, monkeypatch, khipu_app):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": ACCOUNTS}))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    clock = Clock(T0)
    monkeypatch.setattr(routing, "_now", clock)
    return {"data": data, "clock": clock, "tmp": tmp_path, "callers": khipu_app}


# ---- finding 1: a queued MCP request carries its observed owner revision ----


def test_f1_queued_mcp_rule_request_prepared_against_an_old_revision_is_crossed(home):
    routing.add_rule("Owner original.", via="app")
    observed = routing.conversation_view()["rules"][0]["owner_revision"]
    routing.edit_rule(1, "Owner newer.", via="app")
    out = mcp_server._tool_route_rule({"text": "Stale proposal.", "rule": 1,
                                       "owner_request": "change the rule", "base": observed})
    assert out["ok"]
    view = routing.read_view()
    assert view["rules"][0]["text"] == "Owner newer."
    assert view["rules"][0]["pending"] is False
    crossed = [p for p in view["proposals"].values() if p["status"] == "crossed"]
    assert len(crossed) == 1 and crossed[0]["base"] == observed


def test_f1_queued_mcp_priority_request_prepared_against_an_old_revision_is_crossed(home):
    routing.set_priority([{"account": "Claude", "role": "owner first"}], via="app",
                         expect=routing.priority_version())
    observed = routing.conversation_view()["priority"]["owner_revision"]
    routing.set_priority([{"account": "Codex", "role": "owner newer"}], via="app",
                         expect=routing.priority_version())
    out = mcp_server._tool_route_priority({"entries": [{"account": "Grok", "role": "stale"}],
                                           "owner_request": "use Grok first", "base": observed})
    assert out["ok"]
    view = routing.read_view()
    assert [e["label"] for e in view["priority"]["entries"]] == ["Codex"]
    assert view["priority"]["pending"] is False
    crossed = [p for p in view["proposals"].values() if p["status"] == "crossed"]
    assert len(crossed) == 1 and crossed[0]["base"] == observed


# ---- finding 2: Confirm never resurrects an expired proposal -----------------


def test_f2_confirm_after_expiry_keeps_the_owner_words(home):
    routing.add_rule("Owner original.", via="app")
    routing.conversation_view()
    routing.request_rule("Proposal one.", n=1, author=ME)
    home["clock"].advance(days=8)
    view = routing.read_view()
    assert (view["rules"][0]["text"], view["rules"][0]["pending"]) == ("Owner original.", False)
    with pytest.raises(routing.RouteError):
        routing.confirm_rule(1, via="app")
    rule = routing.read_view()["rules"][0]
    assert rule["text"] == "Owner original." and rule["text"] != "Proposal one."


def test_f2_a_write_after_expiry_never_sees_the_expired_overlay(home):
    routing.add_rule("Owner original.", via="app")
    routing.conversation_view()
    routing.request_rule("Proposal one.", n=1, author=ME)
    home["clock"].advance(days=8)
    # A fresh request after expiry is built on the owner's words, not on
    # the expired proposal's.
    routing.conversation_view()
    routing.request_rule("Proposal two.", n=1, author=OTHER)
    rule = routing.read_view()["rules"][0]
    assert (rule["text"], rule["prior_text"]) == ("Proposal two.", "Owner original.")


# ---- finding 3: Retire/Restore ends an old-base overlay ---------------------


def test_f3_retire_and_restore_end_the_live_proposal_without_rebasing(home):
    routing.add_rule("Owner original.", via="app")
    routing.conversation_view()
    original = routing.read_view()["rules"][0]["owner_revision"]
    routing.request_rule("Proposal one.", n=1, author=ME)
    routing.retire_rule(1, via="app")
    routing.restore_rule(1, via="app")
    view = routing.read_view()
    rule = view["rules"][0]
    live = [p for p in view["proposals"].values() if p["status"] == "live"]
    assert live == []
    assert rule["text"] == "Owner original."
    ended = list(view["proposals"].values())
    assert ended[0]["base"] == original
    assert ended[0]["status"] == "overtaken"


# ---- finding 4: the replaced author is actually told ---------------------------


def test_f4_replacement_is_delivered_in_the_conversation_response_and_summary(home):
    routing.add_rule("Owner original.", via="app")
    routing.conversation_view()
    routing.request_rule("Mine.", n=1, author=ME)
    routing.conversation_view()
    routing.request_rule("Theirs.", n=1, author=OTHER)
    answer = routing.conversation_view()
    told = [p for p in answer.get("proposals") or [] if p["status"] == "replaced"]
    assert told and "Claude Code" in told[0]["notify"]
    assert "was replaced by a newer proposal" in routing.summary()["text"]
    assert "Claude Code" in routing.summary()["text"]


# ---- finding 5: Dismiss works for a changed-rule proposal -----------------------


def test_f5_dismiss_of_a_changed_rule_proposal_restores_the_owner_words(home):
    routing.add_rule("Owner original.", via="app")
    routing.conversation_view()
    routing.request_rule("Changed words.", n=1, author=ME)
    routing.dismiss_rule_request(1, via="app")
    view = routing.read_view()
    rule = view["rules"][0]
    assert (rule["text"], rule["pending"]) == ("Owner original.", False)
    dismissed = [p for p in view["proposals"].values() if p["status"] == "dismissed"]
    assert len(dismissed) == 1
