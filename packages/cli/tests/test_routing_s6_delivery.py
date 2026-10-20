"""Hook reservations and actual delivery, using isolated routing state only."""
from __future__ import annotations

import copy
import contextlib
import time

import pytest
from concurrent.futures import ThreadPoolExecutor

from khipu import agent_view, recall_prompt, recall_rule, routing, routing_delivery
from tests.test_routing_acceptance import AUTHOR, replay  # noqa: F401


def test_repeated_tildes_are_words_but_home_paths_still_scrub(replay):
    words = "Grok " + "~" * 2995
    assert agent_view.scrub(words) == words
    assert "~" not in agent_view.scrub("home ~ and ~/private-file")


def test_reservation_retries_do_not_ack_and_lease_reissues_same_page(replay):
    routing.request_rule("Grok " + "z" * 3000, author=AUTHOR)
    before = dict(recall_prompt.routing_marker("s1"))
    first, context, info = replay.offer()
    assert "_delivery_view" not in routing_delivery.snapshot("s1")["flight"]["out"]
    second, _, _ = replay.offer()
    assert second["routing_changes"] == first["routing_changes"]
    assert second["text"] == first["text"]
    replay.ack(info, delivered=False)
    assert dict(recall_prompt.routing_marker("s1")) == before
    replay.advance(60)
    again, _, _ = replay.offer()
    assert again["routing_changes"]["batch"] != first["routing_changes"]["batch"]
    assert again["routing_changes"]["items"] == first["routing_changes"]["items"]
    assert not routing_delivery.acknowledge("s1", first["routing_changes"])
    assert context


def test_components_and_sessions_cannot_consume_each_others_batches(replay):
    routing.request_rule("Grok " + "z" * 3000, author=AUTHOR)
    main, _, info = replay.offer()
    agent_view.prepare_routing_table(refresh=True)
    child = routing.prompt_push(recall_prompt.routing_marker("s1", "agent:child"), budget=1.0)
    assert child["routing_changes"]["component"] == "agent:child"
    assert child["routing_changes"]["items"] == main["routing_changes"]["items"]
    assert not routing_delivery.acknowledge("other", main["routing_changes"])
    wrong_component = dict(main["routing_changes"], component="agent:child")
    assert not routing_delivery.acknowledge("s1", wrong_component)
    replay.ack(info)
    assert routing_delivery.acknowledge("s1", main["routing_changes"])
    assert routing_delivery.acknowledge("s1", child["routing_changes"])
    assert not recall_prompt.routing_marker("s1", "agent:child")


def test_overlapping_offers_reserve_exactly_one_batch(replay):
    routing.report(account="Codex", state="open", author=AUTHOR)
    agent_view.prepare_routing_table(refresh=True)
    out = routing._prompt_push({}, budget=1.0)
    view = out.pop("_delivery_view")
    planned = routing_delivery.plan(out, view, replay.now, routing.settings())
    with ThreadPoolExecutor(max_workers=2) as pool:
        offers = list(pool.map(lambda _: routing_delivery.offer(copy.deepcopy(planned), session="s1"), range(2)))
    assert offers[0]["routing_changes"] == offers[1]["routing_changes"]


def test_revision_change_cancels_unacknowledged_batch(replay):
    routing.request_rule("Grok " + "x" * 3000, author=AUTHOR)
    first, _, info = replay.offer()
    routing.edit_rule(1, "Grok " + "y" * 3000, via="app")
    newer, _, _ = replay.offer()
    assert newer["routing_changes"]["items"][0]["page"]["n"] == 1
    assert newer["routing_changes"]["items"][0]["revision"] != first["routing_changes"]["items"][0]["revision"]
    replay.ack(info)
    assert not recall_prompt.routing_marker("s1")
    state = routing_delivery.snapshot("s1")
    entity = first["routing_changes"]["items"][0]["entity"]
    assert not state["entries"][entity]["acked"]
    assert state["entries"][entity]["offset"] == 0
    again, context, info = replay.offer()
    assert again["routing_changes"] == newer["routing_changes"]
    assert "y" * 100 in context
    replay.ack(info)
    assert routing_delivery.snapshot("s1")["entries"][entity]["offset"] > 0


