"""Scope section 12, hermetic layer only. No signed-in harness or real hub.

Only ContractGap can xfail. Setup, parsing, TypeError, KeyError, unexpected
refusals and ordinary assertions remain failures. Each contract assertion has
one diagnostic id and compares an observed production result with the scope.
The tests contain no implementation of future routing behavior.

7a, 7b and 7c belong to tests/acceptance/routing_harness/ in slice 2b:
received Claude child context, native Codex child context/parent advisory,
and a real T3 provider switch/provider-hook doctor check.
"""
from __future__ import annotations

import copy
import io
import itertools
import json
import re
import runpy
import socket
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from khipu import agent_view, mcp_server, recall_prompt, recall_rule, routing, routing_delegation
from scripts.routing_fixture_sanitize import FixtureError, load_pace, load_replay, main, sanitize


T0 = datetime(2026, 10, 20, 14, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures"
LEGACY = FIXTURES / "routing_delivery_oct14_19.jsonl"
REPLAY = FIXTURES / "routing_acceptance_v1.json"
PACE = FIXTURES / "routing_pace_v1.json"
AUTHOR = {"kind": "conversation", "harness": "codex", "project": "synthetic-routing", "session": "s1"}
OTHER = dict(AUTHOR, harness="claude_code", session="s2")
ACCOUNTS = [{"label": "Grok", "harness": "grok"}, {"label": "Codex", "harness": "codex"},
            {"label": "Claude", "harness": "claude"}]


class ContractGap(AssertionError):
    """A measured mismatch with one approved contract, never a harness exception."""


def expect(actual, expected, reason):
    try:
        assert actual == expected, reason
    except AssertionError as exc:
        raise ContractGap(reason) from exc


def future(slice_id, reason):
    return pytest.mark.xfail(strict=True, raises=ContractGap, reason=f"slice {slice_id}: {reason}")


class Replay:
    def __init__(self, root, monkeypatch):
        self.root, self.patch, self.now = root, monkeypatch, T0
        self.request_ids = itertools.count(1)
        self.data = root / "data"
        self.data.mkdir(parents=True)
        self.config = {"usage_accounts": copy.deepcopy(ACCOUNTS), "routing_reset_check": False,
                       "routing_prompt_chars": 400, "routing_prompt_budget_ms": 1000}
        self.configure()
        self.activate()

    def activate(self):
        self.patch.setenv("HOME", str(self.root))
        self.patch.setenv("KHIPU_DATA_DIR", str(self.data))
        self.patch.setenv("KHIPU_CAPTURE_HOME", str(self.root / "capture"))
        self.patch.setattr(routing, "_now", lambda: self.now)

    def configure(self, **values):
        self.config.update(values)
        (self.data / "config.json").write_text(json.dumps(self.config))

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)

    def write(self, *rows):
        return routing._write_many(list(rows))

    def view(self):
        return routing.read_view(now=self.now)

    def fold(self, rows, *, extra=(), sources=None):
        # read_events normally assigns this local position before the pure
        # fold. Direct replay must supply it too, including for new kinds.
        positioned = [dict(row, _seq=i) for i, row in enumerate(rows)]
        return routing.fold(positioned, now=self.now, cfg=routing.settings(self.config),
                            known=routing.accounts(), extra=extra, sources=sources)

    def event(self, event_type, i=1, **fields):
        return {"type": event_type, "id": f"e{i}", "seq": i, "written_at": routing._iso(T0 + timedelta(seconds=i)),
                "written_by": {"kind": "owner"}, "via": "app", **fields}

    def call(self, name="khipu_route", **args):
        answer = mcp_server.handle_message({"jsonrpc": "2.0", "id": next(self.request_ids), "method": "tools/call",
                                           "params": {"name": name, "arguments": args}})
        assert "error" not in answer, answer
        result = answer["result"]
        return result["isError"], json.loads(result["content"][0]["text"])

    def pull(self, **args):
        error, body = self.call(**args)
        assert not error, body
        return body

    def offer(self, session="s1", *, helper_tick=True):
        if helper_tick:
            agent_view.prepare_routing_table(refresh=True)
        out = routing.prompt_push(recall_prompt.routing_marker(session), budget=1.0, now=self.now)
        at = recall_prompt._RoutingAtPrompt()
        at.session = session
        context, info = at.add_to("", dict(out, at=routing._iso(self.now)))
        return out, context, info

    def ack(self, info, delivered=True):
        delivery = {"routing": info}
        recall_prompt.finish_delivery(delivery, delivered=delivered)

    def usage(self, used=4, *, witness=None, reset=None, **fields):
        at = routing._iso(witness or self.now)
        row = {"ok": True, "observed_at": at, "probed_at": at, "last_good_at": at,
               "windows": [{"name": "weekly", "window_mins": 10080, "used_pct": used,
                            "resets_at": routing._iso(reset or self.now + timedelta(days=6))}], **fields}
        target = self.data / "state" / "usage.json"
        target.parent.mkdir(exist_ok=True)
        target.write_text(json.dumps({"version": 2, "rows": {"codex|": row}}))
        return row


@pytest.fixture
def replay(tmp_path, monkeypatch):
    for name in ("KHIPU_ISOLATED_DIR", "ROUTING_NOW_OFF", "KHIPU_SESSION_ID", "KHIPU_HARNESS",
                 "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_HOME",
                 "CLAUDE_CONFIG_DIR", "KHIPU_DATABASE_URL", "ALZY_DATABASE_URL", "GEMINI_API_KEY",
                 mcp_server.GATEWAY_ACTIVE_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "1")
    monkeypatch.setattr(mcp_server, "_GATEWAY_ACTIVE", False)
    monkeypatch.setattr(routing, "ROUTING_NOW_DISABLE_FILE", str(tmp_path / "disabled"))
    monkeypatch.setattr(routing, "kick_plan_refresh", lambda: False)
    # Accidental access is a harness error, never an expected contract gap.
    attempted = []
    def forbidden(*args, **kwargs):
        attempted.append(True)
        raise AssertionError("acceptance replay attempted a real connection or process")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    from khipu import db
    monkeypatch.setattr(db, "connect", forbidden)
    yield Replay(tmp_path / "mac-a", monkeypatch)
    assert not attempted, "a production fail-open handler swallowed a forbidden connection/process attempt"


def test_contract_gap_is_narrow_and_names_the_specific_assertion():
    with pytest.raises(ContractGap, match="sentinel contract") as error:
        expect(False, True, "sentinel contract")
    assert isinstance(error.value.__cause__, AssertionError)
    expect(True, True, "already implemented")
    with pytest.raises(KeyError):
        expect({}["missing"], True, "must never xfail")


# ---- fixture boundary ----------------------------------------------------------------


def test_every_replay_fixture_has_a_closed_schema_and_no_private_strings():
    # The older runner-adapter fixtures are a different format. This inventory
    # covers every routing replay/pace fixture, including slice 1's input.
    inventory = {p for p in FIXTURES.rglob("*") if p.is_file()
                 and (p.name.startswith(("routing_", "pace_")) or "pace" in p.stem)}
    assert inventory == {LEGACY, REPLAY, PACE}
    legacy = [json.loads(line) for line in LEGACY.read_text().splitlines()]
    sanitized = sanitize(legacy)
    assert load_replay(json.loads(REPLAY.read_text())) == sanitized
    assert load_pace(json.loads(PACE.read_text()))
    # Slice 1 predates the interchange schema. Equality to this closed
    # synthetic vocabulary prevents an arbitrary string being laundered by
    # sanitizing it before validation.
    allowed_words = {
        "Wait before launching new work.", "Use Grok as a regular builder.",
        "Regular builder for ordinary implementation, fixes, tests and documentation. Keep enough allowance "
        "for the other scheduled work. Review completed changes with another model family before integrating "
        "them. This synthetic role is intentionally long enough to exercise the identifying preview instead "
        "of the full priority line.",
    }
    for row in legacy:
        by = row["written_by"]
        assert by["project"] == "synthetic-routing"
        assert by["session"] in {"synthetic-one", "synthetic-two"}
        assert row["id"] in {"e-synthetic-open", "h-synthetic-grok", "e-synthetic-limit",
                             "e-synthetic-reopen", "e-synthetic-priority"}
        for key in ("text", "owner_request"):
            if key in row:
                assert row[key] in allowed_words
        for entry in row.get("entries", []):
            assert set(entry) == {"account", "label", "role"}
            assert entry["account"] == "grok|" and entry["label"] == "Grok"
            assert entry["role"] in allowed_words
    # Validate all remaining leaf values against a canonical synthetic
    # fixture; unknown keys, path-bearing enum values and hidden text fail.
    assert legacy == legacy_fixture()


def legacy_fixture():
    """Construct the slice-1 vocabulary without reading another file."""
    by1 = {"kind": "conversation", "harness": "codex", "project": "synthetic-routing", "session": "synthetic-one"}
    by2 = dict(by1, harness="claude_code", session="synthetic-two")
    def row(kind, identifier, stamp, by, **fields):
        return {"type": kind, "id": identifier, "written_at": stamp, "written_by": by, "via": "mcp", **fields}
    return [
        row("evidence", "e-synthetic-open", "2026-10-14T12:00:00Z", by1,
            account="grok|", state="open", source="report"),
        row("hold", "h-synthetic-grok", "2026-10-14T13:00:00Z", by1,
            scope={"kind": "account", "account": "grok|"}, reason="keep", text="Wait before launching new work.",
            until="2026-10-21T12:00:00Z", owner=False),
        row("evidence", "e-synthetic-limit", "2026-10-16T12:00:00Z", by2,
            account="codex|", state="limited", source="report", until="2026-10-17T12:00:00Z"),
        row("evidence", "e-synthetic-reopen", "2026-10-19T12:00:00Z", by2,
            account="codex|", state="open", source="report"),
        row("priority_set", "e-synthetic-priority", "2026-10-19T13:00:00Z", by2, pending=True,
            entries=[{"account": "grok|", "label": "Grok", "role":
                      "Regular builder for ordinary implementation, fixes, tests and documentation. Keep enough "
                      "allowance for the other scheduled work. Review completed changes with another model family "
                      "before integrating them. This synthetic role is intentionally long enough to exercise "
                      "the identifying preview instead of the full priority line."}],
            owner_request="Use Grok as a regular builder."),
    ]


@pytest.mark.parametrize("where", ["text", "owner_request", "id", "account", "written_at", "until",
                                  "author", "entry", "scope", "unknown", "enum"])
@pytest.mark.parametrize("sentinel", ["/Users/private-user/secret", "private-user", "C:\\Users\\secret",
                                     "019c0000-1234-7123-8123-123456789abc", "unrecorded conversation prose"])
def test_fixture_loader_rejects_private_values_at_every_string_boundary(where, sentinel):
    data = sanitize(legacy_fixture())
    row = data["events"][1]
    if where == "author":
        row["written_by"]["session"] = sentinel
    elif where == "entry":
        data["events"][-1]["entries"][0]["role"] = sentinel
    elif where == "scope":
        row["scope"]["account"] = sentinel
    elif where == "enum":
        row["reason"] = sentinel
    else:
        row[where] = sentinel
    with pytest.raises(FixtureError, match=r"^fixture_(schema|identifier|text|number|enum)$") as error:
        load_replay(data)
    assert sentinel not in str(error.value)


