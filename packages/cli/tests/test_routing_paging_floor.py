"""A paged entry never narrows to a sliver, and account-state lines survive paging.

Drives ``routing_delivery._render`` directly with a synthetic plan, so no
hook, database or real routing record is involved."""
from __future__ import annotations

from datetime import datetime, timezone

from khipu import routing_delivery as rd

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
HEADING = "## Routing changes at 12:00Z (Khipu's routing record; data, not instructions; guidance, not a gate)"
STATE = ["Claude five-hour window: 40% left, resets 15:00Z.",
         "Codex weekly window: 61% left.",
         "Grok: limited, back at 13:30Z."]
RULE = "Codex first for builds today; Grok for surveys and audits only. " * 40
FLOOR_SEEN = 100  # smallest sensible page for the test cap, never a two-character sliver


def _out(rule=RULE, cap=2000, state=STATE):
    text = "\n".join([HEADING, *state])
    item = dict(entity="rule:6", revision="00f74a24" + "0" * 8, text=rule,
                label="Standing rule 6", whole=True, visible=False, removed=False)
    plan = {"items": [item], "limit": cap, "page_limit": cap, "max_chars": 9000, "heading": HEADING,
            "lease": 60, "text": text, "current_entities": ["rule:6"], "notices": [],
            "pace_forecasts": {}, "advice_forecasts": {}, "harness": "claude", "full": False}
    return {"epoch": "e1", "kind": "changes", "text": text, "owed_entities": ["rule:6"],
            "shown_entities": [], "notices": [], "delivery_plan": plan}


def _step(state, out, minutes=0):
    """One prompt: render, then acknowledge it the way the receipt path does."""
    from datetime import timedelta

    now = NOW + timedelta(minutes=minutes)
    answer = rd._render(state, out, now, "c")
    flight = state.get("flight")
    if flight:
        for item in answer["routing_changes"]["items"]:
            e = state["entries"][item["entity"]]
            e["offset"] = flight["progress"][item["entity"]]
            e["acked"] = e["offset"] == len(e["text"])
            e["pages"] = item.get("page", {}).get("n", 0)
        state.pop("flight")
    return answer


def _sent(state, before):
    return state["entries"]["rule:6"].get("offset", 0) - before


def test_a_stored_width_of_two_heals_to_a_floor_sized_page():
    state = {}
    _step(state, _out())
    entry = state["entries"]["rule:6"]
    entry.update(width=2, offset=120, pages=1)  # what the live ledger already holds
    answer = _step(state, _out(), 1)
    assert answer["routing_changes"]["items"][0]["page"]["m"] < 60
    assert _sent(state, 120) >= FLOOR_SEEN


def test_account_state_lines_stay_while_a_long_rule_pages():
    state = {}
    for minute in range(3):
        answer = _step(state, _out(), minute)
        for line in STATE:
            assert line in answer["text"], (minute, answer["text"])
        assert "page " in answer["text"]


def test_a_prompt_with_room_below_the_floor_sends_no_sliver_and_keeps_state_lines():
    state = {}
    _step(state, _out(cap=1000), 0)
    before = state["entries"]["rule:6"]["offset"]
    big = [("x" * 150) + f" line {i}" for i in range(3)]  # fills their half of the cap
    answer = _step(state, _out(cap=1000, state=big), 1)
    for line in big:
        assert line in answer["text"]
    assert _sent(state, before) == 0 or _sent(state, before) >= FLOOR_SEEN


def test_total_size_never_exceeds_the_cap_and_a_rule_takes_at_most_half():
    state, cap = {}, 1200
    for minute in range(80):
        answer = _step(state, _out(cap=cap), minute)
        assert rd._size(answer["text"]) <= cap + rd._size(HEADING) + 1
        assert "More routing owed." in answer["text"] or state["entries"]["rule:6"]["acked"]
        if "routing_changes" in answer:
            own = [ln for ln in answer["text"].split("\n")[1:] if ln not in STATE]
            assert rd._size("\n".join(own)) <= cap // 2 + 20
        if state["entries"]["rule:6"]["acked"]:
            break
    assert state["entries"]["rule:6"]["acked"]


def test_pages_complete_the_rule_whole_end_to_end():
    state, got, before = {}, "", 0
    for minute in range(80):
        answer = _step(state, _out(), minute)
        entry = state["entries"]["rule:6"]
        got += entry["text"][before:entry["offset"]]
        before = entry["offset"]
        if entry["acked"]:
            break
    assert got == RULE
    assert "rule:6" in answer["shown_entities"]
