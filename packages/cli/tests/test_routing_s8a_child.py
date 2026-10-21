"""Slice 8a: Claude Code child block, per-agent ledger, deferred pages.

Hermetic: the acceptance ``replay`` fixture only (temp HOME, fake clock,
synthetic rules). No real transcript, session file or harness is read.
"""
from __future__ import annotations

import io
import json
import sys
import time
from contextlib import redirect_stdout

from khipu import recall_prompt, recall_rule, routing, routing_delegation, routing_delivery
from tests.acceptance.routing_harness.received import rules_received_in_order
from tests.test_routing_acceptance import AUTHOR, replay  # noqa: F401

RULES = [f"SYNTHETIC-ROUTING-OPENING-{i} " + "~" * 2750 + f" SYNTHETIC-ROUTING-END-{i}" for i in range(4)]


def _run(payload, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    with redirect_stdout(buf):
        routing_delegation.main()
    return json.loads(buf.getvalue())


def _ctx(out):
    return out["hookSpecificOutput"]["additionalContext"] if out else ""


def _start(agent="a1", session="s1"):
    return {"hook_event_name": "SubagentStart", "session_id": session, "agent_id": agent,
            "agent_type": "probe", "cwd": "/work/project-a"}


def _tool(agent="a1", session="s1"):
    return {"hook_event_name": "PreToolUse", "session_id": session, "agent_id": agent,
            "agent_type": "probe", "cwd": "/work/project-a", "tool_name": "Bash",
            "tool_input": {"command": "cat probe.txt"}}


def _seed_rules(replay):  # noqa: F811
    for words in RULES:
        routing.add_rule(words, via="app")


def test_child_block_is_bounded_ordered_and_defers_the_rest(replay, monkeypatch):  # noqa: F811
    replay.configure(routing_prompt_chars=2000)
    routing.report(account="Codex", state="limited", until="1h", author=AUTHOR)
    routing.add_hold("wait for the child fixture", scope="account", account="Codex",
                     reason="keep", author=AUTHOR)
    _seed_rules(replay)
    out = _run(_start(), monkeypatch)
    text = _ctx(out)
    assert text, "oversized summary gave the child nothing"
    assert recall_rule._utf16_len(text) <= 4000
    # Account states and holds come before the standing rules.
    assert text.index("Codex:") < text.index("Standing rules:") if "Standing rules:" in text else \
        text.index("Codex:") < text.index(RULES[0][:20])
    assert "Hold on Codex" in text
    # The first rule that fits is whole; the rest are named with an opening.
    assert RULES[0] in text
    for i in (1, 2, 3):
        assert RULES[i] not in text
        assert f"Standing rule {i + 1}" in text
    # The parent's main ledger/marker never moved; the child has its own.
    assert recall_prompt.routing_marker("s1") == {}
    assert routing_delivery.snapshot("s1", "main") == {}
    agent = routing_delivery.snapshot("s1", "agent:a1")
    deferred = [e for e in agent.get("entries", {}).values()
                if e["entity"].startswith("rule:") and not e.get("acked")]
    assert len(deferred) == 3


def test_deferred_pages_arrive_on_the_childs_own_tool_results(replay, monkeypatch):  # noqa: F811
    replay.configure(routing_prompt_chars=2000)
    _seed_rules(replay)
    opening = _ctx(_run(_start(), monkeypatch))
    assert RULES[0] in opening
    pages = []
    for _ in range(12):
        text = _ctx(_run(_tool(), monkeypatch))
        if not text:
            break
        pages.append(text)
    assert rules_received_in_order(pages, RULES[1:])
    assert all(recall_rule._utf16_len(page) <= 2000 for page in pages)
    # Nothing leaked into the parent's component, and the child's is done.
    assert routing_delivery.snapshot("s1", "main") == {}
    assert recall_prompt.routing_marker("s1") == {}
    agent = routing_delivery.snapshot("s1", "agent:a1")
    assert all(e.get("acked") for e in agent["entries"].values() if e["entity"].startswith("rule:"))


def test_deferred_note_pages_arrive_whole(replay, monkeypatch):  # noqa: F811
    replay.configure(routing_prompt_chars=2000, routing_child_chars=1200)
    routing.add_rule("small synthetic rule", via="app")
    note = "synthetic note " + "n" * 3000
    routing.set_note(note, via="app")
    text = _ctx(_run(_start(), monkeypatch))
    assert text and recall_rule._utf16_len(text) <= 1200
    assert note not in text
    pages = []
    for _ in range(10):
        page = _ctx(_run(_tool(), monkeypatch))
        if not page:
            break
        pages.append(page)
    body = "".join(line for page in pages for line in page.splitlines()[1:])
    assert note in body


def test_a_rule_edited_mid_pagination_restarts_at_page_one(replay, monkeypatch):  # noqa: F811
    replay.configure(routing_prompt_chars=2000)
    _seed_rules(replay)
    _run(_start(), monkeypatch)
    first = _ctx(_run(_tool(), monkeypatch))
    assert "Rule 2 page 1/" in first
    routing.edit_rule(2, "SYNTHETIC-ROUTING-OPENING-1 edited " + "y" * 2750 + " SYNTHETIC-ROUTING-END-1",
                      via="app")
    restarted = _ctx(_run(_tool(), monkeypatch))
    assert "changed while being shown" in restarted
    assert "Rule 2 page 1/" in restarted
    assert "y" * 100 in restarted
    assert "~" * 100 not in restarted


def test_child_block_setting_is_registered_and_bounds_the_block(replay, monkeypatch):  # noqa: F811
    assert routing.settings()["routing_child_chars"] == 4000
    replay.configure(routing_child_chars=800)
    _seed_rules(replay)
    text = _ctx(_run(_start(), monkeypatch))
    assert text and recall_rule._utf16_len(text) <= 800


def test_child_start_without_agent_id_is_bounded_and_reserves_nowhere(replay, monkeypatch):  # noqa: F811
    replay.configure(routing_prompt_chars=2000)
    _seed_rules(replay)
    payload = _start()
    del payload["agent_id"]
    text = _ctx(_run(payload, monkeypatch))
    assert text and recall_rule._utf16_len(text) <= 4000
    assert routing_delivery.snapshot("s1", "main") == {}
    assert routing_delivery.snapshot("s1", "agent:unknown") == {}


def _planned(replay):  # noqa: F811
    out = routing.light_status(replay.now, start=True, session_id=None)
    return routing_delivery.plan(dict(out, kind="full"), out.view, replay.now,
                                 routing.settings(), session=None)


def test_per_agent_ledgers_stay_bounded_finished_and_stale(replay):  # noqa: F811
    replay.configure(routing_prompt_chars=2000)
    routing.add_rule("small synthetic rule", via="app")
    planned = _planned(replay)
    stale = routing_delivery.child_offer(planned, session="s1", component="agent:old",
                                         now=replay.now, cfg=routing.settings())
    assert routing_delivery.acknowledge("s1", stale["routing_changes"])
    replay.advance(8 * 86400)
    fresh = routing_delivery.child_offer(planned, session="s1", component="agent:fresh",
                                         now=replay.now, cfg=routing.settings())
    assert routing_delivery.acknowledge("s1", fresh["routing_changes"])
    # A finished component older than the stale age is dropped outright.
    assert routing_delivery.snapshot("s1", "agent:old") == {}
    for i in range(150):
        answer = routing_delivery.child_offer(planned, session="s1", component=f"agent:a{i}",
                                              now=replay.now, cfg=routing.settings())
        if answer.get("routing_changes"):
            assert routing_delivery.acknowledge("s1", answer["routing_changes"])
    path = routing_delivery._path("s1")
    assert path.stat().st_size < 1_048_576
    data = json.loads(path.read_text())
    agents = {name: state for name, state in data.items() if name.startswith("agent:")}
    assert len(agents) <= routing_delivery._AGENT_MAX_COMPONENTS
    # Finished agents keep only revision proof, never the delivered words.
    assert "small synthetic rule" not in json.dumps(agents)
    assert all(not state.get("entries") for state in agents.values())


def test_child_page_read_has_the_parent_prompt_deadline(replay, monkeypatch):  # noqa: F811
    replay.configure(routing_prompt_chars=2000, routing_prompt_budget_ms=50)
    _seed_rules(replay)
    _run(_start(), monkeypatch)
    original = routing._start_view

    def slow(now):
        time.sleep(2)
        return original(now)

    monkeypatch.setattr(routing, "_start_view", slow)
    began = time.monotonic()
    out = _run(_tool(), monkeypatch)
    assert time.monotonic() - began < 1.0
    assert _ctx(out) == ""


def test_hook_context_cap_applies_whatever_child_chars_says(replay, monkeypatch):  # noqa: F811
    replay.configure(routing_prompt_chars=2000)
    _seed_rules(replay)
    for setting in (0, 100000):
        replay.configure(routing_child_chars=setting)
        text = _ctx(_run(_start(f"cap{setting}", f"s-cap{setting}"), monkeypatch))
        assert text
        assert recall_rule._utf16_len(text) <= recall_rule._HOOK_CONTEXT_BUDGET


def test_tiny_child_limits_never_exceed_the_bound(replay, monkeypatch):  # noqa: F811
    _seed_rules(replay)
    for limit in (50, 120):
        replay.configure(routing_child_chars=limit)
        text = _ctx(_run(_start(f"tiny{limit}", f"s-tiny{limit}"), monkeypatch))
        assert recall_rule._utf16_len(text) <= limit
    replay.configure(routing_child_chars=50)
    assert _ctx(_run(_start("tiny-none", "s-tiny-none"), monkeypatch)) == ""


def test_dropped_account_and_hold_lines_are_paged_later(replay, monkeypatch):  # noqa: F811
    replay.configure(routing_prompt_chars=2000, routing_child_chars=150)
    routing.report(account="Codex", state="limited", until="1h", author=AUTHOR)
    routing.add_hold("wait for the child fixture", scope="account", account="Codex",
                     reason="keep", author=AUTHOR)
    _seed_rules(replay)
    opening = _ctx(_run(_start(), monkeypatch))
    assert "Codex:" not in opening and "Hold on Codex" not in opening
    pages = []
    for _ in range(4):
        page = _ctx(_run(_tool(), monkeypatch))
        if page:
            pages.append(page)
    assert any("Codex:" in page for page in pages)
    assert any("Hold on Codex" in page for page in pages)


def test_parallel_tool_calls_do_not_print_the_same_page_twice(replay, monkeypatch):  # noqa: F811
    replay.configure(routing_prompt_chars=2000)
    _seed_rules(replay)
    _run(_start(), monkeypatch)
    first = routing.child_page_push("s1", "a1")
    assert first and first["text"]
    # The page is leased before printing: a second call before its ack
    # gets nothing, not the same page and batch again.
    assert routing.child_page_push("s1", "a1") is None
    assert routing_delivery.acknowledge(first["session"], first["routing_changes"])
    third = routing.child_page_push("s1", "a1")
    assert third and third["routing_changes"]["batch"] != first["routing_changes"]["batch"]


def test_no_agent_identity_means_no_per_agent_pages(replay, monkeypatch):  # noqa: F811
    replay.configure(routing_prompt_chars=2000)
    _seed_rules(replay)
    payload = _tool()
    del payload["agent_id"]
    out, info, kind = routing_delegation.hook_main(json.dumps(payload))
    # A parent's ordinary (non-delegation) Bash call still gets nothing.
    assert out == {} and kind is None
