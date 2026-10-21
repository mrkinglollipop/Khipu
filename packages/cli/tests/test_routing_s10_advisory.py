"""Slice 10t: executable case 10 contracts, using disposable local data only.

Read docs/plans/2026-10-20-routing-first-class.md sections 8 and 13 D.
The tests-first wire seams are tier_table_set (tiers, confirmed), direction_set
(account, topic, value), and view fields tier_table, contradictions, rule_hygiene.
Flags name their kind and affected entity ids, so consumers need not parse prose.
These seams specify new behavior; no test implements the advisory classifier.
Only a measured ContractGap may xfail. Setup and hook protocol errors must fail.
"""
from __future__ import annotations

import copy
import json
import shlex

import pytest

from khipu import routing, routing_delegation
from tests.test_routing_acceptance import AUTHOR, expect

pytest_plugins = ("tests.test_routing_acceptance",)

TIERS = {
    "scope": "opus", "mock": "opus", "ruling": "opus", "hard-security": "opus",
    "failed-fix": "opus", "build": "sonnet", "fix-round": "sonnet", "tests": "sonnet",
    "docs": "sonnet", "synthesis": "sonnet", "mechanical": "haiku",
}
LINE = "Rule 12: fix rounds go to Sonnet; this dispatch is Opus. Say why if intended."


def table(replay, *, confirmed=True, author=None, tiers=None):
    row = {"type": "tier_table_set", "tiers": copy.deepcopy(TIERS if tiers is None else tiers),
           "confirmed": confirmed, "written_by": author or {"kind": "owner"}, "via": "app"}
    replay.write(row)


def payload(model="opus", brief="task: fix-round\nRepair the synthetic fixture."):
    return {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Agent",
            "tool_input": {"model": model, "prompt": brief}}


def hook(call):
    original = copy.deepcopy(call)
    out, _, _ = routing_delegation.hook_main(json.dumps(call))
    assert call == original
    # A deny, ask, modified input, stop, or top-level decision must never hide
    # behind an expected missing advisory.
    assert set(out) <= {"hookSpecificOutput"}, out
    if out:
        assert set(out["hookSpecificOutput"]) == {"hookEventName", "additionalContext"}, out
        assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    return out.get("hookSpecificOutput", {}).get("additionalContext", "")


def dispatch_lines(text):
    # The standing rule's occurrence in the ordinary routing block is not a
    # dispatch result. Exact line matching prevents that false positive.
    return [line for line in text.splitlines() if line.startswith("Rule 12:")]


def test_10_confirmed_tier_table_round_trips_all_classes(replay):
    table(replay)
    for body in (replay.view(), replay.pull()):
        actual = body.get("tier_table") or {}
        expect(actual.get("tiers"), TIERS, "10: confirmed tier table is not exposed with every task class")
        assert actual["confirmed"] is True


def test_10_table_confirmation_survives_reads_and_does_not_reparse_words(replay):
    rule = routing.add_rule("Fix rounds use Opus in this quoted historical example.", via="app")
    table(replay)
    routing.edit_rule(rule["n"], "The owner can change these words independently.", via="app")
    for _ in range(2):
        expect((replay.view().get("tier_table") or {}).get("tiers"), TIERS,
               "10: confirmed table disappeared or was inferred again from owner prose")
        expect(dispatch_lines(hook(payload())), [LINE], "10: confirmed table did not govern dispatch")


def test_10_owner_confirmed_table_is_authority_not_a_hardcoded_default(replay):
    custom = dict(TIERS, **{"fix-round": "opus"})
    table(replay, tiers=custom)
    expect((replay.pull().get("tier_table") or {}).get("tiers"), custom,
           "10: owner-confirmed table values were not retained")
    assert dispatch_lines(hook(payload())) == []
    lines = dispatch_lines(hook(payload("sonnet")))
    expect(len(lines), 1, "10: owner-confirmed tier override was ignored")
    assert "Opus" in lines[0] and "Sonnet" in lines[0]


@pytest.mark.parametrize("replacement", [
    {"confirmed": False}, {"written_by": AUTHOR},
    {"tiers": {"fix-round": "invented-tier"}},
], ids=["unconfirmed", "conversation", "invalid-tier"])
def test_10_untrusted_table_cannot_replace_confirmed_owner_table(replay, replacement):
    table(replay)
    replay.write({"type": "tier_table_set", "tiers": dict(TIERS, **{"fix-round": "opus"}),
                  "confirmed": True, "written_by": {"kind": "owner"}, "via": "app", **replacement})
    expect((replay.view().get("tier_table") or {}).get("tiers"), TIERS,
           "10: valid confirmed table was not retained after untrusted replacement")
    expect(dispatch_lines(hook(payload())), [LINE], "10: replacement changed confirmed dispatch policy")


