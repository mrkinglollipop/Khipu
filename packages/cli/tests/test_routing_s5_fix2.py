"""Round-two regressions through MCP, owner writers and hook delivery."""
from __future__ import annotations

import json
import pytest

from khipu import mcp_server, recall_prompt, recall_rule, routing
from tests.test_routing_s5_fix1 import ACCOUNTS, Clock, ME, OTHER, T0


@pytest.fixture
def home(tmp_path, monkeypatch, khipu_app):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": ACCOUNTS}))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    clock = Clock(T0)
    monkeypatch.setattr(routing, "_now", clock)
    return {"data": data, "clock": clock}


@pytest.fixture(autouse=True)
def identity(monkeypatch):
    monkeypatch.setenv("KHIPU_HARNESS", "claude_code")
    monkeypatch.setenv("KHIPU_SESSION_ID", "fix2-author")


def read_rule():
    return mcp_server._tool_route({})["rules"][0]["owner_revision"]


def proposal(words, author=None):
    base = read_rule()
    return routing.request_rule(words, n=1, author=author or routing.conversation_author(), base=base)


@pytest.mark.parametrize("entry", ["mcp", "writer"])
@pytest.mark.parametrize("base_mode", ["omitted", "forged_current"])
def test_f1_rule_uses_delivered_revision_not_callers_base(home, entry, base_mode):
    routing.add_rule("OWNER E1", via="app")
    observed = read_rule()
    routing.edit_rule(1, "OWNER E2", via="app")
    current = routing.read_view()["rules"][0]["owner_revision"]
    kw = {} if base_mode == "omitted" else {"base": current}
    try:
        if entry == "mcp":
            mcp_server._tool_route_rule(dict(text="STALE E1", rule=1, owner_request="change", **kw))
        else:
            routing.request_rule("STALE E1", n=1, owner_request="change", **kw)
    except routing.RouteError:
        pass
    view = routing.read_view()
    assert view["rules"][0]["text"] == "OWNER E2"
    assert not view["rules"][0]["pending"]
    assert all(p["base"] == observed for p in view["proposals"].values())


@pytest.mark.parametrize("entry", ["mcp", "writer", "conversation"])
@pytest.mark.parametrize("base_mode", ["omitted", "forged_current"])
def test_f1_priority_uses_delivered_revision_not_callers_base(home, entry, base_mode):
    routing.set_priority([{"account": "Claude"}], via="app", expect=routing.priority_version())
    mcp_server._tool_route({})
    # Owner writes must not count as conversation reads.
    routing.set_priority([{"account": "Codex"}], via="app",
                         expect=routing.read_view()["priority_version"])
    view = routing.read_view()
    kw = {} if base_mode == "omitted" else {"base": view["priority"]["owner_revision"]}
    try:
        if entry == "mcp":
            mcp_server._tool_route_priority(dict(entries=[{"account": "Grok"}], owner_request="change", **kw))
        elif entry == "writer":
            routing.request_priority([{"account": "Grok"}], owner_request="change",
                                     expect=view["priority_version"], **kw)
        else:
            routing.conversation_priority([{"account": "Grok"}], owner_request="change",
                                          author=routing.conversation_author(), expect_current=True, **kw)
    except routing.RouteError:
        pass
    held = routing.read_view()["priority"]
    assert [e["label"] for e in held["entries"]] == ["Codex"]
    assert not held["pending"]


@pytest.mark.parametrize("entry", ["rule", "priority"])
def test_f1_no_conversation_read_cannot_guess_revision(home, entry):
    routing.add_rule("OWNER", via="app")
    routing.set_priority([{"account": "Claude"}], via="app", expect=routing.priority_version())
    view = routing.read_view()
    with pytest.raises(routing.RouteError):
        if entry == "rule":
            mcp_server._tool_route_rule({"text": "UNSEEN", "rule": 1, "owner_request": "change",
                                        "base": view["rules"][0]["owner_revision"]})
        else:
            routing.request_priority([{"account": "Grok"}], owner_request="change",
                                     expect=view["priority_version"], base=view["priority"]["owner_revision"])


@pytest.mark.parametrize("entry", ["writer", "conversation"])
def test_f2_base_never_disables_explicit_priority_expect(home, entry):
    routing.set_priority([{"account": "Claude"}], via="app", expect=routing.priority_version())
    stale = routing.priority_version()
    routing.set_priority([{"account": "Codex"}], via="app", expect=stale)
    base = routing.read_view()["priority"]["owner_revision"]
    with pytest.raises(routing.RouteError, match="changed after it was read"):
        if entry == "writer":
            routing.request_priority([{"account": "Grok"}], owner_request="stale", expect=stale, base=base)
        else:
            routing.conversation_priority([{"account": "Grok"}], owner_request="stale", expect=stale,
                                          base=base, author=routing.conversation_author())


def test_f3_retire_restore_ends_overlay_without_rewriting_base(home):
    routing.add_rule("OWNER E1", via="app")
    original = read_rule()
    proposal("OLD PROPOSAL")
    routing.retire_rule(1, via="app")
    retired = next(iter(routing.read_view()["proposals"].values()))
    assert retired["base"] == original
    assert retired["status"] in ("crossed", "overtaken")
    routing.restore_rule(1, via="app")
    view = routing.read_view()
    assert view["rules"][0]["text"] == "OWNER E1"
    assert not view["rules"][0]["pending"]
    assert next(iter(view["proposals"].values()))["base"] == original


