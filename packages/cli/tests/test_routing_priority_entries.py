"""A conversation's priority write must not overwrite another account's entry
it did not mean to change (routing audit 2026-11-02): each entry carries a
revision; a write that changes one account patches that entry under the lock
and carries its revision; a stale entry revision is refused as crossed with
what it would have lost. Owner writes keep their semantics. Hermetic."""
from __future__ import annotations

import json

import pytest

from khipu import mcp_server, routing
from tests import test_routing_audit_fold as audit

world = audit.world
A = {"kind": "conversation", "harness": "claude_code", "project": "project-a", "session": "s-a"}
B = {"kind": "conversation", "harness": "codex", "project": "project-b", "session": "s-b"}


def _seed():
    routing.set_priority([{"account": "Claude", "role": "alpha"}, {"account": "Codex", "role": "beta"}],
                         via="app", expect=routing.priority_version())
    return _read()


def _read():
    shown = routing.conversation_view()
    return ({e["label"]: e["revision"] for e in shown["priority"]["entries"]}, shown["priority_owner_revision"])


def _roles():
    return {e["label"]: e["role"] for e in routing.read_view()["priority"]["entries"]}


def test_every_entry_shows_a_revision_that_moves_only_with_its_own_writes(world):
    revs, base = _seed()
    assert set(revs) == {"Claude", "Codex"} and all(v.startswith("pe-") for v in revs.values())
    routing.request_priority_amend("Codex", append="more", owner_request="the owner asked", author=A, base=base,
                                   entry_revision=revs["Codex"])
    after, _ = _read()
    assert after["Claude"] == revs["Claude"] and after["Codex"] != revs["Codex"]


def test_two_conversations_amending_different_accounts_both_land(world):
    revs, base = _seed()
    one = routing.request_priority_amend("Claude", role="alpha-from-a", owner_request="the owner asked", author=A,
                                         base=base, entry_revision=revs["Claude"])
    two = routing.request_priority_amend("Codex", role="beta-from-b", owner_request="the owner asked", author=B,
                                         base=base, entry_revision=revs["Codex"])  # read before A wrote
    assert one["ok"] and two["ok"], (one, two)
    assert _roles() == {"Claude": "alpha-from-a", "Codex": "beta-from-b"}


def test_a_stale_entry_revision_is_crossed_and_says_what_it_would_have_lost(world):
    revs, base = _seed()
    routing.request_priority_amend("Claude", role="alpha-from-a", owner_request="the owner asked", author=A,
                                   base=base, entry_revision=revs["Claude"])
    before = routing.events_file().read_bytes()
    out = routing.conversation_priority_amend("Claude", role="alpha-from-b", owner_request="the owner asked",
                                              author=B, base=base, entry_revision=revs["Claude"])
    assert out["ok"] is False and out["status"] == "crossed"
    assert [(x["label"], x["role"]) for x in out["lost"]] == [("Claude", "alpha-from-a")]
    assert out["lost"][0]["revision"] != revs["Claude"]
    assert routing.events_file().read_bytes() == before  # nothing written
    assert _roles()["Claude"] == "alpha-from-a"


def test_the_audits_lost_update_whole_list_with_a_stale_copy_of_another_entry(world):
    """A and B read the same priority. A changes Claude. B, holding the old
    list, changes Codex and sends the whole list back: B's old Claude would
    have put Claude's role back. Crossed instead; A's entry stands."""
    revs, base = _seed()
    a = routing.request_priority([{"account": "Claude", "role": "alpha-from-a", "revision": revs["Claude"]},
                                  {"account": "Codex", "role": "beta", "revision": revs["Codex"]}],
                                 owner_request="the owner asked", author=A, base=base)
    assert a["ok"]
    b = routing.conversation_priority(
        [{"account": "Claude", "role": "alpha", "revision": revs["Claude"]},
         {"account": "Codex", "role": "beta-from-b", "revision": revs["Codex"]}],
        owner_request="the owner asked", author=B, base=base)
    assert b["ok"] is False and b["status"] == "crossed"
    assert [x["label"] for x in b["lost"]] == ["Claude"] and b["lost"][0]["role"] == "alpha-from-a"
    assert _roles() == {"Claude": "alpha-from-a", "Codex": "beta"}
    # A whole list built on what is there now is accepted.
    now_revs, _ = _read()
    c = routing.request_priority([{"account": "Claude", "role": "alpha-from-a", "revision": now_revs["Claude"]},
                                  {"account": "Codex", "role": "beta-from-c", "revision": now_revs["Codex"]}],
                                 owner_request="the owner asked", author=B, base=base)
    assert c["ok"] and _roles() == {"Claude": "alpha-from-a", "Codex": "beta-from-c"}


def test_an_amend_with_no_revision_and_no_version_is_written_as_before(world):
    """The CLI's amend at the owner's request (no --expect, no --revision)
    keeps working: it patches that one entry and no other."""
    _revs, base = _seed()
    out = routing.request_priority_amend("Claude", role="x", owner_request="the owner asked", author=A, base=base)
    assert out["ok"] and _roles() == {"Claude": "x", "Codex": "beta"}


