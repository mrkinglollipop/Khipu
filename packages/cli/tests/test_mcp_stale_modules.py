"""A stdio MCP server outlives deploys: after a fast-forward its lazy imports
must not mix module versions (2026-10-08, "cannot import name 'cache_thread'
from 'khipu.t3'")."""
from __future__ import annotations

import os
import sys

import pytest

import khipu
from khipu import mcp_server as mcp


@pytest.fixture
def server(tmp_path, monkeypatch):
    saved_modules = dict(sys.modules)
    saved_attrs = dict(vars(khipu))
    monkeypatch.setattr(khipu, "__path__", [*khipu.__path__, str(tmp_path)])
    monkeypatch.setattr(mcp, "_SEEN_MTIMES", {})
    monkeypatch.setattr(mcp, "_RELOAD_ON_CHANGE", True)
    calls = {}

    def tool(args):
        module = __import__(f"khipu.{args['module']}", fromlist=["x"])
        return {"value": getattr(module, "VALUE")}

    monkeypatch.setitem(mcp.TOOL_FUNCS, "zz_tool", tool)

    def call(module):
        out = mcp.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "zz_tool", "arguments": {"module": module}}})
        calls["last"] = out
        return out["result"]

    yield tmp_path, call
    sys.modules.clear()
    sys.modules.update(saved_modules)
    for name in set(vars(khipu)) - set(saved_attrs):
        delattr(khipu, name)
    for name, value in saved_attrs.items():
        setattr(khipu, name, value)


def _deploy(tmp_path):
    old = tmp_path / "zz_stale_a.py"
    old.write_text("VALUE = 'a'\nNEW_NAME = 'new'\n")
    later = os.stat(old).st_mtime + 10
    os.utime(old, (later, later))
    (tmp_path / "zz_stale_b.py").write_text("from khipu.zz_stale_a import NEW_NAME\nVALUE = NEW_NAME\n")


def test_call_after_deploy_loads_one_consistent_version(server):
    tmp_path, call = server
    (tmp_path / "zz_stale_a.py").write_text("VALUE = 'a'\n")
    assert not call("zz_stale_a").get("isError")
    _deploy(tmp_path)
    result = call("zz_stale_b")
    assert not result.get("isError"), result
    assert '"new"' in result["content"][0]["text"]


def test_without_reload_the_deploy_breaks_the_call(server, monkeypatch):
    tmp_path, call = server
    monkeypatch.setattr(mcp, "_RELOAD_ON_CHANGE", False)
    (tmp_path / "zz_stale_a.py").write_text("VALUE = 'a'\n")
    call("zz_stale_a")
    _deploy(tmp_path)
    result = call("zz_stale_b")
    assert result.get("isError") and "cannot import name" in result["content"][0]["text"]


def test_unchanged_code_keeps_modules_loaded(server):
    tmp_path, call = server
    (tmp_path / "zz_stale_a.py").write_text("VALUE = 'a'\n")
    call("zz_stale_a")
    first = sys.modules["khipu.zz_stale_a"]
    call("zz_stale_a")
    assert sys.modules["khipu.zz_stale_a"] is first
