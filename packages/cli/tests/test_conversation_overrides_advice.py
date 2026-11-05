"""Conversation overrides: what they change in advice, for whom, and when they
stop.  Fake usage only (the routing replay fixture)."""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

import pytest

from khipu import conversation_overrides as overrides, routing
from tests.test_routing_acceptance import AUTHOR  # noqa: F401
from tests.test_routing_s17c_advise import _outside_a_job, _pace_target  # noqa: F401

pytest_plugins = ("tests.test_routing_acceptance",)

MINE = {"KHIPU_SESSION_ID": "sess-mine", "KHIPU_HARNESS": "claude_code"}
OTHER = {"KHIPU_SESSION_ID": "sess-other", "KHIPU_HARNESS": "claude_code"}


@pytest.fixture(autouse=True)
def _no_t3(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_T3_DB", str(tmp_path / "no-t3.sqlite"))


def _grant(replay, *, session="sess-mine", hours=3, **extra):
    now = replay.now
    values = {"account": "Claude", "effect": "ahead_of_pace", "request": "let it run ahead",
              "subject": {"kind": "session", "id": session, "mac": routing._local_mac()}, **extra}
    if "indefinite" not in extra:
        values["until"] = routing._iso(now + timedelta(hours=hours))
    return overrides.grant(values, now=now)


def test_override_changes_only_the_named_conversations_pace_answer(replay):
    _pace_target(replay, used=18)
    before_other = routing.advise("Claude", now=replay.now, environ=OTHER)
    assert before_other["verdict"] == "hold_back"
    row = _grant(replay)
    mine = routing.advise("Claude", now=replay.now, environ=MINE)
    other = routing.advise("Claude", now=replay.now, environ=OTHER)
    assert mine["verdict"] == "go" and mine["reason"].startswith("override until ")
    assert mine["override"]["id"] == row["id"]
    # Everyone else: byte-identical to what they heard before the override.
    assert json.dumps(other, sort_keys=True) == json.dumps(before_other, sort_keys=True)
    assert "override" not in other


def test_override_never_lifts_a_hold_or_a_pause(replay):
    _pace_target(replay, used=18)
    _grant(replay)
    routing.add_hold("machine busy", reason="machine-busy", scope="account", account="Claude", owner=True, via="app")
    held = routing.advise("Claude", now=replay.now, environ=MINE)
    assert held["verdict"] == "closed" and held["reason"] == "held"


def test_ceiling_holds_back_when_the_account_is_over_it(replay):
    _pace_target(replay, used=18)
    _grant(replay, ceiling_pct=10)
    over = routing.advise("Claude", now=replay.now, environ=MINE)
    assert over["verdict"] == "hold_back" and "override ceiling 10% of the weekly window reached" in over["reason"]
    _grant(replay, ceiling_pct=60)
    under = routing.advise("Claude", now=replay.now, environ=MINE)
    assert under["verdict"] == "go"


def test_a_ceiling_with_no_reading_holds_back_rather_than_guessing(replay):
    routing.report(account="Claude", state="open", author=AUTHOR)
    _grant(replay, ceiling_pct=50)
    out = routing.advise("Claude", now=replay.now, environ=MINE)
    assert out["verdict"] == "hold_back" and "can't be checked" in out["reason"]


def test_the_override_stops_applying_by_itself_at_its_end(replay):
    _pace_target(replay, used=18)
    _grant(replay, hours=3)
    assert routing.advise("Claude", now=replay.now, environ=MINE)["verdict"] == "go"
    later = replay.now + timedelta(hours=3, seconds=1)
    assert overrides.list_overrides(now=later) == []
    assert "override" not in routing.advise("Claude", now=later, environ=MINE)


def test_a_second_override_for_the_same_conversation_replaces_the_first(replay):
    first = _grant(replay, hours=3)
    second = _grant(replay, hours=5)
    live = overrides.list_overrides(now=replay.now)
    assert [r["id"] for r in live] == [second["id"]]
    assert second["replaced"] == [{"override": first["id"], "end": second["replaced"][0]["end"]}]
    # Four retries of the same grant still leave one.
    for _ in range(4):
        _grant(replay, hours=5)
    assert len(overrides.list_overrides(now=replay.now)) == 1


def test_an_unknown_conversation_hears_normal_pace_and_why(replay):
    _pace_target(replay, used=18)
    _grant(replay)
    out = routing.advise("Claude", now=replay.now, environ={})
    assert out["verdict"] == "hold_back" and "override" not in out
    assert out["override_refused"] == "Khipu can't identify this conversation, so it uses normal pace."


def test_indefinite_override_asks_again_after_its_review_date_but_keeps_applying(replay):
    _pace_target(replay, used=18)
    row = _grant(replay, indefinite=True)
    later = replay.now + timedelta(days=8)
    key = routing._resolve_account_key("Claude")
    got = overrides.advice(key, environ=MINE, now=later)[0]
    assert got is not None and got["review_due"] is True
    kept = overrides.keep(row["id"], now=later)
    assert overrides.list_overrides(now=later)[0]["review_at"] == kept["review_at"]
    assert overrides.advice(key, environ=MINE, now=later)[0]["review_due"] is False


def test_helpers_share_the_override_unless_it_says_otherwise(replay):
    _pace_target(replay, used=18)
    helper = dict(MINE, KHIPU_JOB="1")
    helper = {"KHIPU_SESSION_ID": "sess-helper", "KHIPU_HARNESS": "claude_code",
              "KHIPU_PARENT_SESSION": "claude_code:sess-mine"}
    _grant(replay)
    assert routing.advise("Claude", now=replay.now, environ=helper)["verdict"] == "go"
    _grant(replay, children=False)
    assert routing.advise("Claude", now=replay.now, environ=helper)["verdict"] == "hold_back"
    assert routing.advise("Claude", now=replay.now, environ=MINE)["verdict"] == "go"


def test_a_native_helper_with_the_parents_session_id_is_excluded_when_helpers_are_off(replay):
    _pace_target(replay, used=18)
    _grant(replay, children=False)
    same_id_helper = dict(MINE, KHIPU_JOB="1")
    assert routing.advise("Claude", now=replay.now, environ=same_id_helper)["verdict"] == "hold_back"
    assert routing.advise("Claude", now=replay.now, environ=MINE)["verdict"] == "go"


def _t3(tmp_path, monkeypatch, rows):
    path = tmp_path / "t3.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE orchestration_v2_projection_provider_threads (thread_id TEXT, payload_json TEXT)")
    con.execute("CREATE TABLE orchestration_v2_projection_threads (thread_id TEXT, payload_json TEXT)")
    for thread, native, parent in rows:
        con.execute("INSERT INTO orchestration_v2_projection_provider_threads VALUES (?, ?)",
                    (thread, json.dumps({"nativeThreadRef": {"nativeId": native}})))
        con.execute("INSERT INTO orchestration_v2_projection_threads VALUES (?, ?)",
                    (thread, json.dumps({"lineage": {"parentThreadId": parent}})))
    con.commit()
    con.close()
    monkeypatch.setenv("KHIPU_T3_DB", str(path))


