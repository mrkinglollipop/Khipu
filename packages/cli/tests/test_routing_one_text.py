"""One routing text for every reader.

A parent conversation, a subagent at its start, a subagent's later tool
results, ``khipu_status``'s routing field, a pull client and ``khipu route
--summary`` / ``--brief`` are all handed the same routing page, under the
same resend rule (its signature changed), and the delivery ledger still
proves, per conversation and per subagent, that the current page was handed
over, and when not, why (owner, 2 Nov: "I need to be confident that it's
going across conversations").

Hermetic: the page-delivery fixture (a data dir per test, a fake clock).
"""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout

from khipu import (agent_view, cli, recall_prompt, routing, routing_delegation, routing_delivery, routing_health,
                   routing_page_file)
from khipu import mcp_server as ms
from tests.test_routing_page_delivery import ME, _record, home  # noqa: F401

PARENT = "11111111-2222-3333-4444-555555555555"


def _hook(payload, monkeypatch) -> dict:
    """One run of the delegation hook entry, as the harness runs it: what it
    printed (and, after printing, what it recorded)."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    with redirect_stdout(buf):
        routing_delegation.main()
    return json.loads(buf.getvalue())


def _ctx(out: dict) -> str:
    return out["hookSpecificOutput"]["additionalContext"] if out else ""


def _start(agent="a1", **extra) -> dict:
    return {"hook_event_name": "SubagentStart", "session_id": PARENT, "agent_id": agent,
            "agent_type": "probe", "cwd": "/work/project-a", **extra}


def _tool(agent="a1") -> dict:
    return {"hook_event_name": "PreToolUse", "session_id": PARENT, "agent_id": agent, "agent_type": "probe",
            "cwd": "/work/project-a", "tool_name": "Bash", "tool_input": {"command": "cat probe.txt"}}


def _parent_page() -> str:
    """What the parent conversation's prompt is handed, acknowledged."""
    agent_view.prepare_routing_table(refresh=True)
    session = routing_delivery.hook_session(PARENT)
    out = routing.prompt_push(None, budget=1.0, session_id=session)
    assert out.get("routing_changes"), out
    assert routing_delivery.acknowledge(session, out["routing_changes"], take=True)
    return out["text"]


def _ledger() -> dict:
    path = routing_delivery._path(routing_delivery.hook_session(PARENT))
    return json.loads(path.read_text()) if path.exists() else {}


def _revision() -> str:
    return routing_delivery.page_revision(routing_page_file.read()["sig"])


def _rows(home) -> list[dict]:  # noqa: F811
    view = routing.read_view()
    return routing_health.doctor_block(view, routing._now(), routing.settings())["identities"]


# ---- 1. subagents get the page ------------------------------------------------------------


def test_a_subagent_at_its_start_gets_the_parents_page_and_its_component_records_it(home, monkeypatch):  # noqa: F811
    _record()
    parent = _parent_page()
    out = _hook(_start(), monkeypatch)
    assert _ctx(out) == parent  # the same text, scrubbed the same way: never more than the parent sees
    assert out["hookSpecificOutput"]["hookEventName"] == "SubagentStart"
    assert "standing rules left" not in _ctx(out)
    state = _ledger()["agent:a1"]
    # Once acknowledged, a subagent's entry is kept as revision-only proof.
    assert set(state.get("entries") or {}) | set(state.get("done") or {}) == {routing_delivery.PAGE_ENTITY}
    assert routing_delivery.page_receipt(state, _revision(), routing._now())["page"] == "handed_over"
    # The parent's own component never moved for the subagent.
    assert routing_delivery.page_receipt(_ledger()["main"], _revision(), routing._now())["page"] == "handed_over"


def test_a_subagents_tool_result_gets_the_page_again_only_when_it_changed(home, monkeypatch):  # noqa: F811
    _record()
    _parent_page()
    _hook(_start(), monkeypatch)
    assert _hook(_tool(), monkeypatch) == {}  # nothing changed since its start
    home["clock"].advance(minutes=2)
    routing.report(account="Codex", state="open", author=ME)  # a status word changes: a new page
    changed = routing_page_file.read()["text"]
    out = _hook(_tool(), monkeypatch)
    assert _ctx(out).startswith(changed.split("\n", 1)[0]) and "Codex: LIMIT REACHED" not in _ctx(out)
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in out["hookSpecificOutput"]
    assert _hook(_tool(), monkeypatch) == {}  # handed over once, not on every call
    assert routing_delivery.page_receipt(_ledger()["agent:a1"], _revision(), routing._now())["page"] == "handed_over"
    # The parent has not had this page yet: its own row says so.
    assert routing_delivery.page_receipt(_ledger()["main"], _revision(), routing._now())["page"] == "behind"


