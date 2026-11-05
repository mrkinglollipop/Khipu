"""Settings sync and answering from either Mac, run for real.

Two fictional Macs (their own data folders, owner turns and clocks) share one
throwaway PostgreSQL (tests/hermetic_pg.py) through the real ``PostgresHub``:
no fake hub, no live state. Each test is something the owner does: a setting
changed on one Mac reaches the other and Undo works there; first switch-on
lists differences and overwrites nothing; a request held on one Mac is answered
on the other and applies once; a stale answer says so; an older Mac's code
(``origin/main``) reads the new lines without harm."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

from khipu import cli, db, mcp_server as ms, needs_you, owner_direction, owner_turns, routing
from khipu import routing_replication as rr
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.test_routing_acceptance import Replay

REPO = Path(__file__).resolve().parents[3]
KEY, OTHER_KEY = "routing_summary_chars", "routing_rule_chars"
_ENV = ("KHIPU_SESSION_ID", "CODEX_THREAD_ID", "GROK_HOOK_NAME", "GROK_HOOK_EVENT", "KHIPU_HARNESS",
        "KHIPU_ISOLATED_DIR", "KHIPU_OWNER_TURN", "CLAUDE_CODE_SESSION_ID")


@pytest.fixture
def pg(hub, monkeypatch):  # noqa: F811
    monkeypatch.setattr(db, "connect", lambda **_kw: psycopg.Connection.connect(hub.dsn))
    conn = hub.connect()
    hub.migrate(conn=conn)
    conn.commit()
    monkeypatch.setattr(routing, "_hub_configured", lambda: True)
    monkeypatch.setattr(routing, "kick_plan_refresh", lambda: False)
    monkeypatch.setattr(ms, "_GATEWAY_ACTIVE", False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    return hub


class Mac(Replay):
    """A fictional Mac: Replay's folders and clock, plus owner turns and the
    conversation tools a harness would run on it."""

    def __init__(self, root, monkeypatch, name, khipu_app):
        self.name, self.app = name, khipu_app
        super().__init__(root, monkeypatch)
        owner_turns.write_settings({"enabled": True})
        with routing._lock():
            self.mac = routing._mac_locked()

    def activate(self):
        super().activate()
        self.patch.setenv("KHIPU_CAPTURE_HOME", str(self.root / "capture"))
        for name in _ENV:
            self.patch.delenv(name, raising=False)
        self.patch.setenv("CLAUDECODE", "1")
        self.patch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
        self.app.as_shell("/opt/harness/claude")

    def say(self, text, session):
        self.activate()
        payload = {"session_id": session, "prompt": text, "cwd": str(self.root)}
        return owner_turns.record(payload, {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}, now=time.time())

    def act(self, action, args, session):
        self.activate()
        self.patch.setenv("CLAUDE_CODE_SESSION_ID", session)
        out = ms.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": {"name": "khipu_owner_act", "arguments": {"action": action, "args": args}}})
        result = out["result"]
        return result["isError"], json.loads(result["content"][0]["text"])

    def waiting(self, session):
        self.activate()
        self.patch.setenv("CLAUDE_CODE_SESSION_ID", session)
        return needs_you.waiting(now=self.now)

    def sync(self):
        self.activate()
        return rr.sync()

    def config_now(self):
        self.activate()
        from khipu.config import load_config
        return load_config()

    def log(self):
        self.activate()
        return owner_direction.read_log()

    def mirrors(self):
        self.activate()
        return rr.remote_requests()

    def undo_in_app(self, line_id):
        self.activate()
        self.app.as_app()
        try:
            return cli.main(["owner-direction", "undo", "--from-app", "--", line_id])
        finally:
            self.app.as_shell("/opt/harness/claude")

    def app_set(self, key, value):
        self.activate()
        self.app.as_app()
        try:
            return cli.main(["config", "--from-app", "--set", key, value])
        finally:
            self.app.as_shell("/opt/harness/claude")

    def paused(self, label="Codex"):
        self.activate()
        return next(x for x in routing.read_view(now=self.now)["accounts"] if x["label"] == label)["state"] == "paused"

    def hold_pause(self):
        """A conversation here asks for something only the owner may do; it is held."""
        self.say("let's pause Codex for the afternoon", "sH")
        bad, out = self.act("direction.request",
                            {"action": "route.pause", "args": {"account": "Codex", "until": "9000h"}}, "sH")
        assert not bad and out["pending"] is True, out
        return out["request"]


@pytest.fixture
def macs(pg, tmp_path, monkeypatch, khipu_app):
    a = Mac(tmp_path / "studio", monkeypatch, "Studio", khipu_app)
    b = Mac(tmp_path / "laptop", monkeypatch, "Laptop", khipu_app)
    assert a.mac != b.mac
    a.configure(routing_mac_names={b.mac: "Laptop"})
    b.configure(routing_mac_names={a.mac: "Studio"})
    return a, b


def both_on(a, b):
    for mac in (a, b):
        mac.configure(routing_replication_live=True)


def sync_all(*order):
    for mac in order:
        assert mac.sync()["ok"]


# ---- a setting changed on one Mac reaches the other, and Undo works there --------------------


def test_a_setting_changed_on_a_reaches_b_and_undo_works_on_b(macs):
    a, b = macs
    both_on(a, b)
    a.activate()
    routing.set_setting(KEY, "900")
    sync_all(a, b)
    assert b.config_now()[KEY] == 900
    (line,) = [x for x in b.log() if x.get("kind") == "action" and x.get("target") == KEY]
    assert line["action"] == "config.routing_setting" and line["auth"] == "other_mac"
    assert "Studio" in line["words"] and line.get("before") == {"value": None, "set": False}

    assert b.undo_in_app(line["id"]) == 0
    assert KEY not in b.config_now()
    sync_all(b)  # a pull never re-applies what the owner took back
    assert KEY not in b.config_now()
    sync_all(a)  # and his Undo is his change: the other Mac follows it
    assert KEY not in a.config_now()


def test_a_later_change_on_this_mac_is_not_overwritten_by_an_older_one_from_the_other(macs):
    a, b = macs
    both_on(a, b)
    a.activate()
    routing.set_setting(KEY, "900")
    sync_all(a)
    b.advance(30)
    b.activate()
    routing.set_setting(KEY, "1100")
    sync_all(b, a, b)
    assert a.config_now()[KEY] == 1100 and b.config_now()[KEY] == 1100


# ---- first switch-on lists differences and overwrites nothing ------------------------------------


def test_first_switch_on_lists_differences_and_overwrites_nothing(macs):
    a, b = macs
    a.configure(**{KEY: 900, OTHER_KEY: 300})
    b.configure(**{KEY: 700, OTHER_KEY: 300})
    a.activate()
    routing.set_setting("routing_replication_live", "on")
    sync_all(a)
    assert b.sync()["disabled"] is True  # B's copy is off: it reads the one switch and nothing else
    assert b.config_now()["routing_replication_live"] is True
    sync_all(b, a)
    b.activate()
    status = rr.settings_status()
    assert [d["key"] for d in status["differences"]] == [KEY]
    assert status["differences"][0]["local"] == 700 and status["differences"][0]["remote"] == 900
    assert b.config_now()[KEY] == 700
    assert not [x for x in b.log() if x.get("target") == KEY]
    items = [i for i in b.waiting("sB")["items"] if i["key"] == "settings-sync"]
    assert items and "1 setting difference" in items[0]["words"]

    # "Use the other Mac's for all" is the app's button: each one is the owner's set, through the app.
    assert b.app_set(KEY, json.dumps(status["differences"][0]["remote"])) == 0
    assert b.config_now()[KEY] == 900
    b.activate()
    assert rr.settings_status()["differences"] == []
    sync_all(b, a)
    assert a.config_now()[KEY] == 900


def test_choosing_this_macs_value_sends_it_to_the_other(macs):
    a, b = macs
    a.configure(**{KEY: 900})
    b.configure(**{KEY: 700})
    a.activate()
    routing.set_setting("routing_replication_live", "on")
    sync_all(a)
    b.sync()
    sync_all(b, a)
    b.activate()
    assert [d["key"] for d in rr.settings_status()["differences"]] == [KEY]
    b.advance(60)
    assert b.app_set(KEY, "700") == 0  # "Use 700": the same value, now chosen
    sync_all(b, a)
    assert a.config_now()[KEY] == 700
    b.activate()
    assert rr.settings_status()["differences"] == []


# ---- flip the copy from a typed conversation on either Mac ------------------------------------


def test_the_copy_is_flipped_on_and_off_from_a_conversation_on_the_other_mac(macs):
    a, b = macs
    a.say("turn on keeping my settings the same", "sA")
    bad, out = a.act("config.routing_setting", {"key": "routing_replication_live", "value": True}, "sA")
    assert not bad and out["ok"] is True, out
    assert a.config_now()["routing_replication_live"] is True
    sync_all(a)
    assert b.config_now().get("routing_replication_live") is not True
    assert b.sync()["disabled"] is True  # off: it reads the one switch and nothing else
    assert b.config_now()["routing_replication_live"] is True

    a.advance(60)
    a.say("turn that off again", "sA")
    bad, out = a.act("config.routing_setting", {"key": "routing_replication_live", "value": False}, "sA")
    assert not bad and out["ok"] is True, out
    assert a.sync()["disabled"] is True  # A is off now, and its off still reaches the hub
    sync_all(b)
    assert b.config_now()["routing_replication_live"] is False


def test_a_mac_that_cannot_reach_the_hub_cannot_hear_the_flip(macs, monkeypatch):
    a, b = macs
    a.activate()
    routing.set_setting("routing_replication_live", "on")
    sync_all(a)
    monkeypatch.setattr(db, "connect", lambda **_kw: (_ for _ in ()).throw(OSError("hub down")))
    out = b.sync()
    assert out["ok"] is False and out["error"] == "replication_unavailable"
    assert b.config_now().get("routing_replication_live") is not True


# ---- what waits on him, answered from either Mac ----------------------------------------------


def test_a_held_request_from_a_is_answered_on_b_and_applies_on_a_once(macs):
    a, b = macs
    both_on(a, b)
    request = a.hold_pause()
    sync_all(a, b)
    shown = b.waiting("sB")
    (item,) = [i for i in shown["items"] if i["key"] == f"held:rm-{request}"]
    assert "Studio" in item["words"] and "pause" in item["words"]
    assert item["settle"]["confirm"]["action"] == "direction.confirm"

    # Before the owner types here, B's conversation cannot say yes for him.
    bad, out = b.act("direction.confirm", {"request": f"rm-{request}"}, "sB")
    assert bad and out["code"] in ("not_confirmed", "no_recorded_turn"), out
    b.say("yes, pause it", "sB")
    bad, out = b.act("direction.confirm", {"request": f"rm-{request}"}, "sB")
    assert not bad and out["sent"] is True, out
    assert not a.paused()

    # The next tick on A applies it with A's own owner handling, and A's log says whose yes it was.
    sync_all(b, a)
    assert a.paused()
    lines = [x for x in a.log() if x.get("action") == "route.pause"]
    assert len(lines) == 1 and lines[0]["auth"] == "direction"
    assert lines[0]["confirmed"]["source"] == "other_mac" and lines[0]["confirmed"]["request"] == request
    sync_all(a, b)
    (done,) = [r for r in b.mirrors() if r["request"] == request]
    assert done["state"] == "applied" and "Applied on Studio" in done["words"]

    # The same answer delivered again, even to a Mac that lost its marker, changes nothing more.
    a.activate()
    state = rr._state()
    state["answers_done"] = []
    rr._save(state)
    b.activate()
    sig = next(e for e in rr.remote_events() if e.get("type") == "request_answer")["sig"]
    routing._write_many(routing.owner_lines(
        [{"type": "request_answer", "request": request, "req_answer": "confirm", "sig": sig}], "direction"))
    sync_all(b, a, a)
    assert len([x for x in a.log() if x.get("action") == "route.pause"]) == 1
    sync_all(a, b)
    assert not [r for r in b.mirrors() if r["state"] == "stale"]


def test_a_no_typed_on_b_drops_the_request_on_a(macs):
    a, b = macs
    both_on(a, b)
    request = a.hold_pause()
    sync_all(a, b)
    b.say("no, leave Codex alone", "sB")
    bad, out = b.act("direction.decline", {"request": f"rm-{request}"}, "sB")
    assert not bad and out["declined"] is True and out["sent"] is True, out
    sync_all(b, a)
    assert not a.paused()
    a.activate()
    row = next(r for r in owner_direction._read_requests(owner_direction.requests_path()) if r["id"] == request)
    assert row["state"] == "declined"


def test_an_answer_to_a_request_that_already_ended_says_so_plainly(macs):
    a, b = macs
    both_on(a, b)
    request = a.hold_pause()
    sync_all(a, b)
    a.say("no, never mind", "sA")  # he answers no on A itself, before B's answer reaches it
    bad, out = a.act("direction.decline", {"request": request}, "sA")
    assert not bad, out
    b.say("yes, pause it", "sB")
    bad, out = b.act("direction.confirm", {"request": f"rm-{request}"}, "sB")
    assert not bad, out
    sync_all(b, a)
    assert not a.paused()
    sync_all(a, b)
    (row,) = [r for r in b.mirrors() if r["request"] == request]
    assert row["state"] == "stale" and row["stale"] is True
    assert "already settled on Studio" in row["words"] and "changed nothing" in row["words"]


def test_a_conversations_setting_request_is_answered_on_the_other_mac(macs):
    a, b = macs
    both_on(a, b)
    a.activate()
    routing.set_setting(KEY, "850", request={"request": "make summaries a bit longer",
                                             "by": {"kind": "conversation", "harness": "codex"},
                                             "at": routing._iso(a.now)})
    sync_all(a, b)
    (item,) = [i for i in b.waiting("sB")["items"] if i["key"] == "held:rm-setting:" + KEY]
    assert "routing setting" in item["words"]
    b.say("yes", "sB")
    bad, out = b.act("direction.confirm", {"request": "rm-setting:" + KEY}, "sB")
    assert not bad and out["sent"] is True, out
    sync_all(b, a)
    assert routing._setting_requests(a.config_now()) == {}
    assert a.config_now()[KEY] == 850
    sync_all(a, b)
    assert b.config_now()[KEY] == 850  # his yes made it his setting on both


# ---- an older Mac on main is unharmed -----------------------------------------------------------

PROBE = """
import json, sys
sys.path.insert(0, sys.argv[2])
import psycopg
from khipu import db, routing, routing_replication as rr
db.connect = lambda **kw: psycopg.Connection.connect(sys.argv[1])
routing._hub_configured = lambda: True
out = rr.sync()
view = routing.read_view()
print(json.dumps({"sync": out, "rules": [r.get("text") for r in view.get("rules", [])],
                  "live": routing._replication_live()}))