def test_owner_words_reassemble_byte_identically_with_unicode_and_paths(replay):
    words = ("Owner /Users/example/kept-path.\nEmoji 😀 and accent e\u0301. " * 220)
    routing.add_rule(words, via="app")
    payloads = []
    pages = []
    for _ in range(100):
        out, context, info = replay.offer()
        if not context:
            break
        assert recall_rule._utf16_len(context) < 10000
        if out.get("routing_changes", {}).get("items", [{}])[0].get("page"):
            assert recall_rule._utf16_len(context.split("\n", 1)[1]) <= (1500 if out["kind"] == "full" else 400)
        changes = out["routing_changes"]
        state = routing_delivery.snapshot("s1")
        for item in changes["items"]:
            if item["entity"].startswith("rule:"):
                entry = state["entries"][item["entity"]]
                end = state["flight"]["progress"][item["entity"]]
                payload = entry["text"][entry["offset"]:end]
                assert payload in context
                payloads.append(payload)
                pages.append(item.get("page"))
        replay.ack(info)
    assert "".join(payloads) == "1. " + words
    assert pages[0]["n"] == 1
    assert [p["n"] for p in pages] == list(range(1, len(pages) + 1))
    assert {p["m"] for p in pages} == {len(pages)}
    assert replay.view()["rules"][0]["text"] == words
    assert not recall_prompt.routing_marker("s1").get("owed")


def test_session_start_and_prompts_share_the_same_reservation(replay):
    routing.add_rule("x" * 10500, via="app")
    start = recall_rule._RoutingAtStart(session_id="s1").begin()
    assert start.text()
    first = start.out
    prompt, _, _ = replay.offer()
    assert prompt["routing_changes"] == first["routing_changes"]
    start.delivered("s1")
    second, _, info = replay.offer()
    assert second["routing_changes"]["items"][0]["page"]["n"] == 2
    replay.ack(info)
    resumed = recall_rule._RoutingAtStart(session_id="s1").begin()
    assert resumed.text()
    assert resumed.out["routing_changes"]["items"][0]["page"]["n"] == 3


def test_expired_ack_without_a_retry_cannot_consume_debt(replay):
    routing.report(account="Codex", state="open", author=AUTHOR)
    out, _, _ = replay.offer()
    replay.advance(60)
    assert not routing_delivery.acknowledge("s1", out["routing_changes"])
    assert not recall_prompt.routing_marker("s1")
    entity = out["routing_changes"]["items"][0]["entity"]
    state = routing_delivery.snapshot("s1")
    assert not state["entries"][entity]["acked"]
    assert state["entries"][entity]["offset"] == 0
    again, context, info = replay.offer()
    assert again["routing_changes"]["items"] == out["routing_changes"]["items"]
    assert again["routing_changes"]["batch"] != out["routing_changes"]["batch"]
    assert context
    replay.ack(info)
    assert routing_delivery.snapshot("s1")["entries"][entity]["acked"]


def test_projection_checks_paths_once_per_read_and_refreshes_between_reads(replay, monkeypatch):
    path = str(replay.root / "private-login")
    replay.configure(usage_accounts=[{"label": "Work", "harness": "codex", "home": path}])
    view = replay.view()
    keys = [entry["key"] for entry in view["accounts"]]
    agent_view.prepare_routing_table(refresh=True)
    original = agent_view._signature
    calls = []
    def signature(extra_keys):
        calls.append(True)
        return original(extra_keys)
    monkeypatch.setattr(agent_view, "_signature", signature)
    with agent_view.routing_projection(keys):
        for _ in range(3):
            assert path not in routing._for_agents(path, view)
    assert len(calls) == 1
    other = str(replay.root / "new-private-login")
    replay.configure(usage_accounts=[{"label": "Work", "harness": "codex", "home": other}])
    view = replay.view()
    keys = [entry["key"] for entry in view["accounts"]]
    # The helper refreshes between these two independently rendered prompts.
    agent_view.prepare_routing_table(refresh=True)
    calls.clear()
    with agent_view.routing_projection(keys):
        assert other not in routing._for_agents(other, view)
    assert len(calls) == 1