@pytest.mark.parametrize("call", [
    payload(),
    dict(payload(), tool_name="Task"),
    dict(payload(), tool_name="spawn_agent", tool_input={"model": "opus", "message": "task: fix-round\nRepair it."}),
    dict(payload(), tool_name="Bash", tool_input={"command": "claude --model opus -p 'task: fix-round\nRepair it.'"}),
    dict(payload(), tool_name="Bash", tool_input={"command": "claude -m opus -p 'task: fix-round\nRepair it.'"}),
    dict(payload(), tool_name="Bash", tool_input={"command": "claude --model=opus -p 'task: fix-round\nRepair it.'"}),
], ids=["Agent", "Task", "native-child", "long-model", "short-model", "equals-model"])
def test_10_opus_fix_round_advisory_on_each_dispatch_shape(replay, call):
    table(replay)
    expect(dispatch_lines(hook(call)), [LINE], "10: Opus fix-round result lacks the exact rule 12 line")


@pytest.mark.parametrize("task,tier", list(TIERS.items()))
def test_10_each_task_class_checks_the_expected_tier(replay, task, tier):
    table(replay)
    wrong = "sonnet" if tier == "opus" else "opus"
    lines = dispatch_lines(hook(payload(wrong, f"task: {task}\nDo the synthetic job.")))
    expect(len(lines), 1, f"10: {task} mismatch has no single advisory")
    assert "12" in lines[0] and tier.title() in lines[0] and wrong.title() in lines[0]
    assert "Say why if intended." in lines[0]
    assert dispatch_lines(hook(payload(tier, f"task: {task}\nDo the synthetic job."))) == []


@pytest.mark.parametrize("task,tier", list(TIERS.items()))
def test_10_matching_tier_gets_no_dispatch_advisory(replay, task, tier):
    table(replay)
    assert dispatch_lines(hook(payload(tier, f"task: {task}\nDo the synthetic job."))) == []


@pytest.mark.parametrize("model", ["opus", "claude-opus-4-6", "claude-opus-4-6-20260101"])
def test_10_model_id_resolves_to_tier(replay, model):
    table(replay)
    expect(dispatch_lines(hook(payload(model))), [LINE], "10: Opus model id has no fix-round advisory")


def test_10_model_from_fake_config_home_without_credentials(replay):
    table(replay)
    home = replay.root / "fake-opus-home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"model": "opus"}))
    call = dict(payload(), tool_name="Bash", tool_input={"command":
        f"CLAUDE_CONFIG_DIR={shlex.quote(str(home))} claude -p 'task: fix-round\nRepair it.'"})
    expect(dispatch_lines(hook(call)), [LINE], "10: local settings model is not used for tier check")
    call["tool_input"]["command"] = (
        f"CLAUDE_CONFIG_DIR={shlex.quote(str(home))} claude --model sonnet -p 'task: fix-round\nRepair it.'")
    assert dispatch_lines(hook(call)) == []


def test_10_explicit_task_class_wins_over_misleading_keywords(replay):
    table(replay)
    expect(dispatch_lines(hook(payload(brief="task: fix-round\nScope, design, ruling, mock: repair their tests."))),
           [LINE], "10: explicit fix-round class lost to keywords in the brief")
    assert dispatch_lines(hook(payload(brief="task: scope\nPlan the fix-round tests."))) == []


def test_10_keyword_fallback_is_explicitly_marked_as_guess(replay):
    table(replay)
    lines = dispatch_lines(hook(payload(brief="Fix round: repair the broken synthetic fixture.")))
    expect(len(lines), 1, "10: keyword fix-round guess is not reported")
    assert "guess" in lines[0].lower() and "Sonnet" in lines[0] and "Opus" in lines[0]


@pytest.mark.parametrize("confirmed,author", [(False, None), (True, AUTHOR)])
def test_10_unconfirmed_or_conversation_table_never_becomes_authority(replay, confirmed, author):
    table(replay, confirmed=confirmed, author=author)
    assert dispatch_lines(hook(payload())) == []


