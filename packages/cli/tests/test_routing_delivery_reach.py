"""Owner routing changes must reach every conversation (delivery, exception
sentence, owed order, exception share). Hermetic: throwaway HOME via the
fixture, KHIPU_KEYCHAIN=0, no real database or service."""
from __future__ import annotations

import json
import re
import statistics
import time
from datetime import datetime, timedelta, timezone

import pytest

from khipu import agent_view, recall_prompt, routing, routing_advice, routing_delivery as rd, routing_pace


T0 = datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc)
ME = {"kind": "conversation", "harness": "claude_code", "project": "routing", "session": "one"}
SESSION = "routing-delivery-reach"
ACCOUNTS = [{"label": "Grok", "harness": "grok"}, {"label": "Codex", "harness": "codex"},
            {"label": "Claude", "harness": "claude"}]


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


@pytest.fixture
def home(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": ACCOUNTS, "routing_prompt_chars": 400}))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    clock = Clock()
    monkeypatch.setattr(routing, "_now", clock)
    return {"data": data, "clock": clock, "tmp": tmp_path}


def _deliver(out):
    at = recall_prompt._RoutingAtPrompt()
    at.session = SESSION
    _ctx, info = at.add_to("", out)
    assert info and info.get("pending")
    assert recall_prompt._routing_delivered(info, True)


def _long_role(n=14):
    """Under ROLE_BUDGET (2000) but long enough to page at the 400-char cap."""
    chunk = ("Keep verifying each scoped repair against the owner's acceptance "
             "criteria, and hand back only after a focused synthetic check. ")
    text = chunk * n
    assert len(text) <= 2000
    return text


def _merge_state(old_entries, items, *, current=("priority",)):
    """Run ``_merge`` on a hand-built ledger and plan; returns the entries."""
    state = {"epoch": "e", "entries": old_entries, "batches": {}}
    out = {"epoch": "e", "version": 1,
           "delivery_plan": {"items": items, "current_entities": list(current), "removed": {}, "notices": [],
                             "at": routing._iso(T0)}}
    rd._merge(state, out)
    return state["entries"]


def _prio(text, revision, **more):
    return dict(entity="priority", text=text, label="priority", revision=revision, whole=True,
                visible=False, removed=False, **more)


def _deliver_until(marker_session, count, home):
    """Deliver ``count`` priority pages; returns the ledger entry."""
    for _ in range(count):
        agent_view.prepare_routing_table(refresh=True)
        out = routing.prompt_push(recall_prompt.routing_marker(marker_session), budget=1.0)
        _deliver(out)
    return rd.snapshot(marker_session)["entries"]["priority"]


def _catch_up(home):
    view = routing.read_view()
    assert recall_prompt.mark_routing_full(SESSION, version=view["version"] - 1, epoch=view["epoch"],
                                           at=routing._iso(T0))


# ---- 1. Stable priority revision; the unread place survives -------------


def test_priority_revision_ignores_clock_wording_and_keeps_unread_offset(home, monkeypatch):
    monkeypatch.setenv("KHIPU_SESSION_ID", "s-reach-1")
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "1")
    until = T0 + timedelta(days=3, hours=8)
    routing.set_priority([{"account": "Codex", "role": _long_role(14), "until": routing._iso(until)},
                          {"account": "Grok", "role": _long_role(14)}],
                         via="app", expect=routing.priority_version())
    _catch_up(home)
    entry = _deliver_until(SESSION, 1, home)
    assert 0 < entry["offset"] < len(entry["text"])
    revision, offset = entry["revision"], entry["offset"]
    # Same absolute until crosses into weekday clock wording ("Mon HH:MMZ") with no write.
    home["clock"].now = T0 + timedelta(days=1, hours=1)
    assert until > home["clock"].now
    after = _deliver_until(SESSION, 1, home)
    assert after["revision"] == revision
    assert after["offset"] > offset and not after["acked"]
    assert not after.get("restarted")


