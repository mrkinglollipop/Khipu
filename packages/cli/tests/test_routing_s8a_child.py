"""Slice 8a: Claude Code child block, per-agent ledger, deferred pages.

Hermetic: the acceptance ``replay`` fixture only (temp HOME, fake clock,
synthetic rules). No real transcript, session file or harness is read.
"""
from __future__ import annotations

import io
import json
import sys
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


def test_no_agent_identity_means_no_per_agent_pages(replay, monkeypatch):  # noqa: F811
    replay.configure(routing_prompt_chars=2000)
    _seed_rules(replay)
    payload = _tool()
    del payload["agent_id"]
    out, info, kind = routing_delegation.hook_main(json.dumps(payload))
    # A parent's ordinary (non-delegation) Bash call still gets nothing.
    assert out == {} and kind is None
