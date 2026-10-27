"""Install-pack leads from the 2026-10-26 Grok triage (Install section)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import integrations as integ
from khipu import session_capture as sc


class _Home(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="install-leads-"))
        for p in (
            mock.patch.object(integ, "HOME", self.home),
            mock.patch.object(integ, "CLAUDE_JSON", self.home / ".claude.json"),
            mock.patch.object(integ, "CLAUDE_SETTINGS", self.home / ".claude" / "settings.json"),
            mock.patch.object(integ, "CURSOR_MCP", self.home / ".cursor" / "mcp.json"),
            mock.patch.object(integ, "CURSOR_HOOKS", self.home / ".cursor" / "hooks.json"),
            mock.patch.object(integ, "AEGIS_TOML", self.home / ".grok" / "config.toml"),
            mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(self.home / ".config" / "khipu")}),
        ):
            p.start()
            self.addCleanup(p.stop)


class CursorProjectUninstallTest(_Home):
    def test_project_uninstall_leaves_the_global_pack(self):
        proj = self.home / "repo"
        proj.mkdir()
        (self.home / ".cursor").mkdir()
        integ.install("cursor")
        self.assertIn("khipu", json.loads((self.home / ".cursor" / "mcp.json").read_text()).get("mcpServers", {}))
        integ.install("cursor", project=str(proj))
        integ.uninstall("cursor", project=str(proj))
        mcp = json.loads((self.home / ".cursor" / "mcp.json").read_text())
        self.assertIn("khipu", mcp.get("mcpServers", {}), "global Cursor MCP entry was removed by a --project uninstall")


class AegisUninstallTest(_Home):
    def test_uninstall_removes_the_memory_backend_install_wrote(self):
        import tomllib

        token = integ.gateway_token_file()
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text("not-a-real-token\n")
        (self.home / ".grok").mkdir()
        (self.home / ".grok" / "config.toml").write_text('[other]\nkey = 1\n')
        integ.install("aegis")
        installed = tomllib.loads((self.home / ".grok" / "config.toml").read_text())
        self.assertEqual((installed.get("memory") or {}).get("backend"), "khipu", "precondition: install wrote [memory]")
        integ.uninstall("aegis")
        after = tomllib.loads((self.home / ".grok" / "config.toml").read_text())
        self.assertNotEqual((after.get("memory") or {}).get("backend"), "khipu", after)
        self.assertNotIn("khipu", after.get("memory") or {}, after)


class CorruptHeartbeatTest(unittest.TestCase):
    def test_corrupt_heartbeat_is_not_reported_as_no_session_yet(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"KHIPU_CAPTURE_HOME": td}):
            sc._write_beat("cursor", {"at": sc._mint_ts(), "last_queued_at": sc._mint_ts()})
            path = sc.dispatch_path("cursor")
            self.assertTrue(path.is_file())
            path.write_text("{")
            out = sc.liveness("cursor")
            self.assertFalse(out.get("ok") and out.get("seen") is False and not out.get("reasons"), out)

    def test_healthy_heartbeat_clears_unreadable_marker(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"KHIPU_CAPTURE_HOME": td}):
            path = sc.dispatch_path("cursor")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{")
            self.assertTrue(
                any("heartbeat unreadable" in r for r in sc.liveness("cursor").get("reasons") or []),
                sc.liveness("cursor"),
            )
            sc._heartbeat(
                "cursor",
                {"at": sc._mint_ts(), "event": "stop", "session_id": "s1", "due": False, "new_turns": 0},
            )
            beat = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("_heartbeat_unreadable", beat, beat)
            out = sc.liveness("cursor")
            self.assertTrue(out.get("ok"), out)
            self.assertFalse(any("heartbeat unreadable" in r for r in out.get("reasons") or []), out)


class AegisUserMemoryUninstallTest(_Home):
    def test_uninstall_leaves_user_authored_identical_memory_block(self):
        import tomllib

        token = integ.gateway_token_file()
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text("not-a-real-token\n")
        (self.home / ".grok").mkdir()
        (self.home / ".grok" / "config.toml").write_text(
            '[other]\nkey = 1\n\n[memory]\nbackend = "khipu"\nenabled = true\n'
        )
        integ.install("aegis")
        integ.uninstall("aegis")
        after = tomllib.loads((self.home / ".grok" / "config.toml").read_text())
        self.assertEqual((after.get("memory") or {}).get("backend"), "khipu", after)
