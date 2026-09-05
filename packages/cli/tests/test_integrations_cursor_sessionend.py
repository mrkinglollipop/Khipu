"""Cursor sessionEnd hook coverage (audit harness matrix: "Cursor: Stop,
PreCompact only"). Cursor's own app bundle names a `sessionEnd` hook event
(verified 2026-09-05 by grepping workbench.desktop.main.js), so the khipu
pack should install the stop hook there too — the "quit without
stopping/compacting" net, same rationale as Claude Code's SessionEnd.

Follows the temp-HOME pattern in tests/test_integrations.py.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import integrations as integ


class _TempHomeCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="khipu-integ-sessionend-"))
        self._patches = [
            mock.patch.object(integ, "HOME", self.home),
            mock.patch.object(integ, "CURSOR_MCP", self.home / ".cursor" / "mcp.json"),
            mock.patch.object(integ, "CURSOR_HOOKS", self.home / ".cursor" / "hooks.json"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()


class CursorSessionEndTest(_TempHomeCase):
    def _seed(self):
        (self.home / ".cursor").mkdir()
        (self.home / ".cursor" / "hooks.json").write_text(json.dumps(
            {"version": 1, "hooks": {"sessionEnd": [{"command": "\"/x/on-quit.sh\"", "timeout": 10}]}}))
        (self.home / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": {}}))

    def test_install_adds_stop_hook_entry_under_sessionend_alongside_legacy(self):
        self._seed()
        integ.install("cursor")
        h = json.loads((self.home / ".cursor" / "hooks.json").read_text())
        se = h["hooks"]["sessionEnd"]
        self.assertEqual(len(se), 2)
        commands = [e["command"] for e in se]
        self.assertIn("\"/x/on-quit.sh\"", commands)          # legacy untouched
        ours = [e for e in se if "khipu-stop-hook" in e["command"]]
        self.assertEqual(len(ours), 1)
        self.assertEqual(ours[0]["command"], integ.stop_hook())
        self.assertEqual(ours[0]["timeout"], 20)
        # stop / preCompact also got the entry, same as before this change.
        self.assertTrue(any("khipu-stop-hook" in e["command"] for e in h["hooks"]["stop"]))
        self.assertTrue(any("khipu-stop-hook" in e["command"] for e in h["hooks"]["preCompact"]))

    def test_install_is_idempotent_no_duplicate_entries(self):
        self._seed()
        integ.install("cursor")
        again = integ.install("cursor")
        self.assertEqual(again["changes"], [])
        h = json.loads((self.home / ".cursor" / "hooks.json").read_text())
        ours = [e for e in h["hooks"]["sessionEnd"] if "khipu-stop-hook" in e["command"]]
        self.assertEqual(len(ours), 1)

    def test_status_reports_hook_sessionend_true(self):
        self._seed()
        integ.install("cursor")
        st = integ.status("cursor")
        self.assertTrue(st["hook_sessionend"])

    def test_status_reports_hook_sessionend_false_before_install(self):
        self._seed()
        st = integ.status("cursor")
        self.assertFalse(st["hook_sessionend"])

    def test_uninstall_removes_only_ours_from_sessionend(self):
        self._seed()
        integ.install("cursor")
        integ.uninstall("cursor")
        h = json.loads((self.home / ".cursor" / "hooks.json").read_text())
        self.assertEqual(h["hooks"]["sessionEnd"], [{"command": "\"/x/on-quit.sh\"", "timeout": 10}])
        st = integ.status("cursor")
        self.assertFalse(st["hook_sessionend"])


if __name__ == "__main__":
    unittest.main()
