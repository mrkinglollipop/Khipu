"""A T3 conversation's prompt opens a routing reminder only when T3's own
message record shows the owner typed it: T3's scheduled wake-ups carry a
conversation's own notes ("Keep it short; this conversation spends the main
Claude account..."), never his words (2026-11-12: eight such reminders were
listed as his). Same words said again in one conversation are one reminder.
Hermetic."""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

from khipu import owner_turns, routing_directions
from tests.test_routing_s19a_directions import CLAUDE_ENV, GLM, SESSION, world  # noqa: F401

THREAD = "thread-1"


def _t3_db(path, messages):
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE orchestration_v2_projection_provider_threads (thread_id TEXT, payload_json TEXT)")
        con.execute("INSERT INTO orchestration_v2_projection_provider_threads VALUES (?, ?)",
                    (THREAD, json.dumps({"nativeThreadRef": {"nativeId": SESSION}})))
        con.execute("CREATE TABLE orchestration_v2_projection_messages (message_id TEXT, thread_id TEXT, role TEXT, "
                    "created_at TEXT, payload_json TEXT)")
        for i, (by, source, text) in enumerate(messages):
            con.execute("INSERT INTO orchestration_v2_projection_messages VALUES (?, ?, 'user', ?, ?)",
                        (f"m{i}", THREAD, f"2026-11-12T00:00:0{i}Z",
                         json.dumps({"createdBy": by, "creationSource": source, "text": text, "threadId": THREAD})))


@pytest.fixture
def t3_world(world, monkeypatch):
    monkeypatch.setattr(owner_turns, "entrypoint_of", lambda *a, **k: "t3")
    monkeypatch.setattr(owner_turns, "dispatched_job", lambda *a, **k: None)
    db = world / "t3.sqlite"
    monkeypatch.setenv("KHIPU_T3_DB", str(db))
    return db


def say(text):
    return owner_turns.record({"session_id": SESSION, "prompt": text, "cwd": "/work"}, CLAUDE_ENV, now=time.time())


WAKE = ("Olivewood orchestrator wake (scheduled backstop). Keep it short; this conversation spends the main "
        "Claude account, which must last until Saturday.")


def test_a_t3_scheduled_wake_opens_no_reminder(t3_world):
    _t3_db(t3_world, [("agent", "mcp", WAKE)])
    say(WAKE)
    assert routing_directions.capture_scan(SESSION, [WAKE]) == []
    assert routing_directions.owed_items() == []


def test_a_direction_he_typed_in_t3_opens_one(t3_world):
    _t3_db(t3_world, [("user", "web", GLM), ("agent", "mcp", WAKE)])
    say(GLM)
    say(WAKE)
    (row,) = routing_directions.capture_scan(SESSION, [GLM, WAKE])
    assert row["typed"] is True and row["status"] == "open", row
    (item,) = routing_directions.owed_items()
    assert item["words"] == row["words"]


def test_an_unreadable_t3_record_opens_no_reminder(t3_world):
    t3_world.write_text("not a database")
    say(GLM)
    assert all(not r.get("typed") for r in routing_directions.capture_scan(SESSION, [GLM]))
    assert routing_directions.owed_items() == []


def test_a_row_marked_his_before_the_check_stops_being_listed(t3_world, monkeypatch):
    _t3_db(t3_world, [("agent", "mcp", WAKE)])
    say(WAKE)
    sha = owner_turns.turns(SESSION)[-1]["prompt_sha256"]
    monkeypatch.setattr(routing_directions, "t3_owner_hashes", lambda session: {sha})  # the old blanket rule
    (row,) = routing_directions.capture_scan(SESSION, [WAKE])
    assert row["typed"] is True
    monkeypatch.undo()
    monkeypatch.setattr(owner_turns, "entrypoint_of", lambda *a, **k: "t3")
    monkeypatch.setattr(owner_turns, "dispatched_job", lambda *a, **k: None)
    monkeypatch.setenv("KHIPU_T3_DB", str(t3_world))
    (row,) = routing_directions.capture_scan(SESSION, [WAKE])
    assert row["typed"] is False
    assert routing_directions.owed_items() == []


def test_the_same_words_said_again_are_one_reminder_and_one_dismissal(world):
    for _ in range(3):
        say(GLM)
    rows = routing_directions.capture_scan(SESSION, [GLM, GLM, GLM])
    assert len(rows) == 3 and all(r["status"] == "open" for r in rows)
    (item,) = routing_directions.owed_items()
    routing_directions.dismiss(item["id"])
    assert routing_directions.owed_items() == []
    assert all(r["status"] == "dismissed" for r in routing_directions.directions())
