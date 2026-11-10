"""Three owner-change losses: a new rule with ``base`` silently crossed; a
crossed notice that names only "rule N"; and owner confirm under load that
refuses as a dispatched job after a failed T3 read.

Hermetic: throwaway HOME via fixtures, ``KHIPU_KEYCHAIN=0`` from the runner,
synthetic T3 DB, never the real home's Khipu state.
"""
from __future__ import annotations

import json
import time

import pytest

from khipu import mcp_server, owner_direction, routing, t3
from tests.test_routing_s5_fix1 import ACCOUNTS, Clock, ME, T0
from tests.test_t3_owner_confirm import (T3_SESSION, THREAD, _be, _confirm_id, _hold_in_t3, _paused,
                                         _t3_hook_turn, world)


@pytest.fixture
def home(tmp_path, monkeypatch, khipu_app):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": ACCOUNTS}))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("KHIPU_SESSION_ID", ME["session"])
    monkeypatch.setenv("KHIPU_HARNESS", "claude_code")
    clock = Clock(T0)
    monkeypatch.setattr(routing, "_now", clock)
    return {"data": data, "clock": clock, "tmp": tmp_path}


# ---- 1. a new rule sent with base is always lost -----------------------------------------------


def test_a_new_rule_with_base_is_live_not_crossed(home):
    """An addition has nothing to be stale against: a caller-sent ``base`` is
    not recorded, so the request becomes a live pending rule."""
    routing.add_rule("An existing rule.", via="app")
    shown = mcp_server._tool_route({"full": True})
    # Agents often send a revision they just read; for an add that value is
    # never the new rule's owner revision ("0"), and today marks it crossed.
    bogus = shown["rules"][0]["owner_revision"]
    assert bogus and bogus != "0"
    out = mcp_server._tool_route_rule({
        "text": "Queue new jobs when a Mac's load per core is above about 2x.",
        "owner_request": "restore the load-balancing rule",
        "base": bogus,
    })
    assert out.get("ok") is True, out
    assert out.get("status") != "crossed", out
    view = routing.read_view()
    live = [r for r in view["rules"] if "load per core" in r["text"]]
    assert len(live) == 1 and live[0]["pending"] is True, view["rules"]
    assert not any(p["status"] == "crossed" for p in view["proposals"].values()), view["proposals"]
    stored = [e for e in routing.read_events()[0] if e.get("type") == "rule_add"
              and "load per core" in (e.get("text") or "")]
    assert stored and (stored[-1].get("base") in (None, "0")), stored[-1]


def test_a_crossed_rule_edit_answer_says_it_did_not_take_effect(home):
    routing.add_rule("OWNER E1", via="app")
    observed = mcp_server._tool_route({"full": True})["rules"][0]["owner_revision"]
    routing.edit_rule(1, "OWNER E2", via="app")
    out = mcp_server._tool_route_rule({
        "text": "Stale proposal.", "rule": 1,
        "owner_request": "change rule 1 to say Stale proposal",
        "base": observed,
    })
    assert out.get("ok") is False, out
    assert out.get("status") == "crossed" or "crossed" in (out.get("error") or "").lower(), out
    assert "never took effect" in (out.get("error") or "").lower() or "did not take effect" in (
        out.get("error") or "").lower(), out
    view = routing.read_view()
    assert view["rules"][0]["text"] == "OWNER E2" and not view["rules"][0]["pending"]


def test_a_crossed_priority_answer_says_it_did_not_take_effect(home):
    routing.set_priority([{"account": "Claude"}], via="app", expect=routing.priority_version())
    observed = mcp_server._tool_route({"full": True})["priority_owner_revision"]
    routing.set_priority([{"account": "Codex"}], via="app", expect=routing.priority_version())
    out = mcp_server._tool_route_priority({
        "entries": [{"account": "Grok"}],
        "owner_request": "put Grok first for the week",
        "base": observed,
    })
    assert out.get("ok") is False, out
    assert "crossed" in (out.get("status") or out.get("error") or "").lower(), out
    assert [e["label"] for e in routing.read_view()["priority"]["entries"]] == ["Codex"]


