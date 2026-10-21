"""Routing slice 19a fix round 1: every finding from the cross-family
review in /tmp/khipu-routing-s19a/fix1/review.md, as a test. Hermetic:
a temp capture home and data dir, fake data only, no hub, no model."""
from __future__ import annotations

import json
import time
import types

import pytest

from khipu import cli, mcp_server, owner_direction, owner_turns, recall_rule, routing_directions

SESSION = "s-19a-fix1"
ENV = {"CLAUDE_CODE_SESSION_ID": SESSION}
CLAUDE_ENV = {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}
T0 = 1_790_000_000.0
GLM = "use GLM only one job at a time"


@pytest.fixture
def world(tmp_path, monkeypatch, khipu_app):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": [
        {"label": "Claude", "harness": "claude"}, {"label": "Claude second", "harness": "claude"},
        {"label": "Codex", "harness": "codex"}, {"label": "Grok", "harness": "grok"},
        {"label": "GLM", "harness": "claude"}]}))
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
    want = owner_direction.intent("route.rule.add", target, facts=facts)
    owner_direction.gate(want, env=ENV, now=time.time() if now is None else now)
    return owner_direction.finish({"ok": True})


def cap(value, *, account="GLM", now=None):
    return act(facts={"cap": {"account": account, "kind": "concurrency", "value": value}}, now=now)


def scan(texts, **kw):
    return routing_directions.capture_scan(SESSION, texts, **kw)


# ---- P1 (1): ordinary prompts open nothing ---------------------------------


@pytest.mark.parametrize("text", [
    "Use the grep tool to find foo",
    "Keep it simple",
    "set up the test fixtures for the parser",
    "limit the output to 5 lines",
    "Send the email to Bob",
    "Use Python 3.11 for this",
    "Don't use Redux here",
    "Prefer Tabs over spaces",
    "Set Foo to $5 in the budget file",
])
def test_ordinary_prompts_open_nothing(world, text):
    assert routing_directions.extract(text) == []
    say(text)
    assert scan([text]) == []
    assert routing_directions.owed_items() == []


def test_the_sentence_splitter_keeps_a_version_number_whole(world):
    (found,) = routing_directions.extract("Route the Python 3.11 jobs to GLM.")
    assert found["words"] == "Route the Python 3.11 jobs to GLM."
    assert found["target"]["entity"] == "GLM"


def test_a_spend_direction_needs_and_names_a_real_account(world):
    (found,) = routing_directions.extract("Keep GLM spend to $35 a week.")
    assert found["target"] == {"entity": "GLM", "kind": "spend", "value": 35}


