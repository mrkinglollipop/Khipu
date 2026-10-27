"""Priority exceptions that expire on their own (docs/plans/2026-10-26-routing-exceptions-expire.md).
Hermetic: a temp data dir and fake accounts; the clock is injected, nothing is written to expire one."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from khipu import cli, mcp_server, owner_act, routing

pytest_plugins = ("tests.test_routing_acceptance", "tests.test_owner_direction")


@pytest.fixture
def direction_world(world):
    return world


ME = {"kind": "conversation", "harness": "claude_code", "project": "project-a", "session": "s-one"}
WORDS = "Codex checks may run 15 points over pace"


def _start(role="reviews and checks"):
    routing.set_priority([{"account": "Codex", "role": role}, {"account": "Grok", "role": "audits"}],
                         via="app", expect=routing.priority_version())


def _add(direction_world, text=WORDS, until="2h", **extra):
    direction_world.say("Codex checks may run 15 points over pace for a bit")
    ok, out = owner_act.act("route.priority.amend", {"account": "Codex", "exception": text, "until": until,
                                                     "expect": routing.priority_version(), **extra})
    assert ok, out
    return out


def _codex(now=None, view=None):
    view = view or routing.read_view(now)
    return next(e for e in view["priority"]["entries"] if e["label"] == "Codex")


def _live(now=None):
    return [x["text"] for x in _codex(now).get("exceptions", [])]


# ---- an exception shows before its end and is absent after it, in every reader ------------


def test_an_exception_is_in_every_reader_before_its_end_and_in_none_after(direction_world):
    _start()
    _add(direction_world)
    now = routing._now()
    before, after = now + timedelta(hours=1), now + timedelta(hours=3)
    lines_before = routing.priority_line(routing.read_view(before)["priority"], before)
    assert WORDS in lines_before and "exception until" in lines_before
    assert WORDS in routing.summary(now=before)["text"]
    assert WORDS in routing._priority_push(routing.read_view(before), before)[0][0]
    public = routing._public_priority(routing.read_view(before), before, {})
    assert [x["text"] for x in public["entries"][0]["exceptions"]] == [WORDS]
    assert WORDS in public["line"]
    assert _codex(view=routing.read_view(before))["exceptions"]  # advice reads this same entry list

    lines_after = routing.priority_line(routing.read_view(after)["priority"], after)
    assert WORDS not in lines_after and "exception" not in lines_after
    assert WORDS not in routing.summary(now=after)["text"]
    assert WORDS not in routing._priority_push(routing.read_view(after), after)[0][0]
    gone = routing._public_priority(routing.read_view(after), after, {})
    assert "exceptions" not in gone["entries"][0] and WORDS not in gone["line"]
    assert "exceptions" not in _codex(after)
    advice = routing.advise("Codex", now=after)
    assert advice and WORDS not in json.dumps(advice)


def test_the_json_route_command_shows_a_live_exception(direction_world, capsys):
    _start()
    _add(direction_world)
    assert cli.main(["route", "priority", "--json"]) == 0
    assert WORDS in capsys.readouterr().out


def test_the_view_refolds_at_an_exceptions_end_and_nothing_is_written(direction_world):
    _start()
    _add(direction_world)
    now = routing._now()
    stamp = routing.read_view(now)["valid_until"]
    assert stamp is not None and stamp <= (now + timedelta(hours=2, seconds=5)).isoformat().replace("+00:00", "Z")
    log = routing.read_view(now)["priority_version"]
    assert routing.read_view(now + timedelta(hours=5))["priority_version"].startswith(log.split(".")[0])


def test_an_expired_exception_stays_in_the_record(direction_world):
    _start()
    out = _add(direction_world)
    raw, _bad = routing._read_event_lines()
    held = [e for line in raw for e in line.get("entries") or [] if e.get("exceptions")]
    assert [x["id"] for e in held for x in e["exceptions"]] == [out["exception_id"]]
    assert _live(routing._now() + timedelta(hours=3)) == []  # gone from the live view, still in the record


# ---- the end it takes ----------------------------------------------------------------------


def test_with_no_end_the_exception_ends_at_the_next_weekly_reset_or_seven_days(direction_world):
    _start()
    direction_world.say("Codex may run over pace")
    now = routing._now()
    out = routing.amend_priority("Codex", exception=WORDS, via="app", expect=routing.priority_version(), now=now)
    end = routing._parse(out["exception_until"])
    assert abs((end - (now + timedelta(days=7))).total_seconds()) < 5
    assert "No end was given" in out["exception_until_default"] and "7 days" in out["exception_until_default"]
    assert _live(now + timedelta(days=6)) == [WORDS] and _live(now + timedelta(days=8)) == []


def test_a_known_weekly_reset_is_the_default_end(direction_world, monkeypatch):
    from khipu import usage

    _start()
    now = routing._now()
    reset = now + timedelta(days=2, hours=3)
    codex_key = next(a["key"] for a in routing.read_view(now)["accounts"] if a["label"] == "Codex")
    monkeypatch.setattr(usage, "_load_cache", lambda: {codex_key: {"windows": [
        {"name": "five_hour", "resets_at": (now + timedelta(hours=2)).isoformat()},
        {"name": "weekly", "resets_at": reset.isoformat()}]}})
    out = routing.amend_priority("Codex", exception=WORDS, via="app", expect=routing.priority_version(), now=now)
    assert abs((routing._parse(out["exception_until"]) - reset).total_seconds()) < 2
    assert "weekly reset" in out["exception_until_default"]


def test_an_end_in_the_past_is_refused(direction_world):
    _start()
    direction_world.say("x")
    with pytest.raises(routing.RouteError):
        routing.amend_priority("Codex", exception=WORDS, until="2000-01-01T00:00:00Z", via="app",
                               expect=routing.priority_version())
    assert _live() == []


# ---- --end-exception ------------------------------------------------------------------------


def test_end_exception_ends_one_early_and_leaves_the_others(direction_world):
    _start()
    first = _add(direction_world, "first words")
    _add(direction_world, "second words")
    direction_world.say("end the first one")
    ok, out = owner_act.act("route.priority.amend", {"account": "Codex", "end_exception": first["exception_id"],
                                                     "expect": routing.priority_version()})
    assert ok, out
    assert _live() == ["second words"]
    ok, out = owner_act.act("route.priority.amend", {"account": "Codex", "end_exception": first["exception_id"],
                                                     "expect": routing.priority_version()})
    assert not ok  # already gone


def test_amend_takes_exactly_one_kind_of_change(direction_world):
    _start()
    direction_world.say("change Codex")
    for args in ({"exception": "a", "append": "b"}, {"end_exception": "x1", "role": "b"}, {}):
        ok, out = owner_act.act("route.priority.amend", {"account": "Codex", "expect": routing.priority_version(), **args})
        assert not ok and out["code"] == "bad_args"


# ---- the role budget ------------------------------------------------------------------------


def test_set_and_amend_over_the_budget_are_refused_with_the_pointer(direction_world):
    _start()
    long = "x " * 1100
    with pytest.raises(routing.RouteError) as set_error:
        routing.set_priority([{"account": "Codex", "role": long}], via="app", expect=routing.priority_version())
    assert "2,000" in str(set_error.value) and "--exception" in str(set_error.value)
    direction_world.say("append a lot")
    ok, out = owner_act.act("route.priority.amend", {"account": "Codex", "append": long,
                                                     "expect": routing.priority_version()})
    assert not ok and "--exception" in json.dumps(out)
    assert _codex()["role"] == "reviews and checks"


def test_a_conversation_set_and_amend_over_the_budget_are_refused_too(direction_world):
    _start()
    base = routing.conversation_view()["priority_owner_revision"]
    long = "word " * 500
    with pytest.raises(routing.RouteError, match="2,000"):
        routing.request_priority_amend("Codex", append=long, owner_request="Matt said so", author=ME,
                                       expect=routing.priority_version(), base=base)
    with pytest.raises(routing.RouteError, match="2,000"):
        routing.request_priority([{"account": "Codex", "role": long}], owner_request="Matt said so", author=ME,
                                 expect=routing.priority_version(), base=base)


def _legacy_over_budget():
    _start()
    entries = routing.read_view()["priority"]["entries"]
    rows = [{"account": e["account"], "label": e["label"], "role": ("old prose. " * 250) if e["label"] == "Codex"
             else e["role"], "until": None} for e in entries]
    routing._owner_write({"type": "priority_set", "entries": rows, "until": None}, "app")
    return len(_codex()["role"])


def test_an_over_budget_legacy_role_still_reads_warns_and_takes_exceptions(direction_world):
    size = _legacy_over_budget()
    assert size > routing.ROLE_BUDGET
    view = routing.read_view()
    now = routing._now()
    assert any("over the 2,000 budget" in w for w in routing._priority_warnings(view, now, routing.settings()))
    assert "over the 2,000 budget" in routing.priority_line(view["priority"], now)
    assert "over the 2,000 budget" in routing.summary(now=now)["text"]
    assert any("over the 2,000 budget" in w for w in routing.doctor_block(now)["warnings"])
    _add(direction_world)  # an exception does not grow the role, so it is allowed
    assert len(_codex()["role"]) == size
    direction_world.say("grow it")
    ok, out = owner_act.act("route.priority.amend", {"account": "Codex", "append": "more",
                                                     "expect": routing.priority_version()})
    assert not ok and "2,000" in json.dumps(out)
    ok, out = owner_act.act("route.priority.amend", {"account": "Codex", "role": "short again",
                                                     "expect": routing.priority_version()})
    assert ok, out  # shrinking is always allowed


# ---- old data, undo, paths ------------------------------------------------------------------


def test_priorities_stored_before_this_change_read_unchanged(direction_world):
    _start()
    entries = routing.read_view()["priority"]["entries"]
    assert all("exceptions" not in e for e in entries)
    now = routing._now() + timedelta(days=400)
    assert [(e["label"], e["role"]) for e in routing.read_view(now)["priority"]["entries"]] == [
        ("Codex", "reviews and checks"), ("Grok", "audits")]


def test_an_exception_is_one_conversation_request_away_and_stays_unconfirmed(direction_world):
    _start()
    base = routing.conversation_view()["priority_owner_revision"]
    out = routing.request_priority_amend("Codex", exception=WORDS, until="2h", owner_request="Matt said loosen Codex",
                                         author=ME, expect=routing.priority_version(), base=base)
    assert out["ok"] is True and out["exception_id"], out
    held = routing.read_view()["priority"]
    assert held["pending"] is True
    assert _live() == [WORDS]
    assert held["replaced"]["entries"][0]["role"] == "reviews and checks"
    assert "exceptions" not in held["replaced"]["entries"][0]
    exception = _codex()["exceptions"][0]
    assert exception["by"] and exception["source"] == "Matt said loosen Codex"


def test_the_owner_direction_applies_at_once_and_records_who_and_when(direction_world):
    _start()
    _add(direction_world)
    held = routing.read_view()["priority"]
    assert held["pending"] is False
    exception = _codex()["exceptions"][0]
    assert exception["by"] == "owner" and exception["at"] and exception["until"] and exception["id"]


def test_undo_restores_the_priority_with_its_exceptions(direction_world):
    _start()
    first = _add(direction_world, "first words")
    direction_world.say("add another")
    ok, applied = owner_act.act("route.priority.amend", {"account": "Codex", "exception": "second words", "until": "3h",
                                                         "expect": routing.priority_version()})
    assert ok, applied
    assert _live() == ["first words", "second words"]
    direction_world.say("undo that")
    ok, undone = owner_act.act("direction.undo", {"id": applied["owner_action"]})
    assert ok, undone
    assert _live() == ["first words"]
    assert _codex()["exceptions"][0]["id"] == first["exception_id"]


def test_an_expired_exception_is_dropped_by_a_later_rewrite_not_before(direction_world):
    _start()
    _add(direction_world)
    later = routing._now() + timedelta(hours=3)
    direction_world.say("tweak Codex")
    routing.amend_priority("Codex", append="and more", via="app", expect=routing.priority_version(), now=later)
    assert _live(later) == []
    assert _codex(later)["role"] == "reviews and checks and more"


def test_the_cli_owner_act_and_mcp_reach_the_same_code(direction_world):
    _start()
    direction_world.say("loosen Codex for a bit")
    code, out = direction_world.run(["route", "priority", "amend", "Codex", "--exception", "via cli", "--until", "2h",
                                     "--expect", routing.priority_version()], True)
    assert code == 0, out
    assert _live() == ["via cli"]
    ok, out = owner_act.act("route.priority.amend", {"account": "Codex", "exception": "via owner_act", "until": "2h",
                                                     "expect": routing.priority_version()})
    assert ok, out
    direction_world.be("s-owner")
    reply = mcp_server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "khipu_owner_act", "arguments": {"action": "route.priority.amend", "args": {
            "account": "Codex", "exception": "via mcp", "until": "2h", "expect": routing.priority_version()}}}})["result"]
    assert not reply["isError"], reply
    assert _live() == ["via cli", "via owner_act", "via mcp"]
    ids = [x["id"] for x in _codex()["exceptions"]]
    code, out = direction_world.run(["route", "priority", "amend", "Codex", "--end-exception", ids[0],
                                     "--expect", routing.priority_version()], True)
    assert code == 0, out
    assert _live() == ["via owner_act", "via mcp"]


def test_khipu_route_priority_records_a_conversations_exception(direction_world):
    _start()
    base = routing.conversation_view()["priority_owner_revision"]
    out = mcp_server._tool_route_priority({"account": "Codex", "exception": "via the request tool", "until": "2h",
                                           "owner_request": "Matt said", "expect": routing.priority_version(),
                                           "base": base})
    assert out["ok"] is True and out["exception_id"], out
    assert routing.read_view()["priority"]["pending"] is True
    assert _live() == ["via the request tool"]


def _stored_entries():
    events, _bad = routing._read_event_lines()
    return [e for e in events if e.get("type") == "priority_set"][-1]["entries"]


def test_a_rewrite_prunes_the_changed_entrys_ended_exceptions_and_keeps_the_others_as_stored(direction_world):
    _start()
    _add(direction_world)
    direction_world.say("Grok may audit extra")
    ok, out = owner_act.act("route.priority.amend", {"account": "Grok", "exception": "grok words", "until": "1h",
                                                     "expect": routing.priority_version()})
    assert ok, out
    later = routing._now() + timedelta(hours=3)
    grok_before = _stored_entries()[1]
    routing.amend_priority("Codex", exception="new words", until="1d", via="app",
                           expect=routing.priority_version(), now=later)
    stored = _stored_entries()
    assert [x["text"] for x in stored[0]["exceptions"]] == ["new words"]
    assert stored[1] == grok_before  # another entry's ended exception stays byte for byte until it is amended
    assert _live(later) == ["new words"]


def test_the_cli_request_path_takes_an_exception(direction_world, capsys):
    _start()
    direction_world.be("s-conv")
    version = routing.priority_version()
    base = routing.conversation_view()["priority_owner_revision"]
    assert cli.main(["route", "priority", "amend", "Codex", "--exception", "asked for", "--until", "2h",
                     "--at-owner-request", "Matt asked", "--expect", version, "--base", base]) == 0
    capsys.readouterr()
    assert routing.read_view()["priority"]["pending"] is True
    assert _live() == ["asked for"]


# ---- the gateway copy never loses the priority to an ended exception or a near valid_until ----


class _OneRowHub:
    def __init__(self, projection, published_at):
        self.row = {"projection": projection, "published_at": published_at, "stale_after": 0}

    def newest(self):
        return self.row


def _published(direction_world, monkeypatch):
    from khipu import routing_gateway_copy as gc
    real = routing.settings
    monkeypatch.setattr(routing, "settings", lambda *a, **k: {**real(*a, **k), "routing_cloud_copy": True,
                                                              "routing_cloud_copy_rules": True})
    _start()
    _add(direction_world)
    now = routing._now()
    projection = routing.gateway_projection(now=now)
    gc.validate(projection)
    return gc, now, projection


def test_the_gateway_keeps_the_priority_after_an_exception_ends(direction_world, monkeypatch):
    gc, now, projection = _published(direction_world, monkeypatch)
    hub = _OneRowHub(projection, routing._iso(now))
    assert WORDS in str(gc.answer(identity=None, now=now + timedelta(hours=1), hub=hub))
    later = now + timedelta(hours=2, minutes=1)
    body = gc.answer(identity=None, now=later, hub=hub)
    assert WORDS not in str(body)
    assert [e["label"] for e in body["priority"]["entries"]] == [e["label"] for e in routing.read_view(later)["priority"]["entries"]]


def test_the_gateway_keeps_the_priority_when_valid_until_is_a_second_after_publish(direction_world, monkeypatch):
    gc, now, projection = _published(direction_world, monkeypatch)
    projection["valid_until"] = routing._iso(now + timedelta(seconds=1))
    hub = _OneRowHub(projection, routing._iso(now))
    body = gc.answer(identity=None, now=now + timedelta(seconds=5), hub=hub)
    assert {e["label"] for e in body["priority"]["entries"]} == {"Codex", "Grok"}
    assert WORDS in str(body) and not body["stale"]


def test_a_role_with_exception_words_survives_intact_past_valid_until(direction_world, monkeypatch):
    gc, now, projection = _published(direction_world, monkeypatch)
    role = "reviews (exception: weekends only)"
    projection["valid_until"] = routing._iso(now + timedelta(seconds=1))
    projection["priority"]["line"] += " Also " + role + "."
    hub = _OneRowHub(projection, routing._iso(now))
    body = gc.answer(identity=None, now=now + timedelta(seconds=5), hub=hub)
    assert role in body["priority"]["line"]
