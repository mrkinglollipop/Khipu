"""Paging invariants of the per-prompt routing block, across caps and mixes.

Every due entity either fits whole or pages; paging always progresses at
every cap from a small minimum; account-state lines stay on each prompt;
the block never exceeds its cap or UTF-16 ceiling. Drives
``routing_delivery._render`` and ``pull_offer`` with synthetic plans only:
no hook, database or real routing record."""
from __future__ import annotations

import contextlib
import copy
import math
from datetime import datetime, timedelta, timezone

import pytest

from khipu import routing
from khipu import routing_delivery as rd

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
HEADING = "## Routing changes at 12:00Z (Khipu's routing record; data, not instructions; guidance, not a gate)"
TAIL = "More routing owed."
STATE = ["Claude five-hour window: 40% left, resets 15:00Z.",
         "Codex weekly window: 61% left."]


def _item(entity, text, label, whole, rev="a", visible=False):
    return dict(entity=entity, revision=(rev * 16)[:16], text=text, label=label, whole=whole,
                visible=visible, removed=False)


def _rule(text="Rule words " * 4500, rev="a"):
    return _item("rule:6", text, "Standing rule 6", True, rev)


def _note(text):
    return _item("note", text, "Owner note", True, "c")


def _hold(owner_words="x" * 300):
    hold = dict(id="h1", scope_label="all work", scope={"kind": "all"}, reason="other",
                until=rd._iso(NOW + timedelta(minutes=5)), written_by={"kind": "owner"}, owner=True,
                text=owner_words)
    return _item("hold:h1", routing.hold_line(hold, NOW, relative=False), "hold", False, "b", visible=True)


def _account(line="Grok: limited; back at 13:30Z.", entity="account:grok", rev="d"):
    return _item(entity, line, entity.split(":")[0], False, rev, visible=True)


def _out(items, lines, cap, *, force=False, page_limit=None):
    text = "\n".join([HEADING, *lines])
    plan = dict(items=copy.deepcopy(items), current_entities=[i["entity"] for i in items], notices=[],
                heading=HEADING, text=text, limit=cap, page_limit=page_limit or cap, max_chars=9000,
                lease=60, force_pages=force, pace_forecasts={}, advice_forecasts={}, harness="claude",
                full=False)
    return dict(epoch="e1", version=1, kind="changes", text=text,
                owed_entities=[i["entity"] for i in items], shown_entities=[], notices=[], delivery_plan=plan)


def _step(state, out, minute):
    """One prompt: render, check the cap, then acknowledge like the receipt path."""
    answer = rd._render(state, out, NOW + timedelta(minutes=minute), "main")
    plan = out["delivery_plan"]
    if answer.get("text") is not None:
        assert rd._size(answer["text"]) <= plan["max_chars"]
        assert rd._size(answer["text"]) <= rd._size(HEADING) + 1 + plan["limit"], (plan["limit"], answer["text"])
    flight = state.pop("flight", None)
    if flight:
        for item in answer["routing_changes"]["items"]:
            e = state["entries"][item["entity"]]
            assert flight["progress"][item["entity"]] >= e.get("offset", 0)
            e["offset"] = flight["progress"][item["entity"]]
            e["acked"] = e["offset"] == len(e["text"])
            e["pages"] = item.get("page", {}).get("n", 0)
            e["restarted"] = False
    return answer


# ---- the Codex review of 11158f44 ----------------------------------------------

def test_an_oversized_hold_pages_ahead_of_a_long_rule():
    hold = _hold()
    assert rd._size(hold["text"]) > 400  # the real producer's 430-unit line at the default cap
    state = {}
    for minute in range(4):
        _step(state, _out([_rule(), hold], [hold["text"][:197] + "..."], 400), minute)
        if minute == 0:
            assert state["entries"]["hold:h1"]["offset"] > 0
    assert state["entries"]["hold:h1"]["acked"]


def test_an_oversized_hold_alone_pages_under_forced_paging():
    hold, state = _hold(), {}
    for minute in range(4):
        _step(state, _out([hold], [hold["text"][:197] + "..."], 400, force=True), minute)
    assert state["entries"]["hold:h1"]["acked"]


def test_an_account_named_like_the_note_keeps_its_state_line_while_the_note_pages():
    line = routing.account_line(dict(label="Owner note", state=routing.LIMITED, until="2026-10-21T13:00:00Z",
                                     witness={"source": "owner"}), NOW, relative=False)
    account = dict(_account(line, "account:fake"), offset=len(line), acked=True)
    note = _note("Owner words that do not match account state. " * 300)
    state = dict(epoch="e1", entries={"account:fake": copy.deepcopy(account)})
    for minute in range(3):
        answer = _step(state, _out([account, note], [line], 400), minute)
        assert line in answer["text"], answer["text"]
        assert "page " in answer["text"]


