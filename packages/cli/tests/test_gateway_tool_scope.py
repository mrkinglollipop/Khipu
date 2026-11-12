"""Per-label gateway authority, tested without a hub or real credentials."""
from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from unittest import mock

import pytest

from khipu import gateway as gw
from khipu import mcp_server as ms
from khipu import request_context as rc
from tests.test_gateway import TOKEN, _post

REFUSAL = {"error": "Tool is not available through this gateway."}


def request(name=None):
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call" if name else "tools/list",
            **({"params": {"name": name, "arguments": {}}} if name else {})}


def payload(response):
    return json.loads(response["result"]["content"][0]["text"])


@pytest.fixture(autouse=True)
def clean_gateway(monkeypatch):
    monkeypatch.setattr(ms, "_GATEWAY_ACTIVE", False)
    monkeypatch.delenv("KHIPU_GATEWAY_ACTIVE", raising=False)
    monkeypatch.delenv("KHIPU_GATEWAY_TOOLS", raising=False)
    monkeypatch.setattr(ms, "_RELOAD_ON_CHANGE", False)


def test_allowed_call_and_list_use_same_label_policy(monkeypatch):
    monkeypatch.setenv("KHIPU_GATEWAY_TOOLS", "bot:khipu_status|khipu_get;other:*")
    handler = mock.Mock(return_value={"ok": True})
    monkeypatch.setitem(ms.TOOL_FUNCS, "khipu_status", handler)
    with rc.gateway_request("bot"):
        assert payload(ms.handle_message(request("khipu_status"))) == {"ok": True}
        names = {t["name"] for t in ms.handle_message(request())["result"]["tools"]}
    assert names == {"khipu_status", "khipu_get"}
    handler.assert_called_once_with({})


@pytest.mark.parametrize("name", [name for name in ms.TOOL_FUNCS if name != "khipu_status"]
                         + ["unknown_tool"])
def test_every_unallowed_tool_is_refused_before_dispatch(monkeypatch, capsys, name):
    # Even explicitly listed local-only and unknown names confer no authority.
    monkeypatch.setenv("KHIPU_GATEWAY_TOOLS",
                       "bot:khipu_status|khipu_owner_act|khipu_needs_you|unknown_tool")
    handler = mock.Mock(side_effect=AssertionError("forbidden handler ran"))
    monkeypatch.setitem(ms.TOOL_FUNCS, name, handler)
    with rc.gateway_request("bot"):
        response = ms.handle_message(request(name))
    assert response["result"]["isError"] is True
    assert payload(response) == REFUSAL
    handler.assert_not_called()
    logged = capsys.readouterr().err
    assert 'tool_refused label="bot"' in logged
    assert "tool=" + json.dumps(name) in logged


def test_policy_is_rechecked_after_listing(monkeypatch):
    monkeypatch.setenv("KHIPU_GATEWAY_TOOLS", "bot:khipu_status")
    with rc.gateway_request("bot"):
        assert ms.handle_message(request())["result"]["tools"]
        monkeypatch.setenv("KHIPU_GATEWAY_TOOLS", "bot:")
        assert payload(ms.handle_message(request("khipu_status"))) == REFUSAL
        assert ms.handle_message(request())["result"]["tools"] == []


@pytest.mark.parametrize("policy,label", [("bot:", "bot"), ("bot", "bot"),
                                         ("bot:*|khipu_status", "bot"),
                                         ("bot:*;bot:khipu_status", "bot"),
                                         ("bot:khipu_status,other:*", "bot"),
                                         ("bot:khipu_status", None)])
def test_empty_malformed_or_unattributed_policy_denies(monkeypatch, policy, label):
    monkeypatch.setenv("KHIPU_GATEWAY_TOOLS", policy)
    monkeypatch.setattr(ms, "_GATEWAY_ACTIVE", True)
    with rc.gateway_request(label) if label else mock.patch.dict("os.environ", {}):
        assert ms.handle_message(request())["result"]["tools"] == []
        assert payload(ms.handle_message(request("khipu_status"))) == REFUSAL