def test_crowded_recall_repacks_an_unshown_batch_without_losing_words(replay):
    words = "owner words " * 580
    routing.add_rule(words, via="app")
    agent_view.prepare_routing_table(refresh=True)
    out = routing.prompt_push(recall_prompt.routing_marker("s1"), budget=1.0)
    original = out["routing_changes"]
    at = recall_prompt._RoutingAtPrompt()
    at.session = "s1"
    ctx, info = at.add_to("memory " + "m" * 7800, dict(out, at=routing._iso(replay.now)))
    assert recall_rule._utf16_len(ctx) <= recall_rule._HOOK_CONTEXT_BUDGET
    assert info["pending"]["routing_changes"]["batch"] != original["batch"]
    assert not routing_delivery.acknowledge("s1", original)
    state = routing_delivery.snapshot("s1")
    entity = routing._rule_component(replay.view()["rules"][0])
    end = state["flight"]["progress"][entity]
    payloads = [state["entries"][entity]["text"][:end]]
    assert payloads[0] in ctx
    assert entity not in info["pending"]["shown"]
    replay.ack(info)
    for _ in range(50):
        out, ctx, info = replay.offer()
        if not ctx:
            break
        state = routing_delivery.snapshot("s1")
        if entity in state["flight"]["progress"]:
            start = state["entries"][entity]["offset"]
            end = state["flight"]["progress"][entity]
            payload = state["entries"][entity]["text"][start:end]
            assert payload in ctx
            payloads.append(payload)
        replay.ack(info)
    assert "".join(payloads) == "1. " + words
    assert not recall_prompt.routing_marker("s1").get("owed")


def test_ack_write_failure_is_fail_open_and_keeps_the_reservation(replay, monkeypatch):
    import contextlib

    routing.report(account="Codex", state="open", author=AUTHOR)
    out, _, info = replay.offer()
    @contextlib.contextmanager
    def unavailable(_session):
        raise OSError("synthetic ledger unavailable")
        yield  # pragma: no cover
    with monkeypatch.context() as patch:
        patch.setattr(routing_delivery, "_transaction", unavailable)
        replay.ack(info)
    assert not recall_prompt.routing_marker("s1")
    again, _, info = replay.offer()
    assert again["routing_changes"] == out["routing_changes"]
    replay.ack(info)
    assert not recall_prompt.routing_marker("s1").get("owed")


def test_start_pages_leave_room_for_the_mandatory_memory_rules(replay):
    routing.add_rule("x" * 7000, via="app")
    start = recall_rule._RoutingAtStart(session_id="s1").begin()
    text = start.text()
    ctx, pushed = recall_rule.fit_start_context(recall_rule.RULE_MD.strip(), text)
    assert pushed
    assert recall_rule._utf16_len(ctx) <= recall_rule._HOOK_CONTEXT_BUDGET
    assert "x" * 100 in text and "page 1/" in text
    start.delivered("s1")
    assert recall_prompt.routing_marker("s1")["owed"]


@pytest.mark.parametrize("partial", [False, True])
def test_new_hold_invalidates_rule_pages_and_arrives_on_next_prompt(replay, partial):
    routing.request_rule("Grok builds " + "x" * 10000, author=AUTHOR)
    first, _, info = replay.offer()
    entity = first["routing_changes"]["items"][0]["entity"]
    if partial:
        replay.ack(info)
        replay.offer()
    hold = routing.add_hold("Launch no new Grok jobs", scope="account", account="Grok",
                            reason="keep", author=AUTHOR)
    newer, context, _ = replay.offer()
    state = routing_delivery.snapshot("s1")
    assert "contradicts active hold" in state["entries"][entity]["text"]
    assert state["entries"][entity]["revision"] != first["routing_changes"]["items"][0]["revision"]
    assert hold["id"] in context
    assert any(i["entity"] == "hold:" + hold["id"] for i in newer["routing_changes"]["items"])


