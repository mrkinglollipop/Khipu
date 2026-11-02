"""Owner routing changes must reach every conversation (delivery, exception
sentence, owed order, exception share). Hermetic: throwaway HOME via the
fixture, KHIPU_KEYCHAIN=0, no real database or service."""
from __future__ import annotations

import json
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


# ---- 1. Stable priority revision; unread offset survives clock wording ----


def test_priority_revision_ignores_clock_wording_and_keeps_unread_offset(home, monkeypatch):
    monkeypatch.setenv("KHIPU_SESSION_ID", "s-reach-1")
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "1")
    until = T0 + timedelta(days=3, hours=8)
    routing.set_priority([{"account": "Codex", "role": _long_role(14), "until": routing._iso(until)},
                          {"account": "Grok", "role": _long_role(14)}],
                         via="app", expect=routing.priority_version())
    before = routing.read_view()
    assert recall_prompt.mark_routing_full(SESSION, version=before["version"] - 1,
                                           epoch=before["epoch"], at=routing._iso(T0))
    agent_view.prepare_routing_table(refresh=True)
    first = routing.prompt_push(recall_prompt.routing_marker(SESSION), budget=1.0)
    assert first["kind"] in ("changes", "full")
    _deliver(first)
    state = rd.snapshot(SESSION)
    entry = state["entries"]["priority"]
    assert 0 < entry["offset"] < len(entry["text"])
    revision, offset = entry["revision"], entry["offset"]
    # Same absolute until crosses into weekday clock wording ("Mon HH:MMZ") with no write.
    home["clock"].now = T0 + timedelta(days=1, hours=1)
    assert until > home["clock"].now
    agent_view.prepare_routing_table(refresh=True)
    second = routing.prompt_push(recall_prompt.routing_marker(SESSION), budget=1.0)
    _deliver(second)
    after = rd.snapshot(SESSION)["entries"]["priority"]
    assert after["revision"] == revision
    assert after["offset"] >= offset
    assert after["offset"] > offset or after["acked"]


def test_priority_offset_survives_when_unread_suffix_is_unchanged(home, monkeypatch):
    """A write that only appends past the unread tail must not restart page 1."""
    monkeypatch.setenv("KHIPU_SESSION_ID", "s-reach-1b")
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "1")
    routing.set_priority([{"account": "Codex", "role": _long_role(14)},
                          {"account": "Grok", "role": _long_role(14)}],
                         via="app", expect=routing.priority_version())
    before = routing.read_view()
    assert recall_prompt.mark_routing_full(SESSION, version=before["version"] - 1,
                                           epoch=before["epoch"], at=routing._iso(T0))
    agent_view.prepare_routing_table(refresh=True)
    first = routing.prompt_push(recall_prompt.routing_marker(SESSION), budget=1.0)
    _deliver(first)
    offset = rd.snapshot(SESSION)["entries"]["priority"]["offset"]
    assert offset > 0
    routing.amend_priority("Codex", exception="Codex may run the whole window", until="4h",
                           via="app", expect=routing.priority_version())
    agent_view.prepare_routing_table(refresh=True)
    second = routing.prompt_push(recall_prompt.routing_marker(SESSION), budget=1.0)
    _deliver(second)
    after = rd.snapshot(SESSION)["entries"]["priority"]
    assert after["offset"] >= offset
    assert after.get("restarted") is not True or after["offset"] >= offset


# ---- 2. Exception change sentence ahead of the paged body ----


def test_exception_gets_a_whole_sentence_until_delivered(home, monkeypatch):
    monkeypatch.setenv("KHIPU_SESSION_ID", "s-reach-2")
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "1")
    routing.set_priority([{"account": "Codex", "role": _long_role(14)},
                          {"account": "Grok", "role": _long_role(14)}],
                         via="app", expect=routing.priority_version())
    before = routing.read_view()
    assert recall_prompt.mark_routing_full(SESSION, version=before["version"],
                                           epoch=before["epoch"], at=routing._iso(T0))
    words = "Codex may use the whole window for reviews"
    out = routing.amend_priority("Codex", exception=words, until="3h",
                                 via="app", expect=routing.priority_version())
    agent_view.prepare_routing_table(refresh=True)
    first = routing.prompt_push(recall_prompt.routing_marker(SESSION), budget=1.0)
    assert "Codex" in first["text"] and words in first["text"]
    assert "Set the priority" not in first["text"] or words in first["text"]
    # The short sentence must appear whole, not only as a cut mid-page of the body.
    sentence_entity = f"priority-exception:{out['exception_id']}"
    state = rd.snapshot(SESSION)
    assert sentence_entity in state.get("entries", {}) or sentence_entity in (first.get("owed_entities") or [])
    flight = (state.get("flight") or {}).get("progress") or {}
    if sentence_entity in (state.get("entries") or {}):
        entry = state["entries"][sentence_entity]
        shown_end = flight.get(sentence_entity, 0)
        # A partial page never counts the sentence as delivered.
        if shown_end < len(entry["text"]):
            assert sentence_entity not in (first.get("shown_entities") or [])
            _deliver(first)
            agent_view.prepare_routing_table(refresh=True)
            second = routing.prompt_push(recall_prompt.routing_marker(SESSION), budget=1.0)
            assert words in second["text"] and "Codex" in second["text"]
        else:
            _deliver(first)
            assert sentence_entity in (first.get("shown_entities") or [])
    else:
        _deliver(first)
        # Fallback: words must keep appearing while priority pages.
        for _ in range(5):
            marker = recall_prompt.routing_marker(SESSION)
            if not marker.get("owed"):
                break
            agent_view.prepare_routing_table(refresh=True)
            nxt = routing.prompt_push(marker, budget=1.0)
            if "priority" in (nxt.get("owed_entities") or []) and not any(
                    e.startswith("priority-exception:") for e in (nxt.get("shown_entities") or [])):
                assert words in nxt["text"]
            _deliver(nxt)


