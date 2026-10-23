"""Round-three regressions, adapted from the independent review probes."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import timedelta

import pytest

from khipu import agent_view, mcp_server, recall_prompt, routing
from tests.test_routing_s5_fix1 import ACCOUNTS, Clock, OTHER, T0


@pytest.fixture
def home(tmp_path, monkeypatch, khipu_app):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": ACCOUNTS}))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("KHIPU_HARNESS", "claude_code")
    monkeypatch.setenv("KHIPU_SESSION_ID", "probe-author")
    clock = Clock(T0)
    monkeypatch.setattr(routing, "_now", clock)
    return {"data": data, "clock": clock, "tmp": tmp_path}


def offer_prompt(*args, **kwargs):
    # The real hook inherits its helper's complete login inventory.
    agent_view.prepare_routing_table(refresh=True)
    return routing.prompt_push(*args, **kwargs)


def delivered(session, out, clock):
    info = {"status": "full", "pending": dict(session=session, version=out["version"], epoch=out["epoch"],
            full=out["kind"] == "full", at=routing._iso(clock.now), notices=out.get("notices"),
            owed=out.get("owed_entities"), shown=out.get("shown_entities"), routing_changes=out.get("routing_changes"))}
    recall_prompt._routing_delivered(info, True)


# --- 1. omitted base binds to the identity's LATEST read, not the read the
#        request was prepared from (parallel tool calls / subagents share it)
def test_p1_omitted_base_after_interleaved_read_never_makes_stale_text_live(home):
    routing.add_rule("OWNER E1", via="app")
    shown = mcp_server._tool_route({})                     # request prepared from this read (e1)
    routing.edit_rule(1, "OWNER E2", via="app")    # Matt writes e2
    shown = mcp_server._tool_route({})                     # sibling read in the same session (subagent / parallel call)
    with pytest.raises(routing.RouteError, match="base"):
        mcp_server._tool_route_rule({"text": "STALE FROM E1", "rule": 1, "owner_request": "change"})
    rule = routing.read_view()["rules"][0]
    assert rule["text"] == "OWNER E2", f"stale text live over unseen e2: {rule['text']!r}"


def test_p1b_priority_omitted_base_after_interleaved_read(home):
    routing.set_priority([{"account": "Claude"}], via="app", expect=routing.priority_version())
    shown = mcp_server._tool_route({})
    routing.set_priority([{"account": "Codex"}], via="app", expect=routing.priority_version())
    shown = mcp_server._tool_route({})
    with pytest.raises(mcp_server._ToolFailure, match="base is required") as refusal:
        mcp_server._tool_route_priority({"entries": [{"account": "Grok"}], "owner_request": "prepared on e1"})
    assert refusal.value.payload["code"] == "bad_args"
    held = routing.read_view()["priority"]
    assert [e["label"] for e in held["entries"]] == ["Codex"], held


# --- 2. notices: an unacked notice for ANOTHER author forces every session's
#        every prompt to a full push
def test_p2_other_authors_notice_does_not_force_full_push_forever(home, monkeypatch):
    routing.add_rule("OWNER", via="app")
    monkeypatch.setenv("KHIPU_SESSION_ID", "author-a")
    shown = mcp_server._tool_route({})
    mcp_server._tool_route_rule({"text": "A", "rule": 1, "owner_request": "x", "base": shown["rules"][0]["owner_revision"]})
    monkeypatch.setenv("KHIPU_SESSION_ID", "author-b")
    shown = mcp_server._tool_route({})
    mcp_server._tool_route_rule({"text": "B", "rule": 1, "owner_request": "x", "base": shown["rules"][0]["owner_revision"]})   # replaces A
    bystander = "bystander"
    kinds = []
    for _ in range(4):
        out = offer_prompt(recall_prompt.routing_marker(bystander), budget=2, session_id=bystander)
        kinds.append((out["kind"], out.get("why")))
        delivered(bystander, out, home["clock"])
        home["clock"].advance(minutes=1)
    assert [k for k, _ in kinds[1:]] == ["none"] * 3, kinds


def test_p2b_sessionless_author_notice_never_acked(home, monkeypatch):
    monkeypatch.delenv("KHIPU_SESSION_ID")
    mcp_server._tool_route_rule({"text": "A", "owner_request": "x"})
    monkeypatch.setenv("KHIPU_SESSION_ID", "author-b")
    shown = mcp_server._tool_route({})
    mcp_server._tool_route_rule({"text": "B", "rule": 1, "owner_request": "x", "base": shown["rules"][0]["owner_revision"]})
    home["clock"].advance(days=6)
    out = offer_prompt(recall_prompt.routing_marker("someone"), budget=2, session_id="someone")
    delivered("someone", out, home["clock"])
    out = offer_prompt(recall_prompt.routing_marker("someone"), budget=2, session_id="someone")
    assert out["kind"] != "full", (out["kind"], out.get("why"))


def test_p2c_notice_reaches_only_its_author(home, monkeypatch):
    routing.add_rule("OWNER", via="app")
    monkeypatch.setenv("KHIPU_SESSION_ID", "author-a")
    shown = mcp_server._tool_route({})
    mcp_server._tool_route_rule({"text": "A", "rule": 1, "owner_request": "x", "base": shown["rules"][0]["owner_revision"]})
    monkeypatch.setenv("KHIPU_SESSION_ID", "author-b")
    shown = mcp_server._tool_route({})
    mcp_server._tool_route_rule({"text": "B", "rule": 1, "owner_request": "x", "base": shown["rules"][0]["owner_revision"]})
    out = offer_prompt(recall_prompt.routing_marker("bystander"), budget=2, session_id="bystander")
    assert "was replaced" not in out["text"], out["text"]


def test_p2d_notice_names_which_proposal(home, monkeypatch):
    routing.add_rule("OWNER", via="app")
    monkeypatch.setenv("KHIPU_SESSION_ID", "author-a")
    shown = mcp_server._tool_route({})
    mcp_server._tool_route_rule({"text": "A", "rule": 1, "owner_request": "x", "base": shown["rules"][0]["owner_revision"]})
    monkeypatch.setenv("KHIPU_SESSION_ID", "author-b")
    shown = mcp_server._tool_route({})
    mcp_server._tool_route_rule({"text": "B", "rule": 1, "owner_request": "x", "base": shown["rules"][0]["owner_revision"]})
    line = next(l for l in routing.summary(session_id="author-a")["text"].splitlines() if "was replaced" in l)
    assert "rule 1" in line or "standing rule 1" in line, line


# --- 3. read receipts: one file per session-less process (the app polls
#        `khipu route --json` every minute)
def test_p3_cli_route_json_does_not_leave_a_receipt_file_per_process(home):
    routing.add_rule("OWNER", via="app")
    import os
    env = dict(os.environ)
    for k in [k for k in env if k.startswith(("CLAUDE", "CODEX", "KHIPU_SESSION", "KHIPU_HARNESS", "GROK"))]:
        env.pop(k)
    for _ in range(3):
        subprocess.run([sys.executable, "-m", "khipu", "route", "--json"], env=env, check=True,
                       capture_output=True, cwd=str(home["tmp"]))
    reads = list((routing.routing_dir() / "reads").glob("*.json"))
    print("RECEIPT FILES", len(reads))
    assert len(reads) == 0, f"{len(reads)} receipt files after 3 app polls"


# --- 4. Undo/Confirm/expiry/Dismiss never lose or resurrect words
def test_p4_replaced_then_owner_undo_restores_owner_words_and_confirm_refused(home, monkeypatch):
    routing.add_rule("OWNER", via="app")
    monkeypatch.setenv("KHIPU_SESSION_ID", "a"); shown = mcp_server._tool_route({})
    mcp_server._tool_route_rule({"text": "A", "rule": 1, "owner_request": "x", "base": shown["rules"][0]["owner_revision"]})
    monkeypatch.setenv("KHIPU_SESSION_ID", "b"); shown = mcp_server._tool_route({})
    mcp_server._tool_route_rule({"text": "B", "rule": 1, "owner_request": "x", "base": shown["rules"][0]["owner_revision"]})
    routing.undo_rule_request(1, via="app")
    v = routing.read_view()
    assert v["rules"][0]["text"] == "OWNER" and not v["rules"][0]["pending"]
    with pytest.raises(routing.RouteError):
        routing.confirm_rule(1, via="app")
    assert routing.read_view()["rules"][0]["text"] == "OWNER"
    statuses = sorted(p["status"] for p in v["proposals"].values())
    assert statuses == ["replaced", "undone"], statuses


def test_p4b_expired_then_new_proposal_then_dismiss_keeps_owner(home):
    routing.add_rule("OWNER", via="app")
    shown = mcp_server._tool_route({})
    mcp_server._tool_route_rule({"text": "OLD", "rule": 1, "owner_request": "x", "base": shown["rules"][0]["owner_revision"]})
    home["clock"].advance(days=8)
    assert routing.read_view()["rules"][0]["text"] == "OWNER"
    shown = mcp_server._tool_route({})
    mcp_server._tool_route_rule({"text": "NEW", "rule": 1, "owner_request": "x", "base": shown["rules"][0]["owner_revision"]})
    routing.dismiss_rule_request(1, via="app")
    v = routing.read_view()
    assert v["rules"][0]["text"] == "OWNER" and not v["rules"][0]["pending"]
    assert "OLD" not in json.dumps(routing.summary()["text"])


def test_p4c_priority_expiry_then_undo_does_not_resurrect(home):
    routing.set_priority([{"account": "Claude", "role": "OWNER ROLE"}], via="app", expect=routing.priority_version())
    shown = mcp_server._tool_route({})
    mcp_server._tool_route_priority({"entries": [{"account": "Grok", "role": "P1"}], "owner_request": "x", "base": shown["priority_owner_revision"]})
    shown = mcp_server._tool_route({})
    mcp_server._tool_route_priority({"entries": [{"account": "Codex", "role": "P2"}], "owner_request": "x", "base": shown["priority_owner_revision"]})
    home["clock"].advance(days=8)
    held = routing.read_view()["priority"]
    assert [e["label"] for e in held["entries"]] == ["Claude"] and not held["pending"], held
    with pytest.raises(routing.RouteError):
        routing.undo_priority(via="app", expect=routing.priority_version())
    assert [e["label"] for e in routing.read_view()["priority"]["entries"]] == ["Claude"]


# --- 5. replay: an exact replay of a request after the owner moved
def test_p5_replayed_request_with_base_is_crossed(home):
    routing.add_rule("OWNER E1", via="app")
    base = mcp_server._tool_route({})["rules"][0]["owner_revision"]
    req = {"text": "P", "rule": 1, "owner_request": "x", "base": base}
    mcp_server._tool_route_rule(req)
    routing.confirm_rule(1, via="app", expect=next(r["revision"] for r in routing.read_view()["rules"] if r["n"] == 1))
    shown = mcp_server._tool_route({})  # reads again; the replay still names e1
    mcp_server._tool_route_rule(dict(req, text="REPLAY"))
    v = routing.read_view()
    assert v["rules"][0]["text"] == "P" and not v["rules"][0]["pending"], v["rules"][0]


# --- 6. owner stops tracking an account: priority_drop advances the owner
#        revision; the live overlay must not keep standing on the older base
def test_p6_account_stop_ends_or_rebases_live_priority_overlay(home):
    from khipu import accounts
    routing.set_priority([{"account": "Claude"}, {"account": "Codex"}], via="app", expect=routing.priority_version())
    shown = mcp_server._tool_route({})
    mcp_server._tool_route_priority({"entries": [{"account": "Grok"}, {"account": "Codex"}], "owner_request": "x", "base": shown["priority_owner_revision"]})
    before = routing.read_view()
    live, = [p for p in before["proposals"].values() if p["status"] == "live"]
    accounts.stop("codex|", expect=accounts.status()["version"])
    v = routing.read_view()
    still = [p for p in v["proposals"].values() if p["status"] == "live"]
    print("OWNER REV", v["priority_owner_revision"], "LIVE", [(p["id"], p["base"]) for p in still],
          "PRIORITY", [e["label"] for e in (v["priority"] or {}).get("entries", [])], (v["priority"] or {}).get("pending"))
    assert all(p["base"] == v["priority_owner_revision"] for p in still)


@pytest.mark.parametrize("kind", ["rule", "priority", "conversation_priority"])
@pytest.mark.parametrize("base", [None, "", " "])
def test_writers_refuse_missing_base_even_with_a_newer_receipt(home, kind, base):
    routing.add_rule("OWNER E1", via="app")
    routing.set_priority([{"account": "Claude"}], via="app", expect=routing.priority_version())
    mcp_server._tool_route({})
    routing.edit_rule(1, "OWNER E2", via="app")
    routing.set_priority([{"account": "Codex"}], via="app", expect=routing.priority_version())
    mcp_server._tool_route({})
    before = routing.events_file().read_bytes()
    with pytest.raises(routing.RouteError, match="base"):
        if kind == "rule":
            routing.request_rule("STALE", n=1, base=base)
        elif kind == "priority":
            routing.request_priority([{"account": "Grok"}], owner_request="stale", base=base,
                                     expect=routing.priority_version())
        else:
            routing.conversation_priority([{"account": "Grok"}], owner_request="stale", base=base,
                                          expect_current=True, author=routing.conversation_author())
    assert routing.events_file().read_bytes() == before


def test_mcp_schemas_require_explicit_base_for_edits_and_priority():
    tools = {t["name"]: t["inputSchema"] for t in mcp_server.TOOLS}
    assert "base" in tools["khipu_route_priority"]["required"]
    assert {"if": {"required": ["rule"]}, "then": {"required": ["base"]}} in tools["khipu_route_rule"]["allOf"]
    for name in ("khipu_route_rule", "khipu_route_priority"):
        assert tools[name]["properties"]["base"]["type"] == "string"
        assert tools[name]["properties"]["base"]["minLength"] == 1


@pytest.mark.parametrize("kind", ["rule", "priority"])
def test_explicit_old_base_stays_crossed_after_a_sibling_read(home, kind):
    routing.add_rule("OWNER E1", via="app")
    routing.set_priority([{"account": "Claude"}], via="app", expect=routing.priority_version())
    first = mcp_server._tool_route({})
    routing.edit_rule(1, "OWNER E2", via="app")
    routing.set_priority([{"account": "Codex"}], via="app", expect=routing.priority_version())
    mcp_server._tool_route({})
    if kind == "rule":
        base = first["rules"][0]["owner_revision"]
        mcp_server._tool_route_rule(dict(text="STALE", rule=1, owner_request="x", base=base))
    else:
        base = first["priority_owner_revision"]
        mcp_server._tool_route_priority(dict(entries=[{"account": "Grok"}], owner_request="x", base=base))
    view = routing.read_view()
    assert view["rules"][0]["text"] == "OWNER E2"
    assert [e["label"] for e in view["priority"]["entries"]] == ["Codex"]
    proposal, = view["proposals"].values()
    assert proposal["status"] == "crossed" and proposal["base"] == base


@pytest.mark.parametrize("kind", ["rule", "priority"])
def test_only_authors_delivery_acknowledges_its_named_notice(home, monkeypatch, kind):
    routing.add_rule("OWNER", via="app")
    for session in ("author-a", "author-b"):
        monkeypatch.setenv("KHIPU_SESSION_ID", session)
        shown = mcp_server._tool_route({})
        if kind == "rule":
            mcp_server._tool_route_rule(dict(text=session, rule=1, owner_request="x",
                                            base=shown["rules"][0]["owner_revision"]))
        else:
            mcp_server._tool_route_priority(dict(entries=[{"account": "Codex", "role": session}], owner_request="x",
                                                base=shown["priority_owner_revision"]))
    bystander = offer_prompt(None, budget=2, session_id="bystander")
    assert not bystander["notices"] and "was replaced" not in bystander["text"]
    delivered("bystander", bystander, home["clock"])
    assert not recall_prompt.routing_marker("author-a").get("notices")
    # The hook's receiver is explicit; its helper process may have another
    # session in its environment. Author A still receives A's own notice.
    notice = offer_prompt(None, budget=2, session_id="author-a")
    assert f"{kind if kind == 'priority' else 'rule 1'} proposal" in notice["text"]
    assert len(notice["notices"]) == 1
    info = {"status": "full", "pending": dict(session="author-a", version=notice["version"],
             epoch=notice["epoch"], full=True, at=routing._iso(home["clock"].now), notices=notice["notices"],
             owed=notice.get("owed_entities"), shown=notice.get("shown_entities"), routing_changes=notice.get("routing_changes"))}
    recall_prompt._routing_delivered(info, False)
    assert routing.summary(session_id="author-a")["notices"] == notice["notices"]
    recall_prompt._routing_delivered(info, True)
    assert set(recall_prompt.routing_marker("author-a")["notices"]) == set(notice["notices"])
    again = offer_prompt(recall_prompt.routing_marker("author-a"), budget=2, session_id="author-a")
    assert again["kind"] == "none" and again["text"] is None


def test_sessionless_poll_never_takes_the_receipt_lock(home, monkeypatch, capsys):
    from khipu import cli

    routing.add_rule("OWNER", via="app")
    view = routing.read_view()
    monkeypatch.delenv("KHIPU_SESSION_ID")
    def forbidden_lock():
        pytest.fail("sessionless poll took the routing lock for a receipt")
    monkeypatch.setattr(routing, "_lock", forbidden_lock)
    routing._record_read(view, rules=True, priority=True)
    assert cli.main(["route", "--json"]) == 0
    assert not (routing.routing_dir() / "reads").exists()
    assert json.loads(capsys.readouterr().out)["rules"][0]["text"] == "OWNER"


def test_sessionless_reads_cannot_supply_a_base_for_a_later_session(home, monkeypatch):
    routing.add_rule("OWNER", via="app")
    monkeypatch.delenv("KHIPU_SESSION_ID")
    shown = mcp_server._tool_route({})
    assert not (routing.routing_dir() / "reads").exists()
    monkeypatch.setenv("KHIPU_SESSION_ID", "new-session")
    with pytest.raises(routing.RouteError, match="not shown"):
        routing.request_rule("UNSEEN", n=1, base=shown["rules"][0]["owner_revision"])


def test_mcp_read_only_delivers_its_own_replacement_notice(home, monkeypatch):
    routing.add_rule("OWNER", via="app")
    for session in ("author-a", "author-b"):
        monkeypatch.setenv("KHIPU_SESSION_ID", session)
        shown = mcp_server._tool_route({})
        mcp_server._tool_route_rule(dict(text=session, rule=1, owner_request="x",
                                        base=shown["rules"][0]["owner_revision"]))
    monkeypatch.setenv("KHIPU_SESSION_ID", "bystander")
    assert all(p["status"] != "replaced" for p in mcp_server._tool_route({})["proposals"])
    assert not recall_prompt.routing_marker("author-a").get("notices")
    monkeypatch.setenv("KHIPU_SESSION_ID", "author-a")
    replaced, = [p for p in mcp_server._tool_route({})["proposals"] if p["status"] == "replaced"]
    assert replaced["rule"] == 1
    assert replaced["id"] in recall_prompt.routing_marker("author-a")["notices"]


@pytest.mark.parametrize("receiver", ["author-a", "bystander", ""])
def test_session_start_delivers_notices_only_to_the_payload_session(home, monkeypatch, capsys, receiver):
    from khipu import recall_rule

    routing.add_rule("OWNER", via="app")
    for session in ("author-a", "author-b"):
        monkeypatch.setenv("KHIPU_SESSION_ID", session)
        shown = mcp_server._tool_route({})
        mcp_server._tool_route_rule(dict(text=session, rule=1, owner_request="x",
                                        base=shown["rules"][0]["owner_revision"]))
    monkeypatch.setenv("KHIPU_SESSION_ID", "author-a")
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "1")
    monkeypatch.setattr(routing, "kick_plan_refresh", lambda: False)
    monkeypatch.setattr(recall_rule, "_session_start_payload", lambda: {"session_id": receiver})
    monkeypatch.setattr(recall_rule, "session_start_context", lambda _: "MEMORY")
    monkeypatch.setattr(recall_rule, "is_helper_session", lambda _: False)
    recall_rule.session_start_main(shape="claude")
    text = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert ("rule 1 proposal" in text) == (receiver == "author-a")
    assert bool(recall_prompt.routing_marker("author-a").get("notices")) == (receiver == "author-a")


def test_saved_pre_fix_proposal_view_is_refolded_without_a_log_change(home):
    from khipu import accounts

    routing.set_priority([{"account": "Claude"}, {"account": "Codex"}], via="app", expect=routing.priority_version())
    shown = mcp_server._tool_route({})
    routing.request_priority([{"account": "Grok"}, {"account": "Codex"}], owner_request="x",
                             base=shown["priority_owner_revision"])
    before = routing.read_view()
    accounts.stop("codex|", expect=accounts.status()["version"])
    after = routing.read_view()
    # Simulate a trusted cache written by the older fold over these events.
    old = dict(after, priority=before["priority"], proposals=before["proposals"], effective=before["effective"])
    old.pop("proposal_schema")
    for proposal in old["proposals"].values():
        proposal.pop("n", None)
    routing._save_view(old)
    log = routing.events_file().read_bytes()
    refreshed = routing.read_view()
    assert refreshed["proposal_schema"] == 1
    assert not refreshed["priority"]["pending"]
    assert [e["label"] for e in refreshed["priority"]["entries"]] == ["Claude"]
    proposal, = refreshed["proposals"].values()
    assert proposal["status"] == "overtaken" and proposal["base"] == shown["priority_owner_revision"]
    assert routing.events_file().read_bytes() == log


@pytest.mark.parametrize("entry", ["mcp-status", "cli-summary"])
def test_author_receiving_a_summary_marks_its_shown_notices(home, monkeypatch, capsys, entry):
    from khipu import cli

    routing.add_rule("OWNER", via="app")
    for session in ("author-a", "author-b"):
        monkeypatch.setenv("KHIPU_SESSION_ID", session)
        shown = mcp_server._tool_route({})
        mcp_server._tool_route_rule(dict(text=session, rule=1, owner_request="x",
                                        base=shown["rules"][0]["owner_revision"]))
    monkeypatch.setenv("KHIPU_SESSION_ID", "author-a")
    if entry == "mcp-status":
        text = mcp_server._routing_light()["text"]
        assert "rule 1 proposal" not in text
        assert not recall_prompt.routing_marker("author-a").get("notices")
        assert routing.summary()["notices"]
        return
    else:
        assert cli.main(["route", "--summary"]) == 0
        text = capsys.readouterr().out
    assert "rule 1 proposal" in text
    assert recall_prompt.routing_marker("author-a")["notices"]
    assert not routing.summary()["notices"]
