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


def test_a_direction_typed_in_a_t3_conversation_still_opens_the_item(world, monkeypatch):
    """T3 is the owner's main harness and opening an item authorizes nothing;
    ``owner_turns.eligible`` still refuses T3 for every authorization path."""
    monkeypatch.setattr(owner_turns, "entrypoint_of", lambda *a, **k: "t3")
    monkeypatch.setattr(owner_turns, "dispatched_job", lambda *a, **k: None)  # T3's record is readable: no job
    say(GLM)
    assert owner_turns.turns(SESSION)[-1]["eligible"] is False
    sha = owner_turns.turns(SESSION)[-1]["prompt_sha256"]
    monkeypatch.setattr(routing_directions, "t3_owner_messages",
                        lambda session: [(time.time(), {sha})])  # T3: he typed it
    (row,) = scan()
    assert row["status"] == "open"


def test_a_t3_machine_or_handoff_turn_opens_nothing(world, monkeypatch):
    monkeypatch.setattr(owner_turns, "entrypoint_of", lambda *a, **k: "t3")
    monkeypatch.setattr(owner_turns, "dispatched_job", lambda *a, **k: None)
    machine = f"<task-notification>\n<task-id>x</task-id>\n<summary>{GLM}</summary>\n</task-notification>"
    handoff = "Context handoff (summary):\nProvider context handoff. Thread: t-1.\n\nUser message:"
    say(machine)
    say(handoff)
    turns = owner_turns.turns(SESSION)
    assert turns[0]["machine"] and not turns[0]["eligible"] and not turns[1]["typed"]
    assert routing_directions.capture_scan(SESSION, [machine, handoff]) == []


def test_a_t3_helper_turn_opens_nothing(world, monkeypatch):
    monkeypatch.setattr(owner_turns, "entrypoint_of", lambda *a, **k: "t3")
    say(GLM, cwd=f"{tempfile.gettempdir()}/t3code-claude-title-abc123")
    assert scan() == []


def test_a_dispatched_jobs_turn_opens_nothing(world, monkeypatch):
    monkeypatch.setattr(owner_turns, "dispatched_job", lambda *a, **k: "a job")
    say(GLM)
    assert owner_turns.turns(SESSION)[-1]["eligible"] is False
    assert scan() == []
    monkeypatch.setattr(owner_turns, "entrypoint_of", lambda *a, **k: "t3")
    assert scan() == []  # nor in T3


def test_one_text_typed_by_him_and_sent_by_a_machine_opens_one_item(world):
    say(GLM, env=NAMES["sdk"])
    say(GLM)
    (row,) = scan()
    assert row["occurrence"] == 2  # his typing is the second; the machine's copy took the first


def test_a_text_the_recorder_never_saw_still_waits_unjoined(world):
    (row,) = scan("use GLM only one job at a time, please")
    assert row["join"] == "unjoined"  # a missed hook: kept as before, joined once a record arrives


# ---- a turn recorded t3-unverified (review of #568 and #570, 2026-11-11) ----

_UNVERIFIED = {"session": SESSION, "entrypoint": "t3-unverified", "native": "claude-desktop",
               "harness": "claude_code", "typed": True}


def test_his_judges_an_unverified_turn_by_its_native_front_end_once_t3_settles_none(world):
    assert routing_directions._his(_UNVERIFIED, owner_turns.settings(), t3="none")


def test_his_keeps_the_t3_rule_for_an_unverified_turn_t3_runs(world):
    conf = owner_turns.settings()
    turn = dict(_UNVERIFIED, prompt_sha256="h1")
    yes = lambda turn: True  # noqa: E731 - T3's record holds his message
    assert routing_directions._his(turn, conf, t3="t3", t3_owner=yes)
    assert not routing_directions._his(dict(turn, machine=True), conf, t3="t3", t3_owner=yes)
    assert not routing_directions._his(turn, conf, t3="t3", t3_owner=lambda turn: False)  # not his message
    assert not routing_directions._his(turn, conf, t3="t3")  # T3's record unread


def test_his_refuses_an_unverified_turn_while_t3_stays_unreadable(world):
    conf = owner_turns.settings()
    assert not routing_directions._his(_UNVERIFIED, conf, t3="unverified")
    assert not routing_directions._his(_UNVERIFIED, conf)


def test_a_scan_opens_an_unverified_turn_once_t3_settles_none(world, monkeypatch):
    import sqlite3

    real = owner_turns._t3_membership
    monkeypatch.setattr(owner_turns, "_t3_membership", lambda session: "unverified")
    say(GLM)
    assert owner_turns.turns(SESSION)[-1]["entrypoint"] == "t3-unverified"
    db = world / "t3.sqlite"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE orchestration_v2_projection_provider_threads (thread_id TEXT, payload_json TEXT)")
    monkeypatch.setenv("KHIPU_T3_DB", str(db))
    monkeypatch.setattr(owner_turns, "_t3_membership", real)
    (row,) = scan()
    assert row["typed"] is True and row["status"] == "open"


def test_a_scheduled_prompt_opens_no_item(world, monkeypatch):
    import json
    from datetime import datetime, timezone
    from pathlib import Path

    transcript = Path(world) / "t.jsonl"
    now = time.time()
    stamp = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    owner_turns.record({"session_id": SESSION, "prompt": GLM, "cwd": "/work", "transcript_path": str(transcript)},
                       CLAUDE_ENV, now=now)
    transcript.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": GLM},
                                      "timestamp": stamp, "isMeta": True, "promptSource": "system",
                                      "wakeupSource": "schedule_wakeup", "turnOrigin": "scheduled"}) + "\n")
    assert scan() == []
