"""Round 6 of the routing-exceptions review (jobs/2026-10-26/route-exceptions-astra): nine
findings, one test each (or more); each failed on 3031a74e. Hermetic: a temp data dir, fake
accounts and, for the gateway copy, a hub stand-in that holds one published row."""
from __future__ import annotations

import errno
from datetime import timedelta

import pytest

from khipu import owner_act, owner_direction, routing, routing_gateway_copy as gc

pytest_plugins = ("tests.test_routing_acceptance", "tests.test_owner_direction")

ME = {"kind": "conversation", "harness": "claude_code", "project": "project-a", "session": "s-one"}
OP = "01a17700-0000-7000-8000-0000000000bb"


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


def _exception(text="words", id_="x0000aaaa", hours=2):
    now = routing._now()
    return {"id": id_, "text": text, "until": routing._iso(now + timedelta(hours=hours)), "at": routing._iso(now),
            "by": "owner", "source": "app"}


# ---- 1 (P1): route.rule.restore never brings back a conversation's unconfirmed proposal ----------


def test_restore_refuses_a_conversations_proposed_rule_the_owner_never_confirmed(direction_world):
    routing.request_rule("A CONVERSATION'S NEW RULE", owner_request="asked", author=ME, via="mcp")
    n = next(r["n"] for r in routing.read_view()["rules"] if r["text"] == "A CONVERSATION'S NEW RULE")
    routing.dismiss_rule_request(n, via="app")
    with pytest.raises(routing.RouteError, match="proposed again, or add it as your own"):
        routing.restore_rule(n, via="app")
    direction_world.say(f"restore rule {n}")
    ok, out = owner_act.act("route.rule.restore", {"rule": str(n)})
    assert not ok
    assert not any(r["text"] == "A CONVERSATION'S NEW RULE" for r in routing.read_view()["rules"])


def test_restore_still_brings_back_the_owners_own_rule(direction_world):
    routing.add_rule("Owner's own rule", via="app")
    routing.retire_rule(1, via="app")
    routing.restore_rule(1, via="app")
    assert [r["text"] for r in routing.read_view()["rules"]] == ["Owner's own rule"]


# ---- 2 (P2): the gateway copy never serves an ended exception -------------------------------------


class _OneRowHub:
    def __init__(self, projection, published_at):
        self.row = {"projection": projection, "published_at": published_at, "stale_after": 0}

    def newest(self):
        return self.row


def test_the_gateway_drops_an_exception_once_it_ends_without_a_republish(direction_world, monkeypatch):
    real = routing.settings
    monkeypatch.setattr(routing, "settings",
                        lambda *a, **k: {**real(*a, **k), "routing_cloud_copy": True,
                                         "routing_cloud_copy_rules": True})
    routing.set_priority([{"account": "Codex", "role": "reviews",
                           "exceptions": [_exception("LOOSER PACE UNTIL TEN", hours=1)]}],
                         via="app", expect=routing.priority_version())
    now = routing._now()
    projection = routing.gateway_projection(now=now)
    gc.validate(projection)
    hub = _OneRowHub(projection, routing._iso(now))
    during = gc.answer(identity=None, now=now + timedelta(minutes=30), hub=hub)
    assert "LOOSER PACE UNTIL TEN" in str(during["priority"]["entries"])
    after = gc.answer(identity=None, now=now + timedelta(hours=2), hub=hub)
    assert "LOOSER PACE UNTIL TEN" not in str(after)


# ---- 3 (P2): a partial restore's retry never writes over a newer owner choice --------------------


def test_a_partial_restore_retry_is_refused_once_the_owner_changed_it(direction_world, monkeypatch):
    routing.set_priority([{"account": "Codex", "role": "reviews"}], via="app", expect=routing.priority_version())
    base = routing.conversation_view()["priority_owner_revision"]
    routing.request_priority([{"account": "Codex", "role": "OLD REQUEST"}], owner_request="asked", author=ME,
                             expect=routing.priority_version(), base=base)
    stored = routing.read_view()["priority_stored"]
    routing.clear_priority(via="app", expect=routing.priority_version())
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
            routing.restore_priority(stored, via="app")
        monkeypatch.setattr(routing, "_write_many", real)
    finally:
        routing._OP.reset(token)
    routing.set_priority([{"account": "Codex", "role": "NEWER OWNER"}], via="app", expect=routing.priority_version())
    token = routing._OP.set(OP)
    try:
        with pytest.raises(routing.RouteError, match="changed after the interrupted undo"):
            routing.restore_priority(stored, via="app")
    finally:
        routing._OP.reset(token)
    assert [e["role"] for e in routing.read_view()["priority"]["entries"]] == ["NEWER OWNER"]


