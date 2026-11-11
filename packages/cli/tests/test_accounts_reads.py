"""The Accounts screen's reads (slice B8): the run-ahead picker's candidates, the app's grant, the Mac on each
owner-actions line (with the other Mac's routing changes), and this Mac's name in ``route --json``.

The grant goes through the one owner path the app's other writes use (``--from-app``); the refusals here are
the ones that path already makes, pinned for the new entry."""
from __future__ import annotations

import json
import os
import socket
import time
from datetime import timedelta

import pytest

from khipu import cli, conversation_overrides as overrides, mac_names, owner_direction, owner_turns, routing
from tests.test_owner_direction_e2e import SESSION, _act, _hook, world as _world  # noqa: F401

world = _world

pytestmark = pytest.mark.usefixtures("world")

OTHER = "b" * 32
SUBJECT = {"kind": "session", "id": "sess-claude-1", "harness": "claude_code"}


@pytest.fixture
def app(world, khipu_app):
    """The Khipu app is the caller (the ``world`` fixture starts every test in an agent's shell)."""
    khipu_app.as_app()
    return khipu_app


def _my_mac() -> str:
    path = routing.routing_dir() / "mac-id"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("a" * 32, encoding="ascii")
    return "a" * 32


@pytest.fixture(autouse=True)
def _no_t3(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_T3_DB", str(tmp_path / "no-t3.sqlite"))


def _in(hours: float) -> str:
    return routing._iso(routing._now() + timedelta(hours=hours))


def _turn(session, *, harness="claude_code", age_s=60, cwd="/Users/matt/Code/Secret-Project", **flags):
    """One recorded owner turn, the way the prompt hook writes it (facts, never words)."""
    now = time.time() - age_s
    row = {"at": routing._iso(routing._now() - timedelta(seconds=age_s)), "ts": now, "harness": harness,
           "session": session, "entrypoint": "cli", "cwd": cwd, "turn": session[:8], "typed": True, "machine": False,
           "wrapped": False, "helper": False, "delegated": False, **flags}
    path = owner_turns.session_file(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _json(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def _candidates(capsys, account="Claude") -> tuple[int, dict]:
    capsys.readouterr()
    code = cli.main(["route", "override", "candidates", "--account", account, "--from-app", "--json"])
    return code, _json(capsys)


def _grant(subject=SUBJECT, until=None, account="Codex", *extra) -> list[str]:
    return ["route", "override", "grant", account, "--subject", json.dumps(subject), *(["--until", until] if until else []),
            "--from-app", *extra]


# ---- candidates ------------------------------------------------------------------------------------------------


def test_candidates_are_only_what_the_picker_shows(app, capsys):
    _turn("sess-claude-1", prompt="SECRET PROMPT TEXT", text="SECRET PROMPT TEXT")
    _turn("sess-claude-1", age_s=5)  # the same conversation again: one row, the later time
    _turn("sess-codex-1", harness="codex")
    code, out = _candidates(capsys)
    assert code == 0 and out["ok"] is True
    (row,) = out["candidates"]
    assert set(row) == {"subject", "title", "mac", "last_active"}
    assert row["subject"]["kind"] == "session" and row["subject"]["id"] == "sess-claude-1"
    assert set(row["subject"]) <= {"kind", "id", "harness", "mac"}
    assert row["title"] == "Claude Code conversation in Secret-Project"
    assert row["mac"] == mac_names.this_mac()
    assert row["last_active"] >= routing._iso(routing._now() - timedelta(seconds=30))
    raw = json.dumps(out)
    assert "SECRET PROMPT" not in raw and "/Users/" not in raw
    # The Codex account gets the Codex conversation.
    _code, codex = _candidates(capsys, "Codex")
    assert [c["subject"]["id"] for c in codex["candidates"]] == ["sess-codex-1"]


def test_candidates_leave_out_what_is_not_the_owners_own_conversation(app, capsys):
    _turn("old-one", age_s=25 * 3600)
    _turn("a-helper", helper=True)
    _turn("a-job", dispatched=True)
    _turn("a-brief", delegated=True)
    _turn("a-machine", machine=True)
    _turn("no-words", typed=False)
    _turn("a-copy", duplicate_of="x")
    _turn("sess-claude-1")
    assert [c["subject"]["id"] for c in _candidates(capsys)[1]["candidates"]] == ["sess-claude-1"]


def test_an_account_with_no_candidates_is_an_empty_list_not_an_error(app, capsys):
    code, out = _candidates(capsys)  # no turn recorded at all
    assert code == 0 and out == {"ok": True, "candidates": []}
    _turn("sess-codex-1", harness="codex")
    assert _candidates(capsys)[1] == {"ok": True, "candidates": []}


def test_candidates_follow_the_lane_map(app, capsys, tmp_path):
    cfg = tmp_path / "data" / "config.json"
    data = json.loads(cfg.read_text())
    data["routing_lane_map"] = {"claude_code": {"claude": "Codex"}}
    cfg.write_text(json.dumps(data))
    _turn("sess-claude-1")
    assert _candidates(capsys, "Claude")[1]["candidates"] == []
    assert [c["subject"]["id"] for c in _candidates(capsys, "Codex")[1]["candidates"]] == ["sess-claude-1"]


def test_a_t3_conversation_is_a_thread_with_its_own_title(app, capsys, monkeypatch):
    from khipu import t3

    monkeypatch.setattr(t3, "session_thread_ids_result", lambda sid: (["thread-abc12345"], True))
    monkeypatch.setattr(t3, "thread_title", lambda tid: "Khipu app mocks")
    _turn("sess-claude-1")
    (row,) = _candidates(capsys)[1]["candidates"]
    assert row["subject"]["kind"] == "thread" and row["subject"]["id"] == "thread-abc12345"
    assert row["title"] == "Khipu app mocks"
    # What the picker hands back is what a grant takes.
    assert cli.main(_grant(row["subject"], _in(2), "Claude")) == 0
    assert overrides.list_overrides()[0]["subject"]["id"] == "thread-abc12345"


def test_only_the_app_gets_candidates(khipu_app, capsys):
    _turn("sess-claude-1")
    khipu_app.as_shell("/opt/harness/claude")
    code, out = _candidates(capsys)
    assert code == 2 and out["ok"] is False and "candidates" not in out
    assert "Secret-Project" not in json.dumps(out)


def test_an_unknown_account_is_named_not_guessed(app, capsys):
    code, out = _candidates(capsys, "No Such Account")
    assert code == 2 and out["ok"] is False and "candidates" not in out


# ---- the grant -------------------------------------------------------------------------------------------------


def test_the_apps_grant_goes_through_the_owner_path_and_ends_by_itself(app, capsys):
    until = _in(3)
    capsys.readouterr()
    assert cli.main(_grant(SUBJECT, until)) == 0
    out = _json(capsys)
    assert set(out) == {"ok", "id", "account", "until", "review_at", "indefinite", "replaced", "owner_action"}
    assert out["ok"] is True and out["until"] == until and out["indefinite"] is False and out["review_at"] is None
    assert out["replaced"] == [] and out["account"].startswith("codex")
    (row,) = overrides.list_overrides()
    # A subject with no Mac (this folder had no identity yet) is bound to this Mac's, made then.
    assert row["subject"] == dict(SUBJECT, mac=routing._local_mac()) and row["until"] == until and not row.get("indefinite")
    # It is on the owner-actions log as the app's, with Undo.
    (line,) = [a for a in owner_direction.actions() if a["action"] == "route.override.grant"]
    assert line["id"] == out["owner_action"] and line["auth"] == "app"
    # At its end it is over, with nothing to clean up first.
    later = routing._parse(until) + timedelta(seconds=1)
    assert overrides.list_overrides(now=later) == []
    # A second grant for the same conversation replaces the first.
    assert cli.main(_grant(SUBJECT, _in(5))) == 0
    assert len(overrides.list_overrides()) == 1


def test_a_grant_with_no_end_is_refused_and_writes_nothing(app, capsys):
    capsys.readouterr()
    assert cli.main(_grant(SUBJECT, None)) == 2
    out = _json(capsys)
    assert out["ok"] is False and "until" in out["error"]
    # Nothing is in force, and the log's written-ahead line (if any) never became an applied change.
    assert overrides.list_overrides() == []
    assert not [a for a in owner_direction.actions() if a["action"] == "route.override.grant" and a["state"] == "applied"]


def test_a_grant_whose_end_has_passed_is_refused(app, capsys):
    capsys.readouterr()
    assert cli.main(_grant(SUBJECT, _in(-1))) == 2
    assert _json(capsys)["ok"] is False and overrides.list_overrides() == []


def test_a_shell_can_not_grant_even_with_the_app_flag(khipu_app, capsys):
    khipu_app.as_shell()
    capsys.readouterr()
    assert cli.main(_grant(SUBJECT, _in(3))) == 2
    assert _json(capsys)["ok"] is False and overrides.list_overrides() == []


def test_an_agents_shell_can_not_grant(khipu_app, capsys):
    khipu_app.as_shell("/opt/harness/claude")
    capsys.readouterr()
    assert cli.main(_grant(SUBJECT, _in(3))) == 2
    assert overrides.list_overrides() == []


def test_a_child_process_of_the_app_can_not_grant(khipu_app, capsys):
    """The app's own process must be the direct caller; something it (or anything) started in between is not."""
    khipu_app.tree = {os.getpid(): (900, "/usr/bin/python3"), 900: (901, "/usr/bin/node"), 901: (1, str(khipu_app.app))}
    capsys.readouterr()
    assert cli.main(_grant(SUBJECT, _in(3))) == 2
    assert overrides.list_overrides() == []


def test_a_dispatched_job_can_not_grant(khipu_app, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_DISPATCHED_LANE", "1")
    khipu_app.as_shell("/opt/harness/claude")
    capsys.readouterr()
    assert cli.main(_grant(SUBJECT, _in(3))) == 2  # the app flag from a job's shell
    assert cli.main(["route", "override", "grant", "Codex", "--subject", json.dumps(SUBJECT), "--until", _in(3)]) == 2
    # and a typed turn does not make a job's call the owner's
    _hook("let this conversation run ahead of pace on Codex for 3 hours")
    bad, out = _act("route.override.grant", {"account": "Codex", "until": _in(3), "request": "x"})
    assert bad and overrides.list_overrides() == [], out


def test_an_agent_with_no_typed_turn_can_not_grant(khipu_app, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "never-typed")
    bad, out = _act("route.override.grant", {"account": "Codex", "until": _in(3), "request": "x"})
    assert bad and out["code"] == "no_recorded_turn" and overrides.list_overrides() == []


def test_the_terminal_refuses_without_a_typed_yes(khipu_app, capsys):
    khipu_app.as_shell()
    capsys.readouterr()
    assert cli.main(["route", "override", "grant", "Codex", "--subject", json.dumps(SUBJECT), "--until", _in(3)]) == 2
    assert overrides.list_overrides() == []


# ---- this Mac's identity under a grant -------------------------------------------------------------------------


def _mac_file():
    return routing.routing_dir() / "mac-id"


def _folder_bytes() -> dict:
    folder = routing.routing_dir()
    return {p.name: p.read_bytes() for p in sorted(folder.iterdir()) if p.is_file()} if folder.is_dir() else {}


def test_candidate_grant_still_matches_after_first_routing_write(app):
    """A fresh data folder: the picker's subject names this Mac, so the first routing write leaves the grant on."""
    assert not _mac_file().exists()
    _turn("sess-claude-1")
    candidate, = overrides.candidates("Claude")
    mine = routing._local_mac()
    assert mine and candidate["subject"]["mac"] == mine
    assert cli.main(_grant(candidate["subject"], _in(2), "Claude")) == 0
    # A normal later owner action is the first routing write.
    routing.pause("Codex", until=_in(1), via="app")
    assert routing._local_mac() == mine
    advice, reason = overrides.advice("claude|", {"harness": "claude_code", "session": "sess-claude-1"}, environ={})
    assert advice is not None, reason


def test_a_subject_frozen_before_this_mac_had_an_identity_is_granted_for_this_mac(app, capsys):
    """An ask frozen on a fresh folder carries no Mac; the grant (this Mac's own log) binds it to this Mac."""
    assert not _mac_file().exists()
    capsys.readouterr()
    assert cli.main(_grant(dict(SUBJECT, mac=None), _in(2), "Claude")) == 0
    mine = routing._local_mac()
    (row,) = overrides.list_overrides()
    assert mine and row["subject"] == dict(SUBJECT, mac=mine)
    routing.pause("Codex", until=_in(1), via="app")
    advice, reason = overrides.advice("claude|", {"harness": "claude_code", "session": SUBJECT["id"]}, environ={})
    assert advice is not None, reason


def test_an_agents_ask_on_a_fresh_folder_names_this_mac(world):
    """The conversation's own ask freezes its subject with this Mac's identity, made then if there is none."""
    from tests.test_owner_direction_e2e import SESSION

    _hook("let this conversation run ahead on Codex for 3 hours")
    if _mac_file().exists():
        _mac_file().unlink()  # stand in for a folder whose first routing write is still to come
    assert not _act("route.override.grant", {"account": "Codex", "until": _in(3), "request": "x"})[0]
    mine = routing._local_mac()
    (row,) = overrides.list_overrides()
    assert mine and row["subject"]["mac"] == mine
    routing.pause("Claude", until=_in(1), via="app")
    advice, reason = overrides.advice("codex|", {"harness": "claude_code", "session": SESSION}, environ={})
    assert advice is not None, reason


def test_on_a_mac_with_an_identity_candidates_and_grant_are_as_before(app, capsys):
    mine = _my_mac()
    _turn("sess-claude-1")
    before = _folder_bytes()
    code, out = _candidates(capsys)
    assert code == 0
    (row,) = out["candidates"]
    assert row["subject"] == {"kind": "session", "id": "sess-claude-1", "harness": "claude_code", "mac": mine}
    assert _folder_bytes() == before  # the read wrote nothing
    capsys.readouterr()
    assert cli.main(_grant(row["subject"], _in(2), "Claude")) == 0
    (grant,) = overrides.list_overrides()
    assert json.dumps(grant["subject"], sort_keys=True) == json.dumps(row["subject"], sort_keys=True)
    assert _mac_file().read_bytes() == before["mac-id"]
    # A subject naming another Mac is kept as it was asked, not rebound to this one.
    assert cli.main(_grant(dict(row["subject"], mac=OTHER), _in(2), "Claude")) == 0
    assert {r["subject"]["mac"] for r in overrides.list_overrides()} == {mine, OTHER}


def test_candidates_with_nothing_to_offer_make_no_identity(app, capsys):
    assert _candidates(capsys)[1] == {"ok": True, "candidates": []}
    assert not _mac_file().exists()


# ---- the Mac on each owner-actions line ------------------------------------------------------------------------


def _event(kind, **fields):
    return {"type": kind, "id": f"ev-{kind}-{len(fields)}", "mac": OTHER, "written_at": routing._iso(routing._now()),
            "written_by": {"kind": "owner"}, **fields}


def test_each_owner_action_names_its_mac_and_the_other_macs_changes_are_in_the_list(khipu_app, capsys, monkeypatch, tmp_path):
    from khipu import routing_replication

    cfg = tmp_path / "data" / "config.json"
    data = json.loads(cfg.read_text())
    data["routing_mac_names"] = {OTHER: "M1"}
    cfg.write_text(json.dumps(data))
    monkeypatch.setattr(mac_names, "this_mac", lambda: "M5")
    mine = _my_mac()
    events = [
        _event("rule_add", n=3, text="Keep Grok under 90%"),
        _event("cap_set", account="grok|", written_at="2026-01-01T00:00:00Z"),
        _event("priority_confirm"),
        _event("hold_end", target="h123"),
        _event("rule_add", n=4, text="mine", mac=mine),                          # this Mac's own, echoed back
        _event("rule_add", n=5, text="pending", pending=True),                   # not in effect yet
        _event("rule_add", n=6, text="old build", mac=None),                     # an older build writes no Mac
        _event("rule_add", n=7, text="a conversation", written_by={"kind": "conversation"}),
        _event("setting_set", key="x"),                                          # not a routing change
    ]
    monkeypatch.setattr(routing_replication, "remote_events", lambda: events)
    # One change made here, one line another Mac sent that was logged here, one from an older build with no name.
    _hook("let this conversation run ahead on Codex for 3 hours")
    assert not _act("route.override.grant", {"account": "Codex", "until": _in(3), "request": "x"})[0]
    owner_direction._append({"kind": "action", "id": "c0ffee01", "at": "2026-02-01T00:00:00Z", "action": "config.routing_setting",
                             "target": "k", "auth": "other_mac", "state": "applied", "facts": {"from": "M1"}})
    owner_direction._append({"kind": "action", "id": "c0ffee02", "at": "2026-02-02T00:00:00Z", "action": "config.routing_setting",
                             "target": "k", "auth": "other_mac", "state": "applied"})
    capsys.readouterr()
    assert cli.main(["owner-direction", "actions", "--json"]) == 0
    rows = _json(capsys)["actions"]
    by_id = {r["id"]: r for r in rows}
    assert all("mac" in r for r in rows)
    assert by_id["c0ffee01"]["mac"] == "M1" and by_id["c0ffee02"]["mac"] is None
    (here,) = [r for r in rows if r["action"] == "route.override.grant"]
    assert here["mac"] == "M5"
    other = [r for r in rows if r["id"].startswith("other-mac:")]
    assert {r["action"] for r in other} == {"route.rule.add", "route.cap.set", "route.priority.confirm", "route.hold.end"}
    assert {r["mac"] for r in other} == {"M1"} and all(r["auth"] == "other_mac" and r["no_undo"] for r in other)
    assert [r["at"] for r in rows] == sorted((r["at"] for r in rows), reverse=True)
    # An unnamed Mac is "Your other Mac", never an id.
    data["routing_mac_names"] = {}
    cfg.write_text(json.dumps(data))
    assert {r["mac"] for r in owner_direction.listing() if r["id"].startswith("other-mac:")} == {"Your other Mac"}


def test_remote_rule_action_names_the_rule_the_hub_numbered(khipu_app):
    """An older pending row keeps its provisional ``n``; the line carries the rule's entity, which the view names
    by the number the hub gave it."""
    from khipu import routing_replication

    _my_mac()
    event = {
        "type": "rule_add", "id": "019ac000-0000-7000-8000-000000000001",
        "entity": "rule-019ac000-0000-7000-8000-000000000001",
        "mac": OTHER, "written_at": routing._iso(routing._now()),
        "written_by": {"kind": "owner"}, "via": "app", "auth": "app",
        "n": 1, "number_pending": True, "text": "Keep the release account held",
    }
    routing_replication._store([{"event_id": event["id"], "hub_order": 7, "rule_number": 7, "payload": event}])
    (row,) = owner_direction._other_mac_lines()
    (remote,) = routing_replication.remote_events()
    assert routing._rule_number(remote) == 7
    rule = next(r for r in routing.read_view()["rules"] if r["entity"] == event["entity"])
    assert rule["n"] == 7 and rule["text"] == event["text"]  # what the app's ruleWords shows for the entity
    assert row["target"] == event["entity"], row


def test_an_older_rule_line_without_an_entity_carries_the_one_routing_derives(khipu_app):
    event = _event("rule_add", n=3, text="Keep Grok under 90%")
    target = owner_direction._other_mac_target(event)
    assert target == routing.event_identity.entity(event) and target.startswith("rule-")
    edit = _event("rule_edit", n=3, target="rule-019ac000-0000-7000-8000-000000000002", text="x")
    assert owner_direction._other_mac_target(edit) == "rule-019ac000-0000-7000-8000-000000000002"
    # Not a rule: unchanged.
    assert owner_direction._other_mac_target(_event("cap_set", account="grok|")) == "grok|"
    assert owner_direction._other_mac_target(_event("priority_confirm")) == "priority"


def test_an_unreadable_other_macs_record_leaves_this_macs_list_alone(khipu_app, monkeypatch):
    from khipu import routing_replication

    def broken():
        raise ValueError("invalid routing replica")

    monkeypatch.setattr(routing_replication, "remote_events", broken)
    owner_direction._append({"kind": "action", "id": "c0ffee03", "at": "2026-02-01T00:00:00Z", "action": "route.pause",
                             "target": "codex|", "auth": "app", "state": "applied"})
    assert [r["id"] for r in owner_direction.listing()] == ["c0ffee03"]


# ---- this Mac's name in route --json ---------------------------------------------------------------------------


def test_route_json_carries_this_macs_name(khipu_app, capsys, monkeypatch):
    monkeypatch.setattr(mac_names, "this_mac", lambda: "M5")
    capsys.readouterr()
    assert cli.main(["route", "--json"]) == 0
    assert _json(capsys)["this_mac"] == "M5"
    assert cli.main(["route", "--json", "--account", "Codex"]) == 0
    assert _json(capsys)["this_mac"] == "M5"
    monkeypatch.setattr(mac_names, "this_mac", lambda: None)
    assert cli.main(["route", "--json"]) == 0
    assert _json(capsys)["this_mac"] is None


def test_this_macs_name_is_the_one_the_owner_gave_else_the_host(khipu_app, tmp_path):
    assert mac_names.this_mac() == socket.gethostname().split(".")[0]
    mine = _my_mac()
    cfg = tmp_path / "data" / "config.json"
    data = json.loads(cfg.read_text())
    data["routing_mac_names"] = {mine: "M5", OTHER: "M1"}
    cfg.write_text(json.dumps(data))
    assert mac_names.this_mac() == "M5" and mac_names.name_of(OTHER) == "M1" and mac_names.name_of("c" * 32) is None
    assert mac_names.name_of(None) is None