def test_sanitizer_is_deterministic_preserves_lengths_relations_and_clock_intervals(capsys, monkeypatch):
    raw = legacy_fixture()
    raw[0]["written_by"]["session"] = "private-session-sentinel"
    raw[1]["text"] = "/Users/private-user/PRIVATE-CONVERSATION-SENTINEL"
    clean = sanitize(raw)
    assert clean == sanitize(raw)
    assert clean["events"][1]["text"] == "x" * len(raw[1]["text"])
    assert clean["events"][1]["scope"]["account"] == clean["events"][0]["account"]
    assert clean["events"][1]["written_at"] - clean["events"][0]["written_at"] == 3600
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(map(json.dumps, raw))))
    assert main() == 0
    output = capsys.readouterr()
    for sentinel in ("private-user", "PRIVATE-CONVERSATION-SENTINEL", "private-session-sentinel", "/Users/"):
        assert sentinel not in output.out + output.err
    assert json.loads(output.out) == clean


def test_sanitizer_accepts_current_production_writers_and_closes_new_fields(replay, monkeypatch, capsys):
    routing.report(account="Codex", state="limited", author=AUTHOR)
    routing.add_rule("SYNTHETIC-RULE-PRIVATE", via="app")
    routing.add_rule("SYNTHETIC-OTHER-RULE", source="/Users/SYNTHETIC-SOURCE", via="app")
    routing.set_priority([{"account": "Grok", "role": "SYNTHETIC-ROLE"},
                          {"account": "Codex", "role": "SYNTHETIC-TIMED-ROLE",
                           "until": "2026-10-20T16:00:00Z"}], via="app",
                         expect=routing.priority_version())
    routing.set_note("/Users/SYNTHETIC-NOTE", via="app")
    raw = [json.loads(line) for line in routing.events_file().read_text().splitlines()]
    assert [r["type"] for r in raw] == ["evidence", "rule_add", "rule_add", "priority_set", "note_set"]
    assert {"seq", "basis", "until_default"} <= raw[0].keys()
    assert {"date", "seeded", "source"} <= raw[1].keys()
    assert raw[1]["source"] == "added by you" and raw[3]["until"] is None
    clean = sanitize(raw)
    assert load_replay(clean) == clean
    assert clean["events"][3]["until"] is None
    assert clean["events"][3]["entries"][0]["until"] is None
    assert clean["events"][3]["entries"][1]["until"] == 7200
    assert clean["events"][0]["until"] - clean["events"][0]["written_at"] == 1800
    assert clean["events"][1]["date"] == -14 * 3600
    assert clean["events"][2]["source"] == "x" * len(raw[2]["source"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(map(json.dumps, raw))))
    assert main() == 0
    captured = capsys.readouterr()
    assert "SYNTHETIC" not in captured.out + captured.err and "/Users/" not in captured.out + captured.err
    assert json.loads(captured.out) == clean
    # Newly admitted fields are typed or scrubbed, never an extension bag.
    for key, bad in (("seq", "PRIVATE"), ("basis", "PRIVATE"), ("until_default", "PRIVATE"),
                     ("date", "PRIVATE"), ("seeded", "PRIVATE"), ("unknown", "PRIVATE")):
        damaged = copy.deepcopy(raw)
        damaged[1][key] = bad
        monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(map(json.dumps, damaged))))
        assert main() == 2
        rejected = capsys.readouterr()
        assert rejected.out == "" and rejected.err == "fixture_rejected\n"


@pytest.mark.parametrize("raw", ['{"PRIVATE-PATH-SENTINEL":1}', '{"text":"PRIVATE-TEXT-SENTINEL"}', 'bad-json'])
def test_sanitizer_errors_never_echo_the_input(raw, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    assert main() == 2
    output = capsys.readouterr()
    assert output.out == "" and output.err == "fixture_rejected\n"


@pytest.mark.parametrize("key,value", [("path", "/Users/private-user"), ("body", "secret prose"),
                                      ("session", "real-session"), ("t", True), ("used", float("nan")),
                                      ("t_err", -1), ("used", 101), ("acct", "private-user"),
                                      ("win", "weekly"), ("reset", "2026-10-20")])
def test_pace_fixture_v1_is_closed_now(key, value):
    row = {"acct": "a1", "win": "w1", "t": 0, "t_err": 0, "used": 4, "reset": 86400}
    row[key] = value
    with pytest.raises(FixtureError):
        load_pace([row])


# ---- 1, 3, 5: slice-1 regressions remain green -----------------------------------------


def seed(replay):
    clean = load_replay(json.loads(REPLAY.read_text()))
    # Decode only the closed interchange format. Aliases are fixture-local;
    # no account/login/session value is recovered from a private source.
    origin = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
    account_keys = {"a1": "grok|", "a2": "codex|"}
    event_ids = {"e1": "e-synthetic-open", "e2": "h-synthetic-grok", "e3": "e-synthetic-limit",
                 "e4": "e-synthetic-reopen", "e5": "e-synthetic-priority"}
    rows = copy.deepcopy(clean["events"])
    for row in rows:
        row["id"] = event_ids[row["id"]]
        for key in ("written_at", "until"):
            if key in row:
                row[key] = routing._iso(origin + timedelta(seconds=row[key]))
        if "account" in row:
            row["account"] = account_keys[row["account"]]
        if "scope" in row and "account" in row["scope"]:
            row["scope"]["account"] = account_keys[row["scope"]["account"]]
        for entry in row.get("entries", []):
            entry["account"] = account_keys[entry["account"]]
            entry["label"] = "Grok" if entry["account"] == "grok|" else "Codex"
    path = routing.events_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(map(json.dumps, rows)) + "\n")
    return replay.view()


@pytest.mark.parametrize("surface", ["summary", "start", "prompt", "mcp"])
def test_1_hold_priority_contradiction_and_end_route_already_arrive(replay, surface):
    seed(replay)
    if surface == "summary":
        text = routing.summary()["text"]
    elif surface == "start":
        text = recall_rule._RoutingAtStart().begin().text()
    elif surface == "prompt":
        routing.report(account="Codex", state="open", author=AUTHOR)
        text = replay.offer()[1]
    else:
        text = json.dumps(replay.pull())
    assert "contradicts" in text and "Grok" in text and "end_hold" in text


def test_1_owner_write_without_a_hold_choice_is_refused(replay):
    seed(replay)
    refused = None
    try:
        routing.set_priority([{"account": "Grok", "role": "builder"}], via="app",
                             expect=routing.priority_version())
    except routing.RouteError as error:
        refused = str(error)
    expect(bool(refused and "h-synthetic-grok" in refused and "keep" in refused and "end" in refused),
           True, "1: owner priority accepted without end-or-keep for h-synthetic-grok")


def test_1_explicit_end_choice_removes_the_hold(replay):
    rows = [json.loads(line) for line in LEGACY.read_text().splitlines()]
    rows.append(replay.event("priority_set", 9, entries=[{"account": "grok|", "role": "builder"}],
                             holds={"h-synthetic-grok": "end"}))
    view = replay.fold(rows)
    expect(view["holds"], [], "1: fold ignores explicit holds end choice")
    replay.write(*rows)
    assert replay.pull()["holds"] == []
    assert "contradicts active hold" not in routing.summary()["text"]
    assert "contradicts active hold" not in replay.offer()[1]


def test_1_explicit_keep_choice_preserves_the_hold(replay):
    seed(replay)
    # The public writer seam is owned by slice 4; the scope names holds.
    import inspect
    expect("holds" in inspect.signature(routing.set_priority).parameters, True,
           "1: owner priority writer has no explicit holds choice")
    routing.set_priority([{"account": "Grok", "role": "builder"}], via="app",
                         expect=routing.priority_version(), holds={"h-synthetic-grok": "keep"})
    assert replay.view()["holds"][0]["id"] == "h-synthetic-grok"


def test_3_eight_short_changes_fit_two_pushes_and_only_shown_entities_ack(replay):
    replay.configure(usage_accounts=ACCOUNTS + [{"label": f"A{i}", "harness": "codex", "home": str(replay.root / f"login-{i}")}
                                              for i in range(8)], routing_line_chars=55)
    routing.report(account="Grok", state="open", author=AUTHOR)
    before = replay.view()
    assert recall_prompt.mark_routing_full("s1", version=before["version"], epoch=before["epoch"], at=routing._iso(T0))
    for i in range(8):
        routing.report(account=f"A{i}", state="open", author=AUTHOR)
    # This direct renderer call starts with the complete inventory a helper prepares.
    from khipu import agent_view
    agent_view.prepare_routing_table(refresh=True)
    seen = []
    for _ in range(2):
        before_delivery = copy.deepcopy(recall_prompt.routing_marker("s1"))
        out, context, info = replay.offer()
        assert context and info["pending"]
        assert len(out["text"].split("\n", 1)[1]) <= 400
        replay.ack(info, delivered=False)
        assert recall_prompt.routing_marker("s1") == before_delivery
        replay.ack(info)
        seen.extend(out["shown_entities"])
    assert len(seen) == len(set(seen)) == 8
    assert not recall_prompt.routing_marker("s1").get("owed")


def test_5_codex_low_is_marked_against_96_percent_without_rewriting_owner_words(replay):
    replay.usage()
    routing.add_rule("Codex is low", via="app")
    text = routing.summary()["text"]
    assert "stale level claim; Codex: Khipu read 96% left" in text
    assert replay.view()["rules"][0]["text"] == "Codex is low"


def test_5_structured_level_claim_expires_at_reset(replay):
    reset = T0 + timedelta(hours=1)
    row = replay.event("priority_set", entries=[{"account": "codex|", "role": "builder",
                                               "level": {"claim": "low", "as_of": routing._iso(T0),
                                                         "until": routing._iso(reset)}}])
    before = replay.fold([row])["priority"]["entries"][0].get("level", {})
    expect(before.get("claim"), "low", "5: structured level claim is discarded by the fold")
    replay.now = reset + timedelta(seconds=1)
    level = replay.fold([row])["priority"]["entries"][0].get("level")
    expect(level, None, "5: structured level remains live after reset")


# ---- 2, 3: pages, revisions, leases -----------------------------------------------------


def test_2_pending_rule_is_whole_across_pages(replay):
    words = "Grok " + "~" * 2995
    routing.request_rule(words, author=AUTHOR)
    delivered = []
    for _ in range(12):
        out, context, info = replay.offer()
        if not context:
            break
        delivered.append(context)
        replay.ack(info)
    # Match the complete synthetic payload, not its identifying preview.
    expect("".join(delivered).count("~"), 2995, "2: pending rule was acknowledged as a preview, with text lost")


def test_2_oversized_session_start_has_a_first_page(replay):
    routing.add_rule("x" * 10500, via="app")
    at = recall_rule._RoutingAtStart().begin()
    text = at.text()
    assert text, "start entry point unexpectedly returned no output"
    expect("x" * 100 in text and len(text) <= 10000, True,
           "2: oversized start sends a pointer rather than a bounded first page")
    at.delivered("s1")
    assert recall_prompt.routing_marker("s1").get("owed")


def test_2_edit_mid_pagination_restarts_revision(replay):
    routing.request_rule("Grok " + "x" * 2995, author=AUTHOR)
    first, _, info = replay.offer()
    replay.ack(info)
    routing.edit_rule(1, "Grok " + "y" * 2995, via="app")
    second, context, _ = replay.offer()
    expect("changed while being shown" in context, True, "2: rule revision change has no pagination restart notice")
    assert "x" * 100 not in context
    assert second["routing_changes"]["items"][0]["page"]["n"] == 1
    assert first["routing_changes"]["items"][0]["revision"] != second["routing_changes"]["items"][0]["revision"]


def test_3_unacknowledged_hook_batch_expires_to_owed(replay):
    routing.report(account="Codex", state="open", author=AUTHOR)
    out, _, _ = replay.offer()
    replay.advance(61)
    again, _, _ = replay.offer()
    expect(bool(out.get("routing_changes", {}).get("batch")), True, "3: hook has no leased change-id batch")
    assert again["routing_changes"]["items"] == out["routing_changes"]["items"]
    assert again["routing_changes"]["batch"] != out["routing_changes"]["batch"]


# ---- 4: local proposal precedence, plus the Mac text-switch contract --------------------


