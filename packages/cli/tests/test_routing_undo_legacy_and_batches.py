"""Round 4 of the routing-exceptions review (jobs/2026-10-26/route-exceptions-sol-r3): legacy undo
snapshots never restore a conversation's words as the owner's; a rule undo restores the live
proposal, not a crossed one; a restore is all-or-nothing. Each test failed on f21520b6.
Hermetic: a temp data dir and fake accounts."""
from __future__ import annotations

import errno

import pytest

from khipu import owner_act, owner_direction, routing

pytest_plugins = ("tests.test_routing_acceptance", "tests.test_owner_direction")

ME = {"kind": "conversation", "harness": "claude_code", "project": "project-a", "session": "s-one"}


@pytest.fixture
def direction_world(tmp_path, monkeypatch, khipu_app):
    # test_owner_direction's world built from its parts (another module's
    # ``world`` wins by name in a whole-directory run).
    import json
    import sys

    from khipu import owner_turns
    from tests import test_owner_direction as od

    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": od.ACCOUNTS}))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    for name in od._SESSION_VARS:
        monkeypatch.delenv(name, raising=False)
    khipu_app.as_shell("/opt/harness/claude")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    owner_turns.write_settings({"enabled": True})
    w = od.World(tmp_path, monkeypatch)
    w.be(od.OWNER)
    return w


def _owner_priority():
    routing.set_priority([{"account": "Codex", "role": "reviews"}, {"account": "Grok", "role": "audits"}],
                         via="app", expect=routing.priority_version())


def _request():
    base = routing.conversation_view()["priority_owner_revision"]
    routing.request_priority([{"account": "Codex", "role": "CONVERSATION WORDS"}], owner_request="Matt asked",
                             author=ME, expect=routing.priority_version(), base=base)


def _legacy(monkeypatch, shape):
    """Snapshots as code before provenance wrote them."""
    real = owner_direction._before

    def old(want):
        out = real(want)
        return shape(want, out) if isinstance(out, dict) else out
    monkeypatch.setattr(owner_direction, "_before", old)


def _act(world, action, args):
    world.say(f"do {action}")
    ok, applied = owner_act.act(action, args)
    assert ok, applied
    world.say("undo that")
    return owner_act.act("direction.undo", {"id": applied["owner_action"]})


# ---- P1: legacy snapshots --------------------------------------------------------------------------


def _old_priority(want, out):
    return {"entries": out.get("entries"), "until": out.get("until"), "stored": True}


def test_a_legacy_priority_undo_over_a_conversations_request_is_refused(direction_world, monkeypatch):
    _owner_priority()
    _request()
    _legacy(monkeypatch, _old_priority)
    ok, out = _act(direction_world, "route.priority.set", {"entries": [{"account": "Grok", "role": "owner's own"}],
                                                          "expect": routing.priority_version()})
    assert not ok and owner_direction.LEGACY_PROVENANCE in str(out)
    held = routing.read_view()["priority"]
    assert [e["role"] for e in held["entries"]] == ["owner's own"]


def test_a_legacy_priority_undo_is_refused_even_for_the_owners_own(direction_world, monkeypatch):
    # Round 5: no snapshot without the provenance mark is restored, the
    # owner's own included; refusing is the accepted cost.
    _owner_priority()
    _legacy(monkeypatch, _old_priority)
    ok, out = _act(direction_world, "route.priority.clear", {"expect": routing.priority_version()})
    assert not ok and owner_direction.LEGACY_PROVENANCE in str(out)
    assert routing.read_view()["priority"] is None


def test_a_legacy_hold_undo_is_refused(direction_world, monkeypatch):
    hold = routing.add_hold("wait for Grok", reason="keep", scope="account", account="Grok", author=ME, via="mcp")
    _legacy(monkeypatch, lambda want, out: {k: out[k] for k in ("text", "reason", "scope", "until")})
    ok, out = _act(direction_world, "route.hold.end", {"hold": hold["entity"]})
    assert not ok and owner_direction.LEGACY_PROVENANCE in str(out)
    assert not [h for h in routing.read_view()["holds"] if h["text"] == "wait for Grok"]


def _pending_edit():
    routing.add_rule("Owner's rule words", via="app")
    shown = routing.conversation_view()
    routing.request_rule("CONVERSATION RULE WORDS", n=1, owner_request="the owner asked", author=ME, via="mcp",
                         base=shown["rules"][0]["owner_revision"])


def test_a_legacy_rule_undo_over_a_pending_edit_is_refused(direction_world, monkeypatch):
    _pending_edit()
    _legacy(monkeypatch, lambda want, out: {"text": out["text"]} if "text" in out else None)
    ok, out = _act(direction_world, "route.rule.undo", {"rule": "1"})
    assert not ok and owner_direction.LEGACY_PROVENANCE in str(out)
    rule = routing.read_view()["rules"][0]
    assert rule["text"] == "Owner's rule words" and not rule.get("pending")


# ---- P2: the live rule proposal, not a crossed one --------------------------------------------------


def test_undo_restores_the_live_rule_proposal_not_a_later_crossed_one(direction_world):
    routing.add_rule("Owner's rule words", via="app")
    stale = routing.conversation_view()["rules"][0]["owner_revision"]
    routing.edit_rule(1, "Owner's second words", via="app")
    fresh = routing.conversation_view()["rules"][0]["owner_revision"]
    routing.request_rule("LIVE REQUEST A", n=1, owner_request="asked", author=ME, via="mcp", base=fresh)
    routing.request_rule("CROSSED REQUEST B", n=1, owner_request="asked", author=ME, via="mcp", base=stale)
    assert routing.read_view()["rules"][0]["text"] == "LIVE REQUEST A"
    ok, out = _act(direction_world, "route.rule.edit", {"rule": "1", "text": "Owner's third words"})
    assert ok, out
    rule = routing.read_view()["rules"][0]
    assert rule["pending"] is True and rule["text"] == "LIVE REQUEST A"


# ---- P2: a restore is all-or-nothing ----------------------------------------------------------------


def _pending_stored_then_cleared():
    _owner_priority()
    _request()
    stored = routing.read_view()["priority_stored"]
    routing.clear_priority(via="app", expect=routing.priority_version())
    return stored


def _short_write(monkeypatch):
    real = routing._write_many

    def first_line_then_disk_full(events):
        if len(events) > 1:
            real(events[:1])
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(events)
    monkeypatch.setattr(routing, "_write_many", first_line_then_disk_full)


def test_a_short_written_restore_leaves_the_record_as_it_was(direction_world, monkeypatch):
    stored = _pending_stored_then_cleared()
    _short_write(monkeypatch)
    with pytest.raises(OSError):
        routing.restore_priority(stored, via="app")
    assert routing.read_view()["priority"] is None  # the predecessor alone never counts


def test_a_retry_after_a_short_write_completes_the_restore(direction_world, monkeypatch):
    stored = _pending_stored_then_cleared()
    token = routing._OP.set("01a17700-0000-7000-8000-000000000001")
    try:
        _short_write(monkeypatch)
        with pytest.raises(OSError):
            routing.restore_priority(stored, via="app")
        monkeypatch.undo()
        out = routing.restore_priority(stored, via="app")
        assert out.get("already_applied") is not True
        held = routing.read_view()["priority"]
        assert held["pending"] is True and [e["role"] for e in held["entries"]] == ["CONVERSATION WORDS"]
        lines = len(routing.read_events()[0])
        routing.restore_priority(stored, via="app")  # a complete attempt exists: nothing more is written
        assert len(routing.read_events()[0]) == lines
    finally:
        routing._OP.reset(token)
