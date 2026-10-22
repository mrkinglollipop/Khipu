"""Paging invariants of the per-prompt routing block, across caps and mixes.

Every due entity either fits whole or pages; paging always progresses at
every cap from the minimum; account-state lines stay on each prompt; a
line is filtered only by the entity its producer recorded for it; the
block never exceeds its cap or UTF-16 ceiling. Drives the real producers
(``routing.summary``), ``routing_delivery.plan``, ``_render`` and
``pull_offer`` with synthetic views and plans only: no hook, database or
real routing record."""
from __future__ import annotations

import contextlib
import copy
import math
from datetime import datetime, timedelta, timezone

import pytest

from khipu import routing
from khipu import routing_delivery as rd
from khipu import usage

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
MIN_CAP = getattr(rd, "MIN_CAP", 200)  # read this way so the older module fails on behaviour, not on a name
_header_label = getattr(rd, "_header_label", lambda label: label)
HEADING = "## Routing changes at 12:00Z (Khipu's routing record; data, not instructions; guidance, not a gate)"
TAIL = "More routing owed."
STATE = ["Claude: 40% left, resets 15:00Z.", "Codex: 61% left."]


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


def _out(items, lines, cap, *, force=False, page_limit=None, mapped=True):
    """A plan as ``rd.plan`` leaves it. ``lines`` are plain lines or
    (entity, line) pairs: the entity its producer recorded for that line."""
    pairs = [(None, x) if isinstance(x, str) else x for x in lines]
    text = "\n".join([HEADING, *(line for _, line in pairs)])
    plan = dict(items=copy.deepcopy(items), current_entities=[i["entity"] for i in items], notices=[],
                heading=HEADING, text=text, limit=cap, page_limit=page_limit or cap, max_chars=9000,
                lease=60, force_pages=force, pace_forecasts={}, advice_forecasts={}, harness="claude",
                full=False, line_entities=[None, *(e for e, _ in pairs)] if mapped else None)
    return dict(epoch="e1", version=1, kind="changes", text=text,
                owed_entities=[i["entity"] for i in items], shown_entities=[], notices=[], delivery_plan=plan)


def _step(state, out, minute):
    """One prompt: render, check the cap, then acknowledge like the receipt path."""
    answer = rd._render(state, out, NOW + timedelta(minutes=minute), "main")
    plan = out["delivery_plan"]
    if answer.get("text") is not None:
        assert rd._size(answer["text"]) <= plan.get("max_chars", 9000)
        cap = max(MIN_CAP, plan["limit"])
        assert rd._size(answer["text"]) <= rd._size(plan["heading"]) + 1 + cap, (cap, answer["text"])
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


# ---- the Codex review of ac392f21 ----------------------------------------------

@pytest.fixture
def no_usage(monkeypatch):
    monkeypatch.setattr(usage, "_load_cache", lambda: {})


CFG = dict(routing_line_chars=200, routing_summary_chars=800, routing_prompt_chars=800, routing_delivery_lease=60)


@pytest.mark.parametrize("cap", [100, 120, 140, 150])
def test_a_rule_that_fits_the_body_is_shown_whole_at_a_low_cap(no_usage, cap):
    labels = ["Codex", "Claude", "Claude second", "Grok", "GLM", "DeepSeek"]
    accounts = [dict(key=f"fake{n}", label=x, state=routing.OPEN, witness={"source": "owner"})
                for n, x in enumerate(labels)]
    rule = dict(n=6, text="Only Codex, Claude, Claude second, Grok, GLM, DeepSeek do urgent work.", confirmed=True)
    view = dict(record=True, epoch="e", version=1, accounts=accounts, holds=[], rules=[rule], unmatched=[],
                changes=[], effective={"rule:6": rule})
    out = dict(epoch="e", version=1, kind="changes", text="## Routing changes", owed_entities=["rule:6"],
               shown_entities=[], notices=[])
    planned = rd.plan(out, view, NOW, dict(CFG, routing_prompt_chars=cap), project=lambda x: x)
    state = {}
    answer = _step(state, planned, 0)
    assert state["entries"]["rule:6"]["acked"], answer["text"]


def _summary_plan(view, now, cap=400):
    owners = []
    out = routing.summary(view, now=now, session_id="s", owners=owners)
    out = dict(out, kind="full", owed_entities=list(view["effective"]), line_entities=owners)
    planned = rd.plan(out, view, now, dict(CFG, routing_summary_chars=cap, routing_prompt_chars=cap),
                      project=lambda x: x)
    return planned


def _ack_all(state, now):
    for e in state["entries"].values():
        e.update(offset=len(e["text"]), acked=True)
    state.pop("flight", None)