def test_dismiss_through_the_cli_owed_close_survives_capture(world, capsys):
    say(GLM)
    (row,) = scan([GLM])
    args = types.SimpleNamespace(snooze=None, dismiss=None, close=row["id"], reopen=None,
                                   reassign=None, reason=None, until=None)
    assert cli.cmd_owed(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["dismissed"][0]["status"] == "dismissed"
    scan([GLM])
    assert routing_directions.owed_items() == []
    assert routing_directions.directions()[0]["status"] == "dismissed"


# ---- P1 (2): occurrences come from the owner turn record, not the window ---


def test_a_prompt_typed_in_two_windows_opens_two_directions(world):
    say(GLM)
    scan([GLM])  # window 1 holds only the first typing
    say(GLM)
    rows = scan([GLM])  # window 2 holds only the second typing
    assert [(r["occurrence"], r["status"]) for r in rows] == [(1, "open"), (2, "open")]
    cap(1)  # acted from the second typing
    rows = routing_directions.refresh()
    assert [(r["occurrence"], r["status"]) for r in rows] == [(1, "open"), (2, "closed")]


def test_the_31_idle_days_case_works_across_windows(world):
    say(GLM, now=T0)
    scan([{"text": GLM, "at": T0}], now=T0)
    later = T0 + 31 * 86400
    say(GLM, now=later)
    assert owner_turns.turns(SESSION, now=later)[-1]["occurrence"] == 2
    rows = scan([{"text": GLM, "at": later}], now=later)
    assert [(r["occurrence"], r["status"]) for r in rows] == [(1, "open"), (2, "open")]
    cap(1, now=later)
    rows = routing_directions.refresh()
    assert [(r["occurrence"], r["status"]) for r in rows] == [(1, "open"), (2, "closed")]


# ---- P1 (3): the owner sees the item, and can dismiss it ---------------------


def test_khipu_owed_rows_carry_the_routing_direction(world):
    say(GLM)
    scan([GLM])
    filters = mcp_server.owed_filters({"kind": "routing_direction"})
    rows = mcp_server._routing_direction_rows("open", filters)
    assert len(rows) == 1
    assert rows[0]["kind"] == "routing_direction" and rows[0]["owner"] == "user"
    assert rows[0]["text"] == f"Routing said here, not in Khipu: '{GLM}'"
    assert mcp_server._routing_direction_rows("open", mcp_server.owed_filters({"owner": "assistant"})) == []
    assert mcp_server._routing_direction_rows("closed", filters) == []


def test_the_pushed_session_slice_names_no_direction_words(world, monkeypatch):
    say(GLM)
    (row,) = scan([GLM])
    from khipu import activity, identity

    monkeypatch.setattr(identity, "resolve_repo_root",
                        lambda cwd: {"repo_root": "/work", "project": "proj"})
    monkeypatch.setattr(activity, "project_slice",
                        lambda **kw: {"commitments": [], "episodes": [], "topics": [], "decisions": []})
    text = recall_rule._pushed_memory_slice("/work")
    assert GLM not in text and "Routing said here" not in text
    assert f"1 routing direction not yet in Khipu: {row['id']}; khipu owed shows them" in text


# ---- P2 (4): a lost counter is marked even after the turns were pruned ------


def test_a_lost_counter_after_pruning_never_reuses_an_occurrence(world):
    say(GLM, now=T0)
    (first,) = scan([{"text": GLM, "at": T0}], now=T0)
    assert first["occurrence"] == 1 and first["join"] == "joined"
    later = T0 + 31 * 86400
    assert owner_turns.turns(SESSION, now=later) == []  # the turn records are pruned
    owner_turns.occurrence_file(SESSION).unlink()  # and the counter file is lost
    say(GLM, now=later)
    state = owner_turns.occurrence_state(SESSION)
    assert state["lost"] is True
    assert owner_turns.turns(SESSION, now=later)[-1]["occurrence"] == 2
    rows = scan([{"text": GLM, "at": later}], now=later)
    by_occurrence = {r["occurrence"]: r for r in rows}
    assert by_occurrence[1]["join"] == "unjoined" and by_occurrence[1]["status"] == "open"
    cap(1, now=later)  # the retyped prompt's own action
    rows = routing_directions.refresh()
    by_occurrence = {r["occurrence"]: r for r in rows}
    assert by_occurrence[1]["status"] == "open"  # never closed by the new typing
    assert by_occurrence[2]["status"] == "closed"


# ---- P3: exact entity matching ----------------------------------------------


def test_claude_does_not_close_on_a_claude_second_action(world):
    text = "Use Claude only one job at a time."
    say(text)
    (row,) = scan([text])
    assert row["target"]["entity"] == "Claude"
    cap(1, account="Claude second")
    routing_directions.refresh()
    assert len(routing_directions.owed_items()) == 1
    cap(1, account="Claude")
    routing_directions.refresh()
    assert routing_directions.owed_items() == []


# ---- P3: unjoined is not sticky ----------------------------------------------


def test_unjoined_clears_when_the_turn_record_arrives_later(world):
    (row,) = scan([GLM])  # capture ran before the hook recorded the turn
    assert row["join"] == "unjoined" and row["status"] == "open"
    say(GLM)
    cap(1)
    (row,) = scan([GLM])
    assert row["join"] == "joined" and row["status"] == "closed"
    assert routing_directions.owed_items() == []