def test_a_subagent_that_started_on_an_older_khipu_gets_the_page_once(home, monkeypatch):  # noqa: F811
    """An ``agent:`` component from the old summary path (entity per line,
    acknowledged) upgrades on its next tool call: the page goes once, the
    old entries go, and nothing is resent after that."""
    _record()
    _parent_page()
    path = routing_delivery._path(routing_delivery.hook_session(PARENT))
    data = json.loads(path.read_text())
    data["agent:old"] = {"entries": {"rule:1": {"entity": "rule:1", "text": "an old rule", "revision": "r1",
                                                "whole": True, "acked": True, "offset": 11, "shown_offset": 11}},
                         "delivery_points": 3, "last_delivery_point": routing._iso(routing._now()),
                         "harness": "claude_code", "slack": 2}
    path.write_text(json.dumps(data))
    first = _hook(_tool("old"), monkeypatch)
    assert _ctx(first) == routing_page_file.read()["text"]
    state = _ledger()["agent:old"]
    assert "rule:1" not in (state.get("entries") or {})
    assert set(state.get("entries") or {}) | set(state.get("done") or {}) >= {routing_delivery.PAGE_ENTITY}
    assert _hook(_tool("old"), monkeypatch) == {}


def test_a_codex_child_gets_the_page_as_a_snapshot_reserved_nowhere(home, monkeypatch):  # noqa: F811
    _record()
    parent = _parent_page()
    out = _hook(_start(harness="codex"), monkeypatch)
    assert _ctx(out) == parent
    assert not [name for name in _ledger() if name.startswith("agent:")]
    # Its later routing comes from its own pulls, never from the parent's ledger.
    assert _hook(dict(_tool(), harness="codex"), monkeypatch) == {}


def test_khipu_status_routing_field_is_the_page(home):  # noqa: F811
    _record()
    parent = _parent_page()
    field = ms._routing_light()
    assert field["text"] == parent
    assert set(field) == {"text", "version", "epoch", "rendered_at", "about"}
    # A read, not a delivery: nothing reserved or acknowledged for it.
    before = _ledger()
    ms._routing_light()
    assert _ledger() == before


def test_a_pull_client_is_offered_the_page_as_one_entity(home):  # noqa: F811
    view = _record()
    now = routing._now()
    identity = {"session": "codex:child-thread", "capability": "pull_only", "harness": "codex"}
    with agent_view.pull_projection():
        changes, delivery = ms._route_pull_attachment(routing, routing_delivery, identity, now, view=view)
    assert [item["entity"] for item in changes["items"]] == [routing_delivery.PAGE_ENTITY]
    assert changes["items"][0]["text"] == routing_page_file.render(view, now)[0]
    assert "page" not in changes["items"][0]  # whole, never paged at the old 400-character limit
    assert delivery["in_flight"] == 1


def test_route_summary_and_brief_print_the_page(home, monkeypatch, capsys):  # noqa: F811
    _record()
    page = routing_page_file.read()["text"]
    for flag in ("--summary", "--brief"):
        assert cli.main(["route", flag]) == 0
        printed = capsys.readouterr().out
        assert printed.startswith("ROUTING ") and "Codex: LIMIT REACHED" in printed
        assert printed.split("\n", 1)[0].split(". ", 1)[1] == page.split("\n", 1)[0].split(". ", 1)[1]


def test_a_brief_carrying_the_page_counts_as_carrying_routing(home):  # noqa: F811
    _record()
    page = routing_page_file.read()["text"]
    assert routing_delegation._brief_has_routing("Do the job.\n\n" + page)
    assert not routing_delegation._brief_has_routing("Check the ROUTING page before you start.")


# ---- 2. delivery is still proven ------------------------------------------------------------


def test_doctor_rows_say_per_conversation_and_per_subagent_that_the_page_was_handed_over(home, monkeypatch):  # noqa: F811
    _record()
    _parent_page()
    _hook(_start(), monkeypatch)
    rows = {row["component"]: row for row in _rows(home)}
    assert rows["main"]["page"] == "handed_over" and rows["main"]["page_why"] is None
    assert rows["agent:a1"]["page"] == "handed_over"
    view = routing.read_view()
    block = routing_health.doctor_block(view, routing._now(), routing.settings())
    assert block["page"]["revision"] == _revision() and block["page"]["handed_over"] == 2
    home["clock"].advance(minutes=2)
    routing.report(account="Codex", state="open", author=ME)
    rows = {row["component"]: row for row in _rows(home)}
    assert rows["main"]["page"] == rows["agent:a1"]["page"] == "behind"
    assert "next prompt or tool call" in rows["main"]["page_why"]


