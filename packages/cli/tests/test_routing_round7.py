"""Round 7 of the routing-exceptions review (jobs/2026-10-26/route-exceptions-sol-r6): each test
failed on 0117bb81. Hermetic: a temp data dir, fake accounts and a one-row hub stand-in."""
from __future__ import annotations

import errno
from datetime import timedelta

import pytest

from khipu import routing, routing_gateway_copy as gc

pytest_plugins = ("tests.test_routing_acceptance", "tests.test_owner_direction")

ME = {"kind": "conversation", "harness": "claude_code", "project": "project-a", "session": "s-one"}
OP = "01a17700-0000-7000-8000-0000000000cc"


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


def _interrupted(monkeypatch, call):
    """Run ``call`` under OP with its batch cut after the first line."""
    real = routing._write_many

    def first_line_then_disk_full(events):
        if len(events) > 1:
            real(events[:1])
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(events)
    token = routing._OP.set(OP)
    try:
        monkeypatch.setattr(routing, "_write_many", first_line_then_disk_full)
        with pytest.raises(OSError):
            call()
    finally:
        monkeypatch.setattr(routing, "_write_many", real)
        routing._OP.reset(token)


def _retry(call):
    token = routing._OP.set(OP)
    try:
        return call()
    finally:
        routing._OP.reset(token)


# ---- (1) the gateway withholds an older copy's baked-in allowance past valid_until ---------------


class _OneRowHub:
    def __init__(self, projection, published_at):
        self.row = {"projection": projection, "published_at": published_at, "stale_after": 0}

    def newest(self):
        return self.row


def test_an_older_copys_baked_in_allowance_is_withheld_past_valid_until(direction_world, monkeypatch):
    real = routing.settings
    monkeypatch.setattr(routing, "settings", lambda *a, **k: {**real(*a, **k), "routing_cloud_copy": True,
                                                              "routing_cloud_copy_rules": True})
    routing.set_priority([{"account": "Codex", "role": "reviews"}], via="app", expect=routing.priority_version())
    now = routing._now()
    projection = routing.gateway_projection(now=now)
    # As code before round 6 published it: the exception only in the line.
    projection["priority"]["line"] += " (exception until 10:01Z: OLD ALLOWANCE)"
    projection["valid_until"] = routing._iso(now + timedelta(hours=1))
    gc.validate(projection)
    hub = _OneRowHub(projection, routing._iso(now))
    assert "OLD ALLOWANCE" in str(gc.answer(identity=None, now=now + timedelta(minutes=30), hub=hub))
    after = gc.answer(identity=None, now=now + timedelta(hours=2), hub=hub)
    assert "OLD ALLOWANCE" not in str(after) and after["stale"] is True


# ---- (2) a cap layer's natural expiry never blocks a first retry ----------------------------------


def test_a_cap_restore_retry_survives_a_layer_expiring_on_its_own(direction_world, monkeypatch):
    now = routing._now()
    routing.set_cap("Codex", window="weekly", kind="share", pct=40, revision="base", via="app", now=now)
    layers = [dict(c) for c in next(a for a in routing.read_view(now)["accounts"] if a["label"] == "Codex")
              ["cap_layers"]]
    # The change being undone is itself timed: the slot's layers change with
    # the clock alone once it ends.
    routing.set_cap("Codex", window="weekly", kind="share", pct=70, revision="undone",
                    until=routing._iso(now + timedelta(hours=1)), via="app", now=now)
    _interrupted(monkeypatch, lambda: routing.restore_cap_layers("Codex", window="weekly", kind="share",
                                                                  layers=layers, via="app", now=now))
    later = now + timedelta(hours=2)  # the timed layer ended on its own; nobody wrote
    _retry(lambda: routing.restore_cap_layers("Codex", window="weekly", kind="share", layers=layers, via="app",
                                              now=later))
    caps = next(a for a in routing.read_view(later)["accounts"] if a["label"] == "Codex").get("caps") or []
    assert [c["pct"] for c in caps] == [40.0]


# ---- (3) a retired rule's retry target is its latest owner revision -------------------------------


def test_a_retired_rule_restore_retry_is_refused_after_an_intervening_owner_change(direction_world, monkeypatch):
    routing.add_rule("Owner's rule words", via="app")
    shown = routing.conversation_view()
    routing.request_rule("CONVERSATION RULE WORDS", n=1, owner_request="asked", author=ME, via="mcp",
                         base=shown["rules"][0]["owner_revision"])
    request = routing.pending_rule_line(routing.read_view()["rules"][0]["entity"])
    routing.retire_rule(1, via="app")
    _interrupted(monkeypatch, lambda: routing.restore_rule_request(1, request, via="app", retired=True))
    routing.restore_rule(1, via="app")  # the owner's own changes in between
    routing.retire_rule(1, via="app")
    with pytest.raises(routing.RouteError, match="changed after the interrupted undo"):
        _retry(lambda: routing.restore_rule_request(1, request, via="app", retired=True))
    assert not routing.read_view()["rules"]
