"""Synthetic producer-to-pager regressions; no live routing state or providers."""
from __future__ import annotations



from khipu import routing, routing_delivery as rd, routing_replication, usage
from tests.test_routing_paging_invariants import NOW, STATE, _item, _out, _step


def test_routing_paging_legacy_offer_stays_byte_identical():
    account = _item("account:fake", "Claude: open.", "Claude", False, visible=True)
    out = _out([account], [(account["entity"], account["text"])], 400)
    assert rd._render({}, out, NOW, "main")["text"] == out["text"]


def test_routing_priority_paging_uses_spare_room_at_default_cap():
    cap = routing.settings()["routing_prompt_chars"]
    assert cap == 400
    role = ("Implement scoped repairs, retain the owner's acceptance criteria, and verify "
            "the final result with focused synthetic fixtures before handing work back. ") * 10
    priority = dict(entries=[dict(account="claude|", label="Claude", role=role),
                            dict(account="codex|", label="Codex", role=role)])
    text = routing.priority_line(priority, NOW)
    assert 2900 <= len(text) <= 3300
    item = _item("priority", text, "priority", True)
    state, got, before = {}, "", 0
    for minute in range(12):
        answer = _step(state, _out([item], STATE, cap), minute)
        assert all(line in answer["text"] for line in STATE)
        entry = state["entries"]["priority"]
        got += text[before:entry["offset"]]
        before = entry["offset"]
        if entry["acked"]:
            break
    assert got == text, (before, len(text))
    assert entry["acked"]