def test_a_page_the_hook_took_and_never_confirmed_reads_as_owed_and_why(home):  # noqa: F811
    _record()
    session = routing_delivery.hook_session(PARENT)
    agent_view.prepare_routing_table(refresh=True)
    out = routing.prompt_push(None, budget=1.0, session_id=session)
    assert out.get("routing_changes")
    state = _ledger()["main"]
    assert routing_delivery.page_receipt(state, _revision(), routing._now())["page"] == "in_flight"
    home["clock"].advance(minutes=5)  # the lease runs out: the hook was killed before it printed
    receipt = routing_delivery.page_receipt(_ledger()["main"], _revision(), routing._now())
    assert receipt["page"] == "owed" and "never confirmed" in receipt["why"]
    late = routing_delivery.page_receipt({"entries": {}}, _revision(), routing._now(), last_status="skipped:budget")
    assert late["page"] == "never" and "ran out of time" in late["why"]
    pull = routing_delivery.page_receipt(None, _revision(), routing._now(), harness="cursor")
    assert "khipu_route" in pull["why"]


def test_khipu_route_delivery_block_names_the_conversation_and_counts_its_subagents(home, monkeypatch):  # noqa: F811
    _record()
    _parent_page()
    _hook(_start("alpha"), monkeypatch)
    _hook(_start("beta"), monkeypatch)
    out = ms._route_page(routing, {"session": routing_delivery.hook_session(PARENT)})
    delivery = out["delivery"]
    assert delivery["revision"] == _revision() and delivery["readable"]
    assert delivery["main"]["page"] == "handed_over"
    assert delivery["subagents"]["count"] == 2 and delivery["subagents"]["handed_over"] == 2
    assert PARENT not in json.dumps(delivery) and "alpha" not in json.dumps(delivery)
    assert ms._route_page(routing, None)["delivery"] == "untracked: pass session_id"


# ---- ported from the child-block tests (slice 8a): what still holds for the page -------------


def test_a_child_start_without_an_agent_id_gets_the_page_and_reserves_nowhere(home, monkeypatch):  # noqa: F811
    _record()
    payload = _start()
    del payload["agent_id"]
    assert _ctx(_hook(payload, monkeypatch)) == routing_page_file.read()["text"]
    assert routing_delivery.snapshot(routing_delivery.hook_session(PARENT), "main") == {}
    assert routing_delivery.snapshot(routing_delivery.hook_session(PARENT), "agent:unknown") == {}


def test_per_agent_ledgers_stay_bounded_finished_and_stale(home):  # noqa: F811
    _record()
    session = routing_delivery.hook_session(PARENT)

    def start(name):
        answer = routing._start_page(routing.read_view(), routing._now(), component=f"agent:{name}", session=session)
        assert routing_delivery.acknowledge(session, answer["routing_changes"], take=True)

    start("old")
    home["clock"].advance(days=8)
    start("fresh")
    # A finished component older than the stale age is dropped outright.
    assert routing_delivery.snapshot(session, "agent:old") == {}
    for i in range(40):
        start(f"a{i}")
    path = routing_delivery._path(session)
    assert path.stat().st_size < 1_048_576
    agents = {name: state for name, state in json.loads(path.read_text()).items() if name.startswith("agent:")}
    assert len(agents) <= routing_delivery._AGENT_MAX_COMPONENTS
    # Finished agents keep only revision proof, never the delivered words.
    assert "ACCOUNTS" not in json.dumps(agents)
    assert all(not state.get("entries") for state in agents.values())


def test_a_subagents_tool_call_read_has_the_parents_prompt_deadline(home, monkeypatch):  # noqa: F811
    import time

    from tests.test_routing_page_delivery import _set

    _record()
    _hook(_start(), monkeypatch)
    home["clock"].advance(minutes=2)
    routing.report(account="Codex", state="open", author=ME)
    _set(home, routing_prompt_budget_ms=50)
    real = routing_page_file.read

    def slow():
        time.sleep(2)
        return real()

    monkeypatch.setattr(routing_page_file, "read", slow)
    began = time.monotonic()
    assert _hook(_tool(), monkeypatch) == {}
    assert time.monotonic() - began < 1.0


