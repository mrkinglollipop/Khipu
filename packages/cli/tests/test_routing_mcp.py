"""Routing truth phase 2 (docs/plans/2026-10-14-routing-truth.md, Tools and
Phase 2): MCP ``khipu_route`` and ``khipu_route_report``, the ``routing``
field of ``khipu_status``'s light payload, the ``routing`` capability, the
instructions line and ``khipu route wait``.

In the test_mcp_server style: every tool is called through
``handle_message`` as a client would call it. Hermetic: each test has its own
data dir (KHIPU_DATA_DIR) whose config.json names the accounts, a fake clock,
and an environment cleared of every harness variable, so the author Khipu
sets comes only from what the test gives the server. Nothing here starts a
process or reaches the hub.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from khipu import mcp_server as ms
from khipu import routing
from khipu.mcp_server import handle_message

T0 = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
ACCOUNTS = [{"label": "Claude", "harness": "claude"},
            {"label": "Claude second", "harness": "claude", "home": "/logins/claude-second"},
            {"label": "Codex", "harness": "codex"},
            {"label": "Grok", "harness": "grok"}]
_HARNESS_ENV = ("KHIPU_HARNESS", "KHIPU_SESSION_ID", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID",
                "CLAUDE_CODE_ENTRYPOINT", "CODEX_THREAD_ID", "CODEX_SANDBOX", "CODEX_HOME", "GROK_HOOK_NAME",
                "GROK_HOOK_EVENT", ms.GATEWAY_ACTIVE_ENV)
_real_wait = routing.wait


class Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> datetime:
        self.now = self.now + timedelta(**kw)
        return self.now


@pytest.fixture
def home(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": ACCOUNTS}))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    for name in _HARNESS_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ms, "_GATEWAY_ACTIVE", False)
    project = tmp_path / "project-z"
    project.mkdir()
    monkeypatch.chdir(project)
    clock = Clock(T0)
    monkeypatch.setattr(routing, "_now", clock)
    return {"data": data, "clock": clock, "tmp": tmp_path}


def _call(name: str, arguments: dict | None = None) -> tuple[bool, dict]:
    out = handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": arguments or {}}})
    result = out["result"]
    return result["isError"], json.loads(result["content"][0]["text"])


def _route(**arguments) -> dict:
    is_error, body = _call("khipu_route", arguments)
    assert not is_error, body
    return body


def _report(**arguments) -> dict:
    is_error, body = _call("khipu_route_report", arguments)
    assert not is_error, body
    return body


def _events() -> list[dict]:
    path = routing.events_file()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _a_full_record() -> None:
    """Every kind of entry: an owner's rule and note, a cli limit, an
    estimated limit, an open report, a down report, a conversation's hold."""
    routing.add_rule("Every result that is acted on gets a re-check from another model family.", via="app")
    routing.set_note("Builds go to Codex this week; Claude second is kept for reviews.", via="app")
    _report(state="limited", account="Claude second",
            message="You've hit your limit · resets at 2026-10-14T17:52:00Z", window="five-hour")
    _report(state="limited", account="Codex", until="1h")
    _report(state="open", account="Claude")
    _report(state="down", account="Grok", message="Please sign in again")
    _report(action="hold", scope="all", reason="machine-busy", text="hold new launches while load1 is over 75")


# ---- no answer contains a pick or a ranking ------------------------------------------------

_PICK_KEYS = re.compile(r"pick|rank|recommend|best|prefer|choose|choice|score|priority|suggest|next_account")
_PICK_WORDS = re.compile(r"\b(recommend\w*|you should use|use \w+ (instead|next)|best (account|lane)|ranked|"
                         r"pick(ed)?|first choice)\b", re.I)


def _walk(value, path="$"):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield path, key, None
            yield from _walk(inner, f"{path}.{key}")
    elif isinstance(value, list):
        for i, inner in enumerate(value):
            yield from _walk(inner, f"{path}[{i}]")
    elif isinstance(value, str):
        yield path, None, value


def _assert_no_pick(answer: dict, label: str) -> None:
    for path, key, text in _walk(answer):
        if key is not None:
            assert not _PICK_KEYS.search(key.lower()), f"{label}: key {path}.{key} reads as a pick or a ranking"
        if text is not None:
            assert not _PICK_WORDS.search(text), f"{label}: {path} reads as a pick: {text!r}"


