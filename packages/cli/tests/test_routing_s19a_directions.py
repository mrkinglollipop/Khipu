"""Routing slice 19a (case 17c, directions part): directions that never
reach Khipu. Hermetic: a temp capture home and data dir, turns recorded the
way the prompt hook records them, no hub, no model."""
from __future__ import annotations

import json
import time

import pytest

from khipu import owner_direction, owner_turns, routing_directions

SESSION = "s-19a"
ENV = {"CLAUDE_CODE_SESSION_ID": SESSION}
CLAUDE_ENV = {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}
T0 = 1_790_000_000.0
GLM = "use GLM only one job at a time"


@pytest.fixture
def world(tmp_path, monkeypatch, khipu_app):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": [
        {"label": "Claude", "harness": "claude"}, {"label": "Codex", "harness": "codex"},
        {"label": "Grok", "harness": "grok"}, {"label": "GLM", "harness": "claude"}]}))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    for name in ("KHIPU_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
        monkeypatch.delenv(name, raising=False)
    khipu_app.as_shell("/opt/harness/claude")
    owner_turns.write_settings({"enabled": True})
    return tmp_path


def say(text, *, now=None, session=SESSION):
    return owner_turns.record({"session_id": session, "prompt": text, "cwd": "/work"},
                              CLAUDE_ENV, now=time.time() if now is None else now)


def act(*, facts=None, target="x", now=None):
    """An applied owner action by direction, from the session's latest turn."""
    want = owner_direction.intent("route.cap.set" if (facts or {}).get("cap") else "route.rule.add", target, facts=facts)
    owner_direction.gate(want, env=ENV, now=time.time() if now is None else now)
    return owner_direction.finish({"ok": True})


def cap(value, *, now=None):
    return act(facts={"cap": {"account": "GLM", "kind": "concurrency", "value": value}}, now=now)


def scan(texts, **kw):
    return routing_directions.capture_scan(SESSION, texts, **kw)


# ---- the recorder: hash, occurrence, never the words -----------------------


def test_record_stores_only_the_hash_and_its_occurrence(world):
    say(GLM)
    say(GLM + "  ")
    rows = owner_turns.turns(SESSION)
    assert [r["occurrence"] for r in rows] == [1, 2]
    assert rows[0]["prompt_sha256"] == owner_turns.prompt_sha256("use  GLM\tonly one job at a time")
    disk = owner_turns.session_file(SESSION).read_text()
    assert "GLM" not in disk and rows[0]["prompt_sha256"] in disk


def test_occurrence_counter_never_expires_with_the_turns(world):
    say(GLM, now=T0)
    later = T0 + 31 * 86400
    assert owner_turns.turns(SESSION, now=later) == []  # the 24-hour prune took the turn
    assert owner_turns.occurrence_state(SESSION)["counts"][owner_turns.prompt_sha256(GLM)] == 1
    say(GLM, now=later)
    assert owner_turns.turns(SESSION, now=later)[-1]["occurrence"] == 2


def test_the_check_stamp_and_log_carry_the_triple_not_the_words(world):
    say(GLM)
    stamp = owner_direction.check("route.pause", env=ENV)
    assert stamp["prompt_sha256"] == owner_turns.prompt_sha256(GLM) and stamp["occurrence"] == 1
    cap(1)
    (line,) = [a for a in owner_direction.actions() if a["state"] == "applied"]
    assert (line["session"], line["prompt_sha256"], line["occurrence"]) == (SESSION, stamp["prompt_sha256"], 1)
    assert GLM not in owner_direction.log_path().read_text()


# ---- case 17c: the owed item and how it closes ------------------------------


def test_a_direction_opens_one_owed_item_naming_the_words(world):
    say(GLM)
    rows = scan([GLM])
    assert len(rows) == 1 and rows[0]["target"] == {"entity": "GLM", "kind": "concurrency", "value": 1}
    (item,) = routing_directions.owed_items()
    assert item["text"] == f"Routing said here, not in Khipu: '{GLM}'"
    assert len(scan([GLM])) == 1  # the next capture does not open a second


def test_the_matching_cap_closes_it_and_cap_4_leaves_it_open(world):
    say(GLM)
    cap(4)
    scan([GLM])
    assert len(routing_directions.owed_items()) == 1
    cap(1)
    scan([GLM])
    assert routing_directions.owed_items() == []


def test_an_unrelated_routing_write_leaves_it_open(world):
    say(GLM)
    scan([GLM])
    act(facts={"note": "unrelated"})
    routing_directions.refresh()
    assert len(routing_directions.owed_items()) == 1


@pytest.mark.parametrize("text", [
    "Should we use GLM only one job at a time?",
    "What if we used GLM only one job at a time?",
    '"use GLM only one job at a time" is what the runner says.',
    "He said use GLM only one job at a time yesterday.",
])
def test_a_question_a_quote_and_a_hypothetical_open_nothing(world, text):
    say(text)
    assert scan([text]) == []
    assert routing_directions.extract(text) == []


def test_a_job_prompt_opens_nothing(world):
    brief = "Build the fixture; use GLM only one job at a time while you do."
    owner_turns.note_brief(brief, parent="parent-1", harness="claude_code")
    say(brief, session="s-job")
    assert routing_directions.capture_scan("s-job", [brief]) == []


def test_two_directions_with_one_written_leave_one_open(world):
    text = "Use GLM only one job at a time. Use Grok for the builds."
    say(text)
    rows = scan([text])
    assert len(rows) == 2
    cap(1)
    rows = routing_directions.refresh()
    assert [r["status"] for r in rows] == ["closed", "open"]
    assert [i["words"] for i in routing_directions.owed_items()] == ["Use Grok for the builds."]


def test_a_direction_without_a_value_closes_only_when_cited(world):
    say("Use Grok for the builds.")
    (row,) = scan(["Use Grok for the builds."])
    assert row["target"]["kind"] is None
    cap(1)
    routing_directions.refresh()
    assert len(routing_directions.owed_items()) == 1
    act(facts={"direction": row["id"]})
    routing_directions.refresh()
    assert routing_directions.owed_items() == []


def test_a_dismissal_survives_the_next_capture(world):
    say(GLM)
    (row,) = scan([GLM])
    routing_directions.dismiss(row["id"])
    scan([GLM])
    assert routing_directions.owed_items() == []
    assert routing_directions.directions()[0]["status"] == "dismissed"


def test_each_occurrence_closes_only_on_its_own_action(world):
    say(GLM)
    say(GLM)  # identical prompt, occurrence 2: the check acts from this turn
    cap(1)
    rows = scan([GLM, GLM])
    assert [(r["occurrence"], r["status"]) for r in rows] == [(1, "open"), (2, "closed")]
    assert len(routing_directions.owed_items()) == 1


def test_a_missed_recorder_turn_leaves_the_direction_unjoined(world):
    say("a different prompt entirely")
    (row,) = scan([GLM])  # the hook never recorded this turn
    assert row["status"] == "open" and row["join"] == "unjoined"
    assert routing_directions.owed_items()[0]["join"] == "unjoined"


def test_a_deleted_counter_file_leaves_earlier_directions_unjoined(world):
    say(GLM)
    (row,) = scan([GLM])
    assert row["join"] == "joined"
    owner_turns.occurrence_file(SESSION).unlink()
    (row,) = scan([GLM])
    assert row["join"] == "unjoined" and row["status"] == "open"


def test_a_late_copy_sharing_an_occurrence_is_one_typing_in_the_scan(world):
    """An Antigravity turn recorded from ``lastUserInput`` and again from its
    flushed step is two rows with one occurrence; the scan numbers it once."""
    say(GLM)
    second = say(GLM)
    owner_turns.record({"session_id": SESSION, "prompt": GLM, "cwd": "/work"}, CLAUDE_ENV,
                       extra={"duplicate_of": second, "occurrence": 2})
    assert [r["occurrence"] for r in owner_turns.turns(SESSION)] == [1, 2, 2]
    assert [r["occurrence"] for r in scan([GLM, GLM], actions=[])] == [1, 2]


def test_pruned_turns_still_join_and_idle_days_keep_occurrences(world):
    say(GLM, now=T0)
    cap(1, now=T0)
    later = T0 + 31 * 86400
    assert owner_turns.turns(SESSION, now=later) == []
    (row,) = routing_directions.capture_scan(SESSION, [{"text": GLM, "at": T0}], now=later)
    assert row["status"] == "closed"  # the log's triple outlives the pruned turn
    say(GLM, now=later)
    assert owner_turns.turns(SESSION, now=later)[-1]["occurrence"] == 2
    cap(1, now=later)
    rows = routing_directions.capture_scan(SESSION, [{"text": GLM, "at": T0}, {"text": GLM, "at": later}], now=later)
    assert [(r["occurrence"], r["status"]) for r in rows] == [(1, "closed"), (2, "closed")]


def test_only_the_words_store_holds_prompt_text(world):
    say(GLM)
    cap(1)
    scan([GLM])
    assert GLM in routing_directions.store_path().read_text()
    assert GLM not in owner_direction.log_path().read_text()
    assert GLM not in owner_turns.session_file(SESSION).read_text()