def test_insertion_before_the_cursor_moves_the_offset_with_the_unread_text(home, monkeypatch):
    """An exception added to an earlier account sits before the cursor; the
    unread suffix is unchanged, so the conversation resumes in the same words."""
    monkeypatch.setenv("KHIPU_SESSION_ID", "s-reach-1b")
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "1")
    routing.set_priority([{"account": "Codex", "role": "short role"},
                          {"account": "Grok", "role": _long_role(14)}],
                         via="app", expect=routing.priority_version())
    _catch_up(home)
    entry = _deliver_until(SESSION, 3, home)
    old_text, old_offset = entry["text"], entry["offset"]
    assert len(old_text.split("; 2. Grok:")[0]) < old_offset < len(old_text)
    routing.amend_priority("Codex", exception="Codex may run all night", until="4h",
                           via="app", expect=routing.priority_version())
    agent_view.prepare_routing_table(refresh=True)
    routing.prompt_push(recall_prompt.routing_marker(SESSION), budget=1.0)
    after = rd.snapshot(SESSION)["entries"]["priority"]
    grown = len(after["text"]) - len(old_text)
    assert grown > 0 and after["revision"] != entry["revision"]
    assert after["offset"] == old_offset + grown
    assert after["text"][after["offset"]:] == old_text[old_offset:]
    assert after["restarted"] is False
    assert after["pages"] == entry["pages"]


def test_text_added_past_the_cursor_restarts_at_page_one(home, monkeypatch):
    monkeypatch.setenv("KHIPU_SESSION_ID", "s-reach-1c")
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "1")
    routing.set_priority([{"account": "Codex", "role": "short role"},
                          {"account": "Grok", "role": _long_role(14)}],
                         via="app", expect=routing.priority_version())
    _catch_up(home)
    entry = _deliver_until(SESSION, 2, home)
    assert entry["offset"] > 0
    # Grok is last: its exception ends the text, so the unread suffix changes.
    routing.amend_priority("Grok", exception="Grok may run all night", until="4h",
                           via="app", expect=routing.priority_version())
    agent_view.prepare_routing_table(refresh=True)
    routing.prompt_push(recall_prompt.routing_marker(SESSION), budget=1.0)
    after = rd.snapshot(SESSION)["entries"]["priority"]
    assert after["offset"] == 0
    assert after["restarted"] is True and after["acked"] is False


def test_offset_kept_only_for_an_unacknowledged_priority_mid_text():
    old = "A" * 100 + "B" * 100
    new = "X" * 30 + old  # inserted before the cursor
    mid = _merge_state({"priority": _prio(old, "old", offset=150, acked=False, pages=2)}, [_prio(new, "new")])
    assert mid["priority"]["offset"] == 180 and mid["priority"]["restarted"] is False
    assert mid["priority"]["pages"] == 2
    # Same text and cursor in another entity: never kept.
    rule = dict(entity="rule:r1", text=old, label="rule", whole=True, visible=False, removed=False)
    kept = _merge_state({"rule:r1": dict(rule, revision="old", offset=150, acked=False)},
                        [dict(rule, text=new, revision="new")], current=("rule:r1",))
    assert kept["rule:r1"]["offset"] == 0
    # The cursor at the start or the very end is not mid-text.
    for at in (0, len(old)):
        got = _merge_state({"priority": _prio(old, "old", offset=at, acked=False)}, [_prio(new, "new")])
        assert got["priority"]["offset"] == 0
    # Unread words changed: restart.
    changed = _merge_state({"priority": _prio(old, "old", offset=150, acked=False)},
                           [_prio(old[:150] + "Z" * 50, "new")])
    assert changed["priority"]["offset"] == 0 and changed["priority"]["acked"] is False


def test_acknowledged_priority_with_a_new_revision_and_the_same_text_stays_acknowledged():
    text = "Your priority: 1. Codex: reviews"
    old = _prio(text, "old-style-hash", offset=len(text), acked=True, shown_offset=len(text))
    got = _merge_state({"priority": old}, [_prio(text, "stable-hash")])["priority"]
    assert got["acked"] is True and got["offset"] == len(text)
    assert got["revision"] == "stable-hash"
    # Nothing is owed: the render has no debt for it.
    assert [e for e in {"priority": got}.values() if not e.get("acked")] == []


