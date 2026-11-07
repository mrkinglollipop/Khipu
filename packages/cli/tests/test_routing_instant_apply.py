"""Routing changes apply as soon as the owner types them (owner decision
2026-11-03): a priority or rule a conversation records at the owner's request,
and a pause, hold, note or lift made through ``khipu_owner_act``, take effect on
the owner's own turn, with a receipt in every conversation and a one-step Undo.

They apply only when the turn is provably the owner's by the checks that
already exist (``owner_direction.check``: an eligible hook turn, and in T3 a
message he typed in T3's record, with no earlier question). Any turn that
fails them keeps today's behaviour: refused or pending. Since 2026-11-07 every
owner change is class A this way except class C (``owner_direction.CLASS_C``:
forget, purge and a catch-up import), which is held with its own words.

Hermetic: a temp data dir and capture home, turns recorded the way the prompt
hook records them, and a synthetic T3 database. Nothing here reads T3's real
database or starts a harness."""
from __future__ import annotations

import json
import sys
import time

import pytest

from khipu import agent_view, mcp_server, owner_act, owner_direction, owner_turns, recall_prompt, routing

from tests import test_t3_owner_confirm as t3t

ACCOUNTS = [{"label": "Claude", "harness": "claude"}, {"label": "Codex", "harness": "codex"},
            {"label": "Grok", "harness": "grok"}]
OWNER = "s-instant-owner"
_ENV = t3t._ENV_VARS


def _clean_env(monkeypatch, session: str | None = None, var: str = "CLAUDE_CODE_SESSION_ID") -> None:
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    if session:
        monkeypatch.setenv(var, session)