def test_an_old_rule_add_with_a_real_base_stays_crossed_on_replay(home):
    """STOP: fold still crosses historical rule_add events that recorded a
    non-\"0\" base; those must not come back to life."""
    author = dict(ME)
    stamp = T0.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    event = {
        "type": "rule_add", "id": "e-old-crossed-add", "n": 77,
        "text": "OLD LOST ADDITION", "source": "added at the owner's request",
        "date": T0.date().isoformat(), "seeded": False, "pending": True,
        "owner_request": "add the lost load rule", "written_by": author, "via": "mcp",
        "base": "not-zero-revision", "entity": "rule-old-crossed-add",
        "proposal": "proposal-old-crossed-add", "written_at": stamp, "mac": "test-mac",
        "op": "op-old-crossed-add",
    }
    path = routing.events_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, sort_keys=True) + "\n")
    view = routing.read_view()
    crossed = [p for p in view["proposals"].values() if p["status"] == "crossed"]
    assert any(p.get("id") == "proposal-old-crossed-add" for p in crossed), view["proposals"]
    assert not any(r.get("text") == "OLD LOST ADDITION" and not r.get("retired")
                   for r in view["rules"]), view["rules"]


# ---- 2. a crossed notice names the request's own words -----------------------------------------


def test_a_crossed_notice_includes_the_owner_request_words(home, monkeypatch):
    routing.add_rule("OWNER E1", via="app")
    monkeypatch.setenv("KHIPU_SESSION_ID", "notice-author")
    observed = mcp_server._tool_route({"full": True})["rules"][0]["owner_revision"]
    routing.edit_rule(1, "OWNER E2", via="app")
    words = "set Cursor (xAI) to 90 percent for the week"
    mcp_server._tool_route_rule({
        "text": "Cursor (xAI) 90%.", "rule": 1, "owner_request": words, "base": observed,
    })
    view = routing.read_view()
    proposal = next(p for p in view["proposals"].values() if p["status"] == "crossed")
    assert proposal.get("owner_request") == words, proposal
    notices = routing._replacement_notices(view, routing._now(), "notice-author")
    assert notices, "authoring conversation should owe a crossed notice"
    line = notices[0][1]
    assert words in line, line
    assert "crossed" in line and "never took effect" in line, line


# ---- 3. owner confirm under load: longer T3 budget; wrong reason fixed -------------------------


def test_a_failed_t3_lookup_on_confirm_says_unreadable_not_dispatched_job(world, monkeypatch):
    request = _hold_in_t3(world)
    world["t3"].message(THREAD, time.time() + 1)
    _t3_hook_turn()
    # After the thread is known, the delegated-child check fails to read: that
    # is not evidence of a dispatched job.
    monkeypatch.setattr(t3, "session_thread_ids_result", lambda *_a, **_k: ([], False))
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "t3_unreadable", out
    assert "dispatched job" not in out["error"].lower(), out
    assert "could not read" in out["error"].lower() and "try again" in out["error"].lower(), out
    assert not _paused()


def test_a_job_lane_cannot_borrow_a_t3_reply_or_burn_the_owners_answer(world):
    request = _hold_in_t3(world)
    world["t3"].message(THREAD, time.time() + 1)
    _t3_hook_turn()
    _be(world, T3_SESSION, CLAUDE_DISPATCHED_LANE="claude-second")
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "no_recorded_turn" and not _paused(), out
    row = next(r for r in owner_direction._read_requests(owner_direction.requests_path()) if r["id"] == request)
    assert row["state"] == "pending"
    world["mp"].delenv("CLAUDE_DISPATCHED_LANE")
    ok, out = _confirm_id(request)
    assert ok and _paused(), out


def test_owner_confirm_retries_a_flaky_t3_message_read(world, monkeypatch):
    """Hold/confirm get several attempts; a single lock blip must not refuse."""
    request = _hold_in_t3(world)
    mid = world["t3"].message(THREAD, time.time() + 1)
    _t3_hook_turn()
    real = t3.owner_reply_read
    calls = {"n": 0}

    def flaky(thread, seen, since):
        calls["n"] += 1
        if calls["n"] < 3:
            return None, False
        return real(thread, seen, since)

    monkeypatch.setattr(t3, "owner_reply_read", flaky)
    ok, out = _confirm_id(request)
    assert ok, out
    assert calls["n"] >= 3
    assert _paused() and out["confirmed"]["message"] == mid


def test_owner_t3_reads_use_a_longer_busy_budget_than_hooks(world):
    hook = t3._read_budget()
    with t3.owner_read_budget():
        owner = t3._read_budget()
    assert owner[1] > hook[1] and owner[2] > hook[2]
    # Hook path still sees the tight budget after the owner context exits.
    assert t3._read_budget() == hook
    assert not t3.owner_read_active()