def test_acknowledged_priority_whose_text_grew_is_owed_from_the_start():
    old_text = "Your priority: 1. Codex: reviews"
    new_text = old_text + "; 2. Grok: builds"
    old = _prio(old_text, "old", offset=len(old_text), acked=True)
    got = _merge_state({"priority": old}, [_prio(new_text, "new")])["priority"]
    assert got["acked"] is False and got["offset"] == 0
    assert got["text"] == new_text and got["restarted"] is False


def test_empty_priority_text_has_no_cursor_to_keep():
    got = _merge_state({"priority": _prio("A" * 50, "old", offset=20, acked=False)}, [_prio("", "new")])["priority"]
    assert got["text"] == "" and got["offset"] == 0 and got["acked"] is False


# ---- 1b. Order: insertion order inside a group; only holds and priority go first ----


def test_due_order_keeps_insertion_order_inside_each_group():
    names = ["rule:z", "account:m", "hold:q", "pace:b", "rule:a", "priority", "account:a", "hold:b", "note",
             "priority-exception:x2", "priority-exception:x1"]
    entries = [dict(entity=n, whole=n.split(":")[0] in ("rule", "priority", "priority-exception", "note"))
               for n in names]
    # Render and health order: holds, lines, exceptions, priority, other whole entries.
    assert [e["entity"] for e in sorted(entries, key=rd._due_rank)] == [
        "hold:q", "hold:b", "account:m", "pace:b", "account:a",
        "priority-exception:x2", "priority-exception:x1", "priority", "rule:z", "rule:a", "note"]
    # Due order: holds first; the priority change moves ahead of older rules and
    # notes only; account and pace lines, and every other entry, keep their place.
    assert [e["entity"] for e in rd._front_order(entries)] == [
        "hold:q", "hold:b", "priority-exception:x2", "priority-exception:x1", "priority",
        "rule:z", "account:m", "pace:b", "rule:a", "account:a", "note"]
    by = lambda *ns: [dict(entity=n, whole=n.startswith(("rule:", "priority"))) for n in ns]
    assert [e["entity"] for e in rd._front_order(by("account:m", "rule:z", "priority"))] == [
        "account:m", "priority", "rule:z"]
    assert [e["entity"] for e in rd._front_order(by("account:m", "priority", "rule:z"))] == [
        "account:m", "priority", "rule:z"]
    assert [e["entity"] for e in rd._front_order(by("account:b", "account:a", "priority"))] == [
        "account:b", "account:a", "priority"]


def test_budget_debt_counts_in_render_order():
    entries = {n: dict(entity=n, text="t" * 100, label=n, revision="r" * 16, whole=n.startswith(("rule:", "priority")),
                       offset=0, acked=False) for n in ("rule:b", "account:z", "priority", "account:a")}
    state = {"entries": entries}
    rd._budget_debt(state, {"page_limit": 400, "limit": 400}, 1)
    ahead = sorted(entries.values(), key=lambda e: e["owed_chars"])
    assert [e["entity"] for e in ahead] == ["account:z", "account:a", "priority", "rule:b"]


# ---- 2. Exception change sentence ahead of the paged body ----


def test_exception_sentence_is_one_short_whole_sentence():
    until = "2026-10-20T18:00:00Z"
    short = routing.exception_change_sentence("Codex", {"text": "Codex may run all night", "until": until})
    assert short == "Exception on Codex until 2026-10-20T18:00:00Z: Codex may run all night."
    words = " ".join(f"w{i:03d}" for i in range(400))  # 2,000 characters
    long = routing.exception_change_sentence("Codex " + "x" * 90, {"text": words, "until": until})
    assert "\n" not in long and len(long) <= 40 + 100 + len(until) + 40
    assert re.fullmatch(r"Exception on Codex\.\.\. until 2026-10-20T18:00:00Z: w000 w001 .* w\d{3}\.\.\.", long)
    kept = long.split(": ", 1)[1][:-3].split(" ")
    assert kept == [f"w{i:03d}" for i in range(len(kept))] and len(kept) * 5 - 1 <= 100  # whole words only