def test_f4_dismiss_crossing_expiry_keeps_owner_rule(home, monkeypatch):
    routing.add_rule("OWNER E1", via="app")
    proposal("EXPIRING")
    home["clock"].advance(days=7, seconds=-1)
    append = routing._owner_write
    def cross_expiry(*args, **kwargs):
        home["clock"].advance(seconds=2)
        return append(*args, **kwargs)
    monkeypatch.setattr(routing, "_owner_write", cross_expiry)
    routing.dismiss_rule_request(1, via="app")
    view = routing.read_view()
    assert len(view["rules"]) == 1
    assert view["rules"][0]["text"] == "OWNER E1"
    assert not view["rules"][0]["pending"]
    assert next(iter(view["proposals"].values()))["status"] == "expired"


def test_f5_replacement_notices_fit_summary_budget(home):
    routing.add_rule("OWNER", via="app")
    config = home["data"] / "config.json"
    cfg = json.loads(config.read_text())
    cfg["routing_summary_chars"] = 1500
    config.write_text(json.dumps(cfg))
    base = read_rule()
    for i in range(160):
        routing.request_rule(f"PROPOSAL {i}", n=1, base=base)
    out = routing.summary()
    # Owner words and heading are exempt; notices are not.
    non_owner = [s for s in out["text"].splitlines() if "proposal by" in s]
    assert sum(len(s) + 1 for s in non_owner) < 1500
    assert out["dropped"] > 0
    counted = sum(len(line) + 1 for line in out["text"].splitlines()[1:]) - out["rules_note_chars"]
    assert counted <= 1500
    assert recall_rule.start_context_fits(out["text"])


def test_f5_hook_acknowledges_only_notices_actually_delivered(home):
    routing.add_rule("OWNER", via="app")
    proposal("FIRST", routing.conversation_author())
    proposal("SECOND", OTHER)
    from khipu import agent_view

    agent_view.prepare_routing_table(refresh=True)
    out = routing.prompt_push(None, budget=2, session_id="fix2-author")
    assert "was replaced by a newer proposal" in out["text"]
    # Delivery recording uses the same public hook boundary as recall.
    info = {"status": "full", "pending": dict(session="fix2-author", version=out["version"],
             epoch=out["epoch"], full=True, at=routing._iso(home["clock"].now),
             notices=out.get("notices"), routing_changes=out.get("routing_changes"))}
    recall_prompt._routing_delivered(info, False)
    assert "was replaced by a newer proposal" in routing.summary()["text"]
    recall_prompt._routing_delivered(info, True)
    assert "was replaced by a newer proposal" not in routing.summary()["text"]
    home["clock"].advance(days=8)
    assert "was replaced by a newer proposal" not in routing.summary()["text"]


def test_f1_a_different_session_cannot_use_another_conversations_read(home, monkeypatch):
    routing.add_rule("OWNER", via="app")
    observed = read_rule()
    monkeypatch.setenv("KHIPU_SESSION_ID", "unread-second-session")
    with pytest.raises(routing.RouteError, match="not shown"):
        mcp_server._tool_route_rule({"text": "UNSEEN", "rule": 1, "owner_request": "change", "base": observed})
    assert routing.read_view()["rules"][0]["text"] == "OWNER"


def test_f5_unacknowledged_notices_age_out(home):
    routing.add_rule("OWNER", via="app")
    proposal("FIRST")
    proposal("SECOND", OTHER)
    assert "was replaced by a newer proposal" in routing.summary()["text"]
    home["clock"].advance(days=8)
    assert "was replaced by a newer proposal" not in routing.summary()["text"]


def test_f1_cli_read_provides_a_receipt_for_its_priority_request(home, capsys):
    from khipu import cli

    routing.set_priority([{"account": "Claude"}], via="app", expect=routing.priority_version())
    assert cli.main(["route", "priority", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    out = routing.request_priority([{"account": "Codex"}], owner_request="change",
                                   expect=routing.priority_version(), base=shown["owner_revision"])
    assert out["ok"]
    assert routing.read_view()["priority"]["pending"]


def test_f1_empty_priority_read_does_not_observe_a_later_owner_write(home, monkeypatch):
    routing.set_priority([{"account": "Claude"}], via="app", expect=routing.priority_version())
    cleared = routing.clear_priority(via="app", expect=routing.priority_version())
    record = routing._record_read
    moved = False

    def move_after_response_is_built(view, **kwargs):
        nonlocal moved
        if not moved:
            moved = True
            routing.set_priority([{"account": "Codex"}], via="app", expect=routing.priority_version())
        return record(view, **kwargs)

    monkeypatch.setattr(routing, "_record_read", move_after_response_is_built)
    shown = mcp_server._tool_route({})
    assert shown["priority"] is None
    mcp_server._tool_route_priority({"entries": [{"account": "Grok"}], "owner_request": "prepared on empty",
                                     "base": shown["priority_owner_revision"]})
    view = routing.read_view()
    assert [e["label"] for e in view["priority"]["entries"]] == ["Codex"]
    crossed, = view["proposals"].values()
    assert crossed["status"] == "crossed"
    assert crossed["base"] == cleared["revision"]