def proposals(replay):
    return [replay.event("rule_add", 1, n=1, text="owner", revision="r1"),
            replay.event("rule_edit", 2, n=1, text="first", pending=True, base="r1", written_by=AUTHOR),
            replay.event("rule_edit", 3, n=1, text="second", pending=True, base="r1", written_by=OTHER)]


def test_4_two_conversations_take_turns_without_refusal(replay, monkeypatch):
    monkeypatch.setenv("KHIPU_SESSION_ID", "s-one")
    routing.add_rule("owner", via="app")
    shown = routing.conversation_view()
    routing.request_rule("first", n=1, author=AUTHOR, base=next(r["owner_revision"] for r in shown["rules"] if r["n"] == 1))
    refused = None
    try:
        shown = routing.conversation_view()
        routing.request_rule("second", n=1, author=OTHER, base=next(r["owner_revision"] for r in shown["rules"] if r["n"] == 1))
    except routing.RouteError as error:
        refused = str(error)
    expect(refused, None, "4: the second conversation is refused by the pending-author lock")
    assert replay.view()["rules"][0]["text"] == "second"


def test_4_priority_conversations_take_turns_without_refusal(replay, monkeypatch):
    monkeypatch.setenv("KHIPU_SESSION_ID", "s-one")
    routing.set_priority([{"account": "Codex", "role": "owner"}], via="app", expect=routing.priority_version())
    shown = routing.conversation_view()
    routing.request_priority([{"account": "Grok", "role": "first"}], owner_request="synthetic request",
                             author=AUTHOR, expect=routing.priority_version(), base=shown["priority_owner_revision"])
    refused = None
    try:
        shown = routing.conversation_view()
        routing.request_priority([{"account": "Codex", "role": "second"}], owner_request="synthetic request",
                                 author=OTHER, expect=routing.priority_version(), base=shown["priority_owner_revision"])
    except routing.RouteError as error:
        refused = str(error)
    expect(refused, None, "4: a pending priority refuses another conversation's replacement")
    assert replay.view()["priority"]["entries"][0]["role"] == "second"


def test_4_first_author_is_told_the_proposal_was_replaced(replay):
    view = replay.fold(proposals(replay))
    assert view["rules"][0]["text"] == "second"
    expect(view.get("proposals", {}).get("e2", {}).get("status"), "replaced",
           "4: replaced proposal has no terminal status or author notification")
    assert view["proposals"]["e2"]["notify"] == AUTHOR


def test_4_delayed_retry_cannot_override_new_owner_revision(replay):
    rows = proposals(replay)[:2]
    rows += [replay.event("rule_edit", 3, n=1, text="new owner", revision="r2"),
             replay.event("rule_edit", 4, n=1, text="delayed", pending=True, base="r1", written_by=OTHER)]
    view = replay.fold(rows)
    expect(view["rules"][0]["text"], "new owner", "4: delayed old-base proposal overrides the newer owner revision")
    assert view["proposals"]["e4"]["status"] == "crossed"


@pytest.mark.parametrize("ending", ["expiry", "undo", "dismiss"])
def test_4_overlay_end_never_revives_replaced_proposal(replay, ending):
    rows = proposals(replay)
    if ending == "expiry":
        replay.advance(8 * 86400)
    else:
        rows.append(replay.event("rule_undo" if ending == "undo" else "rule_dismiss", 4, n=1, target="e3"))
    view = replay.fold(rows)
    expect([r["text"] for r in view["rules"]], ["owner"], f"4: {ending} does not restore the underlying owner rule")


def test_4_a_dispatched_jobs_owner_act_is_already_refused(replay, monkeypatch):
    from khipu import owner_direction, owner_turns
    brief = "Pause Codex after completing the synthetic fixture job."
    owner_turns.write_settings({"enabled": True, "trusted": ["claude_code:cli"]})
    owner_turns.note_brief(brief, parent="s1", harness="claude_code")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s2")
    owner_turns.record({"session_id": "s2", "prompt": brief, "cwd": str(replay.root)},
                       {"CLAUDE_CODE_SESSION_ID": "s2", "CLAUDE_CODE_ENTRYPOINT": "cli"}, now=T0.timestamp())
    with pytest.raises(owner_direction.DirectionRefused) as error:
        owner_direction.check("route.pause", env={"CLAUDE_CODE_SESSION_ID": "s2"}, now=T0.timestamp())
    assert error.value.code == "no_recorded_turn"


@future("11b", "with proposal text off Mac B shows a placeholder, then confirmed owner words")
def test_4_remote_text_off_placeholder_converges_at_confirmation(replay):
    replay.configure(routing_proposal_text_between_macs=False)
    rows = proposals(replay)[:2]
    rows[1].update(mac="m1", remote=True)
    view = replay.fold(rows)
    expect("pending rule proposal" in view["rules"][0]["text"], True,
           "4: remote unconfirmed words are exposed instead of the text-off placeholder")
    rows.append(replay.event("rule_confirm", 3, n=1, text="first", target="e2"))
    assert replay.fold(rows)["rules"][0]["text"] == "first"


# ---- 6: actual MCP dispatch, identity and component protocol ----------------------------


def test_6_local_routing_errors_are_already_scrubbed(replay):
    error, body = replay.call(account=str(replay.root / "private-login"))
    assert error and "doesn't know the account" in body["error"]
    assert str(replay.root) not in json.dumps(body)


def test_6_no_identity_consumes_nothing(replay):
    routing.add_rule("synthetic owner rule", via="app")
    before = routing.events_file().read_bytes()
    first, second = replay.pull(), replay.pull()
    expect(first.get("delivery"), "untracked: pass session_id", "6: unidentified MCP result has no untracked declaration")
    assert first["routing_changes"]["items"] == second["routing_changes"]["items"]
    assert routing.events_file().read_bytes() == before
    assert recall_prompt.routing_marker("s1") == {}


@pytest.mark.parametrize("protocol", ["dropped-response", "duplicate-ack", "foreign-component", "foreign-identity",
                                     "same-component", "cross-component", "provider-switch", "error-result"])
def test_6_pull_protocol(replay, monkeypatch, protocol):
    monkeypatch.setenv("CODEX_THREAD_ID", "s1")
    if protocol == "provider-switch":
        monkeypatch.setenv("KHIPU_SESSION_ID", "t3:synthetic-thread")
    routing.add_rule("synthetic owner rule", via="app")
    if protocol == "provider-switch":
        # Leave two hook leases unacknowledged under the original hooked
        # attachment, before the pull-only provider takes over this root.
        for _ in range(2):
            replay.offer(session="t3:synthetic-thread")
            replay.advance(61)
    first = replay.pull()
    changes = first.get("routing_changes")
    expect(bool(isinstance(changes, dict) and changes.get("batch") and changes.get("items")), True,
           f"6/{protocol}: MCP result has no component-owned change batch")
    batch = changes["batch"]
    assert changes["component"] == "mcp"
    if protocol == "dropped-response":
        assert replay.pull()["routing_changes"] == changes
    elif protocol == "duplicate-ack":
        once = replay.pull(ack=batch)
        assert replay.pull(ack=batch)["delivery"] == once["delivery"]
    elif protocol in {"foreign-component", "foreign-identity"}:
        if protocol == "foreign-identity":
            monkeypatch.setenv("CODEX_THREAD_ID", "s2")
        else:
            changes = replay.offer()[0]["routing_changes"]
            batch = changes["batch"]
        error, body = replay.call(ack=batch)
        assert error and body["code"] == "ack_not_yours"
        monkeypatch.setenv("CODEX_THREAD_ID", "s1")
        assert replay.pull()["routing_changes"]["items"]
    elif protocol == "same-component":
        # Both reads reserve under the same production component lock.
        assert replay.pull()["routing_changes"]["batch"] == batch
    elif protocol == "cross-component":
        main_batch = replay.offer()[0]["routing_changes"]
        assert main_batch["component"] == "main"
        assert main_batch["batch"] != batch
        assert first["delivery"]["duplicates_across_components"]
    elif protocol == "provider-switch":
        assert first["delivery"]["health"] == "red"
        monkeypatch.delenv("CODEX_THREAD_ID")
        monkeypatch.setenv("KHIPU_HARNESS", "grok_build")
        assert replay.pull(ack=batch)["delivery"]["health"] == "green"
    else:
        error, body = replay.call(account=str(replay.root / "private-login"))
        assert error and body["routing_changes"]["items"]
        assert str(replay.root) not in json.dumps(body)


# ---- 8: provider facts and ambiguity ---------------------------------------------------


@pytest.mark.parametrize("providers,lane,expected,candidates", [
    ([{"id": "glm", "family": "Zhipu", "accounts": ["glm:main"]}], "glm", "glm:main", []),
    ([{"id": "deepseek", "family": "DeepSeek", "accounts": ["deepseek:main"]}], "deepseek", "deepseek:main", []),
    ([{"id": "glm", "family": "Zhipu", "accounts": ["glm:one", "glm:two"]}], "glm", None, ["glm:one", "glm:two"]),
    ([{"id": "glm", "family": "Zhipu", "accounts": ["glm:main"], "lane_names": ["shared"]},
      {"id": "deepseek", "family": "DeepSeek", "accounts": ["deepseek:main"], "lane_names": ["shared"]}],
     "shared", None, ["glm:main", "deepseek:main"]),
], ids=["glm", "deepseek", "two-accounts", "name-claimed-twice"])
def test_8_provider_catalog_matches_by_name(replay, providers, lane, expected, candidates):
    rows = [replay.event("provider_set", i + 1, **p) for i, p in enumerate(providers)]
    rows.append(replay.event("evidence", 10, runner="synthetic-runner", lane=lane, state="limited", source="runner"))
    view = replay.fold(rows)
    matched = next((a for a in view["accounts"] if a.get("key") == expected), None) if expected else None
    if expected:
        expect(bool(matched), True, f"8/{lane}: provider fact did not create a matched account")
        assert matched["matched_by"] == "name"
    else:
        assert len(view["unmatched"]) == 1
        expect(sorted(view["unmatched"][0].get("candidates", [])), sorted(candidates),
               "8: ambiguous provider name has no candidate accounts")


# ---- 9: fake HTTP adapter boundary, no key or response body copied to an artifact --------


def test_9_http_quota_redirect_and_key_channels(replay, monkeypatch, capsys):
    from khipu import keychain, usage
    sentinel = "SYNTHETIC-QUOTA-KEY-SENTINEL"
    calls = []
    monkeypatch.setattr(keychain, "get_password", lambda *a, **k: sentinel)
    probed = []
    original_probe = usage.probe_account
    def fake_probe(account, **kwargs):
        if account["harness"] not in {"claude", "codex", "grok"}:
            return original_probe(account, **kwargs)
        probed.append(account["harness"])
        return {**account, "ok": False, "error": "no_limits", "windows": []}
    monkeypatch.setattr(usage, "probe_account", fake_probe)
    # The fake transport is installed at the standard library boundary;
    # production must explicitly disable redirects and stay on this host.
    import urllib.request
    class Redirect:
        status, code, headers = 302, 302, {"Location": "https://unconfirmed.invalid/", "secret": sentinel}
        def read(self, *args):
            return sentinel.encode()
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
    def request(self, req, *args, **kwargs):  # noqa: ARG001 - OpenerDirector.open stand-in
        calls.append(req)
        return Redirect()
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", request)
    replay.write(replay.event("provider_set", id="synthetic-provider", label="Synthetic", family="Synthetic",
                              login="none", probe="http-quota", host="quota.invalid", accounts=["synthetic:main"]))
    # The existing refresh entry point must dispatch the configured adapter.
    usage.refresh_due()
    assert sorted(probed) == ["claude", "codex", "grok"]
    expect(bool(calls), True, "9: http-quota provider fact is ignored by the refresh adapter")
    assert len(calls) == 1 and calls[0].full_url.startswith("https://quota.invalid/")
    output = capsys.readouterr()
    channels = [output.out, output.err, json.dumps(replay.pull())]
    channels += [p.read_text(errors="replace") for p in replay.root.rglob("*") if p.is_file()]
    assert all(sentinel not in channel for channel in channels)
    assert "redirect_refused" in json.dumps(replay.view())