def test_colliding_account_labels_keep_the_other_accounts_state_line(no_usage):
    work = dict(key="a", label="Work", state=routing.DOWN, reason="offline", witness={"source": "owner"})
    backup = dict(key="b", label="Work: open; set by you backup", state=routing.LIMITED,
                  until="2026-10-21T13:00:00Z", witness={"source": "owner"})
    view = dict(record=True, epoch="fake", version=1, accounts=[work, backup], holds=[], rules=[], unmatched=[],
                changes=[], effective={"account:a": work, "account:b": backup})
    state = {}
    rd._render(state, _summary_plan(view, NOW), NOW, "main")
    _ack_all(state, NOW)
    work = dict(work, state=routing.OPEN)
    note = {"at": "2026-10-21T12:00:00Z", "text": "Owner words to page. " * 1000}
    view = dict(view, version=2, accounts=[work, backup], note=note,
                effective={"account:a": work, "account:b": backup, "note": note})
    line_b = routing.account_line(backup, NOW)
    assert line_b.startswith(routing.account_line(work, NOW))  # the collision itself
    for minute in range(3):
        answer = _step(state, _summary_plan(view, NOW), minute)
        assert line_b in answer["text"], answer["text"]
        assert "page " in answer["text"]


def test_an_acknowledged_account_whose_wording_changes_keeps_its_line(no_usage):
    account = dict(key="f", label="Owner note", state=routing.LIMITED, until="2026-10-21T13:00:00Z",
                   witness={"source": "owner"})
    note = {"at": "2026-10-21T12:00:00Z", "text": "Owner words that do not match account state. " * 300}
    view = dict(record=True, epoch="fake", version=1, accounts=[account], holds=[], rules=[], unmatched=[],
                changes=[], effective={"account:f": account})
    state = {}
    rd._render(state, _summary_plan(view, NOW), NOW, "main")
    _ack_all(state, NOW)
    view = dict(view, version=2, note=note, effective={"account:f": account, "note": note})
    for minute in range(1, 4):
        now = NOW + timedelta(minutes=minute)
        line = routing.account_line(account, now)
        assert line != state["entries"]["account:f"]["text"]  # "in 1 h" became "in 59 min"
        answer = _step(state, _summary_plan(view, now), minute)
        assert line in answer["text"], answer["text"]


def test_colliding_labels_in_a_recorded_plan_keep_the_backup_line():
    work = _account("Work: open; set by you", "account:a", "1")
    backup = dict(_account("Work: open; set by you backup: limited, back at 13:00Z", "account:b", "2"),
                  acked=True, offset=54)
    note = _note("Owner words to page. " * 1000)
    was = dict(_account("Work: down (offline)", "account:a", "0"), acked=True, offset=20)
    state = dict(epoch="e1", entries={"account:a": was, "account:b": copy.deepcopy(backup)})  # Work first, as saved
    lines = [("note", note["text"][:150] + "..."), ("account:a", work["text"]), ("account:b", backup["text"])]
    for minute in range(3):
        answer = _step(state, _out([work, backup, note], lines, 800), minute)
        assert backup["text"] in answer["text"], answer["text"]


def test_a_reworded_account_line_in_a_recorded_plan_is_kept():
    account = dict(_account("Owner note: limited, back at 13:00Z (in 1 h)", "account:f", "3"), acked=True, offset=44)
    shown = "Owner note: limited, back at 13:00Z (in 59 min)"
    note = _note("Owner words that do not match account state. " * 300)
    state = dict(epoch="e1", entries={"account:f": copy.deepcopy(account)})
    for minute in range(3):
        answer = _step(state, _out([account, note], [("account:f", shown)], 400), minute)
        assert shown in answer["text"], answer["text"]


def test_a_plan_without_line_owners_hides_no_line():
    pointer = "Standing rule 6 added: " + "Rule words " * 5
    answer = _step({}, _out([_rule()], [pointer, STATE[0]], 1000, mapped=False), 0)
    assert pointer in answer["text"] and STATE[0] in answer["text"]


def test_a_recorded_preview_is_not_repeated_beside_its_page():
    pointer = "Standing rule 6 added: " + "Rule words " * 5
    answer = _step({}, _out([_rule()], [("rule:6", pointer), STATE[0]], 1000), 0)
    assert pointer not in answer["text"] and STATE[0] in answer["text"]


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


@pytest.mark.parametrize("cap", [1, 2, 5, 40, 100, 200, 400])
def test_a_pulled_page_never_exceeds_the_limit_or_the_minimum(cap):
    with _fake_ledger():
        changes, _ = rd.pull_offer(_pull_out("x" * 10000, cap), session="fake", now=NOW)
    for item in changes["items"]:
        assert 2 <= rd._size(item["text"]) <= max(cap, MIN_CAP)


def test_a_stored_pull_width_shrinks_with_the_limit():
    with _fake_ledger() as data:
        rd.pull_offer(_pull_out("x" * 10000, 2000), session="fake", now=NOW)
        state = data["mcp"]
        state.pop("flight")
        state["entries"]["rule:6"].update(offset=1941, pages=1)
        changes, _ = rd.pull_offer(_pull_out("x" * 10000, 200), session="fake", now=NOW)
    assert rd._size(changes["items"][0]["text"]) <= 200


