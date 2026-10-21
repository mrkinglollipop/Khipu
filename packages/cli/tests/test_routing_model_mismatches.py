"""Rule 12 model mismatches the dispatch check finds are kept locally for a week.

They ride only on ``routing.public_view`` (``khipu route --json``, owner
facing). Only disposable homes and synthetic words; every outward projection
is scanned recursively, including dictionary keys.
"""
from __future__ import annotations

import json
import os
from datetime import timedelta

import pytest

from khipu import cli, owner_files, owner_turns, routing, routing_delivery, routing_delegation, routing_mismatches
from tests import test_routing_acceptance as acceptance
from tests.test_routing_s10_advisory import payload, table

replay = acceptance.replay  # the fixture, shared
FIELD = "model_mismatches"
BRIEF_WORDS = "BriefSentinelQuokka"
SESSION = "synthetic-mismatch-session"
PROJECT = "SyntheticMismatchProject"
CWD = f"/private/synthetic-mismatch-folder/{PROJECT}"


def strings(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield str(key)
            yield from strings(inner)
    elif isinstance(value, (list, tuple)):
        for inner in value:
            yield from strings(inner)
    elif isinstance(value, str):
        yield value


def dispatch(_replay, model="opus", brief=None, **extra):
    call = payload(model, brief or f"task: fix-round\n{BRIEF_WORDS}")
    call.update(session_id=SESSION, cwd=CWD, **extra)
    out, _, _ = routing_delegation.hook_main(json.dumps(call))
    return out


@pytest.fixture
def mismatched(replay):
    replay.configure(routing_cloud_copy=True, routing_cloud_copy_rules=True,
                     routing_cloud_copy_hold_words=True, routing_child_chars=400)
    table(replay)
    return replay


def test_a_mismatch_is_recorded_with_labels_only(mismatched):
    assert "Rule 12" in json.dumps(dispatch(mismatched))
    public = routing.public_view(mismatched.view(), mismatched.now)
    row, = public[FIELD]
    assert {k: row[k] for k in ("task", "expected", "actual", "guessed", "project")} == {
        "task": "fix-round", "expected": "sonnet", "actual": "opus", "guessed": False, "project": PROJECT}
    assert row["account"] == "Claude" and row["model"] == "opus" and row["at"]
    stored = owner_turns.folder() / routing_mismatches.STORE_NAME
    assert stat_mode(stored) == 0o600
    text = stored.read_text()
    assert BRIEF_WORDS not in text and SESSION not in text and "synthetic-mismatch-folder" not in text
    assert owner_files.kind_of(stored) == "mismatches"


def stat_mode(path):
    return os.stat(path).st_mode & 0o777


def test_a_guess_is_marked_and_a_match_records_nothing(mismatched):
    dispatch(mismatched, "opus", "Please do the tests for this.")
    dispatch(mismatched, "sonnet", "task: fix-round\nfine")
    dispatch(mismatched, "sonnet", "no class words here at all")
    row, = routing.public_view(mismatched.view(), mismatched.now)[FIELD]
    assert row["task"] == "tests" and row["guessed"] is True


def test_the_claude_p_account_is_its_login_folder_label(mismatched):
    command = "CLAUDE_CONFIG_DIR=/private/synthetic-home/.claude-synthetic-second claude --model opus -p 'task: fix-round'"
    call = {"hook_event_name": "PreToolUse", "session_id": SESSION, "cwd": CWD,
            "tool_name": "Bash", "tool_input": {"command": command}}
    routing_delegation.hook_main(json.dumps(call))
    row, = routing.public_view(mismatched.view(), mismatched.now)[FIELD]
    assert row["account"] == "Claude (.claude-synthetic-second)"
    assert "synthetic-home" not in json.dumps(row)


def test_only_today_is_shown_and_the_store_keeps_seven_days(mismatched):
    base = mismatched.now
    for days in (8, 3, 0):
        routing_mismatches.record({"task": "tests", "expected": "sonnet", "actual": "opus", "guessed": False,
                                   "account": "Claude", "model": "opus", "project": None}, now=base - timedelta(days=days))
    routing_mismatches.record({"task": "build", "expected": "sonnet", "actual": "opus", "guessed": False,
                               "account": "Claude", "model": "opus", "project": None}, now=base)
    stored = json.loads((owner_turns.folder() / routing_mismatches.STORE_NAME).read_text())
    assert len(stored["mismatches"]) == 3  # the 8-day-old one was pruned on write
    today = routing.public_view(mismatched.view(), base)[FIELD]
    assert [r["task"] for r in today] == ["tests", "build"]


def test_a_damaged_store_reads_empty_and_never_breaks_the_view(mismatched):
    owner_turns._ensure_folder()
    (owner_turns.folder() / routing_mismatches.STORE_NAME).write_text("{not json")
    assert routing.public_view(mismatched.view(), mismatched.now)[FIELD] == []
    (owner_turns.folder() / routing_mismatches.STORE_NAME).write_text(json.dumps({"mismatches": "x"}))
    assert routing.public_view(mismatched.view(), mismatched.now)[FIELD] == []


def test_a_failing_writer_never_fails_the_dispatch(mismatched, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk")
    monkeypatch.setattr(owner_turns, "_write_private", boom)
    assert "Rule 12" in json.dumps(dispatch(mismatched))


def test_route_json_carries_it(mismatched, capsys):
    dispatch(mismatched)
    assert cli.main(["route", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)[FIELD]) == 1


@pytest.mark.parametrize("surface", ["push", "child", "mcp", "replication", "gateway", "doctor"])
def test_owner_local_mismatches_never_reach_other_surfaces(mismatched, monkeypatch, surface):
    dispatch(mismatched)
    for i in range(3):
        routing.add_rule(f"Synthetic ordinary rule {i} " + "x" * 900, via="app")
    view = mismatched.view()
    public = routing.public_view(view, mismatched.now)
    assert public[FIELD][0]["project"] == PROJECT
    enriched = dict(view, **{FIELD: public[FIELD]})
    monkeypatch.setattr(routing, "read_view", lambda *a, **kw: enriched)
    monkeypatch.setattr(routing, "_start_view", lambda *a, **kw: enriched)
    if surface == "push":
        output = routing.start_push(budget=2, session_id="synthetic-recipient")
        assert output and output["text"]
    elif surface == "child":
        start = routing.start_push(budget=2, moment="delegation",
                                   child_component="agent:synthetic", child_session="synthetic-recipient")
        assert routing_delivery.acknowledge("synthetic-recipient", start["routing_changes"], now=mismatched.now)
        pages = []
        for _ in range(20):
            page = routing._child_page_push_inline("synthetic-recipient", "synthetic", mismatched.now)
            if not page:
                break
            pages.append(page)
            assert routing_delivery.acknowledge("synthetic-recipient", page["routing_changes"], now=mismatched.now)
        assert start and start["text"] and pages
        output = [start, pages, routing_delivery.snapshot("synthetic-recipient", "agent:synthetic")]
    elif surface == "mcp":
        error, output = mismatched.call(session_id="synthetic-recipient")
        assert not error and output["accounts"]
    elif surface == "replication":
        events = routing._read_all_events()[0]
        events = [dict(e, **{FIELD: public[FIELD]}) for e in events]
        output = routing.replication_payload(events, bindings={})
        assert output
    elif surface == "doctor":
        output = routing.doctor_block(mismatched.now)
        assert output
    else:
        output = routing.gateway_projection(public, now=mismatched.now)
        assert output and output["accounts"] and output["rules"]
    forbidden = (FIELD, PROJECT, SESSION, BRIEF_WORDS, CWD, "synthetic-mismatch-folder")
    for leaf in strings(output):
        assert not any(secret in leaf for secret in forbidden), (surface, leaf)


def test_the_hook_output_and_log_hold_only_the_rule_12_line(mismatched, capsys):
    out = dispatch(mismatched)
    for leaf in strings(out):
        assert not any(w in leaf for w in (BRIEF_WORDS, SESSION, PROJECT, CWD)), leaf