# ---- 10: in-process advisory hook, never a permission decision ---------------------------


@pytest.mark.parametrize("model,expected", [
    pytest.param("opus", True, marks=future("10", "Opus fix-round needs the advisory line with the result")),
    ("sonnet", False),
])
def test_10_fix_round_advisory_never_denies(replay, model, expected):
    routing.add_rule("Rule 12: fix rounds go to Sonnet.", via="app")
    payload = {"hook_event_name": "PreToolUse", "session_id": "s1", "cwd": str(replay.root),
               "tool_name": "Agent", "tool_input": {"model": model, "prompt": "task: fix-round\nRepair the fixture."}}
    out, _, _ = routing_delegation.hook_main(json.dumps(payload))
    assert "permissionDecision" not in json.dumps(out)
    context = out.get("hookSpecificOutput", {}).get("additionalContext", "")
    # Slice 10 owns advisory wording. Compare the additional context with
    # the same hook without a task class, so the standing rule cannot pass.
    control = copy.deepcopy(payload)
    control["tool_input"]["prompt"] = "Repair the fixture."
    ordinary, _, _ = routing_delegation.hook_main(json.dumps(control))
    baseline = ordinary.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "Rule 12" in baseline
    if expected:
        advisory = context.replace(baseline, "")
        expect(context != baseline and "Sonnet" in advisory and "12" in advisory, True,
               "10: Opus fix-round has no rule-12 dispatch advisory")
    else:
        assert context == baseline


# ---- 11a: migration and aliases --------------------------------------------------------


def test_11a_account_id_is_stable_across_installations(replay, tmp_path):
    first = routing.account_id("codex|")
    b = Replay(tmp_path / "mac-b", replay.patch)
    second = routing.account_id("codex|")
    expect((first, second), ("codex:main", "codex:main"), "11a: account ids are per-install salted rather than stable")
    assert b.root != replay.root


@pytest.mark.parametrize("age,restored", [(89, False), (91, False), (91, True)], ids=["alias-live", "alias-expired", "restore-keeps-start"])
def test_11a_alias_window_and_restored_migration_start(replay, age, restored):
    old = routing._legacy_account_id("codex|")
    migration = replay.event("account_migration", aliases={old: "codex:main"}, started_at=routing._iso(T0))
    replay.write(migration)
    if restored:
        # Copy a log, not a view/salt: a reinstall must not restart its age.
        path = routing.events_file()
        raw = path.read_bytes()
        b = Replay(replay.root.parent / "restored", replay.patch)
        routing.events_file().parent.mkdir(parents=True, exist_ok=True)
        routing.events_file().write_bytes(raw)
        replay = b
    replay.advance(age * 86400)
    result, refused = None, None
    try:
        result = routing.resolve_account(old)
    except routing.RouteError as error:
        refused = str(error)
    if age < 90:
        expect(result, "codex:main", "11a: old alias did not resolve to the stable id within 90 days")
    else:
        expect(bool(refused and "expired on" in refused and "codex:main" in refused), True,
               "11a: expired alias was accepted or lacked its explicit date and replacement")


def test_11a_rename_keeps_a_forward(replay):
    view = replay.fold([replay.event("account_rename", account="codex:main", to="codex:second")])
    expect(view["forwards"].get("codex:main"), "codex:second", "11a: stable account rename creates no forward")


def test_11a_duplicate_label_is_refused(replay):
    from khipu import accounts, config, usage

    original = config.load_config()
    events = routing.read_events()[0]
    changed = ACCOUNTS + [{"label": "Codex", "harness": "codex",
                           "home": str(replay.root / "synthetic-other")}]
    with pytest.raises(usage.AccountsError) as error:
        accounts.save_list(changed, expect=config.accounts_version(), via="app")
    assert error.value.code == "name_taken"
    assert config.load_config() == original
    assert routing.read_events()[0] == events


def test_11a_old_local_log_folds_unchanged_and_is_never_rewritten(replay):
    seed(replay)
    path = routing.events_file()
    raw = path.read_bytes()
    one = replay.view()
    unknown = {"state": "unknown", "until": None, "basis": None, "scope": "all", "reason": None,
               "models": [], "disagreement": None, "reset_check": None}
    assert one["effective"] == {
        "account:claude|": dict(unknown, label="Claude", last_seen=None),
        "account:codex|": dict(unknown, label="Codex", last_seen="open"),
        "account:grok|": dict(unknown, label="Grok", last_seen="open"),
        "hold:h-synthetic-grok": {"scope": "Grok", "reason": "keep", "until": "2026-10-21T12:00:00Z",
                                  "owner": False, "text": None},
        "priority": {"entries": [["Grok", "x" * 324, None, True]], "until": None, "pending": True,
                     "by": "a Claude Code conversation", "replaced": None},
    }
    assert path.read_bytes() == raw
    assert one["holds"][0]["id"] == "h-synthetic-grok"


def test_11a_migration_is_append_only_idempotent_and_keeps_the_effective_view(replay):
    from khipu.config import load_config

    before = seed(replay)
    path = routing.events_file()
    original = path.read_bytes()
    old = routing._legacy_account_id("codex|")
    after = replay.view()  # A Mac which issued salted ids migrates on read.
    assert path.read_bytes().startswith(original)
    assert before["effective"] == after["effective"]
    migrations = [e for e in routing.read_events()[0] if e["type"] == "account_migration"]
    assert len(migrations) == 1 and migrations[0]["aliases"][old] == "codex:main"
    assert migrations[0]["started_at"] == routing._iso(T0)
    assert load_config()[routing.account_identity.BINDINGS]["codex:main"] == {"harness": "codex", "home": None}
    migrated = path.read_bytes()
    replay.advance(91 * 86400)
    routing.migrate_accounts()
    assert path.read_bytes() == migrated
    with pytest.raises(routing.RouteError, match="expired on 2027-01-18; use codex:main"):
        routing.resolve_account(old)


def test_11a_bindings_are_local_and_same_label_is_one_identity(replay, tmp_path):
    from khipu.config import load_config

    replay.configure(usage_accounts=[{"harness": "claude", "label": "Claude second",
                                      "home": str(replay.root / "login")}])
    routing.migrate_accounts()
    first = routing.account_id("claude|" + str(replay.root / "login"))
    first_binding = load_config()[routing.account_identity.BINDINGS]
    assert not next(e for e in routing.read_events()[0] if e["type"] == "account_migration")["aliases"]
    b = Replay(tmp_path / "mac-b", replay.patch)
    b.configure(usage_accounts=[{"harness": "claude", "label": "Claude second", "home": str(b.root / "login")}])
    routing.migrate_accounts()
    assert routing.account_id("claude|" + str(b.root / "login")) == first == "claude:second"
    assert load_config()[routing.account_identity.BINDINGS] != first_binding
    assert not (routing.routing_dir() / "id-salt").exists()


def test_11a_alias_expires_at_the_boundary_and_never_rebinds_on_reinstall(replay, tmp_path):
    old = routing._legacy_account_id("codex|")
    routing.migrate_accounts()
    replay.advance(90 * 86400 - 1)
    assert routing.resolve_account(old) == "codex:main"
    replay.advance(1)
    for lookup in (routing.resolve_account, routing._resolve_reported,
                   lambda name: routing.conversation_view(account=name)):
        with pytest.raises(routing.RouteError, match="expired on 2027-01-18; use codex:main"):
            lookup(old)
    Replay(tmp_path / "reinstalled", replay.patch)
    with pytest.raises(routing.RouteError, match="doesn't know that old account id"):
        routing.resolve_account(old)
    assert not routing.events_file().exists()


def test_11a_owner_rename_forwards_ids_aliases_holds_and_history(replay):
    from khipu import accounts, config

    old = routing._legacy_account_id("codex|")
    routing.migrate_accounts()
    routing.report(account="Codex", state="limited", until="2h", author=AUTHOR)
    routing.add_hold("synthetic hold", account="Codex", scope="account", reason="keep", author=AUTHOR)
    accounts.rename("codex|", "Codex second", expect=config.accounts_version(), via="app")
    assert routing.account_id("codex|") == routing.resolve_account("codex:main") == "codex:second"
    assert routing.resolve_account(old) == "codex:second"
    assert replay.view()["forwards"]["codex:main"] == "codex:second"
    assert replay.view()["holds"][0]["scope"]["account"] == "codex|"
    assert next(a for a in replay.view()["accounts"] if a["key"] == "codex|")["state"] == "limited"
    assert set(config.load_config()[routing.account_identity.BINDINGS]) == {"claude:main", "grok:main", "codex:second"}
    replay.advance(91 * 86400)
    with pytest.raises(routing.RouteError, match="use codex:second"):
        routing.resolve_account(old)
    # Expiry affects lookup only: historical alias entries still fold.
    row = replay.event("evidence", account=old, state="limited", basis="cli", source="report",
                       until=routing._iso(replay.now + timedelta(hours=1)))
    replay.write(row)
    assert next(a for a in replay.view()["accounts"] if a["key"] == "codex|")["state"] == "limited"


def test_11a_conversation_cannot_rename_or_supply_identity_events(replay):
    from khipu import accounts, config

    original = config.load_config()
    changed = copy.deepcopy(ACCOUNTS)
    changed[1]["label"] = "Codex second"
    with pytest.raises(accounts.AccountsError, match="only the owner renames"):
        accounts.save_list(changed, expect=config.accounts_version(), request={"by": AUTHOR, "request": "rename"})
    assert config.load_config() == original
    view = replay.fold([dict(replay.event("account_rename", account="codex:main", to="codex:second"),
                             written_by=AUTHOR),
                        dict(replay.event("account_migration", aliases={"acct-000000000000": "codex:main"}),
                             written_by=AUTHOR)])
    assert view["forwards"] == {}
    assert routing.account_identity.state(routing.read_events()[0], routing.accounts())[2] == {}


def test_11a_crossing_renames_forward_the_loser_and_conversations_cannot_override(replay):
    rows = [replay.event("account_rename", 1, account="codex:main", to="codex:second"),
            replay.event("account_rename", 2, account="codex:main", to="codex:third"),
            dict(replay.event("account_rename", 3, account="codex:third", to="codex:fourth"), written_by=OTHER)]
    view = replay.fold(rows)
    assert view["forwards"] == {"codex:main": "codex:third", "codex:second": "codex:third"}


def test_11a_a_forwarded_label_cannot_be_reused_for_another_login(replay):
    from khipu import accounts, config

    accounts.rename("codex|", "Codex second", expect=config.accounts_version(), via="app")
    before = config.load_config()
    changed = before["usage_accounts"] + [{"harness": "codex", "label": "Codex main",
                                          "home": str(replay.root / "another-login")}]
    with pytest.raises(accounts.AccountsError, match="earlier account id"):
        accounts.save_list(changed, expect=config.accounts_version(), via="app")
    assert config.load_config() == before
    assert routing.resolve_account("codex:main") == "codex:second"


