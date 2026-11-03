"""The owner's rules and note are bounded: write-time limits, a render budget
at session start and full resend that never pushes account state out, and a
changed rule or note longer than the prompt's room that is never dropped."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from khipu import agent_view, mcp_server, recall_prompt, recall_rule, routing
from tests.test_routing_acceptance import replay  # noqa: F401

T0 = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
ACCOUNTS = [{"label": "Claude", "harness": "claude"}, {"label": "Codex", "harness": "codex"}]
ME = {"kind": "conversation", "harness": "claude_code", "project": "project-a", "session": "s-one"}


_REAL = {key: routing.SETTINGS[key] for key in ("routing_rule_chars", "routing_rules_max",
                                                "routing_note_chars", "routing_rules_chars")}


@pytest.fixture(autouse=True)
def _real_defaults(monkeypatch):
    # conftest turns the limits off for the older tests; these put them back.
    for key, entry in _REAL.items():
        monkeypatch.setitem(routing.SETTINGS, key, entry)


@pytest.fixture
def home(tmp_path, monkeypatch, khipu_app):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": ACCOUNTS}))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setattr(routing, "_now", lambda: T0)
    return {"data": data, "callers": khipu_app}


def _set(home, **values):
    path = home["data"] / "config.json"
    config = json.loads(path.read_text())
    config.update(values)
    path.write_text(json.dumps(config))


def _cli(argv, capsys):
    from khipu import cli

    rc = cli.main(argv)
    return rc, capsys.readouterr().out


def _sentence(n: int, size: int = 90) -> str:
    return (f"Sentence {n} " + "w" * size)[:size] + "."


def _rule(count: int, size: int = 90) -> str:
    return " ".join(_sentence(i, size) for i in range(count))


# ---- write-time limits, on every path ---------------------------------------------------------


def test_the_defaults_are_the_numbers_the_summary_and_hook_can_carry():
    cfg = routing.settings()
    assert (cfg["routing_rule_chars"], cfg["routing_rules_max"], cfg["routing_note_chars"]) == (600, 40, 1500)
    # The rules budget plus the summary budget plus the heading fit the hook beside the recall rule.
    assert cfg["routing_rules_chars"] + cfg["routing_summary_chars"] + 400 \
        + recall_rule._utf16_len(recall_rule.RULE_MD.strip()) < recall_rule._HOOK_CONTEXT_BUDGET


def test_the_cli_refuses_an_overlong_rule_and_note_with_the_limit_and_the_length(home, capsys):
    rc, out = _cli(["route", "rule", "add", "x" * 601, "--from-app"], capsys)
    answer = json.loads(out)
    assert rc == 2 and answer["ok"] is False and answer["code"] == "too_long"
    assert "601" in answer["error"] and "600" in answer["error"]
    rc, out = _cli(["route", "note", "set", "n" * 1501, "--from-app"], capsys)
    answer = json.loads(out)
    assert rc == 2 and answer["ok"] is False and "1,501" in answer["error"] and "1,500" in answer["error"]
    assert routing.read_view()["rules"] == [] and routing.read_view()["note"] is None
    rc, out = _cli(["route", "rule", "add", "x" * 600, "--from-app"], capsys)
    assert rc == 0 and json.loads(out)["ok"] is True
    rc, out = _cli(["route", "rule", "edit", "1", "y" * 700, "--from-app"], capsys)
    assert rc == 2 and "700" in json.loads(out)["error"]
    assert routing.read_view()["rules"][0]["text"] == "x" * 600


def test_the_cli_refuses_a_rule_past_the_count_and_lets_an_edit_through(home, capsys):
    _set(home, routing_rules_max=2)
    for i in range(2):
        routing.add_rule(f"Rule {i}.", via="app")
    rc, out = _cli(["route", "rule", "add", "One more.", "--from-app"], capsys)
    answer = json.loads(out)
    assert rc == 2 and answer["ok"] is False and answer["code"] == "too_many"
    assert "2" in answer["error"] and "routing_rules_max" in answer["error"]
    routing.edit_rule(1, "Rule 0 changed.", via="app")
    assert len(routing.read_view()["rules"]) == 2


def test_a_conversation_request_over_the_limit_is_refused_over_mcp_and_the_cli(home, capsys):
    out = mcp_server._tool_route_rule({"text": "x" * 601, "owner_request": "add the rule"})
    assert out["ok"] is False and "601" in out["error"] and "600" in out["error"]
    routing.add_rule("Existing.", via="app")
    shown = mcp_server._tool_route({})["rules"][0]["owner_revision"]
    out = mcp_server._tool_route_rule({"text": "x" * 601, "rule": 1, "owner_request": "change it", "base": shown})
    assert out["ok"] is False and out["code"] == "too_long"
    _set(home, routing_rules_max=1)
    out = mcp_server._tool_route_rule({"text": "Another.", "owner_request": "add the rule"})
    assert out["ok"] is False and out["code"] == "too_many"
    home["callers"].as_shell("/usr/local/bin/claude")
    rc, text = _cli(["route", "rule", "add", "x" * 601, "--at-owner-request=add it"], capsys)
    assert rc == 2 and json.loads(text)["ok"] is False
    assert [r["text"] for r in routing.read_view()["rules"]] == ["Existing."]


def test_the_owner_direction_path_is_refused_the_same_way(home):
    with pytest.raises(routing.RuleCapError, match=r"601 characters; the limit is 600"):
        routing.add_rule("x" * 601, via="direction")
    with pytest.raises(routing.RuleCapError, match=r"1,501 characters; the limit is 1,500"):
        routing.set_note("n" * 1501, via="direction")
    assert routing.read_events()[0] == []


def test_a_limit_of_zero_turns_the_check_off_and_undo_restores_stored_words(home):
    _set(home, routing_rule_chars=0, routing_note_chars=0)
    routing.add_rule("x" * 2000, via="app")
    routing.set_note("n" * 3000, via="app")
    _set(home, routing_rule_chars=600, routing_note_chars=1500)
    # Stored words over the limit keep working: retire, restore, and undo's rewrite of them.
    routing.edit_rule(1, "short", via="app")
    routing.edit_rule(1, "x" * 2000, via="app", enforce_caps=False)
    routing.set_note("n" * 3000, via="app", enforce_caps=False)
    rule = routing.read_view()["rules"][0]
    routing.retire_rule(1, via="app")
    routing.restore_rule(1, via="app")
    assert routing.read_view()["rules"][0]["text"] == rule["text"] == "x" * 2000
    assert len(routing.read_view()["note"]["text"]) == 3000


# ---- the render budget -----------------------------------------------------------------------


def _fill(rules=30, size=300):
    for i in range(rules):
        routing.add_rule(f"{i}: " + "r" * (size - 4), via="app")


def test_stored_oversized_rules_render_within_the_budget_with_a_pointer(home):
    _set(home, routing_rule_chars=0)
    routing.add_rule("first: " + "a" * 1200, via="app")
    _set(home, routing_rule_chars=600)
    _fill(rules=20)
    routing.report(account="Codex", state="open", author=ME)
    out = routing.summary()
    text = out["text"]
    budget = routing.settings()["routing_rules_chars"]
    assert out["rules_left_out"] > 0
    owner_lines = [line for line in text.splitlines()
                   if line == "Standing rules:" or (line[:1].isdigit() and ". " in line) or "standing rules left out" in line]
    assert sum(len(line) + 1 for line in owner_lines) <= budget
    assert f"{out['rules_left_out']} of 21 standing rules left out" in text and "`khipu_route`" in text
    assert "Codex" in text
    # Cut at whole rules, in the owner's order: the rules shown are a prefix.
    shown = [line.split(".")[0] for line in text.splitlines() if line[:1].isdigit() and ". " in line]
    assert shown == [str(n) for n in range(1, len(shown) + 1)]
    assert "a" * 1200 in text  # the stored oversized rule that fits the budget is not cut


def test_a_note_longer_than_half_the_budget_is_a_pointer_with_its_first_sentence(home):
    _set(home, routing_note_chars=0)
    routing.set_note("Codex first for builds. " + "n" * 2400, via="app")
    out = routing.summary()
    assert "Codex first for builds." in out["text"] and "n" * 100 not in out["text"]
    assert "read it with `khipu_route`" in out["text"] and "note" in out["covered_entities"]


def test_account_state_survives_a_huge_rules_set_at_session_start(home):
    _set(home, routing_rules_chars=0, routing_rule_chars=0, routing_rules_max=0)
    _fill(rules=40, size=600)
    routing.report(account="Codex", state="limited", author=ME)
    out = routing.summary()
    assert not recall_rule.start_context_fits(out["text"])  # 24,000 characters of rules, budget off
    ctx, pushed = recall_rule.fit_start_context(recall_rule.RULE_MD.strip(), out["text"], lean=out["lean_text"])
    assert pushed and recall_rule.ROUTING_TOO_LONG not in ctx
    assert recall_rule._utf16_len(ctx) <= recall_rule._HOOK_CONTEXT_BUDGET
    assert "Codex" in ctx and "Standing rules:" not in ctx and recall_rule.RULE_MD.strip() in ctx
    assert "read them with `khipu_route`" in ctx
    # With the default budget the same rules arrive pointed to, state intact, in the one summary.
    _set(home, routing_rules_chars=2000)
    budgeted = routing.summary()
    assert recall_rule.start_context_fits(budgeted["text"]) and "Codex" in budgeted["text"]
    assert budgeted["rules_left_out"] > 0


def test_the_whole_summary_is_swapped_for_the_pointer_only_when_account_state_alone_cannot_fit():
    memory = recall_rule.RULE_MD.strip()
    huge = "## Routing\n" + "\n".join("Rule %d: " % i + "x" * 150 for i in range(80))
    state = "## Routing\nCodex: open"
    ctx, pushed = recall_rule.fit_start_context(memory, huge, lean=state)
    assert pushed and ctx.endswith(state)
    ctx, pushed = recall_rule.fit_start_context(memory, huge, lean=huge)
    assert not pushed and ctx.endswith(recall_rule.ROUTING_TOO_LONG)
    ctx, pushed = recall_rule.fit_start_context(memory, huge)
    assert not pushed and ctx.endswith(recall_rule.ROUTING_TOO_LONG)


def test_the_hook_text_carries_the_lean_variant():
    text = recall_rule._Text("x" * 9000)
    text.lean = "## Routing\nCodex: open"
    ctx, pushed = recall_rule.fit_start_context(recall_rule.RULE_MD.strip(), text)
    assert pushed and ctx.endswith("Codex: open")


def test_pointed_to_rules_are_not_owed_again_until_they_change(replay):
    replay.configure(routing_rules_chars=800)
    for i in range(12):
        routing.add_rule(f"{i}: " + "r" * 296, via="app")
    out, context, info = replay.offer()
    assert out["kind"] == "full" and "standing rules left out" in out["text"]
    replay.ack(info)
    assert not recall_prompt.routing_marker("s1").get("owed")
    routing.edit_rule(1, "1: changed, and short.", via="app")
    out, _context, _info = replay.offer()
    assert "1: changed, and short." in out["text"]


# ---- the per-prompt path ---------------------------------------------------------------------


def _marker(replay):
    return {"version": replay.view()["version"], "epoch": replay.view()["epoch"],
            "full_at": routing._iso(replay.now)}


def test_a_changed_long_rule_arrives_as_a_pointer_and_its_first_sentence_with_no_session(replay):
    routing.add_rule("Short rule.", via="app")
    marker = _marker(replay)
    routing.edit_rule(1, _rule(5), via="app")
    out = routing.prompt_push(marker, budget=1.0, now=replay.now)
    text = out["text"]
    assert out["kind"] == "changes"
    assert "Standing rule 1 changed" in text and _sentence(0) in text
    assert _sentence(1) not in text and "`khipu_route`" in text
    assert len(text.split("\n", 1)[1]) <= 400
    assert not any(line.endswith("…") for line in text.splitlines())


def test_a_long_first_sentence_leaves_only_the_pointer_never_a_cut(replay):
    routing.add_rule("Short rule.", via="app")
    marker = _marker(replay)
    routing.edit_rule(1, "w" * 500 + ". Then more.", via="app")
    text = routing.prompt_push(marker, budget=1.0, now=replay.now)["text"]
    assert "Standing rule 1 changed (512 characters" in text and "w" * 50 not in text


def test_a_changed_long_note_gets_the_same_pointer(replay):
    routing.add_rule("Short rule.", via="app")
    marker = _marker(replay)
    routing.set_note(_rule(6), via="app")
    text = routing.prompt_push(marker, budget=1.0, now=replay.now)["text"]
    assert "Note from the owner changed" in text and _sentence(0) in text and _sentence(2) not in text


def test_a_short_changed_rule_still_arrives_whole(replay):
    routing.add_rule("Short rule.", via="app")
    marker = _marker(replay)
    routing.edit_rule(1, "Still short, and whole.", via="app")
    assert "Still short, and whole." in routing.prompt_push(marker, budget=1.0, now=replay.now)["text"]


def test_with_a_session_the_long_rule_is_delivered_whole_across_prompts(replay):
    routing.add_rule("Short rule.", via="app")
    out, context, info = replay.offer()
    replay.ack(info)
    routing.edit_rule(1, _rule(6), via="app")
    seen = []
    for _ in range(40):
        out, context, info = replay.offer()
        if not context:
            break
        seen.append(context)
        assert len(context.split("\n", 1)[-1]) <= 450
        replay.ack(info)
    pages = ["\n".join(line for line in context.split("\n")[2:] if line != "More routing owed.")
             for context in seen]
    assert len(pages) > 1 and "".join(pages) == "1. " + _rule(6)
    assert not recall_prompt.routing_marker("s1").get("owed")


# ---- doctor ---------------------------------------------------------------------------------


def test_doctor_warns_about_stored_words_over_their_limits_and_shows_the_budget(home):
    _set(home, routing_rule_chars=0, routing_note_chars=0, routing_rules_max=0)
    routing.add_rule("a" * 700, via="app")
    for i in range(3):
        routing.add_rule(f"{i}: " + "b" * 300, via="app")
    routing.set_note("n" * 1600, via="app")
    _set(home, routing_rule_chars=600, routing_note_chars=1500, routing_rules_max=3, routing_rules_chars=900)
    block = routing.doctor_block()
    warnings = block["warnings"]
    assert any("Standing rule 1 is longer than the 600-character limit" in w for w in warnings)
    assert any("There are 4 standing rules; the limit for new ones is 3" in w for w in warnings)
    assert any("Your note is 1,600 characters; the limit is 1,500" in w for w in warnings)
    assert any("shows only part of your words" in w for w in warnings)
    row = block["rules_budget"]
    assert row["over_rule_cap"] == [1] and row["rules"] == 4 and row["note_chars"] == 1600
    assert row["left_out"] > 0 and row["start"] == "full"


def test_doctor_shows_the_session_start_swap(home, monkeypatch):
    routing.report(account="Codex", state="open", author=ME)
    _set(home, routing_rules_chars=0, routing_rule_chars=0)
    routing.add_rule("r" * 100, via="app")
    monkeypatch.setattr(recall_rule, "start_context_fits", lambda text, *a: "Standing rules:" not in text)
    block = routing.doctor_block()
    assert block["rules_budget"]["start"] == "lean"
    assert any("conversations get the account state" in w for w in block["warnings"])
    monkeypatch.setattr(recall_rule, "start_context_fits", lambda text, *a: False)
    block = routing.doctor_block()
    assert block["rules_budget"]["start"] == "pointer"
    assert any("even without your rules, note and priority" in w for w in block["warnings"])


# ---- a long priority and a live exception still reach a new conversation ------------------------


def test_the_summary_keeps_a_long_priority_owed_and_leads_with_the_exception(replay):
    names = ["Grok", "Codex", "Claude"]
    entries = [{"account": n, "role": f"{n} role " + " ".join(f"Point {j} " + "p" * 90 + "." for j in range(18))}
               for n in names]
    routing.set_priority(entries, via="app", expect=routing.priority_version())
    routing.amend_priority("Codex", exception="Codex checks may run 15 points over pace", until="2h",
                           via="app", expect=routing.priority_version())
    agent_view.prepare_routing_table(refresh=True)
    out = routing.summary()
    assert "priority" not in out["covered_entities"]
    assert "Exception on Codex until" in out["text"] and "Your priority is too long" in out["text"]
    assert out["text"].index("Exception on Codex") < out["text"].index("Your priority is too long")


def test_a_new_conversation_gets_a_long_priorities_exception_and_body(replay):
    names = ["Grok", "Codex", "Claude"]
    entries = [{"account": n, "role": f"{n} role " + " ".join(f"Point {j} " + "p" * 90 + "." for j in range(18))}
               for n in names]
    routing.set_priority(entries, via="app", expect=routing.priority_version())
    routing.amend_priority("Codex", exception="Codex checks may run 15 points over pace", until="2h",
                           via="app", expect=routing.priority_version())
    held = routing.read_view()["priority"]
    assert len(routing.priority_line(held, replay.now)) > 5000
    exc = next(x for e in held["entries"] for x in e.get("exceptions") or [])
    sentence = routing.exception_change_sentence("Codex", exc)
    agent_view.prepare_routing_table(refresh=True)
    start = recall_rule._RoutingAtStart(session_id="s-new").begin()
    text = start.text()
    assert sentence in text and "priority; revision" in text  # the exception arrives at once, then the body pages
    ctx, pushed = recall_rule.fit_start_context(recall_rule.RULE_MD.strip(), text)
    assert pushed and sentence in ctx
    start.delivered("s-new")
    assert "priority" in recall_prompt.routing_marker("s-new").get("owed", [])  # the body is still owed
    seen = []
    for _ in range(80):
        out, context, info = replay.offer("s-new")
        if not context:
            break
        seen.append(context)
        replay.ack(info)
    body = "\n".join([text, *seen])
    assert "Grok role" in body and "Claude role" in body and "Point 17" in body
    assert not recall_prompt.routing_marker("s-new").get("owed")


def test_the_refusal_tells_an_agent_not_to_paraphrase_and_the_owner_to_shorten(home):
    with pytest.raises(routing.RuleCapError, match="do not shorten or paraphrase his words yourself"):
        routing.add_rule("x" * 601, via="direction")
    with pytest.raises(routing.RuleCapError, match="do not shorten or paraphrase"):
        routing.request_rule("x" * 601, via="mcp", author=ME)
    with pytest.raises(routing.RuleCapError, match="Shorten it and try again"):
        routing.add_rule("x" * 601, via="app")


def test_a_conversations_pending_requests_never_take_the_owners_room(home):
    _set(home, routing_rules_max=2)
    routing.add_rule("Owner rule.", via="app")
    routing.request_rule("A request.", via="mcp", author=ME, owner_request="add it")
    routing.add_rule("Second owner rule.", via="app")  # the pending request does not count
    with pytest.raises(routing.RuleCapError, match="routing_rules_max"):
        routing.add_rule("Third.", via="app")
    with pytest.raises(routing.RuleCapError, match="Tell the owner"):
        routing.request_rule("Another request.", via="mcp", author=ME, owner_request="add it")