def test_level_suffix_invalidates_live_rule_page(replay):
    routing.request_rule("Codex is low. " + "x" * 3000, author=AUTHOR)
    first, _, _ = replay.offer()
    routing.report(account="Codex", state="open", author=AUTHOR)
    replay.usage(used=4)
    newer, _, _ = replay.offer()
    entity = first["routing_changes"]["items"][0]["entity"]
    state = routing_delivery.snapshot("s1")
    assert "stale level claim" in state["entries"][entity]["text"]
    assert state["entries"][entity]["revision"] != first["routing_changes"]["items"][0]["revision"]
    assert newer["routing_changes"]["batch"] != first["routing_changes"]["batch"]


def test_prompt_offer_serializes_with_ack_instead_of_reusing_unlocked_snapshot(replay, monkeypatch):
    routing.request_rule("x" * 3000, author=AUTHOR)
    first, _, _ = replay.offer()
    snapshot = routing_delivery.snapshot
    transaction = routing_delivery._transaction
    acked = []
    def ack_once():
        if not acked:
            acked.append(True)
            assert routing_delivery.acknowledge("s1", first["routing_changes"])
    def stale_snapshot(*args, **kwargs):
        state = snapshot(*args, **kwargs)
        ack_once()
        return state
    @contextlib.contextmanager
    def racing_transaction(session):
        ack_once()
        with transaction(session) as data:
            yield data
    monkeypatch.setattr(routing_delivery, "snapshot", stale_snapshot)
    monkeypatch.setattr(routing_delivery, "_transaction", racing_transaction)
    second, _, _ = replay.offer()
    assert acked
    assert second["routing_changes"]["batch"] != first["routing_changes"]["batch"]
    assert second["routing_changes"]["items"][0]["page"]["n"] == 2


@pytest.mark.parametrize("entity_kind", ["note", "rule"])
@pytest.mark.parametrize("expired", [False, True])
def test_full_summary_replaces_deleted_entity_debt(replay, entity_kind, expired):
    words = "OLD OWNER WORDS " * 250
    if entity_kind == "note":
        routing.set_note(words, via="app")
    else:
        routing.add_rule(words, via="app")
    first, _, _ = replay.offer()
    entity = next(i["entity"] for i in first["routing_changes"]["items"] if i["entity"].startswith(entity_kind))
    if entity_kind == "note":
        routing.end_note(via="app")
    else:
        routing.retire_rule(1, via="app")
    if expired:
        replay.advance(61)
    # No marker was acknowledged: this must take the full-summary path.
    newer, context, info = replay.offer()
    assert newer["kind"] == "full"
    assert "OLD OWNER WORDS" not in context
    state = routing_delivery.snapshot("s1")
    assert "OLD OWNER WORDS" not in state["entries"][entity]["text"]
    assert "ended" in context if entity_kind == "note" else "retired" in context
    assert not routing_delivery.acknowledge("s1", first["routing_changes"])
    replay.ack(info)
    assert routing_delivery.snapshot("s1")["entries"][entity]["acked"]


def test_stalled_volume_refresh_keeps_prompt_routing_and_restores_alarm(replay, monkeypatch):
    import signal
    routing.add_rule("Grok builds", via="app")
    view = replay.view()
    routing._entry_definitions()
    agent_view.prepare_routing_table(refresh=True)
    original = agent_view.os.listdir
    visited = []
    def stalled(path):
        if str(path) == "/Volumes":
            visited.append(True)
            time.sleep(0.2)
            return []
        return original(path)
    monkeypatch.setattr(agent_view.os, "listdir", stalled)
    handler = signal.getsignal(signal.SIGALRM)
    start = time.monotonic()
    out, context, _ = replay.offer(helper_tick=False)
    elapsed = time.monotonic() - start
    assert visited
    assert elapsed < 0.15, elapsed
    assert "Grok builds" in context
    assert out["version"] == view["version"]
    assert signal.getsignal(signal.SIGALRM) == handler
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0