def test_11a_restored_history_uses_the_new_local_binding_forever(replay, tmp_path):
    account = {"harness": "claude", "label": "Claude second", "home": str(replay.root / "login")}
    replay.configure(usage_accounts=[account])
    key = "claude|" + account["home"]
    old = routing._legacy_account_id(key)
    routing.migrate_accounts()
    routing.pause("Claude second", via="app")
    original = routing.events_file().read_bytes()
    b = Replay(tmp_path / "restored-bindings", replay.patch)
    b.configure(usage_accounts=[dict(account, home=str(b.root / "login"))])
    routing.events_file().parent.mkdir(parents=True, exist_ok=True)
    routing.events_file().write_bytes(original)
    routing.migrate_accounts()
    assert routing.resolve_account(old) == "claude:second"
    b.advance(91 * 86400)
    view = b.view()
    assert [(a["key"], a["state"]) for a in view["accounts"]] == [("claude|" + str(b.root / "login"), "paused")]
    assert routing.events_file().read_bytes() == original
    assert not (routing.routing_dir() / "id-salt").exists()
    with pytest.raises(routing.RouteError, match="expired on 2027-01-18; use claude:second"):
        routing.resolve_account(old)


@pytest.mark.parametrize("labels", [("Codex work!", "Codex work"), ("Codex", "Codex main")])
def test_11a_slug_collisions_are_refused_even_on_an_unchanged_stored_list(replay, labels):
    from khipu import usage

    entries = [{"harness": "codex", "label": label, "home": str(replay.root / f"login-{i}")}
               for i, label in enumerate(labels)]
    with pytest.raises(usage.AccountsError, match="duplicate account label"):
        usage.parse_usage_accounts(entries, previous=entries)


# ---- 11b: global identities, ordering, retries, tombstones -------------------------------


def test_11b_two_macs_keep_both_rules_until_hub_numbering(replay):
    rows = [replay.event("rule_add", i, n=1, text=word, mac=f"m{i}", entity=f"rule-{i}",
                         hlc={"wall": 1, "counter": 0, "mac": f"m{i}"}) for i, word in [(1, "first"), (2, "second")]]
    view = replay.fold(rows)
    expect(sorted(r["text"] for r in view["rules"]), ["first", "second"], "11b: concurrent numeric rule keys overwrite one rule")
    assert all(r["n"] is None for r in view["rules"])