def test_long_exception_sentence_is_planned_whole_and_shown_whole(home, monkeypatch):
    monkeypatch.setenv("KHIPU_SESSION_ID", "s-reach-2a")
    routing.set_priority([{"account": "Codex", "role": "reviews"}], via="app", expect=routing.priority_version())
    words = " ".join(f"w{i:03d}" for i in range(99))  # 494 characters, within the stored limit of 500
    out = routing.amend_priority("Codex", exception=words, until="3h", via="app", expect=routing.priority_version())
    view = routing.read_view()
    cfg = dict(routing.settings(), routing_prompt_chars=400)
    planned = rd.plan(dict(kind="changes", text="## Routing changes\n", version=view["version"],
                           epoch=view["epoch"], owed_entities=["priority"], shown_entities=[]),
                      view, home["clock"].now, cfg, session=SESSION)
    item = next(i for i in planned["delivery_plan"]["items"] if i["entity"] == "priority-exception:" + out["exception_id"])
    assert item["whole"] is True and len(item["text"]) <= 200 and item["text"].startswith("Exception on Codex until ")
    answer = rd._render({"epoch": view["epoch"], "entries": {}}, planned, home["clock"].now, "main")
    assert item["text"] in answer["text"].split("\n")  # a whole line, never a "page n/m"
    assert "priority-exception:" + out["exception_id"] in [i["entity"] for i in answer["routing_changes"]["items"]]


def test_sentence_projection_gets_text_and_label_pairs(home, monkeypatch):
    routing.set_priority([{"account": "Codex", "role": "reviews"}], via="app", expect=routing.priority_version())
    routing.amend_priority("Codex", exception="Codex may run all night", until="3h",
                           via="app", expect=routing.priority_version())
    view = routing.read_view()
    cfg = dict(routing.settings(), routing_prompt_chars=400)
    seen = []

    def project(pairs):
        seen.append(list(pairs))
        return [(f"P[{t}]", f"L[{label}]") for t, label in pairs]

    planned = rd.plan(dict(kind="changes", text="## Routing changes\n", version=view["version"],
                           epoch=view["epoch"], owed_entities=["priority"], shown_entities=[]),
                      view, home["clock"].now, cfg, session=SESSION, project=project)
    sentence_calls = [c for c in seen if c and "Exception on" in c[0][0]]
    assert len(sentence_calls) == 1 and all(isinstance(p, tuple) and len(p) == 2 for p in sentence_calls[0])
    item = next(i for i in planned["delivery_plan"]["items"] if i["entity"].startswith("priority-exception:"))
    assert item["text"].startswith("P[Exception on Codex until ") and item["label"] == "L[Exception on Codex]"
    assert item["withheld"] is True  # a projection that changed the words is never acknowledged


def test_exception_sentence_leads_and_is_repeated_until_whole(home, monkeypatch):
    monkeypatch.setenv("KHIPU_SESSION_ID", "s-reach-2")
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "1")
    routing.set_priority([{"account": "Codex", "role": _long_role(14)},
                          {"account": "Grok", "role": _long_role(14)}],
                         via="app", expect=routing.priority_version())
    _catch_up(home)
    words = "Codex may use the whole window for reviews"
    out = routing.amend_priority("Codex", exception=words, until="3h", via="app", expect=routing.priority_version())
    sentence = f"Exception on Codex until {out['exception_until']}: {words}."
    entity = "priority-exception:" + out["exception_id"]
    agent_view.prepare_routing_table(refresh=True)
    first = routing.prompt_push(recall_prompt.routing_marker(SESSION), budget=1.0)
    lines = first["text"].split("\n")
    assert lines[1] == sentence  # ahead of the paged body
    assert entity in [i["entity"] for i in first["routing_changes"]["items"]]
    assert "priority" not in first["shown_entities"] and "More routing owed." in lines
    _deliver(first)
    # Shown whole once: delivered, and never repeated while the body pages on.
    assert rd.snapshot(SESSION)["entries"][entity]["acked"] is True
    agent_view.prepare_routing_table(refresh=True)
    second = routing.prompt_push(recall_prompt.routing_marker(SESSION), budget=1.0)
    assert sentence not in second["text"] and "priority; revision " in second["text"]