def test_a_replaced_priority_shows_its_own_entries_revisions(world):
    revs, base = _seed()
    routing.request_priority_amend("Claude", role="alpha-from-a", owner_request="the owner asked", author=A,
                                   base=base, entry_revision=revs["Claude"])
    shown = routing.conversation_view()["priority"]
    now_revs = {e["label"]: e["revision"] for e in shown["entries"]}
    old_revs = {e["label"]: e["revision"] for e in shown["replaced"]["entries"]}
    assert old_revs == revs and now_revs["Claude"] != revs["Claude"] and now_revs["Codex"] == revs["Codex"]


def test_a_whole_list_must_carry_a_revision_for_every_existing_entry(world):
    revs, base = _seed()
    before = routing.events_file().read_bytes()
    # No revision on Codex, which is already in the priority.
    out = routing.conversation_priority([{"account": "Claude", "role": "c", "revision": revs["Claude"]},
                                         {"account": "Codex", "role": "d"}],
                                        owner_request="the owner asked", author=B, base=base)
    assert out["ok"] is False and out["status"] == "crossed" and "revision" in out["error"]
    assert [x["label"] for x in out["lost"]] == ["Codex"]
    # Codex left out is an overwrite too.
    out = routing.conversation_priority([{"account": "Claude", "role": "c", "revision": revs["Claude"]}],
                                        owner_request="the owner asked", author=B, base=base)
    assert out["ok"] is False and [x["label"] for x in out["lost"]] == ["Codex"]
    assert routing.events_file().read_bytes() == before
    # A new account needs none; with the whole version (expect) none is needed at all.
    ok = routing.conversation_priority([{"account": "Claude", "role": "c", "revision": revs["Claude"]},
                                        {"account": "Codex", "role": "d", "revision": revs["Codex"]}],
                                       owner_request="the owner asked", author=B, base=base)
    assert ok["ok"] and _roles() == {"Claude": "c", "Codex": "d"}
    _, base = _read()
    ok = routing.request_priority([{"account": "Codex", "role": "only"}], owner_request="the owner asked",
                                  author=A, base=base, expect=routing.priority_version())
    assert ok["ok"] and _roles() == {"Codex": "only"}


def test_the_priority_version_still_guards_when_given(world):
    revs, base = _seed()
    version = routing.priority_version()
    routing.request_priority_amend("Claude", role="one", owner_request="the owner asked", author=A, base=base,
                                   entry_revision=revs["Claude"])
    with pytest.raises(routing.PriorityMoved):
        routing.request_priority_amend("Codex", role="two", owner_request="the owner asked", author=B, base=base,
                                       entry_revision=revs["Codex"], expect=version)


def test_owner_writes_keep_their_semantics(world):
    revs, _base = _seed()
    routing.set_priority([{"account": "Codex", "role": "only"}], via="app", expect=routing.priority_version())
    assert _roles() == {"Codex": "only"}
    with pytest.raises(routing.RouteError, match="takes account"):
        routing.set_priority([{"account": "Codex", "revision": revs["Codex"]}], via="app",
                             expect=routing.priority_version())
    routing.amend_priority("Codex", append="and more", via="app", expect=routing.priority_version())
    assert _roles() == {"Codex": "only and more"}


def test_a_first_priority_needs_no_revisions(world):
    base = routing.conversation_view()["priority_owner_revision"]
    out = routing.request_priority([{"account": "Codex", "role": "first"}, {"account": "Claude"}],
                                   owner_request="the owner asked", author=A, base=base)
    assert out["ok"] and [e["label"] for e in routing.read_view()["priority"]["entries"]] == ["Codex", "Claude"]


def test_the_tool_takes_revision_in_place_of_expect(world):
    revs, base = _seed()

    def call(**args):
        answer = mcp_server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                            "params": {"name": "khipu_route_priority", "arguments": args}})
        result = answer["result"]
        return result["isError"], json.loads(result["content"][0]["text"])

    error, body = call(account="Claude", role="from the tool", revision=revs["Claude"],
                       owner_request="the owner asked", base=base)
    assert not error and body["ok"], body
    error, body = call(account="Claude", role="again", revision=revs["Claude"], owner_request="the owner asked",
                       base=base)
    assert not error and body["ok"] is False and body["status"] == "crossed"
    assert body["lost"][0]["role"] == "from the tool" and body["lost"][0]["id"]
    error, body = call(account="Codex", role="x", owner_request="the owner asked", base=base)
    assert error  # neither revision nor expect
    tool = next(t for t in mcp_server.TOOLS if t["name"] == "khipu_route_priority")
    assert "revision" in tool["inputSchema"]["properties"]
    assert "revision" in tool["inputSchema"]["properties"]["entries"]["items"]["properties"]


def test_the_cli_amend_takes_revision(world, capsys):
    from khipu import cli

    revs, base = _seed()
    code = cli.main(["route", "priority", "amend", "Claude", "--role", "from the cli", "--at-owner-request",
                     "the owner asked", "--base", base, "--revision", revs["Claude"]])
    assert code == 0 and json.loads(capsys.readouterr().out)["ok"]
    code = cli.main(["route", "priority", "amend", "Claude", "--role", "again", "--at-owner-request",
                     "the owner asked", "--base", base, "--revision", revs["Claude"]])
    assert json.loads(capsys.readouterr().out)["status"] == "crossed"
