"""Capture's direction scan opens an owed item only from a turn owner
provenance counts as the owner's (``owner_turns.eligible``), not from any
USER: line of a transcript (routing audit 2026-11-02). Hermetic."""
from __future__ import annotations

import tempfile
import time

from khipu import owner_turns, routing_directions
from tests.test_routing_s19a_directions import CLAUDE_ENV, GLM, SESSION, world  # noqa: F401

NAMES = {"sdk": {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "sdk-ts"}}


def say(text, *, env=CLAUDE_ENV, cwd="/work"):
    return owner_turns.record({"session_id": SESSION, "prompt": text, "cwd": cwd}, env, now=time.time())


def scan(text=GLM):
    return routing_directions.capture_scan(SESSION, [text])


def test_a_trusted_typed_turn_opens_the_item(world):
    say(GLM)
    (row,) = scan()
    assert row["words"] and row["status"] == "open"


def test_a_machine_entrypoint_turn_opens_nothing(world):
    turn = say(GLM, env=NAMES["sdk"])
    assert owner_turns.turns(SESSION)[-1]["eligible"] is False
    assert scan() == []


def test_a_t3_helper_session_opens_nothing(world):
    helper = f"{tempfile.gettempdir()}/t3code-claude-title-abc123"
    say(GLM, cwd=helper)
    assert owner_turns.turns(SESSION)[-1]["eligible"] is False
    assert scan() == []


def test_a_t3_conversation_opens_nothing(world, monkeypatch):
    monkeypatch.setattr(owner_turns, "entrypoint_of", lambda *a, **k: "t3")
    say(GLM)
    assert owner_turns.turns(SESSION)[-1]["eligible"] is False
    assert scan() == []


def test_a_dispatched_jobs_turn_opens_nothing(world, monkeypatch):
    monkeypatch.setattr(owner_turns, "dispatched_job", lambda *a, **k: "a job")
    say(GLM)
    assert owner_turns.turns(SESSION)[-1]["eligible"] is False
    assert scan() == []


def test_one_text_typed_by_him_and_sent_by_a_machine_opens_one_item(world):
    say(GLM, env=NAMES["sdk"])
    say(GLM)
    (row,) = scan()
    assert row["occurrence"] == 2  # his typing is the second; the machine's copy took the first


def test_a_text_the_recorder_never_saw_still_waits_unjoined(world):
    (row,) = scan("use GLM only one job at a time, please")
    assert row["join"] == "unjoined"  # a missed hook: kept as before, joined once a record arrives