@pytest.mark.parametrize("model", ["sonnet", "haiku", "gpt-6.1-sol", "", "unrecognized-model"])
def test_10_no_spurious_opus_fix_round_line(replay, model):
    table(replay)
    assert LINE not in dispatch_lines(hook(payload(model)))


def test_10_sonnet_fix_round_receives_no_extra_context(replay):
    table(replay)
    control = hook(payload("sonnet", "A synthetic job without class keywords."))
    assert hook(payload("sonnet")) == control


def test_10_hook_main_errors_still_print_empty_object_and_exit_successfully(replay, monkeypatch, capsys):
    import io
    import sys

    table(replay)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload())))
    def broken(*args, **kwargs):
        raise RuntimeError("synthetic advisory failure")
    monkeypatch.setattr(routing_delegation, "pre_tool_use", broken)
    assert routing_delegation.main() is None
    assert json.loads(capsys.readouterr().out) == {}


def flags(replay, field, surface):
    if surface == "mcp":
        body = replay.pull()
    elif surface == "doctor":
        body = routing.doctor_block()
    else:
        body = replay.view()
    actual = body.get(field, [])
    assert isinstance(actual, list), actual
    return actual


@pytest.mark.parametrize("surface", ["view", "mcp", "doctor"])
@pytest.mark.parametrize("source,state", [("priority", "held"), ("rule", "held"),
                                          ("priority", "paused"), ("rule", "paused"),
                                          ("priority", "down"), ("priority", "untracked")])
def test_10_structured_contradictions_reference_account_and_source(replay, surface, source, state):
    if source == "priority":
        changed = routing.set_priority([{"account": "Codex", "role": "Use Codex for synthetic builds."}],
                                       via="app", expect=routing.priority_version())
    else:
        changed = routing.add_rule("Use Codex for synthetic builds.", via="app")
    if state == "held":
        held = routing.add_hold("Wait.", reason="other", scope="account", account="Codex", owner=True,
                                via="app", hold_id="h-synthetic")
    elif state == "paused":
        routing.pause("Codex", via="app")
    elif state == "down":
        routing.report(account="Codex", state="down", author=AUTHOR)
    else:
        replay.configure(usage_accounts=[a for a in replay.config["usage_accounts"] if a["harness"] != "codex"])
    view = replay.view()
    if state == "held":
        assert any(h["id"] == held["id"] and h["scope"]["account"] == "codex|" for h in view["holds"])
    elif state == "untracked":
        assert view["priority"]["entries"][0]["in_settings"] is False
    else:
        assert next(a for a in view["accounts"] if a["harness"] == "codex")["state"] == state
    found = flags(replay, "contradictions", surface)
    matching = [f for f in found if f.get("account") == "codex:main" and f.get("state") == state
                and f.get("source") == source]
    expect(len(matching), 1, f"10: {source}/{state} contradiction is not structured on {surface}")
    assert changed["id"] in matching[0]["entities"]
    assert matching[0]["text"] and matching[0]["advisory"] is True


def test_10_opposite_structured_directions_do_not_guess_from_prose(replay):
    replay.write(*[{"type": "direction_set", "id": f"direction-{value}", "account": "codex:main",
                    "topic": "dispatch", "value": value, "written_by": {"kind": "owner"}}
                   for value in ("use", "avoid")])
    found = flags(replay, "contradictions", "mcp")
    matching = [f for f in found if f.get("kind") == "opposite_directions" and f.get("account") == "codex:main"]
    expect(len(matching), 1, "10: opposite structured directions lack a contradiction")
    assert set(matching[0]["entities"]) == {"direction-use", "direction-avoid"}
    assert matching[0]["advisory"] is True


def test_10_directions_need_same_account_topic_and_opposite_values(replay):
    replay.write(*[{"type": "direction_set", "account": account, "topic": topic, "value": value,
                    "written_by": {"kind": "owner"}}
                   for account, topic, value in [("codex:main", "dispatch", "use"),
                                                  ("codex:main", "dispatch", "use"),
                                                  ("codex:main", "review", "avoid"),
                                                  ("grok:main", "dispatch", "avoid")]])
    assert not any(f.get("kind") == "opposite_directions" for f in flags(replay, "contradictions", "mcp"))