"""


def test_an_older_mac_on_main_reads_the_new_lines_without_harm(macs, pg, tmp_path):
    a, b = macs
    both_on(a, b)
    a.activate()
    routing.add_rule("keep Codex for tests", via="app")
    routing.set_setting(KEY, "900")
    request = a.hold_pause()
    sync_all(a, b)
    b.say("yes", "sB")
    assert not b.act("direction.confirm", {"request": f"rm-{request}"}, "sB")[0]
    sync_all(b, a)
    with psycopg.Connection.connect(pg.dsn) as conn:
        kinds = {r[0] for r in conn.execute("SELECT DISTINCT payload ->> 'type' FROM routing_events")}
    assert {"setting_set", "request_state", "request_answer", "rule_add"} <= kinds

    old = tmp_path / "old-main"
    old.mkdir()
    tar = subprocess.run(["git", "-C", str(REPO), "archive", "origin/main", "packages/cli/khipu"],
                         capture_output=True)
    if tar.returncode:
        pytest.skip("origin/main is not available to build the older Mac from")
    subprocess.run(["tar", "-x", "-C", str(old)], input=tar.stdout, check=True)
    assert "request_answer" not in (old / "packages/cli/khipu/routing.py").read_text()
    home = tmp_path / "old-home"
    data = home / "data"
    data.mkdir(parents=True)
    (data / "config.json").write_text(json.dumps({"routing_replication_live": True,
                                                  "usage_accounts": [{"label": "Codex", "harness": "codex"}]}))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KHIPU_", "CLAUDE", "CODEX"))}
    env.update(HOME=str(home), KHIPU_DATA_DIR=str(data), KHIPU_CAPTURE_HOME=str(home / "capture"),
               KHIPU_KEYCHAIN="0", PYTHONPATH=str(old / "packages/cli"), PYTHONDONTWRITEBYTECODE="1")
    done = subprocess.run([sys.executable, "-c", PROBE, pg.dsn, str(old / "packages/cli")], capture_output=True, text=True, env=env,
                          timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert result["sync"]["ok"] is True and result["sync"]["caught_up"] is True, result
    assert "keep Codex for tests" in result["rules"], result
    assert result["live"] is True


# ---- account warnings on a Mac that has no account list of its own ------------------------------


def _priority_rows(mac):
    mac.activate()
    view = routing.read_view(now=mac.now)
    from khipu import routing_advisory

    by_key, _ = routing_advisory._accounts(view)
    entries = [(e["label"], e["in_settings"]) for e in (view["priority"] or {}).get("entries", [])]
    return entries, sorted(r["id"] for r in by_key.values())


def test_account_warnings_on_a_mac_with_no_account_list_of_its_own(macs):
    from khipu import accounts, config

    a, b = macs
    both_on(a, b)
    b.config.pop("usage_accounts")
    b.configure()
    a.activate()
    routing.set_priority([{"account": "Codex"}, {"account": "Grok"}], via="app", expect=routing.priority_version())
    sync_all(a, b)
    entries, ids = _priority_rows(b)
    # No list here is not "stopped tracking": no untracked warning, and one id per account.
    assert entries == [("Codex", True), ("Grok", True)]
    assert ids == ["codex:main", "grok:main"]

    # An account the owner stopped on A still warns on B, and only that one.
    a.activate()
    assert accounts.stop("grok|", keep_priority=True, expect=config.accounts_version())["ok"]
    sync_all(a, b)
    entries, ids = _priority_rows(b)
    assert entries == [("Codex", True), ("Grok", False)]
    assert ids == ["codex:main", "grok:main"]


def test_the_same_priority_is_one_id_on_a_mac_that_has_its_own_list(macs):
    a, b = macs
    both_on(a, b)
    a.activate()
    routing.set_priority([{"account": "Codex"}, {"account": "Grok"}], via="app", expect=routing.priority_version())
    sync_all(a, b)
    entries, ids = _priority_rows(b)
    assert entries == [("Codex", True), ("Grok", True)]
    assert ids == ["claude:main", "codex:main", "grok:main"]  # never also codex| or grok| as a second id


# ---- the app and a terminal answer the same way a conversation does -------------------------------


def test_the_app_answers_a_request_held_on_the_other_mac(macs):
    a, b = macs
    both_on(a, b)
    request = a.hold_pause()
    sync_all(a, b)
    b.activate()
    b.app.as_app()
    try:
        assert cli.main(["owner-direction", "answer", "--from-app", f"rm-{request}", "--yes"]) == 0
    finally:
        b.app.as_shell("/opt/harness/claude")
    sync_all(b, a)
    assert a.paused()
    (line,) = [x for x in a.log() if x.get("action") == "route.pause"]
    assert line["confirmed"]["source"] == "other_mac"


def test_a_yes_typed_on_a_terminal_answers_a_request_held_here(macs, monkeypatch):
    a, b = macs
    request = a.hold_pause()
    a.activate()
    monkeypatch.setattr(routing, "typed_yes", lambda *_a, **_k: True)
    assert cli.main(["owner-direction", "answer", request, "--yes"]) == 0
    assert a.paused()
    (line,) = [x for x in a.log() if x.get("action") == "route.pause"]
    assert line["confirmed"]["source"] == "terminal"
    # Answered once: a second yes finds nothing waiting.
    assert cli.main(["owner-direction", "answer", request, "--yes"]) == 2


def test_a_no_typed_on_a_terminal_declines_a_request_held_here(macs, monkeypatch):
    a, _ = macs
    request = a.hold_pause()
    a.activate()
    monkeypatch.setattr(routing, "typed_yes", lambda *_a, **_k: True)
    assert cli.main(["owner-direction", "answer", request, "--no"]) == 0
    assert not a.paused()


def test_needs_you_yes_and_no_answer_a_request_held_on_the_other_mac_the_same_way(macs, monkeypatch):
    """``khipu needs-you --yes/--no`` and ``khipu owner-direction answer`` share one confirm and one decline."""
    a, b = macs
    both_on(a, b)
    yes_request, no_request = a.hold_pause(), a.hold_pause()
    sync_all(a, b)
    b.activate()
    monkeypatch.setattr(routing, "typed_yes", lambda *_a, **_k: True)
    assert cli.main(["needs-you", "--yes", f"held:rm-{yes_request}"]) == 0
    assert cli.main(["needs-you", "--no", f"held:rm-{no_request}"]) == 0
    sync_all(b, a)
    assert a.paused()
    assert [x for x in a.log() if x.get("action") == "route.pause"][0]["confirmed"]["source"] == "other_mac"


# ---- a request held on the other Mac reads the same on both Macs -------------------------------


def _hold_priority_amend(mac, session="sP"):
    """A conversation asks to amend the priority; it is held for the owner."""
    mac.activate()
    routing.set_priority([{"account": "Codex"}, {"account": "Grok"}], via="app", expect=routing.priority_version())
    mac.say("add a note to the Codex priority", session)
    bad, out = mac.act("direction.request", {"action": "route.priority.amend", "args": {
        "account": "Codex", "append": "and docs", "expect": routing.priority_version()}}, session)
    assert not bad and out["pending"] is True, out
    return out["request"]


def _item_on(mac, request, session="sB"):
    (item,) = [i for i in mac.waiting(session)["items"] if i["key"] == f"held:rm-{request}"]
    return item


def test_a_waiting_request_on_the_other_mac_says_who_asked_and_when(macs):
    a, b = macs
    both_on(a, b)
    request = _hold_priority_amend(a)
    sync_all(a, b)
    item = _item_on(b, request)
    assert item["asked_by"]["who"] == "a Claude Code conversation on Studio", item["asked_by"]
    assert item["asked_by"]["when"] == "just now" and "unknown writer" not in json.dumps(item)
    assert item["settle"]["confirm"]["action"] == "direction.confirm" and not item.get("decline_only")


def test_a_request_the_holder_would_refuse_is_decline_only_on_the_other_mac_with_the_same_reason(macs):
    a, b = macs
    both_on(a, b)
    request = _hold_priority_amend(a)
    sync_all(a, b)
    a.activate()
    routing.set_priority([{"account": "Grok"}], via="app", expect=routing.priority_version())  # makes it out of date
    held = next(r for r in owner_direction._read_requests(owner_direction.requests_path()) if r["id"] == request)
    reason = owner_direction.unapplyable_reason(held)
    assert reason
    sync_all(a, b)
    b.now = datetime.now(timezone.utc) + timedelta(days=3)  # the request was asked in real time
    item = _item_on(b, request)
    assert item["decline_only"] is True and item["settle"]["confirm"] is None
    assert reason in item["words"] and "Studio" in item["words"]
    assert item["asked_by"]["who"] == "a Claude Code conversation on Studio"
    assert item["asked_by"]["when"] == "3 days ago", item["asked_by"]
    # Declining still reaches the holder.
    b.say("no, drop it", "sB")
    bad, out = b.act("direction.decline", {"request": f"rm-{request}"}, "sB")
    assert not bad and out["sent"] is True, out
    sync_all(b, a)
    held = next(r for r in owner_direction._read_requests(owner_direction.requests_path()) if r["id"] == request)
    assert held["state"] == "declined"


def test_a_row_mirrored_before_the_fix_is_corrected_by_the_holders_next_line(macs):
    a, b = macs
    both_on(a, b)
    request = _hold_priority_amend(a)
    sync_all(a, b)
    b.activate()
    path = owner_direction.requests_path()
    state = owner_direction._load(path)
    for row in state["requests"]:
        if row.get("remote"):
            row["by"] = {"kind": "other_mac"}
            row["remote"].pop("blocked", None)
            row["created_at"] = owner_direction._iso((b.now - timedelta(seconds=10)).timestamp())
    owner_direction._save(path, state, b.now.timestamp())
    a.activate()
    routing.set_priority([{"account": "Grok"}], via="app", expect=routing.priority_version())
    sync_all(a, b)
    item = _item_on(b, request)
    assert item["decline_only"] is True and item["asked_by"]["who"] == "a Claude Code conversation on Studio"


# ---- the same account names on a Mac that has no account list -----------------------------------


def _account_labels(mac):
    mac.activate()
    return {a["key"]: a["label"] for a in routing.read_view(now=mac.now)["accounts"]}


def test_accounts_the_other_mac_reads_keep_the_owners_names_on_a_mac_with_no_list(macs):
    a, b = macs
    both_on(a, b)
    a.configure(usage_accounts=[{"label": "Claude second", "harness": "claude", "home": str(a.root / "login")},
                                {"label": "Muse", "harness": "muse"}])
    b.config.pop("usage_accounts")
    b.configure()
    a.activate()
    routing.pause("Claude second", until="24h", via="app")
    routing.pause("Muse", until="24h", via="app")
    sync_all(a, b)
    seen, mine = _account_labels(b), _account_labels(a)
    assert {"claude:second", "muse:main"} <= set(seen), seen
    assert (seen["claude:second"], seen["muse:main"]) == ("Claude second", "Muse"), seen
    assert not any(":" in label for label in seen.values())
    assert mine["claude:second"] == seen["claude:second"] if "claude:second" in mine else True


def test_a_label_is_derived_from_a_shared_id_when_no_mac_holds_one():
    assert routing._label_for_key("muse:main") == "Muse"
    assert routing._label_for_key("claude:second") == "Claude second"
    assert routing._label_for_key("codex:main") == "Codex"
    assert routing._label_for_key("claude|/some/home") == "Claude (home)"
