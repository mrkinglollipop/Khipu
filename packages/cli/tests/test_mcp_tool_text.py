"""Tool text and docs must match what the code does (usage cost, gateway forget rule, stale-base routing writes)."""
from __future__ import annotations

from pathlib import Path

from khipu import mcp_server

ROOT = Path(__file__).resolve().parents[3]


def _tool(name):
    return next(t for t in mcp_server.TOOLS if t["name"] == name)


def test_usage_blurb_does_not_say_no_tokens_are_spent():
    # usage._CLAUDE_TURN_ARGS runs a Haiku turn at most once per 30 min per login; docs/mcp-tools.md says so.
    desc = _tool("khipu_usage")["description"]
    assert "no tokens spent" not in desc and "no model call" not in desc


def test_docs_do_not_describe_the_gateway_forget_as_a_four_name_deny_list():
    for rel in ("docs/mcp-tools.md", "docs/operations.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "because the gateway does not check that the episode came from a cloud harness" not in text, rel


def test_gateway_forget_tool_text_names_the_per_token_rule():
    desc = _tool("khipu_forget")["description"]
    assert "token" in desc.lower(), "the blurb still says any cloud-harness capture"


def test_rule_tool_text_does_not_say_the_write_is_live_when_a_stale_base_is_not_applied():
    for name in ("khipu_route_rule", "khipu_route_priority"):
        desc = _tool(name)["description"]
        assert "stale" in desc.lower(), name