def test_10_ending_hold_clears_contradiction_without_changing_rule(replay):
    words = "Use Codex for synthetic builds."
    rule = routing.add_rule(words, via="app")
    hold = routing.add_hold("Wait.", reason="other", scope="account", account="Codex", owner=True, via="app")
    expect(any(f.get("account") == "codex:main" and f.get("state") == "held"
               for f in flags(replay, "contradictions", "mcp")), True,
           "10: held account has no contradiction before ending hold")
    routing.end_hold(hold["id"], owner=True, via="app")
    assert not any(f.get("account") == "codex:main" for f in flags(replay, "contradictions", "mcp"))
    assert next(r for r in replay.view()["rules"] if r["entity"] == rule["entity"])["text"] == words
    hook(payload())


@pytest.mark.parametrize("surface", ["view", "mcp", "doctor"])
@pytest.mark.parametrize("words", ["Use Codex until Saturday.", "Use Codex this week.",
                                   "Use Codex on Friday.", "Use Codex until 2026-10-24."])
def test_10_time_wording_suggests_priority_role_with_until(replay, surface, words):
    rule = routing.add_rule(words, via="app")
    found = flags(replay, "rule_hygiene", surface)
    matching = [f for f in found if f.get("kind") == "move_to_role" and rule["entity"] in f.get("entities", [])]
    expect(len(matching), 1, "10: time-worded standing rule lacks move-to-role suggestion")
    assert matching[0]["account"] == "codex:main"
    assert "priority" in matching[0]["text"].lower() and "until" in matching[0]["text"].lower()
    assert matching[0]["advisory"] is True


@pytest.mark.parametrize("words", ["Use Codex until 2026-10-19.", "Use Codex until Monday."])
def test_10_passed_rule_deadline_is_expired(replay, words):
    # Written before Monday; checked on Tuesday. A bare weekday is therefore
    # the already-passed deadline, not next week's freshly inferred deadline.
    replay.advance(-4 * 86400)
    rule = routing.add_rule(words, via="app")
    replay.advance(4 * 86400)
    found = flags(replay, "rule_hygiene", "mcp")
    expect(any(f.get("kind") == "expired" and rule["entity"] in f.get("entities", []) for f in found),
           True, "10: passed rule deadline has no expired flag")


def test_10_future_deadline_is_not_expired_and_retired_rules_are_not_flagged(replay):
    future_rule = routing.add_rule("Use Codex until 2026-10-24.", via="app")
    retired = routing.add_rule("Use Codex until 2026-10-19.", via="app")
    routing.retire_rule(retired["n"], via="app")
    for surface in ("view", "mcp", "doctor"):
        found = flags(replay, "rule_hygiene", surface)
        assert not any(retired["entity"] in f.get("entities", []) for f in found)
        assert not any(f.get("kind") == "expired" and future_rule["entity"] in f.get("entities", []) for f in found)


def test_10_named_reset_expiry_uses_account_reset(replay):
    replay.usage(reset=replay.now.replace(hour=15))
    rule = routing.add_rule("Use Codex until its weekly reset.", via="app")
    replay.advance(2 * 3600)
    found = flags(replay, "rule_hygiene", "mcp")
    expect(any(f.get("kind") == "expired" and rule["entity"] in f.get("entities", []) for f in found),
           True, "10: passed named weekly reset has no expired flag")


def test_10_rule_overlap_names_both_rules_and_excludes_other_topics(replay):
    a = routing.add_rule("Codex handles synthetic test writing.", via="app")
    b = routing.add_rule("Use Codex for writing synthetic tests.", via="app")
    other = routing.add_rule("Codex may gather synthetic inventory counts.", via="app")
    found = flags(replay, "rule_hygiene", "mcp")
    pairs = [set(f.get("entities", [])) for f in found if f.get("kind") == "overlap"]
    expect({a["entity"], b["entity"]} in pairs, True, "10: same account/topic rules have no overlap flag")
    assert all(other["entity"] not in pair for pair in pairs)


def test_10_priority_role_restatement_names_rule_and_role(replay):
    words = "Codex handles synthetic test writing."
    rule = routing.add_rule(words, via="app")
    priority = routing.set_priority([{"account": "Codex", "role": words}], via="app",
                                    expect=routing.priority_version())
    found = flags(replay, "rule_hygiene", "mcp")
    expect(any(f.get("kind") == "role_restates_rule" and f.get("account") == "codex:main"
               and set(f.get("entities", [])) == {rule["entity"], priority["id"]} for f in found),
           True, "10: priority role restating a rule has no flag with both references")


