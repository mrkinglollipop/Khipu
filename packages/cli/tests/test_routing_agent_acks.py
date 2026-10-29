"""Subagent routing pages are leased only by the hook that prints them,
priced the way they are paged, and judged only on what a child is owed.

Hermetic: the acceptance ``replay`` fixture only (temp HOME, fake clock,
synthetic rules). No real ledger, log or harness is read.
"""
from __future__ import annotations

import time

from khipu import routing, routing_delivery, routing_health
from tests.test_routing_acceptance import AUTHOR, replay  # noqa: F401
from tests.test_routing_s8a_child import RULES, _ctx, _run, _seed_rules, _start, _tool


def _health(replay, agent="a1"):  # noqa: F811
    state = routing_delivery.snapshot("s1", f"agent:{agent}")
    return routing_health.component_health(state, replay.now, slack=2, child=True)


def test_the_read_in_the_hooks_child_process_holds_no_lease(replay, monkeypatch):  # noqa: F811
    replay.configure(routing_prompt_chars=2000)
    _seed_rules(replay)
    _run(_start(), monkeypatch)
    # What the forked read returns may arrive after the hook stopped
    # waiting; it must leave nothing leased that nobody will print.
    late = routing._child_page_push_inline("s1", "a1", replay.now, lease=False)
    assert late and late["text"]
    assert not routing_delivery.snapshot("s1", "agent:a1").get("flight")
    replay.advance(120)
    page = _ctx(_run(_tool(), monkeypatch))
    assert page.splitlines()[0] == late["text"].splitlines()[0]
    state = routing_delivery.snapshot("s1", "agent:a1")
    assert not state.get("expired")
    assert not state.get("flight")


def test_a_lease_taken_by_another_hook_is_not_printed_twice(replay, monkeypatch):  # noqa: F811
    replay.configure(routing_prompt_chars=2000)
    _seed_rules(replay)
    _run(_start(), monkeypatch)
    first = routing._child_page_push_inline("s1", "a1", replay.now, lease=False)
    second = routing._child_page_push_inline("s1", "a1", replay.now, lease=False)
    session = first["session"]
    assert routing_delivery.lease_page(session, "agent:a1", first["lease"], now=replay.now)
    assert not routing_delivery.lease_page(session, "agent:a1", second["lease"], now=replay.now)
    assert routing_delivery.acknowledge(session, first["routing_changes"], now=replay.now)


def test_cold_imports_are_paid_before_the_page_reads_budget(replay, monkeypatch):  # noqa: F811
    replay.configure(routing_prompt_chars=2000, routing_prompt_budget_ms=100)
    _seed_rules(replay)
    _run(_start(), monkeypatch)
    warm = {"done": False}
    read = routing._child_page_push_inline

    def imports():
        warm["done"] = True

    def cold_read(*args, **kwargs):
        if not warm["done"]:
            time.sleep(0.4)  # a cold interpreter importing what the read needs
        return read(*args, **kwargs)

    monkeypatch.setattr(routing, "prompt_imports", imports)
    monkeypatch.setattr(routing, "_child_page_push_inline", cold_read)
    assert _ctx(_run(_tool(), monkeypatch)).startswith("Rule 2 page 1/")


def test_a_child_paged_on_each_tool_call_never_reads_as_stalled(replay, monkeypatch):  # noqa: F811
    replay.configure(routing_prompt_chars=400, routing_child_chars=300)
    routing.report(account="Codex", state="limited", until="1h", author=AUTHOR)
    routing.add_hold("wait for the child fixture", scope="account", account="Codex",
                     reason="keep", author=AUTHOR)
    _seed_rules(replay)
    routing.set_note("synthetic note " + "n" * 1500, via="app")
    _run(_start(), monkeypatch)
    pages = 0
    for _ in range(80):
        replay.advance(5)
        if not _ctx(_run(_tool(), monkeypatch)):
            break
        pages += 1
        health = _health(replay)
        assert health["status"] == "green", (pages, health)
    assert pages > 10
    state = routing_delivery.snapshot("s1", "agent:a1")
    assert not [e["entity"] for e in state.get("entries", {}).values()
                if not e.get("acked") and (e.get("whole") or e.get("child_owed"))]


def test_a_line_first_seen_on_a_tool_call_is_not_owed_to_the_child(replay, monkeypatch):  # noqa: F811
    replay.configure(routing_prompt_chars=2000)
    routing.add_rule("small synthetic rule", via="app")
    _run(_start(), monkeypatch)
    routing.report(account="Codex", state="limited", until="1h", author=AUTHOR)
    for _ in range(8):
        replay.advance(5)
        _run(_tool(), monkeypatch)
    assert _health(replay)["status"] == "green"


def test_a_live_child_that_never_receives_its_pages_still_goes_red(replay, monkeypatch):  # noqa: F811
    replay.configure(routing_prompt_chars=2000)
    _seed_rules(replay)
    _run(_start(), monkeypatch)
    for _ in range(12):
        replay.advance(70)
        # Every read arrives too late for its hook: nothing is printed.
        routing._child_page_push_inline("s1", "a1", replay.now, lease=False)
    health = _health(replay)
    assert health["status"] == "red"
    assert "budget-stall" in health["reasons"]
    assert RULES  # the deferred rules were owed throughout


def test_a_page_read_in_process_is_leased_and_carries_no_lease_payload(replay, monkeypatch):  # noqa: F811
    # The privacy tests read child pages in process and scan every string in
    # them: the page is leased (so it acknowledges) and holds nothing beyond
    # what the child is shown, no flight or delivery plan.
    replay.configure(routing_prompt_chars=2000)
    _seed_rules(replay)
    _run(_start(), monkeypatch)
    page = routing._child_page_push_inline("s1", "a1", replay.now)
    assert page and page["text"]
    assert "lease" not in page
    assert routing_delivery.snapshot("s1", "agent:a1").get("flight")
    assert routing_delivery.acknowledge(page["session"], page["routing_changes"], now=replay.now)
    replay.advance(5)
    printed = routing.child_page_push("s1", "a1", now=replay.now)
    assert printed and "lease" not in printed
