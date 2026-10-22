"""Synthetic producer-to-pager regressions; no live routing state or providers."""
from __future__ import annotations

import copy

import pytest

from khipu import routing, routing_delivery as rd, routing_replication, usage
from tests.test_routing_paging_invariants import NOW, STATE, _item, _out, _step


@pytest.mark.parametrize("embedded,ownership", [(True, "account"), (False, "account"),
                                               (True, "missing"), (True, "other")])
def test_routing_paging_keeps_embedded_pace_once_with_its_receipt(monkeypatch, embedded, ownership):
    cfg = routing.settings()
    monkeypatch.setattr(routing, "settings", lambda: cfg)
    monkeypatch.setattr(usage, "_load_cache", lambda: {})
    monkeypatch.setattr(routing_replication, "age_line", lambda now: None)
    monkeypatch.setattr(rd, "snapshot", lambda *args: {})
    pace = dict(window="five_hour", rate_pct_per_h=3,
                plan=dict(verdict="underpacing", needed_pct_per_h=8, unused_at_reset_pct=25))
    account = dict(key="fake", label="Claude", state=routing.OPEN, configured=True,
                   witness=dict(source="owner"), pace=[pace])
    note = dict(at=rd._iso(NOW), text="Owner's synthetic operating instructions. " * 100)
    view = dict(record=True, epoch="e1", version=1, accounts=[account], holds=[], rules=[], unmatched=[],
                changes=[], note=note, effective={"account:fake": account, "note": note})
    owners = []
    summary = routing.summary(view, now=NOW, session_id="fake", owners=owners)
    pace_text = routing.pace_line_for(account, NOW)
    if not embedded:
        summary["text"] = summary["text"].replace("; " + pace_text, "")
    if ownership == "missing":
        owners = None
    elif ownership == "other":
        owners = [rd.line_token("account:someone-else") if owner == rd.line_token("account:fake")
                  else owner for owner in owners]
    out = dict(summary, kind="full", line_entities=owners)
    out = routing._pace_push_adjust(out, view, NOW, "fake", "main")
    planned = rd.plan(out, view, NOW, cfg, project=lambda value: value)
    account_item = next(i for i in planned["delivery_plan"]["items"] if i["entity"] == "account:fake")
    state = dict(epoch="e1", entries={"account:fake": dict(copy.deepcopy(account_item),
                 acked=True, offset=len(account_item["text"]))})
    answer = rd._render(state, planned, NOW, "main")
    expected = 2 if embedded and ownership != "account" else 1
    assert answer["text"].count(pace_text) == expected, answer["text"]
    account_line = next(line for line in summary["text"].splitlines() if line.startswith("Claude:"))
    assert account_line in answer["text"]
    entity = next(iter(out["pace_forecasts"]))
    assert state["flight"]["progress"][entity] == len(pace_text)
    assert state["flight"]["shown"][entity] == len(pace_text)


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