def test_sentence_is_owed_until_the_hook_records_its_delivery(home, monkeypatch):
    """Offered is not delivered: until the hook records it, the sentence stays owed."""
    monkeypatch.setenv("KHIPU_SESSION_ID", "s-reach-2c")
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "1")
    routing.set_priority([{"account": "Codex", "role": _long_role(14)}], via="app", expect=routing.priority_version())
    _catch_up(home)
    out = routing.amend_priority("Codex", exception="Codex may run all night", until="3h",
                                 via="app", expect=routing.priority_version())
    entity = "priority-exception:" + out["exception_id"]
    agent_view.prepare_routing_table(refresh=True)
    first = routing.prompt_push(recall_prompt.routing_marker(SESSION), budget=1.0)  # never delivered
    assert entity in [i["entity"] for i in first["routing_changes"]["items"]]
    assert rd.snapshot(SESSION)["entries"][entity]["acked"] is False
    assert first["text"].count("Exception on Codex until") == 1


def test_exception_in_a_full_block_counts_as_shown_and_keeps_the_block_byte_identical(home, monkeypatch):
    routing.set_priority([{"account": "Codex", "role": "reviews"}], via="app", expect=routing.priority_version())
    out = routing.amend_priority("Codex", exception="Codex may burst", until="3h",
                                 via="app", expect=routing.priority_version())
    entity = "priority-exception:" + out["exception_id"]
    view = routing.read_view()
    cfg = dict(routing.settings(), routing_prompt_chars=400)
    now = home["clock"].now

    def planned(text):
        return rd.plan(dict(kind="full", text=text, version=view["version"], epoch=view["epoch"],
                            owed_entities=[], shown_entities=[]), view, now, cfg, session=SESSION)

    line = next(i["text"] for i in planned("x")["delivery_plan"]["items"] if i["entity"] == "priority")
    block = "## Routing at 14:00Z (Khipu's routing record)\n" + line  # a small full block that carries the line
    items = {i["entity"]: i for i in planned(block)["delivery_plan"]["items"]}
    assert items["priority"]["visible"] is True and items[entity]["visible"] is True
    answer = rd._render({"epoch": view["epoch"], "entries": {}}, planned(block), now, "main")
    assert answer["text"] == block  # legacy: byte-identical, no sentence added, no paging
    assert entity in answer["shown_entities"] and "priority" in answer["shown_entities"]
    # A full block that does not carry the words leaves the sentence owed.
    bare = "## Routing at 14:00Z (Khipu's routing record)\nCodex: unknown"
    assert {i["entity"]: i for i in planned(bare)["delivery_plan"]["items"]}[entity]["visible"] is False


def test_ended_exception_stops_being_shown_even_mid_delivery(home, monkeypatch):
    monkeypatch.setenv("KHIPU_SESSION_ID", "s-reach-2b")
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "1")
    routing.set_priority([{"account": "Codex", "role": _long_role(14)}], via="app", expect=routing.priority_version())
    _catch_up(home)
    words = "Codex may burst briefly"
    out = routing.amend_priority("Codex", exception=words, until="1h", via="app", expect=routing.priority_version())
    entity = "priority-exception:" + out["exception_id"]
    agent_view.prepare_routing_table(refresh=True)
    first = routing.prompt_push(recall_prompt.routing_marker(SESSION), budget=1.0)  # leased, not yet delivered
    assert words in first["text"]
    assert entity in rd.snapshot(SESSION)["entries"]
    home["clock"].now = T0 + timedelta(hours=2)
    agent_view.prepare_routing_table(refresh=True)
    later = routing.prompt_push(recall_prompt.routing_marker(SESSION), budget=1.0)
    assert entity not in rd.snapshot(SESSION)["entries"]
    assert words not in later["text"] and entity not in later["owed_entities"]