def test_11b_writes_carry_global_event_and_entity_identity(replay):
    routing.add_rule("synthetic owner rule", via="app")
    events, bad = routing.read_events()
    assert bad == 0 and len(events) == 1
    row = events[0]
    expect(bool(re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", row["id"])),
           True, "11b: written event id is not a global UUIDv7")
    assert row["mac"] and row["seq"] >= 1 and row["hlc"]
    assert re.fullmatch(r"rule-[0-9a-f-]{36}", row["entity"])


def test_11b_provisional_numeric_target_is_refused(replay):
    replay.write(replay.event("rule_add", n=1, entity="rule-synthetic-1", number_pending=True, text="first"))
    refused = None
    try:
        routing.edit_rule(1, "second", via="app")
    except routing.RouteError as error:
        refused = str(error)
    expect(bool(refused and "rule-synthetic-1" in refused), True, "11b: provisional number was writable without a uuid refusal")


@pytest.mark.parametrize("different_body", [False, True], ids=["retry-first-result", "retry-body-refused"])
def test_11b_op_retry(replay, different_body):
    row = replay.event("rule_add", n=1, text="first", op="op-synthetic-1")
    first, _ = replay.write(row)
    refused = False
    second = None
    try:
        second, _ = replay.write(dict(row, text="second" if different_body else "first"))
    except routing.RouteError:
        refused = True
    if different_body:
        expect(refused, True, "11b: same op accepted a changed body")
    else:
        expect(second, first, "11b: identical op retry wrote a second event instead of returning the first")


def test_11b_hold_end_before_hold_applies_on_arrival(replay):
    end = replay.event("hold_end", 1, target="h1")
    hold = replay.event("hold", 2, id="h1", scope={"kind": "account", "account": "grok|"}, reason="keep", text="wait")
    expect(replay.fold([end, hold])["holds"], [], "11b: hold-end tombstone was dropped before its target arrived")


# ---- 12: two HOME replay, remote set delivery, observation scope -------------------------


class ReplayHub:
    """An ordered, idempotent transport fixture, never a routing implementation.

    It neither folds nor projects events. This is the test-hub seam for 11b;
    SQL adapters in test_audit1010_hubb/c likewise record boundary traffic.
    """
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE events (accepted INTEGER PRIMARY KEY, id TEXT UNIQUE, sender TEXT, body TEXT)")

    def upload(self, sender, text, *, stop_after=None):
        cursor = 0
        for line in text.splitlines():
            if stop_after is not None and cursor >= stop_after:
                break
            row = json.loads(line)
            self.db.execute("INSERT OR IGNORE INTO events (id,sender,body) VALUES (?,?,?)",
                            (row["id"], sender, json.dumps(row)))
            cursor += 1
        self.db.commit()
        return cursor

    def pull(self, receiver, cursor=0):
        rows = self.db.execute("SELECT accepted,body FROM events WHERE accepted>? AND sender!=? ORDER BY accepted",
                               (cursor, receiver)).fetchall()
        return "".join(body + "\n" for _, body in rows), rows[-1][0] if rows else cursor

    def close(self):
        self.db.close()


def test_replay_hub_preserves_event_identity_and_interrupted_upload_cursor():
    hub = ReplayHub()
    try:
        text = '\n'.join(json.dumps({"id": f"e{i}", "type": "rule_add"}) for i in (1, 2))
        assert hub.upload("m1", text, stop_after=1) == 1
        assert hub.upload("m1", text) == 2
        assert hub.upload("m1", text) == 2
        pulled, cursor = hub.pull("m2")
        assert [json.loads(row)["id"] for row in pulled.splitlines()] == ["e1", "e2"]
        assert hub.pull("m2", cursor) == ("", cursor)
        assert hub.pull("m1") == ("", 0)
    finally:
        hub.close()


@pytest.mark.parametrize("scenario", ["opposite-order", "skew", "duplicate-upload", "interrupted-sync", "restore"])
@future("11b", "the fold must consume the idempotent remote set on both Macs")
def test_12_two_home_replication_recovery(replay, scenario):
    a = replay
    b = Replay(a.root.parent / "mac-b", a.patch)
    if scenario == "skew":
        a.advance(600)
        b.advance(-600)
    a.activate()
    routing.add_rule("first", via="app")
    local_a = routing.events_file().read_text()
    b.activate()
    routing.add_rule("second", via="app")
    local_b = routing.events_file().read_text()
    # A deterministic transport inbox is not a simulated fold. Production
    # read_view must consume remote.jsonl and deduplicate event identities.
    hub = ReplayHub()
    try:
        if scenario == "interrupted-sync":
            assert hub.upload("m2", local_b, stop_after=0) == 0
        hub.upload("m2", local_b)
        hub.upload("m1", local_a)
        if scenario == "duplicate-upload":
            hub.upload("m2", local_b)
        inbox_a, _ = hub.pull("m1")
        inbox_b, _ = hub.pull("m2")
    finally:
        hub.close()
    if scenario == "restore":
        b = Replay(a.root.parent / "mac-restored", a.patch)
        routing.events_file().parent.mkdir(parents=True, exist_ok=True)
        routing.events_file().write_text(local_b)
        inbox_b = local_a
    (routing.routing_dir() / "remote.jsonl").write_text(inbox_b)
    vb = b.view()
    a.activate()
    (routing.routing_dir() / "remote.jsonl").write_text(inbox_a)
    va = a.view()
    expect(sorted(r["text"] for r in va["rules"]), ["first", "second"],
           f"12/{scenario}: Mac A ignores the remote event set")
    assert sorted(r["text"] for r in vb["rules"]) == ["first", "second"]


@pytest.mark.parametrize("proposal_text", [False, True], ids=["proposal-text-off", "proposal-text-on"])
@future("11a", "replication needs the closed payload projector, independently of local agent scrubbing")
def test_12_replication_payload_checks_every_nested_field(replay, proposal_text):
    # This named seam pins the projector contract without substituting a
    # test-written projector. Slice 11a may bind its implementation here.
    projector = getattr(routing, "replication_payload", None)
    expect(callable(projector), True, "12: the Mac-to-hub routing payload projector is not implemented")
    local = str(replay.root / "private-login")
    rows = [replay.event("rule_add", 1, n=1, text="owner " + local),
            replay.event("hold", 2, id="h1", owner=False, text="SYNTHETIC-CONVERSATION-SECRET",
                         scope={"kind": "account", "account": "codex|" + local},
                         written_by=dict(AUTHOR, project="SYNTHETIC-PROJECT", session="SYNTHETIC-SESSION")),
            replay.event("rule_edit", 3, n=1, pending=True, base="e1", text="synthetic proposal",
                         owner_request="synthetic request", written_by=AUTHOR)]
    before = copy.deepcopy(rows)
    payload = projector(rows, proposal_text=proposal_text, bindings={"codex|" + local: "codex:main"})
    assert rows == before
    assert payload[0]["text"] == "owner " + local
    assert payload[1]["scope"]["account"] == "codex:main"
    for key, value in leaves(payload):
        assert key not in {"home", "evidence", "name", "project_name", "session_name"}
        if isinstance(value, str):
            assert "|" + local not in value
            assert value not in {"SYNTHETIC-CONVERSATION-SECRET", "SYNTHETIC-PROJECT", "SYNTHETIC-SESSION"}
    text = json.dumps(payload)
    assert ("synthetic proposal" in text) is proposal_text
    assert ("synthetic request" in text) is proposal_text


@pytest.mark.parametrize("source,state", [("probe", "limited"), ("runner", "limited"), ("probe", "open")])
@future("11b", "persisted observation states converge in opposite arrival orders")
def test_12_observations_converge(replay, source, state):
    rows = [replay.event("observation", 1, account="codex|", state=state, source=source, scope="quota",
                         witness_at=routing._iso(T0), expires_at=routing._iso(T0 + timedelta(hours=2))),
            replay.event("source_seen", 2, source=source, mac="m1")]
    views = [replay.fold(order) for order in (rows, list(reversed(rows))) ]
    for view in views:
        account = next(a for a in view["accounts"] if a["key"] == "codex|")
        expect(account["state"], state, f"12/{source}/{state}: persisted observation is not folded")
    assert views[0]["effective"] == views[1]["effective"]


@future("11b", "a newer witness renews expiry and source heartbeats never do")
def test_12_renewal_source_silence_and_clock_only_expiry(replay):
    rows = [replay.event("observation", 1, account="codex|", state="open", scope="quota", source="probe",
                         witness_at=routing._iso(T0), expires_at=routing._iso(T0 + timedelta(hours=1))),
            replay.event("observation_renew", 2, target="e1", witness_at=routing._iso(T0 + timedelta(minutes=30)),
                         expires_at=routing._iso(T0 + timedelta(minutes=90)))]
    replay.advance(75 * 60)
    account = next(a for a in replay.fold(rows)["accounts"] if a["key"] == "codex|")
    expect(account["state"], "open", "12: newer witness renewal did not extend the observation")
    rows.append(replay.event("source_seen", 3, source="probe", mac="m1"))
    replay.advance(16 * 60)
    assert next(a for a in replay.fold(rows)["accounts"] if a["key"] == "codex|")["state"] == "unknown"


def test_12_cached_reading_never_renews(replay):
    replay.usage()
    replay.view()
    replay.advance(4 * 3600)
    replay.view()
    events, bad = routing.read_events()
    assert bad == 0
    observations = [e for e in events if e["type"] == "observation"]
    expect(len(observations), 1, "12: fresh probe is recomputed rather than persisted as an observation")
    assert not [e for e in events if e["type"] == "observation_renew"]


@future("11b", "machine availability is local and never closes shared account quota")
def test_12_not_installed_on_mac_b_leaves_mac_a_open(replay):
    rows = [replay.event("observation", 1, account="codex|", state="open", scope="quota", mac="m1",
                         source="probe", expires_at=routing._iso(T0 + timedelta(hours=1))),
            replay.event("observation", 2, account="codex|", state="down", scope="machine", mac="m2",
                         source="probe", reason="not_installed", expires_at=routing._iso(T0 + timedelta(hours=1)))]
    replay.configure(routing_mac="m1")
    account = next(a for a in replay.fold(rows)["accounts"] if a["key"] == "codex|")
    expect(account["state"], "open", "12: shared quota is missing or affected by another Mac's availability")


# ---- 13: gateway closed projection and identity isolation -------------------------------


@pytest.mark.parametrize("contract", ["projection", "foreign-delivery", "foreign-ack", "owner-write"])
@future("12", "gateway routing must implement a closed projection and caller-owned delivery")
def test_13_gateway_projection(replay, monkeypatch, contract):
    replay.configure(routing_cloud_copy=True, routing_cloud_copy_rules=True, routing_cloud_copy_hold_words=True)
    routing.add_rule("owner words at " + str(replay.root / "private-login"), via="app")
    routing.set_note("owner note", via="app")
    monkeypatch.setenv(mcp_server.GATEWAY_ACTIVE_ENV, "1")
    error, body = replay.call()
    assert not error
    expect(body.get("available") is not False, True, f"13/{contract}: gateway routing projection is unavailable")
    if contract == "projection":
        assert_gateway_fields(body)
        serialized = json.dumps(body)
        assert str(replay.root) not in serialized
        for secret in ("written_by", "fingerprint", "effective", "source_cache", "login", "mac", "history"):
            assert not any(key == secret for key, _ in leaves(body))
    elif contract == "foreign-delivery":
        assert "s2" not in json.dumps(body.get("delivery"))
    elif contract == "foreign-ack":
        error, body = replay.call(session_id="synthetic-client:s2", ack="synthetic-other-batch")
        assert error and body["code"] == "ack_not_yours"
    else:
        error, body = replay.call("khipu_owner_act", action="routing.pause", args={"account": "Codex"})
        assert error and not body.get("ok")


def leaves(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key, inner
            yield from leaves(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from leaves(inner)


GATEWAY_FIELDS = {
    "root": {"available", "about", "record", "version", "epoch", "accounts", "holds", "rules", "note", "priority",
             "delivery", "routing_changes", "folded_at", "valid_until"},
    "accounts": {"id", "label", "state", "basis", "until", "at", "line", "witness", "models", "pace"},
    "witness": {"source", "state", "basis", "at", "until", "window", "used_pct"},
    "models": {"model", "state", "basis", "until", "at", "line", "witness"},
    "holds": {"id", "scope", "reason", "until", "owner", "text", "line", "at"},
    "scope": {"kind", "account"},
    "rules": {"id", "n", "text", "pending", "confirmed", "revision"},
    "note": {"text", "revision", "at"},
    "priority": {"entries", "until", "pending", "revision", "line"},
    "entries": {"account", "label", "role", "until", "level"},
    "level": {"claim", "as_of", "until"},
    "delivery": {"owed", "in_flight", "acked_through"},
    "routing_changes": {"batch", "component", "items"},
    "items": {"change", "entity", "revision", "page"},
    "page": {"n", "m"},
    "pace": {"window", "cycle_resets_at", "trailing_h", "buckets", "valid_pct", "observed_pct", "gappy",
             "rate_pct_per_h", "why", "plan", "share_cap", "day_cap", "headline"},
    "plan": {"verdict", "needed_pct_per_h", "runs_out_at", "before_reset_s", "unused_at_reset_pct"},
    "share_cap": {"revision", "pct", "verdict", "needed_pct_per_h", "runs_out_at", "before_reset_s", "unused_at_reset_pct"},
    "day_cap": {"revision", "pct", "verdict", "used_24h_pct", "observed_pct", "settles_at_pct", "peak_pct", "peak_at", "reaches_cap_at"},
}


def assert_gateway_fields(value, kind="root"):
    if isinstance(value, list):
        for item in value:
            assert_gateway_fields(item, kind)
    elif isinstance(value, dict):
        assert kind in GATEWAY_FIELDS, f"unlisted gateway object {kind}"
        assert set(value) <= GATEWAY_FIELDS[kind], f"unlisted gateway field in {kind}"
        for key, inner in value.items():
            assert_gateway_fields(inner, key)
    elif isinstance(value, str):
        assert not re.search(r"(?:/Users/|/home/|/tmp/|~[/\\]|[A-Za-z]:\\|(?:^|\s)/[^\s]+)", value)


@pytest.mark.parametrize("kind", list(GATEWAY_FIELDS))
def test_gateway_closed_field_checker_rejects_extra_fields_at_every_depth(kind):
    with pytest.raises(AssertionError, match="unlisted gateway field"):
        assert_gateway_fields({"SYNTHETIC-PRIVATE-HISTORY": {"home": "secret"}}, kind)


def test_gateway_closed_field_checker_traverses_nested_history_and_lists():
    value = {"accounts": [{"label": "Codex", "witness": {"source": "probe", "history": [{"session": "s2"}]}}]}
    with pytest.raises(AssertionError, match="unlisted gateway field"):
        assert_gateway_fields(value)
    value = {"accounts": [{"label": "Codex", "witness": {"source": "/Users/synthetic-private"}}]}
    with pytest.raises(AssertionError):
        assert_gateway_fields(value)


# ---- 14: scanner gets synthetic files, never Matt's files -------------------------------


@pytest.mark.parametrize("line,flag", [
    ("Main Claude account tight until Mon 2026-10-19", True),
    ("Delegation order: second Claude account first", True),
    ("| Codex | Good for builds | Weak at narration |", False),
    ("| Codex | Good for builds | Evidence 2026-10-14 |", False),
    ("> on 2026-10-14 Matt paused Claude", False),
    ("History: on 2026-10-14 Matt paused Claude.", False),
    ("Run codex exec to reach the Codex lane.", False),
], ids=["live-level", "live-order", "capability", "evidence-date", "quote", "past-history", "lane-howto"])
@future("13", "doctor must report scanner evidence from imported routing-state copies")
def test_14_scanner_live_claims_and_false_positives(replay, line, flag):
    document = replay.root / "AGENTS.md"
    document.write_text(line + "\n")
    replay.configure(routing_scan_files=[str(document)])
    body = routing.doctor_block(now=replay.now)
    checks = body.get("routing_copies")
    expect(isinstance(checks, list), True, "14: doctor has no routing-copy scanner evidence")
    assert bool(checks) == flag
    if flag:
        assert checks[0]["line"] == 1 and checks[0]["file"] == str(document)


# ---- 15: each delivery-health red condition and evidence-based recovery -----------------


@pytest.mark.parametrize("condition", ["budget-stall", "lease-expired-twice", "ack-unshown", "prompt-skips",
                                      "cross-mac-stale", "gateway-stale", "unmatched-limit"])
@future("16", "doctor needs routing_delivery health driven by delivery evidence")
def test_15_health_red_and_recovery(replay, condition):
    routing.report(account="Codex", state="open", author=AUTHOR)
    out, _, info = replay.offer()
    if condition in {"budget-stall", "lease-expired-twice"}:
        for _ in range(5 if condition == "budget-stall" else 2):
            replay.advance(61)
            replay.offer()
    elif condition == "ack-unshown":
        assert recall_prompt.mark_routing_seen("s1", version=out["version"], epoch=out["epoch"],
                                              owed=["not-shown"], shown=["not-shown"])
    elif condition == "prompt-skips":
        log = recall_prompt._log_path()
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("".join("session=s1 reason=no_hits ms=1 hits=[] legs=[] degraded=None routing=skipped:timeout\n"
                               for _ in range(2)))
    elif condition in {"cross-mac-stale", "gateway-stale"}:
        replay.configure(**{f"routing_{condition.replace('-', '_')}_at": routing._iso(T0 - timedelta(hours=1))})
    else:
        replay.write(replay.event("evidence", runner="synthetic-runner", lane="unmapped", state="limited",
                                  source="runner", until=routing._iso(T0 + timedelta(hours=3))))
        replay.advance(3601)
    health = routing.doctor_block(now=replay.now).get("routing_delivery")
    expect(isinstance(health, dict), True, f"15/{condition}: doctor has no routing_delivery evidence check")
    assert health["status"] == "red"
    # Recovery must cite the corresponding evidence, never a probe result.
    replay.ack(info)
    if condition == "unmatched-limit":
        replay.configure(routing_lane_map={"synthetic-runner": {"unmapped": "Codex"}})
    elif condition in {"cross-mac-stale", "gateway-stale"}:
        replay.configure(**{f"routing_{condition.replace('-', '_')}_at": routing._iso(replay.now)})
    elif condition == "prompt-skips":
        log.write_text("".join("session=s1 reason=no_hits ms=1 hits=[] legs=[] degraded=None routing=none\n"
                               for _ in range(201)))
    assert routing.doctor_block(now=replay.now)["routing_delivery"]["status"] == "green"


# ---- 16a: pace input, canonical partitions, raw reset evidence ----------------------------


def sample(t, used, *, reset=604800, mac="m1", source="probe", cycle=None, error=0):
    return {"t": t, "used": used, "reset": reset, "mac": mac, "source": source, "cycle": cycle, "t_err": error}


def pace_view(replay, samples, *, now=86400, window="weekly", caps=()):
    replay.now = T0 + timedelta(seconds=now)
    rows = [replay.event("usage_sample", i + 1, account="codex|", window=window,
                         witness_at=routing._iso(T0 + timedelta(seconds=s["t"])), used_pct=s["used"],
                         resets_at=routing._iso(T0 + timedelta(seconds=s["reset"])), mac=s["mac"],
                         source=s["source"], cycle=s["cycle"], t_err=s["t_err"])
            for i, s in enumerate(samples)]
    rows += [replay.event("cap_set", len(rows) + i + 1, account="codex|", window=window, **cap)
             for i, cap in enumerate(caps)]
    view = replay.fold(rows)
    account = next(a for a in view["accounts"] if a["key"] == "codex|")
    # This is the single mapping seam for the future per-account/window
    # schema. No pace calculation or fallback result lives in the rig.
    pace = account.get("pace", {})
    if isinstance(pace, list):
        pace = next((p for p in pace if p["window"] == window), {})
    return pace


def field(value, dotted):
    for key in dotted.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def steady(rate=0.5, *, start=0, end=86400, used=0, reset=604800, step=1800):
    return [sample(t, used + rate * (t - start) / 3600, reset=reset) for t in range(start, end + 1, step)]


@pytest.mark.parametrize("malformed", [False, True], ids=["readings", "error-path"])
def test_16a_extractor_sentinels_witness_age_and_rounding(replay, monkeypatch, capsys, malformed):
    script = Path(__file__).resolve().parents[3] / "tools" / "pace_fixture_extract.py"
    transcript = replay.root / "synthetic-transcript.jsonl"
    surrounding = "SYNTHETIC-FREE-TEXT-SENTINEL"
    tool_arg, tool_result, path_word = "SYNTHETIC-ARG-SENTINEL", "SYNTHETIC-RESULT-SENTINEL", "/Users/SYNTHETIC-PATH"
    # An owner rule creates the record without a newer conversation
    # report overriding the probe's four-minute-old witness.
    routing.add_rule("Synthetic extractor fixture.", via="app")
    replay.usage(witness=T0 - timedelta(minutes=4), reset=T0 + timedelta(days=6))
    block = routing.summary(now=T0)["text"]
    assert block.startswith("## Routing at 14:00Z (")
    assert "96% of its week left, resets Mon 14:00Z (6 d)" in block
    assert "read from Codex 4 min ago" in block
    # Claude Code transcript envelopes, including tool payloads with a decoy
    # routing block. Only the delivered text block supplies a reading.
    stamp = routing._iso(T0 + timedelta(minutes=5))
    rows = [
        {"type": "user", "timestamp": stamp, "message": {"role": "user", "content": [
            {"type": "text", "text": surrounding + "\n" + block}]}},
        {"type": "assistant", "timestamp": stamp, "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tool1", "name": "Read", "input": {
                "file_path": path_word, "text": tool_arg, "decoy": block.replace("96%", "2%")}}]}},
        {"type": "user", "timestamp": stamp, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tool1", "content": tool_result + "\n" + block}]}},
    ]
    transcript.write_text("\n".join(map(json.dumps, rows)) + ("\n{" + surrounding if malformed else "\n"))
    expect(script.is_file(), True, "16a: the deterministic routing-block extractor is not implemented")
    monkeypatch.setattr(sys, "argv", [str(script), str(transcript)])
    exit_code = 0
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as error:
        exit_code = error.code
    captured = capsys.readouterr()
    for sentinel in (surrounding, tool_arg, tool_result, path_word):
        assert sentinel not in captured.out + captured.err
    if malformed:
        # Slice 4Pa defines the error wording; it must reject without echoing
        # input or leaving a partly emitted fixture that looks successful.
        assert isinstance(exit_code, int) and exit_code != 0
        assert captured.out == ""
    else:
        assert exit_code in (None, 0)
        rows = load_pace(json.loads(captured.out))
        assert len(rows) == 1 and rows[0]["used"] == 4
        assert 0 < rows[0]["t_err"] <= 120
        assert abs(rows[0]["t"] + 240) <= rows[0]["t_err"]
        assert abs(rows[0]["reset"] - 6 * 86400) <= rows[0]["t_err"]


@pytest.mark.parametrize("kind", ["rounded-age", "error-too-large", "cached-repeat"])
def test_16a_uncertain_readings_are_not_new_samples(replay, kind):
    samples = steady(end=84600)
    if kind == "cached-repeat":
        samples.append(sample(86399, 12))
        extra, expected = sample(86400, 12, error=60), 49
        assert extra["t"] - samples[-1]["t"] <= extra["t_err"]
    else:
        extra = sample(86400, 12, error=3600 if kind == "rounded-age" else 1801)
        expected = 48
    assert (T0.timestamp() + samples[-1]["t"]) // 900 != (T0.timestamp() + extra["t"]) // 900
    baseline = pace_view(replay, samples)
    expect(baseline.get("buckets"), expected, f"16a/{kind}: baseline sample buckets are missing")
    pace = pace_view(replay, samples + [extra])
    assert pace["buckets"] == expected, f"16a/{kind}: uncertain/cached input was accepted as a fresh bucket"
    assert pace["rate_pct_per_h"] == baseline["rate_pct_per_h"]


@pytest.mark.parametrize("kind", ["same-time-values", "skewed-clock", "different-reset", "late-reading", "two-minute-day"])
def test_16a_mac_merge_is_arrival_order_independent(replay, kind):
    samples = steady()
    if kind == "same-time-values":
        samples += [sample(86400, 12.5, mac="m2")]
    elif kind == "skewed-clock":
        samples += [sample(86400 + 240, 12.5, mac="m2", error=240)]
    elif kind == "different-reset":
        samples += [sample(86400, 12.5, mac="m2", reset=604800 + 240)]
    elif kind == "late-reading":
        samples += [sample(4500, 0.625, mac="m2")]
    else:
        samples = steady(step=120)
    first, second = [pace_view(replay, rows) for rows in (samples, list(reversed(samples)))]
    expected_count = 97 if kind == "two-minute-day" else 50 if kind == "late-reading" else 49
    expect(first.get("buckets"), expected_count,
           f"16a/{kind}: canonical fixed-epoch buckets are missing or chained")
    assert first == second
    if kind in {"same-time-values", "different-reset"}:
        assert first["rate_pct_per_h"] == pytest.approx(12.5 / 24)
    elif kind == "two-minute-day":
        assert first["rate_pct_per_h"] == pytest.approx(0.5)
    if kind == "late-reading":
        on_time = sorted(samples, key=lambda s: s["t"])
        assert first == pace_view(replay, on_time)


@pytest.mark.parametrize("resolution,tail", [
    ("pending", []), ("confirmed", [(86700, 1), (87000, 2)]), ("refuted", [(86700, 95)]),
])
def test_16a_raw_possible_reset_survives_bucket_merge(replay, resolution, tail):
    samples = steady(0.5, used=83) + [sample(86550, 1)]
    pending = pace_view(replay, samples, now=86550)
    expect(pending.get("why"), "possible reset; waiting for the next reading",
           "16a: raw possible reset is missing or hidden by the bucket maximum")
    assert pending["plan"]["verdict"] == "unknown"
    if tail:
        pace = pace_view(replay, samples + [sample(t, value) for t, value in tail], now=tail[-1][0])
        assert pace.get("why") != "possible reset; waiting for the next reading"
        assert pace["plan"]["verdict"] != "unknown", f"16a/{resolution}: settled readings still block the forecast"


def test_16a_fresh_earlier_replacement_reset_wins(replay):
    samples = steady(reset=604800)
    samples += [sample(86401, 12, reset=604800 - 1200)]
    pace = pace_view(replay, samples, now=86401)
    expect(pace.get("cycle_resets_at"), routing._iso(T0 + timedelta(seconds=603600)),
           "16a: current reset does not follow the newest fresh replacement")


# ---- 16b: hand-calculated numerical and schema contracts -------------------------------


def result_vectors():
    # Each vector is input, exact output field(s), and a distinct diagnostic.
    gap = [sample(0, 10), sample(21600, 10), sample(64800, 34), sample(86400, 34)]
    yield "gap-rate", gap, (), {"rate_pct_per_h": 1, "observed_pct": 0, "gappy": True}
    clipped = [sample(-21600, 0), sample(21600, 6), sample(86400, 15)]
    day = ({"kind": "per_day", "pct": 13, "revision": "cap1"},)
    yield "boundary-gain-12-not-15", clipped, day, {"day_cap.used_24h_pct": 12, "day_cap.observed_pct": 0,
                                                  "day_cap.verdict": "unknown"}
    yield "half-hour-total", steady(), day, {"day_cap.used_24h_pct": 12, "observed_pct": 100, "rate_pct_per_h": 0.5}
    yield "day-cap-under", steady(0.25), day, {"day_cap.verdict": "under", "day_cap.used_24h_pct": 6,
                                               "day_cap.settles_at_pct": 6, "day_cap.why": None}
    yield "day-cap-20h-floor-met", steady(start=14400), day, {"day_cap.verdict": "under",
                                                              "day_cap.used_24h_pct": 10, "day_cap.why": None}
    yield "day-cap-below-20h-floor", steady(start=21600), day, {
        "day_cap.verdict": "unknown", "day_cap.why": "the last 24 h are covered for only 18 h"}
    yield "plan-outpacing-values", steady(reset=900000), (), {
        "plan.runs_out_at": routing._iso(T0 + timedelta(seconds=720000)), "plan.before_reset_s": 180000,
        "plan.needed_pct_per_h": pytest.approx(88 / 226)}
    yield "plan-underpacing-values", steady(reset=200000), (), {
        "plan.verdict": "underpacing", "plan.runs_out_at": None,
        "plan.unused_at_reset_pct": pytest.approx(88 - 0.5 * 113600 / 3600)}
    aging_invalid = steady()
    aging_invalid[24]["used"] = aging_invalid[23]["used"] - 1
    yield "day-aging-across-invalid-interval", aging_invalid, day, {
        "day_cap.used_24h_pct": 11.5, "day_cap.peak_pct": 12,
        "day_cap.peak_at": routing._iso(T0 + timedelta(seconds=86400 + 45000))}
    yield "steady-near-no-crossing", steady(), day, {"day_cap.verdict": "near", "day_cap.reaches_cap_at": None,
                                                     "day_cap.settles_at_pct": 12}
    # Phase the trailing window ten minutes past the epoch bucket edge.
    # A pre-window anchor, minute-four endpoint and minute-six endpoint
    # occupy distinct buckets while keeping the narrow peak that a
    # fifteen-minute scan misses. This vector alone is evaluated at 87000.
    aging = [sample(-300, 0), sample(840, 0), sample(960, 0.1)]
    aging += [sample(t, 0.1 + 12.89 * (t - 960) / (87000 - 960)) for t in range(2400, 87001, 1800)]
    assert len({int((T0.timestamp() + s["t"]) // 900) for s in aging}) == len(aging)
    yield "exact-aging-crossing", aging, day, {"day_cap.reaches_cap_at": routing._iso(T0 + timedelta(seconds=87000 + 0.01 / 0.54125 * 3600)),
                                             "day_cap.peak_pct": pytest.approx(13.0260833333),
                                             "day_cap.peak_at": routing._iso(T0 + timedelta(seconds=87240))}
    yield "rising-rate-ages-out", steady(0.4, end=43200) + steady(0.8, start=45000, used=5.2), (
        {"kind": "per_day", "pct": 15, "revision": "cap1"},), {
        "day_cap.verdict": "near", "day_cap.settles_at_pct": pytest.approx(14.4),
        "day_cap.reaches_cap_at": routing._iso(T0 + timedelta(seconds=86400 + 3 * 3600))}
    # H=88, r=.5, n differs by the reset chosen for each target ratio.
    for label, seconds in (("outpacing", 900000), ("underpacing", 200000), ("on_pace", 633600)):
        yield label, steady(reset=seconds), (), {"plan.verdict": label}
    yield "zero-burn", steady(0), (), {"rate_pct_per_h": 0, "plan.verdict": "underpacing",
                                      "plan.unused_at_reset_pct": 100, "plan.runs_out_at": None}
    yield "plan-already-reached", steady(0, used=100), (), {"plan.verdict": "limit_reached", "plan.runs_out_at": None}
    share = ({"kind": "share", "pct": 90, "revision": "share1"},)
    yield "share-already-reached", steady(0, used=92), share, {"share_cap.verdict": "cap_reached", "share_cap.runs_out_at": None}
    yield "day-already-over", steady(1), day, {"day_cap.verdict": "over", "day_cap.used_24h_pct": 24}
    yield "reset-time-passed", steady(reset=86399), (), {"plan.verdict": "unknown", "why": "reset passed; waiting for a new reading"}
    yield "both-caps-carried-apart", steady(), day + share, {"share_cap.pct": 90, "day_cap.pct": 13,
                                                            "share_cap.revision": "share1", "day_cap.revision": "cap1"}
    yield "coverage-below-floor", steady(start=54000), (), {"plan.verdict": "unknown", "why": "readings cover only 9 of the last 24 h"}
    yield "last-reading-old", steady(end=75600), (), {"plan.verdict": "unknown", "why": "last reading 3 h old"}
    yield "only-two-readings", [sample(84600, 10), sample(86400, 11)], (), {
        "plan.verdict": "unknown", "why": "only 2 readings in the last 24 h"}
    # Six hours missing in a 24-hour day lowers confidence without moving r.
    gappy = [s for s in steady() if not 18000 < s["t"] < 41400]
    yield "gappy-readings", gappy, (), {"rate_pct_per_h": 0.5, "gappy": True,
                                       "observed_pct": pytest.approx(72.9166666667)}
    correction = steady()
    correction[20]["used"] = correction[19]["used"] - 1
    yield "correction-interval-excluded", correction, (), {"rate_pct_per_h": 0.5}
    reset_mid = steady(0.5, end=43200, used=80) + steady(0.5, start=45000, used=1, reset=1209600)
    yield "reset-mid-window", reset_mid, day, {"rate_pct_per_h": 0.5}
    yield "reset-day-no-interpolated-drop", reset_mid, day, {"day_cap.used_24h_pct": pytest.approx(11.75)}
    pending = steady(0.5, end=84600, used=80) + [sample(86400, 1)]
    yield "possible-reset-pending", pending, (), {"plan.verdict": "unknown", "why": "possible reset; waiting for the next reading"}
    yield "possible-reset-confirmed", pending[:-3] + [sample(82800, 1), sample(84600, 1), sample(86400, 1)], (), {
        "rate_pct_per_h": pytest.approx(11.25 / 23.5)}
    yield "possible-reset-refuted", pending[:-2] + [sample(84600, 1), sample(86400, 92)], (), {
        "rate_pct_per_h": 0.5}
    # A daily crossing must win even when a plan run-out is farther away.
    urgent = [sample(t, 28 if t <= 14400 else 28 + 0.6 * (t - 14400) / 3600)
              for t in range(0, 86401, 1800)]
    yield "daily-headline-before-plan", urgent, day, {
        "headline": "day_cap", "day_cap.reaches_cap_at": routing._iso(T0 + timedelta(seconds=93600)),
        "plan.runs_out_at": routing._iso(T0 + timedelta(seconds=518400))}


RESULTS = list(result_vectors())


@pytest.mark.parametrize("name,samples,caps,expected", RESULTS, ids=[row[0] for row in RESULTS])
def test_16b_pace_results(replay, name, samples, caps, expected):
    pace = pace_view(replay, samples, caps=caps, now=87000 if name == "exact-aging-crossing" else 86400)
    for key, wanted in expected.items():
        # Absence is distinct from an optional null output. Otherwise a
        # missing pace object could accidentally pass every 'no forecast'.
        parent, _, leaf = key.rpartition(".")
        container = field(pace, parent) if parent else pace
        actual = container.get(leaf, "ABSENT") if isinstance(container, dict) else "ABSENT"
        expect(actual, wanted, f"16b/{name}: {key} violates the hand-calculated pace contract")


@pytest.mark.parametrize("offsets,ambiguous", [
    ([899, 1801], False), ([899, 900, 1800, 2700, 3601], False),
    ([0, 240, 480], False), ([899, 1799, 1801], False),
    ([0, 3000, 6000], True),
], ids=["no-middle", "45m02-span", "four-minute-chain", "jitter-chain", "over-hour-span"])
def test_16b_cycle_grouping_in_every_arrival_order(replay, offsets, ambiguous):
    samples = steady(reset=604800 + min(offsets))
    # Distinct disputed values: identical readings from another Mac would be
    # duplicates of the history's last reading and never reach grouping.
    samples += [sample(86400, 12 + 0.001 * (i + 1), reset=604800 + offset, mac=f"m{i + 2}")
                for i, offset in enumerate(offsets)]
    # Permute the disputed readings while retaining enough real history for
    # a known forecast. A false split gives newest-bucket disagreement.
    history, disputed = samples[:-len(offsets)], samples[-len(offsets):]
    for arrival in itertools.permutations(disputed):
        pace = pace_view(replay, history + list(arrival))
        if ambiguous:
            expect(pace.get("why"), "reset time spread over more than an hour",
                   "16b: reset component span is missing or depends on arrival order")
        else:
            expect(pace.get("cycle_resets_at"), routing._iso(T0 + timedelta(seconds=604800 + min(offsets))),
                   "16b: representative reset is missing or depends on arrival order")
            assert pace["plan"]["verdict"] == "underpacing"
            assert pace["valid_pct"] == 100


@pytest.mark.parametrize("kind", ["two-real-resets", "reusable-weekly-id", "unique-cycle-id", "mixed-cycle-ids",
                                  "idless-outside-component", "newest-witness-disagrees"])
def test_16b_cycle_identity(replay, kind):
    samples = steady()
    for s in samples[24:]:
        s["reset"] += 5 * 3600
    if kind == "reusable-weekly-id":
        for s in samples:
            s["cycle"] = "weekly"
    elif kind in {"unique-cycle-id", "mixed-cycle-ids", "idless-outside-component"}:
        for s in samples:
            s["cycle"] = "unique-cycle-1"
        if kind != "unique-cycle-id":
            samples[-1]["cycle"] = None
            samples[-1]["reset"] += 240 if kind == "mixed-cycle-ids" else 5 * 3600
    elif kind == "newest-witness-disagrees":
        samples += [sample(86400, 12, reset=604800, mac="m2")]
    assert max(s["reset"] for s in samples) - min(s["reset"] for s in samples) >= 5 * 3600
    # Intervals crossing cycle boundaries are excluded as a pair (gain and
    # duration). This scope field exposes identity without inventing cycles.
    joins = kind in {"unique-cycle-id", "mixed-cycle-ids"}
    expected_valid = 100 if joins else 100 * 23.5 / 24
    for arrival in (samples, list(reversed(samples)), samples[24:] + samples[:24]):
        pace = pace_view(replay, arrival)
        if kind == "newest-witness-disagrees":
            expect(pace.get("why"), "reset time disagrees between readings", "16b: newest witness bucket hides disagreeing cycles")
        else:
            expect(pace.get("valid_pct"), pytest.approx(expected_valid),
                   f"16b/{kind}: cycle boundary intervals are missing or incorrectly included")
            assert pace["cycle_resets_at"] == routing._iso(T0 + timedelta(seconds=samples[-1]["reset"]))
            assert pace["rate_pct_per_h"] == pytest.approx(0.5)


def test_16b_synthetic_history_with_uncertainty(replay):
    rows = load_pace(json.loads(PACE.read_text()))
    samples = [sample(row["t"], row["used"], reset=row["reset"], error=row["t_err"]) for row in rows]
    pace = pace_view(replay, samples)
    # Synthetic history rises twelve points in a day; rounding allows 60 s
    # at either boundary, rather than pretending the clocks were exact.
    expect(pace.get("rate_pct_per_h"), pytest.approx(0.5, abs=0.002),
           "16b: synthetic uncertainty-bounded history has no correct trailing rate")


# ---- 16c: pushes compare with the last acknowledged forecast ----------------------------


@pytest.mark.parametrize("scenario", ["one-hour-to-ten-minutes", "band-jitter", "verdict-hysteresis", "limiting-window",
                                     "cycle", "cap-revision", "availability", "cumulative-small-moves", "numbers-alone"])
def test_16c_pace_pushes(replay, scenario):
    # Feed scope-defined usage_sample inputs through the production fold.
    # No invented pace_forecast events or precomputed verdict labels.
    base = {"eta": 3600, "window": "five_hour", "reset": 86400 + 4 * 3600,
            "revision": "cap1", "available": True}
    wanted = 1
    if scenario == "one-hour-to-ten-minutes":
        updates = [dict(base, eta=600)]
    elif scenario == "band-jitter":
        updates = [dict(base, eta=s) for s in (3599, 3601, 3599, 3601)]
    elif scenario == "verdict-hysteresis":
        base["ratio"] = 1.14
        updates = [dict(base, ratio=r) for r in (1.151, 1.149, 1.151)]
    elif scenario == "limiting-window":
        updates = [dict(base, window="weekly")]
    elif scenario == "cycle":
        updates = [dict(base, reset=base["reset"] + 5 * 3600)]
    elif scenario == "cap-revision":
        updates = [dict(base, revision="cap2")]
    elif scenario == "availability":
        updates, wanted = [dict(base, available=False), dict(base, available=True)], 2
    elif scenario == "cumulative-small-moves":
        base.update(eta=24000, window="weekly", reset=86400 + 7 * 86400)
        updates = [dict(base, eta=24000 + i * 2000) for i in (1, 2, 3, 4)]
    else:
        base["eta"] = 7200
        updates, wanted = [dict(base, eta=7190)], 0

    def install_readings(state):
        now = int((replay.now - T0).total_seconds())
        # Two points of headroom, or one against the share cap, sets the
        # forecast time. Hysteresis inputs straddle 1.15 of the needed rate.
        headroom = 1 if scenario == "cap-revision" else 2
        rate = headroom * 3600 / state["eta"]
        if "ratio" in state:
            rate = state["ratio"] * 2 * 3600 / (state["reset"] - now)
        span, step = (3600, 600) if state["window"] == "five_hour" else (86400, 1800)
        history = steady(rate, start=now - span, end=now, used=98 - rate * span / 3600,
                         reset=state["reset"], step=step)
        if not state["available"]:
            history = history[-2:]
        rows = [replay.event("usage_sample", i + 1, account="codex|", window=state["window"],
                             witness_at=routing._iso(T0 + timedelta(seconds=s["t"])), used_pct=s["used"],
                             resets_at=routing._iso(T0 + timedelta(seconds=s["reset"])), mac="m1", source="probe")
                for i, s in enumerate(history)]
        if scenario == "cap-revision":
            rows.append(replay.event("cap_set", len(rows) + 1, account="codex|", window=state["window"],
                                     kind="share", pct=99, revision=state["revision"]))
        # Replace only this isolated sample snapshot. Component delivery
        # markers stay intact, so comparisons use the acknowledged forecast.
        path = routing.events_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        return rate

    replay.now = T0 + timedelta(seconds=86400)
    install_readings(base)
    first, _, info = replay.offer()
    expect("at the trailing average" in first.get("text", ""), True,
           f"16c/{scenario}: usage samples produce no delivered pace forecast")
    replay.ack(info)
    count = 0
    for i, update in enumerate(updates, start=1):
        replay.advance(1)
        install_readings(update)
        out, _, info = replay.offer()
        if out["kind"] != "none":
            count += 1
            replay.ack(info)
    assert count == wanted


def test_4pa_cost_20000_events_with_18000_samples_stays_under_budget(replay):
    import time as _time

    from tests.routing_s4_cost_workload import build

    path = routing.events_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [r for r in build(20_000, routing) if r["type"] != "rule_add"]
    count = 0
    for account in ("codex|", "grok|", "claude|"):
        for window, step in (("weekly", 1800), ("five_hour", 600)):
            for i in range(3000):
                at = T0 - timedelta(seconds=(3000 - i) * step)
                rows.append({"type": "usage_sample", "id": f"s{count}", "account": account,
                             "window": window, "witness_at": routing._iso(at),
                             "used_pct": (i * 0.25) % 90,
                             "resets_at": routing._iso(T0 + timedelta(days=3)), "mac": "m1",
                             "source": "probe", "t_err": 60,
                             "written_at": routing._iso(at), "written_by": {"kind": "probe"},
                             "via": "probe"})
                count += 1
    assert count == 18_000
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    view = replay.view()
    # Samples are trimmed to what each window reads: no window keeps more
    # buckets than its trailing window can hold.
    for entry in view["accounts"]:
        for pace in entry.get("pace") or []:
            limit = 97 if pace["window"] == "weekly" else 13
            assert pace["buckets"] <= limit, (entry["key"], pace["window"], pace["buckets"])
    with path.open("a") as stream:
        stream.write(json.dumps({"type": "evidence", "id": "changed", "account": "grok|",
                                 "state": "open", "source": "report",
                                 "written_at": routing._iso(replay.now),
                                 "written_by": {"kind": "conversation", "harness": "codex",
                                                "project": "p", "session": "z"}, "via": "mcp"}) + "\n")
    replay.usage(witness=replay.now, reset=T0 + timedelta(days=6))
    replay.advance(1)
    # The helper's table preparation is outside the measured push, as in
    # the slice cost workload; the push itself has the one-second budget.
    out, elapsed = None, None
    for _attempt in range(20):
        agent_view.prepare_routing_table(refresh=True)
        started = _time.perf_counter()
        try:
            out = routing.prompt_push(recall_prompt.routing_marker("s1"), budget=1.0, now=replay.now)
        except agent_view.InventoryUnavailable:
            continue
        elapsed = _time.perf_counter() - started
        break
    assert out is not None and out["kind"] in {"changes", "full"}
    assert elapsed < 1.0, f"changed push took {elapsed:.3f}s at the 20,000-event/18,000-sample workload"
