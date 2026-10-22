"""Routing slice 12 (case 13): gateway projection and pull delivery.

Hermetic: a fake gateway only. The gateway is simulated by setting
KHIPU_GATEWAY_ACTIVE and presenting a token label through
``request_context.gateway_request`` around ``handle_message`` — no socket,
no live gateway; a disposable PostgreSQL hub. Every write before the gateway is activated is
local test seeding in a temp HOME.
"""

from __future__ import annotations
import itertools
import json
from datetime import datetime, timedelta, timezone
import pytest
from khipu import mcp_server as ms
from khipu import request_context, routing
from tests.test_routing_acceptance import assert_gateway_fields
from tests import routing_gateway_fixture as gateway_fixtures

gateway_hub = gateway_fixtures.gateway_hub
hub = gateway_fixtures.hub
pg_cluster = gateway_fixtures.pg_cluster






T0 = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
ACCOUNTS = [{"label": "Claude", "harness": "claude"},
            {"label": "Codex", "harness": "codex"},
            {"label": "Grok", "harness": "grok"}]
_HARNESS_ENV = ("KHIPU_HARNESS", "KHIPU_SESSION_ID", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID",
                "CLAUDE_CODE_ENTRYPOINT", "CODEX_THREAD_ID", "CODEX_SANDBOX", "CODEX_HOME",
                "GROK_HOOK_NAME", "GROK_HOOK_EVENT", ms.GATEWAY_ACTIVE_ENV)
_IDS = itertools.count(1)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


@pytest.fixture
def gw(tmp_path, monkeypatch, gateway_hub):
    data = tmp_path / "data"
    data.mkdir()
    config = {"usage_accounts": ACCOUNTS, "routing_cloud_copy": True,
              "routing_cloud_copy_rules": True, "routing_cloud_copy_hold_words": True,
              "routing_reset_check": False}
    (data / "config.json").write_text(json.dumps(config))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    for name in _HARNESS_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ms, "_GATEWAY_ACTIVE", False)
    monkeypatch.setattr(routing, "_now", Clock())
    monkeypatch.setattr(routing, "kick_plan_refresh", lambda: False)
    return {"data": data, "tmp": tmp_path, "config": config, "patch": monkeypatch}


def configure(gw, **values):
    gw["config"].update(values)
    (gw["data"] / "config.json").write_text(json.dumps(gw["config"]))


def seed(gw):
    private = str(gw["tmp"] / "private-login")
    routing.add_rule(f"Owner words naming {private} stay local", via="app")
    routing.set_note(f"Owner note naming {private}", via="app")
    routing.set_priority([{"account": "Codex", "role": "builds"},
                          {"account": "Claude", "role": "reviews"}],
                         via="app", expect=routing.priority_version())
    routing.add_hold("Hold words for maintenance", reason="other", scope="all", owner=True, via="app")
    routing.report(state="limited", account="Codex", until="1h",
                   author={"kind": "conversation", "harness": "codex", "session": "seed"})
    return private


def call(gw, name="khipu_route", label=None, **args):
    if gw.get("autopublish", True):
        from khipu import routing_gateway_copy
        gw["patch"].delenv(ms.GATEWAY_ACTIVE_ENV, raising=False)
        assert routing_gateway_copy.publish(force=True)["ok"]
    gw["patch"].setenv(ms.GATEWAY_ACTIVE_ENV, "1")

    def run():
        return ms.handle_message({"jsonrpc": "2.0", "id": next(_IDS), "method": "tools/call",
                                  "params": {"name": name, "arguments": args}})

    answer = run() if label is None else _with_label(label, run)
    assert "error" not in answer, answer
    result = answer["result"]
    return result["isError"], json.loads(result["content"][0]["text"])


def _with_label(label, run):
    with request_context.gateway_request(label):
        return run()


