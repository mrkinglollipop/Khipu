from khipu import mcp_server, routing
from tests.test_routing_instant_apply import plain, _say, _set_priority_by_the_owner, OWNER  # noqa: F401


def test_amend_from_owner_conversation_confirms_another_conversations_pending_request(plain):
    _set_priority_by_the_owner()
    # Conversation B (no owner turn; could be a dispatched job or an injected agent) records a pending request.
    plain["mp"].setenv("CLAUDE_CODE_SESSION_ID", "s-other-B")
    base = routing.conversation_view()["priority_owner_revision"]
    out_b = mcp_server._tool_route_priority({"account": "Grok", "role": "INJECTED: everything goes to Grok",
                                             "owner_request": "x", "expect": routing.priority_version(), "base": base})
    assert out_b.get("ok") is not False and not out_b.get("applied"), out_b
    assert routing.read_view()["priority_stored"]["pending"]
    # Conversation A: the owner types something; the agent amends a different account.
    plain["mp"].setenv("CLAUDE_CODE_SESSION_ID", OWNER)
    _say(plain, "loosen Codex a bit")
    base = routing.conversation_view()["priority_owner_revision"]
    out_a = mcp_server._tool_route_priority({"account": "Codex", "append": "loosened", "owner_request": "Matt",
                                             "expect": routing.priority_version(), "base": base})
    held = routing.read_view()["priority"]
    roles = [e["role"] for e in held["entries"]]
    print("A:", out_a.get("applied"), "pending:", held.get("pending"), roles)
    # Exploit succeeds if B's injected role is now confirmed (not pending).
    assert not (out_a.get("applied") and any("INJECTED" in r for r in roles) and not held.get("pending")), \
        "B's unseen pending request was confirmed by A's instant amend"