def test_no_routing_answer_contains_a_pick_or_a_ranking(home):
    _a_full_record()
    view = _route()
    answers = {"khipu_route": view, "khipu_route since": _route(since="0"),
               "khipu_route account": _route(account="Codex"),
               "khipu_route_report": _report(state="open", account="Claude"),
               "khipu_status routing": routing.light_status()}
    for label, answer in answers.items():
        _assert_no_pick(answer, label)
    # Accounts come in the record's own order (what closes work first), the
    # same as `khipu route --json`: the answer adds no order of its own.
    plain = routing.public_view(routing.read_view(), home["clock"].now)
    assert [a["key"] for a in view["accounts"]] == [a["key"] for a in plain["accounts"]]
    assert view["about"].startswith("Khipu reports; routing stays with you.")
    for tool in ("khipu_route", "khipu_route_report"):
        desc = next(t for t in ms.TOOLS if t["name"] == tool)["description"]
        assert desc.endswith("Khipu reports; routing stays with you."), tool


# ---- rules, the note, pauses and clears are refused through MCP --------------------------------


@pytest.mark.parametrize("action", ["pause", "lift", "clear", "dismiss", "note", "set_note", "end_note", "rule",
                                    "add_rule", "edit_rule", "retire_rule", "confirm_rule"])
def test_the_owners_changes_are_refused_through_mcp(home, action):
    is_error, body = _call("khipu_route_report", {"action": action, "account": "Codex",
                                                  "text": "Codex is the only lane for builds"})
    assert is_error
    assert "is the owner's to change: in the app (Settings → Routing) or on a terminal" in body["error"]
    assert _events() == []


def test_an_owners_hold_and_a_claimed_author_or_basis_are_refused(home):
    for extra in ({"owner": True}, {"author": {"kind": "owner"}}, {"written_by": {"kind": "owner"}},
                  {"via": "app"}, {"basis": "cli"}):
        is_error, body = _call("khipu_route_report", {"action": "hold", "scope": "all", "reason": "other",
                                                      "text": "keep it", **extra})
        assert is_error and "Khipu sets who wrote this" in body["error"], extra
    is_error, body = _call("khipu_route_report", {"state": "open", "account": "Codex", "cwd": "/elsewhere"})
    assert is_error and "takes no 'cwd'" in body["error"]
    assert _events() == []
    # An owner's hold can't be ended from a conversation either.
    routing.add_hold("keep Claude second for reviews", reason="keep", scope="account", account="Claude second",
                     owner=True, via="app")
    owner_hold = routing.read_view()["holds"][0]["id"]
    is_error, body = _call("khipu_route_report", {"action": "end_hold", "hold_id": owner_hold})
    assert is_error and "that hold is the owner's" in body["error"]


# ---- the author is always set by Khipu ----------------------------------------------------------


def test_the_author_of_every_mcp_write_is_set_by_khipu(home, monkeypatch):
    # The server's own knowledge: the harness that launched it set these.
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "fixture-session-0001")
    out = _report(state="limited", account="Codex", until="2h", name="night builder")
    assert out["basis"] == "estimated"  # a typed time is a guess, whoever types it
    _report(action="hold", scope="account", account="Codex", reason="keep", text="keep for the release build",
            name="night builder")
    hold_id = routing.read_view()["holds"][0]["id"]
    _report(action="end_hold", hold_id=hold_id)
    expected = {"kind": "conversation", "harness": "claude_code", "project": "project-z",
                "session": "fixture-session-0001"}
    events = _events()
    assert [e["type"] for e in events] == ["evidence", "hold", "hold_end"]
    for event in events:
        by = dict(event["written_by"])
        by.pop("name", None)
        assert by == expected, event["type"]
        assert event["via"] == "mcp"
    # A name the conversation gives itself is kept beside the author, never
    # in its place, and never pushed.
    assert events[0]["written_by"]["name"] == "night builder"
    assert "night builder" not in routing.summary()["text"]
    # Another harness's server says another author: still nothing from arguments.
    monkeypatch.delenv("CLAUDECODE")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    monkeypatch.setenv("KHIPU_HARNESS", "grok_build")
    _report(state="open", account="Claude")
    assert _events()[-1]["written_by"]["harness"] == "grok_build"
    monkeypatch.setenv("KHIPU_HARNESS", "something new")
    _report(state="open", account="Claude")
    assert _events()[-1]["written_by"]["harness"] == "unknown"


# ---- khipu_route returns the words the push leaves out --------------------------------------------


