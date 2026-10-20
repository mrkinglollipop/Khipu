import json, uuid
from pathlib import Path
import pytest
from khipu import mcp_server, routing, routing_events
from tests.test_routing_acceptance import Replay

@pytest.fixture
def mac(tmp_path, monkeypatch):
    monkeypatch.setattr(routing, "kick_plan_refresh", lambda: False)
    return Replay(tmp_path / "one", monkeypatch)

AUTH = {"kind": "conversation", "harness": "codex", "project": "p", "session": "s1"}

def test_rerequest_after_dismiss_is_a_new_write(mac):
    first = routing.request_rule("same words", owner_request="please", author=AUTH)
    mac.advance(60)
    routing.dismiss_rule_request(first["entity"], via="app")
    mac.advance(3600)
    routing.request_rule("same words", owner_request="please", author=AUTH)
    assert len(mac.view()["rules"]) == 1, "second identical request after dismiss was swallowed"


def test_mcp_same_request_id_later_report_is_written(mac):
    def report(state):
        return mac.call("khipu_route_report", action="report", state=state, account="Codex",
                        evidence="synthetic")
    report("limited"); mac.advance(60)
    report("open"); mac.advance(60)
    before = len(routing.read_events()[0])
    report("limited")
    assert len(routing.read_events()[0]) == before + 1, "a later real report reusing MCP id 1 was swallowed"

def test_base_of_legacy_rule_is_not_an_unsalted_hash_of_its_text(mac):
    row = mac.event("rule_add", 1, n=1, text="secret words")
    p = routing.events_file(); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(row) + "\n")
    mac.advance(5)
    routing.edit_rule(1, "new", via="app")
    base = routing.read_events()[0][-1]["base"]
    guess = str(uuid.uuid5(routing_events.NAMESPACE, json.dumps(row, sort_keys=True, ensure_ascii=True)))
    assert base != guess
    assert base == routing_events.body_hash(row, routing._id_salt(operations=True))
    assert base != routing_events.body_hash(row, b"another synthetic install salt")


def test_legacy_revision_is_stable_across_salt_cache_reload(mac):
    row = mac.event("rule_add", 1, n=1, text="synthetic words")
    before = routing_events.revision(row)
    routing._ID_SALTS.pop(str(routing.routing_dir() / "id-salt"))
    assert routing_events.revision(row) == before


def test_implicit_requests_at_the_same_time_are_distinct_attempts(mac):
    first = routing.request_rule("same words", author=AUTH)
    second = routing.request_rule("same words", author=AUTH)
    rows = routing.read_events()[0]
    assert first["id"] != second["id"]
    assert rows[0]["op"] != rows[1]["op"]
    assert len(mac.view()["rules"]) == 2


def test_explicit_retry_after_dismiss_returns_existing_line(mac):
    first = routing.request_rule("same words", author=AUTH, op="synthetic-click")
    routing.dismiss_rule_request(first["entity"], via="app")
    before = len(routing.read_events()[0])
    repeated = routing.request_rule("same words", author=AUTH, op="synthetic-click")
    assert repeated["id"] == first["id"]
    assert repeated["entity"] == first["entity"]
    assert repeated["n"] == first["n"]
    assert repeated["ok"]
    assert len(routing.read_events()[0]) == before
    assert mac.view()["rules"] == []


def test_mcp_request_ids_are_scoped_to_server_session(mac, monkeypatch):
    monkeypatch.setenv("KHIPU_HARNESS", "codex")
    monkeypatch.setenv("KHIPU_SESSION_ID", "same-conversation")
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "khipu_route_rule", "arguments": {"text": "synthetic rule",
                                                                      "owner_request": "please add this"}}}

    def call():
        result = mcp_server.handle_message(msg)["result"]
        assert not result["isError"], result
        return json.loads(result["content"][0]["text"])

    monkeypatch.setattr(mcp_server, "_ROUTING_SESSION", "synthetic-server-one", raising=False)
    first = call()
    assert call()["id"] == first["id"]
    monkeypatch.setattr(mcp_server, "_ROUTING_SESSION", "synthetic-server-two")
    second = call()
    assert second["id"] != first["id"]
    assert call()["id"] == second["id"]
    assert len(mac.view()["rules"]) == 2


def test_old_writer_upgrade_preserves_version_and_reports_no_changes(mac):
    # Synthetic log and saved effective view produced by pre-slice commit 6151f23.
    fixture = json.loads((Path(__file__).parent / "fixtures" / "legacy_numbered_upgrade_s3b.json").read_text())
    before = fixture["saved_view"]
    mac.now = routing._parse(before["folded_at"])
    path = routing.events_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    original = "".join(json.dumps(row) + "\n" for row in fixture["events"])
    path.write_text(original)
    routing._save_view(before)
    with routing._lock():
        after = routing._refold_locked(mac.now)
    assert after["effective"] == before["effective"]
    assert after["version"] == before["version"]
    assert after["changes"] == before["changes"]
    assert routing.since(before["version"], after)["entities"] == []
    assert path.read_text() == original


def test_assigned_rules_keep_numeric_keys_and_provisional_rules_use_uuid(mac, monkeypatch):
    assigned = routing.add_rule("assigned words", via="app")
    monkeypatch.setattr(routing, "_pending_numbering", lambda: True)
    provisional = routing.add_rule("provisional words", via="app")
    assert {key for key in mac.view()["effective"] if key.startswith("rule:")} == {
        "rule:1", f"rule:{provisional['entity']}"}
    routing.edit_rule(assigned["entity"], "assigned edit", via="app")
    change = mac.view()["changes"][-1]
    assert change["entities"] == ["rule:1"]