def test_ended_exception_leaves_an_unacknowledged_entry_no_debt():
    sentence = dict(entity="priority-exception:a1", text="Exception on Codex.", label="Exception on Codex",
                    revision="r1", whole=True, visible=False, removed=False)
    got = _merge_state({"priority": _prio("T", "r0", offset=0, acked=False),
                        "priority-exception:a1": dict(sentence, offset=0, acked=False),
                        "priority-exception:old": dict(sentence, entity="priority-exception:old", offset=19, acked=True)},
                       [_prio("T", "r0")])
    assert "priority-exception:a1" not in got and "priority-exception:old" in got


# ---- 3. Unacknowledged priority / exception sentence ahead of older rules ----


def test_priority_change_pages_ahead_of_older_owed_rule(home, monkeypatch):
    """An unacked priority exception sentence and the priority come ahead of an older owed rule."""
    monkeypatch.setenv("KHIPU_SESSION_ID", "s-reach-3")
    routing.add_rule("Standing guidance for every builder run follows here. " * 80, via="app")
    words = "Codex may finish the open reviews"
    routing.set_priority([{"account": "Codex", "role": "reviews and checks"}],
                         via="app", expect=routing.priority_version())
    routing.amend_priority("Codex", exception=words, until="2h", via="app", expect=routing.priority_version())
    view = routing.read_view()
    cfg = dict(routing.settings(), routing_prompt_chars=400)
    rule_entity = routing._rule_component(view["rules"][0])
    rule_text = "Standing rule " + routing.rule_line(view["rules"][0])
    state = {"epoch": view["epoch"], "entries": {
        rule_entity: dict(entity=rule_entity, text=rule_text, label="Standing rule 1",
                          whole=True, revision="a" * 16, offset=200, acked=False, visible=False),
    }}
    planned = rd.plan(dict(kind="changes", text="## Routing changes\n", version=view["version"],
                           epoch=view["epoch"], owed_entities=[rule_entity, "priority"],
                           shown_entities=[], force_pages=True),
                      view, home["clock"].now, cfg, session=SESSION)
    answer = rd._render(state, planned, home["clock"].now, "main")
    items = [i["entity"] for i in answer["routing_changes"]["items"]]
    assert items[:2] == [next(e for e in items if e.startswith("priority-exception:")), "priority"]
    lines = answer["text"].split("\n")
    assert lines[1].startswith("Exception on Codex until ") and words in lines[1]
    assert lines[2].startswith("Your priority (your words")
    # The older rule is owed, not dropped, and never ahead of either.
    assert rule_entity in answer["owed_entities"]
    assert answer["owed_entities"].index("priority") < answer["owed_entities"].index(rule_entity)


# ---- 4. Exception allowed share replaces the share cap for pace and advice ----