def test_parallel_tool_calls_do_not_print_the_same_page_twice(home, monkeypatch):  # noqa: F811
    _record()
    _hook(_start(), monkeypatch)
    home["clock"].advance(minutes=2)
    routing.report(account="Codex", state="open", author=ME)
    first = routing.child_page_push(PARENT, "a1")
    assert first and first["text"]
    # Leased before printing: a second call before its ack gets nothing.
    assert routing.child_page_push(PARENT, "a1") is None
    home["clock"].advance(minutes=5)  # that call never confirmed it: after the lease the page goes again
    third = routing.child_page_push(PARENT, "a1")
    assert third and third["routing_changes"]["batch"] != first["routing_changes"]["batch"]
    assert routing_delivery.acknowledge(third["session"], third["routing_changes"], take=True)
    assert routing.child_page_push(PARENT, "a1") is None


def test_no_agent_identity_means_no_per_agent_pages(home, monkeypatch):  # noqa: F811
    _record()
    payload = _tool()
    del payload["agent_id"]
    out, _info, kind = routing_delegation.hook_main(json.dumps(payload))
    # A parent's ordinary (non-delegation) Bash call still gets nothing.
    assert out == {} and kind is None


# ---- ported from the agent-ack tests: a subagent's page is judged like any other delivery ---


def _agent_health(home, agent="a1"):  # noqa: F811
    state = routing_delivery.snapshot(routing_delivery.hook_session(PARENT), f"agent:{agent}")
    return routing_health.component_health(state, routing._now(), slack=2, child=True)


def test_a_subagent_handed_each_new_page_never_reads_as_stalled(home, monkeypatch):  # noqa: F811
    _record()
    _hook(_start(), monkeypatch)
    for i in range(6):
        home["clock"].advance(minutes=2)
        routing.report(account="Codex", state="open" if i % 2 == 0 else "limited", until=None if i % 2 == 0
                       else "1h", author=ME)
        assert _ctx(_hook(_tool(), monkeypatch)).startswith("ROUTING ")
        assert _agent_health(home)["status"] == "green", _agent_health(home)
        assert _hook(_tool(), monkeypatch) == {}


def test_a_live_subagent_whose_reads_always_arrive_late_still_goes_red(home, monkeypatch):  # noqa: F811
    _record()
    _hook(_start(), monkeypatch)
    home["clock"].advance(minutes=2)
    routing.report(account="Codex", state="open", author=ME)
    for _ in range(4):
        home["clock"].advance(seconds=70)
        # Read in the forked child, and its hook had stopped waiting: nothing is printed or confirmed.
        assert routing._child_page_push_inline(PARENT, "a1")
    health = _agent_health(home)
    assert health["status"] == "red" and "lease-expired-twice" in health["reasons"]
    receipt = routing_delivery.page_receipt(routing_delivery.snapshot(routing_delivery.hook_session(PARENT), "agent:a1"),
                                            _revision(), routing._now())
    assert receipt["page"] == "in_flight"
    home["clock"].advance(seconds=70)
    receipt = routing_delivery.page_receipt(routing_delivery.snapshot(routing_delivery.hook_session(PARENT), "agent:a1"),
                                            _revision(), routing._now())
    assert receipt["page"] == "owed" and "never confirmed" in receipt["why"]


def test_cold_imports_are_paid_before_the_page_reads_budget(home, monkeypatch):  # noqa: F811
    import time

    from tests.test_routing_page_delivery import _set

    _record()
    _hook(_start(), monkeypatch)
    home["clock"].advance(minutes=2)
    routing.report(account="Codex", state="open", author=ME)
    _set(home, routing_prompt_budget_ms=100)
    warm = {"done": False}
    read = routing._child_page_push_inline

    def cold_read(*args, **kwargs):
        if not warm["done"]:
            time.sleep(0.4)  # a cold interpreter importing what the read needs
        return read(*args, **kwargs)

    monkeypatch.setattr(routing, "prompt_imports", lambda: warm.update(done=True))
    monkeypatch.setattr(routing, "_child_page_push_inline", cold_read)
    assert _ctx(_hook(_tool(), monkeypatch)).startswith("ROUTING ")


def test_a_page_read_in_process_is_leased_and_carries_nothing_beyond_the_page(home, monkeypatch):  # noqa: F811
    # The privacy tests read subagent pages in process and scan every string
    # in them: nothing beyond what the subagent is shown, no flight or plan.
    _record()
    _hook(_start(), monkeypatch)
    home["clock"].advance(minutes=2)
    routing.report(account="Codex", state="open", author=ME)
    page = routing._child_page_push_inline(PARENT, "a1")
    assert page and page["text"] == routing_page_file.read()["text"]
    assert not {"lease", "delivery_plan", "delivery_reservation", "page", "sig"} & set(page)
    assert routing_delivery.snapshot(page["session"], "agent:a1").get("flight")
    assert routing_delivery.acknowledge(page["session"], page["routing_changes"], take=True)
