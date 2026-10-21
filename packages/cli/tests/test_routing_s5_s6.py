"""Slice 5 author notices share slice 6's hook leases and page receipts."""
from khipu import agent_view, mcp_server, recall_prompt, routing, routing_delivery
from tests.test_routing_s5_fix3 import delivered, home  # noqa: F401


def seed_notices(home, monkeypatch, count=8):
    monkeypatch.setenv("KHIPU_SESSION_ID", "author-a")
    routing.add_rule("OWNER", via="app")
    base = mcp_server._tool_route({})["rules"][0]["owner_revision"]
    for index in range(count):
        routing.request_rule(f"PROPOSAL {index}", n=1, base=base)
    return {p["id"] for p in routing.read_view()["proposals"].values() if p["status"] == "replaced"}


def offer(session="author-a"):
    agent_view.prepare_routing_table(refresh=True)
    return routing.prompt_push(recall_prompt.routing_marker(session), budget=2, session_id=session)


def test_notices_retry_and_page_through_the_delivery_ledger(home, monkeypatch):
    expected = seed_notices(home, monkeypatch)
    first = offer()
    assert any(i["entity"].startswith("notice:") for i in first["routing_changes"]["items"])
    assert offer()["routing_changes"] == first["routing_changes"]
    assert not recall_prompt.routing_marker("author-a").get("notices")
    home["clock"].advance(seconds=60)
    assert not routing_delivery.acknowledge("author-a", first["routing_changes"])
    retried = offer()
    assert retried["routing_changes"]["batch"] != first["routing_changes"]["batch"]
    assert retried["routing_changes"]["items"] == first["routing_changes"]["items"]
    delivered("author-a", retried, home["clock"])
    for _ in range(40):
        out = offer()
        if out["kind"] == "none":
            break
        state = routing_delivery.snapshot("author-a")
        for ident in out.get("notices", []):
            entity = "notice:" + ident
            assert state["flight"]["progress"][entity] == len(state["entries"][entity]["text"])
        delivered("author-a", out, home["clock"])
    else:
        raise AssertionError("notice pages did not drain")
    assert set(recall_prompt.routing_marker("author-a")["notices"]) == expected
    assert not routing.summary(session_id="author-a")["notices"]
    assert not routing_delivery.snapshot("bystander")


def test_expired_notice_debt_cancels_its_old_lease(home, monkeypatch):
    seed_notices(home, monkeypatch, count=2)
    first = offer()
    assert first["notices"]
    home["clock"].advance(days=8)
    newer = offer()
    assert "was replaced" not in (newer.get("text") or "")
    assert not any(e.startswith("notice:") for e in routing_delivery.snapshot("author-a")["entries"])
    assert not routing_delivery.acknowledge("author-a", first["routing_changes"])


def test_hook_cannot_acknowledge_notice_without_its_lease(home, monkeypatch):
    expected = seed_notices(home, monkeypatch, count=2)
    out = offer()
    unleased = dict(out)
    unleased.pop("routing_changes")
    delivered("author-a", unleased, home["clock"])
    assert not recall_prompt.routing_marker("author-a").get("notices")
    assert not routing_delivery.notice_receipts("author-a")
    delivered("author-a", out, home["clock"])
    assert routing_delivery.notice_receipts("author-a") == expected


def test_ledger_receipt_survives_marker_write_failure(home, monkeypatch):
    expected = seed_notices(home, monkeypatch, count=2)
    out = offer()
    with monkeypatch.context() as patch:
        patch.setattr(recall_prompt, "mark_routing_seen", lambda *a, **k: False)
        delivered("author-a", out, home["clock"])
    assert not recall_prompt.routing_marker("author-a").get("notices")
    assert routing_delivery.notice_receipts("author-a") == expected
    assert "was replaced" not in (offer().get("text") or "")


def test_notice_acknowledged_by_pull_does_not_leave_generic_change_debt(home, monkeypatch):
    seed_notices(home, monkeypatch, count=30)
    first = offer()
    delivered("author-a", first, home["clock"])
    assert any(e.startswith("notice:") for e in recall_prompt.routing_marker("author-a")["owed"])
    mcp_server._tool_route({})
    out = offer()
    assert out["kind"] == "none" and out["text"] is None