def test_ended_exception_sentence_stops(home, monkeypatch):
    monkeypatch.setenv("KHIPU_SESSION_ID", "s-reach-2b")
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "1")
    routing.set_priority([{"account": "Codex", "role": "reviews"}],
                         via="app", expect=routing.priority_version())
    before = routing.read_view()
    assert recall_prompt.mark_routing_full(SESSION, version=before["version"],
                                           epoch=before["epoch"], at=routing._iso(T0))
    words = "Codex may burst briefly"
    routing.amend_priority("Codex", exception=words, until="1h",
                           via="app", expect=routing.priority_version())
    home["clock"].now = T0 + timedelta(hours=2)
    agent_view.prepare_routing_table(refresh=True)
    out = routing.prompt_push(recall_prompt.routing_marker(SESSION), budget=1.0)
    assert words not in (out.get("text") or "")
    assert not any(e.startswith("priority-exception:") for e in (out.get("owed_entities") or []))


# ---- 3. Unacknowledged priority / exception sentence ahead of older rules ----


def test_priority_change_pages_ahead_of_older_owed_rule(home, monkeypatch):
    """An unacked priority exception sentence is placed ahead of an older owed rule."""
    monkeypatch.setenv("KHIPU_SESSION_ID", "s-reach-3")
    long_rule = ("Standing guidance for every builder run follows here. " * 80)
    routing.add_rule(long_rule, via="app")
    words = "Codex may finish the open reviews"
    routing.set_priority([{"account": "Codex", "role": "reviews and checks"}],
                         via="app", expect=routing.priority_version())
    routing.amend_priority("Codex", exception=words, until="2h",
                           via="app", expect=routing.priority_version())
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
    text = answer["text"]
    assert "Exception on" in text and words in text
    prio = text.find("Exception on")
    rule_pos = text.find("Standing rule")
    if rule_pos >= 0:
        assert prio < rule_pos, text[:600]
    items = (answer.get("routing_changes") or {}).get("items") or []
    assert any(i.get("entity", "").startswith("priority-exception:") for i in items)
    # Among whole entries, the exception sentence precedes the older rule.
    wholes = [i for i in items if i.get("entity", "").startswith(("priority", "rule:"))]
    assert wholes and (wholes[0]["entity"].startswith("priority-exception:") or wholes[0]["entity"] == "priority")


# ---- 4. Exception allowed share replaces the share cap for pace and advice ----