def test_wildcard_keeps_local_only_and_unknown_tools_out(monkeypatch):
    monkeypatch.setenv("KHIPU_GATEWAY_TOOLS", "bot:*")
    with rc.gateway_request("bot"):
        names = {t["name"] for t in ms.handle_message(request())["result"]["tools"]}
        assert names == {t["name"] for t in ms.TOOLS} - ms._LOCAL_ONLY
        for name in (*ms._LOCAL_ONLY, "unknown_tool"):
            assert payload(ms.handle_message(request(name))) == REFUSAL


@pytest.mark.parametrize("policy,label", [(None, "bot"), ("other:", "bot"), ("", "bot")])
def test_unconfigured_label_keeps_existing_dispatch(monkeypatch, policy, label):
    if policy is not None:
        monkeypatch.setenv("KHIPU_GATEWAY_TOOLS", policy)
    monkeypatch.setitem(ms.TOOL_FUNCS, "khipu_status", lambda args: {"ok": True})
    with rc.gateway_request(label):
        assert payload(ms.handle_message(request("khipu_status"))) == {"ok": True}
        names = {t["name"] for t in ms.handle_message(request())["result"]["tools"]}
        assert names == {t["name"] for t in ms.TOOLS} - ms._LOCAL_ONLY
        assert ms.handle_message(request("unknown_tool"))["error"]["code"] == -32602


def test_local_stdio_ignores_gateway_policy(monkeypatch):
    monkeypatch.setenv("KHIPU_GATEWAY_TOOLS", "broken-policy")
    monkeypatch.setitem(ms.TOOL_FUNCS, "khipu_status", lambda args: {"ok": True})
    assert payload(ms.handle_message(request("khipu_status"))) == {"ok": True}
    assert ms.handle_message(request())["result"]["tools"] == ms.TOOLS


def test_needs_you_already_refuses_gateway_inside_handler(monkeypatch):
    from khipu import needs_you

    reader = mock.Mock(side_effect=AssertionError("owner asks were read"))
    monkeypatch.setattr(needs_you, "waiting", reader)
    with rc.gateway_request("bot"):
        assert ms._tool_needs_you({}) == {
            "available": False,
            "reason": "What waits on the owner is read in a conversation on the owner's Mac."}
        assert payload(ms.handle_message(request("khipu_needs_you")))["available"] is False
    reader.assert_not_called()


def test_http_label_and_batch_each_enforce_policy(monkeypatch, capsys):
    class Handler(gw.Handler):
        token = TOKEN
        tokens = {"bot": "scope-test-token-0123456789abcdef"}
        rate = gw._Rate(1000)
        token_rate = gw._Rate(1000)
        auth_fail_rate = gw._Rate(1000)

    monkeypatch.setenv("KHIPU_GATEWAY_TOOLS", "bot:khipu_status;default:")
    monkeypatch.setattr(gw, "_record_liveness", lambda *a, **k: None)
    handler = mock.Mock(return_value={"ok": True})
    monkeypatch.setitem(ms.TOOL_FUNCS, "khipu_status", handler)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/mcp"
    try:
        status, denied = _post(url, request("khipu_status"))
        assert status == 200 and payload(denied) == REFUSAL
        status, replies = _post(url, [request("khipu_status"), request("khipu_get"),
                                     request("unknown_tool")], token=Handler.tokens["bot"])
        assert status == 200
        assert payload(replies[0]) == {"ok": True}
        assert [payload(r) for r in replies[1:]] == [REFUSAL, REFUSAL]
        handler.assert_called_once_with({})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert not thread.is_alive()
    output = capsys.readouterr()
    assert TOKEN not in output.err + output.out
    assert Handler.tokens["bot"] not in output.err + output.out
    assert 'tool_refused label="bot" tool="khipu_get"' in output.err
    assert rc.gateway_label() is None