def leaves(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key, inner
            yield from leaves(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from leaves(inner)


def string_leaves(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield str(key)
            yield from string_leaves(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from string_leaves(inner)
    elif isinstance(value, str):
        yield value


def test_gateway_projection_is_closed_path_free_and_switch_gated(gw):
    private = seed(gw)
    error, body = call(gw, session_id="client-a:s1", label="tok-a")
    assert not error, body
    assert body["available"] is True
    assert_gateway_fields(body)
    assert {a["label"] for a in body["accounts"]} == {"Claude", "Codex", "Grok"}
    codex = next(a for a in body["accounts"] if a["label"] == "Codex")
    assert codex["state"] == "limited" and codex["id"]
    assert body["rules"] and body["rules"][0]["text"]
    assert body["note"]["text"] and body["priority"]["entries"][0]["account"] == codex["id"]
    assert body["holds"] and body["holds"][0]["text"]
    serialized = json.dumps(body)
    assert private not in serialized and str(gw["tmp"]) not in serialized
    assert "/private-login" not in serialized
    forbidden = {"written_by", "fingerprint", "effective", "source_cache", "login", "mac", "history",
                 "home", "key", "proposals", "proposal", "unmatched", "sources", "providers", "advice",
                 "caps", "cap_statuses", "disagreement", "last_seen", "owner_request", "pending_by"}
    assert not ({key for key, _ in leaves(body)} & forbidden)
    # The projection cuts paths only in its own copy: local owner words are untouched.
    view = routing.read_view()
    assert private in view["rules"][0]["text"]


def test_gateway_projection_omits_rules_note_and_hold_words_when_switches_off(gw):
    seed(gw)
    configure(gw, routing_cloud_copy_rules=False, routing_cloud_copy_hold_words=False)
    error, body = call(gw, session_id="client-a:s1", label="tok-a")
    assert not error, body
    assert_gateway_fields(body)
    assert not body.get("rules") and not body.get("note") and not body.get("priority")
    assert body["holds"] and "text" not in body["holds"][0]
    assert body["accounts"]  # account state is on whenever the copy is on


def test_gateway_projection_unavailable_when_cloud_copy_off(gw):
    seed(gw)
    configure(gw, routing_cloud_copy=False)
    error, body = call(gw, session_id="client-a:s1", label="tok-a")
    assert not error and body["available"] is False


def test_gateway_since_is_refused_closed(gw):
    seed(gw)
    error, body = call(gw, session_id="client-a:s1", since="1", label="tok-a")
    assert body["available"] is False


def test_gateway_delivery_is_identity_owned(gw):
    seed(gw)
    # No identity: shown, never consumed.
    error, untracked = call(gw, label="tok-a")
    assert not error and untracked["delivery"] == "untracked: pass session_id"
    assert untracked["routing_changes"]["batch"] is None

    error, alice = call(gw, session_id="client-a:s1", label="tok-a")
    assert not error, alice
    alice_batch = alice["routing_changes"]["batch"]
    assert alice_batch and alice["delivery"]["owed"] >= 0

    error, bob = call(gw, session_id="client-b:s2", label="tok-a")
    assert not error, bob
    assert bob["routing_changes"]["batch"] not in (None, alice_batch)
    assert "client-a" not in json.dumps(bob["delivery"]) and alice_batch not in json.dumps(bob)

    # Another identity's ack is refused and leaves the batch where it was.
    error, refused = call(gw, session_id="client-b:s2", ack=alice_batch, label="tok-a")
    assert error and refused["code"] == "ack_not_yours"
    # The same session id under a different token label is another identity.
    error, refused = call(gw, session_id="client-a:s1", ack=alice_batch, label="tok-b")
    assert error and refused["code"] == "ack_not_yours"
    # An entirely unknown batch is answered, never refused: nothing was
    # issued under that name, so there is nothing to acknowledge.
    error, answered = call(gw, session_id="client-a:s1", ack="synthetic-other-batch", label="tok-a")
    assert not error and answered["available"] is True

    # The owner of the batch acks it, then acks each following page/batch
    # until the ledger is acked through the current version.
    batch = alice_batch
    for _ in range(20):
        error, acked = call(gw, session_id="client-a:s1", ack=batch, label="tok-a")
        assert not error, acked
        if acked["delivery"]["acked_through"] == acked["version"]:
            break
        batch = acked["routing_changes"]["batch"]
        assert batch
    else:
        raise AssertionError("gateway delivery never acked through")
    # A duplicate ack is a no-op, not an error.
    error, again = call(gw, session_id="client-a:s1", ack=batch, label="tok-a")
    assert not error, again


CONV_AUTHOR = {"kind": "conversation", "harness": "codex", "project": "projSECRETPROJ",
               "session": "claude:s"}


def seed_planted(gw):
    routing.add_rule("A confirmed owner rule", via="app")
    routing.set_priority([{"account": "Codex", "role": "builds"},
                          {"account": "Claude", "role": "reviews"}],
                         via="app", expect=routing.priority_version())
    routing.add_hold("OWNERHOLDWORDS maintenance", reason="other", scope="all", owner=True, via="app")
    routing.add_hold("CONVHOLDWORDS secret plan", reason="other", scope="all",
                     author=dict(CONV_AUTHOR), via="mcp")
    routing.report(state="limited", account="Codex", until="1h", author=dict(CONV_AUTHOR))


@pytest.mark.parametrize("hold_words", [True, False])
def test_gateway_string_leaves_hide_project_session_and_gate_hold_words(gw, hold_words):
    seed_planted(gw)
    configure(gw, routing_cloud_copy_hold_words=hold_words)
    error, body = call(gw, session_id="client-a:s1", label="tok-a")
    assert not error, body
    assert_gateway_fields(body)
    strings = list(string_leaves(body))
    for sentinel in ("projSECRETPROJ", "claude:s", "CONVHOLDWORDS"):
        assert not any(sentinel in text for text in strings), (sentinel, hold_words)
    if hold_words:
        assert any("OWNERHOLDWORDS" in text for text in strings)
    else:
        assert not any("OWNERHOLDWORDS" in text for text in strings)
    conversation_hold = next(h for h in body["holds"] if not h["owner"])
    assert "text" not in conversation_hold


def test_gateway_pending_proposal_shows_last_confirmed_rule_and_priority(gw):
    routing.add_rule("CONFIRMEDTEXT rule", via="app")
    routing.set_priority([{"account": "Codex", "role": "OWNERROLE"}],
                         via="app", expect=routing.priority_version())
    gw["patch"].setenv("KHIPU_SESSION_ID", "seed-conv")
    shown = routing.conversation_view()
    routing.request_rule("PROPOSALTEXT rule", n=1, author=dict(CONV_AUTHOR),
                         base=next(r["owner_revision"] for r in shown["rules"] if r["n"] == 1))
    shown = routing.conversation_view()
    routing.request_priority([{"account": "Claude", "role": "PROPOSALROLE"}],
                             owner_request="PROPOSALREQUEST", author=dict(CONV_AUTHOR),
                             expect=routing.priority_version(), base=shown["priority_owner_revision"])
    error, body = call(gw, session_id="client-a:s1", label="tok-a")
    assert not error, body
    assert_gateway_fields(body)
    assert [r["text"] for r in body["rules"]] == ["CONFIRMEDTEXT rule"]
    assert body["rules"][0]["pending"] is False
    assert [(e["label"], e["role"]) for e in body["priority"]["entries"]] == [("Codex", "OWNERROLE")]
    assert body["priority"]["pending"] is False
    serialized = json.dumps(body)
    for sentinel in ("PROPOSALTEXT", "PROPOSALROLE", "PROPOSALREQUEST", "projSECRETPROJ", "claude:s"):
        assert sentinel not in serialized


def test_gateway_unknown_expired_and_no_session_acks_are_answered(gw):
    seed(gw)
    error, body = call(gw, session_id="client-a:s1", ack="never-issued-batch", label="tok-a")
    assert not error and body["available"] is True
    error, body = call(gw, ack="never-issued-batch", label="tok-a")
    assert not error and body["delivery"] == "untracked: pass session_id"
    error, alice = call(gw, session_id="client-a:s1", label="tok-a")
    assert not error, alice
    batch = alice["routing_changes"]["batch"]
    assert batch
    routing._now.now += timedelta(seconds=61)
    error, body = call(gw, session_id="client-a:s1", ack=batch, label="tok-a")
    assert not error and body["available"] is True


def test_gateway_no_label_does_not_share_the_gateway_labels_ledger(gw):
    seed(gw)
    error, anon = call(gw, session_id="client-a:s1")
    assert not error, anon
    error, labelled = call(gw, session_id="client-a:s1", label="gateway")
    assert not error, labelled
    anon_batch, labelled_batch = anon["routing_changes"]["batch"], labelled["routing_changes"]["batch"]
    assert anon_batch and labelled_batch and anon_batch != labelled_batch
    error, refused = call(gw, session_id="client-a:s1", ack=anon_batch, label="gateway")
    assert error and refused["code"] == "ack_not_yours"


def test_gateway_pace_pick_never_copies_written_by(gw):
    seed(gw)
    view = routing.read_view()
    planted = {"kind": "conversation", "harness": "codex", "project": "PPROJ", "session": "PSESS"}
    view["accounts"][0]["pace"] = [{"window": "weekly", "headline": "plan", "written_by": dict(planted),
                                    "project": "PPROJ", "session": "PSESS",
                                    "plan": {"verdict": "on_pace", "written_by": dict(planted)}}]
    body = routing.gateway_projection(view, now=routing._now())
    assert body is not None
    assert not ({key for key, _ in leaves(body)} & {"written_by", "project", "session"})
    serialized = json.dumps(body)
    assert "PPROJ" not in serialized and "PSESS" not in serialized


def test_gateway_owner_writes_are_refused_and_write_nothing(gw):
    seed(gw)
    # Materialize any local account migration during the Mac publication;
    # the gateway write-refusal assertions then cover only gateway actions.
    from khipu import routing_gateway_copy
    assert routing_gateway_copy.publish(force=True)["ok"]
    gw["autopublish"] = False
    before = routing.events_file().read_bytes()
    error, body = call(gw, "khipu_owner_act", action="routing.pause", args={"account": "Codex"}, label="tok-a")
    assert error and body["code"] == "not_on_gateway" and not body.get("ok")
    for name, args in (("khipu_route_report", {"state": "open", "account": "Codex"}),
                       ("khipu_route_rule", {"text": "x", "owner_request": "y"}),
                       ("khipu_route_priority", {"entries": [{"account": "Codex"}], "owner_request": "y", "base": "z"}),
                       ("khipu_route_advise", {"account": "Codex"})):
        error, body = call(gw, name, label="tok-a", **args)
        assert body.get("available") is False, (name, body)
    assert routing.events_file().read_bytes() == before
