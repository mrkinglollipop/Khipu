"""Slice 5 author notices share slice 6's hook leases and page receipts."""
from khipu import agent_view, mcp_server, recall_prompt, routing, routing_delivery
from tests.test_routing_s5_fix3 import delivered, home  # noqa: F401


def seed_notices(home, monkeypatch, count=8):
    monkeypatch.setenv("KHIPU_SESSION_ID", "author-a")
    routing.add_rule("OWNER", via="app")
    base = mcp_server._tool_route({"full": True})["rules"][0]["owner_revision"]
    for index in range(count):
        routing.request_rule(f"PROPOSAL {index}", n=1, base=base)
    return {p["id"] for p in routing.read_view()["proposals"].values() if p["status"] == "replaced"}


def offer(session="author-a"):
    agent_view.prepare_routing_table(refresh=True)
    return routing.prompt_push(recall_prompt.routing_marker(session), budget=2, session_id=session)