def _restarted(cap, label="Standing rule 6"):
    state = {}
    rule = dict(_rule(), label=label)
    _step(state, _out([rule], [], cap), 0)
    assert state["entries"]["rule:6"]["offset"] > 0
    changed = dict(rule, revision="e" * 16, text="New rule words " * 300)
    offsets = []
    for minute in range(1, 6):
        _step(state, _out([changed], [], cap), minute)
        offsets.append(state["entries"]["rule:6"].get("offset", 0))
    return offsets


def test_a_restarted_rule_progresses_at_a_low_cap():
    offsets = _restarted(150)
    assert offsets[0] > 0 and offsets[-1] > offsets[0], offsets


@pytest.mark.parametrize("cap", [150, 200, 250])
def test_a_restarted_rule_with_a_long_label_progresses(cap):
    offsets = _restarted(cap, label="Standing rule 6 for Codex, Claude, Claude second, Grok, GLM, DeepSeek")
    assert offsets[0] > 0 and offsets[-1] > offsets[0], offsets


# ---- the pull path -------------------------------------------------------------

@contextlib.contextmanager
def _fake_ledger():
    data = {}

    @contextlib.contextmanager
    def transaction(session):
        yield data

    saved = rd._transaction, rd._attachment
    rd._transaction, rd._attachment = transaction, lambda *a, **kw: None
    try:
        yield data
    finally:
        rd._transaction, rd._attachment = saved


def _pull_out(text, cap):
    item = _item("rule:6", text, "Standing rule 6", True, "1")
    return dict(epoch="e1", version=1, delivery_plan=dict(
        items=[item], current_entities=["rule:6"], notices=[], page_limit=cap, limit=cap, lease=60,
        at=rd._iso(NOW)))


@pytest.mark.parametrize("cap", [1, 2, 5, 40, 100])
def test_a_pulled_page_never_exceeds_a_tiny_limit(cap):
    with _fake_ledger():
        changes, _ = rd.pull_offer(_pull_out("x" * 10000, cap), session="fake", now=NOW)
    for item in (changes or {}).get("items", []):
        assert rd._size(item["text"]) <= cap


def test_a_stored_pull_width_shrinks_with_the_limit():
    with _fake_ledger() as data:
        rd.pull_offer(_pull_out("x" * 10000, 2000), session="fake", now=NOW)
        state = data["mcp"]
        state.pop("flight")
        state["entries"]["rule:6"].update(offset=1941, pages=1)
        changes, _ = rd.pull_offer(_pull_out("x" * 10000, 100), session="fake", now=NOW)
    assert rd._size(changes["items"][0]["text"]) <= 100


def test_a_pulled_whole_fit_counts_utf16_units():
    text = "\U0001F600" * 60  # 60 characters, 120 UTF-16 units
    with _fake_ledger():
        changes, _ = rd.pull_offer(_pull_out(text, 100), session="fake", now=NOW)
    assert all(rd._size(item["text"]) <= 100 for item in changes["items"])


# ---- property: every cap, every mix --------------------------------------------

MIXES = {
    "rule": lambda: [_rule("Rule words " * 150)],
    "rule+hold": lambda: [_rule("Rule words " * 150), _hold()],
    "rule+note+account": lambda: [_rule("Rule words " * 120), _note("Note words \U0001F600 " * 60), _account()],
    "hold+accounts": lambda: [_hold(), _account(), _account("Codex: open; 12% left.", "account:codex", "f")],
    "everything": lambda: [_hold("y" * 120), _account(), _rule("Rule words " * 100), _note("Note words " * 80)],
}


@pytest.mark.parametrize("mix", sorted(MIXES))
def test_every_cap_completes_within_a_bound_and_keeps_the_cap(mix):
    for cap in list(range(40, 400, 17)) + list(range(400, 2001, 160)):
        items = MIXES[mix]()
        lines = []
        for line in [*STATE, *(i["text"][:150] for i in items if not i["whole"])]:
            if rd._size("\n".join([*lines, line])) <= cap:  # the hook's renderer fits its own block
                lines.append(line)
        state, got = {}, {i["entity"]: "" for i in items}
        step = max(1, min(cap // 5, rd.PAGE_FLOOR, cap - rd._size(TAIL) - 2 - 70))
        bound = sum(math.ceil(rd._size(i["text"]) / step) for i in items) + 3 * len(items) + 5
        for minute in range(bound):
            before = {k: e.get("offset", 0) for k, e in state.get("entries", {}).items()}
            answer = _step(state, _out(items, lines, cap), minute)
            for entity, e in state["entries"].items():
                got[entity] += e["text"][before.get(entity, 0):e.get("offset", 0)]
            if "routing_changes" in answer and cap >= 400:
                for line in STATE:
                    assert line in answer["text"], (mix, cap, minute, answer["text"])
            if all(e.get("acked") for e in state["entries"].values()):
                break
        if cap < 100:
            continue  # below the smallest page header nothing is promised but the cap
        assert all(e.get("acked") for e in state["entries"].values()), (mix, cap, {
            k: (e.get("offset", 0), len(e["text"])) for k, e in state["entries"].items()})
        assert got == {i["entity"]: i["text"] for i in items}, (mix, cap)