def _samples(used_now, reset):
    rows = [dict(t=T0 + timedelta(minutes=-m), used=min(used_now, 40 + m // 5), reset=reset,
                 mac="fake", source="probe", cycle=None, t_err=0) for m in range(0, 60 * 24, 30)]
    rows[0]["used"] = used_now
    return rows


def _codex_with_share_exception(home, pct=100, stored_cap=76):
    routing.set_priority([{"account": "Codex", "role": "reviews"}], via="app", expect=routing.priority_version())
    if stored_cap is not None:
        routing.set_cap("Codex", window="weekly", kind="share", pct=stored_cap, revision="share-76", via="app")
    routing.amend_priority("Codex", exception="Codex may use the whole window", until="4h", share_pct=pct,
                           via="app", expect=routing.priority_version())
    view = routing.read_view()
    account = next(a for a in view["accounts"] if a["label"] == "Codex")
    return view, account


def test_exception_share_pct_overrides_share_cap_for_pace(home, monkeypatch):
    monkeypatch.setenv("KHIPU_SESSION_ID", "s-reach-4")
    reset = T0 + timedelta(days=6)
    view, account = _codex_with_share_exception(home)
    stored = [c for c in account["caps"] if c["kind"] == "share"]
    assert [c["pct"] for c in stored] == [76]
    overridden = routing._caps_with_exception_share(stored, view["priority"], account["key"], T0)
    # 80% used: the stored 76% is reached, the exception's 100% is not.
    plain = routing_pace.compute_pace(_samples(80, reset), stored, T0, "weekly")
    assert plain["share_cap"]["pct"] == 76 and plain["share_cap"]["verdict"] == "cap_reached"
    pace = routing_pace.compute_pace(_samples(80, reset), overridden, T0, "weekly")
    assert pace["share_cap"]["pct"] == 100 and pace["share_cap"]["exception_share"] is True
    assert pace["share_cap"]["verdict"] != "cap_reached"
    # The line names the percentage in force and says it is an exception.
    plain = routing_pace.compute_pace(_samples(60, reset), stored, T0, "weekly")
    pace = routing_pace.compute_pace(_samples(60, reset), overridden, T0, "weekly")
    plain["headline"] = pace["headline"] = "share_cap"
    assert "lands on 76%" in routing_pace.pace_line("Codex", plain, T0)
    assert "(priority exception)" not in routing_pace.pace_line("Codex", plain, T0)
    assert "lands on 100% (priority exception)" in routing_pace.pace_line("Codex", pace, T0)
    # The exception's end restores the stored cap exactly.
    after = routing._caps_with_exception_share(stored, view["priority"], account["key"], T0 + timedelta(hours=5))
    assert after == stored


def test_cap_status_reports_the_exception_share_the_verdict_used(home, monkeypatch):
    reset = T0 + timedelta(days=6)
    view, account = _codex_with_share_exception(home)
    stored = [c for c in account["caps"] if c["kind"] == "share"]
    pace = routing_pace.compute_pace(_samples(80, reset),
                                     routing._caps_with_exception_share(stored, view["priority"], account["key"], T0),
                                     T0, "weekly")
    share = next(s for s in routing.cap_statuses(view, dict(account, pace=[pace]), T0) if s["kind"] == "share")
    assert share["pct"] == 100 and share["exception_share"] is True and share["verdict"] != "cap_reached"
    # Without the exception, the stored percentage is the one reported.
    plain = routing_pace.compute_pace(_samples(80, reset), stored, T0, "weekly")
    share = next(s for s in routing.cap_statuses(view, dict(account, pace=[plain]), T0) if s["kind"] == "share")
    assert share["pct"] == 76 and "exception_share" not in share and share["verdict"] == "cap_reached"


def test_advice_honours_the_exception_share_with_a_stored_cap(home, monkeypatch):
    reset = T0 + timedelta(days=6)
    view, account = _codex_with_share_exception(home)
    stored = [c for c in account["caps"] if c["kind"] == "share"]
    pace = routing_pace.compute_pace(_samples(80, reset),
                                     routing._caps_with_exception_share(stored, view["priority"], account["key"], T0),
                                     T0, "weekly")
    advice = routing_advice.account_advice(view, dict(account, pace=[pace]), T0)
    assert advice["reason"] != "share cap_reached" and advice["verdict"] != "closed"
    # Fully used, even the exception's share is reached.
    full = routing_pace.compute_pace(_samples(100, reset),
                                     routing._caps_with_exception_share(stored, view["priority"], account["key"], T0),
                                     T0, "weekly")
    closed = routing_advice.account_advice(view, dict(account, pace=[full]), T0)
    assert closed["verdict"] == "closed"


def test_advice_honours_the_exception_share_with_no_stored_cap(home, monkeypatch):
    reset = T0 + timedelta(days=6)
    view, account = _codex_with_share_exception(home, pct=90, stored_cap=None)
    assert not [c for c in account.get("caps") or [] if c["kind"] == "share"]
    caps = routing._caps_with_exception_share([{"kind": "pace_target", "window": "weekly", "pct": 100,
                                                "slack": 5, "revision": "pt"}], view["priority"], account["key"], T0)
    assert [c["pct"] for c in caps if c["kind"] == "share"] == [90]
    entry = dict(account, caps=[])
    under = routing_pace.compute_pace(_samples(80, reset), caps, T0, "weekly")
    assert under["share_cap"]["exception_share"] is True
    advice = routing_advice.account_advice(view, dict(entry, pace=[under]), T0)
    assert advice["verdict"] != "closed"
    over = routing_pace.compute_pace(_samples(92, reset), caps, T0, "weekly")
    assert over["share_cap"]["verdict"] == "cap_reached"
    closed = routing_advice.account_advice(view, dict(entry, pace=[over]), T0)
    assert closed["verdict"] == "closed" and closed["reason"] == "share cap_reached"
    # No exception share: nothing is invented, advice is as before.
    nothing = routing_pace.compute_pace(_samples(92, reset), [], T0, "weekly")
    assert "exception_share" not in (nothing.get("share_cap") or {})
    plain = routing_advice.account_advice(view, dict(entry, pace=[nothing]), T0)
    assert plain["reason"] != "share cap_reached"


def test_exception_without_share_pct_leaves_share_cap_alone(home):
    routing.set_priority([{"account": "Codex", "role": "reviews"}],
                         via="app", expect=routing.priority_version())
    routing.amend_priority("Codex", exception="Codex may run a bit over even pace",
                           until="2h", via="app", expect=routing.priority_version())
    view = routing.read_view()
    entry = next(e for e in view["priority"]["entries"] if e["label"] == "Codex")
    assert [x.get("share_pct") for x in entry["exceptions"]] == [None]
    caps = [dict(kind="share", window="weekly", pct=76, revision="share-76")]
    assert routing._caps_with_exception_share(caps, view["priority"], entry["account"], T0) == caps


def test_route_priority_mcp_accepts_share_pct_on_amend(home, monkeypatch):
    monkeypatch.setenv("KHIPU_SESSION_ID", "s-reach-4mcp")
    from khipu import mcp_server

    routing.set_priority([{"account": "Codex", "role": "reviews"}],
                         via="app", expect=routing.priority_version())
    shown = routing.conversation_view()
    out = mcp_server._tool_route_priority({
        "account": "Codex", "exception": "Codex may use the whole window", "until": "3h",
        "share_pct": 100, "owner_request": "Matt said Codex may use 100%",
        "expect": routing.priority_version(), "base": shown["priority_owner_revision"],
    })
    view = routing.read_view()
    entry = next(e for e in view["priority"]["entries"] if e["label"] == "Codex")
    assert [(x["id"], x.get("share_pct")) for x in entry["exceptions"]] == [(out["exception_id"], 100)]
    with pytest.raises(ValueError):
        mcp_server._tool_route_priority({
            "account": "Codex", "exception": "x", "share_pct": "all", "owner_request": "Matt said x",
            "expect": routing.priority_version(), "base": shown["priority_owner_revision"]})


# ---- 5. The prompt path stays inside its budget ----

PROMPT_BUDGET_MS = 50


def test_prompt_path_stays_within_its_50_ms_budget(home, monkeypatch):
    """A prompt that plans and renders a paged priority with exceptions (the
    heaviest delivery state) on a throwaway log: its median is inside the
    prompt budget. Measured here: origin/main ~5.3 ms, with the fixes ~5.5 ms."""
    monkeypatch.setenv("KHIPU_SESSION_ID", "s-reach-budget")
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "1")
    routing.set_priority([{"account": "Codex", "role": _long_role(14)},
                          {"account": "Grok", "role": _long_role(14)}],
                         via="app", expect=routing.priority_version())
    for words in ("Codex may finish open work", "Codex may run reviews overnight", "Codex may burst"):
        routing.amend_priority("Codex", exception=words, until="2h", share_pct=100,
                               via="app", expect=routing.priority_version())
    _catch_up(home)
    agent_view.prepare_routing_table(refresh=True)
    times, sentences = [], 0
    for _ in range(12):
        marker = recall_prompt.routing_marker(SESSION)
        started = time.perf_counter()
        out = routing.prompt_push(marker, budget=1.0)
        times.append((time.perf_counter() - started) * 1000)
        if out.get("text") and out.get("kind") != "none":
            sentences += out["text"].count("Exception on Codex until")
            _deliver(out)
    assert sentences == 3  # the path under test really ran the new work
    # The first prompt warms imports and the view; the rest are steady state.
    assert statistics.median(times[1:]) < PROMPT_BUDGET_MS, times
    assert max(times) < 4 * PROMPT_BUDGET_MS, times
