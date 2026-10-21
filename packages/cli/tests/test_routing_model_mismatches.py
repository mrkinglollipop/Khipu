"""Rule 12 model mismatches the dispatch check finds are kept locally for a week.

They ride only on ``routing.public_view`` (``khipu route --json``, owner
facing). Only disposable homes and synthetic words; every outward projection
is scanned recursively, including dictionary keys.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import timedelta

import pytest

from khipu import cli, owner_files, owner_turns, recall_prompt, routing, routing_delivery, routing_delegation, routing_mismatches
from khipu import wire
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


def events():
    return sorted(owner_turns.folder().glob(routing_mismatches.PREFIX + "*.json"))


FOUND = {"task": "tests", "expected": "sonnet", "actual": "opus", "guessed": False,
         "account": "Claude", "model": "opus", "project": None}
PLANTED = "/private/BriefSentinelQuokka/synthetic-session-sentinel/opus"


def test_a_mismatch_is_recorded_with_labels_only(mismatched):
    assert "Rule 12" in json.dumps(dispatch(mismatched))
    public = routing.public_view(mismatched.view(), mismatched.now)
    row, = public[FIELD]
    assert {k: row[k] for k in ("task", "expected", "actual", "guessed", "project")} == {
        "task": "fix-round", "expected": "sonnet", "actual": "opus", "guessed": False, "project": PROJECT}
    assert row["account"] == "Claude" and row["model"] == "opus" and row["at"]
    stored, = events()
    assert os.stat(stored).st_mode & 0o777 == 0o600
    text = stored.read_text()
    assert BRIEF_WORDS not in text and SESSION not in text and "synthetic-mismatch-folder" not in text
    assert owner_files.kind_of(stored) == "mismatches"


def test_a_guess_is_marked_and_a_match_records_nothing(mismatched):
    dispatch(mismatched, "opus", "Please do the tests for this.")
    dispatch(mismatched, "sonnet", "task: fix-round\nfine")
    dispatch(mismatched, "sonnet", "no class words here at all")
    row, = routing.public_view(mismatched.view(), mismatched.now)[FIELD]
    assert row["task"] == "tests" and row["guessed"] is True


def shell(command):
    return {"hook_event_name": "PreToolUse", "session_id": SESSION, "cwd": CWD,
            "tool_name": "Bash", "tool_input": {"command": command}}


def test_the_account_is_khipus_own_label_never_a_folder_name(mismatched):
    home = "/private/synthetic-home/.claude-listed-second"
    mismatched.configure(usage_accounts=mismatched.config["usage_accounts"]
                         + [{"label": "Claude second", "harness": "claude", "home": home}])
    routing_delegation.hook_main(json.dumps(shell(f"CLAUDE_CONFIG_DIR={home} claude --model opus -p 'task: fix-round'")))
    unlisted = "/private/synthetic-home/synthetic-session-sentinel"
    routing_delegation.hook_main(json.dumps(shell(f"CLAUDE_CONFIG_DIR={unlisted} claude --model opus -p 'task: tests'")))
    rows = routing.public_view(mismatched.view(), mismatched.now)[FIELD]
    assert sorted(r["account"] for r in rows) == ["Claude second", routing_mismatches.UNLISTED_ACCOUNT]
    blob = json.dumps(routing.public_view(mismatched.view(), mismatched.now)[FIELD]) + "".join(e.read_text() for e in events())
    assert "synthetic-session-sentinel" not in blob and "synthetic-home" not in blob


def test_private_looking_input_is_dropped_not_truncated(mismatched):
    for model in (PLANTED, "claude-opus-4/x", "a" * 70, "opus\nnewline", "claude-3f2a9c1e-77aa-4c1d"):
        assert routing_mismatches.record(dict(FOUND, model=model, account=PLANTED, project=PLANTED), now=mismatched.now)
    for row in routing.public_view(mismatched.view(), mismatched.now)[FIELD]:
        assert row["model"] == routing_mismatches.UNLISTED_MODEL
        assert row["account"] == routing_mismatches.UNLISTED_ACCOUNT and row["project"] is None
    assert routing_mismatches.record(dict(FOUND, model="claude-opus-4-1"), now=mismatched.now + timedelta(seconds=5))
    assert routing.public_view(mismatched.view(), mismatched.now + timedelta(seconds=9))[FIELD][-1]["model"] == "claude-opus-4-1"
    assert not routing_mismatches.record(dict(FOUND, task="not a class"))
    # A hand-planted file with private text is cleaned on the way out as well.
    bad = events()[0]
    bad.write_text(json.dumps(dict(json.loads(bad.read_text()), model=PLANTED, project=PLANTED, task="x" * 10)))
    shown = json.dumps(routing.public_view(mismatched.view(), mismatched.now)[FIELD])
    assert "synthetic-session-sentinel" not in shown and "BriefSentinelQuokka" not in shown


def test_only_today_is_shown_and_rows_expire_on_read_and_in_the_sweep(mismatched):
    base = mismatched.now
    for days in (9, 8, 3, 0):
        routing_mismatches.record(dict(FOUND), now=base - timedelta(days=days))
    assert len(events()) == 4
    today = routing.public_view(mismatched.view(), base)[FIELD]
    assert len(today) == 1 and len(events()) == 2  # the two past a week went on the read
    routing_mismatches.record(dict(FOUND), now=base - timedelta(days=10))
    assert len(events()) == 3
    out = owner_turns.sweep(now=base.timestamp())
    assert out["ok"] and out["mismatches"] == 1 and len(events()) == 2


def test_a_damaged_store_reads_empty_and_never_breaks_the_view(mismatched):
    folder = owner_turns._ensure_folder()
    (folder / (routing_mismatches.PREFIX + "bad.json")).write_text("{not json")
    (folder / (routing_mismatches.PREFIX + "list.json")).write_text("[1]")
    assert routing.public_view(mismatched.view(), mismatched.now)[FIELD] == []
    assert routing_mismatches.today(mismatched.now) == []


def test_a_failing_writer_never_fails_the_dispatch(mismatched, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk")
    monkeypatch.setattr(routing_mismatches, "_write_event", boom)
    assert "Rule 12" in json.dumps(dispatch(mismatched))
    assert events() == []


def test_a_slow_store_never_holds_the_advisory_back(mismatched, monkeypatch):
    monkeypatch.setattr(routing_mismatches, "_write_event", lambda *a: time.sleep(1.0))
    started = time.monotonic()
    assert "Rule 12" in json.dumps(dispatch(mismatched))
    assert time.monotonic() - started < 0.5


def test_a_fifo_in_the_folder_blocks_neither_the_write_nor_the_read(mismatched):
    folder = owner_turns._ensure_folder()
    os.mkfifo(folder / (routing_mismatches.PREFIX + "00-fifo.json"))
    result = []
    worker = threading.Thread(target=lambda: result.append((
        "Rule 12" in json.dumps(dispatch(mismatched)), routing.public_view(mismatched.view(), mismatched.now)[FIELD])),
        daemon=True)
    worker.start()
    worker.join(3)
    assert result and result[0][0] and len(result[0][1]) == 1


def test_concurrent_writers_lose_no_rows(mismatched):
    start = threading.Barrier(8)

    def one():
        start.wait()
        assert routing_mismatches.record(dict(FOUND), now=mismatched.now)

    workers = [threading.Thread(target=one) for _ in range(8)]
    [w.start() for w in workers]
    [w.join(5) for w in workers]
    assert len(events()) == 8
    assert len(routing.public_view(mismatched.view(), mismatched.now)[FIELD]) == 8


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


def test_the_executable_entry_point_prints_and_logs_no_mismatch_data(mismatched, monkeypatch, capsys):
    home = "/private/synthetic-home/synthetic-session-sentinel"
    call = shell(f"CLAUDE_CONFIG_DIR={home} claude --model {PLANTED} -p 'task: fix-round\n{BRIEF_WORDS}'")
    monkeypatch.setattr(wire, "read_stdin", lambda: json.dumps(call))
    routing_delegation.main()
    printed = capsys.readouterr().out
    assert "Rule 12" in printed
    log = recall_prompt._log_path()
    logged = log.read_text() if log.exists() else ""
    assert "delegation" in logged
    # The existing delegation log line names the session by design; nothing else
    # this feature records may be in it, and no sink holds the planted fields.
    for sink in (printed, logged):
        for secret in (BRIEF_WORDS, PROJECT, CWD, "synthetic-session-sentinel", "synthetic-home", "BriefSentinelQuokka"):
            assert secret not in sink, secret
    row, = routing.public_view(mismatched.view(), mismatched.now)[FIELD]
    assert row["model"] == routing_mismatches.UNLISTED_MODEL and row["account"] == routing_mismatches.UNLISTED_ACCOUNT
    assert not any(secret in e.read_text() for e in events() for secret in (BRIEF_WORDS, SESSION, "synthetic-home", CWD))


def test_the_hook_output_holds_only_the_rule_12_line(mismatched):
    out = dispatch(mismatched)
    for leaf in strings(out):
        assert not any(w in leaf for w in (BRIEF_WORDS, SESSION, PROJECT, CWD)), leaf