def test_a_t3_helper_matches_through_its_parents_link_while_the_parent_override_lives(replay, tmp_path, monkeypatch):
    _t3(tmp_path, monkeypatch, [("th-parent", "sess-mine", None), ("th-child", "sess-child", "th-parent"),
                                ("th-stranger", "sess-stranger", None)])
    _pace_target(replay, used=18)
    row = overrides.grant({"account": "Claude", "effect": "ahead_of_pace", "request": "x",
                           "subject": {"kind": "thread", "id": "th-parent", "mac": routing._local_mac()},
                           "until": routing._iso(replay.now + timedelta(hours=3))}, now=replay.now)
    child = {"KHIPU_SESSION_ID": "sess-child", "KHIPU_HARNESS": "claude_code"}
    stranger = {"KHIPU_SESSION_ID": "sess-stranger", "KHIPU_HARNESS": "claude_code"}
    assert routing.advise("Claude", now=replay.now, environ=MINE)["verdict"] == "go"
    assert routing.advise("Claude", now=replay.now, environ=child)["verdict"] == "go"
    assert routing.advise("Claude", now=replay.now, environ=stranger)["verdict"] == "hold_back"
    # Once the parent's override ends, the child hears normal pace again.
    overrides.end(row["id"], now=replay.now)
    assert routing.advise("Claude", now=replay.now, environ=child)["verdict"] == "hold_back"


def test_a_t3_thread_it_cannot_identify_gets_normal_pace(replay, tmp_path, monkeypatch):
    _t3(tmp_path, monkeypatch, [("th-a", "sess-mine", None), ("th-b", "sess-mine", None)])
    _pace_target(replay, used=18)
    _grant(replay)
    out = routing.advise("Claude", now=replay.now, environ=MINE)
    assert out["verdict"] == "hold_back"
    assert out["override_refused"] == "Khipu can't identify this T3 thread, so it uses normal pace."


def test_an_override_for_a_whole_folder_reaches_each_conversation_in_it(replay, monkeypatch):
    _pace_target(replay, used=18)
    overrides.grant({"account": "Claude", "effect": "ahead_of_pace", "request": "x",
                     "subject": {"kind": "project", "id": "synthetic-folder", "mac": routing._local_mac()},
                     "until": routing._iso(replay.now + timedelta(hours=3))}, now=replay.now)
    monkeypatch.setattr(routing, "_project_name", lambda cwd: "synthetic-folder")
    assert routing.advise("Claude", now=replay.now, environ=MINE)["verdict"] == "go"
    monkeypatch.setattr(routing, "_project_name", lambda cwd: "elsewhere")
    assert routing.advise("Claude", now=replay.now, environ=OTHER)["verdict"] == "hold_back"


def test_a_folder_override_reaches_a_t3_conversation_in_that_folder_only(replay, tmp_path, monkeypatch):
    _t3(tmp_path, monkeypatch, [("th-in", "sess-mine", None), ("th-out", "sess-other", None)])
    _pace_target(replay, used=18)
    overrides.grant({"account": "Claude", "effect": "ahead_of_pace", "request": "x",
                     "subject": {"kind": "project", "id": "synthetic-folder", "mac": routing._local_mac()},
                     "until": routing._iso(replay.now + timedelta(hours=3))}, now=replay.now)
    monkeypatch.setattr(routing, "_project_name", lambda cwd: "synthetic-folder")
    assert routing.advise("Claude", now=replay.now, environ=MINE)["verdict"] == "go"
    monkeypatch.setattr(routing, "_project_name", lambda cwd: "elsewhere")
    assert routing.advise("Claude", now=replay.now, environ=OTHER)["verdict"] == "hold_back"
