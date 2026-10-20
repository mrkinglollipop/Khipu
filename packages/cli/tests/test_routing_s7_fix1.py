"""Routing slice 7 fix round 1: every finding proven through handle_message."""
from __future__ import annotations

import itertools
import json
from datetime import datetime, timezone

import pytest

from khipu import mcp_server as ms
from khipu import routing
from khipu.mcp_server import handle_message

T0 = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
ACCOUNTS = [{"label": "Claude", "harness": "claude"},
            {"label": "Codex", "harness": "codex"},
            {"label": "Grok", "harness": "grok"}]
_HARNESS_ENV = ("KHIPU_HARNESS", "KHIPU_SESSION_ID", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID",
                "CLAUDE_CODE_ENTRYPOINT", "CODEX_THREAD_ID", "CODEX_SANDBOX", "CODEX_HOME",
                "GROK_HOOK_NAME", "GROK_HOOK_EVENT", ms.GATEWAY_ACTIVE_ENV)
SECRET = "sk-proj-" + "A" * 32


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


@pytest.fixture
def home(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": ACCOUNTS}))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in _HARNESS_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ms, "_GATEWAY_ACTIVE", False)
    clock = Clock()
    monkeypatch.setattr(routing, "_now", clock)
    return {"data": data, "clock": clock, "tmp": tmp_path}


_IDS = itertools.count(1)


def _call(name, arguments=None):
    out = handle_message({"jsonrpc": "2.0", "id": next(_IDS), "method": "tools/call",
                          "params": {"name": name, "arguments": arguments or {}}})
    result = out["result"]
    return result["isError"], json.loads(result["content"][0]["text"])


def _route(**args):
    error, body = _call("khipu_route", args)
    assert not error, body
    return body


def _configure(home, **values):
    path = home["data"] / "config.json"
    config = json.loads(path.read_text())
    config.update(values)
    path.write_text(json.dumps(config))


# 1: environment identity wins over an explicit session_id --------------------


def test_env_identity_cannot_be_impersonated_by_explicit_session_id(home, monkeypatch):
    routing.add_rule("synthetic owner rule", via="app")
    monkeypatch.setenv("CODEX_THREAD_ID", "owner-sess")
    batch = _route()["routing_changes"]["batch"]
    monkeypatch.setenv("CODEX_THREAD_ID", "foreign-sess")
    error, body = _call("khipu_route", {"session_id": "owner-sess", "ack": batch})
    assert error and body["code"] == "ack_not_yours", body
    monkeypatch.setenv("CODEX_THREAD_ID", "owner-sess")
    assert _route()["routing_changes"]["batch"] == batch  # owner's debt untouched
    # No environment means an explicit native id cannot borrow its ledger.
    monkeypatch.delenv("CODEX_THREAD_ID")
    out = _route(session_id="codex:owner-sess", ack=batch)
    assert out["delivery"] == "untracked: pass session_id", out
    monkeypatch.setenv("CODEX_THREAD_ID", "owner-sess")
    assert _route()["routing_changes"]["batch"] == batch


# 2: every distinct canonical identity gets its own ledger --------------------


def test_claude_and_codex_with_same_native_id_have_separate_ledgers(home, monkeypatch):
    routing.add_rule("synthetic owner rule", via="app")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "same-id")
    claude_batch = _route()["routing_changes"]["batch"]
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    monkeypatch.setenv("CODEX_THREAD_ID", "same-id")
    codex = _route()
    assert codex["routing_changes"]["batch"] != claude_batch
    error, body = _call("khipu_route", {"ack": claude_batch})
    assert error and body["code"] == "ack_not_yours", body
    _route(ack=codex["routing_changes"]["batch"])
    monkeypatch.delenv("CODEX_THREAD_ID")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "same-id")
    assert _route()["routing_changes"]["batch"] == claude_batch  # claude debt survives


@pytest.mark.parametrize("first,second", [
    ("client:parent/child", "client:parent_child"),
    ("client:" + "x" * 130 + "A", "client:" + "x" * 130 + "B"),
])
def test_similar_explicit_identities_have_separate_ledgers(home, first, second):
    routing.add_rule("synthetic owner rule", via="app")
    batch = _route(session_id=first)["routing_changes"]["batch"]
    error, body = _call("khipu_route", {"session_id": second, "ack": batch})
    assert error and body["code"] == "ack_not_yours", body
    assert _route(session_id=first)["routing_changes"]["batch"] == batch


# 3: strict projection on success and error ------------------------------------


def test_route_answers_withhold_secrets_and_account_keys(home, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "sess-3")
    error, _body = _call("khipu_route_rule", {
        "text": f"Use codex|synthetic-home with {SECRET}",
        "owner_request": "synthetic request"})
    assert not error, _body
    body = _route()
    blob = json.dumps(body)
    assert SECRET not in blob and "codex|" not in blob, blob
    error, err_body = _call("khipu_route", {"account": str(home["tmp"] / "private-login")})
    assert error
    blob = json.dumps(err_body)
    assert SECRET not in blob and "codex|" not in blob, blob
    assert err_body["routing_changes"]["items"]  # the error still carries its batch


# 4: login alias fails closed when the discovery inventory is empty -----------


def test_lane_alias_is_not_in_pull_additions_when_inventory_empty(home, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "sess-4")
    _configure(home, routing_lane_map={"runnerx": {"SecretAliasZZZ": "NoSuchAccount"}})
    routing.report(state="limited", runner="runnerx", lane="SecretAliasZZZ", until="1h",
                   author={"kind": "conversation", "harness": "codex", "project": "p", "session": "s"})
    body = _route()
    additions = json.dumps({"routing_changes": body.get("routing_changes"),
                            "delivery": body.get("delivery")})
    assert "SecretAliasZZZ" not in additions, additions


# 5: a batch reserves only content actually returned ----------------------------


def test_since_current_batch_includes_the_exact_text_it_reserves(home, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "sess-5")
    routing.add_rule("synthetic owner rule words", via="app")
    current = _route()
    body = _route(since=f"{current['epoch']}:{current['version']}")
    assert body["lines"] == []
    items = body["routing_changes"]["items"]
    assert items and all(item.get("text") for item in items), items
    assert any("synthetic owner rule words" in item["text"] for item in items), items
    out = _route(ack=body["routing_changes"]["batch"])
    assert out["delivery"]["acked_through"] is not None


def test_error_batch_includes_the_exact_text_it_reserves(home, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "sess-5e")
    routing.add_rule("synthetic owner rule words", via="app")
    error, body = _call("khipu_route", {"account": str(home["tmp"] / "private-login")})
    assert error
    items = body["routing_changes"]["items"]
    assert items and all(item.get("text") for item in items), items
    assert any("synthetic owner rule words" in item["text"] for item in items)
