"""Slice B6: ``khipu route say --from-app --preview WORDS | --apply TOKEN``.

Words become one previewed change with plain code; apply runs exactly that
change through the existing owner command (so its owner-actions line, its
Undo and its ``expect`` pin are the existing ones). Hermetic: a temp data
dir, a fixed clock (Tue 10 Nov 2026, noon in New York) and the faked process
tree the routing owner check walks. Nothing here reaches the network.
"""
from __future__ import annotations

import json
import os
import socket
import stat
import time
from datetime import datetime, timezone

import pytest

from khipu import owner_direction, routing, routing_say
from tests import test_owner_direction as base

world = base.world  # the fixture, shared

NOW = datetime(2026, 11, 10, 17, 0, tzinfo=timezone.utc)  # Tuesday 12:00 in New York


@pytest.fixture(autouse=True)
def _clock_and_zone(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    monkeypatch.setattr(routing, "_now", lambda: NOW)
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def seeded(world, khipu_app):
    routing.set_priority(["Claude", "Claude second", "Codex", "Grok"], via="app", expect=routing.priority_version())
    routing.add_rule("Prefer Codex for cross-family reviews", via="app")
    routing.add_rule("Keep Grok to one job at a time", via="app")
    khipu_app.as_app()
    return world


def preview(world, words: str) -> tuple[int, dict]:
    return world.run(["route", "say", "--from-app", "--preview", words])


def save(world, token: str) -> tuple[int, dict]:
    return world.run(["route", "say", "--from-app", "--apply", token])


def undo(world, action_id: str) -> tuple[int, dict]:
    return world.run(["owner-direction", "undo", action_id, "--from-app"])


def labels() -> list[str]:
    return [e["label"] for e in routing.read_view(NOW)["priority"]["entries"]]


def state(key: str) -> dict:
    return next(a for a in routing.read_view(NOW)["accounts"] if a["key"] == key)


def snapshot() -> tuple:
    files = tuple(p.read_bytes() if p.exists() else None for p in (routing.events_file(), owner_direction.log_path()))
    return files, json.dumps(routing.read_view(NOW).get("proposals"), sort_keys=True, default=str)


# ---- the phrase table: every shape, the fallback and the awkward ones ---------------------------

FRIDAY = "2026-11-13T05:00:00Z"  # Fri 13 Nov 00:00 in New York
MONDAY = "2026-11-16T05:00:00Z"

PHRASES = [
    # (words, kind, account, ends)
    ("Put Codex first", "priority_first", "Codex", None),
    ("put codex first until Friday", "priority_first_until", "Codex", FRIDAY),
    ("PUT CODEX FIRST UNTIL FRIDAY", "priority_first_until", "Codex", FRIDAY),
    ("Could you please put Codex at the top until Friday, thanks!", "priority_first_until", "Codex", FRIDAY),
    ("make Codex the first choice for 3 days", "priority_first_until", "Codex", "2026-11-13T17:00:00Z"),
    ("Codex first until tomorrow at 6 pm", "priority_first_until", "Codex", "2026-11-11T23:00:00Z"),
    ("move Grok to the top until Nov 14", "priority_first_until", "Grok", "2026-11-14T05:00:00Z"),
    ("put claude second first", "priority_first", "Claude second", None),
    ("Pause Grok until Monday", "pause", "Grok", MONDAY),
    ("pause grok for 2 hours", "pause", "Grok", "2026-11-10T19:00:00Z"),
    ("stop using Grok until next Tuesday", "pause", "Grok", "2026-11-17T05:00:00Z"),
    ("don't use Codex until 2026-11-12 09:30", "pause", "Codex", "2026-11-12T14:30:00Z"),
    ("pause Codex till tomorrow", "pause", "Codex", "2026-11-11T05:00:00Z"),
    ("pause Claude second until Monday at 9am", "pause", "Claude second", "2026-11-16T14:00:00Z"),
    ("make agents stop at 90% on Grok", "stop_at", "Grok", None),
    ("grok: stop at 50 percent", "stop_at", "Grok", None),
    ("Stop Grok at 50%", "stop_at", "Grok", None),
    ("set Codex's limit to 85 percent of the week", "stop_at", "Codex", None),
    ("cap Codex at 85 percent until Friday", "stop_at", "Codex", FRIDAY),
    ("end rule 1", "end_rule", None, None),
    ("Remove the rule about Grok", "end_rule", None, None),
    ("retire the rule that says reviews", "end_rule", None, None),
    # the fallback: anything else is offered as a rule in his words
    ("Use Codex for code review and Claude for planning", "add_rule", None, None),
    ("always prefer the cheaper accounts for docs", "add_rule", None, None),
    ("put Codex first for reviews", "add_rule", None, None),
    ("pause the nightly build whenever it hangs", "add_rule", None, None),
    ("pause Grok when it is slow", "add_rule", None, None),
]


@pytest.mark.parametrize("words,kind,account,ends", PHRASES)
def test_the_phrase_table(seeded, words, kind, account, ends):
    plan = routing_say.interpret(words, NOW)
    assert plan["kind"] == kind, plan
    assert plan["account"] == account
    assert plan["ends"] == ends
    assert plan["fallback"] is (kind == "add_rule")
    if kind == "add_rule":
        assert plan["argv"][:5] == ["route", "rule", "add", " ".join(words.split()), "--from-app"]  # his words, as typed
        assert plan["argv"][5:] == ["--expect=" + routing.pin_text(plan["pin"]["value"])]


def test_a_percent_is_read_both_ways_and_the_window_follows_the_words(seeded):
    one = routing_say.interpret("stop Grok at 50%", NOW)
    two = routing_say.interpret("stop Grok at 50 percent", NOW)
    assert one["argv"] == two["argv"] and "--pct=50" in one["argv"] and "--window=weekly" in one["argv"]
    five = routing_say.interpret("make agents stop at 70% of the five hour window on Codex", NOW)
    assert "--window=five_hour" in five["argv"] and "--pct=70" in five["argv"]
    half = routing_say.interpret("stop Grok at 87.5 percent", NOW)
    assert "--pct=87.5" in half["argv"]


def test_a_time_before_now_is_not_guessed(seeded):
    for words in ("pause Grok until 2020-01-01", "pause Grok until Nov 3", "pause Grok until 8 am",
                  "pause Grok until Tuesday", "put Codex first until 2026-11-10 11:00"):
        with pytest.raises(routing_say.NotUnderstood) as raised:
            routing_say.interpret(words, NOW)
        assert raised.value.reason, words
    assert _reason("pause Grok until 2020-01-01") == "Wed 1 Jan 2020, 12:00 am has already passed. Say a time that is still ahead."
    assert "passed" in _reason("pause Grok until 8 am")
    assert "Today is already" in _reason("pause Grok until Tuesday")
    # a weekday said on that weekday still works with "next", or with a time later today
    assert routing_say.interpret("pause Grok until next Tuesday", NOW)["ends"] == "2026-11-17T05:00:00Z"
    assert routing_say.interpret("pause Grok until Tuesday at 6 pm", NOW)["ends"] == "2026-11-10T23:00:00Z"


def _reason(words: str) -> str:
    with pytest.raises(routing_say.NotUnderstood) as raised:
        routing_say.interpret(words, NOW)
    return raised.value.reason


@pytest.mark.parametrize("words,why", [
    ("pause Grok until 6", "morning or evening"),
    ("pause Grok until whenever", "couldn't read"),
    ("pause Grok until I say so", "ends by itself"),
    ("pause Grok", "ends by itself"),
    ("pause Grok for now", "ends by itself"),
    ("pause Grok until Nov 31", "not a real date"),
    ("pause Grok until 25:00", "not a time of day"),
    ("resume Grok until Friday", "Resuming happens now"),
    ("resume Grok", "isn't paused"),
    ("stop Grok at 0%", "1 to 100"),
    ("stop Grok at 250 percent", "1 to 100"),
    ("end rule 99", "no rule 99"),
    ("end the rule about zebras", "No rule mentions"),
    ("end the rule", "Which rule"),
    ("put Grok first until 2020-01-01", "passed"),
])
def test_an_unreadable_or_unsafe_ask_is_not_understood_with_the_reason(seeded, words, why):
    assert why.lower() in _reason(words).lower()


@pytest.mark.parametrize("words", [
    "put Codex and Grok first",
    "pause Codex and Claude second until Monday",
    "stop Codex and Grok at 90%",
])
def test_two_accounts_are_considered_and_nothing_is_applied(seeded, words):
    before = snapshot()
    rc, out = preview(seeded, words)
    assert rc == 2 and out["code"] == "not_understood" and "token" not in out
    assert len(out["considered"]) == 2 and set(out["considered"]) <= {"Claude", "Claude second", "Codex", "Grok"}
    assert snapshot() == before


@pytest.mark.parametrize("words", ["pause Gemini until Monday", "stop Gemini at 90%", "resume Gemini"])
def test_no_account_is_considered_and_nothing_is_applied(seeded, words):
    before = snapshot()
    rc, out = preview(seeded, words)
    assert rc == 2 and out["code"] == "not_understood" and out["considered"] == []
    assert "which account" in out["error"] and "token" not in out
    assert snapshot() == before
    assert not routing_say._token_path().exists()


def test_an_account_missing_from_the_order_and_a_first_account_are_refused(seeded):
    assert "already first" in _reason("put Claude first")
    routing.set_priority(["Claude", "Codex"], via="app", expect=routing.priority_version())
    assert "isn't in your order" in _reason("put Grok first")


def test_a_conversations_pending_order_must_be_settled_first(seeded):
    shown = routing.conversation_view()
    held = routing.request_priority(
        [{"account": "Codex"}, {"account": "Claude"}], owner_request="codex first",
        author={"kind": "conversation", "harness": "claude_code", "project": "p", "session": "s-x"},
        expect=routing.priority_version(), base=shown["priority_owner_revision"])
    assert held["ok"] is not False, held
    assert "waiting for you" in _reason("put Grok first")


def test_empty_and_long_words_are_refused(seeded):
    assert "Say what to change" in _reason("   ")
    assert "at most" in _reason("x" * 600)


# ---- a preview never writes -------------------------------------------------------------------


@pytest.mark.parametrize("words", [
    "put Codex first", "put Codex first until Friday", "pause Grok until Monday", "stop Grok at 90%",
    "end rule 1", "Use Codex for reviews and Claude for planning",
])
def test_a_preview_changes_nothing_but_its_token(seeded, words, monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("a preview must not reach the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    before = snapshot()
    rc, out = preview(seeded, words)
    assert rc == 0 and out["ok"] is True, out
    assert snapshot() == before  # the record, the owner log and the proposals: byte for byte
    assert owner_direction.actions() == []


def test_a_preview_names_what_was_understood_how_it_is_now_and_how_it_would_be(seeded):
    rc, out = preview(seeded, "put codex first until Friday")
    assert rc == 0
    assert out["understood"] == "Put Codex first until Fri 13 Nov, 12:00 am"
    assert (out["kind"], out["account"], out["fallback"]) == ("priority_first_until", "Codex", False)
    assert out["now"] == "1 Claude, 2 Claude second, 3 Codex, 4 Grok"
    assert out["after"].startswith("Codex goes first until Fri 13 Nov, 12:00 am, then the order goes back to: ")
    assert out["ends"] == FRIDAY and out["ends_words"] == "Fri 13 Nov, 12:00 am"
    assert out["apply"] == {"command": "route say", "args": ["--from-app", "--apply", out["token"]]}
    assert len(out["token"]) == 32 and out["token"].isalnum() and out["expires_at"].endswith("Z")


def test_the_token_file_is_private_like_the_other_owner_state(seeded):
    preview(seeded, "put Codex first")
    path = routing_say._token_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    row = next(iter(json.loads(path.read_text())["tokens"].values()))
    assert set(row) == {"expires", "used", "plan"} and row["used"] is False


# ---- apply does exactly what was shown, through the existing owner action, with Undo ------------


def test_apply_puts_an_account_first_and_undo_puts_the_order_back(seeded):
    _rc, shown = preview(seeded, "put Codex first")
    assert shown["after"] == "1 Codex, 2 Claude, 3 Claude second, 4 Grok"
    rc, done = save(seeded, shown["token"])
    assert rc == 0 and done["applied"] is True and done["kind"] == "priority_first", done
    assert labels() == ["Codex", "Claude", "Claude second", "Grok"]
    receipt = done["receipt"]
    assert receipt["text"] == "Codex is first in your order." and receipt["undoable"] is True
    assert receipt["action_id"] == done["action_id"] and receipt["saved_on"] and receipt["not_reached"] == []
    (line,) = owner_direction.actions()
    assert line["id"] == done["action_id"] and line["auth"] == "app" and line["state"] == "applied"
    rc, undone = undo(seeded, done["action_id"])
    assert rc == 0, undone
    assert labels() == ["Claude", "Claude second", "Codex", "Grok"]


def test_apply_keeps_each_entrys_role_and_end_when_it_reorders(seeded):
    routing.set_priority([{"account": "Claude", "role": "orchestration"}, {"account": "Codex", "role": "reviews"},
                          {"account": "Grok", "role": "audits", "until": "2026-11-20T00:00:00Z"}],
                         via="app", expect=routing.priority_version())
    _rc, shown = preview(seeded, "put grok first")
    rc, _done = save(seeded, shown["token"])
    assert rc == 0
    rows = routing.read_view(NOW)["priority"]["entries"]
    assert [(e["label"], e["role"]) for e in rows] == [("Grok", "audits"), ("Claude", "orchestration"),
                                                       ("Codex", "reviews")]
    assert rows[0]["until"] == "2026-11-20T00:00:00Z"


def test_a_timed_move_is_an_exception_that_ends_by_itself(seeded):
    _rc, shown = preview(seeded, "put Codex first until Friday")
    rc, done = save(seeded, shown["token"])
    assert rc == 0 and done["receipt"]["text"] == "Codex goes first until Fri 13 Nov, 12:00 am. Then the order goes back."
    entry = next(e for e in routing.read_view(NOW)["priority"]["entries"] if e["label"] == "Codex")
    (exception,) = entry["exceptions"]
    assert exception["text"] == "Codex goes first." and exception["until"] == FRIDAY
    assert labels() == ["Claude", "Claude second", "Codex", "Grok"]  # the stored order is untouched
    later = datetime(2026, 11, 13, 6, 0, tzinfo=timezone.utc)
    ended = next(e for e in routing.read_view(later)["priority"]["entries"] if e["label"] == "Codex")
    assert not ended.get("exceptions")
    rc, undone = undo(seeded, done["action_id"])
    assert rc == 0, undone
    entry = next(e for e in routing.read_view(NOW)["priority"]["entries"] if e["label"] == "Codex")
    assert not entry.get("exceptions")


def test_apply_pauses_until_the_time_shown_and_undo_opens_it(seeded):
    _rc, shown = preview(seeded, "pause grok until Monday")
    assert shown["now"] == "Open to agents"
    assert shown["after"] == "Paused until Mon 16 Nov, 12:00 am. It opens again on its own."
    rc, done = save(seeded, shown["token"])
    assert rc == 0 and done["receipt"]["text"] == "Paused Grok until Mon 16 Nov, 12:00 am."
    assert (state("grok|")["state"], state("grok|")["until"]) == ("paused", MONDAY)
    after_it = datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc)
    assert next(a for a in routing.read_view(after_it)["accounts"] if a["key"] == "grok|")["state"] != "paused"
    rc, undone = undo(seeded, done["action_id"])
    assert rc == 0, undone
    assert state("grok|")["state"] != "paused"


def test_apply_resumes_a_paused_account_and_undo_pauses_it_again(seeded):
    routing.pause("Grok", until="3d", via="app", now=NOW)
    _rc, shown = preview(seeded, "resume Grok")
    assert shown["kind"] == "resume" and shown["now"] == "Paused by you"
    rc, done = save(seeded, shown["token"])
    assert rc == 0, done
    assert state("grok|")["state"] != "paused"
    rc, undone = undo(seeded, done["action_id"])
    assert rc == 0, undone
    assert state("grok|")["state"] == "paused"


def test_apply_sets_where_agents_stop_and_undo_restores_the_level_before(seeded):
    routing.set_cap("Grok", window="weekly", kind="share", pct=95, via="app")
    _rc, shown = preview(seeded, "make agents stop at 90% on Grok")
    assert shown["now"] == "Agents stop at 95% of the week"
    assert shown["after"] == "Agents stop at 90% of the week."
    rc, done = save(seeded, shown["token"])
    assert rc == 0 and done["receipt"]["text"] == "Agents stop at 90% of the week on Grok."
    caps = [c for c in state("grok|")["caps"] if c["kind"] == "share"]
    assert [c["pct"] for c in caps] == [90]
    rc, undone = undo(seeded, done["action_id"])
    assert rc == 0, undone
    assert [c["pct"] for c in state("grok|")["caps"] if c["kind"] == "share"] == [95]


def test_a_stop_level_with_an_end_gives_way_to_the_level_before(seeded):
    routing.set_cap("Grok", window="weekly", kind="share", pct=95, via="app")
    _rc, shown = preview(seeded, "stop Grok at 80% until Friday")
    assert "until Fri 13 Nov, 12:00 am, then the level before applies again" in shown["after"]
    rc, _done = save(seeded, shown["token"])
    assert rc == 0
    later = datetime(2026, 11, 13, 6, 0, tzinfo=timezone.utc)
    grok = next(a for a in routing.read_view(later)["accounts"] if a["key"] == "grok|")
    assert [c["pct"] for c in grok["caps"] if c["kind"] == "share"] == [95]


def test_apply_ends_a_rule_and_undo_brings_it_back(seeded):
    _rc, shown = preview(seeded, "end the rule about Grok")
    assert shown["now"] == "Rule 2: Keep Grok to one job at a time" and shown["kind"] == "end_rule"
    rc, done = save(seeded, shown["token"])
    assert rc == 0 and done["receipt"]["text"] == "Ended rule 2: Keep Grok to one job at a time"
    assert [r["n"] for r in routing.read_view(NOW)["rules"]] == [1]
    rc, undone = undo(seeded, done["action_id"])
    assert rc == 0, undone
    assert [r["n"] for r in routing.read_view(NOW)["rules"]] == [1, 2]


def test_apply_adds_the_words_as_a_rule_and_undo_removes_it(seeded):
    words = "Use Codex for code review and Claude for planning"
    _rc, shown = preview(seeded, words)
    assert shown["fallback"] is True and shown["understood"] == "Add it as a rule in your words"
    assert shown["now"] == "2 rules" and shown["after"].startswith("3 rules, including: Use Codex for code review")
    rc, done = save(seeded, shown["token"])
    assert rc == 0, done
    assert [r["text"] for r in routing.read_view(NOW)["rules"]][-1] == words
    rc, undone = undo(seeded, done["action_id"])
    assert rc == 0, undone
    assert len(routing.read_view(NOW)["rules"]) == 2


# ---- an account that is paused or held does not stop a spoken change, and stays as it is -----------


def test_a_paused_account_in_the_order_stays_paused_when_the_order_changes(seeded):
    routing.pause("Grok", until="3d", via="app", now=NOW)
    _rc, shown = preview(seeded, "put Codex first")
    assert shown["after"].endswith("Any pause or hold on these accounts stays as it is.")
    rc, done = save(seeded, shown["token"])
    assert rc == 0, done
    assert labels()[0] == "Codex" and state("grok|")["state"] == "paused"
    rc, undone = undo(seeded, done["action_id"])
    assert rc == 0, undone
    assert state("grok|")["state"] == "paused"


def test_a_timed_move_and_a_new_rule_keep_a_hold_too(seeded):
    hold = routing.add_hold("wait for Grok", scope="account", account="Grok", reason="keep", owner=True, via="app")
    _rc, shown = preview(seeded, "put Codex first until Friday")
    rc, done = save(seeded, shown["token"])
    assert rc == 0, done
    _rc, shown = preview(seeded, "Use Grok only for audits")
    rc, done = save(seeded, shown["token"])
    assert rc == 0, done
    assert any(h["id"] == hold["id"] for h in routing.read_view(NOW)["holds"])


def test_a_hold_that_appears_after_the_preview_is_a_change(seeded):
    _rc, shown = preview(seeded, "put Codex first")
    routing.add_hold("wait for Grok", scope="account", account="Grok", reason="keep", owner=True, via="app")
    before = snapshot()
    rc, out = save(seeded, shown["token"])
    assert rc == 2 and out["code"] == "changed" and snapshot() == before


# ---- a stale preview changes nothing -----------------------------------------------------------


def _moved(seeded, words: str, move) -> None:
    _rc, shown = preview(seeded, words)
    move()
    before = snapshot()
    rc, out = save(seeded, shown["token"])
    assert rc == 2 and out["code"] == "changed", out
    assert out["error"] == "This changed since you saw it, so nothing was changed. Preview it again."
    assert snapshot() == before


def test_a_moved_priority_is_refused(seeded):
    _moved(seeded, "put Codex first",
           lambda: routing.set_priority(["Grok", "Claude"], via="app", expect=routing.priority_version()))


def test_a_moved_priority_is_refused_for_a_timed_move_too(seeded):
    _moved(seeded, "put Codex first until Friday",
           lambda: routing.set_priority(["Grok", "Codex"], via="app", expect=routing.priority_version()))


def test_an_edited_rule_is_refused(seeded):
    _moved(seeded, "end rule 1", lambda: routing.edit_rule("1", "Prefer Codex for reviews only", via="app"))


def test_a_rule_pin_is_the_rules_own_expect(seeded):
    _rc, shown = preview(seeded, "end rule 1")
    token = shown["token"]
    plan = json.loads(routing_say._token_path().read_text())["tokens"][token]["plan"]
    revision = next(r["revision"] for r in routing.read_view(NOW)["rules"] if r["n"] == 1)
    assert f"--expect={revision}" in plan["argv"] and plan["pin"]["value"] == revision


def test_a_priority_pin_is_the_priorities_own_expect(seeded):
    _rc, shown = preview(seeded, "put Codex first")
    plan = json.loads(routing_say._token_path().read_text())["tokens"][shown["token"]]["plan"]
    assert f"--expect={routing.priority_version()}" in plan["argv"]


def test_a_changed_pause_is_refused(seeded):
    _moved(seeded, "pause Grok until Monday", lambda: routing.pause("Grok", until="2h", via="app", now=NOW))


def test_a_pause_lifted_meanwhile_is_refused_for_a_resume(seeded):
    routing.pause("Grok", until="3d", via="app", now=NOW)
    _moved(seeded, "resume Grok", lambda: routing.lift("Grok", via="app", now=NOW))


def test_a_changed_stop_level_is_refused(seeded):
    _moved(seeded, "stop Grok at 90%",
           lambda: routing.set_cap("Grok", window="weekly", kind="share", pct=80, via="app"))


def test_a_rule_added_meanwhile_is_refused_for_a_new_rule(seeded):
    _moved(seeded, "Use Codex for code review and Claude for planning",
           lambda: routing.add_rule("Something else entirely", via="app"))


# ---- tokens: once, short-lived, and nothing else -------------------------------------------------


def test_a_token_is_used_once(seeded):
    _rc, shown = preview(seeded, "pause Grok for 2 hours")
    rc, _done = save(seeded, shown["token"])
    assert rc == 0
    count = len(owner_direction.actions())
    again = snapshot()
    rc, out = save(seeded, shown["token"])
    assert rc == 2 and out["code"] == "already_used"
    assert len(owner_direction.actions()) == count and snapshot() == again


def test_a_token_that_is_used_while_the_change_fails_is_still_used(seeded):
    _rc, shown = preview(seeded, "pause Grok for 2 hours")
    routing.pause("Grok", until="30m", via="app", now=NOW)
    rc, out = save(seeded, shown["token"])
    assert out["code"] == "changed"
    rc, out = save(seeded, shown["token"])
    assert out["code"] == "already_used"


def test_an_unknown_token_changes_nothing(seeded):
    before = snapshot()
    for token in ("nope", "", "x" * 22):
        rc, out = save(seeded, token)
        assert rc == 2 and out["code"] == "no_such_preview", out
    assert snapshot() == before


def test_an_expired_token_changes_nothing(seeded):
    _rc, shown = preview(seeded, "pause Grok for 2 hours")
    path = routing_say._token_path()
    data = json.loads(path.read_text())
    data["tokens"][shown["token"]]["expires"] = time.time() - 1
    path.write_text(json.dumps(data))
    before = snapshot()
    rc, out = save(seeded, shown["token"])
    assert rc == 2 and out["code"] == "no_such_preview"
    assert snapshot() == before


def test_a_token_lives_ten_minutes(seeded):
    _rc, shown = preview(seeded, "pause Grok for 2 hours")
    row = json.loads(routing_say._token_path().read_text())["tokens"][shown["token"]]
    assert 590 < row["expires"] - time.time() <= 600


def test_the_token_names_only_a_fixed_command(seeded):
    _rc, shown = preview(seeded, "pause Grok for 2 hours; and also delete everything --owner-direction")
    row = json.loads(routing_say._token_path().read_text())["tokens"][shown["token"]]
    argv = row["plan"]["argv"]
    assert argv[:3] == ["route", "rule", "add"] and argv[4] == "--from-app"  # the odd ask is one rule's words
    assert argv[3].startswith("pause Grok for 2 hours;") and len(argv) == 6 and argv[5].startswith("--expect=")


# ---- who may do it: exactly the callers --from-app already admits ---------------------------------


@pytest.mark.parametrize("between", [(), ("/opt/codex",), ("/opt/harness/claude",)])
def test_a_shell_or_an_agents_child_cannot_preview_or_apply(seeded, khipu_app, between):
    _rc, shown = preview(seeded, "pause Grok for 2 hours")
    khipu_app.as_shell(*between)
    before = snapshot()
    rc, out = preview(seeded, "pause Grok for 2 hours")
    assert rc == 2 and out["code"] == "owner_only" and "token" not in out
    assert out["error"] == routing.terminal_refusal()
    rc, out = save(seeded, shown["token"])
    assert rc == 2 and out["code"] == "owner_only"
    assert snapshot() == before and state("grok|")["state"] != "paused"
    khipu_app.as_app()  # the refused apply did not use the token up
    rc, done = save(seeded, shown["token"])
    assert rc == 0 and state("grok|")["state"] == "paused", done


def test_a_dispatched_job_cannot_preview_or_apply(seeded, khipu_app, monkeypatch):
    _rc, shown = preview(seeded, "pause Grok for 2 hours")
    khipu_app.as_shell("/opt/harness/claude")
    monkeypatch.setenv("CLAUDE_DISPATCHED_LANE", "lane-1")
    rc, out = preview(seeded, "pause Grok for 2 hours")
    assert rc == 2 and out["code"] == "owner_only"
    rc, out = save(seeded, shown["token"])
    assert rc == 2 and out["code"] == "owner_only"
    assert state("grok|")["state"] != "paused"


def test_the_flag_is_needed_even_from_the_app(seeded):
    rc, out = seeded.run(["route", "say", "--preview", "pause Grok for 2 hours"])
    assert rc == 2 and out["code"] == "owner_only"
    rc, out = seeded.run(["route", "say", "--apply", "abc"])
    assert rc == 2 and out["code"] == "owner_only"


def test_the_terminal_and_direction_flags_are_not_accepted_here(seeded):
    for flag in ("--owner-direction", "--owner", "--yes"):
        with pytest.raises(SystemExit):
            seeded.run(["route", "say", "--from-app", flag, "--preview", "pause Grok for 2 hours"])


def test_one_mode_at_a_time(seeded):
    rc, out = seeded.run(["route", "say", "--from-app"])
    assert rc == 2 and out["code"] == "bad_args"
    rc, out = seeded.run(["route", "say", "--from-app", "--preview", "x", "--apply", "y"])
    assert rc == 2 and out["code"] == "bad_args"


def test_the_words_never_leave_the_machine(seeded, monkeypatch):
    import urllib.request

    def refuse(*a, **k):
        raise AssertionError("no request may be made with the owner's words")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    rc, shown = preview(seeded, "put Codex first until Friday")
    assert rc == 0
    rc, done = save(seeded, shown["token"])
    assert rc == 0, done
    assert os.environ.get("KHIPU_DATABASE_URL") is None


# ---- review of PR 550: the pin is compared under the write's own lock ------------------------------


def _record() -> dict:
    grok = state("grok|")
    return {"state": grok["state"], "until": grok.get("until"),
            "caps": sorted([c.get("pct"), c.get("until")] for c in grok.get("caps") or []
                           if c.get("kind") == "share" and c.get("window") == "weekly"),
            "rules": [r["text"] for r in routing.read_view(NOW)["rules"]],
            "order": labels()}


@pytest.mark.parametrize("case", ["pause", "resume", "cap", "rule", "end_rule"])
def test_a_write_after_apply_read_its_pin_is_refused(seeded, monkeypatch, case):
    """Another writer lands between apply's own read of the pin and the
    command's write: the command compares the pin under its lock, so the
    newer record stays and the answer is ``changed``."""
    if case == "resume":
        routing.pause("Grok", until="3d", via="app", now=NOW)
    if case == "cap":
        routing.set_cap("Grok", window="weekly", kind="share", pct=95, via="app")
    words = {"pause": "pause Grok for 2 hours", "resume": "resume Grok", "cap": "stop Grok at 90%",
             "rule": "Use Codex for reviews only", "end_rule": "end rule 1"}[case]
    rc, shown = preview(seeded, words)
    assert rc == 0, shown
    original = routing_say.pin_value
    fired = False

    def interleave(spec, now, *rest):
        nonlocal fired
        value = original(spec, now, *rest)
        if not fired:
            fired = True
            if case in ("pause", "resume"):
                routing.pause("Grok", until="7d", via="app", now=NOW)
            elif case == "cap":
                routing.set_cap("Grok", window="weekly", kind="share", pct=50, via="app")
            elif case == "rule":
                routing.add_rule("Do not use Codex for reviews", via="app")
            else:
                routing.edit_rule(1, "Use Codex for testing only", via="app")
        return value

    monkeypatch.setattr(routing_say, "pin_value", interleave)
    rc, done = save(seeded, shown["token"])
    newer = _record()
    assert rc == 2 and done.get("code") == "changed", done
    assert fired
    assert _record() == newer  # nothing of the preview's was written
    if case in ("pause", "resume"):
        assert newer["state"] == routing.PAUSED and newer["until"] == "2026-11-17T17:00:00Z"
    elif case == "cap":
        assert newer["caps"] == [[50, None]]
    elif case == "rule":
        assert newer["rules"][-1] == "Do not use Codex for reviews" and len(newer["rules"]) == 3
    else:
        assert newer["rules"][0] == "Use Codex for testing only"


def test_a_priority_preview_pins_the_order_it_shows(seeded, monkeypatch):
    """The entries a preview shows and its version pin come from one read:
    an order written in between makes Save refuse, never restore the old one."""
    original = routing_say._priority_state
    fired = False

    def interleave(view):
        nonlocal fired
        out = original(view)
        if not fired:
            fired = True
            routing.set_priority([{"account": "Grok", "role": "new owner role"}, {"account": "Codex"},
                                  {"account": "Claude"}], via="app", expect=routing.priority_version())
        return out

    monkeypatch.setattr(routing_say, "_priority_state", interleave)
    rc, shown = preview(seeded, "put Codex first")
    assert rc == 0, shown
    assert shown["now"] == "1 Claude, 2 Claude second, 3 Codex, 4 Grok"  # what was read
    assert labels() == ["Grok", "Codex", "Claude"]  # what is there now
    rc, done = save(seeded, shown["token"])
    assert rc == 2 and done.get("code") == "changed", done
    assert labels() == ["Grok", "Codex", "Claude"]
    held = routing.read_view(NOW)["priority"]["entries"]
    assert held[0].get("role") == "new owner role"


def test_resume_says_the_pause_ends_and_what_the_account_is_then(seeded):
    routing.report(account="Grok", state="limited", until="2h", via="cli", now=NOW)
    routing.pause("Grok", until="3d", via="app", now=NOW)
    rc, shown = preview(seeded, "resume Grok")
    assert rc == 0, shown
    assert "open to agents" not in shown["after"].lower(), shown
    assert "limited" in shown["after"].lower(), shown
    rc, done = save(seeded, shown["token"])
    assert rc == 0, done
    assert state("grok|")["state"] == "limited"
    text = done["receipt"]["text"]
    assert "open to agents" not in text.lower(), done
    assert text.startswith("Lifted the pause on Grok.") and "still limited until" in text, text


def test_resume_of_an_account_with_nothing_else_on_it_says_it_is_open(seeded):
    routing.report(account="Grok", state="open", via="cli", now=NOW)
    routing.pause("Grok", until="3d", via="app", now=NOW)
    rc, shown = preview(seeded, "resume Grok")
    assert rc == 0, shown
    rc, done = save(seeded, shown["token"])
    assert rc == 0, done
    assert state("grok|")["state"] == routing.OPEN
    assert done["receipt"]["text"] == "Lifted the pause on Grok. It is open to agents again.", done
