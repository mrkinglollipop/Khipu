"""Three places swallowed an error without a trace. Each now leaves one line naming
only the exception class, never a message or any record content."""
from __future__ import annotations

import pytest

from khipu import mcp_server, routing, routing_delegation, routing_page_file


@pytest.fixture
def capture_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    return tmp_path / "capture" / "logs" / "prompt-recall.log"


def test_a_routing_page_that_cannot_be_written_leaves_one_line(capture_home, monkeypatch, tmp_path):
    monkeypatch.setattr(routing_page_file, "path", lambda: tmp_path / "routing" / "page.txt")

    def boom(*_a, **_k):
        raise RuntimeError("owner said: SECRET words from the view")

    monkeypatch.setattr(routing_page_file, "render", boom)
    assert routing_page_file.write({"folded_at": None}) is False
    lines = capture_home.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and "[khipu-routing-page] routing page not written: RuntimeError" in lines[0]
    assert "SECRET" not in lines[0]


def test_the_status_routing_field_that_fails_leaves_one_stderr_line(monkeypatch, capsys):
    monkeypatch.setattr(mcp_server, "_via_https_gateway", lambda: False)

    def boom(**_k):
        raise ValueError("SECRET routing record text")

    monkeypatch.setattr(routing, "light_status", boom)
    assert mcp_server._routing_light() == {}
    err = capsys.readouterr().err
    assert "[khipu-mcp] khipu_status routing field left out: ValueError" in err and "SECRET" not in err


def test_a_subagent_page_push_that_raises_leaves_one_line(capture_home, monkeypatch):
    def boom(*_a, **_k):
        raise KeyError("SECRET agent brief")

    monkeypatch.setattr(routing, "child_page_push", boom)
    payload = {"session_id": "s-parent", "hook_event_name": "PreToolUse", "tool_name": "Read",
               "tool_input": {"prompt": "SECRET brief text that is long enough to be a brief"}}
    assert routing_delegation._agent_tool_use(payload, {"KHIPU_FEATURE_ROUTING_PUSH": "1"}, "agent-1") == ({}, None, None)
    lines = capture_home.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "[khipu-delegation-hook]" in lines[0] and "session=s-parent" in lines[0]
    assert "outcome=error:child_page_push:KeyError" in lines[0] and "SECRET" not in lines[0]