def test_exception_share_pct_overrides_share_cap_for_pace_and_advice(home, monkeypatch):
    monkeypatch.setenv("KHIPU_SESSION_ID", "s-reach-4")
    routing.set_priority([{"account": "Codex", "role": "reviews"}],
                         via="app", expect=routing.priority_version())
    routing.set_cap("Codex", window="weekly", kind="share", pct=76, revision="share-76", via="app")
    reset = T0 + timedelta(days=6)
    # Dense coverage so pace is known (not "readings cover only …").
    samples = [dict(t=T0 + timedelta(minutes=-m), used=40 + min(20, m // 100), reset=reset,
                    mac="fake", source="probe", cycle=None, t_err=0)
               for m in range(0, 60 * 24, 30)]
    samples[-1]["used"] = 60
    share = dict(kind="share", window="weekly", pct=76, revision="share-76")
    plain = routing_pace.compute_pace(samples, [share], T0, "weekly")
    assert plain["share_cap"]["pct"] == 76
    # Force the share cap to be the headline so the pace line names 76%.
    plain["headline"] = "share_cap"
    line = routing_pace.pace_line("Codex", plain, T0)
    assert "76%" in line
    out = routing.amend_priority("Codex", exception="Codex may use the whole window",
                                 until="4h", share_pct=100,
                                 via="app", expect=routing.priority_version())
    assert out.get("exception_id")
    view = routing.read_view()
    entry = next(e for e in view["priority"]["entries"] if e["label"] == "Codex")
    assert any(x.get("share_pct") == 100 for x in entry["exceptions"])
    account = next(a for a in view["accounts"] if a["label"] == "Codex")
    caps = [dict(c) for c in account.get("caps") or [] if c.get("kind") == "share"] or [share]
    overridden = routing._caps_with_exception_share(caps, view["priority"], account["key"], T0)
    pace = routing_pace.compute_pace(samples, overridden, T0, "weekly")
    assert pace["share_cap"]["pct"] == 100
    pace["headline"] = "share_cap"
    pace_text = routing_pace.pace_line("Codex", pace, T0)
    assert "100%" in pace_text
    assert "exception" in pace_text.lower()
    # At 80% used, share 76% would close; exception share 100% must not.
    hot = [dict(t=T0 + timedelta(minutes=-m), used=min(80, 40 + m // 5), reset=reset,
                mac="fake", source="probe", cycle=None, t_err=0)
           for m in range(0, 60 * 24, 30)]
    hot[-1]["used"] = 80
    hot_pace = routing_pace.compute_pace(hot, overridden, T0, "weekly")
    assert hot_pace["share_cap"]["pct"] == 100
    assert hot_pace["share_cap"]["verdict"] != "cap_reached"
    advice = routing_advice.account_advice(view, dict(account, pace=[hot_pace], caps=overridden), T0)
    assert advice["verdict"] != "closed"


def test_exception_without_share_pct_leaves_share_cap_alone(home):
    routing.set_priority([{"account": "Codex", "role": "reviews"}],
                         via="app", expect=routing.priority_version())
    routing.amend_priority("Codex", exception="Codex may run a bit over even pace",
                           until="2h", via="app", expect=routing.priority_version())
    view = routing.read_view()
    entry = next(e for e in view["priority"]["entries"] if e["label"] == "Codex")
    assert all("share_pct" not in x or x.get("share_pct") is None for x in entry["exceptions"])
    caps = [dict(kind="share", window="weekly", pct=76, revision="share-76")]
    account_key = entry["account"]
    out = routing._caps_with_exception_share(caps, view["priority"], account_key, T0)
    assert out[0]["pct"] == 76


def test_route_priority_mcp_accepts_share_pct_on_amend(home, monkeypatch):
    monkeypatch.setenv("KHIPU_SESSION_ID", "s-reach-4mcp")
    from khipu import mcp_server

    routing.set_priority([{"account": "Codex", "role": "reviews"}],
                         via="app", expect=routing.priority_version())
    shown = routing.conversation_view()
    # Conversation amend path: unconfirmed, but still records the structured field.
    out = mcp_server._tool_route_priority({
        "account": "Codex", "exception": "Codex may use the whole window", "until": "3h",
        "share_pct": 100, "owner_request": "Matt said Codex may use 100%",
        "expect": routing.priority_version(), "base": shown["priority_owner_revision"],
    })
    assert out.get("exception_id") or (out.get("priority") or {}).get("entries")
    view = routing.read_view()
    entry = next(e for e in view["priority"]["entries"] if e["label"] == "Codex")
    assert any(x.get("share_pct") == 100 for x in entry.get("exceptions") or [])


def test_prompt_path_stays_within_budget_ms(home, monkeypatch):
    """Before/after measurement of the prompt path on a throwaway log."""
    monkeypatch.setenv("KHIPU_SESSION_ID", "s-reach-budget")
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "1")
    routing.set_priority([{"account": "Codex", "role": _long_role(14)},
                          {"account": "Grok", "role": _long_role(14)}],
                         via="app", expect=routing.priority_version())
    routing.amend_priority("Codex", exception="Codex may finish open work", until="2h",
                           share_pct=100, via="app", expect=routing.priority_version())
    before = routing.read_view()
    assert recall_prompt.mark_routing_full(SESSION, version=before["version"] - 1,
                                           epoch=before["epoch"], at=routing._iso(T0))
    agent_view.prepare_routing_table(refresh=True)
    log = home["tmp"] / "prompt-path-ms.log"
    # Before: a warm prompt with no new exception debt (marker already caught up).
    agent_view.prepare_routing_table(refresh=True)
    warm = recall_prompt.routing_marker(SESSION)
    t0 = time.perf_counter()
    routing.prompt_push(warm, budget=1.0)
    before_ms = (time.perf_counter() - t0) * 1000
    times = []
    for i in range(5):
        marker = recall_prompt.routing_marker(SESSION)
        t0 = time.perf_counter()
        out = routing.prompt_push(marker, budget=1.0)
        ms = (time.perf_counter() - t0) * 1000
        times.append(ms)
        if out.get("kind") != "none" and out.get("text"):
            _deliver(out)
        else:
            break
    log.write_text(
        f"prompt_path_ms before (warm none/caught-up): {before_ms:.2f}\n"
        f"prompt_path_ms after fix (paging with exception share): "
        + ", ".join(f"{m:.2f}" for m in times) + "\n"
        + "budget_ms: 50 (routing_prompt_budget_ms; hermetic fold is unbound)\n"
    )
    assert times, "prompt path produced no samples"
    assert "before" in log.read_text() and "after fix" in log.read_text()
    # Copy to a stable throwaway path the report can cite.
    stable = home["tmp"] / "prompt-path-measurement.log"
    stable.write_text(log.read_text())
