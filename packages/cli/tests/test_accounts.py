"""Settings clarity phase 9, slice 9B: tracking and managing accounts.

Hermetic: each test gets a temp HOME holding fake login folders (``.claude``,
``.claude-client``, ``.codex`` and a symlink to one), its own data dir
(KHIPU_DATA_DIR), a fake routing clock and the app as its caller
(``khipu_app``). No real CLI starts: ``usage._command`` names a stub that
leaves a mark when run, and the one read a Track makes is answered by a
patched ``usage.probe_all``.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from khipu import accounts, config, discover, routing, usage
from khipu.cli import main

T0 = datetime(2026, 10, 15, 14, 0, tzinfo=timezone.utc)
ME = {"kind": "conversation", "harness": "claude_code", "project": "project-a", "session": "s-one"}


class Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def mac(tmp_path, monkeypatch, khipu_app):
    home = tmp_path / "home"
    home.mkdir()
    for name in (".claude", ".claude-client", ".codex"):
        (home / name).mkdir()
    (home / "client-link").symlink_to(home / ".claude-client")
    monkeypatch.setenv("HOME", str(home))
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s-one")
    clock = Clock(T0)
    monkeypatch.setattr(routing, "_now", clock)
    mark = tmp_path / "cli-started"
    stub = tmp_path / "fake-cli"
    stub.write_text(f'#!/bin/sh\ntouch "{mark}"\nexit 3\n')
    stub.chmod(0o755)
    monkeypatch.setattr(usage, "_command", lambda harness: str(stub) if harness in ("claude", "codex") else None)
    read: list[dict] = []

    def probe_all(accounts_, *, timeout):
        read.extend(accounts_)
        return [dict(usage._blank_row(a), ok=False, error="timeout", probed_at=usage._iso(T0.timestamp()), ms=1)
                for a in accounts_]

    monkeypatch.setattr(usage, "probe_all", probe_all)
    return {"home": home, "data": data, "clock": clock, "mark": mark, "read": read, "callers": khipu_app}


def _key(home: str | None, harness: str = "claude") -> str:
    return usage._key({"harness": harness, "home": home})


def _cli(argv, capsys) -> tuple[int, dict]:
    rc = main(argv)
    return rc, json.loads(capsys.readouterr().out)


def _version() -> str:
    return accounts.status()["version"]


def _stored() -> list | None:
    return config.load_config().get("usage_accounts")


def _set(**values) -> None:
    path = config.config_file()
    data = json.loads(path.read_text()) if path.is_file() else {}
    data.update(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _track(capsys, *entries, expect=None, extra=()) -> tuple[int, dict]:
    expect = _version() if expect is None else expect
    return _cli(["accounts", "track", "--from-app", f"--expect={expect}", f"--entries={json.dumps(list(entries))}",
                 "--no-read", *extra], capsys)


def _stop(capsys, key, *extra, expect=None) -> tuple[int, dict]:
    expect = _version() if expect is None else expect
    return _cli(["accounts", "stop", "--from-app", f"--expect={expect}", f"--account={key}", *extra], capsys)


def _entry(view: dict, key: str) -> dict | None:
    return next((a for a in view["accounts"] if a["key"] == key), None)


def _latest_lines(view: dict) -> list[str]:
    return view["changes"][-1]["lines"] if view.get("changes") else []


CLIENT = {"harness": "claude", "home": "~/.claude-client", "label": None}


# ---- the one save path -----------------------------------------------------------------------


def test_the_default_write_out_changes_no_key(mac, capsys):
    before = {a["key"] for a in routing.accounts()}
    assert before == {"claude|", "codex|"}
    rc, out = _track(capsys, CLIENT)
    assert rc == 0 and out["ok"] is True
    assert _stored() == [{"label": "Claude", "harness": "claude"}, {"label": "Codex", "harness": "codex"},
                         {"label": "Claude client", "harness": "claude", "home": "~/.claude-client"}]
    assert {a["key"] for a in routing.accounts()} == before | {_key("~/.claude-client")}
    assert out["accounts"] == [{"key": _key("~/.claude-client"), "label": "Claude client"}]
    view = routing.read_view()
    assert "Claude client: newly tracked, not read yet" in _latest_lines(view)
    line = routing.account_line(_entry(view, _key("~/.claude-client")), T0)
    assert line == "Claude client: newly tracked, not read yet"
    public = routing.conversation_view()
    client = next(a for a in public["accounts"] if a["label"] == "Claude client")
    assert client["state"] == "unknown" and client["configured"] is True


def test_the_first_read_fills_the_usage_cache(mac, capsys):
    rc, out = _cli(["accounts", "track", "--from-app", f"--expect={_version()}",
                    f"--entries={json.dumps([CLIENT])}"], capsys)
    assert rc == 0 and [a["label"] for a in mac["read"]] == ["Claude client"]
    assert _key("~/.claude-client") in usage._load_cache()
    assert [r["account"] for r in out["rows"]] == ["Claude client"]


def test_stop_then_track_again_gives_back_the_key_and_its_kept_hold(mac, capsys):
    hold = routing.add_hold("keep it for reviews", reason="keep", scope="account", account="claude|", owner=True,
                            via="app")["id"]
    rc, out = _stop(capsys, "claude|")
    assert rc == 0 and out["holds_kept"] == [hold] and out["holds_ended"] == []
    assert _stored() == [{"label": "Codex", "harness": "codex"}]
    assert any(e["cli"] == "claude" and e["why"] == "you stopped tracking it" for e in discover.turned_down())
    assert _entry(routing.read_view(), "claude|") is None
    rc, out = _track(capsys, {"harness": "claude", "home": "~/.claude", "label": None})
    assert rc == 0 and out["accounts"] == [{"key": "claude|", "label": "Claude"}]
    assert _stored()[-1] == {"label": "Claude", "harness": "claude"}  # stored home-less
    held = next(h for h in routing.read_view()["holds"] if h["id"] == hold)
    assert held["scope"] == {"kind": "account", "account": "claude|"} and held["scope_label"] == "Claude"
    assert not any(e["cli"] == "claude" for e in discover.turned_down())


@pytest.mark.parametrize("spelling", ["link", "full", "slash"])
def test_another_spelling_of_a_tracked_folder_is_the_same_login(mac, capsys, spelling):
    assert _track(capsys, CLIENT)[0] == 0
    before = _version()
    home = mac["home"]
    raw = {"link": str(home / "client-link"), "full": str(home / ".claude-client"),
           "slash": "~/.claude-client/"}[spelling]
    rc, out = _track(capsys, {"harness": "claude", "home": raw, "label": "Again"})
    assert rc == 2 and out["code"] == "login_taken"
    assert _version() == before


def test_names_follow_the_naming_rules(mac, capsys):
    assert _track(capsys, CLIENT)[0] == 0
    client = _key("~/.claude-client")
    for name, code in (("Grok", "name_is_cli"), ("muse", "name_is_cli"), ("work|home", "name_has_bar"),
                       (routing.account_id("codex|"), "name_is_id")):
        rc, out = _cli(["accounts", "rename", "--from-app", f"--expect={_version()}", f"--account={client}",
                        f"--name={name}"], capsys)
        assert rc == 2 and out["code"] == code, name
    rc, out = _track(capsys, {"harness": "codex", "home": "~/.codex-x", "label": "Grok"})
    assert rc == 2 and out["code"] == "name_is_cli"
    # The default login keeps its CLI's own name.
    rc, out = _cli(["accounts", "rename", "--from-app", f"--expect={_version()}", "--account=claude|",
                    "--name=Claude"], capsys)
    assert rc == 0 and out["label"] == "Claude"


def test_a_name_khipu_cannot_suggest_is_asked_for(mac, capsys, monkeypatch):
    monkeypatch.setattr(discover, "default_name", lambda *a, **k: None)
    rc, out = _track(capsys, CLIENT)
    assert rc == 2 and out["code"] == "name_needed" and _stored() is None


def test_a_taken_default_name_gets_a_number(mac, capsys):
    _set(usage_accounts=[{"label": "Claude", "harness": "claude"}, {"label": "Claude client", "harness": "codex"}])
    rc, out = _track(capsys, CLIENT)
    assert rc == 0 and out["accounts"][0]["label"] == "Claude client 2"


# ---- rename and the lane map --------------------------------------------------------------------


def test_a_mapped_lane_keeps_its_evidence_after_a_rename(mac, capsys):
    client = _key("~/.claude-client")
    _set(usage_accounts=[{"label": "Claude", "harness": "claude"},
                         {"label": "Claude client", "harness": "claude", "home": "~/.claude-client"}],
         routing_lane_map={"jobs": {"review-2": "Claude client"}})
    routing.report(state="limited", runner="jobs", lane="review-2", until="2h", author=ME)
    assert _entry(routing.read_view(), client)["state"] == "limited"
    routing.set_note("Keep Claude client for reviews.", via="app")
    rc, out = _cli(["accounts", "rename", "--from-app", f"--expect={_version()}", f"--account={client}",
                    "--name=Claude work"], capsys)
    assert rc == 0 and out["lanes"] == 1 and out["mentions"] == [{"kind": "note"}]
    assert config.load_config()["routing_lane_map"] == {"jobs": {"review-2": client}}
    view = routing.read_view()
    entry = _entry(view, client)
    assert entry["label"] == "Claude work" and entry["state"] == "limited"
    assert "Claude client is now called Claude work" in _latest_lines(view)


def test_an_ambiguous_lane_label_is_left_for_the_owner(mac, capsys):
    client, codex = _key("~/.claude-client"), "codex|"
    _set(usage_accounts=[{"label": "Same", "harness": "claude", "home": "~/.claude-client"},
                         {"label": "Same", "harness": "codex"}],
         routing_lane_map={"jobs": {"review-2": "Same"}})
    routing.report(state="limited", runner="jobs", lane="review-2", until="2h", author=ME)
    rc, out = _track(capsys, {"harness": "claude", "home": None, "label": None})
    assert rc == 0
    assert out["lane_repairs"] == [{"runner": "jobs", "lane": "review-2", "target": "Same", "matches": 2}]
    assert config.load_config()["routing_lane_map"] == {"jobs": {"review-2": "Same"}}
    view = routing.read_view()
    for key in (client, codex):
        assert (_entry(view, key) or {}).get("state") != "limited"
    assert view["lane_repairs"] == [{"runner": "jobs", "lane": "review-2", "target": "Same", "matches": 2}]
    assert "Lane review-2 names Same, which two accounts are called; choose one in Settings → Accounts and " \
           "routing." in routing.doctor_block()["warnings"]
    assert accounts.status()["lane_repairs"] == view["lane_repairs"]


def test_the_owner_setting_the_lane_map_stores_keys(mac):
    routing.set_setting("routing_lane_map", json.dumps({"jobs": {"a": "Codex", "b": "Nobody"}}))
    assert config.load_config()["routing_lane_map"] == {"jobs": {"a": "codex|", "b": "Nobody"}}


# ---- stop tracking ------------------------------------------------------------------------------------


def test_stop_with_the_defaults_leaves_no_row(mac, capsys):
    client = _key("~/.claude-client")
    _set(usage_accounts=[{"label": "Claude", "harness": "claude"},
                         {"label": "Claude client", "harness": "claude", "home": "~/.claude-client"},
                         {"label": "Codex", "harness": "codex"}])
    routing.report(state="limited", account="Claude client", until="2h", author=ME)
    routing.set_priority([{"account": "Claude client", "role": "reviews"}, {"account": "Codex"}], via="app")
    routing.pause(client, via="app")
    assert _entry(routing.read_view(), client)["state"] == "paused"
    rc, out = _stop(capsys, client)
    assert rc == 0 and out["priority"] == "removed" and out["pause"] == "ended"
    view = routing.read_view()
    assert _entry(view, client) is None
    assert [e["label"] for e in view["priority"]["entries"]] == ["Codex"]
    assert "Claude client" not in routing.light_status(start=True)["text"]
    doctor = routing.doctor_block()
    assert "Claude client" not in json.dumps(doctor["accounts"]) + json.dumps(doctor["warnings"])
    assert "Claude client: no longer tracked" in _latest_lines(view)
    assert client not in usage._load_cache()


def test_a_kept_pause_keeps_the_row_as_no_longer_tracked(mac, capsys):
    client = _key("~/.claude-client")
    _set(usage_accounts=[{"label": "Claude", "harness": "claude"},
                         {"label": "Claude client", "harness": "claude", "home": "~/.claude-client"}])
    routing.pause(client, via="app")
    rc, out = _stop(capsys, client, "--keep-pause")
    assert rc == 0 and out["pause"] == "kept"
    entry = next(a for a in routing.conversation_view()["accounts"] if a["label"] == "Claude client")
    assert entry["configured"] is False and entry["state"] == "paused"
    assert entry["line"].endswith("no longer tracked")


def test_the_last_account_needs_a_confirmation(mac, capsys):
    _set(usage_accounts=[{"label": "Codex", "harness": "codex"}])
    before = _version()
    rc, out = _stop(capsys, "codex|")
    assert rc == 2 and out["code"] == "last_account" and _version() == before
    rc, out = _stop(capsys, "codex|", "--confirm-last")
    assert rc == 0 and out["none"] is True
    assert usage.configured_accounts() == []
    assert usage.accounts_status()["source"] == "none"
    assert all(e["configured"] is False for e in routing.read_view()["accounts"])


# ---- a moved login folder ---------------------------------------------------------------------------


def test_a_move_carries_every_record_to_the_new_folder(mac, capsys):
    old, office = _key("~/.claude-client"), _key("~/.claude-office")
    (mac["home"] / ".claude-office").mkdir()
    _set(usage_accounts=[{"label": "Claude", "harness": "claude"},
                         {"label": "Claude client", "harness": "claude", "home": "~/.claude-client"}],
         routing_lane_map={"jobs": {"r": "Claude client"}})
    until = T0 + timedelta(hours=3)
    routing.report(state="limited", account="Claude client", author=ME,
                   message=f"Limit reached, resets at {routing._iso(until)}")
    routing.pause(old, via="app")
    hold = routing.add_hold("reviews only", reason="keep", scope="account", account=old, owner=True,
                            via="app")["id"]
    routing.set_priority([{"account": "Claude client"}], via="app")
    usage._save_cache({old: {"harness": "claude", "account": "Claude client", "ok": True, "windows": []}})
    rc, out = _cli(["accounts", "move", "--from-app", f"--expect={_version()}", f"--account={old}",
                    "--folder=~/.claude-office"], capsys)
    assert rc == 0 and out["key"] == office and out["lanes"] == 1
    assert _stored()[1] == {"label": "Claude client", "harness": "claude", "home": "~/.claude-office"}
    view = routing.read_view()
    shown = [a for a in view["accounts"] if a["label"] == "Claude client"]
    assert [a["key"] for a in shown] == [office] and _entry(view, old) is None
    assert shown[0]["state"] == "paused" and shown[0]["underlying"]["state"] == "limited"
    assert next(h for h in view["holds"] if h["id"] == hold)["scope"]["account"] == office
    assert [e["account"] for e in view["priority"]["entries"]] == [office]
    assert config.load_config()["routing_lane_map"] == {"jobs": {"r": office}}
    cache = usage._load_cache()
    assert office in cache and old not in cache
    assert "Claude client's login folder moved" in " ".join(_latest_lines(view))
    assert "claude-office" not in " ".join(_latest_lines(view))
    # A conversation still running from the old folder reports under the old key.
    routing.lift(office, via="app")
    later = T0 + timedelta(hours=4)
    routing._write({"type": "evidence", "source": "report", "state": "limited", "account": old, "basis": "cli",
                    "until": routing._iso(later), "message_time": True, "written_by": ME, "via": "cli"})
    view = routing.read_view()
    assert _entry(view, old) is None
    assert _entry(view, office)["state"] == "limited" and _entry(view, office)["until"] == routing._iso(later)


def test_a_move_to_a_missing_or_tracked_folder_is_refused(mac, capsys):
    old = _key("~/.claude-client")
    _set(usage_accounts=[{"label": "Claude", "harness": "claude"},
                         {"label": "Claude client", "harness": "claude", "home": "~/.claude-client"}])
    rc, out = _cli(["accounts", "move", "--from-app", f"--expect={_version()}", f"--account={old}",
                    "--folder=~/.claude-nowhere"], capsys)
    assert rc == 2 and out["code"] == "folder_not_found"
    rc, out = _cli(["accounts", "move", "--from-app", f"--expect={_version()}", f"--account={old}",
                    "--folder=~/.claude"], capsys)
    assert rc == 2 and out["code"] == "login_taken"


def test_moving_back_ends_the_old_forward():
    events = [{"type": "account_move", "account": "a|", "target": "b|", "written_by": {"kind": "owner"}},
              {"type": "account_move", "account": "b|", "target": "a|", "written_by": {"kind": "owner"}},
              {"type": "account_move", "account": "x|", "target": "y|",
               "written_by": {"kind": "conversation"}}]
    forward = routing.forwarding(events)
    assert forward == {"b|": "a|"}
    assert routing._forwarded("a|", forward) == "a|" and routing._forwarded("b|", forward) == "a|"


# ---- reads -----------------------------------------------------------------------------------------


def test_a_missing_folder_never_yields_the_sign_in_note(mac):
    row = usage.probe_account({"label": "Gone", "harness": "claude", "home": "~/.claude-gone"})
    assert row["ok"] is False and row["error"] == "login_folder_missing"
    assert row["note"] == usage.NO_FOLDER_NOTE and "sign in" not in row["note"].lower()
    assert not mac["mark"].exists(), "no CLI is started for a missing folder"


def test_a_hand_edited_invalid_entry_survives_an_add(mac, capsys):
    odd = {"label": "Second", "harness": "claude", "home": ".claude-second"}
    _set(usage_accounts=[odd, {"label": "Codex", "harness": "codex"}])
    rc, out = _track(capsys, CLIENT)
    assert rc == 0 and _stored()[0] == odd and len(_stored()) == 3


# ---- compare-and-swap and the owner gate -----------------------------------------------------------


def test_a_stale_version_is_refused_and_changes_nothing(mac, capsys):
    assert _track(capsys, CLIENT)[0] == 0
    stale = "a0000000000000000"
    before = config.config_file().read_text()
    client = _key("~/.claude-client")
    for argv in (["accounts", "track", "--from-app", f"--expect={stale}",
                  f"--entries={json.dumps([{'harness': 'codex', 'home': '~/.codex-x', 'label': 'X'}])}"],
                 ["accounts", "rename", "--from-app", f"--expect={stale}", f"--account={client}", "--name=Work"],
                 ["accounts", "stop", "--from-app", f"--expect={stale}", f"--account={client}"],
                 ["config", "--from-app", f"--expect={stale}", "--set", "usage_accounts", "[]"]):
        rc, out = _cli(argv, capsys)
        assert rc == 2 and out["code"] == "version_moved", argv
    assert config.config_file().read_text() == before
    rc, out = _cli(["config", "--from-app", f"--expect={_version()}", "--set", "usage_accounts",
                    json.dumps(_stored()[:2])], capsys)
    assert rc == 0 and len(_stored()) == 2


def test_the_priority_takes_its_version(mac, capsys):
    assert routing.read_view()["priority_version"] == "p0"
    rc, out = _cli(["route", "priority", "set", "Codex", "--from-app", "--expect=p-stale"], capsys)
    assert rc == 2 and out["code"] == "version_moved" and routing.read_view()["priority"] is None
    rc, out = _cli(["route", "priority", "set", "Codex", "--from-app", "--expect=p0"], capsys)
    assert rc == 0
    view = routing.read_view()
    version = view["priority_version"]
    assert version != "p0" and view["priority"]["version"] == version
    assert routing.conversation_view()["priority"]["version"] == version
    rc, out = _cli(["route", "priority", "clear", "--from-app", "--expect=p0"], capsys)
    assert rc == 2 and out["code"] == "version_moved"
    rc, out = _cli(["route", "priority", "clear", "--from-app", f"--expect={version}"], capsys)
    assert rc == 0 and routing.read_view()["priority"] is None


def test_the_route_view_names_the_priority_version_with_no_priority_set(mac, capsys):
    # Setup's first order is saved with the version read even when no priority
    # is set, and after a clear the version is no longer p0.
    rc, out = _cli(["route", "--json"], capsys)
    assert rc == 0 and out["priority"] is None and out["priority_version"] == "p0"
    assert _cli(["route", "priority", "set", "Codex", "--from-app", "--expect=p0"], capsys)[0] == 0
    version = _cli(["route", "--json"], capsys)[1]["priority_version"]
    assert _cli(["route", "priority", "clear", "--from-app", f"--expect={version}"], capsys)[0] == 0
    rc, out = _cli(["route", "--json"], capsys)
    assert out["priority"] is None and out["priority_version"] not in ("p0", version)
    assert _cli(["route", "priority", "set", "Codex", "--from-app", f"--expect={out['priority_version']}"],
                capsys)[0] == 0


def test_owner_writes_need_the_app_or_a_typed_yes(mac, capsys):
    entries = f"--entries={json.dumps([CLIENT])}"
    rc, out = _cli(["accounts", "track", entries], capsys)
    assert rc == 2 and out["code"] == "owner_only"
    mac["callers"].as_shell("/usr/local/bin/claude")
    rc, out = _cli(["accounts", "track", "--from-app", entries], capsys)
    assert rc == 2 and out["code"] == "owner_only"
    rc, out = _cli(["config", "--from-app", "--set", "usage_accounts", "[]"], capsys)
    assert rc == 2 and out["code"] == "owner_only"
    assert not config.config_file().exists()
    assert _cli(["accounts", "status"], capsys)[0] == 0  # reads need no gate


def test_a_conversation_at_the_owners_request_and_its_undo(mac, capsys):
    _set(usage_accounts=[{"label": "Codex", "harness": "codex"}])
    mac["callers"].as_shell("/usr/local/bin/claude")
    value = json.dumps([{"label": "Codex", "harness": "codex"}, {"label": "Claude", "harness": "claude"}])
    rc, out = _cli(["config", "--at-owner-request=add my Claude login", "--set", "usage_accounts", value], capsys)
    assert rc == 0 and len(_stored()) == 2
    request = accounts.status()["request"]
    assert request["request"] == "add my Claude login" and request["by"]["kind"] == "conversation"
    rc, out = _cli(["config", "--at-owner-request=again", "--set", "usage_accounts", "[]"], capsys)
    assert rc == 2 and out["code"] == "request_waiting" and len(_stored()) == 2
    rc, out = _cli(["accounts", "request", "undo", "--from-app"], capsys)
    assert rc == 2 and out["code"] == "owner_only"
    mac["callers"].as_app()
    rc, out = _cli(["accounts", "request", "undo", "--from-app"], capsys)
    assert rc == 0 and _stored() == [{"label": "Codex", "harness": "codex"}]
    assert accounts.status()["request"] is None
    rc, out = _cli(["accounts", "request", "confirm", "--from-app"], capsys)
    assert rc == 2 and out["code"] == "no_request"


# ---- Not offered again and the automatic-tracking switch -------------------------------------------


def test_not_this_one_and_offer_again_round_trip(mac, capsys):
    rc, _out = _cli(["accounts", "decline", "--from-app", "--harness=claude", "--folder=~/.claude-client"], capsys)
    assert rc == 0
    rc, _out = _cli(["accounts", "decline", "--from-app", "--harness=muse"], capsys)
    assert rc == 0
    found = discover.turned_down()
    assert {(e["cli"], e["path"], e["why"]) for e in found} == {
        ("claude", "~/.claude-client", "you chose Not this one"), ("muse", None, "you chose Not this one")}
    client = next(e for e in found if e["cli"] == "claude")
    assert client["identity"] == discover.identity(mac["home"] / ".claude-client")
    # The folder is gone: Offer again still finds it by its stored folder.
    (mac["home"] / ".claude-client").rename(mac["home"] / ".claude-elsewhere")
    rc, out = _cli(["accounts", "offer-again", "--from-app", "--harness=claude", "--folder=~/.claude-client"],
                   capsys)
    assert rc == 0 and out["removed"] == 1
    assert [e["cli"] for e in discover.turned_down()] == ["muse"]


def _found(monkeypatch):
    cands = [{"cli": "claude", "path": "~/.claude-client", "default": False, "offer_on_home": True},
             {"cli": "codex", "path": "~/.codex", "default": True, "offer_on_home": False}]
    monkeypatch.setattr(discover, "discover", lambda **kw: {"ok": True, "candidates": cands, "looking": False})
    monkeypatch.setattr(discover, "cli_states", lambda: {})


def test_the_switch_off_tracks_nothing(mac, capsys, monkeypatch):
    _found(monkeypatch)
    rc, out = _cli(["usage", "--discover", "--json"], capsys)
    assert rc == 0 and out["auto_tracked"] == [] and _stored() is None
    assert not config.config_file().exists()


def test_the_switch_on_tracks_only_what_home_would_offer(mac, capsys, monkeypatch):
    rc, out = _cli(["accounts", "auto-track", "--from-app", "on"], capsys)
    assert rc == 0 and accounts.status()["auto_track"] is True
    _found(monkeypatch)
    rc, out = _cli(["usage", "--discover", "--json"], capsys)
    assert rc == 0
    assert out["auto_tracked"] == [{"key": _key("~/.claude-client"), "label": "Claude client", "via": "auto"}]
    assert [e.get("home") for e in _stored()] == [None, None, "~/.claude-client"]
    assert [a["label"] for a in mac["read"]] == ["Claude client"]
    rc, out = _cli(["accounts", "auto-track", "--from-app", "off"], capsys)
    assert rc == 0 and accounts.status()["auto_track"] is False


# ---- slice 9B review: confirmed findings, each a failing test first ----

def test_a1_a_lane_map_save_never_overwrites_a_track_that_lands_meanwhile(mac, capsys, monkeypatch):
    # The lane-map save reads config.json, then converts labels against the
    # accounts known; a track started by another writer in between must wait
    # for the save and then land, never be overwritten by it.
    real = routing.accounts
    result: dict = {}
    racer: list[threading.Thread] = []

    def track_now():
        try:
            result["track"] = accounts.track([CLIENT], read=False)
        except Exception as err:  # noqa: BLE001 - the test reports whatever the racer hit
            result["error"] = err

    def racing(*a, **k):
        if not racer:
            racer.append(threading.Thread(target=track_now))
            racer[0].start()
            racer[0].join(0.5)
        return real(*a, **k)

    monkeypatch.setattr(routing, "accounts", racing)
    routing.set_setting("routing_lane_map", json.dumps({"jobs": {"a": "Codex"}}))
    assert racer
    racer[0].join(10)
    assert not racer[0].is_alive() and "error" not in result, result
    stored = _stored() or []
    assert any(isinstance(e, dict) and e.get("home") == "~/.claude-client" for e in stored), stored
    assert config.load_config()["routing_lane_map"] == {"jobs": {"a": "codex|"}}


def _moved(mac, capsys):
    old, office = _key("~/.claude-client"), _key("~/.claude-office")
    (mac["home"] / ".claude-office").mkdir()
    _set(usage_accounts=[{"label": "Claude", "harness": "claude"},
                         {"label": "Claude client", "harness": "claude", "home": "~/.claude-client"}])
    old_id = routing.account_id(old)
    rc, out = _cli(["accounts", "move", "--from-app", f"--expect={_version()}", f"--account={old}",
                    "--folder=~/.claude-office"], capsys)
    assert rc == 0 and out["key"] == office
    return old, old_id, office


def test_a2_a_late_report_by_the_old_id_lands_on_the_moved_account(mac, capsys):
    _old, old_id, office = _moved(mac, capsys)
    later = T0 + timedelta(hours=4)
    routing.report(state="limited", account=old_id, author=ME, message=f"Limit reached, resets at {routing._iso(later)}")
    assert _entry(routing.read_view(), office)["state"] == "limited"


def test_a2_a_late_report_by_the_old_key_lands_on_the_moved_account(mac, capsys):
    old, _old_id, office = _moved(mac, capsys)
    later = T0 + timedelta(hours=4)
    routing.report(state="limited", account=old, author=ME, message=f"Limit reached, resets at {routing._iso(later)}")
    assert _entry(routing.read_view(), office)["state"] == "limited"


def test_a2_an_unknown_folder_is_still_refused_after_a_move(mac, capsys):
    _moved(mac, capsys)
    try:
        routing.report(state="limited", account=_key("~/.claude-elsewhere"), author=ME, message="Limit reached")
    except routing.RouteError:
        return
    raise AssertionError("a folder never tracked or moved was accepted")


def test_b1_automatic_tracking_leaves_a_conversations_request_waiting(mac, capsys, monkeypatch):
    _set(usage_accounts=[{"label": "Codex", "harness": "codex"}], usage_auto_track=True)
    mac["callers"].as_shell("/usr/local/bin/claude")
    value = json.dumps([{"label": "Codex", "harness": "codex"}, {"label": "Claude", "harness": "claude"}])
    rc, _out = _cli(["config", "--at-owner-request=add my Claude login", "--set", "usage_accounts", value], capsys)
    assert rc == 0 and accounts.status()["request"] is not None
    _found(monkeypatch)
    rc, out = _cli(["usage", "--discover", "--json"], capsys)
    assert rc == 0
    assert accounts.status()["request"] is not None, "automatic tracking ended a request waiting for the owner"
    mac["callers"].as_app()
    rc, out = _cli(["accounts", "request", "undo", "--from-app"], capsys)
    assert rc == 0, out
    assert [e["harness"] for e in _stored()] == ["codex"]