def test_khipu_route_returns_the_words_the_push_leaves_out(home, monkeypatch):
    monkeypatch.setenv("KHIPU_HARNESS", "codex")
    monkeypatch.setenv("KHIPU_SESSION_ID", "fixture-session-0002")
    _report(state="limited", account="Codex", until="1h", name="release runner",
            evidence="job 41 ended on a usage limit after the third retry")
    _report(action="hold", scope="all", reason="disk-low", text="no new builds until the disk is cleaned")
    pushed = routing.summary()["text"]
    for words in ("release runner", "job 41 ended", "no new builds until the disk is cleaned"):
        assert words not in pushed, words
    view = _route()
    codex = next(a for a in view["accounts"] if a["label"] == "Codex")
    assert codex["witness"]["evidence"] == "job 41 ended on a usage limit after the third retry"
    assert "which calls itself “release runner”" in codex["line"]
    hold = view["holds"][0]
    assert hold["text"] == "no new builds until the disk is cleaned"
    assert hold["line"].endswith("“no new builds until the disk is cleaned”")
    # `since` answers only what changed after a version, as `khipu route --since`.
    first = view["version"]
    assert _route(since=str(first))["entities"] == []
    _report(state="open", account="Codex")
    after = _route(since=f"{view['epoch']}:{first}")
    assert after["complete"] is True and after["entities"] == ["account:codex|"]
    assert _route(since="0", account="Codex")["entities"] == ["account:codex|"]