# ---- 4 (P2): the undo snapshot is read under the same lock as the change -------------------------


def test_a_write_between_the_check_and_the_change_is_what_undo_restores(direction_world, monkeypatch):
    routing.add_rule("initial", via="app")
    real = owner_direction._before
    calls = []

    def racing(want):
        out = real(want)
        if want["action"] == "route.rule.edit" and not calls:
            calls.append(1)
            routing.edit_rule(1, "INTERVENING OWNER", via="app")  # lands after the check, before the change
        return out
    monkeypatch.setattr(owner_direction, "_before", racing)
    direction_world.say("edit rule 1")
    ok, applied = owner_act.act("route.rule.edit", {"rule": "1", "text": "final"})
    assert ok, applied
    direction_world.say("undo that")
    ok, undone = owner_act.act("direction.undo", {"id": applied["owner_action"]})
    assert ok, undone
    assert routing.read_view()["rules"][0]["text"] == "INTERVENING OWNER"


# ---- 5 (P2): cap snapshots are marked; unmarked cap undos are refused ------------------------------


def test_an_unmarked_cap_undo_is_refused(direction_world, monkeypatch):
    routing.set_cap("Codex", window="weekly", kind="share", pct=30, revision="r30", via="app")
    real = owner_direction._before
    monkeypatch.setattr(owner_direction, "_before", lambda want: (
        {k: v for k, v in real(want).items() if k != "provenance"} if isinstance(real(want), dict) else real(want)))
    direction_world.say("set the cap to 60")
    ok, applied = owner_act.act("route.cap.set", {"account": "Codex", "window": "weekly", "kind": "share", "pct": 60})
    assert ok, applied
    direction_world.say("undo that")
    ok, out = owner_act.act("direction.undo", {"id": applied["owner_action"]})
    assert not ok and owner_direction.LEGACY_PROVENANCE in str(out)


# ---- 6 (P2): amend takes the hold choice ------------------------------------------------------------


def test_ending_an_exception_on_a_held_account_takes_the_hold_choice(direction_world):
    routing.set_priority([{"account": "Codex", "role": "reviews", "exceptions": [_exception()]}],
                         via="app", expect=routing.priority_version())
    hold = routing.add_hold("Codex is busy", reason="keep", scope="account", account="Codex", owner=True, via="app")
    direction_world.say("end that Codex exception and keep the hold")
    ok, out = owner_act.act("route.priority.amend", {"account": "Codex", "end_exception": "x0000aaaa",
                                                     "holds": {hold["entity"]: "keep"},
                                                     "expect": routing.priority_version()})
    assert ok, out
    assert "exceptions" not in routing.read_view()["priority"]["entries"][0]


# ---- 7 (P2): the role budget compares stored roles -------------------------------------------------


def test_an_ended_entrys_long_legacy_role_does_not_block_amending_another(direction_world):
    routing.set_priority([{"account": "Codex", "role": "reviews"}, {"account": "Grok", "role": "audits", "until": "1h"}],
                         via="app", expect=routing.priority_version())
    rows = [dict(e, role=("legacy prose " * 175) if e["label"] == "Grok" else e["role"])
            for e in routing.read_view()["priority_stored"]["entries"]]
    routing._owner_write({"type": "priority_set", "entries": rows, "until": None}, "app")
    later = routing._now() + timedelta(hours=2)
    out = routing.amend_priority("Codex", append="and checks", via="app", expect=routing.priority_version(later),
                                 now=later)
    assert out["ok"] is True
    assert routing.read_view(later)["priority"]["entries"][0]["role"] == "reviews and checks"


# ---- 8 (P2): exception ids pass the typed-id check -------------------------------------------------


def test_an_exception_id_shaped_like_a_private_path_is_refused(direction_world):
    with pytest.raises(routing.RouteError):
        routing.set_priority([{"account": "Codex", "exceptions": [_exception(id_="x/Users/someone/private")]}],
                             via="app", expect=routing.priority_version())
    assert routing._checked_exceptions([_exception(id_="x/Users/someone/private")]) is None


# ---- 9 (P3): the 500-character limit holds on the text stored --------------------------------------


def test_an_exception_is_measured_as_stored(direction_world):
    with pytest.raises(routing.RouteError, match="at most 500"):
        routing.set_priority([{"account": "Codex", "exceptions": [_exception(text="x" + " " * 600)]}],
                             via="app", expect=routing.priority_version())
