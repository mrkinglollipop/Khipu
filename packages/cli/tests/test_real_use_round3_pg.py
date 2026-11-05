"""What real use on the second Mac still showed (round 3), on two simulated Macs
and a real hermetic hub."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from khipu import owner_direction, routing
from khipu import routing_replication as rr
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.test_settings_sync_pg import (  # noqa: F401
    Mac, _hold_priority_amend, _item_on, both_on, macs, pg, sync_all)


def _lines(mac, request):
    mac.activate()
    events, _bad = routing._read_event_lines()
    return [e for e in events if e.get("type") == "request_state" and e.get("request") == request]


def _age_the_mirror(b):
    """Put the mirrored row back in the shape the code before #490 wrote."""
    b.activate()
    path = owner_direction.requests_path()
    state = owner_direction._load(path)
    for row in state["requests"]:
        if row.get("remote"):
            row["by"] = {"kind": "other_mac"}
            row["remote"].pop("blocked", None)
            row["created_at"] = owner_direction._iso((b.now - timedelta(seconds=10)).timestamp())
    owner_direction._save(path, state, b.now.timestamp())


def _forget_what_was_published(a):
    """The holder's saved publishing state as the code before the format stamp left it."""
    a.activate()
    state = rr._state()
    state.pop("requests_format", None)
    rr._save(state)


def test_an_upgraded_holder_sends_what_it_holds_once_more_and_the_old_mirror_is_corrected(macs):
    a, b = macs
    both_on(a, b)
    request = _hold_priority_amend(a)
    a.activate()
    routing.set_priority([{"account": "Grok"}], via="app", expect=routing.priority_version())  # now out of date
    sync_all(a, b)
    _age_the_mirror(b)
    _forget_what_was_published(a)
    before = len(_lines(a, request))
    sync_all(a, b)
    assert len(_lines(a, request)) == before + 1
    item = _item_on(b, request)
    assert item["decline_only"] is True and item["asked_by"]["who"] == "a Claude Code conversation on Studio"
    sync_all(a, b)  # once: nothing changed, so nothing more is sent
    assert len(_lines(a, request)) == before + 1


def test_a_request_that_expires_on_the_holder_is_sent_when_it_expires_without_an_edit(macs, monkeypatch):
    a, b = macs
    both_on(a, b)
    a.activate()
    routing.set_priority([{"account": "Codex"}, {"account": "Grok"}], via="app", expect=routing.priority_version())
    a.say("let Grok burn for an hour", "sP")
    ends = datetime.now(timezone.utc) + timedelta(hours=1)
    bad, out = a.act("direction.request", {"action": "route.priority.amend", "args": {
        "account": "Grok", "exception": "burn it", "until": ends.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expect": routing.priority_version()}}, "sP")
    assert not bad and out["pending"] is True, out
    request = out["request"]
    sync_all(a, b)
    assert not _item_on(b, request).get("decline_only")
    monkeypatch.setattr(owner_direction, "_clock", lambda: time.time() + 2 * 3600)  # time passes; nobody edits
    sync_all(a, b)
    assert _item_on(b, request)["decline_only"] is True


def test_a_request_withdrawn_or_dropped_on_the_holder_does_not_sit_on_the_other_mac(macs):
    a, b = macs
    both_on(a, b)
    withdrawn, dropped = _hold_priority_amend(a, "sW"), _hold_priority_amend(a, "sD")
    sync_all(a, b)
    a.activate()
    path = owner_direction.requests_path()
    state = owner_direction._load(path)
    for row in list(state["requests"]):
        if row["id"] == withdrawn:
            row.update(state="withdrawn", reason="forgotten", settled_ts=time.time(), settled_at="2026-11-05T00:00:00Z")
        if row["id"] == dropped:
            state["requests"].remove(row)
    owner_direction._save(path, state, time.time())
    sync_all(a, b)
    assert not [i for i in b.waiting("sB")["items"] if i["key"] in (f"held:rm-{withdrawn}", f"held:rm-{dropped}")]
    states = {r["request"]: r["state"] for r in b.mirrors()}
    assert states[withdrawn] == states[dropped] == "stale"


def test_an_unreadable_request_file_does_not_call_every_request_gone(macs):
    a, b = macs
    both_on(a, b)
    request = _hold_priority_amend(a)
    sync_all(a, b)
    a.activate()
    owner_direction.requests_path().write_text("{not json")
    before = len(_lines(a, request))
    a.sync()
    assert len(_lines(a, request)) == before


# ---- routing_push, the feature switch, copies between Macs like a setting -------------------------


def _feature_on(mac):
    from khipu.config import load_config
    mac.activate()
    return (load_config().get("features") or {}).get("routing_push")


def test_the_routing_push_switch_set_on_one_mac_reaches_the_other_and_can_be_undone(macs):
    from khipu import features
    a, b = macs
    both_on(a, b)
    a.activate()
    features.set_enabled("routing_push", True)
    sync_all(a, b)
    assert _feature_on(b) is True
    (line,) = [x for x in b.log() if x.get("kind") == "action" and x.get("target") == "routing_push"]
    assert line["action"] == "config.routing_setting" and line["auth"] == "other_mac" and "Studio" in line["words"]
    assert line.get("before") == {"value": None, "set": False}
    assert b.undo_in_app(line["id"]) == 0
    assert _feature_on(b) is None
    sync_all(b)  # a pull never re-applies what the owner took back
    assert _feature_on(b) is None


def test_the_first_switch_on_lists_routing_push_as_a_difference_and_overwrites_nothing(macs):
    from khipu import features
    a, b = macs
    a.activate()
    features.set_enabled("routing_push", True)
    b.activate()
    features.set_enabled("routing_push", False)
    a.activate()
    routing.set_setting("routing_replication_live", "on")
    sync_all(a)
    assert b.sync()["disabled"] is True
    sync_all(b, a)
    b.activate()
    differences = rr.settings_status()["differences"]
    (diff,) = [d for d in differences if d["key"] == "routing_push"]
    assert (diff["local"], diff["remote"]) == (False, True) and diff["remote_snapshot"] is True
    assert _feature_on(b) is False


def test_a_switch_that_was_never_copied_off_stays_a_machine_choice_when_the_copy_is_off(macs):
    from khipu import features
    a, b = macs
    a.activate()
    features.set_enabled("routing_push", True)
    sync_all(a)
    assert _feature_on(b) is None