def test_ack_history_is_bounded_and_recent_ack_is_idempotent(replay):
    routing.add_rule("x" * 100000, via="app")
    # This direct renderer call starts with the complete inventory a helper prepares.
    agent_view.prepare_routing_table(refresh=True)
    batches = []
    for _ in range(140):
        out, _, info = replay.offer()
        batches.append(out["routing_changes"])
        replay.ack(info)
    state = routing_delivery.snapshot("s1")
    assert len(state["batches"]) <= 128
    assert routing_delivery.acknowledge("s1", batches[-1])
    assert not routing_delivery.acknowledge("s1", batches[0])
    assert not state["entries"][batches[-1]["items"][0]["entity"]]["acked"]


def test_refit_page_total_never_claims_completion_with_text_still_owed(replay):
    routing.add_rule("x" * 1800, via="app")
    out, _, info = replay.offer()
    replay.ack(info)
    for _ in range(50):
        agent_view.prepare_routing_table(refresh=True)
        out = routing.prompt_push(recall_prompt.routing_marker("s1"), budget=1.0)
        if not out.get("routing_changes"):
            break
        out = routing_delivery.refit("s1", out["routing_changes"], 200)
        assert out and out.get("routing_changes")
        state = routing_delivery.snapshot("s1")
        for item in out["routing_changes"]["items"]:
            if item.get("page"):
                end = state["flight"]["progress"][item["entity"]]
                length = len(state["entries"][item["entity"]]["text"])
                assert (item["page"]["n"] == item["page"]["m"]) == (end == length)
        assert routing_delivery.acknowledge("s1", out["routing_changes"])
    else:
        pytest.fail("refitted pages did not finish")


@pytest.mark.parametrize("entity_kind", ["note", "rule"])
def test_session_start_plan_reconciles_deleted_debt_without_the_view(replay, entity_kind):
    words = "OLD OWNER WORDS " * 250
    if entity_kind == "note":
        routing.set_note(words, via="app")
    else:
        routing.add_rule(words, via="app")
    first, _, _ = replay.offer()
    if entity_kind == "note":
        routing.end_note(via="app")
    else:
        routing.retire_rule(1, via="app")
    out = routing.start_push()
    assert "_delivery_view" not in out
    newer = routing_delivery.offer(out, session="s1")
    assert "OLD OWNER WORDS" not in newer["text"]
    assert not routing_delivery.acknowledge("s1", first["routing_changes"])
    assert routing_delivery.acknowledge("s1", newer["routing_changes"])


def test_stalled_refresh_keeps_known_aliases_without_retired_owner_exemptions(replay, monkeypatch):
    target = replay.root / "PrivateAlias9"
    target.mkdir()
    login = replay.root / "configured-login"
    login.symlink_to(target, target_is_directory=True)
    replay.configure(usage_accounts=[{"label": "Work", "harness": "codex", "home": str(login)}])
    routing.add_rule("Use PrivateAlias9", via="app")
    view = replay.view()
    keys = [a["key"] for a in view["accounts"]]
    agent_view.prepare_routing_table(refresh=True)
    routing.retire_rule(1, via="app")
    def stalled(_keys):
        time.sleep(0.2)
        raise AssertionError("refresh deadline was not enforced")
    monkeypatch.setattr(agent_view, "_signature", stalled)
    with agent_view.routing_projection(keys):
        assert "PrivateAlias9" not in routing._for_agents("Use PrivateAlias9", replay.view())


def test_projection_deadline_cannot_escape_during_alarm_restoration(replay, monkeypatch):
    import signal
    routing.report(account="Codex", state="open", author=AUTHOR)
    handler = signal.getsignal(signal.SIGALRM)
    install = signal.signal
    restored = []
    def late_alarm(sig, value):
        if sig == signal.SIGALRM and value == handler:
            restored.append(True)
            signal.getsignal(sig)(sig, None)
        return install(sig, value)
    monkeypatch.setattr(signal, "signal", late_alarm)
    try:
        out, context, _ = replay.offer()
        assert restored
        assert "Codex" in context
        assert out.get("routing_changes")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        install(signal.SIGALRM, handler)
