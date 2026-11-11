"""Directions in CLI and MCP messages name screens the current app has."""

import ast
from pathlib import Path

import pytest


@pytest.mark.parametrize("filename", ["mcp_server.py", "forget.py", "capture_stores.py", "routing.py"])
def test_message_literals_do_not_name_the_removed_routing_screen(filename):
    path = Path(__file__).resolve().parents[1] / "khipu" / filename
    # Parsing ignores comments and decodes escapes such as \u2192, including
    # adjacent literals and the literal parts of f-strings.
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    stale = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and any(place in node.value for place in ("Settings → Routing", "Settings -> Routing"))
    ]
    assert not stale, f"{filename}: removed Routing screen named on lines {stale}"
