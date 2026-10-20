"""Fix round 2: Undo choices, route-rule choices, and changed-log refolds."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from khipu import mcp_server, owner_act, owner_direction, routing, routing_events
from tests.routing_s4_cost_workload import T0, build

pytest_plugins = ("tests.test_routing_acceptance", "tests.test_owner_direction")


@pytest.fixture
def direction_world(world):
    return world


def _priority(label, role):
    return [{"account": label, "role": role}]


def _set_then_hold(direction_world):
    routing.set_priority(_priority("Grok", "old"), via="app", expect=routing.priority_version())
    hold = routing.add_hold("wait for Grok", scope="account", account="Grok", reason="keep", owner=True, via="app")
    direction_world.say("put Codex first")
    code, applied = direction_world.run(["route", "priority", "set", "--entries=" + json.dumps(_priority("Codex", "new")),
                                         "--expect=" + routing.priority_version()], True)
    assert code == 0, applied
    return hold, applied


def test_direction_undo_keeps_a_held_account_the_action_never_named(direction_world):
    hold, applied = _set_then_hold(direction_world)
    direction_world.say("undo that")
    code, undone = direction_world.run(["owner-direction", "undo", applied["owner_action"]], True)
    assert code == 0, undone
    view = routing.read_view()
    assert view["priority"]["entries"][0]["account"] == "grok|"
    assert view["holds"][0]["id"] == hold["id"]
    inverse = routing.read_events()[0][-1]
    assert inverse["holds"][hold["id"]] == "keep"  # an unnamed current hold defaults to keep


def test_direction_undo_accepts_an_explicit_end_choice(direction_world):
    hold, applied = _set_then_hold(direction_world)
    direction_world.say("undo it and end Grok's hold")
    code, undone = direction_world.run(["owner-direction", "undo", applied["owner_action"],
                                         "--holds=" + json.dumps({hold["id"]: "end"})], True)
    assert code == 0, undone
    assert not routing.read_view()["holds"]
    undo_line = next(row for row in owner_direction.read_log() if row.get("kind") == "undo")
    assert undo_line["facts"]["holds"] == {hold["id"]: "end"}


def test_owner_act_undo_keeps_a_hold_added_after_the_action_without_an_explicit_choice(direction_world):
    routing.set_priority(_priority("Grok", "old"), via="app", expect=routing.priority_version())
    direction_world.say("put Grok as new")
    ok, applied = owner_act.act("route.priority.set", {"entries": _priority("Grok", "new"),
                                                         "expect": routing.priority_version()})
    assert ok, applied
    hold = routing.add_hold("wait for Grok", scope="account", account="Grok", reason="keep", owner=True, via="app")
    direction_world.say("undo that")
    ok, undone = owner_act.act("direction.undo", {"id": applied["owner_action"]})
    assert ok, undone
    assert routing.read_view()["holds"][0]["id"] == hold["id"]


def test_owner_act_undo_accepts_an_explicit_keep_for_a_hold_added_after_the_action(direction_world):
    routing.set_priority(_priority("Grok", "old"), via="app", expect=routing.priority_version())
    direction_world.say("put Grok as new")
    ok, applied = owner_act.act("route.priority.set", {"entries": _priority("Grok", "new"),
                                                         "expect": routing.priority_version()})
    assert ok, applied
    hold = routing.add_hold("wait for Grok", scope="account", account="Grok", reason="keep", owner=True, via="app")
    direction_world.say("undo that and keep the new hold")
    ok, undone = owner_act.act("direction.undo", {"id": applied["owner_action"], "holds": {hold["id"]: "keep"}})
    assert ok, undone
    assert routing.read_view()["holds"][0]["id"] == hold["id"]


def test_undo_defaults_a_hold_named_only_in_the_restored_priority_role(direction_world):
    routing.set_priority(_priority("Codex", "Grok fallback"), via="app", expect=routing.priority_version())
    direction_world.say("put Codex first")
    ok, applied = owner_act.act("route.priority.set", {"entries": _priority("Codex", "first"),
                                                         "expect": routing.priority_version()})
    assert ok, applied
    hold = routing.add_hold("wait for Grok", scope="account", account="Grok", reason="keep", owner=True, via="app")
    direction_world.say("undo that")
    ok, undone = owner_act.act("direction.undo", {"id": applied["owner_action"]})
    assert ok, undone
    assert routing.read_view()["holds"][0]["id"] == hold["id"]


def test_undo_of_a_restored_rule_uses_its_two_argument_inverse(direction_world):
    routing.add_rule("Grok builds after a re-check.", via="app")
    direction_world.say("retire that rule")
    ok, retired = owner_act.act("route.rule.retire", {"rule": 1})
    assert ok, retired
    direction_world.say("restore that rule")
    ok, restored = owner_act.act("route.rule.restore", {"rule": 1})
    assert ok, restored
    direction_world.say("undo that")
    ok, undone = owner_act.act("direction.undo", {"id": restored["owner_action"]})
    assert ok, undone
    assert not routing.read_view()["rules"]


def test_route_rule_mcp_schema_and_write_accept_hold_choices(replay):
    tool = next(item for item in mcp_server.TOOLS if item["name"] == "khipu_route_rule")
    assert tool["inputSchema"]["properties"]["holds"]["additionalProperties"]["enum"] == ["end", "keep"]
    error, body = replay.call("khipu_route_rule", text="Grok builds only with a re-check.",
                              owner_request="The owner asked for this rule.", holds={"hold-grok": "keep"})
    assert not error, body
    assert routing.read_events()[0][-1]["holds"] == {"hold-grok": "keep"}


def test_legacy_revision_reusing_this_fold_salt_is_byte_identical(replay):
    event = {"type": "evidence", "id": "legacy", "account": "grok|", "state": "open", "source": "report",
             "written_at": routing._iso(T0), "written_by": {"kind": "conversation", "harness": "codex",
             "project": "p", "session": "s"}, "via": "mcp"}
    salt = routing._id_salt(operations=True)
    assert routing_events.revision(event) == routing_events.revision(event, operations_salt=salt)
    assert routing._in_write_order([event])[0]["_revision"] == routing_events.revision(event)


def _raw_evidence(event_id):
    return {"type": "evidence", "id": event_id, "account": "grok|", "state": "open", "source": "report",
            "written_at": routing._iso(T0), "written_by": {"kind": "conversation", "harness": "codex",
            "project": "p", "session": "s"}, "via": "mcp"}


def test_parsed_line_cache_parses_only_complete_appended_lines(replay, monkeypatch):
    path = routing.events_file()
    path.parent.mkdir(parents=True)
    path.write_bytes((json.dumps(_raw_evidence("one")) + "\n").encode())
    monkeypatch.setattr(routing, "_PARSED_LINE_CACHE", {})
    parse, calls = routing._line_event, []
    monkeypatch.setattr(routing, "_line_event", lambda *args, **kwargs: (calls.append(args[0]), parse(*args, **kwargs))[1])
    assert [row["id"] for row in routing._read_event_lines()[0]] == ["one"]
    with path.open("ab") as stream:
        stream.write((json.dumps(_raw_evidence("two")) + "\n" + json.dumps(_raw_evidence("three")) + "\n").encode())
    assert [row["id"] for row in routing._read_event_lines()[0]] == ["one", "two", "three"]
    assert len(calls) == 3


def test_parsed_line_cache_reparses_a_replaced_prefix(replay, monkeypatch):
    path = routing.events_file()
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(routing, "_PARSED_LINE_CACHE", {})
    parse, calls = routing._line_event, []
    monkeypatch.setattr(routing, "_line_event", lambda *args, **kwargs: (calls.append(args[0]), parse(*args, **kwargs))[1])
    path.write_bytes((json.dumps(_raw_evidence("one")) + "\n").encode())
    assert routing._read_event_lines()[1] == 0
    path.write_bytes((json.dumps(_raw_evidence("two")) + "\n").encode())
    assert [row["id"] for row in routing._read_event_lines()[0]] == ["two"]
    assert len(calls) == 2


def test_parsed_line_cache_reparses_an_incomplete_final_line_without_rewriting_it(replay, monkeypatch):
    path = routing.events_file()
    path.parent.mkdir(parents=True)
    partial = json.dumps(_raw_evidence("one")).encode()[:-1]
    monkeypatch.setattr(routing, "_PARSED_LINE_CACHE", {})
    parse, calls = routing._line_event, []
    monkeypatch.setattr(routing, "_line_event", lambda *args, **kwargs: (calls.append(args[0]), parse(*args, **kwargs))[1])
    path.write_bytes(partial)
    assert routing._read_event_lines()[1] == 1
    with path.open("ab") as stream:
        stream.write(b"}\n")
    assert [row["id"] for row in routing._read_event_lines()[0]] == ["one"]
    assert path.read_bytes() == partial + b"}\n" and len(calls) == 2


@pytest.mark.parametrize("with_definition", [False, True])
def test_changed_log_prompt_with_source_append_reads_and_orders_once(replay, monkeypatch, with_definition):
    replay.configure(routing_stale_after=4)
    path = routing.events_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in build(20_000, routing)))
    if with_definition:
        routing.set_definition("account", {"id": "codex:main", "label": "Codex", "provider": "codex"}, via="app")
    reset = T0 + timedelta(days=6)
    replay.usage(witness=T0, reset=reset)
    view = replay.view()  # warm baseline and persist the first source observation
    marker = {"version": view["version"], "epoch": view["epoch"], "full_at": routing._iso(replay.now)}
    replay.advance(1)
    replay.usage(witness=replay.now, reset=reset)  # the same quota period has a strictly newer witness
    with path.open("a") as stream:
        stream.write(json.dumps({"type": "evidence", "id": "changed", "account": "grok|", "state": "open",
                                 "source": "report", "written_at": routing._iso(replay.now),
                                 "written_by": {"kind": "conversation", "harness": "codex", "project": "p", "session": "z"},
                                 "via": "mcp"}) + "\n")
    calls = {"read": 0, "order": 0}
    read, order = routing._read_event_lines, routing._in_write_order

    def counted_read(*args, **kwargs):
        calls["read"] += 1
        return read(*args, **kwargs)

    def counted_order(*args, **kwargs):
        calls["order"] += 1
        return order(*args, **kwargs)

    monkeypatch.setattr(routing, "_read_event_lines", counted_read)
    monkeypatch.setattr(routing, "_in_write_order", counted_order)
    replay.advance(1)
    replay.usage(witness=replay.now, reset=reset)
    pushed = routing.prompt_push(marker, budget=1.0, now=replay.now)
    monkeypatch.setattr(routing, "_read_event_lines", read)
    monkeypatch.setattr(routing, "_in_write_order", order)
    events = routing.read_events()[0]
    assert calls == {"read": 1, "order": 1}
    assert pushed["kind"] in {"changes", "full"} and pushed["version"] > marker["version"]
    renewals = [event for event in events if event["type"] == "observation_renew"]
    assert renewals and renewals[-1]["witness_at"] == routing._iso(replay.now)
    assert [event["seq"] for event in events if event.get("seq") is not None] == sorted(
        event["seq"] for event in events if event.get("seq") is not None)