def test_a_pulled_whole_fit_counts_utf16_units():
    text = "\U0001F600" * 150  # 150 characters, 300 UTF-16 units
    with _fake_ledger():
        changes, _ = rd.pull_offer(_pull_out(text, 200), session="fake", now=NOW)
    assert all(rd._size(item["text"]) <= 200 for item in changes["items"])


@pytest.mark.parametrize("text", ["\U0001F600" * 300, "ab" + "\U0001F600c" * 200])
@pytest.mark.parametrize("cap", range(1, 12))
def test_tiny_pull_caps_complete_astral_text(cap, text):
    got = ""
    with _fake_ledger() as data:
        for minute in range(40):
            now = NOW + timedelta(minutes=minute)
            changes, _ = rd.pull_offer(_pull_out(text, cap), session="fake", now=now)
            if changes is None:
                break
            got += "".join(item["text"] for item in changes["items"])
            assert rd.pull_acknowledge("fake", changes["batch"], now=now) == "acked"
    assert got == text
    assert data["mcp"]["entries"]["rule:6"]["acked"]


# ---- property: every cap from the minimum, every mix ----------------------------

def _mix_collide():
    work = _account("Work: open; set by you", "account:a", "1")
    backup = dict(_account("Work: open; set by you backup: limited, back at 13:00Z", "account:b", "2"),
                  acked=True, offset=54)
    return [work, _note("Owner words to page. " * 25)], [backup], [("account:b", backup["text"])]


def _mix_reworded():
    account = dict(_account("Owner note: limited, back at 13:00Z (in 1 h)", "account:f", "3"), acked=True, offset=44)
    shown = "Owner note: limited, back at 13:00Z (in 59 min)"
    return [_note("Note words " * 40), _rule("Rule words " * 40)], [account], [("account:f", shown)]


MIXES = {
    "rule": lambda: ([_rule("Rule words " * 60)], [], []),
    "rule+hold": lambda: ([_rule("Rule words " * 50), _hold()], [], []),
    "rule+note+account": lambda: ([_rule("Rule words " * 40), _note("Note words \U0001F600 " * 25), _account()],
                                  [], []),
    "hold+accounts": lambda: ([_hold(), _account(), _account("Codex: open; 12% left.", "account:codex", "f")], [], []),
    "everything": lambda: ([_hold("y" * 120), _account(), _rule("Rule words " * 30), _note("Note words " * 30)],
                           [], []),
    "colliding labels": _mix_collide,
    "changed wording": _mix_reworded,
}


def _guaranteed(items, cap):
    """The room the state lines are promised: half the cap, less the queue
    head's smallest page (at most the longest header and a floor page)."""
    floor = min(rd.PAGE_FLOOR, cap // 5)
    worst = max(rd._size(f"{_header_label(i['label'])}; revision {i['revision'][:8]}; page ") + 24
                + min(floor, rd._size(i["text"])) for i in items)
    return min(cap // 2, cap - rd._size(TAIL) - 2 - worst - 1)


@pytest.mark.parametrize("mix", sorted(MIXES))
def test_every_cap_completes_within_a_bound_keeps_state_lines_and_the_cap(mix):
    for cap in range(MIN_CAP, 2001):
        items, acked, context = MIXES[mix]()
        state_lines = [] if context else list(STATE)
        own = [(i["entity"], i["text"][:120] + "...") for i in items if not i["whole"]]
        own += [("rule:6", "Standing rule 6 added: Rule words")] if any(i["entity"] == "rule:6" for i in items) else []
        lines = []
        for line in [*state_lines, *context, *own]:  # the producer fits its own block
            if rd._size("\n".join(x if isinstance(x, str) else x[1] for x in [*lines, line])) <= cap:
                lines.append(line)
        out = _out(items + acked, lines, cap)
        shown = [line for line in [*state_lines, *(line for _, line in context)]]
        promised = sum(rd._size(line) + 1 for line in shown) <= _guaranteed(items, cap)
        state = dict(epoch="e1", entries={a["entity"]: copy.deepcopy(a) for a in acked})
        got = {i["entity"]: "" for i in items}
        step = min(cap // 5, rd.PAGE_FLOOR)
        bound = sum(math.ceil(rd._size(i["text"]) / step) for i in items) + 3 * len(items) + 3
        for minute in range(bound):
            before = {k: e.get("offset", 0) for k, e in state["entries"].items()}
            answer = _step(state, out, minute)
            for entity in got:
                e = state["entries"][entity]
                got[entity] += e["text"][before.get(entity, 0):e.get("offset", 0)]
            if promised:
                for line in shown:
                    assert line in answer["text"], (mix, cap, minute, answer["text"])
            if all(e.get("acked") for e in state["entries"].values()):
                break
        assert all(e.get("acked") for e in state["entries"].values()), (mix, cap, {
            k: (e.get("offset", 0), len(e["text"])) for k, e in state["entries"].items()})
        if not any(i["visible"] for i in items if not i["whole"]):
            assert got == {i["entity"]: i["text"] for i in items}, (mix, cap)
        if cap == MIN_CAP:
            assert promised, (mix, "the state lines must be promised at the minimum cap")