@pytest.fixture
def plain(tmp_path, monkeypatch, khipu_app):
    """Claude Code, with the owner's accounts and owner direction on."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": ACCOUNTS, "routing_prompt_chars": 600}))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "1")
    monkeypatch.setattr(mcp_server, "_GATEWAY_ACTIVE", False)
    khipu_app.as_shell("/opt/harness/claude")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    owner_turns.write_settings({"enabled": True})
    _clean_env(monkeypatch, OWNER)
    return {"tmp": tmp_path, "mp": monkeypatch}


@pytest.fixture
def t3world(tmp_path, monkeypatch, khipu_app):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": ACCOUNTS}))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    db = tmp_path / "statev2.sqlite"
    t3db = t3t.T3(db)
    monkeypatch.setenv("KHIPU_T3_DB", str(db))
    _clean_env(monkeypatch)
    khipu_app.as_shell("/opt/harness/claude")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    owner_turns.write_settings({"enabled": True})
    tree = t3t.Tree(tmp_path / "home", monkeypatch)
    return {"tmp": tmp_path, "t3": t3db, "mp": monkeypatch, "tree": tree}


def _say(plain, text="pause Codex until tonight", *, session=OWNER, entry="cli") -> str | None:
    payload = {"session_id": session, "prompt": text, "cwd": str(plain["tmp"])}
    return owner_turns.record(payload, {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": entry}, now=time.time())


def _set_priority_by_the_owner():
    routing.set_priority([{"account": "Codex", "role": "reviews"}, {"account": "Grok", "role": "audits"}],
                         via="app", expect=routing.priority_version())


def _amend_request(append="loosened", **more):
    """What an agent sends to khipu_route_priority for one account."""
    base = routing.conversation_view()["priority_owner_revision"]
    return mcp_server._tool_route_priority({"account": "Codex", "append": append, "owner_request": "Matt said so",
                                            "expect": routing.priority_version(), "base": base, **more})


def _lines():
    return owner_direction.actions()


def _state(label: str) -> str:
    return next(a for a in routing.read_view()["accounts"] if a["label"] == label)["state"]


def _roles():
    return [e["role"] for e in routing.read_view()["priority"]["entries"]]


# ---- an owner-proven turn applies instantly -------------------------------------------------------


def test_an_owner_proven_priority_request_applies_at_once(plain):
    _set_priority_by_the_owner()
    _say(plain)
    out = _amend_request()
    assert out["ok"] is True and out["applied"] is True and out["pending"] is False, out
    held = routing.read_view()["priority"]
    assert not held.get("pending") and _roles() == ["reviews loosened", "audits"]
    assert out["instant"]["undo"] == f"khipu owner-direction undo {out['instant']['owner_action']}"


def test_an_owner_proven_rule_request_applies_at_once(plain):
    _say(plain, "add a rule that builds go to Codex")
    out = mcp_server._tool_route_rule({"text": "Builds go to Codex.", "owner_request": "Matt asked for it"})
    assert out["ok"] is True and out["applied"] is True, out
    (rule,) = routing.read_view()["rules"]
    assert rule["text"] == "Builds go to Codex." and not rule.get("pending") and rule["confirmed"]


def test_a_pause_by_direction_applies_at_once_and_is_receipted(plain):
    _say(plain)
    ok, out = owner_act.act("route.pause", {"account": "Codex", "until": "6h"})
    assert ok and out["ok"] is True, out
    assert _state("Codex") == "paused"
    (receipt,) = routing.recent_receipts(routing.read_view(), routing._now())
    assert receipt["text"].startswith("Paused Codex until ")
    assert "by your direction in a Claude Code conversation" in receipt["text"]
    assert f"`khipu owner-direction undo {out['owner_action']}`" in receipt["text"]


def test_the_note_receipt_carries_the_owners_words(plain):
    _say(plain, "set the note")
    ok, out = owner_act.act("route.note.set", {"text": "Use Grok more this week"})
    assert ok, out
    (receipt,) = routing.recent_receipts(routing.read_view(), routing._now())
    assert receipt["text"].startswith('Set the note: "Use Grok more this week", by your direction')


# ---- the receipt reaches every conversation, whole, until delivered -------------------------------


def _push(session: str) -> dict:
    agent_view.prepare_routing_table(refresh=True)
    return routing.prompt_push(recall_prompt.routing_marker(session), budget=1.0)


def _deliver(session: str, out: dict) -> None:
    at = recall_prompt._RoutingAtPrompt()
    at.session = session
    _ctx, info = at.add_to("", out)
    assert info and info.get("pending")
    assert recall_prompt._routing_delivered(info, True)


def test_every_conversation_gets_the_receipt_first_and_whole_until_delivered(plain):
    others = ["s-other-a", "s-other-b"]
    view = routing.read_view()
    for s in [OWNER, *others]:
        assert recall_prompt.mark_routing_full(s, version=view["version"], epoch=view["epoch"],
                                               at=routing._iso(routing._now()))
    _say(plain)
    ok, out = owner_act.act("route.pause", {"account": "Codex", "until": "6h"})
    assert ok, out
    (receipt,) = routing.recent_receipts(routing.read_view(), routing._now())
    for session in [OWNER, *others]:
        pushed = _push(session)
        lines = pushed["text"].split("\n")
        assert lines[1] == receipt["text"], (session, pushed["text"])
        _deliver(session, pushed)
        again = _push(session)
        assert receipt["text"] not in (again.get("text") or ""), (session, again)


def test_the_receipt_comes_from_the_folded_view_not_the_owner_actions_log(plain, monkeypatch):
    # The prompt path has 50 ms: finding a receipt must not read the log.
    _say(plain)
    ok, _out = owner_act.act("route.pause", {"account": "Codex", "until": "6h"})
    assert ok
    view = routing.read_view()
    monkeypatch.setattr(owner_direction, "read_log", lambda: pytest.fail("the prompt path read the owner log"))
    monkeypatch.setattr(owner_direction, "_find", lambda *_a: pytest.fail("the prompt path read the owner log"))
    assert routing.recent_receipts(view, routing._now())
    _push("s-fresh")


def test_the_route_view_shows_the_change_as_applied_with_its_undo(plain, capsys):
    _set_priority_by_the_owner()
    _say(plain)
    out = _amend_request()
    assert out["applied"] is True
    routing._print_view(routing.read_view(), routing._now(), None)
    printed = capsys.readouterr().out
    assert "Applied by your direction" in printed
    assert f"khipu owner-direction undo {out['instant']['owner_action']}" in printed
    assert "unconfirmed" not in printed.lower().split("applied by your direction")[0]


# ---- a turn that is not provably the owner's keeps today's behaviour ------------------------------


def test_a_dispatched_job_keeps_the_request_pending(plain):
    _set_priority_by_the_owner()
    _say(plain, "do the build and loosen Codex when done", entry="sdk-cli")
    out = _amend_request()
    assert out["ok"] is True and "applied" not in out and "instant" not in out, out
    assert routing.read_view()["priority"]["pending"] is True
    assert routing.recent_receipts(routing.read_view(), routing._now()) == []
    assert _lines() == []


def test_no_owner_turn_keeps_the_request_pending_and_a_pause_refused(plain):
    _set_priority_by_the_owner()
    out = _amend_request()
    assert "applied" not in out and routing.read_view()["priority"]["pending"] is True
    ok, refused = owner_act.act("route.pause", {"account": "Codex", "until": "6h"})
    assert not ok and refused["code"] == "no_recorded_turn"
    assert _state("Codex") != "paused"


def test_a_handoff_or_job_marker_keeps_the_request_pending(plain, monkeypatch):
    _set_priority_by_the_owner()
    _say(plain)
    monkeypatch.setenv("KHIPU_JOB", "1")
    out = _amend_request()
    assert "applied" not in out and routing.read_view()["priority"]["pending"] is True
    assert _lines() == []


def test_a_crossed_request_is_never_applied(plain):
    routing.add_rule("OWNER words", via="app")
    base = mcp_server._tool_route({})["rules"][0]["owner_revision"]
    routing.edit_rule(1, "OWNER moved on", via="app")
    _say(plain)
    out = mcp_server._tool_route_rule({"text": "stale edit", "rule": 1, "owner_request": "y", "base": base})
    assert out.get("ok") is False and "applied" not in out, out
    assert [r["text"] for r in routing.read_view()["rules"]] == ["OWNER moved on"]
    assert _lines() == []


# ---- T3: the confirm's own proof decides ----------------------------------------------------------


def _t3_pause(t3w, account="Grok"):
    t3w["mp"].setenv("CLAUDE_CODE_SESSION_ID", t3t.T3_SESSION)
    return owner_act.act("route.pause", {"account": account, "until": "6h"})


def test_a_t3_owners_own_change_applies_with_no_earlier_question(t3world):
    # Class A (2026-11-07): no ask made in this thread, and the owner's own message proves it.
    mid = t3world["t3"].message(t3t.THREAD, time.time() - 30, text="pause Codex")
    t3t._t3_hook_turn(prompt="pause Codex")
    ok, out = _t3_pause(t3world, "Codex")
    assert ok and out["ok"] is True and "pending" not in out, out
    assert _state("Codex") == "paused" and not owner_direction.requests_path().exists()
    line = next(x for x in _lines() if x["target"] == "codex|")
    assert line["auth"] == "direction" and line["binding"] == "t3" and line["receipt"]
    assert line["instant"]["source"] == "t3" and line["instant"]["message"] == mid
    assert "after_ask" not in line["instant"]


def test_a_t3_thread_the_owner_never_typed_in_is_refused_not_held(t3world):
    # Only a machine message (a wake) in the thread: no owner message proves anything, and nothing is held.
    t3world["t3"].message(t3t.THREAD, time.time() - 5, text="Continue", by="agent", source="mcp",
                          scheduledTaskId="task-1")
    t3t._t3_hook_turn()
    ok, out = _t3_pause(t3world, "Grok")
    assert not out.get("applied") and not out.get("pending"), out
    assert "no message the owner typed" in json.dumps(out), out
    assert _state("Grok") != "paused" and _lines() == [] and not owner_direction.requests_path().exists()


def test_a_t3_message_does_not_expire_after_six_hours(t3world):
    t3world["t3"].message(t3t.THREAD, time.time() - 30, text="pause Grok later")
    # Seven hours on, with the owner's only message that old, and a typed hook turn.
    later = owner_direction._clock() + 7 * 3600
    t3world["mp"].setattr(owner_direction, "_clock", lambda: later)
    t3t._t3_hook_turn()
    ok, out = _t3_pause(t3world, "Grok")
    assert ok and not out.get("pending") and out["owner_action"], out
    assert _state("Grok") == "paused"
    assert owner_direction._find(out["owner_action"])["receipt"]


def test_a_t3_own_priority_request_needs_no_prior_question(t3world):
    _set_priority_by_the_owner()
    # An own request on a typed owner turn needs no earlier question.
    t3world["t3"].message(t3t.THREAD, time.time() - 30, text="loosen Codex")
    t3t._t3_hook_turn()
    t3world["mp"].setenv("CLAUDE_CODE_SESSION_ID", t3t.T3_SESSION)
    out = _amend_request()
    assert out["applied"] and not routing.read_view()["priority"]["pending"], out
    ok, undone = owner_act.act("direction.undo", {"id": out["instant"]["owner_action"]})
    assert ok, undone
    # Another own change in the thread, then a newer owner message: applied.
    ok, paused = _t3_pause(t3world, "Codex")
    assert ok and not paused.get("pending") and _state("Codex") == "paused", paused
    t3world["t3"].message(t3t.THREAD, time.time() - 3, text="loosen Codex a bit")
    t3t._t3_hook_turn()
    out = _amend_request("a bit")
    assert out.get("applied") is True and not routing.read_view()["priority"].get("pending"), out


# ---- Undo: exactly one change, with a receipt -----------------------------------------------------


def test_undo_reverts_exactly_one_change_and_is_receipted(plain):
    _set_priority_by_the_owner()
    _say(plain)
    first = _amend_request()
    ok, rule = owner_act.act("route.rule.add", {"text": "Builds go to Codex."})
    assert first["applied"] and ok, rule
    assert _roles() == ["reviews loosened", "audits"] and len(routing.read_view()["rules"]) == 1
    # Undo the priority change by direction: the rule, a later and different change, stays.
    ok, undone = owner_act.act("direction.undo", {"id": first["instant"]["owner_action"]})
    assert ok and undone["ok"] is True, undone
    assert _roles() == ["reviews", "audits"], "the whole request is taken back, not only its confirmation"
    assert not routing.read_view()["priority"].get("pending")
    assert [r["text"] for r in routing.read_view()["rules"]] == ["Builds go to Codex."]
    receipts = [r["text"] for r in routing.recent_receipts(routing.read_view(), routing._now())]
    assert any(t.startswith("Undid: Confirmed the priority") for t in receipts), receipts
    assert owner_direction.line_state(first["instant"]["owner_action"]) == "undone"
    # The rule's own line is untouched and can still be undone on its own.
    rule_line = next(x for x in _lines() if x["action"] == "route.rule.add")
    assert owner_direction.line_state(rule_line["id"]) == "applied"


def test_undo_of_an_instant_rule_request_takes_the_rule_back(plain):
    routing.add_rule("Original rule", via="app")
    _say(plain)
    base = mcp_server._tool_route({})["rules"][0]["owner_revision"]
    out = mcp_server._tool_route_rule({"text": "Directed edit", "rule": 1, "owner_request": "Matt said so", "base": base})
    assert out["applied"] is True and routing.read_view()["rules"][0]["text"] == "Directed edit"
    ok, undone = owner_act.act("direction.undo", {"id": out["instant"]["owner_action"]})
    assert ok, undone
    (rule,) = routing.read_view()["rules"]
    assert rule["text"] == "Original rule" and not rule.get("pending")


def test_undo_of_an_older_change_is_refused_over_a_newer_one_to_the_same_target(plain):
    _set_priority_by_the_owner()
    _say(plain)
    first = _amend_request("first")
    second = _amend_request("second")
    assert first["applied"] and second["applied"], (first, second)
    ok, refused = owner_act.act("direction.undo", {"id": first["instant"]["owner_action"]})
    assert not ok and "newer" in refused["error"].lower(), refused
    assert _roles()[0].endswith("second")


def test_undo_by_the_cli_path_works_for_an_instant_change(plain):
    from khipu import cli

    _say(plain)
    ok, out = owner_act.act("route.pause", {"account": "Codex", "until": "6h"})
    assert ok and _state("Codex") == "paused"
    _say(plain, "undo that")
    assert cli.main(["owner-direction", "undo", out["owner_action"], "--owner-direction"]) == 0
    assert _state("Codex") != "paused"


# ---- what stays explicit ---------------------------------------------------------------------------


def test_class_c_is_exactly_what_cant_be_taken_back():
    assert owner_direction.CLASS_C == {"forget.send", "forget.here", "forget.not_same", "ledger.purge",
                                       "ledger.import"}
    assert owner_direction.class_c("ledger.import", {"file": "t.json"}) is False
    assert owner_direction.class_c("ledger.import", {"file": "t.json", "catch_up": True}) is True
    for action in ("direction.set", "direction.off", "direction.undo", "config.routing_setting", "accounts.stop",
                   "route.cap.set", "route.definition.set", "route.clear", "route.priority.clear", "project.merge"):
        assert owner_direction.class_c(action, {}) is False, action
    assert "outbox.span_covered" in owner_direction.PLANNED_ASKS  # B, still always asked


def test_a_trust_setting_applies_at_once_and_its_undo_too(plain):
    # Decision 1 (2026-11-07): direction.set is class A, with its Undo.
    _say(plain)
    ok, out = owner_act.act("direction.set", {"key": "latest_window_h", "value": "12"})
    assert ok and out["owner_action"] and not out.get("pending"), out
    assert owner_turns.settings()["latest_window_h"] == 12
    ok, undone = owner_act.act("direction.undo", {"id": out["owner_action"]})
    assert ok, undone
    assert owner_turns.settings()["latest_window_h"] != 12


def test_in_t3_a_trust_setting_applies_and_a_forget_is_held(t3world):
    t3world["t3"].message(t3t.THREAD, time.time() - 2, text="widen the window, and send the forgets")
    t3t._t3_hook_turn()
    t3world["mp"].setenv("CLAUDE_CODE_SESSION_ID", t3t.T3_SESSION)
    assert owner_direction.check("direction.set")["instant"]["source"] == "t3"
    ok, out = owner_act.act("direction.set", {"key": "latest_window_h", "value": "24"})
    assert ok and not out.get("pending") and owner_turns.settings()["latest_window_h"] == 24, out
    ok, out = owner_act.act("forget.send", {})
    assert ok and out["pending"] is True and out["applied"] is False and out["ask"], out
    assert [x["action"] for x in _lines()] == ["direction.set"]


def test_outside_t3_a_forget_is_held_with_its_own_words_too(plain):
    _say(plain, "send the forgets to the hub")
    ok, out = owner_act.act("forget.send", {})
    assert ok and out["pending"] is True and out["applied"] is False and out["ask"], out
    assert _lines() == []


# ---- the audit line carries its evidence -----------------------------------------------------------


def test_the_owner_actions_log_carries_the_evidence_it_relied_on(plain):
    turn = _say(plain)
    ok, out = owner_act.act("route.pause", {"account": "Codex", "until": "6h"})
    assert ok
    (line,) = [x for x in _lines() if x["id"] == out["owner_action"]]
    assert line["auth"] == "direction" and line["session"] == OWNER and line["harness"] == "claude_code"
    assert line["turn"] == turn and line["prompt_sha256"] and line["binding"] == "hook"
    assert line["instant"]["source"] == "hook" and line["instant"]["turn"] == turn
    assert line["instant"]["window_h"] == owner_turns.settings()["latest_window_h"]
    assert line["state"] == "applied" and line["receipt"]["what"].startswith("Paused Codex")
    assert owner_direction.verify_log()["ok"] is True


def test_a_request_applied_instantly_is_logged_with_its_evidence_too(plain):
    _set_priority_by_the_owner()
    turn = _say(plain)
    out = _amend_request()
    (line,) = [x for x in _lines() if x["id"] == out["instant"]["owner_action"]]
    assert line["action"] == "route.priority.confirm" and line["instant"]["turn"] == turn
    assert line["session"] == OWNER and line["before"]["pending"] is True


# ---- review findings (independent security review of 671dd93e) -------------------------------------


def _inject_from_another_conversation(plain, role="INJECTED: everything goes to Grok"):
    plain["mp"].setenv("CLAUDE_CODE_SESSION_ID", "s-other-B")
    base = routing.conversation_view()["priority_owner_revision"]
    out = mcp_server._tool_route_priority({"account": "Grok", "role": role, "owner_request": "x",
                                           "expect": routing.priority_version(), "base": base})
    assert out.get("ok") is not False and not out.get("applied"), out
    assert routing.read_view()["priority_stored"]["pending"]
    plain["mp"].setenv("CLAUDE_CODE_SESSION_ID", OWNER)


def test_an_amend_never_confirms_another_conversations_pending_entry(plain):
    _set_priority_by_the_owner()
    _inject_from_another_conversation(plain)
    _say(plain, "loosen Codex a bit")
    out = _amend_request()
    held = routing.read_view()["priority"]
    assert "applied" not in out and held["pending"] is True, out
    assert any("INJECTED" in e["role"] for e in held["entries"])  # still only a pending request
    assert _lines() == [] and routing.recent_receipts(routing.read_view(), routing._now()) == []


def test_the_chain_through_a_second_unproven_amend_is_not_laundered_either(plain):
    _set_priority_by_the_owner()
    _inject_from_another_conversation(plain)
    # This conversation first amends with no owner turn (pending, carrying B's entry) ...
    assert "applied" not in _amend_request("quietly")
    # ... then the owner types: the amend must still not take B's content live with it.
    _say(plain, "loosen Codex a bit")
    out = _amend_request("again")
    assert "applied" not in out and routing.read_view()["priority"]["pending"] is True, out


def test_an_amend_alone_still_applies_and_the_receipt_names_the_words_that_went_live(plain):
    _set_priority_by_the_owner()
    _say(plain)
    out = _amend_request("loosened for reviews")
    assert out["applied"] is True
    (receipt,) = routing.recent_receipts(routing.read_view(), routing._now())
    assert receipt["text"].startswith('Confirmed the priority: Codex "reviews loosened for reviews"'), receipt
    assert "Grok" not in receipt["text"].split(", by your direction")[0]


def test_a_whole_priority_receipt_names_every_changed_account_with_its_words(plain):
    _set_priority_by_the_owner()
    _say(plain)
    base = routing.conversation_view()["priority_owner_revision"]
    entries = [{"account": "Codex", "role": "reviews", "revision": e["revision"]}
               for e in routing.conversation_view()["priority"]["entries"] if e["label"] == "Codex"]
    entries += [{"account": "Grok", "role": "ALL BUILDS NOW", "revision":
                 next(e["revision"] for e in routing.conversation_view()["priority"]["entries"]
                      if e["label"] == "Grok")}]
    out = mcp_server._tool_route_priority({"entries": entries, "owner_request": "Matt", "base": base})
    assert out.get("applied") is True, out
    (receipt,) = routing.recent_receipts(routing.read_view(), routing._now())
    assert 'Grok "ALL BUILDS NOW"' in receipt["text"] and "Codex" not in receipt["text"].split(", by your")[0]


def test_clearing_the_whole_priority_is_the_owners_own_change_too(plain):
    _set_priority_by_the_owner()
    _say(plain)
    ok, out = owner_act.act("route.priority.clear", {"expect": routing.priority_version()})
    assert ok and out["owner_action"] and not out.get("pending"), out
    assert not routing.read_view()["priority"]


def test_a_burst_of_receipts_never_pushes_other_owner_changes_out(plain):
    _say(plain)
    ok, out = owner_act.act("config.routing_setting", {"key": "routing_prompt_chars", "value": "700"})
    assert ok, out
    for i in range(routing.RECEIPTS_KEPT + 5):
        ok, out = owner_act.act("route.note.set", {"text": f"note {i}"})
        assert ok, out
    effective = routing.read_view()["effective"]
    receipts = [k for k, v in effective.items() if k.startswith("owner:") and v.get("receipt")]
    others = [k for k, v in effective.items() if k.startswith("owner:") and not v.get("receipt")]
    assert len(receipts) == routing.RECEIPTS_KEPT and f"owner:{_lines()[0]['id']}" in others
