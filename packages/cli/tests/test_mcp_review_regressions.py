"""Baseline compatibility and local routing writes use synthetic boundaries."""
import json
from unittest import mock

import pytest

from khipu import mcp_server as ms, owner_writes, routing_directions


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(ms, "_RELOAD_ON_CHANGE", False)
    monkeypatch.setattr(ms, "_via_https_gateway", lambda: False)


def request(name, args):
    return ms.handle_message({"jsonrpc": "2.0", "id": "review", "method": "tools/call",
                              "params": {"name": name, "arguments": args}})


def payload(response):
    result = response["result"]
    assert not result["isError"], result
    return json.loads(result["content"][0]["text"])


@pytest.mark.parametrize("name,base,field", [
    ("khipu_owed_update", {"id": 1, "action": "close"}, "reason"),
    ("khipu_forget", {"id": "ledger:fixture#one"}, "note"),
    ("khipu_route", {}, "session_id"), ("khipu_route", {}, "account"),
    ("khipu_owed", {}, "project"), ("khipu_owed", {}, "query"),
    ("khipu_search", {"query": "fixture"}, "project"),
    ("khipu_decisions", {}, "project"), ("khipu_get", {"id": "fixture"}, "kind"),
    ("khipu_route_report", {}, "text"),
    ("khipu_capture", {"summary": "fixture"}, "project"),
    ("khipu_capture", {"summary": "fixture"}, "session_id"),
    ("khipu_status", {}, "cwd"), ("khipu_ledger", {"verb": "list"}, "evidence"),
])
def test_empty_optionals_are_absent_before_handler(monkeypatch, name, base, field):
    boundary = mock.Mock(return_value={"ok": True})
    monkeypatch.setitem(ms.TOOL_FUNCS, name, boundary)
    monkeypatch.setattr(ms, "_via_https_gateway", lambda: True)
    assert payload(request(name, {**base, field: ""}))["ok"]
    assert field not in boundary.call_args.args[0]


@pytest.mark.parametrize("field", ["topics", "people", "decisions", "preferences"])
def test_optional_text_array_members_keep_baseline_lenience(monkeypatch, field):
    boundary = mock.Mock(return_value={"ok": True})
    monkeypatch.setitem(ms.TOOL_FUNCS, "khipu_capture", boundary)
    payload(request("khipu_capture", {"summary": "fixture", field: [""]}))
    assert boundary.call_args.args[0][field] == [""]


@pytest.mark.parametrize("state", ["LIMITED", " limited "])
def test_report_state_normalized_for_direct_and_protocol_calls(state):
    with mock.patch("khipu.routing.conversation_write", return_value={"ok": True}) as write:
        ms._tool_route_report({"state": state})
        assert write.call_args.args[0]["state"] == "limited"
        payload(request("khipu_route_report", {"state": state}))
        assert write.call_args.args[0]["state"] == "limited"


@pytest.mark.parametrize("children", ["REMOVE", " remove "])
def test_forget_children_normalized_before_ledger(children):
    with mock.patch("khipu.ledger.remove_ref", return_value={"ok": True}) as remove:
        payload(request("khipu_forget", {"id": "ledger:fixture#one", "children": children}))
        assert remove.call_args.kwargs["children"] == "remove"


@pytest.mark.parametrize("args", [{"limit": "5"}, {"semantic": 0}, {"semantic": 1}])
def test_search_numeric_and_boolean_compatibility(args):
    with mock.patch("khipu.embed.hybrid_search", return_value={"results": []}) as search, \
            mock.patch("khipu.query_log.log_query"):
        payload(request("khipu_search", {"query": "fixture", **args}))
    if "limit" in args:
        assert search.call_args.kwargs["limit"] == 5
    else:
        assert search.call_args.kwargs["mode"] == ("semantic" if args["semantic"] else "hybrid")


@pytest.mark.parametrize("args", [[], "", False, 0, None, [["query", "fixture"]]])
def test_nonobject_arguments_are_protocol_errors(args):
    response = request("khipu_search", args)
    assert response["error"]["code"] == -32602
    assert "arguments" in response["error"]["message"]
    assert ms.handle_message({"jsonrpc": "2.0", "id": 2, "method": "ping"})["result"] == {}


def seed_direction():
    did = "rd-00000000000000aa"
    routing_directions._save({"directions": {did: {"id": did, "session": "fixture", "words": "fixture",
                                                 "status": "open", "at": 1, "occurrence": 1}}})
    return did


def test_gateway_cannot_dismiss_local_direction(monkeypatch):
    did = seed_direction()
    monkeypatch.setattr(ms, "_via_https_gateway", lambda: True)
    with mock.patch.object(owner_writes, "run") as writer:
        out = payload(request("khipu_owed_update", {"id": did, "action": "close"}))
    assert out == {"available": False, "reason": ms.ROUTING_GATEWAY_REASON}
    assert routing_directions.directions()[0]["status"] == "open"
    writer.assert_not_called()


def test_local_dismissal_uses_shared_owner_write_dispatch_without_hub():
    did = seed_direction()
    with mock.patch.object(owner_writes, "run", wraps=owner_writes.run) as writer, \
            mock.patch("khipu.db.connect", side_effect=AssertionError("local direction needs no hub")):
        out = payload(request("khipu_owed_update", {"id": did, "action": "close"}))
        again = payload(request("khipu_owed_update", {"id": did, "action": "close"}))
    assert out["ok"] and again["ok"]
    assert writer.call_count == 2
    assert writer.call_args.args[0] == "owed_update"
    assert writer.call_args.kwargs == {"queue_ok": False}
    assert routing_directions.directions()[0]["status"] == "dismissed"