def test_khipu_route_takes_the_same_privacy_path_as_the_cli(home, capsys):
    from khipu import cli

    _a_full_record()
    view = _route()
    for key in ("fingerprint", "digest", "source_cache", "changes", "effective"):
        assert key not in view, key
    assert cli.main(["route", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    for key in ("fingerprint", "digest", "source_cache"):
        assert key not in printed, key
    assert [a["line"] for a in view["accounts"]] == [a["line"] for a in printed["accounts"]]
    assert view["rules"] == printed["rules"] and view["note"] == printed["note"]
    # A name no account matches is refused, never echoed back as a label.
    is_error, body = _call("khipu_route", {"account": "nobody"})
    assert is_error and "doesn't know the account" in body["error"]


# ---- the gateway answers available: false until phase 7 ---------------------------------------------


def test_the_gateway_answers_available_false(home, monkeypatch):
    monkeypatch.setenv(ms.GATEWAY_ACTIVE_ENV, "1")
    assert _route() == {"available": False, "reason": ms.ROUTING_GATEWAY_REASON}
    _, body = _call("khipu_route_report", {"state": "open", "account": "Codex"})
    assert body["available"] is False
    assert ms._routing_light()["available"] is False
    assert _events() == []


# ---- the light status payload, the capability and the instructions line -------------------------------

_NO_PRIOR_WORK = {"items": [], "text": None, "meta": {}}


def test_the_light_status_payload_carries_the_routing_summary(home):
    _a_full_record()
    with mock.patch("khipu.db.connect", side_effect=RuntimeError("no hub in this test")), \
            mock.patch("khipu.recall_prompt.prior_work_for_prompt", return_value=_NO_PRIOR_WORK):
        out = ms._tool_status({"prompt": "where should the build go"})
    routing_field = out["routing"]
    expected = routing.summary()
    assert routing_field["text"] == expected["text"] and routing_field["version"] == expected["version"]
    assert routing_field["text"].startswith("## Routing at 15:00Z (Khipu's routing record, version ")
    assert "Builds go to Codex this week" in routing_field["text"]  # the owner's note, whole
    assert "hold new launches while load1" not in routing_field["text"]  # a conversation's words: never pushed


def test_a_routing_failure_never_breaks_the_light_status(home):
    with mock.patch("khipu.routing.light_status", side_effect=RuntimeError("boom")), \
            mock.patch("khipu.db.connect", side_effect=RuntimeError("no hub in this test")), \
            mock.patch("khipu.recall_prompt.prior_work_for_prompt", return_value=_NO_PRIOR_WORK):
        out = ms._tool_status({"prompt": "where should the build go"})
    assert "routing" not in out and "hub_ok" in out


def test_no_record_on_this_mac_is_said_and_nothing_is_written(home):
    out = routing.light_status()
    assert "No routing record on this Mac." in out["text"]
    view = _route()
    assert view["record"] is False and view["version"] == 0
    assert not routing.events_file().exists() and not routing.view_file().exists()


def test_the_capability_and_the_instructions_line(home):
    from khipu import features
    from khipu.recall_rule import RULE_MD

    assert "routing" in features.capabilities()
    out = handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2025-06-18"}})
    assert "routing" in out["result"]["capabilities"]["experimental"]["khipu"]["capabilities"]
    instructions = out["result"]["instructions"]
    assert instructions == RULE_MD.strip()
    assert "`khipu_route` reads which accounts are open" in instructions
    assert "Khipu reports; you route." in instructions


# ---- khipu route wait --------------------------------------------------------------------------------


class FakeTime:
    """A monotonic clock and a sleep that advance the routing clock too, and
    run one step of the fixture after a chosen sleep."""

    def __init__(self, clock: Clock, at: dict | None = None):
        self.t = 0.0
        self.clock = clock
        self.sleeps: list[float] = []
        self.at = at or {}

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds
        self.clock.advance(seconds=seconds)
        step = self.at.get(len(self.sleeps))
        if step:
            step()


def _conversation_report(**kw) -> dict:
    return routing.report(author=routing.conversation_author(), **kw)


def test_wait_returns_when_the_account_opens(home):
    _conversation_report(state="limited", account="Codex", until="2h")
    fake = FakeTime(home["clock"], at={3: lambda: _conversation_report(state="open", account="Codex")})
    out = routing.wait("Codex", timeout=600, interval=5, sleep=fake.sleep, clock=fake.monotonic)
    assert out["open"] is True and out["ok"] is True and out["state"] == "open"
    assert fake.sleeps == [5, 5, 5]
    assert out["line"].startswith("Codex: open; reported by")
    # It waits and reports: the two reports are the only lines in the record.
    assert [e["type"] for e in _events()] == ["evidence", "evidence"]


def test_wait_returns_at_its_timeout(home):
    _conversation_report(state="limited", account="Codex", until="2h")
    fake = FakeTime(home["clock"])
    before = routing.events_file().read_text()
    out = routing.wait("Codex", timeout=12, interval=5, sleep=fake.sleep, clock=fake.monotonic)
    assert out["open"] is False and out["timed_out"] is True and out["state"] == "limited"
    assert fake.sleeps == [5, 5, 2]
    assert routing.events_file().read_text() == before


def test_wait_counts_a_passed_limit_as_open_but_confirmed_needs_khipus_own_read(home):
    _conversation_report(state="limited", account="Codex", until="10m")
    home["clock"].advance(minutes=11)
    fake = FakeTime(home["clock"])
    out = routing.wait("Codex", timeout=1, interval=1, sleep=fake.sleep, clock=fake.monotonic)
    assert out["open"] is True and out["state"] == "probably_open"
    out = routing.wait("Codex", confirmed=True, timeout=3, interval=1, sleep=fake.sleep, clock=fake.monotonic)
    assert out["open"] is False and out["timed_out"] is True
    # A conversation saying it is open is not a check.
    _conversation_report(state="open", account="Codex")
    out = routing.wait("Codex", confirmed=True, timeout=1, interval=1, sleep=fake.sleep, clock=fake.monotonic)
    assert out["open"] is False and out["state"] == "open"
    # Khipu's own usage probe, with headroom, is.
    home["clock"].advance(seconds=30)
    at = routing._iso(home["clock"].now)
    usage = home["data"] / "state" / "usage.json"
    usage.write_text(json.dumps({"version": 2, "rows": {"codex|": {
        "ok": True, "windows": [{"name": "five_hour", "used_pct": 20.0,
                                 "resets_at": routing._iso(home["clock"].now + timedelta(hours=3))}],
        "probed_at": at, "last_attempt_at": at, "last_good_at": at, "source": "live", "error": None}}}))
    home["clock"].advance(seconds=1)
    out = routing.wait("Codex", confirmed=True, timeout=1, interval=1, sleep=fake.sleep, clock=fake.monotonic)
    assert out["open"] is True and out["state"] == "open"


def test_the_wait_command_exits_0_when_open_and_1_at_its_timeout(home, capsys, monkeypatch):
    from khipu import cli

    _conversation_report(state="limited", account="Codex", until="2h")
    fake = FakeTime(home["clock"])
    monkeypatch.setattr(routing, "wait", lambda *a, **kw: _real_wait(*a, sleep=fake.sleep,
                                                                     clock=fake.monotonic, **kw))
    assert cli.main(["route", "wait", "Codex", "--timeout", "10s"]) == 1
    assert capsys.readouterr().out.startswith("Still not open after the timeout. Codex: limit reached")
    assert cli.main(["route", "wait", "Claude", "--timeout", "10", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["timed_out"] is True
    _conversation_report(state="open", account="Codex")
    assert cli.main(["route", "wait", "Codex"]) == 0
    assert capsys.readouterr().out.startswith("Codex: open;")
    assert cli.main(["route", "wait", "nobody", "--timeout", "1"]) == 2
    assert "doesn't know the account" in json.loads(capsys.readouterr().out)["error"]
    assert cli.main(["route", "wait", "Codex", "--timeout", "soon"]) == 2
