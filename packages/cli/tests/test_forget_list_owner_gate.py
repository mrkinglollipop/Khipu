"""``forget.list`` through ``khipu_owner_act``: a forgotten episode's hub
summary reaches only a conversation the owner is typing in. A background job
still sees what is listed (ids, strings, count), never the text."""
from __future__ import annotations

import json
from unittest import mock

from khipu import mcp_server, owner_direction

LISTING = {"ok": True, "strings": ["k:" + "cd" * 32], "ids": [{"id": 7, "summary": "SECRET FORGOTTEN SUMMARY"}],
           "count": 2, "facts_hidden": True}


def _call(name: str, arguments: dict) -> dict:
    out = mcp_server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": name, "arguments": arguments}})
    return out["result"]


def _listed() -> dict:
    with mock.patch("khipu.forget.unsent_forgets", return_value=json.loads(json.dumps(LISTING))):
        return _call("khipu_owner_act", {"action": "forget.list", "args": {}})


def test_forget_list_without_an_owner_turn_does_not_return_summaries(monkeypatch):
    monkeypatch.setattr(owner_direction, "check", mock.Mock(
        side_effect=owner_direction.DirectionRefused("no_recorded_turn", "no owner turn")))
    result = _listed()
    assert "SECRET FORGOTTEN SUMMARY" not in json.dumps(result)
    reply = json.loads(result["content"][0]["text"])
    assert reply["ids"] == [{"id": 7}] and reply["strings"] == LISTING["strings"] and reply["count"] == 2


def test_forget_list_in_the_owners_conversation_keeps_summaries(monkeypatch):
    monkeypatch.setattr(owner_direction, "check", mock.Mock(return_value={"auth": "direction"}))
    assert "SECRET FORGOTTEN SUMMARY" in json.dumps(_listed())