@pytest.mark.parametrize("age", [24 * 3600 + 1, 25 * 3600], ids=["just-over-24h", "25h"])
def test_10_old_pending_edit_lists_owner_confirmation_and_drop_directions(replay, age, monkeypatch):
    rule = routing.add_rule("Codex handles synthetic tests.", via="app")
    monkeypatch.setenv("KHIPU_SESSION_ID", "s1")
    shown = routing.conversation_view()
    routing.request_rule("Codex handles synthetic docs.", n=rule["n"], author=AUTHOR,
                         owner_request="Change this synthetic rule.",
                         base=shown["rules"][0]["owner_revision"])
    replay.advance(age)
    found = flags(replay, "rule_hygiene", "mcp")
    matching = [f for f in found if f.get("kind") == "long_pending" and rule["entity"] in f.get("entities", [])]
    expect(len(matching), 1, "10: old pending edit lacks its own long-pending flag")
    flag = matching[0]
    assert "owner" in flag["text"].lower() and flag["advisory"] is True
    assert flag["owner_direction"]["confirm"] and flag["owner_direction"]["drop"]
    assert all("\n" not in direction and str(rule["n"]) in direction
               for direction in flag["owner_direction"].values())
    assert "confirm" in flag["owner_direction"]["confirm"].lower()
    assert any(word in flag["owner_direction"]["drop"].lower() for word in ("undo", "drop", "dismiss"))


@pytest.mark.parametrize("age", [0, 24 * 3600])
def test_10_pending_edit_not_old_until_strictly_over_24_hours(replay, age, monkeypatch):
    rule = routing.add_rule("Codex handles synthetic tests.", via="app")
    monkeypatch.setenv("KHIPU_SESSION_ID", "s1")
    shown = routing.conversation_view()
    routing.request_rule("Codex handles synthetic docs.", n=rule["n"], author=AUTHOR,
                         owner_request="Change this synthetic rule.",
                         base=shown["rules"][0]["owner_revision"])
    replay.advance(age)
    assert not any(f.get("kind") == "long_pending" for f in flags(replay, "rule_hygiene", "mcp"))


def test_10_confirming_old_edit_clears_only_its_long_pending_flag(replay, monkeypatch):
    rule = routing.add_rule("Codex handles synthetic tests.", via="app")
    monkeypatch.setenv("KHIPU_SESSION_ID", "s1")
    shown = routing.conversation_view()
    routing.request_rule("Codex handles synthetic docs.", n=rule["n"], author=AUTHOR,
                         owner_request="Change this synthetic rule.",
                         base=shown["rules"][0]["owner_revision"])
    replay.advance(25 * 3600)
    expect(any(f.get("kind") == "long_pending" for f in flags(replay, "rule_hygiene", "mcp")),
           True, "10: old edit has no pending flag before confirmation")
    routing.confirm_rule(rule["n"], via="app")
    assert not any(f.get("kind") == "long_pending" for f in flags(replay, "rule_hygiene", "mcp"))


def test_10_advisory_reads_preserve_owner_bytes_and_log_order(replay):
    words = "  Codex rôle — synthetic tests.\n  Preserve\tall whitespace.  "
    routing.add_rule(words, via="app")
    routing.set_note(words, via="app")
    routing.set_priority([{"account": "Codex", "role": words}], via="app", expect=routing.priority_version())
    table(replay)
    before = routing.events_file().read_bytes()
    for _ in range(2):
        view = replay.view()
        assert view["rules"][0]["text"].encode() == words.encode()
        assert view["note"]["text"].encode() == words.encode()
        assert view["priority"]["entries"][0]["role"].encode() == words.encode()
        replay.pull()
        routing.doctor_block()
        hook(payload())
        assert routing.events_file().read_bytes() == before


def test_10_clean_rules_and_open_priority_have_no_false_flags(replay):
    routing.add_rule("Codex handles synthetic test writing.", via="app")
    routing.add_rule("Grok gathers synthetic inventory counts.", via="app")
    routing.report(account="Codex", state="open", author=AUTHOR)
    routing.set_priority([{"account": "Codex", "role": "Gather synthetic inventory counts."}], via="app",
                         expect=routing.priority_version())
    for surface in ("view", "mcp", "doctor"):
        assert flags(replay, "contradictions", surface) == []
        assert flags(replay, "rule_hygiene", surface) == []
